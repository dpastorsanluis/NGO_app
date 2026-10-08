# backend/data_health.py
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

# ✅ Estándar del proyecto + timestamps UTC
from backend.db import (
    ANON_CIF,
    assert_business_schema_once,
    utc_now_str,
)

# ==========================================================
# Utils (sin db_explorer; 100% coherente)
# ==========================================================


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;",
        (name,),
    ).fetchone()
    return row is not None


def _columns(con: sqlite3.Connection, table: str) -> Set[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    except Exception:
        return set()
    out: Set[str] = set()
    for r in rows:
        name = r["name"] if isinstance(r, sqlite3.Row) else r[1]
        out.add(str(name))
    return out


def _pick(cols: Set[str], *candidates: str) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


def _require_tables(con: sqlite3.Connection, names: Sequence[str]) -> bool:
    return all(_table_exists(con, n) for n in names)


# ==========================================================
# 0) KPIs de Sanidad (para UI proactiva)
# ==========================================================


def check_data_health(con: sqlite3.Connection, *, tenant_id: str) -> Dict[str, Any]:
    """
    Scanner v6 (solo lectura + queries seguras):
      - total_donors / total_certificates / total_donations
      - duplicate_cif_groups / duplicate_cif_donors
      - zombie_certificates
      - mismatched_certificates (solo si existen totales guardados; en tu v6 normal => None/0)
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id requerido")

    # Fail-closed
    assert_business_schema_once(con)

    out: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "ts_utc": _now_utc_iso(),
        "total_donors": 0,
        "total_donations": 0,
        "total_certificates": 0,
        "duplicate_cif_groups": 0,
        "duplicate_cif_donors": 0,
        "zombie_certificates": 0,
        "mismatched_certificates": None,
        "notes": [],
    }

    # Totales
    out["total_donors"] = int(
        con.execute("SELECT COUNT(1) FROM donors WHERE tenant_id=?;", (tenant_id,)).fetchone()[0]
    )
    out["total_donations"] = int(
        con.execute("SELECT COUNT(1) FROM donations WHERE tenant_id=?;", (tenant_id,)).fetchone()[0]
    )
    out["total_certificates"] = int(
        con.execute("SELECT COUNT(1) FROM certificates WHERE tenant_id=?;", (tenant_id,)).fetchone()[0]
    )

    # Duplicados por CIF/NIF normalizado (v6: cifnif_norm). Excluye anónimos.
    try:
        grp = con.execute(
            """
            SELECT COUNT(1) FROM (
                SELECT cifnif_norm
                FROM donors
                WHERE tenant_id=?
                  AND cifnif_norm IS NOT NULL
                  AND TRIM(cifnif_norm) <> ''
                  AND cifnif_norm <> ?
                GROUP BY cifnif_norm
                HAVING COUNT(1) > 1
            );
            """,
            (tenant_id, ANON_CIF),
        ).fetchone()
        out["duplicate_cif_groups"] = int(grp[0] if grp else 0)

        donors = con.execute(
            """
            SELECT COUNT(1)
            FROM donors d
            WHERE d.tenant_id=?
              AND d.cifnif_norm IN (
                  SELECT cifnif_norm
                  FROM donors
                  WHERE tenant_id=?
                    AND cifnif_norm IS NOT NULL
                    AND TRIM(cifnif_norm) <> ''
                    AND cifnif_norm <> ?
                  GROUP BY cifnif_norm
                  HAVING COUNT(1) > 1
              );
            """,
            (tenant_id, tenant_id, ANON_CIF),
        ).fetchone()
        out["duplicate_cif_donors"] = int(donors[0] if donors else 0)
    except Exception as e:
        out["notes"].append(f"No pude calcular duplicados por CIF: {e}")

    # Zombis (cert activo pero contiene donación anulada)
    try:
        out["zombie_certificates"] = int(len(find_certificates_zombies(con, tenant_id=tenant_id, limit=100_000)))
    except Exception as e:
        out["notes"].append(f"No pude calcular zombis: {e}")

    # Descuadres (solo si existen columnas totales guardadas)
    try:
        mism = find_certificates_mismatched_totals(con, tenant_id=tenant_id, limit=100_000, tol=0.01)
        out["mismatched_certificates"] = int(len(mism))
    except Exception as e:
        out["notes"].append(f"No pude calcular descuadres: {e}")

    return out


# ==========================================================
# 2a) Duplicados por CIF (v6 real)
# ==========================================================


def find_duplicate_donors_by_cif(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    limit_groups: int = 200,
) -> List[Dict[str, Any]]:
    """
    Devuelve grupos por cifnif_norm con >1 donante (excluye ANON_CIF).
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id requerido")

    assert_business_schema_once(con)
    limit_groups = max(1, min(int(limit_groups or 200), 2000))

    sql = """
    WITH dup_keys AS (
        SELECT cifnif_norm
        FROM donors
        WHERE tenant_id=?
          AND cifnif_norm IS NOT NULL
          AND TRIM(cifnif_norm) <> ''
          AND cifnif_norm <> ?
        GROUP BY cifnif_norm
        HAVING COUNT(1) > 1
        LIMIT ?
    )
    SELECT
        d.cifnif_norm AS key_norm,
        d.id AS id,
        d.nombre AS nombre,
        d.cifnif_raw AS cifnif,
        d.email AS email,
        d.updated_at AS updated_at
    FROM donors d
    WHERE d.tenant_id=?
      AND d.cifnif_norm IN (SELECT cifnif_norm FROM dup_keys)
    ORDER BY d.cifnif_norm, COALESCE(d.nombre,'') ASC, d.id ASC;
    """

    rows = con.execute(sql, (tenant_id, ANON_CIF, limit_groups, tenant_id)).fetchall()

    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        k = str(r["key_norm"] or "").strip()
        if not k:
            continue
        if k not in groups:
            groups[k] = {"key": k, "count": 0, "donors": []}
        groups[k]["donors"].append(dict(r))
        groups[k]["count"] += 1

    return list(groups.values())


# ==========================================================
# 2b) Duplicados por NOMBRE (v6 real con nombre_norm)
# ==========================================================


def find_duplicate_donors_by_name(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    limit_groups: int = 200,
    min_len: int = 6,
) -> List[Dict[str, Any]]:
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id requerido")

    assert_business_schema_once(con)

    limit_groups = max(1, min(int(limit_groups or 200), 2000))
    min_len = max(2, min(int(min_len or 6), 50))

    sql = """
    WITH base AS (
        SELECT
            nombre_norm AS key_name,
            id,
            nombre,
            cifnif_raw AS cifnif,
            email,
            cifnif_norm
        FROM donors
        WHERE tenant_id=?
          AND nombre_norm IS NOT NULL
          AND TRIM(nombre_norm) <> ''
          AND LENGTH(nombre_norm) >= ?
          AND cifnif_norm <> ?
    ),
    name_dups AS (
        SELECT key_name
        FROM base
        GROUP BY key_name
        HAVING COUNT(1) > 1
        LIMIT ?
    )
    SELECT
        b.key_name,
        b.id, b.nombre, b.cifnif, b.email, b.cifnif_norm
    FROM base b
    WHERE b.key_name IN (SELECT key_name FROM name_dups)
    ORDER BY b.key_name, COALESCE(b.nombre,'') ASC, b.id ASC;
    """

    rows = con.execute(sql, (tenant_id, min_len, ANON_CIF, limit_groups)).fetchall()

    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        k = str(r["key_name"] or "").strip()
        if not k:
            continue
        if k not in groups:
            groups[k] = {"key": k, "count": 0, "donors": []}
        groups[k]["donors"].append(dict(r))
        groups[k]["count"] += 1

    return list(groups.values())


# ==========================================================
# 3) Certificados Zombi (v6: cert activo + donation anulada)
# ==========================================================


def find_certificates_zombies(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id requerido")

    assert_business_schema_once(con)
    limit = max(1, min(int(limit or 200), 200_000))

    if not _require_tables(con, ("certificates", "certificate_items", "donations")):
        return []

    sql = """
    SELECT DISTINCT
        c.id AS cert_id,
        c.numerocertificado AS numerocertificado,
        c.year AS year,
        c.hash AS hash,
        c.status_certificado AS status_certificado,
        c.is_void AS cert_is_void,
        d.id AS donation_id,
        d.is_void AS donation_is_void
    FROM certificates c
    JOIN certificate_items ci ON ci.certificate_id = c.id
    JOIN donations d ON d.id = ci.donation_id
    WHERE c.tenant_id=?
      AND c.is_void=0
      AND d.tenant_id=?
      AND d.is_void=1
    ORDER BY COALESCE(c.year, 0) DESC, c.id DESC
    LIMIT ?;
    """
    rows = con.execute(sql, (tenant_id, tenant_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ==========================================================
# 3b) Certificados descuadrados (solo si existen totales guardados)
# ==========================================================


def find_certificates_mismatched_totals(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    limit: int = 200,
    tol: float = 0.01,
) -> List[Dict[str, Any]]:
    """
    ✅ FIX P0: HAVING correcto:
      HAVING items > 0 AND (mismatch_eur OR mismatch_kg)
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id requerido")

    assert_business_schema_once(con)
    limit = max(1, min(int(limit or 200), 200_000))
    tol = float(tol or 0.01)

    if not _require_tables(con, ("certificates", "certificate_items", "donations")):
        return []

    ccols = _columns(con, "certificates")
    dcols = _columns(con, "donations")

    c_total_eur = _pick(ccols, "total_importe", "importe_total", "total_eur", "total_amount")
    c_total_kg = _pick(ccols, "total_kg", "kg_total", "total_kilos")

    if not (c_total_eur or c_total_kg):
        return []

    d_importe = _pick(dcols, "importe")
    d_kg = _pick(dcols, "kg")

    sum_importe_expr = f"SUM(COALESCE(d.{d_importe}, 0.0))" if d_importe else "0.0"
    sum_kg_expr = f"SUM(COALESCE(d.{d_kg}, 0.0))" if d_kg else "0.0"

    sel_total_eur = f"c.{c_total_eur} AS cert_total_eur," if c_total_eur else "NULL AS cert_total_eur,"
    sel_total_kg = f"c.{c_total_kg} AS cert_total_kg," if c_total_kg else "NULL AS cert_total_kg,"

    mismatch_exprs: List[str] = []
    tol_params: List[float] = []

    if c_total_eur and d_importe:
        mismatch_exprs.append(f"ABS(COALESCE(c.{c_total_eur},0.0) - {sum_importe_expr}) > ?")
        tol_params.append(tol)
    if c_total_kg and d_kg:
        mismatch_exprs.append(f"ABS(COALESCE(c.{c_total_kg},0.0) - {sum_kg_expr}) > ?")
        tol_params.append(tol)

    if not mismatch_exprs:
        return []

    sql = f"""
    SELECT
        c.id AS cert_id,
        c.numerocertificado AS numerocertificado,
        c.hash AS hash,
        {sel_total_eur}
        {sel_total_kg}
        {sum_importe_expr} AS sum_don_eur,
        {sum_kg_expr} AS sum_don_kg,
        COUNT(ci.donation_id) AS items
    FROM certificates c
    LEFT JOIN certificate_items ci ON ci.certificate_id = c.id
    LEFT JOIN donations d ON d.id = ci.donation_id
    WHERE c.tenant_id=?
    GROUP BY c.id
    HAVING COUNT(ci.donation_id) > 0
       AND ({' OR '.join(mismatch_exprs)})
    ORDER BY c.id DESC
    LIMIT ?;
    """

    params: List[Any] = [tenant_id]
    params.extend(tol_params)
    params.append(int(limit))

    rows = con.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


# ==========================================================
# 4) VOID seguro + forense (v6 real)
# ==========================================================


def void_certificate(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    certificate_id: int,
    reason: str,
    voided_by: Optional[str] = None,
) -> Dict[str, Any]:
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id requerido")

    cid = int(certificate_id)
    if cid <= 0:
        raise ValueError("certificate_id inválido")

    if not (reason and str(reason).strip()):
        raise ValueError("Motivo VOID obligatorio.")

    actor = (str(voided_by).strip() if voided_by else "unknown")
    now = utc_now_str()

    assert_business_schema_once(con)

    row = con.execute(
        "SELECT id FROM certificates WHERE tenant_id=? AND id=? LIMIT 1;",
        (tenant_id, cid),
    ).fetchone()
    if not row:
        raise ValueError("certificate_id no existe en este tenant")

    # 🛡️ usar búnker estándar del proyecto (savepoint)
    from backend.db import _write_tx  # import local

    with _write_tx(con):
        con.execute(
            """
            UPDATE certificates
            SET is_void=1,
                status_certificado='VOID',
                status_carta='VOID',
                void_reason=?,
                voided_at=?,
                voided_by=?,
                updated_at=?
            WHERE tenant_id=? AND id=?;
            """,
            (str(reason).strip(), now, actor, now, tenant_id, cid),
        )

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "certificate_id": cid,
        "voided_by": actor,
        "voided_at": now,
        "reason": str(reason).strip(),
    }


# ==========================================================
# 5) Optimización SQLite (realista)
# ==========================================================


def optimize_sqlite(con: sqlite3.Connection, *, vacuum: bool = False) -> Dict[str, Any]:
    """
    Mantenimiento SQLite seguro.
    - PRAGMA optimize + ANALYZE pueden ir en transacción.
    - VACUUM NO puede ir en transacción y puede fallar si hay otras conexiones.

    ✅ P1 "búnker": NO hacer COMMIT manual nunca.
    """
    assert_business_schema_once(con)

    from backend.db import _write_tx  # import local

    with _write_tx(con):
        con.execute("PRAGMA optimize;")
        con.execute("ANALYZE;")

    vacuum_ok: Optional[bool] = None
    vacuum_error: Optional[str] = None

    if vacuum:
        try:
            # ✅ no tocamos transacciones ajenas: si hay tx, no vacuumeamos
            if con.in_transaction:
                vacuum_ok = False
                vacuum_error = "in_transaction"
            else:
                con.execute("VACUUM;")
                vacuum_ok = True
        except sqlite3.OperationalError as e:
            vacuum_ok = False
            vacuum_error = str(e)

    return {
        "optimized": True,
        "vacuum_requested": bool(vacuum),
        "vacuum_ok": vacuum_ok,
        "vacuum_error": vacuum_error,
    }


# ==========================================================
# LEGACY: NO USAR (historial real está en db.py::update_donor_details)
# ==========================================================


def record_tax_id_change(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donor_id: int,
    old_cifnif: str,
    new_cifnif: str,
    changed_by: str,
    reason: str,
) -> Dict[str, Any]:
    # ✅ Fail-fast para evitar dobles escrituras / usos accidentales
    raise RuntimeError("LEGACY: no uses record_tax_id_change(); usa update_donor_details() en backend/db.py")

    # --- Si alguna vez lo reactivas, este era el cuerpo correcto: ---
    # tenant_id = (tenant_id or "").strip()
    # if not tenant_id:
    #     raise ValueError("tenant_id requerido")
    #
    # donor_id = int(donor_id)
    # if donor_id <= 0:
    #     raise ValueError("donor_id inválido")
    #
    # old_raw = (old_cifnif or "").strip()
    # new_raw = (new_cifnif or "").strip()
    #
    # from backend.db import norm_cifnif, _write_tx, _insert_tax_id_history
    #
    # old_norm = norm_cifnif(old_raw)
    # new_norm = norm_cifnif(new_raw)
    #
    # with _write_tx(con):
    #     _insert_tax_id_history(
    #         con,
    #         tenant_id=tenant_id,
    #         donor_id=donor_id,
    #         old_norm=old_norm,
    #         old_raw=old_raw,
    #         new_norm=new_norm,
    #         new_raw=new_raw,
    #         changed_by=(changed_by or "").strip(),
    #         reason=(reason or "").strip(),
    #     )
    #
    # return {
    #     "tenant_id": tenant_id,
    #     "donor_id": donor_id,
    #     "old_cifnif_norm": old_norm,
    #     "new_cifnif_norm": new_norm,
    #     "changed_by": (changed_by or "").strip(),
    #     "reason": (reason or "").strip(),
    #     "changed_at": _now_utc_iso(),
    # }