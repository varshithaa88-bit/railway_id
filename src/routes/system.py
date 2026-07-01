import time
import logging
from flask import Blueprint, jsonify
from src.config import MAX_CONCURRENT_USERS, BASE_DIR
from src.jobs import _HAS_PSUTIL, _psutil, _CPU_PCT_CACHE, _SERVER_BOOT_TS
from src.database import get_active_users_count

log = logging.getLogger("idcard.routes.system")
system_bp = Blueprint("system", __name__)


@system_bp.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "message": "ID Card Generator API is running"})


@system_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "ID Card Generator API is healthy"})


def _system_stats_payload():
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
    active_users = get_active_users_count()

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


@system_bp.route("/api/system/stats", methods=["GET"])
@system_bp.route("/system/stats", methods=["GET"])
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
            "active_users": get_active_users_count(),
            "max_users":    MAX_CONCURRENT_USERS,
            "error":        str(e),
        })
