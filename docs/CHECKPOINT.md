# 当前与历史代码检查点

当前代码版本为 **0.11.0**，2026-10-01完成摄入恢复、完整清单审计与单Console实现。源码与本机editable安装包版本一致；活动进程不会因更新自动加载新版。累计知识资源额度经用户明确批准，模型阶段与科学参数保持既有合同。

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
