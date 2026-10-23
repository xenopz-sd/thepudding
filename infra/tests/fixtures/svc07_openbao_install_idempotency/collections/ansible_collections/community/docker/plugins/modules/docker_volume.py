#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Offline stub for ``community.docker.docker_volume``.

Task 5.5 fixture (spec: svc-07-secrets-manager; extended by the
ci-offline-test-pipeline CI-failure fix, design §3.7). The scoped fake
``community.docker`` collection masks the real one wholesale, so EVERY
``community.docker`` module the ``openbao_install`` role references must exist
here — and, for the ones the role actually INVOKES in the "already-configured"
scenario, must accept the role's real call signature and behave idempotently.

Unlike the gated-off MOCK-NET task, the named-volume pre-create block
(openbao_install/tasks/main.yml) runs even on the already-configured path (it is
NOT gated by the version-check fact — a fresh volume chown is cheap/idempotent).
So this module is really called: the role invokes it as
``docker_volume: {name: <project>_<vol>, state: present}`` in a loop. This stub
therefore declares exactly ``name`` + ``state`` and returns ``changed=False``
(the "volume already present, nothing to do" idempotent result) so the offline
priming run proceeds without a Docker daemon and without perturbing the
``changed=0`` idempotency assertion on the subsequent ``--check`` run.
"""

from __future__ import annotations

from ansible.module_utils.basic import AnsibleModule


def main() -> None:
    module = AnsibleModule(
        # Mirror the subset of the real module's args the role passes.
        argument_spec=dict(
            name=dict(type="str", required=True),
            state=dict(type="str", default="present"),
        ),
        supports_check_mode=True,
    )
    # Idempotent create-if-absent: report the volume as already present so no
    # state changes (changed=False keeps the downstream changed=0 assertion
    # honest).
    module.exit_json(changed=False, volume=dict(Name=module.params["name"]))


if __name__ == "__main__":
    main()
