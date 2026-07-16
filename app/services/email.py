import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from app.config import get_settings

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, body_html: str, body_text: str) -> None:
    settings = get_settings()
    if not settings.smtp_host:
        raise ValueError("SMTP_HOST is not configured")
    if not to:
        raise ValueError("No email recipient configured")

    address = settings.smtp_from or settings.smtp_user
    # Allow SMTP_FROM to already include a display name
    existing_name, existing_addr = parseaddr(address)
    from_addr = existing_addr or address
    from_name = existing_name or settings.smtp_from_name or "Finance"
    from_header = formataddr((from_name, from_addr))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_header
    msg["To"] = to
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    if settings.smtp_use_tls:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(from_addr, [to], msg.as_string())
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(from_addr, [to], msg.as_string())

    logger.info("Sent email to %s from %s: %s", to, from_header, subject)
