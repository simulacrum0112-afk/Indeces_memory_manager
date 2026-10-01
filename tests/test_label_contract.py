"""Label boundary contracts use synthetic local responses only."""
import json
import re
import unittest

from indeces.contracts import GovernedError
from indeces.prompts import LABEL_SCHEMA
from indeces.runtime import label_data


class LabelContractTests(unittest.TestCase):
    def test_provider_schema_constrains_cardinality_and_nonblank_words(self):
        marks = LABEL_SCHEMA["properties"]["marks"]
        for count in (0, 1, 8, 9):
            with self.subTest(count=count):
                output = {"marks": [f"word{index}" for index in range(count)]}
                permitted = marks["minItems"] <= count <= marks["maxItems"]
                if permitted:
                    self.assertEqual(len(label_data(json.dumps(output))), count)
                else:
                    with self.assertRaises(GovernedError):
                        label_data(json.dumps(output))
        pattern = marks["items"]["pattern"]
        self.assertEqual(marks["items"]["maxLength"], 40)
        self.assertIsNone(re.search(pattern, " \t\n"))
        self.assertIsNotNone(re.search(pattern, "合成主题"))

    def test_local_validation_still_rejects_length_and_duplicate_json_keys(self):
        self.assertEqual(label_data(json.dumps({"marks": ["x" * 40]})), ["x" * 40])
        for output in (json.dumps({"marks": ["x" * 41]}), '{"marks":["a"],"marks":["b"]}'):
            with self.subTest(output=output), self.assertRaises(GovernedError):
                label_data(output)
