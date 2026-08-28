from __future__ import annotations

import unittest
from collections.abc import Iterator
from unittest.mock import patch

import requests
from requests.adapters import HTTPAdapter

from healthassure_messaging.http import (
    HttpResponse,
    RequestsHttpTransport,
    TransportFailure,
    TransportFailureKind,
)


class _SyntheticResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        chunks: tuple[bytes, ...] = (b'{"messages":[{"id":"synthetic-id"}]}',),
        retry_after: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}
        self._chunks = chunks
        self.closed = False

    def iter_content(self, *, chunk_size: int) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise AssertionError("chunk_size must be positive")
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class RequestsHttpTransportTests(unittest.TestCase):
    def test_post_uses_secure_explicit_settings_and_has_no_retry(self) -> None:
        session = requests.Session()
        response = _SyntheticResponse(retry_after="7")
        transport = RequestsHttpTransport(session=session)

        with patch.object(session, "post", return_value=response) as post:
            outcome = transport.post_json(
                url="https://graph.facebook.com/v999.0/999999999/messages",
                headers={"Authorization": "Bearer synthetic-placeholder"},
                json_body={"type": "text"},
                timeout=(1.5, 4.0),
            )

        self.assertEqual(
            outcome,
            HttpResponse(
                status_code=200,
                body=b'{"messages":[{"id":"synthetic-id"}]}',
                retry_after="7",
            ),
        )
        post.assert_called_once_with(
            "https://graph.facebook.com/v999.0/999999999/messages",
            headers={"Authorization": "Bearer synthetic-placeholder"},
            json={"type": "text"},
            timeout=(1.5, 4.0),
            allow_redirects=False,
            verify=True,
            stream=True,
        )
        self.assertFalse(session.trust_env)
        adapter = session.get_adapter("https://")
        self.assertIsInstance(adapter, HTTPAdapter)
        assert isinstance(adapter, HTTPAdapter)
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertTrue(response.closed)

    def test_timeout_and_connection_errors_become_sanitized_typed_failures(self) -> None:
        exceptions = (
            (requests.Timeout("sensitive prepared request"), TransportFailureKind.TIMEOUT),
            (
                requests.ConnectionError("sensitive prepared request"),
                TransportFailureKind.NETWORK,
            ),
        )
        for exception, expected_kind in exceptions:
            with self.subTest(kind=expected_kind):
                session = requests.Session()
                transport = RequestsHttpTransport(session=session)
                with patch.object(session, "post", side_effect=exception) as post:
                    outcome = transport.post_json(
                        url="https://graph.facebook.com/v999.0/999999999/messages",
                        headers={"Authorization": "Bearer synthetic-placeholder"},
                        json_body={"type": "text"},
                        timeout=(1.0, 2.0),
                    )
                self.assertEqual(outcome, TransportFailure(expected_kind))
                self.assertNotIn("sensitive", repr(outcome))
                post.assert_called_once()

    def test_oversized_response_is_bounded_and_closed(self) -> None:
        session = requests.Session()
        response = _SyntheticResponse(chunks=(b"1234", b"5678"))
        transport = RequestsHttpTransport(session=session, max_response_bytes=7)

        with patch.object(session, "post", return_value=response):
            outcome = transport.post_json(
                url="https://graph.facebook.com/v999.0/999999999/messages",
                headers={},
                json_body={},
                timeout=(1.0, 2.0),
            )

        self.assertEqual(
            outcome,
            HttpResponse(status_code=200, body=b"", body_too_large=True),
        )
        self.assertTrue(response.closed)

    def test_oversized_retry_after_header_is_discarded(self) -> None:
        session = requests.Session()
        response = _SyntheticResponse(retry_after="9" * 129)
        transport = RequestsHttpTransport(session=session)
        with patch.object(session, "post", return_value=response):
            outcome = transport.post_json(
                url="https://graph.facebook.com/v999.0/999999999/messages",
                headers={},
                json_body={},
                timeout=(1.0, 2.0),
            )
        self.assertIsInstance(outcome, HttpResponse)
        assert isinstance(outcome, HttpResponse)
        self.assertIsNone(outcome.retry_after)

    def test_non_https_url_is_rejected_without_echoing_it(self) -> None:
        transport = RequestsHttpTransport(session=requests.Session())
        with self.assertRaises(ValueError) as context:
            transport.post_json(
                url="http://unsafe.invalid/sensitive-identifier",
                headers={},
                json_body={},
                timeout=(1.0, 2.0),
            )
        self.assertNotIn("sensitive-identifier", str(context.exception))


if __name__ == "__main__":
    unittest.main()
