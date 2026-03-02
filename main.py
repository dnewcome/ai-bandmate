"""
AI Bandmate — Phase 1
Listens to MIDI input, detects key/tempo, generates a bass line.

Usage:
    python main.py [--style rock|blues|jazz|minimal] [--port "My MIDI Device"]

Controls (keyboard while running):
    q  — quit
    s  — print current analysis state
    1/2/3/4/5 — change style (rock/blues/jazz/jazz_minor/minimal)
"""
import argparse
import sys
import time
import threading
import select

import rtmidi

from analysis.midi_buffer import MidiBuffer
from analysis.key_detector import detect_key
from analysis.tempo_tracker import TempoTracker
from generators.bass_generator import BassGenerator, STYLE_PATTERN_MAP
from generators.drum_generator import DrumGenerator
from output.synth import Synth

LEAD_CHANNEL = 0
BASS_CHANNEL = 1
DRUM_CHANNEL = 9
ANALYSIS_INTERVAL = 1.0  # re-analyze every N seconds

LEAD_INSTRUMENTS = {
    "1": ("piano",        "Piano"),
    "2": ("clean_guitar", "Clean Electric Guitar"),
}
STYLE_KEYS = {"1": "rock", "2": "blues", "3": "jazz", "4": "jazz_minor", "5": "minimal"}


def list_midi_ports():
    midi_in = rtmidi.MidiIn()
    ports = midi_in.get_ports()
    print("Available MIDI input ports:")
    for i, name in enumerate(ports):
        print(f"  [{i}] {name}")
    return ports


def open_midi_port(port_name: str | None) -> rtmidi.MidiIn:
    midi_in = rtmidi.MidiIn()
    ports = midi_in.get_ports()
    if not ports:
        print("No MIDI input ports found.")
        sys.exit(1)

    if port_name:
        # --port flag: match by name substring
        matches = [i for i, p in enumerate(ports) if port_name.lower() in p.lower()]
        if not matches:
            print(f"Port '{port_name}' not found. Available:")
            for i, p in enumerate(ports):
                print(f"  [{i}] {p}")
            sys.exit(1)
        idx = matches[0]
    elif len(ports) == 1:
        # Only one port available — use it automatically
        idx = 0
    else:
        # Multiple ports — prompt the user to choose
        print("Available MIDI input ports:")
        for i, p in enumerate(ports):
            print(f"  [{i}] {p}")
        while True:
            try:
                choice = input(f"Select port [0-{len(ports)-1}]: ").strip()
                idx = int(choice)
                if 0 <= idx < len(ports):
                    break
                print(f"  Please enter a number between 0 and {len(ports)-1}.")
            except (ValueError, EOFError):
                print("  Invalid input.")

    print(f"Opening MIDI port: {ports[idx]}")
    midi_in.open_port(idx)
    return midi_in


def select_lead_instrument() -> str:
    print("\nSelect your instrument:")
    for key, (_, label) in LEAD_INSTRUMENTS.items():
        print(f"  [{key}] {label}")
    while True:
        try:
            choice = input(f"Choice [1-{len(LEAD_INSTRUMENTS)}]: ").strip()
            if choice in LEAD_INSTRUMENTS:
                name, label = LEAD_INSTRUMENTS[choice]
                print(f"  -> {label}")
                return name
            print(f"  Please enter a number between 1 and {len(LEAD_INSTRUMENTS)}.")
        except (ValueError, EOFError):
            print("  Invalid input.")


def main():
    parser = argparse.ArgumentParser(description="AI Bandmate Phase 1")
    parser.add_argument("--style", default="rock", choices=list(STYLE_PATTERN_MAP.keys()))
    parser.add_argument("--port", default=None, help="MIDI port name substring")
    parser.add_argument("--instrument", default=None, choices=["piano", "clean_guitar"],
                        help="Lead instrument (skips prompt)")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--sf2", default=None, help="Path to .sf2 soundfont")
    args = parser.parse_args()

    if args.list_ports:
        list_midi_ports()
        return

    # --- Instrument selection ---
    lead_instrument = args.instrument or select_lead_instrument()

    # --- Init components ---
    buffer = MidiBuffer(window_seconds=8.0)
    tempo = TempoTracker()
    bass_gen = BassGenerator(style=args.style)
    drum_gen = DrumGenerator(style=args.style)

    print("Initializing synth...")
    try:
        synth = Synth(sf2_path=args.sf2)
        print(f"  Synth created, sfid={synth.sfid}")
        synth.setup_channel(LEAD_CHANNEL, lead_instrument)
        print(f"  Lead channel {LEAD_CHANNEL} ({lead_instrument}) ready")
        synth.setup_channel(BASS_CHANNEL, "bass_finger")
        print(f"  Bass channel {BASS_CHANNEL} ready")
        synth.setup_channel(DRUM_CHANNEL, "drums")
        print(f"  Drum channel {DRUM_CHANNEL} ready")
        # Quick test tone so we know audio is alive
        synth.note_on(LEAD_CHANNEL, 60, 60)
        import time as _t; _t.sleep(0.3); synth.note_off(LEAD_CHANNEL, 60)
        print("  Audio OK")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Synth init failed: {e}")
        print("Running without audio output (analysis only).")
        synth = None

    def bass_callback(pitch: int, velocity: int, on: bool):
        if synth:
            if on:
                synth.note_on(BASS_CHANNEL, pitch, velocity)
            else:
                synth.note_off(BASS_CHANNEL, pitch)

    def drum_callback(pitch: int, velocity: int, on: bool):
        if synth:
            if on:
                synth.note_on(DRUM_CHANNEL, pitch, velocity)
            else:
                synth.note_off(DRUM_CHANNEL, pitch)

    bass_gen.set_note_callback(bass_callback)
    drum_gen.set_note_callback(drum_callback)

    # --- MIDI input handler ---
    midi_in = open_midi_port(args.port)

    current_key = {"root": "C", "mode": "major", "label": "C major", "root_midi": 0}
    bass_started = False

    def midi_message_handler(message, _data=None):
        nonlocal bass_started
        msg, _delta = message
        status = msg[0] & 0xF0

        if status == 0x90 and len(msg) >= 3 and msg[2] > 0:  # note on
            pitch, velocity = msg[1], msg[2]
            # Play through to lead instrument
            if synth:
                synth.note_on(LEAD_CHANNEL, pitch, velocity)
            # Feed analysis pipeline
            buffer.add(pitch, velocity)
            tempo.on_note()

            if not bass_started and tempo.confidence > 0.3:
                bass_started = True
                bass_gen.start_loop(
                    root_midi=current_key["root_midi"],
                    mode=current_key["mode"],
                    bpm=tempo.bpm,
                )
                drum_gen.start_loop(bpm=tempo.bpm)
                print(f"Band started — {current_key['label']} @ {tempo.bpm} BPM")

        elif (status == 0x80 or (status == 0x90 and msg[2] == 0)) and len(msg) >= 2:  # note off
            pitch = msg[1]
            if synth:
                synth.note_off(LEAD_CHANNEL, pitch)

    midi_in.set_callback(midi_message_handler)

    # --- Analysis loop (background thread) ---
    def analysis_loop():
        last_printed_key = ""
        last_printed_bpm = 0.0

        while True:
            time.sleep(ANALYSIS_INTERVAL)

            # Key detection — update whenever we have a reasonable estimate
            histogram = buffer.pitch_class_histogram()
            key = detect_key(histogram)
            if key["confidence"] > 0.2:
                current_key.update(key)

            # Always push latest key + tempo to both generators
            bass_gen.update_params(
                root_midi=current_key["root_midi"],
                mode=current_key["mode"],
                bpm=tempo.bpm,
            )
            drum_gen.update_params(bpm=tempo.bpm)

            # Print when something meaningful has changed
            key_changed = current_key["label"] != last_printed_key
            bpm_changed = abs(tempo.bpm - last_printed_bpm) > 3.0
            if key_changed or bpm_changed:
                print(
                    f"  key={current_key['label']} ({current_key.get('confidence', 0):.2f})"
                    f"  bpm={tempo.bpm} ({tempo.confidence:.2f})"
                )
                last_printed_key = current_key["label"]
                last_printed_bpm = tempo.bpm

    analysis_thread = threading.Thread(target=analysis_loop, daemon=True)
    analysis_thread.start()

    # --- Main loop (keyboard control) ---
    print("\nAI Bandmate running. Play something!")
    print("Keys: [q]uit  [s]tatus  [1-5] change style")
    print(f"Styles: {STYLE_KEYS}")

    try:
        while True:
            # Non-blocking stdin check
            if select.select([sys.stdin], [], [], 0.1)[0]:
                key = sys.stdin.read(1)
                if key == 'q':
                    break
                elif key == 's':
                    print(f"\nKey: {current_key['label']} (conf={current_key.get('confidence', 0):.2f})")
                    print(f"BPM: {tempo.bpm} (conf={tempo.confidence:.2f})")
                    print(f"Style: {bass_gen.style}")
                elif key in STYLE_KEYS:
                    style = STYLE_KEYS[key]
                    bass_gen.style = style
                    drum_gen.style = style
                    print(f"Style -> {style}")
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down...")
        bass_gen.stop()
        drum_gen.stop()
        midi_in.close_port()
        if synth:
            synth.close()


if __name__ == "__main__":
    main()
