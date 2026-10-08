# backend/generator.py
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from backend.db import (
    ensure_business_schema_once,
    create_import,
    upsert_donor,
    ensure_anonymous_donor,
    require_not_anonymous_donor,
    insert_donation,
    upsert_certificate,
    link_certificate_to_donations,
)

# -------------------- Proyecto / rutas base --------------------
BASE_DIR = Path(__file__).resolve().parent.parent

# ✅ No-leak: NO forzar logo global
DEFAULT_LOGO_REL = ""

RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# -------------------- Helpers: CIF/NIF/NIE España --------------------
_NIF_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def _clean_tax_id(s: str) -> str:
    if s is None:
        return ""
    s = str(s).strip().upper()
    if s in {"NAN", "NONE", "NULL"}:
        return ""
    s = re.sub(r"[\s\-_/\.]", "", s)
    if s in {"NAN", "NONE", "NULL"}:
        return ""
    return s


def validate_spanish_tax_id(raw: str) -> bool:
    s = _clean_tax_id(raw)
    if not s:
        return False

    # DNI/NIF
    if re.fullmatch(r"\d{8}[A-Z]", s):
        num = int(s[:8])
        return s[-1] == _NIF_LETTERS[num % 23]

    # NIE
    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", s):
        prefix = {"X": "0", "Y": "1", "Z": "2"}[s[0]]
        num = int(prefix + s[1:8])
        return s[-1] == _NIF_LETTERS[num % 23]

    # CIF
    if re.fullmatch(r"[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]", s):
        letter = s[0]
        digits = s[1:8]
        control = s[8]

        sum_even = sum(int(digits[i]) for i in (1, 3, 5))
        sum_odd = 0
        for i in (0, 2, 4, 6):
            x = int(digits[i]) * 2
            sum_odd += (x // 10) + (x % 10)

        total = sum_even + sum_odd
        ctrl_num = (10 - (total % 10)) % 10
        ctrl_letter = "JABCDEFGHI"[ctrl_num]

        must_be_digit = letter in "ABEH"
        must_be_letter = letter in "KPQS"
        if must_be_digit:
            return control == str(ctrl_num)
        if must_be_letter:
            return control == ctrl_letter
        return control in (str(ctrl_num), ctrl_letter)

    return False


def _infer_donor_type_from_tax_id(tax_id: str) -> str:
    s = _clean_tax_id(tax_id)
    if not s:
        return "UNKNOWN"
    if re.fullmatch(r"[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]", s):
        return "COMPANY"
    if re.fullmatch(r"\d{8}[A-Z]", s) or re.fullmatch(r"[XYZ]\d{7}[A-Z]", s):
        return "INDIVIDUAL"
    return "UNKNOWN"


def _safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, float) and pd.isna(x):
        return ""
    try:
        if isinstance(x, pd.Timestamp) and pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    return "" if s.upper() in {"NAN", "NONE", "NULL"} else s


def _safe_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        if isinstance(x, float) and pd.isna(x):
            return None
        return int(x)
    except Exception:
        return None


def _deep_merge(base: dict, override: dict) -> dict:
    if not isinstance(base, dict):
        return override
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# -------------------- ✅ Placeholders en textos legales --------------------
_PLACEHOLDER_RE = re.compile(r"\[([^\]]+)\]")


def _render_placeholders(template: str, values: Dict[str, Any]) -> str:
    s = (template or "")
    if not s.strip():
        return ""

    def repl(m: re.Match) -> str:
        key = (m.group(1) or "").strip().upper()
        key = key.replace(" ", "_").replace("-", "_")
        v = values.get(key)
        if v is None:
            return m.group(0)
        vv = str(v).strip()
        return vv if vv else "NO INDICADO"

    return _PLACEHOLDER_RE.sub(repl, s)


# -------------------- Tipos internos para centralizar DRY --------------------
@dataclass
class PreparedRow:
    entidad: str
    cif: str
    cif_clean: str
    email_donante: str
    fecha_dt: Optional[datetime]
    fecha_str: str
    imp: float
    kg: float
    tipo_final: str
    tipo_warnings: List[str]
    cert_status: str
    carta_status: str
    motivos: List[str]


@dataclass
class Totals:
    total_importe: float
    total_kg: float
    fecha_ref: str


@dataclass
class ResultRow:
    numerocertificado: str
    entidad: str
    cifnif: str
    fecha: str
    tipodonacion: str
    importeeur: float
    cantidadkg: float
    hash: str

    estado_certificado: str
    estado_carta: str
    motivos: str = ""

    certificado: str = ""
    carta: str = ""
    email_donante: str = ""


class CertSystem:
    OK = "OK"
    REVIEW = "REVIEW"
    INVALID = "INVALID"
    CRASH = "CRASH"

    @staticmethod
    def _nl2br(s: str) -> str:
        return (s or "").replace("\r\n", "\n").replace("\n", "<br/>")

    def __init__(self, config_path: Path, estado_path: Optional[Path] = None):
        self.config_path = Path(config_path)
        self.estado_path = Path(estado_path) if estado_path else None

        self.base_config = self._load_config()

        self.BRAND = colors.HexColor(self.base_config.get("branding", {}).get("color_principal", "#3B468C"))
        self.GREY_TEXT = colors.HexColor("#444444")
        self.GREY_SOFT = colors.HexColor("#666666")
        self.GREY_BG = colors.HexColor("#F4F6FA")
        self.GRID = colors.HexColor("#DDDDDD")

        self.styles = getSampleStyleSheet()
        self._ensure_styles()

    # -------------------- Timezone --------------------
    def _tz_name(self, cfg: dict) -> str:
        app = cfg.get("app") if isinstance(cfg.get("app"), dict) else {}
        return (app.get("timezone") or "Europe/Madrid").strip() or "Europe/Madrid"

    def _tz(self, cfg: dict) -> ZoneInfo:
        tz_name = self._tz_name(cfg)
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return ZoneInfo("Europe/Madrid")

    def _now(self, cfg: dict) -> datetime:
        return datetime.now(self._tz(cfg))

    # -------------------- Config --------------------
    def _load_config(self) -> dict:
        if not self.config_path.exists():
            raise FileNotFoundError(f"No existe config.json en: {self.config_path}")

        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("config.json inválido (no es un objeto JSON).")

        cfg.setdefault("app", {})
        cfg.setdefault("features", {})
        cfg.setdefault("storage", {})
        cfg.setdefault("defaults", {})

        defaults = cfg.get("defaults") if isinstance(cfg.get("defaults"), dict) else {}

        def _d(name: str) -> dict:
            v = defaults.get(name, {})
            return v if isinstance(v, dict) else {}

        cfg["entidad"] = cfg.get("entidad") if isinstance(cfg.get("entidad"), dict) else _d("entidad")
        cfg["firmas"] = cfg.get("firmas") if isinstance(cfg.get("firmas"), dict) else _d("firmas")
        cfg["textoslegales"] = cfg.get("textoslegales") if isinstance(cfg.get("textoslegales"), dict) else _d("textoslegales")
        cfg["textos_email"] = cfg.get("textos_email") if isinstance(cfg.get("textos_email"), dict) else _d("textos_email")

        cfg.setdefault("numeracion", {})
        cfg.setdefault("branding", {})

        cfg["numeracion"].setdefault("prefijo", "CERT")
        cfg["numeracion"].setdefault("inicio", 1)

        cfg["branding"].setdefault("color_principal", "#3B468C")
        cfg["branding"].setdefault("logo_path", "")
        cfg["branding"].setdefault("logo_filename", "")

        cfg["entidad"].setdefault("nombre", "")
        cfg["entidad"].setdefault("cif", "")
        cfg["entidad"].setdefault("direccion", "")
        cfg["entidad"].setdefault("telefono", "")
        cfg["entidad"].setdefault("email", "")
        cfg["entidad"].setdefault("web", "")
        cfg["entidad"].setdefault("ciudad", "")

        cfg["firmas"].setdefault("nombre", "")
        cfg["firmas"].setdefault("cargo", "")

        cfg["textoslegales"].setdefault("certificadodinero", "")
        cfg["textoslegales"].setdefault("certificadoespecie", "")
        cfg["textoslegales"].setdefault("ley49_block", "")
        cfg["textoslegales"].setdefault("modelo182_block", "")

        cfg["textos_email"].setdefault("firma", "")
        cfg["textos_email"].setdefault("dinero", "")
        cfg["textos_email"].setdefault("especie", "")

        features = cfg.get("features") if isinstance(cfg.get("features"), dict) else {}
        features.setdefault("validate_spanish_tax_id", True)
        features.setdefault("dayfirst", True)
        features.setdefault("is_ley_49_2002", False)
        features.setdefault("include_modelo182_note", True)
        cfg["features"] = features

        storage = cfg.get("storage") if isinstance(cfg.get("storage"), dict) else {}
        storage.setdefault("tmp_out_dir", "backend/tmp_out")
        storage.setdefault("tmp_out_individual_dir", "backend/tmp_out_individual")
        storage.setdefault("branding_dir", "branding")
        storage.setdefault("zip_spool_max_mb", 64)
        storage.setdefault("zip_return_max_mb", 256)
        storage.setdefault("zip_output_dir", "backend/zip_output")
        cfg["storage"] = storage

        return cfg

    def _effective_config(self, tenant_cfg: Optional[dict]) -> dict:
        eff = _deep_merge(self.base_config, tenant_cfg or {})

        eff.setdefault("app", {})
        eff.setdefault("entidad", {})
        eff.setdefault("firmas", {})
        eff.setdefault("textoslegales", {})
        eff.setdefault("textoslegales_raw", {})
        eff.setdefault("textoslegales_rendered", {})
        eff.setdefault("textos_email", {})
        eff.setdefault("numeracion", {})
        eff.setdefault("branding", {})
        eff.setdefault("storage", self.base_config.get("storage", {}))
        eff.setdefault("features", self.base_config.get("features", {}))

        eff["numeracion"].setdefault("prefijo", "CERT")
        eff["numeracion"].setdefault("inicio", 1)

        try:
            self.BRAND = colors.HexColor(eff.get("branding", {}).get("color_principal", "#3B468C"))
        except Exception:
            self.BRAND = colors.HexColor("#3B468C")

        if "TitleBrand" in self.styles.byName:
            self.styles["TitleBrand"].textColor = self.BRAND

        return eff

    # -------------------- Styles --------------------
    def _ensure_styles(self) -> None:
        existing = set(self.styles.byName.keys())

        if "TitleBrand" not in existing:
            self.styles.add(
                ParagraphStyle(
                    name="TitleBrand",
                    parent=self.styles["Title"],
                    fontName="Helvetica-Bold",
                    fontSize=18,
                    textColor=self.BRAND,
                    spaceAfter=8,
                )
            )

        if "Meta" not in existing:
            self.styles.add(
                ParagraphStyle(
                    name="Meta",
                    parent=self.styles["Normal"],
                    fontName="Helvetica",
                    fontSize=9.5,
                    textColor=self.GREY_TEXT,
                    leading=12,
                    spaceAfter=10,
                )
            )

        if "Body" not in existing:
            self.styles.add(
                ParagraphStyle(
                    name="Body",
                    parent=self.styles["Normal"],
                    fontName="Helvetica",
                    fontSize=10.5,
                    leading=14,
                    textColor=colors.black,
                )
            )

        if "SmallGrey" not in existing:
            self.styles.add(
                ParagraphStyle(
                    name="SmallGrey",
                    parent=self.styles["Normal"],
                    fontName="Helvetica",
                    fontSize=8.5,
                    leading=11,
                    textColor=self.GREY_SOFT,
                )
            )

        if "Mono" not in existing:
            self.styles.add(
                ParagraphStyle(
                    name="Mono",
                    parent=self.styles["Normal"],
                    fontName="Courier",
                    fontSize=9,
                    leading=11,
                    textColor=self.GREY_SOFT,
                )
            )

    # -------------------- Normalización --------------------
    def _normalize_key(self, text: Any) -> str:
        if pd.isna(text):
            return ""
        t = unicodedata.normalize("NFD", str(text))
        t = t.encode("ascii", "ignore").decode("utf-8")
        t = t.lower().strip()
        t = t.replace("€", " eur ")
        t = t.replace("%", " pct ")
        t = t.replace("/", " ")
        t = t.replace("-", " ")
        t = re.sub(r"\s+", " ", t).strip()
        t = re.sub(r"[^a-z0-9 ]+", " ", t)
        t = re.sub(r"\s+", "", t)
        return t

    def _sanitize_filename(self, text: Any) -> str:
        if pd.isna(text) or str(text).strip() == "":
            return "desconocido"
        t = unicodedata.normalize("NFD", str(text)).encode("ascii", "ignore").decode("utf-8")
        clean = re.sub(r"[^A-Za-z0-9_-]+", "_", t).strip("_")[:55] or "desconocido"
        if clean.upper() in RESERVED:
            clean = f"ENTIDAD_{clean}"
        return clean

    def _to_float_es(self, v: Any) -> float:
        if v is None:
            return 0.0
        try:
            if isinstance(v, float) and pd.isna(v):
                return 0.0
        except Exception:
            pass

        if isinstance(v, (int, float)) and not (isinstance(v, float) and pd.isna(v)):
            try:
                return float(v)
            except Exception:
                return 0.0

        s = str(v).strip()
        if not s or s.upper() in {"NAN", "NONE", "NULL"}:
            return 0.0

        s = s.replace("\u00A0", " ")
        s = s.replace("€", "").replace("EUR", "").replace("eur", "")
        s = s.replace("kg", "").replace("KG", "").replace("Kg", "")
        s = s.replace(" ", "")

        neg = False
        if s.startswith("(") and s.endswith(")"):
            neg = True
            s = s[1:-1].strip()

        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        else:
            s = s.replace(",", ".")

        try:
            val = float(s)
            return -val if neg else val
        except Exception:
            return 0.0

    # -------------------- Fechas --------------------
    def _as_py_datetime(self, v: Any, *, dayfirst: bool = True) -> Optional[datetime]:
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except Exception:
            pass

        if isinstance(v, datetime):
            return v

        try:
            if isinstance(v, pd.Timestamp):
                if pd.isna(v):
                    return None
                return v.to_pydatetime()
        except Exception:
            pass

        s = str(v).strip()
        if not s or s.upper() in {"NAN", "NONE", "NULL"}:
            return None

        iso_like = bool(re.match(r"^\d{4}-\d{2}-\d{2}", s))
        try:
            dt = pd.to_datetime(s, errors="coerce", dayfirst=(False if iso_like else bool(dayfirst)))
        except Exception:
            return None

        try:
            if pd.isna(dt):
                return None
        except Exception:
            pass

        try:
            return dt.to_pydatetime()  # type: ignore[attr-defined]
        except Exception:
            return dt if isinstance(dt, datetime) else None

    def _parse_fecha(self, v: Any, *, dayfirst: bool = True) -> Optional[datetime]:
        return self._as_py_datetime(v, dayfirst=dayfirst)

    def _format_fecha(self, dt: Any) -> str:
        dt2 = self._as_py_datetime(dt, dayfirst=True)
        return dt2.strftime("%d/%m/%Y") if dt2 else ""

    def _fecha_iso(self, dt: Any) -> str:
        dt2 = self._as_py_datetime(dt, dayfirst=True)
        return dt2.strftime("%Y-%m-%d") if dt2 else ""

    def _norm_tipo(self, v: Any) -> str:
        t = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip().upper()
        t = unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode("utf-8")
        t = t.replace("-", " ").replace("_", " ")
        t = re.sub(r"\s+", " ", t).strip()

        dinero = {
            "DINERO", "DINERARIA", "DINERARIO", "MONETARIA", "EUROS", "EUR",
            "TRANSFERENCIA", "BIZUM", "APORTACION", "APORTACION ECONOMICA",
            "DONACION DINERO", "DONACION ECONOMICA", "ECONOMICA",
        }
        especie = {
            "ESPECIE", "EN ESPECIE", "ALIMENTOS", "ALIMENTO", "PRODUCTO", "PRODUCTOS",
            "MATERIAL", "MATERIALES", "KG", "KILOS", "PESO", "ENTREGA",
            "DONACION ESPECIE", "DONACION EN ESPECIE",
        }

        if t in dinero:
            return "DINERO"
        if t in especie:
            return "ESPECIE"
        return "ERROR"

    def _infer_tipo(self, tipo_norm: str, imp: float, kg: float) -> Tuple[str, List[str]]:
        warnings: List[str] = []
        if tipo_norm in {"DINERO", "ESPECIE"}:
            return tipo_norm, warnings

        if imp > 0 and kg <= 0:
            warnings.append("Tipo inferido por importe")
            return "DINERO", warnings
        if kg > 0 and imp <= 0:
            warnings.append("Tipo inferido por kg")
            return "ESPECIE", warnings
        if imp > 0 and kg > 0:
            warnings.append("Tipo mixto (importe y kg). Revisión manual")
            return "MIXTO", warnings

        warnings.append("Tipo indeterminado")
        return "ERROR", warnings

    def _emitir_num(self, cfg: dict, seq: int) -> str:
        pref = str(cfg.get("numeracion", {}).get("prefijo", "CERT")).strip() or "CERT"
        anio = self._now(cfg).year
        return f"{pref}-{anio}-{seq:06d}"

    def _hash(self, tenant_id: str, entidad: str, cif: str, num_cert: str, fecha: str, imp: float, kg: float) -> str:
        payload = f"{tenant_id}|{entidad}|{cif}|{num_cert}|{fecha}|{imp:.2f}|{kg:.2f}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10].upper()

    # -------------------- IDEMPOTENCIA (row_hash) --------------------
    @staticmethod
    def _money_to_cents(x: float) -> int:
        try:
            return int(round(float(x or 0.0) * 100))
        except Exception:
            return 0

    @staticmethod
    def _kg_to_grams(x: float) -> int:
        try:
            return int(round(float(x or 0.0) * 1000))
        except Exception:
            return 0

    def _make_row_hash(
        self,
        *,
        tenant_id: str,
        cif_clean: str,
        fecha_iso: str,
        tipo_final: str,
        imp: float,
        kg: float,
        entidad: str,
        email: str,
    ) -> str:
        t_id = (tenant_id or "default").strip() or "default"
        cif = _clean_tax_id(cif_clean or "").upper()
        f = (fecha_iso or "").strip()
        tp = (tipo_final or "").strip().upper()

        i_cents = self._money_to_cents(imp)
        k_grams = self._kg_to_grams(kg)

        payload = f"{t_id}|{cif}|{f}|{tp}|{i_cents}|{k_grams}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _try_fetchone_dict(self, cur: sqlite3.Cursor) -> Optional[Dict[str, Any]]:
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description] if cur.description else []
        try:
            return {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
        except Exception:
            return None

    def _fetchall_dicts(self, cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        out: List[Dict[str, Any]] = []
        for r in rows or []:
            try:
                out.append({cols[i]: r[i] for i in range(min(len(cols), len(r)))})
            except Exception:
                continue
        return out

    # -------------------- Lookups --------------------
    def _lookup_existing_by_row_hash(
        self,
        *,
        con: sqlite3.Connection,
        tenant_id: str,
        row_hash: str,
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "donation_id": None,
            "donor_id": None,
            "certificate_id": None,
            "cert_row": None,
            "donor_row": None,
            "donations": [],
        }
        if not row_hash:
            return out

        tenant_id = (tenant_id or "default").strip() or "default"

        try:
            cur = con.execute(
                """
                SELECT
                  d.id AS donation_id,
                  d.donor_id AS donor_id,
                  c.id AS certificate_id,
                  c.numerocertificado AS numerocertificado,
                  c.hash AS hash,
                  c.tipo AS tipo,
                  c.fecha_emision AS fecha_emision,
                  c.status_certificado AS status_certificado,
                  c.status_carta AS status_carta,
                  c.email_to AS email_to,
                  c.cert_pdf_path AS cert_pdf_path,
                  c.carta_pdf_path AS carta_pdf_path,
                  c.meta_snapshot_json AS meta_snapshot_json
                FROM donations d
                LEFT JOIN certificate_items ci ON ci.donation_id = d.id
                LEFT JOIN certificates c ON c.id = ci.certificate_id
                WHERE d.tenant_id = ? AND d.row_hash = ?
                ORDER BY c.id DESC
                LIMIT 1
                """,
                (tenant_id, row_hash),
            )
            base = self._try_fetchone_dict(cur)
            if not base:
                return out

            out["donation_id"] = base.get("donation_id")
            out["donor_id"] = base.get("donor_id")
            out["certificate_id"] = base.get("certificate_id")

            donor_id = base.get("donor_id")
            if donor_id:
                cur2 = con.execute(
                    """
                    SELECT id, tenant_id, nombre, cifnif_norm AS cifnif, email
                    FROM donors
                    WHERE tenant_id = ? AND id = ?
                    LIMIT 1
                    """,
                    (tenant_id, int(donor_id)),
                )
                out["donor_row"] = self._try_fetchone_dict(cur2)

            cert_id = base.get("certificate_id")
            if cert_id:
                out["cert_row"] = {
                    "id": cert_id,
                    "tenant_id": tenant_id,
                    "numerocertificado": base.get("numerocertificado"),
                    "hash": base.get("hash"),
                    "tipo": base.get("tipo"),
                    "fecha_emision": base.get("fecha_emision"),
                    "status_certificado": base.get("status_certificado"),
                    "status_carta": base.get("status_carta"),
                    "email_to": base.get("email_to"),
                    "cert_pdf_path": base.get("cert_pdf_path"),
                    "carta_pdf_path": base.get("carta_pdf_path"),
                    "meta_snapshot_json": base.get("meta_snapshot_json") or "",
                }

                cur3 = con.execute(
                    """
                    SELECT d.id, d.fecha, d.tipo, d.importe, d.kg
                    FROM certificate_items ci
                    JOIN donations d ON d.id = ci.donation_id
                    WHERE ci.certificate_id = ? AND d.tenant_id = ?
                    ORDER BY d.id ASC
                    """,
                    (int(cert_id), tenant_id),
                )
                out["donations"] = self._fetchall_dicts(cur3)
            else:
                did = base.get("donation_id")
                if did:
                    cur4 = con.execute(
                        """
                        SELECT id, fecha, tipo, importe, kg
                        FROM donations
                        WHERE id = ? AND tenant_id = ?
                        LIMIT 1
                        """,
                        (int(did), tenant_id),
                    )
                    one = self._try_fetchone_dict(cur4)
                    out["donations"] = [one] if one else []

            return out

        except Exception:
            return out

    def _lookup_existing_cert_for_donation(
        self,
        *,
        con: sqlite3.Connection,
        tenant_id: str,
        donation_id: Optional[int],
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {"cert_row": None, "donor_row": None, "donations": []}
        if not donation_id:
            return out

        tenant_id = (tenant_id or "default").strip() or "default"
        did = int(donation_id)

        try:
            cur = con.execute(
                """
                SELECT c.*
                FROM certificate_items ci
                JOIN certificates c ON c.id = ci.certificate_id
                JOIN donations d ON d.id = ci.donation_id
                WHERE c.tenant_id=? AND d.tenant_id=? AND ci.donation_id=?
                ORDER BY c.id DESC
                LIMIT 1;
                """,
                (tenant_id, tenant_id, did),
            )
            cert_row = self._try_fetchone_dict(cur)
            if not cert_row:
                return out

            out["cert_row"] = cert_row

            donor_id = cert_row.get("donor_id")
            if donor_id:
                cur2 = con.execute(
                    """
                    SELECT id, tenant_id, nombre, cifnif_norm AS cifnif, email
                    FROM donors
                    WHERE tenant_id=? AND id=? LIMIT 1;
                    """,
                    (tenant_id, int(donor_id)),
                )
                out["donor_row"] = self._try_fetchone_dict(cur2)

            cur3 = con.execute(
                """
                SELECT d.id, d.fecha, d.tipo, d.importe, d.kg
                FROM certificate_items ci
                JOIN donations d ON d.id = ci.donation_id
                WHERE ci.certificate_id = ? AND d.tenant_id = ?
                ORDER BY d.id ASC;
                """,
                (int(cert_row["id"]), tenant_id),
            )
            out["donations"] = self._fetchall_dicts(cur3)

            return out
        except Exception:
            return out

    # -------------------- Snapshot --------------------
    def _build_meta_snapshot_json(self, cfg: dict) -> str:
        """
        LEGACY (v1): guarda SOLO plantillas RAW bajo "textoslegales".
        Se mantiene por compatibilidad.
        """
        branding = cfg.get("branding", {}) or {}
        entidad = cfg.get("entidad", {}) or {}
        firmas = cfg.get("firmas", {}) or {}
        textos = cfg.get("textoslegales", {}) or {}

        snap = {
            "schema": "cert_snapshot_v1",
            "generated_at": self._now(cfg).isoformat(),
            "timezone": self._tz_name(cfg),
            "branding": {
                "color_principal": (branding.get("color_principal") or "").strip(),
                "logo_path": (branding.get("logo_path") or "").strip(),
                "logo_filename": (branding.get("logo_filename") or "").strip(),
            },
            "entidad": {
                "nombre": (entidad.get("nombre") or "").strip(),
                "cif": (entidad.get("cif") or "").strip(),
                "direccion": (entidad.get("direccion") or "").strip(),
                "telefono": (entidad.get("telefono") or "").strip(),
                "email": (entidad.get("email") or "").strip(),
                "web": (entidad.get("web") or "").strip(),
                "ciudad": (entidad.get("ciudad") or "").strip(),
            },
            "firmas": {
                "nombre": (firmas.get("nombre") or "").strip(),
                "cargo": (firmas.get("cargo") or "").strip(),
            },
            "textoslegales": {
                "certificadodinero": (textos.get("certificadodinero") or "").strip(),
                "certificadoespecie": (textos.get("certificadoespecie") or "").strip(),
                "ley49_block": (textos.get("ley49_block") or "").strip(),
                "modelo182_block": (textos.get("modelo182_block") or "").strip(),
            },
        }
        return json.dumps(snap, ensure_ascii=False)

    def _build_meta_snapshot_json_for_cert(
        self,
        *,
        cfg_eff: dict,
        tipo_pdf: str,
        numerocertificado: str,
        hash_seguridad: str,
        textolegal_final: str,
    ) -> str:
        """
        v2:
        - RAW: plantillas tal cual estaban
        - RENDERED: texto final + coherencia con hash/num
        - FEATURES: flags relevantes para fallback RAW
        """
        branding = cfg_eff.get("branding", {}) or {}
        entidad = cfg_eff.get("entidad", {}) or {}
        firmas = cfg_eff.get("firmas", {}) or {}
        textos = cfg_eff.get("textoslegales", {}) or {}
        features = cfg_eff.get("features", {}) if isinstance(cfg_eff.get("features"), dict) else {}

        raw = {
            "certificadodinero": (textos.get("certificadodinero") or "").strip(),
            "certificadoespecie": (textos.get("certificadoespecie") or "").strip(),
            "ley49_block": (textos.get("ley49_block") or "").strip(),
            "modelo182_block": (textos.get("modelo182_block") or "").strip(),
        }

        rendered = {
            "tipo": (tipo_pdf or "").strip().upper(),
            "full": (textolegal_final or "").strip(),
            "hash": (hash_seguridad or "").strip(),
            "numerocertificado": (numerocertificado or "").strip(),
        }

        snap = {
            "schema": "cert_snapshot_v2",
            "generated_at": self._now(cfg_eff).isoformat(),
            "timezone": self._tz_name(cfg_eff),
            "branding": {
                "color_principal": (branding.get("color_principal") or "").strip(),
                "logo_path": (branding.get("logo_path") or "").strip(),
                "logo_filename": (branding.get("logo_filename") or "").strip(),
            },
            "entidad": {
                "nombre": (entidad.get("nombre") or "").strip(),
                "cif": (entidad.get("cif") or "").strip(),
                "direccion": (entidad.get("direccion") or "").strip(),
                "telefono": (entidad.get("telefono") or "").strip(),
                "email": (entidad.get("email") or "").strip(),
                "web": (entidad.get("web") or "").strip(),
                "ciudad": (entidad.get("ciudad") or "").strip(),
            },
            "firmas": {
                "nombre": (firmas.get("nombre") or "").strip(),
                "cargo": (firmas.get("cargo") or "").strip(),
            },
            "features": {
                "is_ley_49_2002": bool(features.get("is_ley_49_2002", False)),
                "include_modelo182_note": bool(features.get("include_modelo182_note", True)),
            },
            "textoslegales_raw": raw,
            "textoslegales_rendered": rendered,
        }
        return json.dumps(snap, ensure_ascii=False)

    def _tenant_cfg_from_snapshot(self, snapshot: dict) -> dict:
        if not isinstance(snapshot, dict):
            return {}

        out: Dict[str, Any] = {}

        for key in ("branding", "entidad", "firmas", "textoslegales", "features"):
            if key in snapshot and isinstance(snapshot.get(key), dict):
                out[key] = snapshot[key]

        if "textoslegales_raw" in snapshot and isinstance(snapshot.get("textoslegales_raw"), dict):
            out["textoslegales_raw"] = snapshot["textoslegales_raw"]

        if "textoslegales_rendered" in snapshot and isinstance(snapshot.get("textoslegales_rendered"), dict):
            out["textoslegales_rendered"] = snapshot["textoslegales_rendered"]

        tz = snapshot.get("timezone")
        if isinstance(tz, str) and tz.strip():
            out["app"] = {"timezone": tz.strip()}

        return out

    def _apply_snapshot_with_logo_fallback(
        self,
        *,
        tenant_cfg_current: dict,
        snap_cfg: dict,
    ) -> Tuple[dict, List[str]]:
        warnings: List[str] = []
        merged = _deep_merge(dict(tenant_cfg_current or {}), dict(snap_cfg or {}))
        cfg_eff = self._effective_config(merged)

        if self._logo_path(cfg_eff) is None:
            branding_snap = (snap_cfg or {}).get("branding", {}) or {}
            has_snap_branding = isinstance(branding_snap, dict) and (
                (branding_snap.get("logo_path") or "").strip()
                or (branding_snap.get("logo_filename") or "").strip()
            )
            if has_snap_branding:
                current_branding = (tenant_cfg_current or {}).get("branding", {}) if isinstance(tenant_cfg_current, dict) else {}
                if isinstance(current_branding, dict) and current_branding:
                    merged2 = dict(merged)
                    merged2["branding"] = dict(current_branding)
                    cfg_eff2 = self._effective_config(merged2)
                    if self._logo_path(cfg_eff2) is not None:
                        warnings.append("SNAPSHOT_LOGO_MISSING: usando logo actual como fallback")
                        return merged2, warnings
                warnings.append("SNAPSHOT_LOGO_MISSING: sin fallback disponible (se emitirá sin logo)")

        return merged, warnings

    # -------------------- Flags --------------------
    def _should_validate_spanish_tax_id(self, cfg: dict) -> bool:
        features = cfg.get("features") if isinstance(cfg.get("features"), dict) else {}
        return bool(features.get("validate_spanish_tax_id", True))

    def _dayfirst(self, cfg: dict) -> bool:
        features = cfg.get("features") if isinstance(cfg.get("features"), dict) else {}
        return bool(features.get("dayfirst", True))

    # -------------------- Existencia IDs --------------------
    def _exists_id(self, con: sqlite3.Connection, table: str, id_: int, tenant_id: str) -> bool:
        tenant_id = (tenant_id or "default").strip() or "default"
        try:
            cur = con.execute(
                f"SELECT 1 FROM {table} WHERE id=? AND tenant_id=? LIMIT 1",
                (int(id_), tenant_id),
            )
            return cur.fetchone() is not None
        except Exception:
            return False

    # -------------------- Tx helpers --------------------
    def _tx_begin_immediate_if_needed(self, con: sqlite3.Connection) -> bool:
        if con.in_transaction:
            return False
        con.execute("BEGIN IMMEDIATE;")
        return True

    def _tx_commit_if_started(self, con: sqlite3.Connection, started: bool) -> None:
        if started and con.in_transaction:
            con.execute("COMMIT;")

    def _tx_rollback_if_started(self, con: sqlite3.Connection, started: bool) -> None:
        if started and con.in_transaction:
            con.execute("ROLLBACK;")

    # -------------------- Numeración --------------------
    def _reserve_next_seq_atomic(
        self,
        *,
        con_auth: sqlite3.Connection,
        tenant_id: str,
        cfg: dict,
    ) -> int:
        from backend import tenant_counter as tc

        ensure_fn = getattr(tc, "ensure_counter_schema", None)
        if callable(ensure_fn):
            ensure_fn(con_auth)

        get_next = getattr(tc, "get_next_counter", None)
        if not callable(get_next):
            raise RuntimeError("backend.tenant_counter.get_next_counter no existe o no es callable")

        tenant_id = (tenant_id or "default").strip() or "default"
        year = self._now(cfg).year
        start_at = int(cfg.get("numeracion", {}).get("inicio", 1) or 1)

        started = self._tx_begin_immediate_if_needed(con_auth)
        try:
            seq = get_next(
                con_auth,
                tenant_id=tenant_id,
                year=year,
                start_at=start_at,
                tz_name=self._tz_name(cfg),
            )
            self._tx_commit_if_started(con_auth, started)
            return int(seq)
        except Exception:
            self._tx_rollback_if_started(con_auth, started)
            raise

    # -------------------- Statuses --------------------
    def _decide_statuses(
        self,
        *,
        cfg: dict,
        entidad: str,
        cif: str,
        fecha_dt: Optional[datetime],
        tipo_final: str,
        imp: float,
        kg: float,
        email_donante: str,
        tipo_warnings: List[str],
    ) -> Tuple[str, str, List[str]]:
        motivos: List[str] = []
        motivos.extend(tipo_warnings)

        cert_status = self.OK
        if not entidad:
            cert_status = self.INVALID
            motivos.append("Entidad vacía (certificado)")

        cif_clean = _clean_tax_id(cif)
        if self._should_validate_spanish_tax_id(cfg):
            if not cif_clean or not validate_spanish_tax_id(cif_clean):
                cert_status = self.INVALID
                motivos.append("CIF/NIF/NIE inválido (certificado)")
        else:
            if not cif_clean:
                cert_status = self.INVALID
                motivos.append("Identificador fiscal vacío (certificado)")

        if not fecha_dt:
            cert_status = self.INVALID
            motivos.append("Fecha inválida (certificado)")
        else:
            try:
                hoy = self._now(cfg).date()
                if fecha_dt.date() > hoy:
                    cert_status = self.INVALID
                    motivos.append("Fecha de donación en el futuro (certificado)")
            except Exception:
                pass

        if tipo_final == "MIXTO":
            cert_status = self.REVIEW
            motivos.append("Tipo mixto (importe y kg) (certificado)")
        elif tipo_final == "ERROR":
            cert_status = self.INVALID
            motivos.append("Tipo indeterminado (certificado)")

        if tipo_final == "DINERO" and imp <= 0:
            cert_status = self.INVALID
            motivos.append("Importe inválido (certificado)")
        if tipo_final == "ESPECIE" and kg <= 0:
            cert_status = self.INVALID
            motivos.append("KG inválidos (certificado)")

        if cert_status in {self.OK, self.REVIEW} and not email_donante:
            motivos.append("Sin email (no se puede envío automático)")

        carta_status = cert_status
        if carta_status not in {self.OK, self.REVIEW}:
            motivos.append("Certificado inválido => carta bloqueada")

        return cert_status, carta_status, motivos

    # -------------------- Preparación de fila --------------------
    def _prepare_row_from_frame_row(self, fila: pd.Series, cfg: dict, *, df_columns: set) -> PreparedRow:
        entidad = _safe_str(fila.get("entidaddonante"))
        cif = _safe_str(fila.get("cifnif"))
        cif_clean = _clean_tax_id(cif)

        fecha_dt = self._as_py_datetime(fila.get("fecha"), dayfirst=self._dayfirst(cfg))
        fecha_str = self._format_fecha(fecha_dt)

        imp = self._to_float_es(fila.get("importeeur", 0))
        kg = self._to_float_es(fila.get("cantidadkg", 0))
        email_donante = _safe_str(fila.get("contactoemail")) if "contactoemail" in df_columns else ""

        tipo_norm = self._norm_tipo(fila.get("tipodonacion"))
        tipo_final, tipo_warnings = self._infer_tipo(tipo_norm, imp, kg)

        cert_status, carta_status, motivos = self._decide_statuses(
            cfg=cfg,
            entidad=entidad,
            cif=cif,
            fecha_dt=fecha_dt,
            tipo_final=tipo_final,
            imp=imp,
            kg=kg,
            email_donante=email_donante,
            tipo_warnings=tipo_warnings,
        )

        return PreparedRow(
            entidad=entidad,
            cif=cif,
            cif_clean=cif_clean,
            email_donante=email_donante,
            fecha_dt=fecha_dt,
            fecha_str=fecha_str,
            imp=imp,
            kg=kg,
            tipo_final=tipo_final,
            tipo_warnings=tipo_warnings,
            cert_status=cert_status,
            carta_status=carta_status,
            motivos=motivos,
        )

    # -------------------- Totales --------------------
    def _compute_totals_from_donations(self, donations: List[Dict[str, Any]]) -> Totals:
        total_importe = 0.0
        total_kg = 0.0
        fecha_ref = ""
        for d in (donations or []):
            f = _safe_str(d.get("fecha"))
            if f and not fecha_ref:
                fecha_ref = f
            try:
                if d.get("importe") is not None:
                    total_importe += float(d.get("importe") or 0.0)
            except Exception:
                pass
            try:
                if d.get("kg") is not None:
                    total_kg += float(d.get("kg") or 0.0)
            except Exception:
                pass
        return Totals(total_importe=total_importe, total_kg=total_kg, fecha_ref=fecha_ref)

    # -------------------- Tipo PDF --------------------
    def _cert_tipo_for_pdf(self, tipo_final: str) -> str:
        if tipo_final == "DINERO":
            return "DINERO"
        if tipo_final == "ESPECIE":
            return "ESPECIE"
        if tipo_final == "MIXTO":
            return "DINERO"
        return "DINERO"

    # -------------------- Render context --------------------
    def _build_render_context(
        self,
        *,
        cfg_eff: dict,
        numerocertificado: str,
        hash_seguridad: str,
        donor_nombre: str,
        donor_cif: str,
        fecha_donacion: str,
        total_importe: float,
        total_kg: float,
        fecha_emision: str,
        tipo_pdf: str,
        email_to: str,
    ) -> Dict[str, Any]:
        entidad_cfg = (cfg_eff.get("entidad", {}) or {})
        firmas_cfg = (cfg_eff.get("firmas", {}) or {})

        textos_src = cfg_eff.get("textoslegales_raw")
        if not (isinstance(textos_src, dict) and textos_src):   # ✅ exige dict y NO vacío
            textos_src = (cfg_eff.get("textoslegales", {}) or {})
        textos_cfg = textos_src if isinstance(textos_src, dict) else {}

        features = cfg_eff.get("features") if isinstance(cfg_eff.get("features"), dict) else {}
        is_ley49 = bool(features.get("is_ley_49_2002", False))
        include_m182 = bool(features.get("include_modelo182_note", True))

        entidad_nombre = (entidad_cfg.get("nombre") or "").strip() or "NUESTRA ASOCIACIÓN"

        donor_cif_clean = _clean_tax_id(donor_cif) or ""
        donor_nombre_clean = (donor_nombre or "").strip()

        donor_display_table = donor_nombre_clean or (donor_cif_clean or "—")
        donor_display_legal = donor_nombre_clean or (donor_cif_clean or "Donante no informado")

        tp = (tipo_pdf or "").strip().upper()
        if tp not in ("DINERO", "ESPECIE"):
            tp = "DINERO"

        # ✅ coherencia freeze header/body si existe rendered
        rendered = cfg_eff.get("textoslegales_rendered")
        hash_final = str(hash_seguridad or "")
        num_final = str(numerocertificado or "")
        textolegal = ""

        if isinstance(rendered, dict):
            r_tipo = (rendered.get("tipo") or "").strip().upper()
            r_full = (rendered.get("full") or "").strip()
            r_hash = (rendered.get("hash") or "").strip()
            r_num = (rendered.get("numerocertificado") or "").strip()

            if r_tipo == tp and r_full:
                textolegal = r_full
                if r_hash:
                    hash_final = r_hash
                if r_num:
                    num_final = r_num

        ph = {
            "NOMBRE_ENTIDAD": (entidad_cfg.get("nombre") or "").strip(),
            "CIF_ENTIDAD": (entidad_cfg.get("cif") or "").strip(),
            "DIRECCION_ENTIDAD": (entidad_cfg.get("direccion") or "").strip(),
            "EMAIL_ENTIDAD": (entidad_cfg.get("email") or "").strip(),
            "WEB_ENTIDAD": (entidad_cfg.get("web") or "").strip(),
            "TELEFONO_ENTIDAD": (entidad_cfg.get("telefono") or "").strip(),
            "CIUDAD_ENTIDAD": (entidad_cfg.get("ciudad") or "").strip(),
            "NUM_CERT": num_final,
            "HASH": hash_final,
            "FECHA_DONACION": str(fecha_donacion or ""),
            "FECHA_EMISION": str(fecha_emision or ""),
            "NOMBRE_DONANTE": donor_display_legal,
            "CIF_DONANTE": donor_cif_clean,
            "EMAIL_DONANTE": (email_to or "").strip(),
            "IMPORTE": f"{float(total_importe or 0.0):.2f}",
            "KG": f"{float(total_kg or 0.0):.2f}",
        }

        if not textolegal:
            key_tpl = "certificadodinero" if tp == "DINERO" else "certificadoespecie"
            textolegal_tpl = (textos_cfg.get(key_tpl) or "").strip()
            if not textolegal_tpl:
                textolegal_tpl = "Texto legal pendiente de configurar en Ajustes."

            if not is_ley49:
                textolegal_tpl = re.sub(
                    r"(?i)\bley\s*49\s*[/\-]?\s*2002\b|\b49\s*[/\-]\s*2002\b",
                    "la normativa fiscal aplicable",
                    str(textolegal_tpl),
                )
                textolegal_tpl = re.sub(
                    r"(?i)\breal\s*decreto\s*1270\s*[/\-]?\s*2003\b|\brd\s*1270\s*[/\-]?\s*2003\b|\br\.d\.\s*1270\s*[/\-]?\s*2003\b",
                    "la normativa fiscal aplicable",
                    str(textolegal_tpl),
                )

            textolegal = _render_placeholders(str(textolegal_tpl), ph)

            if is_ley49:
                ley49_tpl = (textos_cfg.get("ley49_block") or "").strip()
                if not ley49_tpl:
                    ley49_tpl = (
                        "<b>Régimen fiscal aplicable:</b> "
                        "La entidad receptora tiene la condición de entidad beneficiaria del mecenazgo conforme a la "
                        "Ley 49/2002, de 23 de diciembre, y su normativa de desarrollo (Real Decreto 1270/2003)."
                    )
                ley49_block = _render_placeholders(ley49_tpl, ph)

                modelo182_block = ""
                if include_m182:
                    m182_tpl = (textos_cfg.get("modelo182_block") or "").strip()
                    if not m182_tpl:
                        m182_tpl = (
                            "A efectos informativos, la entidad incluirá esta donación en la declaración informativa anual "
                            "de donativos (Modelo 182), conforme a la normativa aplicable."
                        )
                    modelo182_block = _render_placeholders(m182_tpl, ph)

                head_bits = [b for b in [ley49_block, modelo182_block] if (b or "").strip()]
                if head_bits:
                    textolegal = "<br/>".join(head_bits) + "<br/><br/>" + textolegal

        return {
            "numerocertificado": num_final,
            "hash_seguridad": hash_final,
            "entidaddonante": donor_display_table,
            "cifnif": donor_cif_clean or "(no informado)",
            "fecha": (fecha_donacion or "").strip() or "(sin fecha)",
            "importeeur": f"{float(total_importe or 0.0):.2f}",
            "cantidadkg": f"{float(total_kg or 0.0):.2f}",
            "fechaemision": str(fecha_emision or ""),
            "textolegal": textolegal,
            "firm_nombre": (firmas_cfg.get("nombre") or "").strip(),
            "firm_cargo": (firmas_cfg.get("cargo") or "").strip(),
            "entidad_nombre": entidad_nombre,
            "entidad_cif": (entidad_cfg.get("cif") or "").strip(),
            "entidad_direccion": (entidad_cfg.get("direccion") or "").strip(),
            "entidad_telefono": (entidad_cfg.get("telefono") or "").strip(),
            "entidad_email": (entidad_cfg.get("email") or "").strip(),
            "entidad_web": (entidad_cfg.get("web") or "").strip(),
            "entidad_ciudad": (entidad_cfg.get("ciudad") or "").strip(),
            "email_donante": (email_to or "").strip(),
        }

    # -------------------- Mapeo columnas --------------------
    def map_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        original_cols = list(df.columns)
        df.columns = [self._normalize_key(c) for c in df.columns]

        alias_map = {
            "entidaddonante": [
                "entidaddonante", "donante", "empresa", "entidad", "razonsocial", "razon social",
                "razonsoc", "nombre", "denominacion", "organizacion", "sociedad", "cliente",
                "nombre donante", "donante nombre", "proveedor",
            ],
            "cifnif": [
                "cifnif", "cif", "nif", "nie", "dni", "taxid", "identificacion", "identificación",
                "documento", "doc", "nifcif", "cifnifdni", "idfiscal", "id fiscal",
                "cif/nif", "cif nif", "cif - nif",
            ],
            "fecha": [
                "fecha", "fechadonacion", "fecha donacion", "día donación", "dia donacion",
                "date", "diadonacion", "fechaentrega", "fecha entrega", "fecharecepcion", "fecha recepcion",
                "fecharegistro", "fecha registro", "fechadepago", "fecha de pago", "fechafactura", "fecha factura",
            ],
            "tipodonacion": [
                "tipodonacion", "tipo donacion", "tipo de donacion", "tipo", "clase", "tipodedonacion",
                "modalidad", "concepto", "naturaleza", "donacion tipo", "donación tipo",
            ],
            "importeeur": [
                "importeeur", "importe", "importe eur", "importe€", "importe(€)", "importe euros", "importe en euros",
                "euros", "eur", "donacion", "donacioneuros", "cantidad€", "total", "total€", "total euros",
                "aportacion", "aportación", "aportacioneconomica", "aportación económica", "importetotal",
                "importe donado", "importe donación", "importe donacion",
            ],
            "cantidadkg": [
                "cantidadkg", "kg", "kilos", "peso", "cantidad_kg", "cantidad kilos", "cantidadkilos",
                "cantkg", "cant.kg", "cantidad(kg)", "peso(kg)", "peso_kg",
                "cantidadkgs", "kilogramos", "kg entregados", "kgs entregados", "kilos entregados",
                "kg donados", "kilos donados",
            ],
            "contactoemail": [
                "contactoemail", "email", "correo", "correoelectronico", "correo electronico",
                "mail", "e-mail", "emailcontacto", "email_contacto", "email donante", "correo donante",
            ],
        }

        cols = set(df.columns)
        renames: Dict[str, str] = {}
        used = set()

        for target, aliases in alias_map.items():
            for a in aliases:
                a_norm = self._normalize_key(a)
                if a_norm in cols and target not in used:
                    renames[a_norm] = target
                    used.add(target)
                    break

        df.rename(columns=renames, inplace=True)

        df.attrs["__original_cols__"] = original_cols
        df.attrs["__normalized_cols__"] = list(df.columns)
        df.attrs["__renames__"] = dict(renames)
        return df

    def _ensure_required_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        required_defaults: Dict[str, Any] = {
            "entidaddonante": "",
            "cifnif": "",
            "fecha": None,
            "tipodonacion": "",
            "importeeur": 0,
            "cantidadkg": 0,
            "contactoemail": "",
        }
        for k, v in required_defaults.items():
            if k not in df.columns:
                df[k] = v
        return df

    def _mapping_diagnostics(self, df: pd.DataFrame) -> List[str]:
        msgs: List[str] = []
        ren = df.attrs.get("__renames__", {}) if isinstance(df.attrs.get("__renames__", {}), dict) else {}
        msgs.append(f"MAP: renames={ren}")

        orig = df.attrs.get("__original_cols__", [])
        norm = df.attrs.get("__normalized_cols__", [])
        if orig:
            msgs.append("MAP: original_cols=" + " | ".join([str(x) for x in orig]))
        if norm:
            msgs.append("MAP: normalized_cols=" + " | ".join([str(x) for x in norm]))

        for needed in ("entidaddonante", "cifnif", "fecha", "tipodonacion", "importeeur", "cantidadkg"):
            if needed not in df.columns:
                msgs.append(f"MAP_MISSING: {needed}")
        return msgs

    # -------------------- PDFs --------------------
    def _logo_path(self, cfg: dict) -> Optional[Path]:
        branding = (cfg.get("branding", {}) or {})
        lp = str(branding.get("logo_path") or "").strip().replace("\\", "/")

        if lp:
            p = Path(lp)
            if p.is_absolute():
                p = None
            else:
                p = (BASE_DIR / p).resolve()
                try:
                    p.relative_to(BASE_DIR.resolve())
                except Exception:
                    p = None

            if p and p.exists() and p.is_file():
                return p

        if DEFAULT_LOGO_REL:
            p_def = (BASE_DIR / DEFAULT_LOGO_REL).resolve()
            if p_def.exists() and p_def.is_file():
                return p_def

        logo_filename = str(branding.get("logo_filename") or "").strip()
        if logo_filename:
            p2 = (Path(__file__).resolve().parent / logo_filename).resolve()
            if p2.exists() and p2.is_file():
                return p2

        return None

    def _build_header_block(self, story: list, ctx: Dict[str, Any], titulo: str, cfg: dict) -> None:
        lp = self._logo_path(cfg)
        if lp:
            try:
                logo = Image(str(lp), width=3.0 * cm, height=3.0 * cm)
                logo.hAlign = "LEFT"
                story.append(logo)
            except Exception:
                pass

        story.append(Spacer(1, 6))

        bar = Table([[""]], colWidths=[16.0 * cm], rowHeights=[0.28 * cm])
        bar.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.BRAND),
            ("BOX", (0, 0), (-1, -1), 0, self.BRAND),
        ]))
        story.append(bar)
        story.append(Spacer(1, 10))

        story.append(Paragraph(titulo, self.styles["TitleBrand"]))
        story.append(Paragraph(
            f"<b>Nº certificado:</b> {ctx['numerocertificado']} &nbsp;&nbsp;&nbsp; "
            f"<b>Código de verificación:</b> <font face='Courier'>{ctx['hash_seguridad']}</font>",
            self.styles["Meta"],
        ))

    def _make_doc(self, buffer: io.BytesIO, *, left: float, right: float, top: float, bottom: float) -> SimpleDocTemplate:
        return SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=left,
            rightMargin=right,
            topMargin=top,
            bottomMargin=bottom,
        )

    def _build_cert_pdf_bytes(self, ctx: Dict[str, Any], tipo: str, cfg: dict) -> bytes:
        buf = io.BytesIO()
        doc = self._make_doc(
            buf,
            left=2.3 * cm, right=2.3 * cm,
            top=1.8 * cm, bottom=2.0 * cm,
        )
        story: list = []

        titulo = "CERTIFICADO DE DONACIÓN" if tipo == "DINERO" else "JUSTIFICANTE DE ENTREGA"
        self._build_header_block(story, ctx, titulo, cfg)

        parts = [ctx.get("entidad_telefono", "").strip(), ctx.get("entidad_email", "").strip()]
        if (ctx.get("entidad_web") or "").strip():
            parts.append(ctx["entidad_web"].strip())
        contact_line = " · ".join([p for p in parts if p]).strip() or " "

        entidad_txt = (
            f"<b>{ctx['entidad_nombre']}</b>"
            + (f" (CIF <b>{ctx['entidad_cif']}</b>)" if ctx.get("entidad_cif") else "")
            + "<br/>"
            + f"{ctx['entidad_direccion']}<br/>"
            + contact_line
        )

        page_w, _ = A4
        avail_w = page_w - (2.3 * cm) - (2.3 * cm)
        col_left = max(4.0 * cm, avail_w * 0.30)
        col_right = avail_w - col_left

        box = Table(
            [[Paragraph("Entidad receptora", self.styles["Body"]), Paragraph(entidad_txt, self.styles["Body"])]],
            colWidths=[col_left, col_right],
        )
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), self.GREY_BG),
            ("TEXTCOLOR", (0, 0), (0, 0), self.BRAND),
            ("BOX", (0, 0), (-1, -1), 0.6, self.GRID),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("PADDING", (0, 0), (-1, -1), 8),
            ("WORDWRAP", (0, 0), (-1, -1), "CJK"),
        ]))
        story.append(box)
        story.append(Spacer(1, 14))

        rows = [
            ["Donante", ctx["entidaddonante"]],
            ["NIF/CIF", ctx["cifnif"]],
            ["Fecha", ctx["fecha"]],
        ]
        brand_hex = str(cfg.get("branding", {}).get("color_principal", "#3B468C"))
        if tipo == "DINERO":
            rows.append(["Importe", f"<b><font color='{brand_hex}'>{ctx['importeeur']} €</font></b>"])
        else:
            rows.append(["Cantidad", f"<b><font color='{brand_hex}'>{ctx['cantidadkg']} kg</font></b>"])

        t = Table(
            [[Paragraph(a, self.styles["Body"]), Paragraph(b, self.styles["Body"])] for a, b in rows],
            colWidths=[col_left, col_right],
        )
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.35, self.GRID),
            ("BACKGROUND", (0, 0), (0, -1), self.GREY_BG),
            ("TEXTCOLOR", (0, 0), (0, -1), self.BRAND),
            ("PADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("WORDWRAP", (0, 0), (-1, -1), "CJK"),
        ]))
        story.append(t)
        story.append(Spacer(1, 14))

        story.append(Paragraph("<b>Texto legal</b>", self.styles["Body"]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(self._nl2br(ctx.get("textolegal", "")), self.styles["SmallGrey"]))
        story.append(Spacer(1, 18))

        ciudad = (ctx.get("entidad_ciudad") or "").strip()
        firm_nombre = (ctx.get("firm_nombre") or "").strip() or ctx.get("entidad_nombre", "")
        firm_cargo = (ctx.get("firm_cargo") or "").strip()

        if firm_cargo:
            linea_cargo = f"{firm_cargo} — {ctx.get('entidad_nombre', '')}".strip(" —")
        else:
            linea_cargo = f"{ctx.get('entidad_nombre', '')}".strip()

        emit_line = f"Emitido en {ciudad}, <b>{ctx['fechaemision']}</b>" if ciudad else f"Emitido en <b>{ctx['fechaemision']}</b>"

        story.append(Paragraph(
            f"{emit_line}<br/><br/>"
            f"<b>{firm_nombre}</b><br/>"
            f"{linea_cargo}",
            self.styles["Body"],
        ))

        story.append(Spacer(1, 16))
        pie = f"{ctx['entidad_direccion']} · {ctx['entidad_telefono']} · {ctx['entidad_email']}"
        if ctx.get("entidad_web"):
            pie += f" · {ctx['entidad_web']}"
        story.append(Paragraph(
            f"<para align='center'><font color='#666666'>"
            f"{pie} | Código verificación: <font face='Courier'>{ctx['hash_seguridad']}</font>"
            f"</font></para>",
            self.styles["SmallGrey"],
        ))

        doc.build(story)
        return buf.getvalue()

    def _build_letter_pdf_bytes(self, ctx: Dict[str, Any], tipo: str, cfg: dict) -> bytes:
        buf = io.BytesIO()
        doc = self._make_doc(
            buf,
            left=2.8 * cm, right=2.8 * cm,
            top=2.3 * cm, bottom=2.5 * cm,
        )
        story: list = []

        lp = self._logo_path(cfg)
        if lp:
            try:
                logo = Image(str(lp), width=2.1 * cm, height=2.1 * cm)
                logo.hAlign = "LEFT"
                story.append(logo)
                story.append(Spacer(1, 8))
            except Exception:
                pass

        ciudad = (ctx.get("entidad_ciudad") or "").strip()
        if ciudad:
            story.append(Paragraph(f"<para align='right'>{ciudad}, {ctx['fechaemision']}</para>", self.styles["Body"]))
        else:
            story.append(Paragraph(f"<para align='right'>{ctx['fechaemision']}</para>", self.styles["Body"]))

        story.append(Spacer(1, 16))
        don = (ctx.get("entidaddonante") or "").strip()
        saludo = f"Estimados/as <b>{don}</b>," if don and don != "—" else "Estimados/as,"
        story.append(Paragraph(saludo, self.styles["Body"]))
        story.append(Spacer(1, 10))

        contrib = (
            f"una aportación de <b>{ctx['importeeur']} €</b>"
            if tipo == "DINERO"
            else f"una donación de <b>{ctx['cantidadkg']} kg</b>"
        )

        firm_nombre = (ctx.get("firm_nombre") or "").strip() or ctx.get("entidad_nombre", "")
        firm_cargo = (ctx.get("firm_cargo") or "").strip()

        if firm_cargo:
            linea_cargo = f"{firm_cargo} — {ctx.get('entidad_nombre', '')}".strip(" —")
        else:
            linea_cargo = f"{ctx.get('entidad_nombre', '')}".strip()

        cuerpo = (
            f"En nombre de <b>{ctx['entidad_nombre']}</b> queremos expresar nuestro agradecimiento por {contrib}, "
            f"recibida el día <b>{ctx['fecha']}</b>.<br/><br/>"
            f"Su colaboración refuerza nuestra capacidad operativa para apoyar a familias en situación de vulnerabilidad.<br/><br/>"
            f"Adjuntamos el documento correspondiente (<b>{ctx['numerocertificado']}</b>).<br/><br/>"
            f"Con gratitud,<br/><br/>"
            f"<b>{firm_nombre}</b><br/>{linea_cargo}"
        )
        story.append(Paragraph(cuerpo, self.styles["Body"]))
        story.append(Spacer(1, 16))

        box = Table(
            [[Paragraph(f"Código de verificación: <font face='Courier'>{ctx['hash_seguridad']}</font>", self.styles["Mono"])]],
            colWidths=[15.2 * cm],
        )
        box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, self.GRID),
            ("BACKGROUND", (0, 0), (-1, -1), self.GREY_BG),
            ("PADDING", (0, 0), (-1, -1), 8),
            ("WORDWRAP", (0, 0), (-1, -1), "CJK"),
        ]))
        story.append(box)

        doc.build(story)
        return buf.getvalue()

    # -------------------- Persist helpers --------------------
    def _persist_row(
        self,
        *,
        con: sqlite3.Connection,
        cfg: dict,
        tenant_id: str,
        import_id: Optional[int],
        row_index: Optional[int],
        entidad: str,
        cif: str,
        fecha_dt: Optional[datetime],
        tipo_final: str,
        imp: float,
        kg: float,
        email_donante: str,
        num_cert: str,
        hash_seg: str,
        cert_status: str,
        carta_status: str,
        cert_path: str,
        carta_path: str,
        created_by: str = "",
        donor_cache: Optional[Dict[str, int]] = None,
        anon_donor_id: Optional[int] = None,
        meta_snapshot_json: str = "",
        row_hash: Optional[str] = None,
        existing_donation_id: Optional[int] = None,
        existing_donor_id: Optional[int] = None,
    ) -> Tuple[int, int, int]:
        ensure_business_schema_once(con)

        tenant_id = (tenant_id or "default").strip() or "default"
        cif_clean = _clean_tax_id(cif)
        donor_type = _infer_donor_type_from_tax_id(cif_clean)

        cache = donor_cache if isinstance(donor_cache, dict) else {}

        donor_id: int
        if existing_donor_id:
            donor_id = int(existing_donor_id)
        else:
            if cif_clean and (not self._should_validate_spanish_tax_id(cfg) or validate_spanish_tax_id(cif_clean)):
                if cif_clean in cache:
                    donor_id = int(cache[cif_clean])
                else:
                    donor_id = upsert_donor(
                        con,
                        tenant_id=tenant_id,
                        cifnif=cif_clean,
                        nombre=entidad,
                        email=email_donante,
                        donor_type=donor_type,
                        overwrite=False,
                    )
                    cache[cif_clean] = donor_id
            else:
                donor_id = int(anon_donor_id) if anon_donor_id else ensure_anonymous_donor(con, tenant_id=tenant_id)

        donation_id: int
        if existing_donation_id:
            donation_id = int(existing_donation_id)
        else:
            donation_id = insert_donation(
                con,
                tenant_id=tenant_id,
                donor_id=donor_id,
                import_id=import_id,
                row_index=row_index,
                row_hash=row_hash,
                fecha=fecha_dt or "",
                tipo=tipo_final,
                importe=imp if imp > 0 else None,
                kg=kg if kg > 0 else None,
                fuente="excel",
                raw_json="",
            )

        certificate_id = 0
        if num_cert:
            require_not_anonymous_donor(con, donor_id=donor_id)

            year = self._now(cfg).year
            try:
                m = re.search(r"-(\d{4})-", num_cert)
                if m:
                    year = int(m.group(1))
            except Exception:
                pass

            certificate_id = upsert_certificate(
                con,
                tenant_id=tenant_id,
                donor_id=donor_id,
                numerocertificado=num_cert,
                year=year,
                seq=None,
                hash_=hash_seg,
                tipo=tipo_final,
                fecha_emision=self._now(cfg).strftime("%d/%m/%Y"),
                status_certificado=cert_status,
                status_carta=carta_status,
                donation_id=None,
                import_id=import_id,
                created_by=created_by,
                cert_pdf_path=cert_path or "",
                carta_pdf_path=carta_path or "",
                email_to=email_donante,
                email_status="",
                emailed_at="",
                meta_snapshot_json=meta_snapshot_json or "",
                snapshot_update_policy="preserve",
            )
            link_certificate_to_donations(
                con,
                certificate_id=certificate_id,
                donation_ids=[donation_id],
                cleanup_legacy=True,
            )

        return int(donor_id), int(donation_id), int(certificate_id)

    # -------------------- SAVEPOINT util --------------------
    def _savepoint(self, con: sqlite3.Connection, name: str) -> None:
        con.execute(f"SAVEPOINT {name};")

    def _release(self, con: sqlite3.Connection, name: str) -> None:
        con.execute(f"RELEASE SAVEPOINT {name};")

    def _rollback_to(self, con: sqlite3.Connection, name: str) -> None:
        con.execute(f"ROLLBACK TO SAVEPOINT {name};")
        con.execute(f"RELEASE SAVEPOINT {name};")

    def _sp_name(self, idx: Any) -> str:
        try:
            i = int(idx)
        except Exception:
            i = abs(hash(str(idx))) % 1_000_000
        return f"r{i}"

    # -------------------- Re-emisión --------------------
    def build_pdfs_from_db_record(
        self,
        *,
        cert_row: Dict[str, Any],
        donor_row: Dict[str, Any],
        donations: List[Dict[str, Any]],
        tenant_cfg: dict,
    ) -> Tuple[bytes, bytes, Dict[str, Any]]:
        snapshot: Dict[str, Any] = {}
        raw = (cert_row or {}).get("meta_snapshot_json", "") if isinstance(cert_row, dict) else ""
        if raw:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    snapshot = obj
            except Exception:
                snapshot = {}

        snap_cfg = self._tenant_cfg_from_snapshot(snapshot)
        cfg_final, snap_warnings = self._apply_snapshot_with_logo_fallback(
            tenant_cfg_current=(tenant_cfg or {}),
            snap_cfg=snap_cfg,
        )
        cfg_eff = self._effective_config(cfg_final)

        totals = self._compute_totals_from_donations(donations or [])

        cert_tipo_raw = _safe_str((cert_row or {}).get("tipo", "")).upper()
        if cert_tipo_raw in ("DINERO", "MIXTO"):
            tipo_final = "DINERO"
        elif cert_tipo_raw == "ESPECIE":
            tipo_final = "ESPECIE"
        else:
            tipo_final = "ESPECIE" if (totals.total_kg > 0 and totals.total_importe <= 0) else "DINERO"

        tipo_pdf = self._cert_tipo_for_pdf(tipo_final)

        num = _safe_str((cert_row or {}).get("numerocertificado", ""))
        hash_seg = _safe_str((cert_row or {}).get("hash", ""))

        fecha_emision = _safe_str((cert_row or {}).get("fecha_emision", ""))
        if not fecha_emision:
            fecha_emision = self._now(cfg_eff).strftime("%d/%m/%Y")

        donor_name = _safe_str((donor_row or {}).get("nombre", ""))
        donor_cif = _safe_str((donor_row or {}).get("cifnif", ""))

        email_to = _safe_str((cert_row or {}).get("email_to", "")) or _safe_str((donor_row or {}).get("email", ""))

        ctx = self._build_render_context(
            cfg_eff=cfg_eff,
            numerocertificado=num,
            hash_seguridad=hash_seg,
            donor_nombre=donor_name,
            donor_cif=donor_cif,
            fecha_donacion=(totals.fecha_ref or "(según registro)"),
            total_importe=totals.total_importe,
            total_kg=totals.total_kg,
            fecha_emision=fecha_emision,
            tipo_pdf=tipo_pdf,
            email_to=email_to,
        )

        cert_pdf = self._build_cert_pdf_bytes(ctx, tipo_pdf, cfg_eff)

        carta_pdf = b""
        st_carta = _safe_str((cert_row or {}).get("status_carta", "")).upper()
        if st_carta in (self.OK, self.REVIEW):
            carta_pdf = self._build_letter_pdf_bytes(ctx, tipo_pdf, cfg_eff)

        meta = {
            "numerocertificado": ctx["numerocertificado"],
            "hash": ctx["hash_seguridad"],
            "tipo": tipo_pdf,
            "entidad": ctx["entidaddonante"],
            "cifnif": ctx["cifnif"],
            "fecha": ctx["fecha"],
            "importe": totals.total_importe,
            "kg": totals.total_kg,
            "email": ctx["email_donante"],
            "fecha_emision": ctx["fechaemision"],
            "used_snapshot": bool(snapshot),
            "warnings": snap_warnings,
            "status_certificado": _safe_str((cert_row or {}).get("status_certificado", "")),
            "status_carta": _safe_str((cert_row or {}).get("status_carta", "")),
        }
        return cert_pdf, carta_pdf, meta

    # -------------------- Lote (ZIP) --------------------
    def generate_zip_from_excel_bytes(
        self,
        excel_bytes: bytes,
        tenant_cfg: Optional[dict] = None,
        *,
        con_biz: sqlite3.Connection,
        con_auth: sqlite3.Connection,
        tenant_id: str = "default",
        progress_cb: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
        return_mode: str = "bytes",
    ) -> Tuple[Union[bytes, str], dict]:

        # ✅ SIEMPRE inicializa map_msgs (evita "cannot access local variable")
        map_msgs: List[str] = []

        # ✅ P0: auto-protector (por si el caller no activó FK)
        try:
            con_biz.execute("PRAGMA foreign_keys=ON;")
        except Exception:
            pass
        try:
            con_auth.execute("PRAGMA foreign_keys=ON;")
        except Exception:
            pass

        rm = (return_mode or "bytes").strip().lower()
        if rm not in ("bytes", "path"):
            rm = "bytes"

        tenant_id = (tenant_id or "default").strip() or "default"
        cfg_eff = self._effective_config(tenant_cfg)

        now_stamp = self._now(cfg_eff).strftime("%Y-%m")
        storage = cfg_eff.get("storage", {}) or {}
        out_dir_rel = str(storage.get("zip_output_dir", "backend/zip_output") or "backend/zip_output").strip()
        out_dir = (BASE_DIR / out_dir_rel).resolve()

        results: List[ResultRow] = []
        db_failures = 0

        spool_mb = float(storage.get("zip_spool_max_mb", 64) or 64)
        zip_mem = tempfile.SpooledTemporaryFile(max_size=int(spool_mb * 1024 * 1024), mode="w+b")

        try:
            # ---------- Excel ----------
            df = pd.read_excel(
                io.BytesIO(excel_bytes),
                engine="openpyxl",
                dtype=str,
                keep_default_na=False,
            )
            df = self.map_columns(df)
            df = self._ensure_required_columns(df)

            # ✅ aquí ya puedes asignar map_msgs (pero ya existe desde arriba)
            map_msgs = self._mapping_diagnostics(df)

            # ✅ DEBUG: qué features llegan realmente
            try:
                feats = cfg_eff.get("features", {}) if isinstance(cfg_eff.get("features"), dict) else {}
                map_msgs.append("DEBUG_FEATURES=" + json.dumps(feats, ensure_ascii=False))
                map_msgs.append("DEBUG_is_ley49=" + str(bool(feats.get("is_ley_49_2002", False))))
            except Exception as _e:
                map_msgs.append(f"DEBUG_FEATURES_ERROR={_e}")

            dayfirst = self._dayfirst(cfg_eff)
            df["fecha"] = df["fecha"].apply(lambda x: self._parse_fecha(x, dayfirst=dayfirst))

            # ---------- DB ----------
            ensure_business_schema_once(con_biz)

            created_by = _safe_str((tenant_cfg or {}).get("user", "")) if isinstance(tenant_cfg, dict) else ""
            source_filename = _safe_str((tenant_cfg or {}).get("source_filename", "")) if isinstance(tenant_cfg, dict) else ""

            import_id = create_import(
                con_biz,
                tenant_id=tenant_id,
                uploaded_by=created_by,
                source_filename=source_filename,
                source_bytes=excel_bytes,
                total_rows=int(len(df)),
            )

            donor_cache: Dict[str, int] = {}
            anon_donor_id = ensure_anonymous_donor(con_biz, tenant_id=tenant_id)

            total_rows = int(len(df))
            zip_mem.seek(0)

            with zipfile.ZipFile(zip_mem, "w", zipfile.ZIP_DEFLATED) as z:
                # ✅ Debug dentro del ZIP
                try:
                    z.writestr(
                        f"{now_stamp}/_debug_cfg_features.json",
                        json.dumps(
                            cfg_eff.get("features", {}) if isinstance(cfg_eff.get("features"), dict) else {},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                except Exception:
                    pass

                z.writestr(f"{now_stamp}/mapping_diagnostics.txt", "\n".join(map_msgs) + "\n")

                for n_done, (idx, fila) in enumerate(df.iterrows(), start=1):
                    num_cert = ""
                    hash_seg = ""
                    cert_arc = ""
                    carta_arc = ""
                    cert_bytes = b""
                    carta_bytes = b""

                    sp = self._sp_name(idx)
                    sp_active = False
                    reused = False
                    emitir_cert = False

                    try:
                        prepared = self._prepare_row_from_frame_row(fila, cfg_eff, df_columns=set(df.columns))
                        fecha_iso = self._fecha_iso(prepared.fecha_dt)

                        row_hash = self._make_row_hash(
                            tenant_id=tenant_id,
                            cif_clean=prepared.cif_clean,
                            fecha_iso=fecha_iso,
                            tipo_final=prepared.tipo_final,
                            imp=prepared.imp,
                            kg=prepared.kg,
                            entidad=prepared.entidad,
                            email=prepared.email_donante,
                        )

                        existing = self._lookup_existing_by_row_hash(con=con_biz, tenant_id=tenant_id, row_hash=row_hash)
                        existing_donation_id = existing.get("donation_id")
                        existing_donor_id = existing.get("donor_id")

                        if existing_donation_id and not self._exists_id(con_biz, "donations", int(existing_donation_id), tenant_id):
                            existing_donation_id = None
                        if existing_donor_id and not self._exists_id(con_biz, "donors", int(existing_donor_id), tenant_id):
                            existing_donor_id = None

                        existing_cert_pack = self._lookup_existing_cert_for_donation(
                            con=con_biz,
                            tenant_id=tenant_id,
                            donation_id=int(existing_donation_id) if existing_donation_id else None,
                        )

                        existing_cert_row = existing_cert_pack.get("cert_row") or existing.get("cert_row")
                        existing_donor_row = existing_cert_pack.get("donor_row") or existing.get("donor_row")
                        existing_donations = existing_cert_pack.get("donations") or existing.get("donations") or []

                        emitir_cert = prepared.cert_status in (self.OK, self.REVIEW)
                        existing_status_ok = _safe_str((existing_cert_row or {}).get("status_certificado", "")).upper() in (self.OK, self.REVIEW)

                        # 1) Reutilizar
                        if emitir_cert and existing_donation_id and existing_cert_row and existing_status_ok:
                            cert_bytes, carta_bytes, meta = self.build_pdfs_from_db_record(
                                cert_row=existing_cert_row,
                                donor_row=(existing_donor_row or {}),
                                donations=existing_donations,
                                tenant_cfg=(tenant_cfg or {}),
                            )
                            reused = True
                            prepared.motivos.append("REUTILIZADO")

                            prepared.cert_status = _safe_str(existing_cert_row.get("status_certificado", prepared.cert_status)).upper() or prepared.cert_status
                            prepared.carta_status = _safe_str(existing_cert_row.get("status_carta", prepared.carta_status)).upper() or prepared.carta_status

                            num_cert = _safe_str(meta.get("numerocertificado", "")) or _safe_str(existing_cert_row.get("numerocertificado", ""))
                            hash_seg = _safe_str(meta.get("hash", "")) or _safe_str(existing_cert_row.get("hash", ""))

                            if cert_bytes:
                                cert_arc = f"{now_stamp}/certificados/{num_cert.replace('-', '_')}.pdf"
                            if carta_bytes:
                                carta_arc = f"{now_stamp}/cartas/{num_cert.replace('-', '_')}_CARTA.pdf"

                        # 2) Emitir nuevo si había uno inválido
                        elif emitir_cert:
                            seq = self._reserve_next_seq_atomic(con_auth=con_auth, tenant_id=tenant_id, cfg=cfg_eff)
                            num_cert = self._emitir_num(cfg_eff, seq=seq)
                            hash_seg = self._hash(tenant_id, prepared.entidad, prepared.cif, num_cert, prepared.fecha_str, prepared.imp, prepared.kg)
                            tipo_pdf = self._cert_tipo_for_pdf(prepared.tipo_final)

                            ctx = self._build_render_context(
                                cfg_eff=cfg_eff,
                                numerocertificado=num_cert,
                                hash_seguridad=hash_seg,
                                donor_nombre=prepared.entidad,
                                donor_cif=prepared.cif,
                                fecha_donacion=prepared.fecha_str,
                                total_importe=prepared.imp,
                                total_kg=prepared.kg,
                                fecha_emision=self._now(cfg_eff).strftime("%d/%m/%Y"),
                                tipo_pdf=tipo_pdf,
                                email_to=prepared.email_donante,
                            )

                            meta_snapshot_json = self._build_meta_snapshot_json_for_cert(
                                cfg_eff=cfg_eff,
                                tipo_pdf=tipo_pdf,
                                numerocertificado=ctx.get("numerocertificado", num_cert),
                                hash_seguridad=ctx.get("hash_seguridad", hash_seg),
                                textolegal_final=ctx.get("textolegal", ""),
                            )

                            cert_bytes = self._build_cert_pdf_bytes(ctx, tipo_pdf, cfg_eff)
                            cert_arc = f"{now_stamp}/certificados/{ctx['numerocertificado'].replace('-', '_')}.pdf"

                            if prepared.carta_status in (self.OK, self.REVIEW):
                                carta_bytes = self._build_letter_pdf_bytes(ctx, tipo_pdf, cfg_eff)
                                carta_arc = f"{now_stamp}/cartas/{ctx['numerocertificado'].replace('-', '_')}_CARTA.pdf"

                            self._savepoint(con_biz, sp)
                            sp_active = True

                            self._persist_row(
                                con=con_biz,
                                cfg=cfg_eff,
                                tenant_id=tenant_id,
                                import_id=import_id,
                                row_index=_safe_int(idx),
                                entidad=prepared.entidad,
                                cif=prepared.cif,
                                fecha_dt=prepared.fecha_dt,
                                tipo_final=prepared.tipo_final,
                                imp=prepared.imp,
                                kg=prepared.kg,
                                email_donante=prepared.email_donante,
                                num_cert=ctx.get("numerocertificado", num_cert),
                                hash_seg=ctx.get("hash_seguridad", hash_seg),
                                cert_status=prepared.cert_status,
                                carta_status=prepared.carta_status,
                                cert_path=cert_arc,
                                carta_path=carta_arc,
                                created_by=created_by,
                                donor_cache=donor_cache,
                                anon_donor_id=anon_donor_id,
                                meta_snapshot_json=meta_snapshot_json,
                                row_hash=row_hash,
                                existing_donation_id=int(existing_donation_id) if existing_donation_id else None,
                                existing_donor_id=int(existing_donor_id) if existing_donor_id else None,
                            )

                            self._release(con_biz, sp)
                            sp_active = False

                        # ---------- ZIP write ----------
                        if cert_arc and cert_bytes:
                            z.writestr(cert_arc, cert_bytes)
                        if carta_arc and carta_bytes:
                            z.writestr(carta_arc, carta_bytes)

                        results.append(
                            ResultRow(
                                numerocertificado=num_cert,
                                entidad=prepared.entidad,
                                cifnif=prepared.cif_clean,
                                fecha=prepared.fecha_str,
                                tipodonacion=prepared.tipo_final,
                                importeeur=prepared.imp,
                                cantidadkg=prepared.kg,
                                hash=hash_seg,
                                estado_certificado=prepared.cert_status,
                                estado_carta=prepared.carta_status,
                                motivos="; ".join(prepared.motivos),
                                certificado=cert_arc,
                                carta=carta_arc,
                                email_donante=prepared.email_donante,
                            )
                        )

                        if progress_cb:
                            estado_real = "REUTILIZADO" if reused else ("OK" if cert_bytes else ("SKIP" if not emitir_cert else "INVALID"))
                            progress_cb(n_done, total_rows, {
                                "estado": estado_real,
                                "entidad": prepared.entidad,
                                "numerocertificado": num_cert,
                                "motivos": "; ".join(prepared.motivos),
                            })

                    except Exception as row_e:
                        db_failures += 1
                        if sp_active:
                            try:
                                self._rollback_to(con_biz, sp)
                            except Exception:
                                pass

                        results.append(ResultRow(
                            numerocertificado="",
                            entidad=_safe_str(fila.get("entidaddonante")),
                            cifnif=_safe_str(fila.get("cifnif")),
                            fecha="",
                            tipodonacion="",
                            importeeur=0.0,
                            cantidadkg=0.0,
                            hash="",
                            estado_certificado=self.INVALID,
                            estado_carta=self.INVALID,
                            motivos=f"INTERNAL_ERROR_ROW: {row_e}",
                        ))

                        if progress_cb:
                            progress_cb(n_done, total_rows, {"estado": "INVALID", "entidad": "ERROR", "motivos": str(row_e)})

                # log.csv dentro del ZIP (OJO: dentro del with z)
                z.writestr(
                    f"{now_stamp}/log.csv",
                    pd.DataFrame([r.__dict__ for r in results]).to_csv(index=False, encoding="utf-8-sig"),
                )

            # ---------- Summary ----------
            zip_mem.seek(0, 2)
            zip_size_mb = round(zip_mem.tell() / 1024 / 1024, 2)

            summary = self._summarize_results(results, db_failures=db_failures)
            summary["zip_size_mb"] = zip_size_mb
            summary["mapping_diagnostics"] = map_msgs

            if rm == "path":
                out_dir.mkdir(parents=True, exist_ok=True)
                path = (out_dir / f"{tenant_id}_{now_stamp}.zip").resolve()
                zip_mem.seek(0)
                path.write_bytes(zip_mem.read())
                summary["zip_path"] = str(path)
                return str(path), summary

            zip_mem.seek(0)
            return zip_mem.read(), summary

        finally:
            try:
                zip_mem.close()
            except Exception:
                pass


    def _summarize_results(self, results: List[ResultRow], *, db_failures: int) -> dict:
        total = len(results)
        cert_ok = sum(1 for r in results if r.estado_certificado == self.OK)
        cert_review = sum(1 for r in results if r.estado_certificado == self.REVIEW)
        cert_emitidos = cert_ok + cert_review

        carta_ok = sum(1 for r in results if r.estado_carta == self.OK)
        carta_review = sum(1 for r in results if r.estado_carta == self.REVIEW)
        carta_emitidas = carta_ok + carta_review

        filas_sin_nada = sum(
            1 for r in results
            if (r.estado_certificado not in {self.OK, self.REVIEW}) and (r.estado_carta not in {self.OK, self.REVIEW})
        )

        return {
            "total": total,
            "cert_emitidos": cert_emitidos,
            "cert_ok": cert_ok,
            "cert_review": cert_review,
            "cert_invalid": sum(1 for r in results if r.estado_certificado == self.INVALID),
            "cert_crash": sum(1 for r in results if r.estado_certificado == self.CRASH),
            "carta_emitidas": carta_emitidas,
            "carta_ok": carta_ok,
            "carta_review": carta_review,
            "carta_invalid": sum(1 for r in results if r.estado_carta == self.INVALID),
            "carta_crash": sum(1 for r in results if r.estado_carta == self.CRASH),
            "filas_sin_nada": filas_sin_nada,
            "db_failures": int(db_failures),
        }

    # -------------------- Individual --------------------
    def generate_individual_from_excel_bytes(
        self,
        excel_bytes: bytes,
        cifnif: Optional[str] = None,
        tenant_cfg: Optional[dict] = None,
        *,
        con_biz: sqlite3.Connection,
        con_auth: sqlite3.Connection,
        tenant_id: str = "default",
    ) -> Tuple[bytes, bytes, Dict[str, Any]]:

        try:
            con_biz.execute("PRAGMA foreign_keys=ON;")
        except Exception:
            pass
        try:
            con_auth.execute("PRAGMA foreign_keys=ON;")
        except Exception:
            pass

        tenant_id = (tenant_id or "default").strip() or "default"
        cfg_eff = self._effective_config(tenant_cfg)

        if not cifnif:
            raise ValueError("Debes indicar cifnif.")

        df = pd.read_excel(
            io.BytesIO(excel_bytes),
            engine="openpyxl",
            dtype=str,
            keep_default_na=False,
        )
        df = self.map_columns(df)
        df = self._ensure_required_columns(df)

        required = {"entidaddonante", "cifnif", "fecha", "tipodonacion"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Faltan columnas críticas tras mapeo: {', '.join(sorted(missing))}")

        dayfirst = self._dayfirst(cfg_eff)
        df["fecha"] = df["fecha"].apply(lambda x: self._parse_fecha(x, dayfirst=dayfirst))

        target = _clean_tax_id(cifnif)
        df["_cif_norm"] = df["cifnif"].astype(str).map(_clean_tax_id)
        df_sel = df[df["_cif_norm"] == target].copy()

        if df_sel.empty:
            raise ValueError(f"No encontré ninguna fila con CIF/NIF: {cifnif}")

        df_sel["_fecha_sort"] = df_sel["fecha"].apply(lambda x: x if isinstance(x, datetime) else datetime.min)
        df_sel = df_sel.sort_values(by="_fecha_sort", ascending=False)
        fila = df_sel.iloc[0]

        prepared = self._prepare_row_from_frame_row(fila, cfg_eff, df_columns=set(df.columns))

        if prepared.cert_status not in {self.OK, self.REVIEW}:
            raise ValueError("No puedo emitir certificado individual. Motivos: " + "; ".join(prepared.motivos))

        ensure_business_schema_once(con_biz)

        fecha_emision = self._now(cfg_eff).strftime("%d/%m/%Y")
        tipo_pdf = self._cert_tipo_for_pdf(prepared.tipo_final)

        fecha_iso = self._fecha_iso(prepared.fecha_dt)
        row_hash = self._make_row_hash(
            tenant_id=tenant_id,
            cif_clean=prepared.cif_clean,
            fecha_iso=fecha_iso,
            tipo_final=prepared.tipo_final,
            imp=prepared.imp,
            kg=prepared.kg,
            entidad=prepared.entidad,
            email=prepared.email_donante,
        )

        existing = self._lookup_existing_by_row_hash(con=con_biz, tenant_id=tenant_id, row_hash=row_hash)
        existing_donation_id = existing.get("donation_id")
        existing_donor_id = existing.get("donor_id")

        if existing_donation_id and not self._exists_id(con_biz, "donations", int(existing_donation_id), tenant_id):
            existing_donation_id = None
        if existing_donor_id and not self._exists_id(con_biz, "donors", int(existing_donor_id), tenant_id):
            existing_donor_id = None

        existing_cert_pack = self._lookup_existing_cert_for_donation(
            con=con_biz,
            tenant_id=tenant_id,
            donation_id=int(existing_donation_id) if existing_donation_id else None,
        )
        existing_cert_row = existing_cert_pack.get("cert_row") or existing.get("cert_row")
        existing_donor_row = existing_cert_pack.get("donor_row") or existing.get("donor_row")
        existing_donations = existing_cert_pack.get("donations") or existing.get("donations") or []

        existing_status_ok = _safe_str((existing_cert_row or {}).get("status_certificado", "")).upper() in (self.OK, self.REVIEW)

        if existing_donation_id and existing_cert_row and existing_status_ok:
            cert_pdf, carta_pdf, meta = self.build_pdfs_from_db_record(
                cert_row=existing_cert_row,
                donor_row=(existing_donor_row or {}),
                donations=existing_donations,
                tenant_cfg=(tenant_cfg or {}),
            )
            num_cert = _safe_str(meta.get("numerocertificado", "")) or _safe_str(existing_cert_row.get("numerocertificado", ""))
            hash_seg = _safe_str(meta.get("hash", "")) or _safe_str(existing_cert_row.get("hash", ""))

            meta_out = {
                "numerocertificado": num_cert,
                "hash": hash_seg,
                "tipo": prepared.tipo_final,
                "entidad": prepared.entidad,
                "cifnif": prepared.cif_clean,
                "fecha": prepared.fecha_str,
                "importe": prepared.imp,
                "kg": prepared.kg,
                "email": prepared.email_donante,
                "fecha_emision": fecha_emision,
                "warnings": prepared.motivos,
                "status_certificado": prepared.cert_status,
                "status_carta": prepared.carta_status,
            }
            return cert_pdf, carta_pdf, meta_out

        if existing_donation_id and existing_cert_row and not existing_status_ok:
            prepared.motivos.append("EXISTING_CERT_INVALID_NOT_REUSED_EMIT_NEW")

        seq = self._reserve_next_seq_atomic(con_auth=con_auth, tenant_id=tenant_id, cfg=cfg_eff)
        num_cert = self._emitir_num(cfg_eff, seq=seq)
        hash_seg = self._hash(tenant_id, prepared.entidad, prepared.cif, num_cert, prepared.fecha_str, prepared.imp, prepared.kg)

        ctx = self._build_render_context(
            cfg_eff=cfg_eff,
            numerocertificado=num_cert,
            hash_seguridad=hash_seg,
            donor_nombre=prepared.entidad,
            donor_cif=prepared.cif,
            fecha_donacion=prepared.fecha_str,
            total_importe=prepared.imp,
            total_kg=prepared.kg,
            fecha_emision=fecha_emision,
            tipo_pdf=tipo_pdf,
            email_to=prepared.email_donante,
        )

        meta_snapshot_json = self._build_meta_snapshot_json_for_cert(
            cfg_eff=cfg_eff,
            tipo_pdf=tipo_pdf,
            numerocertificado=ctx.get("numerocertificado", num_cert),
            hash_seguridad=ctx.get("hash_seguridad", hash_seg),
            textolegal_final=ctx.get("textolegal", ""),
        )

        cert_pdf = self._build_cert_pdf_bytes(ctx, tipo_pdf, cfg_eff)

        carta_pdf = b""
        if prepared.carta_status in (self.OK, self.REVIEW):
            try:
                carta_pdf = self._build_letter_pdf_bytes(ctx, tipo_pdf, cfg_eff)
            except Exception:
                carta_pdf = b""

        sp = "ind"
        self._savepoint(con_biz, sp)
        try:
            created_by = _safe_str((tenant_cfg or {}).get("user", "")) if isinstance(tenant_cfg, dict) else ""
            source_filename = _safe_str((tenant_cfg or {}).get("source_filename", "")) if isinstance(tenant_cfg, dict) else ""

            import_id = create_import(
                con_biz,
                tenant_id=tenant_id,
                uploaded_by=created_by,
                source_filename=source_filename,
                source_bytes=excel_bytes,
                total_rows=int(len(df)),
            )

            anon_id = ensure_anonymous_donor(con_biz, tenant_id=tenant_id)
            donor_cache: Dict[str, int] = {}

            self._persist_row(
                con=con_biz,
                cfg=cfg_eff,
                tenant_id=tenant_id,
                import_id=import_id,
                row_index=None,
                entidad=prepared.entidad,
                cif=prepared.cif,
                fecha_dt=prepared.fecha_dt,
                tipo_final=prepared.tipo_final,
                imp=prepared.imp,
                kg=prepared.kg,
                email_donante=prepared.email_donante,
                num_cert=ctx.get("numerocertificado", num_cert),
                hash_seg=ctx.get("hash_seguridad", hash_seg),
                cert_status=prepared.cert_status,
                carta_status=prepared.carta_status,
                cert_path=f"individual/{ctx.get('numerocertificado', num_cert).replace('-', '_')}_CERT.pdf",
                carta_path=f"individual/{ctx.get('numerocertificado', num_cert).replace('-', '_')}_CARTA.pdf" if carta_pdf else "",
                created_by=created_by,
                donor_cache=donor_cache,
                anon_donor_id=anon_id,
                meta_snapshot_json=meta_snapshot_json,
                row_hash=row_hash,
                existing_donation_id=int(existing_donation_id) if existing_donation_id else None,
                existing_donor_id=int(existing_donor_id) if existing_donor_id else None,
            )

            num_cert = ctx.get("numerocertificado", num_cert)
            hash_seg = ctx.get("hash_seguridad", hash_seg)

            self._release(con_biz, sp)
        except Exception as e:
            try:
                self._rollback_to(con_biz, sp)
            except Exception:
                pass
            raise e

        meta = {
            "numerocertificado": num_cert,
            "hash": hash_seg,
            "tipo": prepared.tipo_final,
            "entidad": prepared.entidad,
            "cifnif": prepared.cif_clean,
            "fecha": prepared.fecha_str,
            "importe": prepared.imp,
            "kg": prepared.kg,
            "email": prepared.email_donante,
            "fecha_emision": fecha_emision,
            "warnings": prepared.motivos,
            "status_certificado": prepared.cert_status,
            "status_carta": prepared.carta_status,
        }

        return cert_pdf, carta_pdf, meta