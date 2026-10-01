"""Startup lease tests using synthetic config and real temporary file locks."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import api_key_wizard, console, discord_wizard
from indeces.config import load_config
from indeces.credentials import CredentialError, openai_secret_path, secret_path
from indeces.lock import InstanceLock


GUILD = "123456789012345678"
CHANGED_GUILD = "223456789012345678"
TOKEN = "start-lock-synthetic-bot-token-offline-only"
KEY = "start-lock-synthetic-openai-key-offline-only"
CHANGED_MESSAGE = "configuration changed during startup; start again"


class ConsoleStartLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "config.local.toml"
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        self.path.write_bytes(discord_wizard.patch_config(example.read_bytes(), GUILD, ()))
        self.config = load_config(self.path)

    def assert_held(self):
        with self.assertRaisesRegex(RuntimeError, "another Indeces instance"):
            InstanceLock(self.config.state_dir)

    def assert_released(self):
        lease = InstanceLock(self.config.state_dir)
        lease.close()
        self.assertFalse((self.config.state_dir / "memory.sqlite3").exists())
        self.assertFalse(self.config.scratch_dir.exists())

    def change_config(self):
        self.path.write_bytes(discord_wizard.patch_config(self.path.read_bytes(), CHANGED_GUILD, ()))

    def test_hidden_credential_wait_blocks_wizards_and_second_start_before_credentials(self):
        original = self.path.read_bytes()
        stages = []

        def prompt(message):
            self.assert_held()
            stages.append(message)
            for configure, module, loader_name in (
                    (discord_wizard.configure_discord, discord_wizard, "load_discord_token"),
                    (api_key_wizard.configure_api_key, api_key_wizard, "load_openai_key")):
                public_input, hidden_input, output = Mock(), Mock(), Mock()
                with patch.object(module, loader_name) as loader, \
                        patch.object(module, "_vault_snapshot") as vault:
                    self.assertFalse(configure(self.path, input_fn=public_input,
                                               secret_fn=hidden_input, output=output))
                public_input.assert_not_called()
                hidden_input.assert_not_called()
                loader.assert_not_called()
                vault.assert_not_called()
                self.assertIn("stop the running instance", output.call_args.args[0])
            with patch.object(console, "load_discord_token") as token_read, \
                    patch.object(console, "load_openai_key") as key_read, \
                    patch.object(console, "prompt_secret") as nested_prompt:
                with self.assertRaisesRegex(RuntimeError, "another Indeces instance"):
                    console.run(self.config, self.path)
                token_read.assert_not_called()
                key_read.assert_not_called()
                nested_prompt.assert_not_called()
            return TOKEN if "Discord" in message else KEY

        with patch.dict(os.environ, {}, clear=True), \
                patch.object(console, "load_discord_token", return_value=None), \
                patch.object(console, "load_openai_key", return_value=None), \
                patch.object(console, "prompt_secret", side_effect=prompt), \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config, self.path)
        self.assertEqual(len(stages), 2)
        serve.assert_awaited_once()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(secret_path(self.path).exists())
        self.assertFalse(openai_secret_path(self.path).exists())
        self.assert_released()

    def test_already_owned_state_rejects_start_without_any_credential_read(self):
        lease = InstanceLock(self.config.state_dir)
        try:
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(console, "load_discord_token") as token_read, \
                    patch.object(console, "load_openai_key") as key_read, \
                    patch.object(console, "prompt_secret") as prompt, \
                    patch.object(console, "serve", new_callable=AsyncMock) as serve:
                with self.assertRaisesRegex(RuntimeError, "another Indeces instance"):
                    console.run(self.config, self.path)
            token_read.assert_not_called()
            key_read.assert_not_called()
            prompt.assert_not_called()
            serve.assert_not_awaited()
            self.assert_held()
        finally:
            lease.close()
        self.assert_released()

    def test_cancelled_token_or_key_input_releases_startup_lease_without_serving(self):
        for interruption in (EOFError(), KeyboardInterrupt()):
            for stage in ("token", "key"):
                with self.subTest(interruption=type(interruption).__name__, stage=stage), \
                        patch.dict(os.environ, {}, clear=True), \
                        patch.object(console, "load_discord_token", return_value=None if stage == "token" else TOKEN), \
                        patch.object(console, "load_openai_key", return_value=None), \
                        patch.object(console, "prompt_secret", side_effect=interruption), \
                        patch.object(console, "serve", new_callable=AsyncMock) as serve, \
                        redirect_stdout(io.StringIO()) as output:
                    console.run(self.config, self.path)
                serve.assert_not_awaited()
                self.assertIn("Startup cancelled; no connection was made.", output.getvalue())
                self.assert_released()

    def test_token_or_key_vault_error_releases_startup_lease_without_serving(self):
        for stage in ("token", "key"):
            error = CredentialError("credential_corrupt")
            with self.subTest(stage=stage), patch.dict(os.environ, {}, clear=True), \
                    patch.object(console, "load_discord_token", return_value=TOKEN,
                                 side_effect=error if stage == "token" else None), \
                    patch.object(console, "load_openai_key", side_effect=error if stage == "key" else None), \
                    patch.object(console, "prompt_secret") as prompt, \
                    patch.object(console, "serve", new_callable=AsyncMock) as serve:
                with self.assertRaises(CredentialError) as caught:
                    console.run(self.config, self.path)
            self.assertIs(caught.exception, error)
            prompt.assert_not_called()
            serve.assert_not_awaited()
            self.assert_released()

    def test_config_changed_before_lock_validation_rejects_before_credentials_and_releases(self):
        self.change_config()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(console, "load_discord_token") as token_read, \
                patch.object(console, "load_openai_key") as key_read, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            with self.assertRaises(RuntimeError) as caught:
                console.run(self.config, self.path)
        self.assertEqual(str(caught.exception), CHANGED_MESSAGE)
        token_read.assert_not_called()
        key_read.assert_not_called()
        prompt.assert_not_called()
        serve.assert_not_awaited()
        self.assert_released()

    def test_config_changed_during_key_prompt_rejects_before_serving_and_releases(self):
        def prompt(message):
            self.assert_held()
            self.assertIn("OpenAI", message)
            self.change_config()
            return KEY

        with patch.dict(os.environ, {}, clear=True), \
                patch.object(console, "load_discord_token", return_value=TOKEN), \
                patch.object(console, "load_openai_key", return_value=None), \
                patch.object(console, "prompt_secret", side_effect=prompt), \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            with self.assertRaises(RuntimeError) as caught:
                console.run(self.config, self.path)
        self.assertEqual(str(caught.exception), CHANGED_MESSAGE)
        serve.assert_not_awaited()
        self.assert_released()

    def test_async_service_receives_open_lease_and_remains_protected_across_await(self):
        async def service(config, key, token, *, lease):
            self.assertEqual((config, key, token), (self.config, KEY, TOKEN))
            self.assertFalse(lease.stream.closed)
            self.assert_held()
            await asyncio.sleep(0)
            self.assert_held()
            self.assertFalse(lease.stream.closed)

        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": TOKEN, "OPENAI_API_KEY": KEY}, clear=True), \
                patch.object(console, "serve", new_callable=AsyncMock, side_effect=service) as serve:
            console.run(self.config, self.path)
        serve.assert_awaited_once()
        self.assertTrue(serve.call_args.kwargs["lease"].stream.closed)
        self.assert_released()

    def test_async_service_failure_releases_run_owned_lease(self):
        error = RuntimeError("synthetic service failure")
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": TOKEN, "OPENAI_API_KEY": KEY}, clear=True), \
                patch.object(console, "serve", new_callable=AsyncMock, side_effect=error) as serve:
            with self.assertRaises(RuntimeError) as caught:
                console.run(self.config, self.path)
        self.assertIs(caught.exception, error)
        self.assertTrue(serve.call_args.kwargs["lease"].stream.closed)
        self.assert_released()

    def test_serve_early_failure_does_not_close_borrowed_real_lease(self):
        lease = InstanceLock(self.config.state_dir)
        error = OSError("synthetic scratch startup failure")
        try:
            with patch.object(console, "ScratchLog", side_effect=error), \
                    redirect_stdout(io.StringIO()), self.assertRaises(OSError) as caught:
                asyncio.run(console.serve(self.config, KEY, TOKEN, lease=lease))
            self.assertIs(caught.exception, error)
            self.assertFalse(lease.stream.closed)
            self.assert_held()
        finally:
            lease.close()
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
