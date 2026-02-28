import dash_bootstrap_components as dbc
from dash import html


def _metric_card(value, label, description, color):
    return dbc.Col(dbc.Card([
        dbc.CardBody([
            html.H2(value, className="mb-1 fw-bold", style={"color": color, "fontSize": "2.2rem"}),
            html.H6(label, className="text-white mb-1"),
            html.Small(description, className="text-muted"),
        ])
    ], style={"borderLeft": f"4px solid {color}", "backgroundColor": "#1a1f2b"}, className="h-100"), md=3)


def home_layout():
    return html.Div([
        # Hero
        html.Div([
            html.H1("GraphRAG", className="display-3 fw-bold text-danger"),
            html.P("Auditable, Traceable AI Reasoning — Built on Databricks",
                   className="lead text-muted", style={"maxWidth": "700px", "margin": "auto"}),
        ], className="text-center py-4"),

        html.Hr(),

        # Problem / Solution
        dbc.Row([
            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H4("The Problem", className="text-danger"),
                    html.P([
                        "When an LLM makes a recommendation, ",
                        html.Strong("no one can prove why"),
                        ".",
                    ]),
                    html.Ul([
                        html.Li("Compliance teams cannot audit AI decisions"),
                        html.Li("Regulators cannot verify answers are grounded in approved data"),
                        html.Li("The same question asked twice may produce different answers"),
                        html.Li("When the AI hallucinates, there is no mechanism to detect it"),
                    ], className="text-muted"),
                    html.Small(
                        "Standard RAG retrieves flat text chunks by embedding similarity — but the "
                        "retrieval step itself is opaque.",
                        className="text-muted",
                    ),
                ])
            ], style={"borderLeft": "4px solid #dc3545", "backgroundColor": "#1a1f2b"}, className="h-100"), md=6),

            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H4("The Solution", className="text-success"),
                    html.P([html.Strong("GraphRAG makes everything traceable.")]),
                    html.Ul([
                        html.Li(["Every answer includes a structured ", html.Strong("provenance chain")]),
                        html.Li("Every claim traces to a specific source document"),
                        html.Li("The same query always returns the same path"),
                        html.Li("Hallucinations are structurally prevented — the AI reasons only over graph evidence"),
                    ], className="text-muted"),
                    html.Small(
                        "Replace opaque embedding retrieval with structured graph traversal. "
                        "Every answer shows its work.",
                        className="text-muted",
                    ),
                ])
            ], style={"borderLeft": "4px solid #28a745", "backgroundColor": "#1a1f2b"}, className="h-100"), md=6),
        ], className="g-4 mb-4"),

        # Metrics
        html.H4("Key Outcomes", className="mt-4 mb-3"),
        dbc.Row([
            _metric_card("100%", "Auditable", "Every answer includes provenance: path, sources, grounding", "#28a745"),
            _metric_card("0%", "Hallucination Rate", "Retrieval constrained to knowledge graph evidence", "#dc3545"),
            _metric_card("90–900×", "Cost Reduction", "vs GPT-4 API — structured retrieval replaces massive context", "#4682B4"),
            _metric_card("✓", "Deterministic", "Same query over same graph returns same path every time", "#ffc107"),
        ], className="g-3 mb-4"),

        html.Hr(),
        html.P(
            "Navigate to How It Works to see the 5-step pipeline, "
            "or jump straight to the Live Demo to see it in action.",
            className="text-center text-muted py-2",
        ),
    ])
