"""AkiMelody WebView2 Launcher — pywebview (EdgeChromium) shell.

Replaces the Electron shell with a single Python process that:
  1. Runs the Flask backend in a daemon thread on port 5000.
  2. Opens a frameless, transparent pywebview window at http://127.0.0.1:5000.
  3. Exposes a JS bridge (AkiApi) mirroring the old Electron IPC surface so the
     renderer's `window.__aki__` / `window.__TAURI__` shim keeps working.

Run in dev:  python webview_launcher.py
Bundle:      PyInstaller --onedir webview_launcher.py (templates/static bundled
             by app.py's frozen branch via _MEIPASS).
"""
import ctypes
import ctypes.wintypes as wt
import datetime as _dt
import email.utils
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

logger = logging.getLogger("akimelody.webview")

# ── Data dir resolution (mirror server.py + Electron getDataDir) ──────────────
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
    # Packaged: mirror Electron's userData (%LOCALAPPDATA%\AkiMelody) so the
    # WebView2 profile (localStorage/IndexedDB lyrics/cookies) survives updates
    # and stays out of the install dir (which may be non-writable).
    _DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or _BASE, "AkiMelody")
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
    _DATA_DIR = _BASE

# Cookies must live where app.py's BASE_DIR looks for them:
#   frozen → %LOCALAPPDATA%\AkiMelody\cookies.txt
#   dev    → <script_dir>\cookies.txt
_COOKIE_DIR = _DATA_DIR if getattr(sys, "frozen", False) else _BASE
os.makedirs(_COOKIE_DIR, exist_ok=True)
COOKIE_PATH = os.path.join(_COOKIE_DIR, "cookies.txt")
SERVER_PORT = 5000
APP_ID = "com.akimelody.app"
WEBVIEW_STORAGE_DIR = os.path.join(_DATA_DIR, "webview")

_AUTH_COOKIE_NAMES = ("SID", "SSID", "HSID", "__Secure-1PSID")


# ── Flask in a background thread ──────────────────────────────────────────────
# CSP: the renderer is a single inline <script> tag + eval, so
# 'unsafe-inline'/'unsafe-eval' are required; remote art/media flow through
# first-party proxies, and fonts come from Google/CDNJS (bootstrap, font-awesome).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
    "img-src 'self' app: data: blob: https:; "
    "media-src 'self' app: data: blob: https:; "
    "connect-src 'self' http://127.0.0.1:%d https:; "
    "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com;"
    % SERVER_PORT
)


def _start_flask():
    import app as backend

    @backend.app.after_request
    def _security_headers(resp):
        # Only harden the shell's own origin — never inject CSP into responses
        # consumed by the Electron fallback (it sets its own via webRequest).
        if resp.headers.get("Content-Security-Policy") is None:
            resp.headers["Content-Security-Policy"] = _CSP
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    _werk = logging.getLogger("werkzeug")
    _werk.setLevel(logging.ERROR)
    threading.Thread(
        target=backend.app.run,
        kwargs=dict(host="127.0.0.1", port=SERVER_PORT, debug=False, use_reloader=False),
        daemon=True,
        name="flask",
    ).start()


def _wait_for_server(timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", SERVER_PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ── Win32 helpers ─────────────────────────────────────────────────────────────
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_shell32 = ctypes.windll.shell32

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
HC_ACTION = 0

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("wFunc", wt.UINT),
        ("pFrom", wt.LPCWSTR),
        ("pTo", wt.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wt.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wt.LPCWSTR),
    ]


def set_power_save(playing):
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if playing else 0)
    try:
        _kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def _clipboard_write_text(text):
    try:
        _user32.OpenClipboard(0)
        _user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = _kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
        ptr = _kernel32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        _kernel32.GlobalUnlock(h)
        _user32.SetClipboardData(CF_UNICODETEXT, h)
        _user32.CloseClipboard()
        return True
    except Exception:
        try:
            _user32.CloseClipboard()
        except Exception:
            pass
        return False


def _recycle_bin(paths):
    if not paths:
        return 0
    from_list = "\x00".join(paths) + "\x00\x00"
    op = SHFILEOPSTRUCTW(
        hwnd=0,
        wFunc=FO_DELETE,
        pFrom=from_list,
        pTo=None,
        fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT,
        fAnyOperationsAborted=False,
        hNameMappings=None,
        lpszProgressTitle=None,
    )
    try:
        res = _shell32.SHFileOperationW(ctypes.byref(op))
        return len(paths) if res == 0 else 0
    except Exception:
        return 0


def _accent_from_registry():
    """Read Windows accent color (ABGR DWORD) → {r,g,b,hex} or None."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as k:
            val, _ = winreg.QueryValueEx(k, "AccentColor")
        abgr = val & 0x00FFFFFF
        r = abgr & 0xFF
        g = (abgr >> 8) & 0xFF
        b = (abgr >> 16) & 0xFF
        return {"r": str(r), "g": str(g), "b": str(b), "hex": "#%02x%02x%02x" % (r, g, b)}
    except Exception:
        return None


def _reduced_motion():
    """SPI_GETCLIENTAREAANIMATION → True when animations are disabled."""
    try:
        val = wt.BOOL(True)
        _user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(val), 0)
        return not bool(val.value)
    except Exception:
        return False


def _read_system_appearance():
    theme = "dark"
    try:
        import darkdetect

        theme = "dark" if darkdetect.isDark() else "light"
    except Exception:
        pass
    return {
        "theme": theme,
        "accent": _accent_from_registry(),
        "reducedMotion": _reduced_motion(),
    }


# ── Autostart (registry Run key) ──────────────────────────────────────────────
_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "AkiMelody"


def _autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def get_autostart():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, _AUTOSTART_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_autostart(enabled):
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            if enabled:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return get_autostart()
    except Exception:
        return False


# ── Track file resolution (SAVED + music_library) ─────────────────────────────
def _saved_dir():
    return os.path.join(_BASE, "SAVED")


def _resolve_local_files(tid, exts):
    """Return all on-disk paths matching tid across SAVED + music_library."""
    out = []
    if not re.match(r"^[A-Za-z0-9 _\-]+$", tid or ""):
        return out
    saved = _saved_dir()
    for e in exts:
        p = os.path.join(saved, f"{tid}.{e}")
        if os.path.exists(p):
            out.append(p)
    ml = os.path.join(_BASE, "music_library")
    if os.path.isdir(ml):
        for pl in os.listdir(ml):
            pl_dir = os.path.join(ml, pl)
            if not os.path.isdir(pl_dir):
                continue
            for e in exts:
                p = os.path.join(pl_dir, f"{tid}.{e}")
                if os.path.exists(p):
                    out.append(p)
    return out


# ── Media-key low-level keyboard hook ─────────────────────────────────────────
class MediaKeyHook:
    def __init__(self, on_action):
        self._on_action = on_action
        self._proc = None
        self._hook = None
        self._thread = None
        self._stop_event = threading.Event()

    def _callback(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and wParam == WM_KEYDOWN:
            kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kbd.vkCode
            # Swallow the key (return non-zero): if we let it pass through,
            # WebView2's navigator.mediaSession handlers (player.html) fire as
            # well when the window is focused -> double toggle/skip.
            if vk == VK_MEDIA_PLAY_PAUSE:
                self._on_action("toggle")
                return 1
            elif vk == VK_MEDIA_NEXT_TRACK:
                self._on_action("next")
                return 1
            elif vk == VK_MEDIA_PREV_TRACK:
                self._on_action("prev")
                return 1
        return _user32.CallNextHookEx(self._hook, nCode, wParam, lParam)

    def start(self):
        def _run():
            self._proc = ctypes.WINFUNCTYPE(
                wt.LPARAM, ctypes.c_int, wt.WPARAM, wt.LPARAM
            )(self._callback)
            self._hook = _user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0
            )
            if not self._hook:
                return
            msg = wt.MSG()
            while not self._stop_event.is_set():
                r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
            _user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

        self._thread = threading.Thread(target=_run, daemon=True, name="media-keys")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        _user32.PostThreadMessageW(self._thread.ident, 0x0012, 0, 0)  # WM_QUIT


# ── JS bridge (pywebview js_api → window.pywebview.api) ──────────────────────
class AkiApi:
    """Mirrors the old Electron preload surface. Methods exposed to the
    renderer as window.pywebview.api.<method>. All return JSON-serializable."""

    def __init__(self):
        self._window = None
        self._power_save = False
        self._media_hook = None
        self._last_appearance = None
        self._theme_thread = None

    # ── window controls (via __TAURI__.core.invoke) ──────────────────────
    def window_minimize(self):
        if self._window:
            try:
                self._window.minimize()
            except Exception:
                pass
        return True

    def window_toggle_maximize(self):
        if self._window:
            try:
                self._window.maximize() if not _is_maximized(self._window) else self._window.restore()
            except Exception:
                pass
        return True

    def window_close(self):
        if self._window:
            try:
                self._window.destroy()
            except Exception:
                pass
        return True

    # ── YouTube auth (via __TAURI__.core.invoke) ─────────────────────────
    def get_youtube_auth_status(self):
        try:
            import ytmusic_auth as yauth
            st = yauth.get_auth_status()
            return st
        except Exception:
            exists = os.path.exists(COOKIE_PATH) and os.path.getsize(COOKIE_PATH) > 10
            size = os.path.getsize(COOKIE_PATH) if os.path.exists(COOKIE_PATH) else 0
            return {"authenticated": exists, "size": size, "path": COOKIE_PATH}

    def unlink_youtube_account(self):
        try:
            import ytmusic_auth as yauth
            yauth.clear_auth()
        except Exception:
            pass
        if os.path.exists(COOKIE_PATH):
            try:
                os.remove(COOKIE_PATH)
                return "YouTube account unlinked"
            except Exception as e:
                return f"Failed to unlink: {e}"
        return "No YouTube account was linked"

    def _extract_all_cookies(self, window):
        """Extract all cookies from the WebView2 cookie store via pywebview's
        get_cookies().  Returns a list of plain dicts with name/value/domain."""
        try:
            raw = window.get_cookies() or []
            cookies = []
            for sc in raw:
                for _name, morsel in sc.items():
                    cookies.append({
                        "name": morsel.key,
                        "value": morsel.value,
                        "domain": morsel.get("domain", ""),
                        "path": morsel.get("path", "/"),
                        "secure": morsel.get("secure", False),
                        "httponly": morsel.get("httponly", False),
                    })
            return cookies
        except Exception:
            return []

    def start_youtube_login(self):
        """Sign into YouTube Music by navigating the main window through
        Google's Sign-In page.  Afterwards the session cookies are written
        to cookies.txt, the backend rebuilds auth headers, and the UI is
        refreshed via JavaScript push.  Same behaviour dev ↔ built."""
        if not self._window:
            return {"ok": False, "reason": "no-window"}
        threading.Thread(target=self._login_flow, daemon=True, name="yt-login").start()
        return {"ok": True}

    def _login_flow(self):
        """Navigate main window to Google sign-in → poll for music.youtube.com
        redirect → collect cookies from both youtube.com and google.com
        domains → return to the app → rebuild auth on backend."""
        w = self._window
        signin_url = ("https://accounts.google.com/ServiceLogin"
                      "?service=youtube"
                      "&continue=https%3A%2F%2Fmusic.youtube.com%2F"
                      "&passive=false")

        try:
            w.load_url(signin_url)
        except Exception:
            return

        # ── 1. Wait for the user to sign in / Google to redirect ──────────
        deadline = time.time() + 300
        signed_in = False
        while time.time() < deadline:
            time.sleep(1)
            try:
                href = w.evaluate_js("window.location.href") or ""
            except Exception:
                break
            # Once we leave accounts.google.com AND land on any youtube.com
            # subdomain the sign-in is complete.
            if "youtube.com" in href and "accounts.google.com" not in href:
                signed_in = True
                break

        if not signed_in:
            self._login_fail("YouTube login timed out. Please try again.")
            return

        time.sleep(2)

        # ── 2. Extract youtube.com-domain cookies ─────────────────────────
        #     (VISITOR_INFO1_LIVE, LOGIN_INFO, etc.)
        cookies = self._extract_all_cookies(w)

        # ── 3. Navigate to accounts.google.com for google.com-domain cookies
        #     (SAPISID, SID, HSID, etc.) — the user sees the window briefly
        #     show the accounts page (they are already signed in).
        try:
            w.load_url("https://accounts.google.com")
            time.sleep(3)
            more = self._extract_all_cookies(w)
            seen = {c["name"] for c in cookies}
            for c in more:
                if c["name"] not in seen:
                    cookies.append(c)
                    seen.add(c["name"])
        except Exception:
            pass

        # ── 4. Write cookies.txt ──────────────────────────────────────────
        _write_netscape_cookies(COOKIE_PATH, cookies)
        names = [c["name"] for c in cookies]
        print(f"[YT-LOGIN] Wrote {len(cookies)} cookies to {COOKIE_PATH} "
              f"({os.path.getsize(COOKIE_PATH)} bytes): "
              f"SAPISID={'Y' if 'SAPISID' in names else 'N'} "
              f"HSID={'Y' if 'HSID' in names else 'N'} "
              f"SID={'Y' if 'SID' in names else 'N'} "
              f"SSID={'Y' if 'SSID' in names else 'N'} "
              f"__Secure-1PSID={'Y' if '__Secure-1PSID' in names else 'N'}",
              flush=True)

        # ── 5. Return to the app ──────────────────────────────────────────
        try:
            w.load_url(f"http://127.0.0.1:{SERVER_PORT}")
            time.sleep(3)  # let the page render
        except Exception:
            pass

        # ── 6. Rebuild auth headers on backend, then notify the frontend ──
        try:
            import urllib.request
            req = urllib.request.Request(
                f"http://127.0.0.1:{SERVER_PORT}/api/youtube/refresh_auth",
                method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

        time.sleep(1)
        try:
            w.evaluate_js(
                "if(typeof showNotification==='function')"
                "showNotification('YouTube account linked!', 4000);"
                "if(typeof checkYouTubeAuthStatus==='function')"
                "checkYouTubeAuthStatus();"
            )
        except Exception:
            pass

    def _login_fail(self, msg):
        """Navigate back to app and show an error notification."""
        w = self._window
        try:
            w.load_url(f"http://127.0.0.1:{SERVER_PORT}")
            time.sleep(2)
        except Exception:
            pass
        try:
            w.evaluate_js(
                f"if(typeof showNotification==='function')"
                f"showNotification('{msg}', 5000);"
            )
        except Exception:
            pass

    # ── appearance / autostart (via __aki__) ─────────────────────────────
    def get_system_appearance(self):
        return _read_system_appearance()

    def get_autostart(self):
        return get_autostart()

    def set_autostart(self, enabled):
        return set_autostart(bool(enabled))

    # ── playback → power save ────────────────────────────────────────────
    def notify_playback_state(self, is_playing):
        set_power_save(bool(is_playing))
        return True

    # ── cache clean (best-effort; WebView2 cache is small) ───────────────
    def clear_cache(self, opts=None):
        return {"ok": True}

    # ── show in folder / recycle bin ─────────────────────────────────────
    def show_file_in_folder(self, tid):
        files = _resolve_local_files(tid, ("mp3", "m4a", "webm", "opus", "jpg"))
        if files:
            try:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(files[0])])
                return {"ok": True, "path": files[0]}
            except Exception:
                return {"ok": False, "reason": "explorer-failed"}
        saved = _saved_dir()
        if os.path.isdir(saved):
            try:
                subprocess.Popen(["explorer", os.path.normpath(saved)])
                return {"ok": True, "path": saved, "openedFolder": True}
            except Exception:
                pass
        return {"ok": False, "reason": "not-downloaded"}

    def trash_file(self, tid):
        files = _resolve_local_files(tid, ("mp3", "m4a", "webm", "opus", "jpg"))
        n = _recycle_bin(files)
        return {"ok": n > 0, "trashed": n}

    # ── clipboard ────────────────────────────────────────────────────────
    def clipboard_write(self, args=None):
        args = args or {}
        typ = args.get("type", "text")
        url = args.get("url") or ""
        title = args.get("title") or args.get("text") or ""
        payload = f"{title}\n{url}".strip() if url else (title or "")
        ok = _clipboard_write_text(payload)
        return {"ok": ok}

    # ── native file drag-out (not supported in WebView2) ────────────────
    def start_drag(self, tid=None, icon=None):
        return {"ok": False, "reason": "unsupported"}

    # ── tray (not supported in pywebview) ────────────────────────────────
    def tray_set_art(self, data_url=""):
        return True

    def tray_enable(self):
        return {"ok": False}

    def tray_disable(self):
        return {"ok": True}

    def tray_is_supported(self):
        return {"supported": False}

    # ── stream URL cache (no-op — webview uses Flask URLs) ──────────────
    def register_stream_url(self, tid=None, url=None):
        return {"ok": True}

    # ── WebView navigation helper (OAuth flows) ──────────────────────────
    def open_in_webview(self, url):
        """Navigate the app window to an external URL (used for GitHub OAuth
        consent screen). The page loads inside the existing webview."""
        if not self._window:
            return {"ok": False, "reason": "no-window"}
        try:
            self._window.load_url(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_external(self, url):
        """Open a URL in the system default browser (not the webview).
        Used for OAuth flows where we need to keep the app visible."""
        import webbrowser
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Auto-updater: silent installer launch + graceful app quit ──────────
    # The updater downloads an Inno Setup .exe into %LOCALAPPDATA%\AkiMelody\
    # updates\. To install without UAC and without file-lock errors we:
    #   1. Copy the installer to %TEMP%\AkiMelody-Setup.exe (so it runs from
    #      outside the install dir — the installer will overwrite {app}).
    #   2. Spawn `cmd.exe /c timeout /t 2 /nobreak && "<TEMP>\AkiMelody-Setup.exe" /SILENT`
    #      as a DETACHED process. The 2-second delay gives the Python backend
    #      time to exit and release its file locks on the install dir BEFORE
    #      the installer tries to replace them. No REBOOTOK, no reboot.
    #   3. Quit the app immediately after spawning the detached cmd.
    # This keeps the whole flow per-user (no UAC) and avoids locked-file
    # errors without forcing a reboot.
    def launch_installer(self, path=None, silent=True):
        """Launch the downloaded AkiMelody-Setup.exe for a silent update.

        Copies the installer to %TEMP% and spawns it via a detached
        `cmd.exe /c timeout /t 2 /nobreak && "<TEMP>" /SILENT` so the Python
        backend can exit and release file locks before the installer runs.
        Returns `{ok, pid, path}` or an error dict."""
        import tempfile as _tf
        if not path:
            try:
                r = requests.get(f"http://127.0.0.1:{SERVER_PORT}/api/update/status", timeout=3)
                if r.ok:
                    path = (r.json() or {}).get("local_path")
            except Exception:
                path = None
        if not path or not os.path.isfile(path):
            return {"ok": False, "reason": "installer_not_found", "path": path or ""}

        # Copy the installer to %TEMP% so it runs from outside the install dir.
        tmp_dir = _tf.gettempdir()
        tmp_path = os.path.join(tmp_dir, "AkiMelody-Setup.exe")
        try:
            import shutil as _shutil
            _shutil.copy2(path, tmp_path)
        except Exception as e:
            # Fall back to launching from the original location.
            tmp_path = path

        # Build the command: wait 2s, then run the installer silently.
        # /SILENT shows a progress bar but no wizard; /VERYSILENT hides everything.
        # We use /SILENT so the user sees *something* is happening.
        flag = "/VERYSILENT" if silent else "/SILENT"
        # Pass /DIR= so the installer always targets the current install dir
        # (read from HKCU\Software\AkiMelody\InstallPath at runtime).
        cmd = f'cmd.exe /c timeout /t 2 /nobreak && "{tmp_path}" {flag}'
        try:
            # DETACHED + CREATE_NEW_PROCESS_GROUP so the cmd survives after we quit.
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            proc = subprocess.Popen(
                cmd,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
                shell=False,
            )
            # Now quit the app so the installer can overwrite the install dir.
            # We do this on a short timer so the bridge response can still reach JS.
            threading.Timer(0.5, self.quit_app).start()
            return {"ok": True, "pid": proc.pid, "path": tmp_path, "flag": flag}
        except Exception as e:
            logger.warning(f"launch_installer failed: {e}")
            return {"ok": False, "reason": "exception", "error": str(e)}

    def quit_app(self):
        """Graceful shutdown: destroy the webview window. pywebview's
        webview.destroy() unblocks webview.start() which lets Python exit.
        Safe to call from JS — runs on the webview main thread via the bridge."""
        try:
            if self._window:
                try: self._window.destroy()
                except Exception: pass
                self._window = None
        except Exception:
            pass
        # Fallback: hard-exit if destroy didn't unblock (defensive).
        threading.Thread(target=lambda: (time.sleep(1.0), os._exit(0)), daemon=True).start()
        return {"ok": True}

    # ── push-in event dispatchers (called from background threads) ──────
    def _push(self, js):
        if not self._window:
            return
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass

    def _dispatch_theme(self):
        info = _read_system_appearance()
        if info == self._last_appearance:
            return
        self._last_appearance = info
        payload = json.dumps(info).replace("'", "\\'")
        self._push(f"window.__akiPush && window.__akiPush('nativeThemeChanged', '{payload}')")

    def _dispatch_media_key(self, action):
        self._push(f"window.__akiPush && window.__akiPush('mediaKey', '{action}')")

    # ── background threads ───────────────────────────────────────────────
    def _start_background(self):
        self._last_appearance = _read_system_appearance()

        def _theme_loop():
            while True:
                time.sleep(2)
                try:
                    self._dispatch_theme()
                except Exception:
                    pass

        threading.Thread(target=_theme_loop, daemon=True, name="theme-watch").start()

        self._media_hook = MediaKeyHook(self._dispatch_media_key)
        try:
            self._media_hook.start()
        except Exception:
            self._media_hook = None


def _is_maximized(window):
    """Best-effort: pywebview winforms exposes native form via gui; fall back
    to tracking via evaluate_js on the current window state is not available,
    so we compare size to screen work area."""
    try:
        import webview
        from webview.platforms import winforms

        bv = winforms.BrowserView.instances.get(window.uid)
        if bv is not None:
            return bool(bv.WindowState == 2)  # FormWindowState.Maximized
    except Exception:
        pass
    return False


def _write_netscape_cookies(path, cookies):
    lines = ["# Netscape HTTP Cookie File", "# Generated by AkiMelody", ""]
    for c in cookies:
        domain = c.get("domain") or ""
        if not domain:
            continue
        inc_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path2 = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = 0
        expires = c.get("expires")
        if expires and isinstance(expires, str):
            try:
                exp = int(email.utils.parsedate_to_datetime(expires).timestamp())
            except Exception:
                exp = 0
        lines.append(f"{domain}\t{inc_sub}\t{path2}\t{secure}\t{exp}\t{c.get('name')}\t{c.get('value')}")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


# ── Single-instance lock ──────────────────────────────────────────────────────
def _acquire_single_instance():
    try:
        handle = _kernel32.CreateMutexW(None, False, APP_ID)
        if _kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info("[AkiMelody] Starting WebView2 shell...")

    if not _acquire_single_instance():
        print("[AkiMelody] Already running (single instance).")
        return 0

    _start_flask()
    if not _wait_for_server():
        print("[AkiMelody] ERROR: Flask did not start on port %d" % SERVER_PORT)
        return 1
    logger.info("[AkiMelody] Flask ready on :%d", SERVER_PORT)

    import webview

    # Custom drag region = the renderer's .title-bar-drag-zone (WebView2 does
    # not honour Electron's -webkit-app-region CSS).
    webview.settings["DRAG_REGION_SELECTOR"] = ".title-bar-drag-zone"
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = False

    api = AkiApi()
    window = webview.create_window(
        "AkiMelody",
        f"http://127.0.0.1:{SERVER_PORT}",
        js_api=api,
        width=1300,
        height=850,
        min_size=(900, 600),
        frameless=True,
        transparent=True,
        easy_drag=False,
        shadow=False,
        text_select=False,
        zoomable=False,
    )
    if window is None:
        logger.error("pywebview failed to create the window.")
        return 1

    api._window = window
    api._start_background()

    try:
        # Replicate Electron's webPreferences parity in the WebView2 build:
        #   * backgroundThrottling:false          -> keep timers/rAF running when
        #     the window is hidden (visualizer, ontimeupdate, standby clock)
        #   * --js-flags=--max-old-space-size=288 --expose-gc -> renderer memory
        #     cap + the window.gc() the renderer calls on visibilitychange.
        # WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS is appended by the loader to the
        # args pywebview already sets (--disable-features=ElasticOverscroll).
        _extra_args = (
            "--disable-background-timer-throttling "
            "--disable-backgrounding-occluded-windows "
            "--disable-renderer-backgrounding "
            "--js-flags=--max-old-space-size=288 --expose-gc"
        )
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = _extra_args

        # private_mode=False + storage_path keeps localStorage (theme/volume/
        # queue), IndexedDB (lyrics cache) and cookies across launches — the
        # pywebview default private_mode=True puts storage in a tempdir that is
        # deleted on close.
        webview.start(private_mode=False, storage_path=WEBVIEW_STORAGE_DIR)
    finally:
        os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
        set_power_save(False)
        if api._media_hook:
            try:
                api._media_hook.stop()
            except Exception:
                pass
        logger.info("[AkiMelody] Shell closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
