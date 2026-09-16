"""TLS certificate for the Docker node's peer surface, plus the address
detectors the backend shares with it.

The cert is self-signed ECDSA P-256 and carries the peer channel binding —
the node key's signature over the TLS SPKI (desktop/p2p/peer_auth.py) — so a
peer pins the channel to the node instead of trusting a CA or a name. The
SAN is therefore static: nobody who verifies this cert reads it. The Web UI
does not use TLS at all (PROGRESS.md "HTTP on the LAN"); the launcher's own
peer surface mints its cert in desktop/node_identity.ensure_tls_cert.

The cert is regenerated only when the binding is absent or belongs to a
previous identity, so peers see one TLS key for as long as the node keeps
its identity.

`detect_private_host_ips` / `detect_reachable_host_ips` answer "which
addresses are this host's" for the Host guard (auth_hmac), the media host a
DLNA renderer is handed, and the launcher's QR.
"""

import datetime
import ipaddress
import logging
import os
import socket
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


# RFC 6598 "shared address space" — carrier NAT, and what Tailscale hands out.
# Python does not count it as private, which is correct in the abstract and
# wrong for both questions below, in opposite directions.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_private_ipv4(ip: str) -> bool:
    """On a LAN segment we can send multicast to.

    Deliberately excludes CGNAT: a tunnel address is not on any segment, its
    interface cannot carry multicast, and searching from it costs a timeout
    per scan for nothing."""
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


def _is_reachable_ipv4(ip: str) -> bool:
    """An address a device could legitimately reach US at.

    The wider of the two: everything _is_private_ipv4 accepts, plus CGNAT,
    because a tunnel address is exactly how a phone off the home network
    reaches this node. Used for the cert SAN and the Host guard — both answer
    "who might legitimately be talking to us", not "where can we shout"."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return _is_private_ipv4(ip) or (
        isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT)


def detect_private_host_ips() -> list[str]:
    """LAN addresses only — the SSDP search sources and the media host the
    DLNA output hands a renderer on the same segment."""
    return _detect_private_host_ips()


def detect_reachable_host_ips(extra: list[str] | None = None) -> list[str]:
    """Every address this node can be addressed at, tunnels included.

    `extra` is SAUTIUM_HOST_IPS — the only way a container learns the host's
    real addresses, since its own interfaces are all bridge."""
    found = set(_detect_private_host_ips())
    for entry in (extra or []):
        if _is_reachable_ipv4(entry):
            found.add(entry)
            continue
        # A name, which is the better way to write a tunnel address down —
        # it survives the address changing. Resolving OUR OWN configured name
        # is safe; the thing rebinding attacks is resolving a name an attacker
        # supplied, which nothing here ever does.
        try:
            resolved = socket.gethostbyname(entry)
        except OSError as e:
            logger.warning("host entry %r does not resolve (%s)", entry, e)
            continue
        if _is_reachable_ipv4(resolved):
            found.add(resolved)
    return sorted(found)


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


def _build_san() -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = [x509.DNSName(d) for d in STATIC_DNS_SAN]
    entries += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in STATIC_IP_SAN]
    return x509.SubjectAlternativeName(entries)


def _generate_cert(cert_path: Path, key_path: Path,
                   binding: tuple | None = None) -> None:
    """Render a self-signed ECDSA P-256 cert + key with random fields.

    ECDSA rather than Ed25519 because the master's Caddy front serves this
    same file and TLS stacks validate Ed25519 server certs unevenly.

    `binding` = (node_pubkey_hex, sign_fn): embeds the peer channel
    binding (desktop/p2p/peer_auth.py) — the node key's signature over
    this cert's SPKI — so peers can pin the TLS channel to the node.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "sautium-backend"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sautium"),
    ])
    # Naive UTC works on cryptography 41 and 42; tz-aware emits a deprecation
    # warning on 42 because of the API rename to *_utc().
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(_build_san(), critical=False)
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
    )
    if binding is not None:
        from desktop.p2p import peer_auth
        pubkey_hex, sign_fn = binding
        spki = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        builder = builder.add_extension(
            x509.UnrecognizedExtension(
                x509.ObjectIdentifier(peer_auth.TLS_BINDING_OID),
                peer_auth.tls_binding_value(sign_fn, pubkey_hex, spki)),
            critical=False,
        )
    cert = builder.sign(key, hashes.SHA256())

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


def _binding_stale(cert_path: Path, binding: tuple | None) -> bool:
    """The peer channel binding this node needs is absent or belongs to a
    previous identity. Without a requested binding nothing is stale — an
    identity-less caller must not churn an already-accepted cert."""
    if binding is None:
        return False
    from desktop.p2p import peer_auth
    try:
        der = x509.load_pem_x509_certificate(
            cert_path.read_bytes()).public_bytes(serialization.Encoding.DER)
    except Exception:
        return True
    return peer_auth.tls_bound_pubkey(der) != binding[0].lower()


def ensure_cert(
    data_dir: Path | str,
    binding: tuple | None = None,
) -> tuple[Path, Path]:
    """Ensure the peer-surface cert exists in data_dir; regenerate it when
    the peer channel binding is absent or belongs to a previous identity.

    `binding` = (node_pubkey_hex, sign_fn) — see _generate_cert.
    Returns (cert_path, key_path).
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    cert_path = data_dir / CERT_FILENAME
    key_path = data_dir / KEY_FILENAME

    if cert_path.exists() and key_path.exists():
        # Drop any leftover Ed25519 cert from the abandoned
        # deterministic-cert experiment: TLS stacks validate Ed25519
        # server certs unevenly, and the master's front serves this file.
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
        elif _binding_stale(cert_path, binding):
            logger.info(
                "Regenerating cert: no peer channel binding for the current "
                "node identity",
            )
        else:
            return cert_path, key_path
    else:
        logger.info("Generating new cert at %s", cert_path)

    _generate_cert(cert_path, key_path, binding)
    return cert_path, key_path
