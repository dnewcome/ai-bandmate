# AI Bandmate

A real-time AI accompaniment system. Play into a MIDI controller and the system
listens to your notes, figures out what key and tempo you are in, and generates
a matching bass line that follows you as you play — changing keys and tempo on
the fly as you move through a song.

This is Phase 1 of a larger project. The current system uses rule-based
generation built on live music analysis. Future phases add neural drum
generation (Magenta GrooVAE), LLM-driven style orchestration (Ollama), and
audio input from a guitar pickup.

---

## Table of Contents

- [How It Works](#how-it-works)
  - [Signal Flow](#signal-flow)
  - [MIDI Buffer](#midi-buffer)
  - [Key Detection](#key-detection)
  - [Tempo Tracking](#tempo-tracking)
  - [Bass Generation](#bass-generation)
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
  - [Phase 2: Drum Generation](#phase-2-drum-generation)
  - [Phase 3: LLM Style Orchestration](#phase-3-llm-style-orchestration)
  - [Phase 4: Guitar Audio Input](#phase-4-guitar-audio-input)
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
      │  pitch class histogram (every 2s)      │  note-on timestamps
      ▼                                        ▼
 KeyDetector                            TempoTracker
      │                                        │
      │  root note + mode                      │  BPM + confidence
      └──────────────────┬─────────────────────┘
                         │
                         ▼
                   BassGenerator
                  (background thread)
                         │
                         │  MIDI note-on / note-off
                         ▼
                       Synth
                   (FluidSynth)
                         │
                         ▼
                   Audio Output
               (PulseAudio / ALSA)
```

Every incoming MIDI note feeds two subsystems simultaneously: the `MidiBuffer`
accumulates notes for key analysis, and the `TempoTracker` records the timing
of each onset for BPM estimation. A background analysis thread re-runs key
detection every two seconds and updates the bass generator. The bass generator
runs in its own thread, looping bar by bar, picking up parameter changes at
each bar boundary so transitions are always musically clean.

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

Using velocity weighting means harder-played notes (roots, chord tones) have
more influence on key detection than light embellishments.

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
(range -1 to 1). Values above ~0.6 are generally reliable. The system only
acts on detections above 0.4.

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
4. Keep a rolling window of up to 16 recent IOIs
5. Take the **median** of the window to get a robust estimate robust to outliers
6. Try three subdivision hypotheses: the median IOI might represent a half note
   (×2), a quarter note (×1), or an eighth note (×0.5)
7. Of the valid candidates, pick the one closest to 120 BPM (the most common
   musical tempo, used as a Bayesian prior)
8. Compute **confidence** as `1 - (std / median)` — lower variance among recent
   IOIs means higher confidence

The median is used rather than the mean because a single long gap (e.g. you
pause between phrases) would skew a mean significantly but barely moves the
median.

Confidence crosses 0.3 fairly quickly once you play a few evenly-spaced notes,
which is the threshold used to start the bass generator.

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

#### Style Mapping

Styles are aliases for patterns, making it easy to extend:

```python
STYLE_PATTERN_MAP = {
    "rock":       "root_fifth",
    "blues":      "blues",
    "jazz":       "walking_major",
    "jazz_minor": "walking_minor",
    "minimal":    "root_on_one",
}
```

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

**General MIDI instrument numbers** used:

| Name | GM Program | Description |
|---|---|---|
| `bass_finger` | 33 | Fingered electric bass |
| `bass_pick` | 34 | Picked electric bass |
| `bass_fretless` | 35 | Fretless bass |
| `upright_bass` | 32 | Acoustic upright bass |
| `synth_bass` | 38 | Synth bass 1 |

The bass is placed on **MIDI channel 1**. MIDI channel 9 (0-indexed) is
reserved by the General MIDI standard for drums and will be used in Phase 2.

If the synth fails to initialize (missing soundfont, audio driver issue), the
system falls back gracefully and continues running in analysis-only mode,
printing state to the terminal.

---

### Threading Model

The system runs four concurrent threads:

| Thread | Role |
|---|---|
| Main thread | Keyboard input polling via `select()`, startup/shutdown |
| MIDI callback thread | Managed by `python-rtmidi`; calls `midi_message_handler` on each message |
| Analysis thread | Sleeps 2s, recomputes key + BPM, calls `bass_gen.update_params()` |
| Bass playback thread | Loops bar-by-bar, sleeps precisely to hit note timings |
| Note-off threads | One short-lived thread per note, fires the note-off after duration |

State shared between threads (`current_key`, `bass_started`) uses simple
Python assignment which is safe for these types due to the GIL. The bass
generator's `_next_root / _next_mode / _next_bpm` fields are written by the
analysis thread and read by the playback thread at bar boundaries — no mutex
is needed because a slightly stale read just means the change takes one extra
bar, which is musically fine.

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

Python 3.11 or newer is required (uses `list[T]` type hints without `from
__future__ import annotations` and `X | Y` union syntax).

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
  [0] Arturia MiniLab mkII 0
  [1] Midi Through Port-0
```

### Run

```bash
# Use the first available MIDI port, rock style
python main.py

# Specify style
python main.py --style blues

# Specify MIDI port by name (partial match, case-insensitive)
python main.py --port "MiniLab" --style jazz

# Specify a custom soundfont
python main.py --sf2 ~/soundfonts/GeneralUser.sf2
```

The bass will not start immediately. The system waits until the tempo tracker
has enough confidence (after roughly 4–8 evenly-spaced notes) before engaging
the bass. This prevents it from starting in the wrong key or at the wrong tempo.

### Keyboard Controls

While the program is running, single keypresses control the system:

| Key | Action |
|---|---|
| `s` | Print current key, BPM, confidence scores, and active style |
| `1` | Switch to **rock** style (root + fifth) |
| `2` | Switch to **blues** style (eighth-note root/fifth alternation) |
| `3` | Switch to **jazz** style (walking major: 1-3-5-7) |
| `4` | Switch to **jazz_minor** style (walking minor: 1-b3-5-8va) |
| `5` | Switch to **minimal** style (root on beat 1 only) |
| `q` | Quit cleanly |

Style changes take effect at the next bar boundary.

### Styles

| Style | Pattern | Best For |
|---|---|---|
| `rock` | Root on 1, fifth on 3 | Rock, pop, country |
| `blues` | Root/fifth on 8th notes | Blues, R&B, shuffle feels |
| `jazz` | Walking 1-3-5-7 | Jazz, swing, major-key tunes |
| `jazz_minor` | Walking 1-b3-5-octave | Minor jazz, soul, bossa |
| `minimal` | Root on beat 1 only | Sparse arrangements, ballads |

---

## Project Structure

```
ai-bandmate/
├── main.py                     Entry point and runtime loop
├── requirements.txt
├── analysis/
│   ├── __init__.py
│   ├── midi_buffer.py          Rolling note event window + chroma histogram
│   ├── key_detector.py         Krumhansl-Schmuckler key detection
│   └── tempo_tracker.py        IOI-based BPM estimation
├── generators/
│   ├── __init__.py
│   └── bass_generator.py       Rule-based bass patterns + playback thread
└── output/
    ├── __init__.py
    └── synth.py                FluidSynth wrapper
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
```

Then map it to a style name:

```python
STYLE_PATTERN_MAP["reggae"] = "reggae"
```

And add it to `STYLE_KEYS` in `main.py`:

```python
STYLE_KEYS = {"1": "rock", "2": "blues", "3": "jazz",
              "4": "jazz_minor", "5": "minimal", "6": "reggae"}
```

Each tuple is `(beat_offset, scale_degree, octave_shift, duration_beats)`:

- `beat_offset`: When in the 4/4 bar the note starts (0.0 = beat 1, 1.0 = beat
  2, 0.5 = the "and" of beat 1, etc.)
- `scale_degree`: 1=root, 2=second, 3=third, 4=fourth, 5=fifth, 6=sixth,
  7=seventh, 8=octave above root
- `octave_shift`: Shifts the note up or down by octaves (0 = bass octave 2,
  -1 = one octave lower, +1 = one octave higher)
- `duration_beats`: How long the note holds, in beats (0.9 ≈ slightly detached,
  0.45 ≈ walking feel, 0.25 ≈ staccato)

### Phase 2: Drum Generation

The next planned phase uses **Magenta GrooVAE**, a VAE-based model trained on
thousands of drum performances. It takes a simple pattern seed and the current
tempo and generates a humanized drum groove that matches the feel.

Setup will require:

```bash
pip install magenta
```

GrooVAE will run on the GPU via TensorFlow. With a 4070 Super (12GB), inference
takes under 50ms for a 2-bar pattern, which is fast enough for real-time use.
The drum output will go to MIDI channel 9 (the GM drums channel) in `Synth`.

### Phase 3: LLM Style Orchestration

Rather than manually switching styles with keyboard keys, a local LLM watching
the chord progression over a 4–8 bar window can make higher-level decisions:
when to build intensity, when to drop back, when to walk up to a chord change.

The planned approach uses **Ollama** running **Llama 3.1 8B (Q4_K_M)**
(approximately 5GB VRAM). The LLM receives a structured prompt every 4 bars:

```
Current key: A minor, confidence: 0.82
Tempo: 118 BPM, confidence: 0.91
Last 4 bars: chords implied A-C-G-E
Style currently: minimal

Suggest: bass_style, drum_intensity (0-10), feel
```

With the 4070 Super, the LLM and Magenta can both be in VRAM simultaneously
(~5GB + ~3GB = ~8GB, well within the 12GB budget).

### Phase 4: Guitar Audio Input

For players without a MIDI guitar pickup, **basic-pitch** (Spotify's neural
pitch detector) can convert guitar audio to MIDI events in real time:

```bash
pip install basic-pitch
```

An audio capture thread reads from the system microphone or a USB audio
interface and feeds short windows (~50ms) into basic-pitch. The resulting MIDI
events feed into the same `MidiBuffer` / `TempoTracker` pipeline as the MIDI
input path, so no other changes are required.

Recommended guitar-to-computer path for low latency:
- USB audio interface (Focusrite Scarlett Solo, etc.) with ASIO/ALSA at 128
  samples (~3ms at 44.1kHz)
- Or a MIDI guitar pickup (Roland GK-3, Fishman TriplePlay) feeding MIDI
  directly — lower latency, no pitch detection needed

---

## Hardware Notes

This system was developed and tested with:

- **NVIDIA RTX 4070 Super (12GB VRAM)** — sufficient for Ollama (Llama 3.1 8B
  Q4, ~5GB) and Magenta (TensorFlow, ~3GB) simultaneously
- **Linux** with PulseAudio; change `driver="pulseaudio"` to `driver="alsa"` in
  `output/synth.py` for lower-latency ALSA direct access
- A USB MIDI controller on any port

The current Phase 1 codebase uses no GPU at all — all computation is CPU-bound
and extremely lightweight. The GPU becomes relevant in Phase 2 and 3.

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

**Bass doesn't start**

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

- Play in the key for at least 4–8 seconds before the accompaniment kicks in
- Avoid playing notes outside the key early on — chromatic notes confuse the
  histogram before enough diatonic notes accumulate
- Press `s` to see the confidence score; below 0.5 means the system is uncertain
- The 8-second window means a sustained key change takes up to 8 seconds to
  fully register; playing emphatically in the new key speeds this up

**Wrong tempo / bass playing at double or half speed**

The subdivision disambiguation logic biases toward 120 BPM. If you play at an
unusual tempo the system may lock onto a half or double subdivision. Try
playing a clear steady pulse for a few bars, or temporarily set the analysis
window shorter by editing `window_seconds` in `main.py`.
