import os
from pathlib import Path
from mailer import send_email_with_attachments


def main():

    # Email destino para test
    # prioridad:
    # 1. TEST_TO
    # 2. SMTP_USER
    # 3. fallback local
    to_email = (
        os.getenv("TEST_TO")
        or os.getenv("SMTP_USER")
        or "test@example.com"
    )

    # Adjuntar PDF opcional de prueba
    attachments = []

    pdf_path = os.getenv("TEST_PDF")

    if pdf_path:
        p = Path(pdf_path)

        if p.exists():
            attachments.append(
                (
                    p.name,
                    p.read_bytes(),
                    "application/pdf"
                )
            )
            print(f"Adjunto incluido: {p.name}")
        else:
            print("⚠️ TEST_PDF no existe, continuo sin adjunto.")

    else:
        print("ℹ️ No se especificó TEST_PDF, envío sin adjunto.")

    # Enviar (o simular)
    send_email_with_attachments(
        to_email=to_email,
        subject="TEST BdA - Envío automático OK",
        body=(
            "Este es un email de prueba del sistema BdA.\n\n"
            "Si ves esto en consola, el sistema funciona correctamente.\n\n"
            "— Dani"
        ),
        attachments=attachments,
    )

    print("✅ Test completado correctamente")


if __name__ == "__main__":
    main()