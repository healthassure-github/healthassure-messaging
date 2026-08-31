# healthassure-messaging

`healthassure-messaging` is a typed Python SDK for durable business-messaging
orchestration. It provides provider-neutral requests, explicit provider routing,
consent and session policy ports, idempotent dispatch state, and optional Mongo
persistence. A Meta Cloud API provider and Meta delivery-webhook parser are
included.

Stable version 1.0.0 requires Python 3.10 through 3.13.

## Installation

```bash
python -m pip install healthassure-messaging
```

Install the optional caller-owned Mongo persistence adapter with:

```bash
python -m pip install 'healthassure-messaging[mongo]'
```

## Provider-neutral orchestration

```python
from healthassure_messaging import (
    FakeMessagingProvider,
    MessagingGateway,
    MessagingService,
    ProviderRegistry,
    SendDisposition,
)
```

Applications supply policy, session, template, and intent ports to
`MessagingService`. Provider selection is explicit: the service does not retry
or fall back to another provider automatically. Requests are serialized with
schema version `1` before a claimed dispatch.

The optional `MongoMessagingPersistence` accepts a caller-owned PyMongo
`Database` handle. It never creates a client, loads credentials, reads settings,
or connects during import. Index creation occurs only through an explicit
`ensure_indexes()` call. Policy and session event arrays are intentionally
unbounded in this release; production adopters need an archival or compaction
design. Mongo command construction is covered with deterministic fakes, while
real-server atomicity and index compatibility remain an integration evidence
gate for adopters.

## Supported external platform

The included `MetaCloudProvider` supports outbound text and template requests to
the Meta WhatsApp Business Platform, plus signature verification and delivery
status parsing for Meta webhook payloads. Consumers own credentials,
configuration, consent records, template mappings, routing, and operational
controls. The library makes no provider or database call during import.

Meta and WhatsApp are trademarks of Meta Platforms, Inc. This project is not
affiliated with, sponsored by, or endorsed by Meta Platforms, Inc.

## Security and contributing

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
Development and synthetic-data requirements are in
[CONTRIBUTING.md](CONTRIBUTING.md). Changes are recorded in
[CHANGELOG.md](CHANGELOG.md).

## License

Copyright 2026 HealthAssure. Licensed under the Apache License, Version 2.0.
