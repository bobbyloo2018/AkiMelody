"""AkiMelody — PyInstaller + Inno Setup build pipeline.

Produces a per-user, no-UAC Windows installer that packages the Python
backend + pywebview shell as a native --onedir bundle.

Why --onedir (not --onefile):
  --onefile unpacks EVERY asset to a fresh %TEMP%\\_MEIxxxxxx folder on each
  launch — Python DLLs, packages, templates, static, qjs.exe — which adds
  5-15 s of startup delay on every cold start. --onedir runs the exe directly
  out of {localappdata}\\AkiMelody, so the only cold-start cost is the Python
  interpreter itself.

Usage:
  python build.py              # build the --onedir bundle into dist/AkiMelody/
  python build.py --installer  # then run ISCC to compile installer.iss -> AkiMelody-Setup.exe

Prerequisites:
  pip install pyinstaller
  pip install -r requirements.txt
  Inno Setup 6 installed (ISCC on PATH) for the --installer step.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC_DIR = ROOT / "build"


def rm_rf(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def collect_datas():
    """Return a list of (src, dst_folder) tuples for PyInstaller --add-data.

    These are the non-Python assets the Flask backend + launcher need at
    runtime. In --onedir mode they are placed alongside the exe (or inside
    _internal, depending on PyInstaller version); app.py resolves them via
    sys._MEIPASS at frozen time.
    """
    datas = []
    # Flask templates + static
    if (ROOT / "templates").exists():
        datas.append((str(ROOT / "templates"), "templates"))
    if (ROOT / "static").exists():
        datas.append((str(ROOT / "static"), "static"))
    # QuickJS runtime (used by the qjs hint engine)
    qjs = ROOT / "build" / "qjs.exe"
    if qjs.exists():
        datas.append((str(qjs), "build"))
    # Default config files shipped with the app
    for cfg in ("cookies.txt", "headers.json"):
        p = ROOT / cfg
        if p.exists():
            datas.append((str(p), "."))
    # ytmusicapi translation files (.mo) — required at runtime by gettext.
    # Without these, frozen builds crash with "No translation file found for
    # domain: 'base'" on startup.
    try:
        import ytmusicapi as _yt
        yt_locales = Path(_yt.__file__).parent / "locales"
        if yt_locales.exists():
            datas.append((str(yt_locales), "ytmusicapi/locales"))
    except Exception:
        pass
    return datas


def build_onedir(skip_clean: bool = False) -> int:
    """Run PyInstaller in --onedir mode."""
    if not skip_clean:
        rm_rf(DIST / "AkiMelody")
        rm_rf(BUILD / "AkiMelody")

    datas = collect_datas()
    # PyInstaller --add-data separator is ; on Windows, : on POSIX.
    sep = ";" if os.name == "nt" else ":"

    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", "AkiMelody",
        # --onedir: one folder, fast cold start.
        "--onedir",
        # Don't show a console window for the GUI app.
        "--noconfirm",
        "--windowed",
        # App icon.
        "--icon", str(ROOT / "build" / "icon.ico"),
        # Clean build dir between runs.
        "--clean",
    ]
    for src, dst in datas:
        args += ["--add-data", f"{src}{sep}{dst}"]

    # Entry point is the pywebview launcher.
    args.append(str(ROOT / "webview_launcher.py"))

    print(f"[build] Running PyInstaller:\n  {' '.join(args)}\n")
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode != 0:
        print("[build] PyInstaller failed.", file=sys.stderr)
        return result.returncode

    # Verify the output exists.
    exe = DIST / "AkiMelody" / "AkiMelody.exe"
    if not exe.exists():
        # Newer PyInstaller may nest the exe under _internal. Find it.
        candidates = list((DIST / "AkiMelody").rglob("AkiMelody.exe"))
        if candidates:
            exe = candidates[0]
    if exe.exists():
        print(f"[build] OK -> {exe}")
    else:
        print("[build] WARNING: AkiMelody.exe not found in dist/AkiMelody/",
              file=sys.stderr)
    return 0


def build_installer() -> int:
    """Compile installer.iss with Inno Setup's ISCC."""
    iss = ROOT / "installer.iss"
    if not iss.exists():
        print(f"[installer] {iss} not found — skipping.", file=sys.stderr)
        return 1
    # Locate ISCC.exe (Inno Setup 6 default path).
    iscc = shutil.which("ISCC")
    if not iscc:
        cand = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
               "Inno Setup 6" / "ISCC.exe"
        if cand.exists():
            iscc = str(cand)
    if not iscc:
        print("[installer] ISCC not found on PATH. Install Inno Setup 6 first.",
              file=sys.stderr)
        return 1

    args = [iscc, str(iss)]
    print(f"[installer] Compiling:\n  {' '.join(args)}\n")
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode == 0:
        setup_exe = DIST / "AkiMelody-Setup.exe"
        if setup_exe.exists():
            print(f"[installer] OK -> {setup_exe}")
        else:
            print("[installer] Done (setup exe name may differ).")
    else:
        print("[installer] ISCC failed.", file=sys.stderr)
    return result.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="AkiMelody build pipeline.")
    ap.add_argument("--installer", action="store_true",
                    help="Also run ISCC to produce AkiMelody-Setup.exe after "
                         "the --onedir build.")
    ap.add_argument("--skip-clean", action="store_true",
                    help="Don't wipe dist/AkiMelody + build/AkiMelody first.")
    args = ap.parse_args()

    rc = build_onedir(skip_clean=args.skip_clean)
    if rc != 0:
        return rc
    if args.installer:
        rc = build_installer()
    return rc


if __name__ == "__main__":
    sys.exit(main())
