"""AkiMelody Flask Server — Standalone API Entry Point

Serves the API backend on port 5000. Useful for debugging the API without the
WebView2 shell. The shell (`webview_launcher.py`) embeds Flask in-process.
"""
import logging
import os
import signal
import sys

# When frozen by PyInstaller, __file__ resolves inside the temp
# extraction directory.  Pin the working directory to the folder
# that contains the executable so that relative paths (SAVED/,
# music_library/, favorites.json, etc.) resolve next to the binary.
if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))
else:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import app  # noqa: E402


def _handle_sigterm(_signum, _frame):
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    print(f"[AkiMelody] server listening on http://127.0.0.1:{port}", flush=True)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
