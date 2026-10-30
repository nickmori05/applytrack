"""Validation rules shared by the command line, storage, and CSV import."""

from datetime import date

STATUSES = ("applied", "interviewing", "offer", "rejected", "withdrawn")
CLOSED_STATUSES = ("rejected", "withdrawn")


def validate_date(value: str) -> str:
    """Return ``value`` if it is a real calendar date written as ``YYYY-MM-DD``.

    Raises ValueError for other formats, such as ``20260901``, and for dates that
    do not exist, such as ``2026-02-30``.
    """
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Use a valid date in YYYY-MM-DD format.") from error
    if parsed.isoformat() != value:
        raise ValueError("Use a valid date in YYYY-MM-DD format.")
    return value


def nonempty(value: str, label: str) -> str:
    """Return ``value`` without surrounding whitespace, or raise ValueError if blank."""
    value = value.strip()
    if not value:
        raise ValueError(f"{label} cannot be empty.")
    return value


def past_date(value: str, label: str) -> str:
    """Return ``value`` if it is a valid date no later than today's local date.

    ``label`` names the date in the error, for example ``Submission date``.
    """
    if validate_date(value) > date.today().isoformat():
        raise ValueError(f"{label} cannot be in the future.")
    return value


def submission_date(value: str | None) -> str:
    """Return a validated submission date, defaulting to today.

    Raises ValueError if the date is invalid or later than today's local date.
    """
    return date.today().isoformat() if value is None else past_date(value, "Submission date")


def check_follow_up(follow_up_on: str | None, applied_on: str) -> str | None:
    """Return ``follow_up_on`` if it is unset or no earlier than ``applied_on``.

    Both dates must already be validated. A follow-up on the submission day is allowed.
    """
    if follow_up_on is not None and follow_up_on < applied_on:
        raise ValueError(f"Follow-up date cannot be before the submission date ({applied_on}).")
    return follow_up_on


def check_status(status: str) -> str:
    """Return ``status`` if it is one of STATUSES, or raise ValueError."""
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    return status
