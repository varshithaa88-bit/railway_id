import os
import re
import gc
import time
import logging
import tempfile
import traceback
import threading
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from flask import Blueprint, request, jsonify, Response, send_file
import pandas as pd
import requests

from src.config import (
    DEFAULT_SESSION, SCHOOLS, API_BASE_URL, MAX_STUDENTS_PER_REQUEST,
    PREVIEW_DPI, DOWNLOAD_DPI, PREVIEW_EXTERNAL_THRESHOLD,
    PDF_RETENTION_SECONDS, TEMPLATE_CONFIGS, EMPLOYEE_TEMPLATE_KEYS
)
from src.utils.text import class_sort_key
from src.utils.photo import prefetch_photos
from src.utils.pdf import (
    build_pdf_file, _resolve_pdf_tmp_dir, _sanitize_filename,
    _external_storage_enabled, upload_pdf_to_external_storage,
    build_id_card_size_pdf, run_job, run_zip_job
)
from src.jobs import schedule_delete, new_job, prune_old_jobs


log = logging.getLogger("idcard.routes.students")
students_bp = Blueprint("students", __name__)

# Global in-memory store for students
_store = {
    "students":    [],
    "source":      "",
    "school_name": "",
    "school_id":   None,
    "updated_at":  0.0,
}
_store_lock = threading.Lock()

def get_store():
    return _store

def replace_store(records, source, school_name, school_id=None):
    with _store_lock:
        _store["students"]    = list(records)
        _store["source"]      = source
        _store["school_name"] = school_name
        _store["school_id"]   = school_id
        _store["updated_at"]  = time.time()

def filter_students_by_class(students, cls):
    cls = str(cls or "").strip().upper()
    if not cls:
        return list(students)
    return [s for s in students if s.get("class","").strip().upper() == cls]

# Text cleaning / parsing helpers
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENT_RE = re.compile(r"&(?:nbsp|amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);")

def has_html(text) -> bool:
    if not text:
        return False
    if _HTML_TAG_RE.search(text):
        return True
    if _HTML_ENT_RE.search(text):
        return True
    return False

def clean_address(text) -> str:
    if text is None:
        return ""
    s = str(text).strip()
    if not s or s.lower() in {"nan", "none", "null", "nil"}:
        return ""
    if has_html(s):
        return ""
    return s

def format_dob(text) -> str:
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    low = s.lower()
    if low in {"nan", "none", "null", "nil", "0000-00-00", "00-00-0000",
               "0000/00/00", "00/00/0000"}:
        return ""

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

    try:
        dt = pd.to_datetime(s, errors="raise", dayfirst=True)
        if pd.notna(dt):
            return f"{dt.day:02d}-{dt.month:02d}-{dt.year:04d}"
    except Exception:
        pass

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

    return ""

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
            raise ValueError("Could not decode CSV")
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
    "student_name":"student_name", "name":"student_name",
    "class":"class", "class_name":"class", "std":"class", "grade":"class", "standard":"class",
    "section":"section", "section_id":"section", "sec":"section",
    "roll":"roll", "roll_no":"roll", "roll_number":"roll", "rollno":"roll",
    "father_name":"father_name", "father":"father_name", "fathers_name":"father_name",
    "mother_name":"mother_name", "mother":"mother_name", "mothers_name":"mother_name",
    "dob":"dob", "date_of_birth":"dob", "birth_date":"dob",
    "address":"address", "student_address":"address", "residence":"address",
    "mobile":"mobile", "phone":"mobile", "mobile_no":"mobile",
    "contact":"mobile", "father_contact":"mobile",
    "photo_url":"photo_url", "photo":"photo_url", "student_photo":"photo_url",
    "image_url":"photo_url", "photo_link":"photo_url",
    "adm_no":"adm_no", "admission_no":"adm_no", "admission_number":"adm_no",
    "adm":"adm_no", "admno":"adm_no", "reg_no":"adm_no", "registration_no":"adm_no",
    "blood_group":"blood_group", "bloodgroup":"blood_group", "blood":"blood_group",
    "session":"session", "academic_year":"session",
    "gender":"gender", "sex":"gender",
    "bus_route":"bus_route", "bus":"bus_route", "bus_no":"bus_route",
    "bus_number":"bus_route", "route":"bus_route",
}
_MAP_DEBUG_LOGGED = False

def map_api_record(record):
    global _MAP_DEBUG_LOGGED
    out = {
        "student_name":"","class":"","section":"","roll":"","father_name":"",
        "mother_name":"","dob":"","address":"","mobile":"","photo_url":"",
        "adm_no":"","blood_group":"","gender":"","session":DEFAULT_SESSION,
        "bus_route":"",
    }
    unmapped_keys = []
    for k, v in record.items():
        internal = _API_MAP.get(k.strip().lower())
        if internal and v not in (None,"","null","NULL"):
            val = str(v).strip()
            out[internal] = val
        elif not internal and v not in (None,"","null","NULL"):
            unmapped_keys.append(k.strip().lower())

    result = _post_clean_student(out)

    if not _MAP_DEBUG_LOGGED:
        _MAP_DEBUG_LOGGED = True
        log.info("[api-map] RAW first record keys : %s", list(record.keys()))
        log.info("[api-map] RAW first record values: %s", {k: str(v)[:60] for k,v in record.items()})
        log.info("[api-map] MAPPED result          : %s", {k: str(v)[:60] for k,v in result.items()})
        if unmapped_keys:
            log.warning("[api-map] UNMAPPED keys: %s", unmapped_keys)

    return result

def _classes_summary(students):
    cc = defaultdict(int)
    for s in students:
        cc[s.get("class","").strip().upper()] += 1
    return [{"class": k, "count": v} for k, v in sorted(cc.items(), key=lambda x: class_sort_key(x[0]))]


@students_bp.route("/api/schools", methods=["GET"])
@students_bp.route("/schools", methods=["GET"])
def get_schools():
    return jsonify([{"id": k, "name": v} for k, v in SCHOOLS.items()])


@students_bp.route("/api/upload", methods=["POST"])
@students_bp.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file attached. Please choose a file."}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Empty file name"}), 400
    fname = f.filename.strip()
    ext = Path(fname).suffix.lower()
    if ext not in (".xlsx", ".xls", ".csv"):
        return jsonify({"error": f"Unsupported file type '{ext}'."}), 400
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)
        students = parse_file(tmp_path, fname)
        if not students:
            return jsonify({"error": "No student rows found in the file."}), 400
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


@students_bp.route("/api/fetch-school/<int:school_id>", methods=["GET"])
@students_bp.route("/fetch-school/<int:school_id>", methods=["GET"])
def fetch_school(school_id):
    if school_id not in SCHOOLS:
        return jsonify({"error": "Unknown school"}), 400
    url = API_BASE_URL.format(school_id=school_id)
    try:
        # Use simple request
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return jsonify({"error": f"API error: {e}"}), 500

    global _MAP_DEBUG_LOGGED
    _MAP_DEBUG_LOGGED = False
    log.info("[fetch-school] school_id=%s URL=%s", school_id, url)

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
    before = len(students)
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


@students_bp.route("/api/students", methods=["GET"])
@students_bp.route("/students", methods=["GET"])
def get_students():
    cls      = request.args.get("class","").strip().upper()
    students = _store["students"]
    if cls:
        students = [s for s in students if s.get("class","").strip().upper() == cls]
    return jsonify(students)


@students_bp.route("/api/status", methods=["GET"])
@students_bp.route("/status", methods=["GET"])
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


def _request_template_key():
    from src.renderers.base import _resolve_renderer_key
    raw = request.args.get("template", "hebron")
    key = str(raw or "hebron").strip().lower()
    if key in EMPLOYEE_TEMPLATE_KEYS:
        student_key = _resolve_renderer_key(key)
        log.info("Student route received employee template '%s' — coercing to '%s'", key, student_key)
        key = student_key
    if key not in TEMPLATE_CONFIGS or key in EMPLOYEE_TEMPLATE_KEYS:
        return None, jsonify({"error": f"Unknown student template: {raw}"}), 400
    return key, None, None


def _get_students_or_fetch():
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
            resp = requests.get(url, timeout=45)
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


def send_generated_pdf(students, dpi, download_name, as_attachment, allow_external=False, template_key: str = "hebron"):
    if not students:
        return jsonify({"error": "No students loaded"}), 400
    if len(students) > MAX_STUDENTS_PER_REQUEST:
        return jsonify({
            "error": (
                f"Too many students in one request ({len(students)}). "
                f"Please filter by class or increase MAX_STUDENTS_PER_REQUEST."
            )
        }), 413

    # Prod max limit checking
    PROD_MAX_STUDENTS = int(os.environ.get("PROD_MAX_STUDENTS", "1500"))
    if as_attachment and len(students) > PROD_MAX_STUDENTS:
        return jsonify({
            "error": (
                f"Too many students for one PDF ({len(students)} > {PROD_MAX_STUDENTS}). "
                f"Please download class-by-class."
            ),
            "code":  "BATCH_TOO_LARGE",
            "limit": PROD_MAX_STUDENTS,
            "requested": len(students),
        }), 413

    if (not as_attachment) and len(students) >= PREVIEW_EXTERNAL_THRESHOLD and _external_storage_enabled():
        allow_external = True

    _kind = "employees" if str(template_key).endswith("_emp") else "students"
    log.info("PDF generation started: %d %s | template=%s | dpi=%d",
             len(students), _kind, template_key, dpi)

    try:
        prefetch_photos(students)
    except Exception as e:
        log.warning("prefetch_photos error: %s", e)

    try:
        pdf_path = build_pdf_file(students, dpi=dpi, template_key=template_key)
    except Exception as e:
        log.error("build_pdf_file EXCEPTION: %s", e)
        return jsonify({"error": f"PDF generation exception: {e}"}), 500

    if not pdf_path:
        return jsonify({"error": "PDF generation failed"}), 500

    if allow_external and _external_storage_enabled():
        try:
            remote_url = upload_pdf_to_external_storage(pdf_path, download_name)
            if remote_url:
                return jsonify({
                    "success": True,
                    "download_url": remote_url,
                    "download_name": download_name,
                })
        except Exception as e:
            log.warning("External storage upload failed: %s", e)

    safe_name = _sanitize_filename(download_name)
    schedule_delete(pdf_path, PDF_RETENTION_SECONDS)

    try:
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
    except OSError as e:
        log.error("Could not read PDF file: %s", e)
        return jsonify({"error": "PDF file missing after generation"}), 500

    gc.collect()

    disp = ("attachment" if as_attachment else "inline") + f'; filename="{safe_name}"'
    resp = Response(
        pdf_bytes,
        status=200,
        mimetype="application/pdf",
    )
    resp.headers["Content-Disposition"] = disp
    resp.headers["Content-Length"]      = str(len(pdf_bytes))
    resp.headers["X-Accel-Buffering"]   = "no"
    resp.headers["Cache-Control"]       = "no-store"
    return resp


@students_bp.route("/api/preview/all", methods=["GET"])
@students_bp.route("/preview/all", methods=["GET"])
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


@students_bp.route("/api/download/all", methods=["GET"])
@students_bp.route("/download/all", methods=["GET"])
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


@students_bp.route("/api/preview/student", methods=["GET"])
@students_bp.route("/preview/student", methods=["GET"])
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


@students_bp.route("/api/download/student", methods=["GET"])
@students_bp.route("/download/student", methods=["GET"])
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


@students_bp.route("/api/download/zip", methods=["GET"])
@students_bp.route("/download/zip", methods=["GET"])
def download_students_zip():
    template_key, err_resp, err_code = _request_template_key()
    if err_resp:
        return err_resp, err_code
    students, err = _get_students_or_fetch()
    if err:
        return err

    cls_filter = request.args.get("class", "").strip().upper()
    if cls_filter:
        students = [s for s in students if s.get("class", "").strip().upper() == cls_filter]

    if not students:
        return jsonify({"error": "No students to export"}), 400

    log.info("ZIP download: %d students, template=%s", len(students), template_key)

    zip_buf = io.BytesIO()
    used_names = {}

    import zipfile
    import re
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for idx, student in enumerate(students):
            raw_name  = (student.get("student_name") or f"student_{idx+1}").strip()
            cls_label = (student.get("class") or "unknown").strip().upper()
            safe      = re.sub(r"[^\w\-]", "_", raw_name)
            base_name = f"{cls_label}_{safe}.pdf"

            if base_name in used_names:
                used_names[base_name] += 1
                base_name = f"{cls_label}_{safe}_{used_names[base_name]}.pdf"
            else:
                used_names[base_name] = 1

            try:
                pdf_path = build_id_card_size_pdf(student, template_key=template_key)
                if pdf_path and Path(pdf_path).exists():
                    zf.write(pdf_path, arcname=base_name)
                    try:
                        Path(pdf_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    log.warning("ZIP student %s: build_id_card_size_pdf returned no path", raw_name)
            except Exception as exc:
                log.error("ZIP student %s: %s", raw_name, exc)

    zip_buf.seek(0)
    fname = f"student_id_cards_{template_key}" + (f"_{cls_filter}" if cls_filter else "") + ".zip"
    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=fname,
    )


@students_bp.route("/api/jobs/start", methods=["POST", "GET"])
def job_start():
    prune_old_jobs()
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

    PROD_MAX_STUDENTS = int(os.environ.get("PROD_MAX_STUDENTS", "1500"))
    if len(students) > PROD_MAX_STUDENTS:
        return jsonify({
            "error": (
                f"Too many students for one PDF ({len(students)} > {PROD_MAX_STUDENTS}). "
                f"This is a sanity guardrail — please download class-by-class."
            ),
            "code":  "BATCH_TOO_LARGE",
            "limit": PROD_MAX_STUDENTS,
            "requested": len(students),
        }), 413

    jid = new_job(total=len(students))
    threading.Thread(
        target=run_job, args=(jid, students, template_key, fname),
        daemon=True, name=f"pdfjob-{jid[:6]}",
    ).start()
    return jsonify({
        "job_id":   jid,
        "total":    len(students),
        "download_name": fname,
    })


@students_bp.route("/api/jobs/start-zip", methods=["POST", "GET"])
def zip_job_start():
    prune_old_jobs()
    template_key, err_resp, err_code = _request_template_key()
    if err_resp:
        return err_resp, err_code
    students, err = _get_students_or_fetch()
    if err:
        return err

    output_format = request.args.get("format", "pdf").strip().lower()
    if output_format not in ("pdf", "jpeg", "jpg"):
        output_format = "pdf"
    if output_format == "jpg":
        output_format = "jpeg"

    cls = request.args.get("class", "").strip().upper()
    fmt_suffix = "_png" if output_format == "jpeg" else ""
    if cls:
        students = filter_students_by_class(students, cls)
        fname    = f"student_id_cards_{template_key}_{cls}{fmt_suffix}.zip"
    else:
        students = list(students)
        fname    = f"student_id_cards_{template_key}{fmt_suffix}.zip"

    if not students:
        return jsonify({"error": "No students to export."}), 400

    jid = new_job(total=len(students))
    threading.Thread(
        target=run_zip_job,
        args=(jid, students, template_key, fname, "student_name", "class"),
        kwargs={"output_format": output_format},
        daemon=True, name=f"zipjob-{jid[:6]}",
    ).start()
    return jsonify({"job_id": jid, "total": len(students), "download_name": fname,
                    "output_format": output_format})


