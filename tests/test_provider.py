from __future__ import annotations

import unittest

from healthassure_messaging import (
    DuplicateProviderError,
    ErrorCategory,
    FakeMessagingProvider,
    MessageRequest,
    MessagingGateway,
    NormalizedError,
    ProviderContractError,
    ProviderNotFoundError,
    ProviderRegistry,
    SendDisposition,
    SendResult,
    TextMessage,
)

UNSAFE_PROVIDER_DETAIL = "unsafe provider detail +12025550125 private body token-value"


class _ProviderNotFoundAfterInvocation:
    def __init__(self) -> None:
        self.requests: list[MessageRequest] = []

    @property
    def key(self) -> str:
        return "fake"

    def send(self, request: MessageRequest) -> SendResult:
        self.requests.append(request)
        raise ProviderNotFoundError(UNSAFE_PROVIDER_DETAIL)


class _FixedResultProvider:
    def __init__(self, result: SendResult) -> None:
        self._result = result
        self.requests: list[MessageRequest] = []

    @property
    def key(self) -> str:
        return "fake"

    def send(self, request: MessageRequest) -> SendResult:
        self.requests.append(request)
        return self._result


def _request(correlation_id: str = "correlation-1") -> MessageRequest:
    return MessageRequest(
        recipient="+12025550123",
        message=TextMessage(body="Hello"),
        correlation_id=correlation_id,
        idempotency_key=f"idempotency-{correlation_id}",
    )


class ProviderTests(unittest.TestCase):
    def test_duplicate_registration_fails(self) -> None:
        registry = ProviderRegistry()
        provider = FakeMessagingProvider(key="fake", disposition=SendDisposition.ACCEPTED)
        registry.register("fake", provider)
        with self.assertRaises(DuplicateProviderError):
            registry.register("fake", provider)

    def test_missing_provider_fails_explicitly(self) -> None:
        gateway = MessagingGateway(ProviderRegistry())
        with self.assertRaises(ProviderNotFoundError):
            gateway.send(provider_key="missing", request=_request())

    def test_registered_provider_not_found_error_is_a_sanitized_contract_error(self) -> None:
        provider = _ProviderNotFoundAfterInvocation()
        registry = ProviderRegistry()
        registry.register("fake", provider)
        request = _request()

        with self.assertRaises(ProviderContractError) as caught:
            MessagingGateway(registry).send(provider_key="fake", request=request)

        self.assertEqual(
            str(caught.exception),
            "Registered provider violated the gateway contract",
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(UNSAFE_PROVIDER_DETAIL, repr(caught.exception))
        self.assertNotIn(request.recipient, repr(caught.exception))
        self.assertIsInstance(request.message, TextMessage)
        assert isinstance(request.message, TextMessage)
        self.assertNotIn(request.message.body, repr(caught.exception))
        self.assertEqual(provider.requests, [request])

    def test_gateway_result_contract_checks_remain_enforced(self) -> None:
        request = _request()
        invalid_results = (
            SendResult(
                provider_key="wrong-provider",
                disposition=SendDisposition.ACCEPTED,
                correlation_id=request.correlation_id,
            ),
            SendResult(
                provider_key="fake",
                disposition=SendDisposition.ACCEPTED,
                correlation_id="wrong-correlation",
            ),
        )
        for invalid_result in invalid_results:
            with self.subTest(invalid_result=invalid_result):
                provider = _FixedResultProvider(invalid_result)
                registry = ProviderRegistry()
                registry.register("fake", provider)
                with self.assertRaises(ProviderContractError):
                    MessagingGateway(registry).send(provider_key="fake", request=request)
                self.assertEqual(provider.requests, [request])

    def test_explicit_provider_selection_has_no_fallback(self) -> None:
        accepted = FakeMessagingProvider(
            key="accepted",
            disposition=SendDisposition.ACCEPTED,
            provider_message_id="accepted-1",
        )
        rejected = FakeMessagingProvider(
            key="rejected",
            disposition=SendDisposition.REJECTED,
        )
        registry = ProviderRegistry()
        registry.register("accepted", accepted)
        registry.register("rejected", rejected)
        gateway = MessagingGateway(registry)

        request = _request()
        result = gateway.send(provider_key="rejected", request=request)
        self.assertEqual(result.disposition, SendDisposition.REJECTED)
        self.assertEqual(rejected.received_requests, (request,))
        self.assertEqual(accepted.received_requests, ())

    def test_fake_provider_supports_all_dispositions_and_propagates_correlation(self) -> None:
        configured_error = NormalizedError(
            category=ErrorCategory.PROVIDER_TEMPORARY,
            safe_message="Temporary provider failure",
            retriable=True,
            unknown_outcome=False,
        )
        for disposition in SendDisposition:
            with self.subTest(disposition=disposition):
                provider = FakeMessagingProvider(
                    key="fake",
                    disposition=disposition,
                    provider_message_id="fake-message",
                    provider_status="configured-status",
                    error=configured_error if disposition is not SendDisposition.ACCEPTED else None,
                )
                registry = ProviderRegistry()
                registry.register("fake", provider)
                request = _request(correlation_id=f"correlation-{disposition.value}")

                result = MessagingGateway(registry).send(
                    provider_key="fake",
                    request=request,
                )

                self.assertEqual(result.provider_key, "fake")
                self.assertEqual(result.disposition, disposition)
                self.assertEqual(result.correlation_id, request.correlation_id)
                self.assertEqual(result.provider_message_id, "fake-message")
                self.assertEqual(result.provider_status, "configured-status")
                self.assertEqual(provider.received_requests, (request,))

    def test_recorded_request_collection_is_an_immutable_snapshot(self) -> None:
        provider = FakeMessagingProvider(key="fake", disposition=SendDisposition.UNKNOWN)
        first_snapshot = provider.received_requests
        provider.send(_request())
        self.assertEqual(first_snapshot, ())
        self.assertEqual(len(provider.received_requests), 1)


if __name__ == "__main__":
    unittest.main()
