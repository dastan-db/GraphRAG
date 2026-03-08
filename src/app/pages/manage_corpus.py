"""Manage Corpus page — add/remove Bible books and see graph impact in real time."""

from __future__ import annotations

import json
import os
import sys
import traceback

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, dcc, html, no_update

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from bible_registry import BIBLE_BOOKS_ALL

USE_MOCK = os.getenv("USE_MOCK_BACKEND", "false").lower() == "true"

_STATUS_COLORS = {
    "active": "success",
    "available": "secondary",
    "processing": "warning",
    "failed": "danger",
}

_CARD_STYLE_ACTIVE = {
    "backgroundColor": "#1a2e1a",
    "border": "1px solid #28a745",
    "cursor": "pointer",
}
_CARD_STYLE_AVAILABLE = {
    "backgroundColor": "#1a1f2b",
    "border": "1px solid #444",
    "cursor": "pointer",
}
_CARD_STYLE_PROCESSING = {
    "backgroundColor": "#2e2a1a",
    "border": "1px solid #ffc107",
    "cursor": "default",
    "opacity": "0.8",
}


def _book_card(name: str, meta: dict, status: str = "available",
               entity_count: int = 0, rel_count: int = 0) -> dbc.Card:
    """Render a single book as a selectable card."""
    color = _STATUS_COLORS.get(status, "secondary")
    if status == "active":
        style = _CARD_STYLE_ACTIVE.copy()
    elif status == "processing":
        style = _CARD_STYLE_PROCESSING.copy()
    else:
        style = _CARD_STYLE_AVAILABLE.copy()

    stats_text = ""
    if status == "active" and entity_count > 0:
        stats_text = f"{entity_count} entities, {rel_count} rels"

    return dbc.Card(
        dbc.CardBody([
            dbc.Checkbox(
                id={"type": "book-check", "book": name},
                value=False,
                className="float-end",
                disabled=status == "processing",
            ),
            html.H6(name, className="mb-1", style={"fontSize": "0.9rem"}),
            html.Div([
                dbc.Badge(status.upper(), color=color, className="me-1",
                          style={"fontSize": "0.65rem"}),
                html.Small(f"{meta['chapters']} ch", className="text-muted"),
            ]),
            html.Small(stats_text, className="text-muted d-block mt-1",
                       style={"fontSize": "0.7rem"}) if stats_text else html.Div(),
        ], className="p-2"),
        style=style,
        className="mb-2",
    )


def _enterprise_callout() -> html.Div:
    return dbc.Alert([
        html.H6("Enterprise Pattern: Dynamic Data Source Management", className="alert-heading mb-2"),
        html.P([
            "In enterprise applications, ",
            html.Strong("books = data sources"),
            " (ERP systems, contracts, audit logs). Adding a book triggers the full "
            "ingestion pipeline — download, entity extraction, relationship extraction, "
            "and graph analytics rebuild. Removing a book demonstrates how revoking "
            "access to a data source changes the connections visible to the agent.",
        ], className="mb-0 small"),
    ], color="info", className="mb-3")


def _stats_panel(stats=None) -> html.Div:
    """Render the graph stats dashboard."""
    if stats is None:
        return html.Div(
            html.P("Loading stats...", className="text-muted text-center py-4"),
            id="stats-content",
        )

    metrics = [
        ("Entities", stats.total_entities, "fa-users", "danger"),
        ("Relationships", stats.total_relationships, "fa-project-diagram", "primary"),
        ("Active Books", stats.active_books, "fa-book", "success"),
        ("Cross-Book Entities", stats.cross_book_entities, "fa-random", "warning"),
    ]

    metric_cards = []
    for label, value, icon, color in metrics:
        metric_cards.append(dbc.Col(
            dbc.Card(dbc.CardBody([
                html.Div([
                    html.I(className=f"fas {icon} me-2 text-{color}"),
                    html.Small(label, className="text-muted"),
                ]),
                html.H4(f"{value:,}", className=f"text-{color} mb-0 mt-1"),
            ], className="p-2 text-center"), style={"backgroundColor": "#1a1f2b"}),
            width=3,
        ))

    entity_type_items = []
    for etype, count in (stats.entity_type_counts or {}).items():
        entity_type_items.append(
            dbc.ListGroupItem([
                html.Span(etype),
                dbc.Badge(f"{count:,}", color="light", text_color="dark", className="float-end"),
            ], style={"backgroundColor": "#1a1f2b", "border": "1px solid #333",
                       "padding": "0.4rem 0.75rem"})
        )

    rel_type_items = []
    for rtype, count in list((stats.relationship_type_counts or {}).items())[:8]:
        rel_type_items.append(
            dbc.ListGroupItem([
                html.Span(rtype, style={"fontSize": "0.85rem"}),
                dbc.Badge(f"{count:,}", color="light", text_color="dark", className="float-end"),
            ], style={"backgroundColor": "#1a1f2b", "border": "1px solid #333",
                       "padding": "0.4rem 0.75rem"})
        )

    return html.Div([
        dbc.Row(metric_cards, className="g-2 mb-3"),
        dbc.Row([
            dbc.Col([
                html.H6("Entity Types", className="text-muted mb-2"),
                dbc.ListGroup(entity_type_items, flush=True) if entity_type_items
                else html.P("No data", className="text-muted small"),
            ], md=6),
            dbc.Col([
                html.H6("Top Relationship Types", className="text-muted mb-2"),
                dbc.ListGroup(rel_type_items, flush=True) if rel_type_items
                else html.P("No data", className="text-muted small"),
            ], md=6),
        ], className="g-3"),
    ])


def manage_corpus_layout():
    return html.Div([
        html.Div([
            html.H1("Manage Corpus", className="display-5 fw-bold"),
            html.P(
                "Add or remove Bible books to see how the knowledge graph evolves in real time",
                className="lead text-muted",
            ),
        ], className="text-center py-3"),

        _enterprise_callout(),

        html.Hr(),

        # Action bar
        dbc.Row([
            dbc.Col([
                dbc.ButtonGroup([
                    dbc.Button([
                        html.I(className="fas fa-plus me-2"),
                        "Add Selected",
                    ], id="add-books-btn", color="success", size="sm", disabled=True),
                    dbc.Button([
                        html.I(className="fas fa-minus me-2"),
                        "Remove Selected",
                    ], id="remove-books-btn", color="danger", size="sm", disabled=True),
                ]),
            ], width="auto"),
            dbc.Col([
                html.Div(id="selection-summary", className="text-muted small pt-1"),
            ]),
            dbc.Col([
                dbc.Button([
                    html.I(className="fas fa-sync-alt me-1"),
                    "Refresh",
                ], id="refresh-btn", color="outline-secondary", size="sm"),
            ], width="auto"),
        ], className="mb-3 align-items-center"),

        # Pipeline progress (hidden by default)
        html.Div(id="pipeline-progress", className="mb-3"),

        # Main content: books + stats
        dbc.Row([
            # Book grid
            dbc.Col([
                dbc.Tabs([
                    dbc.Tab(label="Old Testament (39)", tab_id="ot"),
                    dbc.Tab(label="New Testament (27)", tab_id="nt"),
                    dbc.Tab(label="All Books (66)", tab_id="all"),
                ], id="testament-tabs", active_tab="all", className="mb-3"),
                html.Div(id="book-grid"),
            ], md=7),

            # Stats dashboard
            dbc.Col([
                html.H5("Knowledge Graph Stats", className="mb-3"),
                html.Div(id="stats-panel"),
            ], md=5),
        ], className="g-4"),

        # Hidden stores
        dcc.Store(id="book-statuses-store", data={}),
        dcc.Store(id="pipeline-run-store", data={"run_id": None, "action": None}),
        dcc.Interval(id="pipeline-poll-interval", interval=5000, disabled=True),
        dcc.Interval(id="initial-load-interval", interval=100, max_intervals=1),
    ])


def register_corpus_callbacks(app):

    @app.callback(
        Output("book-statuses-store", "data"),
        Output("stats-panel", "children"),
        Input("initial-load-interval", "n_intervals"),
        Input("refresh-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def load_data(_n_intervals, _refresh_clicks):
        """Load book statuses and graph stats on page load or refresh."""
        if USE_MOCK:
            from backend.graph_client import get_book_statuses_mock, get_graph_stats_mock
            statuses = get_book_statuses_mock()
            stats = get_graph_stats_mock()
        else:
            from backend.graph_client import get_book_statuses, get_graph_stats
            try:
                statuses = get_book_statuses()
                stats = get_graph_stats()
            except Exception:
                statuses = []
                stats = None

        status_dict = {}
        for s in statuses:
            status_dict[s.book_name] = {
                "testament": s.testament,
                "total_chapters": s.total_chapters,
                "status": s.status,
                "entity_count": s.entity_count,
                "relationship_count": s.relationship_count,
                "verse_count": s.verse_count,
            }

        for name, meta in BIBLE_BOOKS_ALL.items():
            if name not in status_dict:
                status_dict[name] = {
                    "testament": meta["testament"],
                    "total_chapters": meta["chapters"],
                    "status": "available",
                    "entity_count": 0,
                    "relationship_count": 0,
                    "verse_count": 0,
                }

        stats_panel = _stats_panel(stats)
        return status_dict, stats_panel

    @app.callback(
        Output("book-grid", "children"),
        Input("book-statuses-store", "data"),
        Input("testament-tabs", "active_tab"),
    )
    def render_book_grid(status_dict, active_tab):
        """Render the book grid based on the selected testament tab."""
        if not status_dict:
            return html.P("Loading books...", className="text-muted text-center py-4")

        ot_books = []
        nt_books = []
        for name, meta in BIBLE_BOOKS_ALL.items():
            info = status_dict.get(name, {})
            status = info.get("status", "available")
            e_count = info.get("entity_count", 0)
            r_count = info.get("relationship_count", 0)
            card = _book_card(name, meta, status, e_count, r_count)
            if meta["testament"] == "OT":
                ot_books.append(dbc.Col(card, xs=6, sm=4, md=4, lg=3))
            else:
                nt_books.append(dbc.Col(card, xs=6, sm=4, md=4, lg=3))

        if active_tab == "ot":
            return dbc.Row(ot_books, className="g-2")
        elif active_tab == "nt":
            return dbc.Row(nt_books, className="g-2")
        else:
            return html.Div([
                html.H6("Old Testament", className="text-muted mb-2"),
                dbc.Row(ot_books, className="g-2 mb-3"),
                html.H6("New Testament", className="text-muted mb-2"),
                dbc.Row(nt_books, className="g-2"),
            ])

    @app.callback(
        Output("add-books-btn", "disabled"),
        Output("remove-books-btn", "disabled"),
        Output("selection-summary", "children"),
        Input({"type": "book-check", "book": ALL}, "value"),
        State("book-statuses-store", "data"),
    )
    def update_action_buttons(checked_values, status_dict):
        """Enable/disable action buttons based on selection."""
        if not status_dict:
            return True, True, ""

        book_names = list(BIBLE_BOOKS_ALL.keys())
        selected_available = []
        selected_active = []

        for i, checked in enumerate(checked_values):
            if checked and i < len(book_names):
                name = book_names[i]
                info = status_dict.get(name, {})
                status = info.get("status", "available")
                if status == "available":
                    selected_available.append(name)
                elif status == "active":
                    selected_active.append(name)

        total_selected = len(selected_available) + len(selected_active)
        parts = []
        if selected_available:
            parts.append(f"{len(selected_available)} to add")
        if selected_active:
            parts.append(f"{len(selected_active)} to remove")

        summary = f"{total_selected} selected" + (f" ({', '.join(parts)})" if parts else "")
        can_add = len(selected_available) > 0
        can_remove = len(selected_active) > 0

        return not can_add, not can_remove, summary if total_selected > 0 else ""

    @app.callback(
        Output("pipeline-run-store", "data"),
        Output("pipeline-poll-interval", "disabled"),
        Output("pipeline-progress", "children", allow_duplicate=True),
        Input("add-books-btn", "n_clicks"),
        Input("remove-books-btn", "n_clicks"),
        State({"type": "book-check", "book": ALL}, "value"),
        State("book-statuses-store", "data"),
        prevent_initial_call=True,
    )
    def trigger_pipeline(add_clicks, remove_clicks, checked_values, status_dict):
        """Submit the ingestion or removal pipeline."""
        ctx = dash.callback_context
        if not ctx.triggered:
            return no_update, no_update, no_update

        trigger_id = ctx.triggered[0]["prop_id"]
        is_add = "add-books-btn" in trigger_id

        book_names = list(BIBLE_BOOKS_ALL.keys())
        selected = []
        for i, checked in enumerate(checked_values):
            if checked and i < len(book_names):
                name = book_names[i]
                info = status_dict.get(name, {})
                status = info.get("status", "available")
                if is_add and status == "available":
                    selected.append(name)
                elif not is_add and status == "active":
                    selected.append(name)

        if not selected:
            return no_update, no_update, no_update

        action = "add" if is_add else "remove"

        if USE_MOCK:
            run_data = {"run_id": -1, "action": action, "books": selected}
            progress = _render_progress_mock(action, selected)
            return run_data, True, progress

        try:
            from backend.pipeline_client import submit_add_books, submit_remove_books
            if is_add:
                run_id = submit_add_books(selected)
            else:
                run_id = submit_remove_books(selected)

            run_data = {"run_id": run_id, "action": action, "books": selected}
            progress = _render_progress_running(action, selected, run_id, "PENDING", 0)
            return run_data, False, progress

        except Exception as e:
            error_msg = dbc.Alert(
                f"Failed to submit pipeline: {e}",
                color="danger", className="mb-0",
            )
            return {"run_id": None, "action": None}, True, error_msg

    @app.callback(
        Output("pipeline-progress", "children"),
        Output("pipeline-poll-interval", "disabled", allow_duplicate=True),
        Output("book-statuses-store", "data", allow_duplicate=True),
        Output("stats-panel", "children", allow_duplicate=True),
        Input("pipeline-poll-interval", "n_intervals"),
        State("pipeline-run-store", "data"),
        prevent_initial_call=True,
    )
    def poll_pipeline(_n, run_data):
        """Poll pipeline job status and update progress."""
        if not run_data or not run_data.get("run_id"):
            return no_update, True, no_update, no_update

        run_id = run_data["run_id"]
        action = run_data.get("action", "add")
        books = run_data.get("books", [])

        try:
            from backend.pipeline_client import get_run_status
            status = get_run_status(run_id)
        except Exception as e:
            return (
                dbc.Alert(f"Error polling pipeline: {e}", color="danger"),
                True,
                no_update,
                no_update,
            )

        is_done = status.status == "TERMINATED"
        is_failed = is_done and status.result != "SUCCESS"

        progress = _render_progress_running(
            action, books, run_id, status.status,
            status.elapsed_seconds, status.result if is_done else None,
        )

        if is_done or is_failed:
            if USE_MOCK:
                from backend.graph_client import get_book_statuses_mock, get_graph_stats_mock
                statuses = get_book_statuses_mock()
                stats = get_graph_stats_mock()
            else:
                from backend.graph_client import get_book_statuses, get_graph_stats
                try:
                    statuses = get_book_statuses()
                    stats = get_graph_stats()
                except Exception:
                    statuses = []
                    stats = None

            status_dict = {}
            for s in statuses:
                status_dict[s.book_name] = {
                    "testament": s.testament,
                    "total_chapters": s.total_chapters,
                    "status": s.status,
                    "entity_count": s.entity_count,
                    "relationship_count": s.relationship_count,
                    "verse_count": s.verse_count,
                }
            for name, meta in BIBLE_BOOKS_ALL.items():
                if name not in status_dict:
                    status_dict[name] = {
                        "testament": meta["testament"],
                        "total_chapters": meta["chapters"],
                        "status": "available",
                        "entity_count": 0,
                        "relationship_count": 0,
                        "verse_count": 0,
                    }

            return progress, True, status_dict, _stats_panel(stats)

        return progress, False, no_update, no_update


def _render_progress_running(action, books, run_id, status, elapsed, result=None):
    action_label = "Adding" if action == "add" else "Removing"
    books_str = ", ".join(books[:5]) + ("..." if len(books) > 5 else "")
    elapsed_str = f"{int(elapsed)}s" if elapsed else "..."

    if result == "SUCCESS":
        return dbc.Alert([
            html.I(className="fas fa-check-circle me-2"),
            html.Strong("Pipeline Complete "),
            html.Span(f"— {action_label.lower().rstrip('ing')}ed {len(books)} book(s) in {elapsed_str}"),
        ], color="success", dismissable=True)

    if result and result != "SUCCESS":
        return dbc.Alert([
            html.I(className="fas fa-exclamation-triangle me-2"),
            html.Strong(f"Pipeline {result} "),
            html.Span(f"— Run ID: {run_id}"),
        ], color="danger", dismissable=True)

    return dbc.Alert([
        html.Span(dbc.Spinner(size="sm", color="warning"), className="me-2"),
        html.Strong(f"{action_label}: "),
        html.Span(f"{books_str}"),
        html.Span(f" — {status} ({elapsed_str})", className="text-muted ms-2"),
        html.Br(),
        html.Small(f"Run ID: {run_id}", className="text-muted"),
    ], color="warning", className="mb-0")


def _render_progress_mock(action, books):
    action_label = "Adding" if action == "add" else "Removing"
    books_str = ", ".join(books[:5]) + ("..." if len(books) > 5 else "")
    return dbc.Alert([
        html.I(className="fas fa-info-circle me-2"),
        html.Strong("Mock mode: "),
        html.Span(
            f"In production, this would trigger the pipeline to "
            f"{'ingest' if action == 'add' else 'remove'} {len(books)} book(s): {books_str}. "
            f"Each book goes through: download → entity extraction → relationship extraction → "
            f"graph analytics rebuild."
        ),
    ], color="info", dismissable=True)
