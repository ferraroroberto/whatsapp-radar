# Verification

The gate commands are in the [README](../README.md#verification); this page is the detail behind them.

## E2e routing

The gate's e2e stage is routed by `scripts/classify_e2e.py` against the `[e2e]` table in `.fleet.toml`: a docs-only or backend-only diff skips the browser suite (the gate prints the router's reason), inert assets run the smoke target, a single UI surface runs its slice, and anything else runs the full suite. Uncertainty never narrows it: an unclassified path, an empty diff, a router that exits non-zero, prints no verdict or names an unknown tier all run the full suite and say why (`tests/test_gate_e2e_routing.py`).

The offline suite needs no browsers; the e2e smoke tests self-boot the webapp on a free port and require `playwright install chromium webkit` once.

## Static ratchet

The static ratchet compares the tree with the committed `.static-ratchet-baseline.json` and fails only on a *new* finding (jscpd duplicates via `npx`, so Node is required and the first run needs network; ruff `F401,F841`). Existing debt is baselined, not fixed; shrink the baseline with `scripts\static_ratchet.py --update-baseline` in the PR that removes debt. A detector that cannot run fails the gate as NOT CONFIRMED. Procedure and limits: project-scaffolding's `docs/static-ratchet.md`.

## Design capture

Each e2e test that opens a distinct screen calls the vendored `surface(page, "<tab>.<surface>")` (`tests/e2e/_surface.py`). It is inert in the gate; `/design-review whatsapp-radar measure --e2e` arms it (via `DESIGN_SURFACE_CAPTURE`) to screenshot and measure those states in light and dark on a disposable instance (`scripts/design_review_synthetic.py`, the e2e autoboot's seeded throwaway DB), never the live app. A new test that opens a new surface should call `surface()` directly in its body, after its assertions.

## CI

The same gate runs in CI on every branch push and PR to `main` ([`.github/workflows/e2e.yml`](../.github/workflows/e2e.yml)) — the local gate stays the contract; the workflow just creates the `.venv` it expects and calls it unmodified. The WebKit/iPhone e2e leg is the flaky one on the hosted runner, so CI sets `WR_E2E_TIMEOUT_SCALE=3` to give every browser wait budget 3× headroom (local runs leave it unset and keep Playwright's native budgets) and gives only the WebKit projection a bounded rerun.
