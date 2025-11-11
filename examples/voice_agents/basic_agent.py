#!/usr/bin/env python3
"""
Enhanced LiveKit voice agent — synchronous/no-pause speaking behavior with background assessment.

Behavior:
- While agent SPEAKING:
    * Capture transcripts immediately, store latest, set a short ASSESS_WINDOW timer (default 0.12s).
    * After ASSESS_WINDOW of no new partial transcript, run assessment in background:
        - If filler -> DO NOTHING (agent continues speaking). Console: FillerDetectedWhileSpeaking - ignored.
        - If meaningful -> immediate session.interrupt() (debounced) and then respond. Console: MeaningfulInterruptionDetected - interrupting agent.
- While agent NOT speaking:
    * Buffer user speech until SILENCE_AFTER_SPEECH then respond or ask clarification (existing behavior).
- Multi-word fillers supported; runtime updates via admin API / LLM tool still work.
- Clean console logs + JSON audit logs.
"""
import argparse
import asyncio
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

# LiveKit imports (must be installed)
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    RoomOutputOptions,
    RunContext,
    WorkerOptions,
    cli,
    metrics,
)
from livekit.agents.llm import function_tool
from livekit.plugins import silero
from livekit.agents.voice import AgentStateChangedEvent, UserInputTranscribedEvent

load_dotenv()

# -------------------------
# Configuration
# -------------------------
ASR_CONFIDENCE_THRESHOLD = float(os.getenv("ASR_CONFIDENCE_THRESHOLD", "0.65"))
SILENCE_AFTER_SPEECH = float(os.getenv("SILENCE_AFTER_SPEECH", "0.8"))
INTERRUPT_DEBOUNCE = float(os.getenv("INTERRUPT_DEBOUNCE", "0.15"))
ASSESS_WINDOW = float(os.getenv("ASSESS_WINDOW", "0.12"))  # small wait to gather partials while speaking
ADMIN_HTTP_PORT = int(os.getenv("ADMIN_HTTP_PORT", "8088"))
JSON_LOG_PATH = os.getenv("JSON_LOG_PATH", "enhanced_agent_logs.jsonl")

# -------------------------
# Logging (concise console + JSON)
# -------------------------
logging.getLogger("livekit").setLevel(logging.WARNING)
logging.getLogger("livekit.agents").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

class HumanFormatter(logging.Formatter):
    def __init__(self, fmt: str = "%(asctime)s | %(levelname)-7s | %(message)s", datefmt: str = "%Y-%m-%d %H:%M:%S"):
        super().__init__(fmt=fmt, datefmt=datefmt)

class JsonLineFormatter(logging.Formatter):
    def __init__(self, datefmt: str = "%Y-%m-%dT%H:%M:%S"):
        super().__init__(fmt="%(asctime)s", datefmt=datefmt)
    def format(self, record):
        base = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra"):
            base.update(record.extra)
        return json.dumps(base, default=str)

logger = logging.getLogger("enhanced-agent")
logger.setLevel(logging.INFO)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(HumanFormatter())
logger.addHandler(ch)

jh = logging.FileHandler(JSON_LOG_PATH)
jh.setLevel(logging.INFO)
jh.setFormatter(JsonLineFormatter())
logger.addHandler(jh)

def _log(level: str, msg: str, **extra):
    if extra:
        logger.log(getattr(logging, level.upper()), msg, extra={"extra": extra})
    else:
        logger.log(getattr(logging, level.upper()), msg)

def log_info(msg: str, **extra): _log("info", msg, **extra)
def log_warn(msg: str, **extra): _log("warning", msg, **extra)
def log_error(msg: str, **extra): _log("error", msg, **extra)

# -------------------------
# Tokenization / helpers
# -------------------------
TOKEN_RE = re.compile(r"[^\s.,!?;:()\"']+")

def tokenize_lower(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in TOKEN_RE.findall(text)]

def contains_devanagari(text: str) -> bool:
    return any("\u0900" <= ch <= "\u097F" for ch in text)

# -------------------------
# Filler lists (default)
# -------------------------
DEFAULT_EN_FILLERS = {
    "uh", "umm", "hmm", "ah", "er", "uhh", "ummm", "mhmm", "mmh", "mhm", "you know", "like", "okay"
}
DEFAULT_HI_FILLERS = {
    "haan", "achha", "acha", "achhaah", "theek", "thik", "haanji", "haan ji", "arrey", "arre", "bas", "toh"
}

def split_fillers(fillers: Set[str]) -> Tuple[Set[str], Set[Tuple[str,...]]]:
    singles = set()
    multis = set()
    for f in fillers:
        toks = tuple(tokenize_lower(f))
        if not toks:
            continue
        if len(toks) == 1:
            singles.add(toks[0])
        else:
            multis.add(toks)
    return singles, multis

def is_filler_only(transcript: str, singles: Set[str], multis: Set[Tuple[str,...]]) -> Tuple[bool, List[str]]:
    toks = tokenize_lower(transcript)
    if not toks:
        return False, []
    joined = " ".join(toks)
    for m in multis:
        if " ".join(m) == joined:
            return True, [" ".join(m)]
    i = 0
    n = len(toks)
    matched = []
    multis_sorted = sorted(multis, key=lambda x: -len(x))
    while i < n:
        matched_any = False
        for m in multis_sorted:
            L = len(m)
            if i + L <= n and tuple(toks[i:i+L]) == m:
                matched.append(" ".join(m))
                i += L
                matched_any = True
                break
        if matched_any:
            continue
        if toks[i] in singles:
            matched.append(toks[i])
            i += 1
            continue
        return False, matched
    return True, matched

# -------------------------
# Interrupt debounce controller
# -------------------------
class InterruptController:
    def __init__(self, min_interval: float = INTERRUPT_DEBOUNCE):
        self._lock = threading.Lock()
        self.min_interval = min_interval
        self._last = 0.0
    def should_interrupt(self) -> bool:
        with self._lock:
            now = time.time()
            if now - self._last >= self.min_interval:
                self._last = now
                return True
            return False

# -------------------------
# Admin API & session registry
# -------------------------
_SESSION_REGISTRY: Dict[str, AgentSession] = {}
_SESSION_LOCK = threading.Lock()

def register_session(room: str, session: AgentSession):
    with _SESSION_LOCK:
        _SESSION_REGISTRY[room] = session
    log_info("Registered session", room=room)

def get_session(room: str) -> Optional[AgentSession]:
    with _SESSION_LOCK:
        return _SESSION_REGISTRY.get(room)

class AdminHTTPRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, obj: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._send_json(400, {"error": "invalid json"})
            return
        if self.path == "/update_filler":
            room = body.get("room")
            ignored_csv = body.get("ignored_words", "")
            language = body.get("language", None)
            if not room:
                self._send_json(400, {"error": "room required"})
                return
            session = get_session(room)
            if not session:
                self._send_json(404, {"error": "session not found", "room": room})
                return
            new_set = {w.strip().lower() for w in (ignored_csv or "").split(",") if w.strip()}
            if language:
                session.userdata.setdefault("ignored_words_by_lang", {})[language] = new_set
            else:
                session.userdata["ignored_words"] = new_set
            log_info("Admin updated filler list", room=room, language=language or "default", updated=sorted(new_set))
            self._send_json(200, {"ok": True, "updated": sorted(new_set)})
            return
        self._send_json(404, {"error": "unknown path"})

def start_admin_server(port: int = ADMIN_HTTP_PORT):
    def serve():
        httpd = HTTPServer(("0.0.0.0", port), AdminHTTPRequestHandler)
        log_info("Admin HTTP server started", port=port)
        httpd.serve_forever()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t

# -------------------------
# Agent & tools
# -------------------------
class MyAgent(Agent):
    def __init__(self):
        super().__init__(instructions=(
            "You are Kelly, a concise voice assistant. Keep replies brief and clear. No emojis or markdown."
        ))
    async def on_enter(self):
        self.session.generate_reply(instructions="Greet the user briefly and ask how you can help.")
    @function_tool
    async def update_ignored_words(self, context: RunContext, ignored_words_csv: str, language: Optional[str] = None):
        new_set = {w.strip().lower() for w in (ignored_words_csv or "").split(",") if w.strip()}
        if language:
            context.session.userdata.setdefault("ignored_words_by_lang", {})[language] = new_set
            log_info("LLM updated filler list", language=language, updated=sorted(new_set))
        else:
            context.session.userdata["ignored_words"] = new_set
            log_info("LLM updated default filler list", updated=sorted(new_set))
        return {"updated": sorted(new_set), "language": language or "default"}

# -------------------------
# Prewarm: load VAD
# -------------------------
def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    log_info("Prewarm: Silero VAD loaded")

# -------------------------
# Buffer helpers (when agent quiet)
# -------------------------
def schedule_process_after_silence(session: AgentSession, room_name: str, delay: float = SILENCE_AFTER_SPEECH):
    loop = asyncio.get_running_loop()
    pending = session.userdata.get("pending_task")
    if pending:
        try:
            pending.cancel()
        except Exception:
            pass
    handle = loop.call_later(delay, lambda: asyncio.create_task(process_user_buffer(session, room_name)))
    session.userdata["pending_task"] = handle

async def process_user_buffer(session: AgentSession, room_name: str):
    buf = session.userdata.get("user_buffer", {})
    text = buf.get("text", "").strip()
    is_filler = buf.get("is_filler", False)
    matched = buf.get("matched", [])
    session.userdata["pending_task"] = None
    if not text:
        return
    if is_filler:
        log_info("FillerDetectedPostSilence - asking clarification", room=room_name, transcript=text, matched=matched)
        try:
            await session.generate_reply(instructions="I didn't catch a request — what exactly would you like me to do?")
        except Exception as e:
            log_error("generate_reply failed", error=str(e), room=room_name)
    else:
        log_info("UserSpeechPostSilence - responding", room=room_name, transcript=text)
        try:
            await session.generate_reply(instructions=f"User said: {text}")
        except Exception as e:
            log_error("generate_reply failed", error=str(e), room=room_name)
    session.userdata["user_buffer"] = {}

# -------------------------
# Entrypoint: main logic with background assess while speaking (no pause)
# -------------------------
async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": getattr(ctx.room, "name", "<unknown>")}
    room_name = ctx.log_context_fields["room"]
    log_info("Entrypoint starting", room=room_name)

    # start admin server once
    if not getattr(entrypoint, "_admin_started", False):
        start_admin_server(ADMIN_HTTP_PORT)
        entrypoint._admin_started = True

    env_ignored = {w.strip().lower() for w in os.getenv("IGNORED_WORDS", "").split(",") if w.strip()}
    userdata = {
        "ignored_words": env_ignored,
        "ignored_words_by_lang": {"en": set(DEFAULT_EN_FILLERS), "hi": set(DEFAULT_HI_FILLERS)},
        "user_buffer": {},
        "pending_task": None,
        # speaking-background assessment fields:
        "speaking_latest_transcript": "",
        "speaking_latest_conf": 1.0,
        "speaking_assess_handle": None,
    }

    interrupt_ctrl = InterruptController()

    class State:
        def __init__(self):
            self._lock = threading.Lock()
            self._speaking = False
            self._unknown_warned = False
        def set_speaking(self, speaking: bool) -> bool:
            with self._lock:
                changed = (self._speaking != speaking)
                self._speaking = speaking
                if changed:
                    self._unknown_warned = False
                return changed
        def is_speaking(self) -> bool:
            with self._lock:
                return self._speaking
        def should_warn_unknown(self) -> bool:
            with self._lock:
                if not self._unknown_warned:
                    self._unknown_warned = True
                    return True
                return False

    state = State()

    session = AgentSession(
        stt="assemblyai/universal-streaming:en",
        llm="openai/gpt-4.1-mini",
        tts="cartesia/sonic-2:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
        turn_detection="vad",
        vad=ctx.proc.userdata["vad"],
        allow_interruptions=True,
        userdata=userdata,
    )

    register_session(room_name, session)

    VALID_STATES = {"speaking", "listening", "idle", "thinking", "silent"}

    @session.on("agent_state_changed")
    def _on_agent_state(ev: AgentStateChangedEvent):
        raw = (getattr(ev, "state", None) or getattr(ev, "state_name", None) or "")
        s = str(raw).lower().strip()
        if s in VALID_STATES:
            speaking = (s == "speaking")
            changed = state.set_speaking(speaking)
            if changed:
                log_info(f"AgentState: {s}", room=room_name)
        else:
            if state.should_warn_unknown():
                log_warn("AgentState: unknown state (logged once)", room=room_name, state=s)

    # Background assessor called after ASSESS_WINDOW when agent is speaking
    async def bg_assess_while_speaking(session: AgentSession, room_name: str):
        """
        This runs after ASSESS_WINDOW of no new partial transcript.
        It inspects session.userdata['speaking_latest_transcript'] and decides.
        """
        latest = session.userdata.get("speaking_latest_transcript", "").strip()
        conf = session.userdata.get("speaking_latest_conf", 1.0)
        # clear handle
        session.userdata["speaking_assess_handle"] = None
        if not latest:
            return
        if conf < ASR_CONFIDENCE_THRESHOLD:
            log_info("ASR low-confidence ignored (speaking bg)", conf=round(conf,3), text=latest, room=room_name)
            return

        lang = "hi" if contains_devanagari(latest) else "en"
        ignored_by_lang = session.userdata.get("ignored_words_by_lang", {})
        lang_fillers = set(ignored_by_lang.get(lang, set()))
        fallback = set(session.userdata.get("ignored_words", set()))
        merged_fillers = {f.lower() for f in lang_fillers} | {f.lower() for f in fallback}
        singles, multis = split_fillers(merged_fillers)
        all_filler, matched = is_filler_only(latest, singles, multis)

        if all_filler:
            # filler -> do nothing, continue speaking
            log_info("FillerDetectedWhileSpeaking - ignored", room=room_name, transcript=latest, matched=matched)
            return

        # non-filler -> interrupt immediately (debounced) and respond
        if interrupt_ctrl.should_interrupt():
            log_warn("MeaningfulInterruptionDetectedWhileSpeaking - interrupting agent", room=room_name, transcript=latest)
            try:
                session.interrupt()
            except Exception as e:
                log_error("session.interrupt failed", error=str(e), room=room_name)
            # short breathing time then respond
            try:
                await asyncio.sleep(0.08)
                log_info("Responding after meaningful interruption", room=room_name, transcript=latest)
                await session.generate_reply(instructions=f"User said: {latest}")
            except Exception as e:
                log_error("generate_reply after interrupt failed", error=str(e), room=room_name)
        else:
            log_info("Interruption suppressed (debounce) while speaking", room=room_name, transcript=latest)

    # schedule background assessment (cancel previous if exists)
    def schedule_speaking_assessment(session: AgentSession, room_name: str, delay: float = ASSESS_WINDOW):
        loop = asyncio.get_running_loop()
        prev = session.userdata.get("speaking_assess_handle")
        if prev:
            try:
                prev.cancel()
            except Exception:
                pass
        handle = loop.call_later(delay, lambda: asyncio.create_task(bg_assess_while_speaking(session, room_name)))
        session.userdata["speaking_assess_handle"] = handle

    # Synchronous transcription handler: store latest and schedule assessment when speaking,
    # or buffer & schedule when not speaking.
    @session.on("user_input_transcribed")
    def _on_transcribed(ev: UserInputTranscribedEvent):
        transcript_raw = (getattr(ev, "transcript", "") or "").strip()
        confidence = getattr(ev, "confidence", 1.0)
        if not transcript_raw:
            return
        # If agent is speaking -> update latest transcript and schedule a short assessment (non-blocking)
        if state.is_speaking():
            # store latest quickly
            session.userdata["speaking_latest_transcript"] = transcript_raw
            session.userdata["speaking_latest_conf"] = confidence
            # schedule background assessment after ASSESS_WINDOW (cancels previous)
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(lambda: schedule_speaking_assessment(session, room_name, ASSESS_WINDOW))
            # Immediately log that speech was received while speaking (concise)
            log_info("User speech detected while speaking - assessing in background", room=room_name, transcript=transcript_raw)
            return

        # Agent not speaking -> previous behavior: buffer and process after silence
        if confidence < ASR_CONFIDENCE_THRESHOLD:
            log_info("ASR low-confidence ignored", conf=round(confidence,3), text=transcript_raw, room=room_name)
            return

        # prepare merged fillers
        lang = "hi" if contains_devanagari(transcript_raw) else "en"
        ignored_by_lang = session.userdata.get("ignored_words_by_lang", {})
        lang_fillers = set(ignored_by_lang.get(lang, set()))
        fallback = set(session.userdata.get("ignored_words", set()))
        merged_fillers = {f.lower() for f in lang_fillers} | {f.lower() for f in fallback}
        singles, multis = split_fillers(merged_fillers)
        all_filler, matched = is_filler_only(transcript_raw, singles, multis)

        # buffer and schedule processing after silence
        session.userdata["user_buffer"] = {
            "text": transcript_raw,
            "last_ts": time.time(),
            "is_filler": all_filler,
            "matched": matched,
            "confidence": round(confidence,3),
        }
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(lambda: schedule_process_after_silence(session, room_name, SILENCE_AFTER_SPEECH))
        if all_filler:
            log_info("FillerDetectedWhileQuiet - will ask clarification after silence", room=room_name, transcript=transcript_raw, matched=matched)
        else:
            log_info("UserSpeechWhileQuiet - will respond after silence", room=room_name, transcript=transcript_raw)

    # metrics
    usage_collector = metrics.UsageCollector()
    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent):
        try:
            metrics.log_metrics(ev.metrics)
            usage_collector.collect(ev.metrics)
        except Exception:
            log_error("Error logging metrics", room=room_name)

    async def _log_usage():
        try:
            s = usage_collector.get_summary()
            log_info("Usage summary", summary=s, room=room_name)
        except Exception:
            log_error("Error getting usage summary", room=room_name)
    ctx.add_shutdown_callback(_log_usage)

    # safe connect
    try:
        await ctx.connect()
    except Exception:
        pass

    # start session
    await session.start(
        agent=MyAgent(),
        room=ctx.room,
        room_input_options=RoomInputOptions(),
        room_output_options=RoomOutputOptions(transcription_enabled=True),
    )
    log_info("Session started", room=room_name)

# -------------------------
# CLI
# -------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("mode", nargs="?", default=None)
    args = parser.parse_args()

    if args.test:
        log_info("Quick test: imports and logging OK")
        print("Quick test complete — check console logs for behavior.")
    else:
        cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
