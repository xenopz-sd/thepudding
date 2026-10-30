"""Offline structural guards for the composed-onboarder primary-delegation bugfix.

Feature: fix-composed-openbao-onboarder-primary-delegation.

The two SVC-07 day-2 onboarder roles — ``openbao_project_onboard`` (isolation
triple) and ``openbao_workload_approle`` (Workload_AppRole) — perform every
OpenBao operation through ``docker exec <primary container> bao ...`` (plus one
``docker cp`` in ``openbao_project_onboard``). They were authored for the
STANDALONE path ``ansible/playbooks/openbao.yml`` Play 2 (``hosts: openbao``
``become: true``), which already runs ON the primary guest — so an undelegated
``docker exec`` hits the local primary container. When ``consumer_bootstrap``
composes those same roles on ``hosts: localhost``, the undelegated exec runs on
the control node (no such container) and fails ``No such container``.

The fix adds a per-role delegate-host var + a ``delegate_to`` TERNARY to every
docker task (and, in ``openbao_project_onboard``, the policy render + the
``/tmp/<policy>.hcl`` byte-compare slurp). The ternary yields the primary when
the delegate host is set, else ``inventory_hostname`` (run-in-place), so the
empty default is byte-for-byte the current standalone behaviour.

These tests are pure-logic (PyYAML parse of the committed role task files) — they
need no infrastructure. They are the mergeable Tier-1 gate for a change that
touches every docker-handshake task in two shared roles.

Two layers (bugfix.md Derived Properties):

* **Exploration / Fix-Checking (Property 1):** EVERY ``docker exec`` / ``docker
  cp`` task — plus the ``openbao_project_onboard`` policy render + ``/tmp`` slurp
  — carries the delegate-host ternary with an ``inventory_hostname`` else-branch;
  and ``consumer_bootstrap`` STEP 2/3 pass the delegate host var. On UNFIXED code
  these FAIL (no task carries ``delegate_to``) — that is the structural
  counterexample proving the bug.

* **Preservation (Property 2):** the ``projects.yaml`` slurp keeps
  ``delegate_to: localhost`` / ``become: false``; token tasks keep ``no_log``;
  the ``-tls-skip-verify`` splice is present; ``openbao_workload_approle``'s STEP
  8 file/template handoff + ``uri`` GitLab PUTs are NOT delegated. These PASS on
  UNFIXED code (they describe the baseline to preserve) and MUST keep passing
  after the fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/installer/test_composed_onboarder_delegation.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROJECT_ONBOARD_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_project_onboard" / "tasks" / "main.yml"
)
_PROJECT_ONBOARD_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_project_onboard" / "defaults" / "main.yml"
)
_WORKLOAD_APPROLE_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_workload_approle" / "tasks" / "main.yml"
)
_WORKLOAD_APPROLE_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_workload_approle" / "defaults" / "main.yml"
)
_CONSUMER_BOOTSTRAP_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "tasks" / "main.yml"
)

# Per-role delegate-host var names (bugfix.md / design.md "Delegate host var").
_ONBOARD_DELEGATE_VAR = "openbao_onboard_delegate_host"
_WORKLOAD_DELEGATE_VAR = "openbao_workload_delegate_host"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path):
    """Return the flat ordered task list of a role ``tasks/main.yml``."""
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _command_argv_text(task: dict) -> str | None:
    """Return the flat argv string of a command/shell task, else None.

    Covers ``argv`` as a YAML list and as a folded-scalar Jinja ``{{ [...] }}``
    expression. Returns None for non-command tasks.
    """
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if spec is None:
            continue
        if isinstance(spec, str):
            return spec
        if isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                return " ".join(str(a) for a in argv)
            if isinstance(argv, str):
                return argv
    return None


def _is_docker_exec_or_cp(task: dict) -> bool:
    """True iff the task is a command whose argv begins with docker exec/cp.

    The argv is a Jinja ``>-`` folded string like ``{{ ['docker', 'exec', ...] }}``
    (or a plain YAML list for the ``docker cp``), so match on the presence of the
    ``'docker', 'exec'`` / ``'docker', 'cp'`` (or bare ``docker exec`` / ``docker
    cp``) token pair near the start of the argv string.
    """
    argv = _command_argv_text(task)
    if argv is None:
        return False
    # Normalise quoting so both the folded-list form ("'docker', 'exec'") and the
    # plain-list form ("docker cp") are matched.
    flat = argv.replace("'", "").replace('"', "").replace(",", " ")
    flat = " ".join(flat.split())
    return flat.startswith("{{ docker exec") or flat.startswith("docker exec") \
        or flat.startswith("{{ docker cp") or flat.startswith("docker cp") \
        or "docker exec" in flat[:40] or "docker cp" in flat[:40]


def _delegate_to_text(task: dict) -> str:
    return str(task.get("delegate_to", ""))


def _carries_delegate_ternary(task: dict, delegate_var: str) -> bool:
    """True iff the task's delegate_to is the delegate-host ternary with an
    ``inventory_hostname`` else-branch."""
    dt = _delegate_to_text(task)
    return delegate_var in dt and "inventory_hostname" in dt


def _vars_block_text(task: dict) -> str:
    return str(task.get("vars", ""))


# --------------------------------------------------------------------------- #
# Enumerated docker task NAMES per the design (for precise, readable failures).
# --------------------------------------------------------------------------- #
# openbao_project_onboard: 11 tasks (9 docker exec/cp + policy render template +
# /tmp slurp). We match docker exec/cp structurally; the render+slurp are matched
# by their module + dest/src pointing at /tmp/<policy>.hcl.
_POLICY_TMP_MARKER = "openbao_project_policy_name"


def _is_policy_render(task: dict) -> bool:
    """The ansible.builtin.template rendering /tmp/<policy>.hcl."""
    spec = task.get("ansible.builtin.template") or task.get("template")
    if not isinstance(spec, dict):
        return False
    dest = str(spec.get("dest", ""))
    return "/tmp/" in dest and _POLICY_TMP_MARKER in dest


def _is_policy_tmp_slurp(task: dict) -> bool:
    """The ansible.builtin.slurp of /tmp/<policy>.hcl (byte-compare)."""
    spec = task.get("ansible.builtin.slurp") or task.get("slurp")
    if not isinstance(spec, dict):
        return False
    src = str(spec.get("src", ""))
    return "/tmp/" in src and _POLICY_TMP_MARKER in src


def _is_projects_yaml_slurp(task: dict) -> bool:
    """The projects.yaml READ-ONLY validation slurp (control-node repo artifact)."""
    spec = task.get("ansible.builtin.slurp") or task.get("slurp")
    if not isinstance(spec, dict):
        return False
    return "openbao_projects_yaml_path" in str(spec.get("src", ""))


# --------------------------------------------------------------------------- #
# EXPLORATION / FIX-CHECKING (Property 1) — FAILS on unfixed code.
# Validates: Requirements 2.1, 2.2, 2.3, 2.4
# --------------------------------------------------------------------------- #
def test_project_onboard_docker_tasks_carry_delegate_ternary():
    """Property 1 (Validates: Req 2.1, 2.2).

    Every ``docker exec`` / ``docker cp`` task in openbao_project_onboard carries
    the ``openbao_onboard_delegate_host`` ternary with an ``inventory_hostname``
    else-branch. FAILS on unfixed code (no delegate_to) — the counterexample.
    """
    tasks = _load_role_tasks(_PROJECT_ONBOARD_TASKS)
    docker_tasks = [t for t in tasks if _is_docker_exec_or_cp(t)]
    assert docker_tasks, "No docker exec/cp tasks found in openbao_project_onboard — parse error."
    offenders = [
        t.get("name", "<unnamed>")
        for t in docker_tasks
        if not _carries_delegate_ternary(t, _ONBOARD_DELEGATE_VAR)
    ]
    assert not offenders, (
        "openbao_project_onboard docker exec/cp tasks missing the "
        f"{_ONBOARD_DELEGATE_VAR} delegate ternary (so they run on the composing "
        "host and fail 'No such container' under composition on localhost):\n  "
        + "\n  ".join(offenders)
    )


def test_project_onboard_policy_render_and_slurp_carry_delegate_ternary():
    """Property 1, option (a) coherence (Validates: Req 2.2).

    The policy render (template -> /tmp/<policy>.hcl) and the /tmp byte-compare
    slurp must carry the same delegate ternary so the render/slurp/cp are
    coherent on one host. FAILS on unfixed code.
    """
    tasks = _load_role_tasks(_PROJECT_ONBOARD_TASKS)
    render = [t for t in tasks if _is_policy_render(t)]
    tmp_slurp = [t for t in tasks if _is_policy_tmp_slurp(t)]
    assert render, "Policy render (template to /tmp/<policy>.hcl) not found — parse error."
    assert tmp_slurp, "Policy /tmp byte-compare slurp not found — parse error."
    for t in render + tmp_slurp:
        assert _carries_delegate_ternary(t, _ONBOARD_DELEGATE_VAR), (
            f"Task {t.get('name')!r} (policy render/slurp) must carry the "
            f"{_ONBOARD_DELEGATE_VAR} delegate ternary (option (a) coherence, Req 2.2)."
        )


def test_workload_approle_docker_tasks_carry_delegate_ternary():
    """Property 1 (Validates: Req 2.3).

    Every ``docker exec`` task in openbao_workload_approle carries the
    ``openbao_workload_delegate_host`` ternary with an ``inventory_hostname``
    else-branch. FAILS on unfixed code.
    """
    tasks = _load_role_tasks(_WORKLOAD_APPROLE_TASKS)
    docker_tasks = [t for t in tasks if _is_docker_exec_or_cp(t)]
    assert docker_tasks, "No docker exec tasks found in openbao_workload_approle — parse error."
    offenders = [
        t.get("name", "<unnamed>")
        for t in docker_tasks
        if not _carries_delegate_ternary(t, _WORKLOAD_DELEGATE_VAR)
    ]
    assert not offenders, (
        "openbao_workload_approle docker exec tasks missing the "
        f"{_WORKLOAD_DELEGATE_VAR} delegate ternary:\n  " + "\n  ".join(offenders)
    )


def test_consumer_bootstrap_step2_passes_onboard_delegate_host():
    """Property 1 (Validates: Req 2.4).

    consumer_bootstrap STEP 2 (include_role openbao_project_onboard) passes
    ``openbao_onboard_delegate_host`` set to the primary host. FAILS on unfixed.
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    step2 = None
    for t in tasks:
        inc = t.get("ansible.builtin.include_role") or t.get("include_role")
        if isinstance(inc, dict) and "openbao_project_onboard" in str(inc.get("name", "")):
            step2 = t
    assert step2 is not None, "STEP 2 include_role openbao_project_onboard not found."
    vars_text = _vars_block_text(step2)
    assert _ONBOARD_DELEGATE_VAR in vars_text, (
        f"consumer_bootstrap STEP 2 must pass {_ONBOARD_DELEGATE_VAR} so the "
        "composed openbao_project_onboard delegates to the primary (Req 2.4)."
    )
    assert "consumer_openbao_primary_host" in vars_text, (
        "STEP 2 should set the delegate host to consumer_openbao_primary_host."
    )


def test_consumer_bootstrap_step3_passes_workload_delegate_host():
    """Property 1 (Validates: Req 2.4).

    consumer_bootstrap STEP 3 (include_role openbao_workload_approle) passes
    ``openbao_workload_delegate_host`` set to the primary host. FAILS on unfixed.
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    step3 = None
    for t in tasks:
        inc = t.get("ansible.builtin.include_role") or t.get("include_role")
        if isinstance(inc, dict) and "openbao_workload_approle" in str(inc.get("name", "")):
            step3 = t
    assert step3 is not None, "STEP 3 include_role openbao_workload_approle not found."
    vars_text = _vars_block_text(step3)
    assert _WORKLOAD_DELEGATE_VAR in vars_text, (
        f"consumer_bootstrap STEP 3 must pass {_WORKLOAD_DELEGATE_VAR} so the "
        "composed openbao_workload_approle delegates to the primary (Req 2.4)."
    )
    assert "consumer_openbao_primary_host" in vars_text, (
        "STEP 3 should set the delegate host to consumer_openbao_primary_host."
    )


def test_defaults_define_onboard_delegate_vars():
    """Property 1 (Validates: Req 2.1).

    openbao_project_onboard defaults define the empty-string delegate-host +
    root/python connection defaults. FAILS on unfixed.
    """
    data = yaml.safe_load(_PROJECT_ONBOARD_DEFAULTS.read_text(encoding="utf-8"))
    assert _ONBOARD_DELEGATE_VAR in data, f"Missing {_ONBOARD_DELEGATE_VAR} default."
    assert str(data[_ONBOARD_DELEGATE_VAR]) == "", (
        f"{_ONBOARD_DELEGATE_VAR} must default to empty string (run-in-place)."
    )
    assert str(data.get("openbao_onboard_delegate_user")) == "root"
    assert str(data.get("openbao_onboard_delegate_python")) == "/usr/bin/python3"


def test_defaults_define_workload_delegate_vars():
    """Property 1 (Validates: Req 2.3).

    openbao_workload_approle defaults define the empty-string delegate-host +
    root/python connection defaults. FAILS on unfixed.
    """
    data = yaml.safe_load(_WORKLOAD_APPROLE_DEFAULTS.read_text(encoding="utf-8"))
    assert _WORKLOAD_DELEGATE_VAR in data, f"Missing {_WORKLOAD_DELEGATE_VAR} default."
    assert str(data[_WORKLOAD_DELEGATE_VAR]) == "", (
        f"{_WORKLOAD_DELEGATE_VAR} must default to empty string (run-in-place)."
    )
    assert str(data.get("openbao_workload_delegate_user")) == "root"
    assert str(data.get("openbao_workload_delegate_python")) == "/usr/bin/python3"


def test_consumer_bootstrap_defaults_define_primary_host():
    """Property 1 (Validates: Req 2.4).

    consumer_bootstrap defaults define consumer_openbao_primary_host = 10.0.20.10
    (identical to STEP 1's literal). FAILS on unfixed.
    """
    defaults = (
        _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "defaults" / "main.yml"
    )
    data = yaml.safe_load(defaults.read_text(encoding="utf-8"))
    assert "consumer_openbao_primary_host" in data, (
        "consumer_bootstrap defaults must define consumer_openbao_primary_host."
    )
    assert str(data["consumer_openbao_primary_host"]) == "10.0.20.10", (
        "consumer_openbao_primary_host must equal STEP 1's existing literal 10.0.20.10."
    )


# --------------------------------------------------------------------------- #
# PRESERVATION (Property 2) — PASS on unfixed AND after fix.
# Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6, 3.7
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "tasks_path",
    [_PROJECT_ONBOARD_TASKS, _WORKLOAD_APPROLE_TASKS],
    ids=["project_onboard", "workload_approle"],
)
def test_projects_yaml_slurp_keeps_localhost_delegation(tasks_path):
    """Preservation (Validates: Req 3.6).

    The projects.yaml validation slurp in BOTH roles keeps ``delegate_to:
    localhost`` with ``become: false`` and does NOT acquire the new primary
    ternary (it reads a control-node repo artifact).
    """
    tasks = _load_role_tasks(tasks_path)
    slurps = [t for t in tasks if _is_projects_yaml_slurp(t)]
    assert slurps, f"projects.yaml validation slurp not found in {tasks_path.parent.parent.name}."
    for t in slurps:
        assert _delegate_to_text(t) == "localhost", (
            "projects.yaml slurp must keep delegate_to: localhost (reads a "
            "control-node repo artifact, Req 3.6)."
        )
        assert t.get("become") is False, "projects.yaml slurp must keep become: false."
        # It must NOT have the primary-delegation ternary.
        assert "inventory_hostname" not in _delegate_to_text(t), (
            "projects.yaml slurp must NOT acquire the new primary-delegation ternary."
        )


@pytest.mark.parametrize(
    "tasks_path,delegate_var",
    [
        (_PROJECT_ONBOARD_TASKS, _ONBOARD_DELEGATE_VAR),
        (_WORKLOAD_APPROLE_TASKS, _WORKLOAD_DELEGATE_VAR),
    ],
    ids=["project_onboard", "workload_approle"],
)
def test_token_bearing_docker_tasks_keep_no_log(tasks_path, delegate_var):
    """Preservation (Validates: Req 3.2, Property 3).

    Every ``docker exec`` task whose argv carries ``BAO_TOKEN=`` retains
    ``no_log: "{{ openbao_no_log }}"``; the token stays in the exec environment
    (``-e BAO_TOKEN=...``), never on a bare positional argv.
    """
    tasks = _load_role_tasks(tasks_path)
    token_tasks = [
        t for t in tasks
        if _is_docker_exec_or_cp(t) and "BAO_TOKEN=" in (_command_argv_text(t) or "")
    ]
    assert token_tasks, f"No BAO_TOKEN-bearing docker tasks found in {tasks_path}."
    for t in token_tasks:
        assert str(t.get("no_log")) == "{{ openbao_no_log }}", (
            f"Token-bearing task {t.get('name')!r} must keep "
            'no_log: "{{ openbao_no_log }}" (Req 3.2).'
        )
        argv = _command_argv_text(t) or ""
        # The token must ride in the -e BAO_TOKEN=... env, never as a standalone
        # token positional (we assert the -e form is present).
        assert "BAO_TOKEN=" in argv and "-e" in argv, (
            f"Token-bearing task {t.get('name')!r} must carry the token in the "
            "-e BAO_TOKEN=... exec environment (never on a bare argv)."
        )


@pytest.mark.parametrize(
    "tasks_path",
    [_PROJECT_ONBOARD_TASKS, _WORKLOAD_APPROLE_TASKS],
    ids=["project_onboard", "workload_approle"],
)
def test_tls_skip_verify_splice_present(tasks_path):
    """Preservation (Validates: Req 3.3).

    The ``-tls-skip-verify`` conditional splice, gated on
    ``openbao_primary_tls_skip_verify``, remains present in the role's bao
    docker-exec tasks.
    """
    text = tasks_path.read_text(encoding="utf-8")
    assert "-tls-skip-verify" in text, (
        f"{tasks_path} lost the -tls-skip-verify splice (Req 3.3)."
    )
    assert "openbao_primary_tls_skip_verify" in text, (
        f"{tasks_path} lost the openbao_primary_tls_skip_verify gate (Req 3.3)."
    )


def test_project_onboard_admin_token_source_and_fail_closed():
    """Preservation (Validates: Req 3.5).

    The platform-admin token is read from OPENBAO_ADMIN_TOKEN (env var name in
    defaults) and the role fails closed when absent (an assert on its length).
    """
    defaults = yaml.safe_load(_PROJECT_ONBOARD_DEFAULTS.read_text(encoding="utf-8"))
    assert defaults.get("openbao_admin_token_env_var") == "OPENBAO_ADMIN_TOKEN"
    text = _PROJECT_ONBOARD_TASKS.read_text(encoding="utf-8")
    assert "openbao_admin_token | length > 0" in text, (
        "openbao_project_onboard must keep its fail-closed admin-token assert (Req 3.5)."
    )


def test_workload_approle_handoff_and_gitlab_not_delegated():
    """Preservation (Validates: Req 3.1, 3.7).

    openbao_workload_approle's STEP 8 ``file``/``template`` handoff tasks and the
    ``uri`` GitLab PUTs are NOT delegated (they target the control node / GitLab,
    not the primary) — they must NOT acquire the primary-delegation ternary.
    """
    tasks = _load_role_tasks(_WORKLOAD_APPROLE_TASKS)
    checked_file_or_template = 0
    checked_uri = 0
    for t in tasks:
        mods = _module_names(t)
        if {"ansible.builtin.file", "ansible.builtin.template"} & mods:
            # Only the handoff tasks (creds dir / role-id / secret-id files).
            spec = t.get("ansible.builtin.file") or t.get("ansible.builtin.template") or {}
            if "openbao_workload" in str(spec):
                checked_file_or_template += 1
                assert _WORKLOAD_DELEGATE_VAR not in _delegate_to_text(t), (
                    f"Handoff task {t.get('name')!r} must NOT be delegated to the "
                    "primary (it writes credential files on the run host, Req 3.1)."
                )
        if "ansible.builtin.uri" in mods:
            checked_uri += 1
            assert _WORKLOAD_DELEGATE_VAR not in _delegate_to_text(t), (
                f"GitLab PUT task {t.get('name')!r} must NOT be delegated to the "
                "primary (it targets the GitLab API, Req 3.1)."
            )
    assert checked_file_or_template >= 2, (
        "Expected the STEP 8 file/template handoff tasks (creds dir + role-id + "
        "secret-id); parse found too few."
    )
    assert checked_uri >= 2, "Expected the two GitLab uri PUT tasks; parse found too few."


def test_workload_approle_secret_id_fail_closed_and_scrub_preserved():
    """Preservation (Validates: Req 3.7).

    The Secret_ID mint stays fail-closed (``failed_when`` on non-zero rc) and the
    STEP 9 scrub of the Secret_ID + admin-token facts is preserved.
    """
    text = _WORKLOAD_APPROLE_TASKS.read_text(encoding="utf-8")
    assert "openbao_workload_secret_id_mint.rc != 0" in text, (
        "Secret_ID mint must keep its fail-closed failed_when (Req 3.7)."
    )
    assert "openbao_workload_secret_id: \"\"" in text, (
        "STEP 9 must keep scrubbing the Secret_ID fact (Req 3.7)."
    )
    assert "openbao_admin_token: \"\"" in text, (
        "STEP 9 must keep scrubbing the admin-token fact (Req 3.7)."
    )
