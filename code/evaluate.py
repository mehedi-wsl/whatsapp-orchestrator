"""Backtest harness -- the measurement instrument for the router.

Scores the pipeline against the 30 solved rows in dataset/sample_messages.csv
on four independent axes:

    1. action accuracy            (+ 3x3 confusion matrix)
    2. message_type accuracy      (+ per-class breakdown over 11 classes)
    3. evidence top-1 accuracy    (+ recall@3 / recall@5 when the retrieval
                                     layer exposes ranked candidates)
    4. confidence calibration     (mean predicted vs mean gold per action,
                                     and band compliance)

Design rules that must not be relaxed:

  * The pipeline modules (features / retrieval / decide) are imported LAZILY.
    A missing module makes the axes that depend on it read as NOT AVAILABLE.
  * Nothing here ever substitutes fake logic for a missing module. A number
    that could not be computed is printed as "n/a", never as a score.
  * A row that raises is counted as WRONG (denominator stays at 30) and is
    listed in the errors section. Failures can only ever hurt the score.

Stdlib only (numpy permitted but unused). Deterministic output.

    python code/evaluate.py
    python code/evaluate.py compare old_output.csv new_output.csv
"""

from __future__ import annotations

import csv
import dataclasses
import os
import sys
import traceback
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:  # so `import schema` / `import decide` work either way
    sys.path.insert(0, _HERE)

USE_LLM = False  # set by backtest(); the model layer is opt-in

from schema import (  # noqa: E402  (path juggling above is deliberate)
    ACTIONS,
    CONFIDENCE_BANDS,
    DATASET_DIR,
    MESSAGE_TYPES,
    OUTPUT_COLUMNS,
    Message,
)

MESSAGE_FIELDS: Tuple[str, ...] = tuple(f.name for f in dataclasses.fields(Message))

SAMPLE_FILE = "sample_messages.csv"
RECALL_KS = (3, 5)

# csv fields can hold long free text with embedded newlines
csv.field_size_limit(10 ** 7)


# --------------------------------------------------------------------------
# lazy pipeline imports
# --------------------------------------------------------------------------

_MODULE_CACHE: Dict[str, Tuple[Optional[object], Optional[str]]] = {}


def _lazy(name: str) -> Tuple[Optional[object], Optional[str]]:
    """Import a pipeline module. Returns (module, error_message).

    Never raises: a module that does not exist yet is a reported gap, not a
    crash. The error message is kept verbatim so a typo inside an existing
    module is not mistaken for an absent module.
    """
    if name in _MODULE_CACHE:
        return _MODULE_CACHE[name]
    try:
        module = __import__(name)
        result: Tuple[Optional[object], Optional[str]] = (module, None)
    except ImportError as exc:
        result = (None, str(exc))
    except Exception as exc:  # a module that exists but explodes at import
        result = (None, "{}: {}".format(type(exc).__name__, exc))
    _MODULE_CACHE[name] = result
    return result


def _has(module: Optional[object], attr: str) -> bool:
    return module is not None and callable(getattr(module, attr, None))


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def resolve_dataset_dir(dataset_dir: str) -> str:
    """Accept a dataset path relative to cwd or to the repo root."""
    if os.path.isdir(dataset_dir):
        return dataset_dir
    repo_root = os.path.dirname(_HERE)
    alt = os.path.join(repo_root, dataset_dir)
    if os.path.isdir(alt):
        return alt
    return dataset_dir


def _read_csv(path: str) -> List[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def evidence_list(raw: Optional[str]) -> List[str]:
    """Parse an evidence_message_ids cell. 'none'/'' -> []."""
    text = (raw or "").strip()
    if not text or text.lower() == "none":
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _to_float(raw: Optional[str]) -> Optional[float]:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _pct(num: int, den: int) -> Optional[float]:
    return (num / den) if den else None


def _fmt_pct(value: Optional[float]) -> str:
    return "  n/a" if value is None else "{:5.1f}%".format(100.0 * value)


def _fmt_num(value: Optional[float], places: int = 3) -> str:
    return "n/a" if value is None else "{:.{p}f}".format(value, p=places)


def _short(message_id: str) -> str:
    """message_0013 -> 0013, sample_msg_007 -> 007. Keeps the table narrow."""
    return message_id.rsplit("_", 1)[-1] if "_" in message_id else message_id


def _short_evidence(ids: Sequence[str]) -> str:
    return ";".join(_short(i) for i in ids) if ids else "none"


# --------------------------------------------------------------------------
# gold loading
# --------------------------------------------------------------------------

def load_gold(dataset_dir: str = DATASET_DIR) -> List[dict]:
    """Read the 30 solved rows. Each carries Message inputs + gold labels."""
    return _read_csv(os.path.join(resolve_dataset_dir(dataset_dir), SAMPLE_FILE))


def message_from_row(row: dict) -> Message:
    """Build a Message from a sample row, ignoring the gold label columns."""
    return Message(**{name: (row.get(name) or "") for name in MESSAGE_FIELDS})


def gold_summary(gold: List[dict]) -> dict:
    """Label distribution of the solved rows -- useful even with no pipeline."""
    actions = {a: 0 for a in ACTIONS}
    types = {t: 0 for t in MESSAGE_TYPES}
    conf_by_action: Dict[str, List[float]] = {a: [] for a in ACTIONS}
    n_none = 0
    n_multi = 0
    for row in gold:
        action = row.get("action", "")
        actions[action] = actions.get(action, 0) + 1
        mtype = row.get("message_type", "")
        types[mtype] = types.get(mtype, 0) + 1
        conf = _to_float(row.get("confidence"))
        if conf is not None and action in conf_by_action:
            conf_by_action[action].append(conf)
        ids = evidence_list(row.get("evidence_message_ids"))
        if not ids:
            n_none += 1
        elif len(ids) > 1:
            n_multi += 1
    return {
        "n": len(gold),
        "actions": actions,
        "message_types": types,
        "evidence_none_rows": n_none,
        "evidence_multi_rows": n_multi,
        "confidence_range_by_action": {
            a: (min(v), max(v)) if v else None for a, v in conf_by_action.items()
        },
    }


# --------------------------------------------------------------------------
# running the pipeline over the solved rows
# --------------------------------------------------------------------------

def _run_pipeline(dataset_dir: str, gold: List[dict]) -> Tuple[List[dict], dict]:
    """Predict for every solved row. Returns (row_records, availability).

    row_records mirror the gold list one-for-one and in the same order. A row
    whose prediction failed keeps pred_* = None and carries an `error`.
    """
    features, features_err = _lazy("features")
    retrieval, retrieval_err = _lazy("retrieval")
    decide_mod, decide_err = _lazy("decide")

    avail = {
        "features": _has(features, "load_dataset") and _has(features, "build_features"),
        "retrieval": _has(retrieval, "candidates"),
        "decide": _has(decide_mod, "decide"),
        "notes": [],
        "dataset_loaded": False,
    }
    if not avail["features"]:
        avail["notes"].append(
            "features: {}".format(features_err or "load_dataset/build_features missing")
        )
    if not avail["retrieval"]:
        avail["notes"].append(
            "retrieval: {}".format(retrieval_err or "candidates() missing")
        )
    if not avail["decide"]:
        avail["notes"].append("decide: {}".format(decide_err or "decide() missing"))

    records = [
        {
            "message_id": row.get("message_id", ""),
            "gold_action": row.get("action", ""),
            "gold_type": row.get("message_type", ""),
            "gold_evidence": evidence_list(row.get("evidence_message_ids")),
            "gold_confidence": _to_float(row.get("confidence")),
            "pred_action": None,
            "pred_type": None,
            "pred_evidence": None,     # None = not predicted; [] = predicted "none"
            "pred_confidence": None,   # as it would be written to output.csv
            "pred_confidence_raw": None,
            "ranked_candidates": None,
            "error": None,
        }
        for row in gold
    ]

    if not avail["features"]:
        avail["notes"].append(
            "pipeline not run: features.load_dataset() is required to build the Dataset"
        )
        return records, avail

    try:
        ds = features.load_dataset(dataset_dir)  # type: ignore[union-attr]
        avail["dataset_loaded"] = True
    except Exception as exc:
        avail["notes"].append(
            "pipeline not run: features.load_dataset() raised {}: {}".format(
                type(exc).__name__, exc
            )
        )
        avail["features"] = False
        return records, avail

    if avail["decide"] and not avail["retrieval"]:
        avail["notes"].append(
            "DEGRADED: decide() ran with an empty candidate list "
            "(retrieval unavailable) -- scores below are a floor, not a score"
        )

    for row, rec in zip(gold, records):
        try:
            msg = message_from_row(row)
            feats = features.build_features(ds, msg)  # type: ignore[union-attr]

            cands: List[object] = []
            if avail["retrieval"]:
                cands = list(retrieval.candidates(ds, msg, k=max(RECALL_KS)))  # type: ignore[union-attr]
                ranked: List[str] = []
                for cand in cands:
                    cid = getattr(getattr(cand, "message", None), "message_id", None)
                    if cid and cid not in ranked:
                        ranked.append(cid)
                rec["ranked_candidates"] = ranked

            if avail["decide"]:
                decision = decide_mod.decide(ds, msg, feats, cands,  # type: ignore[union-attr]
                                             use_llm=USE_LLM)
                decision = decide_mod.apply_quiet_hours(decision, msg, feats)
                rec["pred_confidence_raw"] = _to_float(getattr(decision, "confidence", None))
                out = decision.to_row()  # validates against the output contract
                rec["pred_action"] = out["action"]
                rec["pred_type"] = out["message_type"]
                rec["pred_evidence"] = evidence_list(out["evidence_message_ids"])
                rec["pred_confidence"] = _to_float(out["confidence"])
            elif avail["retrieval"] and _has(retrieval, "select_evidence"):
                rec["pred_evidence"] = list(retrieval.select_evidence(cands, limit=1))  # type: ignore[union-attr]
        except Exception:
            rec["error"] = traceback.format_exc(limit=3).strip().splitlines()[-1]

    return records, avail


# --------------------------------------------------------------------------
# axis 1 -- action
# --------------------------------------------------------------------------

def score_action(records: List[dict]) -> Optional[dict]:
    predicted = [r for r in records if r["pred_action"] is not None]
    if not predicted:
        return None
    n = len(records)  # errors stay in the denominator
    confusion = {g: {p: 0 for p in ACTIONS} for g in ACTIONS}
    for g in ACTIONS:
        confusion[g]["(none)"] = 0
    correct = 0
    for rec in records:
        gold, pred = rec["gold_action"], rec["pred_action"]
        if gold not in confusion:
            confusion[gold] = {p: 0 for p in ACTIONS}
            confusion[gold]["(none)"] = 0
        key = pred if pred in ACTIONS else "(none)"
        confusion[gold][key] += 1
        if pred is not None and pred == gold:
            correct += 1
    per_class = {}
    for action in ACTIONS:
        gold_n = sum(1 for r in records if r["gold_action"] == action)
        pred_n = sum(1 for r in records if r["pred_action"] == action)
        hit = sum(
            1 for r in records if r["gold_action"] == action and r["pred_action"] == action
        )
        per_class[action] = {
            "gold_n": gold_n,
            "pred_n": pred_n,
            "correct": hit,
            "recall": _pct(hit, gold_n),
            "precision": _pct(hit, pred_n),
        }
    return {
        "n": n,
        "n_predicted": len(predicted),
        "correct": correct,
        "accuracy": _pct(correct, n),
        "confusion": confusion,
        "per_class": per_class,
    }


# --------------------------------------------------------------------------
# axis 2 -- message_type
# --------------------------------------------------------------------------

def score_message_type(records: List[dict]) -> Optional[dict]:
    predicted = [r for r in records if r["pred_type"] is not None]
    if not predicted:
        return None
    n = len(records)
    correct = sum(
        1 for r in records if r["pred_type"] is not None and r["pred_type"] == r["gold_type"]
    )
    classes = list(MESSAGE_TYPES)
    for rec in records:  # anything off-contract must still surface
        for value in (rec["gold_type"], rec["pred_type"]):
            if value and value not in classes:
                classes.append(value)
    per_class = {}
    for cls in classes:
        gold_n = sum(1 for r in records if r["gold_type"] == cls)
        pred_n = sum(1 for r in records if r["pred_type"] == cls)
        hit = sum(1 for r in records if r["gold_type"] == cls and r["pred_type"] == cls)
        per_class[cls] = {
            "gold_n": gold_n,
            "pred_n": pred_n,
            "correct": hit,
            "recall": _pct(hit, gold_n),
            "precision": _pct(hit, pred_n),
        }
    confusions: Dict[str, int] = {}
    for rec in records:
        if rec["pred_type"] is not None and rec["pred_type"] != rec["gold_type"]:
            key = "{} -> {}".format(rec["gold_type"], rec["pred_type"])
            confusions[key] = confusions.get(key, 0) + 1
    return {
        "n": n,
        "n_predicted": len(predicted),
        "correct": correct,
        "accuracy": _pct(correct, n),
        "per_class": per_class,
        "classes_in_order": classes,
        "top_confusions": sorted(confusions.items(), key=lambda kv: (-kv[1], kv[0])),
    }


# --------------------------------------------------------------------------
# axis 3 -- evidence
# --------------------------------------------------------------------------

def score_evidence(records: List[dict]) -> Optional[dict]:
    predicted = [r for r in records if r["pred_evidence"] is not None]
    if not predicted:
        return None

    n = len(records)
    with_gold = [r for r in records if r["gold_evidence"]]
    none_rows = [r for r in records if not r["gold_evidence"]]

    top1 = 0
    top1_with_gold = 0
    none_correct = 0
    for rec in records:
        gold, pred = rec["gold_evidence"], rec["pred_evidence"]
        if pred is None:
            continue
        if not gold:
            if not pred:  # predicting "none" where gold is "none" is CORRECT
                top1 += 1
                none_correct += 1
        elif pred and pred[0] == gold[0]:
            top1 += 1
            top1_with_gold += 1

    result = {
        "n": n,
        "n_predicted": len(predicted),
        "top1_correct": top1,
        "top1_accuracy": _pct(top1, n),
        "n_with_gold_evidence": len(with_gold),
        "top1_correct_with_gold": top1_with_gold,
        "top1_accuracy_with_gold": _pct(top1_with_gold, len(with_gold)),
        "n_gold_none": len(none_rows),
        "gold_none_correct": none_correct,
        "recall_at": {},
        "ranked_available": False,
    }

    ranked_rows = [r for r in with_gold if r["ranked_candidates"] is not None]
    if ranked_rows:
        result["ranked_available"] = True
        result["n_ranked_rows"] = len(ranked_rows)
        for k in RECALL_KS:
            hits = sum(
                1 for r in ranked_rows if r["gold_evidence"][0] in r["ranked_candidates"][:k]
            )
            result["recall_at"][k] = {
                "hits": hits,
                "n": len(ranked_rows),
                "value": _pct(hits, len(ranked_rows)),
            }
    return result


# --------------------------------------------------------------------------
# axis 4 -- confidence calibration
# --------------------------------------------------------------------------

def score_confidence(records: List[dict]) -> Optional[dict]:
    predicted = [r for r in records if r["pred_confidence"] is not None]
    if not predicted:
        return None

    per_action = {}
    for action in ACTIONS:
        rows = [r for r in records if r["gold_action"] == action]
        preds = [r["pred_confidence"] for r in rows if r["pred_confidence"] is not None]
        per_action[action] = {
            "n": len(rows),
            "n_predicted": len(preds),
            "mean_gold": _mean([r["gold_confidence"] for r in rows]),
            "mean_pred": _mean(preds),
            "band": CONFIDENCE_BANDS[action],
        }

    in_band = 0
    raw_in_band = 0
    raw_n = 0
    for rec in predicted:
        band = CONFIDENCE_BANDS.get(rec["pred_action"] or "")
        if not band:
            continue
        lo, hi = band
        if lo - 1e-9 <= rec["pred_confidence"] <= hi + 1e-9:
            in_band += 1
        raw = rec["pred_confidence_raw"]
        if raw is not None:
            raw_n += 1
            if lo - 1e-9 <= raw <= hi + 1e-9:
                raw_in_band += 1

    errors = [
        abs(r["pred_confidence"] - r["gold_confidence"])
        for r in predicted
        if r["gold_confidence"] is not None
    ]
    return {
        "n_predicted": len(predicted),
        "per_gold_action": per_action,
        "in_band_written": in_band,
        "in_band_written_rate": _pct(in_band, len(predicted)),
        "in_band_raw": raw_in_band,
        "in_band_raw_n": raw_n,
        "in_band_raw_rate": _pct(raw_in_band, raw_n),
        "mean_abs_error": _mean(errors),
    }


# --------------------------------------------------------------------------
# report printing
# --------------------------------------------------------------------------

def _rule(char: str = "-", width: int = 84) -> str:
    return char * width


def _print_header(title: str) -> None:
    print("")
    print(_rule("="))
    print(title)
    print(_rule("="))


def _print_diff_table(records: List[dict]) -> None:
    _print_header("PER-ROW DIFF  (ids abbreviated: 001 = sample_msg_001 / message_0001)")
    # group banner, aligned to the exact column offsets of the row format below
    print(" " * 8 + "{:^15}".format("[ action ]") + "  "
          + "{:^33}".format("[ message_type ]") + "  "
          + "{:^25}".format("[ evidence ]"))
    print(
        "{:1} {:>4}  {:<6} {:<6} {:1}  {:<15} {:<15} {:1}  {:<11} {:<11} {:1}".format(
            "!", "ID", "gold", "pred", "", "gold", "pred", "", "gold", "pred", "",
        ).rstrip()
    )
    print(_rule())
    for rec in records:
        pa = rec["pred_action"]
        pt = rec["pred_type"]
        pe = rec["pred_evidence"]
        ge = rec["gold_evidence"]

        a_bad = pa is not None and pa != rec["gold_action"]
        t_bad = pt is not None and pt != rec["gold_type"]
        e_bad = pe is not None and (pe[:1] or [None]) != (ge[:1] or [None])

        flag = "!" if (a_bad or t_bad or e_bad or rec["error"]) else " "
        print(
            "{:1} {:>4}  {:<6} {:<6} {:1}  {:<15} {:<15} {:1}  {:<11} {:<11} {:1}".format(
                flag,
                _short(rec["message_id"]),
                rec["gold_action"],
                pa if pa is not None else "-",
                "X" if a_bad else " ",
                rec["gold_type"],
                pt if pt is not None else "-",
                "X" if t_bad else " ",
                _short_evidence(ge),
                _short_evidence(pe) if pe is not None else "-",
                "X" if e_bad else " ",
            )
        )
    print(_rule())
    print("legend: '!' row has at least one mismatch, 'X' marks the axis, "
          "'-' not predicted (module unavailable)")


def _print_action(axis: Optional[dict]) -> None:
    _print_header("AXIS 1 -- ACTION")
    if axis is None:
        print("NOT AVAILABLE: no action predictions (decide module unavailable)")
        return
    print("accuracy: {}/{} = {}".format(
        axis["correct"], axis["n"], _fmt_pct(axis["accuracy"])))
    print("")
    print("confusion matrix (rows = gold, cols = predicted)")
    header = "{:<10}".format("gold\\pred") + "".join("{:>9}".format(a) for a in ACTIONS)
    header += "{:>9}".format("(none)")
    print(header)
    for g in ACTIONS:
        line = "{:<10}".format(g)
        for p in ACTIONS:
            line += "{:>9}".format(axis["confusion"][g][p])
        line += "{:>9}".format(axis["confusion"][g].get("(none)", 0))
        print(line)
    print("")
    print("{:<10}{:>7}{:>7}{:>9}{:>10}{:>11}".format(
        "action", "gold", "pred", "correct", "recall", "precision"))
    for a in ACTIONS:
        c = axis["per_class"][a]
        print("{:<10}{:>7}{:>7}{:>9}{:>10}{:>11}".format(
            a, c["gold_n"], c["pred_n"], c["correct"],
            _fmt_pct(c["recall"]), _fmt_pct(c["precision"])))


def _print_message_type(axis: Optional[dict]) -> None:
    _print_header("AXIS 2 -- MESSAGE_TYPE  (11 classes)")
    if axis is None:
        print("NOT AVAILABLE: no message_type predictions (decide module unavailable)")
        return
    print("accuracy: {}/{} = {}".format(
        axis["correct"], axis["n"], _fmt_pct(axis["accuracy"])))
    print("")
    print("{:<17}{:>7}{:>7}{:>9}{:>10}{:>11}".format(
        "message_type", "gold", "pred", "correct", "recall", "precision"))
    for cls in axis["classes_in_order"]:
        c = axis["per_class"][cls]
        if c["gold_n"] == 0 and c["pred_n"] == 0:
            print("{:<17}{:>7}{:>7}{:>9}{:>10}{:>11}".format(cls, 0, 0, 0, "    -", "     -"))
            continue
        print("{:<17}{:>7}{:>7}{:>9}{:>10}{:>11}".format(
            cls, c["gold_n"], c["pred_n"], c["correct"],
            _fmt_pct(c["recall"]), _fmt_pct(c["precision"])))
    if axis["top_confusions"]:
        print("")
        print("most frequent type confusions (gold -> predicted):")
        for key, count in axis["top_confusions"][:8]:
            print("  {:<34} {}".format(key, count))


def _print_evidence(axis: Optional[dict]) -> None:
    _print_header("AXIS 3 -- EVIDENCE")
    if axis is None:
        print("NOT AVAILABLE: no evidence predictions "
              "(decide and retrieval modules unavailable)")
        return
    print("top-1 accuracy (all rows):        {}/{} = {}".format(
        axis["top1_correct"], axis["n"], _fmt_pct(axis["top1_accuracy"])))
    print("top-1 accuracy (rows with gold):  {}/{} = {}".format(
        axis["top1_correct_with_gold"], axis["n_with_gold_evidence"],
        _fmt_pct(axis["top1_accuracy_with_gold"])))
    print("gold 'none' rows handled right:   {}/{}".format(
        axis["gold_none_correct"], axis["n_gold_none"]))
    if axis["ranked_available"]:
        for k in RECALL_KS:
            r = axis["recall_at"][k]
            print("recall@{}:                         {}/{} = {}".format(
                k, r["hits"], r["n"], _fmt_pct(r["value"])))
    else:
        print("recall@3 / recall@5:              n/a (retrieval candidates unavailable)")


def _print_confidence(axis: Optional[dict], gold_stats: dict) -> None:
    _print_header("AXIS 4 -- CONFIDENCE CALIBRATION")
    if axis is None:
        print("NOT AVAILABLE: no confidence predictions (decide module unavailable)")
        print("")
        print("gold confidence range per action (target bands):")
        for a in ACTIONS:
            rng = gold_stats["confidence_range_by_action"].get(a)
            lo, hi = CONFIDENCE_BANDS[a]
            print("  {:<8} gold {:<14} band {:.2f}-{:.2f}".format(
                a,
                "n/a" if rng is None else "{:.2f}-{:.2f}".format(*rng),
                lo, hi))
        return
    print("{:<10}{:>5}{:>12}{:>12}{:>10}{:>14}".format(
        "gold act", "n", "mean gold", "mean pred", "delta", "band"))
    for a in ACTIONS:
        c = axis["per_gold_action"][a]
        delta = (
            None if (c["mean_pred"] is None or c["mean_gold"] is None)
            else c["mean_pred"] - c["mean_gold"]
        )
        print("{:<10}{:>5}{:>12}{:>12}{:>10}{:>14}".format(
            a, c["n"], _fmt_num(c["mean_gold"], 3), _fmt_num(c["mean_pred"], 3),
            _fmt_num(delta, 3) if delta is not None else "n/a",
            "{:.2f}-{:.2f}".format(*c["band"])))
    print("")
    print("mean |pred - gold| confidence:        {}".format(
        _fmt_num(axis["mean_abs_error"], 3)))
    print("written conf inside predicted band:   {}/{} = {}".format(
        axis["in_band_written"], axis["n_predicted"],
        _fmt_pct(axis["in_band_written_rate"])))
    if axis["in_band_raw_n"]:
        print("RAW conf inside band before clamping: {}/{} = {}".format(
            axis["in_band_raw"], axis["in_band_raw_n"],
            _fmt_pct(axis["in_band_raw_rate"])))
        print("  (Decision.to_row() clamps into band, so the written rate is not")
        print("   evidence of calibration -- the RAW rate is the honest number.)")


def _print_gold_summary(stats: dict) -> None:
    _print_header("GOLD LABEL SUMMARY  (dataset/sample_messages.csv)")
    print("solved rows: {}".format(stats["n"]))
    print("actions:       " + "  ".join(
        "{}={}".format(a, stats["actions"].get(a, 0)) for a in ACTIONS))
    print("message_types: " + "  ".join(
        "{}={}".format(t, stats["message_types"].get(t, 0)) for t in MESSAGE_TYPES))
    print("evidence: {} rows are 'none', {} rows carry multiple ids".format(
        stats["evidence_none_rows"], stats["evidence_multi_rows"]))


def print_report(result: dict) -> None:
    _print_header("BACKTEST -- {} solved rows".format(result["n_samples"]))
    avail = result["availability"]
    print("modules: features={}  retrieval={}  decide={}".format(
        "ok" if avail["features"] else "MISSING",
        "ok" if avail["retrieval"] else "MISSING",
        "ok" if avail["decide"] else "MISSING"))
    for note in avail["notes"]:
        print("  not available yet: {}".format(note))
    if result["errors"]:
        print("  {} row(s) raised during prediction and are counted as WRONG".format(
            len(result["errors"])))

    _print_gold_summary(result["gold_summary"])
    _print_diff_table(result["rows"])
    _print_action(result["action"])
    _print_message_type(result["message_type"])
    _print_evidence(result["evidence"])
    _print_confidence(result["confidence"], result["gold_summary"])

    if result["errors"]:
        _print_header("ROW ERRORS")
        for message_id, err in result["errors"]:
            print("  {}: {}".format(message_id, err))

    _print_header("HEADLINE")
    degraded = [n for n in avail["notes"] if n.startswith("DEGRADED")]
    for note in degraded:
        print("*** {} ***".format(note))
    axes = [
        ("action accuracy      ", result["action"], "accuracy"),
        ("message_type accuracy", result["message_type"], "accuracy"),
        ("evidence top-1       ", result["evidence"], "top1_accuracy"),
    ]
    for label, axis, key in axes:
        if axis is None:
            print("{}: NOT MEASURED".format(label))
        else:
            print("{}: {}".format(label, _fmt_pct(axis[key])))
    if result["confidence"] is None:
        print("confidence calibration: NOT MEASURED")
    else:
        print("confidence calibration: mean |err| = {}".format(
            _fmt_num(result["confidence"]["mean_abs_error"], 3)))
    print("")


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def backtest(dataset_dir: str = DATASET_DIR, verbose: bool = True,
             use_llm: bool = False) -> dict:
    global USE_LLM
    USE_LLM = use_llm
    """Score the pipeline against the solved rows on all four axes.

    Axes whose modules are missing are returned as None and printed as
    NOT AVAILABLE. Nothing is ever substituted for a missing module.
    """
    resolved = resolve_dataset_dir(dataset_dir)
    gold = load_gold(resolved)
    records, availability = _run_pipeline(resolved, gold)

    result = {
        "dataset_dir": resolved,
        "n_samples": len(gold),
        "availability": availability,
        "gold_summary": gold_summary(gold),
        "action": score_action(records),
        "message_type": score_message_type(records),
        "evidence": score_evidence(records),
        "confidence": score_confidence(records),
        "rows": records,
        "errors": [(r["message_id"], r["error"]) for r in records if r["error"]],
    }
    if verbose:
        print_report(result)
    return result


# --------------------------------------------------------------------------
# output.csv A/B diff
# --------------------------------------------------------------------------

def compare_outputs(path_a: str, path_b: str, verbose: bool = True) -> dict:
    """Diff two output.csv files row by row and summarise what changed."""
    rows_a = _read_csv(path_a)
    rows_b = _read_csv(path_b)
    index_a = {r.get("message_id", ""): r for r in rows_a}
    index_b = {r.get("message_id", ""): r for r in rows_b}

    only_a = sorted(set(index_a) - set(index_b))
    only_b = sorted(set(index_b) - set(index_a))
    common = [r.get("message_id", "") for r in rows_a if r.get("message_id", "") in index_b]

    action_changes: Dict[str, int] = {}
    type_changes: Dict[str, int] = {}
    changed_rows: List[dict] = []
    n_action = n_type = n_evidence = n_identical = 0
    conf_deltas: List[float] = []

    for mid in common:
        a, b = index_a[mid], index_b[mid]
        aa, ba = a.get("action", ""), b.get("action", "")
        at, bt = a.get("message_type", ""), b.get("message_type", "")
        ae = evidence_list(a.get("evidence_message_ids"))
        be = evidence_list(b.get("evidence_message_ids"))
        ca, cb = _to_float(a.get("confidence")), _to_float(b.get("confidence"))
        if ca is not None and cb is not None:
            conf_deltas.append(cb - ca)

        diff = {}
        if aa != ba:
            n_action += 1
            action_changes["{} -> {}".format(aa, ba)] = (
                action_changes.get("{} -> {}".format(aa, ba), 0) + 1)
            diff["action"] = (aa, ba)
        if at != bt:
            n_type += 1
            type_changes["{} -> {}".format(at, bt)] = (
                type_changes.get("{} -> {}".format(at, bt), 0) + 1)
            diff["message_type"] = (at, bt)
        if ae != be:
            n_evidence += 1
            diff["evidence"] = (";".join(ae) or "none", ";".join(be) or "none")
        if not diff and (ca == cb):
            n_identical += 1
        if diff:
            diff["message_id"] = mid
            changed_rows.append(diff)

    result = {
        "path_a": path_a,
        "path_b": path_b,
        "n_a": len(rows_a),
        "n_b": len(rows_b),
        "n_common": len(common),
        "only_in_a": only_a,
        "only_in_b": only_b,
        "n_action_changed": n_action,
        "n_message_type_changed": n_type,
        "n_evidence_changed": n_evidence,
        "n_unchanged": n_identical,
        "action_changes": sorted(action_changes.items(), key=lambda kv: (-kv[1], kv[0])),
        "message_type_changes": sorted(type_changes.items(), key=lambda kv: (-kv[1], kv[0])),
        "mean_confidence_delta": _mean(conf_deltas),
        "changed_rows": changed_rows,
        "columns_a": list(rows_a[0].keys()) if rows_a else [],
        "columns_b": list(rows_b[0].keys()) if rows_b else [],
        "contract_ok_a": bool(rows_a) and tuple(rows_a[0].keys()) == OUTPUT_COLUMNS,
        "contract_ok_b": bool(rows_b) and tuple(rows_b[0].keys()) == OUTPUT_COLUMNS,
    }

    if verbose:
        _print_header("COMPARE  A={}  B={}".format(path_a, path_b))
        print("rows: A={} B={} common={}".format(
            result["n_a"], result["n_b"], result["n_common"]))
        if only_a:
            print("only in A ({}): {}".format(len(only_a), ", ".join(only_a[:10])))
        if only_b:
            print("only in B ({}): {}".format(len(only_b), ", ".join(only_b[:10])))
        if not result["contract_ok_a"] or not result["contract_ok_b"]:
            print("WARNING: column contract mismatch  A_ok={} B_ok={}".format(
                result["contract_ok_a"], result["contract_ok_b"]))
        print("changed: action={}  message_type={}  evidence={}  unchanged={}".format(
            n_action, n_type, n_evidence, n_identical))
        print("mean confidence delta (B - A): {}".format(
            _fmt_num(result["mean_confidence_delta"], 3)))
        if result["action_changes"]:
            print("")
            print("action transitions:")
            for key, count in result["action_changes"]:
                print("  {:<24} {}".format(key, count))
        if result["message_type_changes"]:
            print("")
            print("message_type transitions:")
            for key, count in result["message_type_changes"][:12]:
                print("  {:<34} {}".format(key, count))
        if changed_rows:
            print("")
            print("changed rows ({} shown):".format(min(len(changed_rows), 40)))
            for diff in changed_rows[:40]:
                parts = []
                for field in ("action", "message_type", "evidence"):
                    if field in diff:
                        parts.append("{}: {} -> {}".format(field, *diff[field]))
                print("  {:<12} {}".format(diff["message_id"], " | ".join(parts)))
        print("")
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        if len(sys.argv) != 4:
            print("usage: python code/evaluate.py compare <output_a.csv> <output_b.csv>")
            sys.exit(2)
        compare_outputs(sys.argv[2], sys.argv[3])
    else:
        args = [a for a in sys.argv[1:] if a != "--llm"]
        target = args[0] if args else DATASET_DIR
        backtest(target, use_llm="--llm" in sys.argv)
