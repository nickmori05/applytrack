from contextlib import redirect_stderr, redirect_stdout
from datetime import date
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
