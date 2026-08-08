# Host 与鉴权矩阵

先确定三个独立维度，再执行 setup：

1. benchmark/runner；
2. host：macOS 或 Linux；
3. agent/provider：Codex OAuth、OpenAI-compatible API、Pi 或
   Anthropic-compatible API。

这些组合的依赖和网络路径不同。不要把一种组合的 doctor 结果外推到另一种组合。

## Host 差异

| 项目 | macOS | Linux |
| --- | --- | --- |
| Docker | Docker Desktop 或 OrbStack | 原生 Docker Engine |
| EdgeBench 容器架构 | Docker VM 必须提供 `linux/amd64` | daemon 必须是 `amd64/x86_64` |
| 宿主 Judge | Work container 通过 `host.docker.internal` 访问 | controller 使用 host route + systemd socket bridge |
| 宿主 loopback API | 当前 EdgeBench controller 不支持把 `127.0.0.1` API 从 Mac 桥入容器；使用容器可达的非 loopback URL | 需要 `ip`、`systemd-socket-activate` 和 `systemd-socket-proxyd` |
| `internet=false` | Docker VM 通常不能满足 SForge 的 host `iptables` gate；只能使用 profile 明确声明的 open-network smoke | 需要 SForge 可使用 passwordless `sudo iptables` 完成 allowlist |
| Codex container runtime | 需要 Linux x64 Codex runtime cache | 同样需要 Linux x64 Codex runtime cache |
| Goal Plus container runtime | controller 会把受管 Goal Plus checkout 复制进容器；不能复制 macOS Python/venv | 可选复制兼容目标镜像的 Linux x64 便携 Python；普通 host venv 不能直接复用 |

两种 host 都必须通过 benchmark-native doctor。macOS 能跑 local smoke 不等于官方
offline/network-isolated protocol 已满足；正式 Linux 运行也不能跳过 bridge、resource limit
和 `iptables` 检查。

## 鉴权方式

| 路径 | 支持的鉴权 | 配置来源 | 重要限制 |
| --- | --- | --- | --- |
| EdgeBench Plain/Goal Plus Codex | Codex OAuth 或 OpenAI-compatible API | OAuth auth file，或 `SFORGE_AGENT_*` / `OPENAI_*` env | custom loopback API 只在具备 Linux bridge 时可用 |
| EdgeBench Plain/Goal Plus Pi OAuth | Pi 的 `openai-codex` 登录 | `SFORGE_PI_AUTH_FILE` 或 `~/.pi/agent/auth.json` | 只适用于 `plain-pi` / `goal-plus-pi` |
| EdgeBench Goal Plus + Pi provider API | Pi 显式 provider/model 与 API credential | Pi built-in provider 使用标准 key env；自定义 endpoint 使用 `SFORGE_PI_MODELS_FILE` 或 `~/.pi/agent/models.json` | 使用 `goal-plus-pi-provider`；model 必须写成 `PROVIDER/MODEL` |
| EdgeBench Claude | Anthropic-compatible API | `SFORGE_AGENT_*` 或 `ANTHROPIC_*` env | key 和 base URL 都必需 |
| Common/OpenEvolve 的 Codex 路径 | Codex native login，或显式 OpenAI-compatible endpoint | 省略 `--api-base` 使用 native login；显式 endpoint 使用 `OPENAI_API_KEY` | custom provider 使用 Responses wire API |
| SWE-bench Verified Plain/Goal Plus Codex | profile 固定的 OpenAI-compatible API | `OPENAI_BASE_URL` + `OPENAI_API_KEY` | 只使用 Responses；不读取 OAuth；Linux loopback endpoint 必须桥入 task container |
| SWE-bench Verified Plain/Goal Plus Pi | Pi built-in provider API，或 profile-frozen OpenAI-compatible provider | profile 中的 `PROVIDER/MODEL` + provider 标准 key env，或 `OPENAI_BASE_URL` + `OPENAI_API_KEY` | Z.AI profile 使用 `zai/glm-5.2`；Luna profile 使用 `bench-openai/gpt-5.6-luna` + Responses；均不读取 EdgeBench Pi OAuth |
| Common/OpenEvolve 的 Pi、native OpenEvolve、SkyDiscover | OpenAI-compatible API | `--api-base` + `OPENAI_API_KEY` | 不是 Codex OAuth 路径 |

### Codex OAuth

EdgeBench 查找顺序：

1. `SFORGE_CODEX_AUTH_FILE`；
2. `$CODEX_HOME/auth.json`；
3. 默认 `~/.codex/auth.json`。

OAuth 模式不需要把 token 复制进 profile 或环境变量。doctor 只记录 auth 文件路径和模式，
不记录内容。EdgeBench 还要求：

```text
~/.cache/sforge/codex/codex-0.144.1-linux-x64.tgz
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

Goal Plus + Pi 的 host Node、Pi package 和受管 Goal Plus checkout 只读挂载进精确 task
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

wire API 由 Pi registry 中 provider 的 `api` 字段决定。当前 Pi 能识别的
`anthropic-messages`、`openai-completions`、`openai-responses` 都走同一个
`goal-plus-pi-provider` adapter；bench 控制面只验证 provider/model/credential，
不会把远程 Claude API 或 OpenAI-compatible API 写成不同 method。

“OpenAI-compatible”只表示 endpoint 使用 OpenAI 风格协议族，不等于它实现了 OpenAI
Responses API。必须按实际 route 分开验证：

| Pi `api` | 必须成功的 endpoint | 失败时的结论 |
| --- | --- | --- |
| `openai-completions` | `POST /chat/completions` | 不能使用该 Chat Completions 配置 |
| `openai-responses` | `POST /responses` | 不能选择 Responses；不得用 Chat 成功结果代替 |

选择顺序是 Responses-first，但不是按厂商名猜测：先探测 `/responses`，成功后再通过
Pi streaming + tool loop；任一层失败才回退 `/chat/completions`。可先运行仓库随 Skill
提供的无密钥落盘 probe：

```bash
python3 .agents/skills/benchmark-setup/scripts/probe_openai_wire.py \
  --base-url https://api.example.com/v1 \
  --model provider-model-id \
  --api-key-env PROVIDER_API_KEY
```

该脚本只给出 wire-level 推荐，不能代替 Pi session 和 EdgeBench Work container gate。
如果 `/chat/completions` 成功而 `/responses` 返回 404，只能登记
`api: "openai-completions"`。

截至 2026-08-01 的实测矩阵如下；远端能力会变化，每次正式 campaign 仍须重跑 probe：

| Provider/model | Responses | Chat Completions | Pi 0.83.0 结论 |
| --- | --- | --- | --- |
| Z.AI `glm-5.2` | 404 | 成功 | built-in `zai/glm-5.2` 使用 `openai-completions`；工具回环与 reasoning usage 均通过 |
| DeepSeek `deepseek-v4-flash` | 成功 | 成功 | built-in `deepseek` 仍使用 `openai-completions`；要优先 Responses，使用自定义 `deepseek-responses` registry；Responses 工具回环与 reasoning usage 均通过 |

DeepSeek 的[官方 Responses API 指南](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)
确认当前仅 `deepseek-v4-flash` 支持该接口，`deepseek-v4-pro` 暂不支持；不能因为两者都能
通过 Chat Completions 调用，就把 V4 Pro 登记成 `openai-responses`。该实现是无状态 API，
不支持 `previous_response_id`、`conversation`、`background` 等能力；Agent 验证应使用
它明确支持的 streaming、function tool call 和 tool result 回传链路。

Z.AI 同一把 `ZAI_API_KEY` 应优先写成 `zai/glm-5.2`，不需要额外
`models.json`。DeepSeek built-in 虽已登记 V4 Flash，但 wire API 仍是 Chat
Completions；Responses-first 路径使用
[registry 模板](pi-openai-provider-registry.example.json)。只有评测自定义 endpoint、
切换 wire API 或 Pi 尚未内置的 model/provider 时才创建自定义 registry，例如：

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

1. 记录 `pi --version`，对声明的 wire API 发最小协议请求；不要只探测 base URL 或
   models route。
2. Responses 成功时优先登记 `openai-responses`；再运行一次短 Pi JSON session，确认
   事件中的 `api` 与 registry 一致，并至少完成一次 tool call → tool result → final answer。
3. reasoning model 至少出现 thinking content/event，或响应 usage 中有非零
   reasoning token；只有普通文本输出不算“正在思考”的证据。
4. EdgeBench 路径还必须在实际 Work container、实际运行用户、同一个
   `PI_CODING_AGENT_DIR` 中执行 `pi --list-models <provider>`，确认精确的
   `PROVIDER/MODEL` 可见。host doctor 成功不能替代这一步。

这种 smoke 只证明 provider wiring 和推理事件可用，不是 benchmark 成绩。用户只要求
“跑起来并确认正在思考”时，取得上述证据后立即 stop，保留 partial artifacts，并执行
统一 `finish` 归档；不要继续消耗完整的一小时预算。

models registry 的 `apiKey` 必须写成 `$NAME` 或 `${NAME}`。裸 `NAME` 在 Pi 中是字面值，
不是环境变量引用；明文 credential 和命令型 credential 都会被 adapter 拒绝。adapter
只把引用变量的运行时值传入 Work container；profile、command 和 doctor 输出只记录
变量名，不记录值。控制面在 plan 阶段拒绝裸 model ID，在 doctor 阶段检查 provider、
model 和 credential source；它不会把 Pi provider 错配为 `openai-codex`。

### EdgeBench Goal Plus / Pi runtime cache

`codex-goal-plus`、`pi-goal-plus` 与 `pi-goal-plus-provider` 已由 SForge 原生支持，
不需要 Skill 在容器里手工
拼安装命令。当前真实路径是：

- controller 设置 `SFORGE_GOAL_PLUS_SOURCE_DIR`，`prepare_container` 把受管
  Goal Plus checkout 复制到 `/opt/goal-plus`，因此不会在每个任务里重新 clone；
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
