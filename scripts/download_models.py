"""Download the models F2's voice input and her voice need (Section 2.4:
one script, run once before first use; needs internet that one time only).

    venv\\Scripts\\python.exe scripts\\download_models.py

Downloads into the project's own models/ folder (settings.toml [models]):
- openWakeWord's pre-trained "hey jarvis" model, plus the melspectrogram
  and embedding feature models it runs on (and openWakeWord's own VAD
  model, which its downloader always fetches). License: CC BY-NC-SA 4.0
  (non-commercial; fine for this personal project).
- faster-whisper "base.en" ([models] whisper_size; Systran on Hugging
  Face), CTranslate2 format. License: MIT.
- Her voice, Kokoro: kokoro-onnx's Kokoro-82M
  (Apache-2.0) from the kokoro-onnx GitHub release "model-files-v1.0": the
  model named in [speech.kokoro] model_file (full precision, 310 MB: about
  3x faster than int8 on this laptop's CPU) and the voices file (27 MB,
  holds af_bella).

Optional, only with --voices (needs requirements-voices.txt):
- piper: the Piper voices in [speech.piper] download_voices, from
  rhasspy/piper-voices on Hugging Face (about 60 MB each): en_GB alba
  (CC BY 4.0), en_US amy (MycroftAI mimic3 dataset), en_US hfc_female
  (CC BY-NC-SA 4.0, non-commercial).

    venv\\Scripts\\python.exe scripts\\download_models.py --voices piper

Safe to run again: files already on disk are kept. Hugging Face telemetry
is switched off (R5), and its cache is kept inside models/, not in the
user's home folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Before huggingface_hub is imported: no telemetry (R5), and no cache
# outside the project folder.
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ.setdefault("HF_HOME", str(PROJECT_ROOT / "models" / ".hf_home"))

from jarvis.settings import SETTINGS  # noqa: E402
from jarvis.voice import model_files  # noqa: E402


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def download_wake_word() -> bool:
    """Download openWakeWord's 'hey jarvis' model and its feature models. True on success."""
    try:
        from openwakeword import utils as oww_utils
    except ImportError:
        print("openwakeword isn't installed. Run pip install -r requirements.txt in the JARVIS folder, then run this script again.")
        return False
    target = model_files.wake_word_dir()
    target.mkdir(parents=True, exist_ok=True)
    name = Path(str(SETTINGS["models"]["wake_word_model"])).stem  # e.g. hey_jarvis_v0.1
    print(f"Wake word: downloading '{name}' and its feature models into {target} ...")
    oww_utils.download_models(model_names=[name], target_directory=str(target))
    return model_files.wake_word_ready()


def download_whisper() -> bool:
    """Download faster-whisper base.en into models/. True on success."""
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        print("faster-whisper isn't installed. Run pip install -r requirements.txt in the JARVIS folder, then run this script again.")
        return False
    target = model_files.whisper_dir()
    size = str(SETTINGS["models"]["whisper_size"])
    print(f"Speech-to-text: downloading faster-whisper '{size}' into {target} ...")
    download_model(size, output_dir=str(target))
    return model_files.stt_ready()


_KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
_PIPER_VOICES = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/"


def _fetch(url: str, target: Path) -> bool:
    """Download one file unless it is already there (free, no key)."""
    import urllib.request

    if target.exists() and target.stat().st_size > 0:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print(f"  downloading {url.rsplit('/', 1)[-1]} ...", flush=True)
    # Urllib's default trust store failed on
    # some machines for GitHub's release downloads ("CERTIFICATE_VERIFY_FAILED: unable to get
    # issuer certificate"). certifi's CA bundle (pinned; installed with
    # requests) is used when present.
    context = None
    try:
        import ssl

        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    try:
        with urllib.request.urlopen(url, timeout=60, context=context) as response, open(partial, "wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        partial.replace(target)
        return True
    except OSError as exc:
        print(f"  could not download {url}: {exc}")
        partial.unlink(missing_ok=True)
        return False


def kokoro_dir() -> Path:
    """The folder for Kokoro's model files."""
    return PROJECT_ROOT / str(SETTINGS["speech"]["kokoro"]["model_dir"])


def download_kokoro() -> bool:
    """Download Kokoro's model and voices file. True on success."""
    folder = kokoro_dir()
    print(f"Voice (Kokoro): into {folder} (files already there are kept) ...")
    files = [str(SETTINGS["speech"]["kokoro"]["model_file"]), str(SETTINGS["speech"]["kokoro"]["voices_file"])]
    return all([_fetch(_KOKORO_RELEASE + name, folder / name) for name in files])


def download_piper() -> bool:
    """Download the optional Piper voices (--voices piper). True on success."""
    folder = PROJECT_ROOT / str(SETTINGS["speech"]["piper"]["model_dir"])
    print(f"Piper voices: into {folder} ...")
    ok = True
    for voice in SETTINGS["speech"]["piper"]["download_voices"]:
        lang, name, quality = str(voice).split("-")  # en_GB-alba-medium
        base = f"{_PIPER_VOICES}{lang}/{name}/{quality}/{voice}"
        ok &= _fetch(base + ".onnx", folder / f"{voice}.onnx")
        ok &= _fetch(base + ".onnx.json", folder / f"{voice}.onnx.json")
    return ok


def detector_dir() -> Path:
    """The folder for the YOLOX detector model."""
    return PROJECT_ROOT / str(SETTINGS["camera"]["detector_dir"])


def download_detector() -> bool:
    """F9: YOLOX-S (Megvii, Apache-2.0), 36 MB, from YOLOX's GitHub release."""
    cfg = SETTINGS["camera"]
    print(f"Object detector (YOLOX-S): into {detector_dir()} ...")
    return _fetch(str(cfg["detector_url"]), detector_dir() / str(cfg["detector_file"]))


def main() -> int:
    """Download every model that is missing. Returns 0 if all succeeded, 1 otherwise."""
    if "--voices" in sys.argv:
        wanted = sys.argv[sys.argv.index("--voices") + 1 :]
        ok = True
        if "kokoro" in wanted:
            ok &= download_kokoro()
        if "piper" in wanted:
            ok &= download_piper()
        print("Voices: ready." if ok else "Voices: NOT all downloaded. Check your internet connection, then run this again.")
        return 0 if ok else 1
    ok_wake = download_wake_word()
    ok_stt = download_whisper()
    ok_voice = download_kokoro()
    ok_detector = download_detector()
    print()
    for label, ok, path in (
        ("Wake-word model", ok_wake, model_files.wake_word_dir()),
        ("Speech-to-text model", ok_stt, model_files.whisper_dir()),
        ("Voice model (Kokoro)", ok_voice, kokoro_dir()),
        ("Object detector (YOLOX-S)", ok_detector, detector_dir()),
    ):
        if ok:
            print(f"{label}: ready, {_mb(_size(path))} in {path}")
            files = sorted(p for p in path.rglob("*") if p.is_file() and ".cache" not in p.parts)
            for f in files if len(files) <= 20 else []:
                print(f"    {f.relative_to(path)}  {_mb(f.stat().st_size)}")
        else:
            print(f"{label}: NOT ready. Check your internet connection, then run this script again.")
    return 0 if ok_wake and ok_stt and ok_voice and ok_detector else 1


if __name__ == "__main__":
    raise SystemExit(main())
