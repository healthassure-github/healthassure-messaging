from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from healthassure_messaging.contracts import (
    MessageRequest,
    NormalizedError,
    SendResult,
    TextMessage,
)
from healthassure_messaging.enums import (
    ErrorCategory,
    SendDisposition,
    TemplateComponentType,
)
from healthassure_messaging.http import (
    HttpResponse,
    HttpTransport,
    RequestsHttpTransport,
    TransportFailure,
    TransportFailureKind,
)

_GRAPH_HOST = "https://graph.facebook.com"
_GRAPH_VERSION_PATTERN = re.compile(r"v[1-9][0-9]*\.[0-9]+")
_PHONE_NUMBER_ID_PATTERN = re.compile(r"[1-9][0-9]*")
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_RETRY_AFTER_SECONDS = 86_400


def _validate_timeout(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    if not math.isfinite(value) or value <= 0 or value > _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"{field_name} must be greater than 0 and at most 300 seconds")


@dataclass(frozen=True, slots=True)
class MetaCloudConfig:
    graph_version: str
    phone_number_id: str = field(repr=False)
    access_token: str = field(repr=False)
    connect_timeout: float
    read_timeout: float

    def __post_init__(self) -> None:
        if not isinstance(self.graph_version, str) or not _GRAPH_VERSION_PATTERN.fullmatch(
            self.graph_version
        ):
            raise ValueError("graph_version must match v<major>.<minor>")
        if not isinstance(self.phone_number_id, str) or not _PHONE_NUMBER_ID_PATTERN.fullmatch(
            self.phone_number_id
        ):
            raise ValueError("phone_number_id must be a non-empty decimal identifier")
        if not isinstance(self.access_token, str) or not self.access_token.strip():
            raise ValueError("access_token must be non-empty")
        _validate_timeout(self.connect_timeout, "connect_timeout")
        _validate_timeout(self.read_timeout, "read_timeout")


class MetaCloudProvider:
    key = "meta"

    def __init__(
        self,
        config: MetaCloudConfig,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        if not isinstance(config, MetaCloudConfig):
            raise TypeError("config must be a MetaCloudConfig")
        self._config = config
        self._transport = transport or RequestsHttpTransport()

    def send(self, request: MessageRequest) -> SendResult:
        if not isinstance(request, MessageRequest):
            raise TypeError("request must be a MessageRequest")

        payload = self._build_payload(request)
        if payload is None:
            return self._result(
                request,
                disposition=SendDisposition.REJECTED,
                error=NormalizedError(
                    category=ErrorCategory.UNSUPPORTED,
                    safe_message="The Meta adapter does not support this template component.",
                    retriable=False,
                    unknown_outcome=False,
                ),
            )

        outcome = self._transport.post_json(
            url=(
                f"{_GRAPH_HOST}/{self._config.graph_version}/"
                f"{self._config.phone_number_id}/messages"
            ),
            headers={
                "Authorization": f"Bearer {self._config.access_token}",
                "Content-Type": "application/json",
            },
            json_body=payload,
            timeout=(self._config.connect_timeout, self._config.read_timeout),
        )
        if isinstance(outcome, TransportFailure):
            return self._transport_failure_result(request, outcome)
        return self._response_result(request, outcome)

    @staticmethod
    def _build_payload(request: MessageRequest) -> dict[str, object] | None:
        recipient = request.recipient.removeprefix("+")
        common: dict[str, object] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
        }
        if isinstance(request.message, TextMessage):
            return {
                **common,
                "type": "text",
                "text": {"body": request.message.body, "preview_url": False},
            }

        if any(
            component.component_type is TemplateComponentType.BUTTON
            for component in request.message.components
        ):
            return None

        template_payload: dict[str, object] = {
            "name": request.message.template.name,
            "language": {"code": request.message.template.language_code},
        }
        if request.message.components:
            template_payload["components"] = [
                {
                    "type": component.component_type.value.lower(),
                    "parameters": [
                        {"type": "text", "text": parameter.text}
                        for parameter in component.parameters
                    ],
                }
                for component in request.message.components
            ]
        return {**common, "type": "template", "template": template_payload}

    def _transport_failure_result(
        self,
        request: MessageRequest,
        failure: TransportFailure,
    ) -> SendResult:
        if failure.kind is TransportFailureKind.TIMEOUT:
            category = ErrorCategory.TIMEOUT
            message = "The Meta request timed out after dispatch may have begun."
        else:
            category = ErrorCategory.NETWORK
            message = "The Meta connection failed after dispatch may have begun."
        return self._result(
            request,
            disposition=SendDisposition.UNKNOWN,
            error=NormalizedError(
                category=category,
                safe_message=message,
                retriable=False,
                unknown_outcome=True,
            ),
        )

    def _response_result(self, request: MessageRequest, response: HttpResponse) -> SendResult:
        if 200 <= response.status_code < 300:
            return self._success_result(request, response)
        return self._rejection_result(request, response)

    def _success_result(self, request: MessageRequest, response: HttpResponse) -> SendResult:
        if response.body_too_large:
            return self._protocol_unknown_result(request, response.status_code)
        document = self._decode_json_object(response.body)
        if document is None:
            return self._protocol_unknown_result(request, response.status_code)
        messages = document.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._protocol_unknown_result(request, response.status_code)
        first_message = messages[0]
        if not isinstance(first_message, Mapping):
            return self._protocol_unknown_result(request, response.status_code)
        provider_message_id = first_message.get("id")
        if not isinstance(provider_message_id, str) or not provider_message_id.strip():
            return self._protocol_unknown_result(request, response.status_code)
        return self._result(
            request,
            disposition=SendDisposition.ACCEPTED,
            provider_message_id=provider_message_id,
            provider_status="accepted",
        )

    def _rejection_result(self, request: MessageRequest, response: HttpResponse) -> SendResult:
        category, retriable = self._classify_rejection(response.status_code)
        provider_code: str | None = None
        provider_subcode: int | None = None
        if not response.body_too_large:
            document = self._decode_json_object(response.body)
            error = document.get("error") if document is not None else None
            if isinstance(error, Mapping):
                code = error.get("code")
                if type(code) is int and 0 <= code <= 2_147_483_647:
                    provider_code = str(code)
                subcode = error.get("error_subcode")
                if type(subcode) is int and 0 <= subcode <= 2_147_483_647:
                    provider_subcode = subcode
        if 500 <= response.status_code <= 599 and provider_code is None:
            return self._unverified_server_error_result(request, response.status_code)
        return self._result(
            request,
            disposition=SendDisposition.REJECTED,
            error=NormalizedError(
                category=category,
                safe_message="Meta rejected the message request.",
                retriable=retriable,
                unknown_outcome=False,
                provider_code=provider_code,
                http_status=response.status_code,
                provider_subcode=provider_subcode,
                retry_after_seconds=self._parse_retry_after(response.retry_after),
            ),
        )

    @staticmethod
    def _decode_json_object(body: bytes) -> dict[str, object] | None:
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return document if isinstance(document, dict) else None

    @staticmethod
    def _classify_rejection(status_code: int) -> tuple[ErrorCategory, bool]:
        if status_code == 400:
            return ErrorCategory.VALIDATION, False
        if status_code == 401:
            return ErrorCategory.AUTHENTICATION, False
        if status_code == 403:
            return ErrorCategory.AUTHORIZATION, False
        if status_code == 429:
            return ErrorCategory.RATE_LIMIT, True
        if 500 <= status_code <= 599:
            return ErrorCategory.PROVIDER_TEMPORARY, True
        if 400 <= status_code <= 499:
            return ErrorCategory.PROVIDER_PERMANENT, False
        return ErrorCategory.PROTOCOL, False

    @staticmethod
    def _parse_retry_after(value: str | None) -> int | None:
        if value is None or not value.isascii() or not value.isdecimal():
            return None
        seconds = int(value)
        return seconds if seconds <= _MAX_RETRY_AFTER_SECONDS else None

    def _protocol_unknown_result(self, request: MessageRequest, status_code: int) -> SendResult:
        return self._result(
            request,
            disposition=SendDisposition.UNKNOWN,
            error=NormalizedError(
                category=ErrorCategory.PROTOCOL,
                safe_message="Meta returned an unexpected success response.",
                retriable=False,
                unknown_outcome=True,
                http_status=status_code,
            ),
        )

    def _unverified_server_error_result(
        self,
        request: MessageRequest,
        status_code: int,
    ) -> SendResult:
        return self._result(
            request,
            disposition=SendDisposition.UNKNOWN,
            error=NormalizedError(
                category=ErrorCategory.PROTOCOL,
                safe_message="Meta returned an unverified server-error response.",
                retriable=False,
                unknown_outcome=True,
                http_status=status_code,
            ),
        )

    def _result(
        self,
        request: MessageRequest,
        *,
        disposition: SendDisposition,
        provider_message_id: str | None = None,
        provider_status: str | None = None,
        error: NormalizedError | None = None,
    ) -> SendResult:
        return SendResult(
            provider_key=self.key,
            disposition=disposition,
            correlation_id=request.correlation_id,
            provider_message_id=provider_message_id,
            provider_status=provider_status,
            error=error,
        )
