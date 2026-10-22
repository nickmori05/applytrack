from contextlib import closing, redirect_stderr, redirect_stdout
import csv
from datetime import date
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


class NoteSearchTests(unittest.TestCase):
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

    def test_search_finds_note_text_alongside_company_and_role(self):
        by_note = self.add("Quiet Co", "Analyst", "Recruiter call with Sam Rivera.")
        by_company = self.add("Sam's Bakery")
        self.add("Unrelated", "Designer", "Nothing relevant here.")
        by_both = self.add("Samsung", "Engineer", "Spoke to Sam again.")
        self.assertEqual(self.ids(search="  SAM "), [by_both, by_company, by_note])
        self.assertEqual(self.ids(search="rivera"), [by_note])
        self.assertEqual(self.ids(search="missing"), [])

    def test_note_search_ignores_case_without_dropping_accents_and_treats_text_literally(self):
        accented = self.add("One", "Engineer", "Rencontre à Montréal avec la STRASSE team.")
        self.assertEqual(self.ids(search="straße"), [accented])
        self.assertEqual(self.ids(search="MONTRÉAL"), [accented])
        self.assertEqual(self.ids(search="Montreal"), [])
        for query in ("%", "_", "\\", "' OR 1=1 --"):
            with self.subTest(query=query):
                identifier = self.add("Literal", "Engineer", "Contains " + query)
                self.assertEqual(self.ids(search=query), [identifier])

    def test_note_matches_respect_archive_status_and_due_filters(self):
        today = date(2026, 9, 30)
        due = self.add("Due", "Engineer", "take-home", follow_up="2026-09-29", status="interviewing")
        self.add("Later", "Engineer", "take-home", follow_up="2026-10-05", status="interviewing")
        closed = self.add("Closed", "Engineer", "take-home", follow_up="2026-09-29", status="rejected")
        archived = self.add("Archived", "Engineer", "take-home")
        applytrack.archive_application(self.connection, archived)
        self.assertEqual(self.ids(search="take-home", due_only=True, today=today), [due])
        self.assertEqual(self.ids(search="take-home", status="rejected"), [closed])
        self.assertEqual(self.ids(search="take-home", archived=True), [archived])
        self.assertEqual(len(self.ids(search="take-home")), 3)

    def test_deleted_applications_notes_no_longer_match(self):
        identifier = self.add("Gone", "Engineer", "unique phrase")
        applytrack.delete_application(self.connection, identifier)
        self.assertEqual(self.ids(search="unique phrase"), [])
        self.assertEqual(applytrack.matching_notes(self.connection, "unique phrase"), {})

    def test_matching_notes_groups_matches_oldest_first(self):
        first = self.add("First", "Engineer", "Sam: second day")
        applytrack.add_note(self.connection, first, "Sam: first day", "2026-09-02")
        applytrack.add_note(self.connection, first, "No match", "2026-09-04")
        second = self.add("Second", "Engineer", "Sam again")
        matches = applytrack.matching_notes(self.connection, " sam ")
        self.assertEqual({key: [row["body"] for row in rows] for key, rows in matches.items()},
                         {first: ["Sam: first day", "Sam: second day"], second: ["Sam again"]})

    def test_blank_search_is_still_rejected(self):
        self.add("Example", "Engineer", "Some note")
        with self.assertRaisesRegex(ValueError, "Search cannot be empty."):
            applytrack.list_applications(self.connection, search=" \t")


class NoteExcerptTests(unittest.TestCase):
    def test_short_line_containing_the_match_is_shown_whole(self):
        body = "Agenda:\r\n- pairing with SAM\n- lunch"
        self.assertEqual(applytrack.note_excerpt(body, "sam"), "- pairing with SAM")
        self.assertEqual(applytrack.note_excerpt(body, "agenda"), "Agenda:")

    def test_match_across_a_line_break_falls_back_to_the_first_line(self):
        self.assertEqual(applytrack.note_excerpt("first part\nsecond part", "part\nsecond"), "first part")

    def test_long_lines_are_cut_around_the_match(self):
        line = "a" * 150 + " Sam " + "b" * 150
        excerpt = applytrack.note_excerpt(line, "sam", width=40)
        self.assertEqual(excerpt, "…" + "a" * 9 + " Sam " + "b" * 26 + "…")
        self.assertEqual(applytrack.note_excerpt("Sam " + "b" * 60, "sam", width=40), "Sam " + "b" * 36 + "…")
        self.assertEqual(applytrack.note_excerpt("a" * 60 + " Sam", "sam", width=40), "…" + "a" * 36 + " Sam")

    def test_match_is_located_even_when_casefolding_lengthens_earlier_text(self):
        line = "ß" * 60 + "target" + "z" * 60
        self.assertIn("target", applytrack.note_excerpt(line, "TARGET", width=30))

    def test_control_characters_are_escaped(self):
        self.assertEqual(applytrack.note_excerpt("Call Sam\x1b[2J\x07", "sam"), "Call Sam\\x1b[2J\\x07")


class NoteSearchCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        run_cli(self.database, "add", "Example Company", "Software Engineer", "--applied-on", "2026-09-01")
        run_cli(self.database, "add", "Sam's Bakery", "Baker", "--applied-on", "2026-09-02")
        run_cli(self.database, "note", "1", "Recruiter call with Sam; take-home due Friday.", "--on", "2026-09-03")
        run_cli(self.database, "note", "1", "Agenda:\n- pairing with SAM\x1b[2J", "--on", "2026-09-04")
        run_cli(self.database, "note", "1", "Sent the take-home.", "--on", "2026-09-05")

    def test_list_shows_the_notes_that_matched(self):
        self.assertEqual(run_cli(self.database, "list", "--search", "sam"), (0, (
            "#2  Sam's Bakery — Baker\n"
            "    applied | Applied: 2026-09-02 | Follow up: not set\n"
            "#1  Example Company — Software Engineer\n"
            "    applied | Applied: 2026-09-01 | Follow up: not set\n"
            "    Note 2026-09-03: Recruiter call with Sam; take-home due Friday.\n"
            "    Note 2026-09-04: - pairing with SAM\\x1b[2J\n"
        ), ""))

    def test_list_without_search_shows_no_notes(self):
        self.assertNotIn("Note ", run_cli(self.database, "list")[1])
        self.assertNotIn("Note ", run_cli(self.database, "list", "--search", "software")[1])

    def test_export_search_includes_applications_matched_by_notes(self):
        code, output, _ = run_cli(self.database, "export", "--search", "TAKE-HOME")
        self.assertEqual(code, 0)
        self.assertEqual([row["company"] for row in csv.DictReader(io.StringIO(output))], ["Example Company"])

    def test_archived_notes_are_found_only_with_archived(self):
        run_cli(self.database, "archive", "1")
        self.assertEqual(run_cli(self.database, "list", "--search", "take-home"),
                         (0, "No applications match these filters.\n", ""))
        output = run_cli(self.database, "list", "--archived", "--search", "take-home")[1]
        self.assertIn("    Note 2026-09-05: Sent the take-home.\n", output)

    def test_blank_and_unmatched_searches(self):
        self.assertEqual(run_cli(self.database, "list", "--search", " "), (1, "", "Error: Search cannot be empty.\n"))
        self.assertEqual(run_cli(self.database, "export", "--search", ""), (1, "", "Error: Search cannot be empty.\n"))
        self.assertEqual(run_cli(self.database, "list", "--search", "nobody"),
                         (0, "No applications match these filters.\n", ""))


class OlderDatabaseNoteSearchTests(unittest.TestCase):
    def test_database_from_before_notes_is_upgraded_and_searchable(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("""CREATE TABLE applications (
                id INTEGER PRIMARY KEY, company TEXT NOT NULL, role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'applied', applied_on TEXT NOT NULL, follow_up_on TEXT)""")
            connection.execute(
                "INSERT INTO applications VALUES (4, 'Old Co', 'Engineer', 'applied', '2026-09-01', NULL)"
            )
            connection.commit()
        self.assertEqual(run_cli(database, "list", "--search", "panel"),
                         (0, "No applications match these filters.\n", ""))
        run_cli(database, "note", "4", "Panel interview next week.", "--on", "2026-09-10")
        self.assertEqual(run_cli(database, "list", "--search", "panel")[1], (
            "#4  Old Co — Engineer\n"
            "    applied | Applied: 2026-09-01 | Follow up: not set\n"
            "    Note 2026-09-10: Panel interview next week.\n"
        ))


if __name__ == "__main__":
    unittest.main()
