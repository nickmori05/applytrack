"""Command-line interface: argument parsing, terminal output, and exit codes."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

from .csv_io import export_csv, import_csv
from .storage import (
    add_application,
    archive_application,
    archived_count,
    connect,
    delete_application,
    get_application,
    list_applications,
    restore_application,
    update_application,
)
from .validation import STATUSES

# The database lives in .local/ at the top of the checkout, next to applytrack.py,
# whichever directory the command is run from.
DEFAULT_DATABASE = Path(__file__).resolve().parent.parent / ".local" / "applications.sqlite3"


def _terminal_text(value: str) -> str:
    # Show control characters as escapes so stored text cannot rewrite the terminal.
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value)


def _summary(row: sqlite3.Row) -> str:
    return f"#{row['id']}  {_terminal_text(row['company'])} — {_terminal_text(row['role'])}"


def _confirmed(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def display(
    rows: list[sqlite3.Row], due_only: bool, filtered: bool = False, *, archived: bool = False,
    hidden: int = 0,
) -> None:
    """Print applications for the terminal, or a message explaining an empty list.

    ``filtered``, ``due_only``, and ``archived`` describe the list that was asked
    for; ``hidden`` is the number of archived applications left out of it.
    """
    if not rows:
        if filtered:
            print("No applications match these filters.")
        elif due_only:
            print("No follow-ups due.")
        elif archived:
            print("No archived applications.")
        elif hidden:
            print(f"No active applications; {hidden} archived. Show them with 'list --archived'.")
        else:
            print("No applications yet. Add your first with 'add'.")
        return
    for row in rows:
        print(_summary(row))
        archived_on = f" | Archived: {row['archived_on']}" if row["archived_on"] else ""
        print(
            f"    {row['status']} | Applied: {row['applied_on']}"
            f" | Follow up: {row['follow_up_on'] or 'not set'}{archived_on}"
        )


def parser() -> argparse.ArgumentParser:
    """Build the argument parser for every subcommand."""
    root = argparse.ArgumentParser(description="Track job applications and follow-up dates locally.")
    root.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                      help="SQLite file to use (default: .local/applications.sqlite3 in the checkout)")
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
    listing.add_argument("--archived", action="store_true", help="Show archived applications instead")

    export = commands.add_parser("export", help="Write a CSV export to standard output")
    export.add_argument("--due", action="store_true", help="Only export follow-ups due today or earlier")
    export.add_argument("--status", choices=STATUSES)
    export.add_argument("--search", help="Only export matching company or role text")
    export.add_argument("--archived", action="store_true", help="Export archived applications instead")

    importing = commands.add_parser("import", help="Add applications from a CSV file")
    importing.add_argument("file", help="CSV file to read, or - for standard input")
    importing.add_argument("--dry-run", action="store_true", help="Check the file without saving anything")

    update = commands.add_parser("update", help="Change an application's details, status, or follow-up")
    update.add_argument("id", type=int, help="Application ID")
    update.add_argument("--company", help="Correct the company name")
    update.add_argument("--role", help="Correct the role title")
    update.add_argument("--status", choices=STATUSES)
    update.add_argument("--applied-on", metavar="YYYY-MM-DD", help="Correct the submission date")
    follow_up = update.add_mutually_exclusive_group()
    follow_up.add_argument("--follow-up", metavar="YYYY-MM-DD")
    follow_up.add_argument("--clear-follow-up", action="store_true")

    archive = commands.add_parser("archive", help="Hide an application from lists and exports")
    archive.add_argument("id", type=int, help="Application ID")

    restore = commands.add_parser("restore", help="Return an archived application to lists and exports")
    restore.add_argument("id", type=int, help="Application ID")

    delete = commands.add_parser("delete", help="Permanently remove an application")
    delete.add_argument("id", type=int, help="Application ID")
    delete.add_argument("--yes", action="store_true", help="Delete without asking for confirmation")
    return root


def _add(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    application_id = add_application(connection, args.company, args.role, args.follow_up,
                                     applied_on=args.applied_on)
    print(f"Added application #{application_id}.")
    return 0


def _list(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    rows = list_applications(connection, due_only=args.due, status=args.status,
                             search=args.search, archived=args.archived)
    filtered = args.status is not None or args.search is not None
    hidden = 0 if rows or filtered or args.due or args.archived else archived_count(connection)
    display(rows, args.due, filtered=filtered, archived=args.archived, hidden=hidden)
    return 0


def _export(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    export_csv(list_applications(connection, due_only=args.due, status=args.status,
                                 search=args.search, archived=args.archived), sys.stdout)
    return 0


def _import(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.file == "-":
        count, skipped = import_csv(connection, sys.stdin, dry_run=args.dry_run)
    else:
        with open(args.file, newline="", encoding="utf-8-sig") as source:
            count, skipped = import_csv(connection, source, dry_run=args.dry_run)
    noun = "application" if count == 1 else "applications"
    summary = f"{count} {noun} ready to import" if args.dry_run else f"Imported {count} {noun}"
    print(f"{summary}; skipped {skipped} already saved." if skipped else f"{summary}.")
    return 0


def _update(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    update_application(connection, args.id, args.status, args.follow_up, args.clear_follow_up,
                       company=args.company, role=args.role, applied_on=args.applied_on)
    print(f"Updated application #{args.id}.")
    return 0


def _archive(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    archive_application(connection, args.id)
    print(f"Archived application #{args.id}.")
    return 0


def _restore(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    restore_application(connection, args.id)
    print(f"Restored application #{args.id}.")
    return 0


def _delete(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    row = get_application(connection, args.id)
    if not args.yes and not _confirmed(f"Delete {_summary(row)}? [y/N] "):
        print("Nothing deleted.", file=sys.stderr)
        return 1
    delete_application(connection, args.id)
    print(f"Deleted application #{args.id}.")
    return 0


COMMANDS = {
    "add": _add,
    "list": _list,
    "export": _export,
    "import": _import,
    "update": _update,
    "archive": _archive,
    "restore": _restore,
    "delete": _delete,
}


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit status.

    Invalid input, database errors, and file errors are printed to stderr as
    ``Error: ...`` with status 1. Invalid arguments exit through argparse with status 2.
    """
    args = parser().parse_args(argv)
    try:
        with closing(connect(args.database)) as connection:
            return COMMANDS[args.command](connection, args)
    except (ValueError, sqlite3.Error, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
