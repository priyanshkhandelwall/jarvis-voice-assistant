# JARVIS architecture

Feature codes (F1, F2, ...) and rule codes (R1, R2, ...) used here and in code comments are the project's own labels for each feature and design rule.

JARVIS is a voice assistant for a Windows 10/11 laptop (full features) or a Mac (most features). Python 3.11, a Flet window, local models first, and one online service (Groq) used only when a question needs it. This page shows where things live and how one command travels through the system.

## Folder map

| Folder | What it holds |
|---|---|
| `main.py` | Entry point: re-launches under `pythonw.exe` (no console), then opens the dashboard. |
| `jarvis/controller/` | **F17 core state machine and F7 trust levels.** The only owner of state. Holds the transition table, events, permits, confirmations and the fixed trust table. |
| `jarvis/orchestrator.py` | Glue for one interaction: rudeness filter → interpreter → controller → executor → speech + log. |
| `jarvis/interpreter/` | Text → `Answer` or `ActionRequest`. Matchers are tried in registration order; the first match wins. |
| `jarvis/router/` | General intent router: fuzzy cue words from `settings/intents.toml`, then an optional online *classifier* (it never runs anything itself). |
| `jarvis/actions/` | Local commands: F1 intro, F3 open app, F4 file finder and index, F16a weather, clock, greetings, F5 "why", conversation (Groq, F2). |
| `jarvis/voice/` | F2 voice input: microphone (`audio_input`), "hey jarvis" wake word, Whisper speech-to-text, the listener loop, global hotkeys. |
| `jarvis/speech/` | Her voice (Kokoro, falling back to Zira), Groq Q&A (`qa.py`), F14 mood, rudeness, claim checks, spoken-length rules. |
| `jarvis/logbook/` | F5 Live Activity and decision records, with redaction of secrets before anything is written. |
| `jarvis/dashboard/` | F6 Flet window: panels, gauges, state display, confirmation previews, background start. |
| `jarvis/memory/` | F10 long-term memory (JSON store), with the never-store check for passwords, PINs and cards. |
| `jarvis/screen/` | F8 screen capture (private windows blacked out), Windows OCR, screen commands. |
| `jarvis/vision/` | F9 webcam, YOLOX object detector, price table. |
| `jarvis/files/` | F19 reads PDF/Word/text/CSV locally; summaries go online only after a preview. |
| `jarvis/news/` | F20 topic monitor and F21 briefing (RSS feeds). |
| `jarvis/browser/` | F16b Google searches opened in your own Chrome. |
| `jarvis/system/` | F22 volume, brightness, Wi-Fi, lock, restart, shutdown. |
| `jarvis/youtube/` | F18 YouTube search in Chrome and transcript summaries. |
| `jarvis/online/` | R1 shared online helper (timeout, retry, cooldown) and the LOCAL/ONLINE indicator (F13). |
| `jarvis/security/` | R9 path check used before any file is read or opened. |
| `jarvis/platform/` | What differs between Windows and macOS: `current()` picks `WindowsPlatform` or `MacPlatform` from `sys.platform`. The macOS side uses `open`, `osascript`, `pmset` and `networksetup`; Windows-only libraries are imported only on Windows. |
| `jarvis/settings/` | `settings.toml` (every tunable number and phrase), `intents.toml`, `intro.txt`, `prices.csv`. |
| `jarvis_data/` | Created at run time: memory, topics, logs, caches. Git-ignored. |
| `models/` | Downloaded by `scripts/download_models.py`: wake word, Whisper, Kokoro, YOLOX. Git-ignored. |
| `tests/` | pytest suite (offline, everything external mocked); `conftest.py` guards real data and real apps. |
| `scripts/` | `download_models.py` (run once by `setup.bat`) and `eval_camera_votes.py` (detector evaluation). |
| `setup.bat`, `run.bat` | Windows: one-time setup (Python 3.11 check, venv, packages, models, `.env`) and start. |
| `setup.sh`, `run.sh` | macOS: the same steps (`bash setup.sh`, `bash run.sh`). |

## One command, end to end

```
 microphone ──► wake word ("hey jarvis") ──► Whisper (local speech-to-text)
                                                   │ text            typed box ─┐
                                                   ▼                            │
                                    Orchestrator.handle_command ◄───────────────┘
                                                   │ rudeness filter, mood note
                                                   ▼
                     Interpreter: fixed matchers ─► intent router ─► Groq answer (last)
                                                   │
                         Answer ──────────────────►│◄────────── ActionRequest
                                                   ▼
                   Controller: ACTION_REQUESTED ─► trust policy (fixed table)
                        L0/L1: permit now   │   L2/L3: preview + "confirm"   │   L4: two confirms
                                                   ▼
                                Executor (opens app, saves memory, ...) ─► verify (R12)
                                                   ▼
                          Speech (Kokoro) + Live Activity + decision record (F5)
```

## States (F17)

`SLEEPING → (wake: greeting) SPEAKING → AWAKE → LISTENING → UNDERSTANDING → (CONFIRMING) → ACTING → VERIFYING → SPEAKING → AWAKE`

An answer that needs no action goes from UNDERSTANDING straight to SPEAKING. "Go to sleep" ends in SLEEPING.

- **SLEEPING**: only a wake phrase does anything (conversation and sleep mode, F2/F15).
- **AWAKE**: waiting for a command.
- **LISTENING**: hearing a command.
- **UNDERSTANDING**: interpreting it.
- **CONFIRMING**: an L2–L4 preview waits for "confirm" or "cancel"; it expires on its own.
- **ACTING**: running the executor, which needs a one-time permit.
- **VERIFYING**: checking the result really happened.
- **SPEAKING**: saying the result.

Any transition that isn't in the fixed table is denied by default. A Stop (hotkey or spoken) wins over everything else.

## Where each feature lives

| Feature | Main code |
|---|---|
| F1 Self introduction | `actions/intro.py`, `settings/intro.txt` |
| F2 Voice interaction | `voice/`, `speech/`, `actions/conversation.py` |
| F3 Open any application | `actions/open_app.py`, `actions/app_finder.py` |
| F4 File finder | `actions/find_file.py`, `actions/file_index.py` |
| F5 Live activity log / "why" | `logbook/`, `actions/explain.py` |
| F6 Dashboard | `dashboard/` |
| F7 Trust levels | `controller/trust.py`, `controller/policy.py`, `controller/controller.py` |
| F8 Screen awareness | `screen/` |
| F9 Webcam vision | `vision/` |
| F10 Long-term memory | `memory/` |
| F13 Local-first mode | `online/status.py` |
| F14 Personality | `speech/mood.py`, `speech/rudeness.py`, `actions/greeting.py`, `actions/wake_greeting.py` |
| F15 Sleep mode | `actions/sleep_wake.py`, `controller/` |
| F16 Live web / weather | `speech/qa.py` (live search), `actions/weather.py` |
| F16b Web search in Chrome | `browser/commands.py` |
| F17 Core state machine | `controller/` |
| F18 YouTube tools | `youtube/` |
| F19 File processor | `files/` |
| F20 Topic monitor | `news/topics.py` |
| F21 Morning briefing | `news/briefing.py` |
| F22 System settings | `system/` |
| Intent router | `router/`, `settings/intents.toml` |
