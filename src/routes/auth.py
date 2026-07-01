import time
import logging
from flask import Blueprint, request, jsonify
from src.config import MAX_CONCURRENT_USERS
from src.database import (
    verify_access_code, create_session, remove_session,
    delete_all_sessions, get_active_users_count, _new_session_token
)
# We import _store from routes.students since it's the global in-memory store
from src.routes.students import _store

log = logging.getLogger("idcard.routes.auth")
auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/api/login", methods=["POST"])
@auth_bp.route("/login", methods=["POST"])
def login():
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    code         = (data.get("code") or "").strip()
    resume_token = (data.get("resume_token") or "").strip()
    client_id    = (data.get("client_id") or request.headers.get("X-Client-ID", "")).strip()

    now = time.time()
    
    try:
        username, role = verify_access_code(code)
        if not username:
            return jsonify({"error": "Invalid access code.", "code": "BAD_CODE"}), 401
            
        tok = None
        if resume_token:
            # check_token updates timestamp if valid
            from src.database import check_token
            if check_token(resume_token):
                tok = resume_token
                
        if not tok:
            active_count = get_active_users_count()
            if active_count >= MAX_CONCURRENT_USERS:
                return jsonify({
                    "error":        f"Server is full ({active_count}/{MAX_CONCURRENT_USERS} seats).",
                    "code":         "SEATS_FULL",
                    "active_users": active_count,
                    "max_users":    MAX_CONCURRENT_USERS,
                }), 503
                
            tok = _new_session_token()
            create_session(tok, client_id, username)
            
        active_count = get_active_users_count()
        return jsonify({
            "session_token": tok,
            "active_users":  active_count,
            "max_users":     MAX_CONCURRENT_USERS,
        })
    except Exception as e:
        log.error("Login failed: %s", e)
        return jsonify({"error": f"Login failed: {e}", "code": "SERVER_ERROR"}), 500


@auth_bp.route("/api/logout", methods=["POST"])
@auth_bp.route("/logout", methods=["POST"])
def logout():
    tok = request.headers.get("X-Session-Token", "").strip()
    try:
        if tok:
            remove_session(tok)
        try:
            data = request.get_json(silent=True) or {}
        except Exception:
            data = {}
        cid = (data.get("client_id") or request.headers.get("X-Client-ID", "")).strip()
        if cid:
            # Remove any sessions for this client_id
            import sqlite3
            from src.database import DB_PATH
            with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
                conn.execute("DELETE FROM sessions WHERE client_id = ?", (cid,))
                conn.commit()
                
        active_count = get_active_users_count()
        return jsonify({"ok": True, "active_users": active_count, "max_users": MAX_CONCURRENT_USERS})
    except Exception as e:
        log.error("Logout failed: %s", e)
        return jsonify({"error": f"Logout failed: {e}"}), 500


@auth_bp.route("/api/clear-sessions", methods=["GET", "POST"])
@auth_bp.route("/clear-sessions", methods=["GET", "POST"])
def clear_sessions():
    try:
        delete_all_sessions()
        return jsonify({"ok": True, "active_users": 0, "max_users": MAX_CONCURRENT_USERS})
    except Exception as e:
        log.error("Clear sessions failed: %s", e)
        return jsonify({"error": f"Clear sessions failed: {e}"}), 500


@auth_bp.route("/api/sessions", methods=["GET"])
@auth_bp.route("/sessions", methods=["GET"])
def get_sessions_info():
    students = _store.get("students") or []
    return jsonify({
        "sessions_disabled":   True,
        "active_sessions":     1 if students else 0,
        "your_students_loaded": len(students),
        "your_school":         _store.get("school_name") or "None",
    })
