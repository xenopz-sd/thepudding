# svc-10-internal-dns — SVC-10 internal DNS (PowerDNS) Terraform root

One unprivileged LXC (`svc10-20-01`, `10.0.20.25`, VMID `20015`) on the shared-services VLAN 20, running the
PowerDNS authoritative server and recursor as a Compose stack. It serves the platform zone (`PLATFORM_DOMAIN`
in the cluster env file): wildcards to Traefik and one record per guest, synced from the generated inventories.

- Install and test: [`INSTALL-RUNBOOK.md`](./INSTALL-RUNBOOK.md)
- Spec: [`.kiro/specs/svc-10-internal-dns/`](../../../.kiro/specs/svc-10-internal-dns/)
- Domain requirements: [`SVC-10-internal-dns-requirements.md`](../../../requirements/SVC-10-internal-dns-requirements.md)

Terraform state name `svc-10-internal-dns` (one state per service). Identity is the catalog slot
`svc_code = "svc10"`; the host index (15) comes from the
[Host_Index_Authority ledger](../../platform-foundation/host-indices.yaml). Not an anchor: the zone is rebuilt by
the installer, so the guest is disposable. No secret lives here; the PowerDNS API key is in OpenBao.
