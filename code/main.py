"""Message Notification Router -- entry point.

Reads dataset/messages.csv and writes output.csv with exactly one prediction
row per input message, in input order.

    python code/main.py                     # writes ./output.csv
    python code/main.py --out path.csv      # custom destination
    python code/main.py --dataset dataset   # custom dataset dir

Stdlib + numpy only. No network access and no secrets are required; if an
optional model backend is added later it must read its key from the
environment (never hardcoded, never logged).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schema import DATASET_DIR, OUTPUT_COLUMNS  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Route WhatsApp messages to notify/digest/mute.")
    p.add_argument("--dataset", default=DATASET_DIR,
                   help="dataset directory (default: %(default)s)")
    p.add_argument("--out", default="output.csv",
                   help="output CSV path (default: %(default)s)")
    p.add_argument("--quiet", action="store_true", help="suppress the summary")
    p.add_argument("--rules", action="store_true",
                   help="route with the declarative policy in rules.json using "
                        "the labels in label_store.json (the measured-best "
                        "path). Falls back to the gate stack if unavailable.")
    p.add_argument("--llm", action="store_true",
                   help="enable the local model layer (needs a llama.cpp "
                        "server at $LLM_BASE_URL; falls back to the cached "
                        "responses in code/llm_cache.json, then to rules)")
    return p


def run(dataset_dir: str, out_path: str, quiet: bool = False,
        use_llm: bool = False, use_rules: bool = False) -> int:
    import decide as decide_mod
    import features

    try:
        import retrieval
    except ImportError:
        retrieval = None

    # Without --llm the model layer is not disabled, it is frozen: cached
    # answers still replay, but nothing new is requested and no backend is
    # contacted. A keyless, offline run therefore reproduces the submitted
    # output.csv exactly rather than a degraded version of it.
    try:
        import llm
        llm.set_cache_only(not use_llm)
    except ImportError:
        pass

    # The declarative path is opt-in and degrades: if labels or policy are
    # missing the gate stack still produces a complete, valid output.csv.
    route_mod = extract_mod = None
    if use_rules:
        try:
            import extract as extract_mod
            import route as route_mod
        except (ImportError, OSError):
            route_mod = extract_mod = None

    ds = features.load_dataset(dataset_dir)

    rows = []
    for msg in ds.messages:
        feats = features.build_features(ds, msg)
        cands = retrieval.candidates(ds, msg, k=5) if retrieval else []
        if route_mod is not None:
            labels = extract_mod.labels_for(msg, feats, allow_model=use_llm)
            decision = route_mod.build_decision(msg, feats, labels, cands,
                                                use_llm=use_llm)
        else:
            decision = decide_mod.decide(ds, msg, feats, cands, use_llm=use_llm)
        decision = decide_mod.apply_quiet_hours(decision, msg, feats)

        # Evidence comes from the retrieval layer when it clears its similarity
        # floor. "none" is a legitimate answer and must be allowed through:
        # an earlier version only assigned the result when it was non-empty,
        # so a below-floor verdict silently fell back to the raw candidate
        # list and `none` was never emitted on any of the 110 rows. The floor
        # itself was never wrong -- at 0.10 it declines 8/110 (7.3%) against a
        # gold none-rate of 2/30 (6.7%).
        if retrieval and cands:
            decision.evidence_message_ids = retrieval.select_evidence(cands, limit=1)

        rows.append(decision.to_row())

    # Contract check before writing: exactly one row per input, input order.
    assert len(rows) == len(ds.messages), (
        f"row count {len(rows)} != {len(ds.messages)} input messages")
    assert [r["message_id"] for r in rows] == [m.message_id for m in ds.messages], \
        "output order does not match messages.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(OUTPUT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    if not quiet:
        import collections
        actions = collections.Counter(r["action"] for r in rows)
        types = collections.Counter(r["message_type"] for r in rows)
        with_ev = sum(1 for r in rows if r["evidence_message_ids"] != "none")
        print(f"wrote {out_path}: {len(rows)} rows")
        print("  action:", dict(actions))
        print("  type:  ", dict(types))
        print(f"  with evidence: {with_ev}/{len(rows)}")
        if use_rules:
            print("  policy:", "rules.json" if route_mod else "UNAVAILABLE, used gate stack")
        if retrieval is None:
            print("  note: retrieval unavailable; evidence from precedent only")
        if use_llm:
            try:
                import llm
                print("  llm:", llm.stats())
            except ImportError:
                print("  note: --llm requested but code/llm.py is unavailable")

    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.dataset, args.out, args.quiet, args.llm, args.rules)


if __name__ == "__main__":
    raise SystemExit(main())
