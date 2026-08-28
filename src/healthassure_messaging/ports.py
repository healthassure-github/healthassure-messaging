from __future__ import annotations

from typing import Protocol

from .enums import IntentState
from .service_contracts import (
    DispatchResult,
    IntentCreationResult,
    MessageIntent,
    RecipientEligibility,
    TemplateAlias,
)


class TemplateCatalog(Protocol):
    def get(self, template_key: str) -> TemplateAlias | None:
        """Return the exact alias registered under *template_key*, if any."""


class RecipientEligibilityPolicy(Protocol):
    def evaluate(self, recipient: str) -> RecipientEligibility:
        """Return consent and suppression eligibility for normalized E.164 input."""


class TextSessionPolicy(Protocol):
    def has_active_session(self, recipient: str, purpose_key: str) -> bool:
        """Return whether free-form text is currently eligible for this purpose."""


class IntentRepository(Protocol):
    def get(self, source_flow: str, idempotency_key: str) -> MessageIntent | None:
        """Look up an intent by its backend-scoped idempotency identity."""

    def create_if_absent(self, intent: MessageIntent) -> IntentCreationResult:
        """Atomically create an intent or return the existing scoped intent."""

    def claim(self, intent_id: str) -> MessageIntent | None:
        """Atomically change a pending intent to dispatching."""

    def complete(
        self,
        intent_id: str,
        *,
        expected_state: IntentState,
        result: DispatchResult,
    ) -> MessageIntent | None:
        """Atomically finalize an intent only from the expected state."""
