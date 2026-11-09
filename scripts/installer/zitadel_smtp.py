#!/usr/bin/env python3
"""Idempotently point ZITADEL's INSTANCE email provider at the shared dev mail catcher.

Spec: .kiro/specs/dev-mail-catcher (R4-R6), ADR-0025. DEV-ONLY permanent mock stand-in
(mock-stand-ins.md, Mock 7). Stdlib only, same style as ``zitadel_provision.py``.

Design rules enforced here:

* **Token via ENV only.** The IAM_OWNER access token is read from
  ``ZITADEL_PROVISION_TOKEN`` and sent in the Authorization header. It is never an
  argument, never printed, never in the result JSON.
* **Never clobber a real provider.** The catcher provider is tagged with
  ``MARKER_DESCRIPTION``. If a provider that does NOT carry the marker is the active
  one (a real SMTP relay, or an HTTP provider), the helper REFUSES (exit 4) and changes
  nothing unless ``--replace-existing`` is given. Enabling the mock must never
  silently redirect real users' mail into a sink (mock-stand-ins.md coexistence caveat).
* **Idempotent + reconciling.** Already matching and active => no mutation (``changed``
  false). Marker present but host/port/sender drifted => PUT update. Nothing set => add
  then activate. A read-back FAIL-LOUD gate (exit 3) verifies the final active state.
* **No outbound relay.** This only configures where ZITADEL SENDS; it does not touch the
  catcher, which itself never relays (R13).

REST shapes (admin API, base path ``/admin/v1``), verified 2026-11-07 against the
upstream ``proto/zitadel/admin.proto`` and ``settings.proto`` on ``main``:
``GET /email`` (active), ``POST /email/smtp`` (add, only if nothing is set),
``PUT /email/smtp/{id}`` (update; activates on save), ``POST /email/{id}/_activate``,
``DELETE /email/{id}``. Recorded in tests/fixtures/live-shapes/zitadel-email-provider-smtp.json and
LIVE-CONFIRMED on the pinned ZITADEL v4.18.0 (WS-C16).

Exit codes: 0 ok, 2 bad arguments / missing token, 3 API error or failed read-back,
4 refused (a non-catcher provider is active).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any

TOKEN_ENV_VAR = "ZITADEL_PROVISION_TOKEN"
MARKER_DESCRIPTION = "dev-mail-catcher (Mock 7, ADR-0025)"
EP_EMAIL = "/admin/v1/email"
EP_EMAIL_SMTP = "/admin/v1/email/smtp"
EP_EMAIL_SMTP_ID = "/admin/v1/email/smtp/{id}"
EP_EMAIL_ID = "/admin/v1/email/{id}"
EP_EMAIL_ACTIVATE = "/admin/v1/email/{id}/_activate"
STATE_ACTIVE = "EMAIL_PROVIDER_ACTIVE"
_DEFAULT_TIMEOUT = 30


class ZitadelError(Exception):
    """A non-recoverable ZITADEL API error or a failed read-back."""


class RefusedError(Exception):
    """A real (non-catcher) provider is active; refusing to replace it."""


def desired_body(host: str, port: int, sender_address: str, sender_name: str) -> dict[str, Any]:
    """The AddEmailProviderSMTP / UpdateEmailProviderSMTP body for an unauthenticated,
    unencrypted dev catcher (``none`` auth, ``tls`` false). ``host`` carries the port."""
    return {
        "senderAddress": sender_address,
        "senderName": sender_name,
        "tls": False,
        "host": f"{host}:{port}",
        "description": MARKER_DESCRIPTION,
        "none": {},
    }


class Client:
    def __init__(self, base_url: str, token: str, *, host_header: str | None = None,
                 insecure: bool = False, timeout: int = _DEFAULT_TIMEOUT):
        self._base = base_url.rstrip("/")
        self._token = token
        self._host_header = host_header
        self._timeout = timeout
        self._ctx = ssl.create_default_context()
        if insecure:  # dev/mock self-signed cert only
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self._host_header:
            req.add_header("Host", self._host_header)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"message": raw[:200]}
            if exc.code == 404:
                return 404, parsed
            raise ZitadelError(
                f"ZITADEL API {method} {path} failed: HTTP {exc.code} {parsed.get('message', raw[:200])}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ZitadelError(f"ZITADEL API {method} {path} unreachable: {exc}") from exc


def get_active(client: Client) -> dict | None:
    """The active email provider (the ``config`` object) or None when nothing is set."""
    status, body = client.request("GET", EP_EMAIL)
    if status == 404:
        return None
    cfg = body.get("config")
    return cfg if cfg else None


def _is_ours(cfg: dict | None) -> bool:
    return bool(cfg) and cfg.get("description") == MARKER_DESCRIPTION


def _matches(cfg: dict, want: dict) -> bool:
    smtp = cfg.get("smtp") or {}
    return (
        cfg.get("state") == STATE_ACTIVE
        # Ownership tag is part of "matching": an identical-looking provider that is NOT ours must
        # be re-tagged by a PUT (only reachable with --replace-existing), never judged unchanged.
        # Found live (WS-C16): without this the marker was never applied and the read-back refused.
        and cfg.get("description") == MARKER_DESCRIPTION
        and smtp.get("host") == want["host"]
        and bool(smtp.get("tls", False)) is False
        and smtp.get("senderAddress") == want["senderAddress"]
        and smtp.get("senderName") == want["senderName"]
        and ("none" in smtp or not smtp.get("user"))
    )


def ensure_catcher(client: Client, host: str, port: int, sender_address: str, sender_name: str,
                   *, replace_existing: bool = False) -> dict[str, Any]:
    """Converge the instance email provider to the dev catcher. Returns a non-secret result."""
    want = desired_body(host, port, sender_address, sender_name)
    cur = get_active(client)
    action = "unchanged"
    provider_id = (cur or {}).get("id", "")

    if cur is not None and not _is_ours(cur):
        if not replace_existing:
            kind = "SMTP" if cur.get("smtp") else ("HTTP" if cur.get("http") else "unknown")
            raise RefusedError(
                f"a non-catcher email provider is active (type {kind}, description "
                f"{cur.get('description', '')!r}); refusing to redirect real mail into the dev catcher. "
                "Remove it deliberately or pass --replace-existing."
            )
    if cur is not None and not (cur.get("smtp") or {}) and not _is_ours(cur):
        # replace_existing with a non-SMTP provider: ADD requires "nothing set yet"
        raise ZitadelError("cannot add an SMTP provider while a non-SMTP provider is set; remove it first")

    if cur is not None and (cur.get("smtp") is not None) and _matches(cur, want):
        action = "unchanged"
    elif cur is not None and cur.get("smtp") is not None:
        status, _ = client.request("PUT", EP_EMAIL_SMTP_ID.format(id=provider_id), {**want, "id": provider_id})
        action = "updated"
    else:
        status, resp = client.request("POST", EP_EMAIL_SMTP, want)
        provider_id = str(resp.get("id") or "")
        if not provider_id:
            raise ZitadelError("add email provider returned no id")
        client.request("POST", EP_EMAIL_ACTIVATE.format(id=provider_id), {})
        action = "added"

    # Read-back FAIL-LOUD gate: committed is not the same as active.
    final = get_active(client)
    if final is None or not _matches(final, want) or not _is_ours(final):
        raise ZitadelError(
            "email provider read-back does not match the dev catcher after reconcile "
            f"(expected active SMTP {want['host']}); not reporting success"
        )
    return {"action": action, "changed": action != "unchanged", "provider_id": final.get("id", provider_id),
            "host": want["host"], "state": final.get("state")}


def remove_catcher(client: Client) -> dict[str, Any]:
    """Remove the provider ONLY if it carries the catcher marker."""
    cur = get_active(client)
    if cur is None:
        return {"action": "absent", "changed": False}
    if not _is_ours(cur):
        raise RefusedError("the active email provider is not the dev catcher; refusing to remove it")
    client.request("DELETE", EP_EMAIL_ID.format(id=cur["id"]))
    after = get_active(client)
    if after is not None and _is_ours(after):
        raise ZitadelError("catcher provider still present after DELETE")
    return {"action": "removed", "changed": True, "provider_id": cur["id"]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Point ZITADEL's instance email provider at the dev mail catcher. "
                                f"Token is read from {TOKEN_ENV_VAR}, never a flag.")
    p.add_argument("--base-url", required=True)
    p.add_argument("--smtp-host", required=True, help="catcher VLAN-20 IP or name (no port)")
    p.add_argument("--smtp-port", type=int, default=1025)
    p.add_argument("--sender-address", required=True)
    p.add_argument("--sender-name", default="ZITADEL (dev)")
    p.add_argument("--replace-existing", action="store_true",
                   help="allow replacing a non-catcher SMTP provider (dangerous: redirects real mail)")
    p.add_argument("--remove", action="store_true", help="remove the provider if (and only if) it is the catcher")
    p.add_argument("--host-header", default=None)
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (dev self-signed cert only)")
    args = p.parse_args(argv)

    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        print(f"ERROR: {TOKEN_ENV_VAR} is not set in the environment. The token must be supplied "
              "via that env var, never on the command line.", file=sys.stderr)
        return 2
    if not (1 <= args.smtp_port <= 65535):
        print("ERROR: --smtp-port out of range", file=sys.stderr)
        return 2

    client = Client(args.base_url, token, host_header=args.host_header, insecure=args.insecure)
    try:
        if args.remove:
            result = remove_catcher(client)
        else:
            result = ensure_catcher(client, args.smtp_host, args.smtp_port, args.sender_address,
                                    args.sender_name, replace_existing=args.replace_existing)
    except RefusedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    except ZitadelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
