"""
Rule-based drum generator.
Produces a stream of MIDI percussion note events on GM channel 9.

Patterns are defined as lists of:
  (beat_offset, gm_note, base_velocity, vel_variation)

vel_variation adds a random ± to base_velocity each hit for a human feel.
Swing/shuffle feel is encoded directly in beat_offsets (triplet 8ths = 2/3).
"""
import time
import random
import threading

# GM percussion note numbers (channel 9)
KICK         = 36
SNARE        = 38
HIHAT_CLOSED = 42
HIHAT_PEDAL  = 44
HIHAT_OPEN   = 46
RIDE         = 51
CRASH        = 49
FLOOR_TOM    = 41
MID_TOM      = 47
HIGH_TOM     = 50

# Each entry: (beat_offset, gm_note, base_velocity, vel_variation)
DRUM_PATTERNS = {
    "rock": [
        # Kick on 1 and 3
        (0.0,  KICK,          100, 8),
        (2.0,  KICK,           90, 8),
        # Snare on 2 and 4
        (1.0,  SNARE,          95, 5),
        (3.0,  SNARE,         100, 5),
        # Closed hi-hat on every 8th note, downbeats louder
        (0.0,  HIHAT_CLOSED,   75, 5),
        (0.5,  HIHAT_CLOSED,   55, 5),
        (1.0,  HIHAT_CLOSED,   65, 5),
        (1.5,  HIHAT_CLOSED,   55, 5),
        (2.0,  HIHAT_CLOSED,   75, 5),
        (2.5,  HIHAT_CLOSED,   55, 5),
        (3.0,  HIHAT_CLOSED,   65, 5),
        (3.5,  HIHAT_CLOSED,   55, 5),
    ],
    "blues": [
        # Kick on 1 and 3
        (0.0,   KICK,          100, 8),
        (2.0,   KICK,           90, 8),
        # Snare on 2 and 4
        (1.0,   SNARE,          95, 5),
        (3.0,   SNARE,         100, 5),
        # Shuffle hi-hat: swing 8ths (triplet feel, long-short)
        (0.0,   HIHAT_CLOSED,   80, 5),
        (0.667, HIHAT_CLOSED,   55, 5),
        (1.0,   HIHAT_CLOSED,   70, 5),
        (1.667, HIHAT_CLOSED,   55, 5),
        (2.0,   HIHAT_CLOSED,   80, 5),
        (2.667, HIHAT_CLOSED,   55, 5),
        (3.0,   HIHAT_CLOSED,   70, 5),
        (3.667, HIHAT_CLOSED,   55, 5),
    ],
    "jazz": [
        # Ride cymbal: swing 8th pattern ("ding-ding-a-ding")
        (0.0,   RIDE,  80, 8),
        (0.667, RIDE,  55, 8),
        (1.0,   RIDE,  70, 8),
        (1.667, RIDE,  55, 8),
        (2.0,   RIDE,  80, 8),
        (2.667, RIDE,  55, 8),
        (3.0,   RIDE,  70, 8),
        (3.667, RIDE,  55, 8),
        # Hi-hat foot on 2 and 4
        (1.0,   HIHAT_PEDAL, 65, 5),
        (3.0,   HIHAT_PEDAL, 65, 5),
        # Light kick on beat 1
        (0.0,   KICK,  55, 10),
    ],
    "minimal": [
        (0.0,  KICK,   90, 8),
        (2.0,  SNARE,  85, 8),
    ],
}

DRUM_PATTERNS["jazz_minor"] = DRUM_PATTERNS["jazz"]

DRUM_STYLE_MAP = {
    "rock":       "rock",
    "blues":      "blues",
    "jazz":       "jazz",
    "jazz_minor": "jazz_minor",
    "minimal":    "minimal",
}


class DrumGenerator:
    def __init__(self, style: str = "rock"):
        self.style = style
        self._running = False
        self._thread: threading.Thread | None = None
        self._note_callback = None  # fn(pitch, velocity, note_on: bool)
        self._next_bpm: float = 120.0
        self._next_style: str = style

    def set_note_callback(self, fn):
        self._note_callback = fn

    def start_loop(self, bpm: float):
        self.stop()
        self._running = True
        self._next_bpm = bpm
        self._next_style = self.style
        self._thread = threading.Thread(target=self._play_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def update_params(self, bpm: float):
        """Hot-update BPM; takes effect on next bar."""
        self._next_bpm = bpm
        self._next_style = self.style

    def generate_bar(self, bpm: float) -> list[tuple[float, int, int]]:
        """Returns list of (absolute_time, gm_note, velocity) sorted by time."""
        pattern_key = DRUM_STYLE_MAP.get(self.style, "rock")
        pattern = DRUM_PATTERNS[pattern_key]
        beat_dur = 60.0 / bpm
        now = time.monotonic()
        notes = []
        for beat_offset, note, base_vel, vel_var in pattern:
            velocity = max(1, min(127, base_vel + random.randint(-vel_var, vel_var)))
            notes.append((now + beat_offset * beat_dur, note, velocity))
        return sorted(notes, key=lambda x: x[0])

    def _play_loop(self):
        while self._running:
            bpm = self._next_bpm
            self.style = self._next_style

            bar_notes = self.generate_bar(bpm)
            bar_duration = (60.0 / bpm) * 4

            for start_time, pitch, velocity in bar_notes:
                if not self._running:
                    break
                wait = start_time - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                if self._note_callback:
                    self._note_callback(pitch, velocity, True)
                    # Percussion notes are short — fire note-off after 60ms
                    def _off(p=pitch):
                        time.sleep(0.06)
                        if self._note_callback:
                            self._note_callback(p, 0, False)
                    threading.Thread(target=_off, daemon=True).start()

            # Wait until bar end before starting next
            bar_end = bar_notes[0][0] + bar_duration if bar_notes else time.monotonic()
            remaining = bar_end - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
