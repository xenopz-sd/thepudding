#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Offline stub for ``community.docker.docker_container_info``.

Task 5.5 (spec: svc-07-secrets-manager) fixture — how the "already-configured"
state is SIMULATED for the ``openbao_install`` check-mode idempotency test.

The real ``openbao_install`` role probes the running container with
``community.docker.docker_container_info`` (``check_mode: false`` so it runs even
under ``--check``) and, from that probe, computes the fact
``openbao_already_at_pinned_version`` — True only when the container EXISTS, is
RUNNING, and its image EQUALS the pinned ``openbao_image``. When that fact is
True, every install/bring-up task is gated off (``when: not
openbao_already_at_pinned_version``) and the play reports ``changed=0``.

To exercise that skip path deterministically and OFFLINE — with no Docker
daemon, no real OpenBao container, and no ``requires_infra`` dependency — this
stub SHADOWS the real module on the playbook's ``library/`` search path and
returns a canned probe result describing the pinned image already running. It is
resolved by Ansible ONLY because the fixture playbook sets its module search
path to this directory; it never shadows the real collection module in any
production run.

Crucially, the stub honours the role's REAL call signature: the role invokes the
module with only ``name`` (see openbao_install/tasks/main.yml), so the stub
declares only ``name`` as an argument. The pinned image the stub reports as the
running container's image is sourced from the ``OPENBAO_STUB_PINNED_IMAGE``
environment variable, which the pytest runner sets to the role's pinned
``openbao_image`` (``openbao/openbao:2.4``). Reporting exactly that image is what
drives the role's ``set_fact`` to compute ``openbao_already_at_pinned_version =
True`` and therefore skip the whole bring-up — the "already-configured" state.

The returned shape matches the exact keys the role's ``set_fact`` reads:
``exists`` (bool), ``container.State.Running`` (bool), and
``container.Config.Image`` (str).

This stub always reports ``changed=False`` (a read-only probe never changes
state), so it does not itself perturb the ``changed=0`` assertion.
"""

from __future__ import annotations

import os

from ansible.module_utils.basic import AnsibleModule

#: Env var the pytest runner sets to the role's pinned image so the stub reports
#: the pinned version as already running. Falls back to the well-known pin so a
#: manual ``ansible-playbook`` invocation of the fixture still simulates the
#: already-configured state.
_PINNED_IMAGE_ENV = "OPENBAO_STUB_PINNED_IMAGE"
_DEFAULT_PINNED_IMAGE = "openbao/openbao:2.4"


def main() -> None:
    module = AnsibleModule(
        # The role calls this module with only ``name`` — mirror that exactly so
        # the stub is a drop-in for the real probe under the fixture search path.
        argument_spec=dict(
            name=dict(type="str", required=True),
        ),
        supports_check_mode=True,
    )

    running_image = os.environ.get(_PINNED_IMAGE_ENV, _DEFAULT_PINNED_IMAGE)
    container_name = module.params["name"]

    # Canned "pinned image already running" probe result. Keys mirror the real
    # community.docker.docker_container_info return shape for the subset the
    # openbao_install set_fact inspects.
    result = dict(
        changed=False,
        exists=True,
        container=dict(
            Name=container_name,
            State=dict(Running=True, Status="running"),
            Config=dict(Image=running_image),
        ),
    )
    module.exit_json(**result)


if __name__ == "__main__":
    main()
