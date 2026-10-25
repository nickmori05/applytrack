# ApplyTrack

[![CI](https://github.com/nickmori05/applytrack/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/applytrack/actions/workflows/tests.yml)

ApplyTrack is a command-line tool for tracking job applications and the dates
you need to follow up on them. Applications are kept in a local SQLite database,
so there is no account, server, or network access involved. It uses only the
Python standard library.

- Record applications with their submission and follow-up dates.
- See what needs a follow-up today, filter by status, and search company, role,
  and note text.
- Keep a dated history of each application's status changes.
- Keep dated notes on each application, such as who you spoke to and what comes
  next, and correct or remove them later.
- Summarize your search: statuses, response and interview rates, and how long
  employers take to respond.
- Correct details, archive applications you no longer want to see, or delete mistakes.
- Export to CSV for a spreadsheet, and import from one.
- Back up everything, including history, notes, and archive dates, to a JSON
  file, and restore it into a new database.

## Quick start

Requires Python 3.10 or later and nothing else.

```sh
git clone https://github.com/nickmori05/applytrack.git
cd applytrack
python3 applytrack.py add "Example Company" "Software Engineer" --follow-up 2026-09-16
python3 applytrack.py update 1 --status interviewing
python3 applytrack.py list
python3 applytrack.py list --due
```

The company is fictional; use your own applications and follow-up dates, and the
ID printed by `add`. Run `python3 applytrack.py --help`, or `--help` after any
command, for every option. `python3 -m applytrack` works the same way.

## Usage

### Adding applications

```sh
python3 applytrack.py add "Example Company" "Engineer"
python3 applytrack.py add "Example Company" "Engineer" --applied-on 2026-09-01 --follow-up 2026-09-15
```

The submission date defaults to today and can be any valid `YYYY-MM-DD` date up
to today. Follow-up dates can be any valid date. Company and role are trimmed and
cannot be blank. Invalid input is rejected without saving anything.

All dates use the computer's local calendar date. A follow-up represents a day,
not a reminder at a particular time.

### Listing and searching

```sh
python3 applytrack.py list
python3 applytrack.py list --due
python3 applytrack.py list --status interviewing
python3 applytrack.py list --search "backend"
python3 applytrack.py list --search "sam"
```

`list` shows the newest application first. `--due` shows follow-ups due today or
earlier, oldest first, leaving out `rejected` and `withdrawn` applications. An
`offer` can still have a follow-up, such as a response deadline.

Statuses are `applied`, `interviewing`, `offer`, `rejected`, and `withdrawn`.

`--search` finds literal text anywhere in a company name, role, or any of an
application's notes, so you can find an application by the recruiter's name or a
detail you wrote down. It ignores case, including Unicode case differences
(`STRASSE` matches `Straße`), but not accents (`Montreal` does not match
`Montréal`). `%`, `_`, quotes, and backslashes are ordinary text, not patterns, and
a blank search is rejected.

`list --search` shows each matching note's date and the line that matched below
its application, so you can see why it was found:

```text
#1  Example Company — Software Engineer
    interviewing | Applied: 2026-09-01 | Follow up: 2026-09-15
    Note 2026-09-03: Recruiter call with Sam; take-home due Friday.
```

A line longer than 100 characters is shortened around the match, with `…` where
it was cut. Use `notes ID` to read the full notes.

`--due`, `--status`, and `--search` can be combined, and like the other filters,
`--search` looks only at active applications unless `--archived` is given.
Company, role, and note text is shown with control characters escaped, so stored
text cannot rewrite the terminal.

### Updating

```sh
python3 applytrack.py update 1 --status interviewing
python3 applytrack.py update 1 --follow-up 2026-09-23
python3 applytrack.py update 1 --clear-follow-up
python3 applytrack.py update 1 --company "Example Labs" --role "Backend Engineer"
python3 applytrack.py update 1 --applied-on 2026-08-28
python3 applytrack.py update 1 --status offer --changed-on 2026-09-30
```

Any of these options can be combined. They follow the same rules as `add`, and if
any requested change is invalid, the whole update is rejected. The ID never changes.

### Status history

```sh
python3 applytrack.py update 1 --status interviewing --changed-on 2026-09-12
python3 applytrack.py history 1
```

```text
#1  Example Company — Software Engineer
    2026-09-01  applied
    2026-09-12  interviewing
    2026-09-30  offer
```

Every `update --status` that changes the status adds a dated entry to the
application's history; setting the status it already has adds nothing. The date
is today unless `--changed-on` gives the day the change really happened, such as
the day an interview invitation arrived. It cannot be in the future, before the
submission date, or before the last recorded change, so the history always reads
in order and ends with the current status. For the same reason, `--applied-on`
cannot move the submission date after the first recorded change. Changes made on
the same day are kept in the order they were entered.

The history starts with the submission date. Applications saved before history
was kept, or imported with a status other than `applied`, show that status with
the date `unknown` until it next changes. Archiving does not change the history,
and deleting an application deletes its history.

### Notes

```sh
python3 applytrack.py note 1 "Recruiter call with Sam; take-home due Friday." --on 2026-09-03
python3 applytrack.py note 1 "Sent the take-home."
python3 applytrack.py notes 1
```

```text
#1  Example Company — Software Engineer
    1  2026-09-03  Recruiter call with Sam; take-home due Friday.
    2  2026-09-05  Sent the take-home.
```

`note` adds a note to an application, dated today unless `--on` gives the day it
is about. The date cannot be in the future but can be before the submission date,
for example a conversation with a referrer. The text is trimmed and cannot be blank.
`notes` shows an application's notes oldest first, with notes from the same day in
the order they were added, each with its number. A note spanning several lines
keeps its line breaks, and control characters are escaped as in `list`.

```sh
python3 applytrack.py edit-note 1 1 --text "Recruiter call with Sam Lee; take-home due Friday."
python3 applytrack.py edit-note 1 2 --on 2026-09-04
python3 applytrack.py delete-note 1 2
```

`edit-note ID NUMBER` corrects a note's text, its date, or both, using the number
`notes` shows. The new values follow the same rules as `note`, and an invalid edit
changes nothing. Notes stay in date order, so a new date can move a note; `edit-note`
says when it does, with the note's new number. Numbers belong to one application
and are not stored, so they are the same after a backup is restored, but they can
change when another note is added, moved, or deleted. Run `notes` again before
editing if you are unsure.

`delete-note ID NUMBER` shows the note and asks for confirmation like `delete`:
only `y` or `yes` deletes it, and `--yes` skips the question. Later notes move up
one number. If the number refers to a different note by the time you answer,
because another command changed the notes in between, nothing is deleted.

Archived applications can have notes added, edited, and shown. Deleting an
application deletes its notes.

### Summary statistics

```sh
python3 applytrack.py stats
python3 applytrack.py stats --since 2026-09-01
```

```text
Applications: 4 (1 archived)
    applied       1
    interviewing  0
    offer         1
    rejected      1
    withdrawn     1
Responses: 2 of 4 (50%)
Interviews: 1 of 4 (25%)
Days to first response: median 12, fastest 3, slowest 21 (from 2 responses)
```

`stats` counts every application, archived ones included, since archiving tidies
lists but does not change what happened. `--since` counts only applications
submitted on or after a date.

An application has a response once it reaches `interviewing`, `offer`, or
`rejected`, and an interview once it reaches `interviewing` or `offer`. These come
from its status history, so one that was interviewed and later rejected or
withdrawn still counts as interviewed, while one withdrawn before any response
does not count as a response. Rates are out of all counted applications, so recent
applications that have not had time for a reply lower them.

Days to first response run from the submission date to the first response in the
history. Responses whose date is `unknown`, from before history was kept or from
an import, count toward the response rate but not the timing, and the summary says
how many were left out. The line is omitted when there are no responses.

### Archiving and deleting

```sh
python3 applytrack.py archive 1
python3 applytrack.py list --archived
python3 applytrack.py restore 1
python3 applytrack.py delete 1
python3 applytrack.py delete 1 --yes
```

Archiving hides an application from `list`, `list --due`, and `export` and keeps
its status and dates. `--archived` shows or exports only archived applications,
with the date each was archived, and combines with the other filters. Archived
applications can still be updated or deleted by ID. Archiving one that is already
archived, or restoring one that is not, is an error and changes nothing.

`delete` shows the application and asks for confirmation. Only `y` or `yes`
deletes it; any other answer, or no input, leaves it saved and exits with status 1.
`--yes` skips the question for scripts. Deletion is permanent, so run `backup`
first if you may want the record back, and prefer archiving or the `rejected` and
`withdrawn` statuses for applications that simply ended. A deleted application's
ID is never given to another application.

### Exporting to CSV

```sh
python3 applytrack.py export > applications.csv
python3 applytrack.py export --due --status interviewing > interview-follow-ups.csv
python3 applytrack.py export --archived > archived.csv
```

`export` takes the same filters as `list`, uses the same order, and writes to
standard output; errors go to stderr. `export --search` includes applications
found by their notes, though the notes themselves are not exported. The columns
are `id`, `company`, `role`, `status`, `applied_on`, and `follow_up_on`. A missing
follow-up is an empty cell, and an empty export still has its header. Company and
role text that a spreadsheet would treat as a formula, such as text starting with
`=`, `+`, `-`, or `@`, is written with a leading apostrophe; the stored value is
unchanged. CSV files are ignored by Git.

### Importing from CSV

```sh
python3 applytrack.py import --dry-run applications.csv
python3 applytrack.py import applications.csv
```

The header row must include `company` and `role`. `status`, `applied_on`, and
`follow_up_on` are optional, in any order, and header names ignore case and
surrounding spaces. An `id` column is ignored, so a file from `export` can be
imported directly; imported rows receive new IDs. Any other column is rejected so
a misspelled header is not silently dropped.

Blank cells use the same defaults as `add`, and values follow the same rules. If
any row is invalid, nothing is imported and the error names the row as numbered
in a spreadsheet (the header is row 1). `--dry-run` runs every check and reports
how many applications would be added without saving them.

Rows matching a saved application's company, role, and submission date exactly,
including an archived one, are skipped and counted, so importing the same file
twice does not create copies. Matching rows within one file are all imported,
since two applications to the same role on the same day can be intentional.

Files are read as UTF-8, with or without the byte order mark some spreadsheet
programs add. Use `-` as the file name to read standard input. The apostrophe that
`export` adds to formula-like text is removed again.

### Backing up and restoring

```sh
python3 applytrack.py backup applytrack-backup.json
python3 applytrack.py --database .local/restored.sqlite3 restore-backup --dry-run applytrack-backup.json
python3 applytrack.py --database .local/restored.sqlite3 restore-backup applytrack-backup.json
```

`backup` saves every application, active and archived, with its ID, dates,
status history, and notes, to a JSON file. Unlike `export`, it takes no filters
and loses nothing, so it is the way to keep a copy or move to another computer.
The file is written next to its final name and then moved into place, so an
interrupted backup never leaves a partial file, and on macOS and Linux it is
readable only by you. An existing file is not replaced unless you pass `--force`.
Use `-` as the file name to write to standard output. JSON files are ignored by Git.

`restore-backup` loads a backup into a database with no applications, keeping
every ID, so `history`, `notes`, `update`, and the other commands work on the same
numbers as before. It never merges with or overwrites saved applications: restore
into a new file with `--database`, or move the old database aside first. The whole
file is checked before anything is saved, with the same rules as the other
commands: valid statuses and `YYYY-MM-DD` dates, non-blank text, and a status
history in date order that starts no earlier than the submission date. If any
value is invalid, nothing is restored and the error names the application by its
position in the file. Dates after today are accepted, since the backup may come
from a computer whose calendar was already on the next day. `--dry-run` runs every
check, including that the database is empty, without saving. Use `-` to read
standard input.

A backup also records the highest ID ever given out, so IDs deleted before the
backup are not given to new applications after a restore. Files from a newer
version of ApplyTrack are rejected rather than partly read. The format is
indented UTF-8 JSON:

```json
{
  "format": "applytrack-backup",
  "version": 1,
  "last_id": 1,
  "applications": [
    {
      "id": 1,
      "company": "Example Company",
      "role": "Software Engineer",
      "status": "interviewing",
      "applied_on": "2026-09-01",
      "follow_up_on": null,
      "archived_on": null,
      "status_history": [{"status": "interviewing", "changed_on": "2026-09-12"}],
      "notes": [{"noted_on": "2026-09-03", "body": "Recruiter call with Sam."}]
    }
  ]
}
```

## Data

The default database is `.local/applications.sqlite3` at the top of the checkout,
whichever directory the command is run from; `.local/` is ignored by Git. Use
`--database` for a separate one, for example to experiment or to restore a backup:

```sh
python3 applytrack.py --database .local/demo.sqlite3 add "Example Company" "Engineer"
```

Databases created by earlier versions are upgraded automatically the first time
they are opened, keeping every application and its ID. The status history and
notes start empty, so a status set before the upgrade shows the date `unknown`.
Each upgrade step runs in a single transaction, so if one fails the database is
left as it was and the error is shown. Upgrading needs write access to the
database file. SQLite records the highest saved ID during the upgrade; an ID above
that, deleted before upgrading, can still be used once more.

## Design

```text
applytrack.py         thin entry point for running from a checkout
applytrack/
  validation.py       statuses and the date and text rules shared by every command
  storage.py          SQLite schema, automatic upgrades, and record operations
  history.py          dated status changes and the timeline shown by history
  notes.py            dated notes: numbering, edits, deletion, and matching for --search
  stats.py            the status, response, and timing summary shown by stats
  csv_io.py           CSV export and import
  backup.py           JSON backups of the whole database, and restoring them
  backup_check.py     reading a backup and checking every value before a restore
  cli.py              argument parsing, terminal output, and exit codes
  __main__.py         python3 -m applytrack
tests/                unittest suite, one file per area
```

- **SQLite:** durable local storage without running a server. One table holds
  applications, and two more hold their status changes and notes. A shared or web
  version would need a different storage and access design.
- **Storage is separate from the interface:** `storage` and `csv_io` raise
  `ValueError` for invalid input and never print. `cli` turns errors into an
  `Error: ...` message on stderr with exit status 1.
- **Validate, then write once:** every value in a command or import is checked
  before anything is saved, and each write runs in a transaction, so a rejected
  command leaves the database unchanged.
- **Parameterized SQL:** user-entered values are always data, including
  punctuation in names.
- **Calendar dates as ISO text:** follow-ups are days, and validated `YYYY-MM-DD`
  strings sort correctly in SQL.
- **IDs are never reused:** the table uses `AUTOINCREMENT`, so an ID always refers
  to the same application, even after it is deleted.
- **History is written with the status:** `update` takes the write lock, checks
  the new dates against the recorded history, and saves the status and its history
  entry in one transaction. `delete` removes the application, its history, and its
  notes in one transaction, so neither table depends on SQLite foreign keys being
  enabled. A note is saved only if its application exists.
- **Backups restore only into an empty database:** a restore checks the whole
  file first, then inserts every application with its ID, history, and notes in
  one transaction and raises the ID counter to the backup's highest ID. Refusing
  a non-empty database avoids having to decide how two sets of IDs should merge.
- **Archiving is separate from status:** an `archived_on` date hides an
  application without changing its status, so restoring it brings back exactly
  what was saved.

## Development

Run the test suite from the top of the checkout:

```sh
python3 -m unittest discover -s tests -v
```

Linting uses [Ruff](https://docs.astral.sh/ruff/), configured in `pyproject.toml`.
Install it, for example in a virtual environment, and run `ruff check .`.

The tests use temporary databases and cover persistence across connections,
due-date boundaries, validation and rejected changes, search of names, roles, and
notes, CSV quoting and round trips, imports, deletion prompts, archiving, status
history, notes and their numbering, edits, and deletion, summary statistics, JSON backup round trips and rejected backups,
ID reuse, and upgrading databases from earlier versions, including failed
upgrades. GitHub Actions runs the tests on Python 3.10 and 3.14 and `ruff check`
for pushes to `main` and for pull requests.

## Limitations

- One local database per checkout. There is no synchronization between devices,
  no notifications, and no web interface.
- Importing does not merge rows into existing applications; it only adds or skips.
  A backup can be restored only into an empty database, not merged into one.
- Deleted applications cannot be restored unless they are in a backup taken
  before deleting them. CSV exports do not record whether an application is
  archived, its status history, or its notes; use `backup` for those.
- Backups are not scheduled or encrypted. Run `backup` yourself, and keep the
  file somewhere private, since it holds every note.
- Status history records days, not times, and only changes made with `update`.
  A recorded change cannot be edited or removed except by deleting the application.
- Notes are plain text with a date. Editing or deleting a note keeps no copy of
  the earlier text, so run `backup` first if you may want it back.
- `--search` matches one literal piece of text, not several words in any order,
  and does not look at statuses or dates, which have their own filters. It reads
  every note on each search, which is fast for a personal job search but has no
  full-text index.
- `stats` has no end date or per-company breakdown, and its timing covers only
  responses recorded with a date.
- ApplyTrack is meant to be run from a checkout and is not published as a package.
  The default database location assumes a checkout, so pass `--database` if you
  install it with `pip`.
