"""Store secrets in the macOS Keychain via keyring, with QSettings fallback.

Existing plaintext QSettings values are migrated to the Keychain on first
load and removed from the plist.
"""
try:
    import keyring
except ImportError:
    keyring = None

_SERVICE = "AutoNotes"


def save_secret(settings, name: str, value: str):
    """Persist a secret; empty value deletes it."""
    if keyring is None:
        settings.setValue(name, value)
        return
    try:
        if value:
            keyring.set_password(_SERVICE, name, value)
        else:
            try:
                keyring.delete_password(_SERVICE, name)
            except keyring.errors.PasswordDeleteError:
                pass
        settings.remove(name)  # purge any old plaintext copy
    except Exception:
        settings.setValue(name, value)


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
            pass
    return settings.value(name, "")
