"""
Background scheduler that sends onboarding task reminder emails automatically
-- with no HR input required.

Adapted from the standalone email-integrated build for OnboardAI's SQLite
backend (no pandas dependency): it periodically scans the live `tasks` table
for each employee and emails:
  - any task that is already overdue, and
  - any task that is due within REMINDER_LOOKAHEAD_DAYS.

To avoid spamming the same person every few minutes, a reminder is only
resent to a given employee once every REMINDER_COOLDOWN_HOURS. This state is
kept on disk (data/reminder_state.json) so it survives restarts.
"""
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from backend import email_service

logger = logging.getLogger("onboardai.scheduler")

BASE = Path(__file__).resolve().parents[1]
DATA_DIR = BASE / "data"
REMINDER_STATE_FILE = DATA_DIR / "reminder_state.json"

REMINDER_LOOKAHEAD_DAYS = int(os.getenv("REMINDER_LOOKAHEAD_DAYS", "2"))
REMINDER_COOLDOWN_HOURS = float(os.getenv("REMINDER_COOLDOWN_HOURS", "24"))
REMINDER_CHECK_INTERVAL_MINUTES = int(os.getenv("REMINDER_CHECK_INTERVAL_MINUTES", "60"))

_scheduler: Optional[BackgroundScheduler] = None

_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y", "%m/%d/%Y")


def _load_state() -> dict:
    if REMINDER_STATE_FILE.exists():
        try:
            return json.loads(REMINDER_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REMINDER_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_date(value):
    if not value:
        return None
    text = str(value).strip()[:19]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _due_within(due_date, days: int) -> bool:
    d = _parse_date(due_date)
    if not d:
        return False
    today = datetime.now().date()
    return today <= d <= today + timedelta(days=days)


def _is_overdue(row: dict) -> bool:
    raw = row.get("is_overdue")
    flag = str(raw).strip().lower() in ("true", "1", "yes") if not isinstance(raw, bool) else raw
    if flag:
        return True
    d = _parse_date(row.get("due_date"))
    return bool(d and d < datetime.now().date() and str(row.get("status")) != "Completed")


def run_reminder_sweep(get_pending_tasks_by_employee: Callable[[], dict], get_employee_email: Callable[[str], str]):
    """Scan all pending tasks and send reminder emails automatically.

    get_pending_tasks_by_employee: zero-arg callable returning
        {employee_id: [ {task_name, due_date, status, is_overdue, action_required_by}, ... ]}
    get_employee_email: callable(employee_id) -> email address or "".
    """
    grouped = get_pending_tasks_by_employee()
    if not grouped:
        return

    state = _load_state()
    now = datetime.now(timezone.utc)

    for employee_id, rows in grouped.items():
        overdue_rows, due_soon_rows = [], []
        for row in rows:
            if str(row.get("status")) == "Completed":
                continue
            task_row = {
                "task_name": row.get("task_name", ""),
                "due_date": row.get("due_date") or "",
                "action_required_by": row.get("action_required_by", "Employee"),
            }
            if _is_overdue(row):
                overdue_rows.append(task_row)
            elif _due_within(row.get("due_date"), REMINDER_LOOKAHEAD_DAYS):
                due_soon_rows.append(task_row)

        if not overdue_rows and not due_soon_rows:
            continue

        last_sent = state.get(employee_id)
        if last_sent:
            try:
                last_dt = datetime.fromisoformat(last_sent)
                if now - last_dt < timedelta(hours=REMINDER_COOLDOWN_HOURS):
                    continue  # still within cooldown, skip to avoid spamming
            except Exception:
                pass

        email = get_employee_email(employee_id)
        if not email:
            continue

        sent = email_service.send_task_reminder_email(email, employee_id, due_soon_rows, overdue_rows)
        state[employee_id] = now.isoformat()
        logger.info(
            "Reminder sweep: employee=%s overdue=%d due_soon=%d smtp_sent=%s",
            employee_id, len(overdue_rows), len(due_soon_rows), sent,
        )

    _save_state(state)


def start_scheduler(get_pending_tasks_by_employee: Callable[[], dict], get_employee_email: Callable[[str], str]):
    """Idempotently start the background reminder scheduler."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        lambda: run_reminder_sweep(get_pending_tasks_by_employee, get_employee_email),
        "interval",
        minutes=REMINDER_CHECK_INTERVAL_MINUTES,
        id="onboarding_reminder_sweep",
        next_run_time=datetime.now(),  # also run once immediately at startup
    )
    _scheduler.start()
    logger.info(
        "Reminder scheduler started (interval=%s min, lookahead=%s days, cooldown=%s h)",
        REMINDER_CHECK_INTERVAL_MINUTES, REMINDER_LOOKAHEAD_DAYS, REMINDER_COOLDOWN_HOURS,
    )
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
