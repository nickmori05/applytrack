from contextlib import closing, redirect_stderr, redirect_stdout
import csv
from datetime import date
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack


# The table as created by the version before archiving was added.
PREVIOUS_TABLE = """CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL CHECK(length(trim(company)) > 0),
    role TEXT NOT NULL CHECK(length(trim(role)) > 0),
    status TEXT NOT NULL DEFAULT 'applied'
        CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
    applied_on TEXT NOT NULL,
    follow_up_on TEXT
)"""

PREVIOUS_ROWS = [
    (1, "First", "Engineer", "applied", "2026-09-01", None),
    (3, "O'Brien's", "Backend Engineer", "interviewing", "2026-09-02", "2026-09-20"),
]


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def ids(self, **filters):
        return [row["id"] for row in applytrack.list_applications(self.connection, **filters)]

    def run_cli(self, *arguments):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = applytrack.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_archived_application_is_hidden_until_restored_and_survives_reopening(self):
        keep = applytrack.add_application(self.connection, "Keep", "Engineer", "2026-09-01")
        hide = applytrack.add_application(self.connection, "Hide", "Engineer", "2026-09-01")
        original = dict(applytrack.get_application(self.connection, hide))
        applytrack.archive_application(self.connection, hide, today=date(2026, 9, 9))
        reopened = applytrack.connect(self.database)
        self.addCleanup(reopened.close)
        today = date(2026, 9, 9)
        self.assertEqual([row["id"] for row in applytrack.list_applications(reopened)], [keep])
        self.assertEqual([row["id"] for row in applytrack.list_applications(reopened, due_only=True, today=today)],
                         [keep])
        archived = applytrack.list_applications(reopened, archived=True)
        self.assertEqual([dict(row) for row in archived], [{**original, "archived_on": "2026-09-09"}])
        applytrack.restore_application(reopened, hide)
        self.assertEqual(dict(applytrack.get_application(reopened, hide)), original)
        self.assertEqual(self.ids(), [hide, keep])
        self.assertEqual(self.ids(archived=True), [])

    def test_archived_list_combines_with_other_filters(self):
        backend = applytrack.add_application(self.connection, "Backend Labs", "Engineer", "2026-09-01")
        applytrack.update_application(self.connection, backend, status="interviewing")
        other = applytrack.add_application(self.connection, "Other", "Designer")
        active = applytrack.add_application(self.connection, "Backend Active", "Engineer", "2026-09-01")
        for identifier in (backend, other):
            applytrack.archive_application(self.connection, identifier)
        self.assertEqual(self.ids(archived=True), [other, backend])
        self.assertEqual(self.ids(archived=True, search="backend"), [backend])
        self.assertEqual(self.ids(archived=True, status="interviewing"), [backend])
        self.assertEqual(self.ids(archived=True, due_only=True, today=date(2026, 9, 9)), [backend])
        self.assertEqual(self.ids(search="backend"), [active])

    def test_invalid_archive_and_restore_requests_change_nothing(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer")
        for action in (applytrack.archive_application, applytrack.restore_application):
            for missing in (999, 0, -1):
                with self.subTest(action=action.__name__, missing=missing):
                    with self.assertRaisesRegex(ValueError, f"Application {missing} does not exist."):
                        action(self.connection, missing)
        with self.assertRaisesRegex(ValueError, f"Application {identifier} is not archived."):
            applytrack.restore_application(self.connection, identifier)
        self.assertIsNone(applytrack.get_application(self.connection, identifier)["archived_on"])
        applytrack.archive_application(self.connection, identifier, today=date(2026, 9, 9))
        with self.assertRaisesRegex(ValueError, f"Application {identifier} is already archived."):
            applytrack.archive_application(self.connection, identifier, today=date(2026, 9, 10))
        self.assertEqual(applytrack.get_application(self.connection, identifier)["archived_on"], "2026-09-09")

    def test_archived_applications_still_count_as_saved_when_importing(self):
        applytrack.add_application(self.connection, "Example", "Engineer", applied_on="2026-09-01")
        applytrack.archive_application(self.connection, 1)
        source = "company,role,applied_on\nExample,Engineer,2026-09-01\n"
        self.assertEqual(applytrack.import_csv(self.connection, io.StringIO(source)), (0, 1))
        self.assertEqual(self.ids(), [])

    def test_archived_applications_can_still_be_updated_and_deleted(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer")
        applytrack.archive_application(self.connection, identifier, today=date(2026, 9, 9))
        applytrack.update_application(self.connection, identifier, status="rejected")
        row = applytrack.get_application(self.connection, identifier)
        self.assertEqual((row["status"], row["archived_on"]), ("rejected", "2026-09-09"))
        applytrack.delete_application(self.connection, identifier)
        self.assertEqual(self.ids(archived=True), [])

    def test_cli_archives_lists_exports_and_restores(self):
        applytrack.add_application(self.connection, "Keep", "Engineer")
        hide = applytrack.add_application(self.connection, "Hide", "Engineer", applied_on="2026-09-01")
        self.assertEqual(self.run_cli("archive", str(hide)), (0, f"Archived application #{hide}.\n", ""))
        code, output, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertNotIn("Hide", output)
        self.assertNotIn("Archived:", output)
        code, output, _ = self.run_cli("list", "--archived")
        today = date.today().isoformat()
        self.assertEqual(output, f"#{hide}  Hide — Engineer\n"
                                 f"    applied | Applied: 2026-09-01 | Follow up: not set | Archived: {today}\n")
        _, exported, _ = self.run_cli("export")
        self.assertEqual([row["company"] for row in csv.DictReader(io.StringIO(exported))], ["Keep"])
        _, exported, _ = self.run_cli("export", "--archived")
        self.assertEqual(exported, ",".join(applytrack.EXPORT_COLUMNS)
                         + f"\n{hide},Hide,Engineer,applied,2026-09-01,\n")
        self.assertEqual(self.run_cli("restore", str(hide)), (0, f"Restored application #{hide}.\n", ""))
        self.assertIn("Hide", self.run_cli("list")[1])

    def test_cli_reports_invalid_archive_and_restore_requests(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer")
        for arguments, message in (
            (("archive", "999"), "Application 999 does not exist."),
            (("restore", "999"), "Application 999 does not exist."),
            (("restore", str(identifier)), f"Application {identifier} is not archived."),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(*arguments), (1, "", f"Error: {message}\n"))
        self.run_cli("archive", str(identifier))
        self.assertEqual(self.run_cli("archive", str(identifier)),
                         (1, "", f"Error: Application {identifier} is already archived.\n"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            applytrack.main(["--database", str(self.database), "archive", "first"])

    def test_cli_empty_lists_explain_archived_applications(self):
        self.assertEqual(self.run_cli("list", "--archived")[1], "No archived applications.\n")
        identifier = applytrack.add_application(self.connection, "Example", "Engineer")
        applytrack.archive_application(self.connection, identifier)
        self.assertEqual(self.run_cli("list")[1],
                         "No active applications; 1 archived. Show them with 'list --archived'.\n")
        self.assertEqual(self.run_cli("list", "--due")[1], "No follow-ups due.\n")
        self.assertEqual(self.run_cli("list", "--search", "Example")[1], "No applications match these filters.\n")


class ArchiveUpgradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(PREVIOUS_TABLE)
            connection.executemany("INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?)", PREVIOUS_ROWS)
            connection.commit()

    def raw(self, query):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(query).fetchall()

    def open(self):
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        return connection

    def test_previous_database_gains_archiving_and_keeps_its_records(self):
        connection = self.open()
        rows = applytrack.list_applications(connection)
        self.assertEqual([tuple(row) for row in reversed(rows)], [(*row, None) for row in PREVIOUS_ROWS])
        applytrack.archive_application(connection, 3, today=date(2026, 9, 9))
        self.assertEqual([row["id"] for row in applytrack.list_applications(connection)], [1])
        self.assertEqual(self.raw("SELECT id, archived_on FROM applications ORDER BY id"),
                         [(1, None), (3, "2026-09-09")])
        self.assertEqual(applytrack.add_application(connection, "Next", "Engineer"), 4)

    def test_reopening_an_upgraded_database_changes_nothing(self):
        self.open().close()
        schema = self.raw("SELECT sql FROM sqlite_master ORDER BY name")
        self.assertIn("archived_on", schema[0][0])
        self.open().close()
        self.assertEqual(self.raw("SELECT sql FROM sqlite_master ORDER BY name"), schema)

    def test_cli_works_with_a_previous_database(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(applytrack.main(["--database", str(self.database), "archive", "1"]), 0)
            self.assertEqual(applytrack.main(["--database", str(self.database), "list"]), 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertIn("#3  O'Brien's — Backend Engineer", output.getvalue())
        self.assertNotIn("First", output.getvalue())

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_failed_upgrade_is_reported_and_leaves_the_database_unchanged(self):
        schema = self.raw("SELECT sql FROM sqlite_master ORDER BY name")
        self.database.chmod(0o444)
        self.addCleanup(self.database.chmod, 0o644)
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(errors):
            code = applytrack.main(["--database", str(self.database), "list"])
        self.assertEqual((code, output.getvalue()), (1, ""))
        self.assertIn("Error: attempt to write a readonly database", errors.getvalue())
        self.assertEqual(self.raw("SELECT sql FROM sqlite_master ORDER BY name"), schema)
        self.assertEqual(self.raw("SELECT * FROM applications ORDER BY id"), PREVIOUS_ROWS)


if __name__ == "__main__":
    unittest.main()
