"""Ansible check-mode idempotency test for the ``openbao_init_unseal`` role.

Task 6.3 (spec: svc-07-secrets-manager) — requirements.md Requirement 13.1/13.3
("the init/unseal role skips when the instance already reports
``initialized: true``; a re-run reports zero changed tasks, revokes no token and
deletes no secret"). design.md "Testing Strategy §1 (Ansible check-mode
idempotency)".

WHAT THIS TEST PROVES
---------------------
Run the REAL, unmodified ``openbao_init_unseal`` role in ``--check`` mode against
a fixture "already-initialized" state and assert the play recap reports
``changed=0`` — the role's ``bao status`` / health guard detects
``initialized: true`` and gates OFF every init task
(``when: not openbao_already_initialized``). Because the token-revoke and any
secret-write/delete steps live behind that SAME gate (the unsealer's Transit
enable/key-create today, and the Task 6.2 primary Raft-init / Root_Token
bootstrap+revoke tomorrow), ``changed=0`` is exactly the "init skipped, no token
revoked, no secret deleted" guarantee the task requires. The assertion is on the
WHOLE role recap, so it stays correct however the role grows at its 6.2/7.x
extension points.

A SINGLE ``--check`` RUN SUFFICES (post ADR-0006)
-------------------------------------------------
This test previously used a "prime-then-check" TWO-run shape whose sole stated
justification was that the role's ``vm.swappiness`` sysctl task
(``ansible.posix.sysctl`` with ``state: present``) manages a persistent
``/etc/sysctl.conf`` entry and so reports ``changed`` the FIRST time that entry
is written — a first NORMAL run was needed to prime that entry before a
``--check`` run could be a clean no-op. Per ADR-0006 that host-global
``vm.swappiness`` write has been REMOVED from ``openbao_init_unseal`` (it was
unrunnable on an unprivileged LXC, cross-tenant, and only a heuristic; the swap
control now lives in encrypted host swap + the per-container cap). With the
sysctl task gone, the role's remaining tasks for the fixture's ``unsealer`` role
are all no-ops under ``--check`` against an already-initialized stub:

  * the ``bao status`` / health guard (``check_mode: false``,
    ``changed_when: false``) is a read that never reports a change;
  * every unsealer init task (Transit enable / key create) is gated
    ``when: not openbao_already_initialized`` and is skipped once the stub
    drives that fact True;
  * the primary Raft-init / bootstrap include is gated ``openbao_role ==
    "primary"`` and the observability (task 10.1) + alerts (task 10.2) template
    renders are ALSO gated ``openbao_role == "primary"`` — the fixture runs the
    ``unsealer`` role, so none of them runs.

Nothing left in the role's ``unsealer`` path writes persistent state, so a
SINGLE ``--check`` run against the already-initialized stub is already a genuine
no-op reporting ``changed=0``. The two-run priming shape no longer reflects the
role and has been dropped in favour of that single assertion — cleaner, and it
now genuinely mirrors what the role does.

HOW THE "ALREADY-INITIALIZED" STATE IS SIMULATED
------------------------------------------------
The guard reads ``bao status -format=json`` via ``docker exec`` inside the target
container (NOT a host ``uri`` HTTP read — the primary publishes no host port, so
nothing listens on its ``:8200`` from the LXC host, and the old ``uri`` guard
wrongly derived ``openbao_already_initialized = false`` on an already-initialised
primary; see the 23rd post-implementation correction). Because ``docker exec``
needs a real container, the guard's derived state cannot be faked with an HTTP
stub. Instead this runner drives the guard's OUTPUT fact directly via ``-e``
(highest Ansible var precedence):

  * ``openbao_already_initialized`` -> ``true``

so every init task's ``when: not openbao_already_initialized`` gate evaluates
False and the whole init/unseal path is skipped — exactly the guarantee under
test. This exercises the real gating (the ``when`` conditions on the actual role
tasks), independent of how the guard derives the fact, so it stays correct as the
guard's state-source mechanism evolves.

(ADR-0006 removed the former ``vm.swappiness`` sysctl task from this role, so the
old prime-then-``--check`` dance and the ``openbao_vm_swappiness`` override are no
longer needed — a single ``--check`` run against the gated-off init path is a
genuine no-op.) The docker-exec status guard + Transit-init tasks are gated off by
``not openbao_already_initialized`` and never run, so no Docker daemon is needed.

The fixture drives the ``unsealer`` role so NO secret value (the primary's seal
``transit`` token) is ever needed or templated (no-secret-in-fixtures steering).

GATING (offline-by-construction, absent tooling => skip, never fail)
--------------------------------------------------------------------
Mirrors task 5.5 (``test_svc07_openbao_install_idempotency.py``):

  * ``requires_ansible`` — ``skipif`` on the presence of an ``ansible-playbook``
    binary (``PATH`` first, then the venv path). Absent binary => SKIP cleanly.
  * ``requires_become`` — the role still needs root, but NOT for swappiness (that
    is gone): its observability (task 10.1) and alerts (task 10.2) template
    renders write root-owned files/dirs under ``/etc/openbao/...``
    (``owner: root``), and the unsealer Transit-init runs via ``docker exec``.
    The fixture therefore runs with ``become: true``; this gate ``skipif``s when
    we are neither root nor have passwordless sudo (skips cleanly on an
    unprivileged dev box; runs for real in CI/root).

It is deliberately NOT ``requires_infra``: it needs no live Proxmox/OpenBao, only
an ``ansible-playbook`` binary and a local TCP port for the stub.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_init_unseal_idempotency.py -v
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
_FIXTURE_DIR = _THIS_DIR / "fixtures" / "svc07_openbao_init_unseal_idempotency"
_PLAYBOOK = _FIXTURE_DIR / "already_initialized.yml"
_INVENTORY = _FIXTURE_DIR / "inventory.ini"

#: PLAY RECAP line: "<host> : ok=.. changed=N unreachable=.. failed=.. ..."
_RECAP_RE = re.compile(
    r"^\S+\s*:\s*ok=(?P<ok>\d+)\s+changed=(?P<changed>\d+)\s+"
    r"unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)",
    re.MULTILINE,
)


def _ansible_playbook_binary() -> str | None:
    """Return an ``ansible-playbook`` binary path, or ``None`` if unavailable.

    Same ``shutil.which`` skip idiom the suite uses for the terraform binary
    (conftest.py) and task 5.5. Checks ``PATH`` first, then the project venv path
    explicitly. Absent binary => the test SKIPS cleanly, never fails.
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
    """Whether the role's ``become: true`` can succeed (root / passwordless sudo).

    The ``openbao_init_unseal`` role still needs root — but NOT for swappiness
    (that host-global ``vm.swappiness`` write was removed per ADR-0006). It needs
    it because the observability (task 10.1) and alerts (task 10.2) template
    renders create root-owned files/directories under ``/etc/openbao/...``
    (``owner: root``), and the unsealer Transit-init runs via ``docker exec``.
    The fixture therefore runs with ``become: true``. That works only if we are
    already root, or passwordless sudo is available. This is a capability gate,
    NOT an infra gate: where it is absent (an unprivileged dev box with no
    passwordless sudo) the test SKIPS cleanly rather than failing on a
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
        "requires root or passwordless sudo — the openbao_init_unseal role "
        "renders root-owned files under /etc/openbao/... (owner: root) in its "
        "observability/alerts template tasks and runs the unsealer Transit init "
        "via docker exec, so the fixture runs with become:true (runs as-is in "
        "CI/root; skips cleanly on an unprivileged dev box)"
    ),
)




def _run_playbook(
    binary: str, *, check_mode: bool
) -> subprocess.CompletedProcess:
    """Invoke the fixture playbook once. ``check_mode`` toggles ``--check``.

    Overrides (``-e``, highest precedence):
      * ``openbao_already_initialized``   -> true (drive the guard's OUTPUT fact
                                             directly; the docker-exec status
                                             guard cannot be HTTP-stubbed)
    and sets ``ANSIBLE_ROLES_PATH`` so the real ``openbao_init_unseal`` role
    resolves.
    """
    cmd = [
        binary,
        "-i", str(_INVENTORY),
        str(_PLAYBOOK),
        "-e", "openbao_already_initialized=true",
    ]
    if check_mode:
        cmd.append("--check")

    env = dict(os.environ)
    env["ANSIBLE_ROLES_PATH"] = str(_ANSIBLE_ROLES_DIR)
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


_MAIN_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal" / "tasks" / "main.yml"
)

#: A real `bao status -format=json` body from a healthy transit-sealed primary
#: (trimmed) — the case the 24th correction fixes.
_HEALTHY_STATUS_JSON = (
    '{\n  "type": "transit",\n  "initialized": true,\n  "sealed": false,\n'
    '  "cluster_id": "ad8e0813"\n}'
)


class TestInitGuardDerivation:
    """The init guard must derive `already_initialized=True` from real `bao status`
    JSON. Regression guard for the 24th correction: the guard read valid JSON but
    a `regex_search('^\\s*[{[]')` inside a YAML `>-` FOLDED scalar silently never
    matched (the backslash-escape is mangled by folding), so the guard derived
    `initialized=unknown` and re-ran init on an already-initialised primary.
    """

    def _eval_guard(self, status_stdout: str) -> dict:
        """Evaluate the guard's derive expressions against a given `bao status`
        stdout, using jinja2 with Ansible's `from_json`/`first` semantics."""
        import json as _json
        from jinja2 import Environment

        env = Environment()
        env.filters["from_json"] = _json.loads
        env.filters["first"] = lambda seq: seq[0] if seq else ""
        # Mirror the role's two derive expressions verbatim (behaviour, not text).
        is_json = env.from_string(
            "{{ (s | trim | length > 0) and ((s | trim | first) in ['{', '[']) }}"
        ).render(s=status_stdout).strip() == "True"
        already = False
        if is_json:
            already = env.from_string(
                "{{ (s | from_json).initialized | default(false) }}"
            ).render(s=status_stdout).strip() == "True"
        return {"is_json": is_json, "already_initialized": already}

    def test_healthy_json_derives_already_initialized_true(self):
        """Valid `bao status` JSON with initialized:true -> guard skips init."""
        r = self._eval_guard(_HEALTHY_STATUS_JSON)
        assert r["is_json"] is True
        assert r["already_initialized"] is True, (
            "the guard must derive already_initialized=True from a healthy "
            "`bao status` JSON body (initialized:true) so init is SKIPPED — "
            "the folded-scalar regex bug mis-derived this as unknown"
        )

    def test_uninitialised_and_down_states_proceed(self):
        """initialized:false, empty, and non-JSON stdout all -> proceed (no crash)."""
        assert self._eval_guard('{"initialized": false}')["already_initialized"] is False
        assert self._eval_guard("")["already_initialized"] is False
        assert self._eval_guard("Error checking seal status")["already_initialized"] is False

    def test_guard_does_not_regex_match_in_a_folded_scalar(self):
        """The guard's derive/report must not reintroduce the folded-scalar regex.

        A `regex_search('^\\s*[{[]')` inside a `>-` folded `set_fact`/`debug`
        scalar does not match valid JSON (backslash mangled by YAML folding).
        Pin that the guard derives its JSON-ness without that footgun.
        """
        text = _MAIN_TASKS.read_text(encoding="utf-8")
        # Locate the guard derive/report region.
        start = text.index("Guard — derive openbao_already_initialized")
        end = text.index("report the init decision") + 200
        region = text[start:end]
        assert "regex_search" not in region, (
            "the init guard must not use regex_search in its folded-scalar derive/"
            "report expressions — it silently fails to match valid JSON; test the "
            "first JSON char + from_json instead (24th correction)"
        )
        assert "openbao_status_is_json" in region


class TestOpenbaoInitUnsealCheckModeIdempotency:
    """`openbao_init_unseal` reports changed=0 against an already-initialized
    state (Requirement 13.1, 13.3)."""

    def test_primary_raft_init_gated_on_already_initialized(self):
        """The PRIMARY Raft-init path stays gated `not openbao_already_initialized`.

        Pure-offline sanity check (runs without Ansible/become). The primary's
        one-time `bao operator init` + bootstrap + revoke MUST remain gated off on
        an already-initialised primary (Req 13.3) — re-running it would fail on an
        already-initialised node. This guards against a refactor dropping that gate.
        """
        tasks = (
            _ANSIBLE_ROLES_DIR / "openbao_init_unseal" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        assert "openbao_already_initialized" in tasks, (
            "the role must derive `openbao_already_initialized` from the status guard"
        )
        # The primary bootstrap include must be gated `not openbao_already_initialized`.
        assert 'openbao_role == "primary"' in tasks and "not openbao_already_initialized" in tasks, (
            "the primary Raft-init/bootstrap path must stay gated "
            "`when: openbao_role == 'primary' and not openbao_already_initialized` (Req 13.3)"
        )

    def test_unsealer_bootstrap_delegated_and_gated_on_already_initialized(self):
        """The UNSEALER bootstrap is delegated to `unsealer_bootstrap.yml`, gated.

        Updated for spec `svc07-unsealer-bootstrap-delegation` (Property 4). The
        role now OWNS the unsealer's OWN `bao operator init` (previously the
        orchestrator hand-rolled it and the role only layered Transit on top,
        assuming an already-initialised unsealer — the 25th correction's premise).

        Because the role now performs the unsealer's `operator init` itself, the
        WHOLE unsealer bootstrap — init + unseal + Transit + seal-policy + mint +
        revoke — is CORRECTLY gated on `not openbao_already_initialized`: a fresh
        unsealer runs the full sequence; an already-initialised one skips it (the
        operator holds the Shamir keys offline). This is the exact inverse of the
        old "Transit must NOT be gated" regression guard, which was predicated on
        the operator having already run `operator init` manually. Idempotency
        within the delegated file is still belt-and-braces (tolerant probe;
        enable-if-mount-absent; key create-if-absent) so a re-run that DOES reach
        it stays a clean no-op.
        """
        import yaml as _yaml
        role_dir = _ANSIBLE_ROLES_DIR / "openbao_init_unseal"
        main_tasks = _yaml.safe_load(
            (role_dir / "tasks" / "main.yml").read_text(encoding="utf-8")
        )

        # (1) main.yml must include unsealer_bootstrap.yml, gated on role==unsealer
        #     AND not openbao_already_initialized.
        include = None
        for tk in main_tasks:
            inc = tk.get("ansible.builtin.include_tasks")
            if inc == "unsealer_bootstrap.yml":
                include = tk
                break
        assert include is not None, (
            "tasks/main.yml must delegate the unsealer bootstrap via "
            "`include_tasks: unsealer_bootstrap.yml`"
        )
        when_list = include.get("when")
        when_list = when_list if isinstance(when_list, list) else [when_list]
        when_str = " ".join(str(c) for c in when_list)
        assert any('openbao_role == "unsealer"' in str(c) for c in when_list), (
            "the unsealer_bootstrap include must be gated on role==unsealer"
        )
        assert "not openbao_already_initialized" in when_str, (
            "the unsealer_bootstrap include must be gated on "
            "`not openbao_already_initialized` (Property 4: idempotent re-run is a no-op)"
        )

        # (2) The delegated file must own operator init + unseal + Transit setup,
        #     with per-task idempotency intact.
        unsealer_text = (role_dir / "tasks" / "unsealer_bootstrap.yml").read_text(encoding="utf-8")
        unsealer_tasks = _yaml.safe_load(unsealer_text)

        def _find(name_substr):
            for tk in unsealer_tasks:
                if name_substr in (tk.get("name") or ""):
                    return tk
            raise AssertionError(f"task containing {name_substr!r} not found in unsealer_bootstrap.yml")

        # operator init + threshold unseal are now owned by the role.
        _find("bao operator init")
        _find("bao operator unseal")

        # Transit enable is idempotent (tolerates already-in-use).
        enable = _find("enable the Transit secrets engine")
        assert "already in use" in _yaml.safe_dump(enable), (
            "the enable task must tolerate an already-enabled mount (idempotent)"
        )
        # Transit key create is create-if-absent.
        keytask = _find("create the Transit unseal key")
        assert "-f" in _yaml.safe_dump(keytask.get("ansible.builtin.command", {})), (
            "the key task must be create-if-absent (`bao write -f transit/keys/...`)"
        )

    @requires_ansible
    @requires_become
    def test_check_run_against_already_initialized_state_is_changed_zero(self):
        """A `--check` run against the already-initialized state reports changed=0.

        The fixture drives `openbao_already_initialized = True` (openbao_role:
        unsealer). changed=0 is the "no token revoked, no secret deleted, no drift"
        guarantee (Req 13.1/13.3). Note the corrected model (25th correction): the
        unsealer's Transit tasks are NO LONGER gated off by already_initialized —
        the read-only probe runs (changed_when:false), and the enable/key-create
        run as idempotent reconciles (enable-if-mount-absent / create-if-absent),
        so against an already-configured unsealer they still contribute no change.
        The PRIMARY's one-time Raft init/bootstrap/revoke remains gated off by
        `not already_initialized`. Either way the --check recap is changed=0.

        A SINGLE `--check` run suffices: per ADR-0006 the role no longer writes
        any persistent host state on the `unsealer` path (the `vm.swappiness`
        sysctl task that once forced a prime-then-check two-run shape is gone,
        and the observability/alerts template renders are gated to the `primary`
        role, which this fixture does not exercise). So the one check run is
        already a genuine no-op.
        """
        binary = _ansible_playbook_binary()
        assert binary is not None  # guaranteed by the skipif gate

        # A single --check run against the gated-off init path is a genuine no-op:
        # ADR-0006 removed the vm.swappiness sysctl task (the only step that once
        # forced a prime-then-check two-run shape), and the init/Transit tasks are
        # gated off by `-e openbao_already_initialized=true`.
        check = _run_playbook(binary, check_mode=True)
        assert check.returncode == 0, (
            "check run failed:\n"
            f"STDOUT:\n{check.stdout}\nSTDERR:\n{check.stderr}"
        )
        recap = _parse_recap(check.stdout + check.stderr)
        assert recap["failed"] == 0 and recap["unreachable"] == 0, (
            f"check run had failures/unreachable: {recap}\n{check.stdout}\n{check.stderr}"
        )
        assert recap["changed"] == 0, (
            "openbao_init_unseal must report changed=0 in --check against an "
            "already-initialized state — the primary Raft-init path is gated off, "
            "and the unsealer Transit tasks reconcile idempotently against an "
            f"already-configured unsealer (Req 13.1/13.3), got changed={recap['changed']}.\n"
            f"STDOUT:\n{check.stdout}\nSTDERR:\n{check.stderr}"
        )
