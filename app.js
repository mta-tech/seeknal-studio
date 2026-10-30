"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const MAX_AI_BUFFER_BYTES = 256 * 1024;
const EDITOR_VIEWS = ["graph", "yaml", "logs", "repl", "config"];

function createEditorGroupState() {
  return {
    activeGroup: "left",
    split: false,
    ratio: 50,
    groups: {
      left: { tabs: ["graph"], active: "graph" },
      right: { tabs: [], active: null },
    },
    owners: { graph: "left" },
  };
}

function editorOpen(model, view, groupId = model.activeGroup) {
  if (!EDITOR_VIEWS.includes(view) || !model.groups[groupId]) return false;
  const previousOwner = model.owners[view];
  if (previousOwner && previousOwner !== groupId) {
    const previous = model.groups[previousOwner];
    const index = previous.tabs.indexOf(view);
    previous.tabs = previous.tabs.filter(tab => tab !== view);
    if (previous.active === view) previous.active = previous.tabs[Math.max(0, index - 1)] || previous.tabs[0] || null;
  }
  const group = model.groups[groupId];
  if (!group.tabs.includes(view)) group.tabs.push(view);
  group.active = view;
  model.owners[view] = groupId;
  model.activeGroup = groupId;
  if (groupId === "right") model.split = true;
  return true;
}

function editorClose(model, view, { dirty = false } = {}) {
  if (view === "yaml" && dirty) return false;
  const groupId = model.owners[view];
  if (!groupId) return false;
  const group = model.groups[groupId];
  const index = group.tabs.indexOf(view);
  group.tabs = group.tabs.filter(tab => tab !== view);
  if (group.active === view) group.active = group.tabs[Math.max(0, index - 1)] || group.tabs[0] || null;
  delete model.owners[view];
  return true;
}

function editorSplit(model) {
  model.split = true;
  model.activeGroup = "right";
  return model;
}

function editorMove(model, view, targetGroup) {
  if (!model.owners[view] || !model.groups[targetGroup]) return false;
  if (targetGroup === "right") model.split = true;
  return editorOpen(model, view, targetGroup);
}

function editorMoveActive(model, direction) {
  const sourceId = model.activeGroup;
  const targetId = direction === "left" ? "left" : "right";
  const view = model.groups[sourceId].active;
  if (!view || sourceId === targetId) return false;
  return editorMove(model, view, targetId);
}

function editorMerge(model) {
  const right = model.groups.right;
  for (const view of [...right.tabs]) editorOpen(model, view, "left");
  right.tabs = [];
  right.active = null;
  model.split = false;
  model.activeGroup = "left";
  return model;
}

function createEditorFlowState() {
  return { kind: "plain", phase: "saved", approval: null, validationMessage: null, request: 0, busy: false };
}

function editorFlowAction(flow, dirty) {
  if (flow.phase === "applying") return { label: flow.kind === "draft" ? "Applying draft…" : "Saving changes…", action: null };
  if (flow.busy || flow.phase === "validating") return { label: "Dry running…", action: null };
  if (flow.approval && flow.phase === "passed" && flow.approval.buffer === flow.buffer) {
    return { label: flow.kind === "draft" ? "Apply draft" : "Save changes", action: "apply" };
  }
  if (flow.kind === "plain") return { label: "Save", action: dirty ? "save" : null };
  return { label: flow.kind === "draft" ? "Dry run" : "Dry run changes", action: dirty || flow.kind === "draft" ? "dry-run" : null };
}

function editorFlowTransition(flow, event, payload = {}) {
  if (event === "edit") return { ...flow, buffer: payload.buffer, approval: null, validationMessage: null, phase: payload.dirty ? "dirty" : "saved" };
  if (event === "validation-start") return { ...flow, phase: "validating", approval: payload.approval || null, validationMessage: null };
  if (event === "apply-start") return { ...flow, phase: "applying", busy: true };
  if (event === "validation-finish") {
    const passed = payload.success === true && flow.approval?.buffer === payload.buffer;
    return { ...flow, phase: passed ? "passed" : "failed", approval: passed ? flow.approval : null, validationMessage: payload.message || null };
  }
  if (event === "failure") return { ...flow, phase: "failed", approval: null, validationMessage: payload.message || null, busy: false };
  return flow;
}

function nextRequestGeneration(counter) {
  counter.value += 1;
  return counter.value;
}

function matchesValidationContext(validation, flow, selectedFile, buffer) {
  return Boolean(validation && selectedFile === validation.sourcePath
    && flow.approval?.path === validation.preparedPath
    && flow.approval?.contentHash === validation.contentHash
    && flow.approval?.buffer === buffer);
}

function matchesApplyContext(applied, selectedFile, buffer) {
  return Boolean(applied && selectedFile === applied.sourcePath && buffer === applied.bufferAtApply);
}

function rebaseEditorAfterApply(applied, currentBuffer) {
  return {
    selectedFile: applied.destination,
    originalContent: applied.bufferAtApply,
    dirty: currentBuffer !== applied.bufferAtApply,
    flow: { ...createEditorFlowState(), kind: "active", phase: currentBuffer === applied.bufferAtApply ? "saved" : "dirty", buffer: currentBuffer },
  };
}

function rebaseDirtyDraftAfterPrepare(kind, submittedBuffer, currentBuffer) {
  if (kind !== "draft" || submittedBuffer === currentBuffer) return null;
  return { originalContent: submittedBuffer, dirty: true, buffer: currentBuffer };
}

function captureDraftContext(editorGroups, selectedFile) {
  return { groupId: editorGroups.activeGroup, selectedFile };
}

const state = {
  project: null,
  files: [],
  graph: { nodes: [], edges: [] },
  selectedFile: null,
  selectedNode: null,
  originalContent: "",
  dirty: false,
  jobs: [],
  activeJob: null,
  selectedLogJobId: null,
  jobTimer: null,
  graphQuery: "",
  agent: { mode: "owned", configured: false, healthy: false, ready: false, project_verified: false, busy: false, cancelled: false, cancelPending: false, request: 0, sessionId: `studio-${Date.now()}`, activeTurnId: null, eventSeq: -1, seenEvents: new Set(), completedTurns: new Set(), pollTimer: null, readinessGeneration: 0, readinessTimer: null },
  fileRequest: 0,
  selectionRequest: 0,
  drafts: [],
  draftErrors: [],
  currentDraft: null,
  currentDiff: null,
  draftValidationJobId: null,
  draftSnapshots: new Map(),
  pendingDraftApply: null,
  agentProjectAcknowledged: false,
  projects: { current: null, recent: [], can_switch: false },
  workspaces: { current: null, entries: [], managed: false, can_switch: false, loading: true, editingId: null, forgetId: null, formSnapshot: null, openPending: null, confirmTarget: null, savePending: false, forgetPending: null, unloadAuthorized: false },
  sqlDraftContext: null,
  draftOptions: [],
  handledDraftJobs: new Set(),
  draftJobContexts: new Map(),
  repl: { status: "offline", queryId: null, inspectId: null, queryPending: false, inspectPending: false, reloadPending: false, pollTimer: null, history: [], result: null, stale: false, binding: null, needsReload: false, tablesLoaded: false },
  editorGroups: createEditorGroupState(),
  graphPositions: {},
  environment: { contentHash: null, entries: [], loaded: false, dirty: false },
  editorFlow: createEditorFlowState(),
  editorPrepareGeneration: { value: 0 },
  editorValidation: null,
};

const icons = {
  file: '<svg viewBox="0 0 24 24"><path d="M5 3h9l5 5v13H5V3Z"/><path d="M14 3v6h5"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 6h7l2 2h9v11H3V6Z"/></svg>',
  source: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/></svg>',
  transform: '<svg viewBox="0 0 24 24"><path d="m12 3 8 5v8l-8 5-8-5V8l8-5Z"/><path d="m4 8 8 5 8-5M12 13v8"/></svg>',
  feature: '<svg viewBox="0 0 24 24"><path d="m12 3 8 4-8 4-8-4 8-4Z"/><path d="m4 12 8 4 8-4M4 17l8 4 8-4"/></svg>',
};

function setStatus(message, tone = "") {
  const el = $("#status-message");
  el.textContent = message;
  el.style.color = tone === "error" ? "var(--red)" : tone === "success" ? "var(--mint)" : "";
}

function showError(error, persistent = false) {
  const message = error instanceof Error ? error.message : String(error);
  setStatus(message, "error");
  const banner = document.createElement("div");
  banner.className = "fatal-banner";
  banner.textContent = message;
  document.body.append(banner);
  if (!persistent) setTimeout(() => banner.remove(), 5000);
}

async function api(path, options = {}) {
  const init = { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } };
  if (init.body && typeof init.body !== "string") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : { error: await response.text() };
  if (!response.ok) {
    const error = new Error(payload.error || `${response.status} ${response.statusText}`);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function fileName(path) { return path.split("/").pop(); }
function isDraftPath(path = "") { return path.startsWith(".seeknal/drafts/") || path.includes("/.seeknal/drafts/"); }
function isPipelinePath(path = "") {
  return /\.ya?ml$/i.test(path) && (isDraftPath(path) || path.startsWith("seeknal/") || state.graph.nodes.some(node => nodePath(node) === path));
}
function editorKind(path = "") { return isDraftPath(path) ? "draft" : isPipelinePath(path) ? "active" : "plain"; }
function resetEditorFlow(path = state.selectedFile) {
  state.editorFlow = { ...createEditorFlowState(), kind: editorKind(path), buffer: $("#code-editor")?.value || "" };
}
function invalidateEditorApproval(phase = null) {
  state.editorFlow = { ...state.editorFlow, approval: null, phase: phase || (state.dirty ? "dirty" : "saved") };
}
function nodeName(node) { return node.name || String(node.id || "").split(".").pop(); }
function nodePath(node) { return node.path || state.files.find(file => file.name === nodeName(node))?.path || null; }
function displayKind(kind = "node") { return String(kind).replaceAll("_", " "); }
function isTerminal(job) { return ["succeeded", "success", "failed", "cancelled", "canceled", "timeout"].includes(job?.status); }
function isSuccess(job) { return ["succeeded", "success"].includes(job?.status) && (job?.exit_code == null || job.exit_code === 0); }
function normalizeAgentStatus(status = {}) {
  const identity = status.identity || status.project_identity || {};
  return {
    ...status,
    project_path: status.project_path || identity.project_path || identity.canonical_path || null,
    project_fingerprint: status.project_fingerprint || identity.project_fingerprint || identity.fingerprint || null,
    project_verified: status.project_verified === true || identity.verified === true,
    ready: status.ready === true,
  };
}
function agentIsReady() {
  return Boolean(
    state.agent.configured
    && state.agent.ready === true
    && state.agent.healthy === true
    && state.agent.project_verified === true
    && state.agent.project_path === state.project?.project_path
    && state.agent.capabilities?.draft_only === true
  );
}
function agentIsConfiguring() {
  return state.agent.mode === "owned"
    && !agentIsReady()
    && ["not_started", "configuring"].includes(state.agent.state);
}

function startAgentReadinessWatch() {
  clearTimeout(state.agent.readinessTimer);
  const generation = ++state.agent.readinessGeneration;
  void pollAgentReadiness(generation, 0);
}

async function pollAgentReadiness(generation, attempt) {
  if (generation !== state.agent.readinessGeneration || agentIsReady()) return;
  if (attempt >= 360) {
    state.agent = { ...state.agent, state: "failed", error: "Agent readiness timed out" };
    renderAgentStatus();
    setStatus("Agent readiness timed out", "error");
    return;
  }
  try {
    const status = normalizeAgentStatus(await api("/api/agent/status"));
    if (generation !== state.agent.readinessGeneration) return;
    state.agent = { ...state.agent, ...status };
    renderAgentStatus();
    if (agentIsReady()) {
      setStatus("Project-bound draft agent ready", "success");
      return;
    }
    if (state.agent.state === "failed") {
      setStatus(state.agent.error || "Agent startup failed", "error");
      return;
    }
  } catch {
    if (generation !== state.agent.readinessGeneration) return;
  }
  state.agent.readinessTimer = setTimeout(() => pollAgentReadiness(generation, attempt + 1), 500);
}

async function initialize() {
  try {
    const [project, graph, jobs, agent] = await Promise.all([
      api("/api/state"), api("/api/graph"), api("/api/jobs").catch(() => ({ jobs: [] })), api("/api/agent/status").catch(error => ({ configured: false, healthy: false, error: error.message })),
    ]);
    state.project = project;
    state.files = Array.isArray(project.files) ? project.files : [];
    state.graph = normalizeGraph(graph);
    state.jobs = Array.isArray(jobs.jobs) ? jobs.jobs : Array.isArray(jobs) ? jobs : [];
    state.activeJob = state.jobs.find(job => job.status === "running") || state.jobs.find(job => job.status === "queued") || state.jobs.find(job => !isTerminal(job)) || null;
    state.agent = { ...state.agent, ...normalizeAgentStatus(agent) };
    // Resume before anything else can start a turn that would replace the saved one.
    if (state.agent.mode === "owned") state.agent.resuming = resumeAgentTurn().finally(() => { state.agent.resuming = null; });
    loadGraphPositions();
    await loadDrafts();
    renderProject();
    renderFiles();
    renderGraph();
    renderJobs();
    if (state.activeJob) scheduleJobPoll(state.activeJob.id, 150);
    renderAgentStatus();
    updateEditorActions();
    loadProjects();
    loadWorkspaces();
    refreshReplStatus();
    const initialNode = state.graph.nodes.find(node => nodeName(node) === "customer_ltv") || state.graph.nodes.find(node => node.kind === "transform") || state.graph.nodes[0];
    if (initialNode) await selectNode(initialNode.id, { guard: false });
    else if (state.files[0]) await openFile(state.files[0].path, { guard: false });
    renderEditorGroups();
    setStatus("Seeknal Studio ready", "success");
    if (state.agent.mode === "owned" && !agentIsReady() && state.agent.state !== "failed") startAgentReadinessWatch();
  } catch (error) {
    showError(new Error(`Studio could not start: ${error.message}`), true);
  }
}

function normalizeGraph(graph) {
  return {
    fresh: graph.fresh !== false && graph.freshness !== "stale",
    freshness: graph.freshness || (graph.fresh === false ? "stale" : "current"),
    nodes: Array.isArray(graph.nodes) ? graph.nodes.map(node => ({ ...node, id: String(node.id || `${node.kind}.${node.name}`) })) : [],
    edges: Array.isArray(graph.edges) ? graph.edges.map(edge => ({ source: String(edge.source), target: String(edge.target) })) : [],
  };
}

function renderProject() {
  $("#project-name").textContent = state.project.project || "Seeknal project";
  $("#tree-root").textContent = state.project.project || "seeknal";
  const remote = state.project.remote;
  $("#engine-version").textContent = (state.project.seeknal_version ? `Seeknal ${state.project.seeknal_version}` : "Seeknal engine") + (remote ? ` · SSH ${remote.host}` : "");
  $("#project-path").textContent = remote ? `ssh ${remote.location}` : state.project.project_path || "Local project";
  $("#environment-label").textContent = remote ? `SSH · ${remote.host}` : "Local";
  $("#footer-location").textContent = remote ? `SSH remote · ${remote.host}` : "Local project";
  $("#project-path").title = state.project.project_path || "";
  $("#pipeline-title").textContent = state.project.project || "Pipeline workspace";
  $("#pipeline-breadcrumb").textContent = `${remote?.location || state.project.project_path || "local"} / pipeline`;
  $("#settings-project").textContent = state.project.project || "—";
  $("#settings-path").textContent = remote?.location || state.project.project_path || "—";
  $("#settings-engine").textContent = (state.project.seeknal_version ? `Seeknal ${state.project.seeknal_version}` : "Unavailable") + (remote ? ` on ${remote.host} (SSH remote)` : "");
}

function renderFiles() {
  const tree = $("#file-tree");
  tree.replaceChildren();
  const groups = new Map();
  for (const file of state.files) {
    if (isDraftPath(file.path)) continue;
    const parts = file.path.split("/");
    const folder = parts.length > 1 ? parts.slice(0, -1).join("/") : "project";
    if (!groups.has(folder)) groups.set(folder, []);
    groups.get(folder).push(file);
  }
  for (const [folder, files] of groups) {
    const title = document.createElement("div");
    title.className = "folder-title";
    const folderIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    folderIcon.setAttribute("viewBox", "0 0 24 24");
    const folderPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    folderPath.setAttribute("d", "M3 6h7l2 2h9v11H3V6Z");
    folderIcon.append(folderPath);
    const folderLabel = document.createElement("span");
    folderLabel.textContent = folder;
    title.append(folderIcon, folderLabel);
    tree.append(title);
    for (const file of files.sort((a, b) => a.path.localeCompare(b.path))) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "file-item";
      button.dataset.path = file.path;
      button.title = file.path;
      button.innerHTML = `${icons.file}<span class="file-label"></span><i class="modified"></i>`;
      $(".file-label", button).textContent = fileName(file.path);
      $(".modified", button).hidden = !(state.dirty && state.selectedFile === file.path);
      button.addEventListener("click", async () => { const groupId = state.editorGroups.activeGroup; await openFile(file.path); setMainView("yaml", groupId); });
      tree.append(button);
    }
  }
  syncFileSelection();
}

function renderDraftExplorer() {
  const root = $("#explorer-drafts");
  if (!root) return;
  root.replaceChildren();
  if (!state.drafts.length && !state.draftErrors.length) {
    const empty = document.createElement("div");
    empty.className = "draft-tree-empty";
    empty.textContent = "No drafts yet";
    root.append(empty);
    return;
  }
  for (const draft of state.drafts) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file-item draft-file";
    button.dataset.path = draft.path;
    button.title = draft.path;
    button.innerHTML = `${icons.file}<span class="file-label"></span><i class="modified"></i>`;
    $(".file-label", button).textContent = fileName(draft.path);
    $(".modified", button).hidden = !(state.dirty && state.selectedFile === draft.path);
    button.addEventListener("click", () => selectDraft(draft, { groupId: state.editorGroups.activeGroup }));
    root.append(button);
  }
  for (const error of state.draftErrors) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file-item draft-file draft-error";
    button.dataset.path = error.path || "";
    button.title = error.error || "Seeknal could not parse this draft";
    button.innerHTML = `${icons.file}<span class="file-label"></span><span class="draft-error-mark" aria-label="Invalid draft">!</span>`;
    $(".file-label", button).textContent = fileName(error.path || "Invalid draft");
    button.addEventListener("click", () => openDraftError(error));
    root.append(button);
  }
  syncFileSelection();
}

async function openDraftError(error) {
  if (error.path) {
    try { await openFile(error.path); setMainView("yaml", state.editorGroups.activeGroup); }
    catch (_) { /* openFile already reports the backend error */ }
  }
  setStatus(`${error.path || "Invalid draft"}: ${error.error || "Seeknal could not parse this draft"}`, "error");
}

function syncFileSelection() {
  for (const button of $$(".file-item")) {
    button.classList.toggle("active", button.dataset.path === state.selectedFile);
    const dot = $(".modified", button);
    if (dot) dot.hidden = !(state.dirty && button.dataset.path === state.selectedFile);
  }
}

function graphLayout(nodes, edges) {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const incoming = new Map(nodes.map(node => [node.id, 0]));
  for (const edge of edges) if (byId.has(edge.target) && byId.has(edge.source)) incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1);
  const columns = new Map();
  const queue = nodes.filter(node => incoming.get(node.id) === 0).map(node => node.id);
  if (!queue.length && nodes[0]) queue.push(nodes[0].id);
  for (const id of queue) columns.set(id, 0);
  const seen = new Set();
  while (queue.length) {
    const source = queue.shift();
    if (seen.has(source)) continue;
    seen.add(source);
    for (const edge of edges.filter(item => item.source === source)) {
      columns.set(edge.target, Math.max(columns.get(edge.target) || 0, (columns.get(source) || 0) + 1));
      incoming.set(edge.target, (incoming.get(edge.target) || 1) - 1);
      if (incoming.get(edge.target) <= 0) queue.push(edge.target);
    }
  }
  for (const node of nodes) if (!columns.has(node.id)) columns.set(node.id, 0);
  const grouped = new Map();
  for (const node of nodes) {
    const col = columns.get(node.id);
    if (!grouped.has(col)) grouped.set(col, []);
    grouped.get(col).push(node);
  }
  const maxCol = Math.max(0, ...grouped.keys());
  const positions = new Map();
  for (const [column, items] of grouped) {
    items.forEach((node, row) => positions.set(node.id, {
      x: maxCol === 0 ? 50 : 14 + (column / maxCol) * 72,
      y: 10 + ((row + 1) / (items.length + 1)) * 70,
    }));
  }
  return positions;
}

function graphStorageKey() {
  return `seeknal-studio:graph:${state.project?.remote?.location || state.project?.project_path || "unknown"}`;
}

function loadGraphPositions() {
  try {
    const parsed = JSON.parse(localStorage.getItem(graphStorageKey()) || "{}");
    state.graphPositions = Object.fromEntries(Object.entries(parsed && typeof parsed === "object" ? parsed : {}).filter(([, value]) => Number.isFinite(value?.x) && Number.isFinite(value?.y)));
  }
  catch { state.graphPositions = {}; }
}

function saveGraphPositions() {
  try { localStorage.setItem(graphStorageKey(), JSON.stringify(state.graphPositions)); }
  catch { setStatus("Graph positions could not be saved in this browser", "error"); }
}

function fitGraph() {
  const entries = Object.entries(state.graphPositions);
  if (!entries.length) return;
  const canvas = $("#graph-canvas");
  const cards = $$(".graph-node");
  const padX = Math.max(8, ...cards.map(card => (card.offsetWidth / 2 / Math.max(1, canvas.clientWidth)) * 100));
  const padY = Math.max(8, ...cards.map(card => (card.offsetHeight / 2 / Math.max(1, canvas.clientHeight)) * 100));
  const xs = entries.map(([, value]) => value.x), ys = entries.map(([, value]) => value.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  for (const [id, value] of entries) state.graphPositions[id] = {
    x: maxX === minX ? 50 : padX + ((value.x - minX) / (maxX - minX)) * (100 - padX * 2),
    y: maxY === minY ? 50 : padY + ((value.y - minY) / (maxY - minY)) * (100 - padY * 2),
  };
  saveGraphPositions(); renderGraph();
}

function resetGraphLayout() {
  state.graphPositions = {};
  try { localStorage.removeItem(graphStorageKey()); } catch {}
  renderGraph();
}

function renderGraph() {
  const nodesRoot = $("#graph-nodes");
  nodesRoot.replaceChildren();
  $("#node-count").textContent = `${state.graph.nodes.length} nodes`;
  $("#edge-count").textContent = `${state.graph.edges.length} dependencies`;
  const graphStale = state.dirty || state.graph.fresh === false;
  $("#graph-state").textContent = state.dirty ? "saved graph · buffer modified" : graphStale ? "graph stale · validate or run" : "graph current";
  $("#graph-state").classList.toggle("stale", graphStale);
  const query = state.graphQuery.trim().toLowerCase();
  let visibleNodes = state.graph.nodes;
  let visibleEdges = state.graph.edges;
  if (query) {
    const matches = new Set(state.graph.nodes.filter(node => `${node.id} ${nodeName(node)} ${node.kind}`.toLowerCase().includes(query)).map(node => node.id));
    const context = new Set(matches);
    for (const edge of state.graph.edges) if (matches.has(edge.source) || matches.has(edge.target)) { context.add(edge.source); context.add(edge.target); }
    visibleNodes = state.graph.nodes.filter(node => context.has(node.id));
    visibleEdges = state.graph.edges.filter(edge => context.has(edge.source) && context.has(edge.target));
    $("#graph-state").textContent = matches.size ? `${matches.size} match${matches.size === 1 ? "" : "es"} · ${visibleNodes.length} in context` : "no matches · reset search";
    $("#graph-state").classList.toggle("stale", matches.size === 0);
  }
  if (!visibleNodes.length) {
    const empty = document.createElement("div");
    empty.className = "graph-empty";
    empty.textContent = query ? `No pipeline nodes match “${state.graphQuery.trim()}”. Reset search to restore the graph.` : "No pipeline nodes found in this project.";
    nodesRoot.append(empty);
    $("#graph-edges").replaceChildren();
    return;
  }
  const positions = graphLayout(visibleNodes, visibleEdges);
  let positionsChanged = false;
  for (const node of visibleNodes) {
    const button = document.createElement("button");
    const kindClass = String(node.kind || "node").replaceAll(" ", "_");
    button.type = "button";
    button.className = `graph-node kind-${kindClass}`;
    button.dataset.nodeId = node.id;
    button.setAttribute("aria-label", `${displayKind(node.kind)} ${nodeName(node)}`);
    const position = state.graphPositions[node.id] || positions.get(node.id);
    if (!state.graphPositions[node.id]) { state.graphPositions[node.id] = position; positionsChanged = true; }
    button.style.left = `${position.x}%`;
    button.style.top = `${position.y}%`;
    button.style.transform = "translate(-50%,-50%)";
    const icon = node.kind === "source" ? icons.source : String(node.kind).includes("feature") ? icons.feature : icons.transform;
    button.innerHTML = `<div class="node-top"><span class="node-icon">${icon}</span><span class="node-copy"><small class="node-kind"></small><strong class="node-name"></strong></span><span class="node-state">✓</span></div><div class="node-meta"><span class="row-count"></span></div>`;
    $(".node-kind", button).textContent = displayKind(node.kind);
    $(".node-name", button).textContent = nodeName(node);
    const rowCount = node.row_count;
    $(".row-count", button).textContent = Number.isFinite(rowCount) ? `${rowCount.toLocaleString()} rows` : "Not executed";
    const status = String(node.status || "unknown").toLowerCase();
    const statusEl = $(".node-state", button);
    if (status === "stale" || node.stale || state.graph.fresh === false) {
      statusEl.classList.add("stale");
      statusEl.textContent = "!";
    } else if (!(status === "success" || status === "succeeded" || Number.isFinite(rowCount))) {
      statusEl.classList.add("unknown");
      statusEl.textContent = "·";
    }
    button.classList.toggle("selected", node.id === state.selectedNode);
    let dragged = false;
    button.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      const canvas = $("#graph-canvas");
      const rect = canvas.getBoundingClientRect();
      const buttonRect = button.getBoundingClientRect();
      const grabX = event.clientX - (buttonRect.left + buttonRect.width / 2);
      const grabY = event.clientY - (buttonRect.top + buttonRect.height / 2);
      const startX = event.clientX, startY = event.clientY;
      button.setPointerCapture(event.pointerId);
      button.classList.add("dragging");
      const move = moveEvent => {
        if (Math.abs(moveEvent.clientX - startX) + Math.abs(moveEvent.clientY - startY) > 4) dragged = true;
        if (!dragged) return;
        const marginX = (button.offsetWidth / 2 / rect.width) * 100, marginY = (button.offsetHeight / 2 / rect.height) * 100;
        const x = Math.max(marginX, Math.min(100 - marginX, ((moveEvent.clientX - grabX - rect.left) / rect.width) * 100));
        const y = Math.max(marginY, Math.min(100 - marginY, ((moveEvent.clientY - grabY - rect.top) / rect.height) * 100));
        state.graphPositions[node.id] = { x, y };
        button.style.left = `${x}%`; button.style.top = `${y}%`;
        requestAnimationFrame(renderEdges);
      };
      const up = () => {
        button.classList.remove("dragging");
        button.removeEventListener("pointermove", move); button.removeEventListener("pointerup", up);
        if (dragged) saveGraphPositions();
      };
      button.addEventListener("pointermove", move); button.addEventListener("pointerup", up);
    });
    button.addEventListener("click", event => { if (!dragged) selectNode(node.id); else event.preventDefault(); });
    button.addEventListener("dblclick", () => { const groupId = state.editorGroups.activeGroup; selectNode(node.id).then(() => setMainView("yaml", groupId)); });
    button.addEventListener("keydown", event => {
      const delta = event.shiftKey ? 5 : 1;
      const next = { ...state.graphPositions[node.id] };
      if (event.key === "ArrowLeft") next.x -= delta;
      else if (event.key === "ArrowRight") next.x += delta;
      else if (event.key === "ArrowUp") next.y -= delta;
      else if (event.key === "ArrowDown") next.y += delta;
      else return;
      event.preventDefault();
      const canvas = $("#graph-canvas");
      const marginX = (button.offsetWidth / 2 / canvas.clientWidth) * 100, marginY = (button.offsetHeight / 2 / canvas.clientHeight) * 100;
      next.x = Math.max(marginX, Math.min(100 - marginX, next.x)); next.y = Math.max(marginY, Math.min(100 - marginY, next.y));
      state.graphPositions[node.id] = next; saveGraphPositions(); renderGraph();
      requestAnimationFrame(() => $(`.graph-node[data-node-id="${CSS.escape(node.id)}"]`)?.focus());
    });
    nodesRoot.append(button);
  }
  if (positionsChanged) saveGraphPositions();
  requestAnimationFrame(renderEdges);
}

function renderEdges() {
  const svg = $("#graph-edges");
  const canvas = $("#graph-canvas");
  const canvasRect = canvas.getBoundingClientRect();
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${canvasRect.width} ${canvasRect.height}`);
  for (const edge of state.graph.edges) {
    const source = $(`.graph-node[data-node-id="${CSS.escape(edge.source)}"]`);
    const target = $(`.graph-node[data-node-id="${CSS.escape(edge.target)}"]`);
    if (!source || !target) continue;
    const a = source.getBoundingClientRect();
    const b = target.getBoundingClientRect();
    const x1 = a.right - canvasRect.left;
    const y1 = a.top + a.height / 2 - canvasRect.top;
    const x2 = b.left - canvasRect.left;
    const y2 = b.top + b.height / 2 - canvasRect.top;
    const bend = Math.max(25, (x2 - x1) * .48);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`);
    path.setAttribute("class", `graph-edge${edge.source === state.selectedNode || edge.target === state.selectedNode ? " highlighted" : ""}`);
    svg.append(path);
  }
}

function canLeaveBuffer() {
  return !state.dirty || window.confirm("This file has unsaved changes. Discard them?");
}

function hasActiveWork() {
  return Boolean((state.activeJob && !isTerminal(state.activeJob)) || state.agent.busy || state.repl.queryId || state.repl.queryPending || state.repl.inspectId || state.repl.inspectPending || state.repl.reloadPending);
}

function canSwitchProject() {
  if (state.dirty && !window.confirm("This file has unsaved changes. Discard them and switch projects?")) return false;
  if (hasActiveWork()) { setStatus("Project-bound work is active. Finish or cancel it before switching projects.", "error"); return false; }
  return true;
}

async function openFile(path, { guard = true } = {}) {
  if (path === state.selectedFile) return;
  if (guard && !canLeaveBuffer()) return;
  nextRequestGeneration(state.editorPrepareGeneration);
  try {
    const request = ++state.fileRequest;
    setStatus(`Opening ${path}…`);
    const payload = await api(`/api/file?path=${encodeURIComponent(path)}`);
    if (request !== state.fileRequest) return;
    state.selectedFile = payload.path || path;
    state.originalContent = String(payload.content || "");
    state.dirty = false;
    const editor = $("#code-editor");
    editor.disabled = false;
    editor.value = state.originalContent;
    if (!isDraftPath(state.selectedFile)) { state.currentDraft = null; state.currentDiff = null; }
    resetEditorFlow(state.selectedFile);
    $("#editor-title").textContent = fileName(state.selectedFile);
    $("#save-status").textContent = state.editorFlow.kind === "draft" ? "Draft · not applied" : state.editorFlow.kind === "active" ? "Saved · Active" : "Saved";
    updateEditorActions();
    updateLineNumbers();
    syncFileSelection();
    renderGraph();
    const linked = state.graph.nodes.find(node => nodePath(node) === state.selectedFile || node.name === fileName(path).replace(/\.ya?ml$/, ""));
    if (linked) {
      state.selectedNode = linked.id;
      renderGraph();
    }
    setStatus(`Opened ${state.selectedFile}`);
  } catch (error) { showError(error); }
}

async function selectNode(id, { guard = true } = {}) {
  const selection = ++state.selectionRequest;
  const node = state.graph.nodes.find(item => item.id === id);
  if (!node) return;
  const path = nodePath(node);
  if (path && path !== state.selectedFile) {
    if (guard && !canLeaveBuffer()) return;
    await openFile(path, { guard: false });
  }
  if (selection !== state.selectionRequest) return;
  state.selectedNode = id;
  $("#pipeline-title").textContent = nodeName(node).replaceAll("_", " ");
  $("#pipeline-breadcrumb").textContent = `${state.project?.project || "project"} / ${displayKind(node.kind)} / ${nodeName(node)}`;
  renderGraph();
}

function updateLineNumbers() {
  const editor = $("#code-editor");
  const count = Math.max(1, editor.value.split("\n").length);
  $("#line-numbers").textContent = Array.from({ length: count }, (_, index) => index + 1).join("\n");
  $("#line-numbers").scrollTop = editor.scrollTop;
  renderSyntax(editor.value);
}

function renderSyntax(content) {
  const layer = $("#syntax-layer");
  layer.replaceChildren();
  const tokenPattern = /(['"][^'"\n]*['"]|\b\d+(?:\.\d+)?\b|\b(?:SELECT|FROM|WHERE|GROUP|BY|AS|SUM|MAX|COUNT|CASE|WHEN|THEN|ELSE|END|AND|OR|JOIN|ON|WITH|ORDER|DESC|ASC|DISTINCT)\b|[A-Za-z_][\w-]*(?=\s*:))/gi;
  const sqlWords = /^(?:SELECT|FROM|WHERE|GROUP|BY|AS|SUM|MAX|COUNT|CASE|WHEN|THEN|ELSE|END|AND|OR|JOIN|ON|WITH|ORDER|DESC|ASC|DISTINCT)$/i;
  for (const [lineIndex, line] of content.split("\n").entries()) {
    const commentAt = line.indexOf("#");
    const code = commentAt >= 0 ? line.slice(0, commentAt) : line;
    let cursor = 0;
    for (const match of code.matchAll(tokenPattern)) {
      if (match.index > cursor) layer.append(document.createTextNode(code.slice(cursor, match.index)));
      const token = document.createElement("span");
      token.textContent = match[0];
      token.className = /^['"]/.test(match[0]) ? "syntax-string" : /^\d/.test(match[0]) ? "syntax-number" : sqlWords.test(match[0]) ? "syntax-keyword" : "syntax-key";
      layer.append(token); cursor = match.index + match[0].length;
    }
    layer.append(document.createTextNode(code.slice(cursor)));
    if (commentAt >= 0) { const comment = document.createElement("span"); comment.className = "syntax-comment"; comment.textContent = line.slice(commentAt); layer.append(comment); }
    if (lineIndex < content.split("\n").length - 1) layer.append(document.createTextNode("\n"));
  }
  const editor = $("#code-editor"); layer.scrollTop = editor.scrollTop; layer.scrollLeft = editor.scrollLeft;
}

function markDirty() {
  const buffer = $("#code-editor").value;
  state.dirty = buffer !== state.originalContent;
  if (state.editorFlow.buffer !== buffer) {
    state.editorFlow = editorFlowTransition(state.editorFlow, "edit", { buffer, dirty: state.dirty });
    state.draftValidationJobId = null;
  }
  $("#save-status").textContent = state.editorFlow.kind === "draft" ? "Draft · not applied" : state.dirty ? "Unsaved" : "Saved · Active";
  syncFileSelection();
  $("#graph-state").textContent = state.dirty ? "saved graph · buffer modified" : "graph current";
  if (!state.dirty && state.graph.fresh === false) $("#graph-state").textContent = "graph stale · validate or run";
  $("#graph-state").classList.toggle("stale", state.dirty || state.graph.fresh === false);
  if (state.currentDiff?.path === state.selectedFile) $("#draft-review-status").textContent = state.dirty ? "Draft buffer modified · Dry run will prepare this buffer" : `${state.currentDiff.path} → ${state.currentDiff.destination || "project"}`;
  if (state.repl.result) markReplStale("Editor buffer changed");
  updateEditorActions();
}

async function saveCurrentFile() {
  if (!state.selectedFile || !state.dirty) return;
  if (state.editorFlow.kind !== "plain") {
    await runEditorFlowAction();
    return;
  }
  try {
    $("#save-button").disabled = true;
    $("#save-status").textContent = "Saving…";
    const content = $("#code-editor").value;
    await api("/api/file", { method: "POST", body: { path: state.selectedFile, content } });
    state.originalContent = content;
    state.dirty = false;
    state.editorFlow = { ...state.editorFlow, phase: "saved", approval: null, buffer: content };
    $("#save-status").textContent = "Saved";
    syncFileSelection();
    setStatus(`Saved ${state.selectedFile}`, "success");
    updateEditorActions();
    await refreshGraph();
    if (state.currentDiff?.path === state.selectedFile) {
      await loadDrafts();
      const refreshedDraft = state.drafts.find(draft => draft.path === state.selectedFile);
      if (refreshedDraft) await selectDraft(refreshedDraft);
    }
  } catch (error) {
    $("#save-button").disabled = false;
    $("#save-status").textContent = "Save failed";
    showError(error);
  }
}

async function refreshGraph() {
  try {
    $("#graph-state").textContent = "syncing";
    state.graph = normalizeGraph(await api("/api/graph"));
    renderGraph();
    setStatus("Pipeline graph refreshed", "success");
  } catch (error) { showError(error); }
}

async function refreshWorkspaceState() {
  const [project, graph] = await Promise.all([api("/api/state"), api("/api/graph")]);
  state.project = project;
  state.files = Array.isArray(project.files) ? project.files : [];
  state.graph = normalizeGraph(graph);
  renderProject(); renderFiles(); renderGraph();
  await loadDrafts();
  updateEditorActions();
}

async function startJob(action, node = null, extra = {}, local = {}) {
  if (state.activeJob && !isTerminal(state.activeJob)) return null;
  const exactSnapshotApply = action === "apply-draft" && local.snapshotAuthorized === true;
  if (state.dirty && action !== "validate-draft" && !exactSnapshotApply) {
    setStatus("Save the modified file before running pipeline tools", "error");
    $("#save-button").focus();
    return null;
  }
  try {
    setStatus(`Starting ${action}…`);
    const payload = await api("/api/jobs", { method: "POST", body: { action, ...(node ? { node } : {}), ...extra } });
    const job = payload.job || payload;
    state.activeJob = job;
    state.jobs = [job, ...state.jobs.filter(item => item.id !== job.id)];
    renderJobs();
    scheduleJobPoll(job.id, 150);
    return job;
  } catch (error) { showError(error); return null; }
}

function scheduleJobPoll(id, delay = 700) {
  clearTimeout(state.jobTimer);
  state.jobTimer = setTimeout(() => pollJob(id), delay);
}

async function pollJob(id) {
  try {
    const payload = await api(`/api/jobs/${encodeURIComponent(id)}`);
    const job = payload.job || payload;
    state.activeJob = job;
    state.jobs = [job, ...state.jobs.filter(item => item.id !== job.id)];
    renderJobs();
    if (!isTerminal(job)) scheduleJobPoll(id);
    else {
      setStatus(isSuccess(job) ? `${job.action || "Job"} completed` : `${job.action || "Job"} ${job.status}`, isSuccess(job) ? "success" : "error");
      if (job.action === "draft" && !state.handledDraftJobs.has(job.id)) {
        state.handledDraftJobs.add(job.id);
        if (isSuccess(job) && job.draft_path) await openGeneratedDraft(job);
      }
      if (job.id === state.editorValidation?.jobId) {
        const validation = state.editorValidation;
        const flow = state.editorFlow;
        const exactEditor = matchesValidationContext(validation, flow, state.selectedFile, $("#code-editor").value);
        const validationMessage = isSuccess(job) ? job.validation_message || null : job.validation_error || null;
        if (exactEditor) state.editorFlow = editorFlowTransition(flow, "validation-finish", { success: isSuccess(job), buffer: $("#code-editor").value, message: validationMessage });
        $("#draft-review-status").textContent = isSuccess(job)
          ? exactEditor ? "Draft validated for this exact content hash" : "Editor changed after dry run started · changes need dry run"
          : "Draft validation failed; inspect run logs";
        state.editorValidation = null;
        updateEditorActions();
      }
      if (job.action === "apply-draft" && isSuccess(job)) {
        const applied = state.pendingDraftApply;
        await refreshWorkspaceState();
        if (matchesApplyContext(applied, state.selectedFile, $("#code-editor").value)) {
          state.selectedFile = null;
          state.currentDraft = null;
          state.currentDiff = null;
          await openFile(applied.destination, { guard: false });
          setMainView("yaml", applied.groupId);
          $("#save-status").textContent = "Saved · Active";
          setStatus(`Saved validated changes to ${applied.destination}`, "success");
        } else if (applied && state.selectedFile === applied.sourcePath) {
          const editor = $("#code-editor");
          const rebased = rebaseEditorAfterApply(applied, editor.value);
          state.selectedFile = rebased.selectedFile;
          state.originalContent = rebased.originalContent;
          state.dirty = rebased.dirty;
          state.editorFlow = rebased.flow;
          state.currentDraft = null;
          state.currentDiff = null;
          $("#editor-title").textContent = fileName(state.selectedFile);
          $("#save-status").textContent = "Unsaved";
          syncFileSelection(); updateLineNumbers(); updateEditorActions(); renderGraph();
          setMainView("yaml", applied.groupId);
          setStatus("Changes applied on disk; newer editor edits were preserved", "success");
        } else if (applied) setStatus(`Changes applied to ${applied.destination}; current editor selection was preserved`, "success");
        if (applied) state.draftSnapshots.delete(applied.preparedPath);
        state.pendingDraftApply = null;
        if ($("#draft-dialog").open) $("#draft-dialog").close();
        markReplStale("Project outputs may have changed");
      } else {
        if (job.action === "apply-draft") {
          const applied = state.pendingDraftApply;
          state.pendingDraftApply = null;
          if (applied && state.selectedFile === applied.sourcePath) invalidateEditorApproval("failed");
          state.editorFlow.busy = false;
          updateEditorActions();
        }
        await refreshGraph();
      }
      if (job.action === "run" && isSuccess(job)) markReplStale("Pipeline outputs changed");
    }
  } catch (error) {
    showError(error);
    scheduleJobPoll(id, 1500);
  }
}

function renderJobs() {
  const authoritativeJob = state.activeJob;
  const job = state.jobs.find(item => item.id === state.selectedLogJobId) || authoritativeJob || state.jobs[0];
  $("#job-count").textContent = state.jobs.length;
  $("#problem-count").textContent = state.jobs.filter(item => isTerminal(item) && !isSuccess(item)).length;
  const running = authoritativeJob && !isTerminal(authoritativeJob);
  const displayRunning = job && !isTerminal(job);
  $("#run-button").disabled = Boolean(running);
  $("#validate-button").disabled = Boolean(running);
  $("#cancel-button").hidden = !running;
  $("#cancel-button").title = running ? `Cancel active job ${authoritativeJob.id}` : "No active job";
  $("#cancel-button").setAttribute("aria-label", running ? `Cancel active job ${authoritativeJob.id}` : "Cancel active job");
  const indicator = $("#run-indicator");
  indicator.className = `run-indicator ${!job ? "idle" : displayRunning ? "running" : isSuccess(job) ? "success" : "failed"}`;
  $("#run-title").textContent = !job ? "No run started" : displayRunning ? `${capitalize(job.action || "job")} running` : `${capitalize(job.action || "job")} ${isSuccess(job) ? "succeeded" : job.status}`;
  const details = [];
  if (job?.exit_code != null) details.push(`exit ${job.exit_code}`);
  if (job?.duration_ms != null) details.push(`${(job.duration_ms / 1000).toFixed(2)} s`);
  if (job?.id) details.push(job.id);
  $("#run-detail").textContent = details.join(" · ") || "Pipeline execution appears here";
  $("#job-logs").textContent = job?.logs || job?.output || "No logs yet.";
  const progress = $("#node-progress"); progress.replaceChildren();
  const nodes = Array.isArray(job?.nodes) ? job.nodes : [];
  for (const item of nodes) {
    const el = document.createElement("span"); el.className = `progress-node ${item.status === "success" || item.status === "succeeded" ? "done" : ""}`; el.textContent = item.name || item.id; progress.append(el);
  }
  renderLogJobList(job?.id);
}

function renderLogJobList(selectedId) {
  const root = $("#logs-job-list");
  root.replaceChildren();
  if (!state.jobs.length) {
    const empty = document.createElement("p"); empty.textContent = "No jobs started yet."; root.append(empty); return;
  }
  for (const job of state.jobs) {
    const button = document.createElement("button"); button.type = "button";
    button.className = job.id === selectedId ? "active" : "";
    const status = document.createElement("i"); status.className = isSuccess(job) ? "success" : isTerminal(job) ? "failed" : "running";
    const copy = document.createElement("span");
    const title = document.createElement("strong"); title.textContent = capitalize(job.action || "job");
    const meta = document.createElement("small"); meta.textContent = [job.status, job.id].filter(Boolean).join(" · ");
    copy.append(title, meta); button.append(status, copy);
    button.addEventListener("click", () => selectLogJob(job.id));
    root.append(button);
  }
}

async function selectLogJob(id) {
  state.selectedLogJobId = id; renderJobs();
  try {
    const payload = await api(`/api/jobs/${encodeURIComponent(id)}`); const job = payload.job || payload;
    state.jobs = state.jobs.map(item => item.id === id ? job : item); renderJobs();
  } catch (error) { showError(error); }
}

async function cancelActiveJob() {
  if (!state.activeJob || isTerminal(state.activeJob)) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(state.activeJob.id)}/cancel`, { method: "POST", body: {} });
    setStatus("Cancellation requested");
    scheduleJobPoll(state.activeJob.id, 100);
  } catch (error) { showError(error); }
}

function capitalize(value) { return String(value).charAt(0).toUpperCase() + String(value).slice(1); }

function renderAgentStatus() {
  const configured = Boolean(state.agent.configured);
  const identityMatches = Boolean(
    state.agent.project_verified
    && state.agent.project_path
    && state.project?.project_path
    && state.agent.project_path === state.project.project_path
  );
  const draftOnly = state.agent.capabilities?.draft_only === true;
  const ready = agentIsReady();
  const checking = agentIsConfiguring();
  const badge = $("#agent-badge");
  badge.classList.toggle("connected", ready);
  badge.classList.toggle("checking", checking);
  badge.classList.toggle("disconnected", !ready && !checking);
  let badgeText = "Setup required";
  if (ready) badgeText = "Ready · verified";
  else if (checking) badgeText = "Checking";
  else if (configured) badgeText = "Unavailable";
  $("span", badge).textContent = badgeText;
  const input = $("#agent-input");
  const send = $("#agent-send");
  input.disabled = !ready || state.agent.busy;
  send.disabled = !ready || state.agent.busy;
  let hintText = "Start the project-bound agent in Settings";
  if (state.agent.cancelPending) hintText = "Requesting cancellation…";
  else if (ready) hintText = `${state.agent.model || "Model ready"} · drafts only`;
  else if (checking) hintText = "Verifying provider, tool calling, and project identity…";
  else if (configured) hintText = state.agent.error || "Verifying model and project identity…";
  $("#agent-hint").textContent = hintText;
  $("#agent-cancel").hidden = !state.agent.busy;
  $("#agent-cancel").disabled = state.agent.cancelPending;
  $("#agent-send").hidden = state.agent.busy;
  let settingsText = "Not configured";
  if (ready) settingsText = `${state.agent.provider || "provider"} / ${state.agent.model || "model"} ready`;
  else if (checking) settingsText = `${state.agent.model || "Agent"} is starting`;
  else if (configured) settingsText = `Configured, not ready${state.agent.error ? `: ${state.agent.error}` : ""}`;
  $("#settings-agent").textContent = settingsText;
  $("#settings-agent-identity").textContent = identityMatches ? "Selected project verified" : "Not verified";
  $("#settings-agent-authority").textContent = draftOnly ? "Inspect and draft only; apply/run denied" : "Unsafe or unavailable";
  let configStatus = state.agent.error || "Credentials stay in the backend and never enter page state.";
  if (ready) configStatus = `Verified ${state.agent.model}; project identity and draft-only policy are active.`;
  else if (checking) configStatus = "Agent startup is running in the background…";
  $("#agent-config-status").textContent = configStatus;
  $("#agent-session-label").textContent = ready ? `${state.agent.model} · draft only` : "Draft-only pipeline copilot";
  let configureText = "Start and verify agent";
  if (ready) configureText = "Reverify agent";
  else if (configured) configureText = "Retry verification";
  $("#agent-configure").textContent = configureText;
  $("#agent-configure").disabled = state.agent.busy || checking;
  $("#agent-provider").value = state.agent.provider || "openai";
  $("#agent-model").value = state.agent.model || "deepseek-v4.1-flash:netra";
  let introText = "Start the owned local agent in Settings. It will be bound to this project with draft-only authority.";
  if (ready) {
    introText = "This verified local agent can inspect the selected project and create drafts. You validate, apply, and run every change.";
  } else if (checking) {
    introText = "Starting the owned agent and verifying the selected project, model, and draft-only tool surface…";
  } else if (configured) {
    introText = `Agent configured but not ready${state.agent.error ? `: ${state.agent.error}` : "."}`;
  }
  const introCopy = $("#agent-intro-copy");
  if (introCopy) introCopy.textContent = introText;
  const intro = $(".agent-intro");
  if (intro) {
    let facts = $(".workspace-facts", intro);
    if (!facts) { facts = document.createElement("div"); facts.className = "workspace-facts"; intro.append(facts); }
    facts.replaceChildren();
    const entries = [
      ["Project indexed", `${state.files.length} editable files`],
      ["Lineage loaded", `${state.graph.nodes.length} nodes · ${state.graph.edges.length} edges`],
      ["Project identity", identityMatches ? "Verified local binding" : "Not verified"],
      ["Agent authority", draftOnly ? "Draft only · apply/run denied" : "Unavailable"],
    ];
    for (const [label, detail] of entries) {
      const row = document.createElement("div"); const mark = document.createElement("i"); const copy = document.createElement("span"); const strong = document.createElement("strong");
      strong.textContent = label; copy.append(strong, document.createTextNode(detail)); row.append(mark, copy); facts.append(row);
    }
  }
}

function appendAgentMessage(role, text, className = "") {
  const intro = $(".agent-intro"); if (intro) intro.remove();
  const message = document.createElement("div");
  message.className = `message ${role} ${className}`.trim();
  const label = document.createElement("span"); label.className = "message-label"; label.textContent = role === "user" ? "You" : "Seeknal agent";
  const body = document.createElement("div"); body.textContent = text;
  message.append(label, body); $("#agent-feed").append(message); message.scrollIntoView({ block: "end" });
}

function agentEventText(event) {
  const data = event?.data;
  if (typeof data === "string") return data;
  if (data && typeof data === "object") return data.message || data.text || data.output || data.result || data.summary || "";
  return event?.message || event?.text || "";
}

// Agent answers are untrusted model output: markdown becomes DOM nodes built
// with textContent only (never innerHTML), and only http(s) links are kept.
// Underscore emphasis is ignored so snake_case node names stay intact.
function appendInlineMarkdown(parent, text) {
  const pattern = /(`+)([^`]|[^`][\s\S]*?[^`])\1|\*\*([^*]+)\*\*|~~([^~]+)~~|\*([^*\s][^*]*?)\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) parent.append(text.slice(last, match.index));
    let node;
    if (match[2] !== undefined) { node = document.createElement("code"); node.textContent = match[2]; }
    else if (match[3] !== undefined) { node = document.createElement("strong"); appendInlineMarkdown(node, match[3]); }
    else if (match[4] !== undefined) { node = document.createElement("del"); appendInlineMarkdown(node, match[4]); }
    else if (match[5] !== undefined) { node = document.createElement("em"); appendInlineMarkdown(node, match[5]); }
    else { node = document.createElement("a"); node.href = match[7]; node.target = "_blank"; node.rel = "noopener noreferrer"; appendInlineMarkdown(node, match[6]); }
    parent.append(node); last = match.index + match[0].length;
  }
  if (last < text.length) parent.append(text.slice(last));
}

const MARKDOWN_MAX_CHARS = 60000;
const MARKDOWN_MAX_LINE = 4000;
const MARKDOWN_MAX_DEPTH = 6;

function renderMarkdown(container, text, depth = 0) {
  container.replaceChildren(); container._markdown = text;
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
  // Oversized or deeply nested output is shown as plain text: it keeps every
  // regex below on short lines and bounds blockquote recursion.
  if (String(text).length > MARKDOWN_MAX_CHARS || depth > MARKDOWN_MAX_DEPTH || lines.some(line => line.length > MARKDOWN_MAX_LINE)) {
    const plain = document.createElement("p"); plain.className = "markdown-plain"; plain.textContent = text; container.append(plain);
    return;
  }
  const listItem = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const rule = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/;
  const divider = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  const tableAt = index => lines[index].includes("|") && index + 1 < lines.length && divider.test(lines[index + 1]);
  const cells = line => line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
  const startsBlock = index => /^\s*(```|~~~|>)/.test(lines[index]) || /^\s{0,3}#{1,6}[ \t]/.test(lines[index]) || listItem.test(lines[index]) || rule.test(lines[index]) || tableAt(index);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)/);
    if (fence) {
      const code = []; i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) code.push(lines[i++]);
      i += 1;
      const pre = document.createElement("pre"); const inner = document.createElement("code"); inner.textContent = code.join("\n");
      if (fence[2]) pre.dataset.language = fence[2];
      pre.append(inner); container.append(pre); continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,6})[ \t]+(.*)$/);
    if (heading) {
      // Strip an optional closing run of #s by hand; a regex here backtracks badly.
      let title = heading[2].trimEnd(); let end = title.length;
      while (end > 0 && title[end - 1] === "#") end -= 1;
      if (end === 0 || title[end - 1] === " " || title[end - 1] === "\t") title = title.slice(0, end).trimEnd();
      const node = document.createElement(`h${Math.min(heading[1].length + 2, 6)}`); appendInlineMarkdown(node, title); container.append(node); i += 1; continue;
    }
    if (rule.test(line)) { container.append(document.createElement("hr")); i += 1; continue; }
    if (tableAt(i)) {
      const table = document.createElement("table"); const head = table.createTHead().insertRow();
      for (const cell of cells(line)) { const th = document.createElement("th"); appendInlineMarkdown(th, cell); head.append(th); }
      const body = table.createTBody(); i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) { const row = body.insertRow(); for (const cell of cells(lines[i])) appendInlineMarkdown(row.insertCell(), cell); i += 1; }
      const wrap = document.createElement("div"); wrap.className = "markdown-table"; wrap.append(table); container.append(wrap); continue;
    }
    if (/^\s*>/.test(line)) {
      const quoted = []; while (i < lines.length && /^\s*>/.test(lines[i])) quoted.push(lines[i++].replace(/^\s*>\s?/, ""));
      const quote = document.createElement("blockquote"); renderMarkdown(quote, quoted.join("\n"), depth + 1); container.append(quote); continue;
    }
    if (listItem.test(line)) {
      const stack = [];
      while (i < lines.length) {
        const item = lines[i].match(listItem);
        if (!item) {
          // An indented line without a marker continues the previous item.
          if (!lines[i].trim() || !/^\s/.test(lines[i])) break;
          const li = stack.at(-1).list.lastElementChild; li.append(document.createElement("br")); appendInlineMarkdown(li, lines[i].trim()); i += 1; continue;
        }
        const indent = item[1].replace(/\t/g, "  ").length; const ordered = /\d/.test(item[2]);
        while (stack.length && indent < stack.at(-1).indent) stack.pop();
        if (!stack.length || indent > stack.at(-1).indent) {
          const list = document.createElement(ordered ? "ol" : "ul");
          if (ordered) list.start = parseInt(item[2], 10);
          (stack.length ? stack.at(-1).list.lastElementChild : container).append(list);
          stack.push({ indent, list });
        }
        const li = document.createElement("li"); appendInlineMarkdown(li, item[3]); stack.at(-1).list.append(li); i += 1;
      }
      continue;
    }
    const paragraph = [lines[i++].trim()];
    while (i < lines.length && lines[i].trim() && !startsBlock(i)) paragraph.push(lines[i++].trim());
    const node = document.createElement("p");
    paragraph.forEach((part, index) => { if (index) node.append(document.createElement("br")); appendInlineMarkdown(node, part); });
    container.append(node);
  }
}

function renderAgentAnswer(turnId, text) {
  if (!text) return;
  for (const details of $$(".tool-details", $("#agent-feed"))) {
    if (details._rawText === text) {
      const compact = document.createElement("span"); compact.className = "tool-result-summary"; compact.textContent = "Tool output is included once in the final answer below.";
      details.replaceWith(compact);
    }
  }
  let message = $$(".message.agent[data-turn-id]", $("#agent-feed")).find(item => item.dataset.turnId === turnId);
  if (!message) {
    const intro = $(".agent-intro"); if (intro) intro.remove();
    message = document.createElement("div"); message.className = "message agent"; message.dataset.turnId = turnId;
    const label = document.createElement("span"); label.className = "message-label"; label.textContent = "Seeknal agent";
    const body = document.createElement("div"); body.className = "message-body markdown";
    message.append(label, body); $("#agent-feed").append(message);
  }
  renderMarkdown($(".message-body", message), text);
  message.scrollIntoView({ block: "nearest" });
}

function renderAgentTurnStatus(turnId, status, draftCount = null) {
  let card = $$(".turn-status[data-turn-id]", $("#agent-feed")).find(item => item.dataset.turnId === turnId);
  if (!card) {
    card = document.createElement("div"); card.className = "turn-status"; card.dataset.turnId = turnId;
    const copy = document.createElement("span"); copy.className = "turn-status-copy";
    const review = document.createElement("button"); review.type = "button"; review.className = "turn-review-button"; review.addEventListener("click", openDraftReview);
    card.append(copy, review); $("#agent-feed").append(card);
  }
  $(".turn-status-copy", card).textContent = status;
  const review = $(".turn-review-button", card);
  review.hidden = !(draftCount > 0);
  review.textContent = draftCount > 0 ? `Review ${draftCount} draft${draftCount === 1 ? "" : "s"}` : "Review drafts";
}

// Seeknal's ask_user tool pauses the turn; the answer goes back through
// /api/agent/answer and the worker resumes with the chosen label or text.
function renderAgentQuestion(turnId, data) {
  const card = document.createElement("div"); card.className = "agent-question"; card.dataset.questionId = data.question_id || "";
  const label = document.createElement("span"); label.className = "message-label"; label.textContent = "Seeknal agent asks";
  const title = document.createElement("div"); title.className = "agent-question-title message-body markdown"; renderMarkdown(title, data.question || "The agent needs your input");
  const multi = Boolean(data.multi_select);
  const options = document.createElement("div"); options.className = "agent-question-options";
  for (const option of Array.isArray(data.options) ? data.options : []) {
    const button = document.createElement("button"); button.type = "button"; button.className = "agent-question-option"; button.dataset.label = option.label;
    if (multi) button.setAttribute("aria-pressed", "false");
    const name = document.createElement("strong"); name.textContent = option.label;
    if (option.recommended) { const badge = document.createElement("span"); badge.className = "agent-question-badge"; badge.textContent = "Recommended"; name.append(badge); }
    button.append(name);
    if (option.description) { const description = document.createElement("small"); description.textContent = option.description; button.append(description); }
    button.addEventListener("click", () => {
      if (multi) button.setAttribute("aria-pressed", String(button.getAttribute("aria-pressed") !== "true"));
      else void answerAgentQuestion(turnId, card, option.label);
    });
    options.append(button);
  }
  const other = document.createElement("form"); other.className = "agent-question-other";
  const input = document.createElement("input"); input.type = "text"; input.maxLength = 2000; input.placeholder = multi ? "Add a note (optional)…" : "Or type your own answer…";
  const submit = document.createElement("button"); submit.type = "submit"; submit.textContent = multi ? "Submit" : "Send";
  other.append(input, submit);
  other.addEventListener("submit", event => {
    event.preventDefault();
    const chosen = multi ? $$(".agent-question-option[aria-pressed=\"true\"]", card).map(button => button.dataset.label) : [];
    const answer = [...chosen, input.value.trim()].filter(Boolean).join(", ");
    if (answer) void answerAgentQuestion(turnId, card, answer);
    else $(".agent-question-status", card).textContent = multi ? "Choose at least one option or type an answer" : "Type an answer or choose an option";
  });
  const note = document.createElement("small"); note.className = "agent-question-status"; note.textContent = "The agent is waiting for your answer";
  card.append(label, title, options, other, note);
  return card;
}

function closeAgentQuestion(questionId, text, outcome, note = "The question expired without an answer") {
  const card = $$(".agent-question", $("#agent-feed")).find(item => item.dataset.questionId === questionId);
  if (!card) return;
  card.classList.add(outcome);
  for (const control of $$("button, input", card)) control.disabled = true;
  // A label may itself contain ", "; only split multi-select answers that match no single label.
  const labels = $$(".agent-question-option", card).map(button => button.dataset.label);
  const chosen = labels.includes(text) ? [text] : text.split(", ");
  for (const button of $$(".agent-question-option", card)) button.classList.toggle("chosen", outcome === "answered" && chosen.includes(button.dataset.label));
  $(".agent-question-status", card).textContent = outcome === "answered" ? `You answered: ${text}` : note;
}

async function answerAgentQuestion(turnId, card, answer) {
  if (card.classList.contains("answered") || card.classList.contains("sending")) return;
  card.classList.add("sending");
  for (const control of $$("button, input", card)) control.disabled = true;
  $(".agent-question-status", card).textContent = "Sending your answer…";
  try {
    await api("/api/agent/answer", { method: "POST", body: { session_id: state.agent.sessionId, turn_id: turnId, question_id: card.dataset.questionId, answer } });
    closeAgentQuestion(card.dataset.questionId, answer, "answered");
    renderAgentTurnStatus(turnId, "Agent working…");
  } catch (error) {
    for (const control of $$("button, input", card)) control.disabled = false;
    $(".agent-question-status", card).textContent = error.message;
    showError(error);
  } finally {
    card.classList.remove("sending");
  }
}

function appendAgentEvents(events, turnId = state.agent.activeTurnId || "unknown-turn") {
  if (!Array.isArray(events) || !events.length) return;
  const list = document.createElement("div"); list.className = "event-list";
  const closes = [];
  for (const event of events) {
    const kind = event.type || event.kind || "status";
    const text = agentEventText(event) || event.message || event.text || kind.replaceAll("_", " ");
    if (kind === "answer") { renderAgentAnswer(turnId, text); continue; }
    if (kind === "ask_user") { list.append(renderAgentQuestion(turnId, event.data || {})); continue; }
    if (kind === "user_answer" || kind === "ask_user_timeout") {
      // A card from this same batch is not in the feed yet; close it after appending.
      closes.push([event.data?.question_id, event.data?.text || "", kind === "user_answer" ? "answered" : "expired"]);
      continue;
    }
    const row = document.createElement("div"); row.className = "event-row";
    if (kind === "tool_call" || kind === "tool_result") row.classList.add("tool");
    if (kind === "error") row.classList.add("error");
    if (kind === "tool_result" && text.length > 180) {
      const repeatedByAnswer = $$(".message.agent .message-body", $("#agent-feed")).some(body => (body._markdown ?? body.textContent) === text);
      if (repeatedByAnswer) {
        const compact = document.createElement("span"); compact.className = "tool-result-summary"; compact.textContent = "Tool output is included once in the final answer below."; row.append(compact); list.append(row); continue;
      }
      const details = document.createElement("details"); details.className = "tool-details";
      const summary = document.createElement("summary");
      const toolName = event.data && typeof event.data === "object" ? event.data.tool || event.data.name : "";
      summary.textContent = `${toolName ? `${toolName} · ` : ""}Tool output · ${text.length.toLocaleString()} characters`;
      const raw = document.createElement("pre"); raw.textContent = text; details.append(summary, raw); row.append(details);
      details._rawText = text;
    } else row.textContent = text;
    list.append(row);
  }
  if (list.childElementCount) { $("#agent-feed").append(list); list.scrollIntoView({ block: "nearest" }); }
  for (const [questionId, text, outcome] of closes) closeAgentQuestion(questionId, text, outcome);
}

function normalizeAgentEvents(events) {
  return (Array.isArray(events) ? events : []).map((event, index) => ({
    ...event,
    seq: Number.isFinite(Number(event.seq)) ? Number(event.seq) : index,
  }));
}

function isAgentTerminal(status) {
  return ["succeeded", "failed", "cancelled"].includes(status);
}

function createAgentTurnId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// The active turn is remembered per tab so a reload can return to a question
// that is still waiting for an answer instead of orphaning the turn.
const AGENT_TURN_KEY = "seeknal-studio-agent-turn";

function rememberAgentTurn(sessionId, turnId) {
  try { sessionStorage.setItem(AGENT_TURN_KEY, JSON.stringify({ sessionId, turnId })); } catch { /* storage unavailable */ }
}

function forgetAgentTurn() {
  try { sessionStorage.removeItem(AGENT_TURN_KEY); } catch { /* storage unavailable */ }
}

async function resumeAgentTurn() {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(AGENT_TURN_KEY) || "null"); } catch { saved = null; }
  if (!saved || typeof saved.sessionId !== "string" || typeof saved.turnId !== "string" || state.agent.busy) return;
  let session;
  try {
    session = requireAgentTurn(await api(`/api/agent/sessions/${encodeURIComponent(saved.sessionId)}?turn_id=${encodeURIComponent(saved.turnId)}`), saved.turnId);
  } catch {
    forgetAgentTurn(); return;
  }
  if (isAgentTerminal(session.status) || state.agent.busy) { forgetAgentTurn(); return; }
  const request = ++state.agent.request;
  state.agent.sessionId = saved.sessionId; state.agent.activeTurnId = saved.turnId;
  state.agent.busy = true; state.agent.cancelled = false; state.agent.cancelPending = false; state.agent.eventSeq = -1; state.agent.seenEvents = new Set();
  renderAgentStatus();
  appendAgentMessage("user", "(Reconnected to the agent turn that was running before this page reloaded)");
  renderAgentTurnStatus(saved.turnId, session.pending_question ? "Waiting for your answer" : "Agent working…");
  setStatus("Reconnected to the running agent turn");
  await pollAgentSession(request, saved.sessionId, saved.turnId);
}

function requireAgentTurn(session, turnId) {
  if (!session || session.turn_id !== turnId) throw new Error("Agent turn identity mismatch");
  return session;
}

function takeNewAgentEvents(events, turnId) {
  return normalizeAgentEvents(events)
    .sort((a, b) => a.seq - b.seq)
    .filter(event => {
      const key = `${turnId}:${event.seq}`;
      if (state.agent.seenEvents.has(key)) return false;
      state.agent.seenEvents.add(key); return true;
    });
}

async function finishAgentSession(session) {
  const turnId = session.turn_id || state.agent.activeTurnId || "unknown-turn";
  const normalizedEvents = normalizeAgentEvents(session.events);
  const events = takeNewAgentEvents(normalizedEvents, turnId);
  if (events.length) {
    appendAgentEvents(events, turnId);
    state.agent.eventSeq = Math.max(state.agent.eventSeq, ...events.map(event => event.seq));
  }
  if (state.agent.completedTurns.has(turnId)) return;
  state.agent.completedTurns.add(turnId);
  const unanswered = session.status === "cancelled" ? "The turn was cancelled before you answered" : "The turn ended before you answered";
  for (const card of $$(".agent-question:not(.answered):not(.expired)", $("#agent-feed"))) closeAgentQuestion(card.dataset.questionId, "", "expired", unanswered);
  forgetAgentTurn();
  state.agent.busy = false;
  state.agent.cancelPending = false;
  state.agent.activeTurnId = null;
  if (session.status === "succeeded") {
    state.agent.cancelled = false;
    if (session.answer) renderAgentAnswer(turnId, session.answer);
    setStatus("Agent finished; review any generated drafts", "success");
    renderAgentTurnStatus(turnId, "Turn complete · refreshing draft state");
    await refreshWorkspaceState();
    renderAgentTurnStatus(turnId, state.drafts.length ? "Turn complete · draft ready for explicit review" : "Turn complete · no new draft reported", state.drafts.length);
    if (state.sqlDraftContext) {
      const created = state.drafts.find(draft => !state.sqlDraftContext.before.has(draft.path));
      const requestedContext = state.sqlDraftContext;
      state.sqlDraftContext = null;
      if (created) {
        state.draftSnapshots.set(created.path, { path: requestedContext.path, content: requestedContext.content });
        await openDraftReview();
        await selectDraft(created);
      } else appendAgentMessage("agent", "The turn completed without a matching new draft. No source file was changed.");
    }
  } else if (session.status === "cancelled") {
    state.agent.cancelled = true;
    renderAgentTurnStatus(turnId, "Turn cancelled");
    setStatus("Agent turn cancelled");
  } else {
    state.agent.cancelled = false;
    appendAgentMessage("agent", session.error || "Agent turn failed.", "error");
    renderAgentTurnStatus(turnId, "Turn failed");
    setStatus("Agent request failed", "error");
  }
  renderAgentStatus();
}

// A dropped connection or a 5xx from the backend (or the SSH gateway) is retried
// with backoff before the turn is reported as lost; anything else fails at once.
const AGENT_POLL_RETRIES = 6;
const AGENT_POLL_TIMEOUT_MS = 20000;
function isTransientPollError(error) {
  if (error.status) return error.status >= 500 || error.status === 408 || error.status === 429;
  return error instanceof TypeError || error.name === "TimeoutError";
}

async function pollAgentSession(request, sessionId, turnId, failures = 0) {
  if (request !== state.agent.request || state.agent.cancelled || state.agent.cancelPending) return;
  try {
    const session = requireAgentTurn(
      await api(`/api/agent/sessions/${encodeURIComponent(sessionId)}?turn_id=${encodeURIComponent(turnId)}`, { signal: AbortSignal.timeout(AGENT_POLL_TIMEOUT_MS) }),
      turnId,
    );
    if (request !== state.agent.request || state.agent.cancelled || state.agent.cancelPending) return;
    const events = takeNewAgentEvents(session.events, turnId);
    if (events.length) {
      appendAgentEvents(events, turnId);
      state.agent.eventSeq = Math.max(state.agent.eventSeq, ...events.map(event => event.seq));
    }
    if (!isAgentTerminal(session.status)) {
      renderAgentTurnStatus(turnId, session.pending_question ? "Waiting for your answer" : "Agent working…");
      state.agent.pollTimer = setTimeout(() => pollAgentSession(request, sessionId, turnId), 500);
      return;
    }
    await finishAgentSession(session);
  } catch (error) {
    // A pending cancel owns the turn now: it resumes polling itself if the cancel fails.
    if (request !== state.agent.request || state.agent.cancelled || state.agent.cancelPending) return;
    if (failures < AGENT_POLL_RETRIES && isTransientPollError(error)) {
      renderAgentTurnStatus(turnId, "Connection lost · retrying…");
      state.agent.pollTimer = setTimeout(() => pollAgentSession(request, sessionId, turnId, failures + 1), Math.min(500 * 2 ** failures, 8000));
      return;
    }
    renderAgentTurnStatus(turnId, "Connection lost");
    state.agent.busy = false;
    state.agent.activeTurnId = null;
    appendAgentMessage("agent", error.message, "error");
    setStatus("Agent session polling failed", "error");
    renderAgentStatus();
  }
}

async function askAgent(question) {
  if (state.agent.resuming) await state.agent.resuming;
  if (!agentIsReady() || state.agent.busy) return;
  if (state.agent.mode === "external" && !state.agentProjectAcknowledged) {
    const accepted = window.confirm("Gateway health does not verify which project it controls. Confirm the configured gateway is scoped to this disposable/local project before sending this prompt.");
    if (!accepted) return;
    state.agentProjectAcknowledged = true;
  }
  const request = ++state.agent.request;
  const turnId = createAgentTurnId();
  state.agent.activeTurnId = turnId;
  rememberAgentTurn(state.agent.sessionId, turnId);
  state.agent.busy = true; state.agent.cancelled = false; state.agent.cancelPending = false; state.agent.eventSeq = -1; state.agent.seenEvents = new Set(); renderAgentStatus(); appendAgentMessage("user", question); renderAgentTurnStatus(turnId, "Agent working…");
  setStatus("Agent is working…");
  try {
    const payload = requireAgentTurn(
      await api("/api/agent/ask", { method: "POST", body: { question, session_id: state.agent.sessionId, turn_id: turnId } }),
      turnId,
    );
    if (request !== state.agent.request || state.agent.cancelled) return;
    if (payload.answer || isAgentTerminal(payload.status)) {
      const terminalPayload = payload.answer && !payload.status ? { ...payload, status: "succeeded" } : payload;
      await finishAgentSession(terminalPayload);
      return;
    }
    state.agent.sessionId = payload.session_id || state.agent.sessionId;
    const initialEvents = takeNewAgentEvents(payload.events, turnId);
    appendAgentEvents(initialEvents, turnId);
    state.agent.eventSeq = Math.max(-1, ...initialEvents.map(event => event.seq));
    await pollAgentSession(request, state.agent.sessionId, turnId);
  } catch (error) {
    if (request !== state.agent.request || state.agent.cancelled) return;
    try {
      const session = requireAgentTurn(
        await api(`/api/agent/sessions/${encodeURIComponent(state.agent.sessionId)}?turn_id=${encodeURIComponent(turnId)}`),
        turnId,
      );
      if (request !== state.agent.request || state.agent.cancelled) return;
      if (isAgentTerminal(session.status)) await finishAgentSession(session);
      else {
        setStatus("Agent turn accepted; resumed authoritative polling");
        await pollAgentSession(request, state.agent.sessionId, turnId);
      }
      return;
    } catch {
      // The POST was not authoritatively accepted or the service is unreachable.
    }
    forgetAgentTurn();
    state.agent.busy = false;
    state.agent.cancelPending = false;
    state.agent.activeTurnId = null;
    appendAgentMessage("agent", error.message, "error");
    setStatus("Agent request failed or acceptance is unknown", "error");
    renderAgentStatus();
  }
}

async function cancelAgent() {
  if (!state.agent.busy || state.agent.cancelPending) return;
  const request = state.agent.request;
  const sessionId = state.agent.sessionId;
  const turnId = state.agent.activeTurnId;
  if (!turnId) return;
  state.agent.cancelPending = true;
  clearTimeout(state.agent.pollTimer);
  state.agent.pollTimer = null;
  renderAgentStatus();
  try {
    const session = requireAgentTurn(
      await api("/api/agent/cancel", { method: "POST", body: { session_id: sessionId, turn_id: turnId } }),
      turnId,
    );
    if (request !== state.agent.request) return;
    if (session?.status === "cancelled") {
      state.agent.cancelled = true;
      await finishAgentSession(session);
      state.agent.request += 1;
      return;
    }
    state.agent.cancelPending = false;
    state.agent.cancelled = false;
    setStatus("Cancellation was not confirmed; monitoring the active turn", "error");
    renderAgentStatus();
    await pollAgentSession(request, sessionId, turnId);
  } catch (error) {
    if (request !== state.agent.request) return;
    state.agent.cancelPending = false;
    state.agent.cancelled = false;
    appendAgentMessage("agent", `Cancellation was not confirmed: ${error.message}. The active turn is still being monitored.`, "error");
    setStatus("Cancellation failed; agent state remains active or unknown", "error");
    renderAgentStatus();
    await pollAgentSession(request, sessionId, turnId);
  }
}

async function configureAgent() {
  const button = $("#agent-configure");
  clearTimeout(state.agent.readinessTimer);
  state.agent.readinessGeneration += 1;
  button.disabled = true;
  $("#agent-config-status").textContent = "Starting the owned runtime and probing the configured model…";
  setStatus("Verifying agent provider and project identity…");
  try {
    const status = await api("/api/agent/config", {
      method: "POST",
      body: {
        provider: "openai",
        model: "deepseek-v4.1-flash:netra",
        base_url: "https://ai.sumopod.com/v1",
        key_path: $("#agent-key-path").value.trim(),
      },
    });
    state.agent = { ...state.agent, ...normalizeAgentStatus(status) };
    renderAgentStatus();
    if (agentIsReady()) setStatus("Project-bound draft agent ready", "success");
    else if (agentIsConfiguring()) {
      setStatus("Agent startup continues in the background");
      startAgentReadinessWatch();
    } else throw new Error(state.agent.error || "Agent readiness, project identity, or draft-only policy verification failed");
  } catch (error) {
    state.agent = { ...state.agent, ready: false, healthy: false, error: error.message };
    renderAgentStatus();
    setStatus("Agent setup failed", "error");
  } finally {
    button.disabled = false;
  }
}

async function loadDrafts() {
  try {
    const payload = await api("/api/drafts");
    state.drafts = Array.isArray(payload.drafts) ? payload.drafts : [];
    state.draftErrors = Array.isArray(payload.errors) ? payload.errors : [];
  } catch (error) {
    state.drafts = [];
    state.draftErrors = [{ path: "Draft listing unavailable", error: error.message }];
  }
  $("#draft-count").textContent = state.drafts.length + state.draftErrors.length;
  renderDraftExplorer();
  updateEditorActions();
}

function renderDraftList() {
  const root = $("#draft-list"); root.replaceChildren();
  if (!state.drafts.length && !state.draftErrors.length) {
    const empty = document.createElement("div"); empty.className = "empty-state"; empty.textContent = "No agent drafts in .seeknal/drafts."; root.append(empty);
    $("#draft-diff").textContent = "No changes awaiting review.";
    $("#draft-review-status").textContent = "No changes awaiting review";
    $("#validate-draft").disabled = true; $("#apply-draft").disabled = true;
    return;
  }
  for (const draft of state.drafts) {
    const button = document.createElement("button"); button.type = "button"; button.textContent = draft.path; button.classList.toggle("active", draft.path === state.currentDraft?.path);
    button.addEventListener("click", () => selectDraft(draft)); root.append(button);
  }
  for (const error of state.draftErrors) {
    const button = document.createElement("button"); button.type = "button"; button.className = "draft-error";
    button.textContent = `${error.path || "Invalid draft"} · error`;
    button.addEventListener("click", () => selectDraftError(error)); root.append(button);
  }
}

function selectDraftError(error) {
  state.currentDraft = null; state.currentDiff = null; state.draftValidationJobId = null;
  renderDraftList();
  $("#draft-diff").textContent = `${error.path || "Invalid draft"}\n\n${error.error || "Seeknal could not resolve this draft."}`;
  $("#draft-review-status").textContent = "Invalid draft · fix the file before review";
  $("#validate-draft").disabled = true; $("#apply-draft").disabled = true;
  updateEditorActions();
}

async function selectDraft(draft, { review = false, groupId = state.editorGroups.activeGroup } = {}) {
  if (draft.path !== state.selectedFile && !canLeaveBuffer()) return;
  state.currentDraft = draft; state.currentDiff = null; state.draftValidationJobId = null;
  renderDraftList(); $("#draft-diff").textContent = "Loading real file diff…"; $("#validate-draft").disabled = true; $("#apply-draft").disabled = true;
  try {
    const diff = await api(`/api/diff?path=${encodeURIComponent(draft.path)}`);
    if (state.currentDraft?.path !== draft.path) return;
    state.currentDiff = diff;
    $("#draft-diff").textContent = diff.diff || "No textual change.";
    $("#draft-review-status").textContent = `${draft.path} → ${diff.destination || "project"}`;
    $("#validate-draft").disabled = false;
    await openFile(draft.path, { guard: false });
    setMainView("yaml", groupId);
    if (!review && $("#draft-dialog").open) $("#draft-dialog").close();
    updateEditorActions();
  } catch (error) { $("#draft-diff").textContent = error.message; $("#draft-review-status").textContent = "Could not load diff"; }
}

async function openDraftReview() {
  await loadDrafts(); state.currentDraft = null; state.currentDiff = null; state.draftValidationJobId = null; renderDraftList(); $("#draft-dialog").showModal();
  if (state.drafts[0]) await selectDraft(state.drafts[0], { review: true });
  else if (state.draftErrors[0]) selectDraftError(state.draftErrors[0]);
}

async function validateCurrentDraft() {
  await dryRunEditor();
}

async function applyCurrentDraft() {
  await applyPreparedEditor();
}

function normalizePreparedEditor(payload) {
  const snapshot = payload?.snapshot || payload?.diff || null;
  if (!snapshot?.path || !snapshot?.content_hash) return null;
  return {
    path: snapshot.path,
    contentHash: snapshot.content_hash,
    destination: snapshot.destination || payload.diff?.destination || state.selectedFile,
    destinationHash: snapshot.destination_hash ?? payload.diff?.destination_hash ?? null,
    sourcePath: snapshot.source_path || payload.source_path || state.selectedFile,
    mode: snapshot.mode || payload.mode || editorKind(state.selectedFile),
  };
}

async function prepareEditorSnapshot() {
  const flow = state.editorFlow;
  const request = nextRequestGeneration(state.editorPrepareGeneration);
  const path = state.selectedFile;
  const buffer = $("#code-editor").value;
  const payload = await api("/api/editor/prepare", { method: "POST", body: { path, content: buffer, original_content: state.originalContent } });
  if (request !== state.editorPrepareGeneration.value || path !== state.selectedFile) return null;
  const currentBuffer = $("#code-editor").value;
  if (buffer !== currentBuffer) {
    const rebased = rebaseDirtyDraftAfterPrepare(flow.kind, buffer, currentBuffer);
    if (rebased) {
      state.originalContent = rebased.originalContent;
      state.dirty = rebased.dirty;
      state.editorFlow = editorFlowTransition(state.editorFlow, "edit", { buffer: rebased.buffer, dirty: true });
      updateEditorActions();
    }
    return null;
  }
  const prepared = normalizePreparedEditor(payload);
  if (!prepared) {
    if (payload.unchanged === true && flow.kind === "active") setStatus("No pipeline changes to dry run");
    else throw new Error("The backend did not return a prepared editor snapshot");
    return null;
  }
  if (flow.kind === "draft" && state.dirty) {
    state.originalContent = buffer;
    state.dirty = false;
  }
  state.currentDiff = payload.diff || { path: prepared.path, destination: prepared.destination, content_hash: prepared.contentHash, destination_hash: prepared.destinationHash };
  return { ...prepared, buffer };
}

async function dryRunEditor() {
  const flow = state.editorFlow;
  if (!state.selectedFile || !["active", "draft"].includes(flow.kind) || flow.busy || (state.activeJob && !isTerminal(state.activeJob))) return;
  flow.busy = true;
  flow.phase = "validating";
  flow.approval = null;
  updateEditorActions();
  try {
    const prepared = await prepareEditorSnapshot();
    if (!prepared) { state.editorFlow.phase = state.dirty ? "dirty" : "saved"; return; }
    flow.approval = prepared;
    const job = await startJob("validate-draft", null, { path: prepared.path, content_hash: prepared.contentHash });
    if (!job) { invalidateEditorApproval("failed"); return; }
    state.draftValidationJobId = job.id;
    state.editorValidation = { jobId: job.id, sourcePath: state.selectedFile, groupId: state.editorGroups.owners.yaml || state.editorGroups.activeGroup, preparedPath: prepared.path, contentHash: prepared.contentHash, buffer: prepared.buffer };
    setStatus(`Dry run started for ${fileName(prepared.path)}`);
  } catch (error) {
    invalidateEditorApproval("failed");
    showError(error);
  } finally {
    state.editorFlow.busy = false;
    updateEditorActions();
  }
}

async function applyPreparedEditor() {
  const flow = state.editorFlow;
  const approval = flow.approval;
  const bufferAtApply = $("#code-editor").value;
  if (flow.busy || flow.phase !== "passed" || !approval || approval.buffer !== bufferAtApply) {
    showError("Dry run this exact editor buffer before applying it.");
    return;
  }
  state.editorFlow = editorFlowTransition(flow, "apply-start");
  state.pendingDraftApply = { preparedPath: approval.path, contentHash: approval.contentHash, destination: approval.destination, destinationHash: approval.destinationHash, sourcePath: state.selectedFile, groupId: state.editorGroups.owners.yaml || state.editorGroups.activeGroup, bufferAtApply };
  updateEditorActions();
  const job = await startJob("apply-draft", null, { path: approval.path, content_hash: approval.contentHash, destination_hash: approval.destinationHash, confirmed: true }, { snapshotAuthorized: true });
  if (!job) {
    state.pendingDraftApply = null;
    state.editorFlow = editorFlowTransition(state.editorFlow, "failure");
    updateEditorActions();
  }
}

async function runEditorFlowAction() {
  const choice = editorFlowAction(state.editorFlow, state.dirty);
  if (choice.action === "save") await saveCurrentFile();
  else if (choice.action === "dry-run") await dryRunEditor();
  else if (choice.action === "apply") await applyPreparedEditor();
}

function currentTransformContext() {
  const path = state.selectedFile || "";
  const content = $("#code-editor").value;
  const node = state.graph.nodes.find(item => item.id === state.selectedNode || nodePath(item) === path);
  const eligible = /\.ya?ml$/i.test(path) && (node?.kind === "transform" || /^\s*kind\s*:\s*transform\s*$/im.test(content));
  const inputIds = state.graph.edges.filter(edge => edge.target === node?.id).map(edge => edge.source);
  const inputs = inputIds.map(id => state.graph.nodes.find(item => item.id === id)).filter(Boolean).map(item => ({ id: item.id, name: nodeName(item), kind: item.kind }));
  const schema = Array.isArray(node?.columns) ? node.columns : [];
  const size = new TextEncoder().encode(content).length;
  return { eligible, path, content, node, inputs, schema, size };
}

function updateEditorActions() {
  const context = currentTransformContext();
  const selected = Boolean(state.selectedFile);
  $("#draft-editor").disabled = !state.project || Boolean(state.activeJob && !isTerminal(state.activeJob));
  $("#draft-menu-trigger").disabled = !state.project;
  $("#draft-sql-ai").disabled = !context.eligible || !agentIsReady() || state.agent.busy;
  $("#send-sql-repl").disabled = !context.eligible;
  const flow = state.editorFlow;
  const choice = editorFlowAction(flow, state.dirty);
  const running = Boolean(state.activeJob && !isTerminal(state.activeJob));
  const footer = $("#editor-flow");
  const shouldShow = selected && (flow.kind === "draft" || state.dirty || ["validating", "passed", "applying", "failed"].includes(flow.phase));
  footer.hidden = !shouldShow;
  footer.dataset.phase = flow.phase;
  const labels = {
    validating: ["Dry run in progress", "Validation is running against the prepared snapshot."],
    passed: ["Dry run passed", flow.validationMessage || (flow.kind === "draft" ? "This exact draft is ready to apply." : "This exact buffer is ready to save.")],
    applying: [flow.kind === "draft" ? "Applying validated draft" : "Saving validated changes", "The exact validated snapshot is being committed."],
    failed: ["Dry run failed", flow.validationMessage || "Edits are preserved. Inspect Logs, then try again."],
  };
  const fallback = flow.kind === "draft"
    ? ["Draft · not applied", state.dirty ? "Unsaved draft edits will be prepared before validation." : "Validate this draft before applying it."]
    : flow.kind === "plain"
      ? ["Unsaved changes", "This non-pipeline file can be saved directly."]
      : ["Unsaved changes", "Dry run a snapshot before the active pipeline file can change."];
  const [label, detail] = labels[flow.phase] || fallback;
  $("#editor-flow-label").textContent = label;
  $("#editor-flow-detail").textContent = detail;
  $("#save-button").textContent = choice.label;
  $("#save-button").disabled = !choice.action || running || flow.busy;
  $("#validate-draft").disabled = choice.action !== "dry-run" || running;
  $("#apply-draft").disabled = choice.action !== "apply" || running;
  $("#menu-save").innerHTML = `${choice.label} <kbd>⌘S</kbd>`;
  $("#menu-save").disabled = !choice.action || running || flow.busy;
  $("#menu-dry-run").disabled = choice.action !== "dry-run" || running;
  $("#menu-apply").disabled = choice.action !== "apply" || running;
  $("#menu-apply").textContent = flow.kind === "draft" ? "Apply validated draft" : "Save validated changes";
  renderEditorGroups();
}

function cliDisplayValue(value) {
  const text = String(value || "");
  if (/^[A-Za-z0-9_./:-]+$/.test(text)) return text;
  return `'${text.replaceAll("'", `'"'"'`)}'`;
}

function selectedDraftOption() {
  return state.draftOptions.find(option => option.value === $("#new-draft-type").value) || null;
}

function updateNewDraftForm() {
  const option = selectedDraftOption();
  const python = $("#new-draft-python");
  const deps = $("#new-draft-deps");
  python.disabled = !option?.python_supported;
  if (python.disabled) python.checked = false;
  deps.disabled = !python.checked || python.disabled;
  if (deps.disabled) deps.value = "";
  const nodeType = $("#new-draft-type").value;
  const name = $("#new-draft-name").value.trim();
  const description = $("#new-draft-description").value.trim();
  const command = ["seeknal", "draft"];
  if (nodeType) command.push(cliDisplayValue(nodeType));
  if (name) command.push(cliDisplayValue(name));
  if (description) command.push(`--description=${cliDisplayValue(description)}`);
  if (python.checked) command.push("--python");
  if (python.checked && deps.value.trim()) command.push(`--deps=${cliDisplayValue(deps.value.trim())}`);
  $("#new-draft-command").textContent = command.join(" ");
  $("#new-draft-submit").disabled = !option || !name || Boolean(state.activeJob && !isTerminal(state.activeJob));
  $("#new-draft-note").textContent = option?.python_supported
    ? "YAML is the default. Python is available for this node type. Existing drafts are never overwritten."
    : "This node type supports YAML scaffolding only. Existing drafts are never overwritten.";
}

async function openNewDraftDialog() {
  closeMenus();
  if (!state.project) return showError("Load a Seeknal project before creating a draft");
  const dialog = $("#new-draft-dialog");
  dialog.showModal();
  $("#new-draft-submit").disabled = true;
  $("#new-draft-note").textContent = "Loading node types from this Seeknal runtime…";
  try {
    const payload = await api("/api/drafts/options");
    state.draftOptions = Array.isArray(payload.node_types) ? payload.node_types : [];
    const select = $("#new-draft-type"); select.replaceChildren();
    for (const option of state.draftOptions) {
      const item = document.createElement("option"); item.value = option.value; item.textContent = displayKind(option.normalized || option.value); select.append(item);
    }
    if (!state.draftOptions.length) throw new Error("This Seeknal runtime reported no draftable node types");
    updateNewDraftForm();
    $("#new-draft-name").focus();
  } catch (error) {
    state.draftOptions = [];
    $("#new-draft-type").replaceChildren();
    $("#new-draft-note").textContent = error.message;
    showError(error);
  }
}

async function submitCliDraft() {
  const form = $("#new-draft-form");
  if (!form.reportValidity()) return;
  if (state.dirty) {
    setStatus("Save or discard the modified editor buffer before creating a CLI draft", "error");
    $("#save-button").focus();
    return;
  }
  const option = selectedDraftOption();
  if (!option) return showError("Choose a node type reported by this Seeknal runtime");
  const extra = {
    node_type: option.value,
    name: $("#new-draft-name").value.trim(),
    description: $("#new-draft-description").value.trim(),
    python: $("#new-draft-python").checked,
    deps: $("#new-draft-deps").value.trim(),
  };
  $("#new-draft-submit").disabled = true;
  const draftContext = captureDraftContext(state.editorGroups, state.selectedFile);
  const job = await startJob("draft", null, extra);
  if (job) {
    state.draftJobContexts.set(job.id, draftContext);
    $("#new-draft-dialog").close();
    setStatus("Seeknal draft command started; follow the real CLI logs");
  } else updateNewDraftForm();
}

async function openGeneratedDraft(job) {
  const context = state.draftJobContexts.get(job.id) || { groupId: state.editorGroups.activeGroup, selectedFile: state.selectedFile };
  state.draftJobContexts.delete(job.id);
  await refreshWorkspaceState();
  const draft = state.drafts.find(item => item.path === job.draft_path);
  if (draft && state.selectedFile === context.selectedFile && (!state.dirty || canLeaveBuffer())) {
    await selectDraft(draft, { groupId: context.groupId });
    setStatus(`Generated ${job.draft_path}; edit it here, then Dry run before Apply`, "success");
  } else if (draft) setStatus(`Generated ${job.draft_path}; open it from Drafts when ready`, "success");
}

async function createDraftFromEditor() {
  const context = currentTransformContext();
  if (!context.eligible) return showError("Select a transform YAML file before creating a draft");
  if (context.size > 300 * 1024) return showError("Editor buffer exceeds the 300 KB draft limit");
  try {
    setStatus("Creating reviewable draft from editor…");
    const payload = await api("/api/drafts/from-editor", { method: "POST", body: { path: context.path, content: context.content } });
    await loadDrafts();
    const draft = payload.draft || state.drafts.find(item => item.path === payload.path);
    if (draft?.path) state.draftSnapshots.set(draft.path, { path: context.path, content: context.content });
    await openDraftReview();
    if (draft) await selectDraft(draft);
    setStatus("Draft created; source file was not saved or run", "success");
  } catch (error) { showError(error); }
}

function openSqlDraftDialog() {
  const context = currentTransformContext();
  $("#sql-context-file").textContent = context.path || "No file selected";
  $("#sql-context-node").textContent = context.node ? `${displayKind(context.node.kind)} / ${nodeName(context.node)}` : "No graph transform selected";
  $("#sql-context-inputs").textContent = context.inputs.length || context.schema.length ? `${context.inputs.map(item => item.name).join(", ") || "No graph inputs"}; columns: ${context.schema.join(", ") || "not materialized"}` : "No input/schema metadata available";
  $("#sql-context-buffer").textContent = `${context.content.split("\n").length} lines · ${context.size.toLocaleString()} bytes · ${state.dirty ? "modified buffer" : "saved buffer"}`;
  const reason = !context.eligible ? "Select a transform .yaml or .yml file. Other YAML kinds are not eligible." : context.size > MAX_AI_BUFFER_BYTES ? "This buffer exceeds the 256 KB agent-context limit. Manual drafting remains available up to 300 KB." : !agentIsReady() ? "The project-bound draft agent must be verified before sending." : "Eligible. Review the context, write a request, then choose Send.";
  $("#sql-ai-eligibility").textContent = reason;
  $("#sql-ai-send").disabled = !context.eligible || context.size > MAX_AI_BUFFER_BYTES || !agentIsReady();
  $("#sql-ai-dialog").showModal();
  $("#sql-ai-request").focus();
}

async function sendSqlDraftRequest() {
  const request = $("#sql-ai-request").value.trim();
  const context = currentTransformContext();
  if (!request || !context.eligible || context.size > MAX_AI_BUFFER_BYTES) return;
  const boundedContext = JSON.stringify({ path: context.path, selected_transform: context.node ? { id: context.node.id, name: nodeName(context.node), kind: context.node.kind } : null, inputs: context.inputs, observed_output_columns: context.schema });
  const prompt = [
    "Create or update exactly one reviewable Seeknal transform draft for the editor context below.",
    "Use .seeknal/drafts and keep the SAME transform kind and name. Preserve every non-SQL metadata field. Change only the SQL required by the user request.",
    "Never save the source file, apply a draft, validate, run, or execute SQL. Do not include secrets. If the request cannot be satisfied safely, explain and create nothing.",
    `User request: ${request}`,
    `Context metadata: ${boundedContext}`,
    `Full current editor buffer (${context.size} bytes):\n---BEGIN YAML---\n${context.content}\n---END YAML---`,
  ].join("\n\n");
  state.sqlDraftContext = { before: new Set(state.drafts.map(draft => draft.path)), path: context.path, content: context.content };
  $("#sql-ai-dialog").close();
  $("#sql-ai-request").value = "";
  await askAgent(prompt);
}

function closeMenus() {
  for (const trigger of $$(".menu-trigger")) trigger.setAttribute("aria-expanded", "false");
  for (const menu of $$(".menu-popover")) menu.hidden = true;
  $("#menu-recent-projects")?.setAttribute("aria-expanded", "false");
  if ($("#recent-project-menu")) $("#recent-project-menu").hidden = true;
  $("#draft-action-menu").hidden = true;
  $("#draft-menu-trigger").setAttribute("aria-expanded", "false");
  if ($("#workspace-menu")) closeWorkspaceMenu();
}

function toggleMenu(trigger) {
  const menu = trigger.nextElementSibling;
  const open = menu.hidden;
  closeMenus();
  menu.hidden = !open;
  trigger.setAttribute("aria-expanded", String(open));
  if (open) $("button:not([disabled])", menu)?.focus();
}

function normalizeWorkspaces(payload = {}) {
  const entries = Array.isArray(payload.entries) ? payload.entries : [];
  const current = payload.current || entries.find(item => item.current) || null;
  return {
    current,
    entries,
    managed: payload.managed === true,
    can_switch: payload.can_switch === true,
    instance: payload.instance || null,
    notice: payload.notice || null,
    noticeShown: state.workspaces.noticeShown || null,
    loading: false,
    editingId: state.workspaces.editingId,
    forgetId: state.workspaces.forgetId,
    formSnapshot: state.workspaces.formSnapshot,
    openPending: state.workspaces.openPending,
    confirmTarget: state.workspaces.confirmTarget,
    savePending: state.workspaces.savePending,
    forgetPending: state.workspaces.forgetPending,
    unloadAuthorized: state.workspaces.unloadAuthorized,
  };
}

function workspaceIsCurrent(workspace) {
  if (!workspace) return false;
  if (workspace.id && state.workspaces.current?.id) return workspace.id === state.workspaces.current.id;
  if (workspace.kind === "ssh" || state.workspaces.current?.kind === "ssh") {
    const current = state.workspaces.current;
    return Boolean(current) && workspace.kind === current.kind && workspace.host === current.host && (workspace.ssh_port || null) === (current.ssh_port || null) && workspace.path === current.path;
  }
  return workspace.path === state.workspaces.current?.path || workspace.path === state.project?.project_path;
}

function workspaceOpenReason(workspace) {
  if (workspaceIsCurrent(workspace)) return "Current workspace";
  if (state.workspaces.openPending) return `Switching to ${state.workspaces.openPending.name}…`;
  if (state.workspaces.savePending) return "A workspace registration is being saved…";
  if (state.workspaces.forgetPending) return "A workspace registration is being forgotten…";
  if (workspace.available === false) return workspace.reason || "Project directory is unavailable";
  if (!state.workspaces.managed || !state.workspaces.can_switch) return workspace.reason || "Open requires a managed Studio launcher";
  if (workspace.can_switch === false) return workspace.reason || "This workspace cannot be opened";
  return "";
}

function workspaceLocation(workspace) {
  if (workspace?.kind !== "ssh") return workspace?.path || "Path unavailable";
  return `ssh ${workspace.location || `${workspace.host}:${workspace.path}`}`;
}

function createWorkspaceMenuItem(workspace) {
  const current = workspaceIsCurrent(workspace);
  const unavailable = workspace.available === false;
  const reason = workspaceOpenReason(workspace);
  const button = document.createElement("button");
  button.type = "button"; button.role = "menuitem";
  button.className = `workspace-menu-item${current ? " current" : ""}${unavailable ? " unavailable" : ""}`;
  button.disabled = Boolean(reason);
  button.title = reason || `Open ${workspace.name}`;
  const dot = document.createElement("i"); dot.setAttribute("aria-hidden", "true");
  const copy = document.createElement("span");
  const name = document.createElement("strong"); name.textContent = workspace.name || fileName(workspace.path || "Workspace");
  const path = document.createElement("small"); path.textContent = workspaceLocation(workspace);
  copy.append(name, path);
  const mark = document.createElement("em"); mark.textContent = current ? "Current" : unavailable ? "Unavailable" : "";
  button.append(dot, copy, mark);
  button.addEventListener("click", () => openWorkspace(workspace));
  return button;
}

function renderWorkspaceMenu() {
  const current = state.workspaces.current;
  $("#workspace-menu-label").textContent = current?.name || state.project?.project || "Workspace";
  $("#workspace-menu-mode").textContent = state.workspaces.managed ? "Managed Studio" : "Standalone · Open disabled";
  const root = $("#workspace-menu-list"); root.replaceChildren();
  for (const workspace of state.workspaces.entries) root.append(createWorkspaceMenuItem(workspace));
  if (!root.children.length) {
    const empty = document.createElement("p"); empty.className = "workspace-menu-empty"; empty.textContent = "No saved workspaces yet."; root.append(empty);
  }
  renderRecentProjects();
}

function workspaceStatus(text = "", tone = "") {
  const element = $("#workspace-config-status");
  element.textContent = text; element.className = `workspace-config-status${tone ? ` ${tone}` : ""}`;
}

function workspaceOperationPending() {
  return Boolean(state.workspaces.openPending || state.workspaces.savePending || state.workspaces.forgetPending);
}

function setWorkspaceFormDisabled(disabled) {
  for (const control of $$("input,button,select", $("#workspace-form"))) control.disabled = disabled;
}

function createWorkspaceRow(workspace) {
  const current = workspaceIsCurrent(workspace);
  const unavailable = workspace.available === false;
  const row = document.createElement("div");
  row.className = `workspace-row${current ? " current" : ""}${unavailable ? " unavailable" : ""}`;
  const dot = document.createElement("i"); dot.setAttribute("aria-hidden", "true");
  const copy = document.createElement("div"); copy.className = "workspace-row-copy";
  const heading = document.createElement("div"); heading.className = "workspace-row-title";
  const name = document.createElement("strong"); name.textContent = workspace.name || fileName(workspace.path || "Workspace");
  const badge = document.createElement("span"); badge.className = `workspace-state${current ? " current" : unavailable ? " unavailable" : ""}`; badge.textContent = current ? "Current" : unavailable ? "Unavailable" : "Saved";
  heading.append(name, badge);
  if (workspace.kind === "ssh") { const remote = document.createElement("span"); remote.className = "workspace-state remote"; remote.textContent = "SSH"; heading.append(remote); }
  const path = document.createElement("small"); path.textContent = workspaceLocation(workspace); path.title = workspaceLocation(workspace);
  copy.append(heading, path);
  if (unavailable) { const diagnostic = document.createElement("small"); diagnostic.className = "workspace-diagnostic"; diagnostic.textContent = workspace.reason || "Project directory is unavailable. Edit its saved path."; copy.append(diagnostic); }
  const actions = document.createElement("div"); actions.className = "workspace-row-actions";
  const edit = document.createElement("button"); edit.type = "button"; edit.textContent = unavailable ? "Edit path" : "Edit"; edit.disabled = state.workspaces.savePending; edit.addEventListener("click", () => showWorkspaceForm(workspace));
  const open = document.createElement("button"); open.type = "button"; open.textContent = current ? "Open" : "Open";
  const reason = workspaceOpenReason(workspace); open.disabled = Boolean(reason); open.title = reason; open.addEventListener("click", () => openWorkspace(workspace));
  const forget = document.createElement("button"); forget.type = "button"; forget.textContent = state.workspaces.forgetId === workspace.id ? "Confirm forget" : "Forget";
  forget.disabled = Boolean(state.workspaces.forgetPending || state.workspaces.savePending || state.workspaces.openPending);
  if (state.workspaces.forgetId === workspace.id) forget.classList.add("forget-confirm");
  forget.addEventListener("click", () => requestForgetWorkspace(workspace));
  actions.append(edit, open, forget); row.append(dot, copy, actions);
  if (state.workspaces.forgetId === workspace.id) {
    const note = document.createElement("p"); note.className = "workspace-forget-copy"; note.textContent = "Remove from Studio list; files remain. Select Confirm forget again to remove only this saved registration."; row.append(note);
  }
  return row;
}

function renderWorkspaceList() {
  const root = $("#workspace-list"); root.replaceChildren();
  for (const workspace of state.workspaces.entries) root.append(createWorkspaceRow(workspace));
  if (!root.children.length) { const empty = document.createElement("div"); empty.className = "empty-state"; empty.textContent = "No saved workspaces. Add the current project or another local Seeknal project directory."; root.append(empty); }
}

function renderWorkspaces() {
  renderWorkspaceMenu();
  renderWorkspaceList();
}

async function loadWorkspaces() {
  try {
    state.workspaces = normalizeWorkspaces(await api("/api/workspaces"));
    workspaceStatus(state.workspaces.managed ? "" : "Standalone mode can save and edit entries, but opening another workspace requires a managed Studio launcher.");
    if (state.workspaces.notice && state.workspaces.noticeShown !== state.workspaces.instance) {
      state.workspaces.noticeShown = state.workspaces.instance;
      workspaceStatus(state.workspaces.notice, "error"); setStatus(state.workspaces.notice, "error");
    }
    renderWorkspaces();
    return state.workspaces;
  } catch (error) {
    state.workspaces.loading = false;
    workspaceStatus(`Saved workspaces unavailable: ${error.message}`, "error");
    showError(error);
    return null;
  }
}

function showWorkspaceForm(workspace = null) {
  if (workspaceOperationPending()) return;
  state.workspaces.editingId = workspace?.id || null;
  $("#workspace-id").value = workspace?.id || "";
  $("#workspace-name-input").value = workspace?.name || "";
  $("#workspace-path-input").value = workspace?.path || "";
  $("#workspace-kind").value = workspace?.kind === "ssh" ? "ssh" : "local";
  $("#workspace-host-input").value = workspace?.host || "";
  $("#workspace-port-input").value = workspace?.ssh_port ? String(workspace.ssh_port) : "";
  syncWorkspaceKind();
  state.workspaces.formSnapshot = { id: workspace?.id || "", name: workspace?.name || "", path: workspace?.path || "", kind: $("#workspace-kind").value, host: $("#workspace-host-input").value, port: $("#workspace-port-input").value };
  $("#workspace-form-title").textContent = workspace ? "Edit saved workspace" : "Add workspace";
  $("#workspace-form-error").textContent = "";
  $("#workspace-form").hidden = false;
  $("#workspace-name-input").focus();
}

function hideWorkspaceForm() {
  if (workspaceOperationPending()) return;
  state.workspaces.editingId = null;
  state.workspaces.formSnapshot = null;
  $("#workspace-form").reset(); $("#workspace-id").value = ""; $("#workspace-form-error").textContent = ""; $("#workspace-form").hidden = true; syncWorkspaceKind();
}

function syncWorkspaceKind() {
  const remote = $("#workspace-kind").value === "ssh";
  for (const field of $$(".workspace-ssh-field")) field.hidden = !remote;
  $("#workspace-browse").hidden = remote;
  $("#workspace-path-caption").textContent = remote ? "Remote project directory" : "Project directory";
  $("#workspace-path-input").placeholder = remote ? "/home/user/seeknal-project or ~/seeknal-project" : "/absolute/path/to/seeknal-project";
}

function useCurrentWorkspaceProject() {
  const remote = state.project?.remote;
  $("#workspace-kind").value = remote ? "ssh" : "local";
  $("#workspace-host-input").value = remote?.host || "";
  $("#workspace-port-input").value = remote?.ssh_port ? String(remote.ssh_port) : "";
  syncWorkspaceKind();
  if (remote) { $("#workspace-path-input").value = remote.path; if (!$("#workspace-name-input").value.trim()) $("#workspace-name-input").value = state.project?.project || ""; $("#workspace-name-input").focus(); return; }
  $("#workspace-path-input").value = state.project?.project_path || state.workspaces.current?.path || "";
  if (!$("#workspace-name-input").value.trim()) $("#workspace-name-input").value = state.project?.project || state.workspaces.current?.name || "";
  $("#workspace-name-input").focus();
}

async function browseWorkspace() {
  try {
    const payload = await api("/api/projects/browse", { method: "POST", body: {} });
    if (payload.path) $("#workspace-path-input").value = payload.path;
    else if (!payload.cancelled && payload.supported === false) $("#workspace-form-error").textContent = "Native folder selection is unavailable. Enter an absolute project path.";
  } catch (error) { $("#workspace-form-error").textContent = error.message; }
}

async function saveWorkspace(event) {
  event.preventDefault();
  if (workspaceOperationPending()) return;
  const id = $("#workspace-id").value || null;
  const name = $("#workspace-name-input").value.trim();
  const path = $("#workspace-path-input").value.trim();
  const remote = $("#workspace-kind").value === "ssh";
  const host = $("#workspace-host-input").value.trim();
  const sshPort = $("#workspace-port-input").value.trim();
  if (!name) { $("#workspace-form-error").textContent = "Enter a workspace name."; $("#workspace-name-input").focus(); return; }
  if (remote && !host) { $("#workspace-form-error").textContent = "Enter the SSH host as user@host or an SSH config alias."; $("#workspace-host-input").focus(); return; }
  if (!path) { $("#workspace-form-error").textContent = remote ? "Enter the remote Seeknal project directory." : "Choose a Seeknal project directory."; $("#workspace-path-input").focus(); return; }
  const body = { name, path }; if (id) body.id = id;
  if (remote) { body.host = host; if (sshPort) body.ssh_port = sshPort; }
  try {
    state.workspaces.savePending = true;
    setWorkspaceFormDisabled(true); $("#workspace-form-error").textContent = ""; renderWorkspaces();
    if (remote) workspaceStatus(`Checking ${host} over SSH…`);
    await api("/api/workspaces", { method: "POST", body });
    state.workspaces.savePending = false; hideWorkspaceForm(); await loadWorkspaces(); workspaceStatus(id ? "Saved workspace registration updated. The current project was not switched." : "Workspace saved. Open it explicitly when ready.");
  } catch (error) { $("#workspace-form-error").textContent = error.message; if (remote) workspaceStatus(""); }
  finally { state.workspaces.savePending = false; setWorkspaceFormDisabled(false); renderWorkspaces(); }
}

async function requestForgetWorkspace(workspace) {
  if (workspaceOperationPending()) return;
  if (state.workspaces.forgetId !== workspace.id) { state.workspaces.forgetId = workspace.id; renderWorkspaceList(); return; }
  try {
    state.workspaces.forgetPending = workspace.id; setWorkspaceFormDisabled(true); renderWorkspaces();
    await api("/api/workspaces/remove", { method: "POST", body: { id: workspace.id } });
    if (state.workspaces.forgetPending !== workspace.id) return;
    state.workspaces.forgetPending = null; state.workspaces.forgetId = null; await loadWorkspaces(); workspaceStatus(`${workspace.name || "Workspace"} removed from Studio list; files remain.`);
  } catch (error) { workspaceStatus(error.message, "error"); }
  finally { if (state.workspaces.forgetPending === workspace.id) state.workspaces.forgetPending = null; setWorkspaceFormDisabled(false); renderWorkspaces(); }
}

function hasUnsavedEnvironmentInputs() {
  if (state.environment.dirty) return true;
  return Boolean($("#environment-new-key")?.value.trim() || $$('.environment-row input[type="password"]').some(input => input.value) || $$('.environment-row input[type="checkbox"]').some(input => input.checked));
}

function hasUnsavedWorkspaceForm() {
  if ($("#workspace-form").hidden || !state.workspaces.formSnapshot) return false;
  const snapshot = state.workspaces.formSnapshot;
  return $("#workspace-id").value !== snapshot.id || $("#workspace-name-input").value !== snapshot.name || $("#workspace-path-input").value !== snapshot.path
    || ($("#workspace-kind")?.value || "local") !== (snapshot.kind || "local") || ($("#workspace-host-input")?.value || "") !== (snapshot.host || "") || ($("#workspace-port-input")?.value || "") !== (snapshot.port || "");
}

function guardWorkspaceUnload(event) {
  if (state.workspaces.unloadAuthorized) { state.workspaces.unloadAuthorized = false; return false; }
  if (state.dirty || hasUnsavedEnvironmentInputs() || hasUnsavedWorkspaceForm()) {
    event.preventDefault(); event.returnValue = ""; return true;
  }
  return false;
}

function workspaceSwitchBlockReason() {
  if (hasActiveWork()) return "Project-bound work is active. Finish or cancel the job, agent turn, or REPL operation before switching workspaces.";
  return "";
}

function workspaceDiscardWarnings() {
  const warnings = [];
  if (state.dirty) warnings.push("The modified YAML editor buffer has not been saved.");
  if (hasUnsavedWorkspaceForm()) warnings.push("The workspace name or project path form has unsaved changes.");
  if (hasUnsavedEnvironmentInputs()) warnings.push("Configuration contains unsaved environment values.");
  return warnings;
}

function requestWorkspaceSwitch(workspace) {
  const warnings = workspaceDiscardWarnings();
  if (!warnings.length) return false;
  state.workspaces.confirmTarget = Object.freeze({ id: workspace.id, name: workspace.name, path: workspace.path, discardConfirmed: true });
  $("#workspace-switch-target").textContent = `Switch to ${workspace.name}?`;
  const root = $("#workspace-switch-warnings"); root.replaceChildren();
  for (const warning of warnings) { const item = document.createElement("li"); item.textContent = warning; root.append(item); }
  $("#workspace-switch-dialog").returnValue = ""; $("#workspace-switch-dialog").showModal(); $("#workspace-switch-cancel").focus();
  return true;
}

async function admitWorkspaceOpen(workspace) {
  if (workspaceOperationPending()) return;
  const admitted = Object.freeze({ id: workspace.id, name: workspace.name, path: workspace.path, discardConfirmed: workspace.discardConfirmed === true });
  state.workspaces.openPending = admitted; setWorkspaceFormDisabled(true); renderWorkspaces();
  try {
    workspaceStatus(`Switching to ${admitted.name}…`); setStatus(`Launcher is switching to ${admitted.name}…`);
    const payload = await api("/api/workspaces/open", { method: "POST", body: { id: admitted.id } });
    if (state.workspaces.openPending?.id !== admitted.id) return;
    const target = Object.freeze({ id: payload.workspace?.id || admitted.id, name: payload.workspace?.name || admitted.name, path: payload.workspace?.path || admitted.path, discardConfirmed: admitted.discardConfirmed, instance: state.workspaces.instance || null, remote: payload.workspace?.kind === "ssh" });
    state.workspaces.openPending = target;
    workspaceStatus(target.remote ? `Launcher accepted ${target.name}; connecting over SSH and starting the remote runtime…` : `Launcher accepted ${target.name}; waiting for the project runtime…`);
    setTimeout(() => awaitProjectRestart(target.path, 0, target.id), 700);
  } catch (error) {
    if (state.workspaces.openPending?.id === admitted.id) state.workspaces.openPending = null;
    state.workspaces.unloadAuthorized = false; setWorkspaceFormDisabled(false);
    renderWorkspaces(); workspaceStatus(error.message, "error"); setStatus(error.message, "error"); await loadWorkspaces();
  }
}

function finishWorkspaceSwitchConfirmation() {
  const dialog = $("#workspace-switch-dialog");
  const target = state.workspaces.confirmTarget;
  state.workspaces.confirmTarget = null;
  if (!target) return;
  if (dialog.returnValue === "confirm") void admitWorkspaceOpen(target);
  else { setStatus("Workspace switch cancelled; unsaved editor and Configuration values were preserved."); $("#workspace-menu-trigger").focus(); }
}

function openWorkspace(workspace) {
  closeWorkspaceMenu();
  const unavailableReason = workspaceOpenReason(workspace);
  if (unavailableReason) { workspaceStatus(unavailableReason, "error"); setStatus(unavailableReason, "error"); return; }
  const blocked = workspaceSwitchBlockReason();
  if (blocked) { workspaceStatus(blocked, "error"); setStatus(blocked, "error"); return; }
  if (requestWorkspaceSwitch(workspace)) return;
  void admitWorkspaceOpen(workspace);
}

function closeWorkspaceMenu({ restoreFocus = false } = {}) {
  $("#workspace-menu").hidden = true; $("#workspace-menu-trigger").setAttribute("aria-expanded", "false");
  if (restoreFocus) $("#workspace-menu-trigger").focus();
}

async function toggleWorkspaceMenu() {
  const menu = $("#workspace-menu"); const open = menu.hidden;
  closeMenus(); menu.hidden = !open; $("#workspace-menu-trigger").setAttribute("aria-expanded", String(open));
  if (open) { await loadWorkspaces(); if (!menu.hidden) $("button:not([disabled])", menu)?.focus(); }
}

function navigateWorkspaceMenu(event) {
  const menu = $("#workspace-menu");
  if (event.key === "Escape") { event.preventDefault(); closeWorkspaceMenu({ restoreFocus: true }); return; }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const items = $$('button:not([disabled])', menu); if (!items.length) return;
  event.preventDefault();
  const current = items.indexOf(document.activeElement);
  const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : event.key === "ArrowDown" ? (current + 1 + items.length) % items.length : (current - 1 + items.length) % items.length;
  items[index].focus();
}

function navigateSavedWorkspaceSubmenu(event) {
  const menu = $("#recent-project-menu");
  if (event.key === "Escape" || event.key === "ArrowLeft") { event.preventDefault(); event.stopPropagation(); menu.hidden = true; $("#menu-recent-projects").setAttribute("aria-expanded", "false"); $("#menu-recent-projects").focus(); return; }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const items = $$('button:not([disabled])', menu); if (!items.length) return;
  event.preventDefault();
  const current = items.indexOf(document.activeElement);
  const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : event.key === "ArrowDown" ? (current + 1 + items.length) % items.length : (current - 1 + items.length) % items.length;
  items[index].focus();
}

function manageWorkspaces({ add = false } = {}) {
  closeWorkspaceMenu(); closeMenus(); setMainView("config");
  if (add) showWorkspaceForm();
  else $(".workspaces-card")?.scrollIntoView({ block: "start" });
}

async function loadProjects() {
  try {
    const payload = await api("/api/projects");
    state.projects = payload;
    renderRecentProjects();
    return payload;
  } catch (error) { showError(error); return null; }
}

function renderRecentProjects() {
  const root = $("#recent-project-menu"); root.replaceChildren();
  for (const workspace of state.workspaces.entries) root.append(createWorkspaceMenuItem(workspace));
  if (!root.children.length) { const empty = document.createElement("p"); empty.textContent = "No saved workspaces"; root.append(empty); }
}

async function openProjectDialog() {
  manageWorkspaces({ add: true });
}

async function browseProject() {
  try {
    const payload = await api("/api/projects/browse", { method: "POST", body: {} });
    if (payload.path) $("#project-path-input").value = payload.path;
    else if (!payload.cancelled && payload.supported === false) setStatus("Native browse is unavailable; enter an absolute path");
  } catch (error) { showError(error); }
}

async function switchProject() {
  const path = $("#project-path-input").value.trim();
  if (!path || !path.startsWith("/") || !canSwitchProject()) return;
  try {
    $("#project-open").disabled = true;
    const payload = await api("/api/projects/open", { method: "POST", body: { path } });
    $("#project-switch-note").textContent = `Switching to ${payload.project?.path || path}…`;
    setStatus(`Launcher accepted ${payload.project?.name || path}; restarting…`, "success");
    setTimeout(() => awaitProjectRestart(payload.project?.path || path), 700);
  } catch (error) { $("#project-open").disabled = false; showError(error); }
}

async function awaitProjectRestart(expectedPath, attempts = 0, expectedId = null) {
  try {
    const workspaces = await api("/api/workspaces");
    const startInstance = state.workspaces.openPending?.instance;
    // A new launcher-owned service (including a fallback after a failed SSH
    // connection) reports a new instance; reload to show it and any notice.
    const restarted = startInstance && workspaces.instance ? workspaces.instance !== startInstance : workspaces.current?.path === expectedPath;
    if (restarted) {
      state.workspaces.unloadAuthorized = state.workspaces.openPending?.discardConfirmed === true;
      window.location.reload(); return;
    }
  } catch {
    // The managed host is expected to be briefly unavailable while it restarts.
  }
  // First use of an SSH host may build the pinned runtime (launcher budget ~21 minutes); allow 30.
  const limit = state.workspaces.openPending?.remote ? 3600 : 120;
  if (attempts < limit && (!expectedId || state.workspaces.openPending?.id === expectedId)) setTimeout(() => awaitProjectRestart(expectedPath, attempts + 1, expectedId), 500);
  else {
    state.workspaces.openPending = null; state.workspaces.unloadAuthorized = false; setWorkspaceFormDisabled(false); renderWorkspaces();
    showError("The launcher accepted the project, but this browser could not reconnect to the restarted Studio");
  }
}

const editorViewElements = { graph: ".pipeline-stage", yaml: ".workbench-split", logs: ".logs-page", repl: "#repl-workbench", config: "#configuration-page" };
const editorViewLabels = { graph: "Graph", yaml: "YAML", logs: "Logs", repl: "SQL REPL", config: "Configuration" };
const editorViewPanelIds = { graph: "graph-page", yaml: "yaml-page", logs: "logs-page", repl: "repl-workbench", config: "configuration-page" };

function isViewVisible(view) {
  const owner = state.editorGroups.owners[view];
  return Boolean(owner && state.editorGroups.groups[owner].active === view);
}

function activateEditorGroup(groupId) {
  if (!state.editorGroups.groups[groupId] || (groupId === "right" && !state.editorGroups.split)) return;
  state.editorGroups.activeGroup = groupId;
  for (const groupElement of $$(".editor-group")) groupElement.classList.toggle("active", groupElement.dataset.group === groupId);
}

function editorTabLabel(view) {
  return view === "yaml" && state.selectedFile ? fileName(state.selectedFile) : editorViewLabels[view];
}

function renderEditorGroups() {
  const model = state.editorGroups;
  const root = $("#editor-groups");
  if (!root) return;
  root.dataset.split = String(model.split);
  root.style.setProperty("--left-group-size", `${model.ratio}%`);
  $('.editor-group[data-group="right"]').hidden = !model.split;
  $("#editor-group-divider").hidden = !model.split;
  let parking = $("#editor-view-parking");
  if (!parking) { parking = document.createElement("div"); parking.id = "editor-view-parking"; parking.hidden = true; $(".studio-main").append(parking); }
  for (const groupElement of $$(".editor-group")) {
    const groupId = groupElement.dataset.group;
    const group = model.groups[groupId];
    groupElement.classList.toggle("active", groupId === model.activeGroup);
    const strip = $(".editor-tabstrip", groupElement);
    strip.replaceChildren();
    for (const view of group.tabs) {
      const item = document.createElement("div");
      item.className = "editor-tab-item"; item.draggable = true; item.setAttribute("role", "presentation"); item.classList.toggle("active", group.active === view);
      const tab = document.createElement("button");
      tab.type = "button"; tab.className = "editor-tab"; tab.dataset.view = view; tab.id = `editor-tab-${groupId}-${view}`;
      tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(group.active === view));
      tab.setAttribute("aria-controls", editorViewPanelIds[view]); tab.tabIndex = group.active === view ? 0 : -1;
      const label = document.createElement("span"); label.textContent = editorTabLabel(view);
      const marker = document.createElement("i"); marker.className = "editor-tab-marker";
      marker.hidden = !((view === "yaml" && state.dirty) || (view === "repl" && state.repl.stale));
      const close = document.createElement("button"); close.type = "button"; close.className = "editor-tab-close";
      close.setAttribute("aria-label", `Close ${editorTabLabel(view)}`); close.title = `Close ${editorTabLabel(view)}`; close.textContent = "×";
      close.addEventListener("click", event => { event.stopPropagation(); closeEditorView(view); });
      tab.append(label, marker); item.append(tab, close);
      tab.addEventListener("click", () => { group.active = view; model.activeGroup = groupId; renderEditorGroups(); activateEditorView(view); });
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const index = group.tabs.indexOf(view);
        const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? group.tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + group.tabs.length) % group.tabs.length;
        const nextView = group.tabs[nextIndex]; group.active = nextView; model.activeGroup = groupId;
        renderEditorGroups(); activateEditorView(nextView);
        requestAnimationFrame(() => $(`#editor-tab-${groupId}-${nextView}`)?.focus());
      });
      item.addEventListener("dragstart", event => { event.dataTransfer.setData("text/x-seeknal-view", view); event.dataTransfer.effectAllowed = "move"; });
      strip.append(item);
    }
    const content = $(".editor-group-content", groupElement);
    $(".editor-group-empty", content)?.remove();
    if (!group.active) { const empty = document.createElement("div"); empty.className = "editor-group-empty"; empty.textContent = "Open a view from the activity rail, or move a tab here."; content.append(empty); }
    $('[data-group-action="move-left"]', groupElement).disabled = groupId === "left" || !group.active;
    $('[data-group-action="move-right"]', groupElement).disabled = groupId === "right" || !group.active;
  }
  for (const view of EDITOR_VIEWS) {
    const element = $(editorViewElements[view]);
    if (!element.id) element.id = editorViewPanelIds[view];
    element.setAttribute("role", "tabpanel");
    const owner = model.owners[view];
    if (!owner) { element.hidden = true; element.setAttribute("aria-hidden", "true"); element.removeAttribute("aria-labelledby"); parking.append(element); continue; }
    const content = $(`.editor-group[data-group="${owner}"] .editor-group-content`);
    if (element.parentElement !== content) content.append(element);
    element.hidden = model.groups[owner].active !== view;
    element.setAttribute("aria-hidden", String(element.hidden));
    element.setAttribute("aria-labelledby", `editor-tab-${owner}-${view}`);
  }
  const yamlVisible = Object.values(model.groups).some(group => group.active === "yaml");
  $(".workspace").classList.toggle("has-yaml", yamlVisible);
  requestAnimationFrame(renderEdges);
}

function activateEditorView(view) {
  if (view === "repl") refreshReplStatus();
  else if (view === "logs") renderJobs();
  else if (view === "config") {
    if (!state.environment.loaded || !hasUnsavedEnvironmentInputs()) loadEnvironment();
    void loadWorkspaces();
  }
  else if (view === "graph") requestAnimationFrame(renderEdges);
}

function setMainView(view, groupId = state.editorGroups.activeGroup) {
  if (!editorOpen(state.editorGroups, view, groupId)) return;
  renderEditorGroups(); activateEditorView(view);
}

function closeEditorView(view) {
  if (view === "yaml" && state.dirty) { setStatus("Save the modified YAML before closing its tab", "error"); $("#code-editor").focus(); return false; }
  const closed = editorClose(state.editorGroups, view, { dirty: state.dirty });
  if (closed) { renderEditorGroups(); activateEditorView(state.editorGroups.groups[state.editorGroups.activeGroup].active); }
  return closed;
}

function splitEditorRight() { editorSplit(state.editorGroups); renderEditorGroups(); }
function moveActiveEditor(direction, sourceGroup = state.editorGroups.activeGroup) { state.editorGroups.activeGroup = sourceGroup; if (editorMoveActive(state.editorGroups, direction)) { const view = state.editorGroups.groups[state.editorGroups.activeGroup].active; renderEditorGroups(); activateEditorView(view); } }
function mergeEditorGroups() { editorMerge(state.editorGroups); renderEditorGroups(); activateEditorView(state.editorGroups.groups.left.active); }

function renderEnvironment(payload) {
  const environment = payload.environment || payload;
  state.environment.contentHash = environment.content_hash || null;
  state.environment.entries = Array.isArray(environment.entries) ? environment.entries : [];
  state.environment.loaded = true;
  state.environment.dirty = false;
  $("#environment-path").textContent = `${environment.path || ".env"}${environment.exists ? "" : " · will be created"}`;
  $("#environment-status").textContent = payload.saved ? "Saved" : environment.exists ? "Loaded" : "Not created";
  const root = $("#environment-entries"); root.replaceChildren();
  if (!state.environment.entries.length) {
    const empty = document.createElement("div"); empty.className = "empty-state"; empty.textContent = "No project environment variables are configured."; root.append(empty);
  }
  for (const entry of state.environment.entries) appendEnvironmentRow(entry);
  if (payload.reload?.message) setStatus(payload.reload.message, "success");
}

function appendEnvironmentRow(entry) {
  const root = $("#environment-entries");
  const row = document.createElement("div"); row.className = "environment-row"; row.dataset.key = entry.key;
  const key = document.createElement("div"); key.className = "environment-key";
  const strong = document.createElement("strong"); strong.textContent = entry.key;
  const small = document.createElement("small"); small.textContent = entry.has_value ? "Value configured · hidden" : "Not configured";
  key.append(strong, small);
  const input = document.createElement("input"); input.type = "password"; input.autocomplete = "new-password"; input.spellcheck = false;
  input.placeholder = entry.has_value ? "Leave blank to preserve" : "Enter value"; input.disabled = entry.editable === false;
  input.setAttribute("aria-label", `New value for ${entry.key}`);
  const removeLabel = document.createElement("label"); removeLabel.className = "environment-remove";
  const remove = document.createElement("input"); remove.type = "checkbox"; remove.disabled = entry.removable === false;
  removeLabel.append(remove, document.createTextNode(" Remove"));
  if (entry.protected_reason) { row.title = entry.protected_reason; small.textContent += ` · ${entry.protected_reason}`; }
  row.append(key, input, removeLabel); root.append(row);
}

async function loadEnvironment() {
  $("#environment-status").textContent = "Loading…";
  try { renderEnvironment(await api("/api/environment")); }
  catch (error) { $("#environment-status").textContent = "Unavailable"; showError(error); }
}

function addEnvironmentEntry() {
  const input = $("#environment-new-key"); const key = input.value.trim().toUpperCase();
  if (!/^[A-Z_][A-Z0-9_]*$/.test(key)) { setStatus("Use an uppercase environment key such as WAREHOUSE_TOKEN", "error"); return; }
  if ($(`.environment-row[data-key="${CSS.escape(key)}"]`)) { setStatus(`${key} is already listed`, "error"); return; }
  appendEnvironmentRow({ key, has_value: false, editable: true, removable: true }); input.value = ""; state.environment.dirty = true;
  $(`.environment-row[data-key="${CSS.escape(key)}"] input[type="password"]`)?.focus();
}

async function saveEnvironment(event) {
  event.preventDefault();
  const updates = {}, remove = [];
  for (const row of $$(".environment-row")) {
    const key = row.dataset.key, value = $('input[type="password"]', row).value;
    if ($('input[type="checkbox"]', row).checked) remove.push(key);
    else if (value) updates[key] = value;
  }
  try {
    $("#environment-save").disabled = true; $("#environment-status").textContent = "Saving…";
    const payload = await api("/api/environment", { method: "POST", body: { updates, remove, content_hash: state.environment.contentHash } });
    renderEnvironment(payload); setStatus("Project environment saved without exposing stored values", "success");
  } catch (error) {
    showError(error);
    if (error.status === 409 && error.payload?.error_code === "environment_conflict") {
      await loadEnvironment();
      setStatus("Environment changed on disk. Metadata was refreshed; re-enter unsaved values before saving.", "error");
    } else if (error.status === 409) {
      $("#environment-status").textContent = "Busy · values preserved";
      setStatus("Project work is active. Stop it, then retry; entered values remain in this form.", "error");
    }
  }
  finally { $("#environment-save").disabled = false; }
}

function markReplStale(reason) {
  if (!state.repl.result) return;
  state.repl.stale = true;
  $("#repl-result-status").textContent = `Stale · ${reason}`;
  $("#repl-results").classList.add("stale-result");
  renderEditorGroups();
}

function renderReplStatus(payload = {}) {
  state.repl.status = payload.state || payload.status || (payload.ready ? "ready" : state.repl.status);
  state.repl.binding = payload.project_path || payload.binding?.project_path || state.project?.project_path;
  state.repl.needsReload = payload.needs_reload === true;
  const bindingMatches = Boolean(state.repl.binding && state.repl.binding === state.project?.project_path);
  const ready = ["ready", "running", "idle"].includes(state.repl.status) && bindingMatches && !state.repl.needsReload;
  $("#repl-status").textContent = state.repl.needsReload ? "Reload required" : ready ? "Ready" : bindingMatches ? capitalize(state.repl.status) : "Binding mismatch";
  $("#repl-status").className = `repl-status ${state.repl.needsReload ? "reload" : ready ? "ready" : "offline"}`;
  const provenance = [payload.environment || payload.engine || "Seeknal", payload.session_id ? `session ${String(payload.session_id).slice(0, 8)}` : null, payload.current_run_id ? `run ${String(payload.current_run_id).slice(0, 8)}` : null].filter(Boolean).join(" · ");
  $("#repl-binding").textContent = state.repl.binding ? `${state.repl.binding} · ${provenance}` : "Not started";
  $("#repl-start").disabled = ready;
  const accepting = state.repl.queryPending || state.repl.inspectPending || state.repl.reloadPending;
  $("#repl-run").disabled = !ready || Boolean(state.repl.queryId || state.repl.inspectId || accepting);
  $("#repl-reload").disabled = !bindingMatches || Boolean(state.repl.queryId || state.repl.inspectId || accepting);
  const tables = payload.tables || payload.outputs || [];
  if (tables.length) { renderReplTables(tables); state.repl.tablesLoaded = true; }
  clearTimeout(state.repl.statusTimer);
  if (state.repl.status === "starting") state.repl.statusTimer = setTimeout(refreshReplStatus, 350);
}

function renderReplTables(tables) {
  const root = $("#repl-tables"); root.replaceChildren();
  for (const item of tables) {
    const name = typeof item === "string" ? item : item.name || item.table;
    const row = document.createElement("div"); row.className = "repl-table-row";
    const select = document.createElement("button"); select.type = "button"; select.textContent = name;
    select.addEventListener("click", () => { $("#repl-editor").value = `SELECT * FROM \"${String(name).replaceAll('"', '""')}\" LIMIT 100;`; $("#repl-editor").focus(); });
    const schema = document.createElement("button"); schema.type = "button"; schema.textContent = "schema"; schema.title = `Inspect ${name} schema`;
    schema.addEventListener("click", () => { $("#repl-editor").value = `.schema ${name}`; $("#repl-editor").focus(); });
    row.append(select, schema); root.append(row);
  }
}

async function refreshReplStatus() {
  try {
    renderReplStatus(await api("/api/repl/status"));
    const ready = ["ready", "running", "idle"].includes(state.repl.status) && state.repl.binding === state.project?.project_path && !state.repl.needsReload;
    if (ready && isViewVisible("repl")) await inspectReplTables();
  } catch (error) { renderReplStatus({ status: "offline" }); setStatus(error.message, "error"); }
}

async function startRepl() {
  try {
    renderReplStatus(await api("/api/repl/start", { method: "POST", body: {} }));
    await inspectReplTables();
    setStatus("Project-bound Seeknal REPL started", "success");
  } catch (error) { showError(error); }
}

async function inspectReplTables() {
  if (state.repl.inspectPending || state.repl.queryPending || state.repl.reloadPending || state.repl.inspectId || state.repl.queryId || state.repl.tablesLoaded) return;
  state.repl.inspectPending = true;
  const root = $("#repl-tables");
  root.replaceChildren(Object.assign(document.createElement("p"), { textContent: "Inspecting registered project outputs…" }));
  $("#repl-run").disabled = true; $("#repl-reload").disabled = true;
  try {
    const accepted = await api("/api/repl/query", { method: "POST", body: { sql: ".tables" } });
    const id = accepted.query_id || accepted.id; state.repl.inspectId = id; state.repl.inspectPending = false;
    await pollReplInspection(id);
  } catch (error) {
    state.repl.inspectPending = false; state.repl.inspectId = null; state.repl.tablesLoaded = true;
    root.replaceChildren(Object.assign(document.createElement("p"), { textContent: `Table inspection unavailable: ${error.message}` }));
    await refreshReplStatus();
  }
}

async function pollReplInspection(id) {
  const payload = await api(`/api/repl/query?id=${encodeURIComponent(id)}`);
  if (id !== state.repl.inspectId) return;
  const terminal = ["succeeded", "success", "failed", "cancelled", "canceled"].includes(payload.status);
  if (!terminal) { await new Promise(resolve => setTimeout(resolve, 200)); return pollReplInspection(id); }
  state.repl.inspectId = null;
  if (["succeeded", "success"].includes(payload.status)) {
    const tableIndex = Array.isArray(payload.columns) ? Math.max(0, payload.columns.indexOf("table")) : 0;
    const names = (payload.rows || []).map(row => Array.isArray(row) ? row[tableIndex] : row?.table || row?.table_name || row?.name).filter(Boolean);
    renderReplTables(names); state.repl.tablesLoaded = true;
  } else {
    state.repl.tablesLoaded = true;
    $("#repl-tables").replaceChildren(Object.assign(document.createElement("p"), { textContent: payload.error || "Table inspection failed." }));
  }
  await refreshReplStatus();
}

async function runReplQuery() {
  const sql = $("#repl-editor").value.trim();
  if (!sql || state.repl.queryPending || state.repl.inspectPending || state.repl.reloadPending || state.repl.queryId || state.repl.inspectId) return;
  state.repl.queryPending = true; $("#repl-run").disabled = true;
  try {
    const payload = await api("/api/repl/query", { method: "POST", body: { sql } });
    state.repl.queryId = payload.query_id || payload.id;
    state.repl.queryPending = false;
    state.repl.stale = false;
    state.repl.history.unshift({ sql, id: state.repl.queryId, at: new Date().toLocaleTimeString() });
    renderReplHistory();
    $("#repl-cancel").hidden = false; $("#repl-run").disabled = true;
    $("#repl-result-status").textContent = "Running…";
    pollReplQuery(state.repl.queryId);
  } catch (error) { state.repl.queryPending = false; renderReplError(error.message); await refreshReplStatus(); }
}

async function pollReplQuery(id) {
  try {
    const payload = await api(`/api/repl/query?id=${encodeURIComponent(id)}`);
    if (id !== state.repl.queryId) return;
    const terminal = ["succeeded", "success", "failed", "cancelled", "canceled"].includes(payload.status);
    if (!terminal) { state.repl.pollTimer = setTimeout(() => pollReplQuery(id), 300); return; }
    state.repl.queryId = null; $("#repl-cancel").hidden = true; $("#repl-run").disabled = false;
    if (["succeeded", "success"].includes(payload.status)) renderReplResult(payload);
    else {
      if (payload.error_code === "reload_required") { state.repl.needsReload = true; renderReplStatus({ ...payload, state: "ready", ready: true, project_path: state.repl.binding, needs_reload: true }); }
      renderReplError(payload.error || `Query ${payload.status}`);
    }
  } catch (error) { state.repl.queryId = null; $("#repl-cancel").hidden = true; $("#repl-run").disabled = false; renderReplError(error.message); }
}

function renderReplResult(payload) {
  state.repl.result = payload; state.repl.stale = false;
  $("#repl-results").classList.remove("stale-result"); renderEditorGroups();
  const columns = payload.columns || []; const rows = payload.rows || [];
  if (payload.kind === "tables") renderReplTables(rows.map(row => Array.isArray(row) ? row[0] : row?.name || row?.table).filter(Boolean));
  $("#repl-result-status").textContent = `${payload.historical || payload.current === false ? "Historical · " : ""}${payload.row_count ?? rows.length} rows · ${payload.duration_ms != null ? `${payload.duration_ms} ms` : "complete"}`;
  if (payload.historical || payload.current === false) { state.repl.stale = true; $("#repl-results").classList.add("stale-result"); renderEditorGroups(); }
  const root = $("#repl-results"); root.replaceChildren();
  if (!columns.length) { const pre = document.createElement("pre"); pre.textContent = payload.text || payload.output || payload.message || "Query completed without tabular output."; root.append(pre); return; }
  const table = document.createElement("table"); table.className = "preview-table"; const thead = document.createElement("thead"); const trh = document.createElement("tr");
  for (const column of columns) { const th = document.createElement("th"); th.textContent = String(column); trh.append(th); } thead.append(trh); table.append(thead);
  const tbody = document.createElement("tbody"); for (const row of rows) { const tr = document.createElement("tr"); const values = Array.isArray(row) ? row : columns.map(column => row?.[column]); for (const value of values) { const td = document.createElement("td"); td.textContent = value == null ? "null" : String(value); tr.append(td); } tbody.append(tr); } table.append(tbody); root.append(table);
}

function renderReplError(message) { $("#repl-result-status").textContent = "Error"; const error = document.createElement("pre"); error.className = "preview-error"; error.textContent = message; $("#repl-results").replaceChildren(error); }

function renderReplHistory() {
  const root = $("#repl-history"); root.replaceChildren();
  for (const item of state.repl.history) { const button = document.createElement("button"); button.type = "button"; button.textContent = `${item.at}  ${item.sql.replace(/\s+/g, " ").slice(0, 54)}`; button.title = item.sql; button.addEventListener("click", () => { $("#repl-editor").value = item.sql; }); root.append(button); }
}

async function cancelReplQuery() {
  if (!state.repl.queryId) return; const id = state.repl.queryId;
  try { await api(`/api/repl/query/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} }); pollReplQuery(id); } catch (error) { showError(error); }
}

async function reloadRepl() {
  if (state.repl.reloadPending || state.repl.queryPending || state.repl.inspectPending || state.repl.queryId || state.repl.inspectId) return;
  state.repl.reloadPending = true; $("#repl-reload").disabled = true; $("#repl-run").disabled = true;
  try {
    const payload = await api("/api/repl/reload", { method: "POST", body: {} });
    state.repl.result = null; state.repl.stale = false; state.repl.tablesLoaded = false;
    $("#repl-results").innerHTML = '<div class="empty-state">Outputs re-registered. Run a query to inspect current data.</div>'; $("#repl-result-status").textContent = "Reloaded";
    renderReplStatus(payload); state.repl.reloadPending = false; await inspectReplTables();
  } catch (error) { state.repl.reloadPending = false; showError(error); await refreshReplStatus(); }
}

async function sendSqlToRepl() {
  const context = currentTransformContext(); if (!context.eligible) return;
  const groupId = state.editorGroups.activeGroup;
  try {
    const payload = await api("/api/repl/extract", { method: "POST", body: { path: context.path, content: context.content } });
    $("#repl-editor").value = payload.repl_sql || payload.sql || "";
    setMainView("repl", groupId);
    const rewrites = Array.isArray(payload.rewritten_refs) ? payload.rewritten_refs : [];
    const rewriteNote = rewrites.length ? ` AST-mapped ${rewrites.map(item => `${item.from} → ${item.to}`).join(", ")}.` : " No declared input references required rewriting.";
    $("#repl-query-meta").textContent = `${context.path} · ${rewrites.length} reference rewrite${rewrites.length === 1 ? "" : "s"}`;
    setStatus(`SQL copied to REPL editor; it has not been executed.${rewriteNote}`, "success");
  } catch (error) { showError(error); }
}

function openRunHistory() {
  setMainView("logs");
}

function bindEvents() {
  $("#code-editor").addEventListener("input", () => { updateLineNumbers(); markDirty(); });
  $("#code-editor").addEventListener("scroll", event => { $("#line-numbers").scrollTop = event.currentTarget.scrollTop; $("#syntax-layer").scrollTop = event.currentTarget.scrollTop; $("#syntax-layer").scrollLeft = event.currentTarget.scrollLeft; });
  $("#code-editor").addEventListener("keydown", event => {
    if (event.key === "Tab") {
      event.preventDefault();
      const editor = event.currentTarget;
      const start = editor.selectionStart, end = editor.selectionEnd;
      editor.setRangeText("  ", start, end, "end");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
  $("#save-button").addEventListener("click", runEditorFlowAction);
  $("#draft-editor").addEventListener("click", openNewDraftDialog);
  $("#draft-menu-trigger").addEventListener("click", event => { event.stopPropagation(); const menu = $("#draft-action-menu"); const open = menu.hidden; closeMenus(); menu.hidden = !open; event.currentTarget.setAttribute("aria-expanded", String(open)); if (open) $("button:not([disabled])", menu)?.focus(); });
  $("#draft-sql-ai").addEventListener("click", () => { closeMenus(); openSqlDraftDialog(); });
  $("#send-sql-repl").addEventListener("click", () => { closeMenus(); sendSqlToRepl(); });
  $("#sql-ai-send").addEventListener("click", sendSqlDraftRequest);
  $("#run-button").addEventListener("click", () => startJob("run"));
  $("#validate-button").addEventListener("click", () => startJob("validate"));
  $("#cancel-button").addEventListener("click", cancelActiveJob);
  $("#refresh-graph").addEventListener("click", refreshGraph);
  $("#fit-graph").addEventListener("click", fitGraph);
  $("#layout-graph").addEventListener("click", resetGraphLayout);
  $("#open-selected-editor").addEventListener("click", () => { if (state.selectedFile) setMainView("yaml"); else setStatus("Select a graph node with a YAML file first", "error"); });
  $("#graph-search").addEventListener("input", event => { state.graphQuery = event.target.value; renderGraph(); });
  $("#reset-graph").addEventListener("click", () => { state.graphQuery = ""; $("#graph-search").value = ""; renderGraph(); });
  $("#global-search").addEventListener("input", event => { state.graphQuery = event.target.value; $("#graph-search").value = event.target.value; renderGraph(); });
  $("#agent-toggle").addEventListener("click", () => $("#agent-panel").classList.toggle("open"));
  $("#agent-cancel").addEventListener("click", cancelAgent);
  $("#agent-configure").addEventListener("click", configureAgent);
  $("#workspace-menu-trigger").addEventListener("click", event => { event.stopPropagation(); void toggleWorkspaceMenu(); });
  $("#workspace-menu-trigger").addEventListener("keydown", async event => { if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); if ($("#workspace-menu").hidden) await toggleWorkspaceMenu(); const items = $$('button:not([disabled])', $("#workspace-menu")); items[event.key === "ArrowUp" ? items.length - 1 : 0]?.focus(); } });
  $("#workspace-menu").addEventListener("keydown", navigateWorkspaceMenu);
  $("#recent-project-menu").addEventListener("keydown", navigateSavedWorkspaceSubmenu);
  $("#manage-workspaces").addEventListener("click", () => manageWorkspaces());
  $("#workspace-add").addEventListener("click", () => showWorkspaceForm());
  $("#workspace-use-current").addEventListener("click", useCurrentWorkspaceProject);
  $("#workspace-browse").addEventListener("click", browseWorkspace);
  $("#workspace-kind").addEventListener("change", syncWorkspaceKind);
  $("#workspace-cancel").addEventListener("click", hideWorkspaceForm);
  $("#workspace-form").addEventListener("submit", saveWorkspace);
  $("#workspace-switch-dialog").addEventListener("close", finishWorkspaceSwitchConfirmation);
  $("#log-toggle").addEventListener("click", () => { const focused = $(".logs-page").classList.toggle("output-focused"); $("#log-toggle").textContent = focused ? "Show jobs" : "Focus output"; });
  for (const button of $$('[data-local-action]')) button.addEventListener("click", () => startJob(button.dataset.localAction));
  $("#review-drafts").addEventListener("click", openDraftReview);
  $("#validate-draft").addEventListener("click", validateCurrentDraft);
  $("#apply-draft").addEventListener("click", applyCurrentDraft);
  for (const trigger of $$(".menu-trigger")) trigger.addEventListener("click", event => { event.stopPropagation(); toggleMenu(event.currentTarget); });
  $("#menu-open-project").addEventListener("click", openProjectDialog);
  $("#menu-new-node").addEventListener("click", openNewDraftDialog);
  $("#menu-recent-projects").addEventListener("click", async event => { const submenu = $("#recent-project-menu"); const open = submenu.hidden; if (open) await loadWorkspaces(); submenu.hidden = !open; event.currentTarget.setAttribute("aria-expanded", String(open)); if (open) $("button:not([disabled])", submenu)?.focus(); });
  $("#menu-save").addEventListener("click", () => { closeMenus(); runEditorFlowAction(); });
  $("#menu-view-pipeline").addEventListener("click", () => { closeMenus(); setMainView("graph"); });
  $("#menu-view-repl").addEventListener("click", () => { closeMenus(); setMainView("repl"); });
  $("#menu-view-agent").addEventListener("click", () => { closeMenus(); $("#agent-panel").classList.add("open"); $("#agent-input").focus(); });
  $("#menu-dry-run").addEventListener("click", () => { closeMenus(); validateCurrentDraft(); });
  $("#menu-apply").addEventListener("click", () => { closeMenus(); applyCurrentDraft(); });
  $("#menu-run").addEventListener("click", () => { closeMenus(); startJob("run"); });
  $("#new-draft-type").addEventListener("change", updateNewDraftForm);
  $("#new-draft-name").addEventListener("input", updateNewDraftForm);
  $("#new-draft-description").addEventListener("input", updateNewDraftForm);
  $("#new-draft-python").addEventListener("change", updateNewDraftForm);
  $("#new-draft-deps").addEventListener("input", updateNewDraftForm);
  $("#new-draft-submit").addEventListener("click", submitCliDraft);
  $("#project-browse").addEventListener("click", browseProject);
  $("#project-open").addEventListener("click", switchProject);
  $("#project-path-input").addEventListener("input", event => { $("#project-open").disabled = state.projects.can_switch !== true || !event.target.value.trim().startsWith("/"); });
  $("#repl-start").addEventListener("click", startRepl);
  $("#repl-run").addEventListener("click", runReplQuery);
  $("#repl-cancel").addEventListener("click", cancelReplQuery);
  $("#repl-reload").addEventListener("click", reloadRepl);
  $("#repl-editor").addEventListener("keydown", event => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); runReplQuery(); } });
  $("#repl-editor").addEventListener("input", () => markReplStale("SQL text changed"));
  const showSettings = () => setMainView("config");
  $("#settings-nav").addEventListener("click", showSettings);
  $("#project-info-button").addEventListener("click", showSettings);
  const setRailActive = button => { for (const item of $$(".rail-button")) item.classList.toggle("active", item === button); };
  $("#files-nav").addEventListener("click", event => { $(".workspace").classList.remove("explorer-collapsed"); setRailActive(event.currentTarget); setMainView("yaml"); });
  $("#collapse-explorer").addEventListener("click", () => { $(".workspace").classList.add("explorer-collapsed"); requestAnimationFrame(renderEdges); });
  $("#pipeline-nav").addEventListener("click", event => { setRailActive(event.currentTarget); setMainView("graph"); $("#graph-search").focus(); });
  $("#runs-nav").addEventListener("click", event => { setRailActive(event.currentTarget); openRunHistory(); });
  $("#data-nav").addEventListener("click", event => { setRailActive(event.currentTarget); setMainView("repl"); });
  $("#environment-refresh").addEventListener("click", loadEnvironment);
  $("#environment-add").addEventListener("click", addEnvironmentEntry);
  $("#environment-new-key").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); addEnvironmentEntry(); } });
  $("#environment-form").addEventListener("submit", saveEnvironment);
  $("#environment-form").addEventListener("input", () => { state.environment.dirty = true; });
  $("#environment-form").addEventListener("change", () => { state.environment.dirty = true; });
  $("#editor-focus").addEventListener("click", () => { const focused = $(".workspace").classList.toggle("editor-focused"); $("#editor-focus").textContent = focused ? "Exit focus" : "Focus"; $("#editor-focus").setAttribute("aria-pressed", String(focused)); });
  $("#repl-results-focus").addEventListener("click", () => { const focused = $("#repl-main").classList.toggle("results-focused"); $("#repl-results-focus").textContent = focused ? "Show SQL" : "Focus results"; $("#repl-results-focus").setAttribute("aria-pressed", String(focused)); });
  for (const groupElement of $$(".editor-group")) {
    const groupId = groupElement.dataset.group;
    groupElement.addEventListener("pointerdown", () => activateEditorGroup(groupId));
    groupElement.addEventListener("focusin", () => activateEditorGroup(groupId));
    groupElement.addEventListener("dragover", event => { event.preventDefault(); event.dataTransfer.dropEffect = "move"; groupElement.classList.add("drag-target"); });
    groupElement.addEventListener("dragleave", event => { if (!groupElement.contains(event.relatedTarget)) groupElement.classList.remove("drag-target"); });
    groupElement.addEventListener("drop", event => {
      event.preventDefault(); groupElement.classList.remove("drag-target");
      const view = event.dataTransfer.getData("text/x-seeknal-view");
      if (EDITOR_VIEWS.includes(view) && editorMove(state.editorGroups, view, groupId)) { renderEditorGroups(); activateEditorView(view); }
    });
  }
  for (const button of $$('[data-group-action]')) button.addEventListener("click", event => {
    event.stopPropagation();
    const action = event.currentTarget.dataset.groupAction;
    const sourceGroup = event.currentTarget.closest(".editor-group").dataset.group;
    if (action === "split-right") splitEditorRight();
    else if (action === "merge") mergeEditorGroups();
    else if (action === "move-left") moveActiveEditor("left", sourceGroup);
    else if (action === "move-right") moveActiveEditor("right", sourceGroup);
  });
  $("#editor-group-divider").addEventListener("pointerdown", event => {
    const handle = event.currentTarget, root = $("#editor-groups"), rect = root.getBoundingClientRect(); handle.setPointerCapture(event.pointerId);
    const move = moveEvent => { state.editorGroups.ratio = Math.max(25, Math.min(75, ((moveEvent.clientX - rect.left) / rect.width) * 100)); renderEditorGroups(); handle.setAttribute("aria-valuenow", String(Math.round(state.editorGroups.ratio))); };
    const up = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); requestAnimationFrame(renderEdges); };
    handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", up);
  });
  $("#editor-group-divider").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return; event.preventDefault();
    state.editorGroups.ratio = Math.max(25, Math.min(75, state.editorGroups.ratio + (event.key === "ArrowRight" ? 3 : -3)));
    renderEditorGroups(); event.currentTarget.setAttribute("aria-valuenow", String(Math.round(state.editorGroups.ratio)));
  });
  $("#workspace-resizer").addEventListener("pointerdown", event => {
    const handle = event.currentTarget; handle.setPointerCapture(event.pointerId);
    const move = moveEvent => { const width = Math.max(150, Math.min(440, moveEvent.clientX - 45)); document.documentElement.style.setProperty("--explorer-w", `${width}px`); handle.setAttribute("aria-valuenow", String(Math.round(width))); };
    const up = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); };
    handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", up);
  });
  $("#workspace-resizer").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return; event.preventDefault();
    const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--explorer-w")) || 230;
    const width = Math.max(150, Math.min(440, current + (event.key === "ArrowRight" ? 16 : -16)));
    document.documentElement.style.setProperty("--explorer-w", `${width}px`); event.currentTarget.setAttribute("aria-valuenow", String(Math.round(width)));
  });
  $("#repl-resizer").addEventListener("pointerdown", event => {
    const handle = event.currentTarget, main = $("#repl-main"), rect = main.getBoundingClientRect(); handle.setPointerCapture(event.pointerId);
    const move = moveEvent => { const size = Math.max(110, Math.min(rect.height - 150, moveEvent.clientY - rect.top - 38)); main.style.setProperty("--repl-editor-size", `${size}px`); handle.setAttribute("aria-valuemax", String(Math.max(110, Math.round(rect.height - 150)))); handle.setAttribute("aria-valuenow", String(Math.round(size))); };
    const up = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); };
    handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", up);
  });
  $("#repl-resizer").addEventListener("keydown", event => {
    if (!["ArrowUp", "ArrowDown"].includes(event.key)) return; event.preventDefault();
    const main = $("#repl-main"), editor = $("#repl-editor");
    const next = Math.max(110, Math.min(main.clientHeight - 150, editor.clientHeight + (event.key === "ArrowDown" ? 16 : -16)));
    main.style.setProperty("--repl-editor-size", `${next}px`); event.currentTarget.setAttribute("aria-valuemax", String(Math.max(110, Math.round(main.clientHeight - 150)))); event.currentTarget.setAttribute("aria-valuenow", String(Math.round(next)));
  });
  $("#agent-form").addEventListener("submit", event => {
    event.preventDefault(); const input = $("#agent-input"); const question = input.value.trim(); if (!question) return; input.value = ""; askAgent(question);
  });
  document.addEventListener("keydown", event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") { event.preventDefault(); runEditorFlowAction(); }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "o") { event.preventDefault(); openProjectDialog(); }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#global-search").focus(); }
    if ((event.metaKey || event.ctrlKey) && event.key === "\\") { event.preventDefault(); splitEditorRight(); }
    if (event.key === "Escape") {
      const workspaceMenuWasOpen = !$("#workspace-menu").hidden;
      closeMenus(); if (workspaceMenuWasOpen) closeWorkspaceMenu({ restoreFocus: true }); $(".workspace").classList.remove("editor-focused"); $("#editor-focus").textContent = "Focus"; $("#editor-focus").setAttribute("aria-pressed", "false");
      $("#repl-main").classList.remove("results-focused"); $("#repl-results-focus").textContent = "Focus results"; $("#repl-results-focus").setAttribute("aria-pressed", "false");
      $("#agent-panel").classList.remove("open");
    }
  });
  document.addEventListener("click", event => { if (!event.target.closest(".menu-group") && !event.target.closest(".split-button") && !event.target.closest(".workspace-switcher")) closeMenus(); });
  window.addEventListener("beforeunload", guardWorkspaceUnload);
  window.addEventListener("resize", () => requestAnimationFrame(renderEdges));
  if (window.ResizeObserver) new ResizeObserver(() => requestAnimationFrame(renderEdges)).observe($("#graph-canvas"));
  renderEditorGroups();
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { createEditorGroupState, editorOpen, editorClose, editorSplit, editorMove, editorMoveActive, editorMerge, createEditorFlowState, editorFlowAction, editorFlowTransition, nextRequestGeneration, matchesValidationContext, matchesApplyContext, rebaseEditorAfterApply, rebaseDirtyDraftAfterPrepare, captureDraftContext };
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  window.studio = {
    state,
    refreshGraph,
    openProjectDialog,
    createDraftFromEditor,
    openNewDraftDialog,
    submitCliDraft,
    openSqlDraftDialog,
    sendSqlToRepl,
    setMainView,
    splitEditorRight,
    moveActiveEditor,
    mergeEditorGroups,
    closeEditorView,
    startRepl,
    runReplQuery,
    selectNode,
    startJob,
    save: saveCurrentFile,
    configureAgent,
    askAgent,
    cancelAgent,
    openDraftReview,
    validateCurrentDraft,
    applyCurrentDraft,
    snapshot: () => ({ project: state.project?.project, files: state.files.length, nodes: state.graph.nodes.length, edges: state.graph.edges.length, selectedNode: state.selectedNode, selectedFile: state.selectedFile, dirty: state.dirty, agentConfigured: state.agent.configured, agentReady: state.agent.ready, agentProjectVerified: state.agent.project_verified, agentModel: state.agent.model, activeJob: state.activeJob, editorGroups: state.editorGroups }),
  };
  bindEvents();
  initialize();
}
