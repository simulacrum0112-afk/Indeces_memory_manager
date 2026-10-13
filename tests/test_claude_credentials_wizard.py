"""Offline provider-vault and Console tests; all ciphertext is injected.

No real credential, DPAPI call, provider request, or service startup is used.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from indeces import api_key_wizard, credentials
from indeces.config import load_config
from indeces.credentials import CredentialError
from indeces.lock import InstanceLock


KEY = "synthetic-anthropic-key-for-offline-tests-only"
OLD_KEY = "old-synthetic-anthropic-key-offline-only"
ENV_KEY = "environment-synthetic-anthropic-key-offline-only"
OPENAI_KEY = "synthetic-openai-key-for-separation-tests-only"
ENDPOINT = "https://api.anthropic.com/v1"


def fake_protect(data):
    # Explicit test injection, never a durable plaintext fallback.
    return b"anthropic-test-only:" + bytes(value ^ 0xA5 for value in data)


def fake_unprotect(data):
    if not data.startswith(b"anthropic-test-only:"):
        raise RuntimeError("invalid synthetic ciphertext")
    return bytes(value ^ 0xA5 for value in data.removeprefix(b"anthropic-test-only:"))


class ClaudeCredentialPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = (Path(self.directory.name) / "config.local.toml").resolve()
        self.vault = credentials.anthropic_secret_path(self.config)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.protect = self.stack.enter_context(patch.object(credentials, "_protect_anthropic", side_effect=fake_protect))
        self.unprotect = self.stack.enter_context(patch.object(credentials, "_unprotect_anthropic", side_effect=fake_unprotect))

    def assert_safe(self, caught, code):
        self.assertEqual(caught.exception.code, code)
        for key in (KEY, OLD_KEY, OPENAI_KEY):
            self.assertNotIn(key, repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def payload(self, value):
        data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.vault.write_bytes(credentials._ANTHROPIC_MAGIC + fake_protect(data))

    def test_vault_magic_and_entropy_are_distinct_from_both_old_providers(self):
        self.assertEqual(self.vault, self.config.with_name("config.local.anthropic.secret"))
        self.assertEqual(len({self.vault, credentials.openai_secret_path(self.config), credentials.secret_path(self.config)}), 3)
        self.assertEqual(len({credentials._ANTHROPIC_MAGIC, credentials._OPENAI_MAGIC, credentials._MAGIC}), 3)
        self.assertEqual(len({credentials._ANTHROPIC_ENTROPY, credentials._OPENAI_ENTROPY, credentials._ENTROPY}), 3)
        self.assertIn(ENDPOINT.encode(), credentials._ANTHROPIC_ENTROPY)

    def test_absent_vault_does_not_decrypt_or_load_other_providers(self):
        with patch.object(credentials, "load_openai_key", side_effect=AssertionError("wrong provider")):
            self.assertIsNone(credentials.load_anthropic_key(self.config))
        self.unprotect.assert_not_called()

    def test_roundtrip_only_changes_anthropic_ciphertext_and_binds_official_endpoint(self):
        self.config.write_text("synthetic public configuration", encoding="utf-8")
        openai = credentials.openai_secret_path(self.config)
        discord = credentials.secret_path(self.config)
        openai.write_bytes(b"existing opaque OpenAI ciphertext")
        discord.write_bytes(b"existing opaque Discord ciphertext")
        credentials.save_anthropic_key(self.config, " " + KEY + " ")
        self.assertEqual(json.loads(self.protect.call_args.args[0]),
                         {"version": 1, "provider": "anthropic", "base_url": ENDPOINT, "api_key": KEY})
        self.assertEqual(credentials.load_anthropic_key(self.config), KEY)
        self.assertNotIn(KEY.encode(), self.vault.read_bytes())
        self.assertEqual(openai.read_bytes(), b"existing opaque OpenAI ciphertext")
        self.assertEqual(discord.read_bytes(), b"existing opaque Discord ciphertext")
        self.assertEqual(self.config.read_text(), "synthetic public configuration")
        self.assertEqual({p.name for p in self.config.parent.iterdir()},
                         {self.config.name, self.vault.name, openai.name, discord.name})

    def test_invalid_key_never_encrypts_or_creates_a_file(self):
        with self.assertRaises(CredentialError) as caught:
            credentials.save_anthropic_key(self.config, "short")
        self.assert_safe(caught, "invalid_api_key")
        self.protect.assert_not_called()
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_wrong_header_cross_provider_ciphertext_and_size_fail_before_decryption(self):
        for value in (b"", b"wrong", credentials._ANTHROPIC_MAGIC,
                      credentials._OPENAI_MAGIC + fake_protect(KEY.encode()),
                      credentials._MAGIC + fake_protect(KEY.encode()),
                      credentials._ANTHROPIC_MAGIC + b"x" * (credentials._MAX_CIPHERTEXT + 1)):
            with self.subTest(length=len(value)):
                self.vault.write_bytes(value)
                with self.assertRaises(CredentialError) as caught:
                    credentials.load_anthropic_key(self.config)
                self.assert_safe(caught, "credential_corrupt")
        self.unprotect.assert_not_called()

    def test_strict_payload_checks_provider_endpoint_version_and_duplicate_fields(self):
        good = {"version": 1, "provider": "anthropic", "base_url": ENDPOINT, "api_key": KEY}
        for value in (b"not-json", b"\xff", {}, [],
                      {**good, "version": True}, {**good, "version": 2},
                      {**good, "provider": "openai"}, {**good, "provider": "Anthropic"},
                      {**good, "base_url": "https://api.openai.com/v1"},
                      {**good, "base_url": ENDPOINT + "/"},
                      {**good, "base_url": "https://other.example/v1"},
                      {**good, "model": "claude-opus-5-5"}, {**good, "api_key": " " + KEY},
                      {**good, "api_key": 42}, {**good, "api_key": "short"},
                      ('{"version":1,"provider":"anthropic","base_url":"' + ENDPOINT +
                       '","api_key":"' + KEY + '","api_key":"' + KEY + '"}').encode(),
                      ('{"version":NaN,"provider":"anthropic","base_url":"' + ENDPOINT +
                       '","api_key":"' + KEY + '"}').encode()):
            with self.subTest(value_type=type(value).__name__):
                self.payload(value)
                with self.assertRaises(CredentialError) as caught:
                    credentials.load_anthropic_key(self.config)
                self.assert_safe(caught, "credential_corrupt")

    def test_protector_failure_is_scrubbed_without_plaintext_or_partial_file(self):
        self.protect.side_effect = RuntimeError(KEY)
        with self.assertRaises(CredentialError) as caught:
            credentials.save_anthropic_key(self.config, KEY)
        self.assert_safe(caught, "credential_save_failed")
        self.assertEqual(list(self.config.parent.iterdir()), [])

    def test_unprotector_failure_is_scrubbed_without_exception_context(self):
        credentials.save_anthropic_key(self.config, KEY)
        self.unprotect.side_effect = RuntimeError(KEY)
        with self.assertRaises(CredentialError) as caught:
            credentials.load_anthropic_key(self.config)
        self.assert_safe(caught, "credential_unprotect_failed")

    def test_ciphertext_and_plaintext_return_bounds_are_independent(self):
        for value in (None, "not-bytes", b"", b"x" * (credentials._MAX_CIPHERTEXT + 1)):
            with self.subTest(protected_type=type(value).__name__):
                self.protect.side_effect = None
                self.protect.return_value = value
                with self.assertRaises(CredentialError) as caught:
                    credentials.save_anthropic_key(self.config, KEY)
                self.assert_safe(caught, "credential_protect_failed")
        self.protect.side_effect = fake_protect
        credentials.save_anthropic_key(self.config, KEY)
        for value in (None, "not-bytes", b"", b"x" * (16 * 1024 + 1)):
            with self.subTest(plaintext_type=type(value).__name__):
                self.unprotect.side_effect = None
                self.unprotect.return_value = value
                with self.assertRaises(CredentialError) as caught:
                    credentials.load_anthropic_key(self.config)
                self.assert_safe(caught, "credential_corrupt")

    def test_fsync_and_replace_failure_preserve_prior_ciphertext_and_remove_temporaries(self):
        credentials.save_anthropic_key(self.config, OLD_KEY)
        old = self.vault.read_bytes()
        for name in ("fsync", "replace"):
            with self.subTest(failure=name), patch.object(credentials.os, name, side_effect=OSError(KEY)):
                with self.assertRaises(CredentialError) as caught:
                    credentials.save_anthropic_key(self.config, KEY)
                self.assert_safe(caught, "credential_save_failed")
            self.assertEqual(self.vault.read_bytes(), old)
            self.assertEqual(list(self.config.parent.iterdir()), [self.vault])

    def test_opened_file_identity_is_checked_before_any_decryption(self):
        credentials.save_anthropic_key(self.config, KEY)
        real_fstat = os.fstat

        def changed_identity(descriptor):
            info = real_fstat(descriptor)
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1)

        with patch.object(credentials.os, "fstat", side_effect=changed_identity):
            with self.assertRaises(CredentialError) as caught:
                credentials.load_anthropic_key(self.config)
        self.assert_safe(caught, "credential_unsafe_path")
        self.unprotect.assert_not_called()

    def test_directory_target_is_rejected_before_encrypting_or_decrypting(self):
        self.vault.mkdir()
        for operation in (lambda: credentials.save_anthropic_key(self.config, KEY),
                          lambda: credentials.load_anthropic_key(self.config)):
            with self.assertRaises(CredentialError) as caught:
                operation()
            self.assert_safe(caught, "credential_unsafe_path")
        self.protect.assert_not_called()
        self.unprotect.assert_not_called()


class ClaudeCredentialPlatformTests(unittest.TestCase):
    def test_anthropic_helpers_use_endpoint_bound_entropy(self):
        with patch.object(credentials, "_crypt", return_value=b"synthetic ciphertext") as crypt:
            credentials._protect_anthropic(KEY.encode())
            crypt.assert_called_once_with(KEY.encode(), protect=True, entropy=credentials._ANTHROPIC_ENTROPY)
            crypt.reset_mock()
            credentials._unprotect_anthropic(b"synthetic ciphertext")
            crypt.assert_called_once_with(b"synthetic ciphertext", protect=False, entropy=credentials._ANTHROPIC_ENTROPY)

    def test_native_helpers_fail_closed_off_windows_without_calling_dpapi(self):
        with patch.object(credentials.os, "name", "posix"):
            for helper in (credentials._protect_anthropic, credentials._unprotect_anthropic):
                with self.subTest(helper=helper.__name__), self.assertRaises(CredentialError) as caught:
                    helper(KEY.encode())
                self.assertEqual(caught.exception.code, "unsupported_platform")
                self.assertIsNone(caught.exception.__context__)


class ClaudeApiKeyWizardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = (Path(self.directory.name) / "config.local.toml").resolve()
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        text = example.read_text(encoding="utf-8")
        text = text.replace('[adapter]\n', '[adapter]\nprovider = "anthropic"\n')
        text = text.replace('model = "gpt-6.1-sol"', 'model = "claude-opus-5-5"')
        text = text.replace('https://api.openai.com/v1', ENDPOINT)
        text = text.replace('reasoning = "medium"', 'reasoning = "high"')
        self.path.write_text(text, encoding="utf-8")
        self.original = self.path.read_bytes()
        self.config = load_config(self.path)
        self.vault = credentials.anthropic_secret_path(self.path)
        self.openai = credentials.openai_secret_path(self.path)
        self.openai.write_bytes(b"separate opaque OpenAI ciphertext")
        self.output = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(credentials, "_protect_anthropic", side_effect=fake_protect))
        self.stack.enter_context(patch.object(credentials, "_unprotect_anthropic", side_effect=fake_unprotect))
        self.openai_loader = self.stack.enter_context(patch.object(api_key_wizard, "load_openai_key", side_effect=AssertionError("wrong provider")))
        self.openai_saver = self.stack.enter_context(patch.object(api_key_wizard, "save_openai_key", side_effect=AssertionError("wrong provider")))

    def configure(self, *, answers=None, keys=None, input_fn=None, secret_fn=None, output=None):
        ordinary = input_fn if input_fn is not None else Mock(side_effect=answers if answers is not None else ["y"])
        private = secret_fn if secret_fn is not None else Mock(side_effect=keys if keys is not None else [KEY])
        saved = api_key_wizard.configure_api_key(self.path, input_fn=ordinary, secret_fn=private,
                                                 output=self.output.append if output is None else output)
        return saved, ordinary, private

    def previous(self):
        credentials.save_anthropic_key(self.path, OLD_KEY)
        return self.vault.read_bytes()

    def assert_safe_and_released(self):
        visible = "\n".join(self.output)
        for key in (KEY, OLD_KEY, ENV_KEY, OPENAI_KEY):
            self.assertNotIn(key, visible)
            self.assertNotIn(key.encode(), self.path.read_bytes())
            if self.vault.is_file():
                self.assertNotIn(key.encode(), self.vault.read_bytes())
        self.assertEqual(self.openai.read_bytes(), b"separate opaque OpenAI ciphertext")
        self.openai_loader.assert_not_called()
        self.openai_saver.assert_not_called()
        lease = InstanceLock(self.config.state_dir)
        lease.close()

    def test_claude_wizard_saves_provider_key_without_network_or_config_change(self):
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")) as connect, \
                patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")) as connect_method:
            saved, ordinary, private = self.configure()
        self.assertTrue(saved)
        self.assertEqual(credentials.load_anthropic_key(self.path), KEY)
        self.assertEqual(self.path.read_bytes(), self.original)
        ordinary.assert_called_once()
        private.assert_called_once()
        self.assertIn("Claude (Anthropic)", private.call_args.args[0])
        self.assertIn("claude-opus-5-5", "\n".join(self.output))
        self.assertIn("effort=high", "\n".join(self.output))
        connect.assert_not_called()
        connect_method.assert_not_called()
        self.assert_safe_and_released()

    def test_fable_selection_reuses_anthropic_vault_without_model_bound_payload(self):
        old = self.previous()
        self.path.write_text(self.path.read_text().replace("claude-opus-5-5", "claude-fable-5-1"), encoding="utf-8")
        selected_config = self.path.read_bytes()
        saved, _, _ = self.configure(keys=[""])
        self.assertTrue(saved)
        self.assertEqual(self.vault.read_bytes(), old)
        self.assertEqual(self.path.read_bytes(), selected_config)
        self.assertNotIn(b"model", fake_unprotect(old[len(credentials._ANTHROPIC_MAGIC):]))
        self.assertIn("claude-fable-5-1", "\n".join(self.output))
        self.assert_safe_and_released()

    def test_environment_override_names_only_anthropic_and_does_not_supply_saved_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ENV_KEY, "OPENAI_API_KEY": OPENAI_KEY}):
            saved, _, private = self.configure(keys=["", KEY])
        self.assertTrue(saved)
        self.assertEqual(private.call_count, 2)
        self.assertEqual(credentials.load_anthropic_key(self.path), KEY)
        self.assertIn("ANTHROPIC_API_KEY is set", "\n".join(self.output))
        self.assertNotIn("OPENAI_API_KEY is set", "\n".join(self.output))
        self.assert_safe_and_released()

    def test_running_state_lease_blocks_before_any_input_or_decryption(self):
        old = self.previous()
        lease = InstanceLock(self.config.state_dir)
        try:
            with patch.object(api_key_wizard, "load_anthropic_key", side_effect=AssertionError("unexpected decrypt")):
                saved, ordinary, private = self.configure()
        finally:
            lease.close()
        self.assertFalse(saved)
        ordinary.assert_not_called()
        private.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), old)
        self.assert_safe_and_released()

    def test_decline_or_interrupted_input_leaves_ciphertext_unchanged(self):
        old = self.previous()
        for answers, secret in ((["n"], Mock(return_value=KEY)),
                                (["y"], Mock(side_effect=EOFError)),
                                (["y"], Mock(side_effect=KeyboardInterrupt)),
                                (["y"], Mock(side_effect=OSError(KEY)))):
            with self.subTest(secret=type(secret.side_effect).__name__):
                saved, _, _ = self.configure(answers=answers, secret_fn=secret)
                self.assertFalse(saved)
                self.assertEqual(self.vault.read_bytes(), old)
                self.assert_safe_and_released()

    def test_config_change_during_input_blocks_confirmation_and_save(self):
        old = self.previous()

        def edited(prompt):
            self.path.write_bytes(self.original + b"\n# synthetic concurrent edit\n")
            return KEY

        saved, ordinary, _ = self.configure(secret_fn=edited)
        self.assertFalse(saved)
        ordinary.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), old)
        self.assert_safe_and_released()

    def test_vault_change_during_confirmation_is_not_overwritten_or_rolled_back(self):
        self.previous()
        external = credentials._ANTHROPIC_MAGIC + fake_protect(b"synthetic external edit")

        def edited(prompt):
            self.vault.write_bytes(external)
            return "y"

        saved, _, _ = self.configure(input_fn=edited)
        self.assertFalse(saved)
        self.assertEqual(self.vault.read_bytes(), external)
        self.assert_safe_and_released()

    def test_interrupted_save_after_replace_restores_prior_encrypted_bytes(self):
        old = self.previous()
        native_save = credentials.save_anthropic_key

        def interrupted(path, key):
            native_save(path, key)
            raise KeyboardInterrupt()

        with patch.object(api_key_wizard, "save_anthropic_key", side_effect=interrupted):
            saved, _, _ = self.configure()
        self.assertFalse(saved)
        self.assertEqual(self.vault.read_bytes(), old)
        self.assert_safe_and_released()

    def test_interrupted_save_with_no_old_vault_removes_new_ciphertext(self):
        native_save = credentials.save_anthropic_key

        def interrupted(path, key):
            native_save(path, key)
            raise EOFError()

        with patch.object(api_key_wizard, "save_anthropic_key", side_effect=interrupted):
            saved, _, _ = self.configure()
        self.assertFalse(saved)
        self.assertFalse(self.vault.exists())
        self.assert_safe_and_released()

    def test_post_commit_terminal_failure_keeps_successful_result(self):
        def output(message):
            self.output.append(message)
            if self.vault.exists():
                raise KeyboardInterrupt()

        saved, _, _ = self.configure(output=output)
        self.assertTrue(saved)
        self.assertEqual(credentials.load_anthropic_key(self.path), KEY)
        self.assert_safe_and_released()

    def test_pre_commit_terminal_failure_does_not_prompt_or_change_ciphertext(self):
        old = self.previous()
        saved, ordinary, private = self.configure(output=Mock(side_effect=OSError(KEY)))
        self.assertFalse(saved)
        ordinary.assert_not_called()
        private.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), old)
        self.assert_safe_and_released()

    def test_native_save_failure_is_reported_as_fixed_code_and_restores_old_ciphertext(self):
        old = self.previous()
        with patch.object(api_key_wizard, "save_anthropic_key", side_effect=CredentialError("unsupported_platform")):
            saved, _, _ = self.configure()
        self.assertFalse(saved)
        self.assertEqual(self.vault.read_bytes(), old)
        self.assertIn("unsupported_platform", "\n".join(self.output))
        self.assert_safe_and_released()

    def test_unknown_provider_or_unofficial_endpoint_fails_before_secret_input(self):
        for adapter in (SimpleNamespace(provider="other"),
                        SimpleNamespace(provider="anthropic", base_url="https://other.example/v1", model="claude-opus-5-5")):
            with self.subTest(provider=adapter.provider), patch.object(api_key_wizard, "load_config", return_value=SimpleNamespace(adapter=adapter)):
                saved, ordinary, private = self.configure()
            self.assertFalse(saved)
            ordinary.assert_not_called()
            private.assert_not_called()
            self.assertFalse(self.vault.exists())
            self.assert_safe_and_released()


if __name__ == "__main__":
    unittest.main()
