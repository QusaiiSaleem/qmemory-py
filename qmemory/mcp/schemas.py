"""
Pydantic input models for all Qmemory MCP tools.

Each tool has one input model that enforces enum values, numeric
ranges, string length constraints, and extra='forbid'.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_Category = Literal[
    "self",
    "style",
    "preference",
    "context",
    "decision",
    "idea",
    "feedback",
    "domain",
]

_EvidenceType = Literal["observed", "reported", "inferred", "self"]

_CorrectAction = Literal["correct", "delete", "update", "unlink"]

_HealthCheck = Literal[
    "all",
    "orphans",
    "contradictions",
    "stale",
    "missing_links",
    "gaps",
    "quality",
]

_BASE_CONFIG = ConfigDict(
    str_strip_whitespace=True,
    extra="forbid",
    validate_assignment=False,
)


class BootstrapInput(BaseModel):
    model_config = _BASE_CONFIG
    session_key: str = Field(
        default="default",
        max_length=128,
        description="Session identifier. 'default' is fine.",
    )


class SearchInput(BaseModel):
    model_config = _BASE_CONFIG
    query: str | None = Field(
        default=None,
        max_length=500,
        description="Free-text BM25 query. Omit to get recent memories.",
    )
    category: _Category | None = Field(
        default=None,
        description="Restrict to one category.",
    )
    scope: str | None = Field(
        default=None,
        max_length=128,
        description="Visibility: global | project:xxx | topic:xxx.",
    )
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0)
    after: str | None = Field(default=None)
    before: str | None = Field(default=None)
    include_tool_calls: bool = Field(default=False)
    source_type: str | None = Field(default=None, max_length=64)
    entity_id: str | None = Field(default=None, max_length=128)


class GetInput(BaseModel):
    model_config = _BASE_CONFIG
    ids: list[str] = Field(..., min_length=1, max_length=20)
    include_neighbors: bool = Field(default=False)
    neighbor_depth: int = Field(default=1, ge=1, le=2)


class SaveInput(BaseModel):
    model_config = _BASE_CONFIG
    content: str = Field(..., min_length=1, max_length=4000)
    category: _Category = Field(...)
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    scope: str = Field(default="global", max_length=128)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    source_person: str | None = Field(default=None, max_length=128)
    evidence_type: _EvidenceType = Field(default="observed")
    context_mood: str | None = Field(default=None, max_length=64)


class CorrectInput(BaseModel):
    model_config = _BASE_CONFIG
    memory_id: str = Field(..., min_length=1, max_length=128)
    action: _CorrectAction = Field(...)
    new_content: str | None = Field(default=None, max_length=4000)
    updates: dict | None = Field(default=None)
    edge_id: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class LinkInput(BaseModel):
    model_config = _BASE_CONFIG
    from_id: str = Field(..., min_length=1, max_length=128)
    to_id: str = Field(..., min_length=1, max_length=128)
    relationship_type: str = Field(..., min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=500)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class PersonInput(BaseModel):
    model_config = _BASE_CONFIG
    name: str = Field(..., min_length=1, max_length=256)
    aliases: list[str] | None = Field(default=None, max_length=20)
    contacts: list[dict] | None = Field(default=None, max_length=20)


class BooksInput(BaseModel):
    model_config = _BASE_CONFIG
    book_id: str | None = Field(default=None, max_length=128)
    section: str | None = Field(default=None, max_length=256)
    query: str | None = Field(default=None, max_length=256)


class HealthInput(BaseModel):
    model_config = _BASE_CONFIG
    check: _HealthCheck = Field(default="all")
