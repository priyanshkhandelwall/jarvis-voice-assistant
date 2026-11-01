"""The checklist, end to end: every scenario typed, and spoken by two
Kokoro voices (af_bella, am_michael) over light background noise through
the real wake word + Whisper + listener. Run with: python -m pytest -m e2e
"""

from __future__ import annotations

import traceback
from dataclasses import asdict

import pytest

from .conftest import record
from .e2e_rig import GroqBudgetUsedUp, NOISE_LEVELS, snr_db
from .scenarios import SCENARIOS, Run, finish

pytestmark = pytest.mark.e2e

MODES = [("typed", "af_bella", "light"), ("voice", "af_bella", "light"), ("voice", "am_michael", "light")]


def run_scenario(launch, request, scenario, path: str, voice: str, noise: str, strict: bool):
    """Launch JARVIS for one scenario and mode (typed or voice), run it, and record the result."""
    j = launch(path=path, voice=voice, noise=noise, first_wake=scenario.first_wake, scope=scenario.id)
    j.net.allow(*scenario.online)  # each check still says exactly which services were used, and when
    j.launch()
    run = Run(j, scenario.id, strict=strict)
    error = None
    try:
        scenario.fn(run)
    except GroqBudgetUsedUp as exc:
        pytest.skip(str(exc))
    except Exception:
        error = traceback.format_exc(limit=6)
    if not j.closed and scenario.id != "G5":
        j.settle(20)
    if j.gate.exhausted:
        pytest.skip(f"Groq budget used up ({j.gate.max_real} real calls)")
    run.notes.extend(f"Groq call failed: {e}" for e in j.gate.errors)
    if j.gate.errors:
        run.steps[-1].notes.extend(f"Groq call failed: {e}" for e in j.gate.errors)
    finish(run, scenario.online, scenario.expect_refused)
    timings = asdict(j.timings)
    record(nodeid=request.node.nodeid, kind="checklist" if strict else "mishearing", id=scenario.id, title=scenario.title,
           path=path, voice=voice if path == "voice" else None, noise=noise if path == "voice" else None,
           snr_db=round(snr_db(noise), 1) if path == "voice" else None, noise_rms=NOISE_LEVELS[noise] if path == "voice" else None,
           not_applicable=run.not_applicable, error=error, safety=run.safety,
           steps=[{"id": s.id, "label": s.label, "ok": s.ok, "failures": s.failures, "checks": s.checks, "said": s.said,
                   "transcripts": s.transcripts, "spoken": s.spoken, "notes": s.notes} for s in run.steps],
           timings=timings, groq_real=j.gate.real, groq_replayed=j.gate.replayed,
           hosts=j.net.hosts(), refused=j.net.hosts(allowed=False), online=sorted(set(j.online_calls)))
    return run, error


@pytest.mark.parametrize("mode", MODES, ids=["typed", "voice-af_bella", "voice-am_michael"])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_checklist(launch, request, scenario, mode) -> None:
    """Checklist."""
    path, voice, noise = mode
    run, error = run_scenario(launch, request, scenario, path, voice, noise, strict=True)
    problems = []
    if error:
        problems.append(error)
    problems += [f"SAFETY: {s}" for s in run.safety]
    problems += [f"{s.id} ({s.label}): {f}" for s in run.steps for f in s.failures]
    assert not problems, "\n".join(problems)
