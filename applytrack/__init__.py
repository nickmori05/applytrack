"""ApplyTrack: track job applications and follow-up dates in a local SQLite database.

The package is split by concern:

- ``validation``: statuses and the date and text rules every entry point shares.
- ``storage``: the SQLite schema, automatic upgrades, and record operations.
- ``history``: the dated status changes behind ``history ID``.
- ``notes``: the dated notes behind ``note`` and ``notes ID``.
- ``stats``: the status, response, and timing summary behind ``stats``.
- ``csv_io``: CSV export and import.
- ``backup`` and ``backup_check``: JSON backups of everything, and restoring them.
- ``cli``: argument parsing, terminal output, and exit codes.

The names below are re-exported so callers can use ``import applytrack``.
"""

from .backup import restore_backup, save_backup, snapshot, write_backup
from .backup_check import Counts, Record, read_backup, record_counts
from .cli import DEFAULT_DATABASE, display, main, note_excerpt, note_lines, parser, summary_lines
from .csv_io import EXPORT_COLUMNS, IMPORT_COLUMNS, export_csv, import_csv
from .history import status_changes, timeline
from .notes import application_notes, matching_notes
from .stats import INTERVIEW_STATUSES, RESPONSE_STATUSES, Summary, summarize
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
from .validation import CLOSED_STATUSES, STATUSES, nonempty, submission_date, validate_date

__all__ = [
    "CLOSED_STATUSES",
    "Counts",
    "DEFAULT_DATABASE",
    "EXPORT_COLUMNS",
    "IMPORT_COLUMNS",
    "INTERVIEW_STATUSES",
    "RESPONSE_STATUSES",
    "Record",
    "STATUSES",
    "Summary",
    "add_application",
    "add_note",
    "application_notes",
    "archive_application",
    "archived_count",
    "connect",
    "delete_application",
    "display",
    "export_csv",
    "get_application",
    "import_csv",
    "list_applications",
    "main",
    "matching_notes",
    "nonempty",
    "note_excerpt",
    "note_lines",
    "parser",
    "read_backup",
    "record_counts",
    "restore_application",
    "restore_backup",
    "save_backup",
    "snapshot",
    "status_changes",
    "submission_date",
    "summarize",
    "summary_lines",
    "timeline",
    "update_application",
    "validate_date",
    "write_backup",
]
