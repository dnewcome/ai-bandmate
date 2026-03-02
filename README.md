# AI Bandmate

A real-time AI accompaniment system. Play into a MIDI controller and the system
listens to your notes, figures out what key and tempo you are in, and generates
a matching bass line and drum groove that follows you as you play — changing
keys and tempo on the fly as you move through a song.

The system also plays your MIDI controller through a chosen instrument (piano or
clean electric guitar) so you hear yourself in the mix alongside the band.

Future phases add LLM-driven style orchestration (Ollama) and audio input from
a guitar pickup.

---

## Table of Contents

- [How It Works](#how-it-works)
  - [Signal Flow](#signal-flow)
  - [MIDI Buffer](#midi-buffer)
  - [Key Detection](#key-detection)
  - [Tempo Tracking](#tempo-tracking)
  - [Bass Generation](#bass-generation)
  - [Drum Generation](#drum-generation)
  - [Audio Output](#audio-output)
  - [Threading Model](#threading-model)
- [Installation](#installation)
  - [System Dependencies](#system-dependencies)
  - [Python Dependencies](#python-dependencies)
  - [Soundfonts](#soundfonts)
- [Usage](#usage)
  - [List MIDI Ports](#list-midi-ports)
  - [Run](#run)
  - [Keyboard Controls](#keyboard-controls)
  - [Styles](#styles)
- [Project Structure](#project-structure)
- [Extending the System](#extending-the-system)
  - [Adding Bass Patterns](#adding-bass-patterns)
  - [Adding Drum Patterns](#adding-drum-patterns)
  - [Phase 2: LLM Style Orchestration](#phase-2-llm-style-orchestration)
  - [Phase 3: Guitar Audio Input](#phase-3-guitar-audio-input)
- [Hardware Notes](#hardware-notes)
- [Troubleshooting](#troubleshooting)

---

## How It Works

### Signal Flow

```
MIDI Controller
      │
      │  note-on / note-off messages
      ▼
 MidiBuffer ──────────────────────────────────┐
      │                                        │
      │  pitch class histogram (every 1s)      │  note-on timestamps
      ▼                                        ▼
 KeyDetector                            TempoTracker
      │                                        │
      │  root note + mode           BPM + confidence
      └──────────────┬──────────────────────┬──┘
                     │                      │
              ┌──────┘                      └──────┐
              ▼                                    ▼
       BassGenerator                       DrumGenerator
      (background thread)                (background thread)
              │                                    │
              └──────────────┬─────────────────────┘
                             │  MIDI note-on / note-off
                             ▼
                 ┌─────────────────────┐
                 │        Synth        │
                 │     (FluidSynth)    │
                 │  ch 0: lead inst.   │◄── MIDI passthrough (your playing)
                 │  ch 1: bass         │◄── BassGenerator
                 │  ch 9: drums        │◄── DrumGenerator
                 └─────────────────────┘
                             │
                             ▼
                       Audio Output
                   (PulseAudio / ALSA)
```

Every incoming MIDI note feeds three things simultaneously: the `MidiBuffer`
accumulates notes for key analysis, the `TempoTracker` records onset timing for
BPM estimation, and the note is passed through to FluidSynth so you hear
yourself playing. A background analysis thread re-runs key detection every
second and hot-updates both generators. Each generator runs in its own thread,
looping bar by bar and picking up parameter changes at bar boundaries so
transitions are always musically clean.

---

### MIDI Buffer

**File:** `analysis/midi_buffer.py`

The `MidiBuffer` maintains a rolling window of `NoteEvent` objects — each
storing pitch, velocity, timestamp, and channel. The window defaults to 8
seconds. Events older than the window are pruned lazily on each access.

The primary output of the buffer is a **pitch class histogram**: a 12-element
list (one bucket per semitone, C through B) where each element is the
velocity-weighted sum of all notes in that pitch class, normalized to sum to
1.0. This collapses octave information and produces a compact fingerprint of
what notes have been played recently, which is exactly what key detection
needs.

```
MIDI pitch → pitch % 12 = pitch class (0=C, 1=C#, ..., 11=B)
Each note contributes its velocity/127 to that bucket.
Result is normalized so all 12 values sum to 1.0.
```

Weights also decay **exponentially with age** — a note played 3 seconds ago
contributes ~37% as much as a note played right now. This means a key change
registers within a few seconds rather than waiting for old notes to age out of
the full 8-second window.

---

### Key Detection

**File:** `analysis/key_detector.py`

Key detection uses the **Krumhansl-Schmuckler algorithm**, a well-established
music cognition technique that correlates a pitch-class distribution against
empirically derived key profiles.

Carol Krumhansl and Mark Schmuckler derived their profiles from listener
experiments measuring how well each of the 12 pitch classes "fit" a given key.
For example, in C major, the notes C, E, and G (the tonic chord) feel most
stable, so they have the highest weights. The profiles look like this:

```
Major profile (starting at root):
  [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
   C     C#    D     D#    E     F     F#    G     G#    A     A#    B

Minor profile (starting at root):
  [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
```

The algorithm tests all 24 possible keys (12 roots × major/minor) by rotating
each profile to each possible tonic and computing the **Pearson correlation
coefficient** between the observed histogram and the profile. The key with the
highest correlation wins.

```
For each of 24 keys:
  1. Rotate the profile so index 0 = candidate root
  2. Compute Pearson r between histogram and rotated profile
  3. Keep the key with the highest r

Pearson r = dot(h - mean(h), p - mean(p)) / (|h - mean(h)| * |p - mean(p)|)
```

The returned `confidence` value is the raw Pearson correlation coefficient
(range -1 to 1). Values above ~0.6 are generally reliable. The system updates
on any detection above 0.2 and re-evaluates every second.

**Known limitations:** The algorithm can be confused by highly chromatic playing,
modal music, or playing over a short window where not enough pitch classes have
been heard. Playing at least a few seconds of chord tones before the system
picks up gives better results.

---

### Tempo Tracking

**File:** `analysis/tempo_tracker.py`

Tempo is estimated from **inter-onset intervals (IOI)** — the time gaps between
consecutive note-on events. This is a lightweight approach that works well for
rhythmically consistent playing and requires no audio signal processing.

The algorithm:

1. Record the timestamp of every note-on event using `time.monotonic()`
2. Compute the IOI between each pair of consecutive onsets
3. Reject IOIs outside the valid tempo range (40–240 BPM)
4. If a gap > 2 seconds is detected, clear the IOI buffer — the player paused,
   and stale intervals would corrupt the new estimate
5. Keep a rolling window of up to 8 recent IOIs
6. Take the **median** of the window to get an estimate robust to outliers
7. Try three subdivision hypotheses: the median IOI might represent a half note
   (×2), a quarter note (×1), or an eighth note (×0.5)
8. Of the valid candidates, pick the one closest to 120 BPM (a prior toward
   common musical tempos)
9. Compute **confidence** as `1 - (std / median)` — lower variance means higher
   confidence

Confidence crosses 0.3 fairly quickly once you play a few evenly-spaced notes,
which is the threshold used to start the band.

---

### Bass Generation

**File:** `generators/bass_generator.py`

The bass generator translates musical parameters (root, mode, BPM, style) into
a stream of timed MIDI note events.

#### Scale Degrees

Bass patterns are written in terms of **scale degrees** rather than absolute
pitches, so they work in any key automatically. The degree-to-MIDI conversion
uses semitone offset tables for major and minor scales:

```
Major: 1→0, 2→2, 3→4, 4→5, 5→7, 6→9, 7→11, 8→12
Minor: 1→0, 2→2, 3→3, 4→5, 5→7, 6→8, 7→10, 8→12
```

A scale degree is resolved to a MIDI pitch by taking the detected root note's
pitch class, placing it in octave 2 (MIDI 36 for C), and adding the semitone
offset. The result is clamped to the practical bass guitar range (MIDI 28–60,
E1 to C4).

#### Patterns

Each pattern is a list of `(beat_offset, scale_degree, octave_shift,
duration_beats)` tuples describing where in the bar each note falls:

| Pattern | Description |
|---|---|
| `root_on_one` | Single root note on beat 1. Minimal, leaves maximum space. |
| `root_fifth` | Root on beat 1, fifth on beat 3. Classic rock/pop movement. |
| `walking_major` | Root → third → fifth → seventh across all four beats. Jazz feel. |
| `walking_minor` | Root → minor third → fifth → octave. Jazz/soul in minor keys. |
| `blues` | Root and fifth alternating on eighth notes. Shuffle-ready blues. |

#### Playback Loop

The generator runs in a dedicated **daemon thread**. Each iteration:

1. Reads the current `_next_root`, `_next_mode`, `_next_bpm` (updated by the
   analysis thread via `update_params()`)
2. Calls `generate_bar()` to compute absolute timestamps for all notes in the
   bar (based on `time.monotonic()` at generation time)
3. Iterates through each note, sleeping until its start time, then firing the
   note-on callback
4. Spawns a small per-note thread to fire the note-off after the note's duration
5. Waits for the remainder of the bar duration before looping

Parameter changes take effect at bar boundaries — this prevents mid-bar key
changes that would sound jarring.

---

### Drum Generation

**File:** `generators/drum_generator.py`

The drum generator produces GM percussion events on MIDI channel 9. Like the
bass generator it loops bar-by-bar in a background thread, picking up BPM and
style changes at each bar boundary.

#### Patterns

Each drum pattern is a list of `(beat_offset, gm_note, base_velocity,
vel_variation)` tuples. `vel_variation` adds a random `±N` to `base_velocity`
on every hit, giving a slightly human feel rather than a rigid machine grid.

Swing and shuffle feels are encoded directly in `beat_offset` values using
triplet 8th note positions (multiples of 2/3 of a beat) rather than straight
8ths (multiples of 0.5).

**GM percussion notes used:**

| Constant | GM Note | Instrument |
|---|---|---|
| `KICK` | 36 | Bass Drum 1 |
| `SNARE` | 38 | Acoustic Snare |
| `HIHAT_CLOSED` | 42 | Closed Hi-Hat |
| `HIHAT_PEDAL` | 44 | Pedal Hi-Hat |
| `HIHAT_OPEN` | 46 | Open Hi-Hat |
| `RIDE` | 51 | Ride Cymbal 1 |
| `CRASH` | 49 | Crash Cymbal 1 |

**Patterns per style:**

| Style | Kick | Snare | Hat/Ride |
|---|---|---|---|
| `rock` | beats 1, 3 | beats 2, 4 | straight 8th closed hat |
| `blues` | beats 1, 3 | beats 2, 4 | swing 8th (shuffle) closed hat |
| `jazz` | light on beat 1 | — | swing ride + pedal hat on 2 & 4 |
| `jazz_minor` | same as jazz | — | same as jazz |
| `minimal` | beat 1 | beat 3 | none |

Drum note-offs are fired 60ms after each note-on — percussion sounds in GM
soundfonts are self-sustaining samples and do not need long held notes.

---

### Audio Output

**File:** `output/synth.py`

Audio is produced by **FluidSynth**, a software synthesizer that renders MIDI
to audio in real time using **SoundFont (.sf2)** sample libraries.

The `Synth` class wraps the `pyfluidsynth` bindings and handles:

- Starting the FluidSynth engine with the PulseAudio driver (configurable to
  ALSA for low-latency setups)
- Loading a `.sf2` soundfont file, searching standard system paths automatically
- Configuring MIDI channels with General MIDI programs (instruments)
- Dispatching `note_on` and `note_off` events

**MIDI channel assignments:**

| Channel | Role | Instrument |
|---|---|---|
| 0 | Lead (your playing) | Piano or Clean Electric Guitar |
| 1 | Bass | Finger Bass (GM 33) |
| 9 | Drums | GM Percussion (bank 128) |

**General MIDI instrument numbers available:**

| Name | GM Program | Description |
|---|---|---|
| `piano` | 0 | Acoustic Grand Piano |
| `clean_guitar` | 27 | Electric Guitar (clean) |
| `bass_finger` | 33 | Fingered electric bass |
| `bass_pick` | 34 | Picked electric bass |
| `bass_fretless` | 35 | Fretless bass |
| `upright_bass` | 32 | Acoustic upright bass |
| `synth_bass` | 38 | Synth bass 1 |

If the synth fails to initialize (missing soundfont, audio driver issue), the
system falls back gracefully and continues running in analysis-only mode,
printing state to the terminal.

---

### Threading Model

| Thread | Role |
|---|---|
| Main thread | Keyboard input polling via `select()`, startup/shutdown |
| MIDI callback thread | Managed by `python-rtmidi`; calls `midi_message_handler` on each message |
| Analysis thread | Sleeps 1s, recomputes key + BPM, calls `update_params()` on both generators |
| Bass playback thread | Loops bar-by-bar, sleeps precisely to hit note timings |
| Drum playback thread | Same structure as bass playback, fires percussion events |
| Note-off threads | One short-lived thread per note, fires the note-off after duration |

State shared between threads (`current_key`, `bass_started`) uses simple
Python assignment which is safe for these types due to the GIL. Each
generator's `_next_*` fields are written by the analysis thread and read by the
playback thread at bar boundaries — no mutex is needed because a slightly stale
read just means the change takes one extra bar, which is musically fine.

---

## Installation

### System Dependencies

```bash
# FluidSynth synthesizer engine
sudo apt install fluidsynth

# General MIDI soundfont (FluidR3)
sudo apt install fluid-soundfont-gm

# MIDI library development headers (for python-rtmidi)
sudo apt install libasound2-dev libjack-dev
```

### Python Dependencies

Python 3.11 or newer is required (uses `list[T]` type hints and `X | Y` union
syntax).

```bash
pip install -r requirements.txt
```

Contents of `requirements.txt`:

```
python-rtmidi>=1.5.8
mido>=1.3.2
numpy>=1.26.0
pyfluidsynth>=1.3.3
```

### Soundfonts

The system looks for a soundfont in these locations in order:

1. `--sf2 /path/to/file.sf2` (command-line argument)
2. `/usr/share/sounds/sf2/FluidR3_GM.sf2`
3. `/usr/share/soundfonts/FluidR3_GM.sf2`
4. `~/soundfonts/FluidR3_GM.sf2`

Installing `fluid-soundfont-gm` via apt covers option 2 or 3 depending on
your distribution. For higher quality, replace with a better `.sf2` such as
[GeneralUser GS](https://schristiancollins.com/generaluser.php) or
[Salamander Grand Piano](https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html).

---

## Usage

### List MIDI Ports

Before running, find the name of your MIDI controller:

```bash
python main.py --list-ports
```

Example output:

```
Available MIDI input ports:
  [0] Midi Through Port-0
  [1] X2mini MIDI 1
```

### Run

```bash
# Interactive prompts for port and instrument selection
python main.py

# Skip prompts with flags
python main.py --port X2mini --instrument piano --style rock
python main.py --port X2mini --instrument clean_guitar --style blues

# Specify a custom soundfont
python main.py --port X2mini --instrument piano --sf2 ~/soundfonts/GeneralUser.sf2
```

At startup you will be prompted to choose your lead instrument if `--instrument`
is not provided:

```
Select your instrument:
  [1] Piano
  [2] Clean Electric Guitar
Choice [1-2]:
```

The band does not start immediately. The system waits until the tempo tracker
has enough confidence (after roughly 4–8 evenly-spaced notes) before engaging
bass and drums together.

### Keyboard Controls

While the program is running, single keypresses control the system:

| Key | Action |
|---|---|
| `s` | Print current key, BPM, confidence scores, and active style |
| `1` | Switch to **rock** style |
| `2` | Switch to **blues** style |
| `3` | Switch to **jazz** style |
| `4` | Switch to **jazz_minor** style |
| `5` | Switch to **minimal** style |
| `q` | Quit cleanly |

Style changes affect both bass and drums simultaneously, taking effect at the
next bar boundary.

### Styles

| Style | Bass | Drums |
|---|---|---|
| `rock` | Root on 1, fifth on 3 | Kick 1&3, snare 2&4, straight 8th hat |
| `blues` | Root/fifth shuffle 8ths | Kick 1&3, snare 2&4, swing 8th hat |
| `jazz` | Walking 1-3-5-7 | Swing ride, pedal hat on 2&4, light kick |
| `jazz_minor` | Walking 1-b3-5-octave | Same as jazz |
| `minimal` | Root on beat 1 only | Kick on 1, snare on 3, no hat |

---

## Project Structure

```
ai-bandmate/
├── main.py                     Entry point and runtime loop
├── requirements.txt
├── analysis/
│   ├── __init__.py
│   ├── midi_buffer.py          Rolling note event window + decaying chroma histogram
│   ├── key_detector.py         Krumhansl-Schmuckler key detection
│   └── tempo_tracker.py        IOI-based BPM estimation with pause detection
├── generators/
│   ├── __init__.py
│   ├── bass_generator.py       Rule-based bass patterns + playback thread
│   └── drum_generator.py       Rule-based drum patterns + playback thread
└── output/
    ├── __init__.py
    └── synth.py                FluidSynth wrapper (lead, bass, drums channels)
```

---

## Extending the System

### Adding Bass Patterns

Add an entry to `PATTERNS` in `generators/bass_generator.py`:

```python
PATTERNS["reggae"] = [
    (0.75, 1, 0, 0.2),   # skipped beat 1 (anticipation)
    (2.75, 5, 0, 0.2),   # skipped beat 3
]
STYLE_PATTERN_MAP["reggae"] = "reggae"
```

Each tuple is `(beat_offset, scale_degree, octave_shift, duration_beats)`:

- `beat_offset`: Position in the 4/4 bar (0.0 = beat 1, 1.0 = beat 2, 0.5 =
  the "and" of beat 1, 0.667 = triplet 8th, etc.)
- `scale_degree`: 1=root, 2=second, 3=third, 4=fourth, 5=fifth, 6=sixth,
  7=seventh, 8=octave above root
- `octave_shift`: Shifts the note by octaves (0 = bass register, -1 = lower)
- `duration_beats`: Note length in beats (0.9 ≈ detached, 0.45 ≈ walking,
  0.25 ≈ staccato)

### Adding Drum Patterns

Add an entry to `DRUM_PATTERNS` in `generators/drum_generator.py`:

```python
DRUM_PATTERNS["reggae"] = [
    # Kick on beat 3 only (one-drop)
    (2.0,  KICK,          95, 8),
    # Snare on beats 2 and 4
    (1.0,  SNARE,         90, 5),
    (3.0,  SNARE,         95, 5),
    # Closed hat on all 8th notes
    (0.0,  HIHAT_CLOSED,  65, 5),
    (0.5,  HIHAT_CLOSED,  50, 5),
    (1.0,  HIHAT_CLOSED,  65, 5),
    (1.5,  HIHAT_CLOSED,  50, 5),
    (2.0,  HIHAT_CLOSED,  65, 5),
    (2.5,  HIHAT_CLOSED,  50, 5),
    (3.0,  HIHAT_CLOSED,  65, 5),
    (3.5,  HIHAT_CLOSED,  50, 5),
]
DRUM_STYLE_MAP["reggae"] = "reggae"
```

Each tuple is `(beat_offset, gm_note, base_velocity, vel_variation)`:

- `beat_offset`: Position in the bar (same convention as bass patterns)
- `gm_note`: GM percussion note number (constants defined at top of file)
- `base_velocity`: MIDI velocity 1–127
- `vel_variation`: Random `±N` added each hit for a human feel

Then add the new style to `STYLE_KEYS` in `main.py`:

```python
STYLE_KEYS = {"1": "rock", "2": "blues", "3": "jazz",
              "4": "jazz_minor", "5": "minimal", "6": "reggae"}
```

### Phase 2: LLM Style Orchestration

Rather than manually switching styles with keyboard keys, a local LLM watching
the chord progression over a 4–8 bar window can make higher-level decisions:
when to build intensity, when to drop back, when to walk up to a chord change.
It also enables natural language control — typing "play something more funky" or
"lay back on the drums" rather than pressing a key.

The planned approach uses **Ollama** running **Llama 3.1 8B (Q4_K_M)**
(approximately 5GB VRAM). The LLM receives a structured prompt every 4 bars:

```
Current key: A minor, confidence: 0.82
Tempo: 118 BPM, confidence: 0.91
Last 4 bars: chords implied A-C-G-E
Style currently: minimal

Suggest: bass_style, drum_style, feel
```

With the 4070 Super (12GB VRAM), inference takes well under a bar at 118 BPM.

### Phase 3: Guitar Audio Input

For players without a MIDI guitar pickup, **basic-pitch** (Spotify's neural
pitch detector) can convert guitar audio to MIDI events in real time:

```bash
pip install basic-pitch
```

An audio capture thread reads from the system microphone or a USB audio
interface and feeds short windows (~50ms) into basic-pitch. The resulting MIDI
events feed into the same `MidiBuffer` / `TempoTracker` pipeline as the MIDI
input path, so no other changes are required downstream.

Recommended guitar-to-computer path for low latency:
- USB audio interface (Focusrite Scarlett Solo, etc.) with ALSA at 128
  samples (~3ms at 44.1kHz)
- Or a MIDI guitar pickup (Roland GK-3, Fishman TriplePlay) feeding MIDI
  directly — lower latency, no pitch detection needed

---

## Hardware Notes

This system was developed and tested with:

- **NVIDIA RTX 4070 Super (12GB VRAM)** — not currently used; the entire
  pipeline is CPU-bound and lightweight. The GPU becomes relevant in Phase 2
  (Ollama LLM inference, ~5GB VRAM).
- **Linux** with PulseAudio; change `driver="pulseaudio"` to `driver="alsa"` in
  `output/synth.py` for lower-latency ALSA direct access
- A USB MIDI controller (tested with X2mini)

---

## Troubleshooting

**No MIDI ports found**

Make sure your controller is connected and recognized by the OS:

```bash
aconnect -l        # list ALSA MIDI connections
amidi -l           # list raw MIDI devices
```

**Synth init failed: No soundfont found**

Install the GM soundfont package:

```bash
sudo apt install fluid-soundfont-gm
```

Or point to one explicitly:

```bash
python main.py --sf2 /path/to/your.sf2
```

**Band doesn't start**

The system waits for tempo confidence > 0.3. Play several notes with consistent
spacing — a simple steady rhythm for 2–3 seconds is enough. Press `s` to see
the current confidence scores.

**Audio crackles or stutters**

Switch from PulseAudio to ALSA in `output/synth.py`:

```python
self.fs.start(driver="alsa")
```

Or reduce system audio latency by running with real-time scheduling:

```bash
sudo chrt -f 50 python main.py
```

**Key detection is wrong**

- Play in the key for at least 4–8 seconds before the band kicks in
- Avoid playing notes outside the key early on — chromatic notes confuse the
  histogram before enough diatonic notes accumulate
- Press `s` to see the confidence score; below 0.5 means the system is uncertain
- The exponential decay means recent notes dominate, so playing firmly in a new
  key for 3–4 seconds is usually enough to shift detection

**Wrong tempo / band playing at double or half speed**

The subdivision disambiguation logic biases toward 120 BPM. If you play at an
unusual tempo the system may lock onto a half or double subdivision. Try
playing a clear steady pulse for a few bars, or set the analysis window shorter
by editing `window_seconds` in `main.py`.

**SDL3 warning in terminal**

```
fluidsynth: warning: SDL3 not initialized, SDL3 audio driver won't be usable.
```

This is harmless. FluidSynth was compiled with optional SDL3 support but falls
back to PulseAudio automatically. The warning can be ignored.
