"""FastAPI application for compliance rule evaluation."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import engine


class EvaluateRequest(BaseModel):
    facts: Dict[str, Any] = Field(default_factory=dict)
    rules: Optional[Dict[str, Any]] = None
    # Target may be an inline rule node or the name (string) of a named rule.
    target: Any


class CompareRequest(BaseModel):
    baseline_facts: Dict[str, Any] = Field(default_factory=dict)
    candidate_facts: Dict[str, Any] = Field(default_factory=dict)
    rules: Optional[Dict[str, Any]] = None
    # Same inline-node-or-name semantics as EvaluateRequest.target.
    target: Any


app = FastAPI(
    title="Compliance Rule Evaluation API",
    version="1.0.0",
    description=(
        "Evaluate named compliance rules against a fact dictionary using "
        "true/false/unknown three-valued logic."
    ),
)


@app.exception_handler(engine.RuleError)
async def rule_error_handler(request: Request, exc: engine.RuleError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "rule_error", "detail": str(exc)},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_request", "detail": exc.errors()},
    )


@app.get("/")
def root() -> Dict[str, str]:
    return {"service": "compliance-rule-api", "docs": "/docs"}


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/evaluate")
def evaluate(req: EvaluateRequest) -> Dict[str, Any]:
    """Resolve dependencies, then evaluate the target rule."""
    # Stage 1: structural validation + reference resolution + cycle detection.
    engine.analyze(req.rules, req.target)
    # Stage 2: three-valued evaluation with diagnostics.
    return engine.evaluate(req.rules, req.facts, req.target)


@app.post("/compare")
def compare(req: CompareRequest) -> Dict[str, Any]:
    """Evaluate one rules/target pair against two fact sets and diff them."""
    # Static checks run once for the shared rules/target: malformed nodes,
    # missing refs and cycles reject the whole request before any evaluation.
    engine.analyze(req.rules, req.target)
    # Each side keeps its own short-circuit traversal, diagnostics and order.
    baseline = engine.evaluate(req.rules, req.baseline_facts, req.target)
    candidate = engine.evaluate(req.rules, req.candidate_facts, req.target)
    return {
        "baseline": baseline,
        "candidate": candidate,
        "result_changed": baseline["result"] != candidate["result"],
        "diff": engine.diff_evaluations(baseline, candidate),
    }
