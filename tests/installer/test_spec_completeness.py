"""Offline drift-guard: every requirements.md is spec-complete (WS-M6).

Feature: spec-completeness-guard (WS-M6, enforcing half).

Spec: .kiro/specs/spec-completeness-guard/ (requirements.md FR-1..FR-4; design.md
"The guard"). Traces to PLANNING.md WS-M6 and cross-cutting-categories.md (the
seven mandatory categories) + service-installer.md §5 (a SERVICE spec's Day-0
install/teardown AC).

WHY THIS FILE EXISTS (the risk it closes)
-----------------------------------------
Two spec-quality conventions are mandated in prose but not mechanically checked:
`cross-cutting-categories.md` requires every requirements.md to address all SEVEN
cross-cutting categories, and `service-installer.md` §5 requires a SERVICE spec to
carry a Day-0 install/teardown acceptance criterion. Nothing enforced them, so a
new spec can silently omit a category (five legacy specs did when the 7th category
was added) or a service spec can ship with no Day-0/teardown AC. This guard
enforces both at merge (CI-pytest), going-forward, with the pre-WS-M6 corpus
grandfathered (same adoption pattern as the WS-M3 live-acceptance gate).

The Day-0 check is CONDITIONAL and keys on what the spec DECLARES ABOUT ITSELF: a
spec whose `operator install/test documentation` cross-cutting line self-declares
the "not applicable — no requires-infra tests" form is offline/methodology (not a
service) and is EXEMPT from the Day-0 requirement — so the guard never imposes a
false Day-0 requirement on a pure-offline spec (the false-positive trap).

The helpers below are COPIED locally (NOT imported across test trees -- the
conftest-collision rule), mirroring test_validation_completeness.py (glob
discovery, repo-root via parents[2], a documented allowlist asserted non-vacuous +
non-rotting, self-consistent detection). Pure markdown parse: offline, no
requires_infra marker, runs in the default offline gate.

Validates: FR-1..FR-4 / AC-1..AC-7.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/installer/test_spec_completeness.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_REQUIREMENTS_GLOB = ".kiro/specs/*/requirements.md"

# A known category-complete + Day-0-exempt-or-present spec (non-vacuity anchor).
_NON_VACUITY_ANCHOR = ".kiro/specs/svc-17-18-observability/requirements.md"

# --------------------------------------------------------------------------- #
# GRANDFATHER ALLOWLIST (WS-M6 adoption — self-consistent via the detector below).
# --------------------------------------------------------------------------- #
# Blanket reason for every entry: pre-WS-M6 corpus -- these requirements.md
# predate the spec-completeness check (four were authored before the 7th
# cross-cutting category, "operator install/test documentation", existed; one is a
# deliberately-incomplete BACKLOG STUB). They are tracked for reconciliation (add
# the missing category) as a WS-M6 follow-up, NOT force-fixed here. The guard
# enforces GOING-FORWARD. As a spec is reconciled, REMOVE it from this set -- the
# no-rot test flags a now-compliant entry. The set shrinks monotonically to empty.
# Computed self-consistently by the guard's OWN _is_compliant detector over the
# corpus (never a separate survey), so the allowlist and the live check cannot
# drift (FR-4). Per-entry reason recorded inline: either the pre-7th-category
# specs missing "operator install/test documentation", the oss-doc BACKLOG STUB
# missing all seven, or a pre-WS-M6 service-adjacent spec that carries no explicit
# Day-0/teardown AC (and does not self-declare the offline N/A form).
_GRANDFATHERED = frozenset(
    {
        ".kiro/specs/ci-offline-gate-hardening/requirements.md",  # no Day-0/teardown AC
        ".kiro/specs/fix-openbao-onboard-registry-read/requirements.md",  # no Day-0/teardown AC
        ".kiro/specs/fix-sdn-privilege-model/requirements.md",  # missing: operator install/test documentation
        ".kiro/specs/fix-vlan-host-index-collision/requirements.md",  # no Day-0/teardown AC
        ".kiro/specs/lxc-docker-base-template/requirements.md",  # missing: operator install/test documentation
        ".kiro/specs/openbao-admin-token-bootstrap/requirements.md",  # no Day-0/teardown AC
        ".kiro/specs/openbao-workload-auth/requirements.md",  # no Day-0/teardown AC
        ".kiro/specs/oss-doc-baseline-followup/requirements.md",  # BACKLOG STUB — missing all seven
        ".kiro/specs/proxmox-compute-deterministic-vmid/requirements.md",  # no Day-0/teardown AC
        ".kiro/specs/proxmox-network-foundation/requirements.md",  # missing: operator install/test documentation
        ".kiro/specs/service-install-and-docs-conventions/requirements.md",  # no Day-0/teardown AC
        ".kiro/specs/svc-07-secrets-manager/requirements.md",  # missing: operator install/test documentation
        ".kiro/specs/terraform-ansible-handoff/requirements.md",  # no Day-0/teardown AC
        ".kiro/specs/traefik-cross-guest-ingress/requirements.md",  # no Day-0/teardown AC
    }
)


# --------------------------------------------------------------------------- #
# Detection -- COPIED locally (no cross-tree import; conftest-collision rule).
# --------------------------------------------------------------------------- #
# The seven cross-cutting categories (cross-cutting-categories.md). Each is matched
# by ANY of its tolerant alternatives over the lowercased text -- the guard checks
# the category is ADDRESSED, not its exact heading.
_CATEGORY_TOKENS = {
    "multi-tenancy / VLAN isolation": (r"multi-?tenancy", r"vlan isolation"),
    "self-hosted & EU-sovereignty": (r"eu[- ]sovereignty", r"sovereignty posture"),
    "Terraform state isolation": (r"terraform state isolation",),
    "secrets & credential handling": (r"secrets?.*credential", r"secrets?.*handling"),
    "naming & addressing": (r"naming.*addressing",),
    "deployment-unit": (r"deployment-?unit",),
    "operator install/test documentation": (
        r"operator install/?test",
        r"install/?test documentation",
        r"operator.*documentation",
    ),
}

# Day-0 / teardown acceptance-criterion tokens.
_DAY0_TOKENS = (r"day-?0", r"clean[- ]slate", r"teardown", r"developer b",
                r"automated .*installation")


def _missing_categories(text: str) -> list[str]:
    """The cross-cutting categories NOT addressed anywhere in the text."""
    n = text.lower()
    missing = []
    for cat, toks in _CATEGORY_TOKENS.items():
        if not any(re.search(tok, n) for tok in toks):
            missing.append(cat)
    return missing


def _category_complete(text: str) -> bool:
    return not _missing_categories(text)


def _needs_day0(text: str) -> bool:
    """True iff the Day-0/teardown AC is REQUIRED for this spec.

    Conditional: a spec whose operator-install-doc cross-cutting line self-declares
    the "not applicable — no requires-infra tests" form is offline/methodology (not
    a service) and is EXEMPT. We detect that N/A self-declaration by a
    `not applicable` co-located (within a small window) with a `requires.infra`
    mention -- the exact convention cross-cutting-categories.md's template uses.
    """
    n = text.lower()
    for m in re.finditer(r"not applicable", n):
        window = n[m.start(): m.end() + 80]
        if re.search(r"requires.infra", window):
            return False  # self-declared offline/non-service -> Day-0 exempt
    return True


def _has_day0(text: str) -> bool:
    n = text.lower()
    return any(re.search(tok, n) for tok in _DAY0_TOKENS)


def _is_compliant(text: str) -> bool:
    """A requirements.md is compliant iff category-complete AND (Day-0-exempt OR
    carries a Day-0 AC)."""
    if not _category_complete(text):
        return False
    if _needs_day0(text) and not _has_day0(text):
        return False
    return True


def _iter_requirements_files():
    for path in sorted(_REPO_ROOT.glob(_REQUIREMENTS_GLOB)):
        yield path.relative_to(_REPO_ROOT).as_posix(), path


# --------------------------------------------------------------------------- #
# Test 1 (FR-1) -- every non-grandfathered spec addresses all seven categories.
# --------------------------------------------------------------------------- #
def test_spec_is_category_complete():
    """Every non-grandfathered requirements.md addresses all 7 cross-cutting cats.

    Validates: FR-1 / AC-2/AC-3. cross-cutting-categories.md mandates all seven for
    every spec; this enforces it going-forward (the pre-WS-M6 corpus is
    grandfathered).
    """
    offenders: list[str] = []
    for rel, path in _iter_requirements_files():
        if rel in _GRANDFATHERED:
            continue
        missing = _missing_categories(path.read_text(encoding="utf-8"))
        if missing:
            offenders.append(f"{rel} missing {missing}")

    assert not offenders, (
        "The following requirements.md do NOT address all seven cross-cutting "
        f"categories (cross-cutting-categories.md): {offenders}. Add the missing "
        "category line(s) under 'Mandatory Cross-Cutting Analysis'. (If this is "
        "pre-WS-M6 legacy debt, it belongs in _GRANDFATHERED — but prefer adding "
        "the category.)"
    )


# --------------------------------------------------------------------------- #
# Test 2 (FR-2) -- service specs carry a Day-0/teardown AC (offline specs exempt).
# --------------------------------------------------------------------------- #
def test_service_specs_have_day0_ac():
    """Every non-grandfathered SERVICE spec carries a Day-0/teardown AC.

    Validates: FR-2 / AC-3. A spec is a 'service' for this check unless its
    operator-install-doc cross-cutting line self-declares the "not applicable — no
    requires-infra tests" form; such offline/methodology specs are EXEMPT (so the
    guard never imposes a false Day-0 requirement on a non-service spec).
    """
    offenders: list[str] = []
    for rel, path in _iter_requirements_files():
        if rel in _GRANDFATHERED:
            continue
        text = path.read_text(encoding="utf-8")
        if _needs_day0(text) and not _has_day0(text):
            offenders.append(rel)

    assert not offenders, (
        "The following SERVICE requirements.md declare requires-infra scope (their "
        "operator-install-doc line is NOT the 'not applicable — no requires-infra "
        f"tests' form) but carry no Day-0/teardown acceptance criterion: {offenders}. "
        "Add a Day-0 install / clean-slate/teardown AC (service-installer.md §5), or "
        "— if this spec is genuinely offline — self-declare the N/A form in its "
        "operator-install-doc cross-cutting line."
    )


# --------------------------------------------------------------------------- #
# Test 3 (FR-3) -- the grandfather set has not rotted.
# --------------------------------------------------------------------------- #
def test_grandfather_set_has_not_rotted():
    """Every grandfathered path still exists AND is still non-compliant.

    Validates: FR-3 / AC-4. A reconciled spec (now compliant) must be REMOVED from
    _GRANDFATHERED so the gate enforces it going-forward; a renamed/removed path
    must be dropped. Either rot fails here.
    """
    missing: list[str] = []
    now_compliant: list[str] = []
    for rel in sorted(_GRANDFATHERED):
        path = _REPO_ROOT / rel
        if not path.is_file():
            missing.append(rel)
            continue
        if _is_compliant(path.read_text(encoding="utf-8")):
            now_compliant.append(rel)

    assert not missing, (
        f"Grandfathered requirements.md path(s) no longer exist: {missing}. Remove "
        "them from _GRANDFATHERED (a spec dir was renamed/removed)."
    )
    assert not now_compliant, (
        "Grandfathered requirements.md file(s) are now spec-complete: "
        f"{now_compliant}. They appear reconciled — REMOVE them from _GRANDFATHERED "
        "so the guard enforces them going-forward (do not leave a stale allowlist "
        "entry that could later mask a re-introduced omission)."
    )


# --------------------------------------------------------------------------- #
# Test 4 (FR-3) -- the guard is non-vacuous.
# --------------------------------------------------------------------------- #
def test_guard_is_non_vacuous():
    """A known compliant spec is discovered AND passes both checks.

    Validates: non-vacuity. Guards against a glob/parse regression making the gate
    a silent no-op: the svc-17-18-observability requirements.md must be discovered,
    be category-complete, and (it is a service spec) carry a Day-0 AC.
    """
    seen = {rel for rel, _ in _iter_requirements_files()}
    assert _NON_VACUITY_ANCHOR in seen, (
        f"Expected {_NON_VACUITY_ANCHOR} to be discovered by the requirements.md "
        "glob — not seen; the spec layout may have moved, repoint this guard."
    )
    anchor_text = (_REPO_ROOT / _NON_VACUITY_ANCHOR).read_text(encoding="utf-8")
    assert _category_complete(anchor_text), (
        f"{_NON_VACUITY_ANCHOR} is expected to be category-complete (non-vacuity "
        "anchor) but the detector found missing categories "
        f"{_missing_categories(anchor_text)} — the category tokens may have drifted."
    )
    assert _is_compliant(anchor_text), (
        f"{_NON_VACUITY_ANCHOR} is expected to be fully compliant (non-vacuity "
        "anchor) but the detector flagged it — the detector may have drifted."
    )
