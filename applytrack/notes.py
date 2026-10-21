"""Notes: dated free-text entries about an application, such as a recruiter call.

``storage.add_note`` validates a note and saves it only if its application
exists, and ``storage.delete_application`` removes an application's notes in the
same transaction as the application.
"""

import sqlite3


def create_table(connection: sqlite3.Connection) -> None:
    """Create the notes table and its index if they do not exist."""
    # Like status_changes, rows are removed with their application rather than by
    # a foreign key, which SQLite leaves disabled unless each connection enables it.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            noted_on TEXT NOT NULL,
            body TEXT NOT NULL CHECK(length(trim(body)) > 0)
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS notes_by_application ON notes (application_id, noted_on)")


def lacks_table(connection: sqlite3.Connection) -> bool:
    """Return whether the database predates notes."""
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'notes'").fetchone()
    return row is None


def record_note(connection: sqlite3.Connection, application_id: int, noted_on: str, body: str) -> int | None:
    """Add one validated note if the application exists and return its ID, or None.

    The caller provides the transaction.
    """
    cursor = connection.execute(
        """INSERT INTO notes (application_id, noted_on, body)
           SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM applications WHERE id = ?)""",
        (application_id, noted_on, body, application_id),
    )
    return cursor.lastrowid if cursor.rowcount else None


def delete_notes(connection: sqlite3.Connection, application_id: int) -> None:
    """Remove an application's notes. The caller provides the transaction."""
    connection.execute("DELETE FROM notes WHERE application_id = ?", (application_id,))


def application_notes(connection: sqlite3.Connection, application_id: int) -> list[sqlite3.Row]:
    """Return an application's notes, oldest first, in the order entered within a day."""
    return connection.execute(
        "SELECT id, noted_on, body FROM notes WHERE application_id = ? ORDER BY noted_on, id",
        (application_id,),
    ).fetchall()
