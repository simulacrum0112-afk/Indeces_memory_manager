"""Credential-default routing compatibility, using only synthetic fixtures."""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict, replace
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from tests import test_manual_trial as legacy


DEFAULT_SCOPE = "existing_credential_default"
EXPLICIT_SCOPE = "explicit_organization_project"
# Reuse the same safety counters as the imported fixture, including its
# fail-closed network guards. Separate test runs start with fresh counters.
EVIDENCE = legacy.EVIDENCE


def default_approval(rates, limits, **overrides):
    return legacy.approval(rates, limits, account_scope=DEFAULT_SCOPE,
                           account_id=None, project_id=None, **overrides)


class DefaultScopeSession(legacy.FakeSession):
    def post(self, endpoint, **options):
        disk = json.loads(self.gate.path.read_text(encoding="utf-8"))
        assert disk["requests"][-1]["status"] == "pending"
        assert disk["requests"][-1]["endpoint"] == endpoint
        assert options["allow_redirects"] is False
        headers = options["headers"]
        assert not any(isinstance(name, str) and name.lower() in {
            "openai-organization", "openai-project"} for name in headers)
        payload = options["json"]
        assert legacy.canonical_hash(payload) == disk["requests"][-1]["payload_sha256"]
        self.requests.append({"endpoint": endpoint, "payload": payload,
                              "header_names": tuple(headers)})
        key = ("fake_http_count_requests" if endpoint == legacy.COUNT_ENDPOINT
               else "fake_http_generation_requests")
        EVIDENCE[key] += 1
        value = self.handler(endpoint, payload)
        received = value if isinstance(value, legacy.FakeResponse) else legacy.FakeResponse(value)
        return legacy.FakeResponseContext(received)


class DefaultScopeFixture(legacy.Fixture):
    def default_gate(self, *, rates=None, limits=None, manifest=None, **overrides):
        rates, limits = rates or legacy.tariff(), limits or legacy.PilotLimits()
        manifest = manifest or default_approval(rates, limits, **overrides)
        return self.gate(rates=rates, limits=limits, manifest=manifest)

    def default_adapter(self, gate, handler=legacy.response):
        self.serial += 1
        cfg = legacy.config(self.root / ("default-adapter-" + str(self.serial)))
        scratch = legacy.ScratchLog(cfg.scratch_dir)
        self.scratches.append(scratch)
        normal = legacy.OpenAIAdapter(cfg.adapter, scratch, api_key=legacy.KEY)
        session = DefaultScopeSession(gate, handler)
        factory = unittest.mock.Mock(return_value=session)
        wrapper = legacy.attach_live_transport(normal, gate, explicit_live=True,
                        validated_descriptor=self.descriptor(gate), session_factory=factory)
        wrapper.item_id = "case-alpha"
        self.addAsyncCleanup(wrapper.close)
        return wrapper, session, factory

    async def invoke(self, wrapper):
        return await wrapper.call("reply", "synthetic instructions",
            [{"role": "user", "content": "synthetic prompt"}], "default-scope-trace")

    def root_inputs(self, *, output="one"):
        bundle = legacy.synthetic_inputs()
        bundle.update(kind="approved_frozen", owner_evidence="synthetic default-scope approval")
        bundle["questions"] = [{"item_id": row["id"], "question": row["text"]}
                               for row in legacy.BUNDLE["items"]]
        rates, limits = legacy.tariff(), legacy.PilotLimits()
        manifest = default_approval(rates, limits, config_path=str(self.config_file),
            config_source_sha256=hashlib.sha256(self.config_file.read_bytes()).hexdigest(),
            candidate_identity=legacy.candidate_identity(legacy.CANDIDATE),
            input_bundle_sha256=legacy.canonical_hash(bundle))
        files = {}
        for name, data in (("inputs", bundle), ("approval", manifest), ("tariff", asdict(rates))):
            path = self.root / ("default-" + name + ".json")
            path.write_text(json.dumps(data), encoding="utf-8")
            files[name] = path
        args = legacy.manual_live.parser().parse_args([
            "--live", "--delivery", "api", "--config", str(self.config_file), "--channel", "20",
            "--inputs", str(files["inputs"]), "--approval", str(files["approval"]),
            "--tariff", str(files["tariff"]), "--candidate-root", str(legacy.CANDIDATE),
            "--output", str(self.root / "run-root" / output)])
        return args, manifest


class DefaultScopeGateTests(DefaultScopeFixture, unittest.TestCase):
    def test_default_scope_requires_both_null_ids_and_rejects_invalid_shapes_before_filesystem(self):
        rates, limits = legacy.tariff(), legacy.PilotLimits()
        valid = default_approval(rates, limits)
        variants = [dict(valid, account_id="synthetic-account"),
                    dict(valid, project_id="synthetic-project"),
                    dict(valid, account_id="", project_id=""),
                    dict(valid, account_id=False), dict(valid, project_id=0),
                    dict(valid, account_scope="unknown_scope"),
                    dict(valid, account_scope=None), dict(valid, account_scope=True),
                    dict(valid, account_scope=EXPLICIT_SCOPE)]
        for field in ("account_id", "project_id", "account_scope"):
            variant = dict(valid)
            del variant[field]
            variants.append(variant)
        for index, manifest in enumerate(variants):
            with self.subTest(shape=index), self.assertRaises(legacy.PilotBlocked):
                self.gate(manifest=manifest)
            self.assertFalse((self.root / "approval-ledgers").exists())
        with self.assertRaises(legacy.PilotBlocked) as caught:
            legacy.PilotGate(mode="live", tariff=rates, limits=limits, approval=valid,
                input_bundle_sha256=legacy.SHA, candidate_identity=legacy.SHA)
        self.assertEqual(caught.exception.code, "live_disabled")
        self.assertFalse((self.root / "approval-ledgers").exists())

    def test_legacy_normalized_approval_bytes_and_explicit_routing_descriptor_remain_compatible(self):
        rates, limits = legacy.tariff(), legacy.PilotLimits()
        manifest = legacy.approval(rates, limits, expires_at="2099-01-01T00:00:00+00:00")
        expected = {"approval_id": "synthetic-approval", "status": "approved", "project": "Indeces",
            "account_id": "synthetic-account", "project_id": "synthetic-project", "guild_id": "10",
            "channel_id": "20", "config_path": manifest["config_path"], "config_source_sha256": legacy.SHA,
            "model": "gpt-6.1-sol", "owner_evidence": "synthetic approval fixture only",
            "allowed_stages": ["reply"], "allowed_response_models": ["gpt-6.1-sol"],
            "account_binding_confirmed": True, "background_knowledge_disabled": True, "delivery_mode": "api",
            "input_bundle_sha256": legacy.SHA, "candidate_identity": legacy.SHA,
            "limits": {"input_tokens": 16384, "output_tokens": 2048, "max_count_requests": 2,
                       "max_generations": 2, "hard_cap_usd": "1.00", "cumulative_input_tokens": 32768,
                       "cumulative_output_tokens": 4096, "cumulative_seconds": 90, "call_seconds": 45},
            "tariff_sha256": legacy.canonical_hash(asdict(rates)), "expires_at": "2099-01-01T00:00:00+00:00"}
        self.assertEqual(manifest, expected)
        gate = self.gate(manifest=manifest)
        self.assertEqual(gate.approval, expected)
        self.assertNotIn("account_scope", gate.approval)
        self.assertEqual(gate.approval_sha256, legacy.canonical_hash(manifest))
        self.assertEqual(gate.ledger["approval_sha256"], legacy.canonical_hash(manifest))
        original_path, original_identity = gate.path, gate.ledger["identity_sha256"]
        gate.close()
        reopened = self.gate(manifest=manifest)
        self.assertEqual(reopened.path, original_path)
        self.assertEqual(reopened.ledger["identity_sha256"], original_identity)
        explicit = dict(manifest, approval_id="synthetic-explicit-scope", account_scope=EXPLICIT_SCOPE)
        explicit_gate = self.gate(manifest=explicit)
        self.assertEqual(explicit_gate.approval, explicit)
        self.assertEqual(explicit_gate.approval_sha256, legacy.canonical_hash(explicit))

    def test_same_approval_id_cannot_switch_account_scope_to_obtain_another_ledger(self):
        rates, limits = legacy.tariff(), legacy.PilotLimits()
        for first_default in (True, False):
            with self.subTest(first_default=first_default):
                approval_id = "scope-switch-" + str(first_default).lower()
                explicit = legacy.approval(rates, limits, approval_id=approval_id)
                default = default_approval(rates, limits, approval_id=approval_id)
                original = self.gate(manifest=default if first_default else explicit)
                self.settle(original, self.admit(original))
                path, saved_bytes = original.path, original.path.read_bytes()
                original.close()
                with self.assertRaises(legacy.PilotBlocked) as caught:
                    self.gate(manifest=explicit if first_default else default)
                self.assertEqual(caught.exception.code, "ledger_identity_mismatch")
                self.assertEqual(path.read_bytes(), saved_bytes)

    def test_default_scope_retains_money_token_and_elapsed_time_caps(self):
        rates, limits = legacy.tariff(), replace(legacy.PilotLimits(), hard_cap_usd="0.001")
        gate = self.default_gate(rates=rates, limits=limits, approval_id="default-money-cap")
        self.settle(gate, self.admit(gate))
        with self.assertRaises(legacy.PilotBlocked) as caught:
            self.admit(gate, endpoint=legacy.GENERATION_ENDPOINT)
        self.assertEqual(caught.exception.code, "money_cap")
        self.assertEqual(len(gate.ledger["requests"]), 1)
        limits = replace(legacy.PilotLimits(), cumulative_input_tokens=16384)
        limited = self.default_gate(rates=rates, limits=limits, approval_id="default-token-cap")
        self.settle(limited, self.admit(limited))
        self.settle(limited, self.admit(limited, endpoint=legacy.GENERATION_ENDPOINT), out=9)
        self.settle(limited, self.admit(limited, "case-beta"))
        with self.assertRaises(legacy.PilotBlocked) as caught:
            self.admit(limited, "case-beta", legacy.GENERATION_ENDPOINT)
        self.assertEqual(caught.exception.code, "cumulative_token_limit")
        timed = self.default_gate(approval_id="default-time-cap")
        with self.assertRaises(legacy.PilotBlocked) as caught:
            self.settle(timed, self.admit(timed), elapsed_seconds=46)
        self.assertEqual(caught.exception.code, "call_time_limit")
        self.assertEqual(timed.ledger["requests"][0]["actual_cost_usd"], "0.001")


class DefaultScopeTransportTests(DefaultScopeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_actual_adapter_uses_no_routing_headers_and_preserves_two_item_budget_and_costs(self):
        gate = self.default_gate()
        def handler(endpoint, payload):
            received = legacy.FakeResponse(legacy.response(endpoint, payload))
            received.headers["openai-organization"] = "synthetic-observed-default-organization"
            return received
        wrapper, session, factory = self.default_adapter(gate, handler)
        for item in ("case-alpha", "case-beta"):
            wrapper.item_id = item
            result = await self.invoke(wrapper)
            self.assertEqual((result.input_tokens, result.output_tokens), (40, 9))
        self.assertEqual([row["endpoint"] for row in session.requests],
                         [legacy.COUNT_ENDPOINT, legacy.GENERATION_ENDPOINT] * 2)
        self.assertTrue(all(not any(name.lower() in {"openai-organization", "openai-project"}
            for name in row["header_names"]) for row in session.requests))
        rows = gate.ledger["requests"]
        self.assertEqual([row["actual_cost_usd"] for row in rows], ["0.001", "0.000152"] * 2)
        self.assertTrue(all(row["provider_invoice_verified"] is False for row in rows))
        for offset in (0, 2):
            self.assertEqual(rows[offset]["prompt_sha256"], rows[offset + 1]["prompt_sha256"])
        wrapper.item_id = "case-third"
        with self.assertRaises(legacy.PilotBlocked):
            await self.invoke(wrapper)
        self.assertEqual(len(session.requests), 4)
        factory.assert_called_once()
        gate.close()
        reopened = self.default_gate(manifest=gate.approval)
        with self.assertRaises(legacy.PilotBlocked) as caught:
            self.admit(reopened, "case-third")
        self.assertEqual(caught.exception.code, "request_limit")

    async def test_default_scope_rejects_any_caller_routing_header_before_admission_or_http(self):
        for index, (name, value) in enumerate([
                ("OpenAI-Organization", "synthetic-account"), ("openai-project", "synthetic-project"),
                ("OPENAI-ORGANIZATION", ""), ("OpenAI-Project", None)]):
            with self.subTest(header=name, value=value):
                gate = self.default_gate(approval_id="default-injected-header-" + str(index))
                wrapper, session, factory = self.default_adapter(gate)
                token = legacy.live_transport._ACTIVE_REPLY.set(legacy.live_transport._ReplyContext(
                    "case-alpha", legacy.live_transport.time.monotonic() + 30, 2048))
                try:
                    with self.assertRaises(legacy.PilotBlocked):
                        wrapper.adapter._session.post(legacy.COUNT_ENDPOINT,
                            json=dict(model=legacy.MODEL, input=[], instructions="synthetic",
                                      reasoning={"effort": "medium"}, text={"verbosity": "high"}),
                            allow_redirects=False, headers={"Authorization": "Bearer " + legacy.KEY,
                                "X-Client-Request-Id": "synthetic-client", name: value})
                finally:
                    legacy.live_transport._ACTIVE_REPLY.reset(token)
                factory.assert_not_called()
                self.assertEqual(gate.ledger["stop_code"], "account_routing_conflict")
                self.assertEqual(gate.ledger["requests"], [])
                self.assertEqual(session.requests, [])

    async def test_default_scope_cannot_generate_from_a_different_counted_prompt(self):
        gate = self.default_gate()
        wrapper, session, factory = self.default_adapter(gate)
        token = legacy.live_transport._ACTIVE_REPLY.set(legacy.live_transport._ReplyContext(
            "case-alpha", legacy.live_transport.time.monotonic() + 30, 2048))
        common = dict(model=legacy.MODEL, input=[], instructions="synthetic",
                      reasoning={"effort": "medium"}, text={"verbosity": "high"})
        headers = {"Authorization": "Bearer " + legacy.KEY, "X-Client-Request-Id": "synthetic-client"}
        try:
            async with wrapper.adapter._session.post(legacy.COUNT_ENDPOINT,
                    json=common, headers=headers, allow_redirects=False) as received:
                async for unused in received.content.iter_chunked(128):
                    pass
            changed = dict(common, instructions="changed after counted prompt",
                max_output_tokens=2048, store=False, stream=False, truncation="disabled", tools=[])
            with self.assertRaises(legacy.PilotBlocked) as caught:
                wrapper.adapter._session.post(legacy.GENERATION_ENDPOINT,
                    json=changed, headers=headers, allow_redirects=False)
            self.assertEqual(caught.exception.code, "count_prompt_mismatch")
        finally:
            legacy.live_transport._ACTIVE_REPLY.reset(token)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(len(gate.ledger["requests"]), 1)
        self.assertEqual(gate.ledger["requests"][0]["actual_cost_usd"], "0.001")

    async def test_mutating_default_scope_and_rehashing_descriptor_cannot_change_bound_routing(self):
        gate = self.default_gate()
        wrapper, session, factory = self.default_adapter(gate)
        gate.approval.update(account_scope=EXPLICIT_SCOPE,
                             account_id="synthetic-account", project_id="synthetic-project")
        with self.assertRaises(legacy.PilotBlocked):
            legacy.live_transport.preflight_live_transport(wrapper.adapter.config, gate,
                explicit_live=True, validated_descriptor=self.descriptor(gate))
        with self.assertRaises(legacy.PilotBlocked):
            await self.invoke(wrapper)
        factory.assert_not_called()
        self.assertEqual(gate.ledger["requests"], [])
        self.assertEqual(session.requests, [])

    async def test_default_scope_unknown_usage_stops_without_retry_and_remains_blocked_after_reopen(self):
        for failed_endpoint in (legacy.COUNT_ENDPOINT, legacy.GENERATION_ENDPOINT):
            with self.subTest(endpoint=failed_endpoint):
                gate = self.default_gate(approval_id="default-unknown-" +
                    ("count" if failed_endpoint == legacy.COUNT_ENDPOINT else "generation"))
                def handler(endpoint, payload):
                    data = legacy.response(endpoint, payload)
                    if endpoint == failed_endpoint:
                        del data["input_tokens" if endpoint == legacy.COUNT_ENDPOINT else "usage"]
                    return data
                wrapper, session, factory = self.default_adapter(gate, handler)
                with self.assertRaises(Exception):
                    await self.invoke(wrapper)
                self.assertEqual(gate.ledger["stop_code"], "usage_unknown")
                before = len(session.requests)
                wrapper.item_id = "case-beta"
                with self.assertRaises(legacy.PilotBlocked):
                    await self.invoke(wrapper)
                self.assertEqual(len(session.requests), before)
                if failed_endpoint == legacy.GENERATION_ENDPOINT:
                    self.assertEqual(gate.ledger["requests"][0]["actual_cost_usd"], "0.001")
                gate.close()
                reopened = self.default_gate(manifest=gate.approval)
                with self.assertRaises(legacy.PilotBlocked):
                    self.admit(reopened, "case-beta")
                self.assertEqual(len(reopened.ledger["requests"]), before)


class DefaultScopeRuntimeTests(DefaultScopeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_actual_root_entry_omits_account_and_cannot_refresh_budget_with_another_output(self):
        args, manifest = self.root_inputs()
        self.assertIsNone(args.account)
        valid_tariff = args.tariff.read_bytes()
        invalid_tariff = json.loads(valid_tariff)
        invalid_tariff.update(count_input_per_million="100", count_billing_basis="verified_charged_input_tokens")
        args.tariff.write_text(json.dumps(invalid_tariff), encoding="utf-8")
        reader = unittest.mock.Mock(side_effect=AssertionError("No key with unbounded count fees"))
        try:
            with patch.object(legacy.manual_live, "RUN_ROOT", self.root / "run-root"), \
                 patch.object(legacy.manual_runtime, "load_openai_credential", reader):
                blocked = await legacy.manual_live.run_live(args)
        finally:
            args.tariff.write_bytes(valid_tariff)
        self.assertEqual(blocked["stop_code"], "unverified_tariff")
        reader.assert_not_called()
        self.assertFalse((self.root / "approval-ledgers").exists())
        cfg = legacy.config(self.root / "default-unused-production")
        original_prepare = legacy.manual_runtime.prepare_context
        original_attach = legacy.live_transport.attach_live_transport
        sessions, descriptors = [], []
        def prepared(path, channel, account, items, **kwargs):
            descriptors.append(account)
            kwargs.update(config_loader=lambda unused: cfg, path_validator=lambda value: value,
                lease_factory=lambda unused: unittest.mock.Mock(), id_validator=lambda value: value,
                source_digest_reader=lambda unused: manifest["config_source_sha256"])
            return original_prepare(path, channel, account, items, **kwargs)
        def attached(adapter, gate, **kwargs):
            session = DefaultScopeSession(gate)
            sessions.append(session)
            kwargs["session_factory"] = lambda: session
            return original_attach(adapter, gate, **kwargs)
        with patch.object(legacy.manual_live, "RUN_ROOT", self.root / "run-root"), \
             patch.object(legacy.manual_runtime, "prepare_context", side_effect=prepared), \
             patch.object(legacy.manual_runtime, "load_openai_credential", return_value=legacy.KEY), \
             patch.object(legacy.manual_runtime, "load_discord_credential", side_effect=AssertionError("No token")), \
             patch.object(legacy.live_transport, "attach_live_transport", side_effect=attached), \
             patch("indeces.knowledge.KnowledgeService.__init__", side_effect=AssertionError("No knowledge worker")), \
             patch("indeces.discord_bridge.DiscordBridge.__init__", side_effect=AssertionError("No Gateway")), \
             patch("indeces.console.serve", side_effect=AssertionError("No Console")), \
             redirect_stdout(io.StringIO()):
            result = await legacy.manual_live.run_live(args)
        self.assertEqual(result["status"], "completed", result.get("stop_code"))
        self.assertEqual(result["request_count"], 4)
        self.assertEqual(descriptors, [DEFAULT_SCOPE])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].requests), 4)
        self.assertFalse(result["production_store_opened"])
        self.assertIn("not_provider_verified", result["account_binding"])
        self.assertIn("default", result["account_binding"])
        args.output = self.root / "run-root" / "two"
        reader = unittest.mock.Mock(side_effect=AssertionError("No key after quota exhaustion"))
        with patch.object(legacy.manual_live, "RUN_ROOT", self.root / "run-root"), \
             patch.object(legacy.manual_runtime, "prepare_context", side_effect=prepared), \
             patch.object(legacy.manual_runtime, "load_openai_credential", reader), \
             patch.object(legacy.live_transport, "attach_live_transport", side_effect=AssertionError("No next HTTP")), \
             redirect_stdout(io.StringIO()):
            blocked = await legacy.manual_live.run_live(args)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["stop_code"], "request_limit")
        self.assertEqual(blocked["ledger_path"], result["ledger_path"])
        self.assertEqual(blocked["request_count"], 4)
        reader.assert_not_called()
        for supplied_account in ("synthetic-account", "", DEFAULT_SCOPE):
            args.account = supplied_account
            with self.subTest(account=supplied_account), self.assertRaises(legacy.PilotBlocked):
                legacy.manual_live.metadata(args)
        EVIDENCE["runtime_acceptances"].append({"path": "credential_default_root_mock",
            "delivered": 2, "fake_count": 2, "fake_generation": 2, "real_requests": 0})

    async def test_default_scope_owned_lease_blocks_before_credentials_or_http(self):
        args, manifest = self.root_inputs(output="owned")
        cfg = legacy.config(self.root / "default-owned-unused-production")
        original_prepare = legacy.manual_runtime.prepare_context
        descriptors = []
        def prepared(path, channel, account, items, **kwargs):
            descriptors.append(account)
            kwargs.update(config_loader=lambda unused: cfg, path_validator=lambda value: value,
                lease_factory=unittest.mock.Mock(side_effect=RuntimeError("owned")),
                id_validator=lambda value: value,
                source_digest_reader=lambda unused: manifest["config_source_sha256"])
            return original_prepare(path, channel, account, items, **kwargs)
        reader = unittest.mock.Mock(side_effect=AssertionError("No key when original lease is owned"))
        with patch.object(legacy.manual_live, "RUN_ROOT", self.root / "run-root"), \
             patch.object(legacy.manual_runtime, "prepare_context", side_effect=prepared), \
             patch.object(legacy.manual_runtime, "load_openai_credential", reader), \
             patch.object(legacy.live_transport, "attach_live_transport", side_effect=AssertionError("No HTTP")), \
             redirect_stdout(io.StringIO()):
            blocked = await legacy.manual_live.run_live(args)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["stop_code"], "manual_state_owned_no_takeover")
        self.assertEqual(blocked["request_count"], 0)
        self.assertEqual(descriptors, [DEFAULT_SCOPE])
        reader.assert_not_called()
