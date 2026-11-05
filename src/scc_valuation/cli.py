#!/usr/bin/env python3
"""
scc_valuation.py / cli.py
-------------------------
Replacement-cost estimator for codebases.

`scc` is the only external binary.  The tool classifies files, measures
effective source lines, and (optionally) calibrates an effort model against a
reference repository's git history.  It reports a range, because a single
number would imply precision that this kind of estimate does not have.
"""

import argparse
import fnmatch
import json
import os
import posixpath
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

try:  # pragma: no cover - exercised by the installed console script
    from . import __version__
except ImportError:  # pragma: no cover
    __version__ = "0.0.0"


CLASSES = ("Code", "Test", "Config", "Docs", "Data", "Generated")

DEFAULT_EXCLUDE_DIRS = [
    "node_modules", "vendor", "Pods", ".venv", "venv", "env",
    "dist", "build", "target", "out", "bin", "obj", ".next", ".nuxt",
    ".git", ".svn", ".hg", "coverage", ".nyc_output", ".turbo",
    "__pycache__", ".pytest_cache", ".terraform", "tmp", "logs"
]

DEFAULT_EXCLUDE_FILES = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "Pipfile.lock", "composer.lock", "Gemfile.lock",
    "go.sum", "*.min.js", "*.min.css", "*.map", "*.wasm", "*.pb.go",
    "*_pb2.py", "*.g.dart"
]

# Generated paths are matched before every other rule.
GENERATED_PATTERNS = [
    r"(^|/)gen/schemas/",
    r"(^|/)drizzle/meta/",
    r"(^|/)migrations/meta/",
    r"\.log$",
    r"\.snap$",
    r"(^|/)__snapshots__/",
]

DOCS_LANGUAGES = {
    "markdown", "restructuredtext", "org", "plain text", "license",
}

DATA_LANGUAGES = {
    "json", "jsonl", "csv", "tsv", "svg", "xml",
}

# JSON-family files that are really project configuration, not data.
CONFIG_BASENAME_GLOBS = [
    "package.json",
    "tsconfig*.json",
    "*config*.json",
    "*.conf.json",
    "devcontainer.json",
    ".devcontainer.json",
]

# Unchanged legacy path rules, now applied to the repo-relative path.
TEST_PATTERNS = [
    r"(^|/)(tests?|__tests__|spec|specs|fixtures|e2e|cypress)/",
    r"(_|[-.])(test|spec)\.[a-zA-Z0-9]+$",
    r"^test_.*\.py$",
]

CONFIG_PATTERNS = [
    r"(^|/)(\.github|\.gitlab|terraform|helm|k8s|kubernetes|deploy|docker|ansible)/",
    r"(^|/)(Dockerfile.*|docker-compose.*|Makefile|Vagrantfile|Taskfile.*)",
    r"\.(tf|tfvars|helm|yaml|yml|toml|ini|env\.example)$",
]

DEFAULT_BOT_PATTERNS = [
    "[bot]", "github-actions", "copilot", "claude", "dependabot", "renovate",
]

DEFAULT_VELOCITY = 500.0
DEFAULT_MONTHLY_RATE = 15000.0
DEFAULT_COCOMO_EAF = 0.75
DEFAULT_MIN_COMMITS_PER_MONTH = 10
SUBPROCESS_TIMEOUT = 600


class ValuationError(Exception):
    """A user-facing error."""


# ---------------------------------------------------------------------------
# scc interaction and classification
# ---------------------------------------------------------------------------

def check_scc_installed():
    if not shutil.which("scc"):
        raise ValuationError(
            "'scc' is not installed or not in PATH.\n"
            "Install it via:\n"
            "  - macOS: brew install scc\n"
            "  - Go:    go install github.com/boyter/scc/v4@latest\n"
            "  - Linux: sudo snap install scc  (or download a release from GitHub)"
        )


def normalize_rel_path(location, repo_root):
    """Return a repo-relative path with forward slashes.

    scc is invoked with cwd=<repo> and the argument ".", so Location is
    normally already relative.  This also copes with absolute Locations from
    fixtures or future scc versions.
    """
    raw = str(location or "").replace("\\", "/")
    if os.path.isabs(raw):
        raw = os.path.relpath(raw, str(repo_root)).replace("\\", "/")
    raw = posixpath.normpath(raw)
    while raw.startswith("./"):
        raw = raw[2:]
    if raw == ".":
        raw = ""
    return raw.lstrip("/")


def _language(language):
    return (language or "").strip().casefold()


def classify_file(rel_path, language, legacy=False):
    """Classify one file.  First matching rule wins.

    legacy=True reproduces the pre-calibration classifier (Core/Test/Config
    only) for the documented compatibility flags.
    """
    norm = str(rel_path or "").replace("\\", "/").lstrip("/")

    if not legacy:
        for pattern in GENERATED_PATTERNS:
            if re.search(pattern, norm, re.IGNORECASE):
                return "Generated"

        lang = _language(language)
        if lang in DOCS_LANGUAGES:
            return "Docs"

        if lang in DATA_LANGUAGES:
            base = posixpath.basename(norm).casefold()
            for glob in CONFIG_BASENAME_GLOBS:
                if fnmatch.fnmatchcase(base, glob.casefold()):
                    return "Config"
            return "Data"

    for pattern in TEST_PATTERNS:
        if re.search(pattern, norm, re.IGNORECASE):
            return "Test"
    for pattern in CONFIG_PATTERNS:
        if re.search(pattern, norm, re.IGNORECASE):
            return "Config"
    return "Code"


def classify_path(rel_path):
    """Backward-compatible legacy path-only classifier (Core/Test/Config)."""
    category = classify_file(rel_path, "", legacy=True)
    return "Core" if category == "Code" else category


def run_scc(repo_path):
    """Run scc from inside the repo so exclusions are repo-relative."""
    cmd = [
        "scc", ".",
        "-f", "json",
        "--by-file",
        "--exclude-dir", ",".join(DEFAULT_EXCLUDE_DIRS),
        "--exclude-file", ",".join(DEFAULT_EXCLUDE_FILES),
        "--no-cocomo",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        raise ValuationError("'scc' was not found while trying to run it.")
    except subprocess.TimeoutExpired:
        raise ValuationError("scc timed out after %d seconds." % SUBPROCESS_TIMEOUT)
    except subprocess.CalledProcessError as exc:
        raise ValuationError("scc execution failed:\n%s" % (exc.stderr or exc))

    text = (result.stdout or "").lstrip("\ufeff").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValuationError("Failed to parse scc JSON output: %s" % exc)


def _empty_classes():
    return {
        name: {
            "files": 0, "lines": 0, "code": 0,
            "comments": 0, "blanks": 0, "complexity": 0,
        }
        for name in CLASSES
    }


def build_inventory(scc_data, repo_path, weights, legacy=False):
    """Turn scc --by-file JSON into per-class statistics."""
    stats = _empty_classes()
    lang_totals = {}

    for lang_entry in scc_data or []:
        lang_name = lang_entry.get("Name", "Unknown")
        for file_info in lang_entry.get("Files", []) or []:
            rel_path = normalize_rel_path(file_info.get("Location", ""), repo_path)
            category = classify_file(rel_path, lang_name, legacy=legacy)

            code = int(file_info.get("Code", 0) or 0)
            lines = int(file_info.get("Lines", 0) or 0)
            comments = int(file_info.get("Comment", 0) or 0)
            blanks = int(file_info.get("Blank", 0) or 0)
            complexity = int(file_info.get("Complexity", 0) or 0)

            bucket = stats[category]
            bucket["files"] += 1
            bucket["lines"] += lines
            bucket["code"] += code
            bucket["comments"] += comments
            bucket["blanks"] += blanks
            bucket["complexity"] += complexity

            lang = lang_totals.setdefault(
                lang_name, {"files": 0, "code": 0, "complexity": 0}
            )
            lang["files"] += 1
            lang["code"] += code
            lang["complexity"] += complexity

    effective_sloc = sum(
        stats[name]["code"] * float(weights.get(name, 0.0))
        for name in CLASSES
    )
    return stats, lang_totals, effective_sloc


def analyze_repository(repo_path, weights, legacy=False):
    return build_inventory(run_scc(repo_path), repo_path, weights, legacy=legacy)


# ---------------------------------------------------------------------------
# Calibration from git history
# ---------------------------------------------------------------------------

def count_person_months(repo_path, min_commits=1, bot_patterns=None):
    """Count (lowercased email, YYYY-MM) pairs with at least min_commits.

    Author dates are formatted by git in each commit's recorded timezone.
    """
    if min_commits < 1:
        raise ValuationError("min_commits must be at least 1.")
    if not shutil.which("git"):
        raise ValuationError(
            "git is required for --calibrate-against unless "
            "--reference-person-months is supplied."
        )

    patterns = DEFAULT_BOT_PATTERNS if bot_patterns is None else list(bot_patterns)
    patterns = [p.casefold() for p in patterns if p]

    cmd = [
        "git", "-C", str(repo_path), "log", "--no-merges",
        "--format=%an%x1f%ae%x1f%ad", "--date=format:%Y-%m",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        raise ValuationError("git was not found while trying to read history.")
    except subprocess.TimeoutExpired:
        raise ValuationError("git log timed out after %d seconds." % SUBPROCESS_TIMEOUT)

    if result.returncode != 0:
        raise ValuationError(
            "git log failed for %s:\n%s" % (repo_path, result.stderr or result.stdout)
        )

    counts = Counter()
    for line in (result.stdout or "").splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        name, email, month = parts[0], parts[1].strip(), parts[2].strip()
        identity = ("%s %s" % (name, email)).casefold()
        if any(pattern in identity for pattern in patterns):
            continue
        counts[(email.casefold(), month)] += 1

    return sum(1 for n in counts.values() if n >= min_commits)


# ---------------------------------------------------------------------------
# Effort, cost, and debt
# ---------------------------------------------------------------------------

def cocomo_pm(effective_sloc, eaf=DEFAULT_COCOMO_EAF):
    if effective_sloc <= 0:
        return 0.0
    ksloc = effective_sloc / 1000.0
    return 2.94 * (ksloc ** 1.05) * eaf


def months_range(effective_sloc, model, velocity_low, velocity_high,
                 cocomo_eaf=DEFAULT_COCOMO_EAF):
    """Return (low, high) engineer-months.

    velocity_high is the high-productivity measurement, so it produces the
    low end of the effort range.
    """
    cocomo = cocomo_pm(effective_sloc, cocomo_eaf)
    if model == "cocomo":
        return cocomo, cocomo
    if effective_sloc <= 0:
        return 0.0, 0.0
    if velocity_low <= 0 or velocity_high <= 0:
        raise ValuationError("Velocity must be greater than zero.")

    low = effective_sloc / velocity_high
    high = effective_sloc / velocity_low
    if low > high:
        low, high = high, low

    if model == "blend":
        return (cocomo + low) / 2.0, (cocomo + high) / 2.0
    return low, high


def calculate_debt(stats, legacy=False):
    """Technical-debt deduction from Code and Test statistics.

    In legacy compatibility mode the old all-class complexity formula is used
    so that the documented flags restore the old report exactly.
    """
    if legacy:
        core_code = stats["Code"]["code"]
        density_complexity = sum(stats[name]["complexity"] for name in CLASSES)
        density_denominator = core_code
        density_formula = (
            "all-class complexity / (Code+Docs+Data+Generated SLOC) * 100 "
            "[legacy compatibility]"
        )
    else:
        core_code = stats["Code"]["code"]
        density_complexity = stats["Code"]["complexity"] + stats["Test"]["complexity"]
        density_denominator = core_code
        density_formula = (
            "(Code complexity + Test complexity) / Code SLOC * 100"
        )

    test_code = stats["Test"]["code"]

    complexity_density = (
        density_complexity / density_denominator * 100.0
        if density_denominator > 0 else 0.0
    )
    complexity_penalty = min(0.25, max(0.0, (complexity_density - 15.0) * 0.01))

    test_ratio = (test_code / core_code) if core_code > 0 else 0.0
    test_penalty = min(0.15, max(0.0, (0.20 - test_ratio) * 0.75))

    total_penalty = complexity_penalty + test_penalty
    return {
        "complexity_density": complexity_density,
        "complexity_penalty_pct": complexity_penalty * 100.0,
        "test_ratio": test_ratio * 100.0,
        "test_penalty_pct": test_penalty * 100.0,
        "total_penalty_pct": total_penalty * 100.0,
        "complexity_formula": density_formula,
        "complexity_penalty_formula": "min(0.25, max(0, (density - 15) * 0.01))",
        "test_ratio_formula": "Test SLOC / Code SLOC",
        "test_penalty_formula": "min(0.15, max(0, (0.20 - ratio) * 0.75))",
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="scc-valuation",
        description=(
            "Estimate replacement cost from source lines and (optionally) "
            "calibrate against a reference repository's git history."
        ),
    )
    parser.add_argument(
        "repo_paths", nargs="*", metavar="REPO",
        help="Target repositories (default: current directory)",
    )
    parser.add_argument("--version", action="version", version=__version__)

    parser.add_argument(
        "--monthly-rate", type=float, default=DEFAULT_MONTHLY_RATE,
        help="Fully burdened cost per engineer-month (default: 15000)",
    )
    parser.add_argument(
        "--velocity", type=float, default=None,
        help=(
            "Effective SLOC per engineer-month. Default when no calibration "
            "is supplied: 500 (UNCALIBRATED)."
        ),
    )
    parser.add_argument(
        "--model", choices=("velocity", "cocomo", "blend"), default="velocity",
        help="Effort model (default: velocity)",
    )
    parser.add_argument(
        "--cocomo-eaf", type=float, default=DEFAULT_COCOMO_EAF,
        help="COCOMO effort adjustment factor (default: 0.75)",
    )
    parser.add_argument(
        "--test-weight", type=float, default=0.5,
        help="Effective-SLOC weight for Test files (default: 0.5)",
    )
    parser.add_argument(
        "--config-weight", type=float, default=0.25,
        help="Effective-SLOC weight for Config files (default: 0.25)",
    )
    parser.add_argument(
        "--include-docs", type=float, default=0.0, metavar="W",
        help="Effective-SLOC weight for Docs files (default: 0)",
    )
    parser.add_argument(
        "--include-data", type=float, default=0.0, metavar="W",
        help="Effective-SLOC weight for Data files (default: 0)",
    )
    parser.add_argument(
        "--include-generated", type=float, default=0.0, metavar="W",
        help="Effective-SLOC weight for Generated files (default: 0)",
    )

    parser.add_argument(
        "--calibrate-against", metavar="PATH",
        help="Reference repository used to measure velocity from git history",
    )
    parser.add_argument(
        "--min-commits-per-month", type=int,
        default=DEFAULT_MIN_COMMITS_PER_MONTH, metavar="N",
        help=(
            "Strict person-month threshold (default: 10). "
            "The loose threshold is always 1."
        ),
    )
    parser.add_argument(
        "--reference-person-months", type=float, default=None, metavar="X",
        help="Override both reference person-month measurements with X",
    )
    parser.add_argument(
        "--bot-pattern", action="append", default=None, metavar="SUBSTRING",
        help=(
            "Repeatable bot substring (case-insensitive). Replaces the "
            "built-in bot list when supplied."
        ),
    )

    parser.add_argument(
        "--no-debt-adjustment", action="store_true",
        help="Do not subtract technical-debt penalties from replacement cost",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of the text report",
    )
    return parser


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def legacy_compatibility(args):
    """The documented flag combination that restores the old report."""
    return (
        args.model == "blend"
        and args.include_docs == 1.0
        and args.include_data == 1.0
        and args.include_generated == 1.0
        and args.test_weight == 0.5
        and args.config_weight == 0.25
        and args.calibrate_against is None
        and (args.velocity is None or args.velocity == DEFAULT_VELOCITY)
    )


def weights_from_args(args):
    return {
        "Code": 1.0,
        "Test": args.test_weight,
        "Config": args.config_weight,
        "Docs": args.include_docs,
        "Data": args.include_data,
        "Generated": args.include_generated,
    }


def _uncalibrated_estimates(effective_sloc, cocomo_eaf, velocity=DEFAULT_VELOCITY):
    cocomo = cocomo_pm(effective_sloc, cocomo_eaf)
    vel = effective_sloc / velocity if effective_sloc > 0 and velocity > 0 else 0.0
    return {
        "velocity_500": vel,
        "cocomo": cocomo,
        "blend": (cocomo + vel) / 2.0,
    }


def build_reference(reference_path, args, weights, legacy):
    stats, _langs, effective_sloc = analyze_repository(
        reference_path, weights, legacy=legacy
    )

    if args.reference_person_months is not None:
        if args.reference_person_months <= 0:
            raise ValuationError("--reference-person-months must be greater than zero.")
        strict_pm = loose_pm = args.reference_person_months
        measurement = "override"
    else:
        strict_pm = count_person_months(
            reference_path,
            min_commits=args.min_commits_per_month,
            bot_patterns=args.bot_pattern,
        )
        loose_pm = count_person_months(
            reference_path,
            min_commits=1,
            bot_patterns=args.bot_pattern,
        )
        measurement = "git"

    if effective_sloc <= 0:
        raise ValuationError(
            "Reference repository has no effective SLOC; cannot calibrate."
        )
    if strict_pm <= 0 or loose_pm <= 0:
        raise ValuationError(
            "Reference repository has no qualifying person-months; "
            "cannot calibrate. Check the git history or use "
            "--reference-person-months."
        )

    velocity_high = effective_sloc / strict_pm
    velocity_low = effective_sloc / loose_pm
    if velocity_high < velocity_low:
        velocity_high, velocity_low = velocity_low, velocity_high

    return {
        "path": str(reference_path),
        "name": reference_path.name,
        "effective_sloc": effective_sloc,
        "person_months": {
            "strict": strict_pm,
            "loose": loose_pm,
            "strict_threshold": args.min_commits_per_month,
            "loose_threshold": 1,
            "measurement": measurement,
        },
        "velocity": {
            "high": velocity_high,
            "low": velocity_low,
        },
        "uncalibrated": _uncalibrated_estimates(
            effective_sloc, args.cocomo_eaf
        ),
        "classes": stats,
    }


def _sum_class_stats(all_stats):
    total = _empty_classes()
    for stats in all_stats:
        for name in CLASSES:
            for field in total[name]:
                total[name][field] += stats[name][field]
    return total


def run(args):
    if args.monthly_rate < 0:
        raise ValuationError("--monthly-rate cannot be negative.")
    if args.cocomo_eaf <= 0:
        raise ValuationError("--cocomo-eaf must be greater than zero.")
    for label, value in (
        ("--test-weight", args.test_weight),
        ("--config-weight", args.config_weight),
        ("--include-docs", args.include_docs),
        ("--include-data", args.include_data),
        ("--include-generated", args.include_generated),
    ):
        if value < 0:
            raise ValuationError("%s cannot be negative." % label)
    if args.min_commits_per_month < 1:
        raise ValuationError("--min-commits-per-month must be at least 1.")
    if args.reference_person_months is not None and not args.calibrate_against:
        raise ValuationError(
            "--reference-person-months requires --calibrate-against."
        )
    repo_paths = [Path(p).expanduser().resolve() for p in (args.repo_paths or ["."])]
    for repo in repo_paths:
        if not repo.exists() or not repo.is_dir():
            raise ValuationError(
                "Target path does not exist or is not a directory: %s" % repo
            )

    check_scc_installed()

    weights = weights_from_args(args)
    legacy = legacy_compatibility(args)

    reference = {}
    if args.calibrate_against:
        reference_path = Path(args.calibrate_against).expanduser().resolve()
        if not reference_path.exists() or not reference_path.is_dir():
            raise ValuationError(
                "Reference path does not exist or is not a directory: %s"
                % reference_path
            )
        reference = build_reference(reference_path, args, weights, legacy)
        velocity_high = reference["velocity"]["high"]
        velocity_low = reference["velocity"]["low"]
        velocity_source = "calibrated"
    elif args.velocity is not None:
        if args.velocity <= 0:
            raise ValuationError("--velocity must be greater than zero.")
        velocity_high = velocity_low = args.velocity
        velocity_source = "explicit"
    else:
        velocity_high = velocity_low = DEFAULT_VELOCITY
        velocity_source = "default-uncalibrated"

    repo_results = []
    for repo in repo_paths:
        stats, lang_totals, effective_sloc = analyze_repository(
            repo, weights, legacy=legacy
        )
        months_low, months_high = months_range(
            effective_sloc,
            args.model,
            velocity_low,
            velocity_high,
            args.cocomo_eaf,
        )
        gross_low = months_low * args.monthly_rate
        gross_high = months_high * args.monthly_rate

        debt = calculate_debt(stats, legacy=legacy)
        if args.no_debt_adjustment:
            applied_penalty = 0.0
        else:
            applied_penalty = debt["total_penalty_pct"] / 100.0

        net_low = gross_low * (1.0 - applied_penalty)
        net_high = gross_high * (1.0 - applied_penalty)

        repo_results.append({
            "name": repo.name,
            "path": str(repo),
            "classes": stats,
            "languages": lang_totals,
            "effective_sloc": effective_sloc,
            "person_months": {"low": months_low, "high": months_high},
            "cost": {
                "low": gross_low,
                "high": gross_high,
                "net_low": net_low,
                "net_high": net_high,
            },
            "debt": debt,
            "debt_applied": not args.no_debt_adjustment,
        })

    total_classes = _sum_class_stats([r["classes"] for r in repo_results])
    total_effective = sum(r["effective_sloc"] for r in repo_results)
    total_months_low = sum(r["person_months"]["low"] for r in repo_results)
    total_months_high = sum(r["person_months"]["high"] for r in repo_results)
    total_gross_low = sum(r["cost"]["low"] for r in repo_results)
    total_gross_high = sum(r["cost"]["high"] for r in repo_results)
    total_net_low = sum(r["cost"]["net_low"] for r in repo_results)
    total_net_high = sum(r["cost"]["net_high"] for r in repo_results)

    total = {
        "classes": total_classes,
        "effective_sloc": total_effective,
        "person_months": {"low": total_months_low, "high": total_months_high},
        "cost": {
            "low": total_gross_low,
            "high": total_gross_high,
            "net_low": total_net_low,
            "net_high": total_net_high,
        },
    }

    parameters = {
        "model": args.model,
        "cocomo_eaf": args.cocomo_eaf,
        "monthly_rate": args.monthly_rate,
        "velocity": {
            "high": velocity_high,
            "low": velocity_low,
            "source": velocity_source,
        },
        "weights": weights,
        "debt_adjustment": not args.no_debt_adjustment,
        "legacy_compatibility": legacy,
        "min_commits_per_month": args.min_commits_per_month,
        "reference_person_months": args.reference_person_months,
        "bot_patterns": (
            list(args.bot_pattern)
            if args.bot_pattern is not None else list(DEFAULT_BOT_PATTERNS)
        ),
        "calibrated_against": (
            str(Path(args.calibrate_against).expanduser().resolve())
            if args.calibrate_against else None
        ),
        "velocity_override_ignored": (
            args.velocity
            if args.calibrate_against and args.velocity is not None else None
        ),
    }

    payload = {
        "parameters": parameters,
        "reference": reference,
        "repos": repo_results,
        "total": total,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print_report(payload)
    return payload


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def _fmt_num(value, digits=2):
    if abs(value - round(value)) < 1e-9:
        return "{:,}".format(int(round(value)))
    return "{:,.{digits}f}".format(value, digits=digits)


def _fmt_months(value):
    return "{:,.2f}".format(value)


def _fmt_money(value):
    return "${:,.0f}".format(value)


def _display_names(repos):
    names = [r["name"] for r in repos]
    counts = Counter(names)
    result = []
    for repo in repos:
        name = repo["name"]
        if counts[name] > 1:
            name = "%s (%s)" % (name, repo["path"])
        result.append(name)
    return result


def print_report(payload):
    params = payload["parameters"]
    reference = payload["reference"]
    repos = payload["repos"]
    total = payload["total"]

    print("\n" + "=" * 118)
    print("SCC VALUATION - REPLACEMENT COST ESTIMATE")
    print("=" * 118)
    print("This is replacement cost, not market value.")
    print("Only the current working tree is measured; target-repo git history is not used.")
    if params["velocity"]["source"] == "default-uncalibrated":
        print(
            "STATUS: UNCALIBRATED - no --calibrate-against and no explicit "
            "--velocity. The default 500 SLOC/engineer-month is a legacy "
            "placeholder."
        )
    elif params["velocity"]["source"] == "calibrated":
        print("STATUS: CALIBRATED against %s" % params["calibrated_against"])
    else:
        print("STATUS: EXPLICIT VELOCITY")

    print("\nModel: %s" % params["model"])
    print(
        "  velocity high/low: %s / %s effective SLOC per engineer-month (%s)"
        % (
            _fmt_num(params["velocity"]["high"]),
            _fmt_num(params["velocity"]["low"]),
            params["velocity"]["source"],
        )
    )
    if params.get("velocity_override_ignored") is not None:
        print(
            "  Note: --velocity %s was ignored because --calibrate-against "
            "supplied the velocity." % params["velocity_override_ignored"]
        )
    print("  COCOMO EAF: %s" % params["cocomo_eaf"])
    if params["model"] == "velocity":
        print("  Effort = effective SLOC / velocity")
    elif params["model"] == "cocomo":
        print("  Effort = 2.94 * KSLOC^1.05 * COCOMO EAF")
    else:
        print("  Effort = (COCOMO effort + effective SLOC / velocity) / 2")
    print("  Monthly rate: %s" % _fmt_money(params["monthly_rate"]))
    print(
        "  Weights: Code %s | Test %s | Config %s | Docs %s | Data %s | Generated %s"
        % (
            params["weights"]["Code"],
            params["weights"]["Test"],
            params["weights"]["Config"],
            params["weights"]["Docs"],
            params["weights"]["Data"],
            params["weights"]["Generated"],
        )
    )
    print(
        "  Debt adjustment: %s"
        % ("enabled" if params["debt_adjustment"] else "disabled")
    )
    if params["legacy_compatibility"]:
        print(
            "  Legacy compatibility mode: Docs/Data/Generated are counted as Code "
            "and the old debt formula is used."
        )

    if reference:
        print("\nReference calibration:")
        print("  Path: %s" % reference["path"])
        print("  Effective SLOC: %s" % _fmt_num(reference["effective_sloc"]))
        pm = reference["person_months"]
        if pm["measurement"] == "override":
            print(
                "  Person-months: %s (override applied to both thresholds)"
                % _fmt_num(pm["strict"])
            )
        else:
            print(
                "  Person-months: %s at N=%d (strict) | %s at N=1 (loose)"
                % (
                    _fmt_num(pm["strict"]),
                    pm["strict_threshold"],
                    _fmt_num(pm["loose"]),
                )
            )
        print(
            "  Measured velocity: %s high | %s low"
            % (
                _fmt_num(reference["velocity"]["high"]),
                _fmt_num(reference["velocity"]["low"]),
            )
        )
        unc = reference["uncalibrated"]
        print("  Uncalibrated estimates for the reference:")
        print("    velocity @ 500: %s engineer-months" % _fmt_months(unc["velocity_500"]))
        print("    COCOMO (EAF %s): %s engineer-months"
              % (params["cocomo_eaf"], _fmt_months(unc["cocomo"])))
        print("    blend: %s engineer-months" % _fmt_months(unc["blend"]))
        if unc["velocity_500"] > 0 and pm["strict"] > 0 and pm["loose"] > 0:
            print(
                "    velocity@500 is %.1fx-%.1fx the measured effort"
                % (
                    unc["velocity_500"] / pm["loose"],
                    unc["velocity_500"] / pm["strict"],
                )
            )

    print("\n### Inventory and replacement cost")
    header = (
        "%-30s %10s %10s %10s %10s %10s %10s %12s %10s %10s %13s %13s"
        % (
            "Repository", "Code", "Test", "Config", "Docs", "Data",
            "Generated", "Effective", "Months lo", "Months hi",
            "Gross lo", "Gross hi",
        )
    )
    print(header)
    print("-" * len(header))

    display_names = _display_names(repos)
    for name, repo in zip(display_names, repos):
        c = repo["classes"]
        print(
            "%-30s %10s %10s %10s %10s %10s %10s %12s %10s %10s %13s %13s"
            % (
                name[:30],
                _fmt_num(c["Code"]["code"]),
                _fmt_num(c["Test"]["code"]),
                _fmt_num(c["Config"]["code"]),
                _fmt_num(c["Docs"]["code"]),
                _fmt_num(c["Data"]["code"]),
                _fmt_num(c["Generated"]["code"]),
                _fmt_num(repo["effective_sloc"]),
                _fmt_months(repo["person_months"]["low"]),
                _fmt_months(repo["person_months"]["high"]),
                _fmt_money(repo["cost"]["low"]),
                _fmt_money(repo["cost"]["high"]),
            )
        )
    tc = total["classes"]
    print("-" * len(header))
    print(
        "%-30s %10s %10s %10s %10s %10s %10s %12s %10s %10s %13s %13s"
        % (
            "TOTAL",
            _fmt_num(tc["Code"]["code"]),
            _fmt_num(tc["Test"]["code"]),
            _fmt_num(tc["Config"]["code"]),
            _fmt_num(tc["Docs"]["code"]),
            _fmt_num(tc["Data"]["code"]),
            _fmt_num(tc["Generated"]["code"]),
            _fmt_num(total["effective_sloc"]),
            _fmt_months(total["person_months"]["low"]),
            _fmt_months(total["person_months"]["high"]),
            _fmt_money(total["cost"]["low"]),
            _fmt_money(total["cost"]["high"]),
        )
    )
    print(
        "Effective SLOC = Code + %s*Test + %s*Config + %s*Docs + %s*Data + %s*Generated"
        % (
            params["weights"]["Test"], params["weights"]["Config"],
            params["weights"]["Docs"], params["weights"]["Data"],
            params["weights"]["Generated"],
        )
    )

    print("\n### Technical debt adjustments")
    print("  Complexity density = %s" % (
        "all-class complexity / (Code+Docs+Data+Generated SLOC) * 100 [legacy compatibility]"
        if params["legacy_compatibility"]
        else "(Code complexity + Test complexity) / Code SLOC * 100"
    ))
    print("  Complexity penalty = min(0.25, max(0, (density - 15) * 0.01))")
    print("  Test ratio = Test SLOC / Code SLOC")
    print("  Test penalty = min(0.15, max(0, (0.20 - ratio) * 0.75))")
    if not params["debt_adjustment"]:
        print("  --no-debt-adjustment: net cost equals gross cost.")
    else:
        debt_header = (
            "%-30s %12s %14s %12s %12s %10s %13s %13s"
            % (
                "Repository", "Density", "Complexity -", "Test ratio",
                "Test -", "Total -", "Net lo", "Net hi",
            )
        )
        print(debt_header)
        print("-" * len(debt_header))
        for name, repo in zip(display_names, repos):
            d = repo["debt"]
            print(
                "%-30s %11.2f%% %13.2f%% %11.2f%% %11.2f%% %9.2f%% %13s %13s"
                % (
                    name[:30],
                    d["complexity_density"],
                    d["complexity_penalty_pct"],
                    d["test_ratio"],
                    d["test_penalty_pct"],
                    d["total_penalty_pct"],
                    _fmt_money(repo["cost"]["net_low"]),
                    _fmt_money(repo["cost"]["net_high"]),
                )
            )
        print("-" * len(debt_header))
        print(
            "%-30s %12s %14s %12s %12s %10s %13s %13s"
            % (
                "TOTAL", "-", "-", "-", "-", "-",
                _fmt_money(total["cost"]["net_low"]),
                _fmt_money(total["cost"]["net_high"]),
            )
        )
    print()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run(args)
    except ValuationError as exc:
        parser.exit(2, "Error: %s\n" % exc)
    return 0


if __name__ == "__main__":
    main()
