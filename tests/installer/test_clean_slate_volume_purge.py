"""Offline drift-guard test for clean-slate volume purge (Task 9.1, Property 7).

Feature: svc07-installer-simplification.

Spec: .kiro/specs/svc07-installer-simplification/ (design.md "Correctness
Properties" §Property 7, "Testing Strategy" §"Clean-slate volume-purge
verification"; requirements.md Requirement 5). This is a pure-text/structure
drift guard in the style of ``test_swap_gate.py``'s
``test_harness_logic_matches_playbook`` — it always runs (no ansible-playbook
binary, no live infra needed).

WHAT IS ASSERTED (offline, no live apply, no infra)
---------------------------------------------------
Property 7 — Clean-slate volume purge (Requirements 5.1, 5.2, 5.3, 5.5, 5.6):

  * ``terraform_clean_slate/tasks/reset_target.yml`` issues a scoped
    ``pct destroy ... --purge`` for the target (the destroy command carries
    ``--purge`` AND references ``{{ clean_slate_target.vmid }}``), so backing
    volumes inside the guest rootfs do not survive to a reused VMID/IP
    (Req 5.1, 5.2).
  * The already-absent tolerance is preserved — the destroy task tolerates a
    ``'does not exist'`` stderr so a re-run is idempotent (Req 5.4/5.5).
  * NO global / ``docker volume prune`` (or any unscoped ``volume prune``)
    appears ANYWHERE across the reworked roles/playbooks (Req 5.3), protecting
    other tenants from a cross-tenant prune.

SCOPE OF THE "NO UNSCOPED PRUNE" GREP
-------------------------------------
The scan covers the reworked SVC-07 surface:
  - ansible/roles/svc07_* (all five extracted roles)
  - ansible/roles/terraform_clean_slate
  - ansible/playbooks/svc-07-bootstrap.yml
  - ansible/playbooks/svc07-health.yml
  - ansible/playbooks/tasks/svc07-health-debug-probes.yml

Note: ``proxmox-backup-client prune`` (PBS snapshot retention) is a completely
unrelated operation and is explicitly OUT OF SCOPE — this test scopes its
assertion to ``docker volume prune`` and unscoped ``volume prune``, never PBS
prune. (In practice PBS prune lives in ``openbao_install``, which is not in the
scanned set anyway, but the regex is deliberately narrow regardless.)

GATING
------
Pure text over files already on disk — no ansible-playbook binary, no live
Proxmox — so it is NOT ``requires_infra`` and always runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/installer/ -> repo root is parents[2] (same idiom as test_swap_gate.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]

_RESET_TARGET = (
    _REPO_ROOT
    / "ansible"
    / "roles"
    / "terraform_clean_slate"
    / "tasks"
    / "reset_target.yml"
)

# The reworked roles/playbooks the "no unscoped prune" grep must cover.
_SVC07_ROLE_DIRS = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_preflight",
    _REPO_ROOT / "ansible" / "roles" / "svc07_provision",
    _REPO_ROOT / "ansible" / "roles" / "svc07_bootstrap",
    _REPO_ROOT / "ansible" / "roles" / "svc07_handoff",
    _REPO_ROOT / "ansible" / "roles" / "svc07_clean_slate",
    _REPO_ROOT / "ansible" / "roles" / "terraform_clean_slate",
)

_SVC07_PLAYBOOKS = (
    _REPO_ROOT / "ansible" / "playbooks" / "svc-07-bootstrap.yml",
    _REPO_ROOT / "ansible" / "playbooks" / "svc07-health.yml",
    _REPO_ROOT / "ansible" / "playbooks" / "tasks" / "svc07-health-debug-probes.yml",
)

# Matches a Docker volume prune or a bare/unscoped `volume prune` in any form:
#   docker volume prune
#   docker    volume   prune -f
#   volume prune
# Case-insensitive; tolerant of intervening whitespace. Deliberately does NOT
# match `proxmox-backup-client prune` (no "volume" token) — PBS prune is out of
# scope per the spec's Property 7 note.
_UNSCOPED_PRUNE_RE = re.compile(r"\bvolume\s+prune\b", re.IGNORECASE)


def _iter_yaml_files(root: Path):
    """Yield every .yml/.yaml file under ``root`` (recursively)."""
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in (".yml", ".yaml"):
            yield path


def _collect_scanned_files() -> list[Path]:
    """All YAML files across the reworked SVC-07 role dirs + the named playbooks."""
    files: list[Path] = []
    for role_dir in _SVC07_ROLE_DIRS:
        files.extend(_iter_yaml_files(role_dir))
    for playbook in _SVC07_PLAYBOOKS:
        if playbook.is_file():
            files.append(playbook)
    return files


# =============================================================================
# Property 7 (a) — reset_target.yml purges the target's backing volumes
# =============================================================================


def test_reset_target_issues_scoped_pct_destroy_purge():
    """``reset_target.yml`` issues ``pct destroy ... --purge`` for the target.

    Validates: Requirements 5.1, 5.2 — the destroy purges the guest's backing
    storage (``--purge``) and is scoped to the caller-assembled target vmid
    (``{{ clean_slate_target.vmid }}``), so Docker named volumes inside the guest
    rootfs do not survive to a reused VMID/IP.
    """
    assert _RESET_TARGET.is_file(), (
        f"expected terraform_clean_slate reset_target.yml at {_RESET_TARGET}"
    )
    text = _RESET_TARGET.read_text(encoding="utf-8")

    # The purge and the target vmid must both appear on the destroy command line.
    # Order-tolerant: `pct destroy <vmid> --purge` OR `pct destroy --purge <vmid>`.
    destroy_re = re.compile(
        r"pct\s+destroy\b[^\n]*--purge\b|pct\s+destroy\b[^\n]*--purge",
        re.IGNORECASE,
    )
    assert destroy_re.search(text), (
        "reset_target.yml must issue `pct destroy ... --purge` so the guest's "
        f"backing volumes are removed; not found in:\n{_RESET_TARGET}"
    )

    # The destroy must be scoped to the per-target vmid, not a hardcoded/global id.
    # Find the `pct destroy` command line specifically and assert it carries both
    # `--purge` and the templated target vmid.
    destroy_lines = [
        line
        for line in text.splitlines()
        if "pct destroy" in line.lower()
    ]
    assert destroy_lines, "no `pct destroy` command line found in reset_target.yml"
    assert any(
        "--purge" in line and "clean_slate_target.vmid" in line
        for line in destroy_lines
    ), (
        "the `pct destroy` command must carry BOTH `--purge` AND the templated "
        "`{{ clean_slate_target.vmid }}` so the purge is scoped to the "
        f"caller-assembled target; destroy line(s) were:\n{destroy_lines}"
    )


def test_reset_target_tolerates_already_absent_guest():
    """The destroy tolerates an already-absent guest (idempotent re-run).

    Validates: Requirements 5.4/5.5 — a target already gone from ``pct list`` is
    tolerated so the reset continues without error toward the empty baseline.
    The baseline expresses this as a ``failed_when`` that ignores a
    ``'does not exist'`` stderr on the ``pct destroy`` result.
    """
    text = _RESET_TARGET.read_text(encoding="utf-8")

    assert "does not exist" in text, (
        "reset_target.yml must tolerate an already-absent guest via a "
        "`'does not exist' not in stderr` failed_when guard; the tolerance "
        f"marker was not found in:\n{_RESET_TARGET}"
    )
    # Tie the tolerance to the destroy result, not some unrelated task: the
    # clean_slate_destroy register and the tolerance must co-occur.
    assert "clean_slate_destroy" in text, (
        "expected the `pct destroy --purge` task to register "
        "`clean_slate_destroy` so its already-absent tolerance is scoped to the "
        f"destroy; not found in:\n{_RESET_TARGET}"
    )
    assert re.search(
        r"clean_slate_destroy\.stderr[^\n]*does not exist"
        r"|does not exist[^\n]*clean_slate_destroy\.stderr",
        text,
    ), (
        "the `'does not exist'` tolerance must be evaluated against the "
        "`clean_slate_destroy.stderr` (the destroy result), preserving the "
        f"already-absent idempotence; not found in:\n{_RESET_TARGET}"
    )


# =============================================================================
# Property 7 (b) — no global / docker volume prune anywhere in the rework
# =============================================================================


def test_no_unscoped_volume_prune_anywhere_in_reworked_surface():
    """No ``docker volume prune`` / unscoped ``volume prune`` in the rework.

    Validates: Requirements 5.3, 5.5, 5.6 — clean-slate destruction stays scoped
    to the two SVC-07 VMIDs; a cluster-wide/unscoped Docker volume prune (which
    would hit other tenants) must NOT appear anywhere across the five extracted
    roles, ``terraform_clean_slate``, or the SVC-07 playbooks. ``proxmox-backup-
    client prune`` (PBS retention) is unrelated and out of scope, so this scans
    only for the ``volume prune`` shape.
    """
    scanned = _collect_scanned_files()
    assert scanned, (
        "expected to scan the reworked SVC-07 roles/playbooks, but found no "
        "files — check the scan roots resolved correctly"
    )

    offenders: list[str] = []
    for path in scanned:
        content = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            if _UNSCOPED_PRUNE_RE.search(line):
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "found unscoped/global `volume prune` in the reworked SVC-07 surface — "
        "clean-slate must stay scoped to the two SVC-07 VMIDs (never a "
        "cross-tenant prune):\n" + "\n".join(offenders)
    )


def test_scan_actually_covers_the_named_scope():
    """Sanity guard: the scan set includes each explicitly-named scope target.

    Prevents the "no offenders because we scanned nothing" false pass — if a
    scope path is renamed/removed the drift guard would silently stop covering
    it. This pins the scan to the five roles, terraform_clean_slate, and the
    three named playbooks the task enumerates.
    """
    scanned = set(_collect_scanned_files())
    scanned_str = {str(p) for p in scanned}

    # Each of the three named playbooks must be present on disk and scanned.
    for playbook in _SVC07_PLAYBOOKS:
        assert playbook.is_file(), (
            f"expected named-scope playbook to exist: {playbook}"
        )
        assert str(playbook) in scanned_str, (
            f"named-scope playbook not in the scan set: {playbook}"
        )

    # reset_target.yml (under terraform_clean_slate) must be scanned.
    assert str(_RESET_TARGET) in scanned_str, (
        f"terraform_clean_slate reset_target.yml not in the scan set: {_RESET_TARGET}"
    )

    # Each of the five svc07_* roles must contribute at least one scanned file.
    for role_dir in _SVC07_ROLE_DIRS:
        assert role_dir.is_dir(), f"expected role dir to exist: {role_dir}"
        assert any(str(p).startswith(str(role_dir)) for p in scanned), (
            f"role dir contributed no scanned YAML file: {role_dir}"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
