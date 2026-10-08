# backend/mailer.py
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import List, Optional, Sequence, Tuple

Attachment = Tuple[str, bytes, str]  # (filename, data, mimetype)


def smtp_config_ok() -> bool:
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"]
    return all((os.getenv(k) or "").strip() for k in required)


def _as_list(value: Optional[str | Sequence[str]]) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        parts = [x.strip() for x in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    return [str(x).strip() for x in value if str(x).strip()]


def _looks_like_email(addr: str) -> bool:
    addr = (addr or "").strip()
    if " " in addr:
        return False
    if addr.count("@") != 1:
        return False
    user, domain = addr.split("@", 1)
    return bool(user) and ("." in domain)


def send_email_with_attachments(
    *,
    to_email: str | Sequence[str],
    subject: str,
    body: str,
    attachments: List[Attachment],
    cc: Optional[str | Sequence[str]] = None,
) -> None:
    """
    ✅ Regla 1: este método NO decide "test mode" por su cuenta.
    - O envía, o lanza excepción.

    ✅ Si quieres simulación local: usa SMTP_DRY_RUN=1 explícitamente.
    """
    to_list = _as_list(to_email)
    cc_list = _as_list(cc)

    if not to_list:
        raise ValueError("to_email está vacío.")
    bad = [a for a in (to_list + cc_list) if not _looks_like_email(a)]
    if bad:
        raise ValueError(f"Emails inválidos: {', '.join(bad)}")

    host = (os.getenv("SMTP_HOST") or "").strip()
    port_raw = (os.getenv("SMTP_PORT") or "587").strip()
    user = (os.getenv("SMTP_USER") or "").strip()
    pwd = (os.getenv("SMTP_PASS") or "").strip()
    from_addr = (os.getenv("SMTP_FROM") or "").strip()
    tls = (os.getenv("SMTP_TLS") or "1").strip() == "1"
    timeout = int(((os.getenv("SMTP_TIMEOUT") or "30").strip()) or "30")

    # ✅ DRY RUN explícito (nunca accidental)
    dry_run = (os.getenv("SMTP_DRY_RUN") or "0").strip() == "1"
    if dry_run:
        print("\n==============================")
        print("📧 EMAIL SIMULADO (SMTP_DRY_RUN=1)")
        print("==============================")
        print(f"TO: {', '.join(to_list)}")
        if cc_list:
            print(f"CC: {', '.join(cc_list)}")
        print(f"SUBJECT: {subject}")
        print("BODY (primeros 300 chars):")
        print((body or "")[:300] + ("..." if body and len(body) > 300 else ""))
        print("ATTACHMENTS:")
        for (fn, data, mt) in attachments:
            size_kb = (len(data) / 1024) if isinstance(data, (bytes, bytearray)) else 0.0
            print(f" - {fn} ({(mt or 'application/octet-stream')}) ~ {size_kb:.1f} KB")
        print("==============================\n")
        return

    # ✅ En prod: si falta config, revienta (no simula)
    if not (host and port_raw and user and pwd and from_addr):
        raise RuntimeError("SMTP no configurado (faltan SMTP_HOST/PORT/USER/PASS/FROM).")

    try:
        port = int(port_raw)
    except ValueError:
        port = 587

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg.set_content(body or "")

    for filename, data, mimetype in attachments:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"Adjunto '{filename}' no es bytes.")
        mt = (mimetype or "application/octet-stream").strip()
        if "/" not in mt:
            mt = "application/octet-stream"
        maintype, subtype = mt.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    with smtplib.SMTP(host, port, timeout=timeout) as s:
        if tls:
            s.ehlo()
            s.starttls()
            s.ehlo()
        s.login(user, pwd)
        s.send_message(msg)