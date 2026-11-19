# SVC-03 Object Storage (Garage) Install Runbook — provision from nothing, then test

> **Status: implemented and LIVE-VALIDATED on a throwaway cluster** (Garage `v2.5.0`): from-scratch bring-up,
> idempotent re-run (`changed=0`), two projects' buckets and keys with cross-project denial, one node stopped while
> reads and writes continue, metrics in Prometheus, clean-slate, and a second from-scratch bring-up. Evidence:
> [`validation.md`](../../../.kiro/specs/svc-03-object-storage/validation.md).

This runbook takes you from an empty VLAN 20 to a three-node [Garage](https://garagehq.deuxfleurs.fr) S3 cluster
(Deuxfleurs, France; AGPLv3), then tests it. Spec: [`svc-03-object-storage`](../../../.kiro/specs/svc-03-object-storage/);
domain requirements: [`SVC-03-object-storage-requirements.md`](../../../requirements/SVC-03-object-storage-requirements.md).

> **Placeholders only.** Hostnames and credentials here are placeholders. The cluster secrets and every project key
> live only in OpenBao and are **never printed** (`security-standards.md`).

> **Doctest tiers.** Offline commands carry `<!-- doctest: offline -->`; the Terraform apply, the Ansible runs and the
> teardown carry `<!-- doctest: requires-infra -->`. Tier rules: [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md);
> runbook location: [`structure.md`](../../../.kiro/steering/structure.md).

---

> **Platform domain.** Set `PLATFORM_DOMAIN=<platform-domain>` once in your cluster env file ([`cluster.env.example`](../../../cluster.env.example)); every installer uses it as its platform domain, and an explicit `-e <service>_platform_domain=…` still wins. SVC-10 serves that zone ([runbook](../../../infra/projects/svc-10-internal-dns/INSTALL-RUNBOOK.md)).

## ⚠ Throwaway cluster only for the destructive steps

The clean-slate destroys every object. Agent-driven live runs additionally need `AGENT_LIVE_AUTHORIZED` and the
throwaway guard (`testing-strategy.md`).

## What you get

| Item | Value |
|---|---|
| Guests | `svc03-20-01`, `-02`, `-03`: unprivileged LXC on VLAN 20, `10.0.20.22`/`.23`/`.24` (ledger indices 12/13/14), VMIDs `20012`/`20013`/`20014` |
| Sizing | 2 vCPU, 2 GB RAM, 8 GB rootfs, a dedicated 20 GB data volume at `/var/lib/garage` per node (`-e svc03_data_disk_gb=<n>`) |
| Cluster | replication factor 3, `consistent` mode, one zone `dc1`, capacity per node = 80 % of the data volume |
| S3 API | `http://10.0.20.2x:3900` on VLAN 20 (any node), `https://s3.<platform-domain>` through Traefik (node 1); region `garage`; **path-style** |
| Admin API | `http://10.0.20.2x:3903`, bearer admin token; **reachable only from the Prometheus guest** (`10.0.20.18`) and from the node itself (in-guest firewall, `garage_admin_allowed_sources`) |
| Websites | `https://<bucket>.web.<platform-domain>` through Traefik, for buckets you opt in; port 3902 on VLAN 20 |
| Secrets | `secret/proj-shared-garage/garage` (RPC secret, admin token); `secret/proj-<slug>/garage` (a project's key) |
| Image | `dxflrs/garage:v2.5.0` pinned by digest |

On a single-node Proxmox cluster the three guests share one host: replication protects against losing a guest, not
the host.

## A. One command, provision-then-test

### A.0 — Offline preflight (no cluster needed)

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
cd infra/projects/svc-03-object-storage
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check -i ansible/inventory/localhost.yml ansible/playbooks/svc-03-bootstrap.yml
~/venv/devinfra/bin/ansible-playbook --syntax-check -i ansible/inventory/localhost.yml ansible/playbooks/garage-bucket-provision.yml
```

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest tests/installer/test_svc03_installer.py tests/installer/test_svc03_image_pin.py \
  tests/installer/test_s3_smoke.py -q
~/venv/devinfra/bin/pytest infra/tests/test_svc03_terraform_shape.py -q
```

### A.1 — Prerequisites

- The platform foundation (VLAN-20 VNet `p20`, host gateway) is applied.
- **SVC-07 OpenBao** is up and `OPENBAO_ADMIN_TOKEN` is in your cluster env file (its bootstrap writes it). SVC-03 has
  no offline mode: the cluster secrets must persist in OpenBao.
- **SVC-09 Traefik** is up (the S3 route is written onto its guest). Without Traefik pass
  `-e svc03_traefik_route_enabled=false`; VLAN-20 access still works.
- Your cluster env file has the usual Proxmox keys (`cluster.env.example`).

### A.2 — Install (preflight → provision → bootstrap → health)

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-03-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env
```

What happens: preflight checks `p20` (realized on the node), the datastore, the tools and the OpenBao token;
Terraform creates the three guests; the bootstrap reads the cluster secrets from OpenBao (or generates and stores
them on the very first run), installs Docker, writes `garage.toml` (0600), starts Garage, connects the nodes and
applies the layout; the handoff waits for `healthy`, runs an S3 put/get/delete through a scratch key and bucket it
then removes, and prints the summary. A second run reports `changed=0` and makes no layout change.

### A.3 — Verify

<!-- doctest: requires-infra -->
```bash
ssh root@10.0.20.22 docker exec garage-garage-1 /garage status
ssh root@10.0.20.22 docker exec garage-garage-1 /garage json-api GetClusterHealth
ssh root@10.0.20.22 "curl -s -o /dev/null -w '%{http_code}\n' http://10.0.20.22:3903/health"
```

Expect three healthy nodes, `"status": "healthy"` with `"storageNodesUp": 3`, and `200`. The same `curl` from
your workstation times out on purpose: each Garage guest's `garage-admin-firewall` unit admits only the sources in
`garage_admin_allowed_sources` (default: the Prometheus guest) to port 3903. Add an address there (and re-run the
installer) if another host needs the admin API; `-e garage_admin_firewall_enabled=false` removes the firewall.

### A.4 — Give a project a bucket and a key

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/svc-03-object-storage.generated.yml \
  ansible/playbooks/garage-bucket-provision.yml \
  -e project_slug=<project-slug> \
  -e '{"bucket_purposes": ["artifacts", "datasets"]}' \
  -e cluster_env_file=cluster.dev.env
```

This creates `proj-<slug>-artifacts` and `proj-<slug>-datasets`, one key `proj-<slug>-cikey` with read and write on
those buckets only (no owner flag, cannot create buckets), and stores at `secret/proj-<slug>/garage` the fields
`access_key_id`, `secret_access_key`, `endpoint`, `internal_endpoint`, `region`, `buckets`. A re-run changes nothing
and never rotates the key. After a SVC-03 clean-slate, a re-run creates a new key and overwrites the OpenBao entry.
Purposes are 1-32 lowercase letters or digits with **no hyphen**, so `proj-<slug>-<purpose>` cannot equal another
project's bucket. A project that has a consumer manifest declares the same thing as `object_storage.buckets`, and
`project-consumer-bootstrap.yml` runs this step for it.

To remove a project's buckets (with all their objects), its key and its OpenBao entry, run the same playbook with
`-e garage_bp_action=teardown -e project_slug=<project-slug>` (the consumer clean-slate does this for manifest
projects). It deletes every bucket the key is granted on plus any declared `bucket_purposes`, and fails unless
nothing remains; a second run is a no-op.

### A.4a — Serve a bucket as a static website

Name the website purposes (a subset of the bucket purposes) when provisioning:

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/svc-03-object-storage.generated.yml \
  ansible/playbooks/garage-bucket-provision.yml \
  -e project_slug=<project-slug> \
  -e '{"bucket_purposes": ["artifacts", "docs"], "website_purposes": ["docs"]}' \
  -e cluster_env_file=cluster.dev.env
```

`proj-<slug>-docs` is then served at `https://proj-<slug>-docs.web.<platform-domain>/` (index document
`index.html`). **Everything in a website bucket is public** to anyone who can reach Traefik. Every run reconciles:
buckets not listed get website access switched off and their route removed. Consumer projects declare the same thing
as `object_storage.website` in the manifest.

### A.4b — Rotate a project's key

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/svc-03-object-storage.generated.yml \
  ansible/playbooks/garage-bucket-provision.yml \
  -e garage_bp_action=rotate -e project_slug=<project-slug> \
  -e cluster_env_file=cluster.dev.env
```

It creates a new key with the old key's exact bucket grants, proves it can list each bucket, writes it to
`secret/proj-<slug>/garage` as a new KV version (the previous version stays in OpenBao's history), then deletes the
old key and gives the new one the name `proj-<slug>-cikey`. From that moment the old credentials get 403, so clients
that cache them must re-read OpenBao (the OpenBao Agent re-renders on its own interval). For the catalog's 180-day
cadence, schedule a daily run with `-e garage_bp_rotate_min_age_days=180`: it only rotates a key that old and
otherwise reports "not due". A run interrupted after the old key's deletion is finished by the next run (it renames
the `proj-<slug>-cikey-next` key that OpenBao already holds).

### A.5 — Test what you built (bucket isolation, node loss)

The checks use `scripts/installer/s3_smoke.py` (stdlib SigV4, credentials from the environment only). Read a
project's key from OpenBao into the environment, never onto the command line:

<!-- doctest: requires-infra -->
```bash
read_field() {  # $1 slug, $2 field; prints the value to the caller's $(...) only
  ssh root@10.0.20.10 "BAO_TOKEN='$OPENBAO_ADMIN_TOKEN' docker exec -e BAO_ADDR=https://127.0.0.1:8200 -e BAO_TOKEN \
    openbao-openbao-1 bao kv get -tls-skip-verify -field=$2 secret/proj-$1/garage"
}
export S3_ACCESS_KEY_ID=$(read_field <project-slug> access_key_id)
export S3_SECRET_ACCESS_KEY=$(read_field <project-slug> secret_access_key)
~/venv/devinfra/bin/python scripts/installer/s3_smoke.py --endpoint http://10.0.20.22:3900 \
  --bucket proj-<project-slug>-artifacts roundtrip
~/venv/devinfra/bin/python scripts/installer/s3_smoke.py --endpoint http://10.0.20.22:3900 \
  --bucket proj-<other-project-slug>-artifacts expect-denied
```

Node loss: `ssh root@10.0.20.23 docker stop garage-garage-1`, repeat the `roundtrip` (it still succeeds; health is
`degraded`), then `docker start garage-garage-1` and wait for `healthy`.

### A.6 — Metrics

Re-run SVC-17/18 after SVC-03 exists (its scrape targets come from the generated inventories):

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-17-18-bootstrap.yml -e cluster_env_file=cluster.dev.env -e obs_tls_self_signed=true
```

Prometheus then shows three `up{service="garage"}` targets on `:3903`.

### A.7 — Clean-slate reset (destroys all object data)

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-03-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e svc03_proxmox_host=<proxmox-lan-ip>
```

It destroys the three guests and their data volumes, empties the SVC-03 Terraform state, removes the Traefik
`garage-s3-route.yml` fragment and the generated inventory, and verifies both `pct list` and `terraform state list`
are empty. The OpenBao entries are kept: the next install reuses the cluster secrets, and project keys are re-created
by the next `garage-bucket-provision.yml` run. The platform clean-slate discovers SVC-03 through its catalog row.

## Consuming the credentials from a project

Your CI job or workload reads `secret/proj-<slug>/garage` (through the OpenBao Agent or an OpenBao-authenticated CI
step) and sets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION=garage` and the endpoint. Use path-style
addressing (`aws --endpoint-url ... s3 ...` uses it with `addressing_style = path`; boto3:
`Config(s3={"addressing_style": "path"})`). Inside VLAN 20 use `internal_endpoint`; from elsewhere use `endpoint`
(Traefik, TLS).

## Alerts

SVC-17/18's baseline carries six Garage rules (`platform-garage` group, from Garage's own metrics):

| Alert | Severity | Fires when |
|---|---|---|
| `GarageStorageNodeDown` | warning | a storage node in the layout is not connected for 5 min |
| `GarageQuorumLost` | critical | some partitions have no write quorum for 2 min (writes failing) |
| `GarageUnderReplicated` | warning | some partitions lack a reachable copy for 30 min |
| `GarageDiskAlmostFull` | warning | a node's data volume is over 80 % for 15 min |
| `GarageResyncErrors` | warning | a node has blocks whose last resync failed, for 30 min |
| `GarageS3ServerErrors` | warning | more than 5 % of S3 requests return 5xx for 15 min |

To see one fire: stop a node (`ssh root@10.0.20.24 docker stop garage-garage-1`), wait over 5 minutes, and check
Prometheus or Alertmanager; `docker start garage-garage-1` resolves it. Garage's container logs are not shipped to
Loki yet (WS-C58).

## Degraded modes

| Condition | What you see | Recovery |
|---|---|---|
| One node down | `GetClusterHealth` `degraded`, reads and writes continue; a re-run of the installer fails the health gate naming it | Start the container or re-run the installer |
| Two nodes down | Writes fail; `/health` 503; Prometheus targets down | Restore the nodes |
| Node 1 down | `https://s3.<domain>` fails (single Traefik backend); VLAN-20 clients use `.23`/`.24` | Restore node 1 |
| OpenBao down or sealed | Install stops at the secret read; bucket provision stops before creating a key | Fix SVC-07 via its own bootstrap |
| Traefik absent | Route write fails | Bring SVC-09 up, or `-e svc03_traefik_route_enabled=false` |
| Prometheus moved to another IP | Garage targets down, `InstanceDown` fires (the admin-API firewall drops the scrape) | Update `garage_admin_allowed_sources` and re-run |

## Fallback: Scality CloudServer

If a project needs something Garage does not offer, the documented fallback is
[Scality CloudServer](https://github.com/scality/cloudserver) (Scality, Paris, Apache-2.0), image
`ghcr.io/scality/cloudserver` pinned to `9.5.5` by digest. It is a single-node S3 server with no replication of its
own, so it replaces Garage's S3 API, not its fault tolerance. It replaced MinIO, whose Community Edition was archived
upstream. It is not deployed by the platform; the platform's S3 client contract (create bucket, round trip, list,
purge, delete, cross-account refusal) was checked against it once:

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/pytest -m requires_infra tests/installer/test_s3_fallback_cloudserver.py -q
```

## Image bumps and security advisories

The pin lives in `ansible/roles/garage_install/defaults/main.yml` (tag + digest). Garage publishes security fixes in
the "Security" section of its [release notes](https://git.deuxfleurs.fr/Deuxfleurs/garage/releases), not as GHSA
entries. On every bump: read the release notes, add any new security row to
`tests/fixtures/advisories/garage-advisories.json`, resolve the new digest, and run
`tests/installer/test_svc03_image_pin.py`. Garage v2.5.0 cannot be downgraded once a node runs it.

## Known limitations

- Log shipping to Loki is WS-C58.
- Resizing a data volume needs a layout capacity update (`garage layout assign -c` + `garage layout apply`), which the
  installer does not do for nodes that already have a role.
