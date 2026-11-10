# Evo 自改进平台深度架构解析

> 代码基线：`39bc2a4b4290eadf1cd696bcddd5ac755ec1df86`，2026-11-10。深度：L4，Agent/Harness 专项。以下原有行号对应此审计基线；增量核对至 `a9feef4b96e54e6f6d77eeabdd01d98879a0df30`。
> 输入：本仓库代码、配置、测试、M1—M6 分阶段实测及本地执行状态。
> `[FACT]` 表示已核对的实现或证据；`[INFERENCE]` 是推断；`[TARGET]` 是后续设计；`[UNKNOWN]` 表示尚未证明。历史 M0 目标方案保留在 Git 历史，本文更新为当前实现审计。

## 1. 系统论点与非目标

[FACT] Evo 为 Agent 开发者运行有预算的改进实验。不可变 AgentVersion 是可执行资产，Experiment 固定实验条件，Trial 承担一次隔离执行。控制线程决定下一步，Harbor 启动运行器与验收容器，宿主网关派发模型请求并维护模拟业务账本。

[FACT] 最难约束是：生成一个新 Skill 不等于加载成功，加载成功不等于任务成功，任务成功也不等于可归因的版本提升。因此系统分开记录资产、实际输入、独立验收、配对覆盖和采用判定，并保留未知成本与失败版本。

[FACT] 当前定位为本机单用户、单活动实验、单活动 Trial。浏览器关闭不终止执行；服务进程丢失后只在可确认的 Trial 边界显式恢复。没有多机 Worker、模型权重训练、生产业务操作、多租户认证或任意运行器源码进化。当前也没有 Approval、Question 或暂停等待对象；测试业务不需要新增这些流程。

## 2. 证据账本

本表适用代码基线如上，实测环境为 macOS arm64 + Docker Desktop；CI 为 Ubuntu + Python 3.13。行号对应该提交。

| ID | 结论 | 标签 | 证据 | 置信度 | 缺口 |
| --- | --- | --- | --- | --- | --- |
| C-01 | 一个数据目录一个控制进程、一个活动线程 | [FACT] | `cli.py:20`，`web.py:71` | 高 | 不适用于绕过 CLI 的嵌入使用 |
| C-02 | 版本不可变且核对实际加载及模型输入 | [FACT] | `versions.py:34,63,74,189`；M1、M6 安装实测 | 高 | 输入存在不保证模型遵循 |
| C-03 | 任务包寻址、分组、交替配对和重复 | [FACT] | `taskpacks.py:34,46`，`evaluation.py:5` | 高 | 18 个场景规模有限 |
| C-04 | 无反馈组不传入执行证据，推荐项目隔离 | [FACT] | `optimizer.py:89`，`improvement.py:120,171`；`test_improvement.py` | 高 | 真实对照结果尚待研究完成 |
| C-05 | 调用意图与预算预占同事务，未知用量停止派发 | [FACT] | `experiment_budget.py:46,54,67` | 高 | 无供应商独立账单核对接口 |
| C-06 | Agent 离线、真值与验收在外、验收签名核对 | [FACT] | `harness.py:173,383,459`，`taskpacks.py:91`；`test_business.py` | 高 | 非恶意宿主隔离，非微 VM |
| C-07 | 已确认 Trial 可恢复，原截止时间保留 | [FACT] | `execution_control.py:134,187`；[M4 实测](m4-verification.json) | 高 | 不能恢复 Harness 进程内 Step |
| C-08 | 资源按 job 身份、启动时间和挂载源清理 | [FACT] | `trial_resources.py:24,44,55`；M4/M6 清理证据 | 高 | 无全局孤儿资源服务 |
| C-09 | 采用和回滚通过 CAS 与审计事务 | [FACT] | `improvement.py:29`，`delivery.py:135`；`test_delivery.py` | 高 | 仅改变本地推荐，不部署外部资产 |
| C-10 | 控制 API 绑定回环，写请求有同源检查 | [FACT] | `web.py:326` 与服务绑定；`models.py:26,45` | 高 | 没有用户认证或租户授权 |
| C-11 | DSH/Pi 无相邻项目依赖，可从 clone/wheel 使用 | [FACT] | [M6 安装实测](m6-installation-verification.json)、`pyproject.toml` | 高 | 旧 `/harness` 维护题仍需显式源码 |
| C-12 | 自动诊断、单文件生成、验证、判定均有实现 | [FACT] | `improvement.py:156`；M3 部分真实证据与受控测试 | 高 | 完整真实协议仍在验收 |
| C-13 | 当前吞吐主要受串行执行和外部模型等待限制 | [INFERENCE] | 单 Trial 执行路径与 M6 单题耗时 | 中 | 未做压力测试和延迟分位数统计 |
| C-14 | 多 Worker 需独立领取、租约和共享存储协议 | [TARGET] | 单目录锁、SQLite 与单线程的边界 | 中 | 不在 v0.2 范围 |
| C-15 | 开发反馈是否提升未见任务质量 | [UNKNOWN] | feedback-01/02 均因模型响应问题停止，对照与封存未启动 | 未定 | 不用部分结果替代完整对照 |
| C-16 | 断电恢复、磁盘损坏恢复的 RPO/RTO | [UNKNOWN] | 现有证据仅为进程级故障注入 | 未定 | 尚无备份恢复和电源故障演练 |

证据冲突已按版本区分：M1 的 DSH ATIF 来自旧相邻 adapter；新独立 adapter 保留原生日志及网关轨迹，不生成 ATIF。M3 旧失败记录保留原判定原因，不用新代码改写旧实验。M0 的“目标状态”不再当作实现证据。

## 3. 边界与所有权

| 组件 | 职责 | 所有状态 / 写入接口 | 只读依赖 | 扩容与故障域 |
| --- | --- | --- | --- | --- |
| 本地 Web / Manager | 校验并提交实验，启动线程、返回观察结果 | 请求映射、实验创建；`start_evolution` | 版本、配置 | 一个服务进程；HTTP 断线不影响线程 |
| Improvement / VersionExperiment | 顺序推进阶段与 Trial | 实验阶段、运行、候选引用 | 固定计划、配置快照 | 一个活动实验；进程故障中断 |
| VersionRegistry | 注册、派生和核对资产 | `evo_version`，只新增 | 绑定模型身份 | 同一 Store 锁 |
| ExperimentBudget | 预占、结算、停止派发 | `evo_call/evo_budget` | 实验 epoch、deadline | 单调用串行，Provider 独立故障域 |
| TrialGateway | 中继模型和业务工具、签名账本 | 本 Trial 业务内存状态与文件收据 | 设置、版本 | 随 Trial 结束，不能恢复内部进程态 |
| Harbor / adapter | 容器执行与结果收集 | Harbor job、容器、原生日志 | 只读版本、任务文件 | 每 Trial 新环境 |
| 独立验证器 | 核对签名真值和业务约束 | 结构化 checks、reward、签名回执 | 冻结账本 | 单独验收容器 |
| SQLite / 文件工件 | 保存元数据和持久证据 | Store 事务 / 原子文件替换 | 无 | 本机磁盘；不是跨地域存储 |

[FACT] 元数据以 SQLite 为事实源，容器是否实际存在以 Docker 为事实源；二者通过 `evo_resource` 对账，不能把数据库里的 cleaned 字符串等同于未经核对的资源消失。模拟业务真值由宿主 BusinessService 拥有，账本是其签名快照。页面是派生视图，不决定实验生命周期。

## 4. 核心实体与不变量

| 实体 | 标识与关系 | 写入者 | 不变量 / 终态 |
| --- | --- | --- | --- |
| AgentVersion | `av-` 内容摘要，parent，manifest hash | Registry | API 不覆盖资产；模型身份固定 |
| TaskPack | `tp-` 内容摘要，源码指纹与分组 | 创建实验 | 旧包不覆盖；执行前检测源码漂移 |
| Experiment / Intent | `ex-`，baseline/candidate/project，计划 | 控制器 | 请求和 intent 同事务；条件不可静默改变 |
| Trial / Checkpoint | `tr-` 与逻辑 row key | 执行线程 | 有效终态和检查点同事务；重复 row 不能推进 |
| Call / Budget | `call-`，experiment/phase/trial | 网关与优化器预算适配器 | 每次派发先占用；pending/unknown 阻止下次调用 |
| Diagnosis / Proposal / Output | experiment 或 call ID | 改进控制器 | 原始输出先保存再解析；引用必须可追溯 |
| Project / Decision / Change | current/initial/revision 与 decision ID | 采用事务、回滚事务 | initial 不变；revision 单调；旧判定保留 |
| Resource | Trial ID、epoch、job、workspace | 资源管理器 | 仅清理可归属资源；未清理不能续跑 |

[FACT] 不变量覆盖的是应用协议与正常数据库访问。不可变表有禁止 UPDATE 的触发器，但没有宣称能抵抗宿主管理员直接删除数据库。整库备份必须包含需要复查的工件。

## 5. 接口与协议契约

[FACT] `POST /api/evo/experiments` 需本地 Host、同源 Origin（若有）、自定义请求头和 1–128 字符幂等键。202 表示意图已持久化并提交执行，不表示任务完成；重复同键同 payload 返回已有实验，异 payload 冲突。另有版本注册、取消、继续、导入和推荐回滚接口。

[FACT] GET 观察 API 不拥有任务生命周期。前端轮询完整实验；没有 SSE、长连接恢复或网络背压队列。运行器与模型使用 Responses 协议；供应商流在宿主收齐并解析完成事件后转发，未实现任意 token 位置续传。

[FACT] 版本、实验、任务包、导出包各有 schema 标识。字段演进保留旧记录；没有通用跨版本转换框架，恢复要求代码协议哈希一致。API 错误体供本地用户理解，不承诺第三方 SDK 稳定版本兼容。

## 6. 正常关键链路

| 次序 | 动作 | 持久化或外部副作用 | 取消 / 证据 |
| --- | --- | --- | --- |
| 1 | 绑定基线与生成模型、固定分组和预算 | 请求、实验、intent、项目同事务 | 幂等键关联 |
| 2 | 领取 epoch，开始绝对 deadline | control/budget | 派发前核对 epoch |
| 3 | 解析镜像 ID，准备任务和 Trial 资源 | 资源意图先落库，Harbor 创建容器 | 精确资源归属 |
| 4 | 运行六道开发基线 | 模型调用意图与结算、业务账本 | 独立验收、加载和输入证据 |
| 5 | 诊断并生成一个文件 | 原始响应、诊断、假设、候选版本 | 引用和权限校验；失败保留 |
| 6 | 父子配对完成验证 | 每题结果与检查点 | 缺失或执行错误不折算为失败 |
| 7 | 评估并采用/拒绝 | Decision、推荐 CAS、事件、终态同事务 | 当前 epoch、revision、预算结算 |
| 8 | 导出和复用 | 有界 ZIP / 白名单报告 | 显式本地模型重绑定 |

[FACT] 第一持久点在任何模型请求之前；模型接受请求不是本地已确认结果。最终质量由覆盖、独立验收与逐题回归决定。无反馈组执行相同开发计划，仅生成输入改为公开契约，不以省去执行费用制造预算优势。

## 7. 状态机与持久化

[FACT] Experiment 的主要转换为 `queued → preparing → running → completed/incomplete`；取消通过 control 的 `cancelling` 阻止新派发，再落 `cancelled`。进程丢失产生 `interrupted`；满足恢复条件后 `interrupted → queued` 并领取新 epoch。业务失败 Trial 可以是有效 `failed`，基础设施错误为 `error`，两者不混用。

[FACT] Trial 只在 `passed/failed` 且结果提交时写不可变检查点。已完成结果不会靠页面缓存恢复。默认每题 240 秒，实验有绝对 deadline；环境准备与收尾另有有限等待，因此墙钟结束可能晚于派发截止点。Approval/Question/Pause 不在当前状态机中。

## 8. 并发、幂等与一致性

[FACT] CLI 的 `flock` 排他管理目录，Manager 锁限制活动线程，Store 的重入锁串行化连接，SQLite 事务提交证据。该组合是单机所有权协议，没有跨机器 lease 或心跳。

[FACT] 请求去重键为请求头摘要，在整个数据目录内生效，没有 TTL；不能把同一个键复用于不同项目的不同参数。控制动作另有命名空间。采用校验 current_version 和 revision；控制器校验 epoch 与冻结 intent。回滚先检查目标是已采用祖先，再事务递增 revision；晚到实验无法覆盖它。

[FACT] SQLite 事务解决本地状态一致性，不能撤销已派发的外部模型或已接纳的业务动作。取消以进入派发检查的顺序界定已在途操作，不承诺瞬间撤回。没有数据库和 Provider 的分布式事务，也不声称端到端 exactly-once。

## 9. 副作用与 Effect Receipt

| 副作用 | 提交前意图 | 确认与 Receipt | 未知结果策略 |
| --- | --- | --- | --- |
| 付费模型请求 | call ID、请求哈希、预算预占 | completed response 的实际 usage，SQLite 幂等结算 | 保留 unknown；不自动重试；无外部幂等保证 |
| 模拟业务写入 | 网关检查当前执行权、动作契约 | 宿主状态与签名 ledger，工具查询实际状态 | Agent 任务内按契约查询；控制进程崩溃则不重放 Trial |
| Harbor/Docker 创建 | 唯一 job、workspace、epoch 资源意图 | 真实进程身份与精确 mount 匹配 | 对账清理；未完成清理不得继续 |
| 候选与优化响应 | 调用结果、授权目标、父版本 | 不可变 output 和内容哈希 | 响应已落盘则复用，未落盘且请求已计费则阻止重发 |
| 采用与回滚 | 预期版本/revision/epoch | Decision/Change、指针与事件事务 | 同键返回原记录，陈旧状态冲突 |
| 导出 / 导入 | 校验完整资产、显式连接绑定 | 元数据与文件哈希、导入来源记录 | 不继承远端评分、父子或推荐资格 |

[FACT] 签名账本证明验收读取的是宿主冻结真值，不证明模型费用已到账。版本哈希证明一致性，不是作者签名。版本文件按原文导出，用户自行写入文件的敏感内容不会被自动猜测和删除。

## 10. 故障恢复矩阵

| 故障注入点 | 现象 | 已持久化 | 副作用状态 | 检测 / 恢复负责人 | 去重/围栏 | 用户语义 | 验证测试 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 创建事务失败 | 请求失败 | 无部分实验 | 未执行 | API / SQLite | 事务回滚 | 可原键重试 | `test_failed_create_rolls_back_project_pack_and_experiment` |
| 落库后、领取前退出 | queued 遗留 | 完整意图 | 未派发 | 启动扫描 / control | 新 epoch | 显式继续 | recovery 单测 |
| Provider 接受但回包丢失 | pending/unknown | 调用意图 | 可能计费 | 网关 / 恢复扫描 | 不重发 | 证据不足 | `test_pending_model_call_becomes_unknown_and_cannot_be_replayed` |
| 已知费用、原始优化输出未保存 | 缺少 output | 用量已结算 | 已计费 | resume_reasons | 禁止再生成 | 不能继续 | `test_known_charge_without_raw_output_is_not_reissued` |
| 原始输出保存、实验引用未保存 | 诊断阶段中断 | 原始响应 | 已确认 | 改进控制器 | 输入哈希复用 | 不再调用模型 | `test_captured_optimizer_output_survives_crash_before_experiment_reference` |
| 首题检查点后 SIGKILL | interrupted | Trial 与 checkpoint | 已确认 | 目录重新取得后恢复 | 跳过已确认 row | 保留截止时间继续 | [M4 真实 6/6](m4-verification.json) |
| Trial 中间崩溃 | 未确认 Trial | 部分事件/账本 | 未知 | 恢复扫描 / 资源对账 | 不重放 row | 停止、保留工件 | `test_unconfirmed_trial_and_changed_runtime_refuse_resume` |
| 资源清理失败 | unresolved | 资源身份 | 可能仍运行 | resource reconciler | 只匹配本 Trial | 不能继续 | 精确挂载与旁观容器演练 |
| 判定插入失败 | 决定未提交 | 原评测 | 无采用 | SQLite / finalize | 单事务 | 推荐保留 | 事务失败注入测试 |
| 旧 Worker 迟到 / 回滚后完成 | epoch 或 revision 冲突 | 新控制权/推荐 | 已执行旧工作 | finalize | epoch + CAS | 不应用旧晋升 | recovery、delivery 测试 |
| 取消与终态竞争 | cancelling | 取消状态 | 在途结算 | 执行线程 | 终态提交前再检查 | 取消胜出 | `test_cancellation_between_last_result_and_terminal_commit_wins` |

[FACT] 进程崩溃演练证明已提交检查点保留，不是断电零丢失证明。[UNKNOWN] 未给出生产 RPO/RTO 或磁盘损坏恢复 SLA。人工兜底是导出、保留现场、核对未知用量与资源，不是删除失败记录后重跑冒充原实验。

## 11. 安全与隔离

[FACT] HTTP 只监听回环，POST 核对 Host/Origin/自定义头；这些是本地网页防护，不是多用户认证。模型配置文件使用 0600 权限，公开 API 不返回密钥值；版本含模型身份与端点快照，本地可见，不应把整个数据库当作脱敏导出。

[FACT] Agent 用 node 用户、离线网络、只读版本挂载及中继 spool；不挂载 Docker socket、验收脚本或业务真值。验收使用独立容器和只读账本。网关限制模型和工具请求类型、大小，拒绝重定向和冻结后的业务写入；spool 读取拒绝 symlink/特殊文件。

[FACT] 当前仍是 Docker 共享内核隔离，spool 对 Agent 可写，业务服务由宿主进程处理。没有微 VM、跨租户配额或全局磁盘字节限制，不应将其作为公开不可信代码托管服务。出入口是受控中继，但使用 HTTP 还是 HTTPS 由配置的 Provider 地址决定，系统不能把 HTTP 配置宣称为加密传输。

[FACT] 生成器只改授权现有文件；其输出作为数据校验，不执行任意代码。输入证据引用存在只证明来源可追溯，不证明诊断推理正确。公开仓库意味着任务源可被人查看，分组隔离约束的是平台生成数据流，不是保密机制。

## 12. 性能、容量与成本模型

[FACT] 实验没有排队服务：已有活动实验时返回冲突。每 Trial 模型请求上限 12、报告 Token 派发阈值 180,000、Agent 时限 240 秒；自动改进默认最多 363 次请求、3,000,000 Token、14,400 秒。Token 在响应后结算，最后一次调用可能跨过阈值；未知值不记零。当前不估算货币价格。

[INFERENCE] 对顺序实验，`T ≈ Σ(环境准备 + 模型等待 + 工具执行 + 验收 + 清理) + 优化器时间`。当平均 Trial 服务时间为 W 时，当前吞吐上界约为 `1/W`，而非请求线程数量。Little 定律 `L ≈ λW` 在稳定队列假设下可作容量检查，但本服务拒绝并发实验，因此不能据此虚构线上负载。

[FACT] M6 安装检查四题的执行耗时约 26–38 秒，共 24 次请求、202,389 已知 Token。这是特定模型、单一加载题的样本，不能外推业务全题 P95。[UNKNOWN] 尚无到达率、P95/P99、磁盘增长与长时间压力数据。

[TARGET] 扩容先测每阶段耗时、网关等待和工件增长，再拆共享预算与 Worker 领取；不能直接加线程而保留当前 pending 互斥语义。降级方式为拒绝新实验、限制预算或停止未知执行，不降低验收门槛。

## 13. 可观测性与运维

[FACT] 关联链为 project → experiment → Trial → model call / resource。阶段、checks、输入加载、预算、Decision、原始优化输出与工件可查。没有分布式 Trace 后端、自动告警、Provider 对账或自动日志保留期。

| 问题 | 可查证据 | 操作入口 |
| --- | --- | --- |
| 实验是否卡住 | phase、最后 Trial、pending、绝对 deadline | 实验页、只读 API |
| 是否重复付费 | 幂等请求映射、call ID、phase、input hash | 预算总账与原始输出 |
| 是否误用旧版本 | manifest hash、文件 hash、runtime、input evidence | 单题证据 |
| 是否资源泄漏 | 终态与 `evo_resource.state` 差异 | 重启或恢复前资源对账 |
| 为什么不能继续 | resume_reasons | 页面显示具体阻碍，不猜测重试 |
| 为什么未采用 | decision.reason、逐题覆盖、project revision | 判定、推荐变更历史 |

[FACT] 同一个控制面内部的 trace 是事件文本，不等同于完整分布式 tracing。日常 Runbook 见 [恢复协议](execution-recovery.md) 与 [安装说明](installation.md)。备份或升级前应停新实验、确认活动任务和资源状态，再保留数据库及工件；目前没有内置备份命令。

## 14. 部署、迁移、兼容与回滚

[FACT] `uv.lock`、Harbor 0.22.0、镜像内 DSH/Pi 版本锁定。镜像标签构建可能改变，实验准备时解析为内容 ID 并创建快照标签，两版共用。CLI 自动发现 Harbor，不再从可执行文件位置猜相邻源码根目录。

[FACT] Store 启动创建缺失表/触发器，旧 JSON 对象原样保留；没有 schema migration 管理器或滚动升级。运行代码哈希变化后禁止恢复旧实验，避免新代码混入旧协议；结束后新建实验。推荐回滚只移动资产指针，不回滚数据库结构或进程代码。

[TARGET] 引入数据库迁移前采用 expand/migrate/contract 并先做恢复演练。SQLite WAL 数据应通过一致性备份或停机复制取得，不能只复制一个活动中的主数据库文件后声称备份完整。

## 15. 方案取舍与替代设计

| 决策 | 约束与选择 | 替代方案 | 获得 / 代价 | 失效条件与迁移/回滚 |
| --- | --- | --- | --- | --- |
| 单机所有权 | 目录锁 + 线程 + epoch | 分布式队列/租约 | 易复现；无多机吞吐 | 多用户并发时需共享预算、领取和对象存储 |
| Harbor 执行 | 复用容器与结果协议 | 自建沙盒编排 | 少写生命周期；依赖较重 | 更换底座保留 Trial/Receipt 契约，重新验收 |
| SQLite + 文件 | 本地事务与可检查工件 | PostgreSQL + 对象存储 | 安装简单；磁盘为单点 | 多机前迁移事实源和备份方式 |
| 单文件候选 | 固定执行模型与运行器 | 多因素任意代码改写 | 可归因、权限窄；搜索范围有限 | 新维度需要新版本资产和协议 |
| 遇未知停止 | 保守保留成本与副作用 | 自动重试 | 不制造伪完整结果；可用性下降 | Provider 有可查询请求凭证时再扩展对账 |
| 严格质量门槛 | 完整覆盖、逐题不退化、总分严格增加 | 成本/质量多目标 Pareto | 判定清晰；全通过时无晋升空间 | 后续必须预先定义新目标，不能看结果改门槛 |

以上选择是当前约束下的取舍，不是宣称替代方案普遍更差。

## 16. 测试与验证计划

[FACT] 当前 91 项自动化测试中 90 通过，1 项旧版可选 Docker 集成跳过；[提交 CI](https://github.com/LianWeiSQ/harness-arena/actions/runs/42801717521) 已通过。覆盖内容比数量更重要：

| 层级 | 关键断言 | 证据 |
| --- | --- | --- |
| 事务和幂等 | 创建整体回滚、重复请求不再付费、陈旧指针拒绝 | improvement/web/delivery 测试 |
| 输入与资产 | 越界文件拒绝、非授权模型固定、挂载与真实输入一致 | evolution 测试、M1/M6 实测 |
| 验收 | 正确/空/错误解、签名篡改、遗漏与回归 | business 校准与测试 |
| 恢复 | pending 转未知、输出复用、首题不重跑、取消竞争 | recovery 测试、M4 SIGKILL 演练 |
| 对照 | 无执行证据、相同计划/预算、独立项目、同版本不声称提升 | improvement/business/study 测试 |
| 交付 | ZIP 校验、绑定、报告白名单、导入事务 | delivery 测试、M5 浏览器 |
| 安装 | 新目录与 wheel，无私有相邻依赖 | M6 实测和 CI |

[FACT] 受控测试中的晋升是机制验证，不是实际模型收益。feedback-01 因响应异常及未知用量停止，验证有效覆盖 2/24，对照和封存未启动；[失败报告](studies/2026-11-10-feedback-01-result.json) 保留。feedback-02 也在 22/24 后因连接/读取异常停止，见 [停止报告](studies/2026-11-10-feedback-02-result.json)；新的完整研究仍待完成，不能用通过的 CI 补上缺失质量证据。

[FACT] 605e5c1 增量修正把模型终止状态与费用确认分开：`response_protocol.py:44` 解码 SSE/JSON，`harness.py:138` 先结算用量再拒绝失败/未完成终止，`experiment_budget.py:87` 持久保存白名单响应摘要，`delivery.py:225` 导出再次过滤。已知用量的失败仍停止 Trial，未知用量仍不重放。旧实验缺少摘要，根因不能事后推定；参见 [处理与验证](provider-response-handling.md)。

[FACT] a9feef4 增量改为按 SSE 事件读取，在终止事件结束后解析和结算，不继续等待 HTTP EOF（`response_protocol.py` 的 `read_response`、`harness.py` 的 `TrialGateway.model`）。读取异常仍使 Trial 无效，部分响应中可核对的用量独立记账。截止时间在读取之间检查，仍可能等待一次 socket 空闲超时，不保证硬实时取消。97 项测试中 96 通过、1 项旧版可选测试跳过，CI 通过。

[FACT] 有限真实检查中，SSE/JSON 随机文本回显均通过，Pi 消费了三次以终止事件结束的返回；第四次请求收到上游 HTTP 502，资源清理后停止。完整兼容检查和完整研究尚未通过。见 [收据](transport-compatibility-verification.json)，它不能证明旧连接失败的根因或长期稳定性。

## 17. 未知项与最小验证

| 未知项 | 影响 | 最小验证 | 负责范围 |
| --- | --- | --- | --- |
| [UNKNOWN] 完整反馈组与无反馈组的结果 | 不能声称优化贡献 | 完成预登记两组、独立封存、报告所有成本与失败 | M3/M6 |
| [UNKNOWN] 故障场景更换后的泛化 | 不能当行业基准 | 新领域、新 TaskPack、更大重复量 | 后续评测路线 |
| [UNKNOWN] 断电、磁盘损坏与备份恢复 | 无 RPO/RTO 承诺 | 一致性备份与恢复演练、资源重新对账 | 后续可靠性 |
| [UNKNOWN] 多用户负载和租户隔离 | 不能公开托管不可信任务 | 新认证/租约设计和压力/攻击测试 | 不在 v0.2 范围 |

## 18. 面试三级追问题库

### Q1 一级追问：为什么要分开版本、输入和验收？

**主回答：** 保存 Skill 只证明文件存在。版本核对确保执行了指定资产，输入证据确保运行器送入模型，独立验收再判断业务结果。

**二级追问：** 具体保存什么？ **二级回答：** manifest、逐文件 hash、runner 版本、模型身份、加载收据、Skill 注册与正文读取、签名 checks，均关联 Trial。C-02/C-06。

**三级追问：** 模型读到 Skill 就有效了吗？ **三级回答：** 不能保证遵循；依然可能失败。对照组和配对结果评估效果，缺失覆盖时保持证据不足，不靠日志措辞晋升。

### Q2 为什么恢复不是从头再跑？

**主回答：** 外部调用可能已计费，业务写入可能已成功，重跑会制造额外副作用。

**二级追问：** 怎么判断能继续？ **二级回答：** 核对 intent、protocol hash、epoch、原 deadline、已确认 Trial 的 checkpoint、原始优化输出和 cleaned 资源，全部通过才重新入队。C-05/C-07。

**三级追问：** 保存费用后、保存输出前崩溃呢？ **三级回答：** 已知费用不足以重建输出，系统拒绝再次生成。已有测试覆盖该窗口；M4 实测只证明安全 Trial 边界恢复，不声称每个中间 Step 都能恢复。

### Q3 旧实验为什么不能覆盖新推荐？

**主回答：** 采用是有条件的本地事务，不是写一个 latest 文件。

**二级追问：** 具体条件？ **二级回答：** 当前 epoch、意图、完整验证、已结算预算、父子关系、project.current_version 与 revision；事务同时保存 Decision、指针和事件。C-09。

**三级追问：** 多机 Worker 呢？ **三级回答：** 当前目录锁不支持这个场景。需要共享数据库领取与租约、外部资源归属及预算并发协议，不能直接把 epoch 当分布式锁。

### Q4 怎样证明反馈有贡献？

**主回答：** 同初始资产、模型、候选数和评测预算，比较读开发反馈与不读反馈的生成方式，再报告封存结果。

**二级追问：** 无反馈组少运行开发集会省钱吗？ **二级回答：** 本协议仍运行同样六题并计费，但生成器只收到公开契约和原始资产。实际 Token 分开统计，不造空调用凑齐。

**三级追问：** 两组都不晋升呢？ **三级回答：** 保留初始版本，封存评测注明同一版本；结论只能是本次未证明反馈改进，不能靠换门槛、隐藏失败候选或重复挑选最好结果制造提升。

## 19. 交付收据

- 深度 L4；证据账本核心结论 16 项：FACT 12、INFERENCE 1、TARGET 1、UNKNOWN 2；其他局部未知项已明示。
- 代码与实测分别标注；新对照流程 86 项测试/CI 已通过，桌面配置和 390px 无横向溢出。
- 故障注入覆盖进程 SIGKILL、事务失败、未知用量、取消及旧执行者写入；不扩张为生产 SLA。
- 本次为现状文稿更新，无新增架构图；L4 严格结构校验通过（0 错误、0 警告），不替代实现和实验核对。
- 尚未证明：完整真实反馈研究与封存报告，以及可推广的质量收益。M3/M6 保持未完成。
