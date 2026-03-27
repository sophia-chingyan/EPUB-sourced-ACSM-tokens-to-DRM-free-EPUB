#!/usr/bin/env python3
"""
ACSM to EPUB Converter

Converts Adobe ACSM ebook tokens to DRM-free EPUB files
for personal offline reading. Only supports EPUB-sourced ACSM files.

Prerequisites (installed automatically by setup):
    brew install pugixml libzip openssl curl cmake
    libgourou (built from source)

Usage:
    python3 converter.py --setup          # First-time setup
    python3 converter.py ebook.acsm       # Convert an ACSM file
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

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


# ─── Setup ───────────────────────────────────────────────────────────────


def setup_brew_deps():
    """Install build dependencies via Homebrew."""
    if not shutil.which("brew"):
        print("Homebrew is required. Install from https://brew.sh")
        sys.exit(1)

    deps = ["pugixml", "libzip", "openssl", "curl", "cmake"]
    print(f"Installing build dependencies: {', '.join(deps)}")
    result = run(["brew", "install"] + deps)
    if result.returncode != 0:
        print(f"brew install failed:\n{result.stderr}")
        sys.exit(1)
    print("[OK] Build dependencies installed.")


def _get_brew_prefixes():
    """Get Homebrew prefix paths for dependencies."""
    prefixes = {}
    for dep in ["pugixml", "libzip", "openssl", "curl"]:
        r = run(["brew", "--prefix", dep])
        prefixes[dep] = r.stdout.strip() if r.returncode == 0 else f"/opt/homebrew/opt/{dep}"
    return prefixes


def _patch_makefiles(brew_prefixes):
    """Patch libgourou Makefiles for macOS compatibility."""
    include_flags = " ".join(f"-I{p}/include" for p in brew_prefixes.values())
    lib_flags = " ".join(f"-L{p}/lib" for p in brew_prefixes.values())

    root_mk = LIBGOUROU_DIR / "Makefile"
    content = root_mk.read_text()
    content = content.replace(
        "$(AR) rcs --thin $@ $^",
        "libtool -static -o $@ $^",
    )
    root_mk.write_text(content)

    utils_mk = LIBGOUROU_DIR / "utils" / "Makefile"
    content = utils_mk.read_text()
    content = content.replace(
        "CXXFLAGS=-Wall -fPIC -I$(ROOT)/include",
        f"CXXFLAGS=-Wall -fPIC -I$(ROOT)/include {include_flags}",
    )
    content = content.replace(
        "LDFLAGS += -L$(ROOT) -lcrypto",
        f"LDFLAGS += -L$(ROOT) {lib_flags} -lcrypto",
    )
    utils_mk.write_text(content)


def build_libgourou():
    """Clone and build libgourou from source."""
    if (LIBGOUROU_BIN / "acsmdownloader").exists():
        print("[OK] libgourou already built.")
        return

    repo_url = "https://forge.soutade.fr/soutade/libgourou.git"

    if not LIBGOUROU_DIR.exists():
        print("Cloning libgourou...")
        result = run(["git", "clone", "--recurse-submodules", repo_url, str(LIBGOUROU_DIR)])
        if result.returncode != 0:
            print(f"Clone failed:\n{result.stderr}")
            sys.exit(1)

    brew_prefixes = _get_brew_prefixes()
    include_flags = " ".join(f"-I{p}/include" for p in brew_prefixes.values())

    print("Patching Makefiles for macOS...")
    _patch_makefiles(brew_prefixes)

    print("Building libgourou...")
    env = os.environ.copy()
    env["CXXFLAGS"] = include_flags

    result = run(
        ["make", "BUILD_UTILS=1", "BUILD_STATIC=1", "BUILD_SHARED=0"],
        cwd=str(LIBGOUROU_DIR),
        env=env,
    )
    if result.returncode != 0:
        print(f"Build failed:\n{result.stdout}\n{result.stderr}")
        print("\nTry installing missing deps: brew install pugixml libzip openssl curl")
        sys.exit(1)

    if not (LIBGOUROU_BIN / "acsmdownloader").exists():
        print("Build completed but binaries not found.")
        print(f"Check {LIBGOUROU_BIN} for build output.")
        sys.exit(1)

    print("[OK] libgourou built successfully.")


def do_setup():
    """Run full first-time setup."""
    print("=== Setting up ACSM Converter ===\n")
    setup_brew_deps()
    print()
    build_libgourou()
    print("\n=== Setup complete! ===")
    print("You can now convert ACSM files:")
    print("  python3 converter.py ebook.acsm")


# ─── Conversion ──────────────────────────────────────────────────────────


def detect_format(acsm_path):
    """Parse the ACSM file and raise an error if it is not EPUB."""
    tree = ET.parse(acsm_path)
    root = tree.getroot()
    ns = {"adept": "http://ns.adobe.com/adept"}

    src_elem = root.find(".//adept:src", ns)
    if src_elem is not None and src_elem.text:
        src = src_elem.text.lower()
        if ".pdf" in src or "output=pdf" in src:
            raise RuntimeError(
                "This ACSM file points to a PDF ebook. "
                "Only EPUB-sourced ACSM files are supported."
            )

    # Also check the metadata/format element
    fmt_elem = root.find(".//adept:metadata/adept:format", ns)
    if fmt_elem is not None and fmt_elem.text:
        fmt = fmt_elem.text.strip().lower()
        if "pdf" in fmt:
            raise RuntimeError(
                "This ACSM file points to a PDF ebook (format: "
                f"{fmt_elem.text.strip()}). "
                "Only EPUB-sourced ACSM files are supported."
            )

    # Treat unknown/missing src as EPUB (most common case)
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
        raise RuntimeError("adept_activate not found. Cannot register device.")

    # BUG FIX: adept_activate uses -r for anonymous registration, not -a.
    # The -a flag does not exist in libgourou's adept_activate and would
    # cause the tool to fail silently or error out.
    #
    # Try -r first (standard libgourou flag for anonymous registration).
    # Some forks accept no flags at all for anonymous mode, so fall back
    # to calling without arguments if -r fails.
    try:
        result = run([tool, "-r"], timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Device registration timed out (30s).")

    if result.returncode != 0:
        # Fallback: try without any flags (some builds default to anonymous)
        print("[DEBUG] adept_activate -r failed, trying without flags...", flush=True)
        try:
            result = run([tool], timeout=30)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Device registration timed out (30s).")
        if result.returncode != 0:
            raise RuntimeError(
                f"Device registration failed:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

    # Verify the device file was actually created
    if not device_file.exists():
        raise RuntimeError(
            "Device registration command succeeded but device.xml was not created. "
            f"Check that ADEPT_DIR ({ADEPT_DIR}) is writable.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    print("[OK] Adobe device registered.")


def fulfill_acsm(acsm_path, output_path):
    """Download the DRM-protected EPUB by fulfilling the ACSM token."""
    print(f"Fulfilling ACSM: {acsm_path.name}")
    tool = find_tool("acsmdownloader")
    if not tool:
        raise RuntimeError("acsmdownloader not found.")
    try:
        result = run([tool, "-f", str(acsm_path), "-o", str(output_path)], timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Download timed out (120s). The ACSM token may be expired or the server is unreachable.")
    if result.returncode != 0:
        stderr = result.stderr or result.stdout or ""
        raise RuntimeError(f"ACSM download failed (exit code {result.returncode}): {stderr[:500]}")

    if not output_path.exists():
        raise RuntimeError(f"Download completed but output file not found. stdout: {result.stdout[:200]}")

    size_kb = output_path.stat().st_size / 1024
    print(f"[OK] Downloaded: {output_path.name} ({size_kb:.0f} KB)")


def remove_drm(input_path, output_path):
    """Remove DRM from the downloaded EPUB."""
    print(f"Removing DRM: {input_path.name}")
    tool = find_tool("adept_remove")
    if not tool:
        raise RuntimeError("adept_remove not found.")
    try:
        result = run([tool, "-f", str(input_path), "-o", str(output_path)], timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("DRM removal timed out (60s).")
    if result.returncode != 0:
        raise RuntimeError(f"DRM removal failed: {(result.stderr or result.stdout)[:300]}")

    if not output_path.exists():
        raise RuntimeError(
            f"DRM removal command succeeded but output file not found at {output_path}."
        )

    print(f"[OK] DRM removed: {output_path.name}")


# ─── Link Verification ────────────────────────────────────────────────────


# HTML/XHTML attributes that carry links
_LINK_ATTRS = {
    # element tag (case-insensitive) -> list of attribute names
    "a":          ["href"],
    "area":       ["href"],
    "link":       ["href"],
    "script":     ["src"],
    "img":        ["src", "srcset"],
    "image":      ["href", "{http://www.w3.org/1999/xlink}href"],   # SVG <image>
    "use":        ["href", "{http://www.w3.org/1999/xlink}href"],   # SVG <use>
    "video":      ["src", "poster"],
    "audio":      ["src"],
    "source":     ["src", "srcset"],
    "track":      ["src"],
    "iframe":     ["src"],
    "object":     ["data"],
    "embed":      ["src"],
    "blockquote": ["cite"],
    "q":          ["cite"],
    "ins":        ["cite"],
    "del":        ["cite"],
}

# CSS url(...) pattern
_CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'"\)\s]+)['"]?\s*\)""", re.IGNORECASE)

# Encryption algorithms that indicate real DRM, not just font obfuscation
_DRM_ALGORITHMS = {
    "http://www.w3.org/2001/04/xmlenc#aes128-cbc",
    "http://www.w3.org/2001/04/xmlenc#aes256-cbc",
    "http://www.w3.org/2001/04/xmlenc#tripledes-cbc",
    "http://www.w3.org/2001/04/xmlenc#aes128-gcm",
    "http://www.w3.org/2001/04/xmlenc#aes256-gcm",
}

# Font obfuscation algorithms (these are NOT DRM — they are permitted by the
# EPUB spec and do not prevent reading the book)
_FONT_OBFUSCATION_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",           # IDPF font obfuscation
    "http://ns.adobe.com/pdf/enc#RC",               # Adobe font obfuscation
}


def _resolve_epub_path(base_zip_path: str, href: str) -> str | None:
    """
    Resolve a relative href from a file inside the EPUB zip to a canonical
    zip-entry path. Returns None for external or fragment-only URLs.
    """
    parsed = urlparse(href)
    # External URLs (http/https/ftp/mailto …) and data URIs → skip
    if parsed.scheme and parsed.scheme not in ("", "file"):
        return None
    # Pure fragment anchor (#id) → no separate file needed
    if not parsed.path:
        return None

    raw_path = unquote(parsed.path)
    base_dir = str(PurePosixPath(base_zip_path).parent)
    if base_dir == ".":
        resolved = raw_path
    else:
        resolved = str(PurePosixPath(base_dir) / raw_path)

    # Normalize away any ../ segments
    parts = []
    for part in resolved.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/".join(parts)


def _collect_links_from_html(zip_path: str, text: str) -> list[str]:
    """
    Extract all href/src targets from an HTML/XHTML file's text content.
    Returns a list of raw href strings (not yet resolved).
    """
    links: list[str] = []

    # --- XML/HTML attribute scanning ---
    try:
        root = ET.fromstring(text.encode("utf-8", errors="replace"))
        for elem in root.iter():
            local_tag = elem.tag.split("}")[-1].lower() if "}" in elem.tag else elem.tag.lower()
            for attr in _LINK_ATTRS.get(local_tag, []):
                val = elem.get(attr, "").strip()
                if val:
                    # srcset can be "url1 1x, url2 2x"
                    if attr == "srcset":
                        for part in val.split(","):
                            candidate = part.strip().split()[0]
                            if candidate:
                                links.append(candidate)
                    else:
                        links.append(val)
    except ET.ParseError:
        # Fallback: regex scanning for malformed XML/HTML
        for attr in ("href", "src", "data", "poster", "srcset", "cite"):
            for m in re.finditer(rf"""{attr}\s*=\s*['"]([^'"]+)['"]""", text, re.IGNORECASE):
                links.append(m.group(1).strip())

    # --- Inline style / <style> blocks ---
    for m in _CSS_URL_RE.finditer(text):
        links.append(m.group(1).strip())

    return links


def _collect_links_from_css(text: str) -> list[str]:
    """Extract url(...) references from a CSS file."""
    return [m.group(1).strip() for m in _CSS_URL_RE.finditer(text)]


def _collect_links_from_ncx(text: str) -> list[str]:
    """Extract navPoint content src attributes from an NCX file."""
    links: list[str] = []
    try:
        root = ET.fromstring(text.encode("utf-8", errors="replace"))
        for elem in root.iter():
            local = elem.tag.split("}")[-1].lower() if "}" in elem.tag else elem.tag.lower()
            if local == "content":
                src = elem.get("src", "").strip()
                if src:
                    links.append(src)
    except ET.ParseError:
        for m in re.finditer(r"""src\s*=\s*['"]([^'"]+)['"]""", text, re.IGNORECASE):
            links.append(m.group(1).strip())
    return links


def _collect_links_from_nav(text: str) -> list[str]:
    """Extract href values from an EPUB3 NAV document."""
    links: list[str] = []
    try:
        root = ET.fromstring(text.encode("utf-8", errors="replace"))
        for elem in root.iter():
            local = elem.tag.split("}")[-1].lower() if "}" in elem.tag else elem.tag.lower()
            if local == "a":
                href = (elem.get("href") or "").strip()
                if href:
                    links.append(href)
    except ET.ParseError:
        for m in re.finditer(r"""href\s*=\s*['"]([^'"]+)['"]""", text, re.IGNORECASE):
            links.append(m.group(1).strip())
    return links


class LinkCheckResult:
    """Holds the outcome of an EPUB link integrity audit."""

    def __init__(self):
        self.total_links: int = 0
        self.external_links: int = 0
        self.fragment_links: int = 0
        self.internal_ok: int = 0
        self.broken: list[tuple[str, str, str]] = []   # (source_file, href, resolved)
        self.encrypted_remaining: list[str] = []
        self.obfuscated_fonts: list[str] = []           # font obfuscation (not DRM)
        self.warnings: list[str] = []

    @property
    def has_errors(self) -> bool:
        return bool(self.broken) or bool(self.encrypted_remaining)

    def summary(self) -> str:
        lines = [
            f"Links audited  : {self.total_links}",
            f"  External URLs : {self.external_links}",
            f"  Fragment-only : {self.fragment_links}",
            f"  Internal OK   : {self.internal_ok}",
            f"  Broken        : {len(self.broken)}",
        ]
        if self.encrypted_remaining:
            lines.append(f"  Still encrypted: {len(self.encrypted_remaining)} file(s)")
        if self.obfuscated_fonts:
            lines.append(f"  Obfuscated fonts: {len(self.obfuscated_fonts)} (normal, not DRM)")
        if self.broken:
            lines.append("Broken links:")
            for src, href, resolved in self.broken[:20]:
                lines.append(f"  [{src}] → {href!r}  (resolved: {resolved!r})")
            if len(self.broken) > 20:
                lines.append(f"  … and {len(self.broken) - 20} more.")
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  {w}")
        return "\n".join(lines)


def verify_epub_links(epub_path: Path) -> LinkCheckResult:
    """
    Open a (DRM-free) EPUB and audit every type of internal link.

    Link types checked
    ──────────────────
    1. HTML/XHTML  : <a href>, <img src>, <link href>, <script src>,
                     <video src/poster>, <audio src>, <source src/srcset>,
                     <track src>, <iframe src>, <object data>, <embed src>,
                     <blockquote cite>, <q cite>, SVG <image href>,
                     SVG <use href>, inline style url()
    2. CSS files   : url(…) references (fonts, images, backgrounds)
    3. NCX (EPUB2) : navPoint/content[@src]
    4. NAV (EPUB3) : <a href> in toc.xhtml / nav document
    5. OPF manifest: every item href is present in the zip

    External URLs (http/https/mailto/…) and pure fragment anchors (#id) are
    counted but not treated as errors — they cannot be validated offline.
    """
    result = LinkCheckResult()

    if not epub_path.exists():
        result.warnings.append(f"EPUB file not found: {epub_path}")
        return result

    try:
        zf = zipfile.ZipFile(epub_path, "r")
    except zipfile.BadZipFile as e:
        result.warnings.append(f"Cannot open EPUB as zip: {e}")
        return result

    with zf:
        zip_names_lower = {n.lower(): n for n in zf.namelist()}
        zip_names_set = set(zf.namelist())

        def zip_has(path: str) -> bool:
            """Case-insensitive membership test."""
            return path in zip_names_set or path.lower() in zip_names_lower

        # ── Check for residual encryption ─────────────────────────────────
        # BUG FIX: Distinguish real DRM encryption from harmless font
        # obfuscation. Many DRM-free EPUBs retain encryption.xml with
        # entries for obfuscated fonts (IDPF/Adobe font mangling). These
        # are NOT DRM and should not cause a hard error.
        if "META-INF/encryption.xml" in zip_names_set:
            try:
                enc_xml = zf.read("META-INF/encryption.xml").decode("utf-8", errors="replace")
                enc_root = ET.fromstring(enc_xml)
                for enc_data in enc_root.iter():
                    local = enc_data.tag.split("}")[-1].lower() if "}" in enc_data.tag else enc_data.tag.lower()
                    if local != "encrypteddata":
                        continue

                    # Find the algorithm used
                    algorithm = None
                    uri = None
                    for child in enc_data.iter():
                        child_local = child.tag.split("}")[-1].lower() if "}" in child.tag else child.tag.lower()
                        if child_local == "encryptionmethod":
                            algorithm = child.get("Algorithm", "").strip()
                        elif child_local == "cipherreference":
                            uri = child.get("URI", "").strip()

                    if not uri:
                        continue

                    if algorithm in _FONT_OBFUSCATION_ALGORITHMS:
                        # Font obfuscation — not DRM, perfectly normal
                        result.obfuscated_fonts.append(uri)
                    elif algorithm in _DRM_ALGORITHMS or algorithm is None:
                        # Real DRM encryption or unknown algorithm — flag it
                        result.encrypted_remaining.append(uri)
                    else:
                        # Unknown algorithm — warn but don't hard-fail
                        result.warnings.append(
                            f"Unknown encryption algorithm for {uri}: {algorithm}"
                        )

            except Exception as e:
                result.warnings.append(f"Could not parse encryption.xml: {e}")

        # ── Parse OPF to find all manifest items ─────────────────────────
        opf_path = None
        # Try container.xml first (standard)
        if "META-INF/container.xml" in zip_names_set:
            try:
                container_xml = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
                c_root = ET.fromstring(container_xml)
                for elem in c_root.iter():
                    local = elem.tag.split("}")[-1].lower() if "}" in elem.tag else elem.tag.lower()
                    if local == "rootfile":
                        opf_path = elem.get("full-path", "").strip()
                        break
            except Exception:
                pass
        if not opf_path:
            opf_path = next((n for n in zf.namelist() if n.endswith(".opf")), None)

        manifest_items: dict[str, str] = {}   # id -> zip-path
        spine_items: list[str] = []           # zip-paths in reading order
        nav_path: str | None = None
        ncx_path: str | None = None

        if opf_path:
            try:
                opf_xml = zf.read(opf_path).decode("utf-8", errors="replace")
                opf_root = ET.fromstring(opf_xml)
                opf_dir = str(PurePosixPath(opf_path).parent)

                for elem in opf_root.iter():
                    local = elem.tag.split("}")[-1].lower() if "}" in elem.tag else elem.tag.lower()
                    if local == "item":
                        item_id = elem.get("id", "")
                        href = elem.get("href", "").strip()
                        if href:
                            resolved = _resolve_epub_path(opf_path, href) or href
                            manifest_items[item_id] = resolved
                            props = elem.get("properties", "")
                            media_type = elem.get("media-type", "")
                            if "nav" in props:
                                nav_path = resolved
                            if media_type == "application/x-dtbncx+xml" or href.endswith(".ncx"):
                                ncx_path = resolved

                            # OPF manifest integrity check
                            result.total_links += 1
                            if not zip_has(resolved):
                                result.broken.append((opf_path, href, resolved))
                            else:
                                result.internal_ok += 1

                    elif local == "itemref":
                        idref = elem.get("idref", "")
                        if idref in manifest_items:
                            spine_items.append(manifest_items[idref])

            except Exception as e:
                result.warnings.append(f"Could not parse OPF: {e}")

        # ── Scan all content files for links ──────────────────────────────
        for zip_entry in zf.namelist():
            lower = zip_entry.lower()
            is_html = lower.endswith((".xhtml", ".html", ".htm", ".xml"))
            is_css = lower.endswith(".css")
            is_ncx = lower.endswith(".ncx") or zip_entry == ncx_path
            is_nav = zip_entry == nav_path

            if not (is_html or is_css or is_ncx or is_nav):
                continue

            try:
                text = zf.read(zip_entry).decode("utf-8", errors="replace")
            except Exception as e:
                result.warnings.append(f"Cannot read {zip_entry}: {e}")
                continue

            if is_css:
                raw_links = _collect_links_from_css(text)
            elif is_ncx:
                raw_links = _collect_links_from_ncx(text)
            elif is_nav:
                raw_links = _collect_links_from_nav(text) + _collect_links_from_html(zip_entry, text)
            else:
                raw_links = _collect_links_from_html(zip_entry, text)

            for href in raw_links:
                if not href:
                    continue
                result.total_links += 1

                parsed = urlparse(href)
                # External URL
                if parsed.scheme and parsed.scheme not in ("", "file"):
                    result.external_links += 1
                    continue
                # Pure fragment
                if not parsed.path:
                    result.fragment_links += 1
                    continue

                resolved = _resolve_epub_path(zip_entry, href)
                if resolved is None:
                    result.external_links += 1
                    continue

                if zip_has(resolved):
                    result.internal_ok += 1
                else:
                    result.broken.append((zip_entry, href, resolved))

    return result


# ─── Pipeline ─────────────────────────────────────────────────────────────


def convert_pipeline(acsm_path, output_dir):
    """Generator that yields (step, message) tuples for each conversion step.

    Used by both the CLI (do_convert) and the web interface (app.py).
    Raises RuntimeError on failure.

    Steps:
      1. Check tools
      2. Detect format (EPUB only — raises if PDF)
      3. Register Adobe device
      4. Download EPUB
      5. Remove DRM
      6. Verify link integrity
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
    if not find_tool("acsmdownloader"):
        problems.append("acsmdownloader not found (run: python3 converter.py --setup)")
    if not find_tool("adept_activate"):
        problems.append("adept_activate not found (run: python3 converter.py --setup)")
    if not find_tool("adept_remove"):
        problems.append("adept_remove not found (run: python3 converter.py --setup)")
    if problems:
        raise RuntimeError("Missing components: " + "; ".join(set(problems)))
    yield (1, "All tools ready.")

    # Step 2: Detect format — raises if not EPUB
    detect_format(acsm_path)
    yield (2, "Detected format: EPUB")

    # Step 3: Register device
    register_device()
    yield (3, "Device registered.")

    # Step 4: Download
    drm_file = output_dir / f"{stem}_drm.epub"
    fulfill_acsm(acsm_path, drm_file)
    yield (4, f"Downloaded: {drm_file.name}")

    # Step 5: Remove DRM
    epub_file = output_dir / f"{stem}.epub"
    remove_drm(drm_file, epub_file)
    # Clean up the intermediate DRM file
    try:
        drm_file.unlink()
    except Exception:
        pass
    yield (5, f"DRM removed: {epub_file.name}")

    # Step 6: Verify link integrity
    print("Verifying link integrity...")
    link_result = verify_epub_links(epub_file)

    if link_result.encrypted_remaining:
        # DRM removal left some encrypted content — hard error
        files = ", ".join(link_result.encrypted_remaining[:5])
        raise RuntimeError(
            f"DRM removal incomplete: {len(link_result.encrypted_remaining)} file(s) "
            f"are still encrypted ({files}). The EPUB may not be readable."
        )

    if link_result.broken:
        # Broken internal links — warn but do not fail (publisher may have
        # shipped a broken EPUB; we should not block the user's download)
        broken_count = len(link_result.broken)
        sample = link_result.broken[0]
        warning_msg = (
            f"Link check: {link_result.internal_ok} OK, "
            f"{broken_count} broken (e.g. [{sample[0]}]→{sample[1]!r}). "
            f"The EPUB is usable but some links may not work."
        )
        yield (6, warning_msg)
    else:
        extra = ""
        if link_result.obfuscated_fonts:
            extra = f", {len(link_result.obfuscated_fonts)} obfuscated fonts (normal)"
        yield (
            6,
            f"Links verified: {link_result.internal_ok} internal, "
            f"{link_result.external_links} external, "
            f"{link_result.fragment_links} anchors{extra} — all OK.",
        )

    # Done
    size_mb = epub_file.stat().st_size / (1024 * 1024) if epub_file.exists() else 0
    yield ("done", f"{epub_file.name}|{size_mb:.1f} MB")


def do_convert(acsm_file, output_dir):
    """Run the full ACSM to EPUB conversion pipeline (CLI entry point)."""
    try:
        for step, message in convert_pipeline(acsm_file, output_dir):
            if step == "done":
                parts = message.split("|")
                print(f"\n=== Done! ===\nFile: {parts[0]} ({parts[1]})")
            else:
                print(f"\n=== Step {step}/6: {message} ===")
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Convert EPUB-sourced ACSM ebook tokens to DRM-free EPUB.",
        epilog="First run: python3 converter.py --setup",
    )
    parser.add_argument(
        "acsm_file",
        nargs="?",
        help="Path to the .acsm file to convert",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Install dependencies and build tools (run once)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output",
        help="Output directory (default: output)",
    )
    parser.add_argument(
        "--verify-only",
        metavar="EPUB",
        help="Audit link integrity of an existing EPUB file (no conversion)",
    )
    args = parser.parse_args()

    if args.verify_only:
        result = verify_epub_links(Path(args.verify_only))
        print(result.summary())
        sys.exit(1 if result.has_errors else 0)

    if args.setup:
        do_setup()
        return

    if not args.acsm_file:
        parser.print_help()
        sys.exit(1)

    do_convert(args.acsm_file, args.output_dir)


if __name__ == "__main__":
    main()
