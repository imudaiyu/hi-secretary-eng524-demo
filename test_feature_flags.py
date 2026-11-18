#!/usr/bin/env python3
"""Tests for hirey_core.feature_flags (ENG-537 Option A file/env hot-read flags)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hirey_core import feature_flags


class FeatureFlagsTests(unittest.TestCase):
    def setUp(self) -> None:
        feature_flags.clear_cache()

    def tearDown(self) -> None:
        feature_flags.clear_cache()

    def test_builtin_canary_defaults_false_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_file = Path(tmp) / "missing.json"
            with mock.patch.dict(
                os.environ,
                {feature_flags.ENV_FILE: str(missing_file), feature_flags.ENV_JSON: ""},
                clear=False,
            ):
                self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))
                self.assertEqual(
                    feature_flags.snapshot_flags(),
                    {"eng537_release_rhythm_canary": False},
                )

    def test_env_json_override_takes_precedence_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flag_file = Path(tmp) / "flags.json"
            flag_file.write_text(json.dumps({"eng537_release_rhythm_canary": False}), encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    feature_flags.ENV_FILE: str(flag_file),
                    feature_flags.ENV_JSON: json.dumps({"eng537_release_rhythm_canary": True}),
                },
                clear=False,
            ):
                self.assertTrue(feature_flags.is_enabled("eng537_release_rhythm_canary"))

    def test_hot_reload_via_file_stat_cache_without_clearing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flag_file = Path(tmp) / "feature_flags.json"
            flag_file.write_text(
                json.dumps({"eng537_release_rhythm_canary": False}), encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ,
                {feature_flags.ENV_FILE: str(flag_file), feature_flags.ENV_JSON: ""},
                clear=False,
            ):
                self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))

                # Cache hit should avoid re-opening the file when stat signature is unchanged.
                with mock.patch("builtins.open", side_effect=AssertionError("unexpected disk read")):
                    self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))

                # Update content and bump mtime/size; hot-read picks up change immediately.
                flag_file.write_text(
                    json.dumps({"eng537_release_rhythm_canary": True}), encoding="utf-8"
                )
                stat = flag_file.stat()
                os.utime(flag_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
                self.assertTrue(feature_flags.is_enabled("eng537_release_rhythm_canary"))

                # Deleting the file drops the cache entry and reverts safely to default=False.
                flag_file.unlink()
                self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))

    def test_unregistered_flag_logs_error_and_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_file = Path(tmp) / "missing.json"
            with mock.patch.dict(
                os.environ,
                {feature_flags.ENV_FILE: str(missing_file), feature_flags.ENV_JSON: ""},
                clear=False,
            ):
                with self.assertLogs("hirey_core.feature_flags", level="ERROR") as logs:
                    self.assertFalse(feature_flags.is_enabled("nonexistent_flag_key"))
                self.assertTrue(
                    any("nonexistent_flag_key" in msg for msg in logs.output),
                    logs.output,
                )

    def test_malformed_json_non_bool_and_unreadable_file_fallback_to_false(self) -> None:
        with mock.patch.dict(os.environ, {feature_flags.ENV_JSON: "{bad json"}, clear=False):
            with self.assertLogs("hirey_core.feature_flags", level="ERROR") as logs:
                self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))
            self.assertTrue(any("falling back to default=False" in m for m in logs.output), logs.output)

        with mock.patch.dict(
            os.environ,
            {feature_flags.ENV_JSON: json.dumps(["eng537_release_rhythm_canary"])},
            clear=False,
        ):
            with self.assertLogs("hirey_core.feature_flags", level="ERROR") as logs:
                self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))
            self.assertTrue(any("expected JSON object" in m for m in logs.output), logs.output)

        for bad_val in ("true", 1, 0, None, {}):
            with mock.patch.dict(
                os.environ,
                {feature_flags.ENV_JSON: json.dumps({"eng537_release_rhythm_canary": bad_val})},
                clear=False,
            ):
                with self.assertLogs("hirey_core.feature_flags", level="ERROR") as logs:
                    self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))
                self.assertTrue(any("JSON boolean" in m for m in logs.output), logs.output)

        # Sibling valid boolean stays True while non-boolean / unregistered entries default False.
        extra_spec = feature_flags.FeatureFlag(
            key="eng537_sibling_flag",
            default=False,
            owner="david@hirey.ai",
            issue="ENG-537",
            expires_on="2026-12-01",
            description="Sibling registered flag for partial payload isolation tests.",
        )
        with mock.patch.dict(feature_flags.FEATURE_FLAGS, {"eng537_sibling_flag": extra_spec}):
            mixed_payload = json.dumps(
                {
                    "eng537_release_rhythm_canary": "not_bool",
                    "eng537_sibling_flag": True,
                    "stale_removed_key": True,
                }
            )
            with mock.patch.dict(os.environ, {feature_flags.ENV_JSON: mixed_payload}, clear=False):
                with self.assertLogs("hirey_core.feature_flags", level="WARNING") as logs:
                    snap = feature_flags.snapshot_flags()
                self.assertEqual(
                    snap,
                    {
                        "eng537_release_rhythm_canary": False,
                        "eng537_sibling_flag": True,
                    },
                )
                self.assertTrue(any("stale_removed_key" in m for m in logs.output), logs.output)
                self.assertTrue(any("JSON boolean" in m for m in logs.output), logs.output)

        # Malformed file caches {} under stat key so repeated requests avoid disk re-reads and log floods.
        with tempfile.TemporaryDirectory() as tmp:
            bad_file = Path(tmp) / "bad.json"
            bad_file.write_text('{"eng537_release_rhythm_canary": "yes"}', encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {feature_flags.ENV_FILE: str(bad_file), feature_flags.ENV_JSON: ""},
                clear=False,
            ):
                with self.assertLogs("hirey_core.feature_flags", level="ERROR"):
                    self.assertEqual(
                        feature_flags.snapshot_flags(),
                        {"eng537_release_rhythm_canary": False},
                    )
                with mock.patch("builtins.open", side_effect=AssertionError("unexpected disk read")):
                    self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))

        # Unreadable bytes (UnicodeDecodeError) and PermissionError fallback to False cleanly.
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = Path(tmp) / "binary.json"
            bin_file.write_bytes(b"\xff\xfe\x00bad")
            with mock.patch.dict(
                os.environ,
                {feature_flags.ENV_FILE: str(bin_file), feature_flags.ENV_JSON: ""},
                clear=False,
            ):
                with self.assertLogs("hirey_core.feature_flags", level="ERROR"):
                    self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))
                with mock.patch("builtins.open", side_effect=AssertionError("unexpected disk read")):
                    self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))

        with tempfile.TemporaryDirectory() as tmp:
            locked_file = Path(tmp) / "unreadable.json"
            locked_file.write_text("{}", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {feature_flags.ENV_FILE: str(locked_file), feature_flags.ENV_JSON: ""},
                clear=False,
            ):
                with mock.patch("builtins.open", side_effect=PermissionError("denied")):
                    with self.assertLogs("hirey_core.feature_flags", level="ERROR") as logs:
                        self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))
                    self.assertTrue(any("PermissionError" in m or "denied" in m for m in logs.output), logs.output)

            # Directory path (readable stat, unreadable open -> IsADirectoryError) falls back to False.
            with mock.patch.dict(
                os.environ,
                {feature_flags.ENV_FILE: tmp, feature_flags.ENV_JSON: ""},
                clear=False,
            ):
                feature_flags.clear_cache()
                with self.assertLogs("hirey_core.feature_flags", level="ERROR"):
                    self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))

        # os.stat OSError (e.g. parent permission error) logs error and returns False.
        with mock.patch.dict(
            os.environ,
            {feature_flags.ENV_FILE: "/nonexistent_dir_x/flags.json", feature_flags.ENV_JSON: ""},
            clear=False,
        ):
            with mock.patch("os.stat", side_effect=PermissionError("stat denied")):
                with self.assertLogs("hirey_core.feature_flags", level="ERROR") as logs:
                    self.assertFalse(feature_flags.is_enabled("eng537_release_rhythm_canary"))
                self.assertTrue(any("stat denied" in m for m in logs.output), logs.output)

    def test_unknown_config_key_ignored_while_preserving_valid_keys(self) -> None:
        payload = json.dumps({"unregistered_typo": True, "eng537_release_rhythm_canary": True})
        with mock.patch.dict(os.environ, {feature_flags.ENV_JSON: payload}, clear=False):
            with self.assertLogs("hirey_core.feature_flags", level="WARNING") as logs:
                self.assertEqual(
                    feature_flags.snapshot_flags(),
                    {"eng537_release_rhythm_canary": True},
                )
                self.assertTrue(feature_flags.is_enabled("eng537_release_rhythm_canary"))
            self.assertTrue(any("unregistered_typo" in m for m in logs.output), logs.output)

    def test_validate_flag_config_fails_strictly_on_invalid_payloads(self) -> None:
        feature_flags.validate_flag_config(json.dumps({"eng537_release_rhythm_canary": True}), source="ci")
        for bad in (
            "{bad json",
            json.dumps(["not_an_object"]),
            json.dumps({"": True}),
            json.dumps({"unregistered_typo": True}),
            json.dumps({"eng537_release_rhythm_canary": "true"}),
            json.dumps({"eng537_release_rhythm_canary": 1}),
        ):
            with self.assertRaises(feature_flags.FeatureFlagConfigError):
                feature_flags.validate_flag_config(bad, source="ci")

    def test_validate_registry_enforces_default_false_owner_issue_expiry(self) -> None:
        valid = feature_flags.FeatureFlag(
            key="sample_valid_flag",
            default=False,
            owner="david@hirey.ai",
            issue="ENG-537",
            expires_on="2026-12-31",
            description="Valid sample flag.",
        )
        feature_flags.validate_registry({"sample_valid_flag": valid})

        cases = [
            feature_flags.FeatureFlag(
                key="bad_default_true",
                default=True,
                owner="david@hirey.ai",
                issue="ENG-537",
                expires_on="2026-12-31",
                description="Default must be False.",
            ),
            feature_flags.FeatureFlag(
                key="missing_owner",
                default=False,
                owner="   ",
                issue="ENG-537",
                expires_on="2026-12-31",
                description="Missing owner.",
            ),
            feature_flags.FeatureFlag(
                key="missing_issue",
                default=False,
                owner="david@hirey.ai",
                issue="",
                expires_on="2026-12-31",
                description="Missing issue.",
            ),
            feature_flags.FeatureFlag(
                key="bad_expiry_format",
                default=False,
                owner="david@hirey.ai",
                issue="ENG-537",
                expires_on="2026/12/31",
                description="Bad date format.",
            ),
            feature_flags.FeatureFlag(
                key="bad_expiry_calendar",
                default=False,
                owner="david@hirey.ai",
                issue="ENG-537",
                expires_on="2026-02-30",
                description="Invalid calendar date.",
            ),
            feature_flags.FeatureFlag(
                key="InvalidKeyCase",
                default=False,
                owner="david@hirey.ai",
                issue="ENG-537",
                expires_on="2026-12-31",
                description="Bad key format.",
            ),
        ]
        for bad_flag in cases:
            with self.assertRaises(ValueError):
                feature_flags.validate_registry({bad_flag.key: bad_flag})


if __name__ == "__main__":
    unittest.main()
