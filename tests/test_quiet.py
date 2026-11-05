from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack
from applytrack.activity import MAX_DAYS, check_days, quiet_applications

TODAY = date(2026, 10, 1)


def days_ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


def run_cli(database, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


class QuietApplicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def add(self, company, applied_on, **details):
        return applytrack.add_application(self.connection, company, "Engineer", applied_on=applied_on, **details)

    def quiet(self, days=14, today=TODAY):
        return [(item.application["id"], item.last_active_on, item.activity, item.days)
                for item in quiet_applications(self.connection, days, today)]

    def test_submission_alone_counts_as_activity(self):
        old = self.add("Old", "2026-09-01")
        self.add("Recent", "2026-09-25")
        self.assertEqual(self.quiet(), [(old, "2026-09-01", "submitted", 30)])

    def test_latest_status_change_or_note_resets_the_clock(self):
        changed = self.add("Changed", "2026-08-01")
        noted = self.add("Noted", "2026-08-01")
        applytrack.update_application(self.connection, changed, "interviewing", changed_on="2026-09-10")
        applytrack.add_note(self.connection, noted, "Recruiter call.", "2026-09-05")
        applytrack.add_note(self.connection, noted, "Earlier note.", "2026-08-10")
        self.assertEqual(self.quiet(), [
            (noted, "2026-09-05", "note", 26),
            (changed, "2026-09-10", "became interviewing", 21),
        ])
        self.assertEqual(self.quiet(days=25), [(noted, "2026-09-05", "note", 26)])

    def test_boundary_is_inclusive_and_zero_days_includes_today(self):
        exact = self.add("Exact", "2026-09-17")
        self.add("Day short", "2026-09-18")
        self.assertEqual(self.quiet(), [(exact, "2026-09-17", "submitted", 14)])
        today = self.add("Today", date.today().isoformat())
        self.assertIn(today, [identifier for identifier, *_ in self.quiet(days=0, today=date.today())])
        self.assertNotIn(today, [identifier for identifier, *_ in self.quiet(days=1, today=date.today())])

    def test_note_before_the_submission_does_not_count_as_later_activity(self):
        identifier = self.add("Referral", "2026-09-01")
        applytrack.add_note(self.connection, identifier, "Referral from a former colleague.", "2026-08-20")
        self.assertEqual(self.quiet(), [(identifier, "2026-09-01", "submitted", 30)])

    def test_same_day_activity_names_the_status_change_first(self):
        identifier = self.add("Busy", "2026-09-01")
        applytrack.add_note(self.connection, identifier, "Same day as submitting.", "2026-09-01")
        self.assertEqual(self.quiet(), [(identifier, "2026-09-01", "note", 30)])
        applytrack.update_application(self.connection, identifier, "interviewing", changed_on="2026-09-01")
        self.assertEqual(self.quiet(), [(identifier, "2026-09-01", "became interviewing", 30)])

    def test_latest_of_several_same_day_changes_is_named(self):
        identifier = self.add("Changes", "2026-09-01")
        for status in ("interviewing", "offer"):
            applytrack.update_application(self.connection, identifier, status, changed_on="2026-09-03")
        self.assertEqual(self.quiet(), [(identifier, "2026-09-03", "became offer", 28)])

    def test_closed_and_archived_applications_are_left_out(self):
        waiting = self.add("Waiting", "2026-08-01")
        offer = self.add("Offer", "2026-08-01")
        applytrack.update_application(self.connection, offer, "offer", changed_on="2026-08-05")
        for status in ("rejected", "withdrawn"):
            closed = self.add(status, "2026-08-01")
            applytrack.update_application(self.connection, closed, status, changed_on="2026-08-05")
        archived = self.add("Archived", "2026-08-01")
        applytrack.archive_application(self.connection, archived)
        self.assertEqual([identifier for identifier, *_ in self.quiet()], [waiting, offer])

    def test_follow_up_date_does_not_hide_a_quiet_application(self):
        identifier = self.add("Scheduled", "2026-09-01", follow_up_on="2026-10-20")
        found = quiet_applications(self.connection, 14, TODAY)
        self.assertEqual([item.application["follow_up_on"] for item in found], ["2026-10-20"])
        self.assertEqual(self.quiet()[0][0], identifier)

    def test_quietest_first_then_by_id(self):
        second = self.add("Second", "2026-09-01")
        first = self.add("First", "2026-08-01")
        tied = self.add("Tied", "2026-09-01")
        self.assertEqual([identifier for identifier, *_ in self.quiet()], [first, second, tied])

    def test_imported_status_without_a_date_falls_back_to_the_submission(self):
        csv = io.StringIO("company,role,status,applied_on\nImported,Engineer,interviewing,2026-09-01\n")
        applytrack.import_csv(self.connection, csv)
        self.assertEqual(self.quiet(), [(1, "2026-09-01", "submitted", 30)])

    def test_invalid_day_counts_are_rejected(self):
        self.assertEqual(check_days(0), 0)
        self.assertEqual(check_days(MAX_DAYS), MAX_DAYS)
        for days in (-1, MAX_DAYS + 1, 1.5, "14", True):
            with self.subTest(days=days), self.assertRaisesRegex(ValueError, "Days must be a whole number"):
                quiet_applications(self.connection, days, TODAY)

    def test_works_on_a_database_upgraded_from_before_history_and_notes(self):
        legacy = Path(self.database).with_name("legacy.sqlite3")
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute(
                """CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL,
                   role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'applied', applied_on TEXT NOT NULL,
                   follow_up_on TEXT, archived_on TEXT)"""
            )
            connection.execute("INSERT INTO applications (company, role, status, applied_on)"
                               " VALUES ('Legacy', 'Engineer', 'interviewing', '2026-08-01')")
        with closing(applytrack.connect(legacy)) as connection:
            found = quiet_applications(connection, 14, TODAY)
        self.assertEqual([(item.last_active_on, item.activity) for item in found], [("2026-08-01", "submitted")])


class QuietCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"

    def test_lists_quiet_applications_with_their_last_activity(self):
        run_cli(self.database, "add", "Example Company", "Software Engineer", "--applied-on", days_ago(40))
        run_cli(self.database, "add", "Example Labs", "Backend Engineer", "--applied-on", days_ago(30),
                "--follow-up", days_ago(1))
        run_cli(self.database, "update", "2", "--status", "interviewing", "--changed-on", days_ago(20))
        run_cli(self.database, "add", "Other Co", "Engineer", "--applied-on", days_ago(40))
        run_cli(self.database, "note", "3", "Spoke to the recruiter.", "--on", days_ago(3))
        self.assertEqual(run_cli(self.database, "quiet"), (0, (
            "#1  Example Company — Software Engineer\n"
            f"    applied | Quiet for 40 days, since {days_ago(40)} (submitted) | Follow up: not set\n"
            "#2  Example Labs — Backend Engineer\n"
            f"    interviewing | Quiet for 20 days, since {days_ago(20)} (became interviewing)"
            f" | Follow up: {days_ago(1)}\n"
        ), ""))
        code, output, _ = run_cli(self.database, "quiet", "--days", "3")
        self.assertEqual(code, 0)
        self.assertIn(f"    applied | Quiet for 3 days, since {days_ago(3)} (note) | Follow up: not set\n", output)
        self.assertEqual(output.count("#"), 3)

    def test_days_option_and_singular_wording(self):
        run_cli(self.database, "add", "Example", "Engineer", "--applied-on", days_ago(1))
        self.assertEqual(run_cli(self.database, "quiet"),
                         (0, "No open applications have been quiet for 14 days or more.\n", ""))
        self.assertEqual(run_cli(self.database, "quiet", "--days", "1"), (0, (
            "#1  Example — Engineer\n"
            f"    applied | Quiet for 1 day, since {days_ago(1)} (submitted) | Follow up: not set\n"
        ), ""))
        self.assertEqual(run_cli(self.database, "quiet", "--days", "2"),
                         (0, "No open applications have been quiet for 2 days or more.\n", ""))

    def test_empty_database(self):
        self.assertEqual(run_cli(self.database, "quiet", "--days", "0"),
                         (0, "No open applications have been quiet for 0 days or more.\n", ""))

    def test_escapes_control_characters_in_names(self):
        run_cli(self.database, "add", "Evil\x1b[2JCo", "Engineer", "--applied-on", days_ago(20))
        code, output, _ = run_cli(self.database, "quiet")
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith("#1  Evil\\x1b[2JCo — Engineer\n"))

    def test_out_of_range_days_are_an_error(self):
        for days in ("-1", "3651"):
            with self.subTest(days=days):
                self.assertEqual(run_cli(self.database, "quiet", "--days", days),
                                 (1, "", "Error: Days must be a whole number from 0 to 3650.\n"))

    def test_non_numeric_days_are_rejected_by_argparse(self):
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as raised:
            applytrack.main(["--database", str(self.database), "quiet", "--days", "two"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid int value", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
