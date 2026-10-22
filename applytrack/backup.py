"""JSON backups that round-trip a whole database.

A backup holds every application with its ID, archive date, status history, and
notes, plus the highest ID ever given out, so a restored database keeps the
promise that IDs are never reused. ``backup_check.read_backup`` checks every
value before ``restore_backup`` writes anything, and a restore only goes into a
database with no saved records, so it never mixes with or overwrites existing
applications.
"""

from collections import defaultdict
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import TextIO

from .backup_check import APPLICATION_FIELDS, FORMAT, VERSION, Counts, Record


def _grouped(connection: sqlite3.Connection, query: str, fields: tuple[str, ...]) -> dict[int, list[dict]]:
    groups = defaultdict(list)
    for row in connection.execute(query):
        groups[row["application_id"]].append({field: row[field] for field in fields})
    return groups


def snapshot(connection: sqlite3.Connection) -> dict:
    """Return every saved record as JSON-ready data, read in one transaction."""
    connection.execute("BEGIN")
    try:
        applications = connection.execute("SELECT * FROM applications ORDER BY id").fetchall()
        changes = _grouped(connection, "SELECT * FROM status_changes ORDER BY application_id, changed_on, id",
                           ("status", "changed_on"))
        notes = _grouped(connection, "SELECT * FROM notes ORDER BY application_id, noted_on, id",
                         ("noted_on", "body"))
        sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name = 'applications'").fetchone()
    finally:
        connection.rollback()
    return {
        "format": FORMAT,
        "version": VERSION,
        "last_id": max(sequence[0] if sequence else 0, *(row["id"] for row in applications), 0),
        "applications": [
            {**{field: row[field] for field in APPLICATION_FIELDS},
             "status_history": changes[row["id"]], "notes": notes[row["id"]]}
            for row in applications
        ],
    }


def counts(data: dict) -> Counts:
    """Return the number of applications, status changes, and notes in backup data."""
    applications = data["applications"]
    return Counts(len(applications), sum(len(item["status_history"]) for item in applications),
                  sum(len(item["notes"]) for item in applications))


def save_backup(connection: sqlite3.Connection, path: Path, *, replace: bool = False) -> Counts:
    """Write a backup to ``path`` and return what it holds.

    The file is written beside ``path`` and then moved into place, so an
    interrupted backup never leaves a partial file. Raises ValueError if ``path``
    exists and ``replace`` is false.
    """
    if path.exists() and not replace:
        raise ValueError(f"{path} already exists. Choose another file or pass --force to replace it.")
    data = snapshot(connection)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            write_backup(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return counts(data)


def write_backup(data: dict, output: TextIO) -> None:
    """Write backup data from ``snapshot`` as indented JSON."""
    json.dump(data, output, ensure_ascii=False, indent=2)
    output.write("\n")


def restore_backup(
    connection: sqlite3.Connection, records: list[Record], last_id: int, *, dry_run: bool = False
) -> None:
    """Save checked records from ``read_backup`` in one transaction.

    Raises ValueError, saving nothing, if the database already has applications,
    status history, or notes. With ``dry_run``, only that check runs.
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        saved = connection.execute("SELECT count(*) FROM applications").fetchone()[0]
        others = connection.execute("SELECT (SELECT count(*) FROM status_changes) + (SELECT count(*) FROM notes)")
        if saved or others.fetchone()[0]:
            held = f"{saved} application{'' if saved == 1 else 's'}" if saved else "status history or notes"
            raise ValueError(f"Restore needs an empty database, but this one has {held}. "
                             "Use --database to restore into a new file.")
        if dry_run:
            return
        connection.executemany(f"INSERT INTO applications ({', '.join(APPLICATION_FIELDS)}) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?)", [record.row for record in records])
        connection.executemany(
            "INSERT INTO status_changes (application_id, status, changed_on) VALUES (?, ?, ?)",
            [(record.row[0], *change) for record in records for change in record.status_changes],
        )
        connection.executemany(
            "INSERT INTO notes (application_id, noted_on, body) VALUES (?, ?, ?)",
            [(record.row[0], *note) for record in records for note in record.notes],
        )
        # Keep the highest ID ever used, so IDs deleted before the backup stay retired.
        if connection.execute("UPDATE sqlite_sequence SET seq = max(seq, ?) WHERE name = 'applications'",
                              (last_id,)).rowcount == 0:
            connection.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('applications', ?)", (last_id,))
