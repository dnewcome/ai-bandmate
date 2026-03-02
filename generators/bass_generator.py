"""
Rule-based bass line generator (Phase 1).
Given a root note, mode, and tempo, produces a stream of MIDI note events.

Bass patterns are defined per style. Each pattern is a list of
(beat_offset, scale_degree, octave_offset, duration_beats) tuples.
Scale degrees: 1=root, 5=fifth, 8=octave, 3=third, 7=seventh, etc.
"""
import time
import threading
from dataclasses import dataclass

# MIDI note numbers: bass guitar range is roughly 28 (E1) to 60 (C4)
BASS_OCTAVE = 2  # octave 2 puts root C at MIDI 36

# Scale degree semitone offsets (from root) for major and minor
SCALE_SEMITONES = {
    "major": {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11, 8: 12},
    "minor": {1: 0, 2: 2, 3: 3, 4: 5, 5: 7, 6: 8, 7: 10, 8: 12},
}

# Patterns: list of (beat_offset, scale_degree, octave_shift, duration_beats)
PATTERNS = {
    "root_on_one": [
        (0.0, 1, 0, 0.9),
    ],
    "root_fifth": [
        (0.0, 1, 0, 0.9),
        (2.0, 5, 0, 0.9),
    ],
    "walking_major": [
        (0.0, 1, 0, 0.45),
        (1.0, 3, 0, 0.45),
        (2.0, 5, 0, 0.45),
        (3.0, 7, 0, 0.45),
    ],
    "walking_minor": [
        (0.0, 1, 0, 0.45),
        (1.0, 3, 0, 0.45),
        (2.0, 5, 0, 0.45),
        (3.0, 8, -1, 0.45),  # octave below
    ],
    "blues": [
        (0.0, 1, 0, 0.45),
        (0.5, 1, 0, 0.45),
        (1.0, 5, 0, 0.45),
        (1.5, 5, 0, 0.45),
        (2.0, 1, 0, 0.45),
        (2.5, 1, 0, 0.45),
        (3.0, 5, 0, 0.45),
        (3.5, 5, 0, 0.45),
    ],
}

STYLE_PATTERN_MAP = {
    "rock": "root_fifth",
    "blues": "blues",
    "jazz": "walking_major",
    "jazz_minor": "walking_minor",
    "minimal": "root_on_one",
}


@dataclass
class BassNote:
    pitch: int        # MIDI pitch
    velocity: int
    start_time: float # absolute time.monotonic()
    duration: float   # seconds


class BassGenerator:
    def __init__(self, style: str = "rock", bars: int = 1):
        self.style = style
        self.bars = bars
        self._running = False
        self._thread: threading.Thread | None = None
        self._note_callback = None  # fn(pitch, velocity, on: bool)

    def set_note_callback(self, fn):
        """fn(pitch: int, velocity: int, note_on: bool) called for each note event."""
        self._note_callback = fn

    def _degree_to_midi(self, root_midi: int, degree: int, octave_shift: int, mode: str) -> int:
        semitones = SCALE_SEMITONES[mode].get(degree, 0)
        base = (root_midi % 12) + (BASS_OCTAVE + octave_shift) * 12
        return base + semitones

    def generate_bar(self, root_midi: int, mode: str, bpm: float) -> list[BassNote]:
        """Generate one bar of bass notes."""
        pattern_name = STYLE_PATTERN_MAP.get(self.style, "root_fifth")
        pattern = PATTERNS[pattern_name]
        beat_dur = 60.0 / bpm
        notes = []
        now = time.monotonic()
        for beat_offset, degree, oct_shift, dur_beats in pattern:
            pitch = self._degree_to_midi(root_midi, degree, oct_shift, mode)
            pitch = max(28, min(60, pitch))  # clamp to bass range
            notes.append(BassNote(
                pitch=pitch,
                velocity=90,
                start_time=now + beat_offset * beat_dur,
                duration=dur_beats * beat_dur,
            ))
        return notes

    def start_loop(self, root_midi: int, mode: str, bpm: float):
        """Start a background thread playing bass continuously."""
        self.stop()
        self._running = True
        self._thread = threading.Thread(
            target=self._play_loop,
            args=(root_midi, mode, bpm),
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def update_params(self, root_midi: int, mode: str, bpm: float):
        """Hot-update parameters; takes effect on next bar."""
        self._next_root = root_midi
        self._next_mode = mode
        self._next_bpm = bpm

    def _play_loop(self, root_midi: int, mode: str, bpm: float):
        self._next_root = root_midi
        self._next_mode = mode
        self._next_bpm = bpm

        while self._running:
            root_midi = self._next_root
            mode = self._next_mode
            bpm = self._next_bpm

            bar_notes = self.generate_bar(root_midi, mode, bpm)
            bar_duration = (60.0 / bpm) * 4  # 4/4

            for note in bar_notes:
                if not self._running:
                    break
                wait = note.start_time - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                if self._note_callback:
                    self._note_callback(note.pitch, note.velocity, True)
                # Schedule note-off
                def _off(pitch=note.pitch, dur=note.duration):
                    time.sleep(dur)
                    if self._note_callback:
                        self._note_callback(pitch, 0, False)
                threading.Thread(target=_off, daemon=True).start()

            # Wait until end of bar before starting next
            bar_end = bar_notes[0].start_time + bar_duration if bar_notes else time.monotonic()
            remaining = bar_end - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
