"""SQLite storage for applications: schema, upgrades, and record operations.

Every function takes an open connection from ``connect``. Writes run inside a
transaction, and invalid input raises ValueError before anything is saved.
"""

from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
import sqlite3

from . import history, notes
from .validation import (
    CLOSED_STATUSES,
    check_follow_up,
    check_status,
    nonempty,
    past_date,
    submission_date,
    validate_date,
)


def _create_table(connection: sqlite3.Connection, name: str) -> None:
    # AUTOINCREMENT keeps SQLite from handing out the ID of a deleted application again.
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL CHECK(length(trim(company)) > 0),
            role TEXT NOT NULL CHECK(length(trim(role)) > 0),
            status TEXT NOT NULL DEFAULT 'applied'
                CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
            applied_on TEXT NOT NULL,
            follow_up_on TEXT,
            archived_on TEXT
        )"""
    )


def _reuses_ids(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'applications'"
    ).fetchone()
    return "AUTOINCREMENT" not in row["sql"].upper()


def _migrate_ids(connection: sqlite3.Connection) -> None:
    # Databases created before IDs were kept unique are copied into the current table.
    _create_table(connection, "applications_migrated")
    connection.execute(
        """INSERT INTO applications_migrated
               (id, company, role, status, applied_on, follow_up_on)
           SELECT id, company, role, status, applied_on, follow_up_on FROM applications"""
    )
    connection.execute("DROP TABLE applications")
    connection.execute("ALTER TABLE applications_migrated RENAME TO applications")


def _lacks_archive(connection: sqlite3.Connection) -> bool:
    columns = connection.execute("PRAGMA table_info(applications)").fetchall()
    return "archived_on" not in {column["name"] for column in columns}


def _add_archive(connection: sqlite3.Connection) -> None:
    # Existing applications start out active, so the new column is left NULL.
    connection.execute("ALTER TABLE applications ADD COLUMN archived_on TEXT")


# Applied in order. The history and notes tables are created here rather than
# with the applications table, so a database from before IDs were kept unique is
# rebuilt before anything else exists in it.
UPGRADES = (
    (_reuses_ids, _migrate_ids),
    (_lacks_archive, _add_archive),
    (history.lacks_table, history.create_table),
    (notes.lacks_table, notes.create_table),
)


def _upgrade(
    connection: sqlite3.Connection,
    needed: Callable[[sqlite3.Connection], bool],
    apply: Callable[[sqlite3.Connection], None],
) -> None:
    # Each upgrade runs in one transaction, so a failure leaves the database as it
    # was. The check is repeated after taking the write lock, so two processes
    # opening the same old database do not both apply it.
    connection.execute("BEGIN IMMEDIATE")
    try:
        if needed(connection):
            apply(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def connect(database: Path) -> sqlite3.Connection:
    """Open ``database``, creating it and its folder if needed.

    Databases created by earlier versions are upgraded in place first. If an
    upgrade fails, the connection is closed and the error is raised with the
    database left as it was. Rows are returned as ``sqlite3.Row`` objects.
    """
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.create_function("casefold", 1, str.casefold, deterministic=True)
    _create_table(connection, "applications")
    connection.commit()
    for needed, apply in UPGRADES:
        if needed(connection):
            try:
                _upgrade(connection, needed, apply)
            except BaseException:
                connection.close()
                raise
    return connection


def add_application(
    connection: sqlite3.Connection,
    company: str,
    role: str,
    follow_up_on: str | None = None,
    *,
    applied_on: str | None = None,
) -> int:
    """Save a new application and return its ID.

    ``applied_on`` defaults to today and cannot be in the future, and
    ``follow_up_on`` cannot be before it. Dates use ``YYYY-MM-DD``; company and
    role are trimmed and must not be blank.
    """
    company = nonempty(company, "Company")
    role = nonempty(role, "Role")
    applied_on = submission_date(applied_on)
    if follow_up_on is not None:
        follow_up_on = check_follow_up(validate_date(follow_up_on), applied_on)
    with connection:
        cursor = connection.execute(
            """INSERT INTO applications (company, role, applied_on, follow_up_on)
               VALUES (?, ?, ?, ?)""",
            (company, role, applied_on, follow_up_on),
        )
    return cursor.lastrowid


def list_applications(
    connection: sqlite3.Connection,
    due_only: bool = False,
    today: date | None = None,
    *,
    status: str | None = None,
    search: str | None = None,
    archived: bool = False,
) -> list[sqlite3.Row]:
    """Return applications matching every given filter.

    Active applications are returned unless ``archived`` is true, in which case
    only archived ones are. ``due_only`` keeps follow-ups on or before ``today``
    (default: the local date) that are not rejected or withdrawn, ordered by
    follow-up date; otherwise the newest application comes first. ``search``
    matches company, role, or note text, ignoring case.
    """
    if status is not None:
        check_status(status)
    clauses = ["archived_on IS NOT NULL" if archived else "archived_on IS NULL"]
    values = []
    if search is not None:
        search = nonempty(search, "Search").casefold()
        clauses.append(f"(instr(casefold(company), ?) > 0 OR instr(casefold(role), ?) > 0 OR {notes.MATCHES})")
        values.extend((search, search, search))
    if due_only:
        clauses.append("follow_up_on <= ? AND status NOT IN (?, ?)")
        values.extend(((today or date.today()).isoformat(), *CLOSED_STATUSES))
    if status is not None:
        clauses.append("status = ?")
        values.append(status)
    where = " WHERE " + " AND ".join(clauses)
    order = " ORDER BY follow_up_on, id" if due_only else " ORDER BY id DESC"
    return connection.execute(
        "SELECT * FROM applications" + where + order, values
    ).fetchall()


def update_application(
    connection: sqlite3.Connection,
    application_id: int,
    status: str | None = None,
    follow_up_on: str | None = None,
    clear_follow_up: bool = False,
    *,
    company: str | None = None,
    role: str | None = None,
    applied_on: str | None = None,
    changed_on: str | None = None,
    today: date | None = None,
) -> None:
    """Change the given fields of one application, leaving the rest as they are.

    A new status is added to the application's history, dated ``changed_on`` or
    ``today`` (default: the local date). That date cannot be in the future,
    before the submission date, or before the last recorded change, and the
    submission date cannot move after the first recorded change. When either the
    submission or follow-up date changes, the follow-up cannot end up before the
    submission date; other changes leave dates saved before that rule alone.

    Every value is validated before anything is written, so an invalid change
    rejects the whole update. Raises ValueError if nothing is requested, if a
    follow-up is both set and cleared, or if the application does not exist.
    """
    if changed_on is not None and status is None:
        raise ValueError("A status change date needs a new status.")
    changes = (status, follow_up_on, company, role, applied_on)
    if all(value is None for value in changes) and not clear_follow_up:
        raise ValueError(
            "Choose a company, role, status, submission date, follow-up date, or --clear-follow-up."
        )
    if company is not None:
        company = nonempty(company, "Company")
    if role is not None:
        role = nonempty(role, "Role")
    if status is not None:
        check_status(status)
    if follow_up_on is not None and clear_follow_up:
        raise ValueError("Set a follow-up date or clear it, but not both.")
    if follow_up_on is not None:
        follow_up_on = validate_date(follow_up_on)
    if applied_on is not None:
        applied_on = submission_date(applied_on)
    if changed_on is not None:
        changed_on = past_date(changed_on, "Status change date")
    fields = []
    values = []
    if company is not None:
        fields.append("company = ?")
        values.append(company)
    if role is not None:
        fields.append("role = ?")
        values.append(role)
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if applied_on is not None:
        fields.append("applied_on = ?")
        values.append(applied_on)
    if follow_up_on is not None or clear_follow_up:
        fields.append("follow_up_on = ?")
        values.append(follow_up_on)
    values.append(application_id)
    with connection:
        # Take the write lock before reading, so the checks still hold when writing.
        connection.execute("BEGIN IMMEDIATE")
        _check_follow_up_change(connection, application_id, applied_on, follow_up_on, clear_follow_up)
        changed_on = _status_change_date(connection, application_id, status, applied_on, changed_on, today)
        connection.execute(f"UPDATE applications SET {', '.join(fields)} WHERE id = ?", values)
        if changed_on is not None:
            history.record_change(connection, application_id, status, changed_on)


def _check_follow_up_change(
    connection: sqlite3.Connection,
    application_id: int,
    applied_on: str | None,
    follow_up_on: str | None,
    clear_follow_up: bool,
) -> None:
    # Only a change to either date is checked, so an application saved before the
    # follow-up rule existed can still have its status or names updated.
    if applied_on is None and follow_up_on is None:
        return
    current = get_application(connection, application_id)
    if follow_up_on is not None:
        check_follow_up(follow_up_on, applied_on or current["applied_on"])
    elif not clear_follow_up and current["follow_up_on"] is not None and applied_on > current["follow_up_on"]:
        raise ValueError(f"Submission date cannot be after the follow-up date ({current['follow_up_on']}). "
                         "Change or clear the follow-up as well.")


def _status_change_date(
    connection: sqlite3.Connection,
    application_id: int,
    status: str | None,
    applied_on: str | None,
    changed_on: str | None,
    today: date | None,
) -> str | None:
    # Return the date to record for a new status, or None if the status is not
    # changing, after checking the update against the recorded history.
    current = get_application(connection, application_id)
    first_change, last_change = history.change_dates(connection, application_id)
    if applied_on is not None and first_change is not None and applied_on > first_change:
        raise ValueError(f"Submission date cannot be after the first recorded status change ({first_change}).")
    if status is None or status == current["status"]:
        if changed_on is not None:
            raise ValueError(f"Application {application_id} already has status {status}; there is no change to date.")
        return None
    changed_on = changed_on or (today or date.today()).isoformat()
    if changed_on < (applied_on or current["applied_on"]):
        raise ValueError("Status change date cannot be before the submission date.")
    if last_change is not None and changed_on < last_change:
        raise ValueError(f"Status change date cannot be before the last recorded change ({last_change}).")
    return changed_on


def get_application(connection: sqlite3.Connection, application_id: int) -> sqlite3.Row:
    """Return one application, archived or not, or raise ValueError if it does not exist."""
    row = connection.execute(
        "SELECT * FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Application {application_id} does not exist.")
    return row


def archive_application(
    connection: sqlite3.Connection, application_id: int, today: date | None = None
) -> None:
    """Hide an application from default lists, recording ``today`` as its archive date.

    Raises ValueError if the application does not exist or is already archived.
    """
    with connection:
        cursor = connection.execute(
            "UPDATE applications SET archived_on = ? WHERE id = ? AND archived_on IS NULL",
            ((today or date.today()).isoformat(), application_id),
        )
        if cursor.rowcount == 0:
            get_application(connection, application_id)
            raise ValueError(f"Application {application_id} is already archived.")


def restore_application(connection: sqlite3.Connection, application_id: int) -> None:
    """Make an archived application active again.

    Raises ValueError if the application does not exist or is not archived.
    """
    with connection:
        cursor = connection.execute(
            "UPDATE applications SET archived_on = NULL WHERE id = ? AND archived_on IS NOT NULL",
            (application_id,),
        )
        if cursor.rowcount == 0:
            get_application(connection, application_id)
            raise ValueError(f"Application {application_id} is not archived.")


def archived_count(connection: sqlite3.Connection) -> int:
    """Return how many applications are archived."""
    return connection.execute(
        "SELECT count(*) FROM applications WHERE archived_on IS NOT NULL"
    ).fetchone()[0]


def add_note(
    connection: sqlite3.Connection, application_id: int, body: str, noted_on: str | None = None
) -> int:
    """Save a note about an application, archived or not, and return the note's ID.

    ``body`` is trimmed and must not be blank. ``noted_on`` defaults to today and
    cannot be in the future, but may be before the submission date. Raises
    ValueError if the application does not exist.
    """
    body = nonempty(body, "Note")
    noted_on = date.today().isoformat() if noted_on is None else past_date(noted_on, "Note date")
    with connection:
        note_id = notes.record_note(connection, application_id, noted_on, body)
    if note_id is None:
        raise ValueError(f"Application {application_id} does not exist.")
    return note_id


def find_note(connection: sqlite3.Connection, application_id: int, number: int) -> sqlite3.Row:
    """Return note ``number`` of an application, counting from 1 in the order ``notes ID`` shows.

    Raises ValueError if the application or the note does not exist.
    """
    note = notes.numbered_note(connection, application_id, number)
    if note is None:
        get_application(connection, application_id)
        raise ValueError(f"Application {application_id} has no note {number}.")
    return note


def edit_note(
    connection: sqlite3.Connection,
    application_id: int,
    number: int,
    body: str | None = None,
    noted_on: str | None = None,
) -> int:
    """Replace the text, the date, or both of an application's note ``number``.

    The values follow the rules of ``add_note``. Notes are ordered by date, so a
    new date can move the note; its number afterwards is returned. Raises
    ValueError if nothing is requested, a value is invalid, or the note does not exist.
    """
    if body is None and noted_on is None:
        raise ValueError("Choose new note text, a new date, or both.")
    if body is not None:
        body = nonempty(body, "Note")
    if noted_on is not None:
        noted_on = past_date(noted_on, "Note date")
    with connection:
        # Take the write lock before finding the note, so the number still refers to it.
        connection.execute("BEGIN IMMEDIATE")
        note = find_note(connection, application_id, number)
        connection.execute(
            "UPDATE notes SET body = coalesce(?, body), noted_on = coalesce(?, noted_on) WHERE id = ?",
            (body, noted_on, note["id"]),
        )
        return notes.note_number(connection, application_id, note["id"])


def delete_note(
    connection: sqlite3.Connection, application_id: int, number: int, *, expected_id: int | None = None
) -> None:
    """Permanently remove an application's note ``number``; later notes move up one number.

    ``expected_id`` is the row ID of the note the caller showed for confirmation,
    from ``find_note``. If the number now refers to a different note, for example
    because another command added one, nothing is deleted. Raises ValueError if the
    note does not exist or has changed.
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        note = find_note(connection, application_id, number)
        if expected_id is not None and note["id"] != expected_id:
            raise ValueError(f"Note {number} of application {application_id} changed; run notes and try again.")
        connection.execute("DELETE FROM notes WHERE id = ?", (note["id"],))


def delete_application(connection: sqlite3.Connection, application_id: int) -> None:
    """Permanently remove one application, its status history, and its notes.

    Raises ValueError if the application does not exist.
    """
    with connection:
        history.delete_changes(connection, application_id)
        notes.delete_notes(connection, application_id)
        cursor = connection.execute("DELETE FROM applications WHERE id = ?", (application_id,))
        if cursor.rowcount == 0:
            raise ValueError(f"Application {application_id} does not exist.")


def saved_keys(connection: sqlite3.Connection) -> set[tuple[str, str, str]]:
    """Return ``(company, role, applied_on)`` for every saved application, archived or not."""
    rows = connection.execute("SELECT company, role, applied_on FROM applications")
    return {tuple(row) for row in rows}


def insert_applications(
    connection: sqlite3.Connection, records: Iterable[tuple[str, str, str, str, str | None]]
) -> None:
    """Save ``(company, role, status, applied_on, follow_up_on)`` records in one transaction.

    Records must already be validated, as ``csv_io.import_csv`` does.
    """
    with connection:
        connection.executemany(
            """INSERT INTO applications (company, role, status, applied_on, follow_up_on)
               VALUES (?, ?, ?, ?, ?)""",
            records,
        )
