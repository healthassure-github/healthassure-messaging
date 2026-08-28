from __future__ import annotations

import unittest

from healthassure_messaging import (
    PhoneNumberError,
    normalize_phone_number,
    validate_e164_number,
)


class PhoneNormalizationTests(unittest.TestCase):
    def test_national_number_uses_injected_region_boundary(self) -> None:
        calls: list[tuple[str, str]] = []

        def normalize_national(value: str, region: str) -> str:
            calls.append((value, region))
            return "+12025550123"

        self.assertEqual(
            normalize_phone_number(
                "1111111111",
                default_region="in",
                national_number_normalizer=normalize_national,
            ),
            "+12025550123",
        )
        self.assertEqual(calls, [("1111111111", "IN")])

    def test_already_prefixed_international_number(self) -> None:
        self.assertEqual(normalize_phone_number("+12025550123"), "+12025550123")
        self.assertEqual(normalize_phone_number("0012025550123"), "+12025550123")

    def test_separators_are_removed(self) -> None:
        self.assertEqual(
            normalize_phone_number("+1 (202) 555-0123"),
            "+12025550123",
        )

    def test_national_number_without_region_is_rejected(self) -> None:
        with self.assertRaises(PhoneNumberError):
            normalize_phone_number("2025550123")

    def test_bare_country_prefixed_number_is_rejected_as_ambiguous(self) -> None:
        with self.assertRaises(PhoneNumberError):
            normalize_phone_number("12025550123", default_region="IN")

    def test_invalid_inputs_are_rejected_without_echoing_the_number(self) -> None:
        invalid_values = (
            "",
            "phone:2025550123",
            "+012025550123",
            "1234567890",
            "001",
            "++12025550123",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(PhoneNumberError) as context:
                    normalize_phone_number(value, default_region="IN")
                if value:
                    self.assertNotIn(value, str(context.exception))

    def test_unsupported_national_region_is_explicit(self) -> None:
        with self.assertRaises(PhoneNumberError):
            normalize_phone_number("2025550123", default_region="US")

    def test_request_level_e164_validation_rejects_unnormalized_values(self) -> None:
        with self.assertRaises(PhoneNumberError):
            validate_e164_number("1 202 555 0123")


if __name__ == "__main__":
    unittest.main()
