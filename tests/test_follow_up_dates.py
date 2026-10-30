from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack

BEFORE = "Follow-up date cannot be before the submission date"


def run_cli(database, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


class FollowUpDateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.database = self.folder / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def add(self, follow_up_on="2026-09-15", applied_on="2026-09-01"):
        return applytrack.add_application(self.connection, "Example", "Engineer", follow_up_on,
                                          applied_on=applied_on)

    def row(self, identifier):
        return dict(applytrack.get_application(self.connection, identifier))

    def count(self):
        return self.connection.execute("SELECT count(*) FROM applications").fetchone()[0]

    def test_follow_up_on_or_after_the_submission_date_is_saved(self):
        same_day = self.add("2026-09-01")
        later = self.add("2026-09-15")
        self.assertEqual(self.row(same_day)["follow_up_on"], "2026-09-01")
        self.assertEqual(self.row(later)["follow_up_on"], "2026-09-15")

    def test_follow_up_before_the_submission_date_is_rejected_without_saving(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        for follow_up_on, applied_on, shown in (
            ("2026-08-31", "2026-09-01", "2026-09-01"),
            (yesterday, None, date.today().isoformat()),
        ):
            with self.subTest(applied_on=applied_on):
                with self.assertRaisesRegex(ValueError, rf"^{BEFORE} \({shown}\)\.$"):
                    self.add(follow_up_on, applied_on)
        self.assertEqual(self.count(), 0)

    def test_update_checks_a_new_follow_up_against_the_saved_or_new_submission_date(self):
        identifier = self.add()
        original = self.row(identifier)
        for changes, shown in (
            ({"follow_up_on": "2026-08-31"}, "2026-09-01"),
            ({"follow_up_on": "2026-09-04", "applied_on": "2026-09-05"}, "2026-09-05"),
            ({"follow_up_on": "2026-08-31", "status": "interviewing", "company": "Changed"}, "2026-09-01"),
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, rf"^{BEFORE} \({shown}\)\.$"):
                    applytrack.update_application(self.connection, identifier, **changes)
                self.assertEqual(self.row(identifier), original)
        self.assertEqual(applytrack.status_changes(self.connection, identifier), [])
        applytrack.update_application(self.connection, identifier, follow_up_on="2026-09-05", applied_on="2026-09-05")
        self.assertEqual((self.row(identifier)["applied_on"], self.row(identifier)["follow_up_on"]),
                         ("2026-09-05", "2026-09-05"))

    def test_submission_date_cannot_move_after_the_saved_follow_up(self):
        identifier = self.add("2026-09-10")
        original = self.row(identifier)
        with self.assertRaisesRegex(ValueError, r"^Submission date cannot be after the follow-up date \(2026-09-10\)\. "
                                                r"Change or clear the follow-up as well\.$"):
            applytrack.update_application(self.connection, identifier, applied_on="2026-09-11")
        self.assertEqual(self.row(identifier), original)
        applytrack.update_application(self.connection, identifier, applied_on="2026-09-10")
        applytrack.update_application(self.connection, identifier, applied_on="2026-08-20")
        self.assertEqual(self.row(identifier)["applied_on"], "2026-08-20")

    def test_submission_date_can_move_later_when_the_follow_up_moves_or_is_cleared_too(self):
        moved = self.add("2026-09-10")
        applytrack.update_application(self.connection, moved, applied_on="2026-09-12", follow_up_on="2026-09-20")
        self.assertEqual((self.row(moved)["applied_on"], self.row(moved)["follow_up_on"]),
                         ("2026-09-12", "2026-09-20"))
        cleared = self.add("2026-09-10")
        applytrack.update_application(self.connection, cleared, None, None, True, applied_on="2026-09-12")
        self.assertEqual((self.row(cleared)["applied_on"], self.row(cleared)["follow_up_on"]), ("2026-09-12", None))
        unset = self.add(None)
        applytrack.update_application(self.connection, unset, applied_on="2026-09-12")
        self.assertEqual(self.row(unset)["applied_on"], "2026-09-12")

    def test_import_rejects_a_follow_up_before_the_submission_date_and_saves_nothing(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        for row, shown in (("Second,Engineer,2026-09-01,2026-08-31", "2026-09-01"),
                           (f"Second,Engineer,,{yesterday}", date.today().isoformat())):
            source = f"company,role,applied_on,follow_up_on\nFirst,Engineer,2026-09-01,2026-09-01\n{row}\n"
            for dry_run in (True, False):
                with self.subTest(row=row, dry_run=dry_run):
                    with self.assertRaisesRegex(ValueError, rf"^Row 3: {BEFORE} \({shown}\)\.$"):
                        applytrack.import_csv(self.connection, io.StringIO(source), dry_run=dry_run)
        self.assertEqual(self.count(), 0)

    def test_cli_reports_follow_ups_before_the_submission_date(self):
        self.assertEqual(run_cli(self.database, "add", "Example", "Engineer", "--applied-on", "2026-09-01",
                                 "--follow-up", "2026-08-31"),
                         (1, "", f"Error: {BEFORE} (2026-09-01).\n"))
        self.assertEqual(run_cli(self.database, "add", "Example", "Engineer", "--applied-on", "2026-09-01",
                                 "--follow-up", "2026-09-08")[0], 0)
        for arguments, message in (
            (("--follow-up", "2026-08-31"), f"{BEFORE} (2026-09-01)."),
            (("--applied-on", "2026-09-09"),
             "Submission date cannot be after the follow-up date (2026-09-08). Change or clear the follow-up as well."),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(run_cli(self.database, "update", "1", *arguments), (1, "", f"Error: {message}\n"))
        self.assertEqual(run_cli(self.database, "update", "1", "--applied-on", "2026-09-09", "--clear-follow-up"),
                         (0, "Updated application #1.\n", ""))


class EarlierFollowUpTests(unittest.TestCase):
    """Applications saved before the rule keep working until either date is changed."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.database = self.folder / "applications.sqlite3"
        applytrack.connect(self.database).close()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO applications (company, role, applied_on, follow_up_on) "
                               "VALUES ('Earlier', 'Engineer', '2026-09-10', '2026-09-05')")
            connection.commit()
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def test_earlier_application_can_be_listed_and_updated_in_other_ways(self):
        due = applytrack.list_applications(self.connection, due_only=True, today=date(2026, 9, 10))
        self.assertEqual([row["id"] for row in due], [1])
        applytrack.update_application(self.connection, 1, status="interviewing", company="Earlier Labs")
        row = applytrack.get_application(self.connection, 1)
        self.assertEqual((row["company"], row["status"], row["follow_up_on"]),
                         ("Earlier Labs", "interviewing", "2026-09-05"))
        self.assertEqual(run_cli(self.database, "archive", "1")[0], 0)

    def test_changing_either_date_of_an_earlier_application_applies_the_rule(self):
        with self.assertRaisesRegex(ValueError, BEFORE):
            applytrack.update_application(self.connection, 1, follow_up_on="2026-09-06")
        with self.assertRaisesRegex(ValueError, "Submission date cannot be after the follow-up date"):
            applytrack.update_application(self.connection, 1, applied_on="2026-09-08")
        self.assertEqual(applytrack.get_application(self.connection, 1)["follow_up_on"], "2026-09-05")
        applytrack.update_application(self.connection, 1, follow_up_on="2026-09-17")
        self.assertEqual(applytrack.get_application(self.connection, 1)["follow_up_on"], "2026-09-17")

    def test_backup_of_an_earlier_application_still_restores(self):
        path = self.folder / "backup.json"
        self.assertEqual(run_cli(self.database, "backup", str(path))[0], 0)
        restored = self.folder / "restored.sqlite3"
        self.assertEqual(run_cli(restored, "restore-backup", str(path)),
                         (0, "Restored 1 application, 0 status changes, and 0 notes.\n", ""))
        with closing(applytrack.connect(restored)) as connection:
            row = applytrack.get_application(connection, 1)
            self.assertEqual((row["applied_on"], row["follow_up_on"]), ("2026-09-10", "2026-09-05"))
        self.assertEqual(json.loads(path.read_text())["applications"][0]["follow_up_on"], "2026-09-05")


if __name__ == "__main__":
    unittest.main()
