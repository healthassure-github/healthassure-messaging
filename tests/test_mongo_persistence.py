from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from dataclasses import replace
from typing import Any, cast

from mongo_fakes import FakeDatabase
from pymongo.database import Database

from healthassure_messaging import (
    DispatchResult,
    ErrorCategory,
    InMemoryTemplateCatalog,
    InMemoryTextSessionPolicy,
    IntentState,
    MessageIntent,
    MessageRequest,
    MessagingGateway,
    MessagingService,
    NormalizedError,
    PostDispatchPersistenceError,
    ProviderRegistry,
    RecipientEligibility,
    SendDisposition,
    SendResult,
    TemplateAlias,
    TemplateComponentSpec,
    TemplateComponentType,
)
from healthassure_messaging.persistence import mongo as mongo_module
from healthassure_messaging.persistence.mongo import (
    MONGO_RECORD_SCHEMA_VERSION,
    MongoMessagingPersistence,
    MongoPersistenceConflictError,
    MongoPersistenceError,
    MongoRecordError,
)

RECIPIENT = "+12025550125"
SECOND_RECIPIENT = "+12025550126"


class CompletionFailureProvider:
    key = "meta"

    def __init__(self, fake: FakeDatabase) -> None:
        self._fake = fake
        self.requests: list[MessageRequest] = []

    def send(self, request: MessageRequest) -> SendResult:
        self.requests.append(request)
        self._fake.collection("messaging_intents").fail_next(
            "find_one_and_update",
            "unsafe post-dispatch completion",
        )
        return SendResult(
            provider_key=self.key,
            disposition=SendDisposition.ACCEPTED,
            correlation_id=request.correlation_id,
        )


class FixedClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def _database(fake: FakeDatabase) -> Database[dict[str, Any]]:
    return cast(Database[dict[str, Any]], fake)


def _persistence(
    fake: FakeDatabase | None = None,
    *,
    clock: FixedClock | None = None,
    prefix: str = "messaging",
) -> tuple[MongoMessagingPersistence, FakeDatabase, FixedClock]:
    selected_fake = fake or FakeDatabase()
    selected_clock = clock or FixedClock(100)
    persistence = MongoMessagingPersistence(
        database=_database(selected_fake),
        provider_key="meta",
        phone_number_id="synthetic-phone-id",
        collection_prefix=prefix,
        clock=selected_clock,
    )
    return persistence, selected_fake, selected_clock


def _intent(
    *,
    suffix: str = "1",
    source_flow: str = "synthetic-flow",
    idempotency_key: str = "synthetic-key",
    intent_id: str | None = None,
    correlation_id: str | None = None,
) -> MessageIntent:
    return MessageIntent(
        intent_id=intent_id or f"intent-{suffix}",
        source_flow=source_flow,
        idempotency_key=idempotency_key,
        actor_id="synthetic-actor",
        provider_key="meta",
        correlation_id=correlation_id or f"correlation-{suffix}",
        request_fingerprint=f"fingerprint-{suffix}",
        serialized_request=(
            '{"request":{"correlation_id":"correlation-'
            + suffix
            + '","idempotency_key":"synthetic-key","message":{"body":"synthetic",'
            '"type":"text"},"recipient":"+12025550125"},"schema_version":1}'
        ),
    )


def _accepted_result(intent: MessageIntent) -> DispatchResult:
    return DispatchResult(
        intent_id=intent.intent_id,
        provider_key=intent.provider_key,
        disposition=SendDisposition.ACCEPTED,
        correlation_id=intent.correlation_id,
        provider_message_id="synthetic-provider-message",
        provider_status="accepted",
    )


def _rejected_result(intent: MessageIntent) -> DispatchResult:
    return DispatchResult(
        intent_id=intent.intent_id,
        provider_key=intent.provider_key,
        disposition=SendDisposition.REJECTED,
        correlation_id=intent.correlation_id,
        provider_status="rejected",
        error=NormalizedError(
            category=ErrorCategory.PROVIDER_PERMANENT,
            safe_message="Provider rejected the request",
            provider_code="synthetic-code",
            provider_subcode=7,
            http_status=400,
            retriable=False,
            unknown_outcome=False,
        ),
    )


class OptionalMongoBoundaryTests(unittest.TestCase):
    def test_base_package_imports_when_pymongo_is_blocked(self) -> None:
        source_root = os.path.abspath("src")
        script = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'pymongo' or name.startswith('pymongo.'):
        error = ModuleNotFoundError('blocked synthetic pymongo')
        error.name = 'pymongo'
        raise error
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import healthassure_messaging
assert healthassure_messaging.REQUEST_SCHEMA_VERSION == 1
try:
    import healthassure_messaging.persistence.mongo
except ImportError as error:
    assert '[mongo]' in str(error)
    assert 'blocked synthetic pymongo' not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
else:
    raise AssertionError('optional Mongo module unexpectedly imported')
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = source_root
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class MongoPersistenceConstructionTests(unittest.TestCase):
    def test_construction_only_obtains_collection_handles(self) -> None:
        persistence, fake, _ = _persistence(prefix="synthetic")
        self.assertEqual(MONGO_RECORD_SCHEMA_VERSION, 1)
        self.assertEqual(
            fake.get_collection_calls,
            [
                "synthetic_intents",
                "synthetic_recipient_policies",
                "synthetic_sessions",
                "synthetic_template_aliases",
            ],
        )
        self.assertEqual(fake.write_count, 0)
        self.assertIsNotNone(persistence.intents)
        self.assertIsNotNone(persistence.recipient_policy)
        self.assertIsNotNone(persistence.sessions)
        self.assertIsNotNone(persistence.templates)

    def test_index_plan_is_deterministic_and_contains_no_ttl(self) -> None:
        persistence, fake, _ = _persistence()
        first = persistence.index_plan()
        second = persistence.index_plan()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 8)
        self.assertEqual(
            tuple(definition.name for definition in first),
            (
                "messaging_intents_idempotency_uq",
                "messaging_intents_intent_id_uq",
                "messaging_intents_correlation_id_uq",
                "messaging_intents_recovery",
                "messaging_recipient_policies_recipient_uq",
                "messaging_sessions_scope_uq",
                "messaging_sessions_expiry",
                "messaging_template_aliases_key_uq",
            ),
        )
        self.assertNotIn("expireAfterSeconds", repr(first))
        self.assertEqual(fake.write_count, 0)

    def test_explicit_index_creation_is_repeatable(self) -> None:
        persistence, fake, _ = _persistence()
        names = persistence.ensure_indexes()
        writes_after_first = fake.write_count
        self.assertEqual(names, tuple(definition.name for definition in persistence.index_plan()))
        self.assertEqual(persistence.ensure_indexes(), names)
        self.assertEqual(fake.write_count, writes_after_first)
        for collection_name in fake.get_collection_calls:
            for _, unique in fake.collection(collection_name).indexes.values():
                self.assertIsInstance(unique, bool)

    def test_incompatible_index_failure_is_sanitized(self) -> None:
        persistence, fake, _ = _persistence()
        collection = fake.collection("messaging_intents")
        collection.create_index(
            [("wrong", 1)],
            name="messaging_intents_idempotency_uq",
            unique=False,
        )
        with self.assertRaises(MongoPersistenceError) as caught:
            persistence.ensure_indexes()
        self.assertEqual(str(caught.exception), "Mongo index creation failed")
        self.assertNotIn("unsafe incompatible-index detail", repr(caught.exception))


class MongoIntentRepositoryTests(unittest.TestCase):
    def test_create_returns_existing_scope_and_conflicts_on_unrelated_unique_key(self) -> None:
        persistence, _, _ = _persistence()
        persistence.ensure_indexes()
        original = _intent()
        first = persistence.intents.create_if_absent(original)
        replay = persistence.intents.create_if_absent(replace(original, actor_id="other-actor"))
        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(replay.intent, original)

        unrelated = _intent(
            suffix="2",
            source_flow="other-flow",
            idempotency_key="other-key",
            intent_id=original.intent_id,
        )
        with self.assertRaises(MongoPersistenceConflictError):
            persistence.intents.create_if_absent(unrelated)
        self.assertIsNone(persistence.intents.get("other-flow", "other-key"))

    def test_concurrent_create_has_one_winner(self) -> None:
        persistence, _, _ = _persistence()
        persistence.ensure_indexes()
        intent = _intent()
        barrier = threading.Barrier(3)
        created: list[bool] = []

        def create() -> None:
            barrier.wait()
            created.append(persistence.intents.create_if_absent(intent).created)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(created), [False, True])

    def test_concurrent_claim_has_one_winner_and_one_attempt(self) -> None:
        persistence, fake, _ = _persistence()
        persistence.intents.create_if_absent(_intent())
        barrier = threading.Barrier(3)
        winners: list[MessageIntent | None] = []

        def claim() -> None:
            barrier.wait()
            winners.append(persistence.intents.claim("intent-1"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(winner is not None for winner in winners), 1)
        document = fake.collection("messaging_intents").documents[0]
        self.assertEqual(document["attempt_count"], 1)
        self.assertEqual(len(document["attempts"]), 1)

    def test_conditional_completion_round_trips_result_and_attempt(self) -> None:
        persistence, fake, clock = _persistence()
        intent = _intent()
        persistence.intents.create_if_absent(intent)
        claimed = persistence.intents.claim(intent.intent_id)
        self.assertIsNotNone(claimed)
        clock.value = 110
        result = _rejected_result(intent)
        completed = persistence.intents.complete(
            intent.intent_id,
            expected_state=IntentState.DISPATCHING,
            result=result,
        )
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.state, IntentState.REJECTED)
        self.assertEqual(completed.result, result)
        self.assertIsNone(
            persistence.intents.complete(
                intent.intent_id,
                expected_state=IntentState.DISPATCHING,
                result=result,
            )
        )
        document = fake.collection("messaging_intents").documents[0]
        attempt = document["attempts"][0]
        self.assertEqual(attempt["completed_at_epoch"], 110)
        self.assertEqual(attempt["provider_message_id"], result.provider_message_id)
        self.assertEqual(attempt["error"]["provider_subcode"], 7)

    def test_pending_policy_rejection_has_zero_attempts(self) -> None:
        persistence, fake, _ = _persistence()
        intent = _intent()
        persistence.intents.create_if_absent(intent)
        completed = persistence.intents.complete(
            intent.intent_id,
            expected_state=IntentState.PENDING,
            result=_rejected_result(intent),
        )
        self.assertIsNotNone(completed)
        document = fake.collection("messaging_intents").documents[0]
        self.assertEqual(document["attempt_count"], 0)
        self.assertEqual(document["attempts"], [])

    def test_completion_failure_leaves_record_dispatching(self) -> None:
        persistence, fake, _ = _persistence()
        intent = _intent()
        persistence.intents.create_if_absent(intent)
        persistence.intents.claim(intent.intent_id)
        collection = fake.collection("messaging_intents")
        collection.fail_next("find_one_and_update", "unsafe recipient body token")
        with self.assertRaises(MongoPersistenceError) as caught:
            persistence.intents.complete(
                intent.intent_id,
                expected_state=IntentState.DISPATCHING,
                result=_accepted_result(intent),
            )
        self.assertEqual(str(caught.exception), "Mongo intent completion failed")
        self.assertNotIn("unsafe recipient body token", repr(caught.exception))
        stored = persistence.intents.get(intent.source_flow, intent.idempotency_key)
        assert stored is not None
        self.assertEqual(stored.state, IntentState.DISPATCHING)

    def test_completion_identity_mismatch_never_writes(self) -> None:
        expected_intent = _intent()
        mismatch_results = (
            replace(
                _accepted_result(expected_intent),
                intent_id="unsafe-mismatched-intent",
            ),
            replace(
                _accepted_result(expected_intent),
                provider_key="unsafe-mismatched-provider",
            ),
            replace(
                _accepted_result(expected_intent),
                correlation_id="unsafe-mismatched-correlation",
            ),
        )
        for mismatched_result in mismatch_results:
            with self.subTest(result=mismatched_result):
                persistence, fake, _ = _persistence()
                intent = _intent()
                persistence.intents.create_if_absent(intent)
                persistence.intents.claim(intent.intent_id)
                collection = fake.collection("messaging_intents")
                updates_before = collection.call_count.get("find_one_and_update", 0)
                writes_before = collection.write_count

                with self.assertRaises(MongoPersistenceConflictError) as caught:
                    persistence.intents.complete(
                        intent.intent_id,
                        expected_state=IntentState.DISPATCHING,
                        result=mismatched_result,
                    )

                self.assertEqual(
                    str(caught.exception),
                    "Mongo intent completion identity conflicts with the stored intent",
                )
                self.assertNotIn("unsafe-mismatched", repr(caught.exception))
                self.assertEqual(
                    collection.call_count.get("find_one_and_update", 0),
                    updates_before,
                )
                self.assertEqual(collection.write_count, writes_before)
                document = collection.documents[0]
                self.assertEqual(document["state"], IntentState.DISPATCHING.value)
                self.assertIsNone(document["result"])
                self.assertIsNone(document["attempts"][0]["completed_at_epoch"])

    def test_bounded_stale_recovery_is_unknown_without_new_attempt(self) -> None:
        persistence, fake, clock = _persistence()
        for suffix in ("1", "2"):
            intent = _intent(
                suffix=suffix,
                source_flow=f"flow-{suffix}",
                idempotency_key=f"key-{suffix}",
            )
            persistence.intents.create_if_absent(intent)
            persistence.intents.claim(intent.intent_id)
        clock.value = 200
        self.assertEqual(
            persistence.intents.recover_stale_dispatching(stale_before_epoch=150, limit=1),
            1,
        )
        self.assertEqual(
            persistence.intents.recover_stale_dispatching(stale_before_epoch=150, limit=100),
            1,
        )
        documents = fake.collection("messaging_intents").documents
        self.assertEqual([document["attempt_count"] for document in documents], [1, 1])
        for document in documents:
            self.assertEqual(document["state"], IntentState.UNKNOWN.value)
            self.assertEqual(document["attempts"][0]["state"], SendDisposition.UNKNOWN.value)
            self.assertTrue(document["result"]["error"]["unknown_outcome"])
            self.assertFalse(document["result"]["error"]["retriable"])

    def test_concurrent_recovery_completes_active_attempt_once(self) -> None:
        persistence, fake, clock = _persistence()
        intent = _intent()
        persistence.intents.create_if_absent(intent)
        persistence.intents.claim(intent.intent_id)
        clock.value = 200
        barrier = threading.Barrier(3)
        counts: list[int] = []

        def recover() -> None:
            barrier.wait()
            counts.append(
                persistence.intents.recover_stale_dispatching(stale_before_epoch=150)
            )

        threads = [threading.Thread(target=recover) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(counts), 1)
        document = fake.collection("messaging_intents").documents[0]
        self.assertEqual(document["attempt_count"], 1)
        self.assertEqual(len(document["attempts"]), 1)

    def test_malformed_schema_and_mongo_error_fail_safely(self) -> None:
        persistence, fake, _ = _persistence()
        collection = fake.collection("messaging_intents")
        malformed = {
            "record_schema_version": 99,
            "source_flow": "synthetic-flow",
            "idempotency_key": "synthetic-key",
        }
        collection.insert_one(malformed)
        with self.assertRaises(MongoRecordError):
            persistence.intents.get("synthetic-flow", "synthetic-key")

        collection.fail_next("find_one", "unsafe URI and credentials")
        with self.assertRaises(MongoPersistenceError) as caught:
            persistence.intents.get("other-flow", "other-key")
        self.assertEqual(str(caught.exception), "Mongo intent lookup failed")
        self.assertNotIn("unsafe URI and credentials", repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_service_with_completion_failure_never_retries_provider(self) -> None:
        persistence, fake, _ = _persistence()
        persistence.ensure_indexes()
        provider = CompletionFailureProvider(fake)
        registry = ProviderRegistry()
        registry.register("meta", provider)
        service = MessagingService(
            gateway=MessagingGateway(registry),
            template_catalog=InMemoryTemplateCatalog(()),
            recipient_policy=persistence.recipient_policy,
            text_session_policy=InMemoryTextSessionPolicy(((RECIPIENT, "purpose"),)),
            intent_repository=persistence.intents,
            text_provider_key="meta",
            correlation_id_factory=lambda: "correlation-service",
            intent_id_factory=lambda: "intent-service",
        )
        persistence.recipient_policy.grant_consent(
            event_id="consent-service",
            recipient=RECIPIENT,
            actor_id="actor",
            source="synthetic",
            evidence_reference="evidence",
            occurred_at_epoch=1,
        )
        with self.assertRaises(PostDispatchPersistenceError):
            service.send_text(
                recipient=RECIPIENT,
                body="synthetic body",
                purpose_key="purpose",
                source_flow="flow-service",
                idempotency_key="key-service",
                actor_id="actor",
            )
        self.assertEqual(len(provider.requests), 1)
        stored = persistence.intents.get("flow-service", "key-service")
        assert stored is not None
        self.assertEqual(stored.state, IntentState.DISPATCHING)


class MongoRecipientPolicyTests(unittest.TestCase):
    def _event(self, event_id: str) -> dict[str, object]:
        return {
            "event_id": event_id,
            "recipient": RECIPIENT,
            "actor_id": "synthetic-actor",
            "source": "synthetic-source",
            "evidence_reference": "synthetic-evidence",
            "occurred_at_epoch": 10,
        }

    def test_consent_suppression_idempotency_and_conflict(self) -> None:
        persistence, fake, _ = _persistence()
        policy = persistence.recipient_policy
        self.assertEqual(policy.evaluate(RECIPIENT), RecipientEligibility(False, False))
        granted = policy.grant_consent(**self._event("grant"))  # type: ignore[arg-type]
        self.assertEqual(granted, RecipientEligibility(True, False))
        self.assertEqual(
            policy.grant_consent(**self._event("grant")),  # type: ignore[arg-type]
            granted,
        )
        suppressed = policy.apply_suppression(**self._event("suppress"))  # type: ignore[arg-type]
        self.assertEqual(suppressed, RecipientEligibility(True, True))
        still_suppressed = policy.grant_consent(**self._event("grant-again"))  # type: ignore[arg-type]
        self.assertEqual(still_suppressed, RecipientEligibility(True, True))
        revoked = policy.revoke_consent(**self._event("revoke"))  # type: ignore[arg-type]
        self.assertEqual(revoked, RecipientEligibility(False, True))
        cleared = policy.clear_suppression(**self._event("clear"))  # type: ignore[arg-type]
        self.assertEqual(cleared, RecipientEligibility(False, False))
        events = fake.collection("messaging_recipient_policies").documents[0]["events"]
        self.assertEqual(len(events), 5)

        conflicting = self._event("grant")
        conflicting["evidence_reference"] = "different-evidence"
        with self.assertRaises(MongoPersistenceConflictError):
            policy.grant_consent(**conflicting)  # type: ignore[arg-type]

    def test_malformed_evidence_and_incoherent_state_fail_closed(self) -> None:
        valid_event = {
            "event_id": "event-1",
            "operation": "grant_consent",
            "actor_id": "synthetic-actor",
            "source": "synthetic-source",
            "evidence_reference": "synthetic-evidence",
            "occurred_at_epoch": 10,
        }
        malformed_records = (
            {"events": ["not-a-document"]},
            {"events": [{key: value for key, value in valid_event.items() if key != "actor_id"}]},
            {"events": [{**valid_event, "operation": "unsafe-operation"}]},
            {"events": [{**valid_event, "occurred_at_epoch": -1}]},
            {"events": [valid_event, valid_event]},
            {"consented": True, "events": []},
            {"consented": False, "events": [valid_event]},
            {"suppressed": True, "events": [valid_event]},
        )
        for malformed in malformed_records:
            with self.subTest(malformed=malformed):
                persistence, fake, _ = _persistence()
                document = {
                    "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                    "recipient": RECIPIENT,
                    "consented": False,
                    "suppressed": False,
                    "events": [],
                    **malformed,
                }
                fake.collection("messaging_recipient_policies").insert_one(document)
                with self.assertRaisesRegex(
                    MongoRecordError,
                    "^Mongo record does not match the supported schema$",
                ):
                    persistence.recipient_policy.evaluate(RECIPIENT)

    def test_stored_policy_recipient_must_be_valid_and_match_scope(self) -> None:
        base = {
            "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
            "recipient": RECIPIENT,
            "consented": False,
            "suppressed": False,
            "events": [],
        }
        for stored_recipient in ("not-e164", SECOND_RECIPIENT):
            with self.subTest(stored_recipient=stored_recipient), self.assertRaises(
                MongoRecordError
            ):
                mongo_module._policy_record_from_document(
                    {**base, "recipient": stored_recipient},
                    expected_recipient=RECIPIENT,
                )


class MongoSessionTests(unittest.TestCase):
    def test_scope_purpose_expiry_and_event_idempotency(self) -> None:
        clock = FixedClock(20)
        persistence, fake, _ = _persistence(clock=clock)
        sessions = persistence.sessions
        sessions.open_session(
            event_id="inbound-1",
            recipient=RECIPIENT,
            allowed_purpose_keys=("support", "reminder"),
            opened_at_epoch=10,
            expires_at_epoch=30,
        )
        sessions.open_session(
            event_id="inbound-1",
            recipient=RECIPIENT,
            allowed_purpose_keys=("support", "reminder"),
            opened_at_epoch=10,
            expires_at_epoch=30,
        )
        self.assertTrue(sessions.has_active_session(RECIPIENT, "support"))
        self.assertFalse(sessions.has_active_session(RECIPIENT, "other"))
        self.assertFalse(sessions.has_active_session(SECOND_RECIPIENT, "support"))
        document = fake.collection("messaging_sessions").documents[0]
        self.assertEqual(document["provider_key"], "meta")
        self.assertEqual(document["phone_number_id"], "synthetic-phone-id")
        self.assertEqual(len(document["events"]), 1)
        clock.value = 30
        self.assertFalse(sessions.has_active_session(RECIPIENT, "support"))

        with self.assertRaises(MongoPersistenceConflictError):
            sessions.open_session(
                event_id="inbound-1",
                recipient=RECIPIENT,
                allowed_purpose_keys=("support",),
                opened_at_epoch=10,
                expires_at_epoch=40,
            )

    def test_malformed_events_and_incoherent_state_fail_closed(self) -> None:
        valid_event = {
            "event_id": "inbound-1",
            "allowed_purpose_keys": ["support", "reminder"],
            "opened_at_epoch": 10,
            "expires_at_epoch": 30,
        }
        malformed_records = (
            {"events": ["not-a-document"]},
            {"events": [{key: value for key, value in valid_event.items() if key != "event_id"}]},
            {"events": [{**valid_event, "allowed_purpose_keys": ["support", "support"]}]},
            {"events": [{**valid_event, "allowed_purpose_keys": [""]}]},
            {"events": [{**valid_event, "opened_at_epoch": -1}]},
            {"events": [{**valid_event, "expires_at_epoch": 10}]},
            {"events": [valid_event, valid_event]},
            {
                "allowed_purpose_keys": ["other"],
                "opened_at_epoch": 10,
                "expires_at_epoch": 30,
                "events": [valid_event],
            },
            {
                "allowed_purpose_keys": ["support", "reminder"],
                "opened_at_epoch": 11,
                "expires_at_epoch": 30,
                "events": [valid_event],
            },
            {
                "allowed_purpose_keys": ["support"],
                "opened_at_epoch": 0,
                "expires_at_epoch": 0,
                "events": [],
            },
        )
        for malformed in malformed_records:
            with self.subTest(malformed=malformed):
                persistence, fake, _ = _persistence(clock=FixedClock(20))
                document = {
                    "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                    "provider_key": "meta",
                    "phone_number_id": "synthetic-phone-id",
                    "recipient": RECIPIENT,
                    "allowed_purpose_keys": ["support", "reminder"],
                    "opened_at_epoch": 10,
                    "expires_at_epoch": 30,
                    "events": [valid_event],
                    **malformed,
                }
                fake.collection("messaging_sessions").insert_one(document)
                with self.assertRaisesRegex(
                    MongoRecordError,
                    "^Mongo record does not match the supported schema$",
                ):
                    persistence.sessions.has_active_session(RECIPIENT, "support")

    def test_stored_session_scope_must_match_configuration_and_recipient(self) -> None:
        base = {
            "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
            "provider_key": "meta",
            "phone_number_id": "synthetic-phone-id",
            "recipient": RECIPIENT,
            "allowed_purpose_keys": [],
            "opened_at_epoch": 0,
            "expires_at_epoch": 0,
            "events": [],
        }
        mismatches = (
            {"provider_key": "other-provider"},
            {"phone_number_id": "other-phone"},
            {"recipient": SECOND_RECIPIENT},
            {"recipient": "not-e164"},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch), self.assertRaises(MongoRecordError):
                mongo_module._session_record_from_document(
                    {**base, **mismatch},
                    expected_provider_key="meta",
                    expected_phone_number_id="synthetic-phone-id",
                    expected_recipient=RECIPIENT,
                )


class MongoTemplateCatalogTests(unittest.TestCase):
    def test_ordering_revision_updates_and_deactivation(self) -> None:
        persistence, fake, _ = _persistence()
        persistence.ensure_indexes()
        alias = TemplateAlias(
            key="synthetic-template",
            provider_key="meta",
            template_name="synthetic_template_v1",
            language_code="en_US",
            components=(
                TemplateComponentSpec(
                    component_type=TemplateComponentType.HEADER,
                    parameter_names=("second", "first"),
                ),
                TemplateComponentSpec(
                    component_type=TemplateComponentType.BODY,
                    parameter_names=("third", "first"),
                ),
            ),
        )
        self.assertEqual(
            persistence.templates.save(
                alias,
                expected_revision=None,
                actor_id="synthetic-actor",
                updated_at_epoch=10,
            ),
            1,
        )
        self.assertEqual(persistence.templates.get(alias.key), alias)
        document = fake.collection("messaging_template_aliases").documents[0]
        self.assertEqual(document["components"][0]["parameter_names"], ["second", "first"])

        updated_alias = replace(alias, language_code="en_GB")
        self.assertEqual(
            persistence.templates.save(
                updated_alias,
                expected_revision=1,
                actor_id="synthetic-actor",
                updated_at_epoch=20,
            ),
            2,
        )
        with self.assertRaises(MongoPersistenceConflictError):
            persistence.templates.save(
                alias,
                expected_revision=1,
                actor_id="synthetic-actor",
                updated_at_epoch=30,
            )
        self.assertEqual(
            persistence.templates.deactivate(
                alias.key,
                expected_revision=2,
                actor_id="synthetic-actor",
                updated_at_epoch=30,
            ),
            3,
        )
        self.assertIsNone(persistence.templates.get(alias.key))
        with self.assertRaises(MongoPersistenceConflictError):
            persistence.templates.deactivate(
                alias.key,
                expected_revision=2,
                actor_id="synthetic-actor",
                updated_at_epoch=31,
            )

    def test_malformed_template_audit_metadata_fails_closed(self) -> None:
        base = {
            "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
            "template_key": "synthetic-template",
            "provider_key": "meta",
            "template_name": "synthetic_template",
            "language_code": "en_US",
            "components": [],
            "active": True,
            "revision": 1,
            "actor_id": "synthetic-actor",
            "updated_at_epoch": 10,
        }
        malformed_values = (
            {"active": "yes"},
            {"revision": 0},
            {"revision": True},
            {"actor_id": ""},
            {"updated_at_epoch": -1},
            {"updated_at_epoch": True},
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed), self.assertRaisesRegex(
                MongoRecordError,
                "^Mongo record does not match the supported schema$",
            ):
                mongo_module._alias_from_document({**base, **malformed})


class MongoStoredRecordValidationTests(unittest.TestCase):
    def test_malformed_policy_session_and_template_records_fail_safely(self) -> None:
        persistence, fake, _ = _persistence()
        fake.collection("messaging_recipient_policies").insert_one(
            {
                "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                "recipient": RECIPIENT,
                "consented": "unsafe-not-a-boolean",
                "suppressed": False,
                "events": [],
            }
        )
        with self.assertRaisesRegex(
            MongoRecordError,
            "^Mongo record does not match the supported schema$",
        ):
            persistence.recipient_policy.evaluate(RECIPIENT)

        fake.collection("messaging_sessions").insert_one(
            {
                "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                "provider_key": "meta",
                "phone_number_id": "synthetic-phone-id",
                "recipient": RECIPIENT,
                "allowed_purpose_keys": ["support"],
                "expires_at_epoch": True,
                "events": [],
            }
        )
        with self.assertRaisesRegex(
            MongoRecordError,
            "^Mongo record does not match the supported schema$",
        ):
            persistence.sessions.has_active_session(RECIPIENT, "support")

        fake.collection("messaging_template_aliases").insert_one(
            {
                "record_schema_version": MONGO_RECORD_SCHEMA_VERSION,
                "template_key": "synthetic-template",
                "provider_key": "meta",
                "template_name": "synthetic_template",
                "language_code": "en_US",
                "components": [
                    {
                        "component_type": "unsafe-secret-record-value",
                        "parameter_names": ["name"],
                    }
                ],
                "active": True,
            }
        )
        with self.assertRaises(MongoRecordError) as caught:
            persistence.templates.get("synthetic-template")
        self.assertEqual(
            str(caught.exception),
            "Mongo record does not match the supported schema",
        )
        self.assertNotIn("unsafe-secret-record-value", repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_store_lookup_driver_errors_are_fixed_and_unchained(self) -> None:
        persistence, fake, _ = _persistence()
        checks = (
            (
                fake.collection("messaging_recipient_policies"),
                lambda: persistence.recipient_policy.evaluate(RECIPIENT),
                "Mongo recipient policy lookup failed",
            ),
            (
                fake.collection("messaging_sessions"),
                lambda: persistence.sessions.has_active_session(RECIPIENT, "support"),
                "Mongo session lookup failed",
            ),
            (
                fake.collection("messaging_template_aliases"),
                lambda: persistence.templates.get("synthetic-template"),
                "Mongo template lookup failed",
            ),
        )
        for collection, operation, message in checks:
            with self.subTest(message=message):
                collection.fail_next("find_one", "unsafe raw Mongo detail")
                with self.assertRaises(MongoPersistenceError) as caught:
                    operation()
                self.assertEqual(str(caught.exception), message)
                self.assertNotIn("unsafe raw Mongo detail", repr(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_malformed_result_error_is_fixed_and_unchained(self) -> None:
        malformed_result = {
            "intent_id": "synthetic-intent",
            "provider_key": "meta",
            "disposition": "unsafe-secret-disposition",
            "correlation_id": "synthetic-correlation",
            "provider_message_id": None,
            "provider_status": None,
            "error": None,
            "idempotent_replay": False,
        }
        with self.assertRaises(MongoRecordError) as caught:
            mongo_module._result_from_document(malformed_result)
        self.assertEqual(
            str(caught.exception),
            "Mongo record does not match the supported schema",
        )
        self.assertNotIn("unsafe-secret-disposition", repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)


if __name__ == "__main__":
    unittest.main()
