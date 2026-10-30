"""Offline shape guard for the SVC-06 (ZITADEL) backing-database wiring.

Slice-3 Task 14 (spec: svc-06-identity-provider) — design.md §3.3 + the
cross-cutting "Terraform state isolation" analysis. SVC-06 provisions ZITADEL's
backing PostgreSQL by CONSUMING the SVC-01 reusable root/module (NOT a bespoke
DB and NOT a DB module block folded into the SVC-06 Terraform root). This test
pins that contract OFFLINE, before the orchestration role lands in Task 17:

  1. The ZITADEL backing-DB parameter set (svc06_provision/defaults) exists and
     names the SVC-01 instance identity: a DISTINCT project slug
     `shared-zitadel`, `db_engine=postgres` (ZITADEL needs PG 14-18), VLAN 20,
     a distinct host_index + VMID (no VLAN-20 collision), extensions OFF.
  2. That slug yields the DISTINCT SVC-01 per-instance state name
     `shared-zitadel-svc01-postgres` (via the SVC-01 provision role's
     `<project_slug>-svc01-postgres` derivation), so the backing DB's Terraform
     state is isolated from both the SVC-06 root's own state
     (`svc-06-identity-provider`) and every other SVC-01 instance
     (integration-boundaries §5, domain-rules §2).
  3. SVC-06 does NOT embed a DB `module`/`proxmox_virtual_environment_container`
     for the database in its own Terraform root (when that root lands) — the DB
     is an SVC-01 instance, not a second DB implementation.

Pure-text / PyYAML, always runs (no infra). Lives under tests/installer per the
drift-guard convention (the two conftest.py trees are run separately).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc06_backing_db.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DB_DEFAULTS = _REPO_ROOT / "ansible" / "roles" / "svc06_provision" / "defaults" / "main.yml"
# Fix (spec fix-svc06-clean-slate-backing-db-residue, Req 3.2): the three
# backing-DB IDENTITY values (svc06_db_project_slug/svc06_db_vlan_id/svc06_db_vmid)
# were HOISTED to the play-level vars of svc-06-bootstrap.yml (single source of
# truth) so the clean_slate branch — which skips svc06_provision — can still see
# them. svc06_db_host_index and the provision-only knobs (engine, extensions,
# tier) STAY in svc06_provision/defaults. This test therefore reads the identity
# from the play and the provision-only knobs from the role defaults.
_SVC06_BOOTSTRAP = _REPO_ROOT / "ansible" / "playbooks" / "svc-06-bootstrap.yml"
_SVC01_PROVISION = _REPO_ROOT / "ansible" / "roles" / "svc01_provision" / "tasks" / "main.yml"
_SVC01_ROOT = _REPO_ROOT / "infra" / "projects" / "svc-01-sql-database"
_SVC06_ROOT = _REPO_ROOT / "infra" / "projects" / "svc-06-identity-provider"

# The per-instance SVC-01 state name the backing DB MUST resolve to.
_EXPECTED_DB_STATE_NAME = "shared-zitadel-svc01-postgres"


def _load_play_vars() -> dict:
    """The play-level ``vars:`` of svc-06-bootstrap.yml — the authoritative home
    of the three backing-DB IDENTITY values after the single-source-of-truth
    hoist (Req 3.2)."""
    assert _SVC06_BOOTSTRAP.is_file(), (
        "svc-06-bootstrap.yml must exist and carry the play-level backing-DB identity vars"
    )
    doc = yaml.safe_load(_SVC06_BOOTSTRAP.read_text(encoding="utf-8")) or []
    assert isinstance(doc, list) and doc and isinstance(doc[0], dict), (
        "svc-06-bootstrap.yml must be a play list whose first play is a mapping"
    )
    return doc[0].get("vars", {}) or {}


def _load_db_defaults() -> dict:
    """Merged backing-DB view: the three IDENTITY values from the play-level vars
    (authoritative single source of truth, Req 3.2) OVERLAID on the provision
    role defaults (which still own svc06_db_host_index + the engine/extension/
    tier knobs). This mirrors Ansible precedence (play vars outrank role defaults)
    and lets this shape guard assert the full backing-DB parameter set from its
    post-hoist locations."""
    assert _DB_DEFAULTS.is_file(), (
        "svc06_provision/defaults/main.yml must define the ZITADEL backing-DB "
        "provision-only parameters (engine/extensions/tier/host_index)"
    )
    merged = yaml.safe_load(_DB_DEFAULTS.read_text(encoding="utf-8")) or {}
    play_vars = _load_play_vars()
    for identity_key in ("svc06_db_project_slug", "svc06_db_vlan_id", "svc06_db_vmid"):
        if identity_key in play_vars:
            merged[identity_key] = play_vars[identity_key]
    return merged


# --------------------------------------------------------------------------- #
# 1. The backing-DB parameter set names a correct SVC-01 instance.
# --------------------------------------------------------------------------- #
def test_backing_db_params_present_and_correct():
    # Single source of truth (Req 3.2): the three identity values live in the
    # play-level vars, NOT (also) in svc06_provision/defaults — else they drift.
    role_defaults = yaml.safe_load(_DB_DEFAULTS.read_text(encoding="utf-8")) or {}
    play_vars = _load_play_vars()
    for identity_key in ("svc06_db_project_slug", "svc06_db_vlan_id", "svc06_db_vmid"):
        assert identity_key in play_vars, (
            f"{identity_key} must be a PLAY-LEVEL var in svc-06-bootstrap.yml "
            "(authoritative single source of truth for the clean_slate branch, Req 3.2)"
        )
        assert identity_key not in role_defaults, (
            f"{identity_key} must NOT also be defined in svc06_provision/defaults "
            "(two editable copies drift — Req 3.2); it was hoisted to the play"
        )
    d = _load_db_defaults()
    assert d.get("svc06_db_project_slug") == "shared-zitadel", (
        "the ZITADEL backing DB must use the distinct slug 'shared-zitadel' so its SVC-01 "
        "state name is unique/isolated"
    )
    assert d.get("svc06_db_engine") == "postgres", (
        "ZITADEL requires PostgreSQL 14-18 (requirements §12) — the backing engine must be postgres"
    )
    assert int(d.get("svc06_db_vlan_id")) == 20, (
        "ZITADEL + its backing DB are shared platform services on VLAN 20 (domain-rules §1)"
    )
    # ZITADEL does not need vector/geo/time-series extensions.
    for flag in ("svc06_db_enable_pgvector", "svc06_db_enable_postgis", "svc06_db_enable_timescaledb"):
        assert d.get(flag) is False, f"{flag} must default OFF (ZITADEL does not use that extension)"


def test_backing_db_slug_is_valid_pf_slug():
    """The backing-DB slug must satisfy the PF §4 slug rule (lowercase/hyphen,
    <=20 chars) so the SVC-01 root's project_slug regex accepts it."""
    import re
    d = _load_db_defaults()
    slug = str(d.get("svc06_db_project_slug", ""))
    assert re.match(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$", slug), (
        f"backing-DB slug {slug!r} must be a valid PF §4 slug (the SVC-01 root validates project_slug "
        "with this exact regex, so an invalid slug would fail the DB apply)"
    )


# --------------------------------------------------------------------------- #
# 2. Distinct host_index + VMID (no VLAN-20 collision with known occupants).
# --------------------------------------------------------------------------- #
def test_backing_db_host_index_and_vmid_distinct():
    d = _load_db_defaults()
    host_index = int(d.get("svc06_db_host_index"))
    vmid = int(d.get("svc06_db_vmid"))
    # Known VLAN-20 host_index 0/1 are taken (svc07 primary/unsealer, svc09);
    # the backing DB must not use 0 or 1.
    assert host_index >= 2, (
        "backing-DB host_index must avoid the taken VLAN-20 low indices (0/1) so its computed "
        "IP 10.0.20.1X does not collide (NET-00 §3)"
    )
    # Known VMIDs in use on the shared tier: 1070/1071 (svc07), 1090 (svc09).
    assert vmid not in (100, 666, 1070, 1071, 1090), (
        f"backing-DB VMID {vmid} collides with a known occupant; pick a free VMID"
    )


# --------------------------------------------------------------------------- #
# 3. The slug yields the DISTINCT SVC-01 per-instance state name, isolated from
#    the SVC-06 root's own state and every other SVC-01 instance.
# --------------------------------------------------------------------------- #
def test_backing_db_resolves_to_isolated_svc01_state_name():
    d = _load_db_defaults()
    slug = str(d.get("svc06_db_project_slug"))
    # Mirror the SVC-01 provision role's derivation: `<project_slug>-svc01-postgres`.
    derived = f"{slug}-svc01-postgres"
    assert derived == _EXPECTED_DB_STATE_NAME, (
        f"the backing DB's SVC-01 state name must be {_EXPECTED_DB_STATE_NAME!r} "
        f"(got {derived!r} from slug {slug!r})"
    )
    # The SVC-01 provision role must actually derive the state name from the slug
    # (so our mirrored derivation is faithful, not a guess).
    prov = _SVC01_PROVISION.read_text(encoding="utf-8")
    assert "{{ project_slug }}-svc01-postgres" in prov, (
        "svc01_provision must derive the state name as `<project_slug>-svc01-postgres` for the "
        "backing-DB isolation to hold"
    )
    # And that state name must NOT equal the SVC-06 root's own state name — two
    # isolated states (the DB's and the IdP's), never one shared file.
    assert _EXPECTED_DB_STATE_NAME != "svc-06-identity-provider", (
        "the backing DB and the ZITADEL root must have DISTINCT Terraform state names"
    )


# --------------------------------------------------------------------------- #
# 4. SVC-06's own Terraform root must NOT implement the DB itself.
# --------------------------------------------------------------------------- #
def test_svc06_root_does_not_embed_a_database():
    """If/when the SVC-06 Terraform root exists (Task 15), it must provision only
    the ZITADEL guest — never a postgres/mariadb container of its own (the DB is
    an SVC-01 instance via a child bootstrap, design.md §3.3)."""
    main_tf = _SVC06_ROOT / "main.tf"
    if not main_tf.is_file():
        pytest.skip("SVC-06 Terraform root not built yet (Task 15); the no-embedded-DB "
                    "assertion lands with the root.")
    text = main_tf.read_text(encoding="utf-8")
    # Assert on STRUCTURE, not prose (a 'postgresql-backed' mention in a resource
    # description string is fine). The root must declare NO database container
    # resource and NO db module / SVC-01-root reference.
    assert not re.search(
        r'resource\s+"proxmox_virtual_environment_container"\s+"(db|postgres|mariadb)"', text
    ), (
        "the SVC-06 Terraform root must NOT embed a database container — the backing DB is a "
        "separate SVC-01 instance with its own isolated state (design.md §3.3)"
    )
    assert not re.search(r'module\s+"db"\s*\{', text), (
        "the SVC-06 root must NOT declare a module \"db\" (child-orchestrated SVC-01 instance, not an in-root module)"
    )
    assert "svc-01-sql-database" not in text, (
        "the SVC-06 root must NOT reference the SVC-01 root as a module source (child-orchestrated, own state)"
    )
    # The SVC-01 root must exist to be consumed (it is the backing-DB provider).
    assert (_SVC01_ROOT / "main.tf").is_file(), (
        "the SVC-01 reusable root must be present for SVC-06 to consume as the backing DB"
    )
