#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Offline stub for ``community.docker.docker_volume_info``.

Task 5.5 fixture (spec: svc-07-secrets-manager; extended by the
ci-offline-test-pipeline CI-failure fix, design §3.7). The scoped fake
``community.docker`` collection masks the real one wholesale, so EVERY
``community.docker`` module the ``openbao_install`` role references must exist
here — and, for the ones the role actually INVOKES, must return a shape the
role's downstream tasks consume AND behave so the role's idempotency assertion
(a ``--check`` re-run reports ``changed=0``, Req 13.1/13.2) holds offline.

The named-volume blocks (openbao_install tasks/main.yml) run even on the
already-configured path. The role inspects each volume with
``docker_volume_info: {name: <project>_<vol>}`` and then CHOWNS
``item.volume.Mountpoint`` to the openbao container uid/gid with
``ansible.builtin.file`` (recurse). There are TWO such chown tasks:

  * a PRE-up chown that is UNCONDITIONAL (`recurse: true`, no `when`), and
  * a POST-up chown gated `when: stat.uid != <uid> or stat.gid != <gid>`.

For BOTH to report ``changed=0`` on the ``--check`` re-run, the Mountpoint this
stub returns must be:

  1. DETERMINISTIC per volume name — the SAME path on every invocation (the
     pre-up inspect, the post-up inspect, and the re-run inspect all see one
     dir), and
  2. ALREADY OWNED by the openbao uid/gid — so the unconditional `file` chown
     predicts no change and the conditional one's `when` is false.

An earlier version returned a FRESH ``tempfile.mkdtemp()`` on every call, which
broke BOTH invariants: a new root-owned dir each time → both chowns reported
``changed`` → the idempotency assertion saw ``changed=2``. This version instead
derives a stable path from the volume name under a fixed stub root, creates it
once, and chowns it to the openbao uid/gid so the chowns are genuine no-ops.

The uid/gid default to the role's ``openbao_container_uid``/``_gid`` defaults
(100/1000) and are overridable via ``OPENBAO_STUB_VOL_UID`` / ``_GID`` for
robustness if those defaults ever change (same env-driven-stub pattern as
``OPENBAO_STUB_PINNED_IMAGE``). The chown runs as root in CI (the test is
``@requires_become``); if it cannot chown (unprivileged), it is swallowed — on
such a box the test SKIPS before reaching here anyway.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule

#: Fixed root for the stub's deterministic per-volume mountpoints. A constant
#: (NOT mkdtemp) so the same volume name resolves to the same path across every
#: invocation within and across playbook runs — the determinism invariant above.
_STUB_VOL_ROOT = Path(os.environ.get("OPENBAO_STUB_VOL_ROOT", "/tmp/openbao-stub-vols"))


def _target_owner() -> tuple[int, int]:
    """The openbao container uid/gid the role chowns volume mountpoints to.

    Defaults mirror the role's ``openbao_container_uid``/``_gid`` (100/1000);
    overridable via env so a future default change does not silently reintroduce
    the changed!=0 drift this stub exists to prevent.
    """
    uid = int(os.environ.get("OPENBAO_STUB_VOL_UID", "100"))
    gid = int(os.environ.get("OPENBAO_STUB_VOL_GID", "1000"))
    return uid, gid


def main() -> None:
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type="str", required=True),
        ),
        supports_check_mode=True,
    )
    name = module.params["name"]

    # Deterministic per-volume path. The volume name is used directly; a short
    # hash suffix keeps the dir name filesystem-safe without collisions.
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
    mountpoint = _STUB_VOL_ROOT / f"{name}-{digest}"

    # Create the stable mountpoint and chown it to the openbao uid/gid so the
    # role's (unconditional pre-up AND conditional post-up) `file` chowns both
    # find it already correct → changed=0 on the --check re-run. Best-effort:
    # on an unprivileged box the chown is swallowed (the test skips before here).
    uid, gid = _target_owner()
    try:
        mountpoint.mkdir(parents=True, exist_ok=True)
        os.chown(mountpoint, uid, gid)
    except (OSError, PermissionError):
        pass

    module.exit_json(
        changed=False,
        exists=True,
        volume=dict(Name=name, Mountpoint=str(mountpoint)),
    )


if __name__ == "__main__":
    main()
