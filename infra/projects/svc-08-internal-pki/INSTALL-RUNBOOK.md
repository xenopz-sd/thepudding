# SVC-08 internal PKI — install runbook

**Status: implemented.** The CA, issuance, CRL and clean-slate were live-validated on 2026-11-16 (see
[`validation.md`](../../../.kiro/specs/svc-08-internal-pki/validation.md)). Traefik ACME, the OpenBao listener certificates and the verified seal client were live-validated on
2026-11-17. The openbao-agent sidecars and the consumer path verify too (WS-C60). The listener-cert renewal
mechanism is WS-C61.
Audience: platform operators (Developer B) and service authors (Developer A).

SVC-08 is the OpenBao PKI secrets engine on SVC-07: a root mount `pki` and an issuing mount `pki_int`, keys generated
inside OpenBao and never exported. There is nothing to provision. The installer configures the CA, publishes the public
root, installs it in guest trust stores and proves issuance.

## Prerequisites

- SVC-07 initialized and unsealed, with `OPENBAO_ADMIN_TOKEN` in the cluster env file
  (`svc-07-bootstrap.yml -e openbao_mint_admin_token=true`).
- SVC-10 installed (`svc-10-bootstrap.yml`): OpenBao checks ACME DNS-01 records through its recursor.
- `PLATFORM_DOMAIN` in the cluster env file.

## Provision, then test

Offline preflight:

<!-- doctest: offline -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check -i ansible/inventory/localhost.yml ansible/playbooks/svc-08-bootstrap.yml
```

Offline tests:

<!-- doctest: offline -->
```bash
~/venv/devinfra/bin/python -m pytest tests/installer/test_svc08_pki.py tests/installer/test_svc08_roles.py -q
```

Stand up the CA (requires a live SVC-07 and SVC-10):

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml ansible/playbooks/svc-08-bootstrap.yml -e cluster_env_file=cluster.dev.env
```

The handoff runs a self-test (issue, verify against the root, revoke, rotate and check the CRL). A second run must report
`changed=0` apart from the allow-listed self-test tasks. Then test what you built:

<!-- doctest: requires-infra -->
```bash
openssl x509 -in .pki/platform-root-ca.pem -noout -subject -dates
```

## Getting a certificate for a service

Include the `pki_issue` role from the service's installer with `pki_issue_service` (a role in
`pki_ca_consumers`), `pki_issue_common_name`, `pki_issue_host`, `pki_issue_cert_path`, `pki_issue_key_path` and
`pki_ca_admin_token`. It issues only when the file at the path does not chain to the root, misses a SAN or has 30 days
or fewer left. The issuing credential is a 5-minute child token limited to `pki_int/issue/<service>`.

## Clean-slate

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml ansible/playbooks/svc-08-bootstrap.yml -e cluster_env_file=cluster.dev.env -e clean_slate=true -e clean_slate_confirm=force
```

This disables both mounts (destroying the CA keys), removes the published root, the `PLATFORM_CA_CERT` key and the
discovery marker. Guest trust entries are not removed one by one; they go with the guests or on the next trust run.

## What turns on once the root is published

- **OpenBao (SVC-07):** listener certificates for the primary and unsealer (issued here, reloaded with SIGHUP). Re-run
  `svc-07-bootstrap.yml` afterwards: the seal client switches to verifying the unsealer.
- **Traefik (SVC-09):** re-run `svc-09-bootstrap.yml`. Traefik gets the `internal` ACME resolver and serves a wildcard
  for `<domain>`, `*.<domain>` and `*.web.<domain>` within about a minute. Every route verifies with the root.
- **Grafana (SVC-17/18):** the installer selects the real-CA path; `-e obs_tls_self_signed=true` now fails closed.
- **Projects (consumer path):** remove `OPENBAO_TLS_SELF_SIGNED=true` from the cluster env file. Onboarding and the
  workload's OpenBao Agent then verify against the root. Every guest that runs the `common` baseline trusts it.

Check from your workstation after trusting the root:

<!-- doctest: requires-infra -->
```bash
BAO_ADDR=https://10.0.20.10:8200 BAO_CACERT=.pki/platform-root-ca.pem bao status
```

## Known limits of this increment

- The OpenBao listener certificates last 90 days and are renewed only when `svc-08-bootstrap.yml` runs again (it
  reissues at 30 days left). Run it monthly until a renewal mechanism lands (WS-C61).
- The unsealer's in-container CLI still skips verification (its container is never recreated after init).
- A Traefik restart during a DNS-01 challenge can leave a stale `_acme-challenge` TXT value. Harmless and left as is.
