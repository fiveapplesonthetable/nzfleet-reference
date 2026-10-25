import datetime
import hashlib
import os
import stat
import tempfile
import unittest

from nzfleet import migrations

NOW = datetime.datetime(2026, 10, 23, 9, 8, 7, tzinfo=datetime.timezone.utc)


def write(path, text):
    with open(path, "w") as out:
        out.write(text)


class NewMigration(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, migrations.DIR))

    def test_a_migration_is_versioned_by_the_utc_second(self):
        path = migrations.create(self.root, "a_payment_is_counted", NOW)
        self.assertEqual(path, f"{migrations.DIR}/20261023090807_a_payment_is_counted.sql")
        self.assertEqual(os.path.getsize(os.path.join(self.root, path)), 0)

    def test_a_local_time_is_converted_to_utc(self):
        zone = datetime.timezone(datetime.timedelta(hours=2))
        path = migrations.create(self.root, "x", NOW.astimezone(zone))
        self.assertIn("20261023090807_", path)

    def test_a_version_already_taken_is_refused(self):
        migrations.create(self.root, "first", NOW)
        with self.assertRaises(ValueError):
            migrations.create(self.root, "second", NOW)

    def test_a_name_that_is_not_snake_case_is_refused(self):
        for name in ("", "Upper", "two__under", "_lead", "trail_", "a-b"):
            with self.assertRaises(ValueError, msg=name):
                migrations.create(self.root, name, NOW)


class TemplateDatabase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.dir = os.path.join(self.work, "migrations")
        os.makedirs(self.dir)
        write(os.path.join(self.dir, "0002_second_one.sql"), "create table a (id int);\n")
        write(os.path.join(self.dir, "0001_first.sql"), "create table b (id int);\n")
        write(os.path.join(self.dir, "20260101000000_stamped.sql"), "create table c (id int);\n")
        self.calls = os.path.join(self.work, "calls")
        self.psql = os.path.join(self.work, "psql")
        write(self.psql, f'#!/bin/sh\necho "$*" >>{self.calls}\n')
        os.chmod(self.psql, os.stat(self.psql).st_mode | stat.S_IXUSR)
        migrations.migrate_template("postgres://u:p@host:5432/nzela", self.dir, (self.psql,))
        with open(self.calls) as calls:
            self.calls = calls.read().splitlines()

    def sha(self, name):
        with open(os.path.join(self.dir, name), "rb") as source:
            return hashlib.sha384(source.read()).hexdigest()

    def called(self, text):
        return any(text in call for call in self.calls)

    def test_it_works_on_template1_of_the_same_server(self):
        self.assertTrue(all(c.startswith("postgres://u:p@host:5432/template1 ") for c in self.calls))

    def test_each_migration_is_recorded_by_version(self):
        self.assertTrue(self.called("values (1, 'first'"))

    def test_an_underscore_in_a_description_is_a_space(self):
        self.assertTrue(self.called("values (2, 'second one'"))

    def test_a_stamped_version_is_read_whole(self):
        self.assertTrue(self.called("values (20260101000000, 'stamped'"))

    def test_the_checksum_is_the_files_sha384(self):
        self.assertTrue(self.called(f"decode('{self.sha('0001_first.sql')}', 'hex')"))

    def test_files_are_applied_in_version_order(self):
        applied = [c.split(" -f ")[1].rsplit("/", 1)[1] for c in self.calls if " -f " in c]
        self.assertEqual(applied, ["0001_first.sql", "0002_second_one.sql", "20260101000000_stamped.sql"])


class TemplateOverStandardInput(unittest.TestCase):
    def test_a_psql_that_cannot_see_the_files_gets_them_on_stdin(self):
        work = tempfile.mkdtemp()
        directory = os.path.join(work, "m")
        os.makedirs(directory)
        write(os.path.join(directory, "0001_first.sql"), "create table b (id int);\n")
        psql = os.path.join(work, "psql")
        seen = os.path.join(work, "seen")
        write(psql, f'#!/bin/sh\necho "$*" >>{seen}\ncat >>{seen}\n')
        os.chmod(psql, 0o755)
        migrations.migrate_template("postgres://u:p@h/db", directory, (psql,), stdin=True)
        with open(seen) as out:
            text = out.read()
        self.assertIn("-f -", text)
        self.assertIn("create table b (id int);", text)


if __name__ == "__main__":
    unittest.main()
