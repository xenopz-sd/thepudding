# Requirements Document Template — Developer Services (Proxmox)

Use this structure for every `SVC-xx-<slug>-requirements.md`. It is designed to be handed directly to KIRO for automated implementation, so every section should be concrete enough to generate Terraform, Ansible, and CI/CD pipeline code from.

1. **Overview** — one paragraph: what the service is, why it's part of the standard catalog, which project types depend on it (embedded/drone, ML-video, ML-text, generic backend).
2. **Scope** — explicit in-scope and out-of-scope bullets.
3. **Technology selection** — chosen product, license, origin/HQ (with the European-sourcing rationale from the catalog), and the alternative(s) considered and why they were not chosen as default.
4. **Multi-tenancy model** — shared platform instance vs. dedicated per-project instance (per catalog §1), and exactly how project isolation is implemented (schema/DB/realm/bucket/namespace naming convention).
5. **Functional requirements** — numbered list (FR-1, FR-2, ...) of what the deployed service must do/expose.
6. **Non-functional requirements** — availability target, performance/sizing guidance for a small Proxmox homelab/office cluster, backup/DR expectations, data retention.
7. **Infrastructure architecture** — Proxmox VM or LXC choice and why, minimum resource spec (vCPU/RAM/disk), network placement (VLAN, static IP/DHCP reservation), storage (local-zfs, NFS, etc.). **For any service placing one or more guests on a shared VLAN (VLAN 20, or any project VLAN that already hosts another Terraform root), the doc MUST state the service's host-index allocation and record it in the per-VLAN Host_Index_Authority ledger `infra/platform-foundation/host-indices.yaml` (ADR-0015) — each guest's `host_index` must be unique within its VLAN across independent roots, since `cidrhost("10.0.<vlan>.0/24", 10 + host_index)` collides otherwise. A new shared-VLAN guest without a ledger row is incomplete.**
8. **Terraform scope** — module inputs, resources created, outputs (to feed the Ansible dynamic inventory and other services).
9. **Ansible scope** — roles/playbooks, idempotency expectations, config files templated, secrets injected from SVC-07.
10. **CI/CD pipeline** — stages (lint → plan → manual/auto approve → apply → smoke test), where it runs (GitLab CI/GitHub Actions), state backend reference (GitLab-managed Terraform state per platform foundation decision F1).
11. **Secrets & credentials** — what secrets exist, where they're generated, how they're rotated, how they reach the service (SVC-07 OpenBao integration).
12. **Security & hardening baseline** — TLS everywhere via Traefik (SVC-09), least-privilege service accounts, firewall/VLAN rules, CVE scanning if applicable, auth via SVC-06 (ZITADEL) where the service has a UI/API.
13. **Observability hooks** — metrics endpoint/exporter for Prometheus (SVC-17), log format/shipping to Loki (SVC-18), key alerts to define in Alertmanager.
14. **Acceptance criteria** — a short checklist that defines "done" for KIRO's automated build. For any service with a `requires-infra` acceptance, the checklist MUST include: (a) a from-scratch live bring-up on the throwaway cluster under the `AGENT_LIVE_AUTHORIZED` + fail-closed throwaway-guard interlock (`testing-strategy.md` → "Agent Live-Test Authorization"); (b) a clean-slate reset that leaves `terraform state list` EMPTY — zero surviving guests AND zero non-guest `terraform_data` book-keeping resources — per the `service-installer.md` §4 verification gate (a reported-clean reset that leaves `terraform_data` residue is NOT done); and (c) distinct, non-colliding host IPs for every guest the service and its shared-VLAN peers place (verified against the host-index ledger, ADR-0015).
15. **Open questions / assumptions** — anything left for a human decision before KIRO starts.

Keep each document self-contained: a reader should not need the master catalog open to implement the service, though it should stay consistent with the catalog's tiering, tenancy, and technology decisions.
