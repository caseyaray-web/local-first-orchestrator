"""Real-browser regression for the shipped M6 dashboard bundle."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "m6_browser"


def _node_modules() -> Path | None:
    configured = os.environ.get("M6_BROWSER_NODE_MODULES")
    candidates = [Path(configured)] if configured else []
    candidates.append(FIXTURE / "node_modules")
    for candidate in candidates:
        if (candidate / "playwright").is_dir() and (candidate / "react" / "package.json").is_file():
            return candidate
    return None


def test_m6_actual_dashboard_bundle_partial_stop_and_stale_error_retention() -> None:
    """Run the real bundle with React 19 and Playwright against mounted_api.__wrapped__."""
    node = shutil.which("node")
    node_modules = _node_modules()
    if node is None or node_modules is None:
        pytest.skip(
            "M6 browser fixture prerequisites absent; run the documented isolated npm/browser setup "
            "and set M6_BROWSER_NODE_MODULES"
        )
    assert node is not None and node_modules is not None
    environment = {
        **os.environ,
        "NODE_PATH": str(node_modules),
        "HERMES_M0_CLI": "",
    }
    executable = environment.get("M6_BROWSER_EXECUTABLE")
    if executable:
        browser_ready = Path(executable).is_file()
    else:
        probe = subprocess.run(
            (node, "-e", "const {chromium}=require('playwright'); process.stdout.write(chromium.executablePath())"),
            text=True, capture_output=True, env=environment, timeout=15,
        )
        browser_ready = probe.returncode == 0 and Path(probe.stdout).is_file()
    if not browser_ready:
        pytest.skip("M6 browser fixture has Playwright bindings but no configured/cached Chromium executable")
    process = subprocess.Popen(
        (node, str(FIXTURE / "driver.cjs"), "--repository", str(Path(__file__).parents[1]), "--python", sys.executable),
        cwd=Path(__file__).parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
        pytest.fail("M6 browser fixture timed out after 90 seconds\n" + (stdout or "") + (stderr or ""))
    assert process.returncode == 0, stdout + stderr
    assert '"reactVersion":"19.2.7"' in stdout
    assert '"status":409' in stdout
