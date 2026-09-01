# Host 与鉴权矩阵

先确定三个独立维度，再执行 setup：

1. benchmark/runner；
2. host：macOS 或 Linux；
3. agent/provider：Codex OAuth、OpenAI-compatible API、Pi 或
   Anthropic-compatible API。

这些组合的依赖和网络路径不同。不要把一种组合的 doctor 结果外推到另一种组合。

## Host 差异

<!-- markdownlint-disable MD013 -->

| 项目 | macOS | Linux |
| --- | --- | --- |
| Docker | Docker Desktop 或 OrbStack | 原生 Docker Engine |
| EdgeBench 容器架构 | Docker VM 必须提供 `linux/amd64` | daemon 必须是 `amd64/x86_64` |
| 宿主 Judge | Work container 通过 `host.docker.internal` 访问 | controller 使用 host route + systemd socket bridge |
| 宿主 loopback API | 当前 EdgeBench controller 不支持把 `127.0.0.1` API 从 Mac 桥入容器；使用容器可达的非 loopback URL | 需要 `ip`、`systemd-socket-activate` 和 `systemd-socket-proxyd` |
| API-only Agent 网络 | SForge 通过本地已有的 `ubuntu:22.04` privileged nsenter helper 进入 Docker VM 应用 `iptables`；helper 只执行固定规则且使用 `--pull never` | 需要 SForge 可使用 passwordless `sudo iptables` 完成 Judge + LLM API allowlist |
| Codex container runtime | 需要 Linux x64 Codex runtime cache | 同样需要 Linux x64 Codex runtime cache |
| Goal Plus container runtime | controller 会把受管 Goal Plus source directory 复制进容器；不能复制 macOS Python/venv | 可选复制兼容目标镜像的 Linux x64 便携 Python；普通 host venv 不能直接复用 |

<!-- markdownlint-enable MD013 -->

两种 host 都必须通过 benchmark-native doctor。macOS 能跑 local smoke 不等于官方
offline/network-isolated protocol 已满足；正式 Linux 运行也不能跳过 bridge、resource limit
和 `iptables` 检查。

EdgeBench Agent 测评统一使用 API-only 网络：`internet=true` 即失败；只放行每个 cell 的
Judge 和 main/worker/evidence annotation 实际使用的 LLM API endpoint。OpenAI、Anthropic、
Z.AI/GLM、自定义 local endpoint 可以同时解析成多 endpoint allowlist，但 endpoint 多或
loopback bridge 失败都不能触发开放公网回退。安装依赖发生在 Agent 隔离启用前，npm/PyPI、
公网代理和任务网站不得进入 Agent 运行阶段 allowlist。

## 鉴权方式

<!-- markdownlint-disable MD013 -->

| 路径 | 支持的鉴权 | 配置来源 | 重要限制 |
| --- | --- | --- | --- |
| EdgeBench Plain/Goal Plus Codex | Codex OAuth 或 OpenAI-compatible API | OAuth auth file，或 `SFORGE_AGENT_*` / `OPENAI_*` env | custom loopback API 只在具备 Linux bridge 时可用 |
| EdgeBench Plain/Goal Plus Pi OAuth | Pi 的 `openai-codex` 登录 | `SFORGE_PI_AUTH_FILE` 或 `~/.pi/agent/auth.json` | 只适用于 `plain-pi` / `goal-plus-pi` |
| EdgeBench Goal Plus + Pi provider API | Pi 显式 provider/model 与 API credential | Pi built-in provider 使用标准 key env；自定义 endpoint 使用 `SFORGE_PI_MODELS_FILE` 或 `~/.pi/agent/models.json` | 使用 `goal-plus-pi-provider`；model 必须写成 `PROVIDER/MODEL` |
| EdgeBench Claude | Anthropic-compatible API | `SFORGE_AGENT_*` 或 `ANTHROPIC_*` env | key 和 base URL 都必需 |
| Common/OpenEvolve 的 Codex 路径 | Codex native login，或显式 OpenAI-compatible endpoint | 省略 `--api-base` 使用 native login；显式 endpoint 使用 `OPENAI_API_KEY` | custom provider 使用 Responses wire API |
| SWE-bench Verified Plain/Goal Plus Codex | profile 固定的 OpenAI-compatible API | `OPENAI_BASE_URL` + `OPENAI_API_KEY` | 只使用 Responses；不读取 OAuth；Linux loopback endpoint 必须桥入 task container |
| SWE-bench Verified Plain/Goal Plus Pi | Pi built-in provider API，或 profile-frozen OpenAI-compatible provider | profile 中的 `PROVIDER/MODEL` + provider 标准 key env，或 `OPENAI_BASE_URL` + `OPENAI_API_KEY` | Z.AI profile 使用 `zai/glm-5.2`；Luna profile 使用 `bench-openai/gpt-5.6-luna` + Responses；均不读取 EdgeBench Pi OAuth |
| ZSoft Detect native SWE-agent | benchmark-owned metered OpenAI-compatible proxy | `OPENAI_COMPAT_BASE_URL` + `OPENAI_COMPAT_API_KEY`，optional `OPENAI_COMPAT_HEADERS_JSON` | only `zsoft-detect-swe-agent`; native Linux+bwrap; SWE-agent uses Chat Completions through the host meter; no OAuth or `OPENAI_*` fallback |
| Common/OpenEvolve 的 Pi、native OpenEvolve、SkyDiscover | OpenAI-compatible API | `--api-base` + `OPENAI_API_KEY` | 不是 Codex OAuth 路径 |

<!-- markdownlint-enable MD013 -->

### ZSoft Detect native SWE-agent API

The dedicated ZSoft target preserves the upstream variable names exactly:

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_HEADERS_JSON  # optional JSON object
```

The real key remains in the host-side metering proxy. Bubblewrap receives only
a dummy key and loopback proxy URL. The full doctor validates presence and
header JSON shape without serializing values; the profiled local-asset check
does not inspect credentials. This method does not read Codex OAuth,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, or EdgeBench `SFORGE_AGENT_*` fallbacks.

This runner is native Linux-only. A healthy OrbStack daemon on macOS proves
Docker availability for Docker-owned benchmarks, not Bubblewrap availability
for ZSoft. Do not add an unreviewed container wrapper around the upstream
launcher.

### Codex OAuth

EdgeBench 查找顺序：

1. `SFORGE_CODEX_AUTH_FILE`；
2. `$CODEX_HOME/auth.json`；
3. 默认 `~/.codex/auth.json`。

OAuth 模式不需要把 token 复制进 profile 或环境变量。doctor 只记录 auth 文件路径和模式，
不记录内容。EdgeBench 还要求：

```text
~/.cache/sforge/codex/codex-0.150.1-linux-x64.tgz
```

该缓存是 Work container 使用的 Linux Codex runtime，不是当前 Mac/Linux host 的 Codex
可执行文件本身。

### EdgeBench OpenAI-compatible API

Key 的优先级：

1. `SFORGE_AGENT_API_KEY`
2. `OPENAI_API_KEY`
3. `CODEX_API_KEY`

Base URL 的优先级：

1. `SFORGE_AGENT_API_BASE_URL`
2. `OPENAI_BASE_URL`

自定义 endpoint 应同时设置 key 和 base URL：

```bash
export SFORGE_AGENT_API_KEY='<secret>'
export SFORGE_AGENT_API_BASE_URL='https://api.example.com/v1'
```

controller 会从 host 和 Work container 各做一次鉴权 probe。manifest 只记录使用了哪个环境
变量，不记录值。

对 `goal-plus-codex`，host probe 不是普通 Codex ping。controller 使用 SForge 将采用的
精确 Codex 版本、profile 中的 model/reasoning，以及当次解析并冻结的 Goal Plus source
commit 启动临时 MCP server；必须在 Codex JSON event 中观察到成功的
`goal_plus_monitor_snapshot` `mcp_tool_call`。如果 endpoint 能完成 shell tool call 但拒绝
Goal Plus tool schema，setup 会在任何 Docker 创建前失败。API base/key 仍只从上述环境
变量动态读取，不写入 profile 或源代码。

### SWE-bench Verified Plain/Goal Plus Codex API

SWE-bench 的冻结 Codex profile 不使用上面的 EdgeBench fallback 优先级。它精确选择：

```text
auth_mode=openai-compatible
base_url_env=OPENAI_BASE_URL
api_key_env=OPENAI_API_KEY
wire_api=responses
```

因此，同一 shell 同时存在指向 Anthropic endpoint 的 `SFORGE_AGENT_*` 时，SWE controller
也不得读取它。doctor 和 launch 必须解析同一 profile contract；Codex CLI 使用显式 custom
provider，key 只通过 `docker exec -e OPENAI_API_KEY` 的变量名继承。OAuth auth file 不挂载进
task container，任何 `chatgpt.com` 请求都表示配置回退 bug。

先做 host wire probe：

```bash
python3 .agents/skills/benchmark-setup/scripts/probe_openai_wire.py \
  --base-url "$OPENAI_BASE_URL" \
  --model gpt-5.6-sol \
  --api-key-env OPENAI_API_KEY \
  --probe responses
```

若 `OPENAI_BASE_URL` 监听 `127.0.0.1`，Linux task container 不能直接使用该地址。controller
复用 EdgeBench 的随机端口 `systemd-socket-proxyd` bridge，并保留 `/v1` base path。先检查：

```bash
command -v ip
test -x /usr/bin/systemd-socket-activate
test -x /lib/systemd/systemd-socket-proxyd
```

缺件时由主机管理员按发行版安装，不在 benchmark setup 中自动修改共享主机：

```bash
# Debian / Ubuntu
sudo apt-get install iproute2 systemd

# RHEL / Rocky / AlmaLinux
sudo dnf install iproute systemd
```

安装后仍需统一 `setup --skip-provision` 的完整 doctor 同时通过 host Responses、bridge 和
task-container Responses；只通过 host probe 不能启动 campaign。macOS 当前不支持这条
loopback bridge，必须使用容器可达的非-loopback URL。

### SWE-bench Verified Pi 与 Goal Plus + Pi

SWE-bench 的 Pi profile 使用精确 `PROVIDER/MODEL`。当前 Plain Pi 和 Goal Plus + Pi smoke
都冻结 `zai/glm-5.2`，只按 Pi built-in provider 规则继承 `ZAI_API_KEY`；credential value
不进入 Docker 命令、manifest 或报告。该路径不读取 `SFORGE_PI_AUTH_FILE`、
`openai-codex` OAuth，也不使用 EdgeBench 的 `SFORGE_AGENT_*` fallback。

`swe-bench-verified-sympy-16886-goal-plus-pi-luna-high-smoke` 另行冻结
`bench-openai/gpt-5.6-luna`、high、Responses、`OPENAI_BASE_URL` 和
`OPENAI_API_KEY`。controller 在仓库内 campaign/doctor 临时目录生成只含
`$OPENAI_API_KEY` 引用的 Pi `models.json`；不会读取或修改宿主默认 Pi registry。loopback
URL 必须通过 Linux socket bridge，并依次通过 host Responses、task-container Responses 和
容器内 `pi --offline --list-models bench-openai`。任一失败都不得回退到 `chatgpt.com`、OAuth
或 Z.AI provider。

Goal Plus + Pi 的 host Node、Pi package 和受管 Goal Plus source directory 只读挂载进精确 task
image。Python 依赖来自 `environment/swe-bench-goal-plus-requirements.lock`，安装到一次性
容器的 `/opt/goal-plus-runtime` tmpfs；唯一持久可写依赖缓存是仓库内
`.tmp/swe-bench-verified/goal-plus-pip-cache`。doctor 必须同时验证 Pi 精确 model、Goal Plus
checkout/asset 和容器内 import/CLI，不得用 host `pi --version` 代替容器 gate。

### EdgeBench Pi OAuth

auth JSON 必须包含 `openai-codex` entry：

```bash
export SFORGE_PI_AUTH_FILE=/path/to/pi-auth.json
```

未设置时使用 `~/.pi/agent/auth.json`。Plain Pi 与 Goal Plus + Pi 都使用这条路径；
不能把 common/OpenEvolve 的 Pi direct-API 配置照搬到 EdgeBench。

### EdgeBench Pi provider API

`goal-plus-pi-provider` 不使用 `openai-codex` OAuth。profile 必须冻结精确的
`PROVIDER/MODEL`，例如 `zai/glm-5.2`、`deepseek/deepseek-v4-flash` 或
`deepseek-responses/deepseek-v4-flash`。

Pi built-in provider 不需要额外 registry。adapter 跟随当前解析 Pi 版本的标准 key env，
包括 `ZAI_API_KEY`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、
`ANTHROPIC_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`、`GEMINI_API_KEY`、
`OPENROUTER_API_KEY`、`GROQ_API_KEY`、`MISTRAL_API_KEY`、
`MOONSHOT_API_KEY` 等；Anthropic OAuth token 优先于 API key。完整映射以 controller
的 `PI_BUILTIN_PROVIDER_API_KEYS` 和受管 EdgeBench adapter 的
`BUILTIN_PROVIDER_API_KEYS` 为可执行事实源。

自定义 endpoint 才从 `SFORGE_PI_MODELS_FILE`（默认
`~/.pi/agent/models.json`）读取 `baseUrl`、wire API 和 model registration。Pi 没有一套
适用于所有 provider 的通用 base-URL 环境变量，因此这条路径通常只需两个 host env：
`SFORGE_PI_MODELS_FILE` 和 registry 中 `apiKey` 引用的 credential env。更换 DeepSeek
等 Pi built-in provider 时不需要把 adapter 或 provider 名改成 `glm-proxy`；直接使用其
真实 `PROVIDER/MODEL` 与标准 key env。

这条路径与宿主操作系统无关：macOS 和 Linux 都使用 `pathlib` 解析 host registry，
再由 SForge 只把选中的 provider/model 配置物化到 Linux Work container 的固定 Pi
配置目录；实现中没有
OrbStack socket、`/Users/...` 或其他 macOS 专用路径。服务器可通过
`SFORGE_PI_MODELS_FILE` 指向自己的 registry。

wire API 由外部 Pi registry 中 provider 的 `api` 字段决定。当前 Pi 能识别的
`anthropic-messages`、`openai-completions`、`openai-responses` 都走同一个
`goal-plus-pi-provider` adapter；bench 控制面不追加协议 route、不预测 provider
兼容性，也不把远程 Claude API 或 OpenAI-compatible API 写成不同 method。自定义
provider 只需提供外部配置和该配置引用的 credential environment，例如：

```json
{
  "providers": {
    "zai-openai": {
      "baseUrl": "https://api.z.ai/api/coding/paas/v4",
      "api": "openai-completions",
      "apiKey": "$ZAI_API_KEY",
      "authHeader": true,
      "models": [
        {
          "id": "glm-5.2",
          "reasoning": true,
          "input": ["text"],
          "contextWindow": 200000,
          "maxTokens": 128000
        }
      ]
    }
  }
}
```

API smoke 必须使用 campaign 将采用的 Pi 版本，并按以下顺序取证：

1. controller 把外部 registry 的所选 provider/model 原样复制到仓库本地的隔离
   `PI_CODING_AGENT_DIR`，记录 `pi --version`，再让 Pi 自己按该配置完成一次真实请求；
   controller 不直接构造 provider HTTP 请求。
2. 短 Pi JSON session 必须确认事件中的 provider/model 与请求一致，并至少完成一次
   tool call → tool result → final answer。
3. reasoning model 至少出现 thinking content/event，或响应 usage 中有非零
   reasoning token；只有普通文本输出不算“正在思考”的证据。
4. 宿主机真实回环通过后，controller 才启动诊断 Work container；容器只对 registry
   动态给出的 base URL 做无凭据连通性检查，任意 HTTP 响应都能证明网络可达。协议语义
   继续由随后复制同一份 registry 的 Pi runtime 负责。

这种 smoke 只证明 provider wiring 和推理事件可用，不是 benchmark 成绩。用户只要求
“跑起来并确认正在思考”时，取得上述证据后立即 stop，保留 partial artifacts，并执行
统一 `finish` 归档；不要继续消耗完整的一小时预算。

models registry 的 `apiKey` 必须写成 `$NAME` 或 `${NAME}`。裸 `NAME` 在 Pi 中是字面值，
不是环境变量引用；明文 credential 和命令型 credential 都会被 adapter 拒绝。adapter
只把引用变量的运行时值传入 Work container；profile、command 和 doctor 输出只记录
变量名，不记录值。控制面在 plan 阶段拒绝裸 model ID，在 doctor 阶段检查 provider、
model 和 credential source；它不会把 Pi provider 错配为 `openai-codex`。

### EdgeBench Goal Plus / Pi runtime cache

`codex-goal-plus`、`pi-goal-plus` 与 `pi-goal-plus-provider` 已由 SForge
原生支持，不需要 Skill 在容器里手工拼安装命令，也不允许 Skill 启动 Goal Plus。
Codex 的启动链固定为精确 `$goal-plus ...` UserPromptSubmit、project-local hooks
授权、显式 Goal Plus MCP；恢复只能提交精确 `$goal-plus resume`。普通自然语言、
Skill、plugin 和 MCP create/update 工具都没有启动或恢复权限。Pi 的启动链同样固定为
精确 `/goal-plus ...` extension command；通过 `pi -c` 新进程恢复 native session 时，
只能提交无附加文本的 `/goal-plus resume`。当前真实路径是：

- 默认情况下 controller 使用 registry 声明的官方 Muyuan `master` checkout。临时实验分支
  不修改 registry，而是显式设置 `SFORGE_GOAL_PLUS_SOURCE_DIR` 为外部 checkout 中的
  `plugins/goal-plus`，并设置 `SFORGE_GOAL_PLUS_EXPECTED_REF`。doctor 在 Docker 前要求
  checkout clean、HEAD 与该 ref 的本地 commit 一致、运行资产完整；prepare 冻结 commit，
  launch 再核对后才由 `prepare_container` 复制到 `/opt/goal-plus`。Codex source
  还必须包含 UserPromptSubmit/PreToolUse/Stop hooks 与
  `allow_implicit_invocation: false` Skill metadata；
- SForge 接受 `SFORGE_GOAL_PLUS_PYTHON_DIR`，把 Linux Python 3.10+ runtime
  复制到 `/opt/sforge-python`；该目录必须与 Work container 的平台兼容，macOS
  Python 或 macOS venv 不能使用；
- 未设置便携 Python 时，每个 Work container 仍会执行 Goal Plus `pip install`；
- Pi 的 Node.js 与 Pi package 当前仍按任务安装。Work container 默认使用
  `SFORGE_PI_PACKAGE_VERSION=latest` 跟随新 provider/model 支持；正式可复现 campaign
  应在 profile 的 `pi_package_version` 中冻结 smoke 已验证的精确版本，并保留
  安装日志中的 `pi --version`。下载镜像只能加速下载，不等于已经有可复用 runtime。

因此，进一步降低启动耗时应在 EdgeBench/SForge provision 中生成并校验按
`architecture + Python/Node/Pi version + Goal Plus commit` 定址的 Linux runtime
bundle，再由 `prepare_container` 复制或挂载。bench-goal-plus 只负责选择、传入并在
doctor 中验证该 bundle；不要把构建逻辑写进 Skill，也不要修改或冒充任务固定的
Work/Judge image tag。

### Anthropic-compatible API

Key 的优先级：

1. `SFORGE_AGENT_API_KEY`
2. `ANTHROPIC_AUTH_TOKEN`
3. `ANTHROPIC_API_KEY`

Base URL 的优先级：

1. `SFORGE_AGENT_API_BASE_URL`
2. `ANTHROPIC_BASE_URL`

EdgeBench Claude campaign 要求 key 与 base URL 同时存在，并在 host 和 container
完成协议匹配的 probe。

## Setup 顺序

```bash
python3 scripts/bench.py catalog
python3 scripts/bench.py setup --preset <preset>
python3 scripts/bench.py plan --preset <preset>
```

`setup` 根据 target 执行 bootstrap、doctor 和已登记的 provision。不要在 Skill 中手工复制
平台判断；实际 gate 以 `benchmarks/registry.json`、`benchmarks/runners.json`、
`environment/upstreams.json` 和 runner doctor 为准。

## Secret 边界

- Secret 只存在于继承环境或 host auth store。
- 不把 key、token、cookie、auth JSON 或 provider header 写入仓库。
- 不把 secret-bearing shell 命令保存进 evidence。
- 报告可以记录 auth mode、provider protocol 和变量名，但不能记录变量值。
