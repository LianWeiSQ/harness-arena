# EvoArena

EvoArena 让工具型 Agent 在可验证任务中竞争，并根据执行反馈产生候选策略。比赛、策略差异、父子配对、晋级或拒绝都有持久记录。

首个赛场 FixOps 的任务是恢复订单服务：真实 Docker 容器中运行网关和订单 API，Agent 通过受限工具检查配置、修改上游并探测健康；宿主机上的独立验证器检查健康、创建订单、查询一致性和依赖约束。

## 当前版本

首版已包含不可变策略版本、2 或 4 个席位、多轮比赛、真实模型生成 Child、配对准入、原子席位替换、事件回放、策略差异和本地观战页面。默认两个策略、两轮和一个候选，所有任务顺序运行。

页面可提交自己的策略，再从“自选策略”中选择 2 或 4 个不可变版本参赛。注册策略不发起模型请求；只有启动赛季才执行真实比赛。当前参赛单位是提示词和操作流程，外部自主运行器接入列入后续工作。

当前只有一个故障族，探索、准入和报告集采用不同服务端口变体。每个版本每轮只有一个任务实例，页面标为 SMOKE；结果用于验证闭环，不能据此声称泛化或统计显著提升。首版通过固定动作协议演化提示词与操作流程，暂不训练模型权重。

## 安装和模型配置

需要 macOS 或 Linux、Python 3.12+、uv 和运行中的 Docker。首次构建默认拉取 `python:3.13-slim`。

```sh
uv sync --python 3.13
```

通过进程环境提供模型信息。密钥仅保存在环境或你自己的凭据管理器中；应用不自动读取 `.env`。

```sh
export ARENA_BASE_URL='https://your-provider.example/v1'
export ARENA_MODEL='your-model'
export ARENA_API_KEY='your-key'
uv run evo-arena doctor --probe-model
uv run evo-arena serve
```

打开 <http://127.0.0.1:8787>。支持提供 `/responses` 接口的模型服务；模型通过 JSON 动作协议使用工具，不依赖供应商原生工具调用格式。`doctor --probe-model` 会产生一次真实模型请求；不带该参数只检查配置和 Docker。

可以显式导入已有 Harbor 配置和 DSH 凭据引用，无需复制密钥：

```sh
uv run evo-arena serve \
  --import-config ../harbor-eval/configs/runtime.yaml \
  --credentials ~/.dsh/.credentials.yaml
```

导入文件中的 `model.base_url`、`model.name` 和 `model.api_key_env` 会用来读取配置，凭据文件只读取对应的 `refs` 项。`ARENA_*` 环境变量优先。Harbor 和 DSH 都不是项目独立运行的必要依赖。

常用的文件引用和基础镜像可以保存在本机 JSON profile 中，字段只接受 `import_config`、`credentials` 和 `base_image`，不保存密钥值。相对路径以 profile 所在目录解析。例如已配置本机 profile 时：

```sh
uv run evo-arena serve --profile .evoarena/local-profile.json
```

## 运行一个真实赛季

先校准环境，再运行比赛：

```sh
uv run evo-arena calibrate
uv run evo-arena demo --population 2 --rounds 2 --max-calls 32 --max-tokens 220000
uv run evo-arena serve
```

网页上的开始赛季也会运行真实模型。轮次和种群变化时会调整页面展示的请求上限；CLI 增大种群时请显式设置预算。模型请求数是硬上限；token 按供应商报告累计，在新请求发起前检查，正在执行的一次请求可能使累计超过阈值。遇到用量未知的请求停止继续调用，不能把未知计为零。暂不估算货币费用。

本地镜像拉取受限时，可以通过 `--base-image <可运行 python3 的本地镜像>` 构建同一个场景。环境收据会记录实际基础镜像、镜像 ID、架构和场景源码摘要。2026 年 11 月 3 日的首次实验使用已缓存的 `alexgshaw/openssl-selfsigned-cert:20251031`，架构为 amd64；只采用任务通过数决定晋级，不以该环境耗时声称性能提升。

```sh
uv run evo-arena calibrate --base-image alexgshaw/openssl-selfsigned-cert:20251031
```

命令均可接受 `--data-dir`，默认持久目录为 `.evoarena/`，已排除在 Git 之外。导出一个赛季：

```sh
uv run evo-arena export --season <season-id> --output .evoarena/reports/season.json
```

也可以在命令行注册策略并选择参赛版本。`--version` 重复两次或四次，版本 ID 来自注册输出：

```sh
uv run evo-arena register --name '我的策略' --policy-file ./my-policy.txt
uv run evo-arena demo --version <version-a> --version <version-b>
```

## 比赛和进化规则

每个任务获得新环境，运行记忆在任务间清空；策略通过不可变版本显式继承。同一赛季固定模型、三次动作预算和工具权限。动作包括检查、修改配置、健康探测和提交；最终通过与否由独立业务验证器决定。

前两名轮换成为 Parent。进化器只读取探索任务反馈，生成新策略、修改理由和差异。Parent 和 Child 在同一准入端口变体上重跑，只有 Child 严格增加任务通过数、且没有配对实例退化时才晋级。平分会被拒绝；环境或供应商异常会使判定待评。Parent 和被替换的末位席位可以不同。

最后一轮完成后才打开最终报告变体，比较初始版本与最终仍在席的版本。该结果不回流当前赛季选择。被拒绝的候选仍保留策略、轨迹和准入理由。

## 系统边界

```text
本地浏览器 → HTTP 控制面 → SeasonRunner
                            ├─ SQLite 版本、席位、决定、事件
                            ├─ Responses 模型与候选生成
                            └─ 受限动作工具 → Docker 网关和订单服务
                                             ↑ 宿主机独立业务验收
```

Agent 的模型循环在宿主控制面运行，只能调用四个受限工具。API Key 不进入容器、事件或页面。容器只读根文件系统、移除 capabilities、限定 CPU/内存/PID，配置目录只读挂载；模型可以通过受限配置工具请求修改唯一的业务字段。宿主机运行 Docker，不向参赛 Agent 暴露其管理接口。

SQLite 保存策略版本、赛季、运行、候选、准入和游标事件。晋级在同一事务中校验席位版本并落盘决定与事件；重复决定不会再次替换，过期席位写入会被拒绝。单个数据目录通过文件锁限制只有一个执行控制面。

进程重启后，未完成运行标为 interrupted，清理其拥有的任务容器，历史记录仍可浏览。当前版本需要启动新赛季重试，不会自动重放不确定的动作。它是单机执行平台，尚不提供分布式 Worker 租约、运行中自动接管或多租户服务。

## 验证

```sh
uv run python -m unittest discover -s tests -v
uv run evo-arena calibrate
EVOARENA_DOCKER_TESTS=1 uv run python -m unittest discover -s tests -v
node --check evo_arena/static/app.js
```

测试覆盖不可变快照、晋级事务和重试、过期写入、父子配对覆盖、退化拒绝、预算、供应商错误与重定向、完整进化链，以及可选的真实 Docker 业务验证。测试中使用的可控 Agent 只存在于临时数据库，不会作为真实比赛记录进入页面。

[首场真实实验](docs/first-experiment.md) 保留实际结果及其解释；[面试演示说明](docs/interview-demo.md) 组织讲解和技术边界；[长任务清单](TASKS.md) 记录交付范围和后续工作。
