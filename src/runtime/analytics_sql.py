from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EnronAnalyticsObjects:
    communication_dyads_relation: str
    person_activity_relation: str
    participants_relation: str
    threads_relation: str
    emails_relation: str
    communication_metric_view: str


def get_enron_analytics_objects(catalog: str, schema: str) -> EnronAnalyticsObjects:
    base = f"{catalog}.{schema}"
    return EnronAnalyticsObjects(
        communication_dyads_relation=os.environ.get(
            "GRAPHRAG_ENRON_COMMUNICATION_DYADS_RELATION",
            f"{base}.communication_dyads",
        ),
        person_activity_relation=os.environ.get(
            "GRAPHRAG_ENRON_PERSON_ACTIVITY_RELATION",
            f"{base}.person_activity",
        ),
        participants_relation=os.environ.get(
            "GRAPHRAG_ENRON_PARTICIPANTS_RELATION",
            f"{base}.participants",
        ),
        threads_relation=os.environ.get(
            "GRAPHRAG_ENRON_THREADS_RELATION",
            f"{base}.threads",
        ),
        emails_relation=os.environ.get(
            "GRAPHRAG_ENRON_EMAILS_RELATION",
            f"{base}.emails",
        ),
        communication_metric_view=os.environ.get(
            "GRAPHRAG_ENRON_COMMUNICATION_METRIC_VIEW",
            f"{base}.communication_metrics",
        ),
    )


def render_enron_analytics_materialization_sql(catalog: str, schema: str) -> dict[str, str]:
    base = f"{catalog}.{schema}"
    objects = EnronAnalyticsObjects(
        communication_dyads_relation=os.environ.get(
            "GRAPHRAG_ENRON_COMMUNICATION_DYADS_RELATION",
            f"{base}.mv_communication_dyads",
        ),
        person_activity_relation=os.environ.get(
            "GRAPHRAG_ENRON_PERSON_ACTIVITY_RELATION",
            f"{base}.mv_person_activity",
        ),
        participants_relation=os.environ.get(
            "GRAPHRAG_ENRON_PARTICIPANTS_RELATION",
            f"{base}.participants",
        ),
        threads_relation=os.environ.get(
            "GRAPHRAG_ENRON_THREADS_RELATION",
            f"{base}.threads",
        ),
        emails_relation=os.environ.get(
            "GRAPHRAG_ENRON_EMAILS_RELATION",
            f"{base}.emails",
        ),
        communication_metric_view=os.environ.get(
            "GRAPHRAG_ENRON_COMMUNICATION_METRIC_VIEW",
            f"{base}.communication_metrics",
        ),
    )
    return {
        "mv_communication_dyads": f"""
CREATE OR REPLACE MATERIALIZED VIEW {objects.communication_dyads_relation}
  SCHEDULE EVERY 1 HOUR
AS
SELECT
  person_a,
  person_b,
  SUM(total_count) AS total_count
FROM {base}.communication_dyads
GROUP BY ALL
""".strip(),
        "mv_person_activity": f"""
CREATE OR REPLACE MATERIALIZED VIEW {objects.person_activity_relation}
  SCHEDULE EVERY 1 HOUR
AS
SELECT
  person_id,
  period,
  SUM(emails_sent) AS emails_sent,
  SUM(emails_received) AS emails_received
FROM {base}.person_activity
GROUP BY ALL
""".strip(),
        "communication_metrics": f"""
CREATE OR REPLACE VIEW {objects.communication_metric_view}
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  comment: "Governed Enron communication metrics for Genie and SQL analytics"
  source: {objects.communication_dyads_relation}
  dimensions:
    - name: Sender
      expr: person_a
    - name: Recipient
      expr: person_b
  measures:
    - name: Email Count
      expr: SUM(total_count)
    - name: Dyad Count
      expr: COUNT(1)
  materialization:
    schedule: every 1 hour
    mode: relaxed
$$
""".strip(),
    }
