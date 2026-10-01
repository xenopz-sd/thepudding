"""Ansible check-mode idempotency test for the ``openbao_install`` role.

Task 5.5 (spec: svc-07-secrets-manager) — requirements.md Requirement 13.1/13.2
("the install role skips when the pinned version is already running; a re-run
reports zero changed tasks"). design.md "Testing Strategy §1 (Ansible check-mode
idempotency)".

WHAT THIS TEST PROVES
---------------------
Run the REAL, unmodified ``openbao_install`` role against a fixture
"already-configured" state and assert the play recap reports ``changed=0`` — the
role skips the whole install/bring-up when the pinned OpenBao version is already
running.

HOW THE "ALREADY-CONFIGURED" STATE IS SIMULATED
-----------------------------------------------
Two independent halves, both offline and deterministic (no Docker daemon, no real
OpenBao container, no ``requires_infra`` dependency):

1. *Pinned version already running.* The role's version-check guard probes the
   container with ``community.docker.docker_container_info`` (``check_mode:
   false`` so it runs even under ``--check``) and, from that probe, computes the
   fact ``openbao_already_at_pinned_version``. The fixture playbook's adjacent
   ``library/docker_container_info.py`` stub SHADOWS the real module (Ansible
   auto-discovers a ``library/`` dir next to the playbook) and returns the pinned
   image (sourced from ``OPENBAO_STUB_PINNED_IMAGE``, which this runner sets to
   the role's pinned ``openbao_image``) as already running. That makes the fact
   ``True`` and gates off every bring-up task.

2. *Config already rendered.* The role's ``config.hcl``/dir tasks are
   intentionally NOT gated by the version fact (the ``template``/``file`` modules
   are idempotent by nature). So this runner invokes the playbook TWICE against
   the SAME temp dirs — a first NORMAL run renders ``config.hcl`` and creates the
   dirs, then a second ``--check`` run. In the second run those tasks are already
   satisfied, so they are no-ops too. Only the SECOND (check) run's recap is
   asserted to be ``changed=0``.

The fixture drives the ``unsealer`` role so NO secret value (the primary's seal
``transit`` token) is ever needed or templated (no-secret-in-fixtures steering).
The idempotency machinery under test — the probe-derived fact plus the gated
bring-up — is identical for the primary and the unsealer.

GATING (offline-by-construction, absent tooling => skip, never fail)
--------------------------------------------------------------------
``ansible-playbook`` is NOT installed in the project venv (``~/venv/devinfra``);
prior SVC-07 tasks confirmed this. Per the documentation-testing steering
(absent tooling => skipped, not failed) and the same ``shutil.which`` skip idiom
the suite already uses for the ``terraform`` binary (see
``conftest.py::requires_terraform``), this test is ``skipif``-gated on the
presence of an ``ansible-playbook`` binary (checked on ``PATH`` and at the venv
path). Where Ansible IS present (a developer machine or CI with Ansible
installed) the test RUNS for real; where it is absent it SKIPS CLEANLY with an
explicit reason. It is deliberately NOT ``requires_infra`` — it needs no live
Proxmox/OpenBao, only an ``ansible-playbook`` binary and ``community.docker``
(for the module the stub shadows to resolve its namespace).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_install_idempotency.py -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Repo-relative paths. This file lives at <repo>/infra/tests/... so the repo
# root is two parents up.
# --------------------------------------------------------------------------- #
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
_ANSIBLE_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_FIXTURE_DIR = _THIS_DIR / "fixtures" / "svc07_openbao_install_idempotency"
_PLAYBOOK = _FIXTURE_DIR / "already_configured.yml"
_INVENTORY = _FIXTURE_DIR / "inventory.ini"
#: Scoped fake ``community.docker`` collection. It shadows the FQCN the role
#: probes with (``community.docker.docker_container_info``) so the offline stub
#: reports the pinned image already running, WITHOUT any Docker daemon or real
#: container. (The role calls the module by its fully-qualified name, so a plain
#: ``library/`` short-name override does not intercept it — a collection-shaped
#: override is required.)
_FIXTURE_COLLECTIONS = _FIXTURE_DIR / "collections"

#: The pinned image the role guards on (defaults/main.yml: openbao_image). The
#: stub reports this as the running container's image so the version-check fact
#: computes True. Kept in sync with the role default; asserted below too.
_PINNED_IMAGE = "openbao/openbao:2.4"

#: PLAY RECAP line: "<host> : ok=.. changed=N unreachable=.. failed=.. ..."
_RECAP_RE = re.compile(
    r"^\S+\s*:\s*ok=(?P<ok>\d+)\s+changed=(?P<changed>\d+)\s+"
    r"unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)",
    re.MULTILINE,
)


def _ansible_playbook_binary() -> str | None:
    """Return an ``ansible-playbook`` binary path, or ``None`` if unavailable.

    Same ``shutil.which`` skip idiom the suite uses for the terraform binary
    (conftest.py). Checks ``PATH`` first, then the project venv path explicitly
    (in case a future setup installs Ansible into the venv). Absent binary =>
    the test SKIPS cleanly, never fails.
    """
    on_path = shutil.which("ansible-playbook")
    if on_path:
        return on_path
    venv_candidate = Path.home() / "venv" / "devinfra" / "bin" / "ansible-playbook"
    if venv_candidate.is_file() and os.access(venv_candidate, os.X_OK):
        return str(venv_candidate)
    return None


requires_ansible = pytest.mark.skipif(
    _ansible_playbook_binary() is None,
    reason="requires ansible-playbook, not installed in venv",
)


def _become_available() -> bool:
    """Whether the role's ``become: true`` (root-owned file tasks) can succeed.

    The ``openbao_install`` role sets ``owner: root`` on its config dir / config
    file, so the fixture playbook runs with ``become: true``. That works only if
    we are already root, or passwordless sudo is available. This is a capability
    gate, NOT an infra gate: where it is absent (an unprivileged dev box with no
    passwordless sudo) the test SKIPS cleanly rather than failing on a chown
    permission error. In CI these roles run as root, so become succeeds and the
    test runs for real.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    sudo = shutil.which("sudo")
    if not sudo:
        return False
    try:
        # `sudo -n true` succeeds only with passwordless sudo; never prompts.
        return subprocess.run(
            [sudo, "-n", "true"],
            capture_output=True,
            timeout=10,
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


requires_become = pytest.mark.skipif(
    not _become_available(),
    reason=(
        "requires root or passwordless sudo — the openbao_install role sets "
        "owner:root on its config dir/file, so the fixture runs with become:true "
        "(runs as-is in CI/root; skips cleanly on an unprivileged dev box)"
    ),
)


def _run_playbook(binary: str, *, check_mode: bool, tmp_path: Path) -> subprocess.CompletedProcess:
    """Invoke the fixture playbook once. ``check_mode`` toggles ``--check``.

    Points ``config``/``TLS`` dirs at writable temp dirs so the role never
    touches real system paths, wires the stub's pinned-image env var, and sets
    ``ANSIBLE_ROLES_PATH`` to the repo's ``ansible/roles`` so the real
    ``openbao_install`` role resolves.
    """
    config_dir = tmp_path / "openbao-config"
    tls_dir = tmp_path / "openbao-tls"

    cmd = [
        binary,
        "-i", str(_INVENTORY),
        str(_PLAYBOOK),
        "-e", f"openbao_config_dir={config_dir}",
        "-e", f"openbao_tls_host_dir={tls_dir}",
    ]
    if check_mode:
        cmd.append("--check")

    env = dict(os.environ)
    env["ANSIBLE_ROLES_PATH"] = str(_ANSIBLE_ROLES_DIR)
    # Resolve the scoped fake community.docker collection FIRST so the
    # docker_container_info stub shadows the FQCN the role probes with.
    env["ANSIBLE_COLLECTIONS_PATH"] = str(_FIXTURE_COLLECTIONS)
    # The stub reports this image as the running container's image, driving the
    # version-check fact to True (the "pinned version already running" half).
    env["OPENBAO_STUB_PINNED_IMAGE"] = _PINNED_IMAGE
    # Keep the run hermetic/quiet and deprecation-noise out of the recap parse.
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env["ANSIBLE_LOCALHOST_WARNING"] = "False"
    env["ANSIBLE_RETRY_FILES_ENABLED"] = "False"

    return subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _parse_recap(output: str) -> dict[str, int]:
    """Extract the PLAY RECAP counters from combined stdout+stderr."""
    match = _RECAP_RE.search(output)
    assert match is not None, f"no PLAY RECAP found in ansible output:\n{output}"
    return {k: int(v) for k, v in match.groupdict().items()}


class TestOpenbaoInstallCheckModeIdempotency:
    """`openbao_install` reports changed=0 against an already-configured state
    (Requirement 13.1, 13.2)."""

    def test_role_default_image_pin_matches_fixture(self):
        """Guard: the role's pinned image still equals the value the stub reports.

        Pure-offline sanity check that runs even without Ansible, so a drift in
        the role's `openbao_image` default (which would silently break the
        already-running simulation) is caught as a normal test failure.
        """
        defaults = (
            _ANSIBLE_ROLES_DIR / "openbao_install" / "defaults" / "main.yml"
        ).read_text(encoding="utf-8")
        assert f'openbao_image: "{_PINNED_IMAGE}"' in defaults, (
            "the fixture stub reports "
            f"'{_PINNED_IMAGE}' as the running image to trigger the version-check "
            "skip; keep it in sync with the role's openbao_image default"
        )

    @requires_ansible
    @requires_become
    def test_check_run_against_already_configured_state_is_changed_zero(self, tmp_path):
        """A `--check` run against the already-configured state reports changed=0.

        First a NORMAL run primes the state (renders config.hcl, creates dirs);
        then a `--check` run must be a no-op — the version-check fact skips the
        bring-up and the already-rendered config makes the template tasks no-ops.
        """
        binary = _ansible_playbook_binary()
        assert binary is not None  # guaranteed by the skipif gate

        # Phase 1 — prime the already-configured state (normal, not --check).
        prime = _run_playbook(binary, check_mode=False, tmp_path=tmp_path)
        assert prime.returncode == 0, (
            "priming run failed:\n"
            f"STDOUT:\n{prime.stdout}\nSTDERR:\n{prime.stderr}"
        )
        prime_recap = _parse_recap(prime.stdout + prime.stderr)
        assert prime_recap["failed"] == 0 and prime_recap["unreachable"] == 0, (
            f"priming run had failures: {prime_recap}\n{prime.stdout}\n{prime.stderr}"
        )

        # Phase 2 — the assertion: a --check run is now a no-op.
        check = _run_playbook(binary, check_mode=True, tmp_path=tmp_path)
        assert check.returncode == 0, (
            "check run failed:\n"
            f"STDOUT:\n{check.stdout}\nSTDERR:\n{check.stderr}"
        )
        recap = _parse_recap(check.stdout + check.stderr)
        assert recap["failed"] == 0 and recap["unreachable"] == 0, (
            f"check run had failures/unreachable: {recap}\n{check.stdout}\n{check.stderr}"
        )
        assert recap["changed"] == 0, (
            "openbao_install must report changed=0 in --check against an "
            f"already-configured state (Req 13.1/13.2), got changed={recap['changed']}.\n"
            f"STDOUT:\n{check.stdout}\nSTDERR:\n{check.stderr}"
        )
