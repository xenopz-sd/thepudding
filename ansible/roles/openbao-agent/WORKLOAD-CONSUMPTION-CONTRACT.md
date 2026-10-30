# Workload secret-consumption contract (openbao-workload-auth)

**Audience:** service authors writing a long-running Compose workload that needs
its secrets (DB password, OIDC client secret, …) at container start.

**Purpose (how-to, Requirement 5):** this is the contract your workload MUST obey
to consume secrets through the one sanctioned day-2 mechanism — the
[`openbao-agent`](./README.md) sidecar that authenticates to OpenBao via **AppRole**
and renders your secrets into a file your container reads at start. It is the
downstream consumption half of the
[`openbao-workload-auth`](../../../.kiro/specs/openbao-workload-auth/) feature; the
control-plane role that mints the AppRole is
[`openbao_workload_approle`](../openbao_workload_approle/README.md).

> **Placeholders only.** Every value below is a placeholder; secrets are
> referenced by **key name** only, never by value — no real credential or real
> hostname appears in this doc (Req 5.6, `documentation-testing.md`,
> `security-standards.md`).

---

## The five rules your workload must follow

### 1. Read secrets ONLY from the Rendered_Secret_File (Req 5.1)

Your workload reads its secrets from the **Rendered_Secret_File** — an
`.env`-shaped `key=value` file the `openbao-agent` sidecar writes, at a
**service-owned, mode-`0600`** path. The Agent renders one line per configured
secret mapping; the KEY on the left is the mapping's `env_key`:

```
# Rendered_Secret_File — written by the Agent at 0600, NEVER committed.
DB_PASSWORD=<rendered-at-runtime>
OIDC_CLIENT_SECRET=<rendered-at-runtime>
```

Your workload layers this file over its own non-secret `.env` (the Compose
`env_file` the `docker-compose-app` role already renders). In the stack the Agent
role builds, the Rendered_Secret_File is wired as an `extra_env_files` entry on the
workload service, so its keys arrive as environment variables at container start.
You do **not** read OpenBao directly, and you do **not** bake any secret into your
image or a committed `.env`.

### 2. Start ONLY after the file exists — the Compose service_healthy gate (Req 5.2)

Your workload container MUST NOT start until the Rendered_Secret_File exists. This
is expressed as a Compose dependency on the Agent sidecar being **healthy**, where
the Agent is healthy only once it has written a **non-empty** Rendered_Secret_File:

```yaml
# (rendered by the openbao-agent role via docker-compose-app)
services:
  openbao-agent:
    # ... bao agent sidecar ...
    healthcheck:
      test: ["CMD", "test", "-s", "/openbao/agent/rendered/secrets.env"]
      # retries derived from openbao_agent_render_timeout
  workload:
    depends_on:
      openbao-agent:
        condition: service_healthy      # <-- the start gate (Req 5.2)
```

`depends_on … condition: service_healthy` on the Agent's RSF-presence healthcheck
is the **swap-gate**: real secrets rendered ⇒ Agent healthy ⇒ workload starts;
Primary sealed/unreachable ⇒ no file ⇒ Agent never healthy ⇒ workload never starts
(`composition-wiring.md` fail-closed unavailability). You do not add logic for
this — you inherit it by consuming the stack the role builds.

### 3. The Rendered_Secret_File is the ONLY day-2 secret source (Req 5.3)

Per [`integration-boundaries.md`](../../../.kiro/steering/integration-boundaries.md)
§2, the Rendered_Secret_File is the **only** sanctioned source of your workload's
day-2 secrets. Do not:

- read a secret from a committed `.env`, an Ansible vars file, or a Terraform
  output;
- hardcode a secret into `compose.yaml` or the image;
- call another secrets service.

The only credentials that legitimately live outside OpenBao are the bootstrap
`role_id`/`secret_id` the Agent itself consumes — delivered over the first-secret
channel (a `0600` host file, or a GitLab CI/CD protected+masked variable — PF
FR-8), never committed.

### 4. HTTP/HTTPS ONLY through Traefik, via Docker labels (Req 5.4)

Your workload exposes web traffic **only** through the shared Traefik instance
(SVC-09), registered via Docker labels
([`integration-boundaries.md`](../../../.kiro/steering/integration-boundaries.md)
§1):

```yaml
  workload:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.<name>.rule=Host(`<workload-host>.<platform-domain>`)"
    networks: ["edge"]
```

### 5. NO host `ports:` for web-facing traffic (Req 5.5)

Your workload MUST NOT map a host `ports:` entry for web-facing traffic — Traefik's
Docker-provider label discovery is the ingress path, not a published host port. A
`ports:` mapping for HTTP/HTTPS violates the ingress boundary.

```yaml
  workload:
    # NO  ports: ["443:8443"]   <-- forbidden for web-facing traffic (Req 5.5)
    labels: [ "traefik.enable=true", ... ]   # ingress is via labels, not ports
```

---

## What an operator observes in degraded mode (Req 6.6)

When OpenBao is sealed or unreachable, or the `secret_id` is invalid/expired, the
contract fails **closed** and the failure is observable:

- The **workload container never reaches a healthy state** within
  `openbao_agent_render_timeout` seconds of start (default 120), and serves no
  traffic — the `depends_on … service_healthy` gate is never satisfied because no
  Rendered_Secret_File was written (Req 6.1, 6.2, 6.5).
- The **`openbao-agent` sidecar logs** either an **authentication failure**
  (invalid/expired `secret_id`, at ERROR level) or a **render failure** (Primary
  sealed) — each distinguishable from a successful render — and retries AppRole
  auto-auth at an interval not exceeding `openbao_agent_retry_interval` seconds
  (default 30) rather than exiting silently (Req 6.3, 6.4).
- **No placeholder, partial, or empty secret is ever written.** A missing OpenBao
  never degrades into a canned value — the workload stays down until real secrets
  render (`composition-wiring.md` unavailability principle).

Operationally, the workload's non-healthy state feeds the existing "Compose
healthcheck failing > 5 min" alert class (`security-standards.md`).

---

## Minimal consuming example (display-only)

A sketch of the two services your stack ends up with — the Agent sidecar renders
the file, the workload gates on it and reads it. This is illustrative of the shape
the `openbao-agent` role produces; it is **not** a file you hand-write (the role
renders it via `docker-compose-app`), so it is display-only:

<!-- doctest: display-only -->
```yaml
services:
  openbao-agent:
    image: openbao/openbao:2.4
    command: ["agent", "-config=/openbao/agent/agent.hcl"]
    volumes:
      - "/opt/compose/<slug>-workload/agent.hcl:/openbao/agent/agent.hcl:ro"
      - "/etc/openbao-workload/<slug>/<slug>-workload-role-id:/openbao/agent/role-id:ro"
      - "/etc/openbao-workload/<slug>/<slug>-workload-secret-id:/openbao/agent/secret-id:ro"
      - "/opt/compose/<slug>-workload/rendered:/openbao/agent/rendered"
    healthcheck:
      test: ["CMD", "test", "-s", "/openbao/agent/rendered/secrets.env"]
      interval: "5s"
    restart: "unless-stopped"
    # NO ports:  (the Agent speaks only to the Primary and writes a local file)
  workload:
    image: <workload-image>
    env_file: [".env"]                                   # non-secret config
    extra_env_files: ["/openbao/agent/rendered/secrets.env"]   # the Rendered_Secret_File (0600)
    depends_on:
      openbao-agent: { condition: "service_healthy" }    # start gate (Req 5.2/6.2)
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.<slug>.rule=Host(`<slug>.<platform-domain>`)"
    networks: ["edge"]
    # NO ports:  (Traefik-only ingress — Req 5.5)
```

---

## Verifying the contract offline (Tier-1)

The swap-gate (the `depends_on … service_healthy` + RSF-presence healthcheck + the
no-`ports:` / Traefik-labels shape) is pinned offline by the composition-root test,
which renders the stack from the real `docker-compose-app` template and runs
`docker compose config`. Run it from the `tests/installer` pytest root (invoke the
venv pytest by absolute path per `dev-workflow.md`):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest tests/installer/test_workload_auth_swap_gate.py -q
```

The live login/render + fail-closed behaviour is the `requires-infra`
provision-then-test walkthrough in the SVC-07 install runbook
([`infra/projects/svc-07-secrets-manager/INSTALL-RUNBOOK.md` §6](../../../infra/projects/svc-07-secrets-manager/INSTALL-RUNBOOK.md)),
runnable only against a throwaway cluster under the `AGENT_LIVE_AUTHORIZED` +
throwaway-guard interlock.
