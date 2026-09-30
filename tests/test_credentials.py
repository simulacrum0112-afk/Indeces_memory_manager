from __future__ import annotations

import json
import getpass
import os
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
from unittest.mock import patch

from indeces import credentials
from indeces.credentials import CredentialError, load_discord_token, prompt_secret, save_discord_token, secret_path, validate_token


TOKEN = "synthetic-discord-token-for-offline-tests-only"
SECOND_TOKEN = "replacement-synthetic-discord-token-offline-only"
GUILD = "123456789012345678"


def fake_protect(data: bytes) -> bytes:
    # Explicitly a test cipher, never selected by production code.
    return b"test-only:" + bytes(value ^ 0xA5 for value in data)


def fake_unprotect(data: bytes) -> bytes:
    if not data.startswith(b"test-only:"):
        raise RuntimeError("invalid test ciphertext")
    return bytes(value ^ 0xA5 for value in data.removeprefix(b"test-only:"))


class TokenValidationTests(unittest.TestCase):
    def test_opaque_token_and_outer_spaces(self):
        self.assertEqual(validate_token(f"  {TOKEN}  "), TOKEN)
        self.assertEqual(validate_token("a" * 20), "a" * 20)
        self.assertEqual(validate_token("a" * 4096), "a" * 4096)

    def test_rejects_invalid_inputs_without_echoing(self):
        values = [None, 123, "", " " * 30, "short", "a" * 4097,
                  TOKEN + "\n", "\t" + TOKEN, TOKEN + "\x00", TOKEN + "\x7f",
                  TOKEN + "é", TOKEN + " secret"]
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(CredentialError) as caught:
                    validate_token(value)
                self.assertEqual(caught.exception.code, "invalid_token")
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertIsNone(caught.exception.__context__)

    def test_error_constructor_cannot_echo_arbitrary_input(self):
        error = CredentialError(TOKEN)
        self.assertEqual(str(error), "credential_corrupt")
        self.assertNotIn(TOKEN, repr(error))


class PrivateInputTests(unittest.TestCase):
    def test_private_input_returns_without_echo_fallback(self):
        with patch("indeces.credentials.getpass.getpass", return_value=TOKEN) as hidden, \
                patch("builtins.input") as echoed:
            self.assertEqual(prompt_secret("Synthetic token: "), TOKEN)
        hidden.assert_called_once_with("Synthetic token: ")
        echoed.assert_not_called()

    def test_getpass_warning_stops_before_echoed_input(self):
        def unsupported(prompt):
            warnings.warn(TOKEN, getpass.GetPassWarning)
            return input(prompt)
        filters = list(warnings.filters)
        with patch("indeces.credentials.getpass.getpass", side_effect=unsupported), \
                patch("builtins.input") as echoed:
            with self.assertRaises(CredentialError) as caught:
                prompt_secret("Synthetic token: ")
        self.assertEqual(str(caught.exception), "private_input_unavailable")
        self.assertNotIn(TOKEN, repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(warnings.filters, filters)
        echoed.assert_not_called()

    def test_input_failure_is_scrubbed(self):
        for failure in [getpass.GetPassWarning(TOKEN), OSError(TOKEN), RuntimeError(TOKEN)]:
            with self.subTest(failure_type=type(failure).__name__), \
                    patch("indeces.credentials.getpass.getpass", side_effect=failure):
                with self.assertRaises(CredentialError) as caught:
                    prompt_secret("Synthetic token: ")
                self.assertEqual(str(caught.exception), "private_input_unavailable")
                self.assertNotIn(TOKEN, repr(caught.exception))
                self.assertIsNone(caught.exception.__context__)

    def test_cancellation_propagates(self):
        for failure in [EOFError(), KeyboardInterrupt()]:
            with self.subTest(failure_type=type(failure).__name__), \
                    patch("indeces.credentials.getpass.getpass", side_effect=failure):
                with self.assertRaises(type(failure)):
                    prompt_secret("Synthetic token: ")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "config.local.toml"
        self.protect = patch("indeces.credentials._protect", side_effect=fake_protect).start()
        self.unprotect = patch("indeces.credentials._unprotect", side_effect=fake_unprotect).start()
        self.addCleanup(patch.stopall)

    def test_path_is_adjacent_to_config(self):
        self.assertEqual(secret_path(self.config), self.config.with_name("config.local.discord.secret"))

    def test_absent_returns_none(self):
        self.assertIsNone(load_discord_token(self.config, GUILD))
        self.unprotect.assert_not_called()

    def test_roundtrip_is_ciphertext_and_replacement_is_atomic(self):
        self.config.write_text("unchanged config", encoding="utf-8")
        save_discord_token(self.config, GUILD, f" {TOKEN} ")
        target = secret_path(self.config)
        self.assertTrue(target.read_bytes().startswith(credentials._MAGIC))
        self.assertNotIn(TOKEN.encode(), target.read_bytes())
        self.assertNotIn(GUILD.encode(), target.read_bytes())
        self.assertEqual(load_discord_token(self.config, GUILD), TOKEN)
        save_discord_token(self.config, GUILD, SECOND_TOKEN)
        self.assertEqual(load_discord_token(self.config, GUILD), SECOND_TOKEN)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "unchanged config")
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()),
                         sorted([self.config.name, target.name]))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_guild_scope_mismatch(self):
        save_discord_token(self.config, GUILD, TOKEN)
        with self.assertRaises(CredentialError) as caught:
            load_discord_token(self.config, "123456789012345679")
        self.assertEqual(str(caught.exception), "credential_scope_mismatch")

    def test_invalid_guild_is_not_persisted(self):
        for guild in ["", "0", "-1", "１２３", "1 2", str(2**64), None]:
            with self.subTest(guild_type=type(guild).__name__):
                with self.assertRaises(CredentialError) as caught:
                    save_discord_token(self.config, guild, TOKEN)
                self.assertEqual(str(caught.exception), "invalid_guild_id")
                self.assertFalse(secret_path(self.config).exists())
        self.protect.assert_not_called()

    def test_corrupt_envelope_rejected_before_decryption(self):
        for contents in [b"", b"wrong header", credentials._MAGIC,
                         credentials._MAGIC + b"x" * (credentials._MAX_CIPHERTEXT + 1)]:
            secret_path(self.config).write_bytes(contents)
            with self.assertRaises(CredentialError) as caught:
                load_discord_token(self.config, GUILD)
            self.assertEqual(str(caught.exception), "credential_corrupt")
        self.unprotect.assert_not_called()

    def test_corrupt_protected_payload_is_rejected(self):
        values = [b"not-json", b"\xff", b"[]", b'{}',
                  json.dumps({"version": True, "guild_id": GUILD, "token": TOKEN}).encode(),
                  json.dumps({"version": 2, "guild_id": GUILD, "token": TOKEN}).encode(),
                  json.dumps({"version": 1, "guild_id": GUILD, "token": TOKEN, "extra": 1}).encode(),
                  ('{"version":1,"version":1,"guild_id":"' + GUILD + '","token":"' + TOKEN + '"}').encode()]
        for payload in values:
            secret_path(self.config).write_bytes(credentials._MAGIC + fake_protect(payload))
            with self.assertRaises(CredentialError) as caught:
                load_discord_token(self.config, GUILD)
            self.assertEqual(str(caught.exception), "credential_corrupt")
            self.assertNotIn(TOKEN, repr(caught.exception))
            self.assertIsNone(caught.exception.__context__)

    def test_protect_failure_has_no_plaintext_or_exception_context(self):
        self.protect.side_effect = RuntimeError(TOKEN)
        with self.assertRaises(CredentialError) as caught:
            save_discord_token(self.config, GUILD, TOKEN)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_unprotect_failure_is_scrubbed(self):
        save_discord_token(self.config, GUILD, TOKEN)
        self.unprotect.side_effect = RuntimeError(TOKEN)
        with self.assertRaises(CredentialError) as caught:
            load_discord_token(self.config, GUILD)
        self.assertEqual(str(caught.exception), "credential_unprotect_failed")
        self.assertNotIn(TOKEN, repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    def test_failed_replace_preserves_previous_credential_and_cleans_temp(self):
        save_discord_token(self.config, GUILD, TOKEN)
        original = secret_path(self.config).read_bytes()
        with patch("indeces.credentials.os.replace", side_effect=OSError(TOKEN)):
            with self.assertRaises(CredentialError) as caught:
                save_discord_token(self.config, GUILD, SECOND_TOKEN)
        self.assertEqual(str(caught.exception), "credential_save_failed")
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(secret_path(self.config).read_bytes(), original)
        self.assertEqual(list(self.config.parent.iterdir()), [secret_path(self.config)])

    def test_failed_fsync_leaves_no_partial_file(self):
        with patch("indeces.credentials.os.fsync", side_effect=OSError(TOKEN)):
            with self.assertRaises(CredentialError):
                save_discord_token(self.config, GUILD, TOKEN)
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_fdopen_failure_closes_descriptor_and_cleans_temp(self):
        with patch("indeces.credentials.os.fdopen", side_effect=OSError(TOKEN)), \
                patch("indeces.credentials.os.close", wraps=os.close) as close:
            with self.assertRaises(CredentialError) as caught:
                save_discord_token(self.config, GUILD, TOKEN)
        self.assertEqual(str(caught.exception), "credential_save_failed")
        close.assert_called_once()
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_read_fdopen_failure_closes_descriptor(self):
        save_discord_token(self.config, GUILD, TOKEN)
        with patch("indeces.credentials.os.fdopen", side_effect=OSError(TOKEN)), \
                patch("indeces.credentials.os.close", wraps=os.close) as close:
            with self.assertRaises(CredentialError) as caught:
                load_discord_token(self.config, GUILD)
        self.assertEqual(str(caught.exception), "credential_read_failed")
        close.assert_called_once()
        self.assertIsNone(caught.exception.__context__)

    def test_symlink_target_is_neither_read_nor_replaced(self):
        other = self.config.parent / "other.data"
        other.write_bytes(b"preserved")
        try:
            secret_path(self.config).symlink_to(other)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable for this Windows account")
        for operation in [lambda: save_discord_token(self.config, GUILD, TOKEN),
                          lambda: load_discord_token(self.config, GUILD)]:
            with self.assertRaises(CredentialError) as caught:
                operation()
            self.assertEqual(str(caught.exception), "credential_unsafe_path")
        self.assertEqual(other.read_bytes(), b"preserved")
        self.assertTrue(secret_path(self.config).is_symlink())

    def test_directory_is_rejected(self):
        secret_path(self.config).mkdir()
        with self.assertRaises(CredentialError) as caught:
            save_discord_token(self.config, GUILD, TOKEN)
        self.assertEqual(str(caught.exception), "credential_unsafe_path")


class NativePlatformTests(unittest.TestCase):
    def test_native_helpers_refuse_non_windows(self):
        with patch("indeces.credentials.os.name", "posix"):
            for function in [credentials._protect, credentials._unprotect]:
                with self.assertRaises(CredentialError) as caught:
                    function(TOKEN.encode())
                self.assertEqual(str(caught.exception), "unsupported_platform")

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI required")
    def test_real_dpapi_roundtrip_with_synthetic_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.local.toml"
            save_discord_token(config, GUILD, TOKEN)
            self.assertNotIn(TOKEN.encode(), secret_path(config).read_bytes())
            self.assertEqual(load_discord_token(config, GUILD), TOKEN)

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI required")
    def test_real_dpapi_rejects_tampered_ciphertext(self):
        encrypted = credentials._protect(TOKEN.encode())
        damaged = bytearray(encrypted)
        damaged[-1] ^= 1
        with self.assertRaises(CredentialError) as caught:
            credentials._unprotect(bytes(damaged))
        self.assertEqual(str(caught.exception), "credential_unprotect_failed")

    @unittest.skipIf(os.name == "nt", "Non-Windows behavior")
    def test_no_plaintext_fallback_on_other_platforms(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.local.toml"
            with self.assertRaises(CredentialError) as caught:
                save_discord_token(config, GUILD, TOKEN)
            self.assertEqual(str(caught.exception), "unsupported_platform")
            self.assertEqual(list(config.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
