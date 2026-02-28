import dash_bootstrap_components as dbc
from dash import html, dcc


def apply_layout():
    return html.Div([
        html.Div([
            html.H1("Apply to Your Business", className="display-5 fw-bold"),
            html.P("The pattern is universal — map Bible entities to your domain",
                   className="lead text-muted"),
        ], className="text-center py-3"),
        html.Hr(),

        # Pattern mapping
        html.H4("Map the Pattern to YOUR Domain", className="mb-3"),
        html.P(
            "The Bible demo proves the architecture. The same 5-tool pattern works for "
            "any domain where you need auditable, multi-hop reasoning.",
            className="text-muted mb-3",
        ),
        dbc.Table([
            html.Thead(html.Tr([
                html.Th("GraphRAG Concept"),
                html.Th("Bible Demo"),
                html.Th("Your Domain"),
            ])),
            html.Tbody([
                html.Tr([
                    html.Td("Entities"),
                    html.Td("Person, Place, Event, Group, Concept", className="text-muted"),
                    html.Td("Customer, Product, Supplier, Facility"),
                ]),
                html.Tr([
                    html.Td("Relationships"),
                    html.Td("FAMILY_OF, TRAVELED_TO, SPOKE_TO", className="text-muted"),
                    html.Td("SUPPLIES_TO, MANUFACTURED_AT, DEPENDS_ON"),
                ]),
                html.Tr([
                    html.Td("Provenance"),
                    html.Td("Book + Chapter + Verse citations", className="text-muted"),
                    html.Td("Contract ID + Clause + Date + Approver"),
                ]),
                html.Tr([
                    html.Td("Multi-hop Query"),
                    html.Td('"How is Ruth connected to Jesus?"', className="text-muted"),
                    html.Td('"What suppliers are affected if Facility X shuts down?"'),
                ]),
                html.Tr([
                    html.Td("Source Documents"),
                    html.Td("KJV Bible (5 books)", className="text-muted"),
                    html.Td("ERP data, contracts, audit logs, regulatory filings"),
                ]),
            ]),
        ], bordered=True, dark=True, hover=True, striped=True, className="mb-4"),

        # Supply chain example
        html.H4("Example: Supply Chain Risk Analysis", className="mt-4 mb-3"),
        dbc.Row([
            dbc.Col(dbc.Card(dbc.CardBody([
                html.H5("Scenario", className="text-primary"),
                html.P(
                    "A pharmaceutical company needs to assess supply chain risk when a "
                    "key manufacturing facility faces disruption.",
                    className="text-muted",
                ),
                html.P([html.Strong("Question: "),
                        '"What products are affected if our Taiwan semiconductor supplier goes offline?"'],
                       className="text-muted"),
                html.P([html.Strong("GraphRAG traces: ")], className="text-muted mb-1"),
                html.Div([
                    dbc.Badge("Taiwan Fab", color="danger", className="me-1 p-2"),
                    html.Span(" → ", className="text-muted"),
                    dbc.Badge("Chip A", color="primary", className="me-1 p-2"),
                    html.Span(" → ", className="text-muted"),
                    dbc.Badge("Device X", color="primary", className="me-1 p-2"),
                    html.Span(" → ", className="text-muted"),
                    dbc.Badge("Hospital Y", color="danger", className="p-2"),
                ], className="mb-2"),
                html.P("Every hop is backed by a contract, a BOM record, or a sales agreement.",
                       className="text-muted small mt-2"),
            ]), style={"borderLeft": "3px solid #4682B4", "backgroundColor": "#1a1f2b"}), md=6),

            dbc.Col(dbc.Card(dbc.CardBody([
                html.H5("The 5-Tool Pattern (Generalized)", className="text-white"),
                dcc.Markdown("""```python
@tool
def find_entity(name: str) -> str:
    # SQL: SELECT * FROM entities WHERE name LIKE ...

@tool
def find_connections(entity_name: str) -> str:
    # SQL: SELECT * FROM relationships WHERE ...

@tool
def trace_path(entity_a: str, entity_b: str) -> str:
    # 1-hop → 2-hop → 3-hop SQL queries

@tool
def get_context(entity_name: str) -> str:
    # SQL: SELECT * FROM documents WHERE ...

@tool
def get_entity_summary(entity_name: str) -> str:
    # Combine entity info + relationships
```""", style={"fontSize": "0.8rem"}),
            ]), style={"backgroundColor": "#1a1f2b"}), md=6),
        ], className="g-3 mb-4"),

        # Roadmap
        html.Hr(),
        html.H4("5-Phase Implementation Roadmap", className="mt-3 mb-3"),
        *[_phase_row(p) for p in PHASES],

        # Metrics
        html.Hr(),
        html.H4("Success Metrics", className="mt-3 mb-3"),
        dbc.Table([
            html.Thead(html.Tr([html.Th("Metric"), html.Th("Target"), html.Th("How to Measure")])),
            html.Tbody([
                html.Tr([html.Td("Provenance Chain Score"), html.Td("≥ 95%"), html.Td("provenance_chain scorer (MLflow)")]),
                html.Tr([html.Td("Hallucination Rate"), html.Td("< 5%"), html.Td("hallucination_check scorer (MLflow)")]),
                html.Tr([html.Td("Citation Completeness"), html.Td("≥ 80%"), html.Td("citation_completeness scorer")]),
                html.Tr([html.Td("Reproducibility"), html.Td("100%"), html.Td("Run same query 3x, compare paths")]),
                html.Tr([html.Td("Query Latency (p95)"), html.Td("< 10s"), html.Td("MLflow trace latency metrics")]),
                html.Tr([html.Td("Cost per Query"), html.Td("< $0.01"), html.Td("System tables: billing usage")]),
            ]),
        ], bordered=True, dark=True, hover=True, striped=True, className="mb-4"),

        html.P(
            "Ready to get started? The full source code is in this repository. "
            "Run the notebooks (00–05) to build the knowledge graph, then deploy this web app.",
            className="text-center text-muted py-3",
        ),
    ])


PHASES = [
    {"n": "1", "title": "Data Foundation", "time": "1–2 weeks", "color": "#dc3545",
     "desc": "Set up Unity Catalog, ingest source documents into Delta tables, configure Foundation Model API access."},
    {"n": "2", "title": "Knowledge Graph Extraction", "time": "2–3 weeks", "color": "#4682B4",
     "desc": "Define entity and relationship types. Run LLM extraction. Validate graph quality with domain experts."},
    {"n": "3", "title": "Agent & Tools", "time": "1–2 weeks", "color": "#28a745",
     "desc": "Implement graph traversal tools (adapt the 5-tool pattern). Build LangGraph agent. Log with MLflow."},
    {"n": "4", "title": "Governance & Evaluation", "time": "1–2 weeks", "color": "#9b59b6",
     "desc": "Define evaluation dataset. Build custom scorers. Run evaluation, tune prompts."},
    {"n": "5", "title": "Deploy & Integrate", "time": "1–2 weeks", "color": "#ffc107",
     "desc": "Deploy agent to Model Serving. Build application UI. Set up monitoring with MLflow traces."},
]


def _phase_row(p):
    return dbc.Row([
        dbc.Col(
            html.Div([
                html.Div(f"Phase {p['n']}", className="fw-bold", style={"color": p["color"], "fontSize": "1.2rem"}),
                html.Small(p["time"], className="text-muted"),
            ], className="text-center pt-1"),
            width=2,
        ),
        dbc.Col(
            dbc.Card(dbc.CardBody([
                html.Strong(p["title"], className="text-white"),
                html.P(p["desc"], className="text-muted small mb-0"),
            ]), style={"borderLeft": f"3px solid {p['color']}", "backgroundColor": "#1a1f2b"}),
            width=10,
        ),
    ], className="g-2 mb-2")
