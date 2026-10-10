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


if __name__ == "__main__":
    unittest.main()
