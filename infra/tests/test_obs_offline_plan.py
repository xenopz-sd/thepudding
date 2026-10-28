"""Offline ``terraform plan -json`` shape assertions for the SVC-17/18 root.

Slice task 2.4 (spec: svc-17-18-observability) — requirements FR-16 / AC-2 / AC-11
and design "Testing Strategy" (``test_obs_offline_plan.py`` — computed hostnames
``svc17-prometheus-20-01`` / ``svc17-grafana-20-01`` / ``svc18-loki-20-01`` and
IPs ``10.0.20.18`` / ``.19`` / ``.20``; the golden ``terraform console`` /
HCL-vs-Python cross-check for the three new Host_Index_Authority ledger rows,
ADR-0015 drift mitigation).

Structured EXACTLY like the sibling ``test_svc09_terraform_shape.py`` offline-plan
layer: a module-local ``_obs_offline_plan`` sandbox helper (copy ``infra/`` into a
temp dir, drop a transient ``backend "local" {}`` override into the sandboxed
SVC-17/18 root, ``init -reconfigure`` + ``plan`` with dummy non-contacting vars,
``show -json``) and the same skip-on-provider-install-failure logic. Extended from
one Traefik LXC to THREE observability guests.

**ADR-0015 drift mitigation (folded into the plan-json assertions).** The design
calls for a golden cross-check that the HCL-computed addressing for the three new
ledger rows (prometheus=8 / grafana=9 / loki=10 on VLAN 20) matches the Python
derivation of ``cidrhost("10.0.20.0/24", 10 + host_index)``. Rather than shell out
to ``terraform console`` separately, this module computes the expected IP/hostname
in Python from the SAME formula and asserts the offline PLAN (i.e. the real HCL
``proxmox-compute`` output) resolves to exactly those values — the plan IS the HCL
side of the cross-check, so a drift between the ledger, the HCL formula, and the
Python derivation fails here.

This layer is GATED on a terraform/tofu binary AND an installable provider; it
SKIPS cleanly (never fails) when neither is available offline — same contract as
the SVC-09 sibling.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_obs_offline_plan.py -v
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from conftest import requires_terraform, _terraform_binary

_INFRA_DIR = Path(__file__).resolve().parent.parent

_CONTAINER_TYPE = "proxmox_virtual_environment_container"

#: The three Host_Index_Authority rows this root flips to `allocated` (ADR-0015),
#: with the per-component naming inputs. host_index -> computed IP/hostname is
#: derived below in Python from the SAME cidrhost() formula the HCL uses, which
#: is the golden HCL-vs-Python cross-check.
_LEDGER_ROWS = {
    "prometheus": {"svc_code": "svc17", "component": "prometheus", "host_index": 8, "tag": "proj-shared-prometheus"},
    "grafana": {"svc_code": "svc17", "component": "grafana", "host_index": 9, "tag": "proj-shared-grafana"},
    "loki": {"svc_code": "svc18", "component": "loki", "host_index": 10, "tag": "proj-shared-loki"},
}

_VLAN_ID = 20
_SUBNET = ipaddress.ip_network(f"10.0.{_VLAN_ID}.0/24")

_EXPECTED_SIZES = {
    "prometheus": {"cores": 3, "memory": 4608, "disk": 100},
    "grafana": {"cores": 1, "memory": 1024, "disk": 10},
    "loki": {"cores": 2, "memory": 4096, "disk": 200},
}


def _python_derived_ip(host_index: int) -> str:
    """Python mirror of HCL ``cidrhost("10.0.20.0/24", 10 + host_index)`` (NET-00 §3)."""
    return str(_SUBNET[10 + host_index])


def _python_derived_hostname(svc_code: str, component: str) -> str:
    """Python mirror of the module hostname ``<svc-code>-<component>-<vlan>-01`` (NET-00 §4)."""
    return f"{svc_code}-{component}-{_VLAN_ID}-01"


#: The golden expected (hostname, ip, tag) per guest, computed from the ledger
#: rows via the Python derivation — the "Python side" of the ADR-0015 cross-check.
_EXPECTED = {
    name: {
        "hostname": _python_derived_hostname(row["svc_code"], row["component"]),
        "ipv4": _python_derived_ip(row["host_index"]),
        "tag": row["tag"],
    }
    for name, row in _LEDGER_ROWS.items()
}


# --------------------------------------------------------------------------- #
# Offline-plan sandbox helper for the SVC-17/18 root.
# --------------------------------------------------------------------------- #
_DUMMY_ENDPOINT = "https://dummy.invalid:8006/"
_DUMMY_TOKEN = "terraform@pve!ci=00000000-0000-0000-0000-000000000000"
_DUMMY_TEMPLATE = "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
_DUMMY_GUEST_VARS = (
    "-var", "proxmox_node_name=pve-test",
    "-var", "platform_domain=example.test",
    "-var", f"prometheus_template_file_id={_DUMMY_TEMPLATE}",
    "-var", f"grafana_template_file_id={_DUMMY_TEMPLATE}",
    "-var", f"loki_template_file_id={_DUMMY_TEMPLATE}",
    "-var", "prometheus_vmid=2170",
    "-var", "grafana_vmid=2171",
    "-var", "loki_vmid=2180",
)
_OVERRIDE_FILENAME = "zz_obs_offline_backend_override.tf"
_OVERRIDE_CONTENT = (
    "# Transient, test-scoped local backend override written into a SANDBOX\n"
    "# COPY only by test_obs_offline_plan.py. Overrides the root's partial\n"
    "# `backend \"http\" {}` so an offline `terraform init -reconfigure` +\n"
    "# `plan` succeed without a live GitLab HTTP backend / CI_JOB_TOKEN. NEVER\n"
    "# committed into a real root (PF FR-2).\n"
    "terraform {\n"
    "  backend \"local\" {}\n"
    "}\n"
)


@contextmanager
def _obs_offline_plan() -> Iterator[list[dict]]:
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"
    sandbox = Path(tempfile.mkdtemp(prefix="obs_offline_plan_"))
    try:
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / "projects" / "svc-17-18-observability"
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
            env_markers = (
                "failed to install provider",
                "could not retrieve the list of available versions",
                "failed to query available provider packages",
                "could not connect", "network is unreachable", "dial tcp",
            )
            if any(m in combined for m in env_markers):
                pytest.skip(
                    "requires-infra, skipped: `terraform init` could not install/reuse "
                    "the provider offline — an environmental prerequisite unrelated to the "
                    f"shape under test.\ninit stderr tail:\n{init.stderr[-1200:]}"
                )
            pytest.fail(f"terraform init failed unexpectedly:\n{init.stdout}\n{init.stderr}")
        plan_path = target / "obs.tfplan"
        plan = subprocess.run(
            [
                binary, "plan", "-input=false", "-no-color", "-refresh=false",
                "-var", f"proxmox_endpoint={_DUMMY_ENDPOINT}",
                "-var", f"proxmox_api_token={_DUMMY_TOKEN}",
                *_DUMMY_GUEST_VARS,
                f"-out={plan_path}",
            ],
            cwd=target, capture_output=True, text=True,
        )
        assert plan.returncode == 0, (
            "offline `terraform plan` of the SVC-17/18 root must succeed with dummy vars; "
            f"got exit {plan.returncode}:\n{plan.stdout}\n{plan.stderr}"
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
    if not seq:
        return {} if seq == [] else None
    if isinstance(seq, list):
        return seq[0] if seq else {}
    return seq


# --------------------------------------------------------------------------- #
# terraform plan-json shape (gated).
# --------------------------------------------------------------------------- #
@requires_terraform
class TestObsPlanJsonShape:
    @staticmethod
    @pytest.fixture(scope="class")
    def containers() -> dict[str, dict]:
        with _obs_offline_plan() as changes:
            out: dict[str, dict] = {}
            for c in changes:
                if c["type"] == _CONTAINER_TYPE:
                    out[c["name"]] = c["change"]["after"]
            return out

    @staticmethod
    @pytest.fixture(scope="class")
    def all_types() -> list[str]:
        with _obs_offline_plan() as changes:
            return [c["type"] for c in changes]

    def test_exactly_three_containers_planned(self, all_types):
        assert all_types.count(_CONTAINER_TYPE) == 3, (
            f"plan must create exactly three {_CONTAINER_TYPE}; types planned: {sorted(set(all_types))}"
        )

    def test_three_named_containers_present(self, containers):
        assert set(containers) == {"prometheus", "grafana", "loki"}, (
            f"expected planned containers prometheus/grafana/loki; got {sorted(containers)}"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_computed_ip_hostname_tag(self, containers, name):
        # This is the golden ADR-0015 HCL-vs-Python cross-check: the HCL-computed
        # hostname/IP (plan output) must equal the Python-derived expected value.
        after = containers[name]
        exp = _EXPECTED[name]
        init = _first(after.get("initialization"))
        assert init is not None, f"{name}: no initialization block in plan"
        assert init.get("hostname") == exp["hostname"], (
            f"{name} hostname must be {exp['hostname']} (NET-00 §4); got {init.get('hostname')}"
        )
        ipv4 = _first(_first(init.get("ip_config")).get("ipv4") if init.get("ip_config") else None)
        assert ipv4 is not None, f"{name}: no ip_config.ipv4 in plan"
        assert ipv4.get("address") == f"{exp['ipv4']}/24", (
            f"{name} computed IP must be {exp['ipv4']}/24 "
            f"(cidrhost 10.0.20.0/24, 10+host_index — NET-00 §3); got {ipv4.get('address')}"
        )
        assert after.get("tags") == [exp["tag"]], (
            f"{name} tag must be [{exp['tag']!r}]; got {after.get('tags')}"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_computed_sizes(self, containers, name):
        after = containers[name]
        exp = _EXPECTED_SIZES[name]
        cpu = _first(after.get("cpu"))
        mem = _first(after.get("memory"))
        disk = _first(after.get("disk"))
        assert cpu and cpu.get("cores") == exp["cores"], (
            f"{name} cores must be {exp['cores']}; got {cpu}"
        )
        assert mem and mem.get("dedicated") == exp["memory"], (
            f"{name} memory.dedicated must be {exp['memory']}; got {mem}"
        )
        assert disk and disk.get("size") == exp["disk"], (
            f"{name} disk.size must be {exp['disk']}; got {disk}"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_unprivileged(self, containers, name):
        after = containers[name]
        assert after.get("unprivileged") is True, (
            f"{name} must be unprivileged; got {after.get('unprivileged')}"
        )
