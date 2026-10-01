"""Manual browser QA on temporary SYNTHETIC data, never user's configuration."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from indeces.config import load_config
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.observer import ObserverServer
from indeces.run_records import answer_record, digest, freeze_retrieval
from indeces.scratch import ScratchLog
from indeces.store import Store


def main():
    with TemporaryDirectory(prefix="indeces-synthetic-observer-") as directory:
        root = Path(directory).resolve()
        base = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        config = replace(base, state_dir=root / "state", scratch_dir=root / "scratch",
                         knowledge_dir=root / "knowledge", discord=replace(base.discord, guild_id="123456789012345678"))
        store = Store(config.state_dir)
        graph = MemoryGraph(store.db)
        KnowledgeService(config, store, graph, None, None)
        scope = config.discord.guild_id + ":knowledge"
        groups = [(["长期记忆", "证据", "来源"], "召回材料有明确来源版本，可用记录核对。"),
                  (["长期记忆", "标注词", "治理"], "标签附在原文上，更新完成后整体发布。"),
                  (["NPMI", "标注词", "共现"], "NPMI 按有效文档的标签共现计算。"),
                  (["治理", "权重", "衰减"], "动态边经直接命中强化，也按检索事件衰减。"),
                  (["证据", "来源", "版本"], "旧完整版本在新标词尚未完成时继续被检索。"),
                  (["共现", "权重", "NPMI"], "动态权重与 NPMI 分开保存。"),
                  (["版本", "标注词", "来源"], "原文和标注词属于同一发布版本。"),
                  (["孤立词"], "此节点没有共现边，也应能观察。"),
                  (["<script>synthetic</script>", "证据"], "这是合成的 HTML 注入测试文字，必须按文本展示。")]
        now = datetime.now(timezone.utc)
        for i, (marks, text) in enumerate(groups):
            source = f"kb:synthetic-{i}"
            path = f"合成演示/材料{i+1}.md"
            with store.db:
                store.db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,scope) VALUES(?,?,?,?,?,?,?)",
                                 (source, path, hashlib.sha256(text.encode()).hexdigest(), text, "ready", now.timestamp(), scope))
                store.db.execute("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?)", (source, 0, text, 0, json.dumps(marks, ensure_ascii=False)))
                store.db.execute("INSERT INTO knowledge_desired VALUES(?,?,?)", (scope, path, source))
                store.db.execute("INSERT INTO knowledge_published VALUES(?,?,?)", (scope, path, source))
            graph.add(scope, source, "synthetic", [{"text": text, "quote": text, "marks": marks}], now.timestamp())
        with store.db:
            store.db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,scope) VALUES(?,?,?,?,?,?,?)",
                             ("kb:synthetic-pending", "合成演示/材料1.md", "f"*64, "待标词合成新版", "labelling", now.timestamp(), scope))
            store.db.execute("UPDATE knowledge_desired SET source_id=? WHERE scope=? AND path=?", ("kb:synthetic-pending", scope, "合成演示/材料1.md"))
        for i, query in enumerate(("长期记忆 证据 来源", "NPMI 标注词 共现", "治理 权重 衰减", "证据 来源 版本", "长期记忆 标注词 治理")):
            graph.retrieve(scope, [], query, (now-timedelta(seconds=60-i*10)).timestamp(), event_id=f"synthetic-event-{i}")
        clock = [now-timedelta(seconds=5)]
        scratch = ScratchLog(config.scratch_dir, clock=lambda: clock[0])
        trace = "synthetic-trace-only"
        query = "长期记忆 来源 证据"
        scratch.write("turn_start", trace_id=trace, message_id="synthetic-message", text=query, run_record_version=1, knowledge_scope=scope)
        audit = {}
        records = graph.retrieve(scope, [], query, now.timestamp(), event_id="synthetic-last", audit=audit)
        retrieval = freeze_retrieval(store.db, scope, "synthetic-last", query, records, audit)
        scratch.write("memory_observation", trace_id=trace, audit=audit, audit_sha256=digest(audit))
        scratch.write("retrieval_record", trace_id=trace, record=retrieval, record_sha256=digest(retrieval))
        text = "合成回答：召回的材料附有来源版本。[M1]"
        scratch.write("answer_generated", trace_id=trace, record=answer_record(text, retrieval), retrieval_sha256=digest(retrieval))
        scratch.write("answer_delivered", trace_id=trace, record=answer_record(text, retrieval), retrieval_sha256=digest(retrieval))
        scratch.write("turn_end", trace_id=trace, status="delivered")
        scratch.close()
        observer = ObserverServer(config)
        try:
            print("SYNTHETIC BROWSER QA ONLY; no model/Discord/user data.", flush=True)
            print(observer.start(), flush=True)
            input("Enter to close fixture: ")
        finally:
            observer.close()
            store.close()


if __name__ == "__main__":
    main()
