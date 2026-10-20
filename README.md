# ApplyTrack

[![CI](https://github.com/nickmori05/applytrack/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/applytrack/actions/workflows/tests.yml)

ApplyTrack is a command-line tool for tracking job applications and the dates
you need to follow up on them. Applications are kept in a local SQLite database,
so there is no account, server, or network access involved. It uses only the
Python standard library.

- Record applications with their submission and follow-up dates.
- See what needs a follow-up today, filter by status, and search by company or role.
- Correct details, archive applications you no longer want to see, or delete mistakes.
- Export to CSV for a spreadsheet, and import from one.

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
```

`list` shows the newest application first. `--due` shows follow-ups due today or
earlier, oldest first, leaving out `rejected` and `withdrawn` applications. An
`offer` can still have a follow-up, such as a response deadline.

Statuses are `applied`, `interviewing`, `offer`, `rejected`, and `withdrawn`.

`--search` finds literal text anywhere in a company name or role. It ignores case,
including Unicode case differences (`STRASSE` matches `Straße`), but not accents
(`Montreal` does not match `Montréal`). `%`, `_`, quotes, and backslashes are
ordinary text, not patterns, and a blank search is rejected.

`--due`, `--status`, and `--search` can be combined. Company and role text is
shown with control characters escaped, so stored text cannot rewrite the terminal.

### Updating

```sh
python3 applytrack.py update 1 --status interviewing
python3 applytrack.py update 1 --follow-up 2026-09-23
python3 applytrack.py update 1 --clear-follow-up
python3 applytrack.py update 1 --company "Example Labs" --role "Backend Engineer"
python3 applytrack.py update 1 --applied-on 2026-08-28
```

Any of these options can be combined. They follow the same rules as `add`, and if
any requested change is invalid, the whole update is rejected. The ID never changes.

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
`--yes` skips the question for scripts. Deletion is permanent, so export a CSV
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
standard output; errors go to stderr. The columns are `id`, `company`, `role`,
`status`, `applied_on`, and `follow_up_on`. A missing follow-up is an empty cell,
and an empty export still has its header. Company and role text that a spreadsheet
would treat as a formula, such as text starting with `=`, `+`, `-`, or `@`, is
written with a leading apostrophe; the stored value is unchanged. CSV files are ignored by Git.

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

## Data

The default database is `.local/applications.sqlite3` at the top of the checkout,
whichever directory the command is run from; `.local/` is ignored by Git. Use
`--database` for a separate one, for example to experiment:

```sh
python3 applytrack.py --database .local/demo.sqlite3 add "Example Company" "Engineer"
```

Databases created by earlier versions are upgraded automatically the first time
they are opened, keeping every application and its ID. Each upgrade step runs in
a single transaction, so if one fails the database is left as it was and the error
is shown. Upgrading needs write access to the database file. SQLite records the
highest saved ID during the upgrade; an ID above that, deleted before upgrading,
can still be used once more.

## Design

```text
applytrack.py         thin entry point for running from a checkout
applytrack/
  validation.py       statuses and the date and text rules shared by every command
  storage.py          SQLite schema, automatic upgrades, and record operations
  csv_io.py           CSV export and import
  cli.py              argument parsing, terminal output, and exit codes
  __main__.py         python3 -m applytrack
tests/                unittest suite, one file per area
```

- **SQLite:** durable local storage without running a server. One table fits the
  data. A shared or web version would need a different storage and access design.
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
due-date boundaries, validation and rejected changes, search, CSV quoting and
round trips, imports, deletion prompts, archiving, ID reuse, and upgrading
databases from earlier versions, including failed upgrades. GitHub Actions runs
the tests on Python 3.10 and 3.14 and `ruff check` for pushes to `main` and for
pull requests.

## Limitations

- One local database per checkout. There is no synchronization between devices,
  no notifications, and no web interface.
- Importing does not merge rows into existing applications; it only adds or skips.
- Deleted applications cannot be restored, and CSV exports do not record whether
  an application is archived.
- ApplyTrack is meant to be run from a checkout and is not published as a package.
  The default database location assumes a checkout, so pass `--database` if you
  install it with `pip`.
