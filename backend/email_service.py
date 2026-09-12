"""
Email notification service for OnboardAI.

Handles:
  1. Task-assignment emails  -> sent automatically the moment HR creates an
     employee (which is when onboarding tasks are assigned).
  2. Reminder emails         -> sent automatically by a background scheduler
     (see backend/scheduler.py) for tasks that are due soon or overdue.
     No HR action is required for reminders to go out.

Configuration is read from environment variables (a `.env` file in the
project root is loaded automatically via python-dotenv). If SMTP is not
configured, emails are not silently dropped -- they are written to
`data/email_outbox.log` instead so the flow can still be demoed/tested
without a real mail server, and so every attempt is auditable.

Required env vars for real delivery:
    SMTP_HOST
    SMTP_PORT           (default 587)
    SMTP_USERNAME
    SMTP_PASSWORD
    SMTP_FROM_EMAIL     (default = SMTP_USERNAME)
    SMTP_FROM_NAME      (default "OnboardAI")
    SMTP_USE_TLS        (default "true")
    EMAIL_NOTIFICATIONS_ENABLED (default "true")
"""
import os
import smtplib
import ssl
import json
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("onboardai.email")

BASE = Path(__file__).resolve().parents[1]
DATA_DIR = BASE / "data"
OUTBOX_LOG = DATA_DIR / "email_outbox.log"

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME)
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "OnboardAI")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("1", "true", "yes")
EMAIL_ENABLED = os.getenv("EMAIL_NOTIFICATIONS_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def _smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_EMAIL)


def _log_outbox(to_email: str, subject: str, body_text: str, sent: bool, error: str | None = None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "to": to_email,
        "subject": subject,
        "sent_via_smtp": sent,
        "error": error,
        "body_preview": body_text[:500],
    }
    with OUTBOX_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def send_email(to_email: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """Send a single email. Returns True on a confirmed SMTP send.

    Never raises -- failures are logged and returned as False so a mail
    outage can never break task assignment or the reminder scheduler.
    """
    if not EMAIL_ENABLED:
        _log_outbox(to_email, subject, body_text, sent=False, error="email notifications disabled")
        return False
    if not to_email:
        _log_outbox(to_email, subject, body_text, sent=False, error="missing recipient email")
        return False

    if not _smtp_configured():
        # Demo / not-yet-configured mode: record what would have been sent.
        _log_outbox(to_email, subject, body_text, sent=False, error="SMTP not configured")
        logger.info("SMTP not configured; logged email to %s ('%s') in outbox log instead of sending.",
                    to_email, subject)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = to_email
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        if SMTP_USE_TLS:
            context = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.starttls(context=context)
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        _log_outbox(to_email, subject, body_text, sent=True)
        return True
    except Exception as exc:  # noqa: BLE001 - we want to swallow all send errors
        logger.exception("Failed to send email to %s", to_email)
        _log_outbox(to_email, subject, body_text, sent=False, error=str(exc))
        return False


def _task_lines(task_rows) -> str:
    lines = []
    for t in task_rows:
        due = t.get("due_date") or "no due date"
        lines.append(f"  - {t.get('task_name')} (due {due}, owner: {t.get('action_required_by', 'Employee')})")
    return "\n".join(lines)


def send_task_assignment_email(employee_email: str, employee_id: str, role: str,
                                department: str, login_id: str, task_rows) -> bool:
    """Sent automatically the moment HR assigns onboarding tasks to a new employee."""
    subject = f"Your onboarding tasks have been assigned — {role}"
    task_text = _task_lines(task_rows)
    body_text = (
        f"Hi,\n\n"
        f"Welcome aboard! HR has created your OnboardAI account and assigned your onboarding tasks "
        f"for the role of {role} ({department}).\n\n"
        f"Login ID: {login_id}\n"
        f"Employee ID: {employee_id}\n\n"
        f"Your assigned tasks ({len(task_rows)}):\n{task_text}\n\n"
        f"Please log in to the OnboardAI portal to review and complete these tasks.\n"
        f"You will automatically receive reminder emails as due dates approach, and if any task "
        f"becomes overdue, so you never need to ask HR for a status update.\n\n"
        f"— OnboardAI"
    )
    rows_html = "".join(
        f"<li><b>{t.get('task_name')}</b> — due {t.get('due_date') or 'n/a'} "
        f"(owner: {t.get('action_required_by', 'Employee')})</li>"
        for t in task_rows
    )
    body_html = (
        f"<p>Hi,</p>"
        f"<p>Welcome aboard! HR has created your OnboardAI account and assigned your onboarding tasks "
        f"for the role of <b>{role}</b> ({department}).</p>"
        f"<p>Login ID: <b>{login_id}</b><br>Employee ID: <b>{employee_id}</b></p>"
        f"<p>Your assigned tasks ({len(task_rows)}):</p><ul>{rows_html}</ul>"
        f"<p>Please log in to the OnboardAI portal to review and complete these tasks. "
        f"You will automatically receive reminder emails as due dates approach or if a task becomes overdue.</p>"
        f"<p>— OnboardAI</p>"
    )
    return send_email(employee_email, subject, body_text, body_html)


def send_task_reminder_email(employee_email: str, employee_id: str, due_soon_rows, overdue_rows) -> bool:
    """Sent automatically by the scheduler — no HR input required."""
    total = len(due_soon_rows) + len(overdue_rows)
    if total == 0:
        return False
    subject = f"Reminder: {total} onboarding task(s) need your attention"

    parts_text = []
    if overdue_rows:
        parts_text.append(f"Overdue ({len(overdue_rows)}):\n{_task_lines(overdue_rows)}")
    if due_soon_rows:
        parts_text.append(f"Due soon ({len(due_soon_rows)}):\n{_task_lines(due_soon_rows)}")
    body_text = (
        f"Hi,\n\n"
        f"This is an automatic reminder from OnboardAI about your onboarding tasks.\n\n"
        + "\n\n".join(parts_text) +
        f"\n\nPlease log in to the OnboardAI portal to complete these tasks.\n\n— OnboardAI"
    )

    def _rows_html(rows):
        return "".join(
            f"<li><b>{t.get('task_name')}</b> — due {t.get('due_date') or 'n/a'}</li>" for t in rows
        )
    sections_html = ""
    if overdue_rows:
        sections_html += f"<p><b style='color:#b91c1c'>Overdue ({len(overdue_rows)}):</b></p><ul>{_rows_html(overdue_rows)}</ul>"
    if due_soon_rows:
        sections_html += f"<p><b>Due soon ({len(due_soon_rows)}):</b></p><ul>{_rows_html(due_soon_rows)}</ul>"
    body_html = (
        f"<p>Hi,</p><p>This is an automatic reminder from OnboardAI about your onboarding tasks.</p>"
        f"{sections_html}"
        f"<p>Please log in to the OnboardAI portal to complete these tasks.</p><p>— OnboardAI</p>"
    )
    return send_email(employee_email, subject, body_text, body_html)
