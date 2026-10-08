# backend/bulk_queue.py
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.mailer import smtp_config_ok, send_email_with_attachments


def utc_now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def extract_domain(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return ""
    return email.split("@", 1)[1].strip()


def is_valid_email(email: str) -> bool:
    email = (email or "").strip()
    if not email or "@" not in email:
        return False
    if email.count("@") != 1:
        return False
    user, domain = email.split("@", 1)
    return bool(user) and "." in domain and " " not in email


def make_job_key(cif: str, to_email: str, tenant_id: str) -> str:
    raw = f"{(tenant_id or '').strip()}|{(cif or '').strip().upper()}|{(to_email or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


EMAIL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    tenant_id TEXT NOT NULL,

    cifnif TEXT NOT NULL,
    to_email TEXT NOT NULL,
    domain TEXT,

    job_key TEXT NOT NULL UNIQUE,

    status TEXT NOT NULL,          -- PENDING / SENDING / SENT / FAILED / SKIPPED / SIM_SENT
    attempts INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    last_error TEXT,

    numerocertificado TEXT,
    hash TEXT,
    tipo TEXT,
    fecha_emision TEXT,

    -- auditoría
    enqueued_by TEXT,
    enqueued_at TEXT,
    started_by TEXT,
    started_at TEXT
);
"""

INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_deliveries_tenant ON deliveries(tenant_id);",
    "CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);",
    "CREATE INDEX IF NOT EXISTS idx_deliveries_domain ON deliveries(domain);",
    "CREATE INDEX IF NOT EXISTS idx_deliveries_cif ON deliveries(cifnif);",
    "CREATE INDEX IF NOT EXISTS idx_deliveries_jobkey ON deliveries(job_key);",
]


@dataclass
class QueueConfig:
    max_attempts: int = 3
    sleep_between_ms: int = 300
    backoff_base_ms: int = 800
    test_mode: bool = True

    domain_min_gap_seconds: float = 1.2
    domain_burst: int = 2

    stop_after_success: Optional[int] = None
    attach_cert: bool = True
    attach_letter: bool = True


class BulkEmailQueue:
    def __init__(self, queue_db_path: Path):
        self.queue_db_path = Path(queue_db_path)
        self._init_db()

        self._lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_flag = False

        self._last_domain_sent_at: Dict[str, float] = {}
        self._domain_streak: Dict[str, int] = {}
        self._success_count = 0

        self._started_by: Optional[str] = None
        self._worker_tenant_id: Optional[str] = None

        # snapshot de settings del tenant (incluye user/source_filename si se lo pasan)
        self._worker_tenant_cfg: Optional[dict] = None

        # ✅ rutas DBs
        self._app_db_path: Optional[Path] = None   # AUTH_DB (auth/settings/counters)
        self._biz_db_path: Optional[Path] = None   # BIZ_DB (donors/donations/certs)

    # ---------------- Connections ----------------
    def _connect_queue(self) -> sqlite3.Connection:
        # ✅ P0: isolation_level=None (autocommit) => SAVEPOINT/BEGIN IMMEDIATE seguros y consistentes
        con = sqlite3.connect(self.queue_db_path, check_same_thread=False, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _connect_app(self) -> sqlite3.Connection:
        if not self._app_db_path:
            raise RuntimeError("app_db_path no configurado en start_worker().")
        # ✅ P0: isolation_level=None
        con = sqlite3.connect(self._app_db_path, check_same_thread=False, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _connect_biz(self) -> sqlite3.Connection:
        if not self._biz_db_path:
            raise RuntimeError("biz_db_path no configurado en start_worker().")
        # ✅ P0: isolation_level=None
        con = sqlite3.connect(self._biz_db_path, check_same_thread=False, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    # ---------------- DB init/migrate ----------------
    def _column_exists(self, con: sqlite3.Connection, table: str, column: str) -> bool:
        rows = con.execute(f"PRAGMA table_info({table});").fetchall()
        return any((r["name"] == column) for r in rows)

    def _migrate_if_needed(self, con: sqlite3.Connection) -> None:
        needed = [
            ("tenant_id", "TEXT"),
            ("job_key", "TEXT"),
            ("enqueued_by", "TEXT"),
            ("enqueued_at", "TEXT"),
            ("started_by", "TEXT"),
            ("started_at", "TEXT"),
        ]
        for col, ctype in needed:
            if not self._column_exists(con, "deliveries", col):
                con.execute(f"ALTER TABLE deliveries ADD COLUMN {col} {ctype};")

        if self._column_exists(con, "deliveries", "tenant_id"):
            con.execute("UPDATE deliveries SET tenant_id = COALESCE(tenant_id, 'default');")

        # con ya va en autocommit, pero commit explícito no molesta aquí (migración)
        try:
            con.commit()
        except Exception:
            pass

    def _init_db(self) -> None:
        self.queue_db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect_queue() as con:
            con.execute(EMAIL_TABLE_SQL)
            self._migrate_if_needed(con)
            for sql in INDEXES_SQL:
                try:
                    con.execute(sql)
                except Exception:
                    pass
            try:
                con.commit()
            except Exception:
                pass

    # ---------------- API: enqueue ----------------
    def enqueue_many(
        self,
        items: List[Tuple[str, str]],
        max_attempts: int = 3,
        enqueued_by: Optional[str] = None,
        tenant_id: str = "default",
    ) -> int:
        now = utc_now_str()
        rows = []

        tenant_id = (tenant_id or "default").strip() or "default"

        for cif, email in items:
            cif = (cif or "").strip().upper()
            email = (email or "").strip()
            if not cif:
                continue

            domain = extract_domain(email)
            job_key = make_job_key(cif, email, tenant_id)

            if not is_valid_email(email):
                status = "SKIPPED"
                last_error = "Email inválido"
            else:
                status = "PENDING"
                last_error = None

            rows.append(
                (
                    now,
                    now,
                    tenant_id,
                    cif,
                    email,
                    domain,
                    job_key,
                    status,
                    0,
                    int(max_attempts),
                    last_error,
                    None,
                    None,
                    None,
                    None,
                    enqueued_by,
                    now,
                    None,
                    None,
                )
            )

        if not rows:
            return 0

        with self._connect_queue() as con:
            con.executemany(
                """
                INSERT OR IGNORE INTO deliveries(
                    created_at, updated_at,
                    tenant_id,
                    cifnif, to_email, domain,
                    job_key,
                    status, attempts, max_attempts, last_error,
                    numerocertificado, hash, tipo, fecha_emision,
                    enqueued_by, enqueued_at,
                    started_by, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            try:
                con.commit()
            except Exception:
                pass
            inserted = con.execute("SELECT changes()").fetchone()[0]
            return int(inserted)

    def list_deliveries(self, limit: int = 200, tenant_id: str = "default") -> List[Dict[str, Any]]:
        tenant_id = (tenant_id or "default").strip() or "default"
        with self._connect_queue() as con:
            cur = con.execute(
                "SELECT * FROM deliveries WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def stats(self, tenant_id: str = "default") -> Dict[str, int]:
        tenant_id = (tenant_id or "default").strip() or "default"
        with self._connect_queue() as con:
            cur = con.execute(
                "SELECT status, COUNT(*) as n FROM deliveries WHERE tenant_id=? GROUP BY status",
                (tenant_id,),
            )
            out = {"PENDING": 0, "SENDING": 0, "SENT": 0, "FAILED": 0, "SKIPPED": 0, "SIM_SENT": 0}
            for r in cur.fetchall():
                out[str(r["status"])] = int(r["n"])
            out["TOTAL"] = sum(out.values())
            return out

    def clear_all(self, tenant_id: str = "default") -> None:
        tenant_id = (tenant_id or "default").strip() or "default"
        with self._connect_queue() as con:
            con.execute("DELETE FROM deliveries WHERE tenant_id=?;", (tenant_id,))
            try:
                con.commit()
            except Exception:
                pass

    def retry_failed(self, reset_attempts: bool = False, tenant_id: str = "default") -> int:
        tenant_id = (tenant_id or "default").strip() or "default"
        now = utc_now_str()
        with self._connect_queue() as con:
            if reset_attempts:
                cur = con.execute(
                    """
                    UPDATE deliveries
                    SET status='PENDING', updated_at=?, attempts=0, last_error=NULL
                    WHERE tenant_id=? AND status='FAILED'
                    """,
                    (now, tenant_id),
                )
            else:
                cur = con.execute(
                    """
                    UPDATE deliveries
                    SET status='PENDING', updated_at=?, last_error=NULL
                    WHERE tenant_id=? AND status='FAILED'
                    """,
                    (now, tenant_id),
                )
            try:
                con.commit()
            except Exception:
                pass
            return cur.rowcount or 0

    # ---------------- Worker control ----------------
    def start_worker(
        self,
        motor,
        excel_bytes: bytes,
        build_email_fn,
        cfg: QueueConfig,
        started_by: Optional[str] = None,
        tenant_id: str = "default",
        tenant_cfg: Optional[dict] = None,
        app_db_path: Optional[Path] = None,
        biz_db_path: Optional[Path] = None,
    ) -> None:
        with self._lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return

            self._stop_flag = False
            self._success_count = 0
            self._started_by = started_by or "unknown"
            self._worker_tenant_id = (tenant_id or "default").strip() or "default"
            self._worker_tenant_cfg = tenant_cfg or {}

            self._app_db_path = Path(app_db_path) if app_db_path else None
            self._biz_db_path = Path(biz_db_path) if biz_db_path else None

            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                args=(motor, excel_bytes, build_email_fn, cfg, self._worker_tenant_id, self._worker_tenant_cfg),
                daemon=True,
            )
            self._worker_thread.start()

    def stop_worker(self) -> None:
        with self._lock:
            self._stop_flag = True

    def worker_running(self, tenant_id: Optional[str] = None) -> bool:
        """
        Si se pasa tenant_id, solo True si el worker activo es de ese tenant.
        """
        with self._lock:
            alive = bool(self._worker_thread and self._worker_thread.is_alive())
            if not alive:
                return False
            if tenant_id is None:
                return True
            return (self._worker_tenant_id or "default") == ((tenant_id or "default").strip() or "default")

    # ---------------- Worker loop ----------------
    def _worker_loop(self, motor, excel_bytes: bytes, build_email_fn, cfg: QueueConfig, tenant_id: str, tenant_cfg: dict) -> None:
        self._last_domain_sent_at.clear()
        self._domain_streak.clear()
        self._success_count = 0

        while True:
            with self._lock:
                if self._stop_flag:
                    break

            if cfg.stop_after_success is not None and self._success_count >= int(cfg.stop_after_success):
                with self._lock:
                    self._stop_flag = True
                break

            job = self._claim_next_pending(started_by=self._started_by or "unknown", tenant_id=tenant_id)
            if not job:
                time.sleep(0.4)
                continue

            job_id = int(job["id"])
            cif = (job["cifnif"] or "").strip()
            to_email = (job["to_email"] or "").strip()
            domain = ((job["domain"] if "domain" in job.keys() else "") or extract_domain(to_email)).strip().lower()
            attempts = int(job["attempts"] or 0)
            max_attempts = int(job["max_attempts"] or cfg.max_attempts)

            if not is_valid_email(to_email):
                self._update_attempt(job_id, attempts, "SKIPPED", "Email inválido en ejecución")
                continue

            self._apply_domain_throttle(domain, cfg)

            try:
                # ✅ 1:1: motor v7 requiere con_biz + con_auth
                with self._connect_biz() as con_biz, self._connect_app() as con_auth:
                    cert_pdf, carta_pdf, meta = motor.generate_individual_from_excel_bytes(
                        excel_bytes=excel_bytes,
                        cifnif=cif,
                        tenant_cfg=tenant_cfg,
                        con_biz=con_biz,
                        con_auth=con_auth,
                        tenant_id=tenant_id,
                    )

                email = build_email_fn(motor, meta, to_email)
                subject = email["subject"]
                body = email["body"]

                attachments = []
                if cfg.attach_cert and cert_pdf:
                    attachments.append((f"{meta['numerocertificado']}_CERT.pdf", cert_pdf, "application/pdf"))

                # ✅ NO adjuntar carta si viene vacía
                if cfg.attach_letter and carta_pdf:
                    attachments.append((f"{meta['numerocertificado']}_CARTA.pdf", carta_pdf, "application/pdf"))

                if not attachments:
                    raise RuntimeError("No hay adjuntos seleccionados o los PDFs están vacíos.")

                if cfg.test_mode:
                    self._mark_sent_like(job_id, meta, simulated=True)
                else:
                    if not smtp_config_ok():
                        raise RuntimeError("SMTP no configurado (SMTP_*).")

                    send_email_with_attachments(
                        to_email=to_email,
                        subject=subject,
                        body=body,
                        attachments=attachments,
                    )
                    self._mark_sent_like(job_id, meta, simulated=False)

                self._mark_domain_sent(domain)
                self._success_count += 1
                time.sleep(max(cfg.sleep_between_ms, 0) / 1000.0)

            except Exception as e:
                attempts += 1
                if attempts >= max_attempts:
                    self._update_attempt(job_id, attempts, "FAILED", str(e))
                else:
                    self._update_attempt(job_id, attempts, "PENDING", str(e))
                    backoff = (cfg.backoff_base_ms * (2 ** (attempts - 1))) / 1000.0
                    time.sleep(backoff)

    # ---------------- internals (queue db) ----------------
    def _claim_next_pending(self, started_by: str, tenant_id: str) -> Optional[sqlite3.Row]:
        tenant_id = (tenant_id or "default").strip() or "default"
        now = utc_now_str()
        with self._connect_queue() as con:
            con.execute("BEGIN IMMEDIATE;")
            row = con.execute(
                """
                SELECT * FROM deliveries
                WHERE tenant_id=? AND status='PENDING'
                ORDER BY id ASC
                LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()

            if not row:
                con.execute("COMMIT;")
                return None

            con.execute(
                """
                UPDATE deliveries
                SET status='SENDING', updated_at=?,
                    started_by=COALESCE(started_by, ?),
                    started_at=COALESCE(started_at, ?)
                WHERE id=?;
                """,
                (now, started_by, now, int(row["id"])),

            )
            con.execute("COMMIT;")
            return row

    def _update_attempt(self, job_id: int, attempts: int, status: str, err: str) -> None:
        now = utc_now_str()
        with self._connect_queue() as con:
            con.execute(
                """
                UPDATE deliveries
                SET attempts=?, status=?, last_error=?, updated_at=?
                WHERE id=?;
                """,
                (int(attempts), status, (err or "")[:600], now, int(job_id)),
            )
            try:
                con.commit()
            except Exception:
                pass

    def _mark_sent_like(self, job_id: int, meta: dict, simulated: bool) -> None:
        now = utc_now_str()
        status = "SIM_SENT" if simulated else "SENT"
        with self._connect_queue() as con:
            con.execute(
                """
                UPDATE deliveries
                SET status=?, updated_at=?,
                    numerocertificado=?, hash=?, tipo=?, fecha_emision=?,
                    last_error=NULL
                WHERE id=?;
                """,
                (
                    status,
                    now,
                    meta.get("numerocertificado"),
                    meta.get("hash"),
                    meta.get("tipo"),
                    meta.get("fecha_emision"),
                    int(job_id),
                ),
            )
            try:
                con.commit()
            except Exception:
                pass

    def _apply_domain_throttle(self, domain: str, cfg: QueueConfig) -> None:
        if not domain:
            return
        now = time.time()
        last = self._last_domain_sent_at.get(domain, 0.0)
        since = now - last
        streak = self._domain_streak.get(domain, 0)
        min_gap = max(cfg.domain_min_gap_seconds, 0.0)

        if streak >= cfg.domain_burst and since < min_gap:
            time.sleep(min_gap - since)

    def _mark_domain_sent(self, domain: str) -> None:
        if not domain:
            return
        now = time.time()
        self._last_domain_sent_at[domain] = now
        self._domain_streak[domain] = self._domain_streak.get(domain, 0) + 1
        # reset streaks de otros dominios
        for d in list(self._domain_streak.keys()):
            if d != domain:
                self._domain_streak[d] = 0