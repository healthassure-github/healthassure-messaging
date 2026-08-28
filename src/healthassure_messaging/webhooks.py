from __future__ import annotations

from dataclasses import dataclass

from .enums import DeliveryStatus


def _require_non_empty_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_optional_text(value: object, field_name: str) -> None:
    if value is not None:
        _require_non_empty_text(value, field_name)


@dataclass(frozen=True, slots=True)
class DeliveryError:
    provider_code: str
    provider_subcode: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty_text(self.provider_code, "provider_code")
        if self.provider_subcode is not None and (
            type(self.provider_subcode) is not int
            or not 0 <= self.provider_subcode <= 2_147_483_647
        ):
            raise ValueError("provider_subcode must be a non-negative 32-bit integer")


@dataclass(frozen=True, slots=True)
class DeliveryStatusEvent:
    provider_key: str
    provider_message_id: str
    status: DeliveryStatus
    provider_status: str
    occurred_at_epoch: int
    recipient_id: str
    waba_id: str
    phone_number_id: str
    errors: tuple[DeliveryError, ...] = ()
    conversation_id: str | None = None
    conversation_origin_type: str | None = None
    pricing_model: str | None = None
    pricing_category: str | None = None
    billable: bool | None = None

    def __post_init__(self) -> None:
        _require_non_empty_text(self.provider_key, "provider_key")
        _require_non_empty_text(self.provider_message_id, "provider_message_id")
        if not isinstance(self.status, DeliveryStatus):
            raise TypeError("status must be a DeliveryStatus")
        _require_non_empty_text(self.provider_status, "provider_status")
        if type(self.occurred_at_epoch) is not int or self.occurred_at_epoch < 0:
            raise ValueError("occurred_at_epoch must be a non-negative integer")
        _require_non_empty_text(self.recipient_id, "recipient_id")
        _require_non_empty_text(self.waba_id, "waba_id")
        _require_non_empty_text(self.phone_number_id, "phone_number_id")
        if not isinstance(self.errors, tuple):
            raise TypeError("errors must be a tuple")
        if not all(isinstance(error, DeliveryError) for error in self.errors):
            raise TypeError("errors must contain only DeliveryError values")
        _require_optional_text(self.conversation_id, "conversation_id")
        _require_optional_text(self.conversation_origin_type, "conversation_origin_type")
        _require_optional_text(self.pricing_model, "pricing_model")
        _require_optional_text(self.pricing_category, "pricing_category")
        if self.billable is not None and type(self.billable) is not bool:
            raise TypeError("billable must be a boolean or None")


__all__ = ["DeliveryError", "DeliveryStatusEvent"]
