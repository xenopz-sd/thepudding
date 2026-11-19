# SVC-10 — Internal DNS — Requirements

## 1. Overview

SVC-10 gives the platform a DNS zone of its own, a subdomain of a domain the operator owns (written here as
`<platform-domain>`), so every platform name (`auth.`, `s3.`, `grafana.`, a consumer's `expose` host, website hosts,
guest hostnames like `svc07-20-01.`) resolves inside the platform and on developer machines. It replaces today's
workarounds (`--resolve`, hosts-file entries, the dev issuer-reachability recipe) and is the prerequisite for ACME
issuance by SVC-08 (ADR-0028): every ACME challenge type needs the CA to resolve the requested names. All project
types depend on it indirectly.

## 2. Scope

**In scope:** an authoritative server for `<platform-domain>`; a recursive resolver for platform guests and
developer machines that answers the platform zone from the authoritative server and everything else from upstream;
records derived from what is installed (Traefik names, guest hostnames); a record API for SVC-08's ACME DNS-01
later; a single platform-domain setting in the cluster env file.

**Out of scope:** public DNS for `<platform-domain>` (never published to the internet); DNSSEC for the private zone;
DHCP; per-project zones (projects use names under the platform zone); a second DNS guest for high availability
(later, if needed).

## 3. Technology selection

- **Default: PowerDNS** (Authoritative Server + Recursor) — 🇪🇺 PowerDNS.COM B.V., The Hague, Netherlands (part of
  Open-Xchange, Germany); GPL-2.0. The catalog default. The authoritative server has an HTTP API (records, zones)
  that lego/Traefik and other ACME clients support for DNS-01.
- Alternatives considered: Knot DNS + Knot Resolver (🇪🇺 CZ.NIC, Czech Republic; excellent, but no built-in record
  HTTP API for ACME DNS-01 without extra tooling); CoreDNS (CNCF, 🌍; a single binary, but its file/etcd backends
  make API-driven records less direct); dnsmasq (minimal, no API).

## 4. Multi-tenancy model

**Shared platform service** (VLAN 20). One platform zone; projects get names under it (`<host>.<platform-domain>`
for exposed workloads, `<bucket>.web.<platform-domain>` for websites). Only the platform's own installers write
records; projects cannot.

## 5. Functional requirements

- **FR-1**: The authoritative server MUST serve `<platform-domain>` with: a wildcard `*.<platform-domain>` A record
  to the Traefik guest (every HTTP name, including consumer `expose` hosts, resolves without per-service records); a
  wildcard `*.web.<platform-domain>` to Traefik; one A record per platform guest hostname (`svcNN-…-20-01`, project
  guests) to its IP. Specific records override the wildcard.
- **FR-2**: Guest records MUST be derived from the generated Ansible inventories (file-mediated, never Terraform
  state), so re-running SVC-10's installer after any service install reconciles them; records of torn-down guests
  MUST be removed.
- **FR-3**: The recursor MUST answer `<platform-domain>` from the authoritative server and recurse for everything
  else, and MUST serve only allowed networks (VLAN 20, project VLANs, the developer LAN routed through the host
  gateway).
- **FR-4**: A DNS outage MUST NOT break name resolution for package installs and image pulls on guests: guests keep
  their upstream resolver for public names; only clients that need platform names (containers that call platform
  hosts, the CA's ACME validator, developer machines) use the recursor.
- **FR-5**: The authoritative server's HTTP API MUST be reachable only from the control node and SVC-08's ACME client
  path, with its API key stored in OpenBao.
- **FR-6**: The cluster env file MUST carry one `PLATFORM_DOMAIN` key that every installer uses as its platform
  domain (replacing the per-installer `-e <svc>_platform_domain` overrides, which stay as explicit overrides).
- **FR-7**: The installer MUST print the one-line split-DNS setup for a developer machine (systemd-resolved per-link
  DNS for `~<platform-domain>`, or the macOS `/etc/resolver/` file).

## 6. Non-functional requirements

- **Availability**: one guest; a DNS outage degrades platform name resolution only (FR-4). Restore by re-running the
  installer (records are derived, the zone is reproducible).
- **Sizing**: 1 vCPU, 512 MB RAM, 4 GB disk.
- **Backup**: none needed beyond the Terraform/Ansible inputs (records are regenerated); the API key lives in OpenBao.

## 7. Infrastructure architecture

- One unprivileged LXC on VLAN 20, `svc_code: svc10`, ledger index **15** (`10.0.20.25`, VMID `20015`), not an anchor
  (stateless, regenerated).
- Compose stack: `pdns-auth` (SQLite backend, API on VLAN-20 IP) and `pdns-recursor` (port 53 UDP/TCP on the
  VLAN-20 IP).

## 8. Terraform scope

Root `infra/projects/svc-10-internal-dns/` (state `svc-10-internal-dns`) via `proxmox-compute`; outputs
`inventory_hosts`, `dns_resolver_ip`.

## 9. Ansible scope

`svc-10-bootstrap.yml` (four phases + clean-slate, platform clean-slate row); a `pdns_install` configurator role;
a `dns_records_sync` role (reads all `*.generated.yml`, reconciles the zone through the pdns API, removes stale
records); `PLATFORM_DOMAIN` added to `cluster_env.py` and threaded into every bootstrap's `*_platform_domain`
default.

## 10. CI/CD pipeline

Offline gate as for every service; live acceptance on the throwaway cluster; state `svc-10-internal-dns`.

## 11. Secrets & credentials

The pdns API key: generated once, stored in OpenBao at `secret/proj-shared-dns/pdns`, passed to the containers in
a 0600 file, never printed.

## 12. Security & hardening baseline

Recursor ACL limited to platform networks; no open resolver; API bound to the VLAN-20 IP and restricted by the
in-guest firewall pattern of WS-C53 (control node + the SVC-08 path); no zone transfers; the zone is never published
publicly.

## 13. Observability hooks

Both PowerDNS components expose Prometheus metrics (`/metrics` on their webservers): add scrape rows; alerts for
the recursor down and for SERVFAIL rate.

## 14. Acceptance criteria

- [ ] From-scratch bring-up on the throwaway cluster under `AGENT_LIVE_AUTHORIZED` + the throwaway guard.
- [ ] From a VLAN-20 guest and from a developer machine with split DNS: `auth.<platform-domain>`,
      `s3.<platform-domain>`, `<anything>.<platform-domain>` resolve to Traefik; `svc07-20-01.<platform-domain>`
      resolves to `10.0.20.10`; a public name resolves through the recursor.
- [ ] After a service is torn down and SVC-10 re-run, its guest records are gone.
- [ ] With the DNS guest stopped, `apt update` on another guest still works.
- [ ] Clean-slate leaves `terraform state list` empty; distinct host IP verified against the ledger.

## 15. Open questions / assumptions

- The zone name is the operator's subdomain (set in the cluster env file, never committed).
- Developer machines must add the split-DNS setting once (like the static route of ADR-0004).
- Whether consumer workload containers should default to the recursor is decided with SVC-08/the consumer path.
