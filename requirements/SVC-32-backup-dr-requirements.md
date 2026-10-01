# SVC-32 — Backup & Disaster Recovery Requirements

## 1. Overview

This service provides platform-wide backup and disaster recovery: VM/LXC-level image backups via Proxmox Backup Server, plus targeted application-level backups for services whose data cannot be fully or efficiently recovered from a VM snapshot alone (SVC-01 PostgreSQL via `pg_dump`, SVC-03 Garage/MinIO via bucket replication, SVC-07 OpenBao via Raft snapshot and unseal-key escrow). It is a Tier 1 core service underpinning every project type — without it, a single disk failure or operator mistake on any of the other 30+ catalog services becomes an unrecoverable incident rather than a restore.

## 2. Scope

**In scope:**
- Proxmox Backup Server (PBS) deployment for VM/LXC-level backups of every compute unit in the fleet.
- Backup schedule and retention policy definitions.
- Application-level backup for SVC-01 (PostgreSQL logical dumps), SVC-03 (Garage bucket replication / MinIO mirroring), and SVC-07 (OpenBao Raft snapshot + unseal-key escrow).
- Restore testing cadence and procedure.
- Off-site/secondary copy strategy appropriate for a small self-hosted setup.

**Out of scope:**
- Application-level backup design for every other catalog service individually (each service's own requirements doc should reference this document's PBS baseline and add service-specific detail only where a VM-level snapshot is insufficient, as is the case for SVC-01/03/07 here).
- Long-term compliance/legal-hold archival (not a stated requirement at this scale).
- Cross-region cloud DR (no cloud footprint assumed; this is a self-hosted, on-prem/office-cluster design).

## 3. Technology selection

- **Proxmox Backup Server** (AGPLv3) — default and sole VM/LXC-level backup solution, matching the catalog's Proxmox-native provisioning model. **Proxmox Server Solutions GmbH**, Vienna, Austria ([Proxmox Server Solutions GmbH — legal notice](https://www.proxmox.com)) — fully European origin and governance, and the natural choice given the entire compute layer is already Proxmox VE (F2).
- **`pg_dump`** (PostgreSQL's own bundled logical backup tool, part of the PostgreSQL project referenced in SVC-01) — used for application-level, point-in-time-consistent logical backups of SVC-01 databases, complementing PBS's crash-consistent VM snapshots with a format that is portable across PostgreSQL versions and restorable to a single database or table.
- **Garage's built-in geo-distributed replication** (and MinIO's mirroring/replication feature as the fallback path for any project using the MinIO alternative) — used for SVC-03 object-storage durability, since Garage is explicitly designed for multi-site replication rather than relying solely on VM-level snapshots of its storage nodes ([Garage — geo-distributed replication](https://garagehq.deuxfleurs.fr/)).
- **OpenBao Raft snapshot** — OpenBao's native `bao operator raft snapshot save` mechanism, combined with manual, out-of-band escrow of the Shamir unseal key shares — used for SVC-07, since a sealed OpenBao instance restored from a VM snapshot alone is useless without its unseal keys, which must never live in the same backup artifact as the encrypted data they unlock.
- No alternative technology is evaluated for the VM-level layer since Proxmox Backup Server is the native, purpose-built counterpart to the Proxmox VE compute layer already mandated platform-wide (F2/F3); this choice is carried directly from catalog §3.I (SVC-32) and is not re-litigated here.

## 4. Multi-tenancy model

**Shared platform service.** One Proxmox Backup Server instance backs up every VM/LXC across every project, consistent with catalog §1's guidance that backup/DR benefits from centralized operation. Isolation and organization are implemented via:
- **Datastore namespaces:** PBS supports namespacing within a datastore; each project (or each service tier) is backed up into its own namespace (`ns/<slug>`), keeping per-project retention and access policy administratively separable even though the underlying datastore is shared.
- **Per-project/per-service backup jobs:** each Proxmox VE node's backup schedule references the specific VMIDs/CTIDs belonging to a project, tagged accordingly in Proxmox VE's own tagging system, so a project's backups can be identified, restored, or purged independently.
- **API tokens scoped per backup job:** PBS supports namespace-scoped API tokens; the backup job for a given project uses a token restricted to writing only within that project's namespace, limiting blast radius if a single VM's backup credentials were ever compromised.
- Application-level backups (SVC-01/03/07) are inherently per-project or per-shared-instance already (each project's Postgres database, each project's bucket, the one shared OpenBao instance), so no additional multi-tenancy layer is needed beyond what those services already define in their own requirements docs.

## 5. Functional requirements

- **FR-1:** PBS performs incremental, deduplicated, encrypted backups of every Proxmox VM and LXC container in the fleet on a defined schedule (see §6).
- **FR-2:** PBS backup jobs are defined centrally (via Proxmox VE's backup job scheduler pointing at the PBS datastore) rather than per-node ad hoc, so schedule/retention changes are made in one place.
- **FR-3:** SVC-01 PostgreSQL instances run a nightly `pg_dump` (or `pg_dumpall` for full-cluster metadata) to a local file, which is then captured by the next PBS VM-level backup pass and additionally copied to SVC-03 object storage for a format that survives even a full VM loss between PBS runs.
- **FR-4:** SVC-03 Garage buckets are configured with a replication factor of at least 3 across independent storage nodes/zones by default (Garage's own recommended minimum for full quorum-based durability); where feasible, at least one replica zone is physically or logically separated from the primary Proxmox host (e.g., a separate machine or site) to survive a single-host failure.
- **FR-5:** SVC-07 OpenBao performs a scheduled Raft snapshot (`bao operator raft snapshot save`) at least daily, stored outside the OpenBao VM itself (pushed to SVC-03 or PBS); the Shamir unseal key shares are escrowed manually, out-of-band, split across at least two trusted holders/locations, and are never stored in the same backup artifact, datastore, or credential store as the encrypted Raft snapshot itself.
- **FR-6:** Backup jobs alert on failure via the platform's observability stack (SVC-17/18/20) — a failed backup is treated as an incident, not a silent skip.
- **FR-7:** Restore procedures are documented and tested on a defined cadence (§6) for at least one representative VM, one PostgreSQL database, and the OpenBao snapshot, to confirm recoverability rather than assuming untested backups are valid.
- **FR-8:** PBS enforces backup encryption at rest (client-side encryption key managed outside the PBS server itself, ideally escrowed via the same out-of-band process as the OpenBao unseal keys) so that a stolen backup datastore disk does not expose plaintext VM data.

## 6. Non-functional requirements

- **Availability target:** the backup service itself targets 99% availability for scheduling/monitoring purposes, but its actual value is measured by **Recovery Point Objective (RPO)** and **Recovery Time Objective (RTO)**, not uptime: target RPO of 24 hours for most VMs (nightly backup), RPO of 1 hour or better for SVC-01 (via more frequent `pg_dump` or WAL archiving if a project needs tighter RPO), and target RTO of 4 hours for a full VM restore at this cluster's scale.
- **Performance/sizing** (small homelab/office cluster, ~15–25 VMs/LXCs, modest per-VM disk footprint):
  - PBS server: minimum 2 CPU cores, 2 GB RAM for evaluation-only use; **recommended for this cluster: 4 CPU cores, 8 GB RAM** (PBS builds an in-memory chunk index for deduplication, and the sizing rule of thumb is roughly 1 GB RAM per 1 TB of deduplicated datastore, so 8 GB comfortably covers a multi-TB datastore at this scale) ([Proxmox Backup Server system requirements](https://pbs.proxmox.com/docs/system-requirements.html), [PBS requirements and sizing](https://www.zmanda.com/blog/proxmox-backup-server-requirements-2/)).
  - Datastore disk: sized at roughly 2–3× the sum of all backed-up VMs' used disk space to account for retained history under deduplication (actual growth is workload-dependent; monitor and adjust).
  - Backup schedule bandwidth: nightly full-fleet incremental backups at this scale (tens of VMs, modest per-VM change rate) comfortably fit within an overnight backup window on gigabit internal networking.
- **Backup/DR (this document defines the policy other services reference):**
  - **Schedule:** nightly incremental VM/LXC backups (e.g., 02:00 local time, staggered across nodes to avoid I/O contention); SVC-01 `pg_dump` nightly at 01:00 (before the VM-level backup captures it); SVC-07 Raft snapshot daily; SVC-03 replication is continuous/near-real-time by design (not a scheduled job).
  - **Retention policy:** PBS's built-in retention rules — keep the last 7 daily, last 4 weekly, last 6 monthly backups per VM (`keep-daily=7`, `keep-weekly=4`, `keep-monthly=6`), pruned automatically by PBS's garbage collection. `pg_dump` files retain the same 7-daily/4-weekly/6-monthly pattern in the SVC-03 bucket. OpenBao Raft snapshots retain the last 14 daily copies given their small size and criticality.
  - **Restore testing cadence:** quarterly full-VM restore drill (restore a non-critical VM to a scratch location and verify boot/service health), monthly PostgreSQL restore drill (restore the latest `pg_dump` to a scratch database and run an application-level smoke query), and a semiannual full OpenBao disaster-recovery drill (restore the Raft snapshot to a fresh OpenBao instance and unseal it using the escrowed key shares, confirming the whole chain works end-to-end, not just the snapshot file's existence).
  - **Off-site/secondary copy strategy:** since this is a single small cluster without a second physical site by default, the pragmatic secondary-copy strategy is (a) a second physical disk or NAS target for PBS's datastore, ideally in a different room/circuit than the primary Proxmox hosts, configured as a PBS "sync job" target (`proxmox-backup-manager sync-job`) so backups are asynchronously mirrored; and (b) for the most critical artifacts specifically — OpenBao Raft snapshots, `pg_dump` files, and PBS's own datastore metadata — an additional periodic copy to removable/offline media or a trusted remote location (e.g., a second office, a relative's house, or a low-cost cloud object store used *only* as an encrypted off-site copy, never as primary storage) to survive a total loss of the primary site (fire, theft, flood).
- **Data retention:** per the retention policy above; retention windows should be revisited if a project has specific compliance-driven retention requirements exceeding these defaults.

## 7. Infrastructure architecture

- **Compute unit:** Proxmox Backup Server has an official installer image and is typically run as its own dedicated Proxmox **VM** (not LXC) so that it can present its own storage stack (ZFS datastore) and remain a separate blast-radius/administrative domain from the VE hosts it protects — following the general principle that a backup system should not share fate with the systems it backs up.
- **Minimum resource spec:** 4 vCPU, 8 GB RAM, datastore disk sized per §6 (recommend starting at 2 TB local-zfs or an attached NAS/USB array, scaling with fleet growth), plus a small 32 GB OS disk for the PBS system itself.
- **Network placement:** dedicated "infra-mgmt" VLAN; PBS communicates with every Proxmox VE node over HTTPS on **TCP port 8007** (PBS's single port for API, web UI, and backup data transfer — this port must be reachable from every Proxmox VE node to the PBS host, or backup jobs will fail with a connection error) ([Proxmox Backup Server default port](https://pbs.proxmox.com/docs/proxmox-backup.pdf), [PBS requirements and ports](https://www.zmanda.com/blog/proxmox-backup-server-requirements-2/)).
- **Storage:** local-zfs (or a dedicated ZFS pool on attached disks/NAS) for the PBS datastore, chosen specifically because PBS's deduplication and verification features are built around ZFS/chunk-store semantics; a secondary sync-job target (second disk set or NAS) provides the off-site/secondary copy described in §6.

## 8. Terraform scope

- **Module inputs:** `pbs_vm_name`, `vlan_id` (infra-mgmt), `cpu_cores` (4), `memory_mb` (8192), `os_disk_gb` (32), `datastore_disk_gb` (2048+, sized per fleet), `sync_target_type` (`nas` | `second-disk` | `remote-pbs`).
- **Resources created:** one `proxmox_virtual_environment_vm` for PBS (via `bpg/proxmox`); firewall rules opening port 8007 from every Proxmox VE node's management interface to the PBS VM, and restricting the PBS web UI/API from any other network.
- **Outputs:** `pbs_vm_ip`, `pbs_datastore_name`, `pbs_port` (8007), consumed by every other service's Proxmox VE backup-job configuration (each service's own Terraform/Ansible references this datastore as its backup target).

## 9. Ansible scope

- **Roles:** `pbs_server` (installs and configures the PBS host, ZFS datastore, sync-job target), `pbs_client_registration` (registers each Proxmox VE node with PBS, creates namespace-scoped API tokens per project), `pg_dump_backup` (cron/systemd-timer role applied to SVC-01 VMs, dumps to local disk then pushes to SVC-03), `openbao_snapshot_backup` (scheduled Raft snapshot job on the SVC-07 VM, pushes the encrypted snapshot to SVC-03/PBS, explicitly excludes unseal key material from any automated backup path).
- **Idempotency:** backup job definitions and retention rules are declared in Ansible variables and applied via PBS's API/CLI idempotently (checked for existing job ID before creating); namespace and API token creation checks for existing resources first.
- **Config files templated:** PBS datastore config, per-node backup job schedules (`vzdump.conf` equivalents / PVE backup job definitions), `pg_dump` wrapper script and cron schedule, OpenBao snapshot script and its target bucket path.
- **Secrets injected from SVC-07:** PBS API tokens for each Proxmox VE node, SVC-03 access keys used by the `pg_dump`/OpenBao-snapshot push scripts, backup encryption key (PBS client-side encryption) — noting the inherent bootstrapping consideration that OpenBao's own backup process cannot depend on OpenBao being unsealed to retrieve its own snapshot credentials; this is handled by giving the snapshot script a narrowly-scoped, long-lived static token stored outside OpenBao specifically for this purpose, reviewed periodically.

## 10. CI/CD pipeline

- **Stages:** `lint` (validate backup job YAML/schedule definitions) → `plan` (Terraform plan for the PBS VM and any datastore resizing) → `manual/auto approve` → `apply` → `configure` (Ansible: register nodes, create datastore namespaces, deploy `pg_dump`/OpenBao-snapshot scripts) → `smoke test` (trigger a manual backup job for a test VM, verify it completes and is listed in the PBS datastore; trigger a test `pg_dump` and confirm the resulting file is pushed to SVC-03).
- **Where it runs:** GitLab CI via GitLab Runner (SVC-13).
- **State backend:** GitLab-managed Terraform state (F1), scoped to the `backup-dr` module.
- A separate, low-frequency **scheduled pipeline** (not triggered by code changes) runs the quarterly/monthly/semiannual restore-drill procedures from §6 and records pass/fail results as a pipeline artifact for audit purposes.

## 11. Secrets & credentials

- **PBS datastore encryption key:** generated at PBS setup time, stored in OpenBao (SVC-07) *and* escrowed out-of-band (printed/stored offline) specifically because a scenario where OpenBao itself needs restoring must not depend on OpenBao already being available to retrieve this key.
- **Per-node PBS API tokens:** generated per Proxmox VE node/project namespace, stored in OpenBao, rotated annually.
- **SVC-03 access keys** used by `pg_dump` and OpenBao-snapshot push scripts: stored in OpenBao, scoped read/write only to their specific backup bucket.
- **OpenBao Shamir unseal key shares:** deliberately **not** stored in OpenBao itself (circular dependency) — escrowed manually across at least two trusted holders or physically separated secure locations, with a documented, access-logged procedure for retrieval during a genuine disaster-recovery event.

## 12. Security & hardening baseline

- PBS web UI/API is reachable only from the infra-mgmt VLAN and from Proxmox VE node management interfaces — never exposed externally through Traefik, since PBS itself is not a service that needs public/user-facing access.
- Backups are encrypted at rest via PBS's client-side encryption, so the backup datastore's confidentiality does not depend solely on filesystem/disk-level access control.
- Least-privilege: each Proxmox VE node's PBS API token is scoped to only its own project namespace, not the entire datastore.
- Firewall/VLAN rules: only Proxmox VE node management interfaces may reach PBS's port 8007; the sync-job target (secondary/off-site copy) is reachable only from the PBS VM itself.
- CVE scanning/patching: PBS is kept on a supported release track with regular `apt` updates applied via Ansible on a defined maintenance window, consistent with the rest of the Proxmox-managed fleet.
- Access to the OpenBao unseal-key escrow locations is itself access-logged and restricted to a named, minimal set of trusted individuals — this is a process/organizational control, not a technical one, but it must be documented as part of this service's security baseline.

## 13. Observability hooks

- PBS exposes backup job status and datastore usage via its own API, polled by a small Prometheus exporter (or PBS's built-in metrics, where available in the deployed version) and scraped by SVC-17.
- Backup job success/failure events and PBS system logs are shipped via Promtail to Loki (SVC-18) with `service=pbs` labels; `pg_dump` and OpenBao-snapshot script logs are similarly shipped with `service=pg-dump-backup` / `service=openbao-snapshot` labels.
- Key alerts to define in Alertmanager: backup job failure (any VM/LXC missed its scheduled window), datastore disk usage above 80%, `pg_dump` failure or zero-byte output, OpenBao snapshot failure, sync-job (off-site copy) failure or staleness beyond the expected interval, restore-drill overdue (a scheduled reminder alert if a quarterly/monthly drill has not been logged as completed).

## 14. Acceptance criteria

- [ ] PBS is deployed, every Proxmox VE node is registered as a client, and nightly VM/LXC backups run successfully into namespaced project directories.
- [ ] Retention policy (7 daily / 4 weekly / 6 monthly) is applied and PBS garbage collection correctly prunes old backups.
- [ ] SVC-01 `pg_dump` runs nightly and the resulting file is verified present in SVC-03 object storage.
- [ ] SVC-03 Garage bucket replication factor is confirmed at 3+ across independent zones/nodes.
- [ ] SVC-07 OpenBao Raft snapshot runs daily and is stored outside the OpenBao VM; unseal key shares are confirmed escrowed per the documented out-of-band procedure (verified by an actual test unseal from escrowed shares during the semiannual drill).
- [ ] A quarterly full-VM restore drill, a monthly PostgreSQL restore drill, and at least one semiannual OpenBao DR drill have been successfully executed and logged.
- [ ] A secondary/off-site copy of the PBS datastore (or its most critical contents) exists and its freshness/sync status is monitored.
- [ ] Terraform/Ansible pipeline runs idempotently with no drift on a second apply.

## 15. Open questions / assumptions

- Assumed a single physical site for the primary cluster with only an informal off-site copy strategy (second disk/NAS, occasional offline media, or a trusted remote location) — a human should confirm whether a second real site (e.g., a second office) is available to host a proper PBS sync-job target, which would materially improve DR posture over the assumed baseline.
- Assumed nightly RPO is acceptable for most services; any project needing tighter RPO (e.g., a production database with near-zero data-loss tolerance) should specify WAL-archiving/continuous replication in its own SVC-01 configuration rather than relying solely on this platform-wide nightly baseline.
- Assumed Garage's default replication factor of 3 is achievable with the current node count; if the cluster has fewer than 3 independent storage nodes/zones available, this must be revisited (replication factor cannot exceed available independent failure domains).
- The exact holders and physical locations for OpenBao unseal-key escrow are an organizational decision outside this document's scope and must be agreed upon and documented separately before go-live.
- Datastore disk sizing (2–3× used disk space) is a rule-of-thumb starting point; actual dedup ratio and retention growth should be monitored for the first 1–2 months and the datastore resized if needed.
