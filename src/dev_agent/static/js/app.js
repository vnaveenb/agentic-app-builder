/**
 * AI Dev Agent — Conversational Studio (v2)
 * Phase-based state machine with progressive disclosure.
 */
(function () {
"use strict";

// ── State ──
let sessionId = null;
let eventSource = null;
let editor = null;
let inlineEditor = null;
let editorModels = {};
let inlineModels = {};
let currentPhase = "ideation";
let buildStartTime = null;
let buildTimerInterval = null;
let agentTimes = {};

// ── DOM Refs ──
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const appShell = $("#appShell");
const statusBar = $("#statusBar");
const phaseContainer = $("#phaseContainer");
const toastHost = $("#toastHost");

// Ideation
const ideaInput = $("#ideaInput");
const generateBtn = $("#generateBtn");
const runtimeSelect = $("#runtimeSelect");
const backendSelect = $("#backendSelect");
const iterSlider = $("#iterSlider");
const iterValue = $("#iterValue");

// Building
const buildStream = $("#buildStream");
const planningStream = $("#planningStream");
const inlineEditorArea = $("#inlineEditorArea");
const inlineFileTabs = $("#inlineFileTabs");
const inlineMonaco = $("#inlineMonaco");

// Complete
const fileTabs = $("#fileTabs");
const monacoContainer = $("#monacoContainer");
const completeSummary = $("#completeSummary");
const downloadBtn = $("#downloadBtn");
const iterateBtn = $("#iterateBtn");
const iteratePanel = $("#iteratePanel");
const iterateInput = $("#iterateInput");
const iterateSubmitBtn = $("#iterateSubmitBtn");

// Preview
const launchPreviewBtn = $("#launchPreviewBtn");
const stopPreviewBtn = $("#stopPreviewBtn");
const refreshPreviewBtn = $("#refreshPreviewBtn");
const previewFrame = $("#previewFrame");
const previewOverlay = $("#previewOverlay");

// Sidebar
const providerSelect = $("#providerSelect");
const modelSelect = $("#modelSelect");
const providerBadge = $("#providerBadge");
const manageKeysBtn = $("#manageKeysBtn");
const sessionList = $("#sessionList");
const memorySection = $("#memorySection");
const memoryList = $("#memoryList");
const memoryCount = $("#memoryCount");
const newSessionBtn = $("#newSessionBtn");
const sidebarToggle = $("#sidebarToggle");

// Status
const buildTimer = $("#buildTimer");
const iterationBadge = $("#iterationBadge");

// Modal
const keysModal = $("#keysModal");
const keysList = $("#keysList");

// ── Utilities ──
function escape(s) {
    if (!s) return "";
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
}

function fmtMs(ms) {
    if (ms < 1000) return (ms / 1000).toFixed(1) + "s";
    if (ms < 60000) return (ms / 1000).toFixed(1) + "s";
    const m = Math.floor(ms / 60000);
    const s = ((ms % 60000) / 1000).toFixed(0);
    return `${m}m ${s}s`;
}

function toast(msg, type = "info") {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = msg;
    toastHost.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => {
        el.classList.remove("show");
        setTimeout(() => el.remove(), 300);
    }, 4000);
}

function genId() {
    return crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2);
}

// ── Phase Management ──
function setPhase(phase) {
    if (phase === currentPhase) return;
    currentPhase = phase;
    $$(".phase").forEach((el) => el.classList.remove("active"));
    const target = $(`#phase${phase.charAt(0).toUpperCase() + phase.slice(1)}`);
    if (target) target.classList.add("active");

    if (phase === "ideation") {
        statusBar.classList.remove("visible");
    } else {
        statusBar.classList.add("visible");
    }

    updateTabTitle();
}

function updateTabTitle() {
    const base = "AI Dev Agent";
    if (currentPhase === "building") document.title = `(Building...) ${base}`;
    else if (currentPhase === "complete") document.title = `(Done!) ${base}`;
    else document.title = base;
}

// ── Progress Stepper ──
const AGENTS_ORDER = ["planner", "developer", "designer", "tester", "reviewer"];

function setStepState(agent, state) {
    const step = $(`.step[data-agent="${agent}"]`);
    if (!step) return;
    step.classList.remove("running", "done");
    if (state) step.classList.add(state);

    const idx = AGENTS_ORDER.indexOf(agent);
    if (idx > 0) {
        const line = $(`.step-line[data-after="${AGENTS_ORDER[idx - 1]}"]`);
        if (line) {
            line.classList.remove("done", "active");
            if (state === "done") line.classList.add("done");
            else if (state === "running") line.classList.add("active");
        }
    }
}

function setStepTimer(agent, text) {
    const el = $(`#timer-${agent}`);
    if (el) el.textContent = text;
}

function resetStepper() {
    AGENTS_ORDER.forEach((a) => {
        setStepState(a, null);
        setStepTimer(a, "");
    });
}

// ── Build Timer ──
function startBuildTimer() {
    buildStartTime = performance.now();
    buildTimer.textContent = "0.0s";
    buildTimerInterval = setInterval(() => {
        buildTimer.textContent = fmtMs(performance.now() - buildStartTime);
    }, 100);
}

function stopBuildTimer() {
    if (buildTimerInterval) clearInterval(buildTimerInterval);
    buildTimerInterval = null;
}

// ── Agent Thinking Blocks ──
function createAgentBlock(agent, status = "running") {
    const block = document.createElement("div");
    block.className = "agent-block expanded";
    block.dataset.agent = agent;
    block.dataset.status = status;
    block.innerHTML = `
        <div class="agent-block-header">
            <span class="agent-dot"></span>
            <span class="agent-name">${escape(agent)}</span>
            <span class="agent-status">${status === "running" ? "thinking..." : ""}</span>
            <span class="agent-timer-text"></span>
            <span class="agent-toggle">&#9662;</span>
        </div>
        <div class="agent-block-content">
            <pre class="agent-stream"></pre>
        </div>
    `;
    block.querySelector(".agent-block-header").onclick = () => {
        block.classList.toggle("expanded");
    };
    return block;
}

function getActiveStream() {
    if (currentPhase === "planning") return planningStream;
    return buildStream;
}

// ── Plan Card ──
function renderPlanCard(plan, targetEl) {
    const card = document.createElement("div");
    card.className = "plan-card";

    const tasks = (plan.tasks || []).map(
        (t) => `<div class="plan-task"><span class="plan-task-dot"></span><span>${escape(typeof t === "string" ? t : t.description || t.text || JSON.stringify(t))}</span></div>`
    ).join("");

    const files = (plan.files || []).map(
        (f) => `<span class="plan-chip file">${escape(f)}</span>`
    ).join("");

    const techStack = (plan.tech_stack || []).map(
        (t) => `<span class="plan-chip">${escape(t)}</span>`
    ).join("");

    card.innerHTML = `
        <div class="plan-card-header">
            <span class="plan-card-title">${escape(plan.app_name || "Your App")}</span>
            <span class="plan-card-runtime">${escape(plan.runtime || "auto")}</span>
        </div>
        ${plan.architecture ? `<div class="plan-section"><span class="plan-label">Architecture</span><p class="plan-text">${escape(plan.architecture)}</p></div>` : ""}
        ${techStack ? `<div class="plan-section"><span class="plan-label">Tech Stack</span><div class="plan-chips">${techStack}</div></div>` : ""}
        ${tasks ? `<div class="plan-section"><span class="plan-label">Tasks</span><div class="plan-tasks">${tasks}</div></div>` : ""}
        ${files ? `<div class="plan-section"><span class="plan-label">Files</span><div class="plan-chips">${files}</div></div>` : ""}
        <div class="plan-actions">
            <button class="btn-approve" id="btnApprovePlan">Approve & Build</button>
            <button class="btn-reject" id="btnRejectPlan">Reject</button>
        </div>
    `;

    targetEl.appendChild(card);

    card.querySelector("#btnApprovePlan").onclick = () => approvePlan(card);
    card.querySelector("#btnRejectPlan").onclick = () => rejectPlan(card);
}

async function approvePlan(card) {
    card.querySelector(".plan-actions").innerHTML = `<span style="color:var(--accent-emerald);font-weight:600;">Approved — building...</span>`;
    try {
        await fetch(`/approve-plan/${sessionId}`, { method: "POST" });
        setPhase("building");
    } catch (err) {
        toast("Failed to approve plan", "error");
    }
}

function rejectPlan(card) {
    card.querySelector(".plan-actions").innerHTML = `<span style="color:var(--accent-rose);font-weight:600;">Rejected</span>`;
    if (eventSource) eventSource.close();
    toast("Plan rejected — modify your idea and try again", "info");
    setTimeout(() => setPhase("ideation"), 1500);
}

// ── Monaco Editor ──
function initMonaco(callback) {
    if (window.monaco) { callback(); return; }
    require.config({ paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/vs" } });
    require(["vs/editor/editor.main"], () => {
        monaco.editor.defineTheme("studio-dark", {
            base: "vs-dark",
            inherit: true,
            rules: [],
            colors: { "editor.background": "#0a0a0c", "editor.lineHighlightBackground": "#1a1a1e" },
        });
        callback();
    });
}

function loadFilesIntoEditor(files, container, tabsContainer, isInline = false) {
    initMonaco(() => {
        const models = isInline ? inlineModels : editorModels;

        Object.values(models).forEach((m) => m.dispose());
        if (isInline) inlineModels = {};
        else editorModels = {};
        tabsContainer.innerHTML = "";

        if (!files || Object.keys(files).length === 0) return;

        let currentEditor = isInline ? inlineEditor : editor;
        if (!currentEditor) {
            currentEditor = monaco.editor.create(container, {
                theme: "studio-dark",
                fontSize: 13,
                minimap: { enabled: false },
                fontFamily: "JetBrains Mono, monospace",
                automaticLayout: true,
                padding: { top: 12 },
            });
            if (isInline) inlineEditor = currentEditor;
            else editor = currentEditor;
        }

        const extMap = { py: "python", js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", html: "html", css: "css", json: "json", md: "markdown" };
        const names = Object.keys(files);
        const targetModels = isInline ? inlineModels : editorModels;

        names.forEach((fname) => {
            const ext = fname.split(".").pop();
            const lang = extMap[ext] || "plaintext";
            const model = monaco.editor.createModel(files[fname], lang, monaco.Uri.parse("file:///" + fname));
            targetModels[fname] = model;

            const tab = document.createElement("div");
            tab.className = "file-tab";
            tab.textContent = fname;
            tab.dataset.file = fname;
            tab.onclick = () => switchFileInEditor(fname, tabsContainer, currentEditor, targetModels);
            tabsContainer.appendChild(tab);
        });

        if (names.length) switchFileInEditor(names[0], tabsContainer, currentEditor, targetModels);
    });
}

function switchFileInEditor(fname, tabsContainer, ed, models) {
    tabsContainer.querySelectorAll(".file-tab").forEach((t) => t.classList.remove("active"));
    const tab = tabsContainer.querySelector(`[data-file="${fname}"]`);
    if (tab) tab.classList.add("active");
    if (models[fname] && ed) ed.setModel(models[fname]);
}

// ── SSE Connection ──
function connectSSE() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/stream/${sessionId}`);
    let currentBlock = null;
    let currentAgent = "";

    eventSource.onmessage = ({ data }) => {
        let ev;
        try { ev = JSON.parse(data); } catch { return; }

        switch (ev.event) {
            case "agent_start":
                handleAgentStart(ev);
                currentAgent = ev.agent;
                currentBlock = createAgentBlock(ev.agent);
                getActiveStream().appendChild(currentBlock);
                getActiveStream().scrollTop = getActiveStream().scrollHeight;
                break;

            case "llm_chunk":
                if (currentBlock && ev.agent === currentAgent) {
                    const stream = currentBlock.querySelector(".agent-stream");
                    if (stream) {
                        stream.textContent += ev.chunk;
                        stream.scrollTop = stream.scrollHeight;
                    }
                }
                break;

            case "agent_complete":
                handleAgentComplete(ev, currentBlock);
                currentBlock = null;
                break;

            case "agent_output":
                handleAgentOutput(ev);
                break;

            case "plan_ready":
                handlePlanReady(ev.data || ev);
                break;

            case "files_update":
                handleFilesUpdate(ev);
                break;

            case "pipeline_done":
                handlePipelineDone(ev);
                break;

            case "error":
                handleError(ev);
                break;
        }
    };

    eventSource.onerror = () => {
        console.warn("SSE connection lost");
    };
}

function handleAgentStart(ev) {
    setStepState(ev.agent, "running");
    agentTimes[ev.agent] = { start: performance.now() };
    setStepTimer(ev.agent, "...");
}

function handleAgentComplete(ev, block) {
    setStepState(ev.agent, "done");
    const rec = agentTimes[ev.agent];
    if (rec && rec.start != null) {
        rec.elapsed = performance.now() - rec.start;
        setStepTimer(ev.agent, fmtMs(rec.elapsed));
    }

    if (block) {
        block.dataset.status = "done";
        block.classList.remove("expanded");
        const statusEl = block.querySelector(".agent-status");
        if (statusEl) statusEl.textContent = rec ? fmtMs(rec.elapsed) : "done";
        const timerEl = block.querySelector(".agent-timer-text");
        if (timerEl) timerEl.textContent = "";
    }
}

function handleAgentOutput(ev) {
    if (ev.agent === "developer" && ev.data && ev.data.files_generated) {
        const count = Array.isArray(ev.data.files_generated) ? ev.data.files_generated.length : Object.keys(ev.data.files_generated).length;
        const summary = document.createElement("div");
        summary.className = "agent-summary";
        summary.innerHTML = `<span class="file-count">${count} files</span> generated`;
        getActiveStream().appendChild(summary);
    }
}

function handlePlanReady(plan) {
    setStepState("planner", "done");
    const rec = agentTimes["planner"];
    if (rec && rec.start) {
        rec.elapsed = performance.now() - rec.start;
        setStepTimer("planner", fmtMs(rec.elapsed));
    }

    setPhase("planning");
    renderPlanCard(plan, planningStream);
}

function handleFilesUpdate(ev) {
    if (!ev.files || Object.keys(ev.files).length === 0) return;

    if (currentPhase === "building") {
        inlineEditorArea.style.display = "flex";
        const count = Object.keys(ev.files).length;
        $("#inlineFileCount").textContent = `${count} file${count > 1 ? "s" : ""}`;
        loadFilesIntoEditor(ev.files, inlineMonaco, inlineFileTabs, true);
    }
}

function handlePipelineDone(ev) {
    stopBuildTimer();

    const files = ev.files;
    if (files && Object.keys(files).length > 0) {
        transitionToComplete(files, ev);
    } else {
        fetch(`/files/${sessionId}`)
            .then((r) => r.json())
            .then((data) => {
                if (data.files && Object.keys(data.files).length > 0) {
                    transitionToComplete(data.files, ev);
                } else {
                    toast("Build complete but no files were generated", "error");
                    setPhase("ideation");
                }
            })
            .catch(() => {
                toast("Build complete but failed to load files", "error");
            });
    }

    if (eventSource) eventSource.close();
    notifyBuildComplete();
}

function transitionToComplete(files, ev) {
    setPhase("complete");

    const totalTime = buildStartTime ? fmtMs(performance.now() - buildStartTime) : "";
    completeSummary.innerHTML = `
        <span class="summary-status">Build complete ${totalTime ? `(${totalTime})` : ""}</span>
        <span class="summary-agents">${AGENTS_ORDER.map((a) => `<span style="color:var(--agent-${a})">&#10003; ${a}</span>`).join(" ")}</span>
    `;

    loadFilesIntoEditor(files, monacoContainer, fileTabs, false);
    launchPreviewBtn.disabled = false;
    downloadBtn.disabled = false;
}

function handleError(ev) {
    stopBuildTimer();
    toast(ev.message || "Pipeline error", "error");
    if (eventSource) eventSource.close();
}

// ── Notifications ──
function notifyBuildComplete() {
    updateTabTitle();
    if ("Notification" in window && Notification.permission === "granted") {
        new Notification("Build Complete", { body: "Your app is ready to preview." });
    }
}

function requestNotificationPermission() {
    if ("Notification" in window && Notification.permission === "default") {
        Notification.requestPermission();
    }
}

// ── Generate ──
async function handleGenerate() {
    const idea = ideaInput.value.trim();
    if (!idea) { toast("Enter an app idea", "error"); return; }

    sessionId = genId();
    generateBtn.disabled = true;

    resetStepper();
    planningStream.innerHTML = "";
    buildStream.innerHTML = "";
    inlineEditorArea.style.display = "none";

    const provider = providerSelect.value;
    const model = modelSelect.value;

    const body = {
        idea,
        runtime: runtimeSelect.value,
        max_iterations: parseInt(iterSlider.value),
        backend: backendSelect.value,
        provider: provider || undefined,
        model: model || undefined,
        client_id: getClientId(),
    };

    try {
        const res = await fetch("/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        sessionId = data.session_id;

        addSessionToList(sessionId, idea);
        setPhase("planning");
        startBuildTimer();
        statusBar.classList.add("visible");
        connectSSE();
        requestNotificationPermission();
    } catch (err) {
        toast("Failed to start generation: " + err.message, "error");
    } finally {
        generateBtn.disabled = false;
    }
}

// ── Iterate ──
async function handleIterate() {
    const feedback = iterateInput.value.trim();
    if (!feedback) { toast("Enter feedback", "error"); return; }

    iterateSubmitBtn.disabled = true;
    setPhase("building");
    buildStream.innerHTML = "";
    inlineEditorArea.style.display = "none";
    resetStepper();
    startBuildTimer();

    try {
        await fetch(`/iterate/${sessionId}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ feedback, client_id: getClientId() }),
        });
        connectSSE();
    } catch (err) {
        toast("Iterate failed: " + err.message, "error");
    } finally {
        iterateSubmitBtn.disabled = false;
        iteratePanel.style.display = "none";
    }
}

// ── Sidebar: Sessions ──
function addSessionToList(id, idea) {
    const empty = sessionList.querySelector(".session-empty");
    if (empty) empty.remove();

    const item = document.createElement("div");
    item.className = "session-item active";
    item.innerHTML = `<span class="session-dot"></span><span>${escape(idea.substring(0, 30))}${idea.length > 30 ? "..." : ""}</span>`;

    sessionList.querySelectorAll(".session-item").forEach((i) => i.classList.remove("active"));
    sessionList.prepend(item);
}

// ── Sidebar: Config ──
async function loadConfig() {
    try {
        const clientId = getClientId();
        const res = await fetch(`/providers?client_id=${encodeURIComponent(clientId)}`);
        const cfg = await res.json();

        providerSelect.innerHTML = "";
        (cfg.providers || []).forEach((p) => {
            const opt = document.createElement("option");
            opt.value = p.id;
            opt.textContent = p.label;
            if (p.id === cfg.default_provider) opt.selected = true;
            providerSelect.appendChild(opt);
        });

        updateModels(cfg);
        providerBadge.textContent = `${cfg.default_provider || "gemini"} / ${cfg.default_model || ""}`;
    } catch {
        providerBadge.textContent = "Config unavailable";
    }
}

function updateModels(cfg) {
    modelSelect.innerHTML = "";
    const provider = providerSelect.value;
    const prov = (cfg.providers || []).find((p) => p.id === provider);
    const models = prov ? prov.models : [];
    models.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.label;
        if (m.default) opt.selected = true;
        modelSelect.appendChild(opt);
    });
}

// Store config for reuse on provider change
let _cachedConfig = null;

// ── Keys Modal ──
function openKeysModal() {
    keysModal.showModal();
    loadKeysList();
}

async function loadKeysList() {
    try {
        const res = await fetch("/providers");
        const data = await res.json();
        keysList.innerHTML = (data.providers || []).map((p) => `
            <div style="display:flex;align-items:center;gap:8px;padding:8px;background:var(--bg-surface-raised);border-radius:var(--radius-sm);">
                <span style="flex:1;font-size:var(--text-sm);">${escape(p.name)}</span>
                <span style="font-size:var(--text-xs);color:${p.has_key ? "var(--accent-emerald)" : "var(--text-muted)"};">${p.has_key ? "Saved" : "Not set"}</span>
                <input type="password" placeholder="API key" style="flex:1;padding:4px 8px;background:var(--bg-input);border:1px solid var(--border-default);border-radius:4px;font-size:var(--text-xs);" data-provider="${p.id}">
                <button class="btn-sm" onclick="window._saveKey('${p.id}', this)">Save</button>
            </div>
        `).join("");
    } catch {
        keysList.innerHTML = "<p style='color:var(--text-muted);font-size:var(--text-sm);'>Failed to load providers</p>";
    }
}

window._saveKey = async function (provider, btn) {
    const input = btn.previousElementSibling;
    const key = input.value.trim();
    if (!key) return;
    try {
        await fetch(`/providers/${provider}/key`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: key, client_id: getClientId() }),
        });
        input.value = "";
        toast("Key saved", "success");
        loadKeysList();
    } catch {
        toast("Failed to save key", "error");
    }
};

// ── Memories ──
async function loadMemories() {
    if (!sessionId) return;
    try {
        const res = await fetch(`/memory/${sessionId}`);
        const data = await res.json();
        const memories = data.memories || [];
        if (memories.length === 0) { memorySection.style.display = "none"; return; }

        memorySection.style.display = "block";
        memoryCount.textContent = memories.length;
        memoryList.innerHTML = memories.slice(0, 5).map((m) =>
            `<div class="memory-item"><strong>${escape(m.key)}</strong>: ${escape(m.value)}</div>`
        ).join("");
    } catch {
        memorySection.style.display = "none";
    }
}

// ── Preview ──
async function startPreview() {
    if (!sessionId) return;
    launchPreviewBtn.disabled = true;
    previewFrame.src = "about:blank";
    previewFrame.style.display = "none";
    previewOverlay.textContent = "Preparing preview...";
    previewOverlay.classList.remove("hidden");
    try {
        const res = await fetch(`/preview/${sessionId}/start`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || data.message || "Preview failed");
        }
        const previewUrl = data.url.endsWith("/") ? data.url : `${data.url}/`;
        previewFrame.src = previewUrl;
        previewFrame.style.display = "block";
        previewOverlay.classList.add("hidden");
        stopPreviewBtn.disabled = false;
        refreshPreviewBtn.disabled = false;
        toast(`Preview started${data.mode ? ` (${data.mode})` : ""}`, "success");
    } catch (err) {
        previewFrame.src = "about:blank";
        previewFrame.style.display = "none";
        previewOverlay.textContent = err.message || "Preview failed";
        previewOverlay.classList.remove("hidden");
        toast(`Preview failed: ${err.message || "unknown error"}`, "error");
        launchPreviewBtn.disabled = false;
        stopPreviewBtn.disabled = true;
        refreshPreviewBtn.disabled = true;
    }
}

async function stopPreview() {
    if (!sessionId) return;
    try { await fetch(`/preview/${sessionId}/stop`, { method: "POST" }); } catch {}
    previewFrame.src = "about:blank";
    previewFrame.style.display = "none";
    previewOverlay.textContent = 'Click "Start Preview" to launch';
    previewOverlay.classList.remove("hidden");
    launchPreviewBtn.disabled = false;
    stopPreviewBtn.disabled = true;
    refreshPreviewBtn.disabled = true;
}

// ── Download ──
async function handleDownload() {
    if (!sessionId) return;
    try {
        const res = await fetch(`/files/${sessionId}`);
        const data = await res.json();
        const files = data.files || {};
        if (Object.keys(files).length === 0) { toast("No files to download", "error"); return; }

        // Load JSZip dynamically
        if (!window.JSZip) {
            const script = document.createElement("script");
            script.src = "https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js";
            document.head.appendChild(script);
            await new Promise((resolve) => { script.onload = resolve; });
        }
        const zip = new JSZip();
        Object.entries(files).forEach(([name, content]) => zip.file(name, content));
        const blob = await zip.generateAsync({ type: "blob" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `project-${sessionId.slice(0, 8)}.zip`;
        a.click();
        URL.revokeObjectURL(url);
    } catch {
        toast("Download failed", "error");
    }
}

// ── Client ID ──
function getClientId() {
    let id = localStorage.getItem("devagent_client_id");
    if (!id) {
        id = genId();
        localStorage.setItem("devagent_client_id", id);
    }
    return id;
}

// ── Event Listeners ──
generateBtn.onclick = handleGenerate;
ideaInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) handleGenerate();
});

iterSlider.oninput = () => { iterValue.textContent = iterSlider.value; };

iterateBtn.onclick = () => {
    iteratePanel.style.display = iteratePanel.style.display === "none" ? "flex" : "none";
};
iterateSubmitBtn.onclick = handleIterate;

$$(".template-chip").forEach((chip) => {
    chip.onclick = () => { ideaInput.value = chip.dataset.idea; };
});

manageKeysBtn.onclick = openKeysModal;
$("#keysModalClose").onclick = () => keysModal.close();
sidebarToggle.onclick = () => appShell.classList.toggle("sidebar-collapsed");
newSessionBtn.onclick = () => { setPhase("ideation"); sessionId = null; };

launchPreviewBtn.onclick = startPreview;
stopPreviewBtn.onclick = stopPreview;
refreshPreviewBtn.onclick = () => { if (previewFrame.src) previewFrame.src = previewFrame.src; };

downloadBtn.onclick = handleDownload;

providerSelect.onchange = async () => {
    try {
        const clientId = getClientId();
        const res = await fetch(`/providers?client_id=${encodeURIComponent(clientId)}`);
        const cfg = await res.json();
        updateModels(cfg);
        providerBadge.textContent = `${providerSelect.value} / ${modelSelect.value || ""}`;
    } catch {}
};

// ── Init ──
loadConfig();
setPhase("ideation");

})();
