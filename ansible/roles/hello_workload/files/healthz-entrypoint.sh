#!/bin/sh
# healthz-entrypoint.sh — reference "hello-workload" entrypoint
# (consumer-last-mile-hello-workload §A, Req 1.1/1.2)
#
# A trivial HTTP server, run on a STOCK busybox image (no bespoke build), whose
# ONLY job is to make the OpenBao swap-gate OBSERVABLE at the workload layer:
#
#   GET /healthz  -> 200  IFF  the configured secret is present and non-empty
#                 -> 503  otherwise
#
# SECRETS HYGIENE (Req 5.3 / security-standards.md): this script NEVER logs,
# echoes, or writes the secret *value*. It logs only the boolean presence
# ("DATABASE_URL present" / "DATABASE_URL absent"). The CGI handler likewise
# emits only the status, never the secret.
#
# -------------------------------------------------------------------------- #
# PRESENCE SOURCE = the LIVE, mounted Rendered_Secret_File (RSF), not a boot env
# snapshot (bugfix: fix-hello-workload-cold-boot-rsf-race)
# -------------------------------------------------------------------------- #
# The OpenBao Agent sidecar renders the host's scoped secrets into the RSF, and
# the workload mounts the shared rendered dir READ-ONLY at the in-container path
# $HELLO_WORKLOAD_RSF (default /openbao/agent/rendered/secrets.env). That file is
# LIVE inside the container — the Agent can write/rewrite it after the workload
# has already booted.
#
# WHY READ THE RSF FILE (not $DATABASE_URL from the boot env):
# On a cold `docker compose up`, two create-time snapshots used to freeze the
# "secret absent" state and wedge /healthz at 503 until a manual --force-recreate:
#   1. Docker Compose reads `env_file` entries HOST-side at container-CREATE time;
#      if the host RSF is absent/empty at that instant, nothing is injected and
#      the container env is never re-read for its lifetime.
#   2. This entrypoint used to compute presence ONCE at boot from $DATABASE_URL —
#      an empty boot env froze the marker `absent` forever.
# The swap-gate (`depends_on: openbao-agent {condition: service_healthy}`) is
# meant to delay creation until the RSF is non-empty, but the Compose env-snapshot
# race was still observed live. The robust fix: derive presence from the LIVE RSF
# FILE and REFRESH it on a small bounded cadence, so once the Agent writes the RSF
# the workload reflects `present` WITHOUT a recreate — and flips back to `absent`
# if the RSF is emptied/removed. This STRENGTHENS fail-closed: no RSF / empty RSF
# / key-absent RSF ⇒ `absent` ⇒ 503, at boot AND at runtime.
#
# WHY A MARKER THE CGI READS (not a per-request env/file read in the CGI child):
# busybox httpd SANITIZES the CGI environment (it forwards only CGI-standard vars
# and HTTP_* headers, not arbitrary container env vars), so a CGI handler that
# re-reads $DATABASE_URL always sees it empty -> always 503 (the prior
# fix-hello-workload-cgi-secret-visibility bug). The parent process computes the
# boolean presence here and writes ONLY `present`/`absent` to a marker file
# OUTSIDE cgi-bin/ (never executed/served); the CGI reads that marker. Keeping the
# marker contract means the CGI handler is UNCHANGED by this fix — only the SOURCE
# of the marker (the live RSF file) and its REFRESH cadence change.
#
# busybox ships both httpd and the POSIX shell this script needs, so the stock
# `busybox` Docker Official Image needs no extra layer.

set -eu

HELLO_WORKLOAD_PORT="${HELLO_WORKLOAD_PORT:-8080}"
HELLO_WORKLOAD_WEBROOT="${HELLO_WORKLOAD_WEBROOT:-/tmp/hello-workload}"
HELLO_WORKLOAD_SECRET_ENV="${HELLO_WORKLOAD_SECRET_ENV:-DATABASE_URL}"

# The LIVE Rendered_Secret_File, mounted READ-ONLY into the workload by the
# openbao-agent role. Presence is derived from THIS file (overridable for tests).
HELLO_WORKLOAD_RSF="${HELLO_WORKLOAD_RSF:-/openbao/agent/rendered/secrets.env}"

# How often (seconds) the background loop re-derives presence from the live RSF
# and rewrites the marker. Small and bounded so /healthz flips within a few
# seconds of the RSF appearing/disappearing.
HELLO_WORKLOAD_RSF_REFRESH_SECS="${HELLO_WORKLOAD_RSF_REFRESH_SECS:-2}"

# The boolean presence marker the CGI handler reads. It lives directly in the
# webroot (NOT under cgi-bin/), so busybox httpd never executes or serves it as a
# CGI script. It holds ONLY the word `present` or `absent` — never the value.
HELLO_WORKLOAD_SECRET_PRESENT_MARKER="${HELLO_WORKLOAD_WEBROOT}/.healthz-secret-present"

mkdir -p "${HELLO_WORKLOAD_WEBROOT}/cgi-bin"

# --- Derive boolean presence from the LIVE RSF FILE (NEVER the value) --------
# `present` iff the RSF file contains a line `<KEY>=<non-empty value>` for the
# configured KEY ($DATABASE_URL by default). The grep tests the key + a non-empty
# value by EXIT STATUS only; the matched line/value is never printed, assigned, or
# written — only the boolean word `present`/`absent` is derived from it.
compute_presence() {
    if grep -Eq "^${HELLO_WORKLOAD_SECRET_ENV}=.+" "${HELLO_WORKLOAD_RSF}" 2>/dev/null; then
        printf 'present'
    else
        printf 'absent'
    fi
}

# Atomically (re)write the marker with ONLY the boolean word. Temp-file + mv so
# the CGI never reads a half-written marker.
write_marker() {
    _p="$1"
    _tmp="${HELLO_WORKLOAD_SECRET_PRESENT_MARKER}.tmp.$$"
    printf '%s\n' "${_p}" > "${_tmp}"
    mv -f "${_tmp}" "${HELLO_WORKLOAD_SECRET_PRESENT_MARKER}"
}

# Compute presence ONCE before serving (immediate correctness when the RSF is
# already present at boot), log the boolean only, and seed the marker.
_present="$(compute_presence)"
printf 'hello-workload: %s %s\n' "${HELLO_WORKLOAD_SECRET_ENV}" "${_present}"
write_marker "${_present}"

# --- Write the /healthz CGI handler -----------------------------------------
# busybox httpd treats files under <webroot>/cgi-bin/ as CGI scripts. The handler
# reads the boolean presence MARKER (not the secret env var and not the RSF file,
# which busybox would not forward to the CGI child) and returns 200 iff it says
# `present`, 503 otherwise. It emits ONLY the status word, never the secret.
# The marker path is baked into the handler at generation time.
cat > "${HELLO_WORKLOAD_WEBROOT}/cgi-bin/healthz" <<CGI
#!/bin/sh
# /healthz CGI — 200 iff the presence marker says the secret is present. Reads the
# MARKER, never an env var or the RSF (busybox httpd sanitizes arbitrary env vars
# out of the CGI child). Emits only the status; the secret value is NEVER written
# to the response.
_marker="${HELLO_WORKLOAD_SECRET_PRESENT_MARKER}"
if [ "\$(cat "\${_marker}" 2>/dev/null)" = "present" ]; then
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

# --- Background refresh loop: keep the marker tracking the LIVE RSF ----------
# Re-derive presence from the RSF file every N seconds and rewrite the marker, so
# a post-boot RSF write flips /healthz to 200 (and an emptied RSF flips it back to
# 503) WITHOUT a container recreate. Writes ONLY the boolean word — never the
# value. Runs in the background; the busybox httpd below stays PID 1.
# stdin/stdout/stderr are redirected so the loop never holds the server's
# stdio open (it must never print anyway — it only rewrites the boolean marker).
(
    while true; do
        sleep "${HELLO_WORKLOAD_RSF_REFRESH_SECS}"
        write_marker "$(compute_presence)"
    done
) </dev/null >/dev/null 2>&1 &

printf 'hello-workload: serving /healthz on :%s\n' "${HELLO_WORKLOAD_PORT}"

# -f: foreground (so it is PID 1 and Docker tracks its lifecycle);
# -v: log requests; -p: listen port; -h: webroot (cgi-bin discovered beneath it).
exec busybox httpd -f -v -p "${HELLO_WORKLOAD_PORT}" -h "${HELLO_WORKLOAD_WEBROOT}"
