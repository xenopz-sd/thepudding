# SVC-28 — OTA/Firmware Update Server Requirements

## 1. Overview

This service provides over-the-air (OTA) software and firmware update distribution for embedded Linux device fleets — Raspberry Pi boards, NXP-based boards, and drone flight computers. It is a Tier 1 core service for every embedded/Raspberry Pi/NXP and drone-hardware project: without it, updating deployed devices requires physical access, which does not scale past a handful of units and is operationally unacceptable for a fielded drone fleet. ML-video/ML-text and generic backend projects typically do not need this service unless they include an embedded edge component.

## 2. Scope

**In scope:**
- Mender server (default) for turnkey fleet management with A/B (dual rootfs) atomic updates.
- Eclipse hawkBit + RAUC/SWUpdate clients as the alternative for projects needing custom partition layouts that don't fit Mender's opinionated A/B model.
- Device provisioning/enrollment workflow.
- Deployment/rollout grouping and staged rollout strategy.
- Update artifact signing and signing-key management via SVC-07 (OpenBao) and SVC-08 (EJBCA).

**Out of scope:**
- Application-level (container/Compose) update mechanisms for server-side services — this doc is scoped to embedded device firmware/OS updates, not the platform's own Docker Compose stacks (those are handled by the CI/CD pipeline per service).
- Device inventory/fleet management beyond what's needed for update targeting (broader inventory concerns are SVC-31).
- Network boot / initial OS imaging (SVC-29, iPXE) — this service updates already-provisioned devices, not first-boot imaging.

## 3. Technology selection

- **Mender** (Apache-2.0 client, commercial-friendly self-hosted server edition) — default OTA server. **Northern.tech AS**, Oslo, Norway ([Northern.tech / Mender — Oslo office](https://northern.tech/careers/oslo/versatile-engineer-embedded-and-backend)) — fully European origin and governance.
- **Eclipse hawkBit** (EPL-2.0) + **RAUC** or **SWUpdate** clients — alternative for projects with custom partition layouts that don't map cleanly onto Mender's built-in A/B rootfs assumption. Governed by the **Eclipse Foundation**, Belgium; originated at **Bosch**, Germany ([Eclipse hawkBit](https://projects.eclipse.org/projects/iot.hawkbit)) — also fully European on both governance and origin.
- Mender is the default because it ships a complete, opinionated, turnkey fleet-management experience (server + client + A/B update logic) out of the box, minimizing integration work for the common case (standard Raspberry Pi/NXP Yocto or Debian-based images). hawkBit is reserved for projects whose board/bootloader setup requires a bespoke partition scheme (e.g., a drone flight computer with a non-standard eMMC layout) where RAUC's or SWUpdate's more flexible, DIY update-flow model is a better fit than Mender's fixed A/B convention.
- This selection and its European-sourcing rationale are carried directly from catalog §3.H (SVC-28) and are not re-litigated here.

## 4. Multi-tenancy model

**Shared Mender server** serves all projects, consistent with catalog §1's guidance for services that benefit from central reuse (device fleet management is a natural single-source-of-truth service). Isolation is implemented via Mender's native multi-group model:
- **Device groups:** every device is tagged at enrollment time with a project-scoped group name (`<slug>-devices`), used to scope which devices a project's operators can see and manage in the Mender UI/API.
- **Deployment groups:** rollouts are targeted at a project's device group only; a deployment created for `drone-alpha-devices` cannot accidentally target `drone-beta-devices`.
- **RBAC:** Mender's role-based access control maps project-scoped roles (via ZITADEL SSO integration, see §12) so a project's engineers can only create deployments and view devices within their own device group.
- A project with a genuinely custom partition layout uses the hawkBit alternative, deployed as its own dedicated instance (hawkBit does not have the same natural "one shared server" pull as Mender since it's already the exception path) — this is the one case where "dedicated per project" is the natural default rather than an opt-in choice.

## 5. Functional requirements

- **FR-1:** Mender server ingests signed update artifacts (`.mender` files) and stores them, associating each with a target device type and software version.
- **FR-2:** Mender server exposes a device management UI and REST API for creating deployments, viewing device inventory, and monitoring rollout status.
- **FR-3:** Devices authenticate to the Mender server using a device-specific identity (public key generated on first boot, or a pre-provisioned key/cert) and are placed into a "pending" state requiring operator (or automated policy) acceptance before receiving updates.
- **FR-4:** Updates are delivered via Mender's **A/B (dual rootfs) atomic update mechanism**: the device downloads the new image to the inactive partition, verifies its signature, reboots into it, and only commits (marks it as the new active partition) after a configurable health-check/commit window passes — an automatic rollback to the previous partition occurs if the device fails to check in as healthy within that window.
- **FR-5:** Deployments support staged/phased rollout: an operator can target a percentage of a device group first (canary), monitor success rate, then expand to 100%.
- **FR-6:** hawkBit-based projects use RAUC or SWUpdate clients implementing the same signed-artifact-plus-verification flow, adapted to the project's specific partition/bootloader scheme; hawkBit's rollout/deployment-group concepts map 1:1 onto the same "device group / deployment group" tenancy pattern as Mender.
- **FR-7:** All update artifacts are cryptographically signed before upload to the server; devices refuse to install an artifact whose signature does not verify against the trusted public key baked into the device's bootloader/client config.
- **FR-8:** The server exposes deployment status webhooks/API so a project's own dashboard or CI pipeline can poll or subscribe to rollout progress (e.g., to gate a broader release on canary success).
- **FR-9:** Device provisioning/enrollment integrates with SVC-08 (EJBCA): each device is issued a client certificate at manufacture/first-boot time, used both for Mender/hawkBit device authentication and for MQTT (SVC-21) broker authentication, so a device has one certificate identity across both channels where feasible.

## 6. Non-functional requirements

- **Availability target:** 99% for the shared Mender server; a temporary server outage does not brick already-provisioned devices (they simply retry their next scheduled poll), so this is not a hard real-time dependency, but sustained downtime blocks fleet updates and security patching.
- **Performance/sizing** (small homelab/office cluster, tens to a few hundred embedded/drone devices, infrequent update cadence — not continuous streaming):
  - Mender server (all services: API gateway, device auth, inventory, deployments, deviceconfig, useradm, workflows, plus a Postgres backing DB): 2 vCPU, 4 GB RAM, 100 GB disk (artifact storage dominates; each `.mender` image artifact for a typical embedded Linux image is commonly several hundred MB to a few GB, so disk should scale with expected artifact retention — see below).
  - hawkBit (Java/Spring Boot monolith or microservices split): 2 vCPU, 4 GB RAM, 50 GB disk for a modest device count at this scale.
  - Artifact storage: update images are stored either on local-zfs or proxied to SVC-03 (Garage/S3-compatible) for larger artifact retention — recommended for this cluster given Garage is already the shared object-storage service.
- **Backup/DR:** Mender's Postgres backing database and artifact storage are covered by SVC-32 (VM-level snapshot) plus the application-level Postgres backup pattern from SVC-01 if Mender's DB is hosted there; artifacts themselves are also recoverable by re-signing/re-uploading from CI build artifacts if lost, so the object store is not the sole copy of record.
- **Data retention:** the last 3–5 artifact versions per device type are retained for rollback purposes; older artifacts are pruned on a retention policy to control storage growth, with the CI pipeline's own artifact registry (Harbor/Git LFS) serving as long-term cold storage if needed.

## 7. Infrastructure architecture

- **Compute unit:** Proxmox **VM** for the Mender server stack (its microservices architecture and backing Postgres benefit from predictable resource isolation; also simplifies future horizontal scaling if device count grows) per F2. hawkBit, when used, also runs as a VM for the same reason.
- **Minimum resource spec:** 2 vCPU, 4 GB RAM, 100 GB disk (local-zfs, or NFS-backed if artifact storage needs to grow beyond local disk).
- **Network placement:** dedicated "infra-mgmt" VLAN for the server itself (management/CI access); devices reach the server's public update-check API through Traefik (SVC-09) on the "iot-devices" VLAN boundary, so devices never need direct network access to the management VLAN.
- **Storage:** local-zfs for the Postgres DB and Mender's own service state; artifact blobs proxied to SVC-03 (Garage) object storage to avoid unbounded local disk growth as firmware image sizes and version history accumulate.

## 8. Terraform scope

- **Module inputs:** `vm_name`, `vlan_id` (infra-mgmt), `cpu_cores` (2), `memory_mb` (4096), `disk_gb` (100), `artifact_backend` (`local` | `s3`), `s3_endpoint` (SVC-03 endpoint if `s3` selected).
- **Resources created:** one `proxmox_virtual_environment_vm` for the Mender (or hawkBit) server stack; firewall rules exposing the device-facing API only through Traefik, and the admin UI/API only to the internal management VLAN.
- **Outputs:** `ota_server_vm_ip`, `ota_device_api_url` (external, via Traefik), `ota_admin_url` (internal only), consumed by device-provisioning tooling (SVC-31) and by each embedded project's build pipeline (to know where to push signed artifacts).

## 9. Ansible scope

- **Roles:** `mender_server` (renders the multi-container Compose stack: `mender-api-gateway`, `mender-device-auth`, `mender-inventory`, `mender-deployments`, `mender-useradm`, `mender-workflows-server`, backing Postgres, plus `mender-gui`), `hawkbit_server` (for projects using the alternative), `ota_device_group_provisioning` (idempotently creates/updates per-project device and deployment groups without disturbing other projects' groups on the shared server).
- **Idempotency:** device-group and deployment-group creation calls check for existing resources via the Mender/hawkBit REST API before creating, so re-running the playbook is a no-op when nothing changed.
- **Config files templated:** `docker-compose.yml` for the Mender stack, `ALLOWED_HOSTS` and TLS cert paths for the API gateway, S3 backend configuration for artifact storage (endpoint, bucket, access key reference).
- **Secrets injected from SVC-07:** Mender server TLS certificate/key (from SVC-08), Postgres credentials, S3 access key/secret for SVC-03 artifact storage, artifact-signing private key (see §11).

## 10. CI/CD pipeline

- **Stages:** `lint` (Compose/YAML lint) → `plan` (Terraform plan) → `manual/auto approve` → `apply` → `configure` (Ansible) → `smoke test` (create a test device group, upload a dummy signed artifact, verify it appears in the artifact list via the API).
- **Per-project firmware release pipeline** (separate from the platform pipeline above, runs in each embedded project's own repo): `build` (cross-compile firmware/OS image) → `sign` (sign the `.mender` or RAUC bundle using the project's signing key from OpenBao) → `upload` (push the signed artifact to the shared Mender/hawkBit server via its API) → `deploy` (create a staged deployment targeting a canary subset of the project's device group) → `monitor` (poll rollout status) → `promote` (expand to 100% on canary success, manual or automated gate).
- **Where it runs:** GitLab CI via GitLab Runner (SVC-13).
- **State backend:** GitLab-managed Terraform state (F1) for the server infrastructure; the per-project firmware pipeline does not manage Terraform state (it only calls the OTA server's API).

## 11. Secrets & credentials

- **Artifact-signing key pair:** generated once per project (or once platform-wide if projects share a trust root), private key stored in OpenBao (SVC-07), public key distributed to devices at provisioning time (baked into the bootloader/client trust store) and to the OTA server for verification. Rotation requires a coordinated re-provisioning of the public key on all fielded devices, so rotation cadence is deliberately long (annual, or on suspected key compromise) with a documented key-rotation runbook.
- **Device identity certificates:** issued by SVC-08 (EJBCA) at manufacture/first-boot enrollment, used for device authentication to the Mender/hawkBit API; revocation follows the same CRL mechanism described in the SVC-21 (MQTT) requirements doc, since these are often the same device certificate.
- **Mender server admin credentials:** stored in OpenBao, SSO-federated via ZITADEL for day-to-day operator access (see §12) rather than relying on local passwords.
- **S3 (SVC-03) access key/secret** for artifact storage: stored in OpenBao, scoped to a dedicated artifact bucket.

## 12. Security & hardening baseline

- All external device check-ins and admin UI access go through Traefik (SVC-09) with TLS; the device-facing API additionally requires a valid device certificate (mTLS) or Mender's device auth token issued after enrollment approval.
- Mender admin UI authentication is federated to ZITADEL (SVC-06) via SSO/OIDC where Mender's edition supports it; otherwise local admin accounts are minimized and rotated.
- Least-privilege: device accounts can only report status and download artifacts targeted at their own device group; they cannot enumerate or access other projects' artifacts or device inventories.
- Firewall/VLAN rules: only the iot-devices VLAN (via Traefik) reaches the device-facing API; the admin UI and direct service ports are restricted to the infra-mgmt VLAN.
- Signed-artifact verification is mandatory and non-optional on the device client — an unsigned or badly-signed artifact must be rejected before flashing to the inactive partition.
- CVE scanning: Mender/hawkBit container images pinned to specific version tags, rescanned monthly via Harbor/Trivy (SVC-14).

## 13. Observability hooks

- Mender server exposes internal service metrics (via its own Prometheus-compatible endpoints where available on newer Mender server versions) scraped by SVC-17; where a metrics endpoint isn't natively available, deployment success/failure counts are polled via the Mender API by a small Prometheus exporter script and pushed via a pushgateway.
- hawkBit exposes a Spring Boot Actuator `/actuator/prometheus` endpoint scraped by SVC-17.
- Server and device-enrollment logs are shipped via Promtail to Loki (SVC-18) with `service=mender` (or `hawkbit`) and `project` labels.
- Key alerts to define in Alertmanager: OTA server down, deployment failure rate above threshold for a given rollout, device check-in rate anomaly (a sudden drop suggesting a fleet-wide connectivity or provisioning issue), artifact-signing key nearing its planned rotation date.

## 14. Acceptance criteria

- [ ] Mender (or hawkBit, for the alternative path) server is deployed and its admin UI is reachable via Traefik with TLS.
- [ ] A test device can enroll, be accepted into a project-scoped device group, and receive a signed test artifact via the A/B update flow, including a successful automatic rollback when the artifact is intentionally made to fail its post-update health check.
- [ ] Deployment can be scoped to a canary subset of a device group and later expanded, with rollout status visible via the API/UI.
- [ ] Artifact signature verification is confirmed to reject a tampered or unsigned artifact.
- [ ] Device certificates issued by SVC-08 are accepted for device authentication end-to-end.
- [ ] Terraform/Ansible pipeline runs idempotently with no drift on a second apply.

## 15. Open questions / assumptions

- Assumed the shared Mender server is the default for all embedded/drone projects at launch; the first project with a genuinely non-standard partition layout should trigger provisioning of the hawkBit alternative rather than trying to force-fit Mender.
- Assumed a shared signing trust root across projects is acceptable for the initial rollout; a human should confirm whether per-project signing keys (fully isolated trust roots) are required for regulatory or customer-contractual reasons, especially for drone hardware.
- Exact artifact-retention count (3–5 versions assumed) and S3 vs. local storage trade-off should be revisited once real firmware image sizes and update cadence are known.
- Automatic vs. manual canary-to-100% promotion gating is left as a per-project CI pipeline decision, not mandated platform-wide.
