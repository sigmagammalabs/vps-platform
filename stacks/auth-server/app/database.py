import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATA_DIR = Path(os.getenv("AUTH_DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "licenses.db"


def get_db_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with get_db_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL,
                tool_id TEXT NOT NULL DEFAULT 'all',
                max_devices INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_id INTEGER NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
                hwid TEXT NOT NULL,
                device_name TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                ip_address TEXT,
                UNIQUE(license_id, hwid)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT,
                hwid TEXT,
                tool_id TEXT,
                action TEXT NOT NULL,
                ip_address TEXT,
                timestamp TEXT NOT NULL,
                details TEXT
            );
        """)
        conn.commit()


def generate_random_license_key(prefix: str = "ACT") -> str:
    """Generiert einen formatierten Schlüssel: ACT-XXXX-XXXX-XXXX-XXXX"""
    random_hex = secrets.token_hex(8).upper()
    return f"{prefix}-{random_hex[0:4]}-{random_hex[4:8]}-{random_hex[8:12]}-{random_hex[12:16]}"


def create_license(
    client_name: str,
    tool_id: str = "all",
    days_valid: Optional[int] = 30,
    max_devices: int = 1,
    notes: str = "",
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires_at = None
    if days_valid is not None and days_valid > 0:
        exp_ts = int(now.timestamp()) + (days_valid * 86400)
        expires_at = datetime.fromtimestamp(exp_ts, timezone.utc).isoformat()

    key = generate_random_license_key()

    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO licenses (license_key, client_name, tool_id, max_devices, status, expires_at, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (key, client_name.strip(), tool_id.strip(), max_devices, expires_at, notes.strip(), now_iso, now_iso),
        )
        lic_id = cursor.lastrowid
        conn.commit()

    return {
        "id": lic_id,
        "license_key": key,
        "client_name": client_name,
        "tool_id": tool_id,
        "max_devices": max_devices,
        "status": "active",
        "expires_at": expires_at,
        "notes": notes,
        "created_at": now_iso,
    }


def get_license_by_key(license_key: str) -> Optional[Dict[str, Any]]:
    clean_key = license_key.strip().upper()
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (clean_key,)).fetchone()
        if not row:
            return None
        lic = dict(row)
        dev_rows = conn.execute(
            "SELECT hwid, device_name, first_seen_at, last_seen_at, ip_address FROM devices WHERE license_id = ?",
            (lic["id"],),
        ).fetchall()
        lic["devices"] = [dict(d) for d in dev_rows]
        return lic


def list_licenses() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
        results = []
        for r in rows:
            lic = dict(r)
            dev_rows = conn.execute(
                "SELECT hwid, device_name, first_seen_at, last_seen_at, ip_address FROM devices WHERE license_id = ?",
                (lic["id"],),
            ).fetchall()
            lic["devices"] = [dict(d) for d in dev_rows]

            # Resttage berechnen
            days_left = None
            if lic["expires_at"]:
                try:
                    exp_dt = datetime.fromisoformat(lic["expires_at"])
                    diff = exp_dt - datetime.now(timezone.utc)
                    days_left = max(0, diff.days)
                except Exception:
                    pass
            lic["days_left"] = days_left
            results.append(lic)
        return results


def update_license_status(license_key: str, status: str) -> bool:
    clean_key = license_key.strip().upper()
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        cursor = conn.execute(
            "UPDATE licenses SET status = ?, updated_at = ? WHERE license_key = ?",
            (status, now_iso, clean_key),
        )
        conn.commit()
        return cursor.rowcount > 0


def reset_license_devices(license_key: str) -> int:
    """Löscht alle gebundenen Geräte einer Lizenz (z. B. wenn der Kunde einen neuen PC hat)."""
    clean_key = license_key.strip().upper()
    with get_db_connection() as conn:
        lic = conn.execute("SELECT id FROM licenses WHERE license_key = ?", (clean_key,)).fetchone()
        if not lic:
            return 0
        cursor = conn.execute("DELETE FROM devices WHERE license_id = ?", (lic["id"],))
        conn.commit()
        return cursor.rowcount


def delete_license(license_key: str) -> bool:
    clean_key = license_key.strip().upper()
    with get_db_connection() as conn:
        cursor = conn.execute("DELETE FROM licenses WHERE license_key = ?", (clean_key,))
        conn.commit()
        return cursor.rowcount > 0


def record_device_verification(
    license_id: int,
    hwid: str,
    device_name: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO devices (license_id, hwid, device_name, first_seen_at, last_seen_at, ip_address)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(license_id, hwid) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                device_name = COALESCE(excluded.device_name, devices.device_name),
                ip_address = COALESCE(excluded.ip_address, devices.ip_address)
            """,
            (license_id, hwid, device_name, now_iso, now_iso, ip_address),
        )
        conn.commit()


def log_audit(
    license_key: Optional[str],
    hwid: Optional[str],
    tool_id: Optional[str],
    action: str,
    ip_address: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (license_key, hwid, tool_id, action, ip_address, timestamp, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (license_key, hwid, tool_id, action, ip_address, now_iso, details),
            )
            conn.commit()
    except Exception:
        pass


def get_audit_logs(limit: int = 40) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
