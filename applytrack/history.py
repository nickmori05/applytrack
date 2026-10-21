"""Status history: the date each application's status changed.

``storage.update_application`` records a change in the same transaction as the
new status, so the history and the current status cannot disagree. Applications
saved before history was kept, or imported with a status other than ``applied``,
have a status whose date is unknown; ``timeline`` shows it without a date.
"""

import sqlite3

# A status before any recorded change, or since an unrecorded one, has no date.
Event = tuple[str | None, str]


def create_table(connection: sqlite3.Connection) -> None:
    """Create the status history table and its index if they do not exist."""
    # Rows are removed by storage.delete_application in the same transaction as
    # their application, so the table does not rely on SQLite foreign keys.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS status_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
            changed_on TEXT NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS status_changes_by_application ON status_changes (application_id, changed_on)"
    )


def lacks_table(connection: sqlite3.Connection) -> bool:
    """Return whether the database predates status history."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'status_changes'"
    ).fetchone()
    return row is None


def change_dates(connection: sqlite3.Connection, application_id: int) -> tuple[str | None, str | None]:
    """Return the first and last recorded change dates for an application, or ``(None, None)``."""
    row = connection.execute(
        "SELECT min(changed_on), max(changed_on) FROM status_changes WHERE application_id = ?",
        (application_id,),
    ).fetchone()
    return row[0], row[1]


def record_change(connection: sqlite3.Connection, application_id: int, status: str, changed_on: str) -> None:
    """Add one validated change. The caller provides the transaction."""
    connection.execute(
        "INSERT INTO status_changes (application_id, status, changed_on) VALUES (?, ?, ?)",
        (application_id, status, changed_on),
    )


def delete_changes(connection: sqlite3.Connection, application_id: int) -> None:
    """Remove an application's history. The caller provides the transaction."""
    connection.execute("DELETE FROM status_changes WHERE application_id = ?", (application_id,))


def status_changes(connection: sqlite3.Connection, application_id: int) -> list[sqlite3.Row]:
    """Return an application's recorded changes, oldest first."""
    return connection.execute(
        "SELECT status, changed_on FROM status_changes WHERE application_id = ? ORDER BY changed_on, id",
        (application_id,),
    ).fetchall()


def timeline(application: sqlite3.Row, changes: list[sqlite3.Row]) -> list[Event]:
    """Return ``(date, status)`` events for an application, starting with its submission.

    If the current status was set before history was kept, or by an import, it
    follows the submission with a date of ``None``.
    """
    events: list[Event] = [(application["applied_on"], "applied")]
    events.extend((change["changed_on"], change["status"]) for change in changes)
    if events[-1][1] != application["status"]:
        events.append((None, application["status"]))
    return events
