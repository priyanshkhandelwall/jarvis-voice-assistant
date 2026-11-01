# JARVIS user manual

This guide covers installing JARVIS, talking to her, and what each feature does. For how the code fits together, see [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Install

1. Install [Python 3.11](https://www.python.org/downloads/release/python-3117/) (64-bit). Tick "Add python.exe to PATH".
2. Download the project (GitHub: **Code → Download ZIP**) and unzip it.
3. Double-click `setup.bat`. It:
   1. finds Python 3.11,
   2. creates a virtual environment in `venv\`,
   3. installs the pinned packages from `requirements.txt`,
   4. downloads the models into `models\` (wake word, Whisper speech-to-text, Kokoro voice, YOLOX object detector),
   5. creates `.env` from `.env.example`.

   The first run needs internet and can take up to about 20 minutes (packages about 730 MB installed, models about 520 MB). Running it again keeps what is already there.
4. Optional: paste a free Groq API key (from [console.groq.com](https://console.groq.com/keys)) after `GROQ_API_KEY=` in `.env`. Without it, everything local still works, and online questions tell you how to set it up.

**macOS:** install Python 3.11 (universal2), then in Terminal in the JARVIS folder run `bash setup.sh` (same five steps) and start with `bash run.sh`. Allow the microphone, camera and screen recording when macOS asks (System Settings → Privacy & Security). On macOS there are no global hotkeys, no brightness control and no screen-text reading ("explain my screen" still works); see README.md, "What is different on macOS".

## 2. Start and stop

- Start: double-click `run.bat` (or run `python main.py` inside the virtual environment). The dashboard opens and she greets you.
- Close: close the window. Everything shuts down cleanly.
- Stop the current action: `Ctrl+Shift+X` anywhere (Windows), `Esc` in the window, the STOP button, or say "stop".

## 3. Talking to JARVIS

- **Wake word**: say "Hey Jarvis". She stays in conversation mode, so you can keep giving commands without repeating it.
- **Push-to-talk**: hold `Ctrl+Shift+Space` and speak one command, no wake word needed.
- **Typing**: type a command in the box at the bottom of the dashboard and press Enter.
- **Sleep**: say "go to sleep". She then listens only for "Hey Jarvis".

## 4. Features

### Introduction
"Introduce yourself", "What does JARVIS stand for?"

### Open apps and websites
"Open notepad", "Open Chrome", "Open YouTube". When several apps match, she reads a short numbered list; say "open number two" or the app's name.

### Find files
"Find my report". She lists matching file names (never full paths) and you pick one: "open number one". Her own project folders are never searched.

### Why did you do that?
"Why did you do that?" She explains the last decision from her own record: what she heard, how she understood it and which rule applied.

### Time, date and questions
"What time is it?" and "What's the date?" are answered offline. General and live questions ("What is photosynthesis?") go to Groq. The Internet panel shows ONLINE while a question is being answered.

### Weather
"What's the weather in Sydney?", "Will it rain tomorrow?" (Open-Meteo, no key needed). If a city name matches several places, she asks which one.

### Memory
"Remember my exam is on Friday", "What do you remember?", "Forget my exam", "Forget everything" (two confirmations). Passwords, PINs and card or account numbers are refused.

### Screen
"What's on my screen?" reads the screen locally (Windows OCR). "Explain my screen" sends a smaller picture with private windows blacked out, but only after showing you exactly what will be sent and hearing "confirm".

### Camera
"What can you see?", "How much is this?" (a rough price range from a local table), "Turn on the camera" (live preview in the Webcam Feed panel). Detection is local; she names objects, never people or brands.

### Documents
"Summarize my report" reads a PDF, Word, text or CSV file locally. The summary goes online only after a preview and "confirm".

### YouTube
"Play lofi beats on YouTube" opens the search in Chrome. "Summarize this video" summarises the open video's transcript.

### Web search
"Search best pizza in Sydney" opens a Google search in your own Chrome.

### News topics and morning briefing
"Track solar power", "What am I tracking?", "Stop tracking solar power". "Brief me" gives the weather and the latest headlines for your topics.

### System settings
"Volume up", "Mute", "Set the brightness to 70", "Turn Wi-Fi off", "Lock the laptop", "Restart", "Shut down". Volume and brightness change at once; locking and Wi-Fi need "confirm"; restart and shut down need two confirmations.

### Personality
She varies her greetings by time of day, checks in if you sound frustrated, and stays calm if you are rude (she still does what you asked).

## 5. Trust levels and confirmations

| Level | Examples | What happens |
|---|---|---|
| L0 | answers, "remember" | runs at once |
| L1 | open an app, a file, a website; look at the screen or camera once; volume, brightness; forget one memory | runs at once and is verified |
| L2 / L3 | lock the laptop, forget several memories; send your screen or a file online, turn Wi-Fi on or off, keep the camera on | preview on the dashboard; say "confirm" or "cancel" |
| L4 | restart, shut down, forget everything | two confirmations |

A confirmation expires on its own if you say nothing.

## 6. The dashboard

- **Centre**: her state (sleeping, awake, listening, ...) and the **Live Activity** log of every step. "Copy log" copies it.
- **Left**: sensors (microphone, camera, screen capture), system vitals and the modules list.
- **Right**: pending confirmations, the Internet panel (LOCAL / ONLINE), the webcam feed and quick commands.

## 7. Privacy

- Your data is kept in `jarvis_data\` on your laptop and never uploaded.
- Nothing from your laptop goes online without a preview and your "confirm".
- Secrets are refused by memory and redacted from every log.
- The camera and screen are used only when you ask.

## 8. Troubleshooting

| Problem | What to do |
|---|---|
| "Python 3.11 was not found" | Install Python 3.11 (64-bit) with "Add to PATH" ticked, then run `setup.bat` again. |
| Setup stops while downloading | Check the internet connection and run `setup.bat` again; finished downloads are kept. |
| Online questions say they aren't set up | Add `GROQ_API_KEY` to `.env`, then ask again. |
| She doesn't hear you | Click the Microphone row on the dashboard to pick another microphone, and check Windows' microphone privacy setting. |
| No camera | The camera features say what to set up; everything else keeps working. |

## 9. Running the tests

```
venv\Scripts\python.exe -m pytest tests -q
```

The tests run offline and never open apps, change settings or touch your data.
