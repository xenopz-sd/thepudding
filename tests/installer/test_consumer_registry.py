"""Unit tests for the Registry resolver.

Feature: project-consumer-onboarding (tasks.md Task 3.2; design.md §Components →
Phase-1 Registry resolution; Requirements 2.1, 2.2, 2.4, 2.5).

Concrete examples: an exact-case match returns the vlan_id; a case-mismatch is a
no-match (absent) halt; a decommissioned entry halts; a malformed/unreadable
Registry halts. Each halt is its own distinct exception subclass.

All offline: the module under test is pure (resolver) + a thin file reader, so
these run under the repo's default offline pytest invocation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from consumer_registry import (
    ProjectDecommissionedError,
    RegistryUnreadableError,
    SlugAbsentError,
    resolve_vlan_id,
    resolve_vlan_id_from_file,
)


def _registry() -> dict:
    return {
        "projects": [
            {"slug": "dronefleet", "vlan_id": 100, "status": "active"},
            {"slug": "mlvideo", "vlan_id": 101, "status": "active"},
            {"slug": "edge-sensors", "vlan_id": 102, "status": "decommissioned"},
            {"slug": "legacy", "vlan_id": 103},  # no status -> defaults active
        ]
    }


# --- resolve_vlan_id (pure) ----------------------------------------------


def test_exact_case_match_returns_vlan_id():
    assert resolve_vlan_id(_registry(), "dronefleet") == 100
    assert resolve_vlan_id(_registry(), "mlvideo") == 101


def test_missing_status_defaults_active_and_resolves():
    assert resolve_vlan_id(_registry(), "legacy") == 103


def test_case_mismatch_is_absent_not_a_match():
    with pytest.raises(SlugAbsentError):
        resolve_vlan_id(_registry(), "DroneFleet")
    with pytest.raises(SlugAbsentError):
        resolve_vlan_id(_registry(), "DRONEFLEET")


def test_absent_slug_halts():
    with pytest.raises(SlugAbsentError):
        resolve_vlan_id(_registry(), "nope")


def test_decommissioned_entry_halts():
    with pytest.raises(ProjectDecommissionedError):
        resolve_vlan_id(_registry(), "edge-sensors")


def test_non_mapping_registry_halts_unreadable():
    with pytest.raises(RegistryUnreadableError):
        resolve_vlan_id(["not", "a", "mapping"], "x")


def test_projects_not_a_list_halts_unreadable():
    with pytest.raises(RegistryUnreadableError):
        resolve_vlan_id({"projects": {"slug": "x"}}, "x")


def test_entry_missing_fields_halts_unreadable():
    with pytest.raises(RegistryUnreadableError):
        resolve_vlan_id({"projects": [{"slug": "x"}]}, "x")


def test_non_integer_vlan_id_halts_unreadable():
    reg = {"projects": [{"slug": "x", "vlan_id": "100"}]}
    with pytest.raises(RegistryUnreadableError):
        resolve_vlan_id(reg, "x")


def test_bool_vlan_id_halts_unreadable():
    # bool is an int subclass; a True/False vlan_id is malformed, not 1/0.
    reg = {"projects": [{"slug": "x", "vlan_id": True}]}
    with pytest.raises(RegistryUnreadableError):
        resolve_vlan_id(reg, "x")


# --- resolve_vlan_id_from_file (thin reader) -----------------------------


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "projects.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_from_file_exact_match(tmp_path):
    path = _write(
        tmp_path,
        """\
        projects:
          - slug: dronefleet
            vlan_id: 100
            status: active
        """,
    )
    assert resolve_vlan_id_from_file("dronefleet", path) == 100


def test_from_file_missing_file_halts_unreadable(tmp_path):
    with pytest.raises(RegistryUnreadableError):
        resolve_vlan_id_from_file("x", tmp_path / "does-not-exist.yaml")


def test_from_file_malformed_yaml_halts_unreadable(tmp_path):
    path = _write(tmp_path, "projects: [ {slug: x, vlan_id: 100\n")  # broken YAML
    with pytest.raises(RegistryUnreadableError):
        resolve_vlan_id_from_file("x", path)


def test_from_file_decommissioned_halts(tmp_path):
    path = _write(
        tmp_path,
        """\
        projects:
          - slug: gone
            vlan_id: 104
            status: decommissioned
        """,
    )
    with pytest.raises(ProjectDecommissionedError):
        resolve_vlan_id_from_file("gone", path)


def test_from_file_resolves_against_real_repo_registry():
    # Sanity check against the committed Registry: dronefleet is VLAN 100.
    assert resolve_vlan_id_from_file("dronefleet") == 100
