"""WS-C3 bug-condition + preservation tests — consumer clean-slate must destroy
the per-project workload guest.

Feature: fix-consumer-clean-slate-workload-guest-destroy (WS-C3; PLANNING.md Part
D / §D.1). Offline, PyYAML-structural — the sibling of
``test_consumer_swap_gate.py`` and ``test_svc07_clean_slate_route_removal.py``.

=== WHAT THIS TEST IS (AND WHY IT IS OFFLINE) ===
The bug: ``consumer_clean_slate`` composes the DB's own ``svc-01-bootstrap.yml``
clean-slate (ADR-0011) and reverses the OpenBao/ZITADEL onboarding objects, but
it NEVER destroys the Terraform-provisioned workload guest
(``workload-<vlan>-01``) that ``consumer_provision`` applies from the workload
root ``infra/projects/<slug>/workload`` (state ``<slug>-workload``). So bring-up
provisions a guest that teardown never reverses — a teardown-parity gap.

The fix composes the existing ``terraform_clean_slate`` mechanism (through
``ansible/playbooks/clean-slate.yml``) for the workload target, exactly as
``svc01_clean_slate`` does for its own guest. These tests parse the role YAML on
disk — nothing starts a container, contacts a cluster, or runs ansible — and are
collected under the default offline gate (``tests/installer`` root, its own
``conftest.py``).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_consumer_clean_slate_workload.py -v
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_CS_TASKS = _ROLES_DIR / "consumer_clean_slate" / "tasks" / "main.yml"
_CS_DEFAULTS = _ROLES_DIR / "consumer_clean_slate" / "defaults" / "main.yml"


def _text(path: Path) -> str:
    assert path.is_file(), f"expected {path} to exist"
    return path.read_text(encoding="utf-8")


def _non_comment_text(path: Path) -> str:
    """Role task text with full-line YAML comments stripped, so a forbidden
    pattern mentioned only in an explanatory ``# ...`` comment does not trip the
    'no raw inlined call' assertions (mirrors test_consumer_swap_gate.py)."""
    lines = [
        raw for raw in path.read_text(encoding="utf-8").splitlines()
        if not raw.lstrip().startswith("#")
    ]
    return "\n".join(lines)


# =========================================================================== #
# BUG-CONDITION (FR-1 / AC-1) — FAILS on the committed role, PASSES after fix.
# =========================================================================== #
def test_consumer_clean_slate_composes_workload_teardown():
    """consumer_clean_slate MUST compose a teardown of the per-project workload
    guest — the workload Terraform root routed through the owned
    ``clean-slate.yml`` / ``terraform_clean_slate`` mechanism (never raw surgery).

    On the committed role this is ABSENT -> this test FAILS (the WS-C3 bug).
    After the fix it is present -> PASSES.
    """
    cs = _non_comment_text(_CS_TASKS)
    cs_defaults = _text(_CS_DEFAULTS)

    # (a) The teardown is composed via the owned clean-slate playbook mechanism
    #     (the same seam svc01_clean_slate uses), not a bespoke path.
    assert "clean-slate.yml" in cs, (
        "consumer_clean_slate must compose the workload teardown via "
        "ansible/playbooks/clean-slate.yml (the owned terraform_clean_slate "
        "mechanism) — WS-C3: the workload guest is currently never destroyed."
    )
    # (b) It targets the WORKLOAD Terraform root specifically (distinct from the
    #     DB root), by referencing the workload root path or its defaults var.
    targets_workload_root = (
        "consumer_workload_tf_root" in cs
        or "consumer_workload_tf_root" in cs_defaults
        or "workload" in cs and "clean_slate_targets" in cs
    )
    assert targets_workload_root, (
        "consumer_clean_slate must assemble a clean_slate_targets entry whose "
        "terraform_root is the workload root (infra/projects/<slug>/workload) — "
        "WS-C3."
    )
    # (c) A workload clean_slate_targets entry naming the workload guest.
    assert "clean_slate_targets" in cs and "workload" in cs, (
        "consumer_clean_slate must name a workload clean_slate_targets entry "
        "(workload-<vlan>-01) for the terraform_clean_slate child run — WS-C3."
    )


# =========================================================================== #
# PRESERVATION (FR-6 / AC-2) — hold BEFORE and AFTER the fix.
# =========================================================================== #
def test_db_compose_and_no_touch_invariants_preserved():
    """The fix must not disturb the DB ADR-0011 compose or the Req-7.7 no-touch
    invariants, and must not inline raw cross-boundary surgery."""
    cs = _non_comment_text(_CS_TASKS)
    cs_full = _text(_CS_TASKS)

    # DB teardown still composed via its own bootstrap clean-slate (ADR-0011).
    assert "svc-01-bootstrap.yml" in cs and "clean_slate=true" in cs, (
        "consumer_clean_slate must still compose the DB's own "
        "svc-01-bootstrap.yml clean-slate (ADR-0011, Req 9.5)."
    )

    # Registry + <slug>-infra slice preserved (Req 7.7).
    assert (
        "Req 7.7" in cs_full
        or "infra network slice" in cs_full
        or "<slug>-infra" in cs_full
    ), "consumer_clean_slate must preserve the Registry entry + <slug>-infra slice (Req 7.7)."


def test_no_raw_cross_boundary_surgery_inlined():
    """No raw destructive call is inlined in consumer_clean_slate. The workload
    teardown goes THROUGH clean-slate.yml (terraform_clean_slate), so these raw
    strings must still not appear as direct tasks in this role — the
    composed-vs-inlined carve-out WS-C3 relies on.
    """
    cs = _non_comment_text(_CS_TASKS)
    for forbidden in ("pct destroy", "terraform state rm", "docker volume rm"):
        assert forbidden not in cs, (
            f"consumer_clean_slate must not inline a raw '{forbidden}' — the "
            "workload teardown is composed via clean-slate.yml, not inlined "
            "(Req 9.6)."
        )
