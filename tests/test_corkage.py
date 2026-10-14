"""End-to-end tests for Corkage.

Every test drives the real command line in a throwaway CORKAGE_HOME, so the
suite covers the whole contract: exit codes, streams, store integrity and the
exact empty-state hint lines.

    python3 -m pytest            (or: python3 -m unittest discover -s tests)
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "corkage.py"

PASSPHRASE = "cellar-pass-1"
DEMO_PASSPHRASE = "cenius"

EMPTY_CORK = (
    "No cork entries yet - add one with: "
    "corkage cork add --bottle NAME --condition sound"
)
EMPTY_READING = (
    "No cellar readings yet - add one with: "
    "corkage reading add --temp C --humidity PCT"
)
EMPTY_REMINDER = (
    "No reminders yet - add one with: "
    "corkage reminder add --kind soak --due YYYY-MM-DD --bottle NAME"
)


class CorkageTestBase(unittest.TestCase):
    """Shared harness: one temp cellar home per test, real subprocess runs."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="corkage-test-"))
        self.addCleanup(shutil.rmtree, str(self.home), ignore_errors=True)

    # -- harness ----------------------------------------------------------
    def run_cli(self, *args, passphrase=PASSPHRASE, home=None, stdin=""):
        env = dict(os.environ)
        env["CORKAGE_HOME"] = str(home if home is not None else self.home)
        env["NO_COLOR"] = "1"
        env.pop("CORKAGE_PASSPHRASE", None)
        env.pop("CORKAGE_DEBUG", None)
        if passphrase is not None:
            env["CORKAGE_PASSPHRASE"] = passphrase
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def init_cellar(self, passphrase=PASSPHRASE) -> None:
        result = self.run_cli(
            "init", passphrase=None, stdin=f"{passphrase}\n{passphrase}\n"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue(self.store_file.exists())

    @property
    def store_file(self) -> Path:
        return self.home / "store.json"

    def read_store(self):
        return json.loads(self.store_file.read_text(encoding="utf-8"))

    def snapshot(self):
        stat = self.store_file.stat()
        return self.store_file.read_bytes(), stat.st_mtime_ns

    def assert_store_untouched(self, before):
        content, mtime_ns = before
        self.assertEqual(self.store_file.read_bytes(), content)
        self.assertEqual(self.store_file.stat().st_mtime_ns, mtime_ns)

    def assert_single_error_line(self, result, needle, code=2):
        self.assertEqual(result.returncode, code, msg=result.stdout + result.stderr)
        self.assertIn(needle, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")


class HelpAndInitTests(CorkageTestBase):
    def test_help_lists_commands_and_exit_codes(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for word in ("init", "cork", "reading", "reminder", "demo"):
            self.assertIn(word, result.stdout)
        for line in ("0  ok", "2  usage or validation error", "3  cellar unlock"):
            self.assertIn(line, result.stdout)

    def test_version_flag(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("corkage 1.0.0", result.stdout)

    def test_init_refuses_short_passphrase_then_accepts(self):
        result = self.run_cli(
            "init", passphrase=None, stdin="ab\nsecret123\nsecret123\n"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("passphrase needs at least 4 characters", result.stderr)
        self.assertTrue(self.store_file.exists())

    def test_init_stores_only_salt_and_hash(self):
        passphrase = "bordeaux-2016"
        self.init_cellar(passphrase)
        raw = self.store_file.read_text(encoding="utf-8")
        self.assertNotIn(passphrase, raw)
        store = self.read_store()
        self.assertEqual(len(store["passphrase_salt"]), 32)
        self.assertEqual(len(store["passphrase_hash"]), 64)
        int(store["passphrase_hash"], 16)  # hex, not a readable secret
        self.assertNotIn("passphrase", {key for key in store if key == "passphrase"})
        self.assertEqual(os.stat(self.store_file).st_mode & 0o777, 0o600)

    def test_init_refuses_to_overwrite_an_existing_cellar(self):
        self.init_cellar()
        before = self.snapshot()
        second = self.run_cli("init", passphrase=None, stdin="fresh-pass\nfresh-pass\n")
        self.assertEqual(second.returncode, 3)
        self.assertIn("cellar already initialised at", second.stderr)
        self.assertIn(str(self.store_file), second.stderr)
        self.assert_store_untouched(before)

    def test_data_command_before_init_creates_nothing(self):
        result = self.run_cli("cork", "list")
        self.assertEqual(result.returncode, 3)
        self.assertIn("no cellar yet - run corkage init first", result.stderr)
        self.assertFalse(self.store_file.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_wrong_passphrase_is_one_line_and_leaves_store_alone(self):
        self.init_cellar()
        before = self.snapshot()
        result = self.run_cli("cork", "list", passphrase="not-the-passphrase")
        self.assertEqual(result.returncode, 3)
        self.assertIn("could not unlock the cellar (wrong passphrase)", result.stderr)
        self.assertEqual(result.stderr.strip().count("\n"), 0)
        self.assert_store_untouched(before)

    def test_unreadable_store_reports_a_store_error(self):
        self.init_cellar()
        self.store_file.write_text("{ not json", encoding="utf-8")
        result = self.run_cli("cork", "list")
        self.assertEqual(result.returncode, 3)
        self.assertIn("could not be read", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_passphrase_can_come_from_the_flag(self):
        self.init_cellar()
        added = self.run_cli(
            "cork", "add", "--bottle", "Barolo 2016", "--condition", "sound",
            passphrase=None,
            stdin="",
        )
        # no passphrase anywhere -> one clear line, not a hang
        self.assertEqual(added.returncode, 3)
        self.assertIn("no passphrase given", added.stderr)

        flagged = self.run_cli(
            "cork", "list", "--passphrase", PASSPHRASE, passphrase=None
        )
        self.assertEqual(flagged.returncode, 0, msg=flagged.stderr)
        self.assertIn(EMPTY_CORK, flagged.stdout)

    def test_bare_invocation_prints_status_and_usage(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Corkage 1.0.0", result.stdout)
        self.assertIn("usage: corkage", result.stdout)
        self.assertIn("cellar file:", result.stdout)


class CorkLogTests(CorkageTestBase):
    def test_empty_cork_log_prints_the_hint_and_exits_zero(self):
        self.init_cellar()
        result = self.run_cli("cork", "list")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), EMPTY_CORK)
        self.assertEqual(result.stderr, "")

    def test_cork_add_then_list_newest_first(self):
        self.init_cellar()
        first = self.run_cli(
            "cork", "add", "--bottle", "Rioja 2012", "--condition", "sound",
            "--note", "clean, firm",
        )
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertIn("logged cork #1 for Rioja 2012 (sound)", first.stdout)

        second = self.run_cli(
            "cork", "add", "--bottle", "Barolo 2016", "--condition", "SOAKED",
            "--note", "stained halfway up, slid out clean",
        )
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        # a soaked cork queues exactly one automatic decant reminder
        self.assertIn("decant reminder #", second.stdout)

        listed = self.run_cli("cork", "list")
        self.assertEqual(listed.returncode, 0, msg=listed.stderr)
        lines = [line for line in listed.stdout.splitlines() if line.strip()]
        self.assertIn("BOTTLE", lines[0])
        self.assertIn("Barolo 2016", lines[2])
        self.assertIn("soaked", lines[2])
        self.assertIn("stained halfway up, slid out clean", lines[2])
        self.assertIn("UTC", lines[2])
        self.assertIn("Rioja 2012", lines[3])
        self.assertIn("sound", lines[3])

    def test_unknown_condition_is_one_line_and_leaves_the_log_unchanged(self):
        self.init_cellar()
        self.run_cli("cork", "add", "--bottle", "Rioja 2012", "--condition", "sound")
        before = self.snapshot()

        result = self.run_cli("cork", "add", "--bottle", "Rioja", "--condition", "mushy")
        self.assert_single_error_line(result, "unknown --condition 'mushy'")
        for word in ("sound", "soaked", "crumbly", "moldy"):
            self.assertIn(word, result.stderr)
        self.assert_store_untouched(before)

        listed = self.run_cli("cork", "list")
        self.assertNotIn("mushy", listed.stdout)
        self.assertEqual(len(self.read_store()["corks"]), 1)

    def test_condition_match_is_case_insensitive(self):
        self.init_cellar()
        result = self.run_cli(
            "cork", "add", "--bottle", "Vouvray Sec 2018", "--condition", "CrUmBlY"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(self.read_store()["corks"][0]["condition"], "crumbly")

    def test_cork_add_without_bottle_prints_usage(self):
        self.init_cellar()
        before = self.snapshot()
        result = self.run_cli("cork", "add", "--condition", "sound")
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing required --bottle", result.stderr)
        self.assertIn("usage: corkage cork add", result.stderr)
        self.assert_store_untouched(before)

    def test_cork_list_never_writes_the_store(self):
        self.init_cellar()
        self.run_cli("cork", "add", "--bottle", "Barolo 2016", "--condition", "moldy")
        before = self.snapshot()
        self.run_cli("cork", "list")
        self.assert_store_untouched(before)

    def test_write_leaves_no_temp_files_behind(self):
        self.init_cellar()
        self.run_cli("cork", "add", "--bottle", "Barolo 2016", "--condition", "sound")
        leftovers = [p.name for p in self.home.iterdir() if p.name != "store.json"]
        self.assertEqual(leftovers, [])

    def test_non_ascii_bottle_names_round_trip(self):
        self.init_cellar()
        bottle = "Château Musar 2004"
        added = self.run_cli("cork", "add", "--bottle", bottle, "--condition", "sound")
        self.assertEqual(added.returncode, 0, msg=added.stderr)
        self.assertEqual(self.read_store()["corks"][0]["bottle"], bottle)
        listed = self.run_cli("cork", "list")
        self.assertIn(bottle, listed.stdout)


class ReadingTests(CorkageTestBase):
    def test_empty_reading_log_prints_the_hint_and_exits_zero(self):
        self.init_cellar()
        result = self.run_cli("reading", "list")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), EMPTY_READING)

    def test_reading_add_then_list_newest_first(self):
        self.init_cellar()
        first = self.run_cli(
            "reading", "add", "--temp", "12.5", "--humidity", "64", "--note", "start of winter"
        )
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertIn("recorded reading #1: 12.5 C, 64% humidity", first.stdout)

        second = self.run_cli(
            "reading", "add", "--temp", "13.5", "--humidity", "68", "--note", "after racking"
        )
        self.assertEqual(second.returncode, 0, msg=second.stderr)

        listed = self.run_cli("reading", "list")
        lines = [line for line in listed.stdout.splitlines() if line.strip()]
        self.assertIn("TEMP", lines[0])
        self.assertIn("13.5 C", lines[2])
        self.assertIn("68%", lines[2])
        self.assertIn("after racking", lines[2])
        self.assertIn("12.5 C", lines[3])

    def test_temp_out_of_range_names_the_flag_and_the_range(self):
        self.init_cellar()
        before = self.snapshot()
        result = self.run_cli("reading", "add", "--temp", "90", "--humidity", "68")
        self.assert_single_error_line(result, "--temp")
        self.assertIn("-5", result.stderr)
        self.assertIn("45", result.stderr)
        self.assert_store_untouched(before)

    def test_humidity_out_of_range_names_the_flag_and_the_range(self):
        self.init_cellar()
        result = self.run_cli("reading", "add", "--temp", "13", "--humidity", "140")
        self.assert_single_error_line(result, "--humidity")
        self.assertIn("0 and 100", result.stderr)
        self.assertEqual(self.read_store()["readings"], [])

    def test_non_numeric_values_never_raise_a_traceback(self):
        self.init_cellar()
        for args, needle in (
            (("--temp", "warm", "--humidity", "68"), "--temp needs a number"),
            (("--temp", "13", "--humidity", "damp"), "--humidity needs a number"),
            (("--temp", "nan", "--humidity", "68"), "--temp needs a number"),
        ):
            result = self.run_cli("reading", "add", *args)
            self.assert_single_error_line(result, needle)
            self.assertNotIn("Traceback", result.stderr + result.stdout)
        self.assertEqual(self.read_store()["readings"], [])

    def test_reading_list_never_writes_the_store(self):
        self.init_cellar()
        self.run_cli("reading", "add", "--temp", "13.5", "--humidity", "68")
        before = self.snapshot()
        self.run_cli("reading", "list")
        self.assert_store_untouched(before)


def days_from_today(offset: int) -> str:
    from datetime import date, timedelta

    return (date.today() + timedelta(days=offset)).isoformat()


class ReminderTests(CorkageTestBase):
    def test_empty_reminder_list_prints_the_hint_and_exits_zero(self):
        self.init_cellar()
        result = self.run_cli("reminder", "list")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), EMPTY_REMINDER)

    def test_manual_reminder_shows_kind_bottle_and_due_date(self):
        self.init_cellar()
        added = self.run_cli(
            "reminder", "add", "--kind", "soak", "--due", "2027-06-01",
            "--bottle", "Barolo", "--note", "soak overnight",
        )
        self.assertEqual(added.returncode, 0, msg=added.stderr)
        self.assertIn("added soak reminder #1 for Barolo due 2027-06-01", added.stdout)

        listed = self.run_cli("reminder", "list")
        self.assertEqual(listed.returncode, 0, msg=listed.stderr)
        lines = [line for line in listed.stdout.splitlines() if line.strip()]
        self.assertIn("KIND", lines[0])
        self.assertIn("soak", lines[2])
        self.assertIn("Barolo", lines[2])
        self.assertIn("2027-06-01", lines[2])
        self.assertNotIn("OVERDUE", lines[2])

    def test_unknown_kind_and_bad_due_date_are_one_line_each(self):
        self.init_cellar()
        before = self.snapshot()
        bad_kind = self.run_cli(
            "reminder", "add", "--kind", "snooze", "--due", "2027-06-01", "--bottle", "Barolo"
        )
        self.assert_single_error_line(bad_kind, "unknown --kind 'snooze'")
        self.assertIn("soak", bad_kind.stderr)

        bad_due = self.run_cli(
            "reminder", "add", "--kind", "decant", "--due", "01/06/2024", "--bottle", "Barolo"
        )
        self.assert_single_error_line(bad_due, "YYYY-MM-DD")
        self.assert_store_untouched(before)

    def test_soaked_and_crumbly_corks_queue_one_decant_reminder(self):
        self.init_cellar()
        self.run_cli("cork", "add", "--bottle", "Barolo", "--condition", "soaked")
        self.run_cli("cork", "add", "--bottle", "Barbaresco", "--condition", "sound")
        self.run_cli("cork", "add", "--bottle", "Sauternes", "--condition", "moldy")
        self.run_cli("cork", "add", "--bottle", "Vouvray", "--condition", "crumbly")

        reminders = self.read_store()["reminders"]
        self.assertEqual(len(reminders), 2)
        by_bottle = {reminder["bottle"]: reminder for reminder in reminders}
        self.assertEqual(set(by_bottle), {"Barolo", "Vouvray"})
        expected = {days_from_today(30), days_from_today(29), days_from_today(31)}
        for reminder in reminders:
            self.assertEqual(reminder["kind"], "decant")
            self.assertIsNone(reminder["done_at"])
            self.assertIn(reminder["due_date"], expected)

        listed = self.run_cli("reminder", "list")
        self.assertIn("Barolo", listed.stdout)
        self.assertNotIn("Barbaresco", listed.stdout)

        # a second soaked cork for the same bottle does not duplicate the nudge
        self.run_cli("cork", "add", "--bottle", "Barolo", "--condition", "crumbly")
        self.assertEqual(len(self.read_store()["reminders"]), 2)

    def test_reminder_done_clears_it_and_unknown_id_is_clear(self):
        self.init_cellar()
        self.run_cli(
            "reminder", "add", "--kind", "soak", "--due", days_from_today(3),
            "--bottle", "Barolo",
        )
        done = self.run_cli("reminder", "done", "--id", "1")
        self.assertEqual(done.returncode, 0, msg=done.stderr)
        self.assertIn("reminder #1 marked done", done.stdout)

        listed = self.run_cli("reminder", "list")
        self.assertEqual(listed.stdout.strip(), EMPTY_REMINDER)

        missing = self.run_cli("reminder", "done", "--id", "999")
        self.assert_single_error_line(missing, "no pending reminder with id 999")

        done_twice = self.run_cli("reminder", "done", "--id", "1")
        self.assert_single_error_line(done_twice, "no pending reminder with id 1")

        non_numeric = self.run_cli("reminder", "done", "--id", "abc")
        self.assert_single_error_line(non_numeric, "--id needs a whole number")

    def test_past_due_date_is_marked_overdue(self):
        self.init_cellar()
        self.run_cli(
            "reminder", "add", "--kind", "decant", "--due", "2020-01-01",
            "--bottle", "Rioja Reserva 2012",
        )
        listed = self.run_cli("reminder", "list")
        self.assertEqual(listed.returncode, 0, msg=listed.stderr)
        self.assertIn("OVERDUE", listed.stdout)
        self.assertIn("2020-01-01", listed.stdout)

    def test_reminder_missing_required_flags_print_usage(self):
        self.init_cellar()
        before = self.snapshot()
        missing_due = self.run_cli("reminder", "add", "--kind", "soak", "--bottle", "Barolo")
        self.assertEqual(missing_due.returncode, 2)
        self.assertIn("missing required --due (YYYY-MM-DD)", missing_due.stderr)
        self.assertIn("usage: corkage reminder add", missing_due.stderr)

        missing_kind = self.run_cli("reminder", "add", "--due", "2027-01-01", "--bottle", "Barolo")
        self.assertEqual(missing_kind.returncode, 2)
        self.assertIn("missing required --kind", missing_kind.stderr)
        self.assert_store_untouched(before)


class ContractTests(CorkageTestBase):
    def test_unknown_flag_names_the_flag_and_prints_usage(self):
        self.init_cellar()
        before = self.snapshot()
        result = self.run_cli("reading", "add", "--temp", "20", "--humidity", "50", "--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--bogus", result.stderr)
        self.assertIn("usage: corkage reading add", result.stderr)
        self.assert_store_untouched(before)

    def test_group_without_action_explains_the_actions(self):
        self.init_cellar()
        result = self.run_cli("cork")
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing cork action", result.stderr)
        self.assertIn("usage: corkage cork", result.stderr)

    def test_validation_failures_leave_the_store_byte_identical(self):
        self.init_cellar()
        self.run_cli("cork", "add", "--bottle", "Barolo 2016", "--condition", "sound")
        self.run_cli("reading", "add", "--temp", "13.5", "--humidity", "68")
        before = self.snapshot()

        first = self.run_cli("reading", "add", "--temp", "99", "--humidity", "50")
        self.assert_single_error_line(first, "--temp")
        self.assert_store_untouched(before)

        second = self.run_cli(
            "reminder", "add", "--kind", "soak", "--due", "31/12/2024", "--bottle", "Barolo"
        )
        self.assert_single_error_line(second, "YYYY-MM-DD")
        self.assert_store_untouched(before)

        third = self.run_cli("cork", "add", "--bottle", "Rioja", "--condition", "squishy")
        self.assert_single_error_line(third, "unknown --condition")
        self.assert_store_untouched(before)

    def test_results_go_to_stdout_and_errors_to_stderr(self):
        self.init_cellar()
        self.run_cli("cork", "add", "--bottle", "Barolo 2016", "--condition", "sound")
        listed = self.run_cli("cork", "list")
        self.assertIn("Barolo 2016", listed.stdout)
        self.assertNotIn("Barolo 2016", listed.stderr)

        failed = self.run_cli("cork", "add", "--bottle", "Rioja", "--condition", "mushy")
        self.assertEqual(failed.stdout, "")
        self.assertNotEqual(failed.stderr, "")

    def test_store_file_is_plain_json_a_keeper_can_read(self):
        self.init_cellar()
        self.run_cli("cork", "add", "--bottle", "Barolo 2016", "--condition", "soaked")
        self.run_cli("reading", "add", "--temp", "13.5", "--humidity", "68", "--note", "steady")
        self.run_cli(
            "reminder", "add", "--kind", "soak", "--due", days_from_today(4),
            "--bottle", "Zinfandel 2014",
        )
        store = self.read_store()
        self.assertEqual(store["store_version"], 1)
        self.assertEqual(store["next_ids"], {"cork": 2, "reading": 2, "reminder": 3})
        self.assertEqual(store["corks"][0]["note"], "")
        self.assertEqual(store["readings"][0]["humidity_pct"], 68)
        self.assertEqual(
            sorted(item["kind"] for item in store["reminders"]), ["decant", "soak"]
        )


class DemoCellarTests(CorkageTestBase):
    def test_demo_seeds_the_standard_credential_and_every_log(self):
        seeded = self.run_cli("demo", passphrase=None)
        self.assertEqual(seeded.returncode, 0, msg=seeded.stderr)
        self.assertIn("demo cellar ready at", seeded.stdout)

        corks = self.run_cli("cork", "list", passphrase=DEMO_PASSPHRASE)
        self.assertEqual(corks.returncode, 0, msg=corks.stderr)
        self.assertIn("Barolo 2016", corks.stdout)
        self.assertIn("Vintage Port 1998", corks.stdout)

        readings = self.run_cli("reading", "list", passphrase=DEMO_PASSPHRASE)
        self.assertEqual(readings.returncode, 0, msg=readings.stderr)
        self.assertIn("C", readings.stdout)
        self.assertIn("%", readings.stdout)

        reminders = self.run_cli("reminder", "list", passphrase=DEMO_PASSPHRASE)
        self.assertEqual(reminders.returncode, 0, msg=reminders.stderr)
        self.assertIn("OVERDUE", reminders.stdout)
        self.assertIn("decant", reminders.stdout)

        rejected = self.run_cli("cork", "list", passphrase="not-cenius")
        self.assertEqual(rejected.returncode, 3)

    def test_demo_is_idempotent_and_resettable(self):
        self.run_cli("demo", passphrase=None)
        first = self.read_store()
        counts = (len(first["corks"]), len(first["readings"]), len(first["reminders"]))

        again = self.run_cli("demo", passphrase=None)
        self.assertEqual(again.returncode, 0)
        self.assertIn("already seeded", again.stdout)
        second = self.read_store()
        self.assertEqual(
            (len(second["corks"]), len(second["readings"]), len(second["reminders"])),
            counts,
        )

        self.run_cli(
            "cork", "add", "--bottle", "Nebbiolo 2015", "--condition", "sound",
            passphrase=DEMO_PASSPHRASE,
        )
        self.assertEqual(len(self.read_store()["corks"]), counts[0] + 1)
        self.run_cli("demo", "--reset", passphrase=None)
        self.assertEqual(len(self.read_store()["corks"]), counts[0])

    def test_demo_leaves_a_foreign_cellar_untouched(self):
        self.init_cellar(passphrase="my-own-passphrase")
        before = self.snapshot()
        result = self.run_cli("demo", passphrase=None)
        self.assertEqual(result.returncode, 0)
        self.assertIn("not the demo cellar", result.stderr)
        self.assert_store_untouched(before)


class SingleFileAuditTests(unittest.TestCase):
    def test_corkage_imports_only_the_standard_library(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, msg="relative imports are not allowed")
                if node.module:
                    imported.add(node.module.split(".")[0])
        self.assertTrue(imported)
        self.assertEqual(imported - set(sys.stdlib_module_names), set())
        self.assertNotIn("socket", imported)

    def test_no_project_files_besides_the_single_module(self):
        modules = sorted(
            path.name
            for path in ROOT.glob("*.py")
            if path.is_file()
        )
        self.assertEqual(modules, ["corkage.py"])


if __name__ == "__main__":
    unittest.main()
