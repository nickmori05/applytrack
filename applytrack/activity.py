"""Quiet applications: open applications with nothing recorded for a while.

An application's last activity is the latest of its submission date, its last
recorded status change, and its last note. ``quiet_applications`` finds the
active applications still waiting on an outcome whose last activity is at least
a given number of days old, which are the ones worth chasing or closing.
"""

from datetime import date, timedelta
import sqlite3
from typing import NamedTuple

from .validation import CLOSED_STATUSES

DEFAULT_DAYS = 14
MAX_DAYS = 3650

# The latest status change and note of each application, read with the
# applications so one query covers every row. Both use their table's index.
_QUERY = f"""SELECT applications.*,
    (SELECT changed_on FROM status_changes WHERE application_id = applications.id
     ORDER BY changed_on DESC, id DESC LIMIT 1) AS last_change_on,
    (SELECT status FROM status_changes WHERE application_id = applications.id
     ORDER BY changed_on DESC, id DESC LIMIT 1) AS last_change_status,
    (SELECT max(noted_on) FROM notes WHERE application_id = applications.id) AS last_note_on
FROM applications
WHERE archived_on IS NULL AND status NOT IN ({", ".join("?" for _ in CLOSED_STATUSES)})"""


class Quiet(NamedTuple):
    """An application, the day of its last activity, and what that activity was.

    ``activity`` is ``submitted``, ``note``, or ``became STATUS`` for a status change.
    """

    application: sqlite3.Row
    last_active_on: str
    activity: str
    days: int


def _last_activity(row: sqlite3.Row) -> tuple[str, str]:
    # On a day with several kinds of activity, a status change is named before a
    # note, and a note before the submission, since that is the most informative.
    candidates = [(row["applied_on"], 0, "submitted")]
    if row["last_note_on"] is not None:
        candidates.append((row["last_note_on"], 1, "note"))
    if row["last_change_on"] is not None:
        candidates.append((row["last_change_on"], 2, f"became {row['last_change_status']}"))
    last_active_on, _, activity = max(candidates)
    return last_active_on, activity


def check_days(days: int) -> int:
    """Return ``days`` if it is a whole number from 0 to ``MAX_DAYS``, or raise ValueError."""
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_DAYS:
        raise ValueError(f"Days must be a whole number from 0 to {MAX_DAYS}.")
    return days


def quiet_applications(
    connection: sqlite3.Connection, days: int = DEFAULT_DAYS, today: date | None = None
) -> list[Quiet]:
    """Return open applications whose last activity is at least ``days`` days before ``today``.

    Archived applications and those that are ``rejected`` or ``withdrawn`` are left
    out; ``applied``, ``interviewing``, and ``offer`` are included whatever their
    follow-up date. ``today`` defaults to the local date. The quietest come first,
    then by ID.
    """
    check_days(days)
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    quiet = []
    for row in connection.execute(_QUERY, CLOSED_STATUSES):
        last_active_on, activity = _last_activity(row)
        if last_active_on <= cutoff:
            waited = (today - date.fromisoformat(last_active_on)).days
            quiet.append(Quiet(row, last_active_on, activity, waited))
    quiet.sort(key=lambda item: (item.last_active_on, item.application["id"]))
    return quiet
