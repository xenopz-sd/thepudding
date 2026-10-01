# SVC-11 — Private Mesh VPN Requirements

## 1. Overview

SVC-11 is the shared private mesh VPN, **NetBird**, already in use on this platform, that connects developer laptops, GitLab CI runners, and remote drone/edge hardware into a single flat, encrypted overlay network without requiring inbound firewall ports on any peer. It is the mechanism by which a developer's laptop reaches an internal-only Traefik-routed hostname, a remote drone reaches its telemetry ingestion endpoint, or a CI runner reaches a Proxmox-hosted service that has no public exposure at all. It is Tier 1 because nearly every other service's "how do I administer this without exposing it publicly" story routes through NetBird. It is especially load-bearing for the embedded/drone project type (remote hardware in the field joining the mesh over consumer internet connections) and for any project needing secure remote access to ML training/inference endpoints without a public IP.

## 2. Scope

**In scope:**
- One shared NetBird management/signal/relay deployment (self-hosted) serving all projects and all peer types (laptops, CI runners, drone/edge hardware).
- Per-project ACL groups controlling which peers can reach which internal services.
- Peer enrollment via setup keys (for automated/headless peers like CI runners and drone hardware) and via SSO login (for human developer laptops).
- Integration with SVC-06 (ZITADEL) for peer authentication where supported.
- NAT traversal (STUN/TURN-equivalent relay) so peers behind restrictive NATs (common for remote drone/edge hardware on cellular or consumer connections) can still connect.

**Out of scope:**
- Site-to-site routing to non-NetBird third-party VPNs (no such requirement currently).
- Replacing NetBird's own relay infrastructure with a custom TURN server — the self-hosted deployment's bundled relay is used as-is.
- Split-tunnel policy enforcement beyond NetBird's native ACL/routing groups (no separate NAC/ZTNA product layered on top).

## 3. Technology selection

**Chosen:** NetBird, self-hosted deployment — already in use on this platform. Origin: **NetBird GmbH, Berlin, Germany** ([NetBird GmbH imprint](https://netbird.io)) — genuinely European, no flag needed. Self-hosted deployment is based on WireGuard® under the hood, using the [NetBird self-hosted configuration reference](https://docs.netbird.io/selfhosted/maintenance/configuration-files), which documents the current Docker Compose topology: a combined `netbird-server` container (management + signal + relay + embedded STUN in one binary, image `netbirdio/netbird-server`) plus a `dashboard` container (`netbirdio/dashboard`) and, optionally, a bundled `traefik:v3.6` container for TLS termination if the platform's own shared Traefik (SVC-09) is not reused.

**Alternative considered:** since this is an already-in-use service, the decision was made previously; this document does not re-litigate it. For completeness: Tailscale (US, Tailscale Inc.) was the main commercial alternative previously passed over specifically because self-hosting its control plane (Headscale) is a community reimplementation, not the official product, whereas NetBird's self-hosted mode is first-party-supported and the vendor itself is European.

## 4. Multi-tenancy model

**Pattern:** Shared platform instance (per catalog §1) — one NetBird management server serves every project; there is no per-project VPN deployment.

**Isolation implementation:**
- Each project gets a dedicated NetBird **group** (e.g. `grp-acme-drone`, `grp-ml-vision-01`) that peers are assigned to at enrollment time via their setup key's default group.
- **ACL policies** are defined per group pair: by default, a project's group has bidirectional access only to that project's own service-hosting peers/subnets (its dedicated per-project database VM, its dedicated model-serving endpoint) and to the shared platform-core VLAN peers (Traefik, OpenBao, PowerDNS) that every project legitimately needs to reach. Cross-project group-to-group access is denied by default (implicit-deny ACL model).
- A `grp-platform-admins` group (human operators only, MFA-enforced via ZITADEL SSO) has broad access for troubleshooting across projects.
- Naming convention for setup keys mirrors the group name (`setupkey-<project-slug>-ci`, `setupkey-<project-slug>-edge`) so key provenance is traceable in the NetBird audit log.

## 5. Functional requirements

- **FR-1:** NetBird MUST support enrolling peers via ephemeral or reusable setup keys for headless devices (CI runners, drone/edge hardware) that cannot complete an interactive SSO login flow.
- **FR-2:** NetBird MUST support enrolling developer laptops via interactive SSO login, delegated to ZITADEL (SVC-06) as the identity provider, if NetBird's self-hosted IdP integration supports OIDC delegation to an external provider — otherwise falling back to NetBird's built-in user management for this peer type (see §15).
- **FR-3:** NetBird MUST assign every peer to at least one ACL group at enrollment time; ungrouped/default-group peers MUST NOT have implicit access to any project-scoped resource.
- **FR-4:** NetBird MUST provide NAT traversal (STUN, and TURN-equivalent relay when direct P2P fails) so that drone/edge hardware behind carrier-grade NAT or restrictive firewalls can still join the mesh, per the embedded STUN/relay bundled in the `netbird-server` image.
- **FR-5:** NetBird MUST expose its management API/dashboard on a stable internal hostname routed through Traefik (SVC-09), consistent with every other shared service.
- **FR-6:** NetBird MUST support route advertisement so that a peer (e.g. a Proxmox-hosted gateway) can expose an entire VLAN/subnet to the mesh, allowing remote drone hardware to reach platform-core services without every individual host running the NetBird client.
- **FR-7:** NetBird MUST log peer connect/disconnect events and ACL policy changes to support audit requirements.

## 6. Non-functional requirements

- **Availability target:** 99% during business hours; the management/signal plane being briefly unavailable does not immediately disconnect already-established WireGuard peer-to-peer tunnels (which persist independently once negotiated), so the practical impact of a short outage is "no new peers can join," not "existing connections drop."
- **Performance/sizing:** the combined `netbird-server` container is lightweight for a homelab-scale peer count (tens of peers, not thousands); actual data-plane throughput between peers is direct WireGuard P2P wherever NAT allows, so the server itself only needs to handle signaling/coordination traffic, not proxy the bulk data.
- **Backup/DR:** the NetBird management database (peer registry, ACL groups, setup keys) is backed up nightly to SVC-32; losing it means all peers must re-enroll, which is disruptive but not catastrophic (WireGuard keys are re-generated client-side on re-enrollment).
- **Data retention:** peer activity/audit logs retained 90 days in Loki (SVC-18).

## 7. Infrastructure architecture

- **Compute unit:** LXC container (per F2) — no GPU/kernel-isolation need.
- **Minimum resource spec:** 2 vCPU, 2 GB RAM, 20 GB disk (local-zfs) — sized for the management/signal/relay combined container plus its embedded database at homelab/office peer counts (dozens of peers); the [self-hosted configuration reference](https://docs.netbird.io/selfhosted/maintenance/configuration-files) documents `netbird-server` exposing ports 80 (internal, proxied) and `3478/udp` (STUN) externally.
- **Network placement:** platform-core VLAN, static IP reservation; port `3478/udp` (STUN) must be reachable directly from the internet (not proxied through Traefik, since it's UDP) to support NAT traversal for remote peers — this is the one exception to "every service sits behind Traefik," since STUN/relay traffic is inherently not HTTP.
- **Storage:** local-zfs for the management database and configuration files (`config.yaml` per the [self-hosted configuration reference](https://docs.netbird.io/selfhosted/maintenance/configuration-files)).

## 8. Terraform scope

- **Module inputs:** `vmid`, `hostname` (`svc11-netbird`), `vlan_tag`, `static_ip`, `cpu_cores`, `memory_mb`, `disk_gb`, `proxmox_node`, `public_stun_port` (3478/udp NAT/firewall rule input).
- **Resources created:** one `proxmox_virtual_environment_container` LXC resource (bpg/proxmox provider); a firewall rule opening `3478/udp` on the router/edge firewall; a GitLab-managed Terraform state entry keyed `svc-11-vpn-mesh`.
- **Outputs:** `netbird_management_url`, `netbird_internal_ip`, `netbird_stun_port` — consumed by Ansible for peer-enrollment automation on every other VM/LXC/CI runner/edge device provisioned across the platform.

## 9. Ansible scope

- **Roles:** `netbird_server_install` (deploys the Compose stack per the official self-hosted install flow), `netbird_group_provision` (creates a project's ACL group + default ACL policy given a `project_slug` variable), `netbird_peer_enroll` (installs the NetBird client on a target host/runner and joins it to the mesh using a setup key fetched from OpenBao).
- **Idempotency:** setup-key generation is checked against existing keys by name before creating a new one; ACL group/policy creation checks the NetBird API for an existing group with the same name before creating a duplicate.
- **Config files templated:** `config.yaml` (management server config, per the [self-hosted configuration reference](https://docs.netbird.io/selfhosted/maintenance/configuration-files)), `docker-compose.yml`.
- **Secrets injected from SVC-07:** setup keys and the management API's admin token are generated once, stored in OpenBao under `secret/data/platform/netbird/*`, and read by the `netbird_peer_enroll` role at runtime — never hardcoded in a playbook or committed to the repo.

## 10. CI/CD pipeline

- **Stages:** `lint` → `plan` → manual approval → `apply` → `smoke test` (a throwaway test peer enrolls using a freshly generated setup key, confirms it appears in the peer list via the API, then is deleted).
- **Runs on:** GitLab CI (SVC-13 self-hosted runners) — note the CI runners themselves are also NetBird peers (see §15 for the bootstrapping order dependency this creates), so the *initial* NetBird deployment pipeline must run before runners are enrolled, using a runner that is otherwise network-reachable to the Proxmox host directly.
- **State backend:** GitLab-managed Terraform state, per F1.

## 11. Secrets & credentials

- **What secrets exist:** setup keys (one or more per project, scoped to a default group), the NetBird management API admin token, OIDC client credentials if/when SSO delegation to ZITADEL is enabled.
- **Where generated:** setup keys are generated via the NetBird management API/dashboard at project-onboarding time; the admin token is generated once at initial deployment.
- **Rotation:** setup keys are revocable and reusable-with-expiry; reusable keys used for fleets of drone/edge hardware are rotated quarterly, ephemeral one-time keys are generated per-enrollment for CI runners so no long-lived key sits in a runner's filesystem.
- **How they reach the service:** fetched from OpenBao by the Ansible `netbird_peer_enroll` role at enrollment time; CI runners fetch their own ephemeral setup key via the same JWT/OIDC-authenticated OpenBao flow described in SVC-07 §11.

## 12. Security & hardening baseline

- TLS everywhere: the NetBird management API/dashboard is proxied through Traefik (SVC-09) with a valid TLS certificate; only the raw WireGuard/STUN UDP traffic bypasses Traefik by necessity.
- Least-privilege: default-deny ACL model — a peer with no explicit ACL grant reaches nothing beyond the management plane itself.
- Firewall/VLAN rules: only `3478/udp` is opened externally; the management API port is reachable only via Traefik's internal routing, not directly.
- CVE scanning: `netbirdio/netbird-server` and `netbirdio/dashboard` images are pulled through Harbor's (SVC-14) proxy-cache with Trivy scanning applied on ingestion.
- Auth via SVC-06: administrative dashboard access is gated by ZITADEL SSO where NetBird's self-hosted IdP delegation supports it (see open question in §15); at minimum, the dashboard sits behind the same Traefik forward-auth pattern used for other admin UIs (SVC-09 §12) as a baseline control even if native OIDC delegation is not yet wired up.

## 13. Observability hooks

- NetBird's management server exposes operational metrics (peer count, connection state) that are scraped by SVC-17/Prometheus where an exporter/metrics endpoint is available in the self-hosted build; if no native Prometheus endpoint exists in the deployed version, a periodic API-polling exporter script is used as a stopgap.
- Peer connect/disconnect and ACL change events are shipped to SVC-18/Loki via the management server's audit log output.
- Key alerts to define in Alertmanager: `netbird_management_down` (no new peers can enroll), `netbird_peer_count_drop` (a sudden drop in connected peers, suggesting a network partition or relay failure), `netbird_stun_port_unreachable` (external NAT traversal broken — most impactful for remote drone/edge hardware).

## 14. Acceptance criteria

- [ ] NetBird management/signal/relay LXC provisioned via Terraform; dashboard reachable via Traefik.
- [ ] A test CI runner peer enrolls automatically via a setup key fetched from OpenBao, with zero manual token copy-paste.
- [ ] A test "drone" peer (simulated behind NAT) successfully connects via relay/STUN when direct P2P is blocked.
- [ ] Per-project ACL group created and verified to deny cross-project access in a manual test (peer in `grp-acme-drone` cannot reach a `grp-ml-vision-01`-only resource).
- [ ] Route advertisement verified: a peer can reach an entire advertised subnet without installing the client on every host in that subnet.
- [ ] Nightly backup of the NetBird management database present in Proxmox Backup Server.

## 15. Open questions / assumptions

- **Open question:** does the currently-deployed NetBird self-hosted version support delegating its own login to an external OIDC provider (ZITADEL) for human peer enrollment, or does it require using NetBird's bundled Zitadel-based IdP (NetBird's official self-hosted quick-start historically ships with its own embedded IdP)? This needs a version-specific check against the current [NetBird self-hosted docs](https://docs.netbird.io/selfhosted/maintenance/configuration-files) before FR-2 can be locked in as "delegated SSO" vs. "NetBird-native accounts synced from ZITADEL."
- **Bootstrapping order dependency:** since GitLab CI runners are themselves NetBird peers, the very first NetBird deployment and the first runner registration have a chicken-and-egg ordering problem — assumed solved by running the initial Terraform/Ansible apply for SVC-11 from a runner or admin workstation with direct Proxmox network access, before any runner needs to be a NetBird peer for a *different* reason.
- Assumes a single NetBird management instance is acceptable for now; HA for the management plane is deferred (data-plane WireGuard tunnels between already-connected peers are unaffected by management-plane downtime, so urgency is lower than for Traefik or OpenBao).
- Whether project-scoped route advertisement (FR-6) is used per-project or only for the shared platform-core subnet is left open pending SVC-01/SVC-03 dedicated-instance networking design.
