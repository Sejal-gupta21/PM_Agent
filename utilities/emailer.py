import base64
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
import mimetypes
from typing import Iterable, List, Optional, Tuple, Union

import logging
from .report import format_html_table_for_wi

# NOTE: SMTP-based sending and testing code has been deprecated in favor of
# SendGrid API delivery. The old SMTP functions were removed to avoid
# accidental use; email content generation remains unchanged.

try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import (
        Attachment,
        Disposition,
        FileContent,
        FileName,
        FileType,
        Mail,
    )
except Exception:
    SendGridAPIClient = None
    Mail = None
    Attachment = None
    Disposition = None
    FileContent = None
    FileName = None
    FileType = None


def send_email_via_sendgrid(
    to: List[str],
    subject: str,
    html_body: str,
    attachments: Optional[List[Path]] = None,
) -> Tuple[int, str]:
    """
    Send HTML email via SendGrid API.

    Returns tuple (status_code, body) on success or raises on error.
    Requires configuration: sendgrid_api_key and from_email.
    """
    from config import config as app_config
    api_key = app_config.sendgrid_api_key
    from_email = app_config.from_email
    if not api_key or not from_email:
        raise RuntimeError("SendGrid not configured. Set sendgrid_api_key and from_email in config.yaml.")
    if SendGridAPIClient is None or Mail is None:
        raise RuntimeError("sendgrid package is not installed in the environment")

    message = Mail(from_email=from_email, to_emails=to, subject=subject, html_content=html_body)
    for attachment_path in attachments or []:
        path = Path(attachment_path)
        if not path.exists():
            continue
        with open(path, "rb") as fp:
            encoded = base64.b64encode(fp.read()).decode()
        ctype, _ = mimetypes.guess_type(str(path))
        ctype = ctype or "application/octet-stream"
        if Attachment and FileContent and FileName and FileType and Disposition:
            attachment = Attachment(
                FileContent(encoded),
                FileName(path.name),
                FileType(ctype),
                Disposition("attachment"),
            )
            message.add_attachment(attachment)
    try:
        sg = SendGridAPIClient(api_key)
        resp = sg.send(message)
        # resp.status_code is int, resp.body bytes
        body = resp.body.decode() if hasattr(resp.body, 'decode') else str(resp.body)
        return resp.status_code, body
    except Exception as e:
        print(f"[ERROR] SendGrid send error: {e}")
        # Return a non-2xx-like status to indicate failure and include the error text
        return 500, str(e)


def send_wi_report(work_item_id: str, data: dict, pm_email: Optional[str] = None) -> Tuple[bool, str]:
    """Convenience wrapper: format WI data and send to `pm_email` (or default_pm_email from config).

    Sends via SendGrid when configured, otherwise falls back to SMTP.
    """
    if pm_email is None:
        from config import config as app_config
        pm_email = app_config.default_pm_email
    if not pm_email:
        raise RuntimeError("No project manager email provided and default_pm_email not set.")

    subject = f"WI {work_item_id} - Time Logs & Deployment Schedule"
    body = format_html_table_for_wi(work_item_id, data)
    success, message = send_report_attachment([pm_email], subject, body, None)
    return success, message


def send_email_via_smtp(
    to: List[str],
    subject: str,
    html_body: str,
    attachments: Optional[List[Path]] = None,
) -> Tuple[bool, str]:
    """
    Send HTML email (optionally with attachments) via SMTP.

    Uses SMTP configuration from config.yaml.
    """
    from config import config as app_config
    import logging
    logger = logging.getLogger("pm_agent.emailer")

    host = app_config.smtp_host
    port = app_config.smtp_port
    username = app_config.smtp_username
    password = app_config.smtp_password
    from_email = app_config.smtp_from or username
    
    logger.info(f"Attempting SMTP send to {to} via {host}:{port}")

    if not username or not password or not from_email:
        error_msg = "SMTP not configured. Set SMTP username/password and from address in environment."
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content("HTML-capable email client required.")
    msg.add_alternative(html_body, subtype="html")

    for attachment in attachments or []:
        path = Path(attachment)
        if not path.exists():
            continue
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype.split("/", 1) if ctype else ("application", "octet-stream"))
        with open(path, "rb") as fp:
            data = fp.read()
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)

    try:
        with smtplib.SMTP(host, port) as server:
            server.set_debuglevel(0)
            server.starttls()
            logger.info(f"SMTP: Logging in as {username}")
            server.login(username, password)
            logger.info(f"SMTP: Sending message to {to}")
            server.send_message(msg)
        logger.info(f"SMTP: Email sent successfully to {to}")
        return True, "sent"
    except Exception as e:
        # Provide a clear error message instead of raising to the caller
        logger.error(f"SMTP send error: {e}")
        print(f"[ERROR] SMTP send error: {e}")
        return False, str(e)


def send_report_attachment(
    to_emails: Union[str, Iterable[str]],
    subject: str,
    html_body: str,
    attachments: Optional[Iterable[Union[str, Path]]] = None,
) -> Tuple[bool, str]:
    """Send a report email (with optional attachments) via SendGrid when configured, otherwise SMTP."""
    import logging
    logger = logging.getLogger("pm_agent.emailer")

    if isinstance(to_emails, str):
        to_list = [addr.strip() for addr in to_emails.split(",") if addr.strip()]
    else:
        to_list = [addr.strip() for addr in to_emails if isinstance(addr, str) and addr.strip()]

    logger.info(f"send_report_attachment called: to={to_list}, subject={subject}")
    
    attach_paths: List[Path] = []
    for attachment in attachments or []:
        path = Path(attachment)
        if path.exists():
            attach_paths.append(path)
            logger.info(f"Attachment found: {path}")
        else:
            logger.warning(f"Attachment not found: {path}")

    from config import config as app_config
    sendgrid_ready = (
        SendGridAPIClient is not None
        and Mail is not None
        and app_config.sendgrid_api_key
        and app_config.from_email
    )

    # check SMTP readiness
    smtp_username = app_config.smtp_username
    smtp_password = app_config.smtp_password
    smtp_from = app_config.smtp_from or smtp_username
    smtp_ready = bool(smtp_username and smtp_password and smtp_from)

    logger.info(f"Email provider status: SendGrid={sendgrid_ready}, SMTP={smtp_ready}")

    if not sendgrid_ready and not smtp_ready:
        logger.warning(
            "No email provider configured: neither SendGrid nor SMTP credentials are available. Check config.yaml."
        )
        return False, "No email provider configured (SENDGRID or SMTP)"

    if sendgrid_ready:
        logger.info("Using SendGrid for email delivery")
        try:
            status_code, resp_body = send_email_via_sendgrid(to_list, subject, html_body, attach_paths)
            ok = 200 <= status_code < 300
            return ok, resp_body
        except Exception as e:
            print(f"[ERROR] SendGrid unexpected error: {e}")
            return False, str(e)

    logger.info("Using SMTP for email delivery")
    try:
        ok, msg = send_email_via_smtp(to_list, subject, html_body, attach_paths or None)
        logger.info(f"SMTP result: ok={ok}, msg={msg}")
    except Exception as e:
        logger.error(f"SMTP unexpected error: {e}")
        print(f"[ERROR] SMTP unexpected error: {e}")
        ok, msg = False, str(e)

    # Persist an email send log for diagnostics
    try:
        log_dir = Path('logs')
        log_dir.mkdir(exist_ok=True)
        with (log_dir / 'email.log').open('a', encoding='utf-8') as lf:
            from datetime import datetime as _dt
            lf.write(f"{_dt.utcnow().isoformat()}Z | to={to_list} | subject={subject} | ok={ok} | msg={msg}\n")
    except Exception as _:
        # non-fatal -- do not crash on logging failure
        pass

    return ok, msg


def is_email_ready() -> (bool, str):
    """Return (ready, message). True if SendGrid or SMTP appears configured."""
    from config import config as app_config
    sendgrid_ready = (
        SendGridAPIClient is not None
        and Mail is not None
        and app_config.sendgrid_api_key
        and app_config.from_email
    )
    smtp_username = app_config.smtp_username
    smtp_password = app_config.smtp_password
    smtp_from = app_config.smtp_from or smtp_username
    smtp_ready = bool(smtp_username and smtp_password and smtp_from)
    if sendgrid_ready:
        return True, "SendGrid configured"
    if smtp_ready:
        return True, "SMTP configured"
    return False, "No SendGrid or SMTP credentials configured"
