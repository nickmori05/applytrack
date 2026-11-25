from contextlib import closing, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack
from applytrack.integrity import check_database, check_lines
from applytrack.storage import SCHEMA_VERSION, schema_version


# The applications table from before archiving, history, and notes.
LEGACY_TABLE = """CREATE TABLE applications (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL CHECK(length(trim(company)) > 0),
    role TEXT NOT NULL CHECK(length(trim(role)) > 0),
    status TEXT NOT NULL DEFAULT 'applied'
        CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
    applied_on TEXT NOT NULL,
    follow_up_on TEXT
)"""

STATUSES = "applied, interviewing, offer, rejected, withdrawn"

PROBLEMS = (
    "Application 1 has a blank company.",
    "Application 1 has a blank role.",
    f"Application 1 has status ghosted, which is not one of: {STATUSES}.",
    'Application 1 has submission date "2026-02-30", which is not a valid date.',
    'Application 1 has follow-up date "yesterday", which is not a valid date.',
    'Application 2 has archive date "2026-13-01", which is not a valid date.',
    "Status change 1 refers to application 99, which does not exist.",
    f"Status change 2 of application 2 has status nope, which is not one of: {STATUSES}.",
    'Status change 2 of application 2 has change date "not-a-date", which is not a valid date.',
    "Status change 3 of application 2 is dated 2026-08-01, before its submission date (2026-09-01).",
    "Status change 5 of application 2 is dated 2026-09-10, before status change 4 dated 2026-09-15.",
    "Note 1 refers to application 99, which does not exist.",
    'Note 2 of application 2 has date "09-01-2026", which is not a valid date.',
    "Note 3 of application 2 is blank.",
)


def run_cli(database, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


class SchemaVersionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"

    def raw(self, query, parameters=()):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(query, parameters).fetchall()

    def schema(self):
        return self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def test_new_database_is_marked_with_the_current_schema_version(self):
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertFalse(connection.in_transaction)

    def test_reopening_does_not_change_the_version_or_schema(self):
        applytrack.connect(self.database).close()
        schema = self.schema()
        self.assertEqual(self.raw("PRAGMA user_version"), [(1,)])
        applytrack.connect(self.database).close()
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.raw("PRAGMA user_version"), [(1,)])

    def test_legacy_database_is_upgraded_and_then_marked(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(LEGACY_TABLE)
            connection.execute(
                "INSERT INTO applications VALUES (1, 'Example', 'Engineer', 'interviewing', '2026-09-01', '2026-09-20')"
            )
            connection.commit()
        self.assertEqual(self.raw("PRAGMA user_version"), [(0,)])
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), 1)
        self.assertEqual(self.raw("SELECT company, status, follow_up_on FROM applications"),
                         [("Example", "interviewing", "2026-09-20")])
        self.assertEqual(check_database(connection).problems, ())
        self.assertIn("archived_on", self.raw("SELECT sql FROM sqlite_master WHERE name = 'applications'")[0][0])
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                                  "('status_changes', 'notes') ORDER BY name"),
                         [("notes",), ("status_changes",)])

    def test_failed_upgrade_does_not_mark_the_schema_current(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(LEGACY_TABLE)
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "INSERT INTO applications VALUES (1, 'Example', 'Engineer', 'ghosted', '2026-09-01', NULL)"
            )
            connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            applytrack.connect(self.database)
        self.assertEqual(self.raw("PRAGMA user_version"), [(0,)])
        self.assertNotIn("AUTOINCREMENT", self.raw("SELECT sql FROM sqlite_master WHERE name = 'applications'")[0][0])
        self.assertEqual(self.raw("SELECT status FROM applications"), [("ghosted",)])

    def test_current_schema_without_a_version_is_marked_without_further_changes(self):
        applytrack.connect(self.database).close()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA user_version = 0")
        schema = self.schema()
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), 1)
        self.assertEqual(self.schema(), schema)

    def test_newer_database_is_refused_and_left_unchanged(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY, note TEXT)")
            connection.execute("INSERT INTO kept VALUES (1, 'untouched')")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        before = self.database.read_bytes()
        message = r"schema version 2.*schema version 1"
        with self.assertRaisesRegex(ValueError, message):
            applytrack.connect(self.database)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.raw("PRAGMA user_version"), [(2,)])
        self.assertEqual(self.raw("SELECT note FROM kept"), [("untouched",)])
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name = 'applications'"), [])

    def test_cli_reports_a_newer_database_without_changing_it(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY)")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        before = self.database.read_bytes()
        for command in (("list",), ("check",)):
            with self.subTest(command=command):
                code, output, errors = run_cli(self.database, *command)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertEqual(errors, "Error: This database was written by a newer ApplyTrack "
                                 "(schema version 2); this one uses schema version 1. "
                                 "Update ApplyTrack before opening it.\n")
                self.assertEqual(self.database.read_bytes(), before)

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_current_database_can_be_opened_readonly(self):
        applytrack.connect(self.database).close()
        self.database.chmod(0o444)
        self.addCleanup(self.database.chmod, 0o644)
        code, output, errors = run_cli(self.database, "check")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Schema version 1.\n", output)
        self.assertIn("No problems found.\n", output)
        self.assertEqual(self.raw("PRAGMA user_version"), [(1,)])

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_readonly_database_without_a_version_is_left_unmarked(self):
        applytrack.connect(self.database).close()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA user_version = 0")
        schema = self.schema()
        self.database.chmod(0o444)
        self.addCleanup(self.database.chmod, 0o644)
        code, output, errors = run_cli(self.database, "check")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Error: attempt to write a readonly database", errors)
        self.database.chmod(0o644)
        self.assertEqual(self.raw("PRAGMA user_version"), [(0,)])
        self.assertEqual(self.schema(), schema)


class CheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def test_empty_database_has_no_problems(self):
        report = check_database(self.connection)
        self.assertEqual(report, (1, 0, 0, 0, ()))
        self.assertEqual(check_lines(report), [
            "Schema version 1.",
            "Applications: 0. Status changes: 0. Notes: 0.",
            "No problems found.",
        ])
        self.assertFalse(self.connection.in_transaction)

    def test_unusual_but_saved_records_are_not_problems(self):
        # A follow-up before the submission date, a status with no history, a note
        # from before the submission, a future follow-up, and two changes on one day
        # are all states the commands or an older database can leave behind.
        first = applytrack.add_application(self.connection, "Example", "Engineer", "2026-09-01",
                                           applied_on="2026-09-01")
        applytrack.update_application(self.connection, first, "interviewing", changed_on="2026-09-01")
        applytrack.update_application(self.connection, first, "offer", changed_on="2026-09-01")
        applytrack.add_note(self.connection, first, "Talked before applying.", "2026-08-20")
        second = applytrack.add_application(self.connection, "Other", "Analyst", "2026-12-01",
                                            applied_on="2026-09-05")
        applytrack.import_csv(self.connection, io.StringIO(
            "company,role,status,applied_on\nImported,Engineer,interviewing,2026-09-02\n"))
        with self.connection:
            self.connection.execute("UPDATE applications SET follow_up_on = '2026-08-01' WHERE id = ?", (first,))
            self.connection.execute("UPDATE applications SET archived_on = '2027-01-15' WHERE id = ?", (second,))
        report = check_database(self.connection)
        self.assertEqual(report.problems, ())
        self.assertEqual((report.applications, report.status_changes, report.notes), (3, 2, 1))

    def test_inconsistent_rows_are_reported_in_a_stable_order_and_left_in_place(self):
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        self.connection.execute(
            "INSERT INTO applications (id, company, role, status, applied_on, follow_up_on)"
            " VALUES (1, '  ', ' ', 'ghosted', '2026-02-30', 'yesterday')"
        )
        self.connection.execute(
            "INSERT INTO applications (id, company, role, status, applied_on, follow_up_on, archived_on)"
            " VALUES (2, 'Example', 'Engineer', 'applied', '2026-09-01', '2026-08-15', '2026-13-01')"
        )
        self.connection.executemany(
            "INSERT INTO status_changes (id, application_id, status, changed_on) VALUES (?, ?, ?, ?)",
            (
                (1, 99, "interviewing", "2026-09-05"),
                (2, 2, "nope", "not-a-date"),
                (3, 2, "offer", "2026-08-01"),
                (4, 2, "rejected", "2026-09-15"),
                (5, 2, "interviewing", "2026-09-10"),
            ),
        )
        self.connection.executemany(
            "INSERT INTO notes (id, application_id, noted_on, body) VALUES (?, ?, ?, ?)",
            (
                (1, 99, "2026-09-01", "Gone."),
                (2, 2, "09-01-2026", "Bad date."),
                (3, 2, "2026-09-02", "   "),
            ),
        )
        self.connection.commit()
        before = self.rows()
        # A new connection, as the check command opens: ignore_check_constraints is
        # per connection, and while it is on SQLite's own integrity check skips CHECKs.
        with closing(applytrack.connect(self.database)) as fresh:
            report = check_database(fresh)
        logical = tuple(problem for problem in report.problems if not problem.startswith("SQLite integrity check:"))
        integrity = [problem for problem in report.problems if problem.startswith("SQLite integrity check:")]
        self.assertEqual(report.problems[:len(integrity)], tuple(integrity))
        self.assertEqual(logical, PROBLEMS)
        if sqlite3.sqlite_version_info >= (3, 38, 0):
            self.assertEqual(set(integrity), {
                "SQLite integrity check: CHECK constraint failed in applications",
                "SQLite integrity check: CHECK constraint failed in status_changes",
                "SQLite integrity check: CHECK constraint failed in notes",
            })
        else:
            self.assertEqual(integrity, [])
        self.assertEqual((report.version, report.applications, report.status_changes, report.notes), (1, 2, 5, 3))
        self.assertEqual(check_lines(report)[-1], f"{len(report.problems)} problems found.")
        self.assertEqual(self.rows(), before)
        self.assertFalse(self.connection.in_transaction)
        # A follow-up dated before the submission is valid for application 2, so it is not listed.
        self.assertNotIn("2026-08-15", "\n".join(report.problems))

    def rows(self):
        tables = ("applications", "status_changes", "notes")
        return {table: self.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in tables}

    def test_check_sees_an_open_transaction_without_ending_it(self):
        self.connection.execute("BEGIN")
        self.connection.execute(
            "INSERT INTO notes (application_id, noted_on, body) VALUES (99, '2026-09-01', 'Uncommitted.')"
        )
        report = check_database(self.connection)
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(report.problems, ("Note 1 refers to application 99, which does not exist.",))
        self.connection.rollback()
        self.assertEqual(check_database(self.connection).notes, 0)

    def test_cli_prints_a_clean_database_and_a_problem(self):
        self.assertEqual(run_cli(self.database, "check"), (0, (
            "Schema version 1.\n"
            "Applications: 0. Status changes: 0. Notes: 0.\n"
            "No problems found.\n"
        ), ""))
        applytrack.add_application(self.connection, "Example", "Engineer", applied_on="2026-09-01")
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        self.connection.execute("UPDATE applications SET company = '  '")
        self.connection.commit()
        code, output, errors = run_cli(self.database, "check")
        self.assertEqual((code, errors), (1, ""))
        lines = output.splitlines()
        self.assertEqual(lines[0], "Schema version 1.")
        self.assertEqual(lines[1], "Applications: 1. Status changes: 0. Notes: 0.")
        self.assertEqual(lines[-1], f"{len(lines) - 3} problems found.")
        self.assertIn("Application 1 has a blank company.", lines)
        for line in lines[2:-1]:
            self.assertTrue(line == "Application 1 has a blank company." or line.startswith("SQLite integrity check:"))
        if sqlite3.sqlite_version_info >= (3, 38, 0):
            self.assertIn("SQLite integrity check: CHECK constraint failed in applications", lines)

    def test_cli_escapes_control_characters_in_a_problem(self):
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        self.connection.execute(
            "INSERT INTO applications (company, role, status, applied_on)"
            " VALUES (?, 'Engineer', 'applied', '2026-09-01')",
            ("Evil\x1bCo",),
        )
        self.connection.execute(
            "INSERT INTO status_changes (application_id, status, changed_on) VALUES (1, ?, '2026-09-02')",
            ("bad\x1b",),
        )
        self.connection.commit()
        code, output, errors = run_cli(self.database, "check")
        self.assertEqual((code, errors), (1, ""))
        self.assertIn("has status bad\\x1b, which is not one of:", output)
        self.assertNotIn("\x1b", output)


if __name__ == "__main__":
    unittest.main()
