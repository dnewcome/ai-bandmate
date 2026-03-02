"""
Real-time tempo estimation from note-on inter-onset intervals (IOI).
Uses a simple autocorrelation approach over recent onsets.
"""
import numpy as np
import time


class TempoTracker:
    def __init__(self, min_bpm: float = 40.0, max_bpm: float = 240.0):
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self._bpm: float = 120.0
        self._confidence: float = 0.0
        self._last_onset: float = 0.0
        self._recent_iois: list[float] = []  # inter-onset intervals in seconds
        self._max_iois = 8

    def on_note(self, timestamp: float | None = None):
        """Call on every note-on event."""
        now = timestamp if timestamp is not None else time.monotonic()
        if self._last_onset > 0:
            ioi = now - self._last_onset
            min_ioi = 60.0 / self.max_bpm
            max_ioi = 60.0 / self.min_bpm
            if ioi > 2.0:
                # Long gap — player paused. Discard stale IOIs so old tempo
                # doesn't contaminate the new estimate after they resume.
                self._recent_iois.clear()
                self._confidence = 0.0
            elif min_ioi <= ioi <= max_ioi:
                self._recent_iois.append(ioi)
                if len(self._recent_iois) > self._max_iois:
                    self._recent_iois.pop(0)
                self._estimate()
        self._last_onset = now

    def _estimate(self):
        if len(self._recent_iois) < 3:
            return
        iois = np.array(self._recent_iois)
        median_ioi = float(np.median(iois))
        # Try subdivisions: the IOI might be 1 beat, 1/2, or 2 beats
        candidates = []
        for mult in [0.5, 1.0, 2.0]:
            bpm = 60.0 / (median_ioi * mult)
            if self.min_bpm <= bpm <= self.max_bpm:
                candidates.append(bpm)
        if candidates:
            # Pick the candidate closest to 120 bpm (most natural musical tempo)
            self._bpm = min(candidates, key=lambda b: abs(b - 120.0))
            std = float(np.std(iois))
            self._confidence = max(0.0, 1.0 - std / median_ioi)

    @property
    def bpm(self) -> float:
        return round(self._bpm, 1)

    @property
    def beat_duration(self) -> float:
        """Seconds per beat."""
        return 60.0 / self._bpm

    @property
    def confidence(self) -> float:
        return round(self._confidence, 3)
