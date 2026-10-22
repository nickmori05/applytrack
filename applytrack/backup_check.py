"""Reading and checking a JSON backup before anything from it is restored.

Values follow the same rules as the rest of ApplyTrack: valid statuses, real
``YYYY-MM-DD`` dates, non-blank text, and a status history in date order that
starts no earlier than the submission date. The first invalid value raises
ValueError naming the application, so a damaged backup restores nothing.
"""

from dataclasses import dataclass
import json
from typing import NamedTuple, TextIO

from .validation import check_status, nonempty, validate_date

FORMAT = "applytrack-backup"
VERSION = 1
APPLICATION_FIELDS = ("id", "company", "role", "status", "applied_on", "follow_up_on", "archived_on")


@dataclass(frozen=True)
class Counts:
    """How many records a backup holds."""

    applications: int
    status_changes: int
    notes: int

    def __str__(self) -> str:
        parts = [(self.applications, "application"), (self.status_changes, "status change"), (self.notes, "note")]
        words = [f"{count} {noun}{'' if count == 1 else 's'}" for count, noun in parts]
        return f"{words[0]}, {words[1]}, and {words[2]}"


class Record(NamedTuple):
    """One checked application from a backup, ready to insert."""

    row: tuple
    status_changes: list[tuple[str, str]]
    notes: list[tuple[str, str]]


def _fields(value: object, required: tuple[str, ...], label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    unknown = [key for key in value if key not in required]
    if unknown:
        raise ValueError(f"{label} has an unknown field: {unknown[0]}.")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"{label} is missing {missing[0]}.")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    return value


def _date(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    return validate_date(_text(value, label))


def _list(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return value


def _whole_number(value: object, label: str, minimum: int = 0) -> int:
    # bool is a subclass of int, so true and false are rejected explicitly.
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be a whole number of at least {minimum}.")
    return value


def _history(application: dict, applied_on: str) -> list[tuple[str, str]]:
    changes = []
    for number, item in enumerate(_list(application["status_history"], "status_history"), start=1):
        label = f"Status change {number}"
        item = _fields(item, ("status", "changed_on"), label)
        changed_on = _date(item["changed_on"], f"{label} date")
        if changed_on < applied_on:
            raise ValueError(f"{label} is dated before the submission date.")
        if changes and changed_on < changes[-1][1]:
            raise ValueError(f"{label} is dated before the change listed before it.")
        changes.append((check_status(_text(item["status"], f"{label} status")), changed_on))
    return changes


def _notes(application: dict) -> list[tuple[str, str]]:
    notes = []
    for number, item in enumerate(_list(application["notes"], "notes"), start=1):
        item = _fields(item, ("noted_on", "body"), f"Note {number}")
        notes.append((_date(item["noted_on"], f"Note {number} date"),
                      nonempty(_text(item["body"], f"Note {number}"), f"Note {number}")))
    return notes


def _application(item: object) -> Record:
    item = _fields(item, (*APPLICATION_FIELDS, "status_history", "notes"), "Application")
    identifier = _whole_number(item["id"], "ID", minimum=1)
    applied_on = _date(item["applied_on"], "Submission date")
    row = (
        identifier,
        nonempty(_text(item["company"], "Company"), "Company"),
        nonempty(_text(item["role"], "Role"), "Role"),
        check_status(_text(item["status"], "Status")),
        applied_on,
        _date(item["follow_up_on"], "Follow-up date", optional=True),
        _date(item["archived_on"], "Archive date", optional=True),
    )
    return Record(row, _history(item, applied_on), _notes(item))


def read_backup(source: TextIO) -> tuple[list[Record], int]:
    """Read and check a backup, returning ``(records, last_id)``.

    Raises ValueError naming the first invalid value, so nothing is restored.
    """
    try:
        data = json.load(source)
    except json.JSONDecodeError as error:
        raise ValueError(f"Backup is not valid JSON: {error}.") from error
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("File is not an ApplyTrack backup.")
    version = data.get("version")
    if type(version) is int and version > VERSION:
        raise ValueError(f"Backup version {version} needs a newer ApplyTrack, which reads version {VERSION}.")
    if type(version) is not int or version != VERSION:
        raise ValueError("Backup has an invalid version.")
    data = _fields(data, ("format", "version", "last_id", "applications"), "Backup")
    records, seen = [], set()
    for number, item in enumerate(_list(data["applications"], "applications"), start=1):
        try:
            record = _application(item)
        except ValueError as error:
            raise ValueError(f"Application {number} in the backup: {error}") from error
        if record.row[0] in seen:
            raise ValueError(f"Application {number} in the backup repeats ID {record.row[0]}.")
        seen.add(record.row[0])
        records.append(record)
    last_id = _whole_number(data["last_id"], "Backup last_id")
    if last_id < max(seen, default=0):
        raise ValueError("Backup last_id is lower than an application ID.")
    return records, last_id


def record_counts(records: list[Record]) -> Counts:
    """Return the number of applications, status changes, and notes in checked records."""
    return Counts(len(records), sum(len(record.status_changes) for record in records),
                  sum(len(record.notes) for record in records))
