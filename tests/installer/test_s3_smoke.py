"""Offline tests for scripts/installer/s3_smoke.py (spec svc-03-object-storage, R13/AC-2).

The signer is bound to the AWS-published SigV4 S3 test vector (the "GET Bucket
(List Objects)" example from the S3 API "Authenticating Requests: Using the
Authorization Header" examples), so a regression in canonicalisation fails here,
offline, instead of as a 403 at live run. The two operations are exercised against
a local stub server.
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import threading

import pytest

import s3_smoke

# AWS documentation example credentials (public test vector, not a secret).
_AK = "AKIAIOSFODNN7EXAMPLE"
_SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_NOW = dt.datetime(2013, 5, 24, 0, 0, 0, tzinfo=dt.timezone.utc)


def test_signature_matches_the_aws_list_objects_test_vector():
    headers = s3_smoke.sign_request(
        "GET", "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", b"", _AK, _SK, "us-east-1", _NOW
    )
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert headers["x-amz-content-sha256"] == s3_smoke._EMPTY_SHA256
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, "
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )


def test_payload_hash_is_signed_for_a_put():
    h = s3_smoke.sign_request("PUT", "http://10.0.20.22:3900/b/k", b"abc", _AK, _SK, "garage", _NOW)
    assert h["x-amz-content-sha256"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert "/20130524/garage/s3/aws4_request" in h["Authorization"]


class _Stub(http.server.BaseHTTPRequestHandler):
    store: dict[str, bytes] = {}
    deny = False

    def log_message(self, *a):  # silence
        pass

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        assert self.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
        if self.deny:
            return self._reply(403)
        self.store[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
        self._reply(200)

    def do_GET(self):
        if self.deny:
            return self._reply(403)
        if self.path in self.store:
            return self._reply(200, self.store[self.path])
        self._reply(404)

    def do_DELETE(self):
        self.store.pop(self.path, None)
        self._reply(204)


@pytest.fixture
def stub():
    _Stub.store = {}
    _Stub.deny = False
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _run(monkeypatch, capsys, argv):
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "GKtest")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "not-a-real-secret")
    rc = s3_smoke.main(argv)
    out = json.loads(capsys.readouterr().out)
    return rc, out


def test_roundtrip_succeeds_and_leaves_no_object(stub, monkeypatch, capsys):
    _, url = stub
    rc, out = _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "roundtrip"])
    assert rc == 0 and out["ok"] is True
    assert _Stub.store == {}


def test_roundtrip_fails_when_access_is_refused(stub, monkeypatch, capsys):
    _, url = stub
    _Stub.deny = True
    rc, out = _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "roundtrip"])
    assert rc == 1 and "403" in out["detail"]


def test_expect_denied_passes_only_when_both_calls_are_refused(stub, monkeypatch, capsys):
    _, url = stub
    _Stub.deny = True
    assert _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "expect-denied"])[0] == 0
    _Stub.deny = False
    rc, out = _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "expect-denied"])
    assert rc == 1 and out["ok"] is False


def test_missing_credentials_fail_without_a_request(monkeypatch, capsys):
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    rc = s3_smoke.main(["--endpoint", "http://127.0.0.1:9", "--bucket", "b", "roundtrip"])
    assert rc == 2 and "not set" in capsys.readouterr().out


def test_secret_never_printed(stub, monkeypatch, capsys):
    _, url = stub
    _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "roundtrip"])
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "UNIQUE-SECRET-MARKER")
    s3_smoke.main(["--endpoint", url, "--bucket", "b", "roundtrip"])
    assert "UNIQUE-SECRET-MARKER" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# consumer-object-storage R5 (purge) and R8 (Traefik mode: --connect-to, TLS)
# --------------------------------------------------------------------------- #
import datetime as _dt2
import ssl
import urllib.parse as _up


class _ListingStub(_Stub):
    """Adds ListObjectsV2 and ListMultipartUploads responses, and records Host headers."""

    uploads: list[tuple[str, str]] = []
    hosts: list[str] = []

    def do_GET(self):
        self.hosts.append(self.headers.get("Host", ""))
        path, _, query = self.path.partition("?")
        bucket = path.strip("/").split("/")[0]
        if query.startswith("list-type=2"):
            keys = [k.split("/", 2)[2] for k in self.store if k.startswith(f"/{bucket}/")]
            body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
            xml = ('<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                   f"{body}<IsTruncated>false</IsTruncated></ListBucketResult>").encode()
            return self._reply(200, xml)
        if query.startswith("uploads"):
            body = "".join(f"<Upload><Key>{k}</Key><UploadId>{u}</UploadId></Upload>" for k, u in self.uploads)
            xml = ('<?xml version="1.0"?><ListMultipartUploadsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                   f"{body}</ListMultipartUploadsResult>").encode()
            return self._reply(200, xml)
        return super().do_GET()

    def do_DELETE(self):
        path, _, query = self.path.partition("?")
        if query.startswith("uploadId="):
            uid = _up.unquote(query.split("=", 1)[1])
            type(self).uploads = [u for u in self.uploads if u[1] != uid]
            return self._reply(204)
        self.store.pop(_up.unquote(path), None)
        self._reply(204)


def _serve(handler, tls_ctx=None):
    handler.store = {}
    handler.deny = False
    handler.uploads = []
    handler.hosts = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    if tls_ctx is not None:
        srv.socket = tls_ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_purge_deletes_every_object_and_unfinished_upload(monkeypatch, capsys):
    srv = _serve(_ListingStub)
    try:
        _ListingStub.store.update({"/b/a.txt": b"1", "/b/dir/b.txt": b"2", "/other/c.txt": b"3"})
        _ListingStub.uploads = [("big.bin", "up-1")]
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        rc, out = _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "purge"])
        assert rc == 0 and out["ok"] is True, out
        assert "deleted 2 object(s), aborted 1 upload(s)" == out["detail"]
        assert _ListingStub.store == {"/other/c.txt": b"3"}, "only the named bucket is emptied"
        assert _ListingStub.uploads == []
    finally:
        srv.shutdown()


def test_purge_of_an_empty_bucket_is_a_noop(monkeypatch, capsys):
    srv = _serve(_ListingStub)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        rc, out = _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "purge"])
        assert rc == 0 and out["detail"] == "deleted 0 object(s), aborted 0 upload(s)"
    finally:
        srv.shutdown()


def _test_pki(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = _dt2.datetime.now(_dt2.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test root")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - _dt2.timedelta(days=1)).not_valid_after(now + _dt2.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "s3.example.test")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
            .not_valid_before(now - _dt2.timedelta(days=1)).not_valid_after(now + _dt2.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("s3.example.test")]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_pem = tmp_path / "ca.pem"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem = tmp_path / "leaf.pem"
    cert_pem.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_pem = tmp_path / "leaf.key"
    key_pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_pem, key_pem)
    return ctx, ca_pem


def test_connect_to_reaches_a_tls_route_by_ip_with_the_route_host_name(tmp_path, monkeypatch, capsys):
    ctx, ca_pem = _test_pki(tmp_path)
    srv = _serve(_ListingStub, tls_ctx=ctx)
    try:
        port = srv.server_address[1]
        base = ["--endpoint", f"https://s3.example.test:{port}", "--connect-to", "127.0.0.1", "--bucket", "b"]
        rc, out = _run(monkeypatch, capsys, base + ["--ca-file", str(ca_pem), "roundtrip"])
        assert rc == 0 and out["ok"] is True, out
        assert _ListingStub.hosts and all(h == f"s3.example.test:{port}" for h in _ListingStub.hosts)
    finally:
        srv.shutdown()


def test_tls_is_verified_unless_insecure_is_passed(tmp_path, monkeypatch, capsys):
    ctx, _ = _test_pki(tmp_path)
    srv = _serve(_ListingStub, tls_ctx=ctx)
    try:
        port = srv.server_address[1]
        base = ["--endpoint", f"https://s3.example.test:{port}", "--connect-to", "127.0.0.1", "--bucket", "b"]
        rc, out = _run(monkeypatch, capsys, base + ["roundtrip"])
        assert rc == 1 and "SSL" in out["detail"], "an unknown CA must fail by default (P4)"
        rc, out = _run(monkeypatch, capsys, base + ["--insecure", "roundtrip"])
        assert rc == 0, out
    finally:
        srv.shutdown()


def test_list_op_is_a_read_only_check(monkeypatch, capsys):
    srv = _serve(_ListingStub)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        rc, out = _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "list"])
        assert rc == 0 and out == {"ok": True, "op": "list", "detail": "LIST returned 200"}
        assert _ListingStub.store == {}
    finally:
        srv.shutdown()


def test_bucket_create_and_delete_ops(monkeypatch, capsys):
    srv = _serve(_ListingStub)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        rc, out = _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "create-bucket"])
        assert rc == 0 and out["op"] == "create-bucket"
        rc, out = _run(monkeypatch, capsys, ["--endpoint", url, "--bucket", "b", "delete-bucket"])
        assert rc == 0 and out["op"] == "delete-bucket"
    finally:
        srv.shutdown()
