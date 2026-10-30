from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date
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


class UndoStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)
        self.identifier = applytrack.add_application(self.connection, "Example", "Engineer",
                                                     "2026-09-25", applied_on="2026-09-01")

    def update(self, status, changed_on, identifier=None):
        applytrack.update_application(self.connection, identifier or self.identifier, status=status,
                                      changed_on=changed_on)

    def undo(self, identifier=None):
        return applytrack.undo_status_change(self.connection, identifier or self.identifier)

    def state(self, identifier=None):
        identifier = identifier or self.identifier
        row = applytrack.get_application(self.connection, identifier)
        changes = applytrack.status_changes(self.connection, identifier)
        return row["status"], [(change["changed_on"], change["status"]) for change in changes]

    def test_undo_restores_each_earlier_status_in_turn(self):
        self.update("interviewing", "2026-09-10")
        self.update("rejected", "2026-09-20")
        self.assertEqual(self.undo(), ("rejected", "2026-09-20", "interviewing"))
        self.assertEqual(self.state(), ("interviewing", [("2026-09-10", "interviewing")]))
        self.assertEqual(self.undo(), ("interviewing", "2026-09-10", "applied"))
        self.assertEqual(self.state(), ("applied", []))
        with self.assertRaisesRegex(ValueError, "Application 1 has no recorded status change to undo."):
            self.undo()

    def test_undo_keeps_dates_names_archive_and_other_applications(self):
        other = applytrack.add_application(self.connection, "Other", "Engineer", applied_on="2026-09-01")
        self.update("interviewing", "2026-09-10", other)
        self.update("offer", "2026-09-12")
        applytrack.archive_application(self.connection, self.identifier, today=date(2026, 9, 30))
        before = dict(applytrack.get_application(self.connection, self.identifier))
        self.undo()
        self.assertEqual(dict(applytrack.get_application(self.connection, self.identifier)),
                         {**before, "status": "applied"})
        self.assertEqual(self.state(other), ("interviewing", [("2026-09-10", "interviewing")]))

    def test_undo_of_the_latest_of_several_same_day_changes(self):
        for status in ("interviewing", "rejected", "interviewing"):
            self.update(status, "2026-09-12")
        self.assertEqual(self.undo(), ("interviewing", "2026-09-12", "rejected"))
        self.assertEqual(self.state(), ("rejected", [("2026-09-12", "interviewing"), ("2026-09-12", "rejected")]))

    def test_a_change_with_the_wrong_date_can_be_undone_and_recorded_again(self):
        self.update("interviewing", "2026-09-20")
        with self.assertRaisesRegex(ValueError, "before the last recorded change"):
            self.update("rejected", "2026-09-15")
        self.undo()
        self.update("interviewing", "2026-09-12")
        self.update("rejected", "2026-09-15")
        self.assertEqual(self.state(), ("rejected", [("2026-09-12", "interviewing"), ("2026-09-15", "rejected")]))

    def test_undo_frees_the_submission_date_held_by_the_first_change(self):
        self.update("interviewing", "2026-09-10")
        self.undo()
        applytrack.update_application(self.connection, self.identifier, applied_on="2026-09-15")
        self.assertEqual(applytrack.get_application(self.connection, self.identifier)["applied_on"], "2026-09-15")

    def test_undo_returns_an_imported_application_to_its_imported_status(self):
        applytrack.import_csv(self.connection, io.StringIO(
            "company,role,status,applied_on\nImported,Analyst,interviewing,2026-09-02\n"))
        imported = applytrack.list_applications(self.connection)[0]["id"]
        self.update("offer", "2026-09-15", imported)
        self.assertEqual(self.undo(imported), ("offer", "2026-09-15", "interviewing"))
        self.assertEqual(self.state(imported), ("interviewing", []))

    def test_status_not_set_by_a_recorded_change_is_not_undone(self):
        self.update("interviewing", "2026-09-10")
        with self.connection:
            self.connection.execute("UPDATE applications SET status = 'offer' WHERE id = ?", (self.identifier,))
        with self.assertRaisesRegex(ValueError, "status, offer, was not set by a recorded change"):
            self.undo()
        self.assertEqual(self.state(), ("offer", [("2026-09-10", "interviewing")]))

    def test_unknown_application_is_reported(self):
        with self.assertRaisesRegex(ValueError, "Application 999 does not exist."):
            self.undo(999)
        self.assertFalse(self.connection.in_transaction)

    def test_failure_while_removing_the_change_keeps_the_status(self):
        self.update("interviewing", "2026-09-10")
        with self.connection:
            self.connection.execute("CREATE TRIGGER refuse BEFORE UPDATE OF status ON applications"
                                    " BEGIN SELECT RAISE(ABORT, 'refused'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "refused"):
            self.undo()
        self.assertEqual(self.state(), ("interviewing", [("2026-09-10", "interviewing")]))

    def test_stats_no_longer_count_an_undone_response(self):
        self.update("rejected", "2026-09-10")
        self.assertEqual(applytrack.summarize(self.connection).responded, 1)
        self.undo()
        summary = applytrack.summarize(self.connection)
        self.assertEqual((summary.responded, summary.by_status["applied"]), (0, 1))

    def test_restored_backup_undoes_all_but_its_first_change(self):
        self.update("interviewing", "2026-09-10")
        self.update("offer", "2026-09-20")
        backup = io.StringIO()
        applytrack.write_backup(applytrack.snapshot(self.connection), backup)
        restored = applytrack.connect(self.database.with_name("restored.sqlite3"))
        self.addCleanup(restored.close)
        applytrack.restore_backup(restored, *applytrack.read_backup(io.StringIO(backup.getvalue())))
        self.assertEqual(applytrack.undo_status_change(restored, self.identifier),
                         ("offer", "2026-09-20", "interviewing"))
        with self.assertRaisesRegex(ValueError, "status before the change to interviewing on 2026-09-10 was not "
                                                "recorded, so it cannot be undone."):
            applytrack.undo_status_change(restored, self.identifier)
        self.assertEqual(applytrack.get_application(restored, self.identifier)["status"], "interviewing")

    def test_cli_undoes_a_change_and_shows_the_shorter_history(self):
        run_cli(self.database, "update", "1", "--status", "interviewing", "--changed-on", "2026-09-10")
        run_cli(self.database, "update", "1", "--status", "rejected", "--changed-on", "2026-09-20")
        self.assertEqual(run_cli(self.database, "undo-status", "1"), (0, (
            "Removed the change to rejected on 2026-09-20; application #1 is interviewing again.\n"), ""))
        self.assertEqual(run_cli(self.database, "history", "1"), (0, (
            "#1  Example — Engineer\n"
            "    2026-09-01  applied\n"
            "    2026-09-10  interviewing\n"
        ), ""))

    def test_cli_reports_what_cannot_be_undone(self):
        for arguments, message in (
            (("undo-status", "1"), "Application 1 has no recorded status change to undo."),
            (("undo-status", "999"), "Application 999 does not exist."),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(run_cli(self.database, *arguments), (1, "", f"Error: {message}\n"))
        for arguments in (["undo-status"], ["undo-status", "one"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    applytrack.main(["--database", str(self.database), *arguments])
                self.assertEqual(raised.exception.code, 2)


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

    def test_legacy_changes_undo_back_to_the_first_one(self):
        connection = applytrack.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(applytrack.undo_status_change(connection, 1), ("offer", "2026-09-20", "interviewing"))
        with self.assertRaisesRegex(ValueError, "was not recorded, so it cannot be undone"):
            applytrack.undo_status_change(connection, 1)
        with self.assertRaisesRegex(ValueError, "Application 2 has no recorded status change to undo."):
            applytrack.undo_status_change(connection, 2)
        self.assertEqual(self.raw("SELECT id, status FROM applications ORDER BY id"),
                         [(1, "interviewing"), (2, "interviewing")])
        self.assertEqual(self.raw("SELECT status FROM status_changes"), [("interviewing",)])

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
