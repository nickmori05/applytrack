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

To reschedule or clear a follow-up:

```sh
python3 applytrack.py update 1 --follow-up 2026-09-23
python3 applytrack.py update 1 --clear-follow-up
```

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

## Data

The default database is `.local/applications.sqlite3` next to the script, regardless
of the directory from which it is launched. That folder is excluded from Git.
There is no remote service, account system, or automatic synchronization.

Use a separate database for experiments:

```sh
python3 applytrack.py --database .local/demo.sqlite3 add "Example Company" "Engineer"
```

## Checks

```sh
python3 -m unittest discover -s tests -v
```

Tests cover persistence across connections, due-date boundaries, closed applications,
invalid-input behavior, rescheduling, missing records, and a command-line workflow.
Export tests cover filtering, CSV quoting, empty exports, and formula-like text.
GitHub Actions runs the suite on Python 3.10 and 3.14 for pushes to `main` and pull requests.

## Design decisions

- **SQLite:** durable local storage without provisioning a server. One table fits
  the current data. A shared web application would require a new storage and access design.
- **A command-line interface first:** makes the initial workflow small and easy
  to inspect. Storage functions are separate from argument parsing and display.
- **Calendar dates:** a follow-up represents a day, not a reminder at a particular
  time. ISO-formatted dates allow straightforward ordering after validation.
- **Parameterized SQL:** user-entered values are data, including punctuation in names.

## Current limits and next step

Applications are recorded as submitted today. This version does not edit company
or role, import existing applications, send notifications, or synchronize devices.
Possible next steps include editing company and role details, importing existing
records, and recording the actual submission date for older applications. A web
interface can follow after the core workflow has been used and reviewed.

## Walk through the code

1. Add an application and find where the script writes its database record.
2. Explain why closing and reopening the program does not lose that record.
3. Trace how `list --due` treats today's date and a rejected application.
4. Export a filtered list and explain why punctuation remains inside one CSV cell.
