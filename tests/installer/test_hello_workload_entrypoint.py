"""Offline unit test for the reference hello-workload entrypoint.

Feature: consumer-last-mile-hello-workload §A (Req 1.1, 1.2), task 4.1.

The reference "hello-workload" (``ansible/roles/hello_workload/files/
healthz-entrypoint.sh``) is a trivial busybox-``httpd`` server whose ``/healthz``
endpoint must return **200 iff ``DATABASE_URL`` is present/non-empty**, and a
non-200 (503) when it is absent. That 200-vs-503 behaviour is what makes the
OpenBao swap-gate OBSERVABLE at the workload layer: no rendered secret ⇒ the
workload is not healthy.

These tests exercise the real entrypoint offline — no live DB, no OpenBao, no
Proxmox. busybox ships ``httpd``, so the script runs as-is: the test launches it,
hits ``/healthz`` with the secret env SET vs UNSET, and asserts the HTTP status.
It also asserts the entrypoint never echoes the secret *value* (secret hygiene,
Req 5.3). The dedicated structural swap-gate smoke is task 4.3 — this keeps 4.1's
verification light but real.

Skipped (not failed) when busybox is unavailable, per the offline-degradation
rule.
"""

from __future__ import annotations

import http.client
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

# tests/installer/test_hello_workload_entrypoint.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = (
    _REPO_ROOT / "ansible" / "roles" / "hello_workload" / "files"
    / "healthz-entrypoint.sh"
)

_SECRET_PLACEHOLDER = "postgresql://app:s3cr3t-not-real@10.0.100.10:5432/demo"

pytestmark = pytest.mark.skipif(
    shutil.which("busybox") is None,
    reason="busybox (httpd) not available — offline entrypoint test skipped",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_entrypoint(env_extra: dict[str, str]):
    """Launch the entrypoint on a free port; return (proc, port, webroot)."""
    import os
    import tempfile

    port = _free_port()
    webroot = tempfile.mkdtemp(prefix="hello-workload-")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HELLO_WORKLOAD_PORT": str(port),
        "HELLO_WORKLOAD_WEBROOT": webroot,
    }
    env.update(env_extra)
    proc = subprocess.Popen(
        ["sh", str(_ENTRYPOINT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc, port, webroot


def _get_healthz(port: int, timeout: float = 5.0) -> int:
    """Poll GET /cgi-bin/healthz until the server answers; return the status code."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
            conn.request("GET", "/cgi-bin/healthz")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return resp.status
        except (ConnectionRefusedError, OSError) as exc:  # server not up yet
            last_err = exc
            time.sleep(0.1)
    raise AssertionError(f"hello-workload never answered on :{port} ({last_err})")


def _drain(proc) -> str:
    try:
        proc.terminate()
        out, _ = proc.communicate(timeout=5)
        return out or ""
    except Exception:
        proc.kill()
        return ""


def test_healthz_returns_200_when_database_url_present():
    """Req 1.1/1.2: /healthz is 200 when DATABASE_URL is present/non-empty."""
    proc, port, _ = _run_entrypoint({"DATABASE_URL": _SECRET_PLACEHOLDER})
    try:
        status = _get_healthz(port)
        assert status == 200, f"expected 200 with DATABASE_URL set, got {status}"
    finally:
        _drain(proc)


def test_healthz_returns_503_when_database_url_absent():
    """Req 1.2: /healthz is non-200 (503) when the secret is absent.

    This is the swap-gate signal — no rendered secret ⇒ not healthy.
    """
    proc, port, _ = _run_entrypoint({})  # DATABASE_URL deliberately unset
    try:
        status = _get_healthz(port)
        assert status == 503, f"expected 503 with DATABASE_URL unset, got {status}"
    finally:
        _drain(proc)


def test_healthz_returns_503_when_database_url_empty():
    """Req 1.2: an empty (set-but-blank) DATABASE_URL is treated as absent."""
    proc, port, _ = _run_entrypoint({"DATABASE_URL": ""})
    try:
        status = _get_healthz(port)
        assert status == 503, f"expected 503 with empty DATABASE_URL, got {status}"
    finally:
        _drain(proc)


def test_entrypoint_never_echoes_the_secret_value():
    """Req 5.3 / security-standards: the secret VALUE never appears in output.

    The entrypoint may log boolean presence ("DATABASE_URL present"/"absent") but
    MUST NOT log the value itself.
    """
    proc, port, _ = _run_entrypoint({"DATABASE_URL": _SECRET_PLACEHOLDER})
    try:
        _get_healthz(port)
    finally:
        out = _drain(proc)
    assert _SECRET_PLACEHOLDER not in out, (
        "the entrypoint leaked the DATABASE_URL value into its output — "
        "secret hygiene violation (Req 5.3)."
    )
    assert "s3cr3t-not-real" not in out, "secret substring leaked into output."
    # Positive: it SHOULD log the boolean presence.
    assert "DATABASE_URL present" in out, (
        "the entrypoint should log boolean presence of DATABASE_URL."
    )
