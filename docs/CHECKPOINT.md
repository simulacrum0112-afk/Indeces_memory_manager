# 当前与历史代码检查点

当前代码版本 **0.12.1**；2026-10-01（用户时区）修复已准入机器人提问在模型前的可复现检索超时。重复全边扫描、证据 JSON 处理、完整审计编码和快照复制已优化；静态排序、完整审计、5 秒本地预算及原模型额度不变。失败记录区分真实门禁中断和其他 SQLite 错误。事故诊断只读原数据，试算写入仅位于进程内 SQLite 副本，原文/数据库/统计不进入公开附件。

本机 [OFFLINE_0121.json](../verification/OFFLINE_0121.json) 为815项、0失败、0错误、14跳过、76.495秒；新增31项，含冻结的原算法完整结果/审计/DB兼容、日志逐字节兼容、快照隔离、真实SQLite中断和后续消息恢复。CLI/Console/依赖与只读 npmi 检查通过。[WHEEL_0121.json](../verification/WHEEL_0121.json) 的隔离构建/安装和资源/入口检查通过。源码与本机安装包均为0.12.1；测试仅使用合成数据，没有真实模型或Discord请求。

0.12.1 当前尚未推送，远端 CI 未核验；以下 0.12.0 CI 仅属于历史代码，不能作为本次修复的证据。公开范围有限审计将在提交前核验 main 可达历史与本次 staged 内容。

现有 Console 不会因磁盘源码更新加载新模块；没有自动停止/重启服务。用户显式 stop，等后台清理结束后 quit，重新打开 Console 并 start 才加载新版本。任意规模或高负载的硬时间保证、真实磁盘路径/模型/Discord往返、收尾判断质量和召回相关性仍未验收。

# 0.12.0 历史代码检查点

当前代码版本为 **0.12.0**，用户时区日期 2026-10-01 增加机器人有限对话与模型可选静默。模型、阶段/知识版本预算、静态检索和单请求槽保持；源码与本机 editable 安装包版本一致。没有启动、停止或热加载真实服务。

机器人须显式 @Indeces；每个频道/机器人最多五次准入机会（含跳过、失败、过期），重连/重启保持；新接受的人类显式 @ 重置该频道。模型同一次 reply 请求可判断收尾并静默跳过；前四次回复只提及对方，第五次不再提及。完整合同见 [README](../README.md)、[ARCHITECTURE](ARCHITECTURE.md) 和 [RUN_RECORDS](RUN_RECORDS.md)。首次增加持久门禁表时先备份既有 SQLite，开发与测试没有读取实际数据库或私有配置。

最终本机离线验证：[OFFLINE_0120_20261002T012123075561Z.json](../verification/OFFLINE_0120_20261002T012123075561Z.json)，784 项、0 失败、0 错误、14 跳过、60.402 秒；配置/import、依赖、CLI/Console 和只读 npmi 检查通过。新增 51 项包括持久门禁、传输边界和决策/审计；端到端合成试验确认五次模型调用、一条静默、四次实际模拟发送、第六条调用前拒绝。初次 783 项通过记录保留在 [OFFLINE_0120.json](../verification/OFFLINE_0120.json)，其后增加端到端用例才形成最终 784 项结果。

[WHEEL_0120.json](../verification/WHEEL_0120.json) 确认非 editable 隔离构建/安装、导入、静态默认、CLI 入口和网页资源。原虚拟环境未安装 build backend，首次无隔离 editable 构建未成功；随后通过标准 pip 临时构建隔离完成安装，不更改运行依赖。公开范围有限审计见 [REPOSITORY_AUDIT_0120.json](../verification/REPOSITORY_AUDIT_0120.json)，不上传真实知识、scratch、数据库、凭据或 assets。

运行源码实现为 [`7405790d71b114b887c91ac96c29a40686e3a708`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/7405790d71b114b887c91ac96c29a40686e3a708)，已推送 `main`。[Offline checks / 36951114506](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36951114506) 绑定该精确提交，整体 success；[Ubuntu job](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36951114506/job/110664057019) 和 [Windows job](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36951114506/job/110664057258) 的 unittest、CLI check 步骤均 success，公共元数据见 [CI_0120.json](../verification/CI_0120.json)。公共日志请求返回 403，未解析日志，不推断两个平台的测试/跳过数量。最终 CI 元数据与文档补扫见 [REPOSITORY_AUDIT_0120_DELIVERY.json](../verification/REPOSITORY_AUDIT_0120_DELIVERY.json)。后续交付文档提交不修改运行源码，只推送 main，不创建版本标签或发布工具引用。

真实机器人对话、对方 Bot 的响应策略和模型对收尾的判断质量尚未验收，离线工程及 CI 通过不证明线上体验。新版本在用户显式启动的新进程加载，活动实例不自动更新。

## 0.11.0 历史检查点

历史代码版本 **0.11.0** 于 2026-10-01 完成摄入恢复、完整清单审计与单Console实现。该版源码与当时本机 editable 安装包版本一致；活动进程不会因更新自动加载新版。累计知识资源额度经用户明确批准，模型阶段与科学参数保持既有合同。

新版离线证据为[OFFLINE_0110_20261001T220004127125Z.json](../verification/OFFLINE_0110_20261001T220004127125Z.json)（733项、0失败、0错误、14跳过、61.704秒）；初次时序测试失败另保留[OFFLINE_0110_INITIAL.json](../verification/OFFLINE_0110_INITIAL.json)。第二次732项回归的Windows launcher生命周期测试错误保留在[原结果](../verification/OFFLINE_0110_20261001T215307875585Z.json)，原因和修复见[STATUS.md](STATUS.md)。真实材料、知识统计、scratch和数据库只在本机，公开附件不是这些数据的备份。

非 editable 安装包证据见 [WHEEL_0110_20261001T215221456512Z.json](../verification/WHEEL_0110_20261001T215221456512Z.json)：隔离构建、安装后导入、静态检索默认、`check`/`npmi` 入口和三个网页资源检查通过，未调用真实模型或 Discord。[REPOSITORY_AUDIT_0110.json](../verification/REPOSITORY_AUDIT_0110.json) 记录 `main` 可达历史及当前公开文件的有限规则检查，排除未发布工具引用和运行数据；不重扫 release 或外部附件。

最终待提交范围补扫见[REPOSITORY_AUDIT_0110_FINAL.json](../verification/REPOSITORY_AUDIT_0110_FINAL.json)：132个公开文件，具名合成凭据与unittest限定名称误报逐项核验，未知发现为零；有限规则不证明任意秘密不存在。

0.11.0 实现提交为[`661fddf1d034288afe3c3c088e6c99881588aa17`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/661fddf1d034288afe3c3c088e6c99881588aa17)，CI安装流程修订后的验收提交为[`a0dc1a4ad32d61e7356d41876ecb1657d16aa7a6`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/a0dc1a4ad32d61e7356d41876ecb1657d16aa7a6)。[Offline checks / 36933668298](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36933668298)绑定验收提交，整体success；[Windows job](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36933668298/job/110608758760)733项/3跳过/109.644秒，[Ubuntu job](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36933668298/job/110608759051)733项/12跳过/27.255秒，均0失败/0错误，CLI显示0.11.0。元数据与job日志测试摘要见[CI_0110.json](../verification/CI_0110.json)；初次Windows安装前提失败保留[CI_0110_INITIAL.json](../verification/CI_0110_INITIAL.json)，修复与限制见STATUS。CI只证明合成离线检查，不代表真实Bot、前台聚焦或语义召回验收。后续文档提交保持运行源码不变，不把它记为新运行版本。

CI补修公开范围见[REPOSITORY_AUDIT_0110_CI_FIX.json](../verification/REPOSITORY_AUDIT_0110_CI_FIX.json)，最终交付文件补扫见[REPOSITORY_AUDIT_0110_DELIVERY.json](../verification/REPOSITORY_AUDIT_0110_DELIVERY.json)；原始历史与各次检查范围分别保留。

## 0.10.0 历史检查点

文档同步日期：2026-10-01。运行版本保持 **0.10.0**；本次同步文档与公开验证证据，不修改运行实现、配置、模型、预算、知识状态或运行实例。

## 0.10.0 当时确认的代码基线

| 项目 | 检查点与证据 |
|---|---|
| 仓库 | 公开 MIT [Indeces_memory_manager](https://github.com/simulacrum0112-afk/Indeces_memory_manager)，默认分支 `main` |
| 代码提交 | [`f27013910ae52852a7084ad8f2d92132dcda31c7`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/f27013910ae52852a7084ad8f2d92132dcda31c7) |
| 远端 CI | [Offline checks / 36820824993](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36820824993)，绑定上述提交，整体 `success` |
| Windows CI | [check (windows-latest)](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36820824993/job/110235817268)，`success` |
| Ubuntu CI | [check (ubuntu-latest)](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36820824993/job/110235817462)，`success` |
| 本机离线回归 | [OFFLINE_0100.json](../verification/OFFLINE_0100.json)：676 项、0 失败、0 错误、14 跳过，36.285 秒；Windows / CPython 3.12.14，合成数据 |
| 非 editable 安装包 | [WHEEL_0100.json](../verification/WHEEL_0100.json)：安装路径、静态默认、诊断入口与三个网页资源检查通过 |
| 发布范围审计 | [REPOSITORY_AUDIT_0100.json](../verification/REPOSITORY_AUDIT_0100.json)：main 可达历史及当时待提交内容的有限规则检查 |
| 本次文档核对 | [DOCUMENTATION_CHECKPOINT_0100.json](../verification/DOCUMENTATION_CHECKPOINT_0100.json)：公开元数据、文档链接、版本一致性及发布范围补充检查 |

本页固定代码提交，不将后续仅文档提交误记为新运行版本。文档提交本身由 `main` 的 Git 历史标识；本次只推送 `main`，没有新建或推送版本标签，也不发布本地工具引用。

本机完整回归与 wheel 是上述代码版本的既有证据，本次文档更新没有重新执行整套运行测试。14 项跳过属于平台或权限限制，不记为通过；最初 stdin 测试入口造成的 Windows spawn 失败保留在 [OFFLINE_0100_INITIAL.json](../verification/OFFLINE_0100_INITIAL.json)，最终使用带 main guard 的文件入口通过。远端两个 job 的成功由 GitHub 元数据核对，不从中推断未读取的测试明细或真实联网验收。

## 0.10.0 当时行为与兼容性

- 一个 Discord Gateway、一个消息 worker、一个共享模型请求槽；GPT-6-Luna、默认 `verbosity=high`、三阶段 `reasoning=low`，阶段和版本预算保持原值。
- 回复及低层默认使用静态 NPMI；动态 seed/decay/reinforce 是 shadow 历史。旧无模式事件按实际动态合同核验，不能追认为静态实验；同一事件不能跨模式重放。
- 本地知识更新才触发被动标词；聊天标词与聊天自动入库关闭。有效编辑等待或失败时保留旧完整发布版本，新版整篇完成才原子切换；删除、非法输入和超限遵循撤下合同。
- Windows 交互 Console 的 `start`、`observe`、`knowledge` 使用独立窗口，直接 CLI 保持前台。`npmi` 是不读 scratch、不启动实例的完整范围诊断；观察页默认静态图，绘图限额与统计范围分开。
- 已确认送达不被后续日志失败降为未知，也不重复发送；worker 故障关闭准入。纯模型槽等待超时不处罚供应商，合法 usage 后的失败仍计入已知用量。
- 代码推送不改变已加载的服务进程；本次未启动、停止、重启或热补丁任何真实实例，未迁移现有知识或运行数据。

## 0.10.0 当时未验收

真实模型 schema、账号访问、Discord 往返、生产延迟、长时间运行、知识标签质量、召回相关性和复杂 PDF 提取语义仍缺工程验收证据。高 NPMI 只表示块共现关联；单块或单来源支持的高分不能代替语义支持或置信度。

部分历史 `invalid_labels` 失败缺少保留窗口内的完整调用证据，不能确认全部由旧 schema 缺口造成。0.10.0 的 1–8 项、非空白字符串约束与本地校验保持一致，但不据此自动重试旧失败任务。24 小时外的 scratch 原文不恢复或归档。

Yuki 仅作为已跟踪设计文档的参考，未读取或复制其私有配置和运行数据。检查点不包含真实材料、图统计、凭据、SQLite、scratch 或附件，也不是这些本地数据的备份。秘密扫描和本地 hash 都有明确检查范围，不是任意秘密不存在或科学内容正确的证明。

## 0.10.0 当时文档入口

| 文档 | 负责内容 |
|---|---|
| [README](../README.md) | 安装、配置、Console 命令与总体流程 |
| [项目协作约定](../AGENTS.md) | 授权范围、产品边界、数据保护与科研要求 |
| [ARCHITECTURE](ARCHITECTURE.md) | 六个职责、状态归属、生命周期和失败处理 |
| [BASELINE](BASELINE.md) | 固定上游策略、适配差异和历史兼容性 |
| [KNOWLEDGE_DIRECTORY](KNOWLEDGE_DIRECTORY.md) | 原材料、暂存区、候选限额与只读进度 |
| [PDF_IMPORT](PDF_IMPORT.md) | PDF 转换、物理页、版本发布与提取边界 |
| [OBSERVABILITY](OBSERVABILITY.md) | NPMI、来源、shadow 历史与只读网页限额 |
| [RUN_RECORDS](RUN_RECORDS.md) | scratch 合同、计量、字面引用与24小时保留 |
| [STATUS](STATUS.md) | 各版交付证据、修复记录及未验收项目 |
| [REPOSITORY_PRIVACY](REPOSITORY_PRIVACY.md) | 公开范围、历史审计和禁止发布的数据 |
| [第三方说明](../THIRD_PARTY_NOTICES.md) | 固定上游、MIT 归属与架构参考 |
