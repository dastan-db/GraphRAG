# Your 70B Model Lost to an 8B With a Graph

*Why structure beats parameters for enterprise AI retrieval — and what the evaluation data proves.*

---

The default playbook for improving AI quality in 2026 is straightforward: use a bigger model. When the 8B doesn't work, try the 70B. When the 70B doesn't work, reach for the frontier API. Each step up costs more, runs slower, and adds a vendor dependency — but at least the answers get better.

Except when they don't.

We ran 35 head-to-head test cases comparing a Llama 3.1 8B model equipped with a knowledge graph against a Llama 3.3 70B model running solo. The 8B won.

| Metric | 8B + Graph | 70B Raw LLM |
|---|---|---|
| Core evaluation score (/14) | **8.7** | 5.6 |
| Source grounding (/3) | **1.8** | 0.7 |
| Audit trail (/1) | **1.0** | 0.0 |
| Mean latency | **2.9s** | 8.8s |
| Cost per 1M input tokens | **$0.075** | $1.00 |

A model that is 9x smaller, 13x cheaper, and 3x faster produced answers that scored 55% higher on a rigorous multi-dimensional evaluation. The smaller model also provided a full audit trail for every answer. The larger model could not.

This is not a cherry-picked result. It is a structural consequence of architecture decisions that engineering leaders should understand before signing the next model-serving contract.

## The Evaluation: 35 Cases, 6 Dimensions, No Shortcuts

The evaluation used a purpose-built test suite of 35 core test cases spanning six categories, plus 12 document-scoped access-control cases. Each case was scored across six dimensions by a Llama 3.3 70B judge model (temperature=0) against a detailed rubric with expected tool calls, evidence triples, and gold answers.

The six scoring dimensions:

| Dimension | Max | What It Measures |
|---|---|---|
| Tool use correctness | 2 | Did the agent call the right tools with the right arguments? |
| Evidence correctness | 3 | Are the expected evidence triples present in the response? |
| Grounded answer | 3 | Is the answer derived from tool outputs, not training data? |
| Completeness | 2 | Does the answer cover all expected aspects of the question? |
| Source grounding | 3 | Can every claim be traced to a tool output or corpus citation? |
| Audit trail | 1 | Does a verifiable tool-call chain exist? (structural: 1 for graph, 0 for raw) |

The test suite covers six distinct categories of questions, each targeting a different graph capability: multi-hop traversal, entity disambiguation, constraint queries, set operations, single-hop controls, and adversarial hallucination traps. Four model variants were tested: 8B and 70B, each with and without the knowledge graph.

This is not a benchmark designed to make graphs look good. The control categories include single-hop questions where a raw LLM should do fine, and adversarial cases designed to trip up graph agents that over-rely on tool output.

## Where Structure Dominates: Category-Level Results

The aggregate numbers tell the story, but the per-category breakdown reveals *why* structure matters.

| Category | 8B + Graph | 70B Raw | Delta |
|---|---|---|---|
| Set operations (n=5) | 7.2 | 3.6 | **+3.6** |
| Disambiguation (n=4) | 9.5 | 4.2 | **+5.3** |
| Constraints (n=5) | 8.2 | 4.0 | **+4.2** |
| Control single-hop (n=7) | 12.3 | 5.9 | **+6.4** |
| Adversarial (n=6) | 8.0 | 6.3 | **+1.7** |
| Multi-hop (n=8) | 6.8 | 7.9 | -1.1 |

Three patterns stand out.

**Set operations expose a structural impossibility.** When the question requires enumerating entities that appear in multiple books and computing intersections or differences, the 70B raw model cannot do it reliably. It hallucinates set membership because it has no mechanism to enumerate — it guesses from training data. The 8B graph agent calls `find_cross_book_entities` and `list_entities_by_book`, gets exact results, and reports them. An 8B model with the right tools beats a 70B model with the right training. (At 70B+Graph, this category scored 13.4/14 — a +9.8 delta over 70B Raw.)

**Disambiguation is the enterprise nightmare.** The Bible has multiple people named Mary, multiple places called Bethlehem, and characters who go by different names across books (Elijah/Elias, Paul/Saul). The knowledge graph stores them as separate nodes with distinct entity IDs. The raw LLM conflates them because text has no entity-ID concept. In enterprise systems — multiple "John Smith" clients, similarly named procedures in healthcare, services with overlapping names in a microservice architecture — this failure mode is not academic. The graph solves it structurally. Delta: +5.3 for 8B, +5.5 for 70B.

**Multi-hop is the one category where the 70B raw model wins over 8B+Graph.** This makes sense: multi-hop traversal requires chaining reasoning across multiple entities and books. The larger model's greater reasoning capacity helps reconstruct connections from training data. But note: even here, the 70B raw model cannot prove its reasoning path. It may be correct, but it cannot show its work. And when you give the 70B model the graph (70B+Graph: 9.0/14 vs 70B Raw: 7.9/14), it wins again.

## Source Grounding: The Metric That Changes the Conversation

Correctness is necessary but insufficient for enterprise deployment. The question regulators ask is not "Is the answer right?" but "Can you prove the answer is right?"

Source grounding measures whether every claim in the response traces to a tool output or corpus citation. Here are the multipliers:

| Variant | Source Grounding (/3) | vs. Raw LLM |
|---|---|---|
| 8B + Graph | 1.8 | **4.8x** better than 8B Raw (0.4) |
| 70B + Graph | 2.2 | **3.1x** better than 70B Raw (0.7) |

The graph doesn't just improve answers. It makes answers *provable*. And the improvement is larger at smaller model sizes — precisely because smaller models have less training data to fall back on, so the graph's contribution is proportionally greater.

The audit trail dimension reinforces this. It is a binary structural property: graph agents produce a tool-call chain that can be audited; raw LLMs do not. This is not a quality gradient. It is a capability boundary. No amount of model scaling gives a raw LLM an audit trail.

## The Win/Loss Tally

Across all 35 core test cases (34 where both 8B variants scored without errors):

- **8B: Graph wins 27, Raw wins 7**
- **70B: Graph wins 29, Raw wins 6**

The graph agent wins at both model sizes. The smaller model with graph tools wins more often than a model 9x its size without them.

## The Cost and Latency Argument

Structure doesn't just improve quality — it changes the economics.

| Property | 8B + Graph | 70B Raw |
|---|---|---|
| Input cost (per 1M tokens) | $0.075 | $1.00 |
| Output cost (per 1M tokens) | $0.30 | $1.00 |
| Mean latency | 2.9s | 8.8s |
| p95 latency (est.) | ~5s | ~22s |
| Audit trail | Full tool-call chain | None |

The 8B graph agent is 13x cheaper on input tokens, 3x cheaper on output tokens, and 3x faster at the mean. Its p95 latency is roughly where the 70B raw model's *mean* latency sits.

This matters for engineering leaders making capacity planning decisions. If you can serve the same (or better) quality at 1/13th the token cost and 1/3rd the latency, you can handle 13x more concurrent users on the same serving budget — or redirect that budget to the graph infrastructure that enables the improvement.

## Where the 8B Graph Agent Struggles — An Honest Assessment

The 8B model with graph tools is not universally better than the 70B model with graph tools. Where reasoning capacity matters independently of structure, the larger model wins:

| Category | 8B + Graph | 70B + Graph | Gap |
|---|---|---|---|
| Multi-hop | 6.8 | 9.0 | -2.2 |
| Constraints | 8.2 | 6.6 | +1.6 |
| Adversarial | 8.0 | 8.5 | -0.5 |
| Set operations | 7.2 | 13.4 | -6.2 |

Set operations at 70B+Graph hit 13.4/14 — near perfect — while 8B+Graph manages 7.2/14. The tools provide the right data in both cases, but the 70B model is better at synthesizing multi-entity results into coherent answers. Similarly, multi-hop traversal benefits from the larger model's ability to chain reasoning across tool outputs.

Document-scoped access control also shows a gap: scope compliance is 1.2/2 for 8B+Graph vs 1.5/2 for 70B+Graph. Smaller models are more likely to leak information from restricted documents, even with SQL-level filtering — the model sometimes infers restricted content from adjacent context.

The right takeaway is not "8B is always sufficient." It is "choose architecture first, then right-size the model." An 8B with a graph beats a 70B without one. A 70B with a graph beats everything else.

## The Architecture That Enables This

The knowledge graph is stored in Delta tables on Databricks — entities, relationships, and verse-level provenance. No external graph database. The agent is a LangGraph orchestration layer with purpose-built tools:

| Tool | What It Does |
|---|---|
| `find_entity` | Search entities by name, with optional document-scope filtering |
| `find_connections` | Retrieve relationships for an entity (source or target) |
| `trace_path` | BFS shortest path between two entities across the graph |
| `get_context` | Retrieve text for citation-level provenance |
| `get_entity_summary` | Entity profile with relationship counts and in-document appearances |
| `find_cross_entities` | Entities appearing across multiple documents |

Every tool executes SQL against Delta tables through a pluggable backend (Databricks SQL in production, DuckDB for local development). Every tool call is traced via MLflow. Every query result becomes part of the provenance chain.

The entity pre-lookup step is worth noting: before the agent reasons, it extracts entity mentions from the question, checks them against the graph, and injects "FOUND IN GRAPH" or "NOT IN GRAPH" annotations into the system prompt. This gives even the 8B model a structural head start — it knows which entities exist and which don't before it starts planning tool calls.

The entire system — extraction, graph storage, agent serving, evaluation, and the interactive demo — deploys with a single `databricks bundle deploy` command. Unity Catalog governs the graph. MLflow traces every query. No external services required.

## The Decision Framework for Engineering Leaders

The next time your team proposes scaling up to a larger model because quality isn't sufficient, run this checklist first:

**1. Is the quality gap about knowledge or structure?**
If the model knows the facts but can't reliably connect them across documents, enumerate sets, or disambiguate entities — that's a structure problem. A knowledge graph solves it at any model size.

**2. Do you need provable answers or just plausible ones?**
If your compliance, legal, or audit team needs to trace every claim to source data, no model size gives you an audit trail. Only structured retrieval does.

**3. What's your cost/latency envelope?**
If you're serving hundreds of concurrent users, the 13x cost reduction and 3x latency improvement from using a smaller model with graph tools may matter more than the marginal quality gain from a larger model.

**4. Where does the larger model actually help?**
Complex multi-hop reasoning across many entities and sophisticated set-operation synthesis genuinely benefit from larger models. If your workload is dominated by these query types, invest in both structure *and* scale. If it's dominated by entity lookup, disambiguation, and constrained retrieval — structure alone may be enough.

## The Punchline

The industry's instinct is to solve AI quality problems by scaling up models. The data suggests a different approach: scale up structure.

An 8B model with a knowledge graph scored 8.7/14 on a rigorous 35-case evaluation. A 70B model without one scored 5.6/14. The smaller model was 13x cheaper, 3x faster, and — critically — the only one that could show its work.

Structure is not a substitute for model intelligence. It is a multiplier. And the evaluation data shows that the multiplier from adding a knowledge graph exceeds the multiplier from scaling model parameters by 9x.

The question for engineering leaders is not "Which model should we use?" It is "What structure should we give it?"

---

*This analysis is based on the [GraphRAG Solution Accelerator](https://github.com/databricks/GraphRAG), a Databricks-native implementation of graph-structured retrieval with provenance. The evaluation suite, scoring methodology, and full per-case results are available in the repository. The demo corpus uses five books of the King James Bible as a proxy for enterprise document collections — chosen for its dense entity networks and independently verifiable ground truth.*

*For the complementary governance perspective — why auditability, not just accuracy, is the deployment prerequisite — see [Part 1: Why Your AI Can't Show Its Work](./BLOG.md).*
