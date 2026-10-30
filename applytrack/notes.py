"""Notes: dated free-text entries about an application, such as a recruiter call.

``storage.add_note`` validates a note and saves it only if its application
exists, and ``storage.delete_application`` removes an application's notes in the
same transaction as the application.

Notes are referred to by number: their position, counting from 1, in the order
``application_notes`` returns them. Numbers are per application, so they read the
same after a backup is restored, where note row IDs are not kept.
"""

from collections import defaultdict
import sqlite3

# An SQL condition, used by storage.list_applications, that is true when one of
# the application's notes contains the casefolded search text bound to it.
MATCHES = (
    "EXISTS (SELECT 1 FROM notes WHERE notes.application_id = applications.id"
    " AND instr(casefold(notes.body), ?) > 0)"
)


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


def numbered_note(connection: sqlite3.Connection, application_id: int, number: int) -> sqlite3.Row | None:
    """Return an application's note ``number``, counting from 1 as ``notes ID`` shows them, or None."""
    if number < 1:
        return None
    return connection.execute(
        "SELECT id, noted_on, body FROM notes WHERE application_id = ? ORDER BY noted_on, id LIMIT 1 OFFSET ?",
        (application_id, number - 1),
    ).fetchone()


def note_number(connection: sqlite3.Connection, application_id: int, note_id: int) -> int:
    """Return the number of the note with row ID ``note_id``, which must belong to the application."""
    return connection.execute(
        """SELECT count(*) FROM notes AS earlier, notes AS this
           WHERE this.id = ? AND earlier.application_id = ?
             AND (earlier.noted_on < this.noted_on OR (earlier.noted_on = this.noted_on AND earlier.id <= this.id))""",
        (note_id, application_id),
    ).fetchone()[0]


def matching_notes(connection: sqlite3.Connection, search: str) -> dict[int, list[sqlite3.Row]]:
    """Return the notes containing ``search``, ignoring case, grouped by application.

    Surrounding whitespace in ``search`` is ignored, as in
    ``storage.list_applications``. Each application's notes are oldest first.
    """
    matches = defaultdict(list)
    rows = connection.execute(
        "SELECT id, application_id, noted_on, body FROM notes WHERE instr(casefold(body), ?) > 0"
        " ORDER BY application_id, noted_on, id",
        (search.strip().casefold(),),
    )
    for row in rows:
        matches[row["application_id"]].append(row)
    return dict(matches)
