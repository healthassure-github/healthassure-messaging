from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import unittest
from collections.abc import Callable
from typing import cast

from healthassure_messaging import (
    DeliveryError,
    DeliveryStatus,
    DeliveryStatusEvent,
    verify_meta_webhook_signature,
)


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _event(**overrides: object) -> DeliveryStatusEvent:
    values: dict[str, object] = {
        "provider_key": "meta",
        "provider_message_id": "synthetic-message-001",
        "status": DeliveryStatus.DELIVERED,
        "provider_status": "delivered",
        "occurred_at_epoch": 1_700_000_000,
        "recipient_id": "opaque:synthetic-recipient",
        "waba_id": "synthetic-waba-001",
        "phone_number_id": "synthetic-phone-id-001",
    }
    values.update(overrides)
    return DeliveryStatusEvent(**values)  # type: ignore[arg-type]


def _set_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


class DeliveryContractTests(unittest.TestCase):
    def test_contracts_are_immutable_and_have_no_mutable_defaults(self) -> None:
        error = DeliveryError(provider_code="synthetic-code")
        event = _event(errors=(error,))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _set_attribute(event, "provider_status", "read")
        for contract_type in (DeliveryError, DeliveryStatusEvent):
            with self.subTest(contract=contract_type.__name__):
                for field in dataclasses.fields(contract_type):
                    self.assertNotIsInstance(field.default, (list, dict, set))

    def test_recipient_is_an_opaque_non_empty_provider_identifier(self) -> None:
        event = _event(recipient_id="not-an-e164-value")
        self.assertEqual(event.recipient_id, "not-an-e164-value")

    def test_mandatory_strings_status_timestamp_and_errors_are_validated(self) -> None:
        string_fields = (
            "provider_key",
            "provider_message_id",
            "provider_status",
            "recipient_id",
            "waba_id",
            "phone_number_id",
        )
        for field_name in string_fields:
            with self.subTest(field=field_name), self.assertRaises(ValueError):
                _event(**{field_name: " "})
        for timestamp in (-1, True, 1.5, "1"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                _event(occurred_at_epoch=timestamp)
        with self.assertRaises(TypeError):
            _event(status=cast(DeliveryStatus, "delivered"))
        with self.assertRaises(TypeError):
            _event(errors=cast(tuple[DeliveryError, ...], []))
        with self.assertRaises(TypeError):
            _event(errors=(cast(DeliveryError, "not-an-error"),))

    def test_optional_fields_and_delivery_error_are_strict_and_safe(self) -> None:
        error = DeliveryError(provider_code="131000", provider_subcode=7)
        event = _event(
            errors=(error,),
            conversation_id="synthetic-conversation",
            conversation_origin_type="utility",
            pricing_model="PMP",
            pricing_category="utility",
            billable=False,
        )
        self.assertEqual(event.errors, (error,))
        self.assertFalse(event.billable)
        self.assertEqual(
            {field.name for field in dataclasses.fields(DeliveryError)},
            {"provider_code", "provider_subcode"},
        )
        with self.assertRaises(ValueError):
            DeliveryError(provider_code=" ")
        for subcode in (-1, True, 2_147_483_648):
            with self.subTest(subcode=subcode), self.assertRaises(ValueError):
                DeliveryError(provider_code="1", provider_subcode=subcode)
        with self.assertRaises(TypeError):
            _event(billable=cast(bool | None, "false"))
        with self.assertRaises(ValueError):
            _event(pricing_category=" ")


class MetaWebhookSignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = b'{"synthetic":"payload"}'
        self.secret = "synthetic-app-secret"

    def test_valid_signature(self) -> None:
        self.assertTrue(
            verify_meta_webhook_signature(
                self.body,
                _signature(self.body, self.secret),
                self.secret,
            )
        )

    def test_modified_body_and_wrong_secret_fail(self) -> None:
        signature = _signature(self.body, self.secret)
        self.assertFalse(
            verify_meta_webhook_signature(self.body + b" ", signature, self.secret)
        )
        self.assertFalse(
            verify_meta_webhook_signature(self.body, signature, "different-synthetic-secret")
        )

    def test_missing_and_malformed_headers_fail(self) -> None:
        digest = _signature(self.body, self.secret).removeprefix("sha256=")
        invalid_headers = (
            None,
            f"sha1={digest}",
            f"SHA256={digest}",
            "sha256=not-hex",
            f"sha256={digest[:-1]}",
            f"sha256={digest}0",
            f"sha256= {digest}",
            f"sha256={digest} ",
            f"sha256={digest},sha256={digest}",
            f"sha256={digest};extra",
        )
        for header in invalid_headers:
            with self.subTest(header=header):
                self.assertFalse(
                    verify_meta_webhook_signature(self.body, header, self.secret)
                )

    def test_uppercase_hexadecimal_digest_is_accepted(self) -> None:
        signature = _signature(self.body, self.secret).upper().replace("SHA256=", "sha256=")
        self.assertTrue(verify_meta_webhook_signature(self.body, signature, self.secret))

    def test_empty_secret_and_incorrect_types_are_rejected_safely(self) -> None:
        with self.assertRaisesRegex(ValueError, "app_secret must be non-empty"):
            verify_meta_webhook_signature(self.body, None, "")
        with self.assertRaisesRegex(ValueError, "app_secret must be valid UTF-8 text"):
            verify_meta_webhook_signature(
                self.body,
                _signature(self.body, self.secret),
                "\ud800SYNTHETIC_SECRET_MUST_NOT_APPEAR",
            )
        invalid_calls: tuple[Callable[[], bool], ...] = (
            lambda: verify_meta_webhook_signature(
                cast(bytes, "SYNTHETIC_BODY_MUST_NOT_APPEAR"), None, self.secret
            ),
            lambda: verify_meta_webhook_signature(
                self.body, cast(str | None, 123456), self.secret
            ),
            lambda: verify_meta_webhook_signature(
                self.body, None, cast(str, b"SYNTHETIC_SECRET_MUST_NOT_APPEAR")
            ),
        )
        forbidden = ("SYNTHETIC_BODY", "123456", "SYNTHETIC_SECRET")
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(TypeError) as raised:
                    call()
                diagnostic = f"{raised.exception!r} {raised.exception}"
                for value in forbidden:
                    self.assertNotIn(value, diagnostic)

    def test_unicode_json_is_signed_as_exact_utf8_bytes(self) -> None:
        compact = json.dumps(
            {"text": "Synthetic café नमस्ते"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = _signature(compact, self.secret)
        self.assertTrue(verify_meta_webhook_signature(compact, signature, self.secret))
        reserialized = json.dumps(
            json.loads(compact.decode("utf-8")),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        self.assertNotEqual(reserialized, compact)
        self.assertFalse(verify_meta_webhook_signature(reserialized, signature, self.secret))


if __name__ == "__main__":
    unittest.main()
