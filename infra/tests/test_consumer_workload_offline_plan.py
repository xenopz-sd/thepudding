"""Golden HCL-vs-Python cross-check for the project-VLAN Workload_Consumer_Root.

Feature: consumer-last-mile-hello-workload (tasks.md Task 2.3; design.md
"Testing Strategy" → "Terraform offline" / "HCL-vs-Python drift" bullet,
extended per ADR-0021; Requirements 2.2).

=== WHY THIS EXISTS (extends the ADR-0015/ADR-0021 golden cross-check to the
    non-catalog workload guest) ===

ADR-0015 records a standing caveat (re-affirmed by ADR-0021): the per-guest host
IP / hostname is derived TWICE — once in HCL at apply time (``proxmox-compute``
via ``cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)`` + the
``<identity>-<vlan>-<instance>`` hostname build) and once in the pure Python
derivation layer (``netfoundation.derivation`` / this feature's
``consumer_derivation``). A golden ``terraform plan`` cross-check must diff the
HCL side against the Python side so the two implementations can never silently
drift.

That cross-check already exists for the per-project **DB** guest
(``test_consumer_db_offline_plan.py`` — svc01 at VLAN 100 index 0). This module
EXTENDS it to the per-project **workload** guest — the NEW, non-catalog
``role_code`` guest ADR-0021 introduces — on the SAME project VLAN:

  * **HCL side** — an offline ``terraform plan -json`` of the new workload root
    ``infra/projects/dronefleet/workload`` with the guest ENABLED
    (``-var workload_enabled=true``). The ``workload/`` root resolves vlan_id
    from the registry, reads the ``role_code: workload`` host_index from the
    ledger, and threads both (plus the ADR-0021 ``role_code = "workload"``
    identity) into the shared ``proxmox-compute`` module. The plan's computed
    ``initialization.ip_config.ipv4.address`` + ``hostname`` ARE the HCL side.
  * **Python side** — ``consumer_db_host_ip(100, 1)`` (the thin reuse wrapper
    over ``netfoundation.derivation.host_ip`` — the SAME ``cidrhost`` formula,
    not re-derived) for the IP, ``netfoundation.role_hostname("workload", 100,
    1)`` for the ADR-0021 role-code hostname form, and
    ``netfoundation.peer_host_indices`` over the committed ledger for the
    Guard-4 peer-set VERDICT.

A drift between the ledger, the HCL ``cidrhost``/hostname build, and the Python
derivation fails HERE — for the non-catalog workload guest, confirming the
ADR-0021 role-code path is cross-checked exactly as the catalog svc_code path.

GATED on a terraform/tofu binary AND an installable provider; SKIPS cleanly
(never fails) when neither is available offline — same contract as the DB
sibling (``test_consumer_db_offline_plan.py``) and the SVC-09 / SVC-17-18
offline-plan siblings. NEVER runs a live apply (no Proxmox contact: the root
declares only SDN-attached resources with no read-time data sources, planned
with dummy non-contacting vars and ``-refresh=false``).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_consumer_workload_offline_plan.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import yaml

from conftest import requires_terraform, _terraform_binary

_THIS_DIR = Path(__file__).resolve().parent          # infra/tests/
_INFRA_DIR = _THIS_DIR.parent                        # infra/

# --------------------------------------------------------------------------- #
# Reuse the SHARED Python derivation symbols (testing-strategy.md "Verification
# Scope": reuse, do not fork). ``netfoundation`` lives under
# infra/platform-foundation/scripts; ``consumer_derivation`` under
# scripts/installer. Add both to sys.path with the same idiom the cross-tree
# tests use, then import the pure helpers whose output IS the Python side of the
# cross-check — INCLUDING the ADR-0021 role-code hostname helper
# (``role_hostname``), which is the extension of the stable hostname abstraction
# for the non-catalog identity, NOT a fork of the formula.
# --------------------------------------------------------------------------- #
_NETFOUNDATION_SCRIPTS_DIR = _INFRA_DIR / "platform-foundation" / "scripts"
if str(_NETFOUNDATION_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_NETFOUNDATION_SCRIPTS_DIR))

_INSTALLER_SCRIPTS_DIR = _INFRA_DIR.parent / "scripts" / "installer"
if str(_INSTALLER_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALLER_SCRIPTS_DIR))

from netfoundation import (  # noqa: E402  (after sys.path setup)
    peer_host_indices,
    role_hostname,
)
from consumer_derivation import consumer_db_host_ip  # noqa: E402

# --------------------------------------------------------------------------- #
# Fixtures of the golden case: the registered test slug `dronefleet` on VLAN 100,
# the non-catalog consumer WORKLOAD guest (role_code `workload`) at host_index 1
# -> 10.0.100.11, peer to the DB guest at index 0 (ADR-0021).
# --------------------------------------------------------------------------- #
_CONTAINER_TYPE = "proxmox_virtual_environment_container"
_SLUG = "dronefleet"
_VLAN_ID = 100
_HOST_INDEX = 1                      # the workload guest's ledger index -> .11
_DB_HOST_INDEX = 0                   # the DB guest's index (the workload's peer)
_ROLE_CODE = "workload"
_WORKLOAD_ROOT_REL = Path("projects") / _SLUG / "workload"

#: Ledger, read to prove the HCL plan resolves from the SAME committed source the
#: Python side reads (not a hard-coded fixture).
_HOST_INDICES_YAML = _INFRA_DIR / "platform-foundation" / "host-indices.yaml"


def _ledger_entry_identity(entry: dict) -> str:
    """Return a ledger entry's guest-identity code, enforcing exactly-one-of.

    Per ADR-0021, every ledger row is keyed by EXACTLY ONE of ``svc_code`` (a
    catalog guest) or ``role_code`` (a non-catalog guest, e.g. ``workload``).
    Mirrors the reader in ``test_consumer_db_offline_plan.py`` so the Python
    side is a genuine REUSE of the shared ``peer_host_indices`` symbol over the
    committed ledger rather than a forked re-implementation.
    """
    has_svc = entry.get("svc_code") is not None
    has_role = entry.get("role_code") is not None
    if has_svc == has_role:
        raise ValueError(
            "host-indices.yaml entry must carry EXACTLY ONE of 'svc_code' or "
            f"'role_code' (ADR-0021 exactly-one-of); got entry={entry!r}"
        )
    return entry["svc_code"] if has_svc else entry["role_code"]


def _load_read_view_ledger() -> dict:
    """Load host-indices.yaml into the READ-VIEW shape ``peer_host_indices``
    consumes (``{vlan_id: [{guest_key, host_index}, ...]}``), keying each row on
    its ADR-0021 identity (``<identity>[-component]``)."""
    raw = yaml.safe_load(_HOST_INDICES_YAML.read_text(encoding="utf-8"))
    view: dict[int, list[dict]] = {}
    for vlan_key, entries in raw.items():
        rows = []
        for e in entries:
            guest_key = _ledger_entry_identity(e)
            if e.get("component"):
                guest_key = f"{guest_key}-{e['component']}"
            rows.append({"guest_key": guest_key, "host_index": e["host_index"]})
        view[int(vlan_key)] = rows
    return view


# --- Python side of the cross-check (the "golden" expected values) -------- #

def _python_expected_ip() -> str:
    """Python mirror of HCL ``cidrhost("10.0.100.0/24", 10 + 1)`` via the shared
    reuse wrapper — the Python side of the host-IP cross-check. -> 10.0.100.11"""
    return consumer_db_host_ip(_VLAN_ID, _HOST_INDEX)


def _python_expected_hostname() -> str:
    """Python mirror of the module's ADR-0021 role-code hostname
    ``workload-<vlan>-01`` (NET-00 §4 analogue, ADR-0021)."""
    return role_hostname(_ROLE_CODE, _VLAN_ID, 1)  # "workload-100-01"


def _python_expected_peer_set() -> list[int]:
    """Python side of the peer-set VERDICT: every OTHER host index on VLAN 100,
    excluding the workload guest's own. VLAN 100 also carries the catalog DB
    guest (svc01, host_index 0 — ADR-0015/ADR-0019), so this set is ``[0]`` and
    the proxmox-compute Guard 4 engages (the two guests see each other)."""
    ledger = _load_read_view_ledger()
    return peer_host_indices(ledger, _VLAN_ID, _ROLE_CODE)


# --------------------------------------------------------------------------- #
# Offline-plan sandbox helper for the workload root (mirrors the DB sibling).
# Copies infra/ into a temp dir, drops a transient `backend "local" {}` override
# into the sandboxed workload/ root, init -reconfigure + plan with dummy
# non-contacting vars AND `-var workload_enabled=true` (the guest is OFF by
# default), show -json. Skips cleanly on an offline provider-install failure.
# --------------------------------------------------------------------------- #
_DUMMY_ENDPOINT = "https://dummy.invalid:8006/"
_DUMMY_TOKEN = "terraform@pve!ci=00000000-0000-0000-0000-000000000000"
_DUMMY_TEMPLATE = "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
_DUMMY_VARS = (
    "-var", f"proxmox_endpoint={_DUMMY_ENDPOINT}",
    "-var", f"proxmox_api_token={_DUMMY_TOKEN}",
    "-var", "proxmox_node_name=pve-test",
    "-var", "workload_vmid=4100",
    "-var", f"workload_template_file_id={_DUMMY_TEMPLATE}",
    # ENABLE the otherwise-default-off workload guest so the plan materializes
    # the container + the proxmox-compute module instance to cross-check.
    "-var", "workload_enabled=true",
)
_OVERRIDE_FILENAME = "zz_consumer_workload_offline_backend_override.tf"
_OVERRIDE_CONTENT = (
    "# Transient, test-scoped local backend override written into a SANDBOX\n"
    "# COPY only by test_consumer_workload_offline_plan.py. Overrides the\n"
    "# workload/ root's partial `backend \"http\" {}` so an offline\n"
    "# `terraform init -reconfigure` + `plan` succeed without a live GitLab\n"
    "# HTTP backend / CI_JOB_TOKEN. NEVER committed into a real root (PF FR-2).\n"
    "terraform {\n"
    "  backend \"local\" {}\n"
    "}\n"
)

_ENV_MARKERS = (
    "failed to install provider",
    "could not retrieve the list of available versions",
    "failed to query available provider packages",
    "could not connect", "network is unreachable", "dial tcp",
)


@contextmanager
def _consumer_workload_offline_plan() -> Iterator[list[dict]]:
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"
    sandbox = Path(tempfile.mkdtemp(prefix="consumer_workload_offline_plan_"))
    try:
        # Copy the whole infra/ tree so the workload/ root's relative reads
        # (../../../platform-foundation/{projects,host-indices}.yaml and the
        # ../../../modules/proxmox-compute module source) all resolve inside
        # the sandbox.
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / _WORKLOAD_ROOT_REL
        for stale in (target / ".terraform",):
            if stale.is_dir():
                shutil.rmtree(stale)
        for stale_file in (target / "terraform.tfstate",):
            if stale_file.is_file():
                stale_file.unlink()
        (target / _OVERRIDE_FILENAME).write_text(_OVERRIDE_CONTENT, encoding="utf-8")
        init = subprocess.run(
            [binary, "init", "-reconfigure", "-input=false", "-no-color"],
            cwd=target, capture_output=True, text=True,
        )
        if init.returncode != 0:
            combined = (init.stdout + init.stderr).lower()
            if any(m in combined for m in _ENV_MARKERS):
                pytest.skip(
                    "requires-infra, skipped: `terraform init` could not install/reuse "
                    "the provider offline — an environmental prerequisite unrelated to the "
                    f"cross-check under test.\ninit stderr tail:\n{init.stderr[-1200:]}"
                )
            pytest.fail(f"terraform init failed unexpectedly:\n{init.stdout}\n{init.stderr}")
        plan_path = target / "consumer_workload.tfplan"
        plan = subprocess.run(
            [
                binary, "plan", "-input=false", "-no-color", "-refresh=false",
                *_DUMMY_VARS,
                f"-out={plan_path}",
            ],
            cwd=target, capture_output=True, text=True,
        )
        assert plan.returncode == 0, (
            "offline `terraform plan` of the ENABLED dronefleet/workload root must succeed "
            f"with dummy vars; got exit {plan.returncode}:\n{plan.stdout}\n{plan.stderr}"
        )
        show = subprocess.run(
            [binary, "show", "-json", plan_path.name],
            cwd=target, capture_output=True, text=True,
        )
        assert show.returncode == 0 and show.stdout.strip(), (
            f"`terraform show -json` failed:\n{show.stdout}\n{show.stderr}"
        )
        data = json.loads(show.stdout)
        yield data.get("resource_changes", []) or []
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _first(seq):
    if isinstance(seq, list):
        return seq[0] if seq else {}
    return seq if seq is not None else {}


# --------------------------------------------------------------------------- #
# Golden HCL-vs-Python cross-check on the ENABLED workload guest (gated).
# --------------------------------------------------------------------------- #
@requires_terraform
class TestConsumerWorkloadProjectVlanCrossCheck:
    @staticmethod
    @pytest.fixture(scope="class")
    def workload_container() -> dict:
        with _consumer_workload_offline_plan() as changes:
            containers = [
                c["change"]["after"]
                for c in changes
                if c["type"] == _CONTAINER_TYPE
            ]
            assert len(containers) == 1, (
                f"the ENABLED workload/ root must plan exactly one {_CONTAINER_TYPE} "
                f"(the single consumer workload LXC); planned {len(containers)}"
            )
            return containers[0]

    @staticmethod
    @pytest.fixture(scope="class")
    def input_guard() -> dict:
        """The proxmox-compute Guard anchor's recorded input (carries the
        HCL-resolved vlan_id/host_index/peer_host_indices/role_code verdict)."""
        with _consumer_workload_offline_plan() as changes:
            guards = [
                c["change"]["after"]["input"]
                for c in changes
                if c["type"] == "terraform_data"
                and isinstance(c["change"]["after"].get("input"), dict)
                and c["change"]["after"]["input"].get("hostname")
            ]
            assert guards, "expected the proxmox-compute terraform_data.input_guards anchor in the plan"
            return guards[0]

    def test_hcl_ip_matches_python_derivation(self, workload_container):
        # HCL side: the plan's computed initialization.ip_config.ipv4.address.
        init = _first(workload_container.get("initialization"))
        ipv4 = _first(_first(init.get("ip_config")).get("ipv4") if init.get("ip_config") else None)
        hcl_addr = ipv4.get("address")
        # Python side: the shared reuse wrapper, CIDR-suffixed to match HCL.
        expected = f"{_python_expected_ip()}/24"
        assert hcl_addr == expected, (
            f"HCL-computed workload IP {hcl_addr!r} on VLAN {_VLAN_ID} must equal the "
            f"Python derivation {expected!r} (cidrhost 10.0.{_VLAN_ID}.0/24, 10+{_HOST_INDEX} "
            "= 10.0.100.11 — NET-00 §3). A mismatch is HCL-vs-Python derivation drift "
            "(ADR-0015, extended to the ADR-0021 workload guest)."
        )

    def test_hcl_hostname_matches_python_role_code_derivation(self, workload_container):
        # HCL side: the role-code hostname the proxmox-compute module built.
        init = _first(workload_container.get("initialization"))
        hcl_hostname = init.get("hostname")
        # Python side: the ADR-0021 role-code hostname helper.
        assert hcl_hostname == _python_expected_hostname(), (
            f"HCL-computed role-code hostname {hcl_hostname!r} must equal the Python-derived "
            f"{_python_expected_hostname()!r} (role_code={_ROLE_CODE!r} on VLAN {_VLAN_ID}, "
            "ADR-0021 non-catalog identity, NET-00 §4 analogue). A mismatch is HCL-vs-Python "
            "drift for the role-code hostname form."
        )

    def test_hcl_tag_is_project_workload(self, workload_container):
        assert workload_container.get("tags") == [f"proj-{_SLUG}-workload"], (
            f"workload guest tag must be ['proj-{_SLUG}-workload'] (ADR-0021 sanctioned "
            f"non-catalog convention); got {workload_container.get('tags')}"
        )

    def test_hcl_identity_is_role_code_not_svc_code(self, input_guard):
        # The guest is identified by the ADR-0021 role_code, NOT a svc_code —
        # the exactly-one-of identity the module's Guard 5 enforces.
        assert input_guard.get("role_code") == _ROLE_CODE, (
            f"HCL must identify the workload guest by role_code={_ROLE_CODE!r} (ADR-0021); "
            f"plan recorded role_code={input_guard.get('role_code')!r}"
        )
        assert input_guard.get("svc_code") is None, (
            "the workload guest must carry NO svc_code (exactly-one-of identity, ADR-0021); "
            f"plan recorded svc_code={input_guard.get('svc_code')!r}"
        )

    def test_hcl_peer_set_verdict_matches_python(self, input_guard):
        # HCL side: the peer_host_indices the workload/ root threaded (via the
        # ledger derivation) into proxmox-compute's Guard 4.
        hcl_peers = sorted(input_guard.get("peer_host_indices", []))
        # Python side: the ledger-derived peer set for the workload guest.
        python_peers = sorted(_python_expected_peer_set())
        assert hcl_peers == python_peers, (
            f"HCL peer_host_indices verdict {hcl_peers} must equal the Python "
            f"ledger-derived peer set {python_peers} for role_code={_ROLE_CODE!r} on VLAN "
            f"{_VLAN_ID}. The HCL ledger derivation and the Python derivation must never "
            "drift (ADR-0015 cross-check, extended to the ADR-0021 workload guest)."
        )
        # VLAN 100 carries TWO occupants: the stateful DB guest (svc01,
        # host_index 0) and the non-catalog workload guest (role_code workload,
        # host_index 1 — this guest). So the WORKLOAD guest's peer set is the DB
        # guest's index [0], and the proxmox-compute Guard 4
        # (`!contains(peer, self)`) ENGAGES — confirming the collision guard
        # engages on VLAN 100 from the workload guest's side. The DB sibling's
        # cross-check asserts the symmetric [1]; together the two guests see each
        # other as peers.
        assert hcl_peers == [_DB_HOST_INDEX], (
            "VLAN 100 has two occupants (svc01 DB at index 0 + the workload guest "
            f"at index 1), so the WORKLOAD guest's peer set must be [{_DB_HOST_INDEX}]; "
            f"got {hcl_peers}"
        )

    def test_hcl_resolved_vlan_and_index_from_committed_sources(self, input_guard):
        # The HCL plan must have RESOLVED vlan_id from the registry and host_index
        # from the ledger's role_code=workload row — proven by the guard anchor's
        # recorded inputs matching the committed-source expectations.
        assert input_guard.get("vlan_id") == _VLAN_ID, (
            f"HCL must resolve vlan_id={_VLAN_ID} from projects.yaml for slug {_SLUG!r}; "
            f"plan recorded {input_guard.get('vlan_id')}"
        )
        assert input_guard.get("host_index") == _HOST_INDEX, (
            f"HCL must read host_index={_HOST_INDEX} from host-indices.yaml for "
            f"role_code=workload on VLAN {_VLAN_ID} (ADR-0015/ADR-0021); plan recorded "
            f"{input_guard.get('host_index')}"
        )
