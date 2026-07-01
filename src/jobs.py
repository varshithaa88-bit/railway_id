import os
import gc
import uuid
import time
import logging
import threading
from src.config import JOB_TTL_SECONDS, PDF_RETENTION_SECONDS

log = logging.getLogger("idcard.jobs")
_SERVER_BOOT_TS = time.time()

# Jobs Registry
_jobs = {}
_jobs_lock = threading.Lock()

# fitz (PyMuPDF) thread-safety lock for rendering fallback
_fitz_render_lock = threading.Lock()

# Delayed-Delete Scheduler
_pending_deletes = {}
_pending_lock = threading.Lock()

# CPU Sampler Cache
_CPU_PCT_CACHE = {"value": 0, "ts": 0.0}
_CPU_SAMPLER_STARTED = False

try:
    import psutil as _psutil
    _HAS_PSUTIL = True
    _PSUTIL_PROC = _psutil.Process(os.getpid())
    try:
        _PSUTIL_PROC.cpu_percent(None)
    except Exception:
        pass
    try:
        _psutil.cpu_percent(None)
    except Exception:
        pass
except Exception:
    _psutil = None
    _HAS_PSUTIL = False
    _PSUTIL_PROC = None


def prune_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    dead = []
    with _jobs_lock:
        for jid, j in _jobs.items():
            ts = j.get("finished_at") or j.get("created_at")
            if ts is None or ts < cutoff:
                dead.append(jid)
        for jid in dead:
            try:
                p = _jobs[jid].get("file_path")
                if p:
                    schedule_delete(p, 30)
            except Exception:
                pass
            _jobs.pop(jid, None)


def new_job(total: int) -> str:
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


def job_set(jid: str, **kwargs):
    with _jobs_lock:
        if jid not in _jobs:
            return
        _jobs[jid].update(kwargs)


def job_get(jid: str):
    with _jobs_lock:
        return dict(_jobs[jid]) if jid in _jobs else None


def schedule_delete(path: str, after_seconds: int = None):
    """Mark a file for deletion `after_seconds` later. Idempotent."""
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
    try:
        if path and os.path.exists(path):
            os.unlink(path)
            log.info("[pdf-lifecycle] deleted: %s", path)
        return True
    except Exception as e:
        log.warning("[pdf-lifecycle] delete failed (will retry): %s: %s", path, e)
        return False


def _reaper_loop():
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
                    with _pending_lock:
                        _pending_deletes.pop(p, None)
                else:
                    with _pending_lock:
                        _pending_deletes[p] = time.time() + 60
            prune_old_jobs()
        except Exception:
            pass
        time.sleep(30)


def start_reaper_thread():
    t = threading.Thread(target=_reaper_loop, daemon=True, name="pdf-reaper")
    t.start()


def start_cpu_sampler():
    global _CPU_SAMPLER_STARTED
    if _CPU_SAMPLER_STARTED or not _HAS_PSUTIL:
        return
    _CPU_SAMPLER_STARTED = True
    def _loop():
        while True:
            try:
                pct = _psutil.cpu_percent(interval=1.0)
                _CPU_PCT_CACHE["value"] = int(round(pct))
                _CPU_PCT_CACHE["ts"]    = time.time()
            except Exception:
                time.sleep(1.0)
    t = threading.Thread(target=_loop, name="cpu-sampler", daemon=True)
    t.start()


def get_cpu_usage() -> int:
    return _CPU_PCT_CACHE["value"]


def get_process_ram_mb() -> float:
    if not _HAS_PSUTIL or not _PSUTIL_PROC:
        return 0.0
    try:
        return _PSUTIL_PROC.memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0
