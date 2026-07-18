import os
import gc
import json
import logging
from flask import Blueprint, request, jsonify, Response
from src.config import PDF_RETENTION_SECONDS
from src.jobs import job_get, new_job, prune_old_jobs, schedule_delete
from src.utils.pdf import _sanitize_filename

log = logging.getLogger("idcard.routes.jobs")
jobs_bp = Blueprint("jobs", __name__)


@jobs_bp.route("/api/jobs/<jid>/progress", methods=["GET"])
def job_progress(jid):
    j = job_get(jid)
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


@jobs_bp.route("/api/jobs/<jid>/debug-info", methods=["GET"])
def job_debug_info(jid):
    j = job_get(jid)
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


@jobs_bp.route("/api/jobs/<jid>/file", methods=["GET"])
def job_file(jid):
    log.info("[DOWNLOAD-DEBUG] >>>>>>>>>> /jobs/%s/file HIT <<<<<<<<<<<", jid)
    log.info("[DOWNLOAD-DEBUG]   User-Agent      : %s", request.headers.get("User-Agent", "<none>"))
    log.info("[DOWNLOAD-DEBUG]   Accept          : %s", request.headers.get("Accept", "<none>"))

    j = job_get(jid)
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

    schedule_delete(path, PDF_RETENTION_SECONDS)
    log.info("[pdf-lifecycle] /jobs/%s/file served — file retained for %ds", jid, PDF_RETENTION_SECONDS)

    try:
        with open(path, "rb") as fh:
            pdf_bytes = fh.read()
        log.info("[DOWNLOAD-DEBUG]   bytes_read    : %d (%.1f KB)", len(pdf_bytes), len(pdf_bytes)/1024)
    except OSError as e:
        log.error("[DOWNLOAD-DEBUG] FAILED TO READ FILE INTO RAM: %s", e)
        return jsonify({"error": "file expired or missing"}), 410

    gc.collect()

    resp = Response(pdf_bytes, status=200, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    resp.headers["Content-Length"]      = str(len(pdf_bytes))
    resp.headers["X-Accel-Buffering"]   = "no"
    resp.headers["Cache-Control"]       = "no-store"

    log.info("[DOWNLOAD-DEBUG] <<<<<<<<<< RESPONSE BEING SENT:")
    log.info("[DOWNLOAD-DEBUG]   Status          : 200 OK")
    log.info("[DOWNLOAD-DEBUG]   Content-Length  : %d bytes", len(pdf_bytes))
    return resp


@jobs_bp.route("/api/jobs/<jid>/zip-file", methods=["GET"])
def job_zip_file(jid):
    j = job_get(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    if j["status"] != "done":
        return jsonify({"error": f"job not finished: {j['status']}", "phase": j["phase"],
                        "progress": j["progress"]}), 409
    path = j["file_path"]
    if not path or not os.path.exists(path):
        return jsonify({"error": "file expired or missing"}), 410

    import re
    safe_name = re.sub(r"[^A-Za-z0-9._\-]", "_", j.get("download_name") or "cards.zip")
    schedule_delete(path, PDF_RETENTION_SECONDS)

    try:
        with open(path, "rb") as fh:
            zip_bytes = fh.read()
    except OSError as e:
        log.error("Could not read ZIP file: %s", e)
        return jsonify({"error": "file expired or missing"}), 410

    gc.collect()
    resp = Response(zip_bytes, status=200, mimetype="application/zip")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    resp.headers["Content-Length"]      = str(len(zip_bytes))
    resp.headers["X-Accel-Buffering"]   = "no"
    resp.headers["Cache-Control"]       = "no-store"
    return resp


@jobs_bp.route("/api/jobs/<jid>", methods=["DELETE"])
def job_cancel(jid):
    j = job_get(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    status = j.get("status")
    if status in ("done",):
        log.info("[pdf-lifecycle] /jobs/%s ignored DELETE (status=done, file retained)", jid)
        return jsonify({"ok": True, "retained": True})
    try:
        p = j.get("file_path")
        if p:
            schedule_delete(p, 30)
    except Exception:
        pass
    # We remove it from the in-memory jobs registry in jobs module
    from src.jobs import _jobs, _jobs_lock
    with _jobs_lock:
        _jobs.pop(jid, None)
    return jsonify({"ok": True})
