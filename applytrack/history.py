"""Status history: the date each application's status changed.

``storage.update_application`` records a change in the same transaction as the
new status, so the history and the current status cannot disagree. Applications
saved before history was kept, or imported with a status other than ``applied``,
have a status whose date is unknown; ``timeline`` shows it without a date.

Each change also records the status it replaced, so ``storage.undo_status_change``
can put it back. Changes recorded before that column existed, or restored from a
backup, have no replaced status; the change listed before them shows what it was.
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
            changed_on TEXT NOT NULL,
            previous_status TEXT
                CHECK(previous_status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn'))
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


def lacks_previous_status(connection: sqlite3.Connection) -> bool:
    """Return whether the history table predates recording the status each change replaced."""
    columns = connection.execute("PRAGMA table_info(status_changes)").fetchall()
    return "previous_status" not in {column[1] for column in columns}


def add_previous_status(connection: sqlite3.Connection) -> None:
    """Add the replaced-status column. Changes already recorded keep it NULL."""
    connection.execute(
        "ALTER TABLE status_changes ADD COLUMN previous_status TEXT"
        " CHECK(previous_status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn'))"
    )


def change_dates(connection: sqlite3.Connection, application_id: int) -> tuple[str | None, str | None]:
    """Return the first and last recorded change dates for an application, or ``(None, None)``."""
    row = connection.execute(
        "SELECT min(changed_on), max(changed_on) FROM status_changes WHERE application_id = ?",
        (application_id,),
    ).fetchone()
    return row[0], row[1]


def record_change(
    connection: sqlite3.Connection, application_id: int, status: str, changed_on: str, previous_status: str
) -> None:
    """Add one validated change from ``previous_status``. The caller provides the transaction."""
    connection.execute(
        "INSERT INTO status_changes (application_id, status, changed_on, previous_status) VALUES (?, ?, ?, ?)",
        (application_id, status, changed_on, previous_status),
    )


def delete_changes(connection: sqlite3.Connection, application_id: int) -> None:
    """Remove an application's history. The caller provides the transaction."""
    connection.execute("DELETE FROM status_changes WHERE application_id = ?", (application_id,))


def latest_changes(connection: sqlite3.Connection, application_id: int) -> list[sqlite3.Row]:
    """Return an application's last two recorded changes, newest first."""
    return connection.execute(
        "SELECT id, status, changed_on, previous_status FROM status_changes WHERE application_id = ?"
        " ORDER BY changed_on DESC, id DESC LIMIT 2",
        (application_id,),
    ).fetchall()


def remove_change(connection: sqlite3.Connection, change_id: int) -> None:
    """Remove one recorded change by its row ID. The caller provides the transaction."""
    connection.execute("DELETE FROM status_changes WHERE id = ?", (change_id,))


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
