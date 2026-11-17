"""Store secrets in the macOS Keychain via keyring, with QSettings fallback.

Existing plaintext QSettings values are migrated to the Keychain on first
load and removed from the plist.
"""
import contextlib

try:
    import keyring
except ImportError:
    keyring = None

_SERVICE = "AutoNotes"


def save_secret(settings, name: str, value: str) -> bool:
    """Persist a secret; empty value deletes it.

    Returns True when it is in the Keychain (or was deleted), False when it
    had to fall back to the plaintext preferences file — the caller tells the
    user, since a silent downgrade to plaintext is exactly what this module
    exists to prevent.
    """
    if keyring is None:
        settings.setValue(name, value)
        return not value
    try:
        if value:
            keyring.set_password(_SERVICE, name, value)
        else:
            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(_SERVICE, name)
        settings.remove(name)  # purge any old plaintext copy
        return True
    except Exception:
        settings.setValue(name, value)
        return not value


def load_secret(settings, name: str) -> str:
    if keyring is not None:
        try:
            value = keyring.get_password(_SERVICE, name)
            if value is not None:
                return value
            # Migrate a pre-Keychain plaintext value if one exists
            legacy = settings.value(name, "")
            if legacy:
                keyring.set_password(_SERVICE, name, legacy)
                settings.remove(name)
            return legacy
        except Exception:
            pass   # Keychain unavailable: the plaintext fallback below is all there is
    return settings.value(name, "")
