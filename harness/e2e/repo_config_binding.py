"""Immutable, trial-level bindings for repository evaluation configuration.

Repository YAML affects both snapshot construction and evaluation semantics.  A
live ``config/<repo>.yaml`` must therefore not be re-read independently for
every milestone in a trial.  This module provides the small, entry-point-neutral
primitive used to resolve a config once, freeze its exact bytes in a trial, and
verify that frozen copy whenever the trial is resumed or evaluated.

The module deliberately does not mutate ``trial_metadata.json`` itself.  A
caller stores ``RepoConfigBinding.to_metadata(...)`` alongside its other trial
metadata and later calls ``load_trial_repo_config_binding(...)``.  Metadata that
predates the binding schema remains explicitly unbound and returns ``None``;
metadata which claims the new schema but has an incomplete or corrupt binding
fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Mapping, Optional

import yaml


REPO_CONFIG_BINDING_SCHEMA_VERSION = 1
TRIAL_METADATA_SCHEMA_VERSION_WITH_REPO_CONFIG_BINDING = 2
TRIAL_REPO_CONFIG_FILENAME = "repo_config.yaml"
EMPTY_REPO_CONFIG_BYTES = b"{}\n"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class RepoConfigBindingError(RuntimeError):
    """The repository config cannot be resolved or its binding is invalid."""


def _validate_repo_name(repo_name: object) -> str:
    if not isinstance(repo_name, str) or not repo_name:
        raise RepoConfigBindingError("repo config binding repo_name must be a non-empty string")
    if (
        repo_name in {".", ".."}
        or "\x00" in repo_name
        or "/" in repo_name
        or "\\" in repo_name
    ):
        raise RepoConfigBindingError(f"unsafe repo config binding repo_name: {repo_name!r}")
    return repo_name


def _validate_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RepoConfigBindingError("repo config binding sha256 must be 64 lowercase hex characters")
    return value


def _sha256(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def _parse_repo_config(raw_bytes: bytes, *, origin: str) -> dict[str, Any]:
    """Parse exactly the bytes which were hashed, requiring a YAML mapping."""
    try:
        value = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise RepoConfigBindingError(f"invalid repository config YAML at {origin}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RepoConfigBindingError(
            f"repository config at {origin} must contain a YAML mapping, got {type(value).__name__}"
        )
    return value


@dataclass(frozen=True)
class RepoConfigIdentity:
    """Relocatable config identity suitable for a snapshot sidecar."""

    repo_name: str
    sha256: str
    schema_version: int = REPO_CONFIG_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != REPO_CONFIG_BINDING_SCHEMA_VERSION
        ):
            raise RepoConfigBindingError(
                "unsupported repo config binding schema_version: "
                f"{self.schema_version!r}"
            )
        object.__setattr__(self, "repo_name", _validate_repo_name(self.repo_name))
        object.__setattr__(self, "sha256", _validate_sha256(self.sha256))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repo_name": self.repo_name,
            "sha256": self.sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "RepoConfigIdentity":
        if not isinstance(value, Mapping):
            raise RepoConfigBindingError("repo config binding identity must be an object")
        version = value.get("schema_version")
        if isinstance(version, bool) or version != REPO_CONFIG_BINDING_SCHEMA_VERSION:
            raise RepoConfigBindingError(
                f"unsupported repo config binding schema_version: {version!r}"
            )
        return cls(
            schema_version=version,
            repo_name=value.get("repo_name"),
            sha256=value.get("sha256"),
        )


@dataclass(frozen=True)
class ResolvedRepoConfig:
    """A live config resolved once and held as immutable source bytes."""

    repo_name: str
    raw_bytes: bytes
    config: Mapping[str, Any]
    source_path: Optional[Path]

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_name", _validate_repo_name(self.repo_name))
        if not isinstance(self.raw_bytes, bytes):
            raise RepoConfigBindingError("resolved repo config raw_bytes must be bytes")
        if not isinstance(self.config, Mapping):
            raise RepoConfigBindingError("resolved repo config must be a mapping")

    @property
    def sha256(self) -> str:
        return _sha256(self.raw_bytes)

    @property
    def identity(self) -> RepoConfigIdentity:
        return RepoConfigIdentity(repo_name=self.repo_name, sha256=self.sha256)


@dataclass(frozen=True)
class RepoConfigBinding:
    """A verified trial-local config and its relocatable identity."""

    path: Path
    raw_bytes: bytes
    config: Mapping[str, Any]
    identity: RepoConfigIdentity
    source_path: Optional[Path] = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_bytes, bytes):
            raise RepoConfigBindingError("bound repo config raw_bytes must be bytes")
        if not isinstance(self.config, Mapping):
            raise RepoConfigBindingError("bound repo config must be a mapping")
        actual = _sha256(self.raw_bytes)
        if actual != self.identity.sha256:
            raise RepoConfigBindingError(
                "bound repo config bytes do not match identity: "
                f"expected {self.identity.sha256}, got {actual}"
            )
        if self.source_path is not None and not isinstance(self.source_path, Path):
            raise RepoConfigBindingError("bound repo config source_path must be a Path")

    @property
    def repo_name(self) -> str:
        return self.identity.repo_name

    @property
    def sha256(self) -> str:
        return self.identity.sha256

    def to_metadata(self, trial_root: Path) -> dict[str, Any]:
        """Return the path-bearing identity stored in ``trial_metadata.json``."""
        root = Path(trial_root).resolve()
        path = Path(self.path).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise RepoConfigBindingError(
                f"bound repo config path is outside trial root: {path}"
            ) from exc
        relative_posix = relative.as_posix()
        _validate_binding_relative_path(relative_posix)
        result = self.identity.to_dict()
        result["path"] = relative_posix
        if self.source_path is not None:
            result["source_path"] = str(self.source_path)
        return result


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_repo_config(
    repo_name: str,
    workspace_root: Path,
    *,
    project_root: Optional[Path] = None,
) -> ResolvedRepoConfig:
    """Resolve and read a repository config exactly once.

    Search order intentionally matches the evaluator's historical behavior:
    the data-root config takes precedence over the project checkout fallback.
    Once the first existing candidate is found, any read or parse error is
    authoritative and fails closed; a broken high-priority file must never
    silently select a lower-priority configuration.

    An absent config is a valid, explicitly empty configuration represented by
    stable bytes so a config created later cannot change the frozen trial.
    """
    name = _validate_repo_name(repo_name)
    workspace = Path(workspace_root).resolve()
    project = Path(project_root).resolve() if project_root is not None else _default_project_root()
    candidates = [
        workspace.parent / "config" / f"{name}.yaml",
        project / "config" / f"{name}.yaml",
    ]

    # Preserve priority while avoiding duplicate work when both roots coincide.
    seen: set[Path] = set()
    for candidate in candidates:
        lexical = candidate.absolute()
        if lexical in seen:
            continue
        seen.add(lexical)
        # ``Path.exists`` is false for a dangling symlink.  Such a config is a
        # broken authoritative candidate, not permission to fall through to a
        # lower-priority file.
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            raw_bytes = candidate.read_bytes()
        except OSError as exc:
            raise RepoConfigBindingError(
                f"cannot read repository config at {candidate}: {exc}"
            ) from exc
        config = _parse_repo_config(raw_bytes, origin=str(candidate))
        return ResolvedRepoConfig(
            repo_name=name,
            raw_bytes=raw_bytes,
            config=config,
            source_path=candidate.resolve(),
        )

    return ResolvedRepoConfig(
        repo_name=name,
        raw_bytes=EMPTY_REPO_CONFIG_BYTES,
        config={},
        source_path=None,
    )


def _atomic_install_without_overwrite(target: Path, raw_bytes: bytes) -> None:
    """Atomically install bytes, reusing only an already-identical target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise RepoConfigBindingError(
            f"frozen repo config must not be a symlink: {target}"
        )
    if target.exists():
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise RepoConfigBindingError(f"cannot read frozen repo config at {target}: {exc}") from exc
        if existing != raw_bytes:
            raise RepoConfigBindingError(
                f"refusing to overwrite a different frozen repo config at {target}"
            )
        return

    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as stream:
            temp_name = stream.name
            stream.write(raw_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hard-link publication is atomic and, unlike os.replace(), cannot
            # overwrite a config installed concurrently by another caller.
            os.link(temp_name, target)
        except FileExistsError:
            try:
                existing = target.read_bytes()
            except OSError as exc:
                raise RepoConfigBindingError(
                    f"cannot read concurrently frozen repo config at {target}: {exc}"
                ) from exc
            if existing != raw_bytes:
                raise RepoConfigBindingError(
                    f"a different repo config was concurrently frozen at {target}"
                )
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Durability fsync is best effort on filesystems which do not allow
            # opening or syncing directories; identity checking remains exact.
            pass
    except RepoConfigBindingError:
        raise
    except OSError as exc:
        raise RepoConfigBindingError(f"cannot freeze repository config at {target}: {exc}") from exc
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass


def freeze_repo_config(trial_root: Path, resolved: ResolvedRepoConfig) -> RepoConfigBinding:
    """Freeze ``resolved`` under ``trial_root`` without overwriting drift."""
    root = Path(trial_root).resolve()
    target = root / TRIAL_REPO_CONFIG_FILENAME
    _atomic_install_without_overwrite(target, resolved.raw_bytes)
    # Re-read once after publication.  This also catches an unexpected external
    # mutation between publication and construction of the returned binding.
    try:
        raw_bytes = target.read_bytes()
    except OSError as exc:
        raise RepoConfigBindingError(f"cannot read frozen repo config at {target}: {exc}") from exc
    actual = _sha256(raw_bytes)
    if actual != resolved.sha256:
        raise RepoConfigBindingError(
            f"frozen repo config digest mismatch at {target}: expected {resolved.sha256}, got {actual}"
        )
    config = _parse_repo_config(raw_bytes, origin=str(target))
    return RepoConfigBinding(
        path=target,
        raw_bytes=raw_bytes,
        config=config,
        identity=resolved.identity,
        source_path=resolved.source_path,
    )


def _validate_binding_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise RepoConfigBindingError("repo config binding path must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RepoConfigBindingError(f"unsafe repo config binding path: {value!r}")
    return value


def _metadata_requires_binding(metadata: Mapping[str, Any]) -> bool:
    if "trial_metadata_schema_version" not in metadata:
        return False
    version = metadata.get("trial_metadata_schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RepoConfigBindingError(
            f"invalid trial_metadata_schema_version: {version!r}"
        )
    return version >= TRIAL_METADATA_SCHEMA_VERSION_WITH_REPO_CONFIG_BINDING


def load_trial_repo_config_binding(
    trial_root: Path,
    metadata: Mapping[str, Any],
    *,
    expected_repo_name: Optional[str] = None,
) -> Optional[RepoConfigBinding]:
    """Load and verify a trial binding, or return ``None`` for legacy metadata.

    Absence is compatible only for metadata predating the binding requirement.
    Merely adding a malformed ``repo_config_binding`` key never enables legacy
    fallback.  Likewise, schema v2+ metadata without the required key fails.
    """
    if not isinstance(metadata, Mapping):
        raise RepoConfigBindingError("trial metadata must be an object")
    required = _metadata_requires_binding(metadata)
    if "repo_config_binding" not in metadata:
        if required:
            raise RepoConfigBindingError(
                "trial metadata requires repo_config_binding but none is present"
            )
        return None

    raw_binding = metadata.get("repo_config_binding")
    if not isinstance(raw_binding, Mapping):
        raise RepoConfigBindingError("trial metadata repo_config_binding must be an object")
    identity = RepoConfigIdentity.from_mapping(raw_binding)
    if expected_repo_name is not None:
        expected = _validate_repo_name(expected_repo_name)
        if identity.repo_name != expected:
            raise RepoConfigBindingError(
                "repo config binding repo_name mismatch: "
                f"expected {expected!r}, got {identity.repo_name!r}"
            )

    relative_value = _validate_binding_relative_path(raw_binding.get("path"))
    root = Path(trial_root).resolve()
    lexical_path = root.joinpath(*PurePosixPath(relative_value).parts)
    if lexical_path.is_symlink():
        raise RepoConfigBindingError(
            f"frozen repo config must not be a symlink: {lexical_path}"
        )
    path = lexical_path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RepoConfigBindingError(
            f"repo config binding path escapes trial root: {relative_value!r}"
        ) from exc
    if not path.is_file():
        raise RepoConfigBindingError(f"frozen repo config is missing or not a file: {path}")
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RepoConfigBindingError(f"cannot read frozen repo config at {path}: {exc}") from exc
    actual = _sha256(raw_bytes)
    if actual != identity.sha256:
        raise RepoConfigBindingError(
            f"frozen repo config digest mismatch at {path}: expected {identity.sha256}, got {actual}"
        )
    config = _parse_repo_config(raw_bytes, origin=str(path))

    source_value = raw_binding.get("source_path")
    if source_value is not None and not isinstance(source_value, str):
        raise RepoConfigBindingError("repo config binding source_path must be a string when present")
    source_path = Path(source_value) if source_value is not None else None
    return RepoConfigBinding(
        path=path,
        raw_bytes=raw_bytes,
        config=config,
        identity=identity,
        source_path=source_path,
    )


__all__ = [
    "EMPTY_REPO_CONFIG_BYTES",
    "REPO_CONFIG_BINDING_SCHEMA_VERSION",
    "TRIAL_METADATA_SCHEMA_VERSION_WITH_REPO_CONFIG_BINDING",
    "TRIAL_REPO_CONFIG_FILENAME",
    "RepoConfigBinding",
    "RepoConfigBindingError",
    "RepoConfigIdentity",
    "ResolvedRepoConfig",
    "freeze_repo_config",
    "load_trial_repo_config_binding",
    "resolve_repo_config",
]
