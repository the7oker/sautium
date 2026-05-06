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
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

CERT_VALIDITY_DAYS = 365 * 10
CERT_FILENAME = "cert.pem"
KEY_FILENAME = "key.pem"

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


def _generate_cert(extra_ips: list[str], cert_path: Path, key_path: Path) -> None:
    """Render a self-signed ECDSA P-256 cert + key with random fields.

    Browsers refuse to validate Ed25519 server certs (Chrome/Firefox
    accept Ed25519 in TLS 1.3 protocol but not in cert path validation
    as of 2026), so we stick with ECDSA. Stability across reinstalls
    is achieved at the storage layer instead — service_manager keeps
    cert + key in a per-account user-profile directory that survives
    the typical reinstall scrub.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "sautium-backend"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sautium"),
    ])
    # Naive UTC works on cryptography 41 and 42; tz-aware emits a deprecation
    # warning on 42 because of the API rename to *_utc().
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(_build_san(extra_ips), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
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
        .sign(key, hashes.SHA256())
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


def ensure_cert(
    data_dir: Path | str,
    extra_host_ips: list[str] | None = None,
) -> tuple[Path, Path]:
    """Ensure a self-signed cert exists in data_dir; regen if SAN is stale.

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
        # Drop any leftover Ed25519 cert from the abandoned
        # deterministic-cert experiment. Browsers don't validate
        # Ed25519 server certs (Chrome/Firefox accept the algorithm
        # in TLS 1.3 but not in cert path validation), so they
        # silently refuse to load https://localhost:18000 and the
        # user gets "site can't be reached".
        is_legacy_ed25519 = False
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            algo_oid = cert.signature_algorithm_oid.dotted_string
            # 1.3.101.112 = Ed25519, 1.3.101.113 = Ed448
            if algo_oid in {"1.3.101.112", "1.3.101.113"}:
                is_legacy_ed25519 = True
        except Exception:
            pass
        if is_legacy_ed25519:
            logger.info(
                "Replacing legacy Ed25519 cert at %s with browser-compatible ECDSA",
                cert_path,
            )
        elif not missing:
            logger.debug("Cert %s already covers IPs: %s", cert_path, needed_ips)
            return cert_path, key_path
        else:
            logger.info(
                "Regenerating cert: new private IPs %s not in current SAN %s",
                sorted(missing), sorted(existing),
            )
    else:
        logger.info("Generating new cert at %s", cert_path)

    _generate_cert(needed_ips, cert_path, key_path)
    logger.info(
        "Cert SAN — DNS: %s, IPs: %s",
        list(STATIC_DNS_SAN), list(STATIC_IP_SAN) + needed_ips,
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
    cert, key = ensure_cert(args.data_dir, _parse_ips(args.host_ips))
    print(f"cert={cert}")
    print(f"key={key}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
