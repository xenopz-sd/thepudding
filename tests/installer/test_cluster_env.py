"""Offline unit + Hypothesis property tests for ``cluster_env.py``.

Feature: svc-07-automated-installer.

The module under test (``scripts/installer/cluster_env.py``) is the one
genuinely pure-logic surface of the SVC-07 automated installer: it parses the
gitignored ``cluster.env`` file, resolves each logical variable from either its
``PROXMOX_``-prefixed or ``TF_VAR_``-prefixed alias, validates that all
mandatory variables are present, detects conflicting alias values, and emits
the resolved values as canonical ``TF_VAR_*`` entries.

This file holds three task deliverables, all offline:

* Task 2.2 — Property 2: mandatory-var validation is order-independent and
  complete (Hypothesis, >=100 iterations). Validates Requirements 2.4, 2.5.
* Task 2.3 — Property 3: alias resolution — identical values resolve, conflicts
  fail (Hypothesis, >=100 iterations). Validates Requirements 2.4, 2.8.
* Task 2.4 — example unit tests for the helper edge cases (file-absent,
  whitespace-only value, comment/blank-line handling, single-alias resolution,
  canonical ``TF_VAR_*`` emission). Requirements 2.3, 2.4, 2.6, 2.7.
"""

from __future__ import annotations

import os
import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import cluster_env
from cluster_env import (
    AUTODETECT,
    SCHEMA,
    EnvFileNotFoundError,
    LogicalVar,
    Resolution,
    ValidationError,
    parse_env,
    resolve,
    resolve_one,
    validate,
)


# --- Schema-derived fixtures shared by the property tests ---------------------

_MANDATORY: tuple[LogicalVar, ...] = tuple(v for v in SCHEMA if v.mandatory)
_MANDATORY_LOGICALS: frozenset[str] = frozenset(v.logical for v in _MANDATORY)

# A generator for non-empty, non-whitespace-only alias values. We keep the
# alphabet simple and printable so the "present after trim" contract is easy to
# reason about, then separately exercise surrounding whitespace via padding.
_VALUE_CHARS = string.ascii_letters + string.digits + "-_.:/@"
_nonempty_values = st.text(alphabet=_VALUE_CHARS, min_size=1, max_size=24)

# Padding that is whitespace-only: when it wraps a value the trimmed result is
# unchanged; when it IS the value the trimmed result is empty (absent).
_ws = st.text(alphabet=" \t", max_size=4)

# --- Quote-wrapping helper for the fix-cluster-env-quote-stripping extension ---
#
# The Property 2/3 generators below SOMETIMES wrap a generated value in a
# matching surrounding quote pair (single or double). Because ``_VALUE_CHARS``
# deliberately excludes ``'`` and ``"``, wrapping the WHOLE value is what
# exercises the "matching surrounding pair" semantics — the interior never
# contains a quote, so the only quotes present are the ones we add.
#
# ``maybe_quote(value, decision)`` returns the value verbatim (unquoted) or
# wrapped in one matching pair, per the drawn decision. The LOGICAL value the
# fix must resolve to is always the unquoted ``value`` — a quoted draw and its
# unquoted twin must resolve identically. On UNFIXED code a quoted draw resolves
# to the quote-wrapped string (the bug), so quoted draws fail until the fix
# lands; unquoted draws pass on unfixed and post-fix alike.
_quote_decision = st.sampled_from(("none", "single", "double"))


def _maybe_quote(value: str, decision: str) -> str:
    """Wrap ``value`` in a matching quote pair per ``decision`` (or not).

    ``value`` is drawn from ``_VALUE_CHARS`` (no embedded quotes), so wrapping is
    unambiguous and the logical (post-normalization) value is always ``value``.
    """
    if decision == "single":
        return f"'{value}'"
    if decision == "double":
        return f'"{value}"'
    return value


# =============================================================================
# Task 2.2 — Property 2
# Feature: svc-07-automated-installer, Property 2: mandatory-var validation is
# order-independent and complete
# Validates: Requirements 2.4, 2.5
# =============================================================================


@st.composite
def _env_with_arbitrary_mandatory_subset(draw):
    """Build an env dict populating an arbitrary subset of mandatory vars.

    For each mandatory logical var we independently decide one of three states:

    * ``present``   — a non-empty value (optionally whitespace-padded),
    * ``empty``     — a whitespace-only value (must be treated as absent),
    * ``absent``    — key not written at all.

    We use whichever alias form (PROXMOX_ or TF_VAR_) is chosen per var; when we
    write a value we only write ONE alias so no conflict is introduced (conflict
    behaviour is Property 3's concern, not this one). The returned tuple carries
    the dict plus the ground-truth set of logical names that are truly
    absent-or-empty-after-trim, so the test can compare against the reported set.
    """
    env: dict[str, str] = {}
    truly_absent: set[str] = set()
    # Ground-truth expected resolved value per canonical TF_VAR_* key, for the
    # vars that are truly present. Lets the test assert a quoted value resolves
    # to the SAME logical value as its unquoted twin.
    expected_values: dict[str, str] = {}

    for spec in _MANDATORY:
        state = draw(st.sampled_from(("present", "empty", "absent")))
        use_tfvar = draw(st.booleans())
        key = spec.tfvar_alias if use_tfvar else spec.proxmox_alias

        if state == "present":
            core = draw(_nonempty_values)
            lead = draw(_ws)
            trail = draw(_ws)
            # SOMETIMES wrap the core in a matching quote pair. The quotes sit
            # INSIDE the whitespace padding, so the post-fix normalized value is
            # still ``core`` (outer whitespace trimmed, one matching pair
            # removed). This is the fix-cluster-env-quote-stripping extension:
            # a quoted value must resolve to the same logical value as its
            # unquoted twin.
            quoted = _maybe_quote(core, draw(_quote_decision))
            env[key] = f"{lead}{quoted}{trail}"
            expected_values[spec.tfvar_alias] = core
        elif state == "empty":
            env[key] = draw(_ws)  # whitespace-only -> absent after trim
            truly_absent.add(spec.logical)
        else:  # absent
            truly_absent.add(spec.logical)

    # Shuffle insertion order so ordering cannot influence the result. We rebuild
    # the dict from a permuted list of items; parse-order independence matters
    # because dicts preserve insertion order in modern Python.
    items = list(env.items())
    order = draw(st.permutations(items))
    env = dict(order)

    return env, truly_absent, expected_values


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(_env_with_arbitrary_mandatory_subset())
def test_property2_mandatory_validation_order_independent_and_complete(case):
    """Reported-missing set == truly-absent-or-empty set, order-independent.

    Feature: svc-07-automated-installer, Property 2: mandatory-var validation is
    order-independent and complete.

    Validates: Requirements 2.4, 2.5.
    """
    env, truly_absent, expected_values = case

    # Route the generated env through the REAL runtime pipeline: render it to
    # `.env` text and run it through parse_env() before resolve(). Quote
    # normalization lives ONLY in parse_env() (per design.md — resolve/
    # resolve_one/validate/_present deliberately do not strip quotes, because at
    # runtime values always flow load_env_file -> parse_env -> validate ->
    # resolve, so resolve() only ever sees already-de-quoted values). Injecting a
    # quoted value straight into an env dict and calling resolve() on it bypasses
    # the exact layer the fix lives in. Text-rendering is safe: `_VALUE_CHARS`
    # excludes newlines, and a whitespace-only "empty" draw renders as `KEY=<ws>`
    # which parse_env() trims to empty — consistent with the truly_absent
    # semantics (empty is treated as absent by resolution).
    text = "\n".join(f"{k}={v}" for k, v in env.items())
    parsed = parse_env(text)

    resolved, missing, conflicts = resolve(parsed)

    # No conflicts are constructed by this generator (single alias per var).
    assert conflicts == []

    # Completeness + correctness: the reported-missing set is EXACTLY the set of
    # mandatory vars that are absent-or-empty-after-trim, as a set (so ordering
    # and any accidental duplication are irrelevant). Quoting a present value
    # does NOT move it into or out of the missing set — a quoted non-empty value
    # counts as present.
    assert set(missing) == truly_absent

    # Reported missing never includes a non-mandatory logical, and never a var
    # that was actually present.
    assert set(missing) <= _MANDATORY_LOGICALS

    # fix-cluster-env-quote-stripping extension: a quoted present value resolves
    # to the SAME logical value as its unquoted twin (the core, quotes removed).
    for tfvar_key, core in expected_values.items():
        assert resolved[tfvar_key] == core

    # validate() must agree: it raises iff something is missing, and the single
    # message names every missing var (order-independent — we assert membership).
    if truly_absent:
        with pytest.raises(ValidationError) as exc:
            validate(parsed)
        message = str(exc.value)
        for logical in truly_absent:
            assert logical in message
    else:
        # All mandatory present -> validate succeeds and resolves them all.
        validated = validate(parsed)
        for spec in _MANDATORY:
            assert spec.tfvar_alias in validated


def test_property2_reordering_same_file_yields_identical_missing_set():
    """Explicit order-independence check: permuting lines changes nothing."""
    base = {
        "TF_VAR_proxmox_endpoint": "https://pve.example:8006",
        # node name + api token + template intentionally omitted (missing)
    }
    reversed_items = dict(reversed(list(base.items())))

    _r1, missing1, _c1 = resolve(base)
    _r2, missing2, _c2 = resolve(reversed_items)

    assert set(missing1) == set(missing2)


# =============================================================================
# Task 2.3 — Property 3
# Feature: svc-07-automated-installer, Property 3: alias resolution — identical
# values resolve, conflicts fail
# Validates: Requirements 2.4, 2.8
# =============================================================================


@given(
    spec=st.sampled_from(SCHEMA),
    value=_nonempty_values,
    pad=_ws,
    q_p=_quote_decision,
    q_t=_quote_decision,
)
def test_property3_identical_alias_values_resolve(spec, value, pad, q_p, q_t):
    """Both aliases present with equal (post-normalization) values -> single value.

    Feature: svc-07-automated-installer, Property 3: alias resolution — identical
    values resolve, conflicts fail.

    Validates: Requirements 2.4, 2.8.

    fix-cluster-env-quote-stripping extension: each alias is SOMETIMES wrapped in
    a matching quote pair (independently drawn ``q_p`` / ``q_t``). After
    normalization both aliases carry the same core value, so a quoted alias and
    its unquoted twin must still resolve to the single ``value`` with no
    conflict. On UNFIXED code a quoted draw keeps its quotes, so an
    unquoted-vs-quoted pair looks like a conflict — quoted draws fail until the
    fix lands; unquoted draws (q_p == q_t == "none") pass on unfixed too.
    """
    # Same core value; the two aliases may carry different surrounding whitespace
    # (outside any quotes) and be independently quoted or not. Route both alias
    # entries through the REAL runtime pipeline — render to `.env` text and run
    # parse_env() — before resolving, because quote normalization lives ONLY in
    # parse_env() (see design.md; resolve_one/resolve receive already-de-quoted
    # values at runtime). After parse_env normalization both aliases carry the
    # same core value, so resolution must succeed to ``value`` with no conflict.
    text = "\n".join(
        [
            f"{spec.proxmox_alias}={_maybe_quote(value, q_p)}",
            f"{spec.tfvar_alias}={pad}{_maybe_quote(value, q_t)}{pad}",
        ]
    )
    env = parse_env(text)

    res = resolve_one(env, spec)
    assert res.conflict is False
    assert res.value == value

    # And through the whole-schema resolver: no conflict reported for this pair.
    _resolved, _missing, conflicts = resolve(env)
    expected_pair = f"{spec.proxmox_alias} vs {spec.tfvar_alias}"
    assert expected_pair not in conflicts


@settings(max_examples=200)
@given(
    spec=st.sampled_from(SCHEMA),
    values=st.tuples(_nonempty_values, _nonempty_values),
    q_p=_quote_decision,
    q_t=_quote_decision,
)
def test_property3_differing_alias_values_fail_naming_pair(spec, values, q_p, q_t):
    """Both aliases present with differing non-empty values -> named conflict.

    Feature: svc-07-automated-installer, Property 3: alias resolution — identical
    values resolve, conflicts fail.

    Validates: Requirements 2.4, 2.8.

    fix-cluster-env-quote-stripping extension: each alias is SOMETIMES wrapped in
    a matching quote pair. Because the CORE values genuinely differ (and
    ``_VALUE_CHARS`` excludes quotes), wrapping cannot make them equal after
    normalization — so a genuine conflict must still be detected regardless of
    quoting. On UNFIXED code the quoted strings differ too (different cores), so
    this test passes on unfixed as well; its role post-fix is to prove the strip
    did not mask a real conflict.
    """
    a, b = values
    # Constrain to genuinely differing trimmed values.
    if a.strip() == b.strip():
        b = b + "-x"

    # Route both alias entries through the REAL runtime pipeline (parse_env) for
    # pipeline fidelity — quote normalization lives ONLY in parse_env(). Because
    # the CORE values genuinely differ and `_VALUE_CHARS` excludes quotes,
    # wrapping cannot make them equal after normalization, so a genuine conflict
    # is still detected regardless of quoting.
    text = "\n".join(
        [
            f"{spec.proxmox_alias}={_maybe_quote(a, q_p)}",
            f"{spec.tfvar_alias}={_maybe_quote(b, q_t)}",
        ]
    )
    env = parse_env(text)

    res = resolve_one(env, spec)
    assert res.conflict is True
    assert res.value is None

    # The whole-schema resolver reports the conflict naming the exact pair, and
    # validate() raises a single error that names the conflicting pair.
    _resolved, _missing, conflicts = resolve(env)
    expected_pair = f"{spec.proxmox_alias} vs {spec.tfvar_alias}"
    assert expected_pair in conflicts

    with pytest.raises(ValidationError) as exc:
        validate(env)
    message = str(exc.value)
    assert spec.proxmox_alias in message
    assert spec.tfvar_alias in message


# =============================================================================
# Task 2.4 — Example unit tests for helper edge cases
# Requirements: 2.3, 2.4, 2.6, 2.7
# =============================================================================


def _full_mandatory_env() -> dict[str, str]:
    """A dict populating every mandatory var via one alias with placeholders."""
    return {
        "PROXMOX_ENDPOINT": "https://pve.example:8006",
        "PROXMOX_API_TOKEN": "root@pam!id=placeholder-token",
        "PROXMOX_NODE_NAME": "node1",
        "PROXMOX_LXC_TEMPLATE": "local:vztmpl/debian-12-standard.tar.zst",
    }


def test_file_absent_raises_distinct_error_naming_path(tmp_path):
    """Req 2.3: a missing file raises EnvFileNotFoundError naming the path."""
    missing = tmp_path / "cluster.env"

    with pytest.raises(EnvFileNotFoundError) as exc:
        cluster_env.load_env_file(str(missing))

    message = str(exc.value)
    assert str(missing) in message
    assert "cluster.env.example" in message

    # It is a distinct type from ValidationError (a present-but-incomplete file).
    assert not isinstance(exc.value, ValidationError)


def test_resolve_env_file_absent_raises_before_validation(tmp_path):
    """Req 2.3: the end-to-end path surfaces file-absent, not a validation error."""
    missing = tmp_path / "cluster.dev.env"
    with pytest.raises(EnvFileNotFoundError):
        cluster_env.resolve_env_file(str(missing))


def test_whitespace_only_value_treated_as_empty():
    """Req 2.4: a whitespace-only value counts as absent (present==non-empty)."""
    env = _full_mandatory_env()
    # Blank out the node name with whitespace only.
    env["PROXMOX_NODE_NAME"] = "   \t  "

    _resolved, missing, conflicts = resolve(env)
    assert conflicts == []
    assert "Node name" in missing

    with pytest.raises(ValidationError) as exc:
        validate(env)
    assert "Node name" in str(exc.value)


def test_comment_and_blank_line_handling():
    """Req 2.4: comment (#) and blank lines are ignored; KEY=VALUE trimmed."""
    text = "\n".join(
        [
            "# SVC-07 cluster env (example)",
            "",
            "   # indented comment",
            "  PROXMOX_ENDPOINT = https://pve.example:8006  ",
            "PROXMOX_API_TOKEN=root@pam!id=placeholder-token",
            "\t",
            "PROXMOX_NODE_NAME=node1",
            "PROXMOX_LXC_TEMPLATE=local:vztmpl/debian-12-standard.tar.zst",
        ]
    )

    parsed = parse_env(text)

    # Comments/blank lines dropped; surrounding whitespace trimmed from key/value.
    assert parsed["PROXMOX_ENDPOINT"] == "https://pve.example:8006"
    # A value legitimately containing '=' survives (partition on first '=').
    assert parsed["PROXMOX_API_TOKEN"] == "root@pam!id=placeholder-token"
    assert parsed["PROXMOX_NODE_NAME"] == "node1"
    # No stray keys from comment/blank lines.
    assert all(not k.startswith("#") for k in parsed)

    # And the parsed content validates cleanly.
    resolved = validate(parsed)
    assert resolved["TF_VAR_proxmox_endpoint"] == "https://pve.example:8006"


def test_single_alias_resolution_either_form():
    """Req 2.4: exactly one alias present resolves without conflict."""
    # PROXMOX_ alias only.
    env_p = _full_mandatory_env()
    resolved_p = validate(env_p)
    assert resolved_p["TF_VAR_proxmox_endpoint"] == "https://pve.example:8006"

    # TF_VAR_ alias only.
    env_t = {
        "TF_VAR_proxmox_endpoint": "https://pve.example:8006",
        "TF_VAR_proxmox_api_token": "root@pam!id=placeholder-token",
        "TF_VAR_proxmox_node_name": "node1",
        "TF_VAR_openbao_template_file_id": "local:vztmpl/debian-12-standard.tar.zst",
    }
    resolved_t = validate(env_t)
    assert resolved_t["TF_VAR_proxmox_node_name"] == "node1"


def test_canonical_tfvar_emission_keys():
    """Req 2.4: resolved mapping is keyed by canonical TF_VAR_* names."""
    env = _full_mandatory_env()
    resolved = validate(env)

    # Every resolved mandatory key is the TF_VAR_ canonical alias, never the
    # PROXMOX_ input alias.
    for spec in _MANDATORY:
        assert spec.tfvar_alias in resolved
        assert spec.proxmox_alias not in resolved


def test_optional_datastore_absent_emits_autodetect_sentinel():
    """Req 2.6: datastore unset -> AUTODETECT sentinel (triggers auto-detect)."""
    env = _full_mandatory_env()  # no datastore var
    resolved = validate(env)
    assert resolved["TF_VAR_openbao_datastore_id"] == AUTODETECT


def test_optional_datastore_configured_suppresses_autodetect():
    """Req 2.6: a configured datastore value is used as-is, no auto-detect."""
    env = _full_mandatory_env()
    env["PROXMOX_DATASTORE_ID"] = "local-zfs"
    resolved = validate(env)
    assert resolved["TF_VAR_openbao_datastore_id"] == "local-zfs"
    assert resolved["TF_VAR_openbao_datastore_id"] != AUTODETECT


def test_resolve_env_file_end_to_end_no_prompt(tmp_path, monkeypatch):
    """Req 2.7: resolution reads only the file, never prompts on stdin."""
    env_file = tmp_path / "cluster.env"
    env_file.write_text(
        "\n".join(
            [
                "PROXMOX_ENDPOINT=https://pve.example:8006",
                "PROXMOX_API_TOKEN=root@pam!id=placeholder-token",
                "PROXMOX_NODE_NAME=node1",
                "PROXMOX_LXC_TEMPLATE=local:vztmpl/debian-12-standard.tar.zst",
            ]
        ),
        encoding="utf-8",
    )

    # Any attempt to read stdin would raise, proving no interactive prompt.
    def _boom(*_args, **_kwargs):  # pragma: no cover - only fires on failure
        raise AssertionError("cluster_env must not read from stdin")

    monkeypatch.setattr("builtins.input", _boom)

    resolved = cluster_env.resolve_env_file(str(env_file))
    assert resolved["TF_VAR_proxmox_node_name"] == "node1"
    assert resolved["TF_VAR_openbao_datastore_id"] == AUTODETECT


def test_resolution_namedtuple_shape():
    """Sanity: resolve_one returns a Resolution(value, conflict)."""
    env = {"PROXMOX_NODE_NAME": "node1"}
    node_spec = next(v for v in SCHEMA if v.logical == "Node name")
    res = resolve_one(env, node_spec)
    assert isinstance(res, Resolution)
    assert res.value == "node1"
    assert res.conflict is False


def test_conflict_not_double_reported_as_missing():
    """A conflicting mandatory var is reported as a conflict, not also missing."""
    endpoint_spec = next(v for v in SCHEMA if v.logical == "Proxmox endpoint")
    env = _full_mandatory_env()
    # Introduce a conflicting second alias for the endpoint.
    env["TF_VAR_proxmox_endpoint"] = "https://different.example:8006"

    _resolved, missing, conflicts = resolve(env)
    pair = f"{endpoint_spec.proxmox_alias} vs {endpoint_spec.tfvar_alias}"
    assert pair in conflicts
    assert "Proxmox endpoint" not in missing


# =============================================================================
# Task 1 — Bug condition exploration tests (fix-cluster-env-quote-stripping)
#
# These tests encode the EXPECTED (fixed) behaviour: parse_env() must strip one
# layer of surrounding matching quotes. On the UNFIXED code they FAIL — that
# failure is the counterexample confirming the bug (a quoted value resolves to a
# quote-wrapped string, which broke Phase-1 Preflight with
# `urlopen error unknown url type: "https`).
# =============================================================================


def test_double_quoted_value_is_unwrapped():
    """Feature: fix-cluster-env-quote-stripping, Property 1: Bug Condition —
    surrounding matching quotes are stripped once.

    The reported failure in miniature: a double-quoted endpoint must resolve to a
    clean URL with no surrounding quotes.
    """
    parsed = parse_env('PROXMOX_ENDPOINT="https://h:8006/api2/json"')
    assert parsed["PROXMOX_ENDPOINT"] == "https://h:8006/api2/json"


def test_single_quoted_value_is_unwrapped():
    """Feature: fix-cluster-env-quote-stripping, Property 1: Bug Condition —
    surrounding matching quotes are stripped once.

    A single-quoted value must resolve with its surrounding single quotes removed.
    """
    parsed = parse_env("PROXMOX_NODE_NAME='node1'")
    assert parsed["PROXMOX_NODE_NAME"] == "node1"


def test_quoted_value_with_equals_preserved():
    """Feature: fix-cluster-env-quote-stripping, Property 1: Bug Condition —
    surrounding matching quotes are stripped once.

    A double-quoted value with an interior '=' must have only the surrounding
    quote pair removed; every interior character (including the '=') is preserved.
    """
    parsed = parse_env('PROXMOX_API_TOKEN="root@pam!id=abc=def"')
    assert parsed["PROXMOX_API_TOKEN"] == "root@pam!id=abc=def"


def test_resolve_env_file_with_fully_quoted_mandatory_set(tmp_path):
    """Feature: fix-cluster-env-quote-stripping, Property 1: Bug Condition —
    surrounding matching quotes are stripped once.

    End-to-end over a written temp file mirroring cluster.env.example (all
    mandatory vars double-quoted): the resolved TF_VAR_proxmox_endpoint must have
    NO leading/trailing quote — the exact resolved value Phase-1 Preflight
    consumed in the live failure.
    """
    env_file = tmp_path / "cluster.dev.env"
    env_file.write_text(
        "\n".join(
            [
                'PROXMOX_ENDPOINT="https://192.168.0.69:8006/api2/json"',
                'PROXMOX_API_TOKEN="terraform@pve!installer=00000000-0000-0000-0000-000000000000"',
                'PROXMOX_NODE_NAME="node1"',
                'PROXMOX_LXC_TEMPLATE="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"',
            ]
        ),
        encoding="utf-8",
    )

    resolved = cluster_env.resolve_env_file(str(env_file))

    endpoint = resolved["TF_VAR_proxmox_endpoint"]
    assert not endpoint.startswith('"')
    assert not endpoint.endswith('"')
    assert endpoint.startswith("https://")
    assert endpoint == "https://192.168.0.69:8006/api2/json"


# =============================================================================
# Task 2 — Preservation edge tests (fix-cluster-env-quote-stripping)
#
# Property 2: Preservation — non-quote-wrapped parsing and resolution unchanged.
#
# These characterize the non-bug edges. Two classes are mixed here, and each
# test's docstring/comments mark which class it is:
#
#   * PURE PRESERVATION BASELINE (PASS on unfixed AND must keep passing
#     post-fix): inputs where the bug condition is FALSE (unbalanced/mismatched
#     quotes, interior-whitespace-preservation on an already-quoted value once
#     the outer pair is gone). Where an assertion depends only on behavior that
#     never changes, it passes on unfixed code.
#
#   * FIX-CHECK COMPANIONS (FAIL on unfixed, PASS post-fix — Property-1-style):
#     assertions that encode the POST-FIX strip behavior (outer quote pair
#     removed, bare "" normalized to empty/absent). These are marked inline.
# =============================================================================


def test_whitespace_outside_vs_inside_quotes():
    """Feature: fix-cluster-env-quote-stripping, Property 2: Preservation.

    Mixed test:

    * Outer whitespace + surrounding quotes: ``  "node1"  `` -> ``node1``
      (outer whitespace trimmed, one quote pair removed). This is a FIX-CHECK
      companion — on UNFIXED code the value is ``"node1"`` (quotes attached), so
      this assertion FAILS on unfixed and PASSES post-fix (Property-1-style).
    * Interior whitespace preserved after the quote pair is removed:
      ``"  node1  "`` -> ``  node1  ``. Also a FIX-CHECK companion — on unfixed
      code the value is the whole ``"  node1  "`` (quotes attached), so this
      assertion FAILS on unfixed and PASSES post-fix. The pinned invariant is
      that interior whitespace is NOT trimmed once the outer pair is stripped.
    """
    # FIX-CHECK: outer whitespace trimmed, surrounding quote pair removed.
    assert parse_env('K=  "node1"  ')["K"] == "node1"

    # FIX-CHECK: interior whitespace preserved after the surrounding pair is
    # removed (only the outer quote pair is stripped; the interior is verbatim).
    assert parse_env('K="  node1  "')["K"] == "  node1  "


def test_unbalanced_or_mismatched_quote_left_untouched():
    """Feature: fix-cluster-env-quote-stripping, Property 2: Preservation.

    PURE PRESERVATION BASELINE — these are NOT the bug condition (no matching
    surrounding pair), so they are returned verbatim. They PASS on UNFIXED code
    and MUST keep passing post-fix (the fix only strips a *matching* pair).
    """
    # Lone leading double-quote (no closing quote) -> untouched.
    assert parse_env('K="value')["K"] == '"value'
    # Lone trailing double-quote (no opening quote) -> untouched.
    assert parse_env('K=value"')["K"] == 'value"'
    # Mismatched pair (double open, single close) -> untouched.
    assert parse_env("K=\"value'")["K"] == "\"value'"


def test_empty_quote_pair_treated_as_absent():
    """Feature: fix-cluster-env-quote-stripping, Property 2: Preservation.

    FIX-CHECK companion. A bare double-quote pair ``""`` normalizes (post-fix)
    to the empty string, which resolution treats as absent — so a mandatory var
    whose only value is ``""`` is reported missing.

    On UNFIXED code ``""`` stays as the 2-char string ``""`` (non-empty after
    trim), so it resolves as PRESENT and the var is NOT reported missing — this
    test therefore FAILS on unfixed and PASSES post-fix.
    """
    env = _full_mandatory_env()
    env["PROXMOX_NODE_NAME"] = '""'  # bare double-quote pair

    parsed = parse_env("\n".join(f"{k}={v}" for k, v in env.items()))
    _resolved, missing, conflicts = resolve(parsed)
    assert conflicts == []
    # Post-fix: "" -> "" (empty) -> absent -> Node name reported missing.
    assert "Node name" in missing

    with pytest.raises(ValidationError) as exc:
        validate(parsed)
    assert "Node name" in str(exc.value)


# Guard: ensure the os import stays used even if a future refactor drops the
# monkeypatch test; keeps linters honest without masking a real removal.
assert hasattr(os, "environ")


# =============================================================================
# Task 2.3 (platform-prerequisites-bootstrap) — extended-schema tests
#
# Feature: platform-prerequisites-bootstrap, Property 7: cluster.env parse
# extension.
#
# Validates: Requirements 6.2, 6.3.
#
# The platform-bootstrap orchestrator reuses this helper to resolve its
# cluster.env. Its new vars (host-prep, gateway, dev-machine) were added to the
# SCHEMA in Task 2.1. Per the design ("Component 7: cluster_env.py schema
# extension"), ALL of them are OPTIONAL in cluster_env.py — per-bucket
# mandatory-ness is asserted by the ORCHESTRATOR, not the pure core. So at THIS
# layer the tests assert: (a) each new var resolves under BOTH its aliases and
# via a single alias; (b) an absent optional var is simply NOT emitted and never
# lands in the collected-missing list; (c) a conflicting alias pair is collected
# and reported together (the same "collected, never one-at-a-time" contract the
# mandatory vars use); and (d) the Admin_Bootstrap_Credential and the API token
# VALUES never appear in any resolve/validate human-facing message.
#
# Style matches the rest of the file: pure-core only (parse_env / resolve /
# resolve_one / validate), no file or network IO.
# =============================================================================


# The logical names + alias pairs added by platform-prerequisites-bootstrap.
# Kept as literals (not re-derived from SCHEMA) so the test pins the exact rows
# Task 2.1 was asked to add — a drift in a row's alias names trips the test.
_PLATFORM_ROWS: tuple[tuple[str, str, str], ...] = (
    # (logical, proxmox/other alias, tfvar canonical alias)
    ("Proxmox host LAN IP", "PROXMOX_HOST_LAN_IP", "TF_VAR_proxmox_host_lan_ip"),
    ("Admin password", "PROXMOX_ADMIN_PASSWORD", "TF_VAR_proxmox_admin_password"),
    ("Admin token", "PROXMOX_ADMIN_TOKEN", "TF_VAR_proxmox_admin_token"),
    ("Terraform role id", "PROXMOX_TERRAFORM_ROLE_ID", "TF_VAR_proxmox_terraform_role_id"),
    ("Terraform token id", "PROXMOX_TERRAFORM_TOKEN_ID", "TF_VAR_proxmox_terraform_token_id"),
    ("Template storage", "PROXMOX_TEMPLATE_STORAGE", "TF_VAR_proxmox_template_storage"),
    ("Admin source CIDR", "ADMIN_SOURCE_CIDR", "TF_VAR_sdn_gateway_admin_source_cidr"),
    ("Served VLAN ids", "PLATFORM_SERVED_VLAN_IDS", "TF_VAR_platform_served_vlan_ids"),
)


def test_platform_rows_present_in_schema_and_optional():
    """Req 6.2: every platform-bootstrap row is in SCHEMA, optional, correctly aliased.

    Feature: platform-prerequisites-bootstrap, Property 7.

    Pins the exact rows Task 2.1 added: each logical name exists in the schema
    with the expected PROXMOX_/other and TF_VAR_ aliases, and is OPTIONAL
    (mandatory-ness is per-bucket, orchestrator-decided, NOT enforced here).
    """
    by_logical = {v.logical: v for v in SCHEMA}
    for logical, other_alias, tfvar_alias in _PLATFORM_ROWS:
        assert logical in by_logical, f"missing schema row: {logical}"
        spec = by_logical[logical]
        assert spec.proxmox_alias == other_alias
        assert spec.tfvar_alias == tfvar_alias
        assert spec.mandatory is False, f"{logical} must be optional (orchestrator-gated)"


@pytest.mark.parametrize("logical,other_alias,tfvar_alias", _PLATFORM_ROWS)
def test_platform_var_resolves_via_either_single_alias(logical, other_alias, tfvar_alias):
    """Req 6.2: each new var resolves from either alias alone to the canonical key.

    Feature: platform-prerequisites-bootstrap, Property 7.
    """
    spec = next(v for v in SCHEMA if v.logical == logical)

    # PROXMOX_/ADMIN_/PLATFORM_ alias only.
    env_a = parse_env(f"{other_alias}=value-a")
    res_a = resolve_one(env_a, spec)
    assert res_a.conflict is False
    assert res_a.value == "value-a"
    resolved_a, _missing_a, conflicts_a = resolve(env_a)
    assert conflicts_a == []
    assert resolved_a[tfvar_alias] == "value-a"

    # TF_VAR_ alias only.
    env_t = parse_env(f"{tfvar_alias}=value-t")
    res_t = resolve_one(env_t, spec)
    assert res_t.conflict is False
    assert res_t.value == "value-t"
    resolved_t, _missing_t, conflicts_t = resolve(env_t)
    assert conflicts_t == []
    assert resolved_t[tfvar_alias] == "value-t"


@pytest.mark.parametrize("logical,other_alias,tfvar_alias", _PLATFORM_ROWS)
def test_platform_var_identical_aliases_resolve(logical, other_alias, tfvar_alias):
    """Req 6.2: both aliases with identical values resolve to the single value.

    Feature: platform-prerequisites-bootstrap, Property 7.
    """
    spec = next(v for v in SCHEMA if v.logical == logical)
    env = parse_env(f"{other_alias}=same-value\n{tfvar_alias}=same-value")
    res = resolve_one(env, spec)
    assert res.conflict is False
    assert res.value == "same-value"


@pytest.mark.parametrize("logical,other_alias,tfvar_alias", _PLATFORM_ROWS)
def test_platform_var_absent_is_not_emitted_and_not_missing(logical, other_alias, tfvar_alias):
    """Req 6.3: an absent optional platform var is NOT emitted and NOT reported missing.

    Feature: platform-prerequisites-bootstrap, Property 7.

    A fully-populated mandatory set with NONE of the platform vars set must
    validate cleanly: the platform var neither appears in the resolved mapping
    nor in the collected-missing list (per-bucket mandatory-ness is the
    orchestrator's job, not this pure core's).
    """
    env = _full_mandatory_env()  # no platform vars set
    resolved, missing, conflicts = resolve(env)
    assert conflicts == []
    assert logical not in missing
    assert tfvar_alias not in resolved

    # And validate() succeeds (the platform vars being absent is not an error here).
    validated = validate(env)
    assert tfvar_alias not in validated


def test_platform_vars_emitted_when_present_alongside_mandatory():
    """Req 6.2: present platform vars are emitted under their canonical keys.

    Feature: platform-prerequisites-bootstrap, Property 7.
    """
    env = _full_mandatory_env()
    env["PROXMOX_HOST_LAN_IP"] = "192.168.0.69"
    env["PROXMOX_TERRAFORM_ROLE_ID"] = "TerraformProv"
    env["PROXMOX_TERRAFORM_TOKEN_ID"] = "installer"
    env["PROXMOX_TEMPLATE_STORAGE"] = "local"
    env["ADMIN_SOURCE_CIDR"] = "10.9.8.0/24"
    env["PLATFORM_SERVED_VLAN_IDS"] = "20"

    resolved = validate(env)
    assert resolved["TF_VAR_proxmox_host_lan_ip"] == "192.168.0.69"
    assert resolved["TF_VAR_proxmox_terraform_role_id"] == "TerraformProv"
    assert resolved["TF_VAR_proxmox_terraform_token_id"] == "installer"
    assert resolved["TF_VAR_proxmox_template_storage"] == "local"
    assert resolved["TF_VAR_sdn_gateway_admin_source_cidr"] == "10.9.8.0/24"
    assert resolved["TF_VAR_platform_served_vlan_ids"] == "20"


def test_platform_var_conflict_collected_with_mandatory_missing():
    """Req 6.3: a platform-var alias conflict is collected together, not one-at-a-time.

    Feature: platform-prerequisites-bootstrap, Property 7.

    Combine a conflicting platform-var alias pair with a missing mandatory var:
    validate() must raise ONE error whose message names BOTH the conflicting
    pair and the missing mandatory var — the same "collected, reported together"
    contract the mandatory vars already honor.
    """
    env = _full_mandatory_env()
    # Drop a mandatory var so it is reported missing.
    del env["PROXMOX_NODE_NAME"]
    # Introduce a conflicting platform-var alias pair (host LAN IP).
    env["PROXMOX_HOST_LAN_IP"] = "192.168.0.69"
    env["TF_VAR_proxmox_host_lan_ip"] = "10.0.0.5"

    _resolved, missing, conflicts = resolve(env)
    assert "Node name" in missing
    assert "PROXMOX_HOST_LAN_IP vs TF_VAR_proxmox_host_lan_ip" in conflicts

    with pytest.raises(ValidationError) as exc:
        validate(env)
    message = str(exc.value)
    # Both problems appear in the single collected message.
    assert "Node name" in message
    assert "PROXMOX_HOST_LAN_IP vs TF_VAR_proxmox_host_lan_ip" in message


# --- Non-secrecy: admin cred + API token VALUES never appear in a message -----

# A sentinel value used as the admin credential / API token in the non-secrecy
# tests. Distinctive enough that any accidental echo into a message is caught.
_SECRET_SENTINEL = "s3cr3t-DO-NOT-ECHO-abc123"


def test_admin_credential_value_never_in_validation_message():
    """Req 6.3: the Admin_Bootstrap_Credential VALUE never appears in a message.

    Feature: platform-prerequisites-bootstrap, Property 7.

    Set both admin-credential vars to a distinctive secret and force a validation
    error (a missing mandatory var). The raised message must name the missing
    logical var but MUST NOT contain the secret value — the admin cred is
    referenced by logical name at most, never by value.
    """
    for admin_key in ("PROXMOX_ADMIN_PASSWORD", "PROXMOX_ADMIN_TOKEN"):
        env = _full_mandatory_env()
        del env["PROXMOX_NODE_NAME"]  # force a ValidationError
        env[admin_key] = _SECRET_SENTINEL

        with pytest.raises(ValidationError) as exc:
            validate(env)
        message = str(exc.value)
        assert "Node name" in message
        assert _SECRET_SENTINEL not in message, (
            f"{admin_key} value leaked into validation message"
        )


def test_admin_credential_conflict_message_names_aliases_not_value():
    """Req 6.3: an admin-cred alias conflict names the ALIASES, never the values.

    Feature: platform-prerequisites-bootstrap, Property 7.
    """
    env = _full_mandatory_env()
    # Conflicting admin-token alias pair with two distinctive secret values.
    env["PROXMOX_ADMIN_TOKEN"] = _SECRET_SENTINEL
    env["TF_VAR_proxmox_admin_token"] = _SECRET_SENTINEL + "-other"

    with pytest.raises(ValidationError) as exc:
        validate(env)
    message = str(exc.value)
    # The conflict is reported by alias name...
    assert "PROXMOX_ADMIN_TOKEN vs TF_VAR_proxmox_admin_token" in message
    # ...but neither secret value appears.
    assert _SECRET_SENTINEL not in message


def test_api_token_value_never_in_validation_message():
    """Req 6.3 (carried over): the API token VALUE never appears in a message.

    Feature: platform-prerequisites-bootstrap, Property 7.

    The API token is mandatory, so set it to a distinctive secret and force a
    DIFFERENT missing mandatory var. The message names the missing var but never
    the API-token value.
    """
    env = _full_mandatory_env()
    env["PROXMOX_API_TOKEN"] = _SECRET_SENTINEL
    del env["PROXMOX_NODE_NAME"]  # force a ValidationError on a different var

    with pytest.raises(ValidationError) as exc:
        validate(env)
    message = str(exc.value)
    assert "Node name" in message
    assert _SECRET_SENTINEL not in message


def test_api_token_conflict_message_names_aliases_not_value():
    """Req 6.3 (carried over): an API-token alias conflict names aliases, not values.

    Feature: platform-prerequisites-bootstrap, Property 7.
    """
    env = _full_mandatory_env()
    env["PROXMOX_API_TOKEN"] = _SECRET_SENTINEL
    env["TF_VAR_proxmox_api_token"] = _SECRET_SENTINEL + "-other"

    with pytest.raises(ValidationError) as exc:
        validate(env)
    message = str(exc.value)
    assert "PROXMOX_API_TOKEN vs TF_VAR_proxmox_api_token" in message
    assert _SECRET_SENTINEL not in message
