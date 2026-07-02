const TREE_COLLAPSE_STORAGE = "hutch.collapsed-project-nodes.v1";
const PROJECT_ONLY_STORAGE = "hutch.project-only.v1";
const FLOW_ONLY_STORAGE = "hutch.flow-only.v1";
const SIDEBAR_COLLAPSED_STORAGE = "hutch.sidebar-collapsed.v1";
const QU_AGENT_COLLAPSED_STORAGE = "hutch.qu-agent-collapsed.v1";

const loadStoredSet = key => {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "[]");
    return new Set(Array.isArray(value) ? value.filter(item => typeof item === "string") : []);
  } catch (_error) {
    return new Set();
  }
};

const loadStoredBoolean = key => {
  try {
    return localStorage.getItem(key) === "true";
  } catch (_error) {
    return false;
  }
};

const state = {
  projects: [],
  runs: [],
  campaigns: [],
  selectedProject: null,
  view: "projects",
  selectedRun: null,
  selectedCampaign: null,
  selectedPath: null,
  artifactMode: "rendered",
  selectedGraph: null,
  graphMode: "execution",
  graphView: { scale: 1, x: 0, y: 0 },
  terminalId: null,
  terminalTimer: null,
  caoCatalog: null,
  agentsStore: null,
  flowsStore: null,
  pendingDeleteRun: null,
  quAgent: null,
  quAgentLoading: false,
  xtermTerminal: null,
  editingAgent: null,
  activeSkillInfo: null,
  collapsedProjectNodes: loadStoredSet(TREE_COLLAPSE_STORAGE),
  projectOnly: loadStoredBoolean(PROJECT_ONLY_STORAGE),
  flowOnly: loadStoredBoolean(FLOW_ONLY_STORAGE),
  sidebarCollapsed: loadStoredBoolean(SIDEBAR_COLLAPSED_STORAGE),
  quAgentCollapsed: loadStoredBoolean(QU_AGENT_COLLAPSED_STORAGE),
};
const SVG_NS = "http://www.w3.org/2000/svg";

const node = (tag, className, text) => {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined && text !== null) value.textContent = String(text);
  return value;
};

function artifactFilename(path) {
  const name = String(path || "").split("/").filter(Boolean).pop() || "hutch-artifact.txt";
  return name.replace(/[^\w.-]+/g, "-") || "hutch-artifact.txt";
}

function artifactMimeType(path) {
  const lower = String(path || "").toLowerCase();
  if (lower.endsWith(".md")) return "text/markdown;charset=utf-8";
  if (lower.endsWith(".json") || lower.endsWith(".jsonl")) return "application/json;charset=utf-8";
  return "text/plain;charset=utf-8";
}

function downloadArtifact(item) {
  const blob = new Blob([item.content || ""], { type: artifactMimeType(item.path) });
  const url = URL.createObjectURL(blob);
  const link = node("a");
  link.href = url;
  link.download = artifactFilename(item.path);
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function isFinalReportArtifact(item) {
  const path = String(item?.path || "").toLowerCase();
  const stage = String(item?.stage || "").toLowerCase();
  const filename = path.split("/").pop() || path;
  if (!path || path.startsWith("outbox/") || filename.endsWith(".json")) return false;
  return filename.includes("final-report")
    || filename.includes("final_report")
    || (item.kind === "final" && filename.includes("report"))
    || stage === "final-report";
}

function finalReportArtifactForStage(run, stageId) {
  return (run.deliverables || []).find(item => item.stage === stageId && isFinalReportArtifact(item));
}

const persistViewPreferences = () => {
  try {
    localStorage.setItem(TREE_COLLAPSE_STORAGE, JSON.stringify([...state.collapsedProjectNodes]));
    localStorage.setItem(PROJECT_ONLY_STORAGE, String(state.projectOnly));
    localStorage.setItem(FLOW_ONLY_STORAGE, String(state.flowOnly));
    localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE, String(state.sidebarCollapsed));
    localStorage.setItem(QU_AGENT_COLLAPSED_STORAGE, String(state.quAgentCollapsed));
  } catch (_error) {
    // The dashboard remains functional when browser storage is unavailable.
  }
};

const treeNodeKey = (projectId, itemId) => `${projectId}:${itemId}`;

function bindCollapsible(trigger, content, key) {
  trigger.dataset.collapseKey = key;
  content.dataset.collapseContent = key;
  const apply = (currentTrigger = trigger, currentContent = content) => {
    const collapsed = state.collapsedProjectNodes.has(key);
    currentContent.hidden = collapsed;
    currentTrigger.setAttribute("aria-expanded", String(!collapsed));
    currentTrigger.title = collapsed ? "展开" : "收起";
    const currentIndicator = currentTrigger.querySelector(".tree-chevron");
    if (currentIndicator) currentIndicator.textContent = collapsed ? "▸" : "▾";
  };
  trigger.addEventListener("click", event => {
    event.stopPropagation();
    if (state.collapsedProjectNodes.has(key)) state.collapsedProjectNodes.delete(key);
    else state.collapsedProjectNodes.add(key);
    persistViewPreferences();
    const triggers = [...document.querySelectorAll("[data-collapse-key]")]
      .filter(item => item.dataset.collapseKey === key);
    const contents = [...document.querySelectorAll("[data-collapse-content]")]
      .filter(item => item.dataset.collapseContent === key);
    triggers.forEach((item, index) => apply(item, contents[index] || content));
  });
  apply();
}

function updateProjectOnlyControl() {
  const button = document.querySelector("#project-only");
  button.setAttribute("aria-pressed", String(state.projectOnly));
  button.classList.toggle("active", state.projectOnly);
  button.title = state.projectOnly
    ? "恢复显示 Campaign、Flow 和报告"
    : "隐藏所有 Flow，只显示项目目录与微服务";
}

function updateFlowOnlyControl() {
  const button = document.querySelector("#flow-only");
  button.setAttribute("aria-pressed", String(state.flowOnly));
  button.classList.toggle("active", state.flowOnly);
  button.title = state.flowOnly
    ? "恢复显示所有微服务"
    : "只显示有 Flow 的微服务";
}

function updateSidebarCollapseControl() {
  const shell = document.querySelector(".shell");
  const button = document.querySelector("#sidebar-toggle");
  shell.classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
  button.setAttribute("aria-pressed", String(state.sidebarCollapsed));
  button.setAttribute("aria-label", state.sidebarCollapsed ? "展开左侧边栏" : "收起左侧边栏");
  button.title = state.sidebarCollapsed ? "展开左侧边栏" : "收起左侧边栏";
  button.textContent = state.sidebarCollapsed ? "›" : "‹";
}

function updateQuAgentPanel() {
  const panel = document.querySelector("#qu-agent-panel");
  const status = document.querySelector("#qu-agent-status");
  const body = document.querySelector("#qu-agent-body");
  const toggle = document.querySelector("#qu-agent-toggle");
  const meta = document.querySelector("#qu-agent-meta");
  const errorBox = document.querySelector("#qu-agent-error");
  const start = document.querySelector("#qu-agent-start");
  const stop = document.querySelector("#qu-agent-stop");
  const open = document.querySelector("#qu-agent-open");
  const agent = state.quAgent || {};
  const live = Boolean(agent.live);
  panel.classList.toggle("collapsed", state.quAgentCollapsed);
  panel.classList.toggle("live", live);
  panel.classList.toggle("loading", state.quAgentLoading);
  body.hidden = state.quAgentCollapsed;
  toggle.setAttribute("aria-expanded", String(!state.quAgentCollapsed));
  toggle.textContent = state.quAgentCollapsed ? "展开" : "收起";
  status.textContent = state.quAgentLoading ? "处理中" : live ? "运行中" : "未运行";
  start.disabled = state.quAgentLoading || live;
  stop.disabled = state.quAgentLoading || !live;
  open.disabled = !live || !agent.websocket_path;
  open.title = live
    ? "在 Hutch 中打开 QU tmux terminal"
    : "QU Agent 未运行";
  const session = agent.session || "hutch-qu-agent";
  const windowName = agent.window || "codex";
  const workspace = agent.working_directory || "Hutch workspace";
  meta.textContent = `codex · ${session}:${windowName} · ${workspace}`;
  errorBox.hidden = !agent.error;
  errorBox.textContent = agent.error ? `ERROR: ${agent.error}` : "";
}

async function refreshQuAgent() {
  state.quAgentLoading = true;
  updateQuAgentPanel();
  try {
    state.quAgent = await fetchJSON("/api/qu-agent");
  } catch (error) {
    state.quAgent = { error: error.message, live: false };
  } finally {
    state.quAgentLoading = false;
    updateQuAgentPanel();
  }
}

async function startQuAgent() {
  state.quAgentLoading = true;
  updateQuAgentPanel();
  try {
    state.quAgent = await postJSON("/api/qu-agent/start", {});
  } catch (error) {
    state.quAgent = { error: error.message, live: false };
  } finally {
    state.quAgentLoading = false;
    updateQuAgentPanel();
  }
}

async function stopQuAgent() {
  state.quAgentLoading = true;
  updateQuAgentPanel();
  try {
    state.quAgent = await postJSON("/api/qu-agent/stop", {});
    closeXtermTerminal();
  } catch (error) {
    state.quAgent = { ...state.quAgent, error: error.message };
  } finally {
    state.quAgentLoading = false;
    updateQuAgentPanel();
  }
}

function websocketURL(path) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}

function cleanupXtermTerminal() {
  if (!state.xtermTerminal) return;
  const current = state.xtermTerminal;
  state.xtermTerminal = null;
  if (current.resizeObserver) current.resizeObserver.disconnect();
  if (current.ws) {
    current.ws.onclose = null;
    current.ws.onerror = null;
    if (current.ws.readyState === WebSocket.CONNECTING || current.ws.readyState === WebSocket.OPEN) {
      current.ws.close();
    }
  }
  if (current.term) current.term.dispose();
}

function closeXtermTerminal() {
  const dialog = document.querySelector("#xterm-dialog");
  cleanupXtermTerminal();
  if (dialog.open) dialog.close();
}

function sendXtermTerminalResize() {
  const current = state.xtermTerminal;
  if (!current?.ws || !current?.term || current.ws.readyState !== WebSocket.OPEN) return;
  current.fitAddon.fit();
  current.ws.send(JSON.stringify({ type: "resize", rows: current.term.rows, cols: current.term.cols }));
}

async function openXtermTerminal({ title, meta, websocketPath, errorTarget = null }) {
  if (!websocketPath) return;
  const dialog = document.querySelector("#xterm-dialog");
  const screen = document.querySelector("#xterm-screen");
  const titleNode = document.querySelector("#xterm-title");
  const metaNode = document.querySelector("#xterm-meta");
  const TerminalCtor = window.Terminal;
  const FitAddonCtor = window.FitAddon?.FitAddon || window.FitAddon;
  if (!TerminalCtor || !FitAddonCtor) {
    const error = "xterm.js 未加载，无法打开 terminal。";
    if (errorTarget === "qu") {
      state.quAgent = { ...state.quAgent, error };
      updateQuAgentPanel();
    }
    return;
  }
  cleanupXtermTerminal();
  screen.replaceChildren();
  titleNode.textContent = title || "Terminal";
  metaNode.textContent = meta || "";
  if (!dialog.open) dialog.showModal();

  const term = new TerminalCtor({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "JetBrains Mono, Menlo, Monaco, Consolas, monospace",
    scrollback: 10000,
    theme: {
      background: "#0d1117",
      foreground: "#c9d1d9",
      cursor: "#58a6ff",
      selectionBackground: "#264f78",
      black: "#0d1117",
      red: "#ff7b72",
      green: "#3fb950",
      yellow: "#d29922",
      blue: "#58a6ff",
      magenta: "#bc8cff",
      cyan: "#39d353",
      white: "#c9d1d9",
    },
  });
  const fitAddon = new FitAddonCtor();
  term.loadAddon(fitAddon);
  term.open(screen);

  const ws = new WebSocket(websocketURL(websocketPath));
  ws.binaryType = "arraybuffer";
  state.xtermTerminal = { term, fitAddon, ws, resizeObserver: null };

  ws.onopen = () => {
    sendXtermTerminalResize();
    term.focus();
  };
  ws.onmessage = event => {
    if (event.data instanceof ArrayBuffer) {
      term.write(new Uint8Array(event.data));
    } else if (event.data instanceof Blob) {
      event.data.arrayBuffer().then(buffer => term.write(new Uint8Array(buffer)));
    }
  };
  ws.onclose = () => {
    term.write("\r\n\x1b[33m[Connection closed]\x1b[0m\r\n");
  };
  ws.onerror = () => {
    term.write("\r\n\x1b[31m[WebSocket error]\x1b[0m\r\n");
  };
  term.onData(data => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "input", data }));
    }
  });
  term.attachCustomKeyEventHandler(event => {
    if (event.ctrlKey && event.shiftKey && event.key === "C") {
      const selection = term.getSelection();
      if (selection) navigator.clipboard.writeText(selection).catch(() => {});
      return false;
    }
    return true;
  });
  term.onSelectionChange(() => {
    const selection = term.getSelection();
    if (selection) navigator.clipboard.writeText(selection).catch(() => {});
  });

  let resizeTimer = null;
  const resizeObserver = new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(sendXtermTerminalResize, 50);
  });
  resizeObserver.observe(screen);
  state.xtermTerminal.resizeObserver = resizeObserver;
  requestAnimationFrame(sendXtermTerminalResize);
}

async function openQuAgentTerminal() {
  if (!state.quAgent?.live || !state.quAgent.websocket_path) return;
  await openXtermTerminal({
    title: "QU Agent",
    meta: `codex · ${state.quAgent.session}:${state.quAgent.window} · ${state.quAgent.working_directory}`,
    websocketPath: state.quAgent.websocket_path,
    errorTarget: "qu",
  });
}

function updateStoreNav() {
  document.querySelector("#agents-store").classList.toggle("active", state.view === "agents_store");
  document.querySelector("#flows-store").classList.toggle("active", state.view === "flows_store");
}

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

async function postJSON(url, value) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

async function deleteJSON(url) {
  const response = await fetch(url, { method: "DELETE" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

const deletableRun = run => !["launching", "running"].includes(run.status);

async function deleteRun(run) {
  if (!deletableRun(run)) return;
  state.pendingDeleteRun = run;
  document.querySelector("#delete-message").textContent = `确定删除 ${run.workflow} 的这次 Flow 运行记录吗？`;
  document.querySelector("#delete-run-id").textContent = run.run_id;
  document.querySelector("#delete-error").hidden = true;
  const confirm = document.querySelector("#delete-confirm");
  confirm.disabled = false;
  confirm.textContent = "确认删除";
  const dialog = document.querySelector("#delete-dialog");
  if (!dialog.open) dialog.showModal();
}

const stripAnsi = value => String(value || "")
  .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
  .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
  .replace(/\r/g, "");

function serviceHasFlow(item) {
  return (item.flow_count || 0) > 0
    || (item.flows || []).length > 0
    || state.campaigns.some(campaign => campaign.service?.id === item.id);
}

function visibleServiceCount(items) {
  return items.reduce((total, item) => {
    if (item.type === "directory") return total + visibleServiceCount(item.children || []);
    return total + (!state.flowOnly || serviceHasFlow(item) ? 1 : 0);
  }, 0);
}

function projectVisibleServiceCount(project) {
  return visibleServiceCount(project.tree?.children || []);
}

function sidebarProjects() {
  if (!state.flowOnly) return state.projects;
  return state.projects.filter(project => projectVisibleServiceCount(project) > 0);
}

function updateRunCount() {
  const count = document.querySelector("#run-count");
  const projects = sidebarProjects();
  const serviceCount = state.flowOnly
    ? projects.reduce((total, project) => total + projectVisibleServiceCount(project), 0)
    : state.projects.reduce((total, project) => total + (project.service_count || 0), 0);
  if (state.projectOnly || state.flowOnly) {
    count.textContent = state.flowOnly
      ? `${projects.length} 项目 · ${serviceCount} 有 Flow 微服务`
      : `${state.projects.length} 项目 · ${serviceCount} 微服务`;
    return;
  }
  count.textContent = `${state.projects.length} 项目 · ${state.campaigns.length} 总 Flow · ${state.runs.length} 子 Flow`;
}

async function loadRuns() {
  const count = document.querySelector("#run-count");
  count.textContent = "加载中";
  try {
    [state.projects, state.campaigns] = await Promise.all([
      fetchJSON("/api/projects"),
      fetchJSON("/api/campaigns"),
    ]);
    state.runs = state.projects.flatMap(project => project.flows || []);
    updateRunCount();
    updateProjectOnlyControl();
    updateFlowOnlyControl();
    renderRunList();
    if (state.view === "agents_store") {
      await selectAgentsStore(true);
      return;
    }
    if (state.view === "flows_store") {
      await selectFlowsStore(true);
      return;
    }
    const selectedId = state.selectedRun?.run_id;
    const selectedCampaignId = state.selectedCampaign?.instance_id;
    if (!state.projectOnly && state.view === "flow" && selectedId && state.runs.some(run => run.run_id === selectedId)) {
      await selectRun(selectedId);
    } else if (!state.projectOnly && state.view === "campaign" && selectedCampaignId && state.campaigns.some(item => item.instance_id === selectedCampaignId)) {
      await selectCampaign(selectedCampaignId);
    } else if (sidebarProjects().length) {
      const visible = sidebarProjects();
      const selectedVisible = visible.some(project => project.id === state.selectedProject?.id);
      selectProject(selectedVisible ? state.selectedProject.id : visible[0].id);
    } else {
      const detail = document.querySelector("#detail");
      state.selectedProject = null;
      state.selectedRun = null;
      state.selectedCampaign = null;
      state.view = "projects";
      detail.replaceChildren(node("div", "empty-state", state.flowOnly ? "没有有 Flow 的微服务。" : "没有项目。"));
    }
  } catch (error) {
    count.textContent = "加载失败";
    document.querySelector("#run-list").replaceChildren(node("div", "error", error.message));
  }
}

function renderRunList() {
  updateStoreNav();
  const list = document.querySelector("#run-list");
  list.replaceChildren();
  for (const project of sidebarProjects()) {
    const group = node("section", `project-group${state.selectedProject?.id === project.id ? " active" : ""}`);
    const header = node("div", "project-header");
    const collapse = node("button", "tree-collapse-toggle");
    collapse.type = "button";
    collapse.setAttribute("aria-label", `收起或展开项目 ${project.name}`);
    const indicator = node("span", "tree-chevron", "▾");
    collapse.append(indicator);
    const select = node("button", "project-select");
    select.type = "button";
    const identity = node("div", "project-identity");
    identity.append(node("span", "project-name", project.name), node("span", "project-path", project.root_path || project.repo_path));
    select.append(identity);
    select.addEventListener("click", () => selectProject(project.id));
    header.append(collapse, select, node("span", "project-count", state.flowOnly ? projectVisibleServiceCount(project) : project.service_count));
    group.append(header);
    const tree = node("div", "project-tree-sidebar");
    renderSidebarTree(project.tree?.children || [], tree, project.id, 0);
    bindCollapsible(collapse, tree, `project:${project.id}`);
    group.append(tree);
    list.append(group);
  }
}

function renderSidebarTree(items, container, projectId, depth) {
  for (const item of items) {
    const key = treeNodeKey(projectId, item.id);
    if (item.type === "directory") {
      if (state.flowOnly && visibleServiceCount(item.children || []) === 0) continue;
      const branch = node("section", "sidebar-tree-branch");
      branch.style.setProperty("--tree-depth", depth);
      const title = node("button", "sidebar-tree-title tree-collapse-trigger");
      title.type = "button";
      const indicator = node("span", "tree-chevron", "▾");
      const label = node("span", "tree-folder");
      label.append(indicator, node("span", "", item.name));
      title.append(label, node("span", "", `${state.flowOnly ? visibleServiceCount(item.children || []) : item.service_count} 服务`));
      branch.append(title);
      const children = node("div", "sidebar-tree-children");
      renderSidebarTree(item.children || [], children, projectId, depth + 1);
      bindCollapsible(title, children, key);
      branch.append(children);
      container.append(branch);
      continue;
    }
    if (state.flowOnly && !serviceHasFlow(item)) continue;
    const serviceBox = node("div", "sidebar-service");
    serviceBox.style.setProperty("--tree-depth", depth);
    const serviceTitle = node(
      state.projectOnly ? "div" : "button",
      `sidebar-service-title${state.projectOnly ? "" : " tree-collapse-trigger"}`,
    );
    if (!state.projectOnly) serviceTitle.type = "button";
    const indicator = node(
      "span",
      state.projectOnly ? "tree-leaf-marker" : "tree-chevron",
      state.projectOnly ? "•" : "▾",
    );
    const label = node("span", "tree-folder");
    label.append(indicator, node("span", "", item.name));
    serviceTitle.append(
      label,
      node("span", "", state.projectOnly ? "微服务" : `${item.flow_count} Flow`),
    );
    serviceBox.append(serviceTitle);
    if (!state.projectOnly) {
      const flowContent = node("div", "sidebar-service-content");
      const campaigns = state.campaigns.filter(campaign => campaign.service?.id === item.id);
      for (const campaign of campaigns) {
        const button = node("button", `run-item campaign-item${state.selectedCampaign?.instance_id === campaign.instance_id ? " active" : ""}`);
        button.type = "button";
        button.append(node("span", "campaign-badge", "总 FLOW"));
        button.append(node("span", "run-name", campaign.campaign_id));
        button.append(node("span", "run-id", `${campaign.flow_count} 个子 Flow · ${campaign.phases.join(" → ")}`));
        const meta = node("span", "run-meta");
        meta.append(node("span", `status status-${campaign.status}`, campaign.status));
        meta.append(node("span", "", `${campaign.stages_done}/${campaign.stages_total} stages`));
        button.append(meta);
        button.addEventListener("click", () => selectCampaign(campaign.instance_id));
        flowContent.append(button);
      }
      for (const run of item.flows || []) {
        const button = node("button", `run-item${state.selectedRun?.run_id === run.run_id ? " active" : ""}`);
        button.type = "button";
        button.append(node("span", "run-name", run.workflow));
        button.append(node("span", "run-id", run.run_id));
        const meta = node("span", "run-meta");
        meta.append(node("span", `status status-${run.status}`, run.status));
        meta.append(node("span", "", formatTime(run.finished_at)));
        button.append(meta);
        button.addEventListener("click", () => selectRun(run.run_id));
        flowContent.append(button);
      }
      bindCollapsible(serviceTitle, flowContent, key);
      serviceBox.append(flowContent);
    }
    container.append(serviceBox);
  }
}

function selectProject(projectId) {
  const project = state.projects.find(item => item.id === projectId);
  if (!project) return;
  state.selectedProject = project;
  state.selectedRun = null;
  state.selectedCampaign = null;
  state.selectedPath = null;
  state.view = "projects";
  renderRunList();
  renderProjectDetail(project);
}

async function selectRun(runId) {
  const detail = document.querySelector("#detail");
  detail.replaceChildren(node("div", "empty-state", "正在读取 Flow 证据…"));
  try {
    state.selectedRun = await fetchJSON(`/api/runs/${encodeURIComponent(runId)}`);
    state.selectedCampaign = null;
    state.selectedProject = state.projects.find(project => project.id === state.selectedRun.project?.id)
      || state.selectedRun.project;
    state.view = "flow";
    const preferred = state.selectedRun.deliverables.find(item => item.path.includes("final-report"))
      || state.selectedRun.deliverables.find(item => item.kind === "final")
      || state.selectedRun.deliverables[0];
    state.selectedPath = preferred?.path || null;
    state.artifactMode = preferred?.path?.toLowerCase().endsWith(".md") ? "rendered" : "raw";
    const graphNodes = state.selectedRun.graph?.nodes || [];
    state.selectedGraph = graphNodes.length
      ? { type: "node", id: graphNodes[graphNodes.length - 1].id }
      : null;
    state.graphMode = "execution";
    state.graphView = { scale: 1, x: 0, y: 0 };
    renderRunList();
    renderDetail();
  } catch (error) {
    detail.replaceChildren(node("div", "error", `读取实例失败：${error.message}`));
  }
}

async function selectCampaign(instanceId) {
  const detail = document.querySelector("#detail");
  detail.replaceChildren(node("div", "empty-state", "正在整合 Campaign 证据…"));
  try {
    state.selectedCampaign = await fetchJSON(`/api/campaigns/${encodeURIComponent(instanceId)}`);
    state.selectedRun = null;
    state.selectedProject = state.projects.find(project => project.id === state.selectedCampaign.project?.id)
      || state.selectedCampaign.project;
    state.view = "campaign";
    const graphNodes = state.selectedCampaign.graph?.nodes || [];
    state.selectedGraph = graphNodes.length
      ? { type: "node", id: graphNodes[graphNodes.length - 1].id }
      : null;
    state.graphView = { scale: 1, x: 0, y: 0 };
    renderRunList();
    renderCampaignDetail();
  } catch (error) {
    detail.replaceChildren(node("div", "error", `读取总 Flow 失败：${error.message}`));
  }
}

function renderStoreChips(items, emptyText) {
  const list = node("div", "store-chip-list");
  if (!items.length) {
    list.append(node("span", "graph-muted", emptyText));
    return list;
  }
  for (const item of items) list.append(node("span", "store-chip", item));
  return list;
}

function skillMetadataValue(value) {
  if (Array.isArray(value)) return value.map(skillMetadataValue).join(", ");
  if (value && typeof value === "object") {
    return Object.entries(value)
      .map(([key, child]) => `${key}: ${skillMetadataValue(child)}`)
      .join(" · ");
  }
  if (typeof value === "boolean") return value ? "true" : "false";
  return value === undefined || value === null || value === "" ? "—" : String(value);
}

function skillMetadataEntries(metadata) {
  const rows = [];
  for (const [key, value] of Object.entries(metadata || {})) {
    if (key === "name" || key === "description") continue;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const [childKey, childValue] of Object.entries(value)) {
        rows.push([`${key}.${childKey}`, childValue]);
      }
    } else {
      rows.push([key, value]);
    }
  }
  return rows;
}

function renderSkillPopover(skill) {
  const metadata = skill.metadata || {};
  const panel = node("div", "skill-popover");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", `${skill.name} Skill metadata`);
  panel.addEventListener("click", event => event.stopPropagation());
  panel.append(
    node("p", "eyebrow", "Skill.md header"),
    node("h6", "", metadata.name || skill.name),
  );
  panel.append(
    node(
      "p",
      "skill-popover-description",
      metadata.description || (skill.available ? "未记录 description。" : "未找到 SKILL.md header。"),
    ),
  );
  const rows = node("div", "skill-popover-rows");
  rows.append(skillPopoverRow("Declared", skill.name));
  for (const [key, value] of skillMetadataEntries(metadata)) {
    rows.append(skillPopoverRow(key, skillMetadataValue(value)));
  }
  rows.append(skillPopoverRow("Path", skill.path || "—"));
  panel.append(rows);
  return panel;
}

function skillPopoverRow(label, value) {
  const row = node("div", "skill-popover-row");
  row.append(node("label", "", label), node("span", "", value));
  return row;
}

function toggleSkillPopover(agentId, skillName) {
  const active = state.activeSkillInfo;
  state.activeSkillInfo = active?.agentId === agentId && active?.skillName === skillName
    ? null
    : { agentId, skillName };
  renderAgentsStoreDetail();
}

function renderAgentSkillChips(agent) {
  const list = node("div", "store-chip-list");
  const skills = Array.isArray(agent.skills) ? agent.skills : [];
  if (!skills.length) {
    list.append(node("span", "graph-muted", "未声明 Skills"));
    return list;
  }
  const details = new Map((agent.skill_details || []).map(item => [item.name, item]));
  for (const name of skills) {
    const skill = details.get(name) || { name, available: false, metadata: {}, path: "" };
    const active = state.activeSkillInfo?.agentId === agent.id
      && state.activeSkillInfo?.skillName === name;
    const wrap = node("span", "skill-chip-wrap");
    const button = node("button", `store-chip skill-chip${active ? " active" : ""}`, name);
    button.type = "button";
    button.setAttribute("aria-expanded", String(active));
    button.addEventListener("click", event => {
      event.stopPropagation();
      toggleSkillPopover(agent.id, name);
    });
    wrap.append(button);
    if (active) wrap.append(renderSkillPopover(skill));
    list.append(wrap);
  }
  return list;
}

function renderAgentInstructions(agent) {
  if (!agent.instructions_available) {
    return node("div", "graph-muted", "未找到 AGENTS.md 内容。");
  }
  const block = node("pre", "store-instructions", agent.instructions_content || "");
  block.title = agent.instructions_path || agent.instructions || "AGENTS.md";
  return block;
}

function setAgentEditorBusy(busy) {
  document.querySelector("#agent-edit-input").disabled = busy;
  document.querySelector("#agent-edit-cancel").disabled = busy;
  document.querySelector("#agent-edit-save").disabled = busy;
}

function openAgentEditor(agent) {
  state.editingAgent = agent;
  document.querySelector("#agent-edit-title").textContent = `编辑 ${agent.id} / AGENTS.md`;
  document.querySelector("#agent-edit-meta").textContent = agent.instructions_path || agent.path || "";
  document.querySelector("#agent-edit-input").value = agent.instructions_content || "";
  const error = document.querySelector("#agent-edit-error");
  error.hidden = true;
  error.textContent = "";
  setAgentEditorBusy(false);
  const dialog = document.querySelector("#agent-edit-dialog");
  if (!dialog.open) dialog.showModal();
  requestAnimationFrame(() => document.querySelector("#agent-edit-input").focus());
}

function closeAgentEditor() {
  state.editingAgent = null;
  document.querySelector("#agent-edit-error").hidden = true;
  setAgentEditorBusy(false);
}

async function saveAgentInstructions(event) {
  event.preventDefault();
  const agent = state.editingAgent;
  if (!agent) return;
  const content = document.querySelector("#agent-edit-input").value;
  const error = document.querySelector("#agent-edit-error");
  error.hidden = true;
  error.textContent = "";
  setAgentEditorBusy(true);
  try {
    state.agentsStore = await postJSON(
      `/api/stores/agents/${encodeURIComponent(agent.id)}/instructions`,
      { content },
    );
    document.querySelector("#agent-edit-dialog").close();
    renderAgentsStoreDetail();
  } catch (saveError) {
    error.textContent = `保存失败：${saveError.message}`;
    error.hidden = false;
  } finally {
    setAgentEditorBusy(false);
  }
}

function renderStoreHeader(kind, store, countLabel) {
  const header = node("header", "detail-header store-detail-header");
  const title = node("div");
  title.append(
    node("p", "eyebrow", "Hutch Store"),
    node("h2", "", kind),
    node("p", "detail-subtitle", store.path || "未配置 store 路径"),
  );
  const actions = node("div", "detail-actions");
  actions.append(node("span", `status${store.exists ? "" : " status-orphaned"}`, store.exists ? countLabel : "missing"));
  header.append(title, actions);
  return header;
}

function renderAgentsStoreDetail() {
  const store = state.agentsStore || { agents: [], exists: false };
  const detail = document.querySelector("#detail");
  detail.replaceChildren();
  const agents = Array.isArray(store.agents) ? store.agents : [];
  const skillTotal = agents.reduce((total, agent) => total + (agent.skill_count || 0), 0);
  const mcpTotal = agents.reduce((total, agent) => total + (agent.mcp_count || 0), 0);
  const instructionsTotal = agents.filter(agent => agent.instructions_available).length;
  detail.append(renderStoreHeader("agents_store", store, `${agents.length} roles`));
  const stats = node("div", "stats store-stats");
  stats.append(
    stat("Agent Roles", agents.length),
    stat("AGENTS.md", instructionsTotal),
    stat("Skills", skillTotal),
    stat("MCP Servers", mcpTotal),
    stat("Store", store.exists ? "available" : "missing"),
  );
  detail.append(stats);
  if (!store.exists) {
    detail.append(node("div", "project-notice", "当前 Agent Store 目录不存在。"));
    return;
  }
  const section = node("section", "section");
  section.append(sectionTitle("Agent Store", "展示 role 简述、AGENTS.md、Skills 和 MCP server 摘要"));
  const grid = node("div", "store-card-grid agent-store-grid");
  for (const agent of agents) {
    const card = node("article", "store-card card");
    const head = node("div", "store-card-head");
    const identity = node("div", "store-card-identity");
    identity.append(node("h4", "", agent.id), node("p", "", agent.description || "未记录简述"));
    const actions = node("div", "store-card-actions");
    const edit = node("button", "agent-edit-open", "编辑");
    edit.type = "button";
    edit.title = "编辑 AGENTS.md";
    if ((agent.instructions || "AGENTS.md") !== "AGENTS.md") {
      edit.disabled = true;
      edit.title = "当前仅支持编辑 AGENTS.md";
    }
    edit.addEventListener("click", () => openAgentEditor(agent));
    actions.append(edit, node("span", "store-count", `${agent.skill_count || 0} skills · ${agent.mcp_count || 0} mcp`));
    head.append(identity, actions);
    card.append(head);
    const body = node("div", "agent-store-card-body");
    const instructionsPane = node("div", "agent-store-instructions-pane");
    instructionsPane.append(node("h5", "", "AGENTS.md"), renderAgentInstructions(agent));
    const factsPane = node("div", "agent-store-facts-pane");
    const facts = node("div", "facts store-facts");
    facts.append(
      fact("Instructions", agent.instructions || "AGENTS.md"),
      fact("Instructions Path", agent.instructions_path || "—"),
      fact("Path", agent.path || "—"),
    );
    factsPane.append(facts);
    body.append(instructionsPane, factsPane);
    card.append(body);
    card.append(node("h5", "", "Skills"), renderAgentSkillChips(agent));
    card.append(node("h5", "", "MCP Servers"));
    const mcpList = node("div", "store-mcp-list");
    if (!(agent.mcp_servers || []).length) {
      mcpList.append(node("span", "graph-muted", "未配置 MCP server"));
    }
    for (const server of agent.mcp_servers || []) {
      const row = node("div", "store-mcp-row");
      row.append(node("strong", "", server.name), node("span", "", server.command || server.type || "configured"));
      mcpList.append(row);
    }
    card.append(mcpList);
    grid.append(card);
  }
  section.append(grid);
  detail.append(section);
}

function formatFlowExecution(flow) {
  const execution = flow.execution || {};
  return [
    `concurrency ${execution.max_concurrency ?? "—"}`,
    `attempts ${execution.max_attempts ?? "—"}`,
    `${execution.stage_timeout_seconds ?? "—"}s timeout`,
    execution.no_supervisor ? "direct" : "supervisor",
  ].join(" · ");
}

function renderFlowsStoreDetail() {
  const store = state.flowsStore || { flows: [], exists: false };
  const detail = document.querySelector("#detail");
  detail.replaceChildren();
  const flows = Array.isArray(store.flows) ? store.flows : [];
  const stageTotal = flows.reduce((total, flow) => total + (flow.stage_count || 0), 0);
  const agentTotal = flows.reduce((total, flow) => total + (flow.agent_count || 0), 0);
  detail.append(renderStoreHeader("flows_store", store, `${flows.length} templates`));
  const stats = node("div", "stats store-stats");
  stats.append(
    stat("Flow Templates", flows.length),
    stat("Stages", stageTotal),
    stat("Agent Bindings", agentTotal),
    stat("Store", store.exists ? "available" : "missing"),
  );
  detail.append(stats);
  if (!store.exists) {
    detail.append(node("div", "project-notice", "当前 Flow Store 目录不存在。"));
    return;
  }
  const section = node("section", "section");
  section.append(sectionTitle("Flow Store", "只展示可复用模板，不展示已运行 instance"));
  const grid = node("div", "store-card-grid flow-store-grid");
  for (const flow of flows) {
    const card = node("article", "store-card card");
    const head = node("div", "store-card-head");
    const identity = node("div", "store-card-identity");
    identity.append(node("h4", "", flow.id), node("p", "", flow.description || "未记录简述"));
    head.append(identity, node("span", "store-count", `${flow.stage_count || 0} stages · ${flow.agent_count || 0} agents`));
    card.append(head);
    const facts = node("div", "facts store-facts");
    facts.append(
      fact("Provider", flow.provider || "—"),
      fact("Execution", formatFlowExecution(flow)),
      fact("Version", flow.version || "—"),
      fact("Path", flow.path || "—"),
    );
    card.append(facts);
    card.append(node("h5", "", "Agents"), renderStoreChips(flow.agents || [], "未绑定 Agent"));
    card.append(node("h5", "", "Stages"), renderStoreChips(flow.stages || [], "未定义 Stage"));
    grid.append(card);
  }
  section.append(grid);
  detail.append(section);
}

async function selectAgentsStore(force = false) {
  const detail = document.querySelector("#detail");
  state.selectedProject = null;
  state.selectedRun = null;
  state.selectedCampaign = null;
  state.selectedPath = null;
  state.view = "agents_store";
  renderRunList();
  if (!state.agentsStore || force) {
    detail.replaceChildren(node("div", "empty-state", "正在读取 Agent Store…"));
    try {
      state.agentsStore = await fetchJSON("/api/stores/agents");
    } catch (error) {
      detail.replaceChildren(node("div", "error", `读取 Agent Store 失败：${error.message}`));
      return;
    }
  }
  renderAgentsStoreDetail();
}

async function selectFlowsStore(force = false) {
  const detail = document.querySelector("#detail");
  state.selectedProject = null;
  state.selectedRun = null;
  state.selectedCampaign = null;
  state.selectedPath = null;
  state.activeSkillInfo = null;
  state.view = "flows_store";
  renderRunList();
  if (!state.flowsStore || force) {
    detail.replaceChildren(node("div", "empty-state", "正在读取 Flow Store…"));
    try {
      state.flowsStore = await fetchJSON("/api/stores/flows");
    } catch (error) {
      detail.replaceChildren(node("div", "error", `读取 Flow Store 失败：${error.message}`));
      return;
    }
  }
  renderFlowsStoreDetail();
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

function bindGraphPan(svg, layout, applyGraphView) {
  let drag = null;
  const svgUnits = event => {
    const bounds = svg.getBoundingClientRect();
    return {
      x: ((event.clientX - bounds.left) / bounds.width) * layout.width,
      y: ((event.clientY - bounds.top) / bounds.height) * layout.height,
    };
  };
  svg.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    if (event.target.closest(".graph-node, .graph-edge")) return;
    event.preventDefault();
    drag = {
      pointerId: event.pointerId,
      view: svgUnits(event),
    };
    svg.setPointerCapture(event.pointerId);
    svg.classList.add("panning");
  });
  svg.addEventListener("pointermove", event => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.preventDefault();
    const next = svgUnits(event);
    state.graphView.x += (next.x - drag.view.x) / state.graphView.scale;
    state.graphView.y += (next.y - drag.view.y) / state.graphView.scale;
    drag.view = next;
    applyGraphView();
  });
  const finish = event => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    try {
      svg.releasePointerCapture(event.pointerId);
    } catch (_error) {
      // Pointer capture may already be gone after cancel or browser focus changes.
    }
    drag = null;
    svg.classList.remove("panning");
  };
  svg.addEventListener("pointerup", finish);
  svg.addEventListener("pointercancel", finish);
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
    const runtime = node("div", "graph-runtime");
    runtime.append(node("span", "", text || "未记录 runtime 标识"));
    if (assignment.terminal_id) {
      const enter = node("button", "terminal-open", "打开终端");
      enter.type = "button";
      enter.addEventListener("click", () => openTerminal(assignment, agent));
      runtime.append(enter);
    }
    panel.append(runtime);
  }
  panel.append(node("h5", "", `交付物 ${agent.deliverables.length}`));
  const files = node("div", "graph-files");
  if (!agent.deliverables.length) files.append(node("span", "graph-muted", "该节点没有直接文件交付物。"));
  for (const item of agent.deliverables) {
    const button = node("button", "graph-file", item.path);
    button.type = "button";
    button.addEventListener("click", () => openArtifact(item.path));
    if (isFinalReportArtifact(item)) {
      const row = node("div", "graph-file-row");
      const download = node("button", "graph-file-download", "下载");
      download.type = "button";
      download.title = `下载 ${item.path}`;
      download.setAttribute("aria-label", `下载 ${item.path}`);
      download.addEventListener("click", () => downloadArtifact(item));
      row.append(button, download);
      files.append(row);
    } else {
      files.append(button);
    }
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
  const viewport = svgNode("g", {
    class: "graph-viewport",
    transform: `translate(${state.graphView.x} ${state.graphView.y}) scale(${state.graphView.scale})`,
  });

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
    viewport.append(group);
  }

  for (const item of graph.nodes) {
    const position = layout.positions[item.id];
    const selected = state.selectedGraph?.type === "node" && state.selectedGraph.id === item.id;
    const finalReport = finalReportArtifactForStage(run, item.id);
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
    if (finalReport) {
      const download = svgNode("g", {
        class: "node-download",
        transform: "translate(126 45)",
        tabindex: 0,
        role: "button",
        "aria-label": `下载 ${finalReport.path}`,
      });
      const title = svgNode("title");
      title.textContent = `下载 ${finalReport.path}`;
      const glyph = svgNode("text", { x: 14, y: 13, class: "node-download-label", "text-anchor": "middle" });
      glyph.textContent = "↓";
      download.append(title, svgNode("rect", { width: 28, height: 18, rx: 5 }), glyph);
      download.addEventListener("click", event => {
        event.stopPropagation();
        downloadArtifact(finalReport);
      });
      download.addEventListener("keydown", event => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        event.stopPropagation();
        downloadArtifact(finalReport);
      });
      group.append(download);
    }
    const activate = () => { state.selectedGraph = { type: "node", id: item.id }; renderDetail(); };
    group.addEventListener("click", activate);
    group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") activate(); });
    viewport.append(group);
  }
  svg.append(viewport);
  canvas.append(svg);
  const controls = node("div", "graph-controls graph-mode-controls");
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
  const zoomControls = node("div", "graph-controls graph-zoom-controls");
  const zoomLabel = node("span", "graph-zoom-label", `${Math.round(state.graphView.scale * 100)}%`);
  const applyGraphView = () => {
    viewport.setAttribute(
      "transform",
      `translate(${state.graphView.x} ${state.graphView.y}) scale(${state.graphView.scale})`,
    );
    zoomLabel.textContent = `${Math.round(state.graphView.scale * 100)}%`;
  };
  bindGraphPan(svg, layout, applyGraphView);
  const setZoom = (nextScale, anchorX = layout.width / 2, anchorY = layout.height / 2) => {
    const previous = state.graphView.scale;
    const scale = Math.min(2.5, Math.max(0.4, nextScale));
    if (scale === previous) return;
    state.graphView.x = anchorX - ((anchorX - state.graphView.x) * scale) / previous;
    state.graphView.y = anchorY - ((anchorY - state.graphView.y) * scale) / previous;
    state.graphView.scale = scale;
    applyGraphView();
  };
  for (const [label, title, action] of [
    ["−", "缩小", () => setZoom(state.graphView.scale / 1.2)],
    ["重置", "重置缩放", () => { state.graphView = { scale: 1, x: 0, y: 0 }; applyGraphView(); }],
    ["+", "放大", () => setZoom(state.graphView.scale * 1.2)],
  ]) {
    const button = node("button", "", label);
    button.type = "button";
    button.title = title;
    button.addEventListener("click", action);
    zoomControls.append(button);
  }
  zoomControls.insertBefore(zoomLabel, zoomControls.children[1]);
  canvas.append(zoomControls);
  svg.addEventListener("wheel", event => {
    event.preventDefault();
    const bounds = svg.getBoundingClientRect();
    const anchorX = ((event.clientX - bounds.left) / bounds.width) * layout.width;
    const anchorY = ((event.clientY - bounds.top) / bounds.height) * layout.height;
    setZoom(state.graphView.scale * Math.exp(-event.deltaY * 0.0015), anchorX, anchorY);
  }, { passive: false });
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

const phaseName = phase => ({ recon: "信息收集 / 威胁建模", planning: "审计编排", mining: "审计 / 挖掘" }[phase] || phase || "Flow");

async function openCampaignArtifact(flow, path) {
  await selectRun(flow.run_id);
  state.selectedPath = path;
  state.artifactMode = path.toLowerCase().endsWith(".md") ? "rendered" : "raw";
  renderDetail();
  document.querySelector("#flow-artifacts")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function campaignInspector(campaign) {
  const panel = node("aside", "graph-inspector campaign-inspector");
  const selection = state.selectedGraph;
  if (!selection) {
    panel.append(node("div", "graph-empty", "点击子 Flow 或 handoff 连线查看详情。"));
    return panel;
  }
  if (selection.type === "edge") {
    const edge = campaign.graph.edges.find(item => item.id === selection.id);
    const source = campaign.flows.find(flow => flow.run_id === edge?.source);
    const target = campaign.flows.find(flow => flow.run_id === edge?.target);
    if (!edge || !source || !target) return panel;
    panel.append(node("p", "eyebrow", "Campaign handoff"));
    panel.append(node("h4", "", `${phaseName(source.phase)} → ${phaseName(target.phase)}`));
    panel.append(node("p", "graph-description", "上游 Flow 的持久化产物经 Hutch 校验后，作为下游 Flow 的输入契约。两个 CAO Flow 仍可独立查看和操作。"));
    panel.append(fact("上游", source.workflow), fact("下游", target.workflow));
    return panel;
  }
  const flow = campaign.flows.find(item => item.run_id === selection.id);
  if (!flow) return panel;
  panel.append(node("p", "eyebrow", phaseName(flow.phase)));
  panel.append(node("h4", "", flow.workflow));
  panel.append(
    fact("状态", flow.status),
    fact("Stage", `${flow.agents.filter(agent => agent.stage !== "flow-supervisor").length} / ${flow.stage_count}`),
    fact("CAO Session", flow.cao_session || "未记录"),
  );
  if (flow.summary) panel.append(node("p", "graph-description", flow.summary));
  const open = node("button", "campaign-open-flow", "打开这个子 Flow");
  open.type = "button";
  open.addEventListener("click", () => selectRun(flow.run_id));
  panel.append(open);
  panel.append(node("h5", "", `交付物 ${flow.deliverables.length}`));
  const files = node("div", "graph-files");
  for (const item of flow.deliverables.slice(0, 12)) {
    const button = node("button", "graph-file", item.path);
    button.type = "button";
    button.addEventListener("click", () => openCampaignArtifact(flow, item.path));
    files.append(button);
  }
  if (flow.deliverables.length > 12) files.append(node("span", "graph-muted", `其余 ${flow.deliverables.length - 12} 个产物请进入子 Flow 查看。`));
  panel.append(files);
  return panel;
}

function renderCampaignGraph(campaign) {
  const workspace = node("div", "graph-workspace card campaign-graph-workspace");
  const canvas = node("div", "graph-canvas");
  const graph = campaign.graph || { nodes: [], edges: [] };
  const layout = graphLayout(graph);
  const svg = svgNode("svg", {
    class: "flow-graph campaign-graph",
    viewBox: `0 0 ${layout.width} ${layout.height}`,
    role: "img",
    "aria-label": `${campaign.campaign_id} overall flow graph`,
  });
  const defs = svgNode("defs");
  const marker = svgNode("marker", { id: "campaign-arrow", viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
  marker.append(svgNode("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "arrow-head" }));
  defs.append(marker);
  svg.append(defs);
  const viewport = svgNode("g", {
    class: "graph-viewport",
    transform: `translate(${state.graphView.x} ${state.graphView.y}) scale(${state.graphView.scale})`,
  });
  for (const edge of graph.edges) {
    const source = layout.positions[edge.source];
    const target = layout.positions[edge.target];
    if (!source || !target) continue;
    const x1 = source.x + 83;
    const x2 = target.x - 83;
    const bend = Math.max(35, (x2 - x1) * 0.45);
    const pathValue = `M ${x1} ${source.y} C ${x1 + bend} ${source.y}, ${x2 - bend} ${target.y}, ${x2} ${target.y}`;
    const selected = state.selectedGraph?.type === "edge" && state.selectedGraph.id === edge.id;
    const group = svgNode("g", {
      class: `graph-edge edge-handoff${connectedToSelection(edge) ? " connected" : ""}${selected ? " selected" : ""}`,
      tabindex: 0,
      role: "button",
      "aria-label": `${edge.source} handoff to ${edge.target}`,
    });
    group.append(svgNode("path", { d: pathValue, class: "edge-visible", "marker-end": "url(#campaign-arrow)" }));
    group.append(svgNode("path", { d: pathValue, class: "edge-hit" }));
    const activate = () => { state.selectedGraph = { type: "edge", id: edge.id }; renderCampaignDetail(); };
    group.addEventListener("click", activate);
    group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") activate(); });
    viewport.append(group);
  }
  for (const item of graph.nodes) {
    const position = layout.positions[item.id];
    const selected = state.selectedGraph?.type === "node" && state.selectedGraph.id === item.id;
    const group = svgNode("g", {
      class: `graph-node node-flow phase-${item.phase}${selected ? " selected" : ""}`,
      transform: `translate(${position.x - 83} ${position.y - 36})`,
      tabindex: 0,
      role: "button",
      "aria-label": item.label,
    });
    group.append(svgNode("rect", { width: 166, height: 72, rx: 11 }));
    const label = svgNode("text", { x: 13, y: 27, class: "node-label" });
    label.textContent = shortLabel(phaseName(item.phase));
    const stage = svgNode("text", { x: 13, y: 48, class: "node-stage" });
    stage.textContent = shortLabel(item.label);
    group.append(label, stage, svgNode("circle", { cx: 151, cy: 17, r: 5, class: "node-status" }));
    const activate = () => { state.selectedGraph = { type: "node", id: item.id }; renderCampaignDetail(); };
    group.addEventListener("click", activate);
    group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") activate(); });
    viewport.append(group);
  }
  svg.append(viewport);
  canvas.append(svg);
  const zoomControls = node("div", "graph-controls graph-zoom-controls");
  const zoomLabel = node("span", "graph-zoom-label", `${Math.round(state.graphView.scale * 100)}%`);
  const applyGraphView = () => {
    viewport.setAttribute("transform", `translate(${state.graphView.x} ${state.graphView.y}) scale(${state.graphView.scale})`);
    zoomLabel.textContent = `${Math.round(state.graphView.scale * 100)}%`;
  };
  bindGraphPan(svg, layout, applyGraphView);
  const setZoom = (nextScale, anchorX = layout.width / 2, anchorY = layout.height / 2) => {
    const previous = state.graphView.scale;
    const scale = Math.min(2.5, Math.max(0.4, nextScale));
    if (scale === previous) return;
    state.graphView.x = anchorX - ((anchorX - state.graphView.x) * scale) / previous;
    state.graphView.y = anchorY - ((anchorY - state.graphView.y) * scale) / previous;
    state.graphView.scale = scale;
    applyGraphView();
  };
  for (const [label, title, action] of [
    ["−", "缩小", () => setZoom(state.graphView.scale / 1.2)],
    ["重置", "重置缩放", () => { state.graphView = { scale: 1, x: 0, y: 0 }; applyGraphView(); }],
    ["+", "放大", () => setZoom(state.graphView.scale * 1.2)],
  ]) {
    const button = node("button", "", label);
    button.type = "button";
    button.title = title;
    button.addEventListener("click", action);
    zoomControls.append(button);
  }
  zoomControls.insertBefore(zoomLabel, zoomControls.children[1]);
  canvas.append(zoomControls);
  svg.addEventListener("wheel", event => {
    event.preventDefault();
    const bounds = svg.getBoundingClientRect();
    setZoom(
      state.graphView.scale * Math.exp(-event.deltaY * 0.0015),
      ((event.clientX - bounds.left) / bounds.width) * layout.width,
      ((event.clientY - bounds.top) / bounds.height) * layout.height,
    );
  }, { passive: false });
  const legend = node("div", "graph-legend");
  const legendItem = node("span");
  legendItem.append(node("i", "legend-handoff"), document.createTextNode("validated handoff"));
  legend.append(legendItem);
  canvas.append(legend);
  workspace.append(canvas, campaignInspector(campaign));
  return workspace;
}

function fact(label, value) {
  const box = node("div", "fact");
  box.append(node("label", "", label), node("span", "", value || "—"));
  return box;
}

function assignmentBlock(item, index, agent) {
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
  if (item.terminal_id) {
    const actions = node("div", "assignment-actions");
    const enter = node("button", "terminal-open", "打开终端");
    enter.type = "button";
    enter.addEventListener("click", () => openTerminal(item, agent));
    actions.append(enter);
    box.append(actions);
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
  for (const [index, assignment] of agent.assignments.entries()) card.append(assignmentBlock(assignment, index, agent));
  if (agent.deliverables.length) {
    const line = node("div", "assignment-line");
    line.append(node("label", "", "交付物"), node("span", "", agent.deliverables.map(item => item.path).join(" · ")));
    card.append(line);
  }
  return card;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\[[^\]\n]+\]\([^\s)]+(?:\s+"[^"]*")?\))/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > offset) parent.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      parent.append(node("code", "markdown-inline-code", token.slice(1, -1)));
    } else if (token.startsWith("**") || token.startsWith("__")) {
      const strong = node("strong");
      appendInlineMarkdown(strong, token.slice(2, -2));
      parent.append(strong);
    } else if (token.startsWith("~~")) {
      const strike = node("s");
      appendInlineMarkdown(strike, token.slice(2, -2));
      parent.append(strike);
    } else {
      const parsed = token.match(/^\[([^\]]+)\]\(([^\s)]+)(?:\s+"([^"]*)")?\)$/);
      const label = parsed?.[1] || token;
      const href = parsed?.[2] || "";
      if (/^(https?:|mailto:|#)/i.test(href)) {
        const link = node("a", "markdown-link", label);
        link.href = href;
        if (/^https?:/i.test(href)) {
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        }
        if (parsed?.[3]) link.title = parsed[3];
        parent.append(link);
      } else {
        parent.append(document.createTextNode(label));
      }
    }
    offset = match.index + token.length;
  }
  if (offset < text.length) parent.append(document.createTextNode(text.slice(offset)));
}

const markdownCells = line => {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map(value => value.trim());
};

const markdownTableDivider = line => {
  const cells = markdownCells(line);
  return cells.length > 0 && cells.every(value => /^:?-{3,}:?$/.test(value));
};

const markdownBlockStart = (lines, index) => {
  const line = lines[index] || "";
  return !line.trim()
    || /^\s*```/.test(line)
    || /^\s{0,3}#{1,6}\s+/.test(line)
    || /^\s{0,3}(?:[-*_]\s*){3,}$/.test(line)
    || /^\s*>\s?/.test(line)
    || /^\s*[-*+]\s+/.test(line)
    || /^\s*\d+[.)]\s+/.test(line)
    || (index + 1 < lines.length && line.includes("|") && markdownTableDivider(lines[index + 1]));
};

function renderMarkdown(markdown) {
  const root = node("div", "markdown-body");
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = line.match(/^\s*```\s*([A-Za-z0-9_+.-]*)\s*$/);
    if (fence) {
      index += 1;
      const codeLines = [];
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = node("pre", "markdown-code-block");
      const code = node("code", fence[1] ? `language-${fence[1]}` : "", codeLines.join("\n"));
      pre.append(code);
      root.append(pre);
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const value = document.createElement(`h${heading[1].length}`);
      appendInlineMarkdown(value, heading[2]);
      root.append(value);
      index += 1;
      continue;
    }
    if (/^\s{0,3}(?:[-*_]\s*){3,}$/.test(line)) {
      root.append(document.createElement("hr"));
      index += 1;
      continue;
    }
    if (index + 1 < lines.length && line.includes("|") && markdownTableDivider(lines[index + 1])) {
      const table = node("table", "markdown-table");
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      for (const cell of markdownCells(line)) {
        const th = document.createElement("th");
        appendInlineMarkdown(th, cell);
        headRow.append(th);
      }
      head.append(headRow);
      table.append(head);
      index += 2;
      const body = document.createElement("tbody");
      while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
        const row = document.createElement("tr");
        for (const cell of markdownCells(lines[index])) {
          const td = document.createElement("td");
          appendInlineMarkdown(td, cell);
          row.append(td);
        }
        body.append(row);
        index += 1;
      }
      table.append(body);
      root.append(table);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const quote = document.createElement("blockquote");
      const values = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        values.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      appendInlineMarkdown(quote, values.join(" "));
      root.append(quote);
      continue;
    }
    const unordered = /^\s*[-*+]\s+/.test(line);
    const ordered = /^\s*\d+[.)]\s+/.test(line);
    if (unordered || ordered) {
      const list = document.createElement(ordered ? "ol" : "ul");
      const matcher = ordered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/;
      while (index < lines.length) {
        const itemMatch = lines[index].match(matcher);
        if (!itemMatch) break;
        const item = document.createElement("li");
        const task = itemMatch[1].match(/^\[([ xX])\]\s+(.+)$/);
        if (task) {
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.checked = task[1].toLowerCase() === "x";
          checkbox.disabled = true;
          item.append(checkbox);
          appendInlineMarkdown(item, task[2]);
        } else {
          appendInlineMarkdown(item, itemMatch[1]);
        }
        list.append(item);
        index += 1;
      }
      root.append(list);
      continue;
    }
    const paragraphLines = [line.trim()];
    index += 1;
    while (index < lines.length && !markdownBlockStart(lines, index)) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    const paragraph = document.createElement("p");
    appendInlineMarkdown(paragraph, paragraphLines.join(" "));
    root.append(paragraph);
  }
  return root;
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
    button.addEventListener("click", () => {
      state.selectedPath = item.path;
      state.artifactMode = item.path.toLowerCase().endsWith(".md") ? "rendered" : "raw";
      renderDetail();
    });
    list.append(button);
  }
  if (selected) {
    const header = node("header");
    header.append(node("span", "", selected.path));
    const actions = node("div", "artifact-view-actions");
    actions.append(node("span", "", `${selected.kind} · ${formatBytes(selected.bytes)}`));
    const markdown = selected.path.toLowerCase().endsWith(".md");
    if (markdown) {
      const rendered = node("button", `artifact-mode${state.artifactMode === "rendered" ? " active" : ""}`, "渲染");
      const raw = node("button", `artifact-mode${state.artifactMode === "raw" ? " active" : ""}`, "原文");
      rendered.type = "button";
      raw.type = "button";
      rendered.addEventListener("click", () => { state.artifactMode = "rendered"; renderDetail(); });
      raw.addEventListener("click", () => { state.artifactMode = "raw"; renderDetail(); });
      actions.append(rendered, raw);
    }
    header.append(actions);
    viewer.append(header);
    if (markdown && state.artifactMode === "rendered") {
      viewer.append(renderMarkdown(selected.content));
    } else {
      viewer.append(node("pre", "artifact-source", selected.content));
    }
  } else {
    viewer.append(node("div", "error", "该 Flow 没有可读取的文本产物。"));
  }
  grid.append(list, viewer);
  return grid;
}

async function openReport(report) {
  await selectRun(report.run_id);
  state.selectedPath = report.path;
  state.artifactMode = "rendered";
  renderDetail();
  document.querySelector("#flow-artifacts")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderService(service, projectId) {
  const card = node("article", "service-card card");
  const header = node(
    state.projectOnly ? "div" : "button",
    `service-header${state.projectOnly ? "" : " tree-collapse-trigger"}`,
  );
  if (!state.projectOnly) header.type = "button";
  const indicator = node(
    "span",
    state.projectOnly ? "tree-leaf-marker" : "tree-chevron",
    state.projectOnly ? "•" : "▾",
  );
  const title = node("div");
  const name = node("h4");
  name.append(indicator, node("span", "", service.name));
  title.append(name, node("p", "service-path", service.relative_path || service.repo_path));
  header.append(
    title,
    node(
      "span",
      "service-flow-count",
      state.projectOnly ? "微服务仓库" : `${service.flow_count} Flow · ${service.report_count} 报告`,
    ),
  );
  card.append(header);
  const content = node("div", "service-content");

  if (!state.projectOnly) {
    const campaigns = state.campaigns.filter(campaign => campaign.service?.id === service.id);
    if (campaigns.length) {
      const campaignList = node("div", "service-campaign-list");
      campaignList.append(node("h5", "", "整合总 Flow"));
      for (const campaign of campaigns) {
        const button = node("button", "service-campaign-open");
        button.type = "button";
        const identity = node("span", "service-flow-identity");
        identity.append(node("strong", "", campaign.campaign_id), node("small", "", `${campaign.phases.map(phaseName).join(" → ")} · ${campaign.flow_count} 个 CAO Flow`));
        button.append(
          node("span", "campaign-badge", "总 FLOW"),
          identity,
          node("span", `status status-${campaign.status}`, campaign.status),
          node("span", "service-flow-progress", `${campaign.stages_done}/${campaign.stages_total} stages`),
        );
        button.addEventListener("click", () => selectCampaign(campaign.instance_id));
        campaignList.append(button);
      }
      content.append(campaignList);
    }

    if (service.flows?.length) {
      const flowList = node("div", "service-flow-list");
      for (const run of service.flows) {
        const row = node("div", "service-flow-row");
        const button = node("button", "service-flow-open");
        button.type = "button";
        const identity = node("span", "service-flow-identity");
        identity.append(node("strong", "", run.workflow), node("small", "", run.run_id));
        button.append(
          identity,
          node("span", `status status-${run.status}`, run.status),
          node("span", "service-flow-progress", `${run.stages_done}/${run.stages_total} stages`),
          node("time", "", formatTime(run.finished_at || run.created_at)),
        );
        button.addEventListener("click", () => selectRun(run.run_id));
        const remove = node("button", "flow-delete", "删除");
        remove.type = "button";
        remove.disabled = !deletableRun(run);
        remove.title = remove.disabled ? "运行中的 Flow 不能删除" : "删除该 Flow 记录";
        remove.addEventListener("click", () => deleteRun(run));
        row.append(button, remove);
        flowList.append(row);
      }
      content.append(flowList);
    } else {
      content.append(node("p", "service-empty", "该微服务还没有 Hutch 测试 Flow。"));
    }

    if (service.reports?.length) {
      const reports = node("div", "service-reports");
      reports.append(node("h5", "", "产出报告"));
      for (const report of service.reports) {
        const button = node("button", "report-row");
        button.type = "button";
        button.append(
          node("span", "report-name", report.path),
          node("span", "report-meta", `${report.workflow} · ${formatBytes(report.bytes)} · ${formatTime(report.finished_at)}`),
        );
        button.addEventListener("click", () => openReport(report));
        reports.append(button);
      }
      content.append(reports);
    }
  }
  if (!state.projectOnly) {
    bindCollapsible(header, content, treeNodeKey(projectId, service.id));
    card.append(content);
  }
  return card;
}

function renderProjectTree(items, container, projectId, depth = 0) {
  for (const item of items) {
    if (item.type === "service") {
      const leaf = node("div", "project-tree-leaf");
      leaf.style.setProperty("--tree-depth", depth);
      leaf.append(renderService(item, projectId));
      container.append(leaf);
      continue;
    }
    const branch = node("section", "project-tree-branch");
    branch.style.setProperty("--tree-depth", depth);
    const header = node("button", "project-tree-branch-header tree-collapse-trigger");
    header.type = "button";
    const indicator = node("span", "tree-chevron", "▾");
    const identity = node("div");
    const name = node("h3");
    name.append(indicator, node("span", "", item.name));
    identity.append(name, node("code", "", item.relative_path));
    header.append(
      identity,
      node(
        "span",
        "tree-rollup",
        state.projectOnly
          ? `${item.service_count} 服务`
          : `${item.service_count} 服务 · ${item.flow_count} Flow · ${item.report_count} 报告`,
      ),
    );
    branch.append(header);
    const children = node("div", "project-tree-children");
    renderProjectTree(item.children || [], children, projectId, depth + 1);
    bindCollapsible(header, children, treeNodeKey(projectId, item.id));
    branch.append(children);
    container.append(branch);
  }
}

function renderProjectDetail(project) {
  const detail = document.querySelector("#detail");
  detail.replaceChildren();
  const header = node("header", "detail-header project-detail-header");
  const title = node("div");
  title.append(
    node("p", "eyebrow", "Application Project"),
    node("h2", "", project.name),
    node("p", "detail-subtitle", project.root_path || project.repo_path),
  );
  header.append(title, node("span", `project-availability ${project.available === false ? "missing" : ""}`, project.available === false ? "目录不可用" : "项目总览"));
  detail.append(header);

  const stats = node("div", "stats project-stats");
  stats.append(
    stat("目录节点", project.directory_count),
    stat("微服务仓库", project.service_count),
  );
  if (!state.projectOnly) {
    stats.append(stat("测试 Flow", project.flow_count), stat("产出报告", project.report_count));
  }
  detail.append(stats);

  if (!project.configured) {
    detail.append(node("div", "project-notice", "该项目来自历史单仓库 Flow。配置 ~/.hutch/projects/projects.json 后可归入应用、域和微服务层级。"));
  }
  if (!project.tree?.children?.length) {
    detail.append(node("div", "empty-state", "项目目录树中尚未发现 Git 微服务仓库。"));
    return;
  }
  const section = node("section", "section project-tree-section");
  section.append(sectionTitle(
    "项目目录树",
    state.projectOnly
      ? "仅项目模式；点击任意目录节点收起或展开"
      : "自适应目录层级；Git 仓库是微服务叶子节点",
  ));
  const tree = node("div", "project-tree");
  renderProjectTree(project.tree.children, tree, project.id);
  section.append(tree);
  detail.append(section);
}

function renderCampaignDetail() {
  const campaign = state.selectedCampaign;
  const detail = document.querySelector("#detail");
  detail.replaceChildren();

  const header = node("header", "detail-header campaign-detail-header");
  const title = node("div");
  title.append(
    node("p", "eyebrow", `${campaign.project?.name || "Unknown project"} / ${campaign.service?.name || "service"} / Campaign`),
    node("h2", "", campaign.campaign_id),
    node("p", "detail-subtitle", `${campaign.instance_id} · ${campaign.target || "未记录目录"}`),
  );
  header.append(title, node("span", `status status-${campaign.status}`, campaign.status));
  detail.append(header);

  const stats = node("div", "stats");
  stats.append(
    stat("CAO 子 Flow", campaign.flow_count),
    stat("整体 Stage", `${campaign.stages_done}/${campaign.stages_total}`),
    stat("Agent 节点", campaign.agent_count),
    stat("总运行时长", formatDuration(campaign.duration_seconds)),
  );
  detail.append(stats);
  if (campaign.summary) detail.append(node("div", "summary card", campaign.summary));

  const graphSection = node("section", "section");
  graphSection.append(
    sectionTitle("整体流程", "CAO 子 Flow 级视图；点击节点进入子 Flow，点击边查看 handoff"),
    renderCampaignGraph(campaign),
  );
  detail.append(graphSection);

  const flowsSection = node("section", "section");
  flowsSection.append(sectionTitle("CAO 子 Flow", "各子 Flow 保持独立，可继续查看 Agent、session 和产物"));
  const flowList = node("div", "campaign-flow-list");
  for (const flow of campaign.flows) {
    const card = node("article", "campaign-flow-card card");
    const identity = node("div", "campaign-flow-identity");
    identity.append(
      node("span", "campaign-phase", phaseName(flow.phase)),
      node("h4", "", flow.workflow),
      node("code", "", flow.run_id),
    );
    const open = node("button", "campaign-open-flow", "查看子 Flow");
    open.type = "button";
    open.addEventListener("click", () => selectRun(flow.run_id));
    card.append(identity, node("span", `status status-${flow.status}`, flow.status), node("span", "campaign-flow-progress", `${flow.stage_count} stages · ${flow.deliverables.length} 产物`), open);
    flowList.append(card);
  }
  flowsSection.append(flowList);
  detail.append(flowsSection);

  const artifactsSection = node("section", "section");
  artifactsSection.append(sectionTitle("全流程产物", `${campaign.deliverables.length} 个持久化交付物，按子 Flow 分组`));
  const groups = node("div", "campaign-artifact-groups");
  for (const flow of campaign.flows) {
    const group = node("article", "campaign-artifact-group card");
    group.append(node("h4", "", `${phaseName(flow.phase)} · ${flow.workflow}`));
    const files = node("div", "campaign-artifact-list");
    for (const artifact of flow.deliverables) {
      const button = node("button", "campaign-artifact-open");
      button.type = "button";
      button.append(node("strong", "", artifact.path), node("small", "", `${artifact.kind} · ${formatBytes(artifact.bytes)}`));
      button.addEventListener("click", () => openCampaignArtifact(flow, artifact.path));
      files.append(button);
    }
    if (!flow.deliverables.length) files.append(node("span", "service-empty", "该子 Flow 没有文本产物。"));
    group.append(files);
    groups.append(group);
  }
  artifactsSection.append(groups);
  detail.append(artifactsSection);
}

function renderDetail() {
  const run = state.selectedRun;
  const detail = document.querySelector("#detail");
  detail.replaceChildren();

  const header = node("header", "detail-header");
  const title = node("div");
  title.append(
    node("p", "eyebrow", `${run.project?.name || "Unknown project"} / ${(run.service?.tree_path || []).join(" / ") || "root"} / ${run.service?.name || "service"}`),
    node("h2", "", run.workflow),
    node("p", "detail-subtitle", `${run.run_id} · ${run.service?.repo_path || run.target || "未记录目录"}`),
  );
  const actions = node("div", "detail-actions");
  actions.append(node("span", `status status-${run.status}`, run.status));
  const remove = node("button", "flow-delete", "删除记录");
  remove.type = "button";
  remove.disabled = !deletableRun(run);
  remove.title = remove.disabled ? "运行中的 Flow 不能删除" : "删除该 Flow 记录";
  remove.addEventListener("click", () => deleteRun(run));
  actions.append(remove);
  header.append(title, actions);
  detail.append(header);

  const stats = node("div", "stats");
  stats.append(
    stat("完成时间", formatTime(run.finished_at)),
    stat("运行时长", formatDuration(run.duration_seconds)),
    stat("Agent / Stage", `${run.agents.length} / ${run.stage_count}`),
    stat("源码版本", run.target_head ? run.target_head.slice(0, 12) : "—"),
  );
  detail.append(stats);

  if (run.campaign?.id) {
    const lineage = [
      `Campaign: ${run.campaign.id}`,
      `阶段: ${run.campaign.phase || "unknown"}`,
      run.campaign.intelligence_run_id ? `情报 Run: ${run.campaign.intelligence_run_id}` : null,
      run.campaign.planning_run_id ? `计划 Run: ${run.campaign.planning_run_id}` : null,
      run.campaign.parent_run_id ? `父 Run: ${run.campaign.parent_run_id}` : null,
    ].filter(Boolean).join(" · ");
    detail.append(node("div", "project-notice", lineage));
  }

  if (run.status === "orphaned") {
    detail.append(node("div", "orphaned-notice", `Hutch 原始状态为 ${run.raw_status}，但 CAO 中已不存在 session ${run.cao_session || "（未记录）"}。该记录可安全删除。`));
  }

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

async function refreshTerminal() {
  if (!state.terminalId) return;
  const terminalId = state.terminalId;
  const screen = document.querySelector("#terminal-screen");
  const readonly = document.querySelector("#terminal-readonly");
  const form = document.querySelector("#terminal-form");
  const keys = document.querySelector("#terminal-keys");
  try {
    const value = await fetchJSON(`/api/terminals/${encodeURIComponent(terminalId)}`);
    if (terminalId !== state.terminalId) return;
    const closeToBottom = screen.scrollHeight - screen.scrollTop - screen.clientHeight < 80;
    screen.textContent = stripAnsi(value.output) || "终端暂无输出。";
    if (closeToBottom) screen.scrollTop = screen.scrollHeight;
    document.querySelector("#terminal-meta").textContent = [
      value.session,
      value.window,
      value.status,
      value.working_directory,
    ].filter(Boolean).join(" · ");
    readonly.hidden = value.live;
    form.hidden = !value.live;
    keys.hidden = !value.live;
    if (!value.live && state.terminalTimer) {
      clearInterval(state.terminalTimer);
      state.terminalTimer = null;
    }
  } catch (error) {
    screen.textContent = `读取 terminal 失败：${error.message}`;
  }
}

async function openTerminal(assignment, agent) {
  const terminalId = assignment.terminal_id;
  const title = agent?.profile || assignment.window || "Agent Terminal";
  const baseMeta = [assignment.session, assignment.window, terminalId].filter(Boolean).join(" · ");
  if (!terminalId) return;
  if (state.terminalTimer) clearInterval(state.terminalTimer);
  state.terminalTimer = null;
  state.terminalId = null;
  try {
    const value = await fetchJSON(`/api/terminals/${encodeURIComponent(terminalId)}`);
    if (value.live) {
      await openXtermTerminal({
        title,
        meta: [
          value.session || assignment.session,
          value.window || assignment.window,
          value.status,
          value.working_directory,
          terminalId,
        ].filter(Boolean).join(" · "),
        websocketPath: `/api/terminals/${encodeURIComponent(terminalId)}/ws`,
      });
      return;
    }
  } catch (_error) {
    // Fall through to the durable scrollback dialog, which reports the fetch error.
  }

  const dialog = document.querySelector("#terminal-dialog");
  state.terminalId = terminalId;
  document.querySelector("#terminal-title").textContent = title;
  document.querySelector("#terminal-meta").textContent = baseMeta;
  document.querySelector("#terminal-screen").textContent = "正在连接 tmux pane…";
  document.querySelector("#terminal-input").value = "";
  if (!dialog.open) dialog.showModal();
  await refreshTerminal();
  if (!document.querySelector("#terminal-form").hidden) {
    state.terminalTimer = setInterval(refreshTerminal, 1000);
  }
}

async function openLauncher() {
  const dialog = document.querySelector("#launcher-dialog");
  if (!dialog.open) dialog.showModal();
  const examples = document.querySelector("#launcher-examples");
  examples.replaceChildren(node("span", "graph-muted", "正在读取 CAO flow 与 agent profile…"));
  try {
    state.caoCatalog = await fetchJSON("/api/cao/catalog");
    const flows = Array.isArray(state.caoCatalog.flows) ? state.caoCatalog.flows : [];
    const profiles = Array.isArray(state.caoCatalog.profiles) ? state.caoCatalog.profiles : [];
    const commands = [
      ...flows.slice(0, 6).map(item => `cao flow run ${item.name || item}`),
      ...profiles.slice(0, 4).map(item => `cao launch ${item.name || item.id || item} --provider opencode_cli`),
    ];
    examples.replaceChildren();
    for (const command of commands) {
      const button = node("button", "launcher-example", command);
      button.type = "button";
      button.addEventListener("click", () => {
        document.querySelector("#launcher-input").value = command;
        document.querySelector("#launcher-input").focus();
      });
      examples.append(button);
    }
    if (!commands.length) examples.append(node("span", "graph-muted", "CAO 当前没有可用 flow/profile。"));
  } catch (error) {
    examples.replaceChildren(node("span", "error", `读取 CAO catalog 失败：${error.message}`));
  }
  document.querySelector("#launcher-input").focus();
}

document.querySelector("#terminal-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.querySelector("#terminal-input");
  const message = input.value;
  if (!message || !state.terminalId) return;
  input.disabled = true;
  try {
    await postJSON(`/api/terminals/${encodeURIComponent(state.terminalId)}/input`, { message });
    input.value = "";
    setTimeout(refreshTerminal, 300);
  } catch (error) {
    document.querySelector("#terminal-screen").textContent += `\n\n[Hutch] 发送失败：${error.message}`;
  } finally {
    input.disabled = false;
    input.focus();
  }
});

document.querySelector("#terminal-input").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.querySelector("#terminal-form").requestSubmit();
  }
});

document.querySelector("#terminal-keys").addEventListener("click", async event => {
  const key = event.target.closest("button")?.dataset.key;
  if (!key || !state.terminalId) return;
  try {
    await postJSON(`/api/terminals/${encodeURIComponent(state.terminalId)}/key`, { key });
    setTimeout(refreshTerminal, 200);
  } catch (error) {
    document.querySelector("#terminal-screen").textContent += `\n\n[Hutch] 按键失败：${error.message}`;
  }
});

document.querySelector("#launcher-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.querySelector("#launcher-input");
  const output = document.querySelector("#launcher-output");
  const command = input.value.trim();
  if (!command) return;
  output.textContent = `$ ${command}\n执行中…`;
  input.disabled = true;
  try {
    const result = await postJSON("/api/cao/execute", { command });
    output.textContent = `$ ${command}\n${JSON.stringify(result, null, 2)}`;
    setTimeout(loadRuns, 1200);
  } catch (error) {
    output.textContent = `$ ${command}\nERROR: ${error.message}`;
  } finally {
    input.disabled = false;
    input.focus();
  }
});

document.querySelector("#delete-cancel").addEventListener("click", () => {
  document.querySelector("#delete-dialog").close();
});

document.querySelector("#delete-confirm").addEventListener("click", async () => {
  const run = state.pendingDeleteRun;
  if (!run) return;
  const confirm = document.querySelector("#delete-confirm");
  const cancel = document.querySelector("#delete-cancel");
  const errorBox = document.querySelector("#delete-error");
  confirm.disabled = true;
  cancel.disabled = true;
  confirm.textContent = "删除中…";
  errorBox.hidden = true;
  try {
    await deleteJSON(`/api/runs/${encodeURIComponent(run.run_id)}`);
    if (state.selectedRun?.run_id === run.run_id) state.selectedRun = null;
    state.pendingDeleteRun = null;
    document.querySelector("#delete-dialog").close();
    await loadRuns();
  } catch (error) {
    errorBox.textContent = `删除失败：${error.message}`;
    errorBox.hidden = false;
    confirm.disabled = false;
    cancel.disabled = false;
    confirm.textContent = "重试删除";
  }
});

document.querySelector("#delete-dialog").addEventListener("close", () => {
  state.pendingDeleteRun = null;
  document.querySelector("#delete-cancel").disabled = false;
});

function closeSkillPopover() {
  if (!state.activeSkillInfo) return;
  state.activeSkillInfo = null;
  if (state.view === "agents_store") renderAgentsStoreDetail();
}

document.querySelector("#cao-launcher").addEventListener("click", openLauncher);
document.querySelector("#agents-store").addEventListener("click", () => selectAgentsStore());
document.querySelector("#flows-store").addEventListener("click", () => selectFlowsStore());
document.querySelector("#sidebar-toggle").addEventListener("click", () => {
  state.sidebarCollapsed = !state.sidebarCollapsed;
  persistViewPreferences();
  updateSidebarCollapseControl();
});
document.querySelector("#qu-agent-toggle").addEventListener("click", () => {
  state.quAgentCollapsed = !state.quAgentCollapsed;
  persistViewPreferences();
  updateQuAgentPanel();
});
document.querySelector("#qu-agent-start").addEventListener("click", startQuAgent);
document.querySelector("#qu-agent-stop").addEventListener("click", stopQuAgent);
document.querySelector("#qu-agent-open").addEventListener("click", openQuAgentTerminal);
document.querySelector("#agent-edit-form").addEventListener("submit", saveAgentInstructions);
document.querySelector("#agent-edit-dialog").addEventListener("close", closeAgentEditor);
function refreshSidebarFilters() {
  persistViewPreferences();
  updateProjectOnlyControl();
  updateFlowOnlyControl();
  updateRunCount();
  const visible = sidebarProjects();
  if (state.view === "projects") {
    if (state.selectedProject && visible.some(project => project.id === state.selectedProject.id)) {
      selectProject(state.selectedProject.id);
    } else if (visible.length) {
      selectProject(visible[0].id);
    } else {
      state.selectedProject = null;
      renderRunList();
      document.querySelector("#detail").replaceChildren(node("div", "empty-state", state.flowOnly ? "没有有 Flow 的微服务。" : "没有项目。"));
    }
    return;
  }
  if (state.projectOnly && state.selectedProject) {
    selectProject(state.selectedProject.id);
    return;
  }
  renderRunList();
}
document.querySelector("#flow-only").addEventListener("click", () => {
  state.flowOnly = !state.flowOnly;
  refreshSidebarFilters();
});
document.querySelector("#project-only").addEventListener("click", () => {
  state.projectOnly = !state.projectOnly;
  refreshSidebarFilters();
});
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => document.querySelector(`#${button.dataset.close}`).close());
}
document.addEventListener("click", closeSkillPopover);
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeSkillPopover();
});
document.querySelector("#terminal-dialog").addEventListener("close", () => {
  if (state.terminalTimer) clearInterval(state.terminalTimer);
  state.terminalTimer = null;
  state.terminalId = null;
});
document.querySelector("#xterm-dialog").addEventListener("close", cleanupXtermTerminal);

document.querySelector("#refresh").addEventListener("click", () => {
  loadRuns();
  refreshQuAgent();
});
updateProjectOnlyControl();
updateFlowOnlyControl();
updateSidebarCollapseControl();
updateQuAgentPanel();
updateStoreNav();
refreshQuAgent();
loadRuns();
