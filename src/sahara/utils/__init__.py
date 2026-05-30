"""Utilities — encryption, desktop notifications.

Canonical import paths:
    from sahara.utils import get_passphrase, notify_sync_complete
    from sahara.utils.encryption import get_passphrase, set_passphrase
    from sahara.utils.notifier import notify_sync_complete
"""

from sahara.utils.encryption import (  # noqa: F401
    EncryptionError,
    decrypt_file,
    decrypt_file_with_passphrase,
    delete_passphrase,
    derive_key,
    encrypt_file,
    encrypt_file_with_passphrase,
    generate_salt,
    get_passphrase,
    set_passphrase,
)
from sahara.utils.notifier import (  # noqa: F401
    notify_restore_complete,
    notify_restore_expiring,
    notify_sync_complete,
    notify_sync_error,
)

__all__ = [
    "derive_key", "generate_salt", "get_passphrase", "set_passphrase", "delete_passphrase",
    "encrypt_file", "decrypt_file", "encrypt_file_with_passphrase", "decrypt_file_with_passphrase",
    "EncryptionError",
    "notify_sync_complete", "notify_sync_error",
    "notify_restore_complete", "notify_restore_expiring",
]
