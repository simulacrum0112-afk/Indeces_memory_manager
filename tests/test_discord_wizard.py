from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from indeces import credentials, discord_wizard
from indeces.config import load_config, snowflake
from indeces.credentials import CredentialError, load_discord_token, save_discord_token, secret_path
from indeces.lock import InstanceLock


GUILD = "123456789012345678"
OTHER_GUILD = "223456789012345678"
CHANNEL = "323456789012345678"
OTHER_CHANNEL = "423456789012345678"
TOKEN = "wizard-synthetic-bot-token-offline-only"
OLD_TOKEN = "wizard-previous-bot-token-offline-only"


def fake_protect(data):
    # A portable fixture; production only calls Windows DPAPI.
    return b"wizard-test:" + bytes(value ^ 0xA5 for value in data)


def fake_unprotect(data):
    if not data.startswith(b"wizard-test:"):
        raise RuntimeError("invalid test ciphertext")
    return bytes(value ^ 0xA5 for value in data.removeprefix(b"wizard-test:"))


class DiscordWizardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.local.toml"
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        self.original = example.read_bytes().replace(b'name = "Indices"', b'name = "Indeces"')
        self.path.write_bytes(self.original)
        self.addCleanup(patch.stopall)
        self.protect = patch.object(credentials, "_protect", side_effect=fake_protect).start()
        patch.object(credentials, "_unprotect", side_effect=fake_unprotect).start()
        self.output = []

    def configure(self, answers=None, tokens=None, **kwargs):
        input_fn = kwargs.pop("input_fn", Mock(side_effect=answers or [GUILD, "", "y"]))
        secret_fn = kwargs.pop("secret_fn", Mock(side_effect=tokens or [TOKEN]))
        output = kwargs.pop("output", self.output.append)
        result = discord_wizard.configure_discord(
            self.path, input_fn=input_fn, secret_fn=secret_fn, output=output, **kwargs)
        return result, input_fn, secret_fn

    def previous_credential(self, channels=()):
        self.original = discord_wizard.patch_config(self.original, GUILD, channels)
        self.path.write_bytes(self.original)
        save_discord_token(self.path, GUILD, OLD_TOKEN)
        return secret_path(self.path).read_bytes()

    def assert_released(self):
        lock = InstanceLock(load_config(self.path).state_dir)
        lock.close()

    def test_save_updates_only_identity_and_scope_without_exposing_token(self):
        before = tomllib.loads(self.original.decode())
        saved, _, _ = self.configure(answers=[GUILD, f"{CHANNEL}, {OTHER_CHANNEL} {CHANNEL}", "yes"])
        self.assertTrue(saved)
        after = tomllib.loads(self.path.read_text(encoding="utf-8"))
        expected = deepcopy(before)
        expected["name"] = "Indices"
        expected["discord"]["guild_id"] = GUILD
        expected["discord"]["channel_ids"] = [CHANNEL, OTHER_CHANNEL]
        self.assertEqual(after, expected)
        self.assertEqual(load_discord_token(self.path, GUILD), TOKEN)
        self.assertNotIn(TOKEN, "\n".join(self.output))
        self.assertNotIn(TOKEN.encode(), self.path.read_bytes())
        self.assertNotIn(TOKEN.encode(), secret_path(self.path).read_bytes())
        config = load_config(self.path)
        self.assertFalse(config.scratch_dir.exists())
        self.assertFalse(config.knowledge_dir.exists())
        self.assert_released()

    def test_blank_fields_keep_current_scope_and_hidden_saved_token(self):
        self.previous_credential(channels=(CHANNEL,))
        saved, _, _ = self.configure(answers=["", "", "y"], tokens=[""])
        self.assertTrue(saved)
        config = load_config(self.path)
        self.assertEqual(config.discord.guild_id, GUILD)
        self.assertEqual(config.discord.channel_ids, (CHANNEL,))
        self.assertEqual(load_discord_token(self.path, GUILD), OLD_TOKEN)
        self.assertNotIn(OLD_TOKEN, "\n".join(self.output))

    def test_old_guild_token_can_be_rebound_to_reviewed_new_guild(self):
        self.previous_credential(channels=(CHANNEL,))
        saved, _, _ = self.configure(answers=[OTHER_GUILD, "*", "y"], tokens=[""])
        self.assertTrue(saved)
        config = load_config(self.path)
        self.assertEqual(config.discord.guild_id, OTHER_GUILD)
        self.assertEqual(config.discord.channel_ids, ())
        self.assertEqual(load_discord_token(self.path, OTHER_GUILD), OLD_TOKEN)
        with self.assertRaises(CredentialError):
            load_discord_token(self.path, GUILD)

    def test_invalid_ids_and_tokens_reprompt_without_echoing_inputs(self):
        saved, inputs, secrets = self.configure(
            answers=["not-a-guild", "0", "²", str(2**64), "0" + GUILD,
                     "invalid-channel", "0" + CHANNEL, "y"],
            tokens=["", "bad-token\n", TOKEN])
        self.assertTrue(saved)
        self.assertEqual(inputs.call_count, 8)
        self.assertEqual(secrets.call_count, 3)
        config = load_config(self.path)
        self.assertEqual(config.discord.guild_id, GUILD)
        self.assertEqual(config.discord.channel_ids, (CHANNEL,))
        self.assertNotIn("bad-token", "\n".join(self.output))

    def test_decline_preserves_configuration_and_previous_ciphertext(self):
        cipher = self.previous_credential()
        saved, _, _ = self.configure(answers=[OTHER_GUILD, CHANNEL, "n"])
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(secret_path(self.path).read_bytes(), cipher)
        self.assert_released()

    def test_cancellation_before_save_never_creates_a_vault(self):
        for interruption in (EOFError(), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__):
                saved, _, _ = self.configure(input_fn=Mock(side_effect=interruption))
                self.assertFalse(saved)
                self.assertEqual(self.path.read_bytes(), self.original)
                self.assertFalse(secret_path(self.path).exists())
                self.assert_released()

    def test_cancellation_during_hidden_input_preserves_files(self):
        cipher = self.previous_credential()
        saved, _, _ = self.configure(answers=[OTHER_GUILD, "*"], secret_fn=Mock(side_effect=KeyboardInterrupt()))
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(secret_path(self.path).read_bytes(), cipher)

    def test_running_instance_blocks_setup_before_any_prompt(self):
        lock = InstanceLock(load_config(self.path).state_dir)
        try:
            inputs = Mock(side_effect=AssertionError("must not prompt"))
            secrets = Mock(side_effect=AssertionError("must not prompt"))
            saved, _, _ = self.configure(input_fn=inputs, secret_fn=secrets)
        finally:
            lock.close()
        self.assertFalse(saved)
        inputs.assert_not_called()
        secrets.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_credential_failure_does_not_change_public_configuration(self):
        with patch.object(discord_wizard, "save_discord_token", side_effect=CredentialError("unsupported_platform")):
            saved, _, _ = self.configure()
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(secret_path(self.path).exists())
        self.assertNotIn(TOKEN, "\n".join(self.output))

    def test_config_write_failure_restores_previous_ciphertext(self):
        cipher = self.previous_credential()
        write = discord_wizard._atomic_write

        def fail_config(path, contents):
            if path == self.path:
                raise OSError("synthetic failure with " + TOKEN)
            return write(path, contents)

        with patch.object(discord_wizard, "_atomic_write", side_effect=fail_config):
            saved, _, _ = self.configure(answers=[OTHER_GUILD, "", "y"])
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(secret_path(self.path).read_bytes(), cipher)
        self.assertEqual(load_discord_token(self.path, GUILD), OLD_TOKEN)
        self.assertNotIn(TOKEN, "\n".join(self.output))
        self.assert_released()

    def test_config_write_failure_removes_new_vault_when_none_preexisted(self):
        with patch.object(discord_wizard, "_atomic_write", side_effect=OSError("synthetic failure")):
            saved, _, _ = self.configure()
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(secret_path(self.path).exists())

    def test_interrupt_during_config_write_restores_previous_ciphertext(self):
        cipher = self.previous_credential()
        write = discord_wizard._atomic_write

        def interrupt_config(path, contents):
            if path == self.path:
                raise KeyboardInterrupt()
            return write(path, contents)

        with patch.object(discord_wizard, "_atomic_write", side_effect=interrupt_config):
            saved, _, _ = self.configure(answers=[OTHER_GUILD, "", "y"])
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(secret_path(self.path).read_bytes(), cipher)
        self.assert_released()

    def test_interrupt_after_credential_replace_restores_previous_ciphertext(self):
        cipher = self.previous_credential()
        save = discord_wizard.save_discord_token

        def save_then_interrupt(*args):
            save(*args)
            raise KeyboardInterrupt()

        with patch.object(discord_wizard, "save_discord_token", side_effect=save_then_interrupt):
            saved, _, _ = self.configure(answers=[OTHER_GUILD, "", "y"])
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(secret_path(self.path).read_bytes(), cipher)
        self.assert_released()

    def test_interrupt_after_config_replace_restores_both_files(self):
        cipher = self.previous_credential()
        write = discord_wizard._atomic_write
        interrupted = False

        def write_then_interrupt(path, contents):
            nonlocal interrupted
            write(path, contents)
            if path == self.path and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(discord_wizard, "_atomic_write", side_effect=write_then_interrupt):
            saved, _, _ = self.configure(answers=[OTHER_GUILD, "", "y"])
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(secret_path(self.path).read_bytes(), cipher)

    def test_cancellation_after_commit_reports_already_saved_result(self):
        def output_interrupt_after_saved(message):
            self.output.append(message)
            if message.startswith("Saved."):
                raise KeyboardInterrupt()

        saved, _, _ = self.configure(output=output_interrupt_after_saved)
        self.assertTrue(saved)
        self.assertEqual(load_config(self.path).discord.guild_id, GUILD)
        self.assertEqual(load_discord_token(self.path, GUILD), TOKEN)
        self.assertTrue(any("saved before cancellation" in message for message in self.output))
        self.assert_released()

    def test_rollback_failure_reports_incomplete_recovery_without_claiming_unchanged(self):
        self.previous_credential()
        with patch.object(discord_wizard, "_atomic_write", side_effect=OSError("synthetic failure")):
            saved, _, _ = self.configure(answers=[OTHER_GUILD, "", "y"])
        self.assertFalse(saved)
        self.assertIn("Rollback incomplete", "\n".join(self.output))
        self.assertNotIn("unchanged", "\n".join(self.output))
        self.assertNotIn(TOKEN, "\n".join(self.output))
        self.assert_released()

    def test_corrupt_credential_can_be_replaced_after_warning(self):
        self.original = discord_wizard.patch_config(self.original, GUILD, ())
        self.path.write_bytes(self.original)
        secret_path(self.path).write_bytes(b"corrupt credential fixture")
        saved, _, secrets = self.configure(answers=["", "", "y"], tokens=["", TOKEN])
        self.assertTrue(saved)
        self.assertEqual(secrets.call_count, 2)
        self.assertIn("Saved token unavailable: credential_corrupt", "\n".join(self.output))
        self.assertEqual(load_discord_token(self.path, GUILD), TOKEN)

    def test_corrupt_credential_is_preserved_when_repair_is_declined(self):
        self.original = discord_wizard.patch_config(self.original, GUILD, ())
        self.path.write_bytes(self.original)
        cipher = b"corrupt credential fixture"
        secret_path(self.path).write_bytes(cipher)
        saved, _, _ = self.configure(answers=["", "", "n"])
        self.assertFalse(saved)
        self.assertEqual(secret_path(self.path).read_bytes(), cipher)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_scope_mismatched_credential_can_be_repaired(self):
        self.previous_credential()
        self.path.write_bytes(discord_wizard.patch_config(self.original, OTHER_GUILD, ()))
        saved, _, _ = self.configure(answers=["", "", "y"])
        self.assertTrue(saved)
        self.assertIn("credential_scope_mismatch", "\n".join(self.output))
        self.assertEqual(load_discord_token(self.path, OTHER_GUILD), TOKEN)

    def test_external_config_edit_is_preserved_and_blocks_save(self):
        edited = self.original + b"\n# External user edit\n"
        answers = iter([GUILD, "", "y"])

        def input_edit_on_save(prompt):
            answer = next(answers)
            if "Save" in prompt:
                self.path.write_bytes(edited)
            return answer

        saved, _, _ = self.configure(input_fn=input_edit_on_save)
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), edited)
        self.assertFalse(secret_path(self.path).exists())

    def test_external_vault_edit_is_preserved_and_blocks_save(self):
        self.previous_credential()
        foreign_cipher = b"external-ciphertext-edit"
        answers = iter([OTHER_GUILD, "", "y"])

        def input_edit_on_save(prompt):
            answer = next(answers)
            if "Save" in prompt:
                secret_path(self.path).write_bytes(foreign_cipher)
            return answer

        saved, _, _ = self.configure(input_fn=input_edit_on_save)
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(secret_path(self.path).read_bytes(), foreign_cipher)

    def test_unsupported_inline_table_fails_before_persistence(self):
        start = self.original.index(b"[discord]")
        end = self.original.index(b"[runtime]")
        self.original = (self.original[:start] +
                         b'discord = {guild_id = "", channel_ids = [], queue_capacity = 16, delivery_seconds = 10.0}\n\n' +
                         self.original[end:])
        self.path.write_bytes(self.original)
        saved, _, _ = self.configure()
        self.assertFalse(saved)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(secret_path(self.path).exists())


class ConfigPatchTests(unittest.TestCase):
    def test_scope_edits_preserve_comments_crlf_and_other_bytes(self):
        original = (b'# Header\r\nname = "Indeces" # identity\r\nstate_dir = "state"\r\n\r\n'
                    b'[discord] # transport\r\nguild_id = "" # server\r\n'
                    b'channel_ids = [] # selection\r\nqueue_capacity = 19\r\n\r\n'
                    b'[adapter]\r\nmodel = "gpt-6-luna"\r\n')
        updated = discord_wizard.patch_config(original, GUILD, (CHANNEL,))
        expected = (original.replace(b'name = "Indeces"', b'name = "Indices"')
                    .replace(b'guild_id = ""', f'guild_id = "{GUILD}"'.encode())
                    .replace(b'channel_ids = []', f'channel_ids = ["{CHANNEL}"]'.encode()))
        self.assertEqual(updated, expected)

    def test_quoted_table_keys_and_multiline_array(self):
        original = (b'name = "old"\n["discord"] # quoted table\n'
                    b'"guild_id" = \'123\' # guild comment\n'
                    b"'channel_ids' = [\n  '456',\n] # channels comment\n"
                    b'[other]\nvalue = "guild_id = \\\"stay\\\""\n')
        updated = discord_wizard.patch_config(original, GUILD, (CHANNEL, OTHER_CHANNEL))
        raw = tomllib.loads(updated.decode())
        self.assertEqual(raw["discord"]["guild_id"], GUILD)
        self.assertEqual(raw["discord"]["channel_ids"], [CHANNEL, OTHER_CHANNEL])
        self.assertIn(b' # guild comment\n', updated)
        self.assertIn(b' # channels comment\n', updated)
        self.assertTrue(updated.endswith(original[original.index(b"[other]"):]))

    def test_table_like_text_inside_unrelated_multiline_string_is_unchanged(self):
        original = (b'name = "old"\nnotes = """\n[discord]\nguild_id = "fake"\n"""\n'
                    b'[discord]\nguild_id = "123"\nchannel_ids = []\n')
        updated = discord_wizard.patch_config(original, GUILD, ())
        self.assertEqual(tomllib.loads(original.decode())["notes"], tomllib.loads(updated.decode())["notes"])

    def test_missing_name_and_channels_are_added_in_correct_tables(self):
        original = b'state_dir = "state"\n[discord]\nguild_id = "123"\n[adapter]\nmodel = "gpt-6-luna"'
        updated = discord_wizard.patch_config(original, GUILD, (CHANNEL,))
        self.assertEqual(tomllib.loads(updated.decode()), {
            "name": "Indices", "state_dir": "state",
            "discord": {"guild_id": GUILD, "channel_ids": [CHANNEL]},
            "adapter": {"model": "gpt-6-luna"}})

    def test_snowflakes_normalize_ascii_and_reject_invalid_values(self):
        self.assertEqual(snowflake("0" + GUILD), GUILD)
        self.assertEqual(snowflake(str(2**64 - 1)), str(2**64 - 1))
        for value in ("", "0", "-1", "²", "１２３", "1e18", str(2**64), None, 123):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    snowflake(value)

    def test_atomic_write_fdopen_failure_closes_descriptor_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "configuration.toml"
            path.write_bytes(b"original contents")
            with patch.object(discord_wizard.os, "fdopen", side_effect=OSError("synthetic fdopen failure")) as fdopen:
                with self.assertRaises(OSError):
                    discord_wizard._atomic_write(path, b"replacement contents")
            descriptor = fdopen.call_args.args[0]
            with self.assertRaises(OSError):
                os.fstat(descriptor)
            self.assertEqual(path.read_bytes(), b"original contents")
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
