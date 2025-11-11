# 🔊 LiveKit Agents - Interrupt Handler Feature  
> **Branch:** `feature/livekit-interrupt-handler-ayush-dixit`  
> **Author:** Ayush Dixit  
> **Main file updated:** `examples/voice_agents/basic_agent.py`

---

![feature-banner](https://img.shields.io/badge/feature-interrupt--handler-blue)  
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)]() [![Status](https://img.shields.io/badge/status-ready%20for%20review-brightgreen)]()

---

## 🌟 Overview  
This feature introduces **background filler detection** and **intelligent interrupt handling** for LiveKit voice agents.  
The agent now **ignores filler words** (like *“acha”*, *“umm”*) while speaking — without pausing — and **instantly stops** when meaningful interruptions occur.

> 🧠 **Result:** A voice agent that talks naturally, feels human, and reacts instantly.

---

## 🧭 Motivation  

**🎯 Goal:** Enable a **natural, human-like conversation** flow by removing unnecessary pauses and enabling instant reactions.

✅ Eliminates mid-sentence pauses caused by filler words  
✅ Ensures instant stop on meaningful user input (e.g., “stop”, “wait”)  
✅ Keeps interaction seamless and professional

---
## 🧩 Core Concept 
🎙️ USER speaks
     ↓
ASR generates partial transcripts
     ↓
Agent logic decides ⤵️
  ├── Filler word (uh, acha, hmm...) ➜ IGNORE (continue speaking)
  └── Meaningful interruption (stop, question...) ➜ INTERRUPT immediately



---

## ⚙️ Configuration (from `.env` / `env.example`)

Example .env (create from env.example)
IGNORED_WORDS=uh,umm,hmm,ah,er,uhh,ummm,mhmm,mmh,mhm,like,okay,you know,acha
ASR_CONFIDENCE_THRESHOLD=0.65
SILENCE_AFTER_SPEECH=0.8
INTERRUPT_DEBOUNCE=0.15
ASSESS_WINDOW=0.12
ADMIN_HTTP_PORT=8088
JSON_LOG_PATH=enhanced_agent_logs.jsonl

Required API keys (set in local .env)
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
OPENAI_API_KEY=
CARTESIA_API_KEY=
DEEPGRAM_API_KEY=
ELEVEN_API_KEY=


---

## 🏗️ File & Folder Structure  

agents/
├─ examples/
│ ├─ voice_agents/
│ │ ├─ basic_agent.py # UPDATED — background filler detection + interrupt handler
│ │ ├─ README_ayush_dixit.md # Feature documentation (this file)
│ │ └─ ...
├─ env.example # Environment variable template
└─ .gitignore # Updated to exclude logs and .env files


---

## 🔍 Implementation Details

### 🧠 1. Background Speech Assessment  
Uses **ASSESS_WINDOW (default 0.12s)** to accumulate partial STT results while the agent speaks.  
Analysis runs asynchronously — no blocking, no pauses.

---

### 💬 2. Behavior by State  

| Agent State | Input Type | Action |
|--------------|-------------|---------|
| Speaking | Filler word | Continue speaking (ignore) |
| Speaking | Meaningful | Interrupt immediately |
| Silent | Filler word | Ask clarification |
| Silent | Meaningful | Respond normally |

---

### ⚡ 3. Key Components  

- **Filler detection:** `is_filler_only()` and `split_fillers()`  
- **Interrupt debounce:** `InterruptController` (avoids rapid re-triggers)  
- **Background task scheduling:** Managed via `asyncio` timers  
- **Runtime API:** Update filler words dynamically via `/update_filler`

---

## 🧠 Architecture Diagram  

┌─────────────────────────────────────────────┐
│           🎤 Live Audio Input               │
└──────────────┬──────────────────────────────┘
               │
               ▼
      [ASR Partial Transcripts Stream]
               │
               ▼
 ┌─────────────────────────────────────────────┐
 │   Background Assessment (non-blocking)      │
 │  ├─ Detect filler vs meaningful words       │
 │  ├─ Debounce interrupts                     │
 │  └─ Trigger session.interrupt() if needed   │
 └─────────────────────────────────────────────┘
               │
               ▼
       [Agent TTS / Reply Behavior]


---

## 🧪 Testing Scenarios  

### 🎙️ Agent Speaking  

| Case | Input | Expected Behavior | Console Log |
|------|--------|------------------|--------------|
| 1 | “umm”, “acha”, “haan” | Continues speaking without pause | `FillerDetectedWhileSpeaking - ignored` |
| 2 | “stop”, “wait”, “listen” | Instantly interrupts | `MeaningfulInterruptionDetectedWhileSpeaking - interrupting agent` |

### 🤖 Agent Silent  

| Case | Input | Expected Behavior | Console Log |
|------|--------|------------------|--------------|
| 3 | “umm”, “acha” | Asks clarification | `FillerDetectedPostSilence - asking clarification` |
| 4 | “what can you do?” | Responds normally | `UserSpeechPostSilence - responding` |

---

## 🧰 How to Run Locally  

**Create virtual environment & install dependencies**

python -m venv .venv

Activate venv
.venv\Scripts\Activate.ps1 # Windows

or source .venv/bin/activate for Linux/macOS
pip install -r requirements.txt


**Setup environment**

cp env.example .env


**Run agent**

python examples/voice_agents/basic_agent.py console


**Quick Test Scenarios**

| State | Words | Expected |
|--------|--------|-----------|
| Agent speaking | “umm”, “acha” | Continues speaking |
| Agent speaking | “stop”, “wait” | Interrupts instantly |
| Agent silent | “acha” | Asks clarification |
| Agent silent | “what can you do?” | Responds smartly |

---

## 🔗 Admin Runtime API  

**Update filler words dynamically at runtime**

curl -X POST http://localhost:8088/update_filler \
  -H "Content-Type: application/json" \
  -d '{"room":"mock_room","ignored_words":"acha,achha,haan","language":"hi"}'



**Response:**

{"ok": true, "updated": ["acha", "achha", "haan"]}


---

## 🧾 Example Logs  

2025-11-11 18:39:20 | INFO | User speech detected while speaking - assessing in background
2025-11-11 18:39:20 | INFO | FillerDetectedWhileSpeaking - ignored
2025-11-11 18:39:24 | WARNING | MeaningfulInterruptionDetectedWhileSpeaking - interrupting agent
2025-11-11 18:39:24 | INFO | Responding after meaningful interruption
2025-11-11 18:39:27 | INFO | FillerDetectedPostSilence - asking clarification


---

## 🧾 Feature Summary Table  

| Component | Description | Type |
|------------|--------------|------|
| Background Assessment | Detects filler vs meaningful while speaking | Async task |
| InterruptController | Prevents rapid repeats | Utility |
| Admin API | Runtime filler management | HTTP server |
| JSON Logs | Structured behavior audit | File output |
| Console Logs | Human-readable summary | Terminal output |

---

## 🧭 Reviewer Notes  

- Logs simplified for easy review.  
- Full audits in `enhanced_agent_logs.jsonl`.  
- Verified on Windows 11 + Python 3.11 (PowerShell).

---

## 💡 Future Improvements  

- Adaptive filler learning (contextual awareness)  
- Emotion-based interruption prediction  
- Automated test harness for user interruptions  

---

## 🙋 Author  

**Ayush Dixit**  
B.Tech, IIT Kanpur  
🌍 Rajasthan, India  
📧 Contact: via GitHub Discussions or project thread

---

## 📝 Salescode PS Submission Details  
For the complete **submission-format README** (including implementation details, testing steps, environment setup, and PR checklist),  
👉 **[click here to view README_ayush_dixit.md »](examples/voice_agents/README_ayush_dixit.md)**  

## 🏁 Final Note  

This feature ensures the LiveKit voice agent feels **smoother, faster, and more human-like**.  
It maintains continuous TTS playback while staying responsive to user speech.

✨ **No more awkward pauses - just seamless, intelligent conversations!** ✨






