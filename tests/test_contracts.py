from __future__ import annotations

import dataclasses
import unittest
from typing import cast

from healthassure_messaging import (
    ErrorCategory,
    MessageRequest,
    NormalizedError,
    SendDisposition,
    SendResult,
    TemplateComponent,
    TemplateComponentType,
    TemplateMessage,
    TemplateReference,
    TextMessage,
    TextParameter,
)


def _set_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


class ContractTests(unittest.TestCase):
    def test_contracts_are_immutable(self) -> None:
        message = TextMessage(body="hello")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _set_attribute(message, "body", "changed")

        request = MessageRequest(
            recipient="+12025550123",
            message=message,
            correlation_id="correlation-1",
            idempotency_key="idempotency-1",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _set_attribute(request, "correlation_id", "changed")

    def test_contracts_have_no_mutable_defaults(self) -> None:
        contract_types = (
            NormalizedError,
            TextMessage,
            TemplateReference,
            TextParameter,
            TemplateComponent,
            TemplateMessage,
            MessageRequest,
            SendResult,
        )
        for contract_type in contract_types:
            with self.subTest(contract=contract_type.__name__):
                for field in dataclasses.fields(contract_type):
                    self.assertNotIsInstance(field.default, (list, dict, set))

    def test_non_empty_text_fields_are_validated(self) -> None:
        invalid_factories = (
            lambda: TextMessage(body="  "),
            lambda: TemplateReference(name="", language_code="en"),
            lambda: TemplateReference(name="appointment", language_code=" "),
            lambda: TextParameter(text=""),
            lambda: MessageRequest(
                recipient="+12025550123",
                message=TextMessage(body="hello"),
                correlation_id="",
                idempotency_key="idempotency-1",
            ),
            lambda: MessageRequest(
                recipient="+12025550123",
                message=TextMessage(body="hello"),
                correlation_id="correlation-1",
                idempotency_key=" ",
            ),
        )
        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_normalized_error_has_only_safe_normalized_fields(self) -> None:
        error = NormalizedError(
            category=ErrorCategory.TIMEOUT,
            safe_message="Provider outcome is unknown",
            provider_code="TIMEOUT",
            retriable=True,
            unknown_outcome=True,
        )
        self.assertEqual(error.category, ErrorCategory.TIMEOUT)
        self.assertEqual(error.safe_message, "Provider outcome is unknown")
        self.assertNotIn("raw_response", {field.name for field in dataclasses.fields(error)})
        self.assertNotIn("credentials", {field.name for field in dataclasses.fields(error)})

    def test_normalized_error_optional_transport_fields_are_validated(self) -> None:
        error = NormalizedError(
            category=ErrorCategory.RATE_LIMIT,
            safe_message="Provider rejected the request",
            retriable=True,
            unknown_outcome=False,
            http_status=429,
            provider_subcode=12,
            retry_after_seconds=60,
        )
        self.assertEqual(error.http_status, 429)
        self.assertEqual(error.provider_subcode, 12)
        self.assertEqual(error.retry_after_seconds, 60)

        invalid_values = (
            {"http_status": 99},
            {"http_status": True},
            {"provider_subcode": -1},
            {"provider_subcode": True},
            {"retry_after_seconds": -1},
            {"retry_after_seconds": 86_401},
            {"retry_after_seconds": True},
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(ValueError):
                NormalizedError(
                    category=ErrorCategory.UNKNOWN,
                    safe_message="Safe error",
                    retriable=False,
                    unknown_outcome=False,
                    **values,
                )

    def test_text_request_construction(self) -> None:
        message = TextMessage(body="Appointment confirmed")
        request = MessageRequest(
            recipient="+12025550123",
            message=message,
            correlation_id="correlation-text",
            idempotency_key="idempotency-text",
        )
        self.assertIs(request.message, message)

    def test_template_request_preserves_component_and_parameter_order(self) -> None:
        first = TextParameter(text="first")
        second = TextParameter(text="second")
        header = TemplateComponent(
            component_type=TemplateComponentType.HEADER,
            parameters=(first,),
        )
        body = TemplateComponent(
            component_type=TemplateComponentType.BODY,
            parameters=(second, first),
        )
        message = TemplateMessage(
            template=TemplateReference(name="appointment", language_code="en_IN"),
            components=(header, body),
        )
        request = MessageRequest(
            recipient="+12025550123",
            message=message,
            correlation_id="correlation-template",
            idempotency_key="idempotency-template",
        )
        self.assertIsInstance(request.message, TemplateMessage)
        assert isinstance(request.message, TemplateMessage)
        self.assertEqual(request.message.components, (header, body))
        self.assertEqual(request.message.components[1].parameters, (second, first))

    def test_component_and_message_require_tuples(self) -> None:
        parameter = TextParameter(text="value")
        with self.assertRaises(TypeError):
            TemplateComponent(
                component_type=TemplateComponentType.BODY,
                parameters=cast(tuple[TextParameter, ...], [parameter]),
            )

        with self.assertRaises(TypeError):
            TemplateMessage(
                template=TemplateReference(name="appointment", language_code="en"),
                components=cast(tuple[TemplateComponent, ...], []),
            )

    def test_send_result_validation_and_immutability(self) -> None:
        result = SendResult(
            provider_key="fake",
            disposition=SendDisposition.ACCEPTED,
            correlation_id="correlation-1",
            provider_message_id="message-1",
            provider_status="submitted",
        )
        self.assertEqual(result.disposition, SendDisposition.ACCEPTED)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _set_attribute(result, "provider_status", "changed")


if __name__ == "__main__":
    unittest.main()
