import dash_bootstrap_components as dbc
from dash import html

STEPS = [
    {
        "num": "01", "icon": "fa-file-alt", "title": "Load Unstructured Documents",
        "color": "#dc3545",
        "desc": (
            "Point the pipeline at a document collection. The data is ingested and stored as "
            "Delta tables in Unity Catalog. In our demo, we load five books of the King James "
            "Bible: Genesis, Exodus, Ruth, Matthew, and Acts."
        ),
        "notebook": "01_Data_Prep.py",
    },
    {
        "num": "02", "icon": "fa-search", "title": "Extract Entities & Relationships",
        "color": "#4682B4",
        "desc": (
            "An LLM reads every chapter and extracts structured entities (Person, Place, Event, "
            "Group, Concept) and relationships (FAMILY_OF, TRAVELED_TO, SPOKE_TO). Each "
            "relationship is traced back to its source chapter."
        ),
        "notebook": "02_Build_Knowledge_Graph.py",
    },
    {
        "num": "03", "icon": "fa-project-diagram", "title": "Build Knowledge Graph in Delta",
        "color": "#28a745",
        "desc": (
            "Entities become nodes, relationships become edges — all in Unity Catalog. "
            "No external graph database needed. Four Delta tables: entities, relationships, "
            "entity_mentions, and verses."
        ),
        "notebook": "02_Build_Knowledge_Graph.py",
    },
    {
        "num": "04", "icon": "fa-robot", "title": "Create Agent with Graph Tools",
        "color": "#9b59b6",
        "desc": (
            "A LangGraph agent is equipped with five graph traversal tools: find_entity, "
            "find_connections, trace_path, get_context_verses, and get_entity_summary. "
            "Each tool issues SQL queries against the Delta tables."
        ),
        "notebook": "03_Build_Agent.py",
    },
    {
        "num": "05", "icon": "fa-comments", "title": "Ask Questions, Get Auditable Answers",
        "color": "#ffc107",
        "desc": (
            "Ask any question in natural language. The agent traverses the knowledge graph, "
            "retrieves connected evidence with full provenance, and returns a structured answer "
            "with path, source citations, and a grounding indicator."
        ),
        "notebook": "04_Query_Demo.py",
    },
]


def how_layout():
    rows = []
    for s in STEPS:
        rows.append(
            dbc.Row([
                dbc.Col(
                    html.Div([
                        html.I(className=f"fas {s['icon']} fa-2x", style={"color": s["color"]}),
                        html.Div(f"Step {s['num']}", className="fw-bold mt-2", style={"color": s["color"], "fontSize": "1.2rem"}),
                    ], className="text-center pt-2"),
                    width=1,
                ),
                dbc.Col(
                    dbc.Card(dbc.CardBody([
                        html.H5(s["title"], className="text-white mb-2"),
                        html.P(s["desc"], className="text-muted mb-1"),
                        html.Code(s["notebook"], style={"color": s["color"], "fontSize": "0.8rem"}),
                    ]), style={"borderLeft": f"3px solid {s['color']}", "backgroundColor": "#1a1f2b"}),
                    width=11,
                ),
            ], className="g-3 mb-3")
        )

    return html.Div([
        html.Div([
            html.H1("How It Works", className="display-5 fw-bold"),
            html.P("Five steps from raw documents to auditable AI answers", className="lead text-muted"),
        ], className="text-center py-3"),
        html.Hr(),
        *rows,
        html.Hr(),
        html.P(
            "The entire pipeline runs on Databricks — Unity Catalog, Delta Lake, "
            "Model Serving, and MLflow. No external APIs, no separate graph databases.",
            className="text-center text-muted py-2",
        ),
    ])
