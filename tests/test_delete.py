from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import applytrack


class DeleteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def ids(self):
        return [row["id"] for row in applytrack.list_applications(self.connection)]

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

    def test_delete_removes_only_that_application_and_survives_reopening(self):
        first = applytrack.add_application(self.connection, "First", "Engineer")
        second = applytrack.add_application(self.connection, "Second", "Engineer", "2026-09-10")
        third = applytrack.add_application(self.connection, "Third", "Engineer")
        applytrack.delete_application(self.connection, second)
        reopened = applytrack.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual([row["id"] for row in applytrack.list_applications(reopened)], [third, first])
        self.assertEqual(applytrack.list_applications(reopened, due_only=True), [])

    def test_unknown_identifier_reports_failure_and_changes_nothing(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer")
        for missing in (999, 0, -1):
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    applytrack.delete_application(self.connection, missing)
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    applytrack.get_application(self.connection, missing)
        self.assertEqual(self.ids(), [identifier])

    def test_deleting_twice_reports_failure(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer")
        applytrack.delete_application(self.connection, identifier)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            applytrack.delete_application(self.connection, identifier)

    def test_deleted_application_is_no_longer_skipped_on_import(self):
        applytrack.add_application(self.connection, "Example", "Engineer", applied_on="2026-09-01")
        source = "company,role,applied_on\nExample,Engineer,2026-09-01\n"
        self.assertEqual(applytrack.import_csv(self.connection, io.StringIO(source), dry_run=True), (0, 1))
        applytrack.delete_application(self.connection, self.ids()[0])
        self.assertEqual(applytrack.import_csv(self.connection, io.StringIO(source), dry_run=True), (1, 0))

    def test_cli_yes_deletes_without_prompting(self):
        keep = applytrack.add_application(self.connection, "Keep", "Engineer")
        remove = applytrack.add_application(self.connection, "Remove", "Engineer")
        code, output, errors, asked = self.run_cli("delete", str(remove), "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(output, f"Deleted application #{remove}.\n")
        self.assertEqual(errors, "")
        asked.assert_not_called()
        self.assertEqual(self.ids(), [keep])

    def test_cli_confirmation_names_the_application_and_accepts_yes(self):
        for answer in ("y", " YES "):
            with self.subTest(answer=answer):
                identifier = applytrack.add_application(self.connection, "Example", "Engineer")
                code, output, _, asked = self.run_cli("delete", str(identifier), answer=answer)
                self.assertEqual(code, 0)
                asked.assert_called_once_with(f"Delete #{identifier}  Example — Engineer? [y/N] ")
                self.assertIn(f"Deleted application #{identifier}.", output)
                self.assertNotIn(identifier, self.ids())

    def test_cli_declined_or_closed_confirmation_keeps_the_application(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer", "2026-09-10")
        original = dict(applytrack.get_application(self.connection, identifier))
        for answer in ("", "n", "no", "maybe", EOFError()):
            with self.subTest(answer=answer):
                code, output, errors, asked = self.run_cli("delete", str(identifier), answer=answer)
                self.assertEqual(code, 1)
                asked.assert_called_once()
                self.assertEqual(output, "")
                self.assertIn("Nothing deleted.", errors)
                self.assertEqual(dict(applytrack.get_application(self.connection, identifier)), original)

    def test_cli_unknown_identifier_fails_before_prompting(self):
        applytrack.add_application(self.connection, "Example", "Engineer")
        for extra in ((), ("--yes",)):
            with self.subTest(extra=extra):
                code, _, errors, asked = self.run_cli("delete", "999", *extra, answer="y")
                self.assertEqual(code, 1)
                asked.assert_not_called()
                self.assertIn("Application 999 does not exist.", errors)
        self.assertEqual(len(self.ids()), 1)

    def test_cli_confirmation_escapes_terminal_control_characters(self):
        identifier = applytrack.add_application(self.connection, "Evil\x1b[2J", "Engineer\nRemote")
        _, _, _, asked = self.run_cli("delete", str(identifier), answer="n")
        asked.assert_called_once_with(rf"Delete #{identifier}  Evil\x1b[2J — Engineer\nRemote? [y/N] ")


if __name__ == "__main__":
    unittest.main()
