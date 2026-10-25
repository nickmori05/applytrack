from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
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


class NoteNumberTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)
        self.identifier = applytrack.add_application(self.connection, "Example", "Engineer",
                                                     applied_on="2026-09-01")
        self.other = applytrack.add_application(self.connection, "Other", "Engineer", applied_on="2026-09-01")
        # Added out of order and interleaved with another application's notes, so
        # numbers must follow dates within one application, not row IDs.
        applytrack.add_note(self.connection, self.identifier, "Sent the take-home.", "2026-09-05")
        applytrack.add_note(self.connection, self.other, "Someone else's note.", "2026-09-02")
        applytrack.add_note(self.connection, self.identifier, "Recruiter call with Sam.", "2026-09-03")
        applytrack.add_note(self.connection, self.identifier, "Thank-you email.", "2026-09-05")

    def notes(self, identifier=None):
        rows = applytrack.application_notes(self.connection, identifier or self.identifier)
        return [(row["noted_on"], row["body"]) for row in rows]

    def test_numbers_follow_the_order_notes_are_shown(self):
        bodies = [applytrack.find_note(self.connection, self.identifier, number)["body"] for number in (1, 2, 3)]
        self.assertEqual(bodies, ["Recruiter call with Sam.", "Sent the take-home.", "Thank-you email."])
        self.assertEqual(applytrack.find_note(self.connection, self.other, 1)["body"], "Someone else's note.")

    def test_missing_notes_and_applications_are_reported(self):
        for identifier, number, message in (
            (self.identifier, 4, "Application 1 has no note 4."),
            (self.identifier, 0, "Application 1 has no note 0."),
            (self.identifier, -1, "Application 1 has no note -1."),
            (self.other, 2, "Application 2 has no note 2."),
            (999, 1, "Application 999 does not exist."),
        ):
            with self.subTest(identifier=identifier, number=number), self.assertRaisesRegex(ValueError, message):
                applytrack.find_note(self.connection, identifier, number)

    def test_edit_replaces_text_and_keeps_the_date(self):
        number = applytrack.edit_note(self.connection, self.identifier, 1, "  Recruiter call with Sam Lee.\n")
        self.assertEqual(number, 1)
        self.assertEqual(self.notes()[0], ("2026-09-03", "Recruiter call with Sam Lee."))
        self.assertEqual(self.notes(self.other), [("2026-09-02", "Someone else's note.")])

    def test_edit_replaces_the_date_and_reports_where_the_note_moved(self):
        self.assertEqual(applytrack.edit_note(self.connection, self.identifier, 3, noted_on="2026-09-01"), 1)
        self.assertEqual(self.notes(), [
            ("2026-09-01", "Thank-you email."),
            ("2026-09-03", "Recruiter call with Sam."),
            ("2026-09-05", "Sent the take-home."),
        ])
        # A note moved onto a day that already has notes goes after them, as if added last.
        self.assertEqual(applytrack.edit_note(self.connection, self.identifier, 1, "Thanks sent.", "2026-09-05"), 3)
        self.assertEqual(self.notes()[1:], [("2026-09-05", "Sent the take-home."), ("2026-09-05", "Thanks sent.")])

    def test_edit_may_date_a_note_before_the_submission(self):
        applytrack.edit_note(self.connection, self.identifier, 2, noted_on="2026-08-20")
        self.assertEqual(self.notes()[0], ("2026-08-20", "Sent the take-home."))

    def test_invalid_edits_change_nothing(self):
        before = self.notes()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for identifier, number, body, noted_on, message in (
            (self.identifier, 1, None, None, "Choose new note text, a new date, or both."),
            (self.identifier, 1, " \n", None, "Note cannot be empty."),
            (self.identifier, 1, None, "2026-9-3", "valid date in YYYY-MM-DD"),
            (self.identifier, 1, "New", tomorrow, "Note date cannot be in the future."),
            (self.identifier, 9, "New", None, "Application 1 has no note 9."),
            (999, 1, "New", None, "Application 999 does not exist."),
        ):
            with self.subTest(body=body, noted_on=noted_on), self.assertRaisesRegex(ValueError, message):
                applytrack.edit_note(self.connection, identifier, number, body, noted_on)
        self.assertEqual(self.notes(), before)
        self.assertFalse(self.connection.in_transaction)

    def test_archived_applications_notes_can_be_edited(self):
        applytrack.archive_application(self.connection, self.identifier)
        applytrack.edit_note(self.connection, self.identifier, 2, "Take-home submitted.")
        self.assertEqual(self.notes()[1], ("2026-09-05", "Take-home submitted."))

    def test_edited_text_is_found_by_search(self):
        applytrack.edit_note(self.connection, self.identifier, 1, "Call with Priya.")
        found = applytrack.list_applications(self.connection, search="priya")
        self.assertEqual([row["id"] for row in found], [self.identifier])
        self.assertEqual(applytrack.list_applications(self.connection, search="Sam"), [])

    def test_cli_numbers_notes_and_aligns_numbers_past_nine(self):
        for day in range(6, 14):
            applytrack.add_note(self.connection, self.identifier, f"Day {day}", f"2026-09-{day:02}")
        lines = run_cli(self.database, "notes", "1")[1].splitlines()
        self.assertEqual(lines[1], "     1  2026-09-03  Recruiter call with Sam.")
        self.assertEqual(lines[-1], "    11  2026-09-13  Day 13")

    def test_cli_edits_notes(self):
        self.assertEqual(run_cli(self.database, "edit-note", "1", "2", "--text", "Take-home submitted."),
                         (0, "Updated note 2 of application #1.\n", ""))
        self.assertEqual(run_cli(self.database, "edit-note", "1", "2", "--on", "2026-09-02"),
                         (0, "Updated note 2 of application #1; it is now note 1.\n", ""))
        self.assertEqual(run_cli(self.database, "notes", "1"), (0, (
            "#1  Example — Engineer\n"
            "    1  2026-09-02  Take-home submitted.\n"
            "    2  2026-09-03  Recruiter call with Sam.\n"
            "    3  2026-09-05  Thank-you email.\n"
        ), ""))

    def test_cli_reports_invalid_edits(self):
        before = self.notes()
        for arguments, message in (
            (("edit-note", "1", "1"), "Choose new note text, a new date, or both."),
            (("edit-note", "1", "1", "--text", "  "), "Note cannot be empty."),
            (("edit-note", "1", "4", "--text", "New"), "Application 1 has no note 4."),
            (("edit-note", "999", "1", "--text", "New"), "Application 999 does not exist."),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(run_cli(self.database, *arguments), (1, "", f"Error: {message}\n"))
        self.assertEqual(self.notes(), before)
        for arguments in (["edit-note", "1"], ["edit-note", "1", "first", "--text", "New"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    applytrack.main(["--database", str(self.database), *arguments])
                self.assertEqual(raised.exception.code, 2)

    def test_numbers_survive_a_backup_and_restore(self):
        restored = Path(self.database.parent) / "restored.sqlite3"
        backup = io.StringIO()
        applytrack.write_backup(applytrack.snapshot(self.connection), backup)
        records, last_id = applytrack.read_backup(io.StringIO(backup.getvalue()))
        connection = applytrack.connect(restored)
        self.addCleanup(connection.close)
        applytrack.restore_backup(connection, records, last_id)
        for number in (1, 2, 3):
            with self.subTest(number=number):
                self.assertEqual(applytrack.find_note(connection, self.identifier, number)["body"],
                                 applytrack.find_note(self.connection, self.identifier, number)["body"])


if __name__ == "__main__":
    unittest.main()
