#!/usr/bin/env python3
import os
import time
import signal
import threading
from collections import deque
from datetime import datetime
from typing import Optional, Set, Deque, Tuple, Dict

import mido
from mido import MidiFile, MidiTrack, MetaMessage, Message

# ---------------- Configuration ----------------
DEVICE_NAME_SUBSTRING: Optional[str] = None  # None = first available device
IDLE_TIMEOUT_SECONDS = 60.0
SCAN_INTERVAL_SECONDS = 2.0
TICKS_PER_BEAT = 480
DEFAULT_TEMPO_US_PER_BEAT = 500000  # 120 BPM
RECORDINGS_DIR = os.path.join(os.getcwd(), "recordings")

# --- Control chords ---
# Top 3 white keys on an 88-key: A7=105, B7=107, C8=108
TRIGGER_SPLIT_NOTES: Set[int] = {105, 107, 108}
# Bottom 3 white-ish keys on an 88-key: A0=21, B0=23, C1=24
TRIGGER_PAUSE_NOTES: Set[int] = {21, 23, 24}

CHORD_WINDOW_SECONDS = 0.150     # max interval between the 3 note_on events
SUPPRESS_AFTER_TRIGGER_SECONDS = 2.0  # ignore these notes' ON/OFF briefly
# ------------------------------------------------

class Recorder:
    def __init__(self):
        self.mid: Optional[MidiFile] = None
        self.track: Optional[MidiTrack] = None
        self.last_event_real: Optional[float] = None
        self.current_tempo = DEFAULT_TEMPO_US_PER_BEAT
        self.filename: Optional[str] = None
        self._lock = threading.RLock()

    def _now(self) -> float:
        return time.monotonic()

    def _ensure_folder(self):
        os.makedirs(RECORDINGS_DIR, exist_ok=True)

    def _new_filename(self) -> str:
        return os.path.join(RECORDINGS_DIR, datetime.now().strftime("REC%Y%m%d_%H%M%S.mid"))

    def start(self):
        with self._lock:
            if self.mid is not None:
                return
            self._ensure_folder()
            self.mid = MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)
            self.track = MidiTrack()
            self.mid.tracks.append(self.track)
            self.track.append(MetaMessage('set_tempo', tempo=self.current_tempo, time=0))
            self.track.append(MetaMessage('track_name', name='Live Recording', time=0))
            self.last_event_real = self._now()
            self.filename = self._new_filename()
            print(f"[REC] Started: {os.path.basename(self.filename)}")

    def _seconds_to_ticks(self, seconds: float) -> int:
        return max(0, int(round(mido.second2tick(seconds, TICKS_PER_BEAT, self.current_tempo))))

    def _append_with_delta(self, msg: Message):
        now = self._now()
        delta_sec = 0.0 if self.last_event_real is None else (now - self.last_event_real)
        self.last_event_real = now
        self.track.append(msg.copy(time=self._seconds_to_ticks(delta_sec)))

    def feed(self, msg: Message):
        with self._lock:
            if self.mid is None:
                print("[REC] First activity detected → starting new recording")
                self.start()
            if msg.type == 'set_tempo' and hasattr(msg, 'tempo'):
                self._append_with_delta(msg)
                self.current_tempo = msg.tempo
            else:
                self._append_with_delta(msg)

    def time_since_last_event(self) -> Optional[float]:
        with self._lock:
            return None if self.last_event_real is None else (self._now() - self.last_event_real)

    def stop_if_idle(self, idle_seconds: float) -> bool:
        with self._lock:
            if self.mid is None or self.last_event_real is None:
                return False
            elapsed = self._now() - self.last_event_real
            if elapsed < idle_seconds:
                return False
            self._close_with_delta(elapsed, reason=f"idle {int(elapsed)}s")
            return True

    def split_now(self):
        """Close immediately (used by split-on-chord)."""
        with self._lock:
            if self.mid is None:
                return
            elapsed = 0.0 if self.last_event_real is None else (self._now() - self.last_event_real)
            self._close_with_delta(elapsed, reason="split")

    def _close_with_delta(self, elapsed: float, reason: str):
        if self.mid is None:
            return
        self.track.append(MetaMessage('end_of_track', time=self._seconds_to_ticks(elapsed)))
        tmp = self.filename + ".tmp"
        self.mid.save(tmp)
        os.replace(tmp, self.filename)
        print(f"[REC] Closed ({reason}): {os.path.basename(self.filename)}")
        # Reset
        self.mid = None
        self.track = None
        self.last_event_real = None
        self.filename = None
        self.current_tempo = DEFAULT_TEMPO_US_PER_BEAT

    def force_close(self):
        with self._lock:
            if self.mid is None:
                return
            self.track.append(MetaMessage('end_of_track', time=0))
            tmp = self.filename + ".tmp"
            self.mid.save(tmp)
            os.replace(tmp, self.filename)
            print(f"[REC] Closed (forced): {os.path.basename(self.filename)}")
            self.mid = None
            self.track = None
            self.last_event_real = None
            self.filename = None
            self.current_tempo = DEFAULT_TEMPO_US_PER_BEAT


class ChordDetector:
    """
    Detects control chords within CHORD_WINDOW_SECONDS.
    - Suppresses those notes (and their note_offs briefly) so they are NOT recorded.
    Returns actions for split/pause toggles.
    """
    def __init__(self):
        self.recent_on: Deque[Tuple[float, int]] = deque()
        self.suppressed_until: Dict[int, float] = {}

    def _now(self) -> float:
        return time.monotonic()

    def _clean_recent(self):
        cutoff = self._now() - CHORD_WINDOW_SECONDS
        while self.recent_on and self.recent_on[0][0] < cutoff:
            self.recent_on.popleft()

    def _clean_suppressed(self):
        now = self._now()
        for n in [n for n, t in self.suppressed_until.items() if t <= now]:
            self.suppressed_until.pop(n, None)

    def _arm_suppression(self, notes: Set[int]):
        expiry = self._now() + SUPPRESS_AFTER_TRIGGER_SECONDS
        for n in notes:
            self.suppressed_until[n] = expiry

    def process(self, msg: Message) -> Tuple[bool, bool, bool]:
        """
        Returns (do_split, toggle_pause, suppress_this_msg)
        """
        self._clean_suppressed()

        # Already suppressed?
        if msg.type in ('note_on', 'note_off') and hasattr(msg, 'note'):
            if msg.note in self.suppressed_until:
                return (False, False, True)

        # Only detect on note_on with velocity > 0
        if msg.type == 'note_on' and getattr(msg, 'velocity', 0) > 0:
            self._clean_recent()
            self.recent_on.append((self._now(), msg.note))

            notes_seen = {n for (_, n) in self.recent_on}
            # Check split chord
            if TRIGGER_SPLIT_NOTES.issubset(notes_seen):
                self._arm_suppression(TRIGGER_SPLIT_NOTES)
                self.recent_on.clear()
                return (True, False, True)

            # Check pause chord
            if TRIGGER_PAUSE_NOTES.issubset(notes_seen):
                self._arm_suppression(TRIGGER_PAUSE_NOTES)
                self.recent_on.clear()
                return (False, True, True)

        return (False, False, False)


def pick_input_port() -> Optional[str]:
    names = mido.get_input_names()
    if not names:
        return None
    if DEVICE_NAME_SUBSTRING:
        for n in names:
            if DEVICE_NAME_SUBSTRING.lower() in n.lower():
                return n
        return None
    return names[0]


def monitor_loop():
    recorder = Recorder()
    detector = ChordDetector()
    stop_flag = threading.Event()
    paused = threading.Event()  # set = paused, clear = recording-enabled
    current_port_name = {'name': None}
    current_port_obj = {'port': None}

    def handle_sig(_signum, _frame):
        print("\n[SYS] Signal received, shutting down…")
        stop_flag.set()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    def on_msg(msg: Message):
        # Control chords first
        do_split, toggle_pause, suppress = detector.process(msg)

        if toggle_pause:
            if not paused.is_set():
                # Pausing: close any current recording and block new recordings
                recorder.split_now()
                paused.set()
                print("[CTL] Paused (press bottom A0-B0-C1 again to resume)")
            else:
                paused.clear()
                print("[CTL] Resumed")
            return  # chord notes suppressed

        if do_split:
            recorder.split_now()
            print("[CTL] Split triggered by chord (top A7-B7-C8)")
            return  # chord notes suppressed

        if suppress:
            return

        # If paused, ignore all normal MIDI
        if paused.is_set():
            return

        # Normal path: record the message
        recorder.feed(msg)

    def idle_watchdog():
        while not stop_flag.is_set():
            try:
                # While paused nothing records anyway; stop_if_idle no-ops when not recording
                recorder.stop_if_idle(IDLE_TIMEOUT_SECONDS)
            except Exception as e:
                print(f"[ERR] Watchdog: {e}")
            stop_flag.wait(0.5)

    threading.Thread(target=idle_watchdog, daemon=True).start()

    print("[SYS] Starting device monitor…")
    while not stop_flag.is_set():
        if current_port_obj['port'] is None:
            port_name = pick_input_port()
            if not port_name:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue
            try:
                # mido.set_backend('mido.backends.rtmidi')  # uncomment to pin backend
                port = mido.open_input(port_name, callback=on_msg)
                current_port_obj['port'] = port
                current_port_name['name'] = port_name
                print(f"[DEV] Listening on: {port_name}")
            except Exception as e:
                print(f"[ERR] Could not open '{port_name}': {e}")
                time.sleep(1.0)
                continue

        # Confirm device still present
        try:
            names = set(mido.get_input_names())
        except Exception:
            names = set()
        if current_port_name['name'] not in names:
            print("[DEV] Device disconnected.")
            try:
                if current_port_obj['port'] is not None:
                    current_port_obj['port'].close()
            except Exception:
                pass
            current_port_obj['port'] = None
            current_port_name['name'] = None
            recorder.force_close()
            time.sleep(1.0)
            continue

        time.sleep(0.5)

    # Clean shutdown
    try:
        if current_port_obj['port'] is not None:
            current_port_obj['port'].close()
    except Exception:
        pass
    recorder.force_close()
    print("[SYS] Exited.")


if __name__ == "__main__":
    ports = mido.get_input_names()
    if ports:
        print("[INFO] Available MIDI input ports:")
        for p in ports:
            print(f"  - {p}")
    else:
        print("[INFO] No MIDI input ports detected yet. Waiting…")
    monitor_loop()
