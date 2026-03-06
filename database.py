import sqlite3
import hashlib
from datetime import datetime, timedelta


class Database:
    def __init__(self, db_path="antalogue.db"):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'moderator',
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    sender_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip_hash TEXT NOT NULL,
                    ip_raw TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    banned_by TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now')),
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT UNIQUE NOT NULL,
                    ip1_hash TEXT NOT NULL,
                    ip2_hash TEXT NOT NULL,
                    ip1_raw TEXT DEFAULT '',
                    ip2_raw TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id);
                CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(created_at);
                CREATE INDEX IF NOT EXISTS idx_ban_ip ON bans(ip_hash);
                CREATE INDEX IF NOT EXISTS idx_report_status ON reports(status);
                CREATE INDEX IF NOT EXISTS idx_session_chat ON chat_sessions(chat_id);
            """)

    # ── Admin management ──────────────────────────────────────────────

    def hash_password(self, password):
        return hashlib.sha256(password.encode()).hexdigest()

    def create_admin(self, username, password, role="moderator"):
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO admins (username, password_hash, role) VALUES (?, ?, ?)",
                    (username, self.hash_password(password), role),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def verify_admin(self, username, password):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, username, role FROM admins WHERE username=? AND password_hash=?",
                (username, self.hash_password(password)),
            ).fetchone()
            return dict(row) if row else None

    def list_admins(self):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, username, role, created_at FROM admins ORDER BY created_at"
            ).fetchall()]

    def delete_admin(self, admin_id):
        with self._conn() as conn:
            conn.execute("DELETE FROM admins WHERE id=?", (admin_id,))

    # ── Messages ──────────────────────────────────────────────────────

    def add_message(self, chat_id, sender_index, content):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, sender_index, content) VALUES (?, ?, ?)",
                (chat_id, sender_index, content),
            )

    def get_messages(self, chat_id):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, chat_id, sender_index, content, created_at FROM messages WHERE chat_id=? ORDER BY created_at",
                (chat_id,),
            ).fetchall()]

    def search_messages(self, query, limit=100):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, chat_id, sender_index, content, created_at FROM messages WHERE content LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()]

    # ── Reports ───────────────────────────────────────────────────────

    def add_report(self, chat_id, reason=""):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO reports (chat_id, reason) VALUES (?, ?)",
                (chat_id, reason),
            )

    def get_reports(self, status=None, limit=200):
        with self._conn() as conn:
            if status:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM reports WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()]
            return [dict(r) for r in conn.execute(
                "SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    def resolve_report(self, report_id, status="resolved"):
        with self._conn() as conn:
            conn.execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))

    # ── Bans ──────────────────────────────────────────────────────────

    def ban_ip(self, ip_hash, reason="", banned_by="", duration_hours=None, ip_raw=""):
        expires = None
        if duration_hours:
            expires = (datetime.utcnow() + timedelta(hours=duration_hours)).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bans (ip_hash, ip_raw, reason, banned_by, expires_at) VALUES (?, ?, ?, ?, ?)",
                (ip_hash, ip_raw, reason, banned_by, expires),
            )

    def is_banned(self, ip_hash):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM bans WHERE ip_hash=? AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY created_at DESC LIMIT 1",
                (ip_hash,),
            ).fetchone()
            return dict(row) if row else None

    def list_bans(self):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM bans ORDER BY created_at DESC"
            ).fetchall()]

    def remove_ban(self, ban_id):
        with self._conn() as conn:
            conn.execute("DELETE FROM bans WHERE id=?", (ban_id,))

    # ── Chat sessions ─────────────────────────────────────────────────

    def add_session(self, chat_id, ip1_hash, ip2_hash, ip1_raw="", ip2_raw=""):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO chat_sessions (chat_id, ip1_hash, ip2_hash, ip1_raw, ip2_raw) VALUES (?, ?, ?, ?, ?)",
                (chat_id, ip1_hash, ip2_hash, ip1_raw, ip2_raw),
            )

    def end_session(self, chat_id):
        with self._conn() as conn:
            conn.execute("UPDATE chat_sessions SET active=0 WHERE chat_id=?", (chat_id,))

    def get_sessions(self, limit=100):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM chat_sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    def get_ip_for_chat(self, chat_id, sender_index):
        """Get the ip_hash and ip_raw for a specific sender in a chat session."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ip1_hash, ip2_hash, ip1_raw, ip2_raw FROM chat_sessions WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if row:
                if sender_index == 0:
                    return {"ip_hash": row["ip1_hash"], "ip_raw": row["ip1_raw"]}
                return {"ip_hash": row["ip2_hash"], "ip_raw": row["ip2_raw"]}
            return None

    # ── Stats ─────────────────────────────────────────────────────────

    def get_stats(self):
        with self._conn() as conn:
            total_chats = conn.execute("SELECT COUNT(*) as c FROM chat_sessions").fetchone()["c"]
            active_chats = conn.execute("SELECT COUNT(*) as c FROM chat_sessions WHERE active=1").fetchone()["c"]
            total_messages = conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
            pending_reports = conn.execute("SELECT COUNT(*) as c FROM reports WHERE status='pending'").fetchone()["c"]
            active_bans = conn.execute(
                "SELECT COUNT(*) as c FROM bans WHERE expires_at IS NULL OR expires_at > datetime('now')"
            ).fetchone()["c"]
            return {
                "total_chats": total_chats,
                "active_chats": active_chats,
                "total_messages": total_messages,
                "pending_reports": pending_reports,
                "active_bans": active_bans,
            }

    # ── Cleanup ───────────────────────────────────────────────────────

    def cleanup(self, retention_days=7):
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM chat_sessions WHERE created_at < ? AND active=0", (cutoff,))
            deleted = conn.execute("SELECT changes() as c").fetchone()["c"]
            return deleted
