# backend/db_explorer.py
from __future__ import annotations

import re
import sqlite3
from typing import List, Optional, Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(s: str) -> bool:
    return bool(s and EMAIL_RE.match(s.strip()))


def clean_tax_id(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).strip().upper()
    s = re.sub(r"[\s\-_/\.]", "", s)
    return s


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    r = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(r)


def columns(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({table});").fetchall()
        return [r["name"] if isinstance(r, sqlite3.Row) else r[1] for r in rows]
    except Exception:
        return []


def pick(cols: List[str], *candidates: str) -> Optional[str]:
    s = set(cols)
    for c in candidates:
        if c in s:
            return c
    return None


def search_donors(con: sqlite3.Connection, *, tenant_id: str, query: str, limit: int = 50) -> List[dict]:
    """
    Búsqueda rápida (v6-safe) + flexible (mejor UX):
    ✅ Mantiene el camino index-friendly:
       - si el usuario mete un CIF/NIF/NIE "real" -> busca por igualdad en cifnif_norm y en histórico.
    ✅ Añade camino flexible:
       - LIKE en nombre/email
       - LIKE también en cifnif_raw y cifnif_norm (como texto) para fragmentos ("A46", "0038", etc.)
    ✅ tenant-safe
    """
    if not table_exists(con, "donors"):
        return []

    q = (query or "").strip()
    if not q:
        return []

    cols = columns(con, "donors")
    col_id = pick(cols, "id")
    col_tenant = pick(cols, "tenant_id")

    # v6: puede existir cifnif_raw y/o tax_id / cif / etc.
    col_cif = pick(cols, "cifnif_raw", "cifnif", "tax_id", "cif")
    col_cif_norm = pick(cols, "cifnif_norm", "tax_id_norm", "cifnif")

    col_name = pick(cols, "nombre", "name")
    col_email = pick(cols, "email")
    col_type = pick(cols, "donor_type", "type")
    col_anon = pick(cols, "is_anonymous", "anonymous")

    # guard-clause: mínimo viable para devolver "tarjetas" de donante
    if not (col_id and col_tenant and col_name):
        return []

    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return []

    limit = max(1, min(int(limit or 50), 500))

    # Inputs
    cif_norm = clean_tax_id(q)              # normalizado (sin separadores)
    q_like = f"%{q}%"
    cif_like = f"%{cif_norm}%" if cif_norm else ""

    # Alias seguro
    cif_expr = f"d.{col_cif}" if col_cif else (f"d.{col_cif_norm}" if col_cif_norm else "NULL")

    select_sql = (
        f"SELECT d.{col_id} as id, {cif_expr} as cifnif, d.{col_name} as nombre, "
        f"{('d.' + col_email + ' as email') if col_email else 'NULL as email'}, "
        f"{('d.' + col_type + ' as donor_type') if col_type else 'NULL as donor_type'}, "
        f"{('d.' + col_anon + ' as is_anonymous') if col_anon else '0 as is_anonymous'} "
        f"FROM donors d "
    )

    has_hist = table_exists(con, "donor_tax_id_history")

    # Heurística: si el query parece "id fiscal" (tras limpiar) usamos igualdad como P0.
    # (España: 8-9 suele ser CIF/NIF/NIE; pero dejamos rango amplio para no excluir)
    looks_like_tax_id = bool(cif_norm) and (len(cif_norm) >= 7)

    where_parts: List[str] = []
    params: List[Any] = []

    # Tenant siempre
    where_parts.append(f"d.{col_tenant}=?")
    params.append(tenant_id)

    # --- Bloque de condiciones ---
    conds: List[str] = []

    # 1) Match fuerte por cifnif_norm (=) si existe y parece tax_id real
    if col_cif_norm and looks_like_tax_id:
        conds.append(f"d.{col_cif_norm} = ?")
        params.append(cif_norm)

        # histórico exacto (si existe)
        if has_hist:
            # Nota: asumimos columnas estándar del historial; si cambian, esto no rompe,
            # porque solo se evalúa si la tabla existe. Si tu tabla tiene otros nombres,
            # me lo dices y lo hago v6-safe también.
            conds.append(
                f"""EXISTS (
                    SELECT 1 FROM donor_tax_id_history h
                    WHERE h.tenant_id = ?
                      AND h.donor_id = d.{col_id}
                      AND h.old_cifnif_norm = ?
                )"""
            )
            params.extend([tenant_id, cif_norm])

    # 2) Búsqueda flexible por texto (nombre/email) siempre
    conds.append(f"d.{col_name} LIKE ?")
    params.append(q_like)

    if col_email:
        conds.append(f"d.{col_email} LIKE ?")
        params.append(q_like)

    # 3) Búsqueda flexible por fragmentos de CIF
    #    - si el usuario escribe "A46" o "0038" también debería encontrar
    if col_cif:
        conds.append(f"d.{col_cif} LIKE ?")
        params.append(q_like)

    # cifnif_norm LIKE %cif_norm% (si hay cif_norm y columna)
    if col_cif_norm and cif_like:
        conds.append(f"d.{col_cif_norm} LIKE ?")
        params.append(cif_like)

        if has_hist and cif_like:
            # histórico por fragmento (mejora UX: si recuerdan parte del CIF antiguo)
            # Igual que antes: asumimos nombres estándar del histórico.
            conds.append(
                f"""EXISTS (
                    SELECT 1 FROM donor_tax_id_history h
                    WHERE h.tenant_id = ?
                      AND h.donor_id = d.{col_id}
                      AND h.old_cifnif_norm LIKE ?
                )"""
            )
            params.extend([tenant_id, cif_like])

    # Armado final
    sql = (
        select_sql
        + " WHERE "
        + " AND ".join(where_parts)
        + " AND ("
        + " OR ".join(conds)
        + ") "
        + f"ORDER BY d.{col_name} ASC "
        + "LIMIT ?"
    )
    params.append(int(limit))

    try:
        rows = con.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        # fallback ultra-safe: si algo en el histórico falla por schema distinto,
        # repetimos sin usar EXISTS(historico)
        conds2: List[str] = []
        params2: List[Any] = [tenant_id]

        if col_cif_norm and looks_like_tax_id:
            conds2.append(f"d.{col_cif_norm} = ?")
            params2.append(cif_norm)

        conds2.append(f"d.{col_name} LIKE ?")
        params2.append(q_like)

        if col_email:
            conds2.append(f"d.{col_email} LIKE ?")
            params2.append(q_like)

        if col_cif:
            conds2.append(f"d.{col_cif} LIKE ?")
            params2.append(q_like)

        if col_cif_norm and cif_like:
            conds2.append(f"d.{col_cif_norm} LIKE ?")
            params2.append(cif_like)

        sql2 = (
            select_sql
            + f" WHERE d.{col_tenant}=? AND ("
            + " OR ".join(conds2)
            + f") ORDER BY d.{col_name} ASC LIMIT ?"
        )
        params2.append(int(limit))
        rows = con.execute(sql2, tuple(params2)).fetchall()
        return [dict(r) for r in rows]


def donor_donations(con: sqlite3.Connection, *, tenant_id: str, donor_id: int, limit: int = 200) -> List[dict]:
    if not table_exists(con, "donations"):
        return []

    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return []

    limit = max(1, min(int(limit or 200), 2000))

    cols = columns(con, "donations")
    col_id = pick(cols, "id")
    col_tenant = pick(cols, "tenant_id")
    col_donor = pick(cols, "donor_id")
    col_fecha = pick(cols, "fecha", "date")
    col_tipo = pick(cols, "tipo", "type")
    col_importe = pick(cols, "importe", "amount")
    col_kg = pick(cols, "kg", "kilos")
    col_import_id = pick(cols, "import_id")
    col_row_index = pick(cols, "row_index")

    if not (col_id and col_tenant and col_donor and col_fecha and col_tipo):
        return []

    sql = (
        f"SELECT {col_id} as id, {col_fecha} as fecha, {col_tipo} as tipo, "
        f"{(col_importe + ' as importe') if col_importe else 'NULL as importe'}, "
        f"{(col_kg + ' as kg') if col_kg else 'NULL as kg'}, "
        f"{(col_import_id + ' as import_id') if col_import_id else 'NULL as import_id'}, "
        f"{(col_row_index + ' as row_index') if col_row_index else 'NULL as row_index'} "
        f"FROM donations WHERE {col_tenant}=? AND {col_donor}=? "
        f"ORDER BY {col_id} DESC LIMIT ?"
    )
    rows = con.execute(sql, (tenant_id, int(donor_id), int(limit))).fetchall()
    return [dict(r) for r in rows]


def donor_certificates(con: sqlite3.Connection, *, tenant_id: str, donor_id: int, limit: int = 200) -> List[dict]:
    if not table_exists(con, "certificates"):
        return []

    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return []

    limit = max(1, min(int(limit or 200), 2000))

    cols = columns(con, "certificates")
    col_id = pick(cols, "id")
    col_tenant = pick(cols, "tenant_id")
    col_donor = pick(cols, "donor_id")
    col_num = pick(cols, "numerocertificado", "numero")
    col_hash = pick(cols, "hash", "hash_")
    col_tipo = pick(cols, "tipo")
    col_emision = pick(cols, "fecha_emision", "emitted_at", "fechaemision")
    col_status_cert = pick(cols, "status_certificado")
    col_status_carta = pick(cols, "status_carta")
    col_cert_path = pick(cols, "cert_pdf_path")
    col_carta_path = pick(cols, "carta_pdf_path")
    col_email_to = pick(cols, "email_to")
    col_emailed_at = pick(cols, "emailed_at", "emailed_on")

    if not (col_id and col_tenant and col_donor and col_num and col_hash):
        return []

    sql = (
        f"SELECT {col_id} as id, {col_num} as numerocertificado, {col_hash} as hash, "
        f"{(col_tipo + ' as tipo') if col_tipo else 'NULL as tipo'}, "
        f"{(col_emision + ' as fecha_emision') if col_emision else 'NULL as fecha_emision'}, "
        f"{(col_status_cert + ' as status_certificado') if col_status_cert else 'NULL as status_certificado'}, "
        f"{(col_status_carta + ' as status_carta') if col_status_carta else 'NULL as status_carta'}, "
        f"{(col_cert_path + ' as cert_pdf_path') if col_cert_path else 'NULL as cert_pdf_path'}, "
        f"{(col_carta_path + ' as carta_pdf_path') if col_carta_path else 'NULL as carta_pdf_path'}, "
        f"{(col_email_to + ' as email_to') if col_email_to else 'NULL as email_to'}, "
        f"{(col_emailed_at + ' as emailed_at') if col_emailed_at else 'NULL as emailed_at'} "
        f"FROM certificates WHERE {col_tenant}=? AND {col_donor}=? "
        f"ORDER BY {col_id} DESC LIMIT ?"
    )
    rows = con.execute(sql, (tenant_id, int(donor_id), int(limit))).fetchall()
    return [dict(r) for r in rows]


def certificate_donations(con: sqlite3.Connection, *, certificate_id: int) -> List[dict]:
    """
    Devuelve donaciones asociadas a un certificado (vía tabla intermedia certificate_items).
    """
    if not table_exists(con, "certificate_items") or not table_exists(con, "donations"):
        return []

    cols_ci = columns(con, "certificate_items")
    col_ci_cert = pick(cols_ci, "certificate_id")
    col_ci_don = pick(cols_ci, "donation_id")
    if not (col_ci_cert and col_ci_don):
        return []

    cols_d = columns(con, "donations")
    col_d_id = pick(cols_d, "id")
    col_d_fecha = pick(cols_d, "fecha", "date")
    col_d_tipo = pick(cols_d, "tipo", "type")
    col_d_importe = pick(cols_d, "importe", "amount")
    col_d_kg = pick(cols_d, "kg", "kilos")

    if not (col_d_id and col_d_fecha and col_d_tipo):
        return []

    sql = (
        f"SELECT d.{col_d_id} as id, d.{col_d_fecha} as fecha, d.{col_d_tipo} as tipo, "
        f"{('d.' + col_d_importe + ' as importe') if col_d_importe else 'NULL as importe'}, "
        f"{('d.' + col_d_kg + ' as kg') if col_d_kg else 'NULL as kg'} "
        f"FROM certificate_items ci "
        f"JOIN donations d ON d.{col_d_id} = ci.{col_ci_don} "
        f"WHERE ci.{col_ci_cert} = ? "
        f"ORDER BY d.{col_d_id} DESC"
    )
    rows = con.execute(sql, (int(certificate_id),)).fetchall()
    return [dict(r) for r in rows]