# backend/tenant_settings.py
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import PurePosixPath
from typing import Any, Dict

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
URL_RE = re.compile(r"^[a-zA-Z]+://")


def ensure_settings_schema(con: sqlite3.Connection) -> None:
    """
    tenant_settings: 1 fila por tenant_id (FK a tenants.id en app_db).
    """
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_settings (
            tenant_id TEXT PRIMARY KEY,
            settings_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
        );
        """
    )
    con.commit()


def _default_settings() -> Dict[str, Any]:
    """
    ✅ Compatible con generator (SaaS):
    - entidad / firmas / textoslegales / textos_email / branding
    - features (flags por tenant)
    - storage (NO editable por tenant; coherencia con config.json)
    """
    return {
        "entidad": {
            "nombre": "",
            "cif": "",
            "direccion": "",
            "ciudad": "",
            "email": "",
            "web": "",
            "telefono": "",
        },
        "firmas": {
            "nombre": "",
            "cargo": "",
        },
        "textos_email": {
            "firma": "",
            "dinero": "",
            "especie": "",
        },
        "textoslegales": {
            "certificadodinero": "",
            "certificadoespecie": "",
        },
        "branding": {
            # relativo (ej: branding/<slug>/logo.png)
            "logo_path": "",
            "color_principal": "#3B468C",
            "logo_filename": "",
        },
        "features": {
            "validate_spanish_tax_id": True,
        },
        "storage": {
            "tmp_out_dir": "backend/tmp_out",
            "tmp_out_individual_dir": "backend/tmp_out_individual",
            "branding_dir": "branding",
        },
    }


def _deep_merge(base: Any, override: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_defaults_with(data: Any) -> Dict[str, Any]:
    base = _default_settings()
    if isinstance(data, dict):
        return _deep_merge(base, data)
    return base


def get_tenant_settings(con: sqlite3.Connection, tenant_id: str) -> Dict[str, Any]:
    """
    Devuelve settings del tenant con defaults aplicados.
    Si no existe fila, devuelve defaults (sin escribir a DB).
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return _default_settings()

    ensure_settings_schema(con)

    row = con.execute(
        "SELECT settings_json FROM tenant_settings WHERE tenant_id=? LIMIT 1;",
        (tenant_id,),
    ).fetchone()

    if not row:
        return _default_settings()

    raw = row["settings_json"] if isinstance(row, sqlite3.Row) else row[0]

    try:
        data = json.loads(raw) if raw else {}
        return _merge_defaults_with(data)
    except Exception:
        # si está corrupto, devolvemos defaults (sin tocar DB)
        return _default_settings()


def _is_unsafe_rel_path(lp: str) -> bool:
    """
    Bloquea:
    - rutas absolutas unix (/), home (~), UNC (//)
    - unidad windows (C:/)
    - URLs
    - '..' en partes
    """
    if not lp:
        return False

    lp = lp.replace("\\", "/").strip()

    if lp.startswith("/") or lp.startswith("~") or lp.startswith("//"):
        return True
    if bool(re.match(r"^[A-Za-z]:/", lp)):
        return True
    if bool(URL_RE.match(lp)):
        return True

    p = PurePosixPath(lp)
    if ".." in p.parts:
        return True

    return False


def _normalize_logo_path(lp: str, *, branding_root: str) -> str:
    """
    ✅ Política SaaS anti-leak:
    - solo rutas RELATIVAS
    - sin escapes, sin URLs
    - solo dentro de branding_root (por defecto 'branding')
    - solo extensiones de imagen
    """
    lp = (lp or "").strip().replace("\\", "/")
    if not lp:
        return ""

    if _is_unsafe_rel_path(lp):
        return ""

    allowed_ext = {".png", ".jpg", ".jpeg", ".webp"}
    ext = PurePosixPath(lp).suffix.lower()
    if ext not in allowed_ext:
        return ""

    root = (branding_root or "branding").strip().replace("\\", "/").strip("/")
    if root:
        if not lp.startswith(root + "/"):
            return ""

    return lp


def save_tenant_settings(con: sqlite3.Connection, tenant_id: str, settings: Dict[str, Any]) -> None:
    """
    Guarda settings por tenant:
    - Merge controlado solo de secciones permitidas.
    - 'storage' congelado (NO editable por tenant).
    - Hardening de branding.logo_path + validación de color.
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("El tenant_id es obligatorio.")

    ensure_settings_schema(con)

    clean = _default_settings()

    allowed_sections = {"entidad", "firmas", "textos_email", "textoslegales", "branding", "features"}
    if isinstance(settings, dict):
        for sec in allowed_sections:
            if isinstance(settings.get(sec), dict):
                clean[sec] = _deep_merge(clean.get(sec, {}), settings.get(sec, {}))

    # ✅ Storage congelado (rutas internas)
    clean["storage"] = _default_settings()["storage"]

    # ---- VALIDACIÓN BRANDING ----
    branding = clean.get("branding") if isinstance(clean.get("branding"), dict) else {}
    cp = str(branding.get("color_principal", "")).strip()
    branding["color_principal"] = cp if HEX_COLOR_RE.match(cp) else "#3B468C"

    branding_root = str(clean.get("storage", {}).get("branding_dir", "branding")).strip() or "branding"

    lp = str(branding.get("logo_path", "")).strip()
    branding["logo_path"] = _normalize_logo_path(lp, branding_root=branding_root)

    lf = str(branding.get("logo_filename", "")).strip()
    branding["logo_filename"] = lf

    clean["branding"] = branding

    # ---- VALIDACIÓN FEATURES ----
    features = clean.get("features") if isinstance(clean.get("features"), dict) else {}
    features["validate_spanish_tax_id"] = bool(features.get("validate_spanish_tax_id", True))
    clean["features"] = features

    payload = json.dumps(clean, ensure_ascii=False)

    con.execute(
        """
        INSERT INTO tenant_settings(tenant_id, settings_json)
        VALUES (?, ?)
        ON CONFLICT(tenant_id) DO UPDATE SET
            settings_json=excluded.settings_json,
            updated_at=CURRENT_TIMESTAMP;
        """,
        (tenant_id, payload),
    )
    con.commit()