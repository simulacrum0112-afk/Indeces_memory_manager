# 0.12.6 密集查询审计与边范围

本轮只使用可重建的合成材料，未读取真实知识库、私有配置或 scratch，未启动、停止或热更新真实服务。固定查询命中 16 个标注词，原规则仍只取 4 个 direct hits。

## 定位与修改范围

0.12.5 的一跳选择已经使用邻接索引。每个命中最多前 5 个邻居参与扩展，去重后最多 2 个扩展词，最后最多 3 条出处；没有二跳全展开。全部邻居（含排名超过 5、context gate 拒绝项）的决策审计仍有随命中邻域增长的成本。

剩余全图工作包括 `_edges` 复制/解码所有活跃边，`edge_statistics` 构造全部边，以及完整审计的 JSON 编码。新静态路径 `_query_edges` 通过缓存邻接索引只复制 direct hits 的全部 incident edges；这包括 direct-hit 之间的边、通过和未通过门控的一跳候选，不包含仅连接两个无关邻居的边。按旧源插入序保留 direct evidence，按原字典序保留相关统计行。

新审计明确声明 `selection.edge_statistics_scope="direct_hit_incident_v1"`。`live_edge_count`、`mark_frequencies` 仍表示完整当前图；统计行数现在表示相关边数。独立校验器拒绝未知范围、范围外边、缺失的相关 committed transition，以及与相关边不一致的扩展/排名证据。旧无范围声明的完整审计、显式低层 dynamic 模式和不支持索引的历史边继续使用旧路径，不改写已存事件。

NPMI、corroboration/context gate、去重、来源与 [M]、static/dynamic 分层、动态每轮衰减/舍入/事件幂等及原 5 秒预算没有改变。未迁移数据库 schema、清除缓存结果或重算旧任务。

## 字节一致性合同

基线先保存旧源码同输入产生的规范 JSON；新结果必须直接比较返回记录的完整字节。旧 `edge_statistics` 仅按 direct-hit incident 范围投影，新相关行的全部字段/顺序/浮点编码必须相同。其余 match、observation（包括全部 changed_edges）、频次、候选排序、接受/拒绝决策、selected/source/text hashes 逐字节比较。

整体审计包因为删除无关统计行并增加范围声明而改变，durable hash 和 event-audit JSON 随之变化，不能宣称整包与旧版字节相同。回归测试独立核验实际 durable JSON/hash、旧包投影与新包，其他 memory 表逐行规范字节相同；重放核验原事件包链接，来源替换/撤下、rollback、动态模式及 Unicode 门控均保留比较。

## 尚未完成的全链路目标

本次将暖缓存的静态边材料与边统计限制在命中邻域，但**没有实现整次查询仅随邻域增长**。原 shadow 合同要求每轮处理所有动态边并保存完整 `changed_edges`；审计快照、冻结和独立校验仍处理这些全图字节，完整频次也随全部 marks 增长。冷缓存与材料修改后重建索引仍有全图成本。显式 dynamic/历史异常类型 fallback 保留全图路径。

若严格要求整条链路不随全图增长，必须先决定是否改变全动态观察/完整逐轮 transition 收据合同。本轮仅获设计授权，未获应用授权，也没有静默缩减该证据或扩大时间预算。两种合成规模只能支撑所测条件的曲线；不能证明任意图规模都满足 5 秒或真实 Discord 延迟已验收。

可复现脚本：[benchmark_dense_retrieval.py](../verification/benchmark_dense_retrieval.py)。计时分别报告冷/暖缓存、固定/增长邻域、retrieve、snapshot/freeze 与独立 selection；生产磁盘 scratch 写入未计入，因此 read/freeze 时间不代表完整服务延迟。

`steady` 先在同一合成图上完成一次不同事件的真实检索，再计时下一次新事件，覆盖已播种状态。`--baseline-ref b6c7e34203fae5770bc07f3f71dd15ef0f6c695c` 从可达旧提交读取 `memory.py` 到独立模块，不修改工作区；冻结/scratch 工具使用当前版本的旧完整审计兼容分支。报告记录所用源文件摘要、固定 seed、拓扑、计时边界及每个样本。`--save-baseline` 的完整合成字节只写到仓库外，`--compare-baseline` 比较相同输入；文件只创建一次，不覆盖原证据。

## 测量结果

已播种暖缓存、固定邻域、3 次中位数；每个规模分别与该规模旧版同输入比较。两规模均为 415 条相关边、410 条邻居决策，全图为 49,139/151,195 条边。

| 测量 | 1000 marks | 3000 marks |
|---|---:|---:|
| 旧 retrieve | 1.458 秒 | 5.807 秒 |
| 新 retrieve | 0.802 秒 | 4.448 秒 |
| 旧 retrieve＋snapshot＋freeze | 3.973 秒 | 14.303 秒 |
| 新 retrieve＋snapshot＋freeze | 2.057 秒 | 11.217 秒 |
| 新相关边材料化 | 3.03 毫秒 | 4.89 毫秒 |
| 新 selection | 2.60 毫秒 | 4.39 毫秒 |

独立 profile 的旧 `edge_statistics` 表达式耗时 53.10 毫秒，占 `_selection` 58.74 毫秒的 90.4%；同样本 context-hit 约 0.75 毫秒。新表达式约 0.224 毫秒。因此该函数的热点是全图统计构造；全链路更大的成本还包括边物化、shadow 和规范编码/冻结，不能将上述占比解释为整个 5 秒流程的占比。

增长邻域对照保持 4 个直接命中，相关边从 406→1206、邻居决策从 400→1200；新材料化 2.56→8.52 毫秒、selection 1.34→3.31 毫秒。相关边路径随实际邻域增长，全部 top-k/去重/拒绝证据保留。工作量回归另证明，1000/3000 marks 的固定局部图都只解码相同 18 条相关边的 36 个 JSON 字段，暖重复查询均为零解码。

证据：[steady before](../verification/dense_retrieval_steady_before_20261002.json)、[steady after](../verification/dense_retrieval_steady_after_20261002.json)、[增长邻域 before](../verification/dense_retrieval_star_before_20261002.json)、[增长邻域 after](../verification/dense_retrieval_star_after_20261002.json)、[原统计块 profile](../verification/dense_retrieval_expression_before_20261002.json)、[方法与证据限制](../verification/dense_retrieval_evidence_notes_20261002.json)。每次返回记录与投影审计直接比较规范 bytes，并独立校验实际 durable hash。完整审计包不相同是已声明的范围变化。

初始冷/暖单次报告保留；其阶段计时误包含后续隔离选择调用，阶段字段已由上述修正报告替代，不用于最终曲线。修正 cold/warm after 单次测量仅作说明，不与 3 次 steady 中位数混用。初始 baseline/最终 harness 的元数据及 probe 增补差异在证据说明中记录；固定拓扑和 ordinary timing 政策一致。隔离 selection 的原始计时有 GC/分配噪声，最终局部曲线采用正常 retrieve 的阶段计时。

## 待审：shadow 延迟更新及审计 v2

用户已选择“继续设计 shadow 延迟更新与审计方案，先报告再应用”。下列内容仅为设计；0.12.6 未应用 lazy schema、compact shadow receipt、频次范围迁移或真实库迁移。全部应用须在审阅后另行明确批准。

| 原值／行为 | 拟议值／行为 | 分类与影响 |
|---|---|---|
| η=1、λ=.99；动态 6 位/静态 4 位 Python 舍入 | 完全相同 | 方法参数不变 |
| 每个新 hit 事件一个周期；重复/no-hit 不衰减 | 完全相同；增加 scope 内提交序号 | 运行/恢复表示，不按时间戳推断顺序 |
| 每轮物理更新全部 dynamic 行 | 推进全 scope 的逻辑周期，仅物化相关边及最多 6 个直接强化词对 | 持久表示改变；逻辑轨迹必须与 eager 相同 |
| weight/last_event_id 是物理当前值 | checkpoint 与当前逻辑值分开；所有读取走逻辑访问器 | 存储及读者兼容改变，需审批 |
| 首次 hit 对当时全部 memory_static 行 INSERT OR IGNORE seed | 冻结准确 seed 成员/分数/epoch，按需物化 | 表示改变；包含原 SQL 的全部成员（含舍入零/历史行），不覆盖已有 dynamic，不为后来的静态边 reseed |
| 每轮全图 changed_edges | 显式 v2：相关边完整逻辑转换＋不可变 cycle ledger 引用 | **审计合同改变，需审批**；整包/hash 不再是 v1 |
| 每轮全部 mark_frequencies | 相关边端点频次＋完整 n＋不可变 source revision 引用 | **审计范围改变，需审批**；否则仍有全 marks 成本 |
| 每轮独立完整全图验证 | 本地自足的相关数值验证；显式全链审计另读不可变依赖 | **验证完整性合同改变，需审批**；缺依赖标 partial/unavailable |
| 旧审计、预算、static 默认、来源门禁、去重/[M] | 原样保留 | 不扩额，不重新解释旧事件 |

精确补算从某条边的 checkpoint 开始逐周期执行 `F(w)=round(w*0.99,6)`，在对应周期再执行原强化 `round(w+1,6)`。不能用 `round(w*0.99**gap,6)` 或理想整数 half-even 代替；100 轮反例为 0.366035 与 0.366032，1000 轮为 0.000050 与 0.000043。小权重会停在非零固定点，不能假定最终归零。只有实际算得 `F(w)==w` 后才可跳过后续无强化周期；即使值不变，逻辑 last_event_id 仍指向最新周期。纯数值证据见 [SHADOW_LAZY_DESIGN_0126.json](../verification/SHADOW_LAZY_DESIGN_0126.json)。

补算成本为相关边数乘以缺失周期的精确递推步数；实际固定点可缩短此成本，但未建立任意 legacy 值、长 gap、枢纽邻域的 5 秒保证。未构建的二进制跳跃缓存仍要付构建成本。不得截断大值、改变舍入、加归零阈值或暗增预算来通过测试。

新事件在同一事务内校验 event/mode、分配 cycle seq、按需补算到 t−1、执行当前衰减/强化、生成局部证据并提交 ledger/checkpoint/audit。首次直接创建的无来源词对从零强化，当轮不补 decay；inactive/unsupported 历史仍持续逻辑衰减，却不能绕过活跃来源 gate。重复及 no-hit 不推进周期；首次 seed 之后的静态新增不 reseed。任一步失败全部回滚。

完整历史重建需要不可变的 checkpoint/birth/seed 成员、连续 cycle/hash 链、稀疏强化，以及观察前/选择后对应的 source revision：当时支持 ID、context、co_count、static score、n 和频次。当前 support/static 会被重建，单靠现表和 hash 引用不足以恢复历史证据。修订的全局构建/hash 放在材料发布或离线迁移，完整逐轮旧格式重建是显式只读审计工作，移出回复链。依赖遗漏、篡改或不连续不能报告完整验证。

scratch 局部记录须冻结相关边 checkpoint、gap、强化与来源频次证据，独立重算相关数值；这不证明没有遗漏全图强化。全链验证另外检查不可变 ledger/manifest 的连续性和成员完整性，不能读当前 mutable 数据代替冻结证据，24 小时原文清除合同保留。保存新长期 manifest 的内容、范围与保留策略必须在应用前评审，避免变成过期 scratch 原文的外部副本。

所有 dynamic 读取都须适配，包括 observer 的原始 weight/last_event_id 和 SQL 排序；物理 checkpoint 不能显示成当前权重。全图观察仍可有独立全局成本，但不能放回复链。任意自定义逐行 UPDATE trigger 的副作用无法自动保持；未封存来源修订或未验证 trigger 必须明确兼容边界，不静默替换证据。冷启动/材料变更的索引重建也仍有全图成本，若要求包括这些情形都只随邻域增长，还需另行设计发布时的持久查询索引，不能仅靠 lazy shadow 宣称完成。

迁移仅在批准后、无活动实例、持有 state 租约时进行，先一致 SQLite backup。以当前完整物理状态建立新 epoch 的 seq=0 checkpoint，保留原 weight/seed/context/last_event_id/seeded 标志及旧 audit，不推测旧缺失事件顺序。旧事件 replay 继续链接原 JSON/hash，不生成新 cycle；未知 legacy 不补造。新库必须用实际数据库写入防护阻止旧程序按 eager 方式写 checkpoint；仅增加旧程序不读取的版本标记不足以构成防护，须用旧源码实测拒绝且不产生副作用。回退须停服后完整物化、验证及备份，经批准操作，不能热补丁运行实例。

审批后的 pilot 先用合成库：逐轮 eager/lazy 对照所有逻辑边及相关材料/排名/证据规范字节，覆盖 seed 与已有行、后来新增、不支持词对、撤下/合法新来源、no-hit/replay/跨模式、各边不同 checkpoint、百万 gap、浮点边界、固定点和异常旧值；验证事务失败/并发/读快照及旧事件不可变。再测固定邻域 1000/3000 marks、增长枢纽、冷/暖/首次 seed 的 p50/p95/max 和完整本地冻结。完整旧格式重建也须与 eager 全量字节一致。Pilot 通过前不迁移真实库、不扩大任务，不宣称最坏情况或任意规模满足 5 秒。
