/* AI Dev Agent — front-end controller.
 * Extracted from the former inline <script> and extended with: live agent
 * timing, a test-case explorer, real preview-error surfacing, in-iframe console
 * capture, a memory manager, and a richer version timeline. No build step. */
(function () {
    "use strict";

    // ─── State ───
    let sessionId = null;
    let eventSource = null;
    let editor = null;
    let editorModels = {};
    let activeFile = null;
    let completedAgents = 0;

    // Timing
    const agentTimes = {};       // agent → { start, elapsedMs }
    let buildStartTime = null;
    let buildTimerInterval = null;

    // ─── DOM refs ───
    const appLayout = document.getElementById("appLayout");
    const ideaInput = document.getElementById("ideaInput");
    const runtimeSelect = document.getElementById("runtimeSelect");
    const iterSlider = document.getElementById("iterSlider");
    const iterValue = document.getElementById("iterValue");
    const generateBtn = document.getElementById("generateBtn");
    const providerBadge = document.getElementById("providerBadge");
    const sessionInfo = document.getElementById("sessionInfo");
    const sessionIdEl = document.getElementById("sessionId");
    const iterateSection = document.getElementById("iterateSection");
    const iterateInput = document.getElementById("iterateInput");
    const iterateBtn = document.getElementById("iterateBtn");
    const eventLog = document.getElementById("eventLog");
    const downloadBtn = document.getElementById("downloadBtn");
    const monacoContainer = document.getElementById("monacoContainer");
    const codePlaceholder = document.getElementById("codePlaceholder");
    const fileTabs = document.getElementById("fileTabs");
    const saveStatus = document.getElementById("saveStatus");
    const previewOverlay = document.getElementById("previewOverlay");
    const previewFrame = document.getElementById("previewFrame");
    const previewBanner = document.getElementById("previewBanner");
    const terminalOutput = document.getElementById("terminalOutput");
    const chatMessages = document.getElementById("chatMessages");
    const chatInput = document.getElementById("chatInput");
    const chatSendBtn = document.getElementById("chatSendBtn");
    const memorySection = document.getElementById("memorySection");
    const memoryCards = document.getElementById("memoryCards");
    const versionTimeline = document.getElementById("versionTimeline");
    const timelineTrack = document.getElementById("timelineTrack");
    const buildTimer = document.getElementById("buildTimer");
    const toastHost = document.getElementById("toastHost");

    let currentStreamAgent = null;
    let currentDetails = null;
    let currentDetailsContent = null;
    let activeAgentFilter = null;

    // ─── Toast helper ───
    function toast(msg, kind) {
        if (!toastHost) return;
        const t = document.createElement("div");
        t.className = "toast" + (kind ? " " + kind : "");
        t.textContent = msg;
        toastHost.appendChild(t);
        requestAnimationFrame(() => t.classList.add("show"));
        setTimeout(() => {
            t.classList.remove("show");
            setTimeout(() => t.remove(), 300);
        }, 3200);
    }

    function escape(s) {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function fmtMs(ms) {
        if (ms == null) return "";
        if (ms < 1000) return ms + "ms";
        return (ms / 1000).toFixed(1) + "s";
    }

    // ─── Left Panel Tabs ───
    document.querySelectorAll(".left-tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".left-tab-btn").forEach((b) => b.classList.remove("active"));
            document.querySelectorAll(".left-tab-content").forEach((c) => c.classList.remove("active"));
            btn.classList.add("active");
            document.getElementById(btn.dataset.leftTarget).classList.add("active");
        });
    });

    // ─── Advanced Toggle ───
    const advancedToggle = document.getElementById("advancedToggle");
    const advancedContent = document.getElementById("advancedContent");
    advancedToggle.addEventListener("click", () => {
        advancedToggle.classList.toggle("open");
        advancedContent.classList.toggle("open");
    });

    // ─── Chat ───
    async function sendChat() {
        const msg = chatInput.value.trim();
        if (!msg || !sessionId) return;
        chatInput.value = "";
        chatSendBtn.disabled = true;

        const userBubble = document.createElement("div");
        userBubble.className = "chat-bubble user";
        userBubble.textContent = msg;
        chatMessages.appendChild(userBubble);
        chatMessages.scrollTop = chatMessages.scrollHeight;

        try {
            const res = await fetch(`/chat/${sessionId}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: msg }),
            });
            const data = await res.json();
            const assistantBubble = document.createElement("div");
            assistantBubble.className = "chat-bubble assistant";
            assistantBubble.textContent = data.response || data.message || "No response";
            chatMessages.appendChild(assistantBubble);
            chatMessages.scrollTop = chatMessages.scrollHeight;

            if (data.should_iterate) {
                toast("Chat triggered an iteration", "info");
                resetUI(true);
                connectSSE();
            }
        } catch (e) {
            const errBubble = document.createElement("div");
            errBubble.className = "chat-bubble assistant";
            errBubble.textContent = "Error: could not reach the server.";
            chatMessages.appendChild(errBubble);
        }
        chatSendBtn.disabled = false;
    }

    chatSendBtn.addEventListener("click", sendChat);
    chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendChat();
        }
    });

    // ─── Memory manager ───
    async function fetchMemories() {
        if (!sessionId) return;
        try {
            const res = await fetch(`/memory?session_id=${sessionId}`);
            if (!res.ok) return;
            const data = await res.json();
            const memories = data.memories || [];
            if (memories.length === 0) {
                memorySection.style.display = "none";
                return;
            }
            memorySection.style.display = "";
            memoryCards.innerHTML = "";
            memories.slice(0, 8).forEach((m) => {
                const card = document.createElement("div");
                card.className = "memory-card";
                const score = m.relevance_score != null
                    ? `<span class="memory-score" title="relevance">${Number(m.relevance_score).toFixed(2)}</span>`
                    : "";
                card.innerHTML =
                    `<div class="memory-body"><span class="memory-key">${escape(m.key || m.category)}:</span> ${escape(m.value)}</div>` +
                    `<div class="memory-meta">${score}` +
                    `<button class="memory-del" title="Forget" data-id="${m.id}">✕</button></div>`;
                memoryCards.appendChild(card);
            });
            memoryCards.querySelectorAll(".memory-del").forEach((b) => {
                b.onclick = async () => {
                    const id = b.dataset.id;
                    if (!id) return;
                    const r = await fetch(`/memory/${id}`, { method: "DELETE" });
                    if (r.ok) {
                        toast("Memory forgotten", "info");
                        fetchMemories();
                    }
                };
            });
        } catch (e) {
            /* silent */
        }
    }

    // ─── Version Timeline ───
    async function fetchHistory() {
        if (!sessionId) return;
        const res = await fetch(`/history/${sessionId}`);
        if (!res.ok) return;
        const data = await res.json();
        const versions = data.versions || [];
        if (versions.length === 0) {
            versionTimeline.style.display = "none";
            return;
        }
        versionTimeline.style.display = "";
        timelineTrack.innerHTML = "";
        versions.forEach((v, i) => {
            if (i > 0) {
                const line = document.createElement("div");
                line.className = "timeline-line" + (v.is_current ? "" : " filled");
                timelineTrack.appendChild(line);
            }
            const dot = document.createElement("div");
            dot.className = "timeline-dot" + (v.is_current ? " active" : "");
            const trig = v.trigger ? ` · ${v.trigger}` : "";
            dot.title = `v${v.version}${trig}: ${v.description || ""}`;
            dot.onclick = () => checkoutVersion(v.version);
            timelineTrack.appendChild(dot);
        });
    }

    // ─── Agent log filtering ───
    document.querySelectorAll(".agent-node").forEach((n) => {
        n.addEventListener("click", () => {
            const agent = n.dataset.agent;
            if (activeAgentFilter === agent) {
                activeAgentFilter = null;
                document.querySelectorAll(".agent-node").forEach((other) => (other.style.opacity = "1"));
            } else {
                activeAgentFilter = agent;
                document.querySelectorAll(".agent-node").forEach((other) => (other.style.opacity = "0.4"));
                n.style.opacity = "1";
            }
            document.querySelectorAll(".log-entry, .thinking-details").forEach((el) => {
                if (!activeAgentFilter || el.dataset.agent === activeAgentFilter) el.style.display = "";
                else el.style.display = "none";
            });
        });
    });

    // ─── Init ───
    fetch("/health")
        .then((r) => r.json())
        .then((d) => {
            providerBadge.innerHTML = "⚡ " + d.provider;
        })
        .catch(() => {
            providerBadge.textContent = "offline";
        });
    iterSlider.addEventListener("input", () => {
        iterValue.textContent = iterSlider.value;
    });

    // ─── Panel resizing (gutters) ───
    (function initResize() {
        const gutters = [document.getElementById("gutterLeft"), document.getElementById("gutterRight")];
        let dragging = null,
            startX = 0,
            startLeftW = 0,
            startCenterW = 0;
        const leftPanel = document.getElementById("leftPanel");
        const centerPanel = document.getElementById("centerPanel");
        const rightPanel = document.getElementById("rightPanel");

        function onDown(which, e) {
            dragging = which;
            startX = e.clientX || (e.touches && e.touches[0].clientX);
            startLeftW = leftPanel.getBoundingClientRect().width;
            startCenterW = centerPanel.getBoundingClientRect().width;
            rightPanel.getBoundingClientRect();
            gutters[which === "left" ? 0 : 1].classList.add("active");
        }
        function onMove(e) {
            if (!dragging) return;
            const x = e.clientX || (e.touches && e.touches[0].clientX);
            const delta = x - startX;
            if (dragging === "left") {
                appLayout.style.setProperty("--col-left", Math.max(200, startLeftW + delta) + "px");
                appLayout.style.setProperty("--col-center", Math.max(300, startCenterW - delta) + "px");
            } else {
                appLayout.style.setProperty("--col-center", Math.max(300, startCenterW + delta) + "px");
            }
            if (editor) requestAnimationFrame(() => editor.layout());
        }
        function onUp() {
            dragging = null;
            gutters.forEach((g) => g.classList.remove("active"));
        }
        gutters[0].addEventListener("mousedown", (e) => onDown("left", e));
        gutters[1].addEventListener("mousedown", (e) => onDown("right", e));
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    })();

    // ─── Workspace tabs ───
    document.querySelectorAll(".tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
            document.querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
            btn.classList.add("active");
            document.getElementById(btn.dataset.target).classList.add("active");
            if (btn.dataset.target === "codeContent" && editor) editor.layout();
        });
    });

    // ─── Monaco ───
    require.config({ paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/vs" } });
    require(["vs/editor/editor.main"], function () {
        monaco.editor.defineTheme("tech-dark", {
            base: "vs-dark",
            inherit: true,
            rules: [],
            colors: { "editor.background": "#121214", "editor.lineHighlightBackground": "#1f1f23" },
        });
    });

    function loadFiles(files) {
        if (!window.monaco) {
            setTimeout(() => loadFiles(files), 200);
            return;
        }
        if (!editor) {
            codePlaceholder.style.display = "none";
            monacoContainer.style.display = "block";
            editor = monaco.editor.create(monacoContainer, {
                theme: "tech-dark",
                fontSize: 13,
                minimap: { enabled: false },
                fontFamily: "JetBrains Mono",
                automaticLayout: true,
            });
            editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveFiles);
        }
        Object.values(editorModels).forEach((m) => m.dispose());
        editorModels = {};
        fileTabs.innerHTML = "";

        const mapExt = { py: "python", js: "javascript", html: "html", css: "css", json: "json", md: "markdown" };
        const names = Object.keys(files);
        names.forEach((fname) => {
            const ext = fname.split(".").pop();
            const lang = mapExt[ext] || "plaintext";
            const model = monaco.editor.createModel(files[fname], lang, monaco.Uri.parse("file:///" + fname));
            editorModels[fname] = model;
            model.onDidChangeContent(() => {
                const tab = document.querySelector(`[data-file="${fname}"]`);
                if (tab) tab.classList.add("unsaved");
            });
            const tab = document.createElement("div");
            tab.className = "file-tab";
            tab.textContent = fname;
            tab.dataset.file = fname;
            tab.onclick = () => switchFile(fname);
            fileTabs.appendChild(tab);
        });
        if (names.length) switchFile(names[0]);
    }

    function switchFile(fname) {
        activeFile = fname;
        editor.setModel(editorModels[fname]);
        document.querySelectorAll(".file-tab").forEach((t) => t.classList.toggle("active", t.dataset.file === fname));
    }

    async function saveFiles() {
        if (!sessionId || !editor) return;
        const payload = {};
        for (const [f, m] of Object.entries(editorModels)) payload[f] = m.getValue();
        document.querySelectorAll(".unsaved").forEach((t) => t.classList.remove("unsaved"));
        saveStatus.classList.add("visible");
        setTimeout(() => saveStatus.classList.remove("visible"), 2000);
        await fetch(`/files/${sessionId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ files: payload }),
        });
        toast("Saved & hot-reloaded", "info");
    }

    // ─── Build timer ───
    function startBuildTimer() {
        buildStartTime = performance.now();
        if (buildTimer) buildTimer.classList.add("visible");
        clearInterval(buildTimerInterval);
        buildTimerInterval = setInterval(() => {
            if (buildTimer && buildStartTime != null) {
                buildTimer.textContent = "⏱ " + fmtMs(performance.now() - buildStartTime);
            }
        }, 100);
    }
    function stopBuildTimer() {
        clearInterval(buildTimerInterval);
        if (buildTimer && buildStartTime != null) {
            buildTimer.textContent = "⏱ " + fmtMs(performance.now() - buildStartTime);
        }
    }

    function setAgentTimer(agent, text) {
        const node = document.getElementById(`node-${agent}`);
        if (!node) return;
        let el = node.querySelector(".agent-timer");
        if (!el) {
            el = document.createElement("div");
            el.className = "agent-timer";
            node.appendChild(el);
        }
        el.textContent = text;
    }

    // ─── Action handlers ───
    generateBtn.onclick = async () => {
        const idea = ideaInput.value.trim();
        if (!idea) return;
        generateBtn.disabled = true;
        generateBtn.textContent = "Initializing...";
        resetUI();
        startBuildTimer();
        const resp = await fetch("/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                idea,
                runtime: runtimeSelect.value,
                max_iterations: parseInt(iterSlider.value, 10),
                backend: document.getElementById("backendSelect").value,
            }),
        });
        const data = await resp.json();
        sessionId = data.session_id;
        sessionIdEl.textContent = sessionId;
        sessionInfo.classList.add("visible");
        connectSSE();
    };

    iterateBtn.onclick = async () => {
        const feedback = iterateInput.value.trim();
        if (!feedback) return;
        iterateBtn.disabled = true;
        generateBtn.disabled = true;
        resetUI(true);
        startBuildTimer();
        await fetch(`/iterate/${sessionId}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ feedback }),
        });
        connectSSE();
    };

    function resetUI(keepSession) {
        eventLog.innerHTML = "";
        terminalOutput.innerHTML = "";
        completedAgents = 0;
        document.querySelectorAll(".agent-node").forEach((n) => {
            n.classList.remove("running", "done");
            const t = n.querySelector(".agent-timer");
            if (t) t.remove();
        });
        document.querySelectorAll(".agent-tasks").forEach((el) => (el.innerHTML = ""));
        document.querySelectorAll(".agent-status-item").forEach((el) => el.classList.remove("running", "done"));
        currentStreamAgent = null;
        currentDetails = null;
        currentDetailsContent = null;
        for (const k of Object.keys(agentTimes)) delete agentTimes[k];
    }

    // ─── Test-case explorer ───
    function renderTestExplorer(data) {
        const cases = data.test_cases || [];
        const passed = data.passed != null ? data.passed : data.passed_count;
        const failed = data.failed != null ? data.failed : data.failed_count;
        const el = document.createElement("div");
        el.className = "log-entry test-explorer";
        el.dataset.agent = "tester";
        if (activeAgentFilter && activeAgentFilter !== "tester") el.style.display = "none";
        const summary = escape(data.summary || data.output_summary || "");
        const rows = cases
            .map(
                (c) =>
                    `<li class="tc ${c.passed ? "pass" : "fail"}"><span class="tc-dot"></span>` +
                    `<span class="tc-name">${escape(c.name)}</span>` +
                    (c.error_message ? `<span class="tc-err">${escape(c.error_message)}</span>` : "") +
                    `</li>`
            )
            .join("");
        el.innerHTML =
            `<div class="log-header"><span class="agent-tag tester">tester</span>` +
            `<span class="tc-counts"><span class="pass-text">✓ ${passed || 0}</span> ` +
            `<span class="fail-text">✗ ${failed || 0}</span></span></div>` +
            (cases.length
                ? `<details class="tc-details" open><summary>${cases.length} test case(s)</summary><ul class="tc-list">${rows}</ul></details>`
                : `<div class="log-content">${summary}</div>`);
        eventLog.appendChild(el);
        eventLog.scrollTop = eventLog.scrollHeight;
    }

    // ─── SSE & streaming ───
    function connectSSE() {
        if (eventSource) eventSource.close();
        eventSource = new EventSource(`/stream/${sessionId}`);
        let currentChunkBuffer = "";

        eventSource.onmessage = ({ data }) => {
            const ev = JSON.parse(data);

            if (ev.event === "agent_start") {
                document.getElementById(`node-${ev.agent}`).classList.add("running");
                const statusItem = document.getElementById(`status-${ev.agent}`);
                if (statusItem) statusItem.classList.add("running");
                currentStreamAgent = ev.agent;
                currentChunkBuffer = "";

                agentTimes[ev.agent] = { start: performance.now() };
                setAgentTimer(ev.agent, "running…");

                const details = document.createElement("details");
                details.className = "thinking-details";
                details.dataset.agent = ev.agent;
                details.open = true;
                if (activeAgentFilter && activeAgentFilter !== ev.agent) details.style.display = "none";

                const summary = document.createElement("summary");
                summary.className = "thinking-summary";
                summary.innerHTML = `<span class="stream-indicator" style="background:var(--agent-${ev.agent});"></span> ${ev.agent} is thinking...`;

                const content = document.createElement("div");
                content.className = "thinking-content";

                details.appendChild(summary);
                details.appendChild(content);
                eventLog.appendChild(details);
                eventLog.scrollTop = eventLog.scrollHeight;

                currentDetails = details;
                currentDetailsContent = content;
            } else if (ev.event === "llm_chunk") {
                if (ev.agent === currentStreamAgent && currentDetailsContent) {
                    currentChunkBuffer += ev.chunk;
                    currentDetailsContent.textContent = currentChunkBuffer;
                    eventLog.scrollTop = eventLog.scrollHeight;
                }
            } else if (ev.event === "agent_complete") {
                const node = document.getElementById(`node-${ev.agent}`);
                node.classList.remove("running");
                node.classList.add("done");
                completedAgents++;
                const statusItem = document.getElementById(`status-${ev.agent}`);
                if (statusItem) {
                    statusItem.classList.remove("running");
                    statusItem.classList.add("done");
                }
                const rec = agentTimes[ev.agent];
                if (rec && rec.start != null) {
                    rec.elapsedMs = performance.now() - rec.start;
                    setAgentTimer(ev.agent, fmtMs(rec.elapsedMs));
                }
                if (currentDetails) {
                    currentDetails.querySelector(".thinking-summary").innerHTML = `${ev.agent} reasoning`;
                    currentDetails.open = false;
                    currentDetails = null;
                    currentDetailsContent = null;
                }
            } else if (ev.event === "tasks") {
                const container = document.getElementById(`tasks-${ev.agent}`);
                if (container) {
                    container.innerHTML = "";
                    ev.tasks.forEach((t) => {
                        const item = document.createElement("div");
                        item.className = "task-item";
                        item.id = `task-${ev.agent}-${t.id}`;
                        item.innerHTML = `<span class="task-check"></span><span class="task-text">${escape(t.text)}</span>`;
                        container.appendChild(item);
                    });
                    const first = container.querySelector(".task-item");
                    if (first) first.classList.add("active");
                }
            } else if (ev.event === "task_done") {
                const item = document.getElementById(`task-${ev.agent}-${ev.task_id}`);
                if (item) {
                    item.classList.remove("active");
                    item.classList.add("done");
                    const next = item.nextElementSibling;
                    if (next && !next.classList.contains("done")) next.classList.add("active");
                }
            } else if (ev.event === "task_update") {
                const item = document.getElementById(`task-${ev.agent}-${ev.task_id}`);
                if (item) {
                    const textEl = item.querySelector(".task-text");
                    if (textEl) textEl.textContent = ev.text;
                }
            } else if (ev.event === "agent_output") {
                if (ev.agent === "tester" && ev.data && ("test_cases" in ev.data || "passed" in ev.data)) {
                    renderTestExplorer(ev.data);
                } else {
                    appendLog(ev.agent, ev.data);
                }
            } else if (ev.event === "terminal") {
                appendTerminal(ev.data.source, ev.data.text);
            } else if (ev.event === "preview_reload") {
                if (previewFrame && previewFrame.src && previewFrame.src !== "about:blank") {
                    previewFrame.src = previewFrame.src;
                }
            } else if (ev.event === "pipeline_done") {
                loadFiles(ev.files);
                generateBtn.disabled = false;
                generateBtn.textContent = "Initialize Build";
                iterateSection.classList.add("visible");
                iterateBtn.disabled = false;
                downloadBtn.classList.add("enabled");
                document.getElementById("launchPreviewBtn").disabled = false;
                stopBuildTimer();
                fetchHistory();
                fetchMemories();
                toast("Build complete — " + fmtMs(buildStartTime != null ? performance.now() - buildStartTime : 0), "success");
                eventSource.close();
            } else if (ev.event === "error") {
                appendLog("system", { error: ev.message });
                generateBtn.disabled = false;
                generateBtn.textContent = "Initialize Build";
                stopBuildTimer();
                toast("Pipeline error", "error");
                eventSource.close();
            }
        };
    }

    function appendTerminal(source, text) {
        const l = document.createElement("div");
        l.innerHTML = `<span class="term-src ${escape(source)}">[${escape(source)}]</span> ${escape(text)}`;
        terminalOutput.appendChild(l);
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    }

    function appendLog(agent, data) {
        const el = document.createElement("div");
        el.className = "log-entry";
        el.dataset.agent = agent;
        if (activeAgentFilter && activeAgentFilter !== agent) el.style.display = "none";
        const keys = Object.keys(data)
            .map((k) => `<li><strong>${escape(k)}:</strong> ${escape(JSON.stringify(data[k]).substring(0, 150))}</li>`)
            .join("");
        el.innerHTML = `<div class="log-header"><span class="agent-tag ${agent}">${agent}</span></div>
                        <div class="log-content"><ul>${keys}</ul></div>`;
        eventLog.appendChild(el);
        eventLog.scrollTop = eventLog.scrollHeight;
    }

    async function checkoutVersion(version) {
        if (!sessionId) return;
        const res = await fetch(`/checkout/${sessionId}/${version}`, { method: "POST" });
        if (res.ok) {
            const data = await res.json();
            loadFiles(data.files);
            fetchHistory();
            toast("Checked out v" + version, "info");
        }
    }

    // ─── Preview actions (with real error surfacing) ───
    const btnPlay = document.getElementById("launchPreviewBtn");
    const btnStop = document.getElementById("stopPreviewBtn");
    const btnRef = document.getElementById("refreshPreviewBtn");

    function hideBanner() {
        if (previewBanner) {
            previewBanner.style.display = "none";
            previewBanner.textContent = "";
        }
    }
    function showBanner(msg) {
        if (previewBanner) {
            previewBanner.textContent = msg;
            previewBanner.style.display = "block";
        }
    }

    btnPlay.onclick = async () => {
        btnPlay.disabled = true;
        hideBanner();
        previewOverlay.textContent = "Booting preview…";
        previewOverlay.classList.remove("hidden");
        previewOverlay.style.display = "";
        try {
            const r = await fetch(`/preview/${sessionId}/start`, { method: "POST" });
            if (r.ok) {
                previewFrame.style.display = "block";
                previewFrame.src = `/preview/${sessionId}/`;
                previewOverlay.style.display = "none";
                btnStop.disabled = false;
                btnRef.disabled = false;
            } else {
                // Surface the REAL backend error (captured stderr), not a generic message.
                let detail = `Preview failed (HTTP ${r.status})`;
                try {
                    const body = await r.json();
                    if (body && body.detail) detail = body.detail;
                } catch (e) {
                    /* non-JSON */
                }
                previewOverlay.style.display = "none";
                showBanner("⚠ " + detail);
                appendTerminal("preview", detail);
                toast("Preview failed to start", "error");
                btnPlay.disabled = false;
            }
        } catch (e) {
            previewOverlay.style.display = "none";
            showBanner("⚠ Could not reach the server: " + e);
            btnPlay.disabled = false;
        }
    };

    btnStop.onclick = async () => {
        await fetch(`/preview/${sessionId}/stop`, { method: "POST" });
        previewFrame.src = "about:blank";
        previewFrame.style.display = "none";
        previewOverlay.textContent = "Preview inactive";
        previewOverlay.style.display = "";
        previewOverlay.classList.remove("hidden");
        hideBanner();
        btnStop.disabled = true;
        btnRef.disabled = true;
        btnPlay.disabled = false;
    };

    btnRef.onclick = () => {
        if (previewFrame.src) previewFrame.src = previewFrame.src;
    };

    // ─── In-iframe console capture → terminal + banner ───
    window.addEventListener("message", (e) => {
        const d = e.data;
        if (!d || d.type !== "console") return;
        appendTerminal("console", `[${d.level}] ${d.msg}`);
        if (d.level === "error") showBanner("⚠ Runtime error in preview: " + d.msg);
    });

    // ─── Export & misc ───
    downloadBtn.onclick = () => {
        if (sessionId) window.location.href = `/download/${sessionId}`;
    };
    document.getElementById("terminalClearBtn").onclick = () => {
        terminalOutput.innerHTML = "";
    };
    document.getElementById("copySession").onclick = () => {
        if (!sessionId) return;
        navigator.clipboard.writeText(sessionId);
        toast("Session ID copied", "info");
    };
})();
