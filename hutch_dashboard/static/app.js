const state = { runs: [], selectedRun: null, selectedPath: null, selectedGraph: null, graphMode: "execution" };
const SVG_NS = "http://www.w3.org/2000/svg";

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
    const graphNodes = state.selectedRun.graph?.nodes || [];
    state.selectedGraph = graphNodes.length
      ? { type: "node", id: graphNodes[graphNodes.length - 1].id }
      : null;
    state.graphMode = "execution";
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

const svgNode = (tag, attributes = {}) => {
  const value = document.createElementNS(SVG_NS, tag);
  for (const [key, attribute] of Object.entries(attributes)) value.setAttribute(key, String(attribute));
  return value;
};

function graphLayout(graph) {
  const levels = Object.fromEntries(graph.nodes.map(item => [item.id, item.type === "supervisor" ? 0 : 0]));
  const layoutEdges = graph.edges.filter(item => item.type !== "data");
  for (let round = 0; round < graph.nodes.length + 2; round += 1) {
    let changed = false;
    for (const edge of layoutEdges) {
      const next = (levels[edge.source] || 0) + 1;
      if (next > (levels[edge.target] || 0)) {
        levels[edge.target] = next;
        changed = true;
      }
    }
    if (!changed) break;
  }
  const maxLevel = Math.max(0, ...Object.values(levels));
  const byLevel = new Map();
  for (const item of graph.nodes) {
    const level = levels[item.id] || 0;
    if (!byLevel.has(level)) byLevel.set(level, []);
    byLevel.get(level).push(item);
  }
  const maxRows = Math.max(1, ...[...byLevel.values()].map(items => items.length));
  const width = Math.max(920, (maxLevel + 1) * 220);
  const height = Math.max(390, maxRows * 125 + 90);
  const positions = {};
  for (const [level, items] of byLevel.entries()) {
    const x = maxLevel === 0 ? width / 2 : 105 + level * ((width - 210) / maxLevel);
    items.forEach((item, index) => {
      positions[item.id] = { x, y: ((index + 1) * height) / (items.length + 1) };
    });
  }
  return { width, height, positions };
}

function shortLabel(value) {
  if (!value) return "Agent";
  return value.length > 29 ? `${value.slice(0, 27)}…` : value;
}

function connectedToSelection(edge) {
  if (!state.selectedGraph) return false;
  if (state.selectedGraph.type === "edge") return state.selectedGraph.id === edge.id;
  return edge.source === state.selectedGraph.id || edge.target === state.selectedGraph.id;
}

function graphInspector(run) {
  const panel = node("aside", "graph-inspector");
  const selection = state.selectedGraph;
  if (!selection) {
    panel.append(node("div", "graph-empty", "点击节点或连线查看详细信息。"));
    return panel;
  }
  if (selection.type === "edge") {
    const edge = run.graph.edges.find(item => item.id === selection.id);
    if (!edge) return panel;
    panel.append(node("p", "eyebrow", "Edge detail"), node("h4", "", `${edge.source} → ${edge.target}`));
    panel.append(fact("关系类型", edge.type));
    const description = edge.type === "dispatch"
      ? "Supervisor 通过 CAO 创建并调度根阶段 Agent。"
      : edge.type === "data"
        ? "下游 Agent 读取上游 Agent 的持久化交付物。"
        : edge.type === "dependency"
          ? "下游阶段必须等待上游阶段通过 Hutch 产物校验。"
          : "该边既是阶段完成依赖，也传递持久化文件。";
    panel.append(node("p", "graph-description", description));
    panel.append(node("h5", "", `传递产物 ${edge.transfers.length}`));
    const transferList = node("div", "graph-files");
    if (!edge.transfers.length) transferList.append(node("span", "graph-muted", "无直接文件；边表示调度或状态依赖。"));
    for (const path of edge.transfers) {
      const button = node("button", "graph-file", path);
      button.type = "button";
      button.addEventListener("click", () => openArtifact(path));
      transferList.append(button);
    }
    panel.append(transferList);
    return panel;
  }
  const agent = run.agents.find(item => item.stage === selection.id);
  if (!agent) return panel;
  panel.append(node("p", "eyebrow", agent.stage === "flow-supervisor" ? "Supervisor node" : "Agent node"));
  panel.append(node("h4", "", agent.profile || agent.stage));
  panel.append(fact("阶段", agent.stage), fact("状态", agent.status), fact("候选记录", agent.finding_count));
  if (agent.result_summary) panel.append(node("p", "graph-description", agent.result_summary));
  panel.append(node("h5", "", `CAO 执行 ${agent.assignments.length}`));
  for (const assignment of agent.assignments) {
    const text = [assignment.session, assignment.terminal_id, assignment.window].filter(Boolean).join(" / ");
    panel.append(node("div", "graph-runtime", text || "未记录 runtime 标识"));
  }
  panel.append(node("h5", "", `交付物 ${agent.deliverables.length}`));
  const files = node("div", "graph-files");
  if (!agent.deliverables.length) files.append(node("span", "graph-muted", "该节点没有直接文件交付物。"));
  for (const item of agent.deliverables) {
    const button = node("button", "graph-file", item.path);
    button.type = "button";
    button.addEventListener("click", () => openArtifact(item.path));
    files.append(button);
  }
  panel.append(files);
  return panel;
}

function openArtifact(path) {
  if (!state.selectedRun.deliverables.some(item => item.path === path)) return;
  state.selectedPath = path;
  renderDetail();
  requestAnimationFrame(() => document.querySelector("#flow-artifacts")?.scrollIntoView({ behavior: "smooth", block: "start" }));
}

function renderFlowGraph(run) {
  const workspace = node("div", "graph-workspace card");
  const canvas = node("div", "graph-canvas");
  const graph = run.graph || { nodes: [], edges: [] };
  const visibleEdges = state.graphMode === "data"
    ? graph.edges
    : graph.edges.filter(item => item.type !== "data");
  const layout = graphLayout(graph);
  const svg = svgNode("svg", {
    class: "flow-graph",
    viewBox: `0 0 ${layout.width} ${layout.height}`,
    role: "img",
    "aria-label": `${run.workflow} flow graph`,
  });
  const defs = svgNode("defs");
  const marker = svgNode("marker", { id: "flow-arrow", viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
  marker.append(svgNode("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "arrow-head" }));
  defs.append(marker);
  svg.append(defs);

  for (const edge of visibleEdges) {
    const source = layout.positions[edge.source];
    const target = layout.positions[edge.target];
    if (!source || !target) continue;
    const x1 = source.x + 83;
    const x2 = target.x - 83;
    const bend = Math.max(35, (x2 - x1) * 0.45);
    const pathValue = `M ${x1} ${source.y} C ${x1 + bend} ${source.y}, ${x2 - bend} ${target.y}, ${x2} ${target.y}`;
    const group = svgNode("g", {
      class: `graph-edge edge-${edge.type}${connectedToSelection(edge) ? " connected" : ""}${state.selectedGraph?.type === "edge" && state.selectedGraph.id === edge.id ? " selected" : ""}`,
      tabindex: 0,
      role: "button",
      "aria-label": `${edge.source} to ${edge.target}`,
    });
    group.append(svgNode("path", { d: pathValue, class: "edge-visible", "marker-end": "url(#flow-arrow)" }));
    group.append(svgNode("path", { d: pathValue, class: "edge-hit" }));
    const activate = () => { state.selectedGraph = { type: "edge", id: edge.id }; renderDetail(); };
    group.addEventListener("click", activate);
    group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") activate(); });
    svg.append(group);
  }

  for (const item of graph.nodes) {
    const position = layout.positions[item.id];
    const selected = state.selectedGraph?.type === "node" && state.selectedGraph.id === item.id;
    const group = svgNode("g", {
      class: `graph-node node-${item.type}${selected ? " selected" : ""}`,
      transform: `translate(${position.x - 83} ${position.y - 36})`,
      tabindex: 0,
      role: "button",
      "aria-label": item.label,
    });
    group.append(svgNode("rect", { width: 166, height: 72, rx: 11 }));
    const label = svgNode("text", { x: 13, y: 27, class: "node-label" });
    label.textContent = shortLabel(item.id);
    const stage = svgNode("text", { x: 13, y: 48, class: "node-stage" });
    stage.textContent = shortLabel(item.label);
    const dot = svgNode("circle", { cx: 151, cy: 17, r: 5, class: "node-status" });
    group.append(label, stage, dot);
    const activate = () => { state.selectedGraph = { type: "node", id: item.id }; renderDetail(); };
    group.addEventListener("click", activate);
    group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") activate(); });
    svg.append(group);
  }
  canvas.append(svg);
  const controls = node("div", "graph-controls");
  for (const [mode, label] of [["execution", "执行依赖"], ["data", "完整产物流"]]) {
    const button = node("button", state.graphMode === mode ? "active" : "", label);
    button.type = "button";
    button.addEventListener("click", () => {
      state.graphMode = mode;
      if (mode === "execution" && state.selectedGraph?.type === "edge") {
        const selected = graph.edges.find(item => item.id === state.selectedGraph.id);
        if (selected?.type === "data") state.selectedGraph = { type: "node", id: selected.target };
      }
      renderDetail();
    });
    controls.append(button);
  }
  canvas.append(controls);
  const legend = node("div", "graph-legend");
  for (const [className, text] of [["dispatch", "CAO dispatch"], ["dependency", "dependency"], ["data", "artifact data"]]) {
    const item = node("span");
    item.append(node("i", `legend-${className}`), document.createTextNode(text));
    legend.append(item);
  }
  canvas.append(legend);
  workspace.append(canvas, graphInspector(run));
  return workspace;
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

  const graphSection = node("section", "section");
  graphSection.append(sectionTitle("Flow Graph", `${run.graph.nodes.length} nodes · ${run.graph.edges.length} edges · 默认展示执行依赖，可切换完整产物流`));
  graphSection.append(renderFlowGraph(run));
  detail.append(graphSection);

  const agents = node("section", "section");
  agents.append(sectionTitle("Agents 与 Sessions", `${run.agents.length} 个 Agent 记录`));
  const agentList = node("div", "agent-list");
  for (const agent of run.agents) agentList.append(renderAgent(agent));
  agents.append(agentList);
  detail.append(agents);

  const artifacts = node("section", "section");
  artifacts.id = "flow-artifacts";
  artifacts.append(sectionTitle("Flow 产物", `${run.deliverables.length} 个文本文件；点击左侧文件查看原文`));
  artifacts.append(renderArtifacts(run));
  detail.append(artifacts);
}

document.querySelector("#refresh").addEventListener("click", loadRuns);
loadRuns();
