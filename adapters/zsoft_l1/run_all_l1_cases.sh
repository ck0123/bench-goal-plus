#!/usr/bin/env bash

set -uo pipefail
umask 077

# Locate the bench-goal-plus repository root (WORK_DIR) so this script runs from
# wherever it lives — including its committed home under adapters/zsoft_l1/.
# Resolution order:
#   1. $BENCH_GOAL_PLUS_WORK_DIR if set (explicit override)
#   2. walk up from this script's real directory to the first ancestor that
#      looks like a bench-goal-plus checkout (has scripts/bench.py).
resolve_work_dir() {
    if [[ -n "${BENCH_GOAL_PLUS_WORK_DIR:-}" ]]; then
        printf '%s' "${BENCH_GOAL_PLUS_WORK_DIR%/}"
        return 0
    fi
    local src="${BASH_SOURCE[0]}"
    # Resolve symlinks to the script's real path.
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
    || { printf '错误: 无法定位 bench-goal-plus 仓库根 (设置 BENCH_GOAL_PLUS_WORK_DIR 可覆盖)\n' >&2; exit 1; }
readonly CAMPAIGN_BASE="${L1_CAMPAIGN_BASE:-runs/l1-deepseek-v4.1-flash-1017-withboard}"
if [[ "$CAMPAIGN_BASE" != runs/* || "$CAMPAIGN_BASE" =~ \.\.|/./|// ]]; then
    printf '错误: L1_CAMPAIGN_BASE 必须是 WORK_DIR 下以 runs/ 开头且不含 .. 的相对路径(当前: %s);跨盘存储请用软链接(见脚本头部注释)\n' "$CAMPAIGN_BASE" >&2
    exit 1
fi
readonly ZSOFT_CHECKOUT="$WORK_DIR/third_party/zsoft-bench"
readonly ZSOFT_ROOT="$WORK_DIR/third_party/zsoft-bench/benchmarks/vulnerability/zsoft-l1"
readonly TASKS_DIR="$ZSOFT_ROOT/tasks"
readonly STATE_DIR="$ZSOFT_ROOT/.task-state"
readonly PYTHON="$WORK_DIR/.bench-env/venv/bin/python"
readonly EXPECTED_CASES="${L1_EXPECTED_CASES:-33}"
readonly CURRENT_SOURCE_CACHE="$ZSOFT_ROOT/.cache/sources"
readonly ENV_STAMP_NAME="bench-goal-plus-environment.json"

# Base directory for L1 off-worktree storage (source caches, relocated campaign
# output). Overridable via L1_STORAGE_ROOT so a machine can point it at a roomier
# disk; the default is a neutral, machine-independent path (no username / hash).
readonly L1_STORAGE_ROOT="${L1_STORAGE_ROOT:-/srv/zsoft-l1}"

# Disk-full relocation for campaign output.
#
# The local disk that hosts $WORK_DIR (typically "/") repeatedly fills to 99%
# during L1 runs, which starves docker and turns real cases into false
# INFRA_ERROR seed-preflight failures. When free space on the campaign filesystem
# drops below L1_CAMPAIGN_MIN_FREE_GIB, we relocate the campaign directory onto a
# roomier mounted disk (L1_CAMPAIGN_EXTERNAL_ROOT, default under L1_STORAGE_ROOT)
# and leave a symlink at $WORK_DIR/$CAMPAIGN_BASE so results stay directly
# visible in the current folder. Because the Python side resolves --campaign-dir
# with Path.absolute() (NOT .resolve()), the symlink keeps the runs/ prefix intact
# and the ensure_under(RUNS_ROOT) guard still passes.
readonly L1_CAMPAIGN_MIN_FREE_GIB="${L1_CAMPAIGN_MIN_FREE_GIB:-80}"
readonly L1_CAMPAIGN_EXTERNAL_ROOT="${L1_CAMPAIGN_EXTERNAL_ROOT:-$L1_STORAGE_ROOT/campaigns}"
CAMPAIGN_STORAGE_MODE="local"
CAMPAIGN_STORAGE_RESOLVED=""

MODE="all"
REQUESTED_CASE_ID=""
WALL_TIME_SECONDS=1800
LIVE_SEARCH_CONCURRENCY=4
CELL_CONCURRENCY=1
REPEAT_COUNT=1
MAX_INFRA_ATTEMPTS="${L1_MAX_INFRA_ATTEMPTS:-2}"
STATUS_INTERVAL_SECONDS="${L1_STATUS_INTERVAL_SECONDS:-60}"
# REA 逆向分析工具（沙箱内只读挂载 + workspace 内 rea-analysis Skill）。
# 开关与 detect 侧一致：L1_REA=1 时导出 BENCH_GOAL_PLUS_REA_ROOT，
# 由 Pi worker launcher 把 REA 包只读挂载到沙箱 /opt/rea，
# 并通过 BENCH_GOAL_PLUS_ZSOFT_L1_REA_TOOLS=1 让 adapter 复制 rea-analysis Skill。
readonly L1_REA_ENABLED="${L1_REA:-0}"
# Shared Knowledge Board(opt-in):开启后 bench 传 --shared-cache,执行期由
# shared_knowledge_template() 按 benchmark 导出 PoC 专用模板
# (target_facts / trigger_hypotheses / poc_verdicts,判定条目强制锚定已结算迭代)。
readonly L1_SHARED_KNOWLEDGE_ENABLED="${L1_SHARED_KNOWLEDGE:-0}"
readonly L1_REA_ROOT="${L1_REA_ROOT:-/home/tangzhiyun/workspace/rea}"
ACTIVE_HEARTBEAT_PID=""
SOURCE_CACHE_RESOLVED=""
SOURCE_CACHE_STORAGE_MODE="unconfigured"

usage() {
    cat <<'USAGE'
用法:
  ./run_all_l1_cases.sh
  ./run_all_l1_cases.sh --one
  ./run_all_l1_cases.sh --case-id CASE_ID
  ./run_all_l1_cases.sh --case-id CASE_ID -T 3600 -K 2 -C 1 -R 3
  ./run_all_l1_cases.sh --list-cases

选项:
  --one                         自动选择一个 case 做真实验证
  --case-id CASE_ID             只运行指定 case
  --list-cases                  列出当前 ZSoft checkout 中全部可用 CASE_ID 后退出
  -T, --wall-time-seconds       T: 每个 search 的墙钟预算，默认 1800，当前最小 1260
  -K, --live-search-concurrency
                                K: 每个 task cell 内部 subagent 数，默认 4
  -C, --cell-concurrency        C: 同时运行的 task cell 数，默认 1（当前仅支持 1）
  -R, --repeats                 R: 每个 case 的独立串行 campaign 数，使用 seed 1..R，默认 1
  -A, --max-infra-attempts      基础设施失败时的最大尝试次数，默认 2
  -h, --help                    显示帮助

CASE_ID 示例（完整列表请使用 --list-cases）:
  sample-asan-crash
  civetweb-002-websocket-deflate-oob
  jiuwenswarm-001-tools-add-stdio-rce
  libxml2-002-relaxng-pushcdata-oob
  linux-rxrpc-006-ackall-null-txqueue
  umdk-001-get-lock-request-preauth-crash

可选环境变量:
  L1_PROVIDER                   模型提供方: deepseek(默认) 或 glm
  L1_MODEL                      覆盖默认模型(deepseek→DeepSeek-V4.1-Flash-1017, glm→glm-4.6)
  L1_PI_API                     Pi API 协议: openai-completions(默认) 或 anthropic-messages
  DEEPSEEK_API_KEY / GLM_API_KEY   按 provider 二选一,已设置时不再交互读取
  L1_REA                        1 时在 worker 沙箱内启用 rea CLI（默认 0）
  L1_SHARED_KNOWLEDGE           1 时开启 Shared Knowledge Board（PoC 模板，默认 0；
                                需 BENCH_GOAL_PLUS_SOURCE_DIR 指向含 PR !86 的检出）
  L1_REA_ROOT                   REA 包根目录路径（默认 /home/tangzhiyun/workspace/rea）
  L1_SOURCE_CACHE_DIR           明确指定的额外 .cache/sources 目录；不会自动扫描磁盘
  L1_STORAGE_ROOT               prepare 脚本使用的明确外置存储根目录
  L1_MAX_INFRA_ATTEMPTS         基础设施失败最大尝试次数
  L1_STATUS_INTERVAL_SECONDS    实验运行心跳间隔，默认 60 秒
  L1_CAMPAIGN_MIN_FREE_GIB      本地磁盘可用空间低于该 GiB 时把 campaign 输出迁到外置磁盘，默认 80
  L1_STORAGE_ROOT              外置存储根目录（源码缓存/迁出的 campaign），默认 /srv/zsoft-l1
  L1_CAMPAIGN_BASE              campaign 输出的基路径(默认 runs/benchmark-campaigns-v3;
                                必须以 runs/ 开头;跨盘请用软链接,见脚本头部注释)
  L1_CAMPAIGN_EXTERNAL_ROOT     磁盘不足时的外置 campaign 目录，默认 $L1_STORAGE_ROOT/campaigns/... ；会在当前目录留软链接

环境复用:
  仅复用通过版本指纹校验的 L1 Docker judge 环境。
  锁定源码缓存按 source-lock 元数据单独校验和复用。
  campaign、实验 workspace 和 Goal Plus 状态始终重新创建。

断网契约:
  每个 Pi worker 在 bwrap 沙箱内运行（仅 loopback 网络 + 桩 DNS），唯一
  出口是 api-only LLM 中继，模型无法访问外部源码仓库。运行前脚本会校验
  bwrap/node 可用；跑后可用 scripts/audit_network_access.py 复核出网记录。
  设置 BENCH_GOAL_PLUS_SOURCE_DIR 可改用外部 goal-plus 检出（与 detect 相同
  做法,如 /data/disk/tangzhiyun/muyuan/plugins/goal-plus）;BENCH_GOAL_PLUS_
  EXPECTED_REF 为可选 pin,设置时 HEAD 必须精确匹配。
USAGE
}

die() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

# Free space (in whole GiB) on the filesystem that would host $1.
# Walks up to the nearest existing ancestor so it works before the dir exists.
fs_free_gib() {
    local target="$1"
    while [[ ! -e "$target" && "$target" != "/" ]]; do
        target="$(dirname "$target")"
    done
    local kib
    kib="$(df -Pk "$target" 2>/dev/null | awk 'NR==2 {print $4}')" || return 1
    [[ -n "$kib" ]] || return 1
    printf '%d' "$((kib / 1024 / 1024))"
}

# If the local campaign filesystem is low on space, relocate campaign output to a
# roomier external disk and leave a symlink at $WORK_DIR/$CAMPAIGN_BASE so the
# results stay visible in the current folder. Idempotent and safe to re-run.
ensure_campaign_storage() {
    local local_base="$WORK_DIR/$CAMPAIGN_BASE"

    # Already relocated in a previous run: honor the existing symlink.
    if [[ -L "$local_base" ]]; then
        CAMPAIGN_STORAGE_MODE="external_symlink"
        CAMPAIGN_STORAGE_RESOLVED="$(realpath "$local_base" 2>/dev/null || readlink -f "$local_base")"
        status_message \
            "CAMPAIGN_STORAGE mode=external_symlink target=$CAMPAIGN_STORAGE_RESOLVED reason=existing_symlink"
        return 0
    fi

    local free_gib
    free_gib="$(fs_free_gib "$local_base")" || free_gib=""
    CAMPAIGN_STORAGE_RESOLVED="$(realpath "$local_base" 2>/dev/null || printf '%s' "$local_base")"

    if [[ -n "$free_gib" && "$free_gib" -ge "$L1_CAMPAIGN_MIN_FREE_GIB" ]]; then
        CAMPAIGN_STORAGE_MODE="local"
        status_message \
            "CAMPAIGN_STORAGE mode=local free_gib=$free_gib threshold_gib=$L1_CAMPAIGN_MIN_FREE_GIB path=$CAMPAIGN_STORAGE_RESOLVED"
        return 0
    fi

    # Low (or unknown) local space -> relocate to the external disk.
    local ext_root="$L1_CAMPAIGN_EXTERNAL_ROOT"
    if ! mkdir -p "$ext_root" 2>/dev/null; then
        status_message \
            "CAMPAIGN_STORAGE mode=local_forced free_gib=${free_gib:-unknown} reason=external_root_unwritable path=$ext_root"
        CAMPAIGN_STORAGE_MODE="local"
        return 0
    fi

    local ext_free_gib
    ext_free_gib="$(fs_free_gib "$ext_root")" || ext_free_gib=""
    if [[ -n "$ext_free_gib" && -n "$free_gib" && "$ext_free_gib" -le "$free_gib" ]]; then
        status_message \
            "CAMPAIGN_STORAGE mode=local_forced free_gib=$free_gib ext_free_gib=$ext_free_gib reason=external_not_roomier"
        CAMPAIGN_STORAGE_MODE="local"
        return 0
    fi

    # Migrate any pre-existing local campaign data onto the external disk so no
    # results are lost, then replace the local dir with a symlink.
    if [[ -d "$local_base" && ! -L "$local_base" ]]; then
        status_message "CAMPAIGN_STORAGE migrating existing local data -> $ext_root"
        if ! cp -a "$local_base/." "$ext_root/" 2>/dev/null; then
            status_message \
                "CAMPAIGN_STORAGE mode=local_forced reason=migration_failed path=$local_base"
            CAMPAIGN_STORAGE_MODE="local"
            return 0
        fi
        local backup="${local_base}.pre-relocate.$RUN_STAMP"
        mv "$local_base" "$backup" \
            || die "无法移动本地 campaign 目录以建立软链接: $local_base"
        rm -rf -- "$backup" 2>/dev/null || true
    fi

    mkdir -p "$(dirname "$local_base")"
    ln -sfn "$ext_root" "$local_base" \
        || die "无法建立 campaign 软链接: $local_base -> $ext_root"

    CAMPAIGN_STORAGE_MODE="external_symlink"
    CAMPAIGN_STORAGE_RESOLVED="$(realpath "$local_base" 2>/dev/null || printf '%s' "$ext_root")"
    status_message \
        "CAMPAIGN_STORAGE mode=external_symlink free_gib=${free_gib:-unknown} ext_free_gib=${ext_free_gib:-unknown} target=$CAMPAIGN_STORAGE_RESOLVED symlink=$local_base"
}

while (($# > 0)); do
    case "$1" in
        --one)
            [[ "$MODE" == "all" && -z "$REQUESTED_CASE_ID" ]] \
                || die "--one 不能重复，也不能和 --case-id 或 --list-cases 同时使用"
            MODE="one"
            shift
            ;;
        --case-id)
            (($# >= 2)) || die "--case-id 缺少参数"
            [[ "$MODE" == "all" && -z "$REQUESTED_CASE_ID" ]] \
                || die "--case-id 不能重复，也不能和 --one 或 --list-cases 同时使用"
            MODE="case"
            REQUESTED_CASE_ID="$2"
            shift 2
            ;;
        --list-cases)
            [[ "$MODE" == "all" && -z "$REQUESTED_CASE_ID" ]] \
                || die "--list-cases 不能重复，也不能和 --one 或 --case-id 同时使用"
            MODE="list"
            shift
            ;;
        -A|--max-infra-attempts)
            (($# >= 2)) || die "$1 缺少参数"
            MAX_INFRA_ATTEMPTS="$2"
            shift 2
            ;;
        -T|--wall-time-seconds)
            (($# >= 2)) || die "$1 缺少参数"
            WALL_TIME_SECONDS="$2"
            shift 2
            ;;
        -K|--live-search-concurrency)
            (($# >= 2)) || die "$1 缺少参数"
            LIVE_SEARCH_CONCURRENCY="$2"
            shift 2
            ;;
        -C|--cell-concurrency)
            (($# >= 2)) || die "$1 缺少参数"
            CELL_CONCURRENCY="$2"
            shift 2
            ;;
        -R|--repeats)
            (($# >= 2)) || die "$1 缺少参数"
            REPEAT_COUNT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "未知参数: $1"
            ;;
    esac
done

cd "$WORK_DIR" || die "无法进入工作目录: $WORK_DIR"

for required in find sed sort; do
    require_command "$required"
done
[[ -d "$TASKS_DIR" ]] || die "L1 tasks 目录不存在: $TASKS_DIR"

mapfile -t ALL_CASE_IDS < <(
    find "$TASKS_DIR" -mindepth 2 -maxdepth 2 \
        -type f -name task.json -printf '%h\n' |
        sed 's#.*/##' |
        LC_ALL=C sort
)

((${#ALL_CASE_IDS[@]} > 0)) || die "未找到任何 L1 case"
for discovered_case_id in "${ALL_CASE_IDS[@]}"; do
    [[ "$discovered_case_id" =~ ^[a-z0-9][a-z0-9-]*$ ]] \
        || die "发现不安全的 L1 case ID: $discovered_case_id"
done

if [[ "$MODE" == "list" ]]; then
    printf '可用 L1 CASE_ID（%d 个）:\n' "${#ALL_CASE_IDS[@]}"
    printf '  %s\n' "${ALL_CASE_IDS[@]}"
    exit 0
fi

[[ "$EXPECTED_CASES" =~ ^[1-9][0-9]*$ ]] \
    || die "L1_EXPECTED_CASES 必须是正整数"
[[ "$WALL_TIME_SECONDS" =~ ^[1-9][0-9]*$ ]] \
    || die "T (--wall-time-seconds) 必须是正整数"
((WALL_TIME_SECONDS >= 1260)) \
    || die "T (--wall-time-seconds) 必须至少为 1260 秒（1200 秒 worker 预算 + 60 秒 closeout）"
[[ "$LIVE_SEARCH_CONCURRENCY" =~ ^[1-9][0-9]*$ ]] \
    || die "K (--live-search-concurrency) 必须是正整数"
[[ "$CELL_CONCURRENCY" =~ ^[1-9][0-9]*$ ]] \
    || die "C (--cell-concurrency) 必须是正整数"
((CELL_CONCURRENCY == 1)) \
    || die "当前 zsoft-l1 common-matrix runner 尚未支持 C>1；--cell-concurrency 必须为 1"
[[ "$REPEAT_COUNT" =~ ^[1-9][0-9]*$ ]] \
    || die "R (--repeats) 必须是正整数"
[[ "$MAX_INFRA_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] \
    || die "最大基础设施尝试次数必须是正整数"
[[ "$STATUS_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] \
    || die "L1_STATUS_INTERVAL_SECONDS 必须是正整数"
((STATUS_INTERVAL_SECONDS >= 10)) \
    || die "L1_STATUS_INTERVAL_SECONDS 不能小于 10 秒"

for required in awk cp date docker flock git jq mktemp mv realpath rm sha256sum sleep stat tee; do
    require_command "$required"
done
[[ -x "$PYTHON" ]] || die "Python 环境不存在: $PYTHON"
((${#ALL_CASE_IDS[@]} == EXPECTED_CASES)) \
    || die "L1 case 数量为 ${#ALL_CASE_IDS[@]}，预期为 $EXPECTED_CASES；请先确认任务集版本"

mkdir -p "$WORK_DIR/.tmp"
exec 9>"$WORK_DIR/.tmp/run-l1-cases.lock"
flock -n 9 || die "已有另一个 L1 批量或验证脚本正在运行"

is_known_case() {
    local requested="$1"
    local case_id
    for case_id in "${ALL_CASE_IDS[@]}"; do
        [[ "$case_id" == "$requested" ]] && return 0
    done
    return 1
}

ENV_REUSE_REASON="not_checked"

benchmark_inputs_clean() {
    local case_id="$1"
    local dirty=""

    dirty="$(git -C "$ZSOFT_CHECKOUT" status --porcelain --untracked-files=all -- \
        "benchmarks/vulnerability/zsoft-l1/tasks/$case_id" \
        'benchmarks/vulnerability/zsoft-l1/src' \
        'benchmarks/vulnerability/zsoft-l1/source-locks' 2>/dev/null)" \
        || return 1
    [[ -z "$dirty" ]]
}

# Emit the two judge image names from image-refs.json into $vuln_ref / $fix_ref.
# Accepts all schema variants used by the benchmark tasks:
#   {"vuln_image": ..., "fix_image": ...}                   (legacy prepare)
#   {"target_image": ..., "driver_image": ...}              (prepare_source_service_images)
#   {"runner_image": ..., "vuln_artifact_dir": ..., "fix_artifact_dir": ...} (prepare_kernel_task)
# For the kernel schema both judge sides share the single runner_image; the
# artifact dirs are container mount paths, not image names.
emit_image_refs() {
    local refs="$1"
    local -a lines=()
    mapfile -t lines < <(jq -r '
        if has("vuln_image") then .vuln_image
        elif has("target_image") then .target_image
        else .runner_image end,
        if has("fix_image") then .fix_image
        elif has("driver_image") then .driver_image
        else .runner_image end
    ' "$refs" 2>/dev/null) || true
    ((${#lines[@]} == 2)) || return 1
    vuln_ref="${lines[0]}"
    fix_ref="${lines[1]}"
    [[ -n "$vuln_ref" && -n "$fix_ref" ]]
}

# Collect the docker image ids for $vuln_ref / $fix_ref into $image_ids.
# Both judge sides always map to exactly two ids; for the kernel schema the
# sides share one runner_image, so the same id is recorded for both.
collect_image_ids() {
    mapfile -t image_ids < <(
        docker image inspect --format '{{.Id}}' "$vuln_ref" "$fix_ref" 2>/dev/null
    )
    ((${#image_ids[@]} == 2)) || return 1
    if [[ "$vuln_ref" == "$fix_ref" ]]; then
        image_ids[1]="${image_ids[0]}"
    fi
}

case_environment_reusable() {
    local case_id="$1"
    local task_state="$STATE_DIR/$case_id"
    local refs="$task_state/image-refs.json"
    local stamp="$task_state/$ENV_STAMP_NAME"
    local benchmark_commit=""
    local task_tree=""
    local framework_tree=""
    local source_locks_tree=""
    local refs_sha256=""
    local vuln_ref=""
    local fix_ref=""
    local -a image_ids=()

    ENV_REUSE_REASON="image_refs_missing"
    [[ -f "$refs" && ! -L "$refs" ]] || return 1
    ENV_REUSE_REASON="image_refs_invalid"
    jq -e '
        type == "object" and
        (
            (keys | sort) == ["fix_image", "vuln_image"]
            or (keys | sort) == ["driver_image", "target_image"]
            or (keys | sort) == ["fix_artifact_dir", "runner_image", "vuln_artifact_dir"]
        ) and
        ((.fix_image? // .driver_image? // .runner_image? // "") | type == "string" and length > 0) and
        ((.vuln_image? // .target_image? // .runner_image? // "") | type == "string" and length > 0)
    ' "$refs" >/dev/null 2>&1 || return 1
    emit_image_refs "$refs" || return 1
    ENV_REUSE_REASON="docker_images_missing"
    collect_image_ids || return 1

    ENV_REUSE_REASON="provenance_stamp_missing"
    [[ -f "$stamp" && ! -L "$stamp" ]] || return 1
    ENV_REUSE_REASON="benchmark_inputs_dirty"
    benchmark_inputs_clean "$case_id" || return 1
    benchmark_commit="$(git -C "$ZSOFT_CHECKOUT" rev-parse HEAD 2>/dev/null)" \
        || { ENV_REUSE_REASON="benchmark_commit_unavailable"; return 1; }
    task_tree="$(git -C "$ZSOFT_CHECKOUT" rev-parse \
        "HEAD:benchmarks/vulnerability/zsoft-l1/tasks/$case_id" 2>/dev/null)" \
        || { ENV_REUSE_REASON="task_tree_unavailable"; return 1; }
    framework_tree="$(git -C "$ZSOFT_CHECKOUT" rev-parse \
        'HEAD:benchmarks/vulnerability/zsoft-l1/src' 2>/dev/null)" \
        || { ENV_REUSE_REASON="framework_tree_unavailable"; return 1; }
    source_locks_tree="$(git -C "$ZSOFT_CHECKOUT" rev-parse \
        'HEAD:benchmarks/vulnerability/zsoft-l1/source-locks' 2>/dev/null)" \
        || { ENV_REUSE_REASON="source_locks_tree_unavailable"; return 1; }
    refs_sha256="$(sha256sum "$refs" | awk '{print $1}')" \
        || { ENV_REUSE_REASON="image_refs_hash_failed"; return 1; }

    ENV_REUSE_REASON="provenance_mismatch"
    jq -e \
        --arg case_id "$case_id" \
        --arg benchmark_commit "$benchmark_commit" \
        --arg task_tree "$task_tree" \
        --arg framework_tree "$framework_tree" \
        --arg source_locks_tree "$source_locks_tree" \
        --arg refs_sha256 "$refs_sha256" \
        --arg vuln_ref "$vuln_ref" \
        --arg fix_ref "$fix_ref" \
        --arg vuln_id "${image_ids[0]}" \
        --arg fix_id "${image_ids[1]}" '
        .schema_version == 1 and
        .task_id == $case_id and
        .benchmark_commit == $benchmark_commit and
        .task_tree == $task_tree and
        .framework_tree == $framework_tree and
        .source_locks_tree == $source_locks_tree and
        .image_refs_sha256 == $refs_sha256 and
        .images.vuln.ref == $vuln_ref and
        .images.vuln.id == $vuln_id and
        .images.fix.ref == $fix_ref and
        .images.fix.id == $fix_id
    ' "$stamp" >/dev/null 2>&1 || return 1

    ENV_REUSE_REASON="reusable"
    return 0
}

write_environment_stamp() {
    local case_id="$1"
    local prepared_by="$2"
    local task_state="$STATE_DIR/$case_id"
    local refs="$task_state/image-refs.json"
    local stamp="$task_state/$ENV_STAMP_NAME"
    local temporary=""
    local benchmark_commit=""
    local task_tree=""
    local framework_tree=""
    local source_locks_tree=""
    local refs_sha256=""
    local vuln_ref=""
    local fix_ref=""
    local -a image_ids=()

    [[ -f "$refs" && ! -L "$refs" ]] || return 1
    jq -e '
        type == "object" and
        (
            (keys | sort) == ["fix_image", "vuln_image"]
            or (keys | sort) == ["driver_image", "target_image"]
            or (keys | sort) == ["fix_artifact_dir", "runner_image", "vuln_artifact_dir"]
        ) and
        ((.fix_image? // .driver_image? // .runner_image? // "") | type == "string" and length > 0) and
        ((.vuln_image? // .target_image? // .runner_image? // "") | type == "string" and length > 0)
    ' "$refs" >/dev/null 2>&1 || return 1
    benchmark_inputs_clean "$case_id" || return 1
    emit_image_refs "$refs" || return 1
    collect_image_ids || return 1
    benchmark_commit="$(git -C "$ZSOFT_CHECKOUT" rev-parse HEAD)" || return 1
    task_tree="$(git -C "$ZSOFT_CHECKOUT" rev-parse \
        "HEAD:benchmarks/vulnerability/zsoft-l1/tasks/$case_id")" || return 1
    framework_tree="$(git -C "$ZSOFT_CHECKOUT" rev-parse \
        'HEAD:benchmarks/vulnerability/zsoft-l1/src')" || return 1
    source_locks_tree="$(git -C "$ZSOFT_CHECKOUT" rev-parse \
        'HEAD:benchmarks/vulnerability/zsoft-l1/source-locks')" || return 1
    refs_sha256="$(sha256sum "$refs" | awk '{print $1}')" || return 1
    temporary="$(mktemp "$task_state/.${ENV_STAMP_NAME}.XXXXXX")" || return 1

    if jq -n \
        --arg task_id "$case_id" \
        --arg benchmark_commit "$benchmark_commit" \
        --arg task_tree "$task_tree" \
        --arg framework_tree "$framework_tree" \
        --arg source_locks_tree "$source_locks_tree" \
        --arg refs_sha256 "$refs_sha256" \
        --arg vuln_ref "$vuln_ref" \
        --arg fix_ref "$fix_ref" \
        --arg vuln_id "${image_ids[0]}" \
        --arg fix_id "${image_ids[1]}" \
        --arg prepared_at "$(date --iso-8601=seconds)" \
        --arg prepared_by "$prepared_by" '
        {
            schema_version: 1,
            task_id: $task_id,
            benchmark_commit: $benchmark_commit,
            task_tree: $task_tree,
            framework_tree: $framework_tree,
            source_locks_tree: $source_locks_tree,
            image_refs_sha256: $refs_sha256,
            images: {
                vuln: {ref: $vuln_ref, id: $vuln_id},
                fix: {ref: $fix_ref, id: $fix_id}
            },
            prepared_at: $prepared_at,
            prepared_by: $prepared_by
        }
    ' >"$temporary"; then
        mv "$temporary" "$stamp"
        return 0
    fi
    case "$temporary" in
        "$task_state"/."$ENV_STAMP_NAME".*) rm -f -- "$temporary" ;;
    esac
    return 1
}

select_one_case() {
    local case_id

    # Prefer a case whose current-version judge environment is reusable.
    for case_id in "${ALL_CASE_IDS[@]}"; do
        if case_environment_reusable "$case_id"; then
            printf '%s\n' "$case_id"
            return 0
        fi
    done

    # Otherwise prefer a self-contained case that needs no source download.
    for case_id in "${ALL_CASE_IDS[@]}"; do
        if [[ ! -f "$TASKS_DIR/$case_id/private/source-manifest.json" ]]; then
            printf '%s\n' "$case_id"
            return 0
        fi
    done

    printf '%s\n' "${ALL_CASE_IDS[0]}"
}

declare -a CASE_IDS=()
case "$MODE" in
    all)
        CASE_IDS=("${ALL_CASE_IDS[@]}")
        ;;
    one)
        CASE_IDS=("$(select_one_case)")
        ;;
    case)
        is_known_case "$REQUESTED_CASE_ID" \
            || die "未知 L1 case: $REQUESTED_CASE_ID"
        CASE_IDS=("$REQUESTED_CASE_ID")
        ;;
esac

readonly TOTAL_TASK_CELLS=$((${#CASE_IDS[@]} * REPEAT_COUNT))

readonly L1_PROVIDER="${L1_PROVIDER:-deepseek}"
case "$L1_PROVIDER" in
    deepseek)
        PROVIDER_KEY_ENV="DEEPSEEK_API_KEY"
        PROVIDER_BASE_ENV="DEEPSEEK_BASE_URL"
        # 与 detect 完全一致:走 113 网关(pool key 只放行该网关的模型池),
        # 官方 api.deepseek.com 会以 key_model_access_denied 拒绝此 key。
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
        die "L1_PROVIDER 只能是 deepseek 或 glm(当前: $L1_PROVIDER)"
        ;;
esac
readonly L1_MODEL="${L1_MODEL:-$PROVIDER_MODEL_DEFAULT}"
readonly PROVIDER_KEY_ENV PROVIDER_BASE_ENV
readonly L1_PI_API="${L1_PI_API:-openai-completions}"
case "$L1_PI_API" in
    openai-completions|anthropic-messages) ;;
    *) die "L1_PI_API 只能是 openai-completions 或 anthropic-messages(当前: $L1_PI_API)" ;;
esac

if [[ -z "${!PROVIDER_KEY_ENV:-}" ]]; then
    [[ -t 0 ]] || die "非交互运行前必须先 export $PROVIDER_KEY_ENV"
    read -rsp "请输入 $PROVIDER_KEY_ENV: " PROVIDER_KEY_VALUE
    printf '\n'
    export "$PROVIDER_KEY_ENV=$PROVIDER_KEY_VALUE"
fi
[[ -n "${!PROVIDER_KEY_ENV:-}" ]] || die "$PROVIDER_KEY_ENV 不能为空"
export "$PROVIDER_BASE_ENV=${!PROVIDER_BASE_ENV:-$PROVIDER_BASE_DEFAULT}"
unset OPENAI_BASE_URL OPENAI_API_BASE_URL OPENAI_API_BASE
# 只保留所选 provider 的 base URL,避免断网中继因检测到多个上游而拒绝启动
if [[ "$L1_PROVIDER" == "glm" ]]; then
    unset DEEPSEEK_BASE_URL
else
    unset GLM_BASE_URL
fi

# Shared Knowledge Board 依赖含 PR !86 的 goal-plus 运行时;默认受管检出
# (third_party/muyuan, 2026-09-24) 早于 board 合入,静默回退会拿到旧共享行为。
if [[ "$L1_SHARED_KNOWLEDGE_ENABLED" == "1" ]]; then
    if [[ -z "${BENCH_GOAL_PLUS_SOURCE_DIR:-}" ]]; then
        die "L1_SHARED_KNOWLEDGE=1 需要含 knowledge board 的 goal-plus 运行时:请 export BENCH_GOAL_PLUS_SOURCE_DIR=<检出>(如 /data/disk/tangzhiyun/muyuan/plugins/goal-plus,含 PR !86)"
    fi
    [[ -f "$BENCH_GOAL_PLUS_SOURCE_DIR/src/goal_plus/domain/shared_knowledge.py" ]] \
        || die "L1_SHARED_KNOWLEDGE=1 但检出不含 shared knowledge board(缺 src/goal_plus/domain/shared_knowledge.py): $BENCH_GOAL_PLUS_SOURCE_DIR"
fi

# REA 工具开关：启用时校验包完整性并导出两个环境变量——
#   BENCH_GOAL_PLUS_REA_ROOT            Pi worker launcher 据此把 REA 只读挂载到沙箱 /opt/rea
#   BENCH_GOAL_PLUS_ZSOFT_L1_REA_TOOLS  adapter 据此在 workspace 物化 .agents/skills/rea-analysis
# 默认（L1_REA=0）两个变量都不导出，保持既有 campaign 契约不变。
if [[ "$L1_REA_ENABLED" == "1" ]]; then
    [[ -f "$L1_REA_ROOT/scripts/rea.mjs" && -d "$L1_REA_ROOT/node_modules" ]] \
        || die "L1_REA=1 但 REA 包不完整（需要 scripts/rea.mjs 和 node_modules）: $L1_REA_ROOT"
    export BENCH_GOAL_PLUS_REA_ROOT="$L1_REA_ROOT"
    export BENCH_GOAL_PLUS_ZSOFT_L1_REA_TOOLS=1
else
    unset BENCH_GOAL_PLUS_REA_ROOT BENCH_GOAL_PLUS_ZSOFT_L1_REA_TOOLS
fi

cleanup_runtime() {
    if [[ -n "$ACTIVE_HEARTBEAT_PID" ]]; then
        kill "$ACTIVE_HEARTBEAT_PID" >/dev/null 2>&1 || true
        wait "$ACTIVE_HEARTBEAT_PID" 2>/dev/null || true
        ACTIVE_HEARTBEAT_PID=""
    fi
    unset "$PROVIDER_KEY_ENV"
    unset BENCH_GOAL_PLUS_REA_ROOT BENCH_GOAL_PLUS_ZSOFT_L1_REA_TOOLS
}

handle_interrupt() {
    printf '\n收到中断信号；不会自动重启当前 campaign。\n' >&2
    exit 130
}

trap cleanup_runtime EXIT
trap handle_interrupt INT TERM HUP

readonly RUN_STAMP="$(date +%Y%m%d-%H%M%S)-$$"
readonly LOG_DIR="$WORK_DIR/logs/l1_cases_$RUN_STAMP"
readonly SUMMARY_FILE="$LOG_DIR/summary.txt"
readonly PROGRESS_FILE="$LOG_DIR/progress.log"
mkdir -p "$LOG_DIR/cases" "$CURRENT_SOURCE_CACHE"
: >"$PROGRESS_FILE"

status_message() {
    local message="$*"
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$message" \
        | tee -a "$PROGRESS_FILE"
}

case_status() {
    local log_file="$1"
    shift
    local message="$*"
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$message" \
        | tee -a "$PROGRESS_FILE" "$log_file"
}

[[ -d "$CURRENT_SOURCE_CACHE" ]] \
    || die "L1 锁定源码缓存目录不存在或软链接已失效: $CURRENT_SOURCE_CACHE"
SOURCE_CACHE_RESOLVED="$(realpath "$CURRENT_SOURCE_CACHE")" \
    || die "无法解析 L1 锁定源码缓存路径: $CURRENT_SOURCE_CACHE"
if [[ -L "$CURRENT_SOURCE_CACHE" ]]; then
    SOURCE_CACHE_STORAGE_MODE="external_symlink"
else
    SOURCE_CACHE_STORAGE_MODE="local"
fi

{
    printf 'L1 Case 运行汇总\n'
    printf '开始时间: %s\n' "$(date --iso-8601=seconds)"
    printf '配置: provider=%s model=%s reasoning=high T=%d K=%d C=%d R=%d\n' \
        "$L1_PROVIDER" "$L1_MODEL" "$WALL_TIME_SECONDS" \
        "$LIVE_SEARCH_CONCURRENCY" \
        "$CELL_CONCURRENCY" "$REPEAT_COUNT"
    printf '运行心跳间隔: %s 秒\n' "$STATUS_INTERVAL_SECONDS"
    printf '锁定源码缓存: %s\n' "$CURRENT_SOURCE_CACHE"
    printf '锁定源码缓存实际位置: %s\n' "$SOURCE_CACHE_RESOLVED"
    printf '锁定源码缓存模式: %s\n' "$SOURCE_CACHE_STORAGE_MODE"
    printf 'worker 断网: bwrap loopback-only + api-only LLM 中继\n'
    printf 'Shared Knowledge Board: %s\n' "$([[ "$L1_SHARED_KNOWLEDGE_ENABLED" == "1" ]] && printf 'enabled(PoC 模板)' || printf 'disabled')"
    printf 'goal-plus 运行时来源: %s\n' \
        "$([[ -n "${BENCH_GOAL_PLUS_SOURCE_DIR:-}" ]] && printf 'external %s @ %s' "${BENCH_GOAL_PLUS_EXPECTED_REF:-?}" "$BENCH_GOAL_PLUS_SOURCE_DIR" || printf 'managed checkout (third_party/muyuan)')"
    printf 'REA 工具: %s\n' "$([[ "$L1_REA_ENABLED" == "1" ]] && printf 'enabled root=%s' "$L1_REA_ROOT" || printf 'disabled')"
    printf '计划 case 数: %d\n' "${#CASE_IDS[@]}"
    printf '计划 task cell 数: %d\n' "$TOTAL_TASK_CELLS"
    printf 'case_id\tseed\tstatus\tattempts\tcampaign_id\treason\tcase_log\n'
} >"$SUMMARY_FILE"

status_message "INIT work_dir=$WORK_DIR"
status_message "INIT log_dir=$LOG_DIR progress_file=$PROGRESS_FILE"
status_message \
    "INIT l1_state_dir=$STATE_DIR source_cache=$CURRENT_SOURCE_CACHE source_cache_resolved=$SOURCE_CACHE_RESOLVED storage_mode=$SOURCE_CACHE_STORAGE_MODE"
status_message \
    "INIT rea_tools enabled=$L1_REA_ENABLED root=$L1_REA_ROOT"
status_message \
    "INIT shared_knowledge enabled=$L1_SHARED_KNOWLEDGE_ENABLED template=poc(target_facts/trigger_hypotheses/poc_verdicts)"
status_message \
    "CONFIG provider=$L1_PROVIDER model=$L1_MODEL api=$L1_PI_API reasoning=high T=$WALL_TIME_SECONDS K=$LIVE_SEARCH_CONCURRENCY C=$CELL_CONCURRENCY R=$REPEAT_COUNT"
status_message \
    "QUEUE cases=${#CASE_IDS[@]} task_cells=$TOTAL_TASK_CELLS execution=serial"

# —— worker 断网契约预检（与 zsoft-detect 相同机制，参考 run_all_detect_cases.sh）——
# goal-plus-pi 的每个 Pi worker 都由共享 launcher 放入 bwrap 沙箱：
#   独立 network namespace（仅 loopback）+ 桩 DNS；唯一出口是 api-only LLM
#   中继（沙箱内 127.0.0.1:8787 → 私有 unix socket → 宿主 → 单一模型 API
#   上游）。模型因此无法访问 GitHub 等外部站点，也就无法直接检索上游源码
#   仓库或现成 PoC。manifest 中的 network_isolation=loopback-relay-api-only
#   标注该契约；跑后可用 scripts/audit_network_access.py 复核 worker 出网。
# 下面三段预检保证该机制在本机成立，失败时立刻终止而不是带网运行。

# 启动前校验外部 goal-plus 检出停留在期望 ref 上(如共享平面组件分支),
# 避免静默回退到不含 worker 沙箱/断网中继契约的默认运行时。
# 启动前校验外部 goal-plus 检出身份。EXPECTED_REF 为可选 pin：
# 设置时 HEAD 必须精确匹配（可复现实验）；未设置时直接使用当前 HEAD，
# 下游 upstreams 层仍会校验工作树干净并锁定 prepare↔run 一致性。
if [[ -n "${BENCH_GOAL_PLUS_SOURCE_DIR:-}" ]]; then
    gp_head="$(git -C "$BENCH_GOAL_PLUS_SOURCE_DIR" rev-parse HEAD 2>/dev/null)" \
        || die "BENCH_GOAL_PLUS_SOURCE_DIR 不是可用的 git 检出: $BENCH_GOAL_PLUS_SOURCE_DIR"
    if [[ -n "${BENCH_GOAL_PLUS_EXPECTED_REF:-}" ]]; then
        gp_want="$(git -C "$BENCH_GOAL_PLUS_SOURCE_DIR" rev-parse --verify --end-of-options "${BENCH_GOAL_PLUS_EXPECTED_REF}^{commit}" 2>/dev/null)" \
            || die "goal-plus 检出中找不到 ref: $BENCH_GOAL_PLUS_EXPECTED_REF"
        if [[ "$gp_head" != "$gp_want" ]]; then
            gp_branch="$(git -C "$BENCH_GOAL_PLUS_SOURCE_DIR" branch --show-current 2>/dev/null || true)"
            die "goal-plus 检出 HEAD 与 BENCH_GOAL_PLUS_EXPECTED_REF=$BENCH_GOAL_PLUS_EXPECTED_REF 不一致(当前: ${gp_branch:-detached} $gp_head)。二选一: 精确复现请 git -C "$BENCH_GOAL_PLUS_SOURCE_DIR" checkout $BENCH_GOAL_PLUS_EXPECTED_REF;直接使用当前 HEAD 请 unset BENCH_GOAL_PLUS_EXPECTED_REF"
        fi
        status_message "GOAL_PLUS_SOURCE dir=$BENCH_GOAL_PLUS_SOURCE_DIR ref=$BENCH_GOAL_PLUS_EXPECTED_REF commit=$gp_head"
    else
        status_message "GOAL_PLUS_SOURCE dir=$BENCH_GOAL_PLUS_SOURCE_DIR ref=<current HEAD> commit=$gp_head"
    fi
else
    status_message \
        "GOAL_PLUS_SOURCE mode=managed_checkout(third_party/muyuan;设置 BENCH_GOAL_PLUS_SOURCE_DIR 可改用外部检出,BENCH_GOAL_PLUS_EXPECTED_REF 可选 pin)"
fi

# 宿主机环境加固（无 systemd 用户会话的主机需要）：
# 1) Pi worker 的 Unix socket 要求私有的短路径 XDG_RUNTIME_DIR（本机无 /run/user/<uid>）
# 2) pi-shim 的 `#!/usr/bin/env python3` 必须解析到仓库锁定的 Python 3.12
#    （系统 python 3.9 无法运行 launcher 代码）
# 3) bwrap 沙箱内只能看到 /usr 与 runtime root；断网中继 llm-relay 与 pi 运行时
#    都是 `#!/usr/bin/env node`，沙箱内可用的现代 node 需以真实二进制形式放在
#    ~/.local/bin/node（v22，勿用符号链接——指向 ~/.nvm 的符号链接在沙箱内不可见）
if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
    export XDG_RUNTIME_DIR="$HOME/.xdg-runtime"
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"
fi
export PATH="$WORK_DIR/.bench-env/venv/bin:$HOME/.local/bin:$PATH"

command -v bwrap >/dev/null 2>&1 \
    || die "worker 断网沙箱需要 bwrap（当前 PATH 未找到；正确路径示例: ~/.local/bin/bwrap）"
node_path="$(command -v node 2>/dev/null || true)"
[[ -n "$node_path" ]] || die "worker 断网沙箱需要 node（llm-relay 与 pi 运行时均为 node 程序）"
node_real="$(readlink -f "$node_path")"
if [[ "$node_real" != /usr/* && "$node_real" != "$HOME"/.local/* ]]; then
    die "node 必须以真实二进制位于 /usr 或 ~/.local/bin（当前: $node_path -> $node_real）；nvm 符号链接在沙箱内不可见，断网中继无法启动。请复制真实二进制: cp \"$(readlink -f "$node_path")\" ~/.local/bin/node"
fi
status_message \
    "NETWORK_ISOLATION worker_sandbox=bwrap-loopback-only llm_egress=api-only-relay bwrap=$(command -v bwrap) node=$node_real"

ensure_campaign_storage

PREFLIGHT_LOG="$LOG_DIR/preflight.log"
preflight_started="$(date +%s)"
status_message "PREFLIGHT_START log=$PREFLIGHT_LOG"
if {
    "$PYTHON" scripts/bench.py catalog
    "$PYTHON" scripts/bench.py setup \
        --benchmark zsoft-l1 \
        --method goal-plus-pi \
        --model "$L1_MODEL" \
        --reasoning-effort high \
        --pi-provider-id "$L1_PROVIDER" \
        --pi-api "$L1_PI_API" \
        --pi-api-key-env "$PROVIDER_KEY_ENV" \
        --pi-api-base-env "$PROVIDER_BASE_ENV" \
        --skip-bootstrap \
        --skip-provision
} >"$PREFLIGHT_LOG" 2>&1; then
    status_message "PREFLIGHT_OK elapsed_seconds=$(($(date +%s) - preflight_started))"
else
    status_message "PREFLIGHT_FAILED elapsed_seconds=$(($(date +%s) - preflight_started)) log=$PREFLIGHT_LOG"
    die "运行前环境检查失败，详见 $PREFLIGHT_LOG"
fi

declare -a SOURCE_CACHE_CANDIDATES=()
SOURCE_CACHE_PATHS_CHECKED=0

add_source_cache_candidate() {
    local candidate="$1"
    local resolved=""
    local existing

    [[ -d "$candidate" ]] || return 0
    resolved="$(realpath "$candidate")" || return 0
    [[ "$resolved" != "$(realpath "$CURRENT_SOURCE_CACHE")" ]] || return 0
    for existing in "${SOURCE_CACHE_CANDIDATES[@]}"; do
        [[ "$existing" == "$resolved" ]] && return 0
    done
    SOURCE_CACHE_CANDIDATES+=("$resolved")
}

if [[ -n "${L1_SOURCE_CACHE_DIR:-}" ]]; then
    SOURCE_CACHE_PATHS_CHECKED=$((SOURCE_CACHE_PATHS_CHECKED + 1))
    add_source_cache_candidate "$L1_SOURCE_CACHE_DIR"
fi
if [[ -n "${L1_STORAGE_ROOT:-}" \
    && "$L1_STORAGE_ROOT" == /* \
    && "$L1_STORAGE_ROOT" != "/" ]]; then
    SOURCE_CACHE_PATHS_CHECKED=$((SOURCE_CACHE_PATHS_CHECKED + 1))
    add_source_cache_candidate "${L1_STORAGE_ROOT%/}/source-cache"
fi
for candidate in \
    "$L1_STORAGE_ROOT/source-cache" \
    "$CURRENT_SOURCE_CACHE"; do
    SOURCE_CACHE_PATHS_CHECKED=$((SOURCE_CACHE_PATHS_CHECKED + 1))
    add_source_cache_candidate "$candidate"
done
status_message \
    "SOURCE_CACHE_DISCOVERY mode=explicit_paths checked_roots=$SOURCE_CACHE_PATHS_CHECKED compatible_roots=${#SOURCE_CACHE_CANDIDATES[@]}"

cache_metadata_matches() {
    local cached_source="$1"
    local lock_file="$2"
    local metadata="$cached_source/.source-lock.json"

    [[ -f "$metadata" && -f "$lock_file" ]] || return 1
    jq -e --slurpfile source_lock "$lock_file" '
        ($source_lock | length) == 1 and
        .source_id == $source_lock[0].source_id and
        .kind == $source_lock[0].kind and
        .url == $source_lock[0].url and
        (.commit // null) == ($source_lock[0].commit // null) and
        (.ref // null) == ($source_lock[0].ref // null) and
        (.strip_prefix // null) == ($source_lock[0].strip_prefix // null)
    ' "$metadata" >/dev/null 2>&1
}

reuse_local_source_cache() {
    local case_id="$1"
    local log_file="$2"
    local manifest="$TASKS_DIR/$case_id/private/source-manifest.json"
    local source_id=""
    local lock_file=""
    local target=""
    local candidate=""
    local staging=""

    [[ -f "$manifest" ]] || return 0
    source_id="$(jq -er '.source_id | select(type == "string" and length > 0)' "$manifest")" \
        || return 0
    [[ "$source_id" =~ ^[A-Za-z0-9._-]+$ ]] || return 0
    lock_file="$ZSOFT_ROOT/source-locks/$source_id.json"
    target="$CURRENT_SOURCE_CACHE/$source_id"
    if cache_metadata_matches "$target" "$lock_file"; then
        case_status "$log_file" "SOURCE_CACHE_READY case=$case_id source_id=$source_id location=current"
        return 0
    fi
    if [[ -e "$target" ]]; then
        case_status "$log_file" "SOURCE_CACHE_STALE case=$case_id source_id=$source_id action=framework_fetch_validation"
        return 0
    fi

    for candidate in "${SOURCE_CACHE_CANDIDATES[@]}"; do
        cache_metadata_matches "$candidate/$source_id" "$lock_file" || continue
        case_status "$log_file" "SOURCE_CACHE_REUSE_START case=$case_id source_id=$source_id"
        staging="$(mktemp -d "$CURRENT_SOURCE_CACHE/.reuse-${source_id}.XXXXXX")" \
            || return 0
        if cp -a --reflink=auto "$candidate/$source_id/." "$staging/" \
            && cache_metadata_matches "$staging" "$lock_file"; then
            mv "$staging" "$target"
            case_status "$log_file" "SOURCE_CACHE_REUSED case=$case_id source_id=$source_id"
            return 0
        fi
        case "$staging" in
            "$CURRENT_SOURCE_CACHE"/.reuse-*) rm -rf -- "$staging" ;;
        esac
    done
    case_status "$log_file" "SOURCE_CACHE_MISS case=$case_id source_id=$source_id action=fetch_if_needed"
}

build_bench_args() {
    local case_id="$1"
    local campaign_id="$2"
    local seed="$3"
    BENCH_ARGS=(
        --benchmark zsoft-l1
        --task-id "$case_id"
        --campaign-id "$campaign_id"
        --campaign-dir "$WORK_DIR/$CAMPAIGN_BASE/$campaign_id"
        --method goal-plus-pi
        --seed "$seed"
        --model "$L1_MODEL"
        --reasoning-effort high
        --pi-provider-id "$L1_PROVIDER"
        --pi-api "$L1_PI_API"
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
    if [[ "$L1_SHARED_KNOWLEDGE_ENABLED" == "1" ]]; then
        BENCH_ARGS+=(--shared-cache)
    fi
}

start_launch_heartbeat() {
    local case_id="$1"
    local seed="$2"
    local campaign_dir="$3"
    local log_file="$4"
    local started_epoch="$5"

    (
        local campaign_state="not_created"
        local cell_state="not_created"
        local experiment_status="not_created"
        local final_eval_status="absent"
        local run_dir=""
        while true; do
            sleep "$STATUS_INTERVAL_SECONDS"
            campaign_state="not_created"
            cell_state="not_created"
            experiment_status="not_created"
            final_eval_status="absent"
            run_dir=""
            if [[ -f "$campaign_dir/campaign.json" ]]; then
                campaign_state="$(jq -r '.state // "unknown"' \
                    "$campaign_dir/campaign.json" 2>/dev/null)" \
                    || campaign_state="unreadable"
                cell_state="$(jq -r '
                    if (.cells | type) == "array" and (.cells | length) == 1
                    then .cells[0].state // "unknown"
                    else "unavailable"
                    end
                ' "$campaign_dir/campaign.json" 2>/dev/null)" \
                    || cell_state="unreadable"
                run_dir="$(jq -r '
                    if (.cells | type) == "array" and (.cells | length) == 1
                    then .cells[0].run_dir // empty
                    else empty
                    end
                ' "$campaign_dir/campaign.json" 2>/dev/null)" || run_dir=""
            fi
            case "$run_dir/" in
                "$campaign_dir"/cells/*/)
                    if [[ -f "$run_dir/experiment.json" ]]; then
                        experiment_status="$(jq -r '.status // "unknown"' \
                            "$run_dir/experiment.json" 2>/dev/null)" \
                            || experiment_status="unreadable"
                    fi
                    [[ -f "$run_dir/final-eval.json" ]] \
                        && final_eval_status="present"
                    ;;
            esac
            case_status "$log_file" \
                "EXPERIMENT_HEARTBEAT case=$case_id seed=$seed elapsed_seconds=$(($(date +%s) - started_epoch)) campaign_state=$campaign_state cell_state=$cell_state experiment_status=$experiment_status final_eval=$final_eval_status"
        done
    ) &
    ACTIVE_HEARTBEAT_PID=$!
}

stop_launch_heartbeat() {
    if [[ -n "$ACTIVE_HEARTBEAT_PID" ]]; then
        kill "$ACTIVE_HEARTBEAT_PID" >/dev/null 2>&1 || true
        wait "$ACTIVE_HEARTBEAT_PID" 2>/dev/null || true
        ACTIVE_HEARTBEAT_PID=""
    fi
}

declare -a BENCH_ARGS=()
ATTEMPT_RESULT="INFRA_ERROR"
ATTEMPT_CAMPAIGN_ID=""
ATTEMPT_REASON="not_started"
ATTEMPT_LOG_FILE=""

campaign_contract_valid() {
    local case_id="$1"
    local seed="$2"
    local campaign="$3"

    jq -e \
        --arg case_id "$case_id" \
        --argjson seed "$seed" \
        --arg model "$L1_MODEL" \
        --argjson wall_time "$WALL_TIME_SECONDS" \
        --argjson live_concurrency "$LIVE_SEARCH_CONCURRENCY" \
        --argjson cell_concurrency "$CELL_CONCURRENCY" '
        .task_id == $case_id and
        .methods == ["goal-plus-pi"] and
        .seeds == [$seed] and
        .model == $model and
        .reasoning_effort == "high" and
        .budget.wall_time_seconds == $wall_time and
        .budget.live_search_concurrency == $live_concurrency and
        .budget.requested_live_concurrency == $live_concurrency and
        .budget.cell_concurrency == $cell_concurrency and
        .budget.attempts == 1 and
        .budget.worker_runtime_seconds == 1200 and
        .budget.worker_min_runtime_seconds == 600 and
        (.cells | type) == "array" and
        (.cells | length) == 1 and
        .cells[0].benchmark_id == "zsoft-l1" and
        .cells[0].method == "goal-plus-pi" and
        .cells[0].seed == $seed and
        .cells[0].effective_concurrency == $live_concurrency
    ' "$campaign" >/dev/null 2>&1
}

experiment_base_contract_valid() {
    local case_id="$1"
    local seed="$2"
    local experiment="$3"

    jq -e \
        --arg case_id "$case_id" \
        --argjson seed "$seed" \
        --arg model "$L1_MODEL" \
        --arg provider "$L1_PROVIDER" \
        --arg pi_api "$L1_PI_API" \
        --arg key_env "$PROVIDER_KEY_ENV" \
        --argjson wall_time "$WALL_TIME_SECONDS" \
        --argjson live_concurrency "$LIVE_SEARCH_CONCURRENCY" '
        .method == "goal-plus-pi" and
        .seed == $seed and
        .task_id == $case_id and
        .model == $model and
        .reasoning_effort == "high" and
        .budget.wall_time_seconds == $wall_time and
        .budget.concurrency == $live_concurrency and
        .budget.worker_runtime_seconds == 1200 and
        .budget.worker_min_runtime_seconds == 600 and
        .goal_plus_config.command_config.max_parallel == $live_concurrency and
        .goal_plus_config.metric_name == "success" and
        .goal_plus_config.controller_only_official_evaluation == false and
        .goal_plus_config.early_stop.enabled == true and
        .goal_plus_config.early_stop.metric_name == "success" and
        .goal_plus_config.early_stop.target_score == 1 and
        .task.evaluation_mode == "visible" and
        .pi_provider.id == $provider and
        .pi_provider.api == $pi_api and
        .pi_provider.api_key_env == $key_env and
        .execution.goal_plus_controller_closeout.completed == true
    ' "$experiment" >/dev/null 2>&1
}

experiment_finished_cleanly() {
    local experiment="$1"

    jq -e '
        .status == "finished" and
        (.execution.result_incomplete_reason // null) == null and
        (
            .execution.early_stop_triggered != true or
            (
                .execution.early_stop_completion_verified == true and
                .execution.minimum_lease_completion_waived == true
            )
        )
    ' "$experiment" >/dev/null 2>&1
}

minimum_lease_partial_has_valid_worker_evidence() {
    local experiment="$1"

    jq -e '
        .status == "incomplete" and
        (.execution.result_incomplete_reason | type) == "string" and
        (.execution.result_incomplete_reason | startswith("Search run ")) and
        (.execution.result_incomplete_reason |
            contains(" did not satisfy the Pi minimum lease for jobs: ")) and
        .execution.controller_interrupted == false and
        .execution.hard_killed == false and
        .execution.official_evaluation_withheld != true and
        .execution.evaluator_calls.controller_final_claimed == 1 and
        (.execution.pi_pool_cleanup | type) == "array" and
        (.execution.pi_pool_cleanup | length) > 0 and
        all(
            .execution.pi_pool_cleanup[];
            .state == "closed" and
            .active_count == 0 and
            .close_timed_out == false
        ) and
        any(
            .execution.goal_plus.runs[]?;
            (.worker_verified_candidate_count // 0) > 0 and
            any(
                .pi_pool_jobs[]?;
                .status == "completed" and
                .lease.satisfied == true and
                (.lease.verifier_runs // 0) >= 1
            )
        )
    ' "$experiment" >/dev/null 2>&1
}

final_eval_contract_valid() {
    local case_id="$1"
    local final_eval="$2"

    jq -e --arg case_id "$case_id" '
        .task_id == $case_id and
        .mode == "final" and
        .valid == true and
        (.success | type) == "number" and
        (.success == 0 or .success == 1)
    ' "$final_eval" >/dev/null 2>&1
}

run_case_attempt() {
    local case_id="$1"
    local seed="$2"
    local attempt="$3"
    local task_cell_number="$4"
    local campaign_id=""
    if ((REPEAT_COUNT == 1)); then
        campaign_id="l1-${case_id}-${RUN_STAMP}-a${attempt}"
    else
        campaign_id="l1-${case_id}-seed-${seed}-${RUN_STAMP}-a${attempt}"
    fi
    local campaign_dir="$WORK_DIR/$CAMPAIGN_BASE/$campaign_id"
    local case_log_dir="$LOG_DIR/cases/$case_id"
    if ((REPEAT_COUNT > 1)); then
        case_log_dir="$case_log_dir/seed-$seed"
    fi
    local log_file="$case_log_dir/attempt-${attempt}.log"
    local launch_rc=0
    local campaign_state=""
    local cell_state=""
    local run_dir=""
    local experiment=""
    local final_eval=""
    local success=""
    local stage_started=0
    local environment_miss_reason=""
    local core_reason=""
    local source_fetch_tmpdir=""
    local result_evidence_mode="complete"

    ATTEMPT_RESULT="INFRA_ERROR"
    ATTEMPT_CAMPAIGN_ID="$campaign_id"
    ATTEMPT_REASON="attempt_started"
    ATTEMPT_LOG_FILE="$log_file"
    mkdir -p "$case_log_dir"
    : >"$log_file"
    case_status "$log_file" \
        "CASE_ATTEMPT_START case=$case_id seed=$seed position=$task_cell_number/$TOTAL_TASK_CELLS attempt=$attempt/$MAX_INFRA_ATTEMPTS campaign_id=$campaign_id"

    build_bench_args "$case_id" "$campaign_id" "$seed"
    stage_started="$(date +%s)"
    case_status "$log_file" "PLAN_START case=$case_id campaign_id=$campaign_id"
    "$PYTHON" scripts/bench.py plan "${BENCH_ARGS[@]}" >>"$log_file" 2>&1
    if (($? != 0)); then
        ATTEMPT_REASON="plan_failed"
        case_status "$log_file" \
            "PLAN_FAILED case=$case_id elapsed_seconds=$(($(date +%s) - stage_started))"
        return 0
    fi
    case_status "$log_file" \
        "PLAN_OK case=$case_id elapsed_seconds=$(($(date +%s) - stage_started))"

    stage_started="$(date +%s)"
    case_status "$log_file" "L1_ENV_CHECK_START case=$case_id state_dir=$STATE_DIR/$case_id"
    if case_environment_reusable "$case_id"; then
        case_status "$log_file" \
            "L1_ENV_REUSED case=$case_id elapsed_seconds=$(($(date +%s) - stage_started)) scope=docker_judge_images_and_task_state"
    else
        environment_miss_reason="$ENV_REUSE_REASON"
        case_status "$log_file" \
            "L1_ENV_REBUILD_REQUIRED case=$case_id reason=$environment_miss_reason"

        reuse_local_source_cache "$case_id" "$log_file"
        if [[ -f "$TASKS_DIR/$case_id/private/source-manifest.json" ]]; then
            source_fetch_tmpdir="${SOURCE_CACHE_RESOLVED%/*}/.source-fetch-tmp"
            if ! mkdir -p "$source_fetch_tmpdir" \
                || [[ "$(stat -Lc '%d' "$source_fetch_tmpdir" 2>/dev/null || true)" != "$(stat -Lc '%d' "$CURRENT_SOURCE_CACHE" 2>/dev/null || true)" ]]; then
                ATTEMPT_REASON="source_fetch_temp_dir_unavailable"
                case_status "$log_file" \
                    "FETCH_SOURCES_FAILED case=$case_id reason=$ATTEMPT_REASON temp_dir=$source_fetch_tmpdir"
                return 0
            fi
            stage_started="$(date +%s)"
            case_status "$log_file" \
                "FETCH_SOURCES_START case=$case_id temp_dir=$source_fetch_tmpdir"
            TMPDIR="$source_fetch_tmpdir" PYTHONPATH="$ZSOFT_ROOT/src" \
                "$PYTHON" -m zsoft_poc fetch-sources \
                --task-id "$case_id" \
                --tasks-dir "$TASKS_DIR" >>"$log_file" 2>&1
            if (($? != 0)); then
                ATTEMPT_REASON="fetch_sources_failed"
                case_status "$log_file" \
                    "FETCH_SOURCES_FAILED case=$case_id elapsed_seconds=$(($(date +%s) - stage_started))"
                return 0
            fi
            case_status "$log_file" \
                "FETCH_SOURCES_OK case=$case_id elapsed_seconds=$(($(date +%s) - stage_started))"
        else
            case_status "$log_file" "FETCH_SOURCES_SKIPPED case=$case_id reason=no_external_source"
        fi

        stage_started="$(date +%s)"
        case_status "$log_file" "L1_DOCKER_PREPARE_START case=$case_id"
        PYTHONPATH="$ZSOFT_ROOT/src" "$PYTHON" -m zsoft_poc prepare \
            "$case_id" \
            --tasks-dir "$TASKS_DIR" \
            --state-dir "$STATE_DIR" >>"$log_file" 2>&1
        if (($? != 0)); then
            ATTEMPT_REASON="docker_prepare_failed"
            case_status "$log_file" \
                "L1_DOCKER_PREPARE_FAILED case=$case_id elapsed_seconds=$(($(date +%s) - stage_started))"
            return 0
        fi
        case_status "$log_file" \
            "L1_DOCKER_PREPARE_OK case=$case_id elapsed_seconds=$(($(date +%s) - stage_started))"

        if ! write_environment_stamp "$case_id" "run_all_l1_cases.sh"; then
            ATTEMPT_REASON="environment_stamp_write_failed"
            case_status "$log_file" "L1_ENV_STAMP_FAILED case=$case_id"
            return 0
        fi
        if ! case_environment_reusable "$case_id"; then
            ATTEMPT_REASON="environment_validation_failed_${ENV_REUSE_REASON}"
            case_status "$log_file" \
                "L1_ENV_VALIDATION_FAILED case=$case_id reason=$ENV_REUSE_REASON"
            return 0
        fi
        case_status "$log_file" \
            "L1_ENV_READY case=$case_id stamp=$STATE_DIR/$case_id/$ENV_STAMP_NAME"
    fi

    stage_started="$(date +%s)"
    case_status "$log_file" \
        "EXPERIMENT_LAUNCH_START case=$case_id campaign_id=$campaign_id campaign_dir=$campaign_dir fresh_campaign=true"
    start_launch_heartbeat \
        "$case_id" "$seed" "$campaign_dir" "$log_file" "$stage_started"
    "$PYTHON" scripts/bench.py launch "${BENCH_ARGS[@]}" >>"$log_file" 2>&1
    launch_rc=$?
    stop_launch_heartbeat
    case_status "$log_file" \
        "EXPERIMENT_LAUNCH_EXIT case=$case_id exit_code=$launch_rc elapsed_seconds=$(($(date +%s) - stage_started))"

    if [[ ! -f "$campaign_dir/campaign.json" || -L "$campaign_dir/campaign.json" ]]; then
        ATTEMPT_REASON="campaign_manifest_missing_after_launch_${launch_rc}"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    fi
    campaign_state="$(jq -er '.state // empty' "$campaign_dir/campaign.json" 2>/dev/null)" || {
        ATTEMPT_REASON="campaign_manifest_invalid"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    }
    if ! campaign_contract_valid \
        "$case_id" "$seed" "$campaign_dir/campaign.json"; then
        ATTEMPT_REASON="campaign_contract_invalid"
        case_status "$log_file" \
            "RESULT_VALIDATION_FAILED case=$case_id seed=$seed reason=$ATTEMPT_REASON"
        return 0
    fi
    cell_state="$(jq -r '
        if (.cells | type) == "array" and (.cells | length) == 1
        then .cells[0].state // empty
        else empty
        end
    ' "$campaign_dir/campaign.json" 2>/dev/null)"
    run_dir="$(jq -er '
        if (.cells | type) == "array" and (.cells | length) == 1
        then .cells[0].run_dir
        else empty
        end
    ' "$campaign_dir/campaign.json" 2>/dev/null)" || {
        ATTEMPT_REASON="run_dir_missing"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    }
    case "$run_dir/" in
        "$campaign_dir"/cells/*/) ;;
        *)
            ATTEMPT_REASON="run_dir_outside_campaign"
            case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
            return 0
            ;;
    esac
    experiment="$run_dir/experiment.json"
    final_eval="$run_dir/final-eval.json"

    if ((launch_rc != 0)) \
        || [[ "$campaign_state" != "finished" ]] \
        || [[ "$cell_state" != "finished" ]]; then
        core_reason="unavailable"
        if [[ -f "$experiment" && ! -L "$experiment" ]]; then
            core_reason="$(jq -r \
                '.execution.result_incomplete_reason // "unavailable"' \
                "$experiment" 2>/dev/null)" || core_reason="unreadable"
            core_reason="${core_reason//$'\t'/ }"
            core_reason="${core_reason//$'\n'/ }"
        fi

        if ((launch_rc == 2)) \
            && [[ "$campaign_state" == "partial" ]] \
            && [[ "$cell_state" == "incomplete" ]] \
            && [[ -f "$experiment" && ! -L "$experiment" ]] \
            && [[ -f "$final_eval" && ! -L "$final_eval" ]] \
            && experiment_base_contract_valid "$case_id" "$seed" "$experiment" \
            && minimum_lease_partial_has_valid_worker_evidence "$experiment" \
            && final_eval_contract_valid "$case_id" "$final_eval"; then
            result_evidence_mode="partial_minimum_lease"
            case_status "$log_file" \
                "RESULT_PARTIAL_ACCEPTED case=$case_id policy=valid_final_eval_with_completed_worker campaign_state=$campaign_state cell_state=$cell_state core_reason=$core_reason"
        else
            if [[ "$campaign_state" != "finished" ]]; then
                ATTEMPT_REASON="campaign_state_${campaign_state:-missing}"
            elif [[ "$cell_state" != "finished" ]]; then
                ATTEMPT_REASON="cell_state_${cell_state:-missing}"
            else
                ATTEMPT_REASON="launch_exit_code_${launch_rc}"
            fi
            case_status "$log_file" \
                "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON launch_exit_code=$launch_rc cell_state=${cell_state:-missing} core_reason=$core_reason"
            return 0
        fi
    fi

    if [[ ! -f "$experiment" || -L "$experiment" ]]; then
        ATTEMPT_REASON="experiment_manifest_missing"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    fi
    if [[ ! -f "$final_eval" || -L "$final_eval" ]]; then
        ATTEMPT_REASON="final_eval_missing"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    fi

    case_status "$log_file" "RESULT_VALIDATION_START case=$case_id"
    experiment_base_contract_valid "$case_id" "$seed" "$experiment" || {
        ATTEMPT_REASON="experiment_contract_invalid"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    }
    if [[ "$result_evidence_mode" == "complete" ]] \
        && ! experiment_finished_cleanly "$experiment"; then
        ATTEMPT_REASON="experiment_contract_invalid"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    fi
    final_eval_contract_valid "$case_id" "$final_eval" || {
        ATTEMPT_REASON="final_eval_contract_invalid"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    }

    success="$(jq -r '.success' "$final_eval")"
    if [[ "$success" == "1" ]]; then
        ATTEMPT_RESULT="PASS"
        ATTEMPT_REASON="valid_pass"
    else
        ATTEMPT_RESULT="NOT_PASS"
        ATTEMPT_REASON="valid_not_pass"
    fi
    if [[ "$result_evidence_mode" == "partial_minimum_lease" ]]; then
        ATTEMPT_REASON="${ATTEMPT_REASON}_partial_minimum_lease"
    fi
    case_status "$log_file" \
        "RESULT_VALIDATION_OK case=$case_id seed=$seed result=$ATTEMPT_RESULT reason=$ATTEMPT_REASON evidence_mode=$result_evidence_mode"
}

PASS_COUNT=0
NOT_PASS_COUNT=0
INFRA_COUNT=0
COMPLETED_COUNT=0
declare -a INFRA_CASES=()

for case_index in "${!CASE_IDS[@]}"; do
    case_id="${CASE_IDS[$case_index]}"
    for ((seed = 1; seed <= REPEAT_COUNT; seed++)); do
        task_cell_number=$((case_index * REPEAT_COUNT + seed))
        case_result="INFRA_ERROR"
        attempts_used=0
        final_campaign_id=""
        final_reason="not_started"
        final_log_file=""

        for ((attempt = 1; attempt <= MAX_INFRA_ATTEMPTS; attempt++)); do
            attempts_used="$attempt"
            run_case_attempt "$case_id" "$seed" "$attempt" "$task_cell_number"
            case_result="$ATTEMPT_RESULT"
            final_campaign_id="$ATTEMPT_CAMPAIGN_ID"
            final_reason="$ATTEMPT_REASON"
            final_log_file="$ATTEMPT_LOG_FILE"
            [[ "$case_result" == "INFRA_ERROR" ]] || break
            if ((attempt < MAX_INFRA_ATTEMPTS)); then
                status_message \
                    "CASE_RETRY case=$case_id seed=$seed position=$task_cell_number/$TOTAL_TASK_CELLS attempt=$attempt reason=$final_reason"
            fi
        done

        case "$case_result" in
            PASS)
                PASS_COUNT=$((PASS_COUNT + 1))
                COMPLETED_COUNT=$((COMPLETED_COUNT + 1))
                status_message \
                    "CASE_COMPLETE case=$case_id seed=$seed position=$task_cell_number/$TOTAL_TASK_CELLS result=PASS attempts=$attempts_used"
                ;;
            NOT_PASS)
                NOT_PASS_COUNT=$((NOT_PASS_COUNT + 1))
                COMPLETED_COUNT=$((COMPLETED_COUNT + 1))
                status_message \
                    "CASE_COMPLETE case=$case_id seed=$seed position=$task_cell_number/$TOTAL_TASK_CELLS result=NOT_PASS attempts=$attempts_used"
                ;;
            *)
                INFRA_COUNT=$((INFRA_COUNT + 1))
                INFRA_CASES+=("$case_id:seed-$seed")
                status_message \
                    "CASE_COMPLETE case=$case_id seed=$seed position=$task_cell_number/$TOTAL_TASK_CELLS result=INFRA_ERROR attempts=$attempts_used reason=$final_reason"
                ;;
        esac

        printf '%s\t%d\t%s\t%d\t%s\t%s\t%s\n' \
            "$case_id" "$seed" "$case_result" "$attempts_used" \
            "$final_campaign_id" "$final_reason" "$final_log_file" \
            >>"$SUMMARY_FILE"

        if ((task_cell_number < TOTAL_TASK_CELLS)); then
            sleep 2
        fi
    done
done

if ((COMPLETED_COUNT > 0)); then
    PASS_RATE="$(awk -v passed="$PASS_COUNT" -v completed="$COMPLETED_COUNT" \
        'BEGIN { printf "%.2f%%", (100 * passed) / completed }')"
else
    PASS_RATE="n/a"
fi

{
    printf '\n结束时间: %s\n' "$(date --iso-8601=seconds)"
    printf '计划 task cell: %d\n' "$TOTAL_TASK_CELLS"
    printf '有效完成: %d\n' "$COMPLETED_COUNT"
    printf 'PASS: %d\n' "$PASS_COUNT"
    printf 'NOT_PASS: %d\n' "$NOT_PASS_COUNT"
    printf 'INFRA_ERROR: %d\n' "$INFRA_COUNT"
    printf '有效结果通过率: %s\n' "$PASS_RATE"
    if ((INFRA_COUNT == 0)); then
        printf '最终通过率: %s\n' "$PASS_RATE"
    else
        printf '最终通过率: unavailable（仍有基础设施失败需要重跑）\n'
        printf '待重跑 case/seed: %s\n' "${INFRA_CASES[*]}"
    fi
} >>"$SUMMARY_FILE"

status_message \
    "RUN_COMPLETE planned=$TOTAL_TASK_CELLS completed=$COMPLETED_COUNT pass=$PASS_COUNT not_pass=$NOT_PASS_COUNT infra_error=$INFRA_COUNT summary=$SUMMARY_FILE"

printf '\n运行结束：有效完成 %d/%d，PASS=%d，NOT_PASS=%d，INFRA_ERROR=%d。\n' \
    "$COMPLETED_COUNT" "$TOTAL_TASK_CELLS" "$PASS_COUNT" "$NOT_PASS_COUNT" "$INFRA_COUNT"
printf '有效结果通过率: %s\n' "$PASS_RATE"
printf '汇总文件: %s\n' "$SUMMARY_FILE"

if ((INFRA_COUNT > 0)); then
    exit 2
fi
exit 0
