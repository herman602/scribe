"""OS keyring adapter for PDF passwords."""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError

from app.domain.errors import SecretStoreError
from app.domain.parsing import SecretValue

_SERVICE_NAME = "scribe-local-pdf"


class KeyringSecretStore:
    """Store PDF passwords in the operating-system credential backend."""

    def get(self, reference: str) -> SecretValue | None:
        """Resolve an opaque reference without logging the returned value."""
        _validate_reference(reference)
        try:
            value = keyring.get_password(_SERVICE_NAME, reference)
        except KeyringError as error:
            raise SecretStoreError("OS credential store could not be read") from error
        return SecretValue(value) if value else None

    def set(self, reference: str, value: SecretValue) -> None:
        """Store a PDF password outside SQLite and configuration files."""
        _validate_reference(reference)
        try:
            keyring.set_password(_SERVICE_NAME, reference, value.reveal())
        except KeyringError as error:
            raise SecretStoreError(
                "OS credential store rejected the password"
            ) from error

    def delete(self, reference: str) -> None:
        """Delete one password; missing entries are treated as already absent."""
        _validate_reference(reference)
        try:
            keyring.delete_password(_SERVICE_NAME, reference)
        except keyring.errors.PasswordDeleteError:
            return
        except KeyringError as error:
            raise SecretStoreError(
                "OS credential store could not delete the password"
            ) from error


def _validate_reference(reference: str) -> None:
    if (
        not reference
        or len(reference) > 200
        or any(character.isspace() for character in reference)
    ):
        raise ValueError("secret reference is invalid")
