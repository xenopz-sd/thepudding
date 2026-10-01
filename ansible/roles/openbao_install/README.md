# `openbao_install` Ansible role

Installs and configures OpenBao (SVC-07 Secrets Manager) on a Proxmox LXC as a
Docker Compose stack. This role covers the **server side** of SVC-07 — the two
OpenBao LXCs and their `config.hcl`. It is distinct from the downstream
`openbao-agent` sidecar every *other* service runs (out of scope for SVC-07).

The role renders one of two `config.hcl` shapes depending on `openbao_role`:

- `primary` — `svc07-20-01` (`10.0.20.10`): TLS listener, Raft storage at
  `/openbao/data`, `seal "transit"` auto-unseal against the unsealer,
  Prometheus telemetry, `api_addr`/`cluster_addr`.
- `unsealer` — `svc07-unsealer-20-01` (`10.0.20.11`): a deliberately minimal
  Transit-only config — TLS listener + minimal Raft storage, **no** `seal`
  stanza and **no** KV/database/`aws`/PKI mount.

> **Task scope note:** this role templates `config.hcl` (task 5.1) and the
> Compose stack (task 5.2, via the generic `docker-compose-app` role) and ships
> a concrete restart handler. The version-check idempotency logic (task 5.3)
> prepends a guard to `tasks/main.yml` ahead of the compose block.

## Role layout

```
openbao_install/
  defaults/main.yml               # variable set (image, TLS, telemetry, transit, addressing, compose stack)
  tasks/main.yml                  # config.hcl templating (5.1) + compose stack bring-up (5.2); guarded by 5.3
  handlers/main.yml               # concrete restart handler (docker_compose_v2 recreate)
  templates/config.hcl.j2         # PRIMARY config
  templates/config.unsealer.hcl.j2 # TRANSIT UNSEALER config (no seal stanza)
  meta/main.yml
```

## Compose stacks (task 5.2)

The role builds a service-specific variable set and delegates stack templating
and reconciliation to the generic [`docker-compose-app`](../docker-compose-app/README.md)
role (PF §9, `structure.md`: the shared role every service extends). Two stacks,
selected by `openbao_role`:

- **primary** (`openbao` stack): mounts the Raft data volume (`/openbao/data`),
  the audit-log volume, the rendered `config.hcl` (read-only), and the TLS
  material (read-only host bind-mount, by path). Registers with Traefik via
  Docker labels (`traefik.enable=true`, host `openbao.{{ platform_domain }}`,
  service port 8200) with **no** published host port (PF FR-7). Every
  `OPENBAO_*` config var is passed explicitly in the `.env` with a
  `${VAR:-default}` form (composition-wiring env passthrough); logging is INFO
  to stdout in JSON.
- **unsealer** (`openbao-unsealer` stack): same pinned image, Transit-only
  config, `--memory-swappiness=0` / `mem_swappiness: 0` on the runtime (Req 5.8) —
  the per-container swap cap (#2 in ADR-0006), retained as defence-in-depth —
  **no** Traefik labels, **no** published host port — internal-only. The
  container requests **no** `IPC_LOCK` capability and does **not** mlock:
  `disable_mlock = true` is set in the rendered `config.hcl` (ADR-0006 / the
  OpenBao mlock-removal RFC — Raft-only OpenBao must not mlock).

**No secret value is templated into the Compose stack.** The seal token lives in
the `config.hcl` rendered above (injected at bootstrap from an operator-supplied
external source, then rotated into KV); the TLS key is a mounted file referenced
by path. The `.env` carries non-secret config only.

## Key variables

See [`defaults/main.yml`](./defaults/main.yml) for the full set. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `openbao_role` | `primary` | Selects which config to render (`primary` \| `unsealer`). |
| `openbao_image` | `openbao/openbao:2.4` | Pinned image, pulled through Zot (SVC-14). |
| `openbao_tls_cert_source` | `svc08` | TLS material source (`svc08` EJBCA, or `letsencrypt` fallback). |
| `openbao_storage_path` | `/openbao/data` | Raft integrated-storage path (Req 11). |
| `openbao_telemetry_retention` | `24h` | Prometheus retention (must be ≥ 1 min, Req 10.1). |
| `openbao_unsealer_addr` | `https://10.0.20.11:8200` | Transit seal endpoint on the primary (Req 5.2). |
| `openbao_transit_key_name` | `openbao-unseal` | Transit key name for auto-unseal. |
| `openbao_kv_max_versions` | `10` | KV v2 `max_versions` (consumed by later roles). |

**No secret value lives in this role.** The `seal "transit"` token
(`openbao_transit_token`) is injected at bootstrap from an operator-supplied
external source (a local gitignored `.env` or a GitLab CI/CD protected+masked
variable), then rotated into KV with the external copy removed (task 6.2). It is
never defaulted to a value and never committed.

## Verify the rendered config (offline, Tier-1)

Lint the role's YAML and syntax-check the templating with a throwaway play.
Requires `ansible`/`ansible-lint` on PATH (installed in the project venv);
invoke the venv binaries by absolute path per `dev-workflow.md`.

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/ansible-lint roles/openbao_install
```

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/yamllint roles/openbao_install
```

The structural contract the rendered `config.hcl` files must satisfy
(`tls_disable = false`, no plaintext listener, `seal "transit"` address exactly
`https://10.0.20.11:8200`, Raft path `/openbao/data`, telemetry retention
≥ 1 min, unsealer has no `seal` stanza and no KV/database/`aws`/PKI mount) is
asserted by the config-validation tests added in task 5.4.
