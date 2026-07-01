import os
import re
import time
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS

from src.config import SESSION_TTL
from src.database import init_db, check_token, prune_sessions

# Import Blueprints
from src.routes.auth import auth_bp
from src.routes.students import students_bp
from src.routes.employees import employees_bp
from src.routes.templates import templates_bp
from src.routes.jobs import jobs_bp
from src.routes.system import system_bp

log = logging.getLogger("idcard.app")


def create_app():
    # Setup database
    try:
        init_db()
    except Exception as e:
        log.error("Failed to initialize database: %s", e)

    # Force jobs background threads to start
    import src.jobs

    app = Flask(__name__)

    # Custom logging filter to quieten psutil polling in terminal
    class SystemStatsFilter(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return "/system/stats" not in msg and "/api/system/stats" not in msg

    logging.getLogger("werkzeug").addFilter(SystemStatsFilter())

    # CORS configuration
    CORS(app,
         origins=["*"],
         methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
         allow_headers=["Content-Type", "Authorization", "X-Requested-With", "X-Session-Token", "X-Client-ID"],
         supports_credentials=False,
         expose_headers=["Content-Disposition", "Content-Type", "X-Students-Count", "Content-Length", "X-Job-ID"])

    # Register Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(students_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(templates_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(system_bp)

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
        elif re.match(r"/api/jobs/[0-9a-f]{32}/file", path) or re.match(r"/api/jobs/[0-9a-f]{32}/progress", path):
            is_public = True

        if is_public:
            return

        tok = (request.headers.get("X-Session-Token", "") or request.args.get("token", "")).strip()
        if not tok:
            return jsonify({"error": "Session token required", "code": "NO_SESSION"}), 401

        try:
            # Prune and validate session
            prune_sessions()
            if not check_token(tok):
                return jsonify({"error": "Session has expired or is invalid.", "code": "BAD_SESSION"}), 401
        except Exception as e:
            log.error("Session validation error: %s", e)

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

    return app
