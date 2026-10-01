"""Pure registry views over the ``projects.yaml`` allocation ledger.

Every function here is deterministic and side-effect-free: given an
already-parsed registry mapping it returns the same result, reads no file, and
touches no network. The module operates only on the in-memory registry dict
(the ``{"projects": [...]}`` shape produced by
``onboard_project.load_registry``); parsing and file I/O stay in the reader.

``projects.yaml`` is one append-only file read two ways (feature design,
"The two views over one append-only file"):

- **Allocation_Ledger_View** — :func:`all_vlan_ids`: every recorded VLAN ID
  regardless of status. This is the input to VLAN-ID allocation
  (``next_vlan_id``), so a decommissioned ID still pushes the ceiling up and is
  never reused (Requirement 4.5). Its semantics match
  ``onboard_project.existing_vlan_ids`` — every recorded ``vlan_id``,
  int-validated.
- **Desired_State_View** — :func:`active_vlan_ids`: the VLAN IDs of rows whose
  normalized status is ``active``. This is the input to the ``sdn_gateway``
  converge role, so a ``decommissioned`` row is excluded here (driving teardown)
  while still counted by :func:`all_vlan_ids` (Requirements 6.1, 6.3). It is
  always a subset of :func:`all_vlan_ids`.

:func:`normalize_status` is the single point that defaults a missing or empty
``status`` to ``active`` (so pre-backfill rows and hand-added rows degrade
safely toward the pre-feature behavior, Requirements 5.2, FD.3) and rejects an
unrecognised value.

Adding ``status`` changes only what the *converge role* reads; the allocation
math (``netfoundation.derivation.next_vlan_id``) is unchanged (ADR-0005). See
the feature design's Components (C2) section.
"""

from __future__ import annotations

from typing import Any

#: Default status applied to a row that omits ``status`` or carries an empty
#: value. A missing status degrades safely to ``active`` — identical to the
#: pre-feature registry (Requirements 5.2, FD.3).
DEFAULT_STATUS = "active"

#: The recognised lifecycle status values. ``active`` rows are in the
#: Desired_State_View (their VLAN is served); ``decommissioned`` rows are
#: excluded from it (driving teardown) but remain in the Allocation_Ledger_View
#: so their VLAN ID is never reused (Requirement 4.1).
VALID_STATUSES = ("active", "decommissioned")


def normalize_status(entry: dict[str, Any]) -> str:
    """Return a registry row's normalized lifecycle status.

    A missing or empty ``status`` defaults to :data:`DEFAULT_STATUS`
    (``"active"``), so pre-backfill rows and hand-added rows behave identically
    to an explicit ``status: active`` row (Requirements 5.2, FD.3). An
    unrecognised value is rejected rather than silently treated as active, so a
    typo cannot quietly exclude a live project from the served set.

    Args:
        entry: A single parsed registry row (a mapping). Only its ``status``
            key is consulted here.

    Returns:
        One of :data:`VALID_STATUSES` — the row's status, or
        :data:`DEFAULT_STATUS` when absent/empty.

    Raises:
        ValueError: If ``status`` is present but not one of
            :data:`VALID_STATUSES`.
    """
    raw = entry.get("status")
    if raw is None or raw == "":
        return DEFAULT_STATUS
    if raw not in VALID_STATUSES:
        raise ValueError(
            f"Unrecognised project status {raw!r}; must be one of "
            f"{', '.join(VALID_STATUSES)} (or absent, defaulting to "
            f"{DEFAULT_STATUS!r})."
        )
    return raw


def all_vlan_ids(registry: dict[str, Any]) -> list[int]:
    """Return the Allocation_Ledger_View — every recorded VLAN ID.

    Every row's ``vlan_id`` is returned regardless of its status, matching
    ``onboard_project.existing_vlan_ids`` semantics (Requirement 4.5). This is
    the input to VLAN-ID allocation, so a ``decommissioned`` row still counts
    and its ID is never reused. The view never shrinks when a row is
    decommissioned (Property 4).

    Args:
        registry: The parsed registry mapping with a ``projects`` list.

    Returns:
        The recorded VLAN IDs in registry order.

    Raises:
        ValueError: If any recorded ``vlan_id`` is not an integer, which would
            corrupt the allocation math.
    """
    ids: list[int] = []
    for entry in registry["projects"]:
        vlan_id = entry["vlan_id"]
        if not isinstance(vlan_id, int) or isinstance(vlan_id, bool):
            raise ValueError(
                f"Registry entry for slug {entry.get('slug')!r} has a "
                f"non-integer vlan_id: {vlan_id!r}."
            )
        ids.append(vlan_id)
    return ids


def active_vlan_ids(registry: dict[str, Any]) -> list[int]:
    """Return the Desired_State_View — VLAN IDs of ``active`` rows only.

    A row's status is normalized via :func:`normalize_status`, so a row that
    omits ``status`` counts as ``active`` (Requirement 5.2, FD.3). A
    ``decommissioned`` row is excluded here — driving the converge role to tear
    that VLAN's gateway down — while still being counted by :func:`all_vlan_ids`
    (Requirements 6.1, 6.3). The result is always a subset of
    :func:`all_vlan_ids` (Property 2).

    Args:
        registry: The parsed registry mapping with a ``projects`` list.

    Returns:
        The VLAN IDs of active rows, in registry order.

    Raises:
        ValueError: If any recorded ``vlan_id`` is not an integer, or any row
            carries an unrecognised ``status`` value.
    """
    ids: list[int] = []
    for entry in registry["projects"]:
        if normalize_status(entry) != "active":
            continue
        vlan_id = entry["vlan_id"]
        if not isinstance(vlan_id, int) or isinstance(vlan_id, bool):
            raise ValueError(
                f"Registry entry for slug {entry.get('slug')!r} has a "
                f"non-integer vlan_id: {vlan_id!r}."
            )
        ids.append(vlan_id)
    return ids
