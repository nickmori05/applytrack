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


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY,
            company TEXT NOT NULL CHECK(length(trim(company)) > 0),
            role TEXT NOT NULL CHECK(length(trim(role)) > 0),
            status TEXT NOT NULL DEFAULT 'applied'
                CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
            applied_on TEXT NOT NULL,
            follow_up_on TEXT
        )"""
    )
    connection.commit()
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
    today = date.today().isoformat()
    applied_on = today if applied_on is None else validate_date(applied_on)
    if applied_on > today:
        raise ValueError("Submission date cannot be in the future.")
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
) -> list[sqlite3.Row]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    clauses = []
    values = []
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


def update_application(
    connection: sqlite3.Connection,
    application_id: int,
    status: str | None = None,
    follow_up_on: str | None = None,
    clear_follow_up: bool = False,
    *,
    company: str | None = None,
    role: str | None = None,
) -> None:
    if all(value is None for value in (status, follow_up_on, company, role)) and not clear_follow_up:
        raise ValueError("Choose a company, role, status, follow-up date, or --clear-follow-up.")
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


def _terminal_text(value: str) -> str:
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value)


def display(rows: list[sqlite3.Row], due_only: bool, filtered: bool = False) -> None:
    if not rows:
        if filtered:
            print("No applications match these filters.")
        else:
            print("No follow-ups due." if due_only else "No applications yet. Add your first with 'add'.")
        return
    for row in rows:
        company = _terminal_text(row["company"])
        role = _terminal_text(row["role"])
        print(f"#{row['id']}  {company} — {role}")
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

    export = commands.add_parser("export", help="Write a CSV export to standard output")
    export.add_argument("--due", action="store_true", help="Only export follow-ups due today or earlier")
    export.add_argument("--status", choices=STATUSES)

    update = commands.add_parser("update", help="Change an application's details, status, or follow-up")
    update.add_argument("id", type=int)
    update.add_argument("--company", help="Correct the company name")
    update.add_argument("--role", help="Correct the role title")
    update.add_argument("--status", choices=STATUSES)
    follow_up = update.add_mutually_exclusive_group()
    follow_up.add_argument("--follow-up", metavar="YYYY-MM-DD")
    follow_up.add_argument("--clear-follow-up", action="store_true")
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
                display(list_applications(connection, due_only=args.due, status=args.status),
                        args.due, filtered=args.status is not None)
            elif args.command == "export":
                export_csv(list_applications(connection, due_only=args.due, status=args.status), sys.stdout)
            else:
                update_application(connection, args.id, args.status, args.follow_up, args.clear_follow_up,
                                   company=args.company, role=args.role)
                print(f"Updated application #{args.id}.")
    except (ValueError, sqlite3.Error, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
