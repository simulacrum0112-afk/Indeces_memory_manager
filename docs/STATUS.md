# 0.12.5 路径边界修复

最终全套932项、0失败/错误、20跳过，隔离安装包通过，源码/editable元数据0.12.5。证据链接与真实实例加载限制见 [CHECKPOINT.md](CHECKPOINT.md)。

修复配置任意知识目录、安全错误后继续入库、根路径链接先解析和旧来源缺少根身份四个已证实缺口。OneDrive与链接在副作用前拒绝，旧来源迁移先备份与完整byte digest核验，来源撤下不删除历史或重置未知usage。普通PDF文件缺失的具体原因尚未证实；已审计主要施工命令成功断言项目路径，没有被报告外目录的移动/删除证据。详见 [PATH_BOUNDARY.md](PATH_BOUNDARY.md)。本轮不启停服务、不对活动真实数据库应用迁移。准确测试/推送状态见 [CHECKPOINT.md](CHECKPOINT.md)。

首次全套等待测试因旧维护清理夹具缺少 knowledge_dir 而在创建资源前失败，夹具的无超时 Event 等待掩盖了异常；仅中断本轮自建的离线测试进程，并补齐路径及有界等待。首次保存的 [OFFLINE_0125.json](../verification/OFFLINE_0125.json) 为928项、7错误、20跳过：清理与观察等待夹具也缺完整目录；修正夹具，保留原失败记录，没有放宽生产路径门禁。后续独立复查另补备份/转换稿/扫描子树与数据库只读入口检查；目录检查失败仍按不完整清单保护旧发布。

# 交付状态：0.12.4

2026-10-02（用户时区）修复已成功检索的机器人消息在回复模型之前发生 `fixed_context_too_large`。事故记录证实0.12.3已加载，入口、检索与材料冻结已完成，失败发生在固定上下文规划，没有调用模型。诊断只读 Indeces 自身相关配置、日志链与消息元数据，在内存复算同一输入；未导出真实数据、访问其他智能体数据或启停服务。

根因是材料冻结把完整选中记录复制进 `model_materials`，其中图 evidence、支持记录清单、context 和仅供审计的作者/时间字段重复占据固定预算。现在使用显式 `citation_material_v1` 视图，保留完整 quote、预览、标注词、来源/[M]/PDF页码与排序摘要；完整 evidence 留在图审计。独立校验从候选和材料重建视图，未知/删除标记不能降级绕过；旧完整视图及旧无模式动态记录保持兼容。没有改检索结果、NPMI、排序、完整图审计、本地5秒/模型额度、并发或五轮规则。

全合成比较 [MODEL_CONTEXT_0124_20261002T213232430876Z.json](../verification/MODEL_CONTEXT_0124_20261002T213232430876Z.json) 与固定公开0.12.3 freezer对照：25/100/200条相同四词记录、召回三条，旧模型包7927/12014/19216字节，均在零历史下规划超限；新视图2608/2609/2611字节，原16384 input cap和4096摘要预留下均准入。完整图审计规范字节直接比较一致，选择结果/数据库/来源目录/完整材料保持；整个 retrieval receipt 和模型视图按新合同变化。这里是保守UTF-8字节预留，不能当作实际token计量。复现脚本为 [verify_model_context.py](../verification/verify_model_context.py)，早期同数值hash比较记录也保留。

最终 [OFFLINE_0124_20261002T213550181440Z.json](../verification/OFFLINE_0124_20261002T213550181440Z.json)：887项、0失败、0错误、14跳过、68.188秒，新增15项及CLI/Console/依赖/只读npmi通过。[WHEEL_0124.json](../verification/WHEEL_0124.json) 隔离构建/安装、版本、静态默认和资源检查通过，源码/editable均0.12.4。人类/机器人端到端合成请求、真实schema/Unicode/JSON转义边界、摘要水位、完整输入、引用和PDF绑定、新旧视图篡改检查均覆盖；模拟provider只证明接线，不证明真实回答。

首次 [OFFLINE_0124.json](../verification/OFFLINE_0124.json) 为895项、1错误、14跳过：旧快照复制测试只提供任意audit字典，没有候选，无法满足新视图独立绑定。修正为在真实图审计上附加同一非原生嵌套测试值，仍验证深复制，不放宽生产绑定；新测试导入TestCase导致原有8项重复发现，也改为模块导入。相关24项重跑通过后完成最终全套，不删除失败证据或把首次错误称为通过。

运行源码 [`3a422d1`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/3a422d1dcdc9006e7879d147aa3801809e6b1902) 已推送main，精确提交 [CI 37067919192](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37067919192) 整体与Windows/Ubuntu job、unittest、CLI check步骤均success，见 [CI_0124.json](../verification/CI_0124.json)；只读公共元数据，不读日志/推断远端测试数量。[公开范围审计](../verification/REPOSITORY_AUDIT_0124.json) 覆盖此前main的28提交/492唯一blob与181公开文件，未知发现/禁止路径零。最终仅文档/CI补扫见 [REPOSITORY_AUDIT_0124_DELIVERY.json](../verification/REPOSITORY_AUDIT_0124_DELIVERY.json)，保持运行源码不变。

真正过长的输入仍可能超限，完整审计仍有全图成本；真实模型收尾判断/Discord往返未验收。真实实例不自动更新，用户须stop、等清理、quit、重开Console再start加载0.12.4。

# 0.12.3 历史交付

2026-10-02（用户时区）按用户明确选择保留完整审计、结果和排序逐字节一致，优化 `memory.py` 的材料缓存与局部查找。known/frequencies/原记录序号倒排、前缀树、源边元数据/原边序号/字典序/静态邻接只在材料版本改变后重建。候选匹配仍由原 `_hit` 最终确认；选择只读取命中邻接与 allowed postings。一跳、前五邻居、两个额外词、三个出处、NPMI、corroboration/source gate、去重和 static/dynamic 分层不变。

缓存通过连接 TEMP revision 按 scope 监听 records/static/support，覆盖摄入/撤下/替换、rollback、同连接其他 Graph、直接 SQL/跨 scope REPLACE；外部提交、DDL、连接和 self marks 也使其失效。聊天状态和 shadow 写入不使静态索引失效；输出数据树不能污染缓存。TEMP 源表遮蔽与非法类型旧边保留原兼容路径；观察期间来源变化沿用旧实现的 records-before/edges-after 边界。没有新增持久缓存表、改写历史或改变预算/科学参数。

[MEMORY_INDEX_0123.json](../verification/MEMORY_INDEX_0123.json) 保存全合成、每组5次、包含无关聊天状态写入的完整 retrieve 基准：

| 场景 | 1000 marks 改前→改后 | 3000 marks 改前→改后 |
|---|---|---|
| 首轮建立索引/shadow | 0.199→0.061秒 | 0.697→0.211秒 |
| 已建立索引/shadow | 0.223→0.048秒 | 0.759→0.171秒 |

同一查询的 literal gate 次数由1004/3004降为8/8，暖查询不再读取/解码完整 records，选择只读≤4命中邻接与≤6allowed posting。完整records/audit规范字节及所有memory表摘要与改前基线在20个改后样本完全一致；发布/新增/撤下另行比较也一致。MB级合成基线只在独立临时目录，公开记录给出hash与复现脚本，没有真实材料或服务数据。

**尚未证明全链路线性缩放。** 改前两点倍数约3.50/3.41，最终改后约3.46/3.56，不能将低绝对延迟说成已经消除总耗时超线性。匹配/候选查找的全量扫描已消除，但完整审计仍约1.8→5.4MB，全部shadow边逐轮舍入/更新/审计必须保留；该成本不能在逐字节合同下改成只碰局部。这里计时的是 `MemoryGraph.retrieve`，不含Runtime后续独立冻结、scratch fsync或真实模型/Discord。另3次发布比较：3000marks代表性新增约0.118→0.151秒、撤下0.118→0.161秒，初次add约0.129→0.220秒；TEMP revision跟踪有已测发布开销。

最终 [OFFLINE_0123_20261002T210751360525Z.json](../verification/OFFLINE_0123_20261002T210751360525Z.json)：872项、0失败、0错误、14跳过、74.559秒；新增22项及CLI/Console/依赖/只读npmi通过。[WHEEL_0123.json](../verification/WHEEL_0123.json) 的隔离构建/安装、版本、静态默认和网页资源通过，本机源码/editable均为0.12.3。开发阶段866/870项通过记录保留，不代替最终872项。运行源码 [`de07bdb`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/de07bdb7437893e8009d1e74075bea8f92828ddc) 已推送 main；精确提交 [CI 37065149904](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37065149904) 的 Windows/Ubuntu job、unittest 与 CLI check 步骤均 success，见 [CI_0123.json](../verification/CI_0123.json)（仅公开元数据，未读日志/不推断远端测试数量）。[公开范围有限审计](../verification/REPOSITORY_AUDIT_0123.json) 覆盖此前main可达26提交/468唯一blob及170个公开文件，未知发现/禁止路径为零；本次交付补扫见 [REPOSITORY_AUDIT_0123_DELIVERY.json](../verification/REPOSITORY_AUDIT_0123_DELIVERY.json)，不改变已验证运行源码。 不将这些离线证据冒充实际加载或真实Discord验收。没有访问真实配置/数据库、启停或热更新真实服务；新Console进程显式start后才加载新版。

# 0.12.2 历史交付

2026-10-02（用户时区）根据新的真实消息故障报告排查：已加载 0.12.1，机器人输入已准入，在 `freeze_retrieval` 失败为 `local_memory_timeout` / `SQLITE_INTERRUPT`，随后旧机器人专用分支主动跳过失败提示；同样长输入由人类发送也在本地时间门禁终止，并能送达固定失败提示。因此本次有证据确认准入已通、检索累计耗时仍超限及失败回执静默策略问题；没有证据支持修改 webhook 或扩大入口。

0.12.2 按用户明确要求发送机器人失败回执：`FailureNotice` 类型与模型回答区别，桥接层回复原消息但不添加对方 @、关闭全部 AllowedMentions 与 replied_user。实际回执保存在 failure_notice/discord_delivery 事件，本轮仍 failed，无 assistant 历史、额外模型调用、重试或轮次退款。模型收尾 skip 仍静默；已经尝试发送回答后不补发回执，剩余总轮时间耗尽时诚实记录无法发送，不扩增预算。

检索现在只创建一份不可变规范 JSON 审计快照，scratch 复用同一份 bytes，材料冻结独立解码并重新计算 hash 校验；未使用 mutable dict 的身份缓存。普通 scratch/字典冻结保持原路径，完整日志字节、hash 链、严格校验、flush/fsync、来源与 PDF 回执不变。上下文词匹配缓存只限当前选择调用，使用原 `_hit` 规则，不跨消息或 scope。图公式、静态排名、shadow 更新、模型与本地 5 秒预算均保持。

[OFFLINE_0122.json](../verification/OFFLINE_0122.json)：850项、0失败、0错误、14跳过、65.531秒；新增35项及原回归、CLI/Console/依赖/只读npmi检查通过。[WHEEL_0122.json](../verification/WHEEL_0122.json) 的隔离构建/安装、资源与入口检查通过，源码与本机 editable 版本均为 0.12.2。机器人失败/静默/传输、快照逐字节兼容与冻结、上下文缓存等针对性合成测试已通过；运行源码 [`549fb16`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/549fb161a0a9c749f9f534ff0aba9cc5dd0a8247) 已推送 main，精确绑定的 [CI 37062176475](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37062176475) Windows/Ubuntu job、unittest与CLI check步骤均 success，见 [CI_0122.json](../verification/CI_0122.json)（未读日志/不推断远端测试数量）；[公开范围有限审计](../verification/REPOSITORY_AUDIT_0122.json) 无未知发现或禁止路径。完整结果及推送/CI 见 [CHECKPOINT.md](CHECKPOINT.md)。

诊断只读 Indeces 自身近期记录和配置；对同一输入的试算只写进程内数据库/日志 sink，未导出或上传原文/数据库/真实运行统计。当前输入在内存试算中可进入原预算，但模拟 fsync、资源负载仍有变化，不能作为实际磁盘路径或任意规模时限保证。没有读取 Yuki 私有数据、启动/停止/热加载真实服务、调用真实模型或发送真实 Discord 消息。旧 Console 要退出并重新打开再 start 才加载新版；真实往返与模型收尾判断质量仍待验收。

# 0.12.1 历史交付

2026-10-01（用户时区）修复机器人明确提问在模型调用前的本地检索失败。按用户的截图请求只读核验 Indeces 事故记录：0.12.0 已加载，机器人消息已经准入，模型前终止为 `OperationalError`；同一输入与只读数据库的进程内副本可重现累计本地处理超过原 5 秒后 `SQLITE_INTERRUPT`。旧记录没有保存 SQLite 错误编号，不能把重现当作取回旧异常原文。没有修改、导出或上传真实配置、数据库、知识或 scratch。

修复候选材料逐条扫描全部词边的问题，先按原边顺序筛选直接命中词对；支持/静态/动态合并后只解码最终有效证据一次。动态 shadow 更新保留 Python 原舍入、SQL 参数与顺序，仅批量提交；排序、公式、来源门禁和全部审计均与冻结的 0.12.0 基线比较。scratch 保留严格冻结、规范 JSON 字节、hash、flush/fsync 和失败 poison，复用已编码大字段；召回审计使用独立 JSON 快照，保留非原生 JSON 的 deepcopy 兼容路径以及独立重新计算哈希的校验。

本地时间门禁真实触发的 SQLite 中断现在记录 `local_memory_timeout`、失败阶段及 SQLite 编号；其他数据库错误和外部中断不会被误标。机器人失败仍不发送占位通知，五次准入/模型可选 skip 合同保持。未扩增原 5 秒/阶段 token/时间预算、模型并发或改变图公式。高负载、任意规模本地计算与磁盘 I/O 仍不保证在 5 秒内完成。

[OFFLINE_0121.json](../verification/OFFLINE_0121.json)：815 项、0 失败、0 错误、14 跳过、76.495 秒，CLI/Console、依赖与只读 npmi 检查通过。新增 31 项覆盖完整检索/audit/数据库等价、批量失败回滚、边工作次数、日志逐字节兼容、独立冻结及真实 SQLite 中断/清理/后续消息恢复。[WHEEL_0121.json](../verification/WHEEL_0121.json) 的隔离构建/安装、版本与资源/入口验证通过，本机源码及 editable 安装版本均为 0.12.1。

运行源码 [`75e90f7`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/75e90f7851df9f917b6385437641f3dc54d03ff3) 已推送 main，绑定精确提交的 [CI 36954818516](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36954818516) Windows/Ubuntu job与unittest/CLI check步骤均 success，见 [CI_0121.json](../verification/CI_0121.json)（未读取远端测试日志，不推断其测试数量）；[公开范围有限审计](../verification/REPOSITORY_AUDIT_0121.json) 无未知发现或禁止路径。提交与 CI 状态见 [CHECKPOINT.md](CHECKPOINT.md)。没有启动、停止或热加载真实服务，没有调用真实模型或发送 Discord 消息。当前用户启动的旧 Console 保持原进程模块；用户须显式 stop，等待清理结束后 quit，再重新打开 Console 并 start，才加载 0.12.1。真实 Discord 往返、模型收尾判断和召回相关性仍待验收。

# 0.12.0 历史交付

2026-10-01（用户时区）实现并离线验证机器人五次有限对话。人类与其他机器人均需显式 @Indeces；按频道/机器人预占五次机会，跳过/失败也占次，重启保留。新接受的人类消息重置同频道；重复、自身/webhook不重置。前四次只提及对方，第五次停止接续。模型在同一次 reply 调用中可对收尾选择 skip，保持静默并记入审计，不发送占位消息；失败仍记为 failed，机器人不给固定错误提示。没有扩增模型/阶段预算、并发或聊天入库。

根因是旧入口无条件过滤 `author.bot`，且所有回复禁止提及。现由持久门禁在同步准入时限额/去重，传输层控制对方提及；模型负责是否回应当条消息。首次添加门禁表前备份旧 SQLite；数据库或排队审计故障关闭准入并触发服务故障。两个记录系统存在崩溃窗口，不能宣称 SQLite 和 scratch 同一事务，也不自动重放预占消息。

最终 [OFFLINE_0120_20261002T012123075561Z.json](../verification/OFFLINE_0120_20261002T012123075561Z.json)：784 项、0 失败、0 错误、14 跳过、60.402 秒；新增 51 项覆盖门禁、传输、模型决策/计量/静默和端到端模拟对话。安装包验证 [WHEEL_0120.json](../verification/WHEEL_0120.json) 通过；源码/本机安装包均为 0.12.0。公开范围有限审计 [REPOSITORY_AUDIT_0120.json](../verification/REPOSITORY_AUDIT_0120.json) 与精确推送/CI 检查点见 [CHECKPOINT](CHECKPOINT.md)。

运行源码提交 [`7405790`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/7405790d71b114b887c91ac96c29a40686e3a708) 已推送 main，精确绑定的 [CI 36951114506](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36951114506) Windows/Ubuntu 两个 job 与 unittest/CLI check 步骤均 success，见 [CI_0120.json](../verification/CI_0120.json)。没有读取 CI 测试日志，不宣称远端测试数量。最终文档/CI 元数据补扫见 [REPOSITORY_AUDIT_0120_DELIVERY.json](../verification/REPOSITORY_AUDIT_0120_DELIVERY.json)，后续交付提交保持运行源码不变。

未启动、停止或热加载真实服务，未读取私有配置/数据库/知识材料；尚未验收真实 Discord 往返、对方 Bot 是否允许机器人消息、模型收尾判断质量或召回相关性。新进程显式 start 后才加载新版。

## 0.11.0 历史交付

0.11.0 完成摄入审计/恢复与单 Console 修复。源码和安装包版本一致。文件字节限制、400字符切块和单次模型额度保持原值；经用户明确批准，单知识版本累计额度改为 input196608、output49152、1080秒。旧显式配置不被加载器覆盖；迁移先备份、持锁并等待活动任务结束。

摄入新增元数据审计表，首次增表前备份既有SQLite/WAL一致快照；完整文件清单区分当前校验预测和保存错误。retry保留source ID、分块和累计计量，只恢复失败/未完成项；未知用量、变更文件和耗尽预算仍阻止恢复。schema补齐40字符限制，累计输入用实际计数准入，不再预留整个阶段输入额度；输出预留和阶段预算不变。

Console采用配置绝对路径作用域的内核租约和固定命令通信。服务、无Discord摄入维护及慢只读检查均在后台；连续/并发/不同入口、任务导航、重复调用与异常退出锁恢复有实际进程合成测试。stop/Ctrl+C清理当前拥有的任务，活动任务期间quit拒绝退出。Windows可见窗口聚焦、实际键盘体验与新Console真实Discord往返仍未验收。

最终本机[OFFLINE_0110_20261001T220004127125Z.json](../verification/OFFLINE_0110_20261001T220004127125Z.json)：733项、0失败、0错误、14跳过、61.704秒。包含超过3000标注出现和去重节点、末块及固定查询检索、格式/边界、部分失败/恢复/重复、迁移备份与重复取消。初次[OFFLINE_0110_INITIAL.json](../verification/OFFLINE_0110_INITIAL.json)保留一项失败：旧10ms测试可能在本地准入阶段先超时，却假定请求已发出。测试现在请求真正进入后触发真实asyncio期限，运行逻辑未放宽。

非 editable 安装包 [WHEEL_0110_20261001T215221456512Z.json](../verification/WHEEL_0110_20261001T215221456512Z.json) 的隔离构建、安装后导入、静态检索默认、`check`/`npmi` 入口及三个网页资源通过，未调用真实模型或 Discord。公开范围 [REPOSITORY_AUDIT_0110.json](../verification/REPOSITORY_AUDIT_0110.json) 只复查 `main` 可达历史和当前公开文件的有限秘密形状/路径规则；未发布工具引用、运行数据、release 和外部附件不属于本轮复查范围。

另一次[732项结果](../verification/OFFLINE_0110_20261001T215307875585Z.json)保留1项error：Windows venv的Popen.pid是launcher，原crash fixture等待launcher后过早直接获取仍由真实runtime持有的锁。异常退出改由实际owner执行os._exit，新增原launcher被杀场景的生产有界route恢复检查；两条压力路径各30次通过，生产锁仍由内核决定、没有按PID误抢或扩大等待。

首轮[远端CI](../verification/CI_0110_INITIAL.json)的Ubuntu成功、Windows失败733项中的cmd入口测试：工作流只安装全局Python而未创建真实cmd要求的项目.venv，入口在路由前退出。独立无venv目录复现rc1和同一提示；CI改为两平台创建venv，并用对应解释器安装、执行完整测试与check，保留真实cmd验证。后续验收提交a0dc1a4的Windows/Ubuntu均success：两平台各733项，分别3/12跳过，0失败/0错误；[最终CI附件](../verification/CI_0110.json)与CHECKPOINT绑定提交及job日志摘要，不把离线CI视为真实在线验收。

真实论文清单、调用记录和备份只保存在本机，不进入公开附件；离线通过不代替真实模型、材料语义或召回质量验收。远端证据见[CHECKPOINT.md](CHECKPOINT.md)。以下保留历史版本事实。

截至用户时区日期 2026-10-01，本体已在 `D:\Indeces` 实现。GitHub 仓库为公开 MIT [Indeces_memory_manager](https://github.com/simulacrum0112-afk/Indeces_memory_manager)。源码提交/推送及公开状态由交付消息、Git 历史和 GitHub 元数据确认；创建仓库本身不代表服务已启动。

远端代码检查点已确认：[`f27013910ae52852a7084ad8f2d92132dcda31c7`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/f27013910ae52852a7084ad8f2d92132dcda31c7)，对应 [CI 36820824993](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36820824993) 的 Windows/Ubuntu 两个 job 均为 success。2026-10-01 全部项目 Markdown 同步检查点、当前行为及未验收边界，运行版本仍为0.10.0；本次没有重跑整套运行测试。公开元数据、附件及文档索引统一见 [CHECKPOINT.md](CHECKPOINT.md)。后续文档提交由 Git 历史标识，不冒充新运行版本或源码修复。

已实现：无头/Console 入口、单实例存储锁、一个 Discord Gateway/有界消息队列、显式 @ 门禁、实际送达回执、短期 100%/70% 整轮水位摘要、GPT-6-Luna stateless adaptor、独立阶段预算、计量门禁、usage 核验、超时/阶段熔断、scratch hash 链与持久原文/checkpoint。

0.10.0 审计发现旧代码仍以动态权重扩展和排序，与先前已更正文档中的“静态检索”不一致。Runtime 现明确使用静态 NPMI，只有已保存的可用正静态权重才能一跳扩展，原始正值舍入为零也不可用；动态 seed/decay/reinforce 和不可变历史保留为 shadow 观察。新冻结材料与排序审计绑定 ranking_mode/weight_basis，同一事件不能换模式重放；旧无模式记录按旧动态算术核验，不能追认为静态实验。公式、SQLite schema、知识发布指针及原始材料未迁移。Console 新增 `npmi` 只读完整范围统计，观察网页默认静态层，说明单块/单来源支持和绘图截断；查询规划优化在原限额内避免重复范围扫描。

修复 worker 崩溃后 Gateway 仍继续接收但无人处理、实际 Discord 回执已确认后 scratch 故障丢失送达状态，以及重复取消打断队列/资源清理的路径。清理继续持锁等待实际退出；超时是诊断点，不声称操作系统硬抢占。模型槽等待与远端调用进度分列，不扩大阶段预算或共享单槽；只有本地等待的超时不处罚供应商，合法 usage 后的失败/取消/审计异常保存已知用量，无合法 usage 的已发生成保守标为未知。标词 JSON schema 增加与既有本地合同一致的 1–8 项及非空白字符串约束；本地40字符限制继续保留，未进行真实供应商 schema 验收。

现有六个职责与状态归属写入 [ARCHITECTURE.md](ARCHITECTURE.md)：Service、Bridge、Runtime、KnowledgeService、Adapter、Observer。仅参考 Yuki 三份已跟踪设计文档，没有读取或修改其私有配置/运行数据；没有引入新智能体框架、工具、并发、模型或预算。用户授权的本地 Indeces 图只读审计与合成测试分开完成，实际知识/运行指标未写入公开验证附件。旧失败任务未重试，已有服务未启停或热更新，新代码需下次进程加载才生效。

0.9.3 修复交互 `start` 直接进入 `asyncio.run(serve)`、`observe` 直接等待观察线程导致主 Console 不再读取命令的问题。Windows 三个持续入口 `start`/`observe`/`knowledge` 在独立可见窗口运行，同一解释器、配置绝对路径及源码目录传给子进程；主 Console 继续输入，启动/结束/错误信息留在入口窗口。直接 CLI 保持前台，其他平台提示另开终端。主 `quit` 不停止服务，服务窗口 Ctrl+C 停机。启动锁提前覆盖隐藏凭据输入及服务清理，取得锁后及输入后校验配置，避免向导竞争和重复 Gateway；`serve` 仍可自行管理独立调用的租约。观察 CLI 以短时 join 周期回到 Python，降低 Windows 长等待延迟 Ctrl+C 的风险。没有修改运行中任务、模型、NPMI、科学参数、阶段预算、并发或存储格式。

0.9.2 按用户要求将普通 PDF 转换/后台标词进度与完成、失败、中断结果移出主 Console。Windows Console `knowledge` 打开独立进度窗口，`Indeces-Knowledge.cmd` 或另一终端的 `python -m indeces knowledge` 持续查看；`--once` 单次查看。保留实际材料目录入口及全部 scratch 回执，主 Console 仍提示迁移、撤下、拒绝、扫描故障和后台停摆。进度每两秒以短事务只读 SQLite 查询当前 Guild 的版本、标词块数、发布指针及失败 code，不读取原文、PDF、标注词内容、graph 或 scratch，不占模型请求槽、不重放任务或迁移状态。失败新版与可检索旧版分列，只有 ready 与发布指针一致才显示发布完成；转换排队/进行中不能区分，停服快照不证明服务在线。查看退出不停止服务，原有模型、NPMI、预算、并发与存储 schema 均未改变。当前实例不停止或热更新，新输出规则在下次加载新版时生效。

0.9.0 增加原始材料目录入口：本机实际位置已核验为 `D:\Indeces\knowledge`，桌面“Indeces 知识库”快捷方式、项目根 `Indeces-Knowledge.cmd` 与 Console `knowledge` 方便到达，入口只打开目录，不启动 Bot/模型。根级 `.indeces` 放固定说明、`_staging` 放未入库材料，递归前剪枝，不计候选/标词；正常主题与年份子目录仍入库，目录链接/junction不遍历。保留原文件及自定义指南；可读但不可写的安全目录以只读方式访问，不因指南写入失败阻断。用户批准支持文件上限128→256，默认/示例/现有本地配置已同步，其他模型/单篇/版本预算不增。第257个候选仍按原策略撤下并归档全部当前范围来源，超限恢复需重新入库；暂存不冒充无限索引。详见 [KNOWLEDGE_DIRECTORY.md](KNOWLEDGE_DIRECTORY.md)。

0.2 系列已实现 Console `discord` 命令与 `python -m indeces discord` 本地桥接向导：服务器 Guild ID、可选频道 ID、隐藏输入的 Bot token、无 token 预览与 `y` 保存确认。频道输入留空保留现有设置，`*` 允许目标服务器中的全部符合显式 @ 条件的频道。运行实例持有同一 state 锁时拒绝修改。向导不联网；完成后用户另行执行 `start`。

Bot token 在 Windows 使用当前用户 DPAPI 加密，带应用 entropy、版本包络及 Guild 绑定，原子保存到 `config.local.discord.secret`；其他平台拒绝持久化，不降级为明文。启动来源顺序为 `DISCORD_BOT_TOKEN` → 匹配配置 Guild 的已保存 token → 隐藏的单次会话输入。配置、审计日志和 Git 不包含认证密钥。

0.8.0 增加 Console `apikey` 与 `python -m indeces apikey`：隐藏输入 OpenAI key，展示无密钥预览，确认后以独立当前用户 DPAPI 格式保存到 `config.local.openai.secret`。只更新该凭据文件，不改 TOML 或 Discord token；原 Discord 密文格式保持兼容。活动实例锁、外部修改检查、固定错误与中断恢复防止误覆盖/泄密，保存成功后终端回执失败不误称未保存。启动顺序为 `OPENAI_API_KEY` → 已保存 key → 会话隐藏输入，环境变量优先，损坏的保存文件需先修复；向导没有网络验证或服务启动。用户新指定的 `adapter.verbosity=high` 与三阶段 `reasoning=low` 已写入默认配置和请求，scratch 核验实际参数绑定；输入/输出/时间额度未增加，旧日志仍按旧合同核验。

默认智能体称呼为 **Indeces**，项目/包名保留原名。新增人设用于长期记忆测试：自然简短对话，区分短期上下文、摘要与有来源的长期召回，引用实际提供的来源，不编造记忆、图指标或有效性结论。没有引入模型并发、额外工具或自动评测能力；图公式、阶段预算、聊天标词关闭和本地知识更新的被动标词策略保持既有基线。

0.6.1 将当前 Console、向导、人设、观察页面与文档名称统一为 Indeces。旧产品名配置在加载时映射到新名称，不重写私有配置；自身词过滤接受新旧名字，构造图时仅重建含自身词的静态/支持缓存，保留来源记录、动态权重、事件和不可变审计。保留旧进程互斥协议标识与 DPAPI 固定标识，历史验证文件不追改。scratch 说明页仅在逐字节匹配完整旧固定模板时迁移，用户内容保留。没有启动 Bot 或扩展之前暂停的功能工作。

0.7.0 增加用户要求的 `knowledge/` 原生 PDF 自动入库：冻结原 PDF 字节/digest，独立本机进程转换带物理页号的 Markdown，再按原400字符块策略被动标词；整篇完成后原子切换，等待/失败期间保留旧发布快照。转换有独立输入字节/页数/输出/30秒限额，模型预算/请求槽与图规则未扩大。原 PDF 与转换历史持久保存在SQLite，审阅稿在state/pdf_markdown，不回写knowledge或自动上传。scratch冻结转换版本和所有相等块的物理页位置，不存PDF二进制；观察页可有界核验归档字节hash。OCR关闭，复杂版式和科学内容提取未经真实论文验收。详见 [PDF_IMPORT.md](PDF_IMPORT.md)。

0.3.0 已有知识路径：持续观察本地 Markdown/UTF-8 文本，保持默认 0.5 秒轮询与被动后台标词。Console 增加排队、开始、逐块进度、完成/失败回执，显示版本/来源 ID、digest、当前可检索的发布版本；进度显示已标注/总块数，结束回执显示已知 usage/耗时。未知远端计量在 scratch 明确标记，完整证据仍需查 scratch。聊天标词和聊天自动入库继续关闭，标签只附于原文，不由模型改写知识内容。

有效文件编辑的新版本在 pending/labelling/failed 期间保留旧完整原文、标注词和来源 ID；新文件首次完成前没有旧版回退。所有块、digest 与预算校验通过后，在同一事务中撤回旧来源、添加新记录并切换已发布指针/ready。待处理与已发布指针均按服务器范围记录，避免切换 Guild 后相同 digest 跳过新范围的索引。

删除或清空文档在下一次检测时撤回旧来源；无效输入、单文件超限和全局文件总数超限保留既有撤回/暂停策略。迁移只接纳旧当前 head 中归属明确、ready 且已有完整 active 记录的版本为发布快照，不重复标词；已归档或非当前证据不重新激活。归属/用量无法确认的旧待处理记录隔离至文件内容改变，并显示回执；未知用量的中断状态不自动重试。保存的原文、版本和已知 usage 不删除。

图策略来自固定 MIT 上游，NPMI、η=1、λ=.99、单跳来源门控、静态/动态分表、检索事件幂等与自身称呼排除保留。2026-09-30 曾仅在文档中声明“动态排序暂缓、回复仅静态”，未修改实现；0.10.0 发现该声明与当时实际动态排序接线冲突并修复，旧运行不属于已验证的静态实验。改动粒度、周期、排名与日记过滤的适配差异见 `BASELINE.md`。

0.4.0 新增可核验运行记录：检索后立即冻结全文/quote/标注词、来源版本与 desired/published 指针、模型可见材料、scope/event/query；图记录每步 seed/decay/reinforce、来源支持、NPMI 计数与排名依据。生成文本和实际送达文本分别记录 `[M1]` 一类字面引用的段落、字符偏移及来源绑定。未知标记明确告警，`semantic_support=not_evaluated`、`citation_coverage=not_established`；记录一致性不当成语义、事实或 Hebbian 效果证明。来源文本 SHA 可重算，原文件字节 SHA 仅作元数据保留而不声称验证原字节。

Console `scratch` 校验外层结构/hash 与新每轮记录合同，分别报告 complete/failed/incomplete/invalid/legacy，并以 call_counts 核对成功 adaptor 调用的计量/生成输入、响应、门禁、预算、usage/时间和输出；reply_context 对照实际 `/responses` 请求。图权重与首次事件审计在同一个数据库事务内提交，事务后立即写 `memory_observation` 回执；日志 fsync 另有边界，不能描述成跨数据库与日志原子。日志可能写入后的 I/O 失败禁止当前 writer 续写，不自动修复；后续审计失败不覆盖已确认的 Discord 送达状态。具体记录规范与限制见 [RUN_RECORDS.md](RUN_RECORDS.md)。没有增加模型调用、修改公式/预算或开展真实联网验收。

## 验证证据

- 0.10.0 本机完整离线回归：Windows/CPython 3.12.14，**676 tests / 0 failures / 14 skips**，36.285 秒；跳过原因为平台行为及本机符号链接权限。覆盖静态/shadow 分离、模式重放与冻结审计、防计账遗漏、worker/送达/取消生命周期和只读全量诊断。可复现文件入口、依赖检查、CLI/Console 检查见 [verification/OFFLINE_0100.json](../verification/OFFLINE_0100.json) 与 [verify_offline.py](../verification/verify_offline.py)。早期 stdin 测试入口使 Windows spawn 无法导入 `<stdin>`，产生三项进程测试失败，原记录保留于 [OFFLINE_0100_INITIAL.json](../verification/OFFLINE_0100_INITIAL.json)；改用带 main guard 的文件入口后全部通过，不把测试器故障记为产品修复。非 editable wheel 使用临时隔离构建工具，不改运行 venv，核验安装路径、静态默认、诊断入口及三个网页资源，见 [WHEEL_0100.json](../verification/WHEEL_0100.json)。合成浏览器检查默认静态、搜索范围、shadow说明、390px窄屏及失败后暂停警示，无页面错误；合成1300块/2789词/19459边数据的完整聚合约0.109秒、有界图约0.303秒，是本机测量，不承诺其他硬件速度。没有真实模型或 Discord 验收，没有自动启动实例；长期召回相关性、真实标词质量、复杂PDF语义及线上长时间运行仍待验证。

- 0.9.3 本机完整离线测试：Windows/CPython 3.12.14，**634 tests / 0 failures / 14 skips**，37.203 秒。覆盖三个交互入口后继续输入、子进程不被主退出终止、固定失败留窗、直接 CLI 保持前台、真实临时实例锁的凭据/向导竞争和退出释放，以及观察短等待/清理路径。配置/依赖导入、`pip check`、Console 退出、帮助与未配置 Guild 的留窗失败检查通过，见 [verification/OFFLINE_093.json](../verification/OFFLINE_093.json)。非 editable wheel 在隔离进程核验同一解释器/配置路径传递、连续命令、主窗口不读凭据或启动服务及错误留窗，GUI/子进程启动为 mock，见 [verification/WHEEL_093.json](../verification/WHEEL_093.json)。没有真实模型/Discord 验收，窗口与 Ctrl+C 的真实用户交互仍需区分于离线合同验证；已有服务不停止或重启。

- 0.9.2 本机完整离线测试：Windows/CPython 3.12.14，**613 tests / 0 failures / 14 skips**，40.063 秒。新增当前版本进度覆盖只读不创建/不修改、服务器与路径隔离、并发 WAL 一致快照、checkpoint 刷新、旧发布保留、未发布 ready、截断、终端控制字符和查看中断；真实合成 PDF 转换、标词成功/失败/取消均保持 stdout 静默与完整 scratch 审计。Console 独立窗口路由及 POSIX 解释器/工作目录/引号指令经离线验证，窗口启动 mock，不冒充真实桌面体验。配置/依赖导入、`pip check`、帮助与 Console 退出通过，见 [verification/OFFLINE_092.json](../verification/OFFLINE_092.json)。非 editable wheel 在独立解释器进程验证 knowledge 单次输出、1/2 块进度、旧发布与数据库字节不变，目录 GUI mock，见 [verification/WHEEL_092.json](../verification/WHEEL_092.json)。未读取真实配置/运行数据、未启停实例或调用真实模型/Discord；本次不是后台标词质量或线上验收。

- 0.9.1 公开发布准备：只调整忽略规则、文档和版本元数据，运行实现未变。本机42个私密路径样例均排除、15个公开样例允许，92个已跟踪路径不违反忽略规则；配置/导入、`pip check`、Console退出与安装版本核验通过，见 [verification/OFFLINE_091.json](../verification/OFFLINE_091.json)。对0.9.0基线公开引用可达的11个提交、221个blob及本地工具引用做秘密审计，校验官方checksum的Gitleaks8.30.1只有一项合成Guild ID误报，补充扫描的密钥形状匹配也均为具名离线假值；未发现真实秘密或公开历史中的运行数据路径，详见 [verification/REPOSITORY_AUDIT_091.json](../verification/REPOSITORY_AUDIT_091.json)。本地工具引用中的附件不发布；GitHub release/assets、Actions artifacts、issues/PR均为空。规则扫描不证明任意秘密不存在，完整Actions历史日志未下载审计。最终提交复扫、Windows/Ubuntu CI与GitHub公开元数据以交付消息确认，不以准备记录代替公开验收。
- 0.9.0 本机完整离线测试：Windows/CPython 3.12.14，**588 tests / 0 failures / 14 skips**，62.073 秒。新增目录入口 27、候选扫描与生命周期 16、Console 路由 2 项；真实 Windows junction 测试通过，本机符号链接权限及平台相关用例保留跳过。配置/依赖导入、`pip check`、Console退出、命令帮助通过，见 [verification/OFFLINE_090.json](../verification/OFFLINE_090.json)。非editable wheel在独立进程验证固定说明、Console目录路由、256上限与300个暂存文件排除，Explorer/服务均mock，见 [verification/WHEEL_090.json](../verification/WHEEL_090.json)。审计修复文件链接误占候选额度、目录枚举失败误当删除，以及指南保存失败清理可能删除竞争写入的用户说明；合成故障回归保留旧发布来源及用户README。本机仅同步已授权`max_files`字段、准备管理/暂存目录与桌面文件夹快捷方式，未读取真实材料、凭据、数据库或scratch，未启动Explorer、Bot或模型；精确提交远端CI由交付消息确认。
- 0.8.0 本机完整离线测试：Windows/CPython 3.12.14，**543 tests / 0 failures / 11 skips**，51.336 秒。新增凭据、向导、Console 与 verbosity 合同测试；真实当前用户 DPAPI 仅使用临时合成 key，旧 Discord 格式往返仍通过。配置/依赖导入、`pip check`、Console退出、命令帮助及非editable wheel中的向导→DPAPI保存→路径规范化后的启动读取（service transport mock）均通过，见 [verification/OFFLINE_080.json](../verification/OFFLINE_080.json)。复审修复回执持续中断、snapshot句柄与路径身份竞态、锁关闭错误覆盖提交状态、直接CLI异常文本逃逸，以及向导与启动的配置路径别名不一致；保留Windows/Ubuntu适用的平台测试跳过。仅现有本地TOML中的已授权话量/推理字段在停服锁下同步，未读取已有凭据、知识、数据库或scratch，没有真实模型/Discord调用；远端CI以精确提交交付消息为准。
- 0.6.0 本机完整离线测试：Windows/CPython 3.12.14，**387 tests / 0 failures / 7 skips**，20.850 秒。新增数据读取 28、服务 10、HTTP 路由 14、独立 HTTP 复审 6、scratch 并发 18、前端 Node 合约包装 1 项；7 项跳过为平台/本机符号链接权限相关。配置/导入、`pip check`、Console 退出与非 editable wheel 三个网页资源打包检查通过，记录见 [verification/OFFLINE_060.json](../verification/OFFLINE_060.json)。浏览器用临时合成图/版本/trace 确认搜索、分层、缩放、拖动、来源与逐步权重曲线、引用记录和安全文本渲染，390px 窄屏容器/SVG 边界复查通过，无页面错误。真实 Windows 线程/子进程/大小写路径别名/观察进程中断与超时测试确认读取不会阻碍原清理替换。没有读取或裁剪实际运行记录，没有模型或 Discord 连接；精确提交远端 CI 由交付消息核对。
- 0.6.1 本机完整离线测试：Windows/CPython 3.12.14，**404 tests / 0 failures / 7 skips**，23.906 秒。新增 12 项名称/旧缓存/凭据/跨版本 mutex 兼容测试和 5 项固定说明模板迁移测试。配置/导入、`pip check`、Console 新名称与正常退出、wheel 新名称和网页/identity 资源打包检查通过，记录见 [verification/OFFLINE_061.json](../verification/OFFLINE_061.json)。仅本机固定 scratch 说明页迁移，没有读取或裁剪真实 JSONL/数据库，没有真实模型或 Discord 调用。
- 0.7.0 本机完整离线测试：Windows/CPython 3.12.14，**463 tests / 0 failures / 7 skips**，27.733 秒。新增 PDF 解析/限额/进程回收 15、知识生命周期 26、来源与观察核验 18 项。配置/依赖导入、`pip check`、Console退出和非editable wheel独立进程转换均通过，见 [verification/OFFLINE_070.json](../verification/OFFLINE_070.json)。原创合成双栏/表格两页PDF经Poppler渲染逐页查看，提取保留字面值、页边界及hash；没有重建表格结构。复审修复了重复取消打断子进程回收、错误worker字段类型/过深JSON逃逸固定错误分类两处问题。没有读取真实论文、运行状态或scratch，没有真实模型/Discord调用；精确提交CI以交付消息链接为准。
- 0.5.0 本机完整离线测试：Windows/CPython 3.12.14，**310 tests / 0 failures / 4 skips**，12.959 秒；scratch 45（原 19 + 新增 26）、跨窗口运行记录 15、服务维护 13 项。4 项跳过为当前 Windows 平台/符号链接权限相关用例。配置/导入、`pip check`、Console 退出烟雾与最终 Console checkpoint 显示回归通过，记录见 [verification/OFFLINE_050.json](../verification/OFFLINE_050.json)。只用临时合成数据，没有读取或清理实际 scratch，也未连接模型/Discord；远端 CI 以本版精确提交为准。
- 0.4.0 历史本机完整离线测试：Windows/CPython 3.12.14，**256 tests / 0 failures / 2 skips**，8.255 秒；运行记录 33、Hebbian memory 27、scratch 19、审计故障边界 5 项。配置/导入、`pip check` 与 Console 退出烟雾检查通过，记录见 [verification/OFFLINE_040.json](../verification/OFFLINE_040.json)。精确提交 `e1cc53573ca631f5087ea7aad2de8ff165fcfdcd` 的 [main CI](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36782658061) 和 [v0.4.0 tag CI](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36782658922) 四个 job 均通过：Windows 256 项/0 失败/1 跳过，Ubuntu 256 项/0 失败/2 跳过，四个 CLI 检查通过。没有真实模型/Discord 验收。
- 0.3.0 历史本机完整离线测试：Windows/CPython 3.12.14，**193 tests / 0 failures / 2 skips**，5.419 秒；知识生命周期 52 项（原 23 + 新增 29），人设 7 项。配置/导入、`pip check` 和 Console 退出烟雾检查通过；记录见 [verification/OFFLINE_030.json](../verification/OFFLINE_030.json)。精确提交 `96e3a56d0096c0a9d97a0afc7e5f635d5eb69af8` 的 [main CI](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36779788040) 与 [v0.3.0 tag CI](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36779788302) 四个 job 均通过：Windows 193 项/0 失败/1 跳过，Ubuntu 193 项/0 失败/2 跳过，CLI 配置/导入检查均通过。
- 0.2.1 历史本机完整离线测试：Windows/CPython 3.12.14，**163 tests / 0 failures / 2 skips**，3.201 秒。配置/导入、`pip check` 与 Console 退出烟雾检查均通过；可复现摘要见 [verification/OFFLINE_021.json](../verification/OFFLINE_021.json)。对应精确提交 `0238ac34f558382c29884a1644b8c316eb7eccfd` 的 [main CI](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36776644617) 和 [v0.2.1 tag CI](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36776644454) 均通过：Windows 163 项/0 失败/1 跳过，Ubuntu 163 项/0 失败/2 跳过；四个 CLI 配置/导入检查通过。
- 0.2.0 本机历史离线测试：**163 tests / 0 failures / 2 skips**，6.418 秒；含新增向导 28、Console 14、人设 6、凭据 26 项。摘要见 [verification/OFFLINE_020.json](../verification/OFFLINE_020.json)。[该提交的 CI](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36775737063) 中 Ubuntu 通过，但 Windows 的 3 项故障注入测试未触发预期失败；此历史结果不能称为跨平台验收通过。
- 0.2 系列凭据专项历史记录：**26 tests / 0 failures / 2 skips**。执行了真实 DPAPI 的合成 token 往返与密文篡改拒绝；另外覆盖 Guild 不匹配、密文/JSON 损坏、固定安全错误、原子替换失败保留旧文件、临时文件和句柄清理、禁止 getpass 回退到明文回显。跳过非 Windows 保存行为及本机无法创建符号链接的用例；非 Windows 原生 helper 拒绝持久化通过模拟平台检查。
- 向导覆盖配置预算与无关注释保留、活动实例锁、旧 token 保留/重绑定/损坏修复、取消不保存、两次替换中断后恢复快照、外部修改拒绝覆盖、回滚失败明确报告，以及保存完成后取消的准确提示。Console 验证保存后重载与凭据来源顺序；未以 mock Gateway 的就绪事件作为真实连接证据。
- 0.1.0 历史基线：**89 tests / 0 failures / 0 skips**，最近完整运行 4.291 秒；adaptor 13、context/runtime 14、Discord bridge 16、被动知识版本 23、Hebbian memory 16、scratch 5、单实例锁 2。
- 0.1.0 的配置/依赖导入、`pip check` 和固定依赖检查通过，可复现摘要保存在 [verification/OFFLINE_010.json](../verification/OFFLINE_010.json)。`requirements.lock` 和 Windows/Linux 离线 CI 仍保留；本版远端状态须单独核对。
- 测试使用合成消息、注入模型传输和临时文件/SQLite；覆盖预算超限不发送生成、取消/熔断、摘要失败保留覆盖边界、无聊天标词、文件在 await/索引期间替换、原子 ready 失败回滚、旧崩溃恢复、来源保留、日志损坏检测。

0.1.0 发现并修复：供应商输出类型未受控、排队调用绕过已打开熔断、非法标签漏记已知 usage、文件未扫描时旧标注发布、全局文件超限留下陈旧索引、ready 状态与索引提交的崩溃窗口、旧中断 active 索引恢复，以及 Windows 第二实例先读锁定字节导致 PermissionError/句柄未关闭。上游静态/在线匹配不一致、弱边先去重、时间戳/重复事件与来源错误吞没均在适配中修复或以显式事件替代。

0.1.0 最后一次验证发现计时测试错误假定所有调用 elapsed>0：本机 `monotonic` 为 GetTickCount64，分辨率 0.015625 秒，即时 fake 调用可合法记为 0。已修正测试为允许非负测量，没有人为加 epsilon 或提高虚假精度；clock 信息一并存入历史验证摘要。

0.2.0 审计修复：原 `.isdigit()` 接受非 ASCII 数字和无效雪花 ID；向导加载配置与取得快照之间可能漂移；保存中断可能留下已变更 token 却提示未修改；保存后取消可能错误声称未保存；`fdopen` 失败可能泄漏临时文件句柄；默认 getpass 可能回退到回显输入。现分别以规范化 ID 校验、快照一致性检查、双文件异常回滚/失败报告、提交状态标记、描述符所有权管理及回显拒绝解决。回滚失败和进程突然终止不能被描述成两文件具有同一个原子事务；Guild 绑定可阻止范围不匹配凭据被直接使用。

0.2.1 修复测试的 Windows 路径兼容性：临时目录的 8.3 短路径与向导规范路径在字符串比较时不同，导致三处故障注入未执行。用 `GetShortPathNameW` 构造临时父目录，修改前复现 3 项失败，规范化 fixture 并显式断言注入发生后同样目录下 3 项全部通过。生产向导、图公式和预算没有因此修改；保留原 v0.2.0 标签与失败 CI 历史。

0.3.0 针对知识生命周期审计：旧 head 同时承担文件目标版本与可检索版本，导致编辑时先丢失旧完整来源；旧 head 只按路径记录，切换 Guild 后相同 digest 可能使新范围没有索引。当前分开记录按范围的目标版本/已发布版本，并增加发布过程和迁移隔离回执。还修复空文档发布竞争误撤旧版、迁移遗漏未标注原文尾部却误判完整、日志异常误当文件拒绝，以及后台任务异常后静默停止标词但继续排队的问题。后台不可恢复异常会明确暂停观察/标词，保留已发布图，不自动重放请求；无法保证日志写入时，需修复后重启。图公式、标词模型、阶段与版本预算不变；以上有合成回归证据，真实文件/模型质量仍未验收。

0.4.0 记录审计修复：原 hash 校验未验证连续序号、版本、事件/字段和 UTC 时间结构；预写入序列化失败会提前推进序号，fsync 失败后继续写入可能破坏后续链。现先序列化/冻结再写入，完整 I/O 成功后推进状态，可能写入后的失败 latch 禁止续写，保留文件不自动修复。另修复已确认送达后本地审计异常错误覆盖为 delivery_unknown，以及关闭日志失败跳过数据库/实例锁清理的路径；本次最终测试证据另列，不据设计说明宣称全部验收。

0.5.0 默认 rolling 24h scratch 保留：按事件 UTC timestamp，启动和服务内每 60 秒清理，空闲也执行；停服下次启动再清理。保留行的原序号/hash 不改，同文件 checkpoint 声明删除前缀及仍活跃的截断 ID；`retention_partial` 不冒充完整历史。跨文件先发布待清理声明再剪正文/删全过期文件；失败可能留 `cleanup_pending` 中间状态，服务报告并停止，不称作全目录原子。仅清 scratch 管理文件，不删除知识/SQLite/图审计，不归档到期日志，不增加模型调用或外部定时任务。

## 观察功能与未验收边界

0.6.0 新增用户要求的普通 scratch 目录说明/入口和本机只读图/trace 网页。Console `logs` 显示目录，`observe` 单独运行，`start` 自动显示观察 URL；只绑定 loopback 临时端口与随机路径，所有资产本地提供。数据只读、有范围与字节限额，图/知识和 scratch 截断明确显示。可看 NPMI、动态/有效权重、有效来源、已发布/待更新版本，以及记录过的种子/衰减/强化历史；页面查询与 Runtime 模型槽分离，观察启动失败不会阻止 Bot。

scratch 网页按当前 UTC 的 24 小时窗口过滤；暂停或断网时，浏览器继续移除过期 scratch 事件原文。固定说明页不复制私有 trace，后端与浏览器不生成持久缓存/导出副本。页面的保留记录一致性检查不代替 Console 完整生命周期合同，也不核验语义支持。SQLite 图审计/来源历史独立持久保存。新观察层没有改变 NPMI、η/λ、阶段预算或聊天标词关闭；逐轮语义权重调整尚未启用。详细运行方法与数据/性能边界见 [OBSERVABILITY.md](OBSERVABILITY.md)。

- 工程验证未开展真实 Discord 往返或 OpenAI 模型验收，不据源码、CI 或观察快照推断用户实际使用情况。代表性往返与知识更新试验需用户自行配置后显式 `start`；向导保存成功仅证明本地设置完成，不证明 token 有效、Bot 已入服或有频道权限。
- 部分历史 `invalid_labels` 失败缺少24小时保留窗口内的完整调用证据，全部根因未确认。1–8项与非空白 schema 修复只约束已知本地合同缺口，不证明每个历史失败均由它造成，旧失败任务未自动重试。
- 未验证账号的模型访问、生产延迟、真实 token 分布、文件集标签质量、知识召回相关性或动态权重长期表现；当前预算是工程初值。
- 0.4.0 仅自动检查冻结记录关联、所记录权重/排名算术和字面引用位置；没有自动语义蕴含、事实核验或引用覆盖评估。被召回、出现标记、结论得到支持是三个不同结果。
- 原文件字节未存入运行记录，原字节 digest 仅为版本元数据；SQLite 图/审计事务与 scratch fsync 之间仍可能出现崩溃窗口或缺失阶段。没有外部锚点，不能检测所有完整行尾部删除、整文件删除或整体重写。
- 0.3.0 保留旧版意味着有效编辑完成之前、以及新版本失败期间，回答可能继续引用旧文件内容；回执和来源版本用于明确这一条件。已开始且等待模型的回答保持检索时的证据快照与 trace，删除/发布切换只影响下一次检索，不提供即时撤回已经开始的回复。默认 0.5 秒是目录轮询间隔，不能保证固定的文件检测或模型完成延迟。
- 尚未完成真实运行数据上的 0.3.0 迁移验收；隔离的归属/用量不明待处理版本需内容变化后重建，不自动重激活或重试。离线迁移测试不证明旧知识标签质量。
- 当前每个有直接命中的检索为一个 `.99` 衰减周期，不等同每日周期。该选择明列于基线表，使用者应在真实 pilot 中评估检索频率影响。
- 本地图排序、文件 I/O 和 fsync 不可由 asyncio 强制抢占；当前协作式本地时间检查不能当成任意规模计算的硬时间保证。
- 未收到允许后台标词与回复真实模型请求重叠的选择；当前两任务独立而 HTTP 串行。
- 尚无长时间运行/网络断开/真实 Discord rate-limit 验收。Discord 超时可能已经产生外部消息，不能承诺 exactly-once；无主动发送重试。
- scratch hash 链与裁剪 checkpoint 只检查本地一致性，不是外部公证；日志原文在滚动 24 小时后清除，窗口外完整证据不再可查，没有自动归档或压缩。数据库/知识保留不受该策略影响；停止服务和本地 I/O/时钟异常时不能保证实时到点清理。

没有修改/启动现有 Yuki，没有科研参数变更、计算任务提交或旧结果迁移。不存在依据离线测试宣称“可发表”或“真实检索效果已验证”的结论。
