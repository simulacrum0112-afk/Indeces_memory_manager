0.12.5 路径边界修复的公开材料只含源码、合成测试、有限规则审计与调查边界；不包含被报告的私人目录、文献文件名、内容、真实数据库/聊天统计或操作日志。根身份迁移备份与隔离记录属于state私有数据，原有保留/发布边界不变。详见 [PATH_BOUNDARY.md](PATH_BOUNDARY.md)。

# 公开仓库与本地数据

最终0.12.5运行源码0d78ddb与精确CI验收见 [CHECKPOINT.md](CHECKPOINT.md)；[交付公开范围补扫](../verification/REPOSITORY_AUDIT_0125_DELIVERY.json)覆盖main可达历史和精确暂存公开文件，有限规则未知发现/禁止路径均零。最终CI附件只保存公开元数据；三次早期失败附件保留公开测试诊断摘要，不保存原始job日志或用户运行数据。

0.12.4；2026-10-02 文档核对。仓库公开、MIT、默认 `main` 的元数据与代码 CI 检查点见 [CHECKPOINT.md](CHECKPOINT.md)。文档检查点只存公开提交/验证摘要，不包含真实知识统计或运行记录，也不代替本地数据备份。

本次事故排查只读自身相关配置、运行链和消息元数据，同一输入仅在内存复算；真实原文、配置、日志、数据库和事故数值不进入公开附件。模型材料视图去掉全量图 evidence，不改变完整 scratch 保留合同。新复现使用合成消息/临时SQLite及已公开0.12.3源码对照，没有真实模型调用。有限公开范围审计见本版本 [CHECKPOINT.md](CHECKPOINT.md)。

[REPOSITORY_AUDIT_0124.json](../verification/REPOSITORY_AUDIT_0124.json) 核验此前main可达28提交/492唯一blob与181公开文件，有限规则未知发现/禁止路径零；最终四份交付文档与公开CI元数据补扫见 [REPOSITORY_AUDIT_0124_DELIVERY.json](../verification/REPOSITORY_AUDIT_0124_DELIVERY.json)，运行源码保持3a422d1，不发布本地工具引用、私有运行记录或临时合成数据库。

检索缓存的 TEMP revision 不新增持久运行表；合成基准公开摘要/hash与复现脚本，MB级临时基线不进入Git，原材料、SQLite与scratch排除规则不变。

机器人五次准入/去重的 SQLite 表与迁移备份同属私有运行数据，沿用数据库排除规则；公开验证仅使用临时合成数据。[REPOSITORY_AUDIT_0120.json](../verification/REPOSITORY_AUDIT_0120.json) 明确本版 Git main 与公开文件的有限规则检查范围，不发布本地工具引用，不将文件路径或模型源码审计称为运行数据备份。

仓库采用现有 MIT 许可证，保留动态 Hebbian 策略的上游许可证与归属说明。发布范围为源码、文档、空配置示例，以及使用合成数据的测试和离线验证摘要。

本地数据不属于发布内容：

- `.env` 及其本地变体、`config.local*.toml` 和本地配置备份。
- OpenAI API key、Discord Bot token、`*.secret` 加密凭据及保存中断的临时文件、私钥与凭据导出。
- `scratch/`、JSONL 运行记录、日志、`state/`、SQLite/DB 文件及其 sidecar。
- `knowledge/` 原始材料、PDF 转换稿及本地 `assets/` 附件；只保留空目录入口 `knowledge/.gitkeep`。

`.env.example` 和 `config.example.toml` 可提交，只包含空值、占位符和公开默认设置。运行网页资源在 `indeces/web/`，属于源码，不受本地附件目录的排除规则影响。真实密钥只在环境变量、会话或本地加密向导中提供。

`.gitignore` 防止通常的新增文件被选入提交；它无法清除已经提交的文件，也无法阻止 `git add -f`。自定义凭据文件名或运行目录时，应补充忽略规则，并检查 `git status --short` 与 `git diff --cached --name-only`；不要把真实运行记录改名放进源码、测试或验证目录。

公开发布前检查所有可达分支和标签的 Git 历史、已跟踪路径与内容，并核验 GitHub release 附件、Actions artifacts 等发布面。秘密扫描是有限规则检查，不证明任意格式或未知凭据都能检出。历史公开准备审计见 [REPOSITORY_AUDIT_091.json](../verification/REPOSITORY_AUDIT_091.json) 和 [REPOSITORY_AUDIT_0100.json](../verification/REPOSITORY_AUDIT_0100.json)。0.11.0 的 [REPOSITORY_AUDIT_0110.json](../verification/REPOSITORY_AUDIT_0110.json) 检查 `main` 可达历史与当前公开源码，排除未发布的本地工具引用和运行数据；只记录规则、公开路径、行号、blob 与分类，不保存密钥值或真实运行记录。最后变更后的待提交范围补扫见[REPOSITORY_AUDIT_0110_FINAL.json](../verification/REPOSITORY_AUDIT_0110_FINAL.json)，具名合成凭据和unittest限定名称误报逐项分类，未知发现为零。本轮没有重扫 release、外部附件或任意秘密赋值形式，不扩大历史审计结论。

仅推送主分支和明确的版本标签，不用 `push --mirror` 或 `push --all`。本地工具引用（如 `refs/codex/*`）可能包含私有附件的历史快照，应保留在本机，不发布到 GitHub。公开仓库可另外启用 GitHub 的密钥扫描与推送保护，作为新增提交的补充检查，不替代忽略规则或人工核验。

本地原材料、凭据和运行数据保留在原处；公开仓库不会代替它们的备份。服务、模型请求和 Discord 连接仍由用户在 Console 明确启动。

0.10.0 历史文档同步的链接、版本和 `main` 可达历史/当时待提交内容补充检查保存在 [DOCUMENTATION_CHECKPOINT_0100.json](../verification/DOCUMENTATION_CHECKPOINT_0100.json)。旧验证附件保持原版本和原检查范围；不改写它们来冒充新提交的测试结果，不因 CI 通过扩大秘密扫描或真实联网验收的结论。

0.12.3 的 [REPOSITORY_AUDIT_0123.json](../verification/REPOSITORY_AUDIT_0123.json) 核验 main 可达26提交/468唯一blob与170公开文件，有限规则未知发现/禁止路径均零；最终文档及公共CI元数据补扫见 [REPOSITORY_AUDIT_0123_DELIVERY.json](../verification/REPOSITORY_AUDIT_0123_DELIVERY.json)，源码保持 de07bdb。MB合成基线只留本机临时目录，没有实际服务材料进入公开报告。
