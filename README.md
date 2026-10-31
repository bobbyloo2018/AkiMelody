# AkiMelody

**A feature-rich, art-first approach to music streaming.**

AkiMelody puts album art at the center of listening. Search anything, hit play, and the whole app shifts around the music: colors, glow, and motion pulled straight from the artwork.

![Python](https://img.shields.io/badge/Python-3.8+-blue) ![Flask](https://img.shields.io/badge/Flask-3.x-green) ![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Android-lightgrey) ![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Highlights

- **Art-first design** — 3D-tilting album cards, ambient glow, and a background visualizer themed from each cover
- **Search and play instantly** — YouTube Music and iTunes results, streamed in real time
- **Synced lyrics** — follow along line by line
- **Your library** — favorites, playlists, saved albums, and artist pages with bios
- **Offline ready** — tracks cache locally for instant replays
- **Bring your music with you** — import Spotify Likes and link your YouTube Music account
- **Themes** — community themes, dark and light modes, list or card layouts
- **Media keys** — play, pause, and skip from your keyboard

---

## Quick Start (Desktop)

You'll need **Python 3.8+** and **FFmpeg** on your PATH.

```bash
git clone https://github.com/bobbyloo2018/AkiMelody.git
cd AkiMelody
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

Prefer an installer? `python build.py --installer` builds a Windows setup file (needs Inno Setup 6).

---

## Android

An Android version is on the way, sharing the same backend with a mobile player built for touch. Stay tuned.

---

## License

MIT
