#!/usr/bin/env python3
"""
ACSM to EPUB Converter

Converts Adobe ACSM ebook tokens to DRM-free EPUB files
for personal offline reading.

Prerequisites (Docker handles these):
    libgourou (built from source)
"""

import argparse
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIBGOUROU_DIR = SCRIPT_DIR / "libgourou"
LIBGOUROU_BIN = LIBGOUROU_DIR / "utils"
ADEPT_DIR = Path.home() / ".config" / "adept"


def run(cmd, **kwargs):
    """Run a command and return the result."""
    defaults = {"capture_output": True, "text": True}
    defaults.update(kwargs)
    return subprocess.run(cmd, **defaults)


def find_tool(name):
    """Find a tool, checking local build directory first."""
    local = LIBGOUROU_BIN / name
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    system = shutil.which(name)
    if system:
        return system
    return None


def find_ebook_convert():
    """Find ebook-convert from Calibre (optional, for PDF→EPUB)."""
    cmd = shutil.which("ebook-convert")
    if cmd:
        return cmd
    app_cmd = "/Applications/calibre.app/Contents/MacOS/ebook-convert"
    if Path(app_cmd).exists():
        return app_cmd
    return None


# ─── Conversion ──────────────────────────────────────────────────────────


def detect_format(acsm_path):
    """Parse the ACSM file to detect if the download is EPUB or PDF."""
    tree = ET.parse(acsm_path)
    root = tree.getroot()
    ns = {"adept": "http://ns.adobe.com/adept"}

    src_elem = root.find(".//adept:src", ns)
    if src_elem is not None and src_elem.text:
        src = src_elem.text.lower()
        if ".pdf" in src or "output=pdf" in src:
            return "pdf"
        if ".epub" in src or "output=epub" in src:
            return "epub"

    return "epub"


def register_device():
    """Register an Adobe device (one-time setup)."""
    device_file = ADEPT_DIR / "device.xml"
    if device_file.exists():
        print("[OK] Adobe device already registered.")
        return

    print("Registering Adobe device (anonymous)...")
    tool = find_tool("adept_activate")
    if not tool:
        raise RuntimeError("adept_activate not found. libgourou not built.")
    try:
        result = run([tool, "-a"], timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Device registration timed out (30s).")
    if result.returncode != 0:
        raise RuntimeError(f"Device registration failed: {result.stdout}\n{result.stderr}")

    print("[OK] Adobe device registered.")


def fulfill_acsm(acsm_path, output_path):
    """Download the DRM-protected ebook by fulfilling the ACSM token."""
    print(f"Fulfilling ACSM: {acsm_path.name}")
    tool = find_tool("acsmdownloader")
    if not tool:
        raise RuntimeError("acsmdownloader not found. libgourou not built.")
    try:
        result = run([tool, "-f", str(acsm_path), "-o", str(output_path)], timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Download timed out (120s). The ACSM token may be expired or the server is unreachable.")
    if result.returncode != 0:
        stderr = result.stderr or result.stdout or ""
        print(f"ACSM fulfillment failed:\n{stderr}", flush=True)
        raise RuntimeError(f"ACSM download failed (exit code {result.returncode}): {stderr[:500]}")

    if not output_path.exists():
        raise RuntimeError(f"Download completed but output file not found. stdout: {result.stdout[:200]}")

    size_kb = output_path.stat().st_size / 1024
    print(f"[OK] Downloaded: {output_path.name} ({size_kb:.0f} KB)")


def remove_drm(input_path, output_path):
    """Remove DRM from the downloaded ebook."""
    print(f"Removing DRM: {input_path.name}")
    tool = find_tool("adept_remove")
    if not tool:
        raise RuntimeError("adept_remove not found. libgourou not built.")
    try:
        result = run([tool, "-f", str(input_path), "-o", str(output_path)], timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("DRM removal timed out (60s).")
    if result.returncode != 0:
        raise RuntimeError(f"DRM removal failed: {(result.stderr or result.stdout)[:300]}")

    print(f"[OK] DRM removed: {output_path.name}")


def convert_pipeline(acsm_path, output_dir):
    """Generator that yields (step, message) tuples for each conversion step.

    Used by both the CLI and the web interface.
    Raises RuntimeError on failure.

    Steps:
        1. Check tools
        2. Detect format
        3. Register device
        4. Download ebook
        5. Remove DRM
    """
    acsm_path = Path(acsm_path).resolve()
    if not acsm_path.exists():
        raise RuntimeError(f"File not found: {acsm_path}")
    if acsm_path.suffix != ".acsm":
        raise RuntimeError(f"Not an ACSM file: {acsm_path}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = acsm_path.stem

    # Step 1: Check tools
    problems = []
    for tool_name in ("acsmdownloader", "adept_activate", "adept_remove"):
        if not find_tool(tool_name):
            problems.append(f"{tool_name} not found (libgourou not built)")
    if problems:
        raise RuntimeError("Missing components: " + "; ".join(set(problems)))
    yield (1, "All tools ready.")

    # Step 2: Detect format
    fmt = detect_format(acsm_path)
    if fmt != "epub":
        raise RuntimeError(
            f"This ACSM file points to a {fmt.upper()} download. "
            "Only EPUB-sourced ACSM files are supported."
        )
    yield (2, f"Detected format: {fmt.upper()}")

    # Step 3: Register device
    register_device()
    yield (3, "Device registered.")

    # Step 4: Download
    drm_file = output_dir / f"{stem}_drm.{fmt}"
    fulfill_acsm(acsm_path, drm_file)
    yield (4, f"Downloaded: {drm_file.name}")

    # Step 5: Remove DRM
    clean_file = output_dir / f"{stem}.{fmt}"
    remove_drm(drm_file, clean_file)
    # Clean up DRM copy
    try:
        drm_file.unlink()
    except Exception:
        pass
    yield (5, f"DRM removed: {clean_file.name}")

    # Done
    size_mb = clean_file.stat().st_size / (1024 * 1024) if clean_file.exists() else 0
    yield ("done", f"Conversion complete! File: {clean_file.name} ({size_mb:.1f} MB)")


def do_convert(acsm_file, output_dir):
    """Run the ACSM conversion pipeline (CLI entry point)."""
    try:
        for step, message in convert_pipeline(acsm_file, output_dir):
            if step == "done":
                print(f"\n=== Done! ===\n{message}")
            else:
                print(f"\n=== Step {step}/5: {message} ===")
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Convert ACSM ebook tokens to DRM-free EPUB.",
    )
    parser.add_argument(
        "acsm_file",
        nargs="?",
        help="Path to the .acsm file to convert",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output",
        help="Output directory (default: output)",
    )
    args = parser.parse_args()

    if not args.acsm_file:
        parser.print_help()
        sys.exit(1)

    do_convert(args.acsm_file, args.output_dir)


if __name__ == "__main__":
    main()
