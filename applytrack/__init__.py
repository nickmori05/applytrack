"""ApplyTrack: track job applications and follow-up dates in a local SQLite database.

The package is split by concern:

- ``validation``: statuses and the date and text rules every entry point shares.
- ``storage``: the SQLite schema, automatic upgrades, and record operations.
- ``csv_io``: CSV export and import.
- ``cli``: argument parsing, terminal output, and exit codes.

The names below are re-exported so callers can use ``import applytrack``.
"""

from .cli import DEFAULT_DATABASE, display, main, parser
from .csv_io import EXPORT_COLUMNS, IMPORT_COLUMNS, export_csv, import_csv
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
from .validation import CLOSED_STATUSES, STATUSES, nonempty, submission_date, validate_date

__all__ = [
    "CLOSED_STATUSES",
    "DEFAULT_DATABASE",
    "EXPORT_COLUMNS",
    "IMPORT_COLUMNS",
    "STATUSES",
    "add_application",
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
    "nonempty",
    "parser",
    "restore_application",
    "submission_date",
    "update_application",
    "validate_date",
]
