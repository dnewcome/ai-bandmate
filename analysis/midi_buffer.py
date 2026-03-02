"""
Rolling buffer of recent MIDI note events.
Accumulates notes over a time window for analysis.
"""
import time
from dataclasses import dataclass, field
from collections import deque


@dataclass
class NoteEvent:
    pitch: int       # 0-127
    velocity: int    # 0-127
    timestamp: float # time.monotonic()
    channel: int = 0


class MidiBuffer:
    def __init__(self, window_seconds: float = 4.0):
        self.window_seconds = window_seconds
        self._events: deque[NoteEvent] = deque()

    def add(self, pitch: int, velocity: int, channel: int = 0):
        self._events.append(NoteEvent(pitch, velocity, time.monotonic(), channel))
        self._prune()

    def _prune(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()

    def recent_notes(self) -> list[NoteEvent]:
        self._prune()
        return list(self._events)

    def pitch_class_histogram(self, decay_seconds: float = 3.0) -> list[float]:
        """
        Returns normalized 12-bin chroma histogram with exponential decay.
        Recent notes contribute more than older ones.
        decay_seconds: half-life — a note that age is 1x decay_seconds contributes
        ~37% as much as a note played right now.
        """
        import math
        counts = [0.0] * 12
        events = self.recent_notes()
        if not events:
            return counts
        now = time.monotonic()
        for e in events:
            age = now - e.timestamp
            weight = (e.velocity / 127.0) * math.exp(-age / decay_seconds)
            counts[e.pitch % 12] += weight
        total = sum(counts)
        if total > 0:
            counts = [c / total for c in counts]
        return counts

    def note_on_timestamps(self) -> list[float]:
        """All note-on timestamps in the window (for tempo tracking)."""
        return [e.timestamp for e in self.recent_notes() if e.velocity > 0]
