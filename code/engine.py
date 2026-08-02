"""Rule engine: evaluates rules.json against a label set.

This module contains no routing knowledge. It knows how to test a condition and
how to walk an ordered table; every decision about what should be notified,
digested or muted lives in rules.json. That separation is the point -- policy is
reviewable as data, and a policy change is a diff in a JSON file rather than an
edit to control flow.

Each decision carries the id of the rule that produced it, so any row in
output.csv can be traced back to the exact rule and its stated rationale.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.environ.get("RULES", os.path.join(_HERE, "rules.json"))

with open(RULES_PATH, "r", encoding="utf-8") as _fh:
    POLICY = json.load(_fh)

ACTION_RULES = POLICY["action_rules"]
TYPE_RULES = POLICY["type_rules"]
POLICY_VERSION = POLICY["version"]


class UnknownLabel(KeyError):
    """A rule referenced a label that was never defined or supplied.

    Raised rather than treated as false: a typo in a rule must fail loudly, not
    quietly make the rule unreachable and take a routing decision with it.
    """


def _test(cond: List[Any], labels: Dict[str, Any]) -> bool:
    name, op, want = cond
    if name not in labels:
        raise UnknownLabel(name)
    have = labels[name]

    if op == "==":
        return have == want
    if op == "!=":
        return have != want
    if op == "in":
        return have in want
    if op == "not_in":
        return have not in want
    if op in (">=", "<="):
        # A missing numeric fact must never satisfy a threshold. `None` here
        # means "this sender has no account age on record", which is not the
        # same as "its account age is 0".
        if have is None or isinstance(have, bool):
            return False
        return have >= want if op == ">=" else have <= want
    raise ValueError(f"unknown operator {op!r} in condition {cond!r}")


def _matches(rule: dict, labels: Dict[str, Any]) -> bool:
    return all(_test(c, labels) for c in rule["when"])


def _resolve(value: Any, labels: Dict[str, Any]) -> Any:
    """`@label` in a rule's `then` means "use this label's value"."""
    if isinstance(value, str) and value.startswith("@"):
        return labels.get(value[1:])
    return value


def evaluate(labels: Dict[str, Any]) -> Tuple[dict, dict]:
    """Return (action_outcome, type_outcome), each carrying its rule id.

    Both tables end in an unconditional rule, so a match is guaranteed; if one
    is removed we would rather fail than emit an invalid row.
    """
    action_hit: Optional[dict] = None
    for rule in ACTION_RULES:
        if _matches(rule, labels):
            action_hit = rule
            break
    if action_hit is None:
        raise ValueError("no action rule matched and no default rule exists")

    type_hit: Optional[dict] = None
    for rule in TYPE_RULES:
        if _matches(rule, labels):
            type_hit = rule
            break
    if type_hit is None:
        raise ValueError("no type rule matched and no default rule exists")

    out_a = dict(action_hit["then"])
    out_a["action"] = _resolve(out_a.get("action"), labels)
    out_a["reason"] = _resolve(out_a.get("reason"), labels)
    out_a["rule_id"] = action_hit["id"]

    out_t = {"type": _resolve(type_hit["then"].get("type"), labels),
             "rule_id": type_hit["id"]}
    return out_a, out_t


def referenced_labels() -> set:
    """Every label name the policy depends on -- used to validate the spec."""
    names = set()
    for table in (ACTION_RULES, TYPE_RULES):
        for rule in table:
            for cond in rule["when"]:
                names.add(cond[0])
            for v in rule["then"].values():
                if isinstance(v, str) and v.startswith("@"):
                    names.add(v[1:])
    return names
