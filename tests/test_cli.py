"""Unit tests for scc_valuation.cli.

No network and no real scc invocation: scc output is canned JSON.  The git
tests use a temporary local repository.
"""

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scc_valuation import cli  # noqa: E402


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "scc_by_file.json"
REPO_ROOT = Path("/tmp/deploy/x")


def load_fixture():
    with FIXTURE.open() as handle:
        return json.load(handle)


def absolute_fixture(repo_root=REPO_ROOT):
    data = copy.deepcopy(load_fixture())
    for language in data:
        for file_info in language["Files"]:
            file_info["Location"] = str(repo_root / file_info["Location"])
    return data


class ClassificationTests(unittest.TestCase):
    def test_every_rule_and_first_match_wins(self):
        cases = [
            # Generated before everything, including Data/Config/docs.
            ("gen/schemas/schema.ts", "TypeScript", "Generated"),
            ("src/gen/schemas/schema.json", "JSON", "Generated"),
            ("gen/schemas/README.md", "Markdown", "Generated"),
            ("drizzle/meta/0000.json", "JSON", "Generated"),
            ("drizzle/meta/README.md", "Markdown", "Generated"),
            ("migrations/meta/0001.json", "JSON", "Generated"),
            ("var/app.log", "Plain Text", "Generated"),
            ("src/__snapshots__/app.snap", "Snapshot", "Generated"),
            # Docs before Test/Config.
            ("README.md", "Markdown", "Docs"),
            (".github/README.md", "Markdown", "Docs"),
            ("docs/guide.rst", "ReStructuredText", "Docs"),
            ("notes.org", "Org", "Docs"),
            ("NOTES.txt", "Plain Text", "Docs"),
            ("LICENSE", "License", "Docs"),
            # Data, with the JSON config exception.
            ("data/records.json", "JSON", "Data"),
            ("data/records.jsonl", "JSONL", "Data"),
            ("data/table.csv", "CSV", "Data"),
            ("data/table.tsv", "TSV", "Data"),
            ("assets/icon.svg", "SVG", "Data"),
            ("data/feed.xml", "XML", "Data"),
            ("tests/fixtures/sample.json", "JSON", "Data"),
            ("package.json", "JSON", "Config"),
            ("tsconfig.json", "JSON", "Config"),
            ("tsconfig.build.json", "JSON", "Config"),
            ("vite.config.json", "JSON", "Config"),
            ("src/tauri.conf.json", "JSON", "Config"),
            ("devcontainer.json", "JSON", "Config"),
            (".devcontainer.json", "JSON", "Config"),
            # Test, then Config, then Code.
            ("tests/app.py", "Python", "Test"),
            ("src/foo_test.go", "Go", "Test"),
            ("test_root.py", "Python", "Test"),
            ("spec/app_spec.rb", "Ruby", "Test"),
            ("pkg/test_nested.py", "Python", "Code"),
            (".github/workflows/ci.yml", "YAML", "Config"),
            ("docker/Dockerfile", "Dockerfile", "Config"),
            ("infra/main.tf", "Terraform", "Config"),
            ("Makefile", "Makefile", "Config"),
            ("src/app.ts", "TypeScript", "Code"),
            ("src/main.go", "Go", "Code"),
        ]
        for path, language, expected in cases:
            with self.subTest(path=path, language=language):
                self.assertEqual(
                    cli.classify_file(path, language), expected
                )

    def test_fixture_inventory_has_every_class(self):
        stats, _langs, effective = cli.build_inventory(
            load_fixture(),
            REPO_ROOT,
            {
                "Code": 1.0, "Test": 1.0, "Config": 1.0,
                "Docs": 1.0, "Data": 1.0, "Generated": 1.0,
            },
        )
        self.assertEqual(
            {name: stats[name]["files"] for name in cli.CLASSES},
            {
                "Code": 4, "Test": 4, "Config": 11, "Docs": 6,
                "Data": 8, "Generated": 5,
            },
        )
        self.assertEqual(effective, 38.0)

    def test_default_weights_ignore_docs_data_generated(self):
        _stats, _langs, effective = cli.build_inventory(
            load_fixture(),
            REPO_ROOT,
            {
                "Code": 1.0, "Test": 0.5, "Config": 0.25,
                "Docs": 0.0, "Data": 0.0, "Generated": 0.0,
            },
        )
        # 4 Code + 0.5*4 Test + 0.25*11 Config
        self.assertEqual(effective, 8.75)

    def test_repo_under_deploy_tmp_is_not_misclassified(self):
        stats, _langs, effective = cli.build_inventory(
            absolute_fixture(),
            REPO_ROOT,
            {
                "Code": 1.0, "Test": 0.5, "Config": 0.25,
                "Docs": 0.0, "Data": 0.0, "Generated": 0.0,
            },
        )
        self.assertEqual(stats["Code"]["files"], 4)
        self.assertEqual(stats["Config"]["files"], 11)
        self.assertEqual(effective, 8.75)
        self.assertEqual(
            cli.normalize_rel_path("/tmp/deploy/x/src/app.ts", REPO_ROOT),
            "src/app.ts",
        )

    def test_run_scc_uses_repo_relative_cwd(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(cmd, 0, "[]", "")

        with patch.object(cli.subprocess, "run", side_effect=fake_run):
            cli.run_scc(REPO_ROOT)

        self.assertEqual(captured["cmd"][1], ".")
        self.assertEqual(captured["cwd"], str(REPO_ROOT))
        self.assertNotIn(str(REPO_ROOT), captured["cmd"])
        self.assertIn("tmp", ",".join(cli.DEFAULT_EXCLUDE_DIRS))


class PersonMonthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not installed")

        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name)
        subprocess.run(["git", "init", "-q", str(cls.repo)], check=True)

        cls._commit_count = 0
        # Alice Jan: 10 commits, under two display names and mixed-case email.
        for day in range(1, 11):
            name = "Alice" if day % 2 else "Alice Smith"
            email = "Alice@Example.com" if day % 2 else "alice@example.com"
            cls._commit(cls.repo, name, email, "2024-01-%02dT12:00:00+0000" % day)
        # Alice Feb: 1 commit.
        cls._commit(cls.repo, "Alice Smith", "alice@example.com",
                    "2024-02-01T12:00:00+0000")
        # Bob Jan: 5 commits; Bob Mar: 5 commits.
        for day in range(1, 6):
            cls._commit(cls.repo, "Bob", "bob@example.com",
                        "2024-01-%02dT12:00:00+0000" % day)
            cls._commit(cls.repo, "Bob", "bob@example.com",
                        "2024-03-%02dT12:00:00+0000" % day)
        # Bot Jan: 20 commits, must be excluded.
        for day in range(1, 21):
            cls._commit(
                cls.repo,
                "dependabot[bot]",
                "49699333+dependabot[bot]@users.noreply.github.com",
                "2024-01-%02dT12:00:00+0000" % day,
            )

    @classmethod
    def _commit(cls, repo, name, email, date):
        cls._commit_count += 1
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        })
        subprocess.run(
            [
                "git", "-C", str(repo), "commit", "-q", "--allow-empty",
                "--no-gpg-sign", "-m", "commit %d" % cls._commit_count,
            ],
            env=env,
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counts_at_strict_and_loose_thresholds(self):
        # N=10: Alice Jan only.  Bob never reaches 10 in a month; bots excluded.
        self.assertEqual(
            cli.count_person_months(self.repo, min_commits=10), 1
        )
        # N=1: Alice Jan + Alice Feb + Bob Jan + Bob Mar.
        self.assertEqual(
            cli.count_person_months(self.repo, min_commits=1), 4
        )

    def test_custom_bot_patterns_replace_defaults(self):
        # Exclude Alice via a custom pattern; the default bot list is replaced,
        # so the bot's January month is now counted.
        self.assertEqual(
            cli.count_person_months(
                self.repo, min_commits=1, bot_patterns=["alice"]
            ),
            3,
        )

    def test_invalid_threshold(self):
        with self.assertRaises(cli.ValuationError):
            cli.count_person_months(self.repo, min_commits=0)


class ModelTests(unittest.TestCase):
    def test_velocity_range_is_ordered(self):
        low, high = cli.months_range(10000, "velocity", 1000, 2000)
        self.assertEqual((low, high), (5.0, 10.0))
        self.assertLessEqual(low, high)

    def test_zero_sloc_is_zero(self):
        for model in ("velocity", "cocomo", "blend"):
            self.assertEqual(
                cli.months_range(0, model, 1000, 2000), (0.0, 0.0)
            )

    def test_acceptance_reference_math(self):
        ref_effective = 518984
        strict_pm = 36
        loose_pm = 63
        velocity_high = ref_effective / strict_pm
        velocity_low = ref_effective / loose_pm
        low, high = cli.months_range(
            36034.25, "velocity", velocity_low, velocity_high
        )
        self.assertAlmostEqual(low, 2.50, places=2)
        self.assertAlmostEqual(high, 4.37, places=2)
        self.assertLessEqual(low, high)

    def test_cocomo_formula(self):
        self.assertAlmostEqual(
            cli.cocomo_pm(1000, eaf=0.75), 2.94 * 0.75
        )
        self.assertEqual(cli.cocomo_pm(0, eaf=0.75), 0.0)

    def test_build_reference_uses_measured_months(self):
        args = cli.build_parser().parse_args([
            ".", "--calibrate-against", ".", "--min-commits-per-month", "10",
        ])
        stats, _, _ = cli.build_inventory(
            load_fixture(), REPO_ROOT,
            {"Code": 1.0, "Test": 0.5, "Config": 0.25,
             "Docs": 0.0, "Data": 0.0, "Generated": 0.0},
        )
        with patch.object(
            cli, "analyze_repository", return_value=(stats, {}, 1000.0)
        ), patch.object(
            cli, "count_person_months", side_effect=[10, 50]
        ):
            ref = cli.build_reference(
                Path("/tmp/deploy/x"), args, cli.weights_from_args(args), False
            )

        self.assertEqual(ref["person_months"]["strict"], 10)
        self.assertEqual(ref["person_months"]["loose"], 50)
        self.assertEqual(ref["velocity"]["high"], 100.0)
        self.assertEqual(ref["velocity"]["low"], 20.0)

    def test_reference_person_months_override(self):
        args = cli.build_parser().parse_args([
            ".", "--calibrate-against", ".",
            "--reference-person-months", "25",
        ])
        stats, _, _ = cli.build_inventory(
            load_fixture(), REPO_ROOT,
            {"Code": 1.0, "Test": 0.5, "Config": 0.25,
             "Docs": 0.0, "Data": 0.0, "Generated": 0.0},
        )
        with patch.object(
            cli, "analyze_repository", return_value=(stats, {}, 1000.0)
        ), patch.object(
            cli, "count_person_months"
        ) as counter:
            ref = cli.build_reference(
                Path("/tmp/deploy/x"), args, cli.weights_from_args(args), False
            )
        counter.assert_not_called()
        self.assertEqual(ref["person_months"]["strict"], 25)
        self.assertEqual(ref["person_months"]["loose"], 25)
        self.assertEqual(ref["velocity"]["high"], 40.0)
        self.assertEqual(ref["velocity"]["low"], 40.0)


class DebtTests(unittest.TestCase):
    def _stats(self, code_complexity=20, test_complexity=4,
               config_complexity=100):
        stats = cli._empty_classes()
        stats["Code"].update({"files": 1, "code": 100,
                              "complexity": code_complexity})
        stats["Test"].update({"files": 1, "code": 20,
                              "complexity": test_complexity})
        stats["Config"].update({"files": 1, "code": 100,
                                "complexity": config_complexity})
        return stats

    def test_debt_uses_code_and_test_only(self):
        debt = cli.calculate_debt(self._stats())
        # (20 + 4) / 100 * 100 = 24% density, penalty (24-15)*1% = 9%.
        self.assertAlmostEqual(debt["complexity_density"], 24.0)
        self.assertAlmostEqual(debt["complexity_penalty_pct"], 9.0)
        self.assertAlmostEqual(debt["test_ratio"], 20.0)
        self.assertAlmostEqual(debt["test_penalty_pct"], 0.0)
        self.assertAlmostEqual(debt["total_penalty_pct"], 9.0)

    def test_no_debt_adjustment_makes_net_equal_gross(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args([
                tmp, "--json", "--no-debt-adjustment",
            ])
            with patch.object(cli, "check_scc_installed"), patch.object(
                cli, "run_scc", return_value=load_fixture()
            ):
                with redirect_stdout(io.StringIO()):
                    payload = cli.run(args)
        repo = payload["repos"][0]
        self.assertFalse(repo["debt_applied"])
        self.assertEqual(repo["cost"]["net_low"], repo["cost"]["low"])
        self.assertEqual(repo["cost"]["net_high"], repo["cost"]["high"])



class ReportTests(unittest.TestCase):
    def test_report_has_honest_uncalibrated_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args([tmp])
            output = io.StringIO()
            with patch.object(cli, "check_scc_installed"), patch.object(
                cli, "run_scc", return_value=load_fixture()
            ):
                with redirect_stdout(output):
                    cli.run(args)
        text = output.getvalue()
        self.assertIn("replacement cost, not market value", text)
        self.assertIn("Only the current working tree is measured", text)
        self.assertIn("UNCALIBRATED", text)
        self.assertIn("Gross lo", text)


class CompatibilityAndShapeTests(unittest.TestCase):
    def _run_with_fixture(self, argv, fixture=None):
        if fixture is None:
            fixture = load_fixture()
        args = cli.build_parser().parse_args(argv)
        with patch.object(cli, "check_scc_installed"), patch.object(
            cli, "run_scc", return_value=fixture
        ):
            with redirect_stdout(io.StringIO()):
                return cli.run(args)

    def test_old_effort_and_cost_formulas(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._run_with_fixture([
                tmp, "--json", "--model", "blend",
                "--include-docs", "1", "--include-data", "1",
                "--include-generated", "1",
            ])
            repo = payload["repos"][0]

            stats, _, effective = cli.build_inventory(
                load_fixture(),
                Path(tmp),
                {
                    "Code": 1.0, "Test": 0.5, "Config": 0.25,
                    "Docs": 1.0, "Data": 1.0, "Generated": 1.0,
                },
                legacy=True,
            )
            ksloc = effective / 1000.0
            old_cocomo = 2.94 * (ksloc ** 1.05) * 0.75
            old_velocity = effective / 500.0
            old_blend = (old_cocomo + old_velocity) / 2.0
            old_gross = old_blend * 15000.0

            self.assertTrue(payload["parameters"]["legacy_compatibility"])
            self.assertEqual(repo["effective_sloc"], effective)
            self.assertAlmostEqual(repo["person_months"]["low"], old_blend)
            self.assertAlmostEqual(repo["person_months"]["high"], old_blend)
            self.assertAlmostEqual(repo["cost"]["low"], old_gross)
            self.assertAlmostEqual(repo["cost"]["high"], old_gross)

            # The legacy debt formula is also reproduced exactly.
            debt = cli.calculate_debt(stats, legacy=True)
            expected_net = old_gross * (1.0 - debt["total_penalty_pct"] / 100.0)
            self.assertAlmostEqual(repo["cost"]["net_low"], expected_net)
            self.assertAlmostEqual(repo["cost"]["net_high"], expected_net)

    def test_multiple_repos_and_json_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "one"
            second = Path(tmp) / "two"
            first.mkdir()
            second.mkdir()
            payload = self._run_with_fixture([
                str(first), str(second), "--json",
            ])

        self.assertEqual(
            set(payload), {"parameters", "reference", "repos", "total"}
        )
        self.assertEqual(payload["reference"], {})
        self.assertEqual(len(payload["repos"]), 2)
        self.assertEqual(payload["total"]["effective_sloc"], 2 * 8.75)
        expected_months = (2 * 8.75) / 500.0
        self.assertAlmostEqual(
            payload["total"]["person_months"]["low"], expected_months
        )
        self.assertAlmostEqual(
            payload["total"]["person_months"]["high"], expected_months
        )
        self.assertAlmostEqual(
            payload["total"]["cost"]["low"], expected_months * 15000.0
        )
        self.assertLessEqual(
            payload["total"]["person_months"]["low"],
            payload["total"]["person_months"]["high"],
        )


if __name__ == "__main__":
    unittest.main()
