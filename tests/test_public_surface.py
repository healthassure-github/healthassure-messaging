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
RELEASE_MANIFEST = Path(".github/release-manifests/v1.0.0.json")
RELEASE_VERIFIER = Path(".github/scripts/verify_frozen_release.py")
RELEASE_WORKFLOW = Path(".github/workflows/release.yml")
PUBLIC_GITHUB_FILES = (
    RELEASE_MANIFEST,
    RELEASE_VERIFIER,
    RELEASE_WORKFLOW,
    SOURCE_VALIDATION_WORKFLOW,
)
PUBLIC_SOURCE_ROOTS = (
    Path("src/healthassure_messaging"),
    Path("tests"),
)
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
        for relative in PUBLIC_TOP_LEVEL_FILES + PUBLIC_GITHUB_FILES
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

    def test_github_tree_is_absent_or_contains_exact_public_controls(self) -> None:
        observed = github_files()
        if (PROJECT_ROOT / ".github").is_dir():
            self.assertEqual(
                observed,
                tuple(sorted(path.as_posix() for path in PUBLIC_GITHUB_FILES)),
            )
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

    def test_release_controls_have_exact_public_security_contract(self) -> None:
        if not (PROJECT_ROOT / RELEASE_WORKFLOW).is_file():
            self.assertEqual(github_files(), ())
            return

        manifest = (PROJECT_ROOT / RELEASE_MANIFEST).read_text(encoding="utf-8")
        verifier = (PROJECT_ROOT / RELEASE_VERIFIER).read_text(encoding="utf-8")
        workflow = (PROJECT_ROOT / RELEASE_WORKFLOW).read_text(encoding="utf-8")

        manifest_requirements = (
            '"artifact_source_commit": "fbc9916ee2b714f0edb29a5e503d0f3f72d223cb"',
            '"distribution": "healthassure-messaging"',
            '"request_schema_version": 1',
            '"tag": "v1.0.0"',
            '"version": "1.0.0"',
            '"size": 42333',
            '"size": 74346',
            "f83d08696d27faa58f75d9f88e844bffd6f5fcb7099acfe1120b4c7b56bf8dc8",
            "8c4bcba46e61fc78f34b01a2d7aea760426fa144cd16376014a85df5ab6b17fa",
        )
        for required in manifest_requirements:
            with self.subTest(manifest_requirement=required):
                self.assertIn(required, manifest)

        verifier_requirements = (
            'EXPECTED_REPOSITORY = "healthassure-github/healthassure-messaging"',
            'EXPECTED_RECOVERY_RELEASE_ID = "380237416"',
            'EXPECTED_RECOVERY_PARENT = "e92773c563ca5d438b25b99e15b8351bc37ee3ce"',
            'EXPECTED_RECOVERY_REF = "refs/heads/main"',
            '".github/workflows/release.yml@refs/heads/main"',
            "RECOVERY_CONTROL_PATHS = (",
            'GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"',
            '"releaseAssets(first: 3) { totalCount nodes { name size digest downloadUrl } } "',
            '("rev-parse", "--verify", "--quiet"',
            '"--no-ext-diff"',
            '"--no-textconv"',
            '"--no-renames"',
            'rb"100644 blob ([0-9a-f]{40})',
            "return super().redirect_request(",
            "MAX_EVENT_BYTES = 1_048_576",
            "MAX_GITHUB_RELEASE_BYTES = 1_048_576",
            "MAX_PYPI_JSON_BYTES = 1_048_576",
            "NETWORK_TIMEOUT_SECONDS = 10",
            "POSTFLIGHT_ATTEMPTS = 6",
            'os.environ.get("RELEASE_GITHUB_TOKEN", "")',
            'or workflow_ref != EXPECTED_RECOVERY_WORKFLOW_REF',
            "except VerificationError as error:",
            'error_code = "verification.internal"',
        )
        for required in verifier_requirements:
            with self.subTest(verifier_requirement=required):
                self.assertIn(required, verifier)

        workflow_requirements = (
            "on:\n  release:\n    types:\n      - published\n  workflow_dispatch:\n"
            "    inputs:\n      release_id:\n"
            "        description: Published GitHub Release database ID\n"
            "        required: true\n        type: string\n",
            "permissions:\n  contents: read\n  id-token: write\n",
            "group: publish-healthassure-messaging-v1.0.0",
            "cancel-in-progress: false",
            "runs-on: ubuntu-24.04",
            "timeout-minutes: 15",
            "name: pypi-production",
            "ref: ${{ github.workflow_sha }}",
            "fetch-depth: 0",
            "persist-credentials: false",
            "RELEASE_WORKFLOW_REF: ${{ github.workflow_ref }}",
            "RELEASE_GITHUB_TOKEN: ${{ github.token }}",
            '--workflow-ref "$RELEASE_WORKFLOW_REF"',
            "if: steps.preflight.outputs.publish_needed == 'true'",
            "timeout-minutes: 5\n        continue-on-error: true",
            "continue-on-error: true",
            "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
            "skip-existing: false",
            "verbose: false",
            "attestations: true",
            "if: always()",
        )
        for required in workflow_requirements:
            with self.subTest(workflow_requirement=required):
                self.assertIn(required, workflow)

        prohibited = (
            "pull_request:",
            "push:",
            "schedule:",
            "repository_dispatch:",
            "secrets.",
            "python -m build",
            "python3 -m build",
            "twine",
            "actions/upload-artifact@",
            "curl ",
            "wget ",
            "skip-existing: true",
            "verbose: true",
        )
        for forbidden in prohibited:
            with self.subTest(release_workflow_forbidden=forbidden):
                self.assertNotIn(forbidden, workflow)
        self.assertEqual(
            workflow.count(
                "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
            ),
            1,
        )
        self.assertEqual(workflow.count("${{ github.token }}"), 2)
        self.assertEqual(workflow.count("uses:"), 2)

    def test_github_inventory_rejects_missing_renamed_or_additional_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for relative in PUBLIC_GITHUB_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("public control\n", encoding="utf-8")
            expected = tuple(sorted(path.as_posix() for path in PUBLIC_GITHUB_FILES))
            self.assertEqual(github_files(root), expected)

            missing = root / RELEASE_MANIFEST
            missing.unlink()
            self.assertNotEqual(github_files(root), expected)
            missing.write_text("public control\n", encoding="utf-8")

            renamed = root / RELEASE_VERIFIER
            replacement = renamed.with_name("renamed.py")
            renamed.rename(replacement)
            self.assertNotEqual(github_files(root), expected)
            replacement.rename(renamed)

            extra = root / ".github" / "unexpected.yml"
            extra.write_text("unexpected control\n", encoding="utf-8")
            self.assertEqual(
                github_files(root),
                tuple(sorted((*expected, ".github/unexpected.yml"))),
            )


if __name__ == "__main__":
    unittest.main()
