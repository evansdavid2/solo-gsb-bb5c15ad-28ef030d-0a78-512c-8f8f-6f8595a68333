"""End-to-end tests for the FastAPI /evaluate endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_evaluate_true():
    r = client.post(
        "/evaluate",
        json={
            "facts": {"age": 20, "country": "CN", "tier": "gold"},
            "rules": {
                "is_adult": {"gte": ["age", 18]},
                "domestic": {"eq": ["country", "CN"]},
                "eligible": {
                    "all": [
                        {"ref": "is_adult"},
                        {"ref": "domestic"},
                        {"not": {"eq": ["tier", "banned"]}},
                    ]
                },
            },
            "target": "eligible",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "true"
    assert body["result_known"] is True
    assert body["missing_fields"] == []
    assert body["used_facts"] == ["age", "country", "tier"]


def test_evaluate_unknown_and_diagnostics():
    r = client.post(
        "/evaluate",
        json={
            "facts": {"age": 20},
            "rules": {"eligible": {"all": [
                {"gte": ["age", 18]},
                {"eq": ["country", "CN"]},
            ]}},
            "target": "eligible",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "unknown"
    assert body["missing_fields"] == ["country"]
    assert body["used_facts"] == ["age", "country"]


def test_evaluate_short_circuit_stable():
    payload = {
        "facts": {"a": 0, "b": 2},
        "target": {"all": [{"eq": ["a", 1]}, {"eq": ["b", 2]}]},
    }
    r1 = client.post("/evaluate", json=payload).json()
    r2 = client.post("/evaluate", json=payload).json()
    assert r1 == r2
    assert r1["result"] == "false"
    paths = [p["path"] for p in r1["hit_paths"]]
    assert "$target/all[1]" not in paths


def test_missing_ref_returns_422():
    r = client.post(
        "/evaluate",
        json={"facts": {}, "rules": {"a": {"ref": "ghost"}}, "target": "a"},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "rule_error"
    assert "ghost" in r.json()["detail"]


def test_cycle_returns_422():
    r = client.post(
        "/evaluate",
        json={
            "facts": {},
            "rules": {"a": {"ref": "b"}, "b": {"ref": "a"}},
            "target": "a",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"] == "rule_error"
    assert "cyclic" in r.json()["detail"]


def test_malformed_body_returns_422():
    r = client.post("/evaluate", json={"facts": "not-an-object"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"


def test_inline_target():
    r = client.post(
        "/evaluate",
        json={
            "facts": {"x": 5},
            "target": {"any": [{"gt": ["x", 10]}, {"gt": ["x", 1]}]},
        },
    )
    assert r.status_code == 200
    assert r.json()["result"] == "true"


# --------------------------------------------------------------------------- #
# POST /compare
# --------------------------------------------------------------------------- #


def test_compare_reports_both_sides_and_diff():
    r = client.post(
        "/compare",
        json={
            "rules": {
                "is_adult": {"gte": ["age", 18]},
                "eligible": {"all": [
                    {"ref": "is_adult"},
                    {"eq": ["country", "CN"]},
                ]},
            },
            "target": "eligible",
            "baseline_facts": {"age": 17, "country": "CN"},
            "candidate_facts": {"age": 20, "country": "CN"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    baseline = body["baseline"]
    candidate = body["candidate"]
    assert baseline["result"] == "false"
    assert candidate["result"] == "true"
    assert body["result_changed"] is True

    # Both sides carry full /evaluate-shaped three-valued diagnostics.
    # Baseline short-circuits at the false first child, so country is unread.
    assert baseline["used_facts"] == ["age"]
    assert candidate["used_facts"] == ["age", "country"]
    assert candidate["missing_fields"] == []

    diff = body["diff"]
    # Baseline short-circuits at the false first child, so the country leaf is
    # traversed only on the candidate side and shows up as an added path.
    added = diff["added"]
    assert [e["path"] for e in added] == ["$target/all[1]"]
    assert added[0]["field"] == "country"
    assert added[0]["result"] == "true"
    assert diff["removed"] == []

    changed = diff["result_changed"]
    changed_paths = {c["path"]: c for c in changed}
    leaf = changed_paths["$target/all[0]/ref:is_adult"]
    assert leaf["baseline_result"] == "false"
    assert leaf["candidate_result"] == "true"
    assert leaf["field"] == "age"
    assert leaf["expected"] == 18
    assert changed_paths["$target"]["kind"] == "all"
    assert diff["used_facts"] == {"added": ["country"], "removed": []}
    assert diff["missing_fields"] == {"added": [], "removed": []}


def test_compare_result_unchanged_when_results_match():
    r = client.post(
        "/compare",
        json={
            "target": {"any": [{"eq": ["a", 1]}, {"eq": ["b", 2]}]},
            "baseline_facts": {"a": 1},
            "candidate_facts": {"a": 1, "b": 9},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["baseline"]["result"] == "true"
    assert body["candidate"]["result"] == "true"
    assert body["result_changed"] is False
    # b sits past the any short-circuit on both sides, so it is never read.
    assert body["diff"]["used_facts"] == {"added": [], "removed": []}


def test_compare_short_circuit_adds_and_removes_paths():
    # Outer 'all' stays non-false both sides; the inner 'any' expands on the
    # candidate (new path) while the inner 'all' contracts (removed path).
    r = client.post(
        "/compare",
        json={
            "target": {"all": [
                {"any": [{"eq": ["a", 1]}, {"eq": ["x", 9]}]},
                {"all": [{"eq": ["c", 1]}, {"eq": ["d", 2]}]},
            ]},
            "baseline_facts": {"a": 1, "c": 1},
            "candidate_facts": {"a": 0, "x": 9, "c": 0},
        },
    )
    assert r.status_code == 200
    body = r.json()
    diff = body["diff"]

    added = diff["added"]
    assert [e["path"] for e in added] == ["$target/all[0]/any[1]"]
    assert added[0]["field"] == "x"
    assert added[0]["result"] == "true"

    removed = diff["removed"]
    assert [e["path"] for e in removed] == ["$target/all[1]/all[1]"]
    assert removed[0]["field"] == "d"
    assert removed[0]["result"] == "unknown"

    assert diff["used_facts"] == {"added": ["x"], "removed": ["d"]}
    assert diff["missing_fields"] == {"added": [], "removed": ["d"]}
    # Outer result flips unknown -> false.
    assert body["baseline"]["result"] == "unknown"
    assert body["candidate"]["result"] == "false"
    assert body["result_changed"] is True


def test_compare_expansion_marks_newly_missing_field():
    # Baseline short-circuits at all[0]; candidate reaches all[1] where b is
    # referenced but absent -> added path plus an added missing field.
    r = client.post(
        "/compare",
        json={
            "target": {"all": [{"eq": ["a", 1]}, {"eq": ["b", 2]}]},
            "baseline_facts": {"a": 0, "b": 2},
            "candidate_facts": {"a": 1},
        },
    )
    assert r.status_code == 200
    body = r.json()
    diff = body["diff"]
    assert [e["path"] for e in diff["added"]] == ["$target/all[1]"]
    assert diff["added"][0]["result"] == "unknown"
    assert diff["removed"] == []
    assert diff["used_facts"] == {"added": ["b"], "removed": []}
    assert diff["missing_fields"] == {"added": ["b"], "removed": []}
    # false vs unknown: the tri-state label still changes.
    assert body["result_changed"] is True


def test_compare_missing_fields_added_and_removed():
    r = client.post(
        "/compare",
        json={
            "target": {"all": [{"eq": ["a", 1]}, {"eq": ["b", 2]}]},
            "baseline_facts": {"a": 1},
            "candidate_facts": {"b": 2},
        },
    )
    body = r.json()
    assert body["baseline"]["result"] == "unknown"
    assert body["candidate"]["result"] == "unknown"
    assert body["result_changed"] is False
    # Both fields are referenced on both sides; only presence in facts swaps.
    assert body["diff"]["used_facts"] == {"added": [], "removed": []}
    assert body["diff"]["missing_fields"] == {"added": ["a"], "removed": ["b"]}


def test_compare_missing_ref_returns_422():
    r = client.post(
        "/compare",
        json={
            "rules": {"a": {"ref": "ghost"}},
            "target": "a",
            "baseline_facts": {},
            "candidate_facts": {"x": 1},
        },
    )
    assert r.status_code == 422
    assert r.json()["error"] == "rule_error"
    assert "ghost" in r.json()["detail"]


def test_compare_cycle_returns_422():
    r = client.post(
        "/compare",
        json={
            "rules": {"a": {"ref": "b"}, "b": {"ref": "a"}},
            "target": "a",
            "baseline_facts": {},
            "candidate_facts": {},
        },
    )
    assert r.status_code == 422
    assert "cyclic" in r.json()["detail"]


def test_compare_malformed_body_returns_422():
    r = client.post(
        "/compare",
        json={"baseline_facts": "nope", "candidate_facts": {}, "target": {"eq": ["x", 1]}},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"


def test_compare_missing_target_returns_422():
    r = client.post(
        "/compare",
        json={"baseline_facts": {}, "candidate_facts": {}},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"


def test_compare_defaults_to_empty_facts():
    r = client.post(
        "/compare",
        json={"target": {"eq": ["x", 1]}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["baseline"]["result"] == "unknown"
    assert body["candidate"]["result"] == "unknown"
    assert body["result_changed"] is False
    assert body["diff"] == {
        "added": [],
        "removed": [],
        "result_changed": [],
        "used_facts": {"added": [], "removed": []},
        "missing_fields": {"added": [], "removed": []},
    }
