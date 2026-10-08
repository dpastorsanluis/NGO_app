# backend/auth.py
from __future__ import annotations

import os
import hmac
import hashlib
import sqlite3
import uuid
import re
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict, Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHash

logger = logging.getLogger(__name__)

# ---------------- Streamlit (opcional) ----------------
# ✅ IMPORT OPCIONAL: auth_core NO debe depender de Streamlit
try:
    import streamlit as st  # type: ignore
except Exception:
    st = None  # type: ignore


# =========================
# Config (SaaS hardening)
# =========================
ENV = (os.getenv("ENV", "prod") or "prod").strip().lower()
DEBUG = os.getenv("DEBUG", "0").strip() == "1"

# Bootstrap admin desde ENV: en prod debe estar explícitamente permitido
BOOTSTRAP_MODE = os.getenv("AUTH_BOOTSTRAP_MODE", "0").strip() == "1"

# ✅ OJO: recovery por ENV eliminado a propósito (era backdoor).
# Si quieres recovery, hazlo por superadmin DB + panel/CLI.
BOOTSTRAP_RECOVERY_CODE = (os.getenv("BOOTSTRAP_RECOVERY_CODE") or "").strip()  # solo informativo, ya no se usa

DEFAULT_SESSION_HOURS = int(os.getenv("AUTH_SESSION_HOURS", "12"))
MAX_SESSIONS_PER_USER = int(os.getenv("AUTH_MAX_SESSIONS_PER_USER", "5"))

# 🔒 Hardening extra: evitar bootstrap accidental de tenant 'default' en prod
ALLOW_DEFAULT_TENANT_BOOTSTRAP = os.getenv("ALLOW_DEFAULT_TENANT_BOOTSTRAP", "0").strip() == "1"

# RGPD/Privacidad: si se soft-deletea usuario, anonimizar email (recomendado)
ANONYMIZE_EMAIL_ON_DELETE = os.getenv("ANONYMIZE_EMAIL_ON_DELETE", "1").strip() == "1"

# En prod, si no has tocado AUTH_SESSION_HOURS, recomendamos bajar TTL
if ENV == "prod" and "AUTH_SESSION_HOURS" not in os.environ:
    DEFAULT_SESSION_HOURS = min(DEFAULT_SESSION_HOURS, 6)


# ---------------- UI helpers ----------------
def _ui_warn(msg: str) -> None:
    """UI helper: warning solo si Streamlit existe."""
    if st is not None:
        try:
            st.warning(msg)
        except Exception:
            pass
    else:
        logger.warning("UI_WARN: %s", msg)


def _ui_error(msg: str) -> None:
    if st is not None:
        try:
            st.error(msg)
        except Exception:
            pass
    else:
        logger.error("UI_ERROR: %s", msg)


def _ui_success(msg: str) -> None:
    if st is not None:
        try:
            st.success(msg)
        except Exception:
            pass
    else:
        logger.info("UI_SUCCESS: %s", msg)


def _ui_stop() -> None:
    """UI helper: stop solo si Streamlit existe; si no, lanza error."""
    if st is not None:
        try:
            st.stop()
        except Exception:
            raise RuntimeError("Streamlit stop failed")
    raise RuntimeError("UI function called without Streamlit available")


def _ui_rerun() -> None:
    if st is not None:
        try:
            st.rerun()
        except Exception:
            pass


# ---------------- Argon2 ----------------
_PH = PasswordHasher(
    time_cost=2,
    memory_cost=64 * 1024,  # 64 MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)

# ✅ Slug policy (SaaS serio)
SLUG_RE = re.compile(r"^[a-z0-9-]{3,40}$")
_RESERVED_SLUGS = {"default"}  # 'default' solo bootstrap/controlado

# ✅ “email or username” policy simple (ONG-friendly)
USERNAME_RE = re.compile(r"^[a-z0-9._@+-]{3,80}$", re.IGNORECASE)
EMAIL_SIMPLE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _sha256_legacy(text: str) -> str:
    """Legacy SHA-256 (NO USAR para nuevos hashes)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _consteq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def _norm(s: str) -> str:
    """Normaliza para comparaciones/DB: trim + casefold (más robusto que lower)."""
    return (s or "").strip().casefold()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------- Password policy (ONG-friendly pero SaaS) ----------------
def validate_password_policy(password: str) -> None:
    p = (password or "").strip()
    if len(p) < 10:
        raise ValueError("Contraseña mínima 10 caracteres")
    if not re.search(r"[A-Za-z]", p):
        raise ValueError("La contraseña debe contener al menos 1 letra.")
    if not re.search(r"\d", p):
        raise ValueError("La contraseña debe contener al menos 1 número.")


def hash_password(password: str) -> str:
    validate_password_policy(password)
    return _PH.hash((password or "").strip())


def verify_password(stored_hash: str, password: str) -> bool:
    stored_hash = (stored_hash or "").strip()
    password = (password or "").strip()
    if not stored_hash or not password:
        return False
    try:
        return _PH.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


# ---------------- Fake verify (anti-enumeración timing) ----------------
# ✅ P0 FIX: hash Argon2 PRECOMPUTADO (constante) para evitar regeneración en cada arranque
_FAKE_ARGON2_HASH = os.getenv(
    "AUTH_FAKE_ARGON2_HASH",
    "$argon2id$v=19$m=65536,t=2,p=2$Hwd3x1yeiiUsksGnJ5TfPA$CYKg6nlb772itmZEWFGN7K2XsMJ4A/74Ulv8gO1lw9E",
).strip()


def _fake_verify_cost(password: str) -> None:
    try:
        _PH.verify(_FAKE_ARGON2_HASH, (password or "x"))
    except Exception:
        pass


# ---------------- DB helpers ----------------
def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    cols: set[str] = set()
    for r in rows:
        name = r[1] if isinstance(r, (tuple, list)) else r["name"]
        cols.add(str(name))
    return cols


def _count_users(con: sqlite3.Connection) -> int:
    if not _table_exists(con, "users"):
        return 0
    row = con.execute("SELECT COUNT(*) FROM users;").fetchone()
    return int(row[0]) if row else 0


def _count_tenants(con: sqlite3.Connection) -> int:
    if not _table_exists(con, "tenants"):
        return 0
    row = con.execute("SELECT COUNT(*) FROM tenants;").fetchone()
    return int(row[0]) if row else 0


def _count_admins(con: sqlite3.Connection, tenant_id: str) -> int:
    assert_auth_schema(con)
    row = con.execute(
        "SELECT COUNT(*) FROM users WHERE tenant_id=? AND role='admin' AND deleted_at IS NULL AND disabled_at IS NULL;",
        (tenant_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _tenant_slug_by_id(con: sqlite3.Connection, tenant_id: str) -> str:
    try:
        row = con.execute("SELECT slug FROM tenants WHERE id=? LIMIT 1;", (tenant_id,)).fetchone()
        if not row:
            return ""
        return _norm(row["slug"] if isinstance(row, sqlite3.Row) else row[0])
    except Exception:
        return ""


# ---------------- KV (one-shot flags) ----------------
def ensure_kv_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_kv (
            k TEXT PRIMARY KEY,
            v TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    con.commit()


def kv_get(con: sqlite3.Connection, key: str) -> str:
    try:
        if not _table_exists(con, "auth_kv"):
            return ""
        row = con.execute("SELECT v FROM auth_kv WHERE k=? LIMIT 1;", (key,)).fetchone()
        if not row:
            return ""
        return str(row["v"] if isinstance(row, sqlite3.Row) else row[0] or "")
    except Exception:
        return ""


def kv_set(con: sqlite3.Connection, key: str, val: str) -> None:
    ensure_kv_schema(con)
    con.execute(
        """
        INSERT INTO auth_kv(k, v, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at;
        """,
        (key, val, _now_utc_iso()),
    )
    con.commit()


# ---------------- Schema / migrations ----------------
def ensure_auth_schema(con: sqlite3.Connection) -> None:
    """
    ✅ SOLO STARTUP / MIGRATIONS
    - Crea tablas y ALTERs.
    - No llamar en runtime normal (requests/acciones habituales).
    """
    con.execute("PRAGMA foreign_keys=ON;")

    # KV (flags one-shot)
    ensure_kv_schema(con)

    # Tenants (base)
    if not _table_exists(con, "tenants"):
        con.execute(
            """
            CREATE TABLE tenants (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        con.commit()

    # Tenants columns (soft-disable + soft-delete + updated_at)
    cols_t = _columns(con, "tenants")
    if "enabled" not in cols_t:
        con.execute("ALTER TABLE tenants ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;")
        con.commit()
    if "deleted_at" not in cols_t:
        con.execute("ALTER TABLE tenants ADD COLUMN deleted_at TEXT;")
        con.commit()
    if "updated_at" not in cols_t:
        con.execute("ALTER TABLE tenants ADD COLUMN updated_at TEXT;")
        con.commit()

    con.execute("CREATE INDEX IF NOT EXISTS idx_tenants_created_at ON tenants(created_at);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tenants_enabled ON tenants(enabled);")

    # Users (base)
    if not _table_exists(con, "users"):
        con.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login_at TEXT,
                disabled_at TEXT,
                deleted_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
                UNIQUE(tenant_id, email)
            );
            """
        )
        con.commit()

    cols_u = _columns(con, "users")

    # ---- MIGRACIÓN desde esquema viejo (username PK) ----
    if "username" in cols_u and "tenant_id" not in cols_u:
        _ensure_default_tenant(con)
        con.execute("ALTER TABLE users RENAME TO users_legacy;")
        con.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login_at TEXT,
                disabled_at TEXT,
                deleted_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
                UNIQUE(tenant_id, email)
            );
            """
        )

        default_tenant_id = _get_default_tenant_id(con)
        legacy_rows = con.execute(
            "SELECT username, password_hash, role, created_at FROM users_legacy;"
        ).fetchall()

        for r in legacy_rows:
            username = _norm(r[0] or "")
            pw = (r[1] or "").strip()
            role = (r[2] or "admin").strip().lower()
            created_at = r[3] or None
            if not username or not pw:
                continue
            con.execute(
                """
                INSERT OR IGNORE INTO users(id, tenant_id, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP));
                """,
                (str(uuid.uuid4()), default_tenant_id, username, pw, role, created_at),
            )
        con.commit()
        cols_u = _columns(con, "users")

    # last_login_at
    if "last_login_at" not in cols_u:
        con.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT;")
        con.commit()

    # disabled_at / deleted_at / updated_at
    cols_u = _columns(con, "users")
    if "disabled_at" not in cols_u:
        con.execute("ALTER TABLE users ADD COLUMN disabled_at TEXT;")
        con.commit()
    if "deleted_at" not in cols_u:
        con.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT;")
        con.commit()
    if "updated_at" not in cols_u:
        con.execute("ALTER TABLE users ADD COLUMN updated_at TEXT;")
        con.commit()

    required = {"id", "tenant_id", "email", "password_hash", "role", "created_at"}
    cols_u = _columns(con, "users")
    if not required.issubset(cols_u):
        raise RuntimeError("Esquema inconsistente. Borra la BD o migra manualmente.")

    # Índices útiles
    con.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant_role ON users(tenant_id, role);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant_created ON users(tenant_id, created_at);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(tenant_id, deleted_at, disabled_at);")
    con.commit()

    # ✅ Extra schemas “SaaS serio”
    ensure_login_attempts_schema(con)
    ensure_sessions_schema(con)
    ensure_superadmins_schema(con)


def assert_auth_schema(con: sqlite3.Connection) -> None:
    """
    ✅ Runtime-safe: SOLO verifica que el esquema existe.
    Si falta algo, falla (fail-closed).
    """
    if not _table_exists(con, "tenants") or not _table_exists(con, "users"):
        raise RuntimeError("Auth schema missing. Ejecuta migraciones/startup.")

    cols_t = _columns(con, "tenants")
    cols_u = _columns(con, "users")

    need_t = {"id", "slug", "name", "created_at"}
    need_u = {"id", "tenant_id", "email", "password_hash", "role", "created_at"}

    if not need_t.issubset(cols_t) or not need_u.issubset(cols_u):
        raise RuntimeError("Auth schema invalid/incomplete. Ejecuta migraciones/startup.")


def _ensure_default_tenant(con: sqlite3.Connection) -> None:
    if _count_tenants(con) > 0:
        return
    con.execute(
        "INSERT OR IGNORE INTO tenants(id, slug, name, enabled) VALUES (?, 'default', 'Default', 1);",
        (str(uuid.uuid4()),),
    )
    con.commit()


def _get_default_tenant_id(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT id FROM tenants WHERE slug='default' LIMIT 1;").fetchone()
    if row:
        return str(row[0]) if not isinstance(row, sqlite3.Row) else str(row["id"])
    _ensure_default_tenant(con)
    row2 = con.execute("SELECT id FROM tenants WHERE slug='default' LIMIT 1;").fetchone()
    return str(row2[0]) if not isinstance(row2, sqlite3.Row) else str(row2["id"])


def _tenant_by_slug(con: sqlite3.Connection, slug: str) -> Optional[sqlite3.Row]:
    slug = _norm(slug)
    if not slug:
        return None
    return con.execute("SELECT * FROM tenants WHERE slug=? LIMIT 1;", (slug,)).fetchone()


def _tenant_is_active(row: sqlite3.Row) -> bool:
    """Respeta enabled/deleted_at si existen."""
    try:
        enabled = int(row["enabled"]) if "enabled" in row.keys() else 1
        deleted_at = (row["deleted_at"] or "").strip() if "deleted_at" in row.keys() else ""
        return enabled == 1 and not deleted_at
    except Exception:
        return False


def _validate_slug_or_raise(slug: str, *, allow_reserved: bool = False) -> str:
    s = _norm(slug)
    if not s:
        raise ValueError("slug obligatorio")
    if not SLUG_RE.fullmatch(s):
        raise ValueError("slug inválido. Usa [a-z0-9-] y longitud 3..40")
    if (s in _RESERVED_SLUGS) and not allow_reserved:
        raise ValueError(f"slug reservado: '{s}'")
    return s


def _create_tenant(con: sqlite3.Connection, slug: str, name: str) -> str:
    slug_n = _validate_slug_or_raise(slug, allow_reserved=False)
    name = (name or "").strip()
    if not name:
        raise ValueError("name obligatorio")
    tenant_id = str(uuid.uuid4())
    con.execute(
        "INSERT INTO tenants(id, slug, name, enabled, updated_at) VALUES (?, ?, ?, 1, ?);",
        (tenant_id, slug_n, name, _now_utc_iso()),
    )
    con.commit()
    return tenant_id


# ---------------- Login attempts (rate-limit) ----------------
def ensure_login_attempts_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts_v2 (
            tenant_id TEXT NOT NULL,
            email TEXT NOT NULL,
            fails INTEGER NOT NULL DEFAULT 0,
            first_fail_ts INTEGER,
            last_fail_ts INTEGER,
            locked_until_ts INTEGER,
            PRIMARY KEY (tenant_id, email)
        );
        """
    )
    con.commit()


def assert_login_attempts_schema(con: sqlite3.Connection) -> None:
    if not _table_exists(con, "login_attempts_v2"):
        raise RuntimeError("login_attempts_v2 missing. Ejecuta migraciones/startup.")


def purge_login_attempts(con: sqlite3.Connection, *, older_than_days: int = 90) -> None:
    """✅ Evita crecimiento infinito: borra attempts antiguos."""
    try:
        assert_login_attempts_schema(con)
        cutoff = int(time.time()) - int(max(7, older_than_days)) * 24 * 3600
        con.execute(
            "DELETE FROM login_attempts_v2 WHERE COALESCE(last_fail_ts, first_fail_ts, 0) < ?;",
            (cutoff,),
        )
        con.commit()
    except Exception:
        pass


def _resolve_tenant_id_for_login_attempts(con: sqlite3.Connection, tenant_slug: str) -> Optional[str]:
    """
    ✅ P0: si el tenant no existe / está disabled -> NO escribimos en attempts
    """
    t = _tenant_by_slug(con, tenant_slug)
    if not t:
        return None
    try:
        if isinstance(t, sqlite3.Row) and not _tenant_is_active(t):
            return None
    except Exception:
        return None
    return str(t["id"]) if isinstance(t, sqlite3.Row) else str(t[0])


def _login_lock_status(con: sqlite3.Connection, tenant_slug: str, email: str) -> tuple[bool, int]:
    assert_auth_schema(con)
    assert_login_attempts_schema(con)

    tenant_slug = _norm(tenant_slug)
    email = _norm(email)
    if not tenant_slug or not email:
        return False, 0

    tenant_id = _resolve_tenant_id_for_login_attempts(con, tenant_slug)
    if not tenant_id:
        return False, 0

    now = int(time.time())

    row = con.execute(
        """
        SELECT locked_until_ts FROM login_attempts_v2
        WHERE tenant_id=? AND email=? LIMIT 1;
        """,
        (tenant_id, email),
    ).fetchone()

    if not row:
        return False, 0

    locked_until_raw = row["locked_until_ts"] if isinstance(row, sqlite3.Row) else row[0]
    locked_until = int(locked_until_raw or 0)

    if locked_until > now:
        return True, locked_until - now
    return False, 0


def _register_login_failure(con: sqlite3.Connection, tenant_slug: str, email: str) -> None:
    """
    Política:
    - 5 fallos en 10 minutos => bloqueo 10 minutos
    """
    assert_auth_schema(con)
    assert_login_attempts_schema(con)

    tenant_slug = _norm(tenant_slug)
    email = _norm(email)
    if not tenant_slug or not email:
        return

    tenant_id = _resolve_tenant_id_for_login_attempts(con, tenant_slug)
    if not tenant_id:
        return  # ✅ no registramos para tenant inválido

    now = int(time.time())
    window = 10 * 60
    lock_seconds = 10 * 60
    max_fails = 5

    row = con.execute(
        """
        SELECT fails, first_fail_ts, locked_until_ts
        FROM login_attempts_v2
        WHERE tenant_id=? AND email=? LIMIT 1;
        """,
        (tenant_id, email),
    ).fetchone()

    if not row:
        con.execute(
            """
            INSERT INTO login_attempts_v2(tenant_id, email, fails, first_fail_ts, last_fail_ts, locked_until_ts)
            VALUES (?, ?, 1, ?, ?, NULL);
            """,
            (tenant_id, email, now, now),
        )
        con.commit()
        return

    fails_raw = row["fails"] if isinstance(row, sqlite3.Row) else row[0]
    first_raw = row["first_fail_ts"] if isinstance(row, sqlite3.Row) else row[1]
    locked_raw = row["locked_until_ts"] if isinstance(row, sqlite3.Row) else row[2]

    fails = int(fails_raw or 0)
    first_ts = int(first_raw or now)
    locked_until = int(locked_raw or 0)

    if now - first_ts > window:
        fails = 0
        first_ts = now

    fails += 1

    # si ya está bloqueado, solo registramos timestamp
    if locked_until > now:
        con.execute(
            """
            UPDATE login_attempts_v2
            SET fails=?, last_fail_ts=?
            WHERE tenant_id=? AND email=?;
            """,
            (fails, now, tenant_id, email),
        )
        con.commit()
        return

    new_locked_until = None
    if fails >= max_fails:
        # ✅ al bloquear, resetea ventana para no crecer infinito
        fails = max_fails
        first_ts = now
        new_locked_until = now + lock_seconds

    con.execute(
        """
        UPDATE login_attempts_v2
        SET fails=?, first_fail_ts=?, last_fail_ts=?, locked_until_ts=?
        WHERE tenant_id=? AND email=?;
        """,
        (fails, first_ts, now, new_locked_until, tenant_id, email),
    )
    con.commit()


def _register_login_success(con: sqlite3.Connection, tenant_slug: str, email: str) -> None:
    assert_auth_schema(con)
    assert_login_attempts_schema(con)
    tenant_slug = _norm(tenant_slug)
    email = _norm(email)
    if not tenant_slug or not email:
        return
    tenant_id = _resolve_tenant_id_for_login_attempts(con, tenant_slug)
    if not tenant_id:
        return
    con.execute("DELETE FROM login_attempts_v2 WHERE tenant_id=? AND email=?;", (tenant_id, email))
    con.commit()


# ---------------- Sessions (server-side) ----------------
def ensure_sessions_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            user_agent TEXT,
            ip TEXT,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id, created_at);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, created_at);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);")
    con.commit()


def assert_sessions_schema(con: sqlite3.Connection) -> None:
    if not _table_exists(con, "sessions"):
        raise RuntimeError("sessions table missing. Ejecuta migraciones/startup.")


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        ss = (s or "").strip()
        if not ss:
            return None
        if "T" not in ss and " " in ss:
            ss = ss.replace(" ", "T")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _revoke_old_sessions_if_needed(con: sqlite3.Connection, user_id: str, *, keep: int) -> None:
    """✅ limita sesiones activas por usuario (best-effort)."""
    try:
        assert_sessions_schema(con)
        keep = max(1, int(keep))
        now_iso = _now_utc_iso()
        rows = con.execute(
            """
            SELECT id
            FROM sessions
            WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?
            ORDER BY created_at DESC
            """,
            (user_id, now_iso),
        ).fetchall()

        if not rows:
            return

        ids = [r["id"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]
        if len(ids) <= keep:
            return

        to_revoke = ids[keep:]
        for sid in to_revoke:
            con.execute(
                "UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL;",
                (_now_utc_iso(), sid),
            )
        con.commit()
    except Exception:
        pass


def create_session(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    hours: int = DEFAULT_SESSION_HOURS,
    user_agent: str = "",
    ip: str = "",
) -> str:
    assert_auth_schema(con)
    assert_sessions_schema(con)

    # ✅ valida consistencia user_id pertenece al tenant_id
    row = con.execute(
        "SELECT 1 FROM users WHERE id=? AND tenant_id=? AND deleted_at IS NULL LIMIT 1;",
        (user_id, tenant_id),
    ).fetchone()
    if not row:
        raise ValueError("No se puede crear sesión: usuario no pertenece al tenant o está borrado.")

    # ✅ limit sessions per user
    _revoke_old_sessions_if_needed(con, user_id, keep=max(1, MAX_SESSIONS_PER_USER - 1))

    sid = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(hours=max(1, hours))
    con.execute(
        """
        INSERT INTO sessions(id, tenant_id, user_id, expires_at, user_agent, ip)
        VALUES (?,?,?,?,?,?);
        """,
        (sid, tenant_id, user_id, expires.isoformat(), (user_agent or "")[:200], (ip or "")[:80]),
    )
    con.commit()
    return sid


def get_session(con: sqlite3.Connection, session_id: str) -> Optional[sqlite3.Row]:
    """
    ✅ Incluye tenant en el JOIN para poder invalidar sesiones
    si el tenant está disabled/deleted.
    """
    assert_sessions_schema(con)
    assert_auth_schema(con)

    sid = (session_id or "").strip()
    if not sid:
        return None

    return con.execute(
        """
        SELECT
            s.id, s.tenant_id, s.user_id, s.created_at, s.expires_at, s.revoked_at,
            s.user_agent AS sess_user_agent, s.ip AS sess_ip,
            u.email, u.role, u.disabled_at, u.deleted_at,
            t.enabled AS tenant_enabled, t.deleted_at AS tenant_deleted_at
        FROM sessions s
        JOIN users u   ON u.id = s.user_id
        JOIN tenants t ON t.id = s.tenant_id
        WHERE s.id=? LIMIT 1;
        """,
        (sid,),
    ).fetchone()


def is_session_valid(row: sqlite3.Row) -> bool:
    """
    ✅ FIX: sqlite.Row no tiene .get()
    ✅ Además valida tenant activo.
    """
    try:
        # revoked
        if ("revoked_at" in row.keys()) and ((row["revoked_at"] or "").strip()):
            return False

        # tenant activo
        tenant_enabled = 1
        if "tenant_enabled" in row.keys():
            tenant_enabled = int(row["tenant_enabled"] or 0)
        tenant_deleted_at = ""
        if "tenant_deleted_at" in row.keys():
            tenant_deleted_at = (row["tenant_deleted_at"] or "").strip()

        if tenant_enabled != 1 or tenant_deleted_at:
            return False

        # usuario activo
        disabled_at = (row["disabled_at"] or "").strip() if "disabled_at" in row.keys() else ""
        deleted_at = (row["deleted_at"] or "").strip() if "deleted_at" in row.keys() else ""
        if disabled_at or deleted_at:
            return False

        exp = _parse_iso(row["expires_at"] or "")
        if exp is None:
            return False
        return datetime.now(timezone.utc) < exp
    except Exception:
        return False


def revoke_session(con: sqlite3.Connection, session_id: str) -> None:
    assert_sessions_schema(con)
    sid = (session_id or "").strip()
    if not sid:
        return
    con.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL;", (_now_utc_iso(), sid))
    con.commit()


def revoke_user_sessions(con: sqlite3.Connection, user_id: str) -> None:
    """✅ revocar TODAS las sesiones activas de un usuario."""
    try:
        assert_sessions_schema(con)
        uid = (user_id or "").strip()
        if not uid:
            return
        con.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL;",
            (_now_utc_iso(), uid),
        )
        con.commit()
    except Exception:
        pass


def purge_expired_sessions(con: sqlite3.Connection) -> None:
    """best effort cleanup"""
    try:
        assert_sessions_schema(con)
        now_iso = _now_utc_iso()
        con.execute("DELETE FROM sessions WHERE expires_at < ?;", (now_iso,))
        con.commit()
    except Exception:
        pass


# ---------------- Superadmins (no “ENV godmode”) ----------------
def ensure_superadmins_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS superadmins (
            email TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            note TEXT
        );
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_superadmins_enabled ON superadmins(enabled);")
    con.commit()


def assert_superadmins_schema(con: sqlite3.Connection) -> None:
    if not _table_exists(con, "superadmins"):
        raise RuntimeError("superadmins missing. Ejecuta migraciones/startup.")


def bootstrap_superadmins_from_env(con: sqlite3.Connection) -> None:
    """
    ✅ One-shot bootstrap.
    Evita que ENV se convierta en backdoor permanente.
    """
    assert_auth_schema(con)
    assert_superadmins_schema(con)
    ensure_kv_schema(con)

    if kv_get(con, "superadmins_bootstrapped_from_env") == "1":
        return

    raw = (
        os.getenv("SUPERADMIN_EMAIL")
        or os.getenv("BDA_SUPERADMIN_EMAIL")
        or os.getenv("BDA_SUPERADMIN_USER")
        or ""
    ).strip()
    if not raw:
        return

    emails = [_norm(x) for x in raw.split(",") if _norm(x)]
    if not emails:
        return

    for e in emails:
        con.execute(
            """
            INSERT INTO superadmins(email, enabled, note)
            VALUES (?, 1, 'bootstrapped_from_env')
            ON CONFLICT(email) DO UPDATE SET enabled=1;
            """,
            (e,),
        )
    con.commit()
    kv_set(con, "superadmins_bootstrapped_from_env", "1")


def is_superadmin_db(con: sqlite3.Connection, actor_identity: str) -> bool:
    assert_superadmins_schema(con)
    actor = _norm(actor_identity)
    if not actor:
        return False
    row = con.execute("SELECT enabled FROM superadmins WHERE email=? LIMIT 1;", (actor,)).fetchone()
    if not row:
        return False
    enabled = row["enabled"] if isinstance(row, sqlite3.Row) else row[0]
    return int(enabled or 0) == 1


def is_superadmin(actor_identity: str) -> bool:
    """Legacy helper (sin DB). Mantener por compatibilidad."""
    raw = (
        os.getenv("SUPERADMIN_EMAIL")
        or os.getenv("BDA_SUPERADMIN_EMAIL")
        or os.getenv("BDA_SUPERADMIN_USER")
        or ""
    ).strip()
    if not raw:
        return False
    actor = _norm(actor_identity)
    allow = {_norm(x) for x in raw.split(",") if _norm(x)}
    return actor in allow


def is_superadmin_scoped(con: sqlite3.Connection, actor_identity: str) -> bool:
    """✅ SaaS serio: usa DB como fuente de verdad."""
    try:
        return is_superadmin_db(con, actor_identity)
    except Exception:
        return False


# ---------------- Audit helpers (auth-side) ----------------
def audit_auth_action(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    tenant_slug: str,
    actor_email: str,
    action: str,
    meta: Optional[dict] = None,
) -> None:
    """
    Best-effort: escribe en audit_log si existe.
    """
    try:
        if not _table_exists(con, "audit_log"):
            return
        con.execute(
            """
            INSERT INTO audit_log(tenant_id, tenant_slug, actor, actor_type, action, rid, ip, user_agent, path, status_code, ms, meta_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tenant_id,
                tenant_slug,
                _norm(actor_email),
                "panel",
                action,
                "",
                "",
                "",
                "auth.py",
                200,
                0,
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )
        con.commit()
    except Exception:
        pass


# ---------------- Bootstrap admin desde ENV (SIN Streamlit) ----------------
def ensure_admin_from_env(con: sqlite3.Connection) -> Tuple[bool, str]:
    """
    ✅ bootstrap controlado

    Reglas:
    - En prod: solo si AUTH_BOOTSTRAP_MODE=1
    - En dev: permitido si DEBUG=1 o BOOTSTRAP_MODE=1
    - Nunca actualiza password/rol si ya existe al menos 1 usuario en ese tenant (evita escalada por ENV)
    - En prod, slug 'default' requiere ALLOW_DEFAULT_TENANT_BOOTSTRAP=1

    ⚠️ Recovery por ENV eliminado (era backdoor).
    Si el tenant se queda sin admin, se recupera vía superadmin DB / procedimiento manual.
    """
    assert_auth_schema(con)

    # gating
    if ENV == "prod" and not BOOTSTRAP_MODE:
        return False, ""
    if ENV != "prod" and not (BOOTSTRAP_MODE or DEBUG):
        return False, ""

    tenant_slug = _norm(os.getenv("TENANT_SLUG") or os.getenv("BDA_TENANT_SLUG") or "default")
    tenant_name = (os.getenv("TENANT_NAME") or os.getenv("BDA_TENANT_NAME") or "Default").strip()

    if ENV == "prod" and tenant_slug == "default" and not ALLOW_DEFAULT_TENANT_BOOTSTRAP:
        return False, "En prod, bootstrap de tenant 'default' está bloqueado (ALLOW_DEFAULT_TENANT_BOOTSTRAP=1 para permitir)."

    admin_user = _norm(
        os.getenv("ADMIN_EMAIL")
        or os.getenv("BDA_ADMIN_EMAIL")
        or os.getenv("BDA_ADMIN_USER")
        or "admin@local"
    )
    admin_pwd = (os.getenv("ADMIN_PASSWORD") or os.getenv("BDA_ADMIN_PASSWORD") or "").strip()

    # ✅ si estás en bootstrap mode pero falta password, avisa claro
    if BOOTSTRAP_MODE and not admin_pwd:
        logger.warning("AUTH_BOOTSTRAP_MODE=1 pero falta ADMIN_PASSWORD (no se creará admin).")
        return False, "AUTH_BOOTSTRAP_MODE=1 pero falta ADMIN_PASSWORD."

    if not admin_pwd:
        return False, admin_user

    try:
        validate_password_policy(admin_pwd)
    except Exception as e:
        return False, f"ADMIN_PASSWORD inválida: {e}. No se creó/actualizó."

    try:
        t = _tenant_by_slug(con, tenant_slug)
        if not t:
            slug_n = _validate_slug_or_raise(tenant_slug, allow_reserved=True)
            tenant_id = str(uuid.uuid4())
            con.execute(
                "INSERT INTO tenants(id, slug, name, enabled, updated_at) VALUES (?, ?, ?, 1, ?);",
                (tenant_id, slug_n, tenant_name or "Default", _now_utc_iso()),
            )
            con.commit()
        else:
            if isinstance(t, sqlite3.Row) and not _tenant_is_active(t):
                return False, f"Tenant '{tenant_slug}' deshabilitado/borrado."
            tenant_id = str(t["id"]) if isinstance(t, sqlite3.Row) else str(t[0])

        # usuarios existentes
        row_count = con.execute(
            "SELECT COUNT(*) FROM users WHERE tenant_id=?;",
            (tenant_id,),
        ).fetchone()
        n_users = int((row_count[0] if row_count else 0) or 0)

        # ✅ Caso normal: si ya hay users, NO tocamos nada (evita escalada por ENV)
        if n_users > 0:
            # Si el tenant se quedó sin admins -> NO recuperamos por ENV (procedimiento superadmin)
            admins = _count_admins(con, tenant_id)
            if admins <= 0:
                return True, f"{admin_user} (NOTA: tenant sin admins activos; recovery requiere superadmin DB)"
            return True, admin_user

        # primer admin
        pwd_hash = hash_password(admin_pwd)
        con.execute(
            "INSERT INTO users (id, tenant_id, email, password_hash, role, updated_at) VALUES (?, ?, ?, ?, 'admin', ?);",
            (str(uuid.uuid4()), tenant_id, admin_user, pwd_hash, _now_utc_iso()),
        )
        con.commit()
        return True, admin_user

    except Exception as e:
        try:
            con.rollback()
        except Exception:
            pass
        return False, f"No se pudo inicializar admin desde ENV (se continúa). Motivo: {e}"


# ---------------- Auth logic (login) ----------------
def _validate_login_identity(email_or_user: str) -> str:
    x = _norm(email_or_user)
    if not x or " " in x:
        raise ValueError("usuario/email inválido")
    if not USERNAME_RE.fullmatch(x):
        raise ValueError("usuario/email inválido")
    # ✅ si parece email, valida un mínimo
    if "@" in x and not EMAIL_SIMPLE_RE.fullmatch(x):
        raise ValueError("email inválido")
    return x


def _user_is_active(row: sqlite3.Row) -> bool:
    try:
        deleted_at = (row["deleted_at"] or "").strip() if "deleted_at" in row.keys() else ""
        disabled_at = (row["disabled_at"] or "").strip() if "disabled_at" in row.keys() else ""
        return (not deleted_at) and (not disabled_at)
    except Exception:
        return False


def _check_user(
    con: sqlite3.Connection,
    tenant_slug: str,
    email: str,
    password: str,
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """
    Returns: (valid, role_or_reason, tenant_id, user_id)
    """
    tenant_slug = _norm(tenant_slug)
    try:
        email_n = _validate_login_identity(email)
    except Exception:
        _fake_verify_cost(password)
        return False, "user", None, None

    password = (password or "").strip()
    if not tenant_slug or not email_n or not password:
        _fake_verify_cost(password)
        return False, "user", None, None

    assert_auth_schema(con)
    assert_login_attempts_schema(con)

    locked, _secs = _login_lock_status(con, tenant_slug, email_n)
    if locked:
        return False, "locked", None, None

    t = _tenant_by_slug(con, tenant_slug)
    if not t:
        _fake_verify_cost(password)
        _register_login_failure(con, tenant_slug, email_n)  # will NO-OP if tenant invalid
        return False, "user", None, None

    if isinstance(t, sqlite3.Row) and not _tenant_is_active(t):
        _fake_verify_cost(password)
        _register_login_failure(con, tenant_slug, email_n)
        return False, "user", None, None

    tenant_id = str(t["id"]) if isinstance(t, sqlite3.Row) else str(t[0])

    row = con.execute(
        "SELECT id, email, password_hash, role, disabled_at, deleted_at FROM users WHERE tenant_id=? AND email=? LIMIT 1;",
        (tenant_id, email_n),
    ).fetchone()

    if not row:
        _fake_verify_cost(password)
        _register_login_failure(con, tenant_slug, email_n)
        return False, "user", tenant_id, None

    if isinstance(row, sqlite3.Row) and not _user_is_active(row):
        _fake_verify_cost(password)
        _register_login_failure(con, tenant_slug, email_n)
        return False, "user", tenant_id, (row["id"] if isinstance(row, sqlite3.Row) else row[0])

    user_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
    stored = (row["password_hash"] if isinstance(row, sqlite3.Row) else row[2] or "").strip()
    role = (row["role"] if isinstance(row, sqlite3.Row) else row[3] or "user").strip().lower()

    # 1) Argon2
    if stored.startswith("$argon2"):
        ok = verify_password(stored, password)
        if ok:
            try:
                con.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP, updated_at=? WHERE id=?;", (_now_utc_iso(), user_id))
                con.commit()
            except Exception:
                pass
            _register_login_success(con, tenant_slug, email_n)
            return True, role, tenant_id, user_id
        _register_login_failure(con, tenant_slug, email_n)
        return False, "user", tenant_id, user_id

    # 2) Legacy SHA-256 (silencioso)
    if _SHA256_HEX_RE.fullmatch(stored):
        if _consteq(_sha256_legacy(password), stored.lower()):
            try:
                new_hash = hash_password(password)
                con.execute(
                    "UPDATE users SET password_hash=?, last_login_at=CURRENT_TIMESTAMP, updated_at=? WHERE id=?;",
                    (new_hash, _now_utc_iso(), user_id),
                )
                con.commit()
            except Exception:
                pass
            _register_login_success(con, tenant_slug, email_n)
            return True, role, tenant_id, user_id
        _register_login_failure(con, tenant_slug, email_n)
        return False, "user", tenant_id, user_id

    _fake_verify_cost(password)
    _register_login_failure(con, tenant_slug, email_n)
    return False, "user", tenant_id, user_id


# ---------------- Streamlit UI functions (solo UI) ----------------
def _bootstrap_first_admin(con: sqlite3.Connection) -> None:
    """
    UI bootstrap SOLO si no hay users/tenants.
    """
    assert_auth_schema(con)

    if _count_users(con) > 0 and _count_tenants(con) > 0:
        return

    _ui_warn("No hay organizaciones creadas. Inicializa el sistema.")

    if st is None:
        raise RuntimeError("Bootstrap UI requiere Streamlit, pero no está disponible.")

    with st.form("bootstrap_admin"):
        tenant_name = st.text_input("Nombre Organización", value="Mi ONG")
        tenant_slug = st.text_input("Slug", value="mi-ong")
        u = st.text_input("Email Admin", value="admin@ong.local")
        p1 = st.text_input("Contraseña", type="password")
        p2 = st.text_input("Repite Contraseña", type="password")
        if st.form_submit_button("Crear Admin Master"):
            if p1 != p2:
                st.error("Las contraseñas no coinciden.")
                st.stop()
            try:
                validate_password_policy(p1)
            except Exception as e:
                st.error(str(e))
                st.stop()

            slug_n = _validate_slug_or_raise(tenant_slug, allow_reserved=False)

            tenant_id = str(uuid.uuid4())
            con.execute(
                "INSERT INTO tenants(id, slug, name, enabled, updated_at) VALUES (?, ?, ?, 1, ?);",
                (tenant_id, slug_n, tenant_name.strip() or "Mi ONG", _now_utc_iso()),
            )
            con.execute(
                "INSERT INTO users (id, tenant_id, email, password_hash, role, updated_at) VALUES (?, ?, ?, ?, 'admin', ?);",
                (str(uuid.uuid4()), tenant_id, _norm(u), hash_password(p1), _now_utc_iso()),
            )
            con.commit()
            st.success("Admin creado ✅")
            st.rerun()
    st.stop()


def _streamlit_best_effort_client_meta() -> tuple[str, str]:
    """
    Best-effort: Streamlit no garantiza IP/UA. Intentamos leer headers si existen.
    """
    ip = ""
    ua = ""
    if st is None:
        return ip, ua
    try:
        ctx = getattr(st, "context", None)
        if ctx is not None:
            headers = getattr(ctx, "headers", None)
            if headers:
                ua = (headers.get("user-agent") or headers.get("User-Agent") or "")[:200]
                xff = (headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                xri = (headers.get("x-real-ip") or headers.get("X-Real-IP") or "").strip()
                ip = (xff or xri or "")[:80]
    except Exception:
        pass
    return ip, ua


def login_gate(con: sqlite3.Connection) -> None:
    """
    ✅ Streamlit gate con SESIÓN SERVER-SIDE:
    - session_id en session_state
    - validación contra tabla sessions en cada rerun
    - expiración y revocación centralizada
    """
    assert_auth_schema(con)
    assert_login_attempts_schema(con)
    assert_sessions_schema(con)
    assert_superadmins_schema(con)

    # Cleanup best-effort
    try:
        purge_login_attempts(con)
    except Exception:
        pass

    # Bootstrap superadmins desde ENV (ONE-SHOT)
    try:
        bootstrap_superadmins_from_env(con)
    except Exception:
        pass

    ok, msg = ensure_admin_from_env(con)
    if not ok and msg:
        _ui_warn(msg)

    _bootstrap_first_admin(con)

    if st is None:
        raise RuntimeError("login_gate requiere Streamlit.")

    # Inicializar estado
    if "authenticated" not in st.session_state:
        st.session_state.update(
            {
                "authenticated": False,
                "username": None,
                "role": None,
                "tenant_id": None,
                "tenant_slug": None,
                "session_id": None,
            }
        )

    # ✅ Si está autenticado, validar sesión server-side
    if st.session_state.get("authenticated") and st.session_state.get("session_id"):
        try:
            purge_expired_sessions(con)
            srow = get_session(con, st.session_state["session_id"])
            if srow is None or not is_session_valid(srow):
                st.session_state.update(
                    {
                        "authenticated": False,
                        "username": None,
                        "role": None,
                        "tenant_id": None,
                        "tenant_slug": None,
                        "session_id": None,
                    }
                )
                _ui_warn("Sesión caducada o tenant deshabilitado. Vuelve a iniciar sesión.")
            else:
                # ✅ tenant_slug se deriva de DB por tenant_id (fuente de verdad)
                tid = str(srow["tenant_id"])
                slug_real = _tenant_slug_by_id(con, tid)

                st.session_state.update(
                    {
                        "authenticated": True,
                        "username": srow["email"],
                        "role": (srow["role"] or "").strip().lower(),
                        "tenant_id": tid,
                        "tenant_slug": slug_real,
                    }
                )
                return
        except Exception:
            st.session_state.update(
                {
                    "authenticated": False,
                    "username": None,
                    "role": None,
                    "tenant_id": None,
                    "tenant_slug": None,
                    "session_id": None,
                }
            )

    st.markdown("## 🔒 Acceso")
    with st.form("login_form"):
        tenant_slug = st.text_input("Organización (slug)")
        user = st.text_input("Email/usuario")
        pwd = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Entrar", width="stretch"):
            locked, secs = _login_lock_status(con, tenant_slug, user)
            if locked:
                st.error(f"Demasiados intentos. Prueba de nuevo en {secs} segundos.")
                st.stop()

            valid, role_or_reason, tenant_id, user_id = _check_user(con, tenant_slug, user, pwd)
            if valid and tenant_id and user_id:
                # ✅ rotate sessions on login (reduce reutilización/robo)
                revoke_user_sessions(con, user_id)

                ip, ua = _streamlit_best_effort_client_meta()

                sid = create_session(
                    con,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    hours=DEFAULT_SESSION_HOURS,
                    user_agent=ua or "streamlit",
                    ip=ip or "",
                )
                st.session_state.update(
                    {
                        "authenticated": True,
                        "username": _norm(user),
                        "role": (role_or_reason or "").strip().lower(),
                        "tenant_id": tenant_id,
                        "tenant_slug": _tenant_slug_by_id(con, tenant_id),  # ✅ fuente de verdad
                        "session_id": sid,
                    }
                )
                st.rerun()
            else:
                if role_or_reason == "locked":
                    locked, secs = _login_lock_status(con, tenant_slug, user)
                    st.error(f"Cuenta temporalmente bloqueada. Espera {secs} segundos.")
                else:
                    st.error("Credenciales incorrectas.")
    st.stop()


def logout(con: sqlite3.Connection) -> None:
    """Optional helper: revoca la sesión actual (si existe)."""
    if st is None:
        return
    sid = st.session_state.get("session_id")
    if sid:
        try:
            revoke_session(con, sid)
        except Exception:
            pass
    st.session_state.update(
        {
            "authenticated": False,
            "username": None,
            "role": None,
            "tenant_id": None,
            "tenant_slug": None,
            "session_id": None,
        }
    )
    _ui_rerun()


# ===================== SaaS Admin API (helpers) =====================
def list_tenants(con: sqlite3.Connection) -> list[dict]:
    assert_auth_schema(con)
    rows = con.execute(
        "SELECT id, slug, name, enabled, deleted_at, created_at, updated_at FROM tenants ORDER BY created_at DESC;"
    ).fetchall()
    return [dict(r) for r in rows]


def create_tenant(con: sqlite3.Connection, slug: str, name: str, *, actor_email: str = "") -> str:
    assert_auth_schema(con)
    assert_superadmins_schema(con)

    if not is_superadmin_scoped(con, actor_email):
        raise ValueError("Solo superadmin puede crear tenants.")

    tenant_id = _create_tenant(con, slug=slug, name=name)

    try:
        audit_auth_action(
            con,
            tenant_id=tenant_id,
            tenant_slug=_norm(slug),
            actor_email=actor_email,
            action="auth.tenant.create",
            meta={"name": name},
        )
    except Exception:
        pass

    return tenant_id


def disable_tenant(con: sqlite3.Connection, actor_email: str, tenant_id: str) -> None:
    """✅ SaaS serio: deshabilitar tenant sin borrar datos."""
    assert_auth_schema(con)
    assert_superadmins_schema(con)
    if not is_superadmin_scoped(con, actor_email):
        raise ValueError("Solo superadmin puede deshabilitar tenants.")
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id obligatorio")
    con.execute("UPDATE tenants SET enabled=0, updated_at=? WHERE id=?;", (_now_utc_iso(), tid))
    con.commit()
    try:
        audit_auth_action(
            con,
            tenant_id=tid,
            tenant_slug=_tenant_slug_by_id(con, tid),
            actor_email=actor_email,
            action="auth.tenant.disable",
            meta={},
        )
    except Exception:
        pass


def soft_delete_tenant(con: sqlite3.Connection, actor_email: str, tenant_id: str) -> None:
    """✅ SaaS serio: soft delete tenant (marca deleted_at)."""
    assert_auth_schema(con)
    assert_superadmins_schema(con)
    if not is_superadmin_scoped(con, actor_email):
        raise ValueError("Solo superadmin puede borrar tenants.")
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id obligatorio")
    con.execute("UPDATE tenants SET deleted_at=?, enabled=0, updated_at=? WHERE id=?;", (_now_utc_iso(), _now_utc_iso(), tid))
    con.commit()
    try:
        audit_auth_action(
            con,
            tenant_id=tid,
            tenant_slug=_tenant_slug_by_id(con, tid),
            actor_email=actor_email,
            action="auth.tenant.soft_delete",
            meta={},
        )
    except Exception:
        pass


def get_tenant_id_by_slug(con: sqlite3.Connection, slug: str) -> Optional[str]:
    assert_auth_schema(con)
    t = _tenant_by_slug(con, slug)
    if not t:
        return None
    if isinstance(t, sqlite3.Row) and not _tenant_is_active(t):
        return None
    return str(t["id"]) if isinstance(t, sqlite3.Row) else str(t[0])


def list_users(con: sqlite3.Connection, tenant_id: Optional[str] = None) -> list[dict]:
    assert_auth_schema(con)
    if tenant_id:
        rows = con.execute(
            "SELECT id, tenant_id, email, role, created_at, last_login_at, disabled_at, deleted_at, updated_at FROM users WHERE tenant_id=? ORDER BY created_at DESC;",
            (tenant_id,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, tenant_id, email, role, created_at, last_login_at, disabled_at, deleted_at, updated_at FROM users ORDER BY created_at DESC;"
        ).fetchall()
    return [dict(r) for r in rows]


# ✅ create_user NO permite crear admins (solo user).
def create_user(
    con: sqlite3.Connection,
    tenant_id: str,
    email: str,
    password: str,
    role: str = "user",
    *,
    actor_email: str = "",
) -> str:
    """Función segura: solo crea 'user'. Para admins usa create_user_scoped()."""
    assert_auth_schema(con)

    email_n = _validate_login_identity(email)
    role_n = (role or "user").strip().lower()

    if not tenant_id:
        raise ValueError("tenant_id obligatorio")

    if role_n != "user":
        raise ValueError("create_user solo permite role='user'. Usa create_user_scoped para admins.")

    user_id = str(uuid.uuid4())
    try:
        con.execute(
            "INSERT INTO users(id, tenant_id, email, password_hash, role, updated_at) VALUES (?, ?, ?, ?, ?, ?);",
            (user_id, tenant_id, email_n, hash_password(password), "user", _now_utc_iso()),
        )
        con.commit()

        try:
            audit_auth_action(
                con,
                tenant_id=tenant_id,
                tenant_slug=_tenant_slug_by_id(con, tenant_id),
                actor_email=actor_email or email_n,
                action="auth.user.create",
                meta={"user_id": user_id, "email": email_n, "role": "user"},
            )
        except Exception:
            pass

        return user_id
    except sqlite3.IntegrityError:
        raise ValueError("Ese email/usuario ya existe en ese tenant.")


def create_user_scoped(
    con: sqlite3.Connection,
    actor_tenant_id: str,
    actor_email: str,
    tenant_id: str,
    email: str,
    password: str,
    role: str = "user",
) -> str:
    assert_auth_schema(con)
    assert_superadmins_schema(con)

    role_n = (role or "user").strip().lower()
    if role_n not in {"admin", "user"}:
        raise ValueError("role inválido (admin/user)")

    # Superadmin: puede en cualquier tenant
    if is_superadmin_scoped(con, actor_email):
        pass
    else:
        # No superadmin: solo dentro de su tenant
        if tenant_id != actor_tenant_id:
            raise ValueError("No puedes crear usuarios en otro tenant.")
        # y solo puede crear 'user' (admin creation reservado)
        if role_n == "admin":
            raise ValueError("Solo superadmin puede crear admins.")

    email_n = _validate_login_identity(email)
    user_id = str(uuid.uuid4())
    try:
        con.execute(
            "INSERT INTO users(id, tenant_id, email, password_hash, role, updated_at) VALUES (?, ?, ?, ?, ?, ?);",
            (user_id, tenant_id, email_n, hash_password(password), role_n, _now_utc_iso()),
        )
        con.commit()

        try:
            audit_auth_action(
                con,
                tenant_id=tenant_id,
                tenant_slug=_tenant_slug_by_id(con, tenant_id),
                actor_email=actor_email,
                action="auth.user.create.scoped",
                meta={"user_id": user_id, "email": email_n, "role": role_n},
            )
        except Exception:
            pass

        return user_id
    except sqlite3.IntegrityError:
        raise ValueError("Ese email/usuario ya existe en ese tenant.")


def _require_same_tenant_or_superadmin(
    con: sqlite3.Connection,
    actor_tenant_id: str,
    actor_email: str,
    target_user_id: str,
) -> sqlite3.Row:
    assert_auth_schema(con)
    assert_superadmins_schema(con)

    row = con.execute(
        "SELECT id, tenant_id, role, email, disabled_at, deleted_at FROM users WHERE id=? LIMIT 1;",
        (target_user_id,),
    ).fetchone()

    if not row:
        raise ValueError("Usuario no existe")

    target_tenant_id = row["tenant_id"] if isinstance(row, sqlite3.Row) else row[1]

    if is_superadmin_scoped(con, actor_email):
        return row

    if not actor_tenant_id or target_tenant_id != actor_tenant_id:
        raise ValueError("No tienes permiso para gestionar usuarios de otro tenant.")

    return row


def _deny_self_management_for_non_superadmin(con: sqlite3.Connection, actor_email: str, target_email: str, action: str) -> None:
    if is_superadmin_scoped(con, actor_email):
        return
    if _norm(actor_email) == _norm(target_email):
        raise ValueError(f"No puedes {action} tu propio usuario desde el panel.")


def set_user_role(con: sqlite3.Connection, actor_tenant_id: str, actor_email: str, user_id: str, role: str) -> None:
    assert_auth_schema(con)
    role_n = (role or "").strip().lower()
    if role_n not in {"admin", "user"}:
        raise ValueError("role inválido")

    row = _require_same_tenant_or_superadmin(con, actor_tenant_id, actor_email, user_id)
    tenant_id = row["tenant_id"] if isinstance(row, sqlite3.Row) else row[1]
    current_role = (row["role"] if isinstance(row, sqlite3.Row) else row[2] or "").strip().lower()
    target_email = row["email"] if isinstance(row, sqlite3.Row) else row[3]

    _deny_self_management_for_non_superadmin(con, actor_email, target_email, action="cambiar el rol de")

    if current_role == "admin" and role_n != "admin":
        if _count_admins(con, tenant_id) <= 1:
            raise ValueError("No puedes quitar el rol admin al ÚLTIMO admin del tenant.")

    con.execute("UPDATE users SET role=?, updated_at=? WHERE id=?;", (role_n, _now_utc_iso(), user_id))
    con.commit()

    # recomendado: si quitas admin, revoca sesiones
    if current_role == "admin" and role_n != "admin":
        revoke_user_sessions(con, user_id)

    try:
        audit_auth_action(
            con,
            tenant_id=tenant_id,
            tenant_slug=_tenant_slug_by_id(con, tenant_id),
            actor_email=actor_email,
            action="auth.user.role.set",
            meta={"user_id": user_id, "target_email": target_email, "role": role_n},
        )
    except Exception:
        pass


def disable_user(con: sqlite3.Connection, actor_tenant_id: str, actor_email: str, user_id: str) -> None:
    """✅ SaaS serio: deshabilitar user sin borrar."""
    assert_auth_schema(con)
    row = _require_same_tenant_or_superadmin(con, actor_tenant_id, actor_email, user_id)
    tenant_id = row["tenant_id"] if isinstance(row, sqlite3.Row) else row[1]
    role = (row["role"] if isinstance(row, sqlite3.Row) else row[2] or "").strip().lower()
    target_email = row["email"] if isinstance(row, sqlite3.Row) else row[3]

    _deny_self_management_for_non_superadmin(con, actor_email, target_email, action="deshabilitar")

    if role == "admin" and _count_admins(con, tenant_id) <= 1:
        raise ValueError("No puedes deshabilitar al ÚLTIMO admin del tenant.")

    con.execute(
        "UPDATE users SET disabled_at=?, updated_at=? WHERE id=? AND disabled_at IS NULL AND deleted_at IS NULL;",
        (_now_utc_iso(), _now_utc_iso(), user_id),
    )
    con.commit()
    revoke_user_sessions(con, user_id)

    try:
        audit_auth_action(
            con,
            tenant_id=tenant_id,
            tenant_slug=_tenant_slug_by_id(con, tenant_id),
            actor_email=actor_email,
            action="auth.user.disable",
            meta={"user_id": user_id, "target_email": target_email},
        )
    except Exception:
        pass


def enable_user(con: sqlite3.Connection, actor_tenant_id: str, actor_email: str, user_id: str) -> None:
    """✅ re-habilitar user."""
    assert_auth_schema(con)
    row = _require_same_tenant_or_superadmin(con, actor_tenant_id, actor_email, user_id)
    tenant_id = row["tenant_id"] if isinstance(row, sqlite3.Row) else row[1]
    target_email = row["email"] if isinstance(row, sqlite3.Row) else row[3]

    _deny_self_management_for_non_superadmin(con, actor_email, target_email, action="habilitar")

    con.execute(
        "UPDATE users SET disabled_at=NULL, updated_at=? WHERE id=? AND deleted_at IS NULL;",
        (_now_utc_iso(), user_id),
    )
    con.commit()

    try:
        audit_auth_action(
            con,
            tenant_id=tenant_id,
            tenant_slug=_tenant_slug_by_id(con, tenant_id),
            actor_email=actor_email,
            action="auth.user.enable",
            meta={"user_id": user_id, "target_email": target_email},
        )
    except Exception:
        pass


def reset_user_password(con: sqlite3.Connection, actor_tenant_id: str, actor_email: str, user_id: str, new_password: str) -> None:
    assert_auth_schema(con)
    row = _require_same_tenant_or_superadmin(con, actor_tenant_id, actor_email, user_id)
    tenant_id = row["tenant_id"] if isinstance(row, sqlite3.Row) else row[1]
    target_email = row["email"] if isinstance(row, sqlite3.Row) else row[3]

    _deny_self_management_for_non_superadmin(con, actor_email, target_email, action="resetear la contraseña de")

    con.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?;", (hash_password(new_password), _now_utc_iso(), user_id))
    con.commit()

    # ✅ revocar sesiones activas tras reset
    revoke_user_sessions(con, user_id)

    try:
        audit_auth_action(
            con,
            tenant_id=tenant_id,
            tenant_slug=_tenant_slug_by_id(con, tenant_id),
            actor_email=actor_email,
            action="auth.user.password.reset",
            meta={"user_id": user_id, "target_email": target_email},
        )
    except Exception:
        pass


def delete_user(con: sqlite3.Connection, actor_tenant_id: str, actor_email: str, user_id: str) -> None:
    """
    ✅ SOFT DELETE (no borrado físico).
    - marca deleted_at
    - revoca sesiones
    - respeta "último admin"
    - (opcional) anonimiza email para RGPD
    """
    assert_auth_schema(con)

    row = _require_same_tenant_or_superadmin(con, actor_tenant_id, actor_email, user_id)
    tenant_id = row["tenant_id"] if isinstance(row, sqlite3.Row) else row[1]
    role = (row["role"] if isinstance(row, sqlite3.Row) else row[2] or "").strip().lower()
    target_email = row["email"] if isinstance(row, sqlite3.Row) else row[3]

    _deny_self_management_for_non_superadmin(con, actor_email, target_email, action="borrar")

    if role == "admin" and _count_admins(con, tenant_id) <= 1:
        raise ValueError("No puedes borrar al ÚLTIMO admin del tenant.")

    revoke_user_sessions(con, user_id)

    new_email = target_email
    if ANONYMIZE_EMAIL_ON_DELETE:
        new_email = f"deleted+{user_id}@deleted.local"

    now = _now_utc_iso()
    con.execute(
        """
        UPDATE users
        SET deleted_at=?, disabled_at=COALESCE(disabled_at, ?), email=?, updated_at=?
        WHERE id=? AND deleted_at IS NULL;
        """,
        (now, now, new_email, now, user_id),
    )
    con.commit()

    try:
        audit_auth_action(
            con,
            tenant_id=tenant_id,
            tenant_slug=_tenant_slug_by_id(con, tenant_id),
            actor_email=actor_email,
            action="auth.user.soft_delete",
            meta={"user_id": user_id, "target_email": target_email, "role": role, "anonymized": bool(ANONYMIZE_EMAIL_ON_DELETE)},
        )
    except Exception:
        pass