# 交付状态：0.9.3

截至用户时区日期 2026-09-30，本体已在 `D:\Indeces` 实现。GitHub 仓库为 [Indeces_memory_manager](https://github.com/simulacrum0112-afk/Indeces_memory_manager)。源码提交/推送及公开状态由交付消息、Git 历史和 GitHub 元数据确认；创建仓库本身不代表服务已启动。

已实现：无头/Console 入口、单实例存储锁、一个 Discord Gateway/有界消息队列、显式 @ 门禁、实际送达回执、短期 100%/70% 整轮水位摘要、GPT-6-Luna stateless adaptor、独立阶段预算、计量门禁、usage 核验、超时/阶段熔断、scratch hash 链与持久原文/checkpoint。

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

图策略来自固定 MIT 上游，动态在线排序为已实现、当前暂缓的选项;现阶段回复链仅静态检索；NPMI、η=1、λ=.99、单跳来源门控、静态/动态分表、检索事件幂等与自身称呼排除已验证。改动粒度、周期、排名与日记过滤的适配差异在 `BASELINE.md` 如实列出。2026-09-30 同步更正基线、协作约定与交付状态中的动态排序表述；本次仅修改文档，未变更实现或运行参数，版本保持 0.9.1。

0.4.0 新增可核验运行记录：检索后立即冻结全文/quote/标注词、来源版本与 desired/published 指针、模型可见材料、scope/event/query；图记录每步 seed/decay/reinforce、来源支持、NPMI 计数与排名依据。生成文本和实际送达文本分别记录 `[M1]` 一类字面引用的段落、字符偏移及来源绑定。未知标记明确告警，`semantic_support=not_evaluated`、`citation_coverage=not_established`；记录一致性不当成语义、事实或 Hebbian 效果证明。来源文本 SHA 可重算，原文件字节 SHA 仅作元数据保留而不声称验证原字节。

Console `scratch` 校验外层结构/hash 与新每轮记录合同，分别报告 complete/failed/incomplete/invalid/legacy，并以 call_counts 核对成功 adaptor 调用的计量/生成输入、响应、门禁、预算、usage/时间和输出；reply_context 对照实际 `/responses` 请求。图权重与首次事件审计在同一个数据库事务内提交，事务后立即写 `memory_observation` 回执；日志 fsync 另有边界，不能描述成跨数据库与日志原子。日志可能写入后的 I/O 失败禁止当前 writer 续写，不自动修复；后续审计失败不覆盖已确认的 Discord 送达状态。具体记录规范与限制见 [RUN_RECORDS.md](RUN_RECORDS.md)。没有增加模型调用、修改公式/预算或开展真实联网验收。

## 验证证据

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

## 未完成与边界

0.6.0 新增用户要求的普通 scratch 目录说明/入口和本机只读图/trace 网页。Console `logs` 显示目录，`observe` 单独运行，`start` 自动显示观察 URL；只绑定 loopback 临时端口与随机路径，所有资产本地提供。数据只读、有范围与字节限额，图/知识和 scratch 截断明确显示。可看 NPMI、动态/有效权重、有效来源、已发布/待更新版本，以及记录过的种子/衰减/强化历史；页面查询与 Runtime 模型槽分离，观察启动失败不会阻止 Bot。

scratch 网页按当前 UTC 的 24 小时窗口过滤；暂停或断网时，浏览器继续移除过期 scratch 事件原文。固定说明页不复制私有 trace，后端与浏览器不生成持久缓存/导出副本。页面的保留记录一致性检查不代替 Console 完整生命周期合同，也不核验语义支持。SQLite 图审计/来源历史独立持久保存。新观察层没有改变 NPMI、η/λ、阶段预算或聊天标词关闭；逐轮语义权重调整尚未启用。详细运行方法与数据/性能边界见 [OBSERVABILITY.md](OBSERVABILITY.md)。

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
