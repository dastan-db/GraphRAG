# Databricks notebook source
# MAGIC %md
# MAGIC ### Evaluation Dataset & Scorers
# MAGIC Ground-truth Q&A pairs and custom MLflow scorers for GraphRAG evaluation.

# COMMAND ----------

# DBTITLE 1,Ground-Truth Evaluation Dataset
EVAL_DATASET = [
    {
        "inputs": {"question": "How is Ruth connected to Jesus? Trace the lineage step by step."},
        "expectations": {
            "expected_facts": [
                "Ruth married Boaz",
                "Boaz and Ruth had a son named Obed",
                "Obed was the father of Jesse",
                "Jesse was the father of David",
                "Jesus descended from the line of David",
            ],
        },
    },
    {
        "inputs": {"question": "Which people appear in both Genesis and Exodus?"},
        "expectations": {
            "expected_facts": [
                "Joseph appears in both Genesis and Exodus",
                "God or the Lord appears in both books",
                "Jacob's sons bridge Genesis and Exodus",
            ],
        },
    },
    {
        "inputs": {"question": "Trace the journey of the Israelites from Egypt to the Promised Land. What were the key events and who led them?"},
        "expectations": {
            "expected_facts": [
                "The Israelites were enslaved in Egypt",
                "God sent plagues upon Egypt",
                "Moses led the Israelites out of Egypt",
                "The Red Sea was parted",
                "God gave the Law at Mount Sinai",
            ],
        },
    },
    {
        "inputs": {"question": "What role does Moses play across the Old and New Testament books in our knowledge graph?"},
        "expectations": {
            "expected_facts": [
                "Moses led the Israelites out of Egypt in Exodus",
                "Moses received the Law from God at Sinai",
                "Moses is referenced in Matthew",
                "Moses is referenced in Acts",
            ],
        },
    },
    {
        "inputs": {"question": "Compare the leadership styles of Moses and Paul based on their actions and relationships."},
        "expectations": {
            "expected_facts": [
                "Moses led the Israelites from slavery to freedom",
                "Paul spread Christianity through missionary journeys",
                "Moses received divine commandments directly from God",
                "Paul established churches across multiple cities",
            ],
        },
    },
    {
        "inputs": {"question": "What significant events happened in Egypt across all the books in our knowledge graph?"},
        "expectations": {
            "expected_facts": [
                "Joseph rose to power in Egypt in Genesis",
                "The Israelites were enslaved in Egypt in Exodus",
                "The plagues struck Egypt",
                "The Exodus from Egypt occurred under Moses",
                "Jesus's family fled to Egypt in Matthew",
            ],
        },
    },
    {
        "inputs": {"question": "Who was Abraham and what covenant did God make with him?"},
        "expectations": {
            "expected_facts": [
                "Abraham is a patriarch of Israel",
                "God promised Abraham many descendants",
                "God promised Abraham the land of Canaan",
                "Abraham was called to leave his homeland",
            ],
        },
    },
    {
        "inputs": {"question": "How did Joseph end up in Egypt?"},
        "expectations": {
            "expected_facts": [
                "Joseph was sold by his brothers",
                "Joseph was taken to Egypt",
                "Joseph served in Potiphar's house",
                "Joseph interpreted Pharaoh's dreams",
                "Joseph rose to a position of power in Egypt",
            ],
        },
    },
    {
        "inputs": {"question": "What is the connection between Mount Sinai and the Ten Commandments?"},
        "expectations": {
            "expected_facts": [
                "God gave the Ten Commandments to Moses on Mount Sinai",
                "Moses went up the mountain to receive the Law",
                "The covenant between God and Israel was established at Sinai",
            ],
        },
    },
    {
        "inputs": {"question": "Who were the key figures in the early church described in Acts?"},
        "expectations": {
            "expected_facts": [
                "Peter was a leader of the early church",
                "Paul conducted missionary journeys",
                "Stephen was martyred",
                "The apostles preached in Jerusalem",
            ],
        },
    },
    {
        "inputs": {"question": "What miracles did God perform during the Exodus from Egypt?"},
        "expectations": {
            "expected_facts": [
                "God sent plagues upon Egypt",
                "The Nile was turned to blood",
                "The Red Sea was parted for the Israelites",
                "God provided manna in the wilderness",
            ],
        },
    },
    {
        "inputs": {"question": "How is David connected to both Ruth and Jesus?"},
        "expectations": {
            "expected_facts": [
                "David is a descendant of Ruth through Obed and Jesse",
                "Jesus is a descendant of David",
                "Ruth is in the genealogical line connecting to Jesus through David",
            ],
        },
    },
    {
        "inputs": {"question": "What role does Jerusalem play across the biblical books in our knowledge graph?"},
        "expectations": {
            "expected_facts": [
                "Jerusalem is associated with David",
                "Jesus taught and was crucified near Jerusalem in Matthew",
                "The early church was established in Jerusalem in Acts",
            ],
        },
    },
    {
        "inputs": {"question": "What happened on the road to Damascus in Acts?"},
        "expectations": {
            "expected_facts": [
                "Saul was traveling to Damascus to persecute Christians",
                "Saul encountered a vision of Jesus or a divine light",
                "Saul was blinded",
                "Saul converted and became known as Paul",
            ],
        },
    },
    {
        "inputs": {"question": "How did Peter's role change from Matthew to Acts?"},
        "expectations": {
            "expected_facts": [
                "Peter was a fisherman called as a disciple in Matthew",
                "Peter became a leader of the early church in Acts",
                "Peter preached at Pentecost in Acts",
            ],
        },
    },
    {
        "inputs": {"question": "What covenants are described in Genesis and Exodus?"},
        "expectations": {
            "expected_facts": [
                "God made a covenant with Abraham in Genesis promising land and descendants",
                "God made a covenant with Israel at Sinai in Exodus through Moses",
                "The Abrahamic covenant included circumcision as a sign",
                "The Mosaic covenant involved the Law and commandments",
            ],
        },
    },
    {
        "inputs": {"question": "Who was Pharaoh and what was his role in the Exodus?"},
        "expectations": {
            "expected_facts": [
                "Pharaoh was the ruler of Egypt",
                "Pharaoh enslaved the Israelites",
                "Pharaoh refused to let the Israelites go",
                "God sent plagues to compel Pharaoh",
            ],
        },
    },
    {
        "inputs": {"question": "What is the significance of the Passover in Exodus?"},
        "expectations": {
            "expected_facts": [
                "The Passover commemorates God passing over Israelite homes",
                "The tenth plague killed the firstborn of Egypt",
                "Israelites marked their doorposts with lamb's blood",
                "Passover preceded the Exodus from Egypt",
            ],
        },
    },
    {
        "inputs": {"question": "What was Paul's missionary strategy in Acts?"},
        "expectations": {
            "expected_facts": [
                "Paul traveled to multiple cities across the Mediterranean",
                "Paul preached in synagogues first then to Gentiles",
                "Paul established churches in different regions",
                "Paul faced persecution and imprisonment",
            ],
        },
    },
    {
        "inputs": {"question": "How does the book of Matthew connect the Old Testament to Jesus?"},
        "expectations": {
            "expected_facts": [
                "Matthew's genealogy traces Jesus's lineage to Abraham and David",
                "Matthew frequently quotes Old Testament prophecies",
                "Matthew presents Jesus as the fulfillment of OT promises",
            ],
        },
    },
]

# COMMAND ----------

# DBTITLE 1,Custom Scorers — Governance
from mlflow.genai.scorers import scorer, Guidelines, Correctness, RelevanceToQuery
from mlflow.entities import Feedback

@scorer
def verse_citation(outputs):
    """Checks whether the response cites specific Bible verses in Book Chapter:Verse format."""
    import re
    response = outputs.get("response", "") if isinstance(outputs, dict) else str(outputs)
    pattern = r'(Genesis|Exodus|Ruth|Matthew|Acts)\s+\d+:\d+'
    citations = re.findall(pattern, response)
    return Feedback(
        name="verse_citation",
        value=len(citations) > 0,
        rationale=f"Found {len(citations)} verse citations" if citations else "No verse citations found",
    )

@scorer
def citation_completeness(outputs):
    """Measures the ratio of factual sentences that include a verse citation.

    Splits the answer portion into sentences, counts how many contain a
    Book Chapter:Verse reference, and returns the ratio as a 0-1 score.
    """
    import re
    response = outputs.get("response", "") if isinstance(outputs, dict) else str(outputs)

    answer_section = response.split("### Provenance")[0] if "### Provenance" in response else response
    sentences = [s.strip() for s in re.split(r'[.!?\n]', answer_section) if len(s.strip()) > 20]
    if not sentences:
        return Feedback(name="citation_completeness", value=1.0, rationale="No substantive sentences found")

    cite_pattern = r'(Genesis|Exodus|Ruth|Matthew|Acts)\s+\d+:\d+'
    cited = sum(1 for s in sentences if re.search(cite_pattern, s))
    ratio = cited / len(sentences)

    return Feedback(
        name="citation_completeness",
        value=round(ratio, 3),
        rationale=f"{cited}/{len(sentences)} substantive sentences include verse citations",
    )

@scorer
def provenance_chain(outputs):
    """Checks that the response includes a structured Provenance section with an entity path.

    Looks for the '### Provenance' heading and path indicators (arrows or
    relationship labels in brackets).
    """
    import re
    response = outputs.get("response", "") if isinstance(outputs, dict) else str(outputs)

    has_provenance_heading = bool(re.search(r'(?i)#{1,3}\s*Provenance', response))
    has_path = bool(re.search(r'(→|-->|—\[)', response))
    has_sources_line = bool(re.search(r'(?i)\*?\*?Sources\*?\*?\s*:', response))
    has_grounding_line = bool(re.search(r'(?i)\*?\*?Grounding\*?\*?\s*:', response))

    score_parts = [has_provenance_heading, has_path, has_sources_line, has_grounding_line]
    score = sum(score_parts) / len(score_parts)

    missing = []
    if not has_provenance_heading: missing.append("Provenance heading")
    if not has_path: missing.append("entity path with arrows")
    if not has_sources_line: missing.append("Sources line")
    if not has_grounding_line: missing.append("Grounding indicator")

    rationale = f"Score {score:.0%} — " + (f"missing: {', '.join(missing)}" if missing else "all provenance components present")

    return Feedback(name="provenance_chain", value=round(score, 3), rationale=rationale)

# COMMAND ----------

# DBTITLE 1,Scorer Lists
GOVERNANCE_SCORERS = [
    Guidelines(
        name="hallucination_check",
        guidelines=(
            "The response must NOT contain factual claims about biblical events, people, "
            "or relationships that are not supported by the five books in the knowledge graph "
            "(Genesis, Exodus, Ruth, Matthew, Acts). Every factual assertion must be traceable "
            "to a specific verse or graph relationship. Flag any invented relationships, "
            "fabricated events, or claims about books not in the corpus. "
            "A response that explicitly states 'this information is not in the knowledge graph' "
            "when it lacks data is GOOD. A response that invents an answer is BAD."
        ),
    ),
    citation_completeness,
    provenance_chain,
]

QUALITY_SCORERS = [
    Correctness(),
    RelevanceToQuery(),
    Guidelines(
        name="grounded_reasoning",
        guidelines=(
            "The response must be grounded in specific biblical entities, events, or "
            "relationships rather than providing generic or vague information. It should "
            "reference concrete names, places, and narrative details."
        ),
    ),
    Guidelines(
        name="multi_hop_reasoning",
        guidelines=(
            "For questions that ask about connections, lineages, or cross-book relationships, "
            "the response must show explicit step-by-step reasoning through intermediate "
            "entities or events, not just state the final conclusion."
        ),
    ),
    verse_citation,
]

EVAL_SCORERS = GOVERNANCE_SCORERS + QUALITY_SCORERS

# COMMAND ----------

# DBTITLE 1,Reproducibility Utilities
import re as _re

REPRO_QUESTIONS = [
    "How is Ruth connected to Jesus? Trace the lineage step by step.",
    "What role does Moses play across the Old and New Testament books in our knowledge graph?",
    "How is David connected to both Ruth and Jesus?",
    "What happened on the road to Damascus in Acts?",
    "What is the connection between Mount Sinai and the Ten Commandments?",
]


def extract_citations(text):
    """Extract sorted set of verse citations from a response."""
    pattern = r'(Genesis|Exodus|Ruth|Matthew|Acts)\s+\d+:\d+'
    return sorted(set(_re.findall(pattern, text)))


def extract_path_entities(text):
    """Extract entity names from the provenance Path line."""
    path_match = _re.search(r'(?i)\*?\*?Path\*?\*?\s*:(.+?)(?:\n|$)', text)
    if not path_match:
        return []
    path_line = path_match.group(1)
    entities = _re.split(r'\s*[→\->]+\s*', path_line)
    return [_re.sub(r'\s*\(.*?\)\s*', '', e).strip() for e in entities if e.strip()]


def run_reproducibility_test(predict_fn, questions=None, num_runs=3):
    """Run each question multiple times and measure citation/path consistency.

    Returns a list of dicts with per-question results and an overall rate.
    """
    questions = questions or REPRO_QUESTIONS

    repro_results = {}
    for q in questions:
        repro_results[q] = [predict_fn(q)["response"] for _ in range(num_runs)]

    rows = []
    for q in questions:
        responses = repro_results[q]
        citation_sets = [extract_citations(r) for r in responses]
        path_sets = [extract_path_entities(r) for r in responses]

        all_citations_match = all(c == citation_sets[0] for c in citation_sets)
        all_paths_match = all(p == path_sets[0] for p in path_sets)

        rows.append({
            "Question": q[:70] + "...",
            "Citations Consistent": all_citations_match,
            "Path Consistent": all_paths_match,
            "Runs": num_runs,
        })

    rate = sum(1 for r in rows if r["Citations Consistent"] and r["Path Consistent"]) / len(rows)
    return rows, rate
