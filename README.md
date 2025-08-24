# MIDI Auto Recorder 🎹

A Python utility that continuously monitors a connected MIDI input device and automatically records activity into `.mid` files.  

- Sleeps quietly while no device is present.  
- Starts recording on the first detected MIDI message.  
- Automatically stops (closes the file) after **60 seconds of inactivity**.  
- Creates new files with names like `RECYYYYMMDD_HHMMSS.mid` in the `recordings/` folder.  
- Supports **manual split** and **pause/resume** via special chords on an 88-key piano:  

  - **Split chord** → **A7 (105), B7 (107), C8 (108)** closes the current recording immediately and begins a new one with the next activity.  
  - **Pause chord** → **A0 (21), B0 (23), C1 (24)** toggles pause/resume. While paused, no MIDI is recorded.  

Control chords are not written into the `.mid` files.

---

## Features

- ✅ Continuous background monitoring  
- ✅ Auto-detects device availability (USB plug/unplug safe)  
- ✅ Auto file naming with timestamp  
- ✅ Idle cutoff handling (60s by default)  
- ✅ Manual split with top-end chord  
- ✅ Pause/resume with bottom-end chord  
- ✅ Cross-platform (Windows, macOS, Linux) via [mido](https://mido.readthedocs.io) + [python-rtmidi](https://pypi.org/project/python-rtmidi/)  

---

## Installation

Requires Python 3.8+.

```bash
git clone https://github.com/yourusername/midi-auto-recorder.git
cd midi-auto-recorder
pip install -r requirements.txt
