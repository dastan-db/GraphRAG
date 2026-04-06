# Trust Failure Taxonomy

## Accepted GraphRAG

| taxonomy  | count | example                                                                                                                                      |
| --------- | ----- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| synthesis | 18    | What sequence of finance-crisis events takes Enron from valuation concerns and SEC scrutiny to restatement, merger collapse, and bankruptcy? |
| routing   | 3     | How did Kenneth Lay's executive leadership structure change after he resumed the CEO role in August 2001?                                    |
| model     | 1     | What path connects Arthur Andersen to the FBI document-destruction investigation in the local Enron evidence?                                |

## GPT-5.4

| taxonomy  | count | example                                                                                             |
| --------- | ----- | --------------------------------------------------------------------------------------------------- |
| benchmark | 86    | Which executives reported directly to Kenneth Lay after he resumed the CEO role on August 14, 2001? |
| routing   | 4     | What was the reporting chain from Andrew Fastow to Kenneth Lay in 2001?                             |
| synthesis | 2     | Who was Greg Whalley in Enron's leadership after Jeff Skilling resigned?                            |
| model     | 1     | Who was Andrew Fastow's boss?                                                                       |

## Definitions

- `data`: thin or missing validated coverage in a required cell.
- `tool`: deterministic query or aggregation shape is the limiting factor.
- `synthesis`: retrieved facts exist but the final answer under-explains or under-cites them.
- `routing`: the system picks an inferior primitive or misstates structure/participants.
- `benchmark`: the question fundamentally depends on corpus access that raw GPT does not have.
- `model`: factual or hallucination issues not better explained by routing/synthesis/tooling.
- `operational`: runtime errors or failed executions.
