"""Immutable trial binding for quarantine/runtime policy.

The repository evaluation config and the runtime policy are separate inputs.
The latter controls network quarantine, package-manager offline switches, cache
paths, and (for Go) the exact toolchain/proxy contract.  Reading
``quarantine_configs/<repo>.yaml`` again on resume or in an evaluator worker can
therefore change a trial after its first model turn.

This module intentionally has no process-environment side effects.  It resolves
one live policy into exact bytes, freezes those bytes below the trial root, and
derives the existing ``SWE_MILESTONE_*`` environment from the already-parsed
frozen mapping.  Callers can consequently replace the managed environment as a
single unit instead of trusting ``SWE_MILESTONE_QUARANTINE=1`` as proof that all
of the companion variables were installed.
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

from harness.e2e.quarantine import (
    image_for_resolved_policy,
    normalize_maven_plugin_probes,
    quarantine_coverage_errors_from_config,
    quarantine_env_from_config,
)


RUNTIME_POLICY_BINDING_SCHEMA_VERSION = 1
TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING = 3
TRIAL_RUNTIME_POLICY_FILENAME = "runtime_policy.yaml"
EMPTY_RUNTIME_POLICY_BYTES = b"{}\n"

RUNTIME_POLICY_MODE_PROTECTED = "protected"
RUNTIME_POLICY_MODE_ABSENT = "absent"
RUNTIME_POLICY_MODE_UNPROTECTED = "unprotected"
RUNTIME_POLICY_MODES = frozenset(
    {
        RUNTIME_POLICY_MODE_PROTECTED,
        RUNTIME_POLICY_MODE_ABSENT,
        RUNTIME_POLICY_MODE_UNPROTECTED,
    }
)

# Every variable produced by quarantine.load_quarantine_env.  Integration code
# should clear this complete set before installing RuntimePolicyBinding.env so a
# policy from a previous repository/process cannot leak into the next trial.
RUNTIME_POLICY_ENV_KEYS = frozenset(
    {
        "SWE_MILESTONE_QUARANTINE",
        "SWE_MILESTONE_DENY_DOMAINS",
        "SWE_MILESTONE_DENY_CIDRS",
        "SWE_MILESTONE_FIREWALL_EXEMPT",
        "SWE_MILESTONE_PIP_OFFLINE",
        "SWE_MILESTONE_CARGO_OFFLINE",
        "SWE_MILESTONE_GO_OFFLINE",
        "SWE_MILESTONE_MAVEN_OFFLINE",
        "SWE_MILESTONE_MAVEN_REPO_LOCAL",
        "SWE_MILESTONE_NPM_OFFLINE",
        "SWE_MILESTONE_GO_TOOLCHAIN",
        "SWE_MILESTONE_CACHE_PATHS",
        "SWE_MILESTONE_MAVEN_PLUGIN_PROBES",
        "SWE_MILESTONE_CACHE_FORBID_GLOBS",
        "SWE_MILESTONE_VERIFY_FETCH_URLS",
    }
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class RuntimePolicyBindingError(RuntimeError):
    """The runtime policy cannot be resolved or its binding is invalid."""


def _validate_repo_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimePolicyBindingError(
            "runtime policy binding repo_name must be a non-empty string"
        )
    if (
        value in {".", ".."}
        or "\x00" in value
        or "/" in value
        or "\\" in value
    ):
        raise RuntimePolicyBindingError(
            f"unsafe runtime policy binding repo_name: {value!r}"
        )
    return value


def _validate_mode(value: object) -> str:
    if not isinstance(value, str) or value not in RUNTIME_POLICY_MODES:
        raise RuntimePolicyBindingError(
            f"invalid runtime policy mode {value!r}; expected one of "
            f"{sorted(RUNTIME_POLICY_MODES)}"
        )
    return value


def _validate_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RuntimePolicyBindingError(
            "runtime policy binding sha256 must be 64 lowercase hex characters"
        )
    return value


def _sha256(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def _parse_policy(raw_bytes: bytes, *, origin: str) -> dict[str, Any]:
    """Parse exactly the bytes that are hashed, requiring a YAML mapping."""
    try:
        value = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise RuntimePolicyBindingError(
            f"invalid runtime policy YAML at {origin}: {exc}"
        ) from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimePolicyBindingError(
            f"runtime policy at {origin} must contain a YAML mapping, "
            f"got {type(value).__name__}"
        )
    return value


def derive_runtime_policy_env(
    policy: Mapping[str, Any],
    *,
    mode: str = RUNTIME_POLICY_MODE_PROTECTED,
) -> dict[str, str]:
    """Derive quarantine env from an already-frozen mapping.

    The protected branch is deliberately equivalent to
    :func:`harness.e2e.quarantine.load_quarantine_env`, while avoiding that
    function's live filesystem read.  ``absent`` and explicit ``unprotected``
    bindings both produce an empty managed environment; their distinct modes
    remain recorded in metadata for auditability.
    """
    normalized_mode = _validate_mode(mode)
    if not isinstance(policy, Mapping):
        raise RuntimePolicyBindingError("runtime policy must be a mapping")
    if normalized_mode != RUNTIME_POLICY_MODE_PROTECTED:
        return {}

    # The shared pure function is the canonical derivation logic. Validate the
    # one branch which its legacy wrapper historically reports via sys.exit so
    # this library primitive always raises a typed exception instead.
    closure = policy.get("closure")
    if isinstance(closure, Mapping) and "maven_plugin_probes" in closure:
        try:
            normalize_maven_plugin_probes(closure.get("maven_plugin_probes"))
        except ValueError as exc:
            raise RuntimePolicyBindingError(
                f"invalid closure.maven_plugin_probes: {exc}"
            ) from exc
    try:
        env = quarantine_env_from_config("<frozen>", dict(policy))
    except (TypeError, ValueError) as exc:
        raise RuntimePolicyBindingError(
            f"invalid frozen runtime policy: {exc}"
        ) from exc
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in env.items()
    ):
        raise RuntimePolicyBindingError(
            "quarantine environment derivation returned non-string entries"
        )
    return env


@dataclass(frozen=True)
class RuntimePolicyIdentity:
    """Relocatable policy identity recorded in trial/snapshot metadata."""

    repo_name: str
    sha256: str
    mode: str
    schema_version: int = RUNTIME_POLICY_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != RUNTIME_POLICY_BINDING_SCHEMA_VERSION
        ):
            raise RuntimePolicyBindingError(
                "unsupported runtime policy binding schema_version: "
                f"{self.schema_version!r}"
            )
        object.__setattr__(self, "repo_name", _validate_repo_name(self.repo_name))
        object.__setattr__(self, "sha256", _validate_sha256(self.sha256))
        object.__setattr__(self, "mode", _validate_mode(self.mode))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repo_name": self.repo_name,
            "sha256": self.sha256,
            "mode": self.mode,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "RuntimePolicyIdentity":
        if not isinstance(value, Mapping):
            raise RuntimePolicyBindingError(
                "runtime policy binding identity must be an object"
            )
        version = value.get("schema_version")
        if (
            isinstance(version, bool)
            or version != RUNTIME_POLICY_BINDING_SCHEMA_VERSION
        ):
            raise RuntimePolicyBindingError(
                f"unsupported runtime policy binding schema_version: {version!r}"
            )
        return cls(
            schema_version=version,
            repo_name=value.get("repo_name"),
            sha256=value.get("sha256"),
            mode=value.get("mode"),
        )


@dataclass(frozen=True)
class ResolvedRuntimePolicy:
    """One live policy resolved once into immutable source bytes."""

    repo_name: str
    raw_bytes: bytes
    policy: Mapping[str, Any]
    mode: str
    source_path: Optional[Path]

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_name", _validate_repo_name(self.repo_name))
        object.__setattr__(self, "mode", _validate_mode(self.mode))
        if not isinstance(self.raw_bytes, bytes):
            raise RuntimePolicyBindingError(
                "resolved runtime policy raw_bytes must be bytes"
            )
        if not isinstance(self.policy, Mapping):
            raise RuntimePolicyBindingError(
                "resolved runtime policy must be a mapping"
            )
        if self.mode == RUNTIME_POLICY_MODE_ABSENT:
            if self.source_path is not None or self.raw_bytes != EMPTY_RUNTIME_POLICY_BYTES:
                raise RuntimePolicyBindingError(
                    "absent runtime policy must use the canonical empty policy"
                )
        if self.mode == RUNTIME_POLICY_MODE_PROTECTED and self.source_path is None:
            raise RuntimePolicyBindingError(
                "protected runtime policy must have a source policy file"
            )

    @property
    def sha256(self) -> str:
        return _sha256(self.raw_bytes)

    @property
    def identity(self) -> RuntimePolicyIdentity:
        return RuntimePolicyIdentity(
            repo_name=self.repo_name,
            sha256=self.sha256,
            mode=self.mode,
        )

    @property
    def env(self) -> dict[str, str]:
        return derive_runtime_policy_env(self.policy, mode=self.mode)

    @property
    def effective_policy(self) -> Optional[dict[str, Any]]:
        """Policy consumed by evaluators; disabled modes are explicitly off."""
        if self.mode != RUNTIME_POLICY_MODE_PROTECTED:
            return None
        return dict(self.policy)


@dataclass(frozen=True)
class RuntimePolicyBinding:
    """Verified trial-local policy and its relocatable identity."""

    path: Path
    raw_bytes: bytes
    policy: Mapping[str, Any]
    identity: RuntimePolicyIdentity
    source_path: Optional[Path] = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_bytes, bytes):
            raise RuntimePolicyBindingError(
                "bound runtime policy raw_bytes must be bytes"
            )
        if not isinstance(self.policy, Mapping):
            raise RuntimePolicyBindingError("bound runtime policy must be a mapping")
        actual = _sha256(self.raw_bytes)
        if actual != self.identity.sha256:
            raise RuntimePolicyBindingError(
                "bound runtime policy bytes do not match identity: "
                f"expected {self.identity.sha256}, got {actual}"
            )
        if self.identity.mode == RUNTIME_POLICY_MODE_ABSENT:
            if self.source_path is not None or self.raw_bytes != EMPTY_RUNTIME_POLICY_BYTES:
                raise RuntimePolicyBindingError(
                    "absent runtime policy binding is not canonical"
                )
        if self.source_path is not None and not isinstance(self.source_path, Path):
            raise RuntimePolicyBindingError(
                "bound runtime policy source_path must be a Path"
            )

    @property
    def repo_name(self) -> str:
        return self.identity.repo_name

    @property
    def sha256(self) -> str:
        return self.identity.sha256

    @property
    def mode(self) -> str:
        return self.identity.mode

    @property
    def env(self) -> dict[str, str]:
        return derive_runtime_policy_env(self.policy, mode=self.mode)

    @property
    def effective_policy(self) -> Optional[dict[str, Any]]:
        """Policy consumed by evaluators; disabled modes are explicitly off."""
        if self.mode != RUNTIME_POLICY_MODE_PROTECTED:
            return None
        return dict(self.policy)

    def to_metadata(self, trial_root: Path) -> dict[str, Any]:
        root = Path(trial_root).resolve()
        path = Path(self.path).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise RuntimePolicyBindingError(
                f"bound runtime policy path is outside trial root: {path}"
            ) from exc
        relative_posix = relative.as_posix()
        _validate_relative_path(relative_posix)
        result = self.identity.to_dict()
        result["path"] = relative_posix
        if self.source_path is not None:
            result["source_path"] = str(self.source_path)
        return result


RuntimePolicy = ResolvedRuntimePolicy | RuntimePolicyBinding


def runtime_policy_coverage_errors(policy: RuntimePolicy) -> list[str]:
    """Validate coverage from the exact policy object without a live reread."""
    if not isinstance(policy, (ResolvedRuntimePolicy, RuntimePolicyBinding)):
        raise RuntimePolicyBindingError(
            "runtime policy coverage requires a resolved or frozen policy"
        )
    mapping: Mapping[str, Any] | None
    if policy.mode == RUNTIME_POLICY_MODE_ABSENT:
        mapping = None
    elif (
        policy.mode == RUNTIME_POLICY_MODE_UNPROTECTED
        and policy.source_path is None
        and policy.raw_bytes == EMPTY_RUNTIME_POLICY_BYTES
    ):
        mapping = None
    else:
        mapping = policy.policy
    return quarantine_coverage_errors_from_config(policy.repo_name, mapping)


def image_for_runtime_policy(policy: RuntimePolicy) -> str:
    """Select base/offline image from the same exact policy object."""
    if not isinstance(policy, (ResolvedRuntimePolicy, RuntimePolicyBinding)):
        raise RuntimePolicyBindingError(
            "runtime policy image selection requires a resolved or frozen policy"
        )
    return image_for_resolved_policy(
        policy.repo_name,
        protected=policy.mode == RUNTIME_POLICY_MODE_PROTECTED,
    )


def verify_expected_runtime_policy(
    policy: ResolvedRuntimePolicy,
    *,
    expected_sha256: Optional[str] = None,
    expected_mode: Optional[str] = None,
) -> None:
    """Fail closed when a parent and fresh worker resolved different policy.

    The expected identity is optional for direct/backward-compatible launches,
    but it is an all-or-nothing pair.  ``scripts/run_all.py`` always sends both
    values for fresh workers.
    """
    if not isinstance(policy, ResolvedRuntimePolicy):
        raise RuntimePolicyBindingError(
            "expected-policy verification requires a resolved runtime policy"
        )
    if (expected_sha256 is None) != (expected_mode is None):
        raise RuntimePolicyBindingError(
            "expected runtime policy sha256 and mode must be provided together"
        )
    if expected_sha256 is None:
        return
    sha256 = _validate_sha256(expected_sha256)
    mode = _validate_mode(expected_mode)
    if policy.sha256 != sha256 or policy.mode != mode:
        raise RuntimePolicyBindingError(
            "runtime policy changed between launcher and worker: "
            f"expected sha256={sha256}, mode={mode}; "
            f"resolved sha256={policy.sha256}, mode={policy.mode}"
        )


def runtime_policy_subprocess_env(
    policy: RuntimePolicy | None,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return a worker env with inherited managed policy state replaced.

    Passing ``None`` is intentional for resume workers: they start with no live
    policy state and restore the trial-frozen binding inside ``run_e2e``.
    """
    env = {
        str(key): str(value)
        for key, value in base_env.items()
        if key not in RUNTIME_POLICY_ENV_KEYS
        and key != "SWE_MILESTONE_UNPROTECTED"
    }
    if policy is None:
        return env
    policy_env = policy.env
    unexpected = set(policy_env) - set(RUNTIME_POLICY_ENV_KEYS)
    if unexpected:
        raise RuntimePolicyBindingError(
            f"runtime policy derived unmanaged environment keys: {sorted(unexpected)}"
        )
    env.update(policy_env)
    if policy.mode == RUNTIME_POLICY_MODE_UNPROTECTED:
        env["SWE_MILESTONE_UNPROTECTED"] = "1"
    return env


def resolve_runtime_policy(
    repo_name: str,
    project_root: Path,
    *,
    unprotected: bool = False,
) -> ResolvedRuntimePolicy:
    """Strictly read ``quarantine_configs/<repo>.yaml`` exactly once.

    Missing policy is represented by canonical bytes and the explicit
    ``absent`` mode.  ``unprotected=True`` is always represented by the distinct
    ``unprotected`` mode; if a policy file exists its exact bytes are still
    captured for auditability, but it derives no quarantine environment.
    """
    name = _validate_repo_name(repo_name)
    root = Path(project_root).resolve()
    source = root / "quarantine_configs" / f"{name}.yaml"

    if not source.exists() and not source.is_symlink():
        return ResolvedRuntimePolicy(
            repo_name=name,
            raw_bytes=EMPTY_RUNTIME_POLICY_BYTES,
            policy={},
            mode=(
                RUNTIME_POLICY_MODE_UNPROTECTED
                if unprotected
                else RUNTIME_POLICY_MODE_ABSENT
            ),
            source_path=None,
        )

    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise RuntimePolicyBindingError(
            f"cannot read runtime policy at {source}: {exc}"
        ) from exc
    policy = _parse_policy(raw_bytes, origin=str(source))
    return ResolvedRuntimePolicy(
        repo_name=name,
        raw_bytes=raw_bytes,
        policy=policy,
        mode=(
            RUNTIME_POLICY_MODE_UNPROTECTED
            if unprotected
            else RUNTIME_POLICY_MODE_PROTECTED
        ),
        source_path=source.resolve(),
    )


def _atomic_install_without_overwrite(target: Path, raw_bytes: bytes) -> None:
    """Atomically publish bytes, reusing only an identical regular file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise RuntimePolicyBindingError(
            f"frozen runtime policy must not be a symlink: {target}"
        )
    if target.exists():
        if not target.is_file():
            raise RuntimePolicyBindingError(
                f"frozen runtime policy is not a regular file: {target}"
            )
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise RuntimePolicyBindingError(
                f"cannot read frozen runtime policy at {target}: {exc}"
            ) from exc
        if existing != raw_bytes:
            raise RuntimePolicyBindingError(
                f"refusing to overwrite a different frozen runtime policy at {target}"
            )
        return

    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as stream:
            temp_name = stream.name
            stream.write(raw_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, target)
        except FileExistsError:
            if target.is_symlink():
                raise RuntimePolicyBindingError(
                    f"concurrently frozen runtime policy is a symlink: {target}"
                )
            try:
                existing = target.read_bytes()
            except OSError as exc:
                raise RuntimePolicyBindingError(
                    f"cannot read concurrently frozen runtime policy at {target}: {exc}"
                ) from exc
            if existing != raw_bytes:
                raise RuntimePolicyBindingError(
                    f"a different runtime policy was concurrently frozen at {target}"
                )
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except RuntimePolicyBindingError:
        raise
    except OSError as exc:
        raise RuntimePolicyBindingError(
            f"cannot freeze runtime policy at {target}: {exc}"
        ) from exc
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass


def freeze_runtime_policy(
    trial_root: Path,
    resolved: ResolvedRuntimePolicy,
) -> RuntimePolicyBinding:
    """Freeze one resolved policy below ``trial_root`` without overwrite."""
    root = Path(trial_root).resolve()
    target = root / TRIAL_RUNTIME_POLICY_FILENAME
    _atomic_install_without_overwrite(target, resolved.raw_bytes)
    try:
        raw_bytes = target.read_bytes()
    except OSError as exc:
        raise RuntimePolicyBindingError(
            f"cannot read frozen runtime policy at {target}: {exc}"
        ) from exc
    actual = _sha256(raw_bytes)
    if actual != resolved.sha256:
        raise RuntimePolicyBindingError(
            f"frozen runtime policy digest mismatch at {target}: "
            f"expected {resolved.sha256}, got {actual}"
        )
    policy = _parse_policy(raw_bytes, origin=str(target))
    return RuntimePolicyBinding(
        path=target,
        raw_bytes=raw_bytes,
        policy=policy,
        identity=resolved.identity,
        source_path=resolved.source_path,
    )


def _validate_relative_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
    ):
        raise RuntimePolicyBindingError(
            "runtime policy binding path must be a safe relative POSIX path"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimePolicyBindingError(
            f"unsafe runtime policy binding path: {value!r}"
        )
    return value


def _reject_symlink_components(root: Path, relative: PurePosixPath) -> None:
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise RuntimePolicyBindingError(
                f"frozen runtime policy path contains a symlink: {candidate}"
            )


def _reject_path_symlinks(path: Path) -> None:
    """Reject a symlink in any existing component of an explicit bound path."""
    absolute = path.absolute()
    candidate = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise RuntimePolicyBindingError(
                f"frozen runtime policy path contains a symlink: {candidate}"
            )


def load_bound_runtime_policy(
    repo_name: str,
    path: Path,
    sha256: str,
    mode: str,
) -> RuntimePolicyBinding:
    """Load one explicit frozen policy with no live-policy fallback."""
    identity = RuntimePolicyIdentity(
        repo_name=repo_name,
        sha256=sha256,
        mode=mode,
    )
    policy_path = Path(path)
    _reject_path_symlinks(policy_path)
    if not policy_path.is_file():
        raise RuntimePolicyBindingError(
            f"frozen runtime policy is missing or not a file: {policy_path}"
        )
    try:
        raw_bytes = policy_path.read_bytes()
    except OSError as exc:
        raise RuntimePolicyBindingError(
            f"cannot read frozen runtime policy at {policy_path}: {exc}"
        ) from exc
    actual = _sha256(raw_bytes)
    if actual != identity.sha256:
        raise RuntimePolicyBindingError(
            f"frozen runtime policy digest mismatch at {policy_path}: "
            f"expected {identity.sha256}, got {actual}"
        )
    policy = _parse_policy(raw_bytes, origin=str(policy_path))
    return RuntimePolicyBinding(
        path=policy_path.resolve(),
        raw_bytes=raw_bytes,
        policy=policy,
        identity=identity,
    )


def load_trial_runtime_policy_binding(
    trial_root: Path,
    metadata: Mapping[str, Any],
    *,
    expected_repo_name: Optional[str] = None,
) -> RuntimePolicyBinding:
    """Load and verify a required ``runtime_policy_binding`` metadata object."""
    if not isinstance(metadata, Mapping):
        raise RuntimePolicyBindingError("trial metadata must be an object")
    if "runtime_policy_binding" not in metadata:
        raise RuntimePolicyBindingError(
            "trial metadata requires runtime_policy_binding but none is present"
        )
    raw_binding = metadata.get("runtime_policy_binding")
    if not isinstance(raw_binding, Mapping):
        raise RuntimePolicyBindingError(
            "trial metadata runtime_policy_binding must be an object"
        )
    identity = RuntimePolicyIdentity.from_mapping(raw_binding)
    if expected_repo_name is not None:
        expected = _validate_repo_name(expected_repo_name)
        if identity.repo_name != expected:
            raise RuntimePolicyBindingError(
                "runtime policy binding repo_name mismatch: "
                f"expected {expected!r}, got {identity.repo_name!r}"
            )

    relative_value = _validate_relative_path(raw_binding.get("path"))
    relative = PurePosixPath(relative_value)
    root = Path(trial_root).resolve()
    _reject_symlink_components(root, relative)
    lexical_path = root.joinpath(*relative.parts)
    path = lexical_path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimePolicyBindingError(
            f"runtime policy binding path escapes trial root: {relative_value!r}"
        ) from exc
    if not path.is_file():
        raise RuntimePolicyBindingError(
            f"frozen runtime policy is missing or not a file: {path}"
        )
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RuntimePolicyBindingError(
            f"cannot read frozen runtime policy at {path}: {exc}"
        ) from exc
    actual = _sha256(raw_bytes)
    if actual != identity.sha256:
        raise RuntimePolicyBindingError(
            f"frozen runtime policy digest mismatch at {path}: "
            f"expected {identity.sha256}, got {actual}"
        )
    policy = _parse_policy(raw_bytes, origin=str(path))

    source_value = raw_binding.get("source_path")
    if source_value is not None and not isinstance(source_value, str):
        raise RuntimePolicyBindingError(
            "runtime policy binding source_path must be a string when present"
        )
    source_path = Path(source_value) if source_value is not None else None
    if identity.mode == RUNTIME_POLICY_MODE_ABSENT:
        if source_path is not None or raw_bytes != EMPTY_RUNTIME_POLICY_BYTES:
            raise RuntimePolicyBindingError(
                "absent runtime policy binding is not canonical"
            )
    return RuntimePolicyBinding(
        path=path,
        raw_bytes=raw_bytes,
        policy=policy,
        identity=identity,
        source_path=source_path,
    )


__all__ = [
    "EMPTY_RUNTIME_POLICY_BYTES",
    "RUNTIME_POLICY_BINDING_SCHEMA_VERSION",
    "RUNTIME_POLICY_ENV_KEYS",
    "RUNTIME_POLICY_MODE_ABSENT",
    "RUNTIME_POLICY_MODE_PROTECTED",
    "RUNTIME_POLICY_MODE_UNPROTECTED",
    "RUNTIME_POLICY_MODES",
    "TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING",
    "TRIAL_RUNTIME_POLICY_FILENAME",
    "ResolvedRuntimePolicy",
    "RuntimePolicyBinding",
    "RuntimePolicyBindingError",
    "RuntimePolicyIdentity",
    "derive_runtime_policy_env",
    "freeze_runtime_policy",
    "image_for_runtime_policy",
    "load_bound_runtime_policy",
    "load_trial_runtime_policy_binding",
    "resolve_runtime_policy",
    "runtime_policy_coverage_errors",
    "runtime_policy_subprocess_env",
    "verify_expected_runtime_policy",
]
