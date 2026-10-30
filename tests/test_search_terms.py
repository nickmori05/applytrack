from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import date
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


class SearchTermTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def add(self, company, role="Engineer", *notes, follow_up=None, status="applied"):
        identifier = applytrack.add_application(self.connection, company, role, follow_up, applied_on="2026-09-01")
        if status != "applied":
            applytrack.update_application(self.connection, identifier, status=status, changed_on="2026-09-02")
        for body in notes:
            applytrack.add_note(self.connection, identifier, body, "2026-09-03")
        return identifier

    def ids(self, **filters):
        return [row["id"] for row in applytrack.list_applications(self.connection, **filters)]

    def test_every_term_must_match_in_any_order_and_any_field(self):
        both_in_role = self.add("Quiet Co", "Senior Backend Engineer")
        split = self.add("Backend Labs", "Engineer", "Referred by Sam.")
        in_one_note = self.add("Other", "Analyst", "Sam says the senior backend team is hiring.")
        self.add("Backend Only", "Engineer")
        self.add("Sam's Bakery", "Baker")
        self.assertEqual(self.ids(search=["backend", "SAM"]), [in_one_note, split])
        self.assertEqual(self.ids(search=["sam", "backend"]), [in_one_note, split])
        self.assertEqual(self.ids(search=["senior", "backend"]), [in_one_note, both_in_role])
        self.assertEqual(self.ids(search=["backend", "sam", "missing"]), [])

    def test_terms_spread_over_several_notes_still_match(self):
        identifier = self.add("Example", "Engineer", "Recruiter is Sam.", "Panel on Monday.")
        self.add("Other", "Engineer", "Recruiter is Sam.")
        self.assertEqual(self.ids(search=["sam", "panel"]), [identifier])

    def test_one_term_in_a_list_matches_like_a_single_search(self):
        first = self.add("Backend Labs")
        second = self.add("Other", "Backend Engineer")
        self.assertEqual(self.ids(search=["backend"]), self.ids(search="backend"))
        self.assertEqual(self.ids(search=("backend",)), [second, first])

    def test_repeated_and_overlapping_terms_do_not_change_the_result(self):
        identifier = self.add("Backend Labs")
        self.assertEqual(self.ids(search=["backend", "BACKEND", "back"]), [identifier])

    def test_terms_are_literal_and_casefolded_each_on_their_own(self):
        identifier = self.add("Straße 50%", "Développeur")
        self.add("Strasse", "Engineer")
        self.assertEqual(self.ids(search=["STRASSE", "50%", "DÉVELOPPEUR"]), [identifier])
        self.assertEqual(self.ids(search=["strasse", "_"]), [])

    def test_terms_combine_with_status_due_dates_and_archive(self):
        today = date(2026, 9, 30)
        due = self.add("Due Labs", "Backend", "remote", follow_up="2026-09-29", status="interviewing")
        self.add("Later Labs", "Backend", "remote", follow_up="2026-10-05", status="interviewing")
        self.add("Office Labs", "Backend", "on site", follow_up="2026-09-29", status="interviewing")
        archived = self.add("Archived Labs", "Backend", "remote")
        applytrack.archive_application(self.connection, archived)
        terms = ["backend", "remote"]
        self.assertEqual(self.ids(search=terms, due_only=True, today=today, status="interviewing"), [due])
        self.assertEqual(self.ids(search=terms, archived=True), [archived])
        self.assertEqual(len(self.ids(search=terms)), 2)

    def test_blank_or_missing_terms_are_rejected(self):
        self.add("Example")
        for search in (["backend", "  "], [], ("",)):
            with self.subTest(search=search):
                with self.assertRaisesRegex(ValueError, "Search cannot be empty."):
                    self.ids(search=search)

    def test_matching_notes_returns_notes_with_any_term(self):
        identifier = self.add("Example", "Engineer", "Recruiter is Sam.", "Panel on Monday.", "Nothing else.")
        notes = applytrack.matching_notes(self.connection, ["panel", "SAM"])
        self.assertEqual([note["body"] for note in notes[identifier]], ["Recruiter is Sam.", "Panel on Monday."])
        self.assertEqual(applytrack.matching_notes(self.connection, "panel").keys(), {identifier})

    def test_cli_list_shows_each_note_around_the_term_it_matched(self):
        self.add("Example Labs", "Backend Engineer",
                 "Recruiter call with Sam; " + "details " * 20 + "and a take-home on Friday.",
                 "Panel interview with the backend team.")
        self.add("Other Labs", "Backend Engineer")
        code, output, errors = run_cli(self.database, "list", "--search", "take-home", "--search", "backend")
        self.assertEqual((code, errors), (0, ""))
        lines = output.splitlines()
        self.assertEqual(lines[0], "#1  Example Labs — Backend Engineer")
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[2].startswith("    Note 2026-09-03: …"))
        self.assertTrue(lines[2].endswith("and a take-home on Friday."))
        self.assertEqual(lines[3], "    Note 2026-09-03: Panel interview with the backend team.")

    def test_cli_export_and_list_apply_every_term(self):
        self.add('Keep, "Labs"', "Backend Engineer", "remote")
        self.add("Skip", "Backend Engineer")
        code, output, _ = run_cli(self.database, "export", "--search", "remote", "--search", "backend")
        self.assertEqual(code, 0)
        self.assertEqual([row["company"] for row in csv.DictReader(io.StringIO(output))], ['Keep, "Labs"'])
        self.assertEqual(run_cli(self.database, "list", "--search", "missing", "--search", "backend"),
                         (0, "No applications match these filters.\n", ""))

    def test_cli_rejects_a_blank_term_among_others(self):
        self.add("Example")
        for command in ("list", "export"):
            with self.subTest(command=command):
                self.assertEqual(run_cli(self.database, command, "--search", " ", "--search", "example"),
                                 (1, "", "Error: Search cannot be empty.\n"))


class ExcludeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def add(self, company, role="Engineer", *notes, follow_up=None):
        identifier = applytrack.add_application(self.connection, company, role, follow_up, applied_on="2026-09-01")
        for body in notes:
            applytrack.add_note(self.connection, identifier, body, "2026-09-03")
        return identifier

    def ids(self, **filters):
        return [row["id"] for row in applytrack.list_applications(self.connection, **filters)]

    def test_exclude_leaves_out_company_role_and_note_matches(self):
        kept = self.add("Example Labs", "Backend Engineer", "Full-time, remote.")
        self.add("Contract Co", "Backend Engineer")
        self.add("Example", "Backend Engineer (Contract)")
        self.add("Agency", "Backend Engineer", "Recruiter says it is a CONTRACT role.")
        self.assertEqual(self.ids(exclude="contract"), [kept])
        self.assertEqual(self.ids(exclude=["contract"]), [kept])

    def test_any_excluded_term_leaves_an_application_out(self):
        kept = self.add("Example Labs")
        self.add("Contract Co")
        self.add("Agency", "Engineer", "Via a staffing agency.")
        self.assertEqual(self.ids(exclude=["contract", "staffing"]), [kept])

    def test_exclude_combines_with_search_and_wins_over_it(self):
        kept = self.add("Example Labs", "Backend Engineer", "remote")
        self.add("Other Labs", "Backend Engineer", "remote, but contract")
        self.add("Unrelated", "Designer", "remote")
        self.assertEqual(self.ids(search=["backend", "remote"], exclude="contract"), [kept])
        self.assertEqual(self.ids(search="contract", exclude="contract"), [])

    def test_exclude_combines_with_due_dates_status_and_archive(self):
        today = date(2026, 9, 30)
        due = self.add("Due Labs", follow_up="2026-09-29")
        self.add("Due Contract", follow_up="2026-09-29")
        archived = self.add("Archived Labs")
        self.add("Archived Contract")
        for identifier in (archived, archived + 1):
            applytrack.archive_application(self.connection, identifier)
        self.assertEqual(self.ids(exclude="contract", due_only=True, today=today), [due])
        self.assertEqual(self.ids(exclude="contract", archived=True), [archived])
        self.assertEqual(self.ids(exclude="contract", status="interviewing"), [])

    def test_excluded_text_is_literal_and_ignores_case_like_search(self):
        kept = self.add("Plain")
        self.add("100% Remote", "Engineer", "O'Brien referred me.")
        self.add("Straße Labs")
        self.assertEqual(self.ids(exclude=["%", "o'brien", "STRASSE"]), [kept])
        self.assertEqual(len(self.ids(exclude="_")), 3)

    def test_applications_without_notes_are_not_dropped(self):
        identifier = self.add("Example Labs")
        self.assertEqual(self.ids(exclude="anything"), [identifier])

    def test_blank_excluded_text_is_rejected(self):
        self.add("Example")
        for exclude in ("  ", ["contract", ""], []):
            with self.subTest(exclude=exclude):
                with self.assertRaisesRegex(ValueError, "Excluded text cannot be empty."):
                    self.ids(exclude=exclude)

    def test_cli_list_and_export_leave_out_excluded_applications(self):
        self.add("Example Labs", "Backend Engineer")
        self.add("Contract Co", "Backend Engineer")
        self.add("Agency", "Designer", "contract")
        self.assertEqual(run_cli(self.database, "list", "--exclude", "contract"), (0, (
            "#1  Example Labs — Backend Engineer\n"
            "    applied | Applied: 2026-09-01 | Follow up: not set\n"
        ), ""))
        code, output, _ = run_cli(self.database, "export", "--exclude", "contract", "--exclude", "example")
        self.assertEqual((code, output), (0, ",".join(applytrack.EXPORT_COLUMNS) + "\n"))
        self.assertEqual(run_cli(self.database, "list", "--exclude", "labs", "--exclude", "co"),
                         (0, "No applications match these filters.\n", ""))

    def test_cli_shows_only_notes_matching_search_terms(self):
        self.add("Example Labs", "Backend Engineer", "Remote role.", "Not a contract.")
        self.add("Other Labs", "Backend Engineer", "Remote contract role.")
        self.assertEqual(run_cli(self.database, "list", "--search", "remote", "--exclude", "agency"), (0, (
            "#2  Other Labs — Backend Engineer\n"
            "    applied | Applied: 2026-09-01 | Follow up: not set\n"
            "    Note 2026-09-03: Remote contract role.\n"
            "#1  Example Labs — Backend Engineer\n"
            "    applied | Applied: 2026-09-01 | Follow up: not set\n"
            "    Note 2026-09-03: Remote role.\n"
        ), ""))

    def test_cli_rejects_blank_excluded_text(self):
        self.add("Example")
        for command in ("list", "export"):
            with self.subTest(command=command):
                self.assertEqual(run_cli(self.database, command, "--exclude", " "),
                                 (1, "", "Error: Excluded text cannot be empty.\n"))


if __name__ == "__main__":
    unittest.main()
