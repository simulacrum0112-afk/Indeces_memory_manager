# 0.13.0：持久边键与命中邻域检索

本版将全局索引构建移到摄入、发布和启动迁移阶段。产品默认静态查询从持久索引查命中词，再查这些词的一跳边及允许词的出处倒排；不会先加载整个图，也不会因缓存冷却而在查询内重建。用户另明确批准：静态检索关闭 shadow 更新，字节一致性按**同一冻结动态状态**验证。

这次没有读取真实知识、私有配置或运行数据，没有启停、热更新服务或迁移真实库。已有动态权重和旧事件不删除、不重写。新代码由用户显式重开/start 后加载；启动迁移与摄入可以有全局成本，不能混进查询计时或称为常数成本。

## 改动边界

| 原行为 | 新行为 | 分类／影响 |
|---|---|---|
| 查询冷缓存加载全 scope records/support/static | 发布持久 trie、倒排、频次、邻接及边键；查询只读相关键 | 存储／运行表示 |
| 按文本对存边，内存邻接在查询时建立 | 稳定数字 ID；有向 `(scope,i,j)` 主键；持久邻接 | 存储表示；ID 不重排 |
| 全图 mark_frequencies | 相关边端点的原频次；完整 n/known/live count 保存为发布标量 | 已授权审计范围改变 |
| 静态查询仍全图 shadow 衰减／强化 | 静态查询不 seed、不 decay、不 reinforce，不新增 dynamic event/cycle | 用户明确批准；不能与旧更新后的 dynamic 字段声称逐字节相同 |
| 旧重放解析全图原审计 | 不可变事件头保存输入、模式、状态、数量和原包 SHA256 | 运行表示；原 JSON/hash 保留 |
| 全 shadow 转换及图频次进入冻结 | v2 仅冻结相关边及端点频次，完整候选／出处／[M] 保留 | 整包和 hash 改变，禁止宣称整包逐字节相同 |
| 动态时间结算 | 仅预留发布时间字段与未调用的纯 lazy_decay 函数 | 结构预留；没有启用时间衰减或新动态排序 |
| NPMI、四位舍入、context/corroboration gate、去重、一跳、top-5/extra-2/reference-3、[M]、5 秒预算、串行槽、五轮 | 不变 | 没有科学／数值／额度参数修改 |

## Schema

保留 `memory_records/static/support/dynamic/events/cycles/event_audits` 原表作为来源、历史和显式低层动态路径。新增索引 schema 为 1，查询审计 schema 为 2，两者用途不同。

| 表 | 键／内容 |
|---|---|
| `memory_query_marks` | `id INTEGER PRIMARY KEY AUTOINCREMENT`，`label UNIQUE`。全库稳定 ID；归档不删除 ID |
| `memory_query_edges` | `PRIMARY KEY(scope,i,j)`；原文本端点、static_score、co_count、context_json、evidence_json、原边插入序 ordinal、last_updated、primary_edge、active |
| `memory_query_adjacency` | `(scope,mark_id,i,j)`；neighbor_id、neighbor、static_score；另有 `(scope,mark_id,static_score DESC,neighbor)` 索引 |
| `memory_query_postings` | `(scope,mark_id,record_id)`；按命中／扩展标注词直接取出处 ID |
| `memory_query_frequencies` | `(scope,mark_id)`；已发布完整材料中的原始频次 |
| `memory_query_trie` | `(scope,kind,prefix)`；Latin/substring 前缀及 terminal labels，仅走查询可能命中的前缀 |
| `memory_query_scopes` | `scope`；完整 active_record_count、known_marks_count、live_edge_count、源 revision、self_marks、schema version |
| `memory_query_revisions` | `scope`；records/static/support 持久触发器更新的源版本，事务回滚同步回滚 |
| `memory_query_event_headers` | `(scope,event_id)`；原 query、时间、模式、direct_hit_count、原 observation 状态／数量、original_changes_known、payload_sha256。禁止 UPDATE/DELETE |

对普通对称 NPMI，一条原边发布两个方向的坐标，分数及证据相同。`primary_edge=1` 表示原来源边，镜像为 0；邻接引用原边，召回证据不会重复。若历史原表确实存在反向或 self 键，其存储坐标独立保存；没有启用有向推理或新关系门控。

`last_updated` 是索引数据的发布时间，变化边单调推进、未变化边保留。它**不是已启用的动态衰减 checkpoint**。`lazy_decay` 只有纯函数：调用者必须显式给时间周期，按逐步 Python 舍入结算，未被产品查询、摄入或 observer 调用；没有把原来按检索事件的周期擅改为按秒。将来真正启用动态层仍需另行授权和验证。

SQLite 使用 B-tree 索引，单键 seek 通常为 O(log N)，不能声称严格 hash O(1)。本次保证的是去掉每次查询的全图读取／物化／冻结；查询成本由查询前缀、命中边度数、允许词倒排及候选审计字节决定。

## 查询与发布

1. 在原事务边界内检查持久源 revision、自身词配置和相关表／不可变触发器定义；拒绝 stale/incompatible 索引，不在查询内做全图 fallback。
2. 沿持久前缀键寻找 literal candidates，再用原 `_hit` 验证，按原长度／字典序选择最多四个 direct hits。Unicode IGNORECASE 特例及 casefold 子串语义保持。
3. 由 direct-hit 数字 ID 取所有正静态 incident edges。邻接 SQL 使用完整 scope/mark_id 键，边载荷按 `(scope,i,j)` 取。只点读这些边已有的 legacy dynamic 值，不写动态层。
4. 原选择器处理全部一跳候选，保留超过 top-5、context gate 拒绝及去重失败的决定。只最多两个额外词进入候选出处倒排；没有二跳扩展，也没有先截掉拒绝证据来通过性能测试。
5. 按允许词倒排取记录 ID，再按记录主键读取实际来源。排序、分数累加顺序、截断、完整 quote、[M] 和 PDF 来源绑定保持。
6. 仅局部边生成 `edge_statistics`，端点频次配完整 n 复核 NPMI；CanonicalSnapshot、scratch 与 freeze 消费这个局部审计树。原事件重放只查不可变事件头，保留原包链接。

摄入仍按原 NPMI 重算语义处理本 scope；全局 n/频次改变可能影响多条边，因此不声称摄入是邻域成本。发布在同一事务内 UPSERT 原坐标，追加新 ID／边、将退休派生边置 inactive；邻接／倒排／trie／频次随发布更新。旧内存缓存同时失效，查询不依赖旧缓存。合法版本替换和撤下继续由原 knowledge 发布事务调用 add/deactivate；回滚不会留下半套索引。

跨连接的合法发布直接对下次查询可见。未经发布的 raw SQL 源修改使 revision 不匹配并拒绝查询；维护可在事务内明确重建／发布索引。临时影子源表、相关 schema 变更或自身词配置不匹配也拒绝；无关表 DDL 可继续使用原索引。查询中 source mutation 会在提交前检查并整笔回滚。

## 审计语义和兼容

新包：`schema_version=2`、`audit_scope=direct_hit_neighborhood_v1`；selection 声明 `edge_statistics_scope=direct_hit_incident_v1` 和 `mark_frequencies_scope=edge_endpoints_v1`。observation 明确 `dynamic_shadow_enabled=false`，新事件 status=disabled，changed_edges 为空。全图的 n、known/live 数量是发布标量，**不是本轮全图冻结或全图完整性证明**。

相关边范围是 direct hits 的全部 incident edges，包含拒绝候选；不包含只连接两个无关邻居的边。整包／durable hash 与旧版不同。逐字节验证对象是在相同来源和冻结动态表上，由旧版独立完整选择器投影得到的：完整返回记录、相关边所有字段／顺序、接受拒绝决定、候选排名、selected/hash/来源证据。没有用新结果反推 oracle，也没有把停止 shadow 更新解释成旧 eager 轨迹的精确复刻。

旧 v1 全图／0.12.6 局部边统计包及 hash 原样保留，独立验证器继续识别原合同；旧无模式实验仍按 dynamic 解释，同事件禁止跨模式重放。v2 校验拒绝删除／未知范围标记、范围外边、错误端点频次和任何 shadow 更新。局部自足校验不能证明没有遗漏其他邻域成员或全图事件；完整知识审计／观察仍是单独只读工作，允许全局成本，不能藏入回复链。

新增 schema 前一致 SQLite backup 到 state 相邻 migration_backups；不复制到 scratch 或原文目录。启动负责一次性派生索引和旧事件头建设。部分派生表缺失时先备份、在维护阶段重新发布；全局 ID 映射表丢失则拒绝启动，避免静默重分配 ID。真实实例不热补丁，已有真实库尚未在本轮迁移。

## 修改模块与验证入口

- `indeces/memory_index.py`：新持久 schema、键查找、前缀／邻接／倒排、发布时间与未启用 lazy helper、事件头、迁移备份。
- `indeces/memory.py`：发布维护、源 revision/schema 防护、新静态局部读取；原显式 dynamic 路径保留。
- `indeces/run_records.py`：v2 局部证据独立校验；冻结继续通过同一 CanonicalSnapshot 共享局部树，不重新扫描图。
- `indeces/observer_data.py`、`indeces/web/observer.js`：报告邻域校验范围，保留历史观察能力。
- 新 storage/path/audit/byte-identity 测试覆盖持久冷读、SQL 索引计划、ID／坐标、Unicode、冻结动态值、发布撤下／rollback、跨连接、schema 损失、旧重放链接和 scope downgrade。

既有 v1 eager/cache 测试显式使用历史 fixture，不冒充新版 static 行为；新版生产 static 有独立测试。性能脚本 [benchmark_subgraph_retrieval.py](../verification/benchmark_subgraph_retrieval.py) 直接载入精确旧提交作同状态 oracle，固定 seed，并绑定所有参与源文件 hash。

## 本机验证结果

Python 3.12.14，Windows，内存 SQLite、临时磁盘 scratch；各规模／cache／拓扑三次，共 54 次。预算链路包含 retrieve、CanonicalSnapshot、memory_observation 写入 flush/fsync、freeze；另报告独立图校验与后续 retrieval_record/knowledge_retrieved 写入。以下为三次中位数，单位秒。

| 标注词总数 | 1000 | 3000 | 10000 |
|---|---:|---:|---:|
| 固定邻域相关边 | 415 | 415 | 415 |
| 全图正静态边 | 49,139 | 151,195 | 502,499 |
| 固定邻域 cold retrieve | .062 | .075 | .082 |
| 固定邻域 cold 预算链路 | .100 | .107 | .119 |
| 固定邻域 steady 预算链路 | .048 | .061 | .072 |
| 增长邻域相关边 | 406 | 1206 | 4006 |
| 增长邻域 cold 预算链路 | .074 | .246 | 2.110 |

全部 54 次预算链路最大 **2.491 秒**；包含后续落盘与独立验证的更宽完整链路最大 **3.966 秒**，均低于原 5 秒。固定邻域的实际 SQL 语句均为 784；每 100 个 VM 指令一次的回调计数，在每个 cache 模式下跨三个全图规模相同（236/238/240）。这支持查询工作不再随无关全图边数扫描增长；SQLite B-tree seek 及系统抖动仍可改变耗时。

另用实际临时磁盘 SQLite（WAL、synchronous=FULL、foreign_keys=ON）验证固定 415 边；1000/3000/10000 marks 各三次 cold 查询，预算链路中位 **.091/.105/.078 秒**，最大 .093/.127/.084 秒。准备库约 76.9/236.2/790.3 MB；全部 9 次相关字节对照、独立校验和动态表不变检查通过。磁盘和内存合计 **63 个合成样本**；磁盘准备、backup 和初始化已经温热 OS 页面缓存，不能称作存储冷读。

磁盘 wrapper 调用生产 MemoryGraph/KeyedMemoryIndex 及其 schema/SQL，采用 tuple row_factory（生产 Store 是 sqlite3.Row）；没有建立完整 Store/知识版本/PDF 表或启动 Runtime/Gateway。因此它是查询方法的代表性磁盘试算，不能称为完整生产连接或真实服务验收。计时边界、源码 hash 和这些限制另见 [证据说明](../verification/subgraph_evidence_notes_20261003.json)。

每个样本完整返回 records、literal/direct hits、相关边所有字段、候选决定／排序／证据及 selected/hash 均直接比较旧版本在同一冻结状态上的 canonical bytes，全部相同；每个实际新包另独立校验，动态表不变。摄入、替换、撤下、rollback、跨连接发布、稳定 ID／坐标和必要失效由独立测试覆盖。54 次曲线不是 54 个不同真实语义查询，不代表召回质量评测。

同 Python 3.12 的旧 baseline 单次 steady：1000/3000 固定邻域 retrieve 为 1.275/4.819 秒，retrieve＋snapshot＋freeze 为 3.563/13.597 秒；该旧计时不含 observation scratch，且执行旧全 shadow 合同，因此只作范围改变前的成本说明，不混为同一审计包或多次中位数。另保留 Python 3.14 的 10k 历史测量，不作跨解释器精确倍率对比。

边数增加不等于局部证据字节线性增加：增长邻域的完整审计约 .50/2.53/20.44 MB，保留重复 context/来源支持与完整 ranked candidates 会增加实际局部输出成本。因此不宣称任意枢纽、任意材料密度或任意规模都保证 5 秒，也不声称严格 O(相关边数)。cold 是新 Graph 无查询 primer，**不是 OS 页面缓存冷启动**。

启动／迁移／摄入仍有全图成本并单列：10k 固定图摄入 50.358 秒，cold Graph 初始化中位 5.480 秒、最大 5.977 秒。这些在回复本地预算外，不能报告为小于 5 秒。未测试真实模型、Discord 或私有知识的延迟。

磁盘 10k 准备摄入为 178.262 秒、Graph 初始化中位 11.979 秒，也未计入查询预算。源码绑定见 [SOURCE_0130.json](../verification/SOURCE_0130.json)：性能报告的 source.head 是提交前 checkout HEAD，实际参与源文件 SHA256 与运行源码提交核对；CRLF/LF 规范化单独声明，不冒称 Git blob 和 Windows 工作区原始字节相同。

主要证据：[新曲线](../verification/subgraph_after_20261003.json)、[磁盘曲线](../verification/subgraph_disk_after_20261003.json)、[同解释器旧测量](../verification/subgraph_baseline_py312_20261003.json)、[早期旧测量](../verification/subgraph_baseline_20261003.json)、[998 项最终离线回归](../verification/OFFLINE_0130_20261003T181400856152Z.json)、[wheel](../verification/WHEEL_0130.json)。四项交付验证与发布状态见 [CHECKPOINT.md](CHECKPOINT.md)。
