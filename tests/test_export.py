from contextlib import redirect_stdout
import csv
from datetime import date
import io
from pathlib import Path
import tempfile
import unittest

import applytrack


class ExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "applications.sqlite3"
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def export(self, **filters):
        output = io.StringIO(newline="")
        applytrack.export_csv(applytrack.list_applications(self.connection, **filters), output)
        return list(csv.DictReader(io.StringIO(output.getvalue(), newline="")))

    def test_csv_preserves_commas_quotes_newlines_and_unicode(self):
        company = 'Example, "Labs"'
        role = "Backend engineer\nRemote — Montréal"
        identifier = applytrack.add_application(self.connection, company, role)
        rows = self.export()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], str(identifier))
        self.assertEqual(rows[0]["company"], company)
        self.assertEqual(rows[0]["role"], role)
        self.assertEqual(rows[0]["follow_up_on"], "")

    def test_formula_like_text_is_escaped_without_changing_the_database(self):
        for company in ("=1+1", "+formula", "-formula", "@formula"):
            applytrack.add_application(self.connection, company, "Engineer")
        original = applytrack.list_applications(self.connection)
        exported = self.export()
        self.assertEqual([row["company"] for row in exported],
                         ["'" + row["company"] for row in original])
        self.assertEqual([row["company"] for row in applytrack.list_applications(self.connection)],
                         [row["company"] for row in original])

    def test_empty_export_still_contains_column_names(self):
        output = io.StringIO()
        applytrack.export_csv([], output)
        self.assertEqual(output.getvalue(), ",".join(applytrack.EXPORT_COLUMNS) + "\n")

    def test_status_filter_combines_with_due_dates_and_closed_exclusion(self):
        interviewing = applytrack.add_application(self.connection, "Interview", "Engineer", "2026-09-09")
        rejected = applytrack.add_application(self.connection, "Closed", "Engineer", "2026-09-08")
        applytrack.add_application(self.connection, "Applied", "Engineer", "2026-09-09")
        applytrack.update_application(self.connection, interviewing, status="interviewing")
        applytrack.update_application(self.connection, rejected, status="rejected")
        rows = self.export(due_only=True, today=date(2026, 9, 9), status="interviewing")
        self.assertEqual([row["id"] for row in rows], [str(interviewing)])
        self.assertEqual(self.export(due_only=True, today=date(2026, 9, 9), status="rejected"), [])

    def test_invalid_status_filter_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Status must be one of"):
            applytrack.list_applications(self.connection, status="unknown")

    def test_cli_export_outputs_csv_without_progress_messages(self):
        identifier = applytrack.add_application(self.connection, "Example", "Engineer")
        applytrack.add_application(self.connection, "Other", "Engineer")
        applytrack.update_application(self.connection, identifier, status="interviewing")
        output = io.StringIO()
        with redirect_stdout(output):
            code = applytrack.main(["--database", str(self.database), "export", "--status", "interviewing"])
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual([row["company"] for row in rows], ["Example"])

    def test_empty_filtered_list_does_not_claim_the_database_is_empty(self):
        applytrack.add_application(self.connection, "Example", "Engineer")
        output = io.StringIO()
        with redirect_stdout(output):
            code = applytrack.main(["--database", str(self.database), "list", "--status", "offer"])
        self.assertEqual(code, 0)
        self.assertIn("No applications match these filters", output.getvalue())


if __name__ == "__main__":
    unittest.main()
