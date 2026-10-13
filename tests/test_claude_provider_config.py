"""Offline provider/config boundary cases. Authored; execution is pending.

All keys here are synthetic and all transports are mocked. No real API call.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces import api_key_wizard, console, console_session, model_provider, model_wizard
from indeces.config import AdapterConfig, Budget, load_config
from indeces.credentials import anthropic_secret_path, openai_secret_path
from indeces.lock import InstanceLock


ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-claude-key-offline-only"


class ClaudeProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "config.local.toml"
        self.template = (ROOT / "config.claude.example.toml").read_text(encoding="utf-8")
        self.path.write_text(self.template.replace('guild_id = ""', 'guild_id = "10"'), encoding="utf-8")
        self.config = load_config(self.path)

    def load(self, transform):
        self.path.write_text(transform(self.template), encoding="utf-8")
        return load_config(self.path)

    def test_new_explicit_claude_template_defaults_to_opus_high_without_more_budget(self):
        old = load_config(ROOT / "config.example.toml")
        new = self.config
        self.assertEqual((new.adapter.provider, new.adapter.model), ("anthropic", "claude-opus-5-5"))
        self.assertEqual(new.adapter.base_url, "https://api.anthropic.com/v1")
        self.assertEqual({b.reasoning for b in new.adapter.budgets.values()}, {"high"})
        for stage in ("label", "summary", "reply", "selection", "query"):
            a, b = new.adapter.budgets[stage], old.adapter.budgets[stage]
            self.assertEqual((a.input_tokens, a.output_tokens, a.seconds), (b.input_tokens, b.output_tokens, b.seconds))
        self.assertEqual(new.runtime, old.runtime)
        self.assertEqual(new.knowledge, old.knowledge)
        self.assertEqual(new.pdf, old.pdf)

    def test_fable_is_an_explicit_choice_and_does_not_change_limits(self):
        fable = self.load(lambda t: t.replace('model = "claude-opus-5-5"', 'model = "claude-fable-5-1"'))
        self.assertEqual(fable.adapter.model, "claude-fable-5-1")
        self.assertEqual(fable.adapter.budgets, self.config.adapter.budgets)

    def test_existing_openai_config_without_provider_stays_openai(self):
        config = load_config(ROOT / "config.example.toml")
        self.assertEqual(config.adapter.provider, "openai")
        self.assertEqual(config.adapter.model, "gpt-6.1-sol")
        self.assertEqual({b.reasoning for b in config.adapter.budgets.values()}, {"medium"})

    def test_pre_provider_supplied_key_caller_preserves_openai_default(self):
        config = SimpleNamespace()
        self.assertEqual(model_provider.credential_reference(config), "OPENAI_API_KEY")
        with patch.object(model_provider, "load_openai_key") as openai, \
                patch.object(model_provider, "load_anthropic_key") as claude:
            self.assertEqual(model_provider.resolve_model_key(config, self.path, supplied_key=KEY), KEY)
        openai.assert_not_called()
        claude.assert_not_called()

    def test_pre_provider_factory_remains_official_openai_without_model_inference(self):
        config = SimpleNamespace(model="gpt-6.1-sol", base_url="https://api.openai.com/v1")
        sentinel = object()
        with patch.object(model_provider, "OpenAIAdapter", return_value=sentinel) as openai:
            self.assertIs(model_provider.create_model_adapter(config, None, KEY), sentinel)
        openai.assert_called_once_with(config, None, KEY)
        for model, endpoint in (("claude-opus-5-5", "https://api.anthropic.com/v1"),
                                ("gpt-6.1-sol", "https://other-supplier.invalid/v1")):
            with self.subTest(model=model, endpoint=endpoint), self.assertRaisesRegex(ValueError, "official model and endpoint"):
                model_provider.create_model_adapter(SimpleNamespace(model=model, base_url=endpoint), None, KEY)

    def test_explicit_invalid_provider_never_uses_missing_field_default(self):
        for provider in (None, "", "proxy", "OpenAI", False, 1, [], {}):
            config = SimpleNamespace(provider=provider, model="gpt-6.1-sol", base_url="https://api.openai.com/v1")
            for action in (lambda: model_provider.credential_reference(config),
                           lambda: model_provider.resolve_model_key(config, self.path, supplied_key=KEY),
                           lambda: model_provider.create_model_adapter(config, None, KEY)):
                with self.subTest(provider=provider, action=action), self.assertRaisesRegex(ValueError, "unsupported model provider"):
                    action()

    def test_model_endpoint_never_implicitly_changes_provider(self):
        with self.assertRaisesRegex(ValueError, "migrate the model explicitly"):
            self.load(lambda t: t.replace('provider = "anthropic"\n', ""))

    def test_missing_or_different_supplier_is_rejected_without_fallback(self):
        for transform in (
            lambda t: t.replace('base_url = "https://api.anthropic.com/v1"', 'base_url = ""'),
            lambda t: "\n".join(line for line in t.splitlines() if not line.startswith("base_url =")),
            lambda t: t.replace("https://api.anthropic.com/v1", "https://other-supplier.invalid/v1"),
            lambda t: t.replace("https://api.anthropic.com/v1", "https://api.openai.com/v1"),
        ):
            with self.subTest(transform=transform), self.assertRaises(ValueError):
                self.load(transform)

    def test_unsupported_model_or_provider_declines(self):
        for transform in (
            lambda t: t.replace('model = "claude-opus-5-5"', 'model = "claude-other"'),
            lambda t: t.replace('provider = "anthropic"', 'provider = "proxy"'),
        ):
            with self.subTest(transform=transform), self.assertRaises(ValueError):
                self.load(transform)

    def test_all_claude_stages_require_explicit_high(self):
        for stage in ("label", "summary", "reply", "selection", "query"):
            budgets = dict(self.config.adapter.budgets)
            budgets[stage] = replace(budgets[stage], reasoning="medium")
            with self.subTest(stage=stage), self.assertRaisesRegex(ValueError, "explicit high"):
                replace(self.config.adapter, budgets=budgets)
        with self.assertRaisesRegex(ValueError, "explicit high"):
            self.load(lambda t: "\n".join(line for line in t.splitlines() if not line.startswith("reasoning =")))

    def test_openai_direct_fake_configuration_remains_constructible(self):
        config = AdapterConfig("offline-test-model", "https://offline.invalid", {"reply": Budget(10, 10, 1)})
        self.assertEqual(config.provider, "openai")
        with self.assertRaisesRegex(ValueError, "official model and endpoint"):
            model_provider.create_model_adapter(config, object(), KEY)

    def test_native_context_ceiling_is_checked_before_adapter_construction(self):
        budgets = dict(self.config.adapter.budgets)
        budgets["reply"] = Budget(1_000_000, 20_000, 45, "high")
        with self.assertRaisesRegex(ValueError, "exceeds model context"):
            replace(self.config.adapter, budgets=budgets)

    def test_claude_key_is_provider_scoped_and_never_reads_old_vault(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": "synthetic-wrong-provider-key"}, clear=True), \
                patch.object(model_provider, "load_openai_key") as saved:
            key = model_provider.resolve_model_key(self.config.adapter, self.path)
        self.assertEqual(key, KEY)
        saved.assert_not_called()

    def test_missing_claude_key_cannot_use_openai_vault(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": KEY}, clear=True), \
                patch.object(model_provider, "load_openai_key", return_value=KEY) as saved, \
                patch.object(model_provider, "load_anthropic_key", return_value=None), \
                self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
            model_provider.resolve_model_key(self.config.adapter, self.path)
        saved.assert_not_called()

    def test_claude_saved_key_and_private_session_precedence(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(model_provider, "load_anthropic_key", return_value=KEY) as saved, \
                patch.object(console, "prompt_secret") as prompt:
            self.assertEqual(model_provider.resolve_model_key(self.config.adapter, self.path, prompt=prompt), KEY)
        saved.assert_called_once_with(self.path)
        prompt.assert_not_called()
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(model_provider, "load_anthropic_key", return_value=None), \
                patch.object(console, "prompt_secret", return_value=KEY) as prompt:
            self.assertEqual(model_provider.resolve_model_key(self.config.adapter, self.path, prompt=prompt), KEY)
        self.assertIn("Claude (Anthropic)", prompt.call_args.args[0])

    def test_openai_environment_vault_session_precedence_is_preserved(self):
        config = load_config(ROOT / "config.example.toml").adapter
        with patch.dict("os.environ", {"OPENAI_API_KEY": KEY, "ANTHROPIC_API_KEY": "synthetic-other-provider-key"}, clear=True), \
                patch.object(model_provider, "load_openai_key") as saved:
            self.assertEqual(model_provider.resolve_model_key(config, self.path), KEY)
        saved.assert_not_called()
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(model_provider, "load_openai_key", return_value=KEY) as saved:
            self.assertEqual(model_provider.resolve_model_key(config, self.path), KEY)
        saved.assert_called_once_with(self.path)
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(model_provider, "load_openai_key", return_value=None), \
                patch.object(console, "prompt_secret", return_value=KEY) as prompt:
            self.assertEqual(model_provider.resolve_model_key(config, self.path, prompt=console.prompt_secret), KEY)
        prompt.assert_called_once()

    def test_claude_startup_auth_failure_precedes_discord_lookup(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(console, "load_discord_token") as discord_secret, \
                patch.object(model_provider, "load_anthropic_key", return_value=None), \
                patch.object(console, "prompt_secret", return_value="") as prompt, \
                self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
            console._startup_credentials(self.config, self.path)
        discord_secret.assert_not_called()
        prompt.assert_called_once()
        self.assertIn("Claude (Anthropic)", prompt.call_args.args[0])

    def test_headless_missing_auth_reports_fixed_provider_blocker_without_connection(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch("sys.argv", ["indeces", "start", "--headless", "--config", str(self.path)]), \
                patch.object(console, "InstanceLock") as lease, \
                patch.object(console, "serve") as serve, \
                patch.object(console, "load_discord_token") as discord_secret, \
                patch.object(model_provider, "load_anthropic_key", return_value=None), \
                patch.object(console, "prompt_secret", return_value=""), \
                redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            console.main()
        self.assertEqual(error.exception.code, 2)
        self.assertIn("ANTHROPIC_API_KEY", output.getvalue())
        self.assertIn("No connection was made", output.getvalue())
        serve.assert_not_called()
        discord_secret.assert_not_called()
        lease.return_value.close.assert_called_once()

    def test_claude_apikey_command_cannot_read_or_save_openai_secret(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(api_key_wizard, "load_openai_key") as saved, \
                patch.object(api_key_wizard, "save_openai_key") as save, \
                patch.object(api_key_wizard, "save_anthropic_key") as claude_save:
            messages = []
            result = api_key_wizard.configure_api_key(self.path, output=messages.append,
                                                     secret_fn=lambda _: KEY, input_fn=lambda _: "n")
        self.assertFalse(result)
        self.assertTrue(any("Claude" in line for line in messages))
        for mock in (saved, save, claude_save):
            mock.assert_not_called()

    def test_factory_selects_claude_without_constructing_openai(self):
        sentinel = object()
        scratch = object()
        with patch("indeces.claude_adapter.ClaudeAdapter", return_value=sentinel) as claude, \
                patch.object(model_provider, "OpenAIAdapter") as openai:
            self.assertIs(model_provider.create_model_adapter(self.config.adapter, scratch, KEY), sentinel)
        claude.assert_called_once_with(self.config.adapter, scratch, KEY)
        openai.assert_not_called()

    def test_factory_keeps_explicit_openai_rollback(self):
        config = load_config(ROOT / "config.example.toml").adapter
        sentinel = object()
        with patch.object(model_provider, "OpenAIAdapter", return_value=sentinel) as openai:
            self.assertIs(model_provider.create_model_adapter(config, None, KEY), sentinel)
        openai.assert_called_once_with(config, None, KEY)

    def test_initialization_preserves_production_openai_default_without_migrating_existing_config(self):
        existing = self.path.read_bytes()
        with redirect_stdout(io.StringIO()):
            console.initialize(self.path)
        self.assertEqual(self.path.read_bytes(), existing)
        new_path = self.path.parent / "new.toml"
        with redirect_stdout(io.StringIO()):
            console.initialize(new_path)
        config = load_config(new_path)
        self.assertEqual((config.adapter.provider, config.adapter.model), ("openai", "gpt-6.1-sol"))
        self.assertEqual(console.effective_profiles(config)["anthropic"]["model"], "claude-opus-5-5")

    def test_status_displays_configuration_without_authentication(self):
        with patch.object(console, "resolve_model_key") as resolve, \
                patch.object(model_provider, "load_openai_key") as saved, \
                redirect_stdout(io.StringIO()) as output:
            console.status(self.config, self.path)
        self.assertIn("credential reference: ANTHROPIC_API_KEY", output.getvalue())
        self.assertIn("authentication is checked only at start", output.getvalue())
        self.assertNotIn(KEY, output.getvalue())
        resolve.assert_not_called()
        saved.assert_not_called()


class ModelProfileWizardTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "config.local.toml"
        self.original = (ROOT / "config.example.toml").read_bytes()
        self.path.write_bytes(self.original)
        self.config = load_config(self.path)

    def switch(self, provider, model="", confirm="yes", output=None):
        answers = iter((provider, model, confirm))
        return model_wizard.configure_model(self.path, input_fn=lambda _: next(answers), output=output or (lambda _: None))

    def test_openai_claude_round_trip_preserves_independent_model_efforts_and_all_caps(self):
        self.assertTrue(self.switch("anthropic", "claude-fable-5-1"))
        claude = load_config(self.path)
        self.assertEqual(claude.adapter.model, "claude-fable-5-1")
        self.assertEqual({b.reasoning for b in claude.adapter.budgets.values()}, {"high"})
        self.assertTrue(self.switch("openai"))
        openai = load_config(self.path)
        self.assertEqual(openai.adapter.model, "gpt-6.1-sol")
        self.assertEqual(openai.adapter.budgets, self.config.adapter.budgets)
        self.assertEqual(openai.model_profiles["anthropic"]["model"], "claude-fable-5-1")
        self.assertTrue(self.switch("anthropic"))
        final = load_config(self.path)
        self.assertEqual(final.adapter.model, "claude-fable-5-1")
        for stage, budget in self.config.adapter.budgets.items():
            selected = final.adapter.budgets[stage]
            self.assertEqual((budget.input_tokens, budget.output_tokens, budget.seconds),
                             (selected.input_tokens, selected.output_tokens, selected.seconds))
        for field in ("runtime", "knowledge", "pdf", "discord", "state_dir", "scratch_dir", "knowledge_dir"):
            self.assertEqual(getattr(final, field), getattr(self.config, field))

    def test_existing_explicit_openai_low_profile_is_preserved_after_switches(self):
        self.path.write_bytes(self.original.replace(b'reasoning = "medium"', b'reasoning = "low"'))
        self.assertTrue(self.switch("anthropic"))
        self.assertTrue(self.switch("openai"))
        self.assertEqual({b.reasoning for b in load_config(self.path).adapter.budgets.values()}, {"low"})

    def test_inactive_claude_profile_does_not_shrink_openai_context_but_switch_declines(self):
        original = self.original.replace(b"input_tokens = 16384", b"input_tokens = 1000000")
        self.path.write_bytes(original)
        config = load_config(self.path)
        self.assertEqual(config.adapter.provider, "openai")
        self.assertEqual(console.effective_profiles(config)["anthropic"]["model"], "claude-opus-5-5")
        self.assertFalse(self.switch("claude"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.before-model-*.bak")), [])

    def test_first_claude_choice_uses_opus_high_and_does_not_rewrite_keys(self):
        openai_vault, claude_vault = openai_secret_path(self.path), anthropic_secret_path(self.path)
        openai_vault.write_bytes(b"synthetic-openai-ciphertext-only")
        claude_vault.write_bytes(b"synthetic-claude-ciphertext-only")
        self.assertTrue(self.switch("claude"))
        config = load_config(self.path)
        self.assertEqual(config.adapter.model, "claude-opus-5-5")
        self.assertEqual(openai_vault.read_bytes(), b"synthetic-openai-ciphertext-only")
        self.assertEqual(claude_vault.read_bytes(), b"synthetic-claude-ciphertext-only")

    def test_review_cancellation_preserves_config_and_creates_no_backup(self):
        self.assertFalse(self.switch("anthropic", confirm="n"))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob("*.before-model-*.bak")), [])

    def test_active_state_lease_blocks_model_save_before_prompts(self):
        lease = InstanceLock(self.config.state_dir)
        try:
            with patch("builtins.input") as prompt:
                self.assertFalse(model_wizard.configure_model(self.path, output=lambda _: None))
            prompt.assert_not_called()
        finally:
            lease.close()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_saved_selection_has_byte_exact_rollback_backup(self):
        self.assertTrue(self.switch("anthropic"))
        backups = list(self.path.parent.glob("*.before-model-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)

    def test_commit_failure_restores_prior_selection_and_retains_backup(self):
        writes = []
        def write_then_fail(path, contents):
            path.write_bytes(contents)
            writes.append(contents)
            if len(writes) == 1:
                raise OSError("synthetic commit sink failure")
        with patch.object(model_wizard, "_atomic_write", side_effect=write_then_fail):
            self.assertFalse(self.switch("anthropic"))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob("*.before-model-*.bak"))[0].read_bytes(), self.original)

    def test_terminal_receipt_failure_does_not_misreport_committed_model(self):
        def output(text):
            if text.startswith("Saved model selection"):
                raise RuntimeError("synthetic terminal failure")
        self.assertTrue(self.switch("anthropic", output=output))
        self.assertEqual(load_config(self.path).adapter.provider, "anthropic")

    def test_external_edit_before_confirmation_is_not_overwritten(self):
        answers = iter(("anthropic", "", "yes"))
        def input_fn(prompt):
            if prompt.startswith("Save"):
                self.path.write_bytes(self.original + b"\n# synthetic external edit\n")
            return next(answers)
        self.assertFalse(model_wizard.configure_model(self.path, input_fn=input_fn, output=lambda _: None))
        self.assertTrue(self.path.read_bytes().endswith(b"# synthetic external edit\n"))
        self.assertEqual(list(self.path.parent.glob("*.before-model-*.bak")), [])

    def test_external_edit_during_backup_is_preserved_before_atomic_write(self):
        backup_config = model_wizard._backup_config
        external = self.original + b"\n# synthetic edit during backup fsync\n"
        def backup_then_edit(path, contents):
            result = backup_config(path, contents)
            path.write_bytes(external)
            return result
        with patch.object(model_wizard, "_backup_config", side_effect=backup_then_edit), \
                patch.object(model_wizard, "_atomic_write") as write:
            self.assertFalse(self.switch("anthropic"))
        write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), external)
        self.assertEqual(list(self.path.parent.glob("*.before-model-*.bak"))[0].read_bytes(), self.original)

    def test_profile_cannot_contain_plaintext_key_or_budget(self):
        self.assertTrue(self.switch("anthropic"))
        valid = self.path.read_text(encoding="utf-8")
        for field in ('api_key = "synthetic-key"', 'input_tokens = 65536'):
            self.path.write_text(valid.replace("[model_profiles.openai]\n", "[model_profiles.openai]\n" + field + "\n"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "profile fields"):
                load_config(self.path)

    def test_active_selection_profile_mismatch_declines(self):
        self.assertTrue(self.switch("anthropic"))
        data = self.path.read_bytes().replace(b'model = "claude-opus-5-5"', b'model = "claude-fable-5-1"', 1)
        self.path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "active model profile"):
            load_config(self.path)

    def test_patch_preserves_scope_when_budget_effort_was_omitted(self):
        original = b"\n".join(line for line in self.original.splitlines() if not line.startswith(b"reasoning ="))
        self.path.write_bytes(original)
        config = load_config(self.path)
        updated = model_wizard.patch_model_config(original, config, "anthropic", "claude-opus-5-5")
        before, after = tomllib.loads(original.decode()), tomllib.loads(updated.decode())
        for field in ("name", "state_dir", "scratch_dir", "knowledge_dir", "discord", "runtime", "knowledge", "pdf"):
            self.assertEqual(before[field], after[field])
        for stage in ("label", "summary", "reply"):
            self.assertEqual({k: v for k, v in before["adapter"]["budgets"][stage].items() if k != "reasoning"},
                             {k: v for k, v in after["adapter"]["budgets"][stage].items() if k != "reasoning"})

    def test_model_command_routes_to_console_wizard_without_start(self):
        with patch("sys.argv", ["indeces", "model", "--config", str(self.path)]), \
                patch.object(console, "route_existing", return_value=None), \
                patch.object(console, "configure_model", return_value=True) as wizard, \
                patch.object(console, "run") as run, patch.object(console, "serve") as serve, \
                redirect_stdout(io.StringIO()):
            console.main()
        wizard.assert_called_once_with(self.path)
        run.assert_not_called()
        serve.assert_not_called()

    def test_model_panel_routes_locally_without_hot_reload(self):
        session = console_session.ConsoleSession(self.path, SimpleNamespace(commands=queue.Queue()))
        try:
            with patch.object(console, "configure_model", return_value=True) as wizard, \
                    patch.object(console, "run") as run, patch.object(console, "serve") as serve:
                self.assertTrue(session.dispatch("model"))
            wizard.assert_called_once_with(self.path)
            self.assertIsNone(session.service)
            run.assert_not_called()
            serve.assert_not_called()
        finally:
            session.close()

    def test_owned_active_service_blocks_setup_even_after_external_state_dir_change(self):
        self.path.write_bytes(self.original.replace(b'state_dir = "state"', b'state_dir = "externally-changed-state"'))
        session = console_session.ConsoleSession(self.path, SimpleNamespace(commands=queue.Queue()))
        session.service = SimpleNamespace(active=True)
        try:
            with patch.object(console, "configure_model") as model, \
                    patch.object(console, "configure_api_key") as apikey, \
                    patch.object(console, "configure_discord") as discord, \
                    patch.object(console, "load_config") as loader, \
                    redirect_stdout(io.StringIO()):
                for command in ("model", "apikey", "discord"):
                    self.assertTrue(session.dispatch(command))
            for mock in (model, apikey, discord, loader):
                mock.assert_not_called()
        finally:
            session.service = None
            session.close()


if __name__ == "__main__":
    unittest.main()
