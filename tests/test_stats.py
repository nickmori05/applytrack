from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

import applytrack


def run_cli(database, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


class StatsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def add(self, applied_on, *changes):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer", applied_on=applied_on)
        for status, changed_on in changes:
            applytrack.update_application(self.connection, identifier, status=status, changed_on=changed_on)
        return identifier

    def add_sample(self):
        self.add("2026-09-01", ("interviewing", "2026-09-04"), ("offer", "2026-09-20"))
        self.add("2026-09-03", ("rejected", "2026-09-24"))
        applytrack.archive_application(self.connection, self.add("2026-09-05", ("withdrawn", "2026-09-08")))
        self.add("2026-09-10")

    def test_empty_database_has_an_empty_summary(self):
        summary = applytrack.summarize(self.connection)
        self.assertEqual(summary, applytrack.Summary(0, 0, dict.fromkeys(applytrack.STATUSES, 0), 0, 0, ()))

    def test_counts_statuses_responses_and_days_including_archived(self):
        self.add_sample()
        summary = applytrack.summarize(self.connection)
        self.assertEqual((summary.total, summary.archived), (4, 1))
        self.assertEqual(summary.by_status,
                         {"applied": 1, "interviewing": 0, "offer": 1, "rejected": 1, "withdrawn": 1})
        self.assertEqual((summary.responded, summary.interviewed), (2, 1))
        self.assertEqual(summary.response_days, (3, 21))

    def test_history_decides_responses_not_only_the_current_status(self):
        self.add("2026-09-01", ("interviewing", "2026-09-05"), ("withdrawn", "2026-09-09"))
        self.add("2026-09-01", ("interviewing", "2026-09-02"), ("rejected", "2026-09-12"))
        self.add("2026-09-01", ("withdrawn", "2026-09-03"))
        self.add("2026-09-01", ("rejected", "2026-09-01"))
        summary = applytrack.summarize(self.connection)
        self.assertEqual((summary.responded, summary.interviewed), (3, 2))
        self.assertEqual(summary.response_days, (0, 1, 4))

    def test_undated_responses_count_but_are_not_timed(self):
        applytrack.import_csv(self.connection, io.StringIO(
            "company,role,status,applied_on\nOld,Engineer,offer,2026-09-01\nNew,Engineer,applied,2026-09-02\n"))
        self.add("2026-09-03", ("rejected", "2026-09-10"))
        summary = applytrack.summarize(self.connection)
        self.assertEqual((summary.total, summary.responded, summary.interviewed), (3, 2, 1))
        self.assertEqual(summary.response_days, (7,))

    def test_days_follow_a_corrected_submission_date(self):
        identifier = self.add("2026-09-10", ("interviewing", "2026-09-15"))
        applytrack.update_application(self.connection, identifier, applied_on="2026-09-01")
        self.assertEqual(applytrack.summarize(self.connection).response_days, (14,))

    def test_since_includes_its_own_date_and_rejects_invalid_dates(self):
        self.add_sample()
        self.assertEqual(applytrack.summarize(self.connection, since="2026-09-03").total, 3)
        self.assertEqual(applytrack.summarize(self.connection, since="2026-09-04").total, 2)
        self.assertEqual(applytrack.summarize(self.connection, since="2026-12-01").total, 0)
        for value in ("", "2026-9-3", "2026-02-30", "yesterday"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "valid date"):
                applytrack.summarize(self.connection, since=value)

    def test_cli_prints_the_summary(self):
        self.add_sample()
        self.assertEqual(run_cli(self.database, "stats"), (0, (
            "Applications: 4 (1 archived)\n"
            "    applied       1\n"
            "    interviewing  0\n"
            "    offer         1\n"
            "    rejected      1\n"
            "    withdrawn     1\n"
            "Responses: 2 of 4 (50%)\n"
            "Interviews: 1 of 4 (25%)\n"
            "Days to first response: median 12, fastest 3, slowest 21 (from 2 responses)\n"
        ), ""))
        self.assertEqual(run_cli(self.database, "stats", "--since", "2026-09-04")[1], (
            "Applications submitted on or after 2026-09-04: 2 (1 archived)\n"
            "    applied       1\n"
            "    interviewing  0\n"
            "    offer         0\n"
            "    rejected      0\n"
            "    withdrawn     1\n"
            "Responses: 0 of 2 (0%)\n"
            "Interviews: 0 of 2 (0%)\n"
        ))

    def test_cli_explains_undated_and_partly_dated_responses(self):
        applytrack.import_csv(self.connection, io.StringIO("company,role,status\nOld,Engineer,rejected\n"))
        output = run_cli(self.database, "stats")[1]
        self.assertTrue(output.endswith("Days to first response: no response dates recorded yet\n"))
        self.add("2026-09-01", ("interviewing", "2026-09-04"), ("rejected", "2026-09-06"))
        self.add("2026-09-01", ("offer", "2026-09-10"))
        self.assertTrue(run_cli(self.database, "stats")[1].endswith(
            "Days to first response: median 6, fastest 3, slowest 9"
            " (from 2 of 3 responses; the rest have no recorded date)\n"))

    def test_cli_shows_a_half_day_median_and_a_single_response(self):
        self.add("2026-09-01", ("rejected", "2026-09-02"))
        self.assertTrue(run_cli(self.database, "stats")[1].endswith(
            "Days to first response: median 1, fastest 1, slowest 1 (from 1 response)\n"))
        self.add("2026-09-01", ("interviewing", "2026-09-03"))
        self.assertIn("median 1.5, fastest 1, slowest 2 (from 2 responses)", run_cli(self.database, "stats")[1])

    def test_cli_handles_empty_and_invalid_requests(self):
        self.assertEqual(run_cli(self.database, "stats"),
                         (0, "No applications yet. Add your first with 'add'.\n", ""))
        self.add("2026-09-01")
        self.assertEqual(run_cli(self.database, "stats", "--since", "2026-09-02"),
                         (0, "No applications submitted on or after 2026-09-02.\n", ""))
        self.assertEqual(run_cli(self.database, "stats", "--since", "2026-13-01"),
                         (1, "", "Error: Use a valid date in YYYY-MM-DD format.\n"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            applytrack.main(["--database", str(self.database), "stats", "--until", "2026-09-01"])


class StatsUpgradeTests(unittest.TestCase):
    def test_database_from_before_status_history_is_summarized(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "applications.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("""CREATE TABLE applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'applied',
                    applied_on TEXT NOT NULL, follow_up_on TEXT, archived_on TEXT)""")
                connection.executemany(
                    "INSERT INTO applications (company, role, status, applied_on) VALUES (?, ?, ?, ?)",
                    [("First", "Engineer", "applied", "2026-09-01"), ("Second", "Engineer", "offer", "2026-09-02")],
                )
                connection.commit()
            code, output, errors = run_cli(database, "stats")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Responses: 1 of 2 (50%)\nInterviews: 1 of 2 (50%)\n", output)
        self.assertIn("no response dates recorded yet", output)


if __name__ == "__main__":
    unittest.main()
