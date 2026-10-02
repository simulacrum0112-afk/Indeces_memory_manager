# 公开仓库与本地数据

0.12.0；2026-10-01 文档核对。仓库公开、MIT、默认 `main` 的元数据与代码 CI 检查点见 [CHECKPOINT.md](CHECKPOINT.md)。文档检查点只存公开提交/验证摘要，不包含真实知识统计或运行记录，也不代替本地数据备份。

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
