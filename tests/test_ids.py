from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack


# The table as created before IDs were kept unique.
LEGACY_TABLE = """CREATE TABLE applications (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL CHECK(length(trim(company)) > 0),
    role TEXT NOT NULL CHECK(length(trim(role)) > 0),
    status TEXT NOT NULL DEFAULT 'applied'
        CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
    applied_on TEXT NOT NULL,
    follow_up_on TEXT
)"""

LEGACY_ROWS = [
    (1, "First", "Engineer", "applied", "2026-09-01", None),
    (2, "O'Brien's", "Backend Engineer", "interviewing", "2026-09-02", "2026-09-20"),
    (5, "Third", "Analyst", "withdrawn", "2026-09-03", "2026-09-10"),
]


class IdentifierTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"

    def open(self):
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        return connection

    def create_legacy_database(self, rows=LEGACY_ROWS, *, unchecked=False):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(LEGACY_TABLE)
            if unchecked:
                connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.executemany("INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?)", rows)
            connection.commit()

    def raw(self, query):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(query).fetchall()

    def table_sql(self):
        return self.raw("SELECT sql FROM sqlite_master WHERE name = 'applications'")[0][0]

    def run_cli(self, *arguments):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = applytrack.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_deleted_highest_id_is_not_reused(self):
        connection = self.open()
        first = applytrack.add_application(connection, "First", "Engineer")
        second = applytrack.add_application(connection, "Second", "Engineer")
        applytrack.delete_application(connection, second)
        third = applytrack.add_application(connection, "Third", "Engineer")
        self.assertEqual((first, second, third), (1, 2, 3))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            applytrack.get_application(connection, second)

    def test_ids_stay_unique_across_reopening_and_imports(self):
        connection = self.open()
        identifier = applytrack.add_application(connection, "First", "Engineer")
        applytrack.delete_application(connection, identifier)
        connection.close()
        reopened = self.open()
        applytrack.import_csv(reopened, io.StringIO("company,role\nImported,Engineer\n"))
        self.assertEqual([row["id"] for row in applytrack.list_applications(reopened)], [2])

    def test_legacy_database_keeps_its_records_and_ids(self):
        self.create_legacy_database()
        connection = self.open()
        rows = [tuple(row) for row in reversed(applytrack.list_applications(connection))]
        self.assertEqual(rows, LEGACY_ROWS)
        self.assertIn("AUTOINCREMENT", self.table_sql())
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name LIKE '%migrated%'"), [])

    def test_legacy_database_stops_reusing_ids_after_upgrade(self):
        self.create_legacy_database()
        connection = self.open()
        applytrack.delete_application(connection, 5)
        self.assertEqual(applytrack.add_application(connection, "Next", "Engineer"), 6)

    def test_upgraded_table_still_rejects_invalid_records(self):
        self.create_legacy_database()
        connection = self.open()
        for row in (("  ", "Engineer", "applied"), ("Example", "Engineer", "ghosted")):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                with connection:
                    connection.execute(
                        "INSERT INTO applications (company, role, status, applied_on) VALUES (?, ?, ?, ?)",
                        (*row, "2026-09-01"),
                    )
        self.assertEqual(len(applytrack.list_applications(connection)), len(LEGACY_ROWS))

    def test_empty_legacy_database_is_upgraded(self):
        self.create_legacy_database(rows=[])
        connection = self.open()
        self.assertIn("AUTOINCREMENT", self.table_sql())
        self.assertEqual(applytrack.add_application(connection, "First", "Engineer"), 1)

    def test_reopening_an_upgraded_database_changes_nothing(self):
        self.create_legacy_database()
        self.open().close()
        schema = self.raw("SELECT sql FROM sqlite_master ORDER BY name")
        sequence = self.raw("SELECT * FROM sqlite_sequence")
        self.open().close()
        self.assertEqual(self.raw("SELECT sql FROM sqlite_master ORDER BY name"), schema)
        self.assertEqual(self.raw("SELECT * FROM sqlite_sequence"), sequence)
        self.assertEqual(sequence, [("applications", 5)])

    def test_failed_upgrade_leaves_the_legacy_table_unchanged(self):
        rows = [*LEGACY_ROWS, (6, "Broken", "Engineer", "ghosted", "2026-09-04", None)]
        self.create_legacy_database(rows, unchecked=True)
        with self.assertRaises(sqlite3.IntegrityError):
            applytrack.connect(self.database)
        self.assertNotIn("AUTOINCREMENT", self.table_sql())
        self.assertEqual(self.raw("SELECT * FROM applications ORDER BY id"), rows)
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name LIKE '%migrated%'"), [])

    def test_cli_works_with_a_legacy_database(self):
        self.create_legacy_database()
        code, output, errors = self.run_cli("list", "--status", "interviewing")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("#2  O'Brien's — Backend Engineer", output)
        self.assertEqual(self.run_cli("delete", "5", "--yes")[0], 0)
        self.assertEqual(self.run_cli("add", "Next", "Engineer")[1], "Added application #6.\n")

    def test_cli_reports_a_failed_upgrade(self):
        rows = [(1, "Broken", "Engineer", "ghosted", "2026-09-04", None)]
        self.create_legacy_database(rows, unchecked=True)
        code, output, errors = self.run_cli("list")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Error: CHECK constraint failed", errors)
        self.assertEqual(self.raw("SELECT * FROM applications"), rows)


if __name__ == "__main__":
    unittest.main()
