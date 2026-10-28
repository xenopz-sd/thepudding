"""Offline ledger guard for the SVC-17/SVC-18 observability host-index allocation.

Spec: ``.kiro/specs/svc-17-18-observability`` — task 1.2 (offline ledger guard),
Requirements FR-16, AC-2.

Observability is the first service group to place THREE guests on VLAN 20. Per
NET-00 §4 each ledger ``svc_code`` is a SINGLE stable catalog slot (no hyphen,
matching the derivation layer's ``^svc\\d{2,}$`` invariant), so the three guests
split across their real catalog slots: Prometheus and Grafana are SVC-17
(``svc17``, distinguished by the ``component`` field), and Loki is SVC-18
(``svc18``). FR-16 / AC-2 require those rows in the Host_Index_Authority ledger
(``infra/platform-foundation/host-indices.yaml``, ADR-0015) to be ``allocated``
at

    (svc17, prometheus) -> host_index 8  -> 10.0.20.18
    (svc17, grafana)    -> host_index 9  -> 10.0.20.19
    (svc18, loki)       -> host_index 10 -> 10.0.20.20

and those three indices to be mutually distinct AND distinct from every other
already-``allocated`` VLAN-20 row (0, 1, 2, 4, 5), so the ``proxmox-compute``
``peer_host_indices`` precondition and the pure ``assert_unique_host_indices``
guard both pass for VLAN 20.

This is an OFFLINE test (no ``requires_infra`` marker): it reads the committed
ledger file and exercises the pure derivation guard only — no Terraform, no
network, no live cluster. It mirrors the ledger-loading + ``assert_unique_
host_indices`` idiom used by the derivation-layer example tests
(``infra/platform-foundation/scripts/tests/test_derivation_examples.py``) so the
ledger loader and the uniqueness assertion are reused, not reinvented.

Run (single file, fast)::

    ~/venv/devinfra/bin/pytest infra/tests/test_host_indices_obs.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# --------------------------------------------------------------------------- #
# The pure uniqueness guard lives in the ``netfoundation`` package under
# ``infra/platform-foundation/scripts``. That dir is on sys.path for the tests
# co-located there; this test lives in ``infra/tests/``, so add the scripts dir
# to sys.path before importing — the same ``from netfoundation import ...``
# idiom the derivation-layer tests use, so the guard is reused, not reinvented.
# --------------------------------------------------------------------------- #
_THIS_DIR = Path(__file__).resolve().parent              # infra/tests/
_SCRIPTS_DIR = _THIS_DIR.parent / "platform-foundation" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from netfoundation import (  # noqa: E402  (import after sys.path setup)
    HostIndexCollisionError,
    assert_unique_host_indices,
)

#: The committed Host_Index_Authority ledger. Located relative to this test
#: file: infra/tests/ -> infra/ -> platform-foundation/host-indices.yaml.
_HOST_INDICES_YAML = (
    _THIS_DIR.parent / "platform-foundation" / "host-indices.yaml"
)

#: The three observability rows, keyed by (svc_code, component), mapped to the
#: expected (host_index, IP) the design (§3.6 / Reconciliation 0) and FR-16 pin.
#: Prometheus+Grafana are SVC-17 (svc17); Loki is SVC-18 (svc18).
_OBS_EXPECTED: dict[tuple[str, str], tuple[int, str]] = {
    ("svc17", "prometheus"): (8, "10.0.20.18"),
    ("svc17", "grafana"): (9, "10.0.20.19"),
    ("svc18", "loki"): (10, "10.0.20.20"),
}

#: The host indices every OTHER (non-observability) VLAN-20 row already occupies
#: (svc07 primary=0, unsealer=1, svc09=2, svc01/shared-zitadel DB=4, svc06=5).
#: The three new indices must be distinct from each of these.
_OTHER_VLAN20_ALLOCATED_INDICES = {0, 1, 2, 4, 5}


def _load_ledger() -> dict:
    """Load the committed host-index ledger as a parsed dict keyed by vlan_id."""
    with _HOST_INDICES_YAML.open() as fh:
        return yaml.safe_load(fh)


def _obs_rows(ledger: dict) -> dict[tuple[str, str], dict]:
    """Return VLAN-20 observability rows keyed by (svc_code, component).

    Observability spans two catalog slots: Prometheus+Grafana under ``svc17``
    (disambiguated by ``component``), and Loki under ``svc18``.
    """
    return {
        (entry["svc_code"], entry["component"]): entry
        for entry in ledger[20]
        if entry["svc_code"] in ("svc17", "svc18")
    }


def test_ledger_file_exists():
    """The committed Host_Index_Authority ledger file is present."""
    assert _HOST_INDICES_YAML.is_file(), f"missing ledger: {_HOST_INDICES_YAML}"


def test_obs_three_rows_present_with_expected_indices():
    """The three observability rows resolve to host_index 8/9/10 (FR-16, AC-2)."""
    rows = _obs_rows(_load_ledger())
    assert set(rows) == set(_OBS_EXPECTED), (
        "expected exactly (svc17,prometheus)/(svc17,grafana)/(svc18,loki) rows, "
        f"found {sorted(rows)}"
    )
    for key, (expected_index, _ip) in _OBS_EXPECTED.items():
        assert rows[key]["host_index"] == expected_index, (
            f"observability {key} expected host_index {expected_index}"
        )


def test_obs_rows_are_allocated():
    """All three observability rows are flipped from 'reserved' to 'allocated' (FR-16)."""
    rows = _obs_rows(_load_ledger())
    for key in _OBS_EXPECTED:
        assert rows[key]["status"] == "allocated", (
            f"observability {key} must be status: allocated (not reserved)"
        )


def test_obs_indices_mutually_distinct():
    """The three observability host indices are mutually distinct (AC-2)."""
    rows = _obs_rows(_load_ledger())
    indices = [rows[k]["host_index"] for k in _OBS_EXPECTED]
    assert len(set(indices)) == len(indices), (
        f"observability host indices must be mutually distinct, got {indices}"
    )
    assert sorted(indices) == [8, 9, 10]


def test_obs_indices_distinct_from_other_vlan20_rows():
    """The three observability indices collide with no other VLAN-20 row (0,1,2,4,5)."""
    rows = _obs_rows(_load_ledger())
    obs_indices = {rows[k]["host_index"] for k in _OBS_EXPECTED}
    overlap = obs_indices & _OTHER_VLAN20_ALLOCATED_INDICES
    assert not overlap, (
        f"observability indices {sorted(obs_indices)} overlap existing "
        f"VLAN-20 indices {sorted(_OTHER_VLAN20_ALLOCATED_INDICES)}: "
        f"{sorted(overlap)}"
    )


def test_vlan20_full_ledger_passes_uniqueness_guard():
    """``assert_unique_host_indices`` accepts the full VLAN-20 ledger incl. observability.

    Reuses the pure guard (not a reimplementation) over every VLAN-20 entry's
    host_index — the three new allocated rows plus the pre-existing ones — which
    returns None iff they are all distinct.
    """
    ledger = _load_ledger()
    all_indices = [entry["host_index"] for entry in ledger[20]]
    assert assert_unique_host_indices(20, all_indices) is None


def test_vlan20_guard_rejects_obs_collision():
    """A fabricated observability clash onto an existing index is rejected, VLAN named.

    Guards against a regression where a future edit lands one of the three
    guests back on an already-occupied index (e.g. Prometheus onto svc09's 2).
    """
    ledger = _load_ledger()
    all_indices = [entry["host_index"] for entry in ledger[20]]
    # Force prometheus (8) to collide with svc09 (2).
    colliding = [2 if i == 8 else i for i in all_indices]
    with pytest.raises(HostIndexCollisionError) as excinfo:
        assert_unique_host_indices(20, colliding)
    message = str(excinfo.value)
    assert "20" in message   # names the VLAN
    assert "2" in message     # names the repeated index
