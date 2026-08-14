"""The deployment entrypoint. A host must be able to start this."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_the_entrypoint_exists_where_hosts_look():
    """Railpack runs main.py when it finds one; without it the deploy fails."""
    assert (ROOT / "main.py").is_file()


def test_the_entrypoint_runs_without_the_package_installed():
    """Railpack installs requirements.txt, not this project.

    `python -m lumia.server` therefore fails with ModuleNotFoundError on a
    real deploy. main.py puts src on the path itself, so it boots either way.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'src'); "
         "import main; print(main.main.__module__)"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "lumia.server" in result.stdout


def test_every_start_command_agrees():
    """Two configs naming different commands is a deploy that works by luck."""
    procfile = (ROOT / "Procfile").read_text()
    railway = json.loads((ROOT / "railway.json").read_text())
    railpack = json.loads((ROOT / "railpack.json").read_text())

    command = "python main.py"
    assert command in procfile
    assert railway["deploy"]["startCommand"] == command
    assert railpack["deploy"]["startCommand"] == command


def test_the_page_is_found_from_the_app_root():
    """The control room must not 500 while the health check reports 200."""
    from lumia.server import _find_page

    assert _find_page() is not None


def test_state_never_defaults_inside_site_packages():
    """Installed, `parents[2]/data` lands in the Python installation."""
    from lumia.config import IN_CHECKOUT, default_data_dir

    location = default_data_dir()
    assert "site-packages" not in str(location)
    if not IN_CHECKOUT:
        assert location == Path.cwd() / "data"


def test_the_python_version_is_pinned():
    """Unpinned, the builder picks a version nobody chose."""
    pinned = (ROOT / ".python-version").read_text().strip()
    assert pinned.startswith("3.")


def test_the_server_shuts_down_on_sigterm():
    """Railway sends SIGTERM on every redeploy and scale-down.

    Without a handler the process is killed outright partway through
    whatever it was doing. This boots the real entrypoint, waits for it to
    answer, and asserts the signal ends it cleanly rather than the platform
    having to escalate.
    """
    port = _free_port()
    environment = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}

    process = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=ROOT, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        answered = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2
                ) as response:
                    answered = response.status == 200
                    break
            except OSError:
                time.sleep(0.2)
        assert answered, "the server never came up"

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:  # pragma: no cover - only on failure
            process.kill()
        output = process.communicate()[0] or ""

    assert "shutting down" in output
    assert "stopped cleanly" in output


def test_startup_says_what_the_deployment_actually_is():
    """A container that boots and misbehaves is usually misconfigured.

    The report is the first thing in the logs precisely so that reading it
    beats reading the source, and two of its lines are the ones that make
    a hosted deploy lose data quietly.
    """
    from lumia.server import startup_report

    report = startup_report()
    assert report["agents"] > 0
    assert "data_is_persistent" in report  # unset ASHRAH_DATA_DIR → warned about
    assert report["page_found"] is True
    assert report["contract"]  # fingerprint, so a stale node is visible
