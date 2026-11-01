# `proxmox-compute` Module

Shared module that turns a project's `vlan_id` plus a host position into a fully
computed static address, a machine-facing hostname, and the Proxmox `tags`
value — so no operator ever hand-picks an IP or a hostname (NET-00 §3–§4,
Requirements 2.x/3.x). Every VM/LXC in the platform is expected to go through
this module.

This is a library module — it is not a Terraform root and has no state backend of
its own. It is consumed by each service's Terraform, which supplies the inputs
below and threads the module outputs into a guest resource and the Ansible
dynamic inventory. See the roots it pairs with:
[platform-foundation](../../platform-foundation/README.md) and the
[project template](../../projects/_TEMPLATE/README.md).

---

## Input contract

| Input | Meaning |
|---|---|
| `project_slug` | Project identity; drives the `tags` value `proj-<slug>-<service>`. |
| `service_name` | Human-facing service name for the `tags` value. |
| `svc_code` | Catalog guest identity — the stable catalog slot as `svcNN` (e.g. `svc01`); drives the hostname. Optional (default null); supply this XOR `role_code`. |
| `role_code` | **Non-catalog** guest identity (ADR-0021) — a lowercase role name (e.g. `workload`) for a guest that is NOT a catalog SVC-NN (e.g. a per-project consumer workload); drives the hostname `<role_code>-<vlan_id>-<instance>` exactly as `svc_code` would. Optional (default null); supply this XOR `svc_code`. Must NOT itself be a `svcNN` form. |
| `vlan_id` | The project's assigned VLAN ID (`vlan_id == 10` is rejected). |
| `host_index` | Zero-based host position; IP = `cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)`. |
| `instance` | Zero-padded 2-digit replica counter (default `01`). |
| `component` | Optional; hostname becomes `<identity>-<component>-<vlan_id>-<instance>`. |

### Guest identity: `svc_code` XOR `role_code` (ADR-0021)

Every guest is identified by **exactly one** of two mutually-exclusive inputs —
never both, never neither (enforced at plan time by a precondition, Guard 5):

- **`svc_code`** — for a **catalog** service. Strictly the stable catalog slot
  `svcNN` (NET-00 §4, Req 3.5), so a technology swap within a slot never forces
  a rename.
- **`role_code`** — for a **non-catalog**, role-named guest (ADR-0021), e.g. a
  per-project consumer `workload`. A lowercase role name that is NOT a `svcNN`
  code. Keyed on the stable role (also swap-stable), so `svc_code` keeps its
  single "catalog slot" meaning and is never overloaded.

The computed hostname is `<identity>[-<component>]-<vlan_id>-<instance>`, where
`<identity>` is whichever of `svc_code` / `role_code` was supplied. A catalog
caller (`svc_code` set, `role_code` null) produces exactly the same hostname as
before this contract change.

There is deliberately **no** operator-supplied IP-literal input — the absence is
the contract (Requirement 2.4).

## Output contract

`hostname`, `ipv4_address`, `ip_config`, `gateway`, `subnet_cidr`, `tags`,
`vmid`, `service_role`, `project_slug` — consumed by a guest resource (for
cloud-init injection) and by the Ansible dynamic inventory generator
(integration-boundaries §4).

The machine-facing `hostname` (`<svc-code>-<vlan_id>-<instance>`) and the
human-facing `tags` (`proj-<slug>-<service>`) are two **distinct** conventions
set on the same resource — never conflated (Requirement 3.4).

---

## Validate the module (offline, Tier-1)

The module has no backend, so a schema-only validate needs no infrastructure.
(**Terraform is not installed in the local authoring environment**; this Tier-1
command runs in CI where the binary exists.)

<!-- doctest: offline -->
<!-- cwd: infra/modules/proxmox-compute -->
```bash
terraform fmt -check -recursive
```

<!-- doctest: offline -->
<!-- cwd: infra/modules/proxmox-compute -->
```bash
terraform init -backend=false
terraform validate
```
