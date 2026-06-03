"""LangGraph pipeline — compiles the agent state machine once at module load."""

from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, StateGraph

from src.dev_agent.agents.developer import developer_node
from src.dev_agent.agents.planner import planner_node
from src.dev_agent.agents.reviewer import reviewer_node
from src.dev_agent.agents.tester import tester_node
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


async def run_pipeline(state: DevPipelineState) -> DevPipelineState:
    """Execute the full agent pipeline. Returns final state."""
    return cast(DevPipelineState, await _GRAPH.ainvoke(state))


async def run_iterate_pipeline(state: DevPipelineState) -> DevPipelineState:
    """Execute the iterate pipeline (developer → tester → reviewer). Returns final state."""
    return cast(DevPipelineState, await _ITERATE_GRAPH.ainvoke(state))
