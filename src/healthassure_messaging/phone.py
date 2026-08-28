from __future__ import annotations

import re
from collections.abc import Callable

_SEPARATORS = re.compile(r"[\s().-]")
_ALLOWED_INPUT = re.compile(r"[0-9+()\s.\-]+")
_E164 = re.compile(r"\+[1-9][0-9]{7,14}")
_INDIAN_MOBILE = re.compile(r"[6-9][0-9]{9}")


class PhoneNumberError(ValueError):
    """Raised when a phone number cannot be safely normalized to E.164."""


def is_e164_number(value: object) -> bool:
    """Return whether *value* is a structurally valid supported E.164 number."""

    if not isinstance(value, str) or _E164.fullmatch(value) is None:
        return False

    digits = value[1:]
    if digits.startswith("91"):
        return _INDIAN_MOBILE.fullmatch(digits[2:]) is not None
    return True


def validate_e164_number(value: str) -> str:
    """Validate and return an already-normalized E.164 number."""

    if not is_e164_number(value):
        raise PhoneNumberError("recipient is not a valid E.164 number")
    return value


def _normalize_national_number(value: str, region: str) -> str:
    if region != "IN":
        raise PhoneNumberError("national phone number normalization supports only region IN")

    national_digits = value[1:] if value.startswith("0") else value
    if _INDIAN_MOBILE.fullmatch(national_digits) is None:
        raise PhoneNumberError("Indian national number must be a valid ten-digit mobile number")
    return f"+91{national_digits}"


def normalize_phone_number(
    value: str,
    *,
    default_region: str | None = None,
    national_number_normalizer: Callable[[str, str], str] = _normalize_national_number,
) -> str:
    """Normalize a supported phone number to E.164 without retaining or logging the input."""

    if not isinstance(value, str) or not value.strip():
        raise PhoneNumberError("phone number must be a non-empty string")

    stripped = value.strip()
    if _ALLOWED_INPUT.fullmatch(stripped) is None:
        raise PhoneNumberError("phone number contains unsupported characters")

    compact = _SEPARATORS.sub("", stripped)
    if compact.startswith("+"):
        if not compact[1:].isdigit():
            raise PhoneNumberError("international phone number is malformed")
        return validate_e164_number(compact)

    if compact.startswith("00"):
        international_digits = compact[2:]
        if not international_digits.isdigit():
            raise PhoneNumberError("international phone number is malformed")
        return validate_e164_number(f"+{international_digits}")

    if not compact.isdigit():
        raise PhoneNumberError("national phone number is malformed")
    if default_region is None or not default_region.strip():
        raise PhoneNumberError("default_region is required for national phone numbers")

    region = default_region.strip().upper()
    normalized = national_number_normalizer(compact, region)
    return validate_e164_number(normalized)
