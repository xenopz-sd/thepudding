"""Offline (Tier-1) bug-condition exploration + fix-validation test for the
service clean-slate ``terraform_data`` residue defect.

Spec: .kiro/specs/fix-svc07-clean-slate-terraform-data-residue/
(bugfix.md, design.md, tasks.md). This file is **Task 1** (the bug-condition
exploration test) and doubles as the Task 3.5 fix-validation test — the SAME
test, which FAILS on the unfixed code (proving the residue bug) and PASSES once
the fix empties the full state.

=== WHAT THE BUG IS ===

A service clean-slate destroys its guest LXC containers and removes ONLY the
container resource addresses from Terraform state
(``terraform_clean_slate``/``reset_target.yml`` does exactly one
``terraform state rm <resource_address>`` per target). It never accounts for the
non-guest ``terraform_data`` book-keeping resources living in the same state —
the root-level ledger-check (``terraform_data.svc07_host_index_ledger_check`` for
SVC-07, ``terraform_data.ledger_index_assert`` for SVC-09) and the per-module
``module.*.terraform_data.input_guards`` anchors. So after a reported-clean
(``failed=0``) reset, ``terraform state list`` is NOT empty, and the service's
own verification gate (which inspects only the container addresses) still reports
an empty baseline (the Req 1.2 "gate blind spot").

=== HOW THIS TEST ENCODES IT (offline, provider-free) ===

Per the design Testing Strategy, the PRIMARY encoding is offline and
provider-free (design root-cause #4: a provider-reaching ``terraform destroy``
hung and left a stale state lock, so the fix must clear the pure
``terraform_data`` resources WITHOUT provider contact). For each affected root we:

  1. Seed a REAL local Terraform state (``terraform apply`` of a tiny sandbox
     whose resources are all pure ``terraform_data`` — the guest containers are
     represented by ``terraform_data`` stand-ins so the apply needs no Proxmox
     provider) containing the guest container address(es) PLUS the non-guest
     ``terraform_data`` addresses the real root carries.
  2. Drive the clean-slate state-reconciliation step AS THE ROLE ENCODES IT
     (``_role_reconciliation_rm``): on the UNFIXED role only the per-target
     container ``resource_address`` is removed; once the fix adds a provider-free
     full-state-emptying step, every remaining address is removed.
  3. Assert ``terraform state list`` is EMPTY (Property P / Req 2.1-2.5).

Step 2 reads the COMMITTED role files, so the test is coupled to the real role:
it FAILS on the unfixed role (residue remains) and PASSES after the fix — no test
edit between Task 1 and Task 3.5.

=== TIER ===

Offline / provider-free: the only external tool is the ``terraform`` binary, and
every resource is a provider-less ``terraform_data``. Gated behind the shared
``requires_terraform`` marker (skip — never fail — when no terraform/tofu binary
is present), mirroring the sibling terraform-gated tests.

Run:
  PYTHONPATH=infra/platform-foundation/scripts \\
    ~/venv/devinfra/bin/pytest infra/tests/test_svc07_clean_slate_state_residue.py -q
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from conftest import _terraform_binary, requires_terraform

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"


# --------------------------------------------------------------------------- #
# The affected roots, their guest container resource addresses (what the
# existing clean-slate removes), and the non-guest terraform_data addresses the
# real root carries (the residue). Scoped to the concrete failing roots per the
# design's "Scoped PBT Approach" — this is a deterministic defect.
# --------------------------------------------------------------------------- #
# Each case: (role_name, [container addresses], [non-guest terraform_data addrs]).
_CASES = {
    "svc07": (
        "svc07_clean_slate",
        [
            "proxmox_virtual_environment_container.primary",
            "proxmox_virtual_environment_container.unsealer",
        ],
        [
            "terraform_data.svc07_host_index_ledger_check",
            "module.primary.terraform_data.input_guards",
            "module.unsealer.terraform_data.input_guards",
        ],
    ),
    "svc09": (
        "svc09_clean_slate",
        [
            "proxmox_virtual_environment_container.traefik",
        ],
        [
            "terraform_data.ledger_index_assert",
            "module.traefik.terraform_data.input_guards",
        ],
    ),
}


# --------------------------------------------------------------------------- #
# Seed a real local tfstate containing all the given addresses, as pure
# terraform_data (provider-free). Container addresses are modelled by
# terraform_data stand-ins at the same address, so the apply never needs the
# Proxmox provider — faithful for a STATE-ADDRESS-level test (clean-slate
# reconciliation operates on state addresses, not resource internals).
# --------------------------------------------------------------------------- #
def _seed_state(sandbox: Path, container_addrs: list[str], data_addrs: list[str]) -> Path:
    """Write + apply a tiny root whose state lists exactly the given addresses.

    Every seeded resource is a provider-free ``terraform_data`` so the apply
    needs no Proxmox provider. Guest containers (``proxmox_virtual_environment_
    container.<name>``) are seeded as ``terraform_data.<name>`` stand-ins AT THE
    SAME short name — this is a STATE-ADDRESS-level test (clean-slate
    reconciliation operates on state addresses, not resource internals), and the
    reconciliation driver removes those same stand-in addresses to model the
    role destroying the guest container. The non-guest ``terraform_data`` residue
    is seeded at its REAL address. Returns the root dir (local backend
    configured) whose ``terraform state list`` equals the seeded addresses.
    """
    binary = _terraform_binary()
    assert binary is not None

    root_blocks: list[str] = ['terraform {\n  backend "local" {}\n}\n']
    module_names: set[str] = set()

    def _emit(addr: str) -> None:
        # module.<name>.terraform_data.<res>  OR  <type>.<name>
        m = re.match(r"^module\.([^.]+)\.(.+)$", addr)
        if m:
            module_names.add(m.group(1))
            return
        m2 = re.match(r"^([^.]+)\.([^.]+)$", addr)
        assert m2, f"unhandled address shape: {addr}"
        # Containers become terraform_data.<name> stand-ins; terraform_data.* stay as-is.
        root_blocks.append(f'resource "terraform_data" "{m2.group(2)}" {{}}\n')

    for addr in container_addrs + data_addrs:
        _emit(addr)

    # A single shared child module provides `terraform_data.input_guards`; each
    # module.<name> call instantiates it, yielding module.<name>.terraform_data.input_guards.
    for name in sorted(module_names):
        root_blocks.append(f'module "{name}" {{\n  source = "./guardmod"\n}}\n')
    if module_names:
        guardmod = sandbox / "guardmod"
        guardmod.mkdir(parents=True, exist_ok=True)
        (guardmod / "main.tf").write_text(
            'resource "terraform_data" "input_guards" {}\n', encoding="utf-8"
        )

    (sandbox / "main.tf").write_text("".join(root_blocks), encoding="utf-8")

    subprocess.run(
        [binary, "init", "-no-color", "-input=false"],
        cwd=sandbox, capture_output=True, text=True, check=True,
    )
    subprocess.run(
        [binary, "apply", "-auto-approve", "-no-color", "-input=false"],
        cwd=sandbox, capture_output=True, text=True, check=True,
    )
    return sandbox


def _container_standin_addr(container_addr: str) -> str:
    """The seeded stand-in address for a guest container resource address.

    ``proxmox_virtual_environment_container.primary`` -> ``terraform_data.primary``.
    The reconciliation driver's Step A removes THIS address, modelling the role's
    per-target container ``terraform state rm`` (which, live, removes the real
    container address — here its provider-free stand-in).
    """
    name = container_addr.split(".", 1)[1]
    return f"terraform_data.{name}"


def _state_list(root: Path) -> list[str]:
    binary = _terraform_binary()
    out = subprocess.run(
        [binary, "state", "list"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Reconciliation driver — coupled to the COMMITTED role. On the unfixed role it
# removes ONLY the per-target container resource_address; once the role grows a
# provider-free full-state-emptying step, it removes every remaining address.
# --------------------------------------------------------------------------- #
def _role_text(role_name: str) -> str:
    return (_ROLES / role_name / "tasks" / "main.yml").read_text(encoding="utf-8")


def _reset_target_text() -> str:
    return (_ROLES / "terraform_clean_slate" / "tasks" / "reset_target.yml").read_text(
        encoding="utf-8"
    )


def _task_argv(task: dict) -> list[str]:
    """Flat argv/cmd token list of a command task (COMMAND tokens only, not prose)."""
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                return [str(a) for a in argv]
            if isinstance(spec.get("cmd"), str):
                return spec["cmd"].split()
        elif isinstance(spec, str):
            return spec.split()
    return []


def _role_tasks_flat(role_name: str) -> list[dict]:
    """Flat task list of a role's main.yml + the shared reset_target.yml."""
    out: list[dict] = []
    for text in (_role_text(role_name), _reset_target_text()):
        parsed = yaml.safe_load(text) or []
        if isinstance(parsed, list):
            out.extend(t for t in parsed if isinstance(t, dict))
    return out


def _role_empties_full_state(role_name: str) -> bool:
    """Does the clean-slate (role body OR the shared terraform_clean_slate reset)
    contain a PROVIDER-FREE full-state-emptying step?

    The fix enumerates the residual addresses from ``terraform state list`` and
    removes them with ``terraform state rm`` (NOT ``terraform destroy``). Detected
    structurally from the TASK COMMANDS (not prose/comments — a provider-free
    ``terraform state rm`` task legitimately mentions "NOT terraform destroy" in a
    comment): a ``terraform state rm`` COMMAND task that loops over an enumerated
    residual/remaining set, AND NO ``terraform destroy`` COMMAND anywhere in the
    clean-slate path (provider-free guarantee, Req 2.5).
    """
    tasks = _role_tasks_flat(role_name)

    # Fail-closed on a real `terraform destroy` COMMAND (not a prose mention).
    for t in tasks:
        argv = _task_argv(t)
        if "terraform" in argv and "destroy" in argv:
            return False

    # The fix's signal: a `terraform state rm` COMMAND task driven by a loop over
    # an enumerated residual/remaining address set.
    for t in tasks:
        argv = _task_argv(t)
        is_state_rm = "terraform" in argv and "state" in argv and "rm" in argv
        if not is_state_rm:
            continue
        loop = t.get("loop") or t.get("with_items") or ""
        loop_s = str(loop).lower()
        if "residual" in loop_s or "remaining" in loop_s or "full_state" in loop_s:
            return True
    return False


def _container_addrs_for(role_name: str, declared_containers: list[str]) -> list[str]:
    """The container resource addresses the UNFIXED role removes.

    Robustly: the resource addresses that appear in the role's
    ``resource_address:`` target assembly. Falls back to the declared list.
    """
    text = _role_text(role_name)
    found = re.findall(
        r"resource_address:\s*(proxmox_virtual_environment_container\.\w+)", text
    )
    return found or declared_containers


def _role_reconciliation_rm(
    role_name: str, root: Path, container_addrs: list[str]
) -> None:
    """Drive the role's state-reconciliation step against the seeded state.

    Models the role faithfully: per-target container ``state rm`` always (on the
    container stand-in addresses that exist in the seeded state); PLUS a
    provider-free full-state-empty sweep IFF the role now contains it.
    """
    binary = _terraform_binary()
    # Step A — the guest-container state rm the role ALWAYS does (per target),
    # against the seeded stand-in address for each container.
    for addr in container_addrs:
        subprocess.run(
            [binary, "state", "rm", _container_standin_addr(addr)],
            cwd=root, capture_output=True, text=True,
        )
    # Step B — the fix's provider-free full-state-empty sweep (enumerate +
    # state rm the residue), only present once the role is fixed.
    if _role_empties_full_state(role_name):
        for addr in _state_list(root):
            subprocess.run(
                [binary, "state", "rm", addr],
                cwd=root, capture_output=True, text=True,
            )


# =============================================================================
# Property 1 — Bug Condition: Clean-Slate Leaves Empty Terraform State.
# FAILS on unfixed code (residue remains) -> proves the bug. PASSES post-fix.
# Validates: Requirements 1.1, 1.2, 1.3, 1.4 (bug) / 2.1, 2.2, 2.3, 2.4, 2.5 (fix).
# =============================================================================
@requires_terraform
@pytest.mark.parametrize("case_name", sorted(_CASES))
def test_clean_slate_leaves_empty_state(case_name: str):
    """Validates: Requirements 1.1/1.4 (bug), 2.1/2.4/2.5 (fix) / Property 1.

    For each affected root, after the clean-slate state reconciliation the
    service's ``terraform state list`` must be EMPTY. On the UNFIXED role this
    FAILS: the non-guest ``terraform_data`` addresses survive (the residue).
    """
    role_name, container_addrs, data_addrs = _CASES[case_name]

    with tempfile.TemporaryDirectory(prefix=f"cs_residue_{case_name}_") as tmp:
        root = _seed_state(Path(tmp), container_addrs, data_addrs)

        # Sanity: the seeded state really contains the guest stand-ins + residue.
        seeded = set(_state_list(root))
        expected_container_standins = {_container_standin_addr(a) for a in container_addrs}
        assert expected_container_standins.issubset(seeded), (
            f"seed failed to include container stand-ins; state={sorted(seeded)}"
        )
        assert set(data_addrs).issubset(seeded), (
            f"seed failed to include terraform_data residue; state={sorted(seeded)}"
        )

        # Drive the role's reconciliation and assert FULL emptiness (Property P).
        role_containers = _container_addrs_for(role_name, container_addrs)
        _role_reconciliation_rm(role_name, root, role_containers)

        remaining = _state_list(root)
        assert remaining == [], (
            f"[{case_name}] clean-slate left residual Terraform state — the bug. "
            f"`terraform state list` is NOT empty after a reported-clean reset; "
            f"surviving address(es): {remaining}. A correct clean-slate must empty "
            f"the whole state (Req 2.1-2.5), including the non-guest terraform_data "
            f"book-keeping resources, provider-free."
        )


# =============================================================================
# Req 1.2 — Gate blind spot: the UNFIXED container-scoped gate reports an empty
# baseline while terraform_data residue remains. This is a STRUCTURAL assertion
# on the gate definition; the fix (Task 3.3) tightens it to full-state-empty.
# Validates: Requirements 1.2 (bug) / 2.2 (fix).
# =============================================================================
@pytest.mark.parametrize("case_name", sorted(_CASES))
def test_verification_gate_asserts_full_state_empty(case_name: str):
    """Validates: Requirements 1.2 (bug), 2.2 (fix) / Property 1.

    The service clean-slate's empty-baseline gate must judge emptiness by the
    FULL ``terraform state list`` (zero addresses), not merely by whether the
    container addresses are absent. On the UNFIXED role the ``surviving_state``
    fact is computed by filtering ``terraform state list`` to the container
    addresses ONLY — so a state with surviving ``terraform_data`` still passes
    the gate (the blind spot). This FAILS until Task 3.3 tightens the gate.
    """
    role_name, _containers, _data = _CASES[case_name]
    text = _role_text(role_name)
    tasks = yaml.safe_load(text)

    # Find the set_fact computing the *_surviving_state_addrs fact.
    survivor_fact = f"{case_name}_surviving_state_addrs"
    survivor_body = None
    for t in tasks:
        sf = t.get("ansible.builtin.set_fact") or t.get("set_fact")
        if isinstance(sf, dict) and survivor_fact in sf:
            survivor_body = str(sf[survivor_fact])
            break
    assert survivor_body is not None, (
        f"[{case_name}] no set_fact computing {survivor_fact} found in {role_name}"
    )

    # The blind spot: the body filters to a HARD-CODED container-address list
    # (`select('in', state_list)` over the container addresses). The fixed gate
    # must instead judge the FULL state list — i.e. it must NOT restrict the
    # survivor set to only the container addresses. Signal of the fix: the
    # survivor computation is over the raw `terraform state list` lines (not a
    # pre-filtered container-address literal list).
    filters_to_containers_only = (
        "proxmox_virtual_environment_container" in survivor_body
        and "select(" in survivor_body
    )
    assert not filters_to_containers_only, (
        f"[{case_name}] GATE BLIND SPOT (Req 1.2): the clean-slate gate computes "
        f"`{survivor_fact}` by filtering `terraform state list` to the container "
        f"addresses only, so surviving terraform_data resources do NOT trip the "
        f"gate and a non-empty state reports as an empty baseline. The fix "
        f"(Req 2.2) must assert the FULL state list is empty. body={survivor_body!r}"
    )
