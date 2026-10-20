# ApplyTrack

[![Tests](https://github.com/nickmori05/applytrack/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/applytrack/actions/workflows/tests.yml)

A small command-line tool for tracking job applications and follow-up dates.
Applications stay in a local SQLite database between runs. The first version
uses Python's standard library and has no external package dependencies.

## Quick start

Requires Python 3.10 or later. Clone the repository and open its folder:

```sh
git clone https://github.com/nickmori05/applytrack.git
cd applytrack
```

Then add an application:

```sh
python3 applytrack.py add "Example Company" "Software Engineer" --follow-up 2026-09-16
python3 applytrack.py list
python3 applytrack.py update 1 --status interviewing
python3 applytrack.py list --due
```

The company in this example is fictional. Replace it with an application of your
own and choose a relevant follow-up date. Use the ID printed when you add it.

For an application submitted earlier, supply its actual date:

```sh
python3 applytrack.py add "Example Company" "Engineer" --applied-on 2026-09-01
```

Submission dates default to today. `--applied-on` accepts a valid `YYYY-MM-DD`
date up to today, using the computer's local date. Invalid or future dates are
rejected without adding an application. Lists and CSV exports retain this date.

To correct the submission date of a saved application:

```sh
python3 applytrack.py update 1 --applied-on 2026-08-28
```

The same rules apply: the date must be valid and no later than today.

To reschedule or clear a follow-up:

```sh
python3 applytrack.py update 1 --follow-up 2026-09-23
python3 applytrack.py update 1 --clear-follow-up
```

To correct company or role details without recreating the application:

```sh
python3 applytrack.py update 1 --company "Example Labs" --role "Backend Engineer"
```

Either field can be edited on its own or combined with status, submission date,
and follow-up changes. Surrounding whitespace is trimmed; blank names are rejected.
If any requested change is invalid, the entire update is rejected. The application
ID stays the same, and the submission date changes only when `--applied-on` is given.

To remove an application entirely, such as a duplicate or a test entry:

```sh
python3 applytrack.py delete 1
python3 applytrack.py delete 1 --yes
```

`delete` shows the application's company and role and asks for confirmation.
Only `y` or `yes` deletes it; any other answer, or no input, leaves it saved and
exits with status 1. `--yes` skips the question for scripts. Deletion is permanent
and there is no undo, so export a CSV first if you may want the record back. For an
application that simply ended, prefer `--status rejected` or `--status withdrawn`,
or archive it, which keeps its history. Once deleted, a matching spreadsheet row
is no longer skipped by `import`. A deleted application's ID is never given to
another application, so an old export or note that mentions it cannot point to
the wrong record.

To hide an application without losing it, archive it, and restore it later:

```sh
python3 applytrack.py archive 1
python3 applytrack.py list --archived
python3 applytrack.py restore 1
```

Archived applications are left out of `list`, `list --due`, and `export`, and keep
their status and dates. `--archived` shows or exports only archived applications,
with the date each was archived, and combines with `--status`, `--search`, and
`--due`. They can still be updated or deleted by ID, and `import` still skips
spreadsheet rows that match one. Archiving an archived application, or restoring
one that is not archived, is reported as an error and changes nothing.

Statuses: `applied`, `interviewing`, `offer`, `rejected`, `withdrawn`.
The due list includes today and overdue dates; rejected and withdrawn applications
are excluded. An offer can still have a follow-up, such as a response deadline.
Dates use the computer's local calendar date.

## Filters and CSV exports

```sh
python3 applytrack.py list --status interviewing
python3 applytrack.py list --due --status interviewing
python3 applytrack.py list --search "backend"
python3 applytrack.py export > applications.csv
python3 applytrack.py export --due --status interviewing > interview-follow-ups.csv
python3 applytrack.py export --search "Example Company" > company-applications.csv
```

`--search` finds literal text anywhere in a company name or role. It ignores case,
including Unicode case differences, so `STRASSE` matches `Straße`. Accents still
matter: `Montreal` does not match `Montréal`. Surrounding spaces are trimmed and
a blank search is rejected. `%`, `_`, quotes, and backslashes are ordinary text,
not patterns. Search combines with `--status` and `--due` without changing the
result order. It scans stored names and roles, which is suitable for a local
application list rather than a large search service.

Exports use the same filters and ordering as the terminal list. CSV columns are
`id`, `company`, `role`, `status`, `applied_on`, and `follow_up_on`. Missing
follow-up dates are empty cells; an empty export still includes its header.
Generated CSV files are excluded from Git.
Commas, quotes, Unicode, and embedded newlines are handled by Python's CSV writer.

Company and role text starting with spreadsheet formula prefixes is exported
with a leading apostrophe. Stored values are unchanged. CSV goes to stdout,
so redirect it to a file; errors go to stderr. Repeating a redirection command
with `>` replaces the destination file.

## Importing from CSV

Bring in applications already tracked in a spreadsheet:

```sh
python3 applytrack.py import --dry-run applications.csv
python3 applytrack.py import applications.csv
```

The first row names the columns: `company` and `role` are required; `status`,
`applied_on`, and `follow_up_on` are optional, in any order. Header names ignore
case and surrounding spaces. An `id` column is ignored, so a file produced by
`export` can be imported directly; imported rows receive new IDs. Any other
column is rejected so that a misspelled header is not silently dropped.

Blank cells use the same defaults as `add`: status `applied`, today's submission
date, and no follow-up. Values follow the same rules as `add` and `update`. If
any row is invalid, nothing is imported and the error names the row as it
appears in a spreadsheet (the header is row 1). `--dry-run` runs the same checks
and reports how many applications would be added without saving them.

Rows matching a saved application's company, role, and submission date exactly
are skipped and counted in the summary, so importing the same file again does
not create copies. Correcting a saved application's details or submission date
with `update` changes which spreadsheet rows it matches. Matching rows within one file are all imported, since two
applications to the same role on the same day can be intentional.

Files are read as UTF-8, including files saved by spreadsheet programs with a
byte order mark. Use `-` as the file name to read standard input. Company and
role text that `export` prefixed with an apostrophe is restored to its stored value.

## Data

The default database is `.local/applications.sqlite3` next to the script, regardless
of the directory from which it is launched. That folder is excluded from Git.
There is no remote service, account system, or automatic synchronization.

Databases created by earlier versions are upgraded automatically the first time
they are opened, keeping every application and its ID, with every application
active. Each upgrade step runs in a single transaction, so if one fails the
database is left as it was and the error is shown. Upgrading needs write access
to the database file. SQLite records the highest saved ID during the upgrade; an
ID above that, deleted before upgrading, can still be used once more.

Use a separate database for experiments:

```sh
python3 applytrack.py --database .local/demo.sqlite3 add "Example Company" "Engineer"
```

## Checks

```sh
python3 -m unittest discover -s tests -v
```

Tests cover persistence across connections, due-date boundaries, closed applications,
invalid-input behavior, rescheduling, company, role, and submission date corrections,
missing records, and a command-line workflow.
Delete tests cover removing one record, unknown IDs, declined and closed
confirmation prompts, `--yes`, and the import interaction.
Archive tests cover hiding and restoring applications, combined filters, repeated
or unknown requests, imports, the command line, and upgrading databases created
before archiving existed, including a failed upgrade.
ID tests cover deleted IDs staying unused, upgrading databases from earlier
versions with their records and IDs intact, and a failed upgrade leaving the
database unchanged.
Export tests cover filtering, CSV quoting, empty exports, and formula-like text.
Import tests cover defaults, header handling, row-numbered errors that leave the
database unchanged, export round trips, skipped re-imports, dry runs, and the
command line.
GitHub Actions runs the suite on Python 3.10 and 3.14 for pushes to `main` and pull requests.

## Design decisions

- **SQLite:** durable local storage without provisioning a server. One table fits
  the current data. A shared web application would require a new storage and access design.
- **A command-line interface first:** makes the initial workflow small and easy
  to inspect. Storage functions are separate from argument parsing and display.
- **Calendar dates:** a follow-up represents a day, not a reminder at a particular
  time. ISO-formatted dates allow straightforward ordering after validation.
- **Parameterized SQL:** user-entered values are data, including punctuation in names.
- **IDs are never reused:** the table uses `AUTOINCREMENT`, so an ID always refers
  to the same application, even after it is deleted.
- **Archiving is separate from status:** an `archived_on` date hides an application
  without changing its status, so restoring it brings back exactly what was saved.

## Current limits and next step

This version does not merge imported rows into existing records, restore deleted
applications, record archive state in CSV exports, send notifications, or
synchronize devices. A web interface can follow after the core workflow has been
used and reviewed.

## Walk through the code

1. Add an application and find where the script writes its database record.
2. Explain why closing and reopening the program does not lose that record.
3. Trace how `list --due` treats today's date and a rejected application.
4. Export a filtered list and explain why punctuation remains inside one CSV cell.
