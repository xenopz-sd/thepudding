"""Contract-style tests for the Terraform state-name convention and the
VLAN-ID registry schema (task 8.3).

These are *not* property-based tests. They inspect the committed convention:

1. **State-name uniqueness** (Requirement 6.3) — each Terraform root declares a
   GitLab-managed HTTP backend state name that is unique across roots and
   distinct from ``platform-foundation``. Both roots use a *partial* backend
   (an empty ``backend "http" {}`` block) with the concrete state name supplied
   at ``terraform init`` time via ``-backend-config``. The state name therefore
   lives in the documented ``-backend-config`` address example / comment inside
   each ``versions.tf`` rather than in a literal HCL attribute. We assert on
   that committed convention robustly:
     - the foundation root documents state name ``platform-foundation``;
     - the project template documents the ``<slug>-infra`` state-name pattern;
     - the two differ.

2. **Registry schema validation** (Requirement 5.1) — every entry in
   ``infra/platform-foundation/projects.yaml`` validates against the schema:
     - ``slug``            lowercase-hyphenated, <=20 chars, unique;
     - ``vlan_id``         integer in 100-254 inclusive, globally unique;
     - ``onboarding_date`` ISO 8601 ``YYYY-MM-DD``.

Run with:  ~/venv/devinfra/bin/pytest infra/tests/test_state_and_registry.py
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import pytest
import yaml

# --------------------------------------------------------------------------- #
# Repo-relative paths.
#
# This file lives at  <repo>/infra/tests/test_state_and_registry.py
# so the repo root is three parents up.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_INFRA = _REPO_ROOT / "infra"

FOUNDATION_VERSIONS_TF = _INFRA / "platform-foundation" / "versions.tf"
TEMPLATE_VERSIONS_TF = _INFRA / "projects" / "_TEMPLATE" / "versions.tf"
REGISTRY_YAML = _INFRA / "platform-foundation" / "projects.yaml"

FOUNDATION_STATE_NAME = "platform-foundation"

# Schema constants (design.md "Data Models", Requirements 1.4/5.1/5.3/9.1).
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MAX_LEN = 20
VLAN_ID_MIN = 100
VLAN_ID_MAX = 254

# Lifecycle-status schema (sdn-vlan-gateway-reachability, Requirement 4.1):
# a row's ``status`` is one of these two values; a missing status is treated as
# ``active`` by readers (Requirement 5.2/FD.3) but the committed file always
# carries one.
VALID_STATUSES = ("active", "decommissioned")

# Terraform's GitLab-managed HTTP backend addresses end in
# ``/terraform/state/<state-name>`` (optionally ``/lock``). We recover the
# documented state name(s) from the committed ``-backend-config`` example.
_STATE_ADDR_RE = re.compile(
    r"/terraform/state/(?P<name>[A-Za-z0-9._<>-]+?)(?:/lock)?[\"'\s]"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _read(path: Path) -> str:
    assert path.is_file(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


def _documented_state_names(versions_tf_text: str) -> set[str]:
    """Return the set of state names recovered from the documented
    ``-backend-config`` address examples in a ``versions.tf`` file."""
    return {m.group("name") for m in _STATE_ADDR_RE.finditer(versions_tf_text)}


def _has_partial_http_backend(versions_tf_text: str) -> bool:
    """True if the file declares an (empty / partial) ``backend "http"`` block."""
    return re.search(r'backend\s+"http"\s*\{', versions_tf_text) is not None


def _load_registry() -> list[dict]:
    data = yaml.safe_load(_read(REGISTRY_YAML))
    assert isinstance(data, dict), "projects.yaml must be a mapping at the top level"
    projects = data.get("projects")
    assert isinstance(projects, list), "projects.yaml must have a 'projects:' list"
    return projects


# --------------------------------------------------------------------------- #
# State-name convention tests (Requirement 6.3)
# --------------------------------------------------------------------------- #
def test_versions_files_exist():
    assert FOUNDATION_VERSIONS_TF.is_file(), FOUNDATION_VERSIONS_TF
    assert TEMPLATE_VERSIONS_TF.is_file(), TEMPLATE_VERSIONS_TF


def test_both_roots_use_partial_http_backend():
    """Both roots must declare an HTTP backend (partial, filled at init)."""
    assert _has_partial_http_backend(_read(FOUNDATION_VERSIONS_TF)), (
        "foundation root must declare a backend \"http\" block"
    )
    assert _has_partial_http_backend(_read(TEMPLATE_VERSIONS_TF)), (
        "project template must declare a backend \"http\" block"
    )


def test_foundation_documents_platform_foundation_state_name():
    """The foundation root's documented state name is `platform-foundation`."""
    names = _documented_state_names(_read(FOUNDATION_VERSIONS_TF))
    assert FOUNDATION_STATE_NAME in names, (
        "foundation versions.tf should document the "
        f"'{FOUNDATION_STATE_NAME}' state name in its -backend-config example; "
        f"found {sorted(names)}"
    )
    # And it must NOT accidentally document a `<slug>-infra` pattern.
    infra_named = {n for n in names if n.endswith("-infra")}
    assert not infra_named, (
        "foundation root must not document a '<slug>-infra' state name; "
        f"found {sorted(infra_named)}"
    )


def test_template_documents_slug_infra_state_name_pattern():
    """The project template's documented state name follows `<slug>-infra`.

    The template is slug-agnostic, so the documented example uses the literal
    placeholder ``<slug>`` (i.e. ``<slug>-infra``). We assert the `-infra`
    suffix convention is present and that it is expressed as a placeholder
    pattern, not a hardcoded concrete slug.
    """
    text = _read(TEMPLATE_VERSIONS_TF)
    names = _documented_state_names(text)
    infra_named = {n for n in names if n.endswith("-infra")}
    assert infra_named, (
        "project template versions.tf should document a '<slug>-infra' state "
        f"name in its -backend-config example; found {sorted(names)}"
    )
    # Every documented project state name must be the placeholder pattern,
    # never a concrete/hardcoded slug baked into the template.
    assert "<slug>-infra" in infra_named, (
        "template must document the placeholder pattern '<slug>-infra', "
        f"not a concrete slug; found {sorted(infra_named)}"
    )


def test_foundation_and_project_state_names_differ():
    """`platform-foundation` differs from the `<slug>-infra` pattern."""
    foundation_names = _documented_state_names(_read(FOUNDATION_VERSIONS_TF))
    template_names = _documented_state_names(_read(TEMPLATE_VERSIONS_TF))

    assert FOUNDATION_STATE_NAME in foundation_names
    project_names = {n for n in template_names if n.endswith("-infra")}
    assert project_names, "template must document at least one '<slug>-infra' name"

    # No overlap: the foundation state name is never a project state name.
    assert FOUNDATION_STATE_NAME not in project_names
    assert not (foundation_names & project_names), (
        "foundation and project state-name sets must be disjoint; overlap="
        f"{sorted(foundation_names & project_names)}"
    )
    # The `<slug>-infra` pattern cannot resolve to `platform-foundation`
    # because `platform-foundation` does not end in `-infra`.
    assert not FOUNDATION_STATE_NAME.endswith("-infra")


def test_no_concrete_slug_hardcoded_in_template_backend():
    """The template must not bake a real project slug into its backend example.

    Any documented `<something>-infra` state name in the template must be the
    `<slug>` placeholder form, never a concrete slug from the live registry.
    """
    template_names = _documented_state_names(_read(TEMPLATE_VERSIONS_TF))
    registry_slugs = {p["slug"] for p in _load_registry()}
    for name in template_names:
        if name.endswith("-infra"):
            concrete = name[: -len("-infra")]
            assert concrete not in registry_slugs, (
                f"template backend example hardcodes concrete slug '{concrete}' "
                "instead of the '<slug>' placeholder"
            )


# --------------------------------------------------------------------------- #
# Registry schema tests (Requirement 5.1)
# --------------------------------------------------------------------------- #
def test_registry_is_nonempty_list_of_mappings():
    projects = _load_registry()
    assert projects, "projects.yaml 'projects:' list must not be empty"
    for entry in projects:
        assert isinstance(entry, dict), f"each registry entry must be a mapping: {entry!r}"


def test_registry_entries_have_exactly_the_expected_keys():
    # Required keys every row must carry. ``status`` became a written-on-every-row
    # field with the sdn-vlan-gateway-reachability feature (Requirement 4.1/4.3):
    # the three existing rows were backfilled with ``status: active`` and
    # ``onboard_project.append_entry`` now emits it on every new row. It is
    # therefore a required key here, even though it is *optional at read time*
    # (a missing status degrades to ``active`` per the registry-view layer,
    # Requirement 5.2/FD.3) — the file itself always carries it.
    required = {"slug", "vlan_id", "onboarding_date", "status"}
    # ``decommissioned_date`` (ISO 8601) is an OPTIONAL per-row key recorded only
    # when a row flips to ``status: decommissioned`` (Requirement 4.1). Rows may
    # carry it or not; no other keys are permitted (a stray key would hide a
    # typo'd field).
    allowed = required | {"decommissioned_date"}
    for entry in _load_registry():
        keys = set(entry.keys())
        assert required <= keys, f"entry missing required keys {required - keys}: {entry!r}"
        # No stray/unexpected keys that could hide a typo'd field.
        assert keys <= allowed, f"entry has unexpected keys {keys - allowed}: {entry!r}"


def test_registry_status_values_valid():
    """Every committed row's ``status`` is one of the two valid lifecycle
    values, and a ``decommissioned_date``, when present, is a real ISO 8601
    date (sdn-vlan-gateway-reachability, Requirement 4.1)."""
    for entry in _load_registry():
        status = entry["status"]
        assert isinstance(status, str), f"status must be a string: {entry!r}"
        assert status in VALID_STATUSES, (
            f"status '{status}' must be one of {VALID_STATUSES}: {entry!r}"
        )
        if "decommissioned_date" in entry:
            raw = entry["decommissioned_date"]
            assert isinstance(raw, str), (
                "decommissioned_date must be a quoted ISO 8601 string "
                f"(YYYY-MM-DD): {entry!r}"
            )
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", raw), (
                f"decommissioned_date '{raw}' must match YYYY-MM-DD"
            )
            _dt.date.fromisoformat(raw)


def test_registry_slugs_valid_and_unique():
    projects = _load_registry()
    seen: set[str] = set()
    for entry in projects:
        slug = entry["slug"]
        assert isinstance(slug, str), f"slug must be a string: {entry!r}"
        assert slug == slug.strip(), f"slug must not have surrounding whitespace: {slug!r}"
        assert len(slug) <= SLUG_MAX_LEN, (
            f"slug '{slug}' exceeds {SLUG_MAX_LEN} chars (len={len(slug)})"
        )
        assert SLUG_RE.match(slug), (
            f"slug '{slug}' must be lowercase-hyphenated (^[a-z0-9]+(-[a-z0-9]+)*$)"
        )
        assert slug not in seen, f"duplicate slug in registry: {slug!r}"
        seen.add(slug)


def test_registry_vlan_ids_valid_and_unique():
    projects = _load_registry()
    seen: set[int] = set()
    for entry in projects:
        vlan_id = entry["vlan_id"]
        # A YAML boolean is an int subclass in Python; reject it explicitly.
        assert isinstance(vlan_id, int) and not isinstance(vlan_id, bool), (
            f"vlan_id must be an integer: {entry!r}"
        )
        assert VLAN_ID_MIN <= vlan_id <= VLAN_ID_MAX, (
            f"vlan_id {vlan_id} out of range {VLAN_ID_MIN}-{VLAN_ID_MAX}: {entry!r}"
        )
        assert vlan_id not in seen, f"duplicate vlan_id in registry: {vlan_id}"
        seen.add(vlan_id)


def test_registry_onboarding_dates_iso_8601():
    for entry in _load_registry():
        raw = entry["onboarding_date"]
        assert isinstance(raw, str), (
            "onboarding_date must be a quoted ISO 8601 string (YYYY-MM-DD), "
            f"not an auto-parsed date: {entry!r}"
        )
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", raw), (
            f"onboarding_date '{raw}' must match YYYY-MM-DD"
        )
        # Must be a real calendar date.
        _dt.date.fromisoformat(raw)


def test_registry_cross_entry_uniqueness_summary():
    """Belt-and-suspenders: both slug and vlan_id are globally unique."""
    projects = _load_registry()
    slugs = [p["slug"] for p in projects]
    vlan_ids = [p["vlan_id"] for p in projects]
    assert len(slugs) == len(set(slugs)), "slugs are not globally unique"
    assert len(vlan_ids) == len(set(vlan_ids)), "vlan_ids are not globally unique"


# --------------------------------------------------------------------------- #
# Negative coverage: prove the schema validators actually reject bad data.
# These guard against a validator that is silently a no-op.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_slug",
    [
        "Has-Upper",              # uppercase
        "trailing-",             # trailing hyphen
        "-leading",              # leading hyphen
        "under_score",           # underscore
        "has space",             # whitespace
        "a" * (SLUG_MAX_LEN + 1),  # too long
        "",                       # empty
    ],
)
def test_slug_regex_rejects_invalid(bad_slug):
    valid = bool(SLUG_RE.match(bad_slug)) and 0 < len(bad_slug) <= SLUG_MAX_LEN
    assert not valid, f"slug validator wrongly accepted {bad_slug!r}"


@pytest.mark.parametrize("bad_vlan", [10, 20, 99, 255, 300, 0, -1])
def test_vlan_range_rejects_out_of_range(bad_vlan):
    assert not (VLAN_ID_MIN <= bad_vlan <= VLAN_ID_MAX)


@pytest.mark.parametrize("bad_date", ["2024-13-01", "2024-06-31", "24-06-01", "2024/06/01", "not-a-date"])
def test_date_validator_rejects_invalid(bad_date):
    formatted = bool(re.match(r"^\d{4}-\d{2}-\d{2}$", bad_date))
    real = True
    try:
        _dt.date.fromisoformat(bad_date)
    except ValueError:
        real = False
    assert not (formatted and real), f"date validator wrongly accepted {bad_date!r}"
