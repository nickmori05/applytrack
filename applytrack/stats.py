"""A summary of saved applications: statuses, response rates, and time to respond.

Responses are read from each application's status history, so an application
that was interviewed and later rejected or withdrawn still counts as interviewed.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import sqlite3

from .history import timeline
from .validation import STATUSES, validate_date

# Statuses only an employer can set: reaching any of them means they responded.
RESPONSE_STATUSES = ("interviewing", "offer", "rejected")
INTERVIEW_STATUSES = ("interviewing", "offer")


@dataclass(frozen=True)
class Summary:
    """Counts for a set of applications, archived ones included.

    ``response_days`` holds the days from submission to the first response for
    each response whose date is recorded, so it can be shorter than ``responded``.
    """

    total: int
    archived: int
    by_status: dict[str, int]
    responded: int
    interviewed: int
    response_days: tuple[int, ...]


def _changes_by_application(connection: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    changes = defaultdict(list)
    rows = connection.execute(
        "SELECT application_id, status, changed_on FROM status_changes ORDER BY application_id, changed_on, id"
    )
    for row in rows:
        changes[row["application_id"]].append(row)
    return changes


def _submitted_between(connection: sqlite3.Connection, since: str | None, until: str | None) -> list[sqlite3.Row]:
    clauses = []
    values = []
    if since is not None:
        clauses.append("applied_on >= ?")
        values.append(validate_date(since))
    if until is not None:
        clauses.append("applied_on <= ?")
        values.append(validate_date(until))
    if since is not None and until is not None and since > until:
        raise ValueError(f"The start date ({since}) cannot be after the end date ({until}).")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return connection.execute("SELECT * FROM applications" + where, values).fetchall()


def summarize(connection: sqlite3.Connection, since: str | None = None, until: str | None = None) -> Summary:
    """Summarize every application, or those submitted between ``since`` and ``until``.

    Both dates are ``YYYY-MM-DD``, inclusive, and optional. Responses are counted
    whenever they happened, so an application submitted before ``until`` and
    answered after it still counts as answered.
    """
    applications = _submitted_between(connection, since, until)
    changes = _changes_by_application(connection)
    by_status = dict.fromkeys(STATUSES, 0)
    responded = interviewed = 0
    response_days = []
    for application in applications:
        by_status[application["status"]] += 1
        events = timeline(application, changes[application["id"]])
        statuses = {status for _, status in events}
        interviewed += not statuses.isdisjoint(INTERVIEW_STATUSES)
        first_response = next((event for event in events if event[1] in RESPONSE_STATUSES), None)
        if first_response is None:
            continue
        responded += 1
        if first_response[0] is not None:
            waited = date.fromisoformat(first_response[0]) - date.fromisoformat(application["applied_on"])
            response_days.append(waited.days)
    return Summary(
        total=len(applications),
        archived=sum(application["archived_on"] is not None for application in applications),
        by_status=by_status,
        responded=responded,
        interviewed=interviewed,
        response_days=tuple(sorted(response_days)),
    )
