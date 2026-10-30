"""Golden HCL-vs-Python cross-check for the project-VLAN SVC01_Consumer_Root.

Feature: project-consumer-onboarding (tasks.md Task 6.4; design.md "Testing
Strategy" → "Shared-symbol caveat" / ADR-0015 drift mitigation; Requirements
3.4, 3.7).

=== WHY THIS EXISTS (extends the ADR-0015 golden cross-check to a project VLAN) ===

ADR-0015 records a standing caveat: the per-project host IP / hostname is derived
TWICE — once in HCL at apply time (``proxmox-compute`` via
``cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)``) and once in the pure Python
derivation layer (``netfoundation.derivation`` / this feature's
``consumer_derivation``). A golden ``terraform console`` / ``terraform plan``
cross-check must diff the HCL side against the Python side so the two
implementations can never silently drift.

That cross-check already exists for VLAN-20 rows (``test_obs_offline_plan.py`` —
SVC-17/18 at indices 8/9/10). This module EXTENDS it to the FIRST **project VLAN**
(``dronefleet`` -> VLAN 100), the new path this feature introduces:

  * **HCL side** — an offline ``terraform plan -json`` of the new consumer DB root
    ``infra/projects/dronefleet/db`` (the real HCL: the ``db/`` wrapper resolves
    vlan_id from the registry, reads host_index from the ledger, and threads both
    into the reused SVC-01 root -> ``proxmox-compute``). The plan's computed
    ``initialization.ip_config.ipv4.address`` + ``hostname`` ARE the HCL side.
  * **Python side** — ``consumer_derivation.consumer_db_host_ip(100, 0)`` (the
    thin reuse wrapper over ``netfoundation.derivation.host_ip``) plus
    ``netfoundation.peer_host_indices`` over the committed ledger for the
    ``peer_host_indices`` VERDICT (a single-guest VLAN 100 -> empty peer set ->
    Guard 4 no-op).

A drift between the ledger, the HCL ``cidrhost`` formula, and the Python
derivation fails HERE — on a project VLAN, not just VLAN 20.

GATED on a terraform/tofu binary AND an installable provider; SKIPS cleanly
(never fails) when neither is available offline — same contract as the SVC-09 /
SVC-17-18 offline-plan siblings. NEVER runs a live apply (no Proxmox contact: the
root declares only SDN-attached resources with no read-time data sources, planned
with dummy non-contacting vars and ``-refresh=false``).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_consumer_db_offline_plan.py -v
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
# cross-check.
# --------------------------------------------------------------------------- #
_NETFOUNDATION_SCRIPTS_DIR = _INFRA_DIR / "platform-foundation" / "scripts"
if str(_NETFOUNDATION_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_NETFOUNDATION_SCRIPTS_DIR))

_INSTALLER_SCRIPTS_DIR = _INFRA_DIR.parent / "scripts" / "installer"
if str(_INSTALLER_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALLER_SCRIPTS_DIR))

from netfoundation import peer_host_indices  # noqa: E402  (after sys.path setup)
from consumer_derivation import consumer_db_host_ip  # noqa: E402

# --------------------------------------------------------------------------- #
# Fixtures of the golden case: the registered test slug `dronefleet` on VLAN 100,
# the sole (single-guest) occupant at host_index 0 -> 10.0.100.10.
# --------------------------------------------------------------------------- #
_CONTAINER_TYPE = "proxmox_virtual_environment_container"
_SLUG = "dronefleet"
_VLAN_ID = 100
_HOST_INDEX = 0
_CONSUMER_ROOT_REL = Path("projects") / _SLUG / "db"

#: Ledger + registry, read to prove the HCL plan resolves from the SAME committed
#: sources the Python side reads (not hard-coded fixtures).
_HOST_INDICES_YAML = _INFRA_DIR / "platform-foundation" / "host-indices.yaml"
_PROJECTS_YAML = _INFRA_DIR / "platform-foundation" / "projects.yaml"


def _load_read_view_ledger() -> dict:
    """Load host-indices.yaml into the READ-VIEW shape the shared
    ``netfoundation.peer_host_indices`` consumes.

    The committed ledger keys each entry by ``svc_code`` (+ optional
    ``component`` / ``instance``); the thin reader layer derives a stable
    ``guest_key`` from ``svc_code[-component]`` (mirroring
    ``_vlan20_read_view_ledger`` in the derivation-layer tests). This keeps the
    Python side a genuine REUSE of the shared peer-set symbol rather than a
    forked re-implementation.
    """
    raw = yaml.safe_load(_HOST_INDICES_YAML.read_text(encoding="utf-8"))
    view: dict[int, list[dict]] = {}
    for vlan_key, entries in raw.items():
        rows = []
        for e in entries:
            guest_key = e["svc_code"]
            if e.get("component"):
                guest_key = f"{e['svc_code']}-{e['component']}"
            rows.append({"guest_key": guest_key, "host_index": e["host_index"]})
        view[int(vlan_key)] = rows
    return view


# --- Python side of the cross-check (the "golden" expected values) -------- #

def _python_expected_ip() -> str:
    """Python mirror of HCL ``cidrhost("10.0.100.0/24", 10 + 0)`` via the shared
    reuse wrapper — the Python side of the host-IP cross-check."""
    return consumer_db_host_ip(_VLAN_ID, _HOST_INDEX)  # "10.0.100.10"


def _python_expected_hostname() -> str:
    """Python mirror of the module hostname ``svc01-<vlan>-01`` (NET-00 §4)."""
    return f"svc01-{_VLAN_ID}-01"


def _python_expected_peer_set() -> list[int]:
    """Python side of the peer-set VERDICT: every OTHER host index on VLAN 100,
    excluding this svc01 guest's own. On a single-guest VLAN this is empty, so the
    proxmox-compute Guard 4 (``!contains(peer, self)``) is a strict no-op."""
    ledger = _load_read_view_ledger()
    # The single-SVC-01 project-VLAN guest's read-view key is its svc_code.
    return peer_host_indices(ledger, _VLAN_ID, "svc01")


# --------------------------------------------------------------------------- #
# Offline-plan sandbox helper for the consumer DB root (mirrors the SVC-17/18
# sibling). Copies infra/ into a temp dir, drops a transient `backend "local" {}`
# override into the sandboxed db/ root, init -reconfigure + plan with dummy
# non-contacting vars, show -json. Skips cleanly on an offline provider-install
# failure.
# --------------------------------------------------------------------------- #
_DUMMY_ENDPOINT = "https://dummy.invalid:8006/"
_DUMMY_TOKEN = "terraform@pve!ci=00000000-0000-0000-0000-000000000000"
_DUMMY_TEMPLATE = "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
_DUMMY_VARS = (
    "-var", f"proxmox_endpoint={_DUMMY_ENDPOINT}",
    "-var", f"proxmox_api_token={_DUMMY_TOKEN}",
    "-var", "proxmox_node_name=pve-test",
    "-var", "svc01_vmid=3100",
    "-var", f"svc01_template_file_id={_DUMMY_TEMPLATE}",
)
_OVERRIDE_FILENAME = "zz_consumer_db_offline_backend_override.tf"
_OVERRIDE_CONTENT = (
    "# Transient, test-scoped local backend override written into a SANDBOX\n"
    "# COPY only by test_consumer_db_offline_plan.py. Overrides the db/ root's\n"
    "# partial `backend \"http\" {}` so an offline `terraform init -reconfigure`\n"
    "# + `plan` succeed without a live GitLab HTTP backend / CI_JOB_TOKEN. NEVER\n"
    "# committed into a real root (PF FR-2).\n"
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
def _consumer_db_offline_plan() -> Iterator[list[dict]]:
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"
    sandbox = Path(tempfile.mkdtemp(prefix="consumer_db_offline_plan_"))
    try:
        # Copy the whole infra/ tree so the db/ root's relative reads
        # (../../../platform-foundation/{projects,host-indices}.yaml and the
        # ../../svc-01-sql-database + ../../../modules/proxmox-compute module
        # sources) all resolve inside the sandbox.
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / _CONSUMER_ROOT_REL
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
        plan_path = target / "consumer_db.tfplan"
        plan = subprocess.run(
            [
                binary, "plan", "-input=false", "-no-color", "-refresh=false",
                *_DUMMY_VARS,
                f"-out={plan_path}",
            ],
            cwd=target, capture_output=True, text=True,
        )
        assert plan.returncode == 0, (
            "offline `terraform plan` of the dronefleet/db consumer root must succeed with "
            f"dummy vars; got exit {plan.returncode}:\n{plan.stdout}\n{plan.stderr}"
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
# Golden HCL-vs-Python cross-check on the project VLAN (gated).
# --------------------------------------------------------------------------- #
@requires_terraform
class TestConsumerDbProjectVlanCrossCheck:
    @staticmethod
    @pytest.fixture(scope="class")
    def db_container() -> dict:
        with _consumer_db_offline_plan() as changes:
            containers = [
                c["change"]["after"]
                for c in changes
                if c["type"] == _CONTAINER_TYPE
            ]
            assert len(containers) == 1, (
                f"the consumer db/ root must plan exactly one {_CONTAINER_TYPE} "
                f"(the per-project DB LXC); planned {len(containers)}"
            )
            return containers[0]

    @staticmethod
    @pytest.fixture(scope="class")
    def input_guard() -> dict:
        """The proxmox-compute Guard-4 anchor's recorded input (carries the
        HCL-resolved vlan_id/host_index/peer_host_indices verdict)."""
        with _consumer_db_offline_plan() as changes:
            guards = [
                c["change"]["after"]["input"]
                for c in changes
                if c["type"] == "terraform_data"
                and isinstance(c["change"]["after"].get("input"), dict)
                and c["change"]["after"]["input"].get("hostname")
            ]
            assert guards, "expected the proxmox-compute terraform_data.input_guards anchor in the plan"
            return guards[0]

    def test_hcl_ip_matches_python_derivation(self, db_container):
        # HCL side: the plan's computed initialization.ip_config.ipv4.address.
        init = _first(db_container.get("initialization"))
        ipv4 = _first(_first(init.get("ip_config")).get("ipv4") if init.get("ip_config") else None)
        hcl_addr = ipv4.get("address")
        # Python side: the shared reuse wrapper, CIDR-suffixed to match HCL.
        expected = f"{_python_expected_ip()}/24"
        assert hcl_addr == expected, (
            f"HCL-computed consumer DB IP {hcl_addr!r} on VLAN {_VLAN_ID} must equal the "
            f"Python derivation {expected!r} (cidrhost 10.0.{_VLAN_ID}.0/24, 10+{_HOST_INDEX} "
            "— NET-00 §3). A mismatch is HCL-vs-Python derivation drift (ADR-0015)."
        )

    def test_hcl_hostname_matches_python_derivation(self, db_container):
        init = _first(db_container.get("initialization"))
        hcl_hostname = init.get("hostname")
        assert hcl_hostname == _python_expected_hostname(), (
            f"HCL-computed hostname {hcl_hostname!r} must equal the Python-derived "
            f"{_python_expected_hostname()!r} (svc_code=svc01 on VLAN {_VLAN_ID}, NET-00 §4)."
        )

    def test_hcl_tag_is_project_postgres(self, db_container):
        assert db_container.get("tags") == [f"proj-{_SLUG}-postgres"], (
            f"consumer DB tag must be ['proj-{_SLUG}-postgres'] (PF §4, Req 3.6); "
            f"got {db_container.get('tags')}"
        )

    def test_hcl_peer_set_verdict_matches_python(self, input_guard):
        # HCL side: the peer_host_indices the db/ root threaded (via the reused
        # SVC-01 module's ledger derivation) into proxmox-compute's Guard 4.
        hcl_peers = sorted(input_guard.get("peer_host_indices", []))
        # Python side: the ledger-derived peer set for this guest.
        python_peers = sorted(_python_expected_peer_set())
        assert hcl_peers == python_peers, (
            f"HCL peer_host_indices verdict {hcl_peers} must equal the Python "
            f"ledger-derived peer set {python_peers} for svc01 on VLAN {_VLAN_ID}. "
            "On this single-guest project VLAN both must be empty (Guard 4 no-op)."
        )
        # And on a single-guest VLAN 100 that verdict is specifically empty.
        assert hcl_peers == [], (
            "single-guest project VLAN 100 must derive an EMPTY peer set so the "
            f"proxmox-compute Guard 4 is a strict no-op; got {hcl_peers}"
        )

    def test_hcl_resolved_vlan_and_index_from_committed_sources(self, input_guard):
        # The HCL plan must have RESOLVED vlan_id from the registry and host_index
        # from the ledger — proven by the guard anchor's recorded inputs matching
        # the committed-source expectations (not a free operator input).
        assert input_guard.get("vlan_id") == _VLAN_ID, (
            f"HCL must resolve vlan_id={_VLAN_ID} from projects.yaml for slug {_SLUG!r} "
            f"(Req 2.1); plan recorded {input_guard.get('vlan_id')}"
        )
        assert input_guard.get("host_index") == _HOST_INDEX, (
            f"HCL must read host_index={_HOST_INDEX} from host-indices.yaml for svc01 on "
            f"VLAN {_VLAN_ID} (Req 3.8); plan recorded {input_guard.get('host_index')}"
        )
