"""The mishearing pass: every voice scenario again at medium and heavy
background noise, with both voices (light noise is the checklist run
itself). What Whisper heard and whether the right action still ran are
recorded for the report; a wrong action fails nothing here, but a safety
problem -- an L2+ action without its confirmation, or going online where
the scenario does not allow it -- still fails the test.
"""

from __future__ import annotations

import pytest

from .scenarios import SCENARIOS
from .test_e2e_checklist import run_scenario

pytestmark = pytest.mark.e2e

VOICE_SCENARIOS = [s for s in SCENARIOS if s.voice and s.id not in ("A1", "G5")]  # launch/close have no spoken command
MODES = [(voice, noise) for noise in ("medium", "heavy") for voice in ("af_bella", "am_michael")]


@pytest.mark.parametrize("mode", MODES, ids=[f"{v}-{n}" for v, n in MODES])
@pytest.mark.parametrize("scenario", VOICE_SCENARIOS, ids=[s.id for s in VOICE_SCENARIOS])
def test_mishearing(launch, request, scenario, mode) -> None:
    """Mishearing."""
    voice, noise = mode
    run, error = run_scenario(launch, request, scenario, "voice", voice, noise, strict=False)
    assert not run.safety, "SAFETY: " + "; ".join(run.safety)
