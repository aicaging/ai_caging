"""Database module for Caging - SQLite3 DAO layer."""
import sqlite3
import json
import os
import threading
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = None
_local = threading.local()


def get_connection() -> sqlite3.Connection:
    """Get thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH or "caging.db", timeout=5)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


def _write(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Execute a write SQL with proper rollback on failure.

    Ensures the connection is never left with an open/failed transaction.
    """
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur
    except Exception:
        conn.rollback()
        raise


def init_db(db_path: str):
    """Initialize database and create tables."""
    global DB_PATH
    # Reset thread-local connection if path changes
    if DB_PATH != db_path:
        if hasattr(_local, "conn"):
            try:
                _local.conn.close()
            except Exception:
                pass
            _local.conn = None
    DB_PATH = db_path
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS auto_approve_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL UNIQUE,
            created_by TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('requester','reviewer','admin')),
            api_key_hash TEXT NOT NULL,
            system_user TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS protections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            original_user TEXT NOT NULL,
            requester_id TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT 'na',
            original_mode INTEGER DEFAULT NULL,
            protected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL CHECK(type IN ('exec','release','protect')),
            requester_id TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT 'na',
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            policy_result TEXT,
            risk_score INTEGER,
            dual_approval_required BOOLEAN DEFAULT 0,
            approval_count INTEGER DEFAULT 0,
            reviewer_id TEXT,
            default_reviewer_used BOOLEAN DEFAULT 0,
            review_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            updated_at TIMESTAMP,
            executed_at TIMESTAMP,
            execution_result TEXT,
            escalated_to_parent BOOLEAN DEFAULT 0,
            parent_request_id TEXT,
            FOREIGN KEY (requester_id) REFERENCES users(id),
            FOREIGN KEY (reviewer_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT,
            actor TEXT,
            action TEXT,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (request_id) REFERENCES requests(id)
        );

        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS policy_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            condition TEXT NOT NULL DEFAULT 'true',
            action TEXT NOT NULL CHECK(action IN ('allow','require_human','escalate','deny','ai','ai_screen')),
            reason TEXT DEFAULT '',
            dual_approval BOOLEAN DEFAULT 0,
            priority INTEGER DEFAULT 100,
            enabled BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );
    """)
    conn.commit()

    # Migration: ensure policy table columns exist (for upgrades)
    try:
        conn.execute("ALTER TABLE policy_rules ADD COLUMN reason TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE policy_rules ADD COLUMN dual_approval BOOLEAN DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE policy_rules ADD COLUMN priority INTEGER DEFAULT 100")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE policy_rules ADD COLUMN enabled BOOLEAN DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    # Migration: add ai_screen to CHECK constraint (rebuild table)
    try:
        # Test if ai_screen is valid by trying to insert-and-rollback
        conn.execute("SAVEPOINT _ck_migration")
        conn.execute("INSERT INTO policy_rules(name,condition,action) VALUES('_migration_test','false','ai_screen')")
        conn.execute("ROLLBACK TO _ck_migration")
        conn.execute("RELEASE _ck_migration")
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        # ai_screen not yet allowed — rebuild table
        try:
            conn.execute("ROLLBACK TO _ck_migration")
        except Exception:
            pass
        try:
            conn.execute("RELEASE _ck_migration")
        except Exception:
            pass
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS policy_rules_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                condition TEXT NOT NULL DEFAULT 'true',
                action TEXT NOT NULL CHECK(action IN ('allow','require_human','escalate','deny','ai','ai_screen')),
                reason TEXT DEFAULT '',
                dual_approval BOOLEAN DEFAULT 0,
                priority INTEGER DEFAULT 100,
                enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            );
            INSERT INTO policy_rules_new SELECT * FROM policy_rules;
            DROP TABLE policy_rules;
            ALTER TABLE policy_rules_new RENAME TO policy_rules;
        """)
        conn.commit()

    # Migration: add original_mode to protections table for existing DBs
    try:
        conn.execute("ALTER TABLE protections ADD COLUMN original_mode INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: add password_hash to users table for username/password login
    try:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: add topic to protections table
    try:
        conn.execute("ALTER TABLE protections ADD COLUMN topic TEXT NOT NULL DEFAULT 'na'")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: add topic to requests table
    try:
        conn.execute("ALTER TABLE requests ADD COLUMN topic TEXT NOT NULL DEFAULT 'na'")
    except sqlite3.OperationalError:
        pass  # Column already exists

# ---- User DAO ----
def create_user(client_id: str, role: str, api_key_hash: str, system_user: str) -> dict:
    conn = get_connection()
    _write(conn,
        "INSERT INTO users (id, role, api_key_hash, system_user) VALUES (?, ?, ?, ?)",
        (client_id, role, api_key_hash, system_user),
    )
    return {"id": client_id, "role": role, "system_user": system_user}


def get_user(client_id: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (client_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_api_key_hash(api_key_hash: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE api_key_hash = ?", (api_key_hash,)).fetchone()
    return dict(row) if row else None


def update_user_key(client_id: str, api_key_hash: str) -> dict:
    """Update an existing user's API key hash."""
    conn = get_connection()
    _write(conn, "UPDATE users SET api_key_hash = ? WHERE id = ?", (api_key_hash, client_id))
    return {"id": client_id, "updated": True}


def list_users() -> list:
    conn = get_connection()
    rows = conn.execute("SELECT id, role, system_user, created_at FROM users ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def update_user(client_id: str, **kwargs) -> bool:
    allowed = {"role", "system_user", "api_key_hash", "password_hash"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [client_id]
    conn = get_connection()
    _write(conn, f"UPDATE users SET {set_clause} WHERE id = ?", vals)
    return conn.total_changes > 0


def delete_user(client_id: str) -> bool:
    conn = get_connection()
    _write(conn, "DELETE FROM users WHERE id = ?", (client_id,))
    return conn.total_changes > 0


# ---- Protection DAO ----
def create_protection(path: str, original_user: str, requester_id: str, topic: str = "na") -> dict:
    conn = get_connection()
    try:
        # Save original file mode before changing
        mode = os.stat(path).st_mode & 0o777
        _write(conn,
            "INSERT INTO protections (path, original_user, requester_id, topic, original_mode) VALUES (?, ?, ?, ?, ?)",
            (path, original_user, requester_id, topic, mode),
        )
        return {"path": path, "original_user": original_user, "original_mode": mode}
    except sqlite3.IntegrityError:
        conn.rollback()
        return get_protection(path)


def get_protection(path: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM protections WHERE path = ?", (path,)).fetchone()
    return dict(row) if row else None


def delete_protection(path: str) -> bool:
    conn = get_connection()
    _write(conn, "DELETE FROM protections WHERE path = ?", (path,))
    return conn.total_changes > 0


# ---- Request DAO ----
import threading
from datetime import datetime

_id_lock = threading.Lock()
_id_last_ms: str = ""
_id_seq: int = 0


def _new_id() -> str:
    """Generate a response ID in format: cage1-[yyyyMMddHHmmssSSS]-[seq].

    The sequence number (01–99) resets each millisecond, ensuring uniqueness
    for up to 99 IDs generated within the same millisecond.
    """
    global _id_last_ms, _id_seq
    now = datetime.now()
    ms_key = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    with _id_lock:
        if ms_key == _id_last_ms:
            _id_seq += 1
        else:
            _id_last_ms = ms_key
            _id_seq = 1
        if _id_seq > 99:
            # If seq exceeds 99, sleep 1ms so the timestamp advances
            import time
            time.sleep(0.001)
            now = datetime.now()
            ms_key = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
            _id_last_ms = ms_key
            _id_seq = 1
        return f"cage1-{ms_key}-{_id_seq:02d}"


def create_request(req_type: str, requester_id: str, payload: dict, dual_approval: bool = False,
                   reviewer_id: Optional[str] = None,
                   ttl_hours: int = 24, topic: str = "na") -> dict:
    req_id = _new_id()
    expires_at = (datetime.utcnow() + timedelta(hours=ttl_hours)).isoformat()
    conn = get_connection()
    _write(conn,
        """INSERT INTO requests (id, type, requester_id, topic, payload, status, dual_approval_required,
           approval_count, reviewer_id, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (req_id, req_type, requester_id, topic, json.dumps(payload),
         "pending", 1 if dual_approval else 0,
         reviewer_id, expires_at),
    )
    _audit(req_id, requester_id, "created", f"{req_type} request created")
    return get_request(req_id)


def get_request(request_id: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if row:
        d = dict(row)
        if isinstance(d.get("payload"), str):
            d["payload"] = json.loads(d["payload"])
        if isinstance(d.get("execution_result"), str):
            try:
                d["execution_result"] = json.loads(d["execution_result"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d
    return None


def update_request(request_id: str, **kwargs) -> bool:
    allowed = {"status", "policy_result", "risk_score", "approval_count", "reviewer_id",
               "default_reviewer_used", "review_note", "updated_at", "executed_at",
               "execution_result", "escalated_to_parent", "parent_request_id"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False
    if "execution_result" in updates and isinstance(updates["execution_result"], dict):
        updates["execution_result"] = json.dumps(updates["execution_result"])
    updates["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [request_id]
    conn = get_connection()
    _write(conn, f"UPDATE requests SET {set_clause} WHERE id = ?", vals)
    return conn.total_changes > 0


def list_requests(status: Optional[str] = None, reviewer_id: Optional[str] = None,
                  catalog: Optional[str] = None, limit: int = 100) -> list:
    conn = get_connection()
    sql = "SELECT * FROM requests WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if reviewer_id:
        sql += " AND reviewer_id = ?"
        params.append(reviewer_id)
    if catalog:
        sql += " AND json_extract(payload, '$.catalog') = ?"
        params.append(catalog)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("payload"), str):
            d["payload"] = json.loads(d["payload"])
        result.append(d)
    return result


def delete_request(request_id: str) -> bool:
    """Delete a single request and its audit log entries."""
    conn = get_connection()
    _write(conn, "DELETE FROM audit_log WHERE request_id = ?", (request_id,))
    _write(conn, "DELETE FROM requests WHERE id = ?", (request_id,))
    return True


def delete_all_requests() -> int:
    """Delete ALL requests and their audit log entries. Returns count deleted."""
    conn = get_connection()
    _write(conn, "DELETE FROM audit_log WHERE request_id IN (SELECT id FROM requests)")
    _write(conn, "DELETE FROM requests")
    return conn.total_changes


def count_pending_by_reviewer() -> dict:
    conn = get_connection()
    rows = conn.execute(
        "SELECT reviewer_id, COUNT(*) as cnt FROM requests WHERE status IN ('awaiting_review','first_approved') GROUP BY reviewer_id"
    ).fetchall()
    return {r["reviewer_id"] or "unassigned": r["cnt"] for r in rows}


# ---- Audit DAO ----
def _audit(request_id: str, actor: str, action: str, details: str = ""):
    conn = get_connection()
    _write(conn,
        "INSERT INTO audit_log (request_id, actor, action, details) VALUES (?, ?, ?, ?)",
        (request_id, actor, action, details),
    )


def get_audit_log(request_id: str) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, request_id, actor AS actor_id, action, details AS detail, "
        "timestamp AS created_at FROM audit_log WHERE request_id = ? "
        "ORDER BY timestamp", (request_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---- Config DAO ----
def get_config(key: str, default=None) -> Optional[str]:
    conn = get_connection()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str):
    conn = get_connection()
    _write(conn, "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))


# ---- Policy Rules DAO ----
def create_policy_rule(
    name: str, condition: str, action: str,
    reason: str = "", dual_approval: bool = False,
    priority: int = 100, enabled: bool = True,
) -> dict:
    conn = get_connection()
    cur = _write(conn,
        """INSERT INTO policy_rules (name, condition, action, reason, dual_approval, priority, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, condition, action, reason, int(dual_approval), priority, int(enabled)),
    )
    rule = conn.execute(
        "SELECT * FROM policy_rules WHERE id = ?", (cur.lastrowid,),
    ).fetchone()
    return dict(rule)


def list_policy_rules(enabled_only: bool = False) -> list:
    conn = get_connection()
    if enabled_only:
        rows = conn.execute(
            "SELECT * FROM policy_rules WHERE enabled = 1 ORDER BY priority ASC, id ASC",
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM policy_rules ORDER BY priority ASC, id ASC",
        ).fetchall()
    return [dict(r) for r in rows]


def get_policy_rule(rule_id: int) -> dict:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM policy_rules WHERE id = ?", (rule_id,),
    ).fetchone()
    return dict(row) if row else None


def update_policy_rule(
    rule_id: int, **kwargs,
) -> dict:
    allowed = {"name", "condition", "action", "reason", "dual_approval", "priority", "enabled"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return get_policy_rule(rule_id)
    fields["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [rule_id]
    conn = get_connection()
    _write(conn,
        f"UPDATE policy_rules SET {set_clause} WHERE id = ?", values,
    )
    return get_policy_rule(rule_id)


def delete_policy_rule(rule_id: int) -> bool:
    conn = get_connection()
    _write(conn, "DELETE FROM policy_rules WHERE id = ?", (rule_id,))
    return conn.total_changes > 0


def import_policy_rules_from_yaml(yaml_path: str) -> int:
    """Import rules from a YAML file into DB. Returns count imported."""
    import yaml
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    rules = data.get("rules", [])
    count = 0
    for r in rules:
        create_policy_rule(
            name=r.get("name", "imported"),
            condition=r.get("condition", "true"),
            action=r.get("action", "require_human"),
            reason=r.get("reason", ""),
            dual_approval=r.get("dual_approval", False),
            priority=r.get("priority", 100),
            enabled=r.get("enabled", True),
        )
        count += 1
    return count


# ---- Auto-Approve Topics DAO ----
def add_auto_approve_topic(topic: str, created_by: str) -> dict:
    """Add a topic to the auto-approve list. Returns the row or None if duplicate."""
    conn = get_connection()
    try:
        cur = _write(conn,
            "INSERT INTO auto_approve_topics (topic, created_by) VALUES (?, ?)",
            (topic.strip().lower(), created_by),
        )
        row = conn.execute(
            "SELECT * FROM auto_approve_topics WHERE id = ?", (cur.lastrowid,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.IntegrityError:
        conn.rollback()
        row = conn.execute(
            "SELECT * FROM auto_approve_topics WHERE topic = ?", (topic.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None


def remove_auto_approve_topic(topic: str) -> bool:
    """Remove a topic from the auto-approve list."""
    conn = get_connection()
    _write(conn, "DELETE FROM auto_approve_topics WHERE topic = ?", (topic.strip().lower(),))
    return conn.total_changes > 0


def list_auto_approve_topics() -> list:
    """List all auto-approved topics."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM auto_approve_topics ORDER BY created_at DESC",
    ).fetchall()
    return [dict(r) for r in rows]


def is_topic_auto_approved(topic: str) -> bool:
    """Check if a topic is in the auto-approve list."""
    if not topic or topic.strip().lower() == "na":
        return False
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM auto_approve_topics WHERE topic = ?",
        (topic.strip().lower(),),
    ).fetchone()
    return row is not None
