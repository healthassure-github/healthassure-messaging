from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import requests
from requests.adapters import HTTPAdapter

from ._enum import _StringEnum

DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024


class TransportFailureKind(_StringEnum):
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes
    retry_after: str | None = None
    body_too_large: bool = False

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be an integer between 100 and 599")
        if not isinstance(self.body, bytes):
            raise TypeError("body must be bytes")
        if self.retry_after is not None and not isinstance(self.retry_after, str):
            raise TypeError("retry_after must be a string or None")
        if type(self.body_too_large) is not bool:
            raise TypeError("body_too_large must be a boolean")


@dataclass(frozen=True, slots=True)
class TransportFailure:
    kind: TransportFailureKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TransportFailureKind):
            raise TypeError("kind must be a TransportFailureKind")


HttpOutcome = HttpResponse | TransportFailure


class HttpTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
        timeout: tuple[float, float],
    ) -> HttpOutcome: ...


class RequestsHttpTransport:
    """Small requests transport for provider JSON calls with bounded responses."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._session.mount("https://", HTTPAdapter(max_retries=0))
        self._max_response_bytes = max_response_bytes

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
        timeout: tuple[float, float],
    ) -> HttpOutcome:
        if not url.startswith("https://"):
            raise ValueError("provider URL must use HTTPS")

        try:
            response = self._session.post(
                url,
                headers=dict(headers),
                json=dict(json_body),
                timeout=timeout,
                allow_redirects=False,
                verify=True,
                stream=True,
            )
            try:
                body = bytearray()
                body_too_large = False
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    if len(body) + len(chunk) > self._max_response_bytes:
                        body_too_large = True
                        body.clear()
                        break
                    body.extend(chunk)
                return HttpResponse(
                    status_code=response.status_code,
                    body=bytes(body),
                    retry_after=self._bounded_retry_after(response.headers.get("Retry-After")),
                    body_too_large=body_too_large,
                )
            finally:
                response.close()
        except requests.Timeout:
            return TransportFailure(TransportFailureKind.TIMEOUT)
        except requests.RequestException:
            return TransportFailure(TransportFailureKind.NETWORK)

    @staticmethod
    def _bounded_retry_after(value: str | None) -> str | None:
        if value is None or len(value) > 128:
            return None
        return value
