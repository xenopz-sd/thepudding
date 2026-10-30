"""Offline YAML/structure parse checks for the openbao-workload-auth roles.

Feature: openbao-workload-auth (tasks.md Task 6.1).

Matches the offline-verification style the sibling ``openbao_project_onboard``
role documents in its README ("verified offline by PARSING its YAML with PyYAML
... confirming the check-then-write / ``no_log`` / no-secret-literal structure"):
``ansible``/``ansible-lint`` are not installed in the project venv, so the two
new roles are pinned structurally here by a pure PyYAML parse of every YAML file
they ship. No Ansible runtime, no live OpenBao Primary.

WHAT IS ASSERTED (offline, PyYAML ``safe_load_all`` only)
---------------------------------------------------------
For BOTH roles (``openbao_workload_approle`` control-plane,
``openbao-agent`` data-plane):

1. **Every YAML file parses** — ``defaults/main.yml``, ``tasks/main.yml``,
   ``handlers/main.yml``, ``meta/main.yml`` all ``safe_load`` without error, and
   ``tasks/main.yml`` is a top-level LIST of task dicts.

2. **No secret value is defaulted to a literal** — no key in either role's
   ``defaults/main.yml`` whose NAME denotes a token/secret value
   (``admin_token``, ``secret_id``, ``root_token``, ``password``, ...) carries a
   non-empty literal. Env-var-NAME carriers (``*_env_var``) and the ``no_log``
   expression itself are explicitly allowed (they name a var / express a gate,
   they do not hold a secret). Matches Req 1.8 / 7.2 / design "No secret value is
   defaulted in either role".

3. **Every secret-bearing task carries ``no_log: "{{ openbao_no_log }}"``** — any
   task whose command environment / body carries the Admin_Token, a Secret_ID,
   or a minted-credential response sets ``no_log`` to the ``openbao_no_log``
   expression (NOT ``false``, NOT missing). Matches Req 1.7 / 3.4 / 7.2.

4. **The control role is check-then-write** — every MUTATING ``bao`` call
   (``auth enable`` / ``write``) is gated by a ``when:`` guard that depends on a
   prior read's decision fact, OR is one of the sanctioned always-mint calls
   (the Secret_ID mint, by design re-minted each run). This pins the Req
   1.4/2.6 idempotency structure offline; the live ``changed=0`` proof is the
   ``requires-infra`` task 8.2.

Helpers are COPIED locally (the repo's same-named-``conftest`` cross-tree import
ban), adapted from ``test_secret_hygiene_no_log.py``. This file PARSES YAML only;
it never embeds or prints any secret value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/installer/test_workload_auth_role_structure.py -> repo root parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"

_CONTROL_ROLE = "openbao_workload_approle"
_AGENT_ROLE = "openbao-agent"
_BOTH_ROLES = (_CONTROL_ROLE, _AGENT_ROLE)

# The YAML files every role in this feature ships.
_ROLE_YAML_FILES = (
    "defaults/main.yml",
    "tasks/main.yml",
    "handlers/main.yml",
    "meta/main.yml",
)


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally (no cross-tree import; conftest-collision rule).
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_task_file(path: Path):
    """Return the flat ordered list of task dicts from a role tasks/ file."""
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    tasks: list[dict] = []
    for doc in docs:
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def _task_module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_name(task: dict) -> str:
    return str(task.get("name", ""))


def _no_log_value(task: dict):
    """Return the task's ``no_log`` as True / False / None (absent).

    A literal bool returns as-is; a string idiom that isn't a plain false-like
    literal (e.g. ``"{{ openbao_no_log }}"``) is treated as truthy (suppresses at
    runtime). A plain false-like literal is False.
    """
    if "no_log" not in task:
        return None
    val = task["no_log"]
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text in ("false", "no", "0", ""):
        return False
    return True


def _no_log_is_openbao_expr(task: dict) -> bool:
    """True iff ``no_log`` is the ``openbao_no_log`` expression (the posture)."""
    if "no_log" not in task:
        return False
    return "openbao_no_log" in str(task["no_log"])


def _task_body_text(task: dict) -> str:
    """A flat lowercase string of everything the task DOES + how it is named."""
    parts: list[str] = [_task_name(task)]
    env = task.get("environment")
    if env is not None:
        parts.append(str(env))
    for mod in _task_module_names(task):
        spec = task.get(mod)
        parts.append(str(mod))
        if isinstance(spec, dict):
            for key, value in spec.items():
                parts.append(str(key))
                parts.append(str(value))
        else:
            parts.append(str(spec))
    return " ".join(parts).lower()


# Substrings in a task body that mean the task carries a secret VALUE (not just
# an env-var NAME or secret METADATA like a ttl/num_uses/path). The control role
# passes the admin token as ``BAO_TOKEN=<value>`` and extracts/hands-off the
# minted ``secret_id``. Markers are chosen to match the VALUE carriers and NOT
# the ``*_secret_id_ttl`` / ``*_secret_id_num_uses`` / ``*_secret_id_file`` /
# ``*_secret_id_path`` metadata vars (which merely configure, never hold, a
# secret).
_SECRET_VALUE_MARKERS = (
    "bao_token=",                     # admin token spliced into the docker exec env
    "openbao_admin_token",            # the admin token fact (resolve/assert/scrub)
    "openbao_gitlab_api_token",       # the GitLab API token value
    ".data.secret_id",               # extraction of the secret_id from JSON
    "secret_id_mint",                 # the mint response register (carries secret_id)
    "openbao_workload_secret_id ",    # the minted secret_id fact (bare, w/ trailing space)
    "openbao_workload_secret_id:",    # the scrub set_fact (bare, followed by colon)
    "openbao_workload_secret_id }}",  # the fact interpolated in a value
    "openbao_workload_secret_id)",    # the fact in a length() test
    "secret-id.env.j2",               # the secret_id handoff template render
    "secret_id as a gitlab",          # the gitlab secret_id PUT (by name)
    "the secret_id as",               # the gitlab secret_id PUT task name
)


def _handles_secret_value(task: dict) -> bool:
    body = _task_body_text(task)
    return any(marker in body for marker in _SECRET_VALUE_MARKERS)


# --------------------------------------------------------------------------- #
# 1. Every YAML file parses; tasks/main.yml is a top-level list of task dicts.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", _BOTH_ROLES)
@pytest.mark.parametrize("rel", _ROLE_YAML_FILES)
def test_every_role_yaml_file_parses(role: str, rel: str):
    path = _ROLES_DIR / role / rel
    assert path.is_file(), f"{role}/{rel} is missing"
    # safe_load_all consumes every document; a parse error raises here.
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    assert docs, f"{role}/{rel} parsed to no documents"


@pytest.mark.parametrize("role", _BOTH_ROLES)
def test_tasks_main_is_a_list_of_task_dicts(role: str):
    path = _ROLES_DIR / role / "tasks" / "main.yml"
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    top = [d for d in docs if d is not None]
    assert len(top) == 1, f"{role}/tasks/main.yml should be a single document"
    assert isinstance(top[0], list), (
        f"{role}/tasks/main.yml top-level node must be a LIST of tasks"
    )
    assert all(isinstance(t, dict) for t in top[0]), (
        f"{role}/tasks/main.yml must contain only task dicts"
    )


# --------------------------------------------------------------------------- #
# 2. No secret value defaulted to a literal in either role's defaults.
# --------------------------------------------------------------------------- #
# default KEY names whose VALUE, if a non-empty literal, would be a committed
# secret. ``*_env_var`` NAME carriers and the ``no_log`` gate are excluded.
_SECRET_DEFAULT_NAME_SUBSTRINGS = (
    "admin_token",
    "root_token",
    "secret_id",
    "password",
    "api_token",
    "client_secret",
)


@pytest.mark.parametrize("role", _BOTH_ROLES)
def test_no_secret_value_defaulted_to_a_literal(role: str):
    path = _ROLES_DIR / role / "defaults" / "main.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    offenders = []
    # Suffixes that make a secret-named key CONFIG-about-a-secret (a ttl, a
    # num-uses limit, a file/dir PATH, a mount, a flag) rather than a holder of
    # the secret VALUE itself. These may legitimately carry a literal default.
    metadata_suffixes = (
        "_ttl", "_num_uses", "_file", "_path", "_dir", "_after_read",
        "_mappings", "_mount", "_name", "_env_var", "_url", "_id",
    )
    for key, value in data.items():
        name = key.lower()
        if name == "openbao_no_log":
            continue  # expresses the no_log gate — not a secret
        if any(name.endswith(suffix) for suffix in metadata_suffixes):
            continue  # metadata about a secret, not the secret value
        if not any(s in name for s in _SECRET_DEFAULT_NAME_SUBSTRINGS):
            continue
        # A secret-named default is only acceptable if it is empty / absent.
        if value is None:
            continue
        text = str(value).strip()
        if text == "":
            continue
        offenders.append((key, value))
    assert not offenders, (
        f"{role}/defaults/main.yml defaults a secret-named key to a literal "
        f"value (commit a secret / its placeholder): {offenders}. Secret values "
        "must arrive at runtime (admin token from an env var; role_id/secret_id "
        "from the Primary), never be defaulted."
    )


# --------------------------------------------------------------------------- #
# 3. Every secret-bearing task carries no_log: "{{ openbao_no_log }}".
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", _BOTH_ROLES)
def test_every_secret_bearing_task_sets_openbao_no_log(role: str):
    path = _ROLES_DIR / role / "tasks" / "main.yml"
    offenders = []
    for task in _load_task_file(path):
        if not _handles_secret_value(task):
            continue
        if _no_log_value(task) is not True or not _no_log_is_openbao_expr(task):
            offenders.append(_task_name(task) or "<unnamed task>")
    assert not offenders, (
        f"{role}/tasks/main.yml has secret-bearing task(s) that do NOT carry "
        f'no_log: "{{{{ openbao_no_log }}}}" (Req 1.7/3.4/7.2): {offenders}'
    )


def test_control_role_has_at_least_one_secret_bearing_no_log_task():
    """Sanity: the detector actually matched the control role's token tasks."""
    path = _ROLES_DIR / _CONTROL_ROLE / "tasks" / "main.yml"
    matched = [t for t in _load_task_file(path) if _handles_secret_value(t)]
    assert matched, (
        "the secret-value detector matched NO task in the control role — the "
        "marker list has drifted from the role; fix the detector before trusting "
        "the no_log assertion."
    )


# --------------------------------------------------------------------------- #
# 4. Control role is check-then-write: every mutating bao call is guarded.
# --------------------------------------------------------------------------- #
def _is_bao_command_task(task: dict) -> bool:
    """True iff the task is an ansible.builtin.command invoking the bao CLI."""
    mods = _task_module_names(task)
    cmd_key = None
    for k in ("ansible.builtin.command", "command"):
        if k in task:
            cmd_key = k
            break
    if cmd_key is None:
        return False
    body = _task_body_text(task)
    return "bao" in body and "docker" in body


def _is_mutating_bao_task(task: dict) -> bool:
    """True iff the bao command mutates the Primary (auth enable / write).

    Matched on the argv-token form only (``'enable'`` / ``'write'`` as rendered
    list elements) so a task NAME containing "enabled"/"write" does not false-
    positive. A read is ``auth list`` / ``policy read`` / bare ``read`` /
    ``role-id`` read — none of which carry an ``'enable'`` or ``'write'`` argv
    element.
    """
    body = _task_body_text(task)
    return "'enable'" in body or "'write'" in body


def test_control_role_mutating_bao_calls_are_check_then_write():
    """Every mutating bao call is guarded by a prior-read decision, or is the
    sanctioned always-mint Secret_ID call (Req 1.4/2.6; live changed=0 = task 8.2).
    """
    path = _ROLES_DIR / _CONTROL_ROLE / "tasks" / "main.yml"
    tasks = _load_task_file(path)

    mutating = [
        t for t in tasks if _is_bao_command_task(t) and _is_mutating_bao_task(t)
    ]
    assert mutating, (
        "found no mutating bao calls in the control role — the detector drifted."
    )

    unguarded = []
    for task in mutating:
        body = _task_body_text(task)
        when = task.get("when")
        # The Secret_ID mint is the ONE always-run mutation by design (an
        # un-rereadable, TTL/num_uses-bounded credential — design.md trade-offs).
        is_secret_id_mint = "secret-id" in body and "write" in body and (
            "-f" in body or "'-f'" in body
        )
        if is_secret_id_mint:
            continue
        # Otherwise it must be gated by a when: that references a prior read's
        # decision fact (…_present / …_needs_write).
        when_text = str(when).lower() if when is not None else ""
        guarded = (
            when is not None
            and ("present" in when_text or "needs_write" in when_text)
        )
        if not guarded:
            unguarded.append(_task_name(task) or "<unnamed task>")
    assert not unguarded, (
        "control role has mutating bao call(s) NOT gated by a check-then-write "
        f"decision fact (Req 1.4/2.6): {unguarded}. Each enable/write (except the "
        "by-design Secret_ID mint) must carry a when: on a prior read's "
        "_present/_needs_write fact so a converged state is a no-op."
    )
