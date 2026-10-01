from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
from pathlib import Path
import tempfile
import unittest

import applytrack


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def test_records_survive_reopening_and_preserve_punctuation(self):
        company = "O'Brien's; DROP TABLE applications; --"
        identifier = applytrack.add_application(self.connection, company, "Software Engineer")
        reopened = applytrack.connect(self.database)
        self.addCleanup(reopened.close)
        rows = applytrack.list_applications(reopened)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], identifier)
        self.assertEqual(rows[0]["company"], company)
        self.assertEqual(rows[0]["status"], "applied")

    def test_due_queue_includes_today_and_overdue_but_excludes_closed_and_future(self):
        expected = []
        for name, deadline, status in (
            ("Overdue", "2026-09-08", "applied"),
            ("Today", "2026-09-09", "interviewing"),
            ("Future", "2026-09-10", "applied"),
            ("Rejected", "2026-09-08", "rejected"),
            ("Withdrawn", "2026-09-08", "withdrawn"),
            ("Unscheduled", None, "applied"),
        ):
            identifier = applytrack.add_application(self.connection, name, "Engineer", deadline)
            applytrack.update_application(self.connection, identifier, status=status)
            if name in ("Overdue", "Today"):
                expected.append(identifier)
        actual = applytrack.list_applications(self.connection, due_only=True, today=date(2026, 9, 9))
        self.assertEqual([row["id"] for row in actual], expected)

    def test_submission_date_defaults_to_today_and_accepts_today_explicitly(self):
        today = date.today().isoformat()
        applytrack.add_application(self.connection, "Default", "Engineer")
        applytrack.add_application(self.connection, "Explicit", "Engineer", applied_on=today)
        self.assertEqual([row["applied_on"] for row in applytrack.list_applications(self.connection)],
                         [today, today])

    def test_invalid_submission_dates_do_not_create_records(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        for value in ("", "2026-02-30", "20260901", future):
            with self.subTest(value=value), self.assertRaises(ValueError):
                applytrack.add_application(self.connection, "Example", "Engineer", applied_on=value)
        self.assertEqual(applytrack.list_applications(self.connection), [])

    def test_cli_historical_date_survives_update_list_and_export(self):
        arguments = ["--database", str(self.database)]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(applytrack.main(arguments + ["add", "Example", "Engineer",
                                                         "--applied-on", "2024-02-29"]), 0)
            self.assertEqual(applytrack.main(arguments + ["update", "1", "--role", "Backend Engineer"]), 0)
            self.assertEqual(applytrack.main(arguments + ["list"]), 0)
        self.assertIn("Applied: 2024-02-29", output.getvalue())
        exported = io.StringIO()
        with redirect_stdout(exported):
            self.assertEqual(applytrack.main(arguments + ["export"]), 0)
        self.assertIn(",2024-02-29,", exported.getvalue())
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(applytrack.main(arguments + ["add", "Invalid", "Engineer",
                                                         "--applied-on", "2024-02-30"]), 1)
        self.assertIn("valid date", errors.getvalue())
        self.assertEqual(len(applytrack.list_applications(self.connection)), 1)

    def test_invalid_input_does_not_create_records(self):
        for company, role, deadline in (
            ("   ", "Engineer", None),
            ("Example", "  ", None),
            ("Example", "Engineer", "2026-02-30"),
            ("Example", "Engineer", "20260909"),
        ):
            with self.subTest(company=company, role=role, deadline=deadline):
                with self.assertRaises(ValueError):
                    applytrack.add_application(self.connection, company, role, deadline)
        self.assertEqual(applytrack.list_applications(self.connection), [])

    def test_invalid_update_leaves_existing_status_and_date_unchanged(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer", "2026-09-10")
        with self.assertRaises(ValueError):
            applytrack.update_application(self.connection, identifier, "interviewing", "2026-02-30")
        row = applytrack.list_applications(self.connection)[0]
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["follow_up_on"], "2026-09-10")

    def test_follow_up_can_be_rescheduled_then_cleared_without_losing_status(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer", "2026-09-10")
        applytrack.update_application(self.connection, identifier, status="interviewing")
        applytrack.update_application(self.connection, identifier, follow_up_on="2026-09-12")
        row = applytrack.list_applications(self.connection)[0]
        self.assertEqual(row["follow_up_on"], "2026-09-12")
        applytrack.update_application(self.connection, identifier, clear_follow_up=True)
        row = applytrack.list_applications(self.connection)[0]
        self.assertEqual(row["status"], "interviewing")
        self.assertIsNone(row["follow_up_on"])

    def test_unknown_identifier_reports_failure(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            applytrack.update_application(self.connection, 999, status="interviewing")

    def test_list_preserves_unicode_but_escapes_terminal_control_characters(self):
        applytrack.add_application(self.connection, "Montréal 東京", "Engineer\nRemote\t\x1b[31m\u202eTitle")
        output = io.StringIO()
        with redirect_stdout(output):
            code = applytrack.main(["--database", str(self.database), "list"])
        self.assertEqual(code, 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], r"#1  Montréal 東京 — Engineer\nRemote\t\x1b[31m\u202eTitle")

    def test_company_and_role_edits_preserve_other_fields_and_survive_reopening(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer", "2026-09-10")
        original = dict(applytrack.list_applications(self.connection)[0])
        company = "O'Brien's; DROP TABLE applications; --"
        for changes in ({"company": "  " + company + "  "}, {"role": "  Backend Engineer  "}):
            applytrack.update_application(self.connection, identifier, **changes)
        reopened = applytrack.connect(self.database)
        self.addCleanup(reopened.close)
        row = dict(applytrack.list_applications(reopened)[0])
        self.assertEqual(row, {**original, "company": company, "role": "Backend Engineer"})

    def test_invalid_detail_updates_leave_entire_record_unchanged(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer", "2026-09-10")
        original = dict(applytrack.list_applications(self.connection)[0])
        for changes in (
            {"company": " \t", "status": "interviewing"},
            {"company": "New company", "role": "  "},
            {"role": "New role", "follow_up_on": "2026-02-30"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    applytrack.update_application(self.connection, identifier, **changes)
                self.assertEqual(dict(applytrack.list_applications(self.connection)[0]), original)

    def test_cli_updates_details_together_with_status_and_follow_up(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer", "2026-09-10")
        arguments = ["--database", str(self.database), "update", str(identifier)]
        with redirect_stdout(io.StringIO()):
            code = applytrack.main(arguments + ["--company", "Example Labs", "--role", "Backend Engineer",
                                               "--status", "interviewing", "--clear-follow-up"])
        self.assertEqual(code, 0)
        row = applytrack.list_applications(self.connection)[0]
        self.assertEqual(row["company"], "Example Labs")
        self.assertEqual(row["role"], "Backend Engineer")
        self.assertEqual(row["status"], "interviewing")
        self.assertIsNone(row["follow_up_on"])
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(applytrack.main(arguments + ["--company", "  "]), 1)
        self.assertIn("Company cannot be empty", errors.getvalue())

    def test_conflicting_or_empty_updates_are_rejected(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer")
        for arguments in ({}, {"status": "unknown"}, {"follow_up_on": "2026-09-10", "clear_follow_up": True}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    applytrack.update_application(self.connection, identifier, **arguments)

    def test_cli_workflow_and_error_exit_code(self):
        database_args = ["--database", str(self.database)]
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(applytrack.main(database_args + ["add", "Example", "Engineer"]), 0)
            self.assertEqual(applytrack.main(database_args + ["update", "1", "--status", "interviewing"]), 0)
            self.assertEqual(applytrack.main(database_args + ["list"]), 0)
            self.assertEqual(applytrack.main(database_args + ["update", "999", "--status", "rejected"]), 1)
        self.assertIn("Example", output.getvalue())
        self.assertIn("interviewing", output.getvalue())
        self.assertIn("does not exist", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
