import os
import time
import sqlite3
import logging
import uuid
from src.config import BASE_DIR, SESSION_TTL, MAX_CONCURRENT_USERS, ACCESS_CODE

log = logging.getLogger("idcard.database")

DB_PATH = os.path.join(BASE_DIR, "sessions.db")

def init_db():
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
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
        env_code = (ACCESS_CODE or "").strip()
        if env_code:
            cursor.execute("INSERT OR REPLACE INTO users (username, access_code, role) VALUES (?, ?, ?)", 
                           ("env_user", env_code, "admin"))
            conn.commit()
            log.info("[db] Synced env ACCESS_CODE as 'env_user'")

def _new_session_token() -> str:
    return "session_" + uuid.uuid4().hex

def get_active_users_count() -> int:
    cutoff = time.time() - SESSION_TTL
    try:
        with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
            # Clean expired first
            conn.execute("DELETE FROM sessions WHERE last_activity < ?", (cutoff,))
            conn.commit()
            
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions")
            return cursor.fetchone()[0]
    except Exception as e:
        log.error("[db] Failed to get active users count: %s", e)
        return 0

def check_token(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    cutoff = now - SESSION_TTL
    try:
        with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
            conn.execute("DELETE FROM sessions WHERE last_activity < ?", (cutoff,))
            conn.commit()
            
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM sessions WHERE token = ?", (token,))
            row = cursor.fetchone()
            if row:
                conn.execute("UPDATE sessions SET last_activity = ? WHERE token = ?", (now, token))
                conn.commit()
                return True
    except Exception as e:
        log.error("[db] Session checking error: %s", e)
    return False

def verify_access_code(code: str) -> tuple:
    """Returns (username, role) if code is valid, else (None, None)"""
    try:
        with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT username, role FROM users WHERE access_code = ?", (code,))
            row = cursor.fetchone()
            if row:
                return row
            if ACCESS_CODE and code == ACCESS_CODE:
                return "env_user", "admin"
            if not ACCESS_CODE:
                return "guest", "staff"
    except Exception as e:
        log.error("[db] Access code verification error: %s", e)
    return None, None

def create_session(token: str, client_id: str, username: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
            now = time.time()
            if client_id:
                conn.execute("DELETE FROM sessions WHERE client_id = ?", (client_id,))
            conn.execute("INSERT INTO sessions (token, created, last_activity, client_id, username) VALUES (?, ?, ?, ?, ?)",
                         (token, now, now, client_id, username))
            conn.commit()
            return True
    except Exception as e:
        log.error("[db] Session creation error: %s", e)
    return False

def remove_session(token: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return True
    except Exception as e:
        log.error("[db] Session removal error: %s", e)
    return False

def delete_all_sessions() -> bool:
    try:
        with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
            conn.execute("DELETE FROM sessions")
            conn.commit()
            return True
    except Exception as e:
        log.error("[db] Clear sessions error: %s", e)
    return False

def get_sessions_list() -> list:
    try:
        now = time.time()
        with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT token, username, created, last_activity, client_id FROM sessions")
            rows = cursor.fetchall()
            return [
                {
                    "token_truncated": r[0][:12] + "...",
                    "username": r[1],
                    "age_seconds": int(now - r[2]),
                    "idle_seconds": int(now - r[3]),
                    "client_id": r[4][:12] + "..." if r[4] else "none"
                }
                for r in rows
            ]
    except Exception as e:
        log.error("[db] Failed to list sessions: %s", e)
        return []


def prune_sessions() -> None:
    cutoff = time.time() - SESSION_TTL
    try:
        with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
            conn.execute("DELETE FROM sessions WHERE last_activity < ?", (cutoff,))
            conn.commit()
    except Exception as e:
        log.error("[db] Session pruning error: %s", e)

