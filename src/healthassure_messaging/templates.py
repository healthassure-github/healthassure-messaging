from __future__ import annotations

from collections.abc import Iterable, Mapping

from .contracts import (
    TemplateComponent,
    TemplateMessage,
    TemplateReference,
    TextParameter,
)
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
    parameters: Mapping[str, str],
) -> TemplateMessage:
    """Build an ordered text-parameter template without inference or reordering."""

    if not isinstance(alias, TemplateAlias):
        raise TypeError("alias must be a TemplateAlias")
    if not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping")

    provided: dict[str, str] = {}
    for name, value in parameters.items():
        if not isinstance(name, str) or not name.strip():
            raise TemplateParameterError("template parameter names must be non-empty strings")
        if not isinstance(value, str) or not value.strip():
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

    components = tuple(
        TemplateComponent(
            component_type=component.component_type,
            parameters=tuple(
                TextParameter(text=provided[parameter_name])
                for parameter_name in component.parameter_names
            ),
        )
        for component in alias.components
    )
    return TemplateMessage(
        template=TemplateReference(name=alias.template_name, language_code=alias.language_code),
        components=components,
    )
