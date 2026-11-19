"""WS-C54 — the SVC-03 fallback (Scality CloudServer) honours the same S3 client contract as Garage.

Runs the pinned CloudServer image locally (in-memory backend, its two built-in sample accounts) and drives it with
the same ``scripts/installer/s3_smoke.py`` operations the platform uses against Garage: create a bucket, round trip,
list, purge, delete, and an isolation check (the second account is refused on the first account's bucket). If it
passes, a client that works against Garage works against the fallback without code changes.

Marked ``requires_infra`` (it pulls an image and runs a container), so the offline gate deselects it. Run once with:

  ~/venv/devinfra/bin/pytest -m requires_infra tests/installer/test_s3_fallback_cloudserver.py -q

The sample credentials are CloudServer's public, documented demo keys (``conf/authdata.json`` in the image), not
secrets; they exist only inside the throwaway container.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SMOKE = _ROOT / "scripts" / "installer" / "s3_smoke.py"
_PY = str(Path.home() / "venv" / "devinfra" / "bin" / "python")

# Pinned like every platform image: exact tag + digest (ghcr.io, Scality's own registry).
CLOUDSERVER_IMAGE = (
    "ghcr.io/scality/cloudserver:9.5.5"
    "@sha256:d4bacb4665468fa902fc9c99346c09c2c01d5e1745db200118cfca8595e98709"
)
_ACCOUNT_A = ("accessKey1", "verySecretKey1")  # demo account "Bart" (public sample key)
_ACCOUNT_B = ("accessKey2", "verySecretKey2")  # demo account "Lisa" (public sample key)
_REGION = "us-east-1"

pytestmark = pytest.mark.requires_infra


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def endpoint():
    if not shutil.which("docker"):
        pytest.skip("requires-infra, skipped: docker not available")
    port = _free_port()
    name = f"ws-c54-cloudserver-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8000",
         "-e", "S3BACKEND=mem", "-e", "REMOTE_MANAGEMENT_DISABLE=1", CLOUDSERVER_IMAGE],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"requires-infra, skipped: could not start CloudServer: {run.stderr.strip()[-300:]}")
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(1)
        time.sleep(2)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _s3(endpoint: str, creds: tuple[str, str], bucket: str, op: str) -> dict:
    env = dict(os.environ, S3_ACCESS_KEY_ID=creds[0], S3_SECRET_ACCESS_KEY=creds[1])
    r = subprocess.run([_PY, str(_SMOKE), "--endpoint", endpoint, "--region", _REGION, "--bucket", bucket, op],
                       env=env, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_same_client_contract_as_garage(endpoint):
    bucket = "proj-fallback-artifacts"
    for _ in range(20):  # the server may still be loading its config
        out = _s3(endpoint, _ACCOUNT_A, bucket, "create-bucket")
        if out["ok"]:
            break
        time.sleep(2)
    assert out["ok"], out
    for op in ("roundtrip", "list"):
        assert _s3(endpoint, _ACCOUNT_A, bucket, op)["ok"], op
    assert _s3(endpoint, _ACCOUNT_B, bucket, "expect-denied")["ok"], "another account must be refused (isolation)"
    assert _s3(endpoint, _ACCOUNT_A, bucket, "purge")["ok"]
    assert _s3(endpoint, _ACCOUNT_A, bucket, "delete-bucket")["ok"]
