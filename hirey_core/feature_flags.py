"""File/env config-backed feature flags with hot-reload stat caching (ENG-537).

Option A: zero shared database tables. Reads ``HIREY_FEATURE_FLAGS_JSON`` first,
then falls back to ``HIREY_FEATURE_FLAGS_FILE`` (default
``/etc/hirey/flags/feature_flags.json``) using the same ``(st_mtime_ns, st_size)``
stat cache idiom as ``hirey_core/config.py``.

Every registered flag must default to ``False`` and declare ``owner``, ``issue``,
and ``expires_on`` (``YYYY-MM-DD``). Runtime lookup (``is_enabled`` /
``snapshot_flags``) is fail-safe default-off: missing override files resolve
silently to ``False``, while unreadable files, malformed JSON, non-boolean
entries, or unregistered keys log a visible error/warning and resolve the
affected entry (or all flags on whole-payload error) to ``False`` without
raising to business callers. Standalone ``validate_flag_config`` and
``validate_registry`` remain strict for CI and ConfigMap pre-apply checks.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)

ENV_JSON = "HIREY_FEATURE_FLAGS_JSON"
ENV_FILE = "HIREY_FEATURE_FLAGS_FILE"
DEFAULT_FLAGS_FILE = "/etc/hirey/flags/feature_flags.json"

_FLAG_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_FLAG_FILE_CACHE: dict[str, tuple[int, int, int, dict[str, bool]]] = {}
_FLAG_FILE_LOCK = threading.Lock()


class FeatureFlagConfigError(ValueError):
    """Raised when feature flag configuration or registry declarations fail strict validation."""


@dataclass(frozen=True)
class FeatureFlag:
    key: str
    default: bool = False
    owner: str = ""
    issue: str = ""
    expires_on: str = ""
    description: str = ""


FEATURE_FLAGS: dict[str, FeatureFlag] = {
    "eng537_release_rhythm_canary": FeatureFlag(
        key="eng537_release_rhythm_canary",
        default=False,
        owner="david@hirey.ai",
        issue="ENG-537",
        expires_on="2026-12-01",
        description=(
            "Canary switch verifying ENG-537 hot-read file/env feature flag "
            "evaluation and default-off isolation."
        ),
    ),
}


def validate_registry(registry: Mapping[str, FeatureFlag] = FEATURE_FLAGS) -> None:
    """Validate flag definitions strictly: safe False default, owner, issue, and expiry."""
    if not isinstance(registry, Mapping):
        raise FeatureFlagConfigError("FEATURE_FLAGS registry must be a mapping")
    for map_key, flag in registry.items():
        if not isinstance(flag, FeatureFlag):
            raise FeatureFlagConfigError(f"flag {map_key!r} must be a FeatureFlag instance")
        if flag.key != map_key:
            raise FeatureFlagConfigError(
                f"flag key mismatch: map key {map_key!r} != flag.key {flag.key!r}"
            )
        if not _FLAG_KEY_RE.fullmatch(flag.key):
            raise FeatureFlagConfigError(
                f"invalid feature flag key {flag.key!r}; expected lowercase snake_case"
            )
        if type(flag.default) is not bool or flag.default is not False:
            raise FeatureFlagConfigError(f"feature flag {flag.key!r} must enforce default=False")
        if not isinstance(flag.owner, str) or not flag.owner.strip():
            raise FeatureFlagConfigError(f"feature flag {flag.key!r} requires a non-empty owner")
        if not isinstance(flag.issue, str) or not flag.issue.strip():
            raise FeatureFlagConfigError(f"feature flag {flag.key!r} requires a non-empty issue")
        if not isinstance(flag.expires_on, str) or not _DATE_RE.fullmatch(flag.expires_on):
            raise FeatureFlagConfigError(
                f"feature flag {flag.key!r} requires expires_on in YYYY-MM-DD format"
            )
        try:
            datetime.date.fromisoformat(flag.expires_on)
        except ValueError as exc:
            raise FeatureFlagConfigError(
                f"feature flag {flag.key!r} has invalid calendar date expires_on={flag.expires_on!r}"
            ) from exc
        if not isinstance(flag.description, str) or not flag.description.strip():
            raise FeatureFlagConfigError(
                f"feature flag {flag.key!r} requires a non-empty description"
            )


validate_registry(FEATURE_FLAGS)


def clear_cache() -> None:
    """Clear the cached stat/override state (primarily for tests)."""
    with _FLAG_FILE_LOCK:
        _FLAG_FILE_CACHE.clear()


def validate_flag_config(raw_text: str, source: str = "<config>") -> None:
    """Strictly validate a raw JSON flag payload for CI or pre-apply ConfigMap checks."""
    if not isinstance(raw_text, str):
        raise FeatureFlagConfigError(f"feature flags payload in {source} must be a JSON string")
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise FeatureFlagConfigError(f"malformed JSON in feature flags {source}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise FeatureFlagConfigError(
            f"feature flags payload in {source} must be a JSON object mapping flag keys to booleans"
        )
    for key, value in parsed.items():
        if not isinstance(key, str) or not key.strip():
            raise FeatureFlagConfigError(f"invalid feature flag key {key!r} in {source}")
        if key not in FEATURE_FLAGS:
            raise FeatureFlagConfigError(f"unregistered feature flag {key!r} in {source}")
        if type(value) is not bool:
            raise FeatureFlagConfigError(
                f"feature flag {key!r} in {source} must have a JSON boolean value, got {type(value).__name__}"
            )


def _parse_flag_overrides(raw: str, *, source: str) -> dict[str, bool]:
    """Parse runtime flag overrides fail-safe: log bad input and default affected flags False."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error(
            "feature_flags: failed to load %s (%s); falling back to default=False",
            source,
            exc,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.error(
            "feature_flags: failed to load %s (expected JSON object, got %s); falling back to default=False",
            source,
            type(parsed).__name__,
        )
        return {}
    validated: dict[str, bool] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not key.strip():
            logger.warning("feature_flags: ignoring invalid flag key %r in %s", key, source)
            continue
        if key not in FEATURE_FLAGS:
            logger.warning("feature_flags: ignoring unregistered flag key %r in %s", key, source)
            continue
        if type(value) is not bool:
            logger.error(
                "feature_flags: flag %r in %s must be a JSON boolean (got %s); falling back to default=False",
                key,
                source,
                type(value).__name__,
            )
            continue
        validated[key] = value
    return validated


def _resolve_flags_file_path() -> str:
    configured = os.environ.get(ENV_FILE)
    if configured is not None and configured.strip():
        return configured.strip()
    return DEFAULT_FLAGS_FILE


def load_overrides() -> dict[str, bool]:
    """Load effective override booleans fail-safe from env JSON or stat-cached config file."""
    env_json = os.environ.get(ENV_JSON)
    if env_json is not None and env_json != "":
        return _parse_flag_overrides(env_json, source=ENV_JSON)

    target = _resolve_flags_file_path()
    try:
        stat = os.stat(target)
    except FileNotFoundError:
        with _FLAG_FILE_LOCK:
            _FLAG_FILE_CACHE.pop(target, None)
        return {}
    except OSError as exc:
        with _FLAG_FILE_LOCK:
            _FLAG_FILE_CACHE.pop(target, None)
        logger.error(
            "feature_flags: failed to load %s (%s); falling back to default=False",
            target,
            exc,
        )
        return {}

    cache_key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _FLAG_FILE_LOCK:
        cached = _FLAG_FILE_CACHE.get(target)
        if cached is not None and cached[:3] == cache_key:
            return dict(cached[3])

    actual_key = cache_key
    try:
        with open(target, "r", encoding="utf-8") as handle:
            fstat = os.fstat(handle.fileno())
            actual_key = (fstat.st_mtime_ns, fstat.st_size, fstat.st_ino)
            raw = handle.read()
    except FileNotFoundError:
        with _FLAG_FILE_LOCK:
            _FLAG_FILE_CACHE.pop(target, None)
        return {}
    except (OSError, UnicodeError) as exc:
        logger.error(
            "feature_flags: failed to load %s (%s); falling back to default=False",
            target,
            exc,
        )
        with _FLAG_FILE_LOCK:
            _FLAG_FILE_CACHE[target] = (actual_key[0], actual_key[1], actual_key[2], {})
        return {}

    parsed = _parse_flag_overrides(raw, source=target)
    with _FLAG_FILE_LOCK:
        _FLAG_FILE_CACHE[target] = (actual_key[0], actual_key[1], actual_key[2], parsed)
    return dict(parsed)


def is_enabled(flag_key: str) -> bool:
    """Evaluate a registered feature flag fail-safe, logging and returning False if unregistered."""
    if flag_key not in FEATURE_FLAGS:
        logger.error(
            "feature_flags: is_enabled called with unregistered flag key %r; returning False",
            flag_key,
        )
        return False
    overrides = load_overrides()
    return overrides.get(flag_key, FEATURE_FLAGS[flag_key].default)


def snapshot_flags() -> dict[str, bool]:
    """Return a consistent snapshot of all registered flags and their resolved values."""
    overrides = load_overrides()
    return {
        key: overrides.get(key, spec.default)
        for key, spec in sorted(FEATURE_FLAGS.items())
    }
