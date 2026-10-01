"""Offline link-integrity + convention checks for the accessible-root-README feature.

Spec: ``.kiro/specs/accessible-root-readme/`` (requirements.md / design.md /
tasks.md). This is the single automated evidence that the documentation-only
``accessible-root-readme`` feature is correct, per ``testing-strategy.md`` (tests
are the evidence a feature works) and the design's Testing Strategy section.

=== WHAT THIS IS ===

A **pure-offline** pytest (no cluster, no Terraform binary, no CI). It reads three
documents from the working tree —

  * ``README.md``            (the new Developer-B front door, "The Pudding"),
  * ``docs/README.md``       (the relocated Developer-A maintainer README),
  * ``docs/TESTING.md``      (the relocated Developer-A testing guide),

— and asserts the ten checks enumerated in design.md § Testing Strategy. It joins
the existing ``infra/tests/`` suite, so it is collected by the repository's
standard offline command::

    PYTHONPATH=infra/platform-foundation/scripts \\
      ~/venv/devinfra/bin/pytest \\
      infra/platform-foundation/scripts/tests/ infra/tests/ -q

It carries **no** ``requires_infra`` marker — it runs in the default (offline)
tier. The only external process it shells out to is ``git`` for the scope-guard
check (check 10); when ``git`` is unavailable or the diff cannot be computed, that
single check skips cleanly rather than failing.

=== THE TEN CHECKS (design.md § Testing Strategy) ===

  1. Moved files + root exist; root README is the Developer-B front door and no
     longer holds the Developer-A content (Req 1.1, 4.1, 4.2, 5.1, 5.2).
  2. Logo assets exist and the root README references exactly those paths
     (Req 3.1-3.6).
  3. ``docs/assets/`` / ``docs/README.md`` / ``docs/TESTING.md`` are mutually
     prefix-disjoint (Req 3.7).
  4. Every intra-repo link/image/``src``/``href`` resolves relative to its
     containing file; externals + ``mailto:`` skipped (Req 6.5, 6.7, 6.9).
  5. Anchor fragments resolve via GitHub heading slugification (Req 6.6).
  6. No link targets inside ``The Pudding GitHub README bundle/`` (Req 6.8).
  7. Every runnable fenced block carries a doctest-tier annotation on the line
     immediately preceding its opening fence — ``mermaid`` diagram blocks
     excluded (Req 7.1, 7.2).
  8. The ``platform-bootstrap.yml`` and ``svc-07-bootstrap.yml`` bring-up blocks
     are each ``requires-infra`` (Req 7.3, 7.4).
  9. Implemented-services-index runbook + spec-dir links resolve; SVC-07 and the
     platform prerequisites are both listed (Req 10.1-10.5).
 10. Git-diff scope guard: changed paths are confined to the allowlist
     (Req 11.1, 11.2).

Front-door docs (``LICENSE.md`` / ``CONTRIBUTING.md`` / ``SECURITY.md``) existence
is covered by check 4 (they are intra-repo links in the root README that must
resolve) and reinforced explicitly in check 2's companion assertion (Req 1.10,
2.2-2.4).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Repo-root resolution. This file lives at ``infra/tests/`` so the repo root is
# two parents up.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]

_ROOT_README = _REPO_ROOT / "README.md"
_DOCS_README = _REPO_ROOT / "docs" / "README.md"
_DOCS_TESTING = _REPO_ROOT / "docs" / "TESTING.md"
_LOGO_LIGHT_REL = "docs/assets/the-pudding-logo.png"
_LOGO_DARK_REL = "docs/assets/the-pudding-logo-dark.png"
_BUNDLE_DIR_NAME = "The Pudding GitHub README bundle"

#: The three documents the feature governs, keyed by a short label.
_DOCS: dict[str, Path] = {
    "README.md": _ROOT_README,
    "docs/README.md": _DOCS_README,
    "docs/TESTING.md": _DOCS_TESTING,
}

#: External hosts / schemes excluded from intra-repo resolution (Req 6.9). A
#: target is "external" if it starts with one of these schemes, or its host is
#: one of the allowlisted external hosts.
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:")

# =========================================================================== #
# Shared Markdown / link parsing helpers (task 8.1).
# =========================================================================== #


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_external(target: str) -> bool:
    """True if the target is an external URL or mailto (out of scope, Req 6.9)."""
    return target.strip().lower().startswith(_EXTERNAL_SCHEMES)


def _split_fragment(target: str) -> tuple[str, str | None]:
    """Split ``path#fragment`` into ``(path, fragment-or-None)``."""
    if "#" in target:
        path, frag = target.split("#", 1)
        return path, frag
    return target, None


def _iter_markdown_targets(text: str):
    """Yield every Markdown link/image target ``](target)`` in ``text``.

    Covers both links ``[txt](target)`` and images ``![alt](target)`` — the
    regex keys off the ``](`` sequence, which both share.
    """
    for m in re.finditer(r"\]\(([^)]+)\)", text):
        yield m.group(1).strip()


def _iter_html_targets(text: str):
    """Yield every HTML ``src="..."``, ``srcset="..."``, ``href="..."`` target.

    The root README uses a ``<picture>`` block with ``<source srcset=...>`` and
    an ``<img src=...>`` fallback for the light/dark logo; those image references
    must resolve too (Req 3.3-3.5).
    """
    for m in re.finditer(r'(?:src|srcset|href)\s*=\s*"([^"]+)"', text):
        # srcset can technically carry multiple comma-separated candidates;
        # the logo block uses a single path, but split defensively.
        raw = m.group(1).strip()
        for candidate in raw.split(","):
            tok = candidate.strip().split()  # drop any descriptor like "2x"
            if tok:
                yield tok[0].strip()


def _iter_all_targets(text: str):
    """Yield every link/image/HTML target in a document (Markdown + HTML)."""
    yield from _iter_markdown_targets(text)
    yield from _iter_html_targets(text)


def _github_slug(heading_text: str) -> str:
    """Return the GitHub-style anchor slug for a heading's visible text.

    GitHub's slugification (as used for ``#fragment`` anchors), per observed
    GitHub behaviour:
      * lower-case the text,
      * drop anything that is not a word char, space, or hyphen (this removes
        punctuation like ``.``, ``/``, ``(``, ``)``, backticks, ``:``, ``+``,
        ``&``, and em-dashes) **without** collapsing the surrounding spaces,
      * replace **each** remaining space with a single hyphen (runs of spaces are
        NOT collapsed — so "Docker + Compose" becomes "docker--compose" with a
        double hyphen, because the ``+`` is removed but its two neighbouring
        spaces each become a hyphen; GitHub produces exactly this).
    """
    text = heading_text.strip().lower()
    # Drop characters that are not alphanumeric, space, or hyphen. Note \w here
    # includes underscore, which GitHub keeps.
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    # Replace each individual whitespace character with a hyphen (do NOT collapse
    # runs — that is what preserves the "--" GitHub emits where punctuation sat
    # between two spaces).
    text = re.sub(r"\s", "-", text)
    return text


def _heading_slugs(text: str) -> set[str]:
    """Collect the set of GitHub anchor slugs for all ATX headings in ``text``.

    Only Markdown ATX headings (``#``..``######``) are considered. Fenced code
    blocks are skipped so a ``#``-comment inside a shell block is not mistaken
    for a heading.
    """
    slugs: set[str] = set()
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif stripped.startswith(fence_marker):
                in_fence = False
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            slugs.add(_github_slug(m.group(2)))
    return slugs


# =========================================================================== #
# Fenced-code-block extraction with doctest-tier detection (tasks 8.4).
# =========================================================================== #

_DOCTEST_RE = re.compile(r"<!--\s*doctest:\s*(offline|requires-infra|display-only)\b")
_CWD_RE = re.compile(r"<!--\s*cwd:")
#: Fence languages that are DIAGRAMS, not runnable commands — excluded from the
#: doctest-tier requirement (Req 7.1 note; mermaid is a diagram).
_NON_RUNNABLE_LANGS = {"mermaid"}


class FencedBlock:
    """A fenced code block and the doctest tier (if any) governing it."""

    def __init__(self, lang: str, open_line_idx: int, tier: str | None, body: str):
        self.lang = lang
        self.open_line_idx = open_line_idx  # 0-based line index of the opening fence
        self.tier = tier
        self.body = body


def _extract_fenced_blocks(text: str) -> list[FencedBlock]:
    """Parse fenced code blocks, resolving each block's governing doctest tier.

    The tier is read from the ``<!-- doctest: ... -->`` annotation line that
    governs the block. Per the repo convention and ``documentation-testing.md``,
    the annotation sits immediately before the opening fence; a ``<!-- cwd: ... -->``
    line may sit *between* the annotation and the fence (the annotation then being
    the nearest preceding non-blank line that is not a ``cwd`` comment). So:

      * the line immediately before the fence must be the ``doctest`` annotation
        OR a ``cwd`` comment,
      * if it is a ``cwd`` comment, the line before *that* must be the ``doctest``
        annotation (no blank line between annotation and fence/cwd),
      * any blank line between the annotation and the fence breaks the governance
        (tier resolves to ``None``) — enforcing Req 7.1's "no blank line between".
    """
    lines = text.splitlines()
    blocks: list[FencedBlock] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        m = re.match(r"^(```+|~~~+)\s*([A-Za-z0-9_+-]*)\s*$", stripped)
        if not m:
            i += 1
            continue
        marker = m.group(1)[:3]
        lang = m.group(2).lower()
        open_idx = i
        # Collect body until the closing fence.
        j = i + 1
        body_lines: list[str] = []
        while j < n:
            if lines[j].strip().startswith(marker):
                break
            body_lines.append(lines[j])
            j += 1
        # Resolve the governing doctest tier by scanning upward from open_idx-1.
        tier = _resolve_tier(lines, open_idx)
        blocks.append(FencedBlock(lang, open_idx, tier, "\n".join(body_lines)))
        i = j + 1
    return blocks


def _resolve_tier(lines: list[str], open_idx: int) -> str | None:
    """Return the doctest tier governing the fence at ``open_idx``, or ``None``.

    Walk upward over at most a short prelude of ``cwd`` comment lines (no blank
    line permitted) until a ``doctest`` annotation is found. A blank line, or any
    other content, immediately above the fence breaks governance.
    """
    k = open_idx - 1
    saw_cwd = False
    while k >= 0:
        line = lines[k].strip()
        if _DOCTEST_RE.search(line):
            return _DOCTEST_RE.search(line).group(1)
        if _CWD_RE.search(line):
            # A cwd comment may sit between the annotation and the fence.
            saw_cwd = True
            k -= 1
            continue
        # Any other line (including blank) breaks governance.
        return None
    _ = saw_cwd
    return None


# =========================================================================== #
# Check 1 — moved files + root exist and are the right documents.
# =========================================================================== #


def test_moved_files_and_root_exist_and_are_developer_b_front_door():
    """Req 1.1, 4.1, 4.2, 5.1, 5.2 — the three documents exist and the root is
    the Developer-B front door, not the relocated Developer-A README."""
    assert _ROOT_README.is_file(), "root README.md must exist (Req 1.1)"
    assert _DOCS_README.is_file(), "docs/README.md must exist (Req 4.1)"
    assert _DOCS_TESTING.is_file(), "docs/TESTING.md must exist (Req 5.1)"

    root_text = _read(_ROOT_README)
    # Developer-B front-door markers (design.md front-door contract).
    assert "What is The Pudding?" in root_text, (
        "root README must be the Developer-B front door (missing "
        '"What is The Pudding?" heading) — Req 1.1'
    )
    assert "read-only mirror of tagged releases" in root_text, (
        "root README must carry the release-mirror positioning note (Req 9.1)"
    )
    # The root must NOT still be the Developer-A maintainer README. The
    # Developer-A README's distinctive opening heading was "Developer Services
    # Platform" with the "platform operator stands up once" framing in prose;
    # the Developer-A content now lives under docs/README.md.
    docs_readme_text = _read(_DOCS_README)
    assert "What is The Pudding?" not in docs_readme_text, (
        "docs/README.md should hold the Developer-A content, not the "
        "Developer-B front door (Req 4.3)"
    )
    # And docs/README.md must preserve the Developer-A material (spot-check a
    # distinctive maintainer-README section).
    assert "The network foundation" in docs_readme_text, (
        "docs/README.md must preserve the Developer-A network-foundation "
        "section verbatim (Req 4.3)"
    )
    testing_text = _read(_DOCS_TESTING)
    assert "The two test tiers at a glance" in testing_text, (
        "docs/TESTING.md must preserve the Developer-A testing guide (Req 5.3)"
    )


def test_root_testing_md_is_gone_from_repo_root():
    """Req 5.2 — the repository root no longer holds TESTING.md after the move."""
    assert not (_REPO_ROOT / "TESTING.md").exists(), (
        "repository-root TESTING.md must not exist after relocation (Req 5.2)"
    )


# =========================================================================== #
# Check 2 — logo assets exist and are referenced by exactly those paths; and the
# front-door docs referenced by the root README exist (Req 1.10, 2.2-2.4).
# =========================================================================== #


def test_logo_assets_exist_and_are_referenced():
    """Req 3.1-3.6 — both logo assets exist and the root README references
    exactly ``docs/assets/the-pudding-logo{,-dark}.png``."""
    light = _REPO_ROOT / _LOGO_LIGHT_REL
    dark = _REPO_ROOT / _LOGO_DARK_REL
    assert light.is_file(), f"Logo_Light missing — blocker (Req 3.1, 3.6): {light}"
    assert dark.is_file(), f"Logo_Dark missing — blocker (Req 3.2, 3.6): {dark}"

    root_text = _read(_ROOT_README)
    assert _LOGO_LIGHT_REL in root_text, (
        f"root README must reference {_LOGO_LIGHT_REL} (Req 3.3)"
    )
    assert _LOGO_DARK_REL in root_text, (
        f"root README must reference {_LOGO_DARK_REL} (Req 3.4)"
    )


def test_front_door_docs_referenced_by_root_readme_exist():
    """Req 1.10, 2.2-2.4 — LICENSE.md / CONTRIBUTING.md / SECURITY.md are
    referenced by the root README and resolve to existing repository-root files."""
    root_text = _read(_ROOT_README)
    for name in ("LICENSE.md", "CONTRIBUTING.md", "SECURITY.md"):
        rel = f"./{name}"
        assert rel in root_text, (
            f"root README must link to {rel} (Req 2.2-2.4)"
        )
        assert (_REPO_ROOT / name).is_file(), (
            f"front-door doc referenced by root README is absent: {name} "
            "(Req 1.10)"
        )


# =========================================================================== #
# Check 3 — prefix-disjointness of the three docs/ paths (Req 3.7).
# =========================================================================== #


def test_docs_paths_are_mutually_prefix_disjoint():
    """Req 3.7 — none of ``docs/assets/``, ``docs/README.md``, ``docs/TESTING.md``
    is a path prefix of another."""
    paths = ["docs/assets/", "docs/README.md", "docs/TESTING.md"]
    for a in paths:
        for b in paths:
            if a is b:
                continue
            # Compare as path-component tuples so "docs/README.md" is not a
            # prefix of "docs/README.md.bak"-style false positives, and a true
            # directory prefix (docs/assets/ vs docs/assets/x) would be caught.
            a_parts = tuple(p for p in a.strip("/").split("/") if p)
            b_parts = tuple(p for p in b.strip("/").split("/") if p)
            assert a_parts != b_parts[: len(a_parts)] or a_parts == b_parts, (
                f"{a!r} is a prefix of {b!r} — violates prefix-disjointness "
                "(Req 3.7)"
            )
    # Explicit, direct assertions for clarity.
    assert not "docs/README.md".startswith("docs/assets/")
    assert not "docs/TESTING.md".startswith("docs/assets/")


# =========================================================================== #
# Check 4 + 5 — intra-repo link/image resolution and anchor-fragment resolution.
# =========================================================================== #


def _collect_intra_repo_refs(doc_path: Path):
    """Yield ``(raw_target, path_part, fragment)`` for intra-repo refs in a doc.

    External URLs and ``mailto:`` are excluded (Req 6.9). Bare ``#fragment``
    anchors (no path) are yielded with ``path_part == ""``.
    """
    text = _read(doc_path)
    for target in _iter_all_targets(text):
        if _is_external(target):
            continue
        path_part, fragment = _split_fragment(target)
        yield target, path_part, fragment


def test_intra_repo_links_and_images_resolve():
    """Req 6.5, 6.7, 6.9 — every intra-repo link/image/src/href resolves relative
    to its containing file."""
    failures: list[str] = []
    for label, doc_path in _DOCS.items():
        base = doc_path.parent
        for raw, path_part, _frag in _collect_intra_repo_refs(doc_path):
            if path_part == "":
                continue  # bare #fragment handled in the anchor test
            resolved = (base / path_part).resolve()
            if not resolved.exists():
                failures.append(f"{label}: {raw!r} -> missing {resolved}")
    assert not failures, "Unresolved intra-repo references (Req 6.7):\n" + "\n".join(
        failures
    )


def test_anchor_fragments_resolve():
    """Req 6.6 — every ``path#fragment`` resolves to a target file whose headings
    include the fragment's GitHub slug; every bare ``#fragment`` matches a heading
    in the containing document."""
    failures: list[str] = []
    slug_cache: dict[Path, set[str]] = {}

    def slugs_for(path: Path) -> set[str]:
        key = path.resolve()
        if key not in slug_cache:
            slug_cache[key] = _heading_slugs(_read(path)) if path.is_file() else set()
        return slug_cache[key]

    for label, doc_path in _DOCS.items():
        base = doc_path.parent
        for raw, path_part, fragment in _collect_intra_repo_refs(doc_path):
            if fragment is None:
                continue
            if path_part == "":
                target_file = doc_path  # same-doc anchor
            else:
                target_file = (base / path_part).resolve()
                # A fragment on a directory link is meaningless; only check when
                # the target is a markdown file.
                if target_file.is_dir():
                    continue
                if target_file.suffix.lower() != ".md":
                    continue
                if not target_file.is_file():
                    # resolution failure is reported by the other test; skip here
                    continue
            frag_slug = _github_slug(fragment)
            if frag_slug not in slugs_for(target_file):
                failures.append(
                    f"{label}: fragment '#{fragment}' (slug {frag_slug!r}) not "
                    f"found as a heading in {target_file}"
                )
    assert not failures, "Unresolved anchor fragments (Req 6.6):\n" + "\n".join(
        failures
    )


# =========================================================================== #
# Check 6 — no links into the bundle folder (Req 6.8).
# =========================================================================== #


def test_no_links_into_bundle_folder():
    """Req 6.8 — none of the three documents links into the staging bundle."""
    failures: list[str] = []
    for label, doc_path in _DOCS.items():
        text = _read(doc_path)
        for target in _iter_all_targets(text):
            if _BUNDLE_DIR_NAME in target:
                failures.append(f"{label}: links into bundle -> {target!r}")
    assert not failures, "Links into the bundle folder (Req 6.8):\n" + "\n".join(
        failures
    )


# =========================================================================== #
# Check 7 — every runnable fenced block carries a doctest-tier annotation.
# =========================================================================== #


def test_runnable_blocks_carry_doctest_tier():
    """Req 7.1, 7.2 — every runnable fenced block (mermaid excluded) is governed
    by a doctest-tier annotation on the immediately-preceding (non-cwd) line, with
    no blank line between the annotation and the opening fence."""
    failures: list[str] = []
    for label, doc_path in _DOCS.items():
        text = _read(doc_path)
        for block in _extract_fenced_blocks(text):
            if block.lang in _NON_RUNNABLE_LANGS:
                continue
            if block.tier is None:
                failures.append(
                    f"{label}: fenced block (lang={block.lang or 'none'}) at "
                    f"line {block.open_line_idx + 1} lacks a doctest-tier "
                    "annotation on the immediately-preceding line"
                )
    assert not failures, "Unannotated runnable blocks (Req 7.2):\n" + "\n".join(
        failures
    )


# =========================================================================== #
# Check 8 — the two bring-up blocks are requires-infra (Req 7.3, 7.4).
# =========================================================================== #


def test_bringup_blocks_are_requires_infra():
    """Req 7.3, 7.4 — the ``platform-bootstrap.yml`` and ``svc-07-bootstrap.yml``
    bring-up command blocks each carry the ``requires-infra`` tier."""
    needles = ("platform-bootstrap.yml", "svc-07-bootstrap.yml")
    found: dict[str, list[str]] = {needle: [] for needle in needles}
    for label, doc_path in _DOCS.items():
        text = _read(doc_path)
        for block in _extract_fenced_blocks(text):
            if block.lang in _NON_RUNNABLE_LANGS:
                continue
            for needle in needles:
                if needle in block.body:
                    found[needle].append(f"{label}:{block.tier}")
                    assert block.tier == "requires-infra", (
                        f"{label}: the block invoking {needle} must be "
                        f"requires-infra, got {block.tier!r} (Req 7.4)"
                    )
    for needle in needles:
        assert found[needle], (
            f"expected a command block invoking {needle} in one of the three "
            f"documents (Req 7.4); found none"
        )


# =========================================================================== #
# Check 9 — implemented-services-index links + required entries (Req 10.1-10.5).
# =========================================================================== #


def test_implemented_services_index_links_and_entries():
    """Req 10.1-10.5 — the root README's implemented-services index links each
    built service to an existing ``INSTALL-RUNBOOK.md`` and an existing
    ``.kiro/specs/<name>/`` directory, and lists SVC-07 and the platform
    prerequisites."""
    text = _read(_ROOT_README)

    # 10.5 — SVC-07 and the platform prerequisites must be listed.
    assert "SVC-07" in text, "root README index must list SVC-07 (Req 10.5)"
    assert "Platform prerequisites" in text, (
        "root README index must list the platform prerequisites (Req 10.5)"
    )

    # 10.2 — at least one INSTALL-RUNBOOK.md runbook link, each resolving.
    runbook_links = [
        t for t in _iter_markdown_targets(text) if "INSTALL-RUNBOOK.md" in t
    ]
    assert runbook_links, (
        "root README index must link each service's INSTALL-RUNBOOK.md "
        "(Req 10.2); found none"
    )
    for raw in runbook_links:
        path_part, _frag = _split_fragment(raw)
        resolved = (_ROOT_README.parent / path_part).resolve()
        assert resolved.is_file(), (
            f"implemented-services runbook link does not resolve: {raw!r} -> "
            f"{resolved} (Req 10.2, 10.4)"
        )

    # 10.3 — each service's .kiro/specs/<name>/ spec-directory link resolves, and
    # the two required services' spec dirs are present among them.
    spec_links = [
        t for t in _iter_markdown_targets(text) if ".kiro/specs/" in t
    ]
    assert spec_links, (
        "root README index must link each service's .kiro/specs/<name>/ "
        "directory (Req 10.3); found none"
    )
    resolved_spec_dirs = set()
    for raw in spec_links:
        path_part, _frag = _split_fragment(raw)
        resolved = (_ROOT_README.parent / path_part).resolve()
        assert resolved.is_dir(), (
            f"implemented-services spec-dir link does not resolve to a "
            f"directory: {raw!r} -> {resolved} (Req 10.3, 10.4)"
        )
        resolved_spec_dirs.add(resolved)

    assert (_REPO_ROOT / ".kiro" / "specs" / "svc-07-secrets-manager").resolve() in (
        resolved_spec_dirs
    ), "root README index must link the SVC-07 spec directory (Req 10.3, 10.5)"
    assert (
        _REPO_ROOT / ".kiro" / "specs" / "platform-prerequisites-bootstrap"
    ).resolve() in resolved_spec_dirs, (
        "root README index must link the platform-prerequisites-bootstrap spec "
        "directory (Req 10.3, 10.5)"
    )


# =========================================================================== #
# Check 10 — git-diff scope guard (Req 11.1, 11.2).
# =========================================================================== #

_TEST_FILE_REL = "infra/tests/test_accessible_root_readme_links.py"
#: The feature's own spec directory (requirements/design/tasks/validation). These
#: are the governing spec artifacts the feature is built from and must produce
#: (validation.md), not out-of-scope production code — the scope guard allows the
#: feature's own spec dir alongside the Req 11 doc paths.
_SPEC_DIR_REL = ".kiro/specs/accessible-root-readme/"


def _parse_name_status(stdout: str) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status`` output into ``(status, path)`` tuples."""
    changed: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        # Renames/copies appear as "R100\told\tnew" — record both sides.
        if status.startswith(("R", "C")) and len(parts) >= 3:
            changed.append((status, parts[1]))
            changed.append((status, parts[2]))
        else:
            changed.append((status, parts[-1]))
    return changed


def _changed_paths_vs_main() -> list[tuple[str, str]] | None:
    """Return ``(status, path)`` tuples for the feature's changed paths.

    Primary signal: ``git diff --name-status main...HEAD`` (the three-dot form,
    against the merge-base with ``main``) — the committed feature changes. Because
    this test is also run **before** the feature branch is committed (the
    implementation runs it as the green-gate in task 9), the committed range can
    be empty while the working tree already holds all the moves/edits. So we also
    fold in the working-tree + staged changes vs ``main`` and the untracked files,
    giving a meaningful scope guard both before and after the commit.

    Returns ``None`` ONLY when git itself is unavailable or the ``main...HEAD``
    range cannot be computed (-> the caller skips for that environmental reason).
    A successfully-computed but EMPTY diff returns ``[]`` — that is the clean /
    already-merged tree, where nothing out-of-scope was touched, so the caller
    treats it as a PASS, never a skip. (Distinguishing these two keeps the test
    from perturbing the suite-wide skip-count baseline once the feature is on
    ``main``.)
    """
    committed = subprocess.run(
        ["git", "diff", "--name-status", "main...HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if committed.returncode != 0:
        return None
    changed: list[tuple[str, str]] = list(_parse_name_status(committed.stdout))

    # Staged + unstaged changes relative to main (covers the pre-commit run).
    worktree = subprocess.run(
        ["git", "diff", "--name-status", "main"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if worktree.returncode == 0:
        changed.extend(_parse_name_status(worktree.stdout))

    # Untracked files (the new root README.md + docs/assets/* appear here before
    # staging). Exclude standard-ignored files via --exclude-standard.
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if untracked.returncode == 0:
        for path in untracked.stdout.splitlines():
            path = path.strip()
            if path:
                changed.append(("??", path))

    # Deduplicate while preserving order.
    seen = set()
    deduped: list[tuple[str, str]] = []
    for item in changed:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _is_allowlisted(status: str, path: str) -> bool:
    """True if a changed path is within the feature's scope allowlist.

    Allowlist (design.md § Reconciling the test file with Req 11's scope guard):
      * ``README.md``,
      * anything under ``docs/``,
      * the repository-root ``TESTING.md`` (deleted by the move),
      * anything under ``The Pudding GitHub README bundle/`` (deleted),
      * the one offline test file itself,
      * the feature's own spec directory ``.kiro/specs/accessible-root-readme/``
        (requirements/design/tasks/validation — the governing + produced spec
        artifacts, intrinsic to the feature, not out-of-scope production code).
    """
    if path == "README.md":
        return True
    if path.startswith("docs/"):
        return True
    if path == "TESTING.md":
        return True
    if path.startswith(_BUNDLE_DIR_NAME + "/") or path == _BUNDLE_DIR_NAME:
        return True
    if path == _TEST_FILE_REL:
        return True
    if path.startswith(_SPEC_DIR_REL):
        return True
    return False


def test_git_diff_scope_guard():
    """Req 11.1, 11.2 — the feature's git diff touches only the allowlisted paths.

    Skips cleanly when git is unavailable or the ``main...HEAD`` range cannot be
    computed (offline test must not hard-fail for an environmental git issue).
    """
    changed = _changed_paths_vs_main()
    if changed is None:
        pytest.skip(
            "requires git + a resolvable main...HEAD range; scope guard skipped"
        )
    # An empty (but successfully computed) diff is the clean / already-merged
    # tree: nothing out-of-scope was touched, so this is a PASS, not a skip.
    out_of_scope = [
        (status, path)
        for status, path in changed
        if not _is_allowlisted(status, path)
    ]
    assert not out_of_scope, (
        "documentation-only scope guard failed — out-of-scope files changed "
        "(Req 11.2):\n"
        + "\n".join(f"  {s}\t{p}" for s, p in out_of_scope)
    )
