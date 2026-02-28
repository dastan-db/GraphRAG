import dash_bootstrap_components as dbc
from dash import html, dcc

MERMAID_DIAGRAM = """
graph TB
    subgraph workspace ["Databricks Workspace"]
        subgraph dataLayer ["Data Layer — Unity Catalog"]
            Verses["verses"]
            Entities["entities"]
            Rels["relationships"]
            Mentions["entity_mentions"]
        end
        subgraph modelLayer ["Model Layer"]
            LLM["Foundation Model API"]
            Agent["GraphRAG Agent"]
        end
        subgraph orchLayer ["Orchestration"]
            Tools["5 Graph Tools"]
            MLflow["MLflow Tracing"]
            Serving["Model Serving"]
        end
        subgraph appLayer ["Application"]
            WebApp["Web App"]
            Lakebase["Lakebase"]
        end
    end
    WebApp -->|query| Serving
    Serving --> Agent
    Agent -->|tool calls| Tools
    Tools -->|SQL| dataLayer
    Agent --> LLM
    Agent --> MLflow
    WebApp --> Lakebase
"""


def _info_card(icon, title, text, color):
    return dbc.Col(dbc.Card(dbc.CardBody([
        html.H5([html.I(className=f"fas {icon} me-2"), title], style={"color": color}),
        html.P(text, className="text-muted small mb-0"),
    ]), style={"border": "1px solid #333", "backgroundColor": "#1a1f2b"}, className="h-100"), md=4)


def arch_layout():
    return html.Div([
        html.Div([
            html.H1("Architecture", className="display-5 fw-bold"),
            html.P("How GraphRAG fits inside a Databricks workspace", className="lead text-muted"),
        ], className="text-center py-3"),
        html.Hr(),

        # Mermaid diagram
        html.H4("System Diagram", className="mb-3"),
        html.Div(
            dcc.Markdown(f"```mermaid\n{MERMAID_DIAGRAM}\n```"),
            className="p-3 rounded",
            style={"backgroundColor": "#1a1f2b", "border": "1px solid #333"},
        ),
        html.Small("Diagram: full data flow from web app through agent to Delta tables.", className="text-muted d-block mb-4"),

        # Comparison table
        html.H4("Traditional RAG vs GraphRAG", className="mt-4 mb-3"),
        dbc.Table([
            html.Thead(html.Tr([
                html.Th("Dimension", style={"width": "20%"}),
                html.Th("Traditional RAG"),
                html.Th("GraphRAG"),
            ])),
            html.Tbody([
                html.Tr([
                    html.Td("Retrieval", className="fw-bold"),
                    html.Td("Embedding similarity over flat text chunks", className="text-muted"),
                    html.Td("Structured graph traversal over entities and relationships"),
                ]),
                html.Tr([
                    html.Td("Auditability", className="fw-bold"),
                    html.Td("Opaque — cannot explain which relationships led to the answer", className="text-muted"),
                    html.Td("Full provenance — path, source citations, grounding indicator"),
                ]),
                html.Tr([
                    html.Td("Multi-hop", className="fw-bold"),
                    html.Td("Weak — single-hop chunk retrieval", className="text-muted"),
                    html.Td("Native — trace_path finds multi-hop connections"),
                ]),
                html.Tr([
                    html.Td("Hallucination", className="fw-bold"),
                    html.Td("LLM can invent connections between unrelated chunks", className="text-muted"),
                    html.Td("Structural prevention — agent reasons only over graph evidence"),
                ]),
                html.Tr([
                    html.Td("Reproducibility", className="fw-bold"),
                    html.Td("Non-deterministic — embedding drift alters results", className="text-muted"),
                    html.Td("Deterministic — same query returns same path"),
                ]),
                html.Tr([
                    html.Td("Cost", className="fw-bold"),
                    html.Td("High — large context windows needed", className="text-muted"),
                    html.Td("Low — targeted graph queries (90-900x savings)"),
                ]),
                html.Tr([
                    html.Td("Infrastructure", className="fw-bold"),
                    html.Td("Vector database + embedding model + LLM", className="text-muted"),
                    html.Td("Delta tables in Unity Catalog — no external DB"),
                ]),
            ]),
        ], bordered=True, dark=True, hover=True, striped=True, className="mb-4"),

        # Why Databricks
        html.H4("Why Databricks", className="mt-4 mb-3"),
        dbc.Row([
            _info_card("fa-lock", "Privacy & Security",
                       "Data never leaves your workspace. No external API calls for retrieval. "
                       "Unity Catalog governs access at the table, column, and row level.",
                       "#dc3545"),
            _info_card("fa-chart-bar", "Governance & Lineage",
                       "MLflow traces every agent call. Unity Catalog tracks data lineage. "
                       "System tables provide audit logs. Every question, every answer — recorded.",
                       "#4682B4"),
            _info_card("fa-link", "Native Integration",
                       "Foundation Model APIs, Model Serving, Vector Search, Lakebase, "
                       "Asset Bundles — one platform, one security model, no glue code.",
                       "#28a745"),
        ], className="g-3"),
    ])
