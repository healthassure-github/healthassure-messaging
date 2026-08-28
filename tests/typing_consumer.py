from __future__ import annotations

from collections.abc import Mapping

from healthassure_messaging import (
    DispatchResult,
    FakeMessagingProvider,
    MessagingProvider,
    MessagingService,
    SendDisposition,
)

provider: MessagingProvider = FakeMessagingProvider(
    key="synthetic",
    disposition=SendDisposition.ACCEPTED,
)


def send_template(
    service: MessagingService,
    parameters: Mapping[str, str],
) -> DispatchResult:
    return service.send_template(
        recipient="+12025550127",
        template_key="synthetic-template",
        parameters=parameters,
        source_flow="synthetic-flow",
        idempotency_key="synthetic-idempotency",
        actor_id="synthetic-actor",
    )
