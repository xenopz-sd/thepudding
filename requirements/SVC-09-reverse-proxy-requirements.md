# SVC-09 — Reverse Proxy / TLS Termination Requirements

## 1. Overview

SVC-09 is the shared edge layer that every other service in this catalog sits behind: **Traefik**, already in production use on this platform. It terminates TLS, performs automatic HTTPS certificate issuance/renewal, and routes inbound HTTP(S) traffic to the correct backend Docker container purely from container labels — no manual vhost files to maintain. Because every shared and dedicated service (SVC-01 through SVC-32 wherever they expose an HTTP UI/API) is reverse-proxied by Traefik, it is a Tier 1 platform-foundation dependency: nothing gets a public/internal hostname without it. All project types (embedded/drone, ML-video, ML-text, generic backend) rely on it indirectly the moment any of their dedicated per-project services (a Postgres admin UI, a model-serving endpoint, a project dashboard) needs a stable, TLS-protected URL.

## 2. Scope

**In scope:**
- One shared Traefik instance (or a small HA pair) acting as the single ingress point for all Docker Compose stacks on the platform.
- Automatic HTTPS via Let's Encrypt (public-facing services) and via the internal CA (SVC-08/EJBCA) for internal-only services.
- Docker provider auto-discovery via container labels (`traefik.enable=true`, routing rules).
- A per-project subdomain routing convention.
- Central dashboard access (admin-only, SSO-gated via SVC-06).

**Out of scope:**
- Layer-4/TCP load balancing for non-HTTP protocols that don't fit Traefik's TCP router model well (e.g., raw PostgreSQL wire protocol is NOT proxied through Traefik — SVC-01 instances are reached directly on their VLAN or via NetBird).
- WAF/DDoS mitigation beyond Traefik's basic rate-limit middleware — no dedicated WAF appliance in scope for a homelab/office cluster.
- API gateway features like request transformation, quota enforcement per API key — that is SVC-12 (Tyk), a separate specialized (T3) service.

## 3. Technology selection

**Chosen:** Traefik ([traefik:v3.6](https://hub.docker.com/_/traefik), the official Docker Hub image; current stable line is v3.7 as of this writing per [Traefik's official Docker Hub tags](https://hub.docker.com/_/traefik)) — already in use on this platform, so this document formalizes and extends the existing deployment rather than proposing a new one. License: MIT. Origin: **Traefik Labs, Lyon, France** ([Traefik Labs company registry, Pappers.fr](https://www.pappers.fr)) — genuinely European, satisfying the catalog's European-by-default principle without any flag needed.

**Alternative considered:** nginx / nginx-proxy-manager and Caddy were the main alternatives for label-driven Docker reverse proxying. Both were passed over for the *default* role specifically because Traefik is already deployed and working — re-litigating the reverse-proxy choice would mean re-labeling every existing Compose stack for no operational gain. Caddy (French-adjacent community, but Ardan Labs/US-linked core team) and nginx (F5 Inc., US) were also weaker fits on European governance grounds relative to Traefik Labs' clear Lyon HQ.

## 4. Multi-tenancy model

**Pattern:** Shared platform instance (per catalog §1) — a single Traefik deployment fronts every project's services; there is no per-project Traefik instance.

**Isolation implementation:** routing isolation, not process isolation. Each project's services are distinguished purely by hostname/path convention:
- **Subdomain convention (preferred):** `<service>.<project-slug>.svc.internal.example` for internal-only services and `<service>-<project-slug>.example.com` for anything Let's Encrypt-issued publicly (flat public DNS namespace keeps the Let's Encrypt certificate count manageable via a wildcard cert per zone rather than per-service).
- **Path-based convention (fallback, used only where subdomain delegation is impractical, e.g. quick internal dashboards):** `internal.example.com/<project-slug>/<service>/`.
- Traefik **routers** are named `<project-slug>-<service>` to keep the dashboard's router list self-documenting and to let Ansible templates generate one label block per container deterministically.
- No cross-project routing rule may reference another project's backend — enforced by code review convention on the Compose label templates (Traefik itself has no concept of "tenant," so this is a process control, not a technical one).

## 5. Functional requirements

- **FR-1:** Traefik MUST automatically discover new backend containers via the Docker provider by watching the Docker socket (`providers.docker.endpoint = "unix:///var/run/docker.sock"`) and require an explicit `traefik.enable=true` label per container (`exposedByDefault: false`) so nothing is accidentally exposed.
- **FR-2:** Traefik MUST obtain and renew TLS certificates automatically from Let's Encrypt via the ACME HTTP-01 or DNS-01 challenge for any router tagged for public exposure (`certresolver=letsencrypt`).
- **FR-3:** Traefik MUST obtain TLS certificates from the internal CA (SVC-08/EJBCA) for internal-only hostnames, either via EJBCA's ACME-compatible endpoint (if available) or via a scripted cert-issuance sidecar that drops certs into a file provider watched by Traefik.
- **FR-4:** Traefik MUST redirect all plain HTTP (port 80) traffic to HTTPS (port 443) except for the ACME HTTP-01 challenge path itself.
- **FR-5:** Traefik MUST expose two entrypoints at minimum: `web` (`:80`) and `websecure` (`:443`), per the [official Traefik quick-start](https://doc.traefik.io/traefik/getting-started/quick-start/).
- **FR-6:** Traefik's dashboard (internal API, default port `8080`) MUST NOT be exposed with `--api.insecure=true` in this deployment; it MUST be routed through a dedicated router protected by SSO (SVC-06 forward-auth middleware) rather than left open on a raw port, per the docs' explicit "don't do in production" warning on insecure mode.
- **FR-7:** Traefik MUST support per-router middleware chains (rate limiting, forward-auth to ZITADEL, IP allowlisting) applied via labels, so each project can opt into stricter controls without editing Traefik's static config.
- **FR-8:** Traefik MUST load-balance across multiple replicas of the same backend container automatically when the Docker provider reports more than one matching container (round-robin default).

## 6. Non-functional requirements

- **Availability target:** this is the single point of failure for reaching every other service, so target 99.5%+ during business hours; a second Traefik replica behind a simple keepalived/VRRP floating IP is the recommended upgrade path once uptime requirements tighten, but a single-instance deployment is acceptable for the current homelab/office scale.
- **Performance/sizing:** Traefik itself is very lightweight (the official image is ~50 MB per [Docker Hub tag listing](https://hub.docker.com/_/traefik)); it is not CPU/RAM-bound at this traffic scale — sizing is dominated by the number of concurrent long-lived connections (e.g. streaming ML inference responses) rather than raw request rate.
- **Backup/DR:** the ACME certificate store (`acme.json`) and static/dynamic config files are backed up nightly to SVC-32; losing them just means one Let's Encrypt re-issuance cycle (rate-limited by Let's Encrypt to 50 certs/week per registered domain — plan cert consolidation with wildcards accordingly).
- **Data retention:** access logs retained 30 days in Loki (SVC-18); no long-term retention need beyond troubleshooting and basic traffic analytics.

## 7. Infrastructure architecture

- **Compute unit:** LXC container (per F2) — Traefik has no GPU/kernel-isolation requirement.
- **Minimum resource spec:** 1 vCPU, 512 MB RAM, 4 GB disk (local-zfs) — comfortably above Traefik's actual footprint even under moderate load; bump to 2 vCPU / 1 GB RAM if TLS handshake volume grows substantially (many concurrent HTTPS clients).
- **Network placement:** the Traefik LXC needs an interface on **every** VLAN that hosts a backend service (or a routed path to all of them) since it must reach each container's IP:port for proxying; in practice this means either (a) placing Traefik on a "front" VLAN with routed access to all internal service VLANs, or (b) attaching Traefik's LXC to the Docker network of each host it proxies to. Static IP (not DHCP) is required so DNS (SVC-10/PowerDNS) and public DNS records can point at a stable address; ports 80/443 are the only ones exposed externally.
- **Storage:** local-zfs for the `acme.json` certificate store and dynamic file-provider configs; small (a few MB), but must be on persistent storage, not tmpfs, or certificates re-issue on every container restart.

## 8. Terraform scope

- **Module inputs:** `vmid`, `hostname` (`svc09-traefik`), `vlan_tags` (list, since it may need multiple NICs), `static_ip`, `cpu_cores`, `memory_mb`, `disk_gb`, `proxmox_node`, `letsencrypt_email`, `letsencrypt_dns_provider` (for DNS-01 challenges, e.g. PowerDNS API credentials reference).
- **Resources created:** one `proxmox_virtual_environment_container` LXC resource (bpg/proxmox provider); network interface bindings per VLAN; a GitLab-managed Terraform state entry keyed `svc-09-reverse-proxy`.
- **Outputs:** `traefik_internal_ip`, `traefik_public_ip` (if applicable), `traefik_dashboard_url` — every other service's Ansible role consumes `traefik_internal_ip` to know where its own Compose labels need to be discoverable from (i.e., confirming it's on a Docker network Traefik can reach).

## 9. Ansible scope

- **Roles:** `traefik_install` (pulls `traefik:v3.6`, deploys the Compose stack and static `traefik.yml`), `traefik_letsencrypt_bootstrap` (configures the ACME resolver and DNS-01 provider credentials), `traefik_dashboard_auth` (wires the forward-auth middleware to ZITADEL).
- **Idempotency:** static config (`traefik.yml`) is fully templated and re-applying the playbook is a pure overwrite-and-restart; the ACME certificate store is never touched by Ansible directly (only Traefik itself writes to `acme.json`), preserving certificates across playbook re-runs.
- **Config files templated:** `traefik.yml` (static config: entrypoints, providers, certificatesResolvers), `docker-compose.yml` for the Traefik stack itself.
- **Secrets injected from SVC-07:** the DNS provider API token (for DNS-01 ACME challenges) and the ZITADEL forward-auth client secret are pulled from OpenBao at deploy time via the Ansible `community.hashi_vault` collection (compatible with OpenBao's Vault-API-compatible surface), never hardcoded in the playbook or committed to the repo.

## 10. CI/CD pipeline

- **Stages:** `lint` (terraform fmt/validate, yamllint on `traefik.yml` templates) → `plan` → manual approval → `apply` → `smoke test` (curl the dashboard health endpoint over HTTPS, verify a known test backend's router responds with a valid certificate chain).
- **Runs on:** GitLab CI (SVC-13 self-hosted runners); terraform stages use `hashicorp/terraform:1.x`, smoke test uses a thin `curlimages/curl` image.
- **State backend:** GitLab-managed Terraform state, per F1.

## 11. Secrets & credentials

- **What secrets exist:** the DNS provider API token used for ACME DNS-01 challenges (e.g. PowerDNS API key), the ZITADEL OIDC client secret used by the dashboard's forward-auth middleware, and (for internal-CA-issued certs) the EJBCA enrollment credential.
- **Where generated:** the DNS API token is generated once in PowerDNS's admin UI and stored in OpenBao; the ZITADEL client secret is generated when the ZITADEL application is registered for the Traefik dashboard.
- **Rotation:** DNS API token rotated annually or on suspected compromise; ZITADEL client secret rotated per SVC-06's standard OIDC client rotation policy.
- **How they reach the service:** injected as environment variables into the Traefik Compose stack by the Ansible role, fetched from OpenBao at deploy time (never stored in the Terraform state or the Git repo in plaintext).

## 12. Security & hardening baseline

- TLS everywhere is the *raison d'être* of this service: every router either redirects to HTTPS or is TCP/UDP-only for non-HTTP protocols.
- Least-privilege: the Traefik container's Docker-socket mount is **read-only** (`/var/run/docker.sock:/var/run/docker.sock:ro`) since Traefik only needs to *observe* container labels, never create/modify containers.
- `exposedByDefault: false` is set explicitly so a container must opt in with `traefik.enable=true` — an unlabeled container is never accidentally routable.
- Firewall/VLAN rules: only ports 80/443 are reachable from outside the platform's VLANs; the dashboard port (`8080` internally) is never exposed on a host port at all — it's reached only via a Traefik-routed HTTPS hostname protected by the ZITADEL forward-auth middleware (satisfies FR-6).
- CVE scanning: the `traefik` image is pulled through Harbor's (SVC-14) proxy-cache project, which applies Trivy scanning on ingestion.
- Auth via SVC-06: the Traefik dashboard requires ZITADEL SSO login via forward-auth middleware; no anonymous dashboard access under any circumstance.

## 13. Observability hooks

- Traefik exposes a native Prometheus metrics endpoint (`metrics.prometheus` provider) scraped by SVC-17 at `/metrics` on a dedicated internal-only entrypoint (not the public `web`/`websecure` entrypoints).
- Access logs (JSON format, `accessLog.format: json`) are shipped via Promtail to SVC-18/Loki, tagged with the `router` and `service` fields so per-project traffic can be filtered.
- Key alerts to define in Alertmanager: `traefik_backend_down` (a router's backend has zero healthy servers), `traefik_certificate_expiry_soon` (< 14 days to ACME cert expiry with no successful renewal logged), `traefik_5xx_rate_high` (elevated 5xx response rate on any router), `traefik_entrypoint_down` (the `web`/`websecure` entrypoint itself stops responding — treat as a platform-wide outage alert).

## 14. Acceptance criteria

- [ ] Traefik LXC provisioned via Terraform with static IP reachable from all backend service VLANs.
- [ ] A representative test container with `traefik.enable=true` labels is auto-discovered and routable within seconds of `docker compose up`, with no Traefik restart needed.
- [ ] Public test hostname obtains a valid Let's Encrypt certificate automatically; internal test hostname obtains a valid certificate from SVC-08's internal CA.
- [ ] HTTP→HTTPS redirect verified for both public and internal hostnames.
- [ ] Dashboard reachable only via SSO-protected hostname; direct access to port 8080 from outside the LXC is blocked.
- [ ] Prometheus scrape target live; Alertmanager rules fire correctly in a manual failure-injection test (stop a backend container, observe `traefik_backend_down`).
- [ ] `acme.json` and static config present in nightly Proxmox Backup Server backup.

## 15. Open questions / assumptions

- Assumes DNS-01 challenge support via PowerDNS's API is preferred over HTTP-01 so that internal-only or split-horizon hostnames can still get publicly-trusted certificates without exposing port 80 to the internet — needs confirmation once SVC-10 (PowerDNS) is finalized.
- Assumes a single Traefik instance is acceptable for now; a floating-IP HA pair is deferred until uptime requirements or a second Proxmox host justify it.
- The mechanism for internal-CA (SVC-08/EJBCA) certificate issuance into Traefik's file provider is sketched here but not fully designed — EJBCA's ACME support needs a validation spike before this is locked in.
- Whether project-facing subdomains use a single wildcard cert per zone or per-service certs is left open pending a decision on DNS zone structure in SVC-10's requirements doc.
