from __future__ import annotations

import dataclasses
import unittest
from dataclasses import FrozenInstanceError
from typing import cast

from healthassure_messaging import (
    DispatchResult,
    DuplicateTemplateAliasError,
    ExtraTemplateParameterError,
    InMemoryTemplateCatalog,
    IntentCreationResult,
    IntentState,
    MessageIntent,
    MissingTemplateParameterError,
    RecipientEligibility,
    SendDisposition,
    TemplateAlias,
    TemplateComponentSpec,
    TemplateComponentType,
    build_template_message,
)


class TemplateServiceContractTests(unittest.TestCase):
    def test_aliases_and_component_specs_are_immutable(self) -> None:
        component = TemplateComponentSpec(
            component_type=TemplateComponentType.HEADER,
            parameter_names=("first",),
        )
        alias = TemplateAlias(
            key="synthetic_notice",
            provider_key="primary",
            template_name="synthetic_notice_v1",
            language_code="en_US",
            components=(component,),
        )
        with self.assertRaises(FrozenInstanceError):
            alias.key = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            component.parameter_names = ("changed",)  # type: ignore[misc]

        eligibility = RecipientEligibility(consented=True, suppressed=False)
        with self.assertRaises(FrozenInstanceError):
            eligibility.consented = False  # type: ignore[misc]

    def test_service_contracts_have_no_mutable_defaults(self) -> None:
        contract_types = (
            TemplateComponentSpec,
            TemplateAlias,
            RecipientEligibility,
            DispatchResult,
            MessageIntent,
            IntentCreationResult,
        )
        for contract_type in contract_types:
            with self.subTest(contract=contract_type.__name__):
                for field in dataclasses.fields(contract_type):
                    self.assertNotIsInstance(field.default, (list, dict, set))

    def test_terminal_intent_state_must_match_result_disposition(self) -> None:
        result = DispatchResult(
            intent_id="intent-1",
            provider_key="primary",
            disposition=SendDisposition.ACCEPTED,
            correlation_id="correlation-1",
        )
        with self.assertRaises(ValueError):
            MessageIntent(
                intent_id="intent-1",
                source_flow="synthetic_flow",
                idempotency_key="synthetic-key",
                actor_id="synthetic-actor",
                provider_key="primary",
                correlation_id="correlation-1",
                request_fingerprint="synthetic-fingerprint",
                serialized_request="synthetic-request",
                state=IntentState.REJECTED,
                result=result,
            )

    def test_template_construction_preserves_component_and_parameter_order(self) -> None:
        alias = TemplateAlias(
            key="synthetic_notice",
            provider_key="primary",
            template_name="synthetic_notice_v1",
            language_code="en_US",
            components=(
                TemplateComponentSpec(
                    component_type=TemplateComponentType.HEADER,
                    parameter_names=("header_second", "header_first"),
                ),
                TemplateComponentSpec(
                    component_type=TemplateComponentType.BODY,
                    parameter_names=("body_third", "body_first", "body_second"),
                ),
            ),
        )
        message = build_template_message(
            alias,
            {
                "body_first": "body-1",
                "header_first": "header-1",
                "body_second": "body-2",
                "header_second": "header-2",
                "body_third": "body-3",
            },
        )
        self.assertEqual(
            tuple(component.component_type for component in message.components),
            (TemplateComponentType.HEADER, TemplateComponentType.BODY),
        )
        self.assertEqual(
            tuple(parameter.text for parameter in message.components[0].parameters),
            ("header-2", "header-1"),
        )
        self.assertEqual(
            tuple(parameter.text for parameter in message.components[1].parameters),
            ("body-3", "body-1", "body-2"),
        )

    def test_missing_and_extra_parameters_fail_before_message_construction(self) -> None:
        alias = TemplateAlias(
            key="synthetic_notice",
            provider_key="primary",
            template_name="synthetic_notice_v1",
            language_code="en_US",
            components=(
                TemplateComponentSpec(
                    component_type=TemplateComponentType.BODY,
                    parameter_names=("required",),
                ),
            ),
        )
        with self.assertRaises(MissingTemplateParameterError):
            build_template_message(alias, {})
        with self.assertRaises(ExtraTemplateParameterError):
            build_template_message(alias, {"required": "value", "unexpected": "value"})

    def test_component_free_alias_builds_an_empty_component_tuple(self) -> None:
        alias = TemplateAlias(
            key="synthetic_static",
            provider_key="primary",
            template_name="synthetic_static_v1",
            language_code="en_US",
        )
        self.assertEqual(build_template_message(alias, {}).components, ())

    def test_catalog_rejects_duplicate_alias_keys(self) -> None:
        alias = TemplateAlias(
            key="synthetic_notice",
            provider_key="primary",
            template_name="synthetic_notice_v1",
            language_code="en_US",
        )
        with self.assertRaises(DuplicateTemplateAliasError):
            InMemoryTemplateCatalog((alias, alias))

    def test_button_component_spec_is_not_supported(self) -> None:
        with self.assertRaises(ValueError):
            TemplateComponentSpec(
                component_type=TemplateComponentType.BUTTON,
                parameter_names=("button",),
            )

    def test_component_spec_requires_the_enum_type(self) -> None:
        with self.assertRaises(TypeError):
            TemplateComponentSpec(
                component_type=cast(TemplateComponentType, "header"),
                parameter_names=("value",),
            )


if __name__ == "__main__":
    unittest.main()
