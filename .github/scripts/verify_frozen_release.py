#!/usr/bin/env python3
"""Fail-closed verification for the frozen healthassure-messaging 1.0.0 release."""

from __future__ import annotations

import argparse
import ast
import email
import hashlib
import json
import re
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from enum import Enum
from http.client import HTTPException
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

EXPECTED_REPOSITORY = "healthassure-github/healthassure-messaging"
MANIFEST_PATH = Path(".github/release-manifests/v1.0.0.json")
RELEASE_CONTROL_PATHS = (
    ".github/release-manifests/v1.0.0.json",
    ".github/scripts/verify_frozen_release.py",
    ".github/workflows/release.yml",
    "tests/test_public_surface.py",
    "tests/test_verify_frozen_release.py",
)
CONTROL_PATHS = RELEASE_CONTROL_PATHS
MAX_EVENT_BYTES = 1_048_576
MAX_MANIFEST_BYTES = 16_384
MAX_PYPI_JSON_BYTES = 1_048_576
MAX_ARCHIVE_ENTRIES = 512
MAX_ARCHIVE_CONTENT_BYTES = 8_388_608
NETWORK_TIMEOUT_SECONDS = 10
GIT_TIMEOUT_SECONDS = 10
POSTFLIGHT_ATTEMPTS = 6
POSTFLIGHT_DELAY_SECONDS = 10
PYPI_VERSION_URL = "https://pypi.org/pypi/healthassure-messaging/1.0.0/json"
HEX_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
HEX_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
REQUIREMENT_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)([^;]*)(?:;(.*))?$")
TEXT_ARCHIVE_SUFFIXES = {"", ".in", ".md", ".py", ".toml", ".typed"}
DISCLOSURE_PATTERNS = (
    re.compile(r"\b[a-z0-9.-]+-python[.]pkg[.]dev\b", re.IGNORECASE),
    re.compile("workload" + r"IdentityPools/[A-Za-z0-9._~/-]+"),
    re.compile(
        r"\b[a-z][a-z0-9-]*@[a-z][a-z0-9-]*[.]iam[.]gserviceaccount[.]com\b",
        re.IGNORECASE,
    ),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
    re.compile("-----BEGIN " + r"(?:EC |RSA )?PRIVATE KEY-----"),
    re.compile(r"\bya" + r"29[.][A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}\b"),
    re.compile(r"[+]91[0-9]{10}\b"),
)


class VerificationError(RuntimeError):
    """A fixed, sanitized release-control failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise VerificationError(code)


@dataclass(frozen=True)
class ArtifactSpec:
    kind: str
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseManifest:
    repository: str
    distribution: str
    import_package: str
    version: str
    tag: str
    artifact_source_commit: str
    request_schema_version: int
    python_requires: str
    runtime_dependencies: tuple[str, ...]
    optional_dependencies: Mapping[str, tuple[str, ...]]
    artifacts: tuple[ArtifactSpec, ...]


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    size: int
    download_url: str


@dataclass(frozen=True)
class ReleaseContext:
    manifest: ReleaseManifest
    assets: tuple[ReleaseAsset, ...]
    control_commit: str


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes = b""


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes


GitRunner = Callable[[Path, tuple[str, ...], int], GitResult]
Fetcher = Callable[[str, int, bool], HttpResult]
Sleeper = Callable[[float], None]


class PyPIState(str, Enum):
    ABSENT = "absent"
    EXACT = "exact"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_bounded_file(path: Path, maximum_bytes: int, code: str) -> bytes:
    failed = False
    content = b""
    try:
        if path.is_symlink() or not path.is_file():
            failed = True
        else:
            with path.open("rb") as handle:
                content = handle.read(maximum_bytes + 1)
    except OSError:
        failed = True
    if failed or len(content) > maximum_bytes:
        _fail(code)
    return content


def _parse_json(content: bytes, code: str) -> object:
    failed = False
    parsed: object = None
    try:
        parsed = json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        failed = True
    if failed:
        _fail(code)
    return parsed


def _expect_object(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(code)
    return cast(dict[str, object], value)


def _expect_list(value: object, code: str) -> list[object]:
    if not isinstance(value, list):
        _fail(code)
    return cast(list[object], value)


def _expect_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(code)
    return value


def _expect_integer(value: object, code: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(code)
    return cast(int, value)


def _expect_boolean(value: object, code: str) -> bool:
    if type(value) is not bool:
        _fail(code)
    return cast(bool, value)


def _require_exact_keys(value: Mapping[str, object], keys: set[str], code: str) -> None:
    if set(value) != keys:
        _fail(code)


def _specifier_set(value: str) -> frozenset[str]:
    parts = {part.strip() for part in value.split(",") if part.strip()}
    if not parts or any(
        re.fullmatch(r"(?:<=|>=|<|>|==|!=)[0-9]+(?:[.][0-9]+)*", part) is None
        for part in parts
    ):
        _fail("artifact.metadata")
    return frozenset(parts)


def load_manifest(repository_root: Path) -> ReleaseManifest:
    raw = _parse_json(
        _read_bounded_file(repository_root / MANIFEST_PATH, MAX_MANIFEST_BYTES, "manifest.read"),
        "manifest.invalid",
    )
    manifest = _expect_object(raw, "manifest.invalid")
    _require_exact_keys(
        manifest,
        {
            "artifact_source_commit",
            "artifacts",
            "dependencies",
            "distribution",
            "import_package",
            "manifest_schema_version",
            "python_requires",
            "repository",
            "request_schema_version",
            "tag",
            "version",
        },
        "manifest.invalid",
    )
    if _expect_integer(manifest["manifest_schema_version"], "manifest.invalid") != 1:
        _fail("manifest.invalid")
    repository = _expect_string(manifest["repository"], "manifest.invalid")
    distribution = _expect_string(manifest["distribution"], "manifest.invalid")
    import_package = _expect_string(manifest["import_package"], "manifest.invalid")
    version = _expect_string(manifest["version"], "manifest.invalid")
    tag = _expect_string(manifest["tag"], "manifest.invalid")
    source_commit = _expect_string(manifest["artifact_source_commit"], "manifest.invalid")
    request_schema_version = _expect_integer(
        manifest["request_schema_version"], "manifest.invalid"
    )
    python_requires = _expect_string(manifest["python_requires"], "manifest.invalid")
    if (
        repository != EXPECTED_REPOSITORY
        or distribution != "healthassure-messaging"
        or import_package != "healthassure_messaging"
        or version != "1.0.0"
        or tag != "v1.0.0"
        or HEX_SHA_PATTERN.fullmatch(source_commit) is None
        or source_commit != "fbc9916ee2b714f0edb29a5e503d0f3f72d223cb"
        or request_schema_version != 1
        or _specifier_set(python_requires) != _specifier_set(">=3.10,<3.14")
    ):
        _fail("manifest.invalid")

    dependencies = _expect_object(manifest["dependencies"], "manifest.invalid")
    _require_exact_keys(dependencies, {"runtime", "optional"}, "manifest.invalid")
    runtime = tuple(
        _expect_string(item, "manifest.invalid")
        for item in _expect_list(dependencies["runtime"], "manifest.invalid")
    )
    optional_raw = _expect_object(dependencies["optional"], "manifest.invalid")
    _require_exact_keys(optional_raw, {"dev", "mongo"}, "manifest.invalid")
    dev = tuple(
        _expect_string(item, "manifest.invalid")
        for item in _expect_list(optional_raw["dev"], "manifest.invalid")
    )
    mongo = tuple(
        _expect_string(item, "manifest.invalid")
        for item in _expect_list(optional_raw["mongo"], "manifest.invalid")
    )
    expected_dev = (
        "build>=1.5,<2",
        "check-wheel-contents>=0.6,<1",
        "mypy>=2.3,<3",
        "ruff>=0.16,<0.17",
        "twine>=6.2,<7",
        "types-requests>=2.33.0.20260712,<3",
    )
    if (
        runtime != ("requests>=2.32.3,<3",)
        or dev != expected_dev
        or mongo != ("pymongo>=4.11.1,<5",)
    ):
        _fail("manifest.invalid")

    artifacts: list[ArtifactSpec] = []
    for item in _expect_list(manifest["artifacts"], "manifest.invalid"):
        artifact = _expect_object(item, "manifest.invalid")
        _require_exact_keys(
            artifact, {"filename", "kind", "sha256", "size"}, "manifest.invalid"
        )
        kind = _expect_string(artifact["kind"], "manifest.invalid")
        filename = _expect_string(artifact["filename"], "manifest.invalid")
        size = _expect_integer(artifact["size"], "manifest.invalid", minimum=1)
        sha256 = _expect_string(artifact["sha256"], "manifest.invalid")
        if kind not in {"wheel", "sdist"} or HEX_DIGEST_PATTERN.fullmatch(sha256) is None:
            _fail("manifest.invalid")
        artifacts.append(ArtifactSpec(kind, filename, size, sha256))
    expected_artifacts = (
        ArtifactSpec(
            "wheel",
            "healthassure_messaging-1.0.0-py3-none-any.whl",
            42_333,
            "f83d08696d27faa58f75d9f88e844bffd6f5fcb7099acfe1120b4c7b56bf8dc8",
        ),
        ArtifactSpec(
            "sdist",
            "healthassure_messaging-1.0.0.tar.gz",
            74_346,
            "8c4bcba46e61fc78f34b01a2d7aea760426fa144cd16376014a85df5ab6b17fa",
        ),
    )
    if tuple(artifacts) != expected_artifacts:
        _fail("manifest.invalid")
    return ReleaseManifest(
        repository=repository,
        distribution=distribution,
        import_package=import_package,
        version=version,
        tag=tag,
        artifact_source_commit=source_commit,
        request_schema_version=request_schema_version,
        python_requires=python_requires,
        runtime_dependencies=runtime,
        optional_dependencies={"dev": dev, "mongo": mongo},
        artifacts=tuple(artifacts),
    )


def default_git_runner(repository_root: Path, arguments: tuple[str, ...], limit: int) -> GitResult:
    failed = False
    completed: subprocess.CompletedProcess[bytes] | None = None
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=repository_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if limit else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed or completed is None:
        _fail("git.unavailable")
    output = completed.stdout or b""
    if len(output) > limit:
        _fail("git.output")
    return GitResult(completed.returncode, output)


def _commit_from_result(result: GitResult, code: str) -> str:
    if result.returncode != 0:
        _fail(code)
    failed = False
    value = ""
    try:
        value = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        failed = True
    if failed or HEX_SHA_PATTERN.fullmatch(value) is None:
        _fail(code)
    return value


def _quiet_result(result: GitResult, expected: int, code: str) -> None:
    if result.stdout or result.returncode != expected:
        _fail(code)


def verify_git_trust(
    repository_root: Path,
    manifest: ReleaseManifest,
    workflow_sha: str,
    runner: GitRunner = default_git_runner,
) -> str:
    if HEX_SHA_PATTERN.fullmatch(workflow_sha) is None:
        _fail("context.invalid")
    expected_parent = manifest.artifact_source_commit
    control_paths = RELEASE_CONTROL_PATHS
    tag_commit = _commit_from_result(
        runner(
            repository_root,
            ("rev-parse", "--verify", "--quiet", f"refs/tags/{manifest.tag}^{{commit}}"),
            41,
        ),
        "git.tag",
    )
    head_commit = _commit_from_result(
        runner(repository_root, ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"), 41),
        "git.checkout",
    )
    if tag_commit != workflow_sha or head_commit != workflow_sha:
        _fail("git.checkout")
    parent = _commit_from_result(
        runner(
            repository_root,
            ("rev-parse", "--verify", "--quiet", f"{workflow_sha}^1^{{commit}}"),
            41,
        ),
        "git.release_control_parent",
    )
    if parent != expected_parent:
        _fail("git.release_control_parent")
    second_parent = runner(
        repository_root,
        ("rev-parse", "--verify", "--quiet", f"{workflow_sha}^2^{{commit}}"),
        0,
    )
    if second_parent.stdout:
        _fail("git.output")
    if second_parent.returncode == 0:
        _fail("git.release_control_parent")
    if second_parent.returncode != 1:
        _fail("git.unavailable")

    for path in control_paths:
        changed = runner(
            repository_root,
            (
                "diff",
                "--quiet",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                parent,
                workflow_sha,
                "--",
                path,
            ),
            0,
        )
        _quiet_result(changed, 1, "git.release_control_paths")
    exclusions = tuple(f":(top,exclude,literal){path}" for path in control_paths)
    other_changes = runner(
        repository_root,
        (
            "diff",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            parent,
            workflow_sha,
            "--",
            ".",
            *exclusions,
        ),
        0,
    )
    _quiet_result(other_changes, 0, "git.release_control_paths")

    for path in control_paths:
        entry = runner(repository_root, ("ls-tree", "-z", workflow_sha, "--", path), 512)
        if entry.returncode != 0:
            _fail("git.release_control_entry")
        match = re.fullmatch(rb"100644 blob ([0-9a-f]{40})\t([^\0]+)\0", entry.stdout)
        if match is None or match.group(2) != path.encode("utf-8"):
            _fail("git.release_control_entry")
        checkout = runner(
            repository_root,
            ("diff", "--quiet", "--no-ext-diff", "--no-textconv", workflow_sha, "--", path),
            0,
        )
        _quiet_result(checkout, 0, "git.checkout")
    return workflow_sha


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirects = 0

    def redirect_request(  # type: ignore[override]
        self,
        request: urllib.request.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> urllib.request.Request | None:
        original = urllib.parse.urlsplit(request.full_url)
        target = urllib.parse.urlsplit(new_url)
        self.redirects += 1
        if (
            self.redirects != 1
            or code not in {301, 302, 303, 307, 308}
            or not _is_safe_https_endpoint(original, "github.com")
            or not _is_safe_https_endpoint(
                target, "release-assets.githubusercontent.com"
            )
        ):
            return None
        return super().redirect_request(
            request,
            cast(Any, file_pointer),
            code,
            message,
            cast(Any, headers),
            new_url,
        )


def _is_safe_https_endpoint(parts: urllib.parse.SplitResult, hostname: str) -> bool:
    invalid_port = False
    port: int | None = None
    try:
        port = parts.port
    except ValueError:
        invalid_port = True
    return (
        not invalid_port
        and parts.scheme == "https"
        and parts.hostname == hostname
        and parts.username is None
        and parts.password is None
        and port is None
        and not parts.fragment
    )


def _network_failure_code(url: str) -> str:
    hostname = urllib.parse.urlsplit(url).hostname
    if hostname == "github.com":
        return "network.release_asset"
    if hostname in {"pypi.org", "files.pythonhosted.org"}:
        return "network.pypi"
    return "network.unavailable"


def default_fetch(url: str, limit: int, allow_not_found: bool) -> HttpResult:
    failed = False
    not_found = False
    status = 0
    body = b""
    try:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "frozen-release-verifier/1"},
        )
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            status = response.status
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > limit:
                failed = True
            else:
                body = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        if allow_not_found and error.code == 404:
            not_found = True
        else:
            failed = True
    except (urllib.error.URLError, OSError, ValueError, TimeoutError, HTTPException):
        failed = True
    if not_found:
        return HttpResult(404, b"")
    if failed or status != 200 or len(body) > limit:
        _fail(_network_failure_code(url))
    return HttpResult(status, body)


def _validate_release_download_url(url: str, manifest: ReleaseManifest, filename: str) -> None:
    parts = urllib.parse.urlsplit(url)
    expected_path = (
        f"/{manifest.repository}/releases/download/{manifest.tag}/"
        f"{urllib.parse.quote(filename)}"
    )
    if (
        parts.scheme != "https"
        or parts.hostname != "github.com"
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        or parts.path != expected_path
        or parts.query
        or parts.fragment
    ):
        _fail("release.asset")


def _release_assets_from_record(
    release: Mapping[str, object],
    manifest: ReleaseManifest,
) -> tuple[ReleaseAsset, ...]:
    if (
        _expect_boolean(release.get("draft"), "release.invalid")
        or _expect_boolean(release.get("prerelease"), "release.invalid")
        or _expect_string(release.get("tag_name"), "release.invalid") != manifest.tag
        or not _expect_string(release.get("published_at"), "release.invalid")
    ):
        _fail("release.invalid")
    expected = {artifact.filename: artifact for artifact in manifest.artifacts}
    assets: list[ReleaseAsset] = []
    for raw_asset in _expect_list(release.get("assets"), "release.invalid"):
        asset = _expect_object(raw_asset, "release.asset")
        name = _expect_string(asset.get("name"), "release.asset")
        size = _expect_integer(asset.get("size"), "release.asset", minimum=1)
        state = _expect_string(asset.get("state"), "release.asset")
        download_url = _expect_string(asset.get("browser_download_url"), "release.asset")
        spec = expected.get(name)
        if spec is None or size != spec.size or state != "uploaded":
            _fail("release.asset")
        _validate_release_download_url(download_url, manifest, name)
        assets.append(ReleaseAsset(name, size, download_url))
    if len(assets) != len(expected) or {asset.name for asset in assets} != set(expected):
        _fail("release.asset")
    return tuple(sorted(assets, key=lambda item: item.name))


def verify_release_event(
    event_path: Path,
    manifest: ReleaseManifest,
    *,
    event_name: str,
    repository: str,
    ref: str,
) -> tuple[ReleaseAsset, ...]:
    if (
        event_name != "release"
        or repository != manifest.repository
        or ref != f"refs/tags/{manifest.tag}"
    ):
        _fail("context.invalid")
    payload = _expect_object(
        _parse_json(
            _read_bounded_file(event_path, MAX_EVENT_BYTES, "release.event"),
            "release.event",
        ),
        "release.event",
    )
    if _expect_string(payload.get("action"), "release.invalid") != "published":
        _fail("release.invalid")
    repository_payload = _expect_object(payload.get("repository"), "release.invalid")
    if (
        _expect_string(repository_payload.get("full_name"), "release.invalid")
        != manifest.repository
    ):
        _fail("release.invalid")
    release = _expect_object(payload.get("release"), "release.invalid")
    return _release_assets_from_record(release, manifest)


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not name.startswith(("/", "\\")) and ".." not in path.parts


def _archive_disclosure_is_present(entries: Mapping[str, bytes]) -> bool:
    for name, content in entries.items():
        path = PurePosixPath(name)
        if path.suffix not in TEXT_ARCHIVE_SUFFIXES and path.name not in {
            "LICENSE",
            "METADATA",
            "NOTICE",
            "PKG-INFO",
        }:
            continue
        failed = False
        text = ""
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            failed = True
        if failed or any(pattern.search(text) for pattern in DISCLOSURE_PATTERNS):
            return True
    return False


def _requirement_signature(value: str) -> tuple[str, frozenset[str], str]:
    match = REQUIREMENT_PATTERN.fullmatch(value.strip())
    if match is None:
        _fail("artifact.metadata")
    name = match.group(1).lower().replace("_", "-")
    specifiers = _specifier_set(match.group(2).replace(" ", ""))
    marker = (match.group(3) or "").lower()
    marker = re.sub(r"[\s\"']", "", marker)
    return name, specifiers, marker


def _verify_metadata(metadata_content: bytes, manifest: ReleaseManifest) -> None:
    failed = False
    metadata: Message | None = None
    try:
        metadata = email.message_from_bytes(metadata_content)
    except (UnicodeDecodeError, ValueError):
        failed = True
    if failed or metadata is None:
        _fail("artifact.metadata")
    if (
        metadata.get("Name") != manifest.distribution
        or metadata.get("Version") != manifest.version
        or metadata.get("License-Expression") != "Apache-2.0"
        or _specifier_set(metadata.get("Requires-Python", ""))
        != _specifier_set(manifest.python_requires)
        or set(metadata.get_all("License-File", [])) != {"LICENSE", "NOTICE"}
        or set(metadata.get_all("Provides-Extra", []))
        != set(manifest.optional_dependencies)
    ):
        _fail("artifact.metadata")
    observed = {
        _requirement_signature(requirement)
        for requirement in metadata.get_all("Requires-Dist", [])
    }
    expected = {
        _requirement_signature(requirement)
        for requirement in manifest.runtime_dependencies
    }
    for extra, requirements in manifest.optional_dependencies.items():
        expected.update(
            {
                (*_requirement_signature(requirement)[:2], f"extra=={extra}")
                for requirement in requirements
            }
        )
    if observed != expected:
        _fail("artifact.metadata")


def _verify_request_schema(content: bytes, expected_version: int) -> None:
    failed = False
    module: ast.Module | None = None
    try:
        module = ast.parse(content.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError):
        failed = True
    if failed or module is None:
        _fail("artifact.schema")
    values: list[object] = []
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "REQUEST_SCHEMA_VERSION"
            for target in statement.targets
        ) and isinstance(statement.value, ast.Constant):
            values.append(statement.value.value)
    if values != [expected_version]:
        _fail("artifact.schema")


def _wheel_entries(content: bytes) -> dict[str, bytes]:
    failed = False
    entries: dict[str, bytes] = {}
    observed: set[str] = set()
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            information = archive.infolist()
            if len(information) > MAX_ARCHIVE_ENTRIES:
                _fail("artifact.archive")
            total = 0
            for item in information:
                if not _safe_archive_name(item.filename):
                    _fail("artifact.archive")
                if item.filename in observed:
                    _fail("artifact.archive")
                observed.add(item.filename)
                if item.is_dir():
                    continue
                mode = (item.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    _fail("artifact.archive")
                total += item.file_size
                if total > MAX_ARCHIVE_CONTENT_BYTES:
                    _fail("artifact.archive")
                entries[item.filename] = archive.read(item)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        failed = True
    if failed:
        _fail("artifact.archive")
    return entries


def _sdist_entries(content: bytes) -> dict[str, bytes]:
    failed = False
    entries: dict[str, bytes] = {}
    observed: set[str] = set()
    try:
        with tarfile.open(fileobj=BytesIO(content), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                _fail("artifact.archive")
            total = 0
            for member in members:
                if not _safe_archive_name(member.name) or member.issym() or member.islnk():
                    _fail("artifact.archive")
                if member.name in observed:
                    _fail("artifact.archive")
                observed.add(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    _fail("artifact.archive")
                total += member.size
                if total > MAX_ARCHIVE_CONTENT_BYTES:
                    _fail("artifact.archive")
                extracted = archive.extractfile(member)
                if extracted is None:
                    _fail("artifact.archive")
                entries[member.name] = extracted.read(member.size + 1)
                if len(entries[member.name]) != member.size:
                    _fail("artifact.archive")
    except (OSError, EOFError, tarfile.TarError):
        failed = True
    if failed:
        _fail("artifact.archive")
    return entries


def _verify_wheel(content: bytes, manifest: ReleaseManifest) -> None:
    entries = _wheel_entries(content)
    metadata_names = [name for name in entries if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        _fail("artifact.metadata")
    metadata_name = metadata_names[0]
    dist_info = metadata_name.removesuffix("METADATA")
    expected_legal = {f"{dist_info}licenses/LICENSE", f"{dist_info}licenses/NOTICE"}
    if not expected_legal <= set(entries):
        _fail("artifact.legal")
    typed_path = f"{manifest.import_package}/py.typed"
    schema_path = f"{manifest.import_package}/serialization.py"
    if typed_path not in entries or schema_path not in entries:
        _fail("artifact.typing")
    _verify_metadata(entries[metadata_name], manifest)
    _verify_request_schema(entries[schema_path], manifest.request_schema_version)
    if _archive_disclosure_is_present(entries):
        _fail("artifact.disclosure")


def _verify_sdist(content: bytes, manifest: ReleaseManifest) -> None:
    entries = _sdist_entries(content)
    expected_root = f"healthassure_messaging-{manifest.version}"
    roots = {name.split("/", 1)[0] for name in entries}
    if roots != {expected_root}:
        _fail("artifact.archive")
    metadata_path = f"{expected_root}/PKG-INFO"
    legal_paths = {f"{expected_root}/LICENSE", f"{expected_root}/NOTICE"}
    typed_path = f"{expected_root}/src/{manifest.import_package}/py.typed"
    schema_path = f"{expected_root}/src/{manifest.import_package}/serialization.py"
    if metadata_path not in entries or not legal_paths <= set(entries):
        _fail("artifact.legal")
    if typed_path not in entries or schema_path not in entries:
        _fail("artifact.typing")
    _verify_metadata(entries[metadata_path], manifest)
    _verify_request_schema(entries[schema_path], manifest.request_schema_version)
    if _archive_disclosure_is_present(entries):
        _fail("artifact.disclosure")


def verify_artifact(content: bytes, spec: ArtifactSpec, manifest: ReleaseManifest) -> None:
    if len(content) != spec.size or hashlib.sha256(content).hexdigest() != spec.sha256:
        _fail("artifact.digest")
    if spec.kind == "wheel":
        _verify_wheel(content, manifest)
    elif spec.kind == "sdist":
        _verify_sdist(content, manifest)
    else:
        _fail("manifest.invalid")


def _ensure_empty_artifact_directory(path: Path) -> None:
    failed = False
    try:
        if path.exists():
            if path.is_symlink() or not path.is_dir() or next(path.iterdir(), None) is not None:
                failed = True
        else:
            path.mkdir(mode=0o700)
    except OSError:
        failed = True
    if failed:
        _fail("artifact.directory")


def _write_new_file(path: Path, content: bytes) -> None:
    failed = False
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except OSError:
        failed = True
    if failed:
        _fail("artifact.directory")


def download_release_assets(
    directory: Path,
    assets: Sequence[ReleaseAsset],
    manifest: ReleaseManifest,
    fetcher: Fetcher = default_fetch,
) -> None:
    _ensure_empty_artifact_directory(directory)
    asset_by_name = {asset.name: asset for asset in assets}
    for spec in manifest.artifacts:
        asset = asset_by_name.get(spec.filename)
        if asset is None:
            _fail("release.asset")
        result = fetcher(asset.download_url, spec.size, False)
        if result.status != 200:
            _fail("network.unavailable")
        verify_artifact(result.body, spec, manifest)
        _write_new_file(directory / spec.filename, result.body)


def verify_local_artifacts(directory: Path, manifest: ReleaseManifest) -> None:
    failed = False
    observed: set[str] = set()
    try:
        if directory.is_symlink() or not directory.is_dir():
            failed = True
        else:
            observed = {entry.name for entry in directory.iterdir()}
    except OSError:
        failed = True
    expected = {artifact.filename for artifact in manifest.artifacts}
    if failed or observed != expected:
        _fail("artifact.directory")
    for spec in manifest.artifacts:
        content = _read_bounded_file(directory / spec.filename, spec.size, "artifact.read")
        verify_artifact(content, spec, manifest)


def stage_publisher_artifacts(
    release_directory: Path,
    publisher_directory: Path,
    manifest: ReleaseManifest,
) -> None:
    verify_local_artifacts(release_directory, manifest)
    _ensure_empty_artifact_directory(publisher_directory)
    for spec in manifest.artifacts:
        content = _read_bounded_file(
            release_directory / spec.filename,
            spec.size,
            "artifact.read",
        )
        verify_artifact(content, spec, manifest)
        _write_new_file(publisher_directory / spec.filename, content)
    verify_local_artifacts(publisher_directory, manifest)


def _validate_pypi_file_url(url: str, filename: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "files.pythonhosted.org"
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        or urllib.parse.unquote(PurePosixPath(parts.path).name) != filename
        or parts.query
        or parts.fragment
    ):
        _fail("pypi.state")


def inspect_pypi(manifest: ReleaseManifest, fetcher: Fetcher = default_fetch) -> PyPIState:
    response = fetcher(PYPI_VERSION_URL, MAX_PYPI_JSON_BYTES, True)
    if response.status == 404:
        return PyPIState.ABSENT
    if response.status != 200:
        _fail("pypi.unavailable")
    payload = _expect_object(_parse_json(response.body, "pypi.state"), "pypi.state")
    urls = _expect_list(payload.get("urls"), "pypi.state")
    if len(urls) != len(manifest.artifacts):
        _fail("pypi.state")
    expected = {artifact.filename: artifact for artifact in manifest.artifacts}
    observed: set[str] = set()
    for raw_file in urls:
        file_data = _expect_object(raw_file, "pypi.state")
        filename = _expect_string(file_data.get("filename"), "pypi.state")
        spec = expected.get(filename)
        if spec is None or filename in observed:
            _fail("pypi.state")
        observed.add(filename)
        size = _expect_integer(file_data.get("size"), "pypi.state", minimum=1)
        digests = _expect_object(file_data.get("digests"), "pypi.state")
        sha256 = _expect_string(digests.get("sha256"), "pypi.state")
        packagetype = _expect_string(file_data.get("packagetype"), "pypi.state")
        url = _expect_string(file_data.get("url"), "pypi.state")
        yanked = _expect_boolean(file_data.get("yanked"), "pypi.state")
        expected_type = "bdist_wheel" if spec.kind == "wheel" else "sdist"
        if size != spec.size or sha256 != spec.sha256 or packagetype != expected_type or yanked:
            _fail("pypi.state")
        _validate_pypi_file_url(url, filename)
        artifact_response = fetcher(url, spec.size, False)
        if artifact_response.status != 200:
            _fail("pypi.unavailable")
        verify_artifact(artifact_response.body, spec, manifest)
    if observed != set(expected):
        _fail("pypi.state")
    return PyPIState.EXACT


def verify_pypi_postflight(
    manifest: ReleaseManifest,
    fetcher: Fetcher = default_fetch,
    sleeper: Sleeper = time.sleep,
    *,
    attempts: int = POSTFLIGHT_ATTEMPTS,
    delay_seconds: float = POSTFLIGHT_DELAY_SECONDS,
) -> None:
    if attempts < 1 or attempts > POSTFLIGHT_ATTEMPTS or delay_seconds < 0:
        _fail("pypi.poll")
    for attempt in range(attempts):
        state = inspect_pypi(manifest, fetcher)
        if state is PyPIState.EXACT:
            return
        if attempt + 1 < attempts:
            sleeper(delay_seconds)
    _fail("pypi.not_exact")


def _write_github_output(path: Path, *, publish_needed: bool, state: str) -> None:
    failed = False
    try:
        if path.is_symlink() or not path.is_file():
            failed = True
        else:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"publish_needed={'true' if publish_needed else 'false'}\n")
                handle.write(f"preflight_state={state}\n")
    except OSError:
        failed = True
    if failed:
        _fail("output.invalid")


def _validated_artifact_directory(
    repository_root: Path,
    value: str,
    expected_name: str,
) -> Path:
    failed = False
    root = Path()
    directory = Path()
    try:
        candidate = Path(value)
        if (
            not value
            or candidate.is_absolute()
            or candidate.parts != (expected_name,)
        ):
            failed = True
        else:
            root = repository_root.resolve()
            directory = (root / candidate).resolve()
    except (OSError, RuntimeError):
        failed = True
    if (
        failed
        or directory.parent != root
        or directory.name != expected_name
        or directory == root
    ):
        _fail("artifact.directory")
    return directory


def build_release_context(
    repository_root: Path,
    *,
    event_name: str,
    event_path: Path,
    repository: str,
    ref: str,
    workflow_sha: str,
    runner: GitRunner = default_git_runner,
) -> ReleaseContext:
    manifest = load_manifest(repository_root)
    if repository != EXPECTED_REPOSITORY or event_name != "release":
        _fail("context.invalid")
    control_commit = verify_git_trust(
        repository_root,
        manifest,
        workflow_sha,
        runner,
    )
    assets = verify_release_event(
        event_path,
        manifest,
        event_name=event_name,
        repository=repository,
        ref=ref,
    )
    return ReleaseContext(manifest, assets, control_commit)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("mode", choices=("prepare", "postflight"))
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event-path", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--artifact-directory", required=True)
    parser.add_argument("--publisher-directory")
    parser.add_argument("--github-output")
    return parser


def _run(arguments: Sequence[str]) -> int:
    options = _argument_parser().parse_args(arguments)
    repository_root = Path(options.repository_root)
    context = build_release_context(
        repository_root,
        event_name=options.event_name,
        event_path=Path(options.event_path),
        repository=options.repository,
        ref=options.ref,
        workflow_sha=options.workflow_sha,
    )
    artifact_directory = _validated_artifact_directory(
        repository_root,
        options.artifact_directory,
        "release-assets",
    )
    if options.mode == "prepare":
        if not options.github_output or not options.publisher_directory:
            _fail("output.invalid")
        download_release_assets(artifact_directory, context.assets, context.manifest)
        state = inspect_pypi(context.manifest)
        publish_needed = state is PyPIState.ABSENT
        if publish_needed:
            publisher_directory = _validated_artifact_directory(
                repository_root,
                options.publisher_directory,
                "publisher-assets",
            )
            stage_publisher_artifacts(
                artifact_directory,
                publisher_directory,
                context.manifest,
            )
        _write_github_output(
            Path(options.github_output),
            publish_needed=publish_needed,
            state="eligible" if publish_needed else "exact_existing",
        )
        print(
            "RELEASE_CONTROL_PREFLIGHT "
            + ("ELIGIBLE" if publish_needed else "EXACT_EXISTING")
        )
        return 0
    if options.github_output or options.publisher_directory:
        _fail("output.invalid")
    verify_local_artifacts(artifact_directory, context.manifest)
    verify_pypi_postflight(context.manifest)
    print("RELEASE_CONTROL_POSTFLIGHT EXACT")
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    error_code = ""
    try:
        return _run(sys.argv[1:] if arguments is None else arguments)
    except VerificationError as error:
        error_code = error.code
    except Exception:
        error_code = "verification.internal"
    print(f"RELEASE_CONTROL_ERROR {error_code}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
