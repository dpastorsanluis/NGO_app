# backend/merge.py
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Set


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    r = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;",
        (name,),
    ).fetchone()
    return bool(r)


def _columns(con: sqlite3.Connection, table: str) -> Set[str]:
    rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    cols: Set[str] = set()
    for r in rows:
        name = r["name"] if isinstance(r, sqlite3.Row) else r[1]
        cols.add(str(name))
    return cols


def merge_donors_v6(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    winner_donor_id: int,
    loser_donor_id: int,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    ✅ "Búnker" mínimo (v6):
    - SAVEPOINT (_write_tx) => anidable (estándar del proyecto).
    - Enforce tenant (fail-closed): NO fallback a 'default'.
    - Actualiza updated_at en donations/certificates/donors si existe.
    - Reasigna parent_donor_id -> winner si existe.
    - Bloquea merges con DONANTE ANÓNIMO si db.py expone ANON_CIF.

    Devuelve contadores de filas movidas y si borró el loser.
    """
    tenant_id = str(tenant_id).strip()
    if not tenant_id:
        raise ValueError("tenant_id requerido")

    winner = int(winner_donor_id)
    loser = int(loser_donor_id)
    if winner <= 0 or loser <= 0:
        raise ValueError("winner_donor_id y loser_donor_id deben ser > 0")
    if winner == loser:
        raise ValueError("winner_donor_id y loser_donor_id deben ser distintos.")

    # tablas mínimas
    for t in ("donors", "donations", "certificates"):
        if not _table_exists(con, t):
            raise ValueError(f"No existe tabla {t}.")

    # Import local: tx + now
    from backend.db import _write_tx, utc_now_str  # type: ignore

    now = utc_now_str()

    # Validar existencia + tenant (y coger cifnif_norm para anónimo)
    w = con.execute(
        "SELECT id, tenant_id, cifnif_norm FROM donors WHERE tenant_id=? AND id=? LIMIT 1;",
        (tenant_id, winner),
    ).fetchone()
    l = con.execute(
        "SELECT id, tenant_id, cifnif_norm FROM donors WHERE tenant_id=? AND id=? LIMIT 1;",
        (tenant_id, loser),
    ).fetchone()
    if not w or not l:
        raise ValueError("Donor winner/loser no existe en este tenant.")

    # Bloquear merge con donante anónimo si existe ANON_CIF
    try:
        from backend.db import ANON_CIF  # type: ignore
    except ImportError:
        ANON_CIF = None  # type: ignore

    if ANON_CIF:
        if (w["cifnif_norm"] == ANON_CIF) or (l["cifnif_norm"] == ANON_CIF):
            raise ValueError("No se puede mergear con DONANTE ANÓNIMO.")

    # Contadores (antes)
    donations_count = con.execute(
        "SELECT COUNT(1) AS c FROM donations WHERE tenant_id=? AND donor_id=?;",
        (tenant_id, loser),
    ).fetchone()
    certs_count = con.execute(
        "SELECT COUNT(1) AS c FROM certificates WHERE tenant_id=? AND donor_id=?;",
        (tenant_id, loser),
    ).fetchone()

    n_don = int((donations_count["c"] if isinstance(donations_count, sqlite3.Row) else donations_count[0]) or 0)
    n_cert = int((certs_count["c"] if isinstance(certs_count, sqlite3.Row) else certs_count[0]) or 0)

    cols_don = _columns(con, "donations")
    cols_cert = _columns(con, "certificates")
    cols_donors = _columns(con, "donors")

    moved_children = 0
    if "parent_donor_id" in cols_donors:
        ch = con.execute(
            "SELECT COUNT(1) AS c FROM donors WHERE tenant_id=? AND parent_donor_id=?;",
            (tenant_id, loser),
        ).fetchone()
        moved_children = int((ch["c"] if isinstance(ch, sqlite3.Row) else ch[0]) or 0)

    if dry_run:
        return {
            "dry_run": True,
            "tenant_id": tenant_id,
            "winner_donor_id": winner,
            "loser_donor_id": loser,
            "move": {
                "donations": n_don,
                "certificates": n_cert,
                "children_parent": int(moved_children),
            },
            "deleted_loser": False,
        }

    # TX búnker (anidable)
    with _write_tx(con):
        # donations -> winner (+updated_at si existe)
        if "updated_at" in cols_don:
            con.execute(
                "UPDATE donations SET donor_id=?, updated_at=? WHERE tenant_id=? AND donor_id=?;",
                (winner, now, tenant_id, loser),
            )
        else:
            con.execute(
                "UPDATE donations SET donor_id=? WHERE tenant_id=? AND donor_id=?;",
                (winner, tenant_id, loser),
            )

        # certificates -> winner (+updated_at si existe)
        if "updated_at" in cols_cert:
            con.execute(
                "UPDATE certificates SET donor_id=?, updated_at=? WHERE tenant_id=? AND donor_id=?;",
                (winner, now, tenant_id, loser),
            )
        else:
            con.execute(
                "UPDATE certificates SET donor_id=? WHERE tenant_id=? AND donor_id=?;",
                (winner, tenant_id, loser),
            )

        # hijos: parent_donor_id loser -> winner (+updated_at si existe)
        if "parent_donor_id" in cols_donors:
            if "updated_at" in cols_donors:
                con.execute(
                    "UPDATE donors SET parent_donor_id=?, updated_at=? WHERE tenant_id=? AND parent_donor_id=?;",
                    (winner, now, tenant_id, loser),
                )
            else:
                con.execute(
                    "UPDATE donors SET parent_donor_id=? WHERE tenant_id=? AND parent_donor_id=?;",
                    (winner, tenant_id, loser),
                )

        # borrar loser
        con.execute(
            "DELETE FROM donors WHERE tenant_id=? AND id=?;",
            (tenant_id, loser),
        )

    return {
        "dry_run": False,
        "tenant_id": tenant_id,
        "winner_donor_id": winner,
        "loser_donor_id": loser,
        "move": {
            "donations": n_don,
            "certificates": n_cert,
            "children_parent": int(moved_children),
        },
        "deleted_loser": True,
    }