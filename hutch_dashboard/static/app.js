const state = { runs: [], selectedRun: null, selectedPath: null };

const node = (tag, className, text) => {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined && text !== null) value.textContent = String(text);
  return value;
};

const formatTime = value => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", { hour12: false });
};

const formatDuration = seconds => {
  if (seconds === null || seconds === undefined) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return [hours ? `${hours}h` : "", minutes ? `${minutes}m` : "", `${rest}s`].filter(Boolean).join(" ");
};

const formatBytes = bytes => {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
};

async function fetchJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function loadRuns() {
  const count = document.querySelector("#run-count");
  count.textContent = "加载中";
  try {
    state.runs = await fetchJSON("/api/runs");
    count.textContent = `${state.runs.length} 个已完成实例`;
    renderRunList();
    if (state.runs.length && !state.selectedRun) await selectRun(state.runs[0].run_id);
  } catch (error) {
    count.textContent = "加载失败";
    document.querySelector("#run-list").replaceChildren(node("div", "error", error.message));
  }
}

function renderRunList() {
  const list = document.querySelector("#run-list");
  list.replaceChildren();
  for (const run of state.runs) {
    const button = node("button", `run-item${state.selectedRun?.run_id === run.run_id ? " active" : ""}`);
    button.type = "button";
    button.append(node("span", "run-name", run.workflow));
    button.append(node("span", "run-id", run.run_id));
    const meta = node("span", "run-meta");
    meta.append(node("span", "status", run.status));
    meta.append(node("span", "", formatTime(run.finished_at)));
    button.append(meta);
    button.addEventListener("click", () => selectRun(run.run_id));
    list.append(button);
  }
}

async function selectRun(runId) {
  const detail = document.querySelector("#detail");
  detail.replaceChildren(node("div", "empty-state", "正在读取 Flow 证据…"));
  try {
    state.selectedRun = await fetchJSON(`/api/runs/${encodeURIComponent(runId)}`);
    const preferred = state.selectedRun.deliverables.find(item => item.path.includes("final-report"))
      || state.selectedRun.deliverables.find(item => item.kind === "final")
      || state.selectedRun.deliverables[0];
    state.selectedPath = preferred?.path || null;
    renderRunList();
    renderDetail();
  } catch (error) {
    detail.replaceChildren(node("div", "error", `读取实例失败：${error.message}`));
  }
}

function stat(label, value) {
  const box = node("div", "stat");
  box.append(node("div", "stat-label", label), node("div", "stat-value", value));
  return box;
}

function sectionTitle(title, note) {
  const box = node("div", "section-title");
  box.append(node("h3", "", title));
  if (note) box.append(node("span", "", note));
  return box;
}

function fact(label, value) {
  const box = node("div", "fact");
  box.append(node("label", "", label), node("span", "", value || "—"));
  return box;
}

function assignmentBlock(item, index) {
  const box = node("div", "assignment");
  const values = [
    ["执行", `#${index + 1}${item.attempt ? ` / attempt ${item.attempt}` : ""}`],
    ["Session", item.session || "未记录"],
    ["Terminal", item.terminal_id || "未记录"],
    ["Window", item.window || "已清理 / 未记录"],
    ["Provider", item.provider || "未记录"],
  ];
  for (const [label, value] of values) {
    const line = node("div", "assignment-line");
    line.append(node("label", "", label), node("span", "", value));
    box.append(line);
  }
  return box;
}

function renderAgent(agent) {
  const card = node("article", "agent card");
  const head = node("div", "agent-head");
  const title = node("div");
  title.append(node("h4", "", agent.profile || "未命名 Agent"), node("div", "agent-stage", `${agent.stage}${agent.task_id ? ` · ${agent.task_id}` : ""}`));
  head.append(title, node("span", "status", agent.status));
  card.append(head);
  if (agent.result_summary) card.append(node("p", "agent-summary", agent.result_summary));
  const facts = node("div", "facts");
  facts.append(
    fact("依赖", agent.depends_on.length ? agent.depends_on.join(", ") : "无"),
    fact("候选记录", agent.finding_count),
    fact("开始", formatTime(agent.started_at)),
    fact("完成", formatTime(agent.finished_at)),
  );
  card.append(facts);
  for (const [index, assignment] of agent.assignments.entries()) card.append(assignmentBlock(assignment, index));
  if (agent.deliverables.length) {
    const line = node("div", "assignment-line");
    line.append(node("label", "", "交付物"), node("span", "", agent.deliverables.map(item => item.path).join(" · ")));
    card.append(line);
  }
  return card;
}

function renderArtifacts(run) {
  const grid = node("div", "artifact-grid card");
  const list = node("div", "artifact-list");
  const viewer = node("div", "artifact-viewer");
  const selected = run.deliverables.find(item => item.path === state.selectedPath) || run.deliverables[0];
  for (const item of run.deliverables) {
    const button = node("button", `artifact-button${selected?.path === item.path ? " active" : ""}`);
    button.type = "button";
    button.append(node("strong", "", item.path), node("small", "", `${item.kind} · ${formatBytes(item.bytes)}${item.stage ? ` · ${item.stage}` : ""}`));
    button.addEventListener("click", () => { state.selectedPath = item.path; renderDetail(); });
    list.append(button);
  }
  if (selected) {
    const header = node("header");
    header.append(node("span", "", selected.path), node("span", "", `${selected.kind} · ${formatBytes(selected.bytes)}`));
    viewer.append(header, node("pre", "", selected.content));
  } else {
    viewer.append(node("div", "error", "该 Flow 没有可读取的文本产物。"));
  }
  grid.append(list, viewer);
  return grid;
}

function renderDetail() {
  const run = state.selectedRun;
  const detail = document.querySelector("#detail");
  detail.replaceChildren();

  const header = node("header", "detail-header");
  const title = node("div");
  title.append(node("p", "eyebrow", "Completed flow instance"), node("h2", "", run.workflow), node("p", "detail-subtitle", run.run_id));
  header.append(title, node("span", "status", run.status));
  detail.append(header);

  const stats = node("div", "stats");
  stats.append(
    stat("完成时间", formatTime(run.finished_at)),
    stat("运行时长", formatDuration(run.duration_seconds)),
    stat("Agent / Stage", `${run.agents.length} / ${run.stage_count}`),
    stat("源码版本", run.target_head ? run.target_head.slice(0, 12) : "—"),
  );
  detail.append(stats);

  const overview = node("section", "section");
  overview.append(sectionTitle("Flow 信息", run.cao_session || "旧版独立 session flow"));
  const summary = node("div", "summary card");
  summary.textContent = run.summary || `目标：${run.target || "未记录"}。完整性校验：${run.integrity?.ok === true ? "通过" : run.integrity?.ok === false ? "失败" : "未记录"}。事件数：${run.event_count}。`;
  overview.append(summary);
  detail.append(overview);

  const agents = node("section", "section");
  agents.append(sectionTitle("Agents 与 Sessions", `${run.agents.length} 个 Agent 记录`));
  const agentList = node("div", "agent-list");
  for (const agent of run.agents) agentList.append(renderAgent(agent));
  agents.append(agentList);
  detail.append(agents);

  const artifacts = node("section", "section");
  artifacts.append(sectionTitle("Flow 产物", `${run.deliverables.length} 个文本文件；点击左侧文件查看原文`));
  artifacts.append(renderArtifacts(run));
  detail.append(artifacts);
}

document.querySelector("#refresh").addEventListener("click", loadRuns);
loadRuns();
