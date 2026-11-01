"""Builds the tables of the e2e report from tests/e2e/results/e2e_results.json.

    venv\\Scripts\\python.exe tests\\e2e\\make_report.py > tables.md

Prints markdown: the checklist table, the mishearing table and the timings.
The prose around them (bugs, what can't be automated) is written by hand.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results" / "e2e_results.json"


def cell(text: str) -> str:
    """Text safe for a Markdown table cell."""
    return " ".join(str(text).replace("|", "/").split())


def verdict(row: dict | None) -> str:
    """PASS, FAIL, N/A or 'not run' for one result row."""
    if row is None:
        return "not run"
    if row.get("not_applicable"):
        return "N/A"
    ok = not row.get("error") and not row.get("safety") and all(s["ok"] for s in row["steps"])
    return "PASS" if ok else "FAIL"


def main() -> None:
    """Turn e2e_results.json into the Markdown checklist report."""
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    rows = data["rows"]
    checklist = [r for r in rows if r["kind"] == "checklist"]
    by = defaultdict(dict)
    order = []
    for r in checklist:
        key = "typed" if r["path"] == "typed" else f"voice-{r['voice']}"
        if r["id"] not in by:
            order.append(r["id"])
        by[r["id"]][key] = r
        by[r["id"]]["title"] = r["title"]

    print("| ID | scenario | voice (af_bella / am_michael) | typed | notes |")
    print("|---|---|---|---|---|")
    for sid in order:
        e = by[sid]
        notes = []
        for key in ("typed", "voice-af_bella", "voice-am_michael"):
            r = e.get(key)
            if r is None:
                continue
            for s in r["steps"]:
                for f in s["failures"]:
                    notes.append(f"{key}: {f}")
                notes.extend(f"{key}: {n}" for n in s["notes"] if key != "voice-am_michael")
            if r.get("not_applicable") and key == "typed":
                notes.append(f"typed: N/A ({r['not_applicable']})")
            if r.get("not_applicable") and key.startswith("voice") and key.endswith("bella"):
                notes.append(f"voice: N/A ({r['not_applicable']})")
            if r.get("error"):
                notes.append(f"{key}: error {r['error'].strip().splitlines()[-1]}")
            notes.extend(f"{key}: SAFETY {s}" for s in r.get("safety", []))
        voice = f"{verdict(e.get('voice-af_bella'))} / {verdict(e.get('voice-am_michael'))}"
        print(f"| {sid} | {cell(e['title'])} | {voice} | {verdict(e.get('typed'))} | {cell('; '.join(dict.fromkeys(notes)) or '')[:600]} |")

    print("\n\n| ID | step | noise (SNR) | voice | Whisper transcript | right action? |")
    print("|---|---|---|---|---|---|")
    voice_rows = [r for r in rows if r["path"] == "voice" and not r.get("not_applicable")]
    noise_order = {"light": 0, "medium": 1, "heavy": 2}
    voice_rows.sort(key=lambda r: (order.index(r["id"]) if r["id"] in order else 99, noise_order.get(r["noise"], 9), r["voice"]))
    for r in voice_rows:
        for s in r["steps"]:
            if not s["transcripts"]:
                continue
            heard = " / ".join(t.strip() for t in s["transcripts"])
            ok = "yes" if s["ok"] and not r.get("safety") else "NO: " + cell("; ".join(s["failures"] + r.get("safety", [])))[:160]
            print(f"| {s['id']} | {cell(' / '.join(s['said']))} | {r['noise']} ({r['snr_db']} dB) | {r['voice']} | {cell(heard)} | {ok} |")

    def stats(values):
        values = [v for v in values if v is not None]
        if not values:
            return "-"
        return f"median {statistics.median(values):.2f} s, min {min(values):.2f}, max {max(values):.2f} (n={len(values)})"

    t = defaultdict(list)
    startup = defaultdict(list)
    for r in checklist:
        tm = r["timings"]
        for k in ("wake_latency", "first_audio", "transcribe"):
            t[k + ("" if r["path"] == "voice" or k == "first_audio" else "")].extend(tm[k])
        t[f"command_to_action ({r['path']})"].extend(tm["command_to_action"])
        if r["id"] == "A1":
            for k, v in tm["startup"].items():
                startup[k].append(v)
    print("\n\n| measure | value |")
    print("|---|---|")
    for k, v in sorted(t.items()):
        print(f"| {k} | {stats(v)} |")
    print("\n\n| startup step (seconds after main(page) began) | median of A1 runs |")
    print("|---|---|")
    for k, v in sorted(startup.items(), key=lambda kv: statistics.median(kv[1])):
        print(f"| {k} | {statistics.median(v):.3f} s (n={len(v)}) |")

    outcomes = data.get("outcomes", {})
    counts = defaultdict(int)
    for nodeid, o in outcomes.items():
        counts[o["outcome"]] += 1
    print("\n\noutcomes:", dict(counts), file=sys.stderr)


if __name__ == "__main__":
    main()
