"""
Key detection using Krumhansl-Schmuckler key profiles.
Correlates a pitch-class histogram against all 24 major/minor keys.
"""
import numpy as np

PITCH_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# Krumhansl-Schmuckler profiles
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _correlate(histogram: np.ndarray, profile: np.ndarray) -> list[float]:
    """Pearson correlation for all 12 rotations of the profile."""
    h = histogram - histogram.mean()
    scores = []
    for i in range(12):
        p = np.roll(profile, i)
        p = p - p.mean()
        denom = np.sqrt((h ** 2).sum() * (p ** 2).sum())
        scores.append(float(np.dot(h, p) / denom) if denom > 0 else 0.0)
    return scores


def detect_key(pitch_class_histogram: list[float]) -> dict:
    """
    Returns the best-matching key.

    Returns:
        {
          "root": "A",
          "mode": "minor",
          "label": "A minor",
          "confidence": 0.87,
          "root_midi": 9   # MIDI pitch class (C=0)
        }
    """
    h = np.array(pitch_class_histogram)
    if h.sum() == 0:
        return {"root": "C", "mode": "major", "label": "C major", "confidence": 0.0, "root_midi": 0}

    major_scores = _correlate(h, _MAJOR)
    minor_scores = _correlate(h, _MINOR)

    best_major = max(range(12), key=lambda i: major_scores[i])
    best_minor = max(range(12), key=lambda i: minor_scores[i])

    if major_scores[best_major] >= minor_scores[best_minor]:
        root = best_major
        mode = "major"
        confidence = major_scores[best_major]
    else:
        root = best_minor
        mode = "minor"
        confidence = minor_scores[best_minor]

    return {
        "root": PITCH_NAMES[root],
        "mode": mode,
        "label": f"{PITCH_NAMES[root]} {mode}",
        "confidence": round(confidence, 3),
        "root_midi": root,
    }
