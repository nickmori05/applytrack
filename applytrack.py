import argparse
from contextlib import closing
import csv
from datetime import date
from pathlib import Path
import sqlite3
import sys
from typing import TextIO


STATUSES = ("applied", "interviewing", "offer", "rejected", "withdrawn")
CLOSED_STATUSES = ("rejected", "withdrawn")
DEFAULT_DATABASE = Path(__file__).resolve().parent / ".local" / "applications.sqlite3"
EXPORT_COLUMNS = ("id", "company", "role", "status", "applied_on", "follow_up_on")
IMPORT_COLUMNS = ("company", "role", "status", "applied_on", "follow_up_on")


def validate_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Use a valid date in YYYY-MM-DD format.") from error
    if parsed.isoformat() != value:
        raise ValueError("Use a valid date in YYYY-MM-DD format.")
    return value


def nonempty(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} cannot be empty.")
    return value


def submission_date(value: str | None) -> str:
    today = date.today().isoformat()
    value = today if value is None else validate_date(value)
    if value > today:
        raise ValueError("Submission date cannot be in the future.")
    return value


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
            follow_up_on TEXT
        )"""
    )


def _reuses_ids(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'applications'"
    ).fetchone()
    return "AUTOINCREMENT" not in row["sql"].upper()


def _migrate_ids(connection: sqlite3.Connection) -> None:
    # Databases created before IDs were kept unique are copied into the current
    # table in one transaction, so a failure leaves the original table in place.
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _reuses_ids(connection):
            _create_table(connection, "applications_migrated")
            connection.execute(
                """INSERT INTO applications_migrated
                       (id, company, role, status, applied_on, follow_up_on)
                   SELECT id, company, role, status, applied_on, follow_up_on FROM applications"""
            )
            connection.execute("DROP TABLE applications")
            connection.execute("ALTER TABLE applications_migrated RENAME TO applications")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.create_function("casefold", 1, str.casefold, deterministic=True)
    _create_table(connection, "applications")
    connection.commit()
    if _reuses_ids(connection):
        try:
            _migrate_ids(connection)
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
    company = nonempty(company, "Company")
    role = nonempty(role, "Role")
    applied_on = submission_date(applied_on)
    if follow_up_on is not None:
        follow_up_on = validate_date(follow_up_on)
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
) -> list[sqlite3.Row]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    clauses = []
    values = []
    if search is not None:
        search = nonempty(search, "Search").casefold()
        clauses.append("(instr(casefold(company), ?) > 0 OR instr(casefold(role), ?) > 0)")
        values.extend((search, search))
    if due_only:
        clauses.append("follow_up_on <= ? AND status NOT IN (?, ?)")
        values.extend(((today or date.today()).isoformat(), *CLOSED_STATUSES))
    if status is not None:
        clauses.append("status = ?")
        values.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order = " ORDER BY follow_up_on, id" if due_only else " ORDER BY id DESC"
    return connection.execute(
        "SELECT * FROM applications" + where + order, values
    ).fetchall()


def _spreadsheet_text(value: str) -> str:
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export_csv(rows: list[sqlite3.Row], output: TextIO) -> None:
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
    status = values["status"] or "applied"
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    return (
        _unescape_spreadsheet_text(nonempty(values["company"], "Company")),
        _unescape_spreadsheet_text(nonempty(values["role"], "Role")),
        status,
        submission_date(values["applied_on"] or None),
        validate_date(values["follow_up_on"]) if values["follow_up_on"] else None,
    )


def import_csv(
    connection: sqlite3.Connection, source: TextIO, *, dry_run: bool = False
) -> tuple[int, int]:
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
    saved = connection.execute("SELECT company, role, applied_on FROM applications")
    existing = {tuple(row) for row in saved}
    new_records = [record for record in records if (record[0], record[1], record[3]) not in existing]
    skipped = len(records) - len(new_records)
    if dry_run:
        return len(new_records), skipped
    with connection:
        connection.executemany(
            """INSERT INTO applications (company, role, status, applied_on, follow_up_on)
               VALUES (?, ?, ?, ?, ?)""",
            new_records,
        )
    return len(new_records), skipped


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
) -> None:
    changes = (status, follow_up_on, company, role, applied_on)
    if all(value is None for value in changes) and not clear_follow_up:
        raise ValueError(
            "Choose a company, role, status, submission date, follow-up date, or --clear-follow-up."
        )
    if company is not None:
        company = nonempty(company, "Company")
    if role is not None:
        role = nonempty(role, "Role")
    if status is not None and status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    if follow_up_on is not None and clear_follow_up:
        raise ValueError("Set a follow-up date or clear it, but not both.")
    if follow_up_on is not None:
        follow_up_on = validate_date(follow_up_on)
    if applied_on is not None:
        applied_on = submission_date(applied_on)
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
        cursor = connection.execute(
            f"UPDATE applications SET {', '.join(fields)} WHERE id = ?", values
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Application {application_id} does not exist.")


def get_application(connection: sqlite3.Connection, application_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Application {application_id} does not exist.")
    return row


def delete_application(connection: sqlite3.Connection, application_id: int) -> None:
    with connection:
        cursor = connection.execute("DELETE FROM applications WHERE id = ?", (application_id,))
        if cursor.rowcount == 0:
            raise ValueError(f"Application {application_id} does not exist.")


def _terminal_text(value: str) -> str:
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value)


def _summary(row: sqlite3.Row) -> str:
    return f"#{row['id']}  {_terminal_text(row['company'])} — {_terminal_text(row['role'])}"


def _confirmed(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def display(rows: list[sqlite3.Row], due_only: bool, filtered: bool = False) -> None:
    if not rows:
        if filtered:
            print("No applications match these filters.")
        else:
            print("No follow-ups due." if due_only else "No applications yet. Add your first with 'add'.")
        return
    for row in rows:
        print(_summary(row))
        print(
            f"    {row['status']} | Applied: {row['applied_on']}"
            f" | Follow up: {row['follow_up_on'] or 'not set'}"
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Track job applications and follow-up dates locally.")
    root.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    commands = root.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="Record a submitted application")
    add.add_argument("company")
    add.add_argument("role")
    add.add_argument("--follow-up", metavar="YYYY-MM-DD")
    add.add_argument("--applied-on", metavar="YYYY-MM-DD", help="Submission date (default: today)")

    listing = commands.add_parser("list", help="Show saved applications")
    listing.add_argument("--due", action="store_true", help="Only show follow-ups due today or earlier")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--search", help="Find company or role text, ignoring case")

    export = commands.add_parser("export", help="Write a CSV export to standard output")
    export.add_argument("--due", action="store_true", help="Only export follow-ups due today or earlier")
    export.add_argument("--status", choices=STATUSES)
    export.add_argument("--search", help="Only export matching company or role text")

    importing = commands.add_parser("import", help="Add applications from a CSV file")
    importing.add_argument("file", help="CSV file to read, or - for standard input")
    importing.add_argument("--dry-run", action="store_true", help="Check the file without saving anything")

    update = commands.add_parser("update", help="Change an application's details, status, or follow-up")
    update.add_argument("id", type=int)
    update.add_argument("--company", help="Correct the company name")
    update.add_argument("--role", help="Correct the role title")
    update.add_argument("--status", choices=STATUSES)
    update.add_argument("--applied-on", metavar="YYYY-MM-DD", help="Correct the submission date")
    follow_up = update.add_mutually_exclusive_group()
    follow_up.add_argument("--follow-up", metavar="YYYY-MM-DD")
    follow_up.add_argument("--clear-follow-up", action="store_true")

    delete = commands.add_parser("delete", help="Permanently remove an application")
    delete.add_argument("id", type=int)
    delete.add_argument("--yes", action="store_true", help="Delete without asking for confirmation")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with closing(connect(args.database)) as connection:
            if args.command == "add":
                application_id = add_application(connection, args.company, args.role, args.follow_up,
                                                 applied_on=args.applied_on)
                print(f"Added application #{application_id}.")
            elif args.command == "list":
                display(list_applications(connection, due_only=args.due, status=args.status, search=args.search),
                        args.due, filtered=args.status is not None or args.search is not None)
            elif args.command == "export":
                export_csv(list_applications(connection, due_only=args.due, status=args.status,
                                             search=args.search), sys.stdout)
            elif args.command == "import":
                if args.file == "-":
                    count, skipped = import_csv(connection, sys.stdin, dry_run=args.dry_run)
                else:
                    with open(args.file, newline="", encoding="utf-8-sig") as source:
                        count, skipped = import_csv(connection, source, dry_run=args.dry_run)
                noun = "application" if count == 1 else "applications"
                summary = f"{count} {noun} ready to import" if args.dry_run else f"Imported {count} {noun}"
                print(f"{summary}; skipped {skipped} already saved." if skipped else f"{summary}.")
            elif args.command == "delete":
                row = get_application(connection, args.id)
                if not args.yes and not _confirmed(f"Delete {_summary(row)}? [y/N] "):
                    print("Nothing deleted.", file=sys.stderr)
                    return 1
                delete_application(connection, args.id)
                print(f"Deleted application #{args.id}.")
            else:
                update_application(connection, args.id, args.status, args.follow_up, args.clear_follow_up,
                                   company=args.company, role=args.role,
                                   applied_on=args.applied_on)
                print(f"Updated application #{args.id}.")
    except (ValueError, sqlite3.Error, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
