from __future__ import annotations

import json
import unittest
from importlib.resources import files
from typing import cast

from healthassure_messaging import (
    DeliveryError,
    DeliveryStatus,
    DeliveryStatusEvent,
    MetaWebhookParseError,
    parse_meta_delivery_status_events,
)
from healthassure_messaging.providers import (
    MetaWebhookParseError as ProviderMetaWebhookParseError,
)
from healthassure_messaging.providers import (
    parse_meta_delivery_status_events as provider_parse_meta_delivery_status_events,
)


def _status(
    provider_status: str = "sent",
    *,
    message_id: str = "synthetic-message-001",
    timestamp: object = "1700000000",
    recipient_id: str = "synthetic-recipient-001",
    **extra: object,
) -> dict[str, object]:
    return {
        "id": message_id,
        "status": provider_status,
        "timestamp": timestamp,
        "recipient_id": recipient_id,
        **extra,
    }


def _document(
    statuses: object,
    *,
    waba_id: object = "synthetic-waba-001",
    phone_number_id: object = "synthetic-phone-id-001",
) -> dict[str, object]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "statuses": statuses,
                        },
                    }
                ],
            }
        ],
    }


def _encode(document: object) -> bytes:
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse(document: object) -> tuple[DeliveryStatusEvent, ...]:
    return parse_meta_delivery_status_events(_encode(document))


class MetaWebhookParsingTests(unittest.TestCase):
    def test_one_sent_status(self) -> None:
        events = _parse(_document([_status()]))
        self.assertEqual(
            events,
            (
                DeliveryStatusEvent(
                    provider_key="meta",
                    provider_message_id="synthetic-message-001",
                    status=DeliveryStatus.SENT,
                    provider_status="sent",
                    occurred_at_epoch=1_700_000_000,
                    recipient_id="synthetic-recipient-001",
                    waba_id="synthetic-waba-001",
                    phone_number_id="synthetic-phone-id-001",
                ),
            ),
        )

    def test_all_known_statuses_and_case_sensitive_unknown_are_mapped(self) -> None:
        provider_statuses = ("sent", "delivered", "read", "failed", "deleted", "Sent")
        statuses = [
            _status(value, message_id=f"synthetic-message-{index}")
            for index, value in enumerate(provider_statuses)
        ]
        events = _parse(_document(statuses))
        self.assertEqual(
            tuple(event.status for event in events),
            (
                DeliveryStatus.SENT,
                DeliveryStatus.DELIVERED,
                DeliveryStatus.READ,
                DeliveryStatus.FAILED,
                DeliveryStatus.DELETED,
                DeliveryStatus.UNKNOWN,
            ),
        )
        self.assertEqual(events[-1].provider_status, "Sent")

    def test_multiple_entries_changes_and_statuses_preserve_source_order(self) -> None:
        document = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "synthetic-waba-a",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "synthetic-phone-a"},
                                "statuses": [
                                    _status(message_id="first"),
                                    _status("delivered", message_id="second"),
                                ],
                            },
                        },
                        {"field": "account_update", "value": ["ignored-shape"]},
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "synthetic-phone-a"},
                                "statuses": [_status("read", message_id="third")],
                            },
                        },
                    ],
                },
                {
                    "id": "synthetic-waba-b",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "synthetic-phone-b"},
                                "statuses": [_status("deleted", message_id="fourth")],
                            },
                        }
                    ],
                },
            ],
        }
        events = _parse(document)
        self.assertEqual(
            tuple(event.provider_message_id for event in events),
            ("first", "second", "third", "fourth"),
        )
        self.assertEqual(events[-1].waba_id, "synthetic-waba-b")

    def test_inbound_message_and_status_free_message_changes_return_no_events(self) -> None:
        inbound = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "synthetic-waba-inbound",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "synthetic-phone-inbound"},
                                "messages": [
                                    {
                                        "id": "synthetic-inbound-message",
                                        "type": "text",
                                        "text": {"body": "Harmless synthetic inbound text"},
                                    }
                                ],
                            },
                        },
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "synthetic-phone-inbound"},
                                "statuses": [],
                            },
                        },
                    ],
                }
            ],
        }
        self.assertEqual(_parse(inbound), ())

    def test_non_message_changes_are_ignored_without_inspecting_value(self) -> None:
        document = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {"field": "account_update", "value": "intentionally ignored"}
                    ]
                }
            ],
        }
        self.assertEqual(_parse(document), ())

    def test_failed_status_supports_zero_one_and_multiple_safe_errors(self) -> None:
        statuses = [
            _status("failed", message_id="failed-zero"),
            _status(
                "failed",
                message_id="failed-one",
                errors=[
                    {
                        "code": 131000,
                        "title": "SYNTHETIC_TITLE_MUST_NOT_SURVIVE",
                        "message": "SYNTHETIC_MESSAGE_MUST_NOT_SURVIVE",
                    }
                ],
            ),
            _status(
                "failed",
                message_id="failed-many",
                errors=[
                    {"code": 131001, "error_subcode": 7},
                    {"code": 131002, "error_data": {"details": "not retained"}},
                ],
            ),
        ]
        events = _parse(_document(statuses))
        self.assertEqual(events[0].errors, ())
        self.assertEqual(events[1].errors, (DeliveryError(provider_code="131000"),))
        self.assertEqual(
            events[2].errors,
            (
                DeliveryError(provider_code="131001", provider_subcode=7),
                DeliveryError(provider_code="131002"),
            ),
        )
        diagnostic = repr(events)
        self.assertNotIn("SYNTHETIC_TITLE", diagnostic)
        self.assertNotIn("SYNTHETIC_MESSAGE", diagnostic)
        self.assertNotIn("error_data", diagnostic)

    def test_optional_conversation_and_pricing_fields_are_normalized(self) -> None:
        event = _parse(
            _document(
                [
                    _status(
                        "delivered",
                        conversation={
                            "id": "synthetic-conversation-001",
                            "origin": {"type": "utility", "ignored": "value"},
                            "expiration_timestamp": "1700009999",
                        },
                        pricing={
                            "pricing_model": "PMP",
                            "category": "utility",
                            "billable": True,
                            "ignored": "value",
                        },
                    )
                ]
            )
        )[0]
        self.assertEqual(event.conversation_id, "synthetic-conversation-001")
        self.assertEqual(event.conversation_origin_type, "utility")
        self.assertEqual(event.pricing_model, "PMP")
        self.assertEqual(event.pricing_category, "utility")
        self.assertTrue(event.billable)
        self.assertNotIn("expiration_timestamp", repr(event))
        self.assertNotIn("ignored", repr(event))

    def test_invalid_encoding_json_and_envelope_are_sanitized(self) -> None:
        invalid_payloads = (
            b"\xff\xfeSYNTHETIC_RAW_BODY",
            b'{"SYNTHETIC_RAW_BODY":',
            b"[]",
            _encode({"object": "page", "entry": []}),
            _encode({"object": "whatsapp_business_account"}),
        )
        for raw_body in invalid_payloads:
            with self.subTest(raw_body=raw_body):
                with self.assertRaises(MetaWebhookParseError) as raised:
                    parse_meta_delivery_status_events(raw_body)
                diagnostic = f"{raised.exception!r} {raised.exception} {raised.exception.args}"
                self.assertNotIn("SYNTHETIC_RAW_BODY", diagnostic)
                self.assertNotIn(raw_body.hex(), diagnostic)
                self.assertEqual(raised.exception.__dict__, {})

    def test_invalid_entry_change_and_status_shapes_fail_atomically(self) -> None:
        invalid_documents = (
            {"object": "whatsapp_business_account", "entry": {}},
            {"object": "whatsapp_business_account", "entry": ["entry"]},
            {
                "object": "whatsapp_business_account",
                "entry": [{"id": "waba", "changes": {}}],
            },
            {
                "object": "whatsapp_business_account",
                "entry": [{"id": "waba", "changes": ["change"]}],
            },
            {
                "object": "whatsapp_business_account",
                "entry": [{"id": "waba", "changes": [{"field": 1}]}],
            },
            _document({}),
            _document(["status"]),
        )
        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(MetaWebhookParseError):
                _parse(document)

    def test_missing_and_empty_required_status_evidence_is_rejected(self) -> None:
        valid = _document([_status()])
        variants: list[dict[str, object]] = []
        for field in ("id", "status", "recipient_id"):
            status = _status()
            del status[field]
            variants.append(_document([status]))
            variants.append(_document([{**_status(), field: " "}]))
        variants.extend(
            (
                _document([_status()], waba_id=" "),
                _document([_status()], phone_number_id=" "),
            )
        )
        missing_metadata = json.loads(json.dumps(valid))
        del missing_metadata["entry"][0]["changes"][0]["value"]["metadata"]
        variants.append(cast(dict[str, object], missing_metadata))
        missing_phone = json.loads(json.dumps(valid))
        del missing_phone["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
        variants.append(cast(dict[str, object], missing_phone))
        for document in variants:
            with self.subTest(document=document), self.assertRaises(MetaWebhookParseError):
                _parse(document)

    def test_timestamp_must_be_a_nonnegative_ascii_decimal_string(self) -> None:
        invalid_timestamps: tuple[object, ...] = (
            "not-a-number",
            "-1",
            "",
            " 1",
            "1.0",
            "1" * 5_000,
            "\N{ARABIC-INDIC DIGIT ONE}",
            1,
            -1,
            True,
            None,
        )
        for timestamp in invalid_timestamps:
            with self.subTest(timestamp=timestamp), self.assertRaises(MetaWebhookParseError):
                _parse(_document([_status(timestamp=timestamp)]))

    def test_malformed_errors_conversation_and_pricing_are_rejected(self) -> None:
        invalid_extra_fields: tuple[tuple[str, object], ...] = (
            ("errors", {}),
            ("errors", ["error"]),
            ("errors", [{}]),
            ("errors", [{"code": True}]),
            ("errors", [{"code": -1}]),
            ("errors", [{"code": "131000"}]),
            ("errors", [{"code": 131000, "error_subcode": "7"}]),
            ("conversation", []),
            ("conversation", {}),
            ("conversation", {"id": " "}),
            ("conversation", {"id": "conversation", "origin": []}),
            ("conversation", {"id": "conversation", "origin": {}}),
            ("pricing", []),
            ("pricing", {"pricing_model": 1}),
            ("pricing", {"category": " "}),
            ("pricing", {"billable": "true"}),
        )
        for field, value in invalid_extra_fields:
            with self.subTest(field=field, value=value), self.assertRaises(
                MetaWebhookParseError
            ):
                status = _status()
                status[field] = value
                _parse(_document([status]))

    def test_later_malformed_status_causes_atomic_failure(self) -> None:
        statuses = [
            _status(message_id="would-have-been-valid"),
            _status(message_id=" "),
        ]
        with self.assertRaises(MetaWebhookParseError):
            _parse(_document(statuses))

    def test_raw_payload_and_provider_text_are_not_retained(self) -> None:
        raw_marker = "SYNTHETIC_RAW_PROVIDER_TEXT_MUST_NOT_SURVIVE"
        event = _parse(
            _document(
                [
                    _status(
                        "failed",
                        errors=[
                            {
                                "code": 131000,
                                "title": raw_marker,
                                "message": raw_marker,
                                "href": raw_marker,
                                "error_data": {"details": raw_marker},
                            }
                        ],
                        unknown_field=raw_marker,
                    )
                ]
            )
        )[0]
        self.assertNotIn(raw_marker, repr(event))
        self.assertEqual(event.errors, (DeliveryError(provider_code="131000"),))

    def test_public_imports_and_typed_package_marker(self) -> None:
        self.assertIs(ProviderMetaWebhookParseError, MetaWebhookParseError)
        self.assertIs(
            provider_parse_meta_delivery_status_events,
            parse_meta_delivery_status_events,
        )
        self.assertTrue(files("healthassure_messaging").joinpath("py.typed").is_file())

    def test_parser_requires_bytes(self) -> None:
        with self.assertRaises(TypeError) as raised:
            parse_meta_delivery_status_events(
                cast(bytes, "SYNTHETIC_BODY_MUST_NOT_APPEAR")
            )
        self.assertNotIn("SYNTHETIC_BODY", f"{raised.exception!r} {raised.exception}")


if __name__ == "__main__":
    unittest.main()
