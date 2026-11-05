# scc-valuation

Automated replacement cost and engineering effort evaluator for codebases.
Uses `scc` under the hood with intelligent file categorization, test suite weighting,
dual-model estimation (COCOMO II + Velocity Run-Rate), and technical debt adjustments.

## Installation & Usage (via uv)

You can run this directly using `uvx` without installing it globally:

```bash
uvx --from . scc-valuation /path/to/target-repo
```

Or install it globally via `uv`:
```bash
uv tool install --from . scc-valuation
scc-valuation /path/to/target-repo
```
