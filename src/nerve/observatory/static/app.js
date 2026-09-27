const $ = (id) => document.getElementById(id);

const state = {
  summary: null,
  snapshots: [],
  selectedSnapshotId: null,
  detail: null,
  calibration: [],
  health: null,
  refreshing: false,
};

export function displayValue(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "Unavailable";
  return `${value}${suffix}`;
}

function short(value, left = 7, right = 5) {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const text = String(value);
  return text.length > left + right + 3
    ? `${text.slice(0, left)}…${text.slice(-right)}`
    : text;
}

function formatTime(value) {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? String(value)
    : new Intl.DateTimeFormat(undefined, {
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(date);
}

function formatMetric(value, digits = 3, suffix = "") {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return `${number.toFixed(digits)}${suffix}`;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function clear(element) {
  element.replaceChildren();
  return element;
}

async function api(path) {
  const response = await fetch(`/api/observatory${path}`, {
    method: "GET",
    headers: { Accept: "application/json" },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed: ${response.status}`);
  return body;
}

function metricCard(label, value) {
  const card = node("div", "metric-card");
  card.append(node("span", "", label), node("strong", "", displayValue(value)));
  return card;
}

function renderStatus() {
  const summary = state.summary;
  if (!summary) return;
  const metrics = [
    ["Snapshots", summary.snapshot_count],
    ["Unique tokens", summary.unique_token_count],
    ["Unique pools", summary.unique_pool_count],
    ["Decisions", summary.decision_count],
    ["Expired", summary.expired_decision_count],
    ["Unresolved", summary.unresolved_outcome_count],
    ["Measurement errors", summary.measurement_error_count],
    ["Meme states", summary.memecoin_state?.observation_count],
    ["Meme-state successes", summary.memecoin_state?.successful_count],
    ["Meme-state failures", summary.memecoin_state?.failure_count],
    ["State median latency", displayValue(summary.memecoin_state?.median_latency_ms, " ms")],
    ["State p95 latency", displayValue(summary.memecoin_state?.p95_latency_ms, " ms")],
    ["Chronology observations", summary.chronology?.observation_count],
    ["Complete chronologies", summary.chronology?.complete_count],
    ["Partial chronologies", summary.chronology?.partial_count],
    ["Latest observation", formatTime(summary.latest_observation_at)],
  ];
  clear($("statusGrid")).append(...metrics.map(([label, value]) => metricCard(label, value)));
  const empty = $("emptyState");
  empty.hidden = summary.snapshot_count !== 0;
  empty.textContent =
    "NERVE Phase 0 is installed. No measurement snapshots have been recorded in this database yet. Run `nerve paper-scan` with this DB_PATH to record safe paper observations.";
  const arms = summary.arms || [];
  $("armBadge").textContent = arms.length
    ? arms.map((arm) => arm.display_name).join(" · ")
    : "NO ARMS RECORDED";
}

function renderSnapshots() {
  const tape = clear($("snapshotTape"));
  $("snapshotCount").textContent = `${state.snapshots.length} rows`;
  if (!state.snapshots.length) {
    tape.append(node("p", "empty-state", "No snapshots have been recorded."));
    return;
  }
  for (const snapshot of state.snapshots) {
    const button = node("button", "snapshot-row");
    button.type = "button";
    if (snapshot.snapshot_id === state.selectedSnapshotId) button.classList.add("selected");
    const identity = node("span");
    identity.append(
      node("strong", "", `${formatTime(snapshot.observed_at)} · ${snapshot.chain}`),
      node("small", "", `${short(snapshot.token)} · pool ${short(snapshot.pool)}`),
      node("small", "", `snap ${short(snapshot.snapshot_id)} · impulse ${short(snapshot.impulse_id)}`),
    );
    const truth = node("span", "tape-state");
    truth.append(
      node("strong", `state-${snapshot.outcome_state}`, snapshot.outcome_state.toUpperCase()),
      node("small", "", `${snapshot.decision_arm_count} arm${snapshot.decision_arm_count === 1 ? "" : "s"}`),
      node("small", "", snapshot.stage),
    );
    button.append(identity, truth);
    button.addEventListener("click", () => selectSnapshot(snapshot.snapshot_id));
    tape.append(button);
  }
}

function identityCell(label, value) {
  const cell = node("div", "identity-cell");
  cell.append(node("span", "", label), node("strong", "", displayValue(value)));
  return cell;
}

function timelineItem(label, timestamp, detail, tone = "") {
  const item = node("article", `timeline-item ${tone}`.trim());
  const top = node("div", "timeline-top");
  top.append(node("strong", "", label), node("time", "", formatTime(timestamp)));
  item.append(top, node("p", "", detail));
  return item;
}

function horizonLabel(seconds) {
  return ({ 60: "+1m", 300: "+5m", 900: "+15m", 1800: "+30m" })[seconds] || `+${seconds}s`;
}

function renderDetail() {
  const detail = state.detail;
  const identity = clear($("snapshotIdentity"));
  const trace = clear($("decisionTrace"));
  if (!detail) {
    $("traceTitle").textContent = "Select a snapshot";
    $("detailStage").textContent = "Unavailable";
    trace.append(node("p", "empty-state", "Choose an observation from the snapshot tape."));
    renderMemecoinState();
    renderChronology();
    return;
  }
  const snapshot = detail.snapshot;
  $("traceTitle").textContent = `${snapshot.chain} · ${short(snapshot.token, 10, 6)}`;
  $("detailStage").textContent = displayValue(snapshot.stage);
  identity.append(
    identityCell("Snapshot ID", snapshot.snapshot_id),
    identityCell("Impulse ID", snapshot.impulse_id),
    identityCell("Input hash", short(snapshot.input_hash, 12, 8)),
    identityCell("Pool", short(snapshot.pool, 12, 8)),
  );

  const events = [
    {
      label: "OBSERVED",
      timestamp: snapshot.observed_at,
      detail: `Market observation entered NERVE · ${snapshot.chain}`,
      tone: "",
    },
    {
      label: "SENTINEL SNAPSHOT",
      timestamp: snapshot.captured_at,
      detail: `${snapshot.stage} · input ${short(snapshot.input_hash, 12, 8)}`,
      tone: "success",
    },
  ];
  for (const decision of detail.decisions || []) {
    events.push({
      label: `${decision.display_arm} · START`,
      timestamp: decision.started_at,
      detail: `${decision.strategy_id} · question ${decision.question_version}`,
      tone: "",
    });
    events.push({
      label: `${decision.display_arm} · ${String(decision.status).toUpperCase()}`,
      timestamp: decision.completed_at,
      detail: `${decision.latency_ms} ms · ${displayValue(decision.reason)}`,
      tone: decision.status === "expired" || decision.status === "failed" ? "error" : "success",
    });
    events.push({
      label: `${decision.display_arm} · DEADLINE`,
      timestamp: decision.deadline_at,
      detail: decision.applied_at
        ? `Applied ${formatTime(decision.applied_at)}`
        : decision.status === "abstained"
          ? "ABSTAINED · no execution instruction"
          : "No applied timestamp recorded",
      tone: decision.status === "expired" ? "error" : "",
    });
  }
  for (const execution of detail.execution_observations || []) {
    events.push({
      label: `${execution.arm_id} · QUOTE / EXECUTION OBSERVATION`,
      timestamp: execution.quote_at,
      detail: `quote ${displayValue(execution.quote_price)} · obtainable ${displayValue(execution.obtainable_quantity)} · status ${execution.status}`,
      tone: execution.executable_entry === null ? "" : "success",
    });
  }
  for (const outcome of detail.forward_outcome_events || []) {
    events.push({
      label: `${horizonLabel(outcome.horizon_seconds)} · ${String(outcome.status).toUpperCase()}`,
      timestamp: outcome.resolved_at || outcome.target_at,
      detail: `event recorded ${formatTime(outcome.event_recorded_at)} · return ${formatMetric(outcome.return_pct, 3, "%")} · source ${outcome.source}`,
      tone: outcome.status === "resolved" ? "success" : "",
    });
  }
  events.sort((a, b) => {
    const left = Date.parse(a.timestamp || "") || Number.MAX_SAFE_INTEGER;
    const right = Date.parse(b.timestamp || "") || Number.MAX_SAFE_INTEGER;
    return left - right;
  });
  trace.append(...events.map((event) => timelineItem(event.label, event.timestamp, event.detail, event.tone)));
  renderMemecoinState();
  renderChronology();
}

const chronologyFactGroups = {
  Creation: ["creation_signature", "creation_slot", "creation_block_time", "creation_user", "creation_creator", "creation_mint"],
  Activity: ["observed_buy_event_count", "observed_unique_buyers_count", "observed_sell_event_count", "observed_unique_sellers_count", "unique_buyers_since_creation"],
  "Early cohort": ["early_cohort_target_n", "early_cohort_actual_n", "early_cohort_cutoff_slot", "early_cohort_wallets", "early_cohort_balance_coverage_pct", "early_cohort_current_supply_pct"],
  "Same-slot": ["multi_buyer_slot_count", "max_distinct_buyers_same_slot", "buy_events_in_multi_buyer_slots", "multi_buyer_slot_event_share_pct"],
  Funding: ["funding_probe_enabled", "funding_coverage_pct", "shared_funder_group_count", "creation_user_funded_early_buyer_count", "creation_creator_funded_early_buyer_count"],
  "Creator history": ["creator_history_probe_enabled", "creator_observed_prior_launch_count", "creator_history_truncated"],
};

function renderChronology() {
  const metrics = clear($("chronologyMetrics"));
  const factsPanel = clear($("chronologyFacts"));
  const timeline = clear($("chronologyTimeline"));
  const chronology = state.detail?.chronology;
  const latest = chronology?.latest;
  if (!latest) {
    $("chronologyBadge").textContent = "Unavailable";
    factsPanel.append(node("p", "empty-state", "No usable chronology collection is recorded for this snapshot."));
    return;
  }
  $("chronologyBadge").textContent = String(latest.coverage_status).toUpperCase();
  metrics.append(
    metricCard("Ready", formatTime(latest.ready_at)),
    metricCard("Source cutoff", formatTime(latest.source_cutoff_at)),
    metricCard("Signatures", latest.signature_count),
    metricCard("Transactions", latest.transaction_fetch_count),
    metricCard("Decode failures", latest.decode_failure_count),
    metricCard("Creation reached", Boolean(latest.reached_creation)),
    metricCard("History truncated", Boolean(latest.history_truncated)),
    metricCard("Attempts", chronology.history?.length || 0),
  );
  const byName = new Map((chronology.facts || []).map((fact) => [fact.field_name, fact]));
  for (const [groupName, names] of Object.entries(chronologyFactGroups)) {
    const group = node("section", "fact-group");
    group.append(node("h3", "", groupName));
    const table = node("div", "fact-table");
    for (const name of names) {
      const fact = byName.get(name);
      if (!fact) continue;
      const row = node("div", "fact-row");
      row.append(node("span", "fact-name", name),
        node("strong", "fact-value", displayValue(fact.value, fact.unit ? ` ${fact.unit}` : "")),
        node("span", `fact-status status-${fact.status}`, String(fact.status).toUpperCase()));
      table.append(row);
    }
    group.append(table);
    factsPanel.append(group);
  }
  for (const event of chronology.events || []) {
    const actor = event.user || event.creation_user || event.creator || "";
    timeline.append(timelineItem(String(event.event_type).toUpperCase(), event.block_time,
      `${short(actor, 10, 6)} · slot ${event.slot} · ${event.instruction_path}`, ""));
  }
}

const factGroups = {
  Market: [
    "dex_id", "pair_address", "pair_selection_reason", "pair_created_at", "pair_age_ms", "price_native",
    "price_usd", "liquidity_usd", "liquidity_base", "liquidity_quote", "fdv_usd",
    "market_cap_usd", "m5_buys", "m5_sells", "h1_buys", "h1_sells", "h24_buys",
    "h24_sells", "volume_m5", "volume_h1", "volume_h24", "price_change_m5_pct",
    "price_change_h1_pct", "price_change_h24_pct",
  ],
  Ownership: [
    "token_supply_raw", "token_decimals", "top1_token_accounts_pct",
    "top5_token_accounts_pct", "top10_token_accounts_pct", "top20_token_accounts_pct",
    "top20_accounts_owner_coverage_pct", "largest_owner_within_top20_pct",
    "top5_owners_within_top20_pct", "unique_buyers", "sniper_supply_pct", "bundler_supply_pct",
  ],
  Pump: [
    "bonding_curve_pda", "pump_program_owned", "bonding_curve_exists",
    "virtual_token_reserves", "virtual_quote_reserves", "real_token_reserves",
    "real_quote_reserves", "token_total_supply", "curve_complete", "coin_creator",
    "is_mayhem_mode", "is_cashback_coin", "quote_mint", "creator_fee_bps",
    "is_holder_reward", "canonical_pumpswap_verified", "pump_lifecycle_state",
  ],
};

function renderMemecoinState() {
  const panel = clear($("memestateGroups"));
  const metrics = clear($("memestateMetrics"));
  const memestate = state.detail?.memecoin_state;
  const latest = memestate?.latest;
  if (!latest) {
    $("memestateBadge").textContent = "Unavailable";
    panel.append(node("p", "empty-state", "No successful memecoin-state collection is recorded for this snapshot."));
    return;
  }
  $("memestateBadge").textContent = String(latest.status).toUpperCase();
  metrics.append(
    metricCard("State version", latest.state_version),
    metricCard("Ready", formatTime(latest.ready_at)),
    metricCard("Latency", displayValue(latest.latency_ms, " ms")),
    metricCard("Attempts", memestate.history?.length || 0),
  );
  const byName = new Map((memestate.facts || []).map((fact) => [fact.field_name, fact]));
  for (const [groupName, names] of Object.entries(factGroups)) {
    const group = node("section", "fact-group");
    group.append(node("h3", "", groupName));
    const table = node("div", "fact-table");
    for (const name of names) {
      const fact = byName.get(name);
      if (!fact) continue;
      const row = node("details", "fact-row");
      const summary = node("summary");
      summary.append(
        node("span", "fact-name", name),
        node("strong", "fact-value", displayValue(fact.value, fact.unit ? ` ${fact.unit}` : "")),
        node("span", `fact-status status-${fact.status}`, String(fact.status).toUpperCase()),
      );
      const provenance = node("div", "provenance");
      provenance.append(
        identityCell("Source", fact.source),
        identityCell("Source observed", formatTime(fact.source_observed_at)),
        identityCell("Fetched", formatTime(fact.fetched_at)),
        identityCell("Age", fact.age_ms === null ? null : `${fact.age_ms} ms`),
        identityCell("Reason", fact.reason),
        identityCell("Details", fact.details ? JSON.stringify(fact.details) : null),
      );
      row.append(summary, provenance);
      table.append(row);
    }
    group.append(table);
    panel.append(group);
  }
}

function renderForwardLab() {
  const grid = clear($("forwardGrid"));
  const horizons = state.summary?.forward_lab || [];
  for (const horizon of horizons) {
    const card = node("article", "horizon-card");
    card.append(node("h3", "", horizonLabel(horizon.horizon_seconds)));
    const values = node("div", "horizon-values");
    const entries = [
      ["Resolved", horizon.resolved_count],
      ["Pending", horizon.pending_count],
      ["Unavailable", horizon.unavailable_count],
      ["Mean return", formatMetric(horizon.mean_return_pct, 3, "%")],
      ["Median return", formatMetric(horizon.median_return_pct, 3, "%")],
      ["Positive rate", formatMetric(horizon.positive_return_rate, 3)],
    ];
    for (const [label, value] of entries) {
      const block = node("div");
      block.append(node("span", "", label), node("strong", "", displayValue(value)));
      values.append(block);
    }
    card.append(values);
    grid.append(card);
  }
  const strip = clear($("armComparison"));
  const arms = state.summary?.arms || [];
  if (!arms.length) {
    strip.append(node("span", "arm-note", "No experiment arms have been recorded."));
    return;
  }
  for (const arm of arms) {
    const note = arm.baseline_kind === "abstention"
      ? `${arm.display_name} · abstention baseline; no alpha inference`
      : `${arm.display_name} · ${arm.decision_count} decisions`;
    strip.append(node("span", "arm-note", note));
  }
}

function renderCalibration() {
  const panel = clear($("calibrationPanel"));
  if (!state.calibration.length) {
    panel.append(
      node("p", "calibration-empty", "No calibrated probability arms have been recorded yet."),
    );
    return;
  }
  for (const group of state.calibration) {
    const card = node("article", "calibration-card");
    card.append(
      node(
        "h3",
        "",
        `${group.model_id} · ${group.question_version} · ${horizonLabel(group.horizon_seconds)}`,
      ),
    );
    const metrics = node("div", "calibration-metrics");
    for (const [label, value] of [
      ["Samples", group.sample_count],
      ["Brier", formatMetric(group.brier)],
      ["Log loss", formatMetric(group.log_loss)],
      ["ECE", formatMetric(group.ece)],
    ]) {
      const block = node("div");
      block.append(node("span", "", label), node("strong", "", displayValue(value)));
      metrics.append(block);
    }
    const chart = node("div", "reliability");
    chart.setAttribute("aria-label", "Raw model probability and empirical outcome rate bins");
    for (const bin of group.reliability_bins) {
      const bar = node("div", "reliability-bar");
      const probability = bin.mean_probability === null ? 0 : Number(bin.mean_probability);
      const outcomeRate = bin.positive_rate === null ? 0 : Number(bin.positive_rate);
      bar.style.height = `${Math.max(3, probability * 100)}%`;
      bar.style.setProperty("--outcome-height", `${Math.max(0, outcomeRate * 100)}%`);
      bar.title = bin.count
        ? `raw probability ${formatMetric(bin.mean_probability)} · empirical outcome rate ${formatMetric(bin.positive_rate)} · n=${bin.count}`
        : "Empty reliability bin";
      chart.append(bar);
    }
    card.append(metrics, chart);
    panel.append(card);
  }
}

function healthCard(label, value) {
  const card = node("div", "health-card");
  card.append(node("span", "", label), node("strong", "", displayValue(value)));
  return card;
}

function renderHealth() {
  const health = state.health;
  if (!health) return;
  $("healthBadge").textContent = health.failure_count ? `${health.failure_count} failures` : "No failures recorded";
  $("healthBadge").classList.toggle("error", Boolean(health.failure_count));
  clear($("healthGrid")).append(
    healthCard("Capture success", health.capture_success_count),
    healthCard("Capture failure", health.capture_failure_count),
    healthCard("Outcome failures", health.outcome_failure_count),
    healthCard("Arm failures", health.arm_failure_count),
    healthCard("Meme-state failures", health.memestate_failure_count),
    healthCard("Chronology failures", health.chronology_failure_count),
  );
  const events = clear($("healthEvents"));
  const failures = (health.recent_events || []).filter((event) => event.kind !== "capture_ok");
  if (!health.available) {
    events.append(node("p", "empty-state", "Measurement health records are unavailable in this database schema."));
  } else if (!failures.length) {
    events.append(node("p", "empty-state", "No measurement failures have been recorded."));
  } else {
    for (const event of failures.slice(0, 8)) {
      const row = node("div", "health-event");
      row.append(
        node("time", "", formatTime(event.recorded_at)),
        node("strong", "", event.kind),
        node("span", "", `${event.stage} · ${displayValue(event.message)}`),
      );
      events.append(row);
    }
  }
}

async function selectSnapshot(snapshotId) {
  state.selectedSnapshotId = snapshotId;
  renderSnapshots();
  try {
    state.detail = await api(`/snapshot/${encodeURIComponent(snapshotId)}`);
    renderDetail();
  } catch (error) {
    state.detail = null;
    renderDetail();
    showConnectionError(error);
  }
}

function showConnectionError(error) {
  $("connectionBadge").textContent = error instanceof Error ? error.message : "Truth store unavailable";
  $("connectionBadge").classList.add("error");
}

async function refreshAll() {
  if (state.refreshing) return;
  state.refreshing = true;
  $("refreshButton").disabled = true;
  try {
    const [summary, snapshots, calibration, health] = await Promise.all([
      api("/summary"),
      api("/snapshots?limit=100"),
      api("/calibration"),
      api("/health"),
    ]);
    state.summary = summary;
    state.snapshots = snapshots;
    state.calibration = calibration;
    state.health = health;
    $("connectionBadge").textContent = "Local truth connected";
    $("connectionBadge").classList.remove("error");
    if (
      !state.selectedSnapshotId ||
      !snapshots.some((snapshot) => snapshot.snapshot_id === state.selectedSnapshotId)
    ) {
      state.selectedSnapshotId = snapshots[0]?.snapshot_id || null;
    }
    renderStatus();
    renderSnapshots();
    renderForwardLab();
    renderCalibration();
    renderHealth();
    if (state.selectedSnapshotId) {
      state.detail = await api(`/snapshot/${encodeURIComponent(state.selectedSnapshotId)}`);
    } else {
      state.detail = null;
    }
    renderDetail();
    $("lastRefresh").textContent = `Refreshed ${formatTime(new Date().toISOString())}`;
  } catch (error) {
    showConnectionError(error);
  } finally {
    state.refreshing = false;
    $("refreshButton").disabled = false;
  }
}

if (typeof document !== "undefined") {
  $("refreshButton").addEventListener("click", refreshAll);
  refreshAll();
  window.setInterval(refreshAll, 5000);
}
