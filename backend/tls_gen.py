"""Self-signed TLS certificate generator for the Sautium backend.

Used by both the Docker entrypoint and the desktop launcher to produce a
cert covering loopback addresses, host.docker.internal, and any private
(RFC 1918) host IPv4 addresses — auto-detected from local interfaces or
passed explicitly via CLI / SAUTIUM_HOST_IPS env.

The cert is regenerated only when the set of detected private IPs is
not yet covered by the existing cert's SAN. This keeps the cert stable
across restarts so the phone doesn't have to re-accept the warning.

CLI:
    python tls_gen.py --data-dir /path/to/tls [--host-ips 1.2.3.4,5.6.7.8]
"""

import argparse
import base64
import datetime
import ipaddress
import logging
import os
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

CERT_VALIDITY_DAYS = 365 * 10
CERT_FILENAME = "cert.pem"
KEY_FILENAME = "key.pem"

# Anchor for the deterministic-cert validity window. Far enough in the
# past that the cert is always "currently valid" on a freshly-installed
# device, far enough in the future that we don't have to think about
# expiry rotation as a separate problem.
_DETERMINISTIC_NOT_BEFORE = datetime.datetime(2024, 1, 1)
_DETERMINISTIC_VALIDITY_DAYS = 365 * 100


def _hkdf(seed: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=None,
        info=info,
    ).derive(seed)


def _read_seed_from_env() -> bytes | None:
    """Read base64-encoded TLS seed from SAUTIUM_TLS_SEED env, if present."""
    raw = os.getenv("SAUTIUM_TLS_SEED", "").strip()
    if not raw:
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        logger.warning("SAUTIUM_TLS_SEED is not valid base64 — falling back to random cert")
        return None

# Static SAN entries always present, regardless of host IPs.
STATIC_DNS_SAN = ("localhost", "host.docker.internal")
STATIC_IP_SAN = ("127.0.0.1", "::1")


def _is_private_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        isinstance(addr, ipaddress.IPv4Address)
        and addr.is_private
        and not addr.is_loopback
        and not addr.is_link_local
    )


def _detect_private_host_ips() -> list[str]:
    """Auto-detect private IPv4 addresses bound to local interfaces.

    Combines two probes — getaddrinfo(hostname) for multi-interface
    coverage, and the connect-but-don't-send UDP trick for the primary
    outbound interface. Inside Docker this typically yields only the
    bridge IP (e.g. 172.x); for LAN reachability the operator must
    pass the host's real IP via SAUTIUM_HOST_IPS.
    """
    found: set[str] = set()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_private_ipv4(ip):
                found.add(ip)
    except (socket.gaierror, OSError) as e:
        logger.debug("getaddrinfo failed: %s", e)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 1))
        ip = sock.getsockname()[0]
        if _is_private_ipv4(ip):
            found.add(ip)
    except OSError:
        pass
    finally:
        sock.close()

    return sorted(found)


def _read_existing_san_ips(cert_path: Path) -> set[str]:
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (FileNotFoundError, ValueError) as e:
        logger.debug("Cannot read existing cert %s: %s", cert_path, e)
        return set()
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return set()
    return {str(ip) for ip in san.value.get_values_for_type(x509.IPAddress)}


def _build_san(extra_ips: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = [x509.DNSName(d) for d in STATIC_DNS_SAN]
    seen: set[str] = set()
    for ip in (*STATIC_IP_SAN, *extra_ips):
        if ip in seen:
            continue
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
            seen.add(ip)
        except ValueError:
            logger.warning("Skipping invalid IP for SAN: %r", ip)
    return x509.SubjectAlternativeName(entries)


def _generate_cert(
    extra_ips: list[str],
    cert_path: Path,
    key_path: Path,
    seed: bytes | None = None,
) -> None:
    """Render the self-signed cert + key.

    Two modes:
      * seed=None — random ECDSA P-256 key, current time, random serial,
        SHA-256 signature with a non-deterministic ECDSA k. Each run
        produces fresh DER bytes.
      * seed=bytes — Ed25519 key derived deterministically from the seed
        via HKDF, fixed validity window (2024-01-01 .. +100 years),
        serial derived from the seed, signature is RFC 8032 Ed25519
        (deterministic by spec). Same seed + same SAN IPs = same DER
        bytes, so the browser keeps the trust decision across reinstalls.
    """
    if seed:
        # 32 raw bytes are exactly the Ed25519 seed format expected by
        # from_private_bytes; HKDF lets us reuse the same seed for
        # multiple cert-related derivations without leaking the parent.
        key_seed = _hkdf(seed, b"sautium-tls-cert-key", 32)
        key = Ed25519PrivateKey.from_private_bytes(key_seed)
        # x509 wants a positive serial less than 2^159 (PKIX). 20 bytes
        # of HKDF output, top bit cleared, zero bumped to 1.
        serial_int = int.from_bytes(
            _hkdf(seed, b"sautium-tls-cert-serial", 20), "big",
        ) & ((1 << 159) - 1) or 1
        not_before = _DETERMINISTIC_NOT_BEFORE
        not_after = _DETERMINISTIC_NOT_BEFORE + datetime.timedelta(
            days=_DETERMINISTIC_VALIDITY_DAYS,
        )
        # Ed25519 cert signing has no separate hash — pass None.
        sign_hash = None
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        serial_int = x509.random_serial_number()
        # Naive UTC works on cryptography 41 and 42; tz-aware emits a
        # deprecation warning on 42 because of the API rename to *_utc().
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        not_before = now - datetime.timedelta(minutes=5)
        not_after = now + datetime.timedelta(days=CERT_VALIDITY_DAYS)
        sign_hash = hashes.SHA256()

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "sautium-backend"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sautium"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(serial_int)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(_build_san(extra_ips), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                # Ed25519 doesn't do key agreement; ECDSA P-256 with TLS
                # does. The flag is informational on browser side.
                key_agreement=(seed is None),
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, sign_hash)
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass


def _existing_pubkey_matches_seed(cert_path: Path, seed: bytes) -> bool:
    """True if the cert on disk holds the Ed25519 public key our seed derives."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        existing_pub = cert.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    except Exception:
        return False
    expected_seed = _hkdf(seed, b"sautium-tls-cert-key", 32)
    expected_pub = (
        Ed25519PrivateKey
        .from_private_bytes(expected_seed)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    return existing_pub == expected_pub


def ensure_cert(
    data_dir: Path | str,
    extra_host_ips: list[str] | None = None,
    seed: bytes | None = None,
) -> tuple[Path, Path]:
    """Ensure a self-signed cert exists in data_dir; regen if SAN is stale.

    When `seed` is provided, the cert is derived deterministically from
    it (Ed25519 key + fixed validity window + seed-derived serial). The
    same seed + same SAN IPs always produce identical DER bytes, so a
    fresh install on the same machine yields the same cert hash and the
    browser doesn't re-warn. Without a seed, the legacy random-ECDSA
    path runs.

    Returns (cert_path, key_path).
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    cert_path = data_dir / CERT_FILENAME
    key_path = data_dir / KEY_FILENAME

    detected = _detect_private_host_ips()
    explicit = [ip for ip in (extra_host_ips or []) if _is_private_ipv4(ip)]
    needed_ips = sorted(set(detected) | set(explicit))

    if cert_path.exists() and key_path.exists():
        existing = _read_existing_san_ips(cert_path)
        missing = set(needed_ips) - existing
        san_ok = not missing
        # Seed mismatch — treat the existing cert as legacy/random and
        # regenerate so further reinstalls can deduplicate against the
        # fresh deterministic one.
        seed_ok = True if seed is None else _existing_pubkey_matches_seed(
            cert_path, seed,
        )
        if san_ok and seed_ok:
            logger.debug("Cert %s already covers IPs: %s", cert_path, needed_ips)
            return cert_path, key_path
        if not san_ok:
            logger.info(
                "Regenerating cert: new private IPs %s not in current SAN %s",
                sorted(missing), sorted(existing),
            )
        elif not seed_ok:
            logger.info(
                "Regenerating cert: existing public key doesn't match account-derived seed",
            )
    else:
        logger.info("Generating new cert at %s", cert_path)

    _generate_cert(needed_ips, cert_path, key_path, seed=seed)
    logger.info(
        "Cert SAN — DNS: %s, IPs: %s%s",
        list(STATIC_DNS_SAN),
        list(STATIC_IP_SAN) + needed_ips,
        " (deterministic Ed25519)" if seed else "",
    )
    return cert_path, key_path


def _parse_ips(value: str | None) -> list[str]:
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument(
        "--host-ips",
        default=os.getenv("SAUTIUM_HOST_IPS", ""),
        help="Comma-separated extra private IPs (or env SAUTIUM_HOST_IPS)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cert, key = ensure_cert(
        args.data_dir, _parse_ips(args.host_ips), seed=_read_seed_from_env(),
    )
    print(f"cert={cert}")
    print(f"key={key}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
