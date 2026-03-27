"""Corporate Demo page — Enron email corpus GraphRAG demonstration."""

import json
import os

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, dcc, html, no_update

from backend.agent_client import AgentResponse, query_agent_enron, query_agent_enron_mock

USE_MOCK = os.getenv("USE_MOCK_BACKEND", "false").lower() == "true"

CORPORATE_EXAMPLE_QUESTIONS = [
    "Who was involved in the California energy trading decisions?",
    "What projects did Jeff Skilling manage between 2000-2001?",
    "How did information flow about the Broadband division?",
    "Which executives discussed Fastow's partnerships?",
    "Who communicated most frequently with Kenneth Lay?",
    "What was the organizational structure around Enron Energy Trading?",
]


def corporate_demo_layout():
    return html.Div([
        html.Div([
            html.H1("Corporate Demo", className="display-5 fw-bold"),
            html.P(
                "Explore the Enron email corpus with auditable AI — "
                "track communication patterns, organizational structure, and information flow",
                className="lead text-muted",
            ),
        ], className="text-center py-3"),

        dbc.Alert([
            html.I(className="fas fa-building me-2"),
            html.Strong("Enterprise Use Case: "),
            "This demonstrates GraphRAG applied to corporate email communications "
            "for governance, compliance, and investigation scenarios. Every answer "
            "is traceable to specific emails in the Enron corpus.",
        ], color="info", className="mb-3") if not USE_MOCK else dbc.Alert(
            "Running in demo mode (mock responses). "
            "Set USE_MOCK_BACKEND=false to connect to the live agent.",
            color="warning", className="mb-3",
        ),

        html.Hr(),

        dbc.Card([
            dbc.CardBody([
                html.Div([
                    html.I(className="fas fa-shield-alt me-2 text-warning"),
                    html.H6("Access Tier (Lakebase RLS)", className="d-inline mb-0"),
                ], className="mb-2"),
                html.P(
                    "Switch tiers to see how Row-Level Security policies restrict "
                    "the knowledge graph. Each tier sees a different subgraph.",
                    className="small text-muted mb-2",
                ),
                dbc.RadioItems(
                    id="tier-selector",
                    options=[
                        {"label": "Legal Team (full access)", "value": "legal_team"},
                        {"label": "Executive Team", "value": "executive_team"},
                        {"label": "Analyst Team (restricted)", "value": "analyst_team"},
                    ],
                    value="legal_team",
                    inline=True,
                ),
            ]),
        ], className="mb-3", color="dark", outline=True),

        html.P("Try one of these investigation questions:", className="fw-bold mb-2"),
        html.Div([
            dbc.Button(
                q,
                id={"type": "corp-example-btn", "index": i},
                color="outline-secondary",
                size="sm",
                className="me-2 mb-2",
            )
            for i, q in enumerate(CORPORATE_EXAMPLE_QUESTIONS)
        ], className="mb-3"),

        dbc.Row([
            dbc.Col([
                html.H5("Conversation", className="mb-3"),
                dcc.Loading(
                    id="corp-chat-loading",
                    type="dot",
                    color="#0d6efd",
                    children=html.Div(id="corp-chat-history", style={
                        "height": "400px", "overflowY": "auto", "padding": "1rem",
                        "backgroundColor": "#1a1f2b", "borderRadius": "8px",
                        "border": "1px solid #333",
                    }),
                ),
                dbc.InputGroup([
                    dbc.Input(
                        id="corp-chat-input",
                        placeholder="Ask about the Enron email knowledge graph...",
                        type="text",
                        n_submit=0,
                        className="bg-dark text-white border-secondary",
                    ),
                    dbc.Button("Send", id="corp-send-btn", color="primary", n_clicks=0),
                ], className="mt-2"),
            ], md=7),

            dbc.Col([
                html.H5("Provenance", className="mb-3"),
                html.Div(id="corp-provenance-panel", style={
                    "minHeight": "400px", "maxHeight": "600px", "overflowY": "auto",
                    "padding": "1rem",
                    "backgroundColor": "#1a1f2b", "borderRadius": "8px",
                    "border": "1px solid #333",
                }, children=[
                    html.Div([
                        html.P(
                            "Ask a question to see the traced path and email provenance here",
                            className="text-muted text-center",
                            style={"paddingTop": "8rem"},
                        ),
                    ])
                ]),
            ], md=5),
        ], className="g-4"),

        dcc.Store(id="corp-chat-store", data={"messages": []}),

        html.Hr(className="mt-4"),
        html.P(
            "Every answer above is auditable: the provenance section shows the entity path, "
            "source email citations, and grounding indicator.",
            className="text-center text-muted small",
        ),
    ])


def _render_message(role, content):
    if role == "user":
        return html.Div([
            html.Div([
                html.Strong("You", className="text-primary"),
                html.P(content, className="mb-0 mt-1"),
            ], className="p-2 mb-2 rounded", style={"backgroundColor": "#2a2f3b"}),
        ])
    else:
        return html.Div([
            html.Div([
                html.Strong("GraphRAG Agent", className="text-success"),
                dcc.Markdown(
                    content, className="mb-0 mt-1",
                    style={"color": "#ccc", "fontSize": "0.9rem"},
                ),
            ], className="p-2 mb-2 rounded", style={"backgroundColor": "#1f2937"}),
        ])


def _render_provenance(resp_data):
    if not resp_data:
        return html.P(
            "No response yet.", className="text-muted text-center",
            style={"paddingTop": "8rem"},
        )

    elements = []

    path = resp_data.get("path", "")
    if path:
        elements.append(html.H6("Communication Path", className="text-primary mb-2"))
        parts = [p.strip() for p in path.split("\u2192")]
        path_badges = []
        for i, part in enumerate(parts):
            color = "primary" if i == 0 or i == len(parts) - 1 else "secondary"
            path_badges.append(
                dbc.Badge(
                    part.split("(")[0].strip(),
                    color=color,
                    className="me-1 mb-1 p-2",
                )
            )
            if i < len(parts) - 1:
                path_badges.append(html.Span(" \u2192 ", className="text-muted"))
        elements.append(html.Div(path_badges, className="mb-3"))

    sources = resp_data.get("sources", [])
    if sources:
        elements.append(html.H6("Email Sources", className="text-primary mb-2"))
        for src in sources:
            elements.append(html.Div([
                html.I(className="fas fa-envelope me-2 text-muted"),
                html.Span(src, className="small"),
            ], className="mb-1"))
        elements.append(html.Div(className="mb-3"))

    grounding = resp_data.get("grounding", "")
    if grounding:
        elements.append(html.H6("Grounding", className="text-primary mb-2"))
        if "all claims grounded" in grounding.lower():
            elements.append(
                dbc.Alert(f"  {grounding}", color="success", className="py-2 small")
            )
        elif "partially" in grounding.lower():
            elements.append(
                dbc.Alert(f"  {grounding}", color="warning", className="py-2 small")
            )
        else:
            elements.append(
                dbc.Alert(grounding, color="info", className="py-2 small")
            )

    full_text = resp_data.get("full_text", "")
    if full_text:
        elements.append(html.Details([
            html.Summary(
                "Show raw agent response",
                className="text-muted small mb-2 mt-3",
                style={"cursor": "pointer"},
            ),
            dcc.Markdown(f"```\n{full_text}\n```", style={"fontSize": "0.75rem"}),
        ]))

    return html.Div(elements) if elements else html.P(
        "No provenance data.", className="text-muted",
    )


def register_corporate_demo_callbacks(app):

    @app.callback(
        Output("corp-chat-store", "data"),
        Output("corp-chat-history", "children"),
        Output("corp-provenance-panel", "children"),
        Output("corp-chat-input", "value"),
        Input("corp-send-btn", "n_clicks"),
        Input("corp-chat-input", "n_submit"),
        Input({"type": "corp-example-btn", "index": ALL}, "n_clicks"),
        State("corp-chat-input", "value"),
        State("corp-chat-store", "data"),
        State("tier-selector", "value"),
        prevent_initial_call=True,
    )
    def handle_send(send_clicks, n_submit, example_clicks, user_input, chat_data, tier):
        ctx = dash.callback_context
        if not ctx.triggered:
            return no_update, no_update, no_update, no_update

        trigger_id = ctx.triggered[0]["prop_id"]

        question = None
        if "corp-send-btn" in trigger_id or "corp-chat-input" in trigger_id:
            question = user_input
        elif "corp-example-btn" in trigger_id:
            try:
                parsed = json.loads(trigger_id.rsplit(".", 1)[0])
                idx = parsed["index"]
                if example_clicks[idx]:
                    question = CORPORATE_EXAMPLE_QUESTIONS[idx]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                return no_update, no_update, no_update, no_update

        if not question or not question.strip():
            return no_update, no_update, no_update, no_update

        messages = chat_data.get("messages", [])
        messages.append({"role": "user", "content": question})

        try:
            if USE_MOCK:
                resp = query_agent_enron_mock(question)
            else:
                resp = query_agent_enron(question, tier=tier or "")
        except Exception as e:
            resp = AgentResponse(
                answer=f"**Error querying agent endpoint:** {e}\n\n"
                       "Check that the Enron agent endpoint is running.",
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
