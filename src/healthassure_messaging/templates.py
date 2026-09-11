from __future__ import annotations

from collections.abc import Iterable, Mapping

from .contracts import (
    ImageParameter,
    TemplateComponent,
    TemplateMessage,
    TemplateParameter,
    TemplateReference,
    TextParameter,
)
from .enums import TemplateParameterType
from .service_contracts import (
    DuplicateTemplateAliasError,
    ExtraTemplateParameterError,
    MissingTemplateParameterError,
    TemplateAlias,
    TemplateParameterError,
)


class InMemoryTemplateCatalog:
    """Immutable-snapshot template aliases for tests and consumer prototyping."""

    def __init__(self, aliases: Iterable[TemplateAlias]) -> None:
        resolved: dict[str, TemplateAlias] = {}
        for alias in aliases:
            if not isinstance(alias, TemplateAlias):
                raise TypeError("aliases must contain only TemplateAlias values")
            if alias.key in resolved:
                raise DuplicateTemplateAliasError("template alias key is already registered")
            resolved[alias.key] = alias
        self._aliases = resolved

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._aliases)

    def get(self, template_key: str) -> TemplateAlias | None:
        if not isinstance(template_key, str) or not template_key.strip():
            raise ValueError("template_key must be a non-empty string")
        return self._aliases.get(template_key)


def build_template_message(
    alias: TemplateAlias,
    parameters: Mapping[str, str | ImageParameter],
) -> TemplateMessage:
    """Build ordered typed template parameters without inference or reordering."""

    if not isinstance(alias, TemplateAlias):
        raise TypeError("alias must be a TemplateAlias")
    if not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping")

    provided: dict[str, str | ImageParameter] = {}
    for name, value in parameters.items():
        if not isinstance(name, str) or not name.strip():
            raise TemplateParameterError("template parameter names must be non-empty strings")
        if not (
            (isinstance(value, str) and value.strip())
            or isinstance(value, ImageParameter)
        ):
            raise TemplateParameterError("template parameter values must be non-empty strings")
        provided[name] = value

    required = {
        parameter_name
        for component in alias.components
        for parameter_name in component.parameter_names
    }
    supplied = set(provided)
    if required - supplied:
        raise MissingTemplateParameterError("required template parameters are missing")
    if supplied - required:
        raise ExtraTemplateParameterError("unrecognized template parameters were supplied")

    components: list[TemplateComponent] = []
    for component in alias.components:
        built_parameters: list[TemplateParameter] = []
        for parameter_name, parameter_type in zip(
            component.parameter_names,
            component.parameter_types,
            strict=True,
        ):
            value = provided[parameter_name]
            if parameter_type is TemplateParameterType.TEXT:
                if not isinstance(value, str):
                    raise TemplateParameterError("template parameter type is invalid")
                built_parameters.append(TextParameter(text=value))
            else:
                if not isinstance(value, ImageParameter):
                    raise TemplateParameterError("template parameter type is invalid")
                built_parameters.append(value)
        components.append(
            TemplateComponent(
                component_type=component.component_type,
                parameters=tuple(built_parameters),
            )
        )
    return TemplateMessage(
        template=TemplateReference(name=alias.template_name, language_code=alias.language_code),
        components=tuple(components),
    )
