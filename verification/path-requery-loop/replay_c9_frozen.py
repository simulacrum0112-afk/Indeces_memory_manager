"""Replay saved synthetic native C9 inputs without a store, provider or gold."""
from copy import deepcopy
import json,socket,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from indeces.path_requery_loop import PathRequeryLoopConfig,build_path_feedback
from indeces.run_records import digest
def stable(receipt):
 value=deepcopy(receipt);value.pop("feedback_sha256",None);value["stats"].pop("elapsed_seconds",None);return value
def forbidden(*args,**kwargs): raise AssertionError("C9 replay forbids network")
def main():
 fixture=json.loads(Path(__file__).with_name("FROZEN_C9_NATIVE.json").read_text(encoding="utf-8"))
 before=digest(fixture);rows=[]
 socket.socket.connect=forbidden;socket.socket.connect_ex=forbidden;socket.getaddrinfo=forbidden
 for case in fixture["cases"]:
  result=build_path_feedback(case["frozen"],case["action"],planning_call_id=case["planning_call_id"],config=PathRequeryLoopConfig(**case["config"]))
  assert digest(stable(result))==case["expected_stable_receipt_sha256"],case["name"]
  assert not result["proof"] and not result["truth_verified"] and not result["semantic_support_verified"]
  indices=result.get("accumulated_candidate_indices",[])
  rows.append({"name":case["name"],"status":result["status"],"added_terms":result["added_terms"],
   "accumulated_candidates":len(indices),"formal_count":sum(c["formal_derivation"] is not None for c in result["candidates"]),
   "origin_rounds":[result["candidates"][i]["round_indices"] for i in indices],
   "replayed":True,"semantic_support_verified":False})
 assert digest(fixture)==before
 print(json.dumps({"schema":"dev11_c9_frozen_native_replay_v1","cases":rows,"input_unchanged":True,"store_or_provider_access":False,"gold_or_private_data_read":False},indent=2))
if __name__=="__main__":main()
