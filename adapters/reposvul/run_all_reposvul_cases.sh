#!/usr/bin/env bash
#
# ReposVul 审计批跑脚本(zsoft-detect 同款协议:盲评测 + 事后全迭代结算)。
#
# 用法:
#   ./run_all_reposvul_cases.sh --list-tasks
#   ./run_all_reposvul_cases.sh --one
#   ./run_all_reposvul_cases.sh --task-id jpirko-libndp-71401ba1
#   ./run_all_reposvul_cases.sh --lang C --min-findings 3
#
# 环境变量(与 zsoft 系脚本一致):
#   REPOSVUL_SHARED_KNOWLEDGE   1 时开启 Shared Knowledge Board(audit 模板,
#                               与 detect 同一 function_signatures/value_ranges/
#                               fp_verdicts 三分节;默认 0)
#   REPOSVUL_SKILL              1 时注入 path-validation skill(默认跟随知识板)
#   REPOSVUL_REA                1 时注入 rea-analysis skill + 挂载 REA(默认 0)
#   REA_ROOT                    REA 包根目录(默认 /home/tangzhiyun/workspace/rea)
#   BENCH_GOAL_PLUS_SOURCE_DIR  外部 goal-plus 检出(需含 PR !86;EXPECTED_REF 可选 pin)
#   L1_* / DETECT_* 不适用于本脚本;provider 与 gp-source 检查复用同一套环境变量。

set -uo pipefail
umask 077

resolve_work_dir() {
    if [[ -n "${BENCH_GOAL_PLUS_WORK_DIR:-}" ]]; then
        printf '%s' "${BENCH_GOAL_PLUS_WORK_DIR%/}"
        return 0
    fi
    local src="${BASH_SOURCE[0]}"
    while [[ -L "$src" ]]; do
        local dir
        dir="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"
        src="$(readlink "$src")"
        [[ "$src" != /* ]] && src="$dir/$src"
    done
    local dir
    dir="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/scripts/bench.py" ]]; then
            printf '%s' "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

readonly WORK_DIR="$(resolve_work_dir)"
[[ -n "$WORK_DIR" && -f "$WORK_DIR/scripts/bench.py" ]] \
    || { printf '错误: 无法定位 bench-goal-plus 仓库根\n' >&2; exit 1; }
# 脚本可从任意目录执行:后续 bench.py 等相对路径一律以仓库根解析
cd "$WORK_DIR" || { printf '错误: 无法进入仓库根 %s\n' "$WORK_DIR" >&2; exit 1; }
readonly PYTHON="$WORK_DIR/.bench-env/venv/bin/python"
readonly CAMPAIGN_BASE="${REPOSVUL_CAMPAIGN_BASE:-runs/reposvul-ds-1800-k2}"
if [[ "$CAMPAIGN_BASE" != runs/* || "$CAMPAIGN_BASE" =~ \.\.|/./|// ]]; then
    printf '错误: REPOSVUL_CAMPAIGN_BASE 必须以 runs/ 开头且不含 ..\n' >&2
    exit 1
fi

readonly PROVIDER="${REPOSVUL_PROVIDER:-deepseek}"
case "$PROVIDER" in
    deepseek)
        PROVIDER_KEY_ENV="DEEPSEEK_API_KEY"
        PROVIDER_BASE_ENV="DEEPSEEK_BASE_URL"
        PROVIDER_BASE_DEFAULT="http://113.46.219.251:8080/v1"
        PROVIDER_MODEL_DEFAULT="DeepSeek-V4.1-Flash-1017"
        ;;
    glm)
        PROVIDER_KEY_ENV="GLM_API_KEY"
        PROVIDER_BASE_ENV="GLM_BASE_URL"
        PROVIDER_BASE_DEFAULT="https://open.bigmodel.cn/api/paas/v4"
        PROVIDER_MODEL_DEFAULT="glm-4.6"
        ;;
    *)
        printf '错误: REPOSVUL_PROVIDER 只能是 deepseek 或 glm\n' >&2; exit 1
        ;;
esac
readonly REPOSVUL_MODEL="${REPOSVUL_MODEL:-$PROVIDER_MODEL_DEFAULT}"
readonly PROVIDER_KEY_ENV PROVIDER_BASE_ENV
readonly SHARED_KNOWLEDGE_ENABLED="${REPOSVUL_SHARED_KNOWLEDGE:-0}"
readonly REA_ENABLED="${REPOSVUL_REA:-0}"
readonly REA_ROOT="${REA_ROOT:-/home/tangzhiyun/workspace/rea}"
# path-validation skill 驱动知识板工具:默认跟随知识板开关
if [[ -n "${REPOSVUL_SKILL:-}" ]]; then
    readonly SKILL_ENABLED="$REPOSVUL_SKILL"
else
    readonly SKILL_ENABLED="$SHARED_KNOWLEDGE_ENABLED"
fi

WALL_TIME_SECONDS="${REPOSVUL_T:-1800}"
LIVE_SEARCH_CONCURRENCY="${REPOSVUL_K:-2}"
CELL_CONCURRENCY=1
REPEAT_COUNT="${REPOSVUL_R:-1}"
LANG_FILTER="${REPOSVUL_LANG:-}"
MIN_FINDINGS="${REPOSVUL_MIN_FINDINGS:-1}"
MODE="all"
REQUESTED_TASK_ID=""

die() { printf '错误: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
用法:
  ./run_all_reposvul_cases.sh [选项]

选项:
  --task-id ID        只跑指定任务(见 --list-tasks)
  --one               自动挑一个 C 语言、密度合适的任务验证
  --list-tasks        列出可选任务后退出
  --lang L            语言过滤:C / C++ / Java / Python(默认全部)
  --min-findings N    ground truth 最少 finding 数(默认 1;推荐 ≥3)
  -T 秒                墙钟预算(默认 1800)
  -K 并发              task cell 内 subagent 数(默认 2)
  -R 次                独立重复数(默认 1)
  -h, --help           本帮助

可选环境变量:
  REPOSVUL_PROVIDER            deepseek(默认) 或 glm
  REPOSVUL_MODEL               覆盖默认模型
  REPOSVUL_SHARED_KNOWLEDGE    1 开启 Shared Knowledge Board(默认 0)
  REPOSVUL_SKILL               1/0 强制控制 path-validation skill(默认跟随知识板)
  REPOSVUL_REA                 1 注入 rea-analysis skill + 挂载 REA(默认 0)
  REPOSVUL_CAMPAIGN_BASE       campaign 基路径(默认 runs/reposvul;必须 runs/ 开头)
  REPOSVUL_T / _K / _R         墙钟 / 并发 / 重复
  REPOSVUL_LANG / _MIN_FINDINGS 任务过滤
  DEEPSEEK_API_KEY / GLM_API_KEY   provider 密钥
  BENCH_GOAL_PLUS_SOURCE_DIR   外部 goal-plus 检出(含 PR !86 才能开知识板)
  BENCH_GOAL_PLUS_EXPECTED_REF 可选 ref pin
USAGE
}

list_tasks() {
    "$PYTHON" - "$WORK_DIR" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from adapters.reposvul.adapter import load_catalog
catalog = load_catalog()
lang = __import__("os").environ.get("REPOSVUL_LANG", "")
minimum = int(__import__("os").environ.get("REPOSVUL_MIN_FINDINGS", "1"))
count = 0
for task in catalog["tasks"]:
    if lang and task.get("language") != lang:
        continue
    if task.get("finding_count", 0) < minimum:
        continue
    count += 1
    if count <= 400:
        print(f"{task['task_id']}\t{task['language']}\t{task['finding_count']}\t{task['project']}")
print(f"-- 共 {count} 个任务(过滤: lang={lang or '全部'}, min_findings={minimum})", file=sys.stderr)
PY
}

while (($# > 0)); do
    case "$1" in
        --task-id) (($# >= 2)) || die "--task-id 缺少参数"; MODE="case"; REQUESTED_TASK_ID="$2"; shift 2 ;;
        --one) [[ "$MODE" == "all" ]] || die "--one 不能与 --task-id/--list-tasks 同用"; MODE="one"; shift ;;
        --list-tasks) MODE="list"; shift ;;
        --lang) (($# >= 2)) || die "--lang 缺少参数"; LANG_FILTER="$2"; shift 2 ;;
        --min-findings) (($# >= 2)) || die "--min-findings 缺少参数"; MIN_FINDINGS="$2"; shift 2 ;;
        -T) (($# >= 2)) || die "-T 缺少参数"; WALL_TIME_SECONDS="$2"; shift 2 ;;
        -K) (($# >= 2)) || die "-K 缺少参数"; LIVE_SEARCH_CONCURRENCY="$2"; shift 2 ;;
        -R) (($# >= 2)) || die "-R 缺少参数"; REPEAT_COUNT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1(见 --help)" ;;
    esac
done

export REPOSVUL_LANG="$LANG_FILTER"
export REPOSVUL_MIN_FINDINGS="$MIN_FINDINGS"

[[ "$MODE" == "list" ]] && { list_tasks; exit 0; }

# ---------- 密钥与 provider ----------
if [[ -z "${!PROVIDER_KEY_ENV:-}" ]]; then
    [[ -t 0 ]] || die "非交互运行前必须先 export $PROVIDER_KEY_ENV"
    read -rsp "请输入 $PROVIDER_KEY_ENV: " PROVIDER_KEY_VALUE; printf '\n'
    export "$PROVIDER_KEY_ENV=$PROVIDER_KEY_VALUE"
fi
[[ -n "${!PROVIDER_KEY_ENV:-}" ]] || die "$PROVIDER_KEY_ENV 不能为空"
export "$PROVIDER_BASE_ENV=${!PROVIDER_BASE_ENV:-$PROVIDER_BASE_DEFAULT}"
if [[ "$PROVIDER" == "glm" ]]; then unset DEEPSEEK_BASE_URL; else unset GLM_BASE_URL; fi

# ---------- 知识板前置检查 ----------
if [[ "$SHARED_KNOWLEDGE_ENABLED" == "1" ]]; then
    [[ -n "${BENCH_GOAL_PLUS_SOURCE_DIR:-}" ]] \
        || die "REPOSVUL_SHARED_KNOWLEDGE=1 需要含 knowledge board 的 goal-plus 运行时:请 export BENCH_GOAL_PLUS_SOURCE_DIR=<检出>(如 /data/disk/tangzhiyun/muyuan/plugins/goal-plus)"
    [[ -f "$BENCH_GOAL_PLUS_SOURCE_DIR/src/goal_plus/domain/shared_knowledge.py" ]] \
        || die "检出不含 shared knowledge board(缺 src/goal_plus/domain/shared_knowledge.py): $BENCH_GOAL_PLUS_SOURCE_DIR"
fi

# ---------- goal-plus 检出身份 ----------
if [[ -n "${BENCH_GOAL_PLUS_SOURCE_DIR:-}" ]]; then
    gp_head="$(git -C "$BENCH_GOAL_PLUS_SOURCE_DIR" rev-parse HEAD 2>/dev/null)" \
        || die "BENCH_GOAL_PLUS_SOURCE_DIR 不是可用的 git 检出"
    if [[ -n "${BENCH_GOAL_PLUS_EXPECTED_REF:-}" ]]; then
        gp_want="$(git -C "$BENCH_GOAL_PLUS_SOURCE_DIR" rev-parse --verify --end-of-options "${BENCH_GOAL_PLUS_EXPECTED_REF}^{commit}" 2>/dev/null)" \
            || die "goal-plus 检出中找不到 ref: $BENCH_GOAL_PLUS_EXPECTED_REF"
        [[ "$gp_head" == "$gp_want" ]] \
            || die "goal-plus HEAD 与 EXPECTED_REF 不一致(当前 $gp_head);精确复现请 checkout,直接用 HEAD 请 unset BENCH_GOAL_PLUS_EXPECTED_REF"
    fi
    echo "[gp-source] dir=$BENCH_GOAL_PLUS_SOURCE_DIR commit=$gp_head"
fi

# ---------- 宿主加固 + 断网预检(与 zsoft 系一致) ----------
if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
    export XDG_RUNTIME_DIR="$HOME/.xdg-runtime"
    mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
fi
export PATH="$WORK_DIR/.bench-env/venv/bin:$HOME/.local/bin:$PATH"
command -v bwrap >/dev/null 2>&1 || die "worker 断网沙箱需要 bwrap"
node_path="$(command -v node 2>/dev/null || true)"
[[ -n "$node_path" ]] || die "worker 断网沙箱需要 node(llm-relay 与 pi 运行时均为 node 程序)"
node_real="$(readlink -f "$node_path")"
[[ "$node_real" == /usr/* || "$node_real" == "$HOME"/.local/* ]] \
    || die "node 必须以真实二进制位于 /usr 或 ~/.local/bin(当前: $node_path -> $node_real)"

# ---------- skills / REA 环境 ----------
if [[ "$SKILL_ENABLED" == "1" ]]; then
    export BENCH_GOAL_PLUS_REPOSVUL_TASK_SKILLS=1
else
    unset BENCH_GOAL_PLUS_REPOSVUL_TASK_SKILLS
fi
if [[ "$REA_ENABLED" == "1" ]]; then
    [[ -f "$REA_ROOT/scripts/rea.mjs" && -d "$REA_ROOT/node_modules" ]] \
        || die "REPOSVUL_REA=1 但 REA 包不完整: $REA_ROOT"
    export BENCH_GOAL_PLUS_REPOSVUL_REA_TOOLS=1
    export BENCH_GOAL_PLUS_REA_ROOT="$REA_ROOT"
else
    unset BENCH_GOAL_PLUS_REPOSVUL_REA_TOOLS BENCH_GOAL_PLUS_REA_ROOT
fi

# ---------- 任务选择 ----------
select_task_ids() {
    "$PYTHON" - "$WORK_DIR" <<'PY'
import json, os, sys
sys.path.insert(0, sys.argv[1])
from adapters.reposvul.adapter import load_catalog
mode = os.environ.get("REPOSVUL_MODE", "all")
lang = os.environ.get("REPOSVUL_LANG", "")
minimum = int(os.environ.get("REPOSVUL_MIN_FINDINGS", "1"))
tasks = [
    t for t in load_catalog()["tasks"]
    if (not lang or t.get("language") == lang)
    and t.get("finding_count", 0) >= minimum
]
if mode == "one":
    for t in tasks:  # 首个 C 任务即验证样本
        if t["language"] == "C":
            print(t["task_id"]); raise SystemExit
    if tasks: print(tasks[0]["task_id"])
else:
    for t in tasks:
        print(t["task_id"])
PY
}
export REPOSVUL_MODE="$MODE"
declare -a TASK_IDS=()
if [[ "$MODE" == "case" ]]; then
    "$PYTHON" -c "
import sys; sys.path.insert(0,'$WORK_DIR')
from adapters.reposvul.adapter import catalog_task
catalog_task('$REQUESTED_TASK_ID')" 2>/dev/null \
        || die "未知 reposvul 任务: $REQUESTED_TASK_ID"
    TASK_IDS=("$REQUESTED_TASK_ID")
else
    mapfile -t TASK_IDS < <(select_task_ids)
fi
((${#TASK_IDS[@]} > 0)) || die "过滤后没有可选任务(lang=$LANG_FILTER min_findings=$MIN_FINDINGS)"

readonly RUN_STAMP="$(date +%Y%m%d-%H%M%S)-$$"
readonly LOG_DIR="$WORK_DIR/logs/reposvul_cases_$RUN_STAMP"
mkdir -p "$LOG_DIR"
readonly SUMMARY_FILE="$LOG_DIR/summary.txt"
: > "$SUMMARY_FILE"

status_message() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$LOG_DIR/progress.log"; }

{
    printf 'ReposVul 审计运行汇总\n'
    printf '开始时间: %s\n' "$(date --iso-8601=seconds)"
    printf '配置: provider=%s model=%s T=%d K=%d R=%d\n' "$PROVIDER" "$REPOSVUL_MODEL" "$WALL_TIME_SECONDS" "$LIVE_SEARCH_CONCURRENCY" "$REPEAT_COUNT"
    printf '知识板: %s(audit 模板) | path-validation skill: %s | REA: %s\n' "$SHARED_KNOWLEDGE_ENABLED" "$SKILL_ENABLED" "$REA_ENABLED"
    printf '任务过滤: lang=%s min_findings=%s\n' "${LANG_FILTER:-全部}" "$MIN_FINDINGS"
    printf '计划任务数: %d\n' "${#TASK_IDS[@]}"
    printf 'task_id\tseed\tstatus\tcampaign_id\tcase_log\n'
} > "$SUMMARY_FILE"

status_message "INIT work_dir=$WORK_DIR log_dir=$LOG_DIR tasks=${#TASK_IDS[@]}"
status_message "CONFIG provider=$PROVIDER model=$REPOSVUL_MODEL knowledge=$SHARED_KNOWLEDGE_ENABLED skill=$SKILL_ENABLED rea=$REA_ENABLED"
status_message "NETWORK_ISOLATION worker_sandbox=bwrap-loopback-only llm_egress=api-only-relay bwrap=$(command -v bwrap) node=$node_real"

build_bench_args() {
    local task_id="$1" campaign_id="$2" seed="$3"
    BENCH_ARGS=(
        --benchmark reposvul
        --task-id "$task_id"
        --campaign-id "$campaign_id"
        --campaign-dir "$WORK_DIR/$CAMPAIGN_BASE/$campaign_id"
        --method goal-plus-pi
        --seed "$seed"
        --model "$REPOSVUL_MODEL"
        --reasoning-effort high
        --pi-provider-id "$PROVIDER"
        --pi-api openai-completions
        --pi-api-key-env "$PROVIDER_KEY_ENV"
        --pi-api-base-env "$PROVIDER_BASE_ENV"
        --wall-time-seconds "$WALL_TIME_SECONDS"
        --live-search-concurrency "$LIVE_SEARCH_CONCURRENCY"
        --cell-concurrency "$CELL_CONCURRENCY"
        --worker-runtime-seconds 1200
        --worker-min-runtime-seconds 600
        --skip-bootstrap
        --skip-provision
        --foreground
    )
    if [[ "$SHARED_KNOWLEDGE_ENABLED" == "1" ]]; then
        BENCH_ARGS+=(--shared-cache)
    fi
}

# ---------- 预检 ----------
status_message "PREFLIGHT_START"
if "$PYTHON" scripts/bench.py setup \
    --benchmark reposvul \
    --method goal-plus-pi \
    --model "$REPOSVUL_MODEL" \
    --reasoning-effort high \
    --pi-provider-id "$PROVIDER" \
    --pi-api openai-completions \
    --pi-api-key-env "$PROVIDER_KEY_ENV" \
    --pi-api-base-env "$PROVIDER_BASE_ENV" \
    --skip-bootstrap --skip-provision >"$LOG_DIR/preflight.log" 2>&1; then
    status_message "PREFLIGHT_OK"
else
    status_message "PREFLIGHT_FAILED log=$LOG_DIR/preflight.log"
    die "运行前环境检查失败,详见 $LOG_DIR/preflight.log"
fi

# ---------- 主循环(串行 task × R 重复) ----------
run_task() {
    local task_id="$1" seed="$2"
    local campaign_id="reposvul-${task_id}-$RUN_STAMP-a1"
    local log_file="$LOG_DIR/cases/$task_id-seed$seed.log"
    mkdir -p "$(dirname "$log_file")"
    status_message "CASE_START task=$task_id seed=$seed campaign_id=$campaign_id"
    build_bench_args "$task_id" "$campaign_id" "$seed"
    if "$PYTHON" scripts/bench.py plan "${BENCH_ARGS[@]}" >>"$log_file" 2>&1 \
        && "$PYTHON" scripts/bench.py launch "${BENCH_ARGS[@]}" >>"$log_file" 2>&1; then
        printf '%s\t%s\tCOMPLETED\t%s\t%s\n' "$task_id" "$seed" "$campaign_id" "$log_file" >> "$SUMMARY_FILE"
        status_message "CASE_COMPLETE task=$task_id seed=$seed result=COMPLETED"
    else
        printf '%s\t%s\tINFRA_ERROR\t%s\t%s\n' "$task_id" "$seed" "$campaign_id" "$log_file" >> "$SUMMARY_FILE"
        status_message "CASE_COMPLETE task=$task_id seed=$seed result=INFRA_ERROR(详见 $log_file)"
    fi
}

for task_id in "${TASK_IDS[@]}"; do
    for ((seed=1; seed<=REPEAT_COUNT; seed++)); do
        run_task "$task_id" "$seed"
    done
done

status_message "ALL_DONE summary=$SUMMARY_FILE"
echo
echo "汇总: $SUMMARY_FILE"
echo "跑后断网复核: $PYTHON scripts/audit_network_access.py $WORK_DIR/$CAMPAIGN_BASE"
