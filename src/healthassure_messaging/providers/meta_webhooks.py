from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import NoReturn, cast

from healthassure_messaging.enums import DeliveryStatus
from healthassure_messaging.webhooks import DeliveryError, DeliveryStatusEvent

_SIGNATURE_PATTERN = re.compile(r"sha256=([0-9a-fA-F]{64})")
_EXPECTED_OBJECT = "whatsapp_business_account"
_META_PROVIDER_KEY = "meta"
_MISSING = object()
_KNOWN_STATUSES = {
    "sent": DeliveryStatus.SENT,
    "delivered": DeliveryStatus.DELIVERED,
    "read": DeliveryStatus.READ,
    "failed": DeliveryStatus.FAILED,
    "deleted": DeliveryStatus.DELETED,
}


class MetaWebhookParseError(ValueError):
    """A sanitized failure to parse a Meta delivery-status webhook."""

    def __init__(self) -> None:
        super().__init__("Meta webhook payload is malformed.")


class _InternalParseFailure(Exception):
    pass


def verify_meta_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")
    if signature_header is not None and not isinstance(signature_header, str):
        raise TypeError("signature_header must be a string or None")
    if not isinstance(app_secret, str):
        raise TypeError("app_secret must be a string")
    if not app_secret:
        raise ValueError("app_secret must be non-empty")
    if signature_header is None:
        return False
    match = _SIGNATURE_PATTERN.fullmatch(signature_header)
    if match is None:
        return False
    supplied_digest = bytes.fromhex(match.group(1))
    try:
        secret_bytes = app_secret.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("app_secret must be valid UTF-8 text") from None
    expected_digest = hmac.new(
        secret_bytes,
        raw_body,
        hashlib.sha256,
    ).digest()
    return hmac.compare_digest(supplied_digest, expected_digest)


def parse_meta_delivery_status_events(raw_body: bytes) -> tuple[DeliveryStatusEvent, ...]:
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")

    decoded, document = _decode_json(raw_body)
    del raw_body
    if not decoded:
        raise MetaWebhookParseError from None

    malformed = False
    events: tuple[DeliveryStatusEvent, ...] = ()
    try:
        events = _parse_document(document)
    except _InternalParseFailure:
        malformed = True
    del document
    if malformed:
        raise MetaWebhookParseError from None
    return events


def _decode_json(raw_body: bytes) -> tuple[bool, object]:
    try:
        decoded = raw_body.decode("utf-8")
        document: object = json.loads(decoded)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False, None
    return True, document


def _parse_document(document: object) -> tuple[DeliveryStatusEvent, ...]:
    root = _require_object(document)
    if root.get("object") != _EXPECTED_OBJECT:
        _fail()
    entries = _require_array(root.get("entry", _MISSING))
    events: list[DeliveryStatusEvent] = []
    for entry_value in entries:
        entry = _require_object(entry_value)
        changes = _require_array(entry.get("changes", _MISSING))
        for change_value in changes:
            change = _require_object(change_value)
            field = _require_text(change.get("field", _MISSING))
            if field != "messages":
                continue
            value = _require_object(change.get("value", _MISSING))
            statuses_value = value.get("statuses", _MISSING)
            if statuses_value is _MISSING:
                continue
            statuses = _require_array(statuses_value)
            if not statuses:
                continue
            waba_id = _require_text(entry.get("id", _MISSING))
            metadata = _require_object(value.get("metadata", _MISSING))
            phone_number_id = _require_text(metadata.get("phone_number_id", _MISSING))
            for status_value in statuses:
                events.append(
                    _parse_status(
                        status_value,
                        waba_id=waba_id,
                        phone_number_id=phone_number_id,
                    )
                )
    return tuple(events)


def _parse_status(
    status_value: object,
    *,
    waba_id: str,
    phone_number_id: str,
) -> DeliveryStatusEvent:
    status_object = _require_object(status_value)
    provider_message_id = _require_text(status_object.get("id", _MISSING))
    provider_status = _require_text(status_object.get("status", _MISSING))
    timestamp = _require_timestamp(status_object.get("timestamp", _MISSING))
    recipient_id = _require_text(status_object.get("recipient_id", _MISSING))
    errors = _parse_errors(status_object.get("errors", _MISSING))
    conversation_id, conversation_origin_type = _parse_conversation(
        status_object.get("conversation", _MISSING)
    )
    pricing_model, pricing_category, billable = _parse_pricing(
        status_object.get("pricing", _MISSING)
    )
    return DeliveryStatusEvent(
        provider_key=_META_PROVIDER_KEY,
        provider_message_id=provider_message_id,
        status=_KNOWN_STATUSES.get(provider_status, DeliveryStatus.UNKNOWN),
        provider_status=provider_status,
        occurred_at_epoch=timestamp,
        recipient_id=recipient_id,
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        errors=errors,
        conversation_id=conversation_id,
        conversation_origin_type=conversation_origin_type,
        pricing_model=pricing_model,
        pricing_category=pricing_category,
        billable=billable,
    )


def _parse_errors(value: object) -> tuple[DeliveryError, ...]:
    if value is _MISSING:
        return ()
    errors = _require_array(value)
    parsed: list[DeliveryError] = []
    for error_value in errors:
        error = _require_object(error_value)
        code = error.get("code", _MISSING)
        if type(code) is not int or not 0 <= code <= 2_147_483_647:
            _fail()
        subcode_value = error.get("error_subcode", _MISSING)
        subcode: int | None = None
        if subcode_value is not _MISSING:
            if (
                type(subcode_value) is not int
                or not 0 <= subcode_value <= 2_147_483_647
            ):
                _fail()
            subcode = subcode_value
        parsed.append(DeliveryError(provider_code=str(code), provider_subcode=subcode))
    return tuple(parsed)


def _parse_conversation(value: object) -> tuple[str | None, str | None]:
    if value is _MISSING:
        return None, None
    conversation = _require_object(value)
    conversation_id = _require_text(conversation.get("id", _MISSING))
    origin_value = conversation.get("origin", _MISSING)
    if origin_value is _MISSING:
        return conversation_id, None
    origin = _require_object(origin_value)
    origin_type = _require_text(origin.get("type", _MISSING))
    return conversation_id, origin_type


def _parse_pricing(value: object) -> tuple[str | None, str | None, bool | None]:
    if value is _MISSING:
        return None, None, None
    pricing = _require_object(value)
    pricing_model = _optional_text(pricing.get("pricing_model", _MISSING))
    pricing_category = _optional_text(pricing.get("category", _MISSING))
    billable_value = pricing.get("billable", _MISSING)
    billable: bool | None = None
    if billable_value is not _MISSING:
        if type(billable_value) is not bool:
            _fail()
        billable = billable_value
    return pricing_model, pricing_category, billable


def _require_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail()
    return cast(dict[str, object], value)


def _require_array(value: object) -> list[object]:
    if not isinstance(value, list):
        _fail()
    return cast(list[object], value)


def _require_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail()
    return value


def _optional_text(value: object) -> str | None:
    if value is _MISSING:
        return None
    return _require_text(value)


def _require_timestamp(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        _fail()
    try:
        return int(value)
    except ValueError:
        _fail()


def _fail() -> NoReturn:
    raise _InternalParseFailure from None


__all__ = [
    "MetaWebhookParseError",
    "parse_meta_delivery_status_events",
    "verify_meta_webhook_signature",
]
