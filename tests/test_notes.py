from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack


# The schema as created by the version before notes were added.
PREVIOUS_SCHEMA = (
    """CREATE TABLE applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company TEXT NOT NULL CHECK(length(trim(company)) > 0),
        role TEXT NOT NULL CHECK(length(trim(role)) > 0),
        status TEXT NOT NULL DEFAULT 'applied'
            CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
        applied_on TEXT NOT NULL,
        follow_up_on TEXT,
        archived_on TEXT
    )""",
    """CREATE TABLE status_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        status TEXT NOT NULL
            CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
        changed_on TEXT NOT NULL
    )""",
    "CREATE INDEX status_changes_by_application ON status_changes (application_id, changed_on)",
)

PREVIOUS_ROWS = [
    (1, "First", "Engineer", "interviewing", "2026-09-01", None, None),
    (3, "O'Brien's", "Backend Engineer", "applied", "2026-09-02", "2026-09-20", "2026-09-30"),
]


def run_cli(database, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


class NoteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)
        self.identifier = applytrack.add_application(self.connection, "Example", "Engineer",
                                                     applied_on="2026-09-01")

    def notes(self, connection=None, identifier=None):
        rows = applytrack.application_notes(connection or self.connection, identifier or self.identifier)
        return [(row["noted_on"], row["body"]) for row in rows]

    def test_notes_are_dated_ordered_and_survive_reopening(self):
        applytrack.add_note(self.connection, self.identifier, "Sent a thank-you email.", "2026-09-12")
        applytrack.add_note(self.connection, self.identifier, "Recruiter call with Sam.", "2026-09-03")
        applytrack.add_note(self.connection, self.identifier, "Take-home due Friday.", "2026-09-12")
        reopened = applytrack.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(self.notes(reopened), [
            ("2026-09-03", "Recruiter call with Sam."),
            ("2026-09-12", "Sent a thank-you email."),
            ("2026-09-12", "Take-home due Friday."),
        ])

    def test_note_is_trimmed_and_dated_today_by_default(self):
        note_id = applytrack.add_note(self.connection, self.identifier, "  Spoke to the hiring manager.\n")
        self.assertEqual(note_id, 1)
        self.assertEqual(self.notes(), [(date.today().isoformat(), "Spoke to the hiring manager.")])

    def test_note_may_predate_the_submission_and_belong_to_an_archived_application(self):
        applytrack.archive_application(self.connection, self.identifier)
        applytrack.add_note(self.connection, self.identifier, "Referral from a former colleague.", "2026-08-20")
        self.assertEqual(self.notes(), [("2026-08-20", "Referral from a former colleague.")])
        applytrack.restore_application(self.connection, self.identifier)
        self.assertEqual(len(self.notes()), 1)

    def test_invalid_notes_are_rejected_without_saving(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for identifier, body, noted_on, message in (
            (self.identifier, " \n\t", None, "Note cannot be empty."),
            (self.identifier, "Call", "2026-9-3", "valid date in YYYY-MM-DD"),
            (self.identifier, "Call", "2026-02-30", "valid date in YYYY-MM-DD"),
            (self.identifier, "Call", tomorrow, "Note date cannot be in the future."),
            (999, "Call", None, "Application 999 does not exist."),
        ):
            with self.subTest(body=body, noted_on=noted_on), self.assertRaisesRegex(ValueError, message):
                applytrack.add_note(self.connection, identifier, body, noted_on)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM notes").fetchone()[0], 0)

    def test_table_rejects_a_blank_note_written_directly(self):
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute(
                "INSERT INTO notes (application_id, noted_on, body) VALUES (?, '2026-09-01', '  ')",
                (self.identifier,),
            )

    def test_deleting_an_application_removes_only_its_notes(self):
        other = applytrack.add_application(self.connection, "Other", "Engineer", applied_on="2026-09-01")
        applytrack.add_note(self.connection, self.identifier, "Mine", "2026-09-02")
        applytrack.add_note(self.connection, other, "Theirs", "2026-09-02")
        applytrack.delete_application(self.connection, self.identifier)
        self.assertEqual(self.notes(identifier=self.identifier), [])
        self.assertEqual(self.notes(identifier=other), [("2026-09-02", "Theirs")])
        with self.assertRaisesRegex(ValueError, f"Application {self.identifier} does not exist."):
            applytrack.add_note(self.connection, self.identifier, "Too late")

    def test_failure_while_deleting_notes_keeps_the_application(self):
        applytrack.add_note(self.connection, self.identifier, "Keep me", "2026-09-02")
        with self.connection:
            self.connection.execute(
                "CREATE TRIGGER refuse BEFORE DELETE ON notes BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "refused"):
            applytrack.delete_application(self.connection, self.identifier)
        self.assertEqual(applytrack.get_application(self.connection, self.identifier)["company"], "Example")
        self.assertEqual(self.notes(), [("2026-09-02", "Keep me")])

    def test_cli_adds_and_shows_notes(self):
        self.assertEqual(run_cli(self.database, "notes", "1"),
                         (0, "#1  Example — Engineer\n    No notes yet. Add one with 'note'.\n", ""))
        self.assertEqual(run_cli(self.database, "note", "1", "Recruiter call with Sam.", "--on", "2026-09-03"),
                         (0, "Added a note to application #1.\n", ""))
        run_cli(self.database, "note", "1", "Take-home sent.")
        self.assertEqual(run_cli(self.database, "notes", "1"), (0, (
            "#1  Example — Engineer\n"
            "    1  2026-09-03  Recruiter call with Sam.\n"
            f"    2  {date.today().isoformat()}  Take-home sent.\n"
        ), ""))

    def test_cli_lines_up_multi_line_notes_and_escapes_control_characters(self):
        applytrack.add_note(self.connection, self.identifier, "Agenda:\r\n\n- system design\x1b[2J", "2026-09-03")
        self.assertEqual(run_cli(self.database, "notes", "1")[1], (
            "#1  Example — Engineer\n"
            "    1  2026-09-03  Agenda:\n"
            "\n"
            "                   - system design\\x1b[2J\n"
        ))

    def test_cli_reports_invalid_notes(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for arguments, message in (
            (("note", "1", "   "), "Note cannot be empty."),
            (("note", "1", "Call", "--on", tomorrow), "Note date cannot be in the future."),
            (("note", "999", "Call"), "Application 999 does not exist."),
            (("notes", "999"), "Application 999 does not exist."),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(run_cli(self.database, *arguments), (1, "", f"Error: {message}\n"))
        self.assertEqual(self.notes(), [])
        for arguments in (["note", "1"], ["notes"], ["note", "one", "Call"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    applytrack.main(["--database", str(self.database), *arguments])
                self.assertEqual(raised.exception.code, 2)


class NoteUpgradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            for statement in PREVIOUS_SCHEMA:
                connection.execute(statement)
            connection.executemany("INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?, ?)", PREVIOUS_ROWS)
            connection.execute("INSERT INTO status_changes (application_id, status, changed_on) "
                               "VALUES (1, 'interviewing', '2026-09-12')")
            connection.commit()

    def raw(self, query):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(query).fetchall()

    def schema(self):
        return self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def test_previous_database_gains_notes_and_keeps_its_records_and_history(self):
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(self.raw("SELECT * FROM applications ORDER BY id"), PREVIOUS_ROWS)
        self.assertEqual([tuple(row) for row in applytrack.status_changes(connection, 1)],
                         [("interviewing", "2026-09-12")])
        self.assertEqual(applytrack.application_notes(connection, 3), [])
        applytrack.add_note(connection, 3, "Archived, but worth remembering.", "2026-09-30")
        self.assertEqual(self.raw("SELECT application_id, noted_on, body FROM notes"),
                         [(3, "2026-09-30", "Archived, but worth remembering.")])

    def test_reopening_an_upgraded_database_changes_nothing(self):
        applytrack.connect(self.database).close()
        schema = self.schema()
        self.assertIn(("index", "notes_by_application"), [(kind, name) for kind, name, _ in schema])
        applytrack.connect(self.database).close()
        self.assertEqual(self.schema(), schema)

    def test_cli_adds_notes_to_a_previous_database(self):
        self.assertEqual(run_cli(self.database, "note", "1", "Panel on Monday.", "--on", "2026-09-14")[0], 0)
        self.assertEqual(run_cli(self.database, "notes", "1"),
                         (0, "#1  First — Engineer\n    1  2026-09-14  Panel on Monday.\n", ""))
        self.assertEqual(run_cli(self.database, "delete", "1", "--yes")[0], 0)
        self.assertEqual(self.raw("SELECT count(*) FROM notes"), [(0,)])

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_failed_upgrade_is_reported_and_leaves_the_database_unchanged(self):
        schema = self.schema()
        self.database.chmod(0o444)
        self.addCleanup(self.database.chmod, 0o644)
        code, output, errors = run_cli(self.database, "notes", "1")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Error: attempt to write a readonly database", errors)
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.raw("SELECT * FROM applications ORDER BY id"), PREVIOUS_ROWS)


if __name__ == "__main__":
    unittest.main()
