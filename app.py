#!/usr/bin/env python3
"""Flask web interface for the ACSM to EPUB converter (EPUB sources only)."""

import os
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from functools import wraps
from pathlib import Path

from flask import (
    Flask, jsonify, make_response, render_template, request,
    send_from_directory, session, redirect, url_for,
)

from converter import convert_pipeline

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

SCRIPT_DIR = Path(__file__).resolve().parent

_default_data = "/data" if Path("/data").exists() else str(SCRIPT_DIR / "data")
DATA_DIR   = Path(os.environ.get("DATA_DIR", _default_data))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
COVER_DIR  = DATA_DIR / "covers"

for _d in (UPLOAD_DIR, OUTPUT_DIR, COVER_DIR):
    _d.mkdir(parents=True, exist_ok=True)

TOTAL_STEPS = 6

STEP_LABELS = {
    1: "Checking tools...",
    2: "Detecting format...",
    3: "Registering Adobe device...",
    4: "Downloading EPUB...",
    5: "Removing DRM...",
    6: "Verifying links...",
}

_jobs_lock = threading.Lock()
active_jobs: dict = {}

# FIX: evict finished jobs after this many seconds to prevent memory growth
_JOB_TTL_SECONDS = 3600  # 1 hour


def _evict_old_jobs():
    """Remove completed/errored jobs older than _JOB_TTL_SECONDS."""
    now = time.time()
    to_delete = [
        jid for jid, job in active_jobs.items()
        if job["status"] in ("done", "error")
        and now - job["start_time"] > _JOB_TTL_SECONDS
    ]
    for jid in to_delete:
        del active_jobs[jid]


# ── Auth ──────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if APP_PASSWORD and not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        session["authenticated"] = True
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Wrong password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Cover extraction ──────────────────────────────────────────────────────

def extract_epub_cover(epub_path: Path):
    cover_out = COVER_DIR / f"{epub_path.stem}.jpg"
    if cover_out.exists():
        return cover_out.name
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            cover_name = _find_cover_in_opf(zf) or _find_cover_by_name(zf)
            if cover_name:
                data = zf.read(cover_name)
                ext = Path(cover_name).suffix or ".jpg"
                cover_out = COVER_DIR / f"{epub_path.stem}{ext}"
                cover_out.write_bytes(data)
                return cover_out.name
    except Exception:
        pass
    return None


def _find_cover_in_opf(zf):
    opf_path = next((n for n in zf.namelist() if n.endswith(".opf")), None)
    if not opf_path:
        return None
    opf_xml = zf.read(opf_path).decode("utf-8", errors="replace")
    root = ET.fromstring(opf_xml)
    cover_id = None
    for meta in root.iter():
        if meta.tag.endswith("}meta") or meta.tag == "meta":
            if meta.get("name") == "cover":
                cover_id = meta.get("content")
                break
    if not cover_id:
        for item in root.iter():
            if item.tag.endswith("}item") or item.tag == "item":
                if "cover-image" in (item.get("properties") or ""):
                    href = item.get("href")
                    if href:
                        opf_dir = str(Path(opf_path).parent)
                        return href if opf_dir == "." else f"{opf_dir}/{href}"
        return None
    for item in root.iter():
        if item.tag.endswith("}item") or item.tag == "item":
            if item.get("id") == cover_id:
                href = item.get("href")
                if href:
                    opf_dir = str(Path(opf_path).parent)
                    return href if opf_dir == "." else f"{opf_dir}/{href}"
    return None


def _find_cover_by_name(zf):
    for name in zf.namelist():
        lower = name.lower()
        if "cover" in lower and any(lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png")):
            return name
    return None


# ── Book listing ──────────────────────────────────────────────────────────

def get_books():
    if not OUTPUT_DIR.exists():
        return []
    books = OrderedDict()
    for f in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix == ".epub":
            stem = f.stem
            if stem not in books:
                books[stem] = {"stem": stem, "files": [], "cover": None}
            size_mb = f.stat().st_size / (1024 * 1024)
            books[stem]["files"].append({
                "name": f.name,
                "size": f"{size_mb:.1f} MB",
                "ext": "EPUB",
            })
            if not books[stem]["cover"]:
                cov = extract_epub_cover(f)
                if cov:
                    books[stem]["cover"] = cov
    return list(books.values())


# ── Conversion job runner ─────────────────────────────────────────────────

def run_conversion_job(job_id: str, acsm_path: Path, output_dir: Path):
    import traceback
    print(f"[DEBUG] Job {job_id} started: acsm={acsm_path}, output={output_dir}", flush=True)
    try:
        with _jobs_lock:
            active_jobs[job_id]["current_step"] = 1
            active_jobs[job_id]["current_label"] = STEP_LABELS[1]

        # FIX: convert_pipeline now yields 3-tuples (step, message, is_warning).
        # Previously the is_warning flag was derived by string-matching "broken"
        # in the message, which broke silently if the message changed wording.
        for step, message, is_warning in convert_pipeline(str(acsm_path), str(output_dir)):
            print(f"[DEBUG] Job {job_id} step={step} message={message} warning={is_warning}", flush=True)
            with _jobs_lock:
                if step == "done":
                    active_jobs[job_id]["steps"].append({"step": "done", "message": message})
                    active_jobs[job_id]["status"] = "done"
                    active_jobs[job_id]["done_message"] = message
                else:
                    step_num = int(step)
                    active_jobs[job_id]["steps"].append({
                        "step": step_num,
                        "message": message,
                        "warning": is_warning,
                    })
                    next_step = step_num + 1
                    if next_step <= TOTAL_STEPS:
                        active_jobs[job_id]["current_step"] = next_step
                        active_jobs[job_id]["current_label"] = STEP_LABELS[next_step]

    except RuntimeError as e:
        print(f"[DEBUG] Job {job_id} RuntimeError: {e}", flush=True)
        with _jobs_lock:
            active_jobs[job_id]["status"] = "error"
            active_jobs[job_id]["error"] = str(e)
    except Exception as e:
        print(f"[DEBUG] Job {job_id} Exception: {e}\n{traceback.format_exc()}", flush=True)
        with _jobs_lock:
            active_jobs[job_id]["status"] = "error"
            active_jobs[job_id]["error"] = f"Unexpected error: {e}"
    finally:
        try:
            acsm_path.unlink(missing_ok=True)
        except Exception:
            pass
        # FIX: evict stale jobs whenever a job finishes to bound memory usage
        with _jobs_lock:
            _evict_old_jobs()


# ── Routes ────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    books = get_books()
    resp = make_response(render_template("index.html", books=books))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/library")
@login_required
def library():
    books = get_books()
    resp = make_response(render_template("library.html", books=books))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return {"error": "No file provided"}, 400
    if not file.filename.endswith(".acsm"):
        return {"error": "Only .acsm files are accepted"}, 400
    filename = Path(file.filename).name
    save_path = UPLOAD_DIR / filename
    file.save(save_path)
    return {"filename": filename}


@app.route("/start-convert/<filename>", methods=["POST"])
@login_required
def start_convert(filename):
    filename = Path(filename).name
    acsm_path = UPLOAD_DIR / filename
    if not acsm_path.exists():
        return jsonify({"error": "File not found"}), 404

    job_id = f"{filename}_{int(time.time())}"
    with _jobs_lock:
        active_jobs[job_id] = {
            "filename": filename,
            "status": "running",
            "steps": [],
            "current_step": 0,
            "current_label": "",
            "error": None,
            "done_message": None,
            "start_time": time.time(),
        }
    threading.Thread(
        target=run_conversion_job,
        args=(job_id, acsm_path, OUTPUT_DIR),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/job-status/<job_id>")
@login_required
def job_status(job_id):
    # FIX: read the job under the lock so we never see partial state from a
    # concurrent write in run_conversion_job
    with _jobs_lock:
        if job_id not in active_jobs:
            return jsonify({"error": "Job not found"}), 404
        job = dict(active_jobs[job_id])          # shallow copy — safe to read outside lock
        job["steps"] = list(job["steps"])        # copy list too

    return jsonify({
        "status": job["status"],
        "steps": job["steps"],
        "current_step": job["current_step"],
        "current_label": job["current_label"],
        "error": job["error"],
        "done_message": job["done_message"],
        "elapsed": round(time.time() - job["start_time"]),
    })


@app.route("/download/<filename>")
@login_required
def download(filename):
    filename = Path(filename).name
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        return {"error": "File not found"}, 404
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


@app.route("/delete/<filename>", methods=["POST"])
@login_required
def delete_file(filename):
    filename = Path(filename).name
    stem = Path(filename).stem
    deleted, errors = [], []

    file_path = OUTPUT_DIR / filename
    if file_path.exists():
        try:
            file_path.unlink()
            deleted.append(filename)
        except Exception as e:
            errors.append(str(e))
    else:
        errors.append(f"{filename} not found")

    for d in (UPLOAD_DIR, COVER_DIR):
        if d.exists():
            for f in list(d.iterdir()):
                if f.stem == stem or f.stem.startswith(stem):
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass

    if errors and not deleted:
        return jsonify({"error": "; ".join(errors)}), 404
    return jsonify({"deleted": deleted, "errors": errors})


@app.route("/delete-all", methods=["POST"])
@login_required
def delete_all():
    deleted, errors = [], []
    if OUTPUT_DIR.exists():
        for f in list(OUTPUT_DIR.iterdir()):
            if f.suffix == ".epub":
                try:
                    stem = f.stem
                    f.unlink()
                    deleted.append(f.name)
                    for d in (UPLOAD_DIR, COVER_DIR):
                        if d.exists():
                            for cf in list(d.iterdir()):
                                if cf.stem == stem or cf.stem.startswith(stem):
                                    try:
                                        cf.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                except Exception as e:
                    errors.append(str(e))
    return jsonify({"deleted": deleted, "errors": errors})


@app.route("/cover/<filename>")
@login_required
def cover(filename):
    filename = Path(filename).name
    return send_from_directory(COVER_DIR, filename)


# FIX: /debug-status was missing @login_required — anyone who knew the URL
# could enumerate your files, job history, and internal paths without auth.
@app.route("/debug-status")
@login_required
def debug_status():
    import shutil
    with _jobs_lock:
        jobs_info = {
            jid: {
                "status": job["status"],
                "steps_count": len(job["steps"]),
                "current_step": job["current_step"],
                "error": job["error"],
                "elapsed": round(time.time() - job["start_time"]),
            }
            for jid, job in active_jobs.items()
        }
    return jsonify({
        "data_dir": str(DATA_DIR),
        "active_jobs": jobs_info,
        "upload_files": [f.name for f in UPLOAD_DIR.iterdir()] if UPLOAD_DIR.exists() else [],
        "output_files": [f.name for f in OUTPUT_DIR.iterdir()] if OUTPUT_DIR.exists() else [],
        "acsmdownloader_found": shutil.which("acsmdownloader")
            or str(SCRIPT_DIR / "libgourou" / "utils" / "acsmdownloader"),
        "libgourou_exists": (SCRIPT_DIR / "libgourou" / "utils" / "acsmdownloader").exists(),
        "adept_registered": (Path.home() / ".config" / "adept" / "device.xml").exists(),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
