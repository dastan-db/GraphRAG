"""Persist demo chat sessions to Lakebase (PostgreSQL) or fall back to in-memory."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_USE_LAKEBASE = bool(os.getenv("LAKEBASE_INSTANCE_NAME"))


@dataclass
class ChatMessage:
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class ChatSession:
    id: str = ""
    created_at: str = ""
    messages: list[ChatMessage] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# In-memory fallback (used when Lakebase is not configured)
# ---------------------------------------------------------------------------

_sessions: dict[str, ChatSession] = {}


def create_session() -> ChatSession:
    session = ChatSession()
    _sessions[session.id] = session
    return session


def get_session(session_id: str) -> ChatSession | None:
    return _sessions.get(session_id)


def add_message(session_id: str, role: str, content: str) -> None:
    session = _sessions.get(session_id)
    if session is None:
        return
    session.messages.append(ChatMessage(role=role, content=content))


def list_sessions() -> list[ChatSession]:
    return sorted(_sessions.values(), key=lambda s: s.created_at, reverse=True)


# ---------------------------------------------------------------------------
# Lakebase backend (activated when LAKEBASE_INSTANCE_NAME is set)
# ---------------------------------------------------------------------------

_pg_conn: Any | None = None


def _get_pg_connection():
    global _pg_conn
    if _pg_conn is not None:
        return _pg_conn

    try:
        import psycopg
        from databricks.sdk import WorkspaceClient

        instance_name = os.environ["LAKEBASE_INSTANCE_NAME"]
        db_name = os.getenv("LAKEBASE_DATABASE_NAME", "graphrag_demo")

        w = WorkspaceClient()
        instance = w.database.get_database_instance(name=instance_name)
        cred = w.database.generate_database_credential(
            request_id=str(uuid.uuid4()),
            instance_names=[instance_name],
        )
        username = w.current_user.me().user_name

        conn_str = (
            f"host={instance.read_write_dns} "
            f"dbname={db_name} "
            f"user={username} "
            f"password={cred.token} "
            f"sslmode=require"
        )
        _pg_conn = psycopg.connect(conn_str)
        _ensure_schema(_pg_conn)
        return _pg_conn
    except Exception:
        return None


def _ensure_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                messages JSONB NOT NULL DEFAULT '[]'::jsonb
            )
        """)
    conn.commit()


def create_session_pg() -> ChatSession:
    conn = _get_pg_connection()
    session = ChatSession()
    if conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_sessions (id, created_at, messages) VALUES (%s, %s, %s)",
                (session.id, session.created_at, json.dumps([])),
            )
        conn.commit()
    _sessions[session.id] = session
    return session


def add_message_pg(session_id: str, role: str, content: str) -> None:
    add_message(session_id, role, content)
    conn = _get_pg_connection()
    if conn:
        session = _sessions.get(session_id)
        if session:
            msgs = [{"role": m.role, "content": m.content, "timestamp": m.timestamp} for m in session.messages]
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE chat_sessions SET messages = %s WHERE id = %s",
                    (json.dumps(msgs), session_id),
                )
            conn.commit()
