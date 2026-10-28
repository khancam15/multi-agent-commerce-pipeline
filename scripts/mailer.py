"""
mailer.py — Shared SMTP sender
───────────────────────────────
Sends email through the account configured in .env. Used for buyer
deliveries (order_delivery.py) and failure alerts (alerts.py).

Requires in .env:
    EMAIL_FROM      — sending address
    EMAIL_SMTP_PASS — SMTP password (for Gmail, an App Password)
Optional:
    SMTP_HOST       — default smtp.gmail.com
    SMTP_PORT       — default 587 (STARTTLS)
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def is_configured() -> bool:
    return bool(os.getenv("EMAIL_FROM") and os.getenv("EMAIL_SMTP_PASS"))


def send_email(to: str, subject: str, html_body: str, text_body: str = "") -> None:
    """Send one email. Raises RuntimeError if SMTP is not configured."""
    sender   = os.getenv("EMAIL_FROM", "")
    password = os.getenv("EMAIL_SMTP_PASS", "")
    if not (sender and password):
        raise RuntimeError("EMAIL_FROM and EMAIL_SMTP_PASS must be set in .env")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to
    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, [to], msg.as_string())
