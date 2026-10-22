"""The pre-ship gate routes its e2e stage through `scripts/classify_e2e.py` (#424).

Narrowing the gate is the risky direction, so these pin both ends: a docs-only
diff skips the browser suite with the router's reason, and every way of not
getting a usable verdict (exit code, garbage, unknown tier, no target, a router
that cannot start) still runs the full suite and says why. They drive the real
`scripts/verify-before-ship.ps1 -RouteOnly` against stub routers in a temp
directory: no browser, no webapp, no sidecar, no message sent.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
POWERSHELL = Path(
    "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not POWERSHELL.is_file(),
    reason="the gate is a Windows PowerShell script",
)

FULL = "tier=full target=tests/e2e browsers=chromium,webkit"


def _route(classifier: Path) -> str:
    out = subprocess.run(
        [
            str(POWERSHELL), "-NoProfile", "-NonInteractive",
            "-File", str(GATE), "-RouteOnly", "-Classifier", str(classifier),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=120, check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def _stub(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "stub_router.py"
    script.write_text(body, encoding="utf-8")
    return script


def _real_router_for(tmp_path: Path, *paths: str) -> Path:
    """The real classifier over an explicit file list, as if that were the diff."""
    return _stub(
        tmp_path,
        "import runpy, sys\n"
        f"sys.argv = ['classify_e2e.py', *{list(paths)!r}]\n"
        f"runpy.run_path({str(REPO_ROOT / 'scripts' / 'classify_e2e.py')!r},"
        " run_name='__main__')\n",
    )


def test_docs_only_diff_skips_the_browser_suite_with_the_routers_reason(
    tmp_path: Path,
) -> None:
    out = _route(_real_router_for(tmp_path, "README.md", "docs/architecture.mmd"))
    assert "E2E_ROUTE tier=skip" in out
    reason = next(ln for ln in out.splitlines() if "E2E_ROUTE reason=" in ln)
    assert "README.md" in reason or "docs/" in reason


def test_a_declared_full_path_runs_the_full_suite(tmp_path: Path) -> None:
    out = _route(_real_router_for(tmp_path, "README.md", "app/webapp/server.py"))
    assert f"E2E_ROUTE {FULL}" in out


def test_an_unclassified_path_runs_the_full_suite(tmp_path: Path) -> None:
    out = _route(_real_router_for(tmp_path, "README.md", "mystery/new_thing.xyz"))
    assert f"E2E_ROUTE {FULL}" in out


def test_an_empty_diff_runs_the_full_suite(tmp_path: Path) -> None:
    # The real router reads the live diff when given no paths, which a test
    # cannot pin, so the router's empty-diff verdict is stubbed.
    stub = _stub(
        tmp_path,
        "print('E2E_TIER=full')\nprint('E2E_BROWSERS=')\n"
        "print('E2E_PYTEST_TARGET=tests/e2e')\n"
        "print('E2E_REASON=empty-diff: no changed files')\n",
    )
    out = _route(stub)
    assert f"E2E_ROUTE {FULL}" in out
    assert "empty-diff" in out


def test_a_surface_slice_keeps_its_targets_and_defaults_the_browsers(
    tmp_path: Path,
) -> None:
    stub = _stub(
        tmp_path,
        "print('E2E_TIER=surface')\nprint('E2E_BROWSERS=')\n"
        "print('E2E_PYTEST_TARGET=tests/e2e/a.py tests/e2e/b.py')\n"
        "print('E2E_REASON=one surface')\n",
    )
    out = _route(stub)
    assert "tier=surface target=tests/e2e/a.py tests/e2e/b.py" in out
    assert "browsers=chromium,webkit" in out


def test_a_static_verdict_keeps_the_routers_narrow_browser(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        "print('E2E_TIER=static')\nprint('E2E_BROWSERS=chromium')\n"
        "print('E2E_PYTEST_TARGET=tests/e2e/test_smoke.py')\n"
        "print('E2E_REASON=inert asset')\n",
    )
    out = _route(stub)
    assert "tier=static target=tests/e2e/test_smoke.py browsers=chromium" in out


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("import sys\nprint('E2E_TIER=skip')\nsys.exit(3)\n", "router failed (exit 3)"),
        ("print('not a verdict at all')\n", "no E2E_TIER verdict"),
        ("print('E2E_TIER=')\n", "no E2E_TIER verdict"),
        (
            "print('E2E_TIER=bogus')\nprint('E2E_PYTEST_TARGET=x')\n",
            "unknown tier 'bogus'",
        ),
        ("print('E2E_TIER=static')\n", "without a pytest target"),
        ("raise SystemExit('boom')\n", "router failed (exit 1)"),
    ],
)
def test_a_router_that_gives_no_usable_verdict_runs_the_full_suite(
    tmp_path: Path, body: str, why: str
) -> None:
    out = _route(_stub(tmp_path, body))
    assert f"E2E_ROUTE {FULL}" in out
    assert why in out
    assert "fail-safe" in out


def test_a_router_script_that_does_not_exist_runs_the_full_suite(
    tmp_path: Path,
) -> None:
    out = _route(tmp_path / "missing_router.py")
    assert f"E2E_ROUTE {FULL}" in out
    assert "fail-safe" in out
