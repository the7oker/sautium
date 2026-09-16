"""backend/static/sha256.js against hashlib: the request signer's HMAC has to
match auth_hmac.sign byte for byte, on an http origin where crypto.subtle
does not exist. Runs the module under Node (available in the backend
image); skipped where Node is not."""

import hashlib
import hmac
import json
import os
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_DRIVER = """
const fs = require('node:fs'), vm = require('node:vm');
const window = {};
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), { window });
const { sha256, hmacSha256, toHex } = window.Sautium.hash;
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = cases.map(([keyHex, msgHex]) => {
  const msg = Uint8Array.from(Buffer.from(msgHex, 'hex'));
  const key = Uint8Array.from(Buffer.from(keyHex, 'hex'));
  return [toHex(sha256(msg)), toHex(hmacSha256(key, msg))];
});
process.stdout.write(JSON.stringify(out));
"""


def test_matches_hashlib():
    rng = os.urandom
    cases = [
        (b"", b""),
        (b"Jefe", b"what do ya want for nothing?"),
        (b"k" * 64, b"block-sized key"),
        (b"\xaa" * 131, b"Test Using Larger Than Block-Size Key - Hash Key First"),
        (b"sautium-device-token", "GET\n/api/x?q=і\n1700000000\n".encode()),
        (b"", b"a" * 55), (b"", b"a" * 56), (b"", b"a" * 64), (b"", b"a" * 65),
        (rng(43), rng(1000)), (rng(7), rng(100000)),
    ]
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, "--", str(REPO_ROOT / "backend" / "static" / "sha256.js")],
        input=json.dumps([[k.hex(), m.hex()] for k, m in cases]).encode(),
        capture_output=True, check=True)
    got = json.loads(proc.stdout)
    for (key, msg), (sha, mac) in zip(cases, got):
        assert sha == hashlib.sha256(msg).hexdigest()
        assert mac == hmac.new(key, msg, hashlib.sha256).hexdigest()
