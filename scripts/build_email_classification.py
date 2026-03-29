#!/usr/bin/env python3
"""Rebuild `email_classification` in local DuckDB using the same heuristics as 07i.

Requires exported `emails` table (VARCHAR columns). Arrays may be JSON strings.

Usage:
    python scripts/build_email_classification.py
    python scripts/build_email_classification.py path/to/graphrag_enron.duckdb
"""
from __future__ import annotations

import json
import os
import re
import sys


def _parse_str_list(raw) -> list[str]:
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return []
    s = str(raw).strip()
    if s.startswith("["):
        try:
            v = json.loads(s)
        except json.JSONDecodeError:
            return []
        if isinstance(v, list):
            return [str(x) for x in v if x is not None]
    return []


def reply_depth(subject: str) -> int:
    if not subject:
        return 0
    s = subject.strip()
    n = 0
    while True:
        m = re.match(r"(?i)re:\s*", s)
        if not m:
            break
        n += 1
        s = s[m.end() :].lstrip()
    return n


def all_enron_internal(sender, to_r, cc_r, bcc_r) -> bool:
    parts = []
    if sender:
        parts.append(str(sender).strip())
    for arr in (to_r, cc_r, bcc_r):
        for x in _parse_str_list(arr):
            if x:
                parts.append(str(x).strip())
    if not parts:
        return False
    return all("@enron.com" in p.lower() for p in parts)


def has_attachments(x_from, body) -> bool:
    xf = (x_from or "").lower()
    bd = (body or "").lower()
    return "attachment" in xf or "attachment" in bd


def email_type(sender: str, subject: str, body: str) -> str:
    subj = subject or ""
    subjl = subj.lower()
    snd = (sender or "").lower()
    bod = body or ""

    if "undeliverable" in subjl or "failure notice" in subjl:
        return "bounce"
    if "BEGIN:VCALENDAR" in bod:
        return "calendar"
    if (
        "postmaster" in snd
        or "mailer-daemon" in snd
        or "delivery status" in subjl
        or "out of office" in subjl
    ):
        return "automated"

    t = subj.lstrip()
    tl = t.lower()
    if tl.startswith("fwd:") or tl.startswith("fw:"):
        return "forward"
    if tl.startswith("re:"):
        return "reply"
    return "original"


def main() -> None:
    import duckdb

    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "GRAPHRAG_LOCAL_DB", "data/graphrag_enron.duckdb"
    )
    if not os.path.isfile(db_path):
        print(f"ERROR: DuckDB file not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(db_path)
    if "emails" not in [r[0] for r in con.execute("SHOW TABLES").fetchall()]:
        print(f"ERROR: `emails` missing in {db_path}", file=sys.stderr)
        sys.exit(1)

    rows = con.execute(
        """
        SELECT message_id, sender, subject, body, to_recipients, cc_recipients,
               bcc_recipients, x_from
        FROM emails
        """
    ).fetchall()

    out = []
    for (
        message_id,
        sender,
        subject,
        body,
        to_r,
        cc_r,
        bcc_r,
        x_from,
    ) in rows:
        et = email_type(sender or "", subject or "", body or "")
        to_l = _parse_str_list(to_r)
        cc_l = _parse_str_list(cc_r)
        bc_l = _parse_str_list(bcc_r)
        internal = all_enron_internal(sender, to_l, cc_l, bc_l)
        out.append(
            (
                message_id,
                et,
                reply_depth(subject or ""),
                has_attachments(x_from, body),
                internal,
                et in ("calendar", "automated", "bounce"),
            )
        )

    con.execute("DROP TABLE IF EXISTS email_classification")
    con.execute(
        """
        CREATE TABLE email_classification (
          message_id VARCHAR,
          email_type VARCHAR,
          reply_depth INTEGER,
          has_attachments BOOLEAN,
          is_internal BOOLEAN,
          is_automated BOOLEAN
        )
        """
    )
    if out:
        con.executemany(
            """
            INSERT INTO email_classification VALUES
            (?, ?, ?, ?, ?, ?)
            """,
            out,
        )

    n = con.execute("SELECT COUNT(*) FROM email_classification").fetchone()[0]
    print(f"email_classification: {n:,} rows in {db_path}")
    con.close()


if __name__ == "__main__":
    main()
