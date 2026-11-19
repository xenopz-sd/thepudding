#!/usr/bin/env python3
"""Minimal S3 client for the SVC-03 smoke, isolation and teardown steps (stdlib only).

Specs: .kiro/specs/svc-03-object-storage (R13, AC-2) and
.kiro/specs/consumer-object-storage (R5, R8). The control node has no boto3/aws-cli,
and the steps need only a handful of object operations, so this signs path-style
requests with AWS Signature Version 4 using the standard library.

Credentials come ONLY from the environment (S3_ACCESS_KEY_ID /
S3_SECRET_ACCESS_KEY), never argv, and are never printed.

Usage:
  s3_smoke.py --endpoint http://10.0.20.22:3900 --bucket B roundtrip
  s3_smoke.py --endpoint ... --bucket B expect-denied
  s3_smoke.py --endpoint ... --bucket B purge
  s3_smoke.py --endpoint https://s3.<domain> --connect-to 10.0.20.12 --ca-file root.pem --bucket B roundtrip

``roundtrip`` puts an object, reads it back, compares, deletes it. ``list`` only
lists the bucket (read check, used after a key rotation). ``create-bucket`` /
``delete-bucket`` are plain S3 bucket calls, used to check an S3 server that has no
Garage admin API (the SVC-03 fallback, WS-C54). ``expect-denied``
exits 0 only when a put AND a list are both refused (HTTP 403). ``purge`` deletes
every object and aborts every unfinished multipart upload in the bucket (used by
the consumer teardown before the bucket is deleted).

``--connect-to IP`` opens the TCP connection to IP while sending and signing the
endpoint's host name (Host header and TLS SNI): this reaches a Traefik route without
DNS. ``--ca-file`` verifies TLS against that CA bundle; ``--insecure`` (default off,
dev only: the self-signed Traefik mock) skips certificate verification.
Output is one JSON line: {"ok": bool, "op": ..., "detail": ...}.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import sys
import urllib.parse
import xml.etree.ElementTree as ET

_SERVICE = "s3"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, _SERVICE)
    return _hmac(k, "aws4_request")


def _q(text: str) -> str:
    return urllib.parse.quote(text, safe="-_.~")


def sign_request(method: str, url: str, body: bytes, access_key: str, secret: str,
                 region: str, now: _dt.datetime) -> dict[str, str]:
    """Return the headers (incl. Authorization) for a SigV4-signed path-style request."""
    parts = urllib.parse.urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256
    host = parts.netloc
    canonical_uri = urllib.parse.quote(parts.path or "/", safe="/-_.~")
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    canonical_query = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(query))
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, signed, payload_hash]
    )
    scope = f"{date}/{region}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    signature = hmac.new(signing_key(secret, date, region), string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    out = {k: v for k, v in headers.items() if k != "host"}
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    return out


class _SniHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ``connect_host`` with TLS SNI / hostname check for ``sni``."""

    def __init__(self, connect_host: str, port: int, sni: str, context: ssl.SSLContext, timeout: float):
        super().__init__(connect_host, port, context=context, timeout=timeout)
        self._sni = sni
        self._ctx = context

    def connect(self) -> None:  # pragma: no cover - exercised through the stub-server tests
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self._sni)


class Client:
    def __init__(self, endpoint: str, creds: tuple[str, str], region: str,
                 connect_to: str | None = None, ca_file: str | None = None, insecure: bool = False):
        self.endpoint = endpoint.rstrip("/")
        self.creds = creds
        self.region = region
        parts = urllib.parse.urlsplit(self.endpoint)
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.netloc = parts.netloc
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.connect_to = connect_to or self.host
        self.ctx = None
        if self.scheme == "https":
            if insecure:
                self.ctx = ssl.create_default_context()
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE
            else:
                self.ctx = ssl.create_default_context(cafile=ca_file)

    def call(self, method: str, path: str, query: str = "", body: bytes = b"") -> tuple[int, bytes]:
        url = f"{self.endpoint}{path}" + (f"?{query}" if query else "")
        headers = sign_request(method, url, body, self.creds[0], self.creds[1], self.region,
                               _dt.datetime.now(_dt.timezone.utc))
        headers["Host"] = self.netloc
        if body:
            headers["Content-Length"] = str(len(body))
        if self.scheme == "https":
            conn = _SniHTTPSConnection(self.connect_to, self.port, self.host, self.ctx, 30)
        else:
            conn = http.client.HTTPConnection(self.connect_to, self.port, timeout=30)
        try:
            target = urllib.parse.quote(path, safe="/-_.~") + (f"?{query}" if query else "")
            conn.request(method, target, body=body or None, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()


def roundtrip(c: Client, bucket: str) -> dict:
    path = f"/{bucket}/svc03-smoke.txt"
    payload = b"svc03 smoke " + _dt.datetime.now(_dt.timezone.utc).isoformat().encode()
    put, _ = c.call("PUT", path, body=payload)
    if put != 200:
        return {"ok": False, "op": "roundtrip", "detail": f"PUT returned {put}"}
    get, data = c.call("GET", path)
    if get != 200 or data != payload:
        return {"ok": False, "op": "roundtrip", "detail": f"GET returned {get}, match={data == payload}"}
    delete, _ = c.call("DELETE", path)
    if delete not in (200, 204):
        return {"ok": False, "op": "roundtrip", "detail": f"DELETE returned {delete}"}
    return {"ok": True, "op": "roundtrip", "detail": "put, get (content matched) and delete succeeded"}


def expect_denied(c: Client, bucket: str) -> dict:
    put, _ = c.call("PUT", f"/{bucket}/svc03-isolation-probe.txt", body=b"probe")
    lst, _ = c.call("GET", f"/{bucket}", query="list-type=2")
    ok = put == 403 and lst == 403
    return {"ok": ok, "op": "expect-denied", "detail": f"PUT returned {put}, LIST returned {lst} (both must be 403)"}


def _texts(xml: bytes, tag: str) -> list[str]:
    root = ET.fromstring(xml)
    return [e.text or "" for e in root.iter() if e.tag in (tag, _S3_NS + tag)]


def _uploads(xml: bytes) -> list[tuple[str, str]]:
    root = ET.fromstring(xml)
    out = []
    for up in root.iter():
        if up.tag in ("Upload", _S3_NS + "Upload"):
            key = next((e.text for e in up if e.tag in ("Key", _S3_NS + "Key")), "")
            uid = next((e.text for e in up if e.tag in ("UploadId", _S3_NS + "UploadId")), "")
            out.append((key or "", uid or ""))
    return out


def purge(c: Client, bucket: str) -> dict:
    deleted = aborted = 0
    token = ""
    for _ in range(10000):
        query = "list-type=2" + (f"&continuation-token={_q(token)}" if token else "")
        status, body = c.call("GET", f"/{bucket}", query=query)
        if status != 200:
            return {"ok": False, "op": "purge", "detail": f"LIST returned {status}"}
        for key in _texts(body, "Key"):
            st, _ = c.call("DELETE", f"/{bucket}/{key}")
            if st not in (200, 204):
                return {"ok": False, "op": "purge", "detail": f"DELETE of an object returned {st}"}
            deleted += 1
        truncated = (_texts(body, "IsTruncated") or ["false"])[0] == "true"
        token = (_texts(body, "NextContinuationToken") or [""])[0]
        if not truncated:
            break
    status, body = c.call("GET", f"/{bucket}", query="uploads=")
    if status == 200:
        for key, uid in _uploads(body):
            st, _ = c.call("DELETE", f"/{bucket}/{key}", query=f"uploadId={_q(uid)}")
            if st not in (200, 204):
                return {"ok": False, "op": "purge", "detail": f"abort of a multipart upload returned {st}"}
            aborted += 1
    status, body = c.call("GET", f"/{bucket}", query="list-type=2")
    if status != 200 or _texts(body, "Key"):
        return {"ok": False, "op": "purge", "detail": f"bucket not empty after purge (LIST {status})"}
    return {"ok": True, "op": "purge", "detail": f"deleted {deleted} object(s), aborted {aborted} upload(s)"}


def list_bucket(c: Client, bucket: str) -> dict:
    status, _ = c.call("GET", f"/{bucket}", query="list-type=2")
    return {"ok": status == 200, "op": "list", "detail": f"LIST returned {status}"}


def create_bucket(c: Client, bucket: str) -> dict:
    status, _ = c.call("PUT", f"/{bucket}")
    return {"ok": status == 200, "op": "create-bucket", "detail": f"PUT bucket returned {status}"}


def delete_bucket(c: Client, bucket: str) -> dict:
    status, _ = c.call("DELETE", f"/{bucket}")
    return {"ok": status in (200, 204), "op": "delete-bucket", "detail": f"DELETE bucket returned {status}"}


_OPS = {"roundtrip": roundtrip, "expect-denied": expect_denied, "purge": purge, "list": list_bucket,
        "create-bucket": create_bucket, "delete-bucket": delete_bucket}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", default="garage")
    ap.add_argument("--connect-to", default=None, help="TCP target (IP) while signing the endpoint host")
    ap.add_argument("--ca-file", default=None, help="CA bundle to verify TLS against")
    ap.add_argument("--insecure", action="store_true", help="DEV ONLY: skip TLS certificate verification")
    ap.add_argument("op", choices=sorted(_OPS))
    args = ap.parse_args(argv)
    access = os.environ.get("S3_ACCESS_KEY_ID", "")
    secret = os.environ.get("S3_SECRET_ACCESS_KEY", "")
    if not access or not secret:
        print(json.dumps({"ok": False, "op": args.op, "detail": "S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY not set"}))
        return 2
    client = Client(args.endpoint, (access, secret), args.region,
                    connect_to=args.connect_to, ca_file=args.ca_file, insecure=args.insecure)
    try:
        result = _OPS[args.op](client, args.bucket)
    except (OSError, ssl.SSLError, http.client.HTTPException, ET.ParseError) as err:
        result = {"ok": False, "op": args.op, "detail": f"request failed: {type(err).__name__}: {err}"}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
