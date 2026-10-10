# AkiMelody

A self-hosted music player web app that searches, streams, and downloads music from YouTube Music — 

![Python](https://img.shields.io/badge/Python-3.x-blue) ![Flask](https://img.shields.io/badge/Flask-3.x-green) ![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Features

- **Instant Search & Play** — Search across YouTube Music and iTunes, click a result and it plays immediately
- **Real-Time Streaming** — Audio streams directly from YouTube via yt-dlp (no full download required)
- **Offline Caching** — Tracks are downloaded as MP3s (192kbps) for instant repeat playback
- **Favorites** — Like tracks with the heart button; persisted to disk and auto-downloaded
- **Playlists & Albums** — Create playlists, save full albums, reorder tracks
- **Artist Profiles** — View artist bios (from Wikipedia), discography grouped by album
- **Card & List Layouts** — Toggle between a compact list view and a 3D-tilted card grid
- **3D Visual Effects** — Mouse-tracking tilt, specular shine, ambient glow on album art
- **Glassmorphism UI** — Frosted glass panels, animated backgrounds, smooth transitions

---

## Quick Start

### Prerequisites

- **Python 3.8+**
- **FFmpeg** — Required for audio conversion. Install via:
  - Windows: [ffmpeg.org](https://ffmpeg.org/download.html) (add to PATH)
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`


## License

MIT
