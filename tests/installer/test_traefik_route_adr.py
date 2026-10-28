"""Offline structural doc test for the traefik-cross-guest-ingress decision record.

Task 5.3 (spec: traefik-cross-guest-ingress) — requirements.md R5.1, R5.2, R5.3,
R5.4; design.md §"Testing Strategy" (Offline merge gate: "ADR + constitution
structural test / doc check — ADR-0018 exists with Status/Context/Options/
Decision/Consequences and the rejected alternatives; the project-constitution.md
Decision Log has the ADR-0018 row").

This feature's decision is recorded as ADR-0018 plus a project-constitution
Decision Log row. This module is a pure file-read/parse drift guard that pins the
decision record so it cannot silently regress:

  * ADR EXISTS + SECTION SET (R5.1) — ADR-0018 exists at the conventional
    .kiro/decisions path and carries the SAME heading set its sibling ADRs use
    (Status, Date, Deciders, Context, Options Considered, Decision, Consequences)
    — the headings read off .kiro/decisions/0013-*.md and 0017-*.md.
  * REJECTED ALTERNATIVES (R5.2) — the ADR text records the two rejected
    alternatives: the Docker "Swarm" overlay, and a co-location rejection
    (co-locating all HTTP backends on the Traefik guest).
  * SOLE-INGRESS + INTERNAL-PLUMBING (R5.4) — the ADR states Traefik remains the
    sole LAN-facing ingress and the VLAN-20-internal port is internal plumbing,
    not a second ingress.
  * CONSTITUTION DECISION LOG ROW (R5.3) — project-constitution.md's Decision Log
    references ADR-0018 (by the ADR filename stem) and links the spec dir
    .kiro/specs/traefik-cross-guest-ingress/.
  * CROSS-LINKS RESOLVE — the ADR's #[[file:...]] cross-links (ADR-0013, ADR-0004)
    point at files that actually exist.

Pure-structure, offline: plain text / regex reads of the committed ADR and the
constitution steering file — no infra, no terraform/ansible binary, no live
dependency. Helpers are LOCAL to this module, mirroring the
tests/installer/test_traefik_route*.py siblings — the conftest-collision rule
forbids a cross-tree import (tests/installer vs infra/tests share a same-named
sys.path-injecting conftest.py).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_traefik_route_adr.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Paths. tests/installer/ -> repo root is parents[2].
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DECISIONS = _REPO_ROOT / ".kiro" / "decisions"
_ADR = _DECISIONS / "0018-traefik-cross-guest-ingress-via-file-provider-static-routes.md"
_CONSTITUTION = _REPO_ROOT / ".kiro" / "steering" / "project-constitution.md"
_SPEC_DIR_LINK = ".kiro/specs/traefik-cross-guest-ingress/"
_ADR_STEM = "ADR-0018-traefik-cross-guest-ingress-via-file-provider-static-routes"

# The heading set the sibling ADRs (0013, 0017) use, asserted against ADR-0018.
# (`Date` / `Deciders` are bold metadata lines, not `###` headings, so they are
# matched as `**Field**:` rather than as a markdown heading.)
_REQUIRED_HEADINGS = [
    "Context",
    "Options Considered",
    "Decision",
    "Consequences",
]
_REQUIRED_META = ["Status", "Date", "Deciders"]


# --------------------------------------------------------------------------- #
# Local helpers (no cross-tree import; conftest-collision rule).
# --------------------------------------------------------------------------- #
def _adr_text() -> str:
    assert _ADR.is_file(), (
        f"ADR-0018 must exist at the conventional decisions path {_ADR} "
        "(R5.1) — the traefik-cross-guest-ingress decision record"
    )
    return _ADR.read_text(encoding="utf-8")


def _constitution_text() -> str:
    assert _CONSTITUTION.is_file(), f"project-constitution.md must exist at {_CONSTITUTION}"
    return _CONSTITUTION.read_text(encoding="utf-8")


def _headings(text: str) -> list[str]:
    """Return the stripped text of every markdown ATX heading (## / ### / ...)."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^#{2,}\s+(.*?)\s*$", line)
        if m:
            out.append(m.group(1))
    return out


def _norm(text: str) -> str:
    """Whitespace-collapsed, lowercased text for phrase membership checks."""
    return re.sub(r"\s+", " ", text).lower()


# =========================================================================== #
# R5.1 — ADR-0018 exists with the sibling ADRs' required section set.
# =========================================================================== #
def test_adr_0018_exists():
    """R5.1: ADR-0018 exists at the conventional .kiro/decisions path."""
    assert _ADR.is_file(), f"ADR-0018 missing at {_ADR} (R5.1)"


@pytest.mark.parametrize("heading", _REQUIRED_HEADINGS)
def test_adr_0018_has_required_heading(heading):
    """R5.1: ADR-0018 carries each heading the sibling ADRs (0013/0017) use."""
    headings = _headings(_adr_text())
    assert heading in headings, (
        f"ADR-0018 must have a '{heading}' section heading (R5.1) — matching the "
        f"heading set the sibling ADRs 0013/0017 use; found headings: {headings!r}"
    )


@pytest.mark.parametrize("field", _REQUIRED_META)
def test_adr_0018_has_required_metadata_line(field):
    """R5.1: ADR-0018 carries the Status/Date/Deciders metadata lines the sibling
    ADRs use (bold `**Field**:` lines, not `###` headings)."""
    text = _adr_text()
    assert re.search(rf"^\*\*{re.escape(field)}\*\*\s*:", text, re.MULTILINE), (
        f"ADR-0018 must carry the '**{field}**:' metadata line (R5.1), matching "
        "the sibling ADRs 0013/0017"
    )


def test_adr_0018_alternatives_under_options_considered():
    """R5.2: the rejected alternatives live under 'Options Considered' (the
    sibling ADRs' alternatives heading), not scattered loosely."""
    headings = _headings(_adr_text())
    assert "Options Considered" in headings, (
        "ADR-0018 must record alternatives under an 'Options Considered' heading "
        "(R5.2), matching the sibling ADRs 0013/0017"
    )


# =========================================================================== #
# R5.2 — rejected alternatives recorded (Docker Swarm overlay; co-location).
# =========================================================================== #
def test_adr_0018_records_rejected_swarm_overlay():
    """R5.2: the ADR records the rejected Docker Swarm overlay alternative."""
    norm = _norm(_adr_text())
    assert "swarm" in norm, (
        "ADR-0018 must record the rejected Docker 'Swarm' overlay alternative "
        "(R5.2)"
    )


def test_adr_0018_records_rejected_colocation():
    """R5.2: the ADR records the rejected co-location-on-the-Traefik-guest
    alternative."""
    norm = _norm(_adr_text())
    assert "co-locat" in norm or "colocat" in norm, (
        "ADR-0018 must record the rejected alternative of co-locating all HTTP "
        "backends on the Traefik guest (R5.2)"
    )
    # The co-location must be named against the Traefik guest specifically.
    assert "traefik guest" in norm or "traefik_guest" in norm or "traefik-guest" in norm, (
        "the co-location rejection must name the Traefik guest as the shared "
        "placement target (R5.2)"
    )


# =========================================================================== #
# R5.4 — Traefik sole LAN-facing ingress + VLAN-20 port is internal plumbing.
# =========================================================================== #
def test_adr_0018_states_traefik_sole_lan_facing_ingress():
    """R5.4: the ADR states Traefik remains the sole LAN-facing ingress."""
    norm = _norm(_adr_text())
    assert "sole lan-facing ingress" in norm or "only lan-facing ingress" in norm or (
        "lan-facing ingress" in norm and "traefik" in norm
    ), (
        "ADR-0018 must state Traefik remains the sole LAN-facing ingress (R5.4)"
    )


def test_adr_0018_states_port_is_internal_plumbing_not_second_ingress():
    """R5.4: the ADR states the VLAN-20-internal port is internal plumbing, not a
    second ingress."""
    norm = _norm(_adr_text())
    assert "internal plumbing" in norm, (
        "ADR-0018 must describe the VLAN20_Port as internal plumbing (R5.4)"
    )
    assert "not a second ingress" in norm, (
        "ADR-0018 must state the VLAN20_Port is NOT a second ingress (R5.4)"
    )


# =========================================================================== #
# R5.3 — constitution Decision Log references ADR-0018 + links the spec dir.
# =========================================================================== #
def test_constitution_decision_log_references_adr_0018():
    """R5.3: project-constitution.md references ADR-0018 (by its filename stem)."""
    text = _constitution_text()
    assert _ADR_STEM in text or "ADR-0018" in text, (
        f"project-constitution.md Decision Log must reference the ADR-0018 row "
        f"({_ADR_STEM!r} or 'ADR-0018') (R5.3)"
    )


def test_constitution_decision_log_links_spec_dir():
    """R5.3: the ADR-0018 Decision Log row links this spec's directory."""
    text = _constitution_text()
    assert _SPEC_DIR_LINK in text, (
        f"project-constitution.md Decision Log row must link the spec dir "
        f"{_SPEC_DIR_LINK!r} (R5.3)"
    )


def test_constitution_adr_0018_row_colocates_stem_and_spec_link():
    """R5.3: the ADR-0018 reference and the spec-dir link live on the SAME
    Decision Log table row (not merely both-present-somewhere)."""
    # Decision Log rows are single pipe-delimited lines; find the line carrying
    # the ADR stem and assert the spec-dir link is on that same line.
    rows = [
        ln for ln in _constitution_text().splitlines()
        if _ADR_STEM in ln or "ADR-0018" in ln
    ]
    assert rows, "no Decision Log line references ADR-0018 (R5.3)"
    assert any(_SPEC_DIR_LINK in ln for ln in rows), (
        "the ADR-0018 Decision Log row must carry BOTH the ADR reference and the "
        f"spec-dir link {_SPEC_DIR_LINK!r} on the same row (R5.3)"
    )


# =========================================================================== #
# Cross-links — the ADR's #[[file:...]] references resolve to existing files.
# =========================================================================== #
def test_adr_0018_file_crosslinks_resolve():
    """The ADR's #[[file:...]] cross-links (ADR-0013, ADR-0004) resolve to files
    that actually exist on disk."""
    text = _adr_text()
    links = re.findall(r"#\[\[file:([^\]]+)\]\]", text)
    assert links, "ADR-0018 must carry #[[file:...]] cross-links to its prior ADRs"

    # The two prior ADRs the task names must both be referenced.
    joined = " ".join(links)
    assert "0013-" in joined, "ADR-0018 must cross-link ADR-0013 via #[[file:...]]"
    assert "0004-" in joined, "ADR-0018 must cross-link ADR-0004 via #[[file:...]]"

    for rel in links:
        target = _REPO_ROOT / rel.strip()
        assert target.is_file(), (
            f"ADR-0018 cross-link #[[file:{rel}]] must resolve to an existing "
            f"file; {target} is missing"
        )
