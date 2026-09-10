"""Identity normalization. Pure functions; the DB side is core/repo/identity_repo.py."""
from __future__ import annotations

from email.utils import parseaddr

import phonenumbers


def normalize_email(value: str) -> str:
    """Lowercase + trim. Accepts 'Name <addr>' too and returns just the address."""
    _, addr = parseaddr(value.strip())
    return (addr or value).strip().lower()


def display_name_from_header(value: str) -> str | None:
    name, _ = parseaddr(value.strip())
    return name.strip() or None


def normalize_phone(value: str, default_region: str = "DE") -> str:
    """E.164. Raises ValueError if the number cannot be parsed."""
    try:
        num = phonenumbers.parse(value, default_region)
    except phonenumbers.NumberParseException as e:
        raise ValueError(f"cannot parse phone {value!r}: {e}") from e
    if not phonenumbers.is_possible_number(num):
        raise ValueError(f"not a possible phone number: {value!r}")
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def normalize(kind: str, value: str, default_region: str = "DE") -> str:
    if kind == "email":
        return normalize_email(value)
    if kind == "phone":
        return normalize_phone(value, default_region)
    return value.strip()
