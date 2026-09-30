# 交付状态：0.1.0

截至用户时区日期 2026-09-30，本体已在 `D:\Indeces` 实现。GitHub 私有仓库为 [Indeces_memory_manager](https://github.com/simulacrum0112-afk/Indeces_memory_manager)。源码提交/推送结果由交付消息及 Git 历史确认；创建仓库本身不代表服务已启动。

已实现：无头/Console 入口、单实例存储锁、一个 Discord Gateway/有界消息队列、显式 @ 门禁、实际送达回执、短期 100%/70% 整轮水位摘要、GPT-6-Luna stateless adaptor、独立阶段预算、计量门禁、usage 核验、超时/阶段熔断、scratch hash 链与持久原文/checkpoint。

知识路径已按用户最新要求实现：聊天标词和聊天自动入库关闭；本地 Markdown/UTF-8 文本更新触发被动后台标词。不可变知识版本、当前文件 digest 二次检查、全部块完成后原子发布、旧版本归档、重启未知用量不重试、文件超限暂停索引均有测试。标签只附于原文，不由模型改写知识内容。

图策略来自固定 MIT 上游，并按批准启用动态排序；NPMI、η=1、λ=.99、单跳来源门控、静态/动态分表、检索事件幂等与自身称呼排除已验证。改动粒度、周期、排名与日记过滤的适配差异在 `BASELINE.md` 如实列出。

## 验证证据

- CPython 3.12.14 / Windows，`python -m unittest discover -s tests -q`：**89 tests / 0 failures / 0 skips**，最近完整运行 4.291 秒。
- 分类：adaptor 13；context/runtime 14；Discord bridge 16；被动知识版本 23；Hebbian memory 16；scratch 5；单实例锁 2。
- `python -m indeces check`：配置解析和安装依赖导入通过，discord.py 2.7.1。
- `requirements.lock` 固定当前运行依赖；Windows/Linux 离线 CI 已配置，远端运行状态需单独核对。
- `python -m pip check` 无依赖冲突；可复现验证摘要保存在 [verification/OFFLINE_010.json](../verification/OFFLINE_010.json)。
- 测试使用合成消息、注入模型传输和临时文件/SQLite；覆盖预算超限不发送生成、取消/熔断、摘要失败保留覆盖边界、无聊天标词、文件在 await/索引期间替换、原子 ready 失败回滚、旧崩溃恢复、来源保留、日志损坏检测。

本轮发现并修复：供应商输出类型未受控、排队调用绕过已打开熔断、非法标签漏记已知 usage、文件未扫描时旧标注发布、全局文件超限留下陈旧索引、ready 状态与索引提交的崩溃窗口、旧中断 active 索引恢复，以及 Windows 第二实例先读锁定字节导致 PermissionError/句柄未关闭。上游静态/在线匹配不一致、弱边先去重、时间戳/重复事件与来源错误吞没均在适配中修复或以显式事件替代。

最后一次验证发现计时测试错误假定所有调用 elapsed>0：本机 `monotonic` 为 GetTickCount64，分辨率 0.015625 秒，即时 fake 调用可合法记为 0。已修正测试为允许非负测量，没有人为加 epsilon 或提高虚假精度；clock 信息一并存入验证摘要。

## 未完成与边界

- 未连接真实 Discord、未发送真实消息，未调用真实 OpenAI 模型。需要用户本地填写 guild、凭据后进行一个专用频道的代表性往返与知识更新试验。
- 未验证账号的模型访问、生产延迟、真实 token 分布、文件集标签质量、知识召回相关性或动态权重长期表现；当前预算是工程初值。
- 当前每个有直接命中的检索为一个 `.99` 衰减周期，不等同每日周期。该选择明列于基线表，使用者应在真实 pilot 中评估检索频率影响。
- 本地图排序、文件 I/O 和 fsync 不可由 asyncio 强制抢占；当前协作式本地时间检查不能当成任意规模计算的硬时间保证。
- 未收到允许后台标词与回复真实模型请求重叠的选择；当前两任务独立而 HTTP 串行。
- 尚无长时间运行/网络断开/真实 Discord rate-limit 验收。Discord 超时可能已经产生外部消息，不能承诺 exactly-once；无主动发送重试。
- scratch hash 链只检测本地序列一致性，不是外部不可篡改公证；原文和日志持续保留，尚无自动保留期限或压缩策略。

没有修改/启动现有 Yuki，没有科研参数变更、计算任务提交或旧结果迁移。不存在依据离线测试宣称“可发表”或“真实检索效果已验证”的结论。
