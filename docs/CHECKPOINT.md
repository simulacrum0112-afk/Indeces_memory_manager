# Optional model-selection control 0.16.1

This local change is based on `f209fc3006f5a1f3116ffe1f10dfecce486c01b3`.
`[runtime].model_selection_enabled` is a strict boolean, defaulting to `true`
to preserve 0.16.0 behavior and existing configurations. Explicit `false`
constructs Runtime without a model selector and uses its existing deterministic
`graph.retrieve` path with the configured `retrieval_policy`. It does not add
an automatic fallback, new selection algorithm or runtime configuration editor.
The startup receipt reports the actual selected mode.

The patch retains the default model selector and requires no private configuration
or credential migration. No paid test call or answer-quality experiment is included.
The authorized bounded
offline regression passed all 11 methods on its first run, with no failures,
errors or skips. Eight new methods cover default/true/false and invalid toggle
values, unchanged other configuration, both factory paths, zero selection calls
when disabled under both retrieval policies, full quote/run-record contracts and
actual startup-mode receipts with mocked resources. Three existing methods cover
the enabled full contract, the previous deterministic contract and cleanup fault
handling. Static review then strengthened the disabled tests to require exactly
three materials and restricted the driver to the existing eleven-test whitelist.
Only the two affected disabled methods were rerun; both passed. This is coverage
of 11 distinct methods over 13 executions, not a fresh all-eleven final run.
The first run used seven mock input counts and seven mock generations; the two
rechecks added two of each. External HTTP attempts and fees were zero.
No full suite or 20-question run was performed.
Python syntax, example TOML parsing and diff checks also passed. The exact scope,
source hashes and result are retained in
[model-selection-optional/RESULTS.json](../verification/model-selection-optional/RESULTS.json)
and [SOURCE_MANIFEST.json](../verification/model-selection-optional/SOURCE_MANIFEST.json).

The historical test manifest retains the exact prepared `0.16.0` source identity;
it is not relabelled as a test of the later version string. Package and Console
now declare `0.16.1`; the release manifest separately binds the final source,
standard wheel and isolated installed bytes. Version-only consistency checks
reuse the prior behavior regression without a full-suite or 20-question rerun.
The fresh wheel passed all 52 RECORD entries and exact byte checks for its 46
application source/resource files. Its isolated installation matches those bytes;
package metadata, Python version, Console import origin and default enabled mode
are consistent, and CLI help exits successfully. Only `indeces/__init__.py` changes
application bytes after behavior validation; it changes the version string only.
See [RELEASE_MANIFEST.json](../verification/model-selection-optional/RELEASE_MANIFEST.json)
for the separate release identity and wheel digest. No dependencies were downloaded
and no model call was made by these build/consistency checks.
The approved release uses a new release branch and `v0.16.1`, retaining `v0.16.0`
and main unchanged. Console switching uses normal stop/quit/start, with the
selector still enabled. Source publication does not reload an existing process.
To roll back, select the preserved 0.16.0 installation with a normal operator
switch; preserve all state, scratch and previous selection receipts, and never
replay or reinterpret old events. No database schema change is introduced.

# Post-retrieval model selection 0.16.0

The normal Console now constructs `ModelNPMILabelSelector`. Runtime retrieves
and freezes the eligible candidate pool once, releases the SQLite transaction,
awaits one bounded `selection` model call, validates its existing record IDs,
then commits selection and freezes the original full quotes before replying.
Source revision changes during the model call reject the result. No active
requery, graph-weight change, evidence rewriting or automatic retry is added.

The default new stage is 4096 input / 512 output tokens / 15 seconds, with the
reply reasoning effort. Older configurations retain their three explicit stage
budgets and receive only this separate bounded stage; no private configuration
was rewritten. The normal Console enables this policy in code. The independent
manual entry keeps its explicit ranked selector and original finite contract.

The bounded related regression covered 148 distinct test methods. The first
driver rejected Windows asyncio's loopback self-pipe and is retained as an
invalid harness run (101 errors). With the driver corrected, seven methods
failed (12 failure reports) because the new prepared snapshot serialized a
frozenset. Both snapshot and commit comparison now normalize that field.
Only those seven methods were rechecked: five passed; the remaining two
exposed fixture mistakes (the model projection uses `id`, and a synthetic
source update needs `quote`). After correcting those fixtures, both passed.
No full suite or 20-question evaluation was run or read.

The fresh standard wheel passed all 52 RECORD rows and byte checks for its
46 application source/resource files. Its isolated installation passed the
single Console-factory mock smoke: one prepare, one finish, zero requery,
two mock input counts and two mock generations (selection then reply).
The selected candidates were ranks 4/5/6 from a six-candidate synthetic pool,
and the same three IDs/full quotes reached the validated final materials.
External input-count/generation calls and fees were zero. These checks do not
establish real model latency, answer quality or Discord acceptance. The retained
receipts and source identity are in [model-selection/RESULTS.json](../verification/model-selection/RESULTS.json)
and [model-selection/SOURCE_MANIFEST.json](../verification/model-selection/SOURCE_MANIFEST.json).

One failure-classification audit limit remains: an `invalid_model_selection`
receipt is bound to the actual completed result, but the record verifier does
not independently prove why that result violates the selector contract. Success
still requires the full input, output, candidate and selected-material binding.

Publication and switching the existing dev04 process remain separate gates.
The database schema is unchanged; older code may reject new model-selector
receipts, so a rollback preserves all records and never replays or resets them.
The following sections preserve earlier checkpoints and their historical states.

# Default credential-scope checkpoint 0.15.1.dev2026100704

This checkpoint adds an explicit `existing_credential_default` approval scope for the finite manual entry. It requires null account/project IDs and omission of `--account`, preserving the existing credential's default routing without invented IDs or organization/project headers. Legacy approvals retain their `explicit_organization_project` behavior and original ledger binding. Changing a scope under an existing approval ID remains rejected.

The input-count call, fixed approval ledger, usage/failure accounting and all token/time/fee gates remain in force. Unknown counting fees still block model HTTP. This change does not approve a smoke run, establish a provider billing owner or price, start a passive Gateway, or alter the normal Console's defaults. See [MANUAL_TRIAL.md](MANUAL_TRIAL.md) for the two scopes and operator prerequisites. Actual offline validation passed: 81 targeted checks (11 new plus 70 unchanged legacy checks), 1166 full-suite tests with 20 skips and zero failures/errors, and a fresh standard wheel with 51 verified RECORD rows. The wheel's 45 public source/resource files match the frozen checkout exactly. Isolated CLI and mock entry checks passed; missing-metadata live activation was blocked before requests. The recorded full-check interval is 2026-10-08T00:37:07.008540Z to 2026-10-08T00:40:58.170299Z. The raw-source identity is `d48553c165379897494861940d82c05e9bf714c5b65f86a090cdc6d069b15569`; full/build checks bind the preceding `5398b075...` commit plus the uncommitted changes. Exact CI for a later commit has not yet been observed. Selected evidence and prior release records are in [CHECKPOINT_RESULTS.json](../verification/manual-trial/CHECKPOINT_RESULTS.json) and [SOURCE_MANIFEST.json](../verification/manual-trial/SOURCE_MANIFEST.json).

The preceding exact checkpoint [`5398b075cea499c20a861c1df0f4566d96bc578e`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/5398b075cea499c20a861c1df0f4566d96bc578e) passed [CI 37706120215](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37706120215): overall, Ubuntu and Windows jobs, and both unittest/CLI steps were success in public metadata checked at 2026-10-08T00:14:01Z. No remote logs or test counts were inferred. That evidence applies to the preceding commit, not this in-progress source change.

Earlier final-summary reporting conflated the initial offline handoff with later local verification. Corrected provenance is retained in separate local correction-only receipts; private questions, answers, experiment traces and runtime records are not copied into the public checkout. Overall answer-quality acceptance remains failed. Implementation tests and build checks do not establish retrieval or answer quality.

The already launched native Console remains an idle `0.15.1.dev2026100703` instance. Updating source or passing new checks does not reload it. No real model, Discord or held-out smoke has been performed for this checkpoint; previous failures and all original user changes remain preserved.

---

# Windows fixture checkpoint 0.15.1.dev2026100703

The exact prior application checkpoint `06daff583461953cc6fc65b1101ed56e0988b961` passed this machine's 1155-test suite, but CI run `37703149254` failed its Windows unittest step; Ubuntu succeeded. Anonymous access to that exact job's logs returned HTTP 403; annotations expose exit code 1 only. The remote failed case names and original remote cause remain unknown.

A separate controlled Windows reproduction found a test fixture dependency on checkout location: two unchanged orchestration tests passed only when the test file path lay within the fixed approved project root. Their neutral checkout reproduction failed twice before the fixture correction and passed twice afterwards. The correction changes only Fixture.setUp: its unique logical config remains under the original fixed approval root, while an exact read mapping supplies real synthetic bytes from the owned Temp fixture. All other reads, all 70 test methods/assertions, and all product sources remain unchanged. This is a demonstrated fixture defect, not a claim that unavailable remote logs confirmed its cause.

Fresh complete regression and standard wheel/isolated-install results are recorded in verification/manual-trial/CHECKPOINT_RESULTS.json. The final application identity remains `b77eeea4c54990c2b554ba5a97e24f396bd97b116824b9f76f52d0c12461a30e`; exact append-commit CI is delivered separately. Every prior failure and receipt is retained, and main plus the 17 original user changes remain unchanged.

The authorized native Console was actually launched from an isolated installed wheel and displayed `Indeces 0.15.1.dev2026100703 console` with its menu prompt. It remains idle: no Bot, Gateway, knowledge worker or model request was started by this task. The bounded USD 1 smoke remains blocked before model HTTP because fee/account hard-cap evidence is incomplete. Earlier sections describe their then-current states. No real answer-quality or held-out acceptance is claimed.

---

# Filesystem shutdown checkpoint 0.15.1.dev2026100703

A fresh full run of exact checkpoint `b9c06222989dd17b28baffce2e3c44f0f5e15505` found one Windows PDF watcher teardown error: a reader thread could still hold a synthetic file after KnowledgeService.close returned. Cancellation of the asyncio wait did not stop the underlying filesystem read. This checkpoint retains service ownership of scan/read tasks and waits for their completion before shutdown returns, without publishing cancelled snapshots, changing model budgets or stopping the shared executor.

The prior Linux fixture correction remains; its assertions and production project boundary were preserved. Prior commits, failed CI runs, all full-check receipts and the original 17 user changes remain intact. Targeted lifecycle regression, a new exact-source full regression, rebuilt wheel and exact-commit CI must be recorded separately; earlier passing results are historical evidence. Real model/Discord/held-out smoke remains unrun.

The delivery receipt binds the final source, wheel, command, actual UTC intervals and visible CI state. See [MANUAL_TRIAL.md](MANUAL_TRIAL.md) for operator prerequisites and rollback.

---

# Manual-entry checkpoint 0.15.1.dev2026100702

This independent checkpoint adds the selector protocol and an explicit finite manual entry. The existing main worktree, its 17 user changes, earlier experiments and production runtime are preserved. The source base is clean main `94ae7bd15120070fb24f88d0e7bd8d4e3d622e20`; the checkpoint branch is `checkpoint/manual-live-ready-20261007`.

The real entry reuses OpenAIAdapter and Discord dispatch, with default-off activation, full operator metadata checks, the original state lease, a fixed approval ledger, 2 count/2 generation limits, cumulative token/time/fee gates and persistent failure stops. It starts no knowledge maintenance or summary stage. See [MANUAL_TRIAL.md](MANUAL_TRIAL.md) for prerequisites, exact limits, held-out separation, billing trust and rollback boundaries.

All validation in this development session is offline and synthetic. The delivery report distinguishes targeted checks, full-suite initial failures and corrected checks, standard build/installed wheel checks, and visible CI status. Its exact commit and source identities are recorded as delivery evidence; a version number does not identify an already running service. No real model/Discord/held-out smoke or deployment has been performed. Expenses remain unapproved until the operator supplies a complete approved descriptor.

The sections below preserve earlier delivery records and their then-current states; their runtime/model evidence must not be attributed to this new manual checkpoint.

---

# 当前与历史代码检查点

当前源码 **0.15.0**：用户授权通用检索覆盖修复及有界恢复。Runtime 默认 concept_v1，显式 legacy_v1 可回滚；归一化、64个问题焦点/IDF命中、128个有界正文候选、概念/正文分数与元数据降权替换长度四词及原始命中数量优先。原始记录、图公式/一跳与context gate、static/dynamic分层、三个完整[M]引用、self-reasoning边界、Sol/medium及所有调用额度保留。新策略与旧审计分版核验，不重释旧事件。参数表、算法和边界见 [RETRIEVAL_COVERAGE.md](RETRIEVAL_COVERAGE.md)。

摘要保持4096字节，以字符schema及较低生成目标约束；边界碰撞或超长不保存checkpoint、不重试，保留全部原历史并明确声明连续近期原文的上下文缺口。raw试算发现初始字符schema可以裁断末句，已增加边界拒收和目标压缩；最终两份原失败输入均返回未撞限的完整句子。独立试算单独分类并校验实际transport/输入输出/usage，不再因没有turn_start误报chat。历史缺失chat入口仍拒绝。

用户明确确认所选文献旧未知用量按未量化损失关闭；独占state租约、一致备份后先3块pilot，再完成整篇全部块并事务发布。无新失败/未知usage，旧记录/checkpoint指纹、原PDF digest和数据库完整性核验通过；旧未知值未记零。第一次单篇grant仍用了旧四篇描述符，原记录保留并添加operator scope说明；新grant使用所选来源描述。未启动、停止或热补丁Discord。

原8道金标准逐题实际模型试算均有相关正文/否定证据进入上下文；实际引用材料与模型输入一致，无悬空[M]。不是所有旧公式/图注都进前三，较完整正文替代了它们；逐题私有排名/文字仅在本机scratch并向用户报告，不进入公开Git。“前”用原冻结记录，“后”也包含授权恢复的文献，频率可能略变。此为有限金标准与独立模型验收，不代表所有未来召回、PDF语义/版面、真实Discord或既有Console加载验收。当前不再把降低reasoning作为修复方案。

最终本机 [OFFLINE_0150_20261004T213036033330Z.json](../verification/OFFLINE_0150_20261004T213036033330Z.json)：1061项、20跳过、0失败/错误，CLI/Console/依赖检查通过；[WHEEL_0150.json](../verification/WHEEL_0150.json) 隔离非editable安装、维护入口及配置/静态检查通过。公开报告只含合成测试，真实证据仍只留本机scratch。

运行实现提交 **6538d980d2a201ace0a8bfcf06274affc00abbe6** 已推main。精确提交 [CI 37236537569](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37236537569) 整体、Windows/Ubuntu job及unittest/CLI check步骤均success；[CI_0150.json](../verification/CI_0150.json) 仅公共run/job/step元数据，未读远端日志、不推断远端测试数。发布前 [REPOSITORY_AUDIT_0150_RELEASE.json](../verification/REPOSITORY_AUDIT_0150_RELEASE.json) 核验main可达历史及精确暂存文件，有限规则未知发现和禁止路径均零。交付文档补扫见 [REPOSITORY_AUDIT_0150_DELIVERY.json](../verification/REPOSITORY_AUDIT_0150_DELIVERY.json)。这些检查不证明既有Console已加载；需用户重开Console后显式start。

此前全套1058项揭示旧重放路径读取全payload的两项回归，已用小型策略/上下文事件头修复；随后1058、1059及最终1061均通过，初始失败报告保留，不代替最终结果。


当前源码 **0.14.1**：按用户给定记录只读审计，故障发生在摘要生成等待，输入计量已完成、无模型槽等待，生成响应头/body及usage尚未返回即达到原阶段时限。因此没有可还原的生成raw output，旧用量仍为未知。两次摘要请求使用同一checkpoint及相同输入；失败保留原历史且不推进coverage，下一次聊天仍需同一摘要。客户端记录不能区分供应商生成、远端排队与网络耗时。诊断、参数差异及适用边界见 [SUMMARY_TIMEOUT.md](SUMMARY_TIMEOUT.md)。

用户明确批准摘要阶段20→60秒、总轮100→130秒和一次真实摘要试算；模型Sol/medium、各阶段token、其他阶段时限、high话量、串行槽、切块/图/水位/保留规则不变。本机配置在独占state租约下先保存原字节备份，仅修改两项解析字段及其数字文本，核验其余字段和state绑定一致；不热补丁既有进程。相同失败输入的单次真实试算返回completed、合法usage、非空合规摘要及有效UTF-8长度，耗时超过旧上限且在新上限内，输出token未耗尽。试算没有更新checkpoint、重置旧记录或发送Discord消息，没有自动重试。私有输入、输出、调用ID及计量仅保存在本机既有scratch，不入公开仓库。

用户另授权延时若仍不能解决则降low并恢复原时限；本次试算成功，保留medium，未触发该条件，也未加入自动切换。单次试算只验证所定位的摘要输入，不代表全部未来延迟、推理质量或Discord链路验收。未启停/重启Discord；源码0.14.1和现有安装元数据0.13.0不证明既有Console已加载，用户需重开Console后显式start。

[OFFLINE_0141.json](../verification/OFFLINE_0141.json)：1042项、20跳过、0失败/错误，CLI/Console/依赖/npmi通过；[WHEEL_0141.json](../verification/WHEEL_0141.json) 构建及隔离非editable安装通过。新增合成回归模拟25秒摘要：旧20秒拒绝，新60秒通过，同时核验medium、原输出cap、一次计量及一次生成；原摘要失败/coverage保护测试通过。这些离线测试与获批真实试算分开记载。

运行实现提交 **23107a337185b1a8720f0112b15914e638ed7fdf** 已推main。精确提交 [CI 37228155427](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37228155427) 整体、Windows/Ubuntu job及unittest/CLI check步骤均success；[CI_0141.json](../verification/CI_0141.json) 仅保存公共run/job/step元数据，未读远端日志、不推断测试数量。[REPOSITORY_AUDIT_0141_RELEASE.json](../verification/REPOSITORY_AUDIT_0141_RELEASE.json) 核验main可达历史和精确暂存公开文件，有限规则未知发现/禁止路径均零；交付文档和CI元数据补扫见 [REPOSITORY_AUDIT_0141_DELIVERY.json](../verification/REPOSITORY_AUDIT_0141_DELIVERY.json)。私有配置、凭据、材料和运行数据未包含。后续交付文档提交不修改运行源码，不以其HEAD替代精确CI目标。

历史源码 **0.14.0**：用户明确要求基座 `gpt-6-luna`→`gpt-6.1-sol`、回复/摘要/标词 `low`→`medium`，其他设置不变。已更新固定模型准入、推理默认值、示例配置、适配器描述与四篇维护校验；新模型不支持的none本地拒绝，支持的显式推理值仍保留，加载器不静默替换旧模型。话量high、阶段/累计/维护token与时间额度、串行槽、切块、prompt/schema、图及记录合同不变。完整差异与兼容边界见 [MODEL_MIGRATION.md](MODEL_MIGRATION.md)。

[OFFLINE_0140.json](../verification/OFFLINE_0140.json)：1041项、20跳过、0失败/错误，CLI/Console/依赖/npmi通过；[WHEEL_0140.json](../verification/WHEEL_0140.json) 构建与隔离非editable安装通过。输入计量/生成和scratch的实际模型、medium、高话量及原token/时间上限在三个阶段均有合成验证，维护路径也通过。旧Luna/low日志fixture显式保留原强度继续验证历史兼容。定向测试最初有一项旧负例仍以medium充当不同强度，已改为low；另一次HTTP合成测试得到stage_timeout而不是预期HTTP错误，该任务显示耗时1.5秒、测试期限1秒，独立重测和最终全套均通过。冷启动开销是未独立定位的推测，未调整运行时限或据此断言真实API耗时。

本机配置在取得独占state租约后先保存原字节备份，仅迁移模型和三个reasoning字段；其余解析字段、未修改文本及state目录绑定核验通过。未读取或迁移知识库/私有材料/历史账本、重标、执行维护或真实模型调用；未启停/热更新Discord。当前源码由新Python进程可加载为0.14.0，现有环境安装元数据仍0.13.0；均不能证明既有Console已加载新版。用户需重开Console，再显式start。私有配置和备份不入Git。[REPOSITORY_AUDIT_0140_RELEASE.json](../verification/REPOSITORY_AUDIT_0140_RELEASE.json) 核验main可达历史与公开暂存文件，有限规则未知发现/禁止路径为零；交付文档与CI元数据补扫见 [REPOSITORY_AUDIT_0140_DELIVERY.json](../verification/REPOSITORY_AUDIT_0140_DELIVERY.json)。

运行实现提交 **7bf3094661aeeba85651874837f0705c973be155** 已推main。精确提交 [CI 37223398117](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37223398117) 整体、Windows/Ubuntu job与unittest/CLI check步骤均success；[CI_0140.json](../verification/CI_0140.json) 仅保存公共run/job/step元数据，未读远端日志、不推断测试数量。随后交付文档提交不修改运行源码，也不以其HEAD冒充这个精确CI目标。

历史源码 **0.13.2**：显式四篇维护使用独立 grant/attempt/call/request/label/event 账本；operator 对旧未知损失的确认追加保存，旧版本、标词、计量和失败审计不重置。发送前持久唯一请求 ID 和占额，返回头/实测 usage 在 scratch/标词校验前持久保存。未知生成停整批，跨崩溃、混合孤儿请求和重复启动仍禁止自动重放；新 grant 不能重领已绑定 digest 的额度。全局配置不修改，45秒与新增累计资源仅用于本任务。完整合同见 [BOUNDED_REINGEST.md](BOUNDED_REINGEST.md)。

最终 [OFFLINE_0132_20261003T233908239711Z.json](../verification/OFFLINE_0132_20261003T233908239711Z.json)：1040项、20跳过、0失败/错误，CLI/Console/依赖检查通过。此前 [OFFLINE_0132.json](../verification/OFFLINE_0132.json) 1039项通过，后补计数响应边界测试；最终之后仅补充失败阶段元数据及对应2项定向故障检查，均通过。最终 [WHEEL_0132_20261003T234101621355Z.json](../verification/WHEEL_0132_20261003T234101621355Z.json) 已验证最新阶段元数据代码的隔离安装与维护入口导入。这些合成测试不代表真实模型、PDF语义/版面或Discord验收。

运行实现提交 **50fc8ffb9732f057f521a90deb7369dfdde6bcd9** 已推main。精确提交 [CI 37162611377](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37162611377) 整体、Windows/Ubuntu job及unittest/CLI check步骤均success；[CI_0132.json](../verification/CI_0132.json) 只保存公共run/job/step元数据，未读远端日志、不推断测试数。公开范围 [REPOSITORY_AUDIT_0132_RELEASE.json](../verification/REPOSITORY_AUDIT_0132_RELEASE.json) 核验main可达历史和精确暂存文件，有限规则未知发现/禁止路径均零；交付文档补扫见 [REPOSITORY_AUDIT_0132_DELIVERY.json](../verification/REPOSITORY_AUDIT_0132_DELIVERY.json)。

operator 已明确确认旧四次失败按未知用量损失关闭、不再核验精确usage；确认不是将未知用量记零，旧失败状态和累计计量保留。取得独占state租约并在数据库变更前完成SQLite一致备份后，真实pilot通过，再在同一grant内完成四篇标注并逐篇事务发布。新账实测usage与请求ID回执齐全，无新增未知用量或失败，旧记录指纹、原PDF digest及数据库完整性核验通过；只读进度展示新发布版本ready并保留旧失败证据。检索抽查区分实际标词与查询命中，不把其他标签命中归为所查概念已成标词；并非所有目标概念都形成新标签，不证明PDF纸面内容、语义提取或科学相关性全部通过。私有文件名、原文、digest、请求ID及实际运行计量只留本机既有账本/scratch，不写入公开仓库。维护进程加载上述源码；未启动、重启或热补丁Discord服务。以下状态属于历史轮次。

历史源码 **0.13.1**：知识摄入失败／取消显示逐篇 Console 通知（路径、阶段、code、完成块数、恢复限制）；持久审计与 scratch 保存零基失败块位置。重开后遗留 labelling 会补建失败审计和可见通知，仍标记未知 usage 并阻止自动重放。此前失败已持久保存，但普通逐篇失败通知被省略，旧测试也要求失败时 Console 静默；本次更新该合同。取消回执补齐与版本状态一致的错误码。成功和普通进度不新增通知。

0.13.1 离线验证：[OFFLINE_0131.json](../verification/OFFLINE_0131.json) 1000项、20跳过、0失败/错误，CLI/Console/依赖/npmi通过；[WHEEL_0131.json](../verification/WHEEL_0131.json) 构建及隔离非editable安装通过。这些为合成离线验证，不代表真实论文恢复、真实模型或Discord验收。

模型、15 秒标词阶段、累计额度、切块、并发槽、图规则和整篇事务发布保持基线；供应商生成超时的远端原因未证实，不以扩额或自动重试处理。真实论文诊断按用户本次明确授权限定只读；私有文件名、来源、原文和运行数据不写进公开交付文件。真实实例仍持有 state 租约，未停止、重启、热更新或迁移，未执行真实重摄入／模型验收。未知 usage 的恢复须核验用量或另获明确例外授权；已完成块和累计计量不得清零。源码版本0.13.1；活动环境的已安装元数据仍0.13.0，未为更新版本标识而修改运行环境。下方0.13.0验证证据仅适用于历史版本，0.13.1验证另列。

历史源码版本 **0.13.0**；2026-10-03（用户时区）完成存储层持久索引和静态命中邻域查询。用户明确批准关闭静态 shadow 更新，并在同一冻结动态状态下比较相关边全部字段／结果／排序／来源证据。NPMI、门控、去重、一跳、[M]、5 秒预算、并发与五轮不变。新 schema 和完整边界见 [MEMORY_STORAGE.md](MEMORY_STORAGE.md)；旧 lazy ledger 方案没有激活，仅留纯函数与时间戳结构。

固定 415 条相关边、410 条决策，1000/3000/10000 marks、三次中位：cold retrieve 为 .062/.075/.082 秒；含快照、真实合成 observation scratch flush/fsync 和 freeze 的预算链路为 **.100/.107/.119 秒**，steady 为 .048/.061/.072 秒。无关全图正边增至 49,139/151,195/502,499；固定查询 SQL 均784条、相同 cache模式的每100 VM回调计数跨规模相同。增长邻域406/1206/4006边的cold预算链路为.074/.246/2.110秒。全部54样本预算链路最大2.491秒，更宽的后续落盘＋独立校验链路最大3.966秒；在这些所测条件下均通过原5秒，不证明任意枢纽/输出规模或真实Discord时限。

[磁盘 SQLite 曲线](../verification/subgraph_disk_after_20261003.json) 使用实际临时数据库、WAL/FULL/foreign_keys=ON，固定415边的1k/3k/10k各三次cold预算链路中位 **.091/.105/.078秒**，最大.093/.127/.084秒，准备库约76.9/236.2/790.3MB。全部9次同冻结状态字节对照及独立校验通过，与主曲线合计63个合成样本。cold不表示OS页面缓存冷；磁盘10k摄入178.262秒、初始化中位11.979秒，单列且不在回复预算内。

新包 schema=2/audit_scope=direct_hit_neighborhood_v1，频次仅相关端点；完整计数是发布标量。整包/hash与旧版不同，禁止称为全图状态冻结。每个样本完整records、相关边/决定/排序/证据与旧版同冻结状态canonical bytes一致，实际新包独立校验，动态表不变。旧包及原hash保留，旧重放只查不可变事件头；摄入正确更新索引、稳定坐标、失效与事务回滚有独立测试。

最终 [OFFLINE_0130_20261003T181400856152Z.json](../verification/OFFLINE_0130_20261003T181400856152Z.json)：**998项、20跳过、0失败/错误**，CLI/Console/依赖/npmi通过；[WHEEL_0130.json](../verification/WHEEL_0130.json)构建及隔离安装通过，源码/editable均0.13.0。初次 [OFFLINE_0130.json](../verification/OFFLINE_0130.json)误用未装项目依赖的系统Python3.14，不能作为交付；也暴露旧shadow断言，已更新为当前关闭语义并保留历史v1 fixture覆盖。

[主曲线](../verification/subgraph_after_20261003.json)绑定实际运行源文件hash；[SOURCE_0130.json](../verification/SOURCE_0130.json)核对测量源与运行源码提交，单独说明提交前HEAD及CRLF/LF差异。[Python3.12旧单次成本](../verification/subgraph_baseline_py312_20261003.json)保留原全shadow合同，仅成本对照，不能声称同包相等或与新3次中位完全同条件。[早期Python3.14旧测量](../verification/subgraph_baseline_20261003.json)另存，不作跨解释器精确倍率推断。内存10k固定图摄入50.358秒，cold初始化中位5.480秒、最大5.977秒，仍全局且不在回复预算内。

运行源码 [`06be07c`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/06be07c6444e73833ed4d3eb75c29f51c49b9ac8) 已推送main并核对远端HEAD；精确提交 [CI 37143587991](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37143587991) 整体、Windows/Ubuntu job、unittest与CLI check步骤均success，见 [CI_0130.json](../verification/CI_0130.json)。仅公共run/job/step元数据，未读日志、不推断远端测试数量。后续交付文档／合成磁盘证据提交不改变此运行源码。

公开范围 [REPOSITORY_AUDIT_0130_RELEASE.json](../verification/REPOSITORY_AUDIT_0130_RELEASE.json) 已核验main可达历史和精确暂存文件，有限规则未知发现／禁止路径均零；交付补扫见 [REPOSITORY_AUDIT_0130_DELIVERY.json](../verification/REPOSITORY_AUDIT_0130_DELIVERY.json)。未包含私有材料、配置、运行数据或本地工具引用，只推送main。

本轮未访问真实知识库、私有配置或运行数据，未迁移真实库、未启停/热补丁真实实例，真实模型/Discord未验收。不以源码、CI或离线测量证明真实服务已加载或召回质量。

## 0.12.6 历史检查点

历史源码版本 **0.12.6**；2026-10-02（用户时区）完成密集静态查询的相关边物化与显式审计范围。NPMI、来源/context gate、top-k、去重/[M]、static/dynamic 分层、全 shadow 轨迹和 5 秒预算保持。相关结果/排序/证据规范 bytes 与独立旧版对照一致；旧完整事件及 hash 保留，详情及用户要求的延迟更新待审方案见 [MEMORY_QUERY_SCOPE.md](MEMORY_QUERY_SCOPE.md)。

固定 415 条相关边、410 条决策，3 次 steady 中位数：1000/3000 marks 的 retrieve 从 1.458/5.807→0.802/4.448 秒；retrieve＋snapshot＋freeze 从 3.973/14.303→2.057/11.217 秒。新相关材料化＋选择约 5.63/9.28 毫秒。全图 49,139→151,195 条边；局部路径已收窄，但**整链路仍随全 shadow/频次/审计字节增长，3000 marks 尚未满足 5 秒**。冷索引重建也保留全图成本，不宣称全链路目标已达成。

最终 [OFFLINE_0126_20261003T031610529771Z.json](../verification/OFFLINE_0126_20261003T031610529771Z.json)：958项、0失败/错误、20跳过，CLI/Console/依赖/npmi通过；[WHEEL_0126.json](../verification/WHEEL_0126.json) 隔离构建/安装通过，源码/editable均0.12.6。初次957项2失败保留于 [OFFLINE_0126.json](../verification/OFFLINE_0126.json)：新增负NPMI测试数据已修正；槽超时测试单独30次及最终全套通过，但初次具体失败原因未确认，不臆断为生产 bug。性能初始计时边界修正和全部证据条件见 [证据说明](../verification/dense_retrieval_evidence_notes_20261002.json)。

运行源码 [`db04135`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/db04135fa5e0535a633de649cbd40c77df38b3cc) 已推送main并核对远端HEAD；精确提交 [CI 37092790001](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37092790001) 整体、Windows/Ubuntu、unittest与CLI check步骤均success，见 [CI_0126.json](../verification/CI_0126.json)。只读公共元数据，未读日志/推断远端测试数量。后续交付文档提交不改变该运行源码。

公开范围 [REPOSITORY_AUDIT_0126_RELEASE.json](../verification/REPOSITORY_AUDIT_0126_RELEASE.json) 核验main可达历史和精确暂存公开文件，有限规则未知发现/禁止路径均零；交付补扫见 [REPOSITORY_AUDIT_0126_DELIVERY.json](../verification/REPOSITORY_AUDIT_0126_DELIVERY.json)。本轮未读取真实知识库/私有配置/运行数据，未启动、停止、热更新真实实例，未应用 shadow lazy/v2 或真实库迁移。用户只授权其设计、先报告再应用。

## 0.12.5 历史检查点

当前源码版本 **0.12.5**；2026-10-02（用户时区）知识库路径与旧来源根身份隔离修复。OneDrive、外目录知识配置和链接拒绝；安全错误阻断入库；旧缓存迁移先备份并核验当前文件digest，保留原文/PDF/标词/计量，未知usage不能获得新预算。事故与调查限制见 [PATH_BOUNDARY.md](PATH_BOUNDARY.md)。

最终 [OFFLINE_0125_20261003T000327789883Z.json](../verification/OFFLINE_0125_20261003T000327789883Z.json) 为945项、0失败/错误、20跳过、161.270秒，CLI/Console/依赖/只读npmi通过；[WHEEL_0125_20261003T000101467827Z.json](../verification/WHEEL_0125_20261003T000101467827Z.json) 隔离构建和安装通过，源码/editable元数据均0.12.5。真实Windows junction、8.3短名与reparse模拟拒绝通过，symlink权限和平台差异跳过逐项保存。首次失败记录/等待夹具及平台修正见 [STATUS.md](STATUS.md)。

[实现公开范围有限审计](../verification/REPOSITORY_AUDIT_0125_UPGRADE.json) 核验main可达历史与精确暂存公开文件，有限规则未知发现/禁止路径均零；最终交付补扫见 [REPOSITORY_AUDIT_0125_DELIVERY.json](../verification/REPOSITORY_AUDIT_0125_DELIVERY.json)。早期有限检查保留 [REPOSITORY_AUDIT_0125.json](../verification/REPOSITORY_AUDIT_0125.json)。不含私有目录名、原文、真实运行统计和工具引用。

最终运行源码 [`0d78ddb`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/0d78ddbb1b379e43caf252756b19f26476b757bc) 已推送main并核对远端HEAD；精确提交 [CI 37080507076](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37080507076) 整体与Windows/Ubuntu job、unittest及CLI check步骤均success，见 [CI_0125.json](../verification/CI_0125.json)。此最终CI只读公共元数据，未读日志、不推断远端测试数量；早期失败诊断阅读另见下文。后续交付文档提交不改变该运行源码。

首版运行源码 [`b8de289`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/b8de28993b6233511ed0fdde6d52ac470e6d0e7a) 的 [CI 37076777661](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37076777661) Ubuntu因兼容错误文字断言失败，Windows被取消，见 [CI_0125_INITIAL.json](../verification/CI_0125_INITIAL.json)。兼容提示修正 [`6ff12fa`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/6ff12fa64b542a6709b71cf02010589fa06f4b97) 的 [CI 37077154547](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37077154547) Ubuntu成功、Windows因ANSI通知与8.3短名错误失败，见 [CI_0125_SECOND.json](../verification/CI_0125_SECOND.json)。本机最终验证已包含这些修正，最终CI须绑定随后平台修正提交，历史CI不能代替本版结果。

平台源码 [`fb3d203`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/fb3d20388a2e6dc574606584f0a07d593bc7e047) 已推送main；精确提交 [CI 37078394744](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37078394744) Ubuntu成功，Windows的6项fixture仍比较短名与canonical文字而失败，见 [CI_0125_THIRD.json](../verification/CI_0125_THIRD.json)。随后测试预期规范化，不改变生产代码。平台修正暂存公开范围见 [REPOSITORY_AUDIT_0125_PLATFORM_FIX.json](../verification/REPOSITORY_AUDIT_0125_PLATFORM_FIX.json)，未知发现/禁止路径均零。

路径验收提交 [`2789ec5`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/2789ec51800f26f13a98a68ff37ac7ccfbfeb77c) 的 [CI 37078974247](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37078974247) Windows/Ubuntu与unittest/CLI check均成功，见 [CI_0125_PATH_SUCCESS.json](../verification/CI_0125_PATH_SUCCESS.json)。随后补齐施工中间版的v2禁发迁移、普通撤下/替换的旧digest持有与retry门禁；上述945项、最新wheel和最终0d78ddb CI均已包含这项升级保护。

本轮没有启动/停止真实服务，没有对活动真实数据库应用迁移。另chat在本轮施工期间启动的实例不能据0.12.5版本号认定已加载最终修复，须待当前任务自然结束后由用户显式stop、清理、quit、重开/start。

## 0.12.4 历史检查点

历史代码版本 **0.12.4**；2026-10-02（用户时区）修复回复模型材料包含随知识库增长的图证据而导致固定上下文超限。`citation_material_v1` 保留完整quote、标注词、来源、[M]、PDF页码及排序摘要；全图证据留在完整审计，独立验证及旧记录兼容保持。模型预算、并发、五轮和检索选择规则不变。

[MODEL_CONTEXT_0124_20261002T213232430876Z.json](../verification/MODEL_CONTEXT_0124_20261002T213232430876Z.json) 比较25/100/200条同标签合成记录：模型材料7927/12014/19216→2608/2609/2611字节，原额度下均可准入；完整图审计字节/选择/数据库/材料原文保持，整个模型视图收据按新声明变化。最终 [OFFLINE_0124_20261002T213550181440Z.json](../verification/OFFLINE_0124_20261002T213550181440Z.json)：887项、0失败/错误、14跳过、68.188秒，CLI/Console/依赖/只读npmi通过；[WHEEL_0124.json](../verification/WHEEL_0124.json) 隔离构建/安装验证通过，本机源码/editable均0.12.4。首次失败记录和修正原因见 [STATUS.md](STATUS.md)。

运行源码 [`3a422d1`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/3a422d1dcdc9006e7879d147aa3801809e6b1902) 已推送 main；精确提交 [CI 37067919192](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37067919192) 整体与Windows/Ubuntu job、unittest、CLI check步骤均 success，见 [CI_0124.json](../verification/CI_0124.json)（仅公共元数据，未读日志/不推断远端测试数量）。[公开范围有限审计](../verification/REPOSITORY_AUDIT_0124.json) 覆盖此前main可达28提交/492唯一blob及181个公开文件，未知发现/禁止路径零；最终交付补扫见 [REPOSITORY_AUDIT_0124_DELIVERY.json](../verification/REPOSITORY_AUDIT_0124_DELIVERY.json)，后续文档提交不改变已验证运行源码。

没有自动启停/热加载真实服务，真实模型与Discord仍待用户验收。旧Console须stop、等待清理、quit再重开/start加载新版；历史CI不代替本版证据。

## 0.12.3 历史检查点

历史代码版本 **0.12.3**；2026-10-02（用户时区）保留完整审计和结果/排序字节，缓存材料相关索引并让匹配、邻接和候选查找只访问相关部分。原一跳规则、全动态shadow更新、来源门禁、模型/本地5秒预算不变。

最终合成基准见 [MEMORY_INDEX_0123.json](../verification/MEMORY_INDEX_0123.json)：每组5次，含聊天状态写入，首轮1k/3k由0.199/0.697降至0.061/0.211秒，暖查询0.223/0.759降至0.048/0.171秒；全部完整结果/audit字节与改前一致。完整审计仍约1.8/5.4MB，最终缩放倍数约3.46/3.56；**未证明全链路线性，也不保证任意规模在5秒内完成**。发布开销、方法和局部索引计数证据见 [STATUS.md](STATUS.md)。不发布临时MB合成基线或真实运行数据。

最终 [OFFLINE_0123_20261002T210751360525Z.json](../verification/OFFLINE_0123_20261002T210751360525Z.json) 为872项、0失败、0错误、14跳过、74.559秒，CLI/Console/依赖/只读npmi通过；[WHEEL_0123.json](../verification/WHEEL_0123.json) 隔离构建/安装、版本、静态默认与资源验证通过，本机源码/editable均0.12.3。运行源码 [`de07bdb`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/de07bdb7437893e8009d1e74075bea8f92828ddc) 已推送 main；精确提交 [CI 37065149904](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37065149904) 的 Windows/Ubuntu job、unittest 与 CLI check 步骤均 success，见 [CI_0123.json](../verification/CI_0123.json)（仅公开元数据，未读日志/不推断远端测试数量）。[公开范围有限审计](../verification/REPOSITORY_AUDIT_0123.json) 覆盖此前main可达26提交/468唯一blob及170个公开文件，未知发现/禁止路径为零；本次交付补扫见 [REPOSITORY_AUDIT_0123_DELIVERY.json](../verification/REPOSITORY_AUDIT_0123_DELIVERY.json)，不改变已验证运行源码。本机不自动启停真实服务，旧Console须stop、等待清理、quit再重开/start才加载新版。

## 0.12.2 历史检查点

历史代码版本 **0.12.2**；2026-10-02（用户时区）修复已准入机器人失败无提示的问题，并共享不可变完整审计快照减少检索重复处理。失败回执按类型与模型回答区分，关闭提及；模型 skip 仍静默，五次预占/去重、图规则、模型额度与原 5 秒本地预算不变。旧服务已加载与真实失败的诊断证据只在本机，不发布原文、数据库或真实统计。

[WHEEL_0122.json](../verification/WHEEL_0122.json) 的非 editable 隔离构建/安装、静态默认、CLI 与网页资源验证通过；本机源码与安装包均为0.12.2。[OFFLINE_0122.json](../verification/OFFLINE_0122.json)为850项、0失败、0错误、14跳过、65.531秒；新增35项，CLI/Console/依赖/只读npmi检查通过。运行源码 [`549fb161a0a9c749f9f534ff0aba9cc5dd0a8247`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/549fb161a0a9c749f9f534ff0aba9cc5dd0a8247) 已推送 main，绑定精确提交的 [CI 37062176475](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/37062176475) 整体 success；Windows/Ubuntu job、unittest 与 CLI check 步骤均 success，公共元数据见 [CI_0122.json](../verification/CI_0122.json)。未读取远端job日志，不推断其测试数量。下列历史CI不能代替本版本的证据。公开范围有限审计见 [REPOSITORY_AUDIT_0122.json](../verification/REPOSITORY_AUDIT_0122.json)：main 可达24提交、439个唯一blob（排除空目录哨兵）及160个公开文件；210个有限规则命中按精确历史blob或已声明unittest限定名称分类，未知发现和禁止路径为零，不发布工具引用或真实运行数据。

最终交付文档/公共CI元数据补扫见 [REPOSITORY_AUDIT_0122_DELIVERY.json](../verification/REPOSITORY_AUDIT_0122_DELIVERY.json)；后续交付提交保持运行源码不变，只推送main。

没有自动启动、停止或热加载真实实例，也没有真实模型或Discord请求。旧Console仍保留加载的模块；需要用户明确结束活动任务、退出并重开Console再start。真实Discord送达与对方机器人响应、收尾判断和召回语义质量仍未验收；磁盘I/O、高负载与任意规模本地硬时限不作保证。

# 0.12.1 历史代码检查点

当前代码版本 **0.12.1**；2026-10-01（用户时区）修复已准入机器人提问在模型前的可复现检索超时。重复全边扫描、证据 JSON 处理、完整审计编码和快照复制已优化；静态排序、完整审计、5 秒本地预算及原模型额度不变。失败记录区分真实门禁中断和其他 SQLite 错误。事故诊断只读原数据，试算写入仅位于进程内 SQLite 副本，原文/数据库/统计不进入公开附件。

本机 [OFFLINE_0121.json](../verification/OFFLINE_0121.json) 为815项、0失败、0错误、14跳过、76.495秒；新增31项，含冻结的原算法完整结果/审计/DB兼容、日志逐字节兼容、快照隔离、真实SQLite中断和后续消息恢复。CLI/Console/依赖与只读 npmi 检查通过。[WHEEL_0121.json](../verification/WHEEL_0121.json) 的隔离构建/安装和资源/入口检查通过。源码与本机安装包均为0.12.1；测试仅使用合成数据，没有真实模型或Discord请求。

0.12.1 运行源码提交为 [`75e90f7851df9f917b6385437641f3dc54d03ff3`](https://github.com/simulacrum0112-afk/Indeces_memory_manager/commit/75e90f7851df9f917b6385437641f3dc54d03ff3)，已推送 main。[Offline checks / 36954818516](https://github.com/simulacrum0112-afk/Indeces_memory_manager/actions/runs/36954818516) 绑定该精确提交，整体 success；Windows 与 Ubuntu 的 job、unittest 和 CLI check 步骤均 success，见 [CI_0121.json](../verification/CI_0121.json)。只读取公共元数据，没有读取 job 日志，不推断远端测试数量。[REPOSITORY_AUDIT_0121.json](../verification/REPOSITORY_AUDIT_0121.json) 核验提交前 main 可达22个提交/415个唯一blob与151个公开文件，194个有限规则命中均按精确历史blob或已声明unittest限定名称分类，未知发现和禁止路径为零；不扫描或推送私有运行数据与本地工具引用。

最终交付文档/CI 元数据补扫见 [REPOSITORY_AUDIT_0121_DELIVERY.json](../verification/REPOSITORY_AUDIT_0121_DELIVERY.json)；后续交付提交不改变运行源码，只推送 main。

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
