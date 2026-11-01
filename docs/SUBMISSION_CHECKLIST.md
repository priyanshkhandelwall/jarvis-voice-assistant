# Submission checklist

Due **1 November 2026, 11:59 pm**. Created 23 October 2026. Tick each item off as it is done.

## BEFORE SUBMISSION: settings to change back

- [ ] **Set `debug_scores = false`** in `jarvis/settings/settings.toml` (line 422, `[wake_word]`).
  - It was turned ON on 16 October so the developer could send a real `jarvis_data/logs/wake_scores.log` from the earphone mic to tune the wake threshold.
  - Leave it on until that log has been sent and the threshold has been tuned. Then turn it off.
- [ ] `[wake_word] threshold` (line 413) is still 0.5. Tune it from the real score log first.

## README.md: out-of-date lines (to fix in the README rewrite)

Line numbers are as of 23 October 2026. The README has not been rewritten yet, as asked.

| Line(s) | What it says | What is true now |
|---|---|---|
| 7–14 | Status block: lists F17, F5, F7, F6, F1, F3, F4, F2/F15 only; "laptop tests for conversation mode … have not been run yet" | Also built: F13, F14 (mood check-in), F16 Stage 1 (live questions), F16a (weather), the R15 animation pause and the low-memory warning. Laptop tests: partly done (11 and 16 Oct); see the combined checklist in `docs/reports/2026-10-23-partials-complete.md` |
| 316 and 429 | Two sections both called "## Running JARVIS" | Merge them into one |
| 322–326 | "Nothing meaningful executes yet (F1/F3/F4 aren't built)", and it points to the "Throwaway demo command" | F1–F5 and the rest are built. Remove this |
| 346–356 | "## Throwaway demo command -- removed": build history | Move to a development log, not the tutor's README |
| 380 | L2 executors "aren't built yet, F11 is out of scope" | Done 28 Oct: now says F11 was cut |
| 384 | L3 targets "belong to F8/F9/F16, none built" | F16 Stage 1 and F16a are built. Neither sends laptop data, so still no L3 action. Reword |
| 538 | "every sensor row (mic/camera/screen/keyboard-mouse) is F2/F8/F9/F11, none built yet, so all show OFF" | The microphone row is real (F2). Camera and screen are live rows (F8, F9). The keyboard row was removed with F11 (28 Oct) |
| 559–564 | Modules panel "Seeded 2026-09-22: built = F1, F3, F4, F5, F7; in progress = F2 …, F6 …; pending = F8-F16" | See `jarvis/dashboard/module_status.py` (updated 23 Oct): F8–F12 and F16b pending, the rest built |
| 591–628 | Project layout | Missing: `jarvis/actions/weather.py`, `clock.py`, `command_text.py`, `sleep_wake.py`, `wake_greeting.py`, `web_query.py`, `explain.py`, `greeting.py`; `jarvis/online/` (`service.py`, `status.py`, `budget.py`, `http.py`); `jarvis/speech/mood.py`, `jarvis/speech/claims.py`; `jarvis/dashboard/visibility.py`, `internet_status.py`, `state_display.py`; `tools/capture_window.py` |
| 619 | `module_status.py` "hand-maintained F1-F16 status dict" | F1–F17 plus F16b |
| 442–476 | "What needs installs …" | Still right (weather and mood add no package). Add one line: weather needs internet only, no key |
| (missing) | No section on weather, mood, live questions' daily token budget, or the Open-Meteo credit line | Add them |

## Files and folders

- [ ] **USER_MANUAL.md is missing.** It should cover:
  - every command;
  - what each dashboard panel means;
  - every setup message and what to do about it;
  - what is not built.
- [ ] README rewritten for the tutor. Move the build history to `docs/DEVELOPMENT_LOG.md`.
- [ ] Clean-venv run from the README alone, with no internet and no `GROQ_API_KEY` (spec §8). Then one run with internet.
- [ ] The project is not a git repository, so `.gitignore` does not apply on its own. Leave these out of the submission by hand:
  - `venv/`
  - `models/` (now also `models/ms-playwright`, F16b's Chromium, 740 MB, and `models/yolox/yolox_s.onnx`, F9's detector, 36 MB)
  - `jarvis_data/` (personal data, R18). This includes `jarvis_data/memory.json` and `memory.json.bak` (F10, what the developer told her to remember, added 23 Oct) and any `memory.corrupt-*.json`; and `jarvis_data/browser_profile/` (F16b's own browser profile: cookies of any site the developer logged into there). This includes `jarvis_data/voice_cache/` (11.3 MB of pre-rendered voice lines, added 23 Oct), which rebuilds itself in the background on first start.
  - `__pycache__/` folders
  - `.pytest_cache/`
  - `out_before.txt` (0 bytes)
  - `JARVIS_BUILD_REFERENCE.backup-2026-10-03.md`
  - `JARVIS_SPEC_ADDITIONS_DRAFT.md`
  - `docs/backups/` (2.1 MB of old code copies)
  - `docs/spec-backups/`, unless the tutor should see them
- [x] Voice choice (done 23 Oct): **Kokoro af_bella, full-precision model** (int8 was chosen first, but measured ~3x slower on this laptop's CPU, so the developer switched to full precision). Kokoro's pins are in `requirements.txt`; the default `download_models.py` run fetches its model and voices file. `requirements-voices.txt` now holds only the optional Piper engine. The F2 and F14 voice lines in the spec are final (no PENDING left). Report: `docs/reports/2026-10-23-kokoro-voice.md`.
- [x] Benchmark-only models deleted (23 Oct, 1,193 MiB freed): `faster-whisper-base`, `faster-whisper-small.en`, `faster-whisper-distil-small.en`, `piper/` (3 voices), `kokoro/kokoro-v1.0.int8.onnx` and their Hugging Face cache refs. `models/` now holds only what JARVIS uses (487 MiB): `openwakeword/`, `faster-whisper-base.en/`, `kokoro/kokoro-v1.0.onnx` and `kokoro/voices-v1.0.bin`. `models/` is never submitted anyway. (`tools/whisper_benchmark.py` re-downloads its models if it is ever run again; `tools/tts_benchmark.py` would need `--voices piper` and the int8 file.)
- [ ] No `.env` exists. The key is a Windows environment variable, so nothing secret is in the folder (R16). Check again just before zipping.
- [ ] `requirements.txt`: 80 pins after F9 (F8 added pillow and 7 winrt packages, F9 opencv-python-headless). 71 pins after F16b (playwright, greenlet, pyee added 23 Oct). Previously: 68 pins (Kokoro's 5 added on 23 Oct). A fresh-venv `pip install --dry-run --ignore-installed -r requirements.txt` resolved cleanly on 23 Oct, with exactly the 68 pinned packages and nothing else (`docs/reports/2026-10-23-kokoro-requirements-dryrun.txt`). Re-run it the day before.
- [ ] Voice laptop checks still to do by the developer (they need a person, not a script):
  - Press Ctrl+Shift+X while she is speaking. The scripted test made the same calls as the hotkey: sound stopped in 16-47 ms.
  - ~~Echo guard with the laptop speakers~~ Done 23 Oct: through the laptop speakers, with the guard off she woke herself (1 wake), and with it on there were 0 wakes.
  - Listen to "on this screen" in the intro: both Whisper models heard "the screen" (Kokoro gets the right phonemes).
- [x] Groq short opener (approved and built 23 Oct): "Why is the sky blue?" went from 4.5 s to 1.5 s to first audio. Only one call was left in the 6-call budget to check the final wording, so check a few more answers in normal use.
- [x] Spec: F23 and open question #3 described Zira as her voice. Done 25 Oct: F23 is cut, and #3 now names Kokoro af_bella with Zira only as the fallback.

## Cut features (25 and 28 October 2026)

The developer cut these: "not needed for this submission". Mention them as cut, not missing, in the README and to the tutor.

- **F12 Skills and Workflow Recording.** Spec marked CUT, §11 #11 and #13 closed as cut, Modules panel row removed, its Groq capability line removed.
- **F11 Computer Control** (cut 28 October 2026: "not needed for this submission; it would require the banned input-injection APIs"). Spec marked CUT (original text kept below the banner), Modules panel row removed, its Groq capability line removed, the dashboard's Keyboard / Mouse row removed. The banned-API static test is unchanged.
- **F23 Multilingual replies.** Spec marked CUT, removed from the stretch range (now F18–F22) and the effort table, and the F2/F14 voice lines no longer have a language exception. There were no F23 settings or packages to remove.

## Terms to recheck (spec)

- [ ] Open-Meteo terms, <https://open-meteo.com/en/terms>: non-commercial use, CC BY 4.0, limits. The spec says to recheck before submission.
- [ ] Groq data-use terms. These have still not been checked by the developer (spec, Groq budget section). After reading them, set `[online] terms_checked = true` (F19's file preview) and `[screen] online_terms_checked = true` (F8's screen preview).
- [ ] News feeds (F20/F21), read by the agent on 28 Oct 2026; please confirm.
  - **Bing News search RSS:** personal, non-commercial use "within an RSS aggregator".
  - **BBC:** terms §15, credit "BBC News".
  - **NPR:** "personal site or application".
  - **The Guardian** is left out: its terms forbid automated reading.
- [ ] YouTube transcripts (F18): `youtube-transcript-api` is unofficial, and how it fits YouTube's Terms of Service is unverified (spec F18).
- [ ] F22 Wi-Fi: whether Windows lets JARVIS switch the radio is untested; only reading it was checked.

## Last steps

- [ ] Full `pytest`: all passing.
- [ ] Safety scan: zero banned input or focus APIs in `jarvis/`, `tests/`, `tools/`, `scripts/` and `main.py`.
