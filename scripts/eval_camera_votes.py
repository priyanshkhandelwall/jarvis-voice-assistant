"""Before/after check for the camera change: one frame at
0.45 versus three frames voted (2 of 3) at 0.6.

The only real photo in the repo is YOLOX's own sample (tests/fixtures/
yolox_dog.jpg: a bicycle, a dog and a truck). Each "look" is three frames
made from a scene the way a webcam gives them: sensor noise, a pixel or two
of shake and a small brightness flicker, different per frame. Scenes are the
photo, crops of it (an object counts as present when at least half its box
is in the crop), lighting changes, a mirror image, and object-free walls
(gradients and textures: nothing should be named there).

Scoring: the pickup is called "truck" or "car" by turns, so either name is
right for it. In a crop, an object less than half visible is "don't care":
naming it is not a false positive, missing it is not a miss.

Run: venv/Scripts/python.exe scripts/eval_camera_votes.py
Nothing is saved except the printed table.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.vision.detector import ObjectDetector, vote  # noqa: E402

PHOTO = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "yolox_dog.jpg"
TRUTH = {"bicycle": (124, 118, 560, 421), "dog": (134, 222, 310, 549), "truck": (462, 76, 694, 171)}
SAME = {"car": "truck"}  # the pickup, either name
LOOKS_PER_SCENE = 5


def frames_of(scene: np.ndarray, rng: np.random.Generator, noise: float):
    """Three noisy, slightly shaken frames of `scene`, like a real webcam look."""
    out = []
    for _ in range(3):
        dx, dy = rng.integers(-2, 3, size=2)
        shaken = cv2.warpAffine(scene, np.float32([[1, 0, dx], [0, 1, dy]]), (scene.shape[1], scene.shape[0]),
                                borderMode=cv2.BORDER_REFLECT)
        gain = 1.0 + rng.uniform(-0.04, 0.04)
        noisy = shaken.astype(np.float32) * gain + rng.normal(0, noise, scene.shape)
        out.append(np.clip(noisy, 0, 255).astype(np.uint8))
    return out


def present(crop):
    """(must be named, may be named): at least half visible / any part visible."""
    x0, y0, x1, y1 = crop
    must, may = set(), set()
    for label, (a, b, c, d) in TRUTH.items():
        inter = max(0, min(c, x1) - max(a, x0)) * max(0, min(d, y1) - max(b, y0))
        if inter >= 0.5 * (c - a) * (d - b):
            must.add(label)
        elif inter > 0:
            may.add(label)
    return must, may


def scenes(photo: np.ndarray, rng: np.random.Generator):
    """The test scenes made from the one real photo: (name, image, objects present, expected count)."""
    h, w = photo.shape[:2]
    full = (0, 0, w, h)
    yield "photo", photo, present(full), 6
    for name, crop in [("left half", (0, 0, w // 2 + 40, h)), ("right half", (w // 2 - 40, 0, w, h)),
                       ("top half", (0, 0, w, h // 2 + 60)), ("centre", (100, 60, 620, 480))]:
        x0, y0, x1, y1 = crop
        yield name, cv2.resize(photo[y0:y1, x0:x1], (w, h)), present(crop), 6
    yield "dim room", cv2.convertScaleAbs(photo, alpha=0.45, beta=0), present(full), 8
    yield "bright window", cv2.convertScaleAbs(photo, alpha=1.2, beta=40), present(full), 6
    yield "mirrored", cv2.flip(photo, 1), present(full), 6
    yield "grainy (cheap webcam)", photo, present(full), 18
    yield "small (far away)", cv2.resize(cv2.resize(photo, (w // 3, h // 3)), (w, h)), present(full), 8
    grad = np.tile(np.linspace(60, 200, w, dtype=np.float32), (h, 1))
    yield "plain wall", cv2.merge([grad, grad * 0.95, grad * 0.9]).astype(np.uint8), (set(), set()), 10
    tex = cv2.GaussianBlur(rng.integers(0, 255, (h, w, 3), dtype=np.uint8), (0, 0), 9)
    yield "textured wall", cv2.normalize(tex, None, 40, 220, cv2.NORM_MINMAX), (set(), set()), 10
    door = np.full((h, w, 3), (170, 180, 190), np.uint8)
    cv2.rectangle(door, (260, 40), (520, 560), (225, 228, 232), -1)  # a tall white box: fridge-shaped, not a fridge
    cv2.circle(door, (490, 300), 8, (90, 90, 90), -1)
    yield "white door on a wall", door, (set(), set()), 10


def _same(per_frame):
    from jarvis.vision.detector import Detection

    return [[Detection(SAME.get(d.label, d.label), d.confidence, d.box) for d in found] for found in per_frame]


def main() -> None:
    """Score the detector's single-frame and voted results on every scene and print precision and recall."""
    detector = ObjectDetector()
    photo = cv2.imread(str(PHOTO))
    rng = np.random.default_rng(20261101)
    totals = {"before": [0, 0, 0], "after": [0, 0, 0]}  # true positives, false positives, missed
    rows = []
    for name, scene, (truth, may), noise in scenes(photo, rng):
        stats = {"before": [0, 0, 0], "after": [0, 0, 0]}
        false_names = {"before": set(), "after": set()}
        for _ in range(LOOKS_PER_SCENE):
            frames = frames_of(scene, rng, noise)
            per_frame = [detector.detect(f, min_confidence=0.45) for f in frames]
            said = {"before": {SAME.get(d.label, d.label) for d in per_frame[0]},  # the old way: one frame at 0.45
                    "after": {SAME.get(d.label, d.label) for d in vote(_same(per_frame), 2, 0.6)}}  # new: 2 of 3 at 0.6
            for way, labels in said.items():
                stats[way][0] += len(labels & truth)
                stats[way][1] += len(labels - truth - may)
                stats[way][2] += len(truth - labels)
                false_names[way] |= labels - truth - may
        for way in totals:
            totals[way] = [a + b for a, b in zip(totals[way], stats[way])]
        rows.append((name, ", ".join(sorted(truth)) or "(nothing)", stats, false_names))

    print(f"{LOOKS_PER_SCENE} looks per scene, 3 frames per look. TP / FP / missed; false labels in brackets.\n")
    print(f"| scene | really there | before (1 frame, 0.45) | after (2 of 3, 0.6) |")
    print("|---|---|---|---|")
    for name, truth, stats, false_names in rows:
        cell = lambda way: f"{stats[way][0]} / {stats[way][1]} / {stats[way][2]}" + (  # noqa: E731
            f" [{', '.join(sorted(false_names[way]))}]" if false_names[way] else "")
        print(f"| {name} | {truth} | {cell('before')} | {cell('after')} |")
    print()
    for way, (tp, fp, missed) in totals.items():
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + missed) if tp + missed else 1.0
        print(f"{way:6}: true {tp}, false {fp}, missed {missed} -> precision {precision:.1%}, recall {recall:.1%}")


if __name__ == "__main__":
    main()
