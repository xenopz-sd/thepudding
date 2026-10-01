"""Example tests for the desired-state JSON emitter (``registry_status.py``).

Feature: sdn-vlan-gateway-reachability (design C6)

The emitter is a thin CLI over the pure registry-view functions (property-tested
separately in ``test_registry_views.py``). These tests pin the JSON contract the
``sdn_gateway`` role consumes:

    {"active_vlan_ids": [...], "decommissioned_vlan_ids": [...]}

- ``active_vlan_ids`` = the Desired_State_View (Requirement 6.1).
- ``decommissioned_vlan_ids`` = ``all_vlan_ids`` minus ``active_vlan_ids``
  (Requirement 6.3).
- VLAN 20 (always served, not registry-driven) is deliberately absent.

Run only this file, by absolute venv path::

    PYTHONPATH=infra/platform-foundation/scripts \\
      ~/venv/devinfra/bin/pytest \\
      infra/platform-foundation/scripts/tests/test_registry_status.py -q
"""

from __future__ import annotations

import json

from registry_status import compute_vlan_status, main


def _registry(rows: list[dict]) -> dict:
    return {"projects": rows}


def test_all_active_rows_are_active_none_decommissioned() -> None:
    """Every active row appears in active; the complement is empty.

    Validates: Requirements 6.1, 6.3.
    """
    registry = _registry(
        [
            {"slug": "a", "vlan_id": 100, "status": "active"},
            {"slug": "b", "vlan_id": 101, "status": "active"},
            {"slug": "c", "vlan_id": 102, "status": "active"},
        ]
    )
    assert compute_vlan_status(registry) == {
        "active_vlan_ids": [100, 101, 102],
        "decommissioned_vlan_ids": [],
    }


def test_decommissioned_row_moves_to_complement() -> None:
    """A decommissioned row is excluded from active and present in the complement.

    Validates: Requirements 6.1, 6.3.
    """
    registry = _registry(
        [
            {"slug": "a", "vlan_id": 100, "status": "active"},
            {"slug": "b", "vlan_id": 101, "status": "decommissioned"},
            {"slug": "c", "vlan_id": 102, "status": "active"},
        ]
    )
    assert compute_vlan_status(registry) == {
        "active_vlan_ids": [100, 102],
        "decommissioned_vlan_ids": [101],
    }


def test_missing_status_defaults_to_active() -> None:
    """A row that omits ``status`` counts as active (Req 5.2, FD.3).

    Validates: Requirements 5.2, 6.1.
    """
    registry = _registry(
        [
            {"slug": "a", "vlan_id": 100},
            {"slug": "b", "vlan_id": 101, "status": "decommissioned"},
        ]
    )
    assert compute_vlan_status(registry) == {
        "active_vlan_ids": [100],
        "decommissioned_vlan_ids": [101],
    }


def test_empty_registry_yields_empty_sets() -> None:
    """No project rows means both sets are empty (VLAN 20 is not registry-driven)."""
    assert compute_vlan_status(_registry([])) == {
        "active_vlan_ids": [],
        "decommissioned_vlan_ids": [],
    }


def test_cli_emits_valid_json_from_real_registry(capsys) -> None:
    """The CLI prints valid JSON with exactly the two contract keys.

    Uses the real committed registry (three active rows: VLANs 100/101/102).
    Validates: Requirements 6.1, 6.6.
    """
    exit_code = main(["--json"])
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert sorted(payload.keys()) == ["active_vlan_ids", "decommissioned_vlan_ids"]
    assert payload == {
        "active_vlan_ids": [100, 101, 102],
        "decommissioned_vlan_ids": [],
    }


def test_cli_out_file_matches_stdout(tmp_path, capsys) -> None:
    """``--out`` writes the same JSON payload it prints to stdout."""
    out_path = tmp_path / "vlan-status.json"
    exit_code = main(["--json", "--out", str(out_path)])
    assert exit_code == 0
    stdout_payload = json.loads(capsys.readouterr().out)
    file_payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert stdout_payload == file_payload


def test_cli_aborts_cleanly_on_missing_registry(tmp_path, capsys) -> None:
    """A missing registry aborts with exit 1 and no JSON on stdout."""
    missing = tmp_path / "nope.yaml"
    exit_code = main(["--json", "--registry", str(missing)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR" in captured.err
