import os
import dash
import dash_bootstrap_components as dbc
from dash import html, dcc, Input, Output, State, callback, no_update

from pages.home import home_layout
from pages.how_it_works import how_layout
from pages.architecture import arch_layout
from pages.live_demo import demo_layout, register_demo_callbacks
from pages.apply import apply_layout

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.DARKLY, dbc.icons.FONT_AWESOME],
    suppress_callback_exceptions=True,
    title="GraphRAG — Auditable AI",
)

SIDEBAR_STYLE = {
    "position": "fixed",
    "top": 0,
    "left": 0,
    "bottom": 0,
    "width": "240px",
    "padding": "2rem 1rem",
    "backgroundColor": "#1a1f2b",
    "overflowY": "auto",
}

CONTENT_STYLE = {
    "marginLeft": "260px",
    "padding": "2rem 2.5rem",
    "minHeight": "100vh",
}

NAV_ITEMS = [
    {"label": "Tell", "children": [
        {"href": "/", "icon": "fa-home", "text": "Home"},
        {"href": "/how-it-works", "icon": "fa-cogs", "text": "How It Works"},
        {"href": "/architecture", "icon": "fa-sitemap", "text": "Architecture"},
    ]},
    {"label": "Show", "children": [
        {"href": "/live-demo", "icon": "fa-rocket", "text": "Live Demo"},
    ]},
    {"label": "Tell", "children": [
        {"href": "/apply", "icon": "fa-briefcase", "text": "Apply to Business"},
    ]},
]

def make_sidebar():
    nav_links = []
    for group in NAV_ITEMS:
        nav_links.append(html.Small(group["label"], className="text-muted text-uppercase fw-bold mt-3 mb-1 d-block px-2"))
        for item in group["children"]:
            nav_links.append(
                dbc.NavLink(
                    [html.I(className=f"fas {item['icon']} me-2"), item["text"]],
                    href=item["href"],
                    active="exact",
                    className="rounded",
                )
            )
    return html.Div([
        html.H4("GraphRAG", className="text-danger fw-bold mb-0"),
        html.Small("Auditable AI", className="text-muted"),
        html.Hr(),
        dbc.Nav(nav_links, vertical=True, pills=True),
    ], style=SIDEBAR_STYLE)


app.layout = html.Div([
    dcc.Location(id="url", refresh=False),
    make_sidebar(),
    html.Div(id="page-content", style=CONTENT_STYLE),
])


@callback(Output("page-content", "children"), Input("url", "pathname"))
def render_page(pathname):
    if pathname == "/how-it-works":
        return how_layout()
    elif pathname == "/architecture":
        return arch_layout()
    elif pathname == "/live-demo":
        return demo_layout()
    elif pathname == "/apply":
        return apply_layout()
    return home_layout()


register_demo_callbacks(app)

server = app.server

if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
