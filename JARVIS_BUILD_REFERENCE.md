# JARVIS: Build Reference

**Version 2, 3 October 2026**
**Audience:** the AI coding agent that builds JARVIS.
**Owner:** Priyansh (the developer).

JARVIS stands for "Just A Rather Very Intelligent System". She is the developer's personal AI operating system for his own Windows laptop, with her own identity and personality. She is not a chatbot. (The spec uses "she", as in the developer's own wording.)

---

## 0. How to use this file

1. Read this whole file before writing any code. Re-read the sections that apply before starting each feature.
2. This file is the authority. If a request or your own idea conflicts with it, stop and tell the developer. Do not quietly pick a side.
3. Words used here:
   - **MUST / MUST NOT**: binding.
   - **SHOULD**: the default. Deviate only if you state the reason.
   - **DEFAULT**: the developer has not confirmed this. Use it unless told otherwise, and mention it in your report.
   - **OPEN**: undecided. Ask the developer before building anything that depends on it.
4. Do not add features that are not in Section 6. Do not remove or weaken a rule.
5. Do not invent facts. If you don't know whether a library, model or setting works on this laptop, run it and see. Say what you tested and what you didn't.
6. Only change this file when the developer asks.

---

## 1. What is being built

- `python main.py` opens JARVIS as her own **native desktop window** (the dashboard). It is not a browser tab.
- She listens for "Hey JARVIS", talks like a person, opens apps, finds files, sees the screen and the room through the webcam, and remembers the developer. (Keyboard, mouse and terminal control was F11, cut on 28 October 2026.)
- Her core runs on the laptop for free. She goes online only when needed, and always says so first.
- The dashboard is the main thing people will see, so it must look and feel as good as possible.
- Everything she does is visible on the dashboard: what she heard, what she did, why, and which sensors are on.

---

## 2. Environment and constraints

### 2.1 Developer's laptop

| Item | Value |
|---|---|
| Machine | HP Pavilion, Windows (exact version not recorded) |
| RAM | 16 GB (upgraded from 8 GB, which used to hang). Baseline use with nothing open has not been measured since the upgrade. |
| GPUs | NVIDIA GeForce GTX 1650 Ti (4 GB dedicated VRAM) and an integrated AMD Radeon (shares system RAM) |
| CPU | Not recorded |
| Camera | Built-in laptop webcam |
| Storage | C: NVMe SSD (fast), D: SATA hard disk (slow) |
| Microphone | Earphone microphone during demos |

During a demo, only the code editor and the JARVIS window are running, plus any app JARVIS opens.

### 2.2 The tutor's laptop is unknown

The project is marked by a tutor who runs it, after running `requirements.txt` first. Do NOT assume the tutor's laptop has an NVIDIA GPU, a D: drive, a webcam, Chrome, Word, a Google login, an API key or internet. Every feature that needs one of these MUST follow the missing-setup rule (R14): say what is missing and exactly what to do and where, and everything else MUST keep working.

### 2.3 Cost

**Zero cost.** Only free tools, free models and free API tiers. See rule R1.

### 2.4 Language and delivery

- The main program is Python. Another language is allowed only if it is essential and the developer approves it.
- `requirements.txt` with **pinned versions**.
- `README.md` with exact run steps, including anything pip cannot install (separate programs, model downloads). Provide one script that downloads the models before first run.
- **Minimum mode** (DEFAULT): JARVIS starts and works with no optional libraries, no models, no internet and no API keys. Typed commands, the dashboard, logging and the deterministic commands work. A typed-command box is the backup when the microphone fails.
- The assignment is marked on structure and documentation. Write all code fresh for this project. Do not copy code from other projects or from earlier assessments.

---

## 3. Non-negotiable rules

**R1. Zero cost.** JARVIS never uses a paid service on her own. If a free service ends, hits its limit or asks for payment, she tells the developer, switches that feature off and carries on locally.
- Every online call has an explicit timeout (`[online] request_timeout_seconds`, DEFAULT 10) and at most one automatic retry. A rate-limit reply (HTTP 429) switches that service off for `[online] rate_limit_cooldown_seconds` (DEFAULT 300, or the service's own Retry-After value if it sends one). She says once that the free limit is reached, the dashboard shows the service as LIMITED with the time it returns, and calls during the cooldown go straight to the fallback without touching the network. Tests: mocked 429 → the second call makes no request; mocked hang → fallback within the timeout.

**R2. Local-first.** Speech in and out, conversation, commands, file search, screen reading, webcam vision and memory run on the laptop with free local tools. Online is an extra. In conversation mode (F2, F15) every utterance is transcribed on the laptop: while she is SLEEPING nothing she hears is acted on or sent online, and while she is AWAKE a request goes online only through R3, never a fragment she ignored.

**R3. Online is never silent.** She may choose an online source for a question herself, but before she does she says a short line ("Searching online"; she never says a provider's name), the dashboard shows ONLINE, and the Live Activity panel shows only `searching online, sending "<words>"` — the exact words sent, with **no provider or model name**. The provider and model (for example "Groq, openai/gpt-oss-20b") are recorded **only** in the decision record and `jarvis_data/logs/debug.log`; "why did you do that?" still says she searched online, without naming the provider. No user-visible string (spoken, Live Activity, Internet panel) names the provider, model or search engine behind it (Groq, gpt-oss, browser_search, Exa, Open-Meteo), except: the missing-key setup messages, which must name the key to add or check (`GROQ_API_KEY`); the **Open-Meteo credit line** "Weather data by Open-Meteo.com (CC BY 4.0)", which its licence requires; and the **host name of a website** the developer asked to open or that an answer was read from (for example google.com, bcci.tv), which is on screen anyway (amended 16 October 2026). R4 takes precedence for laptop data; content is never logged.

**R4. Nothing leaves the laptop by surprise.**
- The developer's own spoken question can be sent as he said it (L0).
- Values the developer typed into settings for an online purpose (the weather city, the monitored topics) count as his own words (L0). They are still logged per R4: type, size, destination, time and hash, never the value.
- Anything that comes from the laptop (a screenshot, webcam photo, file, document, clipboard, text read off the screen, memory contents), or a query built from it, MUST NOT be sent until the dashboard preview panel shows **exactly** what will be sent and the developer confirms (L3).
- Previews:
  - Images: the actual image as it will be sent, after any crop or resize.
  - Text: the exact text.
  - Files: name, size and contents. For long documents, the page count, and the developer approves the whole file or picks a part.
- Save what is being sent as a temporary file, show that file, and send **that same file**, checking with a hash that it has not changed. Delete the temporary copy afterwards.
- The developer can crop, cut, edit or cancel before confirming.
- The log records only that something was sent: type, size, destination, time and hash. Never the content.
- Never send Sleep Mode audio, passwords or anything from a password field.

**R5. No telemetry.** JARVIS sends no analytics or usage data anywhere.

**R6. One controller.** The core state machine (F17) is the only thing that changes JARVIS's state. See Section 4.

**R7. Permits.** An action runs only with a one-time permit from the controller. See F17.

**R8. Stop always wins.** "Stop" (and a **system-wide** keyboard hotkey as backup, key combination in `[hotkeys] stop`, chosen by the developer: Ctrl+Shift+X) is a top-priority interrupt, not a queued command. See Section 5.
- The hotkey is registered with Windows' RegisterHotKey (via ctypes or the already-pinned pywin32), which reports only that combination and never watches other keys. If registration fails, fall back to the in-window key, and she says and the dashboard shows: '<combo> is already used by another app. Change [hotkeys] stop in settings.toml, then restart JARVIS.' (<combo> is the configured combination, e.g. Ctrl+Shift+X). F15's wake hotkey uses the same mechanism and the same message with its own key.
- Stop also ends speech immediately: the current sentence is cut off and any queued sentences are discarded.

**R9. Filesystem rule.** One shared function checks every path, and every module MUST call it. No module does its own path check.
- Anything that **changes** files (create, write, save, rename, move, copy, delete, and any path given to a terminal command) works only inside **approved folders**.
- **Read-only** actions (search, open, show) can reach any folder except a **protected list** (for example password stores and browser profile data).
  - ~~The protected list includes `jarvis_data/browser_profile` (F16b's own browser profile).~~ Removed 27 October 2026 with JARVIS's own browser (F16b now opens searches in the developer's own Chrome). `[filesystem] protected_project_folders` stays, empty.
- Every path is turned into a full real path before checking, including following shortcuts and links, so `..` and links cannot escape.
- Never overwrite an existing file without asking.
- When she works through another app's own window (for example typing a filename into Word's Save dialog), she only types a path inside an approved folder. She cannot police what the app itself does, and the docs must say so.

**R10. Terminal rule.**
- Only **fixed command templates** from an allowlist.
- The program builds the command itself from checked parameters. It MUST NEVER run text that came from the developer's speech or from a model as a command.
- Run without a shell, so characters like `;`, `&&` and `|` do nothing.
- Each template allows only its listed flags. Any other flag or extra argument is refused. Example: `git commit` allows only `-m` with the message as a single value, and `git add` takes only paths.
- Deleting, force options (force-push, hard reset) and system-setting changes are not on the list.
- Anything the developer adds to the list has to be written as a template.
- Show the exact final command before running it, and log it.
- Where Python can do the job directly (for example checking what is using the CPU), use Python, not a terminal command.

**R11. Sensitive data.** JARVIS MUST NEVER record, store, log, send or replay passwords, password-field input, authentication codes, private tokens or API keys. She never types the developer's password. If a login is needed, she stops and asks him to log in.

**R12. Verify every action.** After each action, check that it actually worked (window appeared, file exists, page loaded). If it failed, say "Troubleshooting the issue", retry **once**, then report the outcome, or say plainly that it failed. Never say "done" without checking. Where an action cannot be fully verified (lock, restart, shutdown), she says "requested", never "done".

**R13. Explanations come from records.** Every command produces a structured decision record. "Why did you do that?" is answered from that record, never guessed afterwards. The record is her program's data, not model reasoning text.

**R14. Degrade, don't crash.** A missing library, model, key, camera, microphone, internet or login turns that feature off with a clear dashboard status. The core keeps running.
- **Missing-setup rule.** Every failure message says what is missing and exactly what to do and where (for example 'Add GROQ_API_KEY to the .env file in the JARVIS folder, then try again.'). If the developer can't fix it, it says plainly why (for example 'Windows doesn't allow JARVIS to switch Wi-Fi on this laptop.'). Never just "not available".

**R15. Stay light.** The developer's laptop hung when memory ran low, and the project should never lag.
- Cap the dashboard animation frame rate.
- Animation timers pause while the JARVIS window is minimised or hidden and resume when shown. Gauges keep sampling at the slow rate.
- Load a heavy model only when a feature needs it, and unload it afterwards.
- Keep the camera and screen capture loops off unless asked for.
- Show a live memory gauge. Warn when free memory stays below the threshold for `[dashboard] low_memory_consecutive_samples` (DEFAULT 3) slow ticks in a row, and speak the warning at most once per `[dashboard] low_memory_warning_cooldown_seconds` (DEFAULT 300); the gauge itself still turns red immediately. Switch off heavy features instead of freezing.
- Keep the load on the GPU modest.

**R16. Secrets.** API keys never go in code, in the repo or in what gets submitted. Load them from environment variables or from a local settings file kept out of Git. Provide a `.env.example`. A missing key gets a missing-setup message naming the key and the file to add it to (R14).

**R17. Settings.** Every tunable number (thresholds, timeouts, cooldowns, staleness limits, time-of-day boundaries) lives in one settings file, not in the code.

**R18. Personal data.** The data folder holds personal data and MUST be excluded from Git and from submissions (`.gitignore`).

**R19. No overclaiming.** Price figures are estimates. A mood reading is a guess. Do not state that Siri or Alexa "can't" do something without checking a current source.

**R20. Scope.** Build only what was asked. If you see a useful extra, mention it in your report instead of building it.

---

## 4. Architecture rules

### 4.1 Flow

```
Voice or typed input
  -> Controller (state machine, one event queue)
  -> Interpreter (intent + typed parameters; deterministic first, a model only if needed)
  -> Policy engine (trust level, origin, preview or confirmation)
  -> Permit (one-time, for that exact action)
  -> Executor (fixed actions only)
  -> Verifier (did it work? retry once)
  -> Decision record + dashboard + speech
```

### 4.2 Rules

- **Modules never change JARVIS's state.** They send requests to the controller, which checks them against a fixed table of allowed changes.
- **The current state lives only in the controller.** Everything else can read it but not write it.
- **One event queue, handled by one thread**, so two events cannot change state at once. "Stop" jumps the queue.
- **The dashboard runs on the main thread.** Workers reach it only through thread-safe queues.
- **A model may only return structured data** (an intent and typed parameters). It MUST NOT return code, shell strings or a trust level.
- **The policy engine is deterministic** and never asks a model. Trust levels come from a fixed table in code, never from JARVIS or a model.
- **Deny by default.** An action that is not in the fixed action set is refused and logged.
- **The fixed action set** is: open app, open file, open URL, type text, press key, click, move mouse, move file, run an allowlisted terminal template, capture screen, capture camera. Add to it only with the developer's approval.
  - **Approved additions (3 October 2026):** ~~`browser_open_read`, `browser_interact` (F16b)~~ (removed 27 October 2026: a search is `open_url`); `read_document`, `send_text_online`, `write_text_file` (F18, F19); `edit_topic_list` (F20); `set_jarvis_setting` (F21); `set_volume`, `set_mute`, `set_brightness`, `set_wifi`, `lock_screen`, `restart`, `shutdown` (F22). Each feature section gives its trust level.
  - **Approved additions (23 October 2026, the developer's F10 decisions):** `remember` (L0), `forget_memory` (L1), `forget_all_memory` (L4).
  - **Approved addition (23 October 2026, the developer's F8 decisions):** `send_screen_online` (L3).
  - **Approved addition (23 October 2026, the developer's F9 decisions):** `camera_stream` (L3, the continuous camera).
  - **Added 28 October 2026 with F21:** `briefing_online` (L3, the briefing's online part; Section 5's standing approval needs an action to confirm). `edit_topic_list` (F20) is L0 by the developer's decision.
- **Modules** share one small interface (DEFAULT): `can_handle`, `plan`, `execute`, `health_check`, and `undo` where the action is reversible.
- **Suggested layout** (DEFAULT): `main.py`, a `jarvis/` package with separate folders for the controller, policy, actions, one folder per feature module, dashboard and settings, plus a `tests/` folder.

### 4.3 States

`SLEEPING` (the startup state, F15), `AWAKE` (conversation mode: ready for the next utterance, no wake word needed, F2), `LISTENING`, `UNDERSTANDING`, `CONFIRMING`, `ACTING`, `VERIFYING`, `SPEAKING`. The dashboard always shows the current state. (Renamed 11 October 2026: `ASLEEP` is now `SLEEPING` and `WAITING` is now `AWAKE`.)

---

## 5. Trust levels

Five fixed levels. Every action follows them.

| Level | Covers | Behaviour |
|---|---|---|
| **L0** | Answering questions, including a web search of the developer's own spoken words | Immediate |
| **L1** | Opening apps and websites. A one-time look at the screen or camera when the developer asks. Volume, mute and brightness (F22) | Immediate, no question |
| **L2** | Typing, key presses, mouse. Locking the screen (F22) | One preview of the whole planned sequence, then one confirmation |
| **L3** | Anything JARVIS starts herself or keeps running (camera on for gestures or presence, watching the screen). Sending anything from the laptop online. Turning Wi-Fi on or off (F22) | One "confirm" |
| **L4** | Deleting files. Overwriting existing files. Restart and shutdown | Double confirmation |

Rules:

- **Origin.** Every action is tagged either "developer asked for it" or "JARVIS started it". Only the controller sets the tag, from the spoken command. No module can claim it. This is what separates the same capture at L1 from L3.
- **Confirm.** "Confirm" only works if all of these are true:
  - a confirmation is pending;
  - it has not expired (60 seconds, a starting value kept in settings);
  - it applies to that exact action and its exact parameters (for typing text: the target app and the full text to be typed);
  - the "confirm" came from the developer — typed in the command box, or spoken while she is awake and waiting for it (CONFIRMING, approved 11 October 2026) — never from audio captured while she was SPEAKING or inside the F2 self-echo tail, never while SLEEPING, and never from model output. Test: a "confirm" arriving during SPEAKING is rejected.
  - "Cancel" (typed, or spoken while CONFIRMING) drops the pending action; she says "Cancelled." and a later "confirm" finds nothing to confirm.

  If anything about the action changes, the old confirmation is void and she asks again. A "confirm" with nothing pending does nothing, and she says so.
- **(Removed 1 November 2026: there is no standing approval any more; the briefing runs only on request, F21's amendment. Kept below as history.)** **Standing approval for the startup briefing (F21; built 28 October 2026: action `briefing_online`, checked by the controller).** This is the only standing approval; every other confirmation follows the rules above. The first time the briefing's online part would run, the preview shows the exact outbound list (every destination and every string sent) and the developer gives one "confirm". That approval is bound to a SHA-256 hash of the exact outbound list. On later startups, if the list's hash matches, the online part runs without asking again, still saying "Searching online" and showing ONLINE (R3), and each step still gets its own one-time permit (F17). If the list changes in any way, the approval is void and she asks again with a new preview.
- **Stop.** Works from every state, including while she waits for a confirmation. It cancels pending confirmations, halts what is running, and returns her to a safe state. It cannot undo something already finished (for example text already sent online).
- **Undo.** Each reversible executor, at the moment it succeeds, registers its own reverse step (for example move_file records the original path). The undo stack is newest-first, bounded (`[undo] max_depth`, DEFAULT 10) and session-only (cleared on close, F10). An entry is removed before its reverse step runs, so a failed undo is never retried. "Undo" is a normal request through the controller: the reverse step gets its own trust level and permit, and its paths go through R9. With nothing to undo she says so plainly. Irreversible actions (L4 deletes, anything already sent online) are never put on the stack.

---

## 6. Feature specifications

The foundation features are **F17 (state machine), F7 (trust levels), F5 (log and decision records) and the skeleton of F6 (dashboard)**. Other features depend on them, so build them first.

### F1. Self Introduction
- On "Hey JARVIS, introduce yourself" she says the developer's fixed intro text **word for word, the same every time**.
- The text lives in a text file in the settings folder so it can change without touching code.
- **The intro text is locked.** Use exactly this text, word for word, and put it in the settings text file:

  > Hello everyone. I am JARVIS, which stands for Just A Rather Very Intelligent System. Think of me as a personal assistant with a stage name, and a creator, Priyansh, who wanted a computer that listens. I listen for my name, open apps, find files, read the screen, look through the camera, and remember what matters. Every move I make appears live on this screen, and anything risky needs his permission first. Ready? I am.

- Do not edit, shorten or "improve" the text. It names features that are built later (screen, camera, memory, permissions). If a feature is cut from the final version, the developer will change the text himself.
- No styles (professional, casual and so on) for now.
- **Done when:** the settings text file contains exactly the locked text, the text passed to speech and the text recorded in the log match that file character for character, and a test confirms the text is identical across runs (speech mocked).

### F2. Voice Interaction and Human-Like Conversation
- **Conversation mode (approved 11 October 2026; replaces one wake word per command, merges with F15).** She starts SLEEPING (F15). She wakes on "Hey JARVIS" (wake word), "wake up JARVIS" (matched on the local transcript), the wake hotkey (the press-to-talk combination), or the typed wake phrase, then greets by time of day and is AWAKE.
  - **Every wake, by any route, speaks the greeting at once** (wake word, wake phrase, hotkey, typed — the same greeting logic). Redesigned 16 October 2026: short, warm, personal, time-aware and **varied** — one short greeting plus an optional short follow-up, about 12 words at most, picked at random **locally (never online)** from `[greeting.pools]`, with `{name}` from `[user] name` ("Priyansh"). Which pool, first match wins: **welcome_back** (woken within `[greeting] welcome_back_minutes`, DEFAULT 30, of going to sleep), **late_night** (21:00–04:59, a gentle line), **first_wake** (first wake of the day; a day starts at 05:00), then the time band: **early_morning** 05:00–08:59, **morning** 09:00–11:59, **afternoon** 12:00–16:59, **evening** 17:00–20:59 (band start hours in `[greeting]`). None of the last `[greeting] no_repeat_count` (DEFAULT 5) greetings is repeated; when every line of the chosen pool was said recently, the time-neutral `anytime` pool fills in. A missing or empty pool says "Hello, Priyansh. How can I help?" (`[greeting] fallback`). The last few greetings, the day of the last wake and the last sleep time are kept in `jarvis_data/greeting_state.json` so this survives a restart (nothing she heard is stored). Exactly one greeting per wake, even when the wake word and the transcript both fire for one phrase; it is spoken and shown in Live Activity under "woke up (…)".
  - **Fuzzy name (16 October 2026, laptop log).** On the transcript, a word is her name if it is in `[conversation] name_variants` (javis, charvis, jervis, johnless, travis, jarvas, service, …) or within `[conversation] name_max_edit_distance` (DEFAULT 2) letters of "jarvis" (words of at least `name_min_length`, DEFAULT 4, letters). Wake forms, each needing a name-like word, starting at the first or second word: "wake up <name>", "<name> wake up", "hey <name>", and "make up <name>" / "welcome <name>" as mishearings of "wake up". Plain "wake up" or "hey" never wakes her.
  - The wake-word model scores **every** frame while she is SLEEPING, including while an utterance is being recorded (it used to get none once speech started, so it could never fire on the phrase itself; that, plus a noisy mic keeping every recording open for the full `max_command_seconds`, made "Hey, Javis, wake up" wake her about 10 s late).
  - **Noisy microphones:** the speech threshold rises to `[listen] noise_floor_factor` (DEFAULT 2.0) × the background loudness (20th percentile of the last `noise_floor_seconds`, DEFAULT 3), never below `speech_rms_threshold` nor above `noise_threshold_max_rms` (DEFAULT 2500). Asleep recordings stop at `[listen] max_asleep_utterance_seconds` (DEFAULT 4).
  - **"Wake up" while AWAKE** is answered locally: "I'm already awake." (`[conversation] already_awake_reply`), never sent online.
- **AWAKE:** no wake word is needed. Every utterance (end of speech detected) is a command. Fragments are ignored: fewer than `[conversation] min_words` words (DEFAULT 2, except known one-word commands such as "confirm", "cancel", "stop", "goodnight", "hello"), or Whisper's no-speech probability above `[conversation] max_no_speech_prob` (DEFAULT 0.6). An ignored fragment is logged as "ignored: too short/unclear" and never sent online. A spoken "stop" is the R8 Stop.
- **Going to sleep:** "sleep JARVIS", "go to sleep", "goodnight" or "good night JARVIS" → she says "Goodnight." → SLEEPING. Her name may be misheard in any of the ways above ("Good night, Javis"). Handled locally, never sent to Groq. Optional `[conversation] auto_sleep_minutes` (DEFAULT 0 = off, the developer's choice).
  - **Near misses (23 October 2026).** A typed "got to sleep" went to Groq, which answered "Going to sleep now. Good night." while she stayed awake. `[conversation] sleep_phrases` now also has "go sleep", "goto sleep", "got to sleep", "time for sleep", "time to go to sleep", "you can (go to) sleep", and any phrase containing "sleep" also matches within `[conversation] sleep_max_edit_distance` (DEFAULT 2) letters when something said starts with "sle" ("go to sleap", "gone to sleep"). Phrases without "sleep" ("good night") stay exact, so "good light" never sleeps; questions about sleep ("what is sleep mode", "how much sleep do I need") never match.
- **No false claims (23 October 2026).** An online answer only talks; only JARVIS's own commands act (R12, R13). Every Groq system prompt carries `[claims] prompt_rule` (never say you have done, are doing or will do an action), and every Groq answer is checked locally against `[[claims.rules]]` (first-person claims to sleep, open, close, search, remember, set reminders, play music or change settings). If one matches, the whole answer is replaced with "I can't do that from here. Try saying '<the command that does it>'." ('go to sleep', 'open <app>', 'search for <question>'), or "I can't do that yet." when no command exists. Live Activity shows `removed a claim she can't back up: "<words>"`. A live answer may say it searched; "search for", "search online" and "look up" are live words, so the suggested command really searches.
  - **Willing claims too (23 October 2026, laptop log: "I'm ready to open Notepad", "Sure, I can open Visual Studio Code").** Every rule also matches "I can …", "I'm ready/able/going to …", "Sure, I can …", "I'll …" and "let me …" before the action word.
- **The command anywhere in the sentence (23 October 2026, laptop log).** Before the local matchers run, a command is taken from anywhere in what was said: a verb from `[interpreter] command_verbs` (open, open up, pull up, bring up, launch, start, run → open; find) counts where it starts a clause — at the start, after . ! ? ; : or a comma, or after nothing but `command_leads` words and her name ("can you please", "Jarvis, …"). "This is so annoying, nothing is working, open notepad" opens Notepad locally; "You idiot AI. DUM. SHIT AS. Can you please open chat GPT in Chrome" gets the rude reply and then opens it. "How do I open Chrome", "I can't open it" and "find out who won" are not commands. Rudeness and the mood check-in still apply; the online conversation still gets the words as said, and only if no local command matched.
- **A bare name opens it:** a whole command that is exactly an app or site name from `[app_finder.aliases]`, `system_app_aliases`, `website_fallbacks`, `site_mishearings` or `bare_names` ("VS Code", "Chrome", "chat GPT") opens it; a sentence that only mentions an app never does.
- **Sleep mishearings:** `[conversation] sleep_mishearings` ("gota slide", "go to slide", "gotta sleep", …) are sleep phrases.
- She greets by time of day, using the system clock, **only when greeted** ("hello", "good morning"), answered locally with no Groq call. DEFAULT: morning until 12:00, afternoon 12:00 to 17:00, evening after 17:00 (settings).
- **Time and date (L0, never online; 16 October 2026).** "What time is it", "current time", "tell me the time", "what's the date", "what day is it" are answered from the laptop clock. "<city> time" / "time in <city>" uses `[clock.cities]` (city → Windows time-zone name, read from Windows' own registry through pywin32's `win32timezone`; no download). An unlisted city says which setting to add it to; no pywin32 says what to install (R14). Every Groq request's system prompt includes the current local date and time, so she never says she can't tell the time.
- She answers small talk ("how are you", "how was your day") and any question via Groq's free-tier API (**corrected 2026-09-26** -- see "Chosen stack" below; speech capture/wake-word/STT stay fully local/offline, only this specific answering piece goes online, and only when actually called). Per R3, this must say so: she says "Searching online" during UNDERSTANDING, right before the request leaves the laptop (never "Groq" or any provider name; the log records the destination), and the dashboard shows ONLINE. Per R14, a missing key, no internet, or Groq being down degrades to a spoken message saying exactly what to set up and where (for example 'Add GROQ_API_KEY to the .env file, then try again'); never a crash (R14), and never blocking anything else in the app. Groq answers have markdown stripped before they are shown or spoken.
- **Rude or abusive speech:** she says it was too rude or not a good way to say it, and **still does what was asked**. DEFAULT: start with a simple word and phrase list, and improve it only if testing shows it is needed.
- She has the same personality every time, and the same voice every time: Kokoro af_bella (full precision); Zira only as a fallback when Kokoro isn't installed.
- **Later, not now:** interrupting her while she is speaking.
- **Self-echo guard.** Microphone input is ignored while she is speaking (SPEAKING, or any spoken line) and for a short tail after (`[speech] echo_tail_seconds`, DEFAULT 0.6, raised from 0.3 for conversation mode); the wake-word model is reset when the tail ends. She must never respond to her own voice. Voice interruption is still a later item. Test: speech inside the speaking time or the tail starts no recording and no command.
- **Microphone choice.** The input device is stored by **name** (`[audio] input_device_name`, empty = system default) and looked up when the stream opens, never stored as an index (indices change when devices are plugged in). Clicking the dashboard's Microphone row lists every microphone; picking one switches to it at once and saves its name. If the named device is missing or fails to open, she uses the system default, says so once, and the dashboard microphone panel shows which device is live. Audio is captured at the device's native rate when it is 16, 32 or 48 kHz and decimated to 16 kHz in JARVIS with an anti-aliasing filter; otherwise Windows resamples to 16 kHz. Test: named device absent → default used, no crash (R14).
- **Push-to-talk (L0).** Pressing a key combination (`[hotkeys] push_to_talk`, chosen by the developer: Ctrl+Shift+Space, different from the Stop combination). While SLEEPING it is the wake hotkey. While AWAKE it opens the microphone for **one** command at once, without waiting for speech to be detected; it closes when the command is captured or the listen timeout ends. It is "press to talk", not "hold to talk": the combination is registered with Windows' RegisterHotKey, which reports the press only, and the keyboard is **never polled**.
  - While AWAKE it submits SPEECH_STARTED with the source "push-to-talk" in the payload, so the F17 table decides: it is rejected in any state other than AWAKE (for example SPEAKING).
  - Speech captured after push-to-talk counts as speech while awake everywhere in this spec, including Section 5's Confirm rule.
  - Logged: every press, its source and whether the controller accepted it. If registration fails, she says and the dashboard shows: '<combo> is already used by another app. Change [hotkeys] push_to_talk in settings.toml, then restart JARVIS.' (<combo> is the configured combination, e.g. Ctrl+Shift+Space). The wake word still works.
  - Tests: a press while AWAKE → LISTENING; a press while SLEEPING → she wakes; a press during SPEAKING → rejected; registration failure → that exact message and no crash; the module contains no key-state polling call.
- **Depends on:** F17, F5.

**Chosen stack (developer-approved, resolves decision #3 below):**
- **Wake word:** openWakeWord, using its pre-trained "hey jarvis" model. Local/offline. License: CC BY-NC-SA 4.0 (non-commercial -- fine for this personal-use project, flagged since it's a real restriction).
  - Detection threshold in settings (`[wake_word] threshold`, DEFAULT 0.5). The audio callback only copies frames into a bounded queue (frames are dropped when it is full, it never blocks), and the model runs on its own thread. After a detection, queued frames are discarded and the model state is reset so one phrase fires once. 'Ready' on the dashboard means the model files exist on disk, checked without loading the model. It listens only while SLEEPING. Tuning aid: `[wake_word] debug_scores` (**OFF**, developer 28 October 2026; it was on from 16 October for tuning, and `wake_scores.log` is kept) writes the peak score and median microphone loudness (RMS) of each second to `jarvis_data/logs/wake_scores.log`, plus one timing line per asleep utterance (seconds recorded, why it ended, seconds to transcribe) — numbers and fixed words only, never audio or what was said.
- **Speech-to-text:** faster-whisper, `base` model, CPU int8 by default -- no GPU/CUDA dependency in requirements.txt. Local/offline. License: MIT (Whisper weights). ~148MB download.
  - **Model choice, 23 October 2026 (tools/whisper_benchmark.py on this laptop):** 52 clips of the developer's real commands (Zira and David, clean and with background noise) plus 6 noise-only clips. CPU int8, beam 1, VAD, language en, word error rate (WER) / median per command: `base` 5.6% / 1.05 s (hotword JARVIS only); `base.en` 0.8% / 1.03 s (worst 1.42 s) with app/site hotwords and `without_timestamps`; `small.en` 0.4% / 3.30 s; `distil-small.en` 29.4% / 2.94 s; no model invented text on noise. GPU float16 not run: cuBLAS (`cublas64_12.dll`) isn't installed and CUDA is not to be installed. **Now: `base.en`** (`[models] whisper_size`, 141 MB, same as base) with `[stt] hotwords` = her name plus the app and site names he says ("JARVIS, Chrome, ChatGPT, VS Code, Visual Studio Code, Notepad, YouTube, Spotify, Gmail, GitHub, Priyansh") and `[stt] without_timestamps = true`: with the app names in hotwords, a clip of just "VS Code" looped ("VS Code, VS Code, …") and fell back to slower retries, 16-21 s; without timestamp tokens it took 1.03 s with the same words.
  - Transcribe with faster-whisper's VAD filter on, greedy decoding (beam size 1) and no conditioning on previous text, all in settings, so silence and noise don't produce invented words. Biased toward her name with faster-whisper 1.2.1's `hotwords` (`[stt] hotwords = "JARVIS"`); `initial_prompt` exists too but stays empty, since a sentence there can be invented on noise. Checked 16 October 2026: 0 of 20 synthetic silent/noisy clips produced any text, with or without the hotword. Test on the laptop: 20 silent or noisy clips → count the non-empty transcripts.
- **Text-to-speech (23 October 2026):** **Kokoro, voice af_bella**, full-precision model (`[speech] engine = "kokoro"`; the developer's choice after the samples in `docs/voice-samples/`). kokoro-onnx 0.6.1 (MIT) running Kokoro-82M (Apache-2.0); its phonemizer pieces are GPL-3.0 (eSpeak NG, phonemizer), fine for this personal, educational project. Free, local and offline, CPU only. A required part of JARVIS: pinned in `requirements.txt`, and the model (310 MB) and voices file (27 MB) come from the default `scripts/download_models.py` run.
  - **Why full precision, not int8:** measured on this laptop after warm-up, "Good afternoon." took 0.81 s to synthesise in full precision and 2.6-2.9 s in int8 (ONNX Runtime's CPU ConvInteger kernel is 75% of int8's time and has no fast path; thread settings didn't help). The developer chose full precision for the ~1 s first-sentence target.
  - **Responsiveness:** the model loads and warms up on its own thread at startup and never delays the window (R15). She speaks sentence by sentence, synthesising the next while the current one plays. A long first sentence is cut at a clause break only where the rest of it will be ready before the first part finishes (`[speech] first_chunk_*`), so she never pauses mid-sentence. Measured from "answer ready" to first audio (23 October 2026), live synthesis: greeting 0.8-1.0 s, F1 intro 0.75-0.8 s, a three-sentence Groq answer 2.1-3.8 s (Groq's first sentences ran 68-121 characters).
  - **Short opener (23 October 2026, approved by the developer):** every Groq system prompt asks for a first sentence of at most eight words that directly answers the question, then the details (`[groq] opener_rule`, placed with the length rule; the accuracy, no-false-claims and length rules are unchanged). "Why is the sky blue?" went from 4.5 s to 1.5 s to first audio.
  - **Voice cache (23 October 2026):** her fixed lines are pre-rendered with Kokoro af_bella into `jarvis_data/voice_cache/`, one WAV per line, named by a hash of text + voice + model + speed + language: the F1 intro (whole, one file, no gaps), every wake-greeting pool line and greeting reply (name filled in), "Goodnight.", "I'm already awake.", the rude replies, the mood check-in lines and every setup message with no blanks to fill. Rendered in the background once startup is done, a sentence at a time and only while she is quiet; a changed text, voice or model renders again and outdated files are deleted. Any run of sentences in a line that matches a cached line plays from its file (so a rude-reply prefix or a check-in after a live answer is cached too, and the live part is made meanwhile); a missing or unreadable file means live synthesis (R14). Cached audio goes through the same player, so Stop and the echo guard work exactly as for live speech. Measured: greeting and intro start in 0.00-0.02 s (was 0.8-1.0 s); 59 lines, 11.3 MB, 166 s to render the first time. Never submitted (it rebuilds itself).
  - Stop (button, Esc, Ctrl+Shift+X) cuts mid-sentence within one 50 ms playback block and never waits for synthesis in progress. The echo guard and markdown stripping work as for every engine.
  - **Fallback:** if the Kokoro package or model is missing, she uses pyttsx3 with **Microsoft Zira Desktop**, and the setup message is shown and spoken: "The Kokoro voice model isn't downloaded. Run python scripts\download_models.py in the JARVIS folder, then restart JARVIS. Using the Windows voice until then." (a missing package says to run pip install -r requirements.txt). If Zira isn't installed either (for example on the tutor's laptop), she uses the first installed English voice and the dashboard says how to add Zira (decision E, 2026-10-03).
  - **Optional:** `[speech] engine = "piper"` (piper-tts 1.8.0, GPL-3.0-or-later) from `requirements-voices.txt`, voices from `scripts/download_models.py --voices piper`.
- **Microphone capture (Stage 2):** `sounddevice==0.5.6` (MIT, free; the Windows wheel bundles PortAudio). Approved 2026-10-03.
- **Q&A/small-talk piece:** ~~llama-cpp-python local model~~ **corrected 2026-09-26: Groq's free-tier API instead** (`jarvis/speech/qa.py`, model `openai/gpt-oss-20b` -- verified against Groq's live model list; the more commonly-recommended `llama-3.1-8b-instant` no longer exists on their API as of this date). No local model weights are downloaded for this piece; nothing was ever partially downloaded before this correction landed. Free tier, no credit card: gated by rate limits only (default ~30 requests/min, ~1,000/day per Groq's own docs, varies by model), never a per-token charge -- consistent with R1 (zero cost). Key comes from `GROQ_API_KEY` in `.env` (R16), never in code. A missing key, no internet, or a Groq outage/rate-limit degrades to a spoken message saying exactly what to set up and where (for example 'Add GROQ_API_KEY to the .env file, then try again'); never a crash (R14) -- every other feature (wake word, app-opening, file-finding, ...) keeps working regardless (R14). This is the **only** part of F2 that goes online; everything else above stays local.
  - The Groq system prompt includes a short list of what JARVIS can and cannot do, generated at startup from the Modules panel (built vs pending), plus the instruction to say plainly when asked for something not built. This prevents spoken overclaims (R19).
- **Rudeness detection:** simple word/phrase list (the DEFAULT above), not a model. Local.
- Model name/license/size for each locally-downloaded piece above is reported to the developer before any weights are pulled.

- **Laptop log fixes (27 October 2026):**
  - **Sleep anywhere in the sentence:** a sleep phrase ending a longer sentence ("Thank you, go to sleep", "ok jarvis go to sleep now") and any sentence ending "... to sleep" ("Hijar, VS Code to Sleep") sleep locally; a sentence that starts like a question or another command ("how much should I sleep", "what is sleep mode", "remind me to go to sleep") and "I need/want to sleep" do not (`[conversation] sleep_*`).
  - **Confirm or cancel in a sentence:** while CONFIRMING, a sentence whose only command words are confirm words (confirm, yes, go ahead, send it) or only cancel words (cancel, no, stop) counts, every other word being filler (`[conversation] confirm_words`, `cancel_words`, `confirm_filler_words`): "Please send it online. Confirm." confirms. Both kinds in one sentence: she asks again. Section 5's rules are unchanged.
  - **Length requests:** "in one line", "briefly", "in short", "one sentence" -> exactly one sentence; "in detail" -> up to 6 (`[length]`). The model is told, and JARVIS cuts the answer to whole sentences.
  - **Never claims to see:** every online prompt says she cannot see the camera or screen and to answer "Ask me 'what do you see' and I'll look."; a claim check (`[[claims.rules]]` "see") replaces any answer that says otherwise.
  - **Every result is spoken and shown:** every action's result is spoken and shown in Live Activity as "saying: ..." (session-only when it holds laptop data, so never in debug.log).
  - **Shorter gaps between sentences:** one output stream plays a whole line (the sound device was reopened for every sentence), each sentence's own leading and trailing quiet is trimmed, and about 0.1 s is left between sentences (`[speech] sentence_gap_seconds`, `edge_keep_seconds`).
  - **Microphone:** the saved microphone is "Microphone Array (Realtek(R) Audio)". It changes only when picked in the dashboard list (a click that lands as the list opens is ignored; the activity line says it was picked and how long the switch took). Each way of opening a device gets `[audio] open_timeout_seconds`, at most `open_max_attempts` ways, so a stuck driver can't hold a switch for minutes. A missing saved microphone: the default, said once.
  - **Startup:** voice no longer imports speech-to-text before the microphone opens, the wake word's package no longer pulls in scikit-learn and scipy.stats (a stub for openWakeWord's unused verifier training), and the 48 kHz decimation filter is plain numpy (same taps as scipy's firwin): 1,419 files / 123 MB read before "voice ready" became 332 files / 32 MB.

### F3. Open Any Application
- Opens any app installed on the laptop by voice (L1).
- If the name is also a website (for example YouTube), she checks for an installed app first, and if there is none she opens the site in Chrome.
- **Command clean-up (16 October 2026).** Before any local matcher sees a command, politeness and filler are removed (`[interpreter] filler_phrases` / `filler_words`: please, sir, can you, could you, for me, actually, just, now …), and her name at the start, or at the end after a comma. "Open Chrome please" opens Chrome. What is sent online (R3) is the developer's own words, not the cleaned text.
- **Word-by-word matching (23 October 2026, "Launch Virtual Studio" went online):** app names are also matched word by word; two words of at least `[app_finder] token_min_length` (5) letters count as the same word when 1 − edits/length is at least `token_similarity` (0.7): "virtual" ~ "visual" = 0.71, so "virtual studio" → Visual Studio Code 0.81 against Android Studio 0.54. The 0.75 launch threshold, the 0.1 margin and the shared-word rule still apply, so an unsure match is still asked about.
- **Site mishearings:** `[app_finder.site_mishearings]` maps Whisper's spellings to known sites ("chat chip it", "Chad GPT", "chat gbt" → chatgpt), compared with spaces removed.
- **"Open X on/in Chrome"** (also without "open": "Chat GPD on Chrome please"): if X is a known site (`[app_finder.website_fallbacks]`: youtube, chatgpt, gmail, google, github, wikipedia, …, matched with spaces removed and a small mishearing allowed, "chat gpd/gbt" → chatgpt), she opens its URL in Chrome. An unknown site after "open" says which setting to add it to.
- **"Open Chrome and (then) search X"** opens Chrome, then she says "Searching inside Chrome comes with web access, which isn't built yet." (F16). **When F16b (Stage 3) is built, this line is removed** and the command becomes F16b's search. **27 October 2026:** it opens a Google search for X in the developer's own Chrome (`open_url`, L1) and she says "Searching Google for X in Chrome."
- **Windows Store / UWP apps** (Camera, Photos, Clock, …) are found through `shell:AppsFolder` (read in the background, cached `[app_finder] store_apps_cache_seconds`) and opened by their app ID, under the same 0.75 confidence and ambiguity rules.
- Every step appears in the log (F5). Verify per R12.
- If the app cannot be found, say so plainly.
- **Never a wrong app (11 October 2026; "open VS Code" opened Discord).** Aliases in settings (`[app_finder.aliases]`, e.g. "vs code", "vscode", "code" → Visual Studio Code) are exact matches. Otherwise an app must share a word with the request (a whole word, or a request word of 3+ letters that starts a word of the app name) — letter similarity alone never counts. If the best match scores below `[app_finder] launch_threshold` (DEFAULT 0.75), or the top two are within `[app_finder] ambiguity_margin` (DEFAULT 0.1), she does not launch: she says "I'm not sure which app you mean" and lists the top 3 as a numbered pick, like F4. The pick is valid for the next command only, and Stop clears it. Number words count ("open one", "the second one", "number 2").

- **Laptop log fixes (27 October 2026):** a site is said and shown by name ("Opening ChatGPT in Chrome."), never its URL (`[app_finder.site_names]`; the record keeps the URL). A verb glued to a known name is split ("OpenNote Pad" -> "open notepad"), and your/my/the/a are dropped before matching ("open your YouTube in Chrome"). The false-claim reply suggests "open <app>" only for a known app or site named in his own words; otherwise "I can't do that from here."

### F4. File Finder
- "Find my resume" searches the laptop by name, lists the likely matches, and asks which one to open. If there is one match she opens it.
- **No file paths, ever (23 October 2026).** What she says and what Live Activity shows are file names only, never a path. When two results on a list share a name, the name of the folder each is in is added ("Priyansh Passport .pdf in Downloads", "… in DND"), and the drive letter too only if the folder names are also the same ("in Downloads on D"). The decision record keeps the full path, because it is needed to open the file, but it is never shown or spoken (also not in "why did you do that?" or in a refusal: "I can't open <name>: it's in a protected folder.").
- **Folders too (16 October 2026).** The index holds folder names as well as file names. "Find Jarvis folder" searches folder names only and "find my resume file" file names only; the words "folder"/"file" are not part of the name searched for.
- **Picking from the list:** "open 2", "open one", "the second one", "number two". While a list (F4's files or F3's "which app?") is pending, a number always picks from the list shown **last** — never an app search (laptop log: "open one" after four passport files offered OneDrive/OneNote). A number past the end of the list says how many there are.
  - **23 October 2026 (laptop log):** while a list is pending, "open second", "opens second", "the second file", "and the second file please", "second one please", "number two", "2" and the file's name or part of it ("open Priyansh Passport") all pick from that list, and are matched before every other command. A number inside a name ("resume_2024") is a name, not a pick. Two names that fit equally: "More than one file on the list matches …; say its number." A pick that matches nothing ("the ninth one") is answered locally and the list stays; something that is plainly not a pick ("what time is it", "open chrome") lets the list expire and is handled as a new command. A pick is never sent online.
- Read-only, so it can reach any folder except the protected list (R9). It never indexes a protected folder.
- DEFAULT: use a search index that is refreshed in the background, not a full scan every time, because D: is a slow hard disk.
- **Later, not now:** finding files by what they say.

- **Laptop log fixes (27 October 2026):** she says only how many were found ("I found 4 files. Which one would you like to open?"); the numbered list of file NAMES is shown on screen only, never a folder or a path, with (1), (2) after names that are the same. A numbered list (files, apps, places, memories) stays pickable for `[interpreter] pick_valid_seconds` (120) through unrelated commands, until a pick, a newer list, Stop or sleep; "open number one please", "open file number 1", "open one", "the first one", "number 2", "second" pick from it, and "file" in the words means the file list. A pick with no list pending is answered locally, never searched for as an app.

### F5. Live Activity Log and Explainable Reasoning
- The dashboard shows every step live ("opening Chrome", "typing youtube", "opening YouTube") and whether each step worked.
- Every command is saved as a **decision record**: what she heard, what she understood it to mean, the candidates she found and how well each matched, the choice, the rule or trust level that applied, the origin tag, the result, and the verification result.
- "Why did you do that?" is answered from that record (R13). Also "tell me why …", "why did you …", "explain why …" (`[explain] prefixes`), with filler and her name ignored; never sent online.
- She says what she is doing. If something is slow she says "Troubleshooting the issue", retries once, then reports (R12).
- Records MUST NOT contain sensitive data (R11) or any Sleep Mode words (F15).
- **Retention (RESOLVED, §11 #8):** records are kept in memory for the session only. The one exception is `jarvis_data/logs/debug.log`, a persistent redacted troubleshooting log capped at 1 MB (oldest lines dropped).

- **Laptop log fixes (27 October 2026):** "why do you do that", "why'd you do that", "why did you open that", "how come you did that" are answered locally from the record (`[explain]`). A Stop with no command running is noted in debug.log ("stop while idle"), and a failed online search's real cause (for example "HTTP 400 tool_use_failed ...") is kept in the decision record and debug.log, never in Live Activity (R3).

### F6. Futuristic Dashboard
- This is the most visible part of the project. Make it the best possible.
- A native window, not a browser tab. The UI framework is **Flet** (RESOLVED, §11 #2).
- Contents, all showing **real data**, never decoration:
  - an animated core that reacts to her state and to the voice;
  - a live waveform from real audio data;
  - the activity log (F5);
  - her current state (F17);
  - status panels: microphone, camera, screen capture, browser automation (F16b), and memory (the keyboard and mouse control row was removed on 28 October 2026 with F11);
  - an **Internet** panel in the right column (approved 3 October 2026), in the slot the Skills Registry used to hold. Right column order: Confirmation Queue, Internet, Webcam Feed, Quick Commands. It shows LOCAL (cyan `#00E5FF`), ONLINE (green `#5DEAB0`) or LIMITED until HH:MM (amber `#FFB020`), with the panel label in Orbitron and the value in JetBrains Mono;
  - a preview panel for anything about to leave the laptop (R4);
  - a panel per module, and system gauges (CPU and RAM).
- The state label shows SLEEPING, AWAKE, LISTENING, SPEAKING and the other states (4.3). What she hears while asleep appears in the activity log as "heard while asleep: …" for the session only (F15).
- Performance rules in R15 apply. As built (23 October 2026): the animations (the STOP button's breathing glow and the live waveform; the core sphere itself is static) pause on Flet 1.0's window events MINIMIZE/HIDE and resume on RESTORE/SHOW, with `page.window.minimized` and `page.app_visible` read on the slow tick as a fallback; gauges keep sampling. The RAM gauge turns red on the first low sample; the low-memory warning is shown and spoken only after `[dashboard] low_memory_consecutive_samples` low slow ticks in a row, at most once per `low_memory_warning_cooldown_seconds`.
- **Startup (23 October 2026, R15).** Measured with `tools/measure_startup.py` (median of 3): before, the window appeared at 1.1 s but stayed empty and titled "Flet" until the dashboard was built at 5.3 s, because voice (wake-word model 2.4 s, microphone 0.6 s, libraries 0.5 s) started first. Now the window paints a JARVIS splash (#03101A, cyan orb, "J.A.R.V.I.S." in Orbitron, "starting…", blank title) as soon as Python connects, the dashboard is usable at about 1.7 s, and voice, then the file index, then the Store-app list start in the background with each step in Live Activity (voice ready about 6 s; the Microphone row says "starting…" until then; typing works throughout). Each step's time goes to `jarvis_data/logs/startup_timings.log`. Flet's own native window (grey, titled "Flet") still shows for about 0.3 s before Python connects: hiding it would mean starting the window hidden and showing it ourselves (ShowWindow / foregrounding), which the no-focus rule forbids.
- Controls for features that aren't built (Camera, Screen Capture rows; the Webcam Feed panel; the Keyboard / Mouse row until it was removed with F11 on 28 October 2026) stay on the dashboard and all say the same sentence, on hover, on click and in the panel: "<feature> isn't built yet. It's coming in a later stage." (23 October 2026).
- **Modules panel (23 October 2026).** At the normal 1280×820 window the panel had room for its title only. Its list now scrolls inside the panel with the scrollbar always showing, so every row is reachable; each row is one line (a long name ends in "…", the full name and status show on hover). The room came from column 1 only: 2 px less padding above and below each sensor row, 4 px (was 10) between sensor rows, and 8 px (was 14) bottom padding on column 1's panels (the top padding stays 14 so the three columns' titles still line up). The 3:10:4 split, colours and fonts are unchanged.

### F7. Trust Levels and Safe Actions
- Implement Section 5 exactly. Levels come from a fixed table in code.
- **Tests:** level decisions, origin tagging, confirmation expiry and mismatch, and Stop from every state.

### F8. Screen Awareness
- "What's on my screen?" gets an answer with **no permission question**, because the developer is already asking (L1). She describes what she sees, for example a YouTube video he is watching.
- **Protected windows:** check program names and window titles against a protected list the developer sets (for example password managers, banking sites). Cover the window in the capture, or refuse and say why. She cannot detect a sensitive page inside an ordinary browser tab whose title does not match the list. Say so in the docs.
- **Nothing kept:** the screenshot stays in memory only and is never saved. The log records that the developer asked and which window was read, not the text.
- Anything sent online follows R4.
- **Watching the screen** for a change (for example "tell me when this build finishes") is JARVIS-started and continuous, so it is L3. This is a later item.
- **OPEN:** how the screen is read (text recognition and/or a vision model). Propose options.
- A single capture shows one moment. She does not claim to follow what happens in a video over time.
- **Built 23 October 2026 (the developer's F8 decisions; closes §11 #4 and #12):**
  - **Reading:** Windows' built-in OCR (`Windows.Media.Ocr`) through pywinrt (`winrt-Windows.Media.Ocr` 3.2.1 and 6 sibling packages, MIT, verified on PyPI 23 October 2026). Local, free, no model download; it uses Windows' own OCR languages (en-GB and en-US on this laptop). Measured: 2 lines of synthetic text read exactly in 24 ms.
  - **Capture:** passive only. Pillow 13.0.0 (MIT-CMU) `ImageGrab.grab(all_screens=True)` copies what the desktop shows. Windows are listed with read-only calls (EnumWindows, IsWindowVisible, IsIconic, GetWindowText, GetWindowRect, the process name, DWM's cloaked flag), both under per-monitor DPI awareness so the coordinates match.
  - **Protected windows (§11 #12)**, in `[screen]` and editable:
    - title words: 1password, bitwarden, keepass, lastpass, bank, banking, netbank, commbank, paypal, inprivate, incognito, private browsing, private window, windows security;
    - programs: 1Password, Bitwarden, KeePass/KeePassXC, LastPass, Windows Security.
    - Their **whole rectangle is blacked out inside the capture function, before anything else uses the image** (OCR, the preview, anything sent). Their titles are never spoken or sent.
  - **Nothing kept:** the image stays in memory, is never saved and is freed after use. The log records that he asked and which windows were read, never the text. An executor can now return a separate log line, so the spoken text stays out of the decision record and debug.log.
  - **"what's on my screen" / "read my screen"** (`capture_screen`, L1): a short local summary: "I can see N windows: …. I covered one protected window. The first lines of text say: …." Nothing goes online.
  - **"find <word> on my screen"** (L1, local): "Yes, I can see '<word>' in <window title>." / "I can't see '<word>' on your screen."
  - **"explain my screen" / "help me with this" / "what am I looking at" (§11 #4: OCR locally, plus a vision model online only after the R4 preview):**
    - The blacked-out screenshot, downscaled to at most 1280 px (JPEG quality 70), its OCR text and his question are shown on the dashboard's preview card with "This will be sent online." and, until `[screen] online_terms_checked = true`, "Online service terms not yet checked by you".
    - He says "confirm" (`send_screen_online`, **L3**). Cancel, Stop or the timeout sends nothing and frees the preview.
    - **Model:** `qwen/qwen3.8-27b`, the only model with image input on Groq's live `/models` for the developer's key (23 October 2026). One test call with a synthetic screen: HTTP 200 in 0.5 s, 927 tokens, a correct answer that started with a short sentence. Headers: 1,000 requests/day, 8,000 tokens/minute; the image counted about 2,000 tokens against the per-minute budget. Groq's docs: each image counts as 2048 input tokens, at most 3 images per request, a 20 MB cap for image URLs. With `[screen] vision_model = ""`, only the OCR text is sent, to the normal text model.
    - The answer follows the short-opener rule and the false-claims check, and is spoken. It is not logged (it describes laptop data).
  - **Deviation from R4's temporary file:** F8 itself says the screenshot is never saved, so the preview is kept **in memory**. R4's "the same file is sent" check is kept: the SHA-256 of the exact previewed bytes (image, text and question) is in the confirmed action's parameters and is checked again just before sending; a mismatch sends nothing. Crop and edit in the preview are not built (cancel is).
  - **Dashboard:** the Sensor Array's Screen Capture row is live: cyan and "capturing now" only while a capture runs (shown for at least 1.5 s), otherwise "off - only when you ask". The preview card shows the image and text. Layout, colours and fonts unchanged.
  - **Fixed actions:** `capture_screen` (L1, already in the set) and **`send_screen_online` (L3, added with the developer's F8 decisions)**.

- **Laptop log fixes (27 October 2026):** "what's on my screen" reads every window that is showing on every monitor, each line of text belonging to the window it is in, and names each by what it is: "You have YouTube open in Chrome and a Word document called Report. The document starts with: ..." (`[[screen.apps]]`). Minimised windows are named only (no pixels to read); the desktop ("Program Manager"), the taskbar and JARVIS's own window are never read or named. Capture stays passive and protected windows are still blacked out first.

### F9. Webcam Vision
- **One look (L1, no question):** "what's on my desk?" or a bare "turn on the camera" means one capture. She takes a picture, answers, and turns the camera off straight away. (DEFAULT reading of a bare "turn on the camera".)
  - Capture discards the first `[camera] warmup_frames` (DEFAULT 10) frames while exposure settles, keeps one frame, and releases the camera before analysis begins. A near-black frame (mean brightness below `[camera] min_mean_brightness`, DEFAULT 8 of 255) counts as a failed capture under R12: retry once, then say the camera returned a black image. If the configured camera gives no usable frame, try other cameras up to `[camera] max_index_probe` (DEFAULT 5) only inside that developer-asked capture, never at startup (a startup check would be a JARVIS-started capture, L3).
- **Continuous (L3, one "confirm"):** gestures and desk presence keep the camera running, so the developer must ask for them in words ("watch for gestures", "keep the camera on", "track my presence"). The dashboard shows CAMERA ON. It stays on until "camera off", or until Stop, Sleep Mode or closing JARVIS ends it.
- **Objects and prices:**
  - A local detector names what it can see, with its confidence. Anything it cannot identify is "unknown", with no price.
  - Prices come from a **price table** the developer keeps by hand: object type, low price, high price, currency, date updated, source.
  - An entry is **stale** if it is older than 12 months (a starting value in settings) or has no date. She still gives the range but says it is out of date, and always states the date of the figures she used.
  - She compares ranges to answer "most expensive". If ranges overlap she says they are close and does not name a winner. She never names a winner from stale entries without saying so.
  - She never claims to know real value from a picture.
  - With the developer's confirmation she can look up a more specific price online if he tells her the brand or model. She sends only that name, never the photo, and shows the source and date.
  - **OPEN:** where the starting price figures come from. No free price source has been checked.
- She also scans documents into text, and recognises hand gestures (for example an open palm stops her speaking).
- As far as is known, common detectors only recognise a limited set of everyday object types, so many items will come back "unknown". Test this.
- **Built 23 October 2026 (the developer's F9 decisions; closes §11 #5):**
  - **Camera:** OpenCV, the **headless** build (`opencv-python-headless` 5.0.0.93, Apache-2.0, verified on PyPI 23 October 2026). It's the same library as opencv-python minus its GUI module, so it can never open a window. DirectShow backend.
  - **Detector:** **YOLOX-S** (Megvii, **Apache-2.0**, licence read from the repository 23 October 2026; not Ultralytics/AGPL), the 80 COCO object types, on onnxruntime (already installed). 36 MB, from YOLOX's GitHub release `0.1.1rc0` via `scripts/download_models.py` into `models/yolox/`. Chosen over YOLOX-Tiny on YOLOX's own sample photo: S said bicycle 0.95 / dog 0.91 in 115 ms; Tiny said cat 0.56 / dog 0.54 for the same dog. Two labels on one box: only the surer is kept. The detector is dropped from memory after `[camera] unload_after_idle_seconds` (R15).
  - **One look (L1, `capture_camera`):** "what do you see", "look through the camera", "what's this", "what's on my desk":
    - Windows' camera privacy setting is read first (registry, read-only);
    - `[camera] index` is opened, and other indices up to `max_index_probe` (5) are probed **only inside that capture**;
    - 10 warm-up frames are thrown away, one frame is kept, and the camera is **released before analysis**;
    - a black frame (mean < 8) is retried once, then she says the camera returned a black image;
    - "I can see a laptop and a cup." She says the names; Live Activity shows each name with its confidence.
  - **Prices (§11 #5):** a local, hand-edited CSV, `jarvis/settings/prices.csv` (object, low, high, currency, date_updated, source), with rough **AUD** ranges for 47 common COCO objects. **They were written as rough estimates when F9 was built and are not checked against any store** (the source column says so). "how much is this" → "That looks like a laptop. Roughly $800 to $2,500. Those are rough figures from October 2026." Stale (>12 months) or undated: "…so they're out of date." No entry: "…but I don't have a price for it." A person is never priced.
  - **Continuous (L3, `camera_stream`, one confirm):** "turn on the camera" / "keep the camera on" means a live preview in the **Webcam Feed** panel (5 fps, 320 px JPEG in memory) and detections in Live Activity every 5 s ("camera sees: …"). "turn off the camera", Stop, Sleep and closing JARVIS end it. **This changes the earlier DEFAULT** above (a bare "turn on the camera" = one capture): the developer chose continuous on 23 October 2026.
  - **Never saved:** frames are numpy arrays and JPEG bytes in memory only. There is no OpenCV GUI or file-writing call in `jarvis/vision/` (a static test checks this).
  - **Missing setup (R14):**
    - no camera: "I couldn't find a camera. Check it's plugged in and not being used by another app, then try again.";
    - Windows privacy block: "Windows is blocking the camera. Open Settings, Privacy and security, Camera, and turn on Camera access and Let desktop apps access your camera. Then try again.";
    - no model: "The object detector model isn't downloaded. Run python scripts\download_models.py in the JARVIS folder, then try again."
  - **Dashboard:** the Sensor Array's Camera row is live ("capturing now", cyan) only while the camera is in use; the Webcam Feed panel shows the live preview, or "Camera off. Say 'turn on the camera'." Layout, colours and fonts unchanged.
  - **Not built:** hand gestures, desk presence, document scanning, "most expensive" comparisons, and the online price lookup by brand (none of these were in the developer's F9 list).

- **Laptop log fixes (27 October 2026):** "turn on the camera" (and "can you please turn on camera", which the clean-up had turned into a phrase she didn't know) starts the live preview after the L3 confirm; frames are still never saved. "What can you see" / "what's in front of you" look through the camera. For 2 minutes after a look (`[camera] follow_up_seconds`), "how much does the <object> cost" / "how much is the phone" use the price table, and "where is the <object>" / "... left or right" answer from the box: "From the camera's view, the refrigerator is on the left."

### F10. Long-Term Memory
- **Survives a restart (approved persistent memory):** what the developer told her, and usage patterns he has not turned off, stored in one data folder inside the project. DEFAULT: `jarvis_data`, with the path in settings.
- **Does not survive (session data), cleared on close and never written to memory:** the current conversation, pending confirmations, temporary files, camera frames, screenshots, Sleep Mode transcripts, clipboard contents and her mood guess.
- **Never stored:** passwords, codes, tokens (R11).
- The developer can see, correct or delete anything, and deleted stays deleted.
- Explicit memory (what he tells her) is separate from automatic memory (usage patterns such as which apps and files he opens, kept as counts and timestamps, not content). Anything she notices is stored as a labelled pattern, not as a fact. He can turn automatic tracking off.
- Every memory write is shown in the activity log ('Remembered: <label>'), never silent. Each item stores the date saved and whether it was told or noticed; the memory panel lists newest first. Recall ('what do you know about X') is a local keyword match over stored items, with no model call and no network (R2), and lists what matched with dates.
- The data folder is an approved folder (R9) and is excluded from Git and submissions (R18).
- "Where did I save the invoice?" and "continue where I left off" (reopen the last app, project and file) are part of this feature.
- Keep the live memory on C:. D: can be used for backups (DEFAULT).
- **Built 23 October 2026 (the developer's F10 decisions, which override the lines above where they differ):**
  - **Told only.** She remembers ONLY when told. Nothing is noticed or stored automatically (no usage patterns), conversations are never stored, and **memory is never sent online** (no Groq call, no network; `jarvis/memory/` imports no online code).
  - **No dashboard memory panel.** Seeing, correcting and deleting are by voice or typed command only.
  - **Commands** (all local; `[memory]` in settings):
    - "remember (that) X" / "don't forget X" → "Got it.", and Live Activity shows `Remembered: X`. Fixed action `remember`, **L0**.
    - "what do you know / remember about Y" → a local keyword match, up to 3 newest: "You told me X. You said that on <date>." (first person turned round: "my exam" → "your exam"). None → "You haven't told me anything about Y."
    - "what do you remember" → the count and the newest 5 spoken; the full list in Live Activity.
    - "forget Y" → one match is deleted ("Forgotten.", fixed action `forget_memory`, **L1**); several → a numbered list, then "the second one". "forget it" means never mind.
    - Corrections ("actually my exam is on Tuesday") update the one clear match ("Updated."): at least half its keywords in common, and no other item as close. Otherwise it is stored as new. "actually" + a command, a question or "never mind" is not a correction.
    - "forget everything" → fixed action `forget_all_memory`, **L4**: she says "This deletes all N things I remember, and it can't be undone. Say confirm, then confirm again.", then after the first confirm "Say confirm once more to go ahead."
  - **Never stored:** passwords and PINs (any such word), card numbers (13–19 digits passing the Luhn check), bank account numbers (BSB + account, IBAN, or a bank word with digits), and government ID / passport / licence / tax file / Medicare / social security numbers (the keyword plus a number). She says "I won't store that. It looks like a <type>." The refused text is **written nowhere**: the same check runs inside `redact_sensitive()`, so Live Activity, decision records and `debug.log` show `[redacted]`.
  - **File:** `jarvis_data/memory.json` (the developer's choice, inside the project on D:, not C:). Atomic writes (temporary file, then replace). After every save the same content goes to `memory.json.bak`, so deleted stays deleted in the backup too. A damaged file is kept as `memory.corrupt-<time>.json` and the backup is loaded (or an empty memory), and she says so at startup. Cap 500 items: when full she says so, never silently dropping old ones. Writes go through R9's `check_path(WRITE)`.
  - **Not built:** automatic memory (usage patterns), "where did I save the invoice?", "continue where I left off", and the memory panel (by the developer's decisions).

- **Laptop log fixes (27 October 2026):** never stored: a sensitive word (card number, credit/debit card, account number, BSB, PIN, password, passcode, CVV, passport number, licence/license number, tax file number/TFN, Medicare number) together with ANY digit, spoken digit or value, whatever the digits are (the laptop log stored "my card number 411 111 111 111", which fails the Luhn check). The word alone is fine ("remember to change my password"). Luhn is still used for a bare card number. The stored item was deleted from `memory.json` and `memory.json.bak`, and the debug.log line holding it was redacted.

### F11. Computer Control (CUT)

> **CUT 28 October 2026 (the developer's decision: "not needed for this submission; it would require the banned input-injection APIs").** Not built and not planned. It is removed from the build order, the Modules panel and Groq's list of abilities, and the dashboard's Keyboard / Mouse row is gone. The fixed actions `type_text`, `press_key`, `click` and `move_mouse` stay listed in §4.2 and in the trust table, but no executor exists, so a request for one is refused. The text below is kept only as a record of what was specified; none of it applies.

Keyboard, mouse and a limited terminal, with each step under the trust levels. Examples that must work:
- "Open Word and write an application letter."
- "Search photosynthesis and make a 2-page report in Word." She aims for about two pages, checks the page count shown in Word, adjusts, adds source links at the end, and the developer reads it before using it.

Rules:
- **Previews.** A task gets **one preview of the complete planned sequence** (for example: open Word, wait for it, type this letter, save it in Documents), with the full text she will type shown in the preview. The developer confirms once. She asks for any missing details (for example the company or role) **before** the preview.
- Confirming a plan lets her run exactly those steps and no others. A new step, a different target or a changed plan needs a new preview.
- If the keyboard/mouse library has a built-in emergency abort (for example moving the pointer into a screen corner), it stays on for every step and acts as a second Stop: tripping it submits Stop to the controller (R8).
- Text is typed, never pasted through the clipboard. Typing only.
- **Files:** R9. **Terminal:** R10.
- **Depends on:** F17, F7, F5.

### F12. Skills and Workflow Recording (CUT)

> **CUT 25 October 2026 (the developer's decision: "not needed for this submission").** Not built and not planned. The text below is kept only as a record of what was specified; none of it applies.

- On "learn this" she records the developer's multi-step task, shows him the recorded steps, and saves it only when he says yes.
- On replay she shows the steps first, and every step goes through the trust levels.
- She can notice patterns in what he does **through her** and suggest a skill, and only saves it when he says yes. (She cannot see what he does by hand in Chrome. Reading Chrome history stays off.)
- A skill is a **data file** in a fixed format that can only list the fixed actions from Section 4.2. Anything else fails validation and is refused. A skill can never contain code, and JARVIS cannot invent new abilities. New abilities that need code are added to the project by the developer.
- A skill file that did not come from the developer's own recording shows its steps and needs his approval before first use.
- She never records passwords, password-field input, authentication codes, tokens or API keys (R11). A sensitive step is saved as "ask me at replay", and the value is never stored.
- The developer can say "pause recording" at any time.
- The dashboard lists each skill, whether it is enabled, and the highest trust level among its steps. **OPEN (§11 #13):** where on the dashboard this list goes; its old Skills Registry slot now holds the Internet panel (F6).
- Scheduled runs (for example "open this every morning") were **removed**. Do not build them unless the developer asks.

### F13. Local-First Mode with Web Access
- R1, R2 and R3 in practice. She goes online whenever needed, and always says so first.
- The dashboard shows LOCAL, ONLINE or LIMITED until HH:MM (F6's Internet panel).
- If a free service ends, hits its limit or asks for payment, she says so and switches that feature off (R1).
- R1's timeout, single-retry and 429-cooldown rule applies to every online call, and the dashboard shows a rate-limited service as LIMITED with the time it returns.

### F14. Personality and Emotion Awareness
- The same identity every time, and the same voice every time: Kokoro af_bella (full precision); Zira only as a fallback when Kokoro isn't installed.
- She scores how upset the developer sounds from 0 to 1, using a small check on his words and, if testing shows it works, the loudness of his voice.
  - **Built 23 October 2026 (words only):** a weighted word/phrase list in `[mood] weights` ("fed up" 0.85, "annoying" 0.6, "not working" 0.5, …), matched as whole words, longest phrase first, combined so each extra cue pushes the score toward 1 without passing it. A negation shortly before a cue ("not frustrated") scales it down (`negation_factor`); an intensifier right before it ("so annoying") scales it up (`intensity_boost`); any positive word ("thanks", "haha", "impressive") halves the total (`positive_damping`). Local, no model, no network, no package. Voice loudness is **not** used: it has not been tested to work.
- She checks in only when the score is at least `[mood] threshold` (DEFAULT **0.6**; the starting value was 0.8, changed after the 30-sentence test below), at most once every **10 minutes** (`[mood] cooldown_seconds` 600, counted from when she actually asks), and only after she has finished the request: the check-in is the last sentence of the same reply, after the answer or the action's result. Never after a reply that is itself a question (one question at a time). If he says he is fine (the next command only, `[mood] fine_phrases`), she says "Good to hear. I won't ask again this session." and does not ask again that session.
- Below the threshold she behaves exactly as normal.
- When she checks in she asks, she does not tell: "Hey mate, is everything fine? Are you alright?" The line is picked locally at random from `[mood] check_in_pool` (short and kind, never the same line twice running).
- **Live Activity** (23 October 2026): `noticed: you sound frustrated (0.86)` while the command runs, and `checking in: <line>` after what she said. Both lines are **session only**: they are never written to the debug log or anywhere else, so the guess is never saved (F10).
- If he has been rude to her, F2's reply wins and she does not also check in.
- If he tells her he is tired or stressed, she keeps her replies shorter and gentler.
- The guess **never** changes which actions she takes or blocks, and it is never saved. He can turn the feature off.
- **The numbers are provisional.** Before this is turned on, test it with sample sentences (for example 30, covering calm, annoyed, angry, joking and sarcastic) and set the threshold so she stays quiet on calm and joking ones. If it cannot be made to work well enough, the feature stays off.
  - **Tested 23 October 2026** (`tests/test_f14_mood.py`, 15 upset and 15 not upset, in the developer's style): at 0.8, precision 1.00 and recall 0.40 (6 of 15); no false check-in at any threshold from 0.40 up (highest not-upset score 0.30, "I hate waiting for the bus, ha"); recall 0.93 (14 of 15) from 0.40 to 0.60. The threshold is 0.6: that recall with the widest margin above the calm and joking sentences. Missed: sarcasm ("oh great, it crashed again, just perfect"). The word list was written with these kinds of sentences in mind, so the real-world rate is likely lower; recheck on the laptop. It is on (`[mood] enabled = true`).
  - Rudeness wins (F2): a command with a word from `[rudeness] words` gets the rude reply and is not scored at all. With today's list, swearing ("open fucking chrome") always gets the rude reply, never a check-in.
  - Tired or stressed (`[mood] gentle_triggers`, "I'm tired", "I'm stressed", …): for the rest of the session every online answer's system prompt adds `[mood] gentle_prompt` (short, calm, gentle). Local replies are already short.

### F15. Sleep Mode
- **Merged with conversation mode (approved 11 October 2026, F2).** SLEEPING is the startup state. "Sleep JARVIS", "go to sleep" or "goodnight JARVIS" → "Goodnight." → SLEEPING (handled locally, never sent online).
- Only the microphone stays on. Everything else is off: camera, screen capture, apps, files, memory and internet.
- She transcribes every utterance locally and shows it in the activity log as "heard while asleep: …". Nothing is acted on, nothing goes online, and she does not speak.
- She wakes in exactly four ways: "Hey JARVIS" (the wake word), "wake up JARVIS" (matched on the local transcript, with the fuzzy name and wake forms in F2), the wake hotkey (the press-to-talk combination), and the typed wake phrase. Nothing else wakes her. Every time she wakes she greets at once with a short, varied, time-aware greeting chosen locally (F2, `[greeting.pools]`), for example "Evening, Priyansh. What's next?" or "Welcome back, Priyansh."
- While SLEEPING the typed-command box accepts only a wake phrase. Any other typed text is discarded without being logged, shown in history or stored; a short on-screen notice says how to wake her. Test: text typed during a sleep session appears in no file JARVIS wrote.
- Stop while SLEEPING leaves her SLEEPING.
- **Non-retention:** what she hears while asleep exists only in memory and in the session's activity log on screen. It is never written to application logs (including the debug log), crash reports, telemetry (there is none, R5), temporary files or her memory, and it is gone when JARVIS closes. Utterances ignored as fragments while awake are treated the same way.
- **Limits to document:** as far as is known, Windows can copy a program's memory to disk (its pagefile), and anyone can read the screen. JARVIS cannot control either.
- **Tests:** a test that runs a sleep session with known words and then checks that none of them appear in any file JARVIS wrote.

### Shared rules for F16a, F16b and F18–F22

These apply to F16a, F16b and F18–F22. They are not repeated in each section.

1. **Controller (F17).** Every command enters as an event through the one queue. Long work (network, file parsing, Groq) runs on a worker thread and checks `controller.is_stop_requested()` between steps (between chunks, between requests, between page steps). On Stop the worker abandons its work, discards partial results, and deletes any temporary file it made.
2. **Trust (F7).** Levels come from the fixed table in `trust.py`, decided by the policy engine. A model never chooses or changes a level. The new action names are in the fixed action set (4.2); each feature gives its level.
3. **Decision records (F5).** Every command produces one record. Each feature lists its live activity-log steps. Records never hold laptop-data content sent online (R4), passwords or tokens (R11), or Sleep Mode words (F15).
4. **Online (R1, R3).** These calls go through **one shared online helper**. It is a prerequisite and must be built first if it doesn't already exist.
   - **Timeout:** every call has an explicit timeout (`[online] request_timeout_seconds`).
   - **Retries:** at most one automatic retry, and only for timeouts or connection errors, never for a 429.
   - **429 cooldown:**
     - An HTTP 429 turns that service off for `[online] rate_limit_cooldown_seconds`, or for the service's `retry-after` value if it sends one.
     - She says once "The free limit for \<feature\> is reached; it is off until \<time\>." (no service name, R3).
     - The dashboard shows the service as LIMITED.
     - During the cooldown, calls skip the network and go straight to the fallback.
   - **Before each call:** she says "Searching online" (once per command), the dashboard shows ONLINE, Live Activity shows the words sent (`searching online, sending "<words>"`), and the decision record and debug.log record the destination (R3). Where R4 applies, the log records type, size, destination, time and hash only, never the content.
   - **Errors are named separately:** no internet / no key / rate limit / timeout / service error / blocked.
5. **Laptop data (R4).** Anything read from the laptop (file text), or a query built from it, is sent only after the preview panel shows **exactly** what will be sent and the developer confirms (L3). Values the developer typed into settings for an online purpose (the weather city, the monitored topics) count as his own words (L0, R4), and are logged per R4.
   - The text is first written to a temporary file in `jarvis_data/tmp/` and hashed (SHA-256).
   - That file is shown in the preview, re-hashed just before sending, and sent only if the hash still matches.
   - The temporary file is deleted afterwards, on Stop, and at startup (leftovers).
6. **Paths (R9).** Every path goes through `check_path()`.
   - **Reads:** allowed anywhere except the protected list (Pictures on any drive, `%OneDrive%\Pictures`; `jarvis_data/browser_profile` until 27 October 2026, when it was removed).
   - **Writes:** allowed only in the approved folders: Documents, Downloads, Desktop and the project folder.
   - **Overwrites:** never. If the output name exists, she asks first, or uses a numbered name if the developer chose that in the preview.
7. **Degrade (R14).** A missing library, key, browser, voice or connection disables only that feature. The dashboard panel for the module shows the missing-setup message (R14): what is missing and exactly what to do and where. Everything else keeps working.
8. **Settings (R17).** Every number lives in `settings.toml`. The values given are DEFAULT starting values.
9. **HTTP client.** Plain HTTP calls (Open-Meteo, GDELT, RSS) use the standard library's `urllib.request` with the shared timeout. This adds no dependency.
   - XML (RSS) is parsed with `defusedxml`. It is already pulled in by `youtube-transcript-api`; pin it directly.

#### Groq budget, which applies to F18 and F19

These are Groq's published free-plan limits for `openai/gpt-oss-20b`, read on 3 October 2026 from `https://console.groq.com/docs/rate-limits`:

- **30** requests/minute
- **1,000** requests/day
- **8,000** tokens/minute
- **200,000** tokens/day

When rate-limited, Groq returns **HTTP 429** with a `retry-after` header in seconds.

The 8K tokens/minute cap decides how much text can go in one request:

```toml
[groq]
max_chars_per_request = 12000      # about 3,000 tokens of input, leaving room for prompt + answer under 8K TPM
chunk_spacing_seconds = 35          # about 2 requests a minute at that size, under the 8K TPM cap
max_chunks_per_task = 4             # so one summary is at most ~48,000 characters and ~2 minutes
summary_max_tokens = 400
```

The long-text method ("chunked summary") is shared by F18 and F19:
1. Split on paragraph or sentence boundaries into chunks of at most `max_chars_per_request`.
2. Summarise each chunk, waiting `chunk_spacing_seconds` between requests. Check Stop between chunks.
3. If there was more than one chunk, make one final request that combines the chunk summaries.
4. If the text needs more than `max_chunks_per_task` chunks, summarise only the first `max_chunks_per_task` and **say plainly how much was covered**. Never imply the whole text was read.

**Budget note:** one maximum-size task uses about 15,000 tokens, so roughly 13 such tasks fit in Groq's 200K daily token limit.

**Groq's data-use terms have not been checked.** Until the developer checks them, the R4 preview for anything sent to Groq shows: "Groq data-use terms: not yet checked by the developer." The wording lives in settings.

### F16. Web Access (Groq web search first, then the browser)
- If she cannot answer locally, she asks Groq's `openai/gpt-oss-20b` with Groq's built-in **`browser_search`** tool (Groq's own web search). Weather comes from F16a. News headlines come from the RSS feeds in F21's settings.
- **Verified (3 October 2026):**
  - `openai/gpt-oss-20b` is listed as active on Groq's live `GET https://api.groq.com/openai/v1/models` for the developer's key (HTTP 200, 13:10 GMT).
  - Groq's docs (`https://console.groq.com/docs/browser-search`) list `browser_search` for `openai/gpt-oss-20b`, `openai/gpt-oss-120b` and `openai/gpt-oss-safeguard-20b`, enabled with `tools=[{"type": "browser_search"}]`, powered by Exa, and "not compatible with structured outputs".
  - One test call with the developer's free key (13:14 GMT) returned HTTP 200, ran `browser_search` and `browser.open`, answered correctly, and used **11,285 tokens**. The reply contained bracketed citation markers, which must be removed before speaking; the sources are shown on the dashboard.
  - Free-plan limits for `openai/gpt-oss-20b`: 30 requests/minute, **1,000 requests/day**, 8,000 tokens/minute, **200,000 tokens/day**. At about 11,000 tokens per web question (one measured sample), that is roughly 17 web questions a day. Groq's pricing page says nothing about a separate charge for the search tool.
  - Groq's `groq/compound` and `groq/compound-mini` were decommissioned on 21 September 2026 (Groq deprecations page) and must not be used.
- **Re-verified (16 October 2026, live):**
  - `GET /openai/v1/models`: HTTP 200, `openai/gpt-oss-20b` active (context 131,072); no `compound` models listed.
  - `browser_search` with `tool_choice="required"`: HTTP 200 in 2.9–4.2 s. `executed_tools` lists `browser_search` (its own `query`, `topn`, `source`) and then `browser.open`; `search_results.results[]` carries each result's `title` and `url`. Results come from Exa.
  - **Tokens per web question:** 2,475 / 3,307 / 9,840 without a limit on pages opened (it varies with how many pages the model opens); **2,802** with "search once, open at most one page" in the system prompt. The two production-path checks used 3,812 and 3,266.
  - Response headers for the developer's key: `x-ratelimit-limit-requests: 1000` (per day), `x-ratelimit-limit-tokens: 8000` (per minute), with `remaining-*` and `reset-*`. **There is no tokens-per-day header.** Groq's rate-limits page now calls its table "base limits for the Developer plan", so **200,000 tokens/day is unverified for the free key**. The docs don't say whether tool output counts; one observed 9,840-token call succeeded under the 8K-per-minute cap.
  - Without limits, two answers to the same cricket question contradicted each other; the one-page answer matched the scorecard. Hence the one-page rule and naming the source.
- **Live or plain is decided locally, never by the model (16 October 2026).** A question is "live" if it contains a `[web] live_words` or `time_words` entry (whole words: score, match, result, won, news, latest, update, happening with, price, election, today, tonight, right now, …) or a year from the current year on. Only live questions are sent with `tools=[{"type": "browser_search"}]` and `tool_choice="required"`; everything else gets the plain Groq answer with no tool. Time, date and weather are answered by their own local matchers first.
- **Budget (16 October 2026).** The live system prompt adds `[web] live_instructions` ("use browser_search exactly once and open at most ONE result page; at most two spoken sentences; say which website the answer comes from; report results exactly as the source states, don't infer who batted first or chased unless the source says so; state only what the page says; if the page gives a date for the result, say it; if the results don't answer it, say so"), and `[web] answer_max_tokens` (DEFAULT 1500).
  - **Accuracy (23 October 2026).** On 16 October she said West Indies "chased" a total they had set; the "exactly as the source states" sentence was added. Live check 23 October (3 calls): the numbers matched the page the model read every time and nothing was inferred, but the model **opened 2 pages in 2 of 3 calls** (7,553 and 7,588 tokens) despite the one-page rule, and didn't say the date the 1 time it was asked to (it answered with the 1 March 2026 match, not the latest one). These rules are prompts the model doesn't always follow; saying the searched query and the source is what lets the developer catch a wrong match.
  - Every Groq call, live or plain, adds its `usage.total_tokens` to a local daily ledger (`jarvis_data/groq_usage.json`, the date and a number only, reset on the local date). Once `[web] daily_token_budget` (DEFAULT 150,000) is used, live searches are not sent and she says "Today's web-search allowance is used up…".
  - Before a live search, if Groq's last `x-ratelimit-remaining-tokens` is below `[web] min_tokens_per_call` (DEFAULT 4000) and `x-ratelimit-reset-tokens` is at most `[web] max_wait_seconds` (DEFAULT 15), she waits (Stop still works; Live Activity shows "waiting N s for the online allowance"). Otherwise she says to try again in N seconds. Never a retry after a 429 (R1).
- **What was searched is said and shown (16 October 2026).** The query the tool actually ran is taken from `executed_tools` (never from the model's text) and comes first in the answer ("I searched for India vs West Indies T20 score. …"), so a mishearing is obvious. Citation markers (`【…】`) are removed before speaking. If the tool never ran, the model's text is not passed off as live: she says "I couldn't find anything live about that."
- **Recency (23 October 2026).** "The score of India vs West Indies" was answered with the 1 March World Cup match and no date. Now:
  - JARVIS, not the model, adds today's local date to every live question in the words sent: `<question> (today is <date>)`, or `<question> (most recent as of <date>)` when it has a `[web] recency_words` word (latest, last, today, score, news, result, …). Live Activity shows these exact words (R3).
  - The result's date is read by JARVIS from the page the model opened (`executed_tools` browser.open output; the first date in its first `[web] date_search_chars` characters, never a future date; the page the answer names wins), never from the model's text. Live Activity: `result date: <date> (<site>)` or `result date: not shown on the page`.
  - She says it after the answer: "That result is from <date>." (left out when the answer already gives that day and month); for a recency question whose result is older than `[web] stale_days` (DEFAULT 7): "The most recent result I found is from <date>."; with no date on the page: "The page I read didn't show a date for this result."
  - Live check 23 October 2026: the model still found the 1 March match (13,018 tokens, 2 pages) even with "most recent as of 23 October 2026" sent; she now says "The most recent result I found is from 1 March 2026."
- **Known mishearings** in `[web.corrections]` (whole words, longest first, editable) are fixed before a question is sent, and Live Activity shows `fixed mishearing: "<heard>" -> "<sent>"`. Laptop log 16 October: "India vs. Vendus T" was searched as "India vs Vietnam"; now "vendus t" → "west indies t20".
- **Live Activity** for a live question: `heard: …` → `fixed mishearing: …` (if any) → `searching online, sending "<words>"` → `searched for: <tool's query>` → `read 1 page: <site host>` → `saying: …`. The decision record and debug.log hold `Groq, openai/gpt-oss-20b, browser_search (Exa); usage <tokens>; sources <urls>`. Trust L0.
- The developer's own spoken words, and settings values covered by R4's amendment, are L0. R3 applies: "Searching online", ONLINE, and the log records the destination and the words sent. Anything from the laptop follows R4.
- If Groq web search fails, she says so and uses the browser route (F16b) for a plain text question: open the search page and read the results. (Built 23 October 2026, see F16b. A missing key, today's allowance used up or a 429 cooldown already have their own answers and don't use the browser.)
- F16 handles **text questions only**. Images are never sent online by F16; F8 and F9 decide how screen and camera images are read (OPEN #4, #5).
- Detect and name each failure separately: no internet, no key, rate limit reached, not logged in, page changed.
- If a question is ambiguous (for example "who is Jonathan"), she searches, asks which one the developer means, then answers. A plain question like "what is photosynthesis" gets a plain explanation.
- For anything from the laptop, R4 applies: the exact preview, then the same file is sent.

### F16a. Weather

**Purpose.** This answers "What's the weather?" and "Weather in Leeds tomorrow?" from Open-Meteo, a free service that needs no key. It is the only weather code in JARVIS, and F21 calls it rather than having its own.

**Verified (3 October 2026, live docs):**
- **Forecast:** `https://api.open-meteo.com/v1/forecast`.
  - **Current-conditions variables** (`current=`): `temperature_2m`, `apparent_temperature`, `weather_code`, `wind_speed_10m`.
  - **Daily-forecast variables** (`daily=`): `temperature_2m_max`, `temperature_2m_min`, `precipitation_probability_max`, and `weather_code` (added 23 October 2026, so "tomorrow" can say the day's conditions; checked live).
  - **Other parameters:**
    - `timezone` is **required** whenever `daily=` is requested.
    - `forecast_days` can be 0–16 (DEFAULT 7).
  - **API key:** "Only required to commercial use".
- **Geocoding:** `https://geocoding-api.open-meteo.com/v1/search`.
  - Parameters: `name` (required), `count` (1–100, DEFAULT 10), `language` (DEFAULT en), `format`, `countryCode`.
  - Returns `results[]` with `name`, `latitude`, `longitude`, `country`, `admin1`, `timezone`.
  - When nothing matches, `results` is empty.
- **Free terms** (`https://open-meteo.com/en/terms`):
  - Non-commercial use only: personal, home automation and educational use are allowed.
  - Limits: **600 calls/min, 5,000/hour, 10,000/day, 300,000/month**.
  - Data is **CC BY 4.0**, so attribution is required.
  - The terms page shows no effective date; changes are tracked on GitHub. Recheck before submission.

**Behaviour.**
1. The matcher recognises "weather", "forecast", "will it rain" and similar, plus an optional city and an optional "today", "tomorrow" or "now".
2. **City given:** it is the developer's own spoken words, so this is L0.
   **No city given:** use `[weather] default_city` (DEFAULT "Sydney", the developer's choice 16 October 2026). Under R4's amendment this counts as his own words (L0), and it is logged per R4 (type, size, destination, time and hash, never the city).
3. **Geocode.**
   - Cache the result in memory for the session, keyed by the lower-cased city name.
   - Also cache the default city's coordinates in `jarvis_data/weather_cache.json`, which saves one call per startup.
   - Use `count=5`. If the top results are in different countries and the developer didn't name one, she asks "Leeds in England, or Leeds in Alabama?" and waits, **unless** the top result's population is at least `[weather] dominant_population_ratio` (DEFAULT 10) times that of the best result in another country; then she uses the top result. Live check 16 October 2026: "Sydney" returns 5 places in 4 countries (Sydney, Australia 5.6 million; Sidney, Ohio 20,858), so without this rule she would ask every time.
   - When nothing matches, the response has **no `results` key at all** (live check, 16 October 2026); a missing or empty `results` both mean "not found".
4. **Forecast.** One call with `current=` and `daily=` (variables above), `timezone=<the timezone from the geocoding result>`, `forecast_days=2`, and units from settings.
5. **Speak briefly**, using a local table that turns WMO weather codes into words. Example: "In Leeds it's 14 degrees and light rain, feels like 12. Today: high 16, low 9, 70% chance of rain."
   - **As built (23 October 2026):** six sentence templates in `[weather.texts]` (weather / rain / temperature × today / tomorrow), filled only from the forecast just received; a reply missing any value is a service error, never a guess. "Will it rain …" leads with the chance of rain; "temperature …" with the temperatures.
   - **The "which one?" answer** is valid for the next command only: a number ("the second one") or the region ("Ontario"); anything else is handled as a new command. A country after the city ("weather in Leeds England", "Hamilton New Zealand") picks without asking. The question lists one place per country, at most `[weather] max_choices` (3).
   - Not weather: commands starting with open/find/search/close/play ("open weather" opens the Weather app), and anything with a `[weather] skip_phrases` word (explain, define, warning, …). Weather warnings are live questions (F16).
6. The dashboard shows "Weather data by Open-Meteo.com (CC BY 4.0)". This is the required credit line.

**Activity log:** `searching online, sending "Leeds"` → "found Leeds, England" → "forecast received" → "saying: …" → "Weather data by Open-Meteo.com (CC BY 4.0)" (the credit line, R3's exception). When the default city is used, the first line is "searching online: weather, default city" instead of naming it, then "found default city". "Open-Meteo, geocoding/forecast" goes to the decision record and debug.log only, naming only the calls really made. A place already known this session goes straight to `searching online: forecast for <place>`; an ambiguous one shows "asking which place".

**Trust.** **L0**: answering a question. There is no fixed action and no permit.

**Libraries/APIs.**
- Open-Meteo (free, no key, limits above).
- Standard library only (`urllib.request`, `json`).

**Settings.**
```toml
[weather]
default_city = "Sydney"            # developer's choice (16 Oct 2026); empty = she asks which city
dominant_population_ratio = 10     # top result this many times bigger than any other country's -> no question
temperature_unit = "celsius"       # or "fahrenheit"
wind_speed_unit = "kmh"            # kmh | ms | mph | kn
geocode_result_count = 5
forecast_days = 2
```

**Failure handling.**
- **Offline or timeout:** "I can't reach the weather service right now. Check your internet connection, then try again." (the second sentence added 23 October 2026, missing-setup rule R14; all texts in `[weather.texts]`).
- **429:** shared cooldown (shared rule 4).
- **City not found:** "I couldn't find a place called X. Check the spelling, or add the country, for example weather in Leeds England."
- **No city and no default:** "Which city? Say, for example, weather in Sydney, or set [weather] default_city in settings.toml."
- **Unexpected response shape:** treated as a service error; say so, never guess a number.
- **An answer is never invented without data.**

**Tests (pytest, network mocked).**
- Request building:
  - The geocode URL carries `name`, `count` and `language`.
  - The forecast URL carries `timezone` whenever `daily` is requested.
  - Unit parameters come from settings.
- Responses:
  - Empty **or missing** `results` → the "couldn't find" answer, and no forecast call is made.
  - "Sydney" with the live result shape (5 places, 4 countries) → no question, one forecast call; "Leeds" (similar sizes) → asks.
    - Live check 23 October 2026: the real "Leeds" does **not** ask — Leeds, England (536,280) is 45× Leeds, Alabama (11,936), so the 10× rule picks England. The "similar sizes" test uses made-up populations. A real name that asks: "Hamilton" (Ontario 569,353; Waikato, NZ 192,100; Ohio 62,407).
  - Two results in different countries with no country given → asks, and no forecast call is made.
  - WMO code table: known codes map to words; an unknown code → "unusual conditions", not a crash.
- Settings and failures:
  - Default city used → no preview; the log line has type, size, destination, time and hash, and does not contain the city.
  - 429 → cooldown; a second call within the cooldown makes no HTTP request.
  - Timeout → fallback text within the timeout.
- Behaviour:
  - Cache hit → no second geocode call.
  - The credit line is present on the dashboard model.
  - F21 calls the same function (exactly one weather client exists).

**Effort.** About 6 hours.

**Risks.** Geocoding ambiguity for small place names.

### F16b. Web search: opens in the developer's own Chrome (changed 27 October 2026)

**Search opens in the user's Chrome.** The developer's decision after his 27 October laptop test: JARVIS's own Playwright browser, its Chromium download and DuckDuckGo are removed. A search is a website the developer asked to open.

**Behaviour.**
1. **Commands** (cleaned text, `[[browser.commands]]`): "search X", "search for X", "search the web / online for X", "google X", "open chrome and (then) search X". "google chrome", "google docs" and the like are left to F3; "search for X on my screen" is F8.
2. **What happens:** `https://www.google.com/search?q=<X>` (`[browser] search_url`) opens in his normal Chrome through F3's `open_url` (**L1**, F3's executor and R12 check), exactly like opening a website. No automation, no separate profile, nothing is read back from the page.
3. **Spoken:** "Searching Google for X in Chrome." Live Activity: `searching Google for "X" in Chrome` and the "saying:" line. No URL is ever spoken or shown (F3, 27 October 2026); the decision record keeps it.
4. **F16's last resort:** a **live** question whose online search fails with a timeout, a service error, an empty or cut-off answer or nothing found (`[browser] fallback_on`) opens the Google results for his words in Chrome: "I couldn't get a quick answer, so I've opened the results in Chrome." A missing key, today's allowance, a rate-limit cooldown or no internet keep their own answers (F16).
5. **No Chrome (R14):** "I'd search Google for X in Chrome, but Chrome isn't installed. Install Google Chrome, then try again." (and the fallback's own sentence).

**Trust.** L1 (`open_url`): the developer asked, same as opening a URL. `browser_open_read` and `browser_interact` are removed from the fixed action set.

**Libraries.** None. `playwright`, `greenlet` and `pyee` are removed from `requirements.txt`; `scripts/download_models.py` no longer downloads Chromium; `models/ms-playwright` (740 MB) and `jarvis_data/browser_profile` are deleted.

**Tests:** `tests/test_f16b_browser.py` (the commands, the Google URL, the 27 October "Open Chrome and search ChatGPT" and "Tell me the score of India vs New Zealand" transcripts, the fallback rules, no Chrome, and that no Playwright or DuckDuckGo remains).

**History.** Built 23 October 2026 as JARVIS's own Playwright browser (Chrome for Testing 153, 740 MB, DuckDuckGo's HTML page, the top 3 result titles read aloud, fixed action `browser_open_read`). On the 27 October laptop test the results were read but not heard, and the developer chose his own Chrome instead. The full earlier text is in `docs/spec-backups/JARVIS_BUILD_REFERENCE.pre-2026-10-27-laptop-fixes.md`.

### F17. Core State Machine
- States are listed in 4.3.
- **Single authority:** modules send requests, and only the controller changes state, using a fixed table of allowed changes, one event queue and one thread.
- **Permits:** an action runs only with a permit issued for that exact action, its exact parameters, the request that started it, and the current context. Confirming a plan lets the controller issue a separate one-time permit for each planned step. Executors refuse anything without a valid permit.
- **No stale permissions:** a permit becomes invalid when the action completes, fails or is cancelled, when anything about it changes, when the state moves on, or when it times out. It cannot be reused.
- It sets the origin tag (Section 5) and treats Stop as the highest-priority interrupt.
- **JARVIS-started work (approved 3 October 2026, needed by F21; built 28 October 2026 with F20):** a new event `STARTUP_TASK_REQUESTED` with the transition AWAKE → UNDERSTANDING. `Orchestrator.run_startup_task` submits it; the record shows "started by JARVIS: …"; every action and answer in that interaction carries the origin the controller set. The controller sets the origin tag **JARVIS_INITIATED** from that event type; anything a caller puts in the payload is ignored. Every other event keeps DEVELOPER_REQUESTED.
- Every state change is logged, and the dashboard shows the current state.
- **Conversation mode (approved 11 October 2026).** Events and transitions: `WAKE` SLEEPING → SPEAKING (the greeting), then `SPEECH_FINISHED` → AWAKE; `SPEECH_STARTED` AWAKE → LISTENING (an utterance, push-to-talk or a typed command); `LISTEN_TIMEOUT` LISTENING → AWAKE; `GO_TO_SLEEP` SPEAKING → SLEEPING (after "Goodnight.") and AWAKE → SLEEPING (the optional auto-sleep timer); `CANCEL_RECEIVED` CONFIRMING → SPEAKING ("Cancelled."). Nothing but `WAKE` leaves SLEEPING. Stop from SLEEPING leaves her SLEEPING; from every other state she returns to AWAKE.
- **Tests:** it counts as finished **only** when tests for every invalid state change and every stale permit exist and pass. Examples: acting while SLEEPING, acting without a needed confirmation, any event other than `WAKE` while SLEEPING, `WAKE` in any other state, `CANCEL_RECEIVED` outside CONFIRMING, reusing a permit, a "confirm" with nothing pending, `STARTUP_TASK_REQUESTED` from any state other than AWAKE, and a payload claiming an origin.

### Stretch features (F18–F22), part of Section 6

> **STRETCH FEATURES.** F18–F22 are built (F23 was cut on 25 October 2026) **only after F1–F17 are finished and their tests pass**. They must not delay, weaken or reshape any F1–F17 rule. The shared rules above (before F16) apply to all of them.

### F18. YouTube tools (stretch)

> **Built 28 October 2026 (the developer's F18 decisions; these override the draft text below where they differ).**
> - **Play** (L1, `open_url`): "play \<x\> on YouTube" (also "search YouTube for \<x\>", anywhere in a sentence; `play` was added to `[interpreter] command_verbs`) opens **YouTube's search results** for \<x\> in his normal Chrome. No automation, no video picked for him. She says "Searching YouTube for \<x\> in Chrome." No Chrome: what to install (R14).
> - **Summarise:**
>   - **A typed link:** "summarise this video \<link\>". All 4 link forms plus a bare ID; a playlist-only link or a wrong-length ID gets "I couldn't read a YouTube video in that link." The interpreter gained `keep_case` matchers, because video IDs are case-sensitive.
>   - **A topic:** "summarise the YouTube video about \<x\>". One Groq `browser_search` for "YouTube video about \<x\>" (his own words, L0). The video is taken **only from the search tool's own result URLs**, never from a link in the model's text.
>   - **No link:** "summarise this video" with no link asks which video.
>   - **Transcript:** `youtube-transcript-api` **1.2.4**, its current instance API, verified in the installed source. One `YouTubeTranscriptApi(http_client=…)` per call, made on the worker thread (the library says it isn't thread-safe), with a `requests.Session` subclass that applies `[online] request_timeout_seconds`. `fetch(id, languages=["en"])`; on NoTranscriptFound, `list(id)` gives a manual transcript first, then a generated one.
>   - **Summary:** the shared chunked summary (`jarvis/online/chunked.py`, also used by F19), with the short-opener rule placed last in the summary prompt. At most 3 sentences are spoken, and the full summary goes to Live Activity. Auto-generated captions add "names and terms may be wrong". Longer than 48,000 characters: "I summarised the first 79 of 99 minutes."
> - **Trust:** transcripts are public internet content, so there is **no R4 preview** (L0, no fixed action). R3 still applies: "Searching online", ONLINE, Live Activity shows "checking online: YouTube transcript (video dQw4…)", "transcript found (en, manual, 2,089 chars)", "summarising online: part N of M", "summary ready". The decision record and debug.log get the video ID, the size and the SHA-256 of the text sent. **URLs are never spoken or shown** by her (his own typed line excepted).
> - **Failures** each have their own sentence: no transcript, unavailable/private, unplayable, age-restricted, blocked, PoToken, a YouTube error, no internet, a 429 (shared cooldown), no Groq key ("I got the transcript, but couldn't summarise it. …"), library missing.
> - **Live check, 28 October 2026** (Groq calls 1–6 of this run's 15): a typed link gave a transcript (manual, 2,089 chars) and a summary in 3.9 s. "About how photosynthesis works" found a real video in the search results (7,775-char transcript) in 6.0 s. A wrong ID gave "That video is unavailable or private…". After the opener fix the first sentence was 8 words.
> - **Not built:** remembering the last video for "summarise the video I just opened" (play opens a results page, so there is no video ID); "get the transcript" / saving it (`write_text_file`).
> - **Caveat (R19):** the library reads YouTube's internal web endpoints. It is unofficial, can break when YouTube changes, and how it fits YouTube's Terms of Service is unverified.

**Purpose.** This plays a video in the browser, fetches a video's transcript, and summarises it via Groq.

**Verified against the installed library** (`youtube-transcript-api` **1.2.4**, read from its installed source, not from memory; 1.2.4 is also the latest on PyPI, released 29 January 2026, MIT):

- **Instance API.**
  - Create an object with `YouTubeTranscriptApi()`. The old class-level static methods (`get_transcript`, `list_transcripts`) are gone in 1.x.
  - The constructor takes only `proxy_config` and `http_client` (a `requests.Session`).
  - The docstring says an instance is **not thread-safe**, so create one per worker thread.
- **Fetching.**
  - `fetch(video_id, languages=("en",), preserve_formatting=False)` returns a `FetchedTranscript`.
  - That object has `.language_code`, `.is_generated`, `.to_raw_data()`, and snippets with `.text`, `.start` and `.duration`.
  - `video_id` must be the bare ID, **not a URL**.
- **Listing.**
  - `list(video_id)` returns a `TranscriptList`, with `find_transcript(langs)`, `find_generated_transcript(langs)` and `find_manually_created_transcript(langs)`.
  - Each `Transcript` has `.translate(code)`.
- **Exceptions:**
  - `TranscriptsDisabled`, `NoTranscriptFound`, `VideoUnavailable`, `VideoUnplayable`, `InvalidVideoId`, `AgeRestricted`
  - `RequestBlocked` and `IpBlocked`
  - `PoTokenRequired`, `YouTubeRequestFailed`, `YouTubeDataUnparsable`
  - all subclasses of `CouldNotRetrieveTranscript` / `YouTubeTranscriptApiException`.
- **No timeout parameter exists.** Pass in a `requests.Session` subclass that applies `[online] request_timeout_seconds` to every request.
- **Caveat (R19):** this library reads YouTube's internal web endpoints. It is **not an official API**, it can break when YouTube changes, and how it fits YouTube's Terms of Service is unverified. The developer should be aware of that before relying on it.

**Behaviour.**
1. **Play.**
   - **Request forms:** "play \<URL\>", a typed URL, or "play \<search words\>".
   - **Video-ID check:** extract the ID from `watch?v=`, `youtu.be/`, `shorts/` or `embed/` links. It must match `^[A-Za-z0-9_-]{11}$`, otherwise she says she couldn't read the link.
   - **With an ID:** call `open_url` for `https://www.youtube.com/watch?v=<id>` in the developer's **normal Chrome** (L1, F3's executor and verification). (JARVIS's own Playwright browser was removed on 27 October 2026; nothing but his Chrome is used.)
   - **With search words:** open `https://www.youtube.com/results?search_query=<words>` in normal Chrome (L1). Her own spoken words are L0/L1, so R4 doesn't apply. **Optional:** Playwright (F16b), run headless in the JARVIS profile and read-only, may resolve the search to a single video ID by reading the first result; the video then opens through `open_url`. If that isn't built or fails, she opens the search results page and says "Here are the results."
   - She remembers the last opened video ID **for the session only** (F10: not stored).
2. **Transcript / summarise.**
   - **Request forms:** "summarise this video" or "summarise the video I just opened" (uses the session's last video ID), or "summarise \<typed URL\>". Speaking a URL aloud is not supported, so the request must be typed or refer to the last video.
   - **Fetch:** on a worker thread, `fetch(id, languages=[preferred languages from settings])`.
     - On `NoTranscriptFound`: call `list(id)`, take any manually created transcript, then any auto-generated one, in the order the list returns them.
     - The record notes the language and whether it was auto-generated. She says "auto-generated captions, so names and terms may be wrong."
   - **Format:** join the snippet `.text` values. Keep `.start` times so she can report how much was covered.
   - **Summarise:** use the shared chunked-summary method (Groq budget, above).
     - The transcript is **internet content, not laptop data**, so R4 preview doesn't apply. R3 still does: "Searching online", ONLINE, and the log records destination Groq, size, the video ID, and the hash of the text sent.
     - If it was cut short, she says "I summarised the first 38 of 62 minutes."
     - The summary is spoken briefly (`[youtube] spoken_summary_sentences`) and shown in full on the dashboard.
   - **"Get the transcript" without a summary:** shown on the dashboard. It is saved to a file only if the developer asks; that is a write, at L2.

**Activity log:**
- **Play:** "reading video link" → "opening YouTube in Chrome" → "window appeared".
- **Summary:** "checking online: YouTube transcript (id dQw4…)" → "transcript found (en, auto-generated, 12,340 chars)" → "checking online: Groq summary, part 1 of 2" → "part 2 of 2" → "combining" → "summary ready".

**Trust.**
| Action | Level | Why |
|---|---|---|
| Play (open URL in Chrome) | L1 | Same as F3 opening a website. |
| Fetch transcript | L0 | Answering a question: sends only the video ID the developer gave. No fixed action. |
| Summarise via Groq | L0 | Sends internet content, not laptop data, so R4 doesn't apply. R3 does. |
| Save transcript to a file | L2 | A write: one preview showing path and size, then one confirm. R9 approved folders, no overwrite. Fixed action `write_text_file`. |

**Libraries/APIs.**
- `youtube-transcript-api==1.2.4` (MIT, free, no key). It pulls in `requests` and `defusedxml>=0.7.1,<0.8.0`; pin both.
  - **Limits:** YouTube publishes none. The library's own docs note that YouTube may block IPs that make many requests (`RequestBlocked` / `IpBlocked`).
- Groq (existing key, limits in the Groq budget above).

**Settings.**
```toml
[youtube]
preferred_transcript_languages = ["en"]
spoken_summary_sentences = 3
watch_url_template = "https://www.youtube.com/watch?v={id}"
search_url_template = "https://www.youtube.com/results?search_query={query}"
```

**Failure handling.**
- **No transcript** (`TranscriptsDisabled`, or `NoTranscriptFound` after the `list()` fallback): "This video has no transcript, so I can't summarise it." She never summarises from the title or guesses.
- `VideoUnavailable` / `VideoUnplayable` / `AgeRestricted` / `InvalidVideoId` → each has its own plain sentence.
- `RequestBlocked` / `IpBlocked` → "YouTube is blocking transcript requests from this connection right now."
- `PoTokenRequired` → "YouTube now needs a check this tool can't do; transcripts are unavailable."
- `YouTubeRequestFailed` with status 429 → shared R1 cooldown.
- Offline → "no internet".
- No Groq key → transcript is shown but there is no summary; she says why.
- Groq 429 → cooldown; the transcript is still shown.
- Chrome missing → "Chrome isn't installed" (F3 behaviour).

**Tests (network, library and Groq mocked).**
- Link parsing:
  - The ID is extracted from all four URL forms, plus a bare ID.
  - A 10- or 12-character "ID" is rejected.
  - A playlist-only URL → "couldn't read the link".
- Library calls:
  - `fetch` is called with `languages` from settings and the bare ID, never a URL.
  - `NoTranscriptFound` → `list()` fallback is used. If the list is empty → the no-transcript sentence, and Groq is not called.
  - Each exception class maps to its own sentence (a parametrised test over all listed classes).
  - The timeout Session is passed to the constructor.
  - One API instance is created per worker thread (assert the constructor is called inside the worker).
- Chunked summary:
  - 30,000 chars → 3 chunks plus 1 combining call. Spacing is respected (clock mocked).
  - 60,000 chars → 4 chunks only, with a coverage statement computed from `.start` times.
  - Stop between chunks → no further Groq calls and the record result is "stopped".
- Records and session:
  - The R3 log line has destination, size and hash. The record holds no transcript text over the redaction limit.
  - "Summarise the video I just opened" with no video this session → "Which video?"
  - The session video ID is not written to `jarvis_data`.
- Playback opens in Chrome through `open_url`.

**Effort.** About 10 hours.

**Risks.**
- The unofficial library breaks when YouTube changes (`PoTokenRequired` is a sign of that).
- Long videos exceed the free-tier token budget.
- Auto-caption errors appear in summaries.

### F19. File processor (stretch)

> **Built 28 October 2026 (the developer's F19 decisions; these override the draft text below where they differ).**
> - **Commands** (anywhere in a sentence; `read`, `what's in` and `summarise` are in `[interpreter] command_verbs`):
>   - "what's in \<file\>", "how many pages/words/rows in \<file\>", "word count of \<file\>": **local** answers ("report.pdf has 3 pages and about 25 words."; CSV: "budget.csv has 2 rows and 3 columns: date, item and amount.").
>   - "read \<file\>": the first 60 words aloud, local.
>   - "summarise \<file\>": online, after the R4 preview and an **L3** confirm (`send_text_online`).
>   - Sentences that aren't about a file ("read me a poem") fall through to the other matchers.
> - **Finding the file:** F4's index (its matching, PDF/DOCX/TXT/MD/CSV only), F4-style pick rules when several match (a numbered list in Live Activity, names only; "the second one" or part of a name, valid `[interpreter] pick_valid_seconds`), or a typed full path. **R9:** `check_path(READ)` before any read, so protected folders are never read.
> - **Extraction** (local, `jarvis/files/extract.py`):
>   - **PDF:** pypdf, the first 200 pages, page numbers kept. Encrypted: refused, never a password prompt (R11). Under 30 characters a page: "looks like a scanned PDF".
>   - **DOCX:** python-docx, paragraphs plus table cells.
>   - **TXT/MD:** utf-8 → utf-8-sig → cp1252.
>   - **CSV:** the dialect is sniffed; rows, columns, headers, numeric min/max/mean. Only the header, those numbers and the first 200 rows are sent.
>   - **Limits:** over 20 MB is refused. Nothing is ever written to his files, and there is **no conversion** (the developer's decision).
> - **Size limits** (the approved draft): at most 12,000 characters per request and 48,000 per file. Longer: "This file is long. Which part: the start, the middle, or the end?" The next answer picks 48,000 characters from there.
> - **R4:**
>   - **Before the confirm:** the exact text is written to `jarvis_data/tmp/f19-<id>.txt` and hashed. The Confirmation Queue card shows the name, size, SHA-256, the **exact text** (from that file), "This will be sent online." and "Online service terms not yet checked by you" (until **`[online] terms_checked = true`**).
>   - **After "confirm":** the file is re-hashed and sent only if unchanged. The chunks sent are exact slices of it: joined, they are byte for byte the previewed text (a test checks this).
>   - **Cleanup:** the temporary file is deleted after sending, on cancel, Stop or expiry, and at startup. **Cancel sends nothing.** In a Groq cooldown no preview is offered.
> - **Content never reaches Live Activity or debug.log:**
>   - **Live Activity** shows the file name, type, size, character counts, the hash and "confirmed / summary ready / temporary copy deleted". Her spoken reply appears there only as "saying: (about report.pdf: the file's content isn't shown here)" (new `Answer.shown`, and a 4th executor value).
>   - **debug.log** gets "sent text, 257 chars, to Groq (openai/gpt-oss-20b), 12:04, sha256 bcc939149c00…".
> - **Summary:** the shared chunked method (F18), at most 3 sentences spoken, with the short-opener rule. **Live check, 28 October 2026** (a synthetic file, Groq call 7 of 15): a 257-character file was summarised in 1.7 s, the temp file was deleted, and no content appeared in any activity line.
> - **Not built:** conversions and saving results (`write_text_file`, the developer's decision); cutting or editing the text inside the preview (cancel works); a page range for PDFs over 200 pages (only the first 200 are read, and she says so); a separate `read_document` action (local reads are answers, like F4's search).
> - **Packages:** `pypdf==6.19.0` (BSD-3-Clause; the version approved here, though 6.21.3 is the newest), `python-docx==1.2.0` (MIT), `lxml==6.1.3` (BSD-3-Clause).

**Precondition before F19 is built:** Groq data-use terms: developer to verify.

**Purpose.** This summarises or extracts from PDF, DOCX, TXT and CSV files via Groq, and converts between formats where free libraries can do it reliably.

**Behaviour.**
1. **Pick the file.**
   - "Summarise \<file\>" or "extract the tables from \<file\>" resolves the name **through F4** (the same index, matching and numbered pick list).
   - A typed full path is also accepted.
   - The file must pass `check_path(READ)`, so the protected list (Pictures) is refused and the reason is given.
2. **Size check (local).**
   - Files over `[file_processor] max_file_bytes` are refused, with the size given.
   - PDFs over `max_pdf_pages` pages: she asks for a page range.
3. **Extract text locally. No network.**
   - **PDF:** `pypdf`, text per page, page numbers kept.
     - If almost no text comes out (under `min_chars_per_page` on average), she says "This looks like a scanned PDF; I can't read scanned pages."
     - Password-protected PDF: refused. She **never asks for or handles the password** (R11).
   - **DOCX:** `python-docx`, paragraphs plus table cells.
   - **TXT/MD:** read as UTF-8; if that fails, try UTF-8 with BOM, then cp1252, and report which worked.
   - **CSV:** standard library `csv` with the dialect sniffed. Compute row and column counts, column names, and simple per-column stats (numeric min/max/mean, top values) **locally**. Only the header, those stats and the first `max_csv_rows_sent` rows are candidates to send.
4. **Local-only requests** finish here at L1 with no online step: "how many pages", "word count", "show the text".
5. **Online requests** (summarise, explain, extract specific items via Groq):
   - Build the exact text to send: the instruction plus the extracted text, capped at `max_chars_per_request × max_chunks_per_task`. If it's longer, the developer **picks a part** (pages for PDF, a percentage or heading range otherwise), as R4 requires.
   - Write that exact text to `jarvis_data/tmp/<uuid>.txt` and compute its SHA-256.
   - **R4 preview panel** shows: file name, size, page count, the **full text to be sent** (scrollable), character count, estimated tokens, destination ("Groq, openai/gpt-oss-20b"), the number of requests it will take, and the Groq terms line (shared rules).
   - The developer can cut or edit the text in the preview. An edit produces a new temp file and a new hash, and voids the old confirmation (Section 5).
   - **L3 confirm.** Then re-hash and send only if the hash matches, chunked as in the Groq budget.
   - Delete the temp file afterwards and on Stop.
   - The log records **only** "sent text, 18,422 chars, to Groq, 14:02, sha256 9f2c…". **Never the content** (R3: R4 takes precedence for laptop data).
   - The answer is spoken briefly and shown in full.
6. **Saving results or converting.**
   - Output only to an approved folder: DEFAULT the source's folder if approved, else Documents.
   - The output must pass `check_path(WRITE)`. If the name exists, she asks before overwriting.
   - **Conversions supported (free, pure Python, no Word or LibreOffice needed):**
     | From → To | Library |
     |---|---|
     | PDF → TXT | pypdf |
     | DOCX → TXT | python-docx |
     | TXT/MD → DOCX | python-docx (paragraphs only, no styling) |
     | CSV → JSON, JSON (list of flat objects) → CSV | standard library `csv`, `json` |
     | Summary → TXT or DOCX | as above |
   - **Not supported, with the reason said:**
     - PDF → DOCX with layout: free options are GPL- or AGPL-licensed or unreliable.
     - DOCX → PDF: needs Word or LibreOffice installed.
     - Any OCR: belongs to F9's document scanning.
     - XLSX: needs another dependency; not proposed.
   - A conversion is **local only**. Nothing goes online.

**Activity log (summarise):** "finding report.pdf" → "reading report.pdf locally (12 pages)" → "text extracted (18,422 chars)" → "preview ready: waiting for confirm" → "confirmed" → "checking online: Groq, part 1 of 2" → "part 2 of 2" → "summary ready" → "temporary copy deleted".

**Trust.**
| Action | Level | Why |
|---|---|---|
| Read and extract locally, local answers | L1 | Read-only, developer-asked, like opening a file (R9 read rule). Fixed action `read_document`. |
| Send extracted text to Groq | **L3** | Section 5: "Sending anything from the laptop online" is L3, with the R4 exact preview and hash check. Fixed action `send_text_online`. |
| Write a converted file or a summary file | L2 | Creates a file. One preview (output path, size, first lines), one confirm, R9 write rule. Fixed action `write_text_file` (shared with F18). |
| Overwrite an existing file | L4 (double confirm) | R9 "never overwrite without asking". Treated as destructive because the old content is lost. |

**Libraries.**
- `pypdf==6.19.0` (BSD-3-Clause, free, pure Python).
- `python-docx==1.2.0` (MIT, free). It needs `lxml>=3.1.0` and `typing_extensions>=4.9.0`; pin both.
- Standard library: `csv`, `json`, `hashlib`, `uuid`.
- Groq: existing key, limits in the Groq budget.

**Settings.**
```toml
[file_processor]
max_file_bytes = 20000000          # 20 MB: refuse larger files outright
max_pdf_pages = 200                # above this, ask for a page range before extracting
min_chars_per_page = 30            # below this on average, treat the PDF as scanned
max_csv_rows_sent = 200            # header + stats + this many rows at most
max_text_preview_chars = 200000    # preview panel cap (R15); it still shows the full text being sent
temp_dir = "jarvis_data/tmp"
default_output_folder = "%USERPROFILE%\\Documents"
text_encodings_tried = ["utf-8", "utf-8-sig", "cp1252"]
```

**Size limit, in one sentence:** files over 20 MB are refused; at most 12,000 characters go in one Groq request and at most 48,000 per task (4 chunks); anything longer requires the developer to choose the part to send.

**Failure handling.**
- **Missing file or protected path:** refused with the R9 reason.
- **Corrupt or unreadable file:** "I couldn't read \<name\>: \<plain reason\>", with no partial send.
- **Scanned PDF / encrypted PDF:** as above.
- **Offline / no key / 429:** the local extraction is still shown; she says the summary is unavailable and why. In a 429 cooldown, the preview isn't even offered.
- **Hash mismatch at send time:** refused: "The text changed after you confirmed; please check the preview again."
- **Write target not approved:** refused, and approved folders are suggested.
- **Disk full on write:** reported, and the partial output file is removed.

**Tests (temporary folders only; Groq mocked).**
- Path rules:
  - A file under a Pictures folder → refused before any read.
  - Writing to `C:\Windows` → refused.
  - Writing to Documents → allowed.
  - Existing output name → L4 overwrite path required.
- Extraction:
  - Fixture PDF (3 pages) → page text kept with page numbers.
  - A PDF with no text layer → "scanned" message, and Groq is not called.
  - An encrypted fixture PDF → refused, and no password prompt exists in the flow.
  - DOCX with a table → table cells included.
  - cp1252 TXT → decoded, and the encoding is reported.
  - CSV → local stats correct, and at most `max_csv_rows_sent` rows in the outgoing text.
- Size limits:
  - A 25 MB file → refused.
  - 250 pages → asks for a range.
  - 60,000 chars of text → must pick a part; 40,000 chars → 4 chunks.
- R4:
  - The sent bytes equal the temp-file bytes (hash compared).
  - Edit after preview → the old confirm is void.
  - Temp file deleted after success, after failure, after Stop, and leftovers deleted at startup.
  - The log line has type, size, destination, time and hash, and **contains none of the document text** (search the log for a fixture sentence).
- Conversions:
  - Round trip TXT → DOCX → TXT gives the same paragraphs.
  - CSV → JSON → CSV gives the same rows.
  - An unsupported pair → the reason sentence, and no file is written.

**Effort.** About 18 hours.

**Risks.**
- pypdf text order on complex layouts (columns, tables).
- Large previews in Flet (R15), which is why the preview has its own cap.
- Groq's data terms are unchecked (see the precondition).
- Developers may expect PDF→DOCX, which is deliberately out.

### F20. Topic monitor (stretch)

> **Built 28 October 2026 (the developer's F20 decisions; these override the draft text below where they differ).**
> - **Commands** (anywhere in the sentence; `track`, `start tracking`, `stop tracking` and `keep track of` were added to `[interpreter] command_verbs`): "track \<topic\>", "stop tracking \<topic\>", "what am I tracking", and "any news on my topics" / "check my topics". The phrases are in `[topics]`.
> - **Trust:** `edit_topic_list` is **L0** (the developer's decision; the draft said L1): his own words, local, and read back after saving (R12). Listing is a local answer. Checking is L0: the topics are his own words (C5), so there is **no preview**.
> - **Storage:** `jarvis_data/topics.json`, capped at **20 topics** (`[topics] max_topics`; the draft said 10), 60 characters each. It holds each topic, the date added and up to 200 hashes of article URLs already spoken. Headline text is never stored. Writes are atomic and R9-checked (including the rename of a damaged file, which is kept as `topics.corrupt-<time>.json`). A topic that looks like a card, bank or ID number is refused. There is no panel (as for F10).
> - **When:** only on the **first wake of the day** (`Orchestrator.on_first_wake_of_day`, decided by the wake greeter's greeting-day) and on demand. No timer, thread or scheduler (a test checks the module source). The first-wake check is JARVIS-started: it runs through F17's new `STARTUP_TASK_REQUESTED`, so its record says "started by JARVIS: topic check (first wake of the day)" with origin JARVIS_INITIATED. When nothing is tracked, nothing is said or sent. (From F21 on, the briefing takes over this slot when `[briefing] enabled`; see F21.)
> - **Source (§11 #7 closed):** **Bing News search RSS**, one request per topic: `https://www.bing.com/news/search?q=<topic>&format=rss` (`[news] topic_source = "bing"`).
>   - Terms, read 28 October 2026 in the feed's own `<copyright>` element: "These XML results may not be used, reproduced or transmitted in any manner or for any purpose other than rendering Bing results within an RSS aggregator for your personal, non-commercial use." JARVIS reads them for the developer only, so this fits. `bing.com/robots.txt` doesn't disallow `/news/search`.
>   - **Rejected:** Google News RSS. Its feed terms allow "a personal feed reader", but `news.google.com/robots.txt` disallows `/rss` for every agent. Also rejected: GDELT, the old candidate, which has no published terms or limits.
>   - **Fallback:** `topic_source = "feeds"` filters the general feeds (F21) by the topic's words instead, with no per-topic request.
> - **New** = an article URL not in that topic's seen hashes, published within `[news] topic_max_age_hours` (48). At most 3 per topic, each cut to 14 words. Spoken: "2 new headlines on indian cricket: \<title 1\>. \<title 2\>." then "Nothing new on solar or the matildas.", or "No new headlines on your topics." Hashes are added only after the reply has been spoken, never after Stop.
> - **R3/R4:**
>   - "Searching online" is said once, and the Internet panel shows ONLINE.
>   - Live Activity shows `searching online, sending "<topic>"` per topic and `topic N: K new`, for the session only.
>   - debug.log gets "Bing News search RSS; N topic(s) sent: text, 14 characters, sha256 …; \<time\>" and a summary of the reply, never a topic or a headline (new `Answer.logged`).
> - **Failures:**
>   - Offline: "I can't reach the news right now. Check your internet connection, then try again."
>   - One topic failing: "I couldn't check \<topic\>."
>   - 429: the shared cooldown, "The free limit for news is reached; it is off until HH:MM."
>   - Malformed or unsafe XML (parsed with `defusedxml`): a service error. No crash.
> - **Package:** `defusedxml==0.7.1` (PSFL, latest on PyPI, released 8 March 2021).
> - **Requests** are spaced `[news] request_spacing_seconds` (1.5) apart, and Stop is checked between topics.

**Purpose.** The developer keeps a short list of news topics. At startup only, JARVIS checks each one for headlines not seen before and passes them to the F21 briefing.

**Behaviour.**
1. **Manage the list (local, no network):**
   - "Monitor topic \<words\>" adds a topic.
   - "Stop monitoring \<words\>" removes one.
   - "List my topics" lists them.
   - **Storage:** `jarvis_data/topics.json`. Each entry has `{topic, added_on}` plus the hashes of headline URLs already seen (up to `max_seen_per_topic`, oldest dropped first). Headline text is not stored.
   - **Limits:** topics are checked against `max_topics` and `max_topic_chars`, and duplicates are rejected without regard to case.
   - **Panel:** the developer can see and delete topics in the F10-style memory panel. Deleted means deleted.
2. **Check (startup only).**
   - The check runs **only as part of the F21 startup flow, under F21's standing approval (Section 5)**.
   - There is **no background thread, no timer and no scheduled run** (scheduled skill runs belonged to F12, which is cut). With the briefing turned off, topics are never checked automatically, though "check my topics now" still works on demand.
   - For each topic, one request with the `[news] topic_source` query: the topic words, a recent time span and `max_headlines_per_topic` results.
   - Requests are spaced `[news] request_spacing_seconds` apart, and Stop is checked between topics.
   - **New** means the URL hash is not in that topic's seen set. New headlines are handed to F21, and their hashes are added to the seen set after the briefing has spoken them.
3. **On-demand check** ("check my topics now"): the same flow, outside startup. The stored topics are values the developer typed for an online purpose, so under R4's amendment they count as his own words: **L0**, logged per R4.

**Activity log:** "topic added: 'electric buses'" / "checking online: news for 3 topics" → "topic 1: 2 new" → "topic 2: 0 new" → "topic 3: offline, skipped". (Topic names are not written to the log file, per R4.)

**Trust.**
| Action | Level | Why |
|---|---|---|
| Add / remove / list topics | L1 | Developer-asked, local, reversible, inside `jarvis_data` (approved). Fixed action `edit_topic_list`. Every change is shown in the log, like an F10 explicit memory. |
| Check topics at startup | L3 (F21 standing approval) | JARVIS-started (Section 5 origin rule). |
| Check topics on demand | L0 | Developer-asked; stored topics count as his own words under R4's amendment. |

**Libraries/APIs (draft; OPEN #7 was closed on 28 October 2026 with Bing News search RSS, see the Built block above).**
- **Candidate: GDELT DOC 2.0 API**, `https://api.gdeltproject.org/api/v2/doc/doc`.
  - Parameters: `query`, `mode=ArtList`, `format=json`, `maxrecords` (DEFAULT 75, max 250), `timespan` (for example `1d`), `sort=DateDesc`.
  - Free and **no key** as far as its documentation says.
  - **Its documentation states no rate limit or terms** (verified 3 October 2026, GDELT DOC 2.0 announcement page). Be gentle: one request per topic, spaced at least 6 seconds apart. The 6 seconds is a cautious choice, **not a published GDELT figure**.
  - Recheck the terms before relying on it.
- Standard library `urllib` and `json`. No new dependency.

**Settings.**
```toml
[topics]
store_path = "jarvis_data/topics.json"
max_topics = 10
max_topic_chars = 60
max_seen_per_topic = 200

[news]
topic_source = "gdelt"             # OPEN #7: developer to confirm
gdelt_endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"
topic_timespan = "1d"
max_headlines_per_topic = 3
request_spacing_seconds = 6
```

**Failure handling.**
- **Offline:** "Topics: offline, skipped." The briefing goes on without them.
- **429 or other error:** the shared cooldown; that topic is marked "unavailable" and the others continue.
- **Malformed JSON:** that topic is skipped and the problem logged.
- **Corrupt `topics.json`:** renamed to `topics.corrupt-<timestamp>.json`. She says so and starts an empty list. **She never deletes it silently.**
- **Topic text matching an R11 sensitive marker:** refused, and not stored.

**Tests.**
- Managing the list:
  - Add, list and remove round trip in a temporary `jarvis_data`.
  - A duplicate (case-insensitive) is rejected; max topics and max length are enforced.
  - A sensitive-marker topic is refused and never written.
- Checking:
  - Only unseen URLs are "new".
  - Seen hashes are capped at `max_seen_per_topic`.
  - Hashes are updated only after F21 reports the headlines were spoken; with the briefing stopped, they are not updated.
  - **No scheduler:** nothing in the module creates a timer or thread except the startup or on-demand call. Assert that no `threading.Timer` or scheduler is used, and that importing the module makes no network call.
  - On-demand check → no preview; the log line follows R4 (no topic text).
- Failures:
  - Offline → skip message and the briefing continues.
  - Spacing honoured between requests (clock mocked).
  - Corrupt file → renamed, not deleted.

**Effort.** About 6 hours.

**Risks.**
- GDELT's undocumented limits or terms.
- Headline relevance from keyword queries.

### F21. Morning briefing (stretch)

> **Amended 1 November 2026 (the developer): the briefing is NEVER automatic.** The first-wake trigger is removed completely (code, settings and this spec): no `Orchestrator.on_first_wake_of_day`, no `briefing_online` action, no `last_briefing_date`, no `[briefing] enabled`/`state_path`, and `trust.STANDING_APPROVAL_ACTIONS` is empty. It runs ONLY when he asks: "brief me", "morning briefing", "give me my briefing" (`[briefing] on_demand_phrases`, or the router's `briefing` intent), at L0 with no preview. F20's topics are likewise checked only on request ("any news on my topics", or inside a briefing he asked for). Where the text below says "first wake", "standing approval" or `briefing_online`, it is history and no longer applies.
>
> **Built 28 October 2026 (the developer's F21 decisions; these override the draft text below where they differ).**
> - **When (§11 #14 closed):** on the **first wake of the day**, since she starts asleep. It runs right after the wake greeting, through F17's `STARTUP_TASK_REQUESTED` (origin JARVIS_INITIATED). "Brief me" / "morning briefing" (`[briefing] on_demand_phrases`) runs it on demand. **`[briefing] enabled = true`** (the developer's decision; the draft said false). It runs once a day: `last_briefing_date` in `jarvis_data/briefing_state.json` is written only after it finished or he skipped it, never after Stop. With the briefing off, the first wake still checks F20's topics alone.
> - **Order:** greeting (the wake greeting; on demand a time-of-day "Good morning, Priyansh.") → date ("It's Wednesday the 28th of October.", the laptop clock) → the weather for `[weather] default_city` (Sydney), using **F16a's own `WeatherClient` and answer text** (the same client object, so one cooldown) → the top 3 general headlines (BBC and NPR in turn) → F20's new topic headlines. Parts are added while they fit **about 30 s** (`max_spoken_seconds = 30` × `words_per_second = 2.5`). Whatever doesn't fit is listed in Live Activity, and she ends with "More in Live Activity." Topic headlines that weren't said stay new.
> - **Standing approval (Section 5, C4):**
>   - **The fixed action** is `briefing_online` (**L3**). It is the only entry in `trust.STANDING_APPROVAL_ACTIONS`.
>   - **First time:** she says the date, then "Shall I check the weather and news? Say confirm." The Confirmation Queue card shows the exact outbound list. Every destination host and every string sent is on it, for example `api.open-meteo.com (weather): "Sydney"`, each feed URL "(headlines, no query)", and `www.bing.com/news (news search): "solar"`. It also shows "This will be sent online, now and on every first wake of the day while this list stays the same. Any change asks again."
>   - **The confirm** stores the SHA-256 of that list.
>   - **Later first wakes:** if the list hashes the same, the **controller** issues the permit with no confirm, after its own hash check. This happens only for a request the controller itself tagged JARVIS_INITIATED, never for a payload's claim. The transition log says "under the standing approval". "Searching online" is still said and ONLINE is still shown.
>   - **Any change** (city, feed, topic) asks again. If the list changes between the preview and the confirm, nothing is sent.
> - **Skip and Stop:** spoken or typed "skip" while she waits for the confirm is a cancel ("Cancelled."; it counts as skipped for today, and nothing is sent). Typed "skip" while it runs is a Stop. Stop ends it at once and marks nothing.
> - **Offline:** greeting + date + "I can't reach the internet for weather and news right now." One source failing: "I couldn't get the weather." / "I couldn't get the headlines." / "I couldn't check \<topic\>."
> - **No recap** of earlier sessions (F10). **R3/R4:**
>   - Live Activity shows each step: "checking online: weather, default city", the Open-Meteo credit line, "checking online: headlines (2 feeds)", "BBC News: \<title\>" per headline (BBC's terms ask for that credit), and the topic lines (session only).
>   - debug.log gets the destinations, with the city and the topics only as size and hash.
> - **Feeds and terms (read 28 October 2026):**
>   - **BBC:** terms §15 (v1.8, updated 19 September 2022) allow adding the feed with the credit "BBC News".
>   - **NPR:** "You may display, excerpt from, and link to the Content Feeds on your personal site or application."
>   - **The Guardian is not used.** Its terms forbid "any … automated device, program … for … reading" its content unless expressly allowed.
> - **Not built:** the dashboard on/off toggle (`set_jarvis_setting`). The setting is changed in `settings.toml`. A separate permit per online step was also not built: one permit covers the hashed list.
> - **Packages:** none new (`defusedxml` from F20).

**Purpose.** At startup, a short spoken briefing: greeting, date, weather, headlines and new topic results. It must work offline, giving the local parts only.

**Behaviour.**
1. **Off unless enabled** (`[briefing] enabled`, DEFAULT false). The developer turns it on in settings or from a dashboard toggle; the toggle writes settings and is logged.
2. **When it runs.** At startup, after the dashboard is up and the controller is in WAITING, and **only on the first start of the day**: it compares the date with `jarvis_data/briefing_state.json`.
   - It never runs after waking from Sleep Mode (F15).
   - If JARVIS starts in, or the developer puts her into, ASLEEP before it finishes, it is cancelled.
3. **Local part (no network, always runs).**
   - Time-of-day greeting from F2's settings boundaries.
   - Today's date and the time, from the system clock.
   - No previous-session recap of any kind, **because F10 never stores conversations**.
4. **Online part (JARVIS-started, so L3 under the standing approval in Section 5).**
   - The controller submits `STARTUP_TASK_REQUESTED` (F17). The controller sets the origin to **JARVIS_INITIATED** from the event type; no module can set it.
   - The outbound list is built: every destination and every string that will be sent, for example:
     ```
     Open-Meteo geocoding: "Leeds"
     Open-Meteo forecast: lat 53.80, lon -1.55
     RSS: https://feeds.bbci.co.uk/news/rss.xml (no query)
     RSS: https://www.theguardian.com/world/rss (no query)
     RSS: https://feeds.npr.org/1001/rss.xml (no query)
     GDELT: "electric buses", "local elections", "solar"
     ```
   - Its SHA-256 hash is compared with the approved hash stored in `jarvis_data/briefing_state.json`.
     - **First time, or the list changed:** the preview panel shows the exact list. She says "Good morning. It's Friday the 3rd of October. Shall I check the weather and news? Say confirm." One "confirm" within the 60-second expiry approves it and stores the new hash. Not confirmed: she says "Okay, skipping the online part" and nothing is sent.
     - **Hash matches:** the online part runs without asking, still saying "Searching online" and showing ONLINE.
   - Each source runs on a worker thread in order (weather → general headlines → topics), each with its own timeout, its own one-time permit and a Stop check.
5. **Sources.**
   - **Weather:** **calls F16a's function**. There is no separate weather code.
   - **General headlines:** the RSS feeds in `[news] general_feed_urls`, no key, parsed with `defusedxml`, the first `max_general_headlines` titles across the feeds.
   - **Topics:** calls F20's check.
6. **Speaking.**
   - Total spoken length is capped at `max_spoken_seconds`, estimated from word count. Headlines are read as titles only.
   - The full list, with source links, is shown in a dashboard panel.
   - Stop cuts the briefing at once (R8).
7. **After speaking.** Write `last_briefing_date`, and tell F20 which headlines were spoken.

**RSS feeds (verified 3 October 2026: each returned HTTP 200 with `text/xml` RSS).**
- BBC News top stories: `https://feeds.bbci.co.uk/news/rss.xml`
- The Guardian, World news: `https://www.theguardian.com/world/rss`
- NPR News: `https://feeds.npr.org/1001/rss.xml`

Each feed's terms of use have **not been checked**; the developer should check them before submission.

**Activity log:** "startup briefing: local part" → "spoke greeting and date" → "outbound list matches standing approval" (or "preview issued (L3, JARVIS-initiated)" → "confirmed") → "checking online: weather" → "weather ok" → "checking online: headlines (3 feeds)" → "5 headlines" → "checking online: topics (3)" → "2 new" → "briefing finished (48 s)".

**Trust.**
| Part | Level | Why |
|---|---|---|
| Greeting, date, time | L0 | Local, no action. |
| Weather, news and topics at startup | **L3, standing approval** | Section 5: "Anything JARVIS starts herself" is L3. Section 5's standing approval binds one confirm to the hash of the exact outbound list. |
| Toggle on/off | L1 | A settings change for JARVIS's own behaviour, developer-asked, logged. Fixed action `set_jarvis_setting`, limited to an allowlist of keys. |

**Libraries/APIs.** None new: F16a, F20, the RSS feeds via `urllib` + `defusedxml`, and pyttsx3 (F2).

**Settings.**
```toml
[briefing]
enabled = false
state_path = "jarvis_data/briefing_state.json"   # last_briefing_date and the approved outbound-list hash
max_spoken_seconds = 60
words_per_second = 2.5             # used to estimate spoken length
include_weather = true
include_headlines = true
include_topics = true

[news]
general_feed_urls = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://feeds.npr.org/1001/rss.xml",
]
max_general_headlines = 5
```

**Failure handling (degrade to the local part).**
- **Offline:** greeting, date and time, then "I'm offline, so no weather or news this morning."
- **One source fails:** say which, and continue with the rest.
- **Cooldown active:** that part is skipped with the cooldown sentence.
- **No default city:** weather is skipped ("no default city set").
- **Feed list empty:** headlines are skipped.
- **TTS missing:** the briefing is shown on screen only.
- **Startup must never wait on the network:** the dashboard and typed-command box are usable while the briefing waits for its confirm.

**Tests.**
- When it runs:
  - Disabled → nothing happens at startup, and no network mocks are called.
  - Enabled, first start of the day → local part spoken.
  - Second start the same day → no briefing.
  - Never runs after F15 wake.
- Standing approval and origin:
  - The L3 pending confirmation is created with origin JARVIS_INITIATED. A payload claiming another origin is ignored.
  - First run → preview lists the exact strings; no confirm within the expiry → zero HTTP calls.
  - Approved hash stored; next startup with the same list → runs with no preview.
  - Any change to the list (city, topic, feed URL) → approval void, preview shown again.
- Offline and failures:
  - Offline → local part plus offline sentence; no exception.
  - Weather fails, news succeeds → both outcomes spoken correctly.
- Reuse and content:
  - Weather comes **from the F16a function** (mock it and assert the call; also assert no other module imports the Open-Meteo URLs).
  - No recap: the spoken text contains nothing from previous sessions. Seed a decision record and assert it isn't mentioned.
- Speaking:
  - Spoken length is capped.
  - Stop during speech → speech cut and the rest skipped.
  - `last_briefing_date` is written only after completion or a confirmed skip, not on a crash.
- F17:
  - The new event and transition are present in the fixed table.
  - Invalid transitions (for example `STARTUP_TASK_REQUESTED` while ACTING) are rejected.

**Effort.** About 10 hours, including the F17 table changes and tests.

**Risks.** RSS feed terms and stability.

### F22. System settings (stretch)

> **Built 28 October 2026 (the developer's F22 decisions; these override the draft text below where they differ).**
> - **Commands** (`jarvis/system/commands.py`): volume up/down/set to N (digits or words, "forty five")/mute/unmute; brightness up/down/set to N ("dim the screen"); Wi-Fi on/off; lock the screen; restart; shut down. A command later in a sentence is found by the interpreter's existing extraction (F22's verbs were added to `[interpreter] command_verbs`). The parser then matches the whole command, so "how do I turn the volume up" (a question) and "the TV is on mute" (a statement) do nothing.
> - **Trust** (fixed actions, `trust.py`): `set_volume`, `set_mute`, `set_brightness` **L1**; `lock_screen` **L2**; `set_wifi` **L3**; `restart`, `shutdown` **L4** (double confirm). Values outside 0–100 and steps over `max_step_percent` (50) are answered before policy and never reach a permit.
> - **Verify (R12):** every change is read back (volume ±1, brightness ±5, mute, Wi-Fi polled until `verify_timeout_seconds`). On a mismatch she says "Troubleshooting the issue", tries once more, then reports the real value ("I set the volume to 60%, but it's reading 35%."). Lock: "Screen lock requested." Restart: "Restart requested. It starts in 30 seconds unless an app stops it. Say stop to cancel." Never "done".
> - **Restart and shutdown:** the card lists the open windows (F8's read-only listing; protected ones are shown as "a protected window") with "Restart in 30 seconds. Apps won't be force-closed…". After the second confirm the log handlers are flushed and `InitiateSystemShutdown(None, message, 30, False, reboot)` runs, **force always False**. During the countdown, Stop (button, hotkey or spoken), plain "cancel" or "cancel the restart" calls `AbortSystemShutdown`: "Restart cancelled." Too late: "It's too late to cancel: Windows is already restarting."
> - **Wi-Fi:** WinRT `Radio.RequestAccessAsync()`, then the Wi-Fi radio's `SetStateAsync`. Refused: "Windows doesn't allow JARVIS to switch Wi-Fi on this laptop." Never `netsh` or any terminal command (a static test checks there is no subprocess, `os.system` or `netsh` in F22's code). Turning it off says first: "Every online feature stops working until it's back on." Already in that state: answered without asking.
>   - **Read-only check on this laptop (28 October 2026; `tools/f22_readonly_check.py`):** `Radio.get_radios_async()` sees 2 radios, **Wi-Fi (state ON)** and Bluetooth (OFF). `RequestAccessAsync` and `SetStateAsync` were **not** called (they might prompt, and nothing was to be changed), so whether Windows *allows* the switch for an unpackaged Python app is still untested. pycaw reads "Speaker (Realtek(R) Audio)". screen-brightness-control reads one panel through WMI.
> - **Brightness unsupported** (an external monitor without DDC/CI, or a VM): "Brightness isn't controllable on this display." **No speakers:** "No speakers found…". **A missing library** names itself: "That needs the pycaw package, which isn't installed. Run pip install -r requirements.txt in the JARVIS folder, then restart JARVIS."
> - **"Volume up" while muted** also unmutes, because otherwise nothing would be heard.
> - **Not built:** Undo. Section 5's undo stack doesn't exist in JARVIS yet, so no reverse steps are registered.
> - **Tests never touch the real system.** `tests/conftest.py` refuses `WindowsSystem`'s setters, `LockWorkStation`, `InitiateSystemShutdown`, `AbortSystemShutdown`, `ExitWindowsEx` and `set_brightness` in every test.
> - **Packages:** `pycaw==20260927` (MIT), `screen-brightness-control==0.27.2` (MIT), `WMI==1.5.1` (MIT), `winrt-Windows.Devices.Radios==3.2.1` (MIT; 3.2.1, not the newer 4.x, so it matches the pinned `winrt-runtime` 3.2.1).

**Purpose.** Volume, mute, brightness, Wi-Fi on/off, lock, restart and shutdown by voice. Each is checked after acting, and none uses a terminal command.

**Behaviour (common).**
1. The matcher returns a typed request, for example `set_volume{percent:40}` or `change_volume{delta:-10}`. The parameters are validated: percentages 0–100, deltas within ±`max_step_percent`.
2. Policy, then preview or confirm per the table below, then a permit, then the executor on a worker thread.
3. **COM-based libraries** (pycaw, WinRT) initialise COM on the worker thread before use.
4. **Verify (R12):** read the state back and compare it with the target, within tolerance. On failure: "Troubleshooting the issue", retry once, then report the actual state ("I set it to 40, but it's reading 35").
5. **Undo:** reversible actions register their reverse step (Section 5 Undo). Volume, mute, brightness and Wi-Fi are reversible; lock, restart and shutdown are not.

**Per action.**

| Action | How (no terminal, R10) | Verify (R12) | Trust | Why |
|---|---|---|---|---|
| Volume set / up / down | pycaw: default speakers' endpoint volume (`AudioUtilities.GetSpeakers()` → its `EndpointVolume`; 20260927 also has a `volume_percent` helper that clamps to 0–100) | Read the level back; within ±1% | **L1** | Reversible in one step, harmless, developer-asked. |
| Mute / unmute | pycaw endpoint volume `SetMute` / `GetMute` | Mute state matches | **L1** | Same. |
| Brightness set / up / down | `screen-brightness-control`: laptop panel via WMI | Read back; within ±`brightness_tolerance` (panels jump in steps) | **L1** | Reversible, harmless. External monitors without DDC/CI support: "Brightness isn't controllable on this display." |
| Wi-Fi on / off | WinRT `Windows.Devices.Radios`: `Radio.RequestAccessAsync()` once, then pick the radio of kind WiFi, then `SetStateAsync(On or Off)` | Wait for `StateChanged`, or re-read `State` until `verify_timeout_seconds` | **L3** (one confirm) | Level set by the developer. Turning Wi-Fi off cuts every online feature (Groq, weather); the preview says so. |
| Lock screen | `user32.LockWorkStation` via ctypes (a session API, not input injection or focus) | Non-zero return = lock started. **A full check from inside a locked session isn't possible; documented as the R12 limit for this action.** | **L2** | A misheard "lock" interrupts a demo, and only the developer can unlock (R11: she never types his password). |
| Restart | pywin32: enable the shutdown privilege on JARVIS's own process token, then `InitiateSystemShutdown(..., timeout=grace, force=False, reboot=True)` | API success = restart scheduled. Completion can't be checked from a machine that is restarting; documented as an R12 limit. | **L4** (double confirm) | Required by the developer. Closes everything. |
| Shutdown | Same, `reboot=False` | Same | **L4** (double confirm) | Same. |

**Restart and shutdown details.**
1. **Preview:**
   - "Restart in 30 seconds. Apps won't be force-closed. Unsaved work in another app may stop the restart. Say confirm twice."
   - The open top-level window titles are listed so the developer can see what's running. These are read with the existing `win32gui` window listing (a read-only call, not a focus or show call).
2. **After the second confirm:**
   - Flush the logbook and debug log.
   - Schedule the restart or shutdown with the OS's own countdown (`[system] shutdown_grace_seconds`). **Never `force=True`**, so apps with unsaved work can block it.
3. **During the countdown:** **Stop**, "cancel" or the Stop hotkey calls `AbortSystemShutdown` (pywin32). This works because F22 registers a transition listener (the same mechanism F4 uses).
   - This fits Section 5 ("cannot undo something already finished") because the shutdown has not finished yet.
   - She confirms aloud: "Restart cancelled."

**Wi-Fi: needs laptop test; says plainly why if Windows refuses.**
- The Microsoft docs list the `radios` app capability for `SetStateAsync`.
- Whether `RequestAccessAsync` returns *Allowed* for an **unpackaged desktop Python process** has **not been tested**, and the docs don't say. Test it on the laptop **before** building this row.
- If it returns anything else, she says and the Wi-Fi row shows: "Windows doesn't allow JARVIS to switch Wi-Fi on this laptop." **It never falls back to `netsh` or any terminal command** (R10).
- A hardware Wi-Fi switch or policy can override an *Allowed* request (Microsoft docs), which is why the state is re-read.

**Activity log:**
- **Volume:** "set volume 40%: immediate (L1)" → "volume now 40%".
- **Restart:** "restart: preview (L4)" → "confirm 1 of 2" → "confirm 2 of 2" → "restart scheduled in 30 s (apps not forced)" → either "restart cancelled by Stop" or nothing further, because the machine restarts.

**Libraries.**
- `pycaw==20260927` (MIT, free). It needs `comtypes>=1.4.8` and `psutil>=5.9.0`; psutil 7.2.2 is already pinned, and comtypes should be pinned explicitly.
- `screen-brightness-control==0.27.2` (MIT, free). On Windows it requires `wmi`, `pywin32` and `pypiwin32`; pywin32 312 is already pinned. `pypiwin32` is an old compatibility package; check that it installs cleanly in the clean-venv test.
- `winrt-Windows.Devices.Radios==3.2.1` with `winrt-runtime==3.2.1` (MIT, free, part of the pywinrt project, Python ≥ 3.9). The `Windows.Foundation` packages are optional extras; add them only if the clean-venv test needs them.
- `pywin32==312`, already pinned: shutdown privilege, `InitiateSystemShutdown`, `AbortSystemShutdown`.
- Standard library `ctypes`: `LockWorkStation`.
- No API, no key.

**Settings.**
```toml
[system]
volume_step_percent = 10
max_step_percent = 50
brightness_step_percent = 10
brightness_tolerance_percent = 5
volume_tolerance_percent = 1
verify_timeout_seconds = 5.0
verify_poll_interval_seconds = 0.25
shutdown_grace_seconds = 30
```

**Failure handling.**
- **Library missing:** that row names the missing library and says "Run pip install -r requirements.txt in the JARVIS folder, then restart JARVIS."
- **No audio device:** "No speakers found."
- **Brightness unsupported** (desktop monitor, VM): "Brightness isn't controllable on this display."
- **Radio access denied / no Wi-Fi radio:** said plainly.
- **Privilege enable fails:** "Windows didn't allow me to restart, so no restart was requested."
- **Shutdown blocked by an app:** the OS handles it. JARVIS **says** only "restart requested" or "shutdown requested", never "done" or "scheduled" (R12). The activity log may record "restart scheduled in 30 s" as a log entry.
- **Read-back mismatch:** retry once, then report the real value.

**Tests (all OS calls mocked; tests never change real settings or shut anything down).**
- Trust levels from the fixed table:
  - Volume, mute and brightness → L1.
  - Lock → L2.
  - Wi-Fi on and Wi-Fi off → L3, one confirm.
  - Restart and shutdown → L4, needing 2 confirms; 1 confirm doesn't schedule.
- Parameters: out-of-range values (−5, 150, delta 80) are rejected before policy.
- Verification:
  - Read-back mismatch → one retry, then the mismatch sentence.
  - Brightness within tolerance → success.
- Wi-Fi: `RequestAccessAsync` denied → "Windows doesn't allow JARVIS to switch Wi-Fi on this laptop.", and the code path contains no `subprocess` call (assert `subprocess` isn't imported in the module).
- Restart and shutdown:
  - `force=False` is always passed.
  - Stop during the countdown → `AbortSystemShutdown` is called.
  - Stop after the countdown can't apply (mock "already shutting down") → reported honestly.
- R10: there are no terminal or `os.system` calls anywhere in F22 (a static check over the module source).
- Undo: after a volume change the undo restores the previous level; restart is never put on the undo stack.

**Effort.** About 16 hours, plus about 1 hour of laptop testing for Wi-Fi access before committing to that row.

**Risks.**
- WinRT radio access for an unpackaged app.
- WMI brightness quirks with the AMD + NVIDIA hybrid graphics.
- COM threading errors if a worker forgets to initialise COM.
- The `pypiwin32` install.

### F23. Multilingual replies (CUT)

> **CUT 25 October 2026 (the developer's decision: "not needed for this submission").** Not built and not planned. The text below is kept only as a record of what was specified; none of it applies.

**Needs laptop voice test: confirm pyttsx3 can see a non-English voice before building; if not, F23 replies in that language as text and speaks in English.**

**Purpose.** When the developer asks a Q&A or small-talk question in another language, JARVIS answers in that language, using an installed Windows voice for it. If none is installed she shows the reply in that language as text and speaks in English (Zira), saying why.

**Scope limits (stated so nobody assumes more):**
- Only **F2 Q&A and small-talk answers** (the Groq path) become multilingual.
- **Commands stay English:** "open", "find", "confirm" and the F7 words are matched deterministically in English.
- **The F1 intro is always English, word for word:** its matcher returns the locked text and never goes to Groq or translation.
- The F2 rudeness list is English-only (stated as a limit).
- Typed commands have no audio, so they are treated as English (DEFAULT).

**Verified in the installed libraries:**
- **faster-whisper 1.2.1:** `transcribe()` returns `TranscriptionInfo` with `language` (an ISO code such as "hi") and `language_probability`.
- **The `base` model F2 picked is multilingual**, not `base.en`. Keep it that way.
- **pyttsx3 2.99:** the SAPI5 driver builds each voice's `languages` from the voice token's LCID, turned into a locale string such as `en-US` (`drivers/sapi5.py`).
- **Limit:** pyttsx3's SAPI5 driver lists **SAPI voices only**.
  - Voices added through Windows Settings → Language are "OneCore" voices and are **likely not visible** to it. This is a known community issue, not checked on this laptop.
  - The spec records only David and Zira (both en-US) on this laptop.
  - Making OneCore voices visible needs registry edits, which are a system change and **not proposed**.

**Behaviour.**
1. After the wake word, transcribe the command.
   - Accept the detected language only if `language_probability ≥ min_language_probability` **and** the speech lasts at least `min_audio_seconds`. Otherwise use English, because short commands are unreliable.
2. If it is English → normal F2.
3. If it is another language and the interpreter routes it to Q&A (Groq):
   - **Voice lookup:** find an installed pyttsx3 voice whose locale starts with the detected code (for example `hi-`). If several match, use the one in `[multilingual] preferred_voices`, else the first.
   - **Found:** Groq's system prompt adds "Reply only in \<language name\>". After Groq answers, switch the TTS voice for that one reply, then **switch back to Zira**.
   - **Not found:** Groq still replies in the detected language, and that reply is **shown as text** on the dashboard. Before speaking, she says in English: "I don't have a \<language\> voice installed, so I'll answer in English." The spoken answer is in English (Zira), because Zira reading another script would be unintelligible.
4. The decision record adds: detected language, probability, voice used, and whether the fallback was used.
5. **Only Zira is ever stored as the setting.** Per-reply voice switches are never persisted.

**Activity log:** "heard (hi, 0.93): …" → "no Hindi voice installed: text in Hindi, speaking in English" → "checking online: Groq" → "answered".

**Trust.** **L0** (answering a question; it affects speech only). There is no new fixed action. Language and voice choice are deterministic code (a locale match), never a model's decision.

**Libraries.** None new: faster-whisper 1.2.1 (already chosen), pyttsx3 2.99 (already chosen), Groq (limits in the Groq budget; non-English text usually takes more tokens per word, so the free budget goes faster).

**Settings.**
```toml
[multilingual]
enabled = false
min_language_probability = 0.70
min_audio_seconds = 1.5
default_voice_name = "Microsoft Zira Desktop"
preferred_voices = {}              # e.g. { hi = "<exact voice name>" } if one is ever installed
fallback_note = "I don't have a {language} voice installed, so I'll answer in English."
```

**Failure handling.**
- **Low probability or short audio:** English.
- **Voice switch throws:** fall back to Zira and log it.
- **Groq answers in the wrong language:** accepted and logged, because we can't reliably check it.
- **Groq unavailable:** F2's spoken fallback in English.
- **Feature off:** everything is English, as today.

**Tests (whisper, pyttsx3 and Groq mocked).**
- Language decision:
  - `("hi", 0.93)` with 3 s of audio → Hindi path.
  - `("hi", 0.55)` → English.
  - `("hi", 0.95)` with 0.8 s of audio → English.
- Voice choice:
  - A fake voice list with `hi-IN` → that voice selected; the Groq prompt contains the language instruction.
  - Only en-US voices → fallback note spoken first, the Hindi reply shown as text, and the spoken answer in English with Zira.
  - After a foreign-language reply, the active voice is Zira again, and settings are unchanged.
- Fixed English paths:
  - "Introduce yourself" said with a non-English detection → the exact F1 text in English, and Groq is not called (byte-for-byte check against `intro.txt`).
  - Typed input → always English.
  - Commands ("open notepad") are unaffected by detected language.
- Record: has detected language, probability and voice fields.

**Effort.** About 10 hours, plus about 1 hour on the laptop checking which voices pyttsx3 can see.

**Risks.**
- **On this laptop the feature may never produce a non-English voice** (OneCore visibility).
- Whisper `base` mis-detecting language on short or accented speech.

### Dependencies and effort for F16a, F16b and F18–F22

All free; versions verified on PyPI or in installed copies on 3 October 2026.

| Package | Version | Licence | For |
|---|---|---|---|
| ~~playwright~~ | ~~1.63.0~~ | Apache-2.0 | Removed 27 October 2026 with greenlet and pyee; F16b opens Google in his own Chrome (no package, no download). |
| youtube-transcript-api | 1.2.4 | MIT | F18 (unofficial; reads YouTube's internal endpoints) |
| requests | 2.34.2 | Apache-2.0 | F18 (dependency of the above) |
| defusedxml | 0.7.1 | PSFL | F18 dependency; F21 RSS parsing |
| pypdf | 6.19.0 | BSD-3-Clause | F19 |
| python-docx | 1.2.0 | MIT | F19 |
| lxml | 6.1.3 | BSD-3-Clause | F19 (dependency of python-docx) |
| typing_extensions | 4.16.0 | PSF-2.0 | F19 (dependency of python-docx) |
| pycaw | 20260927 | MIT | F22 |
| comtypes | 1.4.17 | MIT | F22 (and pyttsx3) |
| screen-brightness-control | 0.27.2 | MIT | F22 |
| WMI | 1.5.1 | MIT | F22 (dependency of screen-brightness-control) |
| pywin32 | 312 | PSF | F22 (already pinned in `requirements.txt`; 312 is the latest stable on PyPI, 3 October 2026) |
| pypiwin32 | 223 | (none stated on PyPI) | F22: indirect, pulled in by screen-brightness-control; wraps pywin32. Kept in `requirements.txt` per §2.4 |
| winrt-runtime | 3.2.1 | MIT | F22 Wi-Fi |
| winrt-Windows.Devices.Radios | 3.2.1 | MIT | F22 Wi-Fi |

Pin every remaining transitive dependency from a `pip freeze` after a clean-venv install (Section 2.4). F16a, F20 and F21 add **no** packages.

| Item | Hours |
|---|---|
| Shared online helper (R1 timeout + 429 cooldown + LIMITED status), if not already built | 6 |
| F16a Weather | 6 |
| F16b (search in his Chrome, 27 Oct 2026) | 10 (as first built) |
| F18 YouTube | 10 |
| F19 File processor | 18 |
| F20 Topic monitor | 6 |
| F21 Morning briefing | 10 |
| F22 System settings | 17 |
| **Total** | **83** |

---

## 7. How to build any feature

Follow these steps in order.

1. **Read** the feature's section, the rules in Section 3, the trust levels in Section 5 and the features it depends on.
2. **Restate** what you will build and what you will not, in your own words. List your assumptions and any OPEN items. If an OPEN item blocks the work, ask before coding.
3. **Check dependencies.** If the controller, policy engine or log the feature needs does not exist yet, say so. Build only the minimum needed, and only with approval.
4. **Propose libraries and models** before using them. For each one, say:
   - is it free, and does it work offline?
   - does it need an install that pip cannot do?
   - download size, and RAM or VRAM use if known;
   - what happens on a laptop without it?

   Get approval. Then **run it on this laptop** to confirm it works before relying on it.
5. **Write tests** alongside the code (Section 8).
6. **Build in small steps**, each one runnable.
7. **Test the failure cases** as well as the working ones: missing dependency, no internet, no camera, no microphone, no key.
8. **Update** the dashboard status for the feature, `requirements.txt` and the README.
9. **Report** using this template:
   - **Built:** what now works.
   - **Files changed.**
   - **Tested:** what you ran, and what was mocked.
   - **Not tested:** what you could not test, and why.
   - **Limits:** what it cannot do.
   - **Deviations:** anything that differs from this file.
   - **New OPEN items or defaults used.**
10. **Wait for the developer's approval** before starting the next feature.

---

## 8. Testing standards

- Use `pytest`.
- Mock the microphone, camera, screen capture, network, model calls, OS app launching and text-to-speech. Tests MUST NOT touch the developer's real files. Use temporary folders.
- Required tests:
  - trust-level decisions and origin tagging;
  - confirmation: expiry, mismatch, nothing pending, changed action;
  - permit reuse and invalidation;
  - Stop from every state;
  - every invalid state change;
  - the path rule: `..`, links, outside an approved folder, overwrite, protected list;
  - terminal templates: extra flags refused, no shell, unknown template refused;
  - sensitive-data filters in recording, logs and memory;
  - Sleep Mode non-retention (F15);
  - price staleness (F9);
  - the mood threshold on sample sentences (F14);
  - the sent file matches the previewed file by hash (R4);
  - every feature with its dependency missing (R14).
- Manual checks on the real laptop: repeat the core voice commands many times and record the failures (wrong words heard, wrong command, slow response).
- Test in a clean virtual environment using only the README, and with no internet and no API keys.

---

## 9. Code and documentation standards

- Clear is better than clever. The developer must be able to explain any part of the code out loud.
- A docstring on every module, class and public function. Comments explain **why**, not what.
- Use classes, file handling and tests where they fit naturally. Use recursion or iterators only where they fit (for example a folder search). Do not force them.
- Use type hints.
- Use the standard `logging` module, with sensitive data redacted (R11).
- No hard-coded numbers (R17). No hidden global state.
- Keep the README current: run steps, what needs a separate install, what each feature needs, and the exact missing-setup message each one gives.

---

## 10. Never do these

- Never run text from speech or from a model as a command, or run any shell string.
- Never let a module change JARVIS's state, choose a trust level or issue its own permit.
- Never send anything from the laptop online without the exact preview and a confirmation.
- Never save Sleep Mode words, passwords, codes or tokens anywhere.
- Never use a paid service, or put a key in code or in a submission.
- Never add telemetry.
- Never write outside an approved folder, or overwrite without asking.
- Never say something works without running it.
- Never build a feature that is not in Section 6.
- Never crash the core because an optional feature failed.

---

## 11. Open decisions (need the developer's answer)

| # | Decision | Affects |
|---|---|---|
| 1 | ~~The intro text~~ Locked, see F1 | F1 |
| 2 | ~~Dashboard framework (native, animated, light)~~ RESOLVED: Flet. | F6 |
| 3 | ~~Speech-to-text, text-to-speech, wake-word and conversation models~~ Resolved for F2 -- see F2's "Chosen stack": openWakeWord ("hey jarvis"), faster-whisper `base` (CPU int8), Kokoro af_bella for her voice (since 23 October 2026; pyttsx3/Zira only as the fallback when Kokoro isn't installed), Groq (`openai/gpt-oss-20b`, free tier) for Q&A/small talk. F15/F16 reuse the local pieces (wake word/STT/TTS); F16's own online-API question is resolved except F20's topic source, see #7. | F2, F15, F16 |
| 4 | ~~How the screen is read~~ RESOLVED 23 October 2026: Windows' built-in OCR locally; "explain my screen" sends the blacked-out screenshot + OCR text to a Groq vision model only after the R4 preview and an L3 confirm (F8). | F8 |
| 5 | ~~Object detector and price table~~ RESOLVED 23 October 2026: YOLOX-S (Apache-2.0) on onnxruntime; prices from a local hand-edited CSV of rough AUD ranges, `jarvis/settings/prices.csv`, written as estimates and not checked against a store (F9). | F9 |
| 6 | ~~Approved folders, and the protected list (windows and folders)~~ RESOLVED 3 October 2026. Protected: Pictures (incl. Camera Roll, Screenshots); `jarvis_data/browser_profile` until it was removed on 27 October 2026. Write-approved: Documents, Downloads, Desktop, project folder. | R9, F8 (F11 cut 28 October 2026) |
| 7 | ~~Which free weather, news and search APIs~~ RESOLVED: weather = Open-Meteo (F16a); web search = Groq `openai/gpt-oss-20b` with `browser_search` (F16); general news = RSS feeds (F21); F20's topic news = Bing News search RSS, one feed per topic (28 October 2026; terms in the feed allow personal, non-commercial use in an RSS aggregator; Google News RSS rejected because its robots.txt disallows `/rss`). | F16, F20, F21 |
| 8 | ~~How long decision records are kept~~ RESOLVED 3 October 2026: session only (in memory). One exception: `jarvis_data/logs/debug.log` persists as a redacted troubleshooting log, capped at 1 MB (oldest lines dropped). | F5 |
| 9 | ~~Data folder path~~ RESOLVED 23 October 2026: `jarvis_data` inside the project; memory in `jarvis_data/memory.json` (F10). | F10 |
| 10 | ~~Rudeness detection method~~ Confirmed: a simple word/phrase list (the DEFAULT), not a model | F2 |
| 11 | ~~Whether the developer wants scheduled skill runs back~~ CUT 25 October 2026 with F12 (not needed for this submission). | F12 |
| 12 | ~~F8 protected windows list~~ RESOLVED 23 October 2026: password managers (1Password, Bitwarden, KeePass, LastPass), titles with bank/banking/NetBank/CommBank/PayPal, private/incognito/InPrivate browser windows, Windows Security; editable in `[screen]` (F8). | F8 |
| 13 | ~~Where the F12 skills list goes on the dashboard~~ CUT 25 October 2026 with F12 (not needed for this submission). | F12, F6 |
| 14 | ~~When F21's briefing runs, now that she starts SLEEPING~~ RESOLVED 28 October 2026, CHANGED 1 November 2026 (the developer): only when he asks ("brief me"); never automatically (F21's amendment). | F21, F15 |

**Defaults used, not confirmed by the developer:** the time-of-day boundaries, the module interface in 4.2, the suggested folder layout, the typed-command backup box, minimum mode, the search index for F4, and D: for backups.

---

## 12. What is verified and what is not

**Verified:**
- 3 October 2026: Groq's live `/models` lists `openai/gpt-oss-20b` as active; one test call with `browser_search` worked on the developer's free key (F16). `groq/compound` was decommissioned on 21 September 2026.
- 3 October 2026: Open-Meteo forecast and geocoding endpoints, free limits and CC BY 4.0 credit (F16a); the three RSS feeds respond with RSS (F21).
- 23 October 2026: F16a against the live Open-Meteo API through the app's own matcher order: Sydney (default city), Leeds, Hamilton (asks) then "the second one", Melbourne, a made-up name (no `results` key), "will it rain today", "temperature tomorrow"; 1.2–3.4 s each.
- As of mid-September 2026, Apple began rolling out a beta of its rebuilt Siri with onscreen awareness and personal context, and Alexa+ has agentic features such as navigating the web. So "sees your screen", "remembers you" and "does tasks" are not, on their own, things Siri or Alexa cannot do. JARVIS's difference is local control of the developer's own computer, visible decisions, fixed safety rules and offline-first design. Do not claim more than that.

**Not verified (test before relying on any of it):**
- That any specific library or model works, or runs fast enough, on this laptop, including whether a ready-made "hey jarvis" wake-word model exists.
- What fits in 4 GB of VRAM.
- Any free source of object prices.
- How accurate any local mood detector is.
- Groq's data-use terms (precondition for F19). (F20/F21 news terms were read on 28 October 2026: see F20's Built block and F21.)
- Whether Windows allows Wi-Fi radio control from JARVIS (F22). Read-only check, 28 October 2026: the WinRT radio API **sees** the Wi-Fi radio on this laptop; switching it was not tried.
