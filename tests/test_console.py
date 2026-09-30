from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from indeces import console, discord_wizard
from indeces.config import load_config
from indeces.credentials import CredentialError, secret_path


GUILD = "123456789012345678"
TOKEN = "console-synthetic-bot-token-offline-only"
KEY = "synthetic-openai-key-offline-only"


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.local.toml"
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        self.path.write_bytes(discord_wizard.patch_config(example.read_bytes(), GUILD, ()))
        self.config = load_config(self.path)

    def main(self, command="console"):
        with patch("sys.argv", ["indeces", command, "--config", str(self.path)]), redirect_stdout(io.StringIO()) as output:
            console.main()
        return output.getvalue()

    def test_cli_discord_routes_to_wizard_without_starting_service_or_model(self):
        with patch.object(console, "configure_discord", return_value=True) as wizard, \
                patch.object(console, "run") as run, patch.object(console, "serve") as serve, \
                patch.object(console, "load_config") as loader:
            self.main("discord")
        wizard.assert_called_once_with(self.path)
        run.assert_not_called()
        serve.assert_not_called()
        loader.assert_not_called()

    def test_cli_cancelled_discord_setup_returns_failure_code(self):
        with patch.object(console, "configure_discord", return_value=False), \
                self.assertRaises(SystemExit) as error:
            self.main("discord")
        self.assertEqual(error.exception.code, 2)

    def test_interactive_discord_returns_to_console_without_starting_service(self):
        with patch("builtins.input", side_effect=["discord", "quit"]), \
                patch.object(console, "configure_discord", return_value=True) as wizard, \
                patch.object(console, "run") as run:
            output = self.main()
        wizard.assert_called_once_with(self.path)
        run.assert_not_called()
        self.assertIn("discord | start | status", output)

    def test_console_reloads_configuration_after_wizard_before_start(self):
        old_config = self.config

        def save_fixture(path):
            updated = discord_wizard.patch_config(path.read_bytes(), "223456789012345678", ())
            path.write_bytes(updated)
            return True

        with patch("builtins.input", side_effect=["discord", "start", "quit"]), \
                patch.object(console, "configure_discord", side_effect=save_fixture), \
                patch.object(console, "run") as run:
            self.main()
        saved_config, path = run.call_args.args
        self.assertEqual(saved_config.discord.guild_id, "223456789012345678")
        self.assertNotEqual(saved_config.discord.guild_id, old_config.discord.guild_id)
        self.assertEqual(path, self.path)

    def test_start_uses_environment_before_saved_credentials(self):
        with patch.dict(console.os.environ, {"DISCORD_BOT_TOKEN": TOKEN, "OPENAI_API_KEY": KEY}, clear=True), \
                patch.object(console, "load_discord_token") as loader, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config, self.path)
        loader.assert_not_called()
        prompt.assert_not_called()
        serve.assert_awaited_once_with(self.config, KEY, TOKEN)

    def test_start_uses_saved_guild_bound_token_without_prompting_again(self):
        with patch.dict(console.os.environ, {"OPENAI_API_KEY": KEY}, clear=True), \
                patch.object(console, "load_discord_token", return_value=TOKEN) as loader, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config, self.path)
        loader.assert_called_once_with(self.path, GUILD)
        prompt.assert_not_called()
        serve.assert_awaited_once_with(self.config, KEY, TOKEN)

    def test_start_uses_private_session_prompts_when_no_credentials_are_stored(self):
        with patch.dict(console.os.environ, {}, clear=True), \
                patch.object(console, "load_discord_token", return_value=None), \
                patch.object(console, "prompt_secret", side_effect=[TOKEN, KEY]) as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve, \
                redirect_stdout(io.StringIO()) as output:
            console.run(self.config, self.path)
        self.assertEqual(prompt.call_count, 2)
        self.assertIn("Discord bot token", prompt.call_args_list[0].args[0])
        self.assertIn("OpenAI API key", prompt.call_args_list[1].args[0])
        serve.assert_awaited_once_with(self.config, KEY, TOKEN)
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertNotIn(KEY, output.getvalue())
        self.assertFalse(secret_path(self.path).exists())

    def test_start_missing_guild_rejects_before_requesting_credentials(self):
        config = replace(self.config, discord=replace(self.config.discord, guild_id=""))
        with patch.object(console, "load_discord_token") as loader, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve, \
                self.assertRaises(ValueError):
            console.run(config, self.path)
        loader.assert_not_called()
        prompt.assert_not_called()
        serve.assert_not_awaited()

    def test_invalid_saved_token_requires_repair_instead_of_silent_fallback(self):
        with patch.dict(console.os.environ, {}, clear=True), \
                patch.object(console, "load_discord_token", side_effect=CredentialError("credential_scope_mismatch")), \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve, \
                self.assertRaises(CredentialError):
            console.run(self.config, self.path)
        prompt.assert_not_called()
        serve.assert_not_awaited()

    def test_cancelling_private_token_prompt_never_connects(self):
        for interruption in (EOFError(), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__), \
                    patch.dict(console.os.environ, {}, clear=True), \
                    patch.object(console, "load_discord_token", return_value=None), \
                    patch.object(console, "prompt_secret", side_effect=interruption), \
                    patch.object(console, "serve", new_callable=AsyncMock) as serve, \
                    redirect_stdout(io.StringIO()) as output:
                console.run(self.config, self.path)
            serve.assert_not_awaited()
            self.assertIn("no connection", output.getvalue().lower())

    def test_cancelling_key_prompt_after_saved_token_never_connects_or_echoes_token(self):
        with patch.dict(console.os.environ, {}, clear=True), \
                patch.object(console, "load_discord_token", return_value=TOKEN), \
                patch.object(console, "prompt_secret", side_effect=KeyboardInterrupt()), \
                patch.object(console, "serve", new_callable=AsyncMock) as serve, \
                redirect_stdout(io.StringIO()) as output:
            console.run(self.config, self.path)
        serve.assert_not_awaited()
        self.assertIn("no connection", output.getvalue().lower())
        self.assertNotIn(TOKEN, output.getvalue())

    def test_status_looks_only_for_file_presence_and_never_decrypts_credentials(self):
        secret_path(self.path).write_bytes(TOKEN.encode())
        with patch.object(console, "load_discord_token", side_effect=AssertionError("must not decrypt")) as loader, \
                patch.object(console, "prompt_secret") as prompt, redirect_stdout(io.StringIO()) as output:
            console.status(self.config, self.path)
        self.assertIn("present (checked at start)", output.getvalue())
        self.assertIn("not proof of a live Gateway connection", output.getvalue())
        self.assertNotIn(TOKEN, output.getvalue())
        loader.assert_not_called()
        prompt.assert_not_called()

    def test_interactive_credential_error_reports_only_fixed_code(self):
        with patch("builtins.input", side_effect=["start", "quit"]), \
                patch.object(console, "run", side_effect=CredentialError("credential_corrupt")):
            output = self.main()
        self.assertIn("Credential error: credential_corrupt", output)
        self.assertIn("Use discord setup", output)

    def test_interactive_transport_error_never_displays_arbitrary_provider_text(self):
        with patch("builtins.input", side_effect=["start", "quit"]), \
                patch.object(console, "run", side_effect=RuntimeError(TOKEN)):
            output = self.main()
        self.assertIn("Command failed: RuntimeError", output)
        self.assertNotIn(TOKEN, output)


if __name__ == "__main__":
    unittest.main()
