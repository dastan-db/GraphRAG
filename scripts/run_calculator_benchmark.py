"""Calculator Benchmark — quantify the value of the Graph Engine.

The "calculator analogy": a weaker student WITH a calculator beats a stronger
student WITHOUT one on a math test.

Design (SOTA-as-Reference):
  1. BENCHMARK: SOTA model (GPT-5.2) answers each question raw — these answers
     ARE the reference standard.  SOTA is not scored; it defines the bar.
  2. TEST:      Llama 70B + Graph Engine answers the same questions.
  3. BASELINE:  Llama 70B raw answers the same questions.
  An LLM judge (the SOTA model) scores TEST and BASELINE against the
  BENCHMARK reference on a 1-5 scale.

  Questions are scoped to the provided corpus only (Genesis, Exodus, Ruth,
  Matthew, Acts) — NOT general biblical knowledge.

Usage:
    python scripts/run_calculator_benchmark.py
    python scripts/run_calculator_benchmark.py --sota gpt_5_2 --test llama_3_3_70b
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(_SCRIPT_DIR, "..")


def _load_env_file(path: str):
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(os.path.join(PROJECT_ROOT, ".env.local"))

# ---------------------------------------------------------------------------
# Pricing ($/M tokens)
# Databricks FMAPI: DBU rate * ~$0.07/DBU (standard pay-per-token, AWS)
# OpenAI: published rates.  Adjust for your tier/region.
# ---------------------------------------------------------------------------
PRICING = {
    "llama_3_1_8b": {"input": 0.150, "output": 0.450, "label": "Llama 3.1 8B"},
    "llama_3_3_70b": {"input": 0.500, "output": 1.500, "label": "Llama 3.3 70B"},
    "gpt_5_2": {"input": 1.250, "output": 10.000, "label": "GPT-5.2"},
    "gpt_4o_mini": {"input": 0.150, "output": 0.600, "label": "GPT-4o-mini"},
}

MODELS = {
    "llama_3_1_8b": ("databricks", {"GRAPHRAG_LLM_ENDPOINT": "databricks-meta-llama-3-1-8b-instruct"}),
    "llama_3_3_70b": ("databricks", {"GRAPHRAG_LLM_ENDPOINT": "databricks-meta-llama-3-3-70b-instruct"}),
    "gpt_5_2": ("databricks", {"GRAPHRAG_LLM_ENDPOINT": "databricks-gpt-5-2"}),
    "gpt_4o_mini": ("openai", {"OPENAI_MODEL": "gpt-4o-mini"}),
}

SOTA_DEFAULT = "gpt_5_2"
TEST_DEFAULT = "llama_3_3_70b"


# ---------------------------------------------------------------------------
# Questions — no ground truth needed; SOTA answers ARE the reference.
# Mix of factual, multi-hop, analytical, enumerative, and cross-book.
# ---------------------------------------------------------------------------
QUESTIONS = [
    {"id": "ruth_complete_cast",
     "question": "Based ONLY on the book of Ruth: list every named person who appears. For each person, state their role (e.g., husband, mother-in-law) and one key relationship."},
    {"id": "ruth_to_david_lineage",
     "question": "Based ONLY on Genesis, Exodus, Ruth, Matthew, and Acts: trace the exact family lineage from Ruth to King David. Name each person in the chain and cite specific verse references from the book of Ruth."},
    {"id": "jacob_across_books",
     "question": "Based ONLY on Genesis, Exodus, Ruth, Matthew, and Acts: in which of these five books is Jacob mentioned by name? For each book, describe one specific thing Jacob did or that happened to him."},
    {"id": "genesis_ch5_lineage",
     "question": "Based ONLY on Genesis: list every parent-child relationship mentioned in Genesis chapter 5. For each, name the parent and child."},
    {"id": "moses_book_by_book",
     "question": "Based ONLY on Genesis, Exodus, Ruth, Matthew, and Acts: in which of these five books does Moses appear? For each book where he appears, list his key relationships and interactions in that specific book."},
    {"id": "abraham_connections",
     "question": "Based ONLY on Genesis, Exodus, Ruth, Matthew, and Acts: list every person that Abraham has a direct relationship with. State the type of relationship (e.g., PARENT_OF, SPOUSE_OF, SPOKE_TO) and which book it appears in."},
    {"id": "ruth_boaz_path",
     "question": "Based ONLY on the book of Ruth: describe the complete relationship chain between Ruth and Boaz. Include every person involved and cite the specific verses where each relationship is established."},
]


# ---------------------------------------------------------------------------
# Result data
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    group: str          # "sota_raw", "test_graph", "test_raw"
    model_id: str
    question_id: str
    answer: str
    score: int = 0      # 1-5 (judge), 5 for SOTA by definition
    reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None


# ---------------------------------------------------------------------------
# Subprocess helpers (reused from previous version)
# ---------------------------------------------------------------------------
def _parse_metrics(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith("METRICS:"):
            try:
                return json.loads(line[len("METRICS:"):])
            except json.JSONDecodeError:
                pass
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _parse_answer(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        if line.startswith("ANSWER:"):
            try:
                return json.loads(line[len("ANSWER:"):])
            except json.JSONDecodeError:
                return line[len("ANSWER:"):]
    return stdout


def _compute_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICING.get(model_id, {"input": 0, "output": 0})
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def _model_available(model_id: str) -> bool:
    provider, _ = MODELS[model_id]
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "databricks":
        return bool(
            os.environ.get("DATABRICKS_HOST")
            or os.path.isfile(os.path.expanduser("~/.databrickscfg"))
        )
    return False


def _run_with_graph(model_id: str, question: str) -> tuple[str, dict, float]:
    provider, env_overrides = MODELS[model_id]
    env = {**os.environ, "GRAPHRAG_BACKEND": "local", "GRAPHRAG_LLM_PROVIDER": provider}
    env.update(env_overrides)
    t0 = time.monotonic()
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "test_local.py"), question],
        capture_output=True, text=True, timeout=300, env=env, cwd=PROJECT_ROOT,
    )
    latency = time.monotonic() - t0
    if result.returncode != 0:
        raise RuntimeError(f"test_local.py failed: {result.stderr[:500]}")
    return result.stdout, _parse_metrics(result.stdout), latency


def _run_raw_llm(model_id: str, question: str) -> tuple[str, dict, float]:
    provider, env_overrides = MODELS[model_id]
    env = {**os.environ, "GRAPHRAG_LLM_PROVIDER": provider}
    env.update(env_overrides)
    t0 = time.monotonic()
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "test_raw_llm.py"),
         question, "--llm", provider],
        capture_output=True, text=True, timeout=120, env=env, cwd=PROJECT_ROOT,
    )
    latency = time.monotonic() - t0
    if result.returncode != 0:
        raise RuntimeError(f"test_raw_llm.py failed: {result.stderr[:500]}")
    return result.stdout, _parse_metrics(result.stdout), latency


# ---------------------------------------------------------------------------
# LLM-as-Judge
# ---------------------------------------------------------------------------
JUDGE_PROMPT = """You are an impartial judge evaluating a CANDIDATE answer against a REFERENCE answer.

Question: {question}

Reference answer (the standard):
{reference}

Candidate answer (to evaluate):
{candidate}

SCORING RULES:
- Focus on FACTUAL ACCURACY of the candidate's claims. A shorter but correct answer is better than a longer, inaccurate one.
- Do NOT penalize for being more concise than the reference. The candidate may include fewer details — that is fine if the details it DOES include are correct.
- Do NOT penalize for different formatting (bullet points, structured sections, prose vs. lists).
- DO penalize for factual errors, fabricated claims, or incorrect attributions.
- DO reward precise data (exact names, verse citations, specific relationships) that matches the reference.

Score on a 1-5 scale:
  5 = Factual claims are correct and address the core of the question well
  4 = Factual claims are mostly correct, addresses the question with minor gaps
  3 = More correct than incorrect, addresses the main point but has some errors or notable omissions
  2 = Mix of correct and incorrect claims, or fails to address key parts of the question
  1 = Mostly incorrect, fabricated, or does not answer the question

Reply with EXACTLY this format (nothing else):
SCORE: <number>
REASON: <one sentence explanation>"""


def _get_judge_llm(sota_id: str):
    """Instantiate the SOTA LLM for judge calls."""
    provider, env_overrides = MODELS[sota_id]
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        model = env_overrides.get("OPENAI_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
        return ChatOpenAI(model=model, temperature=0.0)
    from databricks_langchain import ChatDatabricks
    endpoint = env_overrides.get("GRAPHRAG_LLM_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct")
    return ChatDatabricks(endpoint=endpoint, temperature=0.0)


def judge_answer(
    llm, question: str, reference: str, candidate: str,
) -> tuple[int, str]:
    """Ask the judge LLM to score a candidate against the reference. Returns (score, reason)."""
    prompt = JUDGE_PROMPT.format(
        question=question, reference=reference, candidate=candidate,
    )
    response = llm.invoke([{"role": "user", "content": prompt}])
    text = response.content.strip()

    score_match = re.search(r"SCORE:\s*(\d)", text)
    reason_match = re.search(r"REASON:\s*(.+)", text, re.IGNORECASE)
    score = int(score_match.group(1)) if score_match else 0
    reason = reason_match.group(1).strip() if reason_match else text[:200]
    return min(max(score, 1), 5), reason


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------
def run_benchmark(sota_id: str, test_id: str) -> list[RunResult]:
    sota_label = PRICING[sota_id]["label"]
    test_label = PRICING[test_id]["label"]
    n_q = len(QUESTIONS)
    total_steps = n_q * 5  # per question: 3 answer runs + 2 judge calls
    step = 0
    results: list[RunResult] = []

    print(f"  Initializing judge ({sota_label})...", flush=True)
    judge_llm = _get_judge_llm(sota_id)

    for q in QUESTIONS:
        qid = q["id"]
        question = q["question"]

        # --- 1. SOTA raw (reference) ---
        step += 1
        print(f"  [{step}/{total_steps}] BENCHMARK  {sota_label:<16} | {qid}...", end=" ", flush=True)
        try:
            stdout, metrics, latency = _run_raw_llm(sota_id, question)
            ref_answer = _parse_answer(stdout)
            ref_cost = _compute_cost(sota_id, metrics["input_tokens"], metrics["output_tokens"])
            results.append(RunResult(
                group="sota_raw", model_id=sota_id, question_id=qid,
                answer=ref_answer, score=5, reason="Reference (defines the standard)",
                input_tokens=metrics["input_tokens"], output_tokens=metrics["output_tokens"],
                total_tokens=metrics["total_tokens"], cost_usd=ref_cost,
                latency_s=round(latency, 1),
            ))
            print(f"OK ({latency:.1f}s, ${ref_cost:.6f})")
        except Exception as e:
            ref_answer = f"[ERROR: {e}]"
            results.append(RunResult(
                group="sota_raw", model_id=sota_id, question_id=qid,
                answer=ref_answer, score=5, error=str(e)[:200],
            ))
            print(f"ERROR: {str(e)[:80]}")

        # --- 2. 8B + graph (test) ---
        step += 1
        print(f"  [{step}/{total_steps}] TEST+graph {test_label:<16} | {qid}...", end=" ", flush=True)
        try:
            stdout, metrics, latency = _run_with_graph(test_id, question)
            test_answer = _parse_answer(stdout)
            test_cost = _compute_cost(test_id, metrics["input_tokens"], metrics["output_tokens"])
            print(f"done ({latency:.1f}s, ${test_cost:.6f})")
        except Exception as e:
            test_answer = f"[ERROR: {e}]"
            test_cost = 0.0
            metrics = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            latency = 0.0
            print(f"ERROR: {str(e)[:80]}")

        # Judge test answer
        step += 1
        print(f"  [{step}/{total_steps}] JUDGE      test+graph       | {qid}...", end=" ", flush=True)
        try:
            score, reason = judge_answer(judge_llm, question, ref_answer, test_answer)
            results.append(RunResult(
                group="test_graph", model_id=test_id, question_id=qid,
                answer=test_answer, score=score, reason=reason,
                input_tokens=metrics["input_tokens"], output_tokens=metrics["output_tokens"],
                total_tokens=metrics["total_tokens"], cost_usd=test_cost,
                latency_s=round(latency, 1),
            ))
            print(f"{score}/5 — {reason[:60]}")
        except Exception as e:
            results.append(RunResult(
                group="test_graph", model_id=test_id, question_id=qid,
                answer=test_answer, score=0, error=str(e)[:200],
                cost_usd=test_cost,
            ))
            print(f"JUDGE ERROR: {str(e)[:80]}")

        # --- 3. 8B raw (baseline) ---
        step += 1
        print(f"  [{step}/{total_steps}] BASELINE   {test_label:<16} | {qid}...", end=" ", flush=True)
        try:
            stdout, metrics, latency = _run_raw_llm(test_id, question)
            base_answer = _parse_answer(stdout)
            base_cost = _compute_cost(test_id, metrics["input_tokens"], metrics["output_tokens"])
            print(f"done ({latency:.1f}s, ${base_cost:.6f})")
        except Exception as e:
            base_answer = f"[ERROR: {e}]"
            base_cost = 0.0
            metrics = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            latency = 0.0
            print(f"ERROR: {str(e)[:80]}")

        # Judge baseline answer
        step += 1
        print(f"  [{step}/{total_steps}] JUDGE      baseline         | {qid}...", end=" ", flush=True)
        try:
            score, reason = judge_answer(judge_llm, question, ref_answer, base_answer)
            results.append(RunResult(
                group="test_raw", model_id=test_id, question_id=qid,
                answer=base_answer, score=score, reason=reason,
                input_tokens=metrics["input_tokens"], output_tokens=metrics["output_tokens"],
                total_tokens=metrics["total_tokens"], cost_usd=base_cost,
                latency_s=round(latency, 1),
            ))
            print(f"{score}/5 — {reason[:60]}")
        except Exception as e:
            results.append(RunResult(
                group="test_raw", model_id=test_id, question_id=qid,
                answer=base_answer, score=0, error=str(e)[:200],
                cost_usd=base_cost,
            ))
            print(f"JUDGE ERROR: {str(e)[:80]}")

        print()

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _group_stats(results: list[RunResult], group: str):
    rs = [r for r in results if r.group == group]
    n = len(rs) or 1
    scores = [r.score for r in rs if r.score > 0]
    avg_score = sum(scores) / len(scores) if scores else 0.0
    total_cost = sum(r.cost_usd for r in rs)
    total_tokens = sum(r.total_tokens for r in rs)
    return {
        "n": len(rs),
        "avg_score": avg_score,
        "pct_of_sota": avg_score / 5.0 * 100,
        "total_cost": total_cost,
        "cost_per_q": total_cost / n,
        "tokens_per_q": total_tokens / n,
    }


def generate_report(results: list[RunResult], sota_id: str, test_id: str):
    sota_label = PRICING[sota_id]["label"]
    test_label = PRICING[test_id]["label"]

    sota = _group_stats(results, "sota_raw")
    test_g = _group_stats(results, "test_graph")
    test_r = _group_stats(results, "test_raw")

    W = 72
    print("\n" + "=" * W)
    print("  CALCULATOR BENCHMARK REPORT")
    print("=" * W)
    print()
    print("  The Calculator Analogy:")
    print("  A weaker student WITH a calculator should beat a stronger")
    print("  student WITHOUT one — even though the stronger student is smarter.")
    print()

    # --- Summary table ---
    print(f"  {'Group':<34} {'Avg Score':>10} {'% of SOTA':>10} {'$/question':>12}")
    print(f"  {'─' * 68}")
    print(f"  BENCHMARK: {sota_label + ' raw':<21} {'5.0/5':>10} {'100%':>10} ${sota['cost_per_q']:>11.6f}")
    print(f"  TEST:      {test_label + ' + graph':<21} {test_g['avg_score']:.1f}/5{' ':>5} {test_g['pct_of_sota']:>9.0f}% ${test_g['cost_per_q']:>11.6f}")
    print(f"  BASELINE:  {test_label + ' raw':<21} {test_r['avg_score']:.1f}/5{' ':>5} {test_r['pct_of_sota']:>9.0f}% ${test_r['cost_per_q']:>11.6f}")

    # --- Calculator lift ---
    lift_pct = test_g["pct_of_sota"] - test_r["pct_of_sota"]
    print(f"""
  Calculator Lift:
    {test_label} raw:     {test_r['avg_score']:.1f}/5  ({test_r['pct_of_sota']:.0f}% of SOTA quality)
    {test_label} + graph: {test_g['avg_score']:.1f}/5  ({test_g['pct_of_sota']:.0f}% of SOTA quality)
    Improvement:          +{lift_pct:.0f} percentage points""")

    # --- Cost comparison ---
    if test_g["cost_per_q"] > 0:
        print(f"""
  Cost:
    {sota_label} raw (benchmark): ${sota['cost_per_q']:.6f}/question  ({sota['tokens_per_q']:.0f} tokens/q)
    {test_label} + graph (test):  ${test_g['cost_per_q']:.6f}/question  ({test_g['tokens_per_q']:.0f} tokens/q)
    {test_label} raw (baseline):  ${test_r['cost_per_q']:.6f}/question  ({test_r['tokens_per_q']:.0f} tokens/q)""")

    # --- Key Insight ---
    print(f"\n  {'─' * 68}")
    print("  KEY INSIGHT:")
    if test_g["pct_of_sota"] >= 80:
        print(f"    The graph engine brings {test_label} to {test_g['pct_of_sota']:.0f}% of {sota_label} quality.")
        print(f"    That's a +{lift_pct:.0f}pp improvement over {test_label} raw ({test_r['pct_of_sota']:.0f}%).")
    elif test_g["pct_of_sota"] > test_r["pct_of_sota"]:
        print(f"    The graph engine improves {test_label} from {test_r['pct_of_sota']:.0f}% to {test_g['pct_of_sota']:.0f}% of SOTA quality (+{lift_pct:.0f}pp).")
    else:
        print(f"    The graph engine did not improve quality in this run ({test_g['pct_of_sota']:.0f}% vs {test_r['pct_of_sota']:.0f}% of SOTA).")

    # --- Per-question breakdown ---
    print(f"\n  Per-Question Scores (1-5, judged against {sota_label} reference):")
    tg_hdr = f"{test_label}+graph"
    tr_hdr = f"{test_label} raw"
    print(f"  {'Question':<26} {tg_hdr:>14} {tr_hdr:>14} {'Lift':>8}")
    print(f"  {'─' * 64}")
    for q in QUESTIONS:
        qid = q["id"]
        g = next((r for r in results if r.group == "test_graph" and r.question_id == qid), None)
        b = next((r for r in results if r.group == "test_raw" and r.question_id == qid), None)
        g_s = g.score if g else 0
        b_s = b.score if b else 0
        lift = g_s - b_s
        lift_str = f"+{lift}" if lift > 0 else str(lift)
        print(f"  {qid:<26} {g_s:>14}/5 {b_s:>14}/5 {lift_str:>8}")

    # --- Judge reasoning ---
    print(f"\n  Judge Reasoning ({test_label} + graph):")
    for q in QUESTIONS:
        qid = q["id"]
        g = next((r for r in results if r.group == "test_graph" and r.question_id == qid), None)
        if g:
            print(f"    {qid}: {g.score}/5 — {g.reason[:80]}")

    print(f"\n{'=' * W}")

    return {
        "sota": sota, "test_graph": test_g, "test_raw": test_r,
        "lift_pct": lift_pct,
    }


def save_results(results: list[RunResult], report_data: dict,
                 sota_id: str, test_id: str):
    data_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "benchmark_results.json")
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "design": {
            "type": "SOTA-as-Reference",
            "sota_model": sota_id,
            "test_model": test_id,
            "judge_model": sota_id,
            "questions": len(QUESTIONS),
        },
        "results": [asdict(r) for r in results],
        "summary": report_data,
        "pricing_table": PRICING,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"Results saved to {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Calculator Benchmark — SOTA-as-Reference",
    )
    parser.add_argument(
        "--sota", choices=list(MODELS.keys()), default=SOTA_DEFAULT,
        help=f"SOTA model (the reference standard; default: {SOTA_DEFAULT})",
    )
    parser.add_argument(
        "--test", choices=list(MODELS.keys()), default=TEST_DEFAULT,
        help=f"Weaker model to test with/without graph (default: {TEST_DEFAULT})",
    )
    args = parser.parse_args()

    sota_label = PRICING[args.sota]["label"]
    test_label = PRICING[args.test]["label"]

    print("=" * 72)
    print("  CALCULATOR BENCHMARK — SOTA-as-Reference")
    print("=" * 72)
    print(f"  BENCHMARK : {sota_label} raw — the reference standard")
    print(f"  TEST      : {test_label} + Graph Engine")
    print(f"  BASELINE  : {test_label} raw")
    print(f"  JUDGE     : {sota_label} (scores TEST and BASELINE vs BENCHMARK)")
    print(f"  Questions : {len(QUESTIONS)}")
    print(f"  Total steps: {len(QUESTIONS) * 5} (3 answer runs + 2 judge calls per question)")
    print()

    results = run_benchmark(args.sota, args.test)
    report_data = generate_report(results, args.sota, args.test)
    save_results(results, report_data, args.sota, args.test)


if __name__ == "__main__":
    main()
