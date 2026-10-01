from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces import credentials
from indeces.credentials import (CredentialError, load_openai_key, openai_secret_path,
                                 save_openai_key, validate_api_key)


KEY = "synthetic-openai-key-for-offline-tests-only"
SECOND_KEY = "replacement-synthetic-openai-key-offline-only"
DISCORD_TOKEN = "synthetic-discord-token-for-compatibility-only"
GUILD = "123456789012345678"


def fake_protect(data: bytes) -> bytes:
    # This test cipher is explicitly injected, never a production fallback.
    return b"openai-test-only:" + bytes(value ^ 0xA5 for value in data)


def fake_unprotect(data: bytes) -> bytes:
    if not data.startswith(b"openai-test-only:"):
        raise RuntimeError("invalid synthetic ciphertext")
    return bytes(value ^ 0xA5 for value in data.removeprefix(b"openai-test-only:"))


class ApiKeyValidationTests(unittest.TestCase):
    def test_opaque_ascii_and_only_outer_ascii_spaces_are_trimmed(self):
        for value in [KEY, "a" * 20, "a" * 4096, "!" * 20, '"\\' * 20]:
            with self.subTest(length=len(value)):
                self.assertEqual(validate_api_key("  " + value + "  "), value)

    def test_invalid_keys_have_fixed_diagnostics_without_echo_or_context(self):
        for value in [None, 123, b"not-a-string", "", " " * 30, "short", "a" * 4097,
                      KEY + "\n", "\t" + KEY, KEY + "\x00", KEY + "\x7f",
                      KEY + "é", KEY + " secret", "\u00a0" + KEY]:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(CredentialError) as caught:
                validate_api_key(value)
            self.assertEqual(caught.exception.code, "invalid_api_key")
            self.assertNotIn(KEY, repr(caught.exception))
            self.assertIsNone(caught.exception.__context__)


class OpenAiPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name).resolve() / "config.local.toml"
        protector = patch.object(credentials, "_protect_openai", side_effect=fake_protect)
        unprotector = patch.object(credentials, "_unprotect_openai", side_effect=fake_unprotect)
        self.protect = protector.start()
        self.unprotect = unprotector.start()
        self.addCleanup(protector.stop)
        self.addCleanup(unprotector.stop)

    def assert_safe_error(self, caught, code):
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(KEY, repr(caught.exception))
        self.assertNotIn(SECOND_KEY, repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def write_payload(self, payload):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        openai_secret_path(self.config).write_bytes(credentials._OPENAI_MAGIC + fake_protect(payload))

    def test_openai_path_is_adjacent_and_distinct_from_discord(self):
        self.assertEqual(openai_secret_path(self.config), self.config.with_name("config.local.openai.secret"))
        self.assertNotEqual(openai_secret_path(self.config), credentials.secret_path(self.config))
        self.assertNotEqual(credentials._OPENAI_MAGIC, credentials._MAGIC)
        self.assertNotEqual(credentials._OPENAI_ENTROPY, credentials._ENTROPY)

    def test_absent_returns_none_without_decrypting(self):
        self.assertIsNone(load_openai_key(self.config))
        self.unprotect.assert_not_called()

    def test_roundtrip_is_encrypted_atomic_and_does_not_change_config(self):
        self.config.write_text("synthetic config remains unchanged", encoding="utf-8")
        save_openai_key(self.config, " " + KEY + " ")
        target = openai_secret_path(self.config)
        ciphertext = target.read_bytes()
        self.assertTrue(ciphertext.startswith(credentials._OPENAI_MAGIC))
        self.assertNotIn(KEY.encode(), ciphertext)
        self.assertNotIn(b'"provider":"openai"', ciphertext)
        self.assertEqual(load_openai_key(self.config), KEY)
        self.assertEqual(json.loads(self.protect.call_args.args[0]),
                         {"version": 1, "provider": "openai", "api_key": KEY})
        save_openai_key(self.config, SECOND_KEY)
        self.assertEqual(load_openai_key(self.config), SECOND_KEY)
        self.assertEqual(self.config.read_text(), "synthetic config remains unchanged")
        self.assertEqual({path.name for path in self.config.parent.iterdir()}, {self.config.name, target.name})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_invalid_key_never_calls_protect_or_writes(self):
        with self.assertRaises(CredentialError) as caught:
            save_openai_key(self.config, "too-short")
        self.assert_safe_error(caught, "invalid_api_key")
        self.protect.assert_not_called()
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_bad_header_or_ciphertext_bound_is_rejected_before_decryption(self):
        for contents in [b"", b"wrong header", credentials._OPENAI_MAGIC,
                         credentials._MAGIC + fake_protect(KEY.encode()),
                         credentials._OPENAI_MAGIC + b"x" * (credentials._MAX_CIPHERTEXT + 1)]:
            with self.subTest(length=len(contents)):
                openai_secret_path(self.config).write_bytes(contents)
                with self.assertRaises(CredentialError) as caught:
                    load_openai_key(self.config)
                self.assert_safe_error(caught, "credential_corrupt")
        self.unprotect.assert_not_called()

    def test_strict_payload_rejects_wrong_schema_provider_fields_and_duplicates(self):
        valid = {"version": 1, "provider": "openai", "api_key": KEY}
        payloads = [b"not-json", b"\xff", b"[]", {},
                    {**valid, "version": True}, {**valid, "version": 1.0}, {**valid, "version": 2},
                    {**valid, "provider": "discord"}, {**valid, "provider": "OpenAI"},
                    {**valid, "provider": None}, {"version": 1, "api_key": KEY},
                    {**valid, "extra": KEY}, {**valid, "api_key": 42},
                    {**valid, "api_key": "short"}, {**valid, "api_key": " " + KEY},
                    {**valid, "api_key": KEY + "\n"}, {**valid, "api_key": "a" * 4097},
                    ('{"version":1,"version":1,"provider":"openai","api_key":"' + KEY + '"}').encode(),
                    ('{"version":1,"provider":"openai","api_key":"' + KEY + '","api_key":"' + KEY + '"}').encode(),
                    ('{"version":NaN,"provider":"openai","api_key":"' + KEY + '"}').encode(),
                    ('{"version":Infinity,"provider":"openai","api_key":"' + KEY + '"}').encode(),
                    b"[" * 1100 + b"0" + b"]" * 1100]
        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__):
                self.write_payload(payload)
                with self.assertRaises(CredentialError) as caught:
                    load_openai_key(self.config)
                self.assert_safe_error(caught, "credential_corrupt")

    def test_decrypted_payload_has_an_independent_length_and_type_bound(self):
        save_openai_key(self.config, KEY)
        for plaintext in [None, "not-bytes", b"", b"x" * (16 * 1024 + 1)]:
            self.unprotect.side_effect = None
            self.unprotect.return_value = plaintext
            with self.subTest(value_type=type(plaintext).__name__), self.assertRaises(CredentialError) as caught:
                load_openai_key(self.config)
            self.assert_safe_error(caught, "credential_corrupt")

    def test_ciphertext_from_protector_is_bounded_before_any_file_write(self):
        for ciphertext in [None, "not-bytes", b"", b"x" * (credentials._MAX_CIPHERTEXT + 1)]:
            self.protect.side_effect = None
            self.protect.return_value = ciphertext
            with self.subTest(value_type=type(ciphertext).__name__), self.assertRaises(CredentialError) as caught:
                save_openai_key(self.config, KEY)
            self.assert_safe_error(caught, "credential_protect_failed")
            self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_protect_failure_has_no_plaintext_files_or_exception_context(self):
        self.protect.side_effect = RuntimeError(KEY)
        with self.assertRaises(CredentialError) as caught:
            save_openai_key(self.config, KEY)
        self.assert_safe_error(caught, "credential_save_failed")
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_native_protect_failure_code_is_preserved(self):
        for code in ["credential_protect_failed", "unsupported_platform"]:
            self.protect.side_effect = CredentialError(code)
            with self.subTest(code=code), self.assertRaises(CredentialError) as caught:
                save_openai_key(self.config, KEY)
            self.assert_safe_error(caught, code)
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_unprotect_failure_is_scrubbed(self):
        save_openai_key(self.config, KEY)
        self.unprotect.side_effect = RuntimeError(KEY)
        with self.assertRaises(CredentialError) as caught:
            load_openai_key(self.config)
        self.assert_safe_error(caught, "credential_unprotect_failed")

    def test_failed_replace_preserves_old_ciphertext_and_cleans_temporary(self):
        save_openai_key(self.config, KEY)
        target = openai_secret_path(self.config)
        original = target.read_bytes()
        with patch.object(credentials.os, "replace", side_effect=OSError(SECOND_KEY)):
            with self.assertRaises(CredentialError) as caught:
                save_openai_key(self.config, SECOND_KEY)
        self.assert_safe_error(caught, "credential_save_failed")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(load_openai_key(self.config), KEY)
        self.assertEqual(list(self.config.parent.iterdir()), [target])

    def test_failed_fsync_preserves_old_ciphertext_and_cleans_temporary(self):
        save_openai_key(self.config, KEY)
        target = openai_secret_path(self.config)
        original = target.read_bytes()
        with patch.object(credentials.os, "fsync", side_effect=OSError(KEY)):
            with self.assertRaises(CredentialError) as caught:
                save_openai_key(self.config, SECOND_KEY)
        self.assert_safe_error(caught, "credential_save_failed")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.config.parent.iterdir()), [target])

    def test_failed_fdopen_closes_writer_descriptor_and_cleans_temporary(self):
        with patch.object(credentials.os, "fdopen", side_effect=OSError(KEY)) as wrapped:
            with self.assertRaises(CredentialError) as caught:
                save_openai_key(self.config, KEY)
        self.assert_safe_error(caught, "credential_save_failed")
        with self.assertRaises(OSError):
            os.fstat(wrapped.call_args.args[0])
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_failed_fdopen_closes_reader_descriptor(self):
        save_openai_key(self.config, KEY)
        with patch.object(credentials.os, "fdopen", side_effect=OSError(KEY)) as wrapped:
            with self.assertRaises(CredentialError) as caught:
                load_openai_key(self.config)
        self.assert_safe_error(caught, "credential_read_failed")
        with self.assertRaises(OSError):
            os.fstat(wrapped.call_args.args[0])

    def test_failed_open_is_scrubbed(self):
        save_openai_key(self.config, KEY)
        with patch.object(credentials.os, "open", side_effect=OSError(KEY)):
            with self.assertRaises(CredentialError) as caught:
                load_openai_key(self.config)
        self.assert_safe_error(caught, "credential_read_failed")

    def test_directory_target_is_rejected_before_read_or_encrypt(self):
        openai_secret_path(self.config).mkdir()
        for operation in [lambda: save_openai_key(self.config, KEY), lambda: load_openai_key(self.config)]:
            with self.assertRaises(CredentialError) as caught:
                operation()
            self.assert_safe_error(caught, "credential_unsafe_path")
        self.protect.assert_not_called()
        self.unprotect.assert_not_called()

    def test_reparse_parent_is_rejected_before_read_or_encrypt(self):
        parent = self.config.parent
        real_lstat = Path.lstat
        def reparse_lstat(path, *args, **kwargs):
            if path == parent:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", new=reparse_lstat):
            for operation in [lambda: save_openai_key(self.config, KEY), lambda: load_openai_key(self.config)]:
                with self.assertRaises(CredentialError) as caught:
                    operation()
                self.assert_safe_error(caught, "credential_unsafe_path")
        self.protect.assert_not_called()
        self.unprotect.assert_not_called()
        self.assertEqual(list(parent.iterdir()), [])

    def test_symlink_target_is_neither_read_nor_replaced(self):
        other = self.config.parent / "preserved.data"
        other.write_bytes(b"synthetic notes")
        target = openai_secret_path(self.config)
        try:
            target.symlink_to(other)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable for this Windows account")
        for operation in [lambda: save_openai_key(self.config, KEY), lambda: load_openai_key(self.config)]:
            with self.assertRaises(CredentialError) as caught:
                operation()
            self.assert_safe_error(caught, "credential_unsafe_path")
        self.assertEqual(other.read_bytes(), b"synthetic notes")
        self.assertTrue(target.is_symlink())

    def test_symlink_parent_is_rejected(self):
        other = self.config.parent / "actual"
        other.mkdir()
        link = self.config.parent / "link"
        try:
            link.symlink_to(other, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation unavailable for this Windows account")
        config = link / "synthetic.toml"
        for operation in [lambda: save_openai_key(config, KEY), lambda: load_openai_key(config)]:
            with self.assertRaises(CredentialError) as caught:
                operation()
            self.assert_safe_error(caught, "credential_unsafe_path")
        self.assertEqual(list(other.iterdir()), [])

    def test_open_descriptor_identity_must_match_the_checked_path(self):
        save_openai_key(self.config, KEY)
        real_fstat = os.fstat
        def different_identity(descriptor):
            info = real_fstat(descriptor)
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1)
        with patch.object(credentials.os, "fstat", side_effect=different_identity):
            with self.assertRaises(CredentialError) as caught:
                load_openai_key(self.config)
        self.assert_safe_error(caught, "credential_unsafe_path")
        self.unprotect.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Windows prevents replacing this open credential handle")
    def test_replaced_target_between_check_and_open_is_rejected(self):
        save_openai_key(self.config, KEY)
        target = openai_secret_path(self.config)
        replacement = self.config.parent / "replacement.data"
        replacement.write_bytes(credentials._OPENAI_MAGIC + fake_protect(b"{}"))
        real_open = os.open
        def swap_after_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            os.replace(replacement, target)
            return descriptor
        with patch.object(credentials.os, "open", side_effect=swap_after_open):
            with self.assertRaises(CredentialError) as caught:
                load_openai_key(self.config)
        self.assert_safe_error(caught, "credential_unsafe_path")
        self.unprotect.assert_not_called()

    def test_prepublication_path_recheck_preserves_previous_file(self):
        save_openai_key(self.config, KEY)
        target = openai_secret_path(self.config)
        original = target.read_bytes()
        with patch.object(credentials, "_check_openai_secret_path",
                          side_effect=[None, CredentialError("credential_unsafe_path")]):
            with self.assertRaises(CredentialError) as caught:
                save_openai_key(self.config, SECOND_KEY)
        self.assert_safe_error(caught, "credential_unsafe_path")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.config.parent.iterdir()), [target])

    def test_discord_format_and_saved_ciphertext_remain_unchanged(self):
        self.assertEqual(credentials._MAGIC, b"INDECES-DISCORD-DPAPI\x00\x01")
        self.assertEqual(credentials._ENTROPY, b"Indeces_memory_manager.discord.credentials.v1")
        with patch.object(credentials, "_protect", side_effect=fake_protect), \
                patch.object(credentials, "_unprotect", side_effect=fake_unprotect):
            credentials.save_discord_token(self.config, GUILD, DISCORD_TOKEN)
            discord = credentials.secret_path(self.config)
            previous = discord.read_bytes()
            self.assertEqual(json.loads(fake_unprotect(previous[len(credentials._MAGIC):])),
                             {"version": 1, "guild_id": GUILD, "token": DISCORD_TOKEN})
            save_openai_key(self.config, KEY)
            self.assertEqual(discord.read_bytes(), previous)
            self.assertEqual(credentials.load_discord_token(self.config, GUILD), DISCORD_TOKEN)
            self.assertEqual(load_openai_key(self.config), KEY)


class OpenAiNativePlatformTests(unittest.TestCase):
    def test_native_helpers_reject_other_platforms_without_plaintext_fallback(self):
        with patch.object(credentials.os, "name", "posix"):
            for helper in [credentials._protect_openai, credentials._unprotect_openai]:
                with self.subTest(helper=helper.__name__), self.assertRaises(CredentialError) as caught:
                    helper(KEY.encode())
                self.assertEqual(caught.exception.code, "unsupported_platform")
                self.assertIsNone(caught.exception.__context__)

    def test_provider_helpers_use_separate_entropy_and_legacy_default(self):
        with patch.object(credentials, "_crypt", return_value=b"synthetic cipher") as crypt:
            credentials._protect(KEY.encode())
            crypt.assert_called_with(KEY.encode(), protect=True)
            credentials._unprotect(b"synthetic cipher")
            crypt.assert_called_with(b"synthetic cipher", protect=False)
            credentials._protect_openai(KEY.encode())
            crypt.assert_called_with(KEY.encode(), protect=True, entropy=credentials._OPENAI_ENTROPY)
            credentials._unprotect_openai(b"synthetic cipher")
            crypt.assert_called_with(b"synthetic cipher", protect=False, entropy=credentials._OPENAI_ENTROPY)

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI required")
    def test_real_dpapi_roundtrip_with_synthetic_key_only(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "synthetic.toml"
            save_openai_key(config, KEY)
            self.assertNotIn(KEY.encode(), openai_secret_path(config).read_bytes())
            self.assertEqual(load_openai_key(config), KEY)
            save_openai_key(config, SECOND_KEY)
            self.assertEqual(load_openai_key(config), SECOND_KEY)

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI required")
    def test_real_dpapi_rejects_tampering_and_other_provider_entropy(self):
        encrypted = credentials._protect_openai(KEY.encode())
        damaged = bytearray(encrypted)
        damaged[-1] ^= 1
        with self.assertRaises(CredentialError) as caught:
            credentials._unprotect_openai(bytes(damaged))
        self.assertEqual(caught.exception.code, "credential_unprotect_failed")
        with self.assertRaises(CredentialError):
            credentials._unprotect(encrypted)
        with self.assertRaises(CredentialError):
            credentials._unprotect_openai(credentials._protect(DISCORD_TOKEN.encode()))

    @unittest.skipIf(os.name == "nt", "Non-Windows durable-save behavior")
    def test_save_on_other_platforms_leaves_no_credential_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "synthetic.toml"
            with self.assertRaises(CredentialError) as caught:
                save_openai_key(config, KEY)
            self.assertEqual(caught.exception.code, "unsupported_platform")
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(list(config.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
