# SVC-21 — MQTT Broker Requirements

## 1. Overview

This service provides the MQTT publish/subscribe broker used for embedded-device and drone telemetry ingestion and command-and-control channels. It is a Tier 1 core service required by every embedded/Raspberry Pi/NXP and drone-hardware project — sensor telemetry, heartbeat/liveness, firmware-update triggers (feeding into SVC-28), and low-latency command dispatch to flight controllers all flow through it. Generic backend and ML projects typically do not need this service directly, though ML-video projects with edge camera nodes may use it for lightweight event signaling.

## 2. Scope

**In scope:**
- Mosquitto broker deployment (default) for embedded/drone telemetry and command topics.
- VerneMQ as the clustered-scale alternative for projects that outgrow a single Mosquitto instance.
- TLS and mutual TLS (mTLS) client authentication using SVC-08 (EJBCA)-issued device certificates.
- Per-project topic namespace convention and ACL/credential model.
- Bridge/relay configuration for forwarding telemetry into SVC-04 (QuestDB) or SVC-01 (Postgres/Timescale) for storage, where applicable.

**Out of scope:**
- General-purpose enterprise message queuing/event bus with complex routing, exchanges, or exactly-once delivery guarantees — that is SVC-22 (RabbitMQ).
- MQTT-to-HTTP bridging/webhooks (handled per-project if needed, not a platform default).
- Device provisioning/enrollment workflow itself (covered by SVC-08 for certs and SVC-31 for inventory) — this doc only covers how the broker *consumes* those certs for authentication.

## 3. Technology selection

- **Mosquitto** (EPL-2.0/EDL-1.0 dual license) — default MQTT broker. Governed by the **Eclipse Foundation AISBL**, Brussels, Belgium; day-to-day development is driven by **Cedalo GmbH**, Freiburg, Germany ([Eclipse Mosquitto](https://mosquitto.org), [Cedalo — About](https://www.cedalo.com/about)) — fully European on both the governance and primary-contributor axis.
- **VerneMQ** (Apache-2.0) — clustered-scale alternative, used only for projects whose device count or message throughput exceeds what a single Mosquitto instance can comfortably serve. Steward is **Octavo Labs AG**, Switzerland, successor to Erlio GmbH, Germany ([VerneMQ / Octavo Labs](https://vernemq.com)) — also fully European. VerneMQ is not the default because most embedded/drone projects at this scale (tens to low hundreds of devices) do not need Erlang/OTP clustering, and Mosquitto's operational simplicity (single binary, single config file) outweighs VerneMQ's horizontal scalability for the common case.
- This choice and its European-sourcing rationale are carried directly from catalog §3.F (SVC-21) and are not re-litigated here.

## 4. Multi-tenancy model

Two supported patterns, chosen per project based on isolation needs:

- **Shared broker (default):** one Mosquitto instance serves multiple projects. Isolation is enforced via:
  - **Topic namespace convention:** every topic is prefixed `proj/<slug>/...`, e.g. `proj/drone-alpha/telemetry/gps`, `proj/drone-alpha/cmd/arm`. No project may publish or subscribe outside its own prefix.
  - **Per-project ACL:** Mosquitto's `acl_file` (or dynamic-security plugin) grants each project's credential/certificate read/write access only to `proj/<slug>/#`. A platform-level monitoring credential may have read-only access to `proj/+/telemetry/#` for cross-project dashboards.
  - **Per-project credentials or certs:** each project is issued its own client certificate CN (`proj-<slug>-device-*`) or username/password pair, mapped to its ACL entry.
- **Dedicated broker instance:** a project needing full isolation (e.g., a safety-critical drone program with regulatory segregation requirements, or a device fleet large enough to risk noisy-neighbor effects) gets its own Mosquitto (or VerneMQ, if scale demands it) Compose stack, torn down with the project per catalog §1.
- The default assumption for a new project is the shared broker; a project's own requirements doc must explicitly request dedicated instance if isolation trumps reuse.

## 5. Functional requirements

- **FR-1:** Broker accepts MQTT 3.1.1 and MQTT 5.0 connections on port 1883 (plaintext, internal VLAN only) and port 8883 (TLS, the only port reachable from device/drone networks).
- **FR-2:** Broker supports MQTT-over-WebSocket on port 9001 for browser-based dashboards/debug tooling, TLS-wrapped when exposed via Traefik.
- **FR-3:** Client authentication is mTLS by default for embedded/drone devices: the broker validates client certificates against the SVC-08 (EJBCA) internal CA chain, using the certificate's CN as the device identity.
- **FR-4:** Username/password authentication (via Mosquitto's password file or dynamic-security plugin) is available as a fallback for non-cert-capable clients (e.g., quick scripts, third-party integrations), scoped to the same per-project ACL model.
- **FR-5:** Per-project ACLs enforce publish/subscribe restriction to `proj/<slug>/#` as described in §4.
- **FR-6:** Broker persists retained messages and QoS 1/2 in-flight state to local disk (`persistence true`) so that a broker restart does not silently drop undelivered messages to offline devices.
- **FR-7:** Broker exposes a `$SYS/#` topic tree and a Prometheus-compatible metrics endpoint (via the `mosquitto-exporter` sidecar, since core Mosquitto does not natively expose `/metrics`) for connection counts, message throughput, and dropped-message counters.
- **FR-8:** A bridge configuration optionally forwards specific topics (e.g., `proj/<slug>/telemetry/#`) to a per-project QuestDB (SVC-04) or Postgres/Timescale (SVC-01) ingestion pipeline for long-term storage and querying.
- **FR-9:** Certificate revocation (device decommissioned/compromised) is enforced via CRL or OCSP checking against SVC-08, with a documented maximum propagation delay (target: 15 minutes via CRL refresh interval).

## 6. Non-functional requirements

- **Availability target:** 99.5% for the shared broker (command channels for active drone operations are latency- and availability-sensitive; a dedicated-instance project doing live flight operations should consider active-active VerneMQ clustering instead).
- **Performance/sizing** (small homelab/office cluster, tens to ~200 concurrent embedded/drone clients, modest telemetry rate — e.g., 1–10 Hz per device):
  - Mosquitto: 1 vCPU, 512 MB–1 GB RAM, 20 GB disk (mostly for persistence/logs) is comfortable at this scale; Mosquitto's own footprint is lightweight (tens of MB RSS under load).
  - `mosquitto-exporter` sidecar: negligible (0.1 vCPU, 64 MB RAM).
  - If VerneMQ clustering is adopted for a high-scale project: minimum 3-node cluster, 2 vCPU / 2 GB RAM per node.
- **Backup/DR:** broker config (`mosquitto.conf`, ACL file, password file) and persistence DB are covered by SVC-32 VM/LXC-level snapshot backup; retained messages are not considered durable long-term data (telemetry of record lives in QuestDB/Postgres per FR-8, not in the broker).
- **Data retention:** the broker itself is not a data store — message retention is limited to in-flight QoS state and explicitly retained-flag messages; durable telemetry history is the responsibility of the downstream time-series/relational store.

## 7. Infrastructure architecture

- **Compute unit:** Proxmox **LXC** container (lightweight Linux service, no GPU/kernel-isolation need, per F2) running Mosquitto in a single Docker Compose stack alongside its exporter sidecar.
- **Minimum resource spec:** 1 vCPU, 1 GB RAM, 20 GB disk (local-zfs).
- **Network placement:** dedicated "iot-devices" VLAN reachable by drone/embedded hardware over Wi-Fi/LTE/wired backhaul as applicable; static IP/DHCP reservation for the broker so device firmware can hardcode or DNS-resolve (via SVC-10 PowerDNS) a stable broker address. Port 8883 (mTLS) is the only port reachable from the device VLAN; port 1883 is restricted to the internal/management VLAN for debugging.
- **Storage:** local-zfs for persistence DB and logs; no shared/NFS storage needed at this scale.

## 8. Terraform scope

- **Module inputs:** `lxc_hostname`, `vlan_id` (iot-devices), `cpu_cores` (1), `memory_mb` (1024), `disk_gb` (20), `ip_address`, `tenancy_mode` (`shared` | `dedicated`), `project_slug` (when dedicated).
- **Resources created:** one `proxmox_virtual_environment_container` (LXC, via `bpg/proxmox`) per broker instance; firewall rules opening 8883 to the iot-devices VLAN and 1883/9001 to the internal VLAN only.
- **Outputs:** `mqtt_broker_ip`, `mqtt_tls_port` (8883), `mqtt_internal_port` (1883), consumed by device-provisioning tooling (SVC-31) and by each project's Ansible inventory for topic/ACL configuration.

## 9. Ansible scope

- **Roles:** `mosquitto_broker` (installs Docker Compose stack, templates `mosquitto.conf`, ACL file, TLS cert paths), `mosquitto_project_acl` (idempotently appends/updates a project's ACL block and per-device credential entries without disturbing other projects' entries on a shared broker).
- **Idempotency:** ACL file and password file are rendered from a Jinja2 template driven by an Ansible variable listing all active project topic prefixes and device credentials; reapplying produces no diff if nothing changed. Broker reload (`mosquitto -c ... -HUP` or container restart) only triggers on an actual config-hash change.
- **Config files templated:** `mosquitto.conf` (listeners, TLS cert/key paths, persistence settings, bridge definitions), `acl.conf`, `passwd` (or `dynamic-security.json` if the dynamic-security plugin is used instead of static files).
- **Secrets injected from SVC-07:** broker TLS server certificate/key (issued by SVC-08, stored in OpenBao, pulled at Ansible run time), per-project username/password credentials for the fallback auth path, CRL file for revocation checking.

## 10. CI/CD pipeline

- **Stages:** `lint` (`mosquitto -c mosquitto.conf -t` config test in a throwaway container) → `plan` (Terraform plan) → `manual/auto approve` → `apply` → `configure` (Ansible) → `smoke test` (mosquitto_pub/mosquitto_sub round-trip against a test topic over mTLS, verify ACL denial on an out-of-namespace topic).
- **Where it runs:** GitLab CI via GitLab Runner (SVC-13).
- **State backend:** GitLab-managed Terraform state (F1), scoped per broker instance (one state per shared broker, or per dedicated-instance project).

## 11. Secrets & credentials

- **Broker server TLS cert/key:** issued by SVC-08 (EJBCA), renewed automatically before expiry (30-day-before-expiry renewal window), stored in OpenBao, injected into the Compose stack at deploy time.
- **Device client certs:** issued per-device by SVC-08 during provisioning/enrollment (see SVC-28/SVC-31 for the enrollment flow); the broker only needs the CA chain to validate them, not the private keys themselves.
- **Per-project fallback username/password credentials:** generated by Ansible (random, 32-byte), stored in OpenBao at `secret/mqtt/<slug>/credentials`, rotated on a 180-day cadence or on suspected compromise.
- **CRL:** published by SVC-08, fetched by the broker on a scheduled interval (target: hourly) and reloaded without a full broker restart.

## 12. Security & hardening baseline

- **mTLS is the primary and required authentication mechanism for drone/embedded devices** — username/password is a fallback only, not the default, given the higher assurance needs of command-and-control channels reaching physical hardware.
- TLS 1.2+ enforced on port 8883; weak ciphers disabled in `mosquitto.conf`.
- WebSocket listener (9001), if enabled, is reverse-proxied through Traefik (SVC-09) for TLS termination and is not directly internet-facing.
- Firewall/VLAN rules: device VLAN can reach only port 8883 on the broker; management/debug access (1883, 9001, metrics) restricted to the internal VLAN.
- Least-privilege: the Mosquitto container runs as the non-root `mosquitto` user (default in the `eclipse-mosquitto` official image); no host network mode.
- ACLs default-deny: any topic not explicitly granted to a project/device is denied by default (`acl_file` uses an implicit deny-all fallthrough).
- CVE scanning: `eclipse-mosquitto` image pinned to a specific version tag (not `:latest`), rescanned monthly via Harbor/Trivy (SVC-14).

## 13. Observability hooks

- `mosquitto-exporter` sidecar exposes broker metrics (`mosquitto_connected_clients`, `mosquitto_messages_received_total`, `mosquitto_messages_dropped_total`, `mosquitto_bytes_received_total`) scraped by Prometheus (SVC-17) on a `/metrics` endpoint (default port 9234 for common `mosquitto-exporter` builds — confirm exact port against the chosen exporter image at implementation time).
- Broker logs (connection/disconnection, auth failures, ACL denials) are shipped via Promtail to Loki (SVC-18) with `project` and `service=mosquitto` labels.
- Key alerts to define in Alertmanager: broker down (`up == 0`), client connection count anomaly (sudden drop suggesting a device fleet outage), message drop rate >0 sustained, TLS cert expiry <14 days, repeated ACL-denial spikes (possible misconfigured or compromised device).

## 14. Acceptance criteria

- [ ] Mosquitto broker is deployed and reachable on 8883 (mTLS) from the iot-devices VLAN and on 1883/9001 only from the internal VLAN.
- [ ] A test device certificate issued by SVC-08 can successfully connect via mTLS and publish/subscribe within its project's topic namespace.
- [ ] Publishing or subscribing outside the project's `proj/<slug>/#` namespace is denied by ACL.
- [ ] Broker metrics appear in Prometheus and a basic Grafana dashboard (connected clients, message rate) is provisioned.
- [ ] Certificate revocation via SVC-08 CRL results in a revoked device being unable to reconnect within the documented propagation window.
- [ ] Terraform/Ansible pipeline runs idempotently with no drift on a second apply.

## 15. Open questions / assumptions

- Assumed shared broker is the default for new projects; a human should confirm per-project during onboarding whether dedicated-instance isolation is required (e.g., for a regulated drone program).
- Assumed CRL-based revocation checking (15-minute propagation target) is acceptable; OCSP stapling would reduce this but adds operational complexity not yet justified at this device count.
- Exact `mosquitto-exporter` image/port to be confirmed at implementation time (several community exporters exist with differing metric names/ports).
- Threshold for migrating a project from shared Mosquitto to dedicated VerneMQ clustering (device count / message rate) is not yet formally defined — should be set once real telemetry volume from the first drone project is observed.
