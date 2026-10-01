from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from indeces import api_key_wizard, credentials
from indeces.config import load_config
from indeces.credentials import CredentialError
from indeces.lock import InstanceLock


NEW_KEY = "sk-offline-api-wizard-new-synthetic-key"
OLD_KEY = "sk-offline-api-wizard-old-synthetic-key"
ENV_KEY = "sk-offline-api-wizard-environment-synthetic-key"
OLD_CIPHER = b"opaque-test-ciphertext-old-no-plaintext-key"
NEW_CIPHER = b"opaque-test-ciphertext-new-no-plaintext-key"
EXTERNAL_CIPHER = b"opaque-test-ciphertext-owned-by-external-editor"


class ApiKeyWizardTests(unittest.TestCase):
    """Wizard orchestration tests; crypto has a separate credential test suite."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = (Path(self.directory.name) / "config.local.toml").resolve()
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        self.original = example.read_bytes()
        self.path.write_bytes(self.original)
        self.config = load_config(self.path)
        self.vault = credentials.openai_secret_path(self.path)
        self.output = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.loader = self.stack.enter_context(patch.object(api_key_wizard, "load_openai_key", side_effect=self.fake_load))
        self.saver = self.stack.enter_context(patch.object(api_key_wizard, "save_openai_key", side_effect=self.fake_save))

    def fake_load(self, path):
        self.assertEqual(Path(path), self.path)
        try:
            ciphertext = self.vault.read_bytes()
        except FileNotFoundError:
            return None
        if ciphertext == OLD_CIPHER:
            return OLD_KEY
        if ciphertext == NEW_CIPHER:
            return NEW_KEY
        raise CredentialError("credential_corrupt")

    def fake_save(self, path, key):
        self.assertEqual(Path(path), self.path)
        if key == OLD_KEY:
            self.vault.write_bytes(OLD_CIPHER)
        elif key == NEW_KEY:
            self.vault.write_bytes(NEW_CIPHER)
        else:
            raise AssertionError("unexpected synthetic key")

    def previous(self):
        self.vault.write_bytes(OLD_CIPHER)
        return OLD_CIPHER

    def configure(self, *, answers=None, keys=None, input_fn=None, secret_fn=None, output=None):
        ordinary = input_fn if input_fn is not None else Mock(side_effect=answers if answers is not None else ["y"])
        private = secret_fn if secret_fn is not None else Mock(side_effect=keys if keys is not None else [NEW_KEY])
        saved = api_key_wizard.configure_api_key(self.path, input_fn=ordinary, secret_fn=private,
                                                  output=self.output.append if output is None else output)
        return saved, ordinary, private

    def assert_released(self):
        lease = InstanceLock(self.config.state_dir)
        lease.close()

    def assert_no_secrets(self):
        visible = "\n".join(self.output)
        for key in (NEW_KEY, OLD_KEY, ENV_KEY):
            self.assertNotIn(key, visible)
            self.assertNotIn(key.encode(), self.path.read_bytes())
            if self.vault.is_file():
                self.assertNotIn(key.encode(), self.vault.read_bytes())

    def test_save_without_guild_changes_only_openai_vault_and_never_connects(self):
        self.assertEqual(self.config.discord.guild_id, "")
        discord_vault = credentials.secret_path(self.path)
        discord_vault.write_bytes(b"opaque-separate-discord-vault")
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")) as connect, \
                patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")) as connect_method:
            saved, ordinary, private = self.configure()
        self.assertTrue(saved)
        ordinary.assert_called_once()
        private.assert_called_once()
        self.saver.assert_called_once_with(self.path, NEW_KEY)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(discord_vault.read_bytes(), b"opaque-separate-discord-vault")
        self.assertEqual(self.vault.read_bytes(), NEW_CIPHER)
        self.assertFalse(self.config.scratch_dir.exists())
        self.assertFalse(self.config.knowledge_dir.exists())
        connect.assert_not_called()
        connect_method.assert_not_called()
        self.assert_no_secrets()
        self.assert_released()

    def test_default_hidden_reader_is_used_without_falling_back_to_ordinary_input(self):
        ordinary = Mock(return_value="yes")
        with patch.object(api_key_wizard, "prompt_secret", return_value=NEW_KEY) as private:
            saved = api_key_wizard.configure_api_key(self.path, input_fn=ordinary, output=self.output.append)
        self.assertTrue(saved)
        private.assert_called_once()
        ordinary.assert_called_once()
        self.assertIn("[y/N]", ordinary.call_args.args[0])
        self.assert_no_secrets()

    def test_lock_is_held_before_hidden_input_confirmation_and_credential_save(self):
        held_at = []

        def probe(label):
            with self.assertRaises(RuntimeError):
                InstanceLock(self.config.state_dir)
            held_at.append(label)

        def private(prompt):
            probe("private")
            return NEW_KEY

        def confirm(prompt):
            probe("confirm")
            return "y"

        def save(path, key):
            probe("save")
            self.fake_save(path, key)

        self.saver.side_effect = save
        saved, _, _ = self.configure(input_fn=confirm, secret_fn=private)
        self.assertTrue(saved)
        self.assertEqual(held_at, ["private", "confirm", "save"])
        self.assert_released()

    def test_running_instance_blocks_setup_before_any_key_or_confirmation_prompt(self):
        self.previous()
        lease = InstanceLock(self.config.state_dir)
        try:
            saved, ordinary, private = self.configure()
        finally:
            lease.close()
        self.assertFalse(saved)
        ordinary.assert_not_called()
        private.assert_not_called()
        self.loader.assert_not_called()
        self.saver.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_released()

    def test_empty_hidden_input_keeps_saved_key_without_exposing_it(self):
        self.previous()
        saved, _, private = self.configure(keys=[""])
        self.assertTrue(saved)
        private.assert_called_once()
        self.saver.assert_called_once_with(self.path, OLD_KEY)
        self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_no_secrets()

    def test_missing_key_and_invalid_hidden_input_reprompt_without_echo(self):
        invalid = NEW_KEY + "\n"
        saved, ordinary, private = self.configure(keys=["", invalid, NEW_KEY])
        self.assertTrue(saved)
        self.assertEqual(private.call_count, 3)
        ordinary.assert_called_once()
        self.assertNotIn(invalid, "\n".join(self.output))
        self.assert_no_secrets()

    def test_only_explicit_yes_saves_and_default_decline_preserves_previous_key(self):
        for answer in ("", "n", "no", "anything", "true"):
            with self.subTest(answer=answer):
                self.previous()
                saved, _, _ = self.configure(answers=[answer])
                self.assertFalse(saved)
                self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
                self.assertEqual(self.path.read_bytes(), self.original)
                self.assert_released()
        self.saver.assert_not_called()

    def test_secret_input_eof_and_keyboard_interrupt_preserve_previous_key(self):
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption.__name__):
                self.previous()
                saved, ordinary, _ = self.configure(secret_fn=Mock(side_effect=interruption))
                self.assertFalse(saved)
                ordinary.assert_not_called()
                self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
                self.assert_released()
        self.saver.assert_not_called()

    def test_confirmation_eof_and_keyboard_interrupt_preserve_previous_key(self):
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption.__name__):
                self.previous()
                saved, _, _ = self.configure(input_fn=Mock(side_effect=interruption))
                self.assertFalse(saved)
                self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
                self.assert_released()
        self.saver.assert_not_called()

    def test_cancel_with_no_old_vault_never_creates_one(self):
        saved, _, _ = self.configure(answers=["n"])
        self.assertFalse(saved)
        self.assertFalse(self.vault.exists())
        self.assertEqual(self.path.read_bytes(), self.original)
        self.saver.assert_not_called()

    def test_corrupt_saved_key_can_be_replaced_without_printing_ciphertext_or_key(self):
        corrupt = b"corrupt-opaque-ciphertext-private-diagnostic"
        self.vault.write_bytes(corrupt)
        saved, _, _ = self.configure()
        self.assertTrue(saved)
        self.assertEqual(self.vault.read_bytes(), NEW_CIPHER)
        self.assertNotIn(corrupt.decode(), "\n".join(self.output))
        self.assert_no_secrets()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_corrupt_old_key_is_preserved_when_replacement_is_cancelled(self):
        corrupt = b"corrupt-opaque-ciphertext"
        self.vault.write_bytes(corrupt)
        saved, _, _ = self.configure(answers=["n"])
        self.assertFalse(saved)
        self.assertEqual(self.vault.read_bytes(), corrupt)
        self.saver.assert_not_called()

    def test_environment_override_is_named_but_not_exposed_or_saved_as_local_key(self):
        os.environ["OPENAI_API_KEY"] = ENV_KEY
        saved, _, _ = self.configure()
        self.assertTrue(saved)
        visible = "\n".join(self.output)
        self.assertIn("OPENAI_API_KEY", visible)
        self.assertIn("priority", visible.lower())
        self.saver.assert_called_once_with(self.path, NEW_KEY)
        self.assertEqual(os.environ["OPENAI_API_KEY"], ENV_KEY)
        self.assert_no_secrets()

    def test_environment_key_does_not_substitute_for_blank_saved_key(self):
        self.previous()
        os.environ["OPENAI_API_KEY"] = ENV_KEY
        saved, _, _ = self.configure(keys=[""])
        self.assertTrue(saved)
        self.saver.assert_called_once_with(self.path, OLD_KEY)
        self.assert_no_secrets()

    def test_config_changed_during_hidden_input_is_rejected_before_confirmation(self):
        self.previous()
        external = self.original + b"\n# external configuration change\n"

        def private(prompt):
            self.path.write_bytes(external)
            return NEW_KEY

        saved, ordinary, _ = self.configure(secret_fn=private)
        self.assertFalse(saved)
        ordinary.assert_not_called()
        self.saver.assert_not_called()
        self.assertEqual(self.path.read_bytes(), external)
        self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
        self.assert_released()

    def test_vault_changed_during_hidden_input_is_rejected_without_rollback_overwrite(self):
        self.previous()

        def private(prompt):
            self.vault.write_bytes(EXTERNAL_CIPHER)
            return NEW_KEY

        saved, ordinary, _ = self.configure(secret_fn=private)
        self.assertFalse(saved)
        ordinary.assert_not_called()
        self.saver.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), EXTERNAL_CIPHER)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_released()

    def test_vault_created_during_hidden_input_is_not_overwritten(self):
        def private(prompt):
            self.vault.write_bytes(EXTERNAL_CIPHER)
            return NEW_KEY

        saved, ordinary, _ = self.configure(secret_fn=private)
        self.assertFalse(saved)
        ordinary.assert_not_called()
        self.saver.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), EXTERNAL_CIPHER)

    def test_config_changed_during_confirmation_is_rejected_before_save(self):
        self.previous()
        external = self.original + b"\n# changed while reviewing\n"

        def confirm(prompt):
            self.path.write_bytes(external)
            return "y"

        saved, _, _ = self.configure(input_fn=confirm)
        self.assertFalse(saved)
        self.saver.assert_not_called()
        self.assertEqual(self.path.read_bytes(), external)
        self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)

    def test_vault_changed_during_confirmation_is_not_overwritten(self):
        self.previous()

        def confirm(prompt):
            self.vault.write_bytes(EXTERNAL_CIPHER)
            return "y"

        saved, _, _ = self.configure(input_fn=confirm)
        self.assertFalse(saved)
        self.saver.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), EXTERNAL_CIPHER)

    def test_vault_deleted_during_confirmation_is_not_recreated(self):
        self.previous()

        def confirm(prompt):
            self.vault.unlink()
            return "y"

        saved, _, _ = self.configure(input_fn=confirm)
        self.assertFalse(saved)
        self.saver.assert_not_called()
        self.assertFalse(self.vault.exists())

    def test_successful_save_stays_true_when_all_followup_outputs_are_interrupted(self):
        for interruption in (EOFError, KeyboardInterrupt, OSError):
            with self.subTest(interruption=interruption.__name__):
                self.previous()
                self.saver.reset_mock()

                def output(text):
                    self.output.append(text)
                    if self.saver.called:
                        raise interruption()

                saved, _, _ = self.configure(output=output)
                self.assertTrue(saved)
                self.assertEqual(self.vault.read_bytes(), NEW_CIPHER)
                self.saver.assert_called_once_with(self.path, NEW_KEY)
                self.assert_released()
        self.assert_no_secrets()

    def test_interrupted_pre_save_output_cancels_safely_even_when_receipts_also_fail(self):
        for interruption in (EOFError, KeyboardInterrupt, OSError):
            with self.subTest(interruption=interruption.__name__):
                self.previous()
                saved, ordinary, private = self.configure(output=Mock(side_effect=interruption))
                self.assertFalse(saved)
                ordinary.assert_not_called()
                private.assert_not_called()
                self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
                self.assert_released()
        self.saver.assert_not_called()

    def test_save_errors_are_fixed_and_keep_previous_key_and_public_configuration(self):
        for error in (CredentialError("unsupported_platform"),
                      OSError("private file failure " + NEW_KEY + " " + OLD_KEY)):
            with self.subTest(error=type(error).__name__):
                self.previous()
                self.saver.side_effect = error
                saved, _, _ = self.configure()
                self.assertFalse(saved)
                self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
                self.assertEqual(self.path.read_bytes(), self.original)
                self.assert_no_secrets()
                self.assert_released()

    def test_interrupted_save_after_replace_restores_previous_ciphertext(self):
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption.__name__):
                self.previous()

                def interrupted(path, key):
                    self.fake_save(path, key)
                    raise interruption()

                self.saver.side_effect = interrupted
                saved, _, _ = self.configure()
                self.assertFalse(saved)
                self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
                self.assertEqual(self.path.read_bytes(), self.original)
                self.assert_released()

    def test_interrupted_save_without_old_vault_removes_new_partial_vault(self):
        def interrupted(path, key):
            self.fake_save(path, key)
            raise KeyboardInterrupt()

        self.saver.side_effect = interrupted
        saved, _, _ = self.configure()
        self.assertFalse(saved)
        self.assertFalse(self.vault.exists())
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_released()

    def test_generic_private_input_error_does_not_escape_or_print_secret_exception(self):
        self.previous()
        error = OSError("private terminal failure " + NEW_KEY)
        saved, ordinary, _ = self.configure(secret_fn=Mock(side_effect=error))
        self.assertFalse(saved)
        ordinary.assert_not_called()
        self.saver.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
        self.assertNotIn("private terminal failure", "\n".join(self.output))
        self.assert_no_secrets()
        self.assert_released()

    def test_unsafe_directory_vault_is_rejected_before_hidden_input(self):
        self.vault.mkdir()
        saved, ordinary, private = self.configure()
        self.assertFalse(saved)
        ordinary.assert_not_called()
        private.assert_not_called()
        self.loader.assert_not_called()
        self.saver.assert_not_called()
        self.assertTrue(self.vault.is_dir())
        self.assert_released()

    def test_oversized_ciphertext_is_rejected_before_hidden_input(self):
        self.vault.write_bytes(b"x" * (70 * 1024 + 1))
        saved, ordinary, private = self.configure()
        self.assertFalse(saved)
        ordinary.assert_not_called()
        private.assert_not_called()
        self.loader.assert_not_called()
        self.saver.assert_not_called()
        self.assertEqual(self.vault.stat().st_size, 70 * 1024 + 1)
        self.assert_released()

    def test_vault_snapshot_rejects_opened_descriptor_that_does_not_match_target(self):
        self.previous()
        other = self.path.parent / "other-synthetic-ciphertext"
        other.write_bytes(EXTERNAL_CIPHER)
        original_open = os.open

        def substituted(path, flags, *args, **kwargs):
            return original_open(other if Path(path) == self.vault else path, flags, *args, **kwargs)

        with patch.object(api_key_wizard.os, "open", side_effect=substituted):
            saved, ordinary, private = self.configure()
        self.assertFalse(saved)
        ordinary.assert_not_called()
        private.assert_not_called()
        self.saver.assert_not_called()
        self.assertEqual(self.vault.read_bytes(), OLD_CIPHER)
        self.assertEqual(other.read_bytes(), EXTERNAL_CIPHER)
        self.assert_released()

    def test_snapshot_fd_is_closed_if_fdopen_fails_with_private_diagnostic(self):
        self.previous()
        opened = []
        original_open = os.open

        def capture(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if Path(path) == self.vault:
                opened.append(descriptor)
            return descriptor

        with patch.object(api_key_wizard.os, "open", side_effect=capture), \
                patch.object(api_key_wizard.os, "fdopen", side_effect=OSError("private fdopen " + OLD_KEY)):
            saved, _, private = self.configure()
        self.assertFalse(saved)
        private.assert_not_called()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assert_no_secrets()
        self.assert_released()

    def test_lock_cleanup_error_cannot_undo_successful_save_or_expose_private_diagnostic(self):
        class FailingCleanup:
            def __init__(inner, state_dir):
                inner.lease = InstanceLock(state_dir)

            def close(inner):
                inner.lease.close()
                raise OSError("private lock cleanup " + NEW_KEY)

        with patch.object(api_key_wizard, "InstanceLock", FailingCleanup):
            saved, _, _ = self.configure()
        self.assertTrue(saved)
        self.assertEqual(self.vault.read_bytes(), NEW_CIPHER)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertNotIn("private lock cleanup", "\n".join(self.output))
        self.assert_no_secrets()
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
