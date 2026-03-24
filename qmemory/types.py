"""
Qmemory Type Definitions

Pydantic models for all graph nodes, edges, and supporting types.
These mirror the TypeScript types in src/types.ts and the SurrealDB
schema in py/qmemory/db/schema.surql — field names must match exactly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Literal types (narrow string unions)
# ---------------------------------------------------------------------------

# The 8 memory categories — used as the category field on Memory
MemoryCategory = Literal[
    "style",       # Communication preferences
    "preference",  # General user preferences
    "context",     # Facts about projects/orgs/situations
    "decision",    # Past decisions made, with rationale
    "idea",        # Future plans, suggestions, proposals
    "feedback",    # User corrections and error reports
    "domain",      # Sector/domain knowledge
    "self",        # Agent self-knowledge (soul) — injected first in context
]

# Dedup decisions returned by the LLM when saving a memory
DedupAction = Literal["ADD", "UPDATE", "NOOP"]

# How a memory was learned — stored as evidence_type on Memory
EvidenceType = Literal["observed", "reported", "inferred", "self"]

# Extraction mode presets — controls how aggressively facts are extracted
ExtractionMode = Literal["economy", "balanced", "aggressive"]


# ---------------------------------------------------------------------------
# NODE: session
# Tracks an agent conversation context (DM, group topic, cron job, etc.)
# ---------------------------------------------------------------------------

class Session(BaseModel):
    # SurrealDB record ID, e.g. "session:abc123"
    id: str
    # Full agent session key, e.g. "agent:main:telegram:group:123:topic:7"
    session_key: str
    # Channel name, e.g. "telegram", "whatsapp"
    channel: str | None = None
    # Conversation type: "direct", "group", "cron", "subagent"
    chat_type: str | None = None
    # Telegram topic ID (for supergroup topics)
    topic_id: str | None = None
    # Telegram group ID
    group_id: str | None = None
    # Visibility scope: "global", "project:xxx", "topic:xxx"
    scope: str = "global"
    # Which LLM model is being used in this session
    model_name: str | None = None
    # Running count of messages in this session
    message_count: int = 0
    # False when the session has ended
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# NODE: message
# A single message stored in the conversation graph.
# ---------------------------------------------------------------------------

class Message(BaseModel):
    # SurrealDB record ID, e.g. "message:abc123"
    id: str
    # FK → session record ID
    session: str
    # Who sent this message
    role: Literal["user", "assistant", "system", "tool"]
    # Raw text content
    content: str
    # Structured tool call objects (for assistant messages that invoke tools)
    tool_calls: list[dict] | None = None
    # For tool-result messages, the name of the tool that was called
    tool_name: str | None = None
    # Approximate token count for budget tracking
    token_count: int | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# NODE: memory
# A single extracted fact stored as a graph node.
# The core unit of long-term knowledge in Qmemory.
# ---------------------------------------------------------------------------

class Memory(BaseModel):
    # SurrealDB record ID, e.g. "memory:abc123"
    id: str
    # The fact itself — one clear statement
    content: str
    # Which category this fact belongs to (see MemoryCategory)
    category: str
    # Importance weight 0.0–1.0 — higher = recalled first
    salience: float = 0.5
    # Visibility scope: "global", "project:xxx", "topic:xxx"
    scope: str = "global"
    # LLM confidence in this fact 0.0–1.0
    confidence: float = 0.8
    # FK → entity record (who said this fact)
    source_person: str | None = None
    # How this memory was learned
    evidence_type: str = "observed"
    # Situational mood when fact was captured
    context_mood: str | None = None
    # When the fact first became true (ISO datetime)
    valid_from: datetime | None = None
    # When the fact expired (ISO datetime) — None means still valid
    valid_until: datetime | None = None
    # How many times this memory has been recalled (biological counter)
    recall_count: int = 0
    # Timestamp of the most recent recall
    last_recalled: datetime | None = None
    # How this memory was sourced: conversation, workspace, agent, linker, reflect, cron
    source_type: str = "conversation"
    # Whether the linker has already processed this memory for graph edges
    linked: bool = False
    # FK → previous version of this memory (version chain on update)
    prev_version: str | None = None
    # Optional vector embedding for semantic search
    embedding: list[float] | None = None
    # False if soft-deleted
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# NODE: entity
# A named thing in the world — person, project, concept, channel, etc.
# ---------------------------------------------------------------------------

class Entity(BaseModel):
    # SurrealDB record ID, e.g. "entity:abc123"
    id: str
    # Display name, e.g. "Qusai", "Telegram", "Project Alpha"
    name: str
    # Entity type: person, project, org, concept, system, topic, book, channel, etc.
    type: str
    # Alternative names or handles for this entity
    aliases: list[str] = []
    # Source system: "whatsapp", "telegram", "hey", "gmail", "smartsheet", etc.
    external_source: str | None = None
    # Reference ID in the external system
    external_id: str | None = None
    # Direct URL to the resource in the external system
    external_url: str | None = None
    # Channel-specific identifier: phone number, username, email
    external_channel: str | None = None
    # Optional vector embedding for semantic search
    embedding: list[float] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# NODE: tool_call
# Ledger entry for every tool the agent invokes.
# Survives context compaction — the agent can always see recent tool history.
# ---------------------------------------------------------------------------

class ToolCall(BaseModel):
    # SurrealDB record ID, e.g. "tool_call:abc123"
    id: str
    # FK → session record ID
    session: str
    # Name of the tool that was invoked
    tool_name: str
    # Short summary of the input parameters (not the full payload)
    input_summary: str = ""
    # Short summary of what the tool returned
    output_summary: str = ""
    # How long the tool took to complete
    duration_ms: int | None = None
    # Token cost of this tool call
    token_count: int = 0
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# NODE: scratchpad
# Per-session working memory — task progress, findings, open questions.
# Survives compaction so the agent always has its notes.
# ---------------------------------------------------------------------------

class Scratchpad(BaseModel):
    # SurrealDB record ID, e.g. "scratchpad:abc123"
    id: str
    # FK → session record ID
    session: str
    # What the agent is currently working on
    task_progress: str = ""
    # Facts the agent has discovered this session
    key_findings: str = ""
    # Questions the agent still needs to answer
    open_questions: str = ""
    # Brief log of recent tool activity
    tool_summary: str = ""
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# NODE: metrics
# Event tracking for token usage, compactions, recalls, etc.
# ---------------------------------------------------------------------------

class Metrics(BaseModel):
    # SurrealDB record ID, e.g. "metrics:abc123"
    id: str
    # FK → session record ID
    session: str
    # What kind of event this is: "token_usage", "compaction", "recall", etc.
    event_type: str
    # JSON-serialized event payload (stored as string in SurrealDB)
    event_data: str | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# EDGE: relates
# Dynamic relationship between any two graph nodes.
# The agent (or linker/reflect/compact) can create any relationship type.
# ---------------------------------------------------------------------------

class RelatesEdge(BaseModel):
    # SurrealDB record ID, e.g. "relates:abc123"
    id: str
    # Source node record ID (the "in" side of the directed edge)
    in_node: str
    # Target node record ID (the "out" side of the directed edge)
    out_node: str
    # Relationship label — any string: "supports", "manages", "blocks", etc.
    type: str
    # Human-readable explanation of why this edge exists
    reason: str | None = None
    # Confidence in this relationship 0.0–1.0
    confidence: float | None = None
    # Who created this edge: "agent", "linker", "compact", "reflect", "system"
    created_by: str = "system"
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Supporting: dedup decision (returned by LLM when saving a memory)
# ---------------------------------------------------------------------------

class DedupDecision(BaseModel):
    # What to do with the new fact
    action: DedupAction
    # Only set for UPDATE — the existing memory ID to update
    target_id: str | None = None
    # Only set for UPDATE — the relationship type to record
    relationship_type: str | None = None
    # Related memory IDs discovered during dedup (any action)
    related: list[dict] = []
    # LLM confidence in this decision
    confidence: float = 0.8


# ---------------------------------------------------------------------------
# Supporting: recall options (passed to recall/search functions)
# ---------------------------------------------------------------------------

class RecallOptions(BaseModel):
    # Free-text semantic search query
    query: str | None = None
    # Filter to specific categories
    categories: list[str] | None = None
    # Filter to specific scope
    scope: str | None = None
    # Minimum salience threshold
    min_salience: float | None = None
    # Only return memories valid at this datetime
    valid_at: datetime | None = None
    # Maximum number of results
    limit: int | None = None
    # Token budget cap for total returned content
    token_budget: int | None = None


# ---------------------------------------------------------------------------
# Supporting: recalled memory (Memory + search metadata)
# ---------------------------------------------------------------------------

class RecalledMemory(Memory):
    # Relevance score from search (higher = more relevant)
    score: float | None = None
    # Which session this memory came from
    source_session: str | None = None
    # True if a contradiction edge was found for this memory
    is_contradicted: bool = False


# ---------------------------------------------------------------------------
# Supporting: extracted fact (returned by LLM extraction)
# ---------------------------------------------------------------------------

class ExtractedFact(BaseModel):
    # The fact as a clear statement
    content: str
    # Which category it belongs to
    category: str
    # Importance weight 0.0–1.0
    salience: float
    # Scope: "global", "project:xxx", etc.
    scope: str
    # LLM confidence in this fact
    confidence: float | None = None
    # Name of the person who said this (resolved to entity later)
    source_person: str | None = None
    # How the fact was learned
    evidence_type: str | None = None
    # Situational mood at time of capture
    context_mood: str | None = None
    # Entity names or rich entity references mentioned alongside this fact
    entities: list[str | dict] | None = None
