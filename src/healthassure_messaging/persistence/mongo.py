from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, TypeVar, cast

try:
    from pymongo import ASCENDING, ReturnDocument
    from pymongo.collection import Collection
    from pymongo.database import Database
    from pymongo.errors import DuplicateKeyError, PyMongoError
except ModuleNotFoundError:
    _pymongo_available = False
else:
    _pymongo_available = True

if not _pymongo_available:
    raise ImportError(
        "Mongo persistence requires the healthassure-messaging[mongo] extra"
    ) from None

from ..contracts import NormalizedError
from ..enums import ErrorCategory, IntentState, SendDisposition, TemplateComponentType
from ..phone import validate_e164_number
from ..service_contracts import (
    DispatchResult,
    IntentCreationResult,
    MessageIntent,
    RecipientEligibility,
    TemplateAlias,
    TemplateComponentSpec,
)

MONGO_RECORD_SCHEMA_VERSION: Final[int] = 1
_MAX_RECOVERY_LIMIT: Final[int] = 1_000
_COLLECTION_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]+")
_Document = dict[str, Any]
_MongoResult = TypeVar("_MongoResult")
_RecordResult = TypeVar("_RecordResult")


class MongoPersistenceError(RuntimeError):
    """Base class for sanitized Mongo persistence failures."""


class MongoPersistenceConflictError(MongoPersistenceError):
    """Raised when an optimistic or idempotent Mongo operation conflicts."""


class MongoRecordError(MongoPersistenceError):
    """Raised when a stored Mongo record violates the package schema."""


def _mongo_call(message: str, operation: Callable[[], _MongoResult]) -> _MongoResult:
    try:
        result = operation()
    except PyMongoError:
        failed = True
    else:
        failed = False
    if failed:
        raise MongoPersistenceError(message) from None
    return result


@dataclass(frozen=True, slots=True)
class MongoIndexDefinition:
    collection_name: str
    name: str
    keys: tuple[tuple[str, int], ...]
    unique: bool = False


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_epoch(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_revision(value: object, field_name: str = "expected_revision") -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _record_error() -> MongoRecordError:
    return MongoRecordError("Mongo record does not match the supported schema")


def _record_call(operation: Callable[[], _RecordResult]) -> _RecordResult:
    try:
        result = operation()
    except (TypeError, ValueError):
        failed = True
    else:
        failed = False
    if failed:
        raise _record_error()
    return result


def _as_document(value: object) -> _Document:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _record_error()
    return dict(cast(Mapping[str, Any], value))


def _doc_text(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _record_error()
    return value


def _doc_optional_text(document: Mapping[str, Any], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _record_error()
    return value


def _doc_int(document: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = document.get(key)
    if type(value) is not int or value < minimum:
        raise _record_error()
    return value


def _doc_optional_int(document: Mapping[str, Any], key: str) -> int | None:
    value = document.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise _record_error()
    return value


def _doc_bool(document: Mapping[str, Any], key: str) -> bool:
    value = document.get(key)
    if type(value) is not bool:
        raise _record_error()
    return value


def _validate_schema(document: Mapping[str, Any]) -> None:
    if document.get("record_schema_version") != MONGO_RECORD_SCHEMA_VERSION:
        raise _record_error()


def _error_to_document(error: NormalizedError | None) -> _Document | None:
    if error is None:
        return None
    return {
        "category": error.category.value,
        "safe_message": error.safe_message,
        "retriable": error.retriable,
        "unknown_outcome": error.unknown_outcome,
        "provider_code": error.provider_code,
        "http_status": error.http_status,
        "provider_subcode": error.provider_subcode,
        "retry_after_seconds": error.retry_after_seconds,
    }


def _error_from_document(value: object) -> NormalizedError | None:
    if value is None:
        return None
    document = _as_document(value)
    def build_error() -> NormalizedError:
        return NormalizedError(
            category=ErrorCategory(_doc_text(document, "category")),
            safe_message=_doc_text(document, "safe_message"),
            retriable=_doc_bool(document, "retriable"),
            unknown_outcome=_doc_bool(document, "unknown_outcome"),
            provider_code=_doc_optional_text(document, "provider_code"),
            http_status=_doc_optional_int(document, "http_status"),
            provider_subcode=_doc_optional_int(document, "provider_subcode"),
            retry_after_seconds=_doc_optional_int(document, "retry_after_seconds"),
        )

    return _record_call(build_error)


def _result_to_document(result: DispatchResult) -> _Document:
    return {
        "intent_id": result.intent_id,
        "provider_key": result.provider_key,
        "disposition": result.disposition.value,
        "correlation_id": result.correlation_id,
        "provider_message_id": result.provider_message_id,
        "provider_status": result.provider_status,
        "error": _error_to_document(result.error),
        "idempotent_replay": result.idempotent_replay,
    }


def _result_from_document(value: object) -> DispatchResult:
    document = _as_document(value)
    def build_result() -> DispatchResult:
        return DispatchResult(
            intent_id=_doc_text(document, "intent_id"),
            provider_key=_doc_text(document, "provider_key"),
            disposition=SendDisposition(_doc_text(document, "disposition")),
            correlation_id=_doc_text(document, "correlation_id"),
            provider_message_id=_doc_optional_text(document, "provider_message_id"),
            provider_status=_doc_optional_text(document, "provider_status"),
            error=_error_from_document(document.get("error")),
            idempotent_replay=_doc_bool(document, "idempotent_replay"),
        )

    return _record_call(build_result)


def _attempt_result_fields(result: DispatchResult) -> _Document:
    return {
        "state": result.disposition.value,
        "disposition": result.disposition.value,
        "provider_message_id": result.provider_message_id,
        "provider_status": result.provider_status,
        "error": _error_to_document(result.error),
    }


def _validate_attempts(document: Mapping[str, Any]) -> None:
    attempts = document.get("attempts")
    if not isinstance(attempts, list):
        raise _record_error()
    attempt_count = _doc_int(document, "attempt_count")
    if len(attempts) != attempt_count:
        raise _record_error()
    for index, raw_attempt in enumerate(attempts, start=1):
        attempt = _as_document(raw_attempt)
        if _doc_int(attempt, "attempt_number", minimum=1) != index:
            raise _record_error()
        _doc_int(attempt, "started_at_epoch")
        _doc_optional_int(attempt, "completed_at_epoch")
        _doc_text(attempt, "state")
        disposition = _doc_optional_text(attempt, "disposition")
        if disposition is not None:
            _record_call(partial(SendDisposition, disposition))
        _doc_optional_text(attempt, "provider_message_id")
        _doc_optional_text(attempt, "provider_status")
        _error_from_document(attempt.get("error"))


def _intent_from_document(value: object) -> MessageIntent:
    document = _as_document(value)
    _validate_schema(document)
    _validate_attempts(document)
    def build_intent() -> MessageIntent:
        state = IntentState(_doc_text(document, "state"))
        result_value = document.get("result")
        result = None if result_value is None else _result_from_document(result_value)
        return MessageIntent(
            intent_id=_doc_text(document, "intent_id"),
            source_flow=_doc_text(document, "source_flow"),
            idempotency_key=_doc_text(document, "idempotency_key"),
            actor_id=_doc_text(document, "actor_id"),
            provider_key=_doc_text(document, "provider_key"),
            correlation_id=_doc_text(document, "correlation_id"),
            request_fingerprint=_doc_text(document, "request_fingerprint"),
            serialized_request=_doc_text(document, "serialized_request"),
            state=state,
            result=result,
        )

    return _record_call(build_intent)


def _intent_to_document(intent: MessageIntent, now: int) -> _Document:
    if intent.state is not IntentState.PENDING or intent.result is not None:
        raise ValueError("new Mongo intents must be pending")
    return {
        "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
        "intent_id": intent.intent_id,
        "source_flow": intent.source_flow,
        "idempotency_key": intent.idempotency_key,
        "actor_id": intent.actor_id,
        "provider_key": intent.provider_key,
        "correlation_id": intent.correlation_id,
        "request_fingerprint": intent.request_fingerprint,
        "serialized_request": intent.serialized_request,
        "state": IntentState.PENDING.value,
        "result": None,
        "created_at_epoch": now,
        "updated_at_epoch": now,
        "dispatch_started_at_epoch": None,
        "completed_at_epoch": None,
        "attempt_count": 0,
        "attempts": [],
    }


class MongoIntentRepository:
    def __init__(self, collection: Collection[_Document], *, clock: Callable[[], int]) -> None:
        self._collection = collection
        self._clock = clock

    def _now(self) -> int:
        return _require_epoch(self._clock(), "clock result")

    def get(self, source_flow: str, idempotency_key: str) -> MessageIntent | None:
        _require_text(source_flow, "source_flow")
        _require_text(idempotency_key, "idempotency_key")
        document = _mongo_call(
            "Mongo intent lookup failed",
            lambda: self._collection.find_one(
                {"source_flow": source_flow, "idempotency_key": idempotency_key}
            ),
        )
        return None if document is None else _intent_from_document(document)

    def create_if_absent(self, intent: MessageIntent) -> IntentCreationResult:
        if not isinstance(intent, MessageIntent):
            raise TypeError("intent must be a MessageIntent")
        scope = {"source_flow": intent.source_flow, "idempotency_key": intent.idempotency_key}
        document = _intent_to_document(intent, self._now())
        duplicate = False
        mongo_failed = False
        try:
            outcome = self._collection.update_one(
                scope,
                {"$setOnInsert": document},
                upsert=True,
            )
            created = outcome.upserted_id is not None
            stored = self._collection.find_one(scope)
        except DuplicateKeyError:
            duplicate = True
            stored = None
            created = False
        except PyMongoError:
            mongo_failed = True
            stored = None
            created = False
        if mongo_failed:
            raise MongoPersistenceError("Mongo intent creation failed") from None
        if duplicate:
            stored = _mongo_call(
                "Mongo intent creation failed",
                lambda: self._collection.find_one(scope),
            )
            if stored is None:
                raise MongoPersistenceConflictError(
                    "Mongo intent identity conflicts with another record"
                ) from None
            created = False
        if stored is None:
            raise MongoPersistenceError("Mongo intent creation could not be confirmed")
        return IntentCreationResult(intent=_intent_from_document(stored), created=created)

    def claim(self, intent_id: str) -> MessageIntent | None:
        _require_text(intent_id, "intent_id")
        current = _mongo_call(
            "Mongo intent claim failed",
            lambda: self._collection.find_one({"intent_id": intent_id}),
        )
        if current is None:
            return None
        intent = _intent_from_document(current)
        if intent.state is not IntentState.PENDING:
            return None
        attempt_count = _doc_int(current, "attempt_count")
        now = self._now()
        attempt = {
            "attempt_number": attempt_count + 1,
            "started_at_epoch": now,
            "completed_at_epoch": None,
            "state": IntentState.DISPATCHING.value,
            "disposition": None,
            "provider_message_id": None,
            "provider_status": None,
            "error": None,
        }
        claimed = _mongo_call(
            "Mongo intent claim failed",
            lambda: self._collection.find_one_and_update(
                {
                    "intent_id": intent_id,
                    "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                    "state": IntentState.PENDING.value,
                    "attempt_count": attempt_count,
                },
                {
                    "$set": {
                        "state": IntentState.DISPATCHING.value,
                        "updated_at_epoch": now,
                        "dispatch_started_at_epoch": now,
                    },
                    "$inc": {"attempt_count": 1},
                    "$push": {"attempts": attempt},
                },
                return_document=ReturnDocument.AFTER,
            ),
        )
        return None if claimed is None else _intent_from_document(claimed)

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
        current = _mongo_call(
            "Mongo intent completion failed",
            lambda: self._collection.find_one(
                {"intent_id": intent_id, "state": expected_state.value}
            ),
        )
        if current is None:
            return None
        stored_intent = _intent_from_document(current)
        if (
            stored_intent.intent_id != intent_id
            or result.intent_id != stored_intent.intent_id
            or result.provider_key != stored_intent.provider_key
            or result.correlation_id != stored_intent.correlation_id
        ):
            raise MongoPersistenceConflictError(
                "Mongo intent completion identity conflicts with the stored intent"
            )
        now = self._now()
        query: _Document = {
            "intent_id": intent_id,
            "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
            "state": expected_state.value,
        }
        updates: _Document = {
            "state": terminal_state.value,
            "result": _result_to_document(result),
            "updated_at_epoch": now,
            "completed_at_epoch": now,
        }
        if expected_state is IntentState.DISPATCHING:
            attempt_count = _doc_int(current, "attempt_count", minimum=1)
            attempts = current.get("attempts")
            if not isinstance(attempts, list) or len(attempts) != attempt_count:
                raise _record_error()
            attempt_index = attempt_count - 1
            active_attempt = _as_document(attempts[attempt_index])
            if active_attempt.get("completed_at_epoch") is not None:
                raise _record_error()
            query.update(
                {
                    "attempt_count": attempt_count,
                    f"attempts.{attempt_index}.completed_at_epoch": None,
                }
            )
            for key, value in _attempt_result_fields(result).items():
                updates[f"attempts.{attempt_index}.{key}"] = value
            updates[f"attempts.{attempt_index}.completed_at_epoch"] = now
        elif expected_state is IntentState.PENDING:
            if _doc_int(current, "attempt_count") != 0 or current.get("attempts") != []:
                raise _record_error()
        else:
            return None
        completed = _mongo_call(
            "Mongo intent completion failed",
            lambda: self._collection.find_one_and_update(
                query,
                {"$set": updates},
                return_document=ReturnDocument.AFTER,
            ),
        )
        return None if completed is None else _intent_from_document(completed)

    def recover_stale_dispatching(
        self,
        *,
        stale_before_epoch: int,
        limit: int = 100,
    ) -> int:
        _require_epoch(stale_before_epoch, "stale_before_epoch")
        if type(limit) is not int or not 1 <= limit <= _MAX_RECOVERY_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_RECOVERY_LIMIT}")
        def find_candidates() -> list[_Document]:
            cursor = (
                self._collection.find(
                    {
                        "state": IntentState.DISPATCHING.value,
                        "dispatch_started_at_epoch": {"$lt": stale_before_epoch},
                    }
                )
                .sort([("dispatch_started_at_epoch", ASCENDING), ("intent_id", ASCENDING)])
                .limit(limit)
            )
            return list(cursor)

        candidates = _mongo_call("Mongo stale-intent recovery failed", find_candidates)
        recovered = 0
        for candidate in candidates:
            intent = _intent_from_document(candidate)
            result = DispatchResult(
                intent_id=intent.intent_id,
                provider_key=intent.provider_key,
                disposition=SendDisposition.UNKNOWN,
                correlation_id=intent.correlation_id,
                provider_status="stale_dispatch_recovered",
                error=NormalizedError(
                    category=ErrorCategory.UNKNOWN,
                    safe_message="Dispatch outcome is unknown after stale recovery",
                    retriable=False,
                    unknown_outcome=True,
                ),
            )
            if self.complete(
                intent.intent_id,
                expected_state=IntentState.DISPATCHING,
                result=result,
            ) is not None:
                recovered += 1
        return recovered


def _policy_event(
    *,
    event_id: str,
    operation: str,
    actor_id: str,
    source: str,
    evidence_reference: str,
    occurred_at_epoch: int,
) -> _Document:
    return {
        "event_id": _require_text(event_id, "event_id"),
        "operation": operation,
        "actor_id": _require_text(actor_id, "actor_id"),
        "source": _require_text(source, "source"),
        "evidence_reference": _require_text(evidence_reference, "evidence_reference"),
        "occurred_at_epoch": _require_epoch(occurred_at_epoch, "occurred_at_epoch"),
    }


_POLICY_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "grant_consent",
        "revoke_consent",
        "apply_suppression",
        "clear_suppression",
    }
)


def _policy_record_from_document(
    value: object,
    *,
    expected_recipient: str,
) -> tuple[RecipientEligibility, tuple[_Document, ...]]:
    document = _as_document(value)
    _validate_schema(document)

    def build_record() -> tuple[RecipientEligibility, tuple[_Document, ...]]:
        stored_recipient = validate_e164_number(_doc_text(document, "recipient"))
        if stored_recipient != expected_recipient:
            raise ValueError("stored recipient does not match the requested scope")
        consented = _doc_bool(document, "consented")
        suppressed = _doc_bool(document, "suppressed")
        raw_events = document.get("events")
        if not isinstance(raw_events, list):
            raise ValueError("events must be a list")

        resolved_events: list[_Document] = []
        event_ids: set[str] = set()
        replayed_consented = False
        replayed_suppressed = False
        for raw_event in raw_events:
            event = _as_document(raw_event)
            event_id = _doc_text(event, "event_id")
            if event_id in event_ids:
                raise ValueError("event IDs must be unique")
            event_ids.add(event_id)
            operation = _doc_text(event, "operation")
            if operation not in _POLICY_OPERATIONS:
                raise ValueError("policy operation is unsupported")
            _doc_text(event, "actor_id")
            _doc_text(event, "source")
            _doc_text(event, "evidence_reference")
            _doc_int(event, "occurred_at_epoch")
            if operation == "grant_consent":
                replayed_consented = True
            elif operation == "revoke_consent":
                replayed_consented = False
            elif operation == "apply_suppression":
                replayed_suppressed = True
            else:
                replayed_suppressed = False
            resolved_events.append(event)

        if consented != replayed_consented or suppressed != replayed_suppressed:
            raise ValueError("stored policy state does not match its event history")
        return RecipientEligibility(consented, suppressed), tuple(resolved_events)

    return _record_call(build_record)


class MongoRecipientEligibilityPolicy:
    def __init__(self, collection: Collection[_Document]) -> None:
        self._collection = collection

    def evaluate(self, recipient: str) -> RecipientEligibility:
        normalized = validate_e164_number(recipient)
        document = _mongo_call(
            "Mongo recipient policy lookup failed",
            lambda: self._collection.find_one({"recipient": normalized}),
        )
        if document is None:
            return RecipientEligibility(consented=False, suppressed=False)
        eligibility, _ = _policy_record_from_document(
            document,
            expected_recipient=normalized,
        )
        return eligibility

    def grant_consent(
        self,
        *,
        event_id: str,
        recipient: str,
        actor_id: str,
        source: str,
        evidence_reference: str,
        occurred_at_epoch: int,
    ) -> RecipientEligibility:
        return self._apply(
            "grant_consent",
            consented=True,
            event_fields={
                "event_id": event_id,
                "recipient": recipient,
                "actor_id": actor_id,
                "source": source,
                "evidence_reference": evidence_reference,
                "occurred_at_epoch": occurred_at_epoch,
            },
        )

    def revoke_consent(
        self,
        *,
        event_id: str,
        recipient: str,
        actor_id: str,
        source: str,
        evidence_reference: str,
        occurred_at_epoch: int,
    ) -> RecipientEligibility:
        return self._apply(
            "revoke_consent",
            consented=False,
            event_fields={
                "event_id": event_id,
                "recipient": recipient,
                "actor_id": actor_id,
                "source": source,
                "evidence_reference": evidence_reference,
                "occurred_at_epoch": occurred_at_epoch,
            },
        )

    def apply_suppression(
        self,
        *,
        event_id: str,
        recipient: str,
        actor_id: str,
        source: str,
        evidence_reference: str,
        occurred_at_epoch: int,
    ) -> RecipientEligibility:
        return self._apply(
            "apply_suppression",
            suppressed=True,
            event_fields={
                "event_id": event_id,
                "recipient": recipient,
                "actor_id": actor_id,
                "source": source,
                "evidence_reference": evidence_reference,
                "occurred_at_epoch": occurred_at_epoch,
            },
        )

    def clear_suppression(
        self,
        *,
        event_id: str,
        recipient: str,
        actor_id: str,
        source: str,
        evidence_reference: str,
        occurred_at_epoch: int,
    ) -> RecipientEligibility:
        return self._apply(
            "clear_suppression",
            suppressed=False,
            event_fields={
                "event_id": event_id,
                "recipient": recipient,
                "actor_id": actor_id,
                "source": source,
                "evidence_reference": evidence_reference,
                "occurred_at_epoch": occurred_at_epoch,
            },
        )

    def _apply(
        self,
        operation: str,
        *,
        event_fields: Mapping[str, object],
        consented: bool | None = None,
        suppressed: bool | None = None,
    ) -> RecipientEligibility:
        recipient_value = event_fields.get("recipient")
        if not isinstance(recipient_value, str):
            raise TypeError("recipient must be a string")
        recipient = validate_e164_number(recipient_value)
        event = _policy_event(
            event_id=cast(str, event_fields.get("event_id")),
            operation=operation,
            actor_id=cast(str, event_fields.get("actor_id")),
            source=cast(str, event_fields.get("source")),
            evidence_reference=cast(str, event_fields.get("evidence_reference")),
            occurred_at_epoch=cast(int, event_fields.get("occurred_at_epoch")),
        )
        initial = {
            "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
            "recipient": recipient,
            "consented": False,
            "suppressed": False,
            "events": [],
        }
        def ensure_and_read() -> _Document | None:
            self._collection.update_one(
                {"recipient": recipient},
                {"$setOnInsert": initial},
                upsert=True,
            )
            return self._collection.find_one({"recipient": recipient})

        current = _mongo_call("Mongo recipient policy update failed", ensure_and_read)
        if current is None:
            raise MongoPersistenceError("Mongo recipient policy update could not be confirmed")
        _, events = _policy_record_from_document(current, expected_recipient=recipient)
        for existing in events:
            if existing.get("event_id") == event["event_id"]:
                if existing != event:
                    raise MongoPersistenceConflictError(
                        "Mongo recipient policy event conflicts with existing evidence"
                    )
                return self.evaluate(recipient)
        state_updates: _Document = {}
        if consented is not None:
            state_updates["consented"] = consented
        if suppressed is not None:
            state_updates["suppressed"] = suppressed
        updated = _mongo_call(
            "Mongo recipient policy update failed",
            lambda: self._collection.find_one_and_update(
                {
                    "recipient": recipient,
                    "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                    "events.event_id": {"$ne": event["event_id"]},
                },
                {"$set": state_updates, "$push": {"events": event}},
                return_document=ReturnDocument.AFTER,
            ),
        )
        if updated is None:
            raced = _mongo_call(
                "Mongo recipient policy update failed",
                lambda: self._collection.find_one({"recipient": recipient}),
            )
            if raced is None:
                raise MongoPersistenceConflictError("Mongo recipient policy update conflicted")
            _, raced_events = _policy_record_from_document(
                raced,
                expected_recipient=recipient,
            )
            if event in raced_events:
                return self.evaluate(recipient)
            raise MongoPersistenceConflictError("Mongo recipient policy update conflicted")
        return self.evaluate(recipient)


def _purpose_keys(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("allowed_purpose_keys must be an iterable of strings")
    resolved: list[str] = []
    seen: set[str] = set()
    for value in values:
        purpose = _require_text(value, "purpose key")
        if purpose in seen:
            raise ValueError("allowed_purpose_keys must not contain duplicates")
        seen.add(purpose)
        resolved.append(purpose)
    if not resolved:
        raise ValueError("allowed_purpose_keys must not be empty")
    return tuple(resolved)


def _stored_purpose_keys(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _record_error()
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_purpose in value:
        purpose = _doc_text({"purpose": raw_purpose}, "purpose")
        if purpose in seen:
            raise _record_error()
        seen.add(purpose)
        resolved.append(purpose)
    if not allow_empty and not resolved:
        raise _record_error()
    return tuple(resolved)


def _session_record_from_document(
    value: object,
    *,
    expected_provider_key: str,
    expected_phone_number_id: str,
    expected_recipient: str,
) -> tuple[tuple[str, ...], int, int, tuple[_Document, ...]]:
    document = _as_document(value)
    _validate_schema(document)

    def build_record() -> tuple[tuple[str, ...], int, int, tuple[_Document, ...]]:
        if _doc_text(document, "provider_key") != expected_provider_key:
            raise ValueError("stored provider does not match the configured scope")
        if _doc_text(document, "phone_number_id") != expected_phone_number_id:
            raise ValueError("stored phone number does not match the configured scope")
        stored_recipient = validate_e164_number(_doc_text(document, "recipient"))
        if stored_recipient != expected_recipient:
            raise ValueError("stored recipient does not match the requested scope")

        purposes = _stored_purpose_keys(
            document.get("allowed_purpose_keys"),
            allow_empty=True,
        )
        opened_at = _doc_int(document, "opened_at_epoch")
        expires_at = _doc_int(document, "expires_at_epoch")
        raw_events = document.get("events")
        if not isinstance(raw_events, list):
            raise ValueError("events must be a list")

        resolved_events: list[_Document] = []
        event_ids: set[str] = set()
        final_purposes: tuple[str, ...] = ()
        final_opened_at = 0
        final_expires_at = 0
        for raw_event in raw_events:
            event = _as_document(raw_event)
            event_id = _doc_text(event, "event_id")
            if event_id in event_ids:
                raise ValueError("event IDs must be unique")
            event_ids.add(event_id)
            event_purposes = _stored_purpose_keys(
                event.get("allowed_purpose_keys"),
                allow_empty=False,
            )
            event_opened_at = _doc_int(event, "opened_at_epoch")
            event_expires_at = _doc_int(event, "expires_at_epoch")
            if event_expires_at <= event_opened_at:
                raise ValueError("session expiry must follow its opening time")
            final_purposes = event_purposes
            final_opened_at = event_opened_at
            final_expires_at = event_expires_at
            resolved_events.append(event)

        if not resolved_events:
            if purposes or opened_at != 0 or expires_at != 0:
                raise ValueError("empty session history must represent the inactive state")
        elif (
            purposes != final_purposes
            or opened_at != final_opened_at
            or expires_at != final_expires_at
        ):
            raise ValueError("stored session state does not match its final event")
        return purposes, opened_at, expires_at, tuple(resolved_events)

    return _record_call(build_record)


class MongoTextSessionPolicy:
    def __init__(
        self,
        collection: Collection[_Document],
        *,
        provider_key: str,
        phone_number_id: str,
        clock: Callable[[], int],
    ) -> None:
        self._collection = collection
        self._provider_key = provider_key
        self._phone_number_id = phone_number_id
        self._clock = clock

    def has_active_session(self, recipient: str, purpose_key: str) -> bool:
        normalized = validate_e164_number(recipient)
        purpose = _require_text(purpose_key, "purpose_key")
        document = _mongo_call(
            "Mongo session lookup failed",
            lambda: self._collection.find_one(
                {
                    "provider_key": self._provider_key,
                    "phone_number_id": self._phone_number_id,
                    "recipient": normalized,
                }
            ),
        )
        if document is None:
            return False
        purposes, _, expires_at, _ = _session_record_from_document(
            document,
            expected_provider_key=self._provider_key,
            expected_phone_number_id=self._phone_number_id,
            expected_recipient=normalized,
        )
        now = _require_epoch(self._clock(), "clock result")
        return purpose in purposes and expires_at > now

    def open_session(
        self,
        *,
        event_id: str,
        recipient: str,
        allowed_purpose_keys: Iterable[str],
        opened_at_epoch: int,
        expires_at_epoch: int,
    ) -> None:
        normalized = validate_e164_number(recipient)
        purposes = _purpose_keys(allowed_purpose_keys)
        opened_at = _require_epoch(opened_at_epoch, "opened_at_epoch")
        expires_at = _require_epoch(expires_at_epoch, "expires_at_epoch")
        if expires_at <= opened_at:
            raise ValueError("expires_at_epoch must be later than opened_at_epoch")
        event = {
            "event_id": _require_text(event_id, "event_id"),
            "allowed_purpose_keys": list(purposes),
            "opened_at_epoch": opened_at,
            "expires_at_epoch": expires_at,
        }
        scope = {
            "provider_key": self._provider_key,
            "phone_number_id": self._phone_number_id,
            "recipient": normalized,
        }
        initial = {
            "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
            **scope,
            "allowed_purpose_keys": [],
            "opened_at_epoch": 0,
            "expires_at_epoch": 0,
            "events": [],
        }
        def ensure_and_read() -> _Document | None:
            self._collection.update_one(scope, {"$setOnInsert": initial}, upsert=True)
            return self._collection.find_one(scope)

        current = _mongo_call("Mongo session update failed", ensure_and_read)
        if current is None:
            raise MongoPersistenceError("Mongo session update could not be confirmed")
        _, _, _, events = _session_record_from_document(
            current,
            expected_provider_key=self._provider_key,
            expected_phone_number_id=self._phone_number_id,
            expected_recipient=normalized,
        )
        for existing in events:
            if existing.get("event_id") == event["event_id"]:
                if existing != event:
                    raise MongoPersistenceConflictError(
                        "Mongo session event conflicts with existing evidence"
                    )
                return
        updated = _mongo_call(
            "Mongo session update failed",
            lambda: self._collection.find_one_and_update(
                {
                    **scope,
                    "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                    "events.event_id": {"$ne": event["event_id"]},
                },
                {
                    "$set": {
                        "allowed_purpose_keys": list(purposes),
                        "opened_at_epoch": opened_at,
                        "expires_at_epoch": expires_at,
                    },
                    "$push": {"events": event},
                },
                return_document=ReturnDocument.AFTER,
            ),
        )
        if updated is None:
            raced = _mongo_call(
                "Mongo session update failed",
                lambda: self._collection.find_one(scope),
            )
            if raced is None:
                raise MongoPersistenceConflictError("Mongo session update conflicted")
            _, _, _, raced_events = _session_record_from_document(
                raced,
                expected_provider_key=self._provider_key,
                expected_phone_number_id=self._phone_number_id,
                expected_recipient=normalized,
            )
            if event in raced_events:
                return
            raise MongoPersistenceConflictError("Mongo session update conflicted")


def _alias_fields(alias: TemplateAlias) -> _Document:
    return {
        "template_key": alias.key,
        "provider_key": alias.provider_key,
        "template_name": alias.template_name,
        "language_code": alias.language_code,
        "components": [
            {
                "component_type": component.component_type.value,
                "parameter_names": list(component.parameter_names),
            }
            for component in alias.components
        ],
    }


def _alias_from_document(value: object) -> TemplateAlias:
    document = _as_document(value)
    _validate_schema(document)
    components_value = document.get("components")
    if not isinstance(components_value, list):
        raise _record_error()
    def build_alias() -> TemplateAlias:
        _doc_bool(document, "active")
        _doc_int(document, "revision", minimum=1)
        _doc_text(document, "actor_id")
        _doc_int(document, "updated_at_epoch")
        components: list[TemplateComponentSpec] = []
        for raw_component in components_value:
            component = _as_document(raw_component)
            names = component.get("parameter_names")
            if not isinstance(names, list) or not all(
                isinstance(name, str) and name.strip() for name in names
            ):
                raise _record_error()
            components.append(
                TemplateComponentSpec(
                    component_type=TemplateComponentType(
                        _doc_text(component, "component_type")
                    ),
                    parameter_names=tuple(cast(list[str], names)),
                )
            )
        return TemplateAlias(
            key=_doc_text(document, "template_key"),
            provider_key=_doc_text(document, "provider_key"),
            template_name=_doc_text(document, "template_name"),
            language_code=_doc_text(document, "language_code"),
            components=tuple(components),
        )

    return _record_call(build_alias)


class MongoTemplateCatalog:
    def __init__(self, collection: Collection[_Document]) -> None:
        self._collection = collection

    def get(self, template_key: str) -> TemplateAlias | None:
        key = _require_text(template_key, "template_key")
        document = _mongo_call(
            "Mongo template lookup failed",
            lambda: self._collection.find_one({"template_key": key, "active": True}),
        )
        return None if document is None else _alias_from_document(document)

    def save(
        self,
        alias: TemplateAlias,
        *,
        expected_revision: int | None,
        actor_id: str,
        updated_at_epoch: int,
    ) -> int:
        if not isinstance(alias, TemplateAlias):
            raise TypeError("alias must be a TemplateAlias")
        actor = _require_text(actor_id, "actor_id")
        updated_at = _require_epoch(updated_at_epoch, "updated_at_epoch")
        fields = _alias_fields(alias)
        if expected_revision is None:
            document = {
                "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                **fields,
                "active": True,
                "revision": 1,
                "actor_id": actor,
                "updated_at_epoch": updated_at,
            }
            duplicate = False
            mongo_failed = False
            try:
                self._collection.insert_one(document)
            except DuplicateKeyError:
                duplicate = True
            except PyMongoError:
                mongo_failed = True
            if mongo_failed:
                raise MongoPersistenceError("Mongo template update failed") from None
            if duplicate:
                raise MongoPersistenceConflictError(
                    "Mongo template revision conflicts with the stored alias"
                ) from None
            return 1
        revision = _require_revision(expected_revision)
        new_revision = revision + 1
        updated = _mongo_call(
            "Mongo template update failed",
            lambda: self._collection.find_one_and_update(
                {
                    "template_key": alias.key,
                    "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                    "revision": revision,
                },
                {
                    "$set": {
                        **fields,
                        "active": True,
                        "revision": new_revision,
                        "actor_id": actor,
                        "updated_at_epoch": updated_at,
                    }
                },
                return_document=ReturnDocument.AFTER,
            ),
        )
        if updated is None:
            raise MongoPersistenceConflictError(
                "Mongo template revision conflicts with the stored alias"
            )
        _alias_from_document(updated)
        return new_revision

    def deactivate(
        self,
        template_key: str,
        *,
        expected_revision: int,
        actor_id: str,
        updated_at_epoch: int,
    ) -> int:
        key = _require_text(template_key, "template_key")
        revision = _require_revision(expected_revision)
        actor = _require_text(actor_id, "actor_id")
        updated_at = _require_epoch(updated_at_epoch, "updated_at_epoch")
        new_revision = revision + 1
        updated = _mongo_call(
            "Mongo template deactivation failed",
            lambda: self._collection.find_one_and_update(
                {
                    "template_key": key,
                    "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                    "revision": revision,
                    "active": True,
                },
                {
                    "$set": {
                        "active": False,
                        "revision": new_revision,
                        "actor_id": actor,
                        "updated_at_epoch": updated_at,
                    }
                },
                return_document=ReturnDocument.AFTER,
            ),
        )
        if updated is None:
            raise MongoPersistenceConflictError(
                "Mongo template revision conflicts with the stored alias"
            )
        _alias_from_document(updated)
        return new_revision


class MongoMessagingPersistence:
    """Mongo-backed package ports over a caller-owned database handle."""

    def __init__(
        self,
        *,
        database: Database[_Document],
        provider_key: str,
        phone_number_id: str,
        collection_prefix: str = "messaging",
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        provider = _require_text(provider_key, "provider_key")
        phone = _require_text(phone_number_id, "phone_number_id")
        prefix = _require_text(collection_prefix, "collection_prefix")
        if _COLLECTION_PREFIX_PATTERN.fullmatch(prefix) is None:
            raise ValueError("collection_prefix contains unsupported characters")
        if not callable(clock):
            raise TypeError("clock must be callable")
        getter = getattr(database, "get_collection", None)
        if not callable(getter):
            raise TypeError("database must provide get_collection")
        self._collection_names = {
            "intents": f"{prefix}_intents",
            "recipient_policy": f"{prefix}_recipient_policies",
            "sessions": f"{prefix}_sessions",
            "templates": f"{prefix}_template_aliases",
        }
        def collection_handles() -> dict[str, Collection[_Document]]:
            return {
                key: database.get_collection(name)
                for key, name in self._collection_names.items()
            }

        self._collections = _mongo_call(
            "Mongo collection handles could not be created",
            collection_handles,
        )
        self.intents = MongoIntentRepository(self._collections["intents"], clock=clock)
        self.recipient_policy = MongoRecipientEligibilityPolicy(
            self._collections["recipient_policy"]
        )
        self.sessions = MongoTextSessionPolicy(
            self._collections["sessions"],
            provider_key=provider,
            phone_number_id=phone,
            clock=clock,
        )
        self.templates = MongoTemplateCatalog(self._collections["templates"])

    def index_plan(self) -> tuple[MongoIndexDefinition, ...]:
        intents = self._collection_names["intents"]
        policies = self._collection_names["recipient_policy"]
        sessions = self._collection_names["sessions"]
        templates = self._collection_names["templates"]
        return (
            MongoIndexDefinition(
                intents,
                f"{intents}_idempotency_uq",
                (("source_flow", ASCENDING), ("idempotency_key", ASCENDING)),
                True,
            ),
            MongoIndexDefinition(
                intents,
                f"{intents}_intent_id_uq",
                (("intent_id", ASCENDING),),
                True,
            ),
            MongoIndexDefinition(
                intents,
                f"{intents}_correlation_id_uq",
                (("correlation_id", ASCENDING),),
                True,
            ),
            MongoIndexDefinition(
                intents,
                f"{intents}_recovery",
                (("state", ASCENDING), ("dispatch_started_at_epoch", ASCENDING)),
            ),
            MongoIndexDefinition(
                policies,
                f"{policies}_recipient_uq",
                (("recipient", ASCENDING),),
                True,
            ),
            MongoIndexDefinition(
                sessions,
                f"{sessions}_scope_uq",
                (
                    ("provider_key", ASCENDING),
                    ("phone_number_id", ASCENDING),
                    ("recipient", ASCENDING),
                ),
                True,
            ),
            MongoIndexDefinition(
                sessions,
                f"{sessions}_expiry",
                (
                    ("provider_key", ASCENDING),
                    ("phone_number_id", ASCENDING),
                    ("expires_at_epoch", ASCENDING),
                ),
            ),
            MongoIndexDefinition(
                templates,
                f"{templates}_key_uq",
                (("template_key", ASCENDING),),
                True,
            ),
        )

    def ensure_indexes(self) -> tuple[str, ...]:
        ensured: list[str] = []
        for definition in self.index_plan():
            collection = next(
                value
                for key, value in self._collections.items()
                if self._collection_names[key] == definition.collection_name
            )
            name = _mongo_call(
                "Mongo index creation failed",
                partial(
                    collection.create_index,
                    list(definition.keys),
                    name=definition.name,
                    unique=definition.unique,
                ),
            )
            if name != definition.name:
                raise MongoPersistenceError("Mongo index creation returned an unexpected name")
            ensured.append(name)
        return tuple(ensured)


__all__ = [
    "MONGO_RECORD_SCHEMA_VERSION",
    "MongoIndexDefinition",
    "MongoIntentRepository",
    "MongoMessagingPersistence",
    "MongoPersistenceConflictError",
    "MongoPersistenceError",
    "MongoRecipientEligibilityPolicy",
    "MongoRecordError",
    "MongoTemplateCatalog",
    "MongoTextSessionPolicy",
]
