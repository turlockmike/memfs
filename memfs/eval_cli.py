#!/usr/bin/env python3
"""mem-eval — LongMemEval benchmark harness for memfs (Neo4j)."""

import argparse
import json
import os
import sys
from collections import defaultdict

from memfs.eval import (
    ingest_benchmark,
    compute_recall,
    generate_hypothesis,
    score_hypothesis,
)


def out(obj):
    print(json.dumps(obj))


def cmd_recall(args):
    print(f"Ingesting {args.benchmark}...", file=sys.stderr)
    count = ingest_benchmark(args.benchmark, args.root)
    print(f"Ingested {count} sessions", file=sys.stderr)

    with open(args.benchmark) as f:
        data = json.load(f)

    results = compute_recall(args.root, data, k=args.k)

    out({
        "recall_at_k": round(results["recall_at_k"], 4),
        "mrr": round(results["mrr"], 4),
        "precision_at_k": round(results["precision_at_k"], 4),
        "k": results["k"],
        "hits": results["total_hits"],
        "total": results["total_questions"],
    })

    by_type = defaultdict(lambda: {"hits": 0, "total": 0})
    for pq in results["per_question"]:
        qt = pq["question_type"]
        by_type[qt]["total"] += 1
        if pq["hit"]:
            by_type[qt]["hits"] += 1

    for qt, counts in sorted(by_type.items()):
        acc = counts["hits"] / counts["total"] if counts["total"] > 0 else 0
        out({"task": qt, "accuracy": round(acc, 4), "hits": counts["hits"], "total": counts["total"]})


def cmd_qa(args):
    print(f"Ingesting {args.benchmark}...", file=sys.stderr)
    count = ingest_benchmark(args.benchmark, args.root)
    print(f"Ingested {count} sessions", file=sys.stderr)

    with open(args.benchmark) as f:
        data = json.load(f)

    limit = args.limit or len(data)
    data = data[:limit]

    with open(args.output, "w") as out_f:
        for i, entry in enumerate(data):
            print(f"[{i+1}/{len(data)}] {entry['question_id']}: {entry['question'][:60]}...",
                  file=sys.stderr)
            try:
                result = generate_hypothesis(
                    args.root, entry, k=5,
                    model=args.model, backend=args.backend,
                )
                out_f.write(json.dumps(result) + "\n")
                out_f.flush()
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                out_f.write(json.dumps({
                    "question_id": entry["question_id"],
                    "hypothesis": f"ERROR: {e}",
                }) + "\n")

    print(f"Wrote {len(data)} hypotheses to {args.output}", file=sys.stderr)


def cmd_score(args):
    with open(args.hypotheses) as f:
        hypotheses = {json.loads(line)["question_id"]: json.loads(line)
                      for line in f if line.strip()}

    with open(args.benchmark) as f:
        benchmark = {e["question_id"]: e for e in json.load(f)}

    correct = 0
    total = 0
    by_type = defaultdict(lambda: {"correct": 0, "total": 0})

    for qid, hyp in hypotheses.items():
        if qid not in benchmark:
            continue
        entry = benchmark[qid]
        hypothesis = hyp["hypothesis"]
        if hypothesis.startswith("ERROR:"):
            continue

        total += 1
        try:
            is_correct = score_hypothesis(
                question=entry["question"],
                answer=entry["answer"],
                hypothesis=hypothesis,
                question_type=entry.get("question_type", ""),
                model=args.model,
                backend=args.backend,
            )
        except Exception as e:
            print(f"  Score error for {qid}: {e}", file=sys.stderr)
            is_correct = False

        qt = entry.get("question_type", "unknown")
        by_type[qt]["total"] += 1
        if is_correct:
            correct += 1
            by_type[qt]["correct"] += 1

        out({
            "question_id": qid,
            "correct": is_correct,
            "question_type": qt,
        })

    accuracy = correct / total if total > 0 else 0
    print(f"\nOverall accuracy: {accuracy:.4f} ({correct}/{total})", file=sys.stderr)
    for qt, counts in sorted(by_type.items()):
        acc = counts["correct"] / counts["total"] if counts["total"] > 0 else 0
        print(f"  {qt}: {acc:.4f} ({counts['correct']}/{counts['total']})", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(prog="mem-eval", description="LongMemEval benchmark harness")
    sub = parser.add_subparsers(dest="command")

    p_recall = sub.add_parser("recall", help="Compute retrieval metrics (Recall@k, MRR)")
    p_recall.add_argument("benchmark", help="Path to LongMemEval JSON")
    p_recall.add_argument("--root", required=True, help="Eval root directory")
    p_recall.add_argument("--k", type=int, default=5, help="Top-k for recall")

    p_qa = sub.add_parser("qa", help="Generate answer hypotheses")
    p_qa.add_argument("benchmark", help="Path to LongMemEval JSON")
    p_qa.add_argument("--root", required=True, help="Eval root directory")
    p_qa.add_argument("--output", required=True, help="Output JSONL file")
    p_qa.add_argument("--limit", type=int, help="Limit number of questions")
    p_qa.add_argument("--backend", default="claude", choices=["claude", "ollama"])
    p_qa.add_argument("--model", default="sonnet")

    p_score = sub.add_parser("score", help="Score hypotheses against ground truth")
    p_score.add_argument("hypotheses", help="Hypotheses JSONL file")
    p_score.add_argument("benchmark", help="Path to LongMemEval JSON")
    p_score.add_argument("--backend", default="claude", choices=["claude", "ollama"])
    p_score.add_argument("--model", default="sonnet")

    # M3 hooks
    p_tcca = sub.add_parser("tcca", help="Run TCCA-instrumented evaluation")
    p_tcca.add_argument("benchmark", help="Path to LongMemEval JSON")
    p_tcca.add_argument("--root", required=True)
    p_tcca.add_argument("--adapter", default="memfs",
                        choices=["memfs", "accumulate", "bm25"])
    p_tcca.add_argument("--output", required=True)
    p_tcca.add_argument("--limit", type=int)
    p_tcca.add_argument("--backend", default="claude", choices=["claude", "ollama"])
    p_tcca.add_argument("--model", default="sonnet")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "tcca":
        from memfs.tcca import cmd_tcca
        cmd_tcca(args)
    else:
        {"recall": cmd_recall, "qa": cmd_qa, "score": cmd_score}[args.command](args)


if __name__ == "__main__":
    main()
