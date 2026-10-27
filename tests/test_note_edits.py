from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import applytrack


class NoteEditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.database = self.folder / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)
        self.identifier = applytrack.add_application(self.connection, "Example", "Engineer",
                                                     applied_on="2026-09-01")
        self.call = applytrack.add_note(self.connection, self.identifier, "Recruiter call with Sam.", "2026-09-03")
        self.sent = applytrack.add_note(self.connection, self.identifier, "Sent the take-home.", "2026-09-05")

    def notes(self, connection=None, identifier=None):
        rows = applytrack.application_notes(connection or self.connection, identifier or self.identifier)
        return [(row["id"], row["noted_on"], row["body"]) for row in rows]

    def run_cli(self, *arguments, answer=None):
        output = io.StringIO()
        errors = io.StringIO()
        if isinstance(answer, BaseException):
            prompt = mock.patch("builtins.input", side_effect=answer)
        else:
            prompt = mock.patch("builtins.input", return_value=answer)
        with prompt as asked, redirect_stdout(output), redirect_stderr(errors):
            code = applytrack.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue(), asked

    def test_editing_text_keeps_the_id_date_and_application_and_survives_reopening(self):
        applytrack.edit_note(self.connection, self.call, "  Recruiter call with Sam Lee.\n")
        reopened = applytrack.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(self.notes(reopened), [
            (self.call, "2026-09-03", "Recruiter call with Sam Lee."),
            (self.sent, "2026-09-05", "Sent the take-home."),
        ])

    def test_editing_the_date_moves_the_note_and_both_can_change_together(self):
        applytrack.edit_note(self.connection, self.call, noted_on="2026-09-07")
        self.assertEqual([row[0] for row in self.notes()], [self.sent, self.call])
        applytrack.edit_note(self.connection, self.sent, "Referral from Ana.", "2026-08-20")
        self.assertEqual(self.notes()[0], (self.sent, "2026-08-20", "Referral from Ana."))

    def test_notes_on_archived_applications_can_be_edited_and_deleted(self):
        applytrack.archive_application(self.connection, self.identifier)
        applytrack.edit_note(self.connection, self.call, "Edited while archived.")
        applytrack.delete_note(self.connection, self.sent)
        self.assertEqual(self.notes(), [(self.call, "2026-09-03", "Edited while archived.")])

    def test_invalid_edits_are_rejected_without_changing_anything(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        before = self.notes()
        for note_id, body, noted_on, message in (
            (self.call, None, None, "Choose new note text, a new note date, or both."),
            (self.call, " \n\t", None, "Note cannot be empty."),
            (self.call, "Valid text", "2026-9-3", "valid date in YYYY-MM-DD"),
            (self.call, "Valid text", "2026-02-30", "valid date in YYYY-MM-DD"),
            (self.call, "Valid text", tomorrow, "Note date cannot be in the future."),
            (999, "Valid text", None, "Note 999 does not exist."),
        ):
            with self.subTest(body=body, noted_on=noted_on), self.assertRaisesRegex(ValueError, message):
                applytrack.edit_note(self.connection, note_id, body, noted_on)
        self.assertEqual(self.notes(), before)

    def test_deleting_removes_only_that_note_and_its_id_is_not_reused(self):
        other = applytrack.add_application(self.connection, "Other", "Engineer", applied_on="2026-09-01")
        theirs = applytrack.add_note(self.connection, other, "Theirs", "2026-09-02")
        applytrack.delete_note(self.connection, theirs)
        applytrack.delete_note(self.connection, self.call)
        self.assertEqual(self.notes(), [(self.sent, "2026-09-05", "Sent the take-home.")])
        self.assertEqual(self.notes(identifier=other), [])
        self.assertEqual(applytrack.get_application(self.connection, self.identifier)["company"], "Example")
        self.assertGreater(applytrack.add_note(self.connection, other, "New", "2026-09-04"), theirs)
        for action in (applytrack.delete_note, applytrack.get_note):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, f"Note {theirs} does not exist."):
                action(self.connection, theirs)

    def test_notes_of_a_deleted_application_cannot_be_edited(self):
        applytrack.delete_application(self.connection, self.identifier)
        with self.assertRaisesRegex(ValueError, f"Note {self.call} does not exist."):
            applytrack.edit_note(self.connection, self.call, "Too late")

    def test_search_and_backup_see_the_edited_text_and_not_the_deleted_note(self):
        applytrack.edit_note(self.connection, self.call, "Phone screen with Priya.")
        applytrack.delete_note(self.connection, self.sent)
        self.assertEqual(applytrack.list_applications(self.connection, search="sam"), [])
        self.assertEqual(len(applytrack.list_applications(self.connection, search="priya")), 1)
        saved = json.loads(json.dumps(applytrack.snapshot(self.connection)))
        self.assertEqual(saved["applications"][0]["notes"],
                         [{"noted_on": "2026-09-03", "body": "Phone screen with Priya."}])

    def test_note_ids_line_up_when_their_widths_differ(self):
        notes = [{"id": 9, "noted_on": "2026-09-03", "body": "First\nsecond line"},
                 {"id": 10, "noted_on": "2026-09-04", "body": "Later"}]
        self.assertEqual(applytrack.note_lines(notes), [
            "    Note 9   2026-09-03  First",
            "                         second line",
            "    Note 10  2026-09-04  Later",
        ])

    def test_cli_shows_note_ids_and_edits_by_them(self):
        self.assertEqual(self.run_cli("note", "1", "Panel on Monday.", "--on", "2026-09-06")[:3],
                         (0, "Added note 3 to application #1.\n", ""))
        self.assertEqual(self.run_cli("edit-note", "1", "--text", "Call with Sam Lee.", "--on", "2026-09-04")[:3],
                         (0, "Updated note 1.\n", ""))
        self.assertEqual(self.run_cli("notes", "1")[1], (
            "#1  Example — Engineer\n"
            "    Note 1  2026-09-04  Call with Sam Lee.\n"
            "    Note 2  2026-09-05  Sent the take-home.\n"
            "    Note 3  2026-09-06  Panel on Monday.\n"
        ))

    def test_cli_reports_invalid_edits(self):
        before = self.notes()
        for arguments, message in (
            (("edit-note", "1"), "Choose new note text, a new note date, or both."),
            (("edit-note", "1", "--text", "  "), "Note cannot be empty."),
            (("edit-note", "1", "--on", "2026-13-01"), "Use a valid date in YYYY-MM-DD format."),
            (("edit-note", "999", "--text", "Call"), "Note 999 does not exist."),
            (("delete-note", "999", "--yes"), "Note 999 does not exist."),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(*arguments)[:3], (1, "", f"Error: {message}\n"))
        self.assertEqual(self.notes(), before)
        for arguments in (["edit-note"], ["edit-note", "one", "--text", "Call"], ["delete-note"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    applytrack.main(["--database", str(self.database), *arguments])
                self.assertEqual(raised.exception.code, 2)

    def test_cli_delete_shows_the_note_and_asks_first(self):
        code, output, errors, asked = self.run_cli("delete-note", str(self.call), answer=" Yes ")
        self.assertEqual((code, errors), (0, ""))
        asked.assert_called_once_with(f"Delete note {self.call}? [y/N] ")
        self.assertEqual(output, (
            "#1  Example — Engineer\n"
            "    Note 1  2026-09-03  Recruiter call with Sam.\n"
            "Deleted note 1 from application #1.\n"
        ))
        self.assertEqual([row[0] for row in self.notes()], [self.sent])

    def test_cli_declined_or_closed_confirmation_keeps_the_note(self):
        before = self.notes()
        for answer in ("", "n", "maybe", EOFError()):
            with self.subTest(answer=answer):
                code, _, errors, asked = self.run_cli("delete-note", str(self.call), answer=answer)
                self.assertEqual(code, 1)
                asked.assert_called_once()
                self.assertEqual(errors, "Nothing deleted.\n")
                self.assertEqual(self.notes(), before)

    def test_cli_yes_deletes_without_prompting_and_unknown_ids_never_prompt(self):
        code, output, errors, asked = self.run_cli("delete-note", str(self.sent), "--yes")
        self.assertEqual((code, output, errors), (0, "Deleted note 2 from application #1.\n", ""))
        asked.assert_not_called()
        code, _, errors, asked = self.run_cli("delete-note", "999", answer="y")
        self.assertEqual((code, errors), (1, "Error: Note 999 does not exist.\n"))
        asked.assert_not_called()


class EarlierDatabaseTests(unittest.TestCase):
    """Databases created before notes could be edited need no upgrade."""

    def test_notes_saved_by_the_previous_version_can_be_edited_and_deleted(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "applications.sqlite3"
        with closing(applytrack.connect(database)) as connection:
            applytrack.add_application(connection, "Example", "Engineer", applied_on="2026-09-01")
        with closing(sqlite3.connect(database)) as connection:
            connection.executemany("INSERT INTO notes (application_id, noted_on, body) VALUES (1, ?, ?)",
                                   [("2026-09-02", "Old note"), ("2026-09-03", "Another")])
            connection.commit()
            schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        with closing(applytrack.connect(database)) as connection:
            applytrack.edit_note(connection, 1, "Old note, corrected")
            applytrack.delete_note(connection, 2)
            self.assertEqual([tuple(row) for row in applytrack.application_notes(connection, 1)],
                             [(1, "2026-09-02", "Old note, corrected")])
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(), schema)


if __name__ == "__main__":
    unittest.main()
