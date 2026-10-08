# app.py
from __future__ import annotations

import io
import inspect
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh

from backend.merge import merge_donors_v6

load_dotenv()

from backend.generator import CertSystem
from backend.mailer import smtp_config_ok, send_email_with_attachments
from backend.bulk_queue import BulkEmailQueue, QueueConfig

# ✅ NEGOCIO (biz.sqlite3)
from backend.db import (
    db_stats,
    list_recent_donors,
    list_recent_certificates,
    get_donor,
    is_anonymous_donor,
    top_donors_by_certificates,
    update_donor_details,
    # ✅ BLOQUEANTE: esquema negocio
    ensure_business_schema_once,
    assert_business_schema_once,
)

# ✅ AUTH (app.sqlite3)
from backend.auth import (
    ensure_auth_schema,          # ✅ MIGRACIONES STARTUP
    login_gate,
    is_superadmin_scoped,        # ✅ superadmin REAL (DB)
    list_tenants,
    create_tenant,
    list_users,
    create_user,                 # ✅ solo role='user' (por diseño)
    create_user_scoped,          # ✅ crear admin/user con reglas SaaS
    set_user_role,
    reset_user_password,
    delete_user,
)

# ✅ SETTINGS (AUTH DB)
from backend.tenant_settings import get_tenant_settings, save_tenant_settings

# ✅ Auditoría (AUTH DB)
from backend.audit import audit_log, fetch_audit_logs, prune_audit_logs

# ✅ Explorador DB (NEGOCIO)
from backend.db_explorer import (
    table_exists as _table_exists,
    search_donors,                 # ✅ firma: query=
    donor_donations,
    donor_certificates,
    certificate_donations,
    is_valid_email,
    clean_tax_id as _clean_tax_id,
)

# ✅ Data health (NEGOCIO)
# ⚠️ IMPORTANTE: NO importamos record_tax_id_change (no hace falta y te rompe si cambias implementación)
from backend.data_health import (
    check_data_health,
    find_duplicate_donors_by_cif,
    find_duplicate_donors_by_name,
    find_certificates_zombies,
    find_certificates_mismatched_totals,
    void_certificate,
    optimize_sqlite,
)

# ---------------- Paths (Separar DBs) ----------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

APP_DB = BASE_DIR / "data" / "app.sqlite3"   # auth/tenants/settings/audit
BIZ_DB = BASE_DIR / "data" / "biz.sqlite3"   # donors/donations/certificates
QUEUE_DB = BASE_DIR / "queue" / "queue.sqlite3"

# ✅ NO-LEAK por defecto: sin logo global
DEFAULT_LOGO_PATH = ""

# Límite de tamaño del logo (bytes)
MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB

# Validación color HEX en frontend (UX)
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# ✅ Bloqueo del worker embebido en producción:
# Solo se permite si exportas ALLOW_EMBEDDED_WORKER=1
ALLOW_EMBEDDED_WORKER = os.getenv("ALLOW_EMBEDDED_WORKER", "0").strip() == "1"


def load_app_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


APP_CFG = load_app_config()
APP_NAME = (APP_CFG.get("app", {}) or {}).get("name") or "NGO Certificates"

STORAGE_CFG = APP_CFG.get("storage", {}) if isinstance(APP_CFG.get("storage", {}), dict) else {}
BRANDING_DIR_REL = str(STORAGE_CFG.get("branding_dir", "branding") or "branding").strip().replace("\\", "/")
BRANDING_DIR = (BASE_DIR / BRANDING_DIR_REL).resolve()


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ✅ isolation_level=None permite SAVEPOINT / control de commits
    con = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def _normalize_email(s: str) -> str:
    return (s or "").strip().lower()


def safe_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    slug = re.sub(r"[^a-z0-9-.]+", "-", slug)
    slug = slug.strip("-._")
    return slug or "default"


def _ext_from_upload(filename: str) -> str:
    n = (filename or "").lower()
    if n.endswith(".png"):
        return ".png"
    if n.endswith(".jpg") or n.endswith(".jpeg"):
        return ".jpg"
    return ".png"


def _save_tenant_logo(tenant_slug: str, uploaded_file) -> str:
    if uploaded_file is None:
        return ""

    data = uploaded_file.getvalue()
    if not data:
        raise ValueError("Archivo vacío.")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError(f"Logo demasiado grande (máx {MAX_LOGO_BYTES // (1024*1024)}MB).")

    BRANDING_DIR.mkdir(parents=True, exist_ok=True)

    slug = safe_slug(tenant_slug)
    tenant_dir = BRANDING_DIR / slug
    tenant_dir.mkdir(parents=True, exist_ok=True)

    ext = _ext_from_upload(getattr(uploaded_file, "name", ""))

    # Limpia logos anteriores
    for old in (tenant_dir / "logo.png", tenant_dir / "logo.jpg"):
        try:
            if old.exists():
                old.unlink()
        except Exception:
            pass

    out = tenant_dir / f"logo{ext}"
    out.write_bytes(data)

    rel = out.relative_to(BASE_DIR).as_posix()
    return rel


def _try_delete_logo_file(logo_path_rel: str) -> None:
    lp = (logo_path_rel or "").strip().replace("\\", "/")
    if not lp:
        return

    p = (BASE_DIR / lp).resolve()
    branding_root = BRANDING_DIR.resolve()
    try:
        p.relative_to(branding_root)
    except Exception:
        return

    try:
        if p.exists() and p.is_file():
            p.unlink()
    except Exception:
        pass


def _current_logo_path_from_settings(settings: dict) -> str:
    branding = settings.get("branding", {}) if isinstance(settings, dict) else {}
    logo_path = (branding.get("logo_path") or "").strip()
    return logo_path or DEFAULT_LOGO_PATH


def _safe_excel_email(v: Any) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    if s.lower() == "nan":
        return ""
    return s


# ---------------- Streamlit base ----------------

st.set_page_config(page_title=APP_NAME, layout="wide")

# ---------------- Login gate (SaaS serio) ----------------

# ✅ AUTH DB (bloqueante)
con_auth = connect_db(APP_DB)
ensure_auth_schema(con_auth)
login_gate(con_auth)

# ✅ BIZ DB (bloqueante)
con_biz = connect_db(BIZ_DB)

# ✅ crea/migra tablas negocio si hace falta
ensure_business_schema_once(con_biz)

# ✅ modo SaaS serio: si el esquema negocio no está OK, que reviente y no deje operar
assert_business_schema_once(con_biz)

current_user = st.session_state.get("username") or "unknown"
current_role = st.session_state.get("role") or "user"
current_tenant_slug = st.session_state.get("tenant_slug") or "default"
current_tenant_id = st.session_state.get("tenant_id") or "default"

is_admin = current_role == "admin"
is_su = is_superadmin_scoped(con_auth, current_user)


def cfg_for_current_tenant() -> dict:
    return get_tenant_settings(con_auth, current_tenant_id) or {}


# ✅ Auto-higiene ligera (1 vez por sesión, invisible)
if st.session_state.get("authenticated"):
    if "maint_done" not in st.session_state:
        try:
            pr = prune_audit_logs(
                con_auth,
                keep_days=365,
                tenant_id=current_tenant_id,
                include_global=True,
                vacuum=False,
            )
            opt = optimize_sqlite(con_biz, vacuum=False)
            st.session_state["maint_done"] = True
            audit_log(
                con_auth,
                tenant_id=current_tenant_id,
                actor_email=current_user,
                actor_role=current_role,
                action="AUTO_MAINT_SESSION",
                target="sqlite",
                meta={"prune": pr, "optimize": opt},
            )
        except Exception:
            st.session_state["maint_done"] = True


# --- UI header (con nombre de ONG dinámico) ---

_cfg = cfg_for_current_tenant()
tenant_entity_name = (_cfg.get("entidad", {}) or {}).get("nombre") or current_tenant_slug or "ONG"

st.title(f"{APP_NAME} · {tenant_entity_name}")
st.caption("DB-first · Lote (ZIP) + Explorador (DB) + Email + Envío masivo (cola).")


# ---------------- Sidebar ----------------

with st.sidebar:
    st.write(f"👤 {current_user}")
    st.write(f"🏢 Tenant: {current_tenant_slug}")
    st.write(f"🔑 Rol: {current_role}")
    if is_su:
        st.caption("⭐ Superadmin")

    if st.button("Cerrar sesión", width="stretch"):
        st.session_state["authenticated"] = False
        st.session_state["username"] = None
        st.session_state["role"] = None
        st.session_state["tenant_id"] = None
        st.session_state["tenant_slug"] = None
        st.session_state["session_id"] = None
        st.rerun()

    st.divider()
    st.write("SMTP:", "✅ OK" if smtp_config_ok() else "❌ NO configurado")

    # ===================== PANEL ADMIN (TENANT) =====================
    st.divider()

    if is_admin:
        st.markdown("## 👥 Usuarios (mi ONG)")

        with st.expander("Crear usuario (solo mi ONG)", expanded=False):
            with st.form("tenant_create_user_form"):
                u_email = st.text_input("Email/usuario", placeholder="usuario@ong.org", key="tenant_u_email")
                _ = st.selectbox("Rol", ["user"], index=0, key="tenant_u_role")
                u_pwd = st.text_input("Contraseña (mín 10)", type="password", key="tenant_u_pwd")
                ok_u = st.form_submit_button("Crear usuario", width="stretch")

            if ok_u:
                try:
                    create_user(con_auth, tenant_id=current_tenant_id, email=u_email, password=u_pwd, role="user")
                    audit_log(
                        con_auth,
                        tenant_id=current_tenant_id,
                        actor_email=current_user,
                        actor_role=current_role,
                        action="USER_CREATE",
                        target=_normalize_email(u_email),
                        meta={"role": "user"},
                    )
                    st.success("Usuario creado ✅")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        with st.expander("Ver usuarios (mi ONG)", expanded=False):
            users_tenant = list_users(con_auth, tenant_id=current_tenant_id)
            st.write("Usuarios:", len(users_tenant))
            st.dataframe(pd.DataFrame(users_tenant), width="stretch", height=240)

        with st.expander("Gestionar usuario (rol / password / borrar)", expanded=False):
            users_tenant = list_users(con_auth, tenant_id=current_tenant_id)
            if not users_tenant:
                st.info("No hay usuarios en este tenant.")
            else:
                uopts = {f"{u['email']} · {u['role']}": u for u in users_tenant}
                ukey = st.selectbox("Selecciona usuario", list(uopts.keys()), key="tenant_manage_user_sel")
                u = uopts[ukey]

                selected_email = _normalize_email(u.get("email", ""))
                me_email = _normalize_email(current_user)
                is_me = selected_email == me_email

                if is_me:
                    st.info("Estás gestionando TU propio usuario. Por seguridad, no puedes degradarte ni borrarte aquí.")

                if is_su:
                    role_options = ["admin", "user"]
                else:
                    role_options = ["user"]

                current_is_admin = (str(u.get("role") or "").strip().lower() == "admin")
                role_disabled = is_me or (not is_su and current_is_admin)

                new_role = st.selectbox(
                    "Nuevo rol",
                    role_options,
                    index=0 if (role_options[0] == "admin" and current_is_admin) else (0 if role_options == ["user"] else 1),
                    key="tenant_manage_new_role",
                    disabled=role_disabled,
                )
                if st.button("Cambiar rol", width="stretch", key="tenant_btn_role", disabled=role_disabled):
                    try:
                        set_user_role(con_auth, current_tenant_id, current_user, u["id"], new_role)
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="USER_ROLE_SET",
                            target=selected_email,
                            meta={"new_role": new_role},
                        )
                        st.success("Rol actualizado ✅")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

                new_pwd = st.text_input("Nueva contraseña (mín 10)", type="password", key="tenant_new_pwd")
                confirm_reset = st.checkbox("Confirmo que quiero resetear la contraseña", value=False, key="tenant_confirm_reset")
                if st.button("Reset password", width="stretch", key="tenant_btn_resetpwd", disabled=(not confirm_reset)):
                    try:
                        reset_user_password(con_auth, current_tenant_id, current_user, u["id"], new_pwd)
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="USER_PASSWORD_RESET",
                            target=selected_email,
                            meta={"by": current_user},
                        )
                        st.success("Password actualizado ✅")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

                st.warning("⚠️ Borrar usuario es irreversible.")
                confirm_delete = st.checkbox("Confirmo borrar este usuario", value=False, key="tenant_confirm_delete")
                if st.button("🗑️ Borrar usuario", width="stretch", key="tenant_btn_delete", disabled=(is_me or not confirm_delete)):
                    try:
                        delete_user(con_auth, current_tenant_id, current_user, u["id"])
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="USER_DELETE",
                            target=selected_email,
                            meta={},
                        )
                        st.success("Usuario borrado ✅")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
    else:
        st.info("🔒 Solo admins del tenant pueden gestionar usuarios.")

    # ===================== PANEL SAAS (SUPERADMIN GLOBAL) =====================
    st.divider()

    if is_su:
        st.markdown("## 🧩 SaaS (Superadmin)")

        with st.expander("Crear tenant (ONG)", expanded=False):
            with st.form("create_tenant_form"):
                t_name = st.text_input("Nombre del tenant", placeholder="Nombre de la ONG")
                t_slug = st.text_input("Slug", placeholder="mi-ong")
                ok_t = st.form_submit_button("Crear tenant", width="stretch")
            if ok_t:
                try:
                    create_tenant(con_auth, slug=t_slug, name=t_name, actor_email=current_user)
                    audit_log(
                        con_auth,
                        tenant_id="__GLOBAL__",
                        actor_email=current_user,
                        actor_role=current_role,
                        action="TENANT_CREATE",
                        target=safe_slug(t_slug),
                        meta={"name": t_name},
                    )
                    st.success("Tenant creado ✅")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        with st.expander("Crear usuario en CUALQUIER tenant", expanded=False):
            tenants = list_tenants(con_auth)
            if not tenants:
                st.info("No hay tenants.")
            else:
                options = {f"{t['name']} ({t['slug']})": t for t in tenants}
                sel = st.selectbox("Tenant destino", list(options.keys()), key="su_tenant_sel")
                tenant_id = options[sel]["id"]

                with st.form("create_user_form"):
                    u_email = st.text_input("Email/usuario", placeholder="admin@ong.org", key="su_email")
                    u_role = st.selectbox("Rol", ["admin", "user"], index=0, key="su_role")
                    u_pwd = st.text_input("Contraseña (mín 10)", type="password", key="su_pwd")
                    ok_u = st.form_submit_button("Crear usuario", width="stretch")
                if ok_u:
                    try:
                        create_user_scoped(
                            con_auth,
                            actor_tenant_id=current_tenant_id,
                            actor_email=current_user,
                            tenant_id=tenant_id,
                            email=u_email,
                            password=u_pwd,
                            role=u_role,
                        )
                        audit_log(
                            con_auth,
                            tenant_id="__GLOBAL__",
                            actor_email=current_user,
                            actor_role=current_role,
                            action="USER_CREATE_GLOBAL",
                            target=_normalize_email(u_email),
                            meta={"role": u_role, "tenant_id": tenant_id},
                        )
                        st.success("Usuario creado ✅")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with st.expander("Ver tenants / usuarios (GLOBAL)", expanded=False):
            tenants = list_tenants(con_auth)
            st.write("Tenants:", len(tenants))
            st.dataframe(pd.DataFrame(tenants), width="stretch", height=180)

            users = list_users(con_auth, tenant_id=None)
            st.write("Usuarios:", len(users))
            st.dataframe(pd.DataFrame(users), width="stretch", height=240)

        with st.expander("Gestionar usuario (GLOBAL)", expanded=False):
            users = list_users(con_auth, tenant_id=None)
            if not users:
                st.info("No hay usuarios.")
            else:
                uopts = {f"{u['email']} · {u['role']} · {str(u.get('tenant_id',''))[:8]}": u for u in users}
                ukey = st.selectbox("Selecciona usuario", list(uopts.keys()), key="su_manage_user_sel")
                u = uopts[ukey]

                target_tenant_id = u.get("tenant_id")

                selected_email = _normalize_email(u.get("email", ""))
                me_email = _normalize_email(current_user)
                is_me = selected_email == me_email
                if is_me:
                    st.info("Por seguridad, no puedes borrarte a ti mismo desde el panel global.")

                new_role = st.selectbox(
                    "Nuevo rol",
                    ["admin", "user"],
                    index=0 if u["role"] == "admin" else 1,
                    key="su_manage_new_role",
                    disabled=is_me,
                )
                if st.button("Cambiar rol", width="stretch", key="su_btn_role", disabled=is_me):
                    try:
                        set_user_role(con_auth, target_tenant_id, current_user, u["id"], new_role)
                        audit_log(
                            con_auth,
                            tenant_id="__GLOBAL__",
                            actor_email=current_user,
                            actor_role=current_role,
                            action="USER_ROLE_SET_GLOBAL",
                            target=selected_email,
                            meta={"new_role": new_role, "tenant_id": target_tenant_id},
                        )
                        st.success("Rol actualizado ✅")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

                new_pwd = st.text_input("Nueva contraseña (mín 10)", type="password", key="su_new_pwd")
                confirm_reset = st.checkbox("Confirmo reset global", value=False, key="su_confirm_reset")
                if st.button("Reset password", width="stretch", key="su_btn_resetpwd", disabled=(is_me or not confirm_reset)):
                    try:
                        reset_user_password(con_auth, target_tenant_id, current_user, u["id"], new_pwd)
                        audit_log(
                            con_auth,
                            tenant_id="__GLOBAL__",
                            actor_email=current_user,
                            actor_role=current_role,
                            action="USER_PASSWORD_RESET_GLOBAL",
                            target=selected_email,
                            meta={"tenant_id": target_tenant_id},
                        )
                        st.success("Password actualizado ✅")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

                st.warning("⚠️ Borrar usuario es irreversible.")
                confirm_delete = st.checkbox("Confirmo borrar global", value=False, key="su_confirm_delete")
                if st.button("🗑️ Borrar usuario", width="stretch", key="su_btn_delete", disabled=(is_me or not confirm_delete)):
                    try:
                        delete_user(con_auth, target_tenant_id, current_user, u["id"])
                        audit_log(
                            con_auth,
                            tenant_id="__GLOBAL__",
                            actor_email=current_user,
                            actor_role=current_role,
                            action="USER_DELETE_GLOBAL",
                            target=selected_email,
                            meta={"tenant_id": target_tenant_id},
                        )
                        st.success("Usuario borrado ✅")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))


# ---------------- Persistir objetos (motor + queue) ----------------

if "motor" not in st.session_state:
    st.session_state["motor"] = CertSystem(CONFIG_PATH)
motor: CertSystem = st.session_state["motor"]

if "queue" not in st.session_state:
    st.session_state["queue"] = BulkEmailQueue(QUEUE_DB)
queue: BulkEmailQueue = st.session_state["queue"]


# ---------------- Email templates ----------------

def template_by_tipo(meta: Dict[str, Any]) -> str:
    cfg = cfg_for_current_tenant()

    entidad_nombre = (cfg.get("entidad", {}) or {}).get("nombre") or "La entidad"
    firma = ((cfg.get("textos_email", {}) or {}).get("firma") or "").strip()

    fecha_emision = meta.get("fecha_emision") or meta.get("fecha") or ""
    numerocertificado = meta.get("numerocertificado", "")
    hash_ = meta.get("hash", "")
    importe = meta.get("importe", 0.0)
    kg = meta.get("kg", 0.0)

    tipo = (meta.get("tipo") or "").upper()

    if tipo == "ESPECIE":
        tpl_custom = ((cfg.get("textos_email", {}) or {}).get("especie") or "").strip()
        tpl = tpl_custom or (
            "Estimados/as,\n\n"
            "Desde {entidad_nombre} queremos agradecer su colaboración.\n\n"
            "Adjuntamos:\n"
            "• Justificante de entrega\n"
            "• Carta de agradecimiento\n\n"
            "Datos:\n"
            "Número: {numerocertificado}\n"
            "Código verificación: {hash}\n"
            "Fecha emisión: {fecha_emision}\n"
            "Cantidad: {kg} kg\n\n"
            "Atentamente,\n"
            "{firma}\n"
        )
    else:
        tpl_custom = ((cfg.get("textos_email", {}) or {}).get("dinero") or "").strip()
        tpl = tpl_custom or (
            "Estimados/as,\n\n"
            "Desde {entidad_nombre} queremos agradecer su colaboración.\n\n"
            "Adjuntamos:\n"
            "• Certificado de donación\n"
            "• Carta de agradecimiento\n\n"
            "Datos:\n"
            "Número: {numerocertificado}\n"
            "Código verificación: {hash}\n"
            "Fecha emisión: {fecha_emision}\n"
            "Importe: {importe} €\n\n"
            "Atentamente,\n"
            "{firma}\n"
        )

    return (
        tpl.format(
            entidad_nombre=entidad_nombre,
            numerocertificado=numerocertificado,
            hash=hash_,
            fecha_emision=fecha_emision,
            importe=f"{float(importe):.2f}" if importe is not None else "",
            kg=f"{float(kg):.2f}" if kg is not None else "",
            firma=firma or entidad_nombre,
        ).strip()
        + "\n"
    )


def build_email(meta: Dict[str, Any], to_email: str) -> Dict[str, str]:
    cfg = cfg_for_current_tenant()
    entidad_nombre = (cfg.get("entidad", {}) or {}).get("nombre") or current_tenant_slug or "ONG"
    subject = f"{entidad_nombre} · Documento {meta.get('numerocertificado','')}".strip()

    body = template_by_tipo(meta)

    entidad_email = ((cfg.get("entidad", {}) or {}).get("email") or "").strip()
    entidad_web = ((cfg.get("entidad", {}) or {}).get("web") or "").strip()
    entidad_tel = ((cfg.get("entidad", {}) or {}).get("telefono") or "").strip()

    footer_lines = []
    if entidad_email:
        footer_lines.append(entidad_email)
    if entidad_web:
        footer_lines.append(entidad_web)
    if entidad_tel:
        footer_lines.append(entidad_tel)

    if footer_lines:
        body = body + "\n" + "\n".join(footer_lines) + "\n"

    return {"subject": subject, "body": body}


# ===================== Motor call (UI thin) =====================

def motor_reemit_from_db(
    *,
    cert_row: dict,
    donor_row: dict,
    donations: List[dict],
    tenant_cfg: dict,
) -> Tuple[bytes, bytes, Dict[str, Any]]:
    fn = getattr(motor, "build_pdfs_from_db_record", None)
    if not callable(fn):
        raise RuntimeError("El motor no expone build_pdfs_from_db_record(). Actualiza backend/generator.py a v7+.")

    return fn(
        cert_row=cert_row,
        donor_row=donor_row,
        donations=donations,
        tenant_cfg=tenant_cfg,
    )


# ---------------- Tabs ----------------

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["📦 Lote (ZIP)", "🧭 Donantes (DB) + Reemitir/Email", "📧 Masivo (cola)", "⚙️ Ajustes", "🧾 Auditoría", "🩺 Sanidad de Datos"]
)

# ===================== TAB 1: Lote =====================

@st.cache_data(show_spinner="Preparando archivo para descarga...")
def get_zip_from_disk_cached(file_path: str, mtime: float) -> bytes:
    p = Path(file_path)
    return p.read_bytes() if p.exists() else b""


def _init_lote_state() -> None:
    ss = st.session_state
    ss.setdefault("zip_bytes", None)
    ss.setdefault("zip_path", None)
    ss.setdefault("summary", None)
    ss.setdefault("lote_live", [])
    ss.setdefault("lote_running", False)
    ss.setdefault("lote_run_id", None)
    ss.setdefault("lote_started_at", None)
    ss.setdefault("lote_progress", {"done": 0, "total": 0, "pct": 0.0, "label": ""})


def _append_log(line: str) -> None:
    ss = st.session_state
    t_str = datetime.now().strftime("%H:%M:%S")
    ss["lote_live"].append(f"[{t_str}] {line}")
    ss["lote_live"] = ss["lote_live"][-120:]


def _reset_run_flags() -> None:
    ss = st.session_state
    ss["lote_running"] = False
    ss["lote_run_id"] = None
    ss["lote_started_at"] = None


def _safe_delete_zip_path(p_str: str) -> bool:
    if not p_str:
        return False
    try:
        p = Path(p_str)
        if p.exists() and p.is_file():
            p.unlink()
            return True
    except Exception:
        return False
    return False


with tab1:
    st.subheader("📦 Generación en lote (Excel → DB + PDFs)")
    _init_lote_state()
    ss = st.session_state

    if ss["lote_running"] and ss["lote_started_at"]:
        if time.time() - float(ss["lote_started_at"]) > 1800:
            _append_log("⚠️ Run stale resetado.")
            _reset_run_flags()

    prog = ss["lote_progress"]
    if ss["lote_running"]:
        st.warning("⏳ Generación en curso... no cierres esta pestaña.")

    if prog.get("total", 0) > 0:
        st.progress(float(prog.get("pct", 0.0)), text=prog.get("label", ""))

    st.markdown("### 🧾 Log de actividad")
    if ss["lote_live"]:
        st.text_area("Historial", value="\n".join(ss["lote_live"]), height=250, disabled=True)
    else:
        st.info("Esperando Excel...")

    if ss["summary"]:
        s = ss["summary"]
        reused = s.get("total", 0) - s.get("cert_emitidos", 0) - s.get("db_failures", 0)
        st.success(f"✅ Proceso finalizado: {s.get('total')} filas | Nuevos: {s.get('cert_emitidos')} | Reutilizados: {reused}")

    if ss["zip_bytes"] or ss["zip_path"]:
        st.divider()
        if ss["zip_bytes"]:
            st.download_button("⬇️ Descargar ZIP (RAM)", data=ss["zip_bytes"], file_name="lote.zip", mime="application/zip")
        elif ss["zip_path"]:
            p = Path(str(ss["zip_path"]))
            if p.exists():
                st.download_button("⬇️ Descargar ZIP (Disco)", data=get_zip_from_disk_cached(str(p), p.stat().st_mtime), file_name=p.name)

        if st.button("🧹 Limpiar sesión", width="stretch"):
            if ss.get("zip_path"):
                _safe_delete_zip_path(str(ss["zip_path"]))
            ss.update({"zip_bytes": None, "zip_path": None, "summary": None, "lote_live": []})
            ss["lote_progress"] = {"done": 0, "total": 0, "pct": 0.0, "label": ""}
            _reset_run_flags()
            st.rerun()

    st.divider()

    f = st.file_uploader("Sube Excel", type=["xlsx"], key="excel_lote")
    return_mode_ui = st.selectbox("Modo", ["Auto", "RAM", "Disco"], index=0, disabled=ss["lote_running"])
    rm = "path" if "disco" in return_mode_ui.lower() or (f and len(f.getvalue()) > 1500000 and "auto" in return_mode_ui.lower()) else "bytes"

    if f and st.button("🚀 Iniciar Generación", type="primary", width="stretch", disabled=ss["lote_running"]):
        ss["lote_running"] = True
        ss["lote_run_id"] = uuid.uuid4().hex
        ss["lote_started_at"] = time.time()
        ss.update({"zip_bytes": None, "zip_path": None, "summary": None, "lote_live": []})

        _append_log("🛰️ Iniciando búnker de datos...")

        l_prog = st.empty()
        l_log = st.empty()

        ui_tracker = {"last_ts": 0.0, "last_done": -1}
        run_id = ss["lote_run_id"]

        try:

            def progress_cb(done: int, total: int, info: Dict[str, Any]) -> None:
                d, t = int(done or 0), int(total or 1)

                if d > ui_tracker["last_done"]:
                    motivos = str(info.get("motivos", "")).upper()
                    icon = "♻️" if "REUTIL" in motivos or "EXIST" in motivos else "✅"
                    if str(info.get("estado")).upper() in {"CRASH", "ERROR"}:
                        icon = "❌"
                    _append_log(f"{icon} {d}/{t} · {str(info.get('entidad',''))[:20]}")
                    ui_tracker["last_done"] = d

                pct = d / t
                ss["lote_progress"] = {"done": d, "total": t, "pct": pct, "label": f"Fila {d}/{t}"}

                now = time.time()
                if (now - ui_tracker["last_ts"]) >= 0.3 or d >= t:
                    ui_tracker["last_ts"] = now
                    l_prog.progress(pct)
                    l_log.code("\n".join(ss["lote_live"][-10:]))

            tenant_cfg = cfg_for_current_tenant() or {}
            tenant_cfg["user"] = current_user
            tenant_cfg["source_filename"] = getattr(f, "name", "") or ""

            res_out, res_summary = motor.generate_zip_from_excel_bytes(
                f.getvalue(),
                tenant_cfg=tenant_cfg,
                con_biz=con_biz,
                con_auth=con_auth,
                tenant_id=current_tenant_id,
                progress_cb=progress_cb,
                return_mode=rm,
            )

            if st.session_state.get("lote_run_id") == run_id:
                ss.update(
                    {
                        "zip_bytes": res_out if rm == "bytes" else None,
                        "zip_path": res_out if rm == "path" else None,
                        "summary": res_summary,
                    }
                )
                _append_log("🎉 Lote finalizado.")
                _reset_run_flags()

                try:
                    audit_log(
                        con_auth,
                        tenant_id=current_tenant_id,
                        actor_email=current_user,
                        actor_role=current_role,
                        action="BATCH_GENERATE_ZIP",
                        target="excel",
                        meta={"return_mode": rm, "summary": res_summary},
                    )
                except Exception:
                    pass

                st.balloons()
                st.rerun()

        except Exception as e:
            _reset_run_flags()
            try:
                audit_log(
                    con_auth,
                    tenant_id=current_tenant_id,
                    actor_email=current_user,
                    actor_role=current_role,
                    action="BATCH_GENERATE_ZIP_ERROR",
                    target="excel",
                    meta={"error": str(e)},
                )
            except Exception:
                pass
            st.error(f"💥 Error: {e}")


# ===================== TAB 2: Explorador DB =====================

with tab2:
    st.subheader("🧭 Donantes (DB) · Buscar, explorar, ver historial y reemitir PDFs")

    if not _table_exists(con_biz, "donors") or not _table_exists(con_biz, "certificates"):
        st.warning("No detecto tablas de negocio (donors/certificates) en BIZ_DB. Primero procesa un lote.")
    else:

        def donor_cif_display(d: dict) -> str:
            return (str(d.get("cifnif_raw") or d.get("cifnif_norm") or "")).strip()

        st.session_state.setdefault("donor_search_results", [])
        st.session_state.setdefault("db_meta", None)
        st.session_state.setdefault("db_cert_pdf", None)
        st.session_state.setdefault("db_carta_pdf", None)

        st.markdown("### 📊 Estado de la DB (tenant actual)")
        try:
            stats = db_stats(con_biz, tenant_id=current_tenant_id)
            a, b, c = st.columns(3)
            a.metric("Donantes", int(stats.get("donors", 0)))
            b.metric("Donaciones", int(stats.get("donations", 0)))
            c.metric("Certificados", int(stats.get("certificates", 0)))
        except Exception as e:
            st.info(f"Stats no disponibles. Detalle: {e}")

        st.divider()
        st.markdown("### 🗂️ Explorar (si no recuerdas el nombre/CIF)")

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("🕒 Ver últimos donantes", width="stretch"):
                try:
                    st.session_state["donor_search_results"] = list_recent_donors(con_biz, tenant_id=current_tenant_id, limit=50)
                    st.success(f"✅ Cargados {len(st.session_state['donor_search_results'])} últimos donantes")
                except Exception as e:
                    st.error(f"Falló list_recent_donors. Detalle: {e}")

        with c2:
            if st.button("🏆 Top donantes por certificados", width="stretch"):
                try:
                    st.session_state["donor_search_results"] = top_donors_by_certificates(con_biz, tenant_id=current_tenant_id, limit=50)
                    st.success(f"✅ Cargados {len(st.session_state['donor_search_results'])} donantes (top por certificados)")
                except Exception as e:
                    st.error(f"Falló top_donors_by_certificates. Detalle: {e}")

        with c3:
            if st.button("📄 Últimos certificados", width="stretch"):
                try:
                    last_certs = list_recent_certificates(con_biz, tenant_id=current_tenant_id, limit=50)
                    st.dataframe(pd.DataFrame(last_certs), width="stretch", height=240)
                    st.info("Tip: copia el donor_id de un certificado y usa el buscador por ID de abajo.")
                except Exception as e:
                    st.error(f"Falló list_recent_certificates. Detalle: {e}")

        with st.expander("🔢 Buscar por ID (donor_id)"):
            donor_id_q = st.number_input("Donor ID", min_value=1, step=1, value=1, key="db_donor_id_q")
            if st.button("Buscar donor_id", width="stretch", key="btn_search_donor_id"):
                try:
                    d = get_donor(con_biz, tenant_id=current_tenant_id, donor_id=int(donor_id_q))
                    st.session_state["donor_search_results"] = [d] if d else []
                    if d:
                        st.success("Encontrado ✅")
                    else:
                        st.warning("No existe ese donor_id.")
                except Exception as e:
                    st.error(f"Error buscando donor_id: {e}")

        st.divider()

        st.markdown("### 🔎 Buscar donante")
        with st.form("db_search_form", clear_on_submit=False):
            q = st.text_input(
                "Buscar por CIF/NIF/NIE, nombre o email",
                placeholder="A46103834 · Mercadona · compras@empresa.com",
                key="db_search_q",
            )
            colS1, colS2 = st.columns([1, 1])
            with colS1:
                do_search = st.form_submit_button("🔎 Buscar", width="stretch")
            with colS2:
                st.caption("Tip: prueba 1 palabra (sin apellidos). CIF exacto encuentra directo.")

        if do_search:
            if not q.strip():
                st.warning("Escribe algo para buscar (nombre, email o CIF).")
            else:
                try:
                    donors = search_donors(con_biz, tenant_id=current_tenant_id, query=q.strip(), limit=50)
                    st.session_state["donor_search_results"] = donors
                    st.session_state["db_meta"] = None
                    st.session_state["db_cert_pdf"] = None
                    st.session_state["db_carta_pdf"] = None

                    if donors:
                        st.success(f"✅ Encontrados {len(donors)} donantes para: “{q.strip()}”")
                    else:
                        st.warning(f"❌ No hay resultados para: “{q.strip()}”. Prueba menos texto (ej: solo 'mercadona').")
                except Exception as e:
                    st.error(f"Error buscando: {e}")

        donors = st.session_state.get("donor_search_results", [])

        if not donors:
            st.info("Arriba tienes ‘Explorar’ para ver lo último subido, o usa el buscador para filtrar.")
        else:
            st.markdown("### 📋 Resultados")
            df_d = pd.DataFrame(donors).copy()

            if "cifnif_raw" in df_d.columns or "cifnif_norm" in df_d.columns:
                df_d["cif_display"] = df_d.apply(lambda r: (r.get("cifnif_raw") or r.get("cifnif_norm") or ""), axis=1)

            st.dataframe(df_d, width="stretch", height=220)

            st.divider()
            st.markdown("### 🧹 Limpieza: Merge duplicados (avanzado)")

            if is_admin:
                with st.expander("Merge de donantes duplicados (por ID) — mueve certificados + donaciones", expanded=False):
                    st.warning(
                        "Esto moverá TODAS las donaciones y certificados del donor duplicado al donor correcto y borrará el duplicado.\n\n"
                        "Recomendación: usa DRY-RUN primero."
                    )

                    id_map = {f"ID {d['id']} · {d.get('nombre','')} · {donor_cif_display(d)}": int(d["id"]) for d in donors}
                    keys = list(id_map.keys())

                    winner_key = st.selectbox("Donante BUENO (winner)", keys, key="merge_winner_key")
                    loser_key = st.selectbox("Donante DUPLICADO (loser)", keys, key="merge_loser_key")

                    dry = st.toggle("Dry-run (no aplica cambios)", value=True, key="merge_dry")
                    confirm = st.checkbox("Confirmo el merge", value=False, key="merge_confirm")

                    if st.button("🧬 Ejecutar MERGE", type="primary", width="stretch", disabled=(not confirm)):
                        try:
                            winner_id = id_map[winner_key]
                            loser_id = id_map[loser_key]

                            res = merge_donors_v6(
                                con_biz,
                                tenant_id=current_tenant_id,
                                winner_donor_id=winner_id,
                                loser_donor_id=loser_id,
                                dry_run=bool(dry),
                            )

                            audit_log(
                                con_auth,
                                tenant_id=current_tenant_id,
                                actor_email=current_user,
                                actor_role=current_role,
                                action="DONOR_MERGE",
                                target=f"{loser_id}→{winner_id}",
                                meta=res,
                            )

                            if res.get("dry_run"):
                                st.success(f"DRY-RUN OK ✅ Se moverían: {res.get('move')}")
                            else:
                                st.success(f"MERGE OK ✅ Movidos: {res.get('move')} · Borrado duplicado={res.get('deleted_loser')}")
                                st.session_state["donor_search_results"] = search_donors(
                                    con_biz, tenant_id=current_tenant_id, query=(q or "").strip(), limit=50
                                )
                                st.session_state["db_meta"] = None
                                st.session_state["db_cert_pdf"] = None
                                st.session_state["db_carta_pdf"] = None
                                st.rerun()

                        except Exception as e:
                            st.error(str(e))
            else:
                st.info("🔒 Solo admins pueden hacer merge.")

            st.divider()

            opts = {f"{d.get('nombre','')} · {donor_cif_display(d)} · ID {d['id']}": d for d in donors}
            sel = st.selectbox("Selecciona donante", list(opts.keys()), key="db_sel_donor_key")
            donor = opts[sel]

            donor_id = int(donor["id"])
            donor_certs = donor_certificates(con_biz, tenant_id=current_tenant_id, donor_id=donor_id, limit=200)
            donor_dons = donor_donations(con_biz, tenant_id=current_tenant_id, donor_id=donor_id, limit=200)

            cA, cB, cC = st.columns([1, 1, 1])
            cA.metric("Certificados", len(donor_certs))
            cB.metric("Donaciones", len(donor_dons))

            try:
                cC.metric("Anon?", int(is_anonymous_donor(con_biz, donor_id=donor_id)))
            except Exception:
                cC.metric("Anon?", 0)

            st.divider()
            st.markdown("### ✏️ Editar ficha de donante (CRM)")

            has_certs = len(donor_certs) > 0

            with st.form(f"donor_edit_form_{donor_id}", clear_on_submit=False):
                new_nombre = st.text_input("Nombre", value=str(donor.get("nombre") or ""), key=f"edit_nombre_{donor_id}")

                old_cif = str(donor.get("cifnif_raw") or donor.get("cifnif_norm") or "")
                new_cif = st.text_input("CIF/NIF/NIE", value=old_cif, key=f"edit_cif_{donor_id}")

                new_email = st.text_input("Email", value=str(donor.get("email") or ""), key=f"edit_email_{donor_id}")

                st.caption("⚠️ Nota: si cambias CIF/NIF, debes justificarlo (trazabilidad Hacienda).")

                cif_changed = (new_cif or "").strip() != (old_cif or "").strip()

                allow_cif_change = st.checkbox(
                    "Permitir cambiar CIF/NIF (avanzado)",
                    value=False,
                    help="Recomendado SOLO para corregir errores reales. Siempre pide motivo.",
                    key=f"allow_cif_change_{donor_id}",
                )

                force_cif_change = False
                cif_change_reason = ""

                if cif_changed:
                    if not allow_cif_change:
                        st.warning("Has modificado el CIF, pero NO has marcado 'Permitir cambiar CIF'.")
                    cif_change_reason = st.text_input(
                        "Motivo del cambio de CIF (obligatorio si cambia)",
                        value="",
                        key=f"edit_cif_reason_{donor_id}",
                    )

                if cif_changed and has_certs:
                    st.warning(
                        "Este donante YA tiene certificados emitidos. Si cambias el CIF, la BD cambiará pero los PDFs "
                        "anteriores seguirán mostrando el CIF antiguo. Solo hazlo si es una corrección necesaria."
                    )
                    force_cif_change = st.checkbox(
                        "FORZAR cambio de CIF aunque existan certificados",
                        value=False,
                        key=f"edit_cif_force_{donor_id}",
                    )

                save_btn = st.form_submit_button("💾 Guardar cambios", width="stretch")

            if save_btn:
                try:
                    cif_changed = (new_cif or "").strip() != (old_cif or "").strip()

                    if cif_changed and not allow_cif_change:
                        st.error("Para cambiar CIF debes marcar 'Permitir cambiar CIF'.")
                    elif cif_changed and not (cif_change_reason or "").strip():
                        st.error("Motivo obligatorio para cambiar CIF.")
                    elif cif_changed and has_certs and not force_cif_change:
                        st.error("Para cambiar CIF con certificados emitidos debes marcar FORZAR.")
                    else:
                        # ✅ IMPORTANTE: update_donor_details YA registra historial en donor_tax_id_history si cambia el CIF.
                        # => NO llamamos record_tax_id_change aquí (evita ImportError y doble registro)

                        try:
                            sig = inspect.signature(update_donor_details)
                            kwargs = dict(
                                con=con_biz,
                                tenant_id=current_tenant_id,
                                donor_id=int(donor["id"]),
                                nombre=new_nombre,
                                email=new_email,
                                cifnif=new_cif,
                                allow_cif_change=bool(allow_cif_change),
                            )
                            if "changed_by" in sig.parameters:
                                kwargs["changed_by"] = current_user
                            if "reason" in sig.parameters:
                                kwargs["reason"] = (cif_change_reason or "").strip()
                            update_donor_details(**kwargs)
                        except TypeError:
                            update_donor_details(
                                con_biz,
                                tenant_id=current_tenant_id,
                                donor_id=int(donor["id"]),
                                nombre=new_nombre,
                                email=new_email,
                                cifnif=new_cif,
                                allow_cif_change=bool(allow_cif_change),
                            )

                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="DONOR_UPDATE",
                            target=str(donor.get("id")),
                            meta={
                                "old": {"nombre": donor.get("nombre"), "cifnif": old_cif, "email": donor.get("email")},
                                "new": {"nombre": new_nombre, "cifnif": new_cif, "email": new_email},
                                "cif_changed": bool(cif_changed),
                                "cif_change_reason": (cif_change_reason or "").strip(),
                                "forced": bool(force_cif_change),
                                "had_certificates": bool(has_certs),
                            },
                        )

                        st.success("Ficha actualizada ✅")

                        refresh_q = (new_cif or "").strip() or (new_nombre or "").strip()
                        st.session_state["donor_search_results"] = search_donors(
                            con_biz, tenant_id=current_tenant_id, query=refresh_q, limit=50
                        )
                        st.session_state["db_meta"] = None
                        st.session_state["db_cert_pdf"] = None
                        st.session_state["db_carta_pdf"] = None
                        st.rerun()

                except Exception as e:
                    st.error(str(e))

            st.divider()
            st.markdown("### 📄 Certificados existentes (DB)")

            if not donor_certs:
                st.info("Este donante aún no tiene certificados en DB.")
            else:
                df_c = pd.DataFrame(donor_certs)
                st.dataframe(df_c, width="stretch", height=240)

                cert_opts = {
                    f"{c.get('numerocertificado','')} · {c.get('tipo','')} · {c.get('fecha_emision','')} · "
                    f"{c.get('status_certificado','')}/{c.get('status_carta','')} · ID {c.get('id')}": c
                    for c in donor_certs
                }
                cert_key = st.selectbox("Selecciona certificado para reemitir/reenviar", list(cert_opts.keys()), key=f"db_sel_cert_{donor_id}")
                cert = cert_opts[cert_key]

                if st.session_state.get("last_cert_id") != int(cert["id"]):
                    st.session_state["last_cert_id"] = int(cert["id"])
                    st.session_state["db_meta"] = None
                    st.session_state["db_cert_pdf"] = None
                    st.session_state["db_carta_pdf"] = None

                dons = certificate_donations(con_biz, certificate_id=int(cert["id"]))
                if dons:
                    st.caption("Donaciones asociadas al certificado:")
                    st.dataframe(pd.DataFrame(dons), width="stretch", height=180)
                else:
                    st.caption("No hay detalle de donaciones asociadas (o no existe certificate_items). Reemisión igual funciona.")

                if st.button("🧾 Reemitir PDFs desde DB", type="primary", key=f"db_btn_reemit_{donor_id}_{cert['id']}"):
                    try:
                        cert_pdf, carta_pdf, meta = motor_reemit_from_db(
                            cert_row=cert,
                            donor_row=donor,
                            donations=dons,
                            tenant_cfg=cfg_for_current_tenant(),
                        )
                        st.session_state["db_cert_pdf"] = cert_pdf
                        st.session_state["db_carta_pdf"] = carta_pdf
                        st.session_state["db_meta"] = meta

                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="CERT_REEMIT_SNAPSHOT",
                            target=str(meta.get("numerocertificado", "")),
                            meta={"cert_id": cert.get("id"), "used_snapshot": bool(meta.get("used_snapshot", False))},
                        )

                        st.success(f"Copia fiel reconstruida ✅ · {meta.get('numerocertificado','')} · Hash {meta.get('hash','')}")
                    except Exception as e:
                        st.error(f"Fallo en la reconstrucción: {e}")

                meta = st.session_state.get("db_meta")
                if meta:
                    cert_pdf = st.session_state.get("db_cert_pdf") or b""
                    carta_pdf = st.session_state.get("db_carta_pdf") or b""

                    c1, c2 = st.columns(2)
                    with c1:
                        st.download_button(
                            "⬇️ Descargar CERTIFICADO/JUSTIFICANTE",
                            data=cert_pdf,
                            file_name=f"{meta['numerocertificado']}_CERT.pdf",
                            mime="application/pdf",
                            key=f"db_dl_cert_{donor_id}_{cert['id']}",
                        )
                    with c2:
                        if carta_pdf:
                            st.download_button(
                                "⬇️ Descargar CARTA",
                                data=carta_pdf,
                                file_name=f"{meta['numerocertificado']}_CARTA.pdf",
                                mime="application/pdf",
                                key=f"db_dl_carta_{donor_id}_{cert['id']}",
                            )
                        else:
                            st.info("Carta no generada (status_carta no OK/REVIEW).")

                    st.divider()
                    st.markdown("### 📨 Reenviar por email (desde DB)")

                    to_email_default = (meta.get("email") or donor.get("email") or "").strip()
                    to_email = st.text_input("Email destino", value=to_email_default, key=f"db_to_email_{donor_id}_{cert['id']}")

                    email_preview = build_email(meta, to_email)
                    with st.expander("Preview email"):
                        st.text(f"TO: {to_email}\nSUBJECT: {email_preview['subject']}\n\n{email_preview['body']}")

                    colE1, colE2 = st.columns([1, 1])
                    with colE1:
                        attach_cert = st.toggle("Adjuntar certificado", value=True, key=f"db_attach_cert_{donor_id}_{cert['id']}")
                    with colE2:
                        attach_letter = st.toggle(
                            "Adjuntar carta",
                            value=bool(carta_pdf),
                            disabled=not bool(carta_pdf),
                            key=f"db_attach_letter_{donor_id}_{cert['id']}",
                        )

                    if st.button("📨 Enviar email ahora", type="secondary", key=f"db_send_email_{donor_id}_{cert['id']}"):
                        if not to_email:
                            st.error("No hay email destino.")
                        elif not is_valid_email(to_email):
                            st.error("El email destino no parece válido.")
                        elif not smtp_config_ok():
                            st.error("SMTP no está configurado.")
                        elif not attach_cert and not attach_letter:
                            st.error("Debes adjuntar certificado y/o carta.")
                        else:
                            try:
                                attachments = []
                                if attach_cert:
                                    attachments.append((f"{meta['numerocertificado']}_CERT.pdf", cert_pdf, "application/pdf"))
                                if attach_letter and carta_pdf:
                                    attachments.append((f"{meta['numerocertificado']}_CARTA.pdf", carta_pdf, "application/pdf"))

                                send_email_with_attachments(
                                    to_email=to_email,
                                    subject=email_preview["subject"],
                                    body=email_preview["body"],
                                    attachments=attachments,
                                )

                                audit_log(
                                    con_auth,
                                    tenant_id=current_tenant_id,
                                    actor_email=current_user,
                                    actor_role=current_role,
                                    action="EMAIL_SEND_ONE_FROM_DB",
                                    target=to_email,
                                    meta={
                                        "numerocertificado": meta.get("numerocertificado"),
                                        "attach_cert": attach_cert,
                                        "attach_letter": attach_letter,
                                    },
                                )
                                st.success(f"Enviado a {to_email} ✅")
                            except Exception as e:
                                st.error(f"Error enviando email: {e}")

            st.divider()
            st.markdown("### 🧾 Historial donaciones (DB)")
            if donor_dons:
                st.dataframe(pd.DataFrame(donor_dons), width="stretch", height=320)
            else:
                st.info("Sin donaciones registradas para este donante.")


# ===================== TAB 3: Masivo (cola) =====================

with tab3:
    st.subheader("📧 Envío masivo (cola)")

    import tempfile

    # ---------------- Helpers UI-safe ----------------
    def _safe_str(x: Any) -> str:
        if x is None:
            return ""
        try:
            if pd.isna(x):
                return ""
        except Exception:
            pass
        s = str(x).strip()
        return "" if s.lower() == "nan" else s

    def _pick_first_email_from_subframe(sub: pd.DataFrame) -> str:
        if "contactoemail" not in sub.columns:
            return ""
        emails = [_safe_excel_email(x) for x in sub["contactoemail"].tolist()]
        emails = [e for e in emails if e]
        return emails[0] if emails else ""

    def _normalize_cif_list(values: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for v in values:
            t = _clean_tax_id(v)
            if not t or t == "NAN":
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out

    def _safe_unlink(path: str) -> None:
        p = (path or "").strip()
        if not p:
            return
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    def _save_upload_to_tempfile(uploaded) -> Tuple[str, str, int]:
        """
        Guarda el XLSX a disco y devuelve:
          (tmp_path, original_name, size_bytes)
        """
        data = uploaded.getvalue()
        size = len(data or b"")
        # Nota: sufijo .xlsx para que openpyxl se porte bien
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
        try:
            tmp.write(data)
            tmp.flush()
        finally:
            try:
                tmp.close()
            except Exception:
                pass
        return tmp.name, (uploaded.name or "(sin nombre)"), size

    def _read_excel_df_from_path(path: str) -> pd.DataFrame:
        if not path or not os.path.exists(path):
            raise ValueError("No hay Excel cargado (ruta temporal no existe).")
        return pd.read_excel(path, engine="openpyxl")

    # ---------------- Worker status ----------------
    try:
        running = queue.worker_running(tenant_id=current_tenant_id)
    except TypeError:
        running = queue.worker_running()

    st.session_state.setdefault("auto_refresh", True)
    if running and st.session_state["auto_refresh"]:
        st_autorefresh(interval=1500, key="queue_refresh_pro")

    # ---------------- KPIs ----------------
    stats = queue.stats(tenant_id=current_tenant_id)
    sent_total = int(stats.get("SENT", 0)) + int(stats.get("SIM_SENT", 0))

    m1, m2, m3, m4, m5 = st.columns([1, 1, 1, 1, 1.2])
    m1.metric("Pendientes", int(stats.get("PENDING", 0)))
    m2.metric("Enviados", sent_total)
    m3.metric("Fallidos", int(stats.get("FAILED", 0)))
    m4.metric("Saltados", int(stats.get("SKIPPED", 0)))
    m5.metric("Total", int(stats.get("TOTAL", 0)))

    topL, topR, topR2 = st.columns([2, 1, 1])
    with topL:
        st.success("🟢 Worker en ejecución" if running else "⚪ Worker detenido")
    with topR:
        st.session_state["auto_refresh"] = st.toggle("Auto-refresh", value=st.session_state["auto_refresh"])
    with topR2:
        if st.button("🔄 Refrescar ahora", width="stretch"):
            st.rerun()

    st.divider()

    # ---------------- 1) Cargar Excel ----------------
    st.markdown("### 1) Cargar Excel (solo para extraer emails masivos)")
    left, right = st.columns([2.2, 1])

    with left:
        # Estado en session_state (solo guardamos PATH, no bytes)
        st.session_state.setdefault("excel_masivo_path", "")
        st.session_state.setdefault("excel_masivo_name", "")
        st.session_state.setdefault("excel_masivo_size", 0)

        f3 = st.file_uploader("Excel de donaciones (XLSX)", type=["xlsx"], key="excel_masivo_pro")

        # Si sube un Excel, lo guardamos a disco y limpiamos el anterior
        if f3 is not None:
            old_path = st.session_state.get("excel_masivo_path", "")
            # guardamos nuevo
            tmp_path, orig_name, size_b = _save_upload_to_tempfile(f3)
            # limpiamos antiguo
            if old_path and old_path != tmp_path:
                _safe_unlink(old_path)

            st.session_state["excel_masivo_path"] = tmp_path
            st.session_state["excel_masivo_name"] = orig_name
            st.session_state["excel_masivo_size"] = int(size_b)

            # al cambiar Excel, invalida preview
            st.session_state["queue_preview_items"] = None
            st.session_state["queue_preview_stats"] = None

        excel_path = st.session_state.get("excel_masivo_path", "")
        excel_name = st.session_state.get("excel_masivo_name", "(sin nombre)")
        excel_size = int(st.session_state.get("excel_masivo_size", 0) or 0)

        if excel_path and os.path.exists(excel_path):
            st.caption(f"Archivo cargado: `{excel_name}` · {excel_size/1024/1024:.2f} MB ✅")

            cA, cB = st.columns([1, 1])
            with cA:
                if st.button("🧹 Quitar Excel cargado", width="stretch"):
                    _safe_unlink(excel_path)
                    st.session_state["excel_masivo_path"] = ""
                    st.session_state["excel_masivo_name"] = ""
                    st.session_state["excel_masivo_size"] = 0
                    st.session_state["queue_preview_items"] = None
                    st.session_state["queue_preview_stats"] = None
                    st.rerun()
            with cB:
                st.caption("Se guarda temporalmente en disco para evitar RAM.")
        else:
            st.warning("Sube el Excel para poder encolar (solo para extraer emails).")

        st.markdown("### 2) Previsualizar + Encolar")

        with st.form("enqueue_form", clear_on_submit=False):
            colE1, colE2 = st.columns([1, 1])
            with colE1:
                usar_email_fijo = st.checkbox("Usar email fijo (testing)", value=False)
            with colE2:
                email_fijo = st.text_input("Email fijo", placeholder="tu_email@gmail.com", disabled=not usar_email_fijo)

            preview_btn = st.form_submit_button("👀 Previsualizar", width="stretch")
            enqueue_btn = st.form_submit_button("➕ Encolar", width="stretch")

        # Guardamos preview en session_state
        st.session_state.setdefault("queue_preview_items", None)
        st.session_state.setdefault("queue_preview_stats", None)

        def _build_preview_items_from_path(
            excel_path_: str,
            *,
            usar_email_fijo_: bool,
            email_fijo_: str,
        ) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
            df = _read_excel_df_from_path(excel_path_)
            df = motor.map_columns(df)

            if "cifnif" not in df.columns:
                raise ValueError("Falta CIF/NIF tras mapeo. Revisa nombres de columnas del Excel.")

            df["_cif_norm"] = df["cifnif"].astype(str).map(_clean_tax_id)
            cifs = _normalize_cif_list(df["_cif_norm"].dropna().tolist())

            items: List[Tuple[str, str]] = []
            n_with_email = 0
            n_no_email = 0
            invalid_emails = 0

            for cif in cifs:
                sub = df[df["_cif_norm"] == cif].copy()
                if usar_email_fijo_:
                    to_email = _safe_str(email_fijo_)
                else:
                    to_email = _pick_first_email_from_subframe(sub)

                to_email = (to_email or "").strip()
                if to_email:
                    if is_valid_email(to_email):
                        n_with_email += 1
                    else:
                        invalid_emails += 1
                else:
                    n_no_email += 1

                items.append((cif, to_email))

            stats_ = {
                "rows": int(len(df)),
                "unique_cifs": int(len(cifs)),
                "with_email": int(n_with_email),
                "no_email": int(n_no_email),
                "invalid_email": int(invalid_emails),
                "using_fixed_email": bool(usar_email_fijo_),
                "excel_name": excel_name,
                "excel_mb": round(excel_size / 1024 / 1024, 3),
            }
            return items, stats_

        if preview_btn:
            if not excel_path or not os.path.exists(excel_path):
                st.error("Sube el Excel primero.")
            elif usar_email_fijo and not is_valid_email(email_fijo):
                st.error("Email fijo inválido.")
            else:
                with st.spinner("Analizando el Excel…"):
                    try:
                        items, stt = _build_preview_items_from_path(
                            excel_path,
                            usar_email_fijo_=usar_email_fijo,
                            email_fijo_=email_fijo,
                        )
                        st.session_state["queue_preview_items"] = items
                        st.session_state["queue_preview_stats"] = stt
                        st.success("Preview generado ✅ (revisa abajo antes de encolar).")
                    except Exception as e:
                        st.error(f"Error en preview: {e}")

        # Render preview
        preview_items = st.session_state.get("queue_preview_items")
        preview_stats = st.session_state.get("queue_preview_stats") or {}

        if preview_items:
            st.divider()
            st.markdown("### ✅ Preview de encolado")

            p1, p2, p3, p4, p5 = st.columns([1, 1, 1, 1, 1.2])
            p1.metric("Filas Excel", int(preview_stats.get("rows", 0)))
            p2.metric("CIF únicos", int(preview_stats.get("unique_cifs", 0)))
            p3.metric("Email válido", int(preview_stats.get("with_email", 0)))
            p4.metric("Sin email", int(preview_stats.get("no_email", 0)))
            p5.metric("Email inválido", int(preview_stats.get("invalid_email", 0)))

            df_prev = pd.DataFrame(preview_items, columns=["cif_norm", "to_email"])
            df_prev["email_ok"] = df_prev["to_email"].map(lambda x: bool(x and is_valid_email(x)))

            st.dataframe(df_prev, width="stretch", height=260, hide_index=True)

            st.caption("Tip: si hay muchos 'sin email', revisa que el Excel tenga la columna 'Email contacto' (mapeada a contactoemail).")

        # Encolar
        if enqueue_btn:
            if not excel_path or not os.path.exists(excel_path):
                st.error("Sube el Excel primero.")
            elif usar_email_fijo and not is_valid_email(email_fijo):
                st.error("Email fijo inválido.")
            else:
                with st.spinner("Preparando encolado…"):
                    try:
                        if not st.session_state.get("queue_preview_items"):
                            items, stt = _build_preview_items_from_path(
                                excel_path,
                                usar_email_fijo_=usar_email_fijo,
                                email_fijo_=email_fijo,
                            )
                            st.session_state["queue_preview_items"] = items
                            st.session_state["queue_preview_stats"] = stt
                        else:
                            items = st.session_state["queue_preview_items"]

                        n = queue.enqueue_many(
                            items,
                            max_attempts=3,
                            enqueued_by=current_user,
                            tenant_id=current_tenant_id,
                        )

                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="QUEUE_ENQUEUE_MANY",
                            target="excel",
                            meta={
                                "count": int(n),
                                "testing_email_fixed": bool(usar_email_fijo),
                                "excel_name": excel_name,
                                "preview": st.session_state.get("queue_preview_stats") or {},
                            },
                        )

                        st.success(f"Encolados {n} registros ✅")

                        # limpia preview para evitar re-encolar sin querer
                        st.session_state["queue_preview_items"] = None
                        st.session_state["queue_preview_stats"] = None
                        st.rerun()

                    except Exception as e:
                        st.error(f"Error encolando: {e}")

    # ---------------- Right panel actions ----------------
    with right:
        st.markdown("### Acciones")

        st.info(
            "✅ **Recomendado**: el worker debe correr como proceso independiente (Docker/systemd).\n\n"
            "La app Streamlit idealmente **solo encola** y **muestra estado**."
        )

        smtp_ok = smtp_config_ok()
        st.write("SMTP:", "✅ OK" if smtp_ok else "❌ NO configurado")

        with st.expander("⚠️ Modo embebido (solo testing)", expanded=False):
            if not ALLOW_EMBEDDED_WORKER:
                st.error("Modo embebido DESHABILITADO. Actívalo con `ALLOW_EMBEDDED_WORKER=1` (solo dev).")
            else:
                st.warning("Streamlit puede matar hilos si se desconecta la sesión. Úsalo solo para pruebas.")
                if not running:
                    if st.button("▶️ Iniciar worker embebido (SIMULADO)", width="stretch", type="primary"):
                        excel_path = st.session_state.get("excel_masivo_path", "")
                        if not excel_path or not os.path.exists(excel_path):
                            st.error("Sube el Excel antes.")
                        else:
                            with st.spinner("Cargando Excel a memoria para el worker (solo ahora)…"):
                                excel_bytes = Path(excel_path).read_bytes()

                            cfg = QueueConfig(test_mode=True)
                            queue.start_worker(
                                motor=motor,
                                excel_bytes=excel_bytes,
                                build_email_fn=lambda _motor, meta, to_email: build_email(meta, to_email),
                                cfg=cfg,
                                started_by=current_user,
                                tenant_id=current_tenant_id,
                                tenant_cfg=cfg_for_current_tenant(),
                                app_db_path=APP_DB,
                                biz_db_path=BIZ_DB,
                            )
                            audit_log(
                                con_auth,
                                tenant_id=current_tenant_id,
                                actor_email=current_user,
                                actor_role=current_role,
                                action="WORKER_START_EMBEDDED",
                                target="queue",
                                meta={"test_mode": True},
                            )
                            st.success("Worker iniciado (SIMULADO, embebido).")
                            st.rerun()
                else:
                    if st.button("⏹ Detener worker embebido", width="stretch"):
                        queue.stop_worker()
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="WORKER_STOP_EMBEDDED",
                            target="queue",
                            meta={},
                        )
                        st.warning("Parando worker…")
                        st.rerun()

        st.divider()

        if is_admin:
            with st.expander("⚙️ Opciones avanzadas (config para worker)", expanded=False):
                modo = st.selectbox("Modo de envío", ["SIMULAR (no envía)", "ENVIAR REAL"], index=0)
                max_attempts = st.number_input("Reintentos máx.", min_value=1, value=3, step=1)
                pause_ms = st.number_input("Pausa entre correos (ms)", min_value=0, value=300, step=100)
                backoff_ms = st.number_input("Backoff base (ms)", min_value=200, value=800, step=100)

                cth1, cth2 = st.columns(2)
                with cth1:
                    domain_gap = st.number_input("Gap dominio (seg)", min_value=0.0, value=1.2, step=0.2)
                with cth2:
                    domain_burst = st.number_input("Máx seguidos mismo dominio", min_value=1, value=2, step=1)

                cA, cB = st.columns(2)
                with cA:
                    attach_cert = st.toggle("Adjuntar Certificado/Justificante", value=True)
                with cB:
                    attach_letter = st.toggle("Adjuntar Carta", value=True)

                stop_enabled = st.toggle("Stop after N", value=False)
                stop_after_n = st.number_input("Parar tras N éxitos", min_value=1, value=10, step=1) if stop_enabled else None

                st.divider()

                if modo == "ENVIAR REAL":
                    st.warning("⚠️ Envío REAL: se enviarán emails a donantes.")
                    if not smtp_ok:
                        st.error("SMTP NO configurado. No guardo una config de envío real sin SMTP.")
                    confirm_real = st.checkbox("Confirmo que quiero ENVIAR REAL", value=False)
                else:
                    confirm_real = True

                if st.button("Guardar config (para worker externo)", width="stretch"):
                    if modo == "ENVIAR REAL" and (not confirm_real):
                        st.error("Falta confirmación para ENVIAR REAL.")
                    elif modo == "ENVIAR REAL" and (not smtp_ok):
                        st.error("SMTP NO configurado.")
                    elif not attach_cert and not attach_letter:
                        st.error("Debes adjuntar CERT y/o CARTA.")
                    else:
                        st.session_state["worker_cfg"] = dict(
                            max_attempts=int(max_attempts),
                            sleep_between_ms=int(pause_ms),
                            backoff_base_ms=int(backoff_ms),
                            test_mode=(modo.startswith("SIMULAR")),
                            domain_min_gap_seconds=float(domain_gap),
                            domain_burst=int(domain_burst),
                            stop_after_success=int(stop_after_n) if stop_enabled else None,
                            attach_cert=bool(attach_cert),
                            attach_letter=bool(attach_letter),
                        )
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="WORKER_CONFIG_SAVE",
                            target="queue",
                            meta=st.session_state["worker_cfg"],
                        )
                        st.success("Config guardada ✅ (lista para worker externo).")

                st.divider()

                colX1, colX2 = st.columns(2)
                with colX1:
                    if st.button("♻️ Reintentar FAILED", width="stretch"):
                        n = queue.retry_failed(reset_attempts=False, tenant_id=current_tenant_id)
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="QUEUE_RETRY_FAILED",
                            target="queue",
                            meta={"count": int(n)},
                        )
                        st.success(f"Reencolados {n} FAILED.")
                        st.rerun()
                with colX2:
                    if st.button("🧹 Vaciar cola", width="stretch"):
                        queue.clear_all(tenant_id=current_tenant_id)
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="QUEUE_CLEAR_ALL",
                            target="queue",
                            meta={},
                        )
                        st.warning("Cola vaciada.")
                        st.rerun()
        else:
            st.info("🔒 Modo Admin requerido para opciones avanzadas.")

    # ---------------- Deliveries log ----------------
    st.divider()
    st.markdown("### 📜 Registro de envíos")

    with st.expander("Ver log", expanded=True):
        deliveries = queue.list_deliveries(limit=300, tenant_id=current_tenant_id)
        df_del = pd.DataFrame(deliveries) if deliveries else pd.DataFrame([])

        if df_del.empty:
            st.info("Aún no hay registros.")
        else:
            col_id = "id" if "id" in df_del.columns else None
            col_status = "status" if "status" in df_del.columns else None
            col_to = "to_email" if "to_email" in df_del.columns else ("email" if "email" in df_del.columns else None)
            col_cif = "cif" if "cif" in df_del.columns else ("cifnif" if "cifnif" in df_del.columns else None)
            col_attempt = "attempt" if "attempt" in df_del.columns else ("attempts" if "attempts" in df_del.columns else None)
            col_err = "error" if "error" in df_del.columns else ("last_error" if "last_error" in df_del.columns else None)

            col_ts = None
            for c in ("updated_at", "sent_at", "created_at", "ts", "timestamp"):
                if c in df_del.columns:
                    col_ts = c
                    break

            if col_id:
                df_del = df_del.sort_values(col_id, ascending=False)

            df_show = pd.DataFrame()
            if col_ts:
                df_show["Fecha/Hora"] = pd.to_datetime(df_del[col_ts], errors="coerce", utc=True)
            if col_status:
                df_show["Estado"] = df_del[col_status].astype(str)
            if col_cif:
                df_show["CIF"] = df_del[col_cif].astype(str)
            if col_to:
                df_show["Email"] = df_del[col_to].astype(str)
            if col_attempt:
                df_show["Intentos"] = df_del[col_attempt]
            if col_err:
                df_show["Error"] = df_del[col_err].astype(str).map(
                    lambda s: (s[:160] + "…") if isinstance(s, str) and len(s) > 160 else s
                )

            if df_show.empty:
                st.dataframe(df_del, width="stretch", height=460)
                csv_data = df_del.to_csv(index=False).encode("utf-8-sig")
            else:
                st.dataframe(
                    df_show,
                    width="stretch",
                    height=460,
                    hide_index=True,
                    column_config={
                        "Fecha/Hora": st.column_config.DatetimeColumn("Fecha/Hora", format="DD/MM/YYYY HH:mm:ss"),
                        "Error": st.column_config.TextColumn("Error", width="large"),
                    },
                )
                csv_data = df_show.to_csv(index=False).encode("utf-8-sig")

            st.download_button(
                "⬇️ Descargar log (CSV)",
                data=csv_data,
                file_name=f"{safe_slug(current_tenant_slug)}_queue_log.csv",
                mime="text/csv",
                width="stretch",
            )

# ===================== TAB 4: Ajustes por tenant =====================

with tab4:
    st.subheader("⚙️ Ajustes del tenant")

    if not is_admin:
        st.info("🔒 Solo admins del tenant pueden modificar los ajustes.")
    else:
        settings = get_tenant_settings(con_auth, current_tenant_id) or {}
        current_logo_path = _current_logo_path_from_settings(settings)

        # Valor actual (siempre hex) con fallback
        current_color = (settings.get("branding", {}) or {}).get("color_principal", "#3B468C") or "#3B468C"
        current_color = str(current_color).strip() or "#3B468C"

        # Regex (una sola vez aquí)
        LEY49_RE = re.compile(r"\bley\s*49\s*[/\-]?\s*2002\b|\b49\s*[/\-]\s*2002\b", re.IGNORECASE)
        RD1270_RE = re.compile(
            r"\breal\s*decreto\s*1270\s*[/\-]?\s*2003\b|\brd\s*1270\s*[/\-]?\s*2003\b|\br\.d\.\s*1270\s*[/\-]?\s*2003\b",
            re.IGNORECASE,
        )

        def _neutralize_ley_refs(s: str) -> str:
            s = str(s or "")
            s = LEY49_RE.sub("la normativa fiscal aplicable", s)
            s = RD1270_RE.sub("la normativa fiscal aplicable", s)
            return s

        # ===================== FORM =====================
        if "legal_dinero" not in st.session_state:
            st.session_state["legal_dinero"] = (settings.get("textoslegales", {}) or {}).get("certificadodinero", "")
        if "legal_especie" not in st.session_state:
            st.session_state["legal_especie"] = (settings.get("textoslegales", {}) or {}).get("certificadoespecie", "")
        
        with st.form("tenant_settings_form"):
            st.markdown("### Datos de la entidad")
            nombre = st.text_input(
                "Nombre entidad",
                value=(settings.get("entidad", {}) or {}).get("nombre", ""),
                key="entidad_nombre",
            )
            cif_entidad = st.text_input(
                "CIF entidad (opcional)",
                value=(settings.get("entidad", {}) or {}).get("cif", ""),
                key="entidad_cif",
            )
            direccion = st.text_input(
                "Dirección (opcional)",
                value=(settings.get("entidad", {}) or {}).get("direccion", ""),
                key="entidad_direccion",
            )
            ciudad = st.text_input(
                "Ciudad emisión (PDF) (opcional)",
                value=(settings.get("entidad", {}) or {}).get("ciudad", ""),
                key="entidad_ciudad",
            )
            email = st.text_input(
                "Email entidad",
                value=(settings.get("entidad", {}) or {}).get("email", ""),
                key="entidad_email",
            )
            web_ = st.text_input(
                "Web",
                value=(settings.get("entidad", {}) or {}).get("web", ""),
                key="entidad_web",
            )
            telefono = st.text_input(
                "Teléfono",
                value=(settings.get("entidad", {}) or {}).get("telefono", ""),
                key="entidad_telefono",
            )

            st.markdown("### Firma PDF")
            firm_nombre = st.text_input(
                "Nombre firmante",
                value=(settings.get("firmas", {}) or {}).get("nombre", ""),
                key="firm_nombre",
            )
            firm_cargo = st.text_input(
                "Cargo firmante",
                value=(settings.get("firmas", {}) or {}).get("cargo", ""),
                key="firm_cargo",
            )

            st.markdown("### Textos legales (PDF)")
            # ✅ Claves para poder actualizar desde st.session_state con el botón de fuera
            if "legal_dinero" not in st.session_state:
                st.session_state["legal_dinero"] = (settings.get("textoslegales", {}) or {}).get("certificadodinero", "")

            legal_dinero = st.text_area(
                "Texto legal DINERO (certificado fiscal)",
                height=160,
                key="legal_dinero",
            )
            legal_especie = st.text_area(
                "Texto legal ESPECIE (justificante de entrega)",
                height=160,
                key="legal_especie",
            )

            st.markdown("### Firma y plantillas email")
            firma_email = st.text_input(
                "Firma (texto email)",
                value=(settings.get("textos_email", {}) or {}).get("firma", ""),
                key="email_firma",
            )
            dinero = st.text_area(
                "Plantilla DINERO (opcional)",
                value=(settings.get("textos_email", {}) or {}).get("dinero", ""),
                height=160,
                key="email_tpl_dinero",
            )
            especie = st.text_area(
                "Plantilla ESPECIE (opcional)",
                value=(settings.get("textos_email", {}) or {}).get("especie", ""),
                height=160,
                key="email_tpl_especie",
            )

            st.markdown("### Validaciones")
            validate_es = st.toggle(
                "Validar CIF/NIF/NIE español (estricto)",
                value=bool((settings.get("features", {}) or {}).get("validate_spanish_tax_id", True)),
                help="Si tu ONG no es de España, puedes desactivarlo para aceptar identificadores genéricos.",
                key="feat_validate_es",
            )

            st.markdown("### Régimen fiscal (Ley 49/2002)")
            ley49 = st.toggle(
                "La entidad está acogida al régimen fiscal de la Ley 49/2002",
                value=bool((settings.get("features", {}) or {}).get("is_ley_49_2002", False)),
                help="Actívalo SOLO si la ONG está oficialmente acogida a la Ley 49/2002.",
                key="feat_ley49",
            )

            # Detectar referencias en el texto ACTUAL del form
            txt_all = f"{legal_dinero}\n{legal_especie}"
            mentions_ley = bool(LEY49_RE.search(txt_all))
            mentions_rd = bool(RD1270_RE.search(txt_all))

            if (not ley49) and (mentions_ley or mentions_rd):
                st.warning(
                    "⚠️ El texto menciona **Ley 49/2002** o **RD 1270/2003**, pero el toggle está desactivado.\n"
                    "Si la entidad NO está acogida, evita afirmarlo en el certificado. Puedes limpiar el texto automáticamente (botón debajo del formulario)."
                )

            st.markdown("### Branding")
            up = st.file_uploader(
                "Subir logo (PNG/JPG) — se guarda solo para esta ONG",
                type=["png", "jpg", "jpeg"],
                key="tenant_logo_upload",
            )

            st.caption(f"Logo actual: `{current_logo_path or '(vacío)'}`")
            st.caption(f"Directorio branding: `{BRANDING_DIR_REL}`")

            colL1, colL2 = st.columns([1, 1])
            with colL1:
                usar_default = st.checkbox(
                    "Usar logo por defecto",
                    value=False,
                    disabled=(DEFAULT_LOGO_PATH.strip() == ""),
                    help="Deshabilitado porque DEFAULT_LOGO_PATH está vacío (no-leak).",
                    key="branding_use_default",
                )
            with colL2:
                quitar_logo = st.checkbox("Quitar logo (sin logo)", value=False, key="branding_remove_logo")

            colC1, colC2 = st.columns([1, 1])
            with colC1:
                color_principal = st.color_picker(
                    "Color principal (selector)",
                    value=current_color if HEX_COLOR_RE.match(current_color) else "#3B468C",
                    key="branding_color_picker",
                )
            with colC2:
                color_hex_manual = st.text_input(
                    "Color (hex) — opcional",
                    value=str(color_principal),
                    help="Si quieres pegar un color corporativo exacto (#RRGGBB).",
                    key="branding_color_manual",
                )

            cp_final = (color_hex_manual or "").strip() or str(color_principal).strip()

            st.markdown(
                f"""
                <div style="display:flex; align-items:center; gap:12px; margin-top:6px;">
                  <div style="width:26px; height:26px; border-radius:6px; background:{cp_final}; border:1px solid #ddd;"></div>
                  <div style="font-size:14px;">Color seleccionado: <code>{cp_final}</code></div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            ok = st.form_submit_button("Guardar ajustes", width="stretch")

        # ===================== BOTÓN AUTO-LIMPIEZA (FUERA DEL FORM) =====================
        # Recalcular con session_state (lo que el usuario ve en pantalla)
        _txt_din = str(st.session_state.get("legal_dinero", "") or "")
        _txt_esp = str(st.session_state.get("legal_especie", "") or "")
        _txt_all = f"{_txt_din}\n{_txt_esp}"
        _mentions_ley = bool(LEY49_RE.search(_txt_all))
        _mentions_rd = bool(RD1270_RE.search(_txt_all))
        _ley49_on = bool(st.session_state.get("feat_ley49", bool((settings.get("features", {}) or {}).get("is_ley_49_2002", False))))

        if (not _ley49_on) and (_mentions_ley or _mentions_rd):
            if st.button("🧼 Quitar referencias (auto)", type="secondary", key="btn_clean_ley_refs"):
                st.session_state["legal_dinero"] = _neutralize_ley_refs(_txt_din)
                st.session_state["legal_especie"] = _neutralize_ley_refs(_txt_esp)
                st.success("Texto limpiado. Ahora pulsa “Guardar ajustes”.")
                st.rerun()

        # ===================== SAVE =====================
        if ok:
            cp = (cp_final or "").strip()
            if not HEX_COLOR_RE.match(cp):
                st.error("Color inválido. Usa formato #RRGGBB (ej: #3B468C).")
            else:
                logo_path_final = ((settings.get("branding", {}) or {}).get("logo_path", "") or "").strip()

                quitar_logo_val = bool(st.session_state.get("branding_remove_logo", False))
                usar_default_val = bool(st.session_state.get("branding_use_default", False))

                if up is not None and not quitar_logo_val:
                    try:
                        old_logo = logo_path_final
                        logo_path_final = _save_tenant_logo(current_tenant_slug, up)
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="TENANT_LOGO_UPLOAD",
                            target=current_tenant_slug,
                            meta={"old": old_logo, "new": logo_path_final},
                        )
                        st.success("Logo subido ✅")
                    except Exception as e:
                        st.error(f"No se pudo guardar el logo: {e}")
                        logo_path_final = ((settings.get("branding", {}) or {}).get("logo_path", "") or "").strip()

                if quitar_logo_val:
                    _try_delete_logo_file(logo_path_final)
                    audit_log(
                        con_auth,
                        tenant_id=current_tenant_id,
                        actor_email=current_user,
                        actor_role=current_role,
                        action="TENANT_LOGO_REMOVE",
                        target=current_tenant_slug,
                        meta={"old": logo_path_final},
                    )
                    logo_path_final = ""

                if usar_default_val and not quitar_logo_val:
                    logo_path_final = DEFAULT_LOGO_PATH or ""

                new_settings = {
                    "entidad": {
                        "nombre": (nombre or "").strip(),
                        "cif": (cif_entidad or "").strip(),
                        "direccion": (direccion or "").strip(),
                        "ciudad": (ciudad or "").strip(),
                        "email": (email or "").strip(),
                        "web": (web_ or "").strip(),
                        "telefono": (telefono or "").strip(),
                    },
                    "firmas": {
                        "nombre": (firm_nombre or "").strip(),
                        "cargo": (firm_cargo or "").strip(),
                    },
                    "textoslegales": {
                        "certificadodinero": st.session_state.get("legal_dinero", ""),
                        "certificadoespecie": st.session_state.get("legal_especie", ""),
                    },
                    "textos_email": {
                        "firma": (firma_email or "").strip(),
                        "dinero": dinero or "",
                        "especie": especie or "",
                    },
                    "branding": {
                        "logo_path": (logo_path_final or "").strip(),
                        "color_principal": cp or "#3B468C",
                    },
                    "features": {
                        "validate_spanish_tax_id": bool(validate_es),
                        "is_ley_49_2002": bool(ley49),
                    },
                }

                save_tenant_settings(con_auth, current_tenant_id, new_settings)
                audit_log(
                    con_auth,
                    tenant_id=current_tenant_id,
                    actor_email=current_user,
                    actor_role=current_role,
                    action="TENANT_SETTINGS_SAVE",
                    target=current_tenant_slug,
                    meta={"keys": list(new_settings.keys())},
                )
                st.success("Ajustes guardados ✅")
                st.rerun()

# ===================== TAB 5: Auditoría =====================

with tab5:
    st.subheader("🧾 Auditoría (acciones admin)")

    if not is_admin and not is_su:
        st.info("🔒 Solo admins/superadmin pueden ver auditoría.")
    else:
        logs = fetch_audit_logs(con_auth, current_tenant_id, limit=600)
        df = pd.DataFrame(logs) if logs else pd.DataFrame([])

        if df.empty:
            st.info("Sin logs aún.")
        else:
            # ---------------- Helpers ----------------
            def _parse_meta(x):
                if x is None:
                    return {}
                if isinstance(x, dict):
                    return x
                s = str(x).strip()
                if not s:
                    return {}
                try:
                    return json.loads(s)
                except Exception:
                    return {}

            def _fmt_dt(x):
                try:
                    ts = pd.to_datetime(x, errors="coerce", utc=True)
                    if pd.isna(ts):
                        return None
                    return ts.to_pydatetime()
                except Exception:
                    return None

            def _coalesce(*vals, default=""):
                for v in vals:
                    if v is None:
                        continue
                    s = str(v).strip()
                    if s and s.lower() != "nan":
                        return s
                return default

            # ---------------- Detectar columnas reales ----------------
            # meta puede llamarse distinto según tu audit.py
            meta_col = None
            for c in ("meta_json", "meta", "metaJson", "metaJSON"):
                if c in df.columns:
                    meta_col = c
                    break

            time_col = None
            for c in ("created_at", "createdAt", "ts", "timestamp", "time"):
                if c in df.columns:
                    time_col = c
                    break

            actor_email_col = "actor_email" if "actor_email" in df.columns else None
            actor_col = "actor" if "actor" in df.columns else None
            role_col = "actor_role" if "actor_role" in df.columns else None
            actor_type_col = "actor_type" if "actor_type" in df.columns else None
            action_col = "action" if "action" in df.columns else None
            target_col = "target" if "target" in df.columns else None

            # ---------------- Normalizar columnas base ----------------
            if meta_col:
                meta_series = df[meta_col]
            else:
                # IMPORTANTÍSIMO: Series vacía, no string
                meta_series = pd.Series([""] * len(df), index=df.index)

            df["meta_dict"] = meta_series.apply(_parse_meta)

            if time_col:
                df["Fecha/Hora"] = df[time_col].apply(_fmt_dt)
            else:
                df["Fecha/Hora"] = None

            df["Usuario"] = df.apply(
                lambda r: _coalesce(
                    r.get(actor_email_col) if actor_email_col else None,
                    r.get(actor_col) if actor_col else None,
                    default="unknown",
                ),
                axis=1,
            )
            df["Rol"] = df.apply(
                lambda r: _coalesce(
                    r.get(role_col) if role_col else None,
                    r.get(actor_type_col) if actor_type_col else None,
                    default="-",
                ),
                axis=1,
            )
            df["Acción"] = df.apply(lambda r: _coalesce(r.get(action_col) if action_col else None, default=""), axis=1)
            df["Recurso"] = df.apply(lambda r: _coalesce(r.get(target_col) if target_col else None, default="-"), axis=1)

            # ---------------- “Detalles” dinámicos (legible) ----------------
            def build_details(row) -> str:
                meta = row.get("meta_dict") or {}
                action = (row.get("Acción") or "").strip().upper()

                # Emails
                if "EMAIL_" in action or "SEND" in action:
                    cert = meta.get("numerocertificado") or meta.get("cert") or ""
                    att = []
                    if meta.get("attach_cert"):
                        att.append("CERT")
                    if meta.get("attach_letter"):
                        att.append("CARTA")
                    att_txt = f"Adjuntos: {', '.join(att)}" if att else "Adjuntos: -"
                    return f"Cert: {cert or '-'} · {att_txt}"

                # Reemisión
                if "REEMIT" in action:
                    cert_id = meta.get("cert_id")
                    used_snap = meta.get("used_snapshot")
                    return f"cert_id={cert_id} · snapshot={'sí' if used_snap else 'no'}"

                # Batch ZIP
                if "BATCH" in action or "GENERATE_ZIP" in action:
                    s = meta.get("summary") if isinstance(meta.get("summary"), dict) else meta
                    total = (s or {}).get("total")
                    cert_ok = (s or {}).get("cert_ok", (s or {}).get("ok"))
                    cert_review = (s or {}).get("cert_review")
                    cert_invalid = (s or {}).get("cert_invalid")
                    db_fail = (s or {}).get("db_failures")
                    zip_mb = (s or {}).get("zip_size_mb")
                    if any(v is not None for v in [total, cert_ok, cert_review, cert_invalid, db_fail, zip_mb]):
                        return (
                            f"Total: {total} · OK: {cert_ok} · Review: {cert_review} · "
                            f"Invalid: {cert_invalid} · DB_fail: {db_fail} · ZIP: {zip_mb}MB"
                        )

                # Merge donors
                if "MERGE" in action:
                    move = meta.get("move")
                    deleted = meta.get("deleted_loser")
                    dry = meta.get("dry_run")
                    parts = []
                    if dry is not None:
                        parts.append(f"dry_run={'sí' if dry else 'no'}")
                    if move is not None:
                        parts.append(f"movidos={move}")
                    if deleted is not None:
                        parts.append(f"borrado_loser={deleted}")
                    return " · ".join(parts) if parts else "Merge ejecutado"

                # VOID / mantenimiento
                if "VOID" in action:
                    reason = meta.get("reason") or meta.get("void_reason") or ""
                    return f"Motivo: {reason}" if reason else "Marcado VOID"

                if "MAINT" in action or "OPTIMIZE" in action or "PRUNE" in action:
                    prune = meta.get("prune") if isinstance(meta.get("prune"), dict) else {}
                    opt = meta.get("optimize") if isinstance(meta.get("optimize"), dict) else {}
                    p_del = prune.get("deleted")
                    p_keep = prune.get("keep_days")
                    vreq = opt.get("vacuum_requested")
                    return f"prune_deleted={p_del} · keep_days={p_keep} · vacuum_req={vreq}"

                # Fallback compacto
                if meta:
                    clean = {}
                    for k, v in meta.items():
                        if v in (None, "", [], {}, False):
                            continue
                        if isinstance(v, list) and len(v) > 5:
                            clean[k] = f"[{len(v)} items]"
                        else:
                            clean[k] = v
                    if clean:
                        s = json.dumps(clean, ensure_ascii=False)
                        return s[:260] + ("…" if len(s) > 260 else "")
                return "—"

            df["Detalles"] = df.apply(build_details, axis=1)

            # ---------------- Filtros ----------------
            with st.expander("🔎 Filtros", expanded=True):
                c1, c2, c3 = st.columns([1.4, 1, 1])

                with c1:
                    q = st.text_input(
                        "Buscar (usuario, acción, recurso, detalles)",
                        value="",
                        placeholder="CERT-2026-000001 · EMAIL · BATCH · dani@...",
                    )

                with c2:
                    actions = sorted([a for a in df["Acción"].dropna().unique().tolist() if str(a).strip()])
                    action_sel = st.multiselect("Acción", options=actions, default=[])

                with c3:
                    users = sorted([u for u in df["Usuario"].dropna().unique().tolist() if str(u).strip()])
                    user_sel = st.multiselect("Usuario", options=users, default=[])

            df_view = df.copy()

            if action_sel:
                df_view = df_view[df_view["Acción"].isin(action_sel)]
            if user_sel:
                df_view = df_view[df_view["Usuario"].isin(user_sel)]

            if q and q.strip():
                qq = q.strip().casefold()

                def _row_match(r):
                    hay = " | ".join(
                        [
                            str(r.get("Usuario", "")),
                            str(r.get("Rol", "")),
                            str(r.get("Acción", "")),
                            str(r.get("Recurso", "")),
                            str(r.get("Detalles", "")),
                        ]
                    ).casefold()
                    return qq in hay

                df_view = df_view[df_view.apply(_row_match, axis=1)]

            if "Fecha/Hora" in df_view.columns:
                df_view = df_view.sort_values("Fecha/Hora", ascending=False)

            # ---------------- Render ----------------
            display_cols = ["Fecha/Hora", "Usuario", "Rol", "Acción", "Recurso", "Detalles"]
            display_cols = [c for c in display_cols if c in df_view.columns]

            st.dataframe(
                df_view[display_cols],
                width="stretch",
                height=520,
                hide_index=True,
                column_config={
                    "Fecha/Hora": st.column_config.DatetimeColumn("Fecha/Hora", format="DD/MM/YYYY HH:mm:ss"),
                    "Detalles": st.column_config.TextColumn("Detalles", width="large"),
                },
            )

            cdl1, cdl2 = st.columns([1, 1])
            with cdl1:
                st.download_button(
                    "⬇️ Descargar auditoría (CSV limpio)",
                    data=df_view[display_cols].to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"{safe_slug(current_tenant_slug)}_audit_clean.csv",
                    mime="text/csv",
                    width="stretch",
                )
            with cdl2:
                st.caption(f"Mostrando {len(df_view)} / {len(df)} eventos")

            with st.expander("🛠️ Debug: ver meta crudo"):
                raw_cols = []
                if time_col and time_col in df_view.columns:
                    raw_cols.append(time_col)
                for c in ("Usuario", "Rol", "Acción", "Recurso"):
                    if c in df_view.columns:
                        raw_cols.append(c)
                if meta_col and meta_col in df_view.columns:
                    raw_cols.append(meta_col)

                if raw_cols:
                    st.dataframe(df_view[raw_cols], width="stretch", height=320)
                else:
                    st.info("No hay columnas raw detectables.")
# ===================== TAB 6: Sanidad de Datos =====================

with tab6:
    st.subheader("🩺 Sanidad de Datos (mantenimiento)")

    if not is_admin and not is_su:
        st.info("🔒 Solo admins/superadmin pueden usar esta pestaña.")
    else:
        st.caption("Objetivo: detectar duplicados, certificados 'zombi', descuadres contables y ejecutar mantenimiento.")

        st.markdown("## 0) KPIs rápidos (panel de control)")
        try:
            kpi = check_data_health(con_biz, tenant_id=current_tenant_id)
        except Exception as e:
            st.error(f"No pude calcular KPIs: {e}")
            kpi = {}

        k1, k2, k3, k4, k5 = st.columns([1, 1, 1, 1, 1])
        k1.metric("Donantes", int(kpi.get("total_donors") or 0))
        k2.metric("Certificados", int(kpi.get("total_certificates") or 0))
        k3.metric("Grupos CIF duplic.", int(kpi.get("duplicate_cif_groups") or 0))
        k4.metric("Donantes duplic.", int(kpi.get("duplicate_cif_donors") or 0))
        k5.metric("Certificados zombi", int(kpi.get("zombie_certificates") or 0))

        if kpi.get("mismatched_certificates") is not None:
            st.metric("Descuadres contables", int(kpi.get("mismatched_certificates") or 0))

        notes = kpi.get("notes") or []
        if notes:
            st.info("Notas del checker:\n\n" + "\n".join([f"• {n}" for n in notes]))

        st.divider()

        st.markdown("## 1) Donantes duplicados (mismo CIF)")
        try:
            dups = find_duplicate_donors_by_cif(con_biz, tenant_id=current_tenant_id, limit_groups=200)
        except Exception as e:
            st.error(str(e))
            dups = []

        if not dups:
            st.success("No hay duplicados por CIF detectados ✅")
        else:
            st.warning(f"He encontrado **{len(dups)}** grupos duplicados.")
            for i, g in enumerate(dups[:30], start=1):
                st.markdown(f"### Grupo {i} · key={g['key']} · {g['count']} donantes")
                st.dataframe(pd.DataFrame(g["donors"]), width="stretch", height=180)

                opts = {f"ID {d['id']} · {d['nombre']} · {d['cifnif']}": int(d["id"]) for d in g["donors"]}
                colsM = st.columns([2, 2, 1, 1])
                with colsM[0]:
                    wkey = st.selectbox(f"Winner (grupo {i})", list(opts.keys()), key=f"health_w_{i}")
                with colsM[1]:
                    lkey = st.selectbox(f"Loser (grupo {i})", list(opts.keys()), key=f"health_l_{i}")
                with colsM[2]:
                    dry = st.toggle("Dry-run", value=True, key=f"health_dry_{i}")
                with colsM[3]:
                    confirm = st.checkbox("Confirmo", value=False, key=f"health_ok_{i}")

                if st.button("🧬 Merge grupo", width="stretch", key=f"health_merge_{i}", disabled=not confirm):
                    try:
                        res = merge_donors_v6(
                            con_biz,
                            tenant_id=current_tenant_id,
                            winner_donor_id=opts[wkey],
                            loser_donor_id=opts[lkey],
                            dry_run=bool(dry),
                        )
                        audit_log(
                            con_auth,
                            tenant_id=current_tenant_id,
                            actor_email=current_user,
                            actor_role=current_role,
                            action="DONOR_MERGE_HEALTH",
                            target=f"{opts[lkey]}→{opts[wkey]}",
                            meta=res,
                        )
                        if res.get("dry_run"):
                            st.success(f"DRY-RUN OK ✅ Se moverían: {res.get('move')}")
                        else:
                            st.success(f"MERGE OK ✅ Movidos: {res.get('move')} · borrado loser={res.get('deleted_loser')}")
                            st.rerun()
                    except Exception as e:
                        st.error(str(e))

        st.divider()

        st.markdown("## 1b) Posibles duplicados por nombre (heurístico)")
        st.caption("Nota: si no existe nombre_norm, este escaneo puede tardar (full scan).")

        scan_names = st.toggle("Escanear duplicados por nombre", value=False)
        if scan_names:
            try:
                name_dups = find_duplicate_donors_by_name(con_biz, tenant_id=current_tenant_id, limit_groups=100, min_len=6)
            except Exception as e:
                st.error(str(e))
                name_dups = []

            if not name_dups:
                st.success("No hay duplicados por nombre detectados ✅")
            else:
                st.warning(f"Encontrados **{len(name_dups)}** grupos por nombre.")
                for i, g in enumerate(name_dups[:15], start=1):
                    st.markdown(f"### Nombre key={g['key']} · {g['count']} donantes")
                    st.dataframe(pd.DataFrame(g["donors"]), width="stretch", height=180)

        st.divider()

        st.markdown("## 2) Certificados 'zombi' (contienen donación anulada)")
        try:
            zombies = find_certificates_zombies(con_biz, tenant_id=current_tenant_id, limit=200)
        except Exception as e:
            st.error(str(e))
            zombies = []

        if not zombies:
            st.success("No hay certificados zombi detectados ✅")
        else:
            dfz = pd.DataFrame(zombies)
            st.warning(f"Detectados **{len(dfz)}** certificados potencialmente zombi.")
            st.dataframe(dfz, width="stretch", height=260)

            st.markdown("### Acción: marcar como VOID (no borra, deja trazabilidad)")
            cert_id = st.number_input("certificate_id", min_value=1, value=int(dfz.iloc[0]["cert_id"]), step=1)
            reason = st.text_input("Motivo VOID", value="Contiene donación anulada / inconsistencia detectada")
            confirm_void = st.checkbox("Confirmo marcar VOID", value=False)

            if st.button("🚫 Marcar VOID", type="primary", disabled=not confirm_void, width="stretch"):
                try:
                    meta_void = void_certificate(
                        con_biz,
                        tenant_id=current_tenant_id,
                        certificate_id=int(cert_id),
                        reason=reason,
                        voided_by=current_user,
                    )
                    audit_log(
                        con_auth,
                        tenant_id=current_tenant_id,
                        actor_email=current_user,
                        actor_role=current_role,
                        action="CERT_VOID",
                        target=str(cert_id),
                        meta={"reason": reason, "void_meta": meta_void},
                    )
                    st.success("Certificado marcado VOID ✅")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        st.divider()

        st.markdown("## 2b) Certificados descuadrados (importe/kg no coincide con sus donaciones)")
        try:
            mism = find_certificates_mismatched_totals(con_biz, tenant_id=current_tenant_id, limit=200, tol=0.01)
        except Exception as e:
            st.error(str(e))
            mism = []

        if not mism:
            st.success("No hay descuadres contables detectados ✅ (o tu esquema no tiene totales en certificates).")
        else:
            dfm = pd.DataFrame(mism)
            st.warning(f"Detectados **{len(dfm)}** certificados descuadrados.")
            st.dataframe(dfm, width="stretch", height=280)

        st.divider()

        st.markdown("## 3) Mantenimiento (logs + optimización SQLite)")
        c1, c2, c3 = st.columns([1, 1, 1.4])

        with c1:
            keep_days = st.number_input("Retención auditoría (días)", min_value=30, max_value=3650, value=365, step=30)
        with c2:
            do_vacuum = st.toggle("VACUUM (costoso)", value=False, help="Úsalo cuando el worker esté parado.")
        with c3:
            st.caption("Esto limpia auditoría y ejecuta PRAGMA optimize + ANALYZE (+ VACUUM opcional).")

        confirm_maint = st.checkbox("Confirmo mantenimiento", value=False)

        if st.button("🧹 Ejecutar mantenimiento", type="primary", width="stretch", disabled=not confirm_maint):
            try:
                pr = prune_audit_logs(
                    con_auth,
                    keep_days=int(keep_days),
                    tenant_id=current_tenant_id,
                    include_global=True,
                    vacuum=False,
                )
                opt = optimize_sqlite(con_biz, vacuum=bool(do_vacuum))

                audit_log(
                    con_auth,
                    tenant_id=current_tenant_id,
                    actor_email=current_user,
                    actor_role=current_role,
                    action="DB_MAINTENANCE",
                    target="sqlite",
                    meta={"prune": pr, "optimize": opt},
                )

                if opt.get("vacuum_requested") and opt.get("vacuum_ok") is False:
                    st.warning(f"OK ✅ prune_deleted={pr.get('deleted')} · VACUUM falló: {opt.get('vacuum_error')}")
                else:
                    st.success(f"OK ✅ prune_deleted={pr.get('deleted')} · vacuum={do_vacuum}")
            except Exception as e:
                st.error(str(e))

# ---------------- Cierre conexiones (RECOMENDADO Windows) ----------------
try:
    con_auth.close()
except Exception:
    pass

try:
    con_biz.close()
except Exception:
    pass