from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
from pathlib import Path
import tempfile
import unittest

import applytrack


class ImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def load(self, text):
        return applytrack.import_csv(self.connection, io.StringIO(text, newline=""))

    def rows(self):
        return [dict(row) for row in applytrack.list_applications(self.connection)][::-1]

    def test_imports_all_columns_and_fills_defaults(self):
        count = self.load(
            "company,role,status,applied_on,follow_up_on\n"
            'Example Labs,"Backend, Remote",interviewing,2026-08-03,2026-09-01\n'
            "  Second Co  ,Engineer,,,\n"
        )
        self.assertEqual(count, 2)
        first, second = self.rows()
        self.assertEqual((first["company"], first["role"], first["status"]),
                         ("Example Labs", "Backend, Remote", "interviewing"))
        self.assertEqual((first["applied_on"], first["follow_up_on"]), ("2026-08-03", "2026-09-01"))
        self.assertEqual((second["company"], second["status"]), ("Second Co", "applied"))
        self.assertEqual((second["applied_on"], second["follow_up_on"]), (date.today().isoformat(), None))

    def test_headers_ignore_case_order_and_an_exported_id_column(self):
        self.load("ID, Role ,Company\n99,Engineer,Example\n")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["id"], 99)
        self.assertEqual((rows[0]["company"], rows[0]["role"]), ("Example", "Engineer"))

    def test_any_invalid_row_rejects_the_whole_file_with_its_row_number(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        cases = (
            ("Example,\n", "Row 3: Role cannot be empty"),
            ("Example,Engineer,hired\n", "Row 3: Status must be one of"),
            (f"Example,Engineer,,{future}\n", "Row 3: Submission date cannot be in the future"),
            ("Example,Engineer,,,2026-02-30\n", "Row 3: Use a valid date"),
            ("Example,Engineer,,,,extra\n", "Row 3: Row has more values than columns"),
        )
        for bad_row, message in cases:
            with self.subTest(bad_row=bad_row), self.assertRaisesRegex(ValueError, message):
                self.load("company,role,status,applied_on,follow_up_on\nGood,Engineer,,,\n" + bad_row)
        self.assertEqual(self.rows(), [])

    def test_header_problems_are_reported_before_any_rows(self):
        cases = (
            ("", "no header row"),
            ("company\nExample\n", "needs a role column"),
            ("company,role,notes\nExample,Engineer,x\n", "Unknown CSV column: notes"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                self.load(text)
        self.assertEqual(self.rows(), [])

    def test_header_only_file_imports_nothing(self):
        self.assertEqual(self.load("company,role\n"), 0)
        self.assertEqual(self.rows(), [])

    def test_exported_file_round_trips_including_formula_like_text(self):
        for company, role in (("=1+1", "Engineer"), ("'Quoted", "-Remote"), ('Example, "Labs"', "Line\nTwo")):
            applytrack.add_application(self.connection, company, role, "2026-09-01", applied_on="2026-08-01")
        exported = io.StringIO(newline="")
        applytrack.export_csv(applytrack.list_applications(self.connection), exported)
        before = self.rows()
        self.assertEqual(self.load(exported.getvalue()), 3)
        after = self.rows()[3:]
        fields = ("company", "role", "status", "applied_on", "follow_up_on")
        self.assertEqual([[row[f] for f in fields] for row in after][::-1],
                         [[row[f] for f in fields] for row in before])

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = applytrack.main(["--database", str(self.database), *args])
        return code, output.getvalue(), errors.getvalue()

    def test_cli_reads_a_spreadsheet_file_with_byte_order_mark(self):
        path = self.database.parent / "applications.csv"
        path.write_text("\ufeffcompany,role\nExample,Engineer\nOther,Designer\n", encoding="utf-8")
        self.assertEqual(self.run_cli("import", str(path)), (0, "Imported 2 applications.\n", ""))
        self.assertEqual([row["company"] for row in self.rows()], ["Example", "Other"])

    def test_cli_dry_run_checks_without_saving(self):
        path = self.database.parent / "applications.csv"
        path.write_text("company,role\nExample,Engineer\n", encoding="utf-8")
        self.assertEqual(self.run_cli("import", "--dry-run", str(path)),
                         (0, "1 application ready to import.\n", ""))
        self.assertEqual(self.rows(), [])

    def test_cli_reports_errors_and_missing_files_on_stderr(self):
        path = self.database.parent / "applications.csv"
        path.write_text("company,role\nExample,\n", encoding="utf-8")
        code, output, errors = self.run_cli("import", str(path))
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Row 2: Role cannot be empty", errors)
        code, output, errors = self.run_cli("import", str(path.with_name("missing.csv")))
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Error:", errors)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
