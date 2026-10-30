"""Offline tests for the CI Root_Discovery helper (scripts/ci/discover_tf_roots.py).

Feature: ci-offline-gate-hardening (design §Testing Strategy 8.1, Property 1).

Parses nothing live — it drives the pure-stdlib discovery helper over the real
`infra/` tree and over synthesized `tmp_path` trees. Lives in the `infra/tests`
pytest root (its natural home — it reads the `infra/` tree) and imports the helper
from `scripts/ci/` via an explicit sys.path insert, so NO conftest change is needed
(the Four_Root_Rule is unaffected — this file only runs under the `infra/tests`
invocation).

Two layers:
* EXPLORATION / FIX-CHECKING — the backend-marker classifier is total (root iff a
  backend block; module excluded), the 9 known roots are discovered, the 1 module
  excluded, nested dronefleet/db + parent both present, fail-loud on zero roots.
* PROPERTY 1 (Hypothesis, >=100 iterations) — over random generated trees,
  discover_roots returns EXACTLY the dirs marked "has backend block".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
    _HYPOTHESIS = True
except Exception:  # pragma: no cover - hypothesis is a dev dep, present in CI
    _HYPOTHESIS = False

# infra/tests/test_discover_tf_roots.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_DIR = _REPO_ROOT / "scripts" / "ci"
if str(_CI_DIR) not in sys.path:
    sys.path.insert(0, str(_CI_DIR))

import discover_tf_roots  # noqa: E402  (sys.path set above)

_HELPER = _CI_DIR / "discover_tf_roots.py"

# The design's pinned on-disk baseline: NINE roots, ONE excluded module.
_KNOWN_ROOTS = {
    "infra/platform-foundation",
    "infra/projects/_TEMPLATE",
    "infra/projects/dronefleet",
    "infra/projects/dronefleet/db",
    "infra/projects/svc-01-sql-database",
    "infra/projects/svc-06-identity-provider",
    "infra/projects/svc-07-secrets-manager",
    "infra/projects/svc-09-reverse-proxy",
    "infra/projects/svc-17-18-observability",
}
_EXCLUDED_MODULE = "infra/modules/proxmox-compute"

_BACKEND_VERSIONS_TF = (
    'terraform {\n'
    '  required_version = ">= 1.6"\n'
    '  backend "http" {}\n'
    '  required_providers {\n'
    '    proxmox = { source = "bpg/proxmox" }\n'
    '  }\n'
    '}\n'
)
_MODULE_VERSIONS_TF = (
    'terraform {\n'
    '  required_version = ">= 1.6"\n'
    '  required_providers {\n'
    '    proxmox = { source = "bpg/proxmox" }\n'
    '  }\n'
    '}\n'
)


# --------------------------------------------------------------------------- #
# EXPLORATION / known-roots baseline (Requirement 3.4; design §5.1)
# --------------------------------------------------------------------------- #
def test_known_roots_discovered_against_real_tree():
    """All nine known roots are discovered against the real infra/ tree."""
    roots = set(discover_tf_roots.discover_roots(_REPO_ROOT / "infra"))
    # Normalize to repo-relative for comparison.
    rel = {Path(r).resolve().relative_to(_REPO_ROOT).as_posix() for r in
           discover_tf_roots.discover_roots(str(_REPO_ROOT / "infra"))}
    missing = _KNOWN_ROOTS - rel
    assert not missing, f"Root_Discovery missed known roots: {sorted(missing)}"


def test_module_excluded_against_real_tree():
    """infra/modules/proxmox-compute (no backend block) is excluded."""
    rel = {Path(r).resolve().relative_to(_REPO_ROOT).as_posix() for r in
           discover_tf_roots.discover_roots(str(_REPO_ROOT / "infra"))}
    assert _EXCLUDED_MODULE not in rel, (
        f"{_EXCLUDED_MODULE} is a MODULE (no backend block) and must be excluded."
    )


def test_nested_dronefleet_db_and_parent_both_present():
    """Nested roots are not pruned: dronefleet AND dronefleet/db both present."""
    rel = {Path(r).resolve().relative_to(_REPO_ROOT).as_posix() for r in
           discover_tf_roots.discover_roots(str(_REPO_ROOT / "infra"))}
    assert "infra/projects/dronefleet" in rel
    assert "infra/projects/dronefleet/db" in rel


def test_output_is_stable_sorted():
    """The helper returns a stable-sorted list (deterministic order, NFR-2)."""
    out = discover_tf_roots.discover_roots(str(_REPO_ROOT / "infra"))
    assert out == sorted(out), "discover_roots output must be stable-sorted."
    # And the known roots are a subset, in sorted order.
    assert len(out) == len(set(out)), "no duplicate roots."


# --------------------------------------------------------------------------- #
# SYNTHESIZED-TREE assertions (Requirements 3.2, 3.3)
# --------------------------------------------------------------------------- #
def test_dummy_root_detected(tmp_path: Path):
    """A dummy root (versions.tf with a backend block) added under a tmp infra
    tree is discovered WITHOUT any .gitlab-ci.yml edit (Req 3.2)."""
    infra = tmp_path / "infra"
    newroot = infra / "projects" / "svc-99-dummy"
    newroot.mkdir(parents=True)
    (newroot / "versions.tf").write_text(_BACKEND_VERSIONS_TF, encoding="utf-8")
    (newroot / "main.tf").write_text('resource "null_resource" "x" {}\n', encoding="utf-8")
    roots = discover_tf_roots.discover_roots(infra)
    assert any(r.endswith("projects/svc-99-dummy") for r in roots), (
        f"dummy root not discovered: {roots}"
    )


def test_dummy_module_excluded(tmp_path: Path):
    """A dummy module (*.tf, no backend block) under infra/modules is excluded (Req 3.3)."""
    infra = tmp_path / "infra"
    mod = infra / "modules" / "dummy-mod"
    mod.mkdir(parents=True)
    (mod / "versions.tf").write_text(_MODULE_VERSIONS_TF, encoding="utf-8")
    (mod / "main.tf").write_text('variable "x" {}\n', encoding="utf-8")
    # Also add a real root so discovery does not fail-loud on zero.
    root = infra / "projects" / "real"
    root.mkdir(parents=True)
    (root / "versions.tf").write_text(_BACKEND_VERSIONS_TF, encoding="utf-8")
    roots = discover_tf_roots.discover_roots(infra)
    assert not any("modules/dummy-mod" in r for r in roots), (
        f"dummy module must be excluded: {roots}"
    )
    assert any(r.endswith("projects/real") for r in roots)


def test_backend_word_in_comment_only_is_excluded(tmp_path: Path):
    """A .tf whose ONLY 'backend' occurrence is in a comment is NOT a root
    (comment-robustness, design §3.1.1)."""
    infra = tmp_path / "infra"
    d = infra / "modules" / "commented"
    d.mkdir(parents=True)
    # The word 'backend' appears ONLY in comments — both # and //.
    (d / "versions.tf").write_text(
        'terraform {\n'
        '  # this module intentionally declares no backend "http" block\n'
        '  // state lives in the calling backend root\n'
        '  required_version = ">= 1.6"\n'
        '}\n',
        encoding="utf-8",
    )
    # A real root so discovery is non-empty.
    root = infra / "projects" / "real"
    root.mkdir(parents=True)
    (root / "versions.tf").write_text(_BACKEND_VERSIONS_TF, encoding="utf-8")
    roots = discover_tf_roots.discover_roots(infra)
    assert not any("modules/commented" in r for r in roots), (
        "a 'backend' mention only in comments must not classify a dir as a root."
    )


def test_block_comment_backend_is_excluded(tmp_path: Path):
    """A 'backend \"http\" {' inside a /* ... */ block comment is NOT a root."""
    infra = tmp_path / "infra"
    d = infra / "modules" / "blockcommented"
    d.mkdir(parents=True)
    (d / "versions.tf").write_text(
        'terraform {\n'
        '  /* example only:\n'
        '     backend "http" {}\n'
        '  */\n'
        '  required_version = ">= 1.6"\n'
        '}\n',
        encoding="utf-8",
    )
    root = infra / "projects" / "real"
    root.mkdir(parents=True)
    (root / "versions.tf").write_text(_BACKEND_VERSIONS_TF, encoding="utf-8")
    roots = discover_tf_roots.discover_roots(infra)
    assert not any("modules/blockcommented" in r for r in roots)


# --------------------------------------------------------------------------- #
# FAIL-LOUD on zero roots (design §Error Handling)
# --------------------------------------------------------------------------- #
def test_cli_fail_loud_on_zero_roots(tmp_path: Path):
    """The CLI exits non-zero when zero roots are found (never a false green)."""
    infra = tmp_path / "infra"
    (infra / "modules" / "only-a-module").mkdir(parents=True)
    (infra / "modules" / "only-a-module" / "versions.tf").write_text(
        _MODULE_VERSIONS_TF, encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, str(_HELPER), str(infra)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, "CLI must exit non-zero on zero roots."
    assert "ZERO Terraform roots" in result.stderr


def test_cli_lists_roots_one_per_line(tmp_path: Path):
    """The CLI prints each root one per line, stable-sorted, exit 0."""
    infra = tmp_path / "infra"
    for name in ("b-root", "a-root"):
        d = infra / "projects" / name
        d.mkdir(parents=True)
        (d / "versions.tf").write_text(_BACKEND_VERSIONS_TF, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_HELPER), str(infra)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines == sorted(lines), "CLI output must be stable-sorted."
    assert any(ln.endswith("projects/a-root") for ln in lines)
    assert any(ln.endswith("projects/b-root") for ln in lines)


def test_nonexistent_infra_dir_fails(tmp_path: Path):
    """A missing infra dir fails (NotADirectoryError -> CLI non-zero)."""
    missing = tmp_path / "does-not-exist"
    with pytest.raises(NotADirectoryError):
        discover_tf_roots.discover_roots(missing)
    result = subprocess.run(
        [sys.executable, str(_HELPER), str(missing)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0


# --------------------------------------------------------------------------- #
# PROPERTY 1 — discovery totality (Hypothesis, >=100 iterations)
# Feature: ci-offline-gate-hardening, Property 1: discover_roots returns exactly
# the dirs that contain a backend-block .tf (root iff backend marker).
# Validates: Requirements 1.2, 1.3, 1.4, 1.6, 3.1, 3.3
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HYPOTHESIS, reason="hypothesis not installed")
@settings(max_examples=120, deadline=None)
@given(
    specs=st.lists(
        st.tuples(
            # a unique-ish relative dir path (1-3 segments of safe names)
            st.lists(st.sampled_from(["a", "b", "c", "d", "proj", "mod", "svc"]),
                     min_size=1, max_size=3),
            st.booleans(),  # has_backend_block
        ),
        min_size=1, max_size=12,
    )
)
def test_property1_discovery_totality(tmp_path_factory, specs):
    """For any generated tree, discover_roots returns EXACTLY the backend-block dirs."""
    infra = tmp_path_factory.mktemp("infra_prop") / "infra"
    infra.mkdir(parents=True)
    expected: set[str] = set()
    # Build the tree; dedupe by path so the same dir is written once (last flag wins).
    by_path: dict[str, bool] = {}
    for segs, has_backend in specs:
        rel = "/".join(segs)
        by_path[rel] = has_backend
    for rel, has_backend in by_path.items():
        d = infra / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "versions.tf").write_text(
            _BACKEND_VERSIONS_TF if has_backend else _MODULE_VERSIONS_TF,
            encoding="utf-8",
        )
        if has_backend:
            expected.add((infra / rel).as_posix())
    got = set(discover_tf_roots.discover_roots(infra))
    assert got == expected, (
        f"totality violated: got {sorted(got)} expected {sorted(expected)}"
    )
