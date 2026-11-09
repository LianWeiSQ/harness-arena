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
  pendingStart: null,
  starting: false,
  comparisonRound: null,
  focusVersion: null,
  metric: "execution_seconds",
};
const statuses = {
  queued: "等待启动",
  preparing: "准备环境",
  running: "对比运行中",
  verifying: "独立验收",
  reporting: "独立复测中",
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
  exploration: "策略对比",
  admission: "新版挑战",
  final_report: "独立复测",
};
const actions = {
  inspect: "检查配置与服务",
  patch_config: "修改网关配置",
  probe: "探测业务健康",
  submit: "提交独立验收",
};
const reasons = {
  no_strict_success_gain: "新版通过数没有增加，下一轮沿用原有策略",
  strict_success_gain: "新版多通过了任务，并保住了原版已通过的任务",
  task_success_regression: "出现原版成功、新版失败的任务",
  paired_execution_interrupted: "配对执行不完整，需要补评",
  invalid_or_unchanged_policy: "策略格式不符合要求，或策略内容没有改变",
};
const eventNames = {
  "season.created": "已创建对比",
  "season.preparing": "准备真实任务环境",
  "round.started": "开始比较本轮策略",
  "run.preparing": "启动独立任务环境",
  "run.finished": "任务执行结束",
  "round.ranked": "本轮结果已记录",
  "mutation.started": "开始改写原版策略",
  "mutation.generated": "新版生成状态更新",
  "child.promoted": "新版获得下一轮资格",
  "child.rejected": "新版未晋级",
  "child.pending": "新版等待补测",
  "report.started": "开始独立复测",
  "season.finished": "本场对比结束",
  "season.interrupted": "本场对比中断",
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
  const target = $("setup").open ? "setup-error" : "error";
  for (const id of ["error", "setup-error"]) {
    const text = id === target ? message : "";
    $(id).hidden = !text;
    $(id).textContent = text || "";
  }
}

function selectSeason(id) {
  if (id === state.selected) return;
  state.selected = id;
  state.cursor = 0;
  state.events = [];
  state.run = null;
  state.detail = null;
  state.comparisonRound = null;
  state.focusVersion = null;
  state.runSignature = null;
  state.lineageSignature = null;
  state.comparisonSignature = null;
  state.runsSignature = null;
  state.feedSignature = null;
  state.reportSignature = null;
  resetTrace("选择图例或排行榜中的策略，查看实际工具动作。");
  renderFocus();
}

function recordDate(iso) {
  if (!iso) return "日期未知";
  return new Date(iso).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  });
}

function renderOverview() {
  const data = state.overview;
  const options = data.seasons.map((s) => `${s.id}:${s.status}`).join("|");
  const select = $("season-select");
  if (select.dataset.options !== options) {
    const records = data.seasons.map((s) => {
      const option = el(
        "option",
        `${recordDate(s.created_at)} · ${s.population} 个策略 · ${statuses[s.status] || s.status}`,
      );
      option.value = s.id;
      return option;
    });
    select.replaceChildren(
      ...(records.length ? records : [el("option", "还没有对比")]),
    );
    select.dataset.options = options;
  }
  select.value = state.selected || "";
  select.disabled = !data.seasons.length;
  $("start").disabled = state.starting || !!data.running || !data.model;
  $("start").textContent = state.starting ? "正在启动…" : "运行对比 →";
  $("cancel").hidden = !data.running;
  $("rounds").disabled = !!data.running;
  const viewingLive = data.running && data.running === state.selected;
  $("status").classList.toggle("live", !!viewingLive);
  $("feed-status").textContent = viewingLive
    ? "实时更新"
    : state.selected
      ? "历史记录"
      : "暂无记录";
  renderRoster();
  if ($("setup").open) renderPlannedBudget();
}

function renderSeason() {
  const { season: s, runs, mutations } = state.detail;
  $("status").textContent = statuses[s.status] || s.status;
  $("model").textContent = s.model?.model || "模型未记录";
  const valid = runs.filter((r) => r.status === "completed");
  $("seat-count").textContent = s.population;
  $("success").textContent =
    `${valid.filter((r) => r.passed).length} / ${valid.length}`;
  $("mutations").textContent =
    `${mutations.length} / ${mutations.filter((m) => m.status === "accepted").length}`;
  $("tokens").textContent = `${num(s.budget.tokens)} token`;
  $("calls").textContent = `${s.budget.calls} / ${s.budget.max_calls}`;
  renderComparison();
  renderLineage();
  renderRuns();
  renderReport();
}

function rankedRounds() {
  const { season, runs } = state.detail;
  const rounds = new Map();
  for (const event of state.events) {
    if (event.kind === "round.ranked" && event.data.leaderboard?.length) {
      rounds.set(event.data.round, event.data.leaderboard);
    }
  }
  const first = season.leaderboard?.[0];
  const savedRound =
    first && runs.find((run) => run.id === first.run_id)?.round;
  if (savedRound && !rounds.has(savedRound)) {
    rounds.set(savedRound, season.leaderboard);
  }
  return [...rounds].sort(([a], [b]) => a - b);
}

function runLink(id, label) {
  const button = el("button", label, "link-button");
  button.type = "button";
  button.onclick = () => chooseRun(id, true);
  return button;
}

function disclosure(label, content) {
  const details = el("details", null, "evidence-disclosure");
  details.append(el("summary", label), content);
  return details;
}

const metrics = {
  execution_seconds: {
    label: "执行耗时",
    value: (value) => `${value.toFixed(2)} 秒`,
  },
  tokens: {
    label: "模型 Token",
    value: (value) => `${num(value)} token`,
  },
};
const svgNamespace = "http://www.w3.org/2000/svg";
function svg(tag, attrs, label) {
  const node = document.createElementNS(svgNamespace, tag);
  for (const [attr, value] of Object.entries(attrs))
    node.setAttribute(attr, value);
  if (label) node.textContent = label;
  return node;
}
function seriesColors(rounds) {
  const versions = new Set(
    rounds.flatMap(([, rows]) => rows.map((r) => r.version)),
  );
  return new Map([...versions].map((id, i) => [id, `var(--s${(i % 6) + 1})`]));
}
function renderChart(rounds = state.detail ? rankedRounds() : []) {
  const host = $("chart");
  const metric = metrics[state.metric];
  const colors = seriesColors(rounds);
  const samples = rounds.flatMap(([, rows]) =>
    rows.map((r) => r[state.metric]).filter(Number.isFinite),
  );
  const signature = JSON.stringify([
    rounds,
    [...colors.keys()].map(name),
    state.metric,
    state.focusVersion,
    host.clientWidth,
    host.clientHeight,
  ]);
  if (signature === state.chartSignature) return;
  state.chartSignature = signature;
  $("chart-caption").textContent =
    state.metric === "execution_seconds"
      ? "单位 秒 · 每个点是一次实际执行，仅统计策略执行阶段。"
      : "单位 token · 每个点是供应商报告的单次策略执行用量。";
  if (!samples.length) {
    host.replaceChildren(
      empty("开始一次对比后，各策略的实测成绩会显示在这里。"),
    );
    return;
  }
  const width = Math.max(230, host.clientWidth - 24);
  const height = host.clientHeight - 10;
  const left = state.metric === "tokens" ? 48 : 35;
  const right = width - 12;
  const top = 16;
  const bottom = height - 29;
  const target = Math.max(...samples, 1) / 5;
  const scale = 10 ** Math.floor(Math.log10(target));
  const step = [1, 2, 2.5, 5, 10].find((n) => n * scale >= target) * scale;
  const ticks = Math.ceil(Math.max(...samples, 1) / step);
  const y = (value) => bottom - (value / (step * ticks)) * (bottom - top);
  const x = (i) =>
    left +
    (right - left) *
      (rounds.length === 1 ? 0.5 : 0.12 + (0.76 * i) / (rounds.length - 1));
  const graph = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `各策略每轮${metric.label}，具体成绩见排行榜`,
  });
  for (let i = 0; i <= ticks; i++) {
    const value = i * step;
    const yy = y(value);
    graph.append(
      svg("line", {
        x1: left,
        x2: right,
        y1: yy,
        y2: yy,
        class: i === 0 ? "baseline" : "gridline",
      }),
      svg(
        "text",
        { x: left - 7, y: yy + 4, "text-anchor": "end" },
        num(Number(value.toPrecision(6))),
      ),
    );
  }
  for (const [i, [round]] of rounds.entries()) {
    graph.append(
      svg("line", {
        x1: x(i),
        x2: x(i),
        y1: top,
        y2: bottom,
        class: "gridline",
      }),
      svg(
        "text",
        { x: x(i), y: bottom + 21, "text-anchor": "middle" },
        `第 ${round} 轮`,
      ),
    );
  }
  for (const [id, color] of colors) {
    const group = svg("g", {
      class: state.focusVersion && state.focusVersion !== id ? "dim" : "",
    });
    group.style.setProperty("--series", color);
    const points = rounds.map(([, rows], i) => {
      const row = rows.find((r) => r.version === id);
      return Number.isFinite(row?.[state.metric]) ? { i, row } : null;
    });
    for (const [index, point] of points.entries()) {
      if (!point) continue;
      const value = point.row[state.metric];
      const previous = points[index - 1];
      if (previous && rounds[index][0] === rounds[index - 1][0] + 1) {
        group.append(
          svg("line", {
            x1: x(previous.i),
            x2: x(point.i),
            y1: y(previous.row[state.metric]),
            y2: y(value),
            class: "series",
          }),
        );
      }
      const dot = svg("circle", {
        cx: x(point.i),
        cy: y(value),
        r: 4.7,
        class: "point",
      });
      dot.append(
        svg(
          "title",
          {},
          `${name(id)} · 第 ${rounds[index][0]} 轮 · ${metric.value(value)}`,
        ),
      );
      group.append(dot);
    }
    if (state.focusVersion === id) graph.append(group);
    else graph.insertBefore(group, graph.querySelector("g:not(.dim)"));
  }
  host.replaceChildren(graph);
}
function swatch(color) {
  const mark = el("span", null, "swatch");
  mark.setAttribute("aria-hidden", "true");
  mark.style.setProperty("--series", color);
  return mark;
}
function renderLegend(rounds) {
  $("chart-legend").replaceChildren(
    ...Array.from(seriesColors(rounds), ([id, color]) => {
      const button = el("button", null, "chip");
      button.type = "button";
      button.dataset.version = id;
      const entries = rounds.filter(([, rows]) =>
        rows.some((row) => row.version === id),
      );
      const [lastRound, lastRows] = entries.at(-1);
      const value = lastRows.find((r) => r.version === id)[state.metric];
      button.append(
        swatch(color),
        el("strong", name(id)),
        el(
          "small",
          `第 ${lastRound} 轮 · ${Number.isFinite(value) ? metrics[state.metric].value(value) : "暂无数据"}`,
        ),
      );
      button.onclick = () => setFocus(state.focusVersion === id ? null : id);
      return button;
    }),
  );
}
function renderFocus() {
  const id = state.focusVersion;
  $("focus-label").textContent = id ? name(id) : "全部策略";
  $("clear-focus").hidden = !id;
  for (const item of $("evolution-feed").querySelectorAll(".feed-item")) {
    item.hidden =
      !!id && id !== item.dataset.parent && id !== item.dataset.child;
  }
  const visible = $("evolution-feed").querySelector(".feed-item:not([hidden])");
  const filteredEmpty = $("evolution-feed").querySelector(".empty");
  if (filteredEmpty) filteredEmpty.hidden = !!visible;
  for (const button of document.querySelectorAll(".chip, .board .strategy")) {
    const selected = button.dataset.version === id;
    button.setAttribute("aria-pressed", String(selected));
    button.closest("tr")?.classList.toggle("selected", selected);
  }
}
function setFocus(id) {
  state.focusVersion = id;
  renderFocus();
  renderChart();
  if (!state.detail) return;
  if (!id) {
    renderRuns();
    return;
  }
  const rounds = rankedRounds();
  const round = state.comparisonRound ?? rounds.at(-1)?.[0];
  const matching = state.detail.runs.filter((r) => r.version === id);
  const run =
    matching.find((r) => r.phase === "exploration" && r.round === round) ||
    matching.findLast((r) => r.phase === "exploration") ||
    matching.at(-1);
  if (run) {
    showPanel("trace-panel");
    chooseRun(run.id, false);
  } else renderRuns();
}
function renderComparison() {
  const rounds = rankedRounds();
  const selected =
    rounds.find(([n]) => n === state.comparisonRound) || rounds.at(-1);
  const signature = JSON.stringify([
    rounds,
    selected?.[0],
    [...seriesColors(rounds).keys()].map((id) => version(id)),
  ]);
  renderChart(rounds);
  if (signature === state.comparisonSignature) return;
  state.comparisonSignature = signature;
  renderLegend(rounds);

  const tabs = $("round-tabs");
  const availableRounds = rounds.map(([n]) => n).join(",");
  if (tabs.dataset.rounds !== availableRounds) {
    tabs.replaceChildren(
      ...rounds.map(([n]) => {
        const button = el("button", `第 ${n} 轮`);
        button.type = "button";
        button.dataset.round = n;
        button.onclick = () => {
          state.comparisonRound = n;
          renderComparison();
          if (state.focusVersion) setFocus(state.focusVersion);
        };
        return button;
      }),
    );
    tabs.dataset.rounds = availableRounds;
  }
  for (const button of tabs.children) {
    button.setAttribute(
      "aria-pressed",
      String(Number(button.dataset.round) === selected?.[0]),
    );
  }

  if (!selected) {
    $("compare-summary").textContent =
      "各策略处理同样的故障，系统用真实下单和查单验收。";
    const row = el("tr");
    const cell = el("td", "还没有完整的轮次结果。", "empty");
    cell.colSpan = 6;
    row.append(cell);
    $("comparison").replaceChildren(row);
    return;
  }

  const [round, ranking] = selected;
  const passed = ranking.filter((r) => r.passed).length;
  const context = round === 1 ? "比较初始策略" : "比较改进判定后留下的策略";
  $("compare-summary").textContent =
    `第 ${round} 轮${context}，${passed} / ${ranking.length} 个策略修复成功。`;
  const colors = seriesColors(rounds);
  $("comparison").replaceChildren(
    ...ranking.map((r) => {
      const row = el("tr");
      const strategy = el("td");
      const button = el("button", null, "strategy");
      button.type = "button";
      button.dataset.version = r.version;
      button.append(
        swatch(colors.get(r.version)),
        document.createTextNode(name(r.version)),
      );
      button.onclick = () => {
        setFocus(r.version);
        $("replay").scrollIntoView({ block: "nearest" });
      };
      strategy.append(button);
      const result = el("td");
      result.append(
        el(
          "span",
          r.passed ? "修复成功" : "未通过验收",
          `pill ${r.passed ? "pass" : "fail"}`,
        ),
      );
      const evidence = el("td");
      evidence.append(runLink(r.run_id, "看回放 ↗"));
      const policy = version(r.version)?.policy;
      if (policy) evidence.append(disclosure("参赛策略", el("p", policy)));
      row.append(
        el("td", r.rank),
        strategy,
        result,
        el(
          "td",
          Number.isFinite(r.execution_seconds)
            ? metrics.execution_seconds.value(r.execution_seconds)
            : "—",
          "number",
        ),
        el("td", num(r.tokens), "number"),
        evidence,
      );
      return row;
    }),
  );
  renderFocus();
}
function renderLineage() {
  const { mutations, decisions } = state.detail;
  const signature = JSON.stringify([
    mutations,
    decisions,
    mutations.map((m) => [version(m.parent), version(m.child)]),
  ]);
  if (signature === state.lineageSignature) return;
  state.lineageSignature = signature;
  const evolutionItems = mutations.map((m) => {
    const decision = decisions.find((d) => d.mutation === m.id);
    const status = decision?.status || m.status;
    const reason = decision?.reason || m.reason;
    const item = el("article", null, "feed-item");
    item.dataset.parent = m.parent;
    if (m.child) item.dataset.child = m.child;
    item.append(
      el("small", `第 ${m.round} 轮后 · ${recordDate(m.created_at)}`),
      el("p", `${name(m.parent)} → ${m.child ? name(m.child) : "等待候选"}`),
      el(
        "span",
        statuses[status] || "等待结果",
        `pill ${status === "accepted" ? "pass" : status === "rejected" ? "fail" : "pending"}`,
      ),
    );
    if (reasons[reason]) item.append(el("p", reasons[reason], "footnote"));
    return item;
  });
  $("evolution-feed").replaceChildren(
    ...evolutionItems,
    empty(
      mutations.length
        ? "这个策略没有参与本场进化。"
        : "首轮之后会尝试生成并检验改进版。",
    ),
  );
  renderFocus();
  $("lineage").replaceChildren(
    ...(mutations.length
      ? mutations.map((m) => {
          const decision = decisions.find((d) => d.mutation === m.id);
          const status = decision?.status || m.status;
          const reason = decision?.reason || m.reason;
          const row = el("article", null, "mutation");
          const banner = el("div", null, "decision-banner");
          const label =
            status === "rejected"
              ? "新版未晋级"
              : statuses[status] || "等待结果";
          banner.append(
            el(
              "span",
              label,
              `pill ${status === "accepted" ? "pass" : status === "rejected" ? "fail" : "pending"}`,
            ),
          );
          if (reason && reasons[reason])
            banner.append(el("strong", reasons[reason]));
          if (Number.isInteger(m.round))
            banner.append(el("span", `第 ${m.round} 轮后`, "card-label"));
          row.append(banner);
          const pair = el("div", null, `paired-grid ${status}`);
          for (const [role, id, successes, run] of [
            [
              "原版",
              m.parent,
              decision?.parent_successes,
              decision?.parent_run,
            ],
            ["改进版", m.child, decision?.child_successes, decision?.child_run],
          ]) {
            const card = el("div", null, "gene");
            card.append(
              el("span", role, "card-label"),
              el("h3", id ? name(id) : "等待新版"),
            );
            card.append(
              el(
                "p",
                Number.isInteger(successes)
                  ? `通过 ${successes} 项任务`
                  : "暂无配对结果",
              ),
            );
            if (run) card.append(runLink(run, `看${role}过程 ↗`));
            if (pair.childElementCount) {
              const edge = el("span", null, "evolution-edge");
              edge.setAttribute("aria-hidden", "true");
              pair.append(edge);
            }
            pair.append(card);
          }
          row.append(pair);
          const hypothesis = version(m.child)?.hypothesis;
          if (hypothesis) row.append(el("p", hypothesis, "hypothesis"));
          if (m.diff) row.append(disclosure("查看策略改动", el("pre", m.diff)));
          if (decision?.replaced_version) {
            row.append(
              el(
                "p",
                `下一轮接替 ${name(decision.replaced_version)}。`,
                "footnote",
              ),
            );
          }
          return row;
        })
      : [empty("完成首轮后，这里会显示策略改动和原版、新版的比较。")]),
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
function showPanel(id) {
  for (const button of $("activity-tabs").children) {
    const selected = button.dataset.panel === id;
    button.setAttribute("aria-pressed", String(selected));
    $(button.dataset.panel).hidden = !selected;
  }
  $("run-select").closest(".run-control").hidden = id === "evolution-panel";
}
function resetTrace(message) {
  $("trace").replaceChildren(empty(message));
  $("verification").replaceChildren(empty("等待独立验收结果。"));
  $("replay-status").textContent = "等待执行记录";
  $("replay-status").className = "pill";
}
function renderRuns() {
  const runs = state.detail.runs.filter(
    (r) => !state.focusVersion || r.version === state.focusVersion,
  );
  const signature = JSON.stringify(
    runs.map((r) => [r.id, r.status, r.passed, name(r.version)]),
  );
  const select = $("run-select");
  if (signature !== state.runsSignature) {
    state.runsSignature = signature;
    const options = runs.map((r) => {
      const option = el(
        "option",
        `${name(r.version)} · ${phases[r.phase] || r.phase} · 第 ${r.round} 轮`,
      );
      option.value = r.id;
      return option;
    });
    select.replaceChildren(
      ...(options.length ? options : [el("option", "暂无执行")]),
    );
    select.disabled = !options.length;
  }
  select.value = state.run || "";
  if (!state.run && runs.length) {
    const latestComparison = rankedRounds()
      .at(-1)?.[1]
      .find((row) => runs.some((r) => r.id === row.run_id));
    chooseRun(latestComparison?.run_id || runs[0].id, false);
  }
}
async function chooseRun(id, scroll) {
  const run = state.detail?.runs.find((r) => r.id === id);
  if (!run) return;
  if (state.focusVersion && state.focusVersion !== run.version) {
    state.focusVersion = null;
    renderFocus();
    renderChart();
  }
  if (id !== state.run) {
    state.run = id;
    state.runSignature = null;
    resetTrace("正在读取执行记录…");
  }
  renderRuns();
  if (scroll) {
    showPanel("trace-panel");
    $("trace-panel").scrollTop = 0;
    $("replay-title").focus({ preventScroll: true });
    $("replay").scrollIntoView({ block: "nearest" });
  }
  await refreshRun();
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
    if (state.run === requested) error(e.message);
  }
}
function renderTrace({ run, events }) {
  $("replay-status").textContent = verdict(run);
  $("replay-status").className = `pill ${tone(run)}`;
  const list = [
    el(
      "div",
      `${name(run.version)} · ${phases[run.phase] || run.phase} · ${run.id}`,
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
      step.append(label);
      const evidence = actionEvidence(d);
      if (evidence) step.append(el("p", evidence, "observed"));
      step.append(
        disclosure(
          "查看工具原始记录",
          el(
            "pre",
            JSON.stringify(
              { action: d.action, result: d.result, usage: d.usage },
              null,
              2,
            ),
          ),
        ),
      );
      list.push(step);
    } else if (e.kind === "action.invalid")
      list.push(el("p", "模型输出了不符合工具协议的动作。", "fail"));
  }
  let verification = empty("等待独立验收结果。");
  if (run.verification) {
    const checks = el("div", null, "checks");
    const tests = run.verification.tests;
    const passed = tests.filter((test) => test.passed).length;
    checks.append(
      el("div", `独立业务验收 · ${passed} / ${tests.length} 项通过`, "muted"),
    );
    const labels = {
      upstream_health: "订单依赖健康",
      create_order: "创建真实订单",
      read_order: "查询订单一致性",
      dependency_constraint: "依赖配置约束",
    };
    for (const t of tests)
      checks.append(
        el(
          "div",
          `${t.passed ? "✓" : "×"} ${labels[t.name] || t.name}`,
          `check ${t.passed ? "pass" : "fail"}`,
        ),
      );
    verification = checks;
  }
  if (run.error)
    list.push(
      el("p", `${run.error.category} · ${run.error.reason}`, "pending"),
    );
  if (!events.length) list.push(empty("正在准备独立环境，尚无 Agent 动作。"));
  $("trace").replaceChildren(...list);
  $("verification").replaceChildren(verification);
}

function actionEvidence({ action, result }) {
  const upstream = result?.configuration?.upstream;
  if (
    action.action === "inspect" &&
    upstream &&
    Array.isArray(result.services)
  ) {
    const order = result.services.find((service) => service.name === "orders");
    if (order?.listen)
      return `网关当前指向 ${upstream}，订单服务实际监听 ${order.listen}。`;
  }
  if (action.action === "patch_config" && upstream)
    return `网关上游已更新为 ${upstream}。`;
  if (action.action === "probe" && Number.isInteger(result?.status)) {
    return `健康检查返回 HTTP ${result.status}。`;
  }
  if (action.action === "submit")
    return "将当前修复交给验收程序，验证下单、查单和依赖配置。";
  return action.summary || "";
}
function renderReport() {
  const { season, seats } = state.detail;
  const rows = season.final_report || [];
  const signature = JSON.stringify([
    rows,
    seats,
    rows.map((r) => version(r.version)),
  ]);
  if (signature === state.reportSignature) return;
  state.reportSignature = signature;
  $("report").replaceChildren(
    ...(rows.length
      ? [
          (() => {
            const wrap = el("div", null, "report-list");
            for (const r of rows) {
              const card = el("article", null, "report-item");
              card.append(
                el("h3", name(r.version)),
                el("small", reportRole(r, seats), "card-label"),
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
              card.append(runLink(r.run_id, "看复测过程 ↗"));
              wrap.append(card);
            }
            return wrap;
          })(),
        ]
      : [
          empty(
            "所有轮次结束后，初始版本和最终参赛版本才会在这里留下复测结果。",
          ),
        ]),
  );
}
function reportRole(row, seats) {
  const finalist = seats.some((seat) => seat.version === row.version);
  if (row.initial && finalist) return "初始版本 · 最终仍参赛";
  return row.initial ? "初始版本" : "最终参赛版本";
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
    if (
      ["error", "setup-error"].some((id) =>
        $(id).textContent.startsWith("读取失败："),
      )
    )
      error("");
    if (!state.selected && state.overview.seasons.length)
      selectSeason(state.overview.seasons[0].id);
    renderOverview();
    if (!state.selected) {
      $("status").textContent = "还没有对比";
      $("model").textContent = state.overview.model?.model || "未配置";
    }
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
      renderComparison();
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
  const rounds = Number($("rounds").value);
  $("round-help").textContent =
    rounds === 2
      ? "第一轮先比修复结果，随后尝试改进一次。第二轮用保留的策略再比，最后独立复测。"
      : "前两轮各尝试改进一次，第三轮比较留下的策略，最后独立复测。";
  const budget = `本次最多发起 ${plannedCalls()} 次模型请求。累计到 220,000 token 后停止派发。`;
  $("notice").textContent =
    state.overview && !state.overview.model
      ? "当前没有配置模型，可以查看历史或保存自己的策略。"
      : budget;
  if (state.overview?.running)
    $("notice").textContent = "已有对比正在运行，结束后可以启动下一场。";
}
$("population").onchange = renderPlannedBudget;
$("rounds").onchange = renderPlannedBudget;
$("open-setup").onclick = () => {
  error("");
  renderPlannedBudget();
  $("setup").showModal();
};
$("close-setup").onclick = () => $("setup").close();
document.querySelector(".header-link").onclick = () => {
  $("method").open = true;
};
$("metric-tabs").onclick = (event) => {
  const metric = event.target.closest("button")?.dataset.metric;
  if (!metric || metric === state.metric) return;
  state.metric = metric;
  for (const button of $("metric-tabs").children)
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.metric === metric),
    );
  renderChart();
  if (state.detail) renderLegend(rankedRounds());
  renderFocus();
};
$("activity-tabs").onclick = (event) => {
  const panel = event.target.closest("button")?.dataset.panel;
  if (panel) showPanel(panel);
};
$("run-select").onchange = (event) => chooseRun(event.target.value, false);
$("clear-focus").onclick = () => setFocus(null);
new ResizeObserver(() => renderChart()).observe($("chart"));

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const label = theme === "dark" ? "切换浅色主题" : "切换深色主题";
  $("theme-toggle").setAttribute("aria-label", label);
  $("theme-toggle").title = label;
}
try {
  applyTheme(
    localStorage.getItem("evoarena-theme") === "dark" ? "dark" : "light",
  );
} catch {
  applyTheme("light");
}
$("theme-toggle").onclick = () => {
  const theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(theme);
  try {
    localStorage.setItem("evoarena-theme", theme);
  } catch {}
};

$("start").onclick = async () => {
  if (state.starting) return;
  error("");
  const chosen = selectedVersions();
  const custom = $("roster").value === "custom";
  if (custom && ![2, 4].includes(chosen.length)) {
    error("请勾选 2 或 4 个不同的策略版本。");
    return;
  }
  const body = JSON.stringify({
    population: custom ? chosen.length : Number($("population").value),
    rounds: Number($("rounds").value),
    max_calls: plannedCalls(),
    max_tokens: 220000,
    ...(custom ? { version_ids: chosen } : {}),
  });
  if (state.pendingStart?.body !== body) {
    state.pendingStart = { body, key: crypto.randomUUID() };
  }
  state.starting = true;
  renderOverview();
  try {
    const data = await api("/api/seasons", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-EvoArena-Request": "1",
        "Idempotency-Key": state.pendingStart.key,
      },
      body,
    });
    state.pendingStart = null;
    selectSeason(data.id);
    $("setup").close();
  } catch (e) {
    error("启动失败：" + e.message);
  } finally {
    state.starting = false;
    renderOverview();
    await refresh();
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
    $("status").textContent = "正在停止";
  } catch (e) {
    error(e.message);
  }
};
renderPlannedBudget();
refresh();
setInterval(refresh, 2000);
