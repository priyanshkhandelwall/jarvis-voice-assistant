"""F6 locked visual design (developer-approved): color tokens,
font family names/paths, and small style helpers shared across app.py.
Pure constants/helpers, no Flet Page access -- kept separate so the
values are easy to grep and cross-check against the spec.
"""

from __future__ import annotations

from pathlib import Path

# -- fonts ---------------------------------------------------------------
# Bundled locally under jarvis/dashboard/assets/fonts/ (SIL OFL 1.1, each
# family's own OFL.txt sits beside its files) -- never loaded from a CDN,
# since the core must work offline (F13/R2). ASSETS_DIR is passed to
# ft.run(assets_dir=...) in app.py's run(); FONT_PATHS below are relative
# to it, which is Flet's documented way to register a locally bundled font
# with page.fonts (a dict of family name -> path under assets_dir).

ASSETS_DIR = str(Path(__file__).resolve().parent / "assets")

FONT_ORBITRON = "Orbitron"  # headers, panel titles, state text, kbd tags, numeric values
FONT_RAJDHANI = "Rajdhani"  # module names, notices
FONT_RAJDHANI_MEDIUM = "Rajdhani Medium"
FONT_RAJDHANI_SEMIBOLD = "Rajdhani SemiBold"
FONT_MONO = "JetBrains Mono"  # log entries, command input, body text

FONT_PATHS: dict[str, str] = {
    FONT_ORBITRON: "fonts/orbitron/Orbitron-Variable.ttf",
    FONT_RAJDHANI: "fonts/rajdhani/Rajdhani-Regular.ttf",
    FONT_RAJDHANI_MEDIUM: "fonts/rajdhani/Rajdhani-Medium.ttf",
    FONT_RAJDHANI_SEMIBOLD: "fonts/rajdhani/Rajdhani-SemiBold.ttf",
    FONT_MONO: "fonts/jetbrains_mono/JetBrainsMono-Regular.ttf",
}

# -- colors (locked spec) ------------------------------------

BG_BASE = "#03101A"
BG_VIGNETTE = "#082B45"
PANEL_BG = "#061826"
PANEL_BORDER = "#123449"
TILE_BG = "#081B29"

TEXT_PRIMARY = "#FFFFFF"
TEXT_SECONDARY = "#A6D4E8"
TEXT_DIM = "#7099AF"

CYAN_ACCENT = "#00E5FF"
ICON_INACTIVE = "#33BFE3"
CHIP_BORDER = "#164058"

STATUS_OK = "#5DEAB0"       # nominal / built / ok
STATUS_ELEVATED = "#FFB020"  # elevated / in-progress
STATUS_CRITICAL = "#FF5C73"  # stop / critical

CONFIRM_BG = "#160F06"
CONFIRM_BORDER = "#4A3210"
