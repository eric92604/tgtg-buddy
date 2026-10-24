"""SMTP email notifier — never raises into callers."""

from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

from app import config as config_mod
from app import secrets as secrets_mod

logger = logging.getLogger(__name__)


class EmailNotifier:
    """Send alert emails using config.json SMTP settings + encrypted password."""

    async def send(self, subject: str, body: str) -> bool:
        """Send an email. Returns True on success, False on failure. Never raises."""
        try:
            cfg = config_mod.load_config()
            smtp = cfg.get("smtp") or {}
            host = (smtp.get("host") or "").strip()
            to_addr = (smtp.get("to_addr") or "").strip()
            from_addr = (smtp.get("from_addr") or to_addr or "").strip()
            if not host or not to_addr or not from_addr:
                logger.warning("SMTP not configured; skipping email: %s", subject)
                return False

            port = int(smtp.get("port") or 587)
            username = (smtp.get("username") or "").strip()
            use_tls = bool(smtp.get("use_tls", True))
            password = secrets_mod.get_smtp_password() or ""

            msg = EmailMessage()
            msg["From"] = from_addr
            msg["To"] = to_addr
            msg["Subject"] = subject
            msg.set_content(body)

            await aiosmtplib.send(
                msg,
                hostname=host,
                port=port,
                username=username or None,
                password=password or None,
                start_tls=use_tls,
            )
            logger.info("Email sent: %s", subject)
            return True
        except Exception:
            logger.exception("Failed to send email: %s", subject)
            return False


notifier = EmailNotifier()
