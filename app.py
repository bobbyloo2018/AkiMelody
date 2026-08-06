"""
AkiMelody (秋メロディ) — Flask Backend
"""

from flask import Flask, render_template, request, jsonify, send_from_directory, Response, redirect as _flask_redirect
import requests
try:
    from curl_cffi.requests import Session as _CurlSession
    _curl = _CurlSession(impersonate="chrome")
    print("[INIT] curl_cffi loaded — Chrome TLS impersonation active", flush=True)
except Exception as _e:
    _curl = None
    print(f"[INIT] curl_cffi unavailable ({_e}), falling back to requests", flush=True)
import re
import secrets
import yt_dlp
import json
import threading
import os
import time
import struct
import hashlib
import random
import shutil
import logging
import unicodedata
import urllib.parse
import wikipediaapi
from pathlib import Path
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from ytmusicapi import YTMusic
import ytmusic_auth as yauth

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("akimelody")

# ── Application version (SemVer) ──────────────────────────────────────────────
# Surfaced via /api/settings and used by the automatic background updater to
# compare against the latest GitHub release tag. Bump this on every release.
APP_VERSION = "1.0.1"

# GitHub repository for release checks (owner/repo shape). Override by setting
# AKI_UPDATE_REPO in the environment. Must match the repo that PostUpdate.bat
# publishes releases to (bobbyloo2018/AkiMelody), otherwise the app queries a
# non-existent repo, GitHub returns 404, and the updater reports "no releases"
# even when a newer version is published.
import os as _os
UPDATE_REPO = _os.environ.get("AKI_UPDATE_REPO", "bobbyloo2018/AkiMelody")



def _is_private_or_link_local(url):
    """Reject SSRF targets: private/loopback/link-local/reserved IPs.

    Resolves the URL host and blocks it if every A-record lands in a
    non-public range (RFC1918, loopback, link-local incl. 169.254.169.254
    cloud metadata, CGNAT, multicast, reserved/benchmark, IPv4-mapped IPv6).
    Also blocks hostnames that are literals without DNS so we never probe the
    local stack. Used by /api/img-proxy so a remote <img> or LAN client cannot
    turn it into a proxy into the host/LAN."""
    import ipaddress
    import socket as _socket

    try:
        host = urllib.parse.urlparse(url).hostname
        if not host:
            return True
        # Hostname literals (e.g. "localhost", "169.254.169.254") skip DNS.
        try:
            ips = [_socket.gethostbyname(host)]
        except OSError:
            return True
        for ip in ips:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                return True
            if not addr.is_global:
                return True
    except Exception:
        return True
    return False


# ── App & Directories ────────────────────────────────────────────────────────
import sys as _sys
if getattr(_sys, "frozen", False):
    # PyInstaller one-file: templates/static are extracted to sys._MEIPASS.
    _meipass = Path(getattr(_sys, "_MEIPASS", Path(_sys.executable).parent))
    app = Flask(__name__,
                template_folder=str(_meipass / "templates"),
                static_folder=str(_meipass / "static"))
    _data = Path(os.environ.get("LOCALAPPDATA", str(Path(_sys.executable).parent))) / "AkiMelody"
    _data.mkdir(parents=True, exist_ok=True)
    BASE_DIR = _data
else:
    app = Flask(__name__)
    BASE_DIR = Path(__file__).parent
SERVER_PORT = int(os.environ.get("AKI_SERVER_PORT", "5000"))
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max request body
SAVED_DIR = BASE_DIR / "SAVED"
SAVED_DIR.mkdir(exist_ok=True)
FAVORITES_JSON = BASE_DIR / "favorites.json"

MUSIC_LIBRARY_DIR = BASE_DIR / "music_library"
PLAYLISTS_DIR = MUSIC_LIBRARY_DIR / "playlists"
PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
LYRICS_DIR = MUSIC_LIBRARY_DIR / "lyrics"
LYRICS_DIR.mkdir(parents=True, exist_ok=True)

_STREAM_CACHE_MAX = 100
_stream_cache = OrderedDict()  # tid -> {"url": str, "exp": float}  (LRU, max _STREAM_CACHE_MAX)
_stream_cache_lock = threading.Lock()
_STREAM_URL_EXPIRE_RE = re.compile(r"[?&]expire=(\d+)")

def _invalidate_stream_cache():
    """Clear all yt-dlp stream URLs from the cache. Called after cookie refresh,
    since URLs cached before auth rotation are likely all 401-expired."""
    with _stream_cache_lock:
        _stream_cache.clear()

def _extract_url_expiry(url: str) -> float:
    """Parse the `expire=<unix_secs>` query param from a googlevideo URL.
    Returns 0.0 when absent (treated as immediately evictable on stale-upstream)."""
    m = _STREAM_URL_EXPIRE_RE.search(url or "")
    try:
        return float(m.group(1)) if m else 0.0
    except (ValueError, TypeError):
        return 0.0

def _cache_stream(tid: str, url: str) -> None:
    """LRU-bound write under _stream_cache_lock. Single source of truth for stream
    URL caching. Skip writes when tid is empty (route variants can call without a tid).
    Stores the parsed `expire=<unix_secs>` so readers can evict stale entries proactively."""
    if not tid:
        return
    with _stream_cache_lock:
        _stream_cache[tid] = {"url": url, "exp": _extract_url_expiry(url)}
        if len(_stream_cache) > _STREAM_CACHE_MAX:
            _stream_cache.popitem(last=False)

def has_valid_auth_state() -> bool:
    """Single source of truth for 'do we have a usable YouTube auth state right now?'.
    Cached in `_auth_state_ok` module global, refreshed only:
      - at process startup (one stat() read of _yt_headers_file)
      - at the end of _rebuild_ytmusic_auth() (the only code path that
        changes _yt_headers_file's state via the auth flow).
    Freshness across cookie rotation is enforced by _rebuild_ytmusic_auth()
    which invalidates _stream_cache on every rebuild."""
    return _auth_state_ok

_download_status = OrderedDict()  # tid -> {"ok": bool, "error": str|None}
_download_status_lock = threading.Lock()
_DOWNLOAD_STATUS_MAX = 500

_playlist_index_cache = None   # cached list from api_playlists
_playlist_index_lock = threading.Lock()

# ── Shared HTTP sessions (connection pooling) ─────────────────────────────────
_itunes_session = requests.Session()
_itunes_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
})
_lrclib_session = requests.Session()
_lrclib_session.headers.update({"Accept": "application/json"})

# ── iTunes art cache + concurrency limiter ────────────────────────────────────
_itunes_art_cache = OrderedDict()  # query_key -> {album_art, art, artist_name, dur, duration, enhanced}
_ITUNES_ART_CACHE_MAX = 500
_itunes_art_lock = threading.Lock()
_itunes_semaphore = threading.Semaphore(2)  # max 2 concurrent iTunes requests

# ── Shared thread pool (avoids per-request creation/teardown) ─────────────────
_io_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="aki-io")
_download_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aki-dl")

# ── Settings cache (avoids disk read on every /api/settings call) ──────────────
_settings_cache = None
_settings_cache_ts = 0.0
_SETTINGS_CACHE_TTL = 10.0  # seconds

# ── Pre-compiled regex patterns ───────────────────────────────────────────────
_SAFE_FILENAME_RE = re.compile(r'^[\w\-\.]+$')
_WDIM_RE = re.compile(r'=w\d+-h\d+[^&"\'\s]*')
_SSIZE_RE = re.compile(r'=[as]\d+[^&"\'\s]*')
_LRC_TIMESTAMP_RE = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\]')
_LRC_TAG_RE = re.compile(r'\[\d+:\d+(?:\.\d+)?\]')

# ── Favorites cache ───────────────────────────────────────────────────────────
_favorites_cache = None
_favorites_cache_ts = 0.0
_FAVORITES_CACHE_TTL = 3.0

# ── Listening stats storage ──────────────────────────────────────────────────
# Three-tier roll-up so the file stays compact for years:
#   raw[]     — individual play events, capped at 28d, then rolled to daily
#   daily[]   — one row per (Day, Track) -> {count, seconds}, capped at 1y, rolled to monthly
#   monthly[] — one row per (Month, Track) -> {count, seconds}, kept forever
#
# All reads/writes happen under `_stats_lock`. `_stats_cache` is a snapshot
# returned by `/api/stats/get` that is invalidated on write and rehydrated lazily.
STATS_JSON = BASE_DIR / "stats.json"
_stats_lock = threading.Lock()
_stats_cache = None
_stats_cache_ts = 0.0
_STATS_CACHE_TTL = 5.0

_STATS_RAW_TTL_DAYS = 28
_STATS_DAILY_TTL_DAYS = 365
_STATS_RAW_MAX = 30000
_STATS_MONTH_KEY_FMT = "%Y-%m"
_STATS_DAY_KEY_FMT = "%Y-%m-%d"


def _empty_stats_doc():
    return {"schema": 1, "raw": [], "daily": {}, "monthly": {}}


def _load_stats_locked():
    """Read stats.json under lock-free disk read; returns a fresh dict copy."""
    try:
        if STATS_JSON.exists() and STATS_JSON.stat().st_size > 0:
            data = json.loads(STATS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("schema") == 1:
                data.setdefault("raw", [])
                data.setdefault("daily", {})
                data.setdefault("monthly", {})
                return data
    except Exception as e:
        log.warning(f"[stats] failed to load stats.json ({e}), rebuilding")
    return _empty_stats_doc()


def _save_stats_locked(data):
    tmp = STATS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, STATS_JSON)


def _ts_to_day(ts):
    t = time.gmtime(ts / 1000.0)
    return time.strftime(_STATS_DAY_KEY_FMT, t)


def _ts_to_month(ts):
    t = time.gmtime(ts / 1000.0)
    return time.strftime(_STATS_MONTH_KEY_FMT, t)


def _rollover_locked(data):
    """Roll raw entries older than 28d into daily; daily older than 365d into monthly.

    Idempotent: a (day|month, track_key) row's `count` and `seconds` are summed so
    the same raw event rolled over twice never double-counts."""
    now_ms = time.time() * 1000.0
    raw_cutoff = now_ms - _STATS_RAW_TTL_DAYS * 86400000.0
    daily_cutoff = now_ms - _STATS_DAILY_TTL_DAYS * 86400000.0

    keep_raw = []
    daily = data.get("daily", {})
    monthly = data.get("monthly", {})

    for ev in data.get("raw", []):
        if ev.get("ts", 0) >= raw_cutoff:
            keep_raw.append(ev)
            continue
        day = _ts_to_day(ev["ts"])
        tkey = ev.get("id", "") or ""
        row = daily.setdefault(day, {}).setdefault(tkey, {"count": 0, "seconds": 0})
        # Only play=1 events increment the play count; progress events only
        # contribute their listened seconds.
        play_corr = ev.get("play", 0)
        try:
            play_corr = 1 if int(play_corr) == 1 else 0
        except Exception:
            play_corr = 0
        row["count"] += play_corr
        row["seconds"] += int(ev.get("sec", 0) or 0)

    for day_key in list(daily.keys()):
        day_ts = None
        try:
            day_ts = time.mktime(time.strptime(day_key, _STATS_DAY_KEY_FMT)) * 1000.0
        except Exception:
            continue
        if day_ts < daily_cutoff:
            month_key = day_key[:7]
            for tkey, v in daily[day_key].items():
                row = monthly.setdefault(month_key, {}).setdefault(tkey, {"count": 0, "seconds": 0})
                row["count"] += v.get("count", 0)
                row["seconds"] += int(v.get("seconds", 0) or 0)
            del daily[day_key]

    data["raw"] = keep_raw[-_STATS_RAW_MAX:]
    data["daily"] = daily
    data["monthly"] = monthly
    return data


def _invalidate_stats_cache():
    global _stats_cache, _stats_cache_ts
    _stats_cache = None
    _stats_cache_ts = 0.0


def _track_meta_for_event(ev):
    """Compact per-track metadata kept once per raw entry; tile cache lives on
    the client. Server only stores what's needed for aggregation & display."""
    return {
        "id": ev.get("id", ""),
        "title": ev.get("title", "")[:140],
        "artist": ev.get("artist", "")[:120],
        "art": ev.get("art", "")[:400],
        "dur": int(ev.get("dur", 0) or 0),
    }


def _aggregate_locked(data, period_days=None):
    """Build the JSON response for /api/stats/get across raw + daily + monthly.

    period_days: None = all time, else look back N days for inclusion."""
    now_ms = time.time() * 1000.0
    cutoff = 0.0 if period_days is None else (now_ms - period_days * 86400000.0)
    by_track = {}
    today_start = _ts_to_day(now_ms + 1)
    today_plays = 0
    total_seconds = 0
    total_plays = 0
    activity = {}  # day_key -> count
    hour_buckets = [0, 0, 0, 0]

    for ev in data.get("raw", []):
        _lts = ev.get("ts", 0)
        if _lts < cutoff:
            continue
        # Honour the play/progress distinction stored at write-time. A "play"
        # event is the canonical count unit (one per completed 75%-boundary
        # crossing); "progress" events only contribute their listened seconds.
        play_corr = ev.get("play", 0)
        try:
            play_corr = 1 if int(play_corr) == 1 else 0
        except Exception:
            play_corr = 0
        total_plays += play_corr
        total_seconds += int(ev.get("sec", 0) or 0)
        d = time.gmtime(_lts / 1000.0)
        day_key = time.strftime(_STATS_DAY_KEY_FMT, d)
        activity[day_key] = activity.get(day_key, 0) + play_corr
        if day_key == today_start:
            today_plays += play_corr
        hour_buckets[d.tm_hour // 6] += play_corr
        id_ = ev.get("id", "")
        t = by_track.get(id_)
        if t is None:
            t = {"id": id_, "title": ev.get("title", ""), "artist": ev.get("artist", ""),
                 "art": ev.get("art", ""), "dur": int(ev.get("dur", 0) or 0),
                 "count": 0, "seconds": 0}
            by_track[id_] = t
        t["count"] += play_corr
        t["seconds"] += int(ev.get("sec", 0) or 0)

    # daily (older, exact day)
    for day_key, tracks in data.get("daily", {}).items():
        try:
            day_ts = time.mktime(time.strptime(day_key, _STATS_DAY_KEY_FMT)) * 1000.0
        except Exception:
            continue
        if day_ts < cutoff:
            continue
        for id_, v in tracks.items():
            total_plays += v["count"]
            total_seconds += v["seconds"]
            activity[day_key] = activity.get(day_key, 0) + v["count"]
            if day_key == today_start:
                today_plays += v["count"]
            t = by_track.get(id_)
            if t is None:
                # No metadata on rolled rows; client resolves via playByLog/history
                t = {"id": id_, "title": "", "artist": "", "art": "", "dur": 0,
                     "count": 0, "seconds": 0}
                by_track[id_] = t
            t["count"] += v["count"]
            t["seconds"] += v["seconds"]

    # monthly (oldest, lowest precision)
    for month_key, tracks in data.get("monthly", {}).items():
        try:
            month_ts = time.mktime(time.strptime(month_key + "-01", _STATS_DAY_KEY_FMT)) * 1000.0
        except Exception:
            continue
        if month_ts < cutoff:
            continue
        for id_, v in tracks.items():
            total_plays += v["count"]
            total_seconds += v["seconds"]
            t = by_track.get(id_)
            if t is None:
                t = {"id": id_, "title": "", "artist": "", "art": "", "dur": 0,
                     "count": 0, "seconds": 0}
                by_track[id_] = t
            t["count"] += v["count"]
            t["seconds"] += v["seconds"]

    by_artist = {}
    for t in by_track.values():
        an = (t.get("artist") or "").strip().lower()
        if an:
            by_artist[an] = {"name": t.get("artist", ""), "count": (by_artist.get(an) or {}).get("count", 0) + t["count"], "art": t.get("art", "")}

    top_tracks = sorted(by_track.values(), key=lambda x: -x["count"])[:10]
    top_artists = sorted(by_artist.values(), key=lambda x: -x["count"])[:8]

    top_track = top_tracks[0] if top_tracks else None
    mins = total_seconds // 60

    return {
        "minutes": int(mins),
        "plays": int(total_plays),
        "seconds": int(total_seconds),
        "today_plays": int(today_plays),
        "unique_tracks": len(by_track),
        "unique_artists": len(by_artist),
        "top_track": top_track,
        "top_tracks": top_tracks,
        "top_artists": top_artists,
        "activity": [{"day": k, "count": activity[k]} for k in sorted(activity)],
        "hour_buckets": [{"name": "Night", "count": hour_buckets[0]},
                         {"name": "Morning", "count": hour_buckets[1]},
                         {"name": "Afternoon", "count": hour_buckets[2]},
                         {"name": "Evening", "count": hour_buckets[3]}],
    }

# ── Lyrics in-memory LRU cache (avoids repeated disk reads) ───────────────────
_lyrics_mem_cache = OrderedDict()  # tid -> {synced, lines}
_LYRICS_MEM_CACHE_MAX = 500
_lyrics_mem_lock = threading.Lock()

# ── Artist bio cache (LRU with TTL) ──────────────────────────────────────────
_artist_bio_cache = OrderedDict()  # name -> (result, timestamp)
_artist_bio_lock = threading.Lock()
_ARTIST_BIO_CACHE_MAX = 200
_ARTIST_BIO_CACHE_TTL = 3600  # 1 hour

# ── Radio suggestion cache (LRU with TTL) ─────────────────────────────────────
# Radio mode re-seeds the same video id many times per session, and each seed
# triggers an expensive get_watch_playlist() + per-track iTunes artwork lookup.
# Caching the resolved result per seed vid avoids hammering YouTube Music /
# Apple after only a handful of songs.
_radio_suggest_cache = OrderedDict()  # vid -> (timestamp, [sanitized tracks])
_radio_suggest_lock = threading.Lock()
_RADIO_SUGGEST_CACHE_MAX = 200
_RADIO_SUGGEST_TTL = 600  # 10 minutes

_radio_recs_cache = OrderedDict()  # vid -> (timestamp, [tracks])
_radio_recs_lock = threading.Lock()
_RADIO_RECS_CACHE_MAX = 200
_RADIO_RECS_TTL = 600  # 10 minutes


# ── Album cache (LRU with TTL) ─────────────────────────────────────────────────
# Albums are static; caching get_album() avoids a network round-trip per view.
_album_cache = OrderedDict()  # album_id -> (timestamp, payload)
_album_lock = threading.Lock()
_ALBUM_CACHE_MAX = 200
_ALBUM_TTL = 3600  # 1 hour

# ── Artist tracks cache (LRU with TTL) ─────────────────────────────────────────
# get_artist_tracks() fires get_artist() + get_playlist() (+ fallback search).
# Discographies change rarely, so cache per (artist, browse_id).
_artist_tracks_cache = OrderedDict()  # key -> (timestamp, tracks)
_artist_tracks_lock = threading.Lock()
_ARTIST_TRACKS_CACHE_MAX = 200
_ARTIST_TRACKS_TTL = 1800  # 30 minutes

# ── Artist profile-image cache (LRU with TTL) ──────────────────────────────────
# get_artist_image() resolves an artist's real profile picture (avatar), not a
# song's album art. Avatars change rarely, so cache for a long TTL.
_artist_image_cache = OrderedDict()  # name -> (timestamp, url)
_artist_image_lock = threading.Lock()
_ARTIST_IMAGE_CACHE_MAX = 300
_ARTIST_IMAGE_CACHE_TTL = 86400  # 24 hours

# ── Liked songs cache (TTL, keyed by limit) ────────────────────────────────────
# get_liked_songs() is an authenticated, moderately expensive call.
_liked_cache = {}  # limit -> (timestamp, payload)
_liked_lock = threading.Lock()
_LIKED_TTL = 60  # 1 minute

# ── YT Music search cache (LRU with TTL) ───────────────────────────────────────
# Search results are stable for minutes; caching avoids repeated YouTube Music
# hits for the same query (also benefits artist-track / radio fallbacks).
_search_cache = OrderedDict()  # key -> (timestamp, results)
_search_lock = threading.Lock()
_SEARCH_CACHE_MAX = 300
_SEARCH_TTL = 300  # 5 minutes

# ── YouTube cookies (for bot-detection bypass + ytmusicapi auth) ───────────────
# Streaming (extract_info) works without cookies. Downloads (download()) need them.
# ytmusicapi also uses cookies for authenticated access (playlists, likes, etc.)
_yt_cookie_file = BASE_DIR / "cookies.txt"
_yt_headers_file = BASE_DIR / "headers.json"

# Fallback: Tauri release mode writes to LOCALAPPDATA/com.akimelody.app/
_tauri_data_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "com.akimelody.app"
_tauri_cookie_file = _tauri_data_dir / "cookies.txt"

def _resolve_cookie_file() -> Path | None:
    """Find cookies.txt: check BASE_DIR first, then Tauri data dir."""
    if _yt_cookie_file.exists() and _yt_cookie_file.stat().st_size > 10:
        return _yt_cookie_file
    if _tauri_cookie_file.exists() and _tauri_cookie_file.stat().st_size > 10:
        return _tauri_cookie_file
    return None

# Auth-failure detection regex: matches yt-dlp / YouTube messages indicating expired cookies.
_AUTH_FAIL_RE = re.compile(r"HTTP Error 401|Sign in to confirm you|bot detection")

def _parse_netscape_cookies(cookie_text: str, youtube_only: bool = False) -> str:
    """Parse Netscape cookies.txt → semicolon-separated Cookie header string.
    If youtube_only=True, only include cookies from youtube.com domains
    (needed for music.youtube.com API — .google.com cookies cause auth rejection)."""
    cookies = []
    for line in cookie_text.splitlines():
        if line.startswith('#') or not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        domain, _, _, _, _, name, value = parts[:7]
        if youtube_only:
            if 'youtube.com' in domain:
                cookies.append(f"{name}={value}")
        else:
            if 'google.com' in domain or 'youtube.com' in domain:
                cookies.append(f"{name}={value}")
    return '; '.join(cookies)

def _load_cookies_to_session(session, cookie_text: str):
    """Load Netscape cookies.txt into a requests.Session cookie jar with proper domain matching."""
    import http.cookiejar
    for line in cookie_text.splitlines():
        if line.startswith('#') or not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        if 'google.com' in domain or 'youtube.com' in domain:
            session.cookies.set(name, value, domain=domain, path=path)

def _generate_auth_headers(cookie_text: str) -> bool:
    """Generate headers.json for ytmusicapi from cookies.txt content.
    Includes BOTH youtube.com and google.com domain cookies because the
    SAPISIDHASH authorization ytmusicapi computes needs the SAPISID cookie
    which lives on .google.com. Excluding google.com cookies produces a
    headers.json that silently fails auth (0.1 kb, no real cookies)."""
    cookie_header = _parse_netscape_cookies(cookie_text, youtube_only=False)
    if not cookie_header:
        print("[AUTH] _generate_auth_headers: no cookies parsed from cookie text", flush=True)
        return False

    # Extract SAPISID from parsed cookies for real SAPISIDHASH
    sapisid = None
    for pair in cookie_header.split("; "):
        if pair.startswith("SAPISID="):
            sapisid = pair.split("=", 1)[1]
            break

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_header,
        "Origin": "https://music.youtube.com",
        "X-Origin": "https://music.youtube.com",
    }

    # Compute real SAPISIDHASH if SAPISID is present
    if sapisid:
        try:
            import ytmusic_auth as yauth
            headers["authorization"] = yauth.compute_sapisidhash(sapisid)
            print("[AUTH] Computed real SAPISIDHASH from SAPISID cookie", flush=True)
        except Exception as exc:
            print(f"[AUTH] Failed to compute SAPISIDHASH: {exc} — using placeholder", flush=True)
            headers["authorization"] = "SAPISIDHASH 0_dummy"
    else:
        headers["authorization"] = "SAPISIDHASH 0_dummy"
        print("[AUTH] No SAPISID cookie found — using placeholder SAPISIDHASH", flush=True)

    try:
        _yt_headers_file.write_text(json.dumps(headers, indent=2), encoding="utf-8")
        # Log which auth cookies are present for diagnostics
        names = [p.split("=", 1)[0] for p in cookie_header.split("; ") if "=" in p]
        has_sapisid = "SAPISID" in names
        has_sid = "SID" in names
        print(f"[AUTH] Generated headers.json ({_yt_headers_file.stat().st_size} bytes): "
              f"SAPISID={'YES' if has_sapisid else 'NO'}, SID={'YES' if has_sid else 'NO'}, "
              f"total_cookies={len(names)}", flush=True)
        return True
    except Exception as e:
        print(f"[INIT] Failed to generate headers.json: {e}", flush=True)
        return False

def _init_yt_cookies():
    """Check for cookies.txt presence. Returns True if cookies available."""
    cookie_file = _resolve_cookie_file()
    if cookie_file:
        print(f"[INIT] Using cookies.txt ({cookie_file} — {cookie_file.stat().st_size} bytes)", flush=True)
        # Sync cookies.txt to BASE_DIR if found elsewhere
        if cookie_file != _yt_cookie_file:
            try:
                shutil.copy2(cookie_file, _yt_cookie_file)
                print(f"[INIT] Synced cookies.txt to {_yt_cookie_file}", flush=True)
            except Exception:
                pass
        # Generate/update headers.json for ytmusicapi
        try:
            _generate_auth_headers(cookie_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return True
    # Clean up stale headers.json if no cookies
    if _yt_headers_file.exists():
        try:
            _yt_headers_file.unlink()
            print("[INIT] Removed stale headers.json (no cookies.txt found)", flush=True)
        except Exception:
            pass
    print("[INIT] No cookies.txt — YouTube downloads may fail (bot detection).", flush=True)
    return False

_yt_cookies_ok = _init_yt_cookies()

def _ydl_extras() -> dict:
    """Return cookie/auth extras for yt-dlp options."""
    cookie_file = _resolve_cookie_file()
    if cookie_file:
        return {"cookies": str(cookie_file)}
    return {}

def _ydl_js_runtimes() -> dict:
    """Enable yt-dlp JS runtimes for YouTube extraction.

    Since yt-dlp ~2025.06, YouTube's player response requires an external JS
    runtime (deno/node/quickjs/bun) to solve the n-sig + player-client dance.
    Without one, yt-dlp silently degrades to placeholder "storyboard" formats
    and no audio comes back. yt-dlp picks the highest-priority runtime that is
    BOTH enabled and present on the machine (priority: deno > node > quickjs >
    bun), so we enable whatever this environment can actually provide:
      - dev: node is on PATH (used for the Electron build), fall back to a
             standalone qjs.exe next to the repo if present.
      - frozen: a bundled qjs.exe ships inside the server (see spec datas) and
             is resolved from sys._MEIPASS — no Node required on user machines.
    """
    runtimes = {}
    qjs = None
    if getattr(_sys, "frozen", False):
        cand = Path(getattr(_sys, "_MEIPASS", Path(_sys.executable).parent)) / "qjs.exe"
        if cand.exists():
            qjs = str(cand)
    else:
        found = shutil.which("qjs") or shutil.which("qjs.exe")
        if found:
            qjs = found
    if qjs:
        runtimes["quickjs"] = {"path": qjs}
    if shutil.which("node"):
        runtimes["node"] = {}
    return runtimes

# Common yt-dlp options shared across extract/download sites.
# no_color: skips ANSI/emitter scan; small happy-path gain.
# quiet / no_warnings / noplaylist: original baseline (preserved here so call sites spread this).
_YDL_COMMON_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "no_color": True,
    # NOTE: do NOT set player_client here. Forcing ["android", "web"] (or any
    # single client) makes modern yt-dlp return only storyboard placeholder
    # formats with zero real audio. Rely on yt-dlp's default client rotation.
    # JS runtime required by modern yt-dlp for YouTube extraction (see
    # _ydl_js_runtimes). Without it only storyboard placeholder formats return.
    "js_runtimes": _ydl_js_runtimes(),
}

# YDL options shared across extract-only sites (no postprocess, no outtmpl).
# retries=1 cuts the fallback-retry-storm when yt-dlp hits an auth-error in
# the format fall-through chain (default is 3, multiplied by 4 formats = 12 retries).
_YDL_EXTRACT_OPTS = {
    **_YDL_COMMON_OPTS,
    "socket_timeout": 15,
    "retries": 1,
}

# Module-level UA shared across all yt-dlp sites for the http_headers key.
_YDL_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ── File reverse index (avoids rglob on every /api/local_file request) ────────
_file_index = {}  # filename -> Path (absolute)
_file_index_lock = threading.Lock()
_file_index_ts = 0.0
_FILE_INDEX_TTL = 30.0

def _get_file_index():
    global _file_index, _file_index_ts
    now = time.time()
    with _file_index_lock:
        if _file_index and (now - _file_index_ts) < _FILE_INDEX_TTL:
            return _file_index
    idx = {}
    if PLAYLISTS_DIR.exists():
        for sub in PLAYLISTS_DIR.rglob("*"):
            if sub.is_file():
                idx[sub.name] = sub
    with _file_index_lock:
        _file_index = idx
        _file_index_ts = now
    return idx

def _invalidate_file_index():
    global _file_index, _file_index_ts
    with _file_index_lock:
        _file_index = {}
        _file_index_ts = 0.0

# ── Community discover cache ──────────────────────────────────────────────────
COMMUNITY_CACHE_JSON = SAVED_DIR / "community_cache.json"
_community_cache = None
_community_cache_ts = 0.0
_COMMUNITY_CACHE_TTL = 3600  # 1 hour
_community_cache_lock = threading.Lock()

# ── Community pinned art flat-file DB ────────────────────────────────────────
PINNED_ART_JSON = SAVED_DIR / "pinned_art.json"
_pinned_art_cache = None
_pinned_art_cache_ts = 0.0
_PINNED_ART_CACHE_TTL = 5.0
_pinned_art_lock = threading.Lock()

# ── MusicBrainz session ──────────────────────────────────────────────────────
_musicbrainz_session = requests.Session()
_musicbrainz_session.headers.update({
    "User-Agent": "AkiMelody/1.0 (https://github.com/akimelody; akimelody@example.com)",
    "Accept": "application/json",
})

# Persistent fallback for /api/proxy_stream when curl_cffi is unavailable.
# One session means we reuse TCP + TLS handshakes across all track plays instead of
# opening a fresh connection per /api/proxy_stream call.
_fallback_proxy_session = requests.Session()
_fallback_proxy_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.youtube.com/",
    "Origin": "https://www.youtube.com",
})

# ── Downloads status cache (expensive endpoint) ───────────────────────────────
_downloads_status_cache = None
_downloads_status_cache_ts = 0.0
_DOWNLOADS_STATUS_CACHE_TTL = 3.0

# ── Auth state cache (avoids _yt_headers_file.stat() per request) ────────────
_auth_state_ok = _yt_headers_file.exists() and _yt_headers_file.stat().st_size > 10

SETTINGS_JSON = BASE_DIR / "settings.json"


def _load_settings() -> dict:
    global _settings_cache, _settings_cache_ts
    now = time.time()
    if _settings_cache is not None and (now - _settings_cache_ts) < _SETTINGS_CACHE_TTL:
        return dict(_settings_cache)
    defaults = {"ui_layout_mode": "card", "cache_limit_gb": 5, "community_showcase_enabled": True}
    if SETTINGS_JSON.exists():
        try:
            data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
            defaults.update(data)
        except Exception:
            pass
    _settings_cache = dict(defaults)
    _settings_cache_ts = now
    return _settings_cache

def _save_settings(data: dict):
    global _settings_cache, _settings_cache_ts
    SETTINGS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _settings_cache = dict(data)
    _settings_cache_ts = time.time()

def _fmt_size(b):
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"

def _init_ytmusic():
    """Initialize YTMusic with auth headers if available, otherwise unauthenticated.
    Uses a requests.Session with cookies loaded from cookies.txt for proper domain matching.
    This avoids sending .google.com domain cookies to music.youtube.com (which rejects them)."""
    if has_valid_auth_state():
        try:
            import requests as _req
            session = _req.Session()
            cookie_file = _resolve_cookie_file()
            if cookie_file:
                _load_cookies_to_session(session, cookie_file.read_text(encoding="utf-8"))
                session.cookies.set("SOCS", "CAI", domain=".youtube.com")
            ytm = YTMusic(str(_yt_headers_file), requests_session=session)
            # Clear ytmusicapi's own cookies dict so cookies={"SOCS":"CAI"} is NOT
            # passed to session.post() — we handle cookies via the session jar instead
            ytm.cookies = {}
            print(f"[INIT] YTMusic authenticated via headers.json + session cookies ({_yt_headers_file.stat().st_size} bytes)", flush=True)
            return ytm
        except Exception as e:
            print(f"[INIT] YTMusic auth failed ({e}), falling back to unauthenticated", flush=True)
    ytm = YTMusic()
    print("[INIT] YTMusic running unauthenticated", flush=True)
    return ytm

ytmusic = _init_ytmusic()

def _rebuild_ytmusic_auth():
    """Re-read cookies.txt and rebuild YTMusic auth. Called after login/link."""
    global ytmusic, _auth_state_ok

    # Check if ytmusic_auth already has a valid auth file (from popup flow)
    try:
        auth_st = yauth.get_auth_status()
        if auth_st.get("authenticated"):
            print(f"[AUTH] Rebuild: ytmusic_auth already has valid auth ({auth_st['size']} bytes at {auth_st['path']})", flush=True)
            # Verify it still works
            if yauth.verify_auth(auth_st["path"]):
                print("[AUTH] Rebuild: auth verified OK — skipping regeneration", flush=True)
                _auth_state_ok = True
                ytmusic = _init_ytmusic()
                _invalidate_stream_cache()
                return True
            else:
                print("[AUTH] Rebuild: auth verification failed — falling back to cookies", flush=True)
    except Exception as exc:
        print(f"[AUTH] Rebuild: ytmusic_auth check failed: {exc}", flush=True)

    # Fall back to cookies.txt → headers.json regeneration
    cookie_file = _resolve_cookie_file()
    if cookie_file:
        print(f"[AUTH] Rebuild: reading cookies from {cookie_file} ({cookie_file.stat().st_size} bytes)", flush=True)
        try:
            _generate_auth_headers(cookie_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[AUTH] Rebuild: _generate_auth_headers failed: {e}", flush=True)
    else:
        print("[AUTH] Rebuild: no cookie file found", flush=True)
    # Update _auth_state_ok BEFORE calling _init_ytmusic() so the
    # has_valid_auth_state() check inside _init_ytmusic sees the current state.
    _auth_state_ok = _yt_headers_file.exists() and _yt_headers_file.stat().st_size > 10
    print(f"[AUTH] Rebuild: _auth_state_ok={_auth_state_ok}", flush=True)
    ytmusic = _init_ytmusic()
    _invalidate_stream_cache()
    return _auth_state_ok

# ── Backend helpers ───────────────────────────────────────────────────────────

def get_track_id(name, artist):
    return hashlib.md5(f"{str(name).strip()}_{str(artist).strip()}".lower().encode()).hexdigest()

def _build_track_dict(name, artist, art, dur, tid, videoId, albumId="", **extra):
    d = {
        "name": name, "artist": artist,
        "art": art, "dur": dur, "tid": tid,
        "videoId": videoId,
        "albumId": albumId or "",
        "local_audio": (SAVED_DIR / f"{tid}.mp3").exists(),
        "local_art": (SAVED_DIR / f"{tid}.jpg").exists(),
    }
    d.update(extra)
    return d

def _load_favorites():
    global _favorites_cache, _favorites_cache_ts
    now = time.time()
    if _favorites_cache is not None and (now - _favorites_cache_ts) < _FAVORITES_CACHE_TTL:
        return list(_favorites_cache)
    favs = []
    if FAVORITES_JSON.exists():
        try:
            favs = json.loads(FAVORITES_JSON.read_text(encoding="utf-8"))
            if not isinstance(favs, list): favs = []
        except Exception:
            pass
    _favorites_cache = favs
    _favorites_cache_ts = now
    return list(favs)

def _invalidate_favorites_cache():
    global _favorites_cache, _favorites_cache_ts
    _favorites_cache = None
    _favorites_cache_ts = 0.0

def itunes_search(query: str, limit: int = 15) -> list:
    if not query.strip(): return []
    try:
        url = "https://itunes.apple.com/search"
        params = {"term": query, "entity": "song", "limit": limit, "country": "JP"}
        resp = _itunes_session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        results = []
        for item in resp.json().get("results", []):
            name = item.get("trackName", "Unknown Track")
            artist = item.get("artistName", "Unknown Artist")
            art = item.get("artworkUrl100", "")
            tid = get_track_id(name, artist)
            results.append({
                "name": name, "artist": artist,
                "art": art.replace("100x100bb.jpg", "600x600bb.jpg"),
                "dur": int(item.get("trackTimeMillis", 210000) / 1000),
                "tid": tid
            })
        return results
    except Exception as e:
        log.warning(f"Search Error: {e}")
        return []

def _record_download_status(tid: str, ok: bool, error: str = None):
    """Record download status with automatic eviction if over limit."""
    with _download_status_lock:
        if len(_download_status) >= _DOWNLOAD_STATUS_MAX:
            # Evict oldest 20% of entries
            evict_count = max(1, _DOWNLOAD_STATUS_MAX // 5)
            for _ in range(evict_count):
                if _download_status:
                    _download_status.popitem(last=False)
        _download_status[tid] = {"ok": ok, "error": error}

def download_track(track: dict) -> bool:
    tid = track.get('tid') or get_track_id(track['name'], track['artist'])
    audio_path = SAVED_DIR / f"{tid}.mp3"
    art_path = SAVED_DIR / f"{tid}.jpg"
    if not art_path.exists() and track.get('art'):
        art_url = track['art']
        if art_url.startswith(('https://', 'http://')):
            try:
                r = requests.get(art_url, timeout=10, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
                if r.ok: art_path.write_bytes(r.content)
            except Exception as e:
                log.warning(f"Art download failed for {tid}: {e}")
    if not audio_path.exists():
        vid = track.get('videoId', '')
        if vid:
            source = f"https://www.youtube.com/watch?v={vid}"
        else:
            source = f"ytsearch1:{track['artist']} {track['name']} audio"
        ydl_opts = {
            'format': 'bestaudio/best', 'outtmpl': str(SAVED_DIR / tid),
            'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}],
            **_YDL_COMMON_OPTS,
            "http_headers": {"User-Agent": _YDL_USER_AGENT},
            **_ydl_extras(),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([source])
            for f in SAVED_DIR.glob(f"{tid}*"):
                if f.suffix == ".mp3":
                    if f.name != f"{tid}.mp3": f.rename(audio_path)
                    break
                elif f.suffix in [".m4a", ".webm", ".opus"]: f.rename(audio_path); break
        except Exception as e:
            log.warning(f"Download failed for {tid}: {e}")
            _record_download_status(tid, False, str(e))
            return False
    _record_download_status(tid, True)
    return True

def get_stream_url(query: str, tid: str = "", vid: str = "", force: bool = False) -> dict:
    if tid and not force:
        with _stream_cache_lock:
            if tid in _stream_cache:
                cached = _stream_cache[tid]
                entry = cached if isinstance(cached, dict) else {"url": cached}
                exp = entry.get("exp") or 0
                url = entry["url"]
                # Evict expired entries inline rather than serving stale URLs.
                if exp and exp <= time.time():
                    _stream_cache.pop(tid, None)
                else:
                    _stream_cache.move_to_end(tid)
                    return {"url": url, "cached": True}
    elif tid and force:
        # Eager-evict the entry being bypassed so the fresh URL re-extracted below
        # isn't shadowed by the stale one for the duration of the request.
        with _stream_cache_lock:
            _stream_cache.pop(tid, None)

    _YDL_HTTP_HEADERS = {"User-Agent": _YDL_USER_AGENT}

    def _extract_url(source):
        formats_to_try = [
            "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "bestaudio/best",
            "best[ext=m4a]/best[ext=webm]/best",
            "best",
        ]
        auth_fail_seen = False
        for fmt in formats_to_try:
            ydl_opts = {
                "format": fmt,
                **_YDL_EXTRACT_OPTS,
                "http_headers": _YDL_HTTP_HEADERS,
                **_ydl_extras(),
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(source, download=False)
                    if "entries" in info:
                        entries = [e for e in info["entries"] if e]
                        if not entries:
                            return (None, auth_fail_seen)
                        info = entries[0]
                    formats = info.get("formats", [])
                    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("url")]
                    if not audio:
                        audio = [f for f in formats if f.get("url")]
                    if not audio:
                        continue
                    best = max(audio, key=lambda f: ({"m4a":3,"webm":2}.get(f.get("ext",""),0), f.get("tbr") or 0))
                    return (best["url"], auth_fail_seen)
            except Exception as e:
                msg = str(e)
                if _AUTH_FAIL_RE.search(msg):
                    print(f"[AUTH_FAIL] source={source[:80]} fmt={fmt} err={msg[:120]}", flush=True)
                    auth_fail_seen = True
                continue
        return (None, auth_fail_seen)

    if vid:
        url, auth_fail_seen = _extract_url(f"https://www.youtube.com/watch?v={vid}")
        if url:
            if not auth_fail_seen:
                _cache_stream(tid, url)
            return {"url": url}
        print(f"[STREAM_URL] vid={vid} FAILED, falling back to ytsearch", flush=True)
        url, auth_fail_seen = _extract_url(f"ytsearch1:{query}")
        if url:
            if not auth_fail_seen:
                _cache_stream(tid, url)
            return {"url": url}
        print(f"[STREAM_URL] ALL FAILED for: {query[:50]}", flush=True)
        return {"error": "Stream not available"}

    if query.startswith("http"):
        url, auth_fail_seen = _extract_url(query)
    else:
        url, auth_fail_seen = _extract_url(f"ytsearch1:{query}")

    if url:
        if not auth_fail_seen:
            _cache_stream(tid, url)
        return {"url": url}
    print(f"[STREAM_URL] ytsearch FAILED for: {query[:50]}", flush=True)
    return {"error": "Stream not available"}

def _clean_search_tokens(query: str) -> str:
    q = " ".join(query.split())
    lower = q.lower()
    if " by " in lower:
        idx = lower.index(" by ")
        track = q[:idx].strip()
        artist = q[idx + 4:].strip()
        if track and artist:
            q = f"{artist} {track}"
    q = q.replace(" - ", " ")
    return " ".join(q.split())

_STOP_WORDS = {"by", "feat", "ft", "and", "the", "a", "an", "of", "in", "on", "at", "to", "for", "is", "it", "or"}

def _tokenize_query(query: str) -> list:
    lower = re.sub(r'[^\w\s]', '', query.lower())
    return [w for w in lower.split() if w and w not in _STOP_WORDS]

def yt_music_search(query: str, limit: int = 7) -> list:
    if not query.strip(): return []
    key = query.lower().strip()
    now = time.time()
    with _search_lock:
        if key in _search_cache:
            ts, res = _search_cache[key]
            if now - ts < _SEARCH_TTL:
                _search_cache.move_to_end(key)
                return res
            del _search_cache[key]
    res = _yt_music_search_uncached(query)
    with _search_lock:
        _search_cache[key] = (now, res)
        if len(_search_cache) > _SEARCH_CACHE_MAX:
            _search_cache.popitem(last=False)
    return res


def _yt_music_search_uncached(query: str) -> list:
    cleaned = _clean_search_tokens(query)
    tokens = _tokenize_query(cleaned)
    try:
        results = ytmusic.search(cleaned, filter="songs", limit=30)
        tracks = []
        for e in results:
            if not e.get("videoId") and not e.get("browseId"): continue
            name = e.get("title", "Unknown")
            artists = e.get("artists") or []
            artist = artists[0].get("name", "Unknown Artist") if artists else "Unknown Artist"
            thumbs = e.get("thumbnails") or []
            art = thumbs[-1]["url"] if thumbs else ""
            dur_str = e.get("duration") or "0:00"
            parts = dur_str.split(":")
            try:
                dur = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0])
            except (ValueError, IndexError):
                dur = 0
            vid = e.get("videoId", "")
            tid = get_track_id(name, artist)
            album = e.get("album") or {}
            tracks.append(_build_track_dict(name, artist, art, dur, tid, vid, album.get("id") or ""))
        for t in tracks:
            title_lower = t["name"].lower()
            artist_lower = t["artist"].lower()
            score = 0
            for tok in tokens:
                if tok in title_lower: score += 2
                if tok in artist_lower: score += 1
            if any(tok == title_lower for tok in tokens):
                score += 50
            t["_score"] = score
        tracks.sort(key=lambda t: t["_score"], reverse=True)
        for t in tracks: del t["_score"]
        return tracks[:7]
    except Exception as e:
        log.warning(f"yt_music_search error: {e}")
        return []

def yt_music_search_filtered(query: str, filter_type: str) -> list:
    if not query.strip(): return []
    key = f"{query.lower().strip()}|{filter_type}"
    now = time.time()
    with _search_lock:
        if key in _search_cache:
            ts, res = _search_cache[key]
            if now - ts < _SEARCH_TTL:
                _search_cache.move_to_end(key)
                return res
            del _search_cache[key]
    res = _yt_music_search_filtered_uncached(query, filter_type)
    with _search_lock:
        _search_cache[key] = (now, res)
        if len(_search_cache) > _SEARCH_CACHE_MAX:
            _search_cache.popitem(last=False)
    return res


def _yt_music_search_filtered_uncached(query: str, filter_type: str) -> list:
    yt_filter = {"track": "songs", "album": "albums", "artist": "artists"}.get(filter_type, "songs")
    try:
        results = ytmusic.search(query, filter=yt_filter, limit=10)
        if filter_type == "track":
            tracks = []
            for e in results:
                if not e.get("videoId"): continue
                name = e.get("title", "Unknown")
                artists = e.get("artists") or []
                artist = artists[0].get("name", "Unknown Artist") if artists else "Unknown Artist"
                thumbs = e.get("thumbnails") or []
                art = thumbs[-1]["url"] if thumbs else ""
                dur_str = e.get("duration") or "0:00"
                parts = dur_str.split(":")
                try:
                    dur = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0])
                except (ValueError, IndexError):
                    dur = 0
                vid = e.get("videoId", "")
                tid = get_track_id(name, artist)
                album = e.get("album") or {}
                tracks.append(_build_track_dict(name, artist, art, dur, tid, vid, album.get("id") or ""))
            return tracks[:10]
        elif filter_type == "album":
            albums = []
            for e in results:
                if not e.get("browseId"): continue
                name = e.get("title", "Unknown Album")
                artists = e.get("artists") or []
                artist = artists[0].get("name", "Unknown Artist") if artists else "Unknown Artist"
                thumbs = e.get("thumbnails") or []
                art = thumbs[-1]["url"] if thumbs else ""
                track_count = e.get("trackCount") or len(e.get("tracks") or [])
                year = e.get("year") or ""
                albums.append({
                    "name": name, "artist": artist,
                    "art": art, "browseId": e["browseId"],
                    "trackCount": track_count, "year": year,
                    "type": "album"
                })
            return albums[:10]
        elif filter_type == "artist":
            artists_list = []
            for e in results:
                if not e.get("browseId"): continue
                name = e.get("artist") or e.get("name") or e.get("title", "Unknown Artist")
                thumbs = e.get("thumbnails") or []
                art = thumbs[-1]["url"] if thumbs else ""
                sub = e.get("subscribers") or ""
                artists_list.append({
                    "name": name, "art": art,
                    "browseId": e["browseId"],
                    "subscribers": sub,
                    "type": "artist"
                })
            return artists_list[:10]
    except Exception as e:
        log.warning(f"yt_music_search_filtered error: {e}")
        return []
    return []

def get_artist_bio(artist_name: str) -> str:
    now = time.time()
    with _artist_bio_lock:
        if artist_name in _artist_bio_cache:
            result, ts = _artist_bio_cache[artist_name]
            if (now - ts) < _ARTIST_BIO_CACHE_TTL:
                _artist_bio_cache.move_to_end(artist_name)
                return result
            del _artist_bio_cache[artist_name]
    for attempt in range(3):
        try:
            wiki = wikipediaapi.Wikipedia('AkiMelody/3.0', 'en')
            search_name = f"{artist_name} (musician)"
            page = wiki.page(search_name)
            if not page.exists():
                page = wiki.page(artist_name)
            if page.exists():
                summary = page.summary
                if summary:
                    result = summary[:500]
                    with _artist_bio_lock:
                        _artist_bio_cache[artist_name] = (result, now)
                        if len(_artist_bio_cache) > _ARTIST_BIO_CACHE_MAX:
                            _artist_bio_cache.popitem(last=False)
                    return result
                log.debug(f"Wikipedia page exists but no summary for {artist_name}")
            else:
                log.debug(f"Wikipedia page not found for {artist_name}")
            break
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            log.warning(f"Wikipedia lookup failed for {artist_name}: {e}")
            break
    fallback = f"Discover and stream official tracks from {artist_name} directly through the player."
    with _artist_bio_lock:
        _artist_bio_cache[artist_name] = (fallback, now)
        if len(_artist_bio_cache) > _ARTIST_BIO_CACHE_MAX:
            _artist_bio_cache.popitem(last=False)
    return fallback

def get_artist_image(artist_name: str):
    """Resolve an artist's real profile picture (avatar), not a song's album art.

    Tries the YTMusic artist search thumbnail first, then the full artist page's
    avatar (`thumbnails` / `artistArtRef`). Collab strings like "A feat. B" are
    retried against the primary artist name so they still resolve.
    """
    def _norm(url):
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        return url

    def _thumb_url(item):
        if not isinstance(item, dict):
            return None
        thumbs = item.get("thumbnails") or []
        if not thumbs:
            ref = item.get("artistArtRef")
            if isinstance(ref, dict):
                return _norm(ref.get("url"))
            if isinstance(ref, list) and ref:
                thumbs = ref
        if thumbs:
            try:
                best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            except Exception:
                best = thumbs[-1]
            url = best.get("url") if isinstance(best, dict) else None
            return _norm(url)
        return None

    def _search_one(name):
        try:
            results = ytmusic.search(name, filter="artists", limit=1)
        except Exception as e:
            log.warning(f"get_artist_image search failed for {name!r}: {e}")
            return None
        if not results:
            return None
        r = results[0]
        art = _thumb_url(r)
        if art:
            return art
        browse_id = r.get("browseId")
        if browse_id:
            try:
                ad = ytmusic.get_artist(browse_id)
                artist_block = (ad or {}).get("artist") or {}
                art = _thumb_url({"thumbnails": artist_block.get("thumbnails") or []})
                if art:
                    return art
                art = _thumb_url(artist_block)
                if art:
                    return art
            except Exception as e:
                log.warning(f"get_artist_image get_artist failed for {name!r}: {e}")
        return None

    candidates = [artist_name]
    if artist_name:
        # Strip collab/feature delimiters and try the primary artist first.
        primary = re.split(r",|&|\bx\b| feat\.?| ft\.?| with ", artist_name, flags=re.IGNORECASE)
        if primary:
            first = primary[0].strip()
            if first and first.lower() != artist_name.lower():
                candidates.insert(0, first)
    for name in candidates:
        art = _search_one(name)
        if art:
            return art
    # Last-resort fallback: iTunes artist artwork (very reliable, CORS-friendly).
    for name in candidates:
        art = _itunes_artist_image(name)
        if art:
            return art
    return None


def _itunes_artist_image(artist_name: str):
    """Look up an artist avatar via the iTunes Search API.

    Returns the upscaled artwork URL or None. Runs only as a fallback when
    YouTube Music cannot resolve an artist picture.
    """
    if not artist_name:
        return None
    try:
        resp = _itunes_session.get(
            "https://itunes.apple.com/search",
            params={"term": artist_name, "entity": "musicArtist", "limit": 5},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        for r in results:
            url = r.get("artworkUrl100")
            if url:
                # Upscale the 100px placeholder to a sharper 300px version.
                url = url.replace("100x100", "300x300")
                return "https:" + url if url.startswith("//") else url
    except Exception as e:
        log.warning(f"iTunes artist image lookup failed for {artist_name!r}: {e}")
    return None


def get_artist_image_cached(artist_name: str):
    now = time.time()
    with _artist_image_lock:
        if artist_name in _artist_image_cache:
            ts, url = _artist_image_cache[artist_name]
            if (now - ts) < _ARTIST_IMAGE_CACHE_TTL:
                _artist_image_cache.move_to_end(artist_name)
                return url
            del _artist_image_cache[artist_name]
    url = get_artist_image(artist_name)
    with _artist_image_lock:
        _artist_image_cache[artist_name] = (now, url)
        if len(_artist_image_cache) > _ARTIST_IMAGE_CACHE_MAX:
            _artist_image_cache.popitem(last=False)
    return url


def _upscale_thumb(url: str) -> str:
    if not url: return url
    url = _WDIM_RE.sub('=w600-h600', url)
    url = _SSIZE_RE.sub('=s600', url)
    return url

def _parse_duration(track: dict) -> int:
    dur_sec = track.get("duration_seconds")
    if dur_sec is not None:
        try: return int(dur_sec)
        except (ValueError, TypeError): pass
    dur_str = track.get("duration") or "0:00"
    if isinstance(dur_str, (int, float)):
        return int(dur_str)
    parts = str(dur_str).split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0])
    except (ValueError, IndexError):
        return 0

def _yt_track_to_dict(e, default_artist):
    """Normalize a raw YT Music track entry into the project Track Schema
    (shared by both branches of _fetch_artist_tracks_uncached)."""
    name = e.get("title", "Unknown")
    artists = e.get("artists") or []
    artist = artists[0].get("name", default_artist) if artists else default_artist
    thumbs = e.get("thumbnails") or []
    art = _upscale_thumb(thumbs[-1]["url"]) if thumbs else ""
    dur = _parse_duration(e)
    vid = e.get("videoId", "")
    tid = get_track_id(name, artist)
    album = e.get("album") or {}
    return _build_track_dict(name, artist, art, dur, tid, vid, album.get("id") or "", albumName=album.get("name") or "")


def get_artist_tracks(artist_name: str, browse_id: str = "") -> list:
    """Cached wrapper: discographies change rarely, so memoize per (artist, browse_id)."""
    key = f"{artist_name.lower().strip()}|{browse_id}"
    now = time.time()
    with _artist_tracks_lock:
        if key in _artist_tracks_cache:
            ts, tracks = _artist_tracks_cache[key]
            if now - ts < _ARTIST_TRACKS_TTL:
                _artist_tracks_cache.move_to_end(key)
                return tracks
            del _artist_tracks_cache[key]
    tracks = _fetch_artist_tracks_uncached(artist_name, browse_id)
    with _artist_tracks_lock:
        _artist_tracks_cache[key] = (now, tracks)
        if len(_artist_tracks_cache) > _ARTIST_TRACKS_CACHE_MAX:
            _artist_tracks_cache.popitem(last=False)
    return tracks


def _fetch_artist_tracks_uncached(artist_name: str, browse_id: str = "") -> list:
    try:
        if browse_id:
            artist_data = ytmusic.get_artist(browse_id)
        else:
            results = ytmusic.search(artist_name, filter="artists", limit=1)
            if results and results[0].get("browseId"):
                artist_data = ytmusic.get_artist(results[0]["browseId"])
            else:
                artist_data = None
        if artist_data:
            songs_section = artist_data.get("songs", {})
            songs_browse = songs_section.get("browseId", "")
            if songs_browse:
                playlist_data = ytmusic.get_playlist(songs_browse, limit=50)
                items = playlist_data.get("tracks", [])
            else:
                items = songs_section.get("results", [])
            tracks = []
            for e in items:
                if not isinstance(e, dict): continue
                tracks.append(_yt_track_to_dict(e, artist_name))
            if tracks:
                return tracks[:50]
    except Exception as e:
        log.warning(f"get_artist_tracks browseId error: {e}")
    try:
        fallback = ytmusic.search(artist_name, filter="songs", limit=20)
        tracks = []
        target = artist_name.lower()
        for e in fallback:
            if e.get("type") != "video": continue
            name = e.get("title", "Unknown")
            artists = e.get("artists") or []
            artist = artists[0].get("name", "Unknown Artist") if artists else "Unknown Artist"
            if target not in artist.lower() and target not in name.lower():
                continue
            tracks.append(_yt_track_to_dict(e, "Unknown Artist"))
        return tracks[:50]
    except Exception as e:
        log.warning(f"get_artist_tracks fallback error: {e}")
        return []

def _parse_lrc(lrc_text: str) -> list:
    """Parse LRC text into normalized lines [{time, text}]."""
    lines = []
    for raw in lrc_text.split('\n'):
        raw = raw.strip()
        if not raw:
            continue
        timestamps = _LRC_TIMESTAMP_RE.findall(raw)
        if not timestamps:
            continue
        text = _LRC_TAG_RE.sub('', raw).strip()
        if not text:
            continue
        for m_s, s_s in timestamps:
            try:
                t = int(m_s) * 60 + float(s_s)
                lines.append({"time": round(t, 2), "text": text})
            except (ValueError, TypeError):
                continue
    lines.sort(key=lambda x: x["time"])
    return lines

def _write_lyrics_cache(tid: str, data: dict, neg_ttl: int = 0):
    """Write lyrics to in-memory LRU + filesystem cache.

    If `neg_ttl` > 0, the entry is treated as a negative-cache hit (empty/no-lyrics result)
    and stamped with an `exp` (epoch-seconds) after which it must be re-validated.
    Positive (real) lyrics carry no `exp` and remain valid until purged manually.
    """
    if neg_ttl > 0:
        data = dict(data)
        data["exp"] = int(time.time()) + neg_ttl
    with _lyrics_mem_lock:
        _lyrics_mem_cache[tid] = data
        if len(_lyrics_mem_cache) > _LYRICS_MEM_CACHE_MAX:
            _lyrics_mem_cache.popitem(last=False)
    try:
        cache_path = LYRICS_DIR / f"{tid}.json"
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"Lyrics cache write failed: {e}")

def _read_lyrics_cache(tid: str) -> dict | None:
    """Read lyrics from in-memory LRU first, then filesystem cache.

    Returns None on miss, or on negative-cache entries whose `exp` field has elapsed
    (in which case the in-memory + on-disk entries are evicted so subsequent calls re-fetch).
    """
    with _lyrics_mem_lock:
        if tid in _lyrics_mem_cache:
            _lyrics_mem_cache.move_to_end(tid)
            return _lyrics_mem_cache[tid]
    try:
        cache_path = LYRICS_DIR / f"{tid}.json"
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            # Negative-cache expiry check — evict + treat as miss once `exp` has elapsed.
            if isinstance(data, dict) and "exp" in data:
                try:
                    if int(time.time()) >= int(data["exp"]):
                        with _lyrics_mem_lock:
                            _lyrics_mem_cache.pop(tid, None)
                        try: cache_path.unlink()
                        except OSError: pass
                        return None
                except (TypeError, ValueError):
                    pass
            with _lyrics_mem_lock:
                _lyrics_mem_cache[tid] = data
                if len(_lyrics_mem_cache) > _LYRICS_MEM_CACHE_MAX:
                    _lyrics_mem_cache.popitem(last=False)
            return data
    except Exception:
        pass
    return None

def _safe_playlist_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name.strip())
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    s = s.strip(". ")
    return s[:80] if s else "Untitled"

def _validate_playlist_path(safe_name: str) -> bool:
    """Ensure resolved path stays under PLAYLISTS_DIR (prevents traversal)."""
    resolved = (PLAYLISTS_DIR / safe_name).resolve()
    return resolved == PLAYLISTS_DIR.resolve() or str(resolved).startswith(str(PLAYLISTS_DIR.resolve()) + os.sep)

def save_album_metadata(playlist_name: str, album_data: dict):
    """Save album metadata to a playlist directory when saving an album."""
    pl_dir = PLAYLISTS_DIR / playlist_name
    pl_dir.mkdir(parents=True, exist_ok=True)
    meta_path = pl_dir / "album.json"
    try:
        meta_path.write_text(
            json.dumps({
                "albumId": album_data.get("albumId", ""),
                "title": album_data.get("title", playlist_name),
                "artist": album_data.get("artist", ""),
                "art": album_data.get("art", ""),
                "trackCount": len(album_data.get("tracks", [])),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _invalidate_file_index()
    except Exception as e:
        log.warning(f"Failed to save album metadata: {e}")

def _write_playlist_meta(track: dict, playlist_name: str, track_number=None) -> str:
    """Synchronously write a track's .meta.json into a playlist dir so the
    playlist is visible (as a pending entry) before the audio download finishes.
    Returns the tid. Safe to call from request threads (not the executor)."""
    pl_dir = PLAYLISTS_DIR / playlist_name
    pl_dir.mkdir(parents=True, exist_ok=True)
    tid = track.get("tid") or get_track_id(track.get("name", ""), track.get("artist", ""))
    if not tid:
        return ""
    meta_path = pl_dir / f"{tid}.meta.json"
    if not meta_path.exists():
        try:
            meta_path.write_text(
                json.dumps({
                    "name": track.get("name", tid),
                    "artist": track.get("artist", "Unknown Artist"),
                    "dur": track.get("dur", 0),
                    "art": track.get("art", ""),
                    "trackNumber": track_number,
                    "videoId": track.get("videoId", ""),
                    "addedAt": time.time(),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            _invalidate_file_index()
        except Exception:
            pass
    return tid


def download_to_playlist(track: dict, playlist_name: str, track_number=None) -> bool:
    pl_dir = PLAYLISTS_DIR / playlist_name
    pl_dir.mkdir(parents=True, exist_ok=True)
    tid = track.get("tid") or get_track_id(track["name"], track["artist"])
    ext_candidates = ["mp3", "m4a", "webm", "opus"]

    if track_number is not None:
        prefix = f"{str(track_number).zfill(2)} - "
    else:
        prefix = ""

    for ext in ext_candidates:
        if (pl_dir / f"{prefix}{tid}.{ext}").exists():
            return True
    # Meta is written synchronously by the caller (api_playlists_add) now, but
    # keep this fallback for direct/legacy callers.
    meta_path = pl_dir / f"{tid}.meta.json"
    if not meta_path.exists():
        try:
            meta_path.write_text(
                json.dumps({
                    "name": track.get("name", tid),
                    "artist": track.get("artist", "Unknown Artist"),
                    "dur": track.get("dur", 0),
                    "art": track.get("art", ""),
                    "trackNumber": track_number,
                    "videoId": track.get("videoId", ""),
                    "addedAt": time.time(),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            _invalidate_file_index()
        except Exception:
            pass
    art_path = pl_dir / f"{tid}.jpg"
    if not art_path.exists() and track.get("art"):
        art_url = track["art"]
        if art_url.startswith(('https://', 'http://')):
            try:
                r = requests.get(art_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.ok:
                    art_path.write_bytes(r.content)
                    _invalidate_file_index()
            except Exception:
                pass
    vid = track.get("videoId", "")
    if vid:
        source = f"https://www.youtube.com/watch?v={vid}"
    else:
        source = f"ytsearch1:{track['artist']} {track['name']} audio"

    if track_number is not None:
        out_name = f"{str(track_number).zfill(2)} - {tid}"
    else:
        out_name = tid

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(pl_dir / out_name),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        **_YDL_COMMON_OPTS,   # keeps quiet=True — progress_hooks still fire regardless
        "noprogress": True,   # silence yt-dlp's own [download] bar; our hook below prints progress
        "http_headers": {"User-Agent": _YDL_USER_AGENT},
        "no_color": True,
        **_ydl_extras(),
        "progress_hooks": [lambda d: print(
            f"\r[playlist] {track.get('name', tid)} — {d.get('_percent_str', '…')} "
            f"({d.get('_speed_str', '')})", end="", flush=True) if d['status'] == 'downloading' else
            print(f"[playlist] {track.get('name', tid)} — processing…", flush=True)
        ],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([source])
        print(f"[playlist] {track.get('name', tid)} — done", flush=True)
        for f in pl_dir.glob(f"{out_name}*"):
            if f.suffix == ".mp3":
                if f.name != f"{out_name}.mp3":
                    f.rename(pl_dir / f"{out_name}.mp3")
                break
            elif f.suffix in [".m4a", ".webm", ".opus"]:
                f.rename(pl_dir / f"{out_name}.mp3")
                break
        _record_download_status(tid, True)
        _invalidate_file_index()
        _invalidate_playlist_cache()
        return True
    except Exception as e:
        log.warning(f"Playlist download failed for {tid}: {e}")
        _record_download_status(tid, False, str(e))
        # Clean up: remove .meta.json so the track doesn't show as "pending" forever
        try:
            if meta_path.exists():
                meta_path.unlink()
        except Exception:
            pass
        # Remove any partial audio files left behind by yt_dlp
        try:
            for f in pl_dir.glob(f"{out_name}*"):
                if f.suffix in (".mp3", ".m4a", ".webm", ".opus", ".part") or f.name.endswith(".temp"):
                    f.unlink(missing_ok=True)
        except Exception:
            pass
        # Remove orphaned artwork file if it exists
        try:
            if art_path.exists():
                art_path.unlink()
        except Exception:
            pass
        _invalidate_file_index()
        _invalidate_playlist_cache()
        return False

# ── TEMP BURST DETECTOR (remove after debugging) ──────────────────────────────
import collections
_REQ_LOG = collections.deque(maxlen=4000)
def _burst_check(endpoint, key=""):
    now = time.time()
    _REQ_LOG.append((now, endpoint, key))
    # window count for same endpoint+key within last 5s
    count = 0
    cutoff = now - 5.0
    for t, ep, k in reversed(_REQ_LOG):
        if t < cutoff:
            break
        if ep == endpoint and (k == "" or k == key):
            count += 1
    if count >= 6:
        print(f"[BURST] {endpoint} key={key!r} x{count} in 5s  >>> LOOP?", flush=True)
# ──────────────────────────────────────────────────────────────────────────────

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    user_agent = request.headers.get("User-Agent", "").lower()
    mobile_words = ["android", "iphone", "ipad", "blackberry", "windows phone"]
    
    # Isolate mobile traffic cleanly without breaking the desktop engine
    if any(word in user_agent for word in mobile_words):
        return render_template("mobile.html")
        
    return render_template("player.html")

@app.route("/api/stream")
def api_stream():
    q = request.args.get("q", "")
    tid = request.args.get("tid", "")
    vid = request.args.get("vid", "")
    force = request.args.get("force", "") in ("1", "true", "yes")
    if q.startswith("http") and "youtube.com" not in q and "youtu.be" not in q:
        return jsonify({"error": "Only YouTube URLs are supported"}), 400
    _c = request.args.get("_c", "")
    print(f"[STREAM] q={q[:60]} tid={tid[:12]} vid={vid[:12]} caller={_c[:60]}", flush=True)
    _burst_check("stream", tid or q[:40])
    if tid:
        for _ext in (".mp3", ".m4a", ".webm", ".opus"):
            if (SAVED_DIR / f"{tid}{_ext}").exists():
                return jsonify({"url": f"/api/local_file?q={tid}{_ext}", "local": True})
        idx = _get_file_index()
        for _ext in (".mp3", ".m4a", ".webm", ".opus"):
            matched = idx.get(f"{tid}{_ext}")
            if matched and matched.is_file():
                return jsonify({"url": f"/api/library_file?q={tid}{_ext}", "local": True})
    try:
        result = get_stream_url(q, tid, vid, force=force)
    except Exception as e:
        print(f"[STREAM] get_stream_url EXCEPTION: {e}", flush=True)
        result = {"error": str(e)}
    if "error" in result:
        print(f"[STREAM] ERROR: {result['error']}", flush=True)
    if "url" in result and not result.get("local") and not result.get("cached"):
        if not tid:
            tid = get_track_id(q, "proxy")
        raw_url = result["url"]
        _cache_stream(tid, result["url"])
        result["url"] = f"/api/proxy_stream?url_key={tid}"
        result["streamUrl"] = raw_url
    elif "url" in result and result.get("cached") and tid:
        raw_url = result["url"]
        result["url"] = f"/api/proxy_stream?url_key={tid}"
        result["streamUrl"] = raw_url
    return jsonify(result)

@app.route("/api/proxy_stream")
def api_proxy_stream():
    url_key = request.args.get("url_key", "")
    with _stream_cache_lock:
        cached = _stream_cache.get(url_key)
    if not cached:
        return jsonify({"error": "Stream URL expired or not found", "stale": True}), 404
    entry = cached if isinstance(cached, dict) else {"url": cached}
    url = entry["url"]
    # Pre-empt: if the YouTube `expire=` token has already passed, evict and signal stale.
    exp = entry.get("exp") or 0
    if exp and exp <= time.time():
        with _stream_cache_lock:
            _stream_cache.pop(url_key, None)
        return jsonify({"error": "Stream URL expired", "stale": True}), 410
    try:
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.youtube.com/",
            "Origin": "https://www.youtube.com",
        }
        range_header = request.headers.get("Range")
        if range_header:
            req_headers["Range"] = range_header
        if _curl:
            upstream = _curl.get(url, headers=req_headers, timeout=15, stream=True)
        else:
            # Reuse _fallback_proxy_session across calls to amortize TCP/TLS handshake.
            # Danger signal: requests.get returned `requests.adapters.HTTPAdapter`-cached
            # conns don't survive hard 5xx; the next call will reconnect — acceptable.
            upstream = _fallback_proxy_session.get(url, headers=req_headers, stream=True, timeout=15)
        if upstream.status_code not in (200, 206):
            # Auto-heal: 403/410 from googlevideo is the classic expired-URL signature.
            # Evict the stale entry so the next /api/stream falls back to yt-dlp re-extract
            # instead of looping on the same dead URL forever.
            if upstream.status_code in (403, 410):
                with _stream_cache_lock:
                    _stream_cache.pop(url_key, None)
                return jsonify({"error": f"Upstream expired ({upstream.status_code})", "stale": True}), 502
            return jsonify({"error": f"Upstream returned {upstream.status_code}"}), 502
        # Root-cause hardening: yt-dlp can return a URL that looks fine but whose
        # upstream is really an HTML/JSON bot-check, consent or "video unavailable"
        # page served as a 200. Streaming that garbage into the <audio> element makes
        # it fail with MEDIA_ERR_SRC_NOT_SUPPORTED (code 4), which previously cascaded
        # into hundreds of re-extracts that all served the same dead URL. Sniff the
        # content type BEFORE streaming: non-media responses are treated as a stale
        # entry (evicted + signalled) so the frontend's candidate fallback can pick a
        # different source instead of choking on HTML bytes.
        ct = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        media_ok = (
            ct.startswith("audio/") or ct.startswith("video/")
            or ct in ("application/octet-stream", "binary/octet-stream", "")
        )
        if not media_ok:
            with _stream_cache_lock:
                _stream_cache.pop(url_key, None)
            print(f"[PROXY] BAD_CONTENT_TYPE url_key={url_key[:12]} ct={ct!r} status={upstream.status_code} — evicted as stale", flush=True)
            try:
                upstream.close()
            except Exception:
                pass
            return jsonify({"error": f"Upstream returned non-media content ({ct})", "stale": True}), 502
        excluded = {"content-encoding", "transfer-encoding", "content-length", "connection"}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass
        return Response(generate(), status=upstream.status_code, headers=resp_headers, mimetype=upstream.headers.get("Content-Type", "audio/mp4"))
    except Exception as e:
        print(f"[PROXY] ERROR: {e}", flush=True)
        return jsonify({"error": str(e)}), 502

@app.route("/api/favorites")
def api_favorites():
    favs = _load_favorites()
    # Batch-check local file existence to minimize syscalls
    audio_tids = {f.get('tid') or get_track_id(f.get('name', ''), f.get('artist', '')) for f in favs}
    existing_audio = {tid for tid in audio_tids if (SAVED_DIR / f"{tid}.mp3").is_file()}
    existing_art = {tid for tid in audio_tids if (SAVED_DIR / f"{tid}.jpg").is_file()}
    for f in favs:
        tid = f.get('tid') or get_track_id(f.get('name', ''), f.get('artist', ''))
        f['tid'] = tid
        f['local_audio'] = tid in existing_audio
        f['local_art'] = tid in existing_art
    return jsonify(favs)

@app.route("/api/save_favorites", methods=["POST"])
def api_save_favorites():
    try:
        favs = request.get_json(force=True)
        FAVORITES_JSON.write_text(json.dumps(favs, ensure_ascii=False, indent=2), encoding="utf-8")
        _invalidate_favorites_cache()
        # Only enqueue favourites that are genuinely missing material locally.
        # The previous version submitted the ENTIRE favourites list to the bounded
        # download executor on every single like/unlike toggle — N yt-dlp task
        # submissions per click even when every track was already downloaded.
        # download_track() early-exits when the audio already exists, but it still
        # paid art_path.exists() + executor scheduling for the whole collection
        # each toggle. Now we filter to the (usually empty) set that actually
        # needs work, so a toggle of an already-downloaded track is ~free.
        for f in favs:
            tid = f.get('tid') or get_track_id(f.get('name', ''), f.get('artist', ''))
            audio_missing = not (SAVED_DIR / f"{tid}.mp3").exists()
            if audio_missing:
                _download_executor.submit(download_track, f)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Listening stats endpoints ────────────────────────────────────────────────
# Frontend pulses POST /api/stats/log with { type, id, title, artist, art, dur, sec }.
#   type="progress"  — periodic actual-listen-seconds accumulator (mid-track).
#   type="play"      — fired once when ≥75% of the track has been heard.
# Both share the same writer; "progress" writes to raw[] (capped, rolled to daily).
# A "play" event is recorded as the canonical count unit (raw[]) and the listen
# seconds attributed to that play are added via a parallel `sec` field.

@app.route("/api/stats/log", methods=["POST"])
def api_stats_log():
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "invalid json"}), 400
    ev_type = (body.get("type") or "").strip()
    tid = (body.get("id") or "").strip()
    if not tid or ev_type not in ("play", "progress"):
        return jsonify({"error": "bad payload"}), 400
    sec = int(body.get("sec", 0) or 0)
    if sec < 0:
        sec = 0
    raw_dur = body.get("dur", 0) or 0
    if isinstance(raw_dur, str) and ":" in raw_dur:
        parts = raw_dur.split(":")
        try:
            if len(parts) == 2:
                dur = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                dur = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                dur = int(raw_dur)
        except (ValueError, IndexError):
            dur = 0
    else:
        try:
            dur = int(raw_dur)
        except (ValueError, TypeError):
            dur = 0
    title = (body.get("title") or "")[:140]
    artist = (body.get("artist") or "")[:120]
    art = (body.get("art") or "")[:400]
    ts = int(time.time() * 1000)

    with _stats_lock:
        data = _load_stats_locked()
        if ev_type == "play":
            # Boundary count — only one raw row per completed play.
            data["raw"].append({
                "id": tid, "title": title, "artist": artist, "art": art,
                "dur": dur, "sec": sec, "ts": ts, "play": 1
            })
        else:
            # Pure listen-time accumulator — no counting toward "plays".
            data["raw"].append({
                "id": tid, "title": title, "artist": artist, "art": art,
                "dur": dur, "sec": sec, "ts": ts, "play": 0
            })
        _rollover_locked(data)
        _save_stats_locked(data)
        _invalidate_stats_cache()
    return jsonify({"ok": True})


@app.route("/api/stats/get")
def api_stats_get():
    global _stats_cache, _stats_cache_ts
    period = (request.args.get("period") or "all").strip().lower()
    pm = {"week": 7, "month": 30, "year": 365}.get(period)
    now = time.time()
    if _stats_cache is not None and (now - _stats_cache_ts) < _STATS_CACHE_TTL and _stats_cache.get("_period") == period:
        return jsonify(_stats_cache)
    with _stats_lock:
        data = _load_stats_locked()
        _rollover_locked(data)
        out = _aggregate_locked(data, pm)
    out["_period"] = period
    _stats_cache = out
    _stats_cache_ts = now
    return jsonify(out)


@app.route("/api/stats/import", methods=["POST"])
def api_stats_import():
    """One-shot migration path for the legacy localStorage `aki_playlog` array.

    Accepts an array of { id, title, artist, art, dur, ts } entries from the client
    and folds them into raw[] with play=1 (counting toward plays) and listen-time
    derived from `dur` per entry (best-guess: assume full duration was heard).
    Idempotent — already-imported tracks are not duplicated thanks to the (id,ts)
    uniqueness check this performs against the existing raw pool."""
    try:
        entries = request.get_json(force=True)
        if not isinstance(entries, list):
            return jsonify({"error": "expected array"}), 400
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    imported = 0
    with _stats_lock:
        data = _load_stats_locked()
        # Check across raw AND rolled tiers so a re-import after a rollover isn't
        # treated as new (a play seconds-old and that rolled entry now live in
        # daily/monthly; (id, day|month) is the natural unique key for those tiers).
        existing = {(e.get("id", ""), e.get("ts", 0)) for e in data["raw"]}
        rolled_day = set()
        for day_key, tracks in data.get("daily", {}).items():
            for tk in tracks.keys():
                rolled_day.add((tk, day_key))
        rolled_month = set()
        for month_key, tracks in data.get("monthly", {}).items():
            for tk in tracks.keys():
                rolled_month.add((tk, month_key))
        for e in entries:
            tid = (e.get("id") or "").strip()
            ts = int(e.get("ts", 0) or 0)
            if not tid or not ts:
                continue
            if (tid, ts) in existing:
                continue
            # Also skip if the same track already has an entry for this exact day
            # or month across any rolled tier (legacy localStorage granularity was
            # at most one entry per (track, second), so the same play event won't
            # appear on two distinct days/months).
            day_key = _ts_to_day(ts)
            month_key = day_key[:7]
            if (tid, day_key) in rolled_day or (tid, month_key) in rolled_month:
                continue
            dur = int(e.get("dur", 0) or 0)
            data["raw"].append({
                "id": tid, "title": (e.get("title", "") or "")[:140],
                "artist": (e.get("artist", "") or "")[:120], "art": (e.get("art", "") or "")[:400],
                "dur": dur, "sec": dur, "ts": ts, "play": 1
            })
            existing.add((tid, ts))
            rolled_day.add((tid, day_key))
            rolled_month.add((tid, month_key))
            imported += 1
        _rollover_locked(data)
        _save_stats_locked(data)
        _invalidate_stats_cache()
    return jsonify({"ok": True, "imported": imported})


@app.route("/api/stats/reset", methods=["POST"])
def api_stats_reset():
    with _stats_lock:
        _save_stats_locked(_empty_stats_doc())
        _invalidate_stats_cache()
    return jsonify({"ok": True})


def _artist_tracks_fallback(artist):
    """Shared search fallback used by both artist routes when get_artist_tracks is empty."""
    try:
        return yt_music_search(artist, limit=7)
    except Exception:
        return []


@app.route("/api/artist")
def api_artist():
    artist = request.args.get("name", "")
    browse_id = request.args.get("browseId", "")
    # Parallelize bio + tracks fetches (independent network calls)
    bio_future = _io_executor.submit(get_artist_bio, artist)
    tracks_future = _io_executor.submit(get_artist_tracks, artist, browse_id)
    try:
        bio = bio_future.result()
    except Exception:
        bio = f"Discover and stream official tracks from {artist} directly through the player."
    try:
        tracks = tracks_future.result()
    except Exception:
        tracks = []
    if not tracks:
        tracks = _artist_tracks_fallback(artist)

    albums = _group_tracks_into_albums(tracks)
    return jsonify({"artist": artist, "bio": bio, "albums": albums, "tracks": tracks})


@app.route("/api/artist/image")
def api_artist_image():
    name = request.args.get("name", "")
    if not name:
        return jsonify({"error": "missing name"}), 400
    art = get_artist_image_cached(name)
    return jsonify({"name": name, "art": art})


@app.route("/api/img-proxy")
def api_img_proxy():
    """Same-origin image proxy so remote album art / avatars always load.

    Why this exists:
      • WebView2 / Edge Tracking Prevention ("Strict") blocks third-party image
        fetches (iTunes mzstatic.com, i.ytimg.com, Last.fm, TMDB) at the
        network layer. The browser doesn't even make the request — it 404s the
        <img>, which previously triggered `imgOnErrorFallback`'s 2x retry
        cascade (3 blocked fetches × N rows × every row scrolling into view),
        saturating the main thread for ~1s on every scroll → the reported
        "freeze ~1s then jump" pattern.
      • Routing every remote art URL through this endpoint makes the fetch
        first-party (http://localhost:5000), which Tracking Prevention never
        blocks. The server fetches the upstream URL, returns bytes with
        strong browser caching so subsequent rows / page loads are instant.

    Properties: https (and http for legacy CDNs) in, https-only out,
      size-capped (10MB — covers 1200² hero art), browser-cacheable for 7 days,
      ETag-less (cache-control alone is enough; URLs include size hashes so
      version skew is impossible), uses curl_cffi Chrome impersonation when
      available so hotlink/Referer-protected CDNs (Apple, Google) succeed.
    """
    import io
    u = request.args.get("u", "")
    if not u:
        return ("", 400)
    # Allow http(s) only — blocks file://, data:, fileURL tricks, SSRF to internal
    # services. Also reject RFC1918/loopback/link-local destinations so a remote
    # <img> (or LAN client) can never turn this endpoint into a proxy into the
    # local network or the host (cloud metadata 169.254.169.254, routers, etc.).
    if not (u.startswith("https://") or u.startswith("http://")):
        return ("", 400)
    try:
        if _is_private_or_link_local(u):
            return ("", 400)
        # Prefer curl_cffi (Chrome TLS impersonation) — bypasses hotlink
        # protections on Apple/Google art CDNs that 403 plain `requests`.
        if _curl is not None:
            r = _curl.get(
                u,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0", "Referer": u},
            )
            status = r.status_code
            ctype = r.headers.get("Content-Type", "image/jpeg")
            raw = r.content
        else:
            r = requests.get(
                u,
                timeout=15,
                stream=True,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0", "Referer": u},
            )
            status = r.status_code
            ctype = r.headers.get("Content-Type", "image/jpeg")
            if status != 200:
                return ("", 404)
            data = io.BytesIO()
            total = 0
            for chunk in r.iter_content(8192):
                total += len(chunk)
                if total > 10 * 1024 * 1024:
                    return ("", 404)
                data.write(chunk)
            raw = data.getvalue()
        if status != 200:
            return ("", 404)
        if not ctype.startswith("image/"):
            # Some CDNs send `application/octet-stream` for .webp/.avif; accept
            # those as images so we don't 404 legits.
            if not ("octet-stream" in ctype or "binary" in ctype):
                return ("", 404)
            ctype = "image/jpeg"
        resp = app.response_class(raw, mimetype=ctype)
        # 7-day browser cache. Combined with the proxy URL including the
        # upstream URL + size param, the LLVM-style URL identity gives perfect
        # cache hit semantics — same source URL always returns the same bytes
        # until the upstream changes (and `?w=` resize URLs are distinct).
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    except Exception as e:
        log.warning(f"img-proxy failed for {u!r}: {e}")
        return ("", 404)


@app.route("/api/artist/bio")
def api_artist_bio():
    artist = request.args.get("name", "")
    if not artist:
        return jsonify({"error": "missing name"}), 400
    try:
        bio = get_artist_bio(artist)
    except Exception:
        bio = ""
    return jsonify({"artist": artist, "bio": bio})


@app.route("/api/artist/tracks")
def api_artist_tracks():
    artist = request.args.get("name", "")
    browse_id = request.args.get("browseId", "")
    if not artist:
        return jsonify({"error": "missing name", "tracks": [], "albums": []}), 400
    try:
        tracks = get_artist_tracks(artist, browse_id)
    except Exception:
        tracks = []
    if not tracks:
        tracks = _artist_tracks_fallback(artist)
    albums = _group_tracks_into_albums(tracks)
    return jsonify({"artist": artist, "albums": albums, "tracks": tracks})


def _group_tracks_into_albums(tracks):
    album_map = {}
    album_order = []
    singles_tracks = []

    for t in tracks:
        album_id = t.get("albumId", "")
        if album_id:
            if album_id not in album_map:
                album_map[album_id] = {
                    "albumId": album_id,
                    "albumTitle": t.get("albumName") or "Unknown Album",
                    "art": t.get("art", ""),
                    "tracks": [],
                }
                album_order.append(album_id)
            album_map[album_id]["tracks"].append(t)
            if not album_map[album_id]["art"] and t.get("art"):
                album_map[album_id]["art"] = t["art"]
        else:
            singles_tracks.append(t)

    albums = [album_map[aid] for aid in album_order]

    if singles_tracks:
        singles_art = ""
        for t in singles_tracks:
            if t.get("art"):
                singles_art = t["art"]
                break
        albums.append({
            "albumId": "singles",
            "albumTitle": "Singles & Appearances",
            "art": singles_art,
            "tracks": singles_tracks,
        })

    return albums


@app.route("/api/album")
def api_album():
    album_id = request.args.get("albumId", "")
    if not album_id:
        return jsonify({"error": "Missing albumId"}), 400
    now = time.time()
    with _album_lock:
        if album_id in _album_cache:
            ts, payload = _album_cache[album_id]
            if now - ts < _ALBUM_TTL:
                _album_cache.move_to_end(album_id)
                return jsonify(payload)
            del _album_cache[album_id]
    try:
        album_data = ytmusic.get_album(album_id)
        title = album_data.get("title", "Unknown Album")
        album_artists = album_data.get("artists") or []
        album_artist = album_artists[0].get("name", "") if album_artists else ""
        album_thumbs = album_data.get("thumbnails") or []
        album_art = _upscale_thumb(album_thumbs[-1]["url"]) if album_thumbs else ""
        items = album_data.get("tracks", [])
        tracks = []
        for idx, e in enumerate(items, start=1):
            if not isinstance(e, dict): continue
            name = e.get("title", "Unknown")
            artists = e.get("artists") or []
            artist = artists[0].get("name", "Unknown Artist") if artists else "Unknown Artist"
            thumbs = e.get("thumbnails") or []
            art = _upscale_thumb(thumbs[-1]["url"]) if thumbs else album_art
            dur = _parse_duration(e)
            vid = e.get("videoId", "")
            tid = get_track_id(name, artist)
            tracks.append(_build_track_dict(name, artist, art, dur, tid, vid, album_id, trackNumber=idx))
        payload = {"title": title, "artist": album_artist, "art": album_art, "tracks": tracks}
        with _album_lock:
            _album_cache[album_id] = (now, payload)
            if len(_album_cache) > _ALBUM_CACHE_MAX:
                _album_cache.popitem(last=False)
        return jsonify(payload)
    except Exception as e:
        log.warning(f"get_album error: {e}")
        return jsonify({"error": str(e), "title": "", "tracks": []})

@app.route("/api/local_file")
def api_local_file():
    q = request.args.get("q", "")
    filename = Path(q).name
    _burst_check("local_file", filename)
    if not filename or filename == "." or filename == ".." or not _SAFE_FILENAME_RE.match(filename):
        return jsonify({"error": "Not found"}), 404
    file_path = SAVED_DIR / filename
    if file_path.exists() and file_path.is_file():
        directory = str(file_path.parent)
        return send_from_directory(directory, filename)
    # Use reverse index instead of rglob
    idx = _get_file_index()
    matched = idx.get(filename)
    if matched and matched.is_file():
        return send_from_directory(str(matched.parent), filename)
    return jsonify({"error": "Not found"}), 404

def _scan_playlists():
    playlists = []
    if PLAYLISTS_DIR.exists():
        for entry in sorted(PLAYLISTS_DIR.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                files = list(entry.iterdir())
                mp3_files = sorted(f for f in files if f.suffix == ".mp3")
                count = len(mp3_files)
                meta_files = sorted(f for f in files if f.name.endswith(".meta.json"))
                # A dir is a real playlist if it has at least one .mp3 (downloaded)
                # OR at least one .meta.json (track queued / downloading).
                # Completely empty dirs (no mp3, no meta) are skipped — those are
                # abandoned create-without-add attempts.
                if count == 0 and not meta_files:
                    continue
                cover_art = ""
                is_album = False
                album_meta = {}
                playlist_meta = {}
                # Read playlist.json (source, spotifyPlaylistId, etc.)
                playlist_meta_file = entry / "playlist.json"
                if playlist_meta_file.exists():
                    try:
                        playlist_meta = json.loads(playlist_meta_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                # Check for explicit cover.jpg first (e.g. Spotify playlist cover)
                local_cover = entry / "cover.jpg"
                if local_cover.exists() and local_cover.stat().st_size > 100:
                    cover_art = f"/api/library_file?q={entry.name}/cover.jpg"
                album_meta_file = entry / "album.json"
                if album_meta_file.exists():
                    try:
                        album_meta = json.loads(album_meta_file.read_text(encoding="utf-8"))
                        if not cover_art:
                            cover_art = album_meta.get("art", "")
                        is_album = True
                    except Exception as e:
                        log.warning(f"Failed to parse album.json in {entry.name}: {e}")
                if not cover_art:
                    # Prefer cover art from a downloaded track's meta first.
                    for f in mp3_files:
                        stem = f.stem
                        if " - " in stem and stem[:2].isdigit():
                            tid = stem.split(" - ", 1)[1]
                        else:
                            tid = stem
                        meta_file = entry / f"{tid}.meta.json"
                        if meta_file.exists():
                            try:
                                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                                cover_art = meta.get("art", "")
                                if cover_art:
                                    break
                            except Exception as e:
                                log.warning(f"Failed to parse meta.json for {tid}: {e}")
                # Fall back to cover art from a pending (not-yet-downloaded) track.
                if not cover_art:
                    for f in meta_files:
                        try:
                            meta = json.loads(f.read_text(encoding="utf-8"))
                            cover_art = meta.get("art", "")
                            if cover_art:
                                break
                        except Exception:
                            pass
                # "count" is the number of downloaded tracks; "pending" is the
                # number of queued tracks whose audio hasn't landed yet.
                pending_count = max(0, len(meta_files) - count)
                entry_data = {
                    "name": entry.name,
                    "count": count,
                    "coverArt": cover_art,
                    "isAlbum": is_album,
                    "albumId": album_meta.get("albumId", ""),
                    "albumArtist": album_meta.get("artist", ""),
                    "pending": pending_count,
                }
                if playlist_meta.get("source"):
                    entry_data["source"] = playlist_meta["source"]
                if playlist_meta.get("spotifyPlaylistId"):
                    entry_data["spotifyPlaylistId"] = playlist_meta["spotifyPlaylistId"]
                playlists.append(entry_data)
    return playlists

def _get_playlist_index():
    global _playlist_index_cache
    with _playlist_index_lock:
        if _playlist_index_cache is None:
            _playlist_index_cache = _scan_playlists()
        return _playlist_index_cache

def _invalidate_playlist_cache():
    global _playlist_index_cache
    with _playlist_index_lock:
        _playlist_index_cache = None

@app.route("/api/playlists")
def api_playlists():
    return jsonify(_get_playlist_index())

@app.route("/api/playlists/create", methods=["POST"])
def api_playlists_create():
    data = request.get_json(force=True) or {}
    raw_name = data.get("name", "")
    safe_name = _safe_playlist_name(raw_name)
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    target = PLAYLISTS_DIR / safe_name
    if target.exists():
        return jsonify({"error": "Playlist already exists"}), 409
    target.mkdir(parents=True, exist_ok=True)
    _invalidate_playlist_cache()
    _invalidate_file_index()
    return jsonify({"success": True, "name": safe_name})

@app.route("/api/playlists/add", methods=["POST"])
def api_playlists_add():
    data = request.get_json(force=True) or {}
    playlist = data.get("playlist", "")
    track = data.get("track", {})
    track_number = data.get("trackNumber")
    album_data = data.get("albumData")
    safe_name = _safe_playlist_name(playlist)
    if not safe_name or not track:
        return jsonify({"error": "Missing playlist or track"}), 400
    if album_data:
        save_album_metadata(safe_name, album_data)
    # Write the meta.json synchronously so the playlist shows up as "pending"
    # immediately when the frontend re-fetches (before the async audio download).
    _write_playlist_meta(track, safe_name, track_number)
    _download_executor.submit(download_to_playlist, track, safe_name, track_number)
    _invalidate_playlist_cache()
    _invalidate_file_index()
    return jsonify({"success": True, "playlist": safe_name})


@app.route("/api/playlists/cover", methods=["POST"])
def api_playlists_cover():
    """Download and save a cover image for a playlist."""
    data = request.get_json(force=True) or {}
    playlist = data.get("playlist", "")
    cover_url = data.get("coverUrl", "")
    safe_name = _safe_playlist_name(playlist)
    if not safe_name or not cover_url:
        return jsonify({"error": "Missing playlist or coverUrl"}), 400
    if not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    pl_dir = PLAYLISTS_DIR / safe_name
    pl_dir.mkdir(parents=True, exist_ok=True)
    cover_path = pl_dir / "cover.jpg"
    try:
        r = requests.get(cover_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok and len(r.content) > 100:
            cover_path.write_bytes(r.content)
            _invalidate_playlist_cache()
            return jsonify({"success": True})
        return jsonify({"error": f"Failed to download cover (HTTP {r.status_code})"}), 502
    except Exception as e:
        log.warning(f"[PLAYLISTS] Cover download failed: {e}")
        return jsonify({"error": f"Download failed: {e}"}), 502


@app.route("/api/playlists/metadata", methods=["POST"])
def api_playlists_metadata():
    """Save playlist metadata (source, spotifyPlaylistId, etc.) to playlist.json."""
    data = request.get_json(force=True) or {}
    playlist = data.get("playlist", "")
    safe_name = _safe_playlist_name(playlist)
    if not safe_name:
        return jsonify({"error": "Missing playlist"}), 400
    if not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    pl_dir = PLAYLISTS_DIR / safe_name
    pl_dir.mkdir(parents=True, exist_ok=True)
    meta_path = pl_dir / "playlist.json"
    meta = {}
    for key in ("source", "spotifyPlaylistId", "description"):
        if key in data:
            meta[key] = data[key]
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        _invalidate_playlist_cache()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlists/enrich_artwork", methods=["POST"])
def api_playlists_enrich_artwork():
    """Batch-enrich artwork for all tracks in a playlist via iTunes.
    Runs in a background thread so the frontend can poll progress."""
    data = request.get_json(force=True) or {}
    playlist = data.get("playlist", "")
    safe_name = _safe_playlist_name(playlist)
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist"}), 400
    pl_dir = PLAYLISTS_DIR / safe_name
    if not pl_dir.exists():
        return jsonify({"error": "Playlist not found"}), 404

    def _enrich_worker():
        meta_files = sorted(f for f in pl_dir.iterdir() if f.name.endswith(".meta.json"))
        print(f"[ENRICH] Starting artwork enrichment for {safe_name}: {len(meta_files)} tracks", flush=True)
        enriched_count = 0
        for mf in meta_files:
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
                # Skip if already has iTunes artwork
                art = meta.get("art", "")
                if art and "mzstatic.com" in art:
                    continue
                # Normalize for fetch_single_itunes_cover
                track = {
                    "name": meta.get("name", ""),
                    "artist": meta.get("artist", ""),
                    "title": meta.get("name", ""),
                    "artist_name": meta.get("artist", ""),
                    "art": art,
                    "album_art": art,
                    "dur": meta.get("dur", 0),
                }
                enriched = fetch_single_itunes_cover(track)
                new_art = enriched.get("art") or enriched.get("album_art", "")
                if new_art and new_art != art:
                    meta["art"] = new_art
                    mf.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                    # Download the artwork file
                    tid = mf.stem.replace(".meta", "")
                    art_path = pl_dir / f"{tid}.jpg"
                    if not art_path.exists() and new_art.startswith(("https://", "http://")):
                        try:
                            r = requests.get(new_art, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                            if r.ok:
                                art_path.write_bytes(r.content)
                        except Exception:
                            pass
                    enriched_count += 1
                    print(f"[ENRICH] {safe_name}: {meta.get('name', tid)} -> {new_art[:80]}", flush=True)
            except Exception as e:
                log.warning(f"Artwork enrichment failed for {mf.name}: {e}")
        _invalidate_file_index()
        _invalidate_playlist_cache()
        print(f"[ENRICH] Done for {safe_name}: {enriched_count}/{len(meta_files)} tracks updated", flush=True)

    _download_executor.submit(_enrich_worker)
    return jsonify({"success": True, "message": "Artwork enrichment started"})


_MP3_BITRATES = {
    (3, 0): (None, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (3, 1): (None, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (2, 0): (None, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (2, 1): (None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (1, 0): (None, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 1): (None, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
}
_MP3_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 1: (11025, 12000, 8000)}


def _mp3_duration_seconds(path) -> float | None:
    """Best-effort MP3 duration in seconds. Skips an ID3v2 tag, reads the
    Xing/Info VBR frame count when present (accurate for VBR), else falls back
    to a CBR estimate from the first frame bitrate. Returns None for non-MP3.
    Cheap: reads at most the first 1MB header region."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as _fh:
            data = _fh.read(1024 * 1024)
    except Exception:
        return None
    if not data:
        return None
    n = len(data)
    pos = 0
    if data[:3] == b"ID3":
        size_tag = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        pos = 10 + size_tag
    first = -1
    for i in range(pos, min(pos + 262144, n - 4)):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            first = i
            break
    if first < 0:
        return None
    hdr = data[first:first + 4]
    ver = (hdr[1] >> 3) & 0x03          # 3=MPEG1, 2=MPEG2, 1=MPEG2.5
    layer = (hdr[1] >> 1) & 0x03        # 1=Layer III, 2=Layer II, 3=Layer I
    crc = not (hdr[1] & 0x01)
    br_idx = (hdr[2] >> 4) & 0x0F
    sr_idx = (hdr[2] >> 2) & 0x03
    key = (ver, layer)
    if key not in _MP3_BITRATES or br_idx in (0, 15) or sr_idx == 3:
        return None
    bitrate_k = _MP3_BITRATES[key][br_idx]
    sr = _MP3_SAMPLE_RATES[ver][sr_idx]
    spf = 384 if layer == 3 else 1152
    frame = data[first:first + 512]
    offsets = []
    if layer == 1:
        offsets.append(4 + (2 if crc else 0) + (32 if ver == 3 else 17))
    else:
        offsets.append(4 + (2 if crc else 0))
    offsets.append(4)
    for off in offsets:
        if len(frame) >= off + 12 and frame[off:off + 4] in (b"Xing", b"Info"):
            flags = struct.unpack(">I", frame[off + 4:off + 8])[0]
            if flags & 1:
                fcount = struct.unpack(">I", frame[off + 8:off + 12])[0]
                if fcount:
                    return fcount * spf / float(sr)
    if bitrate_k:
        return size / (bitrate_k * 1000.0 / 8.0)
    return None


@app.route("/api/playlists/tracks")
def api_playlists_tracks():
    playlist = request.args.get("name", "")
    safe_name = _safe_playlist_name(playlist)
    _burst_check("playlists_tracks", safe_name)
    pl_dir = PLAYLISTS_DIR / safe_name
    if not pl_dir.exists():
        return jsonify([])
    # Collect every tid that already has a downloaded .mp3.
    have_mp3 = set()
    for f in pl_dir.iterdir():
        if f.suffix == ".mp3":
            stem = f.stem
            if " - " in stem:
                prefix = stem.split(" - ", 1)[0]
                if prefix.isdigit():
                    tid = stem.split(" - ", 1)[1]
                else:
                    tid = stem
            else:
                tid = stem
            have_mp3.add(tid)
    tracks = []
    # 1) Downloaded tracks
    for f in sorted(pl_dir.iterdir()):
        if f.suffix == ".mp3" and f.stem and not f.stem.startswith("."):
            stem = f.stem
            if " - " in stem:
                prefix = stem.split(" - ", 1)[0]
                if prefix.isdigit():
                    tid = stem.split(" - ", 1)[1]
                else:
                    tid = stem
            else:
                tid = stem
            art_file = pl_dir / f"{tid}.jpg"
            meta_file = pl_dir / f"{tid}.meta.json"
            name = tid
            artist = "Unknown Artist"
            dur = 0
            art = ""
            track_number = 999
            added_at = f.stat().st_mtime if hasattr(f, "stat") and f.exists() else 0
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    name = meta.get("name", tid)
                    artist = meta.get("artist", "Unknown Artist")
                    dur = meta.get("dur", 0)
                    art = meta.get("art", "")
                    track_number = meta.get("trackNumber", 999)
                    if meta.get("addedAt"):
                        added_at = meta["addedAt"]
                    else:
                        added_at = meta_file.stat().st_mtime
                except Exception:
                    pass
            # Backfill real MP3 duration when the stored one is missing/zero so
            # playlist totals add up correctly (persisted back into meta.json).
            if (not dur or dur <= 0) and f.exists():
                real_dur = _mp3_duration_seconds(f)
                if real_dur:
                    dur = int(round(real_dur))
                    try:
                        if meta_file.exists():
                            _m = json.loads(meta_file.read_text(encoding="utf-8"))
                            _m["dur"] = dur
                            meta_file.write_text(json.dumps(_m, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
            tracks.append({
                "name": name,
                "artist": artist,
                "tid": tid,
                "dur": dur,
                "art": art,
                "trackNumber": track_number,
                "dateAdded": added_at,
                "local_audio": True,
                "local_art": art_file.exists(),
                "playlist": safe_name,
                "pending": False,
            })
    # 2) Pending tracks: have a .meta.json but no .mp3 yet (download queued/failed).
    for f in sorted(pl_dir.iterdir()):
        if f.name.endswith(".meta.json") and f.stem and not f.stem.startswith("."):
            tid = f.stem[:-len(".meta")] if f.stem.endswith(".meta") else f.stem
            if tid in have_mp3:
                continue
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = meta.get("name", tid)
            artist = meta.get("artist", "Unknown Artist")
            dur = meta.get("dur", 0)
            art = meta.get("art", "")
            track_number = meta.get("trackNumber", 999)
            added_at = meta.get("addedAt") or f.stat().st_mtime
            art_file = pl_dir / f"{tid}.jpg"
            tracks.append({
                "name": name,
                "artist": artist,
                "tid": tid,
                "dur": dur,
                "art": art,
                "trackNumber": track_number,
                "dateAdded": added_at,
                "videoId": meta.get("videoId", ""),
                "local_audio": False,
                "local_art": art_file.exists(),
                "playlist": safe_name,
                "pending": True,
            })
            have_mp3.add(tid)  # avoid double-counting if stem parsing was ambiguous
    tracks.sort(key=lambda x: x.get("trackNumber") or 999)
    return jsonify(tracks)

@app.route("/api/library_file")
def api_library_file():
    q = request.args.get("q", "")
    filename = Path(q).name
    if not filename or filename == "." or filename == ".." or not _SAFE_FILENAME_RE.match(filename):
        return jsonify({"error": "Not found"}), 404
    # Use reverse index instead of rglob
    idx = _get_file_index()
    matched = idx.get(filename)
    if matched and matched.is_file():
        return send_from_directory(str(matched.parent), filename)
    return jsonify({"error": "Not found"}), 404

@app.route("/api/settings")
def api_settings():
    s = _load_settings()
    s["app_version"] = APP_VERSION
    # Include changelog hash so the frontend can detect "what's new" on first
    # launch after an update without re-fetching the full CHANGELOG.md.
    try:
        raw = _CHANGELOG_PATH.read_text(encoding="utf-8")
        s["changelog_hash"] = hashlib.md5(raw.encode()).hexdigest()
    except Exception:
        s["changelog_hash"] = ""
    return jsonify(s)


@app.route("/api/settings/wipe", methods=["POST"])
def api_settings_wipe():
    """Reset flat-file user state and clear SAVED cache without removing playlists."""
    global _settings_cache, _settings_cache_ts
    global _community_cache, _community_cache_ts, _pinned_art_cache, _pinned_art_cache_ts

    defaults = {"ui_layout_mode": "card", "cache_limit_gb": 5, "community_showcase_enabled": True}
    failed = 0
    removed = 0

    # Overwrite rather than unlink the core files so their expected schemas remain valid.
    for path, contents in ((FAVORITES_JSON, "[]"), (SETTINGS_JSON, json.dumps(defaults, ensure_ascii=False, indent=2))):
        try:
            path.write_text(contents, encoding="utf-8")
        except OSError:
            failed += 1

    try:
        STATS_JSON.write_text(json.dumps(_empty_stats_doc(), ensure_ascii=False), encoding="utf-8")
        _invalidate_stats_cache()
    except OSError:
        failed += 1

    _invalidate_favorites_cache()
    _settings_cache = dict(defaults)
    _settings_cache_ts = time.time()

    # SAVED is cache/download storage. Preserve music_library (including playlists).
    if SAVED_DIR.exists():
        for path in sorted(SAVED_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    removed += 1
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                failed += 1

    with _lyrics_mem_lock:
        _lyrics_mem_cache.clear()
    with _community_cache_lock:
        _community_cache = None
        _community_cache_ts = 0.0
    with _pinned_art_lock:
        _pinned_art_cache = None
        _pinned_art_cache_ts = 0.0
    with _download_status_lock:
        _download_status.clear()
    _invalidate_stream_cache()

    return jsonify({"ok": True, "removed": removed, "failed": failed, "settings": defaults})

@app.route("/api/settings/toggle_layout", methods=["POST"])
def api_toggle_layout():
    settings = _load_settings()
    current = settings.get("ui_layout_mode", "card")
    settings["ui_layout_mode"] = "list" if current == "card" else "card"
    _save_settings(settings)
    return jsonify(settings)

@app.route("/api/settings/toggle_community_showcase", methods=["POST"])
def api_toggle_community_showcase():
    settings = _load_settings()
    settings["community_showcase_enabled"] = not settings.get("community_showcase_enabled", True)
    _save_settings(settings)
    return jsonify(settings)

@app.route("/api/youtube/refresh_auth", methods=["POST"])
def api_refresh_youtube_auth():
    """Re-read cookies.txt and rebuild YTMusic auth headers. Called after Tauri login."""
    ok = _rebuild_ytmusic_auth()
    if ok:
        return jsonify({"success": True, "message": "YouTube auth refreshed"})
    return jsonify({"success": False, "message": "No valid cookies found"}), 400

@app.route("/api/youtube/auth_status")
def api_youtube_auth_status():
    """Return current YouTube auth state for the Python backend."""
    has_cookies = bool(_resolve_cookie_file())
    has_headers = has_valid_auth_state()
    return jsonify({
        "cookies": has_cookies,
        "ytmusic_auth": has_headers,
        "ytmusic_authenticated": has_headers
    })

@app.route("/api/youtube/liked_songs")
def api_youtube_liked_songs():
    """Fetch the user's liked songs from YouTube Music (requires auth)."""
    if not has_valid_auth_state():
        return jsonify({"error": "Not authenticated", "tracks": []}), 401
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        now = time.time()
        with _liked_lock:
            entry = _liked_cache.get(limit)
            if entry and (now - entry[0]) < _LIKED_TTL:
                return jsonify(entry[1])
        result = ytmusic.get_liked_songs(limit=limit)
        tracks = []
        for t in result.get("tracks", []):
            if not t or t.get("videoId") is None:
                continue
            artists = t.get("artists", [])
            artist_name = artists[0]["name"] if artists else "Unknown Artist"
            name = t.get("title", "Unknown")
            tid = get_track_id(name, artist_name)
            thumbnails = t.get("thumbnails", [])
            art = thumbnails[-1]["url"] if thumbnails else ""
            # Upscale YouTube thumbnails to 600x600 (like search results do)
            art = _WDIM_RE.sub('=w600-h600', art)
            dur_sec = t.get("duration_seconds") or 0
            a_id = (t.get("album") or {}).get("id", "")
            tracks.append(_build_track_dict(name, artist_name, art, dur_sec, tid, t.get("videoId", ""), a_id,
                title=name, artist_name=artist_name, album_art=art, duration=t.get("duration", "")))
        payload = {"tracks": tracks, "total": result.get("trackCount", len(tracks))}
        with _liked_lock:
            _liked_cache[limit] = (now, payload)
        return jsonify(payload)
    except Exception as e:
        log.warning(f"YouTube liked songs fetch failed: {e}")
        return jsonify({"error": str(e), "tracks": []}), 500

@app.route("/api/playlists/delete", methods=["POST"])
def api_playlists_delete():
    data = request.get_json(force=True) or {}
    name = data.get("name", "")
    safe_name = _safe_playlist_name(name)
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    target = PLAYLISTS_DIR / safe_name
    if not target.exists():
        return jsonify({"error": "Playlist not found"}), 404
    try:
        shutil.rmtree(target)
        _invalidate_playlist_cache()
        _invalidate_file_index()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/radio/recommendations")
def api_radio_recommendations():
    vid = request.args.get("vid", "")
    if not vid:
        return jsonify({"error": "Missing vid"}), 400
    now = time.time()
    with _radio_recs_lock:
        if vid in _radio_recs_cache:
            ts, tracks = _radio_recs_cache[vid]
            if now - ts < _RADIO_RECS_TTL:
                _radio_recs_cache.move_to_end(vid)
                return jsonify(tracks[:7])
            del _radio_recs_cache[vid]
    try:
        # Fetch watch playlist which contains recommendations
        data = ytmusic.get_watch_playlist(videoId=vid, limit=10)
        tracks = []
        for e in data.get("tracks", []):
            if not e.get("videoId") or e.get("videoId") == vid:
                continue
            name = e.get("title", "Unknown")
            artists = e.get("artists") or []
            artist = artists[0].get("name", "Unknown Artist") if artists else "Unknown Artist"
            vid_new = e.get("videoId", "")
            thumbs = e.get("thumbnails") or []
            art = _upscale_thumb(thumbs[-1]["url"]) if thumbs else f"https://img.youtube.com/vi/{vid_new}/sddefault.jpg"
            dur = _parse_duration(e)
            tid = get_track_id(name, artist)
            album = e.get("album") or {}
            tracks.append(_build_track_dict(name, artist, art, dur, tid, vid_new, album.get("id") or "",
                artist_name=artist, title=name))
        tracks = tracks[:7]
    except Exception as e:
        log.warning(f"Radio recommendations error: {e}")
        tracks = []
    with _radio_recs_lock:
        _radio_recs_cache[vid] = (now, tracks)
        if len(_radio_recs_cache) > _RADIO_RECS_CACHE_MAX:
            _radio_recs_cache.popitem(last=False)
    return jsonify(tracks)

def standardize_track(track):
    """Ensures a track object has consistent keys for the frontend."""
    # Standardize Artist Name
    artists = track.get("artists")
    a_name = "Unknown Artist"
    if artists and isinstance(artists, list) and len(artists) > 0:
        a_name = artists[0].get("name") or "Unknown Artist"
    else:
        a_name = (
            track.get("artist_name") or
            track.get("artist") or
            track.get("uploader") or
            track.get("author") or
            track.get("name") or
            "Unknown Artist"
        )
    
    # Standardize Title
    title = track.get("title") or track.get("name") or "Unknown"
    
    # Handle Artist type results
    if track.get("type") == "artist" or track.get("resultType") == "artist":
        track["artist_name"] = a_name
        track["title"] = a_name
    else:
        track["artist_name"] = a_name
        track["title"] = title

    # Ensure backward compatibility keys
    track["name"] = track["title"]
    track["artist"] = track["artist_name"]
    
    # Standardize Artwork
    thumbs = track.get("thumbnails")
    current_art = ""
    if thumbs and isinstance(thumbs, list) and len(thumbs) > 0:
        current_art = thumbs[-1].get("url") or ""
    
    if not current_art:
        current_art = track.get("album_art") or track.get("art") or track.get("thumbnail") or ""
    
    if current_art:
        # Don't overwrite if we already have what looks like high-res itunes art
        if "mzstatic.com" in current_art or "itunes.apple.com" in current_art:
            track["album_art"] = current_art
            track["art"] = current_art
        else:
            track["art"] = current_art
            # If it's a youtube thumb, upscale it as a baseline
            if "googleusercontent.com" in current_art or "ytimg.com" in current_art or "img.youtube.com" in current_art:
                track["album_art"] = _upscale_thumb(current_art)
            else:
                track["album_art"] = current_art
    
    # Standardize Duration
    if not track.get("dur"):
        track["dur"] = _parse_duration(track)
    
    # Standardize Track ID
    if not track.get("tid"):
        track["tid"] = get_track_id(track["title"], track["artist_name"])
        
    return track

def fetch_single_itunes_cover(track):
    """
    Exclusive resolver for high-res artwork and standardized metadata.
    """
    track = dict(track)
    track = standardize_track(track)
    
    # Skip iTunes for artist results
    if track.get("type") == "artist" or track.get("resultType") == "artist":
        return track

    # Skip iTunes API for already-enhanced results (iTunes search already provided metadata)
    if track.get("enhanced"):
        if not track.get("duration"):
            yt_duration = track.get('dur') if track.get('dur') is not None else (track.get('duration') or track.get('length'))
            if isinstance(yt_duration, int):
                minutes = yt_duration // 60
                seconds = yt_duration % 60
                track['duration'] = f"{minutes}:{seconds:02d}"
            else:
                track['duration'] = yt_duration or "3:30"
        return track
        
    title = track["title"]
    artist_name = track["artist_name"]
    
    # Clean up title query string
    clean_title = title.split('(')[0].split('-')[0].split('[')[0].strip()
    query_key = f"{artist_name.lower().strip()}|{clean_title.lower().strip()}"
    
    # Check cache first
    with _itunes_art_lock:
        cached = _itunes_art_cache.get(query_key)
        if cached:
            _itunes_art_cache.move_to_end(query_key)
    if cached:
        track['album_art'] = cached.get('album_art', track.get('album_art', ''))
        track['art'] = cached.get('art', track.get('art', ''))
        if cached.get('artist_name'):
            track['artist_name'] = cached['artist_name']
            track['artist'] = cached['artist_name']
        if cached.get('dur'):
            track['dur'] = cached['dur']
        if cached.get('duration'):
            track['duration'] = cached['duration']
        track['enhanced'] = True
        return track

    duration_string = None
    
    # Build a list of candidate query terms — start with the cleaned title, fall back to the original
    # full title if the cleaned version returns no results (e.g. tracks with "(feat. X)" or "- Live").
    candidate_titles = [clean_title]
    if title != clean_title:
        candidate_titles.append(title)
    # Also try just the raw title with no parentheses/brackets but keeping hyphens (for "A - B" style names)
    paren_stripped = title.split('(')[0].split('[')[0].strip()
    if paren_stripped and paren_stripped not in candidate_titles:
        candidate_titles.append(paren_stripped)

    found_result = False
    for attempt in range(3):
        got_429 = False
        for q_title in candidate_titles:
            try:
                if not _itunes_semaphore.acquire(timeout=10):
                    continue
                try:
                    url = "https://itunes.apple.com/search"
                    params = {"term": f"{artist_name} {q_title}", "entity": "song", "limit": 5}
                    response = _itunes_session.get(url, params=params, timeout=5)
                    
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 5))
                        time.sleep(min(retry_after, 8))
                        got_429 = True
                        break  # restart candidate loop after backoff
                    
                    if response.status_code == 200:
                        data = response.json()
                        results = data.get('results', [])
                        if results:
                            result = results[0]
                            
                            # Extract high-res artwork
                            low_res_url = result.get('artworkUrl100', '')
                            if low_res_url:
                                high_res_url = low_res_url.replace('/100x100bb.jpg', '/600x600bb.jpg')
                                track['album_art'] = high_res_url
                                track['art'] = high_res_url
                            
                            if result.get('artistName'):
                                track['artist_name'] = result['artistName']
                                track['artist'] = result['artistName']
                            
                            millis = result.get('trackTimeMillis')
                            if millis:
                                total_seconds = int(millis / 1000)
                                minutes = total_seconds // 60
                                seconds = total_seconds % 60
                                duration_string = f"{minutes}:{seconds:02d}"
                                track['dur'] = total_seconds
                            
                            track['enhanced'] = True
                            found_result = True
                            
                            # Cache the result
                            with _itunes_art_lock:
                                _itunes_art_cache[query_key] = {
                                    'album_art': track.get('album_art', ''),
                                    'art': track.get('art', ''),
                                    'artist_name': track.get('artist_name', ''),
                                    'dur': track.get('dur'),
                                    'duration': duration_string,
                                }
                                if len(_itunes_art_cache) > _ITUNES_ART_CACHE_MAX:
                                    _itunes_art_cache.popitem(last=False)
                            break  # success — stop trying candidates
                        # No results for this candidate — try the next one (if any)
                        continue
                    else:
                        break  # non-200, non-429 — don't retry
                finally:
                    _itunes_semaphore.release()
            except Exception as e:
                log.debug(f"iTunes lookup failed for {title}: {e}")
                break
        if found_result or not got_429:
            break  # done (success or exhausted candidates without 429)
            
    # --- Fallback: Extract YouTube Music native duration if iTunes parsing failed ---
    if not duration_string:
        yt_duration = track.get('dur') or track.get('duration') or track.get('length')
        if isinstance(yt_duration, int):
            minutes = yt_duration // 60
            seconds = yt_duration % 60
            duration_string = f"{minutes}:{seconds:02d}"
        elif isinstance(yt_duration, str) and ":" in yt_duration:
            duration_string = yt_duration
        else:
            duration_string = "3:30"

    track['duration'] = duration_string
    return track

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    filter_type = request.args.get("filter", "all")
    _burst_check("search", f"{filter_type}:{q[:40]}")

    # Dispatch by filter. Cover enhancer only handles track-shape; album / artist
    # shapes have browseId / album fields instead of title / artist_name, and
    # would KeyError inside fetch_single_itunes_cover's iTunes lookup.
    if filter_type == "all":
        # Itunes search already returns high-res artwork; standardize so the
        # frontend gets uniform schema keys (name, artist, tid, dur, etc.)
        results = itunes_search(q, limit=15)
        return jsonify([standardize_track(dict(t)) for t in results])

    if filter_type in ("track", "album", "artist"):
        results = yt_music_search_filtered(q, filter_type)
    else:
        # Unknown filter string: graceful fallback, treat as itunes "all".
        results = itunes_search(q, limit=15)
        return jsonify([standardize_track(dict(t)) for t in results])

    if filter_type == "track":
        # Track path: yt-music returns schemaless dicts → standardize →
        # parallel iTunes cover enhancement for high-res artwork.
        processed_results = list(_io_executor.map(fetch_single_itunes_cover, results))
        return jsonify(processed_results)

    # Album / Artist path: bypass the cover enhancer (shape mismatch).
    # Return as-is from yt-music; consumer renders browseId for navigation.
    return jsonify(results)


@app.route("/api/radio/suggest")
def api_radio_suggest():
    vid = request.args.get("vid", "")
    active_queue = request.args.get("active_queue", "")
    history = request.args.get("history", "")

    active_queue_ids = set(active_queue.split(",")) if active_queue else set()
    recent_history_buffer = set(history.split(",")) if history else set()

    if not vid:
        return jsonify({"error": "Missing vid"}), 400

    try:
        sanitized_tracks = _radio_suggest_tracks(vid)
    except Exception as e:
        log.warning(f"Radio suggest error: {e}")
        sanitized_tracks = []

    # 4. Filter Deduplication (re-applied per-request against current queue/history)
    sanitized_suggestions = [
        track for track in sanitized_tracks
        if track.get('videoId') not in active_queue_ids and track.get('videoId') not in recent_history_buffer
    ][:5]

    return jsonify(sanitized_suggestions)


def _radio_suggest_tracks(vid):
    """Resolve radio suggestion tracks for a seed vid (watch playlist + iTunes art),
    cached per vid with TTL so repeated seeds don't re-hit YouTube Music / Apple."""
    now = time.time()
    with _radio_suggest_lock:
        if vid in _radio_suggest_cache:
            ts, tracks = _radio_suggest_cache[vid]
            if now - ts < _RADIO_SUGGEST_TTL:
                _radio_suggest_cache.move_to_end(vid)
                return tracks
            del _radio_suggest_cache[vid]

    # 1. Primary: Native YouTube Music Watch Playlist (Related)
    try:
        data = ytmusic.get_watch_playlist(videoId=vid, limit=15)
        incoming_suggestions = data.get("tracks", []) or []
    except Exception as e:
        log.warning(f"Radio suggest error (get_watch_playlist): {e}")
        incoming_suggestions = []

    # 2. Fallback: Search by artist if related returns empty or fails
    if not incoming_suggestions:
        try:
            track_info = ytmusic.get_song(vid)
            artist = track_info.get("videoDetails", {}).get("author", "")
            if artist:
                search_results = ytmusic.search(f"{artist} radio", filter="songs", limit=15)
                incoming_suggestions = search_results or []
        except Exception as e:
            log.warning(f"Radio suggest fallback error: {e}")

    # Preliminary normalization for filtering
    raw_list = []
    for e in incoming_suggestions:
        v_id = e.get("videoId")
        if not v_id or v_id == vid:
            continue

        name = e.get("title", "Unknown")
        artists = e.get("artists") or []
        artist = artists[0].get("name", "Unknown Artist") if artists else "Unknown Artist"
        thumbs = e.get("thumbnails") or []
        art = _upscale_thumb(thumbs[-1]["url"]) if thumbs else f"https://img.youtube.com/vi/{v_id}/sddefault.jpg"
        dur = _parse_duration(e)
        tid = get_track_id(name, artist)

        raw_list.append(_build_track_dict(name, artist, art, dur, tid, v_id,
            (e.get("album") or {}).get("id") or "", title=name, artist_name=artist))

    # 3. Parallel iTunes Artwork Resolution
    sanitized_tracks = list(_io_executor.map(fetch_single_itunes_cover, raw_list))

    with _radio_suggest_lock:
        _radio_suggest_cache[vid] = (now, sanitized_tracks)
        if len(_radio_suggest_cache) > _RADIO_SUGGEST_CACHE_MAX:
            _radio_suggest_cache.popitem(last=False)
    return sanitized_tracks

@app.route("/api/lyrics")
def api_lyrics():
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    vid = request.args.get("videoId", "")
    album = request.args.get("album", "")
    dur_str = request.args.get("duration")
    _burst_check("lyrics", f"{title[:30]} | {artist[:20]}")
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    
    duration = None
    if dur_str and dur_str != "undefined":
        try: duration = int(float(dur_str))
        except (ValueError, TypeError):
            log.debug(f"Could not parse duration string: {dur_str}")

    tid = get_track_id(title, artist)

    # 0. Check cache first (memory → disk) — unless force=1 bypass is requested.
    if not force:
        cached = _read_lyrics_cache(tid)
        if cached:
            out = {k: v for k, v in cached.items() if k != "exp"}
            return jsonify(out)

    def is_non_ascii(s):
        return any(ord(c) > 127 for c in s)

    def clean_query(q):
        # L5: full normalisation — NFKC, parenthesised & bare feat./remaster tags,
        # `&`/`and` fold, internal whitespace collapse, `- Topic` suffix.
        q = unicodedata.normalize("NFKC", q)
        q = q.replace(" & ", " and ")
        # Parenthesised (feat./ft./featuring/vs./with/official/lyric/video/...)
        q = re.sub(r'[\(\[][Ff](?:eat|t)\.?.*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Ff]eaturing\s+.*?[\)\]]', '', q, flags=re.IGNORECASE)
        q = re.sub(r'[\(\[][Vv]s\..*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Ww]ith\s+.*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Oo]fficial.*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Ll]yric.*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Vv]ideo.*?[\)\]]', '', q)
        # Bare (no parens) feat./ft./featuring — match up to end or next ` - ` separator
        q = re.sub(r'\s+[Ff](?:eat|t)\.?\s+.*?(?=\s+-\s|$)', '', q)
        q = re.sub(r'\s+[Ff]eaturing\s+.*?(?=\s+-\s|$)', '', q, flags=re.IGNORECASE)
        q = re.sub(r'-?\s*(Remaster(?:ed)?\s*\d{0,4}|Deluxe|Expanded|Bonus|Live|Acoustic|Cover|Version)', '', q, flags=re.IGNORECASE)
        q = q.replace(" - トピック", "").replace(" - Topic", "")
        q = re.sub(r'\s+', ' ', q)
        return q.strip()

    search_title = clean_query(title)
    search_artist = clean_query(artist)

    # 0b. Normalization — resolve non-ASCII artist names to English equivalents
    if is_non_ascii(search_artist):
        try:
            yt_norm = ytmusic.search(f"{search_artist} {search_title}", filter="songs", limit=1)
            if yt_norm and yt_norm[0].get("artists"):
                eng_artist = yt_norm[0]["artists"][0].get("name")
                if eng_artist and not is_non_ascii(eng_artist):
                    search_artist = eng_artist
        except Exception as e:
            log.debug(f"Artist normalization search failed: {e}")

    full_query = f"{search_artist} {search_title}"
    result = {"synced": False, "lines": []}

    # ── Helper: parse LRCLIB response into result dict ──
    def _parse_lrclib_response(data):
        if data.get("syncedLyrics"):
            parsed = _parse_lrc(data["syncedLyrics"])
            if parsed:
                return {"synced": True, "lines": parsed, "_source": "lrclib", "_duration": data.get("duration")}
        if data.get("plainLyrics"):
            return {"synced": False, "lines": [{"time": 0.0, "text": data["plainLyrics"]}], "_source": "lrclib", "_duration": data.get("duration")}
        return None

    # ── Tier 1: Race LRCLIB strict + syncedlyrics (expanded provider list) in parallel ──
    lrclib_url = "https://lrclib.net/api/get"
    lrclib_params = {"track_name": search_title, "artist_name": search_artist}
    if album:
        lrclib_params["album_name"] = album
    if duration:
        lrclib_params["duration"] = duration

    def _fetch_lrclib_strict():
        try:
            r = _lrclib_session.get(lrclib_url, params=lrclib_params, timeout=4)
            if r.ok:
                return _parse_lrclib_response(r.json())
        except Exception as e:
            log.debug(f"LRCLIB strict lookup failed: {e}")
        return None

    def _fetch_syncedlyrics():
        # L1: Expanded provider list. NetEase/Megalobiz retain Asian-market primacy;
        #     Western providers cover the long tail. The two batches are tried sequentially
        #     so a slow NetEase response can't starve Western providers of budget — the
        #     outer threadfuture's timeout (8s, raised in the join below) caps total wall time.
        try:
            import syncedlyrics
            # NetEase/Megalobiz first — Asian-market primacy preserved.
            try:
                lrc = syncedlyrics.search(full_query, providers=["NetEase", "Megalobiz"])
                if lrc and "[" in lrc and ":" in lrc:
                    parsed = _parse_lrc(lrc)
                    if parsed:
                        return {"synced": True, "lines": parsed, "_source": "syncedlyrics_em"}
                if lrc and lrc.strip():
                    return {"synced": False, "lines": [{"time": 0.0, "text": lrc.strip()}], "_source": "syncedlyrics_em"}
            except Exception as e:
                log.debug(f"syncedlyrics (EM) search failed: {e}")
            # Western-market fallback: Musixmatch, Deezer, Genius, Lyricsify, LRCLIB.
            try:
                lrc = syncedlyrics.search(full_query, providers=["Musixmatch", "Deezer", "Genius", "Lyricsify", "LRCLIB"])
                if lrc and "[" in lrc and ":" in lrc:
                    parsed = _parse_lrc(lrc)
                    if parsed:
                        return {"synced": True, "lines": parsed, "_source": "syncedlyrics_western"}
                if lrc and lrc.strip():
                    return {"synced": False, "lines": [{"time": 0.0, "text": lrc.strip()}], "_source": "syncedlyrics_western"}
            except Exception as e:
                log.debug(f"syncedlyrics (Western) search failed: {e}")
        except Exception as e:
            log.debug(f"syncedlyrics import failed: {e}")
        return None

    # L3: Collect BOTH Tier-1 candidates before deciding — no early-exit on race-winner.
    #     Mislabelled syncedlyrics results can no longer beat a duration-matched LRCLIB synced lyric.
    t1_futures = [_io_executor.submit(_fetch_lrclib_strict), _io_executor.submit(_fetch_syncedlyrics)]
    t1_candidates = []
    for f in t1_futures:
        try:
            res = f.result(timeout=8)
            if res:
                t1_candidates.append(res)
        except Exception:
            pass

    def _dur_score(cand):
        """L3: how far the candidate's duration is from the target (0 best). 5 = unknown."""
        cd = cand.get("_duration")
        if not duration or not isinstance(cd, (int, float)):
            return 5
        return abs(cd - duration)

    def _rank(cands):
        """L3: prefer synced > plain; within each, prefer closest-duration match."""
        if not cands:
            return None
        synced = [c for c in cands if c.get("synced")]
        plain = [c for c in cands if not c.get("synced")]
        if synced:
            # Smallest duration delta wins; ties broken by source preference (lrclib is strictest).
            return sorted(synced, key=lambda c: (_dur_score(c), 0 if c.get("_source") == "lrclib" else 1))[0]
        if plain:
            return sorted(plain, key=lambda c: (_dur_score(c), 0 if c.get("_source") == "lrclib" else 1))[0]
        return None

    best = _rank(t1_candidates)
    if best:
        out = {"synced": best["synced"], "lines": best["lines"]}
        _write_lyrics_cache(tid, out)
        return jsonify(out)

    # ── Tier 2: LRCLIB search fallback (fuzzy, validate duration) ──
    t2_candidates = []
    try:
        r = _lrclib_session.get("https://lrclib.net/api/search", params={"q": full_query}, timeout=4)
        if r.ok:
            t2_candidates = r.json() or []
    except Exception as e:
        log.debug(f"LRCLIB search fallback failed: {e}")

    if t2_candidates:
        if duration:
            exact = [c for c in t2_candidates if isinstance(c.get("duration"), (int, float)) and abs(c["duration"] - duration) <= 5]
            if exact:
                t2_candidates = exact
        # Synced first.
        for item in t2_candidates:
            if item.get("syncedLyrics"):
                parsed = _parse_lrc(item["syncedLyrics"])
                if parsed:
                    out = {"synced": True, "lines": parsed}
                    _write_lyrics_cache(tid, out)
                    return jsonify(out)
        # Plain next.
        for item in t2_candidates:
            if item.get("plainLyrics"):
                out = {"synced": False, "lines": [{"time": 0.0, "text": item["plainLyrics"]}]}
                _write_lyrics_cache(tid, out)
                return jsonify(out)

    # ── Tier 3: YTMusic plain lyrics (final resort) ──
    if not vid:
        try:
            yt_res = ytmusic.search(full_query, filter="songs", limit=1)
            if yt_res: vid = yt_res[0].get("videoId")
        except Exception as e:
            log.debug(f"YTMusic search for lyrics failed: {e}")
    if vid:
        try:
            watch_playlist = ytmusic.get_watch_playlist(videoId=vid)
            lyrics_id = watch_playlist.get("lyrics")
            if lyrics_id:
                lyrics_data = ytmusic.get_lyrics(lyrics_id)
                if lyrics_data.get("lyrics"):
                    out = {"synced": False, "lines": [{"time": 0.0, "text": lyrics_data["lyrics"]}]}
                    _write_lyrics_cache(tid, out)
                    return jsonify(out)
        except Exception as e:
            log.debug(f"YTMusic lyrics fetch failed: {e}")

    # Tier 4 fall-through: nothing found — negative-cache for 7 days so repeated plays of
    # the same unlyricked track don't re-hit every provider each time. (L2)
    _write_lyrics_cache(tid, result, neg_ttl=7 * 24 * 3600)
    return jsonify(result)

@app.route("/api/download/status")
def api_download_status():
    with _download_status_lock:
        return jsonify(dict(_download_status))

@app.route("/api/cache/config", methods=["GET", "POST"])
def api_cache_config():
    settings = _load_settings()
    if request.method == "POST":
        try:
            body = request.get_json(force=True)
            limit = body.get("cache_limit_gb")
            if limit is not None:
                settings["cache_limit_gb"] = max(0, min(100, int(limit)))
                _save_settings(settings)
        except Exception:
            pass
    limit = settings.get("cache_limit_gb", 5)
    # Compute current SAVED/ usage
    saved_bytes = 0
    saved_count = 0
    if SAVED_DIR.exists():
        for f in SAVED_DIR.iterdir():
            if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus"):
                try:
                    saved_bytes += f.stat().st_size
                    saved_count += 1
                except OSError:
                    pass
    return jsonify({
        "cache_limit_gb": limit,
        "current_bytes": saved_bytes,
        "current_formatted": _fmt_size(saved_bytes),
        "track_count": saved_count,
    })

@app.route("/api/cache/clean", methods=["POST"])
def api_cache_clean():
    settings = _load_settings()
    limit_gb = settings.get("cache_limit_gb", 5)
    if limit_gb <= 0:
        return jsonify({"pruned": 0, "freed": 0, "freed_formatted": "0 B", "message": "Cache limit is unlimited"})
    limit_bytes = limit_gb * 1073741824

    # Parse active track tid from request body
    active_tid = None
    try:
        body = request.get_json(force=True)
        active_tid = body.get("active_tid")
    except Exception:
        pass

    # Load favorite tids for protection
    fav_tids = set()
    if FAVORITES_JSON.exists():
        try:
            favs = json.loads(FAVORITES_JSON.read_text(encoding="utf-8"))
            for fav in favs:
                t = fav.get("tid") or get_track_id(fav.get("name", ""), fav.get("artist", ""))
                if t:
                    fav_tids.add(t)
        except Exception:
            pass

    # Load playlist tids for protection
    playlist_tids = set()
    if PLAYLISTS_DIR.exists():
        for pl_dir in PLAYLISTS_DIR.iterdir():
            if not pl_dir.is_dir():
                continue
            for f in pl_dir.iterdir():
                if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus"):
                    tid = f.stem
                    if " - " in tid:
                        prefix = tid.split(" - ", 1)[0]
                        if prefix.isdigit():
                            tid = tid.split(" - ", 1)[1]
                    playlist_tids.add(tid)

    # Scan SAVED/ for evictable audio files
    audio_extensions = {".mp3", ".m4a", ".webm", ".opus"}
    evictable = []
    total_bytes = 0
    if SAVED_DIR.exists():
        for f in SAVED_DIR.iterdir():
            if not f.is_file() or f.suffix.lower() not in audio_extensions:
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            total_bytes += size
            tid = f.stem
            # Protection checks
            if tid == active_tid:
                continue
            if tid in fav_tids:
                continue
            if tid in playlist_tids:
                continue
            evictable.append((f, size, tid))

    if total_bytes <= limit_bytes:
        return jsonify({"pruned": 0, "freed": 0, "freed_formatted": "0 B", "message": "Cache is within limit"})

    # Sort oldest first (by mtime)
    evictable.sort(key=lambda x: x[0].stat().st_mtime)

    target_free = total_bytes - limit_bytes
    freed = 0
    pruned = 0
    for f, size, tid in evictable:
        if freed >= target_free:
            break
        try:
            f.unlink()
            freed += size
            pruned += 1
            # Also delete matching artwork
            art = SAVED_DIR / f"{tid}.jpg"
            if art.exists():
                try:
                    freed += art.stat().st_size
                    art.unlink()
                except OSError:
                    pass
        except OSError:
            continue

    return jsonify({
        "pruned": pruned,
        "freed": freed,
        "freed_formatted": _fmt_size(freed),
        "message": f"Pruned {pruned} tracks, freed {_fmt_size(freed)}",
    })

@app.route("/api/downloads/status")
def api_downloads_status():
    global _downloads_status_cache, _downloads_status_cache_ts
    # Serve cached result if fresh (expensive filesystem scan)
    now = time.time()
    if _downloads_status_cache is not None and (now - _downloads_status_cache_ts) < _DOWNLOADS_STATUS_CACHE_TTL:
        return jsonify(_downloads_status_cache)

    active = []
    completed = []
    failed = []
    orphaned_parts = []
    audio_extensions = {".mp3", ".m4a", ".webm", ".opus"}
    total_bytes = 0

    # Build a comprehensive tid → {name, artist} lookup from all sources
    name_map = {}

    # Source 1: favorites.json
    if FAVORITES_JSON.exists():
        try:
            favs = json.loads(FAVORITES_JSON.read_text(encoding="utf-8"))
            for fav in favs:
                t = fav.get("tid") or get_track_id(fav.get("name", ""), fav.get("artist", ""))
                if t:
                    name_map[t] = {"name": fav.get("name", ""), "artist": fav.get("artist", "")}
        except Exception:
            pass

    # Source 2: all .meta.json files in playlists
    if PLAYLISTS_DIR.exists():
        for pl_dir in PLAYLISTS_DIR.iterdir():
            if not pl_dir.is_dir():
                continue
            for meta_file in pl_dir.glob("*.meta.json"):
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    tid = meta_file.stem.replace(".meta", "")
                    name_map[tid] = {"name": meta.get("name", ""), "artist": meta.get("artist", "")}
                except Exception:
                    pass

    # Scan all audio directories
    def _scan_dir(root):
        nonlocal total_bytes
        if not root.exists():
            return
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            suffix = f.suffix.lower()
            if suffix == ".part":
                tid = f.stem
                # Check if a completed audio file exists for this tid — orphaned .part
                has_complete = any(
                    (f.parent / f"{tid}{ext}").exists()
                    for ext in [".mp3", ".m4a", ".webm", ".opus"]
                )
                # Also check with numbered prefix (playlist tracks)
                if not has_complete:
                    for prefix_file in f.parent.glob(f"* - {tid}.*"):
                        if prefix_file.suffix.lower() in audio_extensions:
                            has_complete = True
                            break
                # Also treat as orphaned if file is >5 min old (download was killed)
                is_stale = has_complete or (time.time() - f.stat().st_mtime > 300)
                if is_stale:
                    orphaned_parts.append(str(f.relative_to(BASE_DIR)))
                    try:
                        f.unlink()
                    except Exception:
                        pass
                    continue
                info = name_map.get(tid, {"name": tid, "artist": ""})
                active.append({
                    "tid": tid,
                    "name": info["name"] or tid,
                    "artist": info["artist"],
                    "size": size,
                    "filename": f.name,
                    "path": str(f.relative_to(BASE_DIR)),
                })
            elif suffix in audio_extensions:
                tid = f.stem
                if " - " in tid and tid[:2].isdigit():
                    tid = tid.split(" - ", 1)[1]
                # Find matching artwork and add its size to the track
                art_size = 0
                art_file = f.parent / f"{tid}.jpg"
                if art_file.exists():
                    try:
                        art_size = art_file.stat().st_size
                    except OSError:
                        pass
                total_bytes += size + art_size
                info = name_map.get(tid, {"name": tid, "artist": ""})
                completed.append({
                    "tid": tid,
                    "name": info["name"] or tid,
                    "artist": info["artist"],
                    "filename": f.name,
                    "size": size + art_size,
                    "audioSize": size,
                    "artSize": art_size,
                    "path": str(f.relative_to(BASE_DIR)),
                })

    _scan_dir(SAVED_DIR)
    if PLAYLISTS_DIR.exists():
        for pl_dir in PLAYLISTS_DIR.iterdir():
            if pl_dir.is_dir():
                _scan_dir(pl_dir)

    with _download_status_lock:
        for tid, status in _download_status.items():
            if status and not status.get("ok") and status.get("error"):
                already = any(d["tid"] == tid for d in failed)
                if not already:
                    info = name_map.get(tid, {"name": tid, "artist": ""})
                    failed.append({
                        "tid": tid,
                        "name": info["name"] or tid,
                        "artist": info["artist"],
                        "error": status["error"],
                    })

    result = {
        "active": active,
        "completed": completed,
        "failed": failed,
        "totalBytes": total_bytes,
        "totalFormatted": _fmt_size(total_bytes),
        "completedCount": len(completed),
        "activeCount": len(active),
        "orphanedCleaned": len(orphaned_parts),
    }
    _downloads_status_cache = result
    _downloads_status_cache_ts = time.time()
    return jsonify(result)

# ── Community discover helpers ───────────────────────────────────────────────

def _load_community_cache() -> dict:
    global _community_cache, _community_cache_ts
    now = time.time()
    if _community_cache is not None and (now - _community_cache_ts) < _COMMUNITY_CACHE_TTL:
        return dict(_community_cache)
    if COMMUNITY_CACHE_JSON.exists():
        try:
            _community_cache = json.loads(COMMUNITY_CACHE_JSON.read_text(encoding="utf-8"))
        except Exception:
            _community_cache = {}
    else:
        _community_cache = {}
    _community_cache_ts = now
    return dict(_community_cache)

def _save_community_cache(data: dict):
    global _community_cache, _community_cache_ts
    try:
        COMMUNITY_CACHE_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Failed to write community cache: {e}")
    _community_cache = dict(data)
    _community_cache_ts = time.time()

def _load_pinned_art() -> list:
    global _pinned_art_cache, _pinned_art_cache_ts
    now = time.time()
    if _pinned_art_cache is not None and (now - _pinned_art_cache_ts) < _PINNED_ART_CACHE_TTL:
        return list(_pinned_art_cache)
    if PINNED_ART_JSON.exists():
        try:
            _pinned_art_cache = json.loads(PINNED_ART_JSON.read_text(encoding="utf-8"))
            if not isinstance(_pinned_art_cache, list):
                _pinned_art_cache = []
        except Exception:
            _pinned_art_cache = []
    else:
        _pinned_art_cache = []
    _pinned_art_cache_ts = now
    return list(_pinned_art_cache)

def _save_pinned_art(data: list):
    global _pinned_art_cache, _pinned_art_cache_ts
    try:
        PINNED_ART_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Failed to write pinned art: {e}")
    _pinned_art_cache = list(data)
    _pinned_art_cache_ts = time.time()

def _musicbrainz_search(query: str) -> list:
    """Search MusicBrainz for releases. Returns list of dicts with 'id' and 'title'."""
    try:
        resp = _musicbrainz_session.get(
            "https://musicbrainz.org/ws/2/release",
            params={"query": query, "fmt": "json", "limit": 5},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("releases", [])
        log.debug(f"MusicBrainz search returned {resp.status_code} for query: {query}")
    except Exception as e:
        log.debug(f"MusicBrainz search failed for '{query}': {e}")
    return []

# ── Resolution enforcement ────────────────────────────────────────────────────
_LOW_RES_MARKERS = ("250px", "100px", "50px", "250-", "100-", "50-",
                    "=s100", "=s200", "=s250", "=s500",
                    "/small", "/medium", "/thumbnail",
                    "thumb_", "_small", "_medium")

def _is_low_res_url(url: str) -> bool:
    """Return True if URL points to a known low-resolution thumbnail."""
    lower = url.lower()
    return any(m in lower for m in _LOW_RES_MARKERS)

def _fetch_cover_art(mbid: str) -> list:
    """Fetch structured cover art data from Cover Art Archive for a given MBID.

    Returns list of dicts: {url, front, types, asset_id, has_master}.
    Only includes images that pass the strict resolution filter — low-res
    thumbnails (250, 500, small, medium) are discarded.
    """
    try:
        resp = _musicbrainz_session.get(
            f"https://coverartarchive.org/release/{mbid}",
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            images = data.get("images", [])
            results = []
            for img in images:
                image_url = img.get("image", "")
                if not image_url:
                    continue
                if _is_low_res_url(image_url):
                    continue
                asset_id = hashlib.md5(image_url.encode()).hexdigest()
                has_master = bool(img.get("approved"))
                results.append({
                    "url": image_url,
                    "front": bool(img.get("front", False)),
                    "types": img.get("types", []),
                    "asset_id": asset_id,
                    "has_master": has_master,
                })
            return results
    except Exception as e:
        log.debug(f"Cover Art Archive fetch failed for {mbid}: {e}")
    return []


def _fetch_ytmusic_artist_header(artist_name: str) -> list:
    """Fetch high-res artist banner/header imagery from YouTube Music artist profile.

    Uses ytmusic.get_artist() to retrieve the official artist page header and
    banner images. Returns list of dicts: {url, type, asset_id, width, height}.
    Only returns images that are wider than tall (banners) or square (thumbnails).
    """
    results = []
    try:
        search_results = ytmusic.search(artist_name, filter="artists", limit=3)
        if not search_results:
            return results
        target = artist_name.lower().strip()
        best_browse = None
        for r in search_results:
            if r.get("browseId"):
                r_name = (r.get("name") or "").lower().strip()
                if r_name == target:
                    best_browse = r["browseId"]
                    break
        if not best_browse:
            for r in search_results:
                if r.get("browseId"):
                    best_browse = r["browseId"]
                    break
        if not best_browse:
            return results

        artist_data = ytmusic.get_artist(best_browse)
        if not artist_data:
            return results

        seen_ids = set()

        # Header / banner images (wide-format, highest resolution)
        for thumb in (artist_data.get("thumbnails") or []):
            url = thumb.get("url", "")
            if not url or _is_low_res_url(url):
                continue
            # Strip YouTube dimension params and request max resolution
            clean_url = re.sub(r'=w\d+-h\d+.*', '', url)
            if not clean_url:
                continue
            asset_id = hashlib.md5(clean_url.encode()).hexdigest()
            if asset_id in seen_ids:
                continue
            seen_ids.add(asset_id)
            w = thumb.get("width", 0)
            h = thumb.get("height", 0)
            results.append({
                "url": clean_url,
                "type": "artist-header",
                "asset_id": asset_id,
                "width": w,
                "height": h,
            })

        # Banner images from artist page sections
        for section_key in ("header", "banner"):
            banner = artist_data.get(section_key)
            if isinstance(banner, dict):
                for thumb in (banner.get("thumbnails") or []):
                    url = thumb.get("url", "")
                    if not url or _is_low_res_url(url):
                        continue
                    clean_url = re.sub(r'=w\d+-h\d+.*', '', url)
                    if not clean_url:
                        continue
                    asset_id = hashlib.md5(clean_url.encode()).hexdigest()
                    if asset_id in seen_ids:
                        continue
                    seen_ids.add(asset_id)
                    w = thumb.get("width", 0)
                    h = thumb.get("height", 0)
                    results.append({
                        "url": clean_url,
                        "type": "artist-header",
                        "asset_id": asset_id,
                        "width": w,
                        "height": h,
                    })

        # Sort by width descending — prefer widest banner
        results.sort(key=lambda x: x.get("width", 0), reverse=True)
    except Exception as e:
        log.debug(f"YTMusic artist header fetch failed for '{artist_name}': {e}")
    return results


def _fetch_artist_portrait(artist_name: str) -> list:
    """Fallback: fetch artist portrait from MusicBrainz relations + Wikipedia.

    Requests the highest-resolution Wikipedia thumbnail available (up to 1000px).
    Returns list of dicts: {url, type, asset_id}.
    """
    try:
        resp = _musicbrainz_session.get(
            "https://musicbrainz.org/ws/2/artist/",
            params={"query": artist_name, "fmt": "json", "limit": 3},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        artists = resp.json().get("artists", [])
        if not artists:
            return []

        target = artist_name.lower().strip()
        best = None
        for a in artists:
            a_name = (a.get("name") or "").lower().strip()
            if a_name == target:
                best = a
                break
        if not best:
            best = artists[0]

        artist_mbid = best.get("id", "")
        if not artist_mbid:
            return []

        rel_resp = _musicbrainz_session.get(
            f"https://musicbrainz.org/ws/2/artist/{artist_mbid}",
            params={"fmt": "json", "inc": "url-rels"},
            timeout=10,
        )
        if rel_resp.status_code != 200:
            return []

        relations = rel_resp.json().get("relations", [])
        wiki_url = None
        for rel in relations:
            url_info = rel.get("url", {})
            resource = url_info.get("resource", "")
            if "wikipedia.org/wiki/" in resource:
                wiki_url = resource
                break

        if not wiki_url:
            return []

        wiki_title = wiki_url.split("/wiki/", 1)[-1]
        wiki_title = wiki_title.replace("_", " ")
        encoded_title = urllib.parse.quote(wiki_title, safe="")

        summary_resp = _musicbrainz_session.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded_title}",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if summary_resp.status_code != 200:
            return []

        summary = summary_resp.json()
        thumbnail = summary.get("thumbnail", {})
        img_source = thumbnail.get("source", "")
        if not img_source:
            return []

        # Request the highest-resolution version available (up to 1000px)
        large_url = re.sub(r'/(\d+)px-', '/1000px-', img_source)
        asset_id = hashlib.md5(large_url.encode()).hexdigest()

        return [{
            "url": large_url,
            "type": "artist-portrait",
            "asset_id": asset_id,
        }]
    except Exception as e:
        log.debug(f"Artist portrait fetch failed for '{artist_name}': {e}")
    return []


def _classify_layout_type(image_data: dict) -> str:
    """Determine structural layout tag for an image asset.

    - 'wide-rect': wide-format artist banner / landscape image
    - 'giant-square': rare high-resolution pinned feature
    - 'square': default for standard album art
    """
    img_type = image_data.get("type", "")

    # Artist headers and banners are inherently wide
    if img_type in ("artist-header", "artist-portrait"):
        return "wide-rect"

    # If width/height known and aspect ratio > 1.3, treat as wide
    w = image_data.get("width", 0)
    h = image_data.get("height", 0)
    if w > 0 and h > 0 and (w / h) > 1.3:
        return "wide-rect"

    types = image_data.get("types", [])
    url = image_data.get("url", "")

    # Full-resolution CAA front covers are high-res pinned features
    if "coverartarchive.org" in url and not _is_low_res_url(url):
        if image_data.get("front") or "Front" in types:
            return "giant-square"

    # Approved/master images at full resolution
    if image_data.get("has_master") and not _is_low_res_url(url):
        return "giant-square"

    return "square"


def _discover_art_for_item(artist: str, song: str = "", fav: dict = None) -> list:
    """Strict 3-stage fallback pipeline — always returns ≥1 valid image.

    Stage 1 (Track Search): MusicBrainz/Cover Art Archive for the specific song.
    Stage 2 (Artist-Only): YouTube Music artist profile header/portrait imagery.
    Stage 3 (Local Fail-safe): Native cover art from the user's favorites metadata.

    Returns list of dicts: {url, artist, song, asset_id, layout_type, source}.
    Guaranteed non-empty — Stage 3 ensures every track gets at least one image.
    """
    fav = fav or {}
    seen_asset_ids = set()

    # ════════════════════════════════════════════════════════════════════════
    # STAGE 1 — Track Search (MusicBrainz + Cover Art Archive)
    # ════════════════════════════════════════════════════════════════════════
    all_assets = []
    query = f"{artist} {song}".strip()
    releases = _musicbrainz_search(query)
    if not releases:
        releases = _musicbrainz_search(artist)

    for release in releases[:3]:
        mbid = release.get("id", "")
        if not mbid:
            continue
        cover_assets = _fetch_cover_art(mbid)
        for asset in cover_assets:
            aid = asset["asset_id"]
            if aid in seen_asset_ids:
                continue
            seen_asset_ids.add(aid)
            all_assets.append(asset)
        if len(all_assets) >= 10:
            break

    # Evaluate track search quality
    has_high_res = any(
        a.get("has_master") or not _is_low_res_url(a.get("url", ""))
        for a in all_assets
    )
    track_sufficient = len(all_assets) > 0 and has_high_res

    if track_sufficient:
        results = []
        for asset in all_assets:
            layout = _classify_layout_type(asset)
            results.append({
                "url": asset["url"],
                "artist": artist,
                "song": song,
                "asset_id": asset["asset_id"],
                "layout_type": layout,
                "source": "cover-art-archive",
            })
        return results

    # Track search returned assets but they're insufficient — keep the best one
    # as a guaranteed fallback before trying artist-only search
    stage1_best = None
    if all_assets:
        # Prefer front covers, then approved/master, then first available
        front_covers = [a for a in all_assets if a.get("front")]
        if front_covers:
            stage1_best = front_covers[0]
        else:
            stage1_best = all_assets[0]

    # ════════════════════════════════════════════════════════════════════════
    # STAGE 2 — Artist-Only Fallback (YTMusic artist profile)
    # ════════════════════════════════════════════════════════════════════════
    artist_headers = []
    if artist:
        artist_headers = _fetch_ytmusic_artist_header(artist)

    if not artist_headers and artist:
        artist_headers = _fetch_artist_portrait(artist)

    if artist_headers:
        results = []
        # Include the best Stage 1 asset alongside artist imagery
        if stage1_best:
            aid = stage1_best["asset_id"]
            if aid not in seen_asset_ids:
                seen_asset_ids.add(aid)
                results.append({
                    "url": stage1_best["url"],
                    "artist": artist,
                    "song": song,
                    "asset_id": aid,
                    "layout_type": _classify_layout_type(stage1_best),
                    "source": "cover-art-archive",
                })
        for header in artist_headers:
            asset_id = header.get("asset_id", "")
            if asset_id in seen_asset_ids:
                continue
            seen_asset_ids.add(asset_id)
            layout = _classify_layout_type(header)
            results.append({
                "url": header["url"],
                "artist": artist,
                "song": song,
                "asset_id": asset_id,
                "layout_type": layout,
                "source": "artist-header",
            })
        return results

    # ════════════════════════════════════════════════════════════════════════
    # STAGE 3 — Guaranteed Local Fail-safe (favorites native art)
    # ════════════════════════════════════════════════════════════════════════
    # 3a: Check local art file first (highest fidelity, already downloaded)
    tid = fav.get("tid") or get_track_id(song, artist)
    local_art_path = SAVED_DIR / f"{tid}.jpg"
    if local_art_path.is_file():
        art_url = f"/api/local_file?q={tid}.jpg"
        asset_id = hashlib.md5(art_url.encode()).hexdigest()
        return [{
            "url": art_url,
            "artist": artist,
            "song": song,
            "asset_id": asset_id,
            "layout_type": "square",
            "source": "local-art",
        }]

    # 3b: HTTP art URL from favorites metadata
    art_url = fav.get("art") or fav.get("album_art") or ""
    if art_url and art_url.startswith(("http://", "https://")):
        asset_id = hashlib.md5(art_url.encode()).hexdigest()
        return [{
            "url": art_url,
            "artist": artist,
            "song": song,
            "asset_id": asset_id,
            "layout_type": "square",
            "source": "favorites-fallback",
        }]

    # 3c: Use the best CAA asset from Stage 1 as last-ditch fallback
    if stage1_best:
        aid = stage1_best["asset_id"]
        return [{
            "url": stage1_best["url"],
            "artist": artist,
            "song": song,
            "asset_id": aid,
            "layout_type": _classify_layout_type(stage1_best),
            "source": "cover-art-archive-fallback",
        }]

    # 3d: Absolute last resort — YouTube thumbnail (always available for any videoId)
    video_id = fav.get("videoId", "")
    if video_id:
        yt_thumb = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
        asset_id = hashlib.md5(yt_thumb.encode()).hexdigest()
        return [{
            "url": yt_thumb,
            "artist": artist,
            "song": song,
            "asset_id": asset_id,
            "layout_type": "square",
            "source": "youtube-thumb",
        }]

    # Should never reach here, but return empty if truly nothing exists
    return []


@app.route("/api/community/discover")
def api_community_discover():
    """Discover alternative artwork from MusicBrainz/Cover Art Archive/YTMusic based on favorites.

    3-stage fallback pipeline ensures every track always gets at least one image:
      Stage 1: MusicBrainz/Cover Art Archive track search
      Stage 2: YouTube Music artist profile header/portrait
      Stage 3: Local art file or native favorites metadata

    Returns images with layout_type tags and asset_id keys for structural layout sizing.
    Responses are cached in SAVED/community_cache.json keyed by asset_id to prevent
    redundant backend network calls.
    """
    try:
        favs = _load_favorites()
        if not favs:
            return jsonify({"images": [], "source": "none"})

        # Build a lookup map from artist+song → full fav object (for Stage 3)
        fav_map = {}
        for fav in favs:
            a = (fav.get("artist") or "").strip()
            n = (fav.get("name") or "").strip()
            if a:
                fav_map[f"{a.lower()}|{n.lower()}"] = fav

        # Deduplicate by artist+song combo, preserving full fav data
        seen = set()
        unique_items = []
        for fav in favs:
            artist = fav.get("artist", "").strip()
            song = fav.get("name", "").strip()
            if not artist:
                continue
            key = f"{artist.lower()}|{song.lower()}"
            if key in seen:
                continue
            seen.add(key)
            unique_items.append(fav)

        if not unique_items:
            return jsonify({"images": [], "source": "none"})

        # Random sample up to 6 entries for visual variety
        sample_count = min(6, len(unique_items))
        items = random.sample(unique_items, sample_count)

        # Check cache (keyed by sorted artist+song keys)
        cache_key = "|".join(sorted(
            f"{i.get('artist','')}||{i.get('name','')}" for i in items
        ))
        cache = _load_community_cache()
        if cache_key in cache:
            cached = cache[cache_key]
            # Validate cached results still have valid URLs
            valid_cached = [
                img for img in cached
                if img.get("url") and img["url"].startswith(("http://", "https://", "/"))
            ]
            if valid_cached:
                return jsonify({"images": valid_cached, "source": "cache"})

        # ── 3-Stage Pipeline: every item gets ≥1 guaranteed image ──
        all_images = []
        asset_seen = set()

        def _lookup_one(fav_obj):
            artist = (fav_obj.get("artist") or "").strip()
            song = (fav_obj.get("name") or "").strip()
            try:
                return _discover_art_for_item(artist, song, fav=fav_obj)
            except Exception as e:
                log.debug(f"Community discover failed for {artist}: {e}")
            return []

        futures = [_io_executor.submit(_lookup_one, item) for item in items]
        for fut in futures:
            try:
                results = fut.result(timeout=15)
                for r in results:
                    aid = r.get("asset_id", "")
                    url = r.get("url", "")
                    # Accept HTTP URLs and relative paths (local art via /api/stream)
                    if not url:
                        continue
                    if not url.startswith(("http://", "https://", "/")):
                        continue
                    if aid and aid not in asset_seen:
                        asset_seen.add(aid)
                        all_images.append(r)
            except Exception:
                pass

        log.info(f"Community discover: {len(all_images)} unique assets from {len(items)} items")

        # Sanitize: strip any null/empty URL entries that slipped through
        all_images = [img for img in all_images if img.get("url")]

        # Cache results
        if all_images:
            for img in all_images:
                aid = img.get("asset_id", "")
                if aid:
                    cache[f"asset:{aid}"] = img
            cache[cache_key] = all_images
            _save_community_cache(cache)

        return jsonify({"images": all_images, "source": "live"})
    except Exception as e:
        log.warning(f"Community discover endpoint error: {e}")
        return jsonify({"images": [], "source": "error"})

@app.route("/api/community/pinned")
def api_community_pinned():
    """Return the list of pinned community artworks."""
    return jsonify(_load_pinned_art())

@app.route("/api/community/pin", methods=["POST"])
def api_community_pin():
    """Pin a community artwork: download the image and save metadata."""
    data = request.get_json(force=True) or {}
    caid = data.get("caid", "")
    image_url = data.get("image_url", "")
    artist = data.get("artist", "")
    song = data.get("song", "")

    if not caid or not image_url or not artist:
        return jsonify({"error": "Missing caid, image_url, or artist"}), 400

    # Validate filename pattern
    if not _SAFE_FILENAME_RE.match(caid):
        return jsonify({"error": "Invalid caid format"}), 400

    # Validate URL scheme
    if not image_url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid image URL scheme"}), 400

    # Download the image
    image_path = SAVED_DIR / f"community_{caid}.jpg"
    try:
        r = requests.get(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        image_path.write_bytes(r.content)
    except Exception as e:
        return jsonify({"error": f"Failed to download image: {e}"}), 500

    # Append to pinned_art.json
    pinned = _load_pinned_art()
    entry = {
        "caid": caid,
        "image_url": image_url,
        "artist": artist,
        "song": song,
        "local_path": str(image_path.name),
    }
    # Replace existing entry with same caid if present
    pinned = [p for p in pinned if p.get("caid") != caid]
    pinned.append(entry)
    _save_pinned_art(pinned)

    return jsonify({"success": True, "entry": entry})

@app.route("/api/community/unpin", methods=["DELETE"])
def api_community_unpin():
    """Unpin a community artwork: remove metadata and delete local image."""
    data = request.get_json(force=True) or {}
    caid = data.get("caid", "")

    if not caid:
        return jsonify({"error": "Missing caid"}), 400

    # Validate filename pattern
    if not _SAFE_FILENAME_RE.match(caid):
        return jsonify({"error": "Invalid caid format"}), 400

    # Remove from pinned_art.json
    pinned = _load_pinned_art()
    original_len = len(pinned)
    pinned = [p for p in pinned if p.get("caid") != caid]
    if len(pinned) == original_len:
        return jsonify({"error": "Caid not found in pinned art"}), 404
    _save_pinned_art(pinned)

    # Delete local image file
    image_path = SAVED_DIR / f"community_{caid}.jpg"
    image_path.unlink(missing_ok=True)

    return jsonify({"success": True})

@app.route("/api/spotify/embed_proxy")
def api_spotify_embed_proxy():
    """Proxy fetch of a Spotify embed page to bypass CORS restrictions."""
    playlist_id = request.args.get("playlist_id", "").strip()
    if not playlist_id or not playlist_id.isalnum():
        return jsonify({"error": "Invalid playlist ID"}), 400
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return jsonify({"error": f"Spotify returned status {r.status_code}"}), 502
        return r.text, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        log.warning(f"[SPOTIFY] Embed proxy failed: {e}")
        return jsonify({"error": f"Failed to fetch: {e}"}), 502

@app.route("/mobile")
def mobile_player_test():
    """Testing route — serves the mobile player UI directly."""
    return render_template("mobile_player.html")

# ── GitHub Device Flow + Community Themes ─────────────────────────────────────
# GitHub's Device Flow only needs a PUBLIC client id — no secret, no redirect
# URI. The id is resolved from env (AKI_GITHUB_CLIENT_ID), settings.json
# (github_client_id), or the hardcoded GITHUB_PUBLIC_CLIENT_ID below. When none
# is set the UI prompts the user for their public client id (never a secret).
GITHUB_PUBLIC_CLIENT_ID = "Ov23li7yj6nyzq9MqcIy"
GITHUB_DEVICE_SCOPE = "public_repo,gist"
GITHUB_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_COMMUNITY_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(BASE_DIR))) / "AkiMelody"
COMMUNITY_THEMES_JSON = _COMMUNITY_DATA_DIR / "community_themes.json"
COMMUNITY_PLAYLISTS_JSON = _COMMUNITY_DATA_DIR / "community_playlists.json"

_COMMUNITY_SEED = [
    {
        "id": "seed_pulse",
        "name": "Neon Pulse",
        "author": "akimelody",
        "avatarUrl": "",
        "blur": 18, "opacity": 50, "glow": 40, "dynamicBase": False, "base": "dark",
        "art": None,
        "colors": {
            "primary": {"r": 140, "g": 82, "b": 255},
            "secondary": {"r": 0, "g": 210, "b": 255},
            "highlight": {"r": 255, "g": 107, "b": 157},
            "shadow": {"r": 30, "g": 12, "b": 60},
        },
        "publishedAt": 0,
        "downloads": 0,
        "likes": [],
    },
    {
        "id": "seed_ember",
        "name": "Ember Sunset",
        "author": "akimelody",
        "avatarUrl": "",
        "blur": 22, "opacity": 60, "glow": 30, "dynamicBase": False, "base": "dark",
        "art": None,
        "colors": {
            "primary": {"r": 255, "g": 94, "b": 58},
            "secondary": {"r": 255, "g": 161, "b": 83},
            "highlight": {"r": 255, "g": 214, "b": 124},
            "shadow": {"r": 60, "g": 16, "b": 10},
        },
        "publishedAt": 0,
        "downloads": 0,
        "likes": [],
    },
    {
        "id": "seed_aurora",
        "name": "Aurora Drift",
        "author": "akimelody",
        "avatarUrl": "",
        "blur": 16, "opacity": 45, "glow": 50, "dynamicBase": True, "base": "dark",
        "art": None,
        "colors": None,
        "publishedAt": 0,
        "downloads": 0,
        "likes": [],
    },
]


def _github_client_id():
    """Resolve the PUBLIC client id: env → settings.json → constant. No secret."""
    cid = os.environ.get("AKI_GITHUB_CLIENT_ID", "")
    if not cid:
        try:
            cid = _load_settings().get("github_client_id", "")
        except Exception:
            pass
    return cid or GITHUB_PUBLIC_CLIENT_ID


def _load_community_themes() -> list:
    if not COMMUNITY_THEMES_JSON.exists():
        _save_community_themes(_COMMUNITY_SEED)
        return list(_COMMUNITY_SEED)
    try:
        items = json.loads(COMMUNITY_THEMES_JSON.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return list(_COMMUNITY_SEED)


def _save_community_themes(items: list) -> None:
    COMMUNITY_THEMES_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_community_playlists() -> list:
    if not COMMUNITY_PLAYLISTS_JSON.exists():
        _save_community_playlists([])
        return []
    try:
        items = json.loads(COMMUNITY_PLAYLISTS_JSON.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _save_community_playlists(items: list) -> None:
    COMMUNITY_PLAYLISTS_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/github/config")
def api_github_config():
    cid = _github_client_id()
    token = ""
    user = {}
    try:
        s = _load_settings()
        token = s.get("github_token", "") or ""
        user = s.get("github_user", {}) or {}
    except Exception:
        pass
    return jsonify({
        "configured": bool(cid),
        "token": token,
        "user": user,
    })


@app.route("/api/github/device/code", methods=["POST"])
def api_github_device_code():
    """Start GitHub's Device Flow. Returns the user_code + verification URL for
    the user to approve. A user-supplied client_id (frontend prompt) is persisted
    in settings.json so later sign-ins skip the prompt. No secret involved."""
    data = request.get_json(silent=True) or {}
    cid = (data.get("client_id") or "").strip() or _github_client_id()
    if not cid:
        return jsonify({"ok": False, "error": "GitHub client id is not configured."}), 400
    try:
        s = _load_settings()
        if s.get("github_client_id") != cid:
            s["github_client_id"] = cid
            _save_settings(s)
    except Exception:
        pass
    try:
        resp = requests.post(
            "https://github.com/login/device/code",
            json={"client_id": cid, "scope": GITHUB_DEVICE_SCOPE},
            headers={"Accept": "application/json"}, timeout=20)
        d = resp.json() if resp.headers.get("content-type", "").find("json") != -1 else {}
        if resp.status_code != 200 or "device_code" not in d:
            msg = d.get("error_description") or d.get("error") or f"GitHub device request failed ({resp.status_code})"
            return jsonify({"ok": False, "error": msg}), resp.status_code if resp.status_code >= 400 else 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not reach GitHub: {e}"}), 502
    return jsonify({
        "ok": True,
        "client_id": cid,
        "device_code": d["device_code"],
        "user_code": d.get("user_code", ""),
        "verification_uri": d.get("verification_uri", "https://github.com/login/device"),
        "verification_uri_complete": d.get("verification_uri_complete", ""),
        "interval": int(d.get("interval") or 5),
        "expires_in": int(d.get("expires_in") or 900),
    })


@app.route("/api/github/device/token", methods=["POST"])
def api_github_device_token():
    """Poll GitHub's device-flow token endpoint. Resolves to an access token once
    the user approves on github.com/login/device. Only a public client id is sent."""
    data = request.get_json(silent=True) or {}
    device_code = (data.get("device_code") or "").strip()
    cid = (data.get("client_id") or "").strip() or _github_client_id()
    if not device_code or not cid:
        return jsonify({"ok": False, "error": "Missing device code or client id"}), 400
    try:
        resp = requests.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": cid, "device_code": device_code, "grant_type": GITHUB_DEVICE_GRANT},
            headers={"Accept": "application/json"}, timeout=20)
        d = resp.json() if resp.headers.get("content-type", "").find("json") != -1 else {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not reach GitHub: {e}"}), 502

    if d.get("access_token"):
        token = d["access_token"]
        user = {}
        try:
            r = requests.get("https://api.github.com/user",
                             headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=20)
            if r.ok:
                ud = r.json()
                user = {"username": ud.get("login", ""), "avatarUrl": ud.get("avatar_url", "")}
        except Exception:
            pass
        return jsonify({"ok": True, "accessToken": token, "user": user})

    err = d.get("error") or "unknown"
    result = {"ok": False, "error": err}
    if err == "slow_down":
        result["interval"] = int(d.get("interval") or 5) + 5
    elif err == "authorization_pending":
        result["interval"] = int(d.get("interval") or 5)
    return jsonify(result)


# ── GitHub OAuth PKCE Flow (standard web flow) ─────────────────────────────
# The Device Flow above works but forces the user to copy a code and open
# github.com/login/device in an external browser. For a smoother desktop
# experience we also expose the standard OAuth Authorization Code Flow with
# PKCE: the app opens the familiar GitHub consent screen inside the webview,
# the user approves with their own account, GitHub redirects back to our
# local Flask callback, and we exchange the code for a token. No client secret
# is needed — PKCE proves the request originated from this app.
#
# Client id is resolved from env (AKI_GITHUB_CLIENT_ID) → settings.json
# (github_client_id). No secret is ever stored or sent.

_GITHUB_OAUTH_STATE = {}  # state → {verifier, ts}  (pruned on read)


def _github_oauth_client_id():
    """Resolve the PUBLIC client id for the OAuth web flow."""
    return _github_client_id()


def _make_pkce_pair():
    """Generate a PKCE code_verifier + S256 code_challenge."""
    import base64
    import hashlib
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


@app.route("/api/github/auth/url", methods=["GET"])
def api_github_auth_url():
    """Return the GitHub OAuth authorization URL + a state token the frontend
    passes back to /api/github/auth/callback. The client id comes from
    settings.json (github_client_id) — no prompt."""
    cid = _github_oauth_client_id()
    if not cid:
        return jsonify({"ok": False, "error": "GitHub client id is not configured."}), 400
    state = secrets.token_urlsafe(24)
    verifier, challenge = _make_pkce_pair()
    # Prune entries older than 10 min.
    cutoff = time.time() - 600
    for k in [k for k, v in _GITHUB_OAUTH_STATE.items() if v.get("ts", 0) < cutoff]:
        _GITHUB_OAUTH_STATE.pop(k, None)
    _GITHUB_OAUTH_STATE[state] = {"verifier": verifier, "ts": time.time()}
    params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": f"http://127.0.0.1:{SERVER_PORT}/api/github/auth/callback",
        "scope": GITHUB_DEVICE_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return jsonify({
        "ok": True,
        "url": f"https://github.com/login/oauth/authorize?{params}",
        "state": state,
    })


@app.route("/api/github/auth/callback")
def api_github_auth_callback():
    """GitHub redirects here after the user approves. We exchange the code for
    an access token using the stored PKCE verifier, then redirect back to the
    app with ?github_linked=1 (or ?github_linked=error)."""
    code = request.args.get("code") or ""
    state = request.args.get("state") or ""
    error = request.args.get("error") or ""
    if error:
        return _github_callback_redirect("error", error_description=request.args.get("error_description") or error)
    if not code or not state:
        return _github_callback_redirect("error", error_description="missing_code_or_state")
    entry = _GITHUB_OAUTH_STATE.pop(state, None)
    if not entry:
        return _github_callback_redirect("error", error_description="state_expired_or_invalid")
    cid = _github_oauth_client_id()
    try:
        resp = requests.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": cid,
                "code": code,
                "redirect_uri": f"http://127.0.0.1:{SERVER_PORT}/api/github/auth/callback",
                "code_verifier": entry["verifier"],
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        d = resp.json() if resp.headers.get("content-type", "").find("json") != -1 else {}
        token = d.get("access_token") or ""
        if not token:
            return _github_callback_redirect("error", error_description=d.get("error_description") or "token_exchange_failed")
    except Exception as e:
        return _github_callback_redirect("error", error_description=str(e))
    # Resolve the user profile.
    user = {}
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=20)
        if r.ok:
            ud = r.json()
            user = {"username": ud.get("login", ""), "avatarUrl": ud.get("avatar_url", "")}
    except Exception:
        pass
    # Store token + user in settings so the existing session endpoints work.
    try:
        s = _load_settings()
        s["github_token"] = token
        s["github_user"] = user
        _save_settings(s)
    except Exception:
        pass
    return _github_callback_redirect("ok", token=token, user=user)


def _github_callback_redirect(status, error_description=None, token=None, user=None):
    """Redirect back to the app with a query param indicating the result."""
    parts = [f"github_linked={status}"]
    if error_description:
        parts.append("msg=" + urllib.parse.urlencode({"": error_description})[1:])
    if user and user.get("username"):
        parts.append("user=" + urllib.parse.urlencode({"": user["username"]})[1:])
    return _flask_redirect(f"http://127.0.0.1:{SERVER_PORT}/?{'&'.join(parts)}")

@app.route("/api/github/themes")
def api_github_themes():
    token = (request.args.get("token") or "").strip()
    login = None
    if token:
        try:
            r = requests.get("https://api.github.com/user",
                             headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=10)
            if r.status_code == 200:
                login = r.json().get("login", "")
        except Exception:
            pass
    items = _load_community_themes()
    out = []
    for it in items:
        likes = [str(u) for u in it.get("likes", [])]
        it_copy = dict(it)
        it_copy["likeCount"] = len(likes)
        it_copy["likedByMe"] = bool(login and login in likes)
        it_copy["downloads"] = it.get("downloads", 0)
        it_copy["publishedAt"] = it.get("publishedAt", 0)
        out.append(it_copy)
    return jsonify({"themes": out})


@app.route("/api/github/themes/like", methods=["POST"])
def api_github_theme_like():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    theme_id = str(data.get("id") or "").strip()
    if not theme_id:
        return jsonify({"ok": False, "error": "Missing theme id"}), 400
    if not token:
        return jsonify({"ok": False, "error": "Not signed in to GitHub"}), 401
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=10)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": "GitHub token is invalid or expired"}), 401
        login = r.json().get("login", "")
    except Exception:
        return jsonify({"ok": False, "error": "Could not reach GitHub"}), 502
    if not login:
        return jsonify({"ok": False, "error": "Could not identify your GitHub account"}), 401

    items = _load_community_themes()
    target = next((i for i in items if str(i.get("id")) == theme_id), None)
    if not target:
        return jsonify({"ok": False, "error": "Theme not found"}), 404
    likes = list(target.get("likes", []))
    if login in likes:
        likes.remove(login)
    else:
        likes.append(login)
    target["likes"] = likes
    _save_community_themes(items)
    return jsonify({"ok": True, "likeCount": len(likes), "likedByMe": login in likes})


@app.route("/api/github/themes/download", methods=["POST"])
def api_github_theme_download():
    """Increment download counter for a community theme."""
    data = request.get_json(silent=True) or {}
    theme_id = str(data.get("id") or "").strip()
    if not theme_id:
        return jsonify({"ok": False, "error": "Missing theme id"}), 400
    items = _load_community_themes()
    target = next((i for i in items if str(i.get("id")) == theme_id), None)
    if not target:
        return jsonify({"ok": False, "error": "Theme not found"}), 404
    target["downloads"] = target.get("downloads", 0) + 1
    _save_community_themes(items)
    return jsonify({"ok": True, "downloads": target["downloads"]})


@app.route("/api/github/themes", methods=["POST"])
def api_github_publish_theme():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    theme = data.get("theme")
    if not theme or not isinstance(theme, dict):
        return jsonify({"ok": False, "error": "Missing theme payload"}), 400
    if not token:
        return jsonify({"ok": False, "error": "Not signed in to GitHub"}), 401
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": "GitHub token is invalid or expired"}), 401
        ud = r.json()
    except Exception:
        return jsonify({"ok": False, "error": "Could not reach GitHub"}), 502

    author = ud.get("login", "unknown")
    def _int_theme(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    item = {
        "id": "gh_" + str(int(time.time() * 1000)),
        "name": str(theme.get("name") or "Untitled Theme")[:28],
        "author": author,
        "avatarUrl": ud.get("avatar_url", ""),
        "blur": _int_theme(theme.get("blur"), 18),
        "opacity": _int_theme(theme.get("opacity"), 50),
        "glow": _int_theme(theme.get("glow"), 30),
        "brightness": _int_theme(theme.get("brightness"), 60),
        "radius": _int_theme(theme.get("radius"), 24),
        "dynamicBase": bool(theme.get("dynamicBase")),
        "base": theme.get("base") or "dark",
        "art": theme.get("art") or None,
        "colors": theme.get("colors") or None,
        "publishedAt": int(time.time()),
        "downloads": 0,
        "likes": [],
    }
    items = _load_community_themes()
    items = [i for i in items if not (i.get("author") == author and i.get("name") == item["name"])]
    items.append(item)
    _save_community_themes(items)
    return jsonify({"ok": True, "theme": item})


@app.route("/api/github/themes", methods=["DELETE"])
def api_github_delete_theme():
    """Remove a theme from the community shop. Author-only: the GitHub token is
    resolved to the authenticated login and compared against the theme's author
    before the item is dropped from community_themes.json."""
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    theme_id = str(data.get("id") or "").strip()
    if not theme_id:
        return jsonify({"ok": False, "error": "Missing theme id"}), 400
    if not token:
        return jsonify({"ok": False, "error": "Not signed in to GitHub"}), 401
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": "GitHub token is invalid or expired"}), 401
        login = r.json().get("login", "")
    except Exception:
        return jsonify({"ok": False, "error": "Could not reach GitHub"}), 502
    if not login:
        return jsonify({"ok": False, "error": "Could not identify your GitHub account"}), 401
    items = _load_community_themes()
    target = next((i for i in items if str(i.get("id")) == theme_id), None)
    if not target:
        return jsonify({"ok": False, "error": "Theme not found"}), 404
    if target.get("author") != login:
        return jsonify({"ok": False, "error": "You can only remove your own themes"}), 403
    _save_community_themes([i for i in items if str(i.get("id")) != theme_id])
    return jsonify({"ok": True})


@app.route("/api/community-playlists")
def api_community_playlists():
    token = (request.args.get("token") or "").strip()
    login = None
    if token:
        try:
            r = requests.get("https://api.github.com/user",
                             headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=10)
            if r.status_code == 200:
                login = r.json().get("login", "")
        except Exception:
            pass
    items = _load_community_playlists()
    out = []
    for it in items:
        tracks = it.get("tracks", [])
        total_dur = sum(t.get("dur", 0) for t in tracks)
        it_copy = dict(it)
        it_copy["trackCount"] = len(tracks)
        it_copy["totalDuration"] = total_dur
        it_copy["canDelete"] = bool(login and it.get("author") == login)
        out.append(it_copy)
    return jsonify({"playlists": out})


@app.route("/api/community-playlists", methods=["POST"])
def api_community_playlists_publish():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    playlist = data.get("playlist")
    if not playlist or not isinstance(playlist, dict):
        return jsonify({"ok": False, "error": "Missing playlist payload"}), 400
    if not token:
        return jsonify({"ok": False, "error": "Not signed in to GitHub"}), 401
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": "GitHub token is invalid or expired"}), 401
        ud = r.json()
    except Exception:
        return jsonify({"ok": False, "error": "Could not reach GitHub"}), 502

    author = ud.get("login", "unknown")
    playlist_data = {
        "id": "pl_" + str(int(time.time() * 1000)),
        "name": str(playlist.get("name") or "Untitled Playlist")[:64],
        "description": str(playlist.get("description") or "")[:500],
        "author": author,
        "avatarUrl": ud.get("avatar_url", ""),
        "tracks": playlist.get("tracks") or [],
        "publishedAt": int(time.time()),
    }
    items = _load_community_playlists()
    # Deduplicate by same author + name
    items = [i for i in items if not (i.get("author") == author and i.get("name") == playlist_data["name"])]
    items.append(playlist_data)
    _save_community_playlists(items)
    return jsonify({"ok": True, "playlist": playlist_data})


@app.route("/api/community-playlists", methods=["DELETE"])
def api_community_playlists_delete():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    playlist_id = str(data.get("id") or "").strip()
    if not playlist_id:
        return jsonify({"ok": False, "error": "Missing playlist id"}), 400
    if not token:
        return jsonify({"ok": False, "error": "Not signed in to GitHub"}), 401
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": "GitHub token is invalid or expired"}), 401
        login = r.json().get("login", "")
    except Exception:
        return jsonify({"ok": False, "error": "Could not reach GitHub"}), 502
    if not login:
        return jsonify({"ok": False, "error": "Could not identify your GitHub account"}), 401
    items = _load_community_playlists()
    target = next((i for i in items if str(i.get("id")) == playlist_id), None)
    if not target:
        return jsonify({"ok": False, "error": "Playlist not found"}), 404
    if target.get("author") != login:
        return jsonify({"ok": False, "error": "You can only remove your own playlists"}), 403
    _save_community_playlists([i for i in items if str(i.get("id")) != playlist_id])
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════
#   AUTOMATIC BACKGROUND UPDATE SYSTEM
#   Proxies GitHub releases API + chunked installer download to local disk.
#   Designed so the pywebview shell (or standalone Flask mode) can poll
#   /api/update/check every 6h, then /api/update/download to stream the
#   installer to %LOCALAPPDATA%\AkiMelody\updates\, and finally
#   /api/update/launch to shell-execute the installer on Restart-to-Update.
# ═══════════════════════════════════════════════════════════════════════════

# Single in-flight download + its progress state, guarded by a lock so the
# status endpoint is always consistent with the writer thread.
_update_state = {
    "status": "idle",          # idle | downloading | ready | error
    "progress": 0,             # 0..100 (downloads in percent)
    "received": 0,            # bytes received so far
    "total": 0,               # total bytes (Content-Length), 0 if unknown
    "asset_name": "",          # local filename of the downloaded installer
    "asset_url": "",           # remote browser_download_url
    "release_tag": "",         # e.g. "v1.2.0"
    "release_notes": "",       # raw markdown body
    "release_html_url": "",     # GitHub web URL for the release
    "error": "",               # last error message (status=error)
    "local_path": "",          # absolute path to the downloaded installer on disk
}
_update_lock = threading.Lock()
_update_thread = None          # the background download worker (if running)

# Where to store the installer binary. Mirrors the data dir strategy from
# webview_launcher.py (kept out of the install dir which may be read-only).
def _update_dir():
    """Installer scratch dir: %LOCALAPPDATA%\\AkiMelody\\updates in packaged mode, else SAVED/updates."""
    if os.environ.get("LOCALAPPDATA"):
        d = Path(os.environ["LOCALAPPDATA"]) / "AkiMelody" / "updates"
    else:
        d = SAVED_DIR / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_semver(tag):
    """Parse a SemVer-ish string into a (major, minor, patch, prerelease) tuple.
    Leading `v` is stripped. Prerelease sorts below any release with the same
    numeric core (e.g. 1.0.0-beta < 1.0.0). Returns (0,0,0,'') on parse failure.
    """
    if not tag:
        return (0, 0, 0, "")
    s = str(tag).strip().lstrip("vV").strip()
    # Strip leading non-digit noise ("release-", "version_") — lenient parser.
    m = re.search(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", s)
    if not m:
        return (0, 0, 0, "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def _semver_gt(a, b):
    """True if SemVer tuple `a` is strictly greater than `b`."""
    # Compare numeric cores first.
    for i in range(3):
        if a[i] != b[i]:
            return a[i] > b[i]
    # Cores equal → prerelease decides. A release ("") is greater than a
    # prerelease ("x"); two prereleases compare lexicographically.
    ap, bp = a[3], b[3]
    if ap == bp:
        return False
    if not ap:
        return True           # a is a release, b is a prerelease → a > b
    if not bp:
        return False          # a is a prerelease, b is a release → a < b
    return ap > bp            # both prereleases: lexicographic


@app.route("/api/update/check")
def api_update_check():
    """Compare APP_VERSION against the latest GitHub release.
    Returns the parsed release payload + an `update_available` boolean. A 404
    from GitHub (no releases yet) is reported as `update_available: false`,
    not an error — this is the happy path for fresh installs."""
    try:
        # GitHub requires a User-Agent header on all API requests.
        r = requests.get(
            f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
            headers={
                "User-Agent": "AkiMelody-Updater",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        if r.status_code == 404:
            return jsonify({
                "ok": True,
                "update_available": False,
                "local_version": APP_VERSION,
                "remote_version": None,
                "reason": "no_releases",
            })
        if not r.ok:
            return jsonify({
                "ok": False,
                "error": f"github_http_{r.status_code}",
                "local_version": APP_VERSION,
            }), 502

        data = r.json()
        remote_tag = data.get("tag_name") or ""
        remote_version = remote_tag.lstrip("vV").strip()
        local_tuple = _parse_semver(APP_VERSION)
        remote_tuple = _parse_semver(remote_tag)
        update_available = _semver_gt(remote_tuple, local_tuple)

        # Pick the first .exe asset (the Windows installer). Fallback: first
        # asset of any kind so users on a manually-named build still get one.
        assets = data.get("assets") or []
        pickup = None
        for a in assets:
            name = (a.get("name") or "").lower()
            if name.endswith(".exe"):
                pickup = a
                break
        if pickup is None and assets:
            pickup = assets[0]

        return jsonify({
            "ok": True,
            "update_available": update_available,
            "local_version": APP_VERSION,
            "remote_version": remote_version,
            "remote_tuple": list(remote_tuple),
            "local_tuple": list(local_tuple),
            "release_tag": remote_tag,
            "release_name": data.get("name") or "",
            "release_notes": data.get("body") or "",
            "release_html_url": data.get("html_url") or "",
            "published_at": data.get("published_at") or "",
            "asset": {
                "name": pickup.get("name") if pickup else "",
                "url": pickup.get("browser_download_url") if pickup else "",
                "size": pickup.get("size") if pickup else 0,
            } if pickup else None,
            "repo": UPDATE_REPO,
        })
    except Exception as e:
        log.warning(f"Update check failed: {e}")
        return jsonify({"ok": False, "error": str(e), "local_version": APP_VERSION}), 500


@app.route("/api/update/status")
def api_update_status():
    """Snapshot of the current download state. Cheap, lock-held briefly."""
    with _update_lock:
        return jsonify(dict(_update_state))


def _download_worker(url, dest_path, asset_name):
    """Background thread: streams `url` to `dest_path` with progress updates.
    Uses a fresh requests.Session so it never reuses auth cookies from
    YTMusic/iTunes sessions. Resumable on retry via Range header."""
    global _update_thread
    sess = requests.Session()
    sess.headers.update({"User-Agent": "AkiMelody-Updater"})
    try:
        with _update_lock:
            _update_state.update({
                "status": "downloading", "progress": 0, "received": 0,
                "total": 0, "asset_name": asset_name, "asset_url": url,
                "error": "", "local_path": str(dest_path),
            })
        # Stream to a temp file first so a partial download is never
        # mistaken for a complete installer on the next launch.
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        with sess.get(url, stream=True, timeout=30, allow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            with _update_lock:
                _update_state["total"] = total
            received = 0
            last_pct = -1
            with open(tmp_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 16):  # 64 KiB
                    if not chunk:
                        continue
                    fh.write(chunk)
                    received += len(chunk)
                    with _update_lock:
                        _update_state["received"] = received
                    if total:
                        pct = int(received * 100 / total)
                        if pct != last_pct:
                            last_pct = pct
                            with _update_lock:
                                _update_state["progress"] = pct
            # Rename .part → final only when the write completed fully.
            tmp_path.replace(dest_path)
        with _update_lock:
            _update_state.update({
                "status": "ready", "progress": 100,
                "local_path": str(dest_path),
            })
    except Exception as e:
        with _update_lock:
            _update_state.update({"status": "error", "error": str(e)})
        log.warning(f"Update download failed: {e}")
    finally:
        _update_thread = None


@app.route("/api/update/download", methods=["POST"])
def api_update_download():
    """Start (or re-check) a background download of the latest installer.
    Idempotent: a download already in progress returns its current state
    without restarting; a previously-completed download returns `ready`."""
    global _update_thread
    body = request.get_json(silent=True) or {}
    url = body.get("url")
    name = body.get("name") or "AkiMelody-setup.exe"
    release_tag = body.get("release_tag") or ""
    release_notes = body.get("release_notes") or ""
    release_html_url = body.get("release_html_url") or ""

    with _update_lock:
        cur = _update_state["status"]
        if cur == "downloading" and _update_thread and _update_thread.is_alive():
            return jsonify({"ok": True, "state": dict(_update_state), "already_running": True})
        if cur == "ready" and _update_state.get("local_path") and Path(_update_state["local_path"]).exists():
            return jsonify({"ok": True, "state": dict(_update_state), "already_ready": True})

    if not url:
        return jsonify({"ok": False, "error": "url_required"}), 400

    # Stable filename: include the release tag so multiple releases' installers
    # coexist in the cache dir and the user can roll back manually.
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "AkiMelody-setup")
    dest = _update_dir() / f"{release_tag or 'unknown'}_{safe_name}"

    with _update_lock:
        _update_state.update({
            "release_tag": release_tag,
            "release_notes": release_notes,
            "release_html_url": release_html_url,
        })
    _update_thread = threading.Thread(
        target=_download_worker, args=(url, dest, name),
        daemon=True, name="AkiUpdateDownload"
    )
    _update_thread.start()
    return jsonify({"ok": True, "state": dict(_update_state)})


@app.route("/api/update/launch", methods=["POST"])
def api_update_launch():
    """Shell-execute the downloaded installer (silent mode) and signal the
    client to gracefully quit. Returns the launch method the frontend should
    use (native bridge vs `window.open` fallback vs filesystem URL).

    Foolproof flow mirroring webview_launcher.py's launch_installer: copy the
    installer to %TEMP%, then spawn a DETACHED batch wrapper that POLLS for the
    AkiMelody process to exit (up to ~60 s) — with `taskkill /F` as a fallback
    before running the installer silently. This avoids the Windows "file in
    use" dialog that Inno's `CloseApplications=yes` would otherwise pop on a
    silent install, because the conflicting process is already gone by the
    time the installer scans for it."""
    with _update_lock:
        path = _update_state.get("local_path") or ""
        status = _update_state.get("status")
    if status != "ready" or not path or not Path(path).exists():
        return jsonify({"ok": False, "error": "installer_not_ready"}), 409

    try:
        import tempfile as _tf
        import shutil as _shutil
        import datetime as _dt
        tmp_dir = _tf.gettempdir()
        tmp_installer = os.path.join(tmp_dir, "AkiMelody-Setup.exe")
        try:
            _shutil.copy2(path, tmp_installer)
        except Exception:
            tmp_installer = path

        # Generate the polling wrapper batch (same script the native bridge
        # uses) and spawn it as a DETACHED process so it survives the Flask
        # backend teardown.
        bat_path = os.path.join(tmp_dir, "akimelody_update_flask.bat")
        log_path = os.path.join(tmp_dir, "akimelody_update.log")
        now = _dt.datetime.now().isoformat(timespec="seconds")
        bat = (
            "@echo off\r\n"
            f"echo [{now}] flask update wrapper start >> \"{log_path}\"\r\n"
            f"echo installer={tmp_installer} >> \"{log_path}\"\r\n"
            "set /a tries=0\r\n"
            ":wait_loop\r\n"
            "set /a tries+=1\r\n"
            "tasklist /FI \"IMAGENAME eq AkiMelody.exe\" 2>nul | find /I \"AkiMelody.exe\" >nul\r\n"
            "if errorlevel 1 goto gone\r\n"
            "if %tries% GEQ 60 goto force_kill\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            "goto wait_loop\r\n"
            ":force_kill\r\n"
            f"echo [{now}] process still alive — taskkill /F >> \"{log_path}\"\r\n"
            "taskkill /F /IM AkiMelody.exe >nul 2>&1\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            ":gone\r\n"
            f"echo [{now}] process gone — running installer >> \"{log_path}\"\r\n"
            f'"{tmp_installer}" /SILENT /NORESTART >> "{log_path}" 2>&1\r\n'
            f"echo [{now}] installer exit code %ERRORLEVEL% >> \"{log_path}\"\r\n"
            "(start /b \"\" cmd /c \"timeout /t 2 /nobreak >nul & del \"%~f0\"\")\r\n"
        )
        try:
            with open(bat_path, "w", encoding="ascii", errors="replace") as fh:
                fh.write(bat)
        except Exception as e:
            return jsonify({"ok": False, "error": f"wrapper_write_failed: {e}"}), 500

        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        proc = subprocess.Popen(
            f'cmd.exe /c "{bat_path}"',
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            shell=False,
        )
        return jsonify({
            "ok": True,
            "method": "shell_execute",
            "local_path": tmp_installer,
            "pid": proc.pid,
            "wrapper": bat_path,
        })
    except Exception as e:
        # Fallback: reveal the file so the user can double-click it.
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e), "fallback": "explorer"}), 500


@app.route("/api/update/dismiss", methods=["POST"])
def api_update_dismiss():
    """User dismissed the update toast. We don't kill an in-flight download
    (the user might still want it from Settings), but we DO clear the
    current pending release so the toast doesn't resurrect on the next
    6h poll cycle until a newer release appears."""
    with _update_lock:
        # Keep download progress/local_path — only clear the "available"
        # signal fields the frontend uses to decide whether to show the toast.
        _update_state["release_tag"] = ""
        _update_state["release_notes"] = ""
        _update_state["release_html_url"] = ""
    return jsonify({"ok": True})


@app.route("/api/update/clear", methods=["POST"])
def api_update_clear():
    """Wipe the cached installer + reset state. Called from Settings when the
    user wants to free disk space or retry a corrupted download."""
    with _update_lock:
        path = _update_state.get("local_path") or ""
        _update_state.update({
            "status": "idle", "progress": 0, "received": 0, "total": 0,
            "asset_name": "", "asset_url": "", "release_tag": "",
            "release_notes": "", "release_html_url": "", "error": "",
            "local_path": "",
        })
    if path:
        try:
            p = Path(path)
            if p.exists():
                p.unlink()
            # Also clean partial downloads + sibling installers from earlier
            # releases — the user explicitly asked to clear cache.
            for f in p.parent.glob("*.part"):
                try: f.unlink()
                except Exception: pass
        except Exception:
            pass
    return jsonify({"ok": True})


_CHANGELOG_PATH = BASE_DIR / "CHANGELOG.md"


@app.route("/api/update/changelog")
def api_update_changelog():
    """Return the local CHANGELOG.md with version and hash."""
    try:
        raw = _CHANGELOG_PATH.read_text(encoding="utf-8")
    except Exception:
        raw = ""
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "body": raw,
        "hash": hashlib.md5(raw.encode()).hexdigest() if raw else "",
    })




# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    print("[AkiMelody] server listening on http://0.0.0.0:5000", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
