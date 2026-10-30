# SVC01_Consumer_Root — per-project PostgreSQL database (`db/` root)

This directory is the **SVC01_Consumer_Root**: the thin per-project Terraform
root that gives a Consumer_Project its **own** PostgreSQL guest on its **own**
project VLAN (100+, never VLAN 20). It is the first per-project-VLAN SVC-01
consumer (SVC-01 previously ran only as the shared ZITADEL backing DB on
VLAN 20).

- **Spec:** [`.kiro/specs/project-consumer-onboarding/`](../../../../.kiro/specs/project-consumer-onboarding/)
- **ADR:** [ADR-0019 — per-project SVC-01 consumer root pattern](../../../../.kiro/decisions/0019-per-project-svc01-consumer-root-pattern.md)
- **Install runbook (provision-then-test):** [`../INSTALL-RUNBOOK.md`](../INSTALL-RUNBOOK.md)
- **Consumer journey (end-to-end):** [`../../../../docs/CONSUMER-JOURNEY.md`](../../../../docs/CONSUMER-JOURNEY.md)

> `dronefleet` is the scaffold/test slug. Copy this whole `db/` directory to
> `infra/projects/<your-slug>/db/` and change `var.project_slug`'s default (or
> pass `-var project_slug=<slug>`). Everything else — `vlan_id`, `host_index`,
> host IP — is **derived**, never hand-edited.

---

## What it does (reuse, not a new module — ADR-0019)

This root does **not** re-declare a DB container or re-derive addressing. It
**reuses** the existing reusable SVC-01 root
([`infra/projects/svc-01-sql-database`](../../svc-01-sql-database)) as a module,
threading in only the per-project inputs:

| Concern | Where it comes from | Requirement |
|---|---|---|
| `vlan_id` (100+) | **resolved** from the Registry (`projects.yaml`) for `project_slug` — never an operator input | 2.1, 2.3, 3.1, 3.2 |
| `host_index` | **read** from the Host_Index_Authority (`host-indices.yaml`) — never an operator input | 3.4, 3.8 |
| host IP | `cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)` | 3.4 |
| hostname / tag | `svc01-<vlan_id>-01` / `proj-<slug>-postgres` | 3.6 |
| Terraform state | `<slug>-db` (GitLab HTTP backend), distinct from `<slug>-infra` and every service state | 3.5 |
| provisioning | `proxmox-compute` module (via the reused SVC-01 module) + the `postgres-server` role | 3.3 |
| half-created guest on failure | `prevent_destroy` + re-runnable apply leave none | 3.9 |

The guest's `host_index` and the `peer_host_indices` collision guard come from
the committed ledger, so a missing/colliding index **fails the plan** at the
`proxmox-compute` Guard 4 precondition rather than defaulting to a colliding `0`
(Req 3.7, ADR-0015).

---

## Inputs

| Variable | Purpose |
|---|---|
| `project_slug` | The **only** project-identifying input. Must match a `slug` in [`projects.yaml`](../../../platform-foundation/projects.yaml). The root resolves `vlan_id` from the Registry and `host_index` from the ledger using it — **never** a free `vlan_id`/`host_index` input (Req 2.3, 3.8). Default `dronefleet`. |
| `db_engine` | `postgres` (default) or `mariadb` (strict-EU). |
| `tier` | `small` (default) or `medium`. |
| `proxmox_node_name`, `svc01_vmid`, `svc01_template_file_id`, `svc01_datastore_id` | Per-cluster plumbing threaded into the reused SVC-01 module. |
| `operator_ssh_public_keys` | Public keys injected so Ansible can reach the guest. Public keys only; from `TF_VAR_*`/gitignored `.env`, never committed. |
| `proxmox_endpoint`, `proxmox_api_token`, `proxmox_insecure` | Provider wiring (from CI variables / `TF_VAR_*`). |

## Outputs

`db_host`, `db_port`, `db_engine`, `vlan_id`, `host_index`,
`consumer_db_state_name` (`<slug>-db`), and the generic `inventory_hosts`
surface the Ansible dynamic-inventory generator keys on. **No secret** (the DB
password) is ever a Terraform output — it is written to OpenBao by the Ansible
layer (Req 8.1).

---

## Placeholders — no real credentials

| Placeholder in docs | Substituted with |
|---|---|
| `https://pve.example.internal:8006/` | `TF_VAR_proxmox_endpoint` |
| `USER@REALM!TOKENID=UUID` | `TF_VAR_proxmox_api_token` |
| `<slug>` | the real project slug (e.g. `dronefleet`) |
| `<GITLAB-HOST>` / `<PROJECT-ID>` | `CI_API_V4_URL` / `CI_PROJECT_ID` |

> **Terraform is not installed in the local authoring environment.** The
> `offline` commands below (no infra/credentials) run anywhere the binary
> exists. See [`TESTING.md`](../../../../TESTING.md) and
> [`../INSTALL-RUNBOOK.md`](../INSTALL-RUNBOOK.md) for the full offline-vs-cluster
> guide and the provision-then-test walkthrough.

---

## Offline preflight (Tier-1)

Format and validate the root with no infrastructure or credentials:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
terraform -chdir=infra/projects/dronefleet/db fmt -check -recursive
```

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
terraform -chdir=infra/projects/dronefleet/db init -backend=false
terraform -chdir=infra/projects/dronefleet/db validate
```

Validate the Consumer_Manifest the orchestrator reads (offline, side-effect-free):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/python scripts/installer/consumer_manifest.py \
  --check infra/projects/dronefleet/consumer-manifest.yml
```

## Provision + test

The real `terraform apply` (which creates the DB guest on the live project VLAN)
and the `requires-infra`-gated tests that run against the freshly-provisioned DB
are documented, in order, in the
[**provision-then-test walkthrough**](../INSTALL-RUNBOOK.md) — that runbook is
the single authoritative place for standing the DB up from nothing and then
testing it.
