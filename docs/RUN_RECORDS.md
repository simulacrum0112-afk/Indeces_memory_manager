# 可核验运行记录：0.8.0

运行记录回答三个不同的问题：本轮召回了哪些材料、Hebbian 权重和排序如何变化、模型生成/Discord 实际送达的文字出现了哪些引用标记。它们不自动证明引用段落得到材料的语义支持，也不证明材料本身真实或 Hebbian 治理有效。

所有检查使用可观察输入、输出和状态，不要求隐藏思维链。新增记录与本地校验不增加模型调用；模型、图公式、阶段和知识版本的 token/时间预算保持既有基线。

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
| `memory_observation` | 图事务提交后立即记录 audit 与 audit hash；即使随后材料冻结或本地时间检查失败，仍可查已提交权重的回执 |
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

图审计按 `(scope,event_id)` 绑定查询。首次事件的审计与动态权重更新在同一个 SQLite 事务内提交；首次审计保持不可更新/删除。同一事件重放记录原审计的 hash 链接，不重新执行学习和衰减；无字面命中明确记录未应用观察。

审计包含直接字面命中、seed/decay/reinforce 的每一步、原权重与最终权重、实际改变标记、来源支持 ID、共现计数、静态 NPMI 计数/分数、一跳候选、排名依据和被选材料。保留 `η=1`、`λ=.99`、动态六位/静态四位舍入及既有限额。

校验器依据冻结的字段重算权重步骤、NPMI 和排名算术，把排名使用的边与实际变化后的有效边关联，并检查被选记录、预览 hash 与材料一致。当前没有独立重建全部字面命中、上下文门控及候选扩展资格。它检查所记录证据的内部一致性，不能单凭本地记录证明作者没有遗漏图中的记录或边，也不证明排序更相关。

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
