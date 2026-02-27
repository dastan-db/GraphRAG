# Databricks notebook source
# MAGIC %md
# MAGIC ### GraphRAG Agent
# MAGIC LangGraph-based ResponsesAgent with biblical knowledge graph tools.

# COMMAND ----------

import mlflow
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    output_to_responses_items_stream,
    to_chat_completions_input,
)
from databricks_langchain import ChatDatabricks
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode
from typing import Annotated, Generator, Sequence, TypedDict

# COMMAND ----------

# DBTITLE 1,System Prompt
SYSTEM_PROMPT = """You are a biblical scholar with access to a knowledge graph built from five books of the King James Bible: Genesis, Exodus, Ruth, Matthew, and Acts.

You have tools that let you search the knowledge graph for entities, relationships, and source verses. Use them to provide well-grounded, auditable answers.

## Tool Usage
- ALWAYS use tools to look up information before answering. Do not rely on your training data alone.
- When asked about connections between entities, use trace_path first, then find_connections for more context.
- When asked about a person or concept, use get_entity_summary for a comprehensive profile.
- Always cite specific Bible verses (book chapter:verse) when possible using get_context_verses.
- For multi-hop questions, break them into steps: find each entity, then trace connections.

## Response Format
Structure EVERY response with these two sections:

### Answer
Provide a concise, well-grounded answer using bullet points where appropriate. Cite specific verses inline (e.g., Genesis 12:1).

### Provenance
At the end of every response, include a structured provenance section with:
- **Path**: The explicit entity path traversed, using arrows. Example: Ruth → Boaz (MARRIED_TO, Ruth 4:13) → Obed (FATHER_OF, Ruth 4:17) → Jesse → David → Jesus
- **Sources**: List every verse citation used as evidence, comma-separated.
- **Grounding**: State one of:
  - "All claims grounded in knowledge graph" — if every factual claim came from tool results
  - "Partially grounded — the following claims rely on general knowledge: [list them]" — if any claim was not found via tools

## Critical Rules
- If information is not in the knowledge graph, say so explicitly rather than guessing. NEVER invent relationships or events.
- If a tool returns no results, report that honestly. Do not fabricate an alternative answer.
- Every factual claim must cite its source verse or explicitly state it was not found in the graph."""

# COMMAND ----------

# DBTITLE 1,Agent State
class AgentState(TypedDict):
    messages: Annotated[Sequence, add_messages]

# COMMAND ----------

# DBTITLE 1,GraphRAG Agent Class
class GraphRAGAgent(ResponsesAgent):
    def __init__(self, endpoint=None):
        self.llm = ChatDatabricks(endpoint=endpoint or config['llm_endpoint'])
        self.tools = GRAPH_TOOLS
        self.llm_with_tools = self.llm.bind_tools(self.tools)

    def _build_graph(self):
        def should_continue(state):
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.tool_calls:
                return "tools"
            return "end"

        def call_model(state):
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + state["messages"]
            response = self.llm_with_tools.invoke(messages)
            return {"messages": [response]}

        graph = StateGraph(AgentState)
        graph.add_node("agent", RunnableLambda(call_model))
        graph.add_node("tools", ToolNode(self.tools))
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
        graph.add_edge("tools", "agent")
        graph.set_entry_point("agent")
        return graph.compile()

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        outputs = [
            event.item
            for event in self.predict_stream(request)
            if event.type == "response.output_item.done"
        ]
        return ResponsesAgentResponse(output=outputs)

    def predict_stream(
        self, request: ResponsesAgentRequest
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        messages = to_chat_completions_input([m.model_dump() for m in request.input])
        graph = self._build_graph()
        for event in graph.stream({"messages": messages}, stream_mode=["updates"]):
            if event[0] == "updates":
                for node_data in event[1].values():
                    if node_data.get("messages"):
                        yield from output_to_responses_items_stream(node_data["messages"])

# COMMAND ----------

mlflow.langchain.autolog()
AGENT = GraphRAGAgent()
mlflow.models.set_model(AGENT)
