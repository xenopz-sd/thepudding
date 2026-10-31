"""Offline structural guard for the svc07_clean_slate Proxmox-host auto-default.

Defect fix (spec: fix-svc07-clean-slate-proxmox-host-default) — a third-party
operator deploying against thepudding v0.11.0 hit a clean-slate reset that
HARD-FAILED without an undocumented ``-e svc07_proxmox_host=<lan-ip>``, even
though the Proxmox host LAN IP is ALREADY present in the cluster env file as
``PROXMOX_HOST_LAN_IP`` (emitted by cluster_env.py as
``TF_VAR_proxmox_host_lan_ip``). The fix auto-defaults the clean-slate Proxmox
host from the already-resolved cluster env, so ``-e svc07_proxmox_host=`` is
OPTIONAL (it still WINS when supplied non-empty), and the fail-fast assert only
fires when BOTH sources are empty.

This guard pins the FIX by parsing ONLY the role files (no ``ansible-playbook``,
no live Proxmox — always runs, no ``requires_infra`` gate):

  * ``ansible/roles/svc07_clean_slate/tasks/main.yml`` — a new ``set_fact`` binds
    ``svc07_cluster_env_proxmox_host`` from ``TF_VAR_proxmox_host_lan_ip`` out of
    the SAME ``svc07_cs_env_resolve.stdout | from_json`` using an OPTIONAL read
    (``.get(...)`` / ``default``), with ``no_log: true`` (the source dict carries
    the API token by key); a second ``set_fact`` defines
    ``svc07_effective_proxmox_host`` with ``-e``-override-wins-ELSE-cluster-env
    precedence (NOT ``no_log`` — the assert value must stay diagnosable); the
    "SSH address was supplied" assert gates on ``svc07_effective_proxmox_host``
    (not the raw override) and names BOTH remedies; BOTH ``ansible_host=``
    consumers (the delegated destroy argv and the ``pct list`` verify argv) use
    the effective host, never the raw ``svc07_proxmox_host``.
  * ``ansible/roles/svc07_clean_slate/defaults/main.yml`` — ``svc07_proxmox_host``
    stays ``""`` (the DEFAULT now comes from the cluster env, not a changed
    role-default value).

Mirrors the repo drift-guard style (``test_svc07_clean_slate_route_removal.py``):
helpers COPIED locally (no cross-test-tree import — the tests/installer vs
infra/tests conftest-collision rule), repo root via ``parents[2]``, PyYAML parse
only, never embeds/prints a secret.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc07_clean_slate_proxmox_host_default.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml
from hypothesis import given, strategies as st

# tests/installer/test_svc07_clean_slate_proxmox_host_default.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "svc07_clean_slate"
_ROLE_TASKS = _ROLE_DIR / "tasks" / "main.yml"
_ROLE_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_CLUSTER_ENV_HELPER = _REPO_ROOT / "scripts" / "installer" / "cluster_env.py"

# Keys on a task dict that are directives/containers, NOT the invoked module.
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally per the repo drift-guard convention (no cross-tree
# import; the conftest-collision rule). Mirrors test_svc07_clean_slate_route_removal.
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path) -> list[dict]:
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
        elif isinstance(doc, dict):
            tasks.extend(_iter_tasks([doc]))
    return tasks


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _set_fact_spec(task: dict) -> dict:
    spec = task.get("ansible.builtin.set_fact") or task.get("set_fact")
    return spec if isinstance(spec, dict) else {}


def _task_argv_text(task: dict) -> str:
    parts: list[str] = []
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if spec is None:
            continue
        if isinstance(spec, str):
            parts.append(spec)
        elif isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                parts.append(" ".join(str(a) for a in argv))
            elif isinstance(argv, str):
                parts.append(argv)
    return " ".join(parts)


def _assert_that_text(task: dict) -> str:
    spec = task.get("ansible.builtin.assert") or task.get("assert")
    if not isinstance(spec, dict):
        return ""
    that = spec.get("that")
    if isinstance(that, (list, tuple)):
        return " ".join(str(x) for x in that)
    return str(that or "")


def _assert_failmsg_text(task: dict) -> str:
    spec = task.get("ansible.builtin.assert") or task.get("assert")
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("fail_msg", ""))


def _name(task: dict) -> str:
    return str(task.get("name", ""))


# --------------------------------------------------------------------------- #
# Loaded once at import — deterministic, structural (one parsed tree).
# --------------------------------------------------------------------------- #
_TASKS = _load_role_tasks(_ROLE_TASKS)
_DEFAULTS = yaml.safe_load(_ROLE_DEFAULTS.read_text(encoding="utf-8")) or {}


def _set_facts_defining(var: str) -> list[dict]:
    return [t for t in _TASKS if var in _set_fact_spec(t)]


# =========================================================================== #
# Property 1 — BUG CONDITION / EXPECTED BEHAVIOR.
# Each assertion FAILS on the unfixed role and PASSES after the fix.
# =========================================================================== #
def test_binds_host_lan_ip_from_cluster_env_resolve_json():
    """Change 1: a set_fact binds the host LAN IP from TF_VAR_proxmox_host_lan_ip
    out of svc07_cs_env_resolve.stdout | from_json, using an OPTIONAL read."""
    binders = _set_facts_defining("svc07_cluster_env_proxmox_host")
    assert binders, (
        "No set_fact defines svc07_cluster_env_proxmox_host — the clean-slate "
        "role never auto-defaults the Proxmox host from the cluster env (bug)."
    )
    expr = str(_set_fact_spec(binders[0])["svc07_cluster_env_proxmox_host"])
    assert "TF_VAR_proxmox_host_lan_ip" in expr, (
        "The host-LAN-IP bind must read TF_VAR_proxmox_host_lan_ip from the resolve JSON."
    )
    assert "svc07_cs_env_resolve.stdout" in expr, (
        "The bind must reuse the already-registered svc07_cs_env_resolve.stdout "
        "(no second cluster_env.py invocation)."
    )
    # OPTIONAL read: the key is optional (emitted only when set), so a direct
    # [...] index would KeyError when absent. Require .get(...) / a default path.
    assert (".get(" in expr) or ("default(" in expr), (
        "The host-LAN-IP key is OPTIONAL; the bind must use an optional read "
        "(.get(...) or a default), never a direct ['...'] index that KeyErrors."
    )
    assert "['TF_VAR_proxmox_host_lan_ip']" not in expr, (
        "The bind must NOT use a direct ['TF_VAR_proxmox_host_lan_ip'] index "
        "(the key is optional and may be absent)."
    )


def test_host_lan_ip_bind_is_no_log():
    """Change 1: the host-LAN-IP bind is no_log (defence-in-depth — the source
    resolve dict carries the API token by key, matching the node-name bind)."""
    binders = _set_facts_defining("svc07_cluster_env_proxmox_host")
    assert binders, "svc07_cluster_env_proxmox_host set_fact missing (bug / unfixed)."
    assert binders[0].get("no_log") is True, (
        "The host-LAN-IP bind must be no_log: true (its source dict carries the token)."
    )


def test_effective_host_precedence_set_fact_present():
    """Change 2: a set_fact defines svc07_effective_proxmox_host with
    -e-override-wins-ELSE-cluster-env precedence, and is NOT no_log."""
    binders = _set_facts_defining("svc07_effective_proxmox_host")
    assert binders, (
        "No set_fact defines svc07_effective_proxmox_host — the effective host "
        "(override-else-cluster-env) is never computed (bug / unfixed)."
    )
    task = binders[0]
    expr = str(_set_fact_spec(task)["svc07_effective_proxmox_host"])
    assert "svc07_proxmox_host" in expr, (
        "The effective-host expression must reference the raw override svc07_proxmox_host."
    )
    assert "svc07_cluster_env_proxmox_host" in expr, (
        "The effective-host expression must fall back to svc07_cluster_env_proxmox_host."
    )
    # Diagnosable: the effective host feeds the fail-fast assert's value, so it
    # must NOT be no_log (only the token-bearing source bind is no_log).
    assert task.get("no_log") is not True, (
        "svc07_effective_proxmox_host must NOT be no_log — its value must stay "
        "visible for the fail-fast assert diagnosis (only the token-bearing "
        "source bind is no_log)."
    )


def test_assert_gates_on_effective_host_not_raw_override():
    """Change 3: the 'SSH address was supplied' assert gates on the EFFECTIVE
    host, not the raw svc07_proxmox_host, and names BOTH remedies."""
    candidates = [
        t for t in _TASKS
        if (("ansible.builtin.assert" in t) or ("assert" in t))
        and ("ssh address" in _name(t).lower())
    ]
    assert candidates, "Could not locate the 'Proxmox host SSH address was supplied' assert."
    that = _assert_that_text(candidates[0])
    assert "svc07_effective_proxmox_host" in that, (
        "The assert must gate on svc07_effective_proxmox_host (so a cluster-env "
        "default satisfies it), not the raw -e override."
    )
    assert "svc07_proxmox_host " not in (that + " ").replace("svc07_proxmox_host_", "X"), (
        "The assert must NOT gate on the raw svc07_proxmox_host (the bug: it "
        "demanded the undocumented -e override)."
    )
    fail_msg = _assert_failmsg_text(candidates[0])
    assert "PROXMOX_HOST_LAN_IP" in fail_msg, (
        "The fail_msg must name the PROXMOX_HOST_LAN_IP cluster-env remedy."
    )
    assert "svc07_proxmox_host" in fail_msg, (
        "The fail_msg must still name the -e svc07_proxmox_host= override remedy."
    )


def test_both_ansible_host_consumers_use_effective_host():
    """Change 4: BOTH ansible_host= consumers (the delegated destroy argv and the
    pct list verify argv) use the effective host, never the raw override."""
    consumers = [
        t for t in _TASKS if "ansible_host={{ svc07_" in _task_argv_text(t)
    ]
    assert len(consumers) >= 2, (
        f"Expected at least two ansible_host= consumers (delegated destroy + pct "
        f"list verify); found {len(consumers)}."
    )
    for t in consumers:
        argv = _task_argv_text(t)
        assert "ansible_host={{ svc07_effective_proxmox_host }}" in argv, (
            f"Consumer {_name(t)!r} must use the effective host "
            f"(ansible_host={{{{ svc07_effective_proxmox_host }}}})."
        )
        assert "ansible_host={{ svc07_proxmox_host }}" not in argv, (
            f"Consumer {_name(t)!r} must NOT use the raw svc07_proxmox_host "
            f"(an empty raw override would leak past the gate)."
        )


# =========================================================================== #
# Property 2 — PRESERVATION. Captures behavior that must survive the fix.
# These PASS on the unfixed tree too (they are the baseline to preserve).
# =========================================================================== #
def test_preserve_node_name_bind_and_no_log():
    binders = _set_facts_defining("proxmox_node_name")
    assert binders, "The resolved-node-name bind must remain present."
    task = binders[0]
    expr = str(_set_fact_spec(task)["proxmox_node_name"])
    assert "TF_VAR_proxmox_node_name" in expr
    assert task.get("no_log") is True, "The node-name bind must stay no_log: true."


def test_preserve_cluster_env_resolve_is_no_log():
    resolves = [
        t for t in _TASKS
        if "svc07_cs_env_resolve" == str(t.get("register", ""))
    ]
    assert resolves, "The cluster-env resolve command (svc07_cs_env_resolve) must remain."
    assert resolves[0].get("no_log") is True, (
        "The cluster-env resolve must stay no_log: true (its stdout carries the token)."
    )


def test_preserve_core_clean_slate_tasks_present():
    """Guard / confirmation / destroy / baseline gate all structurally present."""
    names = " || ".join(_name(t).lower() for t in _TASKS)
    for needle in (
        "throwaway",                 # agent-live guard consult / refuse
        "prompt for explicit confirmation",
        "assemble the two svc-07 clean_slate_targets",
        "delegate the reset to clean-slate.yml",
        "gate",                      # empty-baseline GATE assert
    ):
        assert needle in names, f"Expected a clean-slate task mentioning {needle!r}."


def test_preserve_role_default_value_unchanged():
    assert "svc07_proxmox_host" in _DEFAULTS, "defaults/main.yml must still declare svc07_proxmox_host."
    assert _DEFAULTS["svc07_proxmox_host"] == "", (
        "The role default must stay '' — the DEFAULT now comes from the cluster "
        "env, not a changed role-default value."
    )


def test_preserve_cluster_env_helper_untouched_optional_row():
    """cluster_env.py keeps PROXMOX_HOST_LAN_IP as an OPTIONAL schema row; this
    fix adds NO new schema row (the fix reads an existing optional var)."""
    text = _CLUSTER_ENV_HELPER.read_text(encoding="utf-8")
    assert "PROXMOX_HOST_LAN_IP" in text, "cluster_env.py must still know PROXMOX_HOST_LAN_IP."
    assert "TF_VAR_proxmox_host_lan_ip" in text, (
        "cluster_env.py must still emit TF_VAR_proxmox_host_lan_ip."
    )


# =========================================================================== #
# Property 2 (pure logic) — the effective-host precedence, property-based.
# Mirrors the Jinja: override if non-empty-after-trim else cluster-env; the
# assert gate passes iff at least one source is non-empty-after-trim.
# =========================================================================== #
def _effective(override: str, cluster_env: str) -> str:
    return override if len(override.strip()) > 0 else cluster_env


_token = st.text(alphabet=" \t.0123456789abcdef:", min_size=0, max_size=12)


@given(override=_token, cluster_env=_token)
def test_effective_precedence_property(override: str, cluster_env: str):
    eff = _effective(override, cluster_env)
    if override.strip():
        assert eff == override, "A non-empty -e override must WIN verbatim."
    else:
        assert eff == cluster_env, "An empty override must fall back to the cluster env."


@given(override=_token, cluster_env=_token)
def test_assert_gate_fires_iff_both_empty_property(override: str, cluster_env: str):
    eff = _effective(override, cluster_env)
    gate_passes = len(eff.strip()) > 0
    at_least_one = bool(override.strip()) or bool(cluster_env.strip())
    assert gate_passes == at_least_one, (
        "The fail-fast gate must pass iff at least one source is non-empty "
        "(both-empty always fails fast, destroying nothing)."
    )
