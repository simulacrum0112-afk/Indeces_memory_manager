# 交付状态：0.5.0

截至用户时区日期 2026-09-30，本体已在 `D:\Indeces` 实现。GitHub 私有仓库为 [Indeces_memory_manager](https://github.com/simulacrum0112-afk/Indeces_memory_manager)。源码提交/推送结果由交付消息及 Git 历史确认；创建仓库本身不代表服务已启动。

已实现：无头/Console 入口、单实例存储锁、一个 Discord Gateway/有界消息队列、显式 @ 门禁、实际送达回执、短期 100%/70% 整轮水位摘要、GPT-6-Luna stateless adaptor、独立阶段预算、计量门禁、usage 核验、超时/阶段熔断、scratch hash 链与持久原文/checkpoint。

0.2 系列已实现 Console `discord` 命令与 `python -m indeces discord` 本地桥接向导：服务器 Guild ID、可选频道 ID、隐藏输入的 Bot token、无 token 预览与 `y` 保存确认。频道输入留空保留现有设置，`*` 允许目标服务器中的全部符合显式 @ 条件的频道。运行实例持有同一 state 锁时拒绝修改。向导不联网；完成后用户另行执行 `start`。

Bot token 在 Windows 使用当前用户 DPAPI 加密，带应用 entropy、版本包络及 Guild 绑定，原子保存到 `config.local.discord.secret`；其他平台拒绝持久化，不降级为明文。启动来源顺序为 `DISCORD_BOT_TOKEN` → 匹配配置 Guild 的已保存 token → 隐藏的单次会话输入。`OPENAI_API_KEY` 仍只来自环境变量或启动时的隐藏输入。配置、审计日志和 Git 不包含认证密钥。

默认智能体称呼为 **Indices**，项目/包名保留原名。新增人设用于长期记忆测试：自然简短对话，区分短期上下文、摘要与有来源的长期召回，引用实际提供的来源，不编造记忆、图指标或有效性结论。没有引入模型并发、额外工具或自动评测能力；图公式、阶段预算、聊天标词关闭和本地知识更新的被动标词策略保持既有基线。

0.3.0 已有知识路径：持续观察本地 Markdown/UTF-8 文本，保持默认 0.5 秒轮询与被动后台标词。Console 增加排队、开始、逐块进度、完成/失败回执，显示版本/来源 ID、digest、当前可检索的发布版本；进度显示已标注/总块数，结束回执显示已知 usage/耗时。未知远端计量在 scratch 明确标记，完整证据仍需查 scratch。聊天标词和聊天自动入库继续关闭，标签只附于原文，不由模型改写知识内容。

有效文件编辑的新版本在 pending/labelling/failed 期间保留旧完整原文、标注词和来源 ID；新文件首次完成前没有旧版回退。所有块、digest 与预算校验通过后，在同一事务中撤回旧来源、添加新记录并切换已发布指针/ready。待处理与已发布指针均按服务器范围记录，避免切换 Guild 后相同 digest 跳过新范围的索引。

删除或清空文档在下一次检测时撤回旧来源；无效输入、单文件超限和全局文件总数超限保留既有撤回/暂停策略。迁移只接纳旧当前 head 中归属明确、ready 且已有完整 active 记录的版本为发布快照，不重复标词；已归档或非当前证据不重新激活。归属/用量无法确认的旧待处理记录隔离至文件内容改变，并显示回执；未知用量的中断状态不自动重试。保存的原文、版本和已知 usage 不删除。

图策略来自固定 MIT 上游，并按批准启用动态排序；NPMI、η=1、λ=.99、单跳来源门控、静态/动态分表、检索事件幂等与自身称呼排除已验证。改动粒度、周期、排名与日记过滤的适配差异在 `BASELINE.md` 如实列出。

0.4.0 新增可核验运行记录：检索后立即冻结全文/quote/标注词、来源版本与 desired/published 指针、模型可见材料、scope/event/query；图记录每步 seed/decay/reinforce、来源支持、NPMI 计数与排名依据。生成文本和实际送达文本分别记录 `[M1]` 一类字面引用的段落、字符偏移及来源绑定。未知标记明确告警，`semantic_support=not_evaluated`、`citation_coverage=not_established`；记录一致性不当成语义、事实或 Hebbian 效果证明。来源文本 SHA 可重算，原文件字节 SHA 仅作元数据保留而不声称验证原字节。

Console `scratch` 校验外层结构/hash 与新每轮记录合同，分别报告 complete/failed/incomplete/invalid/legacy，并以 call_counts 核对成功 adaptor 调用的计量/生成输入、响应、门禁、预算、usage/时间和输出；reply_context 对照实际 `/responses` 请求。图权重与首次事件审计在同一个数据库事务内提交，事务后立即写 `memory_observation` 回执；日志 fsync 另有边界，不能描述成跨数据库与日志原子。日志可能写入后的 I/O 失败禁止当前 writer 续写，不自动修复；后续审计失败不覆盖已确认的 Discord 送达状态。具体记录规范与限制见 [RUN_RECORDS.md](RUN_RECORDS.md)。没有增加模型调用、修改公式/预算或开展真实联网验收。

## 验证证据

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

## 未完成与边界

- 未连接真实 Discord、未发送真实消息，未调用真实 OpenAI 模型。需要用户在本地向导填写 Guild 与 token，再执行 `start`，完成一个专用频道的代表性往返与知识更新试验。向导保存成功仅证明本地设置完成，不证明 token 有效、Bot 已入服或有频道权限。
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
