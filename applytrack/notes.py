"""Notes: dated free-text entries about an application, such as a recruiter call.

``storage.add_note`` validates a note and saves it only if its application
exists, and ``storage.delete_application`` removes an application's notes in the
same transaction as the application. ``edit_note`` and ``delete_note`` change one
note by the ID that ``notes ID`` shows.
"""

from collections import defaultdict
import sqlite3

from .validation import nonempty, past_date

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


def get_note(connection: sqlite3.Connection, note_id: int) -> sqlite3.Row:
    """Return one note with its application ID, or raise ValueError if it does not exist."""
    row = connection.execute(
        "SELECT id, application_id, noted_on, body FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Note {note_id} does not exist.")
    return row


def edit_note(
    connection: sqlite3.Connection, note_id: int, body: str | None = None, noted_on: str | None = None
) -> None:
    """Replace a note's text, its date, or both, keeping its ID and application.

    The new values follow the rules of ``storage.add_note``: ``body`` is trimmed
    and must not be blank, and ``noted_on`` cannot be in the future. Raises
    ValueError if nothing is requested or the note does not exist.
    """
    if body is None and noted_on is None:
        raise ValueError("Choose new note text, a new note date, or both.")
    fields = []
    values = []
    if body is not None:
        fields.append("body = ?")
        values.append(nonempty(body, "Note"))
    if noted_on is not None:
        fields.append("noted_on = ?")
        values.append(past_date(noted_on, "Note date"))
    with connection:
        cursor = connection.execute(f"UPDATE notes SET {', '.join(fields)} WHERE id = ?", (*values, note_id))
    if cursor.rowcount == 0:
        raise ValueError(f"Note {note_id} does not exist.")


def delete_note(connection: sqlite3.Connection, note_id: int) -> None:
    """Permanently remove one note, leaving its application and other notes as they are.

    Raises ValueError if the note does not exist. Note IDs, like application IDs,
    are never given to another note.
    """
    with connection:
        cursor = connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    if cursor.rowcount == 0:
        raise ValueError(f"Note {note_id} does not exist.")
