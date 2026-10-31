from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
from pathlib import Path
import tempfile
import unittest

import applytrack
from applytrack.validation import MAX_FOLLOW_UP_DAYS, follow_up_in

RANGE = f"Follow-up days must be a whole number from 0 to {MAX_FOLLOW_UP_DAYS}."


def days_from_today(days):
    return (date.today() + timedelta(days=days)).isoformat()


class FollowUpInTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"

    def run_cli(self, *arguments):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = applytrack.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def run_argparse(self, *arguments):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            applytrack.main(["--database", str(self.database), *arguments])
        return raised.exception.code, errors.getvalue()

    def rows(self):
        with closing(applytrack.connect(self.database)) as connection:
            return [dict(row) for row in applytrack.list_applications(connection)]

    def test_days_count_from_today(self):
        self.assertEqual(follow_up_in(0), date.today().isoformat())
        self.assertEqual(follow_up_in(14), days_from_today(14))
        self.assertEqual(follow_up_in(MAX_FOLLOW_UP_DAYS), days_from_today(MAX_FOLLOW_UP_DAYS))

    def test_negative_or_very_large_days_are_rejected(self):
        for days in (-1, MAX_FOLLOW_UP_DAYS + 1, 10**9):
            with self.subTest(days=days):
                with self.assertRaisesRegex(ValueError, f"^{RANGE}$"):
                    follow_up_in(days)

    def test_add_saves_the_follow_up_and_shows_the_date(self):
        code, output, errors = self.run_cli("add", "Example", "Engineer", "--follow-up-in", "14")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, f"Added application #1. Follow up on {days_from_today(14)}.\n")
        self.assertEqual(self.rows()[0]["follow_up_on"], days_from_today(14))

    def test_add_with_zero_days_is_due_today(self):
        self.assertEqual(self.run_cli("add", "Example", "Engineer", "--follow-up-in", "0")[0], 0)
        code, output, _ = self.run_cli("list", "--due")
        self.assertEqual(code, 0)
        self.assertIn(f"Follow up: {date.today().isoformat()}", output)

    def test_add_counts_from_today_even_for_an_earlier_submission(self):
        code, output, _ = self.run_cli("add", "Example", "Engineer", "--applied-on", "2026-09-01",
                                       "--follow-up-in", "7")
        self.assertEqual(code, 0)
        self.assertEqual(output, f"Added application #1. Follow up on {days_from_today(7)}.\n")
        row = self.rows()[0]
        self.assertEqual((row["applied_on"], row["follow_up_on"]), ("2026-09-01", days_from_today(7)))

    def test_add_without_follow_up_in_prints_as_before(self):
        self.assertEqual(self.run_cli("add", "Example", "Engineer"), (0, "Added application #1.\n", ""))
        self.assertEqual(self.run_cli("add", "Example", "Engineer", "--applied-on", "2026-09-01",
                                      "--follow-up", "2026-09-15"),
                         (0, "Added application #2.\n", ""))

    def test_add_rejects_out_of_range_days_without_saving(self):
        for days in ("-1", str(MAX_FOLLOW_UP_DAYS + 1)):
            with self.subTest(days=days):
                self.assertEqual(self.run_cli("add", "Example", "Engineer", "--follow-up-in", days),
                                 (1, "", f"Error: {RANGE}\n"))
        self.assertEqual(self.rows(), [])

    def test_add_rejects_non_numbers_and_a_follow_up_date_as_well(self):
        code, errors = self.run_argparse("add", "Example", "Engineer", "--follow-up-in", "two weeks")
        self.assertEqual(code, 2)
        self.assertIn("invalid int value", errors)
        code, errors = self.run_argparse("add", "Example", "Engineer", "--follow-up", "2026-09-15",
                                         "--follow-up-in", "7")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", errors)
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
