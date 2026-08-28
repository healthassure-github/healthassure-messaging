from __future__ import annotations

import json
import logging
import threading
import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import Literal, cast
from unittest.mock import patch

from healthassure_messaging import (
    DispatchResult,
    ErrorCategory,
    ExtraTemplateParameterError,
    FakeMessagingProvider,
    IdempotencyConflictError,
    InMemoryIntentRepository,
    InMemoryRecipientEligibilityPolicy,
    InMemoryTemplateCatalog,
    InMemoryTextSessionPolicy,
    IntentCreationResult,
    IntentInProgressError,
    IntentState,
    MessageIntent,
    MessageRequest,
    MessagingGateway,
    MessagingProvider,
    MessagingService,
    MetaCloudConfig,
    MetaCloudProvider,
    MissingTemplateParameterError,
    NormalizedError,
    PostDispatchPersistenceError,
    ProviderNotFoundError,
    ProviderRegistry,
    RecipientEligibility,
    SendDisposition,
    SendResult,
    ServiceDependencyError,
    TemplateAlias,
    TemplateComponentSpec,
    TemplateComponentType,
    UnknownTemplateError,
)
from healthassure_messaging.http import HttpOutcome, HttpResponse

SYNTHETIC_RECIPIENT = "+12025550125"
SECOND_SYNTHETIC_RECIPIENT = "+12025550126"
SYNTHETIC_TOKEN = "synthetic-token-never-use"


class _FixedIdFactory:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"{self._prefix}-{self.calls}"


class _RecordingProvider:
    def __init__(
        self,
        *,
        key: str,
        disposition: SendDisposition = SendDisposition.ACCEPTED,
        provider_message_id: str | None = "synthetic-provider-message",
        events: list[str] | None = None,
        exception_text: str | None = None,
        result_provider_key: str | None = None,
        result_correlation_id: str | None = None,
    ) -> None:
        self._key = key
        self._disposition = disposition
        self._provider_message_id = provider_message_id
        self._events = events
        self._exception_text = exception_text
        self._result_provider_key = result_provider_key
        self._result_correlation_id = result_correlation_id
        self.requests: list[MessageRequest] = []

    @property
    def key(self) -> str:
        return self._key

    def send(self, request: MessageRequest) -> SendResult:
        self.requests.append(request)
        if self._events is not None:
            self._events.append("provider_send")
        if self._exception_text is not None:
            raise RuntimeError(self._exception_text)
        error = None
        if self._disposition is SendDisposition.REJECTED:
            error = NormalizedError(
                category=ErrorCategory.PROVIDER_PERMANENT,
                safe_message="Provider rejected the request",
                retriable=False,
                unknown_outcome=False,
                provider_code="synthetic-code",
            )
        elif self._disposition is SendDisposition.UNKNOWN:
            error = NormalizedError(
                category=ErrorCategory.NETWORK,
                safe_message="Provider outcome is unknown",
                retriable=False,
                unknown_outcome=True,
            )
        return SendResult(
            provider_key=self._result_provider_key or self.key,
            disposition=self._disposition,
            correlation_id=self._result_correlation_id or request.correlation_id,
            provider_message_id=self._provider_message_id,
            provider_status=self._disposition.value,
            error=error,
        )


class _BlockingProvider(_RecordingProvider):
    def __init__(self) -> None:
        super().__init__(key="primary")
        self.entered = threading.Event()
        self.release = threading.Event()

    def send(self, request: MessageRequest) -> SendResult:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("synthetic provider test timeout")
        return super().send(request)


class _ProviderNotFoundAfterInvocation:
    def __init__(self, unsafe_exception_text: str) -> None:
        self._unsafe_exception_text = unsafe_exception_text
        self.requests: list[MessageRequest] = []

    @property
    def key(self) -> str:
        return "primary"

    def send(self, request: MessageRequest) -> SendResult:
        self.requests.append(request)
        raise ProviderNotFoundError(self._unsafe_exception_text)


class _SkipFirstClaimRepository(InMemoryIntentRepository):
    def __init__(self) -> None:
        super().__init__()
        self._skip = True

    def claim(self, intent_id: str):  # type: ignore[no-untyped-def]
        if self._skip:
            self._skip = False
            return None
        return super().claim(intent_id)


_RepositoryStage = Literal["get", "create", "claim", "complete"]
_RepositoryBehavior = Literal["raise", "invalid", "none"]


class _RepositoryDouble(InMemoryIntentRepository):
    def __init__(
        self,
        *,
        stage: _RepositoryStage | None = None,
        behavior: _RepositoryBehavior = "raise",
        unsafe_exception_text: str = "unsafe repository detail",
        corrupt_stored_request: bool = False,
    ) -> None:
        super().__init__()
        self._stage = stage
        self._behavior = behavior
        self._unsafe_exception_text = unsafe_exception_text
        self._corrupt_stored_request = corrupt_stored_request
        self.complete_calls = 0

    def _raise_if_configured(self, stage: _RepositoryStage) -> None:
        if self._stage == stage and self._behavior == "raise":
            raise RuntimeError(self._unsafe_exception_text)

    def get(self, source_flow: str, idempotency_key: str) -> MessageIntent | None:
        self._raise_if_configured("get")
        if self._stage == "get" and self._behavior == "invalid":
            return cast(MessageIntent, object())
        return super().get(source_flow, idempotency_key)

    def create_if_absent(self, intent: MessageIntent) -> IntentCreationResult:
        self._raise_if_configured("create")
        if self._stage == "create" and self._behavior == "invalid":
            return cast(IntentCreationResult, object())
        if self._corrupt_stored_request:
            intent = replace(intent, serialized_request="{synthetic malformed request")
        return super().create_if_absent(intent)

    def claim(self, intent_id: str) -> MessageIntent | None:
        self._raise_if_configured("claim")
        if self._stage == "claim" and self._behavior == "invalid":
            return cast(MessageIntent, object())
        return super().claim(intent_id)

    def complete(
        self,
        intent_id: str,
        *,
        expected_state: IntentState,
        result: DispatchResult,
    ) -> MessageIntent | None:
        self.complete_calls += 1
        self._raise_if_configured("complete")
        if self._stage == "complete" and self._behavior == "invalid":
            return cast(MessageIntent, object())
        if self._stage == "complete" and self._behavior == "none":
            return None
        return super().complete(
            intent_id,
            expected_state=expected_state,
            result=result,
        )


class _RecordingEligibilityPolicy:
    def __init__(self, eligibility: RecipientEligibility) -> None:
        self.eligibility = eligibility
        self.recipients: list[str] = []

    def evaluate(self, recipient: str) -> RecipientEligibility:
        self.recipients.append(recipient)
        return self.eligibility


class _MetaRecordingTransport:
    def __init__(self) -> None:
        self.json_bodies: list[Mapping[str, object]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
        timeout: tuple[float, float],
    ) -> HttpOutcome:
        self.json_bodies.append(json_body)
        return HttpResponse(
            status_code=200,
            body=b'{"messages":[{"id":"synthetic-provider-message"}]}',
        )


def _template_alias(
    *,
    key: str = "synthetic_notice",
    template_name: str = "synthetic_notice_v1",
    components: tuple[TemplateComponentSpec, ...] | None = None,
) -> TemplateAlias:
    if components is None:
        components = (
            TemplateComponentSpec(
                component_type=TemplateComponentType.HEADER,
                parameter_names=("header_second", "header_first"),
            ),
            TemplateComponentSpec(
                component_type=TemplateComponentType.BODY,
                parameter_names=("body_second", "body_first"),
            ),
        )
    return TemplateAlias(
        key=key,
        provider_key="primary",
        template_name=template_name,
        language_code="en_US",
        components=components,
    )


class MessagingServiceTests(unittest.TestCase):
    def _service(
        self,
        *,
        provider: MessagingProvider | None = None,
        aliases: tuple[TemplateAlias, ...] | None = None,
        consented: tuple[str, ...] = (SYNTHETIC_RECIPIENT,),
        suppressed: tuple[str, ...] = (),
        active_sessions: tuple[tuple[str, str], ...] = (
            (SYNTHETIC_RECIPIENT, "synthetic_purpose"),
        ),
        repository: InMemoryIntentRepository | None = None,
        recipient_policy: _RecordingEligibilityPolicy | None = None,
        events: list[str] | None = None,
        register_provider: bool = True,
    ) -> tuple[
        MessagingService,
        MessagingProvider,
        InMemoryIntentRepository,
        _FixedIdFactory,
        _FixedIdFactory,
    ]:
        selected_provider = provider or _RecordingProvider(key="primary", events=events)
        registry = ProviderRegistry()
        if register_provider:
            registry.register("primary", selected_provider)
        selected_repository = repository or InMemoryIntentRepository(event_sink=events)
        correlation_ids = _FixedIdFactory("correlation")
        intent_ids = _FixedIdFactory("intent")
        service = MessagingService(
            gateway=MessagingGateway(registry),
            template_catalog=InMemoryTemplateCatalog(aliases or (_template_alias(),)),
            recipient_policy=(
                recipient_policy
                or InMemoryRecipientEligibilityPolicy(
                    consented_recipients=consented,
                    suppressed_recipients=suppressed,
                )
            ),
            text_session_policy=InMemoryTextSessionPolicy(active_sessions),
            intent_repository=selected_repository,
            text_provider_key="primary",
            correlation_id_factory=correlation_ids,
            intent_id_factory=intent_ids,
        )
        return service, selected_provider, selected_repository, correlation_ids, intent_ids

    @staticmethod
    def _send_template(service: MessagingService, **overrides: object):  # type: ignore[no-untyped-def]
        arguments: dict[str, object] = {
            "recipient": SYNTHETIC_RECIPIENT,
            "template_key": "synthetic_notice",
            "parameters": {
                "header_first": "header-1",
                "header_second": "header-2",
                "body_first": "body-1",
                "body_second": "body-2",
            },
            "source_flow": "synthetic_flow",
            "idempotency_key": "synthetic-idempotency",
            "actor_id": "synthetic-actor",
        }
        arguments.update(overrides)
        return service.send_template(**arguments)  # type: ignore[arg-type]

    @staticmethod
    def _send_text(service: MessagingService, **overrides: object):  # type: ignore[no-untyped-def]
        arguments: dict[str, object] = {
            "recipient": SYNTHETIC_RECIPIENT,
            "body": "Synthetic session text",
            "purpose_key": "synthetic_purpose",
            "source_flow": "synthetic_flow",
            "idempotency_key": "synthetic-idempotency",
            "actor_id": "synthetic-actor",
        }
        arguments.update(overrides)
        return service.send_text(**arguments)  # type: ignore[arg-type]

    def test_template_order_and_persist_before_send(self) -> None:
        events: list[str] = []
        service, provider, repository, _, _ = self._service(events=events)
        result = self._send_template(service)

        recording_provider = cast(_RecordingProvider, provider)
        request = recording_provider.requests[0]
        message = request.message
        self.assertEqual(result.disposition, SendDisposition.ACCEPTED)
        self.assertEqual(events, ["create", "claim", "provider_send", "complete"])
        self.assertEqual(repository.events, ("create", "claim", "complete"))
        self.assertEqual(request.recipient, SYNTHETIC_RECIPIENT)
        self.assertEqual(
            tuple(component.component_type for component in message.components),  # type: ignore[union-attr]
            (TemplateComponentType.HEADER, TemplateComponentType.BODY),
        )
        self.assertEqual(
            tuple(parameter.text for parameter in message.components[0].parameters),  # type: ignore[union-attr]
            ("header-2", "header-1"),
        )
        intent = repository.get("synthetic_flow", "synthetic-idempotency")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(json.loads(intent.serialized_request)["schema_version"], 1)
        self.assertEqual(intent.actor_id, "synthetic-actor")

    def test_recipient_is_normalized_before_policy_and_persistence(self) -> None:
        policy = _RecordingEligibilityPolicy(
            RecipientEligibility(consented=True, suppressed=False)
        )
        service, provider, repository, _, _ = self._service(recipient_policy=policy)
        self._send_text(service, recipient="+1 (202) 555-0125")
        self.assertEqual(policy.recipients, [SYNTHETIC_RECIPIENT])
        recording_provider = cast(_RecordingProvider, provider)
        self.assertEqual(recording_provider.requests[0].recipient, SYNTHETIC_RECIPIENT)
        intent = repository.get("synthetic_flow", "synthetic-idempotency")
        assert intent is not None
        self.assertIn(SYNTHETIC_RECIPIENT, intent.serialized_request)

    def test_component_free_service_template_is_omitted_by_meta_adapter(self) -> None:
        transport = _MetaRecordingTransport()
        meta_provider = MetaCloudProvider(
            MetaCloudConfig(
                graph_version="v999.0",
                phone_number_id="1234567890",
                access_token=SYNTHETIC_TOKEN,
                connect_timeout=1.0,
                read_timeout=2.0,
            ),
            transport=transport,
        )
        registry = ProviderRegistry()
        registry.register("meta", meta_provider)
        alias = TemplateAlias(
            key="synthetic_static",
            provider_key="meta",
            template_name="synthetic_static_v1",
            language_code="en_US",
        )
        service = MessagingService(
            gateway=MessagingGateway(registry),
            template_catalog=InMemoryTemplateCatalog((alias,)),
            recipient_policy=InMemoryRecipientEligibilityPolicy(
                consented_recipients=(SYNTHETIC_RECIPIENT,)
            ),
            text_session_policy=InMemoryTextSessionPolicy(),
            intent_repository=InMemoryIntentRepository(),
            text_provider_key="meta",
            correlation_id_factory=_FixedIdFactory("correlation"),
            intent_id_factory=_FixedIdFactory("intent"),
        )
        result = service.send_template(
            recipient=SYNTHETIC_RECIPIENT,
            template_key="synthetic_static",
            parameters={},
            source_flow="synthetic_flow",
            idempotency_key="synthetic-idempotency",
            actor_id="synthetic-actor",
        )
        self.assertEqual(result.disposition, SendDisposition.ACCEPTED)
        payload = transport.json_bodies[0]
        template = cast(Mapping[str, object], payload["template"])
        self.assertNotIn("components", template)

    def test_consent_missing_and_suppression_are_persisted_local_rejections(self) -> None:
        cases = (
            ((), (), "missing-consent"),
            ((SYNTHETIC_RECIPIENT,), (SYNTHETIC_RECIPIENT,), "suppressed"),
        )
        for consented, suppressed, label in cases:
            with self.subTest(case=label):
                service, provider, repository, _, _ = self._service(
                    consented=consented,
                    suppressed=suppressed,
                )
                result = self._send_template(service)
                self.assertEqual(result.disposition, SendDisposition.REJECTED)
                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.category, ErrorCategory.AUTHORIZATION)
                self.assertFalse(result.error.retriable)
                self.assertFalse(result.error.unknown_outcome)
                self.assertEqual(cast(_RecordingProvider, provider).requests, [])
                intent = repository.get("synthetic_flow", "synthetic-idempotency")
                assert intent is not None
                self.assertEqual(intent.state, IntentState.REJECTED)
                self.assertIn('"schema_version":1', intent.serialized_request)

    def test_text_requires_an_active_session(self) -> None:
        inactive, provider, repository, _, _ = self._service(active_sessions=())
        rejected = self._send_text(inactive)
        self.assertEqual(rejected.disposition, SendDisposition.REJECTED)
        self.assertEqual(cast(_RecordingProvider, provider).requests, [])
        intent = repository.get("synthetic_flow", "synthetic-idempotency")
        assert intent is not None
        self.assertEqual(intent.state, IntentState.REJECTED)

        active, active_provider, _, _, _ = self._service()
        accepted = self._send_text(active)
        self.assertEqual(accepted.disposition, SendDisposition.ACCEPTED)
        self.assertEqual(len(cast(_RecordingProvider, active_provider).requests), 1)

    def test_unknown_template_and_parameter_errors_prevent_provider_invocation(self) -> None:
        service, provider, _, _, _ = self._service()
        with self.assertRaises(UnknownTemplateError):
            self._send_template(service, template_key="unknown_synthetic")
        with self.assertRaises(MissingTemplateParameterError):
            self._send_template(service, parameters={})
        with self.assertRaises(ExtraTemplateParameterError):
            self._send_template(
                service,
                parameters={
                    "header_first": "header-1",
                    "header_second": "header-2",
                    "body_first": "body-1",
                    "body_second": "body-2",
                    "extra": "value",
                },
            )
        self.assertEqual(cast(_RecordingProvider, provider).requests, [])

    def test_missing_provider_is_a_replayable_definite_local_rejection(self) -> None:
        service, provider, repository, _, _ = self._service(register_provider=False)
        first = self._send_template(service)
        second = self._send_template(service)

        self.assertEqual(first.disposition, SendDisposition.REJECTED)
        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.assertIsNotNone(first.error)
        assert first.error is not None
        self.assertEqual(first.error.category, ErrorCategory.UNSUPPORTED)
        self.assertFalse(first.error.retriable)
        self.assertFalse(first.error.unknown_outcome)
        self.assertNotIn("primary", first.error.safe_message)
        self.assertNotIn(SYNTHETIC_RECIPIENT, first.error.safe_message)
        self.assertEqual(cast(_RecordingProvider, provider).requests, [])
        self.assertEqual(repository.events, ("create", "claim", "complete"))
        intent = repository.get("synthetic_flow", "synthetic-idempotency")
        assert intent is not None
        self.assertEqual(intent.state, IntentState.REJECTED)
        self.assertEqual(intent.result, first)

    def test_adapter_provider_not_found_error_is_unknown_and_replayable(self) -> None:
        unsafe = (
            f"unsafe adapter {SYNTHETIC_RECIPIENT} private body {SYNTHETIC_TOKEN}"
        )
        provider = _ProviderNotFoundAfterInvocation(unsafe)
        repository = _RepositoryDouble()
        service, _, _, _, _ = self._service(
            provider=provider,
            repository=repository,
        )

        with patch.object(logging.Logger, "_log") as log_call:
            first = self._send_text(service)
            replay = self._send_text(service)

        self.assertEqual(first.disposition, SendDisposition.UNKNOWN)
        self.assertFalse(first.idempotent_replay)
        self.assertTrue(replay.idempotent_replay)
        self.assertIsNotNone(first.error)
        assert first.error is not None
        self.assertEqual(first.error.category, ErrorCategory.UNKNOWN)
        self.assertFalse(first.error.retriable)
        self.assertTrue(first.error.unknown_outcome)
        self.assertNotIn(unsafe, repr(first))
        self.assertNotIn(SYNTHETIC_RECIPIENT, repr(first.error))
        self.assertNotIn(SYNTHETIC_TOKEN, repr(first.error))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(repository.complete_calls, 1)
        self.assertEqual(repository.events, ("create", "claim", "complete"))
        intent = repository.get("synthetic_flow", "synthetic-idempotency")
        assert intent is not None
        self.assertEqual(intent.state, IntentState.UNKNOWN)
        self.assertEqual(intent.result, first)
        self.assertNotIn(unsafe, repr(intent))
        log_call.assert_not_called()

    def test_pre_dispatch_repository_exceptions_are_sanitized_and_provider_free(self) -> None:
        stages: tuple[_RepositoryStage, ...] = ("get", "create", "claim")
        for stage in stages:
            with self.subTest(stage=stage):
                unsafe = f"unsafe {stage} {SYNTHETIC_RECIPIENT} private body {SYNTHETIC_TOKEN}"
                repository = _RepositoryDouble(
                    stage=stage,
                    behavior="raise",
                    unsafe_exception_text=unsafe,
                )
                service, provider, _, _, _ = self._service(repository=repository)
                with (
                    patch.object(logging.Logger, "_log") as log_call,
                    self.assertRaises(ServiceDependencyError) as caught,
                ):
                    self._send_text(service)
                self.assertNotIsInstance(caught.exception, PostDispatchPersistenceError)
                self.assertNotIn(unsafe, str(caught.exception))
                self.assertNotIn(SYNTHETIC_RECIPIENT, repr(caught.exception))
                self.assertNotIn(SYNTHETIC_TOKEN, repr(caught.exception))
                self.assertEqual(cast(_RecordingProvider, provider).requests, [])
                self.assertEqual(repository.complete_calls, 0)
                log_call.assert_not_called()
                if stage == "claim":
                    intent = repository.get("synthetic_flow", "synthetic-idempotency")
                    assert intent is not None
                    self.assertEqual(intent.state, IntentState.PENDING)

    def test_invalid_pre_dispatch_repository_returns_are_provider_free(self) -> None:
        stages: tuple[_RepositoryStage, ...] = ("get", "create", "claim")
        for stage in stages:
            with self.subTest(stage=stage):
                repository = _RepositoryDouble(
                    stage=stage,
                    behavior="invalid",
                )
                service, provider, _, _, _ = self._service(repository=repository)
                with self.assertRaises(ServiceDependencyError) as caught:
                    self._send_text(service)
                self.assertNotIsInstance(caught.exception, PostDispatchPersistenceError)
                self.assertEqual(cast(_RecordingProvider, provider).requests, [])
                self.assertEqual(repository.complete_calls, 0)

    def test_stored_request_deserialization_failure_is_provider_free(self) -> None:
        repository = _RepositoryDouble(corrupt_stored_request=True)
        service, provider, _, _, _ = self._service(repository=repository)
        with self.assertRaises(ServiceDependencyError) as caught:
            self._send_text(service)
        self.assertNotIsInstance(caught.exception, PostDispatchPersistenceError)
        self.assertEqual(str(caught.exception), "stored request could not be restored")
        self.assertNotIn("synthetic malformed request", repr(caught.exception))
        self.assertEqual(cast(_RecordingProvider, provider).requests, [])
        self.assertEqual(repository.complete_calls, 0)
        intent = repository.get("synthetic_flow", "synthetic-idempotency")
        assert intent is not None
        self.assertEqual(intent.state, IntentState.DISPATCHING)

    def test_provider_contract_violations_are_unknown_without_retry(self) -> None:
        cases = (
            ("wrong-provider", None),
            (None, "wrong-correlation"),
        )
        for result_provider_key, result_correlation_id in cases:
            with self.subTest(
                result_provider_key=result_provider_key,
                result_correlation_id=result_correlation_id,
            ):
                provider = _RecordingProvider(
                    key="primary",
                    result_provider_key=result_provider_key,
                    result_correlation_id=result_correlation_id,
                )
                service, _, repository, _, _ = self._service(provider=provider)
                first = self._send_text(service)
                replay = self._send_text(service)
                self.assertEqual(first.disposition, SendDisposition.UNKNOWN)
                self.assertIsNotNone(first.error)
                assert first.error is not None
                self.assertEqual(first.error.category, ErrorCategory.UNKNOWN)
                self.assertTrue(first.error.unknown_outcome)
                self.assertFalse(first.error.retriable)
                self.assertTrue(replay.idempotent_replay)
                self.assertEqual(len(provider.requests), 1)
                intent = repository.get("synthetic_flow", "synthetic-idempotency")
                assert intent is not None
                self.assertEqual(intent.state, IntentState.UNKNOWN)

    def test_post_dispatch_completion_failures_are_typed_and_never_retried(self) -> None:
        behaviors: tuple[_RepositoryBehavior, ...] = ("raise", "none", "invalid")
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                unsafe = (
                    f"unsafe completion {SYNTHETIC_RECIPIENT} private body {SYNTHETIC_TOKEN}"
                )
                repository = _RepositoryDouble(
                    stage="complete",
                    behavior=behavior,
                    unsafe_exception_text=unsafe,
                )
                provider = _RecordingProvider(key="primary")
                service, _, _, _, _ = self._service(
                    provider=provider,
                    repository=repository,
                )
                with (
                    patch.object(logging.Logger, "_log") as log_call,
                    self.assertRaises(PostDispatchPersistenceError) as caught,
                ):
                    self._send_text(service)
                self.assertEqual(
                    str(caught.exception),
                    "Provider result could not be persisted",
                )
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn(unsafe, repr(caught.exception))
                self.assertNotIn(SYNTHETIC_RECIPIENT, repr(caught.exception))
                self.assertNotIn(SYNTHETIC_TOKEN, repr(caught.exception))
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(repository.complete_calls, 1)
                intent = repository.get("synthetic_flow", "synthetic-idempotency")
                assert intent is not None
                self.assertEqual(intent.state, IntentState.DISPATCHING)
                self.assertIsNone(intent.result)
                with self.assertRaises(IntentInProgressError):
                    self._send_text(service)
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(repository.complete_calls, 1)
                log_call.assert_not_called()

    def test_provider_outcomes_are_persisted_and_propagated(self) -> None:
        expected_states = {
            SendDisposition.ACCEPTED: IntentState.ACCEPTED,
            SendDisposition.REJECTED: IntentState.REJECTED,
            SendDisposition.UNKNOWN: IntentState.UNKNOWN,
        }
        for disposition, expected_state in expected_states.items():
            with self.subTest(disposition=disposition):
                provider = _RecordingProvider(key="primary", disposition=disposition)
                service, _, repository, _, _ = self._service(provider=provider)
                result = self._send_template(service)
                self.assertEqual(result.disposition, disposition)
                self.assertEqual(result.provider_message_id, "synthetic-provider-message")
                intent = repository.get("synthetic_flow", "synthetic-idempotency")
                assert intent is not None
                self.assertEqual(intent.state, expected_state)
                self.assertEqual(intent.result, result)

    def test_same_request_replays_terminal_result_with_one_provider_call(self) -> None:
        service, provider, repository, correlations, intent_ids = self._service()
        first = self._send_template(service)
        second = self._send_template(service)
        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(first.correlation_id, second.correlation_id)
        self.assertEqual(first.provider_message_id, second.provider_message_id)
        self.assertEqual(len(cast(_RecordingProvider, provider).requests), 1)
        self.assertEqual(repository.events, ("create", "claim", "complete"))
        self.assertEqual(correlations.calls, 1)
        self.assertEqual(intent_ids.calls, 1)

    def test_idempotency_conflicts_cover_recipient_content_template_and_parameters(self) -> None:
        variants: tuple[tuple[str, dict[str, object]], ...] = (
            ("recipient", {"recipient": SECOND_SYNTHETIC_RECIPIENT}),
            ("content", {"body": "Different synthetic text"}),
            ("purpose", {"purpose_key": "different_purpose"}),
        )
        for label, overrides in variants:
            with self.subTest(kind=label):
                service, _, _, _, _ = self._service(
                    consented=(SYNTHETIC_RECIPIENT, SECOND_SYNTHETIC_RECIPIENT),
                )
                self._send_text(service)
                with self.assertRaises(IdempotencyConflictError):
                    self._send_text(service, **overrides)

        second_alias = _template_alias(
            key="synthetic_other",
            template_name="synthetic_other_v1",
        )
        service, _, _, _, _ = self._service(aliases=(_template_alias(), second_alias))
        self._send_template(service)
        with self.assertRaises(IdempotencyConflictError):
            self._send_template(service, template_key="synthetic_other")

        service, _, _, _, _ = self._service()
        self._send_template(service)
        with self.assertRaises(IdempotencyConflictError):
            self._send_template(
                service,
                parameters={
                    "header_first": "different",
                    "header_second": "header-2",
                    "body_first": "body-1",
                    "body_second": "body-2",
                },
            )

    def test_dispatching_duplicate_is_blocked_concurrently(self) -> None:
        provider = _BlockingProvider()
        service, _, _, _, _ = self._service(provider=provider)
        results: list[SendDisposition] = []
        failures: list[type[BaseException]] = []

        def first_send() -> None:
            try:
                results.append(self._send_text(service).disposition)
            except BaseException as error:  # pragma: no cover - asserted as an empty list
                failures.append(type(error))

        thread = threading.Thread(target=first_send)
        thread.start()
        self.assertTrue(provider.entered.wait(timeout=5))
        with self.assertRaises(IntentInProgressError):
            self._send_text(service)
        provider.release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [SendDisposition.ACCEPTED])
        self.assertEqual(len(provider.requests), 1)

    def test_pending_never_dispatched_intent_can_be_recovered(self) -> None:
        repository = _SkipFirstClaimRepository()
        service, provider, _, correlations, intent_ids = self._service(repository=repository)
        with self.assertRaises(IntentInProgressError):
            self._send_text(service)
        pending = repository.get("synthetic_flow", "synthetic-idempotency")
        assert pending is not None
        self.assertEqual(pending.state, IntentState.PENDING)
        recovered = self._send_text(service)
        self.assertEqual(recovered.disposition, SendDisposition.ACCEPTED)
        self.assertEqual(len(cast(_RecordingProvider, provider).requests), 1)
        self.assertEqual(correlations.calls, 1)
        self.assertEqual(intent_ids.calls, 1)

    def test_unexpected_provider_exception_becomes_unknown_without_retry_or_leak(self) -> None:
        unsafe = f"unsafe {SYNTHETIC_RECIPIENT} body {SYNTHETIC_TOKEN}"
        provider = _RecordingProvider(key="primary", exception_text=unsafe)
        service, _, repository, _, _ = self._service(provider=provider)
        with patch.object(logging.Logger, "_log") as log_call:
            result = self._send_text(service, body="private synthetic body")
        self.assertEqual(result.disposition, SendDisposition.UNKNOWN)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.category, ErrorCategory.UNKNOWN)
        self.assertTrue(result.error.unknown_outcome)
        self.assertFalse(result.error.retriable)
        self.assertNotIn(SYNTHETIC_RECIPIENT, repr(result))
        self.assertNotIn("private synthetic body", repr(result))
        self.assertNotIn(SYNTHETIC_TOKEN, repr(result))
        self.assertNotIn(unsafe, result.error.safe_message)
        log_call.assert_not_called()
        self.assertEqual(len(provider.requests), 1)
        intent = repository.get("synthetic_flow", "synthetic-idempotency")
        assert intent is not None
        self.assertEqual(intent.state, IntentState.UNKNOWN)
        self.assertNotIn(SYNTHETIC_RECIPIENT, repr(intent))
        self.assertNotIn("private synthetic body", repr(intent))

    def test_selected_provider_never_falls_back(self) -> None:
        selected = _RecordingProvider(
            key="primary",
            disposition=SendDisposition.REJECTED,
        )
        fallback = FakeMessagingProvider(
            key="fallback",
            disposition=SendDisposition.ACCEPTED,
        )
        registry = ProviderRegistry()
        registry.register("primary", selected)
        registry.register("fallback", fallback)
        service = MessagingService(
            gateway=MessagingGateway(registry),
            template_catalog=InMemoryTemplateCatalog((_template_alias(),)),
            recipient_policy=InMemoryRecipientEligibilityPolicy(
                consented_recipients=(SYNTHETIC_RECIPIENT,)
            ),
            text_session_policy=InMemoryTextSessionPolicy(
                ((SYNTHETIC_RECIPIENT, "synthetic_purpose"),)
            ),
            intent_repository=InMemoryIntentRepository(),
            text_provider_key="primary",
            correlation_id_factory=_FixedIdFactory("correlation"),
            intent_id_factory=_FixedIdFactory("intent"),
        )
        result = self._send_text(service)
        self.assertEqual(result.disposition, SendDisposition.REJECTED)
        self.assertEqual(len(selected.requests), 1)
        self.assertEqual(fallback.received_requests, ())

    def test_correlation_and_provider_message_id_propagate(self) -> None:
        service, _, _, _, _ = self._service()
        result = self._send_text(service)
        self.assertEqual(result.correlation_id, "correlation-1")
        self.assertEqual(result.provider_message_id, "synthetic-provider-message")
        self.assertEqual(result.provider_key, "primary")


if __name__ == "__main__":
    unittest.main()
