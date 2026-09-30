from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import date
import io
from pathlib import Path
import tempfile
import unittest

import applytrack


class SearchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / 'applications.sqlite3'
        self.connection = applytrack.connect(self.database)
        self.addCleanup(self.connection.close)

    def add(self, company, role='Engineer', deadline=None, status='applied'):
        identifier = applytrack.add_application(self.connection, company, role, deadline)
        applytrack.update_application(self.connection, identifier, status=status)
        return identifier

    def ids(self, **filters):
        return [row['id'] for row in applytrack.list_applications(self.connection, **filters)]

    def test_search_matches_company_or_role_and_preserves_order(self):
        first = self.add('Backend Labs')
        second = self.add('Other Company', 'Senior Backend Engineer')
        self.add('Unrelated', 'Designer')
        self.assertEqual(self.ids(search='  BACKEND  '), [second, first])
        self.assertEqual(self.ids(search='missing'), [])
        self.assertEqual(len(self.ids()), 3)

    def test_unicode_casefold_works_after_reopening_without_dropping_accents(self):
        identifier = self.add('Straße Montréal', 'Développeur')
        reopened = applytrack.connect(self.database)
        self.addCleanup(reopened.close)
        for query in ('STRASSE', 'MONTRÉAL', 'DÉVELOPPEUR'):
            with self.subTest(query=query):
                rows = applytrack.list_applications(reopened, search=query)
                self.assertEqual([row['id'] for row in rows], [identifier])
                self.assertEqual(rows[0]['company'], 'Straße Montréal')
        self.assertEqual(self.ids(search='Montreal'), [])

    def test_special_characters_are_literal_and_sql_text_is_data(self):
        for query in ('%', '_', '\\', "O'Brien", "' OR 1=1 --"):
            with self.subTest(query=query):
                identifier = self.add('Contains ' + query)
                self.assertEqual(self.ids(search=query), [identifier])
        self.assertEqual(len(self.ids()), 5)

    def test_search_combines_with_status_due_dates_and_closed_exclusion(self):
        first = self.add('Match early', deadline='2026-09-28', status='interviewing')
        second = self.add('Match today', deadline='2026-09-30', status='interviewing')
        self.add('Match future', deadline='2026-10-01', status='interviewing')
        self.add('Match applied', deadline='2026-09-28')
        self.add('Unrelated', deadline='2026-09-28', status='interviewing')
        self.add('Match closed', deadline='2026-09-28', status='rejected')
        filters = {'search': 'match', 'due_only': True, 'today': date(2026, 9, 30)}
        self.assertEqual(self.ids(**filters, status='interviewing'), [first, second])
        self.assertEqual(self.ids(**filters, status='rejected'), [])

    def test_cli_list_and_csv_share_search_and_status_filters(self):
        self.add('Keep, "Labs"', 'Backend Engineer', status='interviewing')
        self.add('Skip', 'Backend Engineer')
        args = ['--database', str(self.database)]
        filters = ['--search', 'backend', '--status', 'interviewing']
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(applytrack.main(args + ['list'] + filters), 0)
        self.assertIn('Keep', output.getvalue())
        self.assertNotIn('Skip', output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(applytrack.main(args + ['export'] + filters), 0)
        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual([row['company'] for row in rows], ['Keep, "Labs"'])

    def test_empty_results_and_blank_search_have_distinct_cli_output(self):
        self.add('Example')
        args = ['--database', str(self.database)]
        for command in ('list', 'export'):
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(applytrack.main(args + [command, '--search', 'missing']), 0)
                expected = ('No applications match these filters.\n' if command == 'list'
                            else ','.join(applytrack.EXPORT_COLUMNS) + '\n')
                self.assertEqual(output.getvalue(), expected)
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(applytrack.main(args + [command, '--search', '  ']), 1)
                self.assertEqual(output.getvalue(), '')
                self.assertIn('Search cannot be empty', errors.getvalue())
        self.assertEqual(len(self.ids()), 1)


if __name__ == '__main__':
    unittest.main()
