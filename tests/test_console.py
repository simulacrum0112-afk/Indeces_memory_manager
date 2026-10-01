from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, call, patch

from indeces import console, console_session, discord_wizard
from indeces.config import load_config
from indeces.credentials import CredentialError, openai_secret_path, secret_path


GUILD = "123456789012345678"
TOKEN = "console-synthetic-bot-token-offline-only"
KEY = "synthetic-openai-key-offline-only"


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "config.local.toml"
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

    def test_cli_npmi_reads_aggregate_without_service_credentials_or_scratch(self):
        from indeces.memory import MemoryGraph
        from indeces.store import Store
        store = Store(self.config.state_dir)
        try:
            graph = MemoryGraph(store.db)
            graph.add(f"{GUILD}:knowledge", "synthetic-source", "offline", [
                {"text": "alpha beta", "quote": "alpha beta", "marks": ["alpha", "beta"]}], 1)
        finally:
            store.close()
        with patch.object(console, "run") as run, patch.object(console, "serve") as serve, \
                patch.object(console, "prompt_secret") as secret, patch.object(console, "ScratchLog") as scratch:
            output = self.main("npmi")
        self.assertIn("1 个有效块", output)
        self.assertIn("1 条正 NPMI 边", output)
        self.assertIn("不是置信度", output)
        run.assert_not_called()
        serve.assert_not_called()
        secret.assert_not_called()
        scratch.assert_not_called()
        self.assertFalse(self.config.scratch_dir.exists())

    def test_interactive_npmi_returns_to_command_input(self):
        with patch("builtins.input", side_effect=["npmi", "quit"]), \
                patch.object(console, "run") as run:
            output = self.main()
        self.assertIn("尚无可读取的知识图", output)
        run.assert_not_called()

    def test_npmi_without_config_does_not_initialize_files(self):
        self.path.unlink()
        with patch.object(console, "initialize") as initialize:
            output = self.main("npmi")
        self.assertIn("不会创建配置", output)
        initialize.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_npmi_unavailable_snapshot_does_not_report_empty_graph(self):
        with patch.object(console, "npmi_diagnostics", return_value={
                "statistics": None, "warnings": ["database_snapshot_unavailable"]}):
            output = self.main("npmi")
        self.assertIn("无法判断图状态", output)
        self.assertNotIn("尚无可读取", output)

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
        self.assertIn("discord | apikey | knowledge | start | status", output)

    def test_cli_apikey_routes_to_local_wizard_without_loading_runtime(self):
        with patch.object(console, "configure_api_key", return_value=True) as wizard, \
                patch.object(console, "run") as run, patch.object(console, "serve") as serve, \
                patch.object(console, "load_config") as loader:
            self.main("apikey")
        wizard.assert_called_once_with(self.path)
        run.assert_not_called()
        serve.assert_not_called()
        loader.assert_not_called()

    def test_cli_cancelled_api_key_setup_returns_failure_code(self):
        with patch.object(console, "configure_api_key", return_value=False), \
                self.assertRaises(SystemExit) as error:
            self.main("apikey")
        self.assertEqual(error.exception.code, 2)

    def test_interactive_apikey_returns_to_console_without_starting_service(self):
        with patch("builtins.input", side_effect=["apikey", "quit"]), \
                patch.object(console, "configure_api_key", return_value=True) as wizard, \
                patch.object(console, "run") as run:
            output = self.main()
        wizard.assert_called_once_with(self.path)
        run.assert_not_called()
        self.assertIn("discord | apikey | knowledge | start | status", output)

    def test_cli_knowledge_routes_to_single_console_without_service_or_credentials(self):
        with patch.object(console_session, "run_console") as session, \
                patch.object(console, "run") as run, patch.object(console, "prompt_secret") as prompt:
            self.main("knowledge")
        session.assert_called_once_with(self.path, "knowledge")
        run.assert_not_called()
        prompt.assert_not_called()

    def test_interactive_knowledge_renders_internal_panel_without_new_process(self):
        with patch("builtins.input", side_effect=["knowledge", "quit"]), \
                patch.object(console, "show_knowledge_directory") as directory, \
                patch.object(console, "run") as run:
            output = self.main()
        directory.assert_called_once_with(self.config)
        run.assert_not_called()
        self.assertIn("知识库当前版本状态", output)
        self.assertNotIn("独立窗口", output)

    def test_cli_knowledge_once_reads_one_snapshot_without_starting_service(self):
        with patch("sys.argv", ["indeces", "knowledge", "--once", "--config", str(self.path)]), \
                patch.object(console, "show_knowledge_directory"), \
                patch.object(console, "show_knowledge_progress") as progress, \
                patch.object(console, "run") as run, redirect_stdout(io.StringIO()):
            console.main()
        progress.assert_called_once_with(self.config, watch=False)
        run.assert_not_called()

    def test_legacy_window_entry_routes_to_shared_console(self):
        with patch.object(console_session, "run_console") as session:
            console.open_knowledge_window(self.path)
        session.assert_called_once_with(self.path, "knowledge")

    def test_legacy_window_launcher_rejects_unexpected_command(self):
        with patch.object(console_session, "run_console") as session, self.assertRaises(ValueError):
            console.open_command_window("apikey", self.path)
        session.assert_not_called()

    def test_once_on_start_is_rejected_before_loading_or_starting(self):
        with patch("sys.argv", ["indeces", "start", "--once"]), \
                patch.object(console, "load_config") as load, patch.object(console, "run") as run, \
                self.assertRaises(SystemExit) as error:
            console.main()
        self.assertEqual(error.exception.code, 2)
        load.assert_not_called()
        run.assert_not_called()

    def test_start_panel_reloads_saved_config_after_wizard(self):
        configurations = []
        def save_fixture(path):
            path.write_bytes(discord_wizard.patch_config(path.read_bytes(), "223456789012345678", ()))
            return True
        with patch("builtins.input", side_effect=["discord", "start", "quit"]), \
                patch.object(console, "configure_discord", side_effect=save_fixture), \
                patch.object(console_session.ConsoleSession, "_start", side_effect=configurations.append):
            self.main()
        self.assertEqual(configurations[0].discord.guild_id, "223456789012345678")
        self.assertNotEqual(configurations[0], self.config)

    def test_internal_panels_keep_reading_commands_without_starting_new_terminal(self):
        observer = SimpleNamespace(url="http://127.0.0.1:12345/synthetic/", start=lambda: None, close=lambda: None)
        with patch("builtins.input", side_effect=["start", "knowledge", "observe", "status", "quit"]), \
                patch.object(console_session.ConsoleSession, "_start") as start, \
                patch.object(console, "show_knowledge_directory") as directory, \
                patch.object(console, "ObserverServer", return_value=observer), \
                patch.object(console, "prepare_scratch_directory"), patch.object(console, "npmi") as npmi, \
                patch.object(console, "status") as status, patch.object(console, "prompt_secret") as secret:
            output = self.main()
        start.assert_called_once_with(self.config)
        directory.assert_called_once_with(self.config)
        npmi.assert_called_once_with(self.config)
        status.assert_called_once_with(self.config, self.path)
        secret.assert_not_called()
        self.assertIn("切换页面不会重启", output)
        self.assertNotIn("独立窗口", output)

    def test_cli_start_uses_same_console_by_default(self):
        with patch.object(console_session, "run_console") as session, patch.object(console, "run") as run:
            self.main("start")
        session.assert_called_once_with(self.path, "start")
        run.assert_not_called()

    def test_cli_headless_start_preserves_explicit_foreground_runtime(self):
        with patch("sys.argv", ["indeces", "start", "--headless", "--config", str(self.path)]), \
                patch.object(console, "run") as run, patch.object(console_session, "run_console") as session:
            console.main()
        run.assert_called_once_with(self.config, self.path)
        session.assert_not_called()

    def test_keep_window_legacy_flag_no_longer_opens_or_pauses_terminal(self):
        with patch("sys.argv", ["indeces", "start", "--config", str(self.path), "--keep-window"]), \
                patch.object(console_session, "run_console") as session, patch("builtins.input") as prompt:
            console.main()
        session.assert_called_once_with(self.path, "start")
        prompt.assert_not_called()

    def test_keep_window_on_console_is_rejected_before_loading(self):
        with patch("sys.argv", ["indeces", "console", "--keep-window"]), \
                patch.object(console, "load_config") as loader, self.assertRaises(SystemExit) as error:
            console.main()
        self.assertEqual(error.exception.code, 2)
        loader.assert_not_called()

    def test_live_console_routing_does_not_load_config_or_open_credentials(self):
        with patch.object(console, "route_existing", return_value={"accepted": True, "focused": False}) as route, \
                patch.object(console, "load_config") as loader, patch.object(console, "prompt_secret") as prompt:
            output = self.main("start")
        route.assert_called_once_with(self.path, "start", path=None)
        loader.assert_not_called()
        prompt.assert_not_called()
        self.assertIn("已连接现有", output)

    def test_start_uses_environment_before_saved_credentials(self):
        with patch.dict(console.os.environ, {"DISCORD_BOT_TOKEN": TOKEN, "OPENAI_API_KEY": KEY}, clear=True), \
                patch.object(console, "load_discord_token") as loader, \
                patch.object(console, "load_openai_key") as key_loader, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config, self.path)
        loader.assert_not_called()
        key_loader.assert_not_called()
        prompt.assert_not_called()
        serve.assert_awaited_once_with(self.config, KEY, TOKEN, lease=ANY)

    def test_start_uses_saved_guild_bound_token_without_prompting_again(self):
        with patch.dict(console.os.environ, {"OPENAI_API_KEY": KEY}, clear=True), \
                patch.object(console, "load_discord_token", return_value=TOKEN) as loader, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config, self.path)
        loader.assert_called_once_with(self.path, GUILD)
        prompt.assert_not_called()
        serve.assert_awaited_once_with(self.config, KEY, TOKEN, lease=ANY)

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
        serve.assert_awaited_once_with(self.config, KEY, TOKEN, lease=ANY)
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertNotIn(KEY, output.getvalue())
        self.assertFalse(secret_path(self.path).exists())
        self.assertFalse(openai_secret_path(self.path).exists())

    def test_start_uses_saved_api_key_before_session_prompt(self):
        with patch.dict(console.os.environ, {"DISCORD_BOT_TOKEN": TOKEN}, clear=True), \
                patch.object(console, "load_openai_key", return_value=KEY) as loader, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config, self.path)
        loader.assert_called_once_with(self.path)
        prompt.assert_not_called()
        serve.assert_awaited_once_with(self.config, KEY, TOKEN, lease=ANY)

    def test_start_resolves_config_alias_to_same_credential_location_as_wizard(self):
        child = self.path.parent / "synthetic-parent-alias"
        child.mkdir()
        alias = child / ".." / self.path.name
        with patch.dict(console.os.environ, {}, clear=True), \
                patch.object(console, "load_discord_token", return_value=TOKEN) as token_loader, \
                patch.object(console, "load_openai_key", return_value=KEY) as key_loader, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config, alias)
        token_loader.assert_called_once_with(self.path, GUILD)
        key_loader.assert_called_once_with(self.path)
        prompt.assert_not_called()
        serve.assert_awaited_once_with(self.config, KEY, TOKEN, lease=ANY)

    def test_invalid_saved_api_key_requires_repair_without_fallback_or_connection(self):
        with patch.dict(console.os.environ, {"DISCORD_BOT_TOKEN": TOKEN}, clear=True), \
                patch.object(console, "load_openai_key", side_effect=CredentialError("credential_corrupt")), \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve, \
                self.assertRaises(CredentialError):
            console.run(self.config, self.path)
        prompt.assert_not_called()
        serve.assert_not_awaited()

    def test_invalid_api_key_environment_is_rejected_without_saved_key_fallback(self):
        with patch.dict(console.os.environ, {"DISCORD_BOT_TOKEN": TOKEN, "OPENAI_API_KEY": "invalid\nkey"}, clear=True), \
                patch.object(console, "load_openai_key") as loader, \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve, \
                self.assertRaises(CredentialError) as error:
            console.run(self.config, self.path)
        self.assertEqual(error.exception.code, "invalid_api_key")
        loader.assert_not_called()
        prompt.assert_not_called()
        serve.assert_not_awaited()

    def test_start_without_config_path_uses_session_key_and_does_not_load_vault(self):
        with patch.dict(console.os.environ, {"DISCORD_BOT_TOKEN": TOKEN}, clear=True), \
                patch.object(console, "load_openai_key") as loader, \
                patch.object(console, "prompt_secret", return_value=KEY) as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config)
        loader.assert_not_called()
        prompt.assert_called_once()
        serve.assert_awaited_once_with(self.config, KEY, TOKEN, lease=ANY)

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
        openai_secret_path(self.path).write_bytes(KEY.encode())
        with patch.object(console, "load_discord_token", side_effect=AssertionError("must not decrypt")) as loader, \
                patch.object(console, "load_openai_key", side_effect=AssertionError("must not decrypt")) as key_loader, \
                patch.object(console, "prompt_secret") as prompt, redirect_stdout(io.StringIO()) as output:
            console.status(self.config, self.path)
        self.assertIn("present (checked at start)", output.getvalue())
        self.assertIn("not proof of a live Gateway connection", output.getvalue())
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertNotIn(KEY, output.getvalue())
        loader.assert_not_called()
        key_loader.assert_not_called()
        prompt.assert_not_called()

    def test_interactive_credential_error_reports_only_fixed_code(self):
        with patch("builtins.input", side_effect=["apikey", "quit"]), \
                patch.object(console, "configure_api_key", side_effect=CredentialError("credential_corrupt")):
            output = self.main()
        self.assertIn("Credential error: credential_corrupt", output)
        self.assertIn("Use discord setup", output)

    def test_interactive_transport_error_never_displays_arbitrary_provider_text(self):
        with patch("builtins.input", side_effect=["start", "quit"]), \
                patch.object(console_session.ConsoleSession, "_start", side_effect=RuntimeError(TOKEN)):
            output = self.main()
        self.assertIn("Command failed: RuntimeError", output)
        self.assertNotIn(TOKEN, output)

    def test_direct_start_never_exposes_arbitrary_authentication_exception(self):
        output = io.StringIO()
        with patch("sys.argv", ["indeces", "start", "--headless", "--config", str(self.path)]), \
                patch.object(console, "run", side_effect=Exception(KEY)), \
                redirect_stdout(output), self.assertRaises(SystemExit) as error:
            console.main()
        self.assertEqual(error.exception.code, 2)
        self.assertIn("Startup failed: Exception", output.getvalue())
        self.assertNotIn(KEY, output.getvalue())

    @unittest.skipUnless(os.name == "nt", "current-user DPAPI is Windows only")
    def test_native_wizard_saved_key_is_used_by_next_start_without_echo_or_prompt(self):
        original = self.path.read_bytes()
        with patch.dict(console.os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as output:
            saved = console.configure_api_key(self.path, input_fn=lambda _: "y", secret_fn=lambda _: KEY)
        self.assertTrue(saved)
        ciphertext = openai_secret_path(self.path).read_bytes()
        self.assertNotIn(KEY.encode(), ciphertext)
        self.assertNotIn(KEY, output.getvalue())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(secret_path(self.path).exists())
        with patch.dict(console.os.environ, {"DISCORD_BOT_TOKEN": TOKEN}, clear=True), \
                patch.object(console, "prompt_secret") as prompt, \
                patch.object(console, "serve", new_callable=AsyncMock) as serve:
            console.run(self.config, self.path)
        prompt.assert_not_called()
        serve.assert_awaited_once_with(self.config, KEY, TOKEN, lease=ANY)


if __name__ == "__main__":
    unittest.main()
