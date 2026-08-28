# Contributing

Thank you for helping improve healthassure-messaging.

Before opening a pull request:

1. Create focused tests using synthetic identifiers and reserved fictional
   telephone numbers.
2. Run the complete test suite, Ruff, strict mypy, `compileall`, `tabnanny`, and
   `git diff --check`.
3. Do not add credentials, private endpoints, internal infrastructure names,
   real recipients, or provider response bodies.
4. Keep provider selection explicit. Do not add automatic retries or fallback
   behavior without a separately reviewed contract change.

Security vulnerabilities must follow [SECURITY.md](SECURITY.md), not the public
issue tracker. Contributions are licensed under Apache-2.0.
