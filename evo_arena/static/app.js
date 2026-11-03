"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  overview: null,
  detail: null,
  selected: null,
  run: null,
  events: [],
  cursor: 0,
  runSignature: null,
  pendingKey: null,
};
const statuses = {
  queued: "等待启动",
  preparing: "准备环境",
  running: "比赛进行中",
  verifying: "独立验收",
  reporting: "封存集评测",
  completed: "已完成",
  interrupted: "已中断",
  canceled: "已取消",
  accepted: "已晋级",
  rejected: "已拒绝",
  pending: "等待补评",
  generating: "生成候选",
  evaluating: "配对评测",
};
const phases = {
  exploration: "探索比赛",
  admission: "配对准入",
  final_report: "封存集报告",
};
const actions = {
  inspect: "检查配置与服务",
  patch_config: "修改网关配置",
  probe: "探测业务健康",
  submit: "提交独立验收",
};
const reasons = {
  no_strict_success_gain: "没有增加任务通过数，保留原种群",
  strict_success_gain: "任务通过数增加，且没有配对任务退化",
  task_success_regression: "出现原版成功、新版失败的任务",
  paired_execution_interrupted: "配对执行不完整，需要补评",
  invalid_or_unchanged_policy: "策略格式不符合要求，或策略内容没有改变",
};
const eventNames = {
  "season.created": "新赛季已创建",
  "season.preparing": "准备真实任务环境",
  "round.started": "新一轮比赛开始",
  "run.preparing": "启动独立任务环境",
  "run.finished": "任务执行结束",
  "round.ranked": "本轮排名已发布",
  "mutation.started": "选择 Parent，开始生成候选",
  "mutation.generated": "候选生成状态更新",
  "child.promoted": "Child 晋级并接替席位",
  "child.rejected": "Child 未通过准入",
  "child.pending": "候选等待补评",
  "report.started": "开启封存集评测",
  "season.finished": "赛季结束",
  "season.interrupted": "赛季进程中断",
  "run.interrupted": "任务执行中断",
};
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}
function empty(text) {
  return el("p", text, "empty");
}
function num(v) {
  return typeof v === "number" ? v.toLocaleString("zh-CN") : "未知";
}
function version(id) {
  return state.overview?.versions.find((v) => v.id === id);
}
function name(id) {
  return version(id)?.name || id;
}
function verdict(run) {
  return run.status === "completed"
    ? run.passed
      ? "验收通过"
      : "验收失败"
    : statuses[run.status] || run.status;
}
function tone(run) {
  return run.status === "completed"
    ? run.passed
      ? "pass"
      : "fail"
    : "pending";
}
async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function error(message) {
  $("error").hidden = !message;
  $("error").textContent = message || "";
}
function selectSeason(id) {
  if (id === state.selected) return;
  state.selected = id;
  state.cursor = 0;
  state.events = [];
  state.run = null;
  state.runSignature = null;
  state.detail = null;
  state.lineageSignature = null;
  state.runsSignature = null;
  state.feedSignature = null;
}
function renderOverview() {
  const data = state.overview;
  $("model").textContent = data.model?.model || "未配置模型 · 可浏览历史";
  const options = data.seasons.map((s) => `${s.id}:${s.status}`).join("|");
  if ($("season-select").dataset.options !== options) {
    $("season-select").replaceChildren(
      ...(data.seasons.length
        ? data.seasons.map((s) => {
            const n = el(
              "option",
              `${s.id.slice(0, 8)} · ${statuses[s.status] || s.status}`,
            );
            n.value = s.id;
            return n;
          })
        : [el("option", "尚无赛季")]),
    );
    $("season-select").dataset.options = options;
  }
  $("season-select").value = state.selected || "";
  $("start").disabled = !!data.running || !data.model;
  $("cancel").hidden = !data.running;
  $("population").disabled = !!data.running;
  $("rounds").disabled = !!data.running;
  $("feed-status").textContent =
    data.running === state.selected ? "● LIVE" : "↺ REPLAY";
  renderRoster();
}
function renderSeason() {
  const { season: s, runs, mutations, seats } = state.detail;
  $("status").textContent = statuses[s.status] || s.status;
  $("round-label").textContent =
    `第 ${s.round} / ${s.round_limit} 轮 · ${s.population} 个席位`;
  const valid = runs.filter((r) => r.status === "completed");
  $("success").textContent =
    `${valid.filter((r) => r.passed).length} / ${valid.length}`;
  $("mutations").textContent = mutations.length;
  $("promotion-label").textContent =
    `${mutations.filter((m) => m.status === "accepted").length} 次晋级 · ${mutations.filter((m) => m.status === "rejected").length} 次拒绝`;
  $("tokens").textContent = num(s.budget.tokens);
  $("calls").textContent =
    `${s.budget.calls} / ${s.budget.max_calls} 次模型请求`;
  $("sample").textContent = "SMOKE · 单故障端口变体";
  $("agents").replaceChildren(
    ...seats.map((seat, i) => {
      const v = version(seat.version),
        card = el("article", null, "agent"),
        head = el("div", null, "agent-head");
      head.append(el("span", String(i + 1).padStart(2, "0"), "avatar"));
      const title = el("div");
      title.append(
        el("div", v?.name || seat.version, "agent-name"),
        el("div", seat.version, "agent-id"),
      );
      head.append(title);
      card.append(head, el("p", v?.policy || "读取策略中", "agent-policy"));
      const latest = runs.find((r) => r.version === seat.version),
        bottom = el("div", null, "agent-bottom");
      bottom.append(
        el(
          "span",
          latest ? verdict(latest) : "等待参赛",
          latest ? tone(latest) : "pending",
        ),
        el("span", `第 ${seat.revision + 1} 任`, "muted"),
      );
      card.append(bottom);
      return card;
    }),
  );
  const ranking = s.leaderboard || [];
  $("ranking").replaceChildren(
    ...(ranking.length
      ? ranking.map((r) => {
          const tr = el("tr");
          tr.append(
            el("td", String(r.rank).padStart(2, "0")),
            el("td", name(r.version)),
            el("td", r.passed ? "通过" : "失败", r.passed ? "pass" : "fail"),
            el("td", `${r.execution_seconds}s`),
            el("td", num(r.tokens)),
          );
          const td = el("td"),
            btn = el("button", "回放 ↗");
          btn.onclick = () => chooseRun(r.run_id, true);
          td.append(btn);
          tr.append(td);
          return tr;
        })
      : [
          (() => {
            const row = el("tr"),
              td = el("td", "等待完整比赛结果", "empty");
            td.colSpan = 6;
            row.append(td);
            return row;
          })(),
        ]),
  );
  renderLineage();
  renderRuns();
  renderReport();
}
function renderLineage() {
  const list = state.detail.mutations;
  const signature = JSON.stringify([
    list,
    state.detail.decisions,
    list.map((m) => [version(m.parent), version(m.child)]),
  ]);
  if (signature === state.lineageSignature) return;
  state.lineageSignature = signature;
  $("lineage").replaceChildren(
    ...(list.length
      ? list.map((m) => {
          const row = el("article", null, "mutation"),
            parent = el("div", null, "gene");
          parent.append(
            el("small", "PARENT"),
            el("strong", name(m.parent)),
            el("p", m.parent),
          );
          row.append(parent, el("div", "→", "arrow"));
          const child = el("div", null, "gene child"),
            head = el("div", null, "decision-row");
          head.append(
            el("strong", m.child ? name(m.child) : "正在生成 Child"),
            el(
              "span",
              statuses[m.status] || m.status,
              `decision ${m.status === "accepted" ? "pass" : m.status === "rejected" ? "fail" : "pending"}`,
            ),
          );
          child.append(head);
          if (m.child) {
            child.append(
              el("small", m.child),
              el("p", version(m.child)?.hypothesis || "", "hypothesis"),
            );
          }
          if (m.reason)
            child.append(el("p", reasons[m.reason] || m.reason, "reason"));
          if (m.diff) {
            const d = el("details");
            d.append(el("summary", "查看实际策略差异"), el("pre", m.diff));
            child.append(d);
          }
          const decision = state.detail.decisions.find(
            (d) => d.mutation === m.id,
          );
          if (decision) {
            child.append(
              el(
                "p",
                `配对通过数：Parent ${decision.parent_successes ?? "—"} / Child ${decision.child_successes ?? "—"}`,
                "hypothesis",
              ),
            );
            const links = el("div");
            for (const [id, label] of [
              [decision.parent_run, "Parent 回放"],
              [decision.child_run, "Child 回放"],
            ]) {
              const b = el("button", label);
              b.onclick = () => chooseRun(id, true);
              links.append(b);
            }
            child.append(links);
          }
          row.append(child);
          return row;
        })
      : [empty("首轮比赛完成后，进化器会选择 Parent 并生成一个 Child。")]),
  );
}
function renderFeed() {
  const events = state.events
    .filter((e) => e.kind !== "budget.updated")
    .slice(-32)
    .reverse();
  const signature = JSON.stringify(events);
  if (signature === state.feedSignature) return;
  state.feedSignature = signature;
  $("feed").replaceChildren(
    ...(events.length
      ? events.map((e) => {
          const item = el(
            "div",
            null,
            `feed-item ${e.kind === "agent.action" ? "action" : e.kind.includes("mutation") || e.kind.includes("child.") ? "mutation" : ""}`,
          );
          item.append(
            el(
              "small",
              new Date(e.created).toLocaleTimeString("zh-CN", {
                hour12: false,
                timeZone: "Asia/Shanghai",
              }),
            ),
          );
          let text = eventNames[e.kind] || e.kind;
          if (e.kind === "agent.action")
            text = `${name(e.data.version)} · ${actions[e.data.action] || e.data.action}${e.data.summary ? "：" + e.data.summary : ""}`;
          if (e.kind === "run.finished")
            text += ` · ${e.data.status === "completed" ? (e.data.passed ? "通过" : "失败") : statuses[e.data.status] || e.data.status}`;
          item.append(el("p", text));
          if (e.data.run_id) {
            const b = el("button", e.data.run_id + " ↗");
            b.onclick = () => chooseRun(e.data.run_id, true);
            item.append(b);
          }
          return item;
        })
      : [empty("等待执行事件")]),
  );
}
function renderRuns() {
  const runs = state.detail.runs;
  const signature = JSON.stringify([
    state.run,
    runs.map((r) => [r.id, r.status, r.passed]),
  ]);
  if (signature === state.runsSignature) return;
  state.runsSignature = signature;
  $("runs").replaceChildren(
    ...(runs.length
      ? runs.map((r) => {
          const b = el(
            "button",
            name(r.version),
            `run-btn ${r.id === state.run ? "selected" : ""}`,
          );
          b.append(
            el("span", `${phases[r.phase] || r.phase} · 第 ${r.round} 轮`),
            el("span", verdict(r), tone(r)),
          );
          b.onclick = () => chooseRun(r.id, false);
          return b;
        })
      : [empty("还没有执行记录")]),
  );
  if (!state.run && runs.length) chooseRun(runs[0].id, false);
}
async function chooseRun(id, scroll) {
  if (id !== state.run) {
    state.run = id;
    state.runSignature = null;
  }
  renderRuns();
  await refreshRun();
  if (scroll)
    $("replay").scrollIntoView({ behavior: "smooth", block: "start" });
}
async function refreshRun() {
  if (!state.run) return;
  const requested = state.run;
  try {
    const detail = await api("/api/runs/" + encodeURIComponent(requested));
    if (state.run !== requested) return;
    const signature = JSON.stringify([
      detail.run.status,
      detail.events.at(-1)?.seq,
    ]);
    if (signature === state.runSignature) return;
    state.runSignature = signature;
    renderTrace(detail);
  } catch (e) {
    error(e.message);
  }
}
function renderTrace({ run, events }) {
  $("replay-status").textContent = verdict(run);
  const list = [
    el(
      "div",
      `${run.id} · ${phases[run.phase]} · ${run.execution_seconds ?? "—"}s · ${num(run.usage.tokens)} token`,
      "trace-meta",
    ),
  ];
  for (const e of events) {
    if (e.kind === "action.completed") {
      const d = e.data,
        step = el("article", null, "step"),
        label = el("div", null, "step-label");
      label.append(
        el("strong", String(d.step).padStart(2, "0")),
        el("span", actions[d.action.action] || d.action.action),
      );
      step.append(label, el("p", d.action.summary || "执行受限业务操作"));
      const details = el("details");
      details.append(
        el("summary", "查看动作与工具结果"),
        el(
          "pre",
          JSON.stringify(
            { action: d.action, result: d.result, usage: d.usage },
            null,
            2,
          ),
        ),
      );
      step.append(details);
      list.push(step);
    } else if (e.kind === "action.invalid")
      list.push(el("p", "模型输出了不符合工具协议的动作。", "fail"));
  }
  if (run.verification) {
    const checks = el("div", null, "checks");
    checks.append(el("div", "独立业务验收", "muted"));
    const labels = {
      upstream_health: "订单依赖健康",
      create_order: "创建真实订单",
      read_order: "查询订单一致性",
      dependency_constraint: "依赖配置约束",
    };
    for (const t of run.verification.tests)
      checks.append(
        el(
          "div",
          `${t.passed ? "✓" : "×"} ${labels[t.name] || t.name}`,
          `check ${t.passed ? "pass" : "fail"}`,
        ),
      );
    list.push(checks);
  }
  if (run.error)
    list.push(
      el("p", `${run.error.category} · ${run.error.reason}`, "pending"),
    );
  if (!events.length) list.push(empty("正在准备独立环境，尚无 Agent 动作。"));
  $("trace").replaceChildren(...list);
}
function renderReport() {
  const rows = state.detail.season.final_report || [];
  $("report").replaceChildren(
    ...(rows.length
      ? [
          (() => {
            const wrap = el("div", null, "report-list");
            for (const r of rows) {
              const card = el("article", null, "report-item");
              card.append(
                el("strong", name(r.version)),
                el("small", r.initial ? "初始版本" : "赛季最终版本"),
                el(
                  "p",
                  r.status === "completed"
                    ? r.passed
                      ? "验收通过"
                      : "验收失败"
                    : "评测未完成",
                  r.status === "completed"
                    ? r.passed
                      ? "pass"
                      : "fail"
                    : "pending",
                ),
              );
              const b = el("button", "查看证据");
              b.onclick = () => chooseRun(r.run_id, true);
              card.append(b);
              wrap.append(card);
            }
            return wrap;
          })(),
        ]
      : [
          empty(
            "封存集将在赛季结束后开启。报告只检查同类故障的端口变体，不代表跨故障泛化。",
          ),
        ]),
  );
}
let refreshing = false;
function selectedVersions() {
  return Array.from(
    document.querySelectorAll("#version-picker input:checked"),
    (n) => n.value,
  );
}
function renderRoster() {
  const custom = $("roster").value === "custom";
  $("version-picker").hidden = !custom;
  $("population").disabled = custom || !!state.overview?.running;
  const signature = JSON.stringify(
    state.overview?.versions.map((v) => [v.id, v.name]),
  );
  if (signature === state.rosterSignature) return;
  state.rosterSignature = signature;
  const checked = new Set(selectedVersions());
  const hint = el("span", "勾选 2 或 4 个版本：", "muted");
  const options = (state.overview?.versions || []).map((v) => {
    const label = el("label"),
      input = el("input");
    input.type = "checkbox";
    input.value = v.id;
    input.checked = checked.has(v.id);
    input.onchange = renderPlannedBudget;
    label.append(input, el("span", v.name), el("small", v.id));
    return label;
  });
  $("version-picker").replaceChildren(hint, ...options);
}
$("roster").onchange = () => {
  renderRoster();
  renderPlannedBudget();
};
$("register-form").onsubmit = async (event) => {
  event.preventDefault();
  $("register-submit").disabled = true;
  error("");
  try {
    const result = await api("/api/versions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-EvoArena-Request": "1",
      },
      body: JSON.stringify({
        name: $("policy-name").value,
        policy: $("policy-text").value,
      }),
    });
    $("register-result").textContent = `已注册 ${result.name} · ${result.id}`;
    await refresh();
  } catch (e) {
    error("注册失败：" + e.message);
  } finally {
    $("register-submit").disabled = false;
  }
};
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state.overview = await api("/api/overview");
    if ($("error").textContent.startsWith("读取失败：")) error("");
    if (!state.selected && state.overview.seasons.length)
      selectSeason(state.overview.seasons[0].id);
    renderOverview();
    if (state.selected) {
      const selected = state.selected;
      const detail = await api("/api/seasons/" + encodeURIComponent(selected));
      if (selected !== state.selected) return;
      state.detail = detail;
      renderSeason();
      const events = await api(
        `/api/events?scope=${encodeURIComponent(selected)}&after=${state.cursor}`,
      );
      if (selected !== state.selected) return;
      state.events.push(...events.events);
      if (events.events.length) state.cursor = events.events.at(-1).seq;
      renderFeed();
      await refreshRun();
    }
  } catch (e) {
    error("读取失败：" + e.message);
  } finally {
    refreshing = false;
  }
}
$("season-select").onchange = (e) => {
  selectSeason(e.target.value);
  refresh();
};
function plannedCalls() {
  const chosen = selectedVersions();
  const p =
      $("roster").value === "custom" && chosen.length
        ? chosen.length
        : Number($("population").value),
    r = Number($("rounds").value);
  return Math.min(128, 3 * (p * (r + 1) + 3 * (r - 1)) + (r - 1) + 4);
}
function renderPlannedBudget() {
  $("notice").textContent =
    `真实模型实验 · 每季最多 ${plannedCalls()} 次请求、220,000 token，单环境顺序运行。Token 达阈值后停止新请求，进行中的一次请求可能超出阈值。`;
}
$("population").onchange = renderPlannedBudget;
$("rounds").onchange = renderPlannedBudget;
$("start").onclick = async () => {
  error("");
  const chosen = selectedVersions();
  if ($("roster").value === "custom" && ![2, 4].includes(chosen.length)) {
    error("请勾选 2 或 4 个不同的策略版本。");
    return;
  }
  $("start").disabled = true;
  state.pendingKey ||= crypto.randomUUID();
  try {
    const data = await api("/api/seasons", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-EvoArena-Request": "1",
        "Idempotency-Key": state.pendingKey,
      },
      body: JSON.stringify({
        population: Number($("population").value),
        rounds: Number($("rounds").value),
        max_calls: plannedCalls(),
        max_tokens: 220000,
        ...($("roster").value === "custom" ? { version_ids: chosen } : {}),
      }),
    });
    state.pendingKey = null;
    selectSeason(data.id);
    await refresh();
  } catch (e) {
    error("启动失败：" + e.message);
    $("start").disabled = false;
  }
};
$("cancel").onclick = async () => {
  try {
    await api("/api/cancel", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-EvoArena-Request": "1",
      },
      body: "{}",
    });
    $("notice").textContent =
      "已请求取消。当前模型请求返回后会停止，环境清理和记录保存完成后更新状态。";
  } catch (e) {
    error(e.message);
  }
};
refresh();
setInterval(refresh, 2000);
