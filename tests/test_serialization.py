from __future__ import annotations

import json
import unittest

from healthassure_messaging import (
    REQUEST_SCHEMA_VERSION,
    MessageRequest,
    RequestSerializationError,
    TemplateComponent,
    TemplateComponentType,
    TemplateMessage,
    TemplateReference,
    TextMessage,
    TextParameter,
    UnsupportedSchemaVersionError,
    deserialize_request,
    serialize_request,
)


def _text_request() -> MessageRequest:
    return MessageRequest(
        recipient="+12025550123",
        message=TextMessage(body="Hello"),
        correlation_id="correlation-text",
        idempotency_key="idempotency-text",
    )


def _template_request() -> MessageRequest:
    return MessageRequest(
        recipient="+12025550123",
        message=TemplateMessage(
            template=TemplateReference(name="appointment", language_code="en_IN"),
            components=(
                TemplateComponent(
                    component_type=TemplateComponentType.HEADER,
                    parameters=(TextParameter(text="header-1"),),
                ),
                TemplateComponent(
                    component_type=TemplateComponentType.BODY,
                    parameters=(
                        TextParameter(text="body-1"),
                        TextParameter(text="body-2"),
                    ),
                ),
                TemplateComponent(
                    component_type=TemplateComponentType.BUTTON,
                    parameters=(TextParameter(text="button-1"),),
                ),
            ),
        ),
        correlation_id="correlation-template",
        idempotency_key="idempotency-template",
    )


class SerializationTests(unittest.TestCase):
    def test_text_request_round_trip(self) -> None:
        request = _text_request()
        payload = serialize_request(request)
        self.assertEqual(deserialize_request(payload), request)
        self.assertEqual(deserialize_request(payload.encode("utf-8")), request)

    def test_template_round_trip_preserves_exact_order(self) -> None:
        request = _template_request()
        decoded = deserialize_request(serialize_request(request))
        self.assertEqual(decoded, request)
        self.assertIsInstance(decoded.message, TemplateMessage)
        assert isinstance(decoded.message, TemplateMessage)
        self.assertEqual(
            tuple(component.component_type for component in decoded.message.components),
            (
                TemplateComponentType.HEADER,
                TemplateComponentType.BODY,
                TemplateComponentType.BUTTON,
            ),
        )
        self.assertEqual(
            tuple(parameter.text for parameter in decoded.message.components[1].parameters),
            ("body-1", "body-2"),
        )

    def test_envelope_is_schema_versioned_and_provider_neutral(self) -> None:
        parsed = json.loads(serialize_request(_text_request()))
        self.assertEqual(parsed["schema_version"], REQUEST_SCHEMA_VERSION)
        self.assertEqual(set(parsed), {"schema_version", "request"})
        serialized = serialize_request(_text_request())
        self.assertNotIn("credential", serialized.lower())
        self.assertNotIn("provider_config", serialized.lower())

    def test_unsupported_schema_versions_are_rejected(self) -> None:
        parsed = json.loads(serialize_request(_text_request()))
        for version in (0, 2, True, "1"):
            with self.subTest(version=version):
                parsed["schema_version"] = version
                with self.assertRaises(UnsupportedSchemaVersionError):
                    deserialize_request(json.dumps(parsed))

    def test_unknown_message_type_is_rejected(self) -> None:
        parsed = json.loads(serialize_request(_text_request()))
        parsed["request"]["message"] = {"type": "media"}
        with self.assertRaises(RequestSerializationError):
            deserialize_request(json.dumps(parsed))

    def test_unknown_component_and_parameter_types_are_rejected(self) -> None:
        parsed = json.loads(serialize_request(_template_request()))
        component = parsed["request"]["message"]["components"][0]
        component["type"] = "carousel"
        with self.assertRaises(RequestSerializationError):
            deserialize_request(json.dumps(parsed))

        parsed = json.loads(serialize_request(_template_request()))
        parameter = parsed["request"]["message"]["components"][0]["parameters"][0]
        parameter["type"] = "currency"
        with self.assertRaises(RequestSerializationError):
            deserialize_request(json.dumps(parsed))

    def test_unknown_fields_and_duplicate_keys_are_rejected(self) -> None:
        parsed = json.loads(serialize_request(_text_request()))
        parsed["request"]["business_metadata"] = {}
        with self.assertRaises(RequestSerializationError):
            deserialize_request(json.dumps(parsed))

        duplicate_version = (
            '{"schema_version":1,"schema_version":1,"request":{}}'
        )
        with self.assertRaises(RequestSerializationError):
            deserialize_request(duplicate_version)


if __name__ == "__main__":
    unittest.main()
