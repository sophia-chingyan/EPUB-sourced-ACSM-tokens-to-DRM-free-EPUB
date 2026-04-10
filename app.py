#!/usr/bin/env python3
"""Flask web interface for the ACSM converter."""

import os
import threading
import time
import traceback
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from functools import wraps
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from flask import (
    Flask, jsonify, make_response, render_template,
    request, send_from_directory, session, redirect, url_for,
)

from werkzeug.middleware.proxy_fix import ProxyFix
from converter import convert_pipeline

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ALLOWED_EMAIL        = os.environ.get("ALLOWED_EMAIL", "")

SCRIPT_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = SCRIPT_DIR / "uploads"
OUTPUT_DIR = SCRIPT_DIR / "output"
COVER_DIR  = SCRIPT_DIR / "covers"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
COVER_DIR.mkdir(exist_ok=True)

STEP_LABELS = {
    1: "Checking tools...",
    2: "Detecting format...",
    3: "Registering Adobe device...",
    4: "Downloading ebook...",
    5: "Removing DRM...",
}

active_jobs = {}

# ─── OAuth setup ─────────────────────────────────────────────────────────

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ─── Auth ────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login")
def login():
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo")
    if not user_info:
        return render_template("login_error.html",
                               error="Could not retrieve user info from Google."), 403

    email = user_info.get("email", "")
    if ALLOWED_EMAIL and email.lower() != ALLOWED_EMAIL.lower():
        return render_template("login_error.html",
                               error=f"Access denied for {email}."), 403

    session["authenticated"] = True
    session["user_email"] = email
    session["user_name"] = user_info.get("name", email)
    session["user_picture"] = user_info.get("picture", "")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Cover extraction ───────────────────────────────────────────────────

def extract_epub_cover(epub_path):
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
    opf_path = None
    for name in zf.namelist():
        if name.endswith(".opf"):
            opf_path = name
            break
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


# ─── Book listing ────────────────────────────────────────────────────────

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
                "ext": f.suffix[1:].upper(),
            })
            if not books[stem]["cover"]:
                cover = extract_epub_cover(f)
                if cover:
                    books[stem]["cover"] = cover
    return list(books.values())


# ─── Background conversion ──────────────────────────────────────────────

def run_conversion_job(job_id, acsm_path, output_dir):
    job = active_jobs[job_id]
    total = 5
    print(f"[JOB] {job_id} started: acsm={acsm_path}", flush=True)
    try:
        job["current_step"] = 1
        job["current_label"] = STEP_LABELS[1]

        for step, message in convert_pipeline(str(acsm_path), str(output_dir)):
            print(f"[JOB] {job_id} step={step} msg={message}", flush=True)
            if step == "done":
                job["steps"].append({"step": "done", "message": message})
                job["status"] = "done"
                job["done_message"] = message
            else:
                step_num = int(step)
                job["steps"].append({"step": step_num, "message": message})
                next_step = step_num + 1
                if next_step <= total:
                    job["current_step"] = next_step
                    job["current_label"] = STEP_LABELS[next_step]
    except RuntimeError as e:
        print(f"[JOB] {job_id} error: {e}", flush=True)
        job["status"] = "error"
        job["error"] = str(e)
    except Exception as e:
        print(f"[JOB] {job_id} unexpected: {e}\n{traceback.format_exc()}", flush=True)
        job["status"] = "error"
        job["error"] = f"Unexpected error: {e}"


# ─── Routes ──────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    books = get_books()
    resp = make_response(render_template("index.html", books=books,
                                         user_name=session.get("user_name", ""),
                                         user_picture=session.get("user_picture", "")))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/library")
@login_required
def library():
    books = get_books()
    resp = make_response(render_template("library.html", books=books,
                                          user_name=session.get("user_name", ""),
                                          user_picture=session.get("user_picture", "")))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400
    if not file.filename.endswith(".acsm"):
        return jsonify({"error": "Only .acsm files are accepted"}), 400
    filename = Path(file.filename).name
    save_path = UPLOAD_DIR / filename
    file.save(save_path)
    return jsonify({"filename": filename})


@app.route("/start-convert/<filename>", methods=["POST"])
@login_required
def start_convert(filename):
    filename = Path(filename).name
    acsm_path = UPLOAD_DIR / filename

    if not acsm_path.exists():
        return jsonify({"error": "File not found"}), 404

    job_id = f"{filename}_{int(time.time())}"
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

    t = threading.Thread(
        target=run_conversion_job,
        args=(job_id, acsm_path, OUTPUT_DIR),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/job-status/<job_id>")
@login_required
def job_status(job_id):
    if job_id not in active_jobs:
        return jsonify({"error": "Job not found"}), 404

    job = active_jobs[job_id]
    elapsed = round(time.time() - job["start_time"])

    return jsonify({
        "status": job["status"],
        "steps": job["steps"],
        "current_step": job["current_step"],
        "current_label": job["current_label"],
        "error": job["error"],
        "done_message": job["done_message"],
        "elapsed": elapsed,
    })


@app.route("/download/<filename>")
@login_required
def download(filename):
    filename = Path(filename).name
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


@app.route("/delete/<filename>", methods=["POST"])
@login_required
def delete_file(filename):
    filename = Path(filename).name
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        return jsonify({"error": "File not found"}), 404

    stem = file_path.stem
    try:
        file_path.unlink()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    for d in (UPLOAD_DIR, COVER_DIR):
        for f in d.iterdir():
            if f.stem == stem or f.stem.startswith(stem):
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass

    return jsonify({"status": "deleted", "filename": filename})


@app.route("/delete-all", methods=["POST"])
@login_required
def delete_all():
    deleted = []

    for f in OUTPUT_DIR.iterdir():
        if f.suffix == ".epub":
            try:
                name = f.name
                f.unlink()
                deleted.append(name)
            except Exception:
                pass

    for d in (UPLOAD_DIR, COVER_DIR):
        for f in d.iterdir():
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    return jsonify({"status": "deleted", "deleted": deleted})


@app.route("/cover/<filename>")
@login_required
def cover(filename):
    filename = Path(filename).name
    return send_from_directory(COVER_DIR, filename)


@app.route("/debug-status")
@login_required
def debug_status():
    jobs_summary = {}
    for jid, job in active_jobs.items():
        jobs_summary[jid] = {
            "status": job["status"],
            "steps_count": len(job["steps"]),
            "current_step": job["current_step"],
            "error": job["error"],
            "elapsed": round(time.time() - job["start_time"]),
        }
    upload_files = [f.name for f in UPLOAD_DIR.iterdir()] if UPLOAD_DIR.exists() else []
    output_files = [f.name for f in OUTPUT_DIR.iterdir()] if OUTPUT_DIR.exists() else []
    return jsonify({
        "active_jobs": jobs_summary,
        "upload_files": upload_files,
        "output_files": output_files,
        "libgourou_exists": (SCRIPT_DIR / "libgourou" / "utils" / "acsmdownloader").exists(),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
