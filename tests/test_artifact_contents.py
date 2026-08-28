from __future__ import annotations

import email
import io
import os
import re
import tarfile
import tempfile
import unittest
import zipfile
from collections.abc import Mapping
from pathlib import Path

ARTIFACT_VERSION_ENV = "HEALTHASSURE_MESSAGING_ARTIFACT_VERSION"
ARTIFACT_DIRECTORY_ENV = "HEALTHASSURE_MESSAGING_ARTIFACT_DIR"
ARTIFACT_QUALIFICATION_REQUESTED = any(
    name in os.environ for name in (ARTIFACT_VERSION_ENV, ARTIFACT_DIRECTORY_ENV)
)
PUBLIC_ARCHIVE_SUFFIXES = {"", ".in", ".md", ".py", ".toml", ".typed"}
ARTIFACT_DISCLOSURE_PATTERNS = (
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


def select_frozen_artifacts(environment: Mapping[str, str]) -> tuple[str, Path, Path] | None:
    version = environment.get(ARTIFACT_VERSION_ENV, "").strip()
    directory = environment.get(ARTIFACT_DIRECTORY_ENV, "").strip()
    if not version and not directory:
        return None
    if not version or not directory:
        raise AssertionError("artifact qualification requires both version and directory inputs")
    if re.fullmatch(r"[0-9]+(?:[.][0-9A-Za-z]+)+", version) is None:
        raise AssertionError("artifact qualification version is invalid")
    root = Path(directory)
    wheel = root / f"healthassure_messaging-{version}-py3-none-any.whl"
    sdist = root / f"healthassure_messaging-{version}.tar.gz"
    return version, wheel, sdist


def require_frozen_artifact(path: Path, artifact_kind: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise AssertionError(f"expected frozen {artifact_kind} is absent")


def current_frozen_artifacts() -> tuple[str, Path, Path]:
    selection = select_frozen_artifacts(os.environ)
    if selection is None:
        raise AssertionError("artifact qualification inputs were not supplied")
    return selection


def artifact_disclosure_categories(text: str) -> tuple[str, ...]:
    return tuple(
        name for name, pattern in ARTIFACT_DISCLOSURE_PATTERNS if pattern.search(text)
    )


def _disclosures(entries: dict[str, bytes]) -> tuple[tuple[str, str], ...]:
    findings: list[tuple[str, str]] = []
    for name, content in sorted(entries.items()):
        if Path(name).suffix not in PUBLIC_ARCHIVE_SUFFIXES:
            continue
        text = content.decode("utf-8", errors="strict")
        findings.extend((name, category) for category in artifact_disclosure_categories(text))
    return tuple(findings)


def wheel_disclosures(path: Path) -> tuple[tuple[str, str], ...]:
    with zipfile.ZipFile(path) as archive:
        entries = {
            name: archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/")
        }
    return _disclosures(entries)


def sdist_disclosures(path: Path) -> tuple[tuple[str, str], ...]:
    entries: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AssertionError("sdist contains an unreadable file")
            entries[member.name] = extracted.read()
    return _disclosures(entries)


def verify_wheel_legal_files(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        distribution_root = metadata_name.removesuffix("METADATA")
        expected = {
            distribution_root + "licenses/LICENSE",
            distribution_root + "licenses/NOTICE",
        }
        if not expected <= names:
            raise AssertionError("wheel is missing required legal files")
        metadata = email.message_from_bytes(archive.read(metadata_name))
        if set(metadata.get_all("License-File", [])) != {"LICENSE", "NOTICE"}:
            raise AssertionError("wheel metadata is missing required legal files")


def verify_wheel_version(path: Path, expected_version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise AssertionError("wheel metadata inventory is invalid")
        metadata = email.message_from_bytes(archive.read(metadata_names[0]))
    if metadata.get("Version") != expected_version:
        raise AssertionError("wheel metadata version does not match qualification input")


def verify_sdist_legal_files(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = {member.name for member in archive.getmembers() if member.isfile()}
    roots = {name.split("/", 1)[0] for name in names}
    if len(roots) != 1:
        raise AssertionError("sdist must have exactly one archive root")
    root = next(iter(roots))
    if {f"{root}/LICENSE", f"{root}/NOTICE"} - names:
        raise AssertionError("sdist is missing required legal files")


def verify_sdist_version(path: Path, expected_version: str) -> None:
    with tarfile.open(path, "r:gz") as archive:
        roots = {member.name.split("/", 1)[0] for member in archive.getmembers()}
    if roots != {f"healthassure_messaging-{expected_version}"}:
        raise AssertionError("sdist version does not match qualification input")


class ArtifactLegalFileTests(unittest.TestCase):
    def test_artifact_selection_is_disabled_without_inputs(self) -> None:
        self.assertIsNone(select_frozen_artifacts({}))

    def test_artifact_selection_requires_both_inputs(self) -> None:
        cases = (
            {ARTIFACT_VERSION_ENV: "9.8.7.dev6"},
            {ARTIFACT_DIRECTORY_ENV: "/synthetic/artifacts"},
        )
        for environment in cases:
            with self.subTest(environment=environment), self.assertRaisesRegex(
                AssertionError,
                "requires both version and directory",
            ):
                select_frozen_artifacts(environment)

    def test_artifact_selection_uses_only_the_explicit_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for older_version in ("1.0.0.dev1", "1.0.0.dev2", "1.0.0.dev3"):
                (root / f"healthassure_messaging-{older_version}-py3-none-any.whl").touch()
                (root / f"healthassure_messaging-{older_version}.tar.gz").touch()
            selection = select_frozen_artifacts(
                {
                    ARTIFACT_VERSION_ENV: "9.8.7.dev6",
                    ARTIFACT_DIRECTORY_ENV: str(root),
                }
            )
            self.assertEqual(
                selection,
                (
                    "9.8.7.dev6",
                    root / "healthassure_messaging-9.8.7.dev6-py3-none-any.whl",
                    root / "healthassure_messaging-9.8.7.dev6.tar.gz",
                ),
            )

    def test_missing_explicit_artifacts_fail_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selection = select_frozen_artifacts(
                {
                    ARTIFACT_VERSION_ENV: "9.8.7.dev6",
                    ARTIFACT_DIRECTORY_ENV: str(root),
                }
            )
            assert selection is not None
            _, wheel, sdist = selection
            with self.assertRaisesRegex(AssertionError, "expected frozen wheel is absent"):
                require_frozen_artifact(wheel, "wheel")
            wheel.touch()
            with self.assertRaisesRegex(AssertionError, "expected frozen sdist is absent"):
                require_frozen_artifact(sdist, "sdist")

    def test_artifact_disclosure_scanners_detect_synthetic_infrastructure(self) -> None:
        unsafe = "https://" + "example-region-python" + "." + "pkg" + "." + "dev/simple/"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wheel = root / "synthetic.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("sample/module.py", f'ENDPOINT = "{unsafe}"\n')
            self.assertEqual(wheel_disclosures(wheel), (("sample/module.py", "gar-host"),))

            sdist = root / "synthetic.tar.gz"
            content = f'ENDPOINT = "{unsafe}"\n'.encode()
            with tarfile.open(sdist, "w:gz") as archive:
                information = tarfile.TarInfo("sample-1.0/sample/module.py")
                information.size = len(content)
                archive.addfile(information, io.BytesIO(content))
            self.assertEqual(
                sdist_disclosures(sdist),
                (("sample-1.0/sample/module.py", "gar-host"),),
            )

    def test_wheel_legal_file_verifier_requires_license_and_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            wheel = Path(temporary_directory) / "synthetic.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("sample-1.0.dist-info/licenses/LICENSE", "license")
                archive.writestr("sample-1.0.dist-info/licenses/NOTICE", "notice")
                archive.writestr(
                    "sample-1.0.dist-info/METADATA",
                    "Metadata-Version: 2.4\nLicense-File: LICENSE\nLicense-File: NOTICE\n",
                )
            verify_wheel_legal_files(wheel)

            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("sample-1.0.dist-info/licenses/LICENSE", "license")
                archive.writestr(
                    "sample-1.0.dist-info/METADATA",
                    "Metadata-Version: 2.4\nLicense-File: LICENSE\n",
                )
            with self.assertRaisesRegex(AssertionError, "missing required legal files"):
                verify_wheel_legal_files(wheel)

    def test_sdist_legal_file_verifier_requires_license_and_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sdist = Path(temporary_directory) / "synthetic.tar.gz"
            with tarfile.open(sdist, "w:gz") as archive:
                for name, content in (("LICENSE", b"license"), ("NOTICE", b"notice")):
                    information = tarfile.TarInfo(f"sample-1.0/{name}")
                    information.size = len(content)
                    archive.addfile(information, io.BytesIO(content))
            verify_sdist_legal_files(sdist)

            with tarfile.open(sdist, "w:gz") as archive:
                content = b"license"
                information = tarfile.TarInfo("sample-1.0/LICENSE")
                information.size = len(content)
                archive.addfile(information, io.BytesIO(content))
            with self.assertRaisesRegex(AssertionError, "missing required legal files"):
                verify_sdist_legal_files(sdist)

    @unittest.skipUnless(
        ARTIFACT_QUALIFICATION_REQUESTED,
        "frozen artifact qualification inputs were not supplied",
    )
    def test_frozen_wheel_contains_license_and_notice(self) -> None:
        version, wheel, _ = current_frozen_artifacts()
        require_frozen_artifact(wheel, "wheel")
        verify_wheel_version(wheel, version)
        verify_wheel_legal_files(wheel)

    @unittest.skipUnless(
        ARTIFACT_QUALIFICATION_REQUESTED,
        "frozen artifact qualification inputs were not supplied",
    )
    def test_frozen_wheel_has_no_disclosure_signatures(self) -> None:
        version, wheel, _ = current_frozen_artifacts()
        require_frozen_artifact(wheel, "wheel")
        verify_wheel_version(wheel, version)
        self.assertEqual(wheel_disclosures(wheel), ())

    @unittest.skipUnless(
        ARTIFACT_QUALIFICATION_REQUESTED,
        "frozen artifact qualification inputs were not supplied",
    )
    def test_frozen_sdist_contains_license_and_notice(self) -> None:
        version, _, sdist = current_frozen_artifacts()
        require_frozen_artifact(sdist, "sdist")
        verify_sdist_version(sdist, version)
        verify_sdist_legal_files(sdist)

    @unittest.skipUnless(
        ARTIFACT_QUALIFICATION_REQUESTED,
        "frozen artifact qualification inputs were not supplied",
    )
    def test_frozen_sdist_has_no_disclosure_signatures(self) -> None:
        version, _, sdist = current_frozen_artifacts()
        require_frozen_artifact(sdist, "sdist")
        verify_sdist_version(sdist, version)
        self.assertEqual(sdist_disclosures(sdist), ())


if __name__ == "__main__":
    unittest.main()
