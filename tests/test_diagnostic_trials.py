import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget
from indeces.prompts import SUMMARY, summary_schema
from indeces.scratch import ScratchLog
from indeces.run_records import verify_runs


class DiagnosticTrialsTests(unittest.IsolatedAsyncioTestCase):
    async def trial(self, root, *, omit_start=False, duplicate=False, false_usage=False):
        scratch = ScratchLog(root)
        cfg = AdapterConfig('gpt-6.1-sol', 'https://api.openai.com/v1',
                            {s: Budget(4096, 512, 1, 'medium') for s in ('summary', 'reply', 'label')})
        async def request(path, payload):
            if path == '/responses/input_tokens':
                return {'input_tokens': 10}
            return {'id': 'synthetic', 'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 5},
                    'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"summary":"Attributed observation."}'}]}]}
        adapter = OpenAIAdapter(cfg, scratch, request=request)
        fields = dict(trace_id='trial', operator_approved=True, automatic_retries=0, checkpoint_update=False)
        try:
            if not omit_start:
                scratch.write('diagnostic_summary_trial_start', **fields)
                if duplicate:
                    scratch.write('diagnostic_summary_trial_start', **fields)
            await adapter.call('summary', SUMMARY, [{'role': 'user', 'content': 'Synthetic source.'}], 'trial', summary_schema(4096))
            scratch.write('diagnostic_summary_trial_end', trace_id='trial', status='completed', checkpoint_updated=False,
                          input_tokens=11 if false_usage else 10, output_tokens=5)
        finally:
            await adapter.close()
            scratch.close()
        return verify_runs([scratch.path])

    async def test_independent_summary_is_validated_separately_from_chat(self):
        with TemporaryDirectory() as d:
            result = await self.trial(Path(d))
            self.assertEqual(result['diagnostic_counts'], {'complete': 1, 'invalid': 0})
            self.assertEqual(result['turns'], [])
            self.assertEqual(result['issues'], [])

    async def test_missing_chat_start_is_not_blanket_accepted(self):
        with TemporaryDirectory() as d:
            result = await self.trial(Path(d), omit_start=True)
            self.assertEqual(result['counts']['invalid'], 1)

    async def test_duplicate_boundaries_and_false_usage_are_rejected(self):
        for mode in ('duplicate', 'false_usage'):
            with self.subTest(mode=mode), TemporaryDirectory() as d:
                result = await self.trial(Path(d), **{mode: True})
                self.assertEqual(result['diagnostic_counts']['invalid'], 1)


if __name__ == '__main__':
    unittest.main()
