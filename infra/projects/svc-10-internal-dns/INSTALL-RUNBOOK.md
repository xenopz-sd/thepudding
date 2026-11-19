# SVC-10 Internal DNS Install Runbook — provision the platform DNS zone from nothing, then test it

> **Status: implemented and LIVE-VALIDATED on a throwaway cluster** (PowerDNS authoritative `5.1.5`, recursor
> `5.4.9`): from-scratch bring-up, idempotent re-run (`changed=0`), stale-record removal after a teardown, `apt`
> on another guest with the DNS guest stopped, recovery after a guest restart, clean-slate to an empty baseline,
> and Prometheus scraping both servers. Spec: [`svc-10-internal-dns`](../../../.kiro/specs/svc-10-internal-dns/);
> backlog row [WS-C57](../../../PLANNING.md).

SVC-10 gives the platform its own DNS zone, `<platform-domain>`, so platform names resolve inside the platform and
on developer machines, and so SVC-08 (the OpenBao CA, [ADR-0028](../../../.kiro/decisions/0028-svc08-internal-ca-technology-and-issuance-mode.md))
can validate ACME challenges. It runs [PowerDNS](https://www.powerdns.com/) (GPLv2, Netherlands) as two containers
on one guest:

- **pdns-auth**, the authoritative server, holds the zone:
  - `*.<platform-domain>` and `*.web.<platform-domain>` point to Traefik, so every HTTP name (`auth.`, `s3.`,
    `grafana.`, a consumer's `expose` host, a website bucket) resolves without its own record;
  - one A record per guest hostname (`svc07-20-01.<platform-domain>` and so on), taken from the generated
    Ansible inventories.
- **pdns-recursor** answers clients: it takes the platform zone from pdns-auth and resolves every other name
  from the public DNS. It serves private networks only.

> **Placeholders only.** `<platform-domain>` stands for your zone (for example a `lab.` subdomain of a domain you
> own). Put it in your gitignored cluster env file as `PLATFORM_DOMAIN`; never commit it. The PowerDNS API key
> lives in OpenBao and is never printed (`documentation-testing.md`, `security-standards.md`).

> **Doctest tiers.** Offline preflight commands carry `<!-- doctest: offline -->`; the installer runs, the
> resolution checks and the teardown carry `<!-- doctest: requires-infra -->`. Tier rules are owned by
> [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md); the runbook location by
> [`structure.md`](../../../.kiro/steering/structure.md).

---

## ⚠ Throwaway cluster only

The live steps provision, mutate and tear down real infrastructure. Run them **only against a throwaway Proxmox
SDN test cluster**. Agent-driven live runs additionally require `AGENT_LIVE_AUTHORIZED` and the throwaway guard
(`testing-strategy.md`).

---

## What you get

| Item | Value |
|---|---|
| Guest | `svc10-20-01`, unprivileged LXC (1 vCPU, 512 MB, 4 GB), VLAN 20, `10.0.20.25` (Host_Index_Authority index 15), VMID `20015` |
| Resolver | `10.0.20.25:53` UDP/TCP (pdns-recursor); allowed sources: RFC 1918 networks and loopback only |
| Authoritative | `10.0.20.25:5300` (pdns-auth, SQLite in the Docker volume `pdns-data`); no zone transfers |
| API + metrics | pdns-auth webserver `:8081` (HTTP API needs `X-API-Key`; `/metrics` open); recursor `:8082` (`/metrics`) |
| API firewall | in-guest `PDNS-API` chain: only Traefik (`10.0.20.12`, for the SVC-08 DNS-01 path), Prometheus (`10.0.20.18`) and your `ADMIN_SOURCE_CIDR` reach `:8081`/`:8082` |
| API key | OpenBao `secret/proj-shared-dns/pdns`, field `api_key`; generated on first install, reused afterwards |
| Images | `powerdns/pdns-auth-51` and `powerdns/pdns-recursor-54`, pinned to exact versions **and** digests |
| Alerts | `DnsRecursorDown` (critical), `DnsRecursorServfailRate` (warning, > 10 % SERVFAIL) |

Every port is bound to the guest's VLAN-20 IP, never `0.0.0.0`. DNS is not HTTP ingress, so there is no Traefik
route for this service.

**Guests keep their own resolver.** No guest's `/etc/resolv.conf` is changed, so a DNS outage cannot break
`apt` or image pulls (requirements FR-4). Only clients that need platform names use the recursor: developer
machines (section A.5) and, later, the CA and consumer workload containers.

## A. Developer-B automated path — one command, provision-then-test

### A.0 — Offline preflight (no cluster needed)

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
cd infra/projects/svc-10-internal-dns
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-10-bootstrap.yml
```

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest tests/installer/test_svc10_installer.py \
  tests/installer/test_dns_records.py \
  tests/installer/test_platform_domain.py -q
~/venv/devinfra/bin/pytest infra/tests/test_svc10_terraform_shape.py \
  infra/tests/test_obs_dns_alerts.py -q
```

`test_svc10_installer.py` includes a contract test that renders the PowerDNS configs and runs them in the pinned
images; it is skipped when no Docker daemon is usable.

### A.1 — Prerequisites

- The VLAN-20 fabric (`p20`) from the [platform install runbook](../../platform-foundation/INSTALL-RUNBOOK.md);
  the preflight fails closed naming `p20` if it is absent.
- SVC-07 (OpenBao) up and unsealed, with `OPENBAO_ADMIN_TOKEN` in your cluster env file or environment
  ([SVC-07 runbook](../svc-07-secrets-manager/INSTALL-RUNBOOK.md)). The preflight fails closed without it.
- `PLATFORM_DOMAIN=<platform-domain>` in your cluster env file. Every installer uses it as its platform domain
  (an explicit `-e <service>_platform_domain=…` still wins); without it the zone is the placeholder
  `platform.example.test`.
- Optional: `ADMIN_SOURCE_CIDR` (your workstation's network) to reach the pdns API from your machine.

### A.2 — One-command install (preflight → provision → bootstrap → health)

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-10-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env
```

The orchestrator runs:

1. **Preflight**: env resolve, OpenBao token present, Proxmox auth, `p20` realized-bridge check, datastore
   autodetect, throwaway-guard consult when agent-driven.
2. **Provisioning**: `terraform apply` of the single LXC, inventory generation, `known_hosts`.
3. **Bootstrap**:
   - reads the API key from OpenBao, or generates and stores it on first install; a sealed or unreachable
     OpenBao stops the run;
   - the `common` role;
   - the PowerDNS stack, the zone and the API firewall (`pdns.yml`);
   - the record sync (below).
4. **Handoff**: the health gate, then the banner with the split-DNS commands.

**Record sync.** The sync reads every `ansible/inventory/*.generated.yml` on the control node and computes the
desired A records with `scripts/installer/dns_records.py`. It applies the difference with `pdnsutil`, deleting
stale A records, then wipes the recursor cache for the zone and re-plans; a non-empty second plan fails the run.
Only A records are managed: SOA, NS and any TXT record (for example an ACME `_acme-challenge`) are left alone.

**Re-run SVC-10 after installing or removing any service** so its guest records follow. HTTP names need no
re-run: the wildcard already covers them.

### A.3 — Verify

The Handoff already ran the health gate: both containers running; through the recursor a wildcard name resolves
to Traefik, `svc10-20-01.<platform-domain>` to `10.0.20.25` and a public name to an address; the API refuses a
request without the key (401) and accepts the key; both `/metrics` endpoints answer. To query it yourself from
the control node (which reaches VLAN 20 through the host route, ADR-0004):

<!-- doctest: requires-infra -->
```bash
dig +short @10.0.20.25 auth.<platform-domain>          # 10.0.20.12 (Traefik, wildcard)
dig +short @10.0.20.25 svc07-20-01.<platform-domain>   # 10.0.20.10
dig +short @10.0.20.25 debian.org                      # a public address
```

### A.4 — Test what you built: degraded mode and stale records

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
# 1. Remove a service (here the mail catcher) and re-run SVC-10: its record is deleted.
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/mailcatcher-bootstrap.yml -e cluster_env_file=cluster.dev.env \
  -e clean_slate=true -e clean_slate_confirm=force -e mailcatcher_proxmox_host=<real-proxmox-lan-ip>
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-10-bootstrap.yml -e cluster_env_file=cluster.dev.env
dig +short @10.0.20.25 mailcatcher-20-01.<platform-domain>   # now the wildcard (10.0.20.12)
```

<!-- doctest: requires-infra -->
```bash
# 2. Stop the DNS guest: platform names stop resolving, guests still install packages.
ssh root@<real-proxmox-lan-ip> pct stop 20015
ssh root@10.0.20.21 apt-get update      # still succeeds: guests use their own resolver
ssh root@<real-proxmox-lan-ip> pct start 20015
```

After the restart both containers and the firewall come back on their own (`restart: unless-stopped`, and the
`pdns-api-firewall` unit is `PartOf=docker.service`); a re-run of the installer reports `changed=0`.

### A.5 — Point your workstation at it (split DNS, once)

The installer banner prints these commands with your zone filled in, so you can copy them as they are.
`<platform-domain>` is the value of `PLATFORM_DOMAIN` in your cluster env file: the whole zone (for example
`lab.example.test`), not its public parent domain. Your workstation also needs the VLAN-20 route from the platform
runbook (check with `ip route get 10.0.20.25`).

**Linux with systemd-resolved** (Ubuntu, Fedora, Debian with `systemd-resolved`). One global drop-in, persistent
across reboots and reconnects, and independent of NetworkManager:

<!-- doctest: display-only -->
```bash
sudo mkdir -p /etc/systemd/resolved.conf.d
echo '[Resolve]' | sudo tee /etc/systemd/resolved.conf.d/platform-dns.conf
echo 'DNS=10.0.20.25' | sudo tee -a /etc/systemd/resolved.conf.d/platform-dns.conf
echo 'Domains=~<platform-domain>' | sudo tee -a /etc/systemd/resolved.conf.d/platform-dns.conf
sudo systemctl restart systemd-resolved

# Check
resolvectl status | head -6                  # Global: DNS Servers 10.0.20.25, DNS Domain ~<platform-domain>
resolvectl query svc10-20-01.<platform-domain>   # 10.0.20.25
resolvectl query debian.org                  # public names still resolve
```

Example for `PLATFORM_DOMAIN=lab.example.test`: the file holds `DNS=10.0.20.25` and `Domains=~lab.example.test`.

How it routes (systemd-resolved, checked against the v255 source and on a workstation):

- The leading `~` makes the domain **routing-only**: names under `<platform-domain>` go to `10.0.20.25` only, and
  the domain is not appended to short names (`ping grafana` stays `grafana`).
- Every other name goes to your normal DNS server (from DHCP or NetworkManager) **and** to SVC-10; the first good
  answer wins. So public names keep resolving when SVC-10 is down. The cost: public lookups also reach SVC-10's
  recursor.
- To undo: `sudo rm /etc/systemd/resolved.conf.d/platform-dns.conf && sudo systemctl restart systemd-resolved`.

> **Do not use `resolvectl dns <iface> 10.0.20.25`.** It *replaces* the interface's DNS servers, so every lookup
> goes to SVC-10 (no DNS at all when it is down), and it is lost on reconnect. If you already ran it, restore the
> interface with `sudo resolvectl revert <iface>` and, under NetworkManager, `sudo nmcli device reapply <iface>`.

**macOS.** A per-domain resolver file; the file name is the zone itself, without `~`, and the directory may not
exist yet:

<!-- doctest: display-only -->
```bash
sudo mkdir -p /etc/resolver
echo 'nameserver 10.0.20.25' | sudo tee /etc/resolver/<platform-domain>
scutil --dns | grep -A3 '<platform-domain>'   # shows nameserver 10.0.20.25
```

**Check from either system** with the normal resolver (no `@server`):

<!-- doctest: requires-infra -->
```bash
dig +short svc10-20-01.<platform-domain>      # 10.0.20.25
dig +short auth.<platform-domain>             # 10.0.20.12 (Traefik)
```

### A.6 — Clean-slate reset

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-10-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e svc10_proxmox_host=<real-proxmox-lan-ip>
```

The reset destroys the guest, residue-sweeps the Terraform state, verifies an empty baseline two ways
(`pct list` and `terraform state list`) and removes the generated inventory. The API key stays in OpenBao, so a
re-install reuses it. The platform clean-slate discovers this service through its catalog row.

---

## Degraded modes

| Situation | What happens | What you see |
|---|---|---|
| DNS guest down | platform names stop resolving for clients that use the recursor; guests keep resolving public names (FR-4) | `DnsRecursorDown` after 2 min; `dig` times out |
| Upstream DNS or pdns-auth not answering | recursor returns SERVFAIL | `DnsRecursorServfailRate` after 10 min |
| OpenBao sealed or unreachable at install | install stops before generating a key (a new key would break key holders) | message names `svc-07-bootstrap.yml` |
| A service added or removed without re-running SVC-10 | its guest name is missing or stale; HTTP names still work through the wildcard | `dig` returns the wildcard address; re-run SVC-10 |
| Two inventories give one hostname two IPs | the sync refuses and changes nothing | message names the host and both addresses |

## Image bumps

Both images are pinned to an exact tag **and** digest (`ansible/roles/pdns_install/defaults/main.yml`). The PowerDNS
security advisories are published at <https://doc.powerdns.com/authoritative/security-advisories/> and
<https://doc.powerdns.com/recursor/security-advisories/>. To bump, choose the new tag within the same major series
(the image names carry the series, e.g. `pdns-auth-51`), resolve its digest, update the `*_ref` and `*_digest`
values together, then run A.2 to A.4 on a throwaway cluster.

## Known limitations

- One guest, no secondary server: a DNS outage lasts until the guest is back (records are rebuilt by a re-run).
- The zone is not DNSSEC-signed and never published publicly; the recursor carries a negative trust anchor for it
  so a signed public parent zone does not make it bogus.
- The pdns API is plain HTTP on VLAN 20, limited by the in-guest firewall and the key; TLS for it comes with SVC-08.
- Consumer workload containers do not use the recursor yet; that is decided with SVC-08 and the consumer path.
