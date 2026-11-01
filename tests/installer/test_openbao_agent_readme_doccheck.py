"""Offline doc check for the ``openbao-agent`` role README (task 6.2).

Feature: consumer-last-mile-hello-workload §C (Req 3.4), task 6.2 — the OFFLINE
doc check that guards the task-6.1 "Runtime contract" section the README gained.

=== WHAT THIS IS ===

A **pure-offline** pytest (no cluster, no Ansible/Terraform binary, no network).
It parses ``ansible/roles/openbao-agent/README.md`` from the working tree and
asserts the two things task 6.2 requires (Req 3.4):

  (a) The task-6.1 edit introduced **no runnable command that changes a doctest
      tier** — every runnable (shell) fenced block in the README still carries a
      correct ``<!-- doctest: TIER -->`` annotation, and no runnable block is
      left UNANNOTATED (which would silently default to display-only and hide
      drift — the ``documentation-testing.md`` anti-pattern). The §C runtime
      contract is prose + a non-runnable table ONLY, so it must add no runnable
      block; the three pre-existing runnable blocks are all ``offline``.

  (b) Every **link in the README resolves** — relative links resolve to a real
      file by path, and any ``path#anchor`` fragment resolves to a heading in the
      target markdown file.

It reuses — deliberately, not by copy-drift — the SAME tier-parsing and
link/anchor conventions as the repo's existing offline doc tests
(``infra/tests/test_accessible_root_readme_links.py`` and the task-5.1 parser in
``tests/installer/test_hello_workload_smoke.py``): the ``<!-- doctest: TIER -->``
annotation sits on the nearest preceding non-blank line, a single
``<!-- cwd: ... -->`` line may sit between the annotation and the opening fence,
and ``mermaid`` (and bare/non-shell) fences are not runnable shell blocks.

It carries **no** ``requires_infra`` marker — it runs in the default (offline)
tier (``pytest.ini`` ``addopts = -m "not requires_infra"`` keeps it offline).

=== FORWARD-REFERENCE NOW RESOLVED (task 7.1) ===

The §C section ends with a forward-pointer to the SSO section of
``docs/CONSUMER-JOURNEY.md`` (the §D dev issuer-reachability recipe). Task 6.2
originally required that pointer to stay **prose only** (a backtick file-path
mention, not a Markdown link) because the SSO *section* did not yet exist and an
anchor link would have dangled.

Task 7.1 has since authored that SSO section
(``## SSO consumption: dev issuer-reachability recipe (§D)``), so the pointer is
now a proper resolving Markdown anchor link
(``docs/CONSUMER-JOURNEY.md#sso-consumption-dev-issuer-reachability-recipe-d``).
The generic ``test_all_anchor_fragments_resolve`` check already proves that
anchor resolves to a real heading. This module therefore asserts the file exists
and that the §D pointer is now a RESOLVING anchor link (not a dangling one), in
``test_consumer_journey_sso_section_anchor_link_resolves`` — the task-6.2
"must stay prose" guard has been retired by task 7.1 authoring the section.
"""

from __future__ import annotations

import re
from pathlib import Path

# --------------------------------------------------------------------------- #
# Repo-root resolution. This file lives at ``tests/installer/`` so the repo root
# is two parents up (matches the sibling tests/installer modules).
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_README = _REPO_ROOT / "ansible" / "roles" / "openbao-agent" / "README.md"
_CONSUMER_JOURNEY = _REPO_ROOT / "docs" / "CONSUMER-JOURNEY.md"

_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:")

# --------------------------------------------------------------------------- #
# Tier parsing (identical governance to the two existing repo doctest parsers).
# --------------------------------------------------------------------------- #
_DOCTEST_RE = re.compile(r"<!--\s*doctest:\s*(offline|requires-infra|display-only)\b")
_CWD_RE = re.compile(r"<!--\s*cwd:")
#: Fence languages that ARE runnable shell commands subject to the tier rule.
_RUNNABLE_LANGS = {"bash", "sh", "shell", "console"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_external(target: str) -> bool:
    return target.strip().lower().startswith(_EXTERNAL_SCHEMES)


def _split_fragment(target: str) -> tuple[str, str | None]:
    if "#" in target:
        path, frag = target.split("#", 1)
        return path, frag
    return target, None


def _iter_markdown_targets(text: str):
    """Yield every Markdown link/image target ``](target)`` in ``text``."""
    for m in re.finditer(r"\]\(([^)]+)\)", text):
        yield m.group(1).strip()


def _iter_html_targets(text: str):
    """Yield every HTML ``src=``/``srcset=``/``href=`` target in ``text``."""
    for m in re.finditer(r'(?:src|srcset|href)\s*=\s*"([^"]+)"', text):
        raw = m.group(1).strip()
        for candidate in raw.split(","):
            tok = candidate.strip().split()
            if tok:
                yield tok[0].strip()


def _iter_all_targets(text: str):
    yield from _iter_markdown_targets(text)
    yield from _iter_html_targets(text)


def _github_slug(heading_text: str) -> str:
    """GitHub-style anchor slug for a heading's visible text (see the sibling
    ``test_accessible_root_readme_links.py`` for the exact rules)."""
    text = heading_text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s", "-", text)
    return text


def _heading_slugs(text: str) -> set[str]:
    """GitHub anchor slugs for all ATX headings, skipping fenced code blocks."""
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


def _resolve_tier(lines: list[str], open_idx: int) -> str | None:
    """Return the doctest tier governing the fence at ``open_idx``, or ``None``."""
    k = open_idx - 1
    while k >= 0:
        line = lines[k].strip()
        hit = _DOCTEST_RE.search(line)
        if hit:
            return hit.group(1)
        if _CWD_RE.search(line):
            k -= 1
            continue
        return None
    return None


class _Fence:
    def __init__(self, open_line: int, lang: str, tier: str | None, body: str):
        self.open_line = open_line  # 1-based
        self.lang = lang
        self.tier = tier
        self.body = body


def _fenced_blocks(md_path: Path) -> list[_Fence]:
    """Parse ``md_path`` into every fenced block with its governing tier."""
    lines = _read(md_path).splitlines()
    out: list[_Fence] = []
    i, n = 0, len(lines)
    while i < n:
        m = re.match(r"^(```+|~~~+)\s*([A-Za-z0-9_+-]*)\s*$", lines[i].strip())
        if not m:
            i += 1
            continue
        marker = m.group(1)[:3]
        lang = m.group(2).lower()
        open_idx = i
        j = i + 1
        body: list[str] = []
        while j < n and not lines[j].strip().startswith(marker):
            body.append(lines[j])
            j += 1
        out.append(_Fence(open_idx + 1, lang, _resolve_tier(lines, open_idx), "\n".join(body)))
        i = j + 1
    return out


# =========================================================================== #
# (a) No runnable block added that changes a doctest tier.
# =========================================================================== #


def test_agent_readme_exists():
    assert _AGENT_README.is_file(), f"missing openbao-agent README: {_AGENT_README}"


def test_every_runnable_block_carries_a_valid_doctest_tier():
    """Req 3.4 (a): every RUNNABLE (shell) fenced block in the README carries a
    valid ``<!-- doctest: TIER -->`` annotation on the immediately-preceding
    (non-cwd) line; no runnable block is left unannotated (which would silently
    default to display-only — the documentation-testing.md anti-pattern)."""
    unannotated: list[str] = []
    for block in _fenced_blocks(_AGENT_README):
        if block.lang not in _RUNNABLE_LANGS:
            continue  # mermaid / bare / non-shell fences are not runnable commands
        if block.tier is None:
            unannotated.append(f"line {block.open_line} (lang={block.lang})")
    assert not unannotated, (
        "every runnable fenced block in the openbao-agent README must carry a "
        f"<!-- doctest: TIER --> annotation; unannotated: {unannotated!r}"
    )


def test_runnable_blocks_are_all_offline_tier():
    """Req 3.4 (a): the README's runnable blocks are offline preflight commands
    (ansible-lint / yamllint / the offline golden pytest) — each MUST carry the
    ``offline`` tier. The §C task-6.1 runtime-contract section is prose + a
    non-runnable table only, so it introduced NO runnable block and therefore
    changed no tier; the three runnable blocks here are the pre-existing
    "Verify the role (offline, Tier-1)" commands."""
    runnable = [b for b in _fenced_blocks(_AGENT_README) if b.lang in _RUNNABLE_LANGS]
    assert runnable, "expected the README's offline preflight runnable blocks"
    for block in runnable:
        assert block.tier == "offline", (
            f"runnable block at line {block.open_line} (lang={block.lang}) must be "
            f"`offline`, got {block.tier!r}. Body:\n{block.body}"
        )
    # The known offline preflight commands are present under the offline tier.
    offline_bodies = "\n".join(b.body for b in runnable if b.tier == "offline")
    assert "pytest" in offline_bodies and "ansible-lint" in offline_bodies, (
        "the README must keep its offline preflight commands (ansible-lint + the "
        "offline golden pytest) under the `offline` tier"
    )


def test_runtime_contract_section_added_no_runnable_block():
    """Req 3.4 (a), task-6.1 scope guard: the "Runtime contract" section the
    task-6.1 edit added (from the `## Runtime contract` heading up to the next
    top-level `## ` heading) contains NO fenced block at all — it is prose + a
    non-runnable Markdown table only, so it cannot have added or changed any
    doctest tier."""
    lines = _read(_AGENT_README).splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip() == "## Runtime contract"),
        None,
    )
    assert start is not None, "the task-6.1 '## Runtime contract' section must exist"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    section = lines[start:end]
    fence_lines = [
        start + off + 1
        for off, ln in enumerate(section)
        if re.match(r"^(```+|~~~+)", ln.strip())
    ]
    assert not fence_lines, (
        "the task-6.1 Runtime contract section must be prose + a non-runnable "
        f"table ONLY (no fenced code block); found fences at lines {fence_lines!r}"
    )


# =========================================================================== #
# (b) Every link in the README resolves.
# =========================================================================== #


def test_all_intra_repo_links_resolve():
    """Req 3.4 (b): every intra-repo link/image target in the README resolves to
    a real file relative to the README's directory (external URLs excluded)."""
    base = _AGENT_README.parent
    text = _read(_AGENT_README)
    failures: list[str] = []
    for target in _iter_all_targets(text):
        if _is_external(target):
            continue
        path_part, _frag = _split_fragment(target)
        if path_part == "":
            continue  # bare #fragment handled in the anchor test
        resolved = (base / path_part).resolve()
        if not resolved.exists():
            failures.append(f"{target!r} -> missing {resolved}")
    assert not failures, "Unresolved intra-repo links in the README (Req 3.4):\n" + "\n".join(
        failures
    )


def test_all_anchor_fragments_resolve():
    """Req 3.4 (b): every ``path#anchor`` / bare ``#anchor`` link resolves to a
    heading in the target markdown document (GitHub slugification)."""
    base = _AGENT_README.parent
    text = _read(_AGENT_README)
    slug_cache: dict[Path, set[str]] = {}

    def slugs_for(path: Path) -> set[str]:
        key = path.resolve()
        if key not in slug_cache:
            slug_cache[key] = _heading_slugs(_read(path)) if path.is_file() else set()
        return slug_cache[key]

    failures: list[str] = []
    for target in _iter_all_targets(text):
        if _is_external(target):
            continue
        path_part, fragment = _split_fragment(target)
        if fragment is None:
            continue
        if path_part == "":
            target_file = _AGENT_README
        else:
            target_file = (base / path_part).resolve()
            if target_file.is_dir() or target_file.suffix.lower() != ".md":
                continue
            if not target_file.is_file():
                continue  # path resolution failure reported by the other test
        if _github_slug(fragment) not in slugs_for(target_file):
            failures.append(f"'#{fragment}' not a heading in {target_file}")
    assert not failures, "Unresolved anchor fragments in the README (Req 3.4):\n" + "\n".join(
        failures
    )


# =========================================================================== #
# Forward-reference now resolved (task 7.1): docs/CONSUMER-JOURNEY.md exists and
# its SSO section is authored, so the §D pointer is a RESOLVING anchor link.
# =========================================================================== #

#: The SSO section's GitHub anchor slug (task 7.1). Kept here so the assertion
#: below also documents exactly which heading the §D pointer targets.
_SSO_SECTION_ANCHOR = "sso-consumption-dev-issuer-reachability-recipe-d"


def test_consumer_journey_sso_section_anchor_link_resolves():
    """Req 3.4 / §D pointer (task 7.1 resolved): task 7.1 authored the SSO
    section of ``docs/CONSUMER-JOURNEY.md``, so the README's §C section now
    points at it with a RESOLVING Markdown anchor link (not the pre-7.1 prose
    placeholder). This asserts (1) the journey file exists, (2) the README
    carries exactly such an anchor link, and (3) the anchor resolves to the SSO
    heading in the journey file. (The generic ``test_all_anchor_fragments_resolve``
    also covers resolution; this test documents the task-7.1 contract
    explicitly and pins the expected anchor slug.)"""
    assert _CONSUMER_JOURNEY.is_file(), (
        f"docs/CONSUMER-JOURNEY.md must exist (the §D pointer target); "
        f"missing: {_CONSUMER_JOURNEY}"
    )
    text = _read(_AGENT_README)
    journey_anchor_links = [
        t for t in _iter_markdown_targets(text) if "CONSUMER-JOURNEY.md#" in t
    ]
    assert journey_anchor_links, (
        "task 7.1 authored the SSO section, so the README's §D forward-pointer "
        "must now be a RESOLVING Markdown anchor link into "
        "docs/CONSUMER-JOURNEY.md (found none)"
    )
    # The §D pointer targets the SSO section's slug.
    assert any(
        _split_fragment(t)[1] == _SSO_SECTION_ANCHOR for t in journey_anchor_links
    ), (
        "the §D pointer must target the SSO section anchor "
        f"'#{_SSO_SECTION_ANCHOR}'; found: {journey_anchor_links!r}"
    )
    # And that anchor resolves to a real heading in the journey file.
    assert _SSO_SECTION_ANCHOR in _heading_slugs(_read(_CONSUMER_JOURNEY)), (
        f"'#{_SSO_SECTION_ANCHOR}' must resolve to a heading in "
        f"{_CONSUMER_JOURNEY} (task 7.1 SSO section)"
    )
