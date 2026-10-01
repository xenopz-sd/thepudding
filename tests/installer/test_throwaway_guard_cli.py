"""Example/unit tests for the IO wrapper of ``throwaway_guard.py``.

Feature: agent-live-test-authorization (Task 4.2; design "Component 1 -> Thin IO
wrapper (main)"; Testing Strategy "Example/unit tests").

The pure decision core (``decide`` and its helpers) is covered by the Hypothesis
property tests in ``test_throwaway_guard.py`` (Task 4.1). THIS file exercises the
thin IO seam that Task 4.1 deliberately does not: allowlist YAML parsing
(``load_allowlist``), the ``main`` CLI exit-code contract, and — the key safety
guarantee — that ``main`` never emits the API token to stdout.

Idiom mirrors ``test_cluster_env.py``: tmp files via ``tmp_path``, ``capsys`` for
stdout, ``monkeypatch`` for environment isolation, and calling ``main(argv)``
directly and asserting the returned int. The ``conftest.py`` in this directory
adds ``scripts/installer/`` to ``sys.path`` so ``import throwaway_guard`` resolves.
"""

from __future__ import annotations

import json

import pytest

import throwaway_guard as g


# --- Shared helpers -----------------------------------------------------------

# A sentinel token value used by the non-secrecy tests. It is deliberately
# distinctive so a substring search in stdout is unambiguous. It is NOT a real
# credential — it is placeholder text whose only job is to be searched for.
_SENTINEL_TOKEN = "SENTINEL-TOKEN-DO-NOT-LEAK-zzz=="

# The identity spelling the tmp env files below resolve to. The allowlist entry
# in the AUTHORIZED-path tests must match this after normalization.
_ENDPOINT = "https://shrimp.lan:8006/api2/json"
_NODE = "shrimp"


def _write_env_file(tmp_path, *, endpoint=_ENDPOINT, node=_NODE, token=_SENTINEL_TOKEN):
    """Write a valid throwaway cluster-env file and return its path (str).

    Populates every mandatory ``cluster_env`` var (endpoint, API token, node
    name, LXC template) so ``resolve_env_file`` succeeds and the identity
    resolves. The token defaults to the leak sentinel so the non-secrecy tests
    can assert it never surfaces in stdout.
    """
    env_file = tmp_path / ".env.throwaway"
    env_file.write_text(
        "\n".join(
            [
                f"PROXMOX_ENDPOINT={endpoint}",
                f"PROXMOX_API_TOKEN={token}",
                f"PROXMOX_NODE_NAME={node}",
                "PROXMOX_LXC_TEMPLATE=local:vztmpl/debian-12-standard.tar.zst",
            ]
        ),
        encoding="utf-8",
    )
    return str(env_file)


def _write_allowlist(tmp_path, entries, *, name="throwaway-clusters.yml"):
    """Write an allowlist YAML from an iterable of (endpoint_host, node) pairs."""
    lines = ["throwaway_clusters:"]
    for host, node in entries:
        lines.append(f"  - endpoint_host: {host}")
        lines.append(f"    node_name: {node}")
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# =============================================================================
# 1. Allowlist parse — call g.load_allowlist directly on tmp files
#    Fail-closed on junk (absent / empty / malformed / wrong shape); a valid
#    file yields a normalized tuple of AllowEntry.
#    Validates: Requirement 2.3.
# =============================================================================


def test_load_allowlist_absent_path_returns_empty(tmp_path):
    """Req 2.3: an absent allowlist path fails closed to ()."""
    missing = tmp_path / "does-not-exist.yml"
    assert g.load_allowlist(str(missing)) == ()


def test_load_allowlist_empty_file_returns_empty(tmp_path):
    """Req 2.3: an empty file (YAML -> None) fails closed to ()."""
    path = tmp_path / "empty.yml"
    path.write_text("", encoding="utf-8")
    assert g.load_allowlist(str(path)) == ()


def test_load_allowlist_malformed_yaml_returns_empty(tmp_path):
    """Req 2.3: unparseable YAML fails closed to () (never raises)."""
    path = tmp_path / "malformed.yml"
    # Unterminated flow mapping — a YAML syntax error.
    path.write_text("throwaway_clusters: [ {endpoint_host: a", encoding="utf-8")
    assert g.load_allowlist(str(path)) == ()


def test_load_allowlist_top_level_not_a_dict_returns_empty(tmp_path):
    """Req 2.3: a top-level non-mapping (a bare list) fails closed to ()."""
    path = tmp_path / "list.yml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert g.load_allowlist(str(path)) == ()


def test_load_allowlist_missing_key_returns_empty(tmp_path):
    """Req 2.3: a dict without the throwaway_clusters key fails closed to ()."""
    path = tmp_path / "wrongkey.yml"
    path.write_text("other_key:\n  - endpoint_host: h\n    node_name: n\n", encoding="utf-8")
    assert g.load_allowlist(str(path)) == ()


def test_load_allowlist_key_not_a_list_returns_empty(tmp_path):
    """Req 2.3: throwaway_clusters present but not a list fails closed to ()."""
    path = tmp_path / "notalist.yml"
    path.write_text("throwaway_clusters: not-a-list\n", encoding="utf-8")
    assert g.load_allowlist(str(path)) == ()


def test_load_allowlist_drops_items_missing_a_field(tmp_path):
    """Req 2.3: items missing endpoint_host or node_name are dropped; valid kept."""
    path = tmp_path / "partial.yml"
    path.write_text(
        "\n".join(
            [
                "throwaway_clusters:",
                "  - endpoint_host: keep.lan",  # missing node_name -> dropped
                "  - node_name: orphan",  # missing endpoint_host -> dropped
                "  - endpoint_host: good.lan",  # complete -> kept
                "    node_name: good",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = g.load_allowlist(str(path))
    assert result == (g.AllowEntry(endpoint_host="good.lan", node_name="good"),)


def test_load_allowlist_valid_file_normalizes_and_trims(tmp_path):
    """Req 2.3: a valid file -> tuple of AllowEntry with normalized host + trimmed node.

    The endpoint host is reduced by ``normalize_host`` (scheme/port/path/case
    stripped) and the node name is trimmed, so the loaded entries are directly
    comparable to a resolved ``ClusterIdentity``.
    """
    path = _write_allowlist(
        tmp_path,
        [
            ("https://Shrimp.LAN:8006/api2/json", "  shrimp  "),
            ("192.168.0.69", "node2"),
        ],
    )
    result = g.load_allowlist(path)
    assert result == (
        g.AllowEntry(endpoint_host="shrimp.lan", node_name="shrimp"),
        g.AllowEntry(endpoint_host="192.168.0.69", node_name="node2"),
    )


# =============================================================================
# 2. main() exit codes — 0 AUTHORIZED / 3 REFUSED / 2 usage
#    Env armed/unset via monkeypatch; identity resolved from --env-file.
#    Validates: Requirement 4.4 (exit codes); 3.3 (identity from env file).
# =============================================================================


def _argv(env_file, allowlist, *, service="svc07"):
    return ["--service", service, "--env-file", env_file, "--allowlist", allowlist]


def test_main_armed_and_allowlisted_returns_zero_authorized(tmp_path, monkeypatch, capsys):
    """Req 4.4: armed + identity on allowlist -> exit 0, stdout authorized:true."""
    monkeypatch.setenv("AGENT_LIVE_AUTHORIZED", "1")
    monkeypatch.delenv("AGENT_LIVE_AUTHORIZED_SERVICES", raising=False)
    env_file = _write_env_file(tmp_path)
    allowlist = _write_allowlist(tmp_path, [("shrimp.lan", "shrimp")])

    rc = g.main(_argv(env_file, allowlist))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["authorized"] is True


def test_main_unset_interlock_returns_three_refused(tmp_path, monkeypatch, capsys):
    """Req 4.4: unset interlock -> exit 3, authorized:false, reason names interlock."""
    monkeypatch.delenv("AGENT_LIVE_AUTHORIZED", raising=False)
    monkeypatch.delenv("AGENT_LIVE_AUTHORIZED_SERVICES", raising=False)
    env_file = _write_env_file(tmp_path)
    allowlist = _write_allowlist(tmp_path, [("shrimp.lan", "shrimp")])

    rc = g.main(_argv(env_file, allowlist))

    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["authorized"] is False
    assert "interlock" in payload["reason"].lower()


def test_main_armed_but_not_allowlisted_returns_three(tmp_path, monkeypatch, capsys):
    """Req 4.4: armed but identity NOT on allowlist -> exit 3, reason names it.

    The allowlist holds a DIFFERENT host, so the resolved shrimp.lan/shrimp
    identity is refused with the "not on throwaway allowlist" reason.
    """
    monkeypatch.setenv("AGENT_LIVE_AUTHORIZED", "true")
    monkeypatch.delenv("AGENT_LIVE_AUTHORIZED_SERVICES", raising=False)
    env_file = _write_env_file(tmp_path)
    allowlist = _write_allowlist(tmp_path, [("other-host.lan", "othernode")])

    rc = g.main(_argv(env_file, allowlist))

    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["authorized"] is False
    assert "not on throwaway allowlist" in payload["reason"]


def test_main_armed_but_env_file_absent_returns_three(tmp_path, monkeypatch, capsys):
    """Req 4.4 / 3.3: armed but env file absent -> exit 3, identity unresolved.

    The identity is resolved from the passed ``--env-file``; when that file does
    not exist, resolution fails closed to None and the guard refuses with the
    "cluster identity unresolved" reason.
    """
    monkeypatch.setenv("AGENT_LIVE_AUTHORIZED", "1")
    monkeypatch.delenv("AGENT_LIVE_AUTHORIZED_SERVICES", raising=False)
    missing_env = str(tmp_path / ".env.throwaway.absent")
    allowlist = _write_allowlist(tmp_path, [("shrimp.lan", "shrimp")])

    rc = g.main(_argv(missing_env, allowlist))

    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["authorized"] is False
    assert payload["reason"] == "cluster identity unresolved"
    assert payload["identity"] is None


def test_main_missing_required_arg_raises_systemexit_two(tmp_path, monkeypatch):
    """Req 4.4: a usage error (missing required arg) -> argparse SystemExit(2)."""
    monkeypatch.setenv("AGENT_LIVE_AUTHORIZED", "1")
    allowlist = _write_allowlist(tmp_path, [("shrimp.lan", "shrimp")])

    # Omit the mandatory --env-file argument entirely.
    with pytest.raises(SystemExit) as exc:
        g.main(["--service", "svc07", "--allowlist", allowlist])

    assert exc.value.code == 2


# =============================================================================
# 3. Non-secrecy — the key guarantee.
#    The API token value must NEVER appear in main's stdout (authorized OR
#    refused paths); the non-secret identity fields (host/node) DO appear.
#    Validates: Requirement 3.3 / security + Property 6.
# =============================================================================


def test_main_never_emits_token_authorized_path(tmp_path, monkeypatch, capsys):
    """Req 3.3 / Property 6: on the AUTHORIZED path the token never reaches stdout.

    The env file carries the leak sentinel as its API token. The emitted JSON
    must carry the non-secret identity (host + node) but never the token string.
    """
    monkeypatch.setenv("AGENT_LIVE_AUTHORIZED", "1")
    monkeypatch.delenv("AGENT_LIVE_AUTHORIZED_SERVICES", raising=False)
    env_file = _write_env_file(tmp_path, token=_SENTINEL_TOKEN)
    allowlist = _write_allowlist(tmp_path, [("shrimp.lan", "shrimp")])

    rc = g.main(_argv(env_file, allowlist))
    out = capsys.readouterr().out

    assert rc == 0
    # The secret sentinel must not appear anywhere in stdout.
    assert _SENTINEL_TOKEN not in out
    # The resolved non-secret identity DID parse from the right file and appears.
    payload = json.loads(out)
    assert payload["identity"] == {"endpoint_host": "shrimp.lan", "node_name": "shrimp"}


def test_main_never_emits_token_refused_path(tmp_path, monkeypatch, capsys):
    """Req 3.3 / Property 6: on a REFUSED path the token never reaches stdout.

    Armed but the identity is not allowlisted -> REFUSED. The env file still
    carries the leak sentinel; stdout must expose the identity for audit but
    never the token.
    """
    monkeypatch.setenv("AGENT_LIVE_AUTHORIZED", "1")
    monkeypatch.delenv("AGENT_LIVE_AUTHORIZED_SERVICES", raising=False)
    env_file = _write_env_file(tmp_path, token=_SENTINEL_TOKEN)
    allowlist = _write_allowlist(tmp_path, [("other-host.lan", "othernode")])

    rc = g.main(_argv(env_file, allowlist))
    out = capsys.readouterr().out

    assert rc == 3
    assert _SENTINEL_TOKEN not in out
    # The resolved identity (proving we parsed the right file) is present.
    payload = json.loads(out)
    assert payload["identity"] == {"endpoint_host": "shrimp.lan", "node_name": "shrimp"}
