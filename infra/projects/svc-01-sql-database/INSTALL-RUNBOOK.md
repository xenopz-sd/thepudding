# SVC-01 Install Runbook — provision a per-project database from nothing, then test it

This is the **provision-then-test spine** for SVC-01 (the dedicated-per-project
SQL database): it takes you from an empty throwaway Proxmox cluster to a running
PostgreSQL (or MariaDB) for a given project, then verifies it against the instance
you just built. SVC-01 is a **reusable** root — the same one command stands a DB
up for `dronefleet` on VLAN 100 or for ZITADEL's backing store on VLAN 20.

> To stand this service up as part of the whole platform (dependency-ordered, from
> an empty cluster), see the [all-services walkthrough](../../../docs/TESTING.md#deploy-every-implemented-service-from-scratch-all-services-walkthrough)
> in the testing guide.

Section **A** is the **Developer B** fast path (one Ansible command). Sections
**0–3** are the **Developer A** manual spine.

> **Placeholders only.** DB passwords are generated at provision, stored in
> OpenBao (`secret/proj-<slug>/postgres`), and never committed or echoed.

> **Doctest tiers.** Offline preflight carries `<!-- doctest: offline -->`; the
> apply/install/health/teardown carry `<!-- doctest: requires-infra -->`. Tier
> rules: [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md);
> runbook location: [`structure.md`](../../../.kiro/steering/structure.md).

---

## ⚠ Throwaway cluster only

The live steps provision, mutate, and tear down real infrastructure. Run them
**only against a throwaway / ephemeral Proxmox cluster**. Section 3's teardown
`terraform destroy`s the DB LXC (and its data volume).

---

## Prerequisite: the project's VLAN fabric

SVC-01 attaches to the **project's own** SDN VNet `p<vlan>`:
- **Per-project DB** (VLAN 100–254): the project's own Terraform root must have
  created its VNet first (onboard the project via `projects.yaml`, then apply its
  root).
- **Shared-service backing DB** (VLAN 20, e.g. ZITADEL): `p20` is created by the
  platform-foundation layer.

The SVC-01 Preflight phase fails closed naming the missing `p<vlan>` if it is absent.

---

## A. Developer-B automated path — one command, provision-then-test

### A.0 — Offline preflight (no cluster needed)

Validate the reusable SVC-01 Terraform root:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
cd infra/projects/svc-01-sql-database
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

Parse-check the orchestrator and run the SVC-01 offline suite:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/localhost.yml ansible/playbooks/svc-01-bootstrap.yml
~/venv/devinfra/bin/pytest infra/tests/test_svc01_terraform_shape.py \
  infra/tests/test_svc01_destroy_isolation.py \
  infra/tests/test_svc01_postgres_contract.py -q
~/venv/devinfra/bin/pytest tests/installer/test_svc01_thin_orchestrator_play.py \
  tests/installer/test_svc01_swap_gate.py \
  tests/installer/test_svc01_secret_hygiene.py -q
```

### A.1 — One-command install (provision → bootstrap → health)

Stand a dedicated PostgreSQL up for a project (`project_slug` / `vlan_id` /
`svc01_vmid` are the per-instance identity; pick a free VMID):

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-01-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env \
  -e project_slug=dronefleet -e vlan_id=100 -e svc01_vmid=1101 \
  -e tier=small -e enable_pgvector=true
```

The orchestrator runs Preflight (cluster.env, Proxmox auth, project `p<vlan>`
gate, datastore autodetect), Provisioning (`terraform apply` of the single DB LXC
with per-instance state `<slug>-svc01-postgres`, inventory, known_hosts),
Bootstrap (the `common` role then `postgres.yml` — the DB + least-privilege
`<slug>_app` role + requested extensions, passwords stored in OpenBao), and
Handoff (`svc01-health.yml` + banner).

> **First-run credential-storage behavior** (spec:
> [`fix-svc01-db-install-first-run-transient`](../../../.kiro/specs/fix-svc01-db-install-first-run-transient/)).
> When OpenBao credential storage is wired for the run — i.e. an `openbao_addr`
> is set (e.g. `-e openbao_addr=10.0.20.10`, or via the SVC-06 backing-DB path) —
> the installer resolves `OPENBAO_ADMIN_TOKEN` at Phase-1 preflight with
> precedence: the process environment first, then the cluster env file
> (`cluster_env_file`). So the one-command install works without a manual
> `export` as long as the token is present in the cluster env file (SVC-07's
> bootstrap writes it there by name). If OpenBao is wired but the token resolves
> empty from BOTH sources, the installer fails fast at preflight with an explicit
> message naming `OPENBAO_ADMIN_TOKEN` and the remedy — rather than failing later
> behind a `no_log` mask. A dev/offline run with NO `openbao_addr` set skips the
> OpenBao credential write entirely (the generated passwords stay only in the
> on-guest `0600` env), and the token gate does not apply. The installer likewise
> fails fast at preflight if `project_slug` resolves empty — use the sanctioned
> `svc-01-bootstrap.yml -e project_slug=<slug>` entry point, not a hand-run of the
> standalone `postgres.yml`.

To stand the MariaDB alternative up instead, add `-e db_engine=mariadb`.

### A.2 — Verify the credential handoff (no plaintext anywhere)

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
# The app credential is retrievable ONLY from OpenBao — never a repo/CI log.
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/svc-01-sql-database.generated.yml \
  ansible/playbooks/svc01-health.yml -e project_slug=dronefleet -e vlan_id=100
```

### A.3 — Clean-slate reset

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-01-bootstrap.yml \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e project_slug=dronefleet -e vlan_id=100 -e svc01_vmid=1101 \
  -e svc01_proxmox_host=<real-proxmox-lan-ip>
```

Pct-destroys the DB LXC and removes it from this project's Terraform state, then
verifies an empty baseline (`pct list` + `terraform state list`).

---

## 0–3. Developer-A manual reference spine

- **0. Fabric gate.** Confirm the project VNet `p<vlan>` exists.
- **1. Provision.** `terraform apply` the SVC-01 root with the project's
  `-var project_slug=... -var vlan_id=... -var svc01_vmid=...` (per-instance state
  `<slug>-svc01-postgres`). `prevent_destroy` guards the stateful DB LXC.
- **2. Configure.** `common` installs Docker + Compose; `postgres.yml` runs the
  `postgres-server` role (or `mariadb-server` for `db_engine=mariadb`): the DB,
  the least-privilege `<slug>_app` role, requested extensions, the nightly
  `pg_dump` timer, and the OpenBao credential write.
- **3. Health.** `svc01-health.yml` gates on the container Running + engine
  readiness (`pg_isready`, or MariaDB healthcheck).

## Security notes

- No Traefik, no LAN host port — PostgreSQL is wire-protocol; 5432 is bound only
  to the guest's private VLAN IP (the documented docker-compose-app exception).
- The `<slug>_app` role is NOSUPERUSER/NOCREATEDB/NOCREATEROLE, granted only
  CONNECT/CREATE/TEMPORARY on its own database.
- All passwords are generated at provision, written to OpenBao, never committed.
- Optional `-e enable_firewall_rules=true` adds a defence-in-depth guest firewall
  rule scoping 5432 to the project subnet — default-off and reversible per the
  ADR-0008 posture (the primary isolation is the VLAN-per-project L2 model).
