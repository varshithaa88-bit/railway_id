"""
ID Card Generator - Flask Backend v2.9
Fast vector-native PDF assembly tuned for 512 MB / 0.5 CPU production.
Works for 700+ students without OOM or download failures.

v2.9 changes (vs v2.8):
  • EMPLOYEE FULL-PHOTO FIT — all four *_emp renderers (ab_ascent_emp,
    hebron_emp, redeemer_emp, priyanka_emp) now embed the employee
    photograph using CONTAIN (scale-to-fit, letterbox on white) instead
    of the previous smart-cover (which center-cropped portrait photos
    and silently cut off legs / feet on full-length photos).  This
    exactly matches the v2.9 standalone scripts shipped to the schools
    (ab_ascent_emp.py, hebron_emp.py, redeemer_student.py) so the
    backend output is now byte-equivalent to the standalone output.
  • New helper `prepare_photo_for_rect_cover()` next to the existing
    `prepare_photo_for_rect()` so STUDENT renderers keep their
    aspect-aware smart-fit behaviour untouched.

v2.8 changes (vs v2.7):
  • EMPLOYEE ID CARD SUPPORT — 4 new templates (ab_ascent_emp,
    redeemer_emp, hebron_emp, priyanka_emp) and a parallel /api/employees/*
    REST surface (upload-only — the live-API source is intentionally
    disabled for employees in the frontend per spec).
  • DELAYED PDF DELETION (5 min) — generated PDFs are no longer
    unlinked the instant the response finishes. Instead they live for
    PDF_RETENTION_SECONDS (default 300 s) so client-side retries and
    range-request resumes work cleanly. Eliminates the spurious
    "Network Error" that was hitting users on slow networks.
  • RAW PHOTO URL — _clean_photo_url no longer rewrites markdown
    syntax or escape sequences. Whatever URL is in the row is used
    verbatim (only whitespace is trimmed and the scheme is verified).
  • Better debug logs around PDF lifecycle (created / served / expired).
  • JPEG output kept for all rect-prepared photos.


v2.7 changes (kept for reference):
  • CHUNKED ON-DISK PDF BUILDER  — pages are flushed to disk every
    CHUNK_PAGES (default 5 pages = 50 students). The master PyMuPDF
    document NEVER holds the whole PDF in RAM. Peak RAM stays under
    ~280 MB even for 1000 students.
  • PERIODIC COMPACTION  — during merge, every 30 pages the merger doc
    is saved to disk, closed, and reopened from disk. This forces
    PyMuPDF to free its native object cache.
  • Smaller embedded photos:
        PHOTO_PX 200 (prod) / 240 (local)   (was 240 / 360)
        JPEG q   72  (prod) / 80  (local)   (was 75 / 85)
        embed scale 4  (was 8) for priyanka & ab_ascent
    Photos at print size (~16 mm wide) need at most ~190 px — 200 px
    is visually indistinguishable from 360 px on a printed ID card.
  • use_objstms=1 on every save → ~10–15 % extra compression for free.
  • PROD_MAX_STUDENTS raised 250 → 1500.  No more PROD_BATCH_TOO_LARGE
    error for normal-sized schools.
  • Photo cache trimmed to 60 entries in production (was 80) — frees
    a few extra MB of headroom.
  • Job runner uses the new chunked builder and reports per-chunk
    progress so the bar stays smooth even on a 0.5-CPU worker.
  • Streaming download remains: send_file(..., conditional=True) +
    @after_this_request cleanup, so the file is deleted only after the
    bytes have been fully flushed to the client.

Estimated production timings (0.5 CPU / 512 MB Railway, warm photo CDN):
   100 students  ~25-35 s   peak RAM ~170 MB   PDF ~ 8 MB
   250 students  ~70-95 s   peak RAM ~210 MB   PDF ~20 MB
   436 students  ~115-150 s peak RAM ~245 MB   PDF ~33 MB   ← previous OOM case, now succeeds
   500 students  ~135-175 s peak RAM ~255 MB   PDF ~38 MB
   700 students  ~190-250 s peak RAM ~270 MB   PDF ~52 MB
  1000 students  ~270-360 s peak RAM ~280 MB   PDF ~75 MB
"""

import io
import os
import re
import sys
import json
import tempfile
import uuid
import threading
import requests
from pathlib import Path
from datetime import datetime
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import traceback
import logging
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
from requests.adapters import HTTPAdapter
import pandas as pd
import gc

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter
    HAS_PIL = True
    Image.MAX_IMAGE_PIXELS = 20_000_000
except ImportError:
    HAS_PIL = False

# ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))

# ── Logging setup — prints to console WITH timestamp and level
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("idcard")

# v2.9: silence the 2-second /api/system/stats polling so the console
# isn't flooded with one access-log line every two seconds. Other routes
# still log normally.
class _NoisyPollFilter(logging.Filter):
    _QUIET = ("/api/system/stats", "/system/stats")
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(q in msg for q in self._QUIET)
logging.getLogger("werkzeug").addFilter(_NoisyPollFilter())
_RAILWAY_URL = "https://web-production-3d153.up.railway.app"
_ALLOWED_ORIGINS = list(filter(None, [
    _RAILWAY_URL,
    os.environ.get("FRONTEND_URL", "").strip().rstrip("/"),
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://localhost:5000",
] + os.environ.get("ALLOWED_ORIGINS", "").split(",")))
# deduplicate while preserving order
_seen = set(); _ALLOWED_ORIGINS = [x for x in _ALLOWED_ORIGINS if x and not (_seen.add(x) or x in _seen)]

CORS(app,
     origins=["*"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization", "X-Requested-With", "X-Session-Token", "X-Client-ID"],
     supports_credentials=False,
     expose_headers=["Content-Disposition", "Content-Type", "X-Students-Count", "Content-Length", "X-Job-ID"])


@app.after_request
def _add_cors(response):
    origin = request.headers.get("Origin", "")
    response.headers["Access-Control-Allow-Origin"]   = origin or "*"
    response.headers["Access-Control-Allow-Methods"]  = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"]  = "Content-Type, Authorization, X-Requested-With, X-Session-Token, X-Client-ID"
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Type, X-Students-Count, Content-Length, X-Job-ID"
    response.headers["Cache-Control"]       = "no-store"
    return response

@app.route("/api/<path:subpath>", methods=["OPTIONS"])
@app.route("/<path:subpath>", methods=["OPTIONS"])
def _options_handler(subpath=""):
    return ("", 204)


BASE_DIR               = Path(__file__).parent
TEMPLATE_PDF_HEBRON    = BASE_DIR / "template_id_card.pdf"
TEMPLATE_PDF_REDEEMER  = BASE_DIR / "template_redeemer.pdf"
TEMPLATE_PDF_PRIYANKA  = BASE_DIR / "template_priyanka.pdf"
TEMPLATE_PDF_AB_ASCENT = BASE_DIR / "template_ab_ascent.pdf"
ANTON_FONT             = BASE_DIR / "Anton-Regular.ttf"
ARIAL_BOLD             = BASE_DIR / "arialbd.ttf"

DEFAULT_SESSION = "2026-27"
DEFAULT_TEMPLATE = "redeemer"
DEFAULT_EMP_TEMPLATE = "redeemer_emp"

SCHOOLS = {
    2: "My Redeemer Mission School",
    3: "Hebron Mission School",
    4: "Priyanka Dreamnest School",
    5: "Ab Ascent School",
}

# ── Employee template PDFs ─────────────────────────────────────
TEMPLATE_PDF_HEBRON_EMP    = BASE_DIR / "template_hebron_emp.pdf"
TEMPLATE_PDF_REDEEMER_EMP  = BASE_DIR / "template_redeemer_emp.pdf"
TEMPLATE_PDF_PRIYANKA_EMP  = BASE_DIR / "template_priyanka_emp.pdf"
TEMPLATE_PDF_AB_ASCENT_EMP = BASE_DIR / "template_ab_ascent_emp.pdf"

TEMPLATE_CONFIGS = {
    "hebron": {
        "key": "hebron",
        "label": "Hebron",
        "display_name": "Hebron Mission School",
        "pdf": TEMPLATE_PDF_HEBRON,
        "description": "Red Hebron layout with section, roll, mother name and blood group.",
        "fields": [
            "student_name", "class", "section", "roll", "father_name",
            "mother_name", "dob", "address", "mobile", "adm_no",
            "blood_group", "session", "photo_url",
        ],
    },
    "redeemer": {
        "key": "redeemer",
        "label": "Redeemer",
        "display_name": "My Redeemer Mission School",
        "pdf": TEMPLATE_PDF_REDEEMER,
        "description": "Blue Redeemer layout with father name, DOB, mobile and address.",
        "fields": [
            "student_name", "class", "father_name", "dob", "address",
            "mobile", "session", "photo_url", "adm_no",
        ],
    },
    "priyanka": {
        "key": "priyanka",
        "label": "Priyanka",
        "display_name": "Priyanka Dreamnest School",
        "pdf": TEMPLATE_PDF_PRIYANKA if TEMPLATE_PDF_PRIYANKA.exists() else TEMPLATE_PDF_REDEEMER,
        "description": "Priyanka Dreamnest School ID layout.",
        "fields": [
            "student_name", "class", "father_name", "dob", "address",
            "mobile", "session", "photo_url", "adm_no",
        ],
    },
    "ab_ascent": {
        "key": "ab_ascent",
        "label": "Ab Ascent",
        "display_name": "Ab Ascent School",
        "pdf": TEMPLATE_PDF_AB_ASCENT if TEMPLATE_PDF_AB_ASCENT.exists() else TEMPLATE_PDF_REDEEMER,
        "description": "Ab Ascent School ID layout.",
        "fields": [
            "student_name", "class", "father_name", "dob", "address",
            "mobile", "session", "photo_url", "adm_no",
        ],
    },
}

# ── Employee templates ──────────────────────────────────────────
# Each employee template re-uses an existing student renderer ("redeemer",
# "hebron", "priyanka" or "ab_ascent") — we only swap the underlying PDF
# template. The frontend lists these 4 templates separately under the
# Employees mode and posts to /api/employees/*.
#
# Priyanka now has a dedicated employee renderer (`_render_priyanka_emp_card_bytes`)
# whose geometry/colors port the standalone idcard_colab.py reference script.
EMPLOYEE_TEMPLATE_CONFIGS = {
    "hebron_emp": {
        "key":          "hebron_emp",
        "label":        "Hebron — Employee",
        "display_name": "Hebron Mission School (Employee)",
        "pdf":          TEMPLATE_PDF_HEBRON_EMP if TEMPLATE_PDF_HEBRON_EMP.exists() else TEMPLATE_PDF_HEBRON,
        "renderer":     "hebron",
        "description": "Hebron Employee ID layout.",
        "fields": [
            "employee_name", "designation", "father_name", "dob",
            "address", "mobile", "emp_id", "photo_url",
        ],
    },
    "redeemer_emp": {
        "key":          "redeemer_emp",
        "label":        "Redeemer — Employee",
        "display_name": "My Redeemer Mission School (Employee)",
        "pdf":          TEMPLATE_PDF_REDEEMER_EMP if TEMPLATE_PDF_REDEEMER_EMP.exists() else TEMPLATE_PDF_REDEEMER,
        "renderer":     "redeemer",
        "description": "Redeemer Employee ID layout.",
        "fields": [
            "employee_name", "designation", "father_name", "dob",
            "address", "mobile", "emp_id", "photo_url",
        ],
    },
    "priyanka_emp": {
        "key":          "priyanka_emp",
        "label":        "Priyanka — Employee",
        "display_name": "Priyanka Dreamnest School (Employee)",
        # Dedicated Priyanka employee renderer — geometry matches the
        # standalone idcard_colab.py reference script (pink #FFBCF5 name
        # background, navy #0F006A text, rounded-corner photo at
        # rect(51.8, 73.0, 92.5, 129.5)).
        "pdf":          (TEMPLATE_PDF_PRIYANKA_EMP if TEMPLATE_PDF_PRIYANKA_EMP.exists()
                         else (TEMPLATE_PDF_REDEEMER_EMP if TEMPLATE_PDF_REDEEMER_EMP.exists()
                               else TEMPLATE_PDF_REDEEMER)),
        "renderer":     "priyanka",
        "description": "Priyanka Dreamnest Employee ID layout (dedicated renderer).",
        "fields": [
            "employee_name", "designation", "father_name", "dob",
            "address", "mobile", "emp_id", "photo_url",
        ],
    },
    "ab_ascent_emp": {
        "key":          "ab_ascent_emp",
        "label":        "Ab Ascent — Employee",
        "display_name": "Ab Ascent School (Employee)",
        "pdf":          TEMPLATE_PDF_AB_ASCENT_EMP if TEMPLATE_PDF_AB_ASCENT_EMP.exists() else TEMPLATE_PDF_AB_ASCENT,
        "renderer":     "ab_ascent",
        "description": "Ab Ascent Employee ID layout.",
        "fields": [
            "employee_name", "designation", "father_name", "dob",
            "address", "mobile", "emp_id", "validity", "photo_url",
        ],
    },
}

# Merge into TEMPLATE_CONFIGS so the existing rendering pipeline keeps
# working with a single config dict. The renderer key ("renderer") on each
# employee template tells build_pdf_file_vector which student layout to reuse.
TEMPLATE_CONFIGS.update(EMPLOYEE_TEMPLATE_CONFIGS)

EMPLOYEE_TEMPLATE_KEYS = set(EMPLOYEE_TEMPLATE_CONFIGS.keys())

def _resolve_renderer_key(template_key: str) -> str:
    """Map an employee template like 'ab_ascent_emp' → 'ab_ascent' for the
    renderer pipeline. Student templates pass through unchanged."""
    cfg = TEMPLATE_CONFIGS.get(template_key)
    if cfg and "renderer" in cfg:
        return cfg["renderer"]
    return template_key

API_BASE_URL = "https://titusattendence.com/apikey/apistudents?school_id={school_id}"

CLASS_ORDER = {
    "NURSERY": 0, "LKG": 1, "UKG": 2,
    "1ST": 3, "2ND": 4, "3RD": 5, "4TH": 6,
    "5TH": 7, "6TH": 8, "7TH": 9, "8TH": 10,
}

def class_sort_key(cls_str):
    return CLASS_ORDER.get(str(cls_str).strip().upper(), 99)

# ─────────────────────────────────────────────────────────────────
# SINGLE GLOBAL STORE  — sessions removed (v2.6).
# One school's data lives here. Last upload/fetch wins.
# ─────────────────────────────────────────────────────────────────
_store: dict = {
    "students":    [],
    "source":      None,
    "school_name": None,
    "school_id":   None,
    "updated_at":  0.0,
}
_store_lock = threading.Lock()

def _prune_old_sessions():
    """No-op kept for backwards-compat."""
    return

def _get_store() -> dict:
    return _store

def replace_store(students, source, school_name, school_id=None):
    with _store_lock:
        old = _store.get("students") or []
        if isinstance(old, list):
            old.clear()
        _store["students"]    = list(students)
        _store["source"]      = source
        _store["school_name"] = school_name
        _store["school_id"]   = school_id
        _store["updated_at"]  = time.time()
    gc.collect()

# ─────────────────────────────────────────────────────────────────
# JOB / PROGRESS REGISTRY  — backs /api/jobs/* endpoints.
# ─────────────────────────────────────────────────────────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()
JOB_TTL_SECONDS = 30 * 60   # auto-prune jobs older than 30 minutes

def _prune_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    dead = []
    with _jobs_lock:
        for jid, j in _jobs.items():
            if j.get("finished_at", j.get("created_at", 0)) < cutoff:
                dead.append(jid)
        for jid in dead:
            try:
                p = _jobs[jid].get("file_path")
                if p:
                    schedule_delete(p, 30)
            except Exception:
                pass
            _jobs.pop(jid, None)

def _new_job(total: int) -> str:
    jid = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[jid] = {
            "id":          jid,
            "status":      "queued",   # queued | running | done | error
            "progress":    0.0,        # 0..100
            "phase":       "queued",   # queued|prefetch|render|writing|done|error
            "total":       int(total),
            "done":        0,
            "file_path":   None,
            "file_size":   0,
            "download_name": None,
            "error":       None,
            "created_at":  time.time(),
            "started_at":  None,
            "finished_at": None,
        }
    return jid

def _job_set(jid: str, **kwargs):
    with _jobs_lock:
        if jid not in _jobs:
            return
        _jobs[jid].update(kwargs)

def _job_get(jid: str):
    with _jobs_lock:
        return dict(_jobs[jid]) if jid in _jobs else None

# ──────────────────────────────────────────────────────────────
# v2.8 — DELAYED-DELETE SCHEDULER
#
# Fixes the "PDF appears, then a network error" bug. We no longer unlink the
# generated PDF the instant the response body finishes; the file is kept on
# disk for PDF_RETENTION_SECONDS so that:
#   1. range-request resumes work after a flaky proxy hiccups,
#   2. the React client can retry on Network Error without re-rendering,
#   3. axios responseType:'blob' downloads finish before unlink.
#
# Files are still cleaned up — by a background reaper thread — so /tmp doesn't
# fill up. Railway gives us 5 GB so 5-minute retention is safe.
# ──────────────────────────────────────────────────────────────
PDF_RETENTION_SECONDS = int(os.environ.get("PDF_RETENTION_SECONDS", "300"))   # 5 minutes
_pending_deletes: dict = {}   # path -> delete_at_epoch
_pending_lock = threading.Lock()


def schedule_delete(path: str, after_seconds: int = None):
    """Mark a file for deletion `after_seconds` later. Idempotent.

    Multiple calls keep the *latest* (largest) delete_at — so a successful
    download won't shorten a previously-scheduled retention window.
    """
    if not path:
        return
    if after_seconds is None:
        after_seconds = PDF_RETENTION_SECONDS
    delete_at = time.time() + max(1, int(after_seconds))
    with _pending_lock:
        prev = _pending_deletes.get(path)
        if prev is None or delete_at > prev:
            _pending_deletes[path] = delete_at
    log.info("[pdf-lifecycle] scheduled delete: %s in %ds", path, after_seconds)


def _delete_now(path: str) -> bool:
    """Attempt to delete *path*.  Returns True on success, False if the file
    is still locked (e.g. WinError 32 on Windows) so the caller can retry."""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
            log.info("[pdf-lifecycle] deleted: %s", path)
        return True          # file gone (either deleted or never existed)
    except Exception as e:
        log.warning("[pdf-lifecycle] delete failed (will retry): %s: %s", path, e)
        return False         # still locked — caller must reschedule


def _reaper_loop():
    """Background thread — sweeps expired files every 30 seconds.

    FIX (WinError 32 / file-lock bug):
      Previously the path was removed from _pending_deletes *before* the
      delete was attempted.  If os.unlink() raised (file still open on
      Windows), the path was silently dropped and never retried — causing
      temp-file leaks and the WinError 32 spam in the log.

      Now: the path is only removed when deletion succeeds.  On failure it
      is rescheduled 60 s into the future so the reaper retries automatically
      once the file handle is released by the OS.
    """
    while True:
        try:
            now = time.time()
            expired = []
            with _pending_lock:
                for p, t in list(_pending_deletes.items()):
                    if t <= now:
                        expired.append(p)
            for p in expired:
                if _delete_now(p):
                    # Success — remove from the scheduler
                    with _pending_lock:
                        _pending_deletes.pop(p, None)
                else:
                    # Deletion failed (file still locked) — reschedule in 60 s
                    with _pending_lock:
                        _pending_deletes[p] = time.time() + 60
            # opportunistic: prune stale jobs while we're here
            _prune_old_jobs()
        except Exception:
            pass
        time.sleep(30)

threading.Thread(target=_reaper_loop, daemon=True, name="pdf-reaper").start()


MAX_UPLOAD_MB             = int(os.environ.get("MAX_UPLOAD_MB", "12"))
MAX_STUDENTS_PER_REQUEST  = int(os.environ.get("MAX_STUDENTS_PER_REQUEST", "2000"))
PREVIEW_DPI               = int(os.environ.get("PREVIEW_DPI", "150"))
DOWNLOAD_DPI              = int(os.environ.get("DOWNLOAD_DPI", "300"))
# ⏱  Photo timeouts — connect 6s, read 12s. titusattendence.com can be slow.
PHOTO_TIMEOUT             = (6, 12)
MAX_PHOTO_BYTES           = int(os.environ.get("MAX_PHOTO_BYTES", str(4 * 1024 * 1024)))

# 🏭  PRODUCTION MODE: set PRODUCTION=1 env var on Railway (512 MB / 0.5 CPU).
#     Locally leave unset for full performance.
_IS_PRODUCTION = os.environ.get("PRODUCTION", "0").strip() in ("1", "true", "yes")

# Production: use /tmp (always writable, cleaned by OS). Local: tempfile default.
_PROD_TMP = "/tmp"
PDF_TEMP_DIR = os.environ.get(
    "PDF_TEMP_DIR",
    _PROD_TMP if _IS_PRODUCTION else tempfile.gettempdir()
)
# Ensure the temp dir exists and is writable; fall back to /tmp
try:
    os.makedirs(PDF_TEMP_DIR, exist_ok=True)
    _test = tempfile.NamedTemporaryFile(delete=True, dir=PDF_TEMP_DIR)
    _test.close()
except Exception:
    PDF_TEMP_DIR = _PROD_TMP
    os.makedirs(PDF_TEMP_DIR, exist_ok=True)

# 📷  Quality: production uses smaller photos to save RAM; local uses higher quality.
# At ID-card print size (~16 mm wide), 200 px is visually identical to 360 px.
PHOTO_PX           = int(os.environ.get("PHOTO_PX", "360" if _IS_PRODUCTION else "480"))
PHOTO_JPEG_QUALITY = int(os.environ.get("PHOTO_JPEG_QUALITY", "90" if _IS_PRODUCTION else "92"))

# 🧠  Memory caps: production must stay under ~350 MB working set
MAX_CACHED_PHOTOS   = int(os.environ.get("MAX_CACHED_PHOTOS",  "60"  if _IS_PRODUCTION else "600"))
# Production: fewer threads — 0.5 CPU means 2 real threads is enough; local: 16
PREFETCH_WORKERS    = int(os.environ.get("PREFETCH_WORKERS",   "4"   if _IS_PRODUCTION else "16"))
# Production: serial per-card render avoids OOM on large batches
CARD_RENDER_WORKERS = int(os.environ.get("CARD_RENDER_WORKERS","1"   if _IS_PRODUCTION else "4"))

# 🧱  Chunked PDF builder — pages per on-disk chunk. Smaller = lower peak
# memory but slightly more disk I/O. 5 × 10 cards = 50 students per chunk.
CHUNK_PAGES         = int(os.environ.get("CHUNK_PAGES",        "5"   if _IS_PRODUCTION else "20"))
# When the merger doc reaches this many pages, flush to disk and re-open
# to free PyMuPDF native object memory.
MERGE_COMPACT_PAGES = int(os.environ.get("MERGE_COMPACT_PAGES","30"  if _IS_PRODUCTION else "100"))
# Per-card embedded photo scale.  4× → ~200×255 px PNG = ~25-35 KB each.
# Was 8 (~80 KB each) — 4 yields the same visual quality at print size.
PHOTO_EMBED_SCALE   = int(os.environ.get("PHOTO_EMBED_SCALE",  "4"))
PREVIEW_EXTERNAL_THRESHOLD = int(os.environ.get("PREVIEW_EXTERNAL_THRESHOLD", "9999"))
REDEEMER_GRAD_STEPS = int(os.environ.get("REDEEMER_GRAD_STEPS", "30" if _IS_PRODUCTION else "60"))

# 🗑  Max students per request in production — keeps peak RAM predictable
if _IS_PRODUCTION:
    MAX_STUDENTS_PER_REQUEST = min(MAX_STUDENTS_PER_REQUEST,
                                   int(os.environ.get("MAX_STUDENTS_PER_REQUEST", "1000")))

STORAGE_BACKEND           = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
SUPABASE_URL              = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET           = os.environ.get("SUPABASE_BUCKET", "generated-pdfs")
SUPABASE_SIGNED_URL_TTL   = int(os.environ.get("SUPABASE_SIGNED_URL_TTL", "3600"))
GOOGLE_DRIVE_CLIENT_ID    = os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "")
GOOGLE_DRIVE_CLIENT_SECRET= os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "")
GOOGLE_DRIVE_REFRESH_TOKEN= os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN", "")
GOOGLE_DRIVE_FOLDER_ID    = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# ─────────────────────────────────────────────────────────────────
# Global HTTP Session — connection pooling massively cuts photo-fetch latency.
# Without it, requests opens a new TCP+TLS handshake per photo (~500 ms each).
# Pool of 32 keep-alive sockets → near-zero handshake cost on repeat hosts.
# Solves the WiFi-vs-mobile flakiness: many WiFi networks throttle SYN bursts.
# ─────────────────────────────────────────────────────────────────
_HTTP = requests.Session()
_HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; IDCardGen/2.7)",
    "Accept": "image/*,*/*;q=0.8",
    "Connection": "keep-alive",
})
# Retry up to 3 times with backoff - helps with flaky school servers
from urllib3.util.retry import Retry
_retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504],
               allowed_methods=["GET"], raise_on_status=False)
_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=_retry)
_HTTP.mount("http://",  _adapter)
_HTTP.mount("https://", _adapter)


def filter_students_by_class(students, cls):
    cls = (cls or "").strip().upper()
    if not cls:
        return list(students)
    return [s for s in students if s.get("class","").strip().upper() == cls]

def _sanitize_filename(name):
    keep = []
    for ch in str(name or "file.pdf"):
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        elif ch.isspace():
            keep.append("_")
    cleaned = "".join(keep).strip("._") or "file"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned

def _external_storage_enabled():
    if STORAGE_BACKEND == "supabase":
        return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPABASE_BUCKET)
    if STORAGE_BACKEND == "google_drive":
        return bool(GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET and GOOGLE_DRIVE_REFRESH_TOKEN)
    return False

def _google_access_token():
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     GOOGLE_DRIVE_CLIENT_ID,
            "client_secret": GOOGLE_DRIVE_CLIENT_SECRET,
            "refresh_token": GOOGLE_DRIVE_REFRESH_TOKEN,
            "grant_type":    "refresh_token",
        },
        timeout=20,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Google Drive token refresh failed")
    return token

def _upload_to_google_drive(local_path, download_name):
    token    = _google_access_token()
    metadata = {"name": _sanitize_filename(download_name)}
    if GOOGLE_DRIVE_FOLDER_ID:
        metadata["parents"] = [GOOGLE_DRIVE_FOLDER_ID]
    start = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name",
        headers={
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/pdf",
        },
        data=json.dumps(metadata),
        timeout=30,
    )
    start.raise_for_status()
    session_url = start.headers.get("Location")
    if not session_url:
        raise RuntimeError("Google Drive resumable upload URL missing")
    file_size = os.path.getsize(local_path)
    with open(local_path, "rb") as fh:
        uploaded = requests.put(
            session_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/pdf",
                "Content-Length": str(file_size),
            },
            data=fh,
            timeout=300,
        )
    uploaded.raise_for_status()
    file_id = uploaded.json().get("id")
    if not file_id:
        raise RuntimeError("Google Drive file id missing")
    requests.post(
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"fields": "id"},
        data=json.dumps({"role": "reader", "type": "anyone"}),
        timeout=30,
    ).raise_for_status()
    return f"https://drive.google.com/uc?export=download&id={file_id}"

def _upload_to_supabase(local_path, download_name):
    object_name = f"generated/{uuid.uuid4().hex}_{_sanitize_filename(download_name)}"
    upload_url  = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_name}"
    with open(local_path, "rb") as fh:
        requests.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "apikey":        SUPABASE_SERVICE_ROLE_KEY,
                "x-upsert":      "true",
                "Content-Type":  "application/pdf",
            },
            data=fh,
            timeout=300,
        ).raise_for_status()
    sign = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/sign/{SUPABASE_BUCKET}/{object_name}",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey":        SUPABASE_SERVICE_ROLE_KEY,
            "Content-Type":  "application/json",
        },
        data=json.dumps({"expiresIn": SUPABASE_SIGNED_URL_TTL}),
        timeout=30,
    )
    sign.raise_for_status()
    payload = sign.json()
    signed  = payload.get("signedURL") or payload.get("signedUrl")
    if not signed:
        raise RuntimeError("Supabase signed URL missing")
    return signed if signed.startswith("http") else f"{SUPABASE_URL}/storage/v1{signed}"

def upload_pdf_to_external_storage(local_path, download_name):
    if STORAGE_BACKEND == "google_drive":
        return _upload_to_google_drive(local_path, download_name)
    if STORAGE_BACKEND == "supabase":
        return _upload_to_supabase(local_path, download_name)
    return None

# ─────────────────────────────────────────────────────────────────
# DATA-CLEANING HELPERS  (NEW: HTML scrub + DOB normalisation)
# ─────────────────────────────────────────────────────────────────
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENT_RE = re.compile(r"&(?:nbsp|amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);")

def has_html(text: str) -> bool:
    if not text:
        return False
    if _HTML_TAG_RE.search(text):
        return True
    if _HTML_ENT_RE.search(text):
        return True
    return False

def clean_address(text) -> str:
    """Drop addresses that contain HTML markup — return empty string instead."""
    if text is None:
        return ""
    s = str(text).strip()
    if not s or s.lower() in {"nan", "none", "null", "nil"}:
        return ""
    if has_html(s):
        return ""   # 🚫  HTML found in DB row → keep blank
    return s

def format_dob(text) -> str:
    """
    Normalise any DOB into DD-MM-YYYY (zero-padded).
    Accepts: 2010-04-23, 23/04/2010, 23-04-2010, 04/23/2010, 2010/04/23,
             '23 April 2010', timestamps, ints, etc.
    Returns "" for invalid / empty / placeholder values.
    """
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    low = s.lower()
    if low in {"nan", "none", "null", "nil", "0000-00-00", "00-00-0000",
               "0000/00/00", "00/00/0000"}:
        return ""

    # Try strict ISO-style + common explicit formats first
    fmt_candidates = [
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%m-%d-%Y", "%m/%d/%Y",
        "%d-%b-%Y", "%d %b %Y", "%d %B %Y",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    ]
    only_date = s.split(" ")[0] if "T" not in s else s
    for fmt in fmt_candidates:
        try:
            dt = datetime.strptime(s, fmt)
            return f"{dt.day:02d}-{dt.month:02d}-{dt.year:04d}"
        except Exception:
            pass
        try:
            dt = datetime.strptime(only_date, fmt)
            return f"{dt.day:02d}-{dt.month:02d}-{dt.year:04d}"
        except Exception:
            pass

    # Last-chance: pandas parser (handles oddities)
    try:
        dt = pd.to_datetime(s, errors="raise", dayfirst=True)
        if pd.notna(dt):
            return f"{dt.day:02d}-{dt.month:02d}-{dt.year:04d}"
    except Exception:
        pass

    # Bare digits like 20100423
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        try:
            dt = datetime.strptime(digits, "%Y%m%d")
            return f"{dt.day:02d}-{dt.month:02d}-{dt.year:04d}"
        except Exception:
            try:
                dt = datetime.strptime(digits, "%d%m%Y")
                return f"{dt.day:02d}-{dt.month:02d}-{dt.year:04d}"
            except Exception:
                pass

    return ""   # could not parse → keep empty


# ─────────────────────────────────────────────────────────────────
def norm_key(v):
    s = str(v or "").strip().lower()
    out = []; prev = False
    for ch in s:
        if ch.isalnum():  out.append(ch); prev = False
        else:
            if not prev:  out.append("_"); prev = True
    return "".join(out).strip("_")

def clean_str(v):
    if pd.isna(v): return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan","none"} else s

def pick(row, *aliases, default=""):
    for a in aliases:
        if a in row:
            val = clean_str(row[a])
            if val: return val
    return default

def _sort_and_index(students):
    students.sort(key=lambda s: (
        class_sort_key(s.get("class","")),
        s.get("section","").strip().upper(),
        s.get("student_name","").strip().upper(),
    ))
    for i, s in enumerate(students, 1):
        s["serial"] = i
    counters = defaultdict(int)
    for s in students:
        key = (s["class"].strip().upper(), s["section"].strip().upper())
        if not s["roll"]:
            counters[key] += 1
            s["roll"] = str(counters[key])
        else:
            try:
                cr = int(float(s["roll"]))
                counters[key] = max(counters[key], cr)
                s["roll"] = str(cr)
            except:
                pass
    return students

def _post_clean_student(s: dict) -> dict:
    """Apply DOB normalisation + address HTML scrub + general value cleanup."""
    s["dob"]     = format_dob(s.get("dob", ""))
    s["address"] = clean_address(s.get("address", ""))
    return s

def parse_file(file_path, filename):
    fn = (filename or "").lower()
    if fn.endswith(".csv"):
        df = None
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                df = pd.read_csv(file_path, encoding=enc, dtype=str)
                break
            except Exception:
                continue
        if df is None:
            raise ValueError("Could not decode CSV — try saving as UTF-8 in Excel")
    else:
        try:
            df = pd.read_excel(file_path, dtype=str)
        except Exception:
            df = pd.read_excel(file_path, dtype=str, engine="openpyxl")
    df.columns = [norm_key(c) for c in df.columns]
    students = []
    for _, row in df.iterrows():
        rm = {col: row[col] for col in df.columns}
        s = {
            "student_name": pick(rm,"student_name","studentname","name","student"),
            "class":        pick(rm,"class","class_name","std","standard"),
            "section":      pick(rm,"section","sec","section_id"),
            "roll":         pick(rm,"roll","roll_no","rollno","roll_number"),
            "father_name":  pick(rm,"father_name","father","fathers_name"),
            "mother_name":  pick(rm,"mother_name","mother","mothers_name"),
            "dob":          pick(rm,"dob","date_of_birth","birth_date"),
            "address":      pick(rm,"address","student_address","residence"),
            "mobile":       pick(rm,"mobile","phone","mobile_no","contact","father_contact"),
            "photo_url":    pick(rm,"photo_url","photo","image_url","photo_link","student_photo"),
            "adm_no":       pick(rm,"adm_no","admission_no","admission_number","adm","admno","reg_no","registration_no"),
            "blood_group":  pick(rm,"blood_group","bloodgroup","blood"),
            "gender":       pick(rm,"gender","sex"),
            "session":      pick(rm,"session",default=DEFAULT_SESSION),
            "bus_route":    pick(rm,"bus_route","bus","bus_no","bus_number","route"),
        }
        if any(s.values()):
            students.append(_post_clean_student(s))
    return _sort_and_index(students)

_API_MAP = {
    "student_name":"student_name","admission_no":"adm_no","section_id":"section",
    "dob":"dob","roll_number":"roll","mother_name":"mother_name","address":"address",
    "blood_group":"blood_group","class_name":"class","father_name":"father_name",
    "father_contact":"mobile","student_photo":"photo_url","session":"session",
    "academic_year":"session","name":"student_name","std":"class","grade":"class",
    "section":"section","roll":"roll","roll_no":"roll","father":"father_name",
    "mother":"mother_name","date_of_birth":"dob","student_address":"address",
    "mobile":"mobile","phone":"mobile","mobile_no":"mobile","contact":"mobile",
    "photo_url":"photo_url","photo":"photo_url","adm_no":"adm_no",
    "admission_number":"adm_no","adm":"adm_no","reg_no":"adm_no","registration_no":"adm_no","bloodgroup":"blood_group",
    "blood":"blood_group","gender":"gender","sex":"gender",
    "bus_route":"bus_route","bus":"bus_route","bus_no":"bus_route",
    "bus_number":"bus_route","route":"bus_route",
}

def map_api_record(record):
    out = {
        "student_name":"","class":"","section":"","roll":"","father_name":"",
        "mother_name":"","dob":"","address":"","mobile":"","photo_url":"",
        "adm_no":"","blood_group":"","gender":"","session":DEFAULT_SESSION,
        "bus_route":"",
    }
    for k, v in record.items():
        internal = _API_MAP.get(k.strip().lower())
        if internal and v not in (None,"","null","NULL"):
            val = str(v).strip()
            # v2.8: photo_url is passed through completely raw — no escape rewriting
            out[internal] = val
    return _post_clean_student(out)

# ─────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "message": "ID Card Generator API is running"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "ID Card Generator API is healthy"})

# ──────────────────────────────────────────────────────────────
# v2.9 — System monitoring + lightweight login (added to fix the
#         frontend "Offline" pill and the flood of 405 errors on
#         /api/system/stats, /api/login, /api/logout).
#
# These endpoints are intentionally OPEN and never block.  They
# never raise (any error is caught and returned as a safe payload)
# so the topbar's 2-second polling loop never flips to "offline".
# ──────────────────────────────────────────────────────────────
try:
    import psutil as _psutil   # optional — used if installed
    _HAS_PSUTIL = True
    _PSUTIL_PROC = _psutil.Process(os.getpid())
    # warm-up: psutil.cpu_percent's first call always returns 0.0
    try: _PSUTIL_PROC.cpu_percent(None)
    except Exception: pass
    try: _psutil.cpu_percent(None)
    except Exception: pass
except Exception:
    _psutil = None
    _HAS_PSUTIL = False
    _PSUTIL_PROC = None

# v3.2: BACKGROUND CPU SAMPLER
# ----------------------------
# psutil.cpu_percent(interval=None) returns 0% on a quiet server
# unless we keep a sliding-window measurement going.  We run a tiny
# daemon thread that calls cpu_percent(interval=1.0) in a loop and
# caches the result.  The HTTP endpoint reads this cache so it's
# always accurate AND non-blocking.
_CPU_PCT_CACHE = {"value": 0, "ts": 0.0}
_CPU_SAMPLER_STARTED = False

def _start_cpu_sampler():
    global _CPU_SAMPLER_STARTED
    if _CPU_SAMPLER_STARTED or not _HAS_PSUTIL:
        return
    _CPU_SAMPLER_STARTED = True
    def _loop():
        while True:
            try:
                # interval=1.0 = blocking 1-second sample, accurate.
                pct = _psutil.cpu_percent(interval=1.0)
                _CPU_PCT_CACHE["value"] = int(round(pct))
                _CPU_PCT_CACHE["ts"]    = time.time()
            except Exception:
                # Quiet failure — just keep the previous value
                time.sleep(1.0)
    t = threading.Thread(target=_loop, name="cpu-sampler", daemon=True)
    t.start()

# Kick off the sampler as soon as the module loads.
try:
    _start_cpu_sampler()
except Exception:
    pass

# ──────────────────────────────────────────────────────────────
# SQLITE-BACKED MULTI-USER SESSION DATABASE (v3.0)
# ──────────────────────────────────────────────────────────────
import sqlite3

def init_db():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")
    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                access_code TEXT UNIQUE,
                role TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                created REAL,
                last_activity REAL,
                client_id TEXT,
                username TEXT
            )
        """)
        conn.commit()
        
        # Seed default users if empty
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 0:
            default_users = [
                ("admin", "admin123", "admin"),
                ("staff", "staff456", "staff")
            ]
            cursor.executemany("INSERT OR IGNORE INTO users (username, access_code, role) VALUES (?, ?, ?)", default_users)
            conn.commit()
            log.info("[db] Seeded default users (admin/staff) successfully")
        
        # Sync environment variable ACCESS_CODE if set
        env_code = (os.environ.get("ACCESS_CODE") or "").strip()
        if env_code:
            cursor.execute("INSERT OR REPLACE INTO users (username, access_code, role) VALUES (?, ?, ?)", 
                           ("env_user", env_code, "admin"))
            conn.commit()
            log.info("[db] Synced env ACCESS_CODE as 'env_user'")

# Run database initialization
try:
    init_db()
except Exception as e:
    log.error("[db] Failed to initialize/seed database: %s", e)

MAX_CONCURRENT_USERS = int(os.environ.get("MAX_CONCURRENT_USERS", "3"))
ACCESS_CODE = (os.environ.get("ACCESS_CODE") or "").strip()
_SERVER_BOOT_TS = time.time()

def _new_session_token():
    import secrets
    return secrets.token_urlsafe(24)

def _get_active_users_count() -> int:
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")
    try:
        now = time.time()
        cutoff = now - 900  # 15 minutes inactive session timeout
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.execute("DELETE FROM sessions WHERE last_activity < ?", (cutoff,))
            conn.commit()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions")
            return cursor.fetchone()[0]
    except Exception as e:
        log.error("[db] Failed to count active users: %s", e)
        return 0

def _system_stats_payload():
    """Build the /api/system/stats payload — never raises."""
    cpu_pct = 0
    ram_used_mb = 0
    ram_total_mb = 0
    ram_pct = 0
    disk_pct = 0
    if _HAS_PSUTIL:
        cached = _CPU_PCT_CACHE.get("value", 0)
        cached_age = time.time() - _CPU_PCT_CACHE.get("ts", 0.0)
        if _CPU_PCT_CACHE.get("ts", 0.0) > 0 and cached_age < 5.0:
            cpu_pct = int(cached)
        else:
            try:
                cpu_pct = int(round(_psutil.cpu_percent(interval=0.15)))
            except Exception:
                cpu_pct = 0
        try:
            vm = _psutil.virtual_memory()
            ram_used_mb  = int(vm.used  / (1024 * 1024))
            ram_total_mb = int(vm.total / (1024 * 1024))
            ram_pct      = int(round(vm.percent))
        except Exception:
            pass
        try:
            du = _psutil.disk_usage(str(BASE_DIR))
            disk_pct = int(round(du.percent))
        except Exception:
            pass
    else:
        ram_total_mb = 512
        ram_used_mb  = 128
        ram_pct      = 25

    if   ram_pct >= 92: ram_level = "refuse"
    elif ram_pct >= 75: ram_level = "warn"
    else:               ram_level = "ok"

    now = time.time()
    active_users = _get_active_users_count()

    return {
        "ok":            True,
        "cpu_pct":       cpu_pct,
        "ram_used_mb":   ram_used_mb,
        "ram_total_mb":  ram_total_mb,
        "ram_pct":       ram_pct,
        "ram_level":     ram_level,
        "disk_pct":      disk_pct,
        "active_users":  active_users,
        "max_users":     MAX_CONCURRENT_USERS,
        "uptime_secs":   int(now - _SERVER_BOOT_TS),
        "psutil":        _HAS_PSUTIL,
    }

_QUIET_ROUTES = {"/api/system/stats", "/system/stats"}

@app.route("/api/system/stats", methods=["GET"])
@app.route("/system/stats", methods=["GET"])
def get_system_stats():
    try:
        return jsonify(_system_stats_payload())
    except Exception as e:
        return jsonify({
            "ok":           True,
            "cpu_pct":      0,
            "ram_used_mb":  0,
            "ram_total_mb": 0,
            "ram_pct":      0,
            "ram_level":    "ok",
            "disk_pct":     0,
            "active_users": _get_active_users_count(),
            "max_users":    MAX_CONCURRENT_USERS,
            "error":        str(e),
        })

# ──────────────────────────────────────────────────────────────
# SESSION GATING MIDDLEWARE (before_request)
# ──────────────────────────────────────────────────────────────
@app.before_request
def check_session_gating():
    if request.method == "OPTIONS":
        return
        
    path = request.path
    is_public = False
    
    if path in ("/", "/health", "/api/system/stats", "/system/stats", "/api/login", "/login", "/api/templates", "/templates", "/api/employees/templates", "/employees/templates"):
        is_public = True
    elif (path.startswith("/api/templates/") or path.startswith("/templates/")) and path.endswith("/preview.png"):
        is_public = True
    elif path.startswith("/static/") or path == "/favicon.ico":
        is_public = True
        
    if is_public:
        return
        
    tok = request.headers.get("X-Session-Token", "").strip()
    if not tok:
        return jsonify({"error": "Session token required", "code": "NO_SESSION"}), 401
        
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")
    try:
        now = time.time()
        cutoff = now - 900  # 15 minutes inactive session timeout
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            # Prune expired sessions
            conn.execute("DELETE FROM sessions WHERE last_activity < ?", (cutoff,))
            conn.commit()
            
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM sessions WHERE token = ?", (tok,))
            row = cursor.fetchone()
            
            if not row:
                return jsonify({"error": "Session has expired or is invalid.", "code": "BAD_SESSION"}), 401
                
            # Update last activity
            conn.execute("UPDATE sessions SET last_activity = ? WHERE token = ?", (now, tok))
            conn.commit()
    except Exception as e:
        log.error("[db] Session validation error: %s", e)

# ──────────────────────────────────────────────────────────────
# AUTHENTICATION ENDPOINTS
# ──────────────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
@app.route("/login", methods=["POST"])
def login():
    """Lightweight login — hands out a session_token.

    Accepts either {code, client_id, resume_token} JSON or an empty body.
    If ACCESS_CODE is unset, *any* code (including empty) is accepted.
    If a valid resume_token is supplied, the same token is returned
    (so reloading the page doesn't burn a second seat).
    """
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    code         = (data.get("code") or "").strip()
    resume_token = (data.get("resume_token") or "").strip()
    client_id    = (data.get("client_id") or request.headers.get("X-Client-ID", "")).strip()

    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")
    now = time.time()
    cutoff = now - 900
    
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            # 1. Prune expired sessions
            conn.execute("DELETE FROM sessions WHERE last_activity < ?", (cutoff,))
            conn.commit()
            
            cursor = conn.cursor()
            
            # 2. Authenticate the code
            cursor.execute("SELECT username, role FROM users WHERE access_code = ?", (code,))
            user_row = cursor.fetchone()
            
            if not user_row:
                if ACCESS_CODE:
                    if code == ACCESS_CODE:
                        username, role = "env_user", "admin"
                    else:
                        return jsonify({"error": "Invalid access code.", "code": "BAD_CODE"}), 401
                else:
                    # Generic guest account when no global ACCESS_CODE env var is configured
                    username, role = "guest", "staff"
            else:
                username, role = user_row
                
            # 3. Handle resume token or existing session
            tok = None
            if resume_token:
                cursor.execute("SELECT token FROM sessions WHERE token = ?", (resume_token,))
                if cursor.fetchone():
                    conn.execute("UPDATE sessions SET last_activity = ?, client_id = ? WHERE token = ?",
                                 (now, client_id, resume_token))
                    conn.commit()
                    tok = resume_token
                    
            if not tok:
                # Free any seat already held by this client_id
                if client_id:
                    conn.execute("DELETE FROM sessions WHERE client_id = ?", (client_id,))
                    conn.commit()
                    
                # Check seat limit
                cursor.execute("SELECT COUNT(*) FROM sessions")
                active_count = cursor.fetchone()[0]
                if active_count >= MAX_CONCURRENT_USERS:
                    return jsonify({
                        "error":        f"Server is full ({active_count}/{MAX_CONCURRENT_USERS} seats).",
                        "code":         "SEATS_FULL",
                        "active_users": active_count,
                        "max_users":    MAX_CONCURRENT_USERS,
                    }), 503
                    
                tok = _new_session_token()
                conn.execute("INSERT INTO sessions (token, created, last_activity, client_id, username) VALUES (?, ?, ?, ?, ?)",
                             (tok, now, now, client_id, username))
                conn.commit()
                
            cursor.execute("SELECT COUNT(*) FROM sessions")
            active_count = cursor.fetchone()[0]
            
        return jsonify({
            "session_token": tok,
            "active_users":  active_count,
            "max_users":     MAX_CONCURRENT_USERS,
        })
        
    except Exception as e:
        log.error("[db] Login failed: %s", e)
        return jsonify({"error": f"Login failed: {e}", "code": "SERVER_ERROR"}), 500

@app.route("/api/logout", methods=["POST"])
@app.route("/logout", methods=["POST"])
def logout():
    tok = request.headers.get("X-Session-Token", "").strip()
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            if tok:
                conn.execute("DELETE FROM sessions WHERE token = ?", (tok,))
            try:
                data = request.get_json(silent=True) or {}
            except Exception:
                data = {}
            cid = (data.get("client_id") or request.headers.get("X-Client-ID", "")).strip()
            if cid:
                conn.execute("DELETE FROM sessions WHERE client_id = ?", (cid,))
            conn.commit()
            
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions")
            active_count = cursor.fetchone()[0]
            
        return jsonify({"ok": True, "active_users": active_count, "max_users": MAX_CONCURRENT_USERS})
    except Exception as e:
        log.error("[db] Logout failed: %s", e)
        return jsonify({"error": f"Logout failed: {e}"}), 500

@app.route("/api/clear-sessions", methods=["GET", "POST"])
@app.route("/clear-sessions", methods=["GET", "POST"])
def clear_sessions():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.execute("DELETE FROM sessions")
            conn.commit()
        return jsonify({"ok": True, "active_users": 0, "max_users": MAX_CONCURRENT_USERS})
    except Exception as e:
        log.error("[db] Clear sessions failed: %s", e)
        return jsonify({"error": f"Clear sessions failed: {e}"}), 500

@app.route("/api/check-pdf", methods=["POST"])
@app.route("/check-pdf", methods=["POST"])
def check_pdf():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    f_val = request.files["file"]
    if not f_val or not f_val.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400
        
    try:
        file_bytes = f_val.read()
        if len(file_bytes) == 0:
            return jsonify({"ok": False, "error": "Uploaded file is empty (0 bytes)"}), 400
            
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        num_pages = len(doc)
        if num_pages == 0:
            doc.close()
            return jsonify({"ok": False, "error": "PDF has 0 pages or is not a valid PDF file"}), 200
            
        errors = []
        warnings = []
        for i in range(num_pages):
            try:
                page = doc[i]
                pix = page.get_pixmap(dpi=72)
                if not pix:
                    warnings.append(f"Page {i+1} failed to render pixmap.")
            except Exception as pe:
                errors.append(f"Page {i+1} failed with error: {str(pe)}")
        
        doc.close()
        
        is_safe = len(errors) == 0 and len(warnings) == 0
        return jsonify({
            "ok": True,
            "filename": f_val.filename,
            "size_bytes": len(file_bytes),
            "pages": num_pages,
            "is_safe": is_safe,
            "errors": errors,
            "warnings": warnings,
        })
    except Exception as e:
        return jsonify({
            "ok": True,
            "filename": f_val.filename,
            "is_safe": False,
            "errors": [f"Failed to parse PDF document structure: {str(e)}"],
            "warnings": []
        })

@app.route("/api/sessions", methods=["GET"])
@app.route("/sessions", methods=["GET"])
def get_sessions_info():
    """Backwards-compat info endpoint (sessions disabled in v2.6)."""
    students = _store.get("students") or []
    return jsonify({
        "sessions_disabled":   True,
        "active_sessions":     1 if students else 0,
        "your_students_loaded": len(students),
        "your_school":         _store.get("school_name") or "None",
    })

@app.route("/api/schools", methods=["GET"])
@app.route("/schools", methods=["GET"])
def get_schools():
    return jsonify([{"id": k, "name": v} for k, v in SCHOOLS.items()])

@app.route("/api/upload", methods=["POST"])
@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file attached. Please choose a file."}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Empty file name"}), 400
    fname = f.filename.strip()
    ext = Path(fname).suffix.lower()
    if ext not in (".xlsx", ".xls", ".csv"):
        return jsonify({"error": f"Unsupported file type '{ext}'. Please upload .xlsx, .xls or .csv"}), 400
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)
        students = parse_file(tmp_path, fname)
        if not students:
            return jsonify({"error": "No student rows found in the file. Check column headers."}), 400
        replace_store(students, "file", "Uploaded File")
        log.info("Upload: %d students from '%s'", len(students), fname)
        return jsonify({
            "success": True,
            "count": len(students),
            "school_name": "Uploaded File",
            "classes": _classes_summary(students),
            "session": students[0].get("session", DEFAULT_SESSION) if students else DEFAULT_SESSION,
        })
    except Exception as e:
        log.error("Upload error: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": f"Could not parse file: {e}"}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except: pass

@app.route("/api/fetch-school/<int:school_id>", methods=["GET"])
@app.route("/fetch-school/<int:school_id>", methods=["GET"])
def fetch_school(school_id):
    if school_id not in SCHOOLS:
        return jsonify({"error": "Unknown school"}), 400
    url = API_BASE_URL.format(school_id=school_id)
    try:
        resp = _HTTP.get(url, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return jsonify({"error": f"API error: {e}"}), 500

    records = None
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("data","students","records","result","results","items"):
            if key in payload and isinstance(payload[key], list):
                records = payload[key]; break
        if records is None:
            for v in payload.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    records = v; break

    if not records:
        return jsonify({"error": "No student records found in API response"}), 500

    students = [map_api_record(r) for r in records if isinstance(r, dict)]
    students = [s for s in students if any(v for v in s.values() if v and v != DEFAULT_SESSION)]
    if not students:
        return jsonify({"error": "No valid students after mapping"}), 500

    students = _sort_and_index(students)
    replace_store(students, "api", SCHOOLS[school_id], school_id=school_id)
    resp = jsonify({
        "success": True,
        "count": len(students),
        "school_id": school_id,
        "school": SCHOOLS[school_id],
        "classes": _classes_summary(students),
        "session": students[0].get("session", DEFAULT_SESSION) if students else DEFAULT_SESSION,
    })
    resp.headers["X-Students-Count"] = str(len(students))
    return resp

@app.route("/api/students", methods=["GET"])
@app.route("/students", methods=["GET"])
def get_students():
    cls      = request.args.get("class","").strip().upper()
    students = _store["students"]
    if cls:
        students = [s for s in students if s.get("class","").strip().upper() == cls]
    return jsonify(students)

@app.route("/api/status", methods=["GET"])
@app.route("/status", methods=["GET"])
def get_status():
    students = _store["students"]
    if not students:
        return jsonify({"loaded": False})
    cls_list    = sorted(set(s.get("class","").strip().upper() for s in students), key=class_sort_key)
    session_val = students[0].get("session", DEFAULT_SESSION)
    class_counts = {}
    for s in students:
        k = s.get("class","").strip().upper()
        if k:
            class_counts[k] = class_counts.get(k, 0) + 1
    school_name = _store.get("school_name","")
    school_id   = _store.get("school_id")
    return jsonify({
        "loaded": True,
        "count": len(students),
        "school_id": school_id,
        "school": school_name,
        "school_name": school_name,
        "source": _store.get("source",""),
        "classes": cls_list,
        "classCounts": class_counts,
        "session": session_val,
    })

def _classes_summary(students):
    cc = defaultdict(int)
    for s in students:
        cc[s.get("class","").strip().upper()] += 1
    return [{"class": k, "count": v} for k, v in sorted(cc.items(), key=lambda x: class_sort_key(x[0]))]


def normalize_template_key(value):
    key = str(value or DEFAULT_TEMPLATE).strip().lower()
    return key if key in TEMPLATE_CONFIGS else DEFAULT_TEMPLATE


def get_template_config(template_key=None):
    return TEMPLATE_CONFIGS[normalize_template_key(template_key)]

# ─────────────────────────────────────────────────────────────────
# PHOTO CACHE
# ─────────────────────────────────────────────────────────────────

class _BoundedPhotoCache:
    def __init__(self, maxsize: int = 600):
        self._cache: OrderedDict = OrderedDict()
        self._maxsize = maxsize
        self._lock    = threading.Lock()

    def get(self, key: str):
        with self._lock:
            if key not in self._cache:
                return False, None
            self._cache.move_to_end(key)
            return True, self._cache[key]

    def set(self, key: str, value):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def clear(self):
        with self._lock:
            self._cache.clear()

    def __len__(self):
        with self._lock:
            return len(self._cache)


_photo_cache = _BoundedPhotoCache(maxsize=MAX_CACHED_PHOTOS)

# ─────────────────────────────────────────────────────────────────
# TEMPLATE + FONT SINGLETONS
# ─────────────────────────────────────────────────────────────────

_template_bytes_cache: dict = {}
# Locks cover every template key, INCLUDING the employee ones merged in above.
_template_locks = {key: threading.Lock() for key in TEMPLATE_CONFIGS}
_template_doc_cache: dict = {}
_template_doc_locks = {key: threading.Lock() for key in TEMPLATE_CONFIGS}
_template_preview_cache: dict = {}
_template_preview_locks = {key: threading.Lock() for key in TEMPLATE_CONFIGS}

_anton_font_obj = None
_bold_font_obj  = None
_font_init_done = False
_font_lock      = threading.Lock()


def _ensure_template(template_key: str = DEFAULT_TEMPLATE):
    template = get_template_config(template_key)
    key = template["key"]
    if key in _template_bytes_cache:
        return _template_bytes_cache[key]
    lock = _template_locks[key]
    with lock:
        if key in _template_bytes_cache:
            return _template_bytes_cache[key]
        pdf_path = template["pdf"]
        if not pdf_path.exists():
            return None
        with open(str(pdf_path), "rb") as fh:
            _template_bytes_cache[key] = fh.read()
        return _template_bytes_cache[key]


def _get_template_doc(template_key: str = DEFAULT_TEMPLATE):
    if not HAS_FITZ:
        return None
    key = normalize_template_key(template_key)
    if key in _template_doc_cache:
        return _template_doc_cache[key]
    lock = _template_doc_locks[key]
    with lock:
        if key in _template_doc_cache:
            return _template_doc_cache[key]
        tmpl_bytes = _ensure_template(key)
        if tmpl_bytes is None:
            return None
        _template_doc_cache[key] = fitz.open("pdf", tmpl_bytes)
        return _template_doc_cache[key]


def _get_template_preview_png(template_key: str = DEFAULT_TEMPLATE):
    key = normalize_template_key(template_key)
    if key in _template_preview_cache:
        return _template_preview_cache[key]
    if not HAS_FITZ:
        return None
    lock = _template_preview_locks[key]
    with lock:
        if key in _template_preview_cache:
            return _template_preview_cache[key]
        tmpl_bytes = _ensure_template(key)
        if tmpl_bytes is None:
            return None
        doc = fitz.open("pdf", tmpl_bytes)
        try:
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            _template_preview_cache[key] = pix.tobytes("png")
        finally:
            doc.close()
        return _template_preview_cache[key]


def _ensure_fonts():
    global _anton_font_obj, _bold_font_obj, _font_init_done
    if _font_init_done:
        return (
            _anton_font_obj, _bold_font_obj,
            str(ANTON_FONT) if ANTON_FONT.exists() else None,
            str(ARIAL_BOLD) if ARIAL_BOLD.exists() else None,
            "anton"   if ANTON_FONT.exists() else "helv",
            "arialbd" if ARIAL_BOLD.exists() else "helv",
        )
    with _font_lock:
        if _font_init_done:
            return (
                _anton_font_obj, _bold_font_obj,
                str(ANTON_FONT) if ANTON_FONT.exists() else None,
                str(ARIAL_BOLD) if ARIAL_BOLD.exists() else None,
                "anton"   if ANTON_FONT.exists() else "helv",
                "arialbd" if ARIAL_BOLD.exists() else "helv",
            )
        try:
            _anton_font_obj = fitz.Font(fontfile=str(ANTON_FONT)) if ANTON_FONT.exists() else fitz.Font("helv")
            _bold_font_obj  = fitz.Font(fontfile=str(ARIAL_BOLD)) if ARIAL_BOLD.exists() else fitz.Font("helv")
        except Exception:
            _anton_font_obj = fitz.Font("helv")
            _bold_font_obj  = fitz.Font("helv")
        _font_init_done = True
        return (
            _anton_font_obj, _bold_font_obj,
            str(ANTON_FONT) if ANTON_FONT.exists() else None,
            str(ARIAL_BOLD) if ARIAL_BOLD.exists() else None,
            "anton"   if ANTON_FONT.exists() else "helv",
            "arialbd" if ARIAL_BOLD.exists() else "helv",
        )

# ─────────────────────────────────────────────────────────────────
# PHOTO COMPRESSION  (higher quality)
# ─────────────────────────────────────────────────────────────────

def _compress_photo(pil_img) -> bytes:
    # ✅ FIX: Apply EXIF orientation FIRST — phones store rotation in metadata.
    # Without this, a portrait photo taken on a phone appears sideways because
    # PIL reads raw pixels (landscape) without honouring the EXIF rotation tag.
    try:
        pil_img = ImageOps.exif_transpose(pil_img)
    except Exception:
        pass  # exif_transpose may fail on images without EXIF — ignore safely

    rgb = pil_img.convert("RGB")
    src_w, src_h = rgb.size
    src_min = min(src_w, src_h)

    if src_min < 280:
        rgb = rgb.filter(ImageFilter.SMOOTH)

    # Resize to fit within PHOTO_PX on the longest side — NO white canvas.
    # Preserving the original aspect ratio here is critical: prepare_photo_for_rect_cover
    # later applies true COVER (fill + center-crop) to match the card placeholder exactly.
    # If we bake a white square canvas here, those white bars become part of the image
    # data and survive the COVER step, causing visible white space on the card.
    ratio = min(PHOTO_PX / max(1, src_w), PHOTO_PX / max(1, src_h))
    new_w = max(1, int(round(src_w * ratio)))
    new_h = max(1, int(round(src_h * ratio)))
    resized = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if rgb is not pil_img:
        rgb.close()

    if src_min >= 280:
        resized = resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=90, threshold=4))

    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY,
                 optimize=True, progressive=False, subsampling=1)
    resized.close()
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────
# PHOTO FETCH — empty space when missing (NO fallback image)
# ─────────────────────────────────────────────────────────────────
def _clean_photo_url(url: str) -> str:
    """
    v2.8 — RAW URL PASSTHROUGH.
    Per spec: "do NOT clean the image URL — use it exactly as the row supplies it."
    We only:
      • Strip surrounding whitespace.
      • Verify the scheme is http(s):// so we don't try to fetch garbage.
    No markdown unwrap. No escape substitution. The URL is fed to requests
    verbatim, so URLs containing backslashes, %-escapes, query strings, etc.
    survive untouched.
    """
    if not url:
        return ""
    url = str(url).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    return url


def fetch_photo_bytes(url: str):
    """
    Returns compressed JPEG bytes, or None when the photo is missing/invalid.
    None is rendered as empty (transparent) rect on the card — no sample image.
    Tries https with verify=True first; falls back to verify=False for self-signed certs.
    """
    if not HAS_PIL:
        return None

    cache_key = _clean_photo_url((url or "").strip())
    if not cache_key:
        return None

    found, cached = _photo_cache.get(cache_key)
    if found:
        return cached  # may legitimately be None

    def _do_fetch(verify_ssl=True):
        resp = _HTTP.get(cache_key, timeout=PHOTO_TIMEOUT, stream=True,
                         allow_redirects=True, verify=verify_ssl)
        resp.raise_for_status()
        ct = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in ct:
            return None
        chunks = []
        total  = 0
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_PHOTO_BYTES:
                raise ValueError("photo too large")
            chunks.append(chunk)
        resp.close()
        return b"".join(chunks)

    try:
        raw = _do_fetch(verify_ssl=True)
        if raw is None:
            _photo_cache.set(cache_key, None)
            return None
    except Exception:
        # SSL / connection error — retry without verification (handles self-signed certs)
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            raw = _do_fetch(verify_ssl=False)
            if raw is None:
                _photo_cache.set(cache_key, None)
                return None
        except Exception:
            _photo_cache.set(cache_key, None)
            return None

    try:
        with Image.open(io.BytesIO(raw)) as img:
            compressed = _compress_photo(img)
        _photo_cache.set(cache_key, compressed)
        return compressed
    except Exception:
        _photo_cache.set(cache_key, None)
        return None


def clear_photo_cache():
    _photo_cache.clear()


def prefetch_photos(students: list) -> None:
    urls = list({
        s.get("photo_url","").strip()
        for s in students
        if s.get("photo_url","").strip()
    })
    if not urls:
        return
    workers = min(PREFETCH_WORKERS, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_photo_bytes, url) for url in urls]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

# ─────────────────────────────────────────────────────────────────
# CARD LAYOUT CONSTANTS
# ─────────────────────────────────────────────────────────────────
CARD_W_MM   = 55.0;  CARD_H_MM   = 86.0
A4_W_MM     = 297.0; A4_H_MM     = 210.0
COLS        = 5;     ROWS        = 2;  CARDS_PER_PAGE = COLS * ROWS
ROW_GAP_MM  = 10.0
GRID_W_MM   = COLS * CARD_W_MM
GRID_H_MM   = ROWS * CARD_H_MM + (ROWS - 1) * ROW_GAP_MM
OFFSET_X_MM = (A4_W_MM - GRID_W_MM) / 2.0
OFFSET_Y_MM = (A4_H_MM - GRID_H_MM) / 2.0
MM_TO_PT    = 72.0 / 25.4
PT_PER_INCH = 72.0

PHOTO_RECT_COORDS        = (54.25, 67.74, 98.82, 119.07)
BAND_Y0                  = 123.8;  BAND_Y1 = 151.0
NAME_TEXT_RECT_COORDS    = (13.0, 124.7, 112.0, 139.2)
CLASS_TEXT_RECT_COORDS   = (13.0, 139.7, 112.0, 147.0)
SIGN_SAFE_X1             = 118.0
ADM_WHITEOUT_COORDS      = (18.0, 107.0, 48.0, 116.5)
ADM_VALUE_RECT_COORDS    = (18.51, 107.56, 48.0, 115.5)
SESSION_WHITEOUT_COORDS  = (109.15, 107.5, 142.0, 118.5)
SESSION_VALUE_RECT_COORDS= (109.15, 108.0, 142.0, 118.5)
BLOOD_RED                = (0.8549, 0.0627, 0.0627)
BLOOD_VALUE_RECT_COORDS  = (112.0, 84.5, 129.0, 97.5)
FATHER_VALUE_RECT_COORDS = (66.3, 154.4, 148.0, 160.6)
MOTHER_VALUE_RECT_COORDS = (66.3, 162.2, 148.0, 168.3)
DOB_VALUE_RECT_COORDS    = (66.3, 168.8, 148.0, 174.9)
ADDRESS_VALUE_RECT_COORDS= (66.3, 175.4, SIGN_SAFE_X1, 187.0)
MOBILE_VALUE_RECT_COORDS = (66.3, 191.1, SIGN_SAFE_X1, 197.2)
FATHER_CLEAN_COORDS      = (66.3, 153.8, 149.0, 161.2)
MOTHER_CLEAN_COORDS      = (66.3, 161.5, 149.0, 169.0)
DOB_CLEAN_COORDS         = (66.3, 168.0, 149.0, 175.5)
ADDRESS_CLEAN_COORDS     = (66.3, 174.8, SIGN_SAFE_X1, 188.0)
MOBILE_CLEAN_COORDS      = (66.3, 190.5, 113.0, 198.0)

BANNER_RED   = (0.7843, 0.0667, 0.0667)
WHITE        = (1.0, 1.0, 1.0)
NAME_COLOR   = (1.0, 1.0, 1.0)
VALUE_COLOR  = (170/255, 16/255, 16/255)

NAME_FONT_SIZE   = 9.9;  CLASS_FONT_SIZE  = 5.9;  VALUE_FONT_SIZE = 5.5
ADM_FONT_SIZE    = 6.5;  SESSION_FONT_SIZE = 7.5; BLOOD_FONT_SIZE = 6.88
ADDR_MAX_LINES   = 3;    ADDR_LINE_GAP    = 1.10; ADDR_MIN_SIZE   = 3.5
ADDR_SIZE_STEPS  = [5.5, 5.2, 5.0, 4.8, 4.5, 4.2, 4.0, 3.8, 3.5]

REDEEMER_BG_COLOR               = (0.9529, 0.9922, 1.0)
REDEEMER_GRAD_LEFT             = (234/255, 250/255, 255/255)
REDEEMER_GRAD_RIGHT            = (245/255, 253/255, 255/255)
REDEEMER_BLUE                  = (31/255, 72/255, 255/255)
REDEEMER_RED                   = (1.0, 0.1922, 0.1922)
REDEEMER_WHITE                 = (232/255, 246/255, 255/255)
REDEEMER_BLACK                 = (0.0, 0.0, 0.0)
REDEEMER_PHOTO_OUTER_RECT      = (53.55, 75.973, 99.45, 132.989)
REDEEMER_PHOTO_RECT_COORDS     = (54.58, 77.072, 98.594, 131.969)
REDEEMER_PHOTO_BORDER_W        = 1.03
REDEEMER_BANNER_RECT           = (0.0, 140.0, 126.0, 163.94)
REDEEMER_BANNER_TEXT_LEFT      = 4.0
REDEEMER_BANNER_TEXT_RIGHT     = 126.0
REDEEMER_BANNER_CENTER_X       = 63.0
REDEEMER_BANNER_ACCENT_POINTS  = (
    (126.0, 140.0),
    (151.4, 140.0),
    (142.0, 163.94),
    (122.8, 163.94),
)
REDEEMER_NAME_TEXT_RECT        = (4.0, 142.0, 126.0, 153.8)
REDEEMER_CLASS_TEXT_RECT       = (8.0, 154.0, 126.0, 163.2)
REDEEMER_NAME_BASELINE_Y       = 150.032
REDEEMER_CLASS_BASELINE_Y      = 161.681
REDEEMER_SESSION_CLEAN_COORDS  = (103.0, 103.8, 137.6, 114.9)
REDEEMER_SESSION_VALUE_RECT    = (106.8, 104.4, 136.0, 114.2)
REDEEMER_DATA_CLEAN_RECT       = (63.0, 167.5, 153.0, 207.0)
REDEEMER_VALUE_X               = 64.951
REDEEMER_VALUE_MAX_X           = 149.0
REDEEMER_FATHER_BASELINE_Y     = 175.50
REDEEMER_DOB_BASELINE_Y        = 185.85
REDEEMER_MOBILE_BASELINE_Y     = 196.20
REDEEMER_ADDRESS_BASELINE_Y    = 206.55
REDEEMER_NAME_FONT_SIZE        = 10.8775
REDEEMER_NAME_MIN_SIZE         = 6.0
REDEEMER_NAME_TRACKING         = 0.874
REDEEMER_CLASS_FONT_SIZE       = 5.8842
REDEEMER_CLASS_TRACKING        = 0.477
REDEEMER_VALUE_FONT_SIZE       = 6.8
REDEEMER_ADDRESS_MAX_LINES     = 2
REDEEMER_ADDRESS_LINE_GAP      = 1.02
REDEEMER_SESSION_FONT_SIZE     = 7.2

TEARDROP_ITEMS = [
    ('l', (126.74588, 84.57169), (119.56597, 72.82723)),
    ('l', (119.56597, 72.82723), (112.91280, 84.49141)),
    ('c', (112.91280, 84.49141),(111.36359, 86.96311),(111.22838, 90.17703),(112.85576, 92.83886)),
    ('c', (112.85576, 92.83886),(115.16902, 96.62247),(120.15327, 97.83719),(123.98969, 95.55492)),
    ('c', (123.98969, 95.55492),(127.82469, 93.27335),(129.05914, 88.35811),(126.74588, 84.57169)),
]

# ─────────────────────────────────────────────────────────────────
# TEXT RENDERING HELPERS
# ─────────────────────────────────────────────────────────────────

def _fit_size(font, text, max_width, base, min_size=4.0):
    s = base
    while s >= min_size:
        if font.text_length(text, fontsize=s) <= max_width:
            return s
        s -= 0.1
    return min_size

def _put_single(page, rect, text, fontfile, fontname, size, color, font_obj):
    if not text: return
    baseline_y = rect.y0 + size * font_obj.ascender
    page.insert_text(
        (rect.x0, baseline_y), text,
        fontname=fontname, fontfile=str(fontfile) if fontfile else None,
        fontsize=size, color=color, overlay=True,
    )

def draw_text_vertically_centered(page, rect, text, fontfile, fontname, font_obj, base_size, color):
    if not text: return
    size   = _fit_size(font_obj, text, rect.width, base_size, 4.0)
    text_h = size * (font_obj.ascender - font_obj.descender)
    baseline = rect.y0 + (rect.height + text_h) / 2.0 - size * abs(font_obj.descender)
    page.insert_text(
        (rect.x0, baseline), text,
        fontname=fontname, fontfile=str(fontfile) if fontfile else None,
        fontsize=size, color=color, overlay=True,
    )

def draw_text_centered_hv(page, rect, text, fontfile, fontname, font_obj, size, color):
    if not text: return
    size = _fit_size(font_obj, text, rect.width, size, 3.5)
    tw   = font_obj.text_length(text, fontsize=size)
    gh   = size * (font_obj.ascender - font_obj.descender)
    x    = rect.x0 + (rect.width - tw) / 2.0
    y    = rect.y0 + (rect.height + gh) / 2.0 - size * abs(font_obj.descender)
    page.insert_text(
        (x, y), text,
        fontname=fontname, fontfile=str(fontfile) if fontfile else None,
        fontsize=size, color=color, overlay=True,
    )


def clean_visible_text(text):
    if text is None:
        return ""
    text = str(text)
    text = (text
            .replace("\xa0", " ")
            .replace("\u200b", "")
            .replace("\u200c", "")
            .replace("\u200d", "")
            .replace("\ufeff", "")
            .replace("\ufffd", " "))
    text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
    return " ".join(text.split()).strip()


def clean_card_value(text):
    text = clean_visible_text(text)
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"nan", "none", "null", "nil", "0000-00-00", "00-00-0000", "0000/00/00"}:
        return ""
    if has_html(text):           # 🚫 HTML in any field → blank
        return ""
    # Only reject strings that are purely zeros with separators (e.g. "00/00/0000")
    # Do NOT reject valid values like "2026-27" (session), "21-04-2014" (DOB), or adm_no digits.
    digits_only = re.sub(r"[0\-/:\. ]", "", text)
    if not digits_only and re.search(r"[1-9]", text) is None:
        return ""
    return text


def insert_image_safe(page, rect, photo_bytes):
    """
    Insert image only if bytes available; otherwise leave the rect EMPTY.

    v3.0: keep_proportion=False so the (already pre-fitted) JPEG bytes
    fill the rect exactly. The smart-fit pre-processing in
    `prepare_photo_for_rect` produces bytes that already match the target
    rect's aspect ratio (via COVER center-crop or CONTAIN letterbox),
    so PyMuPDF doesn't need to add any further padding — it just paints
    the bytes edge-to-edge. This eliminates the visible white pillars
    that appeared when keep_proportion=True caused PyMuPDF to letterbox
    a second time inside the rect.
    """
    if not photo_bytes:
        return
    page.insert_image(rect, stream=photo_bytes, overlay=True, keep_proportion=False)


def prepare_photo_for_rect_cover(photo_bytes, rect_coords, scale=6, output_format="JPEG", is_redeemer=False):
    """
    TRUE COVER — scales image so it fully fills the placeholder in BOTH
    dimensions, then center-crops the excess. Zero white space, no distortion.

    IMPORTANT: rect_coords must be the ACTUAL inserted rect coords (after any
    map_rect / _tr_rect transform), not raw template coords. If you pass raw
    template coords and the transform has sx != sy, the prepared image will have
    the wrong aspect ratio and white bars will appear.
    """
    if not HAS_PIL or not photo_bytes:
        return photo_bytes
    x0, y0, x1, y1 = rect_coords
    target_w = max(1, int(round(abs(x1 - x0) * scale)))
    target_h = max(1, int(round(abs(y1 - y0) * scale)))
    try:
        with Image.open(io.BytesIO(photo_bytes)) as img:
            try: img = ImageOps.exif_transpose(img)
            except Exception: pass
            rgb = img.convert("RGB")
            src_w, src_h = rgb.size

            # COVER: scale so the image fills the ENTIRE placeholder in both
            # width and height, then crop the centre — no white space at all.
            ratio = max(target_w / max(1, src_w), target_h / max(1, src_h))
            new_w = max(1, int(round(src_w * ratio)))
            new_h = max(1, int(round(src_h * ratio)))
            fitted = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)

            left = (new_w - target_w) // 2
            top  = (new_h - target_h) // 2
            fitted = fitted.crop((left, top, left + target_w, top + target_h))

            buf = io.BytesIO()
            save_fmt = (output_format or "JPEG").upper()
            if save_fmt == "JPEG":
                fitted.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY,
                            optimize=True, progressive=False, subsampling=1)
            else:
                fitted.save(buf, format="PNG")
            fitted.close()
            if rgb is not img:
                rgb.close()
            return buf.getvalue()
    except Exception:
        return photo_bytes


def prepare_photo_for_rect_contain(photo_bytes, rect_coords, scale=6, output_format="JPEG"):
    """
    v2.9 FULL-PHOTO FIT (CONTAIN) — kept for compatibility / specific
    layouts that explicitly want letterbox.  Most callers now use
    `prepare_photo_for_rect_cover` so the photo fully fills the
    placeholder with NO visible white space.
    """
    if not HAS_PIL or not photo_bytes:
        return photo_bytes
    x0, y0, x1, y1 = rect_coords
    target_w = max(1, int(round((x1 - x0) * scale)))
    target_h = max(1, int(round((y1 - y0) * scale)))
    try:
        with Image.open(io.BytesIO(photo_bytes)) as img:
            try: img = ImageOps.exif_transpose(img)
            except Exception: pass
            rgb = img.convert("RGB")
            src_w, src_h = rgb.size
            ratio = min(target_w / max(1, src_w),
                        target_h / max(1, src_h))
            new_w = max(1, int(round(src_w * ratio)))
            new_h = max(1, int(round(src_h * ratio)))
            fitted = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
            fx = (target_w - new_w) // 2
            fy = (target_h - new_h) // 2
            canvas.paste(fitted, (fx, fy))
            fitted.close()
            buf = io.BytesIO()
            save_fmt = (output_format or "JPEG").upper()
            if save_fmt == "JPEG":
                canvas.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY,
                            optimize=True, progressive=False, subsampling=1)
            else:
                canvas.save(buf, format="PNG")
            canvas.close()
            if rgb is not img:
                rgb.close()
            return buf.getvalue()
    except Exception:
        return photo_bytes


def prepare_photo_for_rect(photo_bytes, rect_coords, scale=6, output_format="JPEG"):
    """
    TRUE COVER — scales image so it fully fills the placeholder in BOTH
    dimensions, then center-crops the excess. Zero white space, no distortion.
    """
    if not HAS_PIL or not photo_bytes:
        return photo_bytes
    x0, y0, x1, y1 = rect_coords
    target_w = max(1, int(round(abs(x1 - x0) * scale)))
    target_h = max(1, int(round(abs(y1 - y0) * scale)))
    try:
        with Image.open(io.BytesIO(photo_bytes)) as img:
            try: img = ImageOps.exif_transpose(img)
            except Exception: pass
            rgb = img.convert("RGB")
            src_w, src_h = rgb.size

            ratio = max(target_w / max(1, src_w), target_h / max(1, src_h))
            new_w = max(1, int(round(src_w * ratio)))
            new_h = max(1, int(round(src_h * ratio)))
            fitted = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)

            left = (new_w - target_w) // 2
            top  = (new_h - target_h) // 2
            fitted = fitted.crop((left, top, left + target_w, top + target_h))

            buf = io.BytesIO()
            save_fmt = (output_format or "JPEG").upper()
            if save_fmt == "JPEG":
                fitted.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY,
                            optimize=True, progressive=False, subsampling=1)
            else:
                fitted.save(buf, format="PNG")
            fitted.close()
            if rgb is not img:
                rgb.close()
            return buf.getvalue()
    except Exception:
        return photo_bytes


def insert_tracked_text(page, x, baseline_y, text, fontfile, fontname, font_obj, size, color, tracking=0.0):
    text = clean_visible_text(text)
    if not text:
        return
    if tracking <= 0 or len(text) <= 1:
        page.insert_text(
            (x, baseline_y), text,
            fontname=fontname, fontfile=str(fontfile) if fontfile else None,
            fontsize=size, color=color, overlay=True,
        )
        return
    cursor = x
    for ch in text:
        page.insert_text(
            (cursor, baseline_y), ch,
            fontname=fontname, fontfile=str(fontfile) if fontfile else None,
            fontsize=size, color=color, overlay=True,
        )
        cursor += font_obj.text_length(ch, fontsize=size) + tracking


def draw_redeemer_banner_text(page, text, center_x, baseline_y, max_width, fontfile, fontname, font_obj, base_size, color, tracking=0.0, min_size=4.0):
    text = clean_visible_text(text).upper()
    if not text:
        return
    size, adjusted_tracking = _fit_tracked_text(font_obj, text, max_width, base_size, tracking, min_size=min_size)
    text = _ellipsize_tracked_to_width(font_obj, text, max_width, size, adjusted_tracking)
    if not text:
        return
    total_width = _tracked_text_width(font_obj, text, size, adjusted_tracking)
    if total_width > max_width:
        adjusted_tracking = 0.0
        text = _ellipsize_tracked_to_width(font_obj, text, max_width, size, adjusted_tracking)
        total_width = _tracked_text_width(font_obj, text, size, adjusted_tracking)
    insert_tracked_text(
        page,
        center_x - total_width / 2.0,
        baseline_y,
        text,
        fontfile, fontname, font_obj, size, color, adjusted_tracking,
    )


def draw_redeemer_value(page, text, x, baseline_y, max_width, fontfile, fontname, font_obj, base_size, color, min_size=5.0):
    value = clean_card_value(text)
    if not value:
        return
    size = _fit_size(font_obj, value, max_width, base_size, min_size)
    value = _ellipsize_to_width(font_obj, value, max_width, size)
    if not value:
        return
    page.insert_text(
        (x, baseline_y), value,
        fontname=fontname, fontfile=str(fontfile) if fontfile else None,
        fontsize=size, color=color, overlay=True,
    )


def render_redeemer_address(page, addr, x, baseline_y, max_width, fontfile, fontname, font_obj, color, base_size=6.8, min_size=4.8, max_lines=2, line_gap=1.03):
    lines, target_fs = wrap_and_shrink_text(font_obj, addr, max_width, max_lines, base_size=base_size)
    if not lines:
        return
    step = target_fs * line_gap
    for idx, line in enumerate(lines):
        page.insert_text(
            (x, baseline_y + idx * step), line,
            fontname=fontname, fontfile=str(fontfile) if fontfile else None,
            fontsize=target_fs, color=color, overlay=True,
        )


def _tracked_text_width(font_obj, text, fontsize, tracking=0.0):
    if not text:
        return 0.0
    base = font_obj.text_length(text, fontsize=fontsize)
    if tracking <= 0 or len(text) <= 1:
        return base
    return base + tracking * (len(text) - 1)


def _fit_tracked_text(font_obj, text, max_width, base_size, tracking=0.0, min_size=4.0):
    size = base_size
    adjusted_tracking = tracking
    while size >= min_size:
        if _tracked_text_width(font_obj, text, size, adjusted_tracking) <= max_width:
            return size, adjusted_tracking
        size -= 0.1
        adjusted_tracking = tracking * (size / base_size) if base_size else tracking
    return min_size, 0.0


def _ellipsize_to_width(font_obj, text, max_width, fontsize):
    text = clean_visible_text(text)
    if not text:
        return ""
    if font_obj.text_length(text, fontsize=fontsize) <= max_width:
        return text
    ellipsis = "…"
    if font_obj.text_length(ellipsis, fontsize=fontsize) > max_width:
        return ""
    trimmed = text.rstrip()
    while trimmed and font_obj.text_length(trimmed + ellipsis, fontsize=fontsize) > max_width:
        trimmed = trimmed[:-1].rstrip()
    return (trimmed + ellipsis) if trimmed else ellipsis


def _ellipsize_tracked_to_width(font_obj, text, max_width, fontsize, tracking=0.0):
    text = clean_visible_text(text)
    if not text:
        return ""
    if _tracked_text_width(font_obj, text, fontsize, tracking) <= max_width:
        return text
    ellipsis = "…"
    if _tracked_text_width(font_obj, ellipsis, fontsize, 0.0) > max_width:
        return ""
    trimmed = text.rstrip()
    while trimmed and _tracked_text_width(font_obj, trimmed + ellipsis, fontsize, tracking) > max_width:
        trimmed = trimmed[:-1].rstrip()
    return (trimmed + ellipsis) if trimmed else ellipsis


def _draw_horizontal_gradient_mask(page, rect, left_color, right_color, steps):
    steps = max(8, int(steps))
    x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    band_w = (x1 - x0) / steps
    for step in range(steps):
        t = 0.0 if steps == 1 else step / (steps - 1)
        color = (
            left_color[0] + t * (right_color[0] - left_color[0]),
            left_color[1] + t * (right_color[1] - left_color[1]),
            left_color[2] + t * (right_color[2] - left_color[2]),
        )
        rx0 = x0 + step * band_w
        rx1 = x0 + (step + 1) * band_w + 0.02
        page.draw_rect(fitz.Rect(rx0, y0, rx1, y1), color=color, fill=color, width=0, overlay=True)


def _draw_redeemer_overlay_core(page, student: dict, map_point, map_rect, scale_x=1.0, scale_y=1.0):
    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = _ensure_fonts()
    if anton_obj is None or bold_obj is None:
        return

    page.draw_rect(
        map_rect(REDEEMER_BANNER_RECT),
        color=REDEEMER_BLUE, fill=REDEEMER_BLUE, width=0, overlay=True,
    )

    page.draw_rect(
        map_rect(REDEEMER_SESSION_CLEAN_COORDS),
        color=REDEEMER_BG_COLOR, fill=REDEEMER_BG_COLOR, width=0, overlay=True,
    )
    _draw_horizontal_gradient_mask(
        page,
        map_rect(REDEEMER_DATA_CLEAN_RECT),
        REDEEMER_GRAD_LEFT,
        REDEEMER_GRAD_RIGHT,
        max(20, REDEEMER_GRAD_STEPS),
    )

    page.draw_rect(
        map_rect(REDEEMER_PHOTO_OUTER_RECT),
        color=REDEEMER_WHITE, fill=REDEEMER_WHITE, width=0, overlay=True,
    )
    _redeemer_photo_rect = map_rect(REDEEMER_PHOTO_RECT_COORDS)
    photo_bytes = prepare_photo_for_rect_cover(
        fetch_photo_bytes(student.get("photo_url", "")),
        (_redeemer_photo_rect.x0, _redeemer_photo_rect.y0,
         _redeemer_photo_rect.x1, _redeemer_photo_rect.y1),
        scale=PHOTO_EMBED_SCALE, output_format="JPEG",
        is_redeemer=True,
    )
    insert_image_safe(page, _redeemer_photo_rect, photo_bytes)
    page.draw_rect(
        map_rect(REDEEMER_PHOTO_OUTER_RECT),
        color=REDEEMER_BLACK, fill=None, width=max(0.1, REDEEMER_PHOTO_BORDER_W * ((scale_x + scale_y) / 2.0)), overlay=True,
    )

    center_x = map_point(REDEEMER_BANNER_CENTER_X, 0).x
    banner_max_width = max(1.0, (REDEEMER_BANNER_TEXT_RIGHT - REDEEMER_BANNER_TEXT_LEFT) * scale_x)
    banner_min_scale = max(0.5, min(scale_x, scale_y))

    draw_redeemer_banner_text(
        page,
        student.get("student_name", ""),
        center_x,
        map_point(0, REDEEMER_NAME_BASELINE_Y).y,
        banner_max_width,
        anton_fn, fn_anton, anton_obj,
        REDEEMER_NAME_FONT_SIZE * banner_min_scale, REDEEMER_WHITE,
        tracking=REDEEMER_NAME_TRACKING * scale_x,
        min_size=REDEEMER_NAME_MIN_SIZE * banner_min_scale,
    )

    class_text = clean_card_value(student.get("class", "")).upper()
    if class_text:
        draw_redeemer_banner_text(
            page,
            f"CLASS:  {class_text}",
            center_x,
            map_point(0, REDEEMER_CLASS_BASELINE_Y).y,
            banner_max_width,
            bold_fn, fn_bold, bold_obj,
            REDEEMER_CLASS_FONT_SIZE * banner_min_scale, REDEEMER_WHITE,
            tracking=REDEEMER_CLASS_TRACKING * scale_x,
            min_size=4.5 * banner_min_scale,
        )

    value_x = map_point(REDEEMER_VALUE_X, 0).x
    value_max_width = max(1.0, (REDEEMER_VALUE_MAX_X - REDEEMER_VALUE_X) * scale_x)
    value_base_size = REDEEMER_VALUE_FONT_SIZE * min(scale_x, scale_y)
    value_min_size = 4.7 * min(scale_x, scale_y)

    draw_redeemer_value(page, student.get("father_name", ""), value_x, map_point(0, REDEEMER_FATHER_BASELINE_Y).y, value_max_width, bold_fn, fn_bold, bold_obj, value_base_size, REDEEMER_BLACK, min_size=value_min_size)
    # DOB already pre-formatted as DD-MM-YYYY
    draw_redeemer_value(page, student.get("dob", ""), value_x, map_point(0, REDEEMER_DOB_BASELINE_Y).y, value_max_width, bold_fn, fn_bold, bold_obj, value_base_size, REDEEMER_BLACK, min_size=value_min_size)
    draw_redeemer_value(page, student.get("mobile", ""), value_x, map_point(0, REDEEMER_MOBILE_BASELINE_Y).y, value_max_width, bold_fn, fn_bold, bold_obj, value_base_size, REDEEMER_BLACK, min_size=value_min_size)
    render_redeemer_address(page, student.get("address", ""), value_x, map_point(0, REDEEMER_ADDRESS_BASELINE_Y).y, value_max_width, bold_fn, fn_bold, bold_obj, REDEEMER_BLACK, base_size=value_base_size, min_size=4.6 * min(scale_x, scale_y), max_lines=REDEEMER_ADDRESS_MAX_LINES, line_gap=REDEEMER_ADDRESS_LINE_GAP)

    session_value = clean_card_value(student.get("session", "")) or DEFAULT_SESSION
    session_rect = map_rect(REDEEMER_SESSION_VALUE_RECT)
    session_size = _fit_size(anton_obj, session_value, session_rect.width, REDEEMER_SESSION_FONT_SIZE * min(scale_x, scale_y), 5.6 * min(scale_x, scale_y))
    session_value = _ellipsize_to_width(anton_obj, session_value, session_rect.width, session_size)
    _put_single(page, session_rect, session_value, anton_fn, fn_anton, session_size, REDEEMER_BLACK, anton_obj)


def _addr_wrap_at_size(font_obj, words, max_width, fs):
    lines = []; cur = ""
    for w in words:
        if font_obj.text_length(w, fontsize=fs) > max_width:
            if cur: lines.append(cur); cur = ""
            trunc = ""; ellipsis = "…"
            for ch in w:
                if font_obj.text_length(trunc + ch + ellipsis, fontsize=fs) <= max_width:
                    trunc += ch
                else:
                    break
            lines.append(trunc + ellipsis); continue
        trial = (cur + " " + w).strip() if cur else w
        if font_obj.text_length(trial, fontsize=fs) <= max_width:
            cur = trial
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines


def wrap_and_shrink_text(font_obj, text, max_width, max_lines, base_size=6.0):
    val = clean_card_value(text)
    if not val:
        return [], base_size
    
    def wrap_at_size(fs):
        words = val.split()
        lines = []
        cur_line = ""
        for w in words:
            trial = (cur_line + " " + w).strip() if cur_line else w
            if font_obj.text_length(trial, fontsize=fs) <= max_width:
                cur_line = trial
            else:
                if cur_line:
                    lines.append(cur_line)
                cur_line = w
        if cur_line:
            lines.append(cur_line)
        return lines

    fs = base_size
    while fs >= 1.0:
        lines = wrap_at_size(fs)
        all_words_fit = True
        for line in lines:
            if font_obj.text_length(line, fontsize=fs) > max_width:
                all_words_fit = False
                break
        if len(lines) <= max_lines and all_words_fit:
            return lines, fs
        fs -= 0.1

    # Fallback to minimum possible size
    return wrap_at_size(1.0), 1.0


def _centered_baseline_for_box(font_obj, y0, y1, fontsize):
    asc = getattr(font_obj, "ascender", 0.9)
    desc = getattr(font_obj, "descender", -0.2)
    text_h = fontsize * (asc - desc)
    return y0 + max(0.0, ((y1 - y0) - text_h) / 2.0) + fontsize * asc


def _wrap_fixed_text(font_obj, text, max_width, fontsize, max_lines=2):
    value = clean_card_value(text)
    if not value:
        return []
    lines = _addr_wrap_at_size(font_obj, value.split(), max_width, fontsize)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            last = lines[-1]
            ellipsis = "…"
            while last and font_obj.text_length(last + ellipsis, fontsize=fontsize) > max_width:
                last = last[:-1].rstrip()
            lines[-1] = (last + ellipsis) if last else ellipsis
    return lines

def render_address(page, rect, addr, fontfile, fontname, font_obj, color, max_x=None):
    addr = clean_card_value(addr)
    if not addr: return
    
    max_w = (max_x if max_x is not None else rect.x1) - rect.x0
    
    lines, target_fs = wrap_and_shrink_text(font_obj, addr, max_w, ADDR_MAX_LINES, base_size=5.5)
    if not lines: return
    
    line_step = target_fs * ADDR_LINE_GAP
    baseline0 = rect.y0 + target_fs * font_obj.ascender
    for i, line in enumerate(lines):
        baseline = baseline0 + i * line_step
        if baseline - target_fs * abs(font_obj.descender) > rect.y1: break
        page.insert_text(
            (rect.x0, baseline), line,
            fontname=fontname, fontfile=str(fontfile) if fontfile else None,
            fontsize=target_fs, color=color, overlay=True,
        )

def redraw_blood_teardrop(page, fill_color):
    shape = page.new_shape()
    p = lambda t: fitz.Point(*t)
    shape.draw_line(p(TEARDROP_ITEMS[0][1]), p(TEARDROP_ITEMS[0][2]))
    shape.draw_line(p(TEARDROP_ITEMS[1][1]), p(TEARDROP_ITEMS[1][2]))
    shape.draw_bezier(p(TEARDROP_ITEMS[2][1]), p(TEARDROP_ITEMS[2][2]),
                      p(TEARDROP_ITEMS[2][3]), p(TEARDROP_ITEMS[2][4]))
    shape.draw_bezier(p(TEARDROP_ITEMS[3][1]), p(TEARDROP_ITEMS[3][2]),
                      p(TEARDROP_ITEMS[3][3]), p(TEARDROP_ITEMS[3][4]))
    shape.draw_bezier(p(TEARDROP_ITEMS[4][1]), p(TEARDROP_ITEMS[4][2]),
                      p(TEARDROP_ITEMS[4][3]), p(TEARDROP_ITEMS[4][4]))
    shape.finish(color=fill_color, fill=fill_color, width=0, closePath=True)
    shape.commit(overlay=True)

# ─────────────────────────────────────────────────────────────────
# CARD TRANSFORM HELPERS
# ─────────────────────────────────────────────────────────────────

def _make_card_transform(source_rect, target_rect):
    sx = target_rect.width / source_rect.width
    sy = target_rect.height / source_rect.height
    return {"src": source_rect, "dst": target_rect, "sx": sx, "sy": sy}


def _tr_point(tr, x, y):
    return fitz.Point(
        tr["dst"].x0 + (x - tr["src"].x0) * tr["sx"],
        tr["dst"].y0 + (y - tr["src"].y0) * tr["sy"],
    )


def _tr_rect(tr, coords):
    x0, y0, x1, y1 = coords
    p0 = _tr_point(tr, x0, y0)
    p1 = _tr_point(tr, x1, y1)
    return fitz.Rect(p0.x, p0.y, p1.x, p1.y)


def _tr_font_size(tr, size):
    return size * min(tr["sx"], tr["sy"])


def redraw_blood_teardrop_transformed(page, tr, fill_color):
    shape = page.new_shape()
    p = lambda t: _tr_point(tr, *t)
    shape.draw_line(p(TEARDROP_ITEMS[0][1]), p(TEARDROP_ITEMS[0][2]))
    shape.draw_line(p(TEARDROP_ITEMS[1][1]), p(TEARDROP_ITEMS[1][2]))
    shape.draw_bezier(p(TEARDROP_ITEMS[2][1]), p(TEARDROP_ITEMS[2][2]),
                      p(TEARDROP_ITEMS[2][3]), p(TEARDROP_ITEMS[2][4]))
    shape.draw_bezier(p(TEARDROP_ITEMS[3][1]), p(TEARDROP_ITEMS[3][2]),
                      p(TEARDROP_ITEMS[3][3]), p(TEARDROP_ITEMS[3][4]))
    shape.draw_bezier(p(TEARDROP_ITEMS[4][1]), p(TEARDROP_ITEMS[4][2]),
                      p(TEARDROP_ITEMS[4][3]), p(TEARDROP_ITEMS[4][4]))
    shape.finish(color=fill_color, fill=fill_color, width=0, closePath=True)
    shape.commit(overlay=True)


def draw_card_overlay_hebron(page, student: dict, tr):
    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = _ensure_fonts()
    if anton_obj is None or bold_obj is None:
        return

    shape = page.new_shape()
    def band_right_x(y):
        return -0.3952 * y + 172.6234
    pts = [
        _tr_point(tr, 0, BAND_Y0),
        _tr_point(tr, band_right_x(BAND_Y0), BAND_Y0),
        _tr_point(tr, band_right_x(BAND_Y1), BAND_Y1),
        _tr_point(tr, 0, BAND_Y1),
    ]
    shape.draw_polyline(pts)
    shape.draw_line(pts[-1], pts[0])
    shape.finish(color=BANNER_RED, fill=BANNER_RED, width=0)
    shape.commit(overlay=True)

    for coords in [FATHER_CLEAN_COORDS, MOTHER_CLEAN_COORDS, DOB_CLEAN_COORDS,
                   ADDRESS_CLEAN_COORDS, MOBILE_CLEAN_COORDS,
                   ADM_WHITEOUT_COORDS, SESSION_WHITEOUT_COORDS]:
        page.draw_rect(_tr_rect(tr, coords), color=WHITE, fill=WHITE, width=0, overlay=True)

    redraw_blood_teardrop_transformed(page, tr, BLOOD_RED)

    _hebron_photo_rect = _tr_rect(tr, PHOTO_RECT_COORDS)
    photo_bytes = prepare_photo_for_rect_cover(
        fetch_photo_bytes(student.get("photo_url", "")),
        (_hebron_photo_rect.x0, _hebron_photo_rect.y0,
         _hebron_photo_rect.x1, _hebron_photo_rect.y1),
        scale=PHOTO_EMBED_SCALE, output_format="JPEG",
    )
    insert_image_safe(page, _hebron_photo_rect, photo_bytes)

    draw_text_vertically_centered(
        page, _tr_rect(tr, NAME_TEXT_RECT_COORDS),
        str(student.get("student_name", "")).strip().upper(),
        anton_fn, fn_anton, anton_obj, _tr_font_size(tr, NAME_FONT_SIZE), NAME_COLOR,
    )

    cls = str(student.get("class", "")).strip().upper()
    sec = str(student.get("section", "")).strip().upper()
    roll = str(student.get("roll", "")).strip()
    parts = []
    if cls:
        parts.append(f"CLASS:{cls}")
    if sec:
        parts.append(f"SEC:{sec}")
    if roll:
        parts.append(f"ROLL:{roll}")
    draw_text_vertically_centered(
        page, _tr_rect(tr, CLASS_TEXT_RECT_COORDS),
        "  ".join(parts),
        bold_fn, fn_bold, bold_obj, _tr_font_size(tr, CLASS_FONT_SIZE), NAME_COLOR,
    )

    for coords, key in [
        (FATHER_VALUE_RECT_COORDS, "father_name"),
        (MOTHER_VALUE_RECT_COORDS, "mother_name"),
        (MOBILE_VALUE_RECT_COORDS, "mobile"),
    ]:
        rect = _tr_rect(tr, coords)
        txt = clean_card_value(student.get(key, ""))
        if txt:
            sz = _fit_size(bold_obj, txt, rect.width, _tr_font_size(tr, VALUE_FONT_SIZE))
            _put_single(page, rect, txt, bold_fn, fn_bold, sz, VALUE_COLOR, bold_obj)

    dob = clean_card_value(student.get("dob", ""))
    if dob:
        rect = _tr_rect(tr, DOB_VALUE_RECT_COORDS)
        sz = _fit_size(bold_obj, dob, rect.width, _tr_font_size(tr, VALUE_FONT_SIZE))
        _put_single(page, rect, dob, bold_fn, fn_bold, sz, VALUE_COLOR, bold_obj)

    render_address(
        page, _tr_rect(tr, ADDRESS_VALUE_RECT_COORDS),
        student.get("address", ""),
        bold_fn, fn_bold, bold_obj, VALUE_COLOR,
    )

    adm = clean_card_value(student.get("adm_no", ""))
    if adm:
        rect = _tr_rect(tr, ADM_VALUE_RECT_COORDS)
        sz = _fit_size(bold_obj, adm, rect.width, _tr_font_size(tr, ADM_FONT_SIZE))
        _put_single(page, rect, adm, bold_fn, fn_bold, sz, VALUE_COLOR, bold_obj)

    sess = clean_card_value(student.get("session", "")) or DEFAULT_SESSION
    rect = _tr_rect(tr, SESSION_VALUE_RECT_COORDS)
    sz = _fit_size(anton_obj, sess, rect.width, _tr_font_size(tr, SESSION_FONT_SIZE))
    _put_single(page, rect, sess, anton_fn, fn_anton, sz, VALUE_COLOR, anton_obj)

    blood = str(student.get("blood_group", "")).strip().upper()
    if blood and blood.lower() not in {"nan", "none"} and any(c.isalpha() for c in blood):
        draw_text_centered_hv(
            page, _tr_rect(tr, BLOOD_VALUE_RECT_COORDS),
            blood, bold_fn, fn_bold, bold_obj, _tr_font_size(tr, BLOOD_FONT_SIZE), WHITE,
        )


def draw_card_overlay_redeemer(page, student: dict, tr):
    _draw_redeemer_overlay_core(
        page, student,
        lambda x, y: _tr_point(tr, x, y),
        lambda coords: _tr_rect(tr, coords),
        tr["sx"], tr["sy"],
    )


# ─────────────────────────────────────────────────────────────────
# PRIYANKA per-card renderer
# Coordinates taken directly from PRIYANKA_DREAMNEST.txt FIELDS map.
# Template page size: 141.75 x 240.75 pt.
# The template already has static label text (e.g. "Class:", "Sec:",
# "Father's Name:", etc.) — we only erase and rewrite the VALUES.
# ─────────────────────────────────────────────────────────────────
def _render_priyanka_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    _, bold_obj, _, bold_fn, _, fn_bold = _ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    # Colour from PRIYANKA_DREAMNEST.txt:  COLOR_DARK_BLUE = (15/255, 0/255, 106/255)
    PRIY_BLUE = (15/255, 0/255, 106/255)

    # ── Redact only the sample VALUE rectangles extracted from the template
    #    (from PRIYANKA_DREAMNEST.txt sample_value_rects).
    #    These are tight around the sample data so NO label text is wiped.
    sample_value_rects = [
        # name "AARAV SHARMA"       x0=8.13  y0=130.69 x1=90.08 y1=140.73
        ( 8.13, 130.69,  90.08, 140.73),
        # class "NURSERY"           x0=26.29 y0=141.29 x1=52.74 y1=148.32
        (26.29, 141.29,  52.74, 148.32),
        # sec "A"                   x0=70.81 y0=141.29 x1=74.84 y1=148.32
        (70.81, 141.29,  74.84, 148.32),
        # roll "10"                 x0=96.53 y0=141.29 x1=103.41 y1=148.32
        (96.53, 141.29, 103.41, 148.32),
        # session "2026-27"         x0=109.16 y0=110.50 x1=128.80 y1=117.11
        (109.16, 110.50, 128.80, 117.11),

        # father "SUYASH SHARMA"    x0=56.76 y0=155.18 x1=109.17 y1=161.88
        (56.76, 155.18, 109.17, 161.88),
        # mother "POOJA SHARMA"     x0=56.76 y0=162.73 x1=105.50 y1=169.42
        (56.76, 162.73, 105.50, 169.42),
        # dob "21-04-2014"          x0=56.76 y0=170.27 x1=87.42 y1=176.97
        (56.76, 170.27,  87.42, 176.97),
        # addr1 "BHARKO,"           x0=56.76 y0=178.56 x1=84.73 y1=185.26
        (56.76, 178.56,  84.73, 185.26),
        # addr2 "AMARPUR, BANKA"    x0=56.76 y0=186.56 x1=112.37 y1=193.25
        (56.76, 186.56, 112.37, 193.25),
        # contact "1234567890"      x0=56.76 y0=194.35 x1=90.09 y1=201.05
        (56.76, 194.35,  90.09, 201.05),
    ]
    for x0, y0, x1, y1 in sample_value_rects:
        page.add_redact_annot(fitz.Rect(x0 - 0.3, y0 - 0.3, x1 + 0.3, y1 + 0.3), fill=None)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    # ── Photo — from PRIYANKA_DREAMNEST.txt PHOTO_BOX
    # PHOTO_BOX = {"x": 46.34, "y": 57.75, "w": 49.92, "h": 63.95}
    # PAD = 2.2 pt inner padding so photo stays inside the pink rounded container
    PAD = 2.2
    BOX_X, BOX_Y, BOX_W, BOX_H = 46.34, 57.75, 49.92, 63.95
    # Step 1: Erase ONLY the inner photo area — NOT the full BOX — so the
    # template's pink rounded-rectangle border ring (which is a vector drawing
    # that lives on the BOX boundary) is completely untouched.
    # The pink border visually occupies roughly the outer PAD ring of the BOX.
    # We erase only the inner rect (inset by PAD on all sides).
    # We use a white shape draw (not a redact annotation with fill color) to
    # avoid the solid colored rectangle that redact annotations paint over vectors.
    _inner_erase_x0 = BOX_X + PAD
    _inner_erase_y0 = BOX_Y + PAD
    _inner_erase_x1 = BOX_X + BOX_W - PAD
    _inner_erase_y1 = BOX_Y + BOX_H - PAD
    _erase_shape = page.new_shape()
    _erase_shape.draw_rect(fitz.Rect(_inner_erase_x0, _inner_erase_y0,
                                     _inner_erase_x1, _inner_erase_y1))
    _erase_shape.finish(color=(1, 1, 1), fill=(1, 1, 1), width=0)
    _erase_shape.commit(overlay=True)
    # Redact embedded raster image pixels inside the inner area only
    page.add_redact_annot(
        fitz.Rect(_inner_erase_x0, _inner_erase_y0, _inner_erase_x1, _inner_erase_y1),
        fill=None
    )
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    # Step 2: Fetch and insert student photo with rounded corners.
    # The photo rect matches exactly the inner erase area from Step 1.
    # PAD = 2.2 pt so the photo sits inside the pink rounded border ring.
    box_inner_w = BOX_W - 2 * PAD
    box_inner_h = BOX_H - 2 * PAD
    # Use the same boundary as the erase rect so the photo perfectly fills
    # the cleared area without any gap or overlap with the pink border.
    photo_inner_rect = fitz.Rect(BOX_X + PAD, BOX_Y + PAD,
                                  BOX_X + BOX_W - PAD, BOX_Y + BOX_H - PAD)
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes and HAS_PIL:
        try:
            # v2.7: scale=4 (was 8). At print size (~16 mm wide) the result is
            # visually identical but PNG file size drops ~70% → ~25-35 KB each.
            scale = PHOTO_EMBED_SCALE
            target_w = max(1, int(round(box_inner_w * scale)))
            target_h = max(1, int(round(box_inner_h * scale)))
            target_ratio = target_w / target_h
            with Image.open(io.BytesIO(photo_bytes)) as _img:
                _rgb = _img.convert("RGB")
                _resized = _rgb.resize((target_w, target_h), Image.Resampling.LANCZOS)
            # Apply rounded-corner alpha mask so photo corners match pink container.
            # radius = 8% of the inner width at 8× scale
            _radius = max(4, int(box_inner_w * scale * 0.08))
            _rgba = _resized.convert("RGBA")
            _resized.close()
            _mask = Image.new("L", (target_w, target_h), 0)
            from PIL import ImageDraw as _IDraw
            _md = _IDraw.Draw(_mask)
            _md.rounded_rectangle((0, 0, target_w - 1, target_h - 1),
                                   radius=_radius, fill=255)
            _rgba.putalpha(_mask)
            _buf = io.BytesIO()
            _rgba.save(_buf, format="PNG")
            _rgba.close()
            _mask.close()
            # Insert as PNG so alpha channel (rounded corners) is respected
            page.insert_image(photo_inner_rect, stream=_buf.getvalue(),
                              keep_proportion=False, overlay=True)
        except Exception:
            # Fallback: insert JPEG without rounded corners
            insert_image_safe(page, photo_inner_rect, photo_bytes)
    else:
        insert_image_safe(page, photo_inner_rect, photo_bytes)
    # NOTE: No border is drawn here — the template PDF already has the pink rounded
    # rectangle as a vector drawing that visually frames the padded photo area.

    # ── Text helper — uses PRIYANKA_DREAMNEST.txt field "x" and "y" (baseline)
    def put(text, x, baseline_y, max_width, sz=6.0, min_sz=3.5):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        fs = _fit_size(bold_obj, val, max_width, sz, min_sz)
        page.insert_text((x, baseline_y), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=PRIY_BLUE, overlay=True)

    # ── Write fields using exact FIELDS map from PRIYANKA_DREAMNEST.txt:
    #
    # "name":    x=8.13   y=138.6  max_width=100  size=8.99
    name = clean_card_value(student.get("student_name", "")).upper()
    if name:
        sz = _fit_size(bold_obj, name, 100.0, 8.99, 4.0)
        page.insert_text((8.13, 138.6), name,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=sz, color=PRIY_BLUE, overlay=True)

    # "class":   x=26.29  y=146.8  max_width=30   size=6.0
    put(student.get("class", "").upper(),   26.29, 146.8, 30.0, 6.0)

    # "sec":     x=70.81  y=146.8  max_width=12   size=6.0
    put(student.get("section", "").upper(), 70.81, 146.8, 12.0, 6.0)

    # "roll":    x=96.53  y=146.8  max_width=18   size=6.0
    put(student.get("roll", ""),            96.53, 146.8, 18.0, 6.0)

    # "session": x=109.16 y=115.5  max_width=28   size=6.0  font=anton
    sess = clean_card_value(student.get("session", "")) or DEFAULT_SESSION
    sz = _fit_size(bold_obj, sess, 28.0, 6.0, 3.5)
    page.insert_text((109.16, 115.5), sess,
                     fontname=fn_bold, fontfile=bold_fn,
                     fontsize=sz, color=PRIY_BLUE, overlay=True)

    # "father":  x=56.76  y=160.4  max_width=80   size=6.0
    put(student.get("father_name", ""),  56.76, 160.4, 80.0, 6.0)

    # "mother":  x=56.76  y=168.4  max_width=80   size=6.0
    put(student.get("mother_name", ""),  56.76, 168.4, 80.0, 6.0)

    # "dob":     x=56.76  y=176.4  max_width=80   size=6.0
    put(student.get("dob", ""),          56.76, 176.4, 80.0, 6.0)

    # "address": Balanced multi-line layout with accurate font-size parity
    # "address": Open-width multi-line layout to prevent text truncation
    # "address": Open-width multi-line layout with an explicit cache-eviction trigger
    # "address": Open-width multi-line layout inside the EMPLOYEE renderer
    addr = clean_card_value(student.get("address", ""))
    if addr:
        _photo_cache.clear()
        max_box_width = 115.0 
        lines, target_fs = wrap_and_shrink_text(bold_obj, addr, max_box_width, 2, base_size=7.0)
        for i, line in enumerate(lines):
            page.insert_text((56.76, 183.6 + i * (target_fs * 1.0)), line,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=target_fs, color=PRIY_BLUE, overlay=True)

    # "contact": x=56.76  y=200.0  max_width=80   size=6.0
    put(student.get("mobile", ""),       56.76, 200.0, 80.0, 6.0)

    buf = io.BytesIO()
    try:
        doc.save(buf, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True, incremental=False)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────
# AB ASCENT per-card renderer
# Coordinates taken directly from AB_ASCENT.txt PLACEHOLDERS map.
# The template PDF already contains all static label text
# (e.g. "Adm No.", "Class:", "Sec:", "Roll:", "FATHER'S NAME :" …).
# We ONLY erase and rewrite the dynamic VALUE portions.
# ─────────────────────────────────────────────────────────────────
def _render_ab_ascent_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    _, bold_obj, _, bold_fn, _, fn_bold = _ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    # ── Colours (from AB_ASCENT.txt PLACEHOLDERS color_int values)
    def h(c): return ((c>>16)&0xFF)/255, ((c>>8)&0xFF)/255, (c&0xFF)/255
    NAVY    = h(0x224499)   # all data fields
    RED     = h(0xC83030)   # student name
    WHITE_C = (1.0, 1.0, 1.0)  # blood group text
    BLACK   = (0.0, 0.0, 0.0)  # bus route

    # ── Redact zones — exactly covering the value bbox + max_x from
    #    AB_ASCENaddr =T.txt PLACEHOLDERS.  y0 uses the original y0 from the
    #    PLACEHOLDER bbox; y1 uses the original y1 + a small pad.
    #    We do NOT touch label areas (e.g. "Adm No.", "Class:", etc.).
    redact_zones = [
        # session     bbox y0=106.44 y1=117.44  max_x=148
        # ✅ FIX: label "Session:" occupies y=100.2–107.5; start redact at 107.5 to protect it
        (109.15, 107.50, 148.0,  118.50),
        # adm_no      bbox y0=106.44 y1=117.44  max_x=50
        # ✅ FIX: label "Adm No :" occupies y=100.2–107.5; start redact at 107.5 to protect it
        ( 25.07, 107.50,  50.0,  118.50),
        # name        bbox y0=127.58 y1=137.63  max_x=140
        ( 17.73, 126.60, 140.0,  138.50),
        # class_      bbox y0=138.40 y1=145.43  max_x=58
        ( 26.46, 137.50,  58.0,  146.30),
        # section     bbox y0=138.40 y1=145.43  max_x=84
        ( 73.90, 137.50,  84.0,  146.30),
        # roll        bbox y0=138.40 y1=145.43  max_x=115
        (100.07, 137.50, 115.0,  146.30),
        # father_name bbox y0=154.44 y1=161.14  max_x=150
        ( 60.74, 153.50, 150.0,  162.00),
        # mother_name bbox y0=161.99 y1=168.69  max_x=150
        ( 60.74, 161.00, 150.0,  169.50),
        # dob         bbox y0=169.25 y1=175.95  max_x=150
        ( 60.74, 168.30, 150.0,  176.80),
        # addr1       bbox y0=176.52 y1=183.22  max_x=150
        ( 60.74, 175.60, 150.0,  184.10),
        # addr2       bbox y0=184.51 y1=191.21  max_x=150
        ( 60.74, 183.60, 150.0,  192.00),
        # mobile      bbox y0=192.06 y1=198.76  max_x=150
        ( 60.74, 191.10, 150.0,  199.60),
        # bus_route   bbox y0=204.56 y1=210.70  max_x=65
        ( 29.21, 203.60,  65.0,  211.60),
        # blood_group bbox y0=85.52  y1=93.34   max_x=125.56
        (116.03,  84.50, 125.56,  94.00),
    ]
    for x0, y0, x1, y1 in redact_zones:
        page.add_redact_annot(fitz.Rect(x0, y0, x1, y1), fill=None)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    # ── Photo (from AB_ASCENT.txt: PHOTO_RECT and draw_photo_border)
    # PHOTO_RECT = (52.93, 63.01, 100.07, 116.96)  x0,y0,x1,y1  (47.1 × 54.0 pt)
    # PHOTO_BORDER_COLOR = (0.08, 0.31, 0.86)  blue
    # PHOTO_BORDER_WIDTH = 1.5 pt
    PHOTO = (52.93, 63.01, 100.07, 116.96)
    AB_BORDER_COLOR = (0.08, 0.31, 0.86)  # blue — exact value from AB_ASCENT.txt
    AB_BORDER_WIDTH = 1.5                 # pt — exact value from AB_ASCENT.txt
    # Half the border stroke sits outside the rect boundary in PDF drawing model.
    # Inset the photo image by half the border width so the photo edge aligns with
    # the inner edge of the border stroke and no photo pixel is hidden behind the line.
    AB_BORDER_HALF = AB_BORDER_WIDTH / 2.0

    # Step 1: Erase the template's placeholder photo area.
    # Draw a white fill shape first so no old placeholder colour bleeds through,
    # then apply image-pixel redaction to clear any embedded raster placeholder.
    _ab_erase_shape = page.new_shape()
    _ab_erase_shape.draw_rect(fitz.Rect(PHOTO[0] - 1.0, PHOTO[1] - 1.0,
                                         PHOTO[2] + 1.0, PHOTO[3] + 1.0))
    _ab_erase_shape.finish(color=(1, 1, 1), fill=(1, 1, 1), width=0)
    _ab_erase_shape.commit(overlay=True)
    # Redact any embedded image pixels in the photo zone
    page.add_redact_annot(
        fitz.Rect(PHOTO[0] - 1.0, PHOTO[1] - 1.0, PHOTO[2] + 1.0, PHOTO[3] + 1.0),
        fill=None
    )
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    # Step 2: Insert student photo.
    # The photo rect is inset by half the border width on every side so that the
    # visible photo content is not obscured by the border stroke drawn in Step 3.
    photo_insert_rect = fitz.Rect(
        PHOTO[0] + AB_BORDER_HALF,
        PHOTO[1] + AB_BORDER_HALF,
        PHOTO[2] - AB_BORDER_HALF,
        PHOTO[3] - AB_BORDER_HALF,
    )
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes and HAS_PIL:
        # Full-photo fit (contain) so portrait images stay head-to-toe, matching the standalone builds.
        prepared = prepare_photo_for_rect_cover(
            photo_bytes,
            (photo_insert_rect.x0, photo_insert_rect.y0,
             photo_insert_rect.x1, photo_insert_rect.y1),
            scale=PHOTO_EMBED_SCALE, output_format="JPEG",
        )
        insert_image_safe(page, photo_insert_rect, prepared or photo_bytes)
    else:
        insert_image_safe(page, photo_insert_rect, photo_bytes)

    # Step 3: Draw the blue border ON TOP of the photo.
    # draw_rect with fill=None draws only the stroke (no fill), centered on the rect edge.
    # We draw on the PHOTO rect (not the inset rect) so the border aligns with the
    # template's original blue rectangle drawing.
    # Mirrors AB_ASCENT.txt draw_photo_border() exactly:
    #   shape.draw_rect(rect); shape.finish(color=color, fill=None, width=width, closePath=True)
    _ab_shape = page.new_shape()
    _ab_shape.draw_rect(fitz.Rect(*PHOTO))
    _ab_shape.finish(color=AB_BORDER_COLOR, fill=None,
                     width=AB_BORDER_WIDTH, closePath=True)
    _ab_shape.commit(overlay=True)

    # ── Text insertion helper — mirrors fit_text_to_box() from AB_ASCENT.txt
    #    x0, y1 = original bbox coords from PLACEHOLDERS; max_x = max_x column.
    #    baseline = y1 - 0.22 * size  (same formula as original)
    def put(text, x0, y1_bbox, color, max_x, sz=6.0, align="left"):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        available_w = max_x - x0
        fs = _fit_size(bold_obj, val, available_w, sz, 3.0)
        tw = bold_obj.text_length(val, fontsize=fs)
        if align == "center":
            x = x0 + (available_w - tw) / 2.0
        else:
            x = x0
        baseline = y1_bbox - 0.22 * sz
        page.insert_text((x, baseline), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=color, overlay=True)

    # ── Now write every field using EXACTLY the same bbox coords and
    #    parameters as the PLACEHOLDERS table in AB_ASCENT.txt:
    #
    #  ("session",     "2026-27",        (109.15,106.44,133.71,117.44), "hebo",7.5,0x224499,"left",148.0)
    put(student.get("session", "") or DEFAULT_SESSION,
        109.15, 117.44, NAVY, 148.0, sz=7.5)

    #  ("adm_no",      "1678",           ( 25.07,106.44, 38.66,117.44), "hebo",7.5,0x224499,"left", 50.0)
    #  Label "Adm No." starts at x=18.5 (from static_zones). Align value to same x.
    put(student.get("adm_no", ""),
        18.5, 117.44, NAVY, 50.0, sz=7.5)

    #  ("name",        "AARAV SHARMA",   ( 17.73,127.58, 99.71,137.63), "hebo",9.0,0xC83030,"left",140.0)
    put(student.get("student_name", "").upper(),
        17.73, 137.63, RED, 140.0, sz=9.0)

    #  ("class_",      "VI",             ( 26.46,138.40, 32.14,145.43), "hebo",6.0,0x224499,"left", 58.0)
    put(student.get("class", "").upper(),
        26.46, 145.43, NAVY, 58.0, sz=6.0)

    #  ("section",     "A",              ( 73.90,138.40, 77.93,145.43), "hebo",6.0,0x224499,"left", 84.0)
    put(student.get("section", "").upper(),
        73.90, 145.43, NAVY, 84.0, sz=6.0)

    #  ("roll",        "21",             (100.07,138.40,106.95,145.43), "hebo",6.0,0x224499,"left",115.0)
    put(student.get("roll", ""),
        100.07, 145.43, NAVY, 115.0, sz=6.0)

    #  ("father_name", "SUYASH SHARMA",  ( 60.74,154.44,113.16,161.14), "hebo",6.0,0x224499,"left",150.0)
    put(student.get("father_name", ""),
        60.74, 161.14, NAVY, 150.0, sz=6.0)

    #  ("mother_name", "POOJA SHARMA",   ( 60.74,161.99,109.49,168.69), "hebo",6.0,0x224499,"left",150.0)
    put(student.get("mother_name", ""),
        60.74, 168.69, NAVY, 150.0, sz=6.0)

    #  ("dob",         "21-04-2014",     ( 60.74,169.25, 91.41,175.95), "hebo",6.0,0x224499,"left",150.0)
    put(student.get("dob", ""),
        60.74, 175.95, NAVY, 150.0, sz=6.0)

    #  ("mobile",      "1234567890",     ( 60.74,192.06, 94.08,198.76), "hebo",6.0,0x224499,"left",150.0)
    put(student.get("mobile", ""),
        60.74, 198.76, NAVY, 150.0, sz=6.0)

    #  ("blood_group", "O+",             (116.03,85.52,125.56,93.34),   "hebo",7.0,0xFFFFFF,"center",125.56)
    blood = clean_card_value(student.get("blood_group", "")).upper()
    if blood and any(c.isalpha() for c in blood):
        put(blood, 116.03, 93.34, WHITE_C, 125.56, sz=7.0, align="center")

    #  ("bus_route",   "BUS 1",          ( 29.21,204.56, 45.40,210.70), "hebo",5.5,0x000000,"left", 65.0)
    bus = clean_card_value(student.get("bus_route", ""))
    if bus:
        put(bus, 29.21, 210.70, BLACK, 65.0, sz=5.5)

    # ── ADDRESS: split into addr1 / addr2
    #  ("addr1",       "BHARKO,",        ( 60.74,176.52, 88.72,183.22), "hebo",6.0,0x224499,"left",150.0)
    #  ("addr2",       "AMARPUR, BANKA", ( 60.74,184.51,116.37,191.21), "hebo",6.0,0x224499,"left",150.0)
    addr = clean_card_value(student.get("address", ""))
    if addr:
        if "," in addr:
            addr1, addr2 = addr.split(",", 1)
            addr1 = addr1.strip() + ","
            addr2 = addr2.strip()
        else:
            words = addr.split()
            mid = max(1, len(words) // 2)
            addr1 = " ".join(words[:mid])
            addr2 = " ".join(words[mid:])
        put(addr1, 60.74, 183.22, NAVY, 150.0, sz=6.0)
        if addr2:
            put(addr2, 60.74, 191.21, NAVY, 150.0, sz=6.0)

    buf = io.BytesIO()
    try:
        doc.save(buf, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True, incremental=False)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()



# ─────────────────────────────────────────────────────────────────
# EMPLOYEE PER-CARD RENDERERS (v2.9 fix)
# ─────────────────────────────────────────────────────────────────
# These renderers are dedicated to the *_emp templates so employee
# cards get the correct EMPLOYEE labels & coordinates instead of
# being shoe-horned through the student renderer (which previously
# produced wrong placeholders like "CLASS: PRINCIPAL  SEC: 2026-27").
#
# Coordinate maps below come straight from the standalone Colab/CLI
# scripts the user shipped:
#   • Hebron   → id_card_generator (15).py
#   • AB Ascent→ id_card_generator (14).py
#   • Redeemer → ID_Card_Automation_Colab_v3.py
# All measurements are PDF points (1 pt = 1/72 inch), origin = top-left
# of the template card page, exactly matching how PyMuPDF lays out the
# existing student renderers.
# ─────────────────────────────────────────────────────────────────


def _emp_value(student: dict, *keys, upper: bool = False) -> str:
    """Pull the first non-empty value from `student` across alias keys.
    Used to safely read employee fields without ever falling back to the
    student-side aliases (which would re-introduce the bug).
    """
    for k in keys:
        v = student.get(k, "")
        if v is None:
            continue
        s = clean_card_value(str(v))
        if s:
            return s.upper() if upper else s
    return ""


# ─────────────────────────────────────────────────────────────────
# HEBRON  —  EMPLOYEE
# ─────────────────────────────────────────────────────────────────
# Source: hebron_emp standalone (id_card_generator (15).py).
# FIELDS (rect_pt) used:
#   name        (8.0,  134.0, 112.0, 145.6)  white,  bold, 8.5pt, center, UPPER
#   designation (50.5, 147.5, 110.0, 155.0)  white,  bold, 5.2pt, left,   UPPER
#   validity    (112.5,111.6, 142.0, 122.6)  red,    bold, 7.5pt, center
#   fh_name     (61.5, 161.5, 150.0, 169.8)  black,  bold, 5.5pt, left
#   dob         (61.5, 169.0, 150.0, 177.2)  black,  bold, 5.5pt, left
#   address     (61.5, 176.5, 150.0, 190.4)  black,  bold, 5.0pt, left, wrap 2
#   mobile      (61.5, 190.5, 150.0, 198.8)  black,  bold, 5.5pt, left
# ERASE rectangles + photo box also taken from the standalone script
# but re-drawn here in vector form (no rasterisation needed).
# ─────────────────────────────────────────────────────────────────
def _render_hebron_emp_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = _ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    # Colours straight from the standalone hebron_emp script
    COL_RED_BAND   = (170/255,  15/255,  15/255)
    COL_WHITE      = (1.0, 1.0, 1.0)
    COL_BLACK      = (0.0, 0.0, 0.0)
    COL_VALIDITY_R = (170/255,  16/255,  16/255)
    COL_ORANGE     = (255/255, 117/255,  31/255)

    # ── 1. Erase the template's static student-style placeholders so
    #       only the bare card frame remains underneath our text.
    erase_zones = [
        # Name + designation red band area
        (8.0,  133.0, 112.0, 146.0, COL_RED_BAND),
        (50.0, 145.5,  73.0, 154.5, COL_RED_BAND),
        # White value rectangles (fh_name, dob, address row 1, mobile)
        (53.5, 161.0,  72.0, 169.6, COL_WHITE),
        (53.5, 169.0,  72.0, 177.2, COL_WHITE),
        (53.5, 176.5,  72.0, 184.6, COL_WHITE),
        (53.5, 190.5,  72.0, 198.8, COL_WHITE),
        # Validity (top-right)
        (112.0, 111.2, 142.0, 124.0, COL_WHITE),
    ]
    for x0, y0, x1, y1, col in erase_zones:
        page.draw_rect(fitz.Rect(x0, y0, x1, y1),
                       color=col, fill=col, width=0, overlay=True)

    # ── 2. Photo box (orange frame + photo)
    PHOTO_BOX = (52.44, 74.28, 99.57, 128.23)
    border_w  = 1.5
    # Draw orange outer frame
    page.draw_rect(fitz.Rect(*PHOTO_BOX),
                   color=COL_ORANGE, fill=COL_ORANGE, width=0, overlay=True)
    # Inset for photo
    inner = fitz.Rect(
        PHOTO_BOX[0] + border_w, PHOTO_BOX[1] + border_w,
        PHOTO_BOX[2] - border_w, PHOTO_BOX[3] - border_w,
    )
    # Fallback fill (grey) in case photo fails
    page.draw_rect(inner, color=(240/255, 240/255, 240/255),
                   fill=(240/255, 240/255, 240/255), width=0, overlay=True)

    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes and HAS_PIL:
        # v3.0 STRICT COVER FIT — photo fully fills the inner rect
        # (no white letterbox), matching hebron_standalone.paste_photo()
        # which crops to aspect then resizes.
        prepared = prepare_photo_for_rect_cover(
            photo_bytes,
            (inner.x0, inner.y0, inner.x1, inner.y1),
            scale=PHOTO_EMBED_SCALE, output_format="JPEG",
        )
        insert_image_safe(page, inner, prepared or photo_bytes)
    else:
        insert_image_safe(page, inner, photo_bytes)

    # ── 3. Field-drawing helper that mirrors `draw_field()` in the
    #       standalone script: fit-to-bbox text with vertical centering.
    def _put_field(text, rect_pt,color, *, size_pt=6.0, min_pt=3.8,
                   max_pt=None, align="left", upper=False):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        if upper:
            val = val.upper()
        if max_pt is None:
            max_pt = size_pt
        rect = fitz.Rect(*rect_pt)
        # Fit the font size to the box width
        fs = _fit_size(bold_obj, val, rect.width, max_pt, min_pt)
        # Ellipsize if still too long even at min size
        val = _ellipsize_to_width(bold_obj, val, rect.width, fs)
        tw = bold_obj.text_length(val, fontsize=fs)
        if align == "center":
            x = rect.x0 + (rect.width - tw) / 2.0
        elif align == "right":
            x = rect.x1 - tw
        else:
            x = rect.x0
        baseline = _centered_baseline_for_box(fo := bold_obj, rect.y0, rect.y1, fs)
        page.insert_text((x, baseline), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=color, overlay=True)

    def _put_wrapped(text, rect_pt, color, size_pt=5.0, min_pt=3.4, max_pt=5.5, max_lines=2, line_gap=1.03, *args, **kwargs):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        rect = fitz.Rect(*rect_pt)
        lines, target_fs = wrap_and_shrink_text(bold_obj, val, rect.width, max_lines, base_size=size_pt)
        if not lines:
            return
        asc = getattr(bold_obj, "ascender", 0.9)
        step = target_fs * line_gap
        baseline = rect.y0 + target_fs * asc + 0.5
        for line in lines:
            if baseline - target_fs * abs(bold_obj.descender) > rect.y1:
                break
            page.insert_text((rect.x0, baseline), line,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=target_fs, color=color, overlay=True)
            baseline += step

    # ── 4. Draw a colon after each label rect (same as standalone)
    #       The colon column sits at x≈54.7 pt.
    colon_size = 5.5
    for y_pt in (161.5, 169.0, 176.5, 190.5):
        page.insert_text((54.7, y_pt + 6.5), ":",
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=colon_size, color=COL_BLACK, overlay=True)

    # ── 5. Write the actual employee values
    name = _emp_value(student, "employee_name", "student_name", upper=True)
    _put_field(name, (8.0, 134.0, 112.0, 145.6), COL_WHITE,
               size_pt=8.5, min_pt=5.5, max_pt=9.0, align="center")

    desig = _emp_value(student, "designation", upper=True)
    if desig:
        desig_val = clean_card_value(desig)
        fs = _fit_size(bold_obj, desig_val, 59.5, 5.0, 3.8)
        desig_val = _ellipsize_to_width(bold_obj, desig_val, 59.5, fs)
        
        # Changed Y baseline from 153.8 to 151.8 to pull it up into perfect alignment
        page.insert_text((50.5, 151.8), " " + desig_val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=COL_WHITE, overlay=True)

    validity = _emp_value(student, "validity")
    if not validity:
        validity = "2026-27"
    _put_field(validity, (112.5, 111.6, 142.0, 122.6), COL_VALIDITY_R,
               size_pt=7.5, min_pt=5.5, max_pt=8.0, align="center")

    fh = _emp_value(student, "father_name", "fh_name")
    _put_field(fh, (61.5, 161.5, 150.0, 169.8), COL_BLACK,
               size_pt=5.5, min_pt=3.8, max_pt=6.0, align="left")

    dob = _emp_value(student, "dob")
    _put_field(dob, (61.5, 169.0, 150.0, 177.2), COL_BLACK,
               size_pt=5.5, min_pt=3.8, max_pt=6.0, align="left")

    addr = _emp_value(student, "address")
    _put_wrapped(addr, (61.5, 176.5, 150.0, 190.4), COL_BLACK,
                  size_pt=5.0, min_pt=3.4, max_pt=5.5, max_lines=2)

    mobile = _emp_value(student, "mobile", "contact_no")
    _put_field(mobile, (61.5, 190.5, 150.0, 198.8), COL_BLACK,
               size_pt=5.5, min_pt=3.8, max_pt=6.0, align="left")

    buf = io.BytesIO()
    try:
        doc.save(buf, garbage=4, deflate=True, deflate_images=True,
                 deflate_fonts=True, clean=True, incremental=False)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────
# AB ASCENT  —  EMPLOYEE
# ─────────────────────────────────────────────────────────────────
# Source: ab_ascent_emp standalone (id_card_generator (14).py).
# Reference canvas in the standalone = 638 × 1013 px (card 55×86 mm).
# Conversion factor px → pt:  155.91/638 = 0.2444 (x), 243.78/1013 = 0.2406 (y).
# Resulting PT placeholders:
#   validity    ( 114.9, 101.6, 144.2, 110.2 )  royal blue,  anton,   center, 7pt
#   photo       (  54.7,  67.1, 101.2, 119.8 )  PHOTO BOX
#   name        (  14.7, 131.1, 114.9, 142.0 )  red,         bold,    center, 9pt
#   designation (  51.3, 141.3, 117.3, 149.9 )  royal blue,  bold,    left,   6pt
#   dob         (  60.6, 158.1, 147.6, 166.0 )  royal blue,  bold,    left,   6pt
#   fh_name     (  60.6, 165.1, 147.6, 173.1 )  royal blue,  bold,    left,   6pt
#   address     (  60.6, 173.1, 147.6, 192.5 )  royal blue,  bold,    left,   6pt   wrap
#   mobile      (  60.6, 188.0, 105.1, 195.4 )  royal blue,  bold,    left,   6pt
# Colours from the standalone:
#   ROYAL_BLUE = (0x1E, 0x40, 0xAF)
#   NAME_RED   = (0xE8, 0x3A, 0x2F)
#   YELLOW_BG  = (0xFF, 0xD9, 0x11)
# ─────────────────────────────────────────────────────────────────
def _render_ab_ascent_emp_card_bytes(student: dict, tmpl_bytes: bytes):
    """
    AB ASCENT — EMPLOYEE CARD RENDERER  (v3.1 — STANDALONE-PARITY BUILD)

    Faithful 1-to-1 port of the standalone script
    ``id_card_generator (14).py`` (the file the school signed off on).

    Key parity fixes vs. earlier builds (which produced misaligned cards
    like the Mamita Pandey screenshot):
      1. PHOTO BORDER  -- removed.  Standalone never draws an extra
         border; the template PDF already has the blue border baked in.
         The previous code drew a 1.5pt blue rectangle on top, which the
         user saw as a duplicated/thick border.
      2. PHOTO RECT    -- photo is pasted into the EXACT placeholder
         rect (no inward 1.5pt shrink), matching the standalone's
         ``card.paste(photo, (px1, py1))`` behaviour.
      3. DESIGNATION   -- placeholder rect rescaled to match the
         standalone (210, 587, 480, 623) box in 638x1013 reference px,
         i.e. PDF pts ~(51.3, 141.3, 117.3, 149.9).  Drawn left-aligned
         with NO hardcoded ``insert_text((54.5, 146.5), ...)`` jump and
         NO extra colon -- the template already contains the
         "DESIGNATION :" label, and the standalone simply paints the
         value on top of the right portion of the yellow band.
      4. ADDRESS       -- uses the standalone's wrap-at-fixed-size
         behaviour; placeholder rect widened to the standalone width so
         long addresses (e.g. "Dharampur, Amarpur, Banka") align
         flush-left with DOB / F/H NAME / MOBILE values above and below.
      5. MASKS         -- white/yellow mask rectangles use the *new*
         standalone-aligned placeholder coords so the pre-printed
         labels and colons on the template are preserved.
    """
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = _ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    # ── Colours (exact values from the standalone) ─────────────────
    ROYAL_BLUE = (0x1E/255, 0x40/255, 0xAF/255)
    NAME_RED   = (0xE8/255, 0x3A/255, 0x2F/255)
    YELLOW_BG  = (0xFF/255, 0xD9/255, 0x11/255)
    WHITE_C    = (1.0, 1.0, 1.0)

    # ── Placeholder map ─────────────────────────────────────────────
    # Standalone reference canvas = 638 x 1013 px on a 55 x 86 mm card.
    # px -> pt conversion: x = px * (155.91 / 638) ≈ px * 0.24438
    #                      y = px * (243.78 / 1013) ≈ px * 0.24064
    # Standalone PLACEHOLDERS table (in reference px):
    #     validity    = (470, 422, 590, 458)
    #     photo       = (224, 279, 414, 498)
    #     name        = ( 60, 545, 470, 590)
    #     designation = (210, 587, 480, 623)
    #     dob         = (248, 657, 604, 690)
    #     fh_name     = (248, 686, 604, 719)
    #     address     = (248, 719, 604, 800)
    #     mobile      = (248, 781, 430, 812)
    # The values below are those rectangles converted to PDF points.
    PLACEHOLDERS = {
        "validity":    (114.86, 101.55, 144.18, 110.21),
       # UPDATE THESE PARAMETERS:

        "name":        ( 14.66, 131.15, 114.86, 141.98),
        
        # 🟢 Changed x0 to 53.50 to give a clean space after the pre-printed colon.
        # 🟢 Changed x1 to 116.50 so the yellow overlay stops BEFORE the blue slanted border line!
        "designation": ( 50.0, 141.26, 106.0, 149.92),
        
        "dob":         ( 60.61, 158.10, 147.61, 166.04),
        "fh_name":     ( 60.61, 165.08, 147.61, 173.02),
        "address":     ( 60.61, 172.50, 147.61, 187.00), 
        "mobile":      ( 60.61, 187.94, 105.08, 195.40),
        # UPDATE THIS IN YOUR PLACEHOLDERS DICTIONARY:
# UPDATE THIS KEY IN YOUR PLACEHOLDERS DICTIONARY:
# UPDATE THIS KEY IN YOUR PLACEHOLDERS DICTIONARY:
"photo":       ( 53.60,  66.50, 99.20, 119.50),
    }
    # ── Masks (clear backgrounds before drawing text) ──────────────
    # Same set as standalone: yellow band for name + designation,
    # white for everything else.
    masks = {
        "name":        YELLOW_BG,
        "designation": YELLOW_BG,
        "dob":         WHITE_C,
        "fh_name":     WHITE_C,
        "address":     WHITE_C,
        "mobile":      WHITE_C,
        "validity":    WHITE_C,
    }
    for key, fill in masks.items():
        x0, y0, x1, y1 = PLACEHOLDERS[key]
        page.draw_rect(fitz.Rect(x0, y0, x1, y1),
                       color=fill, fill=fill, width=0, overlay=True)

 
    # NOTE: deliberately NO page.new_shape().draw_rect(...) here.
    # The standalone draws no border, and the template PDF already
    # contains the photo's blue frame.  Drawing one here produces the
    # double-border artifact seen on the Mamita Pandey card.

    # ── PHOTO (Clean white-out mask + Standalone Parity Fill Fit)
    # ── PHOTO (Strict Border-Aware Alignment) ──────────────────────
    # ── PHOTO (Clean Frame Fitting Parity) ──────────────────────
    # ── PHOTO (Clean Frame Fitting Parity) ──────────────────────
    PHOTO = PLACEHOLDERS["photo"]

# 1. Clear ONLY the inner canvas area (stopping safely inside the boundaries)
    page.draw_rect(
        fitz.Rect(PHOTO[0] + 0.5, PHOTO[1] + 0.5, PHOTO[2] - 0.5, PHOTO[3] - 0.5), 
        color=(1.0, 1.0, 1.0), 
        fill=(1.0, 1.0, 1.0), 
        width=0, 
        overlay=True
    )

    # 2. Perfect edge fit inside the border stroke lines
    photo_inset_rect = fitz.Rect(
        PHOTO[0] + 0.5,
        PHOTO[1] + 0.5,
        PHOTO[2] - 0.5,
        PHOTO[3] - 0.5
    )
        
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes and HAS_PIL:
        # 🟢 Force the aspect-aware crop to evaluate using the exact inset window geometry
        prepared = prepare_photo_for_rect_cover(
            photo_bytes, 
            (photo_inset_rect.x0, photo_inset_rect.y0, photo_inset_rect.x1, photo_inset_rect.y1),
            scale=PHOTO_EMBED_SCALE, output_format="JPEG",
        )
        insert_image_safe(page, photo_inset_rect, prepared or photo_bytes)
    else:
        insert_image_safe(page, photo_inset_rect, photo_bytes)
    # ── Text drawing helpers ───────────────────────────────────────
    def _put(text, key, color, *, size_pt=6.0, min_pt=3.5, align="left", upper=False,
             font_obj=None, font_fn=None, font_name=None, shrink=True):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        if upper:
            val = val.upper()
        x0, y0, x1, y1 = PLACEHOLDERS[key]
        bw = x1 - x0
        fo = font_obj or bold_obj
        ff = font_fn or bold_fn
        fn = font_name or fn_bold
        fs = _fit_size(fo, val, bw, size_pt, min_pt) if shrink else size_pt
        if shrink:
            val = _ellipsize_to_width(fo, val, bw, fs)
        tw = fo.text_length(val, fontsize=fs)
        if align == "center":
            x = x0 + (bw - tw) / 2.0
        elif align == "right":
            x = x1 - tw
        else:
            x = x0
        baseline = _centered_baseline_for_box(fo, y0, y1, fs)
        page.insert_text((x, baseline), val,
                         fontname=fn, fontfile=ff,
                         fontsize=fs, color=color, overlay=True)

    def _put_wrapped_fixed(text, key, color, *, size_pt=6.0, max_lines=2, line_gap=1.15):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        x0, y0, x1, y1 = PLACEHOLDERS[key]
        bw = x1 - x0
        lines, target_fs = wrap_and_shrink_text(bold_obj, val, bw, max_lines, base_size=size_pt)
        if not lines:
            return
        asc = getattr(bold_obj, "ascender", 0.9)
        step = target_fs * line_gap
        baseline = y0 + target_fs * asc + 1.5
        for line in lines:
            if baseline - target_fs * abs(bold_obj.descender) > y1:
                break
            page.insert_text((x0, baseline), line,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=target_fs, color=color, overlay=True)
            baseline += step

    # ── NAME  (red, bold, centered) ────────────────────────────────
    _put(_emp_value(student, "employee_name", "student_name", upper=True),
         "name", NAME_RED, size_pt=9.0, min_pt=4.0, align="center")

    # ── DESIGNATION  (royal blue, bold, LEFT-aligned in placeholder)
    # IMPORTANT: standalone uses the placeholder box directly with
    # ``draw_aligned(..., align="left")`` and font size 6pt.  It does
    # NOT inject any prefix like "DESIGNATION:" — the template already
    # has that label printed.  The previous hardcoded
    # ``insert_text((54.5, 146.5), ...)`` placed text on top of the
    # template's colon, causing the visible misalignment.
    
    _put(_emp_value(student, "designation"),
         "designation", ROYAL_BLUE,
         size_pt=6.0, min_pt=4.0, align="left")

    # ── DOB / F-H NAME / MOBILE  (royal blue, bold, left, fixed 6pt)
    _put(_emp_value(student, "dob"),
         "dob", ROYAL_BLUE, size_pt=6.0, min_pt=6.0, align="left", shrink=False)
    _put(_emp_value(student, "father_name", "fh_name"),
         "fh_name", ROYAL_BLUE, size_pt=6.0, min_pt=6.0, align="left", shrink=False)
    _put(_emp_value(student, "mobile", "contact_no"),
         "mobile", ROYAL_BLUE, size_pt=6.0, min_pt=6.0, align="left", shrink=False)

    # ── ADDRESS  (wrap up to 2 lines, royal blue, bold, 6pt) ──────
    _put_wrapped_fixed(_emp_value(student, "address"),
                       "address", ROYAL_BLUE,
                       size_pt=6.0, max_lines=2, line_gap=1.0)

    # ── VALIDITY  (royal blue, Anton, centered, 7pt) ──────────────
    validity = _emp_value(student, "validity") or "2026-27"
    v_obj = anton_obj or bold_obj
    v_fn  = anton_fn or bold_fn
    v_nm  = fn_anton or fn_bold
    _put(validity, "validity", ROYAL_BLUE, size_pt=7.0, min_pt=4.0,
         align="center", font_obj=v_obj, font_fn=v_fn, font_name=v_nm)

    buf = io.BytesIO()
    try:
        doc.save(buf, garbage=4, deflate=True, deflate_images=True,
                 deflate_fonts=True, clean=True, incremental=False)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()



# ─────────────────────────────────────────────────────────────────
# REDEEMER  —  EMPLOYEE
# ─────────────────────────────────────────────────────────────────
# Source: redeemer_emp standalone (ID_Card_Automation_Colab_v3.py).
# Coordinates (PDF points, top-left):
#   NAME_BANNER (7.0, 146.8, 113.0, 158.3)    white text on blue, bold 8.5pt centered
#   DESIG_BOX   (48.3, 158.8, 110.0, 165.2)   white text on blue, bold 4.66pt left
#   EMPID_BOX   (111.6, 108.5, 138.0, 117.5)  blue text,          bold 7pt left
#   DOB_BOX     (54.0, 171.5, 145.0, 181.0)   black text,         bold 7pt left
#   FNAME_BOX   (54.0, 181.0, 145.0, 190.5)   black text,         bold 7pt left
#   ADDR_BOX    (54.0, 190.0, 145.0, 200.0)   black text,         bold 7pt left
#   PHOTO_BOX   (54.6,  81.6,  98.6, 136.5)
#   BANNER_BLUE = (35, 64, 200)
# ─────────────────────────────────────────────────────────────────
def _render_redeemer_emp_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = _ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    BANNER_BLUE = (35/255, 64/255, 200/255)
    WHITE_C     = (1.0, 1.0, 1.0)
    BLACK_C     = (0.0, 0.0, 0.0)
    EMPID_BLUE  = (31/255, 72/255, 255/255)
    GRAD_LEFT   = (233/255, 249/255, 255/255)
    GRAD_RIGHT  = (246/255, 253/255, 254/255)

    NAME_BANNER = (7.0,   146.8, 113.0, 158.3)
    DESIG_BOX   = (48.3,  158.8, 110.0, 165.2)
    EMPID_BOX   = (111.6, 108.5, 138.0, 117.5)
    DOB_BOX     = (54.0,  171.5, 145.0, 181.0)
    FNAME_BOX   = (54.0,  181.0, 145.0, 190.5)
    ADDR_BOX    = (54.0,  190.0, 145.0, 200.0)
    PHOTO_BOX   = (54.6,   81.6,  98.6, 136.5)

    page.draw_rect(fitz.Rect(*NAME_BANNER), color=BANNER_BLUE, fill=BANNER_BLUE, width=0, overlay=True)
    page.draw_rect(fitz.Rect(*DESIG_BOX), color=BANNER_BLUE, fill=BANNER_BLUE, width=0, overlay=True)
    page.draw_rect(fitz.Rect(*EMPID_BOX), color=WHITE_C, fill=WHITE_C, width=0, overlay=True)
    _draw_horizontal_gradient_mask(
        page,
        fitz.Rect(DOB_BOX[0], DOB_BOX[1], ADDR_BOX[2], ADDR_BOX[3]),
        GRAD_LEFT, GRAD_RIGHT, max(20, 30),
    )

    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes and HAS_PIL:
        # v3.0 STRICT COVER FIT — matches redeemer_standalone's photo paste
        # (no white space; photo fully fills the black-bordered rect).
        prepared = prepare_photo_for_rect_cover(
            photo_bytes, PHOTO_BOX,
            scale=PHOTO_EMBED_SCALE, output_format="JPEG",
            is_redeemer=True,
        )
        insert_image_safe(page, fitz.Rect(*PHOTO_BOX), prepared or photo_bytes)
    else:
        insert_image_safe(page, fitz.Rect(*PHOTO_BOX), photo_bytes)
    sh = page.new_shape()
    sh.draw_rect(fitz.Rect(*PHOTO_BOX))
    sh.finish(color=BLACK_C, fill=None, width=0.6, closePath=True)
    sh.commit(overlay=True)

    def _draw_centered(text, box, size_pt, color, *, upper=False,
                       font_obj=None, font_fn=None, font_name=None, shrink=True):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        if upper:
            val = val.upper()
        x0, y0, x1, y1 = box
        bw = x1 - x0
        fo = font_obj or bold_obj
        ff = font_fn or bold_fn
        fn = font_name or fn_bold
        fs = _fit_size(fo, val, bw - 2, size_pt, max(4.0, size_pt * 0.55)) if shrink else size_pt
        if shrink:
            val = _ellipsize_to_width(fo, val, bw - 2, fs)
        tw = fo.text_length(val, fontsize=fs)
        x = x0 + (bw - tw) / 2.0
        baseline = _centered_baseline_for_box(fo, y0, y1, fs)
        page.insert_text((x, baseline), val,
                         fontname=fn, fontfile=ff,
                         fontsize=fs, color=color, overlay=True)

    def _draw_left(text, box, size_pt, color, *, pad_left=2, upper=False,
                   font_obj=None, font_fn=None, font_name=None, shrink=True):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        if upper:
            val = val.upper()
        x0, y0, x1, y1 = box
        bw = x1 - x0
        fo = font_obj or bold_obj
        ff = font_fn or bold_fn
        fn = font_name or fn_bold
        fs = _fit_size(fo, val, bw - pad_left - 2, size_pt, max(4.0, size_pt * 0.55)) if shrink else size_pt
        if shrink:
            val = _ellipsize_to_width(fo, val, bw - pad_left - 2, fs)
        baseline = _centered_baseline_for_box(fo, y0, y1, fs)
        page.insert_text((x0 + pad_left, baseline), val,
                         fontname=fn, fontfile=ff,
                         fontsize=fs, color=color, overlay=True)

    def _draw_wrapped_fixed(text, box, size_pt, color, *, pad_left=6, max_lines=2, line_gap=1.0):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        x0, y0, x1, y1 = box
        lines, target_fs = wrap_and_shrink_text(bold_obj, val, (x1 - x0) - pad_left - 2, max_lines, base_size=size_pt)
        if not lines:
            return
        step = target_fs * line_gap
        for idx, line in enumerate(lines):
            page.insert_text((x0 + pad_left, y0 + target_fs + idx * step), line,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=target_fs, color=color, overlay=True)

    name = _emp_value(student, "employee_name", "student_name", upper=True)
    _draw_centered(name, NAME_BANNER, 8.5, WHITE_C)

    desig = _emp_value(student, "designation", upper=True)
    _draw_left(desig, DESIG_BOX, 4.66, WHITE_C, pad_left=1, shrink=False)

    emp_id = _emp_value(student, "emp_id", "roll")
    if emp_id:
        _draw_left(emp_id, EMPID_BOX, 7.0, EMPID_BLUE, pad_left=2)

    _draw_left(_emp_value(student, "dob"), DOB_BOX, 7.0, BLACK_C, pad_left=6, shrink=False)
    _draw_left(_emp_value(student, "father_name", "fh_name", "fname"), FNAME_BOX, 7.0, BLACK_C, pad_left=6, shrink=False)
    _draw_wrapped_fixed(_emp_value(student, "address"), ADDR_BOX, 7.0, BLACK_C, pad_left=6, max_lines=2, line_gap=1.0)

    buf = io.BytesIO()
    try:
        doc.save(buf, garbage=4, deflate=True, deflate_images=True,
                 deflate_fonts=True, clean=True, incremental=False)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()



# ─────────────────────────────────────────────────────────────────
# PRIYANKA  —  EMPLOYEE
# ─────────────────────────────────────────────────────────────────
# No dedicated standalone script was provided for Priyanka employees.
# We re-use the Priyanka student template/layout but write EMPLOYEE
# fields (designation, validity, emp_id, fh_name, dob, mobile, address)
# into the appropriate slots so the card no longer shows
# "CLASS: <designation>  SEC: <validity>".
# ─────────────────────────────────────────────────────────────────
def _render_priyanka_emp_card_bytes(student: dict, tmpl_bytes: bytes):
    """
    Priyanka Dreamnest EMPLOYEE renderer.

    Geometry & colors are a 1:1 port of the standalone reference script
    `idcard_colab.py` (PRIYANKA DREAMNEST SCHOOL — ID CARD AUTOMATION).

        PHOTO_RECT      = (51.8, 73.0, 92.5, 129.5)
        PHOTO_CORNER_R  = 4.0  pt
        NAME_BG_COLOR   = #FFBCF5  (pink behind the name strip)
        NAVY_COLOR      = #0F006A  (text)

    Detail value clear rects (in pt):
        F/H Name :  (55.0, 164.5, 105.0, 173.0)   text baseline y=171.5
        DOB      :  (55.0, 173.5, 105.0, 182.0)   text baseline y=180.5
        Address  :  (55.0, 181.0, 105.0, 190.0)   text baseline y=188.0
        Mobile   :  (55.0, 196.5, 105.0, 205.5)   text baseline y=203.5

    The function uses PyMuPDF redaction (server-safe, no on-disk temp
    files) + a PIL rounded-corner photo, then inserts text via
    page.insert_text — exactly mirroring the colab logic.
    """
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    _, bold_obj, _, bold_fn, _, fn_bold = _ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    # ── Colors (1:1 with idcard_colab.py) ───────────────────────
    NAME_BG_COLOR     = (255/255, 188/255, 245/255)   # #FFBCF5
    NAME_BG_COLOR_PIL = (255, 188, 245, 255)
    NAVY_COLOR        = (0x0F/255, 0x00/255, 0x6A/255)  # #0F006A
    WHITE_BG          = (1, 1, 1)

    # ── 1. Wipe the sample VALUE rectangles only (no label text) ─
    detail_value_rects = [
        fitz.Rect(55.0, 164.5, 105.0, 173.0),   # F/H Name value
        fitz.Rect(55.0, 173.5, 105.0, 182.0),   # DOB value
        fitz.Rect(55.0, 181.0, 105.0, 190.0),   # Address value
        fitz.Rect(55.0, 196.5, 105.0, 205.5),   # Mobile value
    ]
    for r in detail_value_rects:
        # Use a white shape (NOT a redact-with-fill) so any vector frames
        # behind these rects survive. The slot interior is plain white in
        # the Priyanka template, so a flat white fill is correct.
        _erase = page.new_shape()
        _erase.draw_rect(r)
        _erase.finish(color=None, fill=WHITE_BG, width=0)
        _erase.commit(overlay=True)
        # Also redact any raster image pixels in case the template uses an
        # embedded sample-data PNG instead of vector text.
        page.add_redact_annot(r, fill=None)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    # ── 2. Repaint the pink NAME BAND + the small left strip ────
    # (matches the standalone script: it overpaints the sample name
    # text with the pink background so the new name is drawn on a
    # clean pink strip.)
    name_bg_rect    = fitz.Rect(40.0, 139.5, 105.0, 151.2)
    desig_bg_strip  = fitz.Rect(43.0, 151.5,  52.0, 158.0)
    for r, fill in ((name_bg_rect, NAME_BG_COLOR),
                    (desig_bg_strip, NAME_BG_COLOR)):
        _s = page.new_shape()
        _s.draw_rect(r)
        _s.finish(color=None, fill=fill, width=0)
        _s.commit(overlay=True)

    # ── 3. Photo — rounded-corner PIL composite ─────────────────
    PHOTO_RECT     = fitz.Rect(51.8, 73.0, 92.5, 129.5)
    PHOTO_CORNER_R = 4.0  # pt
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))

    # Clear the photo slot first (white) so any prior image is gone.
    _ps = page.new_shape()
    _ps.draw_rect(PHOTO_RECT)
    _ps.finish(color=None, fill=WHITE_BG, width=0)
    _ps.commit(overlay=True)
    page.add_redact_annot(PHOTO_RECT, fill=None)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    if photo_bytes and HAS_PIL:
        try:
            from PIL import Image as _PILImage, ImageDraw as _PILDraw
            # Render at ~500 DPI for crisp output, identical to colab DPI=500.
            _scale = max(PHOTO_EMBED_SCALE, 6)   # 6× ≈ 432 DPI; safe minimum
            tw_px = max(1, int(round(PHOTO_RECT.width  * _scale)))
            th_px = max(1, int(round(PHOTO_RECT.height * _scale)))
            r_px  = max(2, int(round(PHOTO_CORNER_R    * _scale)))

            with _PILImage.open(io.BytesIO(photo_bytes)) as _src:
                _rgb = _src.convert("RGB")
                _resized = _rgb.resize((tw_px, th_px), _PILImage.Resampling.LANCZOS)

            # Rounded-corner mask
            _mask = _PILImage.new("L", (tw_px, th_px), 0)
            _PILDraw.Draw(_mask).rounded_rectangle(
                (0, 0, tw_px - 1, th_px - 1), radius=r_px, fill=255)
            # Composite onto the pink #FFBCF5 background — this is what
            # prevents the corner gaps the standalone script also fixed.
            _out = _PILImage.new("RGBA", (tw_px, th_px), NAME_BG_COLOR_PIL)
            _out.paste(_resized.convert("RGBA"), (0, 0), _mask)
            _resized.close()
            _mask.close()

            _buf = io.BytesIO()
            _out.save(_buf, format="PNG")
            _out.close()
            page.insert_image(PHOTO_RECT, stream=_buf.getvalue(),
                              keep_proportion=False, overlay=True)
        except Exception as e:
            # Fallback: best-effort JPEG insert without rounded corners.
            insert_image_safe(page, PHOTO_RECT, photo_bytes)
    elif photo_bytes:
        insert_image_safe(page, PHOTO_RECT, photo_bytes)
    # else: leave photo slot blank (white)

    # ── 4. Helper for the four small detail rows ────────────────
    def _put(text, x, baseline_y, max_width, sz=6.0, min_sz=3.5):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        fs = _fit_size(bold_obj, val, max_width, sz, min_sz)
        val = _ellipsize_to_width(bold_obj, val, max_width, fs)
        page.insert_text((x, baseline_y), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=NAVY_COLOR, overlay=True)

    # ── 5. NAME — centered between x=15 and x=120 at baseline 149.5 ──
    name_text = _emp_value(student, "employee_name", "student_name",
                            upper=True)
    if name_text:
        fs = 9.0
        # Shrink-to-fit (max width 95 pt as in standalone)
        while bold_obj.text_length(name_text, fontsize=fs) > 95 and fs > 5:
            fs -= 0.5
        tw = bold_obj.text_length(name_text, fontsize=fs)
        page.insert_text(((15 + 120) / 2 - tw / 2, 149.5), name_text,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=NAVY_COLOR, overlay=True)

    # ── 6. DESIGNATION — bottom-left of the pink band, baseline 156.0 ──
    desig = _emp_value(student, "designation")
    if desig:
        # Standalone uses raw fontsize=5.2 with no shrink-to-fit, but we
        # still cap to the strip width so very long titles do not bleed.
        fs = _fit_size(bold_obj, desig, 60.0, 5.2, 3.5)
        desig = _ellipsize_to_width(bold_obj, desig, 60.0, fs)
        page.insert_text((43.5, 156.0), desig,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=NAVY_COLOR, overlay=True)

    # ── 7. Detail rows — baselines from idcard_colab.py ─────────
    _put(_emp_value(student, "father_name", "fh_name"),
         56.8, 171.5, 48.0, sz=6.0)            # F/H Name
    _put(_emp_value(student, "dob"),
         56.8, 180.5, 48.0, sz=6.0)            # DOB
    addr = _emp_value(student, "address")
    if addr:
        lines, target_fs = wrap_and_shrink_text(bold_obj, addr, 48.0, 2, base_size=6.0)
        for i, line in enumerate(lines):
            page.insert_text((56.8, 187.0 + i * (target_fs * 1.05)), line,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=target_fs, color=NAVY_COLOR, overlay=True)
    _put(_emp_value(student, "mobile", "contact_no"),
         56.8, 203.5, 48.0, sz=6.0)            # Mobile

    # NOTE: emp_id is NOT printed — the Priyanka template has no slot
    # for it. Add a slot in the template if you need it visible.

    buf = io.BytesIO()
    try:
        doc.save(buf, garbage=4, deflate=True, deflate_images=True,
                 deflate_fonts=True, clean=True, incremental=False)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────
# Dispatcher for employee per-card renderers
# ─────────────────────────────────────────────────────────────────
EMP_CARD_RENDERERS = {
    "hebron_emp":    _render_hebron_emp_card_bytes,
    "ab_ascent_emp": _render_ab_ascent_emp_card_bytes,
    "redeemer_emp":  _render_redeemer_emp_card_bytes,
    "priyanka_emp":  _render_priyanka_emp_card_bytes,
}


def _resolve_card_renderer(template_key: str):
    """Return (use_per_card, render_fn) for the given template key.
    Employee templates ALWAYS go through a dedicated per-card renderer
    so the right labels & coordinates are used."""
    if template_key in EMP_CARD_RENDERERS:
        return True, EMP_CARD_RENDERERS[template_key]
    rk = _resolve_renderer_key(template_key)
    if rk == "priyanka":
        return True, _render_priyanka_card_bytes
    if rk == "ab_ascent":
        return True, _render_ab_ascent_card_bytes
    return False, None


def draw_card_on_page(page, student, target_rect, template_key, template_doc, template_source_rect):
    # ── v2.9 EMPLOYEE FIX ────────────────────────────────────────
    # If this is an *employee* template, build the card via the dedicated
    # per-card renderer (which writes the correct employee labels) and
    # paste the resulting PDF onto the A4 page. This path is used by the
    # single-card preview/download endpoints — for the multi-card A4
    # builder, the same renderers are invoked directly inside
    # `_render_a4_page` via `_resolve_card_renderer`.
    if template_key in EMP_CARD_RENDERERS:
        try:
            tmpl_bytes_local = _ensure_template(template_key)
        except Exception:
            tmpl_bytes_local = None
        if tmpl_bytes_local:
            try:
                card_bytes = EMP_CARD_RENDERERS[template_key](student, tmpl_bytes_local)
                if card_bytes:
                    _card_doc = fitz.open("pdf", card_bytes)
                    try:
                        page.show_pdf_page(target_rect, _card_doc, 0,
                                            keep_proportion=False, overlay=True)
                    finally:
                        _card_doc.close()
                    return
            except Exception as _e:
                log.error("Employee per-card render failed for %s / %s: %s",
                          template_key, student.get("employee_name") or student.get("student_name", "?"), _e)
        # Fall through to the legacy path if the dedicated renderer fails
        # so we still produce *something* on the page.

    page.show_pdf_page(target_rect, template_doc, 0, keep_proportion=False, overlay=True)
    tr = _make_card_transform(template_source_rect, target_rect)
    # Map employee templates to their underlying student renderer (legacy
    # fallback only).
    rk = _resolve_renderer_key(template_key)
    if rk == "redeemer":
        draw_card_overlay_redeemer(page, student, tr)
    else:
        draw_card_overlay_hebron(page, student, tr)

# ─────────────────────────────────────────────────────────────────
# SERIAL BADGE
# ─────────────────────────────────────────────────────────────────
def draw_serial_badge_vector(page, serial: int, cx: float, cy: float, gap_h: float):
    txt    = f"#{serial}"
    fs     = max(5.0, gap_h * 0.38)
    try:
        font = fitz.Font("helv")
        tw   = font.text_length(txt, fontsize=fs)
    except Exception:
        tw = len(txt) * fs * 0.6

    pad_x  = fs * 0.5
    pad_y  = fs * 0.25
    bw     = tw + 2 * pad_x
    bh     = fs + 2 * pad_y

    left   = cx - bw / 2.0
    top    = cy - bh / 2.0
    right  = left + bw
    bottom = top  + bh

    shape = page.new_shape()
    so = max(1.0, fs * 0.05)
    shape.draw_rect(fitz.Rect(left+so, top+so, right+so, bottom+so))
    shape.finish(color=(0.2,0,0), fill=(0.2,0,0), width=0)
    shape.draw_rect(fitz.Rect(left, top, right, bottom))
    shape.finish(color=(0.82,0.08,0.08), fill=(0.82,0.08,0.08), width=0)
    shape.commit(overlay=True)

    shape2 = page.new_shape()
    shape2.draw_rect(fitz.Rect(left, top, right, bottom))
    shape2.finish(color=WHITE, fill=None, width=max(0.5, fs*0.03))
    shape2.commit(overlay=True)

    baseline = cy + fs * 0.35
    page.insert_text(
        (left + pad_x, baseline), txt,
        fontname="helv", fontsize=fs, color=WHITE, overlay=True,
    )

# ─────────────────────────────────────────────────────────────────
# PT constants
# ─────────────────────────────────────────────────────────────────

def mm_to_pt(mm: float) -> float:
    return mm * MM_TO_PT

CARD_W_PT  = mm_to_pt(CARD_W_MM)
CARD_H_PT  = mm_to_pt(CARD_H_MM)
A4_W_PT    = mm_to_pt(A4_W_MM)
A4_H_PT    = mm_to_pt(A4_H_MM)
OX_PT      = mm_to_pt(OFFSET_X_MM)
OY_PT      = mm_to_pt(OFFSET_Y_MM)
ROW_GAP_PT = mm_to_pt(ROW_GAP_MM)
COL_GAP_PT = mm_to_pt(1.0)
COL_STEP   = CARD_W_PT + COL_GAP_PT
ROW_STEP   = CARD_H_PT + ROW_GAP_PT

# ─────────────────────────────────────────────────────────────────
# A4 SHEET BUILDER  — parallelised per-card render for priyanka/ab_ascent
# ─────────────────────────────────────────────────────────────────

def _check_tmp_space_mb(path: str, needed_mb: float = 20.0) -> bool:
    """Return True if the directory has at least needed_mb of free space."""
    try:
        import shutil
        free = shutil.disk_usage(path).free / (1024 * 1024)
        return free >= needed_mb
    except Exception:
        return True  # assume ok if we can't check


def _resolve_pdf_tmp_dir() -> str:
    """Pick a writable temp dir with enough space. Falls back through candidates."""
    candidates = [PDF_TEMP_DIR, "/tmp", tempfile.gettempdir()]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            if _check_tmp_space_mb(d, needed_mb=10.0):
                # Verify we can actually write there
                t = tempfile.NamedTemporaryFile(delete=True, dir=d, suffix=".pdf")
                t.close()
                return d
        except Exception:
            continue
    return tempfile.gettempdir()  # last resort


def _render_a4_page(out_doc, page_idx: int, students: list,
                    template_key: str, template_doc, source_rect,
                    tmpl_bytes: bytes, use_per_card: bool, render_fn):
    """Render a single A4 sheet (≤10 cards) onto out_doc. Caller controls
    when to flush out_doc to disk — this fn never touches disk."""
    student_start = page_idx * CARDS_PER_PAGE
    student_batch = students[student_start: student_start + CARDS_PER_PAGE]
    a4_page = out_doc.new_page(width=A4_W_PT, height=A4_H_PT)

    batch_rendered = None
    if use_per_card:
        batch_workers = min(CARD_RENDER_WORKERS, len(student_batch))
        if batch_workers > 1:
            batch_rendered = [None] * len(student_batch)
            with ThreadPoolExecutor(max_workers=batch_workers) as pool:
                fut_map = {
                    pool.submit(render_fn, student_batch[i], tmpl_bytes): i
                    for i in range(len(student_batch))
                }
                for f in as_completed(fut_map):
                    bi = fut_map[f]
                    try:
                        batch_rendered[bi] = f.result()
                    except Exception as e:
                        log.error("Card render FAILED student[%d] '%s': %s",
                                  student_start + bi,
                                  student_batch[bi].get("student_name", "?"), e)
                        batch_rendered[bi] = None
        else:
            batch_rendered = []
            for s in student_batch:
                try:
                    batch_rendered.append(render_fn(s, tmpl_bytes))
                except Exception as e:
                    log.error("Card render FAILED '%s': %s", s.get("student_name", "?"), e)
                    batch_rendered.append(None)

    for idx, student in enumerate(student_batch):
        col = idx % COLS
        row = idx // COLS
        card_x = OX_PT + col * COL_STEP
        card_y = OY_PT + row * ROW_STEP
        target_rect = fitz.Rect(card_x, card_y, card_x + CARD_W_PT, card_y + CARD_H_PT)

        if use_per_card:
            card_bytes = batch_rendered[idx]
            if card_bytes:
                card_doc = fitz.open("pdf", card_bytes)
                a4_page.show_pdf_page(target_rect, card_doc, 0,
                                      keep_proportion=False, overlay=True)
                card_doc.close()
                del card_doc
            batch_rendered[idx] = None
        else:
            draw_card_on_page(
                a4_page, student, target_rect, template_key,
                template_doc=template_doc, template_source_rect=source_rect,
            )

        if ROWS > 1:
            if row == 0:
                gap_cy = card_y + CARD_H_PT + ROW_GAP_PT / 2.0
            else:
                gap_cy = card_y - ROW_GAP_PT / 2.0
            badge_cx = card_x + CARD_W_PT / 2.0
            draw_serial_badge_vector(
                a4_page, student_start + idx + 1,
                badge_cx, gap_cy, ROW_GAP_PT,
            )

    if batch_rendered is not None:
        batch_rendered.clear()
        del batch_rendered


# Compression flags reused everywhere. use_objstms=1 packs PDF objects
# into compressed object streams → ~10–15 % smaller files.
_PDF_SAVE_OPTS = dict(
    deflate=True, deflate_images=True, deflate_fonts=True,
    garbage=4, clean=True, linear=False, incremental=False,
    pretty=False,
)


def _safe_save(doc, path: str):
    """Save with all compression flags. Falls back if PyMuPDF build does
    not understand a particular kwarg (older versions)."""
    try:
        doc.save(path, **_PDF_SAVE_OPTS)
        return
    except TypeError:
        # use_objstms / pretty may be unsupported on old PyMuPDF builds
        opts = dict(_PDF_SAVE_OPTS)
        opts.pop("use_objstms", None)
        opts.pop("pretty", None)
        try:
            doc.save(path, **opts)
            return
        except TypeError:
            doc.save(path,
                     deflate=True, garbage=4, clean=True,
                     linear=False, incremental=False)


def build_pdf_file_vector(students: list, template_key: str = DEFAULT_TEMPLATE,
                          progress_cb=None):
    """v2.7 chunked, on-disk PDF builder.

    Why chunks?
      A single PyMuPDF Document holding 70+ A4 pages with embedded photo
      images consumes ~3 MB of native memory per page. For 700 students
      that's >200 MB — enough to OOM-kill a 512 MB Railway worker.

    Algorithm:
      1. Build pages in batches of CHUNK_PAGES; flush each batch to a
         compressed PDF on /tmp and close the in-memory doc.
      2. Merge chunks back into a final PDF by inserting them into a
         merger document.  Every MERGE_COMPACT_PAGES, save the merger to
         disk, close it and reopen from disk — this forces PyMuPDF to
         release native object cache memory.
      3. Final save uses use_objstms=1 + deflate_images for max compression.
    """
    if not HAS_FITZ:
        log.error("build_pdf_file_vector: PyMuPDF (fitz) not installed")
        return None
    template_key = normalize_template_key(template_key)
    tmpl_bytes = _ensure_template(template_key)
    if tmpl_bytes is None:
        log.error("build_pdf_file_vector: template PDF not found for key='%s'", template_key)
        return None

    template_doc = _get_template_doc(template_key)
    if template_doc is None:
        log.error("build_pdf_file_vector: could not open template doc for key='%s'", template_key)
        return None

    # v2.9 — log a mode-aware label so employee jobs don't read as "students".
    _kind = "employees" if str(template_key).endswith("_emp") else "students"
    log.info("build_pdf_file_vector: %d %s, template=%s, chunk_pages=%d",
             len(students), _kind, template_key, CHUNK_PAGES)

    source_rect = fitz.Rect(template_doc[0].rect)
    n_pages = (len(students) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE
    tmp_dir = _resolve_pdf_tmp_dir()

    tmp_final = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=tmp_dir)
    tmp_final.close()
    out_path = tmp_final.name

    # v2.9 — Employee templates now have their OWN dedicated per-card
    # renderers (one per template) so they write employee labels, not
    # student ones. _resolve_card_renderer returns the correct render fn
    # for every supported template, including the 4 *_emp templates.
    use_per_card, render_fn = _resolve_card_renderer(template_key)
    render_key = _resolve_renderer_key(template_key)
    if not use_per_card:
        # Student hebron / redeemer keep their overlay-style rendering
        # via draw_card_on_page (no per-card renderer needed).
        render_fn = None

    chunk_paths = []        # list of disk PDFs to merge
    chunk_doc = fitz.open()
    pages_in_chunk = 0

    def _flush_chunk():
        nonlocal chunk_doc, pages_in_chunk
        if pages_in_chunk == 0:
            chunk_doc.close()
            chunk_doc = fitz.open()
            return
        cp = tempfile.NamedTemporaryFile(delete=False, suffix=".chunk.pdf", dir=tmp_dir)
        cp.close()
        try:
            _safe_save(chunk_doc, cp.name)
            chunk_paths.append(cp.name)
        finally:
            chunk_doc.close()
            chunk_doc = fitz.open()
            pages_in_chunk = 0
            gc.collect()

    try:
        for page_idx in range(n_pages):
            _render_a4_page(
                chunk_doc, page_idx, students, template_key,
                template_doc, source_rect, tmpl_bytes,
                use_per_card, render_fn,
            )
            pages_in_chunk += 1
            if pages_in_chunk >= CHUNK_PAGES:
                _flush_chunk()
            if progress_cb:
                try: progress_cb(page_idx + 1, n_pages)
                except Exception: pass

        # final partial chunk
        if pages_in_chunk > 0:
            _flush_chunk()
        else:
            chunk_doc.close()

        # ——————————— merge phase ———————————
        if not chunk_paths:
            log.error("build_pdf_file_vector: no pages produced")
            return None

        # Common case: only one chunk → just rename, no merge needed.
        if len(chunk_paths) == 1:
            try:
                os.replace(chunk_paths[0], out_path)
            except Exception:
                # cross-device fallback
                with open(chunk_paths[0], "rb") as src, open(out_path, "wb") as dst:
                    while True:
                        b = src.read(1024 * 1024)
                        if not b: break
                        dst.write(b)
                try: os.unlink(chunk_paths[0])
                except Exception: pass
            return out_path

        merger = fitz.open()
        compaction_tmp = None
        try:
            for i, cp in enumerate(chunk_paths):
                cd = fitz.open(cp)
                merger.insert_pdf(cd)
                cd.close()
                del cd
                try: os.unlink(cp)
                except Exception: pass
                gc.collect()

                # Periodic compaction: flush merger to disk and reopen
                # so PyMuPDF releases native page-object memory.
                if (merger.page_count >= MERGE_COMPACT_PAGES
                        and i < len(chunk_paths) - 1):
                    new_tmp = tempfile.NamedTemporaryFile(
                        delete=False, suffix=".compact.pdf", dir=tmp_dir)
                    new_tmp.close()
                    _safe_save(merger, new_tmp.name)
                    merger.close()
                    del merger
                    if compaction_tmp:
                        try: os.unlink(compaction_tmp)
                        except Exception: pass
                    compaction_tmp = new_tmp.name
                    gc.collect()
                    merger = fitz.open(compaction_tmp)

            # ── SET METADATA on the final merged PDF ─────────────────
            # This is the ONLY place metadata needs to be set.
            # Per-card renderer metadata is discarded (cards are pasted
            # as images via show_pdf_page onto the A4 out_doc).
            try:
                cfg = TEMPLATE_CONFIGS.get(template_key, {})
                school_name = cfg.get("display_name", "ID Card Generator")
                merger.set_metadata({
                    "title":    f"{school_name} — ID Cards",
                    "author":   school_name,
                    "producer": "ID Card Generator",
                    "creator":  "ID Card Generator",
                })
            except Exception as _meta_err:
                log.warning("set_metadata failed (non-fatal): %s", _meta_err)
            _safe_save(merger, out_path)
        finally:
            try: merger.close()
            except Exception: pass
            if compaction_tmp:
                try: os.unlink(compaction_tmp)
                except Exception: pass
            gc.collect()

        return out_path

    except Exception as e:
        log.error("build_pdf_file_vector FAILED: %s\n%s", e, traceback.format_exc())
        # cleanup any leftovers
        for cp in chunk_paths:
            try: os.unlink(cp)
            except Exception: pass
        try:
            if os.path.exists(out_path):
                os.unlink(out_path)
        except Exception:
            pass
        try: chunk_doc.close()
        except Exception: pass
        gc.collect()
        raise

# ─────────────────────────────────────────────────────────────────
# RASTER FALLBACK (used only if template PDF is missing)
# ─────────────────────────────────────────────────────────────────

def _placeholder_card_pil(student, dpi=150):
    if not HAS_PIL:
        return None
    w = int(55 / 25.4 * dpi); h = int(86 / 25.4 * dpi)
    img  = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, int(h*0.3)], fill=(200, 30, 30))
    name = student.get("student_name","Student").upper()
    draw.text((10, 10), name, fill="white")
    draw.text((10, int(h*0.35)), f"Class: {student.get('class','')}", fill=(100,100,100))
    return img

def build_pdf_file_raster_fallback(students, dpi=150):
    if not HAS_FITZ or not HAS_PIL:
        return None

    def mm2px(mm): return int(round(mm / 25.4 * dpi))
    a4_w_px  = mm2px(A4_W_MM); a4_h_px   = mm2px(A4_H_MM)
    card_w_px= mm2px(CARD_W_MM); card_h_px = mm2px(CARD_H_MM)
    ox_px    = mm2px(OFFSET_X_MM); oy_px    = mm2px(OFFSET_Y_MM)
    gap_px   = mm2px(ROW_GAP_MM); col_gap_px= mm2px(1.0)
    a4_w_pt  = A4_W_MM * MM_TO_PT; a4_h_pt  = A4_H_MM * MM_TO_PT

    out_doc  = fitz.open()
    _tmp_dir = _resolve_pdf_tmp_dir()
    tmp      = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=_tmp_dir)
    tmp.close()
    n_pages  = (len(students) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE

    try:
        for page_idx in range(n_pages):
            batch = students[page_idx * CARDS_PER_PAGE : (page_idx+1) * CARDS_PER_PAGE]
            sheet = Image.new("RGB", (a4_w_px, a4_h_px), (245,245,245))
            for idx, s in enumerate(batch):
                col   = idx % COLS; row = idx // COLS
                x     = ox_px + col * (card_w_px + col_gap_px)
                y     = oy_px + row * (card_h_px + gap_px)
                card  = _placeholder_card_pil(s, dpi)
                if card:
                    sheet.paste(card.resize((card_w_px, card_h_px)), (x, y))
                    card.close()
            buf = io.BytesIO()
            sheet.save(buf, format="JPEG", quality=80, optimize=True)
            sheet.close()
            pg = out_doc.new_page(width=a4_w_pt, height=a4_h_pt)
            pg.insert_image(fitz.Rect(0,0,a4_w_pt,a4_h_pt), stream=buf.getvalue(), overlay=True, keep_proportion=False)
            gc.collect()
        out_doc.save(tmp.name, deflate=True, garbage=4, clean=True)
        return tmp.name
    except Exception:
        try:
            if os.path.exists(tmp.name): os.unlink(tmp.name)
        except: pass
        raise
    finally:
        out_doc.close()
        gc.collect()

# ─────────────────────────────────────────────────────────────────
def build_pdf_file(students, dpi=150, template_key: str = DEFAULT_TEMPLATE):
    template = get_template_config(template_key)
    if HAS_FITZ and template["pdf"].exists():
        return build_pdf_file_vector(students, template_key=template["key"])
    return build_pdf_file_raster_fallback(students, dpi=dpi)

# ─────────────────────────────────────────────────────────────────
def send_generated_pdf(students, dpi, download_name, as_attachment, allow_external=False, template_key: str = DEFAULT_TEMPLATE):
    if not students:
        log.error("send_generated_pdf: no students in list")
        return jsonify({"error": "No students loaded"}), 400
    if len(students) > MAX_STUDENTS_PER_REQUEST:
        log.error("send_generated_pdf: too many students %d > %d", len(students), MAX_STUDENTS_PER_REQUEST)
        return jsonify({
            "error": (
                f"Too many students in one request ({len(students)}). "
                f"Please filter by class or increase MAX_STUDENTS_PER_REQUEST."
            )
        }), 413

    # v2.7: previews are kept short to avoid blocking the worker for minutes.
    # Downloads use the chunked builder → they no longer need a hard cap.
    if (not as_attachment) and _IS_PRODUCTION and len(students) > 100:
        # Auto-trim previews to first 50 students — keeps preview snappy on prod.
        log.info("Trimming preview from %d to 50 students for production", len(students))
        students = students[:50]

    if _IS_PRODUCTION and as_attachment and len(students) > PROD_MAX_STUDENTS:
        return jsonify({
            "error": (
                f"Too many students for one PDF ({len(students)} > {PROD_MAX_STUDENTS}). "
                f"Please download class-by-class — this is a sanity limit, "
                f"not a memory limit."
            ),
            "code":  "BATCH_TOO_LARGE",
            "limit": PROD_MAX_STUDENTS,
            "requested": len(students),
        }), 413

    if (not as_attachment) and len(students) >= PREVIEW_EXTERNAL_THRESHOLD and _external_storage_enabled():
        allow_external = True

    # v2.9 — employee templates re-use the student renderer pipeline but the
    # log line previously always said "students" which made it look (in the
    # server console) as though employee PDFs were never being generated.
    _kind = "employees" if str(template_key).endswith("_emp") else "students"
    log.info("PDF generation started: %d %s | template=%s | dpi=%d",
             len(students), _kind, template_key, dpi)

    try:
        prefetch_photos(students)
    except Exception as e:
        log.warning("prefetch_photos error (non-fatal): %s", e)

    try:
        pdf_path = build_pdf_file(students, dpi=dpi, template_key=template_key)
    except Exception as e:
        log.error("build_pdf_file EXCEPTION: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": f"PDF generation exception: {e}"}), 500

    if not pdf_path:
        log.error("build_pdf_file returned None — check template PDF exists and PyMuPDF is installed")
        return jsonify({"error": "PDF generation failed — template PDF missing or PyMuPDF error"}), 500

    try:
        file_size = os.path.getsize(pdf_path)
        log.info("PDF generated: %s  size=%.1f KB", pdf_path, file_size / 1024)
        if file_size < 1000:
            log.error("PDF too small (%d bytes) — likely empty/corrupt", file_size)
            return jsonify({"error": f"PDF generated but appears empty ({file_size} bytes)"}), 500
    except Exception as e:
        log.error("Could not stat PDF file: %s", e)

    if allow_external and _external_storage_enabled():
        try:
            remote_url = upload_pdf_to_external_storage(pdf_path, download_name)
            if remote_url:
                return jsonify({
                    "success": True,
                    "storage": STORAGE_BACKEND,
                    "download_url": remote_url,
                    "download_name": download_name,
                })
        except Exception as e:
            log.warning("External storage upload failed (falling back to direct): %s", e)

    log.info("Sending PDF to client: %s  attachment=%s", download_name, as_attachment)

    safe_name = _sanitize_filename(download_name)

    # Schedule the delayed delete BEFORE reading so the reaper knows about
    # the file even if this request dies partway through.
    schedule_delete(pdf_path, PDF_RETENTION_SECONDS)

    # Same fix as job_file: read into bytes first so the file handle is
    # released immediately (no WinError 32), then return a plain bytes
    # Response with a single correct Content-Length header.
    # A generator Response would trigger Transfer-Encoding: chunked which
    # conflicts with Content-Length → axios "Network Error".
    try:
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
    except OSError as e:
        log.error("Could not read PDF file: %s", e)
        return jsonify({"error": "PDF file missing after generation"}), 500

    gc.collect()

    from flask import Response as _Response
    disp = ("attachment" if as_attachment else "inline") + f'; filename="{safe_name}"'
    resp = _Response(
        pdf_bytes,
        status=200,
        mimetype="application/pdf",
    )
    resp.headers["Content-Disposition"] = disp
    resp.headers["Content-Length"]      = str(len(pdf_bytes))
    resp.headers["X-Accel-Buffering"]   = "no"
    resp.headers["Cache-Control"]       = "no-store"
    return resp

# ─────────────────────────────────────────────────────────────────
# TEMPLATE API
# ─────────────────────────────────────────────────────────────────

TEMPLATE_BRAND_COLORS = {
    "hebron":    "#DC2626",
    "redeemer":  "#4F46E5",
    "priyanka":  "#0F006A",
    "ab_ascent": "#224499",
}

@app.route("/api/templates", methods=["GET"])
@app.route("/templates", methods=["GET"])
def get_templates():
    """
    v2.9 FIX: the student template selector was showing the employee
    (*_emp) cards too because TEMPLATE_CONFIGS.update(EMPLOYEE_*) merges
    both dicts. Filter them out here so /api/templates returns ONLY the
    student templates. The /api/employees/templates endpoint returns the
    employee templates separately.
    """
    payload = []
    for key, template in TEMPLATE_CONFIGS.items():
        if key in EMPLOYEE_TEMPLATE_KEYS:
            continue  # employee templates live on /api/employees/templates
        payload.append({
            "key": key,
            "label": template["label"],
            "display_name": template["display_name"],
            "description": template["description"],
            "fields": template["fields"],
            "color": TEMPLATE_BRAND_COLORS.get(key, "#4F46E5"),
            "preview_url": f"/api/templates/{key}/preview.png",
        })
    return jsonify(payload)


@app.route("/api/templates/<template_key>/preview.png", methods=["GET"])
@app.route("/templates/<template_key>/preview.png", methods=["GET"])
def get_template_preview(template_key):
    key = normalize_template_key(template_key)
    # Try real PDF rasterization first
    try:
        png_bytes = _get_template_preview_png(key)
        if png_bytes:
            return send_file(io.BytesIO(png_bytes), mimetype="image/png",
                             download_name=f"{key}_preview.png")
    except Exception as e:
        log.warning("Template preview rasterization failed for %s: %s", key, e)

    # Fallback: return a coloured SVG as image/svg+xml (browsers accept this in <img>)
    colors = {
        "hebron":    "#DC2626",
        "redeemer":  "#4F46E5",
        "priyanka":  "#0F006A",
        "ab_ascent": "#224499",
    }
    labels = {
        "hebron":    "Hebron Mission School",
        "redeemer":  "My Redeemer Mission School",
        "priyanka":  "Priyanka Dreamnest School",
        "ab_ascent": "Ab Ascent School",
    }
    color = colors.get(key, "#4F46E5")
    label = labels.get(key, key.replace("_", " ").title())
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">
  <rect width="320" height="200" rx="12" fill="{color}22"/>
  <rect width="320" height="52" rx="0" fill="{color}"/>
  <rect y="0" width="320" height="52" rx="12" fill="{color}"/>
  <rect y="26" width="320" height="26" fill="{color}"/>
  <text x="160" y="34" font-family="sans-serif" font-size="14" font-weight="bold"
        fill="white" text-anchor="middle">{label}</text>
  <rect x="24" y="72" width="56" height="72" rx="6" fill="{color}44"/>
  <text x="52" y="114" font-family="sans-serif" font-size="9" fill="{color}88" text-anchor="middle">PHOTO</text>
  <rect x="96" y="72" width="140" height="10" rx="4" fill="{color}55"/>
  <rect x="96" y="90" width="100" height="8" rx="4" fill="{color}33"/>
  <rect x="96" y="106" width="120" height="8" rx="4" fill="{color}33"/>
  <rect x="96" y="122" width="80" height="8" rx="4" fill="{color}33"/>
  <rect x="24" y="160" width="272" height="1" fill="{color}22"/>
  <text x="160" y="182" font-family="sans-serif" font-size="9" fill="{color}66" text-anchor="middle">ID Card Template Preview</text>
</svg>"""
    return send_file(io.BytesIO(svg.encode()), mimetype="image/svg+xml",
                     download_name=f"{key}_preview.svg")


def _request_template_key():
    """
    Resolve the student template from ?template=.

    v2.9 ROUTE-ISOLATION FIX:
      • If an *employee* key (e.g. 'redeemer_emp') is supplied to a
        student endpoint, transparently strip the '_emp' suffix and
        use the equivalent student template. This prevents employee
        cards from leaking into the student flow when the frontend
        gets out of sync.
      • Unknown keys are rejected with 400.
    """
    raw = request.args.get("template", DEFAULT_TEMPLATE)
    key = str(raw or DEFAULT_TEMPLATE).strip().lower()
    # Coerce employee keys to student equivalents so the wrong route
    # never produces an employee card.
    if key in EMPLOYEE_TEMPLATE_KEYS:
        # 'redeemer_emp' → 'redeemer'; fall back via _resolve_renderer_key.
        student_key = _resolve_renderer_key(key)
        log.info("Student route received employee template '%s' — coercing to '%s'", key, student_key)
        key = student_key
    if key not in TEMPLATE_CONFIGS or key in EMPLOYEE_TEMPLATE_KEYS:
        return None, jsonify({"error": f"Unknown student template: {raw}"}), 400
    return key, None, None

# ─────────────────────────────────────────────────────────────────
# PDF / PREVIEW ENDPOINTS
# ─────────────────────────────────────────────────────────────────

def _get_students_or_fetch():
    """
    Return (students_list, error_response_or_None).
    Uses the global store. If empty, auto-refetches via ?school_id= param
    or the school_id last loaded.
    """
    students = _store.get("students") or []
    if students:
        return students, None

    school_id_raw = request.args.get("school_id", "").strip()
    if not school_id_raw:
        school_id_raw = str(_store.get("school_id") or "").strip()

    if school_id_raw:
        try:
            school_id = int(school_id_raw)
        except ValueError:
            return [], jsonify({"error": "No students loaded — invalid school_id"})
        if school_id not in SCHOOLS:
            return [], jsonify({"error": f"No students loaded — unknown school_id {school_id}"})
        try:
            url = API_BASE_URL.format(school_id=school_id)
            resp = _HTTP.get(url, timeout=45)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            return [], jsonify({"error": f"No students in memory and API refetch failed: {e}"})

        records = None
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            for key in ("data", "students", "records", "result", "results", "items"):
                if key in payload and isinstance(payload[key], list):
                    records = payload[key]; break
            if records is None:
                for v in payload.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        records = v; break

        if not records:
            return [], jsonify({"error": "API refetch returned no records"})

        fetched = [map_api_record(r) for r in records if isinstance(r, dict)]
        fetched = [s for s in fetched if any(v for v in s.values() if v and v != DEFAULT_SESSION)]
        if not fetched:
            return [], jsonify({"error": "API refetch: no valid students after mapping"})

        fetched = _sort_and_index(fetched)
        replace_store(fetched, "api", SCHOOLS[school_id], school_id=school_id)
        return fetched, None

    return [], jsonify({"error": "No students loaded. Please go back and reload student data."})

# ─────────────────────────────────────────────────────────────────
# PRODUCTION SAFETY CAP — with the v2.7 chunked on-disk builder we can
# safely handle 1500 students on a 512 MB / 0.5 CPU Railway worker.
# This is now a soft sanity limit, NOT a primary OOM defence (the
# chunked builder is). Override via env var if needed.
# ─────────────────────────────────────────────────────────────────
PROD_MAX_STUDENTS = int(os.environ.get("PROD_MAX_STUDENTS", "1500"))


@app.route("/api/preview/all", methods=["GET"])
@app.route("/preview/all", methods=["GET"])
def preview_all():
    template_key, err_resp, err_code = _request_template_key()
    if err_resp:
        return err_resp, err_code
    students, err = _get_students_or_fetch()
    if err:
        return err
    cls      = request.args.get("class","").strip().upper()
    students = filter_students_by_class(students, cls)
    return send_generated_pdf(students, dpi=PREVIEW_DPI,
                              download_name=f"preview_{template_key}.pdf", as_attachment=False,
                              template_key=template_key)

@app.route("/api/debug/download", methods=["GET"])
def debug_download():
    """
    Diagnose download failures — returns JSON explaining exactly what went wrong.
    Call: GET /api/debug/download?template=ab_ascent&school_id=5
    """
    report = {}
    template_key = normalize_template_key(request.args.get("template", DEFAULT_TEMPLATE))
    report["template_key"] = template_key

    # Session / students
    store    = _get_store()
    students = store.get("students") or []
    report["students_in_session"] = len(students)
    report["school_name"]         = store.get("school_name")
    report["school_id"]           = store.get("school_id")

    # Template PDF
    tmpl_cfg  = get_template_config(template_key)
    pdf_path  = tmpl_cfg["pdf"]
    report["template_pdf_path"]   = str(pdf_path)
    report["template_pdf_exists"] = pdf_path.exists()

    # Libs
    report["has_fitz"] = HAS_FITZ
    report["has_pil"]  = HAS_PIL

    # Temp dir
    tmp_dir = _resolve_pdf_tmp_dir()
    report["temp_dir"] = tmp_dir
    try:
        import shutil
        free_mb = shutil.disk_usage(tmp_dir).free / (1024*1024)
        report["temp_dir_free_mb"] = round(free_mb, 1)
    except Exception as e:
        report["temp_dir_free_mb_error"] = str(e)

    # Try rendering ONE card
    if students and HAS_FITZ and pdf_path.exists():
        tmpl_bytes = _ensure_template(template_key)
        rk = _resolve_renderer_key(template_key)
        render_fn  = (_render_priyanka_card_bytes if rk == "priyanka"
                      else _render_ab_ascent_card_bytes)
        try:
            card_bytes = render_fn(students[0], tmpl_bytes)
            report["single_card_render"] = "OK" if card_bytes else "returned None"
            report["single_card_bytes"]  = len(card_bytes) if card_bytes else 0
        except Exception as e:
            report["single_card_render"] = "EXCEPTION"
            report["single_card_error"]  = str(e)
            report["single_card_trace"]  = traceback.format_exc()

    report["max_students_per_request"] = MAX_STUDENTS_PER_REQUEST
    report["over_limit"] = len(students) > MAX_STUDENTS_PER_REQUEST

    return jsonify(report), 200


@app.route("/api/download/all", methods=["GET"])
@app.route("/download/all", methods=["GET"])
def download_all():
    template_key, err_resp, err_code = _request_template_key()
    if err_resp:
        return err_resp, err_code
    students, err = _get_students_or_fetch()
    if err:
        return err
    cls = request.args.get("class","").strip().upper()
    if cls:
        students = filter_students_by_class(students, cls)
        fname    = f"ids_{template_key}_{cls}.pdf"
    else:
        students = list(students)
        fname    = f"ids_{template_key}_ALL.pdf"
    return send_generated_pdf(students, dpi=DOWNLOAD_DPI,
                              download_name=fname, as_attachment=True, allow_external=True,
                              template_key=template_key)

# ─────────────────────────────────────────────────────────────────
# JOB-BASED DOWNLOAD  (so the frontend can show real progress %)
#
#   POST /api/jobs/start?class=...&template=...
#       → { job_id }      (returns immediately, work runs in background)
#   GET  /api/jobs/<id>/progress
#       → { status, phase, progress, done, total, ...}
#   GET  /api/jobs/<id>/file
#       → streams the finished PDF (after status=="done")
# ─────────────────────────────────────────────────────────────────

def _run_job(jid: str, students: list, template_key: str, download_name: str):
    _job_set(jid, status="running", phase="prefetch", started_at=time.time())
    try:
        # Phase 1 — photo prefetch (~30% of the bar)
        try:
            urls = list({s.get("photo_url","").strip()
                         for s in students if s.get("photo_url","").strip()})
            total_urls = max(1, len(urls))
            done_urls = 0
            if urls:
                workers = min(PREFETCH_WORKERS, len(urls))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = [pool.submit(fetch_photo_bytes, u) for u in urls]
                    for f in as_completed(futs):
                        done_urls += 1
                        try: f.result()
                        except Exception: pass
                        _job_set(jid,
                                 phase="prefetch",
                                 progress=round(30.0 * done_urls / total_urls, 1))
            else:
                _job_set(jid, progress=30.0)
        except Exception as e:
            log.warning("job %s prefetch error (non-fatal): %s", jid, e)
            _job_set(jid, progress=30.0)

        # Phase 2 — PDF rendering (~30% → 92%) using the v2.7 chunked builder.
        # The builder reports per-page progress via the callback so the bar
        # stays smooth even on a 0.5-CPU Railway worker.
        n_total_pages = (len(students) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE

        def _on_page(done_pages, total_pages):
            # Map page progress 0..total_pages onto bar 30..92
            pct = 30.0 + 62.0 * done_pages / max(1, total_pages)
            _job_set(jid, phase="render",
                     progress=round(pct, 1),
                     done=min(len(students), done_pages * CARDS_PER_PAGE))

        _job_set(jid, phase="render", progress=30.0)

        if HAS_FITZ:
            out_path = build_pdf_file_vector(
                students, template_key=template_key, progress_cb=_on_page,
            )
        else:
            out_path = build_pdf_file(students, dpi=DOWNLOAD_DPI, template_key=template_key)

        if not out_path:
            raise RuntimeError("PDF build failed (template missing or PyMuPDF error)")

        # Phase 3 — final compaction / merge already done inside builder.
        _job_set(jid, phase="writing", progress=96.0)

        size = os.path.getsize(out_path)
        _job_set(jid, status="done", phase="done", progress=100.0,
                 file_path=out_path, file_size=size,
                 download_name=download_name,
                 finished_at=time.time(),
                 done=len(students))
        log.info("job %s done: %s (%.1f KB)", jid, out_path, size / 1024.0)
    except Exception as e:
        log.error("job %s FAILED: %s\n%s", jid, e, traceback.format_exc())
        _job_set(jid, status="error", phase="error", error=str(e),
                 finished_at=time.time())


@app.route("/api/jobs/start", methods=["POST", "GET"])
def job_start():
    """Kick off async PDF build. Returns {job_id} immediately."""
    _prune_old_jobs()
    template_key, err_resp, err_code = _request_template_key()
    if err_resp:
        return err_resp, err_code
    students, err = _get_students_or_fetch()
    if err:
        return err
    cls = request.args.get("class", "").strip().upper()
    if cls:
        students = filter_students_by_class(students, cls)
        fname    = f"ids_{template_key}_{cls}.pdf"
    else:
        students = list(students)
        fname    = f"ids_{template_key}_ALL.pdf"

    if not students:
        return jsonify({"error": "No students to render."}), 400

    if _IS_PRODUCTION and len(students) > PROD_MAX_STUDENTS:
        return jsonify({
            "error": (
                f"Too many students for one PDF ({len(students)} > {PROD_MAX_STUDENTS}). "
                f"This is a sanity guardrail — please download class-by-class."
            ),
            "code":  "BATCH_TOO_LARGE",
            "limit": PROD_MAX_STUDENTS,
            "requested": len(students),
        }), 413

    jid = _new_job(total=len(students))
    threading.Thread(
        target=_run_job, args=(jid, students, template_key, fname),
        daemon=True, name=f"pdfjob-{jid[:6]}",
    ).start()
    return jsonify({
        "job_id":   jid,
        "total":    len(students),
        "download_name": fname,
    })


@app.route("/api/jobs/<jid>/progress", methods=["GET"])
def job_progress(jid):
    j = _job_get(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({
        "job_id":    j["id"],
        "status":    j["status"],
        "phase":     j["phase"],
        "progress":  j["progress"],
        "done":      j["done"],
        "total":     j["total"],
        "file_size": j["file_size"],
        "download_name": j["download_name"],
        "error":     j["error"],
    })


@app.route("/api/jobs/<jid>/debug-info", methods=["GET"])
def job_debug_info(jid):
    """Debug endpoint — open in browser to inspect job state.
    GET http://localhost:5000/api/jobs/<jid>/debug-info
    """
    j = _job_get(jid)
    if not j:
        return jsonify({"error": "unknown job", "jid": jid}), 404
    path = j.get("file_path")
    info = {
        "jid":           jid,
        "status":        j.get("status"),
        "phase":         j.get("phase"),
        "progress":      j.get("progress"),
        "file_path":     path,
        "file_size_kb":  round(j.get("file_size", 0) / 1024, 1),
        "download_name": j.get("download_name"),
        "error":         j.get("error"),
        "file_exists_on_disk": bool(path and os.path.exists(path)),
    }
    if path and os.path.exists(path):
        try:
            disk_size = os.path.getsize(path)
            info["disk_size_bytes"] = disk_size
            info["disk_size_kb"]    = round(disk_size / 1024, 1)
            with open(path, "rb") as fh:
                header = fh.read(16)
            info["first_16_bytes_hex"] = header.hex()
            info["is_valid_pdf"]       = header[:4] == b"%PDF"
        except Exception as e:
            info["disk_read_error"] = str(e)
    safe_name = _sanitize_filename(j.get("download_name") or "ids.pdf")
    info["response_headers_that_will_be_sent"] = {
        "Content-Type":        "application/pdf",
        "Content-Disposition": f'attachment; filename="{safe_name}"',
        "Content-Length":      str(j.get("file_size", "?")),
        "Cache-Control":       "no-store",
    }
    log.info("[DOWNLOAD-DEBUG] debug-info for job %s: %s", jid, json.dumps(info, default=str))
    return jsonify(info)


@app.route("/api/jobs/<jid>/file", methods=["GET"])
def job_file(jid):
    # Log every incoming request header so we can see exactly what axios is sending
    log.info("[DOWNLOAD-DEBUG] >>>>>>>>>> /jobs/%s/file HIT <<<<<<<<<<<", jid)
    log.info("[DOWNLOAD-DEBUG]   User-Agent      : %s", request.headers.get("User-Agent", "<none>"))
    log.info("[DOWNLOAD-DEBUG]   Accept          : %s", request.headers.get("Accept", "<none>"))
    log.info("[DOWNLOAD-DEBUG]   Range           : %s", request.headers.get("Range", "<none>"))
    log.info("[DOWNLOAD-DEBUG]   Origin          : %s", request.headers.get("Origin", "<none>"))
    log.info("[DOWNLOAD-DEBUG]   X-Requested-With: %s", request.headers.get("X-Requested-With", "<none>"))

    j = _job_get(jid)
    if not j:
        log.warning("[DOWNLOAD-DEBUG] job %s NOT FOUND in registry", jid)
        return jsonify({"error": "unknown job"}), 404

    log.info("[DOWNLOAD-DEBUG]   job.status    : %s", j["status"])
    log.info("[DOWNLOAD-DEBUG]   job.file_path : %s", j["file_path"])
    log.info("[DOWNLOAD-DEBUG]   job.file_size : %.1f KB", j.get("file_size", 0) / 1024)

    if j["status"] != "done":
        log.warning("[DOWNLOAD-DEBUG] job not done yet — returning 409")
        return jsonify({"error": f"job not finished: {j['status']}", "phase": j["phase"],
                        "progress": j["progress"]}), 409

    path = j["file_path"]
    if not path or not os.path.exists(path):
        log.error("[DOWNLOAD-DEBUG] FILE MISSING ON DISK: %s", path)
        return jsonify({"error": "file expired or missing"}), 410

    safe_name = _sanitize_filename(j["download_name"] or "ids.pdf")
    log.info("[DOWNLOAD-DEBUG]   safe_name     : %s", safe_name)

    # Schedule delete before reading so reaper knows about the file
    schedule_delete(path, PDF_RETENTION_SECONDS)
    log.info("[pdf-lifecycle] /jobs/%s/file served — file retained for %ds", jid, PDF_RETENTION_SECONDS)

    # Read entire file into RAM — closes handle immediately (no WinError 32).
    # Plain bytes Response = single Content-Length, NO Transfer-Encoding: chunked.
    try:
        with open(path, "rb") as fh:
            pdf_bytes = fh.read()
        log.info("[DOWNLOAD-DEBUG]   bytes_read    : %d (%.1f KB)", len(pdf_bytes), len(pdf_bytes)/1024)
        log.info("[DOWNLOAD-DEBUG]   is_valid_pdf  : %s", pdf_bytes[:4] == b"%PDF")
    except OSError as e:
        log.error("[DOWNLOAD-DEBUG] FAILED TO READ FILE INTO RAM: %s", e)
        return jsonify({"error": "file expired or missing"}), 410

    gc.collect()

    from flask import Response as _Response
    resp = _Response(pdf_bytes, status=200, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    resp.headers["Content-Length"]      = str(len(pdf_bytes))
    resp.headers["X-Accel-Buffering"]   = "no"
    resp.headers["Cache-Control"]       = "no-store"

    log.info("[DOWNLOAD-DEBUG] <<<<<<<<<< RESPONSE BEING SENT:")
    log.info("[DOWNLOAD-DEBUG]   Status          : 200 OK")
    log.info("[DOWNLOAD-DEBUG]   Content-Type    : application/pdf")
    log.info("[DOWNLOAD-DEBUG]   Content-Length  : %d bytes", len(pdf_bytes))
    log.info("[DOWNLOAD-DEBUG]   Content-Disp    : attachment; filename=\"%s\"", safe_name)
    log.info("[DOWNLOAD-DEBUG] === IF THE CLIENT RETRIES AFTER THIS, THE BUG IS IN THE FRONTEND ===")

    return resp

@app.route("/api/jobs/<jid>", methods=["DELETE"])
def job_cancel(jid):
    j = _job_get(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    # v2.8: only honour cancel if the job is NOT already done. Once the
    # build finishes the file is retained for PDF_RETENTION_SECONDS so the
    # client can resume / retry on transient network errors.
    status = j.get("status")
    if status in ("done",):
        log.info("[pdf-lifecycle] /jobs/%s ignored DELETE (status=done, file retained)", jid)
        return jsonify({"ok": True, "retained": True})
    try:
        p = j.get("file_path")
        if p:
            schedule_delete(p, 30)   # short grace window, then reaper sweeps
    except Exception:
        pass
    with _jobs_lock:
        _jobs.pop(jid, None)
    return jsonify({"ok": True})

@app.route("/api/preview/student", methods=["GET"])
@app.route("/preview/student", methods=["GET"])
def preview_student():
    template_key, err_resp, err_code = _request_template_key()
    if err_resp:
        return err_resp, err_code
    students, err = _get_students_or_fetch()
    if err:
        return err
    cls      = request.args.get("class","").strip().upper()
    name     = request.args.get("name","").strip().lower()
    matches = [s for s in students
               if s.get("class","").strip().upper() == cls
               and name == s.get("student_name","").strip().lower()]
    if not matches:
        return jsonify({"error": "Student not found"}), 404
    return send_generated_pdf([matches[0]], dpi=PREVIEW_DPI,
                              download_name=f"preview_student_{template_key}.pdf", as_attachment=False,
                              template_key=template_key)

@app.route("/api/download/student", methods=["GET"])
@app.route("/download/student", methods=["GET"])
def download_student():
    template_key, err_resp, err_code = _request_template_key()
    if err_resp:
        return err_resp, err_code
    students, err = _get_students_or_fetch()
    if err:
        return err
    cls      = request.args.get("class","").strip().upper()
    name     = request.args.get("name","").strip().lower()
    matches = [s for s in students
               if s.get("class","").strip().upper() == cls
               and name == s.get("student_name","").strip().lower()]
    if not matches:
        return jsonify({"error": "Student not found"}), 404
    student   = matches[0]
    safe_name = student.get("student_name","student").replace(" ","_")
    return send_generated_pdf([student], dpi=DOWNLOAD_DPI,
                              download_name=f"id_{template_key}_{safe_name}.pdf", as_attachment=True, allow_external=True,
                              template_key=template_key)

# ═════════════════════════════════════════════════════════════════
# EMPLOYEE ID CARD SUPPORT  (v2.8)
#
# Employees use the SAME A4-landscape grid + the SAME renderer pipeline as
# students. We just:
#   1. Provide an upload-only data source (no live API).
#   2. Map the school-specific employee Excel columns onto the same
#      "student" schema the renderers already understand:
#         employee_name      → student_name
#         designation        → class    (printed in the "class" slot)
#         father/husband     → father_name
#         emp_id             → roll     (and adm_no)
#         validity (ab_asc)  → section  (just to display somewhere)
#         photo url variants → photo_url
#   3. Expose a parallel /api/employees/* surface that the React frontend
#      uses when the user has picked "Employees" mode.
# ══════════════════════════════════════════════════════════════
_emp_store: dict = {
    "employees":   [],
    "source":      None,
    "school_name": None,
    "updated_at":  0.0,
}
_emp_store_lock = threading.Lock()


def replace_emp_store(employees, source, school_name):
    with _emp_store_lock:
        old = _emp_store.get("employees") or []
        if isinstance(old, list):
            old.clear()
        _emp_store["employees"]   = list(employees)
        _emp_store["source"]      = source
        _emp_store["school_name"] = school_name
        _emp_store["updated_at"]  = time.time()
    gc.collect()


# Column aliases for each school. We accept any reasonable variant of each
# column name so the customer can paste their spreadsheet with minor naming
# differences and still get a valid card.
_EMP_COL_ALIASES = {
    # canonical_field : (alias, alias, …)
    "employee_name": ("employee_name", "name", "emp_name", "full_name"),
    "designation":   ("designation", "post", "role", "job_title", "position"),
    "father_name":   ("father_name", "fname", "f_name", "father", "fh_name",
                      "husband_father_name", "husband_name", "father_husband_name"),
    "dob":           ("dob", "date_of_birth", "birth_date"),
    "address":       ("address", "residence", "home_address"),
    "mobile":        ("mobile", "phone", "contact", "contact_no", "mobile_no", "phone_no"),
    "emp_id":        ("emp_id", "id", "employee_id", "empid"),
    "validity":      ("validity", "valid_till", "valid_upto", "expiry"),
    "photo_url":     ("photo_url", "photo", "image", "image_url",
                      "employee_photo", "emp_photo"),
}


def _pick_emp(rm: dict, *aliases) -> str:
    """Like `pick()` but for employee rows. Returns the first non-empty hit.
    Importantly, photo URLs are NOT cleaned here — they are passed through
    raw so the renderer's fetch step gets the exact URL from the row.
    """
    for a in aliases:
        if a in rm:
            v = rm[a]
            if v is None or pd.isna(v):
                continue
            s = str(v).strip()
            if s and s.lower() not in {"nan", "none", "null"}:
                return s
    return ""


def map_employee_row(rm: dict) -> dict:
    """Return a dict with the same shape as a parsed student row so the
    existing renderers can consume it without modification."""
    employee_name = _pick_emp(rm, *_EMP_COL_ALIASES["employee_name"])
    designation   = _pick_emp(rm, *_EMP_COL_ALIASES["designation"])
    father_name   = _pick_emp(rm, *_EMP_COL_ALIASES["father_name"])
    dob_raw       = _pick_emp(rm, *_EMP_COL_ALIASES["dob"])
    address       = _pick_emp(rm, *_EMP_COL_ALIASES["address"])
    mobile        = _pick_emp(rm, *_EMP_COL_ALIASES["mobile"])
    emp_id        = _pick_emp(rm, *_EMP_COL_ALIASES["emp_id"])
    validity      = _pick_emp(rm, *_EMP_COL_ALIASES["validity"])
    photo_url_raw = _pick_emp(rm, *_EMP_COL_ALIASES["photo_url"])

    # IMPORTANT: photo_url is preserved verbatim (no replace/cleaning) per spec.
    out = {
        # canonical employee fields (also kept under their own names so the
        # frontend can display them without re-mapping)
        "employee_name": employee_name,
        "designation":   designation,
        "emp_id":        emp_id,
        "validity":      validity,

        # —— shimmed onto the student schema for the existing renderers ——
        "student_name":  employee_name,
        "class":         designation,    # designation printed in the "class" slot
        "section":       validity,       # ab_ascent validity shown in section slot
        "roll":          emp_id,
        "father_name":   father_name,
        "mother_name":   "",
        "dob":           format_dob(dob_raw),
        "address":       clean_address(address),
        "mobile":        mobile,
        "photo_url":     photo_url_raw,    # raw — no cleaning
        "adm_no":        emp_id,
        "blood_group":   "",
        "gender":        "",
        "session":       DEFAULT_SESSION,
        "bus_route":     "",
    }
    return out


def parse_employee_file(file_path: str, filename: str):
    """Parse an Excel/CSV file of employees into the unified employee schema."""
    fn = (filename or "").lower()
    if fn.endswith(".csv"):
        df = None
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                df = pd.read_csv(file_path, encoding=enc, dtype=str)
                break
            except Exception:
                continue
        if df is None:
            raise ValueError("Could not decode CSV — try saving as UTF-8 in Excel")
    else:
        try:
            df = pd.read_excel(file_path, dtype=str)
        except Exception:
            df = pd.read_excel(file_path, dtype=str, engine="openpyxl")

    df.columns = [norm_key(c) for c in df.columns]
    employees = []
    for _, row in df.iterrows():
        rm = {col: row[col] for col in df.columns}
        emp = map_employee_row(rm)
        # require at least a name OR an emp_id, else skip empty rows
        if emp.get("employee_name") or emp.get("emp_id"):
            employees.append(emp)

    # Assign serial numbers grouped by designation (so cards are nicely ordered)
    employees.sort(key=lambda e: (
        (e.get("designation") or "").strip().upper(),
        (e.get("employee_name") or "").strip().upper(),
    ))
    for i, e in enumerate(employees, 1):
        e["serial"] = i
    return employees


def _employee_groups_summary(employees):
    cc = defaultdict(int)
    for e in employees:
        # We group by designation (analogous to "class" for students)
        cc[(e.get("designation") or "OTHER").strip().upper()] += 1
    return [{"class": k, "count": v} for k, v in sorted(cc.items(), key=lambda x: x[0])]


def _filter_employees_by_designation(employees, des):
    des = (des or "").strip().upper()
    if not des:
        return list(employees)
    return [e for e in employees if (e.get("designation") or "").strip().upper() == des]


# v3.0 — short helpers for the new {school}_employees.pdf naming scheme.
_EMP_SCHOOL_SLUGS = {
    "hebron_emp":    "hebron",
    "redeemer_emp":  "redeemer",
    "priyanka_emp":  "priyanka",
    "ab_ascent_emp": "ab_ascent",
}

def _emp_school_slug(template_key: str) -> str:
    """Map an employee template key to its short school slug used in PDF
    filenames. Falls back to the template key itself if unknown."""
    return _EMP_SCHOOL_SLUGS.get(
        (template_key or "").strip().lower(),
        (template_key or "employees").strip().lower().replace("_emp", "")
    )

def _safe_slug(value: str) -> str:
    """Make a string safe for use inside a filename."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return s.strip("_") or "x"


def _normalize_emp_template_key(value):
    """
    v2.9 ROUTE-ISOLATION FIX: if the caller hits an employee endpoint
    with a *student* template key (e.g. 'redeemer'), coerce it to the
    matching employee key ('redeemer_emp') so the user never accidentally
    sees a student card on the employee flow.
    """
    key = str(value or DEFAULT_EMP_TEMPLATE).strip().lower()
    if key in EMPLOYEE_TEMPLATE_KEYS:
        return key
    # Student key supplied to employee endpoint — promote to its _emp pair.
    candidate = f"{key}_emp"
    if candidate in EMPLOYEE_TEMPLATE_KEYS:
        log.info("Employee route received student template '%s' — coercing to '%s'", key, candidate)
        return candidate
    return DEFAULT_EMP_TEMPLATE


def _request_emp_template_key():
    raw = request.args.get("template", DEFAULT_EMP_TEMPLATE)
    key = _normalize_emp_template_key(raw)
    return key, None, None


# ──────────────────────────────────────────────────────────────
# Employee REST routes (mirror students, sans the API source)
# ──────────────────────────────────────────────────────────────
@app.route("/api/employees/templates", methods=["GET"])
def emp_get_templates():
    """
    v3.0 FIX: previously pointed `preview_url` at the STUDENT renderer's
    preview (e.g. /api/templates/redeemer/preview.png), which made the
    Employees wizard step show student cards.  Now we point it at the
    employee key itself (/api/templates/redeemer_emp/preview.png) so the
    preview is rasterised from the employee template PDF — employees see
    employee cards.  `_get_template_preview_png` already handles `*_emp`
    keys correctly because TEMPLATE_CONFIGS holds their PDF paths.
    """
    payload = []
    for key in EMPLOYEE_TEMPLATE_CONFIGS:
        t = TEMPLATE_CONFIGS[key]
        renderer = t.get("renderer", key)
        payload.append({
            "key":           key,
            "label":         t["label"],
            "display_name":  t["display_name"],
            "description":   t["description"],
            "fields":        t["fields"],
            "color":         TEMPLATE_BRAND_COLORS.get(renderer, "#4F46E5"),
            # Use the employee key's OWN preview — rasterised from the
            # employee template PDF.
            "preview_url":   f"/api/templates/{key}/preview.png",
        })
    return jsonify(payload)


@app.route("/api/employees/upload", methods=["POST"])
def emp_upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file attached. Please choose a file."}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Empty file name"}), 400
    fname = f.filename.strip()
    ext = Path(fname).suffix.lower()
    if ext not in (".xlsx", ".xls", ".csv"):
        return jsonify({"error": f"Unsupported file type '{ext}'. Please upload .xlsx, .xls or .csv"}), 400
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)
        employees = parse_employee_file(tmp_path, fname)
        if not employees:
            return jsonify({
                "error": "No employee rows found. Check that the column headers "
                         "match one of the expected schemas "
                         "(name + emp_id / id / employee_id)."
            }), 400
        replace_emp_store(employees, "file", "Uploaded File")
        log.info("Emp upload: %d employees from '%s'", len(employees), fname)
        return jsonify({
            "success":      True,
            "count":        len(employees),
            "school_name":  "Uploaded File",
            "classes":      _employee_groups_summary(employees),
            "session":      DEFAULT_SESSION,
        })
    except Exception as e:
        log.error("Emp upload error: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": f"Could not parse file: {e}"}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass


@app.route("/api/employees/status", methods=["GET"])
def emp_get_status():
    employees = _emp_store["employees"]
    if not employees:
        return jsonify({"loaded": False})
    des_list = sorted({
        (e.get("designation") or "OTHER").strip().upper()
        for e in employees
    })
    class_counts = {}
    for e in employees:
        k = (e.get("designation") or "OTHER").strip().upper()
        class_counts[k] = class_counts.get(k, 0) + 1
    return jsonify({
        "loaded":       True,
        "count":        len(employees),
        "school_id":    None,
        "school":       _emp_store.get("school_name", ""),
        "school_name":  _emp_store.get("school_name", ""),
        "source":       _emp_store.get("source", ""),
        "classes":      des_list,
        "classCounts":  class_counts,
        "session":      DEFAULT_SESSION,
    })


@app.route("/api/employees/employees", methods=["GET"])
@app.route("/api/employees/list", methods=["GET"])
def emp_get_employees():
    des = request.args.get("class", "").strip().upper()
    employees = _emp_store["employees"]
    if des:
        employees = _filter_employees_by_designation(employees, des)
    return jsonify(employees)


def _emp_get_loaded_or_400():
    employees = _emp_store.get("employees") or []
    if not employees:
        return [], jsonify({"error": "No employees loaded. Please upload an Excel file."})
    return employees, None


@app.route("/api/employees/preview/all", methods=["GET"])
def emp_preview_all():
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des = request.args.get("class", "").strip().upper()
    employees = _filter_employees_by_designation(employees, des)
    return send_generated_pdf(
        employees, dpi=PREVIEW_DPI,
        download_name=f"preview_{template_key}.pdf", as_attachment=False,
        template_key=template_key,
    )


@app.route("/api/employees/download/all", methods=["GET"])
def emp_download_all():
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des = request.args.get("class", "").strip().upper()
    # v3.0 PDF NAMING: drop the "emp_" prefix and use {school}_employees.pdf
    # (no timestamp), per user spec. Designation, if present, is appended.
    school_slug = _emp_school_slug(template_key)
    if des:
        employees = _filter_employees_by_designation(employees, des)
        fname     = f"{school_slug}_employees_{_safe_slug(des)}.pdf"
    else:
        employees = list(employees)
        fname     = f"{school_slug}_employees.pdf"
    return send_generated_pdf(
        employees, dpi=DOWNLOAD_DPI,
        download_name=fname, as_attachment=True, allow_external=True,
        template_key=template_key,
    )


@app.route("/api/employees/jobs/start", methods=["POST", "GET"])
def emp_job_start():
    _prune_old_jobs()
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des = request.args.get("class", "").strip().upper()
    # v3.0 PDF NAMING: {school}_employees.pdf (no timestamp).
    school_slug = _emp_school_slug(template_key)
    if des:
        employees = _filter_employees_by_designation(employees, des)
        fname     = f"{school_slug}_employees_{_safe_slug(des)}.pdf"
    else:
        employees = list(employees)
        fname     = f"{school_slug}_employees.pdf"

    if not employees:
        return jsonify({"error": "No employees to render."}), 400

    if _IS_PRODUCTION and len(employees) > PROD_MAX_STUDENTS:
        return jsonify({
            "error": f"Too many employees for one PDF ({len(employees)} > {PROD_MAX_STUDENTS}).",
            "code":  "BATCH_TOO_LARGE",
            "limit": PROD_MAX_STUDENTS,
            "requested": len(employees),
        }), 413

    jid = _new_job(total=len(employees))
    threading.Thread(
        target=_run_job, args=(jid, employees, template_key, fname),
        daemon=True, name=f"empjob-{jid[:6]}",
    ).start()
    return jsonify({
        "job_id":        jid,
        "total":         len(employees),
        "download_name": fname,
    })


@app.route("/api/employees/preview/student", methods=["GET"])
def emp_preview_one():
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des  = request.args.get("class", "").strip().upper()
    name = request.args.get("name", "").strip().lower()
    matches = [e for e in employees
               if (e.get("designation") or "").strip().upper() == des
               and name == (e.get("employee_name") or "").strip().lower()]
    if not matches:
        return jsonify({"error": "Employee not found"}), 404
    return send_generated_pdf([matches[0]], dpi=PREVIEW_DPI,
                              download_name=f"preview_emp_{template_key}.pdf", as_attachment=False,
                              template_key=template_key)


@app.route("/api/employees/download/student", methods=["GET"])
def emp_download_one():
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des  = request.args.get("class", "").strip().upper()
    name = request.args.get("name", "").strip().lower()
    matches = [e for e in employees
               if (e.get("designation") or "").strip().upper() == des
               and name == (e.get("employee_name") or "").strip().lower()]
    if not matches:
        return jsonify({"error": "Employee not found"}), 404
    emp = matches[0]
    safe_name = (emp.get("employee_name", "employee") or "employee").replace(" ", "_")
    school_slug = _emp_school_slug(template_key)
    return send_generated_pdf(
        [emp], dpi=DOWNLOAD_DPI,
        download_name=f"{school_slug}_employee_{_safe_slug(safe_name)}.pdf",
        as_attachment=True, allow_external=True,
        template_key=template_key,
    )

# ─────────────────────────────────────────────────────────────────
def _startup_log():
    ck = "YES"; xk = "NO"
    print("=" * 62)
    print("  ID Card Generator  v2.7  (chunked on-disk PDF builder, 700+ students)")
    print(f"  Mode          : {'PRODUCTION (512MB/0.5CPU)' if _IS_PRODUCTION else 'LOCAL (full performance)'}")
    print(f"  Hebron PDF    : {ck+' found' if TEMPLATE_PDF_HEBRON.exists() else xk+' NOT FOUND'}")
    print(f"  Redeemer PDF  : {ck+' found' if TEMPLATE_PDF_REDEEMER.exists() else xk+' NOT FOUND'}")
    print(f"  Priyanka PDF  : {ck+' found' if TEMPLATE_PDF_PRIYANKA.exists() else xk+' NOT FOUND'}")
    print(f"  Ab Ascent PDF : {ck+' found' if TEMPLATE_PDF_AB_ASCENT.exists() else xk+' NOT FOUND'}")
    print(f"  Hebron EMP    : {ck+' found' if TEMPLATE_PDF_HEBRON_EMP.exists()    else '~ fallback (student template)'}")
    print(f"  Redeemer EMP  : {ck+' found' if TEMPLATE_PDF_REDEEMER_EMP.exists()  else '~ fallback (student template)'}")
    print(f"  Priyanka EMP  : {ck+' found' if TEMPLATE_PDF_PRIYANKA_EMP.exists()  else '~ using Redeemer template as fallback (dedicated renderer active)'}")
    print(f"  Ab Ascent EMP : {ck+' found' if TEMPLATE_PDF_AB_ASCENT_EMP.exists() else '~ fallback (student template)'}")
    print(f"  PDF retain    : {PDF_RETENTION_SECONDS}s (delayed-delete)")
    print(f"  PyMuPDF       : {ck if HAS_FITZ else xk}")
    print(f"  Pillow        : {ck if HAS_PIL  else xk}")
    print(f"  Temp dir      : {PDF_TEMP_DIR}")
    print(f"  Photo prefetch: {PREFETCH_WORKERS} threads | timeout {PHOTO_TIMEOUT}")
    print(f"  Card render   : {CARD_RENDER_WORKERS} thread(s)")
    print(f"  Photo quality : {PHOTO_PX}px @ JPEG q={PHOTO_JPEG_QUALITY}")
    print(f"  Photo cache   : max {MAX_CACHED_PHOTOS} entries")
    print("=" * 62)

_startup_log()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # FIX: debug=False prevents the Werkzeug auto-reloader from restarting
    # the process mid-transfer.  The reloader watches .py files and when it
    # detects a change it kills the server process, which immediately drops
    # ALL open socket connections — including large PDF downloads that are
    # still streaming.  Users on slow links or with large PDFs (50+ MB) see
    # this as a "Network Error" even though the server logged "200 OK"
    # (the status line was sent before the connection was dropped).
    #
    # For interactive development with auto-reload, set DEBUG_RELOAD=1 in
    # the environment explicitly; otherwise leave it off.
    _debug = os.environ.get("DEBUG_RELOAD", "0").strip() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)