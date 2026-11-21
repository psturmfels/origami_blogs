#!/usr/bin/env python3
"""Convert every MHTML file in web_files/ to a PDF in pdfs/.

The conversion uses headless Chrome (or Chromium), because it renders MHTML
archives and their embedded data-URI images the same way the browser does.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "web_files"
DEFAULT_OUTPUT = ROOT / "pdfs"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
]

WINDOWS_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
]


def find_chrome(explicit: str | None) -> str:
    if explicit:
        if Path(explicit).is_file() or shutil.which(explicit):
            return explicit
        raise SystemExit(f"Chrome not found at: {explicit}")

    env_override = os.environ.get("CHROME") or os.environ.get("CHROME_PATH")
    if env_override:
        candidate = find_chrome(env_override)
        return candidate

    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found

    if os.name == "nt":
        for candidate in WINDOWS_CANDIDATES:
            if Path(candidate).is_file():
                return candidate

    raise SystemExit(
        "Could not find Chrome or Chromium. Install one, or pass --chrome /path/to/chrome."
    )


def output_name(source: Path, used: set[str]) -> str:
    name = source.stem + ".pdf"
    if name not in used:
        used.add(name)
        return name
    index = 2
    while f"{source.stem}-{index}.pdf" in used:
        index += 1
    name = f"{source.stem}-{index}.pdf"
    used.add(name)
    return name


def convert(
    source: Path,
    destination: Path,
    chrome: str,
    timeout: int,
) -> tuple[bool, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.pdf")
    temporary.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="mhtml-pdf-profile-") as profile:
        command = [
            chrome,
            "--headless",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--hide-scrollbars",
            "--mute-audio",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=10000",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={temporary}",
            source.resolve().as_uri(),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            temporary.unlink(missing_ok=True)
            return False, f"timed out after {timeout}s"
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            return False, str(exc)

    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = detail[-1] if detail else f"exit code {result.returncode}"
        return False, detail

    if not temporary.is_file() or temporary.stat().st_size < 100:
        temporary.unlink(missing_ok=True)
        return False, "Chrome did not produce a PDF"

    temporary.replace(destination)
    return True, f"{destination.stat().st_size // 1024} KB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"directory containing MHTML files (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"directory for generated PDFs (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument("--chrome", help="path to a Chrome/Chromium executable")
    parser.add_argument("--timeout", type=int, default=180, help="seconds per file")
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="number of files to convert in parallel (default: 1)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="leave PDFs that already exist untouched",
    )
    args = parser.parse_args()

    chrome = find_chrome(args.chrome)
    if not args.input.is_dir():
        raise SystemExit(f"Input directory not found: {args.input}")

    sources = sorted(
        (
            path
            for path in args.input.iterdir()
            if path.is_file() and path.suffix.lower() in {".mhtml", ".mht"}
        ),
        key=lambda path: path.name.lower(),
    )
    if not sources:
        raise SystemExit(f"No .mhtml or .mht files found in {args.input}")

    args.output.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    tasks = []
    for source in sources:
        destination = args.output / output_name(source, used_names)
        if args.skip_existing and destination.exists():
            print(f"skip  {source.name}")
            continue
        tasks.append((source, destination))

    print(f"Using Chrome: {chrome}")
    print(f"Converting {len(tasks)} file(s) from {args.input} to {args.output}\n")

    failures = 0
    print_lock = threading.Lock()

    def run(task):
        source, destination = task
        ok, detail = convert(source, destination, chrome, args.timeout)
        with print_lock:
            if ok:
                print(f"ok    {source.name} -> {destination.name} ({detail})")
            else:
                print(f"FAIL  {source.name}: {detail}", file=sys.stderr)
        return ok

    if args.jobs > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(run, tasks))
    else:
        results = [run(task) for task in tasks]

    failures = sum(1 for ok in results if not ok)
    print(f"\nDone. {len(results) - failures} PDF(s) written, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
