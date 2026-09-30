import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from indeces.scratch import ScratchLog, canonical, verify


class ScratchTests(unittest.TestCase):
    def test_records_survive_reopen_and_extend_same_hash_chain(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            path = log.path
            log.write("http_request", trace_id="trace", call_id="call", payload={"input": "中文 source"})
            log.write("http_response", trace_id="trace", call_id="call", payload={"text": "visible reply"})
            second_hash = log.previous
            log.close()
            self.assertEqual(verify(path), (2, second_hash))
            log = ScratchLog(Path(directory))
            self.assertEqual(log.sequence, 2)
            log.write("call_end", status="completed")
            third_hash = log.previous
            log.close()
            self.assertEqual(verify(path), (3, third_hash))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[2]["previous_hash"], second_hash)
            self.assertEqual([row["sequence"] for row in rows], [1, 2, 3])
            self.assertEqual(rows[0]["fields"]["payload"]["input"], "中文 source")

    def test_payload_tampering_is_detected_and_prevents_append(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            path = log.path
            log.write("http_request", payload={"text": "original"})
            log.close()
            item = json.loads(path.read_text(encoding="utf-8"))
            item["fields"]["payload"]["text"] = "edited"
            path.write_text(json.dumps(item) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(path)
            with self.assertRaises(ValueError):
                ScratchLog(Path(directory))

    def test_removed_middle_record_breaks_chain(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            path = log.path
            for index in range(3):
                log.write("observation", index=index)
            log.close()
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(path)

    def test_incomplete_write_is_detected_on_restart(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            path = log.path
            log.write("start", value=1)
            log.close()
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"event":')
            with self.assertRaises(ValueError):
                verify(path)

    def test_canonical_json_is_stable_and_rejects_nonfinite_values(self):
        self.assertEqual(canonical({"b": 2, "a": "中文"}), canonical({"a": "中文", "b": 2}))
        with self.assertRaises(ValueError):
            canonical({"number": float("nan")})


if __name__ == "__main__":
    unittest.main()
