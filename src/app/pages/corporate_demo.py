"""Corporate Demo page — Enron email corpus GraphRAG demonstration."""

import json
import os

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, dcc, html, no_update

from backend.agent_client import AgentResponse, ToolCall, query_agent_enron, query_agent_enron_mock

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
                html.H5("Audit Trail", className="mb-3"),
                html.Div(id="corp-provenance-panel", style={
                    "minHeight": "400px", "maxHeight": "600px", "overflowY": "auto",
                    "padding": "1rem",
                    "backgroundColor": "#1a1f2b", "borderRadius": "8px",
                    "border": "1px solid #333",
                }, children=[
                    html.P(
                        "Ask a question to see which graph queries, "
                        "entities, and data sources the agent used.",
                        className="text-muted text-center",
                        style={"paddingTop": "8rem"},
                    ),
                ]),
            ], md=5),
        ], className="g-4"),

        dcc.Store(id="corp-chat-store", data={"messages": []}),

        html.Hr(className="mt-4"),
        html.P(
            "Every answer above is auditable: the audit trail shows which graph queries "
            "the agent ran, what entities were discovered, and which data sources were consulted.",
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


_TOOL_ICONS = {
    "find_entity": "fas fa-search",
    "find_connections": "fas fa-project-diagram",
    "get_source_emails": "fas fa-envelope-open-text",
    "get_entity_summary": "fas fa-id-card",
    "trace_path": "fas fa-route",
    "list_entities_by_book": "fas fa-list",
    "compare_entity_sets": "fas fa-not-equal",
    "get_context_verses": "fas fa-book-open",
}

_TOOL_DESCRIPTIONS = {
    "find_entity": "Entity lookup",
    "find_connections": "Relationship traversal",
    "get_source_emails": "Email source retrieval",
    "get_entity_summary": "Entity profile",
    "trace_path": "Path discovery",
    "list_entities_by_book": "Entity listing",
    "compare_entity_sets": "Set comparison",
    "get_context_verses": "Source text retrieval",
}


_TIER_VISIBLE_SENSITIVITY = {
    "legal_team": {"general", "executive_confidential", "attorney_client_privileged"},
    "executive_team": {"general", "executive_confidential"},
    "analyst_team": {"general"},
}

_REDACTED_FIELDS = {"bcc", "bcc_addresses"}

_TIER_RANK = {"legal_team": 3, "executive_team": 2, "analyst_team": 1}


def _redact_for_tier(output_raw: str, tier: str) -> tuple[object, int]:
    """Parse tool output JSON and redact fields based on ABAC tier.

    Returns (redacted_obj, redaction_count).
    """
    if not output_raw:
        return None, 0

    try:
        data = json.loads(output_raw)
    except (json.JSONDecodeError, TypeError):
        return output_raw, 0

    allowed_sens = _TIER_VISIBLE_SENSITIVITY.get(tier, {"general"})
    rank = _TIER_RANK.get(tier, 1)
    redacted = 0

    def _redact_obj(obj):
        nonlocal redacted
        if isinstance(obj, dict):
            result = {}
            sens = obj.get("sensitivity", "general")
            if isinstance(sens, str) and sens not in allowed_sens:
                redacted += 1
                return f"[REDACTED — requires {sens} clearance]"
            for k, v in obj.items():
                if k in _REDACTED_FIELDS and rank < 3:
                    result[k] = "[MASKED]"
                    redacted += 1
                else:
                    result[k] = _redact_obj(v)
            return result
        if isinstance(obj, list):
            return [_redact_obj(item) for item in obj]
        return obj

    return _redact_obj(data), redacted


def _render_provenance(resp_data):
    if not resp_data:
        return html.P(
            "Ask a question to see which graph queries, "
            "entities, and data sources the agent used.",
            className="text-muted text-center",
            style={"paddingTop": "8rem"},
        )

    elements = []

    tier = resp_data.get("tier", "")
    if tier:
        tier_labels = {
            "legal_team": ("Legal Team — full access", "success"),
            "executive_team": ("Executive Team — partial access", "warning"),
            "analyst_team": ("Analyst Team — restricted", "danger"),
        }
        label, color = tier_labels.get(tier, (tier, "info"))
        elements.append(html.Div([
            html.I(className="fas fa-shield-alt me-2"),
            html.Span("Access Tier: ", className="small fw-bold"),
            dbc.Badge(label, color=color, className="ms-1"),
        ], className="mb-3"))

    tool_calls = resp_data.get("tool_calls", [])
    if tool_calls:
        elements.append(html.H6([
            html.I(className="fas fa-database me-2"),
            "Graph Queries",
            dbc.Badge(str(len(tool_calls)), color="primary", className="ms-2",
                      pill=True, style={"fontSize": "0.7rem"}),
        ], className="text-info mb-2"))

        for idx, tc in enumerate(tool_calls):
            icon = _TOOL_ICONS.get(tc["name"], "fas fa-cog")
            desc = _TOOL_DESCRIPTIONS.get(tc["name"], tc["name"])
            args = tc.get("arguments", {})
            arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""

            card_children = [
                html.Div([
                    html.I(className=f"{icon} me-2 text-info", style={"width": "16px"}),
                    html.Span(desc, className="small fw-bold"),
                ], className="d-flex align-items-center"),
                html.Code(
                    f"{tc['name']}({arg_str})",
                    className="d-block mt-1",
                    style={"fontSize": "0.75rem", "color": "#8be9fd",
                           "whiteSpace": "pre-wrap", "wordBreak": "break-all"},
                ),
            ]

            output_raw = tc.get("output", "")
            if output_raw:
                redacted_obj, redaction_count = _redact_for_tier(output_raw, tier)
                if redacted_obj is not None:
                    pretty = json.dumps(
                        redacted_obj, indent=2, ensure_ascii=False, default=str,
                    )
                    if len(pretty) > 1200:
                        pretty = pretty[:1200] + "\n  …"

                    output_children = [
                        html.Pre(
                            pretty,
                            style={
                                "fontSize": "0.7rem", "color": "#c9d1d9",
                                "backgroundColor": "#010409",
                                "padding": "0.5rem", "borderRadius": "4px",
                                "maxHeight": "200px", "overflowY": "auto",
                                "marginBottom": "0", "whiteSpace": "pre-wrap",
                                "wordBreak": "break-word",
                            },
                        ),
                    ]
                    if redaction_count > 0:
                        output_children.append(html.Div([
                            html.I(className="fas fa-lock me-1"),
                            html.Span(
                                f"{redaction_count} field{'s' if redaction_count != 1 else ''} "
                                f"redacted for {tier.replace('_', ' ')}",
                                className="small",
                            ),
                        ], className="text-warning mt-1",
                           style={"fontSize": "0.7rem"}))

                    card_children.append(
                        html.Details([
                            html.Summary(
                                "Output",
                                className="small text-muted mt-1",
                                style={"cursor": "pointer", "fontSize": "0.7rem"},
                            ),
                            html.Div(output_children, className="mt-1"),
                        ])
                    )

            elements.append(html.Div(
                card_children,
                className="mb-2 p-2 rounded",
                style={"backgroundColor": "#0d1117", "border": "1px solid #21262d"},
            ))

        elements.append(html.Hr(className="my-2", style={"borderColor": "#333"}))

    entities = resp_data.get("entities", [])
    if entities:
        elements.append(html.H6([
            html.I(className="fas fa-tags me-2"),
            "Entities Mentioned",
        ], className="text-info mb-2"))
        elements.append(html.Div([
            dbc.Badge(e, color="secondary", className="me-1 mb-1 p-2",
                      style={"fontSize": "0.75rem"})
            for e in entities[:20]
        ], className="mb-3", style={"lineHeight": "2"}))

    path = resp_data.get("path", "")
    if path:
        elements.append(html.H6([
            html.I(className="fas fa-route me-2"),
            "Communication Path",
        ], className="text-info mb-2"))
        parts = [p.strip() for p in path.split("\u2192")]
        path_badges = []
        for i, part in enumerate(parts):
            color = "primary" if i == 0 or i == len(parts) - 1 else "secondary"
            path_badges.append(dbc.Badge(
                part.split("(")[0].strip(), color=color,
                className="me-1 mb-1 p-2",
            ))
            if i < len(parts) - 1:
                path_badges.append(html.Span(" \u2192 ", className="text-muted"))
        elements.append(html.Div(path_badges, className="mb-3"))

    sources = resp_data.get("sources", [])
    if sources:
        elements.append(html.H6([
            html.I(className="fas fa-envelope me-2"),
            "Email Sources",
        ], className="text-info mb-2"))
        for src in sources:
            elements.append(html.Div([
                html.I(className="fas fa-envelope-open me-2 text-muted"),
                html.Span(src, className="small"),
            ], className="mb-1"))
        elements.append(html.Div(className="mb-3"))

    if not tool_calls and not entities and not path and not sources:
        elements.append(html.P(
            "No structured provenance available for this response.",
            className="text-muted small",
        ))

    return html.Div(elements)


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
            "entities": resp.entities_mentioned,
            "tier": tier or "",
            "tool_calls": [
                {"name": tc.name, "arguments": tc.arguments, "output": tc.output}
                for tc in resp.tool_calls
            ],
        }
        prov = _render_provenance(resp_data)

        return {"messages": messages}, chat_children, prov, ""
