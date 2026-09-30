"""The durable v1 routing graph; note generation is a subsequent owned task."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from reckoner.v1.workflow import nodes
from reckoner.v1.workflow.state import WorkflowState


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    builder = StateGraph(WorkflowState, context_schema=dict)
    names = ("intake", "evidence", "score", "calibrate_route", "persist_decision")
    previous = START
    for name in names:
        builder.add_node(name, getattr(nodes, name))
        builder.add_edge(previous, name)
        previous = name
    builder.add_edge(previous, END)
    return builder.compile(checkpointer=checkpointer)
