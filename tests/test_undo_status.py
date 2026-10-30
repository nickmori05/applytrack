from contextlib import closing, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack


# The schema as created by the version before each change recorded the status it replaced.
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
    """CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        noted_on TEXT NOT NULL,
        body TEXT NOT NULL CHECK(length(trim(body)) > 0)
    )""",
    "CREATE INDEX notes_by_application ON notes (application_id, noted_on)",
)

PREVIOUS_ROWS = [
    (1, "First", "Engineer", "offer", "2026-09-01", None, None),
    (2, "Imported", "Analyst", "interviewing", "2026-09-02", None, "2026-09-30"),
]

PREVIOUS_CHANGES = [
    (1, "interviewing", "2026-09-10"),
    (1, "offer", "2026-09-20"),
]


def run_cli(database, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


class ReplacedStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)
        self.identifier = applytrack.add_application(self.connection, "Example", "Engineer",
                                                     applied_on="2026-09-01")

    def recorded(self, identifier=None):
        rows = self.connection.execute(
            "SELECT previous_status, status, changed_on FROM status_changes WHERE application_id = ? ORDER BY id",
            (identifier or self.identifier,),
        )
        return [tuple(row) for row in rows]

    def test_each_change_records_the_status_it_replaced(self):
        applytrack.update_application(self.connection, self.identifier, status="interviewing",
                                      changed_on="2026-09-10")
        applytrack.update_application(self.connection, self.identifier, status="rejected", changed_on="2026-09-20")
        self.assertEqual(self.recorded(), [("applied", "interviewing", "2026-09-10"),
                                           ("interviewing", "rejected", "2026-09-20")])

    def test_imported_status_is_recorded_as_replaced(self):
        applytrack.import_csv(self.connection, io.StringIO(
            "company,role,status,applied_on\nImported,Analyst,interviewing,2026-09-02\n"))
        imported = applytrack.list_applications(self.connection)[0]["id"]
        applytrack.update_application(self.connection, imported, status="offer", changed_on="2026-09-15")
        self.assertEqual(self.recorded(imported), [("interviewing", "offer", "2026-09-15")])

    def test_unchanged_status_records_nothing(self):
        applytrack.update_application(self.connection, self.identifier, status="applied", company="Example Labs")
        self.assertEqual(self.recorded(), [])

    def test_table_rejects_an_invalid_replaced_status_written_directly(self):
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute(
                "INSERT INTO status_changes (application_id, status, changed_on, previous_status)"
                " VALUES (1, 'offer', '2026-09-10', 'ghosted')"
            )
        self.assertEqual(self.recorded(), [])


class ReplacedStatusUpgradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            for statement in PREVIOUS_SCHEMA:
                connection.execute(statement)
            connection.executemany("INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?, ?)", PREVIOUS_ROWS)
            connection.executemany("INSERT INTO status_changes (application_id, status, changed_on) VALUES (?, ?, ?)",
                                   PREVIOUS_CHANGES)
            connection.commit()

    def raw(self, query):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(query).fetchall()

    def schema(self):
        return self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def test_previous_database_keeps_its_history_with_no_replaced_status(self):
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(self.raw("SELECT * FROM applications ORDER BY id"), PREVIOUS_ROWS)
        self.assertEqual(self.raw("SELECT application_id, status, changed_on, previous_status FROM status_changes"),
                         [(*change, None) for change in PREVIOUS_CHANGES])
        applytrack.update_application(connection, 2, status="rejected", changed_on="2026-10-01")
        self.assertEqual(self.raw("SELECT previous_status FROM status_changes WHERE application_id = 2"),
                         [("interviewing",)])

    def test_reopening_an_upgraded_database_changes_nothing(self):
        applytrack.connect(self.database).close()
        schema = self.schema()
        self.assertIn("previous_status", dict((name, sql) for _, name, sql in schema)["status_changes"])
        applytrack.connect(self.database).close()
        self.assertEqual(self.schema(), schema)

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_failed_upgrade_is_reported_and_leaves_the_database_unchanged(self):
        schema = self.schema()
        self.database.chmod(0o444)
        self.addCleanup(self.database.chmod, 0o644)
        code, output, errors = run_cli(self.database, "history", "1")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Error: attempt to write a readonly database", errors)
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.raw("SELECT application_id, status, changed_on FROM status_changes"), PREVIOUS_CHANGES)


if __name__ == "__main__":
    unittest.main()
