RUN: double-click setup.bat once, then run.bat (or: python main.py after setup)
     macOS: bash setup.sh once, then bash run.sh

Runs on Windows 10/11 (full features) and macOS (most features).

JARVIS - a local-first voice assistant for Windows and macOS
============================================================

FULL PROJECT (download & run): https://github.com/priyanshkhandelwall/jarvis-voice-assistant

JARVIS is a voice assistant that explains every decision it makes, asks
before anything risky, and shows exactly what would go online before it
does. Say "Hey Jarvis" and talk to her.


REQUIREMENTS
------------
- Windows 10 or 11 (64-bit), or macOS
- Python 3.11, 64-bit: https://www.python.org/downloads/release/python-3117/
  (tick "Add python.exe to PATH" when installing)
- A microphone (a webcam is optional, for the camera features)
- Internet for the one-time setup and for online features
- About 1.5 GB of free disk space (the installed packages take about 730 MB and the models about 520 MB)


HOW TO RUN
----------
1. Download the full project from the GitHub link above (Code -> Download ZIP)
   and unzip it.
2. Double-click setup.bat. The first run can take up to about 20 minutes: it
   installs the packages (about 730 MB) and downloads the models (about 520 MB).
3. Optional: get a free Groq API key at https://console.groq.com/keys and put
   it after GROQ_API_KEY= in the .env file that setup created. JARVIS works
   without it; online questions then say how to set it up.
4. Double-click run.bat, then say "Hey Jarvis".
macOS: in Terminal, run bash setup.sh once, then bash run.sh. Allow the
microphone, camera and screen recording when macOS asks.

Keys: Ctrl+Shift+X (or Esc in the window) = Stop.
      Ctrl+Shift+Space = push-to-talk (one command, no wake word).
You can also type commands into the box at the bottom of the window.


DIFFERENT ON macOS
------------------
- No global hotkeys: stop with the STOP button, Esc or by saying "stop".
- No brightness control ("Changing brightness isn't supported on macOS.").
- No screen-text reading (Windows OCR); "explain my screen" works.
- No private-window blackout in screen previews (no window list).
- macOS support was tested by simulation on Windows, not on a real Mac.


ED DECLARATIONS
---------------
- This program uses libraries outside Python's built-ins (see
  requirements.txt; setup.bat installs them).
- It is a GUI application (a Flet dashboard window).
- It does audio processing (microphone input, wake word detection,
  speech-to-text and text-to-speech) and video processing (webcam object
  detection, screen capture and OCR).
- It uses web/network features: the Groq API (online answers), Open-Meteo
  (weather), RSS news feeds, and YouTube transcripts.
- Because of these, it will not run inside Ed. Please run it locally on
  Windows or macOS, using the steps above.


FEATURES
--------
- Open apps and websites ("open notepad", "open YouTube")
- Find files and open the one you pick ("find my report", "open number one")
- Explain itself ("why did you do that?")
- Time and date (offline), questions and live questions (online, Groq)
- Weather ("what's the weather in Sydney?")
- Long-term memory ("remember my exam is on Friday", "what do you remember?")
- Screen reading ("what's on my screen?") and webcam object detection
  ("what can you see?", "how much is this?")
- Document and YouTube summaries ("summarize my report", "summarize this video")
- News topics and a morning briefing ("track solar power", "brief me")
- System settings ("volume up", "set the brightness to 70", "lock the laptop")
- Google search in Chrome ("search best pizza in Sydney")
- Conversation and sleep mode ("go to sleep", then "Hey Jarvis")
- Trust levels L0-L4: risky actions wait for "confirm"; the most serious need
  two confirms
- Privacy previews: nothing from the laptop goes online without a preview
  and "confirm"
- Live Activity dashboard showing every step as it happens


COMP9001 ADVANCED CONCEPTS
--------------------------
Topic                     Where (file:line)                         Why it matters
Classes & Objects         jarvis/speech/output.py:16 (abstract       Kokoro, the Windows voice and silent
                          SpeechOutput), jarvis/speech/              test fakes are interchangeable, so a
                          neural_tts.py:274 (subclass),              missing voice degrades, never crashes.
                          jarvis/controller/permits.py:29 (frozen    A frozen Permit can't change after it
                          dataclass), jarvis/controller/             is issued.
                          controller.py:49 (Controller)
Collections               jarvis/logbook/logbook.py:41 (deque),      Bounded deques keep the newest records
                          jarvis/voice/listener.py:206 (deque),      at a fixed memory cost; trust levels
                          jarvis/controller/trust.py:102 (dict),     are a fixed dict lookup; Counter
                          jarvis/vision/commands.py:192 (Counter)    tallies detected objects.
File I/O                  jarvis/memory/store.py:206 (JSON, atomic   A .tmp file + os.replace() means a
                          write), jarvis/vision/prices.py:54 (CSV),  crash never leaves a half-written
                          jarvis/settings/__init__.py:27 (TOML)      memory file.
Flow control &            jarvis/online/service.py:106 (retry       Every online failure becomes a plain
error handling            loop), jarvis/dashboard/app.py:2035        spoken message, never a traceback;
                          (shutdown steps), jarvis/memory/           each shutdown step is guarded.
                          store.py:79 (custom exception)
Testing                   tests/conftest.py:64 (autouse fixture),    2,060 offline tests; the fixture stops
                          tests/ (pytest)                            any test opening a real app.
Iterators & generators    jarvis/actions/file_index.py:150           Files are yielded one at a time while
                          (walk_root), jarvis/sensors.py:35          walking the disk; the context manager
                          (@contextmanager)                          always turns the sensor off.
Recursion                 jarvis/logbook/records.py:121              Redacts secrets in nested data of any
                          (redact_value), jarvis/system/             depth; reads "two hundred and five".
                          commands.py:88 (to_number)

The Ed upload leaves out the tests/ folder to stay under Ed's 2 MB limit.
The tests (tests/ folder) are in the full project on GitHub. Run them with:
  venv\Scripts\python.exe -m pytest tests -q


CREDITS AND LICENSE
-------------------
All JARVIS code was written for this project. The name is a tribute to
JARVIS from Iron Man. Third-party libraries, models and services (Whisper,
Kokoro, openWakeWord, YOLOX, Flet, Groq, Open-Meteo and others) are used
under their own licences; see README.md in the full project.
License: MIT (see LICENSE). Copyright (c) 2026 Priyansh Khandelwal.
