#!/usr/bin/env bash
# JARVIS one-time setup for macOS. Run it from the JARVIS folder with:
#   bash setup.sh
# Steps: find Python 3.11, make the venv, install the pinned packages,
# download the voice/camera models, create .env. Needs internet this once.
set -u
cd "$(dirname "$0")" || exit 1
echo
echo "=== JARVIS setup (macOS) ==="

# -- 1. Python 3.11 ------------------------------------------------------------
PY=""
for candidate in python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.11 was not found on this Mac."
    echo "Download it from https://www.python.org/downloads/release/python-3117/"
    echo "(macOS 64-bit universal2 installer), then run: bash setup.sh"
    exit 1
fi
echo "[1/5] Python $("$PY" -c 'import sys; print(sys.version.split()[0])') found ($PY)."

# -- 2. The virtual environment -------------------------------------------------
if [ -x "venv/bin/python" ]; then
    echo "[2/5] venv already exists - kept."
else
    echo "[2/5] Creating the virtual environment in venv/ ..."
    "$PY" -m venv venv || { echo "Could not create the virtual environment. Check that Python 3.11 is installed correctly."; exit 1; }
fi

# -- 3. Packages (pinned versions; Windows-only ones are skipped on macOS) -----
echo "[3/5] Installing packages from requirements.txt (a few minutes the first time) ..."
if ! venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt; then
    # Some pinned versions may have no macOS build: retry with them unpinned.
    echo "The pinned packages didn't all install on this Mac. Retrying with requirements-mac.txt,"
    echo "where these are unpinned (pip picks a version that has a macOS build):"
    echo "  opencv-python-headless, scipy, av, onnxruntime, scikit-learn"
    venv/bin/python -m pip install --disable-pip-version-check -r requirements-mac.txt ||
        { echo "Installing the packages failed. Check the internet connection, then run: bash setup.sh"; exit 1; }
fi

# -- 4. Models: wake word, Whisper base.en, Kokoro voice, YOLOX -----------------
echo "[4/5] Downloading the models into models/ (about 520 MB, kept if already there) ..."
venv/bin/python scripts/download_models.py ||
    { echo "Downloading the models failed. Check the internet connection, then run: bash setup.sh"; exit 1; }

# -- 5. .env (secrets; Groq key optional) ----------------------------------------
if [ -f ".env" ]; then
    echo "[5/5] .env already exists - kept."
else
    cp ".env.example" ".env"
    echo "[5/5] Created .env from .env.example. Add your free GROQ_API_KEY there for online answers."
fi

echo
echo "Setup finished. Start JARVIS with: bash run.sh"
echo "When macOS asks, allow the microphone, the camera and screen recording."
