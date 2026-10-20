from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack


# The table as created by the version before status history was added.
PREVIOUS_TABLE = """CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL CHECK(length(trim(company)) > 0),
    role TEXT NOT NULL CHECK(length(trim(role)) > 0),
    status TEXT NOT NULL DEFAULT 'applied'
        CHECK(status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')),
    applied_on TEXT NOT NULL,
    follow_up_on TEXT,
    archived_on TEXT
)"""

PREVIOUS_ROWS = [
    (1, "First", "Engineer", "applied", "2026-09-01", None, None),
    (2, "O'Brien's", "Backend Engineer", "interviewing", "2026-09-02", "2026-09-20", "2026-09-30"),
]


def run_cli(database, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)
        self.identifier = applytrack.add_application(self.connection, "Example", "Engineer",
                                                     applied_on="2026-09-01")

    def changes(self, connection=None):
        rows = applytrack.status_changes(connection or self.connection, self.identifier)
        return [(row["changed_on"], row["status"]) for row in rows]

    def timeline(self):
        row = applytrack.get_application(self.connection, self.identifier)
        return applytrack.timeline(row, applytrack.status_changes(self.connection, self.identifier))

    def update(self, **changes):
        applytrack.update_application(self.connection, self.identifier, **changes)

    def test_status_changes_are_dated_and_survive_reopening(self):
        self.update(status="interviewing", changed_on="2026-09-12")
        self.update(status="offer", today=date(2026, 9, 30))
        reopened = applytrack.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(self.changes(reopened), [("2026-09-12", "interviewing"), ("2026-09-30", "offer")])
        self.assertEqual(self.timeline(),
                         [("2026-09-01", "applied"), ("2026-09-12", "interviewing"), ("2026-09-30", "offer")])

    def test_change_date_defaults_to_the_local_date(self):
        self.update(status="interviewing")
        self.assertEqual(self.changes(), [(date.today().isoformat(), "interviewing")])

    def test_only_a_different_status_is_recorded(self):
        self.update(status="applied", today=date(2026, 9, 5))
        self.update(company="Example Labs", follow_up_on="2026-09-20", applied_on="2026-08-30")
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.timeline(), [("2026-08-30", "applied")])

    def test_changes_on_the_same_day_keep_the_order_they_were_made(self):
        for status in ("interviewing", "rejected", "interviewing"):
            self.update(status=status, changed_on="2026-09-12")
        self.assertEqual([status for _, status in self.changes()], ["interviewing", "rejected", "interviewing"])

    def test_invalid_change_dates_leave_the_application_and_history_unchanged(self):
        self.update(status="interviewing", changed_on="2026-09-12")
        before = dict(applytrack.get_application(self.connection, self.identifier))
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for changes, message in (
            ({"status": "offer", "changed_on": "2026-9-20"}, "valid date in YYYY-MM-DD"),
            ({"status": "offer", "changed_on": tomorrow}, "Status change date cannot be in the future."),
            ({"status": "offer", "changed_on": "2026-09-11"},
             r"cannot be before the last recorded change \(2026-09-12\)\."),
            ({"changed_on": "2026-09-20"}, "A status change date needs a new status."),
            ({"changed_on": "2026-09-20", "company": "Other"}, "A status change date needs a new status."),
            ({"status": "interviewing", "changed_on": "2026-09-20"},
             "already has status interviewing; there is no change to date."),
            ({"status": "offer", "changed_on": "2026-09-20", "role": " "}, "Role cannot be empty."),
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, message):
                    self.update(**changes)
                self.assertEqual(dict(applytrack.get_application(self.connection, self.identifier)), before)
                self.assertEqual(self.changes(), [("2026-09-12", "interviewing")])

    def test_change_date_cannot_precede_the_submission_date(self):
        with self.assertRaisesRegex(ValueError, "cannot be before the submission date."):
            self.update(status="interviewing", changed_on="2026-08-31")
        with self.assertRaisesRegex(ValueError, "cannot be before the submission date."):
            self.update(status="interviewing", changed_on="2026-08-25", applied_on="2026-08-26")
        self.assertEqual(applytrack.get_application(self.connection, self.identifier)["applied_on"], "2026-09-01")
        self.update(status="interviewing", changed_on="2026-08-26", applied_on="2026-08-26")
        self.assertEqual(self.timeline(), [("2026-08-26", "applied"), ("2026-08-26", "interviewing")])

    def test_submission_date_cannot_move_after_the_first_recorded_change(self):
        self.update(status="interviewing", changed_on="2026-09-12")
        with self.assertRaisesRegex(ValueError, r"after the first recorded status change \(2026-09-12\)\."):
            self.update(applied_on="2026-09-13")
        self.assertEqual(applytrack.get_application(self.connection, self.identifier)["applied_on"], "2026-09-01")
        self.update(applied_on="2026-09-12")
        self.update(applied_on="2026-08-15")
        self.assertEqual(self.timeline()[0], ("2026-08-15", "applied"))

    def test_unknown_application_is_reported_before_anything_is_saved(self):
        with self.assertRaisesRegex(ValueError, "Application 999 does not exist."):
            applytrack.update_application(self.connection, 999, status="offer", changed_on="2026-09-20")
        self.assertEqual(applytrack.status_changes(self.connection, 999), [])

    def test_failure_while_recording_history_rolls_back_the_status(self):
        with self.connection:
            self.connection.execute(
                "CREATE TRIGGER refuse BEFORE INSERT ON status_changes BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "refused"):
            self.update(status="interviewing", follow_up_on="2026-09-20")
        row = applytrack.get_application(self.connection, self.identifier)
        self.assertEqual((row["status"], row["follow_up_on"]), ("applied", None))

    def test_deleting_an_application_removes_its_history(self):
        other = applytrack.add_application(self.connection, "Other", "Engineer", applied_on="2026-09-01")
        applytrack.update_application(self.connection, other, status="rejected", changed_on="2026-09-03")
        self.update(status="interviewing", changed_on="2026-09-12")
        applytrack.delete_application(self.connection, self.identifier)
        remaining = self.connection.execute("SELECT application_id, status FROM status_changes").fetchall()
        self.assertEqual([tuple(row) for row in remaining], [(other, "rejected")])

    def test_imported_and_archived_applications_keep_their_history(self):
        applytrack.import_csv(self.connection, io.StringIO(
            "company,role,status,applied_on\nImported,Engineer,interviewing,2026-09-02\n"))
        imported = applytrack.list_applications(self.connection)[0]
        self.assertEqual(applytrack.timeline(imported, []), [("2026-09-02", "applied"), (None, "interviewing")])
        self.update(status="interviewing", changed_on="2026-09-12")
        applytrack.archive_application(self.connection, self.identifier)
        self.update(status="rejected", changed_on="2026-09-20")
        applytrack.restore_application(self.connection, self.identifier)
        self.assertEqual(self.changes(), [("2026-09-12", "interviewing"), ("2026-09-20", "rejected")])

    def test_cli_records_and_shows_history(self):
        self.assertEqual(run_cli(self.database, "update", "1", "--status", "interviewing", "--changed-on",
                                 "2026-09-12"), (0, "Updated application #1.\n", ""))
        run_cli(self.database, "update", "1", "--status", "rejected")
        self.assertEqual(run_cli(self.database, "history", "1"), (0, (
            "#1  Example — Engineer\n"
            "    2026-09-01  applied\n"
            "    2026-09-12  interviewing\n"
            f"    {date.today().isoformat()}  rejected\n"
        ), ""))

    def test_cli_shows_an_undated_status_from_an_import(self):
        source = Path(self.database).with_name("applications.csv")
        source.write_text("company,role,status,applied_on\nImported,Engineer,offer,2026-09-02\n")
        run_cli(self.database, "import", str(source))
        self.assertEqual(run_cli(self.database, "history", "2")[1],
                         "#2  Imported — Engineer\n    2026-09-02  applied\n    unknown     offer\n")

    def test_cli_reports_invalid_history_requests(self):
        for arguments, message in (
            (("history", "999"), "Application 999 does not exist."),
            (("update", "1", "--changed-on", "2026-09-12"), "A status change date needs a new status."),
            (("update", "1", "--status", "offer", "--changed-on", "2026-08-01"),
             "Status change date cannot be before the submission date."),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(run_cli(self.database, *arguments), (1, "", f"Error: {message}\n"))
        self.assertEqual(self.changes(), [])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            applytrack.main(["--database", str(self.database), "history"])


class HistoryUpgradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(PREVIOUS_TABLE)
            connection.executemany("INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?, ?)", PREVIOUS_ROWS)
            connection.commit()

    def raw(self, query):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(query).fetchall()

    def schema(self):
        return self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def test_previous_database_gains_history_and_keeps_its_records(self):
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(self.raw("SELECT * FROM applications ORDER BY id"), PREVIOUS_ROWS)
        self.assertEqual(self.raw("SELECT count(*) FROM status_changes"), [(0,)])
        archived = applytrack.get_application(connection, 2)
        self.assertEqual(applytrack.timeline(archived, []), [("2026-09-02", "applied"), (None, "interviewing")])
        applytrack.update_application(connection, 2, status="offer", changed_on="2026-10-01")
        upgraded = applytrack.get_application(connection, 2)
        self.assertEqual(applytrack.timeline(upgraded, applytrack.status_changes(connection, 2)),
                         [("2026-09-02", "applied"), ("2026-10-01", "offer")])

    def test_database_from_before_unique_ids_gets_every_upgrade_in_order(self):
        self.database.unlink()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(PREVIOUS_TABLE.replace(" AUTOINCREMENT", "").replace(",\n    archived_on TEXT", ""))
            connection.executemany("INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?)",
                                   [row[:-1] for row in PREVIOUS_ROWS])
            connection.commit()
        self.assertNotIn("archived_on", self.raw("SELECT sql FROM sqlite_master")[0][0])
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(self.raw("SELECT * FROM applications ORDER BY id"),
                         [(*row[:-1], None) for row in PREVIOUS_ROWS])
        applytrack.update_application(connection, 1, status="rejected", changed_on="2026-09-05")
        self.assertEqual(self.raw("SELECT application_id, status, changed_on FROM status_changes"),
                         [(1, "rejected", "2026-09-05")])

    def test_reopening_an_upgraded_database_changes_nothing(self):
        applytrack.connect(self.database).close()
        schema = self.schema()
        self.assertEqual([name for kind, name, _ in schema if kind in ("table", "index")],
                         ["applications", "sqlite_sequence", "status_changes", "status_changes_by_application"])
        applytrack.connect(self.database).close()
        self.assertEqual(self.schema(), schema)

    def test_cli_shows_history_for_a_previous_database(self):
        self.assertEqual(run_cli(self.database, "history", "2"),
                         (0, "#2  O'Brien's — Backend Engineer\n    2026-09-02  applied\n"
                             "    unknown     interviewing\n", ""))

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_failed_upgrade_is_reported_and_leaves_the_database_unchanged(self):
        schema = self.schema()
        self.database.chmod(0o444)
        self.addCleanup(self.database.chmod, 0o644)
        code, output, errors = run_cli(self.database, "history", "1")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Error: attempt to write a readonly database", errors)
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.raw("SELECT * FROM applications ORDER BY id"), PREVIOUS_ROWS)


if __name__ == "__main__":
    unittest.main()
