# SVC-06 Install Runbook — provision ZITADEL (backing DB + IdP) from nothing, then test it

This is the **provision-then-test spine** for SVC-06 (the shared ZITADEL identity
provider): it takes you from an empty throwaway Proxmox cluster to a running
ZITADEL — backed by its own dedicated PostgreSQL (an SVC-01 instance) and fronted
by Traefik — then verifies the OIDC discovery endpoint against the instance you
just built.

Section **A** is the **Developer B** fast path (one Ansible command). Sections
**0–4** are the **Developer A** manual spine.

> **Placeholders only.** The `ZITADEL_MASTERKEY`, the backing-DB password, and
> every OIDC client secret are generated/stored in OpenBao
> (`secret/proj-shared-zitadel/...`) and never committed or echoed.

> **Doctest tiers.** Offline preflight carries `<!-- doctest: offline -->`; the
> apply/install/health/teardown carry `<!-- doctest: requires-infra -->`. Tier
> rules: [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md);
> runbook location: [`structure.md`](../../../.kiro/steering/structure.md).

---

## ⚠ Throwaway cluster only

The live steps provision, mutate, and tear down real infrastructure. Run them
**only against a throwaway / ephemeral Proxmox cluster**. Section 4's teardown
`terraform destroy`s the ZITADEL LXC **and** orchestrates the backing DB's own
clean-slate (its data volume).

---

## Prerequisites

- The shared VLAN-20 SDN VNet `p20` must exist (created by the platform-foundation
  layer). SVC-06 Preflight fails closed naming it if absent.
- **SVC-01** (the reusable Postgres root/role) must be present — SVC-06 stands up
  its backing DB by running `svc-01-bootstrap.yml` as a child.
- **SVC-09 (Traefik)** is the production HTTP ingress. When it is **not** on the
  target (dev/offline), use the two default-off mocks (below) so ZITADEL still
  serves HTTPS + attaches an `edge` network:
  - `-e zitadel_tls_self_signed=true` — self-signed cert (control-node generated).
  - `-e zitadel_mock_edge_network=true` — placeholder `edge` network.
  Both are **OFF** in acceptance/production (Traefik owns edge + real TLS). See
  [`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md) Mock 5 + Mock 6.

---

## A. Developer-B automated path — one command, provision-then-test

### A.0 — Offline preflight (no cluster needed)

Validate the ZITADEL Terraform root:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
cd infra/projects/svc-06-identity-provider
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

Parse-check the orchestrator + helper and run the SVC-06 offline suite:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/localhost.yml ansible/playbooks/svc-06-bootstrap.yml
~/venv/devinfra/bin/python -c "import ast; ast.parse(open('scripts/installer/zitadel_provision.py').read())"
~/venv/devinfra/bin/pytest infra/tests/test_svc06_terraform_shape.py \
  infra/tests/test_svc06_zitadel_contract.py -q
~/venv/devinfra/bin/pytest tests/installer/test_svc06_backing_db.py \
  tests/installer/test_svc06_mocks.py tests/installer/test_zitadel_provision.py \
  tests/installer/test_svc06_thin_orchestrator_play.py \
  tests/installer/test_svc06_swap_gate.py tests/installer/test_svc06_secret_hygiene.py -q
~/venv/devinfra/bin/pytest infra/tests/test_svc06_provision_token_wiring.py -q
```

### A.1 — One-command install (backing DB → ZITADEL → org → health)

Stand ZITADEL up end-to-end (pick a free VMID for the ZITADEL guest; the backing
DB VMID defaults to 1102). In dev/offline (no Traefik), add the two mock flags.
**No `ZITADEL_PROVISION_TOKEN` is required** — the provisioning credential is
auto-minted at first-init and stored in OpenBao, and the org-onboarding step
reads it from there (ADR-0016):

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-06-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env \
  -e svc06_zitadel_vmid=1103 \
  -e zitadel_tls_self_signed=true -e zitadel_mock_edge_network=true
```

The orchestrator runs Preflight (cluster.env, Proxmox auth, `p20` gate, datastore
autodetect), Provisioning (a **child `svc-01-bootstrap`** that stands up the
backing DB `proj-shared-zitadel` with its own isolated state, then
`terraform apply` of the ZITADEL LXC), Bootstrap (the `common` role, then
`zitadel.yml` — the ZITADEL stack with masterkey + DB creds from OpenBao — then
`zitadel-org-provision` to onboard a first org + OIDC app), and Handoff
(`svc06-health.yml` OIDC-discovery gate + banner).

> **Credential handoff is wired by default.** `svc06_openbao_addr` defaults to
> the SVC-07 OpenBao primary's VLAN-20 address (`10.0.20.10`), so a standard
> bring-up points BOTH the backing-DB child (`openbao_addr`, SVC-01's write gate)
> and the ZITADEL run (`zitadel_openbao_addr`, ZITADEL's read gate) at the SAME
> OpenBao source. SVC-01 therefore STORES the `shared_zitadel_app` password it
> ALTERs onto the role at `secret/proj-shared-zitadel/postgres` (field
> `app_password`), and ZITADEL READS that identical password — the three chain
> points agree and ZITADEL authenticates. `OPENBAO_ADMIN_TOKEN` must be present
> in the environment so both child runs can reach OpenBao. If OpenBao is wired
> but the read comes back empty, the `zitadel-server` role now **fails fast**
> naming that KV path+field rather than crash-looping on SQLSTATE 28P01. Override
> `svc06_openbao_addr=""` ONLY for a deliberate dev/offline run (then also supply
> an explicit `zitadel_db_password_override`). Spec:
> [`fix-svc06-zitadel-db-app-password-mismatch`](../../../.kiro/specs/fix-svc06-zitadel-db-app-password-mismatch/).

### A.2 — Onboard a project to SSO (Day-2, idempotent)

> **No human-minted token needed.** The ZITADEL management-API provisioning
> credential is obtained **automatically at first-init** and stored in OpenBao
> (spec [`svc06-org-provision-token-wiring`](../../../.kiro/specs/svc06-org-provision-token-wiring/),
> ADR-0016). During the ZITADEL stack's `start-from-init`, ZITADEL's
> `FirstInstance` config seeds a dedicated non-human machine user
> (`provision-sa`, `IAM_OWNER` by ZITADEL necessity for org-creation) with a
> bounded-expiry PAT and writes that PAT to a mounted emit file. The
> `zitadel-server` role reads it once, persists it to
> `secret/proj-shared-zitadel/zitadel` field `provision_token`, and scrubs the
> on-guest file. On a re-run the stored token is read back (never re-minted).
> The `zitadel-org-provision` role reads `provision_token` from OpenBao by
> default — so onboarding needs **no `ZITADEL_PROVISION_TOKEN`** in the env.

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
# Default path: no ZITADEL_PROVISION_TOKEN needed — the role reads the
# auto-minted token from OpenBao. OPENBAO_ADMIN_TOKEN must be in the env.
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/zitadel-org-provision.yml -e zop_project_slug=dronefleet
```

Creates org `proj-dronefleet`, default roles, and the `dronefleet-webapp` OIDC
app; a re-run is a no-op success (already-exists). The OIDC client secret is
written to OpenBao at `secret/proj-dronefleet/zitadel/oidc/dronefleet-webapp`.

**Manual / CI override.** To supply the provisioning token out-of-band instead
of reading it from OpenBao (e.g. a locked-down deployment, or a CI job with the
token in a masked variable), export `ZITADEL_PROVISION_TOKEN` — a non-empty env
value takes precedence over the OpenBao-stored token. It is never committed,
never passed on the argv, and `no_log` throughout:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
# Override path: a non-empty env token wins over the OpenBao value.
export ZITADEL_PROVISION_TOKEN='<service-account-PAT>'
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/zitadel-org-provision.yml -e zop_project_slug=dronefleet
```

### A.3 — Verify OIDC discovery resolves

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/svc-06-identity-provider.generated.yml \
  ansible/playbooks/svc06-health.yml
```

### A.4 — Clean-slate reset (ZITADEL guest + backing DB)

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-06-bootstrap.yml \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e svc06_zitadel_vmid=1103 -e svc06_proxmox_host=<real-proxmox-lan-ip>
```

Pct-destroys the ZITADEL LXC and orchestrates the backing DB's own clean-slate
(child `svc-01-bootstrap -e clean_slate=true`), then verifies an empty baseline
over both guests (`pct list` + `terraform state list`).

---

## 0–4. Developer-A manual reference spine

- **0. Fabric gate.** Confirm the shared VNet `p20` exists.
- **1. Backing DB.** Run `svc-01-bootstrap.yml` with
  `-e project_slug=shared-zitadel -e vlan_id=20 -e svc01_vmid=1102 -e host_index=4
  -e db_engine=postgres` → PostgreSQL `shared_zitadel` + role `shared_zitadel_app`,
  state `shared-zitadel-svc01-postgres`.
- **2. Provision ZITADEL.** `terraform apply` the SVC-06 root with
  `-var zitadel_vmid=1103 -var zitadel_host_index=5`. `prevent_destroy` guards the
  masterkey-stateful LXC.
- **3. Configure.** `common` installs Docker + Compose; `zitadel.yml` runs the
  `zitadel-server` role (masterkey generated-once + DB creds from OpenBao, Traefik
  labels, `ZITADEL_EXTERNAL*`/TLS matching the public endpoint, metrics). The
  backing-DB `app_password` is read from `secret/proj-shared-zitadel/postgres`
  (the SAME OpenBao source SVC-01's child write used, via the shared
  `svc06_openbao_addr` default `10.0.20.10`); a wired-but-empty read **fails fast**
  naming the KV path+field instead of presenting an empty password (SQLSTATE
  28P01).
- **4. Onboard + health.** The `zitadel-server` run also obtains the provisioning
  PAT ZITADEL emits at first-init and stores it at
  `secret/proj-shared-zitadel/zitadel` (field `provision_token`), scrubbing the
  on-guest emit file; `zitadel-org-provision` then reads that token from OpenBao
  (no human token needed) and creates the first org/OIDC app; `svc06-health.yml`
  gates on the container Running + OIDC discovery 200.

## Security notes

- HTTP ingress is via **Traefik labels only** — ZITADEL publishes **no host
  port**; Traefik terminates TLS (or the self-signed mock in dev).
- The `ZITADEL_MASTERKEY` is generated **once** and **never rotated** (a change
  requires a full data re-encryption); it lives only in OpenBao.
- The backing DB is a least-privilege SVC-01 instance with its own isolated state.
- The ZITADEL mgmt-API provisioning token is **auto-minted at first-init** as a
  dedicated non-human machine user's bounded-expiry PAT (`IAM_OWNER` by ZITADEL
  necessity for org-creation, ADR-0016), stored **only** in OpenBao at
  `secret/proj-shared-zitadel/zitadel` (field `provision_token`), `no_log`
  throughout, with the first-init emit file scrubbed after capture. The optional
  `ZITADEL_PROVISION_TOKEN` env override (manual/CI) and the OpenBao admin token
  are read from the environment, never a CLI flag or committed file.
- MFA (TOTP) is enforceable via org login policy — the single most important
  control, since a compromised ZITADEL admin compromises every OIDC consumer.
