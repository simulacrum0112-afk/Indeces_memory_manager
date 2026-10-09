# 可核验运行记录

新加载的 Runtime 在 `turn_start` 声明 `npmi_telemetry_version=1`，每轮首次检索新增独立 `npmi_retrieval` 事件，以 `trace_id/event_id` 关联本轮。`telemetry` 记录实际执行路径、完成/失败状态，以及静态边读取、一跳邻居排序、候选评分和静态重建各阶段的调用数、完成数和耗时。`precomputed_npmi_used` 表示实际读取/使用了已存 NPMI 权重；`formula_recomputed` 与公式尝试/完成次数分别记录是否在该次检索中重新计算公式，不能把静态权重使用说成公式重算。没有命中边时明确记录零；异常保留已发生的步骤，仅记录异常类型，不记录异常原文。

计数来自执行位置，而非事后把 `weight_basis=static_npmi` 翻译成调用标记。候选计数、候选边证据使用次数和相关边数量与本轮图审计交叉校验。阶段计时为包含子调用的协作式实测时间，不能相加当作互斥耗时，也不证明操作系统级抢占。该事件仅覆盖当前静态检索；显式低层 legacy/dynamic 路径标为 `legacy_outside_coverage`，使用状态为未知，不伪写未使用。维护发布及离线公式核验不属于本轮检索计数。

0.17.0.dev11+realentry2 将该合同接入 `prepare_retrieval → await choose → finish_retrieval`。模型选择等待期间暂停遥测并恢复外层 ContextVar；本地检索总耗时排除该等待，后续重查询不串入初始计数。初始图审计是交叉校验的唯一计数依据。预算或账本初始化在检索前失败时，记录一次 `failed/not_entered`、零阶段、零计数和零耗时；选择失败或取消保留实际已执行步骤。

声明版本的 v1、v3 和截断记录均检查事件唯一性、身份、状态、阶段及计数绑定。仍在等待初次选择的有效前缀可以是 partial；保留本轮起点并已出现后续检索、上下文或回答证据时，缺失遥测不能借 partial 跳过校验。起点已过期的旧截断片段不补造声明。早期失败的诊断写入再失败时继续原有预算停机和失败收尾，不重试；缺失事件仍明确校验失败。正常检索事件写入失败会中止回答流程。

该扩展不改变 NPMI 公式、图参数、候选/排序、模型额度、配置、不可变图审计或数据库 schema；旧无声明日志继续按旧合同检查，不补造历史执行标记。事件遵循 scratch 原有 UTC/hash 链及滚动 24 小时保留。源码变更需要新的 Console/Runtime 加载才会生效，现有运行实例不会由本次代码编辑自动更新。

0.13.2 的显式维护任务使用追加式独立账本，详见 [BOUNDED_REINGEST.md](BOUNDED_REINGEST.md)。每次 HTTP 请求在发送前持久保存独立 `X-Client-Request-Id`；供应商 Request ID 在正文读取前记录，Response ID 与实测 usage 在 scratch/输出校验前落盘。scratch 增加这些定位元数据，原 payload 与滚动 24 小时合同不变。维护账本不另建原文输出档案；额度预留、已确认实际 usage、未知 usage 和崩溃后的保守占时分列，不能相互替代。旧未知调用按 operator 的明确决定关闭为未量化损失，保留原失败与计量，不伪写零 usage；新未知调用仍停止整批且不得自动重放。45 秒等覆盖设置仅属该维护任务，全局参数与普通调用保持。

0.12.4 的检索记录仍为 `version=1`，新增 `model_projection="citation_material_v1"`，声明实际送给模型的固定字段视图。每条保留 `id/source_id/scope`、完整 `quote`、原110字符预览及截断声明、`marks/direct_marks/expanded_marks`、排序模式/依据/三个分数、`citation_id/citation_marker` 和可选 PDF 物理页码。完整支持记录 ID、图 context、权重转换和排序证据继续在 `graph_audit`；完整来源版本目录、原文和物理页位置继续冻结在材料与来源目录，不复制进模型请求。

两个独立校验入口从冻结材料和已选候选重建视图，核验键集合与规范 JSON 字节；未知视图标记拒绝，删除标记不能将精简材料降级为旧合同。旧无标记记录继续要求完整 evidence 与候选绑定，保留旧动态/无模式审计兼容。`reply_context` 与实际模型请求仍严格绑定 `model_materials`，答案/送达的 `[M]` 关联保持。旧记录不重写；完整图审计字节不变，整个 retrieval receipt 和模型输入因视图修复而变化。

固定上下文的原 UTF-8 字节预留、水位摘要策略及供应商实际 input token 门禁保持。视图不是截断原文或扩大额度，不能保证任意长当前消息与完整材料一定可容纳。

0.12.3 仅替换内部材料缓存与查找索引；完整全图 edge_statistics、全部频次/邻居/候选决策与 shadow 转换均保留原规范字节，图审计 schema、来源绑定和独立校验不变。缓存私有对象不进入 scratch，仍输出每轮拥有的独立数据树。

0.12.6 静态索引路径声明 `edge_statistics_scope=direct_hit_incident_v1`，只记录与 direct hits 相连的全部活跃边，包含未通过 top-k/context gate 的一跳边。相关行、排名/去重/门控决策及返回材料的规范字节保持；整包/hash 随显式范围改变。全图 `live_edge_count`/频次和全动态 `changed_edges` 仍按原合同记录。校验器保留旧完整记录兼容，拒绝未知范围、范围外或缺失的相关边及不一致证据；重放不重写原事件。详见 [MEMORY_QUERY_SCOPE.md](MEMORY_QUERY_SCOPE.md)。

新的存储层邻域路径使用图审计 `schema_version=2`、`audit_scope="direct_hit_neighborhood_v1"`。`selection.edge_statistics_scope="direct_hit_incident_v1"` 包括直接命中的全部相关 incident edges 和原有的一跳接受/拒绝证据，不能只保留 top-k 接受边。`selection.mark_frequencies_scope="edge_endpoints_v1"` 声明频次仅包含这批边的全部端点；`active_record_count` 和 `live_edge_count` 仍是完整 scope 的标量计数，由摄入索引维护，并非本轮扫描/冻结了全图。NPMI 校验仍使用完整记录数与相关端点的原频次，公式、co_count、舍入及来源支持不变。

此 v2 合同的静态路径不提交动态或 shadow 更新，`observation.dynamic_shadow_enabled=false`、`applied=false`、`changed_edges=[]`、`seeded_edges=0`；已有动态分数只读作历史兼容字段，不作为静态排名依据。新事件状态为 `disabled`，重复事件为 `replay`，无完整旧审计的重复事件可为 `legacy_replay`。重复事件始终链接原不可变 JSON/hash，旧包、旧实际转换及已保存的来源版本不改写。显式低层 dynamic 与历史 v1 全图/相关边合同保持其原有验证分支。

快照、材料冻结、规范编码与独立校验只消费这份已声明的邻域树，不另取全图边或全部标注词频次。来源冻结仍保留实际选中来源的完整版本、原文、标词及 PDF 页码，`[M]` 与模型材料投影不变。Console 运行校验为 v2 添加 `graph_audit_hit_neighborhood_only` 告警，观察页显式显示范围。`complete` 仍只表示已声明范围内的运行记录关联与算术一致，**不表示全图状态、全部动态历史或全图成员完整性已在本轮审计**。

相关边的结果、排序、支持 ID、context、计数及冻结材料可在同一冻结动态状态下与旧全图选择器逐规范字节比较。停止原有 shadow 更新后，新事件动态字段不能与旧版继续逐轮更新的实验声称相同；必须分别报告静态结果/证据对照与这一明确行为变化。审计包因为版本、范围、频次投影及观察声明变化，整体字节和 hash 不同，不能宣称整包逐字节相同。删去/伪造范围标记、把 v2 降级成 v1、范围外边、缺少相关端点频次或动态写入声明均拒绝。内部一致性校验不能单凭局部树证明数据库没有省略边；实现的按键工作量测试和旧选择器对照另外验证摄入与查询索引的范围完整性。

0.12.2 用 `FailureNotice` 标识机器人固定失败回执。`discord_delivery_started` 对此保存 `delivery_kind="failure_notice"` / `notice_text`，不保存 `model_output`；送达完成/失败事件也标注种类。`failure_notice_delivered` 保存实际文字和 ID，但本轮仍是 failed，不创建 assistant 历史、生成答案或模型回答的送达记录。没有增加模型调用、自动重试或轮次退款；模型 `skip` 禁止这类发送。总轮时间耗尽时保持 `failure_notice_skipped=turn_time_exhausted`。现有运行校验区分 failed 与 skipped，不据此声称独立核验所有 Discord 失败回执事件的语义关联。

不可变审计快照共享规范化 bytes 与 hash；日志字段/schema 保持，材料冻结仍独立生成字典并重新计算 hash 校验。旧日志不重写，完整证据与严格重复键/非有限数值校验不削减。

0.12.1 的失败 `turn_end` 增加 `error_type`，SQLite 异常记录库提供的整数 `sqlite_errorcode` / `sqlite_errorname`；本地检索失败记录 `phase`（`retrieval`、`memory_observation`、`freeze_retrieval`）及原 `local_seconds`。仅由本轮时间门禁实际触发的 `SQLITE_INTERRUPT` 转为 `local_memory_timeout`；锁、SQL 错误和外部中断仍保持数据库错误类别，不记录任意异常原文。Python 同步工作末尾仍检查累计时间；这是协作式限制，不是硬抢占。scratch 优化保持原规范 JSON 字节、hash 链、严格输入校验和 flush/fsync 合同。

0.12.0 的机器人输入在 `turn_start.input` 标识 `author_is_bot=true`，`reply_context.response_schema` 绑定实际 Responses 请求的结构化决策合同。`bot_reply_decision` 保存 `action=reply|skip`、解码文字、原始模型 JSON、计量与召回关联。选择 `skip` 时只出现 `turn_end.status=skipped`，没有生成答案、assistant 历史或送达记录；校验器单独统计 skipped，并核验决策、实际请求/响应及用量，不能把静默等同于未发生模型费用或已发送空回复。选择 reply 则冻结解码答案与实际 Discord 文字，受控 @ 和截断属于传输差异，仍分别核验引用。模型失败保持 failed；0.12.0/0.12.1 当时对机器人失败静默，0.12.2 按用户要求改为剩余总轮时间内发送固定失败回执。

传输准入记录另保存机器人轮次与最近人类重置 epoch；持久预占最多五次，跳过/失败不退款。完整历史已有旧人类记录继续按旧合同检查；机器人决策或起始记录超出滚动窗口时仍显示 `retention_partial`，不能恢复或伪造过期证据。此记录合同证明可观察关联，不证明模型正确理解所有收尾消息。

0.11.0的`call_start`成对记录`requested_input_limit`与`effective_input_limit`；有效门禁是原阶段input cap与调用者剩余额度中的较小值。`input_gate.limit`绑定有效门禁，`stage_limit`绑定原阶段额度；实际计量和完成usage须满足有效门禁。失败仍保存供应商确认的真实用量，不丢弃或双计。旧记录缺新字段时按原阶段核验，部分保留窗口只校验存留门禁。累计知识额度的批准迁移见[KNOWLEDGE_DIRECTORY.md](KNOWLEDGE_DIRECTORY.md)，不重置历史计量。

2026-10-03 文档核对；本合同对应的远端代码与验证范围见 [CHECKPOINT.md](CHECKPOINT.md)，不重写旧运行记录。

运行记录回答三个不同的问题：本轮召回了哪些材料、Hebbian 权重和排序如何变化、模型生成/Discord 实际送达的文字出现了哪些引用标记。它们不自动证明引用段落得到材料的语义支持，也不证明材料本身真实或 Hebbian 治理有效。

所有检查使用可观察输入、输出和状态，不要求隐藏思维链。新增记录与本地校验不增加模型调用；模型、图公式和单次阶段的 token/时间预算保持既有基线。0.11.0 的知识版本累计资源额度另经用户批准迁移，记录门禁修复本身不扩额。

0.10.0 的 selection 与每份冻结模型材料增加 `ranking_mode="static"`、`weight_basis="static_npmi"`。校验器绑定模式与排序算术；动态变化仍是 shadow 观察，不能被解释为当前排名依据。旧无此字段的图审计仍按旧动态实现解释，不重写历史，也不能追认为静态实验。图/检索记录 schema 与 SQLite 格式没有迁移。

模型调用另记录 phase、slot_wait_seconds、远端请求/生成是否已发以及是否收到合法 usage。`model_slot_timeout` 仍耗尽原阶段时限，但只有本地等待，没有远端调用，不计入供应商熔断失败。收到合法 usage 后即便输出校验失败，仍保存本次已知计量；“未知为 false”不表示模型输出已通过或已送达。计量请求与生成用量分开描述，不把尚未生成的取消写成未知生成费用。

新增 `call_start.usage_receipt_version=1` 声明用量回执合同：失败调用收到合法生成 usage 时，`call_end.known_usage` 必须保存输入/输出 token，并与该次实际 HTTP 响应及请求绑定。合法 usage 后的格式错误、超限、取消或本地审计异常仍计入已知用量，不能把已发生费用当成零或再次计入。旧无此声明的记录保持兼容，不补写历史；已发生成但没有合法 usage 的失败仍标为未知。

0.13.1 将知识转换／标词失败与取消的逐篇通知显示在 Console 的 service/logs 输出中：文件、失败阶段、code、已标词块数与恢复限制。成功与普通进度仍只写回执。`knowledge_receipt` 增加 `failed_chunk_index`（零基；没有当前失败块时为 null），逐文件持久审计的 details 保存块位置和完成计数，便于 scratch 到期后定位；不补造旧失败的位置。重开服务发现遗留 labelling 时，创建／更新持久失败审计并显示 `interrupted_unknown_usage`，不自动重放。通知转义文件名中的终端控制字符，不显示异常原文或认证信息。模型、时限、累计额度、单槽与整篇事务发布规则不变。

0.8.0 增加 `call_start.verbosity`，绑定计量与生成请求中的 `text.verbosity`；`reasoning.effort` 继续绑定该阶段预算。默认话量为 `high`、三阶段推理为 `low`，生成仍受原输出/时间上限。旧记录两端均没有 verbosity 时保持旧合同兼容，声明与实际请求不一致列为 invalid；API key 的设置/解密不进入 scratch。

## 文件与版本

记录追加到 `scratch/` 的日期 JSONL 文件。文件名采用创建该日志实例时的 UTC 日期；长时间运行的实例不会在午夜自动换文件。每条普通事件采用外层 `version=1`，含原整数 `sequence`、UTC ISO 时间、`event`、对象 `fields`、`previous_hash` 和 SHA-256 `hash`。hash 对移除 `hash` 后的规范 JSON 计算；JSON 为 UTF-8、键排序、紧凑分隔，拒绝重复键和非有限数。裁剪后序号继续从真实尾序号追加，不重新编号；第一条保留事件链接 retention checkpoint 的原前缀锚。

0.4.0 引入的 `turn_start.fields.run_record_version=1` 声明本轮具有下面的记录合同。它独立于外层日志版本、检索记录的 `version` 和图审计的 `schema_version`。保留窗口内缺少这项声明的旧对话列为 `legacy`，不补造当时没有记录的证据；旧格式到期日志也遵循 24 小时清理。

## 滚动 24 小时保留

服务先取得实例锁，再在启动时清理；运行期间每 60 秒检查一次，即使没有消息也执行。UTC `timestamp <= now - 24h` 的事件到期；按记录时间裁剪过期前缀，而不是按文件名或 mtime 删除整天。在线清理会有维护间隔与本地 I/O/调度延迟，停服期间不运行，下次 `start` 先清；没有外部计划任务。`scratch` 命令只读检查，不触发删除。

保留事件行的原字节、序号和 hash 不变。文件首行可有 `kind=scratch_retention_checkpoint`、`version=1` 的独立声明，字段包含 `removed_through_sequence`、`removed_head_hash`、`pruned_at`、`cutoff`、`partial_trace_ids`、`partial_call_ids`、`cleanup_pending` 和自己的 hash。它没有原文或模型 payload；只留仍有窗口内片段的 opaque ID。全过期的非当前日期文件删除；当前写入文件即使正文全过期，也用 checkpoint 延续序号/hash。跨文件过期起点的声明放到仍有对应片段的文件，起锚可为序号 0/零 hash。

UTC 时间须非递减；相等时间允许。时钟回拨、未来原记录、非法结构或损坏链会拒绝写入/清理，不伪造时间，不借保留策略修复日志。清理只枚举 scratch 根内正规日期 JSONL，拒绝链接、reparse point 和越界路径，不递归处理未知文件。专有 `.scratch-retention-<uuid>.tmp` 为未提交替换副本：启动持锁后清理这些普通孤儿文件，不读正文，不保留其额外副本。

每个文件采用同目录临时文件、flush/fsync 和原子 replace；Windows 当前写入句柄先关闭再替换、重开。先持久化所有受影响文件的截断 ID 声明，再裁剪前缀，最后删除全过期文件；目录没有一个共同原子事务。中间失败可能留下 `cleanup_pending=true` 和尚未清完的原行，服务明确停止，无成功清理回执；校验器显示待完成，不能说已满足保留期限。完成声明为 `cleanup_pending=false`，保留行须晚于其 cutoff。下一次启动基于现有可验证原链执行清理，不重放模型调用。

清理数量、文件数量与 cutoff 在 Console/scratch 有回执。这项策略仅删除 scratch 中的到期日志；知识文件、SQLite 消息/来源版本/checkpoint 和持久图事件审计不在删除范围。到期日志不自动归档；窗口外完整调用/引用证据将无法再从 scratch 查阅，已有 checkpoint 只证明剩余链与本地声明一致，不能恢复或公证已删除内容。

## 一轮回答的证据链

0.7.0 的 PDF 来源额外冻结转换元数据（原 PDF/Markdown hash、提取器/策略版本、物理页区间和提取警告）。材料保留所有相等分块的位置和物理页序，并与模型输入及字面引用回执核对；重复内容不能据此声明唯一页号。PDF 原字节在版本化 SQLite 中保存，不进入 scratch，故 scratch 的 original_file_bytes_verifiable / original_pdf_bytes_verifiable 仍为 false。观察页可独立有界核对归档字节；转换一致性不证明版式、公式、表格或语义支持。

| 事件 | 记录与关联 |
|---|---|
| `turn_start` | `trace_id`、Discord message ID、聊天 scope、knowledge scope、原始/规范化输入与本轮预算 |
| `npmi_retrieval` | 实际静态 NPMI 权重使用/公式重算标记、路径、各阶段调用/完成次数、相关边与候选证据使用次数、耗时及失败类型 |
| `memory_observation` | 图事务提交后立即记录 audit 与 audit hash；v2 明确记录 shadow 关闭及邻域选择，历史 v1 保存当时实际已提交的权重转换 |
| `retrieval_record` | 在检索后、任何 await 前冻结的查询、event ID、完整材料和来源目录、图审计及记录 hash |
| `knowledge_retrieved` | 提供给后续上下文的材料与同一个检索记录 hash、检索耗时 |
| `reply_context` | 实际准备调用的 instructions/messages 与检索记录 hash；短期上下文与长期材料分开 |
| `call_start` / HTTP 记录 / `call_end` | 既有 adaptor 的计量、请求/响应、usage、时间、成功/失败/未知计量；由 trace/call ID 关联 |
| `answer_generated` | 完整模型输出、输出 hash、字面引用位置与对应材料；链接同一检索记录 hash |
| `answer_delivered` | Discord 确认送达的实际文本、独立引用位置与 message receipt IDs |
| `turn_end` | 本轮终态和送达回执；失败/中断可以在较早阶段结束 |

模型完整输出与实际送达文本分开保存；Discord 长度截断可能改变引用出现情况。已经取证并等待模型的回复保持检索时快照与 trace，知识发布切换/删除不会事后改写该证据。

## 召回材料与来源版本

每轮最多三个材料，依次分配局部 ID `M1`、`M2`、`M3` 和可见标记 `[M1]` 等。这些 ID 只在本轮有效，通过 `source_id`、数据库 `record_id` 和 scope 绑定真实来源。

| 冻结字段 | 可核验含义 |
|---|---|
| `materials` | 原存储文本、完整 quote、标注词、文本/quote 指纹、检索时 active 状态、块 ID 与来源 ID |
| `model_materials` / `model_payload` | 本轮提供给模型的预览、完整 quote、标注词、排序依据和引用标记；不会把来源目录全文额外塞入模型输入 |
| `sources` | 召回来源的版本目录：路径、`raw_text`、创建时间、scope、状态、分块文本/标注与检索时 desired/published 指针 |
| `normalized_text_sha256` | 对已保存的解码文本 UTF-8 再编码后计算的 SHA-256，可以只靠冻结文本重算 |
| `original_file_bytes_sha256` | 文件入库时原字节的 digest 元数据；本记录不保留原字节，`original_file_bytes_verifiable=false` |

解码文本去除了 UTF-8 BOM；不能从这份文本证明原文件字节，包括 BOM，完全相同。因此文本 hash 与原字节 digest 是两种证据，校验器不把后者宣称为已重算通过。

当前知识来源须在检索时属于该 scope 的完整 `ready` 已发布版本。正常编辑期间，desired 可以指向新版，published 仍指向旧版；被冻结的是当时实际召回的旧原文、旧标签和旧来源，而不是事后读取当前文件替换它们。非知识文件的合成/导入来源没有版本目录时明确标记元数据不可用，不伪造文件信息。

## 图权重与排序

图审计按 `(scope,event_id)` 绑定查询。首次事件的审计在 SQLite 事务内提交；历史 v1 将当时的动态更新放在同一事务，当前静态 v2 不执行动态或 shadow 更新。首次审计保持不可更新/删除。同一事件重放记录原审计的 hash 链接，不重新执行学习和衰减；v2 有命中/无命中都明确记录 shadow 关闭。

历史 v1 审计包含直接字面命中、seed/decay/reinforce 的每一步、原权重与最终权重、实际改变标记、来源支持 ID、共现计数、静态 NPMI 计数/分数、一跳候选、排名依据和被选材料。v2 保留相关边的支持、计数、静态分数、一跳决策与排名证据，并显式缩减到命中邻域及端点频次；不补造未发生的转换。保留 `η=1`、`λ=.99`、动态六位/静态四位舍入及既有限额；动态参数当前仅属于保留的显式低层动态路径与未激活的结构预留。

校验器依据冻结的字段重算 NPMI 和排名算术，并检查被选记录、预览 hash 与材料一致。历史 v1 另外重算当时发生的权重步骤；v2 只在声明的邻域范围校验相关频次/边/候选证据，要求 dynamic/shadow 未应用。静态模式按正静态权重核对排名，历史 shadow 变化不能当作当前静态排名变化。旧动态模式按其动态有效权重核对。同事件重放绑定原模式，模式不同在写入前拒绝。当前没有独立重建全部字面命中、上下文门控及候选扩展资格。它检查所记录证据的内部一致性，不能单凭本地记录证明作者没有遗漏图中的记录或边，也不证明排序更相关。

## 召回、引用与语义支持

| 状态 | 能说明什么 |
|---|---|
| 召回材料 | 检索选中且记录了材料；不说明模型采用了其中某个结论 |
| `resolved` 引用 | 输出中出现本轮合法 `[M1]` 一类标记，可以绑定到材料/来源；不证明段落由材料支持 |
| `unresolved` 引用 | 出现 `[M…]` 标记但无法绑定本轮材料，校验结果明确告警 |
| `uncited_material_ids` | 本轮召回材料没有对应字面标记；不推断模型是否暗中使用 |
| `semantic_support=not_evaluated` | 未做语义蕴含或事实核验 |
| `citation_coverage=not_established` | 未证明所有结论或事实句均有正确引用 |

解析器只识别字面的 `[M…]`，记录出现位置与所在段落。位置采用 Python Unicode 字符偏移，起点包含、终点不包含；段落按双换行分隔。引号、代码块或用户复述中的标记也会计入字面出现，不能解释成模型认可。重复标记保留各次出现，未知标记明确列出；没有标记是可观察结果，不自动判定所有结论错误或语义覆盖完整。

## Console 校验

在 Console 输入 `scratch`，或运行：

```powershell
.\.venv\Scripts\python.exe -m indeces scratch
```

命令先校验各 JSONL 的外层结构、连续序号和 hash 链，再跨文件按 trace 检查新运行记录合同，按 call ID 报告 adaptor 的 `call_counts`。完整调用核对计量与生成请求的共同输入、两次响应、input gate、call_start 预算、实际 usage/时间和 call_end 文本；reply_context 还与实际 `/responses` 的 instructions/input 对照。检查不调用模型，不读取当前知识文件或 SQLite 来替换历史证据，CLI 不打印原文或完整回复。

| 报告状态 | 含义 |
|---|---|
| `complete` | 新合同的完整送达链符合记录关联和内部一致性检查；可能仍有引用告警 |
| `failed` | 有合法失败终态，已有阶段证据符合合同；不表示模型/Discord 调用成功 |
| `incomplete` | 缺少终态等证据，不能以 hash 合法代替流程完整 |
| `invalid` | 合同字段、hash 关联、算术、材料/引用/送达绑定等不一致 |
| `legacy` | 没有新合同声明的旧对话；不补算或假定其已满足新合同 |
| `retention_partial` | 起点因计划清理到期，而窗口内仍有片段；只检查现存可验证字段，不推断过期的材料或预算 |

外层结构/hash 错误或 `invalid` 会使校验命令失败；`failed`、`incomplete`、`legacy`、`retention_partial` 和引用告警分别显示。只有 checkpoint 明确列出的缺起点对象才能列为 `retention_partial`；起点仍在的对象沿用完整合同，不因目录存在 checkpoint 就放宽校验。部分记录的材料 hash、引用位置/可用绑定、HTTP 响应/usage、保留门禁与输出上限仍校验，已有矛盾仍为 invalid；依赖已到期时明确告警，不补造。调用同样单独报告部分状态；被动标词 trace 不当成回答链。`complete` 仅是记录合同完整，不是语义、事实或长期记忆效果验收。

## 写入失败与证据边界

Scratch 在序列化成功之前不推进序号，在 append/flush/fsync 全部成功后才推进本地状态。可能已写入后的 I/O 失败或中断会将当前 writer 标记不可续写；原文件保留，不自动截断、重写或修复。预写入序列化错误不污染 writer。启动时拒绝结构/hash 损坏的已有文件；完整记录能在本地读回，也不能反证之前失败的 fsync 已保证落盘。

SQLite 图/审计事务与 scratch fsync 是两个持久性边界，没有跨数据库与日志的原子事务。进程退出或日志失败可能留下数据库已提交、scratch 缺失的阶段；校验器不能补造这些阶段。已经收到的 Discord 送达确认不会因后续本地日志失败改称网络送达未知，也不会自动重复发送；存储/日志失败会明确提示，清理仍尝试关闭组件、数据库和实例锁。

hash 链没有外部锚点。中间记录删除、内容篡改和部分行损坏可以检测；在完整行边界删除整个尾部、删除整份日志、重新生成整条链不能仅凭剩余本地文件证明不存在。日志采用上述滚动 24 小时保留，没有压缩或外部公证；窗口内原文、完整调用及回复供用户本地查阅，认证密钥不能进入记录。
