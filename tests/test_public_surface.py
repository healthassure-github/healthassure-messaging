from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from pathlib import Path

import healthassure_messaging

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_TERM = "what" + "sapp"
PLATFORM_ALLOWLIST = {
    "NOTICE",
    "README.md",
    "src/healthassure_messaging/providers/meta.py",
    "src/healthassure_messaging/providers/meta_webhooks.py",
    "tests/test_equivalence.py",
    "tests/test_meta.py",
    "tests/test_meta_webhooks.py",
}
SOURCE_VALIDATION_WORKFLOW = Path(".github/workflows/source-validation.yml")
SOURCE_VALIDATION_WORKFLOW_SHA256 = (
    "fe6157b99c5074a6c02bf196f71b2df2c373cab9fd080323f1ed31096a097820"
)
PUBLIC_SOURCE_ROOTS = (
    Path("src/healthassure_messaging"),
    Path("tests"),
)
PUBLIC_WORKFLOW_FILES = (SOURCE_VALIDATION_WORKFLOW,)
PUBLIC_TOP_LEVEL_FILES = (
    Path(".gitignore"),
    Path("CHANGELOG.md"),
    Path("CONTRIBUTING.md"),
    Path("LICENSE"),
    Path("MANIFEST.in"),
    Path("NOTICE"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("pyproject.toml"),
)
PUBLIC_TEXT_SUFFIXES = {"", ".in", ".md", ".py", ".toml", ".typed", ".yml"}


def public_source_files(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    selected = [
        project_root / relative
        for relative in PUBLIC_TOP_LEVEL_FILES + PUBLIC_WORKFLOW_FILES
        if (project_root / relative).is_file()
    ]
    for relative_root in PUBLIC_SOURCE_ROOTS:
        source_root = project_root / relative_root
        if not source_root.is_dir():
            continue
        selected.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file() and path.suffix in PUBLIC_TEXT_SUFFIXES
        )
    return tuple(sorted(selected))


def github_files(project_root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    github_root = project_root / ".github"
    if not github_root.is_dir():
        return ()
    return tuple(
        sorted(
            path.relative_to(project_root).as_posix()
            for path in github_root.rglob("*")
            if path.is_file()
        )
    )


DISCLOSURE_PATTERNS = (
    ("gar-host", re.compile(r"\b[a-z0-9.-]+-python[.]pkg[.]dev\b", re.IGNORECASE)),
    (
        "workload-identity-pool",
        re.compile("workload" + r"IdentityPools/[A-Za-z0-9._~/-]+"),
    ),
    (
        "service-account-address",
        re.compile(
            r"\b[a-z][a-z0-9-]*@[a-z][a-z0-9-]*[.]iam[.]gserviceaccount[.]com\b",
            re.IGNORECASE,
        ),
    ),
    (
        "embedded-authentication-url",
        re.compile(r"https?://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
    ),
    ("private-key", re.compile("-----BEGIN " + r"(?:EC |RSA )?PRIVATE KEY-----")),
    ("oauth-token", re.compile(r"\bya" + r"29[.][A-Za-z0-9_-]+\b")),
    ("github-token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    (
        "email-address",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}\b"),
    ),
    ("plausible-indian-recipient", re.compile(r"[+]91[0-9]{10}\b")),
)


def disclosure_categories(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in DISCLOSURE_PATTERNS if pattern.search(text))


def public_source_disclosures(project_root: Path = PROJECT_ROOT) -> tuple[tuple[str, str], ...]:
    findings: list[tuple[str, str]] = []
    for path in public_source_files(project_root):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(project_root).as_posix()
        findings.extend((relative, category) for category in disclosure_categories(text))
    return tuple(findings)


class PublicSurfaceTests(unittest.TestCase):
    def test_legacy_distribution_namespace_and_generic_names_are_absent(self) -> None:
        legacy_fragments = (
            "healthassure_" + PLATFORM_TERM,
            "healthassure-" + PLATFORM_TERM,
            "Whats" + "AppRequest",
            "Whats" + "AppProvider",
            "Whats" + "AppGateway",
            "FakeWhats" + "AppProvider",
            "Whats" + "AppService",
            "Whats" + "AppDispatchResult",
            "Whats" + "AppIntent",
            "Whats" + "AppServiceError",
            "MongoWhats" + "AppPersistence",
        )
        for path in public_source_files():
            text = path.read_text(encoding="utf-8")
            for fragment in legacy_fragments:
                with self.subTest(path=path, fragment=fragment):
                    self.assertNotIn(fragment, text)

        for exported_name in healthassure_messaging.__all__:
            self.assertNotIn("Whats" + "App", exported_name)

    def test_external_platform_term_occurs_only_in_reviewed_files(self) -> None:
        observed: set[str] = set()
        for path in public_source_files():
            if PLATFORM_TERM in path.read_text(encoding="utf-8").lower():
                observed.add(path.relative_to(PROJECT_ROOT).as_posix())
        self.assertEqual(observed, PLATFORM_ALLOWLIST)

    def test_public_sources_have_no_generic_disclosure_signatures(self) -> None:
        self.assertEqual(public_source_disclosures(), ())

    def test_generic_synthetic_infrastructure_examples_are_detected(self) -> None:
        examples = {
            "gar-host": "https://" + "example-region-python" + "." + "pkg" + "." + "dev/simple/",
            "workload-identity-pool": (
                "projects/123/locations/global/"
                + "workload"
                + "IdentityPools/example/providers/demo"
            ),
            "service-account-address": "reader@" + "example-project.iam." + "gserviceaccount.com",
            "embedded-authentication-url": (
                "https://" + "user:password@" + "packages.example.invalid/simple/"
            ),
            "private-key": "-----BEGIN " + "PRIVATE KEY-----",
            "oauth-token": "ya" + "29.synthetic-token-value",
            "github-token": "ghp" + "_abcdefghijklmnopqrstuvwxyz123456",
            "email-address": "security@" + "example.invalid",
            "plausible-indian-recipient": "+" + "919876543210",
        }
        for expected, value in examples.items():
            with self.subTest(category=expected):
                self.assertIn(expected, disclosure_categories(value))

    def test_forbidden_value_in_real_source_input_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "src" / "healthassure_messaging"
            source.mkdir(parents=True)
            unsafe = "https://" + "example-region-python" + "." + "pkg" + "." + "dev/simple/"
            (source / "unsafe.py").write_text(f'ENDPOINT = "{unsafe}"\n', encoding="utf-8")
            self.assertEqual(
                public_source_disclosures(root),
                (("src/healthassure_messaging/unsafe.py", "gar-host"),),
            )

    def test_generated_outputs_do_not_enter_public_source_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            package = root / "src" / "healthassure_messaging"
            package.mkdir(parents=True)
            (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_module.py").write_text("VALUE = 1\n", encoding="utf-8")
            generated = (
                root / "dist" / "synthetic.whl",
                root / "build" / "generated.py",
                root / "src" / "healthassure_messaging.egg-info" / "PKG-INFO",
                root / ".mypy_cache" / "metadata.json",
                root / ".git" / "config",
            )
            for path in generated:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("generated output\n", encoding="utf-8")
            self.assertEqual(
                tuple(path.relative_to(root).as_posix() for path in public_source_files(root)),
                ("src/healthassure_messaging/module.py", "tests/test_module.py"),
            )

    def test_github_tree_is_absent_or_contains_only_source_validation(self) -> None:
        observed = github_files()
        if (PROJECT_ROOT / SOURCE_VALIDATION_WORKFLOW).is_file():
            self.assertEqual(observed, (SOURCE_VALIDATION_WORKFLOW.as_posix(),))
        else:
            self.assertEqual(observed, ())
        self.assertFalse((PROJECT_ROOT / "RELEASING.md").exists())

    def test_source_validation_workflow_has_exact_public_security_contract(self) -> None:
        workflow = PROJECT_ROOT / SOURCE_VALIDATION_WORKFLOW
        if not workflow.is_file():
            self.assertEqual(github_files(), ())
            return

        content = workflow.read_text(encoding="utf-8")
        self.assertEqual(
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            SOURCE_VALIDATION_WORKFLOW_SHA256,
        )
        required_blocks = (
            "on:\n  pull_request:\n  push:\n    branches:\n      - main\n  workflow_dispatch:\n",
            "permissions:\n  contents: read\n",
            "runs-on: ubuntu-24.04\n    timeout-minutes: 10",
            "fail-fast: false\n      matrix:\n        python-version:\n"
            '          - "3.10"\n          - "3.11"\n          - "3.12"\n          - "3.13"',
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
            "persist-credentials: false",
            "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0",
            "requests==2.32.3",
            "pymongo==4.11.1",
            "ruff==0.16.4",
            "mypy==2.3.1",
            "types-requests==2.33.0.20260712",
            "PYTHONPATH: ${{ github.workspace }}/src",
            "python -m unittest discover -s tests",
            "ruff check --no-cache .",
            "mypy --strict\n          --no-incremental",
            'python-version "${{ matrix.python-version }}"',
            "python -m compileall -q src tests",
            "python -m tabnanny src tests",
            'subprocess.check_output(("git", "ls-files", "-z"))',
            "git diff --check",
            'test -z "$(git status --porcelain=v1 --untracked-files=all)"',
        )
        for required in required_blocks:
            with self.subTest(required=required):
                self.assertIn(required, content)

        prohibited = (
            "secrets.",
            "id-token:",
            "environment:",
            "actions/cache@",
            "actions/upload-artifact@",
            "python -m build",
            "twine",
            "docker",
            "pkg.dev",
            "extra-index-url",
            "curl ",
            "wget ",
            "pull_request_target:",
            "schedule:",
            "repository_dispatch:",
            "release:",
        )
        for forbidden in prohibited:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, content)

        self.assertEqual(content.count("uses:"), 2)
        self.assertEqual(content.count("permissions:"), 1)

    def test_github_inventory_rejects_any_additional_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workflow = root / SOURCE_VALIDATION_WORKFLOW
            workflow.parent.mkdir(parents=True)
            workflow.write_text("source validation\n", encoding="utf-8")
            self.assertEqual(github_files(root), (SOURCE_VALIDATION_WORKFLOW.as_posix(),))

            extra = root / ".github" / "release.yml"
            extra.write_text("release automation\n", encoding="utf-8")
            self.assertEqual(
                github_files(root),
                (".github/release.yml", SOURCE_VALIDATION_WORKFLOW.as_posix()),
            )


if __name__ == "__main__":
    unittest.main()
