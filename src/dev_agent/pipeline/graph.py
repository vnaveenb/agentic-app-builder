"""LangGraph pipeline — compiles the agent state machine once at module load."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from langgraph.graph import END, StateGraph

from src.dev_agent.agents.developer import developer_node
from src.dev_agent.agents.planner import planner_node
from src.dev_agent.agents.reviewer import reviewer_node
from src.dev_agent.agents.tester import tester_node
from src.dev_agent.pipeline.base import PipelineBackend
from src.dev_agent.pipeline.state import DevPipelineState


def _route_after_tester(state: DevPipelineState) -> str:
    """Conditional edge: loop back to developer if tests failed and iterations remain."""
    report = state.get("test_report")
    if report and report.has_critical_bugs and state["iteration"] < state["max_iterations"]:
        return "developer"
    return "reviewer"


def build_graph() -> Any:
    """Build and compile the LangGraph state machine."""
    g = StateGraph(DevPipelineState)
    g.add_node("planner", planner_node)
    g.add_node("developer", developer_node)
    g.add_node("tester", tester_node)
    g.add_node("reviewer", reviewer_node)

    g.set_entry_point("planner")
    g.add_edge("planner", "developer")
    g.add_edge("developer", "tester")
    g.add_conditional_edges(
        "tester",
        _route_after_tester,
        {"developer": "developer", "reviewer": "reviewer"},
    )
    g.add_edge("reviewer", END)

    return g.compile()


_GRAPH = build_graph()


def build_iterate_graph() -> Any:
    """Build a graph that skips the planner — for iterate/refine flows."""
    g = StateGraph(DevPipelineState)
    g.add_node("developer", developer_node)
    g.add_node("tester", tester_node)
    g.add_node("reviewer", reviewer_node)

    g.set_entry_point("developer")
    g.add_edge("developer", "tester")
    g.add_conditional_edges(
        "tester",
        _route_after_tester,
        {"developer": "developer", "reviewer": "reviewer"},
    )
    g.add_edge("reviewer", END)

    return g.compile()


_ITERATE_GRAPH = build_iterate_graph()


class LangGraphBackend(PipelineBackend):
    """LangGraph-based orchestration backend (state machine)."""

    async def run(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        """Execute the full pipeline using LangGraph's astream."""
        final_state = state
        async for chunk in _GRAPH.astream(
            state, stream_mode=["values", "messages"], version="v2"
        ):
            if chunk["type"] == "messages":
                msg, metadata = chunk["data"]
                if msg.content:
                    node_name = metadata.get("langgraph_node", "system")
                    await queue.put({
                        "event": "llm_chunk",
                        "agent": node_name,
                        "chunk": msg.content,
                    })
            elif chunk["type"] == "values":
                final_state = chunk["data"]
        return cast(DevPipelineState, final_state)

    async def run_iterate(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        """Execute the iterate pipeline (skip planner) using LangGraph's astream."""
        final_state = state
        async for chunk in _ITERATE_GRAPH.astream(
            state, stream_mode=["values", "messages"], version="v2"
        ):
            if chunk["type"] == "messages":
                msg, metadata = chunk["data"]
                if msg.content:
                    node_name = metadata.get("langgraph_node", "system")
                    await queue.put({
                        "event": "llm_chunk",
                        "agent": node_name,
                        "chunk": msg.content,
                    })
            elif chunk["type"] == "values":
                final_state = chunk["data"]
        return cast(DevPipelineState, final_state)


# ── Legacy helpers (kept for backwards compatibility with tests) ──────────────


async def run_pipeline(state: DevPipelineState) -> DevPipelineState:
    """Execute the full agent pipeline. Returns final state."""
    return cast(DevPipelineState, await _GRAPH.ainvoke(state))


async def run_iterate_pipeline(state: DevPipelineState) -> DevPipelineState:
    """Execute the iterate pipeline (developer → tester → reviewer). Returns final state."""
    return cast(DevPipelineState, await _ITERATE_GRAPH.ainvoke(state))
