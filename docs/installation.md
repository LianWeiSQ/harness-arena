# 独立安装与依赖边界

Evo 的业务任务、DSH/Pi 适配器、页面和验收器随仓库及 Python wheel 分发。新工作台不读取相邻项目；旧 `/harness` 的桌面进程清理题仍需要用户显式提供原任务源码仓库。

## 从新目录启动

要求 macOS 或 Linux、Python 3.12+、uv、Node.js 22+、Docker。推荐 Python 3.13。使用新数据目录可避免旧模型配置和历史影响首次体验。

```sh
git clone https://github.com/LianWeiSQ/harness-arena.git
cd harness-arena
uv sync --frozen --extra harbor --python 3.13
docker build -t evoarena-harbor:arena -f evo_arena/fixtures/harness/Dockerfile evo_arena/fixtures/harness
uv run --frozen --extra harbor evo-arena calibrate-business
uv run --frozen --extra harbor evo-arena serve --data-dir .evoarena
```

打开 `http://127.0.0.1:8787/evo`。校准应显示 18 个正确解通过、18 个空操作和 18 个错误解拒绝，模型调用为零。设置页检查 Docker、Harbor、任务镜像和 Node；然后配置自己的模型连接。兼容的模型服务必须支持 Responses API、工具调用和用量报告，连接时填写基础地址而非单次请求路径。

没有模型也可以浏览任务、版本入口和明确标注的真实案例回放。案例不会成为本地实验，也不消耗预算。启动真实实验前必须配置连接、创建基线，并检查本次总预算。

## 哪些依赖在什么位置

| 依赖 | 安装位置与用途 |
| --- | --- |
| Python / PyYAML | 本机控制面；最小安装可浏览页面 |
| `harbor==0.22.0` | 可选 `harbor` extra；本机编排容器和收集结果 |
| Node.js 22+ | 本机校准验收器；业务脚本的容器 Node 已在镜像内 |
| DSH `0.1.2-rc.1` / Pi `0.82.1` | 仅安装在任务镜像中；无需安装到个人终端 |
| Docker | 创建离线 Agent 和独立验收容器 |
| 模型连接 | 设置页本机保存或显式环境变量；密钥由宿主机中继使用 |

`uv.lock` 锁定包含 Harbor 的完整依赖；`uv sync --frozen` 不安装 extra，因此依赖较少。运行完整实验请保留 `--extra harbor`。CLI 先找当前 Python 环境的 `harbor`，再找 PATH；`--harbor-bin /absolute/path/to/harbor` 可以覆盖。升级 Harbor 或运行器版本必须重新验证 adapter 和协议，不能直接沿用旧实验继续执行。

DSH 适配器直接实现 Harbor 的 `BaseInstalledAgent` 契约，只生成本场中继配置，不引用私人 provider 默认值。原生 DSH session 日志保留在 Trial 工件中，模型和业务工具轨迹、用量由宿主网关记录。独立适配器不提供旧相邻项目的 ATIF 转换；页面不会把缺失 ATIF 标为已生成。Pi 的原生 Skill 和 DSH 指令前缀能力保持原边界。

## wheel 与持续验证

```sh
uv run --frozen --extra harbor python -m unittest discover -s tests
uv build --wheel
uv venv /tmp/evo-wheel --python 3.13
uv pip install --python /tmp/evo-wheel/bin/python dist/*.whl
cd /tmp
/tmp/evo-wheel/bin/evo-arena calibrate-business
```

安装 wheel 后的基础浏览和校准不依赖源码目录。要在该环境执行模型实验，再安装 `harbor==0.22.0`，构建任务镜像并配置连接。任务镜像 Dockerfile 和脚本也在 wheel 的 `evo_arena/fixtures/harness` 中。

GitHub Actions 执行锁定安装、包含真实 Harbor API 的 adapter 契约测试、业务校准、JS 语法检查和仓库外 wheel 校验。CI 不使用个人密钥，不发起真实模型请求；真实运行证据需单独记录，不能用 CI 代替。

## 本地状态和常见问题

- 默认数据在启动目录的 `.evoarena/`；可用 `--data-dir` 指定。该目录和模型凭据不提交 Git。
- `8787` 已占用时使用 `--port 8788`。同一个数据目录只能有一个控制进程。
- 未发现 Harbor：确认用了 `--extra harbor`，或显式指定 CLI；不要把相邻源码目录加入 `PYTHONPATH`。
- 镜像缺失：执行上面的 Docker 构建。首次镜像下载需要联网，运行 Trial 时容器离线。
- 实验报告可能记录未知用量；未知调用不能按零计费，也不自动重试。恢复边界见 [执行恢复](execution-recovery.md)。
- `calibrate`、`demo`、`register` 和 `export --season` 仍属于历史 FixOps。Evo 业务校准使用 `calibrate-business`，版本和实验报告通过新工作台导出。

独立安装只是 M6 的一个验收项。完整真实自动改进、封存报告和同搜索预算对照仍以 [TASKS.md](../TASKS.md) 为准。
