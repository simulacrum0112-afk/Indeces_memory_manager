"""Manual live-entry contract exercised only with synthetic data and fake HTTP."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = (PROJECT_ROOT / "indeces" / "manual_trial"
        if (PROJECT_ROOT / "indeces" / "manual_trial" / "pilot_gate.py").is_file() else PROJECT_ROOT)
CANDIDATE = PROJECT_ROOT if ROOT != PROJECT_ROOT else ROOT / "checkpoint-repo"
sys.path.insert(0, str(CANDIDATE))
if ROOT != PROJECT_ROOT:
    from indeces.manual_trial import pilot_gate, live_transport, manual_runtime, manual_live, pilot_launcher
else:
    import pilot_gate
    import live_transport
    import manual_runtime
    import manual_live
    import pilot_launcher
COUNT_ENDPOINT, GENERATION_ENDPOINT, MODEL = pilot_gate.COUNT_ENDPOINT, pilot_gate.GENERATION_ENDPOINT, pilot_gate.MODEL
PilotBlocked, PilotGate, PilotLimits, Tariff = pilot_gate.PilotBlocked, pilot_gate.PilotGate, pilot_gate.PilotLimits, pilot_gate.Tariff
canonical_hash = pilot_gate.canonical_hash
attach_live_transport = live_transport.attach_live_transport
candidate_identity, synthetic_inputs = pilot_launcher.candidate_identity, pilot_launcher.synthetic_inputs
from indeces.adapter import OpenAIAdapter
from indeces.config import (AdapterConfig, Budget, Config, DiscordConfig,
                           KnowledgeConfig, RuntimeConfig)
from indeces.contracts import DeliveryReceipt, GovernedError, IncomingMessage
from indeces.memory import MemoryGraph
from indeces.run_records import verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog
from indeces.selection_policy import RankedTopThreeSelector
from indeces.store import Store

EVIDENCE = {"fake_http_count_requests": 0, "fake_http_generation_requests": 0,
            "real_http_requests": 0, "network_attempts_blocked": 0,
            "credential_reads": 0, "default_real_ledger_writes": 0,
            "runtime_acceptances": []}
SHA = hashlib.sha256(b"synthetic, nonprivate test identity").hexdigest()
KEY = "synthetic-noncredential-placeholder"
BUNDLE = {"items": [{"id": "case-alpha", "text": "alpha evidence?"},
                     {"id": "case-beta", "text": "beta evidence?"}]}


def deny_network(*unused, **kwargs):
    EVIDENCE["network_attempts_blocked"] += 1
    raise AssertionError("Network disabled: synthetic manual acceptance only")


def tariff(**overrides):
    values = dict(model=MODEL, generation_input_per_million="2",
                  generation_output_per_million="8", generation_flat="0",
                  count_input_per_million="0", count_flat="0.001",
                  count_billing_basis="verified_flat_fee",
                  provenance={"kind": "trusted_provider", "source": "synthetic_fixture_not_real_tariff",
                              "verified": True})
    values.update(overrides)
    return Tariff(**values)


def approval(rates, limits, **overrides):
    value = dict(approval_id="synthetic-approval", status="approved", project="Indeces",
                 account_id="synthetic-account", project_id="synthetic-project",
                 guild_id="10", channel_id="20", config_path=str(Path(r"D:\Indeces").resolve() / "synthetic-config.toml"),
                 config_source_sha256=SHA, model=MODEL, owner_evidence="synthetic approval fixture only",
                 allowed_stages=["reply"], allowed_response_models=[MODEL],
                 account_binding_confirmed=True, background_knowledge_disabled=True, delivery_mode="api",
                 input_bundle_sha256=SHA, candidate_identity=SHA, limits=asdict(limits),
                 tariff_sha256=canonical_hash(asdict(rates)),
                 expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    value.update(overrides)
    return value


def config(root):
    return Config("Indeces", root / "state", root / "scratch", root / "knowledge",
                  DiscordConfig("10", ("20",), delivery_seconds=2),
                  replace(RuntimeConfig(), turn_seconds=60),
                  AdapterConfig(MODEL, "https://api.openai.com/v1", {
                      "reply": Budget(16384, 2048, 45, "medium"),
                      "summary": Budget(8192, 1024, 1, "medium"),
                      "label": Budget(4096, 512, 1, "medium")}), KnowledgeConfig())


def message(text="alpha evidence?", ident="synthetic-message", **overrides):
    data = dict(message_id=ident, channel_id="20", guild_id="10", author_id="30",
                author_name="Synthetic operator", text=text,
                created_at=datetime.now(timezone.utc).isoformat(), raw_text="<@99> " + text,
                author_is_bot=False)
    data.update(overrides)
    return IncomingMessage(**data)


def response(endpoint, payload):
    if endpoint == COUNT_ENDPOINT:
        # Official count shape need not declare a model.
        return {"input_tokens": 40}
    return {"model": MODEL, "id": "synthetic-response", "status": "completed",
            "usage": {"input_tokens": 40, "output_tokens": 9},
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Synthetic evidence [M1]."}]}]}


class FakeResponse:
    def __init__(self, body, status=200, error=None):
        self.body, self.status, self.error = body, status, error
        self.headers = {"x-request-id": "synthetic-provider-request"}
        self.content = self

    async def iter_chunked(self, size):
        if self.error:
            raise self.error
        body = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        for index in range(0, len(body), size):
            yield body[index:index + size]


class FakeResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *unused):
        return False


class FakeSession:
    def __init__(self, gate, handler=response):
        self.gate, self.handler = gate, handler
        self.requests, self.closed = [], False

    def post(self, endpoint, **options):
        # The durable reservation must exist before constructing the fake request.
        disk = json.loads(self.gate.path.read_text(encoding="utf-8"))
        assert disk["requests"][-1]["status"] == "pending"
        assert disk["requests"][-1]["endpoint"] == endpoint
        assert options["allow_redirects"] is False
        assert options["headers"]["OpenAI-Organization"] == self.gate.approval["account_id"]
        assert options["headers"]["OpenAI-Project"] == self.gate.approval["project_id"]
        payload = options["json"]
        assert canonical_hash(payload) == disk["requests"][-1]["payload_sha256"]
        self.requests.append({"endpoint": endpoint, "payload": payload})
        key = "fake_http_count_requests" if endpoint == COUNT_ENDPOINT else "fake_http_generation_requests"
        EVIDENCE[key] += 1
        value = self.handler(endpoint, payload)
        return FakeResponseContext(value if isinstance(value, FakeResponse) else FakeResponse(value))

    async def close(self):
        self.closed = True


class Fixture:
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="indeces-manual-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.original_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.original_cwd)
        if os.name != "nt":
            # On POSIX the Windows boundary is a relative basename. Resolve
            # it within this fixture's temp cwd, without changing the gate.
            os.chdir(self.root)
        self.config_file = Path(r"D:\Indeces").resolve() / ("synthetic-config-" + self.root.name + ".toml")
        self.config_source_file = self.root / "synthetic-config-source.toml"
        self.config_source_file.write_text("# Synthetic fixture; config readers are injected.\n", encoding="utf-8")
        original_read_bytes = Path.read_bytes

        def fixture_read_bytes(path):
            if path == self.config_file:
                return original_read_bytes(self.config_source_file)
            return original_read_bytes(path)

        self.gates, self.stores, self.scratches, self.contexts = [], [], [], []
        self.serial = 0
        self.guards = [patch.object(Path, "read_bytes", fixture_read_bytes),
                       patch("socket.create_connection", deny_network),
                       patch.object(socket.socket, "connect", deny_network),
                       patch.object(socket.socket, "connect_ex", deny_network),
                       patch.object(pilot_gate, "LIVE_LEDGER_ROOT", self.root / "approval-ledgers"),
                       patch("indeces.config.load_config", side_effect=AssertionError("No private configuration")),
                       patch("indeces.credentials.load_openai_key", side_effect=AssertionError("No stored key")),
                       patch("indeces.credentials.load_discord_token", side_effect=AssertionError("No stored token")),
                       patch("indeces.credentials.prompt_secret", side_effect=AssertionError("No private prompt"))]
        for guard in self.guards:
            guard.start()
            self.addCleanup(guard.stop)

    def tearDown(self):
        for context in self.contexts:
            context.close()
        for scratch in self.scratches:
            scratch.close()
        for store in self.stores:
            store.close()
        for gate in self.gates:
            gate.close()
        for guard in reversed(self.guards):
            guard.stop()
        os.chdir(self.original_cwd)
        self.temp.cleanup()

    def gate(self, *, rates=None, limits=None, manifest=None, **kwargs):
        rates, limits = rates or tariff(), limits or PilotLimits()
        manifest = manifest or approval(rates, limits)
        gate = PilotGate(mode="live", tariff=rates, limits=limits, approval=manifest,
                         input_bundle_sha256=SHA, candidate_identity=SHA, live_enabled=True, **kwargs)
        self.gates.append(gate)
        return gate

    def descriptor(self, gate):
        return dict(model=MODEL, base_url="https://api.openai.com/v1", allowed_response_models=[MODEL],
                    descriptor_sha256=canonical_hash(gate.approval),
                    gate_identity_sha256=gate.ledger["identity_sha256"])

    def admit(self, gate, item="case-alpha", endpoint=COUNT_ENDPOINT, **kwargs):
        return gate.admit(item, "reply", endpoint, SHA, prompt_sha256=SHA,
                          max_output_tokens=2048 if endpoint == GENERATION_ENDPOINT else None,
                          timeout_seconds=45, **kwargs)

    def settle(self, gate, token, inp=40, out=0, **kwargs):
        row = next(row for row in gate.ledger["requests"] if row["reservation_id"] == token)
        gate.settle(token, status=kwargs.pop("status", "completed"),
                    usage={"input_tokens": inp, "output_tokens": out},
                    billing={"status": "known", "verified": True, "source": "synthetic-calculation",
                             "amount_usd": format(gate.tariff.cost(row["kind"], inp, out), "f")}, **kwargs)

    def adapter(self, gate, handler=response, **kwargs):
        self.serial += 1
        cfg = config(self.root / ("adapter-" + str(self.serial)))
        scratch = ScratchLog(cfg.scratch_dir)
        self.scratches.append(scratch)
        normal = OpenAIAdapter(cfg.adapter, scratch, api_key=KEY)
        session = FakeSession(gate, handler)
        factory = unittest.mock.Mock(return_value=session)
        wrapper = attach_live_transport(normal, gate, explicit_live=True,
                    validated_descriptor=self.descriptor(gate), session_factory=factory, **kwargs)
        wrapper.item_id = "case-alpha"
        self.addAsyncCleanup(wrapper.close)
        return wrapper, session, factory

    def context(self, bundle=None, **kwargs):
        cfg = config(self.root / "production-shaped-unused")
        lease = unittest.mock.Mock()
        context = manual_runtime.prepare_context(self.config_file, "20", "synthetic-account",
                    bundle or BUNDLE, config_loader=lambda unused: cfg, path_validator=lambda value: value,
                    lease_factory=lambda unused: lease, id_validator=lambda value: value,
                    source_digest_reader=lambda unused: SHA,
                    network_preflight=kwargs.pop("network_preflight", lambda: {"ready": True}), **kwargs)
        self.contexts.append(context)
        return context

    def runtime_factory(self, wrapper):
        def build(item, context):
            cfg = config(self.root / item["id"])
            store = Store(cfg.state_dir)
            scratch = ScratchLog(cfg.scratch_dir)
            self.stores.append(store)
            self.scratches.append(scratch)
            graph = MemoryGraph(store.db, retrieval_policy=cfg.runtime.retrieval_policy)
            for index in range(6):
                text = ("alpha beta synthetic evidence " + str(index) + " ") * 10
                graph.add("10:knowledge", "synthetic-source-" + str(index), "synthetic-material",
                          [{"text": text[:400], "quote": text[:400], "marks": ["alpha", "beta"]}], 1)
            return Runtime(cfg, store, wrapper, scratch, selector=RankedTopThreeSelector())
        return build


class ManualGateTests(Fixture, unittest.TestCase):
    def test_live_default_and_invalid_metadata_do_not_create_root(self):
        rates, limits = tariff(), PilotLimits()
        self.assertFalse(pilot_gate.LIVE_ENABLED)
        for manifest, code in [(None, "approval_missing_or_mismatched"),
                               (approval(rates, limits), "live_disabled"),
                               (approval(rates, limits, expires_at="2000-01-01T00:00:00+00:00"), "approval_expired")]:
            with self.subTest(code=code), self.assertRaises(PilotBlocked) as caught:
                PilotGate(mode="live", tariff=rates, approval=manifest,
                          input_bundle_sha256=SHA, candidate_identity=SHA)
            self.assertEqual(caught.exception.code, code)
            self.assertFalse((self.root / "approval-ledgers").exists())

    def test_all_required_approval_bindings_fail_before_filesystem(self):
        rates, limits = tariff(), PilotLimits()
        required = approval(rates, limits)
        for field in required:
            value = dict(required)
            del value[field]
            with self.subTest(field=field), self.assertRaises(PilotBlocked):
                self.gate(manifest=value)
            self.assertFalse((self.root / "approval-ledgers").exists())

    def test_numeric_and_unverified_count_fees_are_closed(self):
        for value in (True, 0.2, Decimal("0.2"), "NaN", "Infinity", "-1", "0.0000000000001"):
            with self.subTest(value=repr(value)), self.assertRaises(PilotBlocked):
                self.gate(rates=tariff(generation_input_per_million=value),
                          manifest=approval(tariff(), PilotLimits()))
        with self.assertRaises(PilotBlocked) as caught:
            self.gate(rates=tariff(count_input_per_million="100", count_billing_basis="verified_charged_input_tokens"))
        self.assertEqual(caught.exception.code, "unverified_tariff")
        self.assertFalse((self.root / "approval-ledgers").exists())

    def test_fixed_approval_ledger_cannot_be_changed_by_output_path_or_simultaneous_owner(self):
        gate = self.gate()
        expected = hashlib.sha256(gate.approval["approval_id"].encode()).hexdigest() + ".json"
        self.assertEqual(gate.path.name, expected)
        with self.assertRaises(PilotBlocked) as caught:
            self.gate(path=self.root / "another-output" / "ledger.json")
        self.assertEqual(caught.exception.code, "live_ledger_path_forbidden")
        with self.assertRaises(PilotBlocked) as caught:
            self.gate(manifest=gate.approval)
        self.assertEqual(caught.exception.code, "ledger_locked")
        gate.close()
        reopened = self.gate(manifest=gate.approval)
        self.assertEqual(reopened.path, gate.path)
        reopened.close()
        changed = dict(gate.approval, account_id="other-synthetic-account")
        with self.assertRaises(PilotBlocked) as caught:
            self.gate(manifest=changed)
        self.assertEqual(caught.exception.code, "ledger_identity_mismatch")

    def test_two_items_have_exact_four_admissions_and_reopen_cannot_retry(self):
        gate = self.gate()
        for item in ("case-alpha", "case-beta"):
            self.settle(gate, self.admit(gate, item))
            self.settle(gate, self.admit(gate, item, GENERATION_ENDPOINT), out=9)
        self.assertEqual([row["kind"] for row in gate.ledger["requests"]], ["count", "generation"] * 2)
        gate.close()
        reopened = self.gate(manifest=gate.approval)
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(reopened, "case-third")
        self.assertEqual(caught.exception.code, "request_limit")
        self.assertEqual(len(reopened.ledger["requests"]), 4)

    def test_pending_reopen_stops_and_retains_reserved_cost(self):
        gate = self.gate()
        self.admit(gate)
        gate.close()
        reopened = self.gate(manifest=gate.approval)
        with self.assertRaises(PilotBlocked):
            self.admit(reopened, "case-beta")
        self.assertEqual(reopened.ledger["requests"][0]["reserved_usd"], "0.001")
        self.assertIsNone(reopened.ledger["requests"][0]["actual_cost_usd"])

    def test_cost_and_cumulative_token_reservation_happen_before_transport(self):
        gate = self.gate(limits=replace(PilotLimits(), hard_cap_usd="0.001"))
        self.settle(gate, self.admit(gate))
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate, endpoint=GENERATION_ENDPOINT)
        self.assertEqual(caught.exception.code, "money_cap")
        self.assertEqual(len(gate.ledger["requests"]), 1)
        gate.close()
        rates, limits = tariff(), replace(PilotLimits(), cumulative_input_tokens=16384)
        limited = self.gate(limits=limits, manifest=approval(rates, limits, approval_id="token-limited"))
        self.settle(limited, self.admit(limited))
        self.settle(limited, self.admit(limited, endpoint=GENERATION_ENDPOINT), out=9)
        self.settle(limited, self.admit(limited, "case-beta"))
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(limited, "case-beta", GENERATION_ENDPOINT)
        self.assertEqual(caught.exception.code, "cumulative_token_limit")

    def test_call_time_breach_and_wall_downtime_are_sticky(self):
        gate = self.gate()
        token = self.admit(gate)
        with self.assertRaises(PilotBlocked) as caught:
            self.settle(gate, token, elapsed_seconds=46)
        self.assertEqual(caught.exception.code, "call_time_limit")
        self.assertEqual(gate.ledger["requests"][0]["actual_cost_usd"], "0.001")
        gate.close()
        reopened = self.gate(manifest=gate.approval)
        with self.assertRaises(PilotBlocked):
            self.admit(reopened, "case-beta")
        rates, limits = tariff(), PilotLimits()
        second = self.gate(manifest=approval(rates, limits, approval_id="expired-window"))
        self.settle(second, self.admit(second))
        second.ledger["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        second._save()
        second.close()
        reopened = self.gate(manifest=second.approval)
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(reopened, "case-beta")
        self.assertEqual(caught.exception.code, "cumulative_time_limit")

    def test_approved_calculation_is_not_provider_invoice_and_ledger_contains_no_key_or_prompt(self):
        gate = self.gate()
        self.settle(gate, self.admit(gate))
        row = gate.ledger["requests"][0]
        self.assertEqual(row["cost_basis"], "approved_tariff_calculation")
        self.assertFalse(row["provider_invoice_verified"])
        text = gate.path.read_text(encoding="utf-8")
        self.assertNotIn(KEY, text)
        self.assertNotIn("alpha evidence?", text)

    def test_empty_halt_cannot_clear_unknown_or_reopen_it(self):
        gate = self.gate()
        token = self.admit(gate)
        with self.assertRaises(PilotBlocked):
            gate.settle(token, status="failed")
        self.assertEqual(gate.ledger["stop_code"], "usage_unknown")
        with self.assertRaises(PilotBlocked):
            gate.halt("")
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate, "case-beta")
        self.assertEqual(caught.exception.code, "usage_unknown")
        gate.close()
        reopened = self.gate(manifest=gate.approval)
        self.assertEqual(reopened.ledger["stop_code"], "usage_unknown")

    def test_expiry_after_response_keeps_known_usage_and_calculated_fee(self):
        rates, limits = tariff(), PilotLimits()
        expires = datetime.now(timezone.utc) + timedelta(seconds=5)
        gate = self.gate(manifest=approval(rates, limits, expires_at=expires.isoformat()))
        token = self.admit(gate)
        class AfterExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                return expires + timedelta(seconds=1)
        with patch.object(pilot_gate, "datetime", AfterExpiry), self.assertRaises(PilotBlocked) as caught:
            self.settle(gate, token)
        self.assertEqual(caught.exception.code, "approval_expired")
        row = gate.ledger["requests"][0]
        self.assertEqual(row["usage"], {"input_tokens": 40, "output_tokens": 0})
        self.assertEqual(row["actual_cost_usd"], "0.001")
        self.assertEqual(json.loads(gate.path.read_text())["requests"][0]["actual_cost_usd"], "0.001")


class ManualTransportTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def invoke(self, wrapper, **kwargs):
        return await wrapper.call("reply", "synthetic instructions", [{"role": "user", "content": "synthetic prompt"}],
                                  "synthetic-trace", **kwargs)

    async def test_default_attach_and_invalid_descriptor_or_key_never_construct_session(self):
        gate = self.gate()
        cfg = config(self.root / "unused")
        scratch = ScratchLog(cfg.scratch_dir)
        self.scratches.append(scratch)
        factory = unittest.mock.Mock(side_effect=AssertionError("factory must stay lazy"))
        normal = OpenAIAdapter(cfg.adapter, scratch, api_key=KEY)
        with self.assertRaises(PilotBlocked) as caught:
            attach_live_transport(normal, gate, session_factory=factory)
        self.assertEqual(caught.exception.code, "live_transport_disabled")
        descriptor = self.descriptor(gate)
        for key in descriptor:
            bad = dict(descriptor)
            del bad[key]
            with self.subTest(key=key), self.assertRaises(PilotBlocked):
                attach_live_transport(normal, gate, explicit_live=True, validated_descriptor=bad, session_factory=factory)
        invalid = OpenAIAdapter(cfg.adapter, scratch, api_key="")
        with self.assertRaises(PilotBlocked):
            attach_live_transport(invalid, gate, explicit_live=True, validated_descriptor=descriptor, session_factory=factory)
        factory.assert_not_called()
        self.assertEqual(gate.ledger["requests"], [])

    async def test_key_free_preflight_rejects_stopped_gate_without_credential_lookup(self):
        gate = self.gate()
        cfg = config(self.root / "preflight-only")
        with patch("indeces.credentials.validate_api_key", side_effect=AssertionError("No key needed")):
            ready = live_transport.preflight_live_transport(cfg.adapter, gate, explicit_live=True,
                                                            validated_descriptor=self.descriptor(gate))
            self.assertTrue(ready["ready"])
            with self.assertRaises(PilotBlocked):
                live_transport.preflight_live_transport(cfg.adapter, gate)
            with self.assertRaises(PilotBlocked):
                gate.halt("synthetic_stop")
            with self.assertRaises(PilotBlocked) as caught:
                live_transport.preflight_live_transport(cfg.adapter, gate, explicit_live=True,
                                                        validated_descriptor=self.descriptor(gate))
            self.assertEqual(caught.exception.code, "synthetic_stop")
        self.assertEqual(gate.ledger["requests"], [])

    async def test_delayed_approval_expiry_blocks_credentials_and_session_before_request(self):
        rates, limits = tariff(), PilotLimits()
        expires = datetime.now(timezone.utc) + timedelta(seconds=5)
        gate = self.gate(manifest=approval(rates, limits, expires_at=expires.isoformat()))
        wrapper, session, factory = self.adapter(gate)
        context = self.context(network_preflight=wrapper.require_ready_for_network)
        reader, prompt = unittest.mock.Mock(), unittest.mock.Mock()
        class AfterExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                return expires + timedelta(seconds=1)
        with patch.object(pilot_gate, "datetime", AfterExpiry), self.assertRaises(PilotBlocked) as caught:
            manual_runtime.load_openai_credential(context, environment={"OPENAI_API_KEY": KEY},
                stored_loader=reader, private_prompt=prompt, validator=lambda value: value)
        self.assertEqual(caught.exception.code, "approval_expired")
        reader.assert_not_called()
        prompt.assert_not_called()
        factory.assert_not_called()
        self.assertEqual(session.requests, [])
        self.assertEqual(gate.ledger["requests"], [])

    async def test_conflicting_account_routing_is_rejected_before_admission_or_http(self):
        gate = self.gate()
        wrapper, session, factory = self.adapter(gate)
        token = live_transport._ACTIVE_REPLY.set(live_transport._ReplyContext("case-alpha", live_transport.time.monotonic() + 30, 2048))
        common = dict(model=MODEL, input=[], instructions="synthetic", reasoning={"effort": "medium"},
                      text={"verbosity": "high"})
        try:
            with self.assertRaises(PilotBlocked):
                wrapper.adapter._session.post(COUNT_ENDPOINT, json=common, allow_redirects=False,
                    headers={"Authorization": "Bearer " + KEY, "X-Client-Request-Id": "synthetic-client",
                             "OpenAI-Organization": "other-synthetic-account"})
        finally:
            live_transport._ACTIVE_REPLY.reset(token)
        factory.assert_not_called()
        self.assertEqual(gate.ledger["requests"], [])
        self.assertEqual(session.requests, [])

    async def test_mutable_approval_cannot_change_bound_account_before_http(self):
        gate = self.gate()
        wrapper, session, factory = self.adapter(gate)
        gate.approval["account_id"] = "mutated-synthetic-account"
        # Even rehashing caller metadata cannot replace the original ledger binding.
        descriptor = self.descriptor(gate)
        with self.assertRaises(PilotBlocked):
            live_transport.preflight_live_transport(wrapper.adapter.config, gate, explicit_live=True,
                                                    validated_descriptor=descriptor)
        with self.assertRaises(PilotBlocked):
            await self.invoke(wrapper)
        factory.assert_not_called()
        self.assertEqual(session.requests, [])
        self.assertEqual(gate.ledger["requests"], [])

    async def test_response_account_mismatch_keeps_known_usage_but_stops_unverified_cost(self):
        gate = self.gate()
        def handler(endpoint, payload):
            received = FakeResponse(response(endpoint, payload))
            received.headers["openai-organization"] = "other-synthetic-account"
            return received
        wrapper, session, factory = self.adapter(gate, handler)
        with self.assertRaises(Exception):
            await self.invoke(wrapper)
        self.assertEqual(len(session.requests), 1)
        row = gate.ledger["requests"][0]
        self.assertEqual(row["usage"], {"input_tokens": 40, "output_tokens": 0})
        self.assertIsNone(row["actual_cost_usd"])
        self.assertTrue(gate.ledger["stop_code"])
        records = [json.loads(line) for line in wrapper.adapter.scratch.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(record["event"] == "http_response" for record in records), 1)

    async def test_actual_adapter_normal_http_count_and_generation_have_durable_fee_binding(self):
        gate = self.gate()
        wrapper, session, factory = self.adapter(gate)
        factory.assert_not_called()
        result = await self.invoke(wrapper)
        self.assertEqual(result.text, "Synthetic evidence [M1].")
        self.assertEqual((result.input_tokens, result.output_tokens), (40, 9))
        self.assertEqual([request["endpoint"] for request in session.requests], [COUNT_ENDPOINT, GENERATION_ENDPOINT])
        factory.assert_called_once()
        rows = gate.ledger["requests"]
        self.assertEqual(rows[0]["prompt_sha256"], rows[1]["prompt_sha256"])
        self.assertEqual([row["status"] for row in rows], ["completed", "completed"])
        self.assertEqual([row["actual_cost_usd"] for row in rows], ["0.001", "0.000152"])
        self.assertTrue(all(row["provider_invoice_verified"] is False for row in rows))
        self.assertEqual(set(session.requests[1]["payload"]) - set(session.requests[0]["payload"]),
                         {"max_output_tokens", "store", "stream", "truncation", "tools"})

    async def test_label_and_summary_are_rejected_before_inner_session(self):
        for stage in ("label", "summary"):
            rates, limits = tariff(), PilotLimits()
            gate = self.gate(manifest=approval(rates, limits, approval_id="forbidden-" + stage))
            wrapper, session, factory = self.adapter(gate)
            with self.assertRaises(PilotBlocked):
                await wrapper.call(stage, "synthetic", [], "trace")
            factory.assert_not_called()
            self.assertEqual(session.requests, [])
            self.assertEqual(gate.ledger["stop_code"], "stage_forbidden")

    async def test_changed_counted_schema_is_rejected_before_second_post(self):
        gate = self.gate()
        wrapper, session, factory = self.adapter(gate)
        binding = live_transport._ReplyContext("case-alpha", live_transport.time.monotonic() + 30, 2048)
        context_token = live_transport._ACTIVE_REPLY.set(binding)
        common = dict(model=MODEL, input=[], instructions="synthetic", reasoning={"effort": "medium"},
                      text={"verbosity": "high"})
        headers = {"Authorization": "Bearer " + KEY, "X-Client-Request-Id": "synthetic-client-request"}
        try:
            async with wrapper.adapter._session.post(COUNT_ENDPOINT, json=common, headers=headers, allow_redirects=False) as received:
                async for unused in received.content.iter_chunked(128):
                    pass
            changed = dict(common, text={"verbosity": "high", "format": {"type": "json_schema"}},
                           max_output_tokens=2048, store=False, stream=False, truncation="disabled", tools=[])
            with self.assertRaises(PilotBlocked) as caught:
                wrapper.adapter._session.post(GENERATION_ENDPOINT, json=changed, headers=headers, allow_redirects=False)
            self.assertEqual(caught.exception.code, "count_prompt_mismatch")
        finally:
            live_transport._ACTIVE_REPLY.reset(context_token)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(len(gate.ledger["requests"]), 1)

    async def test_admitted_payload_is_frozen_before_lazy_session_construction(self):
        gate = self.gate()
        wrapper, session, factory = self.adapter(gate)
        binding = live_transport._ReplyContext("case-alpha", live_transport.time.monotonic() + 30, 2048)
        token = live_transport._ACTIVE_REPLY.set(binding)
        payload = dict(model=MODEL, input=[{"role": "user", "content": "original synthetic"}],
                       instructions="synthetic", reasoning={"effort": "medium"}, text={"verbosity": "high"})
        headers = {"Authorization": "Bearer " + KEY, "X-Client-Request-Id": "synthetic-client-request"}
        try:
            request = wrapper.adapter._session.post(COUNT_ENDPOINT, json=payload, headers=headers, allow_redirects=False)
            factory.assert_not_called()
            payload["input"][0]["content"] = "mutated after durable admission"
            headers["Authorization"] = "mutated synthetic header"
            async with request as received:
                async for unused in received.content.iter_chunked(128):
                    pass
        finally:
            live_transport._ACTIVE_REPLY.reset(token)
        self.assertEqual(session.requests[0]["payload"]["input"][0]["content"], "original synthetic")
        self.assertEqual(gate.ledger["requests"][0]["status"], "completed")

    async def test_endpoint_redirect_and_extra_tool_payload_are_blocked_before_post(self):
        for name in ("endpoint", "redirect", "tools"):
            rates, limits = tariff(), PilotLimits()
            gate = self.gate(manifest=approval(rates, limits, approval_id="bad-" + name))
            wrapper, session, factory = self.adapter(gate)
            token = live_transport._ACTIVE_REPLY.set(live_transport._ReplyContext("case-alpha", live_transport.time.monotonic() + 30, 2048))
            payload = dict(model=MODEL, instructions="synthetic", input=[], reasoning={"effort": "medium"}, text={"verbosity": "high"})
            if name == "tools":
                payload["tools"] = [{"type": "web_search"}]
            try:
                with self.assertRaises(PilotBlocked):
                    wrapper.adapter._session.post("https://unapproved.invalid/" if name == "endpoint" else COUNT_ENDPOINT,
                                                  json=payload, allow_redirects=name == "redirect")
            finally:
                live_transport._ACTIVE_REPLY.reset(token)
            self.assertEqual(session.requests, [])
            factory.assert_not_called()

    async def test_wrong_model_unknown_usage_or_billing_stop_with_no_retry(self):
        for case in ("model", "usage", "billing"):
            rates, limits = tariff(), PilotLimits()
            gate = self.gate(manifest=approval(rates, limits, approval_id="unknown-" + case))
            def handler(endpoint, payload):
                data = response(endpoint, payload)
                if endpoint == GENERATION_ENDPOINT:
                    if case == "model":
                        data["model"] = "unapproved-model"
                    elif case == "usage":
                        del data["usage"]
                return data
            kwargs = {"billing_resolver": lambda *unused: None} if case == "billing" else {}
            wrapper, session, factory = self.adapter(gate, handler, **kwargs)
            with self.assertRaises(Exception):
                await self.invoke(wrapper)
            self.assertIn(gate.ledger["stop_code"], {"billing_unknown", "usage_unknown"})
            records = [json.loads(line) for line in wrapper.adapter.scratch.path.read_text(encoding="utf-8").splitlines()]
            preserved = [row["fields"] for row in records if row["event"] == "http_response"]
            self.assertEqual(len(preserved), 1 if case == "billing" else 2)
            self.assertEqual(preserved[0]["payload"]["input_tokens"], 40)
            if case == "model":
                self.assertEqual(preserved[1]["payload"]["model"], "unapproved-model")
            elif case == "usage":
                self.assertNotIn("usage", preserved[1]["payload"])
            before = len(session.requests)
            wrapper.item_id = "case-beta"
            with self.assertRaises(Exception):
                await self.invoke(wrapper)
            self.assertEqual(len(session.requests), before)
            manifest = gate.approval
            gate.close()
            reopened = self.gate(manifest=manifest)
            with self.assertRaises(PilotBlocked):
                self.admit(reopened, "case-beta")

    async def test_http_error_invalid_body_and_body_overflow_preserve_unknown_ledger(self):
        for case in ("http", "json", "body"):
            rates, limits = tariff(), PilotLimits()
            gate = self.gate(manifest=approval(rates, limits, approval_id="body-" + case))
            def handler(endpoint, payload):
                return FakeResponse({"error": "synthetic"}, status=302) if case == "http" else (
                    FakeResponse(b"not-json") if case == "json" else FakeResponse(b"x" * (2 * 1024 * 1024 + 1)))
            wrapper, session, factory = self.adapter(gate, handler)
            with self.assertRaises(Exception):
                await self.invoke(wrapper)
            self.assertEqual(gate.ledger["stop_code"], "usage_unknown")
            self.assertEqual(gate.ledger["requests"][0]["status"], "unknown")
            self.assertEqual(len(session.requests), 1)

    async def test_cancel_after_known_count_retains_fee_and_stops_next_item(self):
        gate = self.gate()
        def handler(endpoint, payload):
            if endpoint == GENERATION_ENDPOINT:
                return FakeResponse(None, error=asyncio.CancelledError())
            return response(endpoint, payload)
        wrapper, session, factory = self.adapter(gate, handler)
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke(wrapper)
        self.assertEqual(gate.ledger["requests"][0]["status"], "completed")
        self.assertEqual(gate.ledger["requests"][0]["actual_cost_usd"], "0.001")
        self.assertTrue(gate.ledger["stop_code"])
        self.assertIsNone(live_transport._ACTIVE_REPLY.get())
        wrapper.item_id = "case-beta"
        with self.assertRaises(Exception):
            await self.invoke(wrapper)
        self.assertEqual(len(session.requests), 2)

    async def test_shorter_gate_deadline_cancels_body_before_native_stage_deadline(self):
        gate = self.gate()
        class HangingResponse(FakeResponse):
            async def iter_chunked(self, size):
                await asyncio.sleep(1)
                yield b"{}"
        wrapper, session, factory = self.adapter(gate, lambda *unused: HangingResponse({}))
        with patch.object(gate, "transport_timeout", return_value=0.01):
            with self.assertRaises(GovernedError) as caught:
                await self.invoke(wrapper)
            self.assertEqual(caught.exception.code, "stage_timeout")
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(gate.ledger["stop_code"], "usage_unknown")
        self.assertIsNone(gate.ledger["requests"][0]["actual_cost_usd"])


class ManualRuntimeTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_preflight_invalid_items_and_owned_lease_never_read_credentials(self):
        reader, lease = unittest.mock.Mock(), unittest.mock.Mock()
        with self.assertRaises(manual_runtime.ManualRuntimeError):
            manual_runtime.prepare_context(self.config_file, "20", "synthetic-account",
                {"items": BUNDLE["items"] + [{"id": "case-third", "text": "third"}]}, config_loader=reader,
                path_validator=lambda value: value, lease_factory=lease, id_validator=lambda value: value)
        reader.assert_not_called()
        lease.assert_not_called()
        cfg = config(self.root / "unused-target")
        with self.assertRaises(manual_runtime.ManualRuntimeError) as caught:
            manual_runtime.prepare_context(self.config_file, "20", "synthetic-account", BUNDLE,
                config_loader=lambda unused: cfg, path_validator=lambda value: value,
                lease_factory=unittest.mock.Mock(side_effect=RuntimeError("owned")), id_validator=lambda value: value,
                source_digest_reader=lambda unused: SHA)
        self.assertEqual(caught.exception.code, "manual_state_owned_no_takeover")

    async def test_credentials_are_only_explicit_injected_late_readers(self):
        context = self.context()
        stored, prompt = unittest.mock.Mock(), unittest.mock.Mock()
        key = manual_runtime.load_openai_credential(context, environment={"OPENAI_API_KEY": KEY},
                stored_loader=stored, private_prompt=prompt, validator=lambda value: value)
        self.assertEqual(key, KEY)
        stored.assert_not_called()
        prompt.assert_not_called()
        context.close()
        with self.assertRaises(manual_runtime.ManualRuntimeError):
            manual_runtime.load_openai_credential(context, environment={}, stored_loader=stored,
                                                  private_prompt=prompt, validator=lambda value: value)
        stored.assert_not_called()

    async def test_missing_preflight_and_changed_config_block_all_credential_readers(self):
        context = self.context(network_preflight=None)
        stored, prompt = unittest.mock.Mock(), unittest.mock.Mock()
        with self.assertRaises(manual_runtime.ManualRuntimeError) as caught:
            manual_runtime.load_openai_credential(context, environment={"OPENAI_API_KEY": KEY},
                stored_loader=stored, private_prompt=prompt, validator=lambda value: value)
        self.assertEqual(caught.exception.code, "manual_live_preflight_required")
        stored.assert_not_called()
        prompt.assert_not_called()
        context.network_preflight = lambda: {"ready": True}
        context.config_loader = lambda unused: replace(context.config, discord=DiscordConfig("11", ("20",)))
        with self.assertRaises(manual_runtime.ManualRuntimeError):
            manual_runtime.load_discord_credential(context, environment={}, stored_loader=stored,
                private_prompt=prompt, validator=lambda value: value)
        stored.assert_not_called()
        prompt.assert_not_called()

    async def test_source_hash_race_after_lease_closes_it_before_credentials(self):
        cfg = config(self.root / "race-unused-production")
        lease = unittest.mock.Mock()
        digest = unittest.mock.Mock(side_effect=[SHA, SHA, hashlib.sha256(b"changed").hexdigest()])
        with self.assertRaises(manual_runtime.ManualRuntimeError):
            manual_runtime.prepare_context(self.config_file, "20", "synthetic-account", BUNDLE,
                config_loader=lambda unused: cfg, path_validator=lambda value: value,
                lease_factory=lambda unused: lease, id_validator=lambda value: value,
                source_digest_reader=digest, network_preflight=lambda: {"ready": True})
        lease.close.assert_called_once()

    async def test_root_cli_missing_metadata_and_describe_only_never_read_credentials(self):
        path = self.root / "synthetic-inputs.json"
        bundle = synthetic_inputs()
        bundle["questions"][1]["question"] = "distinct synthetic question"
        path.write_text(json.dumps(bundle), encoding="utf-8")
        with patch.object(manual_runtime, "load_openai_credential", side_effect=AssertionError("No key read")), \
             patch.object(manual_runtime, "load_discord_credential", side_effect=AssertionError("No token read")), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(PilotBlocked) as caught:
                await manual_live.run_live(manual_live.parser().parse_args(["--live"]))
            self.assertEqual(caught.exception.code, "manual_required_metadata_missing")
            self.assertEqual(manual_live.main(["--describe-input", "--inputs", str(path),
                                              "--candidate-root", str(CANDIDATE)]), 0)
        self.assertFalse((self.root / "approval-ledgers").exists())

    async def test_root_live_orchestration_uses_only_fake_normal_http_and_approval_scoped_ledger(self):
        bundle = synthetic_inputs()
        bundle.update(kind="approved_frozen", owner_evidence="synthetic evaluator approval")
        bundle["questions"] = [{"item_id": row["id"], "question": row["text"]} for row in BUNDLE["items"]]
        cfg = config(self.root / "root-unused-production")
        rates, limits = tariff(), PilotLimits()
        manifest = approval(rates, limits, config_path=str(self.config_file),
            config_source_sha256=hashlib.sha256(self.config_file.read_bytes()).hexdigest(),
            candidate_identity=candidate_identity(CANDIDATE), input_bundle_sha256=canonical_hash(bundle),
            account_binding_confirmed=True, background_knowledge_disabled=True, delivery_mode="api")
        files = {}
        for name, data in (("inputs", bundle), ("approval", manifest), ("tariff", asdict(rates))):
            path = self.root / (name + ".json")
            path.write_text(json.dumps(data), encoding="utf-8")
            files[name] = path
        args = manual_live.parser().parse_args(["--live", "--delivery", "api", "--config", str(self.config_file),
            "--account", "synthetic-account", "--channel", "20", "--inputs", str(files["inputs"]),
            "--approval", str(files["approval"]), "--tariff", str(files["tariff"]),
            "--candidate-root", str(CANDIDATE), "--output", str(self.root / "run-root" / "one")])
        original_prepare, original_attach = manual_runtime.prepare_context, live_transport.attach_live_transport
        sessions = []
        def prepared(path, channel, account, items, **kwargs):
            kwargs.update(config_loader=lambda unused: cfg, path_validator=lambda value: value,
                          lease_factory=lambda unused: unittest.mock.Mock(), id_validator=lambda value: value,
                          source_digest_reader=lambda unused: manifest["config_source_sha256"])
            return original_prepare(path, channel, account, items, **kwargs)
        def attached(adapter, gate, **kwargs):
            session = FakeSession(gate)
            sessions.append(session)
            kwargs["session_factory"] = lambda: session
            return original_attach(adapter, gate, **kwargs)
        with patch.object(manual_live, "RUN_ROOT", self.root / "run-root"), \
             patch.object(manual_runtime, "prepare_context", side_effect=prepared), \
             patch.object(manual_runtime, "load_openai_credential", return_value=KEY), \
             patch.object(manual_runtime, "load_discord_credential", side_effect=AssertionError("API needs no Discord token")), \
             patch.object(live_transport, "attach_live_transport", side_effect=attached), \
             patch("indeces.knowledge.KnowledgeService.__init__", side_effect=AssertionError("No KnowledgeService")), \
             redirect_stdout(io.StringIO()):
            result = await manual_live.run_live(args)
        self.assertEqual(result["status"], "completed", result.get("stop_code"))
        self.assertEqual(result["request_count"], 4)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].requests), 4)
        self.assertFalse(result["existing_console_reused"])
        self.assertFalse(result["production_store_opened"])
        self.assertIn("approval-ledgers", result["ledger_path"])
        self.assertNotIn("run-root", result["ledger_path"])
        EVIDENCE["runtime_acceptances"].append({"path": "root_run_live_api_fake_session", "delivered": 2,
            "fake_count": 2, "fake_generation": 2, "real_requests": 0})
        # Choosing a new output directory cannot allocate a fresh approved budget.
        args.output = self.root / "run-root" / "two"
        key_reader = unittest.mock.Mock(side_effect=AssertionError("Exhausted approval must block before key"))
        with patch.object(manual_live, "RUN_ROOT", self.root / "run-root"), \
             patch.object(manual_runtime, "prepare_context", side_effect=prepared), \
             patch.object(manual_runtime, "load_openai_credential", key_reader), \
             patch.object(live_transport, "attach_live_transport", side_effect=AssertionError("No second session")), \
             redirect_stdout(io.StringIO()):
            blocked = await manual_live.run_live(args)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["stop_code"], "request_limit")
        self.assertEqual(blocked["ledger_path"], result["ledger_path"])
        self.assertEqual(blocked["request_count"], 4)
        key_reader.assert_not_called()

    async def test_root_owned_lease_refuses_before_key_or_http(self):
        bundle = synthetic_inputs()
        bundle.update(kind="approved_frozen", owner_evidence="synthetic frozen approval")
        bundle["questions"][1]["question"] = "distinct synthetic question"
        rates, limits = tariff(), PilotLimits()
        manifest = approval(rates, limits, config_path=str(self.config_file),
            config_source_sha256=hashlib.sha256(self.config_file.read_bytes()).hexdigest(),
            input_bundle_sha256=canonical_hash(bundle))
        args = SimpleNamespace(live=True, delivery="api", candidate_root=CANDIDATE,
                               output=self.root / "run-root" / "owned", channel="20", account="synthetic-account")
        cfg = config(self.root / "owned-unused-production")
        original_prepare = manual_runtime.prepare_context
        def prepared(path, channel, account, items, **kwargs):
            kwargs.update(config_loader=lambda unused: cfg, path_validator=lambda value: value,
                lease_factory=unittest.mock.Mock(side_effect=RuntimeError("owned")),
                id_validator=lambda value: value, source_digest_reader=lambda unused: manifest["config_source_sha256"])
            return original_prepare(path, channel, account, items, **kwargs)
        key_reader = unittest.mock.Mock(side_effect=AssertionError("No key read"))
        with patch.object(manual_live, "RUN_ROOT", self.root / "run-root"), \
             patch.object(manual_live, "metadata", return_value=(bundle, manifest, rates, limits, SHA, self.config_file)), \
             patch.object(manual_runtime, "prepare_context", side_effect=prepared), \
             patch.object(manual_runtime, "load_openai_credential", key_reader), \
             patch.object(live_transport, "attach_live_transport", side_effect=AssertionError("No HTTP attach")):
            blocked = await manual_live.run_live(args)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["stop_code"], "manual_state_owned_no_takeover")
        self.assertEqual(blocked["request_count"], 0)
        key_reader.assert_not_called()

    async def test_run_discord_closes_finite_fake_bridge_after_two_human_deliveries(self):
        context, gate = self.context(), self.gate()
        wrapper, session, factory = self.adapter(gate)
        bridges = []
        class FakeBridge:
            def __init__(self, cfg, facade, scratch):
                self.facade, self.closed, self.token_received = facade, False, False
            async def run(self, token):
                self.token_received = token == "synthetic-discord-placeholder"
                async def deliver(text):
                    return DeliveryReceipt(("synthetic-finite-discord",), text)
                for row in BUNDLE["items"]:
                    incoming = message(row["text"], "discord-" + row["id"])
                    assert self.facade.reserve(incoming, "99")
                    await self.facade.process(incoming, deliver)
                await asyncio.Future()
            async def close(self):
                self.closed = True
        def build(cfg, facade, scratch):
            bridge = FakeBridge(cfg, facade, scratch)
            bridges.append(bridge)
            return bridge
        with patch("indeces.discord_bridge.DiscordBridge.__init__", side_effect=AssertionError("No real Gateway")):
            result = await manual_runtime.run_discord(context, self.runtime_factory(wrapper), None,
                session_seconds=60, delivery_seconds=2, bridge_factory=build,
                token_loader=lambda unused: "synthetic-discord-placeholder")
        self.assertEqual([row["status"] for row in result["outcomes"]], ["completed", "completed"])
        self.assertTrue(bridges[0].closed)
        self.assertTrue(bridges[0].token_received)
        self.assertEqual(len(session.requests), 4)
        EVIDENCE["runtime_acceptances"].append({"path": "run_discord_fake_bridge", "delivered": 2,
            "fake_count": 2, "fake_generation": 2, "real_requests": 0, "actual_discord_delivery": False})

    async def test_finite_bridge_native_run_sets_reconnect_false_with_injected_discord_module(self):
        context, gate = self.context(), self.gate()
        wrapper, session, factory = self.adapter(gate)
        facade = manual_runtime.FiniteRuntimeFacade(context, self.runtime_factory(wrapper), mode="discord")
        starts, stopped = [], []
        scratch = SimpleNamespace(write=lambda *unused, **kwargs: None)
        class FakeBase:
            def __init__(self, cfg, runtime, scratch):
                self.config, self.scratch = cfg.discord, scratch
                self._running, self._worker = False, None
                self._worker_failed = asyncio.Event()
            def _start_worker(self):
                self._worker = asyncio.create_task(asyncio.Event().wait())
            async def _shutdown_worker(self):
                if self._worker is not None:
                    self._worker.cancel()
                    await asyncio.gather(self._worker, return_exceptions=True)
                stopped.append(True)
            def enqueue(self, incoming, bot):
                return False
        class FakeClient:
            def __init__(self, **kwargs):
                self.user = SimpleNamespace(id="99")
            async def __aenter__(self):
                return self
            async def __aexit__(self, *unused):
                await self.close()
            async def start(self, token, *, reconnect):
                starts.append({"synthetic_token": token == "synthetic-discord-placeholder", "reconnect": reconnect})
                await self.setup_hook()
            async def close(self):
                pass
        module = SimpleNamespace(Client=FakeClient,
            Intents=SimpleNamespace(none=lambda: SimpleNamespace(guilds=False, guild_messages=False)),
            AllowedMentions=SimpleNamespace(none=lambda: SimpleNamespace()),
            MemberCacheFlags=SimpleNamespace(none=lambda: SimpleNamespace()))
        bridge = manual_runtime.create_finite_bridge(context.config, facade, scratch,
                    bridge_class=FakeBase, selector=lambda *unused: None, discord_module_factory=lambda: module)
        await bridge.run("synthetic-discord-placeholder")
        self.assertEqual(starts, [{"synthetic_token": True, "reconnect": False}])
        self.assertTrue(stopped)
        self.assertFalse(bridge._running)
        self.assertTrue(bridge._worker.done())
        self.assertEqual(session.requests, [])

    async def test_actual_runtime_api_two_items_verified_without_services_and_reuse_blocked(self):
        context, gate = self.context(), self.gate()
        wrapper, session, factory = self.adapter(gate)
        outputs = []
        with patch("indeces.knowledge.KnowledgeService.__init__", side_effect=AssertionError("No KnowledgeService")), \
             patch("indeces.discord_bridge.DiscordBridge.__init__", side_effect=AssertionError("No Gateway")), \
             patch("indeces.console.serve", side_effect=AssertionError("No Console")):
            result = await manual_runtime.run_api_only(context, self.runtime_factory(wrapper),
                    session_seconds=60, delivery_seconds=2, on_output=lambda item, text: outputs.append((item, text)))
        self.assertEqual([row["status"] for row in result["outcomes"]], ["completed", "completed"])
        self.assertFalse(result["actual_discord_delivery"])
        self.assertFalse(result["stopped"])
        self.assertEqual(len(outputs), 2)
        self.assertEqual(len(session.requests), 4)
        report = verify_runs([scratch.path for scratch in self.scratches])
        self.assertEqual(report["counts"]["complete"], 2)
        self.assertEqual(report["call_counts"]["complete"], 2)
        self.assertEqual(report["issues"], [])
        EVIDENCE["runtime_acceptances"].append({"path": "actual_adapter_finite_api", "delivered": 2,
            "fake_count": 2, "fake_generation": 2, "verify_runs_complete": report["counts"]["complete"],
            "background_workers_started": 0})
        with self.assertRaises(manual_runtime.ManualRuntimeError) as caught:
            await manual_runtime.run_api_only(context, self.runtime_factory(wrapper), session_seconds=60, delivery_seconds=2)
        self.assertEqual(caught.exception.code, "manual_context_already_used_no_retry")
        self.assertEqual(len(session.requests), 4)

    async def test_known_billed_adapter_parse_failure_prevents_second_runtime_item(self):
        context, gate = self.context(), self.gate()
        def handler(endpoint, payload):
            data = response(endpoint, payload)
            if endpoint == GENERATION_ENDPOINT:
                data["output"] = []
            return data
        wrapper, session, factory = self.adapter(gate, handler)
        result = await manual_runtime.run_api_only(context, self.runtime_factory(wrapper), session_seconds=60, delivery_seconds=2)
        self.assertTrue(result["stopped"])
        self.assertEqual(len(result["outcomes"]), 1)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(gate.ledger["requests"][-1]["status"], "completed")
        self.assertEqual(gate.ledger["requests"][-1]["actual_cost_usd"], "0.000152")
        self.assertEqual(gate.ledger["stop_code"], "reply_failed_no_retry")

    async def test_delivery_failure_keeps_calculated_fee_and_persists_batch_stop_on_reopen(self):
        context, gate = self.context(), self.gate()
        wrapper, session, factory = self.adapter(gate)
        def deliver_failed(item, text):
            raise RuntimeError("synthetic delivery failure")
        result = await manual_runtime.run_api_only(context, self.runtime_factory(wrapper),
                session_seconds=60, delivery_seconds=2, on_output=deliver_failed)
        self.assertTrue(result["stopped"])
        self.assertEqual(len(result["outcomes"]), 1)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(gate.ledger["requests"][-1]["actual_cost_usd"], "0.000152")
        self.assertTrue(gate.ledger["stop_code"])
        self.assertEqual(result["persistent_stop_errors"], [])
        manifest = gate.approval
        gate.close()
        reopened = self.gate(manifest=manifest)
        with self.assertRaises(PilotBlocked):
            self.admit(reopened, "case-beta")

    async def test_stop_persistence_error_is_reported_even_when_ram_stop_code_is_set(self):
        context, gate = self.context(), self.gate()
        wrapper, session, factory = self.adapter(gate)
        facade = manual_runtime.FiniteRuntimeFacade(context, self.runtime_factory(wrapper))
        with patch.object(gate, "_save", side_effect=OSError("synthetic disk failure")):
            facade.stop("synthetic_session_failure")
        self.assertEqual(gate.ledger["stop_code"], "synthetic_session_failure")
        self.assertTrue(facade.persistent_stop_errors)
        self.assertIn("OSError", json.dumps(facade.persistent_stop_errors))
        self.assertIsNone(json.loads(gate.path.read_text())["stop_code"])
        self.assertEqual(session.requests, [])

    async def test_finite_bridge_human_explicit_mentions_allowlist_and_dedup(self):
        context, gate = self.context(), self.gate()
        wrapper, session, factory = self.adapter(gate)
        facade = manual_runtime.FiniteRuntimeFacade(context, self.runtime_factory(wrapper), mode="discord")
        class FakeBase:
            def __init__(self, cfg, runtime, scratch):
                self.config, self._accepting, self.accepted = cfg, True, []
            def enqueue(self, incoming, bot):
                if not self._accepting:
                    return False
                self.accepted.append(incoming)
                return True
        bridge = manual_runtime.create_finite_bridge(context.config, facade, None, bridge_class=FakeBase,
                                                      selector=lambda incoming, bot, cfg: incoming)
        invalid = [message(author_is_bot=True), message(guild_id="11"), message(channel_id="21"),
                   message(raw_text="unmentioned"), message(text="unapproved question")]
        for incoming in invalid:
            self.assertFalse(bridge.enqueue(incoming, "99"))
        first = message(ident="alpha-message")
        self.assertTrue(bridge.enqueue(first, "99"))
        self.assertFalse(bridge.enqueue(first, "99"))
        self.assertFalse(bridge.enqueue(message(ident="new-duplicate"), "99"))
        second = message("beta evidence?", "beta-message")
        self.assertTrue(bridge.enqueue(second, "99"))
        self.assertFalse(bridge._accepting)
        self.assertFalse(bridge.enqueue(message("third", "third-message"), "99"))
        delivered = []
        async def deliver(text):
            delivered.append(text)
            return DeliveryReceipt(("synthetic-discord-delivery",), text)
        for incoming in bridge.accepted:
            await facade.process(incoming, deliver)
        self.assertEqual(len(delivered), 2)
        self.assertTrue(facade.done.is_set())
        self.assertFalse(facade.stopped)
        self.assertEqual(len(session.requests), 4)
        EVIDENCE["runtime_acceptances"].append({"path": "actual_adapter_fake_finite_bridge", "delivered": 2,
            "fake_count": 2, "fake_generation": 2, "actual_discord_delivery": False})

    async def test_production_shaped_state_and_shared_history_are_rejected(self):
        context, gate = self.context(), self.gate()
        wrapper, session, factory = self.adapter(gate)
        create = self.runtime_factory(wrapper)
        first = create(context.approved_items[0], context)
        with self.assertRaises(manual_runtime.ManualRuntimeError) as caught:
            manual_runtime.FiniteRuntimeFacade(context, {item["id"]: first for item in context.approved_items})
        self.assertEqual(caught.exception.code, "manual_distinct_fresh_stores_required")
        first.config = replace(first.config, state_dir=context.config.state_dir)
        with self.assertRaises(manual_runtime.ManualRuntimeError) as caught:
            manual_runtime.FiniteRuntimeFacade(context, {item["id"]: first for item in context.approved_items})
        self.assertEqual(caught.exception.code, "manual_production_store_forbidden")
        self.assertEqual(session.requests, [])
