# Verification

The gate commands are in the [README](../README.md#verification); this page is the detail behind them.

## E2e routing

The gate's e2e stage is routed by `scripts/classify_e2e.py` against the `[e2e]` table in `.fleet.toml`: a docs-only or backend-only diff skips the browser suite (the gate prints the router's reason), inert assets run the smoke target, a single UI surface runs its slice, and anything else runs the full suite. Uncertainty never narrows it: an unclassified path, an empty diff, a router that exits non-zero, prints no verdict or names an unknown tier all run the full suite and say why (`tests/test_gate_e2e_routing.py`).

The offline suite needs no browsers; the e2e smoke tests self-boot the webapp on a free port and require `playwright install chromium webkit` once.

## Static ratchet

The static ratchet compares the tree with the committed `.static-ratchet-baseline.json` and fails only on a *new* finding (jscpd duplicates via `npx`, so Node is required and the first run needs network; ruff `F401,F841`). Existing debt is baselined, not fixed; shrink the baseline with `scripts\static_ratchet.py --update-baseline` in the PR that removes debt. A detector that cannot run fails the gate as NOT CONFIRMED. Procedure and limits: project-scaffolding's `docs/static-ratchet.md`.

## Comment ratchet

The comment ratchet counts `#NNN` issue references inside product-code comments (Python `#`, JS `//` and `/* */`, CSS `/* */`; strings, docstrings and URLs are not counted), per file, and fails when a file's count rises above its entry in the committed `.comment-ratchet-baseline.json` or a file outside the baseline gains any. A comment states the present-tense why; history belongs to git, PRs and issues. Existing references are baselined, not fixed; lower the baseline with `scripts\comment_ratchet.py --update-baseline` in the PR that rewrites them. Vendored destinations are excluded automatically. It reads the whole tree, so it is never path-routed, and `verify-before-ship.ps1` calls it only through `scripts.comment_ratchet_gate`, which fails exit 3 (a file could not be scanned) as NOT CONFIRMED, never a pass or a skip. Procedure and limits: project-scaffolding's `docs/comment-ratchet.md`.

## Structure ratchet

The structure ratchet enforces [`layout.md`](layout.md): a file over 1,000 lines, a function over 100 lines, a directory over 40 modules, a `src/` file importing `app/`, and a root folder that neither the declared `shape` (`webapp-tray`) nor `extra_roots` lists all fail when new. Offenders that predate it sit in the committed `.structure-ratchet-baseline.json` at their size and fail when they grow; the root layout is never baselined, a declared exception is data in `extra_roots`. Lower the baseline with `scripts\structure_ratchet.py --update-baseline` in the PR that splits an offender; only `--allow-additions` raises it, and it shows in the diff. It reads the whole tree, so it is never path-routed, and `verify-before-ship.ps1` calls it only through `scripts.structure_ratchet_gate`, which fails exit 3 (git unusable, an unparseable file, an unknown shape) as NOT CONFIRMED, never a pass or a skip. Procedure and limits: project-scaffolding's `docs/structure-ratchet.md`.

## Instruction ratchet

The instruction ratchet runs the weekly prompt audit's deterministic rules over `CLAUDE.md` and `AGENTS.md` (this repo ships no skills or rules files) and fails when a file gains a rule hit beyond the committed `.instruction-ratchet-baseline.json` or passes its size cap (200 lines for `CLAUDE.md`). Existing hits are baselined, not excluded; the baseline is empty today. A hit is a candidate: rewording the line is usually the fix, and only `--allow-additions` raises the baseline, which shows in the diff. It reads the whole tree, so it is never path-routed, and `verify-before-ship.ps1` calls it only through `scripts.instruction_ratchet_gate`, which fails exit 3 (git unusable, an unreadable file) as NOT CONFIRMED, never a pass or a skip. Procedure and limits: project-scaffolding's `docs/instruction-ratchet.md`.

## Doc-refs ratchet

The doc-refs ratchet resolves every repo path, `python -m` module and relative link that `README.md`, `CLAUDE.md` and `docs/**/*.md` name against the tracked tree, and fails on a dangling one the committed `.doc-refs-ratchet-baseline.json` does not record (empty today). No `port` is declared in `.fleet.toml`, so loopback ports are not checked. The fix is to point the reference at what exists; a deliberate pointer to another repo's file (App Launcher's runtime registries) is listed once in `allow_refs`. It reads the whole tree, so it is never path-routed, and `verify-before-ship.ps1` calls it only through `scripts.doc_refs_ratchet_gate`, which fails exit 3 (git unusable, a non-UTF-8 doc, an unclosed fence) as NOT CONFIRMED, never a pass or a skip. Procedure and limits: project-scaffolding's `docs/doc-refs-ratchet.md`.

## Docs ratchet

The docs ratchet compares `README.md` and `docs/**/*.md` with the committed `.docs-ratchet-baseline.json` and fails when the README grows past 40 KiB (or past its baselined size) or a file gains a history marker: an issue reference, a dated heading or a changelog heading. Docs state the present-tense why; history belongs to git, PRs and issues. Existing markers are baselined, not fixed; lower the baseline with `scripts\docs_ratchet.py --update-baseline` in the PR that removes them. It reads the whole tree, so it is never path-routed, and `verify-before-ship.ps1` calls it only through `scripts.docs_ratchet_gate`, which fails exit 3 (the docs could not be scanned) as NOT CONFIRMED, never a pass or a skip. Procedure and limits: project-scaffolding's `docs/docs-ratchet.md`.

## Design capture

Each e2e test that opens a distinct screen calls the vendored `surface(page, "<tab>.<surface>")` (`tests/e2e/_surface.py`). It is inert in the gate; `/design-review whatsapp-radar measure --e2e` arms it (via `DESIGN_SURFACE_CAPTURE`) to screenshot and measure those states in light and dark on a disposable instance (`scripts/design_review_synthetic.py`, the e2e autoboot's seeded throwaway DB), never the live app. A new test that opens a new surface should call `surface()` directly in its body, after its assertions.

## CI

The same gate runs in CI on every branch push and PR to `main` ([`.github/workflows/e2e.yml`](../.github/workflows/e2e.yml)) — the local gate stays the contract; the workflow just creates the `.venv` it expects and calls it unmodified. The WebKit/iPhone e2e leg is the flaky one on the hosted runner, so CI sets `WR_E2E_TIMEOUT_SCALE=3` to give every browser wait budget 3× headroom (local runs leave it unset and keep Playwright's native budgets) and gives only the WebKit projection a bounded rerun.
