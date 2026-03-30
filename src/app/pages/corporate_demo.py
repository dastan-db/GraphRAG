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
    "get_source_context": "fas fa-envelope-open-text",
    "get_entity_summary": "fas fa-id-card",
    "trace_path": "fas fa-route",
    "list_entities_by_book": "fas fa-list",
    "compare_entity_sets": "fas fa-not-equal",
    "find_cross_book_entities": "fas fa-sitemap",
    "get_source_evidence": "fas fa-book-open",
}

_TOOL_DESCRIPTIONS = {
    "find_entity": "Entity lookup",
    "find_connections": "Relationship traversal",
    "get_source_emails": "Email source retrieval",
    "get_source_context": "Source text retrieval",
    "get_entity_summary": "Entity profile",
    "trace_path": "Path discovery",
    "list_entities_by_book": "Entity listing",
    "compare_entity_sets": "Set comparison",
    "find_cross_book_entities": "Cross-context entities",
    "get_source_evidence": "Source text retrieval",
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


_OUTPUT_STYLE = {
    "fontSize": "0.7rem", "color": "#c9d1d9",
    "backgroundColor": "#010409",
    "padding": "0.5rem", "borderRadius": "4px",
    "maxHeight": "250px", "overflowY": "auto",
    "marginBottom": "0",
}


def _summarize_output(tool_name: str, data) -> str:
    """Produce a one-line summary from structured tool output."""
    if isinstance(data, list):
        return f"{len(data)} entities matched"
    if isinstance(data, dict):
        total = data.get("total")
        if tool_name == "find_connections" and "by_type" in data:
            n_types = len(data["by_type"])
            return f"{total} connections across {n_types} relationship type{'s' if n_types != 1 else ''}"
        if tool_name in ("get_source_evidence", "get_source_context", "get_source_emails") and "emails" in data:
            return f"{total} emails found"
        if tool_name in ("get_source_evidence", "get_source_context", "get_source_emails") and "verses" in data:
            return f"{total} verses found"
        if tool_name == "get_entity_summary":
            rels = data.get("relationships", {})
            n_rels = rels.get("total", 0)
            return f"{data.get('type', 'Entity')}: {n_rels} relationships"
        if tool_name == "trace_path":
            hops = data.get("hops", len(data.get("path", data.get("paths", []))))
            return f"Path found ({hops} hop{'s' if hops != 1 else ''})"
        if tool_name == "list_entities_by_book" and "by_type" in data:
            return f"{total} entities in {len(data['by_type'])} categories"
        if tool_name == "compare_entity_sets":
            return f"{data.get('result_count', 0)} entities in result"
        if tool_name == "find_cross_book_entities":
            return f"{total} cross-thread entities"
        if total is not None:
            return f"{total} results"
    if isinstance(data, str):
        first_line = data.split("\n", 1)[0]
        return first_line[:80]
    return ""


def _render_entity_list(data) -> html.Div:
    """Render find_entity output as compact entity cards."""
    items = data if isinstance(data, list) else []
    children = []
    for ent in items[:10]:
        children.append(html.Div([
            html.Span([
                html.Strong(ent.get("name", ""), style={"color": "#e6e6e6"}),
                dbc.Badge(
                    ent.get("type", ""),
                    color="info", className="ms-2",
                    style={"fontSize": "0.6rem", "fontWeight": "normal"},
                ),
            ]),
            html.Div(
                (ent.get("description") or "")[:120],
                className="text-muted",
                style={"fontSize": "0.65rem", "lineHeight": "1.3"},
            ),
        ], className="mb-1 pb-1", style={"borderBottom": "1px solid #21262d"}))
    if len(items) > 10:
        children.append(html.Span(
            f"… and {len(items) - 10} more",
            className="text-muted", style={"fontSize": "0.65rem"},
        ))
    return html.Div(children, style=_OUTPUT_STYLE)


def _render_connections(data: dict) -> html.Div:
    """Render find_connections output grouped by relationship type."""
    by_type = data.get("by_type", {})
    sections = []
    for rel_type, items in sorted(by_type.items(), key=lambda x: -len(x[1])):
        rows = []
        for item in items[:5]:
            rows.append(html.Div([
                html.Span(item.get("source", ""), style={"color": "#e6e6e6", "fontSize": "0.65rem"}),
                html.Span(" → ", className="text-muted", style={"fontSize": "0.65rem"}),
                html.Span(item.get("target", ""), style={"color": "#e6e6e6", "fontSize": "0.65rem"}),
            ]))
        overflow = len(items) - 5
        if overflow > 0:
            rows.append(html.Span(
                f"… +{overflow} more",
                className="text-muted", style={"fontSize": "0.6rem"},
            ))
        sections.append(html.Details([
            html.Summary([
                dbc.Badge(rel_type, color="secondary",
                          style={"fontSize": "0.6rem", "fontWeight": "normal"}),
                html.Span(
                    f" ({len(items)})",
                    className="text-muted", style={"fontSize": "0.65rem"},
                ),
            ], style={"cursor": "pointer", "fontSize": "0.7rem"}, className="mb-1"),
            html.Div(rows, className="ms-2 mb-1"),
        ], open=len(by_type) <= 3))
    return html.Div(sections, style=_OUTPUT_STYLE)


def _render_emails(data: dict) -> html.Div:
    """Render email source context as compact rows."""
    emails = data.get("emails", [])
    rows = []
    for em in emails[:8]:
        rows.append(html.Div([
            html.Span(em.get("date", ""), className="text-muted me-2",
                      style={"fontSize": "0.6rem", "minWidth": "70px", "display": "inline-block"}),
            html.Span(em.get("sender", ""), style={"color": "#8be9fd", "fontSize": "0.65rem"}),
            html.Span(f" | {em.get('subject', '')}", className="text-muted",
                      style={"fontSize": "0.65rem"}),
        ], className="mb-1"))
    if len(emails) > 8:
        rows.append(html.Span(
            f"… +{len(emails) - 8} more emails",
            className="text-muted", style={"fontSize": "0.6rem"},
        ))
    return html.Div(rows, style=_OUTPUT_STYLE)


def _render_entity_summary(data: dict) -> html.Div:
    """Render get_entity_summary output as an entity card with relationships."""
    children = [
        html.Div([
            html.Strong(data.get("name", ""), style={"color": "#e6e6e6"}),
            dbc.Badge(data.get("type", ""), color="info", className="ms-2",
                      style={"fontSize": "0.6rem", "fontWeight": "normal"}),
        ]),
        html.Div(
            (data.get("description") or "")[:200],
            className="text-muted mb-1",
            style={"fontSize": "0.65rem"},
        ),
    ]
    if data.get("first_mention"):
        children.append(html.Div(
            f"First mention: {data['first_mention']}",
            style={"fontSize": "0.6rem", "color": "#8b949e"},
        ))
    rels = data.get("relationships", {})
    if rels and rels.get("by_type"):
        children.append(html.Hr(style={"borderColor": "#21262d", "margin": "0.3rem 0"}))
        children.append(_render_connections(rels))
    return html.Div(children, style=_OUTPUT_STYLE)


def _render_path(data: dict) -> html.Div:
    """Render trace_path output as a visual chain."""
    path_steps = data.get("path", data.get("paths", []))
    badges = []
    for i, step in enumerate(path_steps):
        src = step.get("source", "")
        rel = step.get("relationship", "")
        tgt = step.get("target", "")
        if i == 0:
            badges.append(dbc.Badge(src, color="primary", className="me-1 mb-1",
                                    style={"fontSize": "0.65rem"}))
        badges.append(html.Span(
            f"—[{rel}]→", className="text-muted mx-1",
            style={"fontSize": "0.6rem"},
        ))
        color = "primary" if i == len(path_steps) - 1 else "secondary"
        badges.append(dbc.Badge(tgt, color=color, className="me-1 mb-1",
                                style={"fontSize": "0.65rem"}))
    return html.Div(badges, style={**_OUTPUT_STYLE, "lineHeight": "2"})


def _render_tool_output(tool_name: str, data, redaction_count: int, tier: str):
    """Dispatch to the appropriate rich renderer based on tool name and data shape."""
    redaction_warning = None
    if redaction_count > 0:
        redaction_warning = html.Div([
            html.I(className="fas fa-lock me-1"),
            html.Span(
                f"{redaction_count} field{'s' if redaction_count != 1 else ''} "
                f"redacted for {tier.replace('_', ' ')}",
                className="small",
            ),
        ], className="text-warning mt-1", style={"fontSize": "0.7rem"})

    output_widget = None

    if isinstance(data, str):
        output_widget = dcc.Markdown(
            data[:1200] + ("\n…" if len(data) > 1200 else ""),
            style={"fontSize": "0.7rem", "color": "#c9d1d9"},
        )
    elif tool_name == "find_entity" and isinstance(data, list):
        output_widget = _render_entity_list(data)
    elif tool_name == "find_connections" and isinstance(data, dict) and "by_type" in data:
        output_widget = _render_connections(data)
    elif tool_name in ("get_source_evidence", "get_source_context", "get_source_emails") and isinstance(data, dict):
        if "emails" in data:
            output_widget = _render_emails(data)
        elif "verses" in data:
            verses = data["verses"]
            rows = []
            for v in verses[:10]:
                rows.append(html.Div([
                    html.Strong(v.get("reference", ""), style={"fontSize": "0.65rem", "color": "#8be9fd"}),
                    html.Span(f" — {v.get('text', '')[:150]}", className="text-muted",
                              style={"fontSize": "0.65rem"}),
                ], className="mb-1"))
            if len(verses) > 10:
                rows.append(html.Span(f"… +{len(verses) - 10} more", className="text-muted",
                                      style={"fontSize": "0.6rem"}))
            output_widget = html.Div(rows, style=_OUTPUT_STYLE)
    elif tool_name == "get_entity_summary" and isinstance(data, dict):
        output_widget = _render_entity_summary(data)
    elif tool_name == "trace_path" and isinstance(data, dict) and ("path" in data or "paths" in data):
        output_widget = _render_path(data)
    elif tool_name == "compare_entity_sets" and isinstance(data, dict):
        result = data.get("result", [])
        children = [
            html.Div(f"Set A: {data.get('set_a', {}).get('description', '')} ({data.get('set_a', {}).get('count', 0)})",
                      className="text-muted", style={"fontSize": "0.65rem"}),
            html.Div(f"Set B: {data.get('set_b', {}).get('description', '')} ({data.get('set_b', {}).get('count', 0)})",
                      className="text-muted", style={"fontSize": "0.65rem"}),
            html.Div(f"Operation: {data.get('operation', '')}", className="text-muted mb-1",
                      style={"fontSize": "0.65rem"}),
        ]
        for name in result[:15]:
            children.append(dbc.Badge(name, color="secondary", className="me-1 mb-1",
                                      style={"fontSize": "0.6rem"}))
        if len(result) > 15:
            children.append(html.Span(f"… +{len(result) - 15} more", className="text-muted",
                                      style={"fontSize": "0.6rem"}))
        output_widget = html.Div(children, style=_OUTPUT_STYLE)
    else:
        pretty = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        if len(pretty) > 1200:
            pretty = pretty[:1200] + "\n  …"
        output_widget = html.Pre(pretty, style={**_OUTPUT_STYLE, "whiteSpace": "pre-wrap", "wordBreak": "break-word"})

    parts = [output_widget]
    if redaction_warning:
        parts.append(redaction_warning)
    return html.Div(parts, className="mt-1")


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

        for tc in tool_calls:
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
                    summary_text = _summarize_output(tc["name"], redacted_obj)
                    if summary_text:
                        card_children.append(html.Div(
                            summary_text,
                            className="mt-1",
                            style={"fontSize": "0.7rem", "color": "#8b949e"},
                        ))

                    card_children.append(
                        html.Details([
                            html.Summary(
                                "Show details",
                                className="small text-muted mt-1",
                                style={"cursor": "pointer", "fontSize": "0.7rem"},
                            ),
                            _render_tool_output(tc["name"], redacted_obj, redaction_count, tier),
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
