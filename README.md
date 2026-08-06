# AkiMelody (秋メロディ) v3.0

A self-hosted music player web app that searches, streams, and downloads music from YouTube Music and iTunes — wrapped in a dark glassmorphism UI with 3D card effects and dynamic album art.

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
- **Dark Glassmorphism UI** — Frosted glass panels, animated backgrounds, smooth transitions

---

## Quick Start

### Prerequisites

- **Python 3.8+**
- **FFmpeg** — Required for audio conversion. Install via:
  - Windows: [ffmpeg.org](https://ffmpeg.org/download.html) (add to PATH)
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`

### Installation

```bash
# Clone or download the project
cd Aki

# Install Python dependencies
pip install -r requirements.txt

# Launch (Windows)
Launch.bat

# Or launch manually
python app.py
```

The app opens at **http://localhost:5000**.

---

## How to Use

### Searching
1. Type a song, artist, or album name in the search bar (right panel)
2. Press **Enter** to search
3. Use the **filter dropdown** to narrow results:
   - **All** — iTunes search (instant play on first result)
   - **Track** — YouTube Music songs (instant play)
   - **Album** — YouTube Music albums (click to view tracklist)
   - **Artist** — YouTube Music artists (click to view profile)

### Playing Music
- Click any track in the queue, search results, or playlist to play
- Use the player controls: **Shuffle**, **Previous**, **Play/Pause**, **Next**, **Repeat**
- Click the **seek bar** to jump to any point in the track
- Adjust volume via **Settings**

### Favorites
- Click the **heart icon** on the now-playing bar to like/unlike a track
- Switch to the **Favorites** view via the navigation bar to see all liked tracks
- Favorites are auto-downloaded in the background for offline playback

### Playlists
- Click the **+** button on any track to add it to a playlist
- Select an existing playlist or create a new one from the dropdown
- Access all playlists from the **Library** view
- Tracks download in the background when added

### Saving Albums
- Open an album (from search results or artist profile)
- Click **Save Album** to download all tracks as a named playlist
- Saved albums appear in the Library with album art

### Artist Profiles
- Click any **artist name** throughout the app to open their profile
- View their bio, discography grouped by album, and top tracks
- Click album headers to navigate into the album view

### Layout Modes
- Open **Settings** and toggle between **Card** and **List** mode
- Card mode shows a responsive grid with 3D tilt effects
- List mode shows compact horizontal rows
- The queue view always uses list mode

---

## Project Structure

```
Aki/
├── app.py                     # Flask backend (all API routes + helpers)
├── requirements.txt           # Python dependencies
├── settings.json              # UI layout preference
├── favorites.json             # Liked tracks data
├── Launch.bat                 # Windows one-click launcher
├── templates/
│   └── player.html            # Single-page frontend (HTML + CSS + JS)
├── SAVED/                     # Downloaded track cache
│   ├── {tid}.mp3              # Audio files
│   └── {tid}.jpg              # Album art
└── music_library/
    └── playlists/             # User playlists and saved albums
        ├── My Playlist/
        │   ├── {tid}.mp3
        │   ├── {tid}.jpg
        │   └── {tid}.meta.json
        └── Album Name/
            ├── album.json     # Album metadata marker
            ├── 01 - {tid}.mp3 # Numbered tracks
            └── ...
```

### Track IDs (tid)
Every track is identified by an MD5 hash of `"{name}_{artist}"` (lowercased). This hash is used as the filename for all cached audio, art, and metadata files.

---

## Configuration

| File | Purpose |
|------|---------|
| `settings.json` | UI layout mode (`"card"` or `"list"`) |
| `favorites.json` | Array of liked track objects |
| `SAVED/` | Cached audio and art for individual tracks |
| `music_library/playlists/` | Playlist directories with audio, art, and metadata |

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | Python, Flask |
| Music Search | ytmusicapi (YouTube Music), iTunes Search API |
| Audio Streaming | yt-dlp (YouTube extraction) |
| Audio Conversion | FFmpeg (via yt-dlp postprocessor) |
| Artist Bios | Wikipedia API |
| Frontend | Vanilla HTML/CSS/JS (no frameworks) |
| Fonts | M PLUS Rounded 1c, Outfit (Google Fonts) |
| Icons | Font Awesome 6.5.0 |

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Serve the player UI |
| GET | `/api/search?q=&filter=` | Search for tracks/albums/artists |
| GET | `/api/stream?q=&tid=&vid=` | Get a playable audio URL |
| GET | `/api/favorites` | Load liked tracks |
| POST | `/api/save_favorites` | Save favorites + trigger downloads |
| GET | `/api/artist?name=&browseId=` | Artist profile with bio and tracks |
| GET | `/api/album?albumId=` | Album details and tracklist |
| GET | `/api/playlists` | List all playlists |
| POST | `/api/playlists/create` | Create a new playlist |
| POST | `/api/playlists/add` | Add a track to a playlist |
| GET | `/api/playlists/tracks?name=` | List tracks in a playlist |
| GET | `/api/settings` | Load settings |
| POST | `/api/settings/toggle_layout` | Toggle card/list mode |

---

## License

MIT
