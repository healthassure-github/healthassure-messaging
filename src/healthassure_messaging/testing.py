from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from threading import RLock

from .enums import IntentState, SendDisposition
from .phone import validate_e164_number
from .service_contracts import (
    DispatchResult,
    IntentCreationResult,
    MessageIntent,
    RecipientEligibility,
)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


class InMemoryRecipientEligibilityPolicy:
    """Fixed consent and suppression snapshots with no mutation API."""

    def __init__(
        self,
        *,
        consented_recipients: Iterable[str] = (),
        suppressed_recipients: Iterable[str] = (),
    ) -> None:
        self._consented = self._validated_recipients(consented_recipients)
        self._suppressed = self._validated_recipients(suppressed_recipients)

    @staticmethod
    def _validated_recipients(recipients: Iterable[str]) -> frozenset[str]:
        validated: set[str] = set()
        for recipient in recipients:
            validated.add(validate_e164_number(recipient))
        return frozenset(validated)

    def evaluate(self, recipient: str) -> RecipientEligibility:
        normalized = validate_e164_number(recipient)
        return RecipientEligibility(
            consented=normalized in self._consented,
            suppressed=normalized in self._suppressed,
        )


class InMemoryTextSessionPolicy:
    """Fixed active-session snapshot keyed by normalized recipient and purpose."""

    def __init__(self, active_sessions: Iterable[tuple[str, str]] = ()) -> None:
        validated: set[tuple[str, str]] = set()
        for recipient, purpose_key in active_sessions:
            _require_text(purpose_key, "purpose_key")
            validated.add((validate_e164_number(recipient), purpose_key))
        self._active_sessions = frozenset(validated)

    def has_active_session(self, recipient: str, purpose_key: str) -> bool:
        normalized = validate_e164_number(recipient)
        _require_text(purpose_key, "purpose_key")
        return (normalized, purpose_key) in self._active_sessions


class InMemoryIntentRepository:
    """Thread-safe deterministic intent storage for tests and prototypes."""

    def __init__(self, *, event_sink: list[str] | None = None) -> None:
        self._by_scope: dict[tuple[str, str], MessageIntent] = {}
        self._scope_by_id: dict[str, tuple[str, str]] = {}
        self._events: list[str] = []
        self._event_sink = event_sink
        self._lock = RLock()

    @property
    def events(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._events)

    def _record(self, event: str) -> None:
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink.append(event)

    def get(self, source_flow: str, idempotency_key: str) -> MessageIntent | None:
        _require_text(source_flow, "source_flow")
        _require_text(idempotency_key, "idempotency_key")
        with self._lock:
            return self._by_scope.get((source_flow, idempotency_key))

    def create_if_absent(self, intent: MessageIntent) -> IntentCreationResult:
        if not isinstance(intent, MessageIntent):
            raise TypeError("intent must be a MessageIntent")
        scope = (intent.source_flow, intent.idempotency_key)
        with self._lock:
            existing = self._by_scope.get(scope)
            if existing is not None:
                return IntentCreationResult(intent=existing, created=False)
            if intent.intent_id in self._scope_by_id:
                raise ValueError("intent_id is already present")
            self._by_scope[scope] = intent
            self._scope_by_id[intent.intent_id] = scope
            self._record("create")
            return IntentCreationResult(intent=intent, created=True)

    def claim(self, intent_id: str) -> MessageIntent | None:
        _require_text(intent_id, "intent_id")
        with self._lock:
            scope = self._scope_by_id.get(intent_id)
            if scope is None:
                return None
            intent = self._by_scope[scope]
            if intent.state is not IntentState.PENDING:
                return None
            claimed = replace(intent, state=IntentState.DISPATCHING)
            self._by_scope[scope] = claimed
            self._record("claim")
            return claimed

    def complete(
        self,
        intent_id: str,
        *,
        expected_state: IntentState,
        result: DispatchResult,
    ) -> MessageIntent | None:
        _require_text(intent_id, "intent_id")
        if not isinstance(expected_state, IntentState):
            raise TypeError("expected_state must be an IntentState")
        if not isinstance(result, DispatchResult):
            raise TypeError("result must be a DispatchResult")
        terminal_state = {
            SendDisposition.ACCEPTED: IntentState.ACCEPTED,
            SendDisposition.REJECTED: IntentState.REJECTED,
            SendDisposition.UNKNOWN: IntentState.UNKNOWN,
        }[result.disposition]
        with self._lock:
            scope = self._scope_by_id.get(intent_id)
            if scope is None:
                return None
            intent = self._by_scope[scope]
            if intent.state is not expected_state:
                return None
            completed = replace(intent, state=terminal_state, result=result)
            self._by_scope[scope] = completed
            self._record("complete")
            return completed
