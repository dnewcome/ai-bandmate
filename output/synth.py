"""
FluidSynth wrapper for real-time MIDI playback.
Requires fluidsynth installed and a soundfont (.sf2).

Install:
    sudo apt install fluidsynth fluid-soundfont-gm
    pip install pyfluidsynth

Common soundfont paths:
    /usr/share/sounds/sf2/FluidR3_GM.sf2
    /usr/share/soundfonts/FluidR3_GM.sf2
"""
import os
import fluidsynth

DEFAULT_SF2_PATHS = [
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/soundfonts/FluidR3_GM.sf2",
    os.path.expanduser("~/soundfonts/FluidR3_GM.sf2"),
]

# General MIDI program numbers
GM_PROGRAMS = {
    "piano":          0,   # Acoustic Grand Piano
    "clean_guitar":  27,   # Electric Guitar (clean)
    "bass_finger":   33,   # Finger Bass
    "bass_pick":     34,   # Pick Bass
    "bass_fretless": 35,   # Fretless Bass
    "upright_bass":  32,   # Acoustic Bass
    "synth_bass":    38,   # Synth Bass 1
    "drums":          0,   # Channel 9 is always drums in GM
}


class Synth:
    def __init__(self, sf2_path: str | None = None, gain: float = 0.8):
        self.fs = fluidsynth.Synth(gain=gain)
        self.fs.start(driver="pulseaudio")  # change to "alsa" if needed

        sf2 = sf2_path or self._find_sf2()
        if not sf2:
            raise FileNotFoundError(
                "No soundfont found. Install fluid-soundfont-gm or set sf2_path."
            )
        self.sfid = self.fs.sfload(sf2)
        self._channels: dict[str, int] = {}

    def _find_sf2(self) -> str | None:
        for path in DEFAULT_SF2_PATHS:
            if os.path.exists(path):
                return path
        return None

    def setup_channel(self, channel: int, instrument: str = "bass_finger"):
        """Configure a MIDI channel with a GM instrument."""
        program = GM_PROGRAMS.get(instrument, 33)
        if channel == 9:
            self.fs.program_select(channel, self.sfid, 128, 0)
        else:
            self.fs.program_select(channel, self.sfid, 0, program)

    def note_on(self, channel: int, pitch: int, velocity: int):
        self.fs.noteon(channel, pitch, velocity)

    def note_off(self, channel: int, pitch: int):
        self.fs.noteoff(channel, pitch)

    def close(self):
        self.fs.delete()
