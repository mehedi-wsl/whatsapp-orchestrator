"""Score the labels+rules path against the 30 solved rows.

Reports the two modes separately so the model's contribution is visible:

  table  -- labels extracted by the model, rules evaluated in code
  judged -- labels extracted by the model, and the model also selects which
            non-guarded rule applies

Run:  python code/score_rules.py [--judged] [--limit N]
"""

from __future__ import annotations

import csv
import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract          # noqa: E402
import features         # noqa: E402
import route            # noqa: E402
import schema           # noqa: E402


def main(argv) -> int:
    judged = "--judged" in argv
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])

    ds = features.load_dataset("dataset")
    fields = [f.name for f in dataclasses.fields(schema.Message)]
    rows = list(csv.DictReader(open("dataset/sample_messages.csv")))
    if limit:
        rows = rows[:limit]

    ok_a = ok_t = 0
    misses = []
    for r in rows:
        msg = schema.Message(**{k: r.get(k, "") for k in fields})
        feats = features.build_features(ds, msg)
        labels = extract.labels_for(msg, feats, allow_model=True)
        out = route.route(labels, msg, feats, use_llm=judged)

        pred_a = out["action"]
        pred_t = out.get("message_type")
        if pred_t is None:
            _, t = __import__("engine").evaluate(labels)
            pred_t = t["type"]

        a_hit = pred_a == r["action"]
        t_hit = pred_t == r["message_type"]
        ok_a += a_hit
        ok_t += t_hit
        if not (a_hit and t_hit):
            misses.append((r["message_id"][-3:], r["action"], pred_a,
                           r["message_type"], pred_t, out["rule_id"],
                           out["decided_by"]))

    n = len(rows)
    mode = "judged (model selects rule)" if judged else "table (rules in code)"
    print(f"\nmode: {mode}   labels: {extract.coverage()}")
    print(f"action:       {ok_a}/{n} = {ok_a / n:6.1%}")
    print(f"message_type: {ok_t}/{n} = {ok_t / n:6.1%}")
    if misses:
        print(f"\n{'id':5} {'gold_a':7}{'pred_a':7} {'gold_t':16}{'pred_t':16} rule / by")
        for m in misses:
            print(f"{m[0]:5} {m[1]:7}{m[2]:7} {m[3]:16}{m[4]:16} {m[5]} / {m[6]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
