# backend/audit.py
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;",
        (name,),
    ).fetchone()
    return row is not None


def ensure_audit_tables(con: sqlite3.Connection) -> None:
    """
    Crea las tablas si no existen.
    ✅ NO hace commit aquí: el caller decide.
    """
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            actor_email TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            meta_json TEXT NOT NULL
        );
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON audit_logs(tenant_id, ts_utc);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);")


def audit_log(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    actor_email: str,
    actor_role: str,
    action: str,
    target: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Escribe en audit_logs usando SAVEPOINT (anidable).
    Si falla, NO revienta el negocio.
    """
    ensure_audit_tables(con)

    meta = meta or {}
    try:
        meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        meta_json = json.dumps({"_meta_dump_error": True}, ensure_ascii=False)

    sp_name = f"sp_audit_{int(datetime.now(timezone.utc).timestamp() * 1_000_000)}_{id(con) % 1_000_000}"

    try:
        con.execute(f"SAVEPOINT {sp_name};")
        con.execute(
            """
            INSERT INTO audit_logs (ts_utc, tenant_id, actor_email, actor_role, action, target, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                _now_utc_iso(),
                str(tenant_id or "").strip(),
                str(actor_email or "").strip().casefold(),
                str(actor_role or "").strip(),
                str(action or "").strip(),
                str(target or "").strip(),
                meta_json,
            ),
        )
        con.execute(f"RELEASE SAVEPOINT {sp_name};")
    except Exception as e:
        try:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
            con.execute(f"RELEASE SAVEPOINT {sp_name};")
        except Exception:
            pass
        # No relanzamos: auditoría no debe tumbar el negocio
        print(f"CRÍTICO: Falló audit_logs: {e}")


def prune_audit_logs(
    con: sqlite3.Connection,
    *,
    keep_days: int = 365,
    tenant_id: Optional[str] = None,
    include_global: bool = True,
    vacuum: bool = False,
) -> Dict[str, Any]:
    """
    Limpia logs antiguos usando SAVEPOINT (anidable).
    """
    ensure_audit_tables(con)

    keep_days = max(1, min(int(keep_days or 365), 3650))
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")

    deleted = 0
    sp_name = f"sp_prune_audit_{int(datetime.now(timezone.utc).timestamp() * 1_000_000)}_{id(con) % 1_000_000}"

    try:
        con.execute(f"SAVEPOINT {sp_name};")

        if tenant_id is None:
            cur = con.execute("DELETE FROM audit_logs WHERE ts_utc < ?;", (cutoff_iso,))
        else:
            t_id = str(tenant_id).strip()
            if include_global:
                cur = con.execute(
                    """
                    DELETE FROM audit_logs
                    WHERE ts_utc < ?
                      AND (tenant_id = ? OR tenant_id = '__GLOBAL__');
                    """,
                    (cutoff_iso, t_id),
                )
            else:
                cur = con.execute(
                    "DELETE FROM audit_logs WHERE ts_utc < ? AND tenant_id = ?;",
                    (cutoff_iso, t_id),
                )

        deleted = int(cur.rowcount or 0)
        con.execute(f"RELEASE SAVEPOINT {sp_name};")
    except Exception:
        try:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
            con.execute(f"RELEASE SAVEPOINT {sp_name};")
        except Exception:
            pass
        raise

    # vacuum incremental (opcional). Si hay transacción abierta, lo intentamos igual y si falla lo ignoramos.
    if vacuum:
        try:
            # incremental_vacuum es menos agresivo que VACUUM total
            con.execute("PRAGMA incremental_vacuum(100);")
        except Exception:
            pass

    return {"deleted": deleted, "cutoff_utc": cutoff_iso, "keep_days": keep_days}


def fetch_audit_logs(
    con: sqlite3.Connection,
    tenant_id: str,
    *,
    limit: int = 200,
    include_global: bool = True,
) -> List[Dict[str, Any]]:
    """
    Read-only.
    """
    if not _table_exists(con, "audit_logs"):
        return []

    limit = max(1, min(int(limit or 200), 2000))
    t_id = str(tenant_id or "").strip()
    if not t_id:
        return []

    if include_global:
        sql = """
        SELECT id, ts_utc, tenant_id, actor_email, actor_role, action, target, meta_json
        FROM audit_logs
        WHERE (tenant_id = ? OR tenant_id = '__GLOBAL__')
        ORDER BY id DESC
        LIMIT ?;
        """
        rows = con.execute(sql, (t_id, limit)).fetchall()
    else:
        sql = """
        SELECT id, ts_utc, tenant_id, actor_email, actor_role, action, target, meta_json
        FROM audit_logs
        WHERE tenant_id = ?
        ORDER BY id DESC
        LIMIT ?;
        """
        rows = con.execute(sql, (t_id, limit)).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except Exception:
            meta = {}
        out.append(
            {
                "id": r["id"],
                "ts_utc": r["ts_utc"],
                "tenant_id": r["tenant_id"],
                "actor_email": r["actor_email"],
                "actor_role": r["actor_role"],
                "action": r["action"],
                "target": r["target"],
                "meta": meta,
            }
        )
    return out