# Changelog

All notable changes to this project are documented in this file.

## 1.0.0.dev5 - 2026-08-29

- Prepare a public source-validation workflow without enabling GitHub Actions.

## 1.0.0.dev4 - 2026-08-29

- Require an explicit artifact version and directory for frozen-artifact qualification.

## 1.0.0.dev3 - 2026-08-28

- Remove trailing blank lines at end of file so root-commit whitespace validation passes.

## 1.0.0.dev2 - 2026-08-28

- Restrict public-source disclosure tests to explicit source, test,
  documentation, and packaging inputs.
- Replace private infrastructure denylist values with generic synthetic
  disclosure signatures.
- Include both `LICENSE` and `NOTICE` in wheel license metadata and contents.

## 1.0.0.dev1 - 2026-08-27

- Establish the provider-neutral `healthassure_messaging` public API.
- Include explicit provider registry and gateway contracts, durable service
  orchestration, Meta Cloud transport and delivery-webhook parsing.
- Include optional caller-owned Mongo persistence with deterministic index plans,
  conditional writes, and bounded stale-dispatch recovery.
- Preserve serialized request schema version `1`.
