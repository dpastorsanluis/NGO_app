# backend/tenant_counter.py
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Set


# ✅ TX helper local (NO depende de backend.db)
@contextmanager
def _write_tx(con: sqlite3.Connection):
    sp_name = f"sp_{int(datetime.now(timezone.utc).timestamp() * 1_000_000)}_{id(con) % 1_000_000}"
    try:
        con.execute(f"SAVEPOINT {sp_name};")
        yield
        con.execute(f"RELEASE SAVEPOINT {sp_name};")
    except Exception:
        try:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
        finally:
            try:
                con.execute(f"RELEASE SAVEPOINT {sp_name};")
            except Exception:
                pass
        raise


def _now_year(tz_name: str = "Europe/Madrid") -> int:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Madrid")
    return datetime.now(tz).year


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;",
        (name,),
    ).fetchone()
    return row is not None


def _columns(con: sqlite3.Connection, table: str) -> Set[str]:
    rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    cols: Set[str] = set()
    for r in rows:
        name = r["name"] if isinstance(r, sqlite3.Row) else r[1]
        cols.add(str(name))
    return cols


def ensure_counter_schema(con: sqlite3.Connection, *, tz_name: str = "Europe/Madrid") -> None:
    """
    Crea/migra la tabla tenant_counters en la AUTH DB.
    Independiente de backend.db (sin imports cruzados).
    """
    con.execute("PRAGMA foreign_keys=ON;")

    if not _table_exists(con, "tenant_counters"):
        with _write_tx(con):
            con.execute(
                """
                CREATE TABLE tenant_counters (
                    tenant_id TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    ultimo_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, year)
                );
                """
            )
        return

    # Migración legacy: si falta la columna year, recrea y copia
    if "year" not in _columns(con, "tenant_counters"):
        year = _now_year(tz_name)
        with _write_tx(con):
            con.execute("ALTER TABLE tenant_counters RENAME TO tenant_counters_legacy;")
            con.execute(
                """
                CREATE TABLE tenant_counters (
                    tenant_id TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    ultimo_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, year)
                );
                """
            )
            con.execute(
                """
                INSERT OR IGNORE INTO tenant_counters(tenant_id, year, ultimo_id, updated_at)
                SELECT tenant_id, ?, ultimo_id, updated_at
                FROM tenant_counters_legacy;
                """,
                (int(year),),
            )
            # Si quieres, puedes limpiar legacy, pero no es obligatorio:
            # con.execute("DROP TABLE tenant_counters_legacy;")


def get_next_counter(
    con: sqlite3.Connection,
    tenant_id: str,
    *,
    year: int,
    start_at: int = 1,
    tz_name: str = "Europe/Madrid",
) -> int:
    """
    ✅ IDEMPOTENTE: incrementa el contador por (tenant_id, year) de forma atómica.

    NOTA:
    - La rama principal usa RETURNING (SQLite >= 3.35).
    - Incluye fallback compatible si RETURNING no está disponible.
    """
    ensure_counter_schema(con, tz_name=tz_name)

    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id vacío en get_next_counter()")

    year_i = int(year)
    start_i = max(1, int(start_at))
    floor = start_i - 1

    # Intento 1: RETURNING (rápido, atómico, SQLite moderno)
    try:
        with _write_tx(con):
            row = con.execute(
                """
                INSERT INTO tenant_counters(tenant_id, year, ultimo_id)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id, year) DO UPDATE SET
                    ultimo_id = CASE
                        WHEN tenant_counters.ultimo_id < ? THEN ? + 1
                        ELSE tenant_counters.ultimo_id + 1
                    END,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING ultimo_id;
                """,
                (tenant_id, year_i, start_i, floor, floor),
            ).fetchone()

        if row is None:
            raise RuntimeError("Fallo crítico en contador: RETURNING no devolvió datos.")

        return int(row["ultimo_id"] if isinstance(row, sqlite3.Row) else row[0])

    except sqlite3.OperationalError as e:
        # Fallback: SQLite viejo sin RETURNING (>=3.35)
        msg = str(e).lower()
        if "returning" not in msg:
            raise

    # Intento 2: Fallback sin RETURNING (100% compatible)
    with _write_tx(con):
        # 1) asegurar fila
        con.execute(
            """
            INSERT OR IGNORE INTO tenant_counters(tenant_id, year, ultimo_id)
            VALUES (?, ?, ?);
            """,
            (tenant_id, year_i, start_i),
        )

        # 2) incrementar (respetando start_at)
        con.execute(
            """
            UPDATE tenant_counters
            SET
              ultimo_id = CASE
                WHEN ultimo_id < ? THEN ? + 1
                ELSE ultimo_id + 1
              END,
              updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id=? AND year=?;
            """,
            (floor, floor, tenant_id, year_i),
        )

        # 3) leer valor final
        row2 = con.execute(
            "SELECT ultimo_id FROM tenant_counters WHERE tenant_id=? AND year=? LIMIT 1;",
            (tenant_id, year_i),
        ).fetchone()

    if row2 is None:
        raise RuntimeError("Fallo crítico en contador (fallback): no se pudo leer ultimo_id.")

    return int(row2["ultimo_id"] if isinstance(row2, sqlite3.Row) else row2[0])


def set_counter(con: sqlite3.Connection, tenant_id: str, *, year: int, ultimo_id: int) -> None:
    """
    Set manual (admin/repair). Independiente de backend.db.
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id vacío en set_counter()")

    ensure_counter_schema(con)

    with _write_tx(con):
        con.execute(
            """
            INSERT INTO tenant_counters(tenant_id, year, ultimo_id)
            VALUES (?, ?, ?)
            ON CONFLICT(tenant_id, year) DO UPDATE SET
                ultimo_id=excluded.ultimo_id,
                updated_at=CURRENT_TIMESTAMP;
            """,
            (tenant_id, int(year), int(ultimo_id)),
        )