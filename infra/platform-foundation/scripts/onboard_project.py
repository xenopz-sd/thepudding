#!/usr/bin/env python3
"""Onboard a new project onto the network foundation.

This CI-runnable script performs the *first step* of the onboarding flow
described in the feature design's "Onboarding / CI merge-request flow" sequence:

1. read the ``projects.yaml`` VLAN-ID registry (NET-00 §7, Requirement 5.1);
2. abort if the slug is already present (Requirement 5.3, Property 7);
3. compute the next sequential, never-reused VLAN ID via the pure derivation
   layer (``netfoundation.derivation.next_vlan_id``) rather than reimplementing
   the allocation math (Requirements 1.5, 1.6, 9.1);
4. abort if the VLAN-ID space is exhausted (Requirement 9.2, catches
   ``VlanExhaustionError``);
5. append ``{slug, vlan_id, onboarding_date}`` to the registry
   (Requirement 5.2, onboarding_date = today, ISO 8601 ``YYYY-MM-DD``);
6. open or update a GitLab merge request carrying that diff (Requirement 5.2).

**This script never mutates live infrastructure.** It only edits the registry
file and opens/updates a merge request. The actual VLAN (VNet/subnet) is created
later by the project root's ``terraform apply``, gated by the MR review
(design "Onboarding / CI merge-request flow"). There is deliberately no
``terraform``/``pvesh``/Proxmox API call anywhere in this module.

The GitLab MR interaction is kept behind an injectable seam
(:class:`MergeRequestOpener` / :class:`GitLabMergeRequestOpener`) so task 2.3's
unit tests can mock it — no live MR is ever created in a test. Credentials are
read from the GitLab CI environment (``CI_API_V4_URL``, ``CI_PROJECT_ID``,
``CI_JOB_TOKEN`` or ``GITLAB_TOKEN``) and are NEVER hardcoded, logged, or echoed.

Only the Python standard library plus PyYAML (already a project dependency,
per the venv) are used; the GitLab API call uses ``urllib`` from stdlib.

Invoke via the project venv binary by absolute path, e.g.::

    ~/venv/devinfra/bin/python infra/platform-foundation/scripts/onboard_project.py --slug my-project

(set ``PYTHONPATH`` to include this ``scripts`` dir so ``netfoundation`` resolves).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from netfoundation.derivation import VlanExhaustionError, next_vlan_id

# --- Decision-trail logging (composition-wiring steering) ----------------
#
# Per the composition-wiring steering, the decision an operator most needs to
# see — which VLAN ID was allocated, or WHY onboarding aborted — is logged at
# INFO level so it appears in the CI job's captured output, not only in the
# final stdout line. Messages carry an explicit, greppable decision prefix:
#
#   ALLOCATION  a slug was allocated a new VLAN ID and the registry + MR updated
#   ABORT       a recoverable refusal (duplicate/invalid slug, unreadable or
#               malformed registry, or an MR failure) — nothing was assigned
#   EXHAUSTION  the VLAN-ID space (…-254) is full; no ID is reused or wrapped
#
# The logger NEVER receives a credential: the GitLab token is only ever read
# into MergeRequestConfig.token and sent as an HTTP header; it is not passed to
# any logging call anywhere in this module.
_LOG = logging.getLogger("onboard_project")

#: Explicit, greppable decision-prefix strings for the operator-facing trail.
_PREFIX_ALLOCATION = "ALLOCATION"
_PREFIX_ABORT = "ABORT"
_PREFIX_EXHAUSTION = "EXHAUSTION"

# --- Slug validation (PF §4, Requirement 5.3) ----------------------------

#: A project slug is lowercase, hyphen-separated alphanumeric segments, and at
#: most 20 characters (PF §4). It must start and end with an alphanumeric so a
#: leading/trailing/doubled hyphen is rejected.
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SLUG_MAX_LEN = 20

#: Default location of the registry relative to this script:
#: scripts/ -> platform-foundation/projects.yaml
_DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "projects.yaml"


class OnboardingError(Exception):
    """A recoverable onboarding failure that aborts without side effects.

    Raised for duplicate slugs, invalid slugs, exhaustion, and malformed or
    unreadable registries. The CLI turns this into a non-zero exit with a
    clear message (Requirements 5.3, 9.2, FD.2); nothing is written and no MR
    is opened when it is raised.
    """


def validate_slug(slug: str) -> str:
    """Validate and return a project slug (lowercase-hyphenated, <=20 chars).

    Args:
        slug: The candidate slug.

    Returns:
        The validated slug, unchanged.

    Raises:
        OnboardingError: If the slug is empty, too long, or not
            lowercase-hyphenated (PF §4).
    """
    if not slug:
        raise OnboardingError("Slug must not be empty.")
    if len(slug) > _SLUG_MAX_LEN:
        raise OnboardingError(
            f"Slug '{slug}' is {len(slug)} characters; the maximum is "
            f"{_SLUG_MAX_LEN} (PF §4)."
        )
    if not _SLUG_PATTERN.match(slug):
        raise OnboardingError(
            f"Slug '{slug}' is invalid: it must be lowercase alphanumeric "
            "segments separated by single hyphens, with no leading, trailing, "
            "or doubled hyphens (PF §4)."
        )
    return slug


# --- Registry read / parse (Requirement 5.1, FD.2) -----------------------


def load_registry(registry_path: Path) -> dict[str, Any]:
    """Read and parse the ``projects.yaml`` registry.

    Args:
        registry_path: Path to the registry file.

    Returns:
        The parsed registry mapping with a ``projects`` list.

    Raises:
        OnboardingError: If the file is missing, unreadable, not valid YAML,
            or does not match the expected ``{projects: [...]}`` schema
            (FD.2 — abort and report without assigning).
    """
    try:
        raw = registry_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise OnboardingError(
            f"Registry file not found: {registry_path}."
        ) from exc
    except OSError as exc:
        raise OnboardingError(
            f"Registry file could not be read: {registry_path} ({exc})."
        ) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise OnboardingError(
            f"Registry file is not valid YAML: {registry_path} ({exc})."
        ) from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise OnboardingError(
            f"Registry root must be a mapping with a 'projects' list; "
            f"got {type(data).__name__}."
        )
    projects = data.get("projects")
    if projects is None:
        projects = []
    if not isinstance(projects, list):
        raise OnboardingError(
            "Registry 'projects' key must be a list; "
            f"got {type(projects).__name__}."
        )
    for i, entry in enumerate(projects):
        if not isinstance(entry, dict):
            raise OnboardingError(
                f"Registry entry #{i} is not a mapping: {entry!r}."
            )
        if "slug" not in entry or "vlan_id" not in entry:
            raise OnboardingError(
                f"Registry entry #{i} is missing a 'slug' or 'vlan_id': "
                f"{entry!r}."
            )
    data["projects"] = projects
    return data


def existing_slugs(registry: dict[str, Any]) -> list[str]:
    """Return the slugs already recorded in the registry."""
    return [entry["slug"] for entry in registry["projects"]]


def existing_vlan_ids(registry: dict[str, Any]) -> list[int]:
    """Return the VLAN IDs already recorded in the registry.

    Raises:
        OnboardingError: If any recorded ``vlan_id`` is not an integer, which
            would corrupt the allocation math (FD.2).
    """
    ids: list[int] = []
    for entry in registry["projects"]:
        vlan_id = entry["vlan_id"]
        if not isinstance(vlan_id, int) or isinstance(vlan_id, bool):
            raise OnboardingError(
                f"Registry entry for slug '{entry.get('slug')}' has a "
                f"non-integer vlan_id: {vlan_id!r}."
            )
        ids.append(vlan_id)
    return ids


# --- Allocation (Requirements 1.5, 1.6, 5.3, 9.2) ------------------------


@dataclass(frozen=True)
class Allocation:
    """The computed result of onboarding a new slug."""

    slug: str
    vlan_id: int
    onboarding_date: str


@dataclass(frozen=True)
class OnboardingResult:
    """The outcome of an onboarding run.

    Attributes:
        allocation: The :class:`Allocation` that was recorded.
        mr_opened: Whether a merge request was actually opened/updated. Truthy
            for a non-empty opener dict from ``GitLabMergeRequestOpener`` (an MR
            was opened/updated) and False for ``_NullMergeRequestOpener``'s empty
            ``{}`` (no MR was opened).
    """

    allocation: Allocation
    mr_opened: bool


def allocate(
    registry: dict[str, Any],
    slug: str,
    *,
    today: datetime.date | None = None,
) -> Allocation:
    """Compute the VLAN-ID allocation for a new slug (no side effects).

    Args:
        registry: The parsed registry mapping.
        slug: The already-validated new project slug.
        today: The onboarding date; defaults to the current UTC date. Injected
            for deterministic testing.

    Returns:
        The :class:`Allocation` (slug, vlan_id, ISO-8601 onboarding_date).

    Raises:
        OnboardingError: If the slug is already present (Requirement 5.3,
            Property 7).
        VlanExhaustionError: If the VLAN-ID space is exhausted
            (Requirement 9.2) — surfaced from the derivation layer and caught
            by the caller/CLI.
    """
    if slug in existing_slugs(registry):
        raise OnboardingError(
            f"Slug '{slug}' is already present in the registry; refusing to "
            "allocate a second VLAN ID for it (Requirement 5.3)."
        )
    vlan_id = next_vlan_id(existing_vlan_ids(registry))
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()
    return Allocation(
        slug=slug,
        vlan_id=vlan_id,
        onboarding_date=today.isoformat(),
    )


def append_entry(registry_path: Path, allocation: Allocation) -> str:
    """Append the allocation to the registry file and return the new content.

    The append preserves the existing file (comments and formatting) and adds a
    new block-style entry at the end of the ``projects:`` list, matching the
    existing hand-authored style so the merge-request diff is minimal and
    reviewable. Every appended row carries ``status: active`` (Requirement 4.6,
    the Desired_State_View lifecycle field from C1) — a newly onboarded project
    is active by definition; decommissioning is a later ``status`` flip in a
    reviewable MR, never a row removal. Allocation math is unaffected:
    ``existing_vlan_ids``/``allocate`` continue to count all rows regardless of
    status, so a decommissioned ID is never reused.

    Args:
        registry_path: Path to the registry file (must already exist).
        allocation: The computed allocation to record.

    Returns:
        The full new file content that was written.

    Raises:
        OnboardingError: If the file cannot be written.
    """
    existing = registry_path.read_text(encoding="utf-8")
    body = existing if existing.endswith("\n") else existing + "\n"
    entry = (
        f"  - slug: {allocation.slug}\n"
        f"    vlan_id: {allocation.vlan_id}\n"
        f'    onboarding_date: "{allocation.onboarding_date}"\n'
        f"    status: active\n"
    )
    new_content = body + entry
    try:
        registry_path.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        raise OnboardingError(
            f"Failed to write registry file {registry_path}: {exc}."
        ) from exc
    return new_content


# --- GitLab merge-request seam (Requirement 5.2) -------------------------


@dataclass(frozen=True)
class MergeRequestConfig:
    """Configuration for opening/updating a GitLab merge request.

    Populated from GitLab CI environment variables. ``token`` is a secret and
    MUST NOT be logged or echoed anywhere.
    """

    api_url: str
    project_id: str
    token: str
    source_branch: str
    target_branch: str
    title: str

    @staticmethod
    def from_env(
        allocation: Allocation,
        env: dict[str, str] | None = None,
    ) -> "MergeRequestConfig":
        """Build the config from CI environment variables.

        Reads ``CI_API_V4_URL``, ``CI_PROJECT_ID``, and a token from
        ``GITLAB_TOKEN`` or ``CI_JOB_TOKEN``. The branch names default to CI's
        ``CI_COMMIT_REF_NAME`` (source) and ``CI_DEFAULT_BRANCH`` (target) when
        present.

        Raises:
            OnboardingError: If a required variable is absent (so the failure
                is a clear, surfaced message rather than a stray ``KeyError``).
                Note the token variable name is reported, never its value.
        """
        env = dict(os.environ if env is None else env)
        api_url = env.get("CI_API_V4_URL", "")
        project_id = env.get("CI_PROJECT_ID", "")
        token = env.get("GITLAB_TOKEN") or env.get("CI_JOB_TOKEN") or ""
        missing = []
        if not api_url:
            missing.append("CI_API_V4_URL")
        if not project_id:
            missing.append("CI_PROJECT_ID")
        if not token:
            missing.append("GITLAB_TOKEN or CI_JOB_TOKEN")
        if missing:
            raise OnboardingError(
                "Cannot open a merge request: missing GitLab CI environment "
                f"variable(s): {', '.join(missing)}."
            )
        source_branch = env.get(
            "CI_COMMIT_REF_NAME", f"onboard-{allocation.slug}"
        )
        target_branch = env.get("CI_DEFAULT_BRANCH", "main")
        return MergeRequestConfig(
            api_url=api_url.rstrip("/"),
            project_id=project_id,
            token=token,
            source_branch=source_branch,
            target_branch=target_branch,
            title=(
                f"onboard: {allocation.slug} -> VLAN {allocation.vlan_id}"
            ),
        )


class MergeRequestOpener(Protocol):
    """Injectable seam for opening/updating a merge request.

    Implementations perform the actual side effect. Tests inject a fake so no
    live MR is ever created (design Testing Strategy §3, task 2.3).
    """

    def open_or_update(self, allocation: Allocation) -> dict[str, Any]:
        """Open (or update an existing) MR for the allocation; return its info."""
        ...


class GitLabMergeRequestOpener:
    """Opens/updates a GitLab MR via the REST API using stdlib ``urllib``.

    The API token is read from the environment via :class:`MergeRequestConfig`
    and sent in the ``PRIVATE-TOKEN``/``JOB-TOKEN`` header; it is never logged.
    If a merge request from ``source_branch`` to ``target_branch`` already
    exists, its title is updated (open/update semantics, Requirement 5.2);
    otherwise a new one is created.
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env

    def _headers(self, config: MergeRequestConfig) -> dict[str, str]:
        # CI_JOB_TOKEN uses the JOB-TOKEN header; a personal/group token uses
        # PRIVATE-TOKEN. We set whichever is present without echoing the value.
        env = dict(os.environ if self._env is None else self._env)
        header = (
            "PRIVATE-TOKEN" if env.get("GITLAB_TOKEN") else "JOB-TOKEN"
        )
        return {header: config.token, "Content-Type": "application/json"}

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Report status and URL, never the token.
            raise OnboardingError(
                f"GitLab API {method} {url} failed with HTTP "
                f"{exc.code}: {exc.reason}."
            ) from exc
        except urllib.error.URLError as exc:
            raise OnboardingError(
                f"GitLab API {method} {url} could not be reached: "
                f"{exc.reason}."
            ) from exc
        return json.loads(body) if body else {}

    def open_or_update(self, allocation: Allocation) -> dict[str, Any]:
        config = MergeRequestConfig.from_env(allocation, self._env)
        headers = self._headers(config)
        base = (
            f"{config.api_url}/projects/{config.project_id}/merge_requests"
        )
        # Look for an existing open MR on the same source branch.
        list_url = (
            f"{base}?state=opened&source_branch={config.source_branch}"
        )
        existing = self._request("GET", list_url, headers)
        if isinstance(existing, list) and existing:
            iid = existing[0]["iid"]
            update_url = f"{base}/{iid}"
            return self._request(
                "PUT",
                update_url,
                headers,
                {"title": config.title},
            )
        return self._request(
            "POST",
            base,
            headers,
            {
                "source_branch": config.source_branch,
                "target_branch": config.target_branch,
                "title": config.title,
            },
        )


# --- Orchestration (pure of I/O policy; side effects are injected) -------


def onboard(
    slug: str,
    registry_path: Path,
    mr_opener: MergeRequestOpener,
    *,
    today: datetime.date | None = None,
) -> OnboardingResult:
    """Run the full onboarding side-effect sequence for a slug.

    Validates the slug, loads and checks the registry, computes the allocation,
    appends it to the registry file, then opens/updates the merge request via
    the injected ``mr_opener``. Never touches live infrastructure.

    Args:
        slug: The raw slug from the CLI (validated here).
        registry_path: Path to ``projects.yaml``.
        mr_opener: The injected merge-request seam (mocked in tests).
        today: Optional injected date for deterministic tests.

    Returns:
        The :class:`OnboardingResult` carrying the recorded ``allocation`` and
        ``mr_opened`` — the latter is ``bool()`` of the opener's return value,
        so it is truthy for a non-empty API-response dict from
        ``GitLabMergeRequestOpener`` (an MR was opened/updated) and False for
        ``_NullMergeRequestOpener``'s empty ``{}`` (no MR was opened).

    Raises:
        OnboardingError: On any recoverable abort (invalid/duplicate slug,
            unreadable/malformed registry, MR failure).
        VlanExhaustionError: When the VLAN-ID space is exhausted.
    """
    slug = validate_slug(slug)
    registry = load_registry(registry_path)
    allocation = allocate(registry, slug, today=today)
    append_entry(registry_path, allocation)
    mr_result = mr_opener.open_or_update(allocation)
    return OnboardingResult(allocation=allocation, mr_opened=bool(mr_result))


# --- CLI ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI parser."""
    parser = argparse.ArgumentParser(
        prog="onboard_project.py",
        description=(
            "Onboard a new project: compute its next sequential VLAN ID, "
            "append it to the projects.yaml registry, and open/update a "
            "GitLab merge request with the diff. Never mutates live "
            "infrastructure (registry edit + MR only)."
        ),
    )
    parser.add_argument(
        "--slug",
        required=True,
        help=(
            "New project slug (lowercase-hyphenated, <=20 chars). Must not "
            "already be present in the registry."
        ),
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=_DEFAULT_REGISTRY,
        help=(
            "Path to the projects.yaml VLAN-ID registry "
            f"(default: {_DEFAULT_REGISTRY})."
        ),
    )
    parser.add_argument(
        "--no-mr",
        action="store_true",
        help=(
            "Skip opening the merge request (allocate + append registry "
            "only). Useful for local dry runs outside CI."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help=(
            "Emit the DEBUG-level decision trail as well as the INFO-level "
            "ALLOCATION/ABORT/EXHAUSTION messages."
        ),
    )
    return parser


class _NullMergeRequestOpener:
    """A no-op MR seam used when --no-mr is passed (no live MR, no CI env)."""

    def open_or_update(self, allocation: Allocation) -> dict[str, Any]:
        print(
            "  (--no-mr) skipped opening a merge request for "
            f"'{allocation.slug}'."
        )
        return {}


def _configure_logging(verbose: bool) -> None:
    """Configure the decision-trail logger to write to stderr.

    Per the composition-wiring steering, the application's own loggers must be
    visible (INFO minimum) — otherwise the ALLOCATION/ABORT/EXHAUSTION trail is
    silently discarded. Logging goes to STDERR so it never interleaves with the
    machine-parseable final stdout line, and so a downstream consumer can
    capture the human decision trail separately from the result. Idempotent:
    it will not stack duplicate handlers if ``main`` is called more than once
    (e.g. in tests).
    """
    level = logging.DEBUG if verbose else logging.INFO
    if not _LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        _LOG.addHandler(handler)
    _LOG.setLevel(level)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code.

    Emits an explicit, greppable decision trail (ALLOCATION / ABORT /
    EXHAUSTION) at INFO/ERROR level to stderr, plus the existing final result
    line to stdout on success and an ``ERROR: …`` line to stderr on failure.
    The success path exits 0, a recoverable abort exits 1, and VLAN-ID
    exhaustion exits 2 — so a CI job can distinguish "full address space" from
    "bad input" by exit code alone. No credential is ever logged.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    _LOG.debug(
        "onboarding requested for slug=%r using registry=%s (open MR: %s)",
        args.slug,
        args.registry,
        not args.no_mr,
    )

    mr_opener: MergeRequestOpener = (
        _NullMergeRequestOpener() if args.no_mr else GitLabMergeRequestOpener()
    )

    try:
        result = onboard(args.slug, args.registry, mr_opener)
        allocation = result.allocation
    except VlanExhaustionError as exc:
        # The VLAN-ID space is full: surface it explicitly and never reuse or
        # wrap an ID (Requirement 9.2). Distinct exit code (2) and prefix so a
        # CI job can alert on exhaustion specifically.
        _LOG.error("%s: %s", _PREFIX_EXHAUSTION, exc)
        print(f"ERROR: {_PREFIX_EXHAUSTION}: {exc}", file=sys.stderr)
        return 2
    except OnboardingError as exc:
        # A recoverable refusal (duplicate/invalid slug, unreadable/malformed
        # registry, or MR failure): nothing was assigned (Requirements 5.3,
        # FD.2). Actionable message, exit 1.
        _LOG.error("%s: %s", _PREFIX_ABORT, exc)
        print(f"ERROR: {_PREFIX_ABORT}: {exc}", file=sys.stderr)
        return 1

    # Success: state slug + vlan_id + date on the decision trail AND the final
    # stdout result line. The MR clause is selected from what actually
    # happened (result.mr_opened), not from args.no_mr, so the decision trail
    # never overclaims an MR that was not opened.
    if result.mr_opened:
        mr_clause_info = (
            "registry updated and merge request opened/updated for review."
        )
        mr_clause_stdout = (
            "Registry updated; merge request opened/updated for review."
        )
    else:
        mr_clause_info = "registry updated; no merge request was opened."
        mr_clause_stdout = "Registry updated; no merge request was opened."

    _LOG.info(
        "%s: slug '%s' -> VLAN %d (onboarding_date %s); %s",
        _PREFIX_ALLOCATION,
        allocation.slug,
        allocation.vlan_id,
        allocation.onboarding_date,
        mr_clause_info,
    )
    print(
        f"Onboarded '{allocation.slug}': VLAN {allocation.vlan_id}, "
        f"onboarding_date {allocation.onboarding_date}. {mr_clause_stdout}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
