#!/bin/sh
# healthz-entrypoint.sh — reference "hello-workload" entrypoint
# (consumer-last-mile-hello-workload §A, Req 1.1/1.2)
#
# A trivial HTTP server, run on a STOCK busybox image (no bespoke build), whose
# ONLY job is to make the OpenBao swap-gate OBSERVABLE at the workload layer:
#
#   GET /healthz  -> 200  IFF  $DATABASE_URL is present and non-empty
#                 -> 503  otherwise
#
# DATABASE_URL arrives at container start from the agent-rendered
# Rendered_Secret_File (layered as an extra_env_files entry by the openbao-agent
# role — task 4.2), NEVER from a committed/templated secret. This script reads it
# from the environment only.
#
# SECRETS HYGIENE (Req 5.3 / security-standards.md): this script NEVER logs,
# echoes, or writes the DATABASE_URL *value*. It logs only the boolean presence
# ("DATABASE_URL present" / "DATABASE_URL absent"). The CGI handler likewise
# emits only the status, never the secret.
#
# Implemented with busybox httpd + a CGI handler so the 200-vs-503 decision is
# re-evaluated PER REQUEST against the live environment (not frozen at boot).
# busybox ships both httpd and the POSIX shell this script needs, so the stock
# `busybox` Docker Official Image needs no extra layer.

set -eu

HELLO_WORKLOAD_PORT="${HELLO_WORKLOAD_PORT:-8080}"
HELLO_WORKLOAD_WEBROOT="${HELLO_WORKLOAD_WEBROOT:-/tmp/hello-workload}"
HELLO_WORKLOAD_SECRET_ENV="${HELLO_WORKLOAD_SECRET_ENV:-DATABASE_URL}"

# --- Presence log (boolean only — NEVER the value) --------------------------
# Resolve the configured secret var ($DATABASE_URL by default) indirectly so the
# value is never expanded into the log line.
if [ -n "$(eval "printf '%s' \"\${${HELLO_WORKLOAD_SECRET_ENV}:-}\"")" ]; then
    printf 'hello-workload: %s present\n' "${HELLO_WORKLOAD_SECRET_ENV}"
else
    printf 'hello-workload: %s absent\n' "${HELLO_WORKLOAD_SECRET_ENV}"
fi

# --- Write the /healthz CGI handler -----------------------------------------
# busybox httpd treats files under <webroot>/cgi-bin/ as CGI scripts. The
# handler re-reads the env PER REQUEST and returns 200 iff the secret is present,
# 503 otherwise. It emits ONLY the status word, never the secret value.
mkdir -p "${HELLO_WORKLOAD_WEBROOT}/cgi-bin"

cat > "${HELLO_WORKLOAD_WEBROOT}/cgi-bin/healthz" <<CGI
#!/bin/sh
# /healthz CGI — 200 iff the rendered secret is present (re-checked per request).
# Emits only the status; the secret value is NEVER written to the response.
_secret_name="\${HELLO_WORKLOAD_SECRET_ENV:-DATABASE_URL}"
_secret_val="\$(eval "printf '%s' \"\\\${\${_secret_name}:-}\"")"
if [ -n "\${_secret_val}" ]; then
    printf 'Status: 200 OK\r\n'
    printf 'Content-Type: text/plain\r\n'
    printf '\r\n'
    printf 'hello-workload healthy\n'
else
    printf 'Status: 503 Service Unavailable\r\n'
    printf 'Content-Type: text/plain\r\n'
    printf '\r\n'
    printf 'hello-workload not ready: secret absent\n'
fi
CGI
chmod 0755 "${HELLO_WORKLOAD_WEBROOT}/cgi-bin/healthz"

printf 'hello-workload: serving /healthz on :%s\n' "${HELLO_WORKLOAD_PORT}"

# -f: foreground (so it is PID 1 and Docker tracks its lifecycle);
# -v: log requests; -p: listen port; -h: webroot (cgi-bin discovered beneath it).
exec busybox httpd -f -v -p "${HELLO_WORKLOAD_PORT}" -h "${HELLO_WORKLOAD_WEBROOT}"
