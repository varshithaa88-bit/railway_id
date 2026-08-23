import os
import re
import gc
import time
import logging
import tempfile
import traceback
import threading
import zipfile
import io
from pathlib import Path
from collections import defaultdict

from flask import Blueprint, request, jsonify, send_file, Response
import pandas as pd
import requests

from src.config import (
    DEFAULT_SESSION, DEFAULT_EMP_TEMPLATE, EMPLOYEE_TEMPLATE_CONFIGS, TEMPLATE_CONFIGS,
    EMPLOYEE_TEMPLATE_KEYS, TEMPLATE_BRAND_COLORS, PREVIEW_DPI, DOWNLOAD_DPI,
    EMPLOYEE_API_URLS, SCHOOLS, API_BASE_URL
)
from src.utils.text import clean_card_value
from src.utils.photo import prefetch_photos, fetch_photo_bytes, prepare_photo_for_rect_cover, insert_image_safe
from src.utils.pdf import build_id_card_size_pdf, build_backside_id_card_size_pdf, run_job, run_zip_job, _pdf_to_png_bytes
from src.jobs import new_job, prune_old_jobs, job_get, schedule_delete
from src.routes.students import _post_clean_student, format_dob, clean_address, norm_key, send_generated_pdf

log = logging.getLogger("idcard.routes.employees")
employees_bp = Blueprint("employees", __name__)

# Global in-memory store for employees
_emp_store = {
    "employees":   [],
    "source":      "",
    "school_name": "",
    "updated_at":  0.0,
}
_emp_store_lock = threading.Lock()

def get_emp_store():
    return _emp_store

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


_EMP_COL_ALIASES = {
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

# API field mapping for employee API response
_EMP_API_FIELD_MAPPING = {
    "name": "employee_name",
    "designation": "designation",
    "husband_name": "father_name",  # API uses husband_name for father_name
    "dob": "dob",
    "address": "address",
    "contact": "mobile",  # API uses contact for mobile
    "id": "emp_id",  # API uses id for emp_id
    "employee_photo": "photo_url",
}


def _pick_emp(rm: dict, *aliases) -> str:
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
    employee_name = _pick_emp(rm, *_EMP_COL_ALIASES["employee_name"])
    designation   = _pick_emp(rm, *_EMP_COL_ALIASES["designation"])
    father_name   = _pick_emp(rm, *_EMP_COL_ALIASES["father_name"])
    dob_raw       = _pick_emp(rm, *_EMP_COL_ALIASES["dob"])
    address       = _pick_emp(rm, *_EMP_COL_ALIASES["address"])
    mobile        = _pick_emp(rm, *_EMP_COL_ALIASES["mobile"])
    emp_id        = _pick_emp(rm, *_EMP_COL_ALIASES["emp_id"])
    validity      = _pick_emp(rm, *_EMP_COL_ALIASES["validity"])
    photo_url_raw = _pick_emp(rm, *_EMP_COL_ALIASES["photo_url"])

    out = {
        "employee_name": employee_name,
        "designation":   designation,
        "emp_id":        emp_id,
        "validity":      validity,

        # shims
        "student_name":  employee_name,
        "class":         designation,
        "section":       validity,
        "roll":          emp_id,
        "father_name":   father_name,
        "mother_name":   "",
        "dob":           format_dob(dob_raw),
        "address":       clean_address(address),
        "mobile":        mobile,
        "photo_url":     photo_url_raw,
        "adm_no":        emp_id,
        "blood_group":   "",
        "gender":        "",
        "session":       DEFAULT_SESSION,
        "bus_route":     "",
    }
    return out


def parse_employee_file(file_path: str, filename: str):
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
    employees = []
    for _, row in df.iterrows():
        rm = {col: row[col] for col in df.columns}
        emp = map_employee_row(rm)
        if emp.get("employee_name") or emp.get("emp_id"):
            employees.append(emp)

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
        cc[(e.get("designation") or "OTHER").strip().upper()] += 1
    return [{"class": k, "count": v} for k, v in sorted(cc.items(), key=lambda x: x[0])]


def _filter_employees_by_designation(employees, des):
    des = (des or "").strip().upper()
    if not des:
        return list(employees)
    return [e for e in employees if (e.get("designation") or "").strip().upper() == des]


_EMP_SCHOOL_SLUGS = {
    "hebron_emp":    "hebron",
    "redeemer_emp":  "redeemer",
    "priyanka_emp":  "priyanka",
    "ab_ascent_emp": "ab_ascent",
}

def _emp_school_slug(template_key: str) -> str:
    return _EMP_SCHOOL_SLUGS.get(
        (template_key or "").strip().lower(),
        (template_key or "employees").strip().lower().replace("_emp", "")
    )

def _safe_slug(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return s.strip("_") or "x"


def _normalize_emp_template_key(value):
    key = str(value or DEFAULT_EMP_TEMPLATE).strip().lower()
    if key in EMPLOYEE_TEMPLATE_KEYS:
        return key
    candidate = f"{key}_emp"
    if candidate in EMPLOYEE_TEMPLATE_KEYS:
        log.info("Employee route received student template '%s' — coercing to '%s'", key, candidate)
        return candidate
    return DEFAULT_EMP_TEMPLATE


def _request_emp_template_key():
    raw = request.args.get("template", DEFAULT_EMP_TEMPLATE)
    key = _normalize_emp_template_key(raw)
    return key, None, None


def _emp_get_loaded_or_400():
    employees = _emp_store.get("employees") or []
    if not employees:
        return [], jsonify({"error": "No employees loaded. Please upload an Excel file."})
    return employees, None


def map_api_employee_row(api_data: dict) -> dict:
    """Map API employee data to internal format."""
    # Map API fields to internal field names
    mapped = {}
    for api_field, internal_field in _EMP_API_FIELD_MAPPING.items():
        if api_field in api_data:
            mapped[internal_field] = api_data[api_field]
    
    # Use existing map_employee_row for final processing
    return map_employee_row(mapped)


@employees_bp.route("/api/employees/fetch-school/<int:school_id>", methods=["GET", "OPTIONS"])
def fetch_employees_from_api(school_id: int):
    """Fetch employees from API for a specific school."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    
    # Check if school has employee API
    if school_id not in EMPLOYEE_API_URLS:
        school_name = SCHOOLS.get(school_id, f"School {school_id}")
        return jsonify({
            "error": f"Employee API not available for {school_name}. Please upload employee data via Excel/CSV file.",
            "school_id": school_id,
            "school_name": school_name,
            "api_available": False
        }), 400
    
    api_url = EMPLOYEE_API_URLS[school_id]
    school_name = SCHOOLS.get(school_id, f"School {school_id}")
    
    log.info("[fetch-employees] school_id=%d URL=%s", school_id, api_url)
    
    try:
        response = requests.get(api_url, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") != "success":
            return jsonify({"error": "API returned unsuccessful status"}), 400
        
        employees_data = data.get("data", [])
        if not employees_data:
            return jsonify({"error": "No employee data found in API response"}), 400
        
        # Map API data to internal format
        employees = []
        for emp_data in employees_data:
            mapped_emp = map_api_employee_row(emp_data)
            if mapped_emp.get("employee_name") or mapped_emp.get("emp_id"):
                employees.append(mapped_emp)
        
        # Sort employees
        employees.sort(key=lambda e: (
            (e.get("designation") or "").strip().upper(),
            (e.get("employee_name") or "").strip().upper(),
        ))
        
        # Add serial numbers
        for i, e in enumerate(employees, 1):
            e["serial"] = i
        
        # Update store
        replace_emp_store(employees, "api", school_name)
        
        log.info("[fetch-employees] Loaded %d employees from API for %s", len(employees), school_name)
        
        return jsonify({
            "success": True,
            "count": len(employees),
            "school_id": school_id,
            "school_name": school_name,
            "source": "api",
            "classes": _employee_groups_summary(employees),
            "session": DEFAULT_SESSION,
        })
        
    except requests.exceptions.Timeout:
        return jsonify({"error": "API request timed out"}), 504
    except requests.exceptions.RequestException as e:
        log.error("[fetch-employees] API request failed: %s", e)
        return jsonify({"error": f"API request failed: {str(e)}"}), 500
    except Exception as e:
        log.error("[fetch-employees] Unexpected error: %s", e)
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@employees_bp.route("/api/employees/templates", methods=["GET"])
def emp_get_templates():
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
            "preview_url":   f"/api/templates/{key}/preview.png",
        })
    return jsonify(payload)


@employees_bp.route("/api/employees/upload", methods=["POST"])
def emp_upload_file():
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
        employees = parse_employee_file(tmp_path, fname)
        if not employees:
            return jsonify({
                "error": "No employee rows found."
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


@employees_bp.route("/api/employees/status", methods=["GET"])
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


@employees_bp.route("/api/employees/employees", methods=["GET"])
@employees_bp.route("/api/employees/list", methods=["GET"])
def emp_get_employees():
    des = request.args.get("class", "").strip().upper()
    employees = _emp_store["employees"]
    if des:
        employees = _filter_employees_by_designation(employees, des)
    return jsonify(employees)


@employees_bp.route("/api/employees/preview/all", methods=["GET"])
def emp_preview_all():
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des = request.args.get("class", "").strip().upper()
    employees = _filter_employees_by_designation(employees, des)
    side = request.args.get("side", "front").strip().lower()
    if side not in ("front", "back"):
        side = "front"
    return send_generated_pdf(
        employees, dpi=PREVIEW_DPI,
        download_name=f"preview_{template_key}_{side}.pdf", as_attachment=False,
        template_key=template_key, side=side,
    )


@employees_bp.route("/api/employees/preview/backside/all", methods=["GET"])
def emp_preview_backside_all():
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
        download_name=f"preview_{template_key}_back.pdf", as_attachment=False,
        template_key=template_key, side="back",
    )


@employees_bp.route("/api/employees/download/all", methods=["GET"])
def emp_download_all():
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des = request.args.get("class", "").strip().upper()
    school_slug = _emp_school_slug(template_key)
    side = request.args.get("side", "front").strip().lower()
    if side not in ("front", "back"):
        side = "front"
    if des:
        employees = _filter_employees_by_designation(employees, des)
        fname     = f"{school_slug}_employees_{_safe_slug(des)}_{side}.pdf"
    else:
        employees = list(employees)
        fname     = f"{school_slug}_employees_{side}.pdf"
    return send_generated_pdf(
        employees, dpi=DOWNLOAD_DPI,
        download_name=fname, as_attachment=True, allow_external=True,
        template_key=template_key, side=side,
    )


@employees_bp.route("/api/employees/download/backside/all", methods=["GET"])
def emp_download_backside_all():
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des = request.args.get("class", "").strip().upper()
    school_slug = _emp_school_slug(template_key)
    if des:
        employees = _filter_employees_by_designation(employees, des)
        fname     = f"{school_slug}_employees_{_safe_slug(des)}_back.pdf"
    else:
        employees = list(employees)
        fname     = f"{school_slug}_employees_back.pdf"
    return send_generated_pdf(
        employees, dpi=DOWNLOAD_DPI,
        download_name=fname, as_attachment=True, allow_external=True,
        template_key=template_key, side="back",
    )


@employees_bp.route("/api/employees/jobs/start", methods=["POST", "GET"])
def emp_job_start():
    prune_old_jobs()
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err
    des = request.args.get("class", "").strip().upper()
    school_slug = _emp_school_slug(template_key)
    side = request.args.get("side", "front").strip().lower()
    if side not in ("front", "back"):
        side = "front"
    if des:
        employees = _filter_employees_by_designation(employees, des)
        fname     = f"{school_slug}_employees_{_safe_slug(des)}_{side}.pdf"
    else:
        employees = list(employees)
        fname     = f"{school_slug}_employees_{side}.pdf"

    if not employees:
        return jsonify({"error": "No employees to render."}), 400

    PROD_MAX_STUDENTS = int(os.environ.get("PROD_MAX_STUDENTS", "1500"))
    if len(employees) > PROD_MAX_STUDENTS:
        return jsonify({
            "error": f"Too many employees for one PDF ({len(employees)} > {PROD_MAX_STUDENTS}).",
            "code":  "BATCH_TOO_LARGE",
            "limit": PROD_MAX_STUDENTS,
            "requested": len(employees),
        }), 413

    jid = new_job(total=len(employees))
    threading.Thread(
        target=run_job, args=(jid, employees, template_key, fname),
        kwargs={"side": side},
        daemon=True, name=f"empjob-{jid[:6]}",
    ).start()
    return jsonify({
        "job_id":        jid,
        "total":         len(employees),
        "download_name": fname,
    })


@employees_bp.route("/api/employees/preview/student", methods=["GET"])
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
    side = request.args.get("side", "front").strip().lower()
    if side not in ("front", "back"):
        side = "front"
    return send_generated_pdf([matches[0]], dpi=PREVIEW_DPI,
                               download_name=f"preview_emp_{template_key}_{side}.pdf", as_attachment=False,
                               template_key=template_key, side=side)


@employees_bp.route("/api/employees/preview/backside/student", methods=["GET"])
def emp_preview_backside_one():
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
                               download_name=f"preview_emp_{template_key}_back.pdf", as_attachment=False,
                               template_key=template_key, side="back")


@employees_bp.route("/api/employees/download/student", methods=["GET"])
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
    side = request.args.get("side", "front").strip().lower()
    if side not in ("front", "back"):
        side = "front"
    
    # Check if PNG format requested
    output_format = request.args.get("format", "pdf").strip().lower()
    if output_format in ("png", "jpeg", "jpg"):
        if side == "front":
            pdf_path = build_id_card_size_pdf(emp, template_key=template_key, skip_flatten=True)
        else:
            pdf_path = build_backside_id_card_size_pdf(emp, template_key=template_key, skip_flatten=True)
        if not pdf_path:
            return jsonify({"error": "PDF generation failed"}), 500
        try:
            png_bytes = _pdf_to_png_bytes(pdf_path, dpi=600)
            Path(pdf_path).unlink(missing_ok=True)
            if not png_bytes:
                return jsonify({"error": "PNG conversion failed"}), 500
            safe_name_png = f"{safe_name}_{side}.png"
            resp = Response(png_bytes, status=200, mimetype="image/png")
            resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name_png}"'
            resp.headers["Content-Length"] = str(len(png_bytes))
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except Exception as e:
            log.error("PNG download error: %s", e)
            return jsonify({"error": f"PNG generation failed: {e}"}), 500
    
    # For individual card downloads, skip PDF downgrade to avoid corruption
    if side == "front":
        pdf_path = build_id_card_size_pdf(emp, template_key=template_key, skip_flatten=True)
    else:
        pdf_path = build_backside_id_card_size_pdf(emp, template_key=template_key, skip_flatten=True)
    
    if not pdf_path:
        return jsonify({"error": "PDF generation failed"}), 500
    
    try:
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
    except OSError as e:
        log.error("Could not read individual card PDF file: %s", e)
        try:
            Path(pdf_path).unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"error": "PDF file missing after generation"}), 500
    
    # Clean up the temp file immediately after reading
    try:
        Path(pdf_path).unlink(missing_ok=True)
    except Exception:
        pass
    
    safe_name = _sanitize_filename(f"{school_slug}_employee_{_safe_slug(safe_name)}_{side}.pdf")
    disp = f'attachment; filename="{safe_name}"'
    resp = Response(
        pdf_bytes,
        status=200,
        mimetype="application/pdf",
    )
    resp.headers["Content-Disposition"] = disp
    resp.headers["Content-Length"] = str(len(pdf_bytes))
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@employees_bp.route("/api/employees/download/backside/student", methods=["GET"])
def emp_download_backside_one():
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
    
    # Check if PNG format requested
    output_format = request.args.get("format", "pdf").strip().lower()
    if output_format in ("png", "jpeg", "jpg"):
        pdf_path = build_backside_id_card_size_pdf(emp, template_key=template_key, skip_flatten=True)
        if not pdf_path:
            return jsonify({"error": "PDF generation failed"}), 500
        try:
            png_bytes = _pdf_to_png_bytes(pdf_path, dpi=600)
            Path(pdf_path).unlink(missing_ok=True)
            if not png_bytes:
                return jsonify({"error": "PNG conversion failed"}), 500
            safe_name_png = f"{safe_name}_back.png"
            resp = Response(png_bytes, status=200, mimetype="image/png")
            resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name_png}"'
            resp.headers["Content-Length"] = str(len(png_bytes))
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except Exception as e:
            log.error("PNG download error: %s", e)
            return jsonify({"error": f"PNG generation failed: {e}"}), 500
    
    # For individual card downloads, skip PDF downgrade to avoid corruption
    pdf_path = build_backside_id_card_size_pdf(emp, template_key=template_key, skip_flatten=True)
    
    if not pdf_path:
        return jsonify({"error": "PDF generation failed"}), 500
    
    try:
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
    except OSError as e:
        log.error("Could not read individual card PDF file: %s", e)
        try:
            Path(pdf_path).unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"error": "PDF file missing after generation"}), 500
    
    # Clean up the temp file immediately after reading
    try:
        Path(pdf_path).unlink(missing_ok=True)
    except Exception:
        pass
    
    safe_name = _sanitize_filename(f"{school_slug}_employee_{_safe_slug(safe_name)}_back.pdf")
    disp = f'attachment; filename="{safe_name}"'
    resp = Response(
        pdf_bytes,
        status=200,
        mimetype="application/pdf",
    )
    resp.headers["Content-Disposition"] = disp
    resp.headers["Content-Length"] = str(len(pdf_bytes))
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@employees_bp.route("/api/employees/download/zip", methods=["GET"])
def emp_download_zip():
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err

    des_filter = request.args.get("class", "").strip().upper()
    if des_filter:
        employees = _filter_employees_by_designation(employees, des_filter)
    else:
        employees = list(employees)

    if not employees:
        return jsonify({"error": "No employees to export"}), 400

    log.info("Employee ZIP download: %d employees, template=%s", len(employees), template_key)

    school_slug = _emp_school_slug(template_key)
    zip_buf = io.BytesIO()
    used_names = {}

    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for idx, emp in enumerate(employees):
            raw_name  = (emp.get("employee_name") or f"employee_{idx+1}").strip()
            des_label = (emp.get("designation") or "unknown").strip().upper()
            safe      = re.sub(r"[^\w\-]", "_", raw_name)
            base_name = f"{des_label}_{safe}.pdf"

            if base_name in used_names:
                used_names[base_name] += 1
                base_name = f"{des_label}_{safe}_{used_names[base_name]}.pdf"
            else:
                used_names[base_name] = 1

            try:
                pdf_path = build_id_card_size_pdf(emp, template_key=template_key)
                if pdf_path and Path(pdf_path).exists():
                    zf.write(pdf_path, arcname=base_name)
                    try:
                        Path(pdf_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    log.warning("Employee ZIP %s: build_id_card_size_pdf returned no path", raw_name)
            except Exception as exc:
                log.error("Employee ZIP %s: %s", raw_name, exc)

    zip_buf.seek(0)
    suffix = f"_{des_filter}" if des_filter else ""
    fname  = f"{school_slug}_employees{suffix}_individual.zip"
    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=fname,
    )


@employees_bp.route("/api/employees/jobs/start-zip", methods=["POST", "GET"])
def emp_zip_job_start():
    prune_old_jobs()
    template_key, err_resp, err_code = _request_emp_template_key()
    if err_resp:
        return err_resp, err_code
    employees, err = _emp_get_loaded_or_400()
    if err:
        return err

    output_format = request.args.get("format", "pdf").strip().lower()
    if output_format not in ("pdf", "jpeg", "jpg"):
        output_format = "pdf"
    if output_format == "jpg":
        output_format = "jpeg"

    side = request.args.get("side", "front").strip().lower()
    if side not in ("front", "back"):
        side = "front"

    des = request.args.get("class", "").strip().upper()
    school_slug = _emp_school_slug(template_key)
    fmt_suffix  = "_png" if output_format == "jpeg" else ""
    side_suffix = f"_{side}" if side == "back" else ""
    if des:
        employees = _filter_employees_by_designation(employees, des)
        fname     = f"{school_slug}_employees_{_safe_slug(des)}{side_suffix}_individual{fmt_suffix}.zip"
    else:
        employees = list(employees)
        fname     = f"{school_slug}_employees{side_suffix}_individual{fmt_suffix}.zip"

    if not employees:
        return jsonify({"error": "No employees to export."}), 400

    jid = new_job(total=len(employees))
    threading.Thread(
        target=run_zip_job,
        args=(jid, employees, template_key, fname, "employee_name", "designation"),
        kwargs={"output_format": output_format, "side": side},
        daemon=True, name=f"empzipjob-{jid[:6]}",
    ).start()
    return jsonify({"job_id": jid, "total": len(employees), "download_name": fname,
                    "output_format": output_format, "side": side})
