"""Compliance rule engine.

Rule grammar
------------

Every rule node is an object with exactly one operator key:

* ``{"all": [node, ...]}``  logical conjunction (Kleene three-valued logic)
* ``{"any": [node, ...]}``  logical disjunction
* ``{"not": node}``         logical negation
* ``{"ref": "rule_name"}``  reference to a named rule
* ``{"eq":  ["field", value]}`` ... ``{"lte": ["field", value]}``
                            fact comparison against a JSON literal

Values use tri-state logic represented by Python ``True`` / ``False`` /
``None`` where ``None`` means *unknown* (a fact field is missing or the
comparison is not defined for the given types).

Evaluation happens in two stages:

1. :func:`analyze` validates the whole request structurally, resolves every
   ``ref`` and rejects missing references or dependency cycles.
2. :func:`evaluate` evaluates the target rule while recording the rule paths
   actually traversed (short-circuit included), the facts actually read and
   the fact fields that were referenced but not provided.
3. :func:`diff_evaluations` aligns two evaluations of the same rule tree by
   path, powering the ``/compare`` endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import sys

# Deep ref chains (long acyclic dependency paths) are evaluated recursively.
sys.setrecursionlimit(10_000)

Tri = Optional[bool]  # True / False / None(unknown)

LOGIC_OPS = ("all", "any", "not", "ref")
COMPARE_OPS = ("eq", "ne", "gt", "gte", "lt", "lte")
KNOWN_OPS = LOGIC_OPS + COMPARE_OPS


class RuleError(ValueError):
    """A static rule error: malformed node, missing ref or cycle."""


# --------------------------------------------------------------------------- #
# Structural validation
# --------------------------------------------------------------------------- #


def _location(path: str) -> str:
    return f" at {path}" if path else ""


def _validate_node(node: Any, location: str) -> None:
    """Recursively validate one rule node."""
    if not isinstance(node, dict):
        raise RuleError(
            f"rule node must be an object{_location(location)}, got "
            f"{type(node).__name__}"
        )

    keys = list(node.keys())
    known = [k for k in keys if k in KNOWN_OPS]
    if len(known) == 0:
        raise RuleError(
            f"rule node{_location(location)} must contain exactly one operator "
            f"key, one of {list(KNOWN_OPS)}; got keys {keys!r}"
        )
    if len(known) > 1:
        raise RuleError(
            f"rule node{_location(location)} must contain exactly one operator "
            f"key, found {known}"
        )
    if len(keys) > 1:
        unknown = [k for k in keys if k not in KNOWN_OPS]
        raise RuleError(
            f"rule node{_location(location)} has unsupported keys {unknown}; "
            f"allowed keys are {list(KNOWN_OPS)}"
        )

    op = known[0]
    value = node[op]

    if op in ("all", "any"):
        if not isinstance(value, list):
            raise RuleError(
                f"'{op}' value{_location(location)} must be an array of rule "
                f"nodes, got {type(value).__name__}"
            )
        for i, child in enumerate(value):
            _validate_node(child, f"{location}/{op}[{i}]" if location else f"{op}[{i}]")
        return

    if op == "not":
        _validate_node(value, f"{location}/not" if location else "not")
        return

    if op == "ref":
        if not isinstance(value, str) or not value:
            raise RuleError(
                f"'ref' value{_location(location)} must be a non-empty rule "
                f"name string, got {value!r}"
            )
        return

    # comparison operators
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not value[0]
    ):
        raise RuleError(
            f"'{op}' value{_location(location)} must be [fact_field, literal] "
            f"with a non-empty field name, got {value!r}"
        )


def validate_request(rules: Any, target: Any) -> None:
    """Validate the named-rule map and the target rule up front."""
    if rules is None:
        rules = {}
    if not isinstance(rules, dict):
        raise RuleError("'rules' must be an object mapping rule names to nodes")
    for name, node in rules.items():
        if not isinstance(name, str) or not name:
            raise RuleError(f"rule names must be non-empty strings, got {name!r}")
        _validate_node(node, f"rules[{name}]")

    if isinstance(target, str):
        if not target:
            raise RuleError("'target' rule name must be a non-empty string")
        return
    _validate_node(target, "target")


def collect_refs(node: Any, out: set[str]) -> None:
    """Collect every rule name referenced (directly) from ``node``."""
    if "ref" in node:
        out.add(node["ref"])
        return
    for op in ("all", "any"):
        if op in node:
            for child in node[op]:
                collect_refs(child, out)
            return
    if "not" in node:
        collect_refs(node["not"], out)


def find_missing_refs(rules: dict[str, Any], target: Any) -> list[str]:
    """Return a sorted list of referenced rule names that are not defined."""
    missing: set[str] = set()
    referenced: set[str] = set()
    for node in rules.values():
        collect_refs(node, referenced)
    if isinstance(target, dict):
        collect_refs(target, referenced)
    else:
        referenced.add(target)  # target string is itself an implicit ref
    missing = {name for name in referenced if name not in rules}
    return sorted(missing)


def find_cycle(rules: dict[str, Any]) -> Optional[list[str]]:
    """Iterative DFS cycle detection over the named-rule dependency graph.

    Returns the first cycle found (as a list of rule names closing back on
    itself), or ``None`` when the graph is acyclic. Sorted iteration makes
    the reported cycle stable for a given request.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in rules}
    # parent[name] tells which rule edge discovered it, for cycle reporting.
    parent: dict[str, str] = {}

    for root in sorted(rules):
        if color[root] != WHITE:
            continue
        # stack frames: (node, ordered direct deps not yet walked)
        deps: set[str] = set()
        collect_refs(rules[root], deps)
        stack: list[tuple[str, list[str]]] = [(root, sorted(deps))]
        color[root] = GRAY
        while stack:
            name, pending = stack[-1]
            if not pending:
                color[name] = BLACK
                stack.pop()
                continue
            nxt = pending.pop(0)
            if nxt not in rules:
                continue  # missing refs are reported separately
            if color[nxt] == GRAY:
                # Reconstruct the cycle from the DFS stack.
                chain = [frame[0] for frame in stack]
                idx = chain.index(nxt)
                return chain[idx:] + [nxt]
            if color[nxt] == WHITE:
                parent[nxt] = name
                child_deps: set[str] = set()
                collect_refs(rules[nxt], child_deps)
                color[nxt] = GRAY
                stack.append((nxt, sorted(child_deps)))
    return None


def analyze(rules: dict[str, Any] | None, target: Any) -> None:
    """Run all static checks. Raises :class:`RuleError` on the first problem."""
    validate_request(rules, target)
    rules = rules or {}

    missing = find_missing_refs(rules, target)
    if missing:
        raise RuleError(
            f"missing referenced rule(s): {missing}; define them in 'rules' "
            f"or remove the references"
        )

    cycle = find_cycle(rules)
    if cycle is not None:
        raise RuleError("cyclic rule reference detected: " + " -> ".join(cycle))


# --------------------------------------------------------------------------- #
# Three-valued primitives
# --------------------------------------------------------------------------- #


def _not(value: Tri) -> Tri:
    if value is None:
        return None
    return not value


def _compare(op: str, actual: Any, expected: Any) -> Tri:
    """Apply a comparison. Missing fact / undefined ordering -> unknown."""
    if actual is _MISSING:
        return None
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if actual is None or expected is None:
            # Ordering of null is not defined; equality already handled above.
            return None
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
    except TypeError:
        # E.g. "abc" > 1, dict > list: not comparable -> unknown.
        return None
    raise RuleError(f"unknown comparison operator: {op}")  # pragma: no cover


_MISSING = object()


# --------------------------------------------------------------------------- #
# Evaluation with diagnostics
# --------------------------------------------------------------------------- #


@dataclass
class Trace:
    """Diagnostics collected while evaluating a single request."""

    used_facts: set[str] = field(default_factory=set)
    missing_fields: set[str] = field(default_factory=set)
    hit_paths: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        path: str,
        kind: str,
        result: Tri,
        *,
        field: Optional[str] = None,
        expected: Any = None,
        ref: Optional[str] = None,
    ) -> None:
        entry: dict[str, Any] = {
            "path": path,
            "kind": kind,
            "result": _label(result),
        }
        if field is not None:
            entry["field"] = field
            entry["expected"] = expected
        if ref is not None:
            entry["ref"] = ref
        self.hit_paths.append(entry)


def _label(value: Tri) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def evaluate(
    rules: dict[str, Any] | None,
    facts: dict[str, Any] | None,
    target: Any,
) -> dict[str, Any]:
    """Evaluate ``target`` against ``facts`` and return result + diagnostics.

    Static analysis must have passed (see :func:`analyze`).
    """
    rules = rules or {}
    facts = facts or {}
    trace = Trace()

    if isinstance(target, str):
        result = _eval_named(rules, facts, target, "$target", trace)
    else:
        result = _eval_node(rules, facts, target, "$target", trace)

    return {
        "result": _label(result),
        "result_known": result is not None,
        "used_facts": sorted(trace.used_facts),
        "missing_fields": sorted(trace.missing_fields),
        "hit_paths": trace.hit_paths,
    }


def _eval_named(
    rules: dict[str, Any],
    facts: dict[str, Any],
    name: str,
    path: str,
    trace: Trace,
) -> Tri:
    node = rules[name]
    return _eval_node(rules, facts, node, path, trace, ref=name)


def _eval_node(
    rules: dict[str, Any],
    facts: dict[str, Any],
    node: dict[str, Any],
    path: str,
    trace: Trace,
    ref: Optional[str] = None,
) -> Tri:
    # --- all: false short-circuits; unknown only if no false seen ----------
    if "all" in node:
        combined: Tri = True
        for i, child in enumerate(node["all"]):
            val = _eval_node(
                rules, facts, child, f"{path}/all[{i}]", trace
            )
            if val is False:
                combined = False
                break  # short-circuit: remaining children are not traversed
            if val is None:
                combined = None
        trace.add(path, "all", combined, ref=ref)
        return combined

    # --- any: true short-circuits; unknown only if no true seen ------------
    if "any" in node:
        combined = False
        for i, child in enumerate(node["any"]):
            val = _eval_node(
                rules, facts, child, f"{path}/any[{i}]", trace
            )
            if val is True:
                combined = True
                break  # short-circuit
            if val is None:
                combined = None
        trace.add(path, "any", combined, ref=ref)
        return combined

    if "not" in node:
        val = _not(_eval_node(rules, facts, node["not"], f"{path}/not", trace))
        trace.add(path, "not", val, ref=ref)
        return val

    if "ref" in node:
        name = node["ref"]
        return _eval_named(rules, facts, name, f"{path}/ref:{name}", trace)

    # --- comparison ---------------------------------------------------------
    op = next(k for k in COMPARE_OPS if k in node)
    field_name, expected = node[op]
    trace.used_facts.add(field_name)
    if field_name in facts:
        actual: Any = facts[field_name]
    else:
        actual = _MISSING
        trace.missing_fields.add(field_name)
    val = _compare(op, actual, expected)
    trace.add(
        path,
        "compare",
        val,
        field=field_name,
        expected=expected,
        ref=ref,
    )
    return val


# --------------------------------------------------------------------------- #
# Comparing two evaluations of the same rule tree (used by POST /compare)
# --------------------------------------------------------------------------- #


def _entry_index(hit_paths: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index hit-path entries by their unique path."""
    return {entry["path"]: entry for entry in hit_paths}


def _set_diff(baseline: list[str], candidate: list[str]) -> dict[str, list[str]]:
    """Added/removed members between two already-sorted lists."""
    base, cand = set(baseline), set(candidate)
    return {
        "added": sorted(cand - base),
        "removed": sorted(base - cand),
    }


def diff_evaluations(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Align two evaluations of the same rules/target and report the delta.

    ``baseline`` and ``candidate`` are outputs of :func:`evaluate` run against
    different facts. Paths are aligned 1:1 because the rule tree is identical;
    short-circuiting can still make a path appear on only one side.

    Returned keys:

    * ``added``   : full hit-path entries traversed only on the candidate side
    * ``removed`` : full hit-path entries traversed only on the baseline side
    * ``result_changed`` : entries present on both sides whose three-valued
      ``result`` differs
    * ``used_facts`` / ``missing_fields`` : ``{"added": [...], "removed": [...]}``

    All lists are stable: ``added`` follows the candidate traversal order,
    ``removed`` and ``result_changed`` follow the baseline traversal order, and
    set diffs are sorted lexicographically (the evaluation outputs already are).
    """
    base_by_path = _entry_index(baseline["hit_paths"])
    cand_by_path = _entry_index(candidate["hit_paths"])

    # Preserve traversal order on the originating side instead of sorting paths
    # lexically, so output stays tied to deterministic evaluation order.
    added = [
        entry for entry in candidate["hit_paths"]
        if entry["path"] not in base_by_path
    ]
    removed = [
        entry for entry in baseline["hit_paths"]
        if entry["path"] not in cand_by_path
    ]

    result_changed: list[dict[str, Any]] = []
    for entry in baseline["hit_paths"]:
        other = cand_by_path.get(entry["path"])
        if other is not None and other["result"] != entry["result"]:
            change = {
                "path": entry["path"],
                "kind": entry["kind"],
                "baseline_result": entry["result"],
                "candidate_result": other["result"],
            }
            # Static metadata (field/expected/ref) is identical on both sides
            # because the rule tree is shared; carry it for readability.
            if "field" in entry:
                change["field"] = entry["field"]
                change["expected"] = entry["expected"]
            if "ref" in entry:
                change["ref"] = entry["ref"]
            result_changed.append(change)

    return {
        "added": added,
        "removed": removed,
        "result_changed": result_changed,
        "used_facts": _set_diff(baseline["used_facts"], candidate["used_facts"]),
        "missing_fields": _set_diff(
            baseline["missing_fields"], candidate["missing_fields"]
        ),
    }
