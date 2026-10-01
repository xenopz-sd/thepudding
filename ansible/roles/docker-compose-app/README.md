# `docker-compose-app` Ansible role (generic)

The shared, generic role every service extends (PF §9, `structure.md`). Given a
service-specific variable set, it:

1. templates a `compose.yaml` and a `.env` from Jinja2 onto the target host, and
2. reconciles the stack **declaratively** via `community.docker.docker_compose_v2`
   — never a shell `docker compose up` (PF §9 idempotency rule).

New per-service Ansible logic should build a variable set and invoke this role,
rather than writing a bespoke compose-templating role (`structure.md`).

## Boundaries this role enforces

- **No host `ports:`.** Web-facing services register with Traefik via Docker
  labels (PF FR-7 / `integration-boundaries` §1). The template deliberately does
  not emit a `ports:` block; a service needing a host port must handle that in
  its own bespoke role.
- **No secret values.** The `.env` this role renders carries **non-secret config
  only**. Runtime secrets are fetched from OpenBao (SVC-07) by the Agent at
  container-start time (`integration-boundaries` §2, PF §11). The `dca_env`
  mapping passed in must never contain a real credential value — only config or
  reference-by-name.

## Variable set

See [`defaults/main.yml`](./defaults/main.yml). Highlights:

| Variable | Purpose |
|---|---|
| `dca_app_name` | Stack name; also the Compose project name and stack directory name. |
| `dca_stack_dir` | Where `compose.yaml`/`.env` are written (default `/opt/compose/<app>`). |
| `dca_services` | List of compose service dicts (image, command, volumes, labels, networks, `mem_swappiness`, `runtime_options`). |
| `dca_volumes` | Named volumes declared at the top level. |
| `dca_networks` | External/pre-existing networks to attach (e.g. Traefik's edge net). |
| `dca_env` | Flat `KEY: value` mapping rendered into `.env` (non-secret config only). |
| `dca_state` | `present` (default) brings the stack up; a caller handler can force recreate. |

## Example invocation

A calling role builds the variable set and includes this role, e.g.:

```yaml
- name: Deploy the stack
  ansible.builtin.include_role:
    name: docker-compose-app
  vars:
    dca_app_name: "myservice"
    dca_services:
      - name: myservice
        image: "example/myservice:1.2"
        labels:
          - "traefik.enable=true"
          - "traefik.http.routers.myservice.rule=Host(`myservice.example.test`)"
        networks: ["edge"]
    dca_networks:
      - { name: "edge", external: true }
    dca_env:
      MYSERVICE_LOG_LEVEL: "info"
```

## Verify the rendered stack (offline, Tier-1)

Lint the role and validate that a rendered `compose.yaml` parses. Requires
`ansible`/`ansible-lint` on PATH (project venv); invoke venv binaries by
absolute path per `dev-workflow.md`.

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/ansible-lint roles/docker-compose-app
```

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/yamllint roles/docker-compose-app
```


## Private VLAN-bound ports (opt-in exception to PF FR-7)

`ports:` is NOT emitted by default — web-facing services use Traefik labels
(PF FR-7). A service MAY set an explicit `ports:` list on its `dca_services`
entry ONLY for a **private, VLAN-bound** port that is not LAN/Traefik ingress —
bound to the guest's VLAN IP (e.g. `"10.0.20.11:8200:8200"`), never `0.0.0.0`.
The sole current use is the SVC-07 Transit unsealer, whose seal API must be
reachable by the primary OpenBao over VLAN 20 (a different LXC). This does not
open a Traefik/LAN ingress path, so it does not violate FR-7. See
`.kiro/steering/mock-stand-ins.md` is unrelated; the rationale lives in the
`compose.yaml.j2` header note and the `openbao_install` unsealer stack comment.
