from __future__ import annotations

import json
import unittest

from healthassure_messaging import (
    DeliveryStatus,
    ErrorCategory,
    IntentState,
    MessageRequest,
    SendDisposition,
    TemplateComponent,
    TemplateComponentType,
    TemplateMessage,
    TemplateReference,
    TextParameter,
    deserialize_request,
    serialize_request,
)
from healthassure_messaging.http import TransportFailureKind


class StringEnumCompatibilityTests(unittest.TestCase):
    def test_members_preserve_string_equality_value_and_formatting(self) -> None:
        cases = (
            (DeliveryStatus.DELIVERED, "delivered"),
            (SendDisposition.ACCEPTED, "accepted"),
            (ErrorCategory.PROTOCOL, "protocol"),
            (IntentState.DISPATCHING, "dispatching"),
            (TemplateComponentType.HEADER, "header"),
            (TransportFailureKind.TIMEOUT, "TIMEOUT"),
        )
        for member, expected in cases:
            with self.subTest(member=member):
                self.assertIsInstance(member, str)
                self.assertEqual(member.value, expected)
                self.assertEqual(member, expected)
                self.assertEqual(expected, member)
                self.assertEqual(str(member), expected)
                self.assertEqual(f"{member}", expected)
                self.assertEqual(hash(member), hash(expected))
                self.assertEqual({expected: "found"}[member], "found")

    def test_members_encode_as_plain_json_strings(self) -> None:
        payload = {
            "delivery": DeliveryStatus.READ,
            "disposition": SendDisposition.UNKNOWN,
            "category": ErrorCategory.NETWORK,
            "component": TemplateComponentType.BODY,
            "transport": TransportFailureKind.NETWORK,
        }
        self.assertEqual(
            json.dumps(payload, separators=(",", ":")),
            (
                '{"delivery":"read","disposition":"unknown","category":"network",'
                '"component":"body","transport":"NETWORK"}'
            ),
        )

    def test_request_serialization_and_round_trip_preserve_enum_values(self) -> None:
        request = MessageRequest(
            recipient="+12025550124",
            message=TemplateMessage(
                template=TemplateReference(name="synthetic_template", language_code="en_US"),
                components=(
                    TemplateComponent(
                        component_type=TemplateComponentType.HEADER,
                        parameters=(TextParameter(text="first"),),
                    ),
                    TemplateComponent(
                        component_type=TemplateComponentType.BODY,
                        parameters=(TextParameter(text="second"),),
                    ),
                ),
            ),
            correlation_id="synthetic-correlation",
            idempotency_key="synthetic-idempotency",
        )

        serialized = serialize_request(request)
        document = json.loads(serialized)
        components = document["request"]["message"]["components"]
        self.assertEqual([component["type"] for component in components], ["header", "body"])
        self.assertEqual(deserialize_request(serialized), request)


if __name__ == "__main__":
    unittest.main()
