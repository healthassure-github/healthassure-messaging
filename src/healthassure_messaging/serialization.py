from __future__ import annotations

import json
from collections.abc import Callable
from typing import NoReturn, cast

from .contracts import (
    MessageRequest,
    TemplateComponent,
    TemplateMessage,
    TemplateReference,
    TextMessage,
    TextParameter,
)
from .enums import TemplateComponentType

REQUEST_SCHEMA_VERSION = 1


class RequestSerializationError(ValueError):
    """Raised when a request envelope is malformed or violates the schema."""


class UnsupportedSchemaVersionError(RequestSerializationError):
    """Raised when a request envelope uses an unsupported schema version."""


def _message_to_data(message: TextMessage | TemplateMessage) -> dict[str, object]:
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
                "parameters": [
                    {"type": "text", "text": parameter.text}
                    for parameter in component.parameters
                ],
            }
            for component in message.components
        ],
    }


def serialize_request(request: MessageRequest) -> str:
    """Serialize a request into a deterministic schema-versioned JSON envelope."""

    if not isinstance(request, MessageRequest):
        raise TypeError("request must be a MessageRequest")

    envelope: dict[str, object] = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request": {
            "recipient": request.recipient,
            "message": _message_to_data(request.message),
            "correlation_id": request.correlation_id,
            "idempotency_key": request.idempotency_key,
        },
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _reject_constant(_: str) -> NoReturn:
    raise RequestSerializationError("non-standard JSON constants are not supported")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RequestSerializationError("duplicate JSON object keys are not supported")
        result[key] = value
    return result


def _expect_object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RequestSerializationError(f"{location} must be a JSON object")
    return cast(dict[str, object], value)


def _expect_list(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise RequestSerializationError(f"{location} must be a JSON array")
    return cast(list[object], value)


def _expect_string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise RequestSerializationError(f"{location} must be a string")
    return value


def _expect_exact_keys(
    value: dict[str, object], expected: set[str], location: str
) -> dict[str, object]:
    if set(value) != expected:
        raise RequestSerializationError(f"{location} contains missing or unknown fields")
    return value


def _parse_component(value: object) -> TemplateComponent:
    component = _expect_exact_keys(
        _expect_object(value, "template component"),
        {"type", "parameters"},
        "template component",
    )
    component_type_value = _expect_string(component["type"], "template component type")
    try:
        component_type = TemplateComponentType(component_type_value)
    except ValueError as error:
        raise RequestSerializationError("unknown template component type") from error

    parameters: list[TextParameter] = []
    for raw_parameter in _expect_list(component["parameters"], "template parameters"):
        parameter = _expect_exact_keys(
            _expect_object(raw_parameter, "template parameter"),
            {"type", "text"},
            "template parameter",
        )
        if _expect_string(parameter["type"], "template parameter type") != "text":
            raise RequestSerializationError("unknown template parameter type")
        parameters.append(TextParameter(text=_expect_string(parameter["text"], "parameter text")))

    return TemplateComponent(component_type=component_type, parameters=tuple(parameters))


def _parse_message(value: object) -> TextMessage | TemplateMessage:
    message = _expect_object(value, "message")
    message_type = _expect_string(message.get("type"), "message type")
    if message_type == "text":
        _expect_exact_keys(message, {"type", "body"}, "text message")
        return TextMessage(body=_expect_string(message["body"], "text body"))

    if message_type == "template":
        _expect_exact_keys(message, {"type", "template", "components"}, "template message")
        template = _expect_exact_keys(
            _expect_object(message["template"], "template reference"),
            {"name", "language_code"},
            "template reference",
        )
        components = tuple(
            _parse_component(component)
            for component in _expect_list(message["components"], "template components")
        )
        return TemplateMessage(
            template=TemplateReference(
                name=_expect_string(template["name"], "template name"),
                language_code=_expect_string(template["language_code"], "template language code"),
            ),
            components=components,
        )

    raise RequestSerializationError("unknown message type")


def deserialize_request(payload: str | bytes) -> MessageRequest:
    """Deserialize and strictly validate a schema-versioned request envelope."""

    if not isinstance(payload, (str, bytes)):
        raise TypeError("payload must be str or bytes")

    loads: Callable[..., object] = json.loads
    try:
        parsed = loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RequestSerializationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestSerializationError("payload is not valid JSON") from error

    envelope = _expect_exact_keys(
        _expect_object(parsed, "envelope"),
        {"schema_version", "request"},
        "envelope",
    )
    schema_version = envelope["schema_version"]
    if type(schema_version) is not int or schema_version != REQUEST_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError("unsupported request schema version")

    request = _expect_exact_keys(
        _expect_object(envelope["request"], "request"),
        {"recipient", "message", "correlation_id", "idempotency_key"},
        "request",
    )
    return MessageRequest(
        recipient=_expect_string(request["recipient"], "recipient"),
        message=_parse_message(request["message"]),
        correlation_id=_expect_string(request["correlation_id"], "correlation_id"),
        idempotency_key=_expect_string(request["idempotency_key"], "idempotency_key"),
    )
