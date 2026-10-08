# backend/db.py
from __future__ import annotations

from backend.db_explorer import clean_tax_id, is_valid_email

import hashlib
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Cache sin fugas
try:
    import weakref

    _WEAK_OK = True
except Exception:
    weakref = None  # type: ignore
    _WEAK_OK = False


# =============================
# Connection
# =============================


def connect_db(db_path: Path) -> sqlite3.Connection:
    """
    1:1 con tu _connect_sqlite() del main:
    - WAL
    - foreign_keys=ON
    - busy_timeout=5000
    - timeout=30, isolation_level=None
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row

    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA busy_timeout=5000;")
    con.execute("PRAGMA synchronous=NORMAL;")  # OK con WAL + Streamlit/worker
    return con


# =============================
# Helpers
# =============================

# ✅ v6: search columns + donor_tax_id_history + snapshot guardrails
BUSINESS_SCHEMA_VERSION = 6

ANON_CIF = "__ANON__"
ANON_NAME = "DONANTE ANÓNIMO"

DONOR_TYPES = {"UNKNOWN", "INDIVIDUAL", "COMPANY"}

# Snapshot guardrails
MAX_SNAPSHOT_CHARS = 50_000  # límite duro de tamaño (evita inflar DB)
MAX_SNAPSHOT_B64_RUN = 8_000  # run sospechoso de base64


def utc_now_str() -> str:
    """1:1 con auth/main: ISO UTC."""
    return datetime.now(timezone.utc).isoformat()


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;",
        (name,),
    ).fetchone()
    return row is not None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    except Exception:
        return set()
    cols: set[str] = set()
    for r in rows:
        name = r["name"] if isinstance(r, sqlite3.Row) else r[1]
        cols.add(str(name))
    return cols


def _norm(s: str) -> str:
    return (s or "").strip()


def _norm_casefold(s: str) -> str:
    return (s or "").strip().casefold()


def norm_cifnif(cifnif: str) -> str:
    x = _norm(cifnif).upper()
    x = x.replace(" ", "").replace("\t", "").replace("\n", "")
    return x


def norm_email(email: str) -> str:
    # 1:1 con auth: casefold (más robusto que lower)
    return _norm_casefold(email)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def make_row_hash(*parts: Any) -> str:
    """
    Hash estable para deduplicar filas.
    IMPORTANTÍSIMO: si cambias qué metes aquí, puedes romper dedupe histórico.
    """
    flat = "|".join("" if p is None else str(p).strip() for p in parts)
    return sha256_text(flat)


# =============================
# ✅ 1. Gestor de transacciones blindado (SAVEPOINT SIEMPRE)
# =============================


@contextmanager
def _write_tx(con: sqlite3.Connection):
    """
    ✅ SAVEPOINT: El único método que permite 'búnkeres dentro de búnkeres'.
    No preguntamos si hay transacción; creamos un punto de control siempre.

    - Si existe transacción externa: se anida seguro.
    - Si NO existe: SQLite abre una implícita y el RELEASE actúa como commit de ese bloque.
    """
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


# =============================
# ✅ Search normalization (v6)
# =============================

_WS_RE = re.compile(r"\s+")


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s


def _norm_search(s: str) -> str:
    """
    Normalización para búsquedas indexables:
    - casefold
    - sin acentos
    - espacios colapsados
    """
    t = _strip_accents(_norm(s)).casefold()
    t = _WS_RE.sub(" ", t).strip()
    return t


def _build_donor_search_fields(*, nombre: str, email: str, cifnif_norm_: str) -> tuple[str, str, str]:
    # IMPORTANTE: email debe venir ya normalizado con norm_email()
    nombre_norm = _norm_search(nombre)
    email_norm = _norm_search(email)
    cif_search = _norm_search(cifnif_norm_)
    cifnif_search = " ".join(p for p in (cif_search, nombre_norm, email_norm) if p).strip()
    return nombre_norm, email_norm, cifnif_search


# =============================
# ✅ Snapshot guardrails (v6)
# =============================

_B64_ALPHABET = re.compile(r"^[A-Za-z0-9+/=\s]+$")


def _looks_like_base64_blob(s: str) -> bool:
    if not s:
        return False
    t = s.strip()
    if len(t) < MAX_SNAPSHOT_B64_RUN:
        return False
    if "data:image" in t.lower() or "data:application" in t.lower():
        return True
    if _B64_ALPHABET.match(t) and (t.count("\n") + t.count("\r") <= 10):
        return True
    return False


def sanitize_snapshot_json(meta_snapshot_json: str) -> str:
    s = meta_snapshot_json or ""
    if not s.strip():
        return ""

    if len(s) > MAX_SNAPSHOT_CHARS:
        raise ValueError(f"meta_snapshot_json demasiado grande ({len(s)} chars). Límite={MAX_SNAPSHOT_CHARS}")

    for token in re.findall(r"[A-Za-z0-9+/=]{%d,}" % MAX_SNAPSHOT_B64_RUN, s):
        if _looks_like_base64_blob(token):
            raise ValueError("meta_snapshot_json parece contener binario/base64 (bloqueado).")

    if "data:image" in s.lower() or "data:application" in s.lower():
        raise ValueError("meta_snapshot_json contiene data:* (binario embebido) (bloqueado).")

    return s


# =============================
# Fecha: normalización robusta
# =============================

_MONTHS_ES = {
    "enero": 1,
    "ene": 1,
    "febrero": 2,
    "feb": 2,
    "marzo": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "mayo": 5,
    "may": 5,
    "junio": 6,
    "jun": 6,
    "julio": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "septiembre": 9,
    "sep": 9,
    "setiembre": 9,
    "octubre": 10,
    "oct": 10,
    "noviembre": 11,
    "nov": 11,
    "diciembre": 12,
    "dic": 12,
}

_MONTHS_EN = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_MONTHS_ALL = {**_MONTHS_ES, **_MONTHS_EN}


def _excel_serial_to_date(n: float) -> Optional[date]:
    try:
        if n is None:
            return None
        n = float(n)
        if n <= 0:
            return None
        base = date(1899, 12, 30)
        return base + timedelta(days=int(n))
    except Exception:
        return None


def _parse_date_to_iso(value: Any) -> str:
    """
    Convierte entradas típicas (Excel/str/datetime/date/serial) a 'YYYY-MM-DD'.
    Si no se puede, devuelve "".
    """
    if value is None:
        return ""

    if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.date().strftime("%Y-%m-%d")

    if isinstance(value, (int, float)):
        d = _excel_serial_to_date(value)
        return d.strftime("%Y-%m-%d") if d else ""

    try:
        s_try = str(value).strip()
        if s_try:
            s_try2 = s_try.replace(",", ".")
            if s_try2.replace(".", "", 1).isdigit():
                d = _excel_serial_to_date(float(s_try2))
                if d:
                    return d.strftime("%Y-%m-%d")
    except Exception:
        pass

    s = str(value).strip()
    if not s or s.lower() == "nan":
        return ""

    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
    except Exception:
        pass

    low = s.lower().replace(",", " ").replace("  ", " ").strip()
    parts = [p for p in low.split(" ") if p]
    if len(parts) >= 2:
        m = _MONTHS_ALL.get(parts[0])
        if m and parts[1].isdigit() and len(parts[1]) == 4:
            try:
                return date(int(parts[1]), int(m), 1).strftime("%Y-%m-%d")
            except Exception:
                return ""

    for sep in ("/", "-", "."):
        if sep in s:
            pp = [p.strip() for p in s.split(sep)]
            if len(pp) >= 3:
                p1, p2, p3 = pp[0], pp[1], pp[2]
                p3 = p3[:10]

                if len(p1) == 4 and p1.isdigit():
                    y = int(p1)
                    if not p2.isdigit():
                        return ""
                    m2 = int(p2)
                    d_str = pp[2][:2]
                    if not d_str.isdigit():
                        return ""
                    d2 = int(d_str)
                    try:
                        return date(y, m2, d2).strftime("%Y-%m-%d")
                    except Exception:
                        return ""

                if len(p3) >= 4 and p3[:4].isdigit():
                    y = int(p3[:4])
                    if not (p1.isdigit() and p2.isdigit()):
                        return ""
                    a = int(p1)
                    b = int(p2)

                    # Heurística ES
                    if a > 12 and 1 <= b <= 12:
                        d2, m2 = a, b
                    elif b > 12 and 1 <= a <= 12:
                        m2, d2 = a, b
                    else:
                        d2, m2 = a, b

                    try:
                        return date(y, m2, d2).strftime("%Y-%m-%d")
                    except Exception:
                        return ""

    return ""


def _year_from_iso(iso: str) -> Optional[int]:
    if not iso or len(iso) < 4:
        return None
    try:
        return int(iso[:4])
    except Exception:
        return None


# =============================
# Schema versioning
# =============================


def _get_user_version(con: sqlite3.Connection) -> int:
    row = con.execute("PRAGMA user_version;").fetchone()
    try:
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _set_user_version(con: sqlite3.Connection, version: int) -> None:
    con.execute(f"PRAGMA user_version={int(version)};")


def _ensure_schema_meta(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )


def _get_schema_version(con: sqlite3.Connection) -> int:
    if not _table_exists(con, "schema_meta"):
        return 0
    row = con.execute(
        "SELECT value FROM schema_meta WHERE key='business_schema_version' LIMIT 1;"
    ).fetchone()
    if not row:
        return 0
    v = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        return int(v)
    except Exception:
        return 0


def _set_schema_version(con: sqlite3.Connection, version: int) -> None:
    _ensure_schema_meta(con)
    con.execute(
        """
        INSERT INTO schema_meta(key, value)
        VALUES ('business_schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """,
        (str(int(version)),),
    )


def _quick_schema_check(con: sqlite3.Connection) -> bool:
    must_tables = {
        "donors",
        "imports",
        "donations",
        "certificates",
        "certificate_items",
        "email_logs",
        "schema_meta",
        "donor_tax_id_history",
    }
    for t in must_tables:
        if not _table_exists(con, t):
            return False

    dcols = _columns(con, "donors")
    must_don = {
        "donor_type",
        "cifnif_norm",
        "tenant_id",
        "cifnif_raw",
        "nombre_norm",
        "email_norm",
        "cifnif_search",
    }
    if not must_don.issubset(dcols):
        return False

    doncols = _columns(con, "donations")
    if not {"tenant_id", "donor_id", "is_void", "fecha_iso", "year_int"}.issubset(doncols):
        return False

    ccols = _columns(con, "certificates")
    must_cert = {
        "tenant_id",
        "donor_id",
        "numerocertificado",
        "is_void",
        "void_reason",
        "voided_at",
        "voided_by",
        "meta_snapshot_json",
        "snapshot_update_policy",
    }
    if not must_cert.issubset(ccols):
        return False

    return True


def _create_tables_v6(con: sqlite3.Connection) -> None:
    """
    Crea tablas v6 si NO existen.
    OJO: NO redefine tablas existentes (para evitar “híbridos”).
    """
    with _write_tx(con):
        _ensure_schema_meta(con)

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS donors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,

                parent_donor_id INTEGER,

                donor_type TEXT NOT NULL DEFAULT 'UNKNOWN',

                cifnif_norm TEXT NOT NULL,
                cifnif_raw TEXT,

                nombre TEXT,
                nombre_norm TEXT,
                email TEXT,
                email_norm TEXT,
                cifnif_search TEXT,

                telefono TEXT,
                direccion TEXT,
                ciudad TEXT,
                notas TEXT,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(tenant_id, cifnif_norm),
                FOREIGN KEY (parent_donor_id) REFERENCES donors(id) ON DELETE SET NULL
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS donor_tax_id_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                donor_id INTEGER NOT NULL,

                old_cifnif_norm TEXT,
                old_cifnif_raw TEXT,
                new_cifnif_norm TEXT,
                new_cifnif_raw TEXT,

                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                changed_by TEXT,
                reason TEXT,

                FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE CASCADE
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                uploaded_by TEXT,
                source_filename TEXT,
                source_sha256 TEXT,
                total_rows INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS donations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                donor_id INTEGER NOT NULL,
                import_id INTEGER,
                row_index INTEGER,
                row_hash TEXT,

                fecha TEXT,
                fecha_iso TEXT,
                year_int INTEGER,
                tipo TEXT,
                importe REAL,
                kg REAL,

                fuente TEXT,
                raw_json TEXT,

                is_void INTEGER NOT NULL DEFAULT 0,
                void_reason TEXT,
                voided_at TEXT,
                voided_by TEXT,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(tenant_id, row_hash),
                FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE CASCADE,
                FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE SET NULL
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS certificates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                donor_id INTEGER NOT NULL,

                donation_id INTEGER,        -- LEGACY (no usar)
                import_id INTEGER,

                numerocertificado TEXT NOT NULL,
                year INTEGER,
                seq INTEGER,
                hash TEXT,
                tipo TEXT,

                fecha_emision TEXT,
                status_certificado TEXT,
                status_carta TEXT,

                is_void INTEGER NOT NULL DEFAULT 0,
                void_reason TEXT,
                voided_at TEXT,
                voided_by TEXT,

                cert_pdf_path TEXT,
                carta_pdf_path TEXT,
                created_by TEXT,

                email_to TEXT,
                email_status TEXT,
                emailed_at TEXT,

                meta_snapshot_json TEXT,
                snapshot_update_policy TEXT,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(tenant_id, numerocertificado),
                FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE CASCADE,
                FOREIGN KEY (donation_id) REFERENCES donations(id) ON DELETE SET NULL,
                FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE SET NULL
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS certificate_items (
                certificate_id INTEGER NOT NULL,
                donation_id INTEGER NOT NULL,
                PRIMARY KEY (certificate_id, donation_id),
                FOREIGN KEY (certificate_id) REFERENCES certificates(id) ON DELETE CASCADE,
                FOREIGN KEY (donation_id) REFERENCES donations(id) ON DELETE RESTRICT
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                certificate_id INTEGER,
                to_email TEXT,
                subject TEXT,
                status TEXT,
                error TEXT,
                sent_by TEXT,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (certificate_id) REFERENCES certificates(id) ON DELETE SET NULL
            );
            """
        )

        # índices
        con.execute("CREATE INDEX IF NOT EXISTS idx_donors_tenant ON donors(tenant_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donors_cif ON donors(tenant_id, cifnif_norm);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donors_parent ON donors(parent_donor_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donors_type ON donors(tenant_id, donor_type);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donors_nombre_norm ON donors(tenant_id, nombre_norm);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donors_email_norm ON donors(tenant_id, email_norm);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donors_cif_search ON donors(tenant_id, cifnif_search);")

        con.execute("CREATE INDEX IF NOT EXISTS idx_taxhist_tenant ON donor_tax_id_history(tenant_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_taxhist_donor ON donor_tax_id_history(tenant_id, donor_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_taxhist_old ON donor_tax_id_history(tenant_id, old_cifnif_norm);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_taxhist_new ON donor_tax_id_history(tenant_id, new_cifnif_norm);")

        con.execute("CREATE INDEX IF NOT EXISTS idx_imports_tenant ON imports(tenant_id);")

        con.execute("CREATE INDEX IF NOT EXISTS idx_donations_tenant ON donations(tenant_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donations_donor ON donations(donor_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donations_import ON donations(import_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donations_void ON donations(tenant_id, is_void);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donations_year ON donations(tenant_id, year_int);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_donations_fecha_iso ON donations(tenant_id, fecha_iso);")

        con.execute("CREATE INDEX IF NOT EXISTS idx_certs_tenant ON certificates(tenant_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_certs_donor ON certificates(donor_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_certs_year ON certificates(tenant_id, year);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_certs_email_status ON certificates(tenant_id, email_status);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_certs_void ON certificates(tenant_id, is_void);")

        con.execute("CREATE INDEX IF NOT EXISTS idx_items_cert ON certificate_items(certificate_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_items_donation ON certificate_items(donation_id);")

        con.execute("CREATE INDEX IF NOT EXISTS idx_email_logs_tenant ON email_logs(tenant_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_email_logs_cert ON email_logs(certificate_id);")


def _safe_add_column(con: sqlite3.Connection, table: str, column_def: str) -> None:
    try:
        colname = column_def.split()[0].strip()
        if colname in _columns(con, table):
            return
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column_def};")
    except Exception:
        pass


def _migrate_to_v6(con: sqlite3.Connection) -> None:
    _create_tables_v6(con)

    if _table_exists(con, "donors"):
        with _write_tx(con):
            _safe_add_column(con, "donors", "nombre_norm TEXT")
            _safe_add_column(con, "donors", "email_norm TEXT")
            _safe_add_column(con, "donors", "cifnif_search TEXT")

    # backfill (IMPORTANTE: email normalizado con norm_email)
    try:
        rows = con.execute(
            """
            SELECT id, tenant_id, nombre, email, cifnif_norm
            FROM donors
            WHERE nombre_norm IS NULL OR nombre_norm='' OR
                  email_norm IS NULL OR email_norm='' OR
                  cifnif_search IS NULL OR cifnif_search='';
            """
        ).fetchall()
        if rows:
            with _write_tx(con):
                for r in rows:
                    nn, en, cs = _build_donor_search_fields(
                        nombre=_norm(r["nombre"] or ""),
                        email=norm_email(r["email"] or ""),
                        cifnif_norm_=_norm(r["cifnif_norm"] or ""),
                    )
                    con.execute(
                        """
                        UPDATE donors
                        SET nombre_norm=?,
                            email_norm=?,
                            cifnif_search=?,
                            updated_at=?
                        WHERE id=?;
                        """,
                        (nn, en, cs, utc_now_str(), int(r["id"])),
                    )
    except Exception:
        pass

    try:
        with _write_tx(con):
            con.execute("CREATE INDEX IF NOT EXISTS idx_donors_nombre_norm ON donors(tenant_id, nombre_norm);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_donors_email_norm ON donors(tenant_id, email_norm);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_donors_cif_search ON donors(tenant_id, cifnif_search);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_taxhist_tenant ON donor_tax_id_history(tenant_id);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_taxhist_donor ON donor_tax_id_history(tenant_id, donor_id);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_taxhist_old ON donor_tax_id_history(tenant_id, old_cifnif_norm);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_taxhist_new ON donor_tax_id_history(tenant_id, new_cifnif_norm);")
    except Exception:
        pass


def ensure_business_schema(con: sqlite3.Connection) -> None:
    """
    Esquema de negocio BdA (BUSINESS_DB).
    """
    con.execute("PRAGMA foreign_keys=ON;")

    uv = _get_user_version(con)
    sv = _get_schema_version(con)

    if uv >= BUSINESS_SCHEMA_VERSION and sv >= BUSINESS_SCHEMA_VERSION and _quick_schema_check(con):
        return

    # DB totalmente nueva
    if (
        not _table_exists(con, "donors")
        and not _table_exists(con, "donations")
        and not _table_exists(con, "certificates")
    ):
        _create_tables_v6(con)
        with _write_tx(con):
            _set_schema_version(con, BUSINESS_SCHEMA_VERSION)
            _set_user_version(con, BUSINESS_SCHEMA_VERSION)
        return

    _migrate_to_v6(con)

    if _quick_schema_check(con):
        with _write_tx(con):
            _set_schema_version(con, BUSINESS_SCHEMA_VERSION)
            _set_user_version(con, BUSINESS_SCHEMA_VERSION)


# =============================
# ✅ Fast cache (sin fugas)
# =============================

from typing import Optional as _OptionalTyping

if _WEAK_OK:
    try:
        from weakref import WeakKeyDictionary

        _SCHEMA_OK_WEAK: _OptionalTyping[WeakKeyDictionary[sqlite3.Connection, bool]] = WeakKeyDictionary()
    except Exception:
        _SCHEMA_OK_WEAK = None
else:
    _SCHEMA_OK_WEAK = None

_SCHEMA_OK_LRU: Dict[int, bool] = {}
_SCHEMA_OK_LRU_ORDER: List[int] = []
_SCHEMA_OK_LRU_MAX = 32


def ensure_business_schema_once(con: sqlite3.Connection) -> None:
    """
    ✅ Asegura esquema una sola vez por conexión SIN fugas.
    (Para admin/repair/arranque)
    """
    if _SCHEMA_OK_WEAK is not None:
        try:
            if _SCHEMA_OK_WEAK.get(con):
                return
            ensure_business_schema(con)
            _SCHEMA_OK_WEAK[con] = True
            return
        except Exception:
            pass

    key = id(con)
    if _SCHEMA_OK_LRU.get(key):
        return
    ensure_business_schema(con)
    _SCHEMA_OK_LRU[key] = True
    _SCHEMA_OK_LRU_ORDER.append(key)
    if len(_SCHEMA_OK_LRU_ORDER) > _SCHEMA_OK_LRU_MAX:
        old = _SCHEMA_OK_LRU_ORDER.pop(0)
        _SCHEMA_OK_LRU.pop(old, None)


# =============================
# Runtime fail-closed guards
# =============================


def assert_business_schema(con: sqlite3.Connection) -> None:
    if not _quick_schema_check(con):
        raise RuntimeError("Business schema missing/incomplete. Ejecuta migraciones/startup.")


def assert_business_schema_once(con: sqlite3.Connection) -> None:
    # Reusa el mismo cache, pero NO hace DDL: solo verifica
    if _SCHEMA_OK_WEAK is not None:
        try:
            if _SCHEMA_OK_WEAK.get(con):
                return
        except Exception:
            pass

    key = id(con)
    if _SCHEMA_OK_LRU.get(key):
        return

    assert_business_schema(con)
    _SCHEMA_OK_LRU[key] = True
    _SCHEMA_OK_LRU_ORDER.append(key)
    if len(_SCHEMA_OK_LRU_ORDER) > _SCHEMA_OK_LRU_MAX:
        old = _SCHEMA_OK_LRU_ORDER.pop(0)
        _SCHEMA_OK_LRU.pop(old, None)


# =============================
# Tenant enforcement helpers (P0)
# =============================


def _require_tenant_id(tenant_id: str) -> str:
    t = _norm(tenant_id)
    if not t:
        raise ValueError("tenant_id requerido")
    return t


def _require_donor_in_tenant(con: sqlite3.Connection, *, tenant_id: str, donor_id: int) -> None:
    row = con.execute(
        "SELECT 1 FROM donors WHERE tenant_id=? AND id=? LIMIT 1;",
        (_require_tenant_id(tenant_id), int(donor_id)),
    ).fetchone()
    if not row:
        raise ValueError("donor_id no pertenece al tenant o no existe")


def _require_import_in_tenant(con: sqlite3.Connection, *, tenant_id: str, import_id: int) -> None:
    row = con.execute(
        "SELECT 1 FROM imports WHERE tenant_id=? AND id=? LIMIT 1;",
        (_require_tenant_id(tenant_id), int(import_id)),
    ).fetchone()
    if not row:
        raise ValueError("import_id no pertenece al tenant o no existe")


def _get_certificate_tenant(con: sqlite3.Connection, certificate_id: int) -> Optional[str]:
    row = con.execute(
        "SELECT tenant_id FROM certificates WHERE id=? LIMIT 1;",
        (int(certificate_id),),
    ).fetchone()
    if not row:
        return None
    return str(row["tenant_id"])


def _get_donation_tenant(con: sqlite3.Connection, donation_id: int) -> Optional[str]:
    row = con.execute(
        "SELECT tenant_id FROM donations WHERE id=? LIMIT 1;",
        (int(donation_id),),
    ).fetchone()
    if not row:
        return None
    return str(row["tenant_id"])


# =============================
# Backfill explícito
# =============================


def run_backfill_dates(
    con: sqlite3.Connection,
    *,
    batch_size: int = 5000,
    max_batches: int = 1,
) -> Dict[str, int]:
    """
    Rellena fecha_iso/year_int para donations donde falten.
    (Admin/repair)
    """
    ensure_business_schema_once(con)

    processed = 0
    for _ in range(max(1, int(max_batches))):
        rows = con.execute(
            """
            SELECT id, fecha
            FROM donations
            WHERE (year_int IS NULL OR year_int=0 OR fecha_iso IS NULL OR fecha_iso='')
            LIMIT ?;
            """,
            (int(batch_size),),
        ).fetchall()

        if not rows:
            break

        with _write_tx(con):
            for r in rows:
                iso = _parse_date_to_iso(r["fecha"])
                y = _year_from_iso(iso)
                con.execute(
                    "UPDATE donations SET fecha_iso=?, year_int=? WHERE id=?;",
                    (iso or None, int(y) if y else None, int(r["id"])),
                )
        processed += len(rows)

    remaining_row = con.execute(
        """
        SELECT COUNT(*)
        FROM donations
        WHERE (year_int IS NULL OR year_int=0 OR fecha_iso IS NULL OR fecha_iso='');
        """
    ).fetchone()
    remaining = int(remaining_row[0]) if remaining_row else 0
    return {"processed": int(processed), "remaining": int(remaining)}


# =============================
# Donante ANÓNIMO por tenant
# =============================


def ensure_anonymous_donor(con: sqlite3.Connection, *, tenant_id: str) -> int:
    ensure_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)

    row = con.execute(
        "SELECT id FROM donors WHERE tenant_id=? AND cifnif_norm=? LIMIT 1;",
        (tenant_id, ANON_CIF),
    ).fetchone()
    if row:
        return int(row["id"])

    now = utc_now_str()
    nn, en, cs = _build_donor_search_fields(nombre=ANON_NAME, email=norm_email(""), cifnif_norm_=ANON_CIF)

    with _write_tx(con):
        con.execute(
            """
            INSERT OR IGNORE INTO donors(
                tenant_id, parent_donor_id,
                donor_type,
                cifnif_norm, cifnif_raw,
                nombre, nombre_norm,
                email, email_norm,
                cifnif_search,
                telefono, direccion, ciudad, notas,
                created_at, updated_at
            ) VALUES (?, NULL, 'UNKNOWN', ?, ?, ?, ?, '', ?, ?, '', '', '', 'AUTO', ?, ?);
            """,
            (tenant_id, ANON_CIF, ANON_CIF, ANON_NAME, nn, en, cs, now, now),
        )

    row2 = con.execute(
        "SELECT id FROM donors WHERE tenant_id=? AND cifnif_norm=? LIMIT 1;",
        (tenant_id, ANON_CIF),
    ).fetchone()
    if not row2:
        raise RuntimeError("No se pudo crear/leer DONANTE ANÓNIMO.")
    return int(row2["id"])


def is_anonymous_donor(con: sqlite3.Connection, *, donor_id: int) -> bool:
    assert_business_schema_once(con)
    row = con.execute(
        "SELECT cifnif_norm FROM donors WHERE id=? LIMIT 1;",
        (int(donor_id),),
    ).fetchone()
    if not row:
        return False
    return (row["cifnif_norm"] or "") == ANON_CIF


def require_not_anonymous_donor(con: sqlite3.Connection, *, donor_id: int) -> None:
    if is_anonymous_donor(con, donor_id=int(donor_id)):
        raise ValueError("No se puede emitir certificado para DONANTE ANÓNIMO. Completa CIF/NIF del donante.")


# =============================
# Donor Tax ID History (v6)
# =============================


def _insert_tax_id_history(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donor_id: int,
    old_norm: str,
    old_raw: str,
    new_norm: str,
    new_raw: str,
    changed_by: str = "",
    reason: str = "",
) -> None:
    try:
        con.execute(
            """
            INSERT INTO donor_tax_id_history(
                tenant_id, donor_id,
                old_cifnif_norm, old_cifnif_raw,
                new_cifnif_norm, new_cifnif_raw,
                changed_at, changed_by, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                _require_tenant_id(tenant_id),
                int(donor_id),
                _norm(old_norm),
                _norm(old_raw),
                _norm(new_norm),
                _norm(new_raw),
                utc_now_str(),
                _norm(changed_by),
                _norm(reason),
            ),
        )
    except Exception:
        pass


# =============================
# Donors
# =============================


def upsert_donor(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    cifnif: str,
    nombre: str = "",
    email: str = "",
    telefono: str = "",
    direccion: str = "",
    ciudad: str = "",
    notas: str = "",
    parent_donor_id: Optional[int] = None,
    donor_type: str = "UNKNOWN",
    overwrite: bool = False,
) -> int:
    """
    Upsert por clave fiscal (tenant_id + cifnif_norm).
    ✅ Nested-safe: _write_tx.
    ✅ P0: parent_donor_id (si viene) debe ser del mismo tenant.
    """
    assert_business_schema_once(con)

    tenant_id = _require_tenant_id(tenant_id)

    cifnif_raw = _norm(cifnif)
    cifnif_norm_ = norm_cifnif(cifnif_raw)
    if not cifnif_norm_:
        raise ValueError("cifnif requerido")

    dt = _norm(donor_type).upper() or "UNKNOWN"
    if dt not in DONOR_TYPES:
        dt = "UNKNOWN"

    if parent_donor_id:
        _require_donor_in_tenant(con, tenant_id=tenant_id, donor_id=int(parent_donor_id))

    now = utc_now_str()

    row = con.execute(
        "SELECT * FROM donors WHERE tenant_id=? AND cifnif_norm=? LIMIT 1;",
        (tenant_id, cifnif_norm_),
    ).fetchone()

    if not row:
        nn, en, cs = _build_donor_search_fields(
            nombre=_norm(nombre),
            email=norm_email(email),
            cifnif_norm_=cifnif_norm_,
        )
        with _write_tx(con):
            cur = con.execute(
                """
                INSERT INTO donors(
                    tenant_id, parent_donor_id,
                    donor_type,
                    cifnif_norm, cifnif_raw,
                    nombre, nombre_norm,
                    email, email_norm,
                    cifnif_search,
                    telefono, direccion, ciudad, notas,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    tenant_id,
                    int(parent_donor_id) if parent_donor_id else None,
                    dt,
                    cifnif_norm_,
                    cifnif_raw,
                    _norm(nombre),
                    nn,
                    norm_email(email),
                    en,
                    cs,
                    _norm(telefono),
                    _norm(direccion),
                    _norm(ciudad),
                    _norm(notas),
                    now,
                    now,
                ),
            )
            donor_id = int(cur.lastrowid or 0)

        if donor_id:
            return donor_id

        row2 = con.execute(
            "SELECT id FROM donors WHERE tenant_id=? AND cifnif_norm=? LIMIT 1;",
            (tenant_id, cifnif_norm_),
        ).fetchone()
        if not row2:
            raise RuntimeError("No se pudo insertar/leer el donante.")
        return int(row2["id"])

    donor_id = int(row["id"])

    def pick(incoming: str, current: str, *, is_email: bool = False) -> str:
        if overwrite:
            return norm_email(incoming) if is_email else _norm(incoming)
        nv = _norm(incoming)
        if not nv:
            return _norm(current)
        return norm_email(incoming) if is_email else nv

    new_nombre = pick(nombre, row["nombre"] or "")
    new_email = pick(email, row["email"] or "", is_email=True)
    new_tel = pick(telefono, row["telefono"] or "")
    new_dir = pick(direccion, row["direccion"] or "")
    new_ciudad = pick(ciudad, row["ciudad"] or "")
    new_notas = pick(notas, row["notas"] or "")

    new_parent = row["parent_donor_id"]
    if overwrite:
        new_parent = int(parent_donor_id) if parent_donor_id else None
    else:
        if parent_donor_id:
            new_parent = int(parent_donor_id)

    if new_parent:
        _require_donor_in_tenant(con, tenant_id=tenant_id, donor_id=int(new_parent))

    current_type = _norm(row["donor_type"] or "UNKNOWN").upper()
    if overwrite or current_type in ("", "UNKNOWN"):
        new_type = dt
    else:
        new_type = current_type

    nn, en, cs = _build_donor_search_fields(nombre=new_nombre, email=new_email, cifnif_norm_=cifnif_norm_)

    with _write_tx(con):
        con.execute(
            """
            UPDATE donors
            SET parent_donor_id=?,
                donor_type=?,
                cifnif_raw=?,
                nombre=?,
                nombre_norm=?,
                email=?,
                email_norm=?,
                cifnif_search=?,
                telefono=?,
                direccion=?,
                ciudad=?,
                notas=?,
                updated_at=?
            WHERE tenant_id=? AND id=?;
            """,
            (
                new_parent,
                new_type,
                cifnif_raw,
                new_nombre,
                nn,
                new_email,
                en,
                cs,
                new_tel,
                new_dir,
                new_ciudad,
                new_notas,
                now,
                tenant_id,
                donor_id,
            ),
        )

    return donor_id


def get_donor_by_cif(con: sqlite3.Connection, *, tenant_id: str, cifnif: str) -> Optional[Dict[str, Any]]:
    assert_business_schema_once(con)
    row = con.execute(
        "SELECT * FROM donors WHERE tenant_id=? AND cifnif_norm=? LIMIT 1;",
        (_require_tenant_id(tenant_id), norm_cifnif(cifnif)),
    ).fetchone()
    return dict(row) if row else None


def get_donor(con: sqlite3.Connection, *, tenant_id: str, donor_id: int) -> Optional[Dict[str, Any]]:
    assert_business_schema_once(con)
    row = con.execute(
        "SELECT * FROM donors WHERE tenant_id=? AND id=? LIMIT 1;",
        (_require_tenant_id(tenant_id), int(donor_id)),
    ).fetchone()
    return dict(row) if row else None


def search_donors(con: sqlite3.Connection, *, tenant_id: str, q: str, limit: int = 50) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)
    qq = _norm_search(q)
    if not qq:
        return []

    like = f"%{qq}%"
    cur = con.execute(
        """
        SELECT *
        FROM donors
        WHERE tenant_id=?
          AND (
              cifnif_norm LIKE ?
              OR nombre_norm LIKE ?
              OR email_norm LIKE ?
              OR cifnif_search LIKE ?
          )
        ORDER BY updated_at DESC
        LIMIT ?;
        """,
        (tenant_id, like, like, like, like, int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def db_stats(con: sqlite3.Connection, *, tenant_id: str) -> Dict[str, int]:
    assert_business_schema_once(con)
    t = _require_tenant_id(tenant_id)
    out: Dict[str, int] = {}

    out["donors"] = int(con.execute("SELECT COUNT(*) FROM donors WHERE tenant_id=?;", (t,)).fetchone()[0])
    out["donations"] = int(con.execute("SELECT COUNT(*) FROM donations WHERE tenant_id=?;", (t,)).fetchone()[0])
    out["certificates"] = int(con.execute("SELECT COUNT(*) FROM certificates WHERE tenant_id=?;", (t,)).fetchone()[0])

    return out


def list_recent_donors(con: sqlite3.Connection, *, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    cur = con.execute(
        """
        SELECT *
        FROM donors
        WHERE tenant_id=?
        ORDER BY updated_at DESC, id DESC
        LIMIT ?;
        """,
        (_require_tenant_id(tenant_id), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def list_recent_certificates(con: sqlite3.Connection, *, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    cur = con.execute(
        """
        SELECT *
        FROM certificates
        WHERE tenant_id=?
        ORDER BY updated_at DESC, id DESC
        LIMIT ?;
        """,
        (_require_tenant_id(tenant_id), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def top_donors_by_certificates(con: sqlite3.Connection, *, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    cur = con.execute(
        """
        SELECT d.*,
               COUNT(c.id) AS n_cert
        FROM donors d
        LEFT JOIN certificates c
          ON c.tenant_id = d.tenant_id
         AND c.donor_id  = d.id
        WHERE d.tenant_id=?
        GROUP BY d.id
        ORDER BY n_cert DESC, d.updated_at DESC, d.id DESC
        LIMIT ?;
        """,
        (_require_tenant_id(tenant_id), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def count_donors_missing_type(con: sqlite3.Connection, *, tenant_id: str) -> int:
    assert_business_schema_once(con)
    row = con.execute(
        """
        SELECT COUNT(*) AS n
        FROM donors
        WHERE tenant_id=? AND (donor_type IS NULL OR donor_type='' OR donor_type='UNKNOWN');
        """,
        (_require_tenant_id(tenant_id),),
    ).fetchone()
    return int(row["n"]) if row else 0


def list_donors_missing_type(con: sqlite3.Connection, *, tenant_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    cur = con.execute(
        """
        SELECT *
        FROM donors
        WHERE tenant_id=? AND (donor_type IS NULL OR donor_type='' OR donor_type='UNKNOWN')
        ORDER BY updated_at DESC
        LIMIT ?;
        """,
        (_require_tenant_id(tenant_id), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def change_donor_cif(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donor_id: int,
    new_cifnif: str,
    changed_by: str = "",
    reason: str = "",
) -> None:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)
    if not donor_id:
        raise ValueError("donor_id requerido")

    new_raw = _norm(new_cifnif)
    new_norm = norm_cifnif(new_raw)
    if not new_norm:
        raise ValueError("new_cifnif requerido")

    now = utc_now_str()

    if is_anonymous_donor(con, donor_id=int(donor_id)):
        raise ValueError("No se puede cambiar CIF del DONANTE ANÓNIMO.")

    row = con.execute(
        "SELECT id, cifnif_raw, cifnif_norm, nombre, email, notas FROM donors WHERE tenant_id=? AND id=? LIMIT 1;",
        (tenant_id, int(donor_id)),
    ).fetchone()
    if not row:
        raise ValueError("Donante no existe")

    old_raw = _norm(row["cifnif_raw"] or "")
    old_norm = _norm(row["cifnif_norm"] or "")

    other = con.execute(
        "SELECT id FROM donors WHERE tenant_id=? AND cifnif_norm=? LIMIT 1;",
        (tenant_id, new_norm),
    ).fetchone()
    if other and int(other["id"]) != int(donor_id):
        raise ValueError("Ese CIF ya existe en otro donante. Usa merge_donors(source, target).")

    note = ""
    if reason or changed_by:
        note = f"[{now}] CIF cambiado por {(_norm(changed_by) or 'system')}: {(_norm(reason) or 'sin motivo')}"

    nn, en, cs = _build_donor_search_fields(
        nombre=_norm(row["nombre"] or ""),
        email=norm_email(row["email"] or ""),
        cifnif_norm_=new_norm,
    )

    with _write_tx(con):
        if new_norm != old_norm:
            _insert_tax_id_history(
                con,
                tenant_id=tenant_id,
                donor_id=int(donor_id),
                old_norm=old_norm,
                old_raw=old_raw,
                new_norm=new_norm,
                new_raw=new_raw,
                changed_by=changed_by,
                reason=reason,
            )

        if note:
            con.execute(
                """
                UPDATE donors
                SET cifnif_norm=?, cifnif_raw=?,
                    nombre_norm=?,
                    email_norm=?,
                    cifnif_search=?,
                    notas=TRIM(COALESCE(notas,'') || '\n' || ?),
                    updated_at=?
                WHERE tenant_id=? AND id=?;
                """,
                (new_norm, new_raw, nn, en, cs, note, now, tenant_id, int(donor_id)),
            )
        else:
            con.execute(
                """
                UPDATE donors
                SET cifnif_norm=?, cifnif_raw=?,
                    nombre_norm=?,
                    email_norm=?,
                    cifnif_search=?,
                    updated_at=?
                WHERE tenant_id=? AND id=?;
                """,
                (new_norm, new_raw, nn, en, cs, now, tenant_id, int(donor_id)),
            )


def merge_donors(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    source_donor_id: int,
    target_donor_id: int,
    merged_by: str = "",
    reason: str = "",
    keep_source_as_child: bool = True,
    prefer: str = "fill_missing",  # "fill_missing" | "target" | "source"
) -> None:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)

    s = int(source_donor_id)
    t = int(target_donor_id)
    if not s or not t or s == t:
        raise ValueError("source_donor_id y target_donor_id deben ser distintos y > 0")

    if is_anonymous_donor(con, donor_id=s) or is_anonymous_donor(con, donor_id=t):
        raise ValueError("No se puede fusionar con DONANTE ANÓNIMO.")

    srow = con.execute("SELECT * FROM donors WHERE tenant_id=? AND id=?;", (tenant_id, s)).fetchone()
    trow = con.execute("SELECT * FROM donors WHERE tenant_id=? AND id=?;", (tenant_id, t)).fetchone()
    if not srow or not trow:
        raise ValueError("Donante source/target no existe")

    now = utc_now_str()
    note = f"[{now}] MERGE {s} -> {t} por {(_norm(merged_by) or 'system')}: {(_norm(reason) or 'sin motivo')}"

    prefer = (_norm(prefer) or "fill_missing").lower()
    if prefer not in {"fill_missing", "target", "source"}:
        prefer = "fill_missing"

    def _pick_field(field: str) -> str:
        sv = _norm(srow[field] or "")
        tv = _norm(trow[field] or "")
        if prefer == "target":
            return tv
        if prefer == "source":
            return sv or tv
        return tv if tv else sv

    new_nombre = _pick_field("nombre")
    new_email = norm_email(_pick_field("email"))
    new_tel = _pick_field("telefono")
    new_dir = _pick_field("direccion")
    new_ciudad = _pick_field("ciudad")

    stype = _norm(srow["donor_type"] or "UNKNOWN").upper()
    ttype = _norm(trow["donor_type"] or "UNKNOWN").upper()
    new_type = ttype
    if ttype in ("", "UNKNOWN") and stype not in ("", "UNKNOWN"):
        new_type = stype

    cif_target = _norm(trow["cifnif_norm"] or "")
    nn, en, cs = _build_donor_search_fields(nombre=new_nombre, email=new_email, cifnif_norm_=cif_target)

    with _write_tx(con):
        con.execute(
            "UPDATE donations SET donor_id=?, updated_at=? WHERE tenant_id=? AND donor_id=?;",
            (t, now, tenant_id, s),
        )
        con.execute(
            "UPDATE certificates SET donor_id=?, updated_at=? WHERE tenant_id=? AND donor_id=?;",
            (t, now, tenant_id, s),
        )

        con.execute(
            """
            UPDATE donors
            SET nombre=?,
                nombre_norm=?,
                email=?,
                email_norm=?,
                cifnif_search=?,
                telefono=?,
                direccion=?,
                ciudad=?,
                donor_type=?,
                notas=TRIM(COALESCE(notas,'') || '\n' || ?),
                updated_at=?
            WHERE tenant_id=? AND id=?;
            """,
            (new_nombre, nn, new_email, en, cs, new_tel, new_dir, new_ciudad, new_type, note, now, tenant_id, t),
        )

        if keep_source_as_child:
            con.execute(
                """
                UPDATE donors
                SET parent_donor_id=?,
                    notas=TRIM(COALESCE(notas,'') || '\n' || ?),
                    updated_at=?
                WHERE tenant_id=? AND id=?;
                """,
                (t, note, now, tenant_id, s),
            )
        else:
            con.execute(
                """
                UPDATE donors
                SET notas=TRIM(COALESCE(notas,'') || '\n' || ?),
                    updated_at=?
                WHERE tenant_id=? AND id=?;
                """,
                (note, now, tenant_id, s),
            )


# =============================
# Imports
# =============================


def create_import(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    uploaded_by: str = "",
    source_filename: str = "",
    source_bytes: Optional[bytes] = None,
    total_rows: int = 0,
) -> int:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)

    sha = sha256_bytes(source_bytes) if source_bytes else None

    with _write_tx(con):
        cur = con.execute(
            """
            INSERT INTO imports(tenant_id, uploaded_by, source_filename, source_sha256, total_rows)
            VALUES (?, ?, ?, ?, ?);
            """,
            (tenant_id, _norm(uploaded_by), _norm(source_filename), sha, int(total_rows or 0)),
        )
        import_id = int(cur.lastrowid or 0)

    return import_id


def set_import_total_rows(con: sqlite3.Connection, *, tenant_id: str, import_id: int, total_rows: int) -> None:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)
    _require_import_in_tenant(con, tenant_id=tenant_id, import_id=int(import_id))
    with _write_tx(con):
        con.execute("UPDATE imports SET total_rows=? WHERE tenant_id=? AND id=?;", (int(total_rows or 0), tenant_id, int(import_id)))


# =============================
# Donations
# =============================


def insert_donation(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donor_id: int,
    import_id: Optional[int] = None,
    row_index: Optional[int] = None,
    row_hash: Optional[str] = None,
    fecha: Any = "",
    tipo: str = "",
    importe: Optional[float] = None,
    kg: Optional[float] = None,
    fuente: str = "excel",
    raw_json: str = "",
) -> int:
    """
    ✅ P0: valida donor_id/import_id dentro del tenant
    """
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)
    if not donor_id:
        raise ValueError("donor_id requerido")

    _require_donor_in_tenant(con, tenant_id=tenant_id, donor_id=int(donor_id))
    if import_id:
        _require_import_in_tenant(con, tenant_id=tenant_id, import_id=int(import_id))

    now = utc_now_str()

    iso = _parse_date_to_iso(fecha)
    y = _year_from_iso(iso)

    if fecha is None:
        fecha_str = ""
    elif isinstance(fecha, (datetime, date)):
        fecha_str = _parse_date_to_iso(fecha) or str(fecha)
    else:
        fecha_str = _norm(str(fecha))

    rh = _norm(row_hash)
    if not rh:
        rh = make_row_hash(tenant_id, donor_id, fecha_str, iso, _norm(tipo).upper(), importe, kg, row_index)

    existing = con.execute(
        "SELECT id FROM donations WHERE tenant_id=? AND row_hash=? LIMIT 1;",
        (tenant_id, rh),
    ).fetchone()
    if existing:
        return int(existing["id"])

    with _write_tx(con):
        cur = con.execute(
            """
            INSERT OR IGNORE INTO donations(
                tenant_id, donor_id, import_id, row_index, row_hash,
                fecha, fecha_iso, year_int,
                tipo, importe, kg, fuente, raw_json,
                is_void, void_reason, voided_at, voided_by,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?);
            """,
            (
                tenant_id,
                int(donor_id),
                int(import_id) if import_id else None,
                int(row_index) if row_index is not None else None,
                rh,
                fecha_str,
                iso or None,
                int(y) if y else None,
                _norm(tipo).upper(),
                float(importe) if importe is not None else None,
                float(kg) if kg is not None else None,
                _norm(fuente),
                raw_json or "",
                now,
                now,
            ),
        )
        new_id = int(cur.lastrowid or 0)

    if new_id:
        return new_id

    row2 = con.execute(
        "SELECT id FROM donations WHERE tenant_id=? AND row_hash=? LIMIT 1;",
        (tenant_id, rh),
    ).fetchone()
    if row2:
        return int(row2["id"])

    raise RuntimeError("No se pudo insertar la donación.")


def list_donations_by_donor(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donor_id: int,
    limit: int = 200,
    include_void: bool = False,
) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)
    _require_donor_in_tenant(con, tenant_id=tenant_id, donor_id=int(donor_id))

    where_void = "" if include_void else "AND is_void=0"
    cur = con.execute(
        f"""
        SELECT *
        FROM donations
        WHERE tenant_id=? AND donor_id=? {where_void}
        ORDER BY COALESCE(fecha_iso, fecha, created_at) DESC, id DESC
        LIMIT ?;
        """,
        (tenant_id, int(donor_id), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def preview_void_donation_impact(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donation_id: int,
) -> Dict[str, Any]:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)

    drow = con.execute(
        "SELECT id, donor_id, year_int, is_void FROM donations WHERE tenant_id=? AND id=? LIMIT 1;",
        (tenant_id, int(donation_id)),
    ).fetchone()
    if not drow:
        raise ValueError("Donación no existe")

    certs = con.execute(
        """
        SELECT c.id, c.numerocertificado, c.year, c.is_void, c.status_certificado
        FROM certificates c
        JOIN certificate_items ci ON ci.certificate_id = c.id
        WHERE c.tenant_id=? AND ci.donation_id=?
        ORDER BY COALESCE(c.year, 0) DESC, c.id DESC;
        """,
        (tenant_id, int(donation_id)),
    ).fetchall()

    return {
        "donation_id": int(drow["id"]),
        "donor_id": int(drow["donor_id"]),
        "year_int": int(drow["year_int"]) if drow["year_int"] is not None else None,
        "already_void": bool(int(drow["is_void"] or 0)),
        "affected_certificates": [dict(r) for r in certs],
        "affected_count": int(len(certs)),
    }


def _mark_certificates_void_for_donation(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donation_id: int,
    reason: str,
    voided_by: str,
) -> int:
    tenant_id = _require_tenant_id(tenant_id)
    now = utc_now_str()

    cert_rows = con.execute(
        """
        SELECT DISTINCT c.id
        FROM certificates c
        JOIN certificate_items ci ON ci.certificate_id = c.id
        WHERE c.tenant_id=? AND ci.donation_id=?;
        """,
        (tenant_id, int(donation_id)),
    ).fetchall()

    if not cert_rows:
        return 0

    cert_ids = [int(r["id"]) for r in cert_rows]
    with _write_tx(con):
        con.executemany(
            """
            UPDATE certificates
            SET is_void=1,
                status_certificado='VOID',
                void_reason=?,
                voided_at=?,
                voided_by=?,
                updated_at=?
            WHERE tenant_id=? AND id=?;
            """,
            [(_norm(reason), now, _norm(voided_by), now, tenant_id, cid) for cid in cert_ids],
        )
    return len(cert_ids)


def void_donation(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donation_id: int,
    reason: str,
    voided_by: str = "",
    require_explicit_ack: bool = True,
) -> Dict[str, Any]:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)
    if not donation_id:
        raise ValueError("donation_id requerido")

    impact = preview_void_donation_impact(con, tenant_id=tenant_id, donation_id=int(donation_id))
    if require_explicit_ack and impact["affected_count"] > 0:
        raise ValueError(
            "Esta donación pertenece a uno o más certificados. "
            "Debes confirmar explícitamente en la UI antes de anularla."
        )

    now = utc_now_str()
    with _write_tx(con):
        con.execute(
            """
            UPDATE donations
            SET is_void=1,
                void_reason=?,
                voided_at=?,
                voided_by=?,
                updated_at=?
            WHERE tenant_id=? AND id=?;
            """,
            (_norm(reason), now, _norm(voided_by), now, tenant_id, int(donation_id)),
        )

        affected = _mark_certificates_void_for_donation(
            con,
            tenant_id=tenant_id,
            donation_id=int(donation_id),
            reason=reason,
            voided_by=voided_by,
        )

    impact["certificates_voided"] = int(affected)
    return impact


# =============================
# Certificates
# =============================


def upsert_certificate(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donor_id: int,
    numerocertificado: str,
    year: Optional[int] = None,
    seq: Optional[int] = None,
    hash_: str = "",
    tipo: str = "",
    fecha_emision: str = "",
    status_certificado: str = "",
    status_carta: str = "",
    donation_id: Optional[int] = None,  # LEGACY (IGNORADO)
    import_id: Optional[int] = None,
    created_by: str = "",
    cert_pdf_path: str = "",
    carta_pdf_path: str = "",
    email_to: str = "",
    email_status: str = "",
    emailed_at: str = "",
    meta_snapshot_json: str = "",
    snapshot_update_policy: str = "preserve",  # "preserve" | "overwrite"
) -> int:
    """
    ✅ P0: valida donor_id/import_id dentro del tenant
    """
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)
    if not donor_id:
        raise ValueError("donor_id requerido")

    _require_donor_in_tenant(con, tenant_id=tenant_id, donor_id=int(donor_id))
    if import_id:
        _require_import_in_tenant(con, tenant_id=tenant_id, import_id=int(import_id))

    num = _norm(numerocertificado)
    if not num:
        raise ValueError("numerocertificado requerido")

    incoming_snapshot = sanitize_snapshot_json(meta_snapshot_json)
    now = utc_now_str()

    row = con.execute(
        "SELECT id, meta_snapshot_json FROM certificates WHERE tenant_id=? AND numerocertificado=? LIMIT 1;",
        (tenant_id, num),
    ).fetchone()

    pol = (_norm(snapshot_update_policy) or "preserve").lower()
    if pol not in {"preserve", "overwrite"}:
        pol = "preserve"

    def _snap_value(existing: Any) -> str:
        if pol == "overwrite":
            return incoming_snapshot or ""
        ex = (existing or "") if existing is not None else ""
        return ex if str(ex).strip() else (incoming_snapshot or "")

    if not row:
        with _write_tx(con):
            cur = con.execute(
                """
                INSERT INTO certificates(
                    tenant_id, donor_id, donation_id, import_id,
                    numerocertificado, year, seq, hash, tipo,
                    fecha_emision, status_certificado, status_carta,
                    is_void, void_reason, voided_at, voided_by,
                    cert_pdf_path, carta_pdf_path, created_by,
                    email_to, email_status, emailed_at,
                    meta_snapshot_json, snapshot_update_policy,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    tenant_id,
                    int(donor_id),
                    int(import_id) if import_id else None,
                    num,
                    int(year) if year is not None else None,
                    int(seq) if seq is not None else None,
                    _norm(hash_),
                    _norm(tipo).upper(),
                    _norm(fecha_emision),
                    _norm(status_certificado),
                    _norm(status_carta),
                    _norm(cert_pdf_path),
                    _norm(carta_pdf_path),
                    _norm(created_by),
                    norm_email(email_to),
                    _norm(email_status),
                    _norm(emailed_at),
                    incoming_snapshot or "",
                    pol,
                    now,
                    now,
                ),
            )
            cert_id = int(cur.lastrowid or 0)

        if cert_id:
            return cert_id

        row2 = con.execute(
            "SELECT id FROM certificates WHERE tenant_id=? AND numerocertificado=? LIMIT 1;",
            (tenant_id, num),
        ).fetchone()
        if not row2:
            raise RuntimeError("No se pudo insertar/leer el certificado.")
        return int(row2["id"])

    cert_id = int(row["id"])
    snap_final = _snap_value(row["meta_snapshot_json"] if row else "")

    with _write_tx(con):
        con.execute(
            """
            UPDATE certificates
            SET donor_id=?,
                donation_id=NULL,
                import_id=COALESCE(?, import_id),
                year=COALESCE(?, year),
                seq=COALESCE(?, seq),
                hash=COALESCE(NULLIF(?,''), hash),
                tipo=COALESCE(NULLIF(?,''), tipo),
                fecha_emision=COALESCE(NULLIF(?,''), fecha_emision),
                status_certificado=COALESCE(NULLIF(?,''), status_certificado),
                status_carta=COALESCE(NULLIF(?,''), status_carta),
                cert_pdf_path=COALESCE(NULLIF(?,''), cert_pdf_path),
                carta_pdf_path=COALESCE(NULLIF(?,''), carta_pdf_path),
                created_by=COALESCE(NULLIF(?,''), created_by),
                email_to=COALESCE(NULLIF(?,''), email_to),
                email_status=COALESCE(NULLIF(?,''), email_status),
                emailed_at=COALESCE(NULLIF(?,''), emailed_at),
                meta_snapshot_json=?,
                snapshot_update_policy=COALESCE(NULLIF(?,''), snapshot_update_policy),
                updated_at=?
            WHERE tenant_id=? AND id=?;
            """,
            (
                int(donor_id),
                int(import_id) if import_id else None,
                int(year) if year is not None else None,
                int(seq) if seq is not None else None,
                _norm(hash_),
                _norm(tipo).upper(),
                _norm(fecha_emision),
                _norm(status_certificado),
                _norm(status_carta),
                _norm(cert_pdf_path),
                _norm(carta_pdf_path),
                _norm(created_by),
                norm_email(email_to),
                _norm(email_status),
                _norm(emailed_at),
                snap_final,
                pol,
                now,
                tenant_id,
                cert_id,
            ),
        )

    return cert_id


def link_certificate_to_donations(
    con: sqlite3.Connection,
    *,
    certificate_id: int,
    donation_ids: List[int],
    cleanup_legacy: bool = True,
) -> int:
    """
    ✅ P0: enforce cross-tenant safety (cert y donations deben ser del mismo tenant)
    """
    assert_business_schema_once(con)
    if not certificate_id:
        raise ValueError("certificate_id requerido")
    if not donation_ids:
        return 0

    cert_tenant = _get_certificate_tenant(con, int(certificate_id))
    if not cert_tenant:
        raise ValueError("certificate_id no existe")

    rows: list[tuple[int, int]] = []
    for did in donation_ids:
        did_i = int(did)
        if did_i <= 0:
            continue
        dt = _get_donation_tenant(con, did_i)
        if not dt:
            raise ValueError(f"donation_id no existe: {did_i}")
        if dt != cert_tenant:
            raise ValueError("No se puede linkear: donation_id pertenece a otro tenant.")
        rows.append((int(certificate_id), did_i))

    if not rows:
        return 0

    before = con.execute(
        "SELECT COUNT(*) FROM certificate_items WHERE certificate_id=?;",
        (int(certificate_id),),
    ).fetchone()
    before_n = int(before[0]) if before else 0

    with _write_tx(con):
        con.executemany(
            "INSERT OR IGNORE INTO certificate_items(certificate_id, donation_id) VALUES (?, ?);",
            rows,
        )
        if cleanup_legacy:
            con.execute("UPDATE certificates SET donation_id=NULL WHERE id=?;", (int(certificate_id),))

    after = con.execute(
        "SELECT COUNT(*) FROM certificate_items WHERE certificate_id=?;",
        (int(certificate_id),),
    ).fetchone()
    after_n = int(after[0]) if after else 0

    return max(0, after_n - before_n)


def list_certificates_by_donor(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donor_id: int,
    limit: int = 200,
    include_void: bool = False,
) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)
    _require_donor_in_tenant(con, tenant_id=tenant_id, donor_id=int(donor_id))

    where_void = "" if include_void else "AND is_void=0"
    cur = con.execute(
        f"""
        SELECT *
        FROM certificates
        WHERE tenant_id=? AND donor_id=? {where_void}
        ORDER BY COALESCE(year, 0) DESC, COALESCE(fecha_emision, created_at) DESC, id DESC
        LIMIT ?;
        """,
        (tenant_id, int(donor_id), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def search_certificates(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    q: str,
    limit: int = 200,
    include_void: bool = False,
) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)

    qq = f"%{_norm_casefold(q)}%"
    where_void = "" if include_void else "AND is_void=0"
    cur = con.execute(
        f"""
        SELECT *
        FROM certificates
        WHERE tenant_id=?
          {where_void}
          AND (
              lower(coalesce(numerocertificado,'')) LIKE ?
              OR lower(coalesce(hash,'')) LIKE ?
              OR lower(coalesce(email_to,'')) LIKE ?
              OR lower(coalesce(tipo,'')) LIKE ?
          )
        ORDER BY updated_at DESC
        LIMIT ?;
        """,
        (tenant_id, qq, qq, qq, qq, int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def set_certificate_email_status(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    numerocertificado: str,
    email_to: str,
    email_status: str,
    emailed_at: Optional[str] = None,
) -> None:
    assert_business_schema_once(con)
    tenant_id = _norm(tenant_id)
    num = _norm(numerocertificado)
    if not tenant_id or not num:
        return
    now = utc_now_str()

    with _write_tx(con):
        con.execute(
            """
            UPDATE certificates
            SET email_to=?,
                email_status=?,
                emailed_at=?,
                updated_at=?
            WHERE tenant_id=? AND numerocertificado=?;
            """,
            (
                norm_email(email_to),
                _norm(email_status),
                _norm(emailed_at) or now,
                now,
                tenant_id,
                num,
            ),
        )


# =============================
# ✅ Zombie healer
# =============================


def heal_zombie_certificates(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    void_reason: str = "AUTO: contiene donación anulada",
    voided_by: str = "system",
    limit: int = 5000,
) -> Dict[str, Any]:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)

    now = utc_now_str()

    rows = con.execute(
        """
        SELECT DISTINCT c.id, c.numerocertificado
        FROM certificates c
        JOIN certificate_items ci ON ci.certificate_id = c.id
        JOIN donations d ON d.id = ci.donation_id
        WHERE c.tenant_id=?
          AND c.is_void=0
          AND d.is_void=1
        ORDER BY COALESCE(c.year, 0) DESC, c.id DESC
        LIMIT ?;
        """,
        (tenant_id, int(limit)),
    ).fetchall()

    if not rows:
        return {"healed": 0, "sample": []}

    ids = [int(r["id"]) for r in rows]
    sample = [str(r["numerocertificado"]) for r in rows[:50]]

    with _write_tx(con):
        con.executemany(
            """
            UPDATE certificates
            SET is_void=1,
                status_certificado='VOID',
                void_reason=?,
                voided_at=?,
                voided_by=?,
                updated_at=?
            WHERE tenant_id=? AND id=? AND is_void=0;
            """,
            [(_norm(void_reason), now, _norm(voided_by), now, tenant_id, cid) for cid in ids],
        )

    return {"healed": int(len(ids)), "sample": sample}


# =============================
# Email logs
# =============================


def insert_email_log(
    con: sqlite3.Connection,
    *,
    tenant_id: Optional[str],
    certificate_id: Optional[int],
    to_email: str,
    subject: str,
    status: str,
    error: str = "",
    sent_by: str = "",
    sent_at: Optional[str] = None,
) -> int:
    assert_business_schema_once(con)

    with _write_tx(con):
        cur = con.execute(
            """
            INSERT INTO email_logs(tenant_id, certificate_id, to_email, subject, status, error, sent_by, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                _norm(tenant_id or ""),
                int(certificate_id) if certificate_id else None,
                norm_email(to_email),
                _norm(subject),
                _norm(status),
                (error or "")[:1000],
                _norm(sent_by),
                _norm(sent_at) or utc_now_str(),
            ),
        )
        new_id = int(cur.lastrowid or 0)

    return new_id


def list_email_logs(con: sqlite3.Connection, *, tenant_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    assert_business_schema_once(con)
    cur = con.execute(
        """
        SELECT *
        FROM email_logs
        WHERE tenant_id=?
        ORDER BY id DESC
        LIMIT ?;
        """,
        (_require_tenant_id(tenant_id), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


# =============================
# ✅ Data health scanner
# =============================


def check_data_health_legacy(con: sqlite3.Connection, *, tenant_id: str) -> Dict[str, Any]:
    assert_business_schema_once(con)
    tenant_id = _require_tenant_id(tenant_id)

    row = con.execute(
        """
        SELECT COUNT(*) AS n
        FROM donors
        WHERE tenant_id=? AND (donor_type IS NULL OR donor_type='' OR donor_type='UNKNOWN');
        """,
        (tenant_id,),
    ).fetchone()
    donors_unknown = int(row["n"]) if row else 0

    donors_unknown_sample = con.execute(
        """
        SELECT id, cifnif_norm, nombre, email, updated_at
        FROM donors
        WHERE tenant_id=? AND (donor_type IS NULL OR donor_type='' OR donor_type='UNKNOWN')
        ORDER BY updated_at DESC
        LIMIT 50;
        """,
        (tenant_id,),
    ).fetchall()

    row2 = con.execute(
        """
        SELECT COUNT(*) AS n
        FROM donations
        WHERE tenant_id=? AND is_void=0 AND (year_int IS NULL OR year_int=0);
        """,
        (tenant_id,),
    ).fetchone()
    donations_missing_year = int(row2["n"]) if row2 else 0

    donations_missing_year_sample = con.execute(
        """
        SELECT id, donor_id, fecha, tipo, importe, kg, created_at
        FROM donations
        WHERE tenant_id=? AND is_void=0 AND (year_int IS NULL OR year_int=0)
        ORDER BY created_at DESC
        LIMIT 50;
        """,
        (tenant_id,),
    ).fetchall()

    row3 = con.execute(
        """
        SELECT COUNT(*) AS n
        FROM certificates c
        JOIN certificate_items ci ON ci.certificate_id = c.id
        JOIN donations d ON d.id = ci.donation_id
        WHERE c.tenant_id=?
          AND c.is_void=0
          AND d.is_void=1;
        """,
        (tenant_id,),
    ).fetchone()
    zombie_certs = int(row3["n"]) if row3 else 0

    zombie_certs_sample = con.execute(
        """
        SELECT DISTINCT c.id, c.numerocertificado, c.year, c.status_certificado
        FROM certificates c
        JOIN certificate_items ci ON ci.certificate_id = c.id
        JOIN donations d ON d.id = ci.donation_id
        WHERE c.tenant_id=?
          AND c.is_void=0
          AND d.is_void=1
        ORDER BY COALESCE(c.year, 0) DESC, c.id DESC
        LIMIT 50;
        """,
        (tenant_id,),
    ).fetchall()

    tax_hist_sample = con.execute(
        """
        SELECT id, donor_id, old_cifnif_norm, new_cifnif_norm, changed_at, changed_by
        FROM donor_tax_id_history
        WHERE tenant_id=?
        ORDER BY id DESC
        LIMIT 50;
        """,
        (tenant_id,),
    ).fetchall()

    return {
        "tenant_id": tenant_id,
        "donors_unknown_type": donors_unknown,
        "donors_unknown_sample": [dict(r) for r in donors_unknown_sample],
        "donations_missing_year": donations_missing_year,
        "donations_missing_year_sample": [dict(r) for r in donations_missing_year_sample],
        "zombie_certificates": zombie_certs,
        "zombie_certificates_sample": [dict(r) for r in zombie_certs_sample],
        "tax_id_history_sample": [dict(r) for r in tax_hist_sample],
    }


# =============================
# ✅ Donor CRM editor (v6 + history)
# =============================


def update_donor_details(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    donor_id: int,
    nombre: str,
    email: str,
    cifnif: str,
    allow_cif_change: bool = True,
    changed_by: str = "",
    reason: str = "",
) -> None:
    """
    ✅ P0: tenant_id requerido (NO default fallback)
    ✅ Política anónimo coherente: NO editable
    """
    assert_business_schema_once(con)

    tenant_id = _require_tenant_id(tenant_id)
    donor_id = int(donor_id)
    if donor_id <= 0:
        raise ValueError("donor_id requerido")

    nombre = (nombre or "").strip()
    email = (email or "").strip()

    cif_raw = (clean_tax_id(cifnif) or "").strip()
    cif_norm = norm_cifnif(cif_raw)

    if not nombre:
        raise ValueError("El nombre no puede estar vacío.")

    if email and (not is_valid_email(email)):
        raise ValueError("Email inválido.")

    row = con.execute(
        "SELECT id, tenant_id, cifnif_raw, cifnif_norm FROM donors WHERE id=? AND tenant_id=? LIMIT 1;",
        (donor_id, tenant_id),
    ).fetchone()
    if not row:
        raise ValueError("Donante no encontrado.")

    if is_anonymous_donor(con, donor_id=donor_id):
        raise ValueError("No se puede editar el DONANTE ANÓNIMO.")

    old_raw = (row["cifnif_raw"] or "").strip()
    old_norm = (row["cifnif_norm"] or "").strip()

    if (cif_norm != old_norm) and (not allow_cif_change):
        raise ValueError("Cambio de CIF deshabilitado por configuración.")

    if cif_norm and cif_norm != old_norm:
        other = con.execute(
            "SELECT id FROM donors WHERE tenant_id=? AND cifnif_norm=? AND id<>? LIMIT 1;",
            (tenant_id, cif_norm, donor_id),
        ).fetchone()
        if other:
            raise ValueError(
                "Ya existe otro donante con ese CIF/NIF en este tenant. "
                "No hago merge automático (por seguridad)."
            )

    now = utc_now_str()

    effective_cif = cif_norm if cif_norm else old_norm
    nn, en, cs = _build_donor_search_fields(
        nombre=nombre,
        email=norm_email(email),
        cifnif_norm_=effective_cif,
    )

    with _write_tx(con):
        if cif_norm and cif_norm != old_norm:
            _insert_tax_id_history(
                con,
                tenant_id=tenant_id,
                donor_id=donor_id,
                old_norm=old_norm,
                old_raw=old_raw,
                new_norm=cif_norm,
                new_raw=cif_raw,
                changed_by=changed_by,
                reason=reason,
            )

        con.execute(
            """
            UPDATE donors
            SET nombre=?,
                nombre_norm=?,
                email=?,
                email_norm=?,
                cifnif_raw=?,
                cifnif_norm=?,
                cifnif_search=?,
                updated_at=?
            WHERE id=? AND tenant_id=?;
            """,
            (
                nombre,
                nn,
                norm_email(email),
                en,
                cif_raw,
                effective_cif,
                cs,
                now,
                donor_id,
                tenant_id,
            ),
        )