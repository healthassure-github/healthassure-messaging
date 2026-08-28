from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace

from .contracts import (
    Message,
    MessageRequest,
    NormalizedError,
    SendResult,
    TextMessage,
)
from .enums import ErrorCategory, IntentState, SendDisposition
from .phone import normalize_phone_number
from .ports import (
    IntentRepository,
    RecipientEligibilityPolicy,
    TemplateCatalog,
    TextSessionPolicy,
)
from .provider import MessagingGateway, ProviderNotFoundError
from .serialization import deserialize_request, serialize_request
from .service_contracts import (
    DispatchResult,
    IdempotencyConflictError,
    IntentCreationResult,
    IntentInProgressError,
    IntentStateError,
    MessageIntent,
    PostDispatchPersistenceError,
    RecipientEligibility,
    ServiceDependencyError,
    TemplateAlias,
    UnknownTemplateError,
)
from .templates import build_template_message


def _new_identifier() -> str:
    return uuid.uuid4().hex


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _message_fingerprint_data(message: Message) -> dict[str, object]:
    if isinstance(message, TextMessage):
        return {"type": "text", "body": message.body}
    return {
        "type": "template",
        "template": {
            "name": message.template.name,
            "language_code": message.template.language_code,
        },
        "components": [
            {
                "type": component.component_type.value,
                "parameters": [parameter.text for parameter in component.parameters],
            }
            for component in message.components
        ],
    }


def _request_fingerprint(
    *,
    recipient: str,
    provider_key: str,
    message: Message,
    operation_kind: str,
    operation_key: str,
) -> str:
    encoded = json.dumps(
        {
            "recipient": recipient,
            "provider_key": provider_key,
            "operation_kind": operation_kind,
            "operation_key": operation_key,
            "message": _message_fingerprint_data(message),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MessagingService:
    """Provider-neutral orchestration over an explicitly configured gateway."""

    def __init__(
        self,
        *,
        gateway: MessagingGateway,
        template_catalog: TemplateCatalog,
        recipient_policy: RecipientEligibilityPolicy,
        text_session_policy: TextSessionPolicy,
        intent_repository: IntentRepository,
        text_provider_key: str,
        default_region: str | None = None,
        correlation_id_factory: Callable[[], str] = _new_identifier,
        intent_id_factory: Callable[[], str] = _new_identifier,
    ) -> None:
        if not isinstance(gateway, MessagingGateway):
            raise TypeError("gateway must be a MessagingGateway")
        self._gateway = gateway
        self._template_catalog = template_catalog
        self._recipient_policy = recipient_policy
        self._text_session_policy = text_session_policy
        self._intent_repository = intent_repository
        self._text_provider_key = _require_text(text_provider_key, "text_provider_key")
        if default_region is not None:
            _require_text(default_region, "default_region")
        self._default_region = default_region
        if not callable(correlation_id_factory):
            raise TypeError("correlation_id_factory must be callable")
        if not callable(intent_id_factory):
            raise TypeError("intent_id_factory must be callable")
        self._correlation_id_factory = correlation_id_factory
        self._intent_id_factory = intent_id_factory

    def send_template(
        self,
        *,
        recipient: str,
        template_key: str,
        parameters: Mapping[str, str],
        source_flow: str,
        idempotency_key: str,
        actor_id: str,
    ) -> DispatchResult:
        normalized_recipient = normalize_phone_number(
            recipient,
            default_region=self._default_region,
        )
        _require_text(template_key, "template_key")
        alias = self._get_template_alias(template_key)
        message = build_template_message(alias, parameters)
        return self._send(
            recipient=normalized_recipient,
            message=message,
            provider_key=alias.provider_key,
            operation_kind="template",
            operation_key=template_key,
            source_flow=source_flow,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            text_purpose_key=None,
        )

    def send_text(
        self,
        *,
        recipient: str,
        body: str,
        purpose_key: str,
        source_flow: str,
        idempotency_key: str,
        actor_id: str,
    ) -> DispatchResult:
        normalized_recipient = normalize_phone_number(
            recipient,
            default_region=self._default_region,
        )
        _require_text(purpose_key, "purpose_key")
        message = TextMessage(body=body)
        return self._send(
            recipient=normalized_recipient,
            message=message,
            provider_key=self._text_provider_key,
            operation_kind="text",
            operation_key=purpose_key,
            source_flow=source_flow,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            text_purpose_key=purpose_key,
        )

    def _get_template_alias(self, template_key: str) -> TemplateAlias:
        try:
            alias = self._template_catalog.get(template_key)
        except Exception:
            raise ServiceDependencyError("template catalog lookup failed") from None
        if alias is None:
            raise UnknownTemplateError("template alias is not registered")
        return alias

    def _send(
        self,
        *,
        recipient: str,
        message: Message,
        provider_key: str,
        operation_kind: str,
        operation_key: str,
        source_flow: str,
        idempotency_key: str,
        actor_id: str,
        text_purpose_key: str | None,
    ) -> DispatchResult:
        _require_text(source_flow, "source_flow")
        _require_text(idempotency_key, "idempotency_key")
        _require_text(actor_id, "actor_id")
        fingerprint = _request_fingerprint(
            recipient=recipient,
            provider_key=provider_key,
            message=message,
            operation_kind=operation_kind,
            operation_key=operation_key,
        )

        existing = self._get_intent(source_flow, idempotency_key)
        if existing is not None:
            return self._continue_intent(
                existing,
                request_fingerprint=fingerprint,
                recipient=recipient,
                text_purpose_key=text_purpose_key,
            )

        correlation_id = _require_text(
            self._correlation_id_factory(),
            "generated correlation_id",
        )
        intent_id = _require_text(self._intent_id_factory(), "generated intent_id")
        request = MessageRequest(
            recipient=recipient,
            message=message,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        intent = MessageIntent(
            intent_id=intent_id,
            source_flow=source_flow,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            provider_key=provider_key,
            correlation_id=correlation_id,
            request_fingerprint=fingerprint,
            serialized_request=serialize_request(request),
        )
        creation = self._create_intent(intent)
        return self._continue_intent(
            creation.intent,
            request_fingerprint=fingerprint,
            recipient=recipient,
            text_purpose_key=text_purpose_key,
        )

    def _continue_intent(
        self,
        intent: MessageIntent,
        *,
        request_fingerprint: str,
        recipient: str,
        text_purpose_key: str | None,
    ) -> DispatchResult:
        if intent.request_fingerprint != request_fingerprint:
            raise IdempotencyConflictError("idempotency scope is already used by another request")
        if intent.state in (
            IntentState.ACCEPTED,
            IntentState.REJECTED,
            IntentState.UNKNOWN,
        ):
            return self._terminal_replay(intent)
        if intent.state is IntentState.DISPATCHING:
            raise IntentInProgressError("dispatch is already in progress")
        if intent.state is not IntentState.PENDING:
            raise IntentStateError("intent has an unsupported state")

        if not self._is_policy_eligible(recipient, text_purpose_key):
            return self._complete_local_rejection(intent)

        claimed = self._claim_intent(intent.intent_id)
        if claimed is None:
            return self._resolve_claim_race(intent)
        return self._dispatch_claimed(claimed)

    def _is_policy_eligible(self, recipient: str, text_purpose_key: str | None) -> bool:
        try:
            eligibility = self._recipient_policy.evaluate(recipient)
        except Exception:
            raise ServiceDependencyError("recipient policy evaluation failed") from None
        if not isinstance(eligibility, RecipientEligibility):
            raise ServiceDependencyError("recipient policy returned an invalid result")
        if not eligibility.consented or eligibility.suppressed:
            return False
        if text_purpose_key is None:
            return True
        try:
            active = self._text_session_policy.has_active_session(
                recipient,
                text_purpose_key,
            )
        except Exception:
            raise ServiceDependencyError("text session policy evaluation failed") from None
        if type(active) is not bool:
            raise ServiceDependencyError("text session policy returned an invalid result")
        return active

    def _complete_local_rejection(self, intent: MessageIntent) -> DispatchResult:
        result = DispatchResult(
            intent_id=intent.intent_id,
            provider_key=intent.provider_key,
            disposition=SendDisposition.REJECTED,
            correlation_id=intent.correlation_id,
            provider_status="policy_rejected",
            error=NormalizedError(
                category=ErrorCategory.AUTHORIZATION,
                safe_message="Messaging policy denied the request",
                retriable=False,
                unknown_outcome=False,
            ),
        )
        completed = self._complete_intent(
            intent.intent_id,
            expected_state=IntentState.PENDING,
            result=result,
        )
        if completed is None:
            return self._resolve_claim_race(intent)
        assert completed.result is not None
        return completed.result

    def _dispatch_claimed(self, intent: MessageIntent) -> DispatchResult:
        try:
            request = deserialize_request(intent.serialized_request)
        except Exception:
            raise ServiceDependencyError("stored request could not be restored") from None

        provider_invoked = False
        try:
            provider_result = self._gateway.send(
                provider_key=intent.provider_key,
                request=request,
            )
            provider_invoked = True
            result = self._from_provider_result(intent, provider_result)
        except ProviderNotFoundError:
            result = DispatchResult(
                intent_id=intent.intent_id,
                provider_key=intent.provider_key,
                disposition=SendDisposition.REJECTED,
                correlation_id=intent.correlation_id,
                provider_status="provider_not_registered",
                error=NormalizedError(
                    category=ErrorCategory.UNSUPPORTED,
                    safe_message="Selected messaging provider is unavailable",
                    retriable=False,
                    unknown_outcome=False,
                ),
            )
        except Exception:
            provider_invoked = True
            result = DispatchResult(
                intent_id=intent.intent_id,
                provider_key=intent.provider_key,
                disposition=SendDisposition.UNKNOWN,
                correlation_id=intent.correlation_id,
                error=NormalizedError(
                    category=ErrorCategory.UNKNOWN,
                    safe_message="Dispatch outcome is unknown",
                    retriable=False,
                    unknown_outcome=True,
                ),
            )

        return self._complete_claimed_intent(
            intent,
            result=result,
            provider_invoked=provider_invoked,
        )

    def _complete_claimed_intent(
        self,
        intent: MessageIntent,
        *,
        result: DispatchResult,
        provider_invoked: bool,
    ) -> DispatchResult:
        terminal_state = {
            SendDisposition.ACCEPTED: IntentState.ACCEPTED,
            SendDisposition.REJECTED: IntentState.REJECTED,
            SendDisposition.UNKNOWN: IntentState.UNKNOWN,
        }[result.disposition]
        expected = replace(intent, state=terminal_state, result=result)
        completion_raised = False
        try:
            completed = self._intent_repository.complete(
                intent.intent_id,
                expected_state=IntentState.DISPATCHING,
                result=result,
            )
        except Exception:
            completion_raised = True
            completed = None

        if completion_raised or not isinstance(completed, MessageIntent) or completed != expected:
            if provider_invoked:
                raise PostDispatchPersistenceError(
                    "Provider result could not be persisted"
                ) from None
            raise ServiceDependencyError("intent completion failed")
        assert completed.result is not None
        return completed.result

    @staticmethod
    def _from_provider_result(
        intent: MessageIntent,
        result: SendResult,
    ) -> DispatchResult:
        return DispatchResult(
            intent_id=intent.intent_id,
            provider_key=result.provider_key,
            disposition=result.disposition,
            correlation_id=result.correlation_id,
            provider_message_id=result.provider_message_id,
            provider_status=result.provider_status,
            error=result.error,
        )

    def _resolve_claim_race(self, intent: MessageIntent) -> DispatchResult:
        current = self._get_intent(intent.source_flow, intent.idempotency_key)
        if current is None:
            raise IntentStateError("intent disappeared during dispatch claim")
        if current.request_fingerprint != intent.request_fingerprint:
            raise IdempotencyConflictError("idempotency scope is already used by another request")
        if current.state in (
            IntentState.ACCEPTED,
            IntentState.REJECTED,
            IntentState.UNKNOWN,
        ):
            return self._terminal_replay(current)
        raise IntentInProgressError("dispatch is already in progress")

    @staticmethod
    def _terminal_replay(intent: MessageIntent) -> DispatchResult:
        if intent.result is None:
            raise IntentStateError("terminal intent does not contain a result")
        return replace(intent.result, idempotent_replay=True)

    def _get_intent(self, source_flow: str, idempotency_key: str) -> MessageIntent | None:
        try:
            intent = self._intent_repository.get(source_flow, idempotency_key)
        except Exception:
            raise ServiceDependencyError("intent lookup failed") from None
        if intent is not None and not isinstance(intent, MessageIntent):
            raise ServiceDependencyError("intent repository returned an invalid result")
        return intent

    def _create_intent(self, intent: MessageIntent) -> IntentCreationResult:
        try:
            creation = self._intent_repository.create_if_absent(intent)
        except Exception:
            raise ServiceDependencyError("intent creation failed") from None
        if not isinstance(creation, IntentCreationResult):
            raise ServiceDependencyError("intent repository returned an invalid creation result")
        return creation

    def _claim_intent(self, intent_id: str) -> MessageIntent | None:
        try:
            claimed = self._intent_repository.claim(intent_id)
        except Exception:
            raise ServiceDependencyError("intent claim failed") from None
        if claimed is not None and not isinstance(claimed, MessageIntent):
            raise ServiceDependencyError("intent repository returned an invalid claim result")
        return claimed

    def _complete_intent(
        self,
        intent_id: str,
        *,
        expected_state: IntentState,
        result: DispatchResult,
    ) -> MessageIntent | None:
        try:
            completed = self._intent_repository.complete(
                intent_id,
                expected_state=expected_state,
                result=result,
            )
        except Exception:
            raise ServiceDependencyError("intent completion failed") from None
        if completed is not None and not isinstance(completed, MessageIntent):
            raise ServiceDependencyError("intent repository returned an invalid completion result")
        return completed
