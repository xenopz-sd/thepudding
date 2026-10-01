"""Offline guard for the SSH-public-key-injection half of
fix-cluster-env-endpoint-path (member (c)).

Feature: fix-cluster-env-endpoint-path — member (c) of the "Proxmox connection
settings not derived from the operator's cluster config" family: (a) endpoint
path, (b) TLS verification, (c) SSH public-key injection for guest reachability.

Root cause (same class as (a) and (b)): a Proxmox/Terraform connection input
that the operator's cluster config should supply was never fed to the Terraform
consumer. The SVC-07 LXCs are created with NO authorized SSH key because the
Terraform var ``operator_ssh_public_keys`` (list(string), default ``[]``) was
never passed, so ``main.tf``'s ``dynamic "user_account"`` block is omitted
(strict no-op) and Ansible's Phase-3 ``common.yml`` run fails with
``Permission denied (publickey,password)``.

The fix has two seams, guarded here offline:

1. ``scripts/installer/cluster_env.py`` gains an OPTIONAL schema row that
   resolves the operator-supplied PATH string (``PROXMOX_SSH_PUBLIC_KEY_FILE`` /
   ``TF_VAR_operator_ssh_public_key_file``) — it does NO file IO and NO JSON-list
   encoding, staying a generic path/string resolver. Absent => not emitted.

2. ``ansible/playbooks/svc-07-bootstrap.yml`` reads that path, ``expanduser``s
   it, reads the ``.pub`` file, and JSON-encodes a 1-element ``list(string)`` into
   ``TF_VAR_operator_ssh_public_keys`` (a Terraform list, so it MUST be JSON, e.g.
   ``["ssh-ed25519 AAAA... comment"]``). Absent => ``"[]"`` => Terraform block
   omitted => strict no-op.

Because the file-read + JSON encoding lives only in the playbook's Jinja (no
Python helper), this guard has three independent halves:

* cluster_env schema resolution (absent => not emitted; present => the path
  string), consistent with the existing schema tests.
* a faithful reproduction of the list/JSON encoding rule the playbook uses
  (``json.loads(result) == [pubkey]``; absent => ``"[]"``), using a fake key
  literal — never a real key.
* a grep-style structural assertion that ``svc07_tf_env`` carries
  ``TF_VAR_operator_ssh_public_keys`` wired from the resolved pub-key path (the
  read task + the ``to_json`` list encoding), so the wiring can't silently
  regress.

The AUTHORITATIVE check remains the live Phase-3 ``common.yml`` SSH now
succeeding after a clean re-provision; this offline guard pins the resolution +
encoding rule and its presence in the installer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import cluster_env
from cluster_env import resolve

# tests/installer/test_ssh_public_key.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
# The SSH-public-key injection seam moved out of the ~1900-line
# svc-07-bootstrap.yml when it was thinned to a ~150-line orchestrator
# (svc07-installer-simplification, Task 3.1/11). Its two halves now live in
# distinct files:
#   * the PATH bind + expanduser + stat/assert fail-fast + .pub read +
#     JSON-list encode (into svc07_operator_ssh_public_keys_json) is Phase 1,
#     now in ansible/roles/svc07_preflight/tasks/main.yml;
#   * the svc07_tf_env entry TF_VAR_operator_ssh_public_keys (fed from that
#     encoded fact) is a Phase-2 default, now in
#     ansible/roles/svc07_provision/defaults/main.yml.
# The structural guards below read whichever file now owns the asserted content;
# their INTENT is unchanged.
_PREFLIGHT_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_preflight" / "tasks" / "main.yml"
)
_PROVISION_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_provision" / "defaults" / "main.yml"
)

# A deliberately fake public-key literal — never a real key (offline, no infra).
_FAKE_PUBKEY = "ssh-ed25519 AAAAFAKE test@example"

# The canonical passthrough key cluster_env.py emits for the resolved PATH — the
# ``_file`` suffix marks it as the path, distinct from the list-typed
# ``TF_VAR_operator_ssh_public_keys`` the guests actually consume.
_PATH_KEY = "TF_VAR_operator_ssh_public_key_file"

# The mandatory vars every resolve() call needs, so the SSH row is the only
# variable under test.
_BASE_ENV = {
    "PROXMOX_ENDPOINT": "https://h:8006",
    "PROXMOX_API_TOKEN": "tok",
    "PROXMOX_NODE_NAME": "node",
    "PROXMOX_LXC_TEMPLATE": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
}


# =============================================================================
# 1. cluster_env schema resolution — the SSH-key-file path is OPTIONAL.
# =============================================================================


def test_ssh_key_file_absent_is_not_emitted():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    With no ``PROXMOX_SSH_PUBLIC_KEY_FILE`` / ``TF_VAR_operator_ssh_public_key_file``
    in the env, the resolved mapping does NOT carry the passthrough path key —
    the playbook reads that absence as "no key -> empty [] list -> strict no-op".
    """
    resolved, missing, conflicts = resolve(dict(_BASE_ENV))
    assert _PATH_KEY not in resolved
    # Optional var absent must NOT be reported as missing/conflicting.
    assert missing == []
    assert conflicts == []


def test_ssh_key_file_present_resolves_the_path_via_proxmox_alias():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    The ``PROXMOX_``-prefixed alias resolves to the path string verbatim — the
    helper resolves a PATH, it does not read the file or encode a list.
    """
    env = dict(_BASE_ENV, PROXMOX_SSH_PUBLIC_KEY_FILE="~/.ssh/id_ed25519.pub")
    resolved, _, _ = resolve(env)
    assert resolved[_PATH_KEY] == "~/.ssh/id_ed25519.pub"


def test_ssh_key_file_present_resolves_the_path_via_tfvar_alias():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    The ``TF_VAR_``-prefixed alias resolves identically (single-alias path).
    """
    env = dict(_BASE_ENV)
    env["TF_VAR_operator_ssh_public_key_file"] = "/home/op/.ssh/git.pub"
    resolved, _, _ = resolve(env)
    assert resolved[_PATH_KEY] == "/home/op/.ssh/git.pub"


def test_ssh_key_file_conflicting_aliases_is_a_conflict():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    Defining BOTH aliases with differing non-empty values is a conflict, exactly
    like every other schema row — consistency with the existing alias contract.
    """
    env = dict(
        _BASE_ENV,
        PROXMOX_SSH_PUBLIC_KEY_FILE="~/.ssh/a.pub",
    )
    env["TF_VAR_operator_ssh_public_key_file"] = "~/.ssh/b.pub"
    resolved, missing, conflicts = resolve(env)
    assert _PATH_KEY not in resolved
    assert any("SSH_PUBLIC_KEY_FILE" in c for c in conflicts)


def test_ssh_key_file_is_optional_in_schema():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    The schema row is OPTIONAL (mandatory=False), so an env missing it still
    validates — provisioning without a key is the documented default.
    """
    row = next(v for v in cluster_env.SCHEMA if v.logical == "SSH public key file")
    assert row.mandatory is False
    assert row.proxmox_alias == "PROXMOX_SSH_PUBLIC_KEY_FILE"
    assert row.tfvar_alias == _PATH_KEY


# =============================================================================
# 2. The list/JSON encoding rule the playbook uses (faithful reproduction).
#
# The playbook computes, in Jinja:
#   [ lookup('file', path) | trim ] | to_json      (path set)
#   "[]"                                            (path unset)
# This mirrors that exactly in Python so the rule is pinned offline.
# =============================================================================


def _encode_keys_json(pubkey_contents: str | None) -> str:
    """Reproduce the playbook's list/JSON encoding rule.

    ``None`` (no path supplied) -> ``"[]"``. Otherwise a 1-element JSON list of
    the trimmed key contents, matching ``[ ... | trim ] | to_json``.
    """
    if pubkey_contents is None:
        return "[]"
    return json.dumps([pubkey_contents.strip()])


def test_encoding_absent_key_is_empty_json_list():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    No key supplied => ``"[]"`` — a valid empty JSON list Terraform reads as an
    empty ``list(string)`` => ``dynamic user_account`` omitted => strict no-op.
    """
    result = _encode_keys_json(None)
    assert result == "[]"
    assert json.loads(result) == []


def test_encoding_present_key_is_one_element_json_list():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    A supplied public key becomes a 1-element JSON list — the form a Terraform
    ``list(string)`` var requires (``json.loads(result) == [pubkey]``).
    """
    result = _encode_keys_json(_FAKE_PUBKEY)
    assert json.loads(result) == [_FAKE_PUBKEY]


def test_encoding_strips_surrounding_whitespace():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    The key contents are trimmed (a trailing newline from the ``.pub`` file must
    not leak into the JSON), matching the playbook's ``| trim``.
    """
    result = _encode_keys_json(f"  {_FAKE_PUBKEY}\n")
    assert json.loads(result) == [_FAKE_PUBKEY]


# =============================================================================
# 3. Structural guard — the playbook actually wires it.
# =============================================================================


def test_playbook_tf_env_carries_operator_ssh_public_keys():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    ``svc07_tf_env`` MUST carry a ``TF_VAR_operator_ssh_public_keys`` entry fed
    from the encoded list fact (defaulting to ``"[]"``), so the Terraform list
    var is always defined and the wiring can't silently disappear. Repointed to
    ``svc07_provision/defaults/main.yml`` (where ``svc07_tf_env`` now lives) for
    the svc07-installer-simplification rework.
    """
    text = _PROVISION_DEFAULTS.read_text(encoding="utf-8")

    assert "TF_VAR_operator_ssh_public_keys" in text, (
        "svc07_tf_env is missing the TF_VAR_operator_ssh_public_keys entry — the "
        "guests would be created with no authorized SSH key and Ansible could not "
        "reach them."
    )
    # It must be fed from the encoded-list fact, defaulting to an empty list.
    pattern = re.compile(
        r"TF_VAR_operator_ssh_public_keys\s*:\s*['\"]?\{\{\s*"
        r"svc07_operator_ssh_public_keys_json\s*\|\s*default\(\s*['\"]\[\]['\"]\s*\)",
    )
    assert pattern.search(text), (
        "TF_VAR_operator_ssh_public_keys must be fed from "
        "svc07_operator_ssh_public_keys_json (default '[]'), so an unset key "
        "yields an empty list (strict no-op) and a set key yields the JSON list."
    )


def test_playbook_reads_pubkey_file_and_json_encodes_a_list():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    The Phase-1 preflight role must READ the resolved pub-key file and
    ``to_json``-encode a 1-element list — the read+encode seam that turns a PATH
    into the Terraform list value. Repointed to ``svc07_preflight/tasks/main.yml``
    (Task 3.1/11) — the seam moved verbatim out of the thinned play.
    """
    text = _PREFLIGHT_TASKS.read_text(encoding="utf-8")

    # Resolves the path from cluster_env's passthrough key + expanduser.
    assert "TF_VAR_operator_ssh_public_key_file" in text, (
        "the playbook must bind the resolved SSH-key-file PATH from cluster_env's "
        "TF_VAR_operator_ssh_public_key_file passthrough key."
    )
    assert "expanduser" in text, (
        "the playbook must expanduser the SSH-key-file path so a leading ~ resolves."
    )
    # Reads the file and JSON-encodes a 1-element list.
    encode = re.compile(
        r"\[\s*lookup\(\s*['\"]ansible\.builtin\.file['\"]\s*,\s*"
        r"svc07_ssh_public_key_file\s*\)\s*\|\s*trim\s*\]\s*\|\s*to_json",
    )
    assert encode.search(text), (
        "the playbook must read the .pub file via lookup('ansible.builtin.file', "
        "svc07_ssh_public_key_file) and JSON-encode it as a 1-element list "
        "([ ... | trim ] | to_json) for the Terraform list(string) var."
    )


def test_playbook_fails_fast_when_pubkey_path_set_but_file_missing():
    """Feature: fix-cluster-env-endpoint-path, member (c).

    A path set but pointing at a missing/unreadable file must HALT preflight —
    guard that the stat + assert-exists fail-fast is present (same philosophy as
    the env-file-absent gate). Repointed to ``svc07_preflight/tasks/main.yml``
    (Task 3.1/11) — the fail-fast moved verbatim into the Phase-1 role.
    """
    text = _PREFLIGHT_TASKS.read_text(encoding="utf-8")
    # The assert references the stat result's exists/readable — the fail-fast.
    assert "svc07_ssh_public_key_stat" in text
    assert re.search(r"svc07_ssh_public_key_stat\.stat\.exists", text), (
        "the playbook must assert the supplied SSH-key file exists (fail-fast)."
    )
