# Databricks notebook source
# MAGIC %md
# MAGIC # 05 — Evaluation (Interactive Debug)
# MAGIC
# MAGIC Self-contained notebook for interactive debugging. Run cell by cell.
# MAGIC Once everything works, update the production notebook in `notebooks/05_Evaluation.py` with the fixes.
# MAGIC
# MAGIC **Key fixes validated here:**
# MAGIC - `COLLECT_LIST` ordering via `ARRAY_SORT(COLLECT_LIST(STRUCT(...)))` pattern
# MAGIC - No `@mlflow.trace` on predict functions (evaluate() auto-traces)
# MAGIC - No `mlflow.start_run()` wrapping `mlflow.genai.evaluate()` (evaluate() manages its own runs)
# MAGIC - Correct `mlflow.search_traces()` API (uses `experiment_ids`, not `run_id`)

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install -U "mlflow[databricks]>=3.1.0" databricks-langchain langgraph>=0.3.4 databricks-agents pydantic numpy --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configuration (inlined — no %run dependencies)
config = {}
config['catalog'] = 'serverless_8e8gyh_catalog'
config['schema'] = 'graphrag_bible'
config['llm_endpoint'] = 'databricks-meta-llama-3-3-70b-instruct'
config['small_llm_endpoint'] = 'databricks-meta-llama-3-1-8b-instruct'
config['embedding_endpoint'] = 'databricks-gte-large-en'
config['external_llm_endpoint'] = 'databricks-gpt-5-2'
config['judge_endpoint'] = 'databricks-claude-sonnet-4-6'

config['verses_table'] = f"{config['catalog']}.{config['schema']}.verses"
config['chapters_table'] = f"{config['catalog']}.{config['schema']}.chapters"
config['entities_table'] = f"{config['catalog']}.{config['schema']}.entities"
config['relationships_table'] = f"{config['catalog']}.{config['schema']}.relationships"
config['entity_mentions_table'] = f"{config['catalog']}.{config['schema']}.entity_mentions"

_ = spark.sql(f"USE CATALOG {config['catalog']}")
_ = spark.sql(f"USE SCHEMA {config['schema']}")

print("Config loaded:", config)

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Section A: Flat RAG — Fixed SQL

# COMMAND ----------

# DBTITLE 1,Build Chapter Chunks (fixed COLLECT_LIST ordering)
import mlflow
import numpy as np

def build_chapter_chunks():
    """Concatenate verses into chapter-level chunks, ordered by verse number."""
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    rows = spark.sql(f"""
        SELECT book, chapter,
               CONCAT(book, ' Chapter ', chapter) as chunk_id,
               CONCAT_WS(' ',
                 TRANSFORM(
                   ARRAY_SORT(COLLECT_LIST(STRUCT(verse_number, text))),
                   x -> x.text
                 )
               ) as text
        FROM {config['verses_table']}
        GROUP BY book, chapter
        ORDER BY book, chapter
    """).collect()

    return [
        {"chunk_id": r["chunk_id"], "book": r["book"], "chapter": r["chapter"], "text": r["text"]}
        for r in rows
    ]

chunks = build_chapter_chunks()
print(f"Built {len(chunks)} chapter chunks")

# COMMAND ----------

# DBTITLE 1,DEBUG: Verify chunk ordering
sample = chunks[0]
print(f"First chunk: {sample['chunk_id']}")
print(f"Text starts with: {sample['text'][:200]}")
print("---")
sample_last = chunks[-1]
print(f"Last chunk: {sample_last['chunk_id']}")
print(f"Text starts with: {sample_last['text'][:200]}")

# COMMAND ----------

# DBTITLE 1,Embed Texts via Foundation Model API
def embed_texts(texts, endpoint=None):
    """Embed a batch of texts using the Databricks embedding endpoint."""
    import mlflow.deployments
    client = mlflow.deployments.get_deploy_client("databricks")
    ep = endpoint or config['embedding_endpoint']

    all_embeddings = []
    batch_size = 16
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        response = client.predict(endpoint=ep, inputs={"input": batch})
        all_embeddings.extend([item["embedding"] for item in response.data])

    return np.array(all_embeddings)

def cosine_similarity_search(query_embedding, corpus_embeddings, top_k=5):
    """Return indices and scores of the top-k most similar corpus items."""
    q_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    c_norms = corpus_embeddings / (np.linalg.norm(corpus_embeddings, axis=1, keepdims=True) + 1e-10)
    similarities = c_norms @ q_norm
    top_indices = np.argsort(similarities)[::-1][:top_k]
    return top_indices, similarities[top_indices]

# COMMAND ----------

# DBTITLE 1,Build Flat RAG Pipeline
FLAT_RAG_SYSTEM_PROMPT = """You are a biblical scholar. Answer the question using ONLY the Bible passages provided below.
If the passages do not contain enough information, say so clearly.
Always cite specific passages (Book Chapter:Verse) when possible.

Retrieved Passages:
{context}"""


class FlatRAGPipeline:
    def __init__(self, llm_endpoint=None, embedding_endpoint=None, top_k=5):
        self.llm_endpoint = llm_endpoint or config['llm_endpoint']
        self.embedding_endpoint = embedding_endpoint or config['embedding_endpoint']
        self.top_k = top_k
        self.chunks = None
        self.embeddings = None

    def build_index(self):
        self.chunks = build_chapter_chunks()
        texts = [c["text"] for c in self.chunks]
        self.embeddings = embed_texts(texts, self.embedding_endpoint)
        print(f"Indexed {len(self.chunks)} chapter chunks (embedding dim={self.embeddings.shape[1]})")

    @mlflow.trace(span_type="RETRIEVER")
    def retrieve(self, question):
        q_emb = embed_texts([question], self.embedding_endpoint)[0]
        indices, scores = cosine_similarity_search(q_emb, self.embeddings, self.top_k)
        return [
            {
                "chunk_id": self.chunks[int(idx)]["chunk_id"],
                "text": self.chunks[int(idx)]["text"][:2000],
                "score": float(score),
            }
            for idx, score in zip(indices, scores)
        ]

    @mlflow.trace
    def query(self, question):
        retrieved = self.retrieve(question)
        context = "\n\n".join(f"[{r['chunk_id']}]\n{r['text']}" for r in retrieved)
        prompt = FLAT_RAG_SYSTEM_PROMPT.format(context=context)

        import mlflow.deployments
        client = mlflow.deployments.get_deploy_client("databricks")
        response = client.predict(
            endpoint=self.llm_endpoint,
            inputs={
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": question},
                ],
                "temperature": 0.1,
                "max_tokens": 2048,
            },
        )
        return response.choices[0]["message"]["content"]


flat_rag = FlatRAGPipeline(
    llm_endpoint=config['llm_endpoint'],
    embedding_endpoint=config['embedding_endpoint'],
    top_k=5,
)
flat_rag.build_index()

# COMMAND ----------

# DBTITLE 1,DEBUG: Test flat RAG with one question
try:
    test_response = flat_rag.query("Who is Moses?")
    print(f"Flat RAG response length: {len(test_response)}")
    print(test_response[:500])
except Exception as e:
    print(f"ERROR in flat_rag.query: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Section B: Scorer Smoke Tests

# COMMAND ----------

# DBTITLE 1,Define Scorers (inlined from evaluation.py)
from mlflow.genai.scorers import scorer, Guidelines, Correctness, RelevanceToQuery
from mlflow.entities import Feedback
import re

@scorer
def verse_citation(outputs):
    """Checks whether the response cites specific Bible verses."""
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
    """Measures the ratio of factual sentences that include a verse citation."""
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
    """Checks that the response includes a structured Provenance section."""
    response = outputs.get("response", "") if isinstance(outputs, dict) else str(outputs)
    has_provenance_heading = bool(re.search(r'(?i)#{1,3}\s*Provenance', response))
    has_path = bool(re.search(r'(→|-->|—\[)', response))
    has_sources_line = bool(re.search(r'(?i)\*?\*?Sources\*?\*?\s*:', response))
    has_grounding_line = bool(re.search(r'(?i)\*?\*?Grounding\*?\*?\s*:', response))
    score_parts = [has_provenance_heading, has_path, has_sources_line, has_grounding_line]
    score_val = sum(score_parts) / len(score_parts)
    missing = []
    if not has_provenance_heading: missing.append("Provenance heading")
    if not has_path: missing.append("entity path with arrows")
    if not has_sources_line: missing.append("Sources line")
    if not has_grounding_line: missing.append("Grounding indicator")
    rationale = f"Score {score_val:.0%} — " + (f"missing: {', '.join(missing)}" if missing else "all provenance components present")
    return Feedback(name="provenance_chain", value=round(score_val, 3), rationale=rationale)

_HALLUCINATION_GUIDELINES = (
    "The response must NOT contain factual claims about biblical events, people, "
    "or relationships that are not supported by the five books in the knowledge graph "
    "(Genesis, Exodus, Ruth, Matthew, Acts). Every factual assertion must be traceable "
    "to a specific verse or graph relationship. Flag any invented relationships, "
    "fabricated events, or claims about books not in the corpus. "
    "A response that explicitly states 'this information is not in the knowledge graph' "
    "when it lacks data is GOOD. A response that invents an answer is BAD."
)

_GROUNDED_REASONING_GUIDELINES = (
    "The response must be grounded in specific biblical entities, events, or "
    "relationships rather than providing generic or vague information. It should "
    "reference concrete names, places, and narrative details."
)

_MULTI_HOP_REASONING_GUIDELINES = (
    "For questions that ask about connections, lineages, or cross-book relationships, "
    "the response must show explicit step-by-step reasoning through intermediate "
    "entities or events, not just state the final conclusion."
)

def build_scorers(judge_model=None):
    """Build scorer lists, optionally using a custom judge endpoint."""
    judge_kwargs = {"model": judge_model} if judge_model else {}

    governance = [
        Guidelines(name="hallucination_check", guidelines=_HALLUCINATION_GUIDELINES, **judge_kwargs),
        citation_completeness,
        provenance_chain,
    ]
    quality = [
        Correctness(**judge_kwargs),
        RelevanceToQuery(**judge_kwargs),
        Guidelines(name="grounded_reasoning", guidelines=_GROUNDED_REASONING_GUIDELINES, **judge_kwargs),
        Guidelines(name="multi_hop_reasoning", guidelines=_MULTI_HOP_REASONING_GUIDELINES, **judge_kwargs),
        verse_citation,
    ]
    return governance + quality

GOVERNANCE_SCORERS = [
    Guidelines(name="hallucination_check", guidelines=_HALLUCINATION_GUIDELINES),
    citation_completeness,
    provenance_chain,
]

QUALITY_SCORERS = [
    Correctness(),
    RelevanceToQuery(),
    Guidelines(name="grounded_reasoning", guidelines=_GROUNDED_REASONING_GUIDELINES),
    Guidelines(name="multi_hop_reasoning", guidelines=_MULTI_HOP_REASONING_GUIDELINES),
    verse_citation,
]

EVAL_SCORERS = GOVERNANCE_SCORERS + QUALITY_SCORERS
print(f"Loaded {len(EVAL_SCORERS)} scorers (default judge)")

judge_model = f"databricks:/{config['judge_endpoint']}"
EVAL_SCORERS_WITH_JUDGE = build_scorers(judge_model=judge_model)
print(f"Loaded {len(EVAL_SCORERS_WITH_JUDGE)} scorers (judge: {judge_model})")

# COMMAND ----------

# DBTITLE 1,DEBUG: Test each scorer against a known-good output
test_output = {
    "response": (
        "### Answer\n"
        "Ruth married Boaz (Ruth 4:13). They had a son named Obed (Ruth 4:17). "
        "Obed was the father of Jesse, and Jesse was the father of David. "
        "Jesus descended from the line of David (Matthew 1:6).\n\n"
        "### Provenance\n"
        "- **Path**: Ruth \u2192 Boaz (MARRIED_TO, Ruth 4:13) \u2192 Obed (FATHER_OF, Ruth 4:17) \u2192 Jesse \u2192 David \u2192 Jesus\n"
        "- **Sources**: Ruth 4:13, Ruth 4:17, Matthew 1:6\n"
        "- **Grounding**: All claims grounded in knowledge graph"
    )
}

for s in [verse_citation, citation_completeness, provenance_chain]:
    try:
        result = s(outputs=test_output)
        print(f"  {s.__name__}: value={result.value}, rationale={result.rationale}")
    except Exception as e:
        print(f"  {s.__name__}: ERROR - {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Section C: Predict Function Smoke Tests

# COMMAND ----------

# DBTITLE 1,Graph Traversal Tools (inlined from src/agent/tools.py)
from langchain_core.tools import tool

@tool
def find_entity(name: str) -> str:
    """Search for a biblical entity by name. Returns matching entities with their type, description, and first mention.
    Use this when the user asks about a specific person, place, event, or concept.

    Args:
        name: The name to search for (e.g., "Moses", "Jerusalem", "covenant")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    results = spark.sql(f"""
        SELECT name, entity_type, description, first_mention_book, first_mention_chapter
        FROM {config['entities_table']}
        WHERE LOWER(name) LIKE LOWER('%{name}%')
        ORDER BY name
        LIMIT 10
    """).collect()

    if not results:
        return f"No entity found matching '{name}'."

    lines = []
    for r in results:
        lines.append(
            f"- **{r['name']}** ({r['entity_type']}): {r['description']} "
            f"[First mentioned: {r['first_mention_book']} ch.{r['first_mention_chapter']}]"
        )
    return "\n".join(lines)

@tool
def find_connections(entity_name: str) -> str:
    """Find all relationships involving a given entity — both as source and target.
    Use this to understand how a person, place, or concept is connected to others in the biblical narrative.

    Args:
        entity_name: The entity name to find connections for (e.g., "Abraham", "Egypt")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    entity_id = "_".join(entity_name.lower().split())

    results = spark.sql(f"""
        SELECT
            COALESCE(e1.name, r.source_entity) as source_name,
            r.relationship_type,
            COALESCE(e2.name, r.target_entity) as target_name,
            r.description,
            r.book,
            r.chapter
        FROM {config['relationships_table']} r
        LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
        WHERE r.source_entity LIKE '%{entity_id}%'
           OR r.target_entity LIKE '%{entity_id}%'
        ORDER BY r.book, r.chapter
        LIMIT 30
    """).collect()

    if not results:
        return f"No connections found for '{entity_name}'."

    lines = [f"Connections for '{entity_name}' ({len(results)} found):"]
    for r in results:
        lines.append(
            f"- {r['source_name']} --[{r['relationship_type']}]--> {r['target_name']}: "
            f"{r['description']} ({r['book']} ch.{r['chapter']})"
        )
    return "\n".join(lines)

@tool
def trace_path(entity_a: str, entity_b: str) -> str:
    """Find how two entities are connected, tracing up to 3 hops through the knowledge graph.
    Use this for multi-hop questions like 'How is Ruth connected to Jesus?'

    Args:
        entity_a: Starting entity name (e.g., "Ruth")
        entity_b: Ending entity name (e.g., "Jesus")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    id_a = "_".join(entity_a.lower().split())
    id_b = "_".join(entity_b.lower().split())

    # 1-hop
    direct = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type as rel,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description, r.book
        FROM {config['relationships_table']} r
        LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
        WHERE (r.source_entity LIKE '%{id_a}%' AND r.target_entity LIKE '%{id_b}%')
           OR (r.source_entity LIKE '%{id_b}%' AND r.target_entity LIKE '%{id_a}%')
    """).collect()

    if direct:
        lines = [f"Direct connection between {entity_a} and {entity_b}:"]
        for r in direct:
            lines.append(f"  {r['src']} --[{r['rel']}]--> {r['tgt']}: {r['description']} ({r['book']})")
        return "\n".join(lines)

    # 2-hop
    two_hop = spark.sql(f"""
        SELECT COALESCE(e1.name, r1.source_entity) as src,
               r1.relationship_type as rel1,
               COALESCE(e_mid.name, r1.target_entity) as mid,
               r2.relationship_type as rel2,
               COALESCE(e2.name, r2.target_entity) as tgt
        FROM {config['relationships_table']} r1
        JOIN {config['relationships_table']} r2 ON r1.target_entity = r2.source_entity
        LEFT JOIN {config['entities_table']} e1 ON r1.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e_mid ON r1.target_entity = e_mid.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r2.target_entity = e2.entity_id
        WHERE r1.source_entity LIKE '%{id_a}%' AND r2.target_entity LIKE '%{id_b}%'
        LIMIT 10
    """).collect()

    if two_hop:
        lines = [f"2-hop path from {entity_a} to {entity_b}:"]
        for r in two_hop:
            lines.append(f"  {r['src']} --[{r['rel1']}]--> {r['mid']} --[{r['rel2']}]--> {r['tgt']}")
        return "\n".join(lines)

    # 3-hop
    three_hop = spark.sql(f"""
        SELECT COALESCE(e1.name, r1.source_entity) as src,
               r1.relationship_type as rel1,
               COALESCE(e_m1.name, r1.target_entity) as mid1,
               r2.relationship_type as rel2,
               COALESCE(e_m2.name, r2.target_entity) as mid2,
               r3.relationship_type as rel3,
               COALESCE(e3.name, r3.target_entity) as tgt
        FROM {config['relationships_table']} r1
        JOIN {config['relationships_table']} r2 ON r1.target_entity = r2.source_entity
        JOIN {config['relationships_table']} r3 ON r2.target_entity = r3.source_entity
        LEFT JOIN {config['entities_table']} e1 ON r1.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e_m1 ON r1.target_entity = e_m1.entity_id
        LEFT JOIN {config['entities_table']} e_m2 ON r2.target_entity = e_m2.entity_id
        LEFT JOIN {config['entities_table']} e3 ON r3.target_entity = e3.entity_id
        WHERE r1.source_entity LIKE '%{id_a}%' AND r3.target_entity LIKE '%{id_b}%'
        LIMIT 10
    """).collect()

    if three_hop:
        lines = [f"3-hop path from {entity_a} to {entity_b}:"]
        for r in three_hop:
            lines.append(f"  {r['src']} --[{r['rel1']}]--> {r['mid1']} --[{r['rel2']}]--> {r['mid2']} --[{r['rel3']}]--> {r['tgt']}")
        return "\n".join(lines)

    return f"No path found between '{entity_a}' and '{entity_b}' within 3 hops. Try using find_connections on each entity separately."

@tool
def get_context_verses(entity_name: str, book: str = "") -> str:
    """Get actual Bible verses that mention a specific entity. Provides source text for grounding answers.

    Args:
        entity_name: The entity name to find verses for (e.g., "Moses")
        book: Optional — filter to a specific book (e.g., "Genesis"). Leave empty for all books.
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    book_filter = f"AND v.book = '{book}'" if book else ""

    results = spark.sql(f"""
        SELECT v.book, v.chapter, v.verse_number, v.text
        FROM {config['verses_table']} v
        WHERE v.text LIKE '%{entity_name}%'
        {book_filter}
        ORDER BY v.book, v.chapter, v.verse_number
        LIMIT 15
    """).collect()

    if not results:
        return f"No verses found mentioning '{entity_name}'" + (f" in {book}" if book else "") + "."

    lines = [f"Verses mentioning '{entity_name}' ({len(results)} found):"]
    for r in results:
        lines.append(f"  {r['book']} {r['chapter']}:{r['verse_number']} — {r['text']}")
    return "\n".join(lines)

@tool
def get_entity_summary(entity_name: str) -> str:
    """Get a comprehensive profile of a biblical entity: type, description, all relationships, and all books it appears in.
    Use this for broad questions about who someone is or what role they play.

    Args:
        entity_name: The entity to summarize (e.g., "Abraham", "Jerusalem")
    """
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    entity_id = "_".join(entity_name.lower().split())

    entity_rows = spark.sql(f"""
        SELECT name, entity_type, description, first_mention_book, first_mention_chapter
        FROM {config['entities_table']}
        WHERE entity_id LIKE '%{entity_id}%'
        LIMIT 1
    """).collect()

    if not entity_rows:
        return f"Entity '{entity_name}' not found in the knowledge graph."

    ent = entity_rows[0]
    lines = [
        f"**{ent['name']}** ({ent['entity_type']})",
        f"Description: {ent['description']}",
        f"First mentioned: {ent['first_mention_book']} ch.{ent['first_mention_chapter']}",
    ]

    books = spark.sql(f"""
        SELECT DISTINCT book FROM {config['relationships_table']}
        WHERE source_entity LIKE '%{entity_id}%' OR target_entity LIKE '%{entity_id}%'
        ORDER BY book
    """).collect()
    if books:
        lines.append(f"Appears in: {', '.join(r['book'] for r in books)}")

    rels = spark.sql(f"""
        SELECT COALESCE(e1.name, r.source_entity) as src,
               r.relationship_type,
               COALESCE(e2.name, r.target_entity) as tgt,
               r.description
        FROM {config['relationships_table']} r
        LEFT JOIN {config['entities_table']} e1 ON r.source_entity = e1.entity_id
        LEFT JOIN {config['entities_table']} e2 ON r.target_entity = e2.entity_id
        WHERE r.source_entity LIKE '%{entity_id}%' OR r.target_entity LIKE '%{entity_id}%'
        LIMIT 20
    """).collect()

    if rels:
        lines.append(f"\nKey relationships ({len(rels)}):")
        for r in rels:
            lines.append(f"  {r['src']} --[{r['relationship_type']}]--> {r['tgt']}: {r['description']}")

    return "\n".join(lines)

GRAPH_TOOLS = [find_entity, find_connections, trace_path, get_context_verses, get_entity_summary]

# COMMAND ----------

# DBTITLE 1,GraphRAG Agent (inlined from src/agent/agent.py)
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    output_to_responses_items_stream,
    to_chat_completions_input,
)
from databricks_langchain import ChatDatabricks
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode
from typing import Annotated, Generator, Sequence, TypedDict

SYSTEM_PROMPT = """You are a biblical scholar with access to a knowledge graph built from five books of the King James Bible: Genesis, Exodus, Ruth, Matthew, and Acts.

You have tools that let you search the knowledge graph for entities, relationships, and source verses. Use them to provide well-grounded, auditable answers.

## Tool Usage
- ALWAYS use tools to look up information before answering. Do not rely on your training data alone.
- When asked about connections between entities, use trace_path first, then find_connections for more context.
- When asked about a person or concept, use get_entity_summary for a comprehensive profile.
- Always cite specific Bible verses (book chapter:verse) when possible using get_context_verses.
- For multi-hop questions, break them into steps: find each entity, then trace connections.

## Response Format
Structure EVERY response with these two sections:

### Answer
Provide a concise, well-grounded answer using bullet points where appropriate. Cite specific verses inline (e.g., Genesis 12:1).

### Provenance
At the end of every response, include a structured provenance section with:
- **Path**: The explicit entity path traversed, using arrows. Example: Ruth → Boaz (MARRIED_TO, Ruth 4:13) → Obed (FATHER_OF, Ruth 4:17) → Jesse → David → Jesus
- **Sources**: List every verse citation used as evidence, comma-separated.
- **Grounding**: State one of:
  - "All claims grounded in knowledge graph" — if every factual claim came from tool results
  - "Partially grounded — the following claims rely on general knowledge: [list them]" — if any claim was not found via tools

## Critical Rules
- If information is not in the knowledge graph, say so explicitly rather than guessing. NEVER invent relationships or events.
- If a tool returns no results, report that honestly. Do not fabricate an alternative answer.
- Every factual claim must cite its source verse or explicitly state it was not found in the graph."""

class AgentState(TypedDict):
    messages: Annotated[Sequence, add_messages]

class GraphRAGAgent(ResponsesAgent):
    def __init__(self, endpoint=None):
        self.llm = ChatDatabricks(endpoint=endpoint or config['llm_endpoint'])
        self.tools = GRAPH_TOOLS
        self.llm_with_tools = self.llm.bind_tools(self.tools)

    def _build_graph(self):
        def should_continue(state):
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.tool_calls:
                return "tools"
            return "end"

        def call_model(state):
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + state["messages"]
            response = self.llm_with_tools.invoke(messages)
            return {"messages": [response]}

        graph = StateGraph(AgentState)
        graph.add_node("agent", RunnableLambda(call_model))
        graph.add_node("tools", ToolNode(self.tools))
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
        graph.add_edge("tools", "agent")
        graph.set_entry_point("agent")
        return graph.compile()

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        outputs = [
            event.item
            for event in self.predict_stream(request)
            if event.type == "response.output_item.done"
        ]
        return ResponsesAgentResponse(output=outputs)

    def predict_stream(
        self, request: ResponsesAgentRequest
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        messages = to_chat_completions_input([m.model_dump() for m in request.input])
        graph = self._build_graph()
        for event in graph.stream({"messages": messages}, stream_mode=["updates"]):
            if event[0] == "updates":
                for node_data in event[1].values():
                    if node_data.get("messages"):
                        yield from output_to_responses_items_stream(node_data["messages"])

mlflow.langchain.autolog()
AGENT = GraphRAGAgent()

# COMMAND ----------

# DBTITLE 1,Test: Direct LLM predict (simplest)
def predict_direct_llm(question):
    import mlflow.deployments
    client = mlflow.deployments.get_deploy_client("databricks")
    response = client.predict(
        endpoint=config['llm_endpoint'],
        inputs={
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a biblical scholar. Answer based on your knowledge of "
                        "Genesis, Exodus, Ruth, Matthew, and Acts from the King James Bible. "
                        "Cite specific verses when possible."
                    ),
                },
                {"role": "user", "content": question},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        },
    )
    return {"response": response.choices[0]["message"]["content"]}

try:
    result = predict_direct_llm("Who is Moses?")
    print(f"Direct LLM OK - response length: {len(result['response'])}")
    print(result['response'][:300])
except Exception as e:
    print(f"ERROR: {e}")

# COMMAND ----------

# DBTITLE 1,Test: Direct External predict (frontier model)
def predict_direct_external(question):
    import mlflow.deployments
    client = mlflow.deployments.get_deploy_client("databricks")
    response = client.predict(
        endpoint=config['external_llm_endpoint'],
        inputs={
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a biblical scholar. Answer based on your knowledge of "
                        "Genesis, Exodus, Ruth, Matthew, and Acts from the King James Bible. "
                        "Cite specific verses when possible."
                    ),
                },
                {"role": "user", "content": question},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        },
    )
    return {"response": response.choices[0]["message"]["content"]}

try:
    result = predict_direct_external("Who is Moses?")
    print(f"Direct External OK - response length: {len(result['response'])}")
    print(result['response'][:300])
except Exception as e:
    print(f"ERROR: {e}")

# COMMAND ----------

# DBTITLE 1,Test: GraphRAG predict (70B)
from mlflow.types.responses import ResponsesAgentRequest

agent_70b = AGENT

def predict_graphrag_70b(question):
    request = ResponsesAgentRequest(input=[{"role": "user", "content": question}])
    result = agent_70b.predict(request)
    texts = [item.text for item in result.output if hasattr(item, "text")]
    return {"response": "\n".join(texts)}

try:
    result = predict_graphrag_70b("Who is Moses?")
    print(f"GraphRAG 70B OK - response length: {len(result['response'])}")
    print(result['response'][:300])
except Exception as e:
    print(f"ERROR: {e}")

# COMMAND ----------

# DBTITLE 1,Test: Flat RAG predict
def predict_flat_rag_70b(question):
    response = flat_rag.query(question)
    return {"response": response}

try:
    result = predict_flat_rag_70b("Who is Moses?")
    print(f"Flat RAG OK - response length: {len(result['response'])}")
    print(result['response'][:300])
except Exception as e:
    print(f"ERROR: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Section D: Minimal Evaluate — 1 Row, 1 Scorer

# COMMAND ----------

# DBTITLE 1,Minimal evaluate() — isolate the framework
mlflow.set_experiment("/Shared/graphrag_bible_evaluation_debug")

MINI_DATASET = [
    {
        "inputs": {"question": "Who is Moses?"},
        "expectations": {
            "expected_facts": [
                "Moses led the Israelites out of Egypt",
                "Moses received the Law from God at Sinai",
            ],
        },
    },
]

try:
    result = mlflow.genai.evaluate(
        data=MINI_DATASET,
        predict_fn=predict_direct_llm,
        scorers=[verse_citation],
    )
    print(f"Minimal evaluate() SUCCESS - run_id: {result.run_id}")
    print(f"Metrics: {result.metrics}")
except Exception as e:
    print(f"Minimal evaluate() FAILED: {e}")
    import traceback; traceback.print_exc()

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Section E: Incremental Scorer Addition

# COMMAND ----------

# DBTITLE 1,Add scorers one by one
scorer_groups = [
    ("verse_citation only", [verse_citation]),
    ("+ citation_completeness", [verse_citation, citation_completeness]),
    ("+ provenance_chain", [verse_citation, citation_completeness, provenance_chain]),
    ("+ hallucination_check (Guidelines)", GOVERNANCE_SCORERS + [verse_citation]),
    ("+ Correctness", GOVERNANCE_SCORERS + [Correctness(), verse_citation]),
    ("ALL scorers", EVAL_SCORERS),
]

for label, scorers in scorer_groups:
    try:
        result = mlflow.genai.evaluate(
            data=MINI_DATASET,
            predict_fn=predict_direct_llm,
            scorers=scorers,
        )
        print(f"  {label}: OK ({len(result.metrics)} metrics)")
    except Exception as e:
        print(f"  {label}: FAILED - {e}")
        break

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Section F: Full Dataset, Single Config

# COMMAND ----------

# DBTITLE 1,Evaluation Dataset (first 5 rows for quick test)
EVAL_DATASET_MINI = [
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
]

print(f"Mini eval dataset: {len(EVAL_DATASET_MINI)} rows")

# COMMAND ----------

# DBTITLE 1,Run: GraphRAG 70B on mini dataset with all scorers
try:
    result = mlflow.genai.evaluate(
        data=EVAL_DATASET_MINI,
        predict_fn=predict_graphrag_70b,
        scorers=EVAL_SCORERS,
    )
    print("GraphRAG 70B on mini dataset: SUCCESS")
    print(f"Run ID: {result.run_id}")
    for k, v in sorted(result.metrics.items()):
        print(f"  {k}: {v}")
except Exception as e:
    print(f"FAILED: {e}")
    import traceback; traceback.print_exc()

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Section G: Cost Analysis — Correct API

# COMMAND ----------

# DBTITLE 1,Test mlflow.search_traces() with correct API
try:
    experiment = mlflow.get_experiment_by_name("/Shared/graphrag_bible_evaluation_debug")
    if experiment:
        traces_df = mlflow.search_traces(
            experiment_ids=[experiment.experiment_id],
            max_results=5,
        )
        print(f"search_traces() OK - found {len(traces_df)} traces")
        print(f"Columns: {list(traces_df.columns)}")
        if len(traces_df) > 0:
            print(f"Sample row keys: {list(traces_df.iloc[0].keys())[:10]}")
    else:
        print("Experiment not found - run Section D first")
except Exception as e:
    print(f"search_traces() FAILED: {e}")
    import traceback; traceback.print_exc()

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## Summary
# MAGIC
# MAGIC If all sections above pass, the fixes are validated. Apply them to the production notebook `notebooks/05_Evaluation.py`:
# MAGIC
# MAGIC 1. `COLLECT_LIST` ordering: use `ARRAY_SORT(COLLECT_LIST(STRUCT(...)))` in `flat_rag.py`
# MAGIC 2. No `@mlflow.trace` on predict functions
# MAGIC 3. No `mlflow.start_run()` wrapping `mlflow.genai.evaluate()`
# MAGIC 4. `mlflow.search_traces(experiment_ids=[...])` instead of `run_id=`
# MAGIC 5. Remove unused `SpanType` import
