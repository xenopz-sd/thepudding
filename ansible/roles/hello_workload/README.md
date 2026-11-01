# `hello_workload` Ansible role — the reference "hello-workload"

The **§A reference workload** for the
[`consumer-last-mile-hello-workload`](../../../.kiro/specs/consumer-last-mile-hello-workload/)
feature (Req 1.1, 1.2). It is a trivial HTTP server whose `/healthz` endpoint
returns **200 only when the agent-rendered `DATABASE_URL` is present**, and a
non-200 (503) when it is absent — which is what makes the OpenBao **swap-gate
observable at the workload layer**: no rendered secret ⇒ not healthy.

This role carries **no stand-up logic of its own**. The reference workload is
stood up by the existing [`openbao-agent`](../openbao-agent/README.md) role
(spec task 4.2), which builds the Agent sidecar + workload Compose stack via
[`docker-compose-app`](../docker-compose-app/README.md). This role is the
discoverable **reference variable set + entrypoint artifact** that task 4.2
threads into that path — no new agent wiring.

> **Placeholders / no secrets.** `DATABASE_URL` is referenced by **key name**
> only; its value is rendered at container start into the 0600
> Rendered_Secret_File by the Agent (the v0.11.3 `{db, database_url,
> DATABASE_URL}` bridge), never committed, templated, or echoed. The entrypoint
> logs only the boolean presence (`DATABASE_URL present` / `absent`), never the
> value.

## Image choice — stock image + tiny entrypoint (NOT a bespoke build)

Per the design decision in `design.md` "Component 3", the reference uses a
**stock minimal image plus a tiny mounted entrypoint**, not a bespoke-built
image:

- `hello_workload_image` defaults to **`busybox:1.37`** — a Docker Official
  Image that ships both `httpd` (with CGI) and a POSIX shell. That is enough to
  express "200 iff `DATABASE_URL` present" with a **mounted** entrypoint
  ([`files/healthz-entrypoint.sh`](./files/healthz-entrypoint.sh)) — so there is
  **no Dockerfile, no SVC-14 build step**, and nothing to keep EU-sourcing-clean
  beyond an upstream official base.
- A bespoke `Dockerfile` built/pushed via the platform registry (SVC-14) is the
  **fallback only** — it was not needed, because the stock image can express the
  required behaviour.

## What the entrypoint does (`/healthz` 200-iff-secret)

[`files/healthz-entrypoint.sh`](./files/healthz-entrypoint.sh), run on the stock
`busybox` image:

1. logs the **boolean presence** of the configured secret env
   (`DATABASE_URL` by default) — never its value;
2. writes a `busybox httpd` **CGI handler** at `<webroot>/cgi-bin/healthz` that
   re-reads the environment **per request** and emits
   `Status: 200` iff the secret is present/non-empty, else `Status: 503`
   (emitting only the status, never the secret);
3. `exec`s `busybox httpd -f -p <port> -h <webroot>`.

`DATABASE_URL` arrives at container start from the Agent-rendered
Rendered_Secret_File (the `openbao-agent` role layers it as an `extra_env_files`
entry — task 4.2). The entrypoint reads it from the environment only.

## Variable set (consumed by task 4.2)

See [`defaults/main.yml`](./defaults/main.yml). Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `hello_workload_image` | `busybox:1.37` | Stock reference image (not bespoke-built). |
| `hello_workload_secret_env` | `DATABASE_URL` | Env key the workload gates `/healthz` on. |
| `hello_workload_http_port` | `8080` | `/healthz` listen port (no host port — Traefik-only ingress). |
| `hello_workload_command` | `sh /usr/local/bin/healthz-entrypoint.sh` | Runs the mounted entrypoint on the stock image. |
| `hello_workload_healthcheck` | `wget … /cgi-bin/healthz` | Compose healthcheck — healthy only once `/healthz` is 200. |
| `hello_workload_secret_mappings` | `[{db, database_url, DATABASE_URL}]` | The v0.11.3 default DB agent mapping task 4.2 passes as `openbao_agent_secret_mappings`. |

The workload is **Traefik-only ingress with no host port** — the
`openbao-agent` role the stand-up composes already emits Traefik labels and no
`ports:` (the structural assertion is task 4.3).

## Verify the entrypoint (offline, Tier-1)

The `/healthz` 200-iff-`DATABASE_URL`-present logic is exercised offline — no
live DB — by launching the real entrypoint (busybox `httpd`) and probing
`/healthz` with the secret env set vs unset. Invoke the venv pytest by absolute
path per `dev-workflow.md`:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest tests/installer/test_hello_workload_entrypoint.py -q
```

Lint the entrypoint shell script (skipped if `shellcheck` is unavailable):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
shellcheck ansible/roles/hello_workload/files/healthz-entrypoint.sh
```

The dedicated structural swap-gate smoke (depends_on `service_healthy`, no host
port, Traefik label, reads the RSF) is spec task 4.3; the live happy-path +
fail-closed walkthrough is the `requires-infra` smoke in task 5.

## §D dev issuer-reachability recipe — conditional NOT triggered here

Requirement 4.2 is **conditional**: _"WHEN the reference hello-workload (§A)
includes an SSO-consuming component THEN it SHALL demonstrate the dev
[issuer-reachability] recipe."_ (design.md Component 3 makes the OIDC/JWKS fetch
**optional** — "optionally, performs an OIDC JWKS fetch".)

This reference **does not include an SSO-consuming component** — it is a trivial
busybox `/healthz` server that gates only on the presence of the agent-rendered
`DATABASE_URL`. It performs **no** OIDC discovery, **no** JWKS fetch, and
validates **no** ZITADEL tokens. So **Req 4.2's conditional is not triggered**,
and this role deliberately carries **no `extra_hosts` / JWKS dev overlay** — a
conscious "conditional not triggered" outcome, not a silent omission. Adding an
opt-in SSO overlay would introduce an SSO component the reference intentionally
does not have, contradicting the design's "keep §A dependency-light" decision
(stock busybox image + tiny entrypoint, no bespoke build).

The dev recipe itself (the §D `extra_hosts` alias
`auth.platform.example.test` → Traefik `10.0.20.12`, plus the dev self-signed
JWKS posture) is authored once as the **copyable, display-only Compose fragment**
in the SSO section of the consumer journey — see
[`docs/CONSUMER-JOURNEY.md` → "SSO consumption: dev issuer-reachability recipe (§D)"](../../../docs/CONSUMER-JOURNEY.md#sso-consumption-dev-issuer-reachability-recipe-d).
A consumer whose workload **does** validate ZITADEL tokens copies that fragment
onto their workload service as a dev-only overlay (flagged dev-only; the
production posture is the SVC-08/EJBCA trusted-CA path, never the skip-verify
switch). This is the §D demonstration Req 4.2 points at; it is not duplicated
here.
