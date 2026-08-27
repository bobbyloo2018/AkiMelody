"""
AkiMelody (秋メロディ) — Flask Backend
"""

from flask import Flask, render_template, request, jsonify, send_from_directory, Response, redirect as _flask_redirect
import requests
try:
    from curl_cffi.requests import Session as _CurlSession
    _curl = _CurlSession(impersonate="chrome")
    _stream_curl = _CurlSession(impersonate="chrome")
    print("[INIT] curl_cffi loaded — Chrome TLS impersonation active", flush=True)
except Exception as _e:
    _curl = None
    _stream_curl = None
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
import tempfile
import logging
import unicodedata
import urllib.parse
import wikipediaapi
from difflib import SequenceMatcher
from contextlib import contextmanager
from pathlib import Path
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from ytmusicapi import YTMusic
import ytmusic_auth as yauth
from data_paths import prepare_data_paths, resolve_bundled_tool
from security_utils import (
    UPDATE_MAX_BYTES,
    env_flag,
    is_allowed_update_url,
    is_loopback_address,
    is_trusted_installer,
    safe_filename_component,
)
# Cover Art Fetching Engine — uniform ranked provider pipeline.
# Imported lazily so a missing httpx in a stripped-down dev env never breaks
# Flask startup; the enrich route resolves it on first use.
artwork_fetcher = None
def _ensure_artwork_fetcher():
    global artwork_fetcher
    if artwork_fetcher is None:
        import artwork_fetcher as _af
        artwork_fetcher = _af
        artwork_fetcher.configure_cache(BASE_DIR / "artwork_resolutions.json")
    return artwork_fetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("akimelody")

# ── Application version (SemVer) ──────────────────────────────────────────────
# Surfaced via /api/settings and used by the automatic background updater to
# compare against the latest GitHub release tag. Bump this on every release.
APP_VERSION = "1.0.5"

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
DATA_PATHS, _DATA_MIGRATION = prepare_data_paths(__file__)
RESOURCE_DIR = DATA_PATHS.resources
# BASE_DIR remains as a compatibility alias for code that reports paths
# relative to the mutable data root.
BASE_DIR = DATA_PATHS.root
app = Flask(
    __name__,
    template_folder=str(RESOURCE_DIR / "templates"),
    static_folder=str(RESOURCE_DIR / "static"),
)
if _DATA_MIGRATION.get("copied"):
    log.info(
        "Migrated %s legacy data files into %s (originals retained)",
        _DATA_MIGRATION["copied"],
        BASE_DIR,
    )
SERVER_PORT = int(os.environ.get("AKI_SERVER_PORT", "5000"))
LAN_ACCESS_ENABLED = env_flag("AKI_ALLOW_LAN")
LAN_ACCESS_TOKEN = os.environ.get("AKI_LAN_TOKEN", "").strip()
LAN_ACCESS_COOKIE = "aki_lan_access"
ANDROID_ACCESS_TOKEN = os.environ.get("AKI_ANDROID_TOKEN", "").strip()
ANDROID_ACCESS_COOKIE = "aki_android_access"
IS_ANDROID = env_flag("AKI_ANDROID")
SYNCEDLYRICS_ENABLED = not IS_ANDROID
SERVER_BIND_HOST = "0.0.0.0" if LAN_ACCESS_ENABLED else "127.0.0.1"
if LAN_ACCESS_ENABLED and len(LAN_ACCESS_TOKEN) < 20:
    raise RuntimeError(
        "AKI_ALLOW_LAN requires AKI_LAN_TOKEN with at least 20 characters. "
        "Leave AKI_ALLOW_LAN unset for private localhost-only mode."
    )
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max request body


@app.before_request
def _protect_local_service():
    """Keep LAN access authenticated and reject browser cross-site writes."""
    remote_is_local = is_loopback_address(request.remote_addr)

    # Android apps share a device network namespace, so loopback binding alone
    # doesn't prove the caller is our WebView. The shell supplies a fresh token
    # at process startup, exchanges it once for an HttpOnly cookie, and never
    # exposes it to JavaScript or persistent storage.
    if ANDROID_ACCESS_TOKEN:
        supplied = request.headers.get("X-Aki-Android-Token", "") or request.cookies.get(ANDROID_ACCESS_COOKIE, "")
        query_token = request.args.get("android_token", "")
        if request.method == "GET" and request.path == "/mobile" and query_token:
            if secrets.compare_digest(query_token, ANDROID_ACCESS_TOKEN):
                query = [
                    (key, value)
                    for key in request.args
                    if key != "android_token"
                    for value in request.args.getlist(key)
                ]
                target = request.path
                if query:
                    target += "?" + urllib.parse.urlencode(query)
                response = _flask_redirect(target)
                response.set_cookie(
                    ANDROID_ACCESS_COOKIE,
                    ANDROID_ACCESS_TOKEN,
                    httponly=True,
                    samesite="Strict",
                    secure=False,
                )
                response.headers["Cache-Control"] = "no-store"
                return response
        if not supplied or not secrets.compare_digest(supplied, ANDROID_ACCESS_TOKEN):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "android_auth_required"}), 401
            return Response("AkiMelody Android access token required.", status=401, mimetype="text/plain")

    if LAN_ACCESS_ENABLED and not remote_is_local:
        supplied = request.headers.get("X-Aki-Token", "") or request.cookies.get(LAN_ACCESS_COOKIE, "")
        query_token = request.args.get("token", "")

        # A one-time URL is convenient on phones; immediately replace it with
        # an HttpOnly, same-site cookie and remove the secret from the address.
        if request.method == "GET" and request.path in {"/", "/mobile"} and query_token:
            if secrets.compare_digest(query_token, LAN_ACCESS_TOKEN):
                query = [
                    (key, value)
                    for key in request.args
                    if key != "token"
                    for value in request.args.getlist(key)
                ]
                target = request.path
                if query:
                    target += "?" + urllib.parse.urlencode(query)
                response = _flask_redirect(target)
                response.set_cookie(
                    LAN_ACCESS_COOKIE,
                    LAN_ACCESS_TOKEN,
                    max_age=30 * 24 * 60 * 60,
                    httponly=True,
                    samesite="Strict",
                    secure=request.is_secure,
                )
                response.headers["Cache-Control"] = "no-store"
                return response
        if not supplied or not secrets.compare_digest(supplied, LAN_ACCESS_TOKEN):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "lan_auth_required"}), 401
            return Response("AkiMelody LAN access token required.", status=401, mimetype="text/plain")

    # CORS does not prevent every cross-site form submission. When a browser
    # supplies Origin for a write, require it to match this exact app origin.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin")
        if origin:
            try:
                parsed = urllib.parse.urlparse(origin)
                same_origin = parsed.scheme in {"http", "https"} and parsed.netloc.lower() == request.host.lower()
            except ValueError:
                same_origin = False
            if not same_origin:
                return jsonify({"ok": False, "error": "cross_origin_request_blocked"}), 403


@app.after_request
def _no_store_word_sync(resp):
    # The Word Sync addon/fetcher are under active development; never let a
    # persistent browser/WebView2 cache serve stale copies.
    if request.path.startswith("/static/js/word-"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

SAVED_DIR = DATA_PATHS.saved
SAVED_DIR.mkdir(exist_ok=True)
FAVORITES_JSON = BASE_DIR / "favorites.json"

MUSIC_LIBRARY_DIR = DATA_PATHS.music_library
PLAYLISTS_DIR = DATA_PATHS.playlists
PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
PLAYLIST_STAGING_DIR = PLAYLISTS_DIR / ".staging"
PLAYLIST_STAGING_DIR.mkdir(parents=True, exist_ok=True)
LYRICS_DIR = DATA_PATHS.lyrics
LYRICS_DIR.mkdir(parents=True, exist_ok=True)

_STREAM_CACHE_MAX = 100
_stream_cache = OrderedDict()  # tid -> {"url": str, "exp": float}  (LRU, max _STREAM_CACHE_MAX)
_stream_cache_lock = threading.Lock()
_stream_resolution_lock = threading.Lock()
_stream_resolution_inflight = {}
_STREAM_URL_EXPIRE_RE = re.compile(r"[?&]expire=(\d+)")

# Stable recording identities live much longer than googlevideo stream URLs.
# Keeping these caches separate lets queue look-ahead resolve an imported track
# early without creating an expiring audio URL until playback is close.
RECORDING_RESOLUTIONS_JSON = BASE_DIR / "recording_resolutions.json"
_RECORDING_RESOLUTION_SCHEMA = 2
_RECORDING_RESOLUTION_TTL = 45 * 24 * 60 * 60
_RECORDING_RESOLUTION_MAX = 2500
_recording_resolution_cache = None
_recording_resolution_lock = threading.RLock()
_recording_resolution_inflight = {}
_recording_prefetch_pending = set()

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

def _cache_stream(tid: str, url: str, identity: str = "", metadata: dict | None = None) -> None:
    """LRU-bound write under _stream_cache_lock. Single source of truth for stream
    URL caching. Skip writes when tid is empty (route variants can call without a tid).
    Stores the parsed `expire=<unix_secs>` so readers can evict stale entries proactively."""
    if not tid:
        return
    with _stream_cache_lock:
        entry = {"url": url, "exp": _extract_url_expiry(url), "identity": identity}
        if metadata:
            for key in ("matchedVideoId", "matchConfidence", "recordingResolved"):
                if metadata.get(key) is not None:
                    entry[key] = metadata[key]
        _stream_cache[tid] = entry
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
_artwork_jobs = OrderedDict()
_artwork_jobs_lock = threading.Lock()
_ARTWORK_JOBS_MAX = 100

_playlist_index_cache = None   # cached list from api_playlists
_playlist_index_lock = threading.Lock()
_playlist_index_generation = 0
_playlist_catalog_lock = threading.RLock()
_playlist_locks_guard = threading.Lock()
_playlist_locks = {}

# ── Shared HTTP sessions (connection pooling) ─────────────────────────────────
_itunes_session = requests.Session()
_itunes_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
})
_lrclib_session = requests.Session()
_lrclib_session.headers.update({"Accept": "application/json"})


class _BoundedRequestsSession(requests.Session):
    """Session with a default deadline for libraries which omit one."""
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 15)
        return super().request(method, url, **kwargs)

# ── Shared thread pool (avoids per-request creation/teardown) ─────────────────
_io_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="aki-io")
_download_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aki-dl")
_offline_collection_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aki-offline")
_offline_asset_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aki-offline-assets")
_resolution_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aki-resolve")
_artwork_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aki-art")
_stream_warm_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aki-stream-warm")
_stream_warm_lock = threading.Lock()
_stream_warm_latest = {}
_stream_warm_pending = set()

_offline_collection_jobs = OrderedDict()
_offline_collection_jobs_lock = threading.Lock()
_OFFLINE_COLLECTION_JOBS_MAX = 100

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


def _atomic_write_json(path, data):
    """Write *data* as JSON to *path* atomically (temp-file + os.replace).

    If the target is ``favorites.json`` or ``settings.json``, a crash or power
    loss mid-write can no longer leave a truncated file; the reader will always
    see the previous complete version until the ``os.replace`` completes.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
_LYRICS_CACHE_VERSION = 2

# ── Artist bio cache (LRU with TTL) ──────────────────────────────────────────
_artist_bio_cache = OrderedDict()  # name -> (result, timestamp)
_artist_bio_lock = threading.Lock()
_ARTIST_BIO_CACHE_MAX = 200
_ARTIST_BIO_CACHE_TTL = 3600  # 1 hour

# ── Radio suggestion cache (LRU with TTL) ─────────────────────────────────────
# Radio mode re-seeds the same video id many times per session, and each seed
# triggers an expensive get_watch_playlist() + per-track artwork resolution.
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
_LIKED_TTL = 30  # 30 seconds - shorter to reduce stale auth window

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
_yt_oauth_file = BASE_DIR / "oauth.json"
_yt_oauth_credentials_file = BASE_DIR / "youtube_oauth_credentials.json"
_youtube_oauth_pending = OrderedDict()
_youtube_oauth_lock = threading.Lock()
_YOUTUBE_OAUTH_PENDING_MAX = 4
_YOUTUBE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_YOUTUBE_OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube"

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
            kwargs = {"domain": domain, "path": path}
            if expires and expires != "0":
                try:
                    kwargs["expires"] = int(expires)
                except Exception:
                    pass
            session.cookies.set(name, value, **kwargs)

def _generate_auth_headers(cookie_text: str) -> bool:
    """Generate headers.json for ytmusicapi from cookies.txt content.
    The request Cookie header mirrors what a browser sends to music.youtube.com:
    only YouTube-domain cookies. Google-domain cookies remain in cookies.txt and
    the requests cookie jar, but are never flattened into a cross-domain header."""
    cookie_header = _parse_netscape_cookies(cookie_text, youtube_only=True)
    if not cookie_header:
        print("[AUTH] _generate_auth_headers: no cookies parsed from cookie text", flush=True)
        return False

    # Extract SAPISID or __Secure-1PSID from parsed cookies for real SAPISIDHASH
    sapisid = None
    all_cookie_header = _parse_netscape_cookies(cookie_text, youtube_only=False)
    for pair in (cookie_header + "; " + all_cookie_header).split("; "):
        if pair.startswith("SAPISID="):
            sapisid = pair.split("=", 1)[1]
            break
        if pair.startswith("__Secure-1PSID="):
            sapisid = pair.split("=", 1)[1]
            break

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_header,
        "Origin": "https://music.youtube.com",
        "X-Origin": "https://music.youtube.com",
    }

    # Compute real SAPISIDHASH if SAPISID or __Secure-1PSID is present
    if sapisid:
        try:
            import ytmusic_auth as yauth
            headers["authorization"] = yauth.compute_sapisidhash(sapisid)
            print("[AUTH] Computed real SAPISIDHASH from cookie", flush=True)
        except Exception as exc:
            print(f"[AUTH] Failed to compute SAPISIDHASH: {exc} — using placeholder", flush=True)
            headers["authorization"] = "SAPISIDHASH 0_dummy"
    else:
        headers["authorization"] = "SAPISIDHASH 0_dummy"
        print("[AUTH] No SAPISID/__Secure-1PSID cookie found — using placeholder SAPISIDHASH", flush=True)

    try:
        _yt_headers_file.write_text(json.dumps(headers, indent=2), encoding="utf-8")
        # Log which auth cookies are present for diagnostics
        names = [p.split("=", 1)[0] for p in cookie_header.split("; ") if "=" in p]
        all_names = [p.split("=", 1)[0] for p in (cookie_header + "; " + all_cookie_header).split("; ") if "=" in p]
        has_sapisid = "SAPISID" in all_names
        has_secure_1psid = "__Secure-1PSID" in all_names
        has_sid = "SID" in all_names
        print(f"[AUTH] Generated headers.json ({_yt_headers_file.stat().st_size} bytes): "
              f"SAPISID={'YES' if has_sapisid else 'NO'}, __Secure-1PSID={'YES' if has_secure_1psid else 'NO'}, "
              f"SID={'YES' if has_sid else 'NO'}, total_cookies={len(all_names)}", flush=True)
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
    # build.py installs QuickJS at <resources>/build/qjs.exe in packaged mode;
    # the same path exists in the source checkout. Older bundles placed it at
    # the resource root, so retain that fallback before checking PATH.
    qjs_path = resolve_bundled_tool(
        RESOURCE_DIR,
        (Path("build") / "qjs.exe", Path("qjs.exe")),
        ("qjs", "qjs.exe"),
    )
    qjs = str(qjs_path) if qjs_path else None
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
if _YDL_COMMON_OPTS["js_runtimes"]:
    print(f"[INIT] yt-dlp JavaScript runtime: {_YDL_COMMON_OPTS['js_runtimes']}", flush=True)
else:
    print("[INIT] WARNING: no yt-dlp JavaScript runtime found; YouTube playback may fail", flush=True)

# YDL options shared across extract-only sites (no postprocess, no outtmpl).
# retries=1 cuts the fallback-retry-storm when yt-dlp hits an auth-error in
# the format fall-through chain (default is 3, multiplied by 4 formats = 12 retries).
_YDL_EXTRACT_OPTS = {
    **_YDL_COMMON_OPTS,
    "socket_timeout": 15,
    "retries": 1,
}

# Downloads should fail quickly enough to retry with a fresh extraction, while
# still tolerating the short connection stalls which are common on phones.
# Keep this separate from extract-only options: fragment retries only apply
# while media bytes are being written.
_YDL_DOWNLOAD_OPTS = {
    **_YDL_COMMON_OPTS,
    "socket_timeout": 20,
    "retries": 2,
    "fragment_retries": 2,
    "extractor_retries": 1,
    "concurrent_fragment_downloads": 2,
}

# Module-level UA shared across all yt-dlp sites for the http_headers key.
_YDL_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ── File reverse index (avoids rglob on every /api/local_file request) ────────
_file_index = {}  # filename -> Path (absolute)
_file_index_lock = threading.Lock()
_file_index_ts = 0.0
_FILE_INDEX_TTL = 300.0
_LOCAL_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".webm", ".opus")
_LOCAL_MEDIA_EXTENSIONS = frozenset((*_LOCAL_AUDIO_EXTENSIONS, ".jpg", ".jpeg", ".png", ".webp"))
_LOCAL_MEDIA_MIMETYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".opus": "audio/ogg",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _local_media_mimetype(path: Path) -> str | None:
    """Return an explicit WebView-safe MIME type for downloaded media.

    Chaquopy's compact Python runtime doesn't always ship the same mimetype
    table as desktop Python. With ``nosniff`` enabled, an octet-stream response
    can therefore be rejected by Android's media element even when the bytes
    are a valid m4a/webm/opus file.
    """
    return _LOCAL_MEDIA_MIMETYPES.get(path.suffix.lower())


def _indexed_media_key(path: Path) -> str:
    """Return the canonical lookup name for a playlist media file.

    Playlist downloads may be stored as ``01 - <tid>.mp3`` to preserve album
    ordering. Playback addresses media by the universal track id, so index a
    numbered file under ``<tid>.mp3`` as well as its physical filename.
    """
    if path.suffix.lower() not in _LOCAL_AUDIO_EXTENSIONS:
        return path.name
    stem = path.stem
    if " - " in stem:
        prefix, candidate = stem.split(" - ", 1)
        if prefix.isdigit() and candidate:
            return f"{candidate}{path.suffix.lower()}"
    return path.name

def _get_file_index():
    global _file_index, _file_index_ts
    now = time.time()
    # Rebuild under the lock. The earlier check-then-scan implementation let
    # /api/stream and /api/media/status recursively scan the same library at
    # the same time after expiry, exactly when playback needed the disk least.
    with _file_index_lock:
        if _file_index and (now - _file_index_ts) < _FILE_INDEX_TTL:
            return _file_index
        idx = {}
        if PLAYLISTS_DIR.exists():
            for sub in PLAYLISTS_DIR.rglob("*"):
                if sub.is_file() and sub.suffix.lower() in _LOCAL_MEDIA_EXTENSIONS:
                    idx[sub.name] = sub
                    canonical = _indexed_media_key(sub)
                    idx.setdefault(canonical, sub)
        _file_index = idx
        _file_index_ts = time.time()
        return idx

def _invalidate_file_index():
    global _file_index, _file_index_ts
    with _file_index_lock:
        _file_index = {}
        _file_index_ts = 0.0


def _resolve_local_audio(tid: str, index=None):
    """Resolve a track id to a non-empty local audio file and its storage tier."""
    tid = str(tid or "").strip()
    if not tid or not _SAFE_FILENAME_RE.fullmatch(tid):
        return None, None
    for ext in _LOCAL_AUDIO_EXTENSIONS:
        candidate = SAVED_DIR / f"{tid}{ext}"
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate, "saved"
        except OSError:
            continue
    media_index = index if index is not None else _get_file_index()
    for ext in _LOCAL_AUDIO_EXTENSIONS:
        candidate = media_index.get(f"{tid}{ext}")
        try:
            if candidate and candidate.is_file() and candidate.stat().st_size > 0:
                return candidate, "library"
        except OSError:
            continue
    return None, None


def _resolve_local_art(tid: str, index=None):
    tid = str(tid or "").strip()
    if not tid or not _SAFE_FILENAME_RE.fullmatch(tid):
        return None, None
    candidate = SAVED_DIR / f"{tid}.jpg"
    try:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate, "saved"
    except OSError:
        pass
    media_index = index if index is not None else _get_file_index()
    candidate = media_index.get(f"{tid}.jpg")
    try:
        if candidate and candidate.is_file() and candidate.stat().st_size > 0:
            return candidate, "library"
    except OSError:
        pass
    return None, None


def _local_media_payload(tid: str, index=None) -> dict:
    audio, audio_source = _resolve_local_audio(tid, index=index)
    art, art_source = _resolve_local_art(tid, index=index)
    payload = {
        "local_audio": bool(audio),
        "local_art": bool(art),
        "audio_source": audio_source,
        "art_source": art_source,
    }
    if audio:
        endpoint = "local_file" if audio_source == "saved" else "library_file"
        payload["url"] = f"/api/{endpoint}?q={urllib.parse.quote(audio.name)}"
        payload["format"] = audio.suffix.lower().lstrip(".")
    return payload

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


def _invalidate_downloads_status_cache():
    global _downloads_status_cache, _downloads_status_cache_ts
    _downloads_status_cache = None
    _downloads_status_cache_ts = 0.0

# ── Auth state cache (avoids auth-file stat calls per request) ────────────────
def _load_youtube_oauth_credentials() -> dict:
    client_id = os.environ.get("AKI_YOUTUBE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("AKI_YOUTUBE_OAUTH_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}
    try:
        saved = json.loads(_yt_oauth_credentials_file.read_text(encoding="utf-8"))
        client_id = str(saved.get("client_id") or "").strip()
        client_secret = str(saved.get("client_secret") or "").strip()
        if client_id and client_secret:
            return {"client_id": client_id, "client_secret": client_secret}
    except Exception:
        pass
    return {}


def _ytmusic_player_audio_url(video_id: str) -> str:
    """Return the best direct audio URL already exposed by YTMusic's player API.

    Android's local OAuth-backed YTMusic client can often provide streamingData
    without starting yt-dlp's JavaScript extraction path. Only explicit audio
    formats with a direct URL are accepted; ciphered or unplayable responses
    fall through to the established yt-dlp resolver.
    """
    video_id = str(video_id or "").strip()
    if not video_id:
        return ""
    try:
        payload = ytmusic.get_song(video_id) or {}
        status = (payload.get("playabilityStatus") or {}).get("status")
        if status and status != "OK":
            return ""
        streaming = payload.get("streamingData") or {}
        formats = list(streaming.get("adaptiveFormats") or []) + list(streaming.get("formats") or [])
        audio = []
        for item in formats:
            mime = str(item.get("mimeType") or "").lower()
            url = str(item.get("url") or "")
            if url.startswith("https://") and mime.startswith("audio/"):
                audio.append(item)
        if not audio:
            return ""
        best = max(audio, key=lambda item: (
            3 if "audio/mp4" in str(item.get("mimeType") or "").lower() else
            2 if "audio/webm" in str(item.get("mimeType") or "").lower() else 1,
            int(item.get("bitrate") or 0),
        ))
        return str(best.get("url") or "")
    except Exception as exc:
        log.debug("YTMusic direct player stream unavailable for %s: %s", video_id, exc)
        return ""


def _has_youtube_oauth_state() -> bool:
    try:
        return _yt_oauth_file.is_file() and _yt_oauth_file.stat().st_size > 20 and bool(_load_youtube_oauth_credentials())
    except OSError:
        return False


_auth_state_ok = _has_youtube_oauth_state() or (_yt_headers_file.exists() and _yt_headers_file.stat().st_size > 10)

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
    _atomic_write_json(SETTINGS_JSON, data)
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
    oauth_credentials = _load_youtube_oauth_credentials()
    if _has_youtube_oauth_state() and oauth_credentials:
        try:
            try:
                from ytmusicapi.auth.oauth.credentials import OAuthCredentials
            except ImportError:
                from ytmusicapi import OAuthCredentials
            ytm = YTMusic(
                str(_yt_oauth_file),
                oauth_credentials=OAuthCredentials(
                    client_id=oauth_credentials["client_id"],
                    client_secret=oauth_credentials["client_secret"],
                ),
            )
            print("[INIT] YTMusic authenticated via local OAuth device token", flush=True)
            return ytm
        except Exception as e:
            print(f"[INIT] YTMusic OAuth init failed ({e}), trying browser auth", flush=True)
    if has_valid_auth_state():
        try:
            session = _BoundedRequestsSession()
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
            cookie_text = cookie_file.read_text(encoding="utf-8")
            _generate_auth_headers(cookie_text)
            # Also save to ytmusic_auth.json for Tauri bridge auth status check
            try:
                import json as _json
                headers = _json.loads(_yt_headers_file.read_text(encoding="utf-8"))
                yauth.save_auth(headers)
                print("[AUTH] Rebuild: saved auth to ytmusic_auth.json", flush=True)
            except Exception as e:
                print(f"[AUTH] Rebuild: failed to save ytmusic_auth.json: {e}", flush=True)
        except Exception as e:
            print(f"[AUTH] Rebuild: _generate_auth_headers failed: {e}", flush=True)
    else:
        print("[AUTH] Rebuild: no cookie file found", flush=True)
    # Update _auth_state_ok BEFORE calling _init_ytmusic() so the
    # has_valid_auth_state() check inside _init_ytmusic sees the current state.
    _auth_state_ok = _has_youtube_oauth_state() or (_yt_headers_file.exists() and _yt_headers_file.stat().st_size > 10)
    print(f"[AUTH] Rebuild: _auth_state_ok={_auth_state_ok}", flush=True)
    ytmusic = _init_ytmusic()
    with _liked_lock:
        _liked_cache.clear()
    _invalidate_stream_cache()
    return _auth_state_ok

# ── Backend helpers ───────────────────────────────────────────────────────────

def get_track_id(name, artist):
    return hashlib.md5(f"{str(name).strip()}_{str(artist).strip()}".lower().encode()).hexdigest()


_TRACK_VERSION_PATTERNS = OrderedDict((
    ("live", re.compile(r"\blive\b|\bin concert\b", re.I)),
    ("acoustic", re.compile(r"\bacoustic\b|\bunplugged\b", re.I)),
    ("remix", re.compile(r"\bremix(?:ed)?\b|\bclub mix\b|\bdance mix\b", re.I)),
    ("remaster", re.compile(r"\bremaster(?:ed)?\b(?:\s*\d{2,4})?", re.I)),
    ("instrumental", re.compile(r"\binstrumental\b|\bkaraoke\b|\bminus one\b", re.I)),
    ("cover", re.compile(r"\bcover\b|\btribute\b", re.I)),
    ("demo", re.compile(r"\bdemo\b|\brough mix\b", re.I)),
    ("sped-up", re.compile(r"\bsped[ -]?up\b|\bnightcore\b", re.I)),
    ("slowed", re.compile(r"\bslowed\b|\bslowed\s*(?:and|&)\s*reverb\b", re.I)),
    ("edit", re.compile(r"\bradio edit\b|\bsingle edit\b|\bshort edit\b", re.I)),
))
_IDENTITY_NOISE_RE = re.compile(
    r"\b(?:official\s+)?(?:music\s+)?video\b|\bofficial audio\b|\blyric(?:s)?(?: video)?\b|\bvisuali[sz]er\b",
    re.I,
)
_FEATURE_CREDIT_RE = re.compile(r"\s+(?:feat(?:uring)?|ft)\.?\s+.+$", re.I)


def _coerce_duration_seconds(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    text = str(value).strip()
    try:
        if ":" not in text:
            return float(text)
        parts = [float(part) for part in text.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (TypeError, ValueError):
        pass
    return None


def _normalize_identity_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("&", " and ").replace(" - topic", " ")
    text = _IDENTITY_NOISE_RE.sub(" ", text)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _track_version_profile(title: str) -> dict:
    raw = unicodedata.normalize("NFKC", str(title or ""))
    # Version words are meaningful in qualifiers, not necessarily in the song's
    # actual name ("Live Forever", "Cover Me"). Prefer bracketed and dash/colon
    # suffixes so those ordinary titles do not become false live/cover matches.
    bracketed = list(re.finditer(r"[\(\[\{]([^\)\]\}]+)[\)\]\}]", raw))
    suffix = re.search(r"\s+[\-–—:]\s+(.+)$", raw)
    qualifier_parts = [match.group(1) for match in bracketed]
    if suffix:
        qualifier_parts.append(suffix.group(1))
    qualifier_text = " | ".join(qualifier_parts)
    tags = {name for name, pattern in _TRACK_VERSION_PATTERNS.items() if pattern.search(qualifier_text)}
    base = raw
    for match in reversed(bracketed):
        inner = match.group(1)
        if any(pattern.search(inner) for pattern in _TRACK_VERSION_PATTERNS.values()):
            base = base[:match.start()] + " " + base[match.end():]
    if suffix and any(pattern.search(suffix.group(1)) for pattern in _TRACK_VERSION_PATTERNS.values()):
        base = base[:suffix.start()]
    base = _IDENTITY_NOISE_RE.sub(" ", base)
    base = _FEATURE_CREDIT_RE.sub("", base)
    base = re.sub(r"[\(\[\{]\s*[\-–—,:/]*\s*[\)\]\}]", " ", base)
    base = re.sub(r"\s+[\-–—]\s*$", " ", base)
    return {"base": _normalize_identity_text(base), "tags": tags}


def _identity_similarity(left: str, right: str) -> float:
    a = _normalize_identity_text(left)
    b = _normalize_identity_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    aset, bset = set(a.split()), set(b.split())
    token = len(aset & bset) / max(1, len(aset | bset))
    containment = min(len(aset & bset) / max(1, len(aset)), len(aset & bset) / max(1, len(bset)))
    return max(seq, token * 0.75 + containment * 0.25)


def _artist_identity_compatible(target_artist: str, candidate_artist: str) -> bool:
    """Require a real artist-name relationship, not merely a good song title.

    YouTube cover uploads often repeat the original artist in the video title,
    which can produce an excellent combined score even when the credited artist
    is unrelated. Artist compatibility is therefore a hard gate. Extra credited
    collaborators are allowed, but cover/karaoke/tribute labels are not.
    """
    target_raw = _FEATURE_CREDIT_RE.sub("", str(target_artist or ""))
    candidate_raw = _FEATURE_CREDIT_RE.sub("", str(candidate_artist or ""))
    target = _normalize_identity_text(target_raw)
    candidate = _normalize_identity_text(candidate_raw)
    if not target or not candidate:
        return False
    if target == candidate:
        return True
    # YTMusic returns all credited artists joined by commas. An exact target
    # credit inside that list is safe even when the complete joined string has
    # low edit similarity (for example "Gorillaz, De La Soul").
    candidate_credits = {
        _normalize_identity_text(part)
        for part in re.split(r"\s*,\s*", candidate_raw)
        if _normalize_identity_text(part)
    }
    if target in candidate_credits:
        return True
    target_words = set(target.split())
    candidate_words = set(candidate.split())
    if (candidate_words - target_words) & {"cover", "covers", "karaoke", "tribute", "impersonator"}:
        return False
    common = target_words & candidate_words
    if not common:
        return False
    shorter_coverage = len(common) / max(1, min(len(target_words), len(candidate_words)))
    return shorter_coverage >= 0.8 and _identity_similarity(target, candidate) >= 0.62


def _score_track_candidate(target: dict, candidate: dict) -> dict:
    """Score candidate identity while explicitly penalizing version drift."""
    target_profile = _track_version_profile(target.get("title") or target.get("name") or "")
    candidate_profile = _track_version_profile(candidate.get("title") or candidate.get("name") or candidate.get("trackName") or "")
    title_sim = _identity_similarity(target_profile["base"], candidate_profile["base"])
    artist_sim = _identity_similarity(
        _FEATURE_CREDIT_RE.sub("", str(target.get("artist") or target.get("artistName") or "")),
        _FEATURE_CREDIT_RE.sub("", str(candidate.get("artist") or candidate.get("artistName") or "")),
    )
    artist_compatible = _artist_identity_compatible(
        target.get("artist") or target.get("artistName") or "",
        candidate.get("artist") or candidate.get("artistName") or "",
    )
    album_sim = _identity_similarity(target.get("album") or "", candidate.get("album") or candidate.get("albumName") or "")
    target_album_id = str(target.get("albumId") or target.get("album_id") or "").strip()
    candidate_album_id = str(candidate.get("albumId") or candidate.get("album_id") or "").strip()
    target_duration = _coerce_duration_seconds(target.get("duration") or target.get("dur"))
    candidate_duration = _coerce_duration_seconds(candidate.get("duration") or candidate.get("dur"))
    duration_delta = abs(target_duration - candidate_duration) if target_duration is not None and candidate_duration is not None else None
    missing_tags = target_profile["tags"] - candidate_profile["tags"]
    unexpected_tags = candidate_profile["tags"] - target_profile["tags"]

    score = title_sim * 55 + artist_sim * 25
    if target_profile["tags"] == candidate_profile["tags"]:
        score += 10
    score -= len(missing_tags) * 17
    score -= len(unexpected_tags) * 22
    if album_sim:
        score += album_sim * 5
    if target_album_id and candidate_album_id:
        score += 10 if target_album_id == candidate_album_id else -4
    video_type = str(candidate.get("videoType") or "").upper()
    if video_type.endswith("_ATV"):
        score += 7  # canonical YouTube Music album audio
    elif video_type.endswith("_OMV"):
        score += 1  # official video; may contain a different intro/outro
    if duration_delta is not None:
        if duration_delta <= 2:
            score += 18
        elif duration_delta <= 5:
            score += 14
        elif duration_delta <= 12:
            score += 7
        elif duration_delta > 30:
            score -= 22
        elif duration_delta > 20:
            score -= 12

    material_versions = {"live", "acoustic", "remix", "instrumental", "cover", "demo", "sped-up", "slowed", "edit"}
    hard_conflict = bool((unexpected_tags | missing_tags) & material_versions)
    acceptable = title_sim >= 0.68 and artist_compatible and score >= 62 and not hard_conflict
    return {
        "score": round(score, 2),
        "acceptable": acceptable,
        "titleSimilarity": round(title_sim, 3),
        "artistSimilarity": round(artist_sim, 3),
        "artistCompatible": artist_compatible,
        "durationDelta": round(duration_delta, 2) if duration_delta is not None else None,
        "missingVersions": sorted(missing_tags),
        "unexpectedVersions": sorted(unexpected_tags),
    }


def _rank_track_candidates(target: dict, candidates: list, minimum: float = 62) -> list:
    ranked = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        match = _score_track_candidate(target, candidate)
        if match["acceptable"] and match["score"] >= minimum:
            enriched = dict(candidate)
            enriched["_match"] = match
            ranked.append(enriched)
    ranked.sort(key=lambda item: item["_match"]["score"], reverse=True)
    return ranked


def _lyrics_identity_signature(title: str, artist: str, duration=None) -> str:
    profile = _track_version_profile(title)
    dur = _coerce_duration_seconds(duration)
    identity = "|".join((profile["base"], ",".join(sorted(profile["tags"])), _normalize_identity_text(artist), str(round(dur or 0))))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _recording_identity_signature(title: str, artist: str, duration=None,
                                  album: str = "", album_id: str = "") -> str:
    """Stable key for a recording choice; unlike a stream URL it survives restarts."""
    profile = _track_version_profile(title)
    dur = _coerce_duration_seconds(duration)
    identity = "|".join((
        profile["base"],
        ",".join(sorted(profile["tags"])),
        _normalize_identity_text(artist),
        str(round(dur or 0)),
        _normalize_identity_text(album),
        str(album_id or "").strip(),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _art_source_hint(url: str) -> str:
    lower = str(url or "").lower()
    if lower.startswith(("/api/", "/static/", "app:")):
        return "local"
    if "mzstatic.com" in lower or "itunes.apple.com" in lower:
        return "apple"
    if "i.scdn.co" in lower or "scdn.co" in lower:
        return "spotify"
    if "ytimg.com" in lower or "googleusercontent.com" in lower or "img.youtube.com" in lower:
        return "youtube"
    return "provider" if lower else ""

def _build_track_dict(name, artist, art, dur, tid, videoId, albumId="", **extra):
    local_audio, _audio_source = _resolve_local_audio(tid)
    local_art, _art_source = _resolve_local_art(tid)
    d = {
        "name": name, "artist": artist,
        "art": art, "dur": dur, "tid": tid,
        "videoId": videoId,
        "albumId": albumId or "",
        "local_audio": bool(local_audio),
        "local_art": bool(local_art),
    }
    d.update(extra)
    candidates = d.get("art_candidates") if isinstance(d.get("art_candidates"), list) else []
    candidates = [str(url) for url in candidates if url]
    if art:
        candidates.insert(0, art)
    if videoId:
        candidates.append(f"https://i.ytimg.com/vi/{videoId}/maxresdefault.jpg")
        candidates.append(f"https://i.ytimg.com/vi/{videoId}/hqdefault.jpg")
    d["art_candidates"] = list(dict.fromkeys(candidates))[:6]
    # Update art to the best available candidate (highest‑resolution, first in list)
    if d["art_candidates"] and d["art_candidates"][0] != d["art"]:
        d["art"] = d["art_candidates"][0]
    d.setdefault("art_source", _art_source_hint(art))
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
        # The old JP storefront default localized otherwise-English releases and
        # could return an artist identity which no longer matched the recording
        # resolver. Use the US English catalogue as the neutral default, then
        # rank exact title/artist text ahead of Apple's broader popularity order.
        params = {
            "term": query,
            "media": "music",
            "entity": "song",
            "limit": limit,
            "country": "US",
            "lang": "en_us",
        }
        resp = _itunes_session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        results = []
        query_text = _normalize_identity_text(query)
        query_words = set(query_text.split())
        for provider_index, item in enumerate(resp.json().get("results", [])):
            name = item.get("trackName", "Unknown Track")
            artist = item.get("artistName", "Unknown Artist")
            art = item.get("artworkUrl100", "")
            tid = get_track_id(name, artist)
            title_text = _normalize_identity_text(name)
            artist_text = _normalize_identity_text(artist)
            searchable_words = set(f"{title_text} {artist_text}".split())
            matched_words = len(query_words & searchable_words)
            score = matched_words * 18
            if query_text == title_text:
                score += 120
            elif title_text.startswith(query_text):
                score += 75
            elif query_text and query_text in title_text:
                score += 45
            if query_words and query_words.issubset(searchable_words):
                score += 35
            # For an ASCII/English query, gently prefer readable Latin metadata.
            # This is a tie-breaker rather than a filter, so international songs
            # and artists remain available when the user searches for them.
            if query.isascii():
                letters = [char for char in artist if char.isalpha()]
                latin_letters = [char for char in letters if "LATIN" in unicodedata.name(char, "")]
                if letters:
                    score += 12 * (len(latin_letters) / len(letters))
            results.append({
                "name": name, "artist": artist,
                "art": art.replace("100x100bb.jpg", "600x600bb.jpg"),
                "dur": int(item.get("trackTimeMillis", 210000) / 1000),
                "tid": tid,
                "_search_score": score,
                "_provider_index": provider_index,
            })
        results.sort(key=lambda item: (-item["_search_score"], item["_provider_index"]))
        for item in results:
            item.pop("_search_score", None)
            item.pop("_provider_index", None)
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
    _invalidate_downloads_status_cache()

def _download_source_for_track(track: dict) -> tuple[str, str]:
    """Resolve one recording-accurate YouTube source for an offline download."""
    vid = str(track.get("videoId") or "").strip()
    if not vid:
        resolved = resolve_recording(
            track.get("name") or track.get("title") or "",
            track.get("artist") or track.get("artist_name") or "",
            track.get("dur") or track.get("duration"),
            track.get("album") or track.get("albumName") or "",
            track.get("albumId") or "",
        )
        vid = str((resolved or {}).get("videoId") or "")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}", vid
    raise LookupError(
        f"No exact recording found for {track.get('artist', '')} - {track.get('name', '')}"
    )


def _artwork_urls_for_track(track: dict, resolve_missing: bool = False) -> list[str]:
    """Return one deduplicated, resolver-ranked URL list for all download paths."""
    source = dict(track or {})
    candidates = source.get("art_candidates") if isinstance(source.get("art_candidates"), list) else []
    urls = []
    for item in candidates:
        urls.append(item.get("url", "") if isinstance(item, dict) else item)
    urls.extend((source.get("album_art", ""), source.get("art", ""), source.get("thumbnail", "")))
    if resolve_missing and len([url for url in urls if url]) < 2:
        try:
            resolved = _ensure_artwork_fetcher().resolve_artwork(source)
            ranked = resolved.get("art_candidates") if isinstance(resolved.get("art_candidates"), list) else []
            urls = list(ranked) + [resolved.get("album_art", ""), resolved.get("art", "")] + urls
        except Exception as exc:
            log.debug("Artwork resolution before download failed: %s", exc)
    return list(dict.fromkeys(
        str(url).strip() for url in urls
        if isinstance(url, str) and url.strip().startswith(("https://", "http://"))
    ))[:6]


def _is_raster_art(content: bytes, content_type: str = "") -> bool:
    """Reject error pages and SVG/script payloads before caching as local art."""
    if "svg" in str(content_type).lower() or not content or len(content) < 128:
        return False
    head = content[:32]
    return (
        head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n") or
        head.startswith((b"GIF87a", b"GIF89a")) or
        (head.startswith(b"RIFF") and head[8:12] == b"WEBP") or
        (len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis", b"heic", b"heix"))
    )


def _download_first_artwork(candidates: list[str]) -> tuple[bytes | None, str]:
    """Fetch the first valid distinct candidate once, bounded to 8 MiB."""
    for url in list(dict.fromkeys(candidates or []))[:4]:
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        try:
            if _is_private_or_link_local(url):
                continue
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "image/*", "Referer": url}
            response = (_curl.get(url, timeout=8, headers=headers, stream=True) if _curl is not None else
                        requests.get(url, timeout=8, allow_redirects=True, headers=headers, stream=True))
            try:
                if response.status_code != 200:
                    continue
                chunks, total = [], 0
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        chunks = []
                        break
                    chunks.append(chunk)
                content = b"".join(chunks)
                if _is_raster_art(content, response.headers.get("Content-Type", "")):
                    return content, url
            finally:
                response.close()
        except Exception:
            continue
    return None, ""


def _android_stream_extension(content_type: str, url: str) -> str:
    """Map a resolved audio response to a WebView-compatible local suffix."""
    value = urllib.parse.unquote(f"{content_type} {url}").lower()
    if "webm" in value:
        return ".webm"
    if "opus" in value or "audio/ogg" in value:
        return ".opus"
    return ".m4a"


def _download_android_resolved_audio(track: dict, target_dir: Path, stem: str) -> tuple[Path, str]:
    """Download through the same accurate resolver used by foreground playback.

    This avoids a second independent yt-dlp extraction in Chaquopy, joins any
    in-flight playback resolution, and refreshes a stale signed URL internally.
    """
    title = str(track.get("name") or track.get("title") or "").strip()
    artist = str(track.get("artist") or track.get("artist_name") or "").strip()
    tid = str(track.get("tid") or get_track_id(title, artist))
    query = f"{artist} {title} audio".strip()
    last_error = "No playable stream was resolved"
    target_dir.mkdir(parents=True, exist_ok=True)
    partial = target_dir / f".{stem}.{threading.get_ident()}.audio.part"
    partial_suffix = ""

    try:
        for force in (False, True):
            result = resolve_stream_singleflight(
                query, tid, str(track.get("videoId") or ""), force=force,
                title=title, artist=artist,
                duration=track.get("dur") or track.get("duration"),
                album=track.get("album") or track.get("albumName") or "",
                album_id=track.get("albumId") or "",
            )
            url = str((result or {}).get("url") or "")
            if not url:
                last_error = str((result or {}).get("error") or last_error)
                continue

            # Two transport attempts preserve already-written bytes. If the
            # signed URL itself has expired, the outer forced resolution obtains
            # a fresh URL and resumes the same partial file with a Range request.
            for transport_attempt in range(2):
                response = None
                try:
                    offset = partial.stat().st_size if partial.exists() else 0
                    headers = {
                        "User-Agent": _YDL_USER_AGENT,
                        "Referer": "https://www.youtube.com/",
                        "Origin": "https://www.youtube.com",
                        "Accept-Encoding": "identity",
                        "Range": f"bytes={offset}-",
                    }
                    session = _stream_curl if _stream_curl is not None else _fallback_proxy_session
                    response = session.get(url, headers=headers, timeout=30, stream=True)
                    if response.status_code not in (200, 206):
                        last_error = f"Media host returned HTTP {response.status_code}"
                        if response.status_code in (403, 410):
                            break
                        continue
                    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                    if content_type and not (
                        content_type.startswith(("audio/", "video/")) or
                        content_type in ("application/octet-stream", "binary/octet-stream")
                    ):
                        last_error = f"Media host returned {content_type}"
                        break
                    suffix = _android_stream_extension(content_type, url)
                    if partial_suffix and partial_suffix != suffix and offset:
                        partial.unlink(missing_ok=True)
                        partial_suffix = suffix
                        last_error = "Resolved audio format changed; restarting transfer"
                        continue
                    partial_suffix = suffix
                    append = response.status_code == 206 and offset > 0
                    if not append:
                        offset = 0
                    expected_total = 0
                    content_range = str(response.headers.get("Content-Range") or "")
                    range_total = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
                    if range_total.isdigit():
                        expected_total = int(range_total)
                    elif response.headers.get("Content-Length"):
                        expected_total = offset + int(response.headers["Content-Length"])
                    with open(partial, "ab" if append else "wb") as output:
                        for chunk in response.iter_content(chunk_size=256 * 1024):
                            if chunk:
                                output.write(chunk)
                    completed_size = partial.stat().st_size
                    if completed_size < 16 * 1024:
                        raise RuntimeError("Resolved media response was unexpectedly short")
                    if expected_total and completed_size < expected_total:
                        raise RuntimeError(f"Media transfer stopped at {completed_size} of {expected_total} bytes")
                    destination = target_dir / f"{stem}{suffix}"
                    os.replace(partial, destination)
                    matched_vid = str((result or {}).get("matchedVideoId") or track.get("videoId") or "")
                    return destination, matched_vid
                except Exception as exc:
                    last_error = str(exc)
                finally:
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
        raise RuntimeError(last_error)
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def download_track(track: dict) -> bool:
    tid = track.get('tid') or get_track_id(track['name'], track['artist'])
    audio_path, _storage_tier = _resolve_local_audio(tid)
    if not audio_path:
        try:
            if IS_ANDROID:
                _downloaded, _resolved_vid = _download_android_resolved_audio(
                    dict(track, tid=tid), SAVED_DIR, tid,
                )
            else:
                source, _resolved_vid = _download_source_for_track(track)
                ydl_opts = {
                    'format': 'bestaudio/best', 'outtmpl': str(SAVED_DIR / tid),
                    'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}],
                    **_YDL_DOWNLOAD_OPTS,
                    "http_headers": {"User-Agent": _YDL_USER_AGENT},
                    **_ydl_extras(),
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([source])
                desktop_audio_path = SAVED_DIR / f"{tid}.mp3"
                for f in SAVED_DIR.glob(f"{tid}*"):
                    if f.suffix == ".mp3":
                        if f.name != f"{tid}.mp3": f.rename(desktop_audio_path)
                        break
                    elif f.suffix in [".m4a", ".webm", ".opus"]:
                        f.rename(desktop_audio_path)
                        break
            audio_path, _storage_tier = _resolve_local_audio(tid)
            if not audio_path:
                raise RuntimeError("Download completed without a playable audio file")
        except Exception as e:
            log.warning(f"Download failed for {tid}: {e}")
            _record_download_status(tid, False, str(e))
            return False
    # Audio availability is the critical path. Artwork is intentionally fetched
    # afterwards, and failure to cache art never discards playable local audio.
    art_path = SAVED_DIR / f"{tid}.jpg"
    if not art_path.exists():
        try:
            art_bytes, _working_url = _download_first_artwork(
                _artwork_urls_for_track(track, resolve_missing=True)
            )
            if art_bytes:
                tmp_art = art_path.with_suffix(".jpg.tmp")
                tmp_art.write_bytes(art_bytes)
                os.replace(tmp_art, art_path)
        except Exception as exc:
            log.debug("Offline artwork cache failed for %s: %s", tid, exc)
    _record_download_status(tid, True)
    return True


_favorite_downloads_pending = set()
_favorite_downloads_pending_lock = threading.Lock()


def _download_favorite_once(track: dict):
    tid = track.get('tid') or get_track_id(track.get('name', ''), track.get('artist', ''))
    try:
        return download_track(track)
    finally:
        with _favorite_downloads_pending_lock:
            _favorite_downloads_pending.discard(tid)


def _enqueue_favorite_download(track: dict) -> bool:
    tid = track.get('tid') or get_track_id(track.get('name', ''), track.get('artist', ''))
    if not tid or _resolve_local_audio(tid)[0]:
        return False
    with _favorite_downloads_pending_lock:
        if tid in _favorite_downloads_pending:
            return False
        _favorite_downloads_pending.add(tid)
    with _download_status_lock:
        _download_status[tid] = {"ok": None, "error": None, "pending": True}
    _invalidate_downloads_status_cache()
    try:
        _download_executor.submit(_download_favorite_once, dict(track, tid=tid))
    except Exception:
        with _favorite_downloads_pending_lock:
            _favorite_downloads_pending.discard(tid)
        raise
    return True

def get_stream_url(query: str, tid: str = "", vid: str = "", force: bool = False,
                   title: str = "", artist: str = "", duration=None,
                   album: str = "", album_id: str = "") -> dict:
    """Single-pass stream URL resolution.

    Flow:
    1. Check stream cache (tid + recording identity)
    2. Determine target videoId:
       - Explicit vid parameter
       - resolve_recording(title, artist, ...) for metadata-based lookup
       - Fallback: ytsearch1:query
    3. Try YTMusic direct player URL (fast path, skipped if force=True)
    4. ONE yt-dlp extraction for the determined videoId
    5. Cache and return
    """
    recording_identity = _recording_identity_signature(title, artist, duration, album, album_id) if title and artist else ""
    requested_identity = recording_identity

    # 1. Stream cache lookup
    if tid and not force:
        with _stream_cache_lock:
            if tid in _stream_cache:
                cached = _stream_cache[tid]
                entry = cached if isinstance(cached, dict) else {"url": cached}
                exp = entry.get("exp") or 0
                url = entry["url"]
                if (requested_identity and entry.get("identity") != requested_identity) or (exp and exp <= time.time()):
                    _stream_cache.pop(tid, None)
                else:
                    _stream_cache.move_to_end(tid)
                    result = {"url": url, "cached": True}
                    for key in ("matchedVideoId", "matchConfidence", "recordingResolved"):
                        if entry.get(key) is not None:
                            result[key] = entry[key]
                    return result
    elif tid and force:
        with _stream_cache_lock:
            _stream_cache.pop(tid, None)

    _YDL_HTTP_HEADERS = {"User-Agent": _YDL_USER_AGENT}

    def _extract_url_for_vid(target_vid: str) -> tuple[str | None, bool]:
        """Extract stream URL for a specific videoId. Returns (url, auth_fail_seen)."""
        source = f"https://www.youtube.com/watch?v={target_vid}"

        # Fast path: YTMusic direct player URL (try ALWAYS, even on force)
        # force=True only bypasses cache, not the extraction method order
        try:
            direct_url = _ytmusic_player_audio_url(target_vid)
        except NameError:
            direct_url = ""
        if direct_url:
            return direct_url, False

        # Single yt-dlp extraction - gets complete formats table in one pass
        fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
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
                        return None, False
                    info = entries[0]
                formats = info.get("formats", [])
                audio = [f for f in formats if f.get("vcodec") == "none" and f.get("url")]
                if not audio:
                    audio = [f for f in formats if f.get("url")]
                if not audio:
                    return info.get("url"), False
                best = max(audio, key=lambda f: ({"m4a": 3, "webm": 2}.get(f.get("ext", ""), 0), f.get("tbr") or 0))
                return best["url"], False
        except Exception as e:
            msg = str(e)
            auth_fail_seen = bool(_AUTH_FAIL_RE.search(msg))
            if auth_fail_seen:
                print(f"[AUTH_FAIL] vid={target_vid} err={msg[:120]}", flush=True)
            return None, auth_fail_seen

    # 2. Determine target videoId
    target_vid = vid
    match_result = {}

    if not target_vid and title and artist:
        # Use recording resolution to find the best matching videoId
        resolved = resolve_recording(title, artist, duration, album, album_id, exclude_vid="", force=force)
        if resolved and resolved.get("videoId"):
            target_vid = resolved["videoId"]
            match_result = {
                "matchedVideoId": target_vid,
                "matchConfidence": resolved.get("_match", {}).get("score") or resolved.get("confidence"),
                "recordingResolved": True,
            }
            # Cache the recording resolution for future use
            if recording_identity:
                _cache_recording_resolution(recording_identity, resolved, source="search")

    if not target_vid:
        # Fallback: search query (no metadata to resolve recording)
        target_vid = None  # ytsearch will be used in extraction

    # 3. Extract stream URL
    url = None
    auth_fail_seen = False

    if target_vid:
        url, auth_fail_seen = _extract_url_for_vid(target_vid)
        if not url and not force and title and artist:
            # One fallback: try a fresh recording resolution (in case cached was stale)
            print(f"[STREAM_URL] vid={target_vid} extraction failed, re-resolving recording", flush=True)
            _invalidate_recording_resolution(recording_identity)
            resolved = resolve_recording(title, artist, duration, album, album_id, exclude_vid=target_vid, force=True)
            if resolved and resolved.get("videoId") and resolved["videoId"] != target_vid:
                target_vid = resolved["videoId"]
                match_result = {
                    "matchedVideoId": target_vid,
                    "matchConfidence": resolved.get("_match", {}).get("score") or resolved.get("confidence"),
                    "recordingResolved": True,
                }
                if recording_identity:
                    _cache_recording_resolution(recording_identity, resolved, source="search_fallback")
                url, auth_fail_seen = _extract_url_for_vid(target_vid)
    else:
        # ytsearch fallback (no title/artist metadata)
        source = f"ytsearch1:{query}"
        fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
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
                    if entries:
                        info = entries[0]
                        target_vid = info.get("videoId", "")
                        if target_vid and recording_identity:
                            match_result = {
                                "matchedVideoId": target_vid,
                                "matchConfidence": 0,
                                "recordingResolved": True,
                            }
                formats = info.get("formats", [])
                audio = [f for f in formats if f.get("vcodec") == "none" and f.get("url")]
                if not audio:
                    audio = [f for f in formats if f.get("url")]
                if audio:
                    best = max(audio, key=lambda f: ({"m4a": 3, "webm": 2}.get(f.get("ext", ""), 0), f.get("tbr") or 0))
                    url = best["url"]
                else:
                    url = info.get("url")
        except Exception as e:
            msg = str(e)
            auth_fail_seen = bool(_AUTH_FAIL_RE.search(msg))
            if auth_fail_seen:
                print(f"[AUTH_FAIL] ytsearch err={msg[:120]}", flush=True)

    if url:
        if not auth_fail_seen:
            _cache_stream(tid, url, requested_identity, match_result)
        return {"url": url, **match_result}

    print(f"[STREAM_URL] ALL FAILED for: {query[:50]} (vid={target_vid})", flush=True)
    return {"error": "Stream not available"}


def resolve_stream_singleflight(query: str, tid: str = "", vid: str = "", force: bool = False,
                                title: str = "", artist: str = "", duration=None,
                                album: str = "", album_id: str = "") -> dict:
    """Share one expensive yt-dlp extraction between warming and playback.

    A click that arrives while its search-result warmer is still running waits
    on the same extraction instead of launching a second QuickJS/YouTube pass.
    Forced stale-URL recovery deliberately bypasses this coordination.
    """
    if force:
        return get_stream_url(
            query, tid, vid, force=True, title=title, artist=artist,
            duration=duration, album=album, album_id=album_id,
        )
    identity = (_recording_identity_signature(title, artist, duration, album, album_id)
                if title and artist else "")
    key = str((f"{tid}|{identity}" if tid and identity else tid or identity) or hashlib.sha256(
        f"{query}|{vid}".encode("utf-8")
    ).hexdigest()[:24])
    owner = False
    with _stream_resolution_lock:
        entry = _stream_resolution_inflight.get(key)
        if entry is None:
            entry = {"event": threading.Event(), "result": None}
            _stream_resolution_inflight[key] = entry
            owner = True
    if not owner:
        if entry["event"].wait(timeout=30):
            result = entry.get("result")
            if isinstance(result, dict):
                return dict(result)
        # The owner exceeded the normal API timeout or terminated unexpectedly;
        # preserve the established cold path as the final safety net.
        return get_stream_url(
            query, tid, vid, title=title, artist=artist,
            duration=duration, album=album, album_id=album_id,
        )
    try:
        result = get_stream_url(
            query, tid, vid, title=title, artist=artist,
            duration=duration, album=album, album_id=album_id,
        )
        entry["result"] = dict(result) if isinstance(result, dict) else result
        return result
    finally:
        with _stream_resolution_lock:
            if _stream_resolution_inflight.get(key) is entry:
                _stream_resolution_inflight.pop(key, None)
            entry["event"].set()

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


def _version_aware_yt_candidates(title: str, artist: str, duration=None,
                                 exclude_vid: str = "", limit: int = 4,
                                 album: str = "", album_id: str = "") -> list:
    """Return only identity-safe YouTube Music song candidates, best first."""
    target = {"title": title, "artist": artist, "duration": duration,
              "album": album, "albumId": album_id}
    query = " ".join(part for part in (artist, title) if part).strip()
    if not query:
        return []
    try:
        results = ytmusic.search(query, filter="songs", limit=20)
    except Exception as exc:
        log.debug(f"Version-aware YT search failed: {exc}")
        return []
    candidates = []
    for item in results or []:
        video_id = item.get("videoId", "")
        if not video_id or video_id == exclude_vid:
            continue
        artists = item.get("artists") or []
        thumbnails = item.get("thumbnails") or []
        art = thumbnails[-1].get("url", "") if thumbnails and isinstance(thumbnails[-1], dict) else ""
        candidates.append({
            "title": item.get("title", ""),
            "artist": ", ".join(a.get("name", "") for a in artists if a.get("name")),
            "album": (item.get("album") or {}).get("name", ""),
            "albumId": (item.get("album") or {}).get("id", ""),
            "duration": item.get("duration_seconds") or item.get("duration"),
            "videoId": video_id,
            "videoType": item.get("videoType", ""),
            "art": art,
        })
    return _rank_track_candidates(target, candidates, minimum=68)[:max(1, limit)]


def _load_recording_resolutions_locked() -> OrderedDict:
    global _recording_resolution_cache
    if _recording_resolution_cache is not None:
        return _recording_resolution_cache
    entries = OrderedDict()
    try:
        if RECORDING_RESOLUTIONS_JSON.exists():
            doc = json.loads(RECORDING_RESOLUTIONS_JSON.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("schema") == _RECORDING_RESOLUTION_SCHEMA:
                for key, value in (doc.get("entries") or {}).items():
                    if isinstance(value, dict) and value.get("videoId"):
                        entries[str(key)] = value
    except Exception as exc:
        log.warning(f"Recording resolution cache could not be loaded: {exc}")
    _recording_resolution_cache = entries
    return entries


def _save_recording_resolutions_locked() -> None:
    cache = _load_recording_resolutions_locked()
    while len(cache) > _RECORDING_RESOLUTION_MAX:
        cache.popitem(last=False)
    _atomic_write_json(RECORDING_RESOLUTIONS_JSON, {
        "schema": _RECORDING_RESOLUTION_SCHEMA,
        "entries": cache,
    })


def _cached_recording_resolution(identity: str, exclude_vid: str = "", target: dict | None = None) -> dict | None:
    if not identity:
        return None
    with _recording_resolution_lock:
        cache = _load_recording_resolutions_locked()
        entry = cache.get(identity)
        if not entry:
            return None
        age = time.time() - float(entry.get("resolvedAt") or 0)
        invalid_identity = bool(target) and entry.get("source") != "provided" and not _score_track_candidate(target, entry)["acceptable"]
        if age > _RECORDING_RESOLUTION_TTL or entry.get("videoId") == exclude_vid or invalid_identity:
            cache.pop(identity, None)
            try:
                _save_recording_resolutions_locked()
            except Exception as exc:
                log.debug("Invalid recording cache entry could not be removed: %s", exc)
            return None
        cache.move_to_end(identity)
        return dict(entry)


def _cache_recording_resolution(identity: str, candidate: dict, source: str = "search") -> None:
    video_id = str((candidate or {}).get("videoId") or "").strip()
    if not identity or not video_id:
        return
    match = (candidate or {}).get("_match") or {}
    entry = {
        "videoId": video_id,
        "title": str((candidate or {}).get("title") or "")[:500],
        "artist": str((candidate or {}).get("artist") or "")[:500],
        "album": str((candidate or {}).get("album") or "")[:500],
        "albumId": str((candidate or {}).get("albumId") or "")[:256],
        "duration": _coerce_duration_seconds((candidate or {}).get("duration")),
        "videoType": str((candidate or {}).get("videoType") or "")[:128],
        "art": str((candidate or {}).get("art") or "")[:4096],
        "confidence": float(match.get("score") or (100 if source == "provided" else 0)),
        "source": source,
        "resolvedAt": time.time(),
    }
    with _recording_resolution_lock:
        cache = _load_recording_resolutions_locked()
        existing = cache.get(identity) or {}
        if (existing.get("videoId") == video_id
                and time.time() - float(existing.get("resolvedAt") or 0) < 24 * 60 * 60):
            cache.move_to_end(identity)
            return
        cache[identity] = entry
        cache.move_to_end(identity)
        try:
            _save_recording_resolutions_locked()
        except Exception as exc:
            log.warning(f"Recording resolution cache could not be saved: {exc}")


def _invalidate_recording_resolution(identity: str) -> None:
    if not identity:
        return
    with _recording_resolution_lock:
        cache = _load_recording_resolutions_locked()
        if cache.pop(identity, None) is not None:
            try:
                _save_recording_resolutions_locked()
            except Exception as exc:
                log.warning(f"Recording resolution cache could not be updated: {exc}")


def resolve_recording(title: str, artist: str, duration=None, album: str = "",
                      album_id: str = "", exclude_vid: str = "", force: bool = False) -> dict | None:
    """Resolve metadata to one stable YouTube recording, deduplicating concurrent searches."""
    identity = _recording_identity_signature(title, artist, duration, album, album_id)
    target = {"title": title, "artist": artist, "duration": duration, "album": album, "albumId": album_id}
    if not force:
        cached = _cached_recording_resolution(identity, exclude_vid=exclude_vid, target=target)
        if cached:
            cached["cached"] = True
            cached["identity"] = identity
            return cached

    owner = False
    with _recording_resolution_lock:
        event = _recording_resolution_inflight.get(identity)
        if event is None:
            event = threading.Event()
            _recording_resolution_inflight[identity] = event
            owner = True
    if not owner:
        completed = event.wait(timeout=12)
        cached = _cached_recording_resolution(identity, exclude_vid=exclude_vid, target=target)
        if cached:
            cached["cached"] = True
            cached["identity"] = identity
            return cached
        if completed and force:
            return resolve_recording(
                title, artist, duration, album, album_id,
                exclude_vid=exclude_vid, force=True,
            )
        return None

    try:
        ranked = _version_aware_yt_candidates(
            title, artist, duration, exclude_vid=exclude_vid, limit=4,
            album=album, album_id=album_id,
        )
        if not ranked:
            return None
        best = ranked[0]
        _cache_recording_resolution(identity, best, source="search")
        result = dict(best)
        result["identity"] = identity
        return result
    finally:
        with _recording_resolution_lock:
            _recording_resolution_inflight.pop(identity, None)
            event.set()


def _prefetch_recording(track: dict) -> None:
    title = str(track.get("title") or track.get("name") or "").strip()
    artist = str(track.get("artist") or track.get("artist_name") or "").strip()
    if not title or not artist or track.get("videoId"):
        return
    identity = _recording_identity_signature(
        title, artist, track.get("duration") or track.get("dur"),
        track.get("album") or track.get("albumName") or "", track.get("albumId") or "",
    )
    if _cached_recording_resolution(identity):
        return
    with _recording_resolution_lock:
        if identity in _recording_prefetch_pending:
            return
        _recording_prefetch_pending.add(identity)

    def worker():
        try:
            resolve_recording(
                title, artist, track.get("duration") or track.get("dur"),
                track.get("album") or track.get("albumName") or "", track.get("albumId") or "",
            )
        finally:
            with _recording_resolution_lock:
                _recording_prefetch_pending.discard(identity)

    _resolution_executor.submit(worker)

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
    query_versions = _track_version_profile(cleaned)["tags"]
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
            candidate_versions = _track_version_profile(t["name"])["tags"]
            score += 8 if candidate_versions == query_versions else 0
            score -= 20 * len(query_versions - candidate_versions)
            score -= 25 * len(candidate_versions - query_versions)
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
                track = _build_track_dict(name, artist, art, dur, tid, vid, album.get("id") or "")
                query_tokens = _tokenize_query(query)
                haystack = set(_tokenize_query(f"{artist} {name}"))
                track["_score"] = len(set(query_tokens) & haystack) * 3
                query_versions = _track_version_profile(query)["tags"]
                candidate_versions = _track_version_profile(name)["tags"]
                track["_score"] += 8 if candidate_versions == query_versions else 0
                track["_score"] -= 20 * len(query_versions - candidate_versions)
                track["_score"] -= 25 * len(candidate_versions - query_versions)
                tracks.append(track)
            tracks.sort(key=lambda item: item.get("_score", 0), reverse=True)
            for track in tracks:
                track.pop("_score", None)
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


def _lyrics_content_score(candidate: dict, target_duration=None) -> tuple[float, str | None]:
    lines = candidate.get("lines") or []
    if not lines:
        return -100.0, "empty"
    if candidate.get("synced"):
        timed = [line for line in lines if isinstance(line, dict) and isinstance(line.get("time"), (int, float)) and str(line.get("text") or "").strip()]
        if len(timed) < 3:
            return -100.0, "too_few_timed_lines"
        duration = _coerce_duration_seconds(target_duration)
        last_time = max(line["time"] for line in timed)
        if duration and last_time > duration + max(12, duration * 0.08):
            return -100.0, "timestamps_exceed_track"
        score = 9.0
        if duration:
            coverage = last_time / max(1.0, duration)
            if coverage >= 0.72:
                score += 5
            elif coverage < 0.25:
                score -= 8
        return score, None
    text = "\n".join(str(line.get("text") or "") for line in lines if isinstance(line, dict)).strip()
    alpha_count = sum(char.isalpha() for char in text)
    if alpha_count < 24:
        return -100.0, "plain_lyrics_too_short"
    return 2.0, None


def _select_lyrics_candidate(candidates: list, target_duration=None, minimum: float = 68) -> dict | None:
    ranked = []
    for candidate in candidates or []:
        if not candidate:
            continue
        match = candidate.get("_match") or {}
        if not match.get("acceptable"):
            continue
        content_score, rejection = _lyrics_content_score(candidate, target_duration)
        if rejection:
            continue
        source_bonus = 4 if candidate.get("_source") == "lrclib" else 0
        total = float(match.get("score") or 0) + content_score + source_bonus
        if total >= minimum:
            selected = dict(candidate)
            selected["_selectionScore"] = round(total, 2)
            ranked.append(selected)
    ranked.sort(key=lambda item: item["_selectionScore"], reverse=True)
    return ranked[0] if ranked else None

def _write_lyrics_cache(tid: str, data: dict, neg_ttl: int = 0, identity: str = ""):
    """Write lyrics to in-memory LRU + filesystem cache.

    If `neg_ttl` > 0, the entry is treated as a negative-cache hit (empty/no-lyrics result)
    and stamped with an `exp` (epoch-seconds) after which it must be re-validated.
    Positive (real) lyrics carry no `exp` and remain valid until purged manually.
    """
    data = dict(data)
    data["cacheVersion"] = _LYRICS_CACHE_VERSION
    if identity:
        data["identity"] = identity
    if neg_ttl > 0:
        data["exp"] = int(time.time()) + neg_ttl
    with _lyrics_mem_lock:
        _lyrics_mem_cache[tid] = data
        if len(_lyrics_mem_cache) > _LYRICS_MEM_CACHE_MAX:
            _lyrics_mem_cache.popitem(last=False)
    try:
        cache_path = LYRICS_DIR / f"{tid}.json"
        _atomic_write_json(cache_path, data)
    except Exception as e:
        log.warning(f"Lyrics cache write failed: {e}")

def _read_lyrics_cache(tid: str, identity: str = "") -> dict | None:
    """Read lyrics from in-memory LRU first, then filesystem cache.

    Returns None on miss, or on negative-cache entries whose `exp` field has elapsed
    (in which case the in-memory + on-disk entries are evicted so subsequent calls re-fetch).
    """
    with _lyrics_mem_lock:
        if tid in _lyrics_mem_cache:
            data = _lyrics_mem_cache[tid]
            try:
                expired = bool(data.get("exp")) and int(time.time()) >= int(data["exp"])
            except (TypeError, ValueError):
                expired = False
            if not expired and data.get("cacheVersion") == _LYRICS_CACHE_VERSION and (not identity or data.get("identity") == identity):
                _lyrics_mem_cache.move_to_end(tid)
                return data
            _lyrics_mem_cache.pop(tid, None)
    try:
        cache_path = LYRICS_DIR / f"{tid}.json"
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("cacheVersion") != _LYRICS_CACHE_VERSION or (identity and data.get("identity") != identity):
                try: cache_path.unlink()
                except OSError: pass
                return None
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


def _get_playlist_lock(playlist_name: str):
    """Return the stable re-entrant lock for one sanitized playlist name."""
    with _playlist_locks_guard:
        lock = _playlist_locks.get(playlist_name)
        if lock is None:
            lock = threading.RLock()
            _playlist_locks[playlist_name] = lock
        return lock


@contextmanager
def _playlist_guard(playlist_name: str):
    """Serialize filesystem operations that mutate or validate one playlist."""
    lock = _get_playlist_lock(playlist_name)
    with lock:
        yield


def _quarantine_invalid_json(path: Path, reason: str):
    """Preserve malformed JSON beside the original so recovery is reversible."""
    if not path.exists():
        return
    quarantine = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
    try:
        os.replace(path, quarantine)
        log.warning(f"[playlists] quarantined {path.name}: {reason}")
    except OSError as e:
        log.warning(f"[playlists] could not quarantine {path.name}: {e}")


def _read_playlist_json(path: Path, label: str, *, quarantine=True) -> dict:
    """Read a playlist-owned JSON object, rejecting arrays/scalars and bad JSON."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as e:
        if quarantine:
            _quarantine_invalid_json(path, f"{label}: {e}")
        else:
            log.warning(f"[playlists] invalid {label} at {path}: {e}")
        return {}


def _coerce_playlist_duration(value) -> int:
    try:
        return max(0, min(int(float(value or 0)), 24 * 60 * 60))
    except (TypeError, ValueError, OverflowError):
        return 0


def _coerce_track_number(value):
    try:
        value = int(value)
        return value if 1 <= value <= 1_000_000 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_playlist_track(track: dict) -> dict | None:
    """Normalize imported/client track data to the backward-compatible schema."""
    if not isinstance(track, dict):
        return None
    name = str(track.get("name") or track.get("title") or "").strip()[:500]
    artist = str(track.get("artist") or track.get("artist_name") or "").strip()[:500]
    if not name or not artist:
        return None
    normalized = {
        "name": name,
        "artist": artist,
        "tid": get_track_id(name, artist),
        "dur": _coerce_playlist_duration(track.get("dur") or track.get("duration")),
        "art": str(track.get("art") or track.get("album_art") or "")[:4096],
        "videoId": str(track.get("videoId") or "")[:128],
    }
    art_candidates = track.get("art_candidates")
    if isinstance(art_candidates, list):
        normalized["art_candidates"] = [str(url)[:4096] for url in art_candidates[:6] if isinstance(url, str) and url]
    for key, limit in (("art_source", 64), ("spotify_image_url", 4096), ("isrc", 32)):
        value = str(track.get(key) or "").strip()[:limit]
        if value:
            normalized[key] = value
    try:
        if track.get("art_resolved_at"):
            normalized["art_resolved_at"] = float(track["art_resolved_at"])
        if track.get("art_confidence") is not None:
            normalized["art_confidence"] = float(track["art_confidence"])
    except (TypeError, ValueError):
        pass
    album_id = str(track.get("albumId") or "")[:256]
    if album_id:
        normalized["albumId"] = album_id
    album_name = str(track.get("album") or track.get("albumName") or "").strip()[:500]
    if album_name:
        normalized["album"] = album_name
        normalized["albumName"] = album_name
    number = _coerce_track_number(track.get("trackNumber"))
    if number is not None:
        normalized["trackNumber"] = number
    return normalized


def _playlist_meta_payload(track: dict, track_number=None, added_at=None) -> dict:
    number = _coerce_track_number(track_number)
    return {
        "name": track.get("name", track.get("tid", "Unknown")),
        "artist": track.get("artist", "Unknown Artist"),
        "dur": _coerce_playlist_duration(track.get("dur")),
        "art": track.get("art", ""),
        "trackNumber": number,
        "videoId": track.get("videoId", ""),
        "albumId": track.get("albumId", ""),
        "album": track.get("album") or track.get("albumName") or "",
        "albumName": track.get("albumName") or track.get("album") or "",
        "art_candidates": track.get("art_candidates") or [],
        "art_source": track.get("art_source", ""),
        "spotify_image_url": track.get("spotify_image_url", ""),
        "isrc": track.get("isrc", ""),
        "art_resolved_at": track.get("art_resolved_at", 0),
        "art_confidence": track.get("art_confidence", 0),
        "addedAt": float(added_at or time.time()),
        "downloadState": track.get("downloadState", "pending"),
        "downloadError": str(track.get("downloadError") or "")[:500],
    }


def _read_track_meta(path: Path, tid: str, *, quarantine=True) -> dict:
    raw = _read_playlist_json(path, f"track metadata for {tid}", quarantine=quarantine)
    if not raw:
        return {}
    normalized = _normalize_playlist_track(raw)
    if not normalized:
        if quarantine:
            _quarantine_invalid_json(path, "track metadata has no name or artist")
        return {}
    # The filename is authoritative for existing local media. New writes and
    # imports still use the canonical id generated by _normalize_playlist_track.
    normalized["tid"] = tid
    normalized["trackNumber"] = _coerce_track_number(raw.get("trackNumber"))
    state = str(raw.get("downloadState") or "pending")
    normalized["downloadState"] = state if state in {"remote", "pending", "ready"} else "remote"
    normalized["downloadError"] = str(raw.get("downloadError") or "")[:500]
    try:
        normalized["addedAt"] = float(raw.get("addedAt") or path.stat().st_mtime)
    except (OSError, TypeError, ValueError):
        normalized["addedAt"] = 0.0
    return normalized


def _merge_track_meta_extras(path: Path, normalized: dict) -> dict:
    """Preserve optional provider fields while enforcing normalized core data."""
    raw = _read_playlist_json(path, f"track metadata for {path.name}")
    if not raw:
        return normalized
    raw.update(normalized)
    return raw


def _tid_from_audio_filename(path: Path) -> str:
    stem = path.stem
    if " - " in stem:
        prefix, candidate = stem.split(" - ", 1)
        if prefix.isdigit():
            return candidate
    return stem


def _playlist_existing_tids_locked(pl_dir: Path) -> set:
    tids = set()
    if not pl_dir.exists():
        return tids
    for path in pl_dir.iterdir():
        if path.name.endswith(".meta.json"):
            tid = path.name[:-len(".meta.json")]
            if _read_track_meta(path, tid):
                tids.add(tid)
        elif path.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus"}:
            tids.add(_tid_from_audio_filename(path))
    return tids


def _next_playlist_track_number_locked(pl_dir: Path) -> int:
    numbers = _playlist_track_numbers_locked(pl_dir)
    return (max(numbers) if numbers else 0) + 1


def _playlist_track_numbers_locked(pl_dir: Path) -> set:
    numbers = set()
    if pl_dir.exists():
        for path in pl_dir.glob("*.meta.json"):
            tid = path.name[:-len(".meta.json")]
            meta = _read_track_meta(path, tid)
            number = meta.get("trackNumber")
            if number:
                numbers.add(number)
    return numbers


def _unique_playlist_name_locked(base_name: str) -> str:
    candidate = _safe_playlist_name(base_name)
    if not (PLAYLISTS_DIR / candidate).exists():
        return candidate
    for suffix in range(2, 10_000):
        candidate = _safe_playlist_name(f"{base_name} ({suffix})")
        if not (PLAYLISTS_DIR / candidate).exists():
            return candidate
    raise RuntimeError("Could not allocate a unique playlist name")

def save_album_metadata(playlist_name: str, album_data: dict):
    """Save album metadata to a playlist directory when saving an album."""
    try:
        with _playlist_catalog_lock:
            with _playlist_guard(playlist_name):
                pl_dir = PLAYLISTS_DIR / playlist_name
                pl_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write_json(pl_dir / "album.json", {
                    "albumId": album_data.get("albumId", ""),
                    "title": album_data.get("title", playlist_name),
                    "artist": album_data.get("artist", ""),
                    "art": album_data.get("art", ""),
                    "trackCount": len(album_data.get("tracks", [])),
                })
        _invalidate_file_index()
    except Exception as e:
        log.warning(f"Failed to save album metadata: {e}")

def _write_playlist_meta_locked(track: dict, playlist_name: str, track_number=None) -> tuple[str, bool]:
    """Write one metadata file while the caller holds the playlist lock."""
    pl_dir = PLAYLISTS_DIR / playlist_name
    pl_dir.mkdir(parents=True, exist_ok=True)
    tid = track.get("tid") or get_track_id(track.get("name", ""), track.get("artist", ""))
    if not tid:
        return "", False
    if tid in _playlist_existing_tids_locked(pl_dir):
        return tid, False
    requested_number = _coerce_track_number(track_number)
    used_numbers = _playlist_track_numbers_locked(pl_dir)
    number = requested_number if requested_number and requested_number not in used_numbers else ((max(used_numbers) if used_numbers else 0) + 1)
    _atomic_write_json(pl_dir / f"{tid}.meta.json", _playlist_meta_payload(track, number))
    return tid, True


def _write_playlist_meta(track: dict, playlist_name: str, track_number=None) -> tuple[str, bool]:
    """Synchronously write a track's .meta.json into a playlist dir so the
    playlist is visible (as a pending entry) before the audio download finishes.
    Returns ``(tid, created)``. Network work must happen after this returns."""
    with _playlist_guard(playlist_name):
        result = _write_playlist_meta_locked(track, playlist_name, track_number)
    if result[1]:
        _invalidate_file_index()
    return result


def _set_playlist_download_state(playlist_name: str, tid: str, state: str, error: str = "", video_id: str = ""):
    meta_path = PLAYLISTS_DIR / playlist_name / f"{tid}.meta.json"
    with _playlist_guard(playlist_name):
        current = _read_playlist_json(meta_path, f"track metadata for {tid}") or {}
        if not current:
            return
        current["downloadState"] = state
        current["downloadError"] = str(error or "")[:500]
        if video_id:
            current["videoId"] = video_id
        _atomic_write_json(meta_path, current)


def _cache_playlist_track_art(track: dict, playlist_name: str) -> bool:
    tid = track.get("tid") or get_track_id(track.get("name", ""), track.get("artist", ""))
    pl_dir = PLAYLISTS_DIR / playlist_name
    art_path = pl_dir / f"{tid}.jpg"
    try:
        if art_path.exists() and art_path.stat().st_size > 100:
            return True
    except OSError:
        pass
    try:
        art_bytes, _working_url = _download_first_artwork(
            _artwork_urls_for_track(track, resolve_missing=True)
        )
        if not art_bytes:
            return False
        with _playlist_guard(playlist_name):
            if not pl_dir.exists():
                return False
            try:
                if art_path.exists() and art_path.stat().st_size > 100:
                    return True
            except OSError:
                pass
            tmp_art = art_path.with_suffix(".jpg.tmp")
            tmp_art.write_bytes(art_bytes)
            os.replace(tmp_art, art_path)
        _invalidate_file_index()
        return True
    except Exception as exc:
        log.debug("Playlist artwork cache failed for %s: %s", tid, exc)
        return False


def download_to_playlist(track: dict, playlist_name: str, track_number=None, *, cache_art=True) -> bool:
    pl_dir = PLAYLISTS_DIR / playlist_name
    tid = track.get("tid") or get_track_id(track["name"], track["artist"])
    meta_path = pl_dir / f"{tid}.meta.json"
    ext_candidates = ["mp3", "m4a", "webm", "opus"]

    if track_number is not None:
        prefix = f"{str(track_number).zfill(2)} - "
    else:
        prefix = ""

    already_local = False
    with _playlist_guard(playlist_name):
        if not pl_dir.exists() or not meta_path.exists():
            _record_download_status(tid, False, "Playlist was removed before download started")
            return False
        for ext in ext_candidates:
            if (pl_dir / f"{prefix}{tid}.{ext}").exists():
                already_local = True
                break
    if already_local:
        _set_playlist_download_state(playlist_name, tid, "ready")
        if cache_art:
            _cache_playlist_track_art(track, playlist_name)
        return True
    _set_playlist_download_state(playlist_name, tid, "pending")
    if track_number is not None:
        out_name = f"{str(track_number).zfill(2)} - {tid}"
    else:
        out_name = tid

    # Reuse any verified on-device copy before touching the network. This is
    # especially important when the same liked song appears in several imported
    # playlists or albums: one successful transfer should satisfy all of them.
    shared_audio, _shared_tier = _resolve_local_audio(tid)
    if shared_audio:
        try:
            destination = pl_dir / f"{out_name}{shared_audio.suffix.lower()}"
            with _playlist_guard(playlist_name):
                if not pl_dir.exists() or not meta_path.exists():
                    raise FileNotFoundError("Playlist was removed before local media could be reused")
                if shared_audio.resolve() != destination.resolve():
                    shutil.copy2(shared_audio, destination)
            _set_playlist_download_state(playlist_name, tid, "ready", video_id=track.get("videoId", ""))
            if cache_art:
                _cache_playlist_track_art(track, playlist_name)
            _record_download_status(tid, True)
            _invalidate_file_index()
            _invalidate_playlist_cache()
            return True
        except Exception as exc:
            log.debug("Could not reuse local audio for %s: %s", tid, exc)

    try:
        stage_dir = Path(tempfile.mkdtemp(prefix=f"{tid[:12]}-", dir=PLAYLIST_STAGING_DIR))
    except OSError as e:
        log.warning(f"Could not create playlist download staging directory for {tid}: {e}")
        _record_download_status(tid, False, str(e))
        _set_playlist_download_state(playlist_name, tid, "remote", str(e))
        _invalidate_playlist_cache()
        return False

    def progress_hook(state):
        if state.get("status") == "downloading":
            print(
                f"\r[playlist] {track.get('name', tid)} — {state.get('_percent_str', '…')} "
                f"({state.get('_speed_str', '')})", end="", flush=True,
            )
        else:
            print(f"[playlist] {track.get('name', tid)} — processing…", flush=True)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(stage_dir / out_name),
        **_YDL_DOWNLOAD_OPTS,
        "noprogress": True,   # silence yt-dlp's own [download] bar; our hook below prints progress
        "http_headers": {"User-Agent": _YDL_USER_AGENT},
        "no_color": True,
        **_ydl_extras(),
        "progress_hooks": [progress_hook],
    }
    ydl_opts["postprocessors"] = [{
        "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192",
    }]
    try:
        if IS_ANDROID:
            completed, resolved_vid = _download_android_resolved_audio(
                dict(track, tid=tid), stage_dir, out_name,
            )
        else:
            source, resolved_vid = _download_source_for_track(track)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([source])
            completed = next(
                (f for f in stage_dir.glob(f"{out_name}*") if f.suffix in {".mp3", ".m4a", ".webm", ".opus"}),
                None,
            )
        if resolved_vid and not track.get("videoId"):
            track["videoId"] = resolved_vid
        print(f"[playlist] {track.get('name', tid)} — done", flush=True)
        with _playlist_guard(playlist_name):
            if not pl_dir.exists() or not meta_path.exists():
                raise FileNotFoundError(f"Playlist was removed while downloading: {playlist_name}")
            if completed is None:
                raise FileNotFoundError("Downloader completed without an audio file")
            destination_suffix = completed.suffix if IS_ANDROID else ".mp3"
            os.replace(completed, pl_dir / f"{out_name}{destination_suffix}")
        _set_playlist_download_state(playlist_name, tid, "ready", video_id=resolved_vid)
        if cache_art:
            _cache_playlist_track_art(track, playlist_name)
        _record_download_status(tid, True)
        _invalidate_file_index()
        _invalidate_playlist_cache()
        return True
    except Exception as e:
        log.warning(f"Playlist download failed for {tid}: {e}")
        _record_download_status(tid, False, str(e))
        # Preserve playlist metadata so a failed item stays visible and can be
        # retried by Offline Play instead of disappearing from the collection.
        _set_playlist_download_state(playlist_name, tid, "remote", str(e))
        _invalidate_file_index()
        _invalidate_playlist_cache()
        return False
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

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
    
    # Isolate mobile traffic cleanly without breaking the desktop engine.
    # The legacy mobile template remains available at /mobile/legacy while the
    # first-class album-centered player is developed independently.
    if any(word in user_agent for word in mobile_words):
        return render_template("mobile_player.html")
         
    return render_template("player.html")


@app.route("/mobile/legacy")
def mobile_player_legacy():
    return render_template("mobile.html")

@app.route("/api/stream")
def api_stream():
    q = request.args.get("q", "")
    tid = request.args.get("tid", "")
    vid = request.args.get("vid", "")
    title = request.args.get("title", "")
    artist = request.args.get("artist", "")
    duration = request.args.get("duration", "")
    album = request.args.get("album", "")
    album_id = request.args.get("album_id", "") or request.args.get("albumId", "")
    force = request.args.get("force", "") in ("1", "true", "yes")
    local_only = request.args.get("local_only", "") in ("1", "true", "yes")
    skip_local = request.args.get("skip_local", "") in ("1", "true", "yes")
    if q.startswith("http") and "youtube.com" not in q and "youtu.be" not in q:
        return jsonify({"error": "Only YouTube URLs are supported"}), 400
    _c = request.args.get("_c", "")
    print(f"[STREAM] q={q[:60]} tid={tid[:12]} vid={vid[:12]} caller={_c[:60]}", flush=True)
    _burst_check("stream", tid or q[:40])
    if tid and not skip_local:
        local = _local_media_payload(tid)
        if local.get("local_audio"):
            return jsonify({
                "url": local["url"],
                "local": True,
                "offlineReady": True,
                "source": local.get("audio_source"),
                "format": local.get("format"),
            })
    if local_only:
        return jsonify({
            "error": "Track is not available offline",
            "offline": True,
            "local": False,
        }), 503
    try:
        result = _resolve_stream_url(
            q, tid, vid, force=force, title=title, artist=artist,
            duration=duration, album=album, album_id=album_id,
        )
    except Exception as e:
        print(f"[STREAM] EXCEPTION: {e}", flush=True)
        result = {"error": str(e)}
    if "error" in result:
        print(f"[STREAM] ERROR: {result['error']}", flush=True)
        return jsonify(result), 502
    # Cache and return proxy URL
    if result.get("url") and not result.get("local"):
        stream_identity = _recording_identity_signature(title, artist, duration, album, album_id) if title and artist else ""
        _cache_stream(tid, result["url"], stream_identity, result)
    result["url"] = f"/api/proxy_stream?url_key={tid}"
    if not _c.startswith("mobile-player"):
        result["streamUrl"] = result["url"]
    if result.get("url") and not result.get("local"):
        result["transport"] = "proxy"
    return jsonify(result)


def _resolve_stream_url(q, tid, vid, force, title, artist, duration, album, album_id):
    """Resolve a playable stream URL for the given track.
    
    Single-pass extraction:
    1. Check stream cache (respecting force flag)
    2. Resolve recording identity
    3. ONE yt-dlp extraction with proper cookies + JS runtime
    4. Cache and return raw googlevideo URL
    """
    recording_identity = _recording_identity_signature(title, artist, duration, album, album_id) if title and artist else ""
    
    # 1. Stream cache lookup
    if tid and not force:
        with _stream_cache_lock:
            if tid in _stream_cache:
                cached = _stream_cache[tid]
                entry = cached if isinstance(cached, dict) else {"url": cached}
                exp = entry.get("exp") or 0
                url = entry["url"]
                if (recording_identity and entry.get("identity") != recording_identity) or (exp and exp <= time.time()):
                    _stream_cache.pop(tid, None)
                else:
                    _stream_cache.move_to_end(tid)
                    result = {"url": url, "cached": True}
                    for key in ("matchedVideoId", "matchConfidence", "recordingResolved"):
                        if entry.get(key) is not None:
                            result[key] = entry[key]
                    return result
    elif tid and force:
        with _stream_cache_lock:
            _stream_cache.pop(tid, None)
    
    # 2. Determine target videoId
    target_vid = vid
    match_result = {}
    
    if not target_vid and title and artist:
        resolved = resolve_recording(title, artist, duration, album, album_id, exclude_vid="", force=force)
        if resolved and resolved.get("videoId"):
            target_vid = resolved["videoId"]
            match_result = {
                "matchedVideoId": target_vid,
                "matchConfidence": resolved.get("_match", {}).get("score") or resolved.get("confidence"),
                "recordingResolved": True,
            }
            if recording_identity:
                _cache_recording_resolution(recording_identity, resolved, source="search")
    
    if not target_vid:
        # Fallback: ytsearch query
        search_query = q if q else f"{artist} {title}" if artist and title else ""
        if not search_query:
            return {"error": "No search query available"}
        target_vid = f"ytsearch1:{search_query}"
    
    # 3. Single yt-dlp extraction with full options
    url, auth_fail_seen = _extract_stream_url(target_vid)
    if not url:
        print(f"[STREAM_URL] ALL FAILED for: {q[:50]} (vid={target_vid})", flush=True)
        return {"error": "Stream not available"}
    
    if not auth_fail_seen:
        _cache_stream(tid, url, recording_identity, match_result)
    return {"url": url, **match_result}


def _extract_stream_url(target_vid):
    """Extract a playable stream URL using yt-dlp with full options.
    
    Returns (url, auth_fail_seen) or (None, False) on failure.
    """
    source = f"https://www.youtube.com/watch?v={target_vid}" if not target_vid.startswith("ytsearch") else target_vid
    
    fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
    ydl_opts = {
        "format": fmt,
        **_YDL_EXTRACT_OPTS,
        "http_headers": {"User-Agent": _YDL_USER_AGENT},
        **_ydl_extras(),
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source, download=False)
            if "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    return None, False
                info = entries[0]
            formats = info.get("formats", [])
            # Strict audio format filtering: must have audio codec, no video codec, audio MIME type
            audio = []
            for f in formats:
                vcodec = f.get("vcodec") or ""
                acodec = f.get("acodec") or ""
                mime = (f.get("mimeType") or "").lower()
                ext = (f.get("ext") or "").lower()
                format_id = (f.get("format_id") or "").lower()
                url = f.get("url") or ""
                if not url:
                    continue
                # Must have audio codec, no video codec, and be audio MIME or known audio ext
                if acodec == "none" or vcodec != "none":
                    continue
                if not (mime.startswith("audio/") or ext in ("m4a", "webm", "mp3", "opus", "ogg", "aac")):
                    continue
                # Skip thumbnails/storyboards
                if any(x in format_id for x in ("thumbnail", "storyboard", "still", "image")):
                    continue
                audio.append(f)
            if not audio:
                # Fallback: any format with audio codec and no video codec
                audio = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url")]
            if not audio:
                return info.get("url"), False
            best = max(audio, key=lambda f: ({"m4a": 3, "webm": 2}.get(f.get("ext", ""), 0), f.get("tbr") or 0))
            return best["url"], False
    except Exception as e:
        msg = str(e)
        auth_fail_seen = bool(_AUTH_FAIL_RE.search(msg))
        if auth_fail_seen:
            print(f"[AUTH_FAIL] vid={target_vid} err={msg[:120]}", flush=True)
        return None, auth_fail_seen


def _stream_warm_is_current(scope: str, generation: str) -> bool:
    with _stream_warm_lock:
        return _stream_warm_latest.get(scope) == generation


def _probe_stream_url(url: str) -> bool:
    """Read one small range to warm DNS/TLS/CDN state without buffering audio.
    Uses the same headers as the proxy's INITIAL request (no Range) to accurately
    detect expired/blocked URLs before returning them to the frontend."""
    if not url or not url.startswith(("https://", "http://")):
        return False
    # Match the proxy's initial request: no Range header on first request
    # (browser doesn't send Range initially; proxy only adds if client sends it)
    headers = {
        "User-Agent": _YDL_USER_AGENT,
        "Referer": "https://www.youtube.com/",
        "Origin": "https://www.youtube.com",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    # Add YouTube/Google cookies to avoid 403 bot checks on googlevideo
    cookie_hdr = ""
    try:
        cookie_path = _resolve_cookie_file()
        if cookie_path:
            try:
                txt = cookie_path.read_text(encoding="utf-8", errors="ignore")
                cookie_hdr = _parse_netscape_cookies(txt, youtube_only=False)
                if cookie_hdr:
                    headers["Cookie"] = cookie_hdr
            except Exception:
                pass
    except Exception:
        pass
    response = None
    try:
        session = _stream_curl if _stream_curl is not None else _fallback_proxy_session
        response = session.get(url, headers=headers, timeout=8, stream=True)
        if response.status_code not in (200, 206):
            return False
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type and not (
            content_type.startswith(("audio/", "video/")) or
            content_type in ("application/octet-stream", "binary/octet-stream")
        ):
            return False
        iterator = response.iter_content(chunk_size=32768)
        chunk = next(iterator, b"")
        return bool(chunk)
    except Exception as exc:
        log.debug("Stream warm probe failed: %s", exc)
        return False
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _queue_stream_warm(track: dict, scope: str, generation: str, immediate: bool = False) -> bool:
    tid = str(track.get("tid") or get_track_id(track.get("name", ""), track.get("artist", "")))
    pending_key = (scope, generation, tid)
    with _stream_warm_lock:
        if pending_key in _stream_warm_pending:
            return False
        _stream_warm_pending.add(pending_key)

    def worker():
        started = time.perf_counter()
        try:
            # A brief intent window lets a hover/click supersede the automatic
            # top-result warm before expensive QuickJS extraction begins.
            # Skip for explicit user clicks (immediate=true).
            if not immediate:
                time.sleep(0.2)
            if not _stream_warm_is_current(scope, generation):
                return
            if _local_media_payload(tid).get("local_audio"):
                return
            name = track.get("name") or track.get("title") or ""
            artist = track.get("artist") or track.get("artist_name") or ""
            result = resolve_stream_singleflight(
                f"{artist} {name} audio".strip(), tid, track.get("videoId") or "",
                title=name, artist=artist, duration=track.get("dur") or track.get("duration"),
                album=track.get("album") or track.get("albumName") or "",
                album_id=track.get("albumId") or "",
            )
            if not _stream_warm_is_current(scope, generation):
                return
            if isinstance(result, dict) and result.get("url"):
                _probe_stream_url(result["url"])
                log.debug("Stream warmed in %.0fms: %s — %s",
                          (time.perf_counter() - started) * 1000, artist, name)
        finally:
            with _stream_warm_lock:
                _stream_warm_pending.discard(pending_key)

    _stream_warm_executor.submit(worker)
    return True


@app.route("/api/stream/prefetch", methods=["POST"])
def api_stream_prefetch():
    """Warm recording IDs for queues or full streams for likely search clicks."""
    data = request.get_json(silent=True) or {}
    tracks = data.get("tracks") if isinstance(data, dict) else None
    if not isinstance(tracks, list):
        return jsonify({"error": "tracks must be a list"}), 400
    mode = "stream" if data.get("mode") == "stream" else "recording"
    scope = re.sub(r"[^a-zA-Z0-9_.:-]", "", str(data.get("scope") or mode))[:80] or mode
    generation = str(data.get("generation") or time.time_ns())[:80]
    immediate = bool(data.get("immediate"))
    with _stream_warm_lock:
        if scope not in _stream_warm_latest and len(_stream_warm_latest) >= 100:
            _stream_warm_latest.pop(next(iter(_stream_warm_latest)), None)
        _stream_warm_latest[scope] = generation
    accepted = 0
    limit = 2 if mode == "stream" else 4
    for raw in tracks[:limit]:
        if not isinstance(raw, dict):
            continue
        track = {
            "name": str(raw.get("name") or raw.get("title") or "")[:500],
            "artist": str(raw.get("artist") or raw.get("artist_name") or "")[:500],
            "dur": raw.get("dur") or raw.get("duration") or "",
            "album": str(raw.get("album") or raw.get("albumName") or "")[:500],
            "albumId": str(raw.get("albumId") or "")[:256],
            "videoId": str(raw.get("videoId") or "")[:128],
            "tid": str(raw.get("tid") or "")[:128],
        }
        if not track["name"] or not track["artist"]:
            continue
        if mode == "stream":
            accepted += int(_queue_stream_warm(track, scope, generation, immediate))
        elif not track["videoId"]:
            _prefetch_recording(track)
            accepted += 1
    return jsonify({"success": True, "queued": accepted, "mode": mode}), 202


@app.route("/api/media/status", methods=["POST"])
def api_media_status():
    """Batch-reconcile persisted client flags with the files currently on disk."""
    data = request.get_json(silent=True) or {}
    tids = data.get("tids") if isinstance(data, dict) else None
    if not isinstance(tids, list):
        return jsonify({"error": "tids must be a list"}), 400
    clean_tids = []
    seen = set()
    for raw in tids[:1000]:
        tid = str(raw or "").strip()
        if tid and tid not in seen and _SAFE_FILENAME_RE.fullmatch(tid):
            seen.add(tid)
            clean_tids.append(tid)
    index = _get_file_index()
    return jsonify({
        "tracks": {tid: _local_media_payload(tid, index=index) for tid in clean_tids},
        "count": len(clean_tids),
    })

@app.route("/api/proxy_stream")
def api_proxy_stream():
    url_key = request.args.get("url_key", "")
    print(f"[PROXY-DEBUG] request url_key={url_key[:12]}... cache_exists={url_key in _stream_cache}", flush=True)
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
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        }
        # Add YouTube/Google cookies to avoid 403 bot checks on googlevideo
        try:
            cookie_path = _resolve_cookie_file()
            if cookie_path:
                try:
                    txt = cookie_path.read_text(encoding="utf-8", errors="ignore")
                    # Use YouTube/Google cookies for googlevideo requests
                    cookie_hdr = _parse_netscape_cookies(txt, youtube_only=False)
                    if cookie_hdr:
                        req_headers["Cookie"] = cookie_hdr
                except Exception:
                    pass
        except Exception:
            pass
        range_header = request.headers.get("Range")
        if range_header:
            req_headers["Range"] = range_header
        if _stream_curl:
            upstream = _stream_curl.get(url, headers=req_headers, timeout=15, stream=True)
        else:
            # Reuse _fallback_proxy_session across calls to amortize TCP/TLS handshake.
            # Danger signal: requests.get returned `requests.adapters.HTTPAdapter`-cached
            # conns don't survive hard 5xx; the next call will reconnect — acceptable.
            upstream = _fallback_proxy_session.get(url, headers=req_headers, stream=True, timeout=15)
        print(f"[PROXY-DEBUG] upstream_status={upstream.status_code} content_type={upstream.headers.get('Content-Type')}", flush=True)
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
        # MediaSource on Android Chromium is noticeably quicker to begin a
        # ranged response when the upstream byte boundaries survive the local
        # proxy. Content-Length is safe to retain when Requests/curl_cffi has
        # not decoded a compressed entity; YouTube media responses are
        # normally identity encoded. Keep stripping hop-by-hop headers.
        excluded = {"content-encoding", "transfer-encoding", "connection"}
        if upstream.headers.get("Content-Encoding"):
            excluded.add("content-length")
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        def generate():
            try:
                # A smaller first yield lowers time-to-first-audio in Android
                # WebView while remaining large enough to avoid Python-heavy
                # per-chunk overhead on sustained playback.
                for chunk in upstream.iter_content(chunk_size=32768):
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
    # Batch-reconcile against both SAVED and playlist storage. A track that is
    # downloaded inside a playlist is just as offline-ready as a SAVED copy.
    audio_tids = {f.get('tid') or get_track_id(f.get('name', ''), f.get('artist', '')) for f in favs}
    index = _get_file_index()
    statuses = {tid: _local_media_payload(tid, index=index) for tid in audio_tids}
    for f in favs:
        tid = f.get('tid') or get_track_id(f.get('name', ''), f.get('artist', ''))
        f['tid'] = tid
        f['local_audio'] = statuses[tid]['local_audio']
        f['local_art'] = statuses[tid]['local_art']
    return jsonify(favs)

@app.route("/api/save_favorites", methods=["POST"])
def api_save_favorites():
    try:
        favs = request.get_json(force=True)
        _atomic_write_json(FAVORITES_JSON, favs)
        _invalidate_favorites_cache()
        # Only enqueue favourites that are genuinely missing material locally.
        # The previous version submitted the ENTIRE favourites list to the bounded
        # download executor on every single like/unlike toggle — N yt-dlp task
        # submissions per click even when every track was already downloaded.
        # download_track() early-exits when the audio already exists, but it still
        # paid art_path.exists() + executor scheduling for the whole collection
        # each toggle. Now we filter to the (usually empty) set that actually
        # needs work, so a toggle of an already-downloaded track is ~free.
        queued = []
        for f in favs:
            tid = f.get('tid') or get_track_id(f.get('name', ''), f.get('artist', ''))
            f['tid'] = tid
            if _enqueue_favorite_download(f):
                queued.append(tid)
        return jsonify({"success": True, "queued": queued})
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

    LQIP (Low Quality Image Placeholder) support:
      Add `?lqip=1` to get a tiny (10px) blurred JPEG (~200-500 bytes) suitable
      for inline base64 placeholders. Optional params: `w` (width, default 10),
      `q` (quality 1-100, default 10), `blur` (gaussian radius, default 20).
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

    # LQIP parameters
    lqip = request.args.get("lqip", "") in ("1", "true", "yes")
    lqip_w = max(1, min(int(request.args.get("w", "10")), 50))
    lqip_q = max(1, min(int(request.args.get("q", "10")), 100))
    lqip_blur = max(0, min(int(request.args.get("blur", "20")), 50))

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

        # LQIP processing: generate tiny blurred placeholder
        if lqip:
            try:
                from PIL import Image, ImageFilter
                img = Image.open(io.BytesIO(raw))
                img = img.convert("RGB")
                # Downscale to tiny size
                img.thumbnail((lqip_w, lqip_w), Image.Resampling.LANCZOS)
                # Apply gaussian blur
                if lqip_blur > 0:
                    img = img.filter(ImageFilter.GaussianBlur(radius=lqip_blur))
                # Encode to JPEG with low quality
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=lqip_q, optimize=True)
                raw = buf.getvalue()
                ctype = "image/jpeg"
            except Exception as e:
                log.warning(f"LQIP generation failed for {u!r}: {e}")
                # Fall through to return original image

        resp = app.response_class(raw, mimetype=ctype)
        # 7-day browser cache. Combined with the proxy URL including the
        # upstream URL + size param, the LLVM-style URL identity gives perfect
        # cache hit semantics — same source URL always returns the same bytes
        # until the upstream changes (and `?w=` resize URLs are distinct).
        # For LQIP, use shorter cache (1 day) since it's a derivative.
        cache_age = 86400 if lqip else 604800
        resp.headers["Cache-Control"] = f"public, max-age={cache_age}, immutable"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    except Exception as e:
        log.warning(f"img-proxy failed for {u!r}: {e}")
        return ("", 404)


@app.route("/api/artwork/resolve", methods=["POST"])
def api_artwork_resolve():
    """Resolve ranked artwork alternatives for any Track Schema source."""
    data = request.get_json(silent=True) or {}
    tracks = data.get("tracks") if isinstance(data, dict) else None
    if not isinstance(tracks, list):
        return jsonify({"error": "tracks must be a list"}), 400
    safe_tracks = []
    for raw in tracks[:25]:
        if not isinstance(raw, dict):
            continue
        safe_tracks.append({
            "name": str(raw.get("name") or raw.get("title") or "")[:500],
            "artist": str(raw.get("artist") or raw.get("artist_name") or "")[:500],
            "album": str(raw.get("album") or raw.get("albumName") or "")[:500],
            "dur": raw.get("dur") or raw.get("duration") or "",
            "videoId": str(raw.get("videoId") or "")[:128],
            "art": str(raw.get("art") or "")[:4096],
            "album_art": str(raw.get("album_art") or "")[:4096],
            "art_candidates": raw.get("art_candidates") if isinstance(raw.get("art_candidates"), list) else [],
            "art_source": str(raw.get("art_source") or "")[:64],
            "spotify_image_url": str(raw.get("spotify_image_url") or "")[:4096],
            "isrc": str(raw.get("isrc") or "")[:32],
        })
    force = bool(data.get("force")) if isinstance(data, dict) else False
    return jsonify({"tracks": resolve_artwork_batch(safe_tracks, force=force)})


@app.route("/api/artwork/upload", methods=["POST"])
def api_artwork_upload():
    """Upload custom artwork for a track. Expects multipart/form-data with:
    - file: image file (will be cropped to 1:1 square)
    - tid: track ID (used as filename)
    Returns the local artwork URL."""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    tid = request.form.get('tid', '').strip()
    if not tid or not _SAFE_FILENAME_RE.fullmatch(tid):
        return jsonify({"error": "Invalid or missing track ID"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    # Validate file type
    allowed_types = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
    if file.content_type not in allowed_types:
        return jsonify({"error": "Unsupported file type"}), 400
    # Read and process image
    try:
        from PIL import Image
        img = Image.open(file.stream)
        img = img.convert("RGB")
        # Crop to 1:1 square (center crop)
        w, h = img.size
        size = min(w, h)
        left = (w - size) // 2
        top = (h - size) // 2
        img = img.crop((left, top, left + size, top + size))
        # Resize to max 1200x1200 for storage
        max_size = 1200
        if size > max_size:
            img = img.resize((max_size, max_size), Image.Resampling.LANCZOS)
        # Save to SAVED_DIR
        save_path = SAVED_DIR / f"{tid}.jpg"
        img.save(save_path, format="JPEG", quality=90, optimize=True)
    except Exception as e:
        log.warning(f"Artwork upload failed for {tid}: {e}")
        return jsonify({"error": f"Failed to process image: {e}"}), 500
    # Invalidate caches
    _invalidate_file_index()
    return jsonify({
        "success": True,
        "url": f"/api/local_file?q={tid}.jpg",
        "tid": tid
    })


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
        response = send_from_directory(
            directory, filename, conditional=True, mimetype=_local_media_mimetype(file_path)
        )
        response.headers.setdefault("Accept-Ranges", "bytes")
        response.headers.setdefault("Cache-Control", "private, no-cache")
        response.headers["X-Aki-Local"] = "saved"
        return response
    # Use reverse index instead of rglob
    idx = _get_file_index()
    matched = idx.get(filename)
    if matched and matched.is_file():
        response = send_from_directory(
            str(matched.parent), matched.name, conditional=True, mimetype=_local_media_mimetype(matched)
        )
        response.headers.setdefault("Accept-Ranges", "bytes")
        response.headers.setdefault("Cache-Control", "private, no-cache")
        response.headers["X-Aki-Local"] = "library"
        return response
    return jsonify({"error": "Not found"}), 404

def _scan_playlists():
    playlists = []
    if not PLAYLISTS_DIR.exists():
        return playlists
    for entry in sorted(PLAYLISTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        with _playlist_guard(entry.name):
            try:
                files = list(entry.iterdir())
            except OSError:
                continue
            audio_files = sorted(f for f in files if f.suffix.lower() in _LOCAL_AUDIO_EXTENSIONS)
            meta_files = sorted(f for f in files if f.name.endswith(".meta.json"))
            if not audio_files and not meta_files:
                continue
            audio_tids = {_tid_from_audio_filename(path) for path in audio_files}
            valid_meta = {}
            for meta_path in meta_files:
                tid = meta_path.name[:-len(".meta.json")]
                meta = _read_track_meta(meta_path, tid)
                if meta:
                    valid_meta[tid] = meta
            if not audio_files and not valid_meta:
                continue
            count = len(audio_tids | set(valid_meta))
            playlist_meta = _read_playlist_json(entry / "playlist.json", "playlist metadata")
            album_meta = _read_playlist_json(entry / "album.json", "album metadata")
            cover_art = ""
            local_cover = entry / "cover.jpg"
            try:
                if local_cover.exists() and local_cover.stat().st_size > 100:
                    cover_art = f"/api/library_file?q={entry.name}/cover.jpg"
            except OSError:
                pass
            if not cover_art:
                cover_art = str(album_meta.get("art") or "")
            if not cover_art:
                ordered_meta = []
                for audio in audio_files:
                    ordered_meta.append(entry / f"{_tid_from_audio_filename(audio)}.meta.json")
                ordered_meta.extend(meta_files)
                seen = set()
                for meta_path in ordered_meta:
                    if meta_path in seen or not meta_path.exists():
                        continue
                    seen.add(meta_path)
                    tid = meta_path.name[:-len(".meta.json")]
                    cover_art = _read_track_meta(meta_path, tid).get("art", "")
                    if cover_art:
                        break
            pending_count = sum(
                1 for tid, meta in valid_meta.items()
                if tid not in audio_tids and meta.get("downloadState") == "pending"
            )
            entry_data = {
                "name": entry.name,
                "count": count,
                "downloaded": len(audio_tids),
                "coverArt": cover_art,
                "isAlbum": bool(album_meta),
                "albumId": album_meta.get("albumId", ""),
                "albumArtist": album_meta.get("artist", ""),
                "pending": pending_count,
            }
            for key in ("source", "spotifyPlaylistId", "description"):
                if playlist_meta.get(key):
                    entry_data[key] = playlist_meta[key]
            playlists.append(entry_data)
    return playlists

def _get_playlist_index():
    global _playlist_index_cache
    with _playlist_index_lock:
        cached = _playlist_index_cache
        generation = _playlist_index_generation
    if cached is not None:
        return cached
    # Never hold the cache lock while acquiring per-playlist locks. Writers
    # invalidate the cache after releasing their playlist lock, which gives us
    # one consistent lock order and avoids scan/write deadlocks.
    scanned = _scan_playlists()
    with _playlist_index_lock:
        if _playlist_index_cache is None and generation == _playlist_index_generation:
            _playlist_index_cache = scanned
        return _playlist_index_cache if _playlist_index_cache is not None else scanned

def _invalidate_playlist_cache():
    global _playlist_index_cache, _playlist_index_generation
    with _playlist_index_lock:
        _playlist_index_cache = None
        _playlist_index_generation += 1

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
    with _playlist_catalog_lock:
        target = PLAYLISTS_DIR / safe_name
        if target.exists():
            return jsonify({"error": "Playlist already exists"}), 409
        with _playlist_guard(safe_name):
            target.mkdir(parents=True, exist_ok=False)
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
    should_download = data.get("download", True) is not False
    safe_name = _safe_playlist_name(playlist)
    if not safe_name or not track:
        return jsonify({"error": "Missing playlist or track"}), 400
    track = _normalize_playlist_track(track)
    if not track:
        return jsonify({"error": "Track name and artist are required"}), 400
    track["downloadState"] = "pending" if should_download else "remote"
    if album_data:
        save_album_metadata(safe_name, album_data)
    # Write the meta.json synchronously so the playlist shows up as "pending"
    # immediately when the frontend re-fetches (before the async audio download).
    tid, created = _write_playlist_meta(track, safe_name, track_number)
    if created and should_download:
        _download_executor.submit(download_to_playlist, track, safe_name, track_number)
    _invalidate_playlist_cache()
    _invalidate_file_index()
    return jsonify({"success": True, "playlist": safe_name, "tid": tid, "duplicate": not created})


def _offline_job_snapshot(playlist_name: str) -> dict | None:
    with _offline_collection_jobs_lock:
        job = _offline_collection_jobs.get(playlist_name)
        return dict(job) if job else None


def _update_offline_job(playlist_name: str, **changes):
    with _offline_collection_jobs_lock:
        job = _offline_collection_jobs.get(playlist_name)
        if job is not None:
            job.update(changes)
            _offline_collection_jobs.move_to_end(playlist_name)


def _prepare_offline_playlist_tracks(playlist_name: str, tracks: list) -> list:
    """Persist a complete collection without starting unbounded per-track jobs."""
    prepared = []
    pl_dir = PLAYLISTS_DIR / playlist_name
    with _playlist_guard(playlist_name):
        pl_dir.mkdir(parents=True, exist_ok=True)
        local_tids = {
            _tid_from_audio_filename(path)
            for path in pl_dir.iterdir()
            if path.suffix.lower() in _LOCAL_AUDIO_EXTENSIONS
        }
        used_numbers = _playlist_track_numbers_locked(pl_dir)
        next_number = (max(used_numbers) if used_numbers else 0) + 1
        seen_tids = set()
        for index, raw_track in enumerate(tracks or []):
            track = _normalize_playlist_track(raw_track)
            if not track:
                continue
            tid = track.get("tid") or get_track_id(track.get("name", ""), track.get("artist", ""))
            if not tid:
                continue
            if tid in seen_tids:
                continue
            seen_tids.add(tid)
            track["tid"] = tid
            requested_number = _coerce_track_number(raw_track.get("trackNumber")) or index + 1
            number = requested_number
            if number in used_numbers:
                current_meta = _read_playlist_json(pl_dir / f"{tid}.meta.json", f"track metadata for {tid}") or {}
                number = _coerce_track_number(current_meta.get("trackNumber")) or next_number
            used_numbers.add(number)
            next_number = max(next_number, number + 1)
            meta_path = pl_dir / f"{tid}.meta.json"
            existing = _read_playlist_json(meta_path, f"track metadata for {tid}") or {}
            payload = _playlist_meta_payload(track, number, existing.get("addedAt") or time.time())
            # Keep richer previously-resolved provider values when a sparse
            # persisted playlist is sent back by the client.
            for key, value in existing.items():
                if key not in payload or not payload.get(key):
                    payload[key] = value
            payload["downloadState"] = "ready" if tid in local_tids else "pending"
            payload["downloadError"] = ""
            _atomic_write_json(meta_path, payload)
            prepared.append(dict(track, trackNumber=number, downloadState=payload["downloadState"]))
    _invalidate_playlist_cache()
    _invalidate_file_index()
    return prepared


def _cache_offline_lyrics(track: dict) -> bool:
    """Use the normal version-aware lyrics route so offline and live agree."""
    try:
        params = {
            "title": track.get("name", ""),
            "artist": track.get("artist", ""),
            "videoId": track.get("videoId", ""),
            "album": track.get("album") or track.get("albumName") or "",
            "duration": track.get("dur") or track.get("duration") or "",
        }
        with app.test_request_context("/api/lyrics", query_string=params):
            response = api_lyrics()
            if isinstance(response, tuple):
                response = response[0]
            payload = response.get_json(silent=True) if hasattr(response, "get_json") else None
        return bool(payload and payload.get("lines"))
    except Exception as exc:
        log.debug("Offline lyrics cache failed for %s: %s", track.get("name", "track"), exc)
        return False


def _cache_offline_track_assets(track: dict, playlist_name: str) -> dict:
    return {
        "art": _cache_playlist_track_art(track, playlist_name),
        "lyrics": _cache_offline_lyrics(track),
    }


def _cache_offline_playlist_cover(playlist_name: str, cover_url: str) -> bool:
    if not cover_url:
        return False
    pl_dir = PLAYLISTS_DIR / playlist_name
    cover_path = pl_dir / "cover.jpg"
    try:
        if cover_path.exists() and cover_path.stat().st_size > 100:
            return True
        content, _working_url = _download_first_artwork([cover_url])
        if not content:
            return False
        with _playlist_guard(playlist_name):
            if not pl_dir.exists():
                return False
            temporary = cover_path.with_suffix(".jpg.tmp")
            temporary.write_bytes(content)
            os.replace(temporary, cover_path)
        _invalidate_playlist_cache()
        _invalidate_file_index()
        return True
    except Exception as exc:
        log.debug("Offline collection cover failed for %s: %s", playlist_name, exc)
        return False


def _download_offline_collection(playlist_name: str, tracks: list, cover_url: str):
    asset_futures = []
    cover_future = _offline_asset_executor.submit(
        _cache_offline_playlist_cover, playlist_name, cover_url
    ) if cover_url else None
    downloaded = failed = 0
    errors = []
    _update_offline_job(playlist_name, status="downloading", startedAt=time.time())
    for index, track in enumerate(tracks):
        title = str(track.get("name") or "Track")
        _update_offline_job(playlist_name, current=title)
        ok = download_to_playlist(
            track, playlist_name, track.get("trackNumber") or index + 1, cache_art=False
        )
        if ok:
            downloaded += 1
            asset_futures.append(_offline_asset_executor.submit(
                _cache_offline_track_assets, track, playlist_name
            ))
        else:
            failed += 1
            with _download_status_lock:
                status = dict(_download_status.get(track.get("tid") or "", {}))
            message = str(status.get("error") or "Download failed")[:180]
            errors.append(f"{title}: {message}")
        _update_offline_job(
            playlist_name, processed=index + 1, downloaded=downloaded,
            failed=failed, errors=errors[-8:],
        )

    _update_offline_job(playlist_name, status="assets", current="Saving covers and lyrics")
    artwork = lyrics = assets_processed = 0
    for future in asset_futures:
        try:
            result = future.result()
            artwork += int(bool(result.get("art")))
            lyrics += int(bool(result.get("lyrics")))
        except Exception as exc:
            log.debug("Offline asset worker failed: %s", exc)
        assets_processed += 1
        _update_offline_job(
            playlist_name, assetsProcessed=assets_processed,
            artwork=artwork, lyrics=lyrics,
        )
    cover_ready = False
    if cover_future:
        try:
            cover_ready = bool(cover_future.result())
        except Exception:
            pass
    _invalidate_playlist_cache()
    _invalidate_file_index()
    _update_offline_job(
        playlist_name, status="complete", current="", cover=cover_ready,
        artwork=artwork, lyrics=lyrics, finishedAt=time.time(),
    )


def _download_offline_collection_guarded(playlist_name: str, tracks: list, cover_url: str):
    try:
        _download_offline_collection(playlist_name, tracks, cover_url)
    except Exception as exc:
        log.exception("Offline collection worker failed for %s", playlist_name)
        snapshot = _offline_job_snapshot(playlist_name) or {}
        errors = list(snapshot.get("errors") or [])
        errors.append(str(exc)[:180])
        _update_offline_job(
            playlist_name, status="complete", current="", finishedAt=time.time(),
            failed=max(int(snapshot.get("failed") or 0), 1), errors=errors[-8:],
        )


@app.route("/api/playlists/offline", methods=["POST"])
def api_playlists_offline():
    data = request.get_json(force=True) or {}
    requested_name = str(data.get("playlist") or "").strip()
    safe_name = _safe_playlist_name(requested_name)
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    incoming_tracks = data.get("tracks")
    if incoming_tracks is not None and not isinstance(incoming_tracks, list):
        return jsonify({"error": "Tracks must be a list"}), 400
    album_data = data.get("albumData") if isinstance(data.get("albumData"), dict) else None
    existing_collection = bool(data.get("existing", True))

    # A remote album may share a title with an unrelated playlist. Preserve the
    # existing collection and allocate a distinct local album in that case.
    with _playlist_catalog_lock:
        target = PLAYLISTS_DIR / safe_name
        if album_data and not existing_collection and target.exists():
            current_album = _read_playlist_json(target / "album.json", "album metadata")
            same_album = bool(
                current_album and album_data.get("albumId") and
                current_album.get("albumId") == album_data.get("albumId")
            )
            if not same_album:
                safe_name = _unique_playlist_name_locked(safe_name)
                target = PLAYLISTS_DIR / safe_name
        with _playlist_guard(safe_name):
            target.mkdir(parents=True, exist_ok=True)

    tracks = incoming_tracks
    if tracks is None:
        tracks = _load_playlist_tracks(safe_name)
    if not tracks:
        return jsonify({"error": "This collection has no tracks"}), 400
    prepared = _prepare_offline_playlist_tracks(safe_name, tracks)
    if not prepared:
        return jsonify({"error": "This collection has no valid tracks"}), 400
    if album_data:
        album_payload = dict(album_data, tracks=prepared)
        save_album_metadata(safe_name, album_payload)

    with _offline_collection_jobs_lock:
        running = _offline_collection_jobs.get(safe_name)
        if running and running.get("status") in {"queued", "downloading", "assets"}:
            return jsonify(dict(running)), 202
        while len(_offline_collection_jobs) >= _OFFLINE_COLLECTION_JOBS_MAX:
            removable = next((key for key, job in _offline_collection_jobs.items()
                              if job.get("status") == "complete"), None)
            if removable is None:
                break
            _offline_collection_jobs.pop(removable, None)
        job = {
            "playlist": safe_name, "status": "queued", "total": len(prepared),
            "processed": 0, "downloaded": 0, "failed": 0,
            "assetsProcessed": 0, "artwork": 0, "lyrics": 0,
            "cover": False, "current": "", "errors": [], "createdAt": time.time(),
        }
        _offline_collection_jobs[safe_name] = job
    _offline_collection_executor.submit(
        _download_offline_collection_guarded, safe_name, prepared, str(data.get("coverUrl") or "")
    )
    return jsonify(dict(job)), 202


@app.route("/api/playlists/offline/status")
def api_playlists_offline_status():
    safe_name = _safe_playlist_name(request.args.get("playlist", ""))
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    job = _offline_job_snapshot(safe_name)
    if not job:
        return jsonify({"playlist": safe_name, "status": "idle"})
    return jsonify(job)


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
        content, working_url = _download_first_artwork([cover_url])
        if content:
            with _playlist_guard(safe_name):
                if not pl_dir.exists():
                    return jsonify({"error": "Playlist was removed"}), 409
                tmp_cover = cover_path.with_suffix(".jpg.tmp")
                tmp_cover.write_bytes(content)
                os.replace(tmp_cover, cover_path)
            _invalidate_playlist_cache()
            return jsonify({"success": True, "source": working_url})
        return jsonify({"error": "Failed to download a valid playlist cover"}), 502
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
    meta = {}
    for key in ("source", "spotifyPlaylistId", "description"):
        if key in data:
            meta[key] = data[key]
    try:
        with _playlist_catalog_lock:
            with _playlist_guard(safe_name):
                pl_dir = PLAYLISTS_DIR / safe_name
                pl_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write_json(pl_dir / "playlist.json", meta)
        _invalidate_playlist_cache()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlists/enrich_artwork", methods=["POST"])
def api_playlists_enrich_artwork():
    """Start one deduplicated artwork-resolution job for a playlist."""
    data = request.get_json(force=True) or {}
    playlist = data.get("playlist", "")
    force = bool(data.get("force"))
    safe_name = _safe_playlist_name(playlist)
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist"}), 400
    pl_dir = PLAYLISTS_DIR / safe_name
    if not pl_dir.exists():
        return jsonify({"error": "Playlist not found"}), 404

    with _artwork_jobs_lock:
        existing = _artwork_jobs.get(safe_name)
        if existing and existing.get("status") in ("queued", "running"):
            return jsonify(dict(existing)), 202
        job = {
            "playlist": safe_name, "status": "queued", "total": 0,
            "processed": 0, "resolved": 0, "downloaded": 0,
            "missing": 0, "startedAt": time.time(), "finishedAt": 0,
        }
        _artwork_jobs[safe_name] = job
        _artwork_jobs.move_to_end(safe_name)
        while len(_artwork_jobs) > _ARTWORK_JOBS_MAX:
            _artwork_jobs.popitem(last=False)

    def _set_job(**updates):
        with _artwork_jobs_lock:
            current = _artwork_jobs.setdefault(safe_name, {"playlist": safe_name})
            current.update(updates)

    def _enrich_worker():
        _set_job(status="running")
        with _playlist_guard(safe_name):
            if not pl_dir.exists():
                _set_job(status="failed", error="Playlist was removed", finishedAt=time.time())
                return
            meta_files = sorted(f for f in pl_dir.iterdir() if f.name.endswith(".meta.json"))
            meta_snapshot = [(mf, _read_playlist_json(mf, f"track metadata for {mf.name}")) for mf in meta_files]
        print(f"[ENRICH] Starting artwork enrichment for {safe_name}: {len(meta_files)} tracks", flush=True)
        pending: list[tuple] = []
        for mf, meta in meta_snapshot:
            if not meta:
                continue
            existing_candidates = meta.get("art_candidates") if isinstance(meta.get("art_candidates"), list) else []
            if meta.get("art_resolved_at") and len(existing_candidates) >= 2:
                continue
            pending.append((mf, meta, meta.get("art", "") or meta.get("album_art", "")))

        _set_job(total=len(pending))

        if not pending:
            _set_job(status="complete", finishedAt=time.time())
            return

        results = resolve_artwork_batch([meta for _mf, meta, _art in pending], force=force)
        resolved_count = downloaded_count = missing_count = 0
        for (mf, meta, art_before), enriched in zip(pending, results):
            try:
                new_art = enriched.get("art") or enriched.get("album_art", "")
                tid = mf.stem.replace(".meta", "")
                art_path = pl_dir / f"{tid}.jpg"
                candidates = enriched.get("art_candidates") or ([new_art] if new_art else [])
                art_bytes, working_url = (None, "") if art_path.exists() else _download_first_artwork(candidates)
                if working_url:
                    new_art = working_url
                with _playlist_guard(safe_name):
                    if not pl_dir.exists() or not mf.exists():
                        continue
                    current = _read_playlist_json(mf, f"track metadata for {mf.name}")
                    if not current:
                        continue
                    if new_art:
                        current["art"] = new_art
                        current["album_art"] = new_art
                    current["art_candidates"] = candidates
                    current["art_source"] = enriched.get("art_source", "")
                    current["art_confidence"] = enriched.get("art_confidence", 0)
                    if enriched.get("videoId"):
                        current["videoId"] = enriched["videoId"]
                    if enriched.get("album"):
                        current["album"] = enriched["album"]
                    if enriched.get("albumId"):
                        current["albumId"] = enriched["albumId"]
                    if enriched.get("recording_confidence"):
                        current["recording_confidence"] = enriched["recording_confidence"]
                    current["art_resolved_at"] = time.time()
                    _atomic_write_json(mf, current)
                    if art_bytes and not art_path.exists():
                        tmp_art = art_path.with_suffix(".jpg.tmp")
                        tmp_art.write_bytes(art_bytes)
                        os.replace(tmp_art, art_path)
                if new_art:
                    resolved_count += 1
                else:
                    missing_count += 1
                if art_bytes:
                    downloaded_count += 1
                _set_job(
                    processed=resolved_count + missing_count, resolved=resolved_count,
                    downloaded=downloaded_count, missing=missing_count,
                )
            except Exception as e:
                log.warning(f"Artwork enrichment failed for {mf.name}: {e}")
                missing_count += 1
                _set_job(processed=resolved_count + missing_count, missing=missing_count)
        _invalidate_file_index()
        _invalidate_playlist_cache()
        _set_job(
            status="complete", processed=len(pending), resolved=resolved_count,
            downloaded=downloaded_count, missing=missing_count, finishedAt=time.time(),
        )
        print(f"[ENRICH] Done for {safe_name}: {resolved_count}/{len(pending)} resolved", flush=True)

    def _run_enrich_worker():
        try:
            _enrich_worker()
        except Exception as exc:
            log.warning(f"Artwork enrichment job failed for {safe_name}: {exc}")
            _set_job(status="failed", error=str(exc), finishedAt=time.time())

    _artwork_executor.submit(_run_enrich_worker)
    return jsonify(dict(job)), 202


@app.route("/api/playlists/enrich_artwork/status")
def api_playlists_enrich_artwork_status():
    safe_name = _safe_playlist_name(request.args.get("playlist", ""))
    if not safe_name:
        return jsonify({"error": "Invalid playlist"}), 400
    with _artwork_jobs_lock:
        job = _artwork_jobs.get(safe_name)
        return jsonify(dict(job) if job else {"playlist": safe_name, "status": "idle"})


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


def _load_playlist_tracks_unlocked(safe_name: str) -> list:
    pl_dir = PLAYLISTS_DIR / safe_name
    if not pl_dir.exists():
        return []
    # Android keeps native m4a/webm/opus containers, while desktop normally
    # stores mp3. Treat every supported local audio suffix identically.
    have_audio = set()
    for f in pl_dir.iterdir():
        if f.suffix.lower() in _LOCAL_AUDIO_EXTENSIONS:
            tid = _tid_from_audio_filename(f)
            have_audio.add(tid)
    tracks = []
    # 1) Downloaded tracks
    for f in sorted(pl_dir.iterdir()):
        if f.suffix.lower() in _LOCAL_AUDIO_EXTENSIONS and f.stem and not f.stem.startswith("."):
            tid = _tid_from_audio_filename(f)
            art_file = pl_dir / f"{tid}.jpg"
            meta_file = pl_dir / f"{tid}.meta.json"
            name = tid
            artist = "Unknown Artist"
            dur = 0
            art = ""
            track_number = 999
            added_at = f.stat().st_mtime if hasattr(f, "stat") and f.exists() else 0
            if meta_file.exists():
                meta = _read_track_meta(meta_file, tid)
                if meta:
                    name = meta.get("name", tid)
                    artist = meta.get("artist", "Unknown Artist")
                    dur = meta.get("dur", 0)
                    art = meta.get("art", "")
                    track_number = meta.get("trackNumber", 999)
                    if meta.get("addedAt"):
                        added_at = meta["addedAt"]
                    else:
                        added_at = meta_file.stat().st_mtime
            # Backfill real MP3 duration when the stored one is missing/zero so
            # playlist totals add up correctly (persisted back into meta.json).
            if (not dur or dur <= 0) and f.exists():
                real_dur = _mp3_duration_seconds(f)
                if real_dur:
                    dur = int(round(real_dur))
                    try:
                        if meta_file.exists():
                            _m = _read_playlist_json(meta_file, f"track metadata for {tid}")
                            _m["dur"] = dur
                            _atomic_write_json(meta_file, _m)
                    except Exception:
                        pass
            tracks.append({
                "name": name,
                "artist": artist,
                "tid": tid,
                "dur": dur,
                "art": art,
                "videoId": meta.get("videoId", "") if meta_file.exists() and meta else "",
                "albumId": meta.get("albumId", "") if meta_file.exists() and meta else "",
                "album": meta.get("album", "") if meta_file.exists() and meta else "",
                "albumName": meta.get("albumName", "") if meta_file.exists() and meta else "",
                "art_candidates": meta.get("art_candidates", []) if meta_file.exists() and meta else [],
                "art_source": meta.get("art_source", "") if meta_file.exists() and meta else "",
                "art_confidence": meta.get("art_confidence", 0) if meta_file.exists() and meta else 0,
                "art_resolved_at": meta.get("art_resolved_at", 0) if meta_file.exists() and meta else 0,
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
            if tid in have_audio:
                continue
            meta = _read_track_meta(f, tid)
            if not meta:
                continue
            name = meta.get("name", tid)
            artist = meta.get("artist", "Unknown Artist")
            dur = meta.get("dur", 0)
            art = meta.get("art", "")
            track_number = meta.get("trackNumber", 999)
            added_at = meta.get("addedAt") or f.stat().st_mtime
            art_file = pl_dir / f"{tid}.jpg"
            is_pending = meta.get("downloadState") != "remote"
            tracks.append({
                "name": name,
                "artist": artist,
                "tid": tid,
                "dur": dur,
                "art": art,
                "trackNumber": track_number,
                "dateAdded": added_at,
                "videoId": meta.get("videoId", ""),
                "albumId": meta.get("albumId", ""),
                "album": meta.get("album", ""),
                "albumName": meta.get("albumName", ""),
                "art_candidates": meta.get("art_candidates", []),
                "art_source": meta.get("art_source", ""),
                "art_confidence": meta.get("art_confidence", 0),
                "art_resolved_at": meta.get("art_resolved_at", 0),
                "local_audio": False,
                "local_art": art_file.exists(),
                "playlist": safe_name,
                "pending": is_pending,
                "remote_only": not is_pending,
            })
            have_audio.add(tid)  # avoid double-counting if stem parsing was ambiguous
    tracks.sort(key=lambda x: x.get("trackNumber") or 999)
    return tracks


def _load_playlist_tracks(safe_name: str) -> list:
    with _playlist_guard(safe_name):
        return _load_playlist_tracks_unlocked(safe_name)


@app.route("/api/playlists/tracks")
def api_playlists_tracks():
    safe_name = _safe_playlist_name(request.args.get("name", ""))
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify([])
    _burst_check("playlists_tracks", safe_name)
    return jsonify(_load_playlist_tracks(safe_name))


_PLAYLIST_EXPORT_SCHEMA = "akimelody-playlist"
_PLAYLIST_EXPORT_VERSION = 1
_PLAYLIST_IMPORT_MAX_TRACKS = 5000


def _playlist_export_track(track: dict) -> dict:
    return {
        "name": track.get("name", ""),
        "artist": track.get("artist", ""),
        "tid": get_track_id(track.get("name", ""), track.get("artist", "")),
        "dur": _coerce_playlist_duration(track.get("dur")),
        "art": track.get("art", ""),
        "videoId": track.get("videoId", ""),
        "albumId": track.get("albumId", ""),
        "album": track.get("album") or track.get("albumName") or "",
        "art_candidates": track.get("art_candidates") or [],
        "art_source": track.get("art_source", ""),
        "trackNumber": _coerce_track_number(track.get("trackNumber")),
        "dateAdded": track.get("dateAdded", 0),
        "localAudio": bool(track.get("local_audio")),
    }


def _playlist_export_document(safe_name: str) -> dict | None:
    with _playlist_guard(safe_name):
        pl_dir = PLAYLISTS_DIR / safe_name
        if not pl_dir.exists():
            return None
        tracks = _load_playlist_tracks_unlocked(safe_name)
        playlist_meta = _read_playlist_json(pl_dir / "playlist.json", "playlist metadata")
        album_meta = _read_playlist_json(pl_dir / "album.json", "album metadata")
        return {
            "schema": _PLAYLIST_EXPORT_SCHEMA,
            "version": _PLAYLIST_EXPORT_VERSION,
            "exportedAt": int(time.time()),
            "appVersion": APP_VERSION,
            "playlist": {
                "name": safe_name,
                "description": str(playlist_meta.get("description") or "")[:2000],
                "source": str(playlist_meta.get("source") or "")[:128],
                "isAlbum": bool(album_meta),
            },
        "tracks": [
            _playlist_export_track(_merge_track_meta_extras(pl_dir / f"{track['tid']}.meta.json", track))
            for track in tracks
        ],
        }


def _playlist_m3u_document(safe_name: str) -> str | None:
    with _playlist_guard(safe_name):
        if not (PLAYLISTS_DIR / safe_name).exists():
            return None
        tracks = _load_playlist_tracks_unlocked(safe_name)
    lines = ["#EXTM3U", f"#PLAYLIST:{safe_name}"]
    for track in tracks:
        duration = _coerce_playlist_duration(track.get("dur")) or -1
        label = f"{track.get('artist', 'Unknown Artist')} - {track.get('name', 'Unknown')}".replace("\r", " ").replace("\n", " ")
        lines.append(f"#EXTINF:{duration},{label}")
        if track.get("videoId"):
            lines.append(f"https://music.youtube.com/watch?v={urllib.parse.quote(str(track['videoId']), safe='')}")
        elif track.get("local_audio"):
            query = urllib.parse.urlencode({
                "tid": track.get("tid", ""),
                "name": track.get("name", ""),
                "artist": track.get("artist", ""),
            })
            lines.append(f"http://127.0.0.1:{SERVER_PORT}/api/stream?{query}")
        else:
            lines.append(f"ytsearch1:{urllib.parse.quote_plus(label + ' audio')}")
    return "\n".join(lines) + "\n"


def _parse_playlist_import_document(data) -> tuple[dict | None, str | None]:
    if not isinstance(data, dict):
        return None, "Import file must contain a JSON object"
    if data.get("schema") != _PLAYLIST_EXPORT_SCHEMA:
        return None, "This is not an AkiMelody playlist export"
    if data.get("version") != _PLAYLIST_EXPORT_VERSION:
        return None, f"Unsupported playlist version: {data.get('version')}"
    playlist = data.get("playlist")
    tracks = data.get("tracks")
    if not isinstance(playlist, dict) or not isinstance(tracks, list):
        return None, "Playlist export is missing playlist or track data"
    if len(tracks) > _PLAYLIST_IMPORT_MAX_TRACKS:
        return None, f"Playlist contains more than {_PLAYLIST_IMPORT_MAX_TRACKS} tracks"
    requested_name = _safe_playlist_name(str(playlist.get("name") or "Imported Playlist"))
    normalized = []
    invalid = []
    duplicates_in_file = []
    seen = set()
    for index, raw_track in enumerate(tracks):
        track = _normalize_playlist_track(raw_track)
        if not track:
            invalid.append(index + 1)
            continue
        if track["tid"] in seen:
            duplicates_in_file.append(index + 1)
            continue
        seen.add(track["tid"])
        track["trackNumber"] = len(normalized) + 1
        track["downloadState"] = "remote"
        normalized.append(track)
    return {
        "requestedName": requested_name,
        "description": str(playlist.get("description") or "")[:2000],
        "source": str(playlist.get("source") or "import")[:128] or "import",
        "tracks": normalized,
        "invalidRows": invalid,
        "duplicateRows": duplicates_in_file,
    }, None


def _playlist_import_preview(parsed: dict, mode: str, requested_name: str | None = None) -> dict:
    base_name = _safe_playlist_name(requested_name or parsed["requestedName"])
    with _playlist_catalog_lock:
        exists = (PLAYLISTS_DIR / base_name).exists()
        target_name = _unique_playlist_name_locked(base_name) if mode == "copy" and exists else base_name
        existing_tids = set()
        if mode == "merge" and exists:
            with _playlist_guard(base_name):
                existing_tids = _playlist_existing_tids_locked(PLAYLISTS_DIR / base_name)
    duplicate_existing = [track["tid"] for track in parsed["tracks"] if track["tid"] in existing_tids]
    return {
        "valid": True,
        "requestedName": base_name,
        "targetName": target_name,
        "nameConflict": exists,
        "mode": mode,
        "totalRows": len(parsed["tracks"]) + len(parsed["invalidRows"]) + len(parsed["duplicateRows"]),
        "validTracks": len(parsed["tracks"]),
        "newTracks": len(parsed["tracks"]) - len(duplicate_existing),
        "duplicateInFile": len(parsed["duplicateRows"]),
        "duplicateExisting": len(duplicate_existing),
        "invalidTracks": len(parsed["invalidRows"]),
        "downloadNote": "Imported tracks stream normally; audio downloads are not included in the export.",
    }


@app.route("/api/playlists/export")
def api_playlists_export():
    safe_name = _safe_playlist_name(request.args.get("name", ""))
    export_format = str(request.args.get("format") or "json").lower()
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    if export_format == "m3u":
        body = _playlist_m3u_document(safe_name)
        mimetype, extension = "audio/x-mpegurl", "m3u"
    else:
        document = _playlist_export_document(safe_name)
        body = json.dumps(document, ensure_ascii=False, indent=2) if document else None
        mimetype, extension = "application/json", "akiplaylist.json"
    if body is None:
        return jsonify({"error": "Playlist not found"}), 404
    response = Response(body, mimetype=mimetype)
    filename = safe_filename_component(safe_name, fallback="playlist") + "." + extension
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/playlists/import/preview", methods=["POST"])
def api_playlists_import_preview():
    data = request.get_json(force=True) or {}
    parsed, error = _parse_playlist_import_document(data.get("document"))
    if error:
        return jsonify({"valid": False, "error": error}), 400
    mode = "merge" if data.get("mode") == "merge" else "copy"
    return jsonify(_playlist_import_preview(parsed, mode, data.get("name")))


@app.route("/api/playlists/import", methods=["POST"])
def api_playlists_import():
    data = request.get_json(force=True) or {}
    parsed, error = _parse_playlist_import_document(data.get("document"))
    if error:
        return jsonify({"error": error}), 400
    if not parsed["tracks"]:
        return jsonify({"error": "Playlist contains no valid tracks"}), 400
    mode = "merge" if data.get("mode") == "merge" else "copy"
    requested_name = _safe_playlist_name(str(data.get("name") or parsed["requestedName"]))
    if not requested_name or not _validate_playlist_path(requested_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    return jsonify(_apply_playlist_import(parsed, mode, requested_name))


def _apply_playlist_import(parsed: dict, mode: str, requested_name: str) -> dict:
    """Apply an already validated import under catalog + playlist locks."""
    with _playlist_catalog_lock:
        exists = (PLAYLISTS_DIR / requested_name).exists()
        if mode == "merge":
            target_name = requested_name
        else:
            target_name = _unique_playlist_name_locked(requested_name) if exists else requested_name
        with _playlist_guard(target_name):
            pl_dir = PLAYLISTS_DIR / target_name
            pl_dir.mkdir(parents=True, exist_ok=True)
            existing = _playlist_existing_tids_locked(pl_dir)
            next_number = _next_playlist_track_number_locked(pl_dir)
            added = 0
            duplicate_existing = 0
            for track in parsed["tracks"]:
                if track["tid"] in existing:
                    duplicate_existing += 1
                    continue
                track_number = next_number if mode == "merge" else added + 1
                _atomic_write_json(
                    pl_dir / f"{track['tid']}.meta.json",
                    _playlist_meta_payload(track, track_number),
                )
                existing.add(track["tid"])
                next_number += 1
                added += 1
            playlist_meta = _read_playlist_json(pl_dir / "playlist.json", "playlist metadata")
            playlist_meta.update({
                "source": parsed["source"] or "import",
                "description": parsed["description"],
                "importedAt": int(time.time()),
                "importSchema": _PLAYLIST_EXPORT_SCHEMA,
                "importVersion": _PLAYLIST_EXPORT_VERSION,
            })
            _atomic_write_json(pl_dir / "playlist.json", playlist_meta)
    _invalidate_playlist_cache()
    _invalidate_file_index()
    return {
        "success": True,
        "name": target_name,
        "mode": mode,
        "added": added,
        "duplicates": duplicate_existing + len(parsed["duplicateRows"]),
        "invalid": len(parsed["invalidRows"]),
    }

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
        response = send_from_directory(
            str(matched.parent), matched.name, conditional=True, mimetype=_local_media_mimetype(matched)
        )
        response.headers.setdefault("Accept-Ranges", "bytes")
        response.headers.setdefault("Cache-Control", "private, no-cache")
        response.headers["X-Aki-Local"] = "library"
        return response
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
    for path, data in ((FAVORITES_JSON, []), (SETTINGS_JSON, defaults)):
        try:
            _atomic_write_json(path, data)
        except OSError:
            failed += 1

    try:
        _atomic_write_json(STATS_JSON, _empty_stats_doc())
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


def _safe_cookie_header_pairs(header: str) -> list[tuple[str, str]]:
    """Parse a native CookieManager header without accepting control data."""
    pairs = []
    for item in str(header or "")[:32768].split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
            continue
        if not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
            continue
        pairs.append((name, value))
    return pairs


@app.route("/api/youtube/import_cookies", methods=["POST"])
def api_youtube_import_cookies():
    """Persist cookies captured by the app-private Android login WebView."""
    global ytmusic, _auth_state_ok
    if not IS_ANDROID:
        return jsonify({"error": "Native cookie import is only available in the Android app"}), 400
    data = request.get_json(silent=True) or {}
    sources = (
        (".youtube.com", _safe_cookie_header_pairs(data.get("youtube", ""))),
        (".google.com", _safe_cookie_header_pairs(data.get("google", ""))),
    )
    youtube_names = {name for name, _value in sources[0][1]}
    if "SAPISID" not in youtube_names or not ({"SID", "__Secure-1PSID", "__Secure-3PSID"} & youtube_names):
        return jsonify({
            "error": "YouTube Music has not finished creating its session yet",
            "missing": [name for name in ("SAPISID", "SID") if name not in youtube_names],
        }), 400

    lines = ["# Netscape HTTP Cookie File", "# Captured locally by AkiMelody Android"]
    seen = set()
    for domain, pairs in sources:
        for name, value in pairs:
            key = (domain, name)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    temporary = _yt_cookie_file.with_suffix(".txt.tmp")
    try:
        # This explicit browser sign-in replaces any older device-code token.
        # Otherwise _init_ytmusic would continue preferring OAuth and the newly
        # captured cookies would never actually back account-library requests.
        try:
            _yt_oauth_file.unlink(missing_ok=True)
            yauth.clear_auth()
        except Exception as exc:
            log.debug("Old YouTube auth cleanup was incomplete: %s", exc)
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, _yt_cookie_file)
        if not _generate_auth_headers(_yt_cookie_file.read_text(encoding="utf-8")):
            raise RuntimeError("Captured cookies could not create YouTube Music authorization")
        _auth_state_ok = True
        ytmusic = _init_ytmusic()
        verification = _fetch_youtube_likes_raw(1, timeout=18)
        if not isinstance(verification, dict):
            raise RuntimeError("YouTube Music did not return an account library")
        with _liked_lock:
            _liked_cache.clear()
        _invalidate_stream_cache()
        return jsonify({
            "authenticated": True, "cookies": len(seen),
            "library_verified": True,
        })
    except Exception as exc:
        _auth_state_ok = False
        return jsonify({"error": str(exc)}), 502
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _save_youtube_oauth_credentials(client_id: str, client_secret: str) -> dict:
    credentials = {
        "client_id": str(client_id or "").strip(),
        "client_secret": str(client_secret or "").strip(),
    }
    if len(credentials["client_id"]) < 20 or len(credentials["client_secret"]) < 8:
        raise ValueError("A valid YouTube OAuth client ID and secret are required")
    _atomic_write_json(_yt_oauth_credentials_file, credentials)
    return credentials


@app.route("/api/youtube/oauth/device", methods=["POST"])
def api_youtube_oauth_device():
    """Begin Google's device authorization flow entirely on this device.

    Google blocks account sign-in inside embedded WebViews. Device authorization
    keeps the consent page in the phone's trusted browser while the local Flask
    process receives and stores the resulting refreshable token.
    """
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id") or "").strip()
    client_secret = str(data.get("client_secret") or "").strip()
    try:
        credentials = (
            _save_youtube_oauth_credentials(client_id, client_secret)
            if client_id or client_secret
            else _load_youtube_oauth_credentials()
        )
    except ValueError as error:
        return jsonify({"error": str(error), "needs_credentials": True}), 400
    if not credentials:
        return jsonify({
            "error": "Enter a YouTube Data API OAuth client ID and secret to connect on this phone",
            "needs_credentials": True,
        }), 400

    try:
        response = requests.post(
            _YOUTUBE_DEVICE_CODE_URL,
            data={"client_id": credentials["client_id"], "scope": _YOUTUBE_OAUTH_SCOPE},
            timeout=12,
        )
        payload = response.json()
    except Exception as error:
        return jsonify({"error": f"Could not start YouTube sign-in: {error}"}), 502
    if not response.ok or not payload.get("device_code"):
        message = payload.get("error_description") or payload.get("error") or "YouTube rejected the sign-in request"
        return jsonify({"error": message}), 502

    flow_id = secrets.token_urlsafe(24)
    interval = max(5, int(payload.get("interval") or 5))
    with _youtube_oauth_lock:
        now = time.time()
        for key in list(_youtube_oauth_pending):
            if _youtube_oauth_pending[key].get("expires_at", 0) <= now:
                _youtube_oauth_pending.pop(key, None)
        _youtube_oauth_pending[flow_id] = {
            "device_code": payload["device_code"],
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "interval": interval,
            "next_poll": 0.0,
            "expires_at": now + int(payload.get("expires_in") or 900),
        }
        while len(_youtube_oauth_pending) > _YOUTUBE_OAUTH_PENDING_MAX:
            _youtube_oauth_pending.popitem(last=False)
    return jsonify({
        "flow_id": flow_id,
        "user_code": payload.get("user_code", ""),
        "verification_url": payload.get("verification_url") or payload.get("verification_uri") or "https://www.google.com/device",
        "interval": interval,
        "expires_in": int(payload.get("expires_in") or 900),
    })


@app.route("/api/youtube/oauth/poll", methods=["POST"])
def api_youtube_oauth_poll():
    global ytmusic, _auth_state_ok
    data = request.get_json(silent=True) or {}
    flow_id = str(data.get("flow_id") or "").strip()
    with _youtube_oauth_lock:
        flow = _youtube_oauth_pending.get(flow_id)
        if not flow:
            return jsonify({"error": "This sign-in request is no longer active"}), 404
        now = time.time()
        if flow["expires_at"] <= now:
            _youtube_oauth_pending.pop(flow_id, None)
            return jsonify({"error": "The YouTube sign-in code expired"}), 410
        if flow["next_poll"] > now:
            return jsonify({"pending": True, "retry_after": max(1, int(flow["next_poll"] - now + 0.5))})
        flow["next_poll"] = now + flow["interval"]
        request_data = dict(flow)

    try:
        response = requests.post(
            _YOUTUBE_TOKEN_URL,
            data={
                "client_id": request_data["client_id"],
                "client_secret": request_data["client_secret"],
                "device_code": request_data["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=12,
        )
        payload = response.json()
    except Exception as error:
        return jsonify({"error": f"Could not check YouTube sign-in: {error}"}), 502

    if not response.ok:
        reason = payload.get("error", "authorization_pending")
        if reason == "authorization_pending":
            return jsonify({"pending": True, "retry_after": request_data["interval"]})
        if reason == "slow_down":
            with _youtube_oauth_lock:
                if flow_id in _youtube_oauth_pending:
                    _youtube_oauth_pending[flow_id]["interval"] += 5
            return jsonify({"pending": True, "retry_after": request_data["interval"] + 5})
        with _youtube_oauth_lock:
            _youtube_oauth_pending.pop(flow_id, None)
        status = 403 if reason == "access_denied" else 410 if reason == "expired_token" else 502
        return jsonify({"error": payload.get("error_description") or reason.replace("_", " ").title()}), status

    if not payload.get("access_token") or not payload.get("refresh_token"):
        return jsonify({"error": "YouTube returned an incomplete authorization token"}), 502
    token = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_in": int(payload.get("expires_in") or 3600),
        "expires_at": int(time.time()) + int(payload.get("expires_in") or 3600),
        "scope": payload.get("scope") or _YOUTUBE_OAUTH_SCOPE,
        "token_type": payload.get("token_type") or "Bearer",
    }
    _atomic_write_json(_yt_oauth_file, token)
    with _youtube_oauth_lock:
        _youtube_oauth_pending.pop(flow_id, None)
    _auth_state_ok = True
    ytmusic = _init_ytmusic()
    with _liked_lock:
        _liked_cache.clear()
    _invalidate_stream_cache()
    return jsonify({"authenticated": True, "message": "YouTube Music connected"})


@app.route("/api/youtube/auth_status")
def api_youtube_auth_status():
    """Return current YouTube auth state for the Python backend."""
    has_cookies = bool(_resolve_cookie_file())
    has_headers = has_valid_auth_state()
    oauth_credentials = bool(_load_youtube_oauth_credentials())
    oauth_token = _has_youtube_oauth_state()

    # Check if headers.json is potentially expired (SAPISIDHASH expires ~2 hours)
    headers_age = 0
    if _yt_headers_file.exists():
        try:
            headers_age = time.time() - _yt_headers_file.stat().st_mtime
        except Exception:
            pass
    headers_potentially_expired = headers_age > 7200  # 2 hours

    return jsonify({
        "authenticated": has_headers and not headers_potentially_expired,
        "cookies": has_cookies,
        "ytmusic_auth": has_headers,
        "ytmusic_authenticated": has_headers,
        "oauth_configured": oauth_credentials,
        "oauth_token": oauth_token,
        "android": IS_ANDROID,
        "headers_age_seconds": int(headers_age),
        "headers_potentially_expired": headers_potentially_expired,
    })


def _fetch_youtube_likes_raw(limit: int, timeout: int = 18) -> dict:
    """Fetch the account library with a hard route-level deadline."""
    future = _io_executor.submit(ytmusic.get_liked_songs, limit=limit)
    try:
        result = future.result(timeout=timeout)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise TimeoutError("YouTube Music liked songs timed out") from exc
    if not isinstance(result, dict):
        raise RuntimeError("YouTube Music returned an invalid liked-songs response")
    return result


# Matches upstream ytmusicapi/HTTP messages that indicate the embedded
# SAPISIDHASH or session cookies expired. Used to trigger one bounded
# rebuild+retry instead of misreporting a logged-in session as "unauthenticated".
_YTMUSIC_AUTH_FAIL_RE = re.compile(r"401|unauthorized|unauthenticated", re.I)
_ytmusic_auth_rebuild_lock = threading.Lock()
_ytmusic_auth_rebuild_last = 0.0
_YTMUSIC_AUTH_REBUILD_COOLDOWN = 60.0


def _maybe_rebuild_ytmusic_auth() -> bool:
    """Cooldown-bounded, single-flight auth rebuild. Regenerating headers.json
    refreshes the embedded SAPISIDHASH timestamp, which covers the long-session
    case where an otherwise-valid login slowly stops authenticating."""
    global _ytmusic_auth_rebuild_last
    with _ytmusic_auth_rebuild_lock:
        now = time.time()
        if now - _ytmusic_auth_rebuild_last < _YTMUSIC_AUTH_REBUILD_COOLDOWN:
            return False
        _ytmusic_auth_rebuild_last = now
    try:
        return bool(_rebuild_ytmusic_auth())
    except Exception as exc:
        log.warning(f"Implicit YouTube auth rebuild failed: {exc}")
        return False


@app.route("/api/youtube/liked_songs")
def api_youtube_liked_songs():
    """Fetch the user's liked songs from YouTube Music (requires auth)."""
    if not has_valid_auth_state():
        # cookies.txt can appear after startup (web login race, migration).
        # Give the cached state one bounded rebuild from the cookie file before
        # wrongly declaring the session unauthenticated.
        if not (_resolve_cookie_file() and _maybe_rebuild_ytmusic_auth() and has_valid_auth_state()):
            return jsonify({"error": "not_authenticated", "tracks": []}), 401
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        now = time.time()
        with _liked_lock:
            entry = _liked_cache.get(limit)
            if entry and (now - entry[0]) < _LIKED_TTL:
                return jsonify(entry[1])
        try:
            result = _fetch_youtube_likes_raw(limit)
        except Exception as first_err:
            # Auth-flavoured failures can come from a stale SAPISIDHASH on a
            # genuinely-logged-in session (playback keeps working because
            # streaming doesn't need SAPISIDHASH). Rebuild once and retry.
            if not (_YTMUSIC_AUTH_FAIL_RE.search(str(first_err)) and _maybe_rebuild_ytmusic_auth()):
                raise
            result = _fetch_youtube_likes_raw(limit)
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
        msg = str(e)
        log.warning(f"YouTube liked songs fetch failed: {msg}")
        if _YTMUSIC_AUTH_FAIL_RE.search(msg):
            return jsonify({"error": "not_authenticated", "tracks": []}), 401
        return jsonify({"error": msg, "transient": True, "tracks": []}), 500

@app.route("/api/playlists/delete", methods=["POST"])
def api_playlists_delete():
    data = request.get_json(force=True) or {}
    name = data.get("name", "")
    safe_name = _safe_playlist_name(name)
    if not safe_name or not _validate_playlist_path(safe_name):
        return jsonify({"error": "Invalid playlist name"}), 400
    try:
        with _playlist_catalog_lock:
            with _playlist_guard(safe_name):
                target = PLAYLISTS_DIR / safe_name
                if not target.exists():
                    return jsonify({"error": "Playlist not found"}), 404
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
        candidates = track.get("art_candidates") if isinstance(track.get("art_candidates"), list) else []
        track["art_candidates"] = list(dict.fromkeys(
            [current_art] + [str(url) for url in candidates if url] +
            ([f"https://i.ytimg.com/vi/{track['videoId']}/maxresdefault.jpg"] if track.get("videoId") else [])
            + ([f"https://i.ytimg.com/vi/{track['videoId']}/hqdefault.jpg"] if track.get("videoId") else [])
        ))[:6]
        track.setdefault("art_source", _art_source_hint(current_art))
    
    # Standardize Duration
    if not track.get("dur"):
        track["dur"] = _parse_duration(track)
    
    # Standardize Track ID
    if not track.get("tid"):
        track["tid"] = get_track_id(track["title"], track["artist_name"])
        
    return track

def resolve_artwork_batch(tracks: list[dict], force: bool = False) -> list[dict]:
    """Resolve artwork, recovering sparse metadata through recording matching."""
    normalized = [standardize_track(dict(track)) for track in tracks or [] if isinstance(track, dict)]
    fetcher = _ensure_artwork_fetcher()
    resolved = fetcher.fetch_artwork_batch(normalized, force=force)

    missing = [index for index, track in enumerate(resolved)
               if not (track.get("art") or track.get("album_art") or track.get("art_candidates"))]
    if not missing:
        return resolved

    def recover_context(index):
        track = normalized[index]
        recording = resolve_recording(
            track.get("name") or track.get("title") or "",
            track.get("artist") or track.get("artist_name") or "",
            track.get("dur") or track.get("duration"),
            track.get("album") or track.get("albumName") or "",
            track.get("albumId") or "",
        )
        if not recording:
            return index, None
        enriched = dict(track)
        enriched["videoId"] = recording.get("videoId") or enriched.get("videoId", "")
        enriched["album"] = enriched.get("album") or recording.get("album", "")
        enriched["albumId"] = enriched.get("albumId") or recording.get("albumId", "")
        enriched["dur"] = enriched.get("dur") or recording.get("duration") or 0
        if recording.get("art"):
            enriched["art"] = recording["art"]
            enriched["album_art"] = recording["art"]
            enriched["art_candidates"] = [recording["art"]]
            enriched["art_source"] = "youtube"
        enriched["recording_confidence"] = (
            recording.get("confidence") or (recording.get("_match") or {}).get("score") or 0
        )
        return index, standardize_track(enriched)

    futures = [_resolution_executor.submit(recover_context, index) for index in missing]
    recovered = []
    for future in as_completed(futures):
        try:
            index, track = future.result()
            if track:
                recovered.append((index, track))
        except Exception as exc:
            log.debug("Artwork recording-context recovery failed: %s", exc)
    if recovered:
        second_pass = fetcher.fetch_artwork_batch([track for _index, track in recovered], force=True)
        for (index, _track), result in zip(recovered, second_pass):
            resolved[index] = result
    return resolved

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    filter_type = request.args.get("filter", "all")
    _burst_check("search", f"{filter_type}:{q[:40]}")

    # Dispatch by filter. Artwork matching only applies to track-shaped results;
    # album and artist shapes keep their native provider images.
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
        # one shared ranked artwork pass with bounded provider concurrency.
        processed_results = resolve_artwork_batch(results)
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
    """Resolve radio suggestion tracks for a seed vid (watch playlist + ranked art),
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

    # 3. Shared ranked artwork resolution (deduplicated and concurrency-bounded)
    sanitized_tracks = resolve_artwork_batch(raw_list)

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
    duration = _coerce_duration_seconds(dur_str)
    tid = get_track_id(title, artist)
    identity = _lyrics_identity_signature(title, artist, duration)
    target = {"title": title, "artist": artist, "album": album, "duration": duration}

    # Cache v2 binds the entry to normalized identity + version tags + duration.
    # Older positive entries are deliberately re-evaluated instead of living forever.
    if not force:
        cached = _read_lyrics_cache(tid, identity)
        if cached:
            out = {k: v for k, v in cached.items() if k not in {"exp", "cacheVersion", "identity"}}
            return jsonify(out)

    def is_non_ascii(s):
        return any(ord(c) > 127 for c in s)

    def clean_query(q):
        # Provider queries drop presentation noise and feature credits but KEEP
        # material version markers. Removing "live" or "remix" here was a major
        # source of valid-title/wrong-recording matches.
        q = unicodedata.normalize("NFKC", q)
        q = q.replace(" & ", " and ")
        q = re.sub(r'[\(\[][Ff](?:eat|t)\.?.*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Ff]eaturing\s+.*?[\)\]]', '', q, flags=re.IGNORECASE)
        q = re.sub(r'[\(\[][Vv]s\..*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Ww]ith\s+.*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Oo]fficial.*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Ll]yric.*?[\)\]]', '', q)
        q = re.sub(r'[\(\[][Vv]ideo.*?[\)\]]', '', q)
        q = re.sub(r'\s+[Ff](?:eat|t)\.?\s+.*?(?=\s+-\s|$)', '', q)
        q = re.sub(r'\s+[Ff]eaturing\s+.*?(?=\s+-\s|$)', '', q, flags=re.IGNORECASE)
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

    full_query = f"{search_artist} {search_title}".strip()
    result = {"synced": False, "lines": []}

    def _parse_lrclib_response(data, strict=False):
        if not isinstance(data, dict) or data.get("instrumental"):
            return None
        candidate_identity = {
            "title": data.get("trackName") or (title if strict else ""),
            "artist": data.get("artistName") or (artist if strict else ""),
            "album": data.get("albumName") or "",
            "duration": data.get("duration"),
        }
        match = _score_track_candidate(target, candidate_identity)
        if data.get("syncedLyrics"):
            parsed = _parse_lrc(data["syncedLyrics"])
            if parsed:
                return {
                    "synced": True, "lines": parsed, "_source": "lrclib",
                    "_match": match, "_duration": data.get("duration"),
                    "_matchedTitle": candidate_identity["title"], "_matchedArtist": candidate_identity["artist"],
                }
        if data.get("plainLyrics"):
            return {
                "synced": False, "lines": [{"time": 0.0, "text": data["plainLyrics"]}],
                "_source": "lrclib", "_match": match, "_duration": data.get("duration"),
                "_matchedTitle": candidate_identity["title"], "_matchedArtist": candidate_identity["artist"],
            }
        return None

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
                return _parse_lrclib_response(r.json(), strict=True)
        except Exception as e:
            log.debug(f"LRCLIB strict lookup failed: {e}")
        return None

    def _fetch_syncedlyrics():
        # syncedlyrics returns no candidate metadata. It remains a useful long-tail
        # source, but receives a deliberately lower identity confidence and cannot
        # override a metadata-validated LRCLIB match.
        version_sensitive = bool(_track_version_profile(title)["tags"])
        proxy_score = 56 if version_sensitive else 72
        proxy_match = {"score": proxy_score, "acceptable": not version_sensitive}
        try:
            import syncedlyrics
            provider_groups = (
                ("syncedlyrics_em", ["NetEase", "Megalobiz"]),
                ("syncedlyrics_western", ["Musixmatch", "Deezer", "Genius", "Lyricsify", "LRCLIB"]),
            )
            for source, providers in provider_groups:
                try:
                    lrc = syncedlyrics.search(full_query, providers=providers)
                    if lrc and "[" in lrc and ":" in lrc:
                        parsed = _parse_lrc(lrc)
                        if parsed:
                            return {"synced": True, "lines": parsed, "_source": source, "_match": proxy_match}
                    if lrc and lrc.strip():
                        return {"synced": False, "lines": [{"time": 0.0, "text": lrc.strip()}], "_source": source, "_match": proxy_match}
                except Exception as e:
                    log.debug(f"{source} search failed: {e}")
        except Exception as e:
            log.debug(f"syncedlyrics import failed: {e}")
        return None

    t1_futures = [_io_executor.submit(_fetch_lrclib_strict)]
    if SYNCEDLYRICS_ENABLED:
        t1_futures.append(_io_executor.submit(_fetch_syncedlyrics))
    candidates = []
    try:
        for f in as_completed(t1_futures, timeout=8):
            res = f.result(timeout=8)
            if res:
                candidates.append(res)
    except Exception:
        # Provider work is best-effort; LRCLIB fuzzy search below remains available.
        pass

    # Metadata-bearing fuzzy candidates are scored before any source is chosen.
    try:
        r = _lrclib_session.get(
            "https://lrclib.net/api/search",
            params={"track_name": search_title, "artist_name": search_artist},
            timeout=4,
        )
        if r.ok:
            for item in (r.json() or [])[:20]:
                parsed = _parse_lrclib_response(item)
                if parsed:
                    candidates.append(parsed)
    except TimeoutError as e:
        log.warning(f"YouTube liked songs fetch timed out: {e}")
        return jsonify({"error": str(e), "tracks": []}), 504
    except Exception as e:
        log.debug(f"LRCLIB search fallback failed: {e}")

    best = _select_lyrics_candidate(candidates, duration)
    if best:
        out = {
            "synced": bool(best["synced"]), "lines": best["lines"],
            "source": best.get("_source", "unknown"),
            "confidence": best.get("_selectionScore", 0),
            "matchedTitle": best.get("_matchedTitle", ""),
            "matchedArtist": best.get("_matchedArtist", ""),
        }
        _write_lyrics_cache(tid, out, identity=identity)
        return jsonify(out)

    # YTMusic lyrics are tied to a video id. Preserve the selected video's exact
    # version; when absent, choose a scored candidate instead of result[0].
    if not vid:
        try:
            yt_results = ytmusic.search(full_query, filter="songs", limit=10)
            yt_candidates = []
            for item in yt_results or []:
                artists = item.get("artists") or []
                yt_candidates.append({
                    "title": item.get("title", ""),
                    "artist": artists[0].get("name", "") if artists else "",
                    "album": (item.get("album") or {}).get("name", ""),
                    "duration": item.get("duration_seconds") or item.get("duration"),
                    "videoId": item.get("videoId", ""),
                })
            ranked_yt = _rank_track_candidates(target, yt_candidates, minimum=68)
            if ranked_yt:
                vid = ranked_yt[0].get("videoId")
        except Exception as e:
            log.debug(f"YTMusic search for lyrics failed: {e}")
    if vid:
        try:
            watch_playlist = ytmusic.get_watch_playlist(videoId=vid)
            lyrics_id = watch_playlist.get("lyrics")
            if lyrics_id:
                lyrics_data = ytmusic.get_lyrics(lyrics_id)
                if lyrics_data.get("lyrics"):
                    yt_candidate = {
                        "synced": False,
                        "lines": [{"time": 0.0, "text": lyrics_data["lyrics"]}],
                        "_source": "ytmusic",
                        "_match": {"score": 90, "acceptable": True},
                    }
                    selected = _select_lyrics_candidate([yt_candidate], duration)
                    if selected:
                        out = {"synced": False, "lines": selected["lines"], "source": "ytmusic", "confidence": selected["_selectionScore"]}
                        _write_lyrics_cache(tid, out, identity=identity)
                        return jsonify(out)
        except Exception as e:
            log.debug(f"YTMusic lyrics fetch failed: {e}")

    _write_lyrics_cache(tid, result, neg_ttl=7 * 24 * 3600, identity=identity)
    return jsonify(result)

# Host whitelist for the word-lyric JSON proxy (see api_proxy_json below).
_JSON_PROXY_ALLOWED_HOSTS = {"music.163.com", "lrclib.net"}
_JSON_PROXY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@app.route("/api/proxy_json")
def api_proxy_json():
    """Same-origin JSON proxy for the per-word lyric fetcher.

    WebView2 enforces CORS and music.163.com sends no Access-Control-Allow-
    Origin header, so the renderer can never read NetEase responses directly.
    This route fetches a host-whitelisted URL server-side and returns the JSON.
    Used only by static/js/word-lyric-fetcher.js as a CORS workaround.
    """
    url = request.args.get("url", "")
    if len(url) > 512:
        return jsonify({"error": "url too long"}), 400
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return jsonify({"error": "bad url"}), 400
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _JSON_PROXY_ALLOWED_HOSTS:
        return jsonify({"error": "host not allowed"}), 403
    headers = {
        "User-Agent": _JSON_PROXY_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    referer = request.args.get("referer", "")
    if referer and len(referer) <= 256:
        headers["Referer"] = referer
    try:
        resp = requests.get(url, headers=headers, timeout=12)
    except requests.RequestException:
        return jsonify({"error": "upstream error"}), 502
    if resp.status_code != 200:
        return jsonify({"error": f"upstream status {resp.status_code}"}), resp.status_code
    try:
        return jsonify(resp.json())
    except ValueError:
        return jsonify({"error": "not json"}), 502

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
_COMMUNITY_DATA_DIR = BASE_DIR
_COMMUNITY_DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    authenticated = False
    user = {}
    try:
        s = _load_settings()
        authenticated = bool(s.get("github_token", ""))
        user = s.get("github_user", {}) or {}
    except Exception:
        pass
    return jsonify({
        "configured": bool(cid),
        "authenticated": authenticated,
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
#   installer to the shared AkiMelody data root's updates\ directory, then
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
_approved_update_assets = {}   # release tag -> asset metadata from GitHub check

# Installer cache shared by every launch mode and kept separate from code.
def _update_dir():
    """Return the shared private installer cache directory."""
    d = DATA_PATHS.updates
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

        # Only executable release assets can enter the install flow. The
        # browser never gets to nominate an arbitrary URL for execution.
        assets = data.get("assets") or []
        pickup = None
        for a in assets:
            name = (a.get("name") or "").lower()
            if name.endswith(".exe"):
                pickup = a
                break

        approved_asset = None
        asset_error = ""
        if pickup:
            asset_url = pickup.get("browser_download_url") or ""
            try:
                asset_size = int(pickup.get("size") or 0)
            except (TypeError, ValueError):
                asset_size = 0
            if not is_allowed_update_url(asset_url):
                asset_error = "untrusted_asset_url"
            elif asset_size <= 0 or asset_size > UPDATE_MAX_BYTES:
                asset_error = "invalid_asset_size"
            else:
                approved_asset = {
                    "name": pickup.get("name") or "AkiMelody-Setup.exe",
                    "url": asset_url,
                    "size": asset_size,
                    "release_notes": data.get("body") or "",
                    "release_html_url": data.get("html_url") or "",
                    "published_at": data.get("published_at") or "",
                    "approved_at": time.time(),
                }
                if remote_tag:
                    with _update_lock:
                        _approved_update_assets[remote_tag] = dict(approved_asset)
                        # A tiny bounded cache is enough for repeated checks and
                        # prevents stale releases accumulating indefinitely.
                        while len(_approved_update_assets) > 8:
                            oldest = min(
                                _approved_update_assets,
                                key=lambda tag: _approved_update_assets[tag].get("approved_at", 0),
                            )
                            _approved_update_assets.pop(oldest, None)

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
                "name": approved_asset["name"],
                "url": approved_asset["url"],
                "size": approved_asset["size"],
            } if approved_asset else None,
            "asset_error": asset_error,
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


def _download_worker(url, dest_path, asset_name, expected_size):
    """Background thread: streams `url` to `dest_path` with progress updates.
    Uses a fresh requests.Session so it never reuses auth cookies from
    YTMusic/iTunes sessions. Size and redirect targets are checked before the
    temporary file can become an executable installer."""
    global _update_thread
    sess = requests.Session()
    sess.headers.update({"User-Agent": "AkiMelody-Updater"})
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        with _update_lock:
            _update_state.update({
                "status": "downloading", "progress": 0, "received": 0,
                "total": 0, "asset_name": asset_name, "asset_url": url,
                "error": "", "local_path": str(dest_path),
            })
        # Stream to a temp file first so a partial download is never
        # mistaken for a complete installer on the next launch.
        with sess.get(url, stream=True, timeout=30, allow_redirects=True) as r:
            r.raise_for_status()
            if not is_allowed_update_url(r.url):
                raise ValueError("update_redirect_not_trusted")
            declared_size = int(r.headers.get("Content-Length") or 0)
            if declared_size > UPDATE_MAX_BYTES:
                raise ValueError("update_too_large")
            if expected_size and declared_size and declared_size != expected_size:
                raise ValueError("update_size_mismatch")
            total = expected_size or declared_size
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
                    if received > UPDATE_MAX_BYTES or (expected_size and received > expected_size):
                        raise ValueError("update_too_large")
                    with _update_lock:
                        _update_state["received"] = received
                    if total:
                        pct = int(received * 100 / total)
                        if pct != last_pct:
                            last_pct = pct
                            with _update_lock:
                                _update_state["progress"] = pct
            if expected_size and received != expected_size:
                raise ValueError("update_size_mismatch")
            # Rename .part → final only when the write completed and its size
            # matches the GitHub release metadata approved by update/check.
            tmp_path.replace(dest_path)
        with _update_lock:
            _update_state.update({
                "status": "ready", "progress": 100,
                "local_path": str(dest_path),
            })
    except Exception as e:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
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
    release_tag = str(body.get("release_tag") or "").strip()

    with _update_lock:
        cur = _update_state["status"]
        if cur == "downloading" and _update_thread and _update_thread.is_alive():
            return jsonify({"ok": True, "state": dict(_update_state), "already_running": True})
        if (cur == "ready" and _update_state.get("release_tag") == release_tag
                and _update_state.get("local_path")
                and is_trusted_installer(_update_state["local_path"], _update_dir())
                and Path(_update_state["local_path"]).exists()):
            return jsonify({"ok": True, "state": dict(_update_state), "already_ready": True})
        approved = dict(_approved_update_assets.get(release_tag) or {})

    if not approved:
        return jsonify({"ok": False, "error": "release_not_approved"}), 409

    # Stable filename: include the release tag so multiple releases' installers
    # coexist in the cache dir and the user can roll back manually.
    safe_tag = safe_filename_component(release_tag, "unknown")
    safe_name = safe_filename_component(approved.get("name"), "AkiMelody-Setup.exe")
    if not safe_name.lower().endswith(".exe"):
        return jsonify({"ok": False, "error": "invalid_installer_type"}), 409
    dest = _update_dir() / f"{safe_tag}_{safe_name}"
    if not is_trusted_installer(dest, _update_dir()):
        return jsonify({"ok": False, "error": "invalid_installer_path"}), 409

    with _update_lock:
        _update_state.update({
            "release_tag": release_tag,
            "release_notes": approved.get("release_notes") or "",
            "release_html_url": approved.get("release_html_url") or "",
        })
    _update_thread = threading.Thread(
        target=_download_worker,
        args=(approved["url"], dest, approved["name"], approved["size"]),
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
    installer to %TEMP%, then spawn a DETACHED batch wrapper that waits a
    short fixed grace period (3 s) before running the installer silently with
    /SILENT /CLOSEAPPLICATIONS /NORESTART. The grace window covers our own
    teardown; Inno's `CloseApplications=yes` (installer.iss) then handles any
    straggler AkiMelody.exe during install.

    We do NOT poll `tasklist | find /I "AkiMelody.exe"` for the process to
    vanish: that loops forever when the app is running as `python.exe` (via
    Launch.bat from source), since the visible name is `python.exe`, not
    `AkiMelody.exe`. The name-poll was the bug behind the cmd window stuck
    on `find /I "AkiMelody.exe"` doing nothing."""
    with _update_lock:
        path = _update_state.get("local_path") or ""
        status = _update_state.get("status")
    if (status != "ready" or not path
            or not is_trusted_installer(path, _update_dir())
            or not Path(path).is_file()):
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

        # Generate the wrapper batch (mirrors the native bridge's wrapper).
        bat_path = os.path.join(tmp_dir, "akimelody_update_flask.bat")
        log_path = os.path.join(tmp_dir, "akimelody_update.log")
        now = _dt.datetime.now().isoformat(timespec="seconds")
        bat = (
            "@echo off\r\n"
            f"echo [{now}] flask update wrapper start >> \"{log_path}\"\r\n"
            f"echo installer={tmp_installer} >> \"{log_path}\"\r\n"
            f"echo [{now}] grace wait 3s before install >> \"{log_path}\"\r\n"
            "timeout /t 3 /nobreak >nul\r\n"
            f"echo [{now}] running installer >> \"{log_path}\"\r\n"
            f'"{tmp_installer}" /SILENT /CLOSEAPPLICATIONS /NORESTART >> "{log_path}" 2>&1\r\n'
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
    if path and is_trusted_installer(path, _update_dir()):
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


_CHANGELOG_PATH = RESOURCE_DIR / "CHANGELOG.md"


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
    print(f"[AkiMelody] server listening on http://{SERVER_BIND_HOST}:{SERVER_PORT}", flush=True)
    app.run(host=SERVER_BIND_HOST, port=SERVER_PORT, debug=False, use_reloader=False)
