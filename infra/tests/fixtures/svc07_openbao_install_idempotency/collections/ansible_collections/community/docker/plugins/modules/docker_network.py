#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Offline stub for ``community.docker.docker_network``.

Task 5.5 fixture (spec: svc-07-secrets-manager; extended by the
ci-offline-test-pipeline CI-failure fix, design §3.7). The scoped fake
``community.docker`` collection masks the real one wholesale, so EVERY
``community.docker`` module the ``openbao_install`` role references must exist
here or Ansible fails at PARSE time (FQCN resolution precedes ``when:``).

The role's ``MOCK-NET`` task references ``community.docker.docker_network`` (the
opt-in Traefik ``edge`` placeholder, ``openbao_mock_edge_network``, default OFF).
In the "already-configured" idempotency scenario that task is GATED OFF, so this
module is not invoked there today. But to stay robust if the gate ever changes,
this stub accepts the role's real call signature (``name`` + ``state``) and
returns ``changed=False`` (idempotent create-if-absent) rather than failing — the
same functional-stub approach as the sibling ``docker_volume`` stub, so a future
caller does not hit a spurious parameter error.
"""

from __future__ import annotations

from ansible.module_utils.basic import AnsibleModule


def main() -> None:
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type="str", required=True),
            state=dict(type="str", default="present"),
        ),
        supports_check_mode=True,
    )
    module.exit_json(changed=False, network=dict(Name=module.params["name"]))


if __name__ == "__main__":
    main()
