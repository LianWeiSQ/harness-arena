"use strict";

const $ = (id) => document.getElementById(id);
const statusText = {
  queued: "等待启动",
  preparing: "准备赛场",
  running: "评测中",
  completed: "已完成",
  incomplete: "含执行异常",
  passed: "验收通过",
  failed: "未通过验收",
  error: "执行异常",
};
const active = new Set(["queued", "preparing", "running"]);
const judged = new Set(["passed", "failed"]);
const shortName = { dsh: "DSH", pi: "Pi" };
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const scenarioNames = {
  running: "直接进程仍在运行",
  "already-exited": "直接进程已退出",
};
let overview, match, matchId, signature, pendingKey, pendingPayload;
let metric = "seconds";
let task = "process-cleanup";
let tab = "checks";
let starting = false;
let reading = false;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "读取对局失败");
  return data;
}

function error(message) {
  $("error-message").textContent = message;
  $("error-message").hidden = !message;
}

function runFor(key, runner) {
  return match?.runs.find((run) => run.task === key && run.runner === runner);
}

function recordsOf(data) {
  return data.matches.filter(
    (row) => row.mode === "arena" || row.mode === "dsh",
  );
}

function taskResult(key, runners) {
  const runs = runners.map((runner) => runFor(key, runner));
  if (runs.some((run) => run?.status === "error")) return "执行异常";
  if (!runs.every((run) => judged.has(run?.status))) return "待验收";
  const winners = runs.filter((run) => run.status === "passed");
  if (runners.length === 1) return winners.length ? "验收通过" : "未通过";
  if (winners.length === 1) return `${shortName[winners[0].runner]} 胜`;
  return winners.length ? "平局 · 双方通过" : "平局 · 双方未通过";
}

function drawOverview() {
  $("status").textContent = match ? statusText[match.status] : "等待评测";
  $("status").classList.toggle("live", active.has(match?.status));
  $("model").textContent = match?.model || overview.model || "待配置";
  $("start-match").disabled =
    starting ||
    !overview.ready ||
    overview.matches.some((row) => active.has(row.status));
  const { seconds, calls, tokens } = match?.limits || overview.limits;
  $("budget").textContent = overview.ready
    ? `单次执行 · ${seconds} 秒 · 最多 ${calls} 次模型请求 · 累计 ${tokens.toLocaleString()} token 后停止派发`
    : "本地启动时需指定 Harbor CLI、任务仓库和模型配置。";
  const records = recordsOf(overview);
  const key = records.map((row) => row.id + row.status).join("|");
  if ($("matches").dataset.records !== key) {
    $("matches").dataset.records = key;
    $("matches").replaceChildren(
      ...(records.length
        ? records.map((row) => {
            const roster = row.runners || ["dsh"];
            const format = roster.map((id) => shortName[id] || id).join(" vs ");
            const option = el(
              "option",
              `${dateFormat.format(new Date(row.created))} · ${format} · ${statusText[row.status]}`,
            );
            option.value = row.id;
            return option;
          })
        : [el("option", "暂无记录")]),
    );
    $("matches").disabled = !records.length;
  }
  $("matches").value = matchId || "";
}

function draw() {
  drawOverview();
  const runnerIds =
    match?.runners ||
    (match?.mode === "dsh" ? ["dsh"] : Object.keys(overview.runners));
  const duel = runnerIds.length === 2;
  const keys = Object.keys(overview.tasks);
  const scored = keys.filter((key) =>
    runnerIds.every((runner) => judged.has(runFor(key, runner)?.status)),
  );
  const points = runnerIds.map(
    (runner) =>
      scored.filter((key) => runFor(key, runner).status === "passed").length,
  );
  const scoreAvailable = scored.length > 0;
  const scoredLabel = `${duel ? "有效配对" : "完成验收"} ${scored.length} / ${keys.length} 题`;
  const errorCount =
    match?.runs.filter((run) => run.status === "error").length || 0;
  const winner =
    points[0] === points[1]
      ? "本场平局"
      : `${shortName[runnerIds[points[0] > points[1] ? 0 : 1]]} 获胜`;
  $("scoreboard").classList.toggle("paired", duel);
  $("evidence-grid").classList.toggle("paired", duel);
  $("title").textContent =
    runnerIds.map((id) => shortName[id] || id).join(" vs ") +
    (duel ? "" : " · 单项评测");
  $("coverage").textContent = `${scored.length} / ${keys.length}`;
  const runs = match?.runs || [];
  $("total-calls").textContent = runs
    .reduce((sum, run) => sum + (run.usage?.calls || 0), 0)
    .toLocaleString();
  $("total-tokens").textContent = runs
    .reduce((sum, run) => sum + (run.usage?.tokens || 0), 0)
    .toLocaleString();
  drawChart(runnerIds, keys);
  drawActivity();
  drawStandings(runnerIds, points);
  $("scoreboard").replaceChildren(
    ...runnerIds.map((runner, index) => {
      const panel = el("div", null, `score runner-${runner}`);
      const version = match?.runs.find(
        (run) => run.runner === runner && run.runtime_version,
      )?.runtime_version;
      panel.append(
        el(
          "small",
          `选手 ${String(index + 1).padStart(2, "0")}${version ? ` · v${version}` : ""}`,
        ),
        el("h3", overview.runners[runner]),
        el("span", scoreAvailable ? points[index] : "—", "score-value"),
        el("small", duel ? "配对任务得分" : "有效任务通过数"),
      );
      return panel;
    }),
  );
  $("verdict").textContent = !match
    ? "等待首场对局。"
    : active.has(match.status)
      ? `评测进行中 · ${scoredLabel}`
      : match.status === "incomplete"
        ? `${duel ? "本场未定胜负" : "单跑未完成"} · ${scoredLabel}${errorCount ? ` · ${errorCount} 次执行异常` : ""}`
        : `${duel ? winner : "单跑结束"} · ${scoredLabel}`;

  for (const button of $("evidence-tabs").children)
    button.setAttribute("aria-pressed", String(button.dataset.tab === tab));
  $("tasks").replaceChildren(
    ...keys.map((key) => {
      const info = overview.tasks[key];
      const button = el("button");
      button.type = "button";
      button.setAttribute("aria-pressed", String(key === task));
      const meta = el("span", null, "task-meta");
      meta.append(
        el("small", info.category),
        el("small", taskResult(key, runnerIds), "task-outcome"),
      );
      button.append(meta, el("strong", info.title));
      button.onclick = () => {
        task = key;
        draw();
      };
      return button;
    }),
  );
  $("task-description").textContent = overview.tasks[task].prompt;
  $("evidence-grid").replaceChildren(
    ...runnerIds.map((runner) => evidence(runner)),
  );
}

function evidence(runner) {
  const run = runFor(task, runner);
  const panel = el("article", null, `evidence-column runner-${runner}`);
  const label = el(
    "span",
    statusText[run?.status] || "等待执行",
    `tag ${run?.status === "passed" ? "pass" : run?.status === "failed" || run?.status === "error" ? "fail" : "pending"}`,
  );
  panel.append(el("h3", overview.runners[runner]), label);
  if (!run) {
    panel.append(el("p", "该选手的这道任务尚未执行。", "muted"));
    return panel;
  }
  const metrics = [
    run.seconds != null ? `${run.seconds} 秒` : null,
    run.usage
      ? `${run.usage.calls} 次请求 · ${run.usage.tokens.toLocaleString()} token`
      : null,
  ].filter(Boolean);
  if (metrics.length) panel.append(el("p", metrics.join(" · "), "muted"));
  if (run.error) panel.append(el("p", run.error, "fail"));
  if (run.stop_reason) {
    const note = judged.has(run.status) ? " · 按当时产物验收" : "";
    panel.append(el("p", run.stop_reason + note, "pending"));
  }
  if (tab === "trace") {
    for (const [index, step] of run.trace.entries()) {
      const item = el("details", null, "trace-step");
      item.dataset.key = `${runner}-${task}-${index}`;
      item.open = openTraces.has(item.dataset.key);
      item.append(
        el("summary", `${index + 1}. ${step.label}`),
        el("pre", step.text),
      );
      panel.append(item);
    }
  } else if (tab === "patch") {
    panel.append(
      run.patch ? el("pre", run.patch) : el("p", "此项执行暂无补丁。", "muted"),
    );
  } else {
    const list = el("ul", null, "checks");
    for (const check of run.checks) {
      const description =
        check.label ||
        `${check.file} · ${scenarioNames[check.scenario] || check.scenario}`;
      list.append(
        el(
          "li",
          `${check.passed ? "✓" : "×"} ${description}`,
          check.passed ? "pass" : "fail",
        ),
      );
    }
    if (list.childElementCount) {
      const passed = run.checks.filter((check) => check.passed).length;
      panel.append(
        el("p", `独立验收 · ${passed} / ${run.checks.length} 项通过`),
        list,
      );
    } else {
      panel.append(el("p", "等待独立验收记录。", "muted"));
    }
    if (run.ledger)
      panel.append(el("p", `外部账本 · ${run.ledger.writes} 笔写入`));
  }
  return panel;
}

const openTraces = new Set();
$("evidence-grid").addEventListener("click", (event) => {
  const details = event.target.closest("summary")?.parentElement;
  if (!details?.dataset.key) return;
  if (details.open) openTraces.delete(details.dataset.key);
  else openTraces.add(details.dataset.key);
});

async function refresh() {
  if (reading) return;
  reading = true;
  try {
    const nextOverview = await api("/api/harness");
    const records = recordsOf(nextOverview);
    const id =
      matchId ||
      records.find((row) => row.mode === "arena")?.id ||
      records[0]?.id;
    const nextMatch = id
      ? await api("/api/harness/matches/" + encodeURIComponent(id))
      : null;
    if (matchId && matchId !== id) return;
    matchId = id;
    overview = nextOverview;
    match = nextMatch;
    const next = JSON.stringify([overview, match, starting]);
    if (next !== signature) {
      signature = next;
      draw();
    }
  } catch (problem) {
    error(problem.message);
  } finally {
    reading = false;
  }
}
$("matches").onchange = (event) => {
  matchId = event.target.value;
  openTraces.clear();
  refresh();
};
$("evidence-tabs").onclick = (event) => {
  const selected = event.target.closest("button")?.dataset.tab;
  if (selected && overview) {
    tab = selected;
    draw();
  }
};
function rosterChanged() {
  const count = $("runner-options").querySelectorAll("input:checked").length;
  $("roster-hint").textContent =
    count === 2
      ? "2 位选手，同题对战。"
      : count === 1
        ? "1 位选手，运行单项评测。"
        : "至少选择 1 位选手。";
  $("launch-match").disabled = !count || !$("match-model").value || starting;
}
$("start-match").onclick = () => {
  const models = overview.models;
  $("match-model").replaceChildren(
    ...models.profiles.map((profile) => {
      const option = el("option", `${profile.name} · ${profile.model}`);
      option.value = profile.id;
      return option;
    }),
  );
  $("match-model").value = models.default || "";
  $("runner-options").replaceChildren(
    ...Object.keys(overview.runners).map((runner) => {
      const label = el("label", null, `runner-option runner-${runner}`);
      const input = el("input");
      input.type = "checkbox";
      input.value = runner;
      input.checked = true;
      const description = el("span");
      description.append(
        el("strong", overview.runners[runner]),
        el("small", runner === "dsh" ? "终端工具 · PTC" : "读写文件 · Bash"),
      );
      label.append(input, description);
      return label;
    }),
  );
  $("dialog-error").hidden = true;
  rosterChanged();
  $("match-dialog").showModal();
};
$("runner-options").onchange = rosterChanged;
$("match-model").onchange = rosterChanged;
$("close-dialog").onclick = () => $("match-dialog").close();
$("match-form").onsubmit = async (event) => {
  event.preventDefault();
  if (starting) return;
  const payload = JSON.stringify({
    model_profile: $("match-model").value,
    runners: [...$("runner-options").querySelectorAll("input:checked")].map(
      (input) => input.value,
    ),
  });
  if (payload !== pendingPayload) pendingKey = null;
  pendingPayload = payload;
  pendingKey ||= crypto.randomUUID();
  starting = true;
  error("");
  rosterChanged();
  drawOverview();
  try {
    const created = await api("/api/harness/matches", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-EvoArena-Request": "1",
        "Idempotency-Key": pendingKey,
      },
      body: payload,
    });
    pendingKey = null;
    openTraces.clear();
    matchId = created.id;
    $("match-dialog").close();
  } catch (problem) {
    $("dialog-error").textContent = problem.message;
    $("dialog-error").hidden = false;
  } finally {
    starting = false;
    rosterChanged();
    refresh();
  }
};
$("metric-tabs").onclick = (event) => {
  const selected = event.target.closest("button")?.dataset.metric;
  if (selected && overview) {
    metric = selected;
    draw();
  }
};
function svgNode(tag, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes))
    node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  return node;
}
function drawChart(runners, tasks) {
  const tokens = metric === "tokens";
  $("chart-title").textContent = tokens ? "逐题 Token 用量" : "逐题执行耗时";
  for (const button of $("metric-tabs").children)
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.metric === metric),
    );
  const valueOf = (run) => (tokens ? run?.usage?.tokens : run?.seconds);
  const values = (match?.runs || [])
    .map(valueOf)
    .filter((value) => value != null);
  if (!values.length) {
    $("performance-chart").replaceChildren(
      el("p", "完成执行后，展示每位选手的实测耗时和 Token 用量。", "empty"),
    );
    return;
  }
  const maximum = Math.max(...values, 1) * 1.2;
  const chartWidth = Math.max(280, $("performance-chart").clientWidth - 32);
  const narrow = chartWidth < 500;
  const left = narrow ? 96 : 150;
  const plotWidth = chartWidth - left - 48;
  const format = (value) =>
    tokens ? `${(value / 1000).toFixed(1)}k` : `${value.toFixed(1)}s`;
  const svg = svgNode("svg", {
    viewBox: `0 0 ${chartWidth} 182`,
    role: "img",
    "aria-label": $("chart-title").textContent,
  });
  svg.append(
    svgNode("title", {}, "当前对局每位选手的实测用量，未完成的任务显示等待"),
  );
  for (let index = 0; index <= 4; index++) {
    const x = left + (index * plotWidth) / 4;
    svg.append(
      svgNode("line", { x1: x, y1: 8, x2: x, y2: 151, class: "chart-grid" }),
      svgNode(
        "text",
        { x, y: 174, "text-anchor": "middle", class: "chart-label" },
        format((maximum * index) / 4),
      ),
    );
  }
  tasks.forEach((key, index) => {
    const y = 20 + index * 73;
    svg.append(
      svgNode(
        "text",
        { x: 0, y: y + 17, class: "chart-label" },
        key === "process-cleanup" ? "01  进程清理" : "02  超时恢复",
      ),
    );
    runners.forEach((runner, offset) => {
      const value = valueOf(runFor(key, runner));
      const barY = y + offset * 25;
      const width = value == null ? 0 : (value / maximum) * plotWidth;
      const bar = svgNode("rect", {
        x: left,
        y: barY,
        width,
        height: 13,
        class: `chart-bar runner-${runner}`,
      });
      bar.append(
        svgNode(
          "title",
          {},
          `${shortName[runner]} · ${overview.tasks[key].title} · ${value == null ? "等待" : format(value)}`,
        ),
      );
      svg.append(
        bar,
        svgNode(
          "text",
          { x: left + 6 + width, y: barY + 11, class: "chart-value" },
          value == null ? `${shortName[runner]} 等待` : format(value),
        ),
      );
    });
  });
  $("performance-chart").replaceChildren(svg);
  $("chart-note").textContent = tokens
    ? "供应商报告的累计 Token · 含重复输入"
    : "单位为秒 · 数值越低，执行用时越短";
}
function drawActivity() {
  const steps = (match?.runs || []).flatMap((run) =>
    run.trace.map((step) => ({ ...step, runner: run.runner, task: run.task })),
  );
  steps.sort((a, b) => b.time.localeCompare(a.time));
  $("activity-feed").replaceChildren(
    ...steps.slice(0, 14).map((step) => {
      const item = el("div", null, `activity runner-${step.runner}`);
      const heading = el("div", null, "activity-heading");
      heading.append(
        el("strong", shortName[step.runner]),
        el(
          "time",
          new Date(step.time).toLocaleTimeString("zh-CN", { hour12: false }),
        ),
      );
      item.append(
        heading,
        el("p", step.label),
        el("small", step.text.slice(0, 160)),
      );
      return item;
    }),
  );
  if (!steps.length)
    $("activity-feed").append(
      el("p", "启动对局后，工具调用与验收动态会出现在这里。", "empty"),
    );
}
function drawStandings(runners, points) {
  $("standings-body").replaceChildren(
    ...runners.map((runner, index) => {
      const runs = (match?.runs || []).filter((run) => run.runner === runner);
      const row = el("tr", null, `runner-${runner}`);
      const name = el("td", overview.runners[runner], "runner-name");
      const version = runs.find((run) => run.runtime_version)?.runtime_version;
      if (version) name.append(el("span", `v${version}`, "table-version"));
      const finished = runs.filter((run) => run.seconds != null);
      const seconds = finished.reduce((sum, run) => sum + run.seconds, 0);
      const usage = runs.filter((run) => run.usage);
      row.append(
        name,
        el("td", points[index]),
        el(
          "td",
          `${runs.filter((run) => judged.has(run.status)).length} / ${Object.keys(overview.tasks).length}`,
        ),
        el("td", finished.length ? `${seconds.toFixed(1)}s` : "—"),
        el(
          "td",
          usage.length
            ? usage.reduce((sum, run) => sum + run.usage.calls, 0)
            : "—",
        ),
        el(
          "td",
          usage.length
            ? usage
                .reduce((sum, run) => sum + run.usage.tokens, 0)
                .toLocaleString()
            : "—",
        ),
        el("td", runs.filter((run) => run.status === "error").length),
      );
      return row;
    }),
  );
}
try {
  document.documentElement.dataset.theme =
    localStorage.getItem("arena-theme") || "light";
} catch {}
$("theme-toggle").onclick = () => {
  const theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("arena-theme", theme);
  } catch {}
};
refresh();
window.addEventListener("resize", () => {
  if (overview) draw();
});
setInterval(refresh, 2000);
