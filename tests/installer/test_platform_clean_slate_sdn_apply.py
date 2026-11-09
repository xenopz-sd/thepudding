"""WS-C25 — the platform clean-slate tries a non-reboot SDN apply before it asks for a host reboot.

Observed live twice (WS-C19, WS-C20): after ``terraform destroy`` the Proxmox SDN config was empty, but the kernel
bridge ``p20`` stayed UP, so the clean-slate (correctly, no false-clean) halted and offered ONLY
``platform_clean_slate_reboot=true``. Running ``pvesh set /cluster/sdn`` on the host removed ``p20`` in seconds
without a reboot while ``vmbr0`` (the host's LAN link) stayed up. The clean-slate should do that itself, re-verify,
and keep the reboot as the fallback.

Pure offline: the task list is parsed and the real Jinja expressions are evaluated with Ansible's templater.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _ROOT / "ansible" / "roles" / "platform_clean_slate"


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")) if isinstance(t, dict)]


def _defaults() -> dict:
    return yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))


def _idx(fragment: str) -> int:
    for i, t in enumerate(_tasks()):
        if fragment in str(t.get("name")):
            return i
    raise AssertionError(f"no task named like {fragment!r}")


def _task(fragment: str) -> dict:
    return _tasks()[_idx(fragment)]


def _argv_text(t: dict) -> str:
    argv = (t.get("ansible.builtin.command") or {}).get("argv")
    return " ".join(str(a) for a in argv) if isinstance(argv, list) else str(argv)


def _when(t: dict) -> str:
    w = t.get("when")
    return " and ".join(str(x) for x in w) if isinstance(w, list) else str(w)


_PRESENT = "Compute whether the SDN zone/bridge is still present"
_APPLY = "Apply the (now empty) SDN config on the host"
_RECOMPUTE = "Re-compute whether the SDN zone/bridge is still present"
_REBOOT = "Reboot the Proxmox host to clear the stuck SDN zone"
_HALT = "Halt on a surviving SDN zone without the reboot opt-in"


def test_non_vacuity_existing_steps_are_found():
    for frag in (_PRESENT, _REBOOT, _HALT):
        _idx(frag)


def test_sdn_apply_runs_after_the_presence_probe_and_before_the_reboot_and_the_halt():
    """Bug condition: there is no non-reboot step at all, so the only recovery offered is a host reboot."""
    assert _idx(_PRESENT) < _idx(_APPLY) < _idx(_RECOMPUTE) < _idx(_REBOOT)
    assert _idx(_RECOMPUTE) < _idx(_HALT)


def test_sdn_apply_is_the_least_disruptive_pvesh_call_on_the_proxmox_host_inventory():
    t = _task(_APPLY)
    text = _argv_text(t)
    assert "pvesh set /cluster/sdn" in text
    assert "platform_clean_slate_proxmox_inventory" in text and "ansible_host=" in text
    assert "-b" in t["ansible.builtin.command"]["argv"], "pvesh needs root on the node, like the reboot step"
    for bad in ("reboot", "pct ", "terraform", "ifdown", "ip link del", "rm "):
        assert bad not in text, f"the apply step must not contain {bad!r}"


def test_sdn_apply_only_runs_when_the_bridge_survived_and_is_switchable():
    w = _when(_task(_APPLY))
    assert "platform_cs_sdn_present" in w
    assert "platform_clean_slate_sdn_apply" in w
    # default ON (a safer step than a reboot), but an explicit, documented opt-out exists
    assert _defaults()["platform_clean_slate_sdn_apply"] is True
    play = yaml.safe_load((_ROOT / "ansible" / "playbooks" / "platform-bootstrap.yml").read_text(encoding="utf-8"))
    assert play[0]["vars"]["platform_clean_slate_reboot"] is False, "the reboot stays an opt-in, default OFF"


def test_reboot_and_halt_judge_the_recomputed_fact_not_the_first_probe():
    """The recompute re-binds the SAME fact name the reboot and halt tasks already read."""
    rec = _task(_RECOMPUTE)
    assert "platform_cs_sdn_present" in rec["ansible.builtin.set_fact"]
    assert "platform_cs_sdn_present" in _when(_task(_REBOOT)) and "platform_cs_sdn_present" in _when(_task(_HALT))
    # and the recompute is gated on the apply having been attempted, so a clean host costs nothing
    assert "platform_clean_slate_sdn_apply" in _when(rec)


def test_reprobe_polls_for_the_bridge_to_disappear_rather_than_sleeping():
    """Runtime pattern P3: gate on a polled, real signal with a bounded retry, never a fixed sleep."""
    probes = [t for t in _tasks() if "Re-probe" in str(t.get("name"))]
    assert len(probes) == 2, "re-probe both signals (ip link and pvesh vnets)"
    link = next(t for t in probes if "ip link" in str(t["name"]))
    assert link.get("until") and "platform_clean_slate_sdn_apply_poll_retries" in str(link["retries"])
    assert _defaults()["platform_clean_slate_sdn_apply_poll_retries"] >= 3
    assert _defaults()["platform_clean_slate_sdn_apply_poll_delay"] >= 1
    assert "does not exist" in str(link["until"])
    assert not any("ansible.builtin.pause" in t for t in _tasks() if "SDN" in str(t.get("name"))), "no fixed sleep"


def _recompute_expr() -> str:
    return str(_task(_RECOMPUTE)["ansible.builtin.set_fact"]["platform_cs_sdn_present"])


def _render(link_out: str, pvesh_out: str) -> bool:
    variables = {"platform_clean_slate_sdn_vnet_id": "p20",
                 "platform_cs_iplink_out2": link_out, "platform_cs_pvesh_out2": pvesh_out}
    return bool(Templar(loader=DataLoader(), variables=variables).template(trust_as_template(_recompute_expr())))


@pytest.mark.parametrize("link_out,pvesh_out,expected", [
    ('shrimp | FAILED | rc=1 >>\nDevice "p20" does not exist.', "", False),                   # applied: gone
    ("5: p20: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue", "", True),              # still up
    ('Device "p20" does not exist.', "│ vnet │ p20 │ vlanzone │", True),                         # still in the SDN config
    ("shrimp | UNREACHABLE! => Failed to connect", "shrimp | UNREACHABLE! => Failed to connect", True),  # unknown => not clean
])
def test_recompute_never_reports_clean_on_a_survivor_or_an_unreachable_host(link_out, pvesh_out, expected):
    """No false-clean (Req 4.3): the same presence logic as the first probe, applied to the re-probe output."""
    assert _render(link_out, pvesh_out) is expected


@pytest.mark.parametrize("apply_on,expect", [(True, "already ran and the bridge STILL survived it"),
                                             (False, "was disabled")])
def test_halt_message_names_the_automatic_apply_and_keeps_the_reboot_remedy(apply_on, expect):
    halt = _task(_HALT)
    tpl = Templar(loader=DataLoader(), variables={
        "platform_clean_slate_sdn_apply": apply_on, "platform_clean_slate_sdn_vnet_id": "p20"})
    note = tpl.template(trust_as_template(halt["vars"]["platform_cs_apply_note"]))
    tpl2 = Templar(loader=DataLoader(), variables={
        "platform_clean_slate_sdn_apply": apply_on, "platform_clean_slate_sdn_vnet_id": "p20",
        "platform_cs_apply_note": note})
    msg = " ".join(str(tpl2.template(trust_as_template(halt["ansible.builtin.fail"]["msg"]))).split())
    assert expect in msg, "the operator must learn whether the lighter step ran"
    assert "platform_clean_slate_reboot=true" in msg, "the reboot remedy is kept as the fallback"
    assert "NOT clean and will NOT be reported clean" in msg, "still no false-clean"


def test_apply_step_leaves_no_service_state_untouched_by_construction():
    """The new step sits after the fabric destroy and cannot reach a service root (boundary invariant, Property 6)."""
    assert _idx(_APPLY) > _idx("terraform destroy (platform-foundation SDN fabric, scoped)")
    for t in _tasks():
        if "SDN apply" in str(t.get("name")) or "SDN config" in str(t.get("name")):
            assert "infra/projects" not in _argv_text(t)
