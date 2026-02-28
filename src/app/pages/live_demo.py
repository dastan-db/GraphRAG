import os
import json

import dash_bootstrap_components as dbc
import dash
from dash import html, dcc, Input, Output, State, callback, no_update, ALL

from backend.agent_client import query_agent, query_agent_mock, AgentResponse

USE_MOCK = os.getenv("USE_MOCK_BACKEND", "false").lower() == "true"

EXAMPLE_QUESTIONS = [
    "How is Ruth connected to Jesus?",
    "Which people appear in both Genesis and Exodus?",
    "What role does Moses play across the books?",
    "How is David connected to both Ruth and Jesus?",
    "What happened on the road to Damascus?",
    "Trace the Israelites' journey from Egypt.",
]

ENTITY_COLORS = {
    "Person": "#dc3545", "Place": "#4682B4", "Event": "#28a745",
    "Group": "#ffc107", "Concept": "#9b59b6", "Unknown": "#888",
}


def demo_layout():
    return html.Div([
        html.Div([
            html.H1("Live Demo", className="display-5 fw-bold"),
            html.P("Ask a question and see the auditable, traced answer in real time",
                   className="lead text-muted"),
        ], className="text-center py-3"),

        dbc.Alert(
            "Running in demo mode (mock responses). Set USE_MOCK_BACKEND=false to connect to the live agent.",
            color="info", className="mb-3",
        ) if USE_MOCK else html.Div(),

        html.Hr(),

        # Example question buttons
        html.P("Try one of these questions:", className="fw-bold mb-2"),
        html.Div([
            dbc.Button(q, id={"type": "example-btn", "index": i}, color="outline-secondary",
                       size="sm", className="me-2 mb-2")
            for i, q in enumerate(EXAMPLE_QUESTIONS)
        ], className="mb-3"),

        # Main layout: chat + provenance
        dbc.Row([
            # Chat column
            dbc.Col([
                html.H5("Conversation", className="mb-3"),
                html.Div(id="chat-history", style={
                    "height": "400px", "overflowY": "auto", "padding": "1rem",
                    "backgroundColor": "#1a1f2b", "borderRadius": "8px", "border": "1px solid #333",
                }),
                dbc.InputGroup([
                    dbc.Input(id="chat-input", placeholder="Ask about the biblical knowledge graph...",
                              type="text", className="bg-dark text-white border-secondary"),
                    dbc.Button("Send", id="send-btn", color="danger", n_clicks=0),
                ], className="mt-2"),
            ], md=7),

            # Provenance column
            dbc.Col([
                html.H5("Provenance", className="mb-3"),
                html.Div(id="provenance-panel", style={
                    "minHeight": "400px", "padding": "1rem",
                    "backgroundColor": "#1a1f2b", "borderRadius": "8px", "border": "1px solid #333",
                }, children=[
                    html.Div([
                        html.P("Ask a question to see the traced path and provenance here",
                               className="text-muted text-center", style={"paddingTop": "8rem"}),
                    ])
                ]),
            ], md=5),
        ], className="g-4"),

        # Hidden store for chat state
        dcc.Store(id="chat-store", data={"messages": []}),
        dcc.Store(id="last-response-store", data=None),

        html.Hr(className="mt-4"),
        html.P(
            "Every answer above is auditable: the provenance section shows the exact entity path, "
            "source citations, and grounding indicator.",
            className="text-center text-muted small",
        ),
    ])


def _render_message(role, content):
    if role == "user":
        return html.Div([
            html.Div([
                html.Strong("You", className="text-danger"),
                html.P(content, className="mb-0 mt-1"),
            ], className="p-2 mb-2 rounded", style={"backgroundColor": "#2a2f3b"}),
        ])
    else:
        return html.Div([
            html.Div([
                html.Strong("GraphRAG Agent", className="text-success"),
                dcc.Markdown(content, className="mb-0 mt-1",
                             style={"color": "#ccc", "fontSize": "0.9rem"}),
            ], className="p-2 mb-2 rounded", style={"backgroundColor": "#1f2937"}),
        ])


def _render_provenance(resp_data):
    if not resp_data:
        return html.P("No response yet.", className="text-muted text-center", style={"paddingTop": "8rem"})

    elements = []

    path = resp_data.get("path", "")
    if path:
        elements.append(html.H6("Traced Path", className="text-danger mb-2"))
        parts = [p.strip() for p in path.split("→")]
        path_badges = []
        for i, part in enumerate(parts):
            path_badges.append(dbc.Badge(part.split("(")[0].strip(), color="danger" if i == 0 or i == len(parts) - 1 else "primary", className="me-1 mb-1 p-2"))
            if i < len(parts) - 1:
                path_badges.append(html.Span(" → ", className="text-muted"))
        elements.append(html.Div(path_badges, className="mb-3"))

    sources = resp_data.get("sources", [])
    if sources:
        elements.append(html.H6("Source Citations", className="text-danger mb-2"))
        for src in sources:
            elements.append(html.Div([html.I(className="fas fa-book me-2 text-muted"), src], className="mb-1 small"))
        elements.append(html.Div(className="mb-3"))

    grounding = resp_data.get("grounding", "")
    if grounding:
        elements.append(html.H6("Grounding", className="text-danger mb-2"))
        if "all claims grounded" in grounding.lower():
            elements.append(dbc.Alert(f"✅ {grounding}", color="success", className="py-2 small"))
        elif "partially" in grounding.lower():
            elements.append(dbc.Alert(f"⚠️ {grounding}", color="warning", className="py-2 small"))
        else:
            elements.append(dbc.Alert(grounding, color="info", className="py-2 small"))

    full_text = resp_data.get("full_text", "")
    if full_text:
        elements.append(html.Details([
            html.Summary("Show raw agent response", className="text-muted small mb-2 mt-3", style={"cursor": "pointer"}),
            dcc.Markdown(f"```\n{full_text}\n```", style={"fontSize": "0.75rem"}),
        ]))

    return html.Div(elements) if elements else html.P("No provenance data.", className="text-muted")


def register_demo_callbacks(app):

    @app.callback(
        Output("chat-store", "data"),
        Output("chat-history", "children"),
        Output("provenance-panel", "children"),
        Output("chat-input", "value"),
        Input("send-btn", "n_clicks"),
        Input({"type": "example-btn", "index": ALL}, "n_clicks"),
        State("chat-input", "value"),
        State("chat-store", "data"),
        prevent_initial_call=True,
    )
    def handle_send(send_clicks, example_clicks, user_input, chat_data):
        ctx = dash.callback_context
        if not ctx.triggered:
            return no_update, no_update, no_update, no_update

        trigger_id = ctx.triggered[0]["prop_id"]

        question = None
        if "send-btn" in trigger_id:
            question = user_input
        elif "example-btn" in trigger_id:
            try:
                idx_str = trigger_id.split('"index":')[1].split("}")[0].strip()
                idx = int(idx_str)
                if example_clicks[idx]:
                    question = EXAMPLE_QUESTIONS[idx]
            except (IndexError, ValueError):
                return no_update, no_update, no_update, no_update

        if not question or not question.strip():
            return no_update, no_update, no_update, no_update

        messages = chat_data.get("messages", [])
        messages.append({"role": "user", "content": question})

        try:
            if USE_MOCK:
                resp = query_agent_mock(question)
            else:
                resp = query_agent(question)
        except Exception as e:
            from backend.agent_client import AgentResponse
            resp = AgentResponse(
                answer=f"**Error querying agent endpoint:** {e}\n\nCheck that the `graphrag-bible-agent` model serving endpoint is running.",
                provenance_raw="", path="", sources=[], grounding="",
                full_text=str(e),
            )

        messages.append({"role": "assistant", "content": resp.answer})

        chat_children = [_render_message(m["role"], m["content"]) for m in messages]

        resp_data = {
            "path": resp.path,
            "sources": resp.sources,
            "grounding": resp.grounding,
            "full_text": resp.full_text,
        }
        prov = _render_provenance(resp_data)

        return {"messages": messages}, chat_children, prov, ""
