"""Offline structural guard for the consumer workload-guest provisioning gate.

Feature: consumer-last-mile-hello-workload (tasks.md Task 3.2; design.md
"Property 3 — No-block no-op" + Component 2; Requirements 2.3, 5.4).

=== WHAT THIS TEST IS (AND WHY IT IS OFFLINE) ===

Task 3.1 threaded an OPTIONAL ``workload`` block through ``consumer_provision``:
a ``terraform apply`` of the per-project ``workload/`` root plus the inventory
regeneration for the workload guest. Task 3.2 is the offline structural proof of
**Property 3 (No-block no-op, Req 2.3/5.4)**: a manifest with NO ``workload``
block — or ``workload.enabled: false`` — provisions NO guest and adds NO workload
host to any inventory, behaving exactly as before this feature.

Rather than run Ansible/Terraform live (``requires-infra``), this test PARSES
``ansible/roles/consumer_provision/tasks/main.yml`` OFFLINE with PyYAML and
asserts the fail-closed gate is a PROPERTY of the rendered role, mirroring the
sibling ``test_consumer_swap_gate.py`` / ``test_consumer_workload_creds_dir.py``
structural style:

  * the gate fact ``consumer_workload_enabled`` is DERIVED from
    ``consumer_manifest.workload.enabled | default(false)`` — the ``default(false)``
    is what makes a missing ``workload`` key a strict no-op (never a raise);
  * the whole workload-provisioning block (the ``terraform apply`` of the
    workload root AND the workload ``generate_inventory.py`` run) is GATED on
    ``consumer_workload_enabled | bool`` — so when the gate is false NOTHING in
    the block runs: no workload-root ``terraform apply``, no workload inventory
    host;
  * every secret-bearing workload task (the ones carrying the Proxmox
    endpoint/token in ``TF_VAR_*`` env, the SSH key read, the env-file slurp,
    and the ``terraform apply``) preserves ``no_log: true`` and echoes no secret.

Everything here reads YAML on disk; nothing starts a container, contacts a
cluster, or runs ansible. It is collected under the default offline gate.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_consumer_workload_provision_gate.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

# tests/installer/<this file> -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISION = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_provision" / "tasks" / "main.yml"
)

# The exact gate condition the role derives the gate fact from (quoted in the
# task report). A missing `workload` key is caught by `default(false)`.
_GATE_SOURCE_EXPR = "consumer_manifest.workload.enabled | default(false)"
# The gate the workload block + its tasks are conditioned on.
_GATE_WHEN = "consumer_workload_enabled | bool"


# --------------------------------------------------------------------------- #
# Helpers — load / walk the role task list.
# --------------------------------------------------------------------------- #
def _text() -> str:
    assert _PROVISION.is_file(), f"expected {_PROVISION} to exist"
    return _PROVISION.read_text(encoding="utf-8")


def _top_level_tasks() -> list:
    """Return the role's top-level ordered task list (not descended).

    Parsed ONCE and cached so object identity is stable across helpers (the
    `gated_ids` id() comparison in the apply-location test relies on a single
    parse — two independent `yaml.safe_load_all` calls would yield distinct
    dicts for the same task and break the identity check)."""
    global _TOP_LEVEL_CACHE
    if _TOP_LEVEL_CACHE is not None:
        return _TOP_LEVEL_CACHE
    docs = list(yaml.safe_load_all(_text()))
    tasks: list = []
    for doc in docs:
        if isinstance(doc, list):
            tasks.extend(t for t in doc if isinstance(t, dict))
    assert tasks, "consumer_provision tasks/main.yml must have tasks"
    _TOP_LEVEL_CACHE = tasks
    return tasks


_TOP_LEVEL_CACHE: list | None = None


def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _all_tasks() -> list:
    return list(_iter_tasks(_top_level_tasks()))


def _name(task: dict) -> str:
    return str(task.get("name", ""))


def _when_text(task: dict) -> str:
    when = task.get("when")
    if when is None:
        return ""
    if isinstance(when, list):
        return " && ".join(str(w) for w in when)
    return str(when)


def _command_argv_text(task: dict) -> str | None:
    """Return the flat argv/cmd string of a command/shell task, else None."""
    for mod in (
        "ansible.builtin.command",
        "ansible.builtin.shell",
        "command",
        "shell",
    ):
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
            cmd = spec.get("cmd")
            if isinstance(cmd, str):
                return cmd
    return None


def _workload_gate_block() -> dict:
    """Return the top-level `block:` task gated on the workload-enabled fact.

    This is the block whose `when` is `consumer_workload_enabled | bool` and
    which contains the `terraform apply` of the workload root.
    """
    for task in _top_level_tasks():
        if "block" not in task:
            continue
        if _GATE_WHEN in _when_text(task):
            return task
    raise AssertionError(
        "no top-level `block` gated on "
        f"'{_GATE_WHEN}' found in consumer_provision — the workload-provisioning "
        "tasks must live inside a single gated block (strict no-op, Req 2.3)"
    )


# =========================================================================== #
# Property 3 — No-block no-op: the gate fact derives from default(false).
# Validates: Requirements 2.3, 5.4
# =========================================================================== #
def test_gate_fact_derived_from_workload_enabled_default_false():
    """``consumer_workload_enabled`` is set from
    ``consumer_manifest.workload.enabled | default(false)`` — the ``default(false)``
    makes a manifest with NO ``workload`` block (or ``enabled: false``) a strict
    no-op rather than a raise (Req 2.3, 5.4)."""
    set_fact_tasks = [
        t
        for t in _all_tasks()
        if ("ansible.builtin.set_fact" in t or "set_fact" in t)
    ]
    gate_setters = []
    for t in set_fact_tasks:
        spec = t.get("ansible.builtin.set_fact") or t.get("set_fact") or {}
        if isinstance(spec, dict) and "consumer_workload_enabled" in spec:
            gate_setters.append((t, str(spec.get("consumer_workload_enabled", ""))))
    assert gate_setters, (
        "consumer_provision must set_fact consumer_workload_enabled — the gate "
        "fact evaluated once so a missing `workload` key can never raise (Req 2.3)"
    )
    # The gate fact must be derived from the manifest key with a `default(false)`.
    derivations = " ".join(expr for _, expr in gate_setters)
    assert _GATE_SOURCE_EXPR in derivations, (
        "consumer_workload_enabled must be derived from "
        f"'{_GATE_SOURCE_EXPR}' (default(false) = strict no-op on an absent "
        f"`workload` block); got derivation(s): {derivations!r}"
    )
    # The gate-fact set_fact itself must NOT be gated on the workload being
    # enabled (it must always run, so the no-op INFO + the block gate can read
    # it even when absent).
    gate_task = gate_setters[0][0]
    assert _GATE_WHEN not in _when_text(gate_task), (
        "the gate fact must be evaluated unconditionally (it is the thing the "
        "block gate then reads); it must not itself be gated on the workload"
    )


def test_workload_provisioning_lives_in_a_single_enabled_gated_block():
    """All workload-provisioning tasks live inside ONE block gated on
    ``consumer_workload_enabled | bool`` — so when the gate is false, NOTHING in
    the block runs (Req 2.3)."""
    block = _workload_gate_block()
    assert _when_text(block) == _GATE_WHEN or _GATE_WHEN in _when_text(block), (
        f"the workload block must be gated exactly on '{_GATE_WHEN}'"
    )
    # Sanity: the block is non-empty and contains child tasks.
    inner = block.get("block")
    assert isinstance(inner, list) and inner, "the gated workload block must be non-empty"


def test_workload_terraform_apply_is_inside_the_gated_block():
    """The workload-root ``terraform apply`` is INSIDE the enabled-gated block, so
    a no-``workload`` manifest triggers NO ``terraform apply`` of the workload
    subroot (Req 2.3) — the core of the strict no-op."""
    block = _workload_gate_block()
    block_tasks = list(_iter_tasks([block]))
    apply_tasks = []
    for t in block_tasks:
        argv = _command_argv_text(t)
        if argv and "terraform" in argv and "apply" in argv:
            apply_tasks.append(t)
    assert apply_tasks, (
        "the workload-root `terraform apply` must live INSIDE the "
        f"'{_GATE_WHEN}'-gated block (no apply when the workload is absent, Req 2.3)"
    )
    # And there must be NO workload `terraform apply` OUTSIDE the gated block.
    gated_ids = {id(t) for t in block_tasks}
    for t in _all_tasks():
        if id(t) in gated_ids:
            continue
        argv = _command_argv_text(t)
        if not argv:
            continue
        if "terraform" in argv and "apply" in argv and "workload" in (
            argv + _name(t)
        ).lower():
            raise AssertionError(
                "a workload-related `terraform apply` was found OUTSIDE the "
                "enabled-gated block — it must be gated for the strict no-op (Req 2.3): "
                f"task {_name(t)!r}"
            )


def test_workload_inventory_generation_is_inside_the_gated_block():
    """The workload ``generate_inventory.py`` run is INSIDE the gated block, so a
    no-``workload`` manifest adds NO workload host to any inventory (Req 2.3)."""
    block = _workload_gate_block()
    block_tasks = list(_iter_tasks([block]))
    gen_tasks = []
    for t in block_tasks:
        argv = _command_argv_text(t)
        if argv and "generate_inventory.py" in argv:
            gen_tasks.append(t)
    assert gen_tasks, (
        "the workload `generate_inventory.py` run must live INSIDE the "
        f"'{_GATE_WHEN}'-gated block (no workload host added when absent, Req 2.3)"
    )
    # The gated workload inventory generation must target the workload root /
    # workload inventory (not the DB inventory), proving it is the workload host.
    joined = " ".join(_command_argv_text(t) or "" for t in gen_tasks)
    assert "workload" in joined, (
        "the gated inventory generation must target the workload root/inventory "
        "(the workload host appears ONLY because the guest is enabled)"
    )


def test_disabled_path_is_a_pure_debug_noop():
    """When the gate is false the only workload task that runs is a ``debug`` INFO
    no-op (``when: not (consumer_workload_enabled | bool)``) — it mutates nothing
    and echoes no secret (Req 2.3)."""
    noop_tasks = [
        t
        for t in _top_level_tasks()
        if ("ansible.builtin.debug" in t or "debug" in t)
        and "not (consumer_workload_enabled" in _when_text(t)
    ]
    assert noop_tasks, (
        "there must be a debug INFO task gated on `not (consumer_workload_enabled "
        "| bool)` documenting the strict no-op when the workload is absent (Req 2.3)"
    )
    for t in noop_tasks:
        # a debug no-op must not be a command/terraform/inventory mutation.
        assert "ansible.builtin.command" not in t and "command" not in t, (
            "the disabled-path task must be a pure debug no-op, never a command"
        )


# =========================================================================== #
# Secret hygiene — no_log preserved on secret-bearing workload tasks (Req 5.3).
# Mirrors the no_log assertions in the sibling swap-gate / bridge tests.
# =========================================================================== #
def _truthy_no_log(task: dict) -> bool:
    val = str(task.get("no_log", "")).strip().lower()
    return val in ("true", "{{ openbao_no_log }}", "yes")


def test_token_bearing_workload_tasks_preserve_no_log():
    """Every workload task that handles the Proxmox endpoint/token (the
    ``TF_VAR_*`` env, the env-file slurp, the CI backend values, the SSH key read,
    the ``terraform init``/``apply``) MUST carry ``no_log`` so no secret value is
    echoed (Req 5.3). Mirrors the swap-gate/hygiene tests' no_log assertions."""
    block = _workload_gate_block()
    block_tasks = list(_iter_tasks([block]))

    # 1) The terraform apply carries the token via TF_VAR_* env -> must no_log.
    apply_tasks = [
        t
        for t in block_tasks
        if (_command_argv_text(t) or "") and "terraform" in (_command_argv_text(t) or "")
        and "apply" in (_command_argv_text(t) or "")
    ]
    assert apply_tasks, "expected a workload terraform apply task"
    for t in apply_tasks:
        assert _truthy_no_log(t), (
            f"workload `terraform apply` ({_name(t)!r}) carries the Proxmox token "
            "via TF_VAR_* env and MUST carry no_log: true (Req 5.3)"
        )

    # 2) terraform init tasks (token in env) must no_log.
    init_tasks = [
        t
        for t in block_tasks
        if (_command_argv_text(t) or "") and "terraform" in (_command_argv_text(t) or "")
        and "init" in (_command_argv_text(t) or "")
    ]
    for t in init_tasks:
        assert _truthy_no_log(t), (
            f"workload `terraform init` ({_name(t)!r}) runs with the token in its "
            "environment and MUST carry no_log: true (Req 5.3)"
        )

    # 3) Secret-bearing set_fact / slurp tasks that bind the token / env values /
    #    SSH key must no_log. Identify them by the facts/paths they touch.
    # NOTE: the operator SSH *public* key (consumer_workload_ssh_key_file /
    # consumer_workload_ssh_keys_json) is deliberately NOT in this set — a public
    # key is not a secret, so its read/encode tasks need not carry no_log. The
    # markers below are the genuinely secret-bearing bindings: the Proxmox
    # token/endpoint env, the raw env-file slurp + its KEY=VALUE pairs, and the
    # derived GitLab CI job-token/state values.
    secret_markers = (
        "proxmox_api_token",
        "TF_VAR_",
        "consumer_workload_env_slurp",
        "consumer_workload_env_pairs",
        "consumer_workload_env_ci_values",
        "consumer_workload_gitlab_state_address",
        "consumer_workload_ci_job_token",
    )
    for t in block_tasks:
        spec_set = t.get("ansible.builtin.set_fact") or t.get("set_fact")
        spec_slurp = t.get("ansible.builtin.slurp") or t.get("slurp")
        body = ""
        set_keys: set[str] = set()
        if isinstance(spec_set, dict):
            body = yaml.safe_dump(spec_set)
            set_keys = set(spec_set.keys())
        elif isinstance(spec_slurp, dict):
            body = yaml.safe_dump(spec_slurp)
        else:
            continue
        touches_secret = any(m in body for m in secret_markers)
        # The env-KEY-NAMES extraction is deliberately NOT no_log — it SETS
        # consumer_workload_env_keys (names only; it regex-strips every value),
        # so it exposes no secret even though it READS the pairs. Key the
        # exclusion on the fact being SET, not on what the task reads from.
        names_only = set_keys == {"consumer_workload_env_keys"}
        if touches_secret and not names_only:
            assert _truthy_no_log(t), (
                f"secret-bearing workload task ({_name(t)!r}) binds a token/secret "
                "value and MUST carry no_log: true (Req 5.3)"
            )


def test_no_secret_value_literal_echoed_in_workload_block():
    """No workload task echoes a raw secret VALUE — the ``debug`` tasks print only
    names/flags/INFO, never a token (Req 5.3). Structural: no debug ``msg``
    references the token/SSH-key facts."""
    block = _workload_gate_block()
    for t in _iter_tasks([block]):
        spec = t.get("ansible.builtin.debug") or t.get("debug")
        if not isinstance(spec, dict):
            continue
        msg = str(spec.get("msg", ""))
        for forbidden in (
            "proxmox_api_token",
            "ci_job_token",
            "ssh_keys_json",
            "ssh_key_file",
        ):
            assert forbidden not in msg, (
                f"a workload debug msg ({_name(t)!r}) must not echo the secret "
                f"fact '{forbidden}' (Req 5.3)"
            )
