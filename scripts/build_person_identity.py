#!/usr/bin/env python3
"""Build local `person_identity` table in DuckDB (mirrors notebooks/07e).

Reads Enron tables from data/graphrag_enron.duckdb and writes/replaces person_identity.

Usage:
    python scripts/build_person_identity.py
    python scripts/build_person_identity.py --db path/to/graphrag_enron.duckdb
"""
from __future__ import annotations

import argparse
import re
import sys


def slugify(name: str | None) -> str | None:
    if name is None:
        return None
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


CUSTODIAN_FIXUPS = {
    "kenneth_lay": ["ken_lay", "dr_ken_lay", "dr_kenneth_lay", "kenny_lay",
                    "kenneth_l_lay", "k_lay", "kenneth_lay_enron_com"],
    "jeff_skilling": ["jeffrey_skilling", "jeffrey_k_skilling", "j_skilling",
                      "jeff_skilling_enron_com", "jeffrey_skilling_enron_com"],
    "andrew_fastow": ["andy_fastow", "andrew_s_fastow", "a_fastow",
                      "andrew_fastow_enron_com"],
    "david_delainey": ["dave_delainey", "d_delainey", "david_w_delainey"],
    "jeff_dasovich": ["jeffrey_dasovich", "j_dasovich"],
    "vince_kaminski": ["vincent_kaminski", "wincenty_kaminski", "v_kaminski",
                       "vince_j_kaminski"],
    "louise_kitchen": ["l_kitchen"],
    "sara_shackleton": ["s_shackleton", "sara_shackleton_enron_com"],
    "chris_germany": ["christopher_germany", "c_germany"],
    "eric_bass": ["e_bass"],
    "phillip_allen": ["phil_allen", "p_allen", "phillip_k_allen"],
    "john_arnold": ["j_arnold"],
    "sally_beck": ["s_beck", "sally_beck_enron_com"],
    "lynn_blair": ["l_blair"],
    "larry_campbell": ["l_campbell", "lawrence_campbell"],
    "sherron_watkins": ["s_watkins", "sherron_watkins_enron_com"],
    "richard_causey": ["rick_causey", "r_causey"],
    "rick_buy": ["r_buy", "richard_buy"],
    "tim_belden": ["t_belden", "timothy_belden"],
    "michael_kopper": ["m_kopper", "mike_kopper"],
    "greg_whalley": ["g_whalley", "gregory_whalley"],
    "cliff_baxter": ["c_baxter", "j_clifford_baxter"],
    "kenneth_rice": ["ken_rice", "k_rice"],
    "mark_frevert": ["m_frevert"],
    "rebecca_mark": ["r_mark", "rebecca_mark_jusbasche"],
    "enron_corp": ["enron", "enron_corporation", "enron_inc", "enron_company"],
    "enron_energy_services": ["ees"],
    "enron_broadband_services": ["ebs", "enron_broadband"],
    "federal_energy_regulatory_commission": ["ferc"],
    "california_public_utilities_commission": ["cpuc"],
    "pacific_gas_and_electric": ["pg_e", "pge", "pacific_gas_electric"],
}


def _custodian_pair_rows():
    rows = []
    for canonical, variants in CUSTODIAN_FIXUPS.items():
        for v in variants:
            if v != canonical:
                rows.append((v, canonical))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Build person_identity in local DuckDB")
    parser.add_argument("--db", default="data/graphrag_enron.duckdb", help="DuckDB file path")
    args = parser.parse_args()

    try:
        import duckdb
    except ImportError:
        print("ERROR: pip install duckdb", file=sys.stderr)
        return 1

    con = duckdb.connect(args.db)

    required = ["entities", "entity_aliases", "participants"]
    for t in required:
        r = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [t],
        ).fetchone()
        if not r or r[0] == 0:
            print(f"ERROR: table '{t}' missing in {args.db}", file=sys.stderr)
            return 1

    con.execute("DROP TABLE IF EXISTS custodian_pairs")
    con.execute("CREATE TABLE custodian_pairs (alias_id VARCHAR, canonical_id VARCHAR)")
    con.executemany("INSERT INTO custodian_pairs VALUES (?, ?)", _custodian_pair_rows())

    con.create_function("slugify", slugify, [str], str, null_handling="special")

    con.execute(
        """
        CREATE OR REPLACE TABLE person_identity AS
        WITH
        entities_person AS (
          SELECT entity_id, name AS canonical_name
          FROM entities
          WHERE entity_type = 'Person'
        ),
        alias_person AS (
          SELECT DISTINCT ea.alias_id, ea.canonical_id
          FROM entity_aliases ea
          INNER JOIN entities_person ep ON ea.canonical_id = ep.entity_id
        ),
        alias_tagged AS (
          SELECT
            ap.alias_id,
            ap.canonical_id,
            CASE
              WHEN EXISTS (
                SELECT 1 FROM custodian_pairs c
                WHERE c.alias_id = ap.alias_id AND c.canonical_id = ap.canonical_id
              )
              THEN 'custodian'
              ELSE 'ai'
            END AS alias_source
          FROM alias_person ap
        ),
        alias_priority AS (
          SELECT
            canonical_id,
            MAX(CASE WHEN alias_source = 'custodian' THEN 1 ELSE 0 END) AS has_custodian,
            MAX(CASE WHEN alias_source = 'ai' THEN 1 ELSE 0 END) AS has_ai
          FROM alias_tagged
          GROUP BY canonical_id
        ),
        aliases_agg AS (
          SELECT canonical_id, LIST(DISTINCT alias_id ORDER BY alias_id) AS aliases
          FROM alias_tagged
          GROUP BY canonical_id
        ),
        slug_map AS (
          SELECT entity_id AS slug, entity_id FROM entities_person
          UNION
          SELECT alias_id AS slug, canonical_id AS entity_id FROM alias_person
        ),
        parts_slugs AS (
          SELECT email_address, slugify(TRIM(name_normalized)) AS slug FROM participants
          UNION
          SELECT email_address, slugify(regexp_extract(email_address, '^([^@]+)', 1)) AS slug
          FROM participants
        ),
        emails_by_person AS (
          SELECT sm.entity_id, LIST(DISTINCT ps.email_address ORDER BY ps.email_address) AS email_addresses
          FROM parts_slugs ps
          INNER JOIN slug_map sm ON ps.slug = sm.slug
          WHERE ps.slug IS NOT NULL AND ps.slug <> ''
          GROUP BY sm.entity_id
        ),
        joined AS (
          SELECT
            p.entity_id,
            p.canonical_name,
            COALESCE(e.email_addresses, CAST([] AS VARCHAR[])) AS email_addresses,
            COALESCE(a.aliases, CAST([] AS VARCHAR[])) AS aliases,
            CASE
              WHEN COALESCE(pr.has_custodian, 0) = 1 THEN 'custodian'
              WHEN COALESCE(pr.has_ai, 0) = 1 THEN 'ai'
              WHEN len(COALESCE(e.email_addresses, CAST([] AS VARCHAR[]))) > 0 THEN 'email_header'
              ELSE 'ai'
            END AS source
          FROM entities_person p
          LEFT JOIN aliases_agg a ON p.entity_id = a.canonical_id
          LEFT JOIN emails_by_person e ON p.entity_id = e.entity_id
          LEFT JOIN alias_priority pr ON p.entity_id = pr.canonical_id
        )
        SELECT
          entity_id,
          canonical_name,
          email_addresses,
          aliases,
          source,
          CASE source
            WHEN 'custodian' THEN 1.0
            WHEN 'email_header' THEN 0.7
            ELSE 0.85
          END AS confidence
        FROM joined
        """
    )

    con.execute("DROP TABLE custodian_pairs")

    n = con.execute("SELECT COUNT(*) FROM person_identity").fetchone()[0]
    print(f"person_identity: {n:,} rows in {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
