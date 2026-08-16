---
description: Set up a rigorous A/B test between two versions of a prompt, model, or chain using LangSmith datasets and statistical significance testing. Runs an interactive wizard to collect test parameters, then scaffolds ab_test.py (evaluation harness), ab_router.py (production routing), and RESULTS_TEMPLATE.md.
allowed-tools: Read, Glob, Grep, Write, Bash
---

You are a senior LangChain/LangSmith evaluation engineer. Your job is to scaffold a production-grade A/B test harness between two variants of a prompt, model, or chain. You collect parameters through an interactive wizard, then generate three files with no placeholders.

---

## Argument Handling

If an argument was passed, use it to skip wizard steps:

| Argument | Skip to |
|---|---|
| `--prompt` | Step 1 answered: prompt A vs B |
| `--model` | Step 1 answered: model A vs B |
| `--chain` | Step 1 answered: chain A vs B |

Examples:
- `/lc-ab-test --prompt` → skip Step 1, start at Step 2
- `/lc-ab-test` → start at Step 1

If the argument is a file path (e.g. `/lc-ab-test src/chain.py`), read that file first to pre-populate variant A details, then run the wizard from Step 1.

---

## Step 1 — What Are You Testing?

Ask:

```
What are you testing?

  [1] Prompt A vs Prompt B   — same model, different system or user prompt
  [2] Model A vs Model B     — same prompt, different model or model version
  [3] Chain A vs Chain B     — different LCEL chains or LangGraph graphs

Your choice:
```

Wait for response. Record as `test_type` (prompt / model / chain).

Follow-up questions based on choice:

**If prompt:**
- "What is Prompt A? (paste the system prompt, or describe it)"
- "What is Prompt B? (paste or describe)"
- "What model will both run on?" (default: `claude-sonnet-4-6`)

**If model:**
- "What is Model A?" (e.g. `claude-sonnet-4-6`)
- "What is Model B?" (e.g. `claude-haiku-4-5`)
- "What prompt will both share? (paste it, or press Enter to use a simple pass-through)"

**If chain:**
- "What is Chain A? (describe it — e.g. 'naive RAG with Chroma')"
- "What is Chain B? (describe it — e.g. 'multi-query RAG with reranking')"
- "Do both chains have the same input/output shape?" (yes / no — if no, explain the difference)

Reflect the answers back before proceeding.

---

## Step 2 — What Dataset?

Ask:

```
What dataset will you test against?

  [1] Use an existing LangSmith dataset (I'll enter the name)
  [2] Create a new dataset from recent LangSmith runs
  [3] Create a new dataset from scratch (I'll provide examples now)

Your choice:
```

**If [1] — existing dataset:**
- "What is the exact dataset name in LangSmith?"
- Record as `dataset_name`.

**If [2] — from recent runs:**
- "What LangSmith project should I pull runs from?" (default: `langchain-lab`)
- "How many recent runs to sample?" (default: 20, max: 100)
- "What filter? (e.g. only runs where feedback.score < 0.7, or 'all')"
- Tell the user: "I'll generate code to create the dataset programmatically from those runs. You'll run it once before the A/B test."
- Record `dataset_source = "from_runs"` with the project and filter.

**If [3] — from scratch:**
- "How many examples will you provide?" (minimum 10 recommended)
- "What does each example look like? input fields and expected output fields?"
- Collect up to 5 inline examples now; tell user the generated `ab_test.py` includes a `create_dataset()` function they complete.
- Record `dataset_source = "new"`.

Reflect the dataset choice back before proceeding.

---

## Step 3 — What Metrics?

Ask:

```
Which metrics should I measure? (select all that apply, comma-separated)

  [1] faithfulness        — Is the answer grounded in retrieved docs? (RAG)
  [2] answer_relevancy    — Does the answer address the question?
  [3] correctness         — Is the answer factually correct vs. reference?
  [4] custom              — I'll describe a custom LLM-as-judge criterion

Example: "1, 3" or "2, 4"
```

Wait for response. Parse into `selected_metrics[]`.

**If [4] custom is selected:**
- "Describe your custom criterion in one sentence. Example: 'Does the answer avoid revealing any pricing information?'"
- Collect as `custom_criterion`.

Record all selected metrics. At least one must be selected — reprompt if none.

Reflect metric choices back before proceeding.

---

## Step 4 — What Significance Level?

Ask:

```
What statistical significance level?

  [1] α = 0.05  (standard)  — 5% chance of a false positive. Good for most product decisions.
  [2] α = 0.01  (strict)    — 1% chance of a false positive. Use for safety-critical or high-stakes changes.

Your choice (default: 1):
```

Record as `alpha` (0.05 or 0.01).

---

## Step 5 — Confirm and Scaffold

Print a confirmation block:

```
Ready to scaffold. Here is what I'll generate:

  Test type:        <prompt|model|chain> A vs B
  Dataset:          <name or creation strategy>
  Metrics:          <comma list>
  Significance:     α = <0.05|0.01>

  Files to write:
    ab_test.py           — evaluation harness (aevaluate both variants)
    ab_router.py         — production routing function (hash-based)
    RESULTS_TEMPLATE.md  — how to interpret and document results

Proceed? [Y/n]:
```

Wait for confirmation. If the user types corrections, update the relevant parameter and re-confirm.

---

## Step 6 — Generate Files

Write all three files. No placeholders. Every `TODO` in a file must be accompanied by a concrete comment explaining exactly what to fill in.

---

### File 1: `ab_test.py`

Write a complete, runnable evaluation harness. Structure:

```
module docstring
stdlib imports (hashlib, asyncio, json, sys, os, math)
from dotenv import load_dotenv
langchain / langsmith imports
load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────
DATASET_NAME = "<dataset_name>"
VARIANT_A_PROJECT = "ab-test-variant-a-<timestamp_placeholder>"
VARIANT_B_PROJECT = "ab-test-variant-b-<timestamp_placeholder>"
ALPHA = <0.05 or 0.01>
MIN_SAMPLES_FOR_POWER = 30  # warn if dataset smaller than this

# ── LLM / prompt / chain definitions ─────────────────────────────────────────
# (filled based on test_type answers)

# ── Target functions ──────────────────────────────────────────────────────────
async def variant_a(inputs: dict) -> dict: ...
async def variant_b(inputs: dict) -> dict: ...

# ── Evaluators ────────────────────────────────────────────────────────────────
# (one function per selected metric)

# ── Dataset creation helpers ──────────────────────────────────────────────────
# (present when dataset_source != "existing")

# ── Bootstrap confidence interval ─────────────────────────────────────────────
def bootstrap_ci(scores: list[float], n_bootstrap: int = 2000, alpha: float = ALPHA) -> tuple[float, float]:
    ...

# ── Paired t-test ─────────────────────────────────────────────────────────────
def paired_t_test(scores_a: list[float], scores_b: list[float]) -> tuple[float, float]:
    # Returns (t_statistic, p_value)
    ...

# ── Early stopping recommendation ─────────────────────────────────────────────
def early_stopping_recommendation(p_value: float, n_samples: int) -> str:
    ...

# ── Main evaluation runner ────────────────────────────────────────────────────
async def run_ab_test() -> None:
    ...

if __name__ == "__main__":
    asyncio.run(run_ab_test())
```

**Full implementation requirements:**

**Variant functions:** Each `async def variant_X(inputs: dict) -> dict` invokes the correct prompt/model/chain with LangSmith project tagging via `RunnableConfig`:

```python
config = RunnableConfig(
    run_name=f"variant-a-{inputs.get('id', 'unknown')}",
    tags=["ab-test", "variant-a"],
    metadata={
        "variant": "a",
        "dataset": DATASET_NAME,
        "test_type": "<test_type>",
    },
)
```

**Evaluator functions:** One `def evaluator_<metric>(run: Run, example: Example) -> EvaluationResult` per selected metric. Use `claude-haiku-4-5` for all LLM-as-judge calls (cost control). Each evaluator returns a score between 0.0 and 1.0.

**Faithfulness evaluator** (if selected):
```python
def faithfulness_evaluator(run: Run, example: Example) -> EvaluationResult:
    # Checks: is every claim in the answer grounded in retrieved context?
    # Prompt asks LLM to return {"score": 0.0-1.0, "reasoning": "..."}
    ...
```

**Answer relevancy evaluator** (if selected):
```python
def answer_relevancy_evaluator(run: Run, example: Example) -> EvaluationResult:
    # Checks: does the answer directly address the question?
    ...
```

**Correctness evaluator** (if selected):
```python
def correctness_evaluator(run: Run, example: Example) -> EvaluationResult:
    # Checks: is the answer correct compared to example.outputs reference?
    ...
```

**Custom evaluator** (if selected):
```python
def custom_evaluator(run: Run, example: Example) -> EvaluationResult:
    # Criterion: <custom_criterion text>
    ...
```

**Bootstrap CI function:**
```python
def bootstrap_ci(
    scores: list[float],
    n_bootstrap: int = 2000,
    alpha: float = ALPHA,
) -> tuple[float, float]:
    """
    Returns (lower_bound, upper_bound) of the (1-alpha)*100% CI
    for the mean score, computed via percentile bootstrap.
    """
    import random
    n = len(scores)
    if n == 0:
        return (0.0, 0.0)
    boot_means = []
    for _ in range(n_bootstrap):
        sample = [scores[random.randint(0, n - 1)] for _ in range(n)]
        boot_means.append(sum(sample) / n)
    boot_means.sort()
    lo_idx = int((alpha / 2) * n_bootstrap)
    hi_idx = int((1 - alpha / 2) * n_bootstrap)
    return (boot_means[lo_idx], boot_means[hi_idx])
```

**Paired t-test function:**
```python
def paired_t_test(
    scores_a: list[float],
    scores_b: list[float],
) -> tuple[float, float]:
    """
    Computes a paired two-tailed t-test over per-example score differences.
    Returns (t_statistic, p_value).
    Paired because both variants run on the same examples.
    Falls back to (0.0, 1.0) if not enough samples.
    """
    if len(scores_a) != len(scores_b) or len(scores_a) < 2:
        return (0.0, 1.0)
    diffs = [a - b for a, b in zip(scores_a, scores_b)]
    n = len(diffs)
    mean_d = sum(diffs) / n
    variance = sum((d - mean_d) ** 2 for d in diffs) / (n - 1)
    if variance == 0:
        return (0.0, 1.0)
    import math
    se = math.sqrt(variance / n)
    t_stat = mean_d / se
    # Approximate p-value using normal distribution for large n,
    # or use scipy.stats.t.sf if scipy is available
    try:
        from scipy import stats
        p_value = 2 * stats.t.sf(abs(t_stat), df=n - 1)
    except ImportError:
        # Approximation: two-tailed normal CDF for n >= 30
        p_value = 2 * (1 - _normal_cdf(abs(t_stat)))
    return (t_stat, p_value)


def _normal_cdf(z: float) -> float:
    """Approximation of the standard normal CDF (Abramowitz & Stegun 26.2.17)."""
    import math
    t = 1 / (1 + 0.2316419 * abs(z))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z ** 2) * poly
```

**Early stopping function:**
```python
def early_stopping_recommendation(p_value: float, n_samples: int) -> str:
    """
    Recommends whether to stop early, continue, or declare a winner.
    Uses a conservative rule: require p < alpha/2 before n=30 to account
    for the increased false-positive rate of early peeking.
    """
    if n_samples < 10:
        return "CONTINUE — too few samples to draw any conclusion (need at least 10)."
    if n_samples < MIN_SAMPLES_FOR_POWER:
        threshold = ALPHA / 2  # conservative early-stop threshold
        if p_value < threshold:
            return (
                f"EARLY WIN possible — p={p_value:.4f} < {threshold} (conservative threshold). "
                f"Consider stopping if cost is a concern, but n={n_samples} is below the "
                f"recommended {MIN_SAMPLES_FOR_POWER} for full power."
            )
        return f"CONTINUE — only {n_samples} samples so far. Run to at least {MIN_SAMPLES_FOR_POWER}."
    if p_value < ALPHA:
        return f"SIGNIFICANT — p={p_value:.4f} < α={ALPHA}. Safe to declare a winner."
    if p_value < 0.10:
        return f"TRENDING — p={p_value:.4f} approaches significance. Gather more data."
    return f"NO DIFFERENCE — p={p_value:.4f}. Variants perform equivalently on this metric."
```

**Main runner:**
```python
async def run_ab_test() -> None:
    from datetime import datetime
    import os
    from langsmith import Client
    from langsmith.evaluation import aevaluate

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    project_a = f"ab-test-variant-a-{timestamp}"
    project_b = f"ab-test-variant-b-{timestamp}"

    client = Client()

    # Warn on small dataset
    dataset = client.read_dataset(dataset_name=DATASET_NAME)
    example_count = sum(1 for _ in client.list_examples(dataset_id=dataset.id))
    if example_count < MIN_SAMPLES_FOR_POWER:
        print(
            f"WARNING: Dataset has {example_count} examples. "
            f"Recommend at least {MIN_SAMPLES_FOR_POWER} for reliable results."
        )

    evaluators = [<list of selected evaluator functions>]

    print(f"Running Variant A in project: {project_a}")
    print(f"Running Variant B in project: {project_b}")
    print(f"Dataset: {DATASET_NAME} ({example_count} examples)")
    print()

    # Run both variants concurrently
    results_a, results_b = await asyncio.gather(
        aevaluate(
            variant_a,
            data=DATASET_NAME,
            evaluators=evaluators,
            experiment_prefix="variant-a",
            project_name=project_a,
            max_concurrency=4,
        ),
        aevaluate(
            variant_b,
            data=DATASET_NAME,
            evaluators=evaluators,
            experiment_prefix="variant-b",
            project_name=project_b,
            max_concurrency=4,
        ),
    )

    # Extract per-example scores for statistical tests
    df_a = results_a.to_pandas()
    df_b = results_b.to_pandas()

    print("=" * 60)
    print("A/B TEST RESULTS")
    print("=" * 60)
    print(f"Variant A experiment: {results_a.experiment_url}")
    print(f"Variant B experiment: {results_b.experiment_url}")
    print()

    for metric in [<metric key names as strings>]:
        col = f"feedback.{metric}"
        if col not in df_a.columns or col not in df_b.columns:
            print(f"[{metric}] — metric not found in results, skipping.")
            continue

        scores_a = df_a[col].dropna().tolist()
        scores_b = df_b[col].dropna().tolist()
        mean_a = sum(scores_a) / len(scores_a) if scores_a else 0.0
        mean_b = sum(scores_b) / len(scores_b) if scores_b else 0.0
        ci_a = bootstrap_ci(scores_a)
        ci_b = bootstrap_ci(scores_b)
        t_stat, p_value = paired_t_test(scores_a, scores_b)
        recommendation = early_stopping_recommendation(p_value, len(scores_a))

        print(f"Metric: {metric}")
        print(f"  Variant A: mean={mean_a:.3f}  95% CI=[{ci_a[0]:.3f}, {ci_a[1]:.3f}]")
        print(f"  Variant B: mean={mean_b:.3f}  95% CI=[{ci_b[0]:.3f}, {ci_b[1]:.3f}]")
        print(f"  Δ (A - B): {mean_a - mean_b:+.3f}")
        print(f"  Paired t-test: t={t_stat:.3f}, p={p_value:.4f}")
        print(f"  Recommendation: {recommendation}")
        print()

    print("View side-by-side in LangSmith:")
    print(f"  https://smith.langchain.com — open '{DATASET_NAME}' dataset → Experiments tab")
    print(f"  Select '{project_a}' and '{project_b}' to compare.")
```

**Dataset creation helper** (when `dataset_source == "from_runs"`):
```python
async def create_dataset_from_runs(
    project_name: str = "<project_name>",
    limit: int = <limit>,
    score_filter: float | None = None,  # e.g. 0.7 means only runs with score < 0.7
) -> str:
    """
    Creates a LangSmith dataset from recent runs in a project.
    Returns the dataset name.
    """
    from langsmith import Client
    client = Client()
    dataset_name = f"{project_name}-ab-test-{<timestamp>}"

    runs = list(client.list_runs(
        project_name=project_name,
        run_type="chain",
        limit=limit,
    ))
    if score_filter is not None:
        runs = [r for r in runs if r.feedback_stats and
                r.feedback_stats.get("score", {}).get("avg", 1.0) < score_filter]

    dataset = client.create_dataset(dataset_name, description=f"Auto-created from {project_name} runs")
    client.create_examples(
        inputs=[r.inputs for r in runs],
        outputs=[r.outputs for r in runs],
        dataset_id=dataset.id,
    )
    print(f"Created dataset '{dataset_name}' with {len(runs)} examples.")
    return dataset_name
```

---

### File 2: `ab_router.py`

Write a production routing function. Structure:

```python
"""
ab_router.py — Deterministic A/B routing for production traffic.

Routes a user_id to variant "a" or "b" using a hash-based assignment.
Assignment is:
  - Deterministic: same user_id always gets the same variant
  - Consistent: adding new tests does not re-assign existing users
  - Controllable: rollout_percentage controls the A/B split
  - Auditable: variant is logged to LangSmith via metadata

Usage:
    from ab_router import get_variant, route_request

    variant = get_variant(user_id="user-123", test_name="prompt-test-v2")
    # variant is "a" or "b"

    result = await route_request(
        user_id="user-123",
        test_name="prompt-test-v2",
        inputs={"question": "..."},
    )
"""

import hashlib
from typing import Literal

# ── Configuration ──────────────────────────────────────────────────────────────

# Percentage of traffic assigned to Variant B (0-100).
# Default 50 = even split. Set to 10 for a cautious 10% rollout.
ROLLOUT_PERCENTAGE: int = 50

# Name of this test — must match the test_name used in get_variant() calls.
# Change this when starting a new test to re-randomize assignments.
DEFAULT_TEST_NAME: str = "<test_type>-test-v1"


# ── Core routing function ──────────────────────────────────────────────────────

def get_variant(
    user_id: str,
    test_name: str = DEFAULT_TEST_NAME,
    rollout_percentage: int = ROLLOUT_PERCENTAGE,
) -> Literal["a", "b"]:
    """
    Returns "a" or "b" for the given user_id and test_name.

    Assignment is stable: the same (user_id, test_name) always returns
    the same variant. Changing test_name resets assignments for all users.

    Args:
        user_id: Any stable user identifier (UUID, email hash, etc.)
        test_name: Unique name for this A/B test. Acts as a namespace.
        rollout_percentage: 0-100. Percentage of users assigned to variant B.

    Returns:
        "a" or "b"
    """
    # Hash the combination of user_id and test_name for stable, namespaced assignment
    key = f"{test_name}:{user_id}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    # Take the first 8 hex chars → 32-bit integer → 0-99 bucket
    bucket = int(digest[:8], 16) % 100
    return "b" if bucket < rollout_percentage else "a"


def get_variant_metadata(
    user_id: str,
    test_name: str = DEFAULT_TEST_NAME,
) -> dict:
    """
    Returns a metadata dict suitable for passing to LangSmith via RunnableConfig.
    Attach this to every LLM call so you can filter by variant in LangSmith.
    """
    variant = get_variant(user_id=user_id, test_name=test_name)
    return {
        "ab_test_name": test_name,
        "ab_variant": variant,
        "user_id": user_id,
    }


# ── Production routing ──────────────────────────────────────────────────────────

async def route_request(
    user_id: str,
    inputs: dict,
    test_name: str = DEFAULT_TEST_NAME,
) -> dict:
    """
    Routes a real production request to variant A or B.
    Logs the variant assignment to LangSmith automatically.

    Replace the variant_a_chain / variant_b_chain references below
    with your actual production chains.
    """
    from langchain_core.runnables import RunnableConfig
    # Import your actual chains here:
    # from your_module import variant_a_chain, variant_b_chain

    variant = get_variant(user_id=user_id, test_name=test_name)
    metadata = get_variant_metadata(user_id=user_id, test_name=test_name)

    config = RunnableConfig(
        tags=["ab-test", f"variant-{variant}", test_name],
        metadata=metadata,
        run_name=f"ab-{variant}-{user_id[:8]}",
    )

    if variant == "a":
        # TODO: replace with your actual Variant A invocation
        # result = await variant_a_chain.ainvoke(inputs, config=config)
        raise NotImplementedError("Replace with variant_a_chain.ainvoke(inputs, config=config)")
    else:
        # TODO: replace with your actual Variant B invocation
        # result = await variant_b_chain.ainvoke(inputs, config=config)
        raise NotImplementedError("Replace with variant_b_chain.ainvoke(inputs, config=config)")

    return result  # noqa: F821


# ── Utilities ──────────────────────────────────────────────────────────────────

def assignment_stats(
    user_ids: list[str],
    test_name: str = DEFAULT_TEST_NAME,
) -> dict:
    """
    Given a list of user_ids, returns the actual A/B split.
    Use to verify the routing is balanced before launch.

    Example:
        stats = assignment_stats(["user-1", "user-2", ..., "user-1000"])
        print(stats)
        # {'a': 498, 'b': 502, 'a_pct': 49.8, 'b_pct': 50.2}
    """
    counts = {"a": 0, "b": 0}
    for uid in user_ids:
        counts[get_variant(uid, test_name)] += 1
    total = len(user_ids)
    return {
        "a": counts["a"],
        "b": counts["b"],
        "a_pct": round(100 * counts["a"] / total, 1) if total else 0,
        "b_pct": round(100 * counts["b"] / total, 1) if total else 0,
    }


if __name__ == "__main__":
    # Quick smoke test
    import sys

    test = DEFAULT_TEST_NAME
    sample_users = [f"user-{i}" for i in range(1000)]
    stats = assignment_stats(sample_users, test)
    print(f"Test: {test}")
    print(f"Split over 1000 users: A={stats['a_pct']}%  B={stats['b_pct']}%")

    # Verify stability
    uid = "user-42"
    v1 = get_variant(uid, test)
    v2 = get_variant(uid, test)
    assert v1 == v2, "Assignment is not stable!"
    print(f"user-42 always routes to variant: {v1}")
    print("Stability check passed.")
```

---

### File 3: `RESULTS_TEMPLATE.md`

Write a Markdown template the user fills in after running the test.

```markdown
# A/B Test Results: <test_type> — <short description>

**Test ID:** `<DEFAULT_TEST_NAME>`
**Date run:** YYYY-MM-DD
**Dataset:** `<DATASET_NAME>` (N examples)
**Significance level:** α = <ALPHA>

---

## Variants

| | Variant A | Variant B |
|---|---|---|
| Description | TODO: describe variant A | TODO: describe variant B |
| Model | TODO | TODO |
| Prompt / config | TODO: paste or link | TODO: paste or link |

---

## Results

Fill in after running `python ab_test.py`.

| Metric | Variant A mean | Variant B mean | Δ (A − B) | 95% CI on Δ | p-value | Significant? |
|---|---|---|---|---|---|---|
| TODO | — | — | — | — | — | — |

LangSmith experiment links:
- Variant A: TODO (paste URL from `ab_test.py` output)
- Variant B: TODO (paste URL from `ab_test.py` output)

---

## Statistical Interpretation

### How to read this table

- **Δ (A − B):** positive means A is better; negative means B is better.
- **95% CI on Δ:** if this interval does not cross zero, the difference is practically significant regardless of the p-value. If it does cross zero, the true effect could go either way.
- **p-value:** probability of seeing a difference this large if there were no real effect. Below α = <ALPHA> is statistically significant.
- **Both must agree:** a significant p-value with a CI that crosses zero is a signal of high variance, not a reliable winner. Collect more data.

### When to declare a winner

Declare a winner only when ALL of the following are true:

- [ ] p-value < α = <ALPHA> on the primary metric
- [ ] 95% CI on Δ does not cross zero
- [ ] n ≥ 30 examples (statistical power requirement)
- [ ] No external confounders (time-of-day, dataset drift, model version change)

### When to keep testing

Keep testing if:

- p-value is between <ALPHA> and 0.15 ("trending but not significant")
- CIs overlap substantially
- Sample size is below 30

### When to abandon the test

Abandon if:

- CIs overlap completely and n > 100 (true null result)
- Both variants score below your quality floor on all metrics (both are broken)
- Test conditions have changed (model update, dataset shift)

---

## Early Stopping Log

Use this section to track intermediate checks. Do not stop the test early unless the p-value is below α/2 = <ALPHA/2> (conservative threshold for early peeking).

| Date | n samples evaluated | Primary metric p-value | Recommendation | Action taken |
|---|---|---|---|---|
| TODO | | | | |

---

## Decision

**Winner:** Variant A / Variant B / No winner (keep current)

**Rationale:**
TODO: Write 2-3 sentences explaining the decision. Reference the specific metrics and CI bounds that drove the choice.

**Action:**
- [ ] Deploy winning variant to 100% of traffic
- [ ] Update `ab_router.py`: set `ROLLOUT_PERCENTAGE` to 0 (all A) or 100 (all B)
- [ ] Archive this document to `docs/ab-tests/`
- [ ] Tag the winning LangSmith experiment as `winner` in the UI
- [ ] Open a follow-up test to test the next hypothesis

---

## Lessons Learned

TODO: What did you learn from this test that should inform future experiments?

1.
2.
3.
```

---

## Step 7 — Post-Scaffold Summary

After writing all three files, print:

```
Files written:
  ab_test.py           — run with: python ab_test.py
  ab_router.py         — import get_variant() in your production app
  RESULTS_TEMPLATE.md  — fill in after the test completes

Before running:
  1. Confirm LANGSMITH_API_KEY and LANGSMITH_TRACING=true are set in .env
  2. <If dataset_source == "from_runs">: run create_dataset_from_runs() first
     (call it at the bottom of ab_test.py or in a separate script)
  3. <If dataset_source == "new">: complete the examples list in create_dataset()
  4. Install scipy for exact p-values (optional):
       pip install scipy

To run the test:
  python ab_test.py

To use the router in production:
  from ab_router import get_variant, route_request

  variant = get_variant(user_id=current_user.id)
  # Then invoke variant_a_chain or variant_b_chain accordingly.
  # See ab_router.py for the full route_request() async helper.

To interpret results:
  Open RESULTS_TEMPLATE.md and fill it in after python ab_test.py completes.
  Compare experiments side-by-side at:
    https://smith.langchain.com — open '<DATASET_NAME>' → Experiments tab.
```

---

## Output Rules

- Write all three files before printing the post-scaffold summary.
- Do not emit placeholders in generated Python — every function must be runnable with only the user's actual API keys set.
- `RESULTS_TEMPLATE.md` may contain `TODO` markers — these are intentional blanks for the user to fill in.
- Use `claude-haiku-4-5` for all LLM-as-judge evaluator calls (not `claude-sonnet-4-6`). The primary model under test is set by the user's variant definitions.
- If the user's test_type is `model` and they did not provide a shared prompt, use this pass-through default:
  ```
  You are a helpful assistant. Answer the following question: {question}
  ```
- Default project name for new datasets: `langchain-lab` (matches the plugin default).
- Do not write a `plugin.json` entry — the user adds this command to their existing manifest manually.
