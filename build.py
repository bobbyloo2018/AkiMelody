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
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC_DIR = BUILD / "spec"
WORK_DIR = BUILD / "pyinstaller"
ACTIVE_BUNDLE_DIR = DIST / "AkiMelody"
MYAPP_VERSION = "1.0.3"
FORBIDDEN_BUNDLE_FILES = {
    "cookies.txt",
    "headers.json",
    "ytmusic_auth.json",
    "favorites.json",
    "settings.json",
    "stats.json",
    "community_themes.json",
    "community_playlists.json",
}


def rm_rf(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def prepare_work_dir() -> Path:
    """Return a clean PyInstaller work directory.

    Windows can briefly retain a handle to ``localpycs`` after an interrupted
    build (and OneDrive can hold the directory while syncing).  PyInstaller's
    own ``--clean`` then fails before it can start.  Retry the normal path and
    fall back to a unique sibling directory when the stale path remains.
    """
    if not WORK_DIR.exists():
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        return WORK_DIR
    for _ in range(3):
        rm_rf(WORK_DIR)
        if not WORK_DIR.exists():
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            return WORK_DIR
        time.sleep(1)
    fallback = BUILD / f"pyinstaller-{os.getpid()}-{int(time.time())}"
    rm_rf(fallback)
    fallback.mkdir(parents=True, exist_ok=True)
    print(f"[build] Existing work directory is locked; using {fallback}")
    return fallback


def prepare_bundle_dir() -> Path:
    """Choose a writable PyInstaller output directory."""
    target = DIST / "AkiMelody"
    rm_rf(target)
    if not target.exists():
        return target
    fallback = Path(tempfile.gettempdir()) / f"AkiMelody-build-{os.getpid()}" / "AkiMelody"
    rm_rf(fallback.parent)
    fallback.parent.mkdir(parents=True, exist_ok=True)
    print(f"[build] Existing bundle is locked; using {fallback}")
    return fallback


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
    # Read-only release notes are displayed by /api/update/changelog. Mutable
    # config/auth files must never be bundled into a public installer.
    changelog = ROOT / "CHANGELOG.md"
    if changelog.exists():
        datas.append((str(changelog), "."))
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
    global ACTIVE_BUNDLE_DIR
    work_dir = WORK_DIR
    if not skip_clean:
        ACTIVE_BUNDLE_DIR = prepare_bundle_dir()
        work_dir = prepare_work_dir()
    SPEC_DIR.mkdir(parents=True, exist_ok=True)

    datas = collect_datas()
    # PyInstaller --add-data separator is ; on Windows, : on POSIX.
    sep = ";" if os.name == "nt" else ":"

    # Version info for Windows file properties (Details tab)
    version_file = ROOT / "build" / "version_info.txt"
    if not version_file.exists():
        # PyInstaller expects a text file with version info in VSVersionInfo format
        # Using the standard format recognized by PyInstaller's version parsing
        version_parts = MYAPP_VERSION.split('.')
        filevers = tuple(int(x) for x in version_parts + ['0'] * (4 - len(version_parts)))
        version_file.write_text(f"""
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers},
    prodvers={filevers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'AkiMelody'),
        StringStruct(u'FileDescription', u'AkiMelody Music Player'),
        StringStruct(u'FileVersion', u'{MYAPP_VERSION}'),
        StringStruct(u'InternalName', u'AkiMelody'),
        StringStruct(u'LegalCopyright', u'Copyright (C) 2024 AkiMelody'),
        StringStruct(u'OriginalFilename', u'AkiMelody.exe'),
        StringStruct(u'ProductName', u'AkiMelody'),
        StringStruct(u'ProductVersion', u'{MYAPP_VERSION}')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", "AkiMelody",
        # --onedir: one folder, fast cold start.
        "--onedir",
        # Don't show a console window for the GUI app.
        "--noconfirm",
        "--windowed",
        # yt-dlp-ejs is required for full YouTube signature solving. Its JS
        # assets are loaded dynamically and must be collected explicitly.
        "--collect-all", "yt_dlp_ejs",
        # Keep PyInstaller's generated machine-specific spec/work files out of
        # the repository root and away from build/qjs.exe.
        "--specpath", str(SPEC_DIR),
        "--workpath", str(work_dir),
        "--distpath", str(ACTIVE_BUNDLE_DIR.parent),
        # App icon.
        "--icon", str(ROOT / "build" / "icon.ico"),
        # Version info for Windows file properties.
        "--version-file", str(version_file),
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
    exe = ACTIVE_BUNDLE_DIR / "AkiMelody.exe"
    if not exe.exists():
        # Newer PyInstaller may nest the exe under _internal. Find it.
        candidates = list(ACTIVE_BUNDLE_DIR.rglob("AkiMelody.exe"))
        if candidates:
            exe = candidates[0]
    if exe.exists():
        print(f"[build] OK -> {exe}")
    else:
        print(f"[build] ERROR: AkiMelody.exe not found in {ACTIVE_BUNDLE_DIR}/",
              file=sys.stderr)
        return 1
    leaked = [
        path for path in ACTIVE_BUNDLE_DIR.rglob("*")
        if path.is_file() and path.name.lower() in FORBIDDEN_BUNDLE_FILES
    ]
    if leaked:
        print("[build] ERROR: private runtime data entered the application bundle:", file=sys.stderr)
        for path in leaked:
            print(f"  {path}", file=sys.stderr)
        return 1
    (BUILD / "last_bundle_path.txt").write_text(
        str(ACTIVE_BUNDLE_DIR), encoding="utf-8"
    )
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

    args = [iscc, f"/DBundleDir={ACTIVE_BUNDLE_DIR}", str(iss)]
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
                    help="Don't wipe dist/AkiMelody + build/pyinstaller first.")
    args = ap.parse_args()

    rc = build_onedir(skip_clean=args.skip_clean)
    if rc != 0:
        return rc
    if args.installer:
        rc = build_installer()
    return rc


if __name__ == "__main__":
    sys.exit(main())
