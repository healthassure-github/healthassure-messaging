from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from typing import Any, cast

from mongo_fakes import FakeDatabase
from pymongo.database import Database

from healthassure_messaging import (
    REQUEST_SCHEMA_VERSION,
    DeliveryStatus,
    FakeMessagingProvider,
    MessageRequest,
    MessagingGateway,
    ProviderRegistry,
    SendDisposition,
    TextMessage,
    parse_meta_delivery_status_events,
    serialize_request,
    verify_meta_webhook_signature,
)
from healthassure_messaging.persistence.mongo import MongoMessagingPersistence
from healthassure_messaging.providers.meta import MetaCloudProvider


class BehavioralEquivalenceTests(unittest.TestCase):
    def test_schema_one_serialization_is_byte_exact(self) -> None:
        request = MessageRequest(
            recipient="+12025550123",
            message=TextMessage(body="Synthetic message"),
            correlation_id="correlation-1",
            idempotency_key="idempotency-1",
        )
        self.assertEqual(REQUEST_SCHEMA_VERSION, 1)
        self.assertEqual(
            serialize_request(request),
            '{"request":{"correlation_id":"correlation-1",'
            '"idempotency_key":"idempotency-1",'
            '"message":{"body":"Synthetic message","type":"text"},'
            '"recipient":"+12025550123"},"schema_version":1}',
        )

    def test_meta_text_payload_preserves_exact_protocol_values(self) -> None:
        request = MessageRequest(
            recipient="+12025550124",
            message=TextMessage(body="Synthetic message"),
            correlation_id="correlation-2",
            idempotency_key="idempotency-2",
        )
        self.assertEqual(
            MetaCloudProvider._build_payload(request),
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": "12025550124",
                "type": "text",
                "text": {"body": "Synthetic message", "preview_url": False},
            },
        )

    def test_signature_and_delivery_event_contracts_are_preserved(self) -> None:
        secret = "synthetic-app-secret"
        document = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "synthetic-business-account",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "999999999999999"},
                                "statuses": [
                                    {
                                        "id": "synthetic-provider-message",
                                        "status": "delivered",
                                        "timestamp": "1700000000",
                                        "recipient_id": "12025550125",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        raw_body = json.dumps(document, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(
            secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        self.assertTrue(verify_meta_webhook_signature(raw_body, signature, secret))
        events = parse_meta_delivery_status_events(raw_body)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, DeliveryStatus.DELIVERED)
        self.assertEqual(events[0].recipient_id, "12025550125")

    def test_gateway_routes_once_and_preserves_provider_result(self) -> None:
        provider = FakeMessagingProvider(
            key="synthetic",
            disposition=SendDisposition.ACCEPTED,
            provider_message_id="provider-message-1",
        )
        registry = ProviderRegistry()
        registry.register("synthetic", provider)
        request = MessageRequest(
            recipient="+12025550126",
            message=TextMessage(body="Synthetic message"),
            correlation_id="correlation-3",
            idempotency_key="idempotency-3",
        )
        result = MessagingGateway(registry).send(provider_key="synthetic", request=request)
        self.assertEqual(result.disposition, SendDisposition.ACCEPTED)
        self.assertEqual(result.provider_message_id, "provider-message-1")
        self.assertEqual(provider.received_requests, (request,))

    def test_mongo_index_plan_preserves_eight_index_shapes(self) -> None:
        fake = FakeDatabase()
        database = cast(Database[dict[str, Any]], fake)
        persistence = MongoMessagingPersistence(
            database=database,
            provider_key="meta",
            phone_number_id="synthetic-phone-id",
            clock=lambda: 100,
        )
        plan = persistence.index_plan()
        self.assertEqual(len(plan), 8)
        self.assertEqual(
            tuple(definition.collection_name for definition in plan),
            (
                "messaging_intents",
                "messaging_intents",
                "messaging_intents",
                "messaging_intents",
                "messaging_recipient_policies",
                "messaging_sessions",
                "messaging_sessions",
                "messaging_template_aliases",
            ),
        )
        self.assertEqual(sum(definition.unique for definition in plan), 6)
        self.assertTrue(
            all("expireAfterSeconds" not in dict(definition.keys) for definition in plan)
        )


if __name__ == "__main__":
    unittest.main()
