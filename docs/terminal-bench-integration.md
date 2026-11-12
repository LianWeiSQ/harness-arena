# Terminal-Bench 2.1 接入

日期：2026-11-12。

当前 `/evo` 是独立评测工作台，不要求先创建父版本、候选或改进项目。

## 来源与完整性

- 官方包：`terminal-bench/terminal-bench-2-1`。
- 官方源码：<https://github.com/harbor-framework/terminal-bench-2-1>。
- 固定提交：`12b7565679ce5006230b820666353673a133db93`。
- 89 个任务，使用仓库 `tasks/` 下的完整题目、环境与验收脚本。
- 接入时使用 Harbor 0.22.0 的 `Task` 解析每道题，记录独立内容校验和；运行前再核验文件未变。
- 官方来源通过 Git 固定提交下载，不从旧版 `terminal-bench@2.0` 注册表替代，不改任务名称冒充 2.1。

页面「接入官方任务集」负责下载与格式检查，不运行模型。旧自建业务题和维护题已退出主入口，旧启动 API 返回 410；历史数据库和原实现保留为归档。

## 运行

选择任务集与任务，再添加 Harness × 模型组合；重复次数和单题预算对所有组合一致。当前顺序执行，保留官方时限，不重试失败的 Trial。真实评测只由「开始评测」触发。

`BenchmarkPi` / `BenchmarkDSH` 在任务原始环境中安装固定版本 Node 和运行器，不再要求任务基于旧业务镜像。每道题拥有独立中继目录、调用预算、Harbor Job 与结果路径。真正的模型密钥留在宿主，Harbor 配置与容器中使用本地代理占位配置。

安装阶段单独允许 30 分钟，不改变任务本身的执行时限。当前沿用受控适配器：Pi 不读取环境中的 Skill、扩展和上下文文件；DSH 使用 headless / PTC，关闭递归子 Agent 与工作流工具。结果代表这套明确配置，不等同于所有运行器默认能力或官方排行榜配置。

Harbor 的 `verifier_result.rewards` 是评分来源。二值 reward 计入通过/未通过；异常、缺少 reward、未执行不计成答错。自建任务的连续或多维奖励原样显示。完整覆盖前不公布该组合的完整通过率。

连接失败、未知用量或预算耗尽会停止后续派发。停止请求等待当前已发出的调用结算，再终止 Harbor。服务重启只标记中断，不自动重复任务或付费请求。当前不提供断点续跑；强制杀死宿主进程后可能需要手动检查遗留 Harbor 容器。

## 自建入口

「任务集 → 导入自建评测集」接受服务器本机路径，可指向单题目录或包含多题的目录。每道题须通过 Harbor 格式检查。导入复制内容快照，拒绝符号链接和特殊文件；修改源目录后再次导入得到新的内容版本。不会自动重新引入原来的 18 道自建业务题。

## 不调用模型的验收

- 真实官方仓库下载、固定提交核对、89 个任务解析。
- 89 × 2 份 DSH/Pi 配置由 Harbor `JobConfig` 校验，原始时限和验收保持启用。
- Harbor `install_only` 在官方 `fix-git` 环境中检查两种运行器安装；不会运行 Agent、解题或运行验收器。
- 单元测试覆盖内容快照、防篡改、非法任务、组合去重、重复提交、互斥、结果分类、预算异常停止、重启不重放和中继命令契约。
- 隔离浏览器 fixture 覆盖选择任务、提交、两组合结果、证据、刷新和手机布局。fixture 数据只存在于临时数据库，不能计入真实评测成绩。
- 真实工作区只读浏览、任务选择和环境检查；没有提交评测。

最终安装检查：Pi `0.82.1` 与 DSH `0.1.2-rc.1` 均成功，`agent_execution` 和 `verifier_result` 均为空。DSH 复查复用了公开 npm 下载缓存；早先暴露的 HTTP/2 下载中断和安装时限不足已修复。125 项单元测试中 124 项通过，1 项可选 Docker 测试跳过。[机器可读记录](terminal-bench-verification.json)。

首次运行仍可能需要拉取不同任务的镜像和依赖；完整 89 题涉及不同内存、GPU、网络要求。格式和安装检查不能替代真实模型执行，也不代表全套任务已完成或获得官方榜单成绩。

```sh
uv run --frozen --extra harbor python -m unittest discover -s tests
node --check evo_arena/static/benchmarks.js

# 交互测试：独立临时数据库，未连接真实模型
uv run --frozen --extra harbor python tests/manual_benchmark_server.py
EVO_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs node tests/manual_benchmark_browser.mjs
```

## 下一步

当前完成评测入口。基于通用任务结果的诊断、候选资产、隔离开发/验收集合及连续进化尚未接入，不能把原业务题的单文件改进实验当作 Terminal-Bench Evo 成果。
