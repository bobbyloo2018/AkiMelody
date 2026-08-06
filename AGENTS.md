# AGENTS.md — AkiMelody (condensed)

Flask + vanilla JS music player over YouTube Music. No build system, framework, ORM, or DB. Filesystem storage only.

---

## Stack

| Layer | File |
|---|---|
| Backend (all routes + helpers) | `app.py` |
| Desktop frontend (single-file HTML+CSS+JS) | `templates/player.html` |
| Mobile frontend | `templates/mobile.html`, `static/js/mobile.js`, `static/css/mobile.css` |
| Entry / launcher | `Launch.bat` (runs `webview_launcher.py`), `server.py` (standalone API entry) |
| Shell | `webview_launcher.py` — pywebview (WebView2 / EdgeChromium) |
| Bundling | PyInstaller (`webview_launcher.py`, app.py frozen branch handles `templates`/`static` via `_MEIPASS` + `%LOCALAPPDATA%\AkiMelody` data dir) |

Storage: `favorites.json`, `settings.json`, `SAVED/{tid}.{mp3,jpg}`, `music_library/playlists/{Playlist}/{tid}.{mp3,jpg,meta.json}`.

---

## Shell architecture (`webview_launcher.py`)

Default runtime is **pywebview (WebView2 / EdgeChromium)**. Single Python process: Flask in a daemon thread on port 5000 + frameless/transparent `webview.create_window` at `http://127.0.0.1:5000`.

- **JS bridge**: `AkiApi` (js_api) exposes the bridge surface the renderer's `window.__aki__` / `window.__TAURI__` shim builds on. pywebview auto-serialises return values; `_`-prefixed attrs/methods are NOT exposed — **never store the window (or any non-callable .NET object) on a public js_api attr**: pywebview recurses `dir()` into every public non-callable attr and infinitely loops on WinForms `FontFamily.GenericSansSerif`/`SyncRoot` self-referencing properties (util.py `get_functions`).
- **Frontend shim**: `templates/player.html` right after `<body>` builds `window.__aki__` / `window.__TAURI__` from `window.pywebview.api` (polling `whenApiReady`, since pywebview injects JS only after load and fires `pywebviewready`). Marker `window.__AKI_WEBVIEW__` keeps the renderer on Flask URLs. Guards: `resolveArtUrl`, `nativeAudioUrl`.
- **Drag**: WebView2 ignores `-webkit-app-region`; pywebview drag via `webview.settings['DRAG_REGION_SELECTOR']='.title-bar-drag-zone'` (renderer's custom title bar).
- **Win32 reimpls**: media keys via `WH_KEYBOARD_LL` hook → `__akiPush('mediaKey', toggle|next|prev)` (hook swallows the VKs so WebView2's `mediaSession` handlers don't double-fire); theme watch loop → `__akiPush('nativeThemeChanged', ...)`; power-save via `SetThreadExecutionState`; autostart via HKCU Run key; clipboard via `CF_UNICODETEXT`; recycle-bin via `SHFileOperationW`; show-in-folder via `explorer /select,`.
- **Unsupported → stubs**: tray (`tray_*`), native drag-out (`start_drag`) return `{ok:false}`; `clear_cache`/`register_stream_url` no-ops. `spotify_autosync` stays on the Flask route.
- **YouTube auth**: `start_youtube_login` navigates to music.youtube.com, polls `get_cookies()` for SID/SSID/HSID/`__Secure-1PSID`, writes Netscape `cookies.txt`, returns to `/?yt_login=1` (120s timeout → `?yt_login=timeout`).
- **Persistence**: `webview.start(private_mode=False, storage_path=%LOCALAPPDATA%\AkiMelody\webview)` — keeps localStorage/IndexedDB (theme/volume/queue, lyrics) across launches. `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` disables background-timer/renderer throttling and sets `--js-flags=--max-old-space-size=288 --expose-gc` (visualizer/ontimeupdate must keep running when hidden). Flask binds `127.0.0.1`; a CSP is injected via `after_request`.
- `Launch.bat`: checks Python + deps, then runs `python webview_launcher.py`.
- Deps: `pywebview>=6.0.0`, `pythonnet>=3.0.0`, `darkdetect>=0.8.0`.

---

## Track ID — universal key

```python
tid = md5(f"{name}_{artist}".lower().strip())
```

Used for filenames, artwork, metadata, lyrics cache, downloads.

---

## Backend (`app.py`)

### Helper extraction

`_build_track_dict(track, source)` at line ~1586 normalises every search result into Track Schema — 7 call sites deduplicated.

`standardize_track(track)` at line ~1552 — second normaliser for radio/playlist flows.

### Routes

```text
GET  /                          → desktop or mobile (UA sniff)
GET  /mobile                    → mobile player test page
GET  /api/stream                → SAVED → playlist → YT stream URL proxy
GET  /api/proxy_stream          → bytes-range proxy for `<audio>` src
GET  /api/local_file            → serve from SAVED/music_library
GET  /api/library_file          → path-safe music_library file serve
GET  /api/favorites             → cached _load_favorites
POST /api/save_favorites        → stringify-safe save, 200 on error
GET  /api/artist                → artist page data (tracks + albums)
GET  /api/artist/bio            → Wikipedia sanitised HTML
GET  /api/artist/tracks         → top tracks
GET  /api/album                 → album tracks + artwork
GET  /api/search                → all:iTunes | track|album|artist:YT Music
GET  /api/home/dashboard        → curated home categories
GET  /api/playlists             → list + contents
POST /api/playlists/create      → name-sanitised
POST /api/playlists/add
GET  /api/playlists/tracks
POST /api/playlists/delete
GET  /api/lyrics                → LRCLIB / syncedlyrics, cached SAVED/lyrics/
GET  /api/downloads/status
GET  /api/download/status
GET/POST /api/cache/config      → yt-dlp cache settings + clean
POST /api/cache/clean
GET/POST /api/settings
POST /api/settings/toggle_layout
POST /api/settings/toggle_community_showcase
GET  /api/youtube/auth_status
POST /api/youtube/refresh_auth
GET  /api/youtube/liked_songs
GET  /api/radio/recommendations
GET  /api/radio/suggest
GET  /api/community/discover
GET  /api/community/pinned
POST /api/community/pin
DELETE /api/community/unpin
GET  /api/spotify/favorites
POST /api/spotify/import
POST /api/spotify/autosync
GET  /api/spotify/autosync/status
```

### Downloads

`_download_executor = ThreadPoolExecutor(max_workers=4)` — bounded, tracked in `_download_status`.

### Cookies & auth

`cookies.txt` (netscape) → `_load_cookies_to_session` → YTMusic auth. Auto-rebuild on 401.

---

## Track Schema

```js
{ name, artist, tid, art, duration, dur, videoId, albumId, local_audio, local_art }
```

Backward-compatible only.

---

## Desktop Architecture (`templates/player.html`)

### Global state

```js
S = { queue, queueIdx, liked, playlists, playing, shuffle, repeat, radioMode, view }
```

Persistence: `akimelody_theme`, `akimelody_volume`, `akimelody_queue`, `akimelody_settings`.

### View dispatcher (`switchAkiView`)

| `S.view` | `#view-id` |
|---|---|
| `home` | `#homeView` |
| `list` | `#listView` |
| `artist` | `#artistView` |
| `album` | `#albumView` |
| `themes` | `#themeShopView` |
| theater (overlay) | `#akiTheaterView` |

Spatial transitions via FLIP engine (`Spatial`). Switches: FLIP animation with artwork preload → triggerPopInAnimation.

### Rendering

| Concern | Mechanism |
|---|---|
| Card entrance | `animate-pop-content` class + `@keyframes dynamicPopIn` (scale+opacity), stagger via `:nth-child(1..12)` delays |
| Synced restart | `triggerPopInAnimation()` — rAF-batched, limited to 30 elements, waits for images to load (1.2s timeout) |
| Offscreen skip | `content-visibility: auto` + `contain-intrinsic-size: 500px` on `.home-content`, `.scroll-zone`, `.artist-scroll`, `.album-track-list` |
| GPU compositing | `will-change: transform` on `.scroll-zone` |
| Image retry | `imgOnErrorFallback()` — 2 retries with cache-bust, 400/800ms delays, opacity transition 0.25s, data-URI SVG final fallback |
| Image fade-in | `@keyframes imgFadeIn` on `.content-canvas img`, `.scroll-zone img`, etc. |
| List diff | `renderList` smart-diff (`_renderListState`) — fast-paths activeIdx-only changes |
| Home cache | `_homeDashCache` — skips API re-fetch on back-navigation |

### VFX / Audio Visualizer

`AudioContext` + `AnalyserNode` → 5-band frequency groups → channel envelope system (slow/medium/fast).

5 frame-phase VFX effects:

| Effect | Writes | Optimisation |
|---|---|---|
| `palette-write` | 5 CSS vars on artStage | `_writeVar` value-cache |
| `audio-write` | 8 CSS vars (artStage + sidebar) | `_writeVar` skips unchanged |
| `ambient-glow` | 3 CSS vars on glow layer | `_writeVar` skips unchanged |
| `sidebar-palette` | 3 CSS vars on sidebar | `_writeVar` skips unchanged |
| `energy-flow` | 4 CSS vars on artStage | `_writeVar` skips unchanged |

Total: ~23 CSS var writes/frame → ~10 real writes/frame after caching.

### Palette Engine

`PaletteEngine` extracts 5-role palette (primary/secondary/accent/shadow/highlight) from album art via 32×32 canvas downscale and hue bucketing. Monochrome fallback generates complementary hues.

### Dynamic Theme (DNA)

22 `--dna-*` CSS custom properties control backdrop blur, saturation, card opacity, border, shadow, etc. Updated on track change via canvas colour extraction.

### Audio

| Handler | Action |
|---|---|
| `AUDIO.ontimeupdate` | Seek bar, time display, viz frame request, scroll-into-view for active track |
| `AUDIO.onended` | Repeat/radio/next dispatch, history push, `renderRecentPlayed()` |
| `AUDIO.onerror` | Fallback: retry proxy → direct YT → skip |

### Override section (bottom of file)

- `renderRecentPlayed` — history list rebuild (called on boot, track end, track change)
- `playTrack` hook — persists to history
- `setInterval(60s)` — relative time refresh

---

## Mobile (`static/js/mobile.js`)

`mApiFetch` supports POST body. `mNext()` implements shuffle. Image `onerror` fallback on thumbnails. No `S.searchResults`.

---

## Key performance invariants

- `void el.offsetWidth` forced reflows only inside `requestAnimationFrame`, capped at 30 elements
- CSS var writes use `_writeVar` with per-property cache on `style._lastVar`
- Offscreen DOM skipped via `content-visibility: auto` (modern Chromium/Firefox)
- `triggerPopInAnimation` defers until all `<img>` in view have `complete` (1.2s timeout)
