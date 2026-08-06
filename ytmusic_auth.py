"""ytmusic_auth.py — YouTube Music auth header generation, verification, and persistence.

Lifecycle:
  1. WebView2 captures session cookies after user logs in.
  2. build_auth_headers() converts cookies → ytmusicapi-compatible headers dict
     with a real SAPISIDHASH authorization value.
  3. save_auth() persists to %LocalAppData%/AkiMelody/ytmusic_auth.json
     (and headers.json for backward compat).
  4. verify_auth() performs a silent test query via ytmusicapi.
  5. clear_auth() removes all auth artefacts.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path


AUTH_FILENAME = "ytmusic_auth.json"
_COMPAT_FILENAME = "headers.json"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── Path resolution ─────────────────────────────────────────────────────────

def _data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", "")) / "AkiMelody"
    return Path(__file__).parent


def auth_path() -> Path:
    return _data_dir() / AUTH_FILENAME


def compat_path() -> Path:
    return _data_dir() / _COMPAT_FILENAME


# ── SAPISIDHASH ─────────────────────────────────────────────────────────────

def compute_sapisidhash(sapisid: str,
                         origin: str = "https://music.youtube.com") -> str:
    ts = int(time.time())
    raw = f"{ts} {sapisid} {origin}"
    h = hashlib.sha1(raw.encode("ascii")).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


# ── Cookie → Header conversion ─────────────────────────────────────────────

def cookies_to_header(cookies: list) -> str:
    """Semicolon-separated Cookie header value."""
    return "; ".join(
        f"{c['name']}={c['value']}"
        for c in cookies
        if c.get("name") and c.get("value") is not None
    )


def build_auth_headers(cookies: list,
                        user_agent: str | None = None) -> dict:
    """Build the full headers dict ytmusicapi expects.

    Returns a plain dict ready for json.dump — includes User-Agent,
    Cookie, Origin, X-Origin, and a real SAPISIDHASH authorization.
    """
    ua = user_agent or _USER_AGENT
    cookie_hdr = cookies_to_header(cookies)

    sapisid = next(
        (c["value"] for c in cookies if c.get("name") == "SAPISID"),
        None,
    )

    headers = {
        "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_hdr,
        "Origin": "https://music.youtube.com",
        "X-Origin": "https://music.youtube.com",
    }

    if sapisid:
        headers["authorization"] = compute_sapisidhash(sapisid)

    return headers


# ── Persistence ─────────────────────────────────────────────────────────────

def save_auth(headers: dict, path: str | None = None) -> str:
    """Write auth headers to disk.  Returns the primary path.

    Writes *both* ytmusic_auth.json (canonical) and headers.json (backward
    compat so the existing _init_ytmusic / _rebuild_ytmusic_auth code works
    without changes).
    """
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)

    primary = str(path or auth_path())
    compat = str(compat_path())

    for p in (primary, compat):
        try:
            Path(p).write_text(json.dumps(headers, indent=2), encoding="utf-8")
        except Exception:
            pass

    return primary


# ── Verification ────────────────────────────────────────────────────────────

def verify_auth(path: str | None = None) -> bool:
    """Silently test the auth file against YouTube Music.

    Calls YTMusic.get_library_songs(limit=1) — if it returns without error
    the credentials are valid.
    """
    target = str(path or auth_path())
    if not Path(target).exists():
        return False
    try:
        from ytmusicapi import YTMusic
        ytm = YTMusic(target)
        ytm.get_library_songs(limit=1)
        return True
    except Exception:
        return False


# ── Status / Cleanup ────────────────────────────────────────────────────────

def get_auth_status() -> dict:
    """Return {authenticated: bool, path: str, size: int}."""
    for p in (auth_path(), compat_path()):
        if p.exists() and p.stat().st_size > 10:
            return {"authenticated": True, "path": str(p),
                    "size": p.stat().st_size}
    return {"authenticated": False, "path": "", "size": 0}


def clear_auth():
    """Remove all auth files."""
    for p in (auth_path(), compat_path()):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
