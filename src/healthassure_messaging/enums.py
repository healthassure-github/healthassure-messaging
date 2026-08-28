from __future__ import annotations

from ._enum import _StringEnum


class SendDisposition(_StringEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ErrorCategory(_StringEnum):
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    PROVIDER_TEMPORARY = "provider_temporary"
    PROVIDER_PERMANENT = "provider_permanent"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class TemplateComponentType(_StringEnum):
    HEADER = "header"
    BODY = "body"
    BUTTON = "button"


class DeliveryStatus(_StringEnum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class IntentState(_StringEnum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
