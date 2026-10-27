"""Command-line interface: argument parsing, terminal output, and exit codes."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
from statistics import median
import sys

from .backup import restore_backup, save_backup, snapshot, write_backup
from .backup_check import read_backup, record_counts
from .csv_io import export_csv, import_csv
from .history import status_changes, timeline
from .notes import application_notes, delete_note, edit_note, get_note, matching_notes
from .stats import Summary, summarize
from .storage import (
    add_application,
    add_note,
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


def note_lines(notes: list[sqlite3.Row]) -> list[str]:
    """Return the indented lines ``notes ID`` prints below the application.

    Each note follows its ID, used by ``edit-note`` and ``delete-note``, and its
    date. Later lines of a multi-line note line up under its first line.
    """
    if not notes:
        return ["    No notes yet. Add one with 'note'."]
    width = max(len(f"Note {note['id']}") for note in notes)
    lines = []
    for note in notes:
        first, *rest = (_terminal_text(line) for line in note["body"].replace("\r\n", "\n").split("\n"))
        lines.append(f"    {'Note ' + str(note['id']):<{width}}  {note['noted_on']}  {first}")
        lines.extend(f"    {'':{width + 12}}  {line}".rstrip() for line in rest)
    return lines


EXCERPT_WIDTH = 100


def note_excerpt(body: str, search: str, width: int = EXCERPT_WIDTH) -> str:
    """Return the line of a note that contains ``search``, shortened to about ``width`` characters.

    Case is ignored as in ``--search``. A long line is cut to a window that starts
    shortly before the match, with ``…`` marking each cut. If no single line holds
    the match, for example because it spans a line break, the first line is used.
    """
    search = search.strip().casefold()
    lines = body.replace("\r\n", "\n").split("\n")
    line = next((line for line in lines if search in line.casefold()), lines[0])
    if len(line) <= width:
        return _terminal_text(line)
    # Find where the match starts in the original text, which casefolding can lengthen.
    start = next((index for index in range(len(line)) if line[index:].casefold().startswith(search)), 0)
    start = max(0, min(start - width // 4, len(line) - width))
    window = line[start:start + width]
    return ("…" if start else "") + _terminal_text(window) + ("…" if start + width < len(line) else "")


def _confirmed(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def display(
    rows: list[sqlite3.Row], due_only: bool, filtered: bool = False, *, archived: bool = False,
    hidden: int = 0, notes: dict[int, list[sqlite3.Row]] | None = None, search: str | None = None,
) -> None:
    """Print applications for the terminal, or a message explaining an empty list.

    ``filtered``, ``due_only``, and ``archived`` describe the list that was asked
    for; ``hidden`` is the number of archived applications left out of it. With
    ``search``, each application's ``notes`` that matched it are shown as excerpts.
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
        if notes and search is not None:
            for note in notes.get(row["id"], []):
                print(f"    Note {note['noted_on']}: {note_excerpt(note['body'], search)}")


def _share(count: int, total: int) -> str:
    return f"{count} of {total} ({count / total:.0%})"


def _dated(days: tuple[int, ...], responded: int) -> str:
    if len(days) == responded:
        return f"from {responded} response" + ("" if responded == 1 else "s")
    return f"from {len(days)} of {responded} responses; the rest have no recorded date"


def summary_lines(summary: Summary, since: str | None = None) -> list[str]:
    """Return the lines ``stats`` prints for a summary of at least one application.

    ``since`` is the submission date the summary was limited to, if any.
    """
    archived = f" ({summary.archived} archived)" if summary.archived else ""
    scope = f" submitted on or after {since}" if since else ""
    lines = [f"Applications{scope}: {summary.total}{archived}"]
    lines.extend(f"    {status:<12}  {count}" for status, count in summary.by_status.items())
    lines.append(f"Responses: {_share(summary.responded, summary.total)}")
    lines.append(f"Interviews: {_share(summary.interviewed, summary.total)}")
    days = summary.response_days
    if days:
        lines.append(f"Days to first response: median {median(days):g}, fastest {days[0]}, slowest {days[-1]}"
                     f" ({_dated(days, summary.responded)})")
    elif summary.responded:
        lines.append("Days to first response: no response dates recorded yet")
    return lines


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
    listing.add_argument("--search", help="Find company, role, or note text, ignoring case")
    listing.add_argument("--archived", action="store_true", help="Show archived applications instead")

    export = commands.add_parser("export", help="Write a CSV export to standard output")
    export.add_argument("--due", action="store_true", help="Only export follow-ups due today or earlier")
    export.add_argument("--status", choices=STATUSES)
    export.add_argument("--search", help="Only export applications with matching company, role, or note text")
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
    update.add_argument("--changed-on", metavar="YYYY-MM-DD",
                        help="Date the new --status took effect (default: today)")
    follow_up = update.add_mutually_exclusive_group()
    follow_up.add_argument("--follow-up", metavar="YYYY-MM-DD")
    follow_up.add_argument("--clear-follow-up", action="store_true")

    stats = commands.add_parser("stats", help="Summarize statuses, responses, and time to respond")
    stats.add_argument("--since", metavar="YYYY-MM-DD", help="Only count applications submitted on or after this date")

    history = commands.add_parser("history", help="Show the dates an application's status changed")
    history.add_argument("id", type=int, help="Application ID")

    note = commands.add_parser("note", help="Add a dated note to an application")
    note.add_argument("id", type=int, help="Application ID")
    note.add_argument("text", help="The note, such as who you spoke to and what comes next")
    note.add_argument("--on", metavar="YYYY-MM-DD", help="Day the note is about (default: today)")

    notes = commands.add_parser("notes", help="Show an application's notes")
    notes.add_argument("id", type=int, help="Application ID")

    editing = commands.add_parser("edit-note", help="Correct a note's text or date")
    editing.add_argument("id", type=int, help="Note ID, as shown by 'notes'")
    editing.add_argument("--text", help="Replace the note's text")
    editing.add_argument("--on", metavar="YYYY-MM-DD", help="Change the day the note is about")

    removing = commands.add_parser("delete-note", help="Permanently remove one note")
    removing.add_argument("id", type=int, help="Note ID, as shown by 'notes'")
    removing.add_argument("--yes", action="store_true", help="Delete without asking for confirmation")

    backup = commands.add_parser("backup", help="Save everything, including history and notes, to a JSON file")
    backup.add_argument("file", help="JSON file to write, or - for standard output")
    backup.add_argument("--force", action="store_true", help="Replace the file if it already exists")

    restoring = commands.add_parser("restore-backup", help="Load a JSON backup into an empty database")
    restoring.add_argument("file", help="JSON file from backup, or - for standard input")
    restoring.add_argument("--dry-run", action="store_true", help="Check the backup without saving anything")

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
    notes = matching_notes(connection, args.search) if rows and args.search is not None else None
    display(rows, args.due, filtered=filtered, archived=args.archived, hidden=hidden,
            notes=notes, search=args.search)
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
                       company=args.company, role=args.role, applied_on=args.applied_on,
                       changed_on=args.changed_on)
    print(f"Updated application #{args.id}.")
    return 0


def _stats(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    summary = summarize(connection, since=args.since)
    if summary.total:
        print("\n".join(summary_lines(summary, args.since)))
    elif args.since:
        print(f"No applications submitted on or after {args.since}.")
    else:
        print("No applications yet. Add your first with 'add'.")
    return 0


def _history(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    row = get_application(connection, args.id)
    print(_summary(row))
    for changed_on, status in timeline(row, status_changes(connection, args.id)):
        print(f"    {changed_on or 'unknown':<10}  {status}")
    return 0


def _note(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    note_id = add_note(connection, args.id, args.text, args.on)
    print(f"Added note {note_id} to application #{args.id}.")
    return 0


def _notes(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    row = get_application(connection, args.id)
    print(_summary(row))
    print("\n".join(note_lines(application_notes(connection, args.id))))
    return 0


def _edit_note(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    edit_note(connection, args.id, args.text, args.on)
    print(f"Updated note {args.id}.")
    return 0


def _delete_note(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    note = get_note(connection, args.id)
    if not args.yes:
        print(_summary(get_application(connection, note["application_id"])))
        print("\n".join(note_lines([note])))
        if not _confirmed(f"Delete note {args.id}? [y/N] "):
            print("Nothing deleted.", file=sys.stderr)
            return 1
    delete_note(connection, args.id)
    print(f"Deleted note {args.id} from application #{note['application_id']}.")
    return 0


def _backup(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.file == "-":
        write_backup(snapshot(connection), sys.stdout)
        return 0
    counts = save_backup(connection, Path(args.file), replace=args.force)
    print(f"Backed up {counts} to {args.file}.")
    return 0


def _restore_backup(connection: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.file == "-":
        records, last_id = read_backup(sys.stdin)
    else:
        with open(args.file, encoding="utf-8-sig") as source:
            records, last_id = read_backup(source)
    restore_backup(connection, records, last_id, dry_run=args.dry_run)
    counts = record_counts(records)
    print(f"Backup is valid: {counts} ready to restore." if args.dry_run else f"Restored {counts}.")
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
    "history": _history,
    "stats": _stats,
    "note": _note,
    "notes": _notes,
    "edit-note": _edit_note,
    "delete-note": _delete_note,
    "backup": _backup,
    "restore-backup": _restore_backup,
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
