#!/usr/bin/env python3
"""
scc_valuation.py / cli.py
-------------------------
Automated replacement cost and engineering effort evaluator for codebases.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

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

def check_scc_installed():
    if not shutil.which("scc"):
        sys.exit(
            "Error: 'scc' is not installed or not in PATH.\n"
            "Install it via:\n"
            "  - macOS: brew install scc\n"
            "  - Go:    go install github.com/boyter/scc/v4@latest\n"
            "  - Linux: sudo snap install scc  (or download release from GitHub)"
        )

def run_scc(repo_path: Path):
    cmd = [
        "scc",
        str(repo_path),
        "-f", "json",
        "--by-file",
        "--exclude-dir", ",".join(DEFAULT_EXCLUDE_DIRS),
        "--exclude-file", ",".join(DEFAULT_EXCLUDE_FILES),
        "--no-cocomo",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        sys.exit(f"scc execution failed: {e.stderr}")
    except json.JSONDecodeError as e:
        sys.exit(f"Failed to parse scc JSON output: {e}")

def classify_path(rel_path: str) -> str:
    norm = rel_path.replace("\\", "/")
    for pattern in TEST_PATTERNS:
        if re.search(pattern, norm, re.IGNORECASE):
            return "Test"
    for pattern in CONFIG_PATTERNS:
        if re.search(pattern, norm, re.IGNORECASE):
            return "Config"
    return "Core"

def analyze_repository(repo_path: Path, test_weight: float, config_weight: float):
    scc_data = run_scc(repo_path)

    stats = {
        "Core": {"files": 0, "lines": 0, "code": 0, "comments": 0, "blanks": 0, "complexity": 0},
        "Test": {"files": 0, "lines": 0, "code": 0, "comments": 0, "blanks": 0, "complexity": 0},
        "Config": {"files": 0, "lines": 0, "code": 0, "comments": 0, "blanks": 0, "complexity": 0},
    }
    lang_totals = {}

    for lang_entry in scc_data:
        lang_name = lang_entry.get("Name", "Unknown")
        files = lang_entry.get("Files", [])

        for file_info in files:
            loc_path = file_info.get("Location", "")
            category = classify_path(loc_path)

            code = file_info.get("Code", 0)
            lines = file_info.get("Lines", 0)
            comment = file_info.get("Comment", 0)
            blank = file_info.get("Blank", 0)
            complexity = file_info.get("Complexity", 0)

            stats[category]["files"] += 1
            stats[category]["lines"] += lines
            stats[category]["code"] += code
            stats[category]["comments"] += comment
            stats[category]["blanks"] += blank
            stats[category]["complexity"] += complexity

            if lang_name not in lang_totals:
                lang_totals[lang_name] = {"files": 0, "code": 0, "complexity": 0}
            lang_totals[lang_name]["files"] += 1
            lang_totals[lang_name]["code"] += code
            lang_totals[lang_name]["complexity"] += complexity

    effective_sloc = (
        stats["Core"]["code"]
        + (stats["Test"]["code"] * test_weight)
        + (stats["Config"]["code"] * config_weight)
    )

    return stats, lang_totals, effective_sloc

def calculate_valuation(
    effective_sloc: float,
    core_code: int,
    test_code: int,
    total_complexity: int,
    monthly_rate: float,
    velocity_rate: float,
):
    ksloc = max(effective_sloc / 1000.0, 0.001)
    cocomo_pm = 2.94 * (ksloc ** 1.05) * 0.75
    cocomo_schedule_mo = 3.67 * (cocomo_pm ** 0.317)

    velocity_pm = effective_sloc / velocity_rate
    blended_pm = (cocomo_pm + velocity_pm) / 2.0
    gross_cost = blended_pm * monthly_rate

    cd = (total_complexity / core_code * 100) if core_code > 0 else 0
    complexity_penalty_pct = min(0.25, max(0.0, (cd - 15.0) * 0.01))

    test_ratio = (test_code / core_code) if core_code > 0 else 0
    test_penalty_pct = min(0.15, max(0.0, (0.20 - test_ratio) * 0.75))

    total_debt_pct = complexity_penalty_pct + test_penalty_pct
    debt_deduction = gross_cost * total_debt_pct
    net_replacement_value = max(gross_cost - debt_deduction, 0.0)

    return {
        "ksloc": ksloc,
        "cocomo_pm": cocomo_pm,
        "cocomo_schedule_mo": cocomo_schedule_mo,
        "velocity_pm": velocity_pm,
        "blended_pm": blended_pm,
        "gross_cost": gross_cost,
        "complexity_density": cd,
        "complexity_penalty_pct": complexity_penalty_pct * 100,
        "test_ratio": test_ratio * 100,
        "test_penalty_pct": test_penalty_pct * 100,
        "total_debt_pct": total_debt_pct * 100,
        "debt_deduction": debt_deduction,
        "net_replacement_value": net_replacement_value,
    }

def format_report(repo_path: Path, stats, lang_totals, effective_sloc, val, args):
    total_files = sum(s["files"] for s in stats.values())
    total_code = sum(s["code"] for s in stats.values())
    total_lines = sum(s["lines"] for s in stats.values())

    print("\n" + "=" * 78)
    print(f"CODEBASE REPLACEMENT VALUE AUDIT: {repo_path.resolve().name}")
    print("=" * 78)

    print("\n### 1. Codebase Inventory Breakdown")
    print(f"{'Category':<12} | {'Files':>8} | {'Raw Lines':>12} | {'Code (SLOC)':>12} | {'Weight':>8} | {'Effective SLOC':>14}")
    print("-" * 78)
    print(f"{'Core App':<12} | {stats['Core']['files']:>8,d} | {stats['Core']['lines']:>12,d} | {stats['Core']['code']:>12,d} | {'100%':>8} | {stats['Core']['code']:>14,d}")
    print(f"{'Tests':<12} | {stats['Test']['files']:>8,d} | {stats['Test']['lines']:>12,d} | {stats['Test']['code']:>12,d} | {f'{int(args.test_weight*100)}%':>8} | {int(stats['Test']['code']*args.test_weight):>14,d}")
    print(f"{'IaC / Config':<12} | {stats['Config']['files']:>8,d} | {stats['Config']['lines']:>12,d} | {stats['Config']['code']:>12,d} | {f'{int(args.config_weight*100)}%':>8} | {int(stats['Config']['code']*args.config_weight):>14,d}")
    print("-" * 78)
    print(f"{'TOTAL':<12} | {total_files:>8,d} | {total_lines:>12,d} | {total_code:>12,d} | {'-':>8} | {int(effective_sloc):>14,d}")

    sorted_langs = sorted(lang_totals.items(), key=lambda x: x[1]["code"], reverse=True)[:5]
    print("\n### 2. Primary Languages (Top 5)")
    for lang, lstats in sorted_langs:
        pct = (lstats['code'] / total_code * 100) if total_code > 0 else 0
        print(f"  - {lang:<16}: {lstats['code']:>8,d} SLOC ({pct:>5.1f}%) | Complexity: {lstats['complexity']:,d}")

    print("\n### 3. Engineering Effort & Schedule")
    print(f"  - Modernized COCOMO II Effort : {val['cocomo_pm']:.1f} Person-Months")
    print(f"  - Estimated COCOMO Duration   : {val['cocomo_schedule_mo']:.1f} Months")
    print(f"  - Velocity Run-Rate Effort    : {val['velocity_pm']:.1f} Person-Months (@ {args.velocity} LLOC/dev-month)")
    print(f"  - Blended Baseline Effort     : {val['blended_pm']:.1f} Person-Months")

    print("\n### 4. Financial Replacement Valuation")
    print(f"  - Fully Burdened Rate Assumed : ${args.monthly_rate:,.2f} / engineer-month")
    print(f"  - Gross Replacement Cost      : ${val['gross_cost']:,.2f}")
    print("\n### 5. Technical Debt & Quality Adjustments")
    print(f"  - Complexity Density (CC/SLOC): {val['complexity_density']:.2f}% (Penalty: -{val['complexity_penalty_pct']:.1f}%)")
    print(f"  - Test Suite Ratio (Test/Core): {val['test_ratio']:.2f}% (Penalty: -{val['test_penalty_pct']:.1f}%)")
    print(f"  - Total Technical Debt Offset : -${val['debt_deduction']:,.2f} (-{val['total_debt_pct']:.1f}%)")
    print("-" * 78)
    print(f"  >>> FAIR NET REPLACEMENT VALUE: ${val['net_replacement_value']:,.2f} <<<")
    print("=" * 78 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Estimate codebase replacement cost and effort using scc.")
    parser.add_argument("repo_path", nargs="?", default=".", help="Path to target repository (default: current directory)")
    parser.add_argument("--monthly-rate", type=float, default=15000.0, help="Fully burdened monthly developer cost (default: 15000)")
    parser.add_argument("--velocity", type=float, default=500.0, help="Net logical LOC authored per developer-month (default: 500)")
    parser.add_argument("--test-weight", type=float, default=0.50, help="Effort discount multiplier for tests (default: 0.50)")
    parser.add_argument("--config-weight", type=float, default=0.25, help="Effort discount multiplier for configs/IaC (default: 0.25)")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format instead of table")

    args = parser.parse_args()
    check_scc_installed()

    repo = Path(args.repo_path)
    if not repo.exists() or not repo.is_dir():
        sys.exit(f"Target path does not exist or is not a directory: {args.repo_path}")

    stats, lang_totals, effective_sloc = analyze_repository(repo, args.test_weight, args.config_weight)

    total_complexity = sum(s["complexity"] for s in stats.values())
    val = calculate_valuation(
        effective_sloc=effective_sloc,
        core_code=stats["Core"]["code"],
        test_code=stats["Test"]["code"],
        total_complexity=total_complexity,
        monthly_rate=args.monthly_rate,
        velocity_rate=args.velocity,
    )

    if args.json:
        payload = {
            "repository": repo.resolve().name,
            "inventory": stats,
            "effective_sloc": effective_sloc,
            "valuation": val
        }
        print(json.dumps(payload, indent=2))
    else:
        format_report(repo, stats, lang_totals, effective_sloc, val, args)

if __name__ == "__main__":
    main()
