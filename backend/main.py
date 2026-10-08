# backend/main.py  
from __future__ import annotations  
  
import io  
import os  
import zipfile  
import sqlite3  
import hashlib  
import hmac  
import logging  
import secrets  
import base64  
import time  
import json  
from dataclasses import dataclass  
from datetime import datetime, timezone, timedelta  
from pathlib import Path  
from typing import Optional, Tuple, List, Dict, Callable, Any  
  
from fastapi import (  
    FastAPI,  
    UploadFile,  
    File,  
    HTTPException,  
    Header,  
    Query,  
    Depends,  
    Request,  
)  
from fastapi.responses import StreamingResponse, JSONResponse  
from starlette.status import (  
    HTTP_400_BAD_REQUEST,  
    HTTP_401_UNAUTHORIZED,  
    HTTP_403_FORBIDDEN,  
    HTTP_409_CONFLICT,  
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,  
    HTTP_500_INTERNAL_SERVER_ERROR,  
    HTTP_202_ACCEPTED,  
)  
  
# ✅ CORS opcional  
try:  
    from fastapi.middleware.cors import CORSMiddleware  
except Exception:  
    CORSMiddleware = None  # type: ignore  
  
# ⚠️ mantenemos imports existentes  
from backend.generator import CertSystem  
from backend.auth import ensure_auth_schema, get_tenant_id_by_slug  
from backend.tenant_settings import get_tenant_settings  
  
# Excel row count safeguard  
try:  
    from openpyxl import load_workbook  
except Exception:  
    load_workbook = None  # type: ignore  
  
# anyio timeout (FastAPI uses it under the hood)  
try:  
    import anyio  
except Exception:  
    anyio = None  # type: ignore  
  
  
# ---------------- App ----------------  
  
app = FastAPI(title="NGO Certificates API", version="2.3.3")  
  
BASE_DIR = Path(__file__).resolve().parent.parent  
CONFIG_PATH = BASE_DIR / "config.json"  
  
DATA_DIR = BASE_DIR / "data"  
AUTH_DB = DATA_DIR / "app.sqlite3"  # auth/tenants/settings/api keys/audit  
BIZ_DB = DATA_DIR / "biz.sqlite3"   # donors/certs (negocio)  
  
  
# ---------------- Config ----------------  
  
MAX_UPLOAD_MB = int(os.getenv("API_MAX_UPLOAD_MB", "15"))  
  
# Circuit breakers  
MAX_EXCEL_ROWS = int(os.getenv("API_MAX_EXCEL_ROWS", "50000"))  
MAX_PROCESS_SECONDS = int(os.getenv("API_MAX_PROCESS_SECONDS", "35"))  
  
# ✅ DEV-only escape hatch (doble seguro)  
ENV = os.getenv("ENV", "prod").strip().lower()  
DEBUG = os.getenv("DEBUG", "0").strip() == "1"  
ALLOW_INSECURE_NO_KEYS = os.getenv("API_ALLOW_INSECURE_NO_KEYS", "0").strip() == "1"  
  
# ✅ En SaaS serio: obliga a especificar tenant  
REQUIRE_TENANT = os.getenv("API_REQUIRE_TENANT", "1").strip() == "1"  
  
# MASTER API KEY (break-glass)  
MASTER_API_KEY = os.getenv("API_MASTER_KEY", "").strip()  
MASTER_ALLOW_IPS = {ip.strip() for ip in os.getenv("API_MASTER_ALLOWLIST_IPS", "").split(",") if ip.strip()}  
ALLOW_MASTER = os.getenv("API_ALLOW_MASTER", "0").strip() == "1"  
  
# ✅ Optional: limitar MASTER a paths admin (evita accidentes)  
MASTER_ADMIN_ONLY = os.getenv("API_MASTER_ADMIN_ONLY", "1").strip() == "1"  
  
# ✅ HARDENING: MASTER está apagado por defecto; en prod SOLO si se habilita explícitamente  
MASTER_ENABLED = False  
if MASTER_API_KEY and ALLOW_MASTER and MASTER_ALLOW_IPS:  
    if ENV == "prod":  
        MASTER_ENABLED = bool(MASTER_ADMIN_ONLY)  # en prod solo admin paths  
    else:  
        MASTER_ENABLED = True  # en no-prod puedes usarlo (pero sigue allowlist)  
  
# Rate limit simple en memoria (1 instancia)  
# ⚠️ Single-instance: si escalas (2 réplicas), el RL se rompe. Mover a Redis/proxy.  
RL_GENERAR_PER_MIN = int(os.getenv("API_RL_GENERAR_PER_MIN", "10"))  
RL_INDIV_PER_MIN = int(os.getenv("API_RL_INDIV_PER_MIN", "30"))  
RL_ADMIN_PER_MIN = int(os.getenv("API_RL_ADMIN_PER_MIN", "60"))  
DISABLE_RL = os.getenv("API_DISABLE_RL", "0").strip() == "1"  
  
# Migraciones en runtime (solo si lo permites explícitamente)  
RUN_DB_MIGRATIONS_ON_STARTUP = os.getenv("RUN_DB_MIGRATIONS_ON_STARTUP", "0").strip() == "1"  
  
# CORS (solo si vas a consumir desde browser)  
CORS_ALLOW_ORIGINS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]  
  
# Proxy headers  
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0").strip() == "1"  
PROXY_ALLOWLIST_IPS = {ip.strip() for ip in os.getenv("PROXY_ALLOWLIST_IPS", "").split(",") if ip.strip()}  
  
# Admin hardening policy  
REQUIRE_ADMIN_IP_ALLOWLIST = os.getenv("REQUIRE_ADMIN_IP_ALLOWLIST", "0").strip() == "1"  
  
# Idempotency  
IDEMPOTENCY_TTL_MIN = int(os.getenv("API_IDEMPOTENCY_TTL_MIN", "30"))  # ventana de lock/replay-metadata  
# ✅ Si un "processing" se queda colgado (crash/timeout), permitimos recuperar lock tras X segundos  
IDEMPOTENCY_STALE_SECONDS = int(os.getenv("API_IDEMPOTENCY_STALE_SECONDS", str(max(60, MAX_PROCESS_SECONDS * 3))))  
  
# Upload hardening  
STRICT_XLSX_ONLY = os.getenv("API_STRICT_XLSX", "0").strip() == "1"  
  
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()  
  
# ✅ No pises el logging del runner (uvicorn/gunicorn). Solo configura si no hay handlers.  
_root = logging.getLogger()  
if not _root.handlers:  
    logging.basicConfig(level=LOG_LEVEL)  
logger = logging.getLogger("ngo-api")  
logger.setLevel(LOG_LEVEL)  
  
  
# ---------------- CORS (opcional) ----------------  
if CORSMiddleware is not None and CORS_ALLOW_ORIGINS:  
    app.add_middleware(  
        CORSMiddleware,  
        allow_origins=CORS_ALLOW_ORIGINS,  
        allow_credentials=True,  
        allow_methods=["GET", "POST", "OPTIONS"],  
        allow_headers=[  
            "Authorization",  
            "Content-Type",  
            "X-Tenant-Slug",  
            "X-Request-Id",  
            "X-Forwarded-For",  
            "X-Real-IP",  
            "Idempotency-Key",  
        ],  
        expose_headers=[  
            "X-Request-Id",  
            "X-Tenant",  
            "X-Key",  
            "X-Idempotency-Status",  
            "X-Idempotency-Result-SHA256",  
        ],  
        max_age=600,  
    )  
  
  
# ---------------- DB helpers ----------------  
  
def _connect_sqlite(db_path: Path) -> sqlite3.Connection:  
    db_path.parent.mkdir(parents=True, exist_ok=True)  
    con = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level=None)  
    con.row_factory = sqlite3.Row  
    con.execute("PRAGMA journal_mode=WAL;")  
    con.execute("PRAGMA foreign_keys=ON;")  
    con.execute("PRAGMA busy_timeout=5000;")  
    return con  
  
  
def connect_auth_db() -> sqlite3.Connection:  
    return _connect_sqlite(AUTH_DB)  
  
  
def connect_biz_db() -> sqlite3.Connection:  
    return _connect_sqlite(BIZ_DB)  
  
  
def _table_exists(con: sqlite3.Connection, table: str) -> bool:  
    row = con.execute(  
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",  
        (table,),  
    ).fetchone()  
    return row is not None  
  
  
def _columns(con: sqlite3.Connection, table: str) -> set[str]:  
    try:  
        return {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}  
    except Exception:  
        return set()  
  
  
def _tenant_is_enabled_if_supported(con: sqlite3.Connection, tenant_id: str) -> bool:  
    """  
    ✅ SaaS hardening:  
    Si la tabla tenants tiene columnas enabled/deleted_at, las respeta.  
    Si no existen, no bloquea (compatibilidad hacia atrás).  
    """  
    if not _table_exists(con, "tenants"):  
        return True  
  
    cols = _columns(con, "tenants")  
    has_enabled = "enabled" in cols  
    has_deleted = "deleted_at" in cols  
  
    if not (has_enabled or has_deleted):  
        return True  # legacy schema  
  
    try:  
        if has_enabled and has_deleted:  
            row = con.execute(  
                "SELECT enabled, deleted_at FROM tenants WHERE id=? LIMIT 1",  
                (tenant_id,),  
            ).fetchone()  
            if not row:  
                return False  
            enabled = int(row["enabled"] or 0)  
            deleted_at = (row["deleted_at"] or "").strip()  
            return (enabled == 1) and (not deleted_at)  
        elif has_enabled:  
            row = con.execute("SELECT enabled FROM tenants WHERE id=? LIMIT 1", (tenant_id,)).fetchone()  
            if not row:  
                return False  
            return int(row["enabled"] or 0) == 1  
        else:  
            row = con.execute("SELECT deleted_at FROM tenants WHERE id=? LIMIT 1", (tenant_id,)).fetchone()  
            if not row:  
                return False  
            return not (row["deleted_at"] or "").strip()  
    except Exception:  
        # fail-closed para SaaS serio  
        return False  
  
  
def resolve_tenant(con: sqlite3.Connection, tenant_slug: Optional[str]) -> Tuple[Optional[str], str]:  
    """  
    ✅ Anti-enumeración (P0):  
    - Esta función NO lanza 403 si el tenant no existe.  
    - Devuelve (None, slug) y el caller hará _auth_fail() con fake-verify.  
    """  
    slug_raw = (tenant_slug or "").strip().lower()  
  
    if REQUIRE_TENANT and not slug_raw:  
        raise HTTPException(  
            status_code=HTTP_400_BAD_REQUEST,  
            detail="Falta tenant. Usa header X-Tenant-Slug o query ?tenant_slug=...",  
        )  
  
    slug = slug_raw or "default"  
    tid = get_tenant_id_by_slug(con, slug)  
    if not tid:  
        return None, slug  
  
    if not _tenant_is_enabled_if_supported(con, tid):  
        return None, slug  
  
    return tid, slug  
  
  
def get_or_open_auth_con(request: Request) -> sqlite3.Connection:  
    """  
    ✅ Garantiza que TODA conexión auth_con abierta por endpoint  
    quede en request.state.auth_con para cierre al final del request.  
    """  
    con = getattr(request.state, "auth_con", None)  
    if con is None:  
        con = connect_auth_db()  
        request.state.auth_con = con  
    return con  
  
  
# ---------------- API keys + Audit schema ----------------  
  
def _maybe_add_column(con: sqlite3.Connection, table: str, col: str, decl: str) -> None:  
    cols = [r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]  
    if col not in cols:  
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl};")  
  
  
def ensure_api_keys_schema(con: sqlite3.Connection) -> None:  
    """  
    Tabla con:  
      - key_prefix: identificador corto para lookup rápido (no expone secreto)  
      - key_hash: hash fuerte PBKDF2 o legacy sha256  
      - algo: 'pbkdf2' o 'sha256_legacy'  
      - scopes: csv simple 'generate,individual,admin'  
    """  
    con.execute(  
        """  
        CREATE TABLE IF NOT EXISTS tenant_api_keys (  
            id INTEGER PRIMARY KEY AUTOINCREMENT,  
            tenant_id TEXT NOT NULL,  
            key_prefix TEXT NOT NULL,  
            key_hash TEXT NOT NULL,  
            algo TEXT NOT NULL DEFAULT 'pbkdf2',  
            scopes TEXT NOT NULL DEFAULT '',  
            label TEXT,  
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,  
            revoked_at TEXT  
        );  
        """  
    )  
  
    _maybe_add_column(con, "tenant_api_keys", "algo", "TEXT NOT NULL DEFAULT 'pbkdf2'")  
    _maybe_add_column(con, "tenant_api_keys", "scopes", "TEXT NOT NULL DEFAULT ''")  
    _maybe_add_column(con, "tenant_api_keys", "label", "TEXT")  
    _maybe_add_column(con, "tenant_api_keys", "revoked_at", "TEXT")  
  
    con.execute("CREATE INDEX IF NOT EXISTS idx_tenant_api_keys_tenant ON tenant_api_keys(tenant_id);")  
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_api_keys_prefix ON tenant_api_keys(tenant_id, key_prefix);")  
  
  
def ensure_audit_schema(con: sqlite3.Connection) -> None:  
    con.execute(  
        """  
        CREATE TABLE IF NOT EXISTS audit_log (  
            id INTEGER PRIMARY KEY AUTOINCREMENT,  
            tenant_id TEXT NOT NULL,  
            tenant_slug TEXT,  
            actor TEXT,  
            actor_type TEXT,  
            action TEXT NOT NULL,  
            rid TEXT,  
            ip TEXT,  
            user_agent TEXT,  
            path TEXT,  
            status_code INTEGER,  
            ms INTEGER,  
            meta_json TEXT,  
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP  
        );  
        """  
    )  
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id, created_at);")  
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at);")  
  
  
def ensure_idempotency_schema(con: sqlite3.Connection) -> None:  
    """  
    Idempotency mínimo “serio” SIN storage:  
      - evita reprocesado  
      - deja metadata para detectar replay  
  
    Cuando tengas S3/MinIO, aquí guardas storage_key del ZIP y ya puedes replay perfecto.  
    """  
    con.execute(  
        """  
        CREATE TABLE IF NOT EXISTS idempotency_keys (  
            id INTEGER PRIMARY KEY AUTOINCREMENT,  
            tenant_id TEXT NOT NULL,  
            scope TEXT NOT NULL,              -- 'generate'|'individual'  
            idem_key TEXT NOT NULL,           -- raw string  
            request_sha256 TEXT NOT NULL,     -- hash del input (excel + params)  
            status TEXT NOT NULL,             -- 'processing'|'done'|'failed'  
            result_sha256 TEXT,               -- hash del zip/pdf result (si done)  
            meta_json TEXT,  
            created_at TEXT NOT NULL,         -- ISO UTC (lo ponemos nosotros)  
            expires_at TEXT NOT NULL  
        );  
        """  
    )  
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_idem ON idempotency_keys(tenant_id, scope, idem_key);")  
    con.execute("CREATE INDEX IF NOT EXISTS idx_idem_exp ON idempotency_keys(expires_at);")  
    con.execute("CREATE INDEX IF NOT EXISTS idx_idem_created ON idempotency_keys(created_at);")  
  
  
def audit_event(  
    tenant_id: str,  
    *,  
    tenant_slug: str,  
    actor: str,  
    actor_type: str,  
    action: str,  
    rid: str,  
    ip: Optional[str],  
    user_agent: Optional[str],  
    path: str,  
    status_code: int,  
    ms: int,  
    meta: Optional[dict] = None,  
    con: Optional[sqlite3.Connection] = None,  
) -> None:  
    """  
    ✅ Auditoría persistida en AUTH_DB (best effort)  
    ✅ Reusa conexión si se le pasa `con` (evita abrir sqlite por evento)  
    """  
    try:  
        own = False  
        if con is None:  
            con = connect_auth_db()  
            own = True  
        try:  
            con.execute(  
                """  
                INSERT INTO audit_log(  
                    tenant_id, tenant_slug, actor, actor_type, action, rid, ip, user_agent, path, status_code, ms, meta_json  
                )  
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)  
                """,  
                (  
                    tenant_id,  
                    tenant_slug,  
                    actor,  
                    actor_type,  
                    action,  
                    rid,  
                    ip or "",  
                    user_agent or "",  
                    path or "",  
                    int(status_code),  
                    int(ms),  
                    json.dumps(meta or {}, ensure_ascii=False),  
                ),  
            )  
        finally:  
            if own:  
                con.close()  
    except Exception:  
        logger.exception("audit_event_failed action=%s tenant_id=%s rid=%s", action, tenant_id, rid)  
  
  
# ---------------- Tokens ----------------  
  
def parse_bearer_token(authorization: Optional[str]) -> Optional[str]:  
    if not authorization:  
        return None  
    parts = authorization.strip().split()  
    if len(parts) != 2:  
        return None  
    if parts[0].lower() != "bearer":  
        return None  
    token = parts[1].strip()  
    return token or None  
  
  
def tenant_has_any_keys(con: sqlite3.Connection, tenant_id: str) -> bool:  
    row = con.execute(  
        "SELECT 1 FROM tenant_api_keys WHERE tenant_id=? AND revoked_at IS NULL LIMIT 1",  
        (tenant_id,),  
    ).fetchone()  
    return row is not None  
  
  
# ---- Hash de API keys: PBKDF2 (sin dependencias) + soporte legacy sha256 ----  
  
def _pbkdf2_hash(raw_key: str, *, iterations: int = 200_000, salt_b64: Optional[str] = None) -> str:  
    if salt_b64:  
        salt = base64.urlsafe_b64decode(salt_b64.encode("utf-8"))  
    else:  
        salt = secrets.token_bytes(16)  
        salt_b64 = base64.urlsafe_b64encode(salt).decode("utf-8")  
  
    dk = hashlib.pbkdf2_hmac("sha256", raw_key.encode("utf-8"), salt, iterations, dklen=32)  
    dk_b64 = base64.urlsafe_b64encode(dk).decode("utf-8")  
    return f"pbkdf2$sha256${iterations}${salt_b64}${dk_b64}"  
  
  
def _pbkdf2_verify(raw_key: str, stored: str) -> bool:  
    try:  
        parts = stored.split("$")  
        if len(parts) != 5:  
            return False  
        scheme, alg, it_s, salt_b64, _dk_b64 = parts  
        if scheme != "pbkdf2":  
            return False  
        if alg != "sha256":  
            return False  
        iterations = int(it_s)  
        test = _pbkdf2_hash(raw_key, iterations=iterations, salt_b64=salt_b64)  
        return hmac.compare_digest(test, stored)  
    except Exception:  
        return False  
  
  
def _sha256_hex(s: str) -> str:  
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()  
  
  
def _sha256_bytes(b: bytes) -> str:  
    return hashlib.sha256(b).hexdigest()  
  
  
def _sha256_text(s: str) -> str:  
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()  
  
  
_ALLOWED_SCOPES = {"generate", "individual", "admin", "*"}  
_ALLOWED_KEY_ALGOS = {"pbkdf2", "sha256_legacy", "sha256"}  
  
  
def _normalize_scopes(scopes_csv: str) -> List[str]:  
    scopes: List[str] = []  
    for s in (scopes_csv or "").split(","):  
        t = s.strip().lower()  
        if t and t in _ALLOWED_SCOPES:  
            scopes.append(t)  
    out: List[str] = []  
    for s in scopes:  
        if s not in out:  
            out.append(s)  
    return out  
  
  
def _normalize_algo(algo: str) -> str:  
    a = (algo or "").strip().lower()  
    if a == "pbkdfdf2":  
        a = "pbkdf2"  
    if a == "sha256":  
        a = "sha256_legacy"  
    if a not in _ALLOWED_KEY_ALGOS:  
        a = "sha256_legacy"  
    return a  
  
  
def _extract_prefix_from_key(raw_key: str) -> Optional[str]:  
    """  
    Formato OBLIGATORIO:  
        k_<prefix>_<secret>  
    """  
    k = (raw_key or "").strip()  
    if not k.startswith("k_"):  
        return None  
    parts = k.split("_", 2)  
    if len(parts) != 3:  
        return None  
    prefix = parts[1].strip()  
    secret = parts[2].strip()  
    if not prefix or not secret:  
        return None  
    return prefix[:24]  
  
  
def verify_tenant_api_key_and_scopes(con: sqlite3.Connection, tenant_id: str, raw_key: str) -> Tuple[bool, List[str], str]:  
    """  
    Returns:  
      (ok, scopes, key_prefix)  
    """  
    prefix = _extract_prefix_from_key(raw_key)  
    if not prefix:  
        return False, [], ""  
  
    rows = con.execute(  
        """  
        SELECT key_prefix, key_hash, algo, scopes  
        FROM tenant_api_keys  
        WHERE tenant_id=? AND key_prefix=? AND revoked_at IS NULL  
        """,  
        (tenant_id, prefix),  
    ).fetchall()  
  
    for r in rows:  
        algo = _normalize_algo(r["algo"] or "pbkdf2")  
        scopes = _normalize_scopes(r["scopes"] or "")  
        kp = (r["key_prefix"] or "")[:24]  
  
        ok = False  
        if algo == "pbkdf2":  
            ok = _pbkdf2_verify(raw_key, r["key_hash"])  
        else:  
            ok = hmac.compare_digest(_sha256_hex(raw_key), r["key_hash"])  
  
        if ok:  
            # ✅ scopes vacío = key inválida (evita “keys rotas”)  
            if not scopes:  
                return False, [], ""  
            return True, scopes, kp  
  
    return False, [], ""  
  
  
# ---------------- Rate limit simple (memoria) ----------------  
  
_RL_BUCKET: Dict[str, List[float]] = {}  
  
  
def _rate_limit(key: str, max_per_min: int) -> None:  
    """  
    ⚠️ Single-instance only.  
    Si escalas, implementa Redis (token bucket) o rate-limit en proxy.  
    """  
    if DISABLE_RL:  
        return  
  
    now = time.time()  
    window_start = now - 60.0  
    bucket = _RL_BUCKET.get(key)  
    if not bucket:  
        _RL_BUCKET[key] = [now]  
        return  
    bucket[:] = [t for t in bucket if t >= window_start]  
    if len(bucket) >= max_per_min:  
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Rate limit excedido. Intenta más tarde.")  
    bucket.append(now)  
  
  
# ---------------- Request context ----------------  
  
@dataclass(frozen=True)  
class TenantContext:  
    tenant_id: str  
    tenant_slug: str  
    request_id: str  
    key_prefix: str  
    scopes: List[str]  
    client_ip: str  
    user_agent: str  
    actor_type: str  # api_key / admin_key / master / dev  
  
  
def _get_client_ip(request: Request) -> str:  
    """  
    ✅ IP real detrás de proxy:  
    - Solo confiamos en X-Forwarded-For / X-Real-IP si TRUST_PROXY_HEADERS=1  
      y el request viene de un proxy allowlisted (PROXY_ALLOWLIST_IPS), si hay allowlist.  
    """  
    direct_ip = ""  
    try:  
        if request.client:  
            direct_ip = request.client.host or ""  
    except Exception:  
        direct_ip = ""  
  
    if not TRUST_PROXY_HEADERS:  
        return direct_ip  
  
    if PROXY_ALLOWLIST_IPS and direct_ip and direct_ip not in PROXY_ALLOWLIST_IPS:  
        return direct_ip  
  
    xff = (request.headers.get("X-Forwarded-For") or "").strip()  
    if xff:  
        first = xff.split(",")[0].strip()  
        if first:  
            return first  
  
    xri = (request.headers.get("X-Real-IP") or "").strip()  
    if xri:  
        return xri  
  
    return direct_ip  
  
  
@app.middleware("http")  
async def request_meta_middleware(request: Request, call_next):  
    """  
    ✅ Fix #1:  
    - Setea headers ANTES de devolver response (stream-safe).  
    - Cierra auth_con al final del request.  
    """  
    rid = request.headers.get("X-Request-Id") or secrets.token_hex(12)  
    request.state.request_id = rid  
  
    started = datetime.now(timezone.utc)  
    response = None  
    try:  
        response = await call_next(request)  
  
        # headers antes del return (StreamingResponse safe)  
        response.headers["X-Request-Id"] = rid  
        response.headers.setdefault("X-Content-Type-Options", "nosniff")  
        response.headers.setdefault("Referrer-Policy", "no-referrer")  
        response.headers.setdefault("Cache-Control", "no-store")  
        response.headers.setdefault("X-Frame-Options", "DENY")  
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")  
  
        return response  
    except Exception as e:  
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)  
        logger.exception(  
            "request_crash rid=%s method=%s path=%s ms=%s err=%s",  
            rid,  
            request.method,  
            request.url.path,  
            elapsed_ms,  
            str(e),  
        )  
        raise  
    finally:  
        # close shared auth_con  
        try:  
            con = getattr(request.state, "auth_con", None)  
            if con is not None:  
                try:  
                    con.close()  
                except Exception:  
                    pass  
                request.state.auth_con = None  
        except Exception:  
            pass  
  
        try:  
            if response is not None:  
                elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)  
                logger.info(  
                    "request rid=%s method=%s path=%s ms=%s status=%s",  
                    rid,  
                    request.method,  
                    request.url.path,  
                    elapsed_ms,  
                    response.status_code,  
                )  
        except Exception:  
            pass  
  
  
# ---------------- Auth fail hardening ----------------  
  
_FAKE_HASH = _pbkdf2_hash("k_deadbeef_" + secrets.token_urlsafe(16))  
  
  
def _fake_verify_constant_time(token: str) -> None:  
    try:  
        _pbkdf2_verify(token or "x", _FAKE_HASH)  
    except Exception:  
        pass  
  
  
def _auth_fail(token_for_timing: str = "invalid") -> HTTPException:  
    """  
    ✅ Todas las rutas de fallo sensible pasan por aquí  
    (P0 anti-enumeración + timing más uniforme).  
    """  
    _fake_verify_constant_time(token_for_timing)  
    return HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Invalid credentials")  
  
  
def _audit_auth_fail_best_effort(  
    request: Request,  
    *,  
    tenant_id: str,  
    tenant_slug: str,  
    rid: str,  
    ip: str,  
    user_agent: str,  
    path: str,  
    reason: str,  
) -> None:  
    try:  
        con = getattr(request.state, "auth_con", None)  
        if con is None:  
            con = connect_auth_db()  
        audit_event(  
            tenant_id,  
            tenant_slug=tenant_slug,  
            actor="unknown",  
            actor_type="auth_fail",  
            action="api.auth.fail",  
            rid=rid,  
            ip=ip,  
            user_agent=user_agent,  
            path=path,  
            status_code=HTTP_403_FORBIDDEN,  
            ms=0,  
            meta={"reason": reason},  
            con=con,  
        )  
        try:  
            if con is not getattr(request.state, "auth_con", None):  
                con.close()  
        except Exception:  
            pass  
    except Exception:  
        pass  
  
  
def _tenant_admin_ip_allowlist(tenant_cfg: dict) -> set[str]:  
    raw = ""  
    try:  
        raw = (tenant_cfg.get("admin_ip_allowlist") or "").strip()  
        if not raw:  
            sec = tenant_cfg.get("security") or {}  
            raw = (sec.get("admin_ip_allowlist") or "").strip()  
    except Exception:  
        raw = ""  
  
    allow = set()  
    for ip in (raw or "").split(","):  
        t = ip.strip()  
        if t:  
            allow.add(t)  
    return allow  
  
  
def require_tenant_ctx(  
    request: Request,  
    authorization: Optional[str] = Header(default=None, alias="Authorization"),  
    x_tenant_slug: Optional[str] = Header(default=None, alias="X-Tenant-Slug"),  
    tenant_slug: Optional[str] = Query(default=None),  
) -> TenantContext:  
    """  
    ✅ Tenant guard + API key:  
    - Obliga Authorization: Bearer <API_KEY>  
    - Anti-flood: si token no cumple formato k_<prefix>_<secret> -> falla sin tocar DB  
    - Resuelve tenant por slug (AUTH_DB)  
    - Verifica que la key pertenece al tenant  
    - Devuelve scopes + key_prefix + actor_type  
    """  
    rid = getattr(request.state, "request_id", None) or secrets.token_hex(12)  
  
    token = parse_bearer_token(authorization)  
    if not token:  
        raise HTTPException(  
            status_code=HTTP_401_UNAUTHORIZED,  
            detail="Missing bearer token",  
            headers={"WWW-Authenticate": "Bearer"},  
        )  
  
    client_ip = _get_client_ip(request)  
    user_agent = request.headers.get("User-Agent", "") or ""  
    path = str(request.url.path)  
  
    # ✅ Si token no cumple formato, cortamos aquí (salvo master)  
    is_master_token = MASTER_ENABLED and secrets.compare_digest(token, MASTER_API_KEY)  
    if not is_master_token:  
        if _extract_prefix_from_key(token) is None:  
            _audit_auth_fail_best_effort(  
                request,  
                tenant_id="unknown",  
                tenant_slug=(x_tenant_slug or tenant_slug or "").strip().lower() or "unknown",  
                rid=rid,  
                ip=client_ip,  
                user_agent=user_agent,  
                path=path,  
                reason="bad_format",  
            )  
            raise _auth_fail(token)  
  
    con = get_or_open_auth_con(request)  
  
    # ✅ Fix #3: NO DDL en requests.  
    # Si falta tabla -> fail closed (y que lo arregle startup/migraciones)  
    if not _table_exists(con, "tenant_api_keys"):  
        _audit_auth_fail_best_effort(  
            request,  
            tenant_id="unknown",  
            tenant_slug=(x_tenant_slug or tenant_slug or "").strip().lower() or "unknown",  
            rid=rid,  
            ip=client_ip,  
            user_agent=user_agent,  
            path=path,  
            reason="missing_tenant_api_keys_table",  
        )  
        raise _auth_fail(token)  
  
    tid, slug = resolve_tenant(con, x_tenant_slug or tenant_slug)  
    if not tid:  
        _audit_auth_fail_best_effort(  
            request,  
            tenant_id="unknown",  
            tenant_slug=slug,  
            rid=rid,  
            ip=client_ip,  
            user_agent=user_agent,  
            path=path,  
            reason="tenant_not_found_or_disabled",  
        )  
        raise _auth_fail(token)  
  
    # Tenant sin keys: bloqueado salvo DEV explícito (nunca prod).  
    if not tenant_has_any_keys(con, tid):  
        allow_dev = (ENV != "prod") and DEBUG and ALLOW_INSECURE_NO_KEYS  
        if allow_dev:  
            logger.warning("DEV_INSECURE_NO_KEYS_ENABLED tenant_id=%s tenant=%s", tid, slug)  
            return TenantContext(  
                tenant_id=tid,  
                tenant_slug=slug,  
                request_id=rid,  
                key_prefix="DEV",  
                scopes=["*"],  
                client_ip=client_ip,  
                user_agent=user_agent,  
                actor_type="dev",  
            )  
        _audit_auth_fail_best_effort(  
            request,  
            tenant_id=tid,  
            tenant_slug=slug,  
            rid=rid,  
            ip=client_ip,  
            user_agent=user_agent,  
            path=path,  
            reason="no_keys_bootstrap_required",  
        )  
        raise _auth_fail(token)  
  
    # ✅ Fix #2: MASTER solo admin endpoints, siempre allowlisted  
    if is_master_token:  
        if client_ip and (client_ip not in MASTER_ALLOW_IPS):  
            _audit_auth_fail_best_effort(  
                request,  
                tenant_id=tid,  
                tenant_slug=slug,  
                rid=rid,  
                ip=client_ip,  
                user_agent=user_agent,  
                path=path,  
                reason="master_ip_not_allowlisted",  
            )  
            raise _auth_fail(token)  
  
        if MASTER_ADMIN_ONLY and not path.startswith("/admin/"):  
            _audit_auth_fail_best_effort(  
                request,  
                tenant_id=tid,  
                tenant_slug=slug,  
                rid=rid,  
                ip=client_ip,  
                user_agent=user_agent,  
                path=path,  
                reason="master_admin_only_policy",  
            )  
            raise _auth_fail(token)  
  
        return TenantContext(  
            tenant_id=tid,  
            tenant_slug=slug,  
            request_id=rid,  
            key_prefix="MASTER",  
            scopes=["*"],  
            client_ip=client_ip,  
            user_agent=user_agent,  
            actor_type="master",  
        )  
  
    ok, scopes, prefix = verify_tenant_api_key_and_scopes(con, tid, token)  
    if not ok:  
        _audit_auth_fail_best_effort(  
            request,  
            tenant_id=tid,  
            tenant_slug=slug,  
            rid=rid,  
            ip=client_ip,  
            user_agent=user_agent,  
            path=path,  
            reason="bad_key_or_hash",  
        )  
        raise _auth_fail(token)  
  
    scopes_lower = [s.lower() for s in scopes]  
    actor_type = "admin_key" if ("admin" in scopes_lower) else "api_key"  
  
    return TenantContext(  
        tenant_id=tid,  
        tenant_slug=slug,  
        request_id=rid,  
        key_prefix=(prefix or "KEY"),  
        scopes=scopes,  
        client_ip=client_ip,  
        user_agent=user_agent,  
        actor_type=actor_type,  
    )  
  
  
def require_scope(ctx: TenantContext, needed: str) -> None:  
    if "*" in ctx.scopes:  
        return  
    if needed.lower() not in [s.lower() for s in ctx.scopes]:  
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail=f"API key sin scope '{needed}'.")  
  
  
def require_admin_hardening(request: Request, ctx: TenantContext, tenant_cfg: dict) -> None:  
    if ENV == "dev" and DEBUG:  
        return  
  
    allow = _tenant_admin_ip_allowlist(tenant_cfg or {})  
  
    if REQUIRE_ADMIN_IP_ALLOWLIST and not allow:  
        raise HTTPException(  
            status_code=HTTP_403_FORBIDDEN,  
            detail="Admin bloqueado: falta admin_ip_allowlist (policy).",  
        )  
  
    if not allow:  
        return  
  
    ip = _get_client_ip(request)  
    if ip and ip not in allow:  
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Admin no permitido desde esta IP.")  
  
  
# ---------------- System ----------------  
  
system = CertSystem(CONFIG_PATH)  
  
  
def _as_download_zip(zip_bytes: bytes, filename: str, extra_headers: dict | None = None) -> StreamingResponse:  
    buf = io.BytesIO(zip_bytes)  
    headers = {  
        "Content-Disposition": f'attachment; filename="{filename}"',  
        "Cache-Control": "no-store",  
    }  
    if extra_headers:  
        headers.update({k: str(v) for k, v in extra_headers.items()})  
    return StreamingResponse(buf, media_type="application/zip", headers=headers)  
  
  
def _enforce_upload_filename_and_type(file: UploadFile) -> None:  
    name = (file.filename or "").lower()  
  
    # ✅ Fix #7: opción estricta  
    if STRICT_XLSX_ONLY:  
        if not name.endswith(".xlsx"):  
            raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Sube un Excel .xlsx (modo estricto).")  
    else:  
        # ⚠️ no aceptamos xlsm (macro-enabled)  
        if not name.endswith((".xlsx", ".xls")):  
            raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Sube un Excel .xlsx/.xls")  
  
    if file.content_type and file.content_type not in (  
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  
        "application/vnd.ms-excel",  
        "application/octet-stream",  
    ):  
        logger.warning("upload suspicious content-type=%s filename=%s", file.content_type, file.filename)  
  
  
def _content_length_guard(request: Request, max_bytes: int) -> None:  
    cl = request.headers.get("content-length")  
    if not cl:  
        return  
    try:  
        n = int(cl)  
        if n > max_bytes:  
            raise HTTPException(  
                status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,  
                detail=f"Archivo demasiado grande. Máx {MAX_UPLOAD_MB} MB.",  
            )  
    except ValueError:  
        return  
  
  
async def _read_upload_limited(request: Request, file: UploadFile, *, max_bytes: int) -> bytes:  
    _content_length_guard(request, max_bytes)  
    _enforce_upload_filename_and_type(file)  
  
    buf = io.BytesIO()  
    read_total = 0  
    chunk_size = 1024 * 1024  # 1MB  
  
    while True:  
        chunk = await file.read(chunk_size)  
        if not chunk:  
            break  
        read_total += len(chunk)  
        if read_total > max_bytes:  
            raise HTTPException(  
                status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,  
                detail=f"Archivo demasiado grande. Máx {MAX_UPLOAD_MB} MB.",  
            )  
        buf.write(chunk)  
  
    data = buf.getvalue()  
    if not data:  
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="El archivo está vacío.")  
    return data  
  
  
def _count_excel_rows_fast(excel_bytes: bytes) -> Optional[int]:  
    if load_workbook is None:  
        return None  
    try:  
        wb = load_workbook(filename=io.BytesIO(excel_bytes), read_only=True, data_only=True)  
        ws = wb.active  
        n = getattr(ws, "max_row", None)  
        if isinstance(n, int) and n > 0:  
            return n  
  
        cnt = 0  
        for _ in ws.iter_rows(values_only=True):  
            cnt += 1  
            if cnt > MAX_EXCEL_ROWS + 1:  
                break  
        return cnt  
    except Exception:  
        return None  
  
  
async def _run_with_timeout(fn: Callable[[], Any], seconds: int):  
    if anyio is None:  
        return fn()  
  
    async def _run():  
        return await anyio.to_thread.run_sync(fn, cancellable=True)  
  
    with anyio.fail_after(seconds):  
        return await _run()  
  
  
# ---------------- Idempotency ----------------  
  
def _idem_expires_at_iso(ttl_min: int) -> str:  
    return (datetime.now(timezone.utc) + timedelta(minutes=max(1, ttl_min))).isoformat()  
  
  
def _idem_purge_expired(con: sqlite3.Connection) -> None:  
    try:  
        now_iso = datetime.now(timezone.utc).isoformat()  
        con.execute("DELETE FROM idempotency_keys WHERE expires_at < ?", (now_iso,))  
    except Exception:  
        pass  
  
  
def _parse_iso_dt(s: str) -> Optional[datetime]:  
    try:  
        ss = (s or "").strip()  
        if not ss:  
            return None  
        # admite "YYYY-MM-DD HH:MM:SS" de sqlite legacy  
        if "T" not in ss and " " in ss:  
            ss = ss.replace(" ", "T")  
        dt = datetime.fromisoformat(ss)  
        if dt.tzinfo is None:  
            dt = dt.replace(tzinfo=timezone.utc)  
        return dt.astimezone(timezone.utc)  
    except Exception:  
        return None  
  
  
def _idem_try_lock(  
    con: sqlite3.Connection,  
    *,  
    tenant_id: str,  
    scope: str,  
    idem_key: str,  
    request_sha256: str,  
) -> Tuple[str, Optional[str], Optional[dict]]:  
    """  
    Returns:  
      status: 'locked'|'replay'|'conflict'  
      result_sha256, meta (si replay)  
    """  
    _idem_purge_expired(con)  
  
    row = con.execute(  
        """  
        SELECT status, request_sha256, result_sha256, meta_json, created_at  
        FROM idempotency_keys  
        WHERE tenant_id=? AND scope=? AND idem_key=?  
        """,  
        (tenant_id, scope, idem_key),  
    ).fetchone()  
  
    now = datetime.now(timezone.utc)  
  
    if row is None:  
        con.execute(  
            """  
            INSERT INTO idempotency_keys(tenant_id, scope, idem_key, request_sha256, status, result_sha256, meta_json, created_at, expires_at)  
            VALUES (?,?,?,?,?,?,?,?,?)  
            """,  
            (  
                tenant_id,  
                scope,  
                idem_key,  
                request_sha256,  
                "processing",  
                "",  
                json.dumps({}, ensure_ascii=False),  
                now.isoformat(),  
                _idem_expires_at_iso(IDEMPOTENCY_TTL_MIN),  
            ),  
        )  
        return "locked", None, None  
  
    prev_req = (row["request_sha256"] or "")  
    st = (row["status"] or "").lower()  
  
    if prev_req != request_sha256:  
        return "conflict", None, None  
  
    if st == "done":  
        meta = {}  
        try:  
            meta = json.loads(row["meta_json"] or "{}")  
        except Exception:  
            meta = {}  
        return "replay", (row["result_sha256"] or ""), meta  
  
    # ✅ Fix #5: si se quedó en processing demasiado tiempo, permitimos "steal lock"  
    if st == "processing":  
        created = _parse_iso_dt(row["created_at"] or "")  
        if created is not None:  
            age = (now - created).total_seconds()  
            if age >= float(IDEMPOTENCY_STALE_SECONDS):  
                con.execute(  
                    """  
                    UPDATE idempotency_keys  
                    SET created_at=?, expires_at=?, status='processing', result_sha256='', meta_json=?  
                    WHERE tenant_id=? AND scope=? AND idem_key=?  
                    """,  
                    (  
                        now.isoformat(),  
                        _idem_expires_at_iso(IDEMPOTENCY_TTL_MIN),  
                        json.dumps({"stolen": True, "stolen_at": now.isoformat()}, ensure_ascii=False),  
                        tenant_id,  
                        scope,  
                        idem_key,  
                    ),  
                )  
                return "locked", None, None  
  
    return "conflict", None, None  
  
  
def _idem_mark_done(  
    con: sqlite3.Connection,  
    *,  
    tenant_id: str,  
    scope: str,  
    idem_key: str,  
    result_sha256: str,  
    meta: dict,  
) -> None:  
    con.execute(  
        """  
        UPDATE idempotency_keys  
        SET status='done', result_sha256=?, meta_json=?, expires_at=?  
        WHERE tenant_id=? AND scope=? AND idem_key=?  
        """,  
        (  
            result_sha256,  
            json.dumps(meta or {}, ensure_ascii=False),  
            _idem_expires_at_iso(IDEMPOTENCY_TTL_MIN),  
            tenant_id,  
            scope,  
            idem_key,  
        ),  
    )  
  
  
def _idem_mark_failed(con: sqlite3.Connection, *, tenant_id: str, scope: str, idem_key: str, meta: dict) -> None:  
    con.execute(  
        """  
        UPDATE idempotency_keys  
        SET status='failed', meta_json=?, expires_at=?  
        WHERE tenant_id=? AND scope=? AND idem_key=?  
        """,  
        (json.dumps(meta or {}, ensure_ascii=False), _idem_expires_at_iso(IDEMPOTENCY_TTL_MIN), tenant_id, scope, idem_key),  
    )  
  
  
# ---------------- API key management (admin) ----------------  
  
def _make_api_key() -> Tuple[str, str]:  
    prefix = secrets.token_hex(5)  # 10 chars  
    secret = secrets.token_urlsafe(32)  
    raw = f"k_{prefix}_{secret}"  
    return raw, prefix[:24]  
  
  
def _now_utc_iso() -> str:  
    return datetime.now(timezone.utc).isoformat()  
  
  
def _validate_scopes_policy(requester: TenantContext, scopes_list: List[str]) -> List[str]:  
    """  
    ✅ Política SaaS seria y CONSISTENTE:  
  
    - Las keys 'admin' existen para gestión de API keys y endpoints admin.  
    - Una key 'admin' NO puede mezclar generate/individual (evita “superkeys”).  
    - MASTER siempre puede crear 'admin'.  
    - Una admin_key EXISTENTE puede crear/rotar otras admin_key (admin-only).  
    """  
    scopes_list = [s.lower() for s in scopes_list]  
  
    if "admin" in scopes_list and "*" not in scopes_list:  
        if requester.actor_type in ("master", "admin_key"):  
            return ["admin"]  
        raise HTTPException(  
            status_code=HTTP_403_FORBIDDEN,  
            detail="No autorizado a crear API keys con scope 'admin'.",  
        )  
  
    scopes_list = [s for s in scopes_list if s != "admin"]  
    if not scopes_list:  
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Scopes vacíos o inválidos.")  
    return scopes_list  
  
  
def create_api_key(con: sqlite3.Connection, tenant_id: str, *, scopes_csv: str, label: str = "") -> Dict[str, str]:  
    raw, prefix = _make_api_key()  
    key_hash = _pbkdf2_hash(raw)  
  
    con.execute(  
        """  
        INSERT INTO tenant_api_keys(tenant_id, key_prefix, key_hash, algo, scopes, label)  
        VALUES (?,?,?,?,?,?)  
        """,  
        (tenant_id, prefix, key_hash, "pbkdf2", scopes_csv or "", label or ""),  
    )  
    return {"raw_key": raw, "key_prefix": prefix, "scopes": scopes_csv or "", "label": label or ""}  
  
  
def revoke_api_key(con: sqlite3.Connection, tenant_id: str, prefix: str) -> None:  
    con.execute(  
        """  
        UPDATE tenant_api_keys  
        SET revoked_at=?  
        WHERE tenant_id=? AND key_prefix=? AND revoked_at IS NULL  
        """,  
        (_now_utc_iso(), tenant_id, prefix[:24]),  
    )  
  
  
def list_api_keys(con: sqlite3.Connection, tenant_id: str) -> List[dict]:  
    rows = con.execute(  
        """  
        SELECT key_prefix, scopes, label, created_at, revoked_at  
        FROM tenant_api_keys  
        WHERE tenant_id=?  
        ORDER BY id DESC  
        """,  
        (tenant_id,),  
    ).fetchall()  
    return [  
        {  
            "key_prefix": r["key_prefix"],  
            "scopes": r["scopes"] or "",  
            "label": r["label"] or "",  
            "created_at": r["created_at"],  
            "revoked_at": r["revoked_at"],  
        }  
        for r in rows  
    ]  
  
  
# ---------------- Startup (schema once) ----------------  
  
@app.on_event("startup")  
def _startup_init():  
    DATA_DIR.mkdir(parents=True, exist_ok=True)  
  
    con = None  
    try:  
        con = connect_auth_db()  
        ensure_auth_schema(con)        # tenants/users/settings (según tu auth.py)  
        ensure_api_keys_schema(con)    # tenant_api_keys  
        ensure_audit_schema(con)       # audit_log  
        ensure_idempotency_schema(con) # idempotency  
  
        if RUN_DB_MIGRATIONS_ON_STARTUP:  
            logger.warning("MIGRATIONS_RAN_ON_STARTUP env=%s auth_db=%s", ENV, str(AUTH_DB))  
        else:  
            logger.info("startup_ok env=%s auth_db=%s (infra schema ensured)", ENV, str(AUTH_DB))  
    finally:  
        try:  
            if con:  
                con.close()  
        except Exception:  
            pass  
  
  
# ---------------- Routes ----------------  
  
@app.get("/health")  
def health():  
    if ENV == "prod":  
        return {"ok": True, "version": app.version}  
    return {"ok": True, "version": app.version, "env": ENV, "master_enabled": MASTER_ENABLED}  
  
  
def _params_json_normalized(params: dict) -> str:  
    # ✅ Fix #6: normalización estable para request_sha  
    try:  
        return json.dumps(params or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)  
    except Exception:  
        return "{}"  
  
  
@app.post("/generar")  
async def generar(  
    request: Request,  
    ctx: TenantContext = Depends(require_tenant_ctx),  
    file: UploadFile = File(...),  
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),  
):  
    require_scope(ctx, "generate")  
  
    # ✅ MASTER nunca puede ejecutar generate/individual (solo admin)  
    if ctx.actor_type in ("admin_key", "master"):  
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Esta key no puede ejecutar 'generate'.")  
  
    _rate_limit(f"{ctx.tenant_id}:{ctx.key_prefix}:generate", RL_GENERAR_PER_MIN)  
  
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024  
    excel_bytes = await _read_upload_limited(request, file, max_bytes=max_bytes)  
  
    n_rows = _count_excel_rows_fast(excel_bytes)  
    if n_rows is not None and n_rows > MAX_EXCEL_ROWS:  
        raise HTTPException(  
            status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,  
            detail=f"Excel demasiado grande: {n_rows} filas. Máx {MAX_EXCEL_ROWS}.",  
        )  
  
    excel_hash = _sha256_bytes(excel_bytes)  
  
    started = datetime.now(timezone.utc)  
    status_code = 200  
    summary: dict = {}  
  
    auth_con: Optional[sqlite3.Connection] = None  
    idem_status = ""  
    idem_result_sha = ""  
    zip_bytes: bytes = b""  
  
    try:  
        auth_con = get_or_open_auth_con(request)  
        tenant_cfg = get_tenant_settings(auth_con, ctx.tenant_id) or {}  
  
        # ✅ Idempotency (lock)  
        if idempotency_key:  
            idem_key = (idempotency_key or "").strip()  
            if len(idem_key) > 200:  
                raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Idempotency-Key demasiado largo.")  
  
            params_json = _params_json_normalized({"tenant_id": ctx.tenant_id, "scope": "generate"})  
            req_sha = _sha256_hex(f"{excel_hash}|{params_json}")  
  
            st, rsha, meta = _idem_try_lock(  
                auth_con,  
                tenant_id=ctx.tenant_id,  
                scope="generate",  
                idem_key=idem_key,  
                request_sha256=req_sha,  
            )  
  
            if st == "replay":  
                return JSONResponse(  
                    status_code=HTTP_202_ACCEPTED,  
                    content={  
                        "idempotency": "already_done",  
                        "result_sha256": rsha,  
                        "meta": meta or {},  
                        "note": "Procesado previamente. Replay binario requiere storage (S3/MinIO) para re-entregar el ZIP.",  
                    },  
                    headers={  
                        "X-Request-Id": ctx.request_id,  
                        "X-Tenant": ctx.tenant_slug,  
                        "X-Key": ctx.key_prefix,  
                        "X-Idempotency-Status": "replay",  
                        "X-Idempotency-Result-SHA256": rsha or "",  
                    },  
                )  
  
            if st == "conflict":  
                raise HTTPException(  
                    status_code=HTTP_409_CONFLICT,  
                    detail={"idempotency": "conflict", "note": "Idempotency-Key en uso o usado con otro payload."},  
                )  
            idem_status = "locked"  
  
        def _work():
            biz_con = connect_biz_db()
            auth_con2 = connect_auth_db()
            try:
                return system.generate_zip_from_excel_bytes(
                    excel_bytes,
                    tenant_cfg=tenant_cfg,
                    con_biz=biz_con,
                    con_auth=auth_con2,
                    tenant_id=ctx.tenant_id,
                    progress_cb=None,
                    return_mode="bytes",
                )
            finally:
                try: biz_con.close()
                except Exception: pass
                try: auth_con2.close()
                except Exception: pass
  
        zip_bytes, summary = await _run_with_timeout(_work, MAX_PROCESS_SECONDS)  
        zip_sha = _sha256_bytes(zip_bytes)  
        idem_result_sha = zip_sha  
  
        if idempotency_key:  
            _idem_mark_done(  
                auth_con,  
                tenant_id=ctx.tenant_id,  
                scope="generate",  
                idem_key=(idempotency_key or "").strip(),  
                result_sha256=zip_sha,  
                meta={"summary": summary, "excel_sha256": excel_hash},  
            )  
            idem_status = "done"  
  
    except HTTPException as he:  
        status_code = int(getattr(he, "status_code", 400) or 400)  
        try:  
            if auth_con is not None and idempotency_key and idem_status == "locked":  
                _idem_mark_failed(  
                    auth_con,  
                    tenant_id=ctx.tenant_id,  
                    scope="generate",  
                    idem_key=(idempotency_key or "").strip(),  
                    meta={"error": "http_exception", "status_code": status_code},  
                )  
        except Exception:  
            pass  
        raise  
    except ValueError as e:  
        status_code = 400  
        try:  
            if auth_con is not None and idempotency_key and idem_status == "locked":  
                _idem_mark_failed(  
                    auth_con,  
                    tenant_id=ctx.tenant_id,  
                    scope="generate",  
                    idem_key=(idempotency_key or "").strip(),  
                    meta={"error": "value_error", "detail": str(e)},  
                )  
        except Exception:  
            pass  
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e))  
    except Exception as e:  
        status_code = 500  
        logger.exception(  
            "generar_failed rid=%s tenant_id=%s tenant=%s key=%s scope=generate err=%s",  
            ctx.request_id, ctx.tenant_id, ctx.tenant_slug, ctx.key_prefix, str(e),  
        )  
        try:  
            if auth_con is not None and idempotency_key and idem_status == "locked":  
                _idem_mark_failed(  
                    auth_con,  
                    tenant_id=ctx.tenant_id,  
                    scope="generate",  
                    idem_key=(idempotency_key or "").strip(),  
                    meta={"error": "exception", "detail": str(e)},  
                )  
        except Exception:  
            pass  
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Error generando ZIP.")  
    finally:  
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)  
  
        meta = {  
            "rows": n_rows,  
            "excel_sha256": excel_hash,  
        }  
        try:  
            meta.update({  
                "total": int(summary.get("total", 0)),  
                "cert_ok": int(summary.get("cert_ok", summary.get("ok", 0))),  
                "cert_review": int(summary.get("cert_review", 0)),  
                "cert_invalid": int(summary.get("cert_invalid", 0)),  
                "cert_crash": int(summary.get("cert_crash", 0)),  
                "carta_ok": int(summary.get("carta_ok", 0)),  
                "carta_review": int(summary.get("carta_review", 0)),  
                "carta_invalid": int(summary.get("carta_invalid", 0)),  
                "carta_crash": int(summary.get("carta_crash", 0)),  
            })  
        except Exception:  
            pass  
  
        audit_event(  
            ctx.tenant_id,  
            tenant_slug=ctx.tenant_slug,  
            actor=ctx.key_prefix,  
            actor_type=ctx.actor_type,  
            action="api.generate.ok" if status_code == 200 else "api.generate.fail",  
            rid=ctx.request_id,  
            ip=ctx.client_ip,  
            user_agent=ctx.user_agent,  
            path=str(request.url.path),  
            status_code=status_code,  
            ms=elapsed_ms,  
            meta=meta,  
            con=auth_con,  
        )  
  
    if not zip_bytes:  
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="ZIP vacío (unexpected).")  
  
    headers = {  
        "X-Request-Id": ctx.request_id,  
        "X-Tenant": ctx.tenant_slug,  
        "X-Key": ctx.key_prefix,  
        "X-Excel-SHA256": excel_hash,  
        "X-Rows": n_rows or 0,  
        "X-Total": summary.get("total", 0),  
        "X-Cert-OK": summary.get("cert_ok", summary.get("ok", 0)),  
        "X-Cert-Review": summary.get("cert_review", 0),  
        "X-Cert-Invalid": summary.get("cert_invalid", 0),  
        "X-Cert-Crash": summary.get("cert_crash", 0),  
        "X-Carta-OK": summary.get("carta_ok", 0),  
        "X-Carta-Review": summary.get("carta_review", 0),  
        "X-Carta-Invalid": summary.get("carta_invalid", 0),  
        "X-Carta-Crash": summary.get("carta_crash", 0),  
    }  
    if idempotency_key:  
        headers["X-Idempotency-Status"] = idem_status or "done"  
        headers["X-Idempotency-Result-SHA256"] = idem_result_sha or ""  
  
    return _as_download_zip(zip_bytes, filename=f"certificados_{ctx.tenant_slug}.zip", extra_headers=headers)  
  
  
@app.post("/individual/{cifnif}")  
async def individual(  
    cifnif: str,  
    request: Request,  
    ctx: TenantContext = Depends(require_tenant_ctx),  
    file: UploadFile = File(...),  
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),  
):  
    require_scope(ctx, "individual")  
  
    # ✅ MASTER nunca puede ejecutar generate/individual (solo admin)  
    if ctx.actor_type in ("admin_key", "master"):  
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Esta key no puede ejecutar 'individual'.")  
  
    _rate_limit(f"{ctx.tenant_id}:{ctx.key_prefix}:individual", RL_INDIV_PER_MIN)  
  
    cifnif = (cifnif or "").strip()  
    if not cifnif:  
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="CIF/NIF vacío.")  
  
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024  
    excel_bytes = await _read_upload_limited(request, file, max_bytes=max_bytes)  
  
    n_rows = _count_excel_rows_fast(excel_bytes)  
    if n_rows is not None and n_rows > MAX_EXCEL_ROWS:  
        raise HTTPException(  
            status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,  
            detail=f"Excel demasiado grande: {n_rows} filas. Máx {MAX_EXCEL_ROWS}.",  
        )  
  
    excel_hash = _sha256_bytes(excel_bytes)  
  
    started = datetime.now(timezone.utc)  
    status_code = 200  
  
    meta: dict = {}  
    auth_con: Optional[sqlite3.Connection] = None  
  
    idem_status = ""  
    idem_result_sha = ""  
    zip_bytes: bytes = b""  
  
    try:  
        auth_con = get_or_open_auth_con(request)  
        tenant_cfg = get_tenant_settings(auth_con, ctx.tenant_id) or {}  
  
        if idempotency_key:  
            idem_key = (idempotency_key or "").strip()  
            if len(idem_key) > 200:  
                raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Idempotency-Key demasiado largo.")  
  
            params_json = _params_json_normalized({  
                "tenant_id": ctx.tenant_id,  
                "scope": "individual",  
                "cifnif_sha": _sha256_text(cifnif),  
            })  
            req_sha = _sha256_hex(f"{excel_hash}|{params_json}")  
  
            st, rsha, meta0 = _idem_try_lock(  
                auth_con,  
                tenant_id=ctx.tenant_id,  
                scope="individual",  
                idem_key=idem_key,  
                request_sha256=req_sha,  
            )  
  
            if st == "replay":  
                return JSONResponse(  
                    status_code=HTTP_202_ACCEPTED,  
                    content={  
                        "idempotency": "already_done",  
                        "result_sha256": rsha,  
                        "meta": meta0 or {},  
                        "note": "Procesado previamente. Replay binario requiere storage (S3/MinIO).",  
                    },  
                    headers={  
                        "X-Request-Id": ctx.request_id,  
                        "X-Tenant": ctx.tenant_slug,  
                        "X-Key": ctx.key_prefix,  
                        "X-Idempotency-Status": "replay",  
                        "X-Idempotency-Result-SHA256": rsha or "",  
                    },  
                )  
  
            if st == "conflict":  
                raise HTTPException(  
                    status_code=HTTP_409_CONFLICT,  
                    detail={"idempotency": "conflict", "note": "Idempotency-Key en uso o usado con otro payload."},  
                )  
            idem_status = "locked"  
  
        def _work():
            biz_con = connect_biz_db()
            auth_con2 = connect_auth_db()
            try:
                return system.generate_individual_from_excel_bytes(
                    excel_bytes=excel_bytes,
                    cifnif=cifnif,
                    tenant_cfg=tenant_cfg,
                    con_biz=biz_con,
                    con_auth=auth_con2,
                    tenant_id=ctx.tenant_id,
                )
            finally:
                try: biz_con.close()
                except Exception: pass
                try: auth_con2.close()
                except Exception: pass
  
        cert_pdf, carta_pdf, meta = await _run_with_timeout(_work, MAX_PROCESS_SECONDS)  
  
        zip_buf = io.BytesIO()  
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as z:  
            num = str(meta.get("numerocertificado", "")).strip() or "CERT"  
            if cert_pdf:  
                z.writestr(f"{num}_CERT.pdf", cert_pdf)  
            if carta_pdf:  
                z.writestr(f"{num}_CARTA.pdf", carta_pdf)  
  
        zip_buf.seek(0)  
        zip_bytes = zip_buf.read()  
        zip_sha = _sha256_bytes(zip_bytes)  
        idem_result_sha = zip_sha  
  
        if idempotency_key:  
            _idem_mark_done(  
                auth_con,  
                tenant_id=ctx.tenant_id,  
                scope="individual",  
                idem_key=(idempotency_key or "").strip(),  
                result_sha256=zip_sha,  
                meta={"meta": meta, "excel_sha256": excel_hash},  
            )  
            idem_status = "done"  
  
    except HTTPException as he:  
        status_code = int(getattr(he, "status_code", 400) or 400)  
        try:  
            if auth_con is not None and idempotency_key and idem_status == "locked":  
                _idem_mark_failed(  
                    auth_con,  
                    tenant_id=ctx.tenant_id,  
                    scope="individual",  
                    idem_key=(idempotency_key or "").strip(),  
                    meta={"error": "http_exception", "status_code": status_code},  
                )  
        except Exception:  
            pass  
        raise  
    except ValueError as e:  
        status_code = 400  
        try:  
            if auth_con is not None and idempotency_key and idem_status == "locked":  
                _idem_mark_failed(  
                    auth_con,  
                    tenant_id=ctx.tenant_id,  
                    scope="individual",  
                    idem_key=(idempotency_key or "").strip(),  
                    meta={"error": "value_error", "detail": str(e)},  
                )  
        except Exception:  
            pass  
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e))  
    except Exception as e:  
        status_code = 500  
        logger.exception(  
            "individual_failed rid=%s tenant_id=%s tenant=%s key=%s scope=individual err=%s",  
            ctx.request_id, ctx.tenant_id, ctx.tenant_slug, ctx.key_prefix, str(e),  
        )  
        try:  
            if auth_con is not None and idempotency_key and idem_status == "locked":  
                _idem_mark_failed(  
                    auth_con,  
                    tenant_id=ctx.tenant_id,  
                    scope="individual",  
                    idem_key=(idempotency_key or "").strip(),  
                    meta={"error": "exception", "detail": str(e)},  
                )  
        except Exception:  
            pass  
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="Error generando individual.")  
    finally:  
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)  
  
        audit_meta = {  
            "rows": n_rows,  
            "excel_sha256": excel_hash,  
            "cifnif_sha256": _sha256_text(cifnif),  
        }  
        try:  
            audit_meta.update({  
                "numerocertificado": str(meta.get("numerocertificado", "")),  
                "hash": str(meta.get("hash", "")),  
                "tipo": str(meta.get("tipo", "")),  
            })  
        except Exception:  
            pass  
  
        audit_event(  
            ctx.tenant_id,  
            tenant_slug=ctx.tenant_slug,  
            actor=ctx.key_prefix,  
            actor_type=ctx.actor_type,  
            action="api.individual.ok" if status_code == 200 else "api.individual.fail",  
            rid=ctx.request_id,  
            ip=ctx.client_ip,  
            user_agent=ctx.user_agent,  
            path=str(request.url.path),  
            status_code=status_code,  
            ms=elapsed_ms,  
            meta=audit_meta,  
            con=auth_con,  
        )  
  
    if not zip_bytes:  
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail="ZIP vacío (unexpected).")  
  
    headers = {  
        "X-Request-Id": ctx.request_id,  
        "X-Tenant": ctx.tenant_slug,  
        "X-Key": ctx.key_prefix,  
        "X-Excel-SHA256": excel_hash,  
        "X-Rows": n_rows or 0,  
        "X-Numerocertificado": meta.get("numerocertificado", ""),  
        "X-Hash": meta.get("hash", ""),  
        "X-Tipo": meta.get("tipo", ""),  
    }  
    if idempotency_key:  
        headers["X-Idempotency-Status"] = idem_status or "done"  
        headers["X-Idempotency-Result-SHA256"] = idem_result_sha or ""  
  
    safe_cif = cifnif.replace(" ", "").replace("/", "_")  
    return _as_download_zip(zip_bytes, filename=f"{ctx.tenant_slug}_{safe_cif}.zip", extra_headers=headers)  
  
  
# ---------------- Admin endpoints: API key rotation ----------------  
  
def _require_admin_actor(ctx: TenantContext) -> None:  
    # ✅ Fix #8: además del scope, exige tipo de actor  
    if ctx.actor_type not in ("admin_key", "master"):  
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Solo admin_key/master puede usar endpoints admin.")  
  
  
@app.get("/admin/api-keys")  
def admin_list_keys(  
    request: Request,  
    ctx: TenantContext = Depends(require_tenant_ctx),  
):  
    require_scope(ctx, "admin")  
    _require_admin_actor(ctx)  
    _rate_limit(f"{ctx.tenant_id}:{ctx.key_prefix}:admin_list_keys", RL_ADMIN_PER_MIN)  
  
    auth_con = get_or_open_auth_con(request)  
    tenant_cfg = get_tenant_settings(auth_con, ctx.tenant_id) or {}  
    require_admin_hardening(request, ctx, tenant_cfg)  
  
    audit_event(  
        ctx.tenant_id,  
        tenant_slug=ctx.tenant_slug,  
        actor=ctx.key_prefix,  
        actor_type=ctx.actor_type,  
        action="api.admin.api_keys.list",  
        rid=ctx.request_id,  
        ip=ctx.client_ip,  
        user_agent=ctx.user_agent,  
        path=str(request.url.path),  
        status_code=200,  
        ms=0,  
        meta={},  
        con=auth_con,  
    )  
  
    return {"tenant": ctx.tenant_slug, "keys": list_api_keys(auth_con, ctx.tenant_id)}  
  
  
@app.post("/admin/api-keys/create")  
def admin_create_key(  
    request: Request,  
    scopes: str = Query(default="generate,individual", description="csv scopes: generate,individual,admin"),  
    label: str = Query(default="", description="label opcional"),  
    ctx: TenantContext = Depends(require_tenant_ctx),  
):  
    require_scope(ctx, "admin")  
    _require_admin_actor(ctx)  
    _rate_limit(f"{ctx.tenant_id}:{ctx.key_prefix}:admin_create_key", RL_ADMIN_PER_MIN)  
  
    scopes_csv = ",".join([s.strip().lower() for s in (scopes or "").split(",") if s.strip()])  
    scopes_list = _normalize_scopes(scopes_csv)  
    scopes_list = _validate_scopes_policy(ctx, scopes_list)  
    scopes_csv = ",".join(scopes_list)  
  
    auth_con = get_or_open_auth_con(request)  
    tenant_cfg = get_tenant_settings(auth_con, ctx.tenant_id) or {}  
    require_admin_hardening(request, ctx, tenant_cfg)  
  
    out = create_api_key(auth_con, ctx.tenant_id, scopes_csv=scopes_csv, label=label or "")  
  
    audit_event(  
        ctx.tenant_id,  
        tenant_slug=ctx.tenant_slug,  
        actor=ctx.key_prefix,  
        actor_type=ctx.actor_type,  
        action="api.admin.api_keys.create",  
        rid=ctx.request_id,  
        ip=ctx.client_ip,  
        user_agent=ctx.user_agent,  
        path=str(request.url.path),  
        status_code=200,  
        ms=0,  
        meta={"created_prefix": out.get("key_prefix", ""), "scopes": scopes_csv, "label": label or ""},  
        con=auth_con,  
    )  
  
    return {"tenant": ctx.tenant_slug, **out}  # ⚠️ raw_key solo se devuelve una vez  
  
  
@app.post("/admin/api-keys/revoke/{key_prefix}")  
def admin_revoke_key(  
    key_prefix: str,  
    request: Request,  
    ctx: TenantContext = Depends(require_tenant_ctx),  
):  
    require_scope(ctx, "admin")  
    _require_admin_actor(ctx)  
    _rate_limit(f"{ctx.tenant_id}:{ctx.key_prefix}:admin_revoke_key", RL_ADMIN_PER_MIN)  
  
    kp = (key_prefix or "").strip()[:24]  
    if not kp:  
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="key_prefix vacío.")  
  
    auth_con = get_or_open_auth_con(request)  
    tenant_cfg = get_tenant_settings(auth_con, ctx.tenant_id) or {}  
    require_admin_hardening(request, ctx, tenant_cfg)  
  
    revoke_api_key(auth_con, ctx.tenant_id, kp)  
  
    audit_event(  
        ctx.tenant_id,  
        tenant_slug=ctx.tenant_slug,  
        actor=ctx.key_prefix,  
        actor_type=ctx.actor_type,  
        action="api.admin.api_keys.revoke",  
        rid=ctx.request_id,  
        ip=ctx.client_ip,  
        user_agent=ctx.user_agent,  
        path=str(request.url.path),  
        status_code=200,  
        ms=0,  
        meta={"revoked_prefix": kp},  
        con=auth_con,  
    )  
  
    return {"ok": True, "tenant": ctx.tenant_slug, "revoked": kp}