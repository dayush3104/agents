<!-- README.md for feature/livekit-interrupt-handler-<yourname> -->
# LiveKit Agents — Interrupt Handler Feature  
> *Branch:* feature/livekit-interrupt-handler-<yourname>  
> *Author:* <Your Name> — implemented background filler detection & interrupt handling for voice agents

---

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)]()
[![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)]()
[![Status](https://img.shields.io/badge/status-Ready%20for%20review-brightgreen)]()

---

## Table of contents
- [What changed](#what-changed)
- [Design & How it works](#design--how-it-works)
- [What works (verified)](#what-works-verified)
- [Known issues & limitations](#known-issues--limitations)
- [Environment & dependencies](#environment--dependencies)
- [How to run & test locally](#how-to-run--test-locally)
- [Testing checklist (cases to verify)](#testing-checklist-cases-to-verify)
- [Submission deliverables](#submission-deliverables)
- [PR description template (copy into your PR)](#pr-description-template-copy-into-your-pr)

---

## What changed
This feature introduces *background assessment of user speech* to decide whether it's:
1. *Filler* (e.g., "umm", "acha", "you know") — do not interrupt agent while speaking.
2. *Meaningful interruption* — immediately interrupt agent and respond.

Main changes:
- examples/voice_agents/basic_agent.py — *updated* (interrupt handler + filler detection).
- A lightweight runtime admin API (/update_filler) to update filler lists per language.
- Logging improvements: concise human-friendly console logs + JSON audit log file.
- Configurable parameters (via .env) for timing thresholds and confidence.

---

## Design & How it works
High-level flow:

1. *During agent speaking*
   - The system *captures* ASR transcripts immediately and stores the latest transcript (no blocking).
   - A tiny background assessment window (ASSESS_WINDOW, default 0.12s) consolidates partial transcripts.
   - After the window the transcript is *assessed* (fast tokenization + set lookups).
     - If filler → *do nothing; agent continues speaking with **no audible pause*. Console: FillerDetectedWhileSpeaking - ignored.
     - If meaningful → session.interrupt() is called *immediately* (debounced) and the agent responds. Console: MeaningfulInterruptionDetected - interrupting agent.
2. *When agent is NOT speaking*
   - User speech is buffered until SILENCE_AFTER_SPEECH (default 0.8s).
   - After silence:
     - If filler-only → agent asks a clarifying question: “I didn’t catch a request — what exactly would you like me to do?”
     - If meaningful → agent responds to the user utterance.
3. *Runtime configuration*
   - Update filler lists via HTTP endpoint POST /update_filler (JSON body: {"room":"<room>", "ignored_words":"a,b,c", "language":"en"}) or via the LLM callable function update_ignored_words.

---

## What works (verified)
- Background assessment while agent speaking — *no blocking* on the TTS stream.
- Accurate multi-word and single-word filler detection (English + Hindi defaults).
- Immediate interruption for meaningful user speech (with small debounce to prevent repeated stops).
- When quiet, detection and follow-up behavior (clarify vs respond).
- Logs: concise console messages for reviewers + JSON audit file (enhanced_agent_logs.jsonl).

---

## Known issues & limitations
- Partial ASR behavior: if the ASR provider returns many extremely short partial transcripts, tuning ASSESS_WINDOW may be required.
- Accuracy depends on ASR quality — low-confidence transcripts are ignored (config ASR_CONFIDENCE_THRESHOLD).
- The current filler lists are heuristic; you may need to extend them for domain-specific phrases.
- If TTS provider has delays in stopping, there may be a small audible cut when interrupting (we sleep a tiny amount before generating reply to let TTS stop).

---

## Environment & dependencies
*Tested with*
- Python 3.10 / 3.11 (recommend >=3.10)
- pip packages listed in repo pyproject.toml / requirements.txt (LiveKit Agents + plugins)
  - livekit-agents (>=1.0)
  - python-dotenv
  - silero plugin (or as provided by LiveKit plugins)
  - openai / cartesia package keys required to use those providers

*Important environment variables* (.env):
```ini
# optional overrides
IGNORED_WORDS=uh,umm,hmm,ah,er,uhh,ummm,mhmm,mmh,mhm,like,okay,you know
ASR_CONFIDENCE_THRESHOLD=0.65
SILENCE_AFTER_SPEECH=0.8
INTERRUPT_DEBOUNCE=0.15
ASSESS_WINDOW=0.12

# production keys (required to connect to LiveKit, OpenAI, etc.)
LIVEKIT_URL=...
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
OPENAI_API_KEY=...
CARTESIA_API_KEY=...
JSON_LOG_PATH=enhanced_agent_logs.jsonl
ADMIN_HTTP_PORT=8088