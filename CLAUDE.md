# Project 7 — AI Dev Agent

Multi-agent code generation platform with BYOK, live preview, and orchestration visualization.

## Architecture

```
POST /generate → Planner → [plan_ready event, waits for approval]
POST /approve-plan → Developer → Designer → Tester → (loop or Reviewer) → pipeline_done
```

**Services** (docker-compose):
- `app` (port 8007) — FastAPI, LangGraph pipeline, BYOK key storage
- `sandbox` (port 8009) — Isolated code execution (no secrets, no DB)
- `postgres` — Sessions, versions, encrypted keys
- `redis` — Session state cache

**Network isolation**: `data` net (app↔postgres↔redis), `exec` net (app↔sandbox)

## Module Map

```
src/dev_agent/
├── agents/          # Agent nodes: planner, developer, designer, tester, reviewer
│   └── prompts.py   # ALL agent prompts (single source of truth)
├── pipeline/        # LangGraph state machine, graph wiring, base backend
├── sandbox/         # Code execution: python_runner, node_runner, build_runner, preview_server
├── db/              # SQLAlchemy models, keys_store (BYOK encryption)
├── security/        # keyvault.py (Fernet encryption, auto-gen key)
├── memory/          # Cross-session learning extraction
├── versioning/      # File snapshots + diffs
├── static/          # Frontend (vanilla JS, no build step)
│   ├── index.html   # SPA shell: sidebar + phase-based main area
│   ├── js/app.js    # Phase state machine, SSE handling, Monaco editor
│   └── css/         # Split design system
│       ├── tokens.css     # Design tokens (colors, spacing, typography)
│       ├── reset.css      # CSS reset + base styles
│       ├── layout.css     # Grid shell, sidebar, main area, responsive
│       ├── components.css # Buttons, forms, badges, toasts, modal
│       ├── phases.css     # Ideation/planning/building/complete phases
│       ├── agents.css     # Thinking blocks, progress stepper, plan card
│       └── editor.css     # Monaco integration, file tabs, preview
├── main.py          # FastAPI endpoints, session management, SSE streaming
├── config.py        # YAML model config loader
├── llm.py           # LLM context builder (BYOK key resolution)
├── cache.py         # Redis caching (excludes llm_context)
└── schemas.py       # Pydantic request/response models

shared/              # Provider abstraction layer
├── providers.py     # build_chat_model() factory (multi-provider)
└── adapter.py       # extract_text(), safety filter, tool-call normalization

config/
└── models.yaml      # Single source of truth for providers, models, agent roles
```

## Conventions

- **Async everywhere**: All I/O uses `async/await` (asyncpg, aiohttp, async generators)
- **Type hints required**: All functions fully typed, enforced by mypy (`disallow_untyped_defs = true`)
- **Naming**: files=snake_case, classes=PascalCase, agents=lowercase ("planner", "developer")
- **Linting**: Ruff (line-length 100, rules E/W/F/I/B/C4/UP)
- **Imports**: `src.dev_agent.*` for app code, `shared.*` for provider layer
- **Secrets**: Never log or cache API keys. `llm_context` excluded from Redis cache.
- **Dependencies**: Pin exact versions (no `^` or `~`) in requirements.txt and generated package.json

## Adding a New Agent

1. Create `src/dev_agent/agents/{name}.py` following `reviewer.py` pattern
2. Add `{NAME}_PROMPT` to `agents/prompts.py`
3. Add `{NAME}_TASKS` list to `agents/retry.py`
4. Wire into `pipeline/graph.py` — add node, add edges
5. Add `<g class="canvas-node">` to `index.html` canvas SVG
6. Add `--agent-{name}` color to `theme.css` `:root`
7. Add edge mapping in `app.js` `_edgeMap`

## Adding a New Provider

1. Add provider config block to `config/models.yaml`
2. Add `langchain-{provider}` to `requirements.txt` (pinned)
3. Handle in `shared/providers.py` `build_chat_model()`
4. If provider has quirks (safety filters, multi-part), handle in `shared/adapter.py`

## Key Design Decisions

- **Plan approval gate**: Pipeline pauses after planner, user must approve before build starts
- **Designer agent**: Dedicated styling pass between developer and tester
- **ENCRYPTION_KEY auto-gen**: Key auto-generated on first run, persisted to Docker volume, env var override for production
- **extract_text() for streaming**: All LLM chunk output passes through `shared/adapter.extract_text()` to handle Gemini's multi-part content format
- **Sandbox isolation**: Generated code runs in a separate container with no access to secrets/DB

## Running

```bash
docker compose up --build
# App: http://localhost:8007
# First run auto-generates ENCRYPTION_KEY (persisted in appdata volume)
```

## Testing

```bash
pytest -m unit          # Mocked LLM, fast
pytest -m integration   # Requires live API keys in .env
```

## Frontend Design System

- **Layout**: Sidebar + phase-based main area ("Conversational Studio")
- **Phases**: Ideation → Planning → Building → Complete (each owns the viewport)
- **Agent UX**: Thinking blocks (expand while running, collapse when done) — like Claude's thinking
- **Progressive files**: Code appears in inline editor as each agent finishes
- **Accent**: Electric violet `#8b5cf6` (primary), cyan `#06b6d4` (secondary)
- **Agent colors**: planner=cyan, developer=violet, designer=pink, tester=emerald, reviewer=amber
- **Progress stepper**: Horizontal dots + connecting lines (replaces SVG canvas)
- **Notifications**: Browser notifications + tab title updates for long builds
- **Tokens**: `--bg-base`, `--bg-surface`, `--text-primary/secondary/muted`, `--space-{1-16}`, `--radius-{sm/md/lg/xl}`
