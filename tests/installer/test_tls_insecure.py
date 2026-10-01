"""Offline guard for the TLS-verification half of fix-cluster-env-endpoint-path.

Feature: fix-cluster-env-endpoint-path, TLS half — the Terraform provider's TLS
verification must be DERIVED from the same operator toggle Phase-1 Preflight
uses, so preflight (lenient) and Terraform (formerly strict) can no longer drift.

Root cause (same class as the endpoint-path defect): a bpg/proxmox provider
CONNECTION setting was not derived from the cluster config preflight already
honors. Preflight tolerated the homelab self-signed cert via the play var
``svc07_proxmox_validate_certs`` (used as each uri task's ``validate_certs``),
but the installer never passed ``TF_VAR_proxmox_insecure`` to Terraform, so
``terraform apply`` failed at ``tls: failed to verify certificate: x509:
certificate signed by unknown authority``.

The fix wires ``TF_VAR_proxmox_insecure`` into ``svc07_tf_env`` in
``ansible/playbooks/svc-07-bootstrap.yml``, derived from the SAME toggle via the
Jinja expression::

    TF_VAR_proxmox_insecure: "{{ (not (svc07_proxmox_validate_certs | bool)) | lower }}"

so ``insecure == NOT validate_certs``, emitted as the Terraform-accepted bool
strings ``"true"``/``"false"``.

Because the derivation lives only in Jinja (no Python helper — Option A), this
offline guard has two independent halves:

1. A faithful reproduction of the bool -> string rule, asserting
   validate_certs True -> "false" and False -> "true" (and idempotent under the
   accepted string/bool inputs Ansible's ``| bool`` coerces).
2. A grep-style structural assertion that the playbook's ``svc07_tf_env`` really
   carries a ``TF_VAR_proxmox_insecure`` entry DERIVED from
   ``svc07_proxmox_validate_certs`` — so the wiring can't silently disappear.

The AUTHORITATIVE check remains the live Phase-2 ``terraform apply`` now getting
past the x509 cert error; this offline guard pins the derivation rule and its
presence in the playbook.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/installer/test_tls_insecure.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
# svc07_tf_env (which carries TF_VAR_proxmox_insecure) moved out of the
# ~1900-line svc-07-bootstrap.yml when it was thinned to a ~150-line orchestrator
# (svc07-installer-simplification, Task 4.2/11): the Phase-2 tf_env is now a
# default of the svc07_provision role. The structural guard below reads the
# provision-role defaults; its INTENT (insecure derived from
# svc07_proxmox_validate_certs) is unchanged.
_PROVISION_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_provision" / "defaults" / "main.yml"
)


def _derive_insecure(validate_certs: bool) -> str:
    """Faithful reproduction of the playbook's Jinja derivation rule.

    Mirrors ``(not (svc07_proxmox_validate_certs | bool)) | lower`` — the boolean
    is negated and rendered as the lowercase Terraform-accepted bool string
    ``"true"``/``"false"``. Kept in sync with the Jinja in
    ``ansible/playbooks/svc-07-bootstrap.yml``'s ``svc07_tf_env``.
    """
    return str(not validate_certs).lower()


@pytest.mark.parametrize(
    ("validate_certs", "expected_insecure"),
    [
        # validate_certs OFF (homelab self-signed default) -> provider insecure ON.
        (False, "true"),
        # validate_certs ON (trusted cert) -> provider must verify (insecure OFF).
        (True, "false"),
    ],
)
def test_insecure_is_negation_of_validate_certs(validate_certs, expected_insecure):
    """Feature: fix-cluster-env-endpoint-path, TLS half.

    ``TF_VAR_proxmox_insecure`` is the lowercased-string negation of
    ``svc07_proxmox_validate_certs`` — the single toggle governs both preflight
    and the Terraform provider, so they cannot drift.
    """
    assert _derive_insecure(validate_certs) == expected_insecure


def test_derived_strings_are_terraform_bool_literals():
    """Feature: fix-cluster-env-endpoint-path, TLS half.

    The derivation only ever emits ``"true"`` / ``"false"`` — the exact string
    literals Terraform accepts for a ``bool`` ``TF_VAR_*`` — for either toggle
    value, so Terraform never receives an unparseable insecure value.
    """
    assert {_derive_insecure(True), _derive_insecure(False)} == {"true", "false"}


def test_playbook_tf_env_derives_insecure_from_validate_certs():
    """Feature: fix-cluster-env-endpoint-path, TLS half.

    Structural guard: the provision role's ``svc07_tf_env`` MUST carry a
    ``TF_VAR_proxmox_insecure`` entry derived from ``svc07_proxmox_validate_certs``
    (negated, lowercased). This pins the wiring so it can't silently regress —
    the live Phase-2 ``terraform apply`` past the x509 cert error is the
    authoritative end-to-end confirmation. Repointed to
    ``svc07_provision/defaults/main.yml`` (Task 4.2/11) where ``svc07_tf_env``
    now lives.
    """
    text = _PROVISION_DEFAULTS.read_text(encoding="utf-8")

    # The TF_VAR_proxmox_insecure key must exist in the playbook.
    assert "TF_VAR_proxmox_insecure" in text, (
        "svc07_tf_env is missing the TF_VAR_proxmox_insecure entry — the "
        "bpg/proxmox provider's TLS verification would fall back to its default "
        "and drift from preflight's svc07_proxmox_validate_certs toggle."
    )

    # It must be DERIVED from svc07_proxmox_validate_certs via a negation, not
    # hardcoded. Match the whole mapping line tolerantly (quoting/spacing).
    pattern = re.compile(
        r"TF_VAR_proxmox_insecure\s*:\s*['\"]?\{\{\s*\(\s*not\s*\(\s*"
        r"svc07_proxmox_validate_certs\s*\|\s*bool\s*\)\s*\)\s*\|\s*lower\s*\}\}",
    )
    assert pattern.search(text), (
        "TF_VAR_proxmox_insecure must be derived from svc07_proxmox_validate_certs "
        "as `(not (svc07_proxmox_validate_certs | bool)) | lower` so ONE toggle "
        "governs both preflight and the Terraform provider."
    )
