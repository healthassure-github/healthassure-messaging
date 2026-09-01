from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = PROJECT_ROOT / ".github" / "scripts" / "verify_frozen_release.py"
VERIFIER_AVAILABLE = VERIFIER_PATH.is_file()

verifier: Any = None
if VERIFIER_AVAILABLE:
    specification = importlib.util.spec_from_file_location(
        "healthassure_messaging_release_verifier", VERIFIER_PATH
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("release verifier import failed")
    verifier = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = verifier
    specification.loader.exec_module(verifier)


def _metadata() -> bytes:
    return (
        b"Metadata-Version: 2.4\n"
        b"Name: healthassure-messaging\n"
        b"Version: 1.0.0\n"
        b"License-Expression: Apache-2.0\n"
        b"License-File: LICENSE\n"
        b"License-File: NOTICE\n"
        b"Requires-Python: <3.14,>=3.10\n"
        b"Requires-Dist: requests<3,>=2.32.3\n"
        b"Provides-Extra: mongo\n"
        b'Requires-Dist: pymongo<5,>=4.11.1; extra == "mongo"\n'
        b"Provides-Extra: dev\n"
        b'Requires-Dist: build<2,>=1.5; extra == "dev"\n'
        b'Requires-Dist: check-wheel-contents<1,>=0.6; extra == "dev"\n'
        b'Requires-Dist: mypy<3,>=2.3; extra == "dev"\n'
        b'Requires-Dist: ruff<0.17,>=0.16; extra == "dev"\n'
        b'Requires-Dist: twine<7,>=6.2; extra == "dev"\n'
        b'Requires-Dist: types-requests<3,>=2.33.0.20260712; extra == "dev"\n'
        b"\n"
    )


def _wheel_bytes(
    *,
    metadata: bytes | None = None,
    schema: int = 1,
    include_typed: bool = True,
    include_notice: bool = True,
    disclosure: str | None = None,
    symlink: bool = False,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "healthassure_messaging/serialization.py",
            f"REQUEST_SCHEMA_VERSION = {schema}\n",
        )
        if include_typed:
            archive.writestr("healthassure_messaging/py.typed", "")
        if disclosure is not None:
            archive.writestr("healthassure_messaging/unsafe.py", disclosure)
        root = "healthassure_messaging-1.0.0.dist-info/"
        archive.writestr(root + "METADATA", metadata or _metadata())
        archive.writestr(root + "licenses/LICENSE", "license\n")
        if include_notice:
            archive.writestr(root + "licenses/NOTICE", "notice\n")
        if symlink:
            information = zipfile.ZipInfo("healthassure_messaging/link.py")
            information.create_system = 3
            information.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(information, "serialization.py")
    return output.getvalue()


def _sdist_bytes(
    *,
    metadata: bytes | None = None,
    schema: int = 1,
    include_typed: bool = True,
    include_notice: bool = True,
    disclosure: str | None = None,
) -> bytes:
    output = io.BytesIO()
    root = "healthassure_messaging-1.0.0"
    entries: dict[str, bytes] = {
        f"{root}/PKG-INFO": metadata or _metadata(),
        f"{root}/LICENSE": b"license\n",
        f"{root}/src/healthassure_messaging/serialization.py": (
            f"REQUEST_SCHEMA_VERSION = {schema}\n".encode()
        ),
    }
    if include_notice:
        entries[f"{root}/NOTICE"] = b"notice\n"
    if include_typed:
        entries[f"{root}/src/healthassure_messaging/py.typed"] = b""
    if disclosure is not None:
        entries[f"{root}/src/healthassure_messaging/unsafe.py"] = disclosure.encode()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in entries.items():
            information = tarfile.TarInfo(name)
            information.size = len(content)
            archive.addfile(information, io.BytesIO(content))
    return output.getvalue()


def _manifest(
    wheel: bytes | None = None,
    sdist: bytes | None = None,
    *,
    source_commit: str = "1" * 40,
) -> Any:
    wheel_content = wheel if wheel is not None else _wheel_bytes()
    sdist_content = sdist if sdist is not None else _sdist_bytes()
    artifacts = (
        verifier.ArtifactSpec(
            "wheel",
            "healthassure_messaging-1.0.0-py3-none-any.whl",
            len(wheel_content),
            hashlib.sha256(wheel_content).hexdigest(),
        ),
        verifier.ArtifactSpec(
            "sdist",
            "healthassure_messaging-1.0.0.tar.gz",
            len(sdist_content),
            hashlib.sha256(sdist_content).hexdigest(),
        ),
    )
    return verifier.ReleaseManifest(
        repository="healthassure-github/healthassure-messaging",
        distribution="healthassure-messaging",
        import_package="healthassure_messaging",
        version="1.0.0",
        tag="v1.0.0",
        artifact_source_commit=source_commit,
        request_schema_version=1,
        python_requires=">=3.10,<3.14",
        runtime_dependencies=("requests>=2.32.3,<3",),
        optional_dependencies={
            "dev": (
                "build>=1.5,<2",
                "check-wheel-contents>=0.6,<1",
                "mypy>=2.3,<3",
                "ruff>=0.16,<0.17",
                "twine>=6.2,<7",
                "types-requests>=2.33.0.20260712,<3",
            ),
            "mongo": ("pymongo>=4.11.1,<5",),
        },
        artifacts=artifacts,
    )


def _release_url(filename: str) -> str:
    return (
        "https://github.com/healthassure-github/healthassure-messaging/"
        f"releases/download/v1.0.0/{filename}"
    )


def _release_payload(manifest: Any) -> dict[str, object]:
    return {
        "action": "published",
        "repository": {"full_name": manifest.repository},
        "release": {
            "draft": False,
            "prerelease": False,
            "tag_name": manifest.tag,
            "published_at": "2026-08-31T00:00:00Z",
            "assets": [
                {
                    "name": artifact.filename,
                    "size": artifact.size,
                    "state": "uploaded",
                    "browser_download_url": _release_url(artifact.filename),
                }
                for artifact in manifest.artifacts
            ],
        },
    }


def _recovery_event_payload(release_id: object = "380237416") -> dict[str, object]:
    return {
        "inputs": {"release_id": release_id},
        "repository": {"full_name": "healthassure-github/healthassure-messaging"},
    }


def _github_release_payload(
    manifest: Any,
    *,
    tag_commit: str = "e92773c563ca5d438b25b99e15b8351bc37ee3ce",
) -> bytes:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "release": {
                        "databaseId": 380237416,
                        "isDraft": False,
                        "isPrerelease": False,
                        "publishedAt": "2026-09-01T00:00:00Z",
                        "tagName": manifest.tag,
                        "tagCommit": {"oid": tag_commit},
                        "releaseAssets": {
                            "totalCount": len(manifest.artifacts),
                            "nodes": [
                                {
                                    "name": artifact.filename,
                                    "size": artifact.size,
                                    "digest": f"sha256:{artifact.sha256}",
                                    "downloadUrl": _release_url(artifact.filename),
                                }
                                for artifact in manifest.artifacts
                            ],
                        },
                    }
                }
            }
        }
    ).encode()


def _write_event(test_case: unittest.TestCase, payload: object) -> Path:
    temporary = tempfile.TemporaryDirectory()
    test_case.addCleanup(temporary.cleanup)
    path = Path(temporary.name) / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _pypi_url(filename: str) -> str:
    return f"https://files.pythonhosted.org/packages/synthetic/{filename}"


def _pypi_payload(manifest: Any) -> bytes:
    return json.dumps(
        {
            "urls": [
                {
                    "filename": artifact.filename,
                    "size": artifact.size,
                    "digests": {"sha256": artifact.sha256},
                    "packagetype": "bdist_wheel" if artifact.kind == "wheel" else "sdist",
                    "url": _pypi_url(artifact.filename),
                    "yanked": False,
                }
                for artifact in manifest.artifacts
            ]
        }
    ).encode()


class FakeFetcher:
    def __init__(self, responses: dict[str, list[object]]) -> None:
        self.responses = {url: list(values) for url, values in responses.items()}
        self.calls: list[tuple[str, int, bool]] = []

    def __call__(self, url: str, limit: int, allow_not_found: bool) -> Any:
        self.calls.append((url, limit, allow_not_found))
        values = self.responses.get(url)
        if not values:
            raise AssertionError("unexpected fetch")
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeGitHubFetcher:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, url: str, limit: int, token: str) -> Any:
        self.calls.append((url, limit, len(token)))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _exact_fetcher(manifest: Any, wheel: bytes, sdist: bytes) -> FakeFetcher:
    by_kind = {"wheel": wheel, "sdist": sdist}
    responses: dict[str, list[object]] = {
        verifier.PYPI_VERSION_URL: [verifier.HttpResult(200, _pypi_payload(manifest))]
    }
    for artifact in manifest.artifacts:
        responses[_pypi_url(artifact.filename)] = [
            verifier.HttpResult(200, by_kind[artifact.kind])
        ]
    return FakeFetcher(responses)


def _runner_with_second_parent_status(status: int) -> Any:
    def runner(root: Path, arguments: tuple[str, ...], limit: int) -> Any:
        if (
            arguments[:3] == ("rev-parse", "--verify", "--quiet")
            and "^2^{commit}" in arguments[-1]
        ):
            return verifier.GitResult(status)
        return verifier.default_git_runner(root, arguments, limit)

    return runner


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout.strip()


def _write_control_paths(
    repository: Path,
    paths: tuple[str, ...],
    *,
    missing: str | None = None,
    prefix: str = "control",
) -> None:
    for relative in paths:
        if relative == missing:
            continue
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{prefix} {relative}\n", encoding="utf-8")


def _git_repository(
    test_case: unittest.TestCase,
    *,
    missing: str | None = None,
    extra: bool = False,
    executable: str | None = None,
    symlink: str | None = None,
    merge: bool = False,
    root: bool = False,
) -> tuple[Path, str, str, Any]:
    temporary = tempfile.TemporaryDirectory()
    test_case.addCleanup(temporary.cleanup)
    repository = Path(temporary.name)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Synthetic Tester")
    _git(repository, "config", "user.email", "synthetic" + "@" + "example.invalid")
    if root:
        _write_control_paths(repository, verifier.RELEASE_CONTROL_PATHS)
        _git(repository, "add", ".")
        _git(repository, "commit", "-q", "-m", "root controls")
        control = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v1.0.0")
        return repository, "0" * 40, control, _manifest(source_commit="0" * 40)

    (repository / "README.md").write_text("stable source\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-q", "-m", "stable")
    stable = _git(repository, "rev-parse", "HEAD")
    if merge:
        _git(repository, "switch", "-q", "-c", "controls")
        _write_control_paths(repository, verifier.RELEASE_CONTROL_PATHS)
        _git(repository, "add", ".")
        _git(repository, "commit", "-q", "-m", "controls")
        _git(repository, "switch", "-q", "main")
        _git(repository, "merge", "-q", "--no-ff", "controls", "-m", "merge controls")
    else:
        _write_control_paths(
            repository,
            verifier.RELEASE_CONTROL_PATHS,
            missing=missing,
        )
        if extra:
            (repository / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")
        if executable is not None:
            os.chmod(repository / executable, 0o755)
        if symlink is not None:
            target = repository / symlink
            target.unlink()
            os.symlink("target", target)
        _git(repository, "add", ".")
        _git(repository, "commit", "-q", "-m", "controls")
    control = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v1.0.0")
    return repository, stable, control, _manifest(source_commit=stable)


def _recovery_git_repository(
    test_case: unittest.TestCase,
    *,
    missing: str | None = None,
    extra: bool = False,
    executable: str | None = None,
    symlink: str | None = None,
    merge: bool = False,
    root: bool = False,
) -> tuple[Path, str, str, str, Any]:
    temporary = tempfile.TemporaryDirectory()
    test_case.addCleanup(temporary.cleanup)
    repository = Path(temporary.name)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Synthetic Tester")
    _git(repository, "config", "user.email", "synthetic" + "@" + "example.invalid")
    if root:
        _write_control_paths(repository, verifier.RECOVERY_CONTROL_PATHS)
        _git(repository, "add", ".")
        _git(repository, "commit", "-q", "-m", "root recovery")
        recovery = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v1.0.0")
        manifest = _manifest(source_commit="0" * 40)
        return repository, "0" * 40, recovery, recovery, manifest

    (repository / "README.md").write_text("stable source\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-q", "-m", "stable")
    stable = _git(repository, "rev-parse", "HEAD")
    _write_control_paths(repository, verifier.RELEASE_CONTROL_PATHS)
    _git(repository, "add", ".")
    _git(repository, "commit", "-q", "-m", "release controls")
    release_control = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v1.0.0")

    if merge:
        _git(repository, "switch", "-q", "-c", "recovery")
        _write_control_paths(
            repository,
            verifier.RECOVERY_CONTROL_PATHS,
            prefix="recovery",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-q", "-m", "recovery controls")
        _git(repository, "switch", "-q", "main")
        _git(repository, "merge", "-q", "--no-ff", "recovery", "-m", "merge recovery")
    else:
        _write_control_paths(
            repository,
            verifier.RECOVERY_CONTROL_PATHS,
            missing=missing,
            prefix="recovery",
        )
        if extra:
            (repository / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")
        if executable is not None:
            os.chmod(repository / executable, 0o755)
        if symlink is not None:
            target = repository / symlink
            target.unlink()
            os.symlink("target", target)
        _git(repository, "add", ".")
        _git(repository, "commit", "-q", "-m", "recovery controls")
    recovery = _git(repository, "rev-parse", "HEAD")
    return repository, stable, release_control, recovery, _manifest(source_commit=stable)


@unittest.skipUnless(VERIFIER_AVAILABLE, "release verifier is intentionally absent from sdists")
class FrozenReleaseVerifierTests(unittest.TestCase):
    def assert_sanitized(self, error: BaseException, unsafe: str = "") -> None:
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        if unsafe:
            self.assertNotIn(unsafe, str(error))
            self.assertNotIn(unsafe, repr(error))

    def test_reviewed_manifest_loads_exact_frozen_values(self) -> None:
        manifest = verifier.load_manifest(PROJECT_ROOT)
        self.assertEqual(
            manifest.artifact_source_commit,
            "fbc9916ee2b714f0edb29a5e503d0f3f72d223cb",
        )
        self.assertEqual(tuple(item.size for item in manifest.artifacts), (42_333, 74_346))
        self.assertEqual(manifest.request_schema_version, 1)

    def test_manifest_rejects_duplicate_unknown_and_changed_fields(self) -> None:
        original = (PROJECT_ROOT / verifier.MANIFEST_PATH).read_text(encoding="utf-8")
        variants = (
            original.replace('"version": "1.0.0"', '"version": "1.0.0", "version": "2.0.0"'),
            original.replace('"version": "1.0.0"', '"version": "1.0.0", "unknown": true'),
            original.replace('"request_schema_version": 1', '"request_schema_version": 2'),
        )
        for content in variants:
            with (
                self.subTest(content_hash=hashlib.sha256(content.encode()).hexdigest()),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                path = root / verifier.MANIFEST_PATH
                path.parent.mkdir(parents=True)
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(verifier.VerificationError) as raised:
                    verifier.load_manifest(root)
                self.assert_sanitized(raised.exception)

    def test_exact_single_parent_control_commit_passes(self) -> None:
        repository, _, control, manifest = _git_repository(self)
        self.assertEqual(verifier.verify_git_trust(repository, manifest, control), control)

    def test_wrong_parent_root_merge_missing_and_fifth_path_fail(self) -> None:
        repository, _, control, manifest = _git_repository(self)
        wrong_parent = copy.copy(manifest)
        object.__setattr__(wrong_parent, "artifact_source_commit", "0" * 40)
        cases: list[tuple[Path, str, Any]] = [(repository, control, wrong_parent)]
        for options in (
            {"root": True},
            {"merge": True},
            {"missing": verifier.CONTROL_PATHS[-1]},
            {"extra": True},
        ):
            candidate, _, candidate_control, candidate_manifest = _git_repository(self, **options)
            cases.append((candidate, candidate_control, candidate_manifest))
        for candidate, candidate_control, candidate_manifest in cases:
            with self.subTest(commit=candidate_control):
                with self.assertRaises(verifier.VerificationError) as raised:
                    verifier.verify_git_trust(candidate, candidate_manifest, candidate_control)
                self.assert_sanitized(raised.exception)

    def test_executable_symlink_and_checkout_mismatch_fail(self) -> None:
        cases = (
            {"executable": verifier.CONTROL_PATHS[0]},
            {"symlink": verifier.CONTROL_PATHS[1]},
        )
        for options in cases:
            repository, _, control, manifest = _git_repository(self, **options)
            with self.subTest(options=options):
                with self.assertRaises(verifier.VerificationError) as raised:
                    verifier.verify_git_trust(repository, manifest, control)
                self.assert_sanitized(raised.exception)
        repository, _, control, manifest = _git_repository(self)
        (repository / verifier.CONTROL_PATHS[0]).write_text("changed checkout\n", encoding="utf-8")
        with self.assertRaises(verifier.VerificationError) as raised:
            verifier.verify_git_trust(repository, manifest, control)
        self.assert_sanitized(raised.exception)

    def test_second_parent_status_interpretation_is_exact(self) -> None:
        for status, passes in ((0, False), (1, True), (2, False), (128, False)):
            repository, _, control, manifest = _git_repository(self)
            runner = _runner_with_second_parent_status(status)

            with self.subTest(status=status):
                if passes:
                    self.assertEqual(
                        verifier.verify_git_trust(repository, manifest, control, runner), control
                    )
                else:
                    with self.assertRaises(verifier.VerificationError) as raised:
                        verifier.verify_git_trust(repository, manifest, control, runner)
                    self.assert_sanitized(raised.exception)

    def test_exact_recovery_control_commit_passes(self) -> None:
        repository, _, release_control, recovery, manifest = _recovery_git_repository(self)
        with mock.patch.object(
            verifier,
            "EXPECTED_RECOVERY_PARENT",
            release_control,
        ):
            self.assertEqual(
                verifier.verify_git_trust(
                    repository,
                    manifest,
                    recovery,
                    event_name="workflow_dispatch",
                ),
                recovery,
            )

    def test_recovery_root_merge_missing_and_extra_paths_fail(self) -> None:
        for options in (
            {"root": True},
            {"merge": True},
            {"missing": verifier.RECOVERY_CONTROL_PATHS[-1]},
            {"extra": True},
        ):
            repository, _, release_control, recovery, manifest = _recovery_git_repository(
                self, **options
            )
            with (
                self.subTest(options=options),
                mock.patch.object(
                    verifier,
                    "EXPECTED_RECOVERY_PARENT",
                    release_control,
                ),
                self.assertRaises(verifier.VerificationError) as raised,
            ):
                verifier.verify_git_trust(
                    repository,
                    manifest,
                    recovery,
                    event_name="workflow_dispatch",
                )
            self.assert_sanitized(raised.exception)

    def test_recovery_non_blob_mode_and_checkout_mismatches_fail(self) -> None:
        for options in (
            {"executable": verifier.RECOVERY_CONTROL_PATHS[0]},
            {"symlink": verifier.RECOVERY_CONTROL_PATHS[1]},
        ):
            repository, _, release_control, recovery, manifest = _recovery_git_repository(
                self, **options
            )
            with (
                self.subTest(options=options),
                mock.patch.object(
                    verifier,
                    "EXPECTED_RECOVERY_PARENT",
                    release_control,
                ),
                self.assertRaises(verifier.VerificationError) as raised,
            ):
                verifier.verify_git_trust(
                    repository,
                    manifest,
                    recovery,
                    event_name="workflow_dispatch",
                )
            self.assert_sanitized(raised.exception)

        repository, _, release_control, recovery, manifest = _recovery_git_repository(self)
        (repository / verifier.RECOVERY_CONTROL_PATHS[0]).write_text(
            "checkout mismatch\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(
                verifier,
                "EXPECTED_RECOVERY_PARENT",
                release_control,
            ),
            self.assertRaises(verifier.VerificationError) as raised,
        ):
            verifier.verify_git_trust(
                repository,
                manifest,
                recovery,
                event_name="workflow_dispatch",
            )
        self.assert_sanitized(raised.exception)

        repository, _, release_control, recovery, manifest = _recovery_git_repository(self)

        def tree_entry_runner(
            root: Path,
            arguments: tuple[str, ...],
            limit: int,
        ) -> Any:
            if arguments[:2] == ("ls-tree", "-z"):
                path = arguments[-1]
                if path == verifier.RECOVERY_CONTROL_PATHS[0]:
                    return verifier.GitResult(
                        0,
                        b"040000 tree " + b"a" * 40 + b"\t" + path.encode() + b"\0",
                    )
            return verifier.default_git_runner(root, arguments, limit)

        with (
            mock.patch.object(
                verifier,
                "EXPECTED_RECOVERY_PARENT",
                release_control,
            ),
            self.assertRaises(verifier.VerificationError) as tree_error,
        ):
            verifier.verify_git_trust(
                repository,
                manifest,
                recovery,
                tree_entry_runner,
                event_name="workflow_dispatch",
            )
        self.assert_sanitized(tree_error.exception)

    def test_tag_workflow_and_oversized_git_output_fail(self) -> None:
        repository, _, control, manifest = _git_repository(self)
        with self.assertRaises(verifier.VerificationError):
            verifier.verify_git_trust(repository, manifest, "0" * 40)

        def oversized(_: Path, __: tuple[str, ...], limit: int) -> Any:
            return verifier.GitResult(0, b"a" * (limit + 1))

        with self.assertRaises(verifier.VerificationError) as raised:
            verifier.verify_git_trust(repository, manifest, control, oversized)
        self.assert_sanitized(raised.exception)

    def test_git_oserror_is_fixed_and_unchained(self) -> None:
        unsafe = "sensitive synthetic git detail"
        with (
            mock.patch.object(verifier.subprocess, "run", side_effect=OSError(unsafe)),
            self.assertRaises(verifier.VerificationError) as raised,
        ):
            verifier.default_git_runner(PROJECT_ROOT, ("rev-parse", "HEAD"), 41)
        self.assertEqual(str(raised.exception), "git.unavailable")
        self.assert_sanitized(raised.exception, unsafe)

    def test_exact_release_event_passes(self) -> None:
        manifest = _manifest()
        path = _write_event(self, _release_payload(manifest))
        assets = verifier.verify_release_event(
            path,
            manifest,
            event_name="release",
            repository=manifest.repository,
            ref="refs/tags/v1.0.0",
        )
        self.assertEqual(
            {asset.name for asset in assets},
            {item.filename for item in manifest.artifacts},
        )

    def test_wrong_repository_event_ref_tag_draft_and_prerelease_fail(self) -> None:
        manifest = _manifest()
        base = _release_payload(manifest)
        cases: list[tuple[dict[str, object], str, str, str]] = [
            (base, "push", manifest.repository, "refs/tags/v1.0.0"),
            (base, "release", "example/other", "refs/tags/v1.0.0"),
            (base, "release", manifest.repository, "refs/heads/main"),
        ]
        wrong_action = copy.deepcopy(base)
        wrong_action["action"] = "created"
        cases.append((wrong_action, "release", manifest.repository, "refs/tags/v1.0.0"))
        for field, value in (("draft", True), ("prerelease", True), ("tag_name", "v2.0.0")):
            payload = copy.deepcopy(base)
            release = payload["release"]
            assert isinstance(release, dict)
            release[field] = value
            cases.append((payload, "release", manifest.repository, "refs/tags/v1.0.0"))
        for payload, event_name, repository, ref in cases:
            with self.subTest(event_name=event_name, repository=repository, ref=ref):
                path = _write_event(self, payload)
                with self.assertRaises(verifier.VerificationError) as raised:
                    verifier.verify_release_event(
                        path,
                        manifest,
                        event_name=event_name,
                        repository=repository,
                        ref=ref,
                    )
                self.assert_sanitized(raised.exception)

    def test_missing_extra_and_malformed_release_assets_fail(self) -> None:
        manifest = _manifest()
        base = _release_payload(manifest)
        release = base["release"]
        assert isinstance(release, dict)
        assets = release["assets"]
        assert isinstance(assets, list)
        variants = []
        missing = copy.deepcopy(base)
        missing_release = missing["release"]
        assert isinstance(missing_release, dict)
        missing_release["assets"] = assets[:1]
        variants.append(missing)
        extra = copy.deepcopy(base)
        extra_release = extra["release"]
        assert isinstance(extra_release, dict)
        extra_assets = extra_release["assets"]
        assert isinstance(extra_assets, list)
        extra_assets.append(copy.deepcopy(extra_assets[0]))
        variants.append(extra)
        malformed = copy.deepcopy(base)
        malformed_release = malformed["release"]
        assert isinstance(malformed_release, dict)
        malformed_assets = malformed_release["assets"]
        assert isinstance(malformed_assets, list)
        first = malformed_assets[0]
        assert isinstance(first, dict)
        first["size"] = 1
        variants.append(malformed)
        for payload in variants:
            with self.assertRaises(verifier.VerificationError) as raised:
                verifier.verify_release_event(
                    _write_event(self, payload),
                    manifest,
                    event_name="release",
                    repository=manifest.repository,
                    ref="refs/tags/v1.0.0",
                )
            self.assert_sanitized(raised.exception)

    def test_event_size_is_bounded(self) -> None:
        manifest = _manifest()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        event = Path(temporary.name) / "event.json"
        event.write_bytes(b"x" * (verifier.MAX_EVENT_BYTES + 1))
        with self.assertRaises(verifier.VerificationError) as raised:
            verifier.verify_release_event(
                event,
                manifest,
                event_name="release",
                repository=manifest.repository,
                ref="refs/tags/v1.0.0",
            )
        self.assert_sanitized(raised.exception)

    def test_exact_recovery_dispatch_event_passes(self) -> None:
        release_id = verifier.verify_recovery_event(
            _write_event(self, _recovery_event_payload()),
            event_name="workflow_dispatch",
            repository=verifier.EXPECTED_REPOSITORY,
            ref=verifier.EXPECTED_RECOVERY_REF,
            workflow_ref=verifier.EXPECTED_RECOVERY_WORKFLOW_REF,
        )
        self.assertEqual(release_id, "380237416")

    def test_recovery_dispatch_context_and_release_id_are_exact(self) -> None:
        exact = _recovery_event_payload()
        context_cases = (
            ("release", verifier.EXPECTED_REPOSITORY, verifier.EXPECTED_RECOVERY_REF,
             verifier.EXPECTED_RECOVERY_WORKFLOW_REF),
            ("workflow_dispatch", "example/other", verifier.EXPECTED_RECOVERY_REF,
             verifier.EXPECTED_RECOVERY_WORKFLOW_REF),
            ("workflow_dispatch", verifier.EXPECTED_REPOSITORY, "refs/heads/other",
             verifier.EXPECTED_RECOVERY_WORKFLOW_REF),
            ("workflow_dispatch", verifier.EXPECTED_REPOSITORY,
             verifier.EXPECTED_RECOVERY_REF,
             "healthassure-github/healthassure-messaging/"
             ".github/workflows/other.yml@refs/heads/main"),
        )
        for event_name, repository, ref, workflow_ref in context_cases:
            with (
                self.subTest(
                    event_name=event_name,
                    repository=repository,
                    ref=ref,
                    workflow_ref=workflow_ref,
                ),
                self.assertRaises(verifier.VerificationError) as raised,
            ):
                verifier.verify_recovery_event(
                    _write_event(self, exact),
                    event_name=event_name,
                    repository=repository,
                    ref=ref,
                    workflow_ref=workflow_ref,
                )
            self.assert_sanitized(raised.exception)

        invalid_inputs: tuple[object, ...] = (
            {},
            {"inputs": {}, "repository": exact["repository"]},
            _recovery_event_payload(None),
            _recovery_event_payload(380237416),
            _recovery_event_payload(""),
            _recovery_event_payload("380237415"),
            {
                "inputs": {"release_id": "380237416"},
                "repository": {"full_name": "example/other"},
            },
            {
                "inputs": {"release_id": "380237416", "extra": "value"},
                "repository": exact["repository"],
            },
        )
        for payload in invalid_inputs:
            with (
                self.subTest(payload_type=type(payload).__name__),
                self.assertRaises(verifier.VerificationError) as raised,
            ):
                verifier.verify_recovery_event(
                    _write_event(self, payload),
                    event_name="workflow_dispatch",
                    repository=verifier.EXPECTED_REPOSITORY,
                    ref=verifier.EXPECTED_RECOVERY_REF,
                    workflow_ref=verifier.EXPECTED_RECOVERY_WORKFLOW_REF,
                )
            self.assert_sanitized(raised.exception)

    def test_recovery_release_api_returns_only_exact_assets(self) -> None:
        manifest = _manifest()
        fetcher = FakeGitHubFetcher(
            verifier.HttpResult(200, _github_release_payload(manifest))
        )
        assets = verifier.fetch_recovery_release(
            manifest,
            "380237416",
            "synthetic-runtime-token",
            fetcher,
        )
        self.assertEqual(
            {asset.name for asset in assets},
            {artifact.filename for artifact in manifest.artifacts},
        )
        self.assertEqual(
            fetcher.calls,
            [
                (
                    verifier.GITHUB_GRAPHQL_URL,
                    verifier.MAX_GITHUB_RELEASE_BYTES,
                    len("synthetic-runtime-token"),
                )
            ],
        )
        self.assertNotIn("description", verifier.GITHUB_RELEASE_QUERY)
        self.assertNotIn("body", verifier.GITHUB_RELEASE_QUERY)

    def test_recovery_context_binds_event_git_and_github_release(self) -> None:
        repository, _, release_control, recovery, manifest = _recovery_git_repository(self)
        event = _write_event(self, _recovery_event_payload())
        fetcher = FakeGitHubFetcher(
            verifier.HttpResult(
                200,
                _github_release_payload(manifest, tag_commit=release_control),
            )
        )
        with (
            mock.patch.object(verifier, "load_manifest", return_value=manifest),
            mock.patch.object(
                verifier,
                "EXPECTED_RECOVERY_PARENT",
                release_control,
            ),
        ):
            context = verifier.build_release_context(
                repository,
                event_name="workflow_dispatch",
                event_path=event,
                repository=verifier.EXPECTED_REPOSITORY,
                ref=verifier.EXPECTED_RECOVERY_REF,
                workflow_ref=verifier.EXPECTED_RECOVERY_WORKFLOW_REF,
                workflow_sha=recovery,
                github_token="synthetic-runtime-token",
                github_fetcher=fetcher,
            )
        self.assertEqual(context.control_commit, recovery)
        self.assertEqual(len(context.assets), 2)
        self.assertEqual(len(fetcher.calls), 1)

    def test_recovery_release_api_rejects_malformed_or_inexact_records(self) -> None:
        manifest = _manifest()
        exact = json.loads(_github_release_payload(manifest))
        assert isinstance(exact, dict)
        variants: list[bytes] = [b"not-json"]
        for field, value in (
            ("databaseId", 380237415),
            ("isDraft", True),
            ("isPrerelease", True),
            ("tagName", "v2.0.0"),
            ("publishedAt", None),
        ):
            payload = copy.deepcopy(exact)
            data = payload["data"]
            assert isinstance(data, dict)
            repository = data["repository"]
            assert isinstance(repository, dict)
            release = repository["release"]
            assert isinstance(release, dict)
            release[field] = value
            variants.append(json.dumps(payload).encode())
        wrong_commit = copy.deepcopy(exact)
        data = wrong_commit["data"]
        assert isinstance(data, dict)
        repository = data["repository"]
        assert isinstance(repository, dict)
        release = repository["release"]
        assert isinstance(release, dict)
        tag_commit = release["tagCommit"]
        assert isinstance(tag_commit, dict)
        tag_commit["oid"] = "0" * 40
        variants.append(json.dumps(wrong_commit).encode())

        wrong_digest = copy.deepcopy(exact)
        data = wrong_digest["data"]
        assert isinstance(data, dict)
        repository = data["repository"]
        assert isinstance(repository, dict)
        release = repository["release"]
        assert isinstance(release, dict)
        release_assets = release["releaseAssets"]
        assert isinstance(release_assets, dict)
        nodes = release_assets["nodes"]
        assert isinstance(nodes, list)
        first = nodes[0]
        assert isinstance(first, dict)
        first["digest"] = "sha256:" + "0" * 64
        variants.append(json.dumps(wrong_digest).encode())

        missing_asset = copy.deepcopy(exact)
        data = missing_asset["data"]
        assert isinstance(data, dict)
        repository = data["repository"]
        assert isinstance(repository, dict)
        release = repository["release"]
        assert isinstance(release, dict)
        release_assets = release["releaseAssets"]
        assert isinstance(release_assets, dict)
        assets = release_assets["nodes"]
        assert isinstance(assets, list)
        release_assets["nodes"] = assets[:1]
        variants.append(json.dumps(missing_asset).encode())

        for variant in variants:
            fetcher = FakeGitHubFetcher(verifier.HttpResult(200, variant))
            with (
                self.subTest(payload_hash=hashlib.sha256(variant).hexdigest()),
                self.assertRaises(verifier.VerificationError) as raised,
            ):
                verifier.fetch_recovery_release(
                    manifest,
                    "380237416",
                    "synthetic-runtime-token",
                    fetcher,
                )
            self.assert_sanitized(raised.exception)

    def test_recovery_api_failures_and_tokens_are_sanitized(self) -> None:
        unsafe = "unsafe synthetic API detail"
        manifest = _manifest()
        fetcher = FakeGitHubFetcher(OSError(unsafe))
        with self.assertRaises(verifier.VerificationError) as raised:
            verifier.fetch_recovery_release(
                manifest,
                "380237416",
                "synthetic-runtime-token",
                fetcher,
            )
        self.assertEqual(str(raised.exception), "network.github_release")
        self.assert_sanitized(raised.exception, unsafe)

        for token in ("", "contains whitespace", "line\nbreak"):
            with (
                self.subTest(token_length=len(token)),
                self.assertRaises(verifier.VerificationError) as token_error,
            ):
                verifier.fetch_recovery_release(
                    manifest,
                    "380237416",
                    token,
                    FakeGitHubFetcher(
                        verifier.HttpResult(200, _github_release_payload(manifest))
                    ),
                )
            self.assertEqual(str(token_error.exception), "github.token")
            self.assert_sanitized(token_error.exception, token)

    def test_exact_wheel_and_sdist_pass_full_artifact_validation(self) -> None:
        wheel = _wheel_bytes()
        sdist = _sdist_bytes()
        manifest = _manifest(wheel, sdist)
        verifier.verify_artifact(wheel, manifest.artifacts[0], manifest)
        verifier.verify_artifact(sdist, manifest.artifacts[1], manifest)

    def test_artifacts_reject_integrity_metadata_and_disclosure_defects(self) -> None:
        unsafe = "https://" + "example-region-python" + "." + "pkg" + "." + "dev/simple/"
        bad_metadata = _metadata().replace(b"requests<3,>=2.32.3", b"requests<4,>=2.32.3")
        variants = (
            _wheel_bytes(metadata=bad_metadata),
            _wheel_bytes(schema=2),
            _wheel_bytes(include_typed=False),
            _wheel_bytes(include_notice=False),
            _wheel_bytes(disclosure=unsafe),
            _wheel_bytes(symlink=True),
        )
        for content in variants:
            manifest = _manifest(content, _sdist_bytes())
            with self.subTest(content_hash=hashlib.sha256(content).hexdigest()):
                with self.assertRaises(verifier.VerificationError) as raised:
                    verifier.verify_artifact(content, manifest.artifacts[0], manifest)
                self.assert_sanitized(raised.exception, unsafe)
        valid = _wheel_bytes()
        manifest = _manifest(valid, _sdist_bytes())
        with self.assertRaises(verifier.VerificationError):
            verifier.verify_artifact(valid + b"changed", manifest.artifacts[0], manifest)

    def test_duplicate_wheel_and_sdist_entries_fail(self) -> None:
        wheel_buffer = io.BytesIO()
        with zipfile.ZipFile(wheel_buffer, "w") as archive:
            archive.writestr("duplicate", b"first")
            archive.writestr("duplicate", b"second")
        wheel = wheel_buffer.getvalue()
        wheel_manifest = _manifest(wheel, _sdist_bytes())
        with self.assertRaises(verifier.VerificationError) as wheel_error:
            verifier.verify_artifact(wheel, wheel_manifest.artifacts[0], wheel_manifest)
        self.assert_sanitized(wheel_error.exception)

        sdist_buffer = io.BytesIO()
        with tarfile.open(fileobj=sdist_buffer, mode="w:gz") as archive:
            for content in (b"first", b"second"):
                information = tarfile.TarInfo("duplicate")
                information.size = len(content)
                archive.addfile(information, io.BytesIO(content))
        sdist = sdist_buffer.getvalue()
        sdist_manifest = _manifest(_wheel_bytes(), sdist)
        with self.assertRaises(verifier.VerificationError) as sdist_error:
            verifier.verify_artifact(sdist, sdist_manifest.artifacts[1], sdist_manifest)
        self.assert_sanitized(sdist_error.exception)

    def test_release_download_uses_exact_assets_once_and_refuses_overwrite(self) -> None:
        wheel = _wheel_bytes()
        sdist = _sdist_bytes()
        manifest = _manifest(wheel, sdist)
        assets = tuple(
            verifier.ReleaseAsset(item.filename, item.size, _release_url(item.filename))
            for item in manifest.artifacts
        )
        by_kind = {"wheel": wheel, "sdist": sdist}
        fetcher = FakeFetcher(
            {
                _release_url(item.filename): [verifier.HttpResult(200, by_kind[item.kind])]
                for item in manifest.artifacts
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "release-assets"
            verifier.download_release_assets(directory, assets, manifest, fetcher)
            verifier.verify_local_artifacts(directory, manifest)
            self.assertEqual(len(fetcher.calls), 2)
            with self.assertRaises(verifier.VerificationError):
                verifier.download_release_assets(directory, assets, manifest, fetcher)

    def test_pypi_absent_and_exact_states(self) -> None:
        wheel = _wheel_bytes()
        sdist = _sdist_bytes()
        manifest = _manifest(wheel, sdist)
        absent = FakeFetcher(
            {verifier.PYPI_VERSION_URL: [verifier.HttpResult(404, b"")]}
        )
        self.assertIs(verifier.inspect_pypi(manifest, absent), verifier.PyPIState.ABSENT)
        exact = _exact_fetcher(manifest, wheel, sdist)
        self.assertIs(verifier.inspect_pypi(manifest, exact), verifier.PyPIState.EXACT)
        self.assertEqual(len(exact.calls), 3)

    def test_pypi_partial_extra_mismatched_and_unverifiable_states_fail(self) -> None:
        wheel = _wheel_bytes()
        sdist = _sdist_bytes()
        manifest = _manifest(wheel, sdist)
        exact_payload = json.loads(_pypi_payload(manifest))
        assert isinstance(exact_payload, dict)
        exact_urls = exact_payload["urls"]
        assert isinstance(exact_urls, list)
        variants: list[object] = [
            {"urls": exact_urls[:1]},
            {"urls": [*exact_urls, copy.deepcopy(exact_urls[0])]},
        ]
        mismatch = copy.deepcopy(exact_payload)
        mismatch_urls = mismatch["urls"]
        assert isinstance(mismatch_urls, list)
        mismatch_file = mismatch_urls[0]
        assert isinstance(mismatch_file, dict)
        mismatch_file["size"] = 1
        variants.append(mismatch)
        for payload in variants:
            fetcher = FakeFetcher(
                {
                    verifier.PYPI_VERSION_URL: [
                        verifier.HttpResult(200, json.dumps(payload).encode())
                    ]
                }
            )
            payload_hash = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
            with self.subTest(payload_hash=payload_hash):
                with self.assertRaises(verifier.VerificationError) as raised:
                    verifier.inspect_pypi(manifest, fetcher)
                self.assert_sanitized(raised.exception)

    def test_postflight_accepts_eventual_exact_and_rejects_persistent_absence(self) -> None:
        wheel = _wheel_bytes()
        sdist = _sdist_bytes()
        manifest = _manifest(wheel, sdist)
        responses: dict[str, list[object]] = {
            verifier.PYPI_VERSION_URL: [
                verifier.HttpResult(404, b""),
                verifier.HttpResult(200, _pypi_payload(manifest)),
            ]
        }
        by_kind = {"wheel": wheel, "sdist": sdist}
        for item in manifest.artifacts:
            responses[_pypi_url(item.filename)] = [
                verifier.HttpResult(200, by_kind[item.kind])
            ]
        sleeps: list[float] = []
        verifier.verify_pypi_postflight(
            manifest,
            FakeFetcher(responses),
            sleeps.append,
            attempts=2,
            delay_seconds=0.5,
        )
        self.assertEqual(sleeps, [0.5])
        absent = FakeFetcher(
            {
                verifier.PYPI_VERSION_URL: [
                    verifier.HttpResult(404, b""),
                    verifier.HttpResult(404, b""),
                ]
            }
        )
        with self.assertRaises(verifier.VerificationError) as raised:
            verifier.verify_pypi_postflight(
                manifest, absent, lambda _: None, attempts=2, delay_seconds=0
            )
        self.assertEqual(str(raised.exception), "pypi.not_exact")

    def test_network_failures_are_fixed_and_unchained(self) -> None:
        unsafe = "sensitive synthetic network detail"
        cases = (
            ("https://pypi.org/example", "network.pypi"),
            (_release_url("artifact.whl"), "network.release_asset"),
        )
        for url, expected_code in cases:
            for exception in (OSError(unsafe), TimeoutError(unsafe)):
                opener = mock.Mock()
                opener.open.side_effect = exception
                with (
                    self.subTest(
                        url_hash=hashlib.sha256(url.encode()).hexdigest(),
                        exception_type=type(exception).__name__,
                    ),
                    mock.patch.object(
                        verifier.urllib.request,
                        "build_opener",
                        return_value=opener,
                    ),
                    self.assertRaises(verifier.VerificationError) as raised,
                ):
                    verifier.default_fetch(url, 100, False)
                self.assertEqual(str(raised.exception), expected_code)
                self.assert_sanitized(raised.exception, unsafe)

    def test_release_asset_redirect_delegates_to_standard_library_once(self) -> None:
        handler = verifier._SafeRedirectHandler()
        request = verifier.urllib.request.Request(
            "https://github.com/healthassure-github/healthassure-messaging/"
            "releases/download/v1.0.0/artifact.whl"
        )
        expected = verifier.urllib.request.Request(
            "https://release-assets.githubusercontent.com/synthetic/artifact.whl"
        )
        signed_url = (
            "https://release-assets.githubusercontent.com/synthetic/"
            "artifact.whl?signature=synthetic-value"
        )
        with mock.patch.object(
            verifier.urllib.request.HTTPRedirectHandler,
            "redirect_request",
            return_value=expected,
        ) as parent:
            allowed = handler.redirect_request(
                request,
                io.BytesIO(),
                302,
                "redirect",
                Message(),
                signed_url,
            )
        self.assertIs(allowed, expected)
        parent.assert_called_once()
        self.assertEqual(handler.__dict__, {"redirects": 1})
        self.assertNotIn("synthetic-value", repr(handler))

        rejected_second = handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "redirect",
            Message(),
            "https://release-assets.githubusercontent.com/synthetic/second.whl",
        )
        self.assertIsNone(rejected_second)

    def test_release_asset_redirect_rejects_unsafe_boundaries(self) -> None:
        safe_original = (
            "https://github.com/healthassure-github/healthassure-messaging/"
            "releases/download/v1.0.0/artifact.whl"
        )
        safe_target = (
            "https://release-assets.githubusercontent.com/synthetic/"
            "artifact.whl?signature=synthetic-value"
        )
        cases = (
            (safe_original.replace("github.com", "example.invalid"), safe_target, 302),
            (safe_original.replace("https://", "http://"), safe_target, 302),
            (
                safe_original.replace("github.com", "user:pass" + "@" + "github.com"),
                safe_target,
                302,
            ),
            (safe_original.replace("github.com", "github.com:444"), safe_target, 302),
            (safe_original + "#fragment", safe_target, 302),
            (safe_original, safe_target.replace("release-assets.githubusercontent.com",
                                                 "example.invalid"), 302),
            (safe_original, safe_target.replace("https://", "http://"), 302),
            (
                safe_original,
                safe_target.replace(
                    "release-assets.githubusercontent.com",
                    "user:pass" + "@" + "release-assets.githubusercontent.com",
                ),
                302,
            ),
            (safe_original, safe_target.replace("release-assets.githubusercontent.com",
                                                 "release-assets.githubusercontent.com:444"), 302),
            (safe_original, safe_target + "#fragment", 302),
            (safe_original, safe_target, 304),
        )
        for original, target, code in cases:
            with self.subTest(
                original_hash=hashlib.sha256(original.encode()).hexdigest(),
                target_hash=hashlib.sha256(target.encode()).hexdigest(),
                code=code,
            ):
                rejected = verifier._SafeRedirectHandler().redirect_request(
                    verifier.urllib.request.Request(original),
                    io.BytesIO(),
                    code,
                    "redirect",
                    Message(),
                    target,
                )
                self.assertIsNone(rejected)

    def test_github_api_network_failure_is_fixed_and_unchained(self) -> None:
        unsafe = "signed-query=synthetic-sensitive-detail"
        opener = mock.Mock()
        opener.open.side_effect = OSError(unsafe)
        with (
            mock.patch.object(
                verifier.urllib.request,
                "build_opener",
                return_value=opener,
            ),
            self.assertRaises(verifier.VerificationError) as raised,
        ):
            verifier.default_github_fetch(
                verifier.GITHUB_GRAPHQL_URL,
                100,
                "synthetic-runtime-token",
            )
        self.assertEqual(str(raised.exception), "network.github_release")
        self.assert_sanitized(raised.exception, unsafe)

    def test_github_api_uses_fixed_bounded_projection_and_ephemeral_token(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.headers = Message()
        response.read.return_value = b"{}"
        response.__enter__.return_value = response
        checked = False

        def open_request(request: Any, *, timeout: float) -> Any:
            nonlocal checked
            self.assertEqual(timeout, verifier.NETWORK_TIMEOUT_SECONDS)
            self.assertEqual(request.full_url, verifier.GITHUB_GRAPHQL_URL)
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                json.loads(request.data),
                {"query": verifier.GITHUB_RELEASE_QUERY},
            )
            self.assertEqual(
                request.get_header("Authorization"),
                "Bearer synthetic-runtime-token",
            )
            checked = True
            return response

        opener = mock.Mock()
        opener.open.side_effect = open_request
        with mock.patch.object(
            verifier.urllib.request,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            result = verifier.default_github_fetch(
                verifier.GITHUB_GRAPHQL_URL,
                100,
                "synthetic-runtime-token",
            )
        self.assertTrue(checked)
        self.assertEqual(result, verifier.HttpResult(200, b"{}"))
        self.assertIsInstance(
            build_opener.call_args.args[0],
            verifier._RejectRedirectHandler,
        )
        self.assertNotIn("synthetic-runtime-token", repr(opener.mock_calls))

    def test_failure_codes_do_not_retain_unsafe_details(self) -> None:
        unsafe = "recipient token raw exception"
        error = verifier.VerificationError("release.invalid")
        self.assert_sanitized(error, unsafe)
        self.assertNotIn(unsafe, str(error))


if __name__ == "__main__":
    unittest.main()
