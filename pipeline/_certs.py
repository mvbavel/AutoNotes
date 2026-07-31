"""Build a CA bundle trusting both the macOS system store and certifi.

A frozen app can't rely on the host having Homebrew's OpenSSL certificate
path, so certifi is bundled. But trusting certifi *alone* breaks on networks
that do TLS inspection (Zscaler, Netskope, …): the proxy re-signs every
connection with a corporate root that lives in the macOS keychain and is
deliberately absent from Mozilla's list. Combining both sources covers the
no-Homebrew case and the inspected-network case at once.
"""
import os
import subprocess

_SECURITY = "/usr/bin/security"

# System.keychain holds admin/MDM-installed roots (where Zscaler lands);
# SystemRootCertificates.keychain holds Apple's built-in roots.
_KEYCHAINS = (
    "/Library/Keychains/System.keychain",
    "/System/Library/Keychains/SystemRootCertificates.keychain",
)

_BEGIN = "-----BEGIN CERTIFICATE-----"
_END = "-----END CERTIFICATE-----"


def bundle_path() -> str:
    return os.path.join(
        os.path.expanduser("~/Library/Application Support/AutoNotes"),
        "ca-bundle.pem",
    )


def _export_keychain(path: str) -> str:
    """PEM text for every certificate in a keychain ('' if unavailable)."""
    if not os.path.exists(path):
        return ""
    try:
        proc = subprocess.run(
            [_SECURITY, "find-certificate", "-a", "-p", path],
            capture_output=True, text=True, timeout=60,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _pem_blocks(text: str) -> list[str]:
    """Split PEM text into individual certificate blocks."""
    blocks, current = [], None
    for line in text.splitlines():
        if line.strip() == _BEGIN:
            current = [line.strip()]
        elif current is not None:
            current.append(line.strip())
            if line.strip() == _END:
                blocks.append("\n".join(current))
                current = None
    return blocks


def _is_stale(dest: str) -> bool:
    """True if the combined bundle is missing or older than its sources."""
    try:
        bundle_mtime = os.path.getmtime(dest)
    except OSError:
        return True
    for kc in _KEYCHAINS:
        try:
            if os.path.getmtime(kc) > bundle_mtime:
                return True
        except OSError:
            continue
    return False


def build_ca_bundle(certifi_pem: str, dest: str | None = None,
                    force: bool = False) -> str | None:
    """Write certifi + macOS system roots to a combined PEM bundle.

    Returns the bundle path, or None if it couldn't be produced (callers
    should then fall back to certifi alone). Reuses an existing bundle
    unless it is older than the keychains it was built from.
    """
    dest = dest or bundle_path()
    if not force and not _is_stale(dest):
        return dest

    try:
        with open(certifi_pem, encoding="utf-8") as f:
            certifi_text = f.read()
    except OSError:
        return None

    seen, blocks = set(), []
    for text in [certifi_text] + [_export_keychain(kc) for kc in _KEYCHAINS]:
        for block in _pem_blocks(text):
            if block not in seen:
                seen.add(block)
                blocks.append(block)

    if not blocks:
        return None

    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(blocks) + "\n")
        os.replace(tmp, dest)   # atomic: never leave a half-written bundle
    except OSError:
        return None
    return dest


def configure_ssl_env() -> str | None:
    """Point OpenSSL, requests and libcurl at the combined bundle.

    Falls back to certifi alone if the combined bundle can't be built, and
    does nothing at all if certifi is unavailable. Returns the path in use.
    """
    try:
        import certifi
        certifi_pem = certifi.where()
    except Exception:
        return None
    if not os.path.exists(certifi_pem):
        return None

    ca = build_ca_bundle(certifi_pem) or certifi_pem
    os.environ["SSL_CERT_FILE"] = ca
    os.environ["SSL_CERT_DIR"] = os.path.dirname(ca)
    os.environ["REQUESTS_CA_BUNDLE"] = ca
    os.environ["CURL_CA_BUNDLE"] = ca   # libcurl backend used by curl_cffi
    return ca


def use_native_trust() -> bool:
    """Verify TLS through the macOS trust store instead of OpenSSL's rules.

    A merged CA bundle is not enough on an inspected network. Python 3.13+
    turns on VERIFY_X509_STRICT by default, and Zscaler's root declares
    basicConstraints without marking it critical — an RFC 5280 violation that
    strict verification rejects outright ("Basic Constraints of CA cert not
    marked critical"), even once the root is present. macOS accepts it, which
    is why the same sites load in Safari. truststore routes verification
    through that store, so corporate roots work without us having to weaken
    verification for everyone else.
    """
    try:
        import truststore
        truststore.inject_into_ssl()
        return True
    except Exception:
        return False


def configure_trust() -> dict:
    """Set up TLS for both in-process clients and child processes.

    Returns {"native_trust": bool, "ca_bundle": str | None} so callers can log
    what actually took effect.
    """
    native = use_native_trust()
    # Still needed even when native trust is active: truststore only patches
    # this process's ssl module, while yt-dlp runs as a child process and
    # curl_cffi's libcurl backend never touches Python's ssl at all. Both read
    # these variables.
    ca = configure_ssl_env()
    return {"native_trust": native, "ca_bundle": ca}
