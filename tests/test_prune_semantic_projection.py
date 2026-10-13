"""Pure offline semantic projection tests; no provider or production reads.

Model outputs below are synthetic intent proposals, not evidence of scientific
accuracy or successful live bilingual intent recognition.
"""
import json
from types import SimpleNamespace
import unittest

from indeces.contracts import GovernedError
from indeces.prune_question import (
    GroundedPlan, prepare_grounding, prepare_semantic_grounding,
    resolve_semantic_plan, strict_json,
)


def graph():
    return SimpleNamespace(nodes={
        11: {"name": "synthetic alpha", "coarse": "entity", "fine": "synthetic"},
        22: {"name": "synthetic beta", "coarse": "entity", "fine": "synthetic"},
        33: {"name": "synthetic relation", "coarse": "relation", "fine": "synthetic"},
        44: {"name": "synthetic gamma", "coarse": "entity", "fine": "synthetic"},
    }, m2c={101: 11, 102: 11, 201: 22, 301: 33, 401: 44})


def plan(**changes):
    result = {"mode": "single", "confidence": "high",
              "seed_terms_A": ["synthetic alpha"], "seed_terms_B": [],
              "target": "relation", "level": "coarse", "reply_text": ""}
    result.update(changes)
    return result


class SemanticProjectionTests(unittest.TestCase):
    def setUp(self):
        self.graph = graph()
        self.question = "请说明合成甲的关系，并提供来源。"
        self.grounding = prepare_semantic_grounding(self.graph, self.question)

    def resolve(self, proposal=None):
        return resolve_semantic_plan(self.graph, self.question,
                                     plan() if proposal is None else proposal,
                                     self.grounding)

    def reject(self, reason, proposal):
        with self.assertRaises(GovernedError) as caught:
            self.resolve(proposal)
        self.assertEqual(str(caught.exception), "prune_question_" + reason)
        self.assertFalse(caught.exception.remote_usage_unknown)

    def test_chinese_projection_maps_real_original_members(self):
        result = self.resolve()
        self.assertEqual(result.plan, GroundedPlan((101,), (), "relation", "coarse"))
        self.assertEqual(result.provenance, "semantic_model_projection_v1")
        self.assertEqual(result.reply_text, "")

    def test_mixed_language_question_does_not_need_literal_targets(self):
        question = "synthetic alpha 与合成乙有什么区别？"
        grounding = prepare_semantic_grounding(self.graph, question)
        result = resolve_semantic_plan(self.graph, question,
                                      plan(mode="dual", seed_terms_B=["synthetic beta"]),
                                      grounding)
        self.assertEqual(result.plan.seeds_A, (101,))
        self.assertEqual(result.plan.seeds_B, (201,))

    def test_only_coarse_catalog_enters_payload(self):
        payload = json.loads(self.grounding.user)
        self.assertEqual(set(payload), {"question", "allowed_coarse_targets"})
        self.assertEqual(payload["allowed_coarse_targets"], ["entity", "relation"])
        self.assertNotIn("synthetic alpha", self.grounding.user)
        self.assertNotIn("101", self.grounding.user)
        self.assertNotIn("11", self.grounding.user)

    def test_normalized_exact_case_width_and_space(self):
        result = self.resolve(plan(seed_terms_A=["  ＳＹＮＴＨＥＴＩＣ   ALPHA  "]))
        self.assertEqual(result.plan.seeds_A, (101,))

    def test_unknown_term_declines_without_aliases_or_fuzzy_matching(self):
        self.reject("semantic_unknown_name", plan(seed_terms_A=["synthetic alfa"]))

    def test_ambiguous_normalized_name_declines(self):
        self.graph.nodes[55] = {"name": "SYNTHETIC ALPHA", "coarse": "entity", "fine": "synthetic"}
        self.graph.m2c[501] = 55
        self.grounding = prepare_semantic_grounding(self.graph, self.question)
        self.reject("semantic_ambiguous_name", plan())

    def test_missing_original_member_declines(self):
        del self.graph.m2c[101]
        del self.graph.m2c[102]
        self.grounding = prepare_semantic_grounding(self.graph, self.question)
        self.reject("missing_original_member", plan())

    def test_low_confidence_graph_declines(self):
        self.reject("semantic_invalid_graph_plan", plan(confidence="low"))

    def test_unknown_or_fine_target_declines(self):
        self.reject("semantic_invalid_graph_plan", plan(target="imaginary"))
        self.reject("semantic_invalid_graph_plan", plan(level="fine"))

    def test_empty_target_pool_declines(self):
        self.reject("empty_target_pool", plan(seed_terms_A=["synthetic relation"]))

    def test_single_cannot_smuggle_second_group(self):
        self.reject("semantic_invalid_graph_plan", plan(seed_terms_B=["synthetic beta"]))

    def test_dual_requires_distinct_groups(self):
        self.reject("semantic_invalid_graph_plan", plan(mode="dual"))
        self.reject("ambiguous_seed_overlap", plan(mode="dual", seed_terms_B=["SYNTHETIC ALPHA"]))

    def test_duplicate_normalized_seed_declines(self):
        self.reject("semantic_duplicate_seed", plan(seed_terms_A=["synthetic alpha", "SYNTHETIC ALPHA"]))

    def test_strict_fields_and_bounded_groups(self):
        value = plan(extra="untrusted")
        self.reject("semantic_invalid_shape", value)
        self.reject("semantic_invalid_shape", plan(seed_terms_A=["synthetic alpha"] * 7))
        self.reject("semantic_invalid_shape", plan(reply_text="x" * 801))

    def test_decoded_surrogate_rejected(self):
        with self.assertRaises(ValueError):
            strict_json('{"mode":"single","confidence":"high","seed_terms_A":["\\ud800"],'
                        '"seed_terms_B":[],"target":"relation","level":"coarse","reply_text":""}')
        self.reject("semantic_invalid_unicode", plan(seed_terms_A=["\ud800"]))

    def test_graph_mutation_between_projection_and_resolution_declines(self):
        self.graph.nodes[11]["name"] = "replacement"
        self.reject("grounding_changed", plan())

    def test_grounding_question_or_schema_mutation_declines(self):
        with self.assertRaises(GovernedError):
            resolve_semantic_plan(self.graph, self.question + " changed", plan(), self.grounding)
        self.grounding.schema["properties"]["level"]["enum"].append("fine")
        self.reject("grounding_changed", plan())

    def test_chat_and_abstain_are_explicit_non_graph_results(self):
        chat_question = "你好"
        chat = resolve_semantic_plan(self.graph, chat_question,
            plan(mode="chat", seed_terms_A=[], target="", level="", reply_text="你好。"),
            prepare_semantic_grounding(self.graph, chat_question))
        self.assertEqual((chat.mode, chat.plan, chat.reply_text), ("chat", None, "你好。"))
        decline = self.resolve(plan(mode="abstain", confidence="low", seed_terms_A=[],
                                    target="", level="", reply_text="请澄清你希望查询的概念。"))
        self.assertIsNone(decline.plan)
        self.reject("semantic_invalid_non_graph_plan", plan(mode="chat", reply_text="hello"))

    def test_science_question_cannot_use_chat_to_bypass_prune(self):
        self.reject("semantic_chat_requires_social_question", plan(mode="chat", seed_terms_A=[],
            target="", level="", reply_text="An unsupported scientific answer"))
        question = "hi, explain a scientific reaction"
        with self.assertRaises(GovernedError):
            resolve_semantic_plan(self.graph, question, plan(mode="chat", seed_terms_A=[],
                target="", level="", reply_text="An unsupported scientific answer"),
                prepare_semantic_grounding(self.graph, question))

    def test_abstain_discards_model_factual_text_and_returns_host_clarification(self):
        proposed = "An unsupported factual answer with invented sources"
        result = self.resolve(plan(mode="abstain", confidence="low", seed_terms_A=[],
            target="", level="", reply_text=proposed))
        self.assertNotEqual(result.reply_text, proposed)
        self.assertIn("概念", result.reply_text)

    def test_graph_modes_cannot_return_ungrounded_answer(self):
        self.reject("semantic_invalid_graph_plan", plan(reply_text="unsupported factual answer"))
        self.assertIn("never use\nchat to answer a scientific", self.grounding.instructions)

    def test_existing_literal_grounding_is_still_literal(self):
        with self.assertRaises(GovernedError) as caught:
            prepare_grounding(self.graph, self.question)
        self.assertEqual(str(caught.exception), "prune_question_no_grounded_vocabulary")

    def test_invalid_question_fails_before_any_projection(self):
        for question in ("", " ", "\ud800", 1):
            with self.assertRaises(GovernedError):
                prepare_semantic_grounding(self.graph, question)


if __name__ == "__main__":
    unittest.main()
