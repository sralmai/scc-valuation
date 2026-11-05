# scc-valuation

Replacement-cost estimator for codebases. `scc` measures source lines; git
history can calibrate the effort model against a reference repository.

The default output is intentionally a **range**, not a single number. The
estimate is replacement cost for the current working tree — it is not a market
valuation, and it does not inspect the target repositories' git history.

## Installation and usage

Run directly with `uvx`:

```bash
uvx --from . scc-valuation /path/to/target-repo
```

Or install it:

```bash
uv tool install --from . scc-valuation
scc-valuation /path/to/target-repo
```

`scc` must be installed separately and available in `PATH` (version 4.x is
recommended).

## What gets counted

Each file is classified once, using its path relative to the repository root:

| Class | Rule |
|---|---|
| `Generated` | `gen/schemas/`, `drizzle/meta/`, `migrations/meta/`, `*.log`, `*.snap`, `__snapshots__/` |
| `Docs` | Markdown, ReStructuredText, Org, Plain Text, License |
| `Data` | JSON, JSONL, CSV, TSV, SVG, XML (unless the basename is a config file) |
| `Config` | JSON config files such as `package.json`, `tsconfig*.json`, `*config*.json`, `*.conf.json`, `devcontainer.json`, plus the legacy IaC/config path rules |
| `Test` | the legacy test path rules, applied to the repo-relative path |
| `Code` | everything else |

`Data` files whose basename matches `package.json`, `tsconfig*.json`,
`*config*.json`, `*.conf.json`, `devcontainer.json`, or `.devcontainer.json`
are classified as `Config`.

Effective SLOC is:

```text
Code + 0.5 * Test + 0.25 * Config
```

Docs, Data, and Generated files weigh 0 by default but are always shown in the
inventory. Re-weight them with `--include-docs W`, `--include-data W`, and
`--include-generated W`.

## Calibrated estimates

Without `--calibrate-against` or an explicit `--velocity`, the run is
**UNCALIBRATED** and uses the legacy default of 500 effective SLOC per
engineer-month. That default is a placeholder and should not be trusted.

Calibrate against a representative repository:

```bash
scc-valuation --calibrate-against /path/to/reference-repo /path/to/target-repo
```

For the reference, the tool reads `git log --no-merges` author dates and counts
an `(email, YYYY-MM)` pair as a person-month when it has at least
`--min-commits-per-month` commits (default 10). It measures again with a loose
threshold of 1 commit. The strict measurement gives the high velocity and the
loose measurement gives the low velocity. Identity is the lowercased author
email; bot-like authors are excluded.

The target estimate is then a range:

```text
low  engineer-months = effective SLOC / high velocity
high engineer-months = effective SLOC / low velocity
```

### Worked example

Suppose the reference has 518,984 effective SLOC, 36 person-months at N=10, and
63 person-months at N=1:

```text
high velocity = 518,984 / 36 = 14,416.2 SLOC/engineer-month
low  velocity = 518,984 / 63 =  8,237.8 SLOC/engineer-month
```

For a target with 36,034.25 effective SLOC:

```text
low  = 36,034.25 / 14,416.2 = 2.50 engineer-months
high = 36,034.25 /  8,237.8 = 4.37 engineer-months
```

At $15,000 per engineer-month that is **$37.5k–$65.5k** for the target.

The report also prints what the uncalibrated model would have said for the
reference, so the size of the correction is visible.

To override both reference measurements (for example, when git history is
unavailable):

```bash
scc-valuation \
  --calibrate-against /path/to/reference-repo \
  --reference-person-months 48 \
  /path/to/target-repo
```

## Models and costs

`--model velocity` (default) uses the calibrated velocity range.
`--model cocomo` uses `2.94 * KSLOC^1.05 * --cocomo-eaf` (default EAF `0.75`).
`--model blend` averages COCOMO and velocity.

```text
effective SLOC -> engineer-months -> gross replacement cost
                -> technical-debt adjustment -> net replacement cost
```

Technical-debt penalties are computed from `Code` and `Test` only:

```text
complexity density = (Code complexity + Test complexity) / Code SLOC * 100
complexity penalty = min(0.25, max(0, (density - 15) * 0.01))
test ratio         = Test SLOC / Code SLOC
test penalty       = min(0.15, max(0, (0.20 - ratio) * 0.75))
```

Use `--no-debt-adjustment` to report gross cost as net cost.

## Multiple repositories

Pass several paths to get one table and a total row:

```bash
scc-valuation --calibrate-against ../reference-repo repo-a repo-b repo-c
```

`--json` emits:

```json
{"parameters": {}, "reference": {}, "repos": [], "total": {}}
```

## Important flags

| Flag | Purpose |
|---|---|
| `--model {velocity,cocomo,blend}` | Select the effort model (default `velocity`) |
| `--velocity V` | Explicit velocity; without this or `--calibrate-against` the run is uncalibrated |
| `--cocomo-eaf F` | COCOMO effort adjustment factor (default `0.75`) |
| `--test-weight W` / `--config-weight W` | Effective-SLOC weights for Test and Config (defaults `0.5` and `0.25`) |
| `--calibrate-against PATH` | Measure velocity from a reference repository |
| `--min-commits-per-month N` | Strict person-month threshold (default `10`; loose is always `1`) |
| `--reference-person-months X` | Replace both reference measurements with `X` |
| `--bot-pattern SUBSTRING` | Repeatable; replaces the built-in bot list |
| `--include-docs W` / `--include-data W` / `--include-generated W` | Re-weight normally-ignored classes |
| `--no-debt-adjustment` | Skip technical-debt deductions |
| `--json` | Machine-readable output |

## Compatibility with the old report

The old report counted docs and data as code and always blended COCOMO with a
fixed 500-SLOC/month velocity. To reproduce its effort and cost formulas:

```bash
scc-valuation \
  --model blend \
  --include-docs 1 \
  --include-data 1 \
  --include-generated 1 \
  /path/to/repo
```

That combination also uses the old classification rules (Core/Test/Config only)
so that `package.json`-style config files are counted the way they were before.
See `CHANGELOG.md` for the default-output change.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests use canned `scc` JSON and a temporary local git repository; they do
not invoke `scc` and do not use the network.
