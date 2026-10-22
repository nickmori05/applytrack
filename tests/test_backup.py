from contextlib import closing, redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

import applytrack


def run_cli(database, *arguments, stdin=None):
    output = io.StringIO()
    errors = io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors), mock.patch("sys.stdin", io.StringIO(stdin or "")):
        code = applytrack.main(["--database", str(database), *arguments])
    return code, output.getvalue(), errors.getvalue()


def table_rows(connection):
    return {
        "applications": [tuple(row) for row in connection.execute("SELECT * FROM applications ORDER BY id")],
        "status_changes": [tuple(row) for row in connection.execute(
            "SELECT application_id, status, changed_on FROM status_changes ORDER BY application_id, changed_on, id")],
        "notes": [tuple(row) for row in connection.execute(
            "SELECT application_id, noted_on, body FROM notes ORDER BY application_id, noted_on, id")],
    }


class BackupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.database = self.folder / "applications.sqlite3"
        self.connection = self.open(self.database)

    def open(self, path):
        connection = applytrack.connect(path)
        self.addCleanup(connection.close)
        return connection

    def populate(self):
        first = applytrack.add_application(self.connection, "Straße, \"Labs\"", "=Backend\tEngineer",
                                           "2026-09-20", applied_on="2026-09-01")
        applytrack.update_application(self.connection, first, status="interviewing", changed_on="2026-09-05")
        applytrack.update_application(self.connection, first, status="offer", changed_on="2026-09-05")
        applytrack.add_note(self.connection, first, "Agenda:\r\n- system design\x1b[2J", "2026-09-04")
        applytrack.add_note(self.connection, first, "Second note, same day.", "2026-09-04")
        applytrack.add_note(self.connection, first, "Referral before applying.", "2026-08-20")
        second = applytrack.add_application(self.connection, "Archived Co", "Analyst", applied_on="2026-09-02")
        applytrack.archive_application(self.connection, second)
        deleted = applytrack.add_application(self.connection, "Deleted", "Engineer", applied_on="2026-09-03")
        applytrack.delete_application(self.connection, deleted)
        return first, second, deleted

    def backup_text(self, connection=None):
        output = io.StringIO()
        applytrack.write_backup(applytrack.snapshot(connection or self.connection), output)
        return output.getvalue()

    def restore_text(self, text, connection, **options):
        records, last_id = applytrack.read_backup(io.StringIO(text))
        applytrack.restore_backup(connection, records, last_id, **options)
        return applytrack.record_counts(records)

    def test_backup_round_trips_every_record_and_keeps_retired_ids(self):
        first, second, deleted = self.populate()
        text = self.backup_text()
        restored = self.open(self.folder / "restored.sqlite3")
        counts = self.restore_text(text, restored)
        self.assertEqual(str(counts), "2 applications, 2 status changes, and 3 notes")
        self.assertEqual(table_rows(restored), table_rows(self.connection))
        self.assertEqual(self.backup_text(restored), text)
        self.assertEqual([row["id"] for row in applytrack.list_applications(restored, archived=True)], [second])
        self.assertEqual([note["body"] for note in applytrack.application_notes(restored, first)],
                         ["Referral before applying.", "Agenda:\r\n- system design\x1b[2J", "Second note, same day."])
        self.assertEqual([tuple(row) for row in applytrack.status_changes(restored, first)],
                         [("interviewing", "2026-09-05"), ("offer", "2026-09-05")])
        self.assertEqual(applytrack.add_application(restored, "Next", "Engineer"), deleted + 1)

    def test_backup_format_is_documented_json(self):
        first, _, _ = self.populate()
        data = json.loads(self.backup_text())
        self.assertEqual((data["format"], data["version"], data["last_id"]), ("applytrack-backup", 1, 3))
        self.assertEqual(data["applications"][0], {
            "id": first, "company": "Straße, \"Labs\"", "role": "=Backend\tEngineer", "status": "offer",
            "applied_on": "2026-09-01", "follow_up_on": "2026-09-20", "archived_on": None,
            "status_history": [{"status": "interviewing", "changed_on": "2026-09-05"},
                               {"status": "offer", "changed_on": "2026-09-05"}],
            "notes": [{"noted_on": "2026-08-20", "body": "Referral before applying."},
                      {"noted_on": "2026-09-04", "body": "Agenda:\r\n- system design\x1b[2J"},
                      {"noted_on": "2026-09-04", "body": "Second note, same day."}],
        })
        self.assertIn("Straße", self.backup_text())

    def test_empty_database_round_trips(self):
        text = self.backup_text()
        self.assertEqual(json.loads(text), {"format": "applytrack-backup", "version": 1, "last_id": 0,
                                            "applications": []})
        restored = self.open(self.folder / "restored.sqlite3")
        self.assertEqual(str(self.restore_text(text, restored)), "0 applications, 0 status changes, and 0 notes")
        self.assertEqual(applytrack.add_application(restored, "First", "Engineer"), 1)

    def test_restoring_keeps_a_higher_sequence_in_the_target(self):
        text = self.backup_text()
        target = self.open(self.folder / "target.sqlite3")
        for company in ("One", "Two"):
            applytrack.delete_application(target, applytrack.add_application(target, company, "Engineer"))
        self.restore_text(text, target)
        self.assertEqual(applytrack.add_application(target, "Three", "Engineer"), 3)

    def test_restore_refuses_a_database_with_saved_records(self):
        self.populate()
        text = self.backup_text()
        before = table_rows(self.connection)
        for options in ({}, {"dry_run": True}):
            with self.subTest(options=options), self.assertRaisesRegex(
                    ValueError, "Restore needs an empty database, but this one has 2 applications."):
                self.restore_text(text, self.connection, **options)
        self.assertEqual(table_rows(self.connection), before)
        leftover = self.open(self.folder / "leftover.sqlite3")
        with leftover:
            leftover.execute("INSERT INTO notes (application_id, noted_on, body) VALUES (7, '2026-09-01', 'x')")
        with self.assertRaisesRegex(ValueError, "this one has status history or notes"):
            self.restore_text(text, leftover)

    def test_dry_run_checks_without_saving(self):
        self.populate()
        target = self.open(self.folder / "target.sqlite3")
        self.assertEqual(self.restore_text(self.backup_text(), target, dry_run=True).applications, 2)
        self.assertEqual(table_rows(target), {"applications": [], "status_changes": [], "notes": []})

    def test_failure_while_restoring_saves_nothing(self):
        self.populate()
        target = self.open(self.folder / "target.sqlite3")
        with target:
            target.execute("CREATE TRIGGER refuse BEFORE INSERT ON notes BEGIN SELECT RAISE(ABORT, 'refused'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "refused"):
            self.restore_text(self.backup_text(), target)
        self.assertEqual(table_rows(target), {"applications": [], "status_changes": [], "notes": []})
        self.assertEqual(target.execute("SELECT seq FROM sqlite_sequence").fetchall(), [])

    def test_invalid_backups_are_rejected_without_saving(self):
        self.populate()
        valid = json.loads(self.backup_text())

        def changed(path, value):
            data = copy.deepcopy(valid)
            *parents, last = path
            target = data
            for key in parents:
                target = target[key]
            if value is KeyError:
                del target[last]
            else:
                target[last] = value
            return json.dumps(data)

        first = ("applications", 0)
        history = (*first, "status_history")
        cases = [
            ("", "Backup is not valid JSON: Expecting value"),
            ("[]", "File is not an ApplyTrack backup."),
            (changed(("format",), "other"), "File is not an ApplyTrack backup."),
            (changed(("version",), 2), "Backup version 2 needs a newer ApplyTrack, which reads version 1."),
            (changed(("version",), "1"), "Backup has an invalid version."),
            (changed(("version",), True), "Backup has an invalid version."),
            (changed(("extra",), 1), "Backup has an unknown field: extra."),
            (changed(("last_id",), KeyError), "Backup is missing last_id."),
            (changed(("last_id",), 1), "Backup last_id is lower than an application ID."),
            (changed(("last_id",), -1), "Backup last_id must be a whole number of at least 0."),
            (changed(("applications",), {}), "applications must be a list."),
            (changed(first, "x"), "Application 1 in the backup: Application must be a JSON object."),
            (changed((*first, "id"), 0), "Application 1 in the backup: ID must be a whole number of at least 1."),
            (changed((*first, "id"), True), "ID must be a whole number of at least 1."),
            (changed((*first, "id"), "1"), "ID must be a whole number of at least 1."),
            (changed(("applications", 1, "id"), 1), "Application 2 in the backup repeats ID 1."),
            (changed((*first, "company"), "  "), "Application 1 in the backup: Company cannot be empty."),
            (changed((*first, "role"), 5), "Role must be text."),
            (changed((*first, "status"), "ghosted"), "Status must be one of: applied"),
            (changed((*first, "applied_on"), "2026-02-30"), "Use a valid date in YYYY-MM-DD format."),
            (changed((*first, "follow_up_on"), "20260920"), "Use a valid date in YYYY-MM-DD format."),
            (changed((*first, "archived_on"), 20260920), "Archive date must be text."),
            (changed((*first, "notes"), KeyError), "Application is missing notes."),
            (changed((*history, 0, "changed_on"), "2026-08-31"),
             "Status change 1 is dated before the submission date."),
            (changed((*history, 1, "changed_on"), "2026-09-04"),
             "Status change 2 is dated before the change listed before it."),
            (changed((*history, 0, "status"), "hired"), "Status must be one of"),
            (changed((*history, 0, "when"), "2026-09-05"), "Status change 1 has an unknown field: when."),
            (changed((*first, "notes", 1, "body"), "\n"), "Application 1 in the backup: Note 2 cannot be empty."),
            (changed((*first, "notes", 0), []), "Note 1 must be a JSON object."),
        ]
        target = self.open(self.folder / "target.sqlite3")
        for text, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, re.escape(message)):
                self.restore_text(text, target)
        self.assertEqual(table_rows(target), {"applications": [], "status_changes": [], "notes": []})

    def test_save_backup_refuses_to_overwrite_without_replace(self):
        self.populate()
        path = self.folder / "backup.json"
        path.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists. Choose another file or pass --force"):
            applytrack.save_backup(self.connection, path)
        self.assertEqual(path.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(applytrack.save_backup(self.connection, path, replace=True).notes, 3)
        self.assertEqual(path.read_text(encoding="utf-8"), self.backup_text())

    def test_interrupted_backup_leaves_the_old_file_and_no_partial_file(self):
        path = self.folder / "backup.json"
        path.write_text("previous backup", encoding="utf-8")

        def fail(data, output):
            output.write("{partial")
            raise OSError("disk full")

        with mock.patch("applytrack.backup.write_backup", fail), self.assertRaisesRegex(OSError, "disk full"):
            applytrack.save_backup(self.connection, path, replace=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "previous backup")
        self.assertEqual(sorted(item.name for item in self.folder.iterdir()), ["applications.sqlite3", "backup.json"])

    @unittest.skipIf(os.name != "posix", "file modes are POSIX-specific")
    def test_backup_file_is_readable_only_by_its_owner(self):
        path = self.folder / "backup.json"
        applytrack.save_backup(self.connection, path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class BackupCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.database = self.folder / "applications.sqlite3"
        run_cli(self.database, "add", "Example", "Engineer", "--applied-on", "2026-09-01")
        run_cli(self.database, "update", "1", "--status", "interviewing", "--changed-on", "2026-09-10")
        run_cli(self.database, "note", "1", "Recruiter call with Sam.", "--on", "2026-09-03")
        run_cli(self.database, "archive", "1")

    def test_backup_and_restore_through_files(self):
        path = self.folder / "backup.json"
        self.assertEqual(run_cli(self.database, "backup", str(path)),
                         (0, f"Backed up 1 application, 1 status change, and 1 note to {path}.\n", ""))
        self.assertEqual(run_cli(self.database, "backup", str(path)), (1, "", (
            f"Error: {path} already exists. Choose another file or pass --force to replace it.\n")))
        self.assertEqual(run_cli(self.database, "backup", str(path), "--force")[0], 0)
        restored = self.folder / "restored.sqlite3"
        self.assertEqual(run_cli(restored, "restore-backup", "--dry-run", str(path)),
                         (0, "Backup is valid: 1 application, 1 status change, and 1 note ready to restore.\n", ""))
        self.assertEqual(run_cli(restored, "list", "--archived")[1], "No archived applications.\n")
        self.assertEqual(run_cli(restored, "restore-backup", str(path)),
                         (0, "Restored 1 application, 1 status change, and 1 note.\n", ""))
        self.assertEqual(run_cli(restored, "notes", "1")[1],
                         "#1  Example — Engineer\n    2026-09-03  Recruiter call with Sam.\n")
        self.assertEqual(run_cli(restored, "history", "1")[1],
                         "#1  Example — Engineer\n    2026-09-01  applied\n    2026-09-10  interviewing\n")
        self.assertIn("Archived: ", run_cli(restored, "list", "--archived")[1])
        self.assertEqual(run_cli(restored, "restore-backup", str(path)), (1, "", (
            "Error: Restore needs an empty database, but this one has 1 application. "
            "Use --database to restore into a new file.\n")))

    def test_standard_output_and_input_and_a_byte_order_mark(self):
        code, text, errors = run_cli(self.database, "backup", "-")
        self.assertEqual((code, errors, json.loads(text)["last_id"]), (0, "", 1))
        restored = self.folder / "restored.sqlite3"
        self.assertEqual(run_cli(restored, "restore-backup", "-", stdin=text)[0], 0)
        self.assertEqual(run_cli(restored, "backup", "-")[1], text)
        path = self.folder / "bom.json"
        path.write_text(text, encoding="utf-8-sig")
        self.assertEqual(run_cli(self.folder / "bom.sqlite3", "restore-backup", str(path))[0], 0)

    def test_errors_are_reported_with_exit_codes(self):
        missing = self.folder / "missing.json"
        code, output, errors = run_cli(self.database, "restore-backup", str(missing))
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Error: [Errno 2] No such file or directory", errors)
        code, output, errors = run_cli(self.database, "backup", str(self.folder / "no" / "backup.json"))
        self.assertEqual((code, output), (1, ""))
        self.assertIn("No such file or directory", errors)
        self.assertEqual(run_cli(self.folder / "new.sqlite3", "restore-backup", "-", stdin="{}"),
                         (1, "", "Error: File is not an ApplyTrack backup.\n"))
        for arguments in (["backup"], ["restore-backup"], ["restore-backup", "a.json", "--force"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    applytrack.main(["--database", str(self.database), *arguments])
                self.assertEqual(raised.exception.code, 2)


class OlderDatabaseBackupTests(unittest.TestCase):
    def test_database_from_before_ids_were_unique_is_upgraded_then_backed_up(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("""CREATE TABLE applications (
                id INTEGER PRIMARY KEY, company TEXT NOT NULL, role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'applied', applied_on TEXT NOT NULL, follow_up_on TEXT)""")
            connection.executemany("INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?)", [
                (2, "Second", "Engineer", "interviewing", "2026-09-02", "2026-09-20"),
                (5, "Fifth", "Analyst", "withdrawn", "2026-09-03", None),
            ])
            connection.commit()
        code, text, _ = run_cli(database, "backup", "-")
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertEqual((data["last_id"], [item["id"] for item in data["applications"]]), (5, [2, 5]))
        self.assertEqual(data["applications"][0]["status_history"], [])
        restored = Path(directory.name) / "restored.sqlite3"
        self.assertEqual(run_cli(restored, "restore-backup", "-", stdin=text)[0], 0)
        self.assertEqual(run_cli(restored, "history", "2")[1],
                         "#2  Second — Engineer\n    2026-09-02  applied\n    unknown     interviewing\n")


if __name__ == "__main__":
    unittest.main()
