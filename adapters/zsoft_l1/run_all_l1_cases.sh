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
# Base directory (relative to WORK_DIR) that benchmarks write their campaign runs to.
# Isolated from the historical "runs/benchmark-campaigns" so new data does not mix with
# prior runs; the Python side must receive --campaign-dir explicitly or it would fall
# back to runs/benchmark-campaigns (common_matrix). Keep this under runs/ so the
# ensure_under(RUNS_ROOT) guard in common_matrix passes.
readonly CAMPAIGN_BASE="runs/benchmark-campaigns-v3"
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
MAX_INFRA_ATTEMPTS="${L1_MAX_INFRA_ATTEMPTS:-2}"
STATUS_INTERVAL_SECONDS="${L1_STATUS_INTERVAL_SECONDS:-60}"
ACTIVE_HEARTBEAT_PID=""
SOURCE_CACHE_RESOLVED=""
SOURCE_CACHE_STORAGE_MODE="unconfigured"

usage() {
    cat <<'USAGE'
用法:
  ./run_all_l1_cases.sh
  ./run_all_l1_cases.sh --one
  ./run_all_l1_cases.sh --case-id CASE_ID

选项:
  --one                         自动选择一个 case 做真实验证
  --case-id CASE_ID             只运行指定 case
  --max-infra-attempts NUMBER   基础设施失败时的最大尝试次数，默认 2
  -h, --help                    显示帮助

可选环境变量:
  DEEPSEEK_API_KEY              已设置时不再交互读取
  L1_SOURCE_CACHE_DIR           明确指定的额外 .cache/sources 目录；不会自动扫描磁盘
  L1_STORAGE_ROOT               prepare 脚本使用的明确外置存储根目录
  L1_MAX_INFRA_ATTEMPTS         基础设施失败最大尝试次数
  L1_STATUS_INTERVAL_SECONDS    实验运行心跳间隔，默认 60 秒
  L1_CAMPAIGN_MIN_FREE_GIB      本地磁盘可用空间低于该 GiB 时把 campaign 输出迁到外置磁盘，默认 80
  L1_STORAGE_ROOT              外置存储根目录（源码缓存/迁出的 campaign），默认 /srv/zsoft-l1
  L1_CAMPAIGN_EXTERNAL_ROOT     磁盘不足时的外置 campaign 目录，默认 $L1_STORAGE_ROOT/campaigns/... ；会在当前目录留软链接

环境复用:
  仅复用通过版本指纹校验的 L1 Docker judge 环境。
  锁定源码缓存按 source-lock 元数据单独校验和复用。
  campaign、实验 workspace 和 Goal Plus 状态始终重新创建。
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
                || die "--one 不能和 --case-id 重复使用"
            MODE="one"
            shift
            ;;
        --case-id)
            (($# >= 2)) || die "--case-id 缺少参数"
            [[ "$MODE" == "all" && -z "$REQUESTED_CASE_ID" ]] \
                || die "--case-id 不能重复，也不能和 --one 同时使用"
            MODE="case"
            REQUESTED_CASE_ID="$2"
            shift 2
            ;;
        --max-infra-attempts)
            (($# >= 2)) || die "--max-infra-attempts 缺少参数"
            MAX_INFRA_ATTEMPTS="$2"
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

[[ "$EXPECTED_CASES" =~ ^[1-9][0-9]*$ ]] \
    || die "L1_EXPECTED_CASES 必须是正整数"
[[ "$MAX_INFRA_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] \
    || die "最大基础设施尝试次数必须是正整数"
[[ "$STATUS_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] \
    || die "L1_STATUS_INTERVAL_SECONDS 必须是正整数"
((STATUS_INTERVAL_SECONDS >= 10)) \
    || die "L1_STATUS_INTERVAL_SECONDS 不能小于 10 秒"

cd "$WORK_DIR" || die "无法进入工作目录: $WORK_DIR"

for required in awk cp date docker find flock git jq mktemp mv realpath rm sed sha256sum sleep sort stat tee; do
    require_command "$required"
done
[[ -x "$PYTHON" ]] || die "Python 环境不存在: $PYTHON"
[[ -d "$TASKS_DIR" ]] || die "L1 tasks 目录不存在: $TASKS_DIR"

mkdir -p "$WORK_DIR/.tmp"
exec 9>"$WORK_DIR/.tmp/run-l1-cases.lock"
flock -n 9 || die "已有另一个 L1 批量或验证脚本正在运行"

mapfile -t ALL_CASE_IDS < <(
    find "$TASKS_DIR" -mindepth 2 -maxdepth 2 \
        -type f -name task.json -printf '%h\n' |
        sed 's#.*/##' |
        LC_ALL=C sort
)

((${#ALL_CASE_IDS[@]} > 0)) || die "未找到任何 L1 case"
((${#ALL_CASE_IDS[@]} == EXPECTED_CASES)) \
    || die "L1 case 数量为 ${#ALL_CASE_IDS[@]}，预期为 $EXPECTED_CASES；请先确认任务集版本"
for discovered_case_id in "${ALL_CASE_IDS[@]}"; do
    [[ "$discovered_case_id" =~ ^[a-z0-9][a-z0-9-]*$ ]] \
        || die "发现不安全的 L1 case ID: $discovered_case_id"
done

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

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
    [[ -t 0 ]] || die "非交互运行前必须先 export DEEPSEEK_API_KEY"
    read -rsp '请输入 DEEPSEEK_API_KEY: ' DEEPSEEK_API_KEY
    printf '\n'
fi
[[ -n "$DEEPSEEK_API_KEY" ]] || die "DEEPSEEK_API_KEY 不能为空"
export DEEPSEEK_API_KEY
export DEEPSEEK_BASE_URL='https://api.deepseek.com'
unset OPENAI_BASE_URL OPENAI_API_BASE_URL OPENAI_API_BASE

cleanup_runtime() {
    if [[ -n "$ACTIVE_HEARTBEAT_PID" ]]; then
        kill "$ACTIVE_HEARTBEAT_PID" >/dev/null 2>&1 || true
        wait "$ACTIVE_HEARTBEAT_PID" 2>/dev/null || true
        ACTIVE_HEARTBEAT_PID=""
    fi
    unset DEEPSEEK_API_KEY
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
    printf '配置: model=deepseek-v4-flash reasoning=high T=1800 K=4 C=1 R=1\n'
    printf '运行心跳间隔: %s 秒\n' "$STATUS_INTERVAL_SECONDS"
    printf '锁定源码缓存: %s\n' "$CURRENT_SOURCE_CACHE"
    printf '锁定源码缓存实际位置: %s\n' "$SOURCE_CACHE_RESOLVED"
    printf '锁定源码缓存模式: %s\n' "$SOURCE_CACHE_STORAGE_MODE"
    printf '计划 case 数: %d\n' "${#CASE_IDS[@]}"
    printf 'case_id\tstatus\tattempts\tcampaign_id\treason\tcase_log\n'
} >"$SUMMARY_FILE"

status_message "INIT work_dir=$WORK_DIR"
status_message "INIT log_dir=$LOG_DIR progress_file=$PROGRESS_FILE"
status_message \
    "INIT l1_state_dir=$STATE_DIR source_cache=$CURRENT_SOURCE_CACHE source_cache_resolved=$SOURCE_CACHE_RESOLVED storage_mode=$SOURCE_CACHE_STORAGE_MODE"
status_message "CONFIG model=deepseek-v4-flash reasoning=high T=1800 K=4 C=1 R=1"
status_message "QUEUE cases=${#CASE_IDS[@]} execution=serial"

ensure_campaign_storage

PREFLIGHT_LOG="$LOG_DIR/preflight.log"
preflight_started="$(date +%s)"
status_message "PREFLIGHT_START log=$PREFLIGHT_LOG"
if {
    "$PYTHON" scripts/bench.py catalog
    "$PYTHON" scripts/bench.py setup \
        --benchmark zsoft-l1 \
        --method goal-plus-pi \
        --model deepseek-v4-flash \
        --reasoning-effort high \
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
    BENCH_ARGS=(
        --benchmark zsoft-l1
        --task-id "$case_id"
        --campaign-id "$campaign_id"
        --campaign-dir "$WORK_DIR/$CAMPAIGN_BASE/$campaign_id"
        --method goal-plus-pi
        --seed 1
        --model deepseek-v4-flash
        --reasoning-effort high
        --pi-provider-id deepseek
        --pi-api openai-completions
        --pi-api-key-env DEEPSEEK_API_KEY
        --pi-api-base-env DEEPSEEK_BASE_URL
        --wall-time-seconds 1800
        --live-search-concurrency 4
        --cell-concurrency 1
        --worker-runtime-seconds 1200
        --worker-min-runtime-seconds 600
        --skip-bootstrap
        --skip-provision
        --foreground
    )
}

start_launch_heartbeat() {
    local case_id="$1"
    local campaign_dir="$2"
    local log_file="$3"
    local started_epoch="$4"

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
                "EXPERIMENT_HEARTBEAT case=$case_id elapsed_seconds=$(($(date +%s) - started_epoch)) campaign_state=$campaign_state cell_state=$cell_state experiment_status=$experiment_status final_eval=$final_eval_status"
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

run_case_attempt() {
    local case_id="$1"
    local attempt="$2"
    local case_number="$3"
    local campaign_id="l1-${case_id}-${RUN_STAMP}-a${attempt}"
    local campaign_dir="$WORK_DIR/$CAMPAIGN_BASE/$campaign_id"
    local case_log_dir="$LOG_DIR/cases/$case_id"
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

    ATTEMPT_RESULT="INFRA_ERROR"
    ATTEMPT_CAMPAIGN_ID="$campaign_id"
    ATTEMPT_REASON="attempt_started"
    ATTEMPT_LOG_FILE="$log_file"
    mkdir -p "$case_log_dir"
    : >"$log_file"
    case_status "$log_file" \
        "CASE_ATTEMPT_START case=$case_id position=$case_number/${#CASE_IDS[@]} attempt=$attempt/$MAX_INFRA_ATTEMPTS campaign_id=$campaign_id"

    build_bench_args "$case_id" "$campaign_id"
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
    start_launch_heartbeat "$case_id" "$campaign_dir" "$log_file" "$stage_started"
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
    jq -e --arg case_id "$case_id" '
        .status == "finished" and
        .method == "goal-plus-pi" and
        .seed == 1 and
        .task_id == $case_id and
        .model == "deepseek-v4-flash" and
        .reasoning_effort == "high" and
        .budget.wall_time_seconds == 1800 and
        .budget.concurrency == 4 and
        .budget.worker_runtime_seconds == 1200 and
        .budget.worker_min_runtime_seconds == 600 and
        .goal_plus_config.command_config.max_parallel == 4 and
        .goal_plus_config.metric_name == "success" and
        .goal_plus_config.controller_only_official_evaluation == false and
        .goal_plus_config.early_stop.enabled == true and
        .goal_plus_config.early_stop.metric_name == "success" and
        .goal_plus_config.early_stop.target_score == 1 and
        .task.evaluation_mode == "visible" and
        .pi_provider.id == "deepseek" and
        .pi_provider.api == "openai-completions" and
        .pi_provider.api_key_env == "DEEPSEEK_API_KEY" and
        .execution.goal_plus_controller_closeout.completed == true and
        (.execution.result_incomplete_reason // null) == null and
        (
            .execution.early_stop_triggered != true or
            (
                .execution.early_stop_completion_verified == true and
                .execution.minimum_lease_completion_waived == true
            )
        )
    ' "$experiment" >/dev/null 2>&1 || {
        ATTEMPT_REASON="experiment_contract_invalid"
        case_status "$log_file" "RESULT_VALIDATION_FAILED case=$case_id reason=$ATTEMPT_REASON"
        return 0
    }

    jq -e --arg case_id "$case_id" '
        .task_id == $case_id and
        .valid == true and
        (.success | type) == "number" and
        (.success == 0 or .success == 1)
    ' "$final_eval" >/dev/null 2>&1 || {
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
    case_status "$log_file" \
        "RESULT_VALIDATION_OK case=$case_id result=$ATTEMPT_RESULT reason=$ATTEMPT_REASON"
}

PASS_COUNT=0
NOT_PASS_COUNT=0
INFRA_COUNT=0
COMPLETED_COUNT=0
declare -a INFRA_CASES=()

for case_index in "${!CASE_IDS[@]}"; do
    case_id="${CASE_IDS[$case_index]}"
    case_number=$((case_index + 1))
    case_result="INFRA_ERROR"
    attempts_used=0
    final_campaign_id=""
    final_reason="not_started"
    final_log_file=""

    for ((attempt = 1; attempt <= MAX_INFRA_ATTEMPTS; attempt++)); do
        attempts_used="$attempt"
        run_case_attempt "$case_id" "$attempt" "$case_number"
        case_result="$ATTEMPT_RESULT"
        final_campaign_id="$ATTEMPT_CAMPAIGN_ID"
        final_reason="$ATTEMPT_REASON"
        final_log_file="$ATTEMPT_LOG_FILE"
        [[ "$case_result" == "INFRA_ERROR" ]] || break
        if ((attempt < MAX_INFRA_ATTEMPTS)); then
            status_message \
                "CASE_RETRY case=$case_id position=$case_number/${#CASE_IDS[@]} attempt=$attempt reason=$final_reason"
        fi
    done

    case "$case_result" in
        PASS)
            PASS_COUNT=$((PASS_COUNT + 1))
            COMPLETED_COUNT=$((COMPLETED_COUNT + 1))
            status_message \
                "CASE_COMPLETE case=$case_id position=$case_number/${#CASE_IDS[@]} result=PASS attempts=$attempts_used"
            ;;
        NOT_PASS)
            NOT_PASS_COUNT=$((NOT_PASS_COUNT + 1))
            COMPLETED_COUNT=$((COMPLETED_COUNT + 1))
            status_message \
                "CASE_COMPLETE case=$case_id position=$case_number/${#CASE_IDS[@]} result=NOT_PASS attempts=$attempts_used"
            ;;
        *)
            INFRA_COUNT=$((INFRA_COUNT + 1))
            INFRA_CASES+=("$case_id")
            status_message \
                "CASE_COMPLETE case=$case_id position=$case_number/${#CASE_IDS[@]} result=INFRA_ERROR attempts=$attempts_used reason=$final_reason"
            ;;
    esac

    printf '%s\t%s\t%d\t%s\t%s\t%s\n' \
        "$case_id" "$case_result" "$attempts_used" "$final_campaign_id" \
        "$final_reason" "$final_log_file" \
        >>"$SUMMARY_FILE"

    if ((case_number < ${#CASE_IDS[@]})); then
        sleep 2
    fi
done

if ((COMPLETED_COUNT > 0)); then
    PASS_RATE="$(awk -v passed="$PASS_COUNT" -v completed="$COMPLETED_COUNT" \
        'BEGIN { printf "%.2f%%", (100 * passed) / completed }')"
else
    PASS_RATE="n/a"
fi

{
    printf '\n结束时间: %s\n' "$(date --iso-8601=seconds)"
    printf '计划: %d\n' "${#CASE_IDS[@]}"
    printf '有效完成: %d\n' "$COMPLETED_COUNT"
    printf 'PASS: %d\n' "$PASS_COUNT"
    printf 'NOT_PASS: %d\n' "$NOT_PASS_COUNT"
    printf 'INFRA_ERROR: %d\n' "$INFRA_COUNT"
    printf '有效结果通过率: %s\n' "$PASS_RATE"
    if ((INFRA_COUNT == 0)); then
        printf '最终通过率: %s\n' "$PASS_RATE"
    else
        printf '最终通过率: unavailable（仍有基础设施失败需要重跑）\n'
        printf '待重跑 case: %s\n' "${INFRA_CASES[*]}"
    fi
} >>"$SUMMARY_FILE"

status_message \
    "RUN_COMPLETE planned=${#CASE_IDS[@]} completed=$COMPLETED_COUNT pass=$PASS_COUNT not_pass=$NOT_PASS_COUNT infra_error=$INFRA_COUNT summary=$SUMMARY_FILE"

printf '\n运行结束：有效完成 %d/%d，PASS=%d，NOT_PASS=%d，INFRA_ERROR=%d。\n' \
    "$COMPLETED_COUNT" "${#CASE_IDS[@]}" "$PASS_COUNT" "$NOT_PASS_COUNT" "$INFRA_COUNT"
printf '有效结果通过率: %s\n' "$PASS_RATE"
printf '汇总文件: %s\n' "$SUMMARY_FILE"

if ((INFRA_COUNT > 0)); then
    exit 2
fi
exit 0
