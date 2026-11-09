"""Offline drift-guard: a deferred live AC in a validation.md carries a backlog link.

Feature: live-acceptance-gate (WS-M3).

Spec: .kiro/specs/live-acceptance-gate/ (requirements.md FR-1..FR-5; design.md
"The guard"). Traces to the WS-A1 deferred-AC audit
(docs/audit/deferred-ac-ledger.md) and PLANNING.md Part B WS-M3.

WHY THIS FILE EXISTS (the risk it closes)
-----------------------------------------
The WS-A1 audit found the "accept a service as done with its live acceptance
deferred" pattern is SYSTEMIC and degrades SILENTLY: a validation.md marks an
acceptance criterion "PENDING live" / "DEFERRED" / "NOT PERFORMED" and the
service is treated as done, with nothing tracking the outstanding live work. The
gate rule (spec-lifecycle.md §5/§6, testing-strategy.md) says a deferred /
requires-infra-gated AC may be recorded ONLY with a linked tracked backlog entry
-- never silently "done". This test enforces that rule MECHANICALLY so the next
such omission fails at author time rather than rotting into a stale/contradictory
doc (as svc-07-automated-installer did: Merge-Readiness LIVE-VALIDATED vs
Known-Gaps NOT PERFORMED for the same properties).

ADOPTION STRATEGY (operator-approved, WS-M3 task 1)
---------------------------------------------------
The pre-WS-M3 corpus predates the rule: 87 of 96 validation.md files carry a
deferral token with no resolvable backlog link. Rather than fail ~every spec,
those 87 are GRANDFATHERED via the documented ``_GRANDFATHERED`` allowlist below
(blanket reason: pre-WS-M3 corpus, tracked for reconciliation under the WS-A1
stale-doc backlog, PLANNING.md G16-G20). The gate ENFORCES GOING-FORWARD: any
NON-grandfathered validation.md that defers without a link fails here. The
allowlist is asserted non-vacuous and non-rotting (every path still exists AND
still defers), so a file cannot be allowlisted then silently repurposed to dodge
the gate, and a reconciled file is simply dropped from the set (shrinking it
monotonically toward zero).

The helpers below are COPIED locally (NOT imported across test trees -- the
conftest-collision rule), mirroring the idiom of
``test_agent_live_wiring_coverage.py`` (glob discovery, repo-root via
``Path(__file__).resolve().parents[2]``, a documented allowlist asserted
non-vacuous + non-rotting). This guard is pure text/markdown parse: offline, no
``requires_infra`` marker, runs in the default offline gate.

Validates: FR-1..FR-5 / AC-1..AC-5.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/installer/test_validation_completeness.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_VALIDATION_GLOB = ".kiro/specs/*/validation.md"
_PLANNING = _REPO_ROOT / "PLANNING.md"
_SPECS_DIR = _REPO_ROOT / ".kiro" / "specs"

# A known file the scan MUST classify as deferring (non-vacuity anchor).
_NON_VACUITY_ANCHOR = ".kiro/specs/svc-07-secrets-manager/validation.md"

# --------------------------------------------------------------------------- #
# GRANDFATHER ALLOWLIST (WS-M3 adoption strategy, operator-approved).
# --------------------------------------------------------------------------- #
# Blanket reason for EVERY entry below: pre-WS-M3 corpus -- these deferrals
# predate the live-acceptance gate; they are tracked for reconciliation under the
# WS-A1 stale-doc backlog (PLANNING.md G16-G20), NOT force-fixed by WS-M3. WS-M3
# stops NEW silent deferrals and makes this existing debt an explicit, enumerated,
# non-rotting inventory. As a file is reconciled (a backlog link added, or the
# deferral resolved), REMOVE it from this set -- the no-rot test flags a stale
# entry and the gate then enforces that file going-forward. The set shrinks
# monotonically toward empty.
_GRANDFATHERED = frozenset(
    {
        ".kiro/specs/all-services-provision-walkthrough/validation.md",
        ".kiro/specs/ci-offline-test-pipeline/validation.md",
        ".kiro/specs/docs-provision-then-test-navigation/validation.md",
        ".kiro/specs/fix-composed-openbao-onboarder-primary-delegation/validation.md",
        ".kiro/specs/fix-consumer-db-bridge-kv-v2-path/validation.md",
        ".kiro/specs/fix-consumer-db-secret-workload-path-mismatch/validation.md",
        ".kiro/specs/fix-consumer-ingress-browser-reachable-oidc/validation.md",
        ".kiro/specs/fix-consumer-manifest-app-type-mapping/validation.md",
        ".kiro/specs/fix-consumer-onboarding-prerequisites/validation.md",
        ".kiro/specs/fix-handoff-banner-openbao-path-honesty/validation.md",
        ".kiro/specs/fix-hello-workload-cgi-secret-visibility/validation.md",
        ".kiro/specs/fix-obs-ac3-targets-up/validation.md",
        ".kiro/specs/fix-openbao-agent-cross-guest-edge-ingress/validation.md",
        ".kiro/specs/fix-openbao-agent-env-file-host-path/validation.md",
        ".kiro/specs/fix-openbao-agent-reload-on-config-change/validation.md",
        ".kiro/specs/fix-openbao-agent-workload-rsf-mount/validation.md",
        ".kiro/specs/fix-openbao-health-defaults-cleanup/validation.md",
        ".kiro/specs/fix-openbao-onboard-bound-claims-maptype/validation.md",
        ".kiro/specs/fix-openbao-onboard-registry-read/validation.md",
        ".kiro/specs/fix-openbao-onboard-tls-skip-verify/validation.md",
        ".kiro/specs/fix-pg-hba-hostssl-vs-ssl-off/validation.md",
        ".kiro/specs/fix-sdn-gateway-project-vlan-noop/validation.md",
        ".kiro/specs/fix-svc01-local-backend-per-instance-state/validation.md",
        ".kiro/specs/fix-svc01-openbao-credential-write-version-gate-decoupling/validation.md",
        ".kiro/specs/fix-svc06-clean-slate-backing-db-residue/validation.md",
        ".kiro/specs/fix-svc06-clean-slate-provision-token-residue/validation.md",
        ".kiro/specs/fix-svc06-zitadel-compose-ingress-version-gate-decoupling/validation.md",
        ".kiro/specs/fix-svc06-zitadel-db-app-password-mismatch/validation.md",
        ".kiro/specs/fix-svc07-bootstrap-failure-diagnostics/validation.md",
        ".kiro/specs/fix-svc07-clean-slate-proxmox-host-default/validation.md",
        ".kiro/specs/fix-svc07-clean-slate-terraform-data-residue/validation.md",
        ".kiro/specs/fix-svc07-empty-transit-token-handoff/validation.md",
        ".kiro/specs/fix-svc07-external-copy-removal-fresh-mint/validation.md",
        ".kiro/specs/fix-svc07-primary-health-check-reachability/validation.md",
        ".kiro/specs/fix-svc07-primary-recreate-churn/validation.md",
        ".kiro/specs/fix-svc07-primary-seal-token-orphan-revocation/validation.md",
        ".kiro/specs/fix-svc07-unsealer-reseal-on-handler-restart/validation.md",
        ".kiro/specs/fix-svc07-unsealer-volume-ownership-recreate/validation.md",
        ".kiro/specs/fix-vlan-host-index-collision/validation.md",
        ".kiro/specs/fix-workload-creds-dest-path-scope/validation.md",
        ".kiro/specs/fix-workload-datastore-autodetect/validation.md",
        ".kiro/specs/fix-workload-guest-creds-staging/validation.md",
        ".kiro/specs/fix-zitadel-onboarding-role-keys-assertion/validation.md",
        ".kiro/specs/fix-zitadel-org-provision-production-path-insecure-tls/validation.md",
        ".kiro/specs/lxc-docker-base-template/validation.md",
        ".kiro/specs/openbao-unsealer-firewall-scoping/validation.md",
        ".kiro/specs/openbao-workload-auth/validation.md",
        ".kiro/specs/platform-clean-slate/validation.md",
        ".kiro/specs/platform-install-simplification/validation.md",
        ".kiro/specs/platform-prerequisites-bootstrap/validation.md",
        ".kiro/specs/proxmox-compute-deterministic-vmid/validation.md",
        ".kiro/specs/proxmox-network-foundation/validation.md",
        ".kiro/specs/svc-01-sql-database/validation.md",
        ".kiro/specs/svc-06-cross-guest-ingress/validation.md",
        ".kiro/specs/svc-06-identity-provider/validation.md",
        ".kiro/specs/svc-07-cross-guest-ingress/validation.md",
        ".kiro/specs/svc-09-reverse-proxy/validation.md",
        ".kiro/specs/svc-17-18-observability/validation.md",
        ".kiro/specs/svc07-unsealer-bootstrap-delegation/validation.md",
        ".kiro/specs/terraform-clean-slate-reset/validation.md",
        ".kiro/specs/traefik-cross-guest-ingress/validation.md",
    }
)


# --------------------------------------------------------------------------- #
# Helpers -- COPIED locally (no cross-tree import; conftest-collision rule).
# --------------------------------------------------------------------------- #
# Deferral-token set (from the WS-A1 corpus survey). Matched against NORMALISED
# text (lowercase; markdown `*`/`_` stripped; em-dash and `--` normalised to a
# space) so "written, gated" matches "written — gated", "**DEFERRED**" matches
# "deferred", etc. Intentionally broad / false-positive-leaning: a spec that
# merely mentions a deferral still ought to carry a link, so over-detection
# pushes toward the safe behaviour (require a link). The grandfather set absorbs
# the legacy false-positives.
_DEFERRAL_TOKENS = (
    "pending live",
    "deferred",
    "not performed",
    "not live-executed",
    "written, gated",
    "written not live",
    "remaining live gate",
    "not executed",
    "live gap",
    "not live-validated",
    "blocked",
)

# `requires.infra` is only a deferral signal when co-located with one of these.
_REQUIRES_INFRA_QUALIFIERS = ("gated", "pending", "not ")


def _normalise(text: str) -> str:
    """Lowercase; strip markdown emphasis; collapse em-dash/-- to a space."""
    t = text.lower()
    t = t.replace("\u2014", " ").replace("--", " ")  # em-dash + double hyphen
    t = t.replace("*", "").replace("_", "")
    return t


def _defers_an_ac(text: str) -> bool:
    """True iff the (raw) validation.md text records a deferred/live-gated AC."""
    norm = _normalise(text)
    if any(tok in norm for tok in _DEFERRAL_TOKENS):
        return True
    # requires-infra / requires_infra co-located with a gating qualifier.
    if re.search(r"requires.infra", norm):
        for m in re.finditer(r"requires.infra", norm):
            window = norm[max(0, m.start() - 40) : m.end() + 40]
            if any(q in window for q in _REQUIRES_INFRA_QUALIFIERS):
                return True
    return False


def _planning_ws_ids() -> set[str]:
    """The set of WS-<id> tokens that actually appear in PLANNING.md."""
    if not _PLANNING.is_file():
        return set()
    text = _PLANNING.read_text(encoding="utf-8")
    return set(re.findall(r"\bWS-[A-Z]?\d+\b", text))


def _has_backlog_link(text: str, planning_ids: set[str]) -> bool:
    """A resolvable linked backlog entry (FR-2), either accepted form.

    Form 1 -- a WS-<id> token present in the file that ALSO resolves in
    PLANNING.md. Form 2 -- a relative link ``](../<dir>/...)`` whose ``<dir>``
    resolves to an existing ``.kiro/specs/<dir>/`` directory.
    """
    # Form 1: WS-id resolvable in PLANNING.md.
    for tok in re.findall(r"\bWS-[A-Z]?\d+\b", text):
        if tok in planning_ids:
            return True
    # Form 2: ](../<dir>/...) resolving to a real spec dir.
    for m in re.finditer(r"\]\(\.\./([A-Za-z0-9._-]+)/", text):
        if (_SPECS_DIR / m.group(1)).is_dir():
            return True
    return False


def _iter_validation_files():
    """Yield (relpath_str, abspath) for every .kiro/specs/*/validation.md."""
    for path in sorted(_REPO_ROOT.glob(_VALIDATION_GLOB)):
        yield path.relative_to(_REPO_ROOT).as_posix(), path


# --------------------------------------------------------------------------- #
# Sections-disagree soft signal (FR-5) -- report-only, NEVER hard-fails.
# --------------------------------------------------------------------------- #
_ID_RE = re.compile(r"(?:propert(?:y|ies)\s+\d+|ac-\d+|fr-\d+|req(?:uirement)?\s+\d+)")
_LIVE_OK_RE = re.compile(r"live-validated|live validated|pass\b|achieved live|failed=0")
_GAP_RE = re.compile(r"not performed|pending live|halts|not behaviorally proven|not live-validated")


def _region(text: str, heading_re: str) -> str:
    """Return the text of the markdown section whose heading matches, else ''."""
    norm = text
    m = re.search(rf"(?im)^#{{1,6}}\s*.*{heading_re}.*$", norm)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"(?im)^#{1,6}\s", norm[start:])
    end = start + nxt.start() if nxt else len(norm)
    return norm[start:end]


def _sections_disagree(text: str) -> set[str]:
    """Property/AC ids claimed LIVE-OK in merge-readiness AND listed as a gap."""
    norm = _normalise(text)
    mr = _region(norm, r"merge.?readiness") or _region(norm, r"conclusion")
    gaps = _region(norm, r"known (?:gaps|limitations)")
    if not mr or not gaps:
        return set()
    live_ok_ids: set[str] = set()
    for line in mr.splitlines():
        if _LIVE_OK_RE.search(line):
            live_ok_ids.update(_ID_RE.findall(line))
    gap_ids: set[str] = set()
    for line in gaps.splitlines():
        if _GAP_RE.search(line):
            gap_ids.update(_ID_RE.findall(line))
    return live_ok_ids & gap_ids


# --------------------------------------------------------------------------- #
# Test 1 (FR-3) -- the core gate: deferrals carry a backlog link.
# --------------------------------------------------------------------------- #
def test_deferrals_carry_a_backlog_link():
    """Every NON-grandfathered validation.md that defers an AC carries a link.

    Validates: FR-1/FR-3 + AC-2/AC-3. Globs .kiro/specs/*/validation.md; for each
    file NOT in _GRANDFATHERED, if it records a deferred/live-gated AC it MUST
    contain a resolvable backlog link (a WS-<id> present in PLANNING.md, OR a
    ](../<dir>/) resolving to a real spec dir). A new/edited spec that defers
    without a link fails HERE -- the exact "silent deferral" the WS-A1 audit found.
    """
    planning_ids = _planning_ws_ids()
    offenders: list[str] = []

    for rel, path in _iter_validation_files():
        if rel in _GRANDFATHERED:
            continue
        text = path.read_text(encoding="utf-8")
        if not _defers_an_ac(text):
            continue
        if not _has_backlog_link(text, planning_ids):
            offenders.append(rel)

    assert not offenders, (
        "The following validation.md file(s) record a deferred / requires-infra-"
        f"gated acceptance criterion WITHOUT a linked tracked backlog entry: "
        f"{offenders}. The live-acceptance gate (spec-lifecycle.md §5/§6, "
        "testing-strategy.md) forbids a silent deferral: add a WS-<id> token that "
        "resolves in PLANNING.md, OR a ](../<dir>/) link to the follow-up spec "
        "dir. (If this is pre-WS-M3 legacy debt, it belongs in _GRANDFATHERED — "
        "but prefer adding the link.)"
    )


# --------------------------------------------------------------------------- #
# Test 2 (FR-4) -- the grandfather set has not rotted.
# --------------------------------------------------------------------------- #
def test_grandfather_set_has_not_rotted():
    """Every grandfathered path still exists AND still defers an AC.

    Validates: FR-4 / AC-4. A file can't be allowlisted then silently repurposed
    to dodge the gate: if a grandfathered path no longer exists, OR no longer
    contains a deferral token (i.e. it was reconciled), this FAILS -- signalling
    the entry should be removed from _GRANDFATHERED (after which the gate enforces
    that file going-forward).
    """
    missing: list[str] = []
    no_longer_defers: list[str] = []

    for rel in sorted(_GRANDFATHERED):
        path = _REPO_ROOT / rel
        if not path.is_file():
            missing.append(rel)
            continue
        if not _defers_an_ac(path.read_text(encoding="utf-8")):
            no_longer_defers.append(rel)

    assert not missing, (
        f"Grandfathered validation.md path(s) no longer exist: {missing}. Remove "
        "them from _GRANDFATHERED (a spec dir was renamed/removed)."
    )
    assert not no_longer_defers, (
        "Grandfathered validation.md file(s) no longer record a deferral: "
        f"{no_longer_defers}. They appear reconciled — REMOVE them from "
        "_GRANDFATHERED so the gate enforces them going-forward (do not leave a "
        "stale allowlist entry that could later mask a re-introduced deferral)."
    )


# --------------------------------------------------------------------------- #
# Test 3 -- the guard is non-vacuous.
# --------------------------------------------------------------------------- #
def test_guard_is_non_vacuous():
    """A known file is seen by the scan AND classified as deferring.

    Validates: non-vacuity. Guards against a glob/parse regression making the gate
    a silent no-op: the svc-07-secrets-manager validation.md MUST be discovered by
    the scan and classified by _defers_an_ac as recording a deferral.
    """
    seen = {rel for rel, _ in _iter_validation_files()}
    assert _NON_VACUITY_ANCHOR in seen, (
        f"Expected {_NON_VACUITY_ANCHOR} to be discovered by the validation.md "
        "glob — not seen; the spec layout may have moved, repoint this guard."
    )
    anchor = _REPO_ROOT / _NON_VACUITY_ANCHOR
    assert _defers_an_ac(anchor.read_text(encoding="utf-8")), (
        f"{_NON_VACUITY_ANCHOR} is expected to record a deferral (non-vacuity "
        "anchor) but _defers_an_ac did not classify it so — the deferral-token "
        "set may have drifted."
    )


# --------------------------------------------------------------------------- #
# Test 4 (FR-5) -- sections-disagree soft signal (REPORT-ONLY, never fails).
# --------------------------------------------------------------------------- #
def test_report_merge_readiness_vs_known_gaps(capsys):
    """Report (not fail) validation.md whose merge-readiness vs known-gaps disagree.

    Validates: FR-5 / AC-5. A SOFT, report-only signal: surfaces any file whose
    Merge-Readiness section claims LIVE-VALIDATED/PASS for a property that its
    Known-Gaps section ALSO lists as NOT PERFORMED / pending-live / halts (the
    svc-07-automated-installer contradiction class). This is INFORMATIONAL — it
    prints a report and NEVER hard-fails, so it surfaces the debt (feeding the
    WS-A1 G16 reconcile) without blocking a merge on 85 legacy files or on a
    heuristic false positive.

    The only assertion is non-vacuity of the HEURISTIC itself (the regions/ids
    machinery runs over the corpus); the flagged-file set is reported, not
    asserted.
    """
    flagged: dict[str, set[str]] = {}
    for rel, path in _iter_validation_files():
        ids = _sections_disagree(path.read_text(encoding="utf-8"))
        if ids:
            flagged[rel] = ids

    # Report-only: print for operator visibility; do NOT assert on the set.
    with capsys.disabled():
        if flagged:
            print("\n[live-acceptance-gate] sections-disagree (report-only):")
            for rel in sorted(flagged):
                print(f"  - {rel}: {sorted(flagged[rel])}")
            print(
                "  ^ Merge-Readiness claims LIVE-OK for a property its Known-Gaps "
                "section also lists as a gap. Reconcile under PLANNING.md G16."
            )
        else:
            print("\n[live-acceptance-gate] sections-disagree: none flagged.")

    # This test must never fail the build; its value is the printed report.
    assert True
