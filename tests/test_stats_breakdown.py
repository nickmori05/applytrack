from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

import applytrack


def run_cli(database, *arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


class BreakdownTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def add(self, company, applied_on, *changes):
        identifier = applytrack.add_application(self.connection, company, "Engineer", applied_on=applied_on)
        for status, changed_on in changes:
            applytrack.update_application(self.connection, identifier, status=status, changed_on=changed_on)
        return identifier

    def add_sample(self):
        self.add("Example Labs", "2026-08-20", ("interviewing", "2026-08-25"))
        self.add("Example Labs", "2026-09-01", ("rejected", "2026-09-15"))
        self.add("Other Co", "2026-09-03")
        applytrack.archive_application(self.connection, self.add("Acme", "2026-09-05", ("offer", "2026-09-20")))
        self.add("example labs", "2026-10-02")

    def groups(self, by, **limits):
        return [(label, summary.total, summary.responded, summary.interviewed, summary.response_days)
                for label, summary in applytrack.summarize_by(self.connection, by, **limits)]

    def test_by_company_ignores_case_and_lists_the_busiest_first(self):
        self.add_sample()
        self.assertEqual(self.groups("company"), [
            ("example labs", 3, 2, 1, (5, 14)),
            ("Acme", 1, 1, 1, (15,)),
            ("Other Co", 1, 0, 0, ()),
        ])

    def test_company_is_shown_as_most_recently_entered(self):
        self.add("ACME", "2026-09-01")
        self.add("Acme", "2026-09-02")
        later = self.add("acme", "2026-09-03")
        applytrack.update_application(self.connection, later, company="AcMe")
        self.assertEqual([label for label, *_ in self.groups("company")], ["AcMe"])

    def test_by_month_uses_the_submission_month_in_calendar_order(self):
        self.add_sample()
        self.add("Later", "2026-01-31")
        self.assertEqual(self.groups("month"), [
            ("2026-01", 1, 0, 0, ()),
            ("2026-08", 1, 1, 1, (5,)),
            ("2026-09", 3, 2, 1, (14, 15)),
            ("2026-10", 1, 0, 0, ()),
        ])

    def test_groups_add_up_to_the_summary_and_follow_the_date_range(self):
        self.add_sample()
        ranges = ({}, {"since": "2026-09-01"}, {"until": "2026-09-03"}, {"since": "2026-09-02", "until": "2026-09-30"})
        for limits in ranges:
            summary = applytrack.summarize(self.connection, **limits)
            for by in applytrack.GROUPINGS:
                with self.subTest(limits=limits, by=by):
                    groups = applytrack.summarize_by(self.connection, by, **limits)
                    self.assertEqual(sum(group.total for _, group in groups), summary.total)
                    self.assertEqual(sum(group.archived for _, group in groups), summary.archived)
                    self.assertEqual(sum(group.responded for _, group in groups), summary.responded)
                    self.assertEqual(sorted(day for _, group in groups for day in group.response_days),
                                     list(summary.response_days))
        self.assertEqual(self.groups("company", since="2026-09-02", until="2026-09-30"),
                         [("Acme", 1, 1, 1, (15,)), ("Other Co", 1, 0, 0, ())])

    def test_empty_database_and_unknown_groupings(self):
        self.assertEqual(applytrack.summarize_by(self.connection, "company"), [])
        self.add("Example", "2026-09-01")
        for by in ("role", "Company", ""):
            with self.subTest(by=by), self.assertRaisesRegex(ValueError, "Group by one of: company, month."):
                applytrack.summarize_by(self.connection, by)

    def test_cli_prints_the_summary_then_the_table(self):
        self.add_sample()
        code, output, errors = run_cli(self.database, "stats", "--by", "company")
        self.assertEqual((code, errors), (0, ""))
        summary, table = output.split("\n\n")
        self.assertEqual(summary + "\n", run_cli(self.database, "stats")[1])
        self.assertEqual(table, (
            "Company       Applications  Responses  Interviews  Median days\n"
            "example labs             3    2 (67%)     1 (33%)          9.5\n"
            "Acme                     1   1 (100%)    1 (100%)           15\n"
            "Other Co                 1     0 (0%)      0 (0%)            -\n"
        ))

    def test_cli_breaks_down_a_date_range_by_month(self):
        self.add_sample()
        output = run_cli(self.database, "stats", "--by", "month", "--since", "2026-09-01")[1]
        self.assertTrue(output.startswith("Applications submitted on or after 2026-09-01: 4 (1 archived)\n"))
        self.assertTrue(output.endswith(
            "\n\n"
            "Month    Applications  Responses  Interviews  Median days\n"
            "2026-09             3    2 (67%)     1 (33%)         14.5\n"
            "2026-10             1     0 (0%)      0 (0%)            -\n"
        ))

    def test_cli_shortens_long_names_and_escapes_control_characters(self):
        self.add("A Company Name Well Over Thirty Characters Long", "2026-09-01")
        self.add("Bad\x1b[2JName", "2026-09-02")
        lines = run_cli(self.database, "stats", "--by", "company")[1].split("\n\n")[1].splitlines()
        self.assertEqual(lines[0][:32], "Company                         ")
        self.assertEqual(lines[1][:32], "A Company Name Well Over Thir…  ")
        self.assertEqual(lines[2][:32], "Bad\\x1b[2JName                  ")

    def test_cli_handles_empty_and_invalid_requests(self):
        self.assertEqual(run_cli(self.database, "stats", "--by", "company"),
                         (0, "No applications yet. Add your first with 'add'.\n", ""))
        self.add("Example", "2026-09-01")
        self.assertEqual(run_cli(self.database, "stats", "--by", "month", "--until", "2026-08-31"),
                         (0, "No applications submitted on or before 2026-08-31.\n", ""))
        for arguments in (["stats", "--by", "role"], ["stats", "--by"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    applytrack.main(["--database", str(self.database), *arguments])
                self.assertEqual(raised.exception.code, 2)

    def test_breakdown_lines_shows_undated_responses_without_a_median(self):
        applytrack.import_csv(self.connection, io.StringIO("company,role,status\nOld,Engineer,offer\n"))
        groups = applytrack.summarize_by(self.connection, "company")
        self.assertEqual(applytrack.breakdown_lines(groups, "company")[1],
                         "Old                 1   1 (100%)    1 (100%)            -")


if __name__ == "__main__":
    unittest.main()
