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
    # BUGFIX fix-hello-workload-cold-boot-rsf-race: presence is now derived from
    # the LIVE mounted RSF FILE, not the boot env var. These base-suite tests
    # express "secret present/absent" by SEEDING that RSF file from the supplied
    # DATABASE_URL (the new source of truth), so their observable contract
    # (200-iff-present, boolean-presence log) is preserved against the new
    # mechanism. A non-empty DATABASE_URL -> a seeded RSF line; unset/empty ->
    # no RSF file -> absent. The secret still never enters the container env for
    # presence purposes, matching how the RSF drives presence in production.
    rsf_dir = tempfile.mkdtemp(prefix="hello-workload-basersf-")
    rsf_path = os.path.join(rsf_dir, "secrets.env")
    env["HELLO_WORKLOAD_RSF"] = rsf_path
    env["HELLO_WORKLOAD_RSF_REFRESH_SECS"] = "1"
    _secret_val = env_extra.get("DATABASE_URL")
    if _secret_val:
        with open(rsf_path, "w", encoding="utf-8") as _fh:
            _fh.write(f"DATABASE_URL={_secret_val}\n")
    # Preserve any non-secret env the caller passed; the RSF now carries the
    # secret, so we do not need DATABASE_URL in the process env itself.
    env.update({k: v for k, v in env_extra.items() if k != "DATABASE_URL"})
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


# =========================================================================== #
# BUGFIX: fix-hello-workload-cgi-secret-visibility
#
# STRUCTURAL assertions over healthz-entrypoint.sh for the CGI secret-visibility
# bug. These are the AUTHORITATIVE bug-condition + preservation gate.
#
# WHY STRUCTURAL (not functional): busybox `httpd` sanitizes the CGI environment
# and does NOT forward arbitrary container env vars (like DATABASE_URL) to CGI
# children — so a CGI handler that re-reads $DATABASE_URL always sees it empty
# and returns 503 even when the secret IS present (verified live on the stock
# busybox:1.37 image). But whether busybox forwards the var is BUILD-DEPENDENT:
# on Ubuntu's busybox 1.36.1 it IS forwarded, so the functional probe above
# passes even on the buggy code and CANNOT distinguish buggy from fixed. The
# structural tests below parse the script and assert the MECHANISM — the CGI
# reads a boot-time boolean presence marker written by the parent (which has the
# env), not the sanitized CGI env var — which is the deterministic bug->fix
# delta on any host.
#
# See .kiro/specs/fix-hello-workload-cgi-secret-visibility/ (bugfix.md/design.md).
# =========================================================================== #

import re  # noqa: E402

_ENTRYPOINT_SRC = _ENTRYPOINT.read_text(encoding="utf-8")


def _extract_cgi_handler_heredoc(src: str) -> str:
    """Return the body of the generated `cgi-bin/healthz` CGI handler.

    The entrypoint writes the handler via a ``cat > <webroot>/cgi-bin/healthz
    <<CGI ... CGI`` heredoc. Isolate that body so assertions target the CGI
    CHILD script (what busybox executes per request), not the parent entrypoint.
    """
    m = re.search(r"cgi-bin/healthz\"?\s*<<'?CGI'?\n(.*?)\nCGI\b", src, re.DOTALL)
    assert m, (
        "could not locate the cgi-bin/healthz heredoc in the entrypoint — the "
        "test's parsing assumption drifted from the script."
    )
    return m.group(1)


# --------------------------------------------------------------------------- #
# Property 1: Bug Condition (red on unfixed code, green after fix).
# --------------------------------------------------------------------------- #
def test_cgi_handler_reads_presence_marker_not_raw_env():
    """Property 1 / Req 2.1: the CGI handler derives presence from a boot-time
    MARKER FILE, not by reading the configured secret env var directly.

    On the UNFIXED script the CGI handler ``eval``s ``${DATABASE_URL}`` /
    ``${HELLO_WORKLOAD_SECRET_ENV}`` from its own (busybox-sanitized) environment
    — so this assertion FAILS (confirms the bug: the CGI depends on an env var
    busybox httpd will not forward to the child). After the fix the handler reads
    the marker and this PASSES.
    """
    cgi = _extract_cgi_handler_heredoc(_ENTRYPOINT_SRC)
    # The CGI must reference the boot-time presence marker.
    assert "MARKER" in cgi or "-present" in cgi or "healthz-secret-present" in cgi, (
        "the /healthz CGI handler must read a boot-time presence MARKER file "
        "(e.g. .healthz-secret-present) written by the parent entrypoint — not "
        "the secret env var. busybox httpd sanitizes DATABASE_URL out of the CGI "
        "child environment, so an env-var read is always empty -> always 503. "
        f"CGI handler body:\n{cgi}"
    )
    # The CGI must NOT derive presence from the raw secret env var.
    assert "HELLO_WORKLOAD_SECRET_ENV" not in cgi, (
        "the CGI handler must not resolve HELLO_WORKLOAD_SECRET_ENV itself — that "
        "indirection belongs in the parent, which computes the boolean marker."
    )
    assert "DATABASE_URL" not in cgi, (
        "the CGI handler must not reference DATABASE_URL directly — it reads only "
        "the boolean marker (busybox does not forward DATABASE_URL to CGI)."
    )
    # No per-request eval of a secret var inside the CGI child.
    assert "eval" not in cgi, (
        "the CGI handler must not `eval` a secret env var — it reads the marker. "
        f"CGI handler body:\n{cgi}"
    )


def test_parent_entrypoint_writes_boolean_only_presence_marker():
    """Property 1 / Req 2.1, 2.3: the PARENT entrypoint (which DOES have the
    container env) writes a presence marker whose content is ONLY the boolean
    ``present``/``absent`` — never the secret value — located OUTSIDE cgi-bin so
    busybox never executes/serves it.

    FAILS on unfixed code (no marker is written at all)."""
    # Parent context = the whole script minus the CGI heredoc body.
    cgi = _extract_cgi_handler_heredoc(_ENTRYPOINT_SRC)
    parent = _ENTRYPOINT_SRC.replace(cgi, "")

    # A marker variable/path is defined and written by the parent.
    assert re.search(r"(MARKER|healthz-secret-present)", parent), (
        "the parent entrypoint must define/write a boot-time presence marker "
        "file (e.g. ${HELLO_WORKLOAD_WEBROOT}/.healthz-secret-present)."
    )
    # The marker must live OUTSIDE cgi-bin (never executed/served as CGI).
    assert "cgi-bin/.healthz" not in parent and "cgi-bin/healthz-secret" not in parent, (
        "the presence marker must NOT live under cgi-bin/ (busybox would treat "
        "it as a CGI script); put it directly in the webroot."
    )
    # The marker write must emit only the boolean words — never the secret value.
    # Accept a written-through shell var that only ever holds present/absent.
    assert re.search(r"\b(present|absent)\b", parent), (
        "the parent must compute the boolean presence words present/absent."
    )


# --------------------------------------------------------------------------- #
# Hygiene (holds before and after — guards the change).
# --------------------------------------------------------------------------- #
def test_entrypoint_never_interpolates_the_secret_value_anywhere():
    """Req 2.3 / 5.3: neither the marker write, the CGI handler, nor any log line
    interpolates the secret VALUE (or a rendered-field path). Only the boolean
    presence / status word is ever emitted or persisted."""
    forbidden = re.compile(r"\.Data\.data\.|printf[^\n]*\$\{?DATABASE_URL")
    assert not forbidden.search(_ENTRYPOINT_SRC), (
        "the entrypoint must never printf/echo/write the DATABASE_URL value — "
        "only the boolean presence or the status word (Req 2.3 / 5.3)."
    )


# --------------------------------------------------------------------------- #
# Property 2: Preservation (green on unfixed AND fixed code).
# --------------------------------------------------------------------------- #
def test_preserve_200_503_contract_bodies():
    """Property 2 / Req 3.1: the 200-iff-present / 503-otherwise contract and the
    exact response bodies are unchanged."""
    cgi = _extract_cgi_handler_heredoc(_ENTRYPOINT_SRC)
    assert "Status: 200 OK" in cgi and "Status: 503 Service Unavailable" in cgi, (
        "the CGI handler must keep the 200/503 status contract."
    )
    assert "hello-workload healthy" in cgi, "the healthy body must be preserved."
    assert "hello-workload not ready: secret absent" in cgi, (
        "the not-ready body must be preserved."
    )


def test_preserve_secret_env_indirection_and_config_vars():
    """Property 2 / Req 3.2: the HELLO_WORKLOAD_SECRET_ENV indirection (default
    DATABASE_URL) and the port/webroot config vars with their defaults are
    preserved (resolved in the PARENT)."""
    assert "HELLO_WORKLOAD_SECRET_ENV" in _ENTRYPOINT_SRC
    assert ":-DATABASE_URL" in _ENTRYPOINT_SRC, (
        "the default configured secret name must remain DATABASE_URL."
    )
    assert ":-8080" in _ENTRYPOINT_SRC, "the default port 8080 must be preserved."
    assert "/tmp/hello-workload" in _ENTRYPOINT_SRC, (
        "the default webroot /tmp/hello-workload must be preserved."
    )


def test_preserve_busybox_httpd_server_line_and_endpoint():
    """Property 2 / Req 3.3: the server is still busybox httpd in the foreground
    on the configured port/webroot, with the CGI handler at cgi-bin/healthz."""
    assert re.search(r"exec\s+busybox\s+httpd\s+-f\b", _ENTRYPOINT_SRC), (
        "the server must remain `exec busybox httpd -f ...` (foreground PID 1)."
    )
    assert "-p \"${HELLO_WORKLOAD_PORT}\"" in _ENTRYPOINT_SRC
    assert "-h \"${HELLO_WORKLOAD_WEBROOT}\"" in _ENTRYPOINT_SRC
    assert "cgi-bin/healthz" in _ENTRYPOINT_SRC, (
        "the CGI endpoint path /cgi-bin/healthz must be preserved (the "
        "healthcheck probes it)."
    )


def test_preserve_boolean_presence_startup_log_value_free():
    """Property 2 / Req 3.4: the parent still logs the boolean presence of the
    configured secret and never the value.

    Asserts the preserved BEHAVIOR (the emitted log text) rather than a specific
    printf format string: the parent emits ``hello-workload: <NAME> present`` /
    ``... absent`` for the configured secret, derived from the boolean
    ``present``/``absent`` words — never the secret value.
    """
    parent = _ENTRYPOINT_SRC.replace(_extract_cgi_handler_heredoc(_ENTRYPOINT_SRC), "")
    # The parent logs the human-facing presence line for the configured secret.
    assert "hello-workload:" in parent and "present" in parent and "absent" in parent, (
        "the startup boolean-presence log (hello-workload: <NAME> present/absent) "
        "must be preserved."
    )
    # It logs the configured secret NAME, not its value.
    assert "HELLO_WORKLOAD_SECRET_ENV" in parent, (
        "the presence log must reference the configured secret by NAME."
    )
    assert _SECRET_PLACEHOLDER not in _ENTRYPOINT_SRC


# =========================================================================== #
# BUGFIX: fix-hello-workload-cold-boot-rsf-race
#
# Cold-boot RACE: on a fresh `docker compose up` the workload container can be
# CREATED before the OpenBao Agent has written a non-empty Rendered_Secret_File
# (RSF). The UNFIXED entrypoint then (a) gets an empty $DATABASE_URL from the
# Compose env_file host-side create-time snapshot and (b) freezes its presence
# marker `absent` ONCE at boot — so /healthz stays 503 forever even after the
# Agent renders the RSF, until a manual `--force-recreate`.
#
# The workload ALREADY mounts the shared rendered dir READ-ONLY at the
# in-container path (/openbao/agent/rendered/secrets.env) — a LIVE file the Agent
# writes. The fix makes the entrypoint derive presence from that live RSF FILE
# and refresh it on a small bounded cadence, so health self-heals on cold boot
# WITHOUT weakening fail-closed (no/empty/key-absent RSF ⇒ absent ⇒ 503).
#
# See .kiro/specs/fix-hello-workload-cold-boot-rsf-race/ (bugfix.md/design.md).
# =========================================================================== #


def _run_entrypoint_with_rsf(rsf_path: str, env_extra: dict[str, str] | None = None):
    """Launch the entrypoint pointed at a test RSF file; return (proc, port, webroot).

    HELLO_WORKLOAD_RSF overrides the in-container mounted RSF path so the test can
    create/empty the file at will. The secret env is deliberately NOT set — the
    whole point of the fix is that presence derives from the RSF FILE, not the
    boot env.
    """
    import os
    import tempfile

    port = _free_port()
    webroot = tempfile.mkdtemp(prefix="hello-workload-coldboot-")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HELLO_WORKLOAD_PORT": str(port),
        "HELLO_WORKLOAD_WEBROOT": webroot,
        "HELLO_WORKLOAD_RSF": rsf_path,
        # Keep the refresh cadence snappy so the test does not wait long.
        "HELLO_WORKLOAD_RSF_REFRESH_SECS": "1",
    }
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        ["sh", str(_ENTRYPOINT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc, port, webroot


def _poll_healthz_until(port: int, want_status: int, timeout: float = 8.0) -> bool:
    """Poll /cgi-bin/healthz until it returns `want_status`; True if seen in time."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
            conn.request("GET", "/cgi-bin/healthz")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == want_status:
                return True
        except (ConnectionRefusedError, OSError):
            pass
        time.sleep(0.2)
    return False


# --------------------------------------------------------------------------- #
# Property 1: Bug Condition — FUNCTIONAL (red on unfixed code).
# --------------------------------------------------------------------------- #
def test_coldboot_rsf_written_after_boot_flips_healthz_to_200():
    """Property 1 / Req 2.1, 2.2: when the workload boots with the RSF
    absent/empty (empty secret env) and the OpenBao Agent LATER writes a non-empty
    RSF, /healthz flips to 200 within a bounded interval WITHOUT a recreate.

    RED on the UNFIXED entrypoint: its marker is frozen `absent` at boot and never
    re-reads the RSF, so /healthz stays 503 and the 200-poll times out.
    """
    import os
    import tempfile

    rsf_dir = tempfile.mkdtemp(prefix="hello-workload-rsf-")
    rsf_path = os.path.join(rsf_dir, "secrets.env")
    # Cold boot: RSF file does NOT exist yet.
    proc, port, _ = _run_entrypoint_with_rsf(rsf_path)
    try:
        # Initially absent -> 503 (fail-closed holds at boot).
        assert _poll_healthz_until(port, 503, timeout=5.0), (
            "expected 503 while the RSF is absent at cold boot"
        )
        # Agent renders a non-empty RSF AFTER the workload has booted.
        with open(rsf_path, "w", encoding="utf-8") as fh:
            fh.write(f"DATABASE_URL={_SECRET_PLACEHOLDER}\n")
        # The fix must flip /healthz to 200 within a few refresh cycles.
        assert _poll_healthz_until(port, 200, timeout=8.0), (
            "COLD-BOOT RACE: /healthz never flipped to 200 after the RSF was "
            "written post-boot. The entrypoint must derive presence from the live "
            "mounted RSF file and refresh it — not freeze a boot-time marker."
        )
    finally:
        _drain(proc)


def test_coldboot_rsf_emptied_after_present_flips_back_to_503():
    """Property 1 / Req 2.3: fail-closed holds at RUNTIME. After the RSF is
    present (200), emptying/removing it flips /healthz back to 503.

    RED on the UNFIXED entrypoint for the complementary reason (marker frozen at
    boot); GREEN after the fix because presence tracks the live RSF both ways.
    """
    import os
    import tempfile

    rsf_dir = tempfile.mkdtemp(prefix="hello-workload-rsf-")
    rsf_path = os.path.join(rsf_dir, "secrets.env")
    with open(rsf_path, "w", encoding="utf-8") as fh:
        fh.write(f"DATABASE_URL={_SECRET_PLACEHOLDER}\n")
    proc, port, _ = _run_entrypoint_with_rsf(rsf_path)
    try:
        assert _poll_healthz_until(port, 200, timeout=6.0), (
            "expected 200 with a non-empty RSF present from boot"
        )
        # The RSF loses its secret (e.g. Agent re-renders empty when Primary seals).
        with open(rsf_path, "w", encoding="utf-8") as fh:
            fh.write("")
        assert _poll_healthz_until(port, 503, timeout=8.0), (
            "runtime fail-closed: /healthz must flip back to 503 once the RSF no "
            "longer carries a non-empty secret."
        )
    finally:
        _drain(proc)


def test_coldboot_rsf_fix_never_leaks_secret_value():
    """Property 1 / Req 3.3: the live-RSF presence check never emits the secret
    value into output/logs even when the RSF carries it."""
    import os
    import tempfile

    rsf_dir = tempfile.mkdtemp(prefix="hello-workload-rsf-")
    rsf_path = os.path.join(rsf_dir, "secrets.env")
    with open(rsf_path, "w", encoding="utf-8") as fh:
        fh.write(f"DATABASE_URL={_SECRET_PLACEHOLDER}\n")
    proc, port, _ = _run_entrypoint_with_rsf(rsf_path)
    try:
        _poll_healthz_until(port, 200, timeout=6.0)
    finally:
        out = _drain(proc)
    assert _SECRET_PLACEHOLDER not in out, "the RSF-derived check leaked the value."
    assert "s3cr3t-not-real" not in out, "secret substring leaked into output."


# --------------------------------------------------------------------------- #
# Property 1: Bug Condition — STRUCTURAL (host-independent; red on unfixed code).
# --------------------------------------------------------------------------- #
def test_entrypoint_derives_presence_from_rsf_file_not_only_boot_env():
    """Property 1 / Req 2.1: the entrypoint must reference a configurable RSF FILE
    path and a REFRESH mechanism (background loop / periodic re-derivation), so
    presence is NOT computed solely from a single boot-time env read.

    RED on the UNFIXED entrypoint: it has no RSF file path and no refresh loop —
    it reads the secret env once at boot. GREEN after the fix.
    """
    src = _ENTRYPOINT.read_text(encoding="utf-8")
    # A configurable in-container RSF file path.
    assert "HELLO_WORKLOAD_RSF" in src, (
        "the entrypoint must expose a configurable RSF path "
        "(HELLO_WORKLOAD_RSF, default the mounted /openbao/agent/rendered/"
        "secrets.env) and derive presence from that live file."
    )
    assert "/openbao/agent/rendered/secrets.env" in src, (
        "the default RSF path must be the in-container mounted RSF."
    )
    # A refresh mechanism so a post-boot RSF write is reflected without recreate.
    assert re.search(r"while\s+", src) and "REFRESH" in src.upper(), (
        "the entrypoint must refresh the presence marker on a bounded cadence "
        "(a background loop re-deriving present/absent from the RSF file), not "
        "freeze it once at boot."
    )
    # Presence must be derived from the RSF file (a grep over the file).
    assert re.search(r"grep[^\n]*HELLO_WORKLOAD_RSF", src) or re.search(
        r"grep[^\n]*secrets\.env", src
    ), (
        "presence must be derived by testing the configured KEY against the RSF "
        "FILE contents, not the boot-time env var alone."
    )
