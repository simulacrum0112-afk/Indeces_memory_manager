import unittest

from indeces.user_notices import user_notice


class UserNoticeTests(unittest.TestCase):
    def test_model_stop_is_neutral_and_does_not_claim_verified_absence_or_intent(self):
        text = user_notice("model_stop")
        self.assertNotIn("model_stop", text)
        self.assertNotIn("没有任何", text)
        self.assertNotIn("按你的要求", text)
        self.assertIn("不能", text)

    def test_capacity_time_and_processing_failures_have_distinct_explanations(self):
        capacity = user_notice("input_token_limit")
        timeout = user_notice("stage_timeout")
        service = user_notice("provider_network_error")
        evidence = user_notice("model_stop")
        self.assertEqual(len({capacity, timeout, service, evidence}), 4)
        self.assertIn("处理范围", capacity)
        self.assertIn("耗时", timeout)
        for text in (capacity, timeout, service):
            self.assertNotIn("材料不足", text)

    def test_no_additional_material_does_not_claim_the_initial_evidence_was_empty(self):
        for reason in ("empty_results", "no_new_evidence", "duplicate_query"):
            with self.subTest(reason=reason):
                text = user_notice(reason)
                self.assertIn("更多", text)
                self.assertNotIn("没有任何", text)
                self.assertNotIn(reason, text)

    def test_incomplete_response_does_not_guess_token_capacity_or_service_cause(self):
        text = user_notice("incomplete_response")
        self.assertIn("完整结果", text)
        self.assertNotIn("处理范围", text)
        self.assertNotIn("材料不足", text)
        self.assertNotIn("incomplete_response", text)

    def test_dynamic_unknown_codes_never_enter_the_public_notice(self):
        for reason in ("provider_http_429", "PrivateFailureClass", "new_backend_reason_123", None, {}):
            with self.subTest(reason=reason):
                text = user_notice(reason)
                self.assertEqual(text, user_notice("provider_network_error"))
                if isinstance(reason, str):
                    self.assertNotIn(reason, text)

    def test_clarification_is_preserved_only_for_the_verified_ambiguity_route(self):
        question = "你指的是哪一段时间？"
        self.assertEqual(user_notice("ambiguity", clarification=question), "想确认一下：" + question)
        self.assertNotIn(question, user_notice("provider_network_error", clarification=question))
