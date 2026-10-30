"""CSV export and import, compatible with each other and with spreadsheet programs."""

import csv
import sqlite3
from typing import TextIO

from .storage import insert_applications, saved_keys
from .validation import check_follow_up, check_status, nonempty, submission_date, validate_date

EXPORT_COLUMNS = ("id", "company", "role", "status", "applied_on", "follow_up_on")
IMPORT_COLUMNS = ("company", "role", "status", "applied_on", "follow_up_on")


def _spreadsheet_text(value: str) -> str:
    # A leading apostrophe stops spreadsheets from running text as a formula.
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export_csv(rows: list[sqlite3.Row], output: TextIO) -> None:
    """Write ``rows`` to ``output`` as CSV with an EXPORT_COLUMNS header.

    Company and role text that a spreadsheet would treat as a formula is
    prefixed with an apostrophe; ``import_csv`` removes it again.
    """
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([
            _spreadsheet_text(row[column]) if column in ("company", "role") else row[column]
            for column in EXPORT_COLUMNS
        ])


def _unescape_spreadsheet_text(value: str) -> str:
    if value.startswith("'") and _spreadsheet_text(value[1:]) != value[1:]:
        return value[1:]
    return value


def _import_row(row: dict) -> tuple:
    if None in row:
        raise ValueError("Row has more values than columns.")
    values = {column: (row.get(column) or "").strip() for column in IMPORT_COLUMNS}
    status = check_status(values["status"] or "applied")
    applied_on = submission_date(values["applied_on"] or None)
    follow_up_on = validate_date(values["follow_up_on"]) if values["follow_up_on"] else None
    return (
        _unescape_spreadsheet_text(nonempty(values["company"], "Company")),
        _unescape_spreadsheet_text(nonempty(values["role"], "Role")),
        status,
        applied_on,
        check_follow_up(follow_up_on, applied_on),
    )


def import_csv(
    connection: sqlite3.Connection, source: TextIO, *, dry_run: bool = False
) -> tuple[int, int]:
    """Add the applications in a CSV file and return ``(added, skipped)``.

    The header must name ``company`` and ``role``; other IMPORT_COLUMNS and an
    ignored ``id`` column are optional. Every row is validated before anything is
    saved, and an invalid row raises ValueError naming its spreadsheet row number.
    Rows matching a saved application's company, role, and submission date are
    skipped. With ``dry_run``, nothing is saved and the counts are what would be.
    """
    reader = csv.DictReader(source)
    if reader.fieldnames is None:
        raise ValueError("CSV file has no header row.")
    reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]
    unknown = [name for name in reader.fieldnames if name not in (*IMPORT_COLUMNS, "id")]
    if unknown:
        raise ValueError(f"Unknown CSV column: {unknown[0]}.")
    missing = [name for name in ("company", "role") if name not in reader.fieldnames]
    if missing:
        raise ValueError(f"CSV file needs a {missing[0]} column.")
    records = []
    for number, row in enumerate(reader, start=2):
        try:
            records.append(_import_row(row))
        except ValueError as error:
            raise ValueError(f"Row {number}: {error}") from error
    existing = saved_keys(connection)
    new_records = [record for record in records if (record[0], record[1], record[3]) not in existing]
    skipped = len(records) - len(new_records)
    if not dry_run:
        insert_applications(connection, new_records)
    return len(new_records), skipped
