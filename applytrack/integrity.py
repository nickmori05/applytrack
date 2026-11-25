"""Checks that a database is consistent, without changing it.

``check_database`` reports problems the other commands cannot create: a blank
name or note, a status that is not one of the five, a date that is not a real
day, a status change dated before the submission or before a change entered
earlier, or a status change or note whose application is gone. It also runs
SQLite's own integrity check. A follow-up from before it had to fall on or
after the submission date is kept, and so is a status with no history, which is
how an import or an older database looks.
"""

import sqlite3
from typing import NamedTuple

from .storage import schema_version
from .validation import STATUSES, validate_date

_STATUSES = ", ".join(STATUSES)


class Report(NamedTuple):
    """The schema version, row counts, and problems found by ``check_database``."""

    version: int
    applications: int
    status_changes: int
    notes: int
    problems: tuple[str, ...]


def check_lines(report: Report) -> list[str]:
    """Return the lines ``check`` prints for a report."""
    lines = [
        f"Schema version {report.version}.",
        (
            f"Applications: {report.applications}. Status changes: {report.status_changes}. "
            f"Notes: {report.notes}."
        ),
    ]
    lines.extend(report.problems)
    count = len(report.problems)
    if count == 0:
        lines.append("No problems found.")
    else:
        lines.append(f"{count} problem{'' if count == 1 else 's'} found.")
    return lines


def check_database(connection: sqlite3.Connection) -> Report:
    """Return problems in ``connection`` and leave it unchanged.

    The read runs in its own transaction when one is not already open, so the
    counts and the problems describe the same moment. Archived applications are
    included.
    """
    owns_transaction = not connection.in_transaction
    try:
        if owns_transaction:
            connection.execute("BEGIN")
        return _report(connection)
    finally:
        if owns_transaction and connection.in_transaction:
            connection.rollback()


def _report(connection: sqlite3.Connection) -> Report:
    applications = connection.execute(
        "SELECT id, company, role, status, applied_on, follow_up_on, archived_on"
        " FROM applications ORDER BY id"
    ).fetchall()
    changes = connection.execute(
        "SELECT id, application_id, status, changed_on FROM status_changes ORDER BY id"
    ).fetchall()
    notes = connection.execute(
        "SELECT id, application_id, noted_on, body FROM notes ORDER BY id"
    ).fetchall()
    by_id = {row["id"]: row for row in applications}
    problems = _integrity_problems(connection)
    for application in applications:
        problems.extend(_application_problems(application))
    previous: dict[int, tuple[int, str]] = {}
    for change in changes:
        problems.extend(_change_problems(change, by_id, previous))
    for note in notes:
        problems.extend(_note_problems(note, by_id))
    return Report(schema_version(connection), len(applications), len(changes), len(notes), tuple(problems))


def _integrity_problems(connection: sqlite3.Connection) -> list[str]:
    rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if rows == ["ok"]:
        return []
    if not rows:
        return ["SQLite integrity check returned no result."]
    return [f"SQLite integrity check: {message}" for message in rows]


def _application_problems(application: sqlite3.Row) -> list[str]:
    application_id = application["id"]
    problems = []
    if _blank(application["company"]):
        problems.append(f"Application {application_id} has a blank company.")
    if _blank(application["role"]):
        problems.append(f"Application {application_id} has a blank role.")
    if application["status"] not in STATUSES:
        problems.append(
            f"Application {application_id} has status {application['status']}, which is not one of: {_STATUSES}."
        )
    dates = [("submission", "applied_on")]
    if application["follow_up_on"] is not None:
        dates.append(("follow-up", "follow_up_on"))
    if application["archived_on"] is not None:
        dates.append(("archive", "archived_on"))
    for label, column in dates:
        problem = _date_problem(application_id, label, application[column])
        if problem is not None:
            problems.append(problem)
    return problems


def _date_problem(application_id: int, label: str, value: object) -> str | None:
    if not _invalid_date(value):
        return None
    return f"Application {application_id} has {label} date {_shown(value)}, which is not a valid date."


def _change_problems(
    change: sqlite3.Row,
    applications: dict[int, sqlite3.Row],
    previous: dict[int, tuple[int, str]],
) -> list[str]:
    change_id = change["id"]
    application_id = change["application_id"]
    changed_on = change["changed_on"]
    problems = []
    application = applications.get(application_id)
    if application is None:
        problems.append(f"Status change {change_id} refers to application {application_id}, which does not exist.")
    if change["status"] not in STATUSES:
        problems.append(
            f"Status change {change_id} of application {application_id} has status {change['status']}, "
            f"which is not one of: {_STATUSES}."
        )
    if _invalid_date(changed_on):
        problems.append(
            f"Status change {change_id} of application {application_id} has change date {_shown(changed_on)}, "
            "which is not a valid date."
        )
        return problems
    applied_on = None if application is None else application["applied_on"]
    if applied_on is not None and not _invalid_date(applied_on) and changed_on < applied_on:
        problems.append(
            f"Status change {change_id} of application {application_id} is dated {changed_on}, "
            f"before its submission date ({applied_on})."
        )
    earlier = previous.get(application_id)
    if earlier is not None and changed_on < earlier[1]:
        problems.append(
            f"Status change {change_id} of application {application_id} is dated {changed_on}, "
            f"before status change {earlier[0]} dated {earlier[1]}."
        )
    previous[application_id] = (change_id, changed_on)
    return problems


def _note_problems(note: sqlite3.Row, applications: dict[int, sqlite3.Row]) -> list[str]:
    note_id = note["id"]
    application_id = note["application_id"]
    problems = []
    if application_id not in applications:
        problems.append(f"Note {note_id} refers to application {application_id}, which does not exist.")
    if _invalid_date(note["noted_on"]):
        problems.append(
            f"Note {note_id} of application {application_id} has date {_shown(note['noted_on'])}, "
            "which is not a valid date."
        )
    if _blank(note["body"]):
        problems.append(f"Note {note_id} of application {application_id} is blank.")
    return problems


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _invalid_date(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        validate_date(value)
    except ValueError:
        return True
    return False


def _shown(value: object) -> str:
    return "missing" if value is None else f'"{value}"'
