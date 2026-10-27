/** Enforce the benchmark's continuous worker lanes without driving Search itself. */
export default function parallelExecution(pi) {
  const deadline = Date.parse(process.env.BENCH_GOAL_PLUS_EXPLORATION_DEADLINE_AT || "");
  if (!Number.isFinite(deadline)) return;
  const lanes = new Map();
  const pending = new Map();
  let controlledStop = false;
  const shutdown = new Set([
    "goal_plus_session_interrupt", "goal_plus_session_stop", "goal_plus_session_close",
  ]);
  const failedStates = new Set(["failed", "error", "timed_out", "interrupted", "stopped"]);
  const closeout = new Set([
    "goal_plus_search_select", "goal_plus_search_promote",
    "goal_plus_search_apply_promotion", "goal_plus_record_search_result",
  ]);

  function observe(message, input = {}) {
    if (message.isError) return;
    let result = message.details;
    if (!result || typeof result !== "object") {
      try {
        result = JSON.parse(message.content.find((item) => item.type === "text").text);
      } catch { return; }
    }
    if (result.ok === false) return;
    if (message.toolName === "goal_plus_set_status") {
      controlledStop = ["blocked", "paused", "aborted", "stopped"].includes(input.status);
    }
    if (message.toolName === "goal_plus_session_run" && result.session_id) {
      controlledStop = false;
      for (const [id, lane] of lanes) {
        if (id !== result.session_id && lane.run_id === input.run_id
            && lane.candidate_id === input.candidate_id) lanes.delete(id);
      }
      lanes.set(result.session_id, {
        session_id: result.session_id, candidate_id: input.candidate_id,
        run_id: input.run_id, call_id: result.call_id, state: result.state,
      });
    }
    if (message.toolName === "goal_plus_session_wait_any") {
      for (const receipt of [...(result.running || []), ...(result.finished || [])]) {
        const lane = lanes.get(receipt.session_id);
        // An old receipt must never retire a newer invocation of the same session.
        if (lane && lane.call_id === receipt.call_id) lane.state = receipt.state;
      }
    }
  }

  pi.on("session_start", (_event, ctx) => {
    lanes.clear();
    pending.clear();
    controlledStop = false;
    for (const entry of ctx.sessionManager.getEntries()) {
      const message = entry.message;
      if (!message) continue;
      if (message.role === "assistant") {
        for (const item of message.content || []) {
          if (item.type === "toolCall") pending.set(item.id, item.arguments);
        }
      } else if (message.role === "toolResult") {
        observe(message, pending.get(message.toolCallId));
      }
    }
  });
  pi.on("tool_result", (event) => observe(event, event.input));
  pi.on("tool_call", (event) => {
    if (Date.now() >= deadline) return;
    const remaining = Math.ceil((deadline - Date.now()) / 1000);
    const idle = [...lanes.values()].filter((lane) => lane.state !== "running");
    const lane = lanes.get(event.input.session_id);
    if (shutdown.has(event.toolName) && lane && !controlledStop && !failedStates.has(lane.state)) {
      return { block: true, reason:
        `Actual UTC is ${new Date().toISOString()}; exploration ends at ${new Date(deadline).toISOString()} `
        + `(${remaining}s remain). Do not interrupt, stop, or close a healthy worker early. `
        + "Continue the same candidate/session. Failed invocation recovery is allowed; "
        + "for a real user pause or dependency blocker, first record the Goal control state." };
    }
    const completing = event.toolName === "goal_plus_set_status"
      && event.input.status === "complete";
    if (closeout.has(event.toolName) || completing) {
      return { block: true, reason:
        `Benchmark exploration has ${remaining}s remaining. Each worker budget is PER INVOCATION, `
        + "not a cumulative candidate lifetime. Continue the same candidates with new session_run "
        + "call_ids; do not select, close lanes, or complete before the exploration deadline. "
        + "User stop/pause and genuine dependency blockers remain available through Goal control." };
    }
    if (!controlledStop && event.toolName === "goal_plus_session_wait_any" && idle.length
        && event.input.timeout_seconds !== 0) {
      return { block: true, reason:
        "Resume returned lanes BEFORE waiting for the other workers: "
        + JSON.stringify(idle) + ". Read their durable Evidence, then use session_run with "
        + "a new call_id and new_session=false. Budget is per invocation. "
        + "A zero-time detail poll is allowed; waiting for the whole batch is not." };
    }
  });
}
