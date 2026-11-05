# Changelog

## 0.2.0

### Changed

- **The default output changes.** Docs, data, and generated files are now
  classified separately and weigh 0 by default. Previously they were counted as
  Core code. Generated files are recognised before every other rule.
- File classification uses the path relative to the repository root, so a repo
  cloned under `deploy/`, `docker/`, `terraform/`, or `tmp/` is no longer
  misclassified or skipped because of its ancestors.
- The default effort model is now `velocity`, and the run is labelled
  **UNCALIBRATED** unless `--calibrate-against` or `--velocity` is supplied.
  The old default velocity of 500 effective SLOC per engineer-month remains
  only as the uncalibrated placeholder.
- The report shows a low/high engineer-month and cost range instead of a single
  blended number.
- Technical-debt penalties are computed from Code and Test only, and the report
  prints the formulas. `--no-debt-adjustment` can disable them.

### Added

- `--calibrate-against PATH` measures velocity from a reference repository's
  git history at N=10 (high velocity) and N=1 (low velocity).
- `--min-commits-per-month`, `--reference-person-months`, and repeatable
  `--bot-pattern` calibration options.
- `--model {velocity,cocomo,blend}`, `--cocomo-eaf`, and explicit `--velocity`.
- `--include-docs`, `--include-data`, `--include-generated` weights.
- Multiple repository arguments and a total row.
- JSON output with `parameters`, `reference`, `repos`, and `total`.
- Stdlib `unittest` coverage for classification, person-months, model formulas,
  ranges, and the compatibility mode.

### Compatibility

To restore the old effort and cost formulas exactly:

```bash
scc-valuation \
  --model blend \
  --include-docs 1 \
  --include-data 1 \
  --include-generated 1 \
  /path/to/repo
```

That flag combination uses the legacy classifier (Core = Code + Docs + Data +
Generated), the old COCOMO/velocity blend, and the old debt formula.
