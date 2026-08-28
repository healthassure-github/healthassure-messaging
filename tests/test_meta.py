from __future__ import annotations

import json
import os
import secrets
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, field
from unittest.mock import patch

from healthassure_messaging import (
    REQUEST_SCHEMA_VERSION,
    ErrorCategory,
    FakeMessagingProvider,
    MessageRequest,
    MessagingGateway,
    MetaCloudConfig,
    MetaCloudProvider,
    ProviderRegistry,
    SendDisposition,
    TemplateComponent,
    TemplateComponentType,
    TemplateMessage,
    TemplateReference,
    TextMessage,
    TextParameter,
    serialize_request,
)
from healthassure_messaging.http import (
    HttpOutcome,
    HttpResponse,
    HttpTransport,
    TransportFailure,
    TransportFailureKind,
)

SYNTHETIC_GRAPH_VERSION = "v999.0"
SYNTHETIC_PHONE_NUMBER_ID = "999999999999999"
SYNTHETIC_ACCESS_TOKEN = "synthetic-placeholder-access-token"
SYNTHETIC_RECIPIENT = "+12025550124"


@dataclass(frozen=True, slots=True)
class _RecordedCall:
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    json_body: Mapping[str, object] = field(repr=False)
    timeout: tuple[float, float]


class _RecordingTransport(HttpTransport):
    def __init__(self, outcomes: HttpOutcome | tuple[HttpOutcome, ...]) -> None:
        self._outcomes = outcomes if isinstance(outcomes, tuple) else (outcomes,)
        self.calls: list[_RecordedCall] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
        timeout: tuple[float, float],
    ) -> HttpOutcome:
        self.calls.append(
            _RecordedCall(
                url=url,
                headers=dict(headers),
                json_body=dict(json_body),
                timeout=timeout,
            )
        )
        return self._outcomes[len(self.calls) - 1]


def _json_response(
    status_code: int,
    document: object,
    *,
    retry_after: str | None = None,
) -> HttpResponse:
    return HttpResponse(
        status_code=status_code,
        body=json.dumps(document).encode("utf-8"),
        retry_after=retry_after,
    )


def _success_response(provider_message_id: str = "synthetic-provider-message-id") -> HttpResponse:
    return _json_response(200, {"messages": [{"id": provider_message_id}]})


def _config() -> MetaCloudConfig:
    return MetaCloudConfig(
        graph_version=SYNTHETIC_GRAPH_VERSION,
        phone_number_id=SYNTHETIC_PHONE_NUMBER_ID,
        access_token=SYNTHETIC_ACCESS_TOKEN,
        connect_timeout=1.25,
        read_timeout=3.5,
    )


def _text_request(*, body: str = "Synthetic adapter test") -> MessageRequest:
    return MessageRequest(
        recipient=SYNTHETIC_RECIPIENT,
        message=TextMessage(body=body),
        correlation_id="synthetic-correlation",
        idempotency_key="synthetic-idempotency",
    )


def _template_request(
    components: tuple[TemplateComponent, ...],
) -> MessageRequest:
    return MessageRequest(
        recipient=SYNTHETIC_RECIPIENT,
        message=TemplateMessage(
            template=TemplateReference(
                name="synthetic_template",
                language_code="en_US",
            ),
            components=components,
        ),
        correlation_id="synthetic-template-correlation",
        idempotency_key="synthetic-template-idempotency",
    )


class MetaConfigurationTests(unittest.TestCase):
    def test_explicit_valid_graph_version_is_required(self) -> None:
        self.assertEqual(_config().graph_version, SYNTHETIC_GRAPH_VERSION)
        for version in ("", "999.0", "v999", "latest", "v0.1", "v1.-1"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError) as context:
                    MetaCloudConfig(
                        graph_version=version,
                        phone_number_id=SYNTHETIC_PHONE_NUMBER_ID,
                        access_token=SYNTHETIC_ACCESS_TOKEN,
                        connect_timeout=1.0,
                        read_timeout=2.0,
                    )
                self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, str(context.exception))

    def test_phone_number_id_and_token_are_required(self) -> None:
        with self.assertRaises(ValueError):
            MetaCloudConfig(
                graph_version=SYNTHETIC_GRAPH_VERSION,
                phone_number_id="",
                access_token=SYNTHETIC_ACCESS_TOKEN,
                connect_timeout=1.0,
                read_timeout=2.0,
            )
        with self.assertRaises(ValueError):
            MetaCloudConfig(
                graph_version=SYNTHETIC_GRAPH_VERSION,
                phone_number_id=SYNTHETIC_PHONE_NUMBER_ID,
                access_token="",
                connect_timeout=1.0,
                read_timeout=2.0,
            )

    def test_credentials_and_identifier_are_absent_from_config_repr(self) -> None:
        rendered = repr(_config())
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, rendered)
        self.assertNotIn(SYNTHETIC_PHONE_NUMBER_ID, rendered)

    def test_configuration_is_not_loaded_from_the_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "META_ACCESS_TOKEN": "environment-secret",
                "META_PHONE_NUMBER_ID": "111111111",
            },
            clear=True,
        ):
            config = _config()
        self.assertEqual(config.graph_version, SYNTHETIC_GRAPH_VERSION)
        self.assertNotIn("environment-secret", repr(config))


class MetaPayloadTests(unittest.TestCase):
    def test_exact_text_post_contract_and_recipient_representation(self) -> None:
        transport = _RecordingTransport(_success_response())
        provider = MetaCloudProvider(_config(), transport=transport)

        result = provider.send(_text_request())

        self.assertEqual(result.disposition, SendDisposition.ACCEPTED)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(
            call.url,
            "https://graph.facebook.com/v999.0/999999999999999/messages",
        )
        self.assertEqual(set(call.headers), {"Authorization", "Content-Type"})
        self.assertTrue(
            secrets.compare_digest(
                call.headers["Authorization"],
                f"Bearer {SYNTHETIC_ACCESS_TOKEN}",
            )
        )
        self.assertEqual(call.headers["Content-Type"], "application/json")
        self.assertEqual(call.timeout, (1.25, 3.5))
        self.assertEqual(
            call.json_body,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": "12025550124",
                "type": "text",
                "text": {
                    "body": "Synthetic adapter test",
                    "preview_url": False,
                },
            },
        )

    def test_unicode_text_is_preserved_and_never_logged_or_returned(self) -> None:
        body = "Unicode test: नमस्ते 👋"
        transport = _RecordingTransport(
            _json_response(
                400,
                {
                    "error": {
                        "code": 100,
                        "message": body,
                        "fbtrace_id": "synthetic-trace",
                    }
                },
            )
        )
        provider = MetaCloudProvider(_config(), transport=transport)

        with self.assertNoLogs():
            result = provider.send(_text_request(body=body))

        self.assertEqual(transport.calls[0].json_body["text"], {"body": body, "preview_url": False})
        self.assertNotIn(body, repr(result))
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, repr(result))
        self.assertNotIn(SYNTHETIC_RECIPIENT, repr(result))

    def test_template_order_and_text_parameter_order_are_exact(self) -> None:
        header = TemplateComponent(
            component_type=TemplateComponentType.HEADER,
            parameters=(TextParameter(text="header-one"),),
        )
        body = TemplateComponent(
            component_type=TemplateComponentType.BODY,
            parameters=(TextParameter(text="body-one"), TextParameter(text="body-two")),
        )
        transport = _RecordingTransport(_success_response())
        MetaCloudProvider(_config(), transport=transport).send(
            _template_request((header, body))
        )

        self.assertEqual(
            transport.calls[0].json_body["template"],
            {
                "name": "synthetic_template",
                "language": {"code": "en_US"},
                "components": [
                    {
                        "type": "header",
                        "parameters": [{"type": "text", "text": "header-one"}],
                    },
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": "body-one"},
                            {"type": "text", "text": "body-two"},
                        ],
                    },
                ],
            },
        )

    def test_empty_template_components_are_omitted(self) -> None:
        transport = _RecordingTransport(_success_response())
        MetaCloudProvider(_config(), transport=transport).send(_template_request(()))
        template = transport.calls[0].json_body["template"]
        self.assertIsInstance(template, dict)
        assert isinstance(template, dict)
        self.assertNotIn("components", template)

    def test_button_is_rejected_before_http(self) -> None:
        button = TemplateComponent(
            component_type=TemplateComponentType.BUTTON,
            parameters=(TextParameter(text="unsupported"),),
        )
        transport = _RecordingTransport(_success_response())

        result = MetaCloudProvider(_config(), transport=transport).send(
            _template_request((button,))
        )

        self.assertEqual(result.disposition, SendDisposition.REJECTED)
        self.assertEqual(result.error.category if result.error else None, ErrorCategory.UNSUPPORTED)
        self.assertEqual(transport.calls, [])


class MetaResponseTests(unittest.TestCase):
    def test_success_captures_any_non_empty_message_id_and_correlation(self) -> None:
        transport = _RecordingTransport(_success_response("non-prefixed-synthetic-id"))
        result = MetaCloudProvider(_config(), transport=transport).send(_text_request())
        self.assertEqual(result.provider_key, "meta")
        self.assertEqual(result.disposition, SendDisposition.ACCEPTED)
        self.assertEqual(result.provider_message_id, "non-prefixed-synthetic-id")
        self.assertEqual(result.provider_status, "accepted")
        self.assertEqual(result.correlation_id, "synthetic-correlation")
        self.assertIsNone(result.error)

    def test_unexpected_2xx_responses_are_unknown_protocol_outcomes(self) -> None:
        responses = (
            HttpResponse(status_code=200, body=b"not-json"),
            _json_response(200, {}),
            _json_response(200, {"messages": [{}]}),
            _json_response(200, {"messages": [{"id": ""}]}),
            HttpResponse(status_code=200, body=b"", body_too_large=True),
        )
        for response in responses:
            with self.subTest(response=response):
                result = MetaCloudProvider(
                    _config(), transport=_RecordingTransport(response)
                ).send(_text_request())
                self.assertEqual(result.disposition, SendDisposition.UNKNOWN)
                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.category, ErrorCategory.PROTOCOL)
                self.assertTrue(result.error.unknown_outcome)
                self.assertFalse(result.error.retriable)

    def test_timeout_and_connection_ambiguity_are_unknown_without_retry(self) -> None:
        failures = (
            (TransportFailureKind.TIMEOUT, ErrorCategory.TIMEOUT),
            (TransportFailureKind.NETWORK, ErrorCategory.NETWORK),
        )
        for kind, category in failures:
            with self.subTest(kind=kind):
                transport = _RecordingTransport(TransportFailure(kind))
                result = MetaCloudProvider(_config(), transport=transport).send(_text_request())
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(result.disposition, SendDisposition.UNKNOWN)
                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.category, category)
                self.assertTrue(result.error.unknown_outcome)
                self.assertFalse(result.error.retriable)

    def test_definite_http_rejections_are_normalized(self) -> None:
        mappings = (
            (400, ErrorCategory.VALIDATION, False),
            (401, ErrorCategory.AUTHENTICATION, False),
            (403, ErrorCategory.AUTHORIZATION, False),
            (429, ErrorCategory.RATE_LIMIT, True),
            (404, ErrorCategory.PROVIDER_PERMANENT, False),
        )
        provider_body = {
            "error": {
                "message": "unsafe provider text with customer data",
                "code": 131_000,
                "error_subcode": 249_4010,
                "fbtrace_id": "synthetic-trace",
            }
        }
        for status, category, retriable in mappings:
            with self.subTest(status=status):
                result = MetaCloudProvider(
                    _config(),
                    transport=_RecordingTransport(_json_response(status, provider_body)),
                ).send(_text_request())
                self.assertEqual(result.disposition, SendDisposition.REJECTED)
                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.category, category)
                self.assertEqual(result.error.retriable, retriable)
                self.assertFalse(result.error.unknown_outcome)
                self.assertEqual(result.error.http_status, status)
                self.assertEqual(result.error.provider_code, "131000")
                self.assertEqual(result.error.provider_subcode, 2_494_010)
                self.assertNotIn("unsafe provider text", repr(result))
                self.assertNotIn("fbtrace", repr(result).lower())
                self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, repr(result))

    def test_structured_500_and_503_rejections_require_numeric_meta_error_code(self) -> None:
        provider_body = {
            "error": {
                "message": "unsafe provider text",
                "code": 131_000,
                "error_subcode": 2_494_010,
                "fbtrace_id": "synthetic-trace",
            }
        }
        for status in (500, 503):
            with self.subTest(status=status):
                result = MetaCloudProvider(
                    _config(),
                    transport=_RecordingTransport(_json_response(status, provider_body)),
                ).send(_text_request())
                self.assertEqual(result.disposition, SendDisposition.REJECTED)
                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.category, ErrorCategory.PROVIDER_TEMPORARY)
                self.assertTrue(result.error.retriable)
                self.assertFalse(result.error.unknown_outcome)
                self.assertEqual(result.error.http_status, status)
                self.assertEqual(result.error.provider_code, "131000")
                self.assertEqual(result.error.provider_subcode, 2_494_010)
                self.assertNotIn("unsafe provider text", repr(result))
                self.assertNotIn("fbtrace", repr(result).lower())

    def test_unverified_5xx_responses_are_unknown_protocol_outcomes(self) -> None:
        responses = (
            HttpResponse(status_code=502, body=b"<html>synthetic upstream error</html>"),
            HttpResponse(status_code=503, body=b""),
            HttpResponse(status_code=502, body=b'{"error":'),
            HttpResponse(status_code=503, body=b"", body_too_large=True),
            _json_response(503, {"error": {"code": "131000"}}),
        )
        for response in responses:
            with self.subTest(status=response.status_code, oversized=response.body_too_large):
                result = MetaCloudProvider(
                    _config(),
                    transport=_RecordingTransport(response),
                ).send(_text_request())
                self.assertEqual(result.disposition, SendDisposition.UNKNOWN)
                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.category, ErrorCategory.PROTOCOL)
                self.assertTrue(result.error.unknown_outcome)
                self.assertFalse(result.error.retriable)
                self.assertEqual(result.error.http_status, response.status_code)
                self.assertIsNone(result.error.provider_code)
                self.assertNotIn("synthetic upstream error", repr(result))

    def test_retry_after_is_integer_only_and_bounded(self) -> None:
        for value, expected in (
            ("0", 0),
            ("60", 60),
            ("86400", 86_400),
            ("86401", None),
            ("-1", None),
            ("1.5", None),
            ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ):
            with self.subTest(value=value):
                result = MetaCloudProvider(
                    _config(),
                    transport=_RecordingTransport(
                        _json_response(429, {"error": {"code": 4}}, retry_after=value)
                    ),
                ).send(_text_request())
                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.retry_after_seconds, expected)

    def test_gateway_selects_meta_explicitly_without_fallback(self) -> None:
        transport = _RecordingTransport(_success_response())
        meta = MetaCloudProvider(_config(), transport=transport)
        fallback = FakeMessagingProvider(key="fake", disposition=SendDisposition.ACCEPTED)
        registry = ProviderRegistry()
        registry.register("meta", meta)
        registry.register("fake", fallback)

        result = MessagingGateway(registry).send(
            provider_key="meta",
            request=_text_request(),
        )

        self.assertEqual(result.provider_key, "meta")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(fallback.received_requests, ())

    def test_request_serialization_contract_remains_version_one(self) -> None:
        serialized = serialize_request(_text_request())
        document = json.loads(serialized)
        self.assertEqual(REQUEST_SCHEMA_VERSION, 1)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(set(document), {"schema_version", "request"})
        self.assertNotIn("meta", serialized.lower())
        self.assertNotIn("access_token", serialized.lower())


if __name__ == "__main__":
    unittest.main()
