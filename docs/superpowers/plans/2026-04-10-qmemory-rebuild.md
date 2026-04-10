# Qmemory Rebuild Mission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the MCP layer on a single FastMCP package, activate per-user SurrealDB isolation via `/mcp/u/{code}/` URLs, migrate Qusai's legacy data from `main` to `user_qusai`, deploy the background worker to Railway, and ship two small search improvements — all as one coherent mission.

**Architecture:** One operations table (`qmemory/mcp/operations.py`) feeds both stdio and HTTP transports. A FastAPI middleware extracts `user_code` from the URL path, looks it up in a new `qmemory.admin` database, and sets a ContextVar that routes every subsequent `get_db()` call to the correct per-user SurrealDB database. A dedicated Railway worker service iterates all active users once per hour to maintain their graphs.

**Tech Stack:** Python 3.11+, FastAPI, FastMCP (official `mcp` SDK v1.x), Pydantic v2, SurrealDB v3, pytest + pytest-asyncio, Railway (API + worker services), Click (admin CLI).

**Spec:** `docs/superpowers/specs/2026-04-10-qmemory-rebuild-design.md` — read this before starting. All 10 locked decisions (D1–D10) and 5 resolved open questions are in §4 and §8.

---

## Pre-flight context (read before Task 0.1)

**What already exists** (do NOT rebuild):

- `qmemory/db/client.py` — `_user_db` ContextVar (line 38) and `get_db()` that already honors it (line 68). The per-request routing infrastructure is wired.
- `qmemory/db/provision.py` — `provision_user_db(user_id)` already creates `user_{id}` database and applies schema. Idempotent. Works.
- `tests/test_db_routing.py` — tests `_user_db` ContextVar routing against a real SurrealDB.
- `tests/test_provision.py` — tests `provision_user_db()`.
- `qmemory/db/schema.surql`, `schema_cloud.surql` — base + cloud schemas exist.
- `qmemory/worker/__init__.py` — worker loop with 5 jobs (linker, dedup, decay, reflector, linter). Correct, just never started on Railway.
- `qmemory/cli.py` — `worker` command exists with `--interval` and `--once` flags.

**What must be built new:**

- `qmemory/mcp/schemas.py`, `operations.py`, `registry.py`, `errors.py` — the operations-table layer.
- `qmemory/db/admin_schema.surql` — tiny schema for the admin DB.
- `qmemory/db/client.py::get_admin_db()` — new helper.
- `qmemory/app/data/eff_large_wordlist.txt` + `excluded_words.txt` — word source for user codes.
- `qmemory/app/wordlist.py` + `user_code.py` — generator.
- `qmemory/app/middleware/user_context.py` — `MCPUserMiddleware`.
- `qmemory/admin/cli.py` — four admin commands.
- Railway worker service (manual setup, runbook committed).

**What must be rewritten:**

- `qmemory/mcp/server.py` → shrunk to ~20 lines.
- `qmemory/app/main.py` → MCP mount section + CORS fix; use operations table.
- `qmemory/app/routes/signup.py` → zero-friction flow.
- `qmemory/worker/__init__.py` → add `--all-users` mode.

**What must be deleted:**

- `qmemory_import` tool (stub).
- `fastmcp>=2.0` dependency (jlowin's package).

**Branch:** `rebuild-2026-04-10`. **Do all work on this branch.** Do NOT merge until Phase 6 verification passes.

**Conventions in this codebase:**
- `from __future__ import annotations` at the top of every Python module.
- Type hints everywhere: `str | None`, not `Optional[str]`.
- Logger per module: `logger = logging.getLogger(__name__)`.
- Database queries via `await query(db, "SELECT ...", {"x": val})` — never f-string interpolation into SurrealQL.
- Async context managers (`async with get_db() as db:`) for every DB call.
- Test files mirror source structure: `qmemory/foo/bar.py` → `tests/test_foo/test_bar.py`.
- Pytest asyncio mode is `"auto"` (from `pyproject.toml`) — mark tests `async def` without `@pytest.mark.asyncio`.

---

# Phase 0 — Safety Net

### Task 0.1: Create feature branch and pre-rebuild tag

**Files:** none (git operations only)

- [ ] **Step 1: Verify clean working tree**

```bash
git status
```

Expected: only the known working-directory changes. If unexpected state, stop and investigate.

- [ ] **Step 2: Create the feature branch**

```bash
git checkout -b rebuild-2026-04-10
```

Expected: `Switched to a new branch 'rebuild-2026-04-10'`

- [ ] **Step 3: Tag the current commit as the rollback point**

```bash
git tag pre-rebuild-2026-04-10
git show pre-rebuild-2026-04-10 --no-patch --format="%H %s"
```

Expected: tag shows the current HEAD commit hash.

### Task 0.2: Back up production SurrealDB

**Files:** local backup file in `~/qmemory-backups/`

- [ ] **Step 1: Export the production `main` database**

```bash
cd /Users/qusaiabushanap/dev/qmemory-py
./surrealdb/backup.sh
```

Expected: prints backup path and row counts. Memory count should be ~8,635.

- [ ] **Step 2: Verify gzip integrity**

```bash
gunzip -t $(ls -t ~/qmemory-backups/*.gz | head -1)
echo "exit: $?"
```

Expected: `exit: 0`

- [ ] **Step 3: Count memories inside the backup**

```bash
zgrep -c "^UPDATE memory:" $(ls -t ~/qmemory-backups/*.gz | head -1)
```

Expected: approximately 8,635. Write the number down.

- [ ] **Step 4: Copy to a second location**

```bash
cp $(ls -t ~/qmemory-backups/*.gz | head -1) ~/Desktop/qmemory-backup-pre-rebuild.surql.gz
ls -lh ~/Desktop/qmemory-backup-pre-rebuild.surql.gz
```

### Task 0.3: Snapshot Railway environment variables

**Files:** `~/railway-env-*-pre-rebuild.txt` (local only)

- [ ] **Step 1: Export API service env vars**

```bash
railway variables --service qmemory > ~/railway-env-api-pre-rebuild.txt
wc -l ~/railway-env-api-pre-rebuild.txt
```

- [ ] **Step 2: Export SurrealDB service env vars**

```bash
railway variables --service surrealdb > ~/railway-env-surrealdb-pre-rebuild.txt
wc -l ~/railway-env-surrealdb-pre-rebuild.txt
```

- [ ] **Step 3: Confirm both files exist**

```bash
ls -la ~/railway-env-*-pre-rebuild.txt
```

Expected: both files exist in `$HOME`, not in the repo.

---

# Phase 1 — MCP Layer Rebuild

**Phase exit criteria:** single FastMCP package, single operations table, nine tools (not ten), Pydantic input models with constraints, annotations on every tool, unified error handling, `uv run pytest tests/` passes, `qmemory serve` boots, `curl` against `/mcp/tools/list` returns 9 tool schemas. Single-user behavior unchanged — still reads `main` database.

### Task 1.1: Remove `fastmcp>=2.0` from dependencies

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock` (auto-regenerated)

- [ ] **Step 1: Edit `pyproject.toml` to drop jlowin's package**

Open `pyproject.toml`, find the `dependencies = [...]` block, remove the line `"fastmcp>=2.0",`. Keep `"mcp[cli]>=1.0.0"`.

- [ ] **Step 2: Regenerate the lockfile**

```bash
uv lock
```

- [ ] **Step 3: Verify the package is gone**

```bash
grep -c "^name = \"fastmcp\"" uv.lock || echo "(absent)"
```

Expected: `0` or `(absent)`.

- [ ] **Step 4: Sync the environment**

```bash
uv sync
```

- [ ] **Step 5: Grep for any remaining imports**

```bash
grep -rn "^import fastmcp\|^from fastmcp" qmemory/ tests/
```

Expected: exactly one hit — `qmemory/app/main.py:24: import fastmcp`. Fixed in Task 1.7.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: drop jlowin's fastmcp package - unify on official mcp SDK"
```

### Task 1.2: Create the error wrapper

**Files:**
- Create: `qmemory/mcp/errors.py`
- Create: `tests/test_mcp/__init__.py`
- Create: `tests/test_mcp/test_errors.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp/__init__.py` (empty).

Create `tests/test_mcp/test_errors.py`:

```python
"""Tests for the MCP error wrapper."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from qmemory.mcp.errors import safe_tool


class _EchoInput(BaseModel):
    value: str


async def _ok_handler(i: _EchoInput) -> dict:
    return {"echoed": i.value}


async def _raise_handler(i: _EchoInput) -> dict:
    raise RuntimeError("boom")


async def test_safe_tool_returns_json_text_on_success():
    result_text = await safe_tool(
        name="test_echo",
        handler=_ok_handler,
        validated=_EchoInput(value="hello"),
    )
    parsed = json.loads(result_text)
    assert parsed == {"echoed": "hello"}


async def test_safe_tool_catches_exceptions_and_returns_is_error():
    result_text = await safe_tool(
        name="test_raise",
        handler=_raise_handler,
        validated=_EchoInput(value="anything"),
    )
    parsed = json.loads(result_text)
    assert parsed["isError"] is True
    assert "content" in parsed
    assert parsed["content"][0]["type"] == "text"
    assert "test_raise" in parsed["content"][0]["text"]
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_mcp/test_errors.py -v
```

Expected: `ModuleNotFoundError: No module named 'qmemory.mcp.errors'`

- [ ] **Step 3: Create the errors module**

Create `qmemory/mcp/errors.py`:

```python
"""
MCP error wrapper.

Every MCP tool call in Qmemory is wrapped by safe_tool(). It catches
any exception from the handler and returns a valid MCP tool error
response (isError: true + content block) instead of letting the
exception crash the JSON-RPC transport.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

logger = logging.getLogger(__name__)


async def safe_tool(
    name: str,
    handler: Callable[[Any], Awaitable[Any]],
    validated: BaseModel,
) -> str:
    """Invoke an MCP tool handler with uniform error handling."""
    start = time.monotonic()
    logger.info("Tool call: %s(%s)", name, _scrub_for_log(validated))

    try:
        result = await handler(validated)
    except Exception as exc:
        logger.exception("Unhandled exception in %s", name)
        error_text = (
            f"Internal error in {name}: {type(exc).__name__}. "
            "Check server logs for details."
        )
        return json.dumps(
            {
                "isError": True,
                "content": [{"type": "text", "text": error_text}],
            },
            ensure_ascii=False,
        )

    elapsed = time.monotonic() - start
    logger.info("%s completed in %.2fs", name, elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)


def _scrub_for_log(model: BaseModel) -> dict:
    """Return a short, log-safe dict of the input (truncates long strings)."""
    data = model.model_dump()
    for k, v in list(data.items()):
        if isinstance(v, str) and len(v) > 120:
            data[k] = v[:117] + "..."
    return data
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_mcp/test_errors.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add qmemory/mcp/errors.py tests/test_mcp/__init__.py tests/test_mcp/test_errors.py
git commit -m "feat(mcp): add safe_tool error wrapper"
```

### Task 1.3: Create Pydantic input schemas for all nine tools

**Files:**
- Create: `qmemory/mcp/schemas.py`
- Create: `tests/test_mcp/test_schemas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp/test_schemas.py`:

```python
"""Tests for MCP tool input validation schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qmemory.mcp.schemas import (
    BootstrapInput,
    SearchInput,
    GetInput,
    SaveInput,
    CorrectInput,
    LinkInput,
    PersonInput,
    BooksInput,
    HealthInput,
)


def test_bootstrap_accepts_default_session_key():
    m = BootstrapInput()
    assert m.session_key == "default"


def test_search_rejects_limit_above_50():
    with pytest.raises(ValidationError):
        SearchInput(limit=9999)


def test_search_rejects_invalid_category():
    with pytest.raises(ValidationError):
        SearchInput(category="nonexistent")


def test_search_accepts_valid_category():
    m = SearchInput(category="preference", limit=20)
    assert m.category == "preference"
    assert m.limit == 20


def test_save_rejects_salience_out_of_range():
    with pytest.raises(ValidationError):
        SaveInput(content="fact", category="context", salience=2.5)


def test_save_accepts_boundary_salience():
    m = SaveInput(content="fact", category="context", salience=1.0)
    assert m.salience == 1.0


def test_save_rejects_invalid_evidence_type():
    with pytest.raises(ValidationError):
        SaveInput(
            content="fact",
            category="context",
            evidence_type="hallucinated",
        )


def test_save_strips_whitespace_from_content():
    m = SaveInput(content="  fact  ", category="context")
    assert m.content == "fact"


def test_get_rejects_empty_ids_list():
    with pytest.raises(ValidationError):
        GetInput(ids=[])


def test_get_rejects_more_than_20_ids():
    with pytest.raises(ValidationError):
        GetInput(ids=[f"memory:id{i}" for i in range(21)])


def test_correct_rejects_invalid_action():
    with pytest.raises(ValidationError):
        CorrectInput(memory_id="memory:abc", action="obliterate")


def test_correct_accepts_valid_action():
    m = CorrectInput(memory_id="memory:abc", action="delete")
    assert m.action == "delete"


def test_link_rejects_confidence_above_1():
    with pytest.raises(ValidationError):
        LinkInput(
            from_id="memory:a",
            to_id="memory:b",
            relationship_type="supports",
            confidence=1.5,
        )


def test_person_requires_non_empty_name():
    with pytest.raises(ValidationError):
        PersonInput(name="")


def test_books_accepts_all_none_fields():
    m = BooksInput()
    assert m.book_id is None


def test_health_rejects_invalid_check():
    with pytest.raises(ValidationError):
        HealthInput(check="fake_check_type")


def test_health_accepts_default_all():
    m = HealthInput()
    assert m.check == "all"


def test_schemas_forbid_extra_fields():
    with pytest.raises(ValidationError):
        SearchInput(query="test", made_up_field="nope")
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_mcp/test_schemas.py -v
```

Expected: `ModuleNotFoundError: No module named 'qmemory.mcp.schemas'`

- [ ] **Step 3: Create the schemas module**

Create `qmemory/mcp/schemas.py`:

```python
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
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_mcp/test_schemas.py -v
```

Expected: all 17 tests pass.

- [ ] **Step 5: Commit**

```bash
git add qmemory/mcp/schemas.py tests/test_mcp/test_schemas.py
git commit -m "feat(mcp): add Pydantic input models for all 9 tools"
```

### Task 1.4: Create the operations table

**Files:**
- Create: `qmemory/mcp/operations.py`
- Create: `tests/test_mcp/test_operations.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp/test_operations.py`:

```python
"""Tests for the MCP operations table."""

from __future__ import annotations

from qmemory.mcp.operations import OPERATIONS
from qmemory.mcp.schemas import (
    BootstrapInput,
    SearchInput,
    GetInput,
    SaveInput,
    CorrectInput,
    LinkInput,
    PersonInput,
    BooksInput,
    HealthInput,
)


def test_exactly_nine_operations_defined():
    assert len(OPERATIONS) == 9


def test_all_operation_names_are_unique():
    names = [op.name for op in OPERATIONS]
    assert len(names) == len(set(names))


def test_operation_names_use_qmemory_prefix():
    for op in OPERATIONS:
        assert op.name.startswith("qmemory_"), f"{op.name} missing prefix"


def test_qmemory_import_is_not_in_operations():
    names = [op.name for op in OPERATIONS]
    assert "qmemory_import" not in names


def test_every_operation_has_description():
    for op in OPERATIONS:
        assert op.description
        assert len(op.description) >= 20


def test_every_operation_has_input_model():
    expected = {
        "qmemory_bootstrap": BootstrapInput,
        "qmemory_search": SearchInput,
        "qmemory_get": GetInput,
        "qmemory_save": SaveInput,
        "qmemory_correct": CorrectInput,
        "qmemory_link": LinkInput,
        "qmemory_person": PersonInput,
        "qmemory_books": BooksInput,
        "qmemory_health": HealthInput,
    }
    for op in OPERATIONS:
        assert op.input_model is expected[op.name]


def test_read_only_tools_have_correct_annotations():
    read_only = {
        "qmemory_bootstrap",
        "qmemory_search",
        "qmemory_get",
        "qmemory_books",
        "qmemory_health",
    }
    for op in OPERATIONS:
        if op.name in read_only:
            assert op.annotations.readOnlyHint is True
            assert op.annotations.destructiveHint is False


def test_correct_tool_has_destructive_hint():
    for op in OPERATIONS:
        if op.name == "qmemory_correct":
            assert op.annotations.destructiveHint is True
            return
    raise AssertionError("qmemory_correct not found")
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_mcp/test_operations.py -v
```

Expected: `ModuleNotFoundError: No module named 'qmemory.mcp.operations'`

- [ ] **Step 3: Create the operations module**

Create `qmemory/mcp/operations.py`:

```python
"""
Qmemory MCP operations table — single source of truth.

All 9 tools are declared here once. Both transports mount these
via registry.mount_operations(). Core business logic lives in
qmemory/core/*; handlers here are thin lambdas that call core
functions and return dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from qmemory.mcp import schemas


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    input_model: type[BaseModel]
    annotations: ToolAnnotations
    handler: Callable[[Any], Awaitable[dict]]


async def _bootstrap(i: schemas.BootstrapInput) -> dict:
    from qmemory.core.recall import assemble_context
    return await assemble_context(i.session_key)


async def _search(i: schemas.SearchInput) -> dict:
    from qmemory.core.search import search_memories
    return await search_memories(
        query_text=i.query,
        category=i.category,
        scope=i.scope,
        limit=i.limit,
        offset=i.offset,
        after=i.after,
        before=i.before,
        include_tool_calls=i.include_tool_calls,
        source_type=i.source_type,
        entity_id=i.entity_id,
    )


async def _get(i: schemas.GetInput) -> dict:
    from qmemory.core.get import get_memories
    return await get_memories(
        ids=i.ids,
        include_neighbors=i.include_neighbors,
        neighbor_depth=i.neighbor_depth,
    )


async def _save(i: schemas.SaveInput) -> dict:
    from qmemory.core.save import save_memory
    return await save_memory(
        content=i.content,
        category=i.category,
        salience=i.salience,
        scope=i.scope,
        confidence=i.confidence,
        source_person=i.source_person,
        evidence_type=i.evidence_type,
        context_mood=i.context_mood,
    )


async def _correct(i: schemas.CorrectInput) -> dict:
    from qmemory.core.correct import correct_memory
    return await correct_memory(
        memory_id=i.memory_id,
        action=i.action,
        new_content=i.new_content,
        updates=i.updates,
        edge_id=i.edge_id,
        reason=i.reason,
    )


async def _link(i: schemas.LinkInput) -> dict:
    from qmemory.core.link import link_nodes
    return await link_nodes(
        from_id=i.from_id,
        to_id=i.to_id,
        relationship_type=i.relationship_type,
        reason=i.reason,
        confidence=i.confidence,
    )


async def _person(i: schemas.PersonInput) -> dict:
    from qmemory.core.person import create_person
    return await create_person(
        name=i.name,
        aliases=i.aliases,
        contacts=i.contacts,
    )


async def _books(i: schemas.BooksInput) -> dict:
    from qmemory.core.books import list_books, list_sections, read_section

    if i.book_id and i.section:
        return await read_section(book_id=i.book_id, section=i.section)
    if i.book_id:
        return await list_sections(book_id=i.book_id)
    return await list_books(query_text=i.query)


async def _health(i: schemas.HealthInput) -> dict:
    from qmemory.core.health import get_latest_report

    result = await get_latest_report(check=i.check)
    if result is None:
        return {
            "status": "no_report",
            "message": "No health report found. Worker must run first.",
            "actions": [
                {
                    "tool": "shell",
                    "command": "qmemory worker --once",
                    "description": "Generate a health report",
                }
            ],
        }
    return result


OPERATIONS: list[Operation] = [
    Operation(
        name="qmemory_bootstrap",
        description=(
            "Load your full memory context at conversation start. "
            "Returns self-model, cross-session memories grouped by category, "
            "graph map, and session info. Call once at the START of every conversation."
        ),
        input_model=schemas.BootstrapInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_bootstrap,
    ),
    Operation(
        name="qmemory_search",
        description=(
            "Search cross-session memory by meaning, category, or scope. "
            "Returns memories grouped by category with graph context. "
            "Use qmemory_get if you already have memory IDs. "
            "Use qmemory_books to browse book knowledge hierarchically."
        ),
        input_model=schemas.SearchInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_search,
    ),
    Operation(
        name="qmemory_get",
        description=(
            "Fetch memories or entities by ID with optional graph neighbor traversal. "
            "Use qmemory_search if you only have a query text, not IDs."
        ),
        input_model=schemas.GetInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_get,
    ),
    Operation(
        name="qmemory_save",
        description=(
            "Save a fact to cross-session memory with evidence tracking. "
            "Runs deduplication automatically - returns ADD/UPDATE/NOOP action."
        ),
        input_model=schemas.SaveInput,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=_save,
    ),
    Operation(
        name="qmemory_correct",
        description=(
            "Fix or delete a memory. Soft-delete only - preserves audit trail. "
            "Actions: correct (new version), delete (soft), update (metadata), unlink (remove edge)."
        ),
        input_model=schemas.CorrectInput,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=_correct,
    ),
    Operation(
        name="qmemory_link",
        description=(
            "Create a relationship edge between any two nodes in the memory graph. "
            "Relationship type is free-form (supports, contradicts, caused_by, etc.)."
        ),
        input_model=schemas.LinkInput,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_link,
    ),
    Operation(
        name="qmemory_person",
        description=(
            "Create or find a person entity with linked identities across systems "
            "(Telegram, email, WhatsApp, etc.). Returns existing if found."
        ),
        input_model=schemas.PersonInput,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_person,
    ),
    Operation(
        name="qmemory_books",
        description=(
            "Browse books in your knowledge library hierarchically: "
            "list books, see sections, read section. "
            "Use qmemory_search(source_type='from_book') for keyword search across all books."
        ),
        input_model=schemas.BooksInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_books,
    ),
    Operation(
        name="qmemory_health",
        description=(
            "Read the latest worker health report: orphans, stale facts, "
            "missing links, quality issues, coverage gaps. Worker must run first."
        ),
        input_model=schemas.HealthInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_health,
    ),
]
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_mcp/test_operations.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add qmemory/mcp/operations.py tests/test_mcp/test_operations.py
git commit -m "feat(mcp): add operations table - single source of truth for 9 tools"
```

### Task 1.5: Create the registry helper

**Files:**
- Create: `qmemory/mcp/registry.py`
- Create: `tests/test_mcp/test_registry.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp/test_registry.py`:

```python
"""Tests for the FastMCP mount helper."""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from qmemory.mcp.operations import OPERATIONS
from qmemory.mcp.registry import mount_operations


def test_mount_registers_nine_tools():
    mcp = FastMCP("test_mount")
    mount_operations(mcp, OPERATIONS)
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 9


def test_mount_tool_names_match_operations():
    mcp = FastMCP("test_mount_names")
    mount_operations(mcp, OPERATIONS)
    tools = asyncio.run(mcp.list_tools())
    registered = {t.name for t in tools}
    expected = {op.name for op in OPERATIONS}
    assert registered == expected
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_mcp/test_registry.py -v
```

Expected: `ModuleNotFoundError: No module named 'qmemory.mcp.registry'`

- [ ] **Step 3: Create the registry module**

Create `qmemory/mcp/registry.py`:

```python
"""
FastMCP mount helper — loops OPERATIONS and registers each tool on
the given FastMCP server via the official SDK's @mcp.tool() decorator.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from qmemory.mcp.errors import safe_tool
from qmemory.mcp.operations import Operation


def mount_operations(mcp: FastMCP, operations: list[Operation]) -> None:
    """Register every Operation as a tool on the given server."""
    for op in operations:
        _register_one(mcp, op)


def _register_one(mcp: FastMCP, op: Operation) -> None:
    """Register a single Operation with the FastMCP server.

    Uses inspect.Signature to give the wrapper the same parameters
    as the input model, so FastMCP's schema introspection produces
    the right JSON schema.
    """
    input_model = op.input_model
    fields = input_model.model_fields

    sig_params: list[inspect.Parameter] = []
    for name, field in fields.items():
        if field.is_required():
            default = inspect.Parameter.empty
        else:
            default = field.default if field.default is not None else None
        sig_params.append(
            inspect.Parameter(
                name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=field.annotation,
            )
        )

    async def wrapper(**kwargs: Any) -> str:
        try:
            validated = input_model(**kwargs)
        except Exception as exc:
            return json.dumps(
                {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": f"Invalid arguments for {op.name}: {exc}",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        return await safe_tool(name=op.name, handler=op.handler, validated=validated)

    wrapper.__name__ = op.name
    wrapper.__doc__ = op.description
    wrapper.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=sig_params, return_annotation=str
    )

    mcp.tool(
        name=op.name,
        description=op.description,
        annotations=op.annotations,
    )(wrapper)
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_mcp/test_registry.py -v
```

Expected: 2 tests pass. If FastMCP SDK's `list_tools()` API differs, adjust the test — check via:

```bash
uv run python -c "from mcp.server.fastmcp import FastMCP; import asyncio; m = FastMCP('t'); print([x.name for x in asyncio.run(m.list_tools())])"
```

- [ ] **Step 5: Commit**

```bash
git add qmemory/mcp/registry.py tests/test_mcp/test_registry.py
git commit -m "feat(mcp): add mount_operations registry helper"
```

### Task 1.6: Rewrite the stdio entry point

**Files:**
- Modify: `qmemory/mcp/server.py` (full rewrite to ~20 lines)

- [ ] **Step 1: Replace `qmemory/mcp/server.py` entirely**

Overwrite `qmemory/mcp/server.py`:

```python
"""
Qmemory MCP Server (stdio transport).

Local FastMCP server for Claude Code and developer use. All tool
definitions come from qmemory/mcp/operations.py via mount_operations().
Edit that file to change any tool.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from qmemory.mcp.operations import OPERATIONS
from qmemory.mcp.registry import mount_operations

mcp = FastMCP(
    "qmemory_mcp",
    instructions=(
        "Graph memory for AI agents. "
        "Call qmemory_bootstrap first to load your full memory context. "
        "Then use qmemory_search to find specific memories, qmemory_save to "
        "record new facts, qmemory_correct to fix errors, qmemory_link to "
        "create relationships between knowledge nodes, and qmemory_person to "
        "manage person entities."
    ),
)

mount_operations(mcp, OPERATIONS)
```

- [ ] **Step 2: Run the full test suite**

```bash
uv run pytest tests/ -x --tb=short
```

Expected: all tests pass.

- [ ] **Step 3: Smoke-test the stdio entry point imports cleanly**

```bash
uv run python -c "from qmemory.mcp.server import mcp; print(mcp.name)"
```

Expected: prints `qmemory_mcp`.

- [ ] **Step 4: Commit**

```bash
git add qmemory/mcp/server.py
git commit -m "refactor(mcp): shrink stdio server to 20 lines via mount_operations"
```

### Task 1.7: Rewrite the HTTP entry point

**Files:**
- Modify: `qmemory/app/main.py` (delete tool definitions, import from `mcp.server.fastmcp`, call `mount_operations`, fix CORS)

- [ ] **Step 1: Replace `qmemory/app/main.py`**

Overwrite `qmemory/app/main.py`:

```python
"""
Qmemory Cloud — FastAPI + FastMCP HTTP Server.

HTTP entry point for Qmemory Cloud. Tool definitions live in
qmemory/mcp/operations.py. Mounts the FastMCP HTTP sub-app at /mcp.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from mcp.server.fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from qmemory.app.config import get_app_settings
from qmemory.app.routes.auth import get_session_user, router as auth_router
from qmemory.app.routes.connect import router as connect_router
from qmemory.app.routes.dashboard import router as dashboard_router
from qmemory.app.routes.memories import router as memories_router
from qmemory.app.routes.tokens import router as tokens_router
from qmemory.db.client import is_healthy
from qmemory.mcp.operations import OPERATIONS
from qmemory.mcp.registry import mount_operations

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

mcp = FastMCP(
    "qmemory_mcp",
    instructions=(
        "Graph memory for AI agents. "
        "Call qmemory_bootstrap first to load your full memory context. "
        "Then use qmemory_search to find specific memories, qmemory_save to "
        "record new facts, qmemory_correct to fix errors, qmemory_link to "
        "create relationships between knowledge nodes, and qmemory_person to "
        "manage person entities."
    ),
)

mount_operations(mcp, OPERATIONS)

settings = get_app_settings()

mcp_app = mcp.streamable_http_app()

api = FastAPI(
    title="Qmemory Cloud",
    version="1.0.0",
    description="Graph-based memory for AI agents - HTTP API",
    debug=settings.debug,
    lifespan=mcp_app.lifespan,
)

api.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=604800,
    session_cookie="qmemory_session",
    same_site="lax",
    https_only=False,
)

api.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://claude.ai",
        "https://claude.com",
        "https://anthropic.com",
        "https://www.anthropic.com",
    ],
    allow_origin_regex=r"https://.*\.anthropic\.com",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
    max_age=86400,
)
logger.info("CORS middleware enabled - explicit claude.ai/anthropic.com origins")

api.include_router(auth_router)
api.include_router(connect_router)
api.include_router(dashboard_router)
api.include_router(memories_router)
api.include_router(tokens_router)


@api.get("/")
async def root_redirect(request: Request):
    user = get_session_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@api.get("/health")
async def health_check():
    start = time.monotonic()
    db_ok = await is_healthy()
    elapsed = time.monotonic() - start
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "unreachable",
        "response_time_ms": round(elapsed * 1000, 1),
    }


api.mount("/mcp", mcp_app)
logger.info("Qmemory Cloud app created - MCP mounted at /mcp/ (no auth yet)")
```

- [ ] **Step 2: Verify imports resolve**

```bash
uv run python -c "from qmemory.app.main import api; print('routes:', len(api.routes))"
```

Expected: prints `routes: N` where N >= 10.

- [ ] **Step 3: Verify no jlowin fastmcp imports remain**

```bash
grep -rn "^import fastmcp\|^from fastmcp" qmemory/ tests/
```

Expected: no matches.

- [ ] **Step 4: Run the full test suite**

```bash
uv run pytest tests/ -x --tb=short
```

Expected: all tests pass.

- [ ] **Step 5: Manual HTTP smoke test**

Terminal A:
```bash
uv run uvicorn qmemory.app.main:api --port 3777 --log-level info
```

Terminal B:
```bash
curl -s -X POST http://localhost:3777/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python -m json.tool | head -50
```

Expected: JSON with `result.tools` containing 9 tools. `qmemory_import` must NOT be present.

Stop uvicorn (Ctrl-C).

- [ ] **Step 6: Commit**

```bash
git add qmemory/app/main.py
git commit -m "refactor(mcp): rewrite HTTP entry point to use mount_operations + fix CORS"
```

### Task 1.8: Update CLAUDE.md

**Files:** `CLAUDE.md`

- [ ] **Step 1: Edit CLAUDE.md**

Find the section "MCP Tools (10 total)" and change it to "MCP Tools (9 total)". Delete the row for `qmemory_import`. Replace the "IMPORTANT" paragraph about tools being in TWO places with:

```markdown
**IMPORTANT**: All tool definitions live in a single place — `qmemory/mcp/operations.py`. Both transports (stdio via `qmemory/mcp/server.py` and HTTP via `qmemory/app/main.py`) mount from the same OPERATIONS table via `qmemory.mcp.registry.mount_operations()`. Edit operations.py once; both transports pick it up.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md reflects single-source-of-truth operations table"
```

**Phase 1 checkpoint — run full suite before moving on:**

```bash
uv run pytest tests/ -v 2>&1 | tail -20
```

All tests must pass.

---

# Phase 2 — Multi-User Isolation

**Phase exit criteria:** `/mcp/u/{code}/tools/list` returns 9 tools when the code is valid, 404 otherwise. Two signed-up users see zero memory overlap. The legacy `/mcp/` endpoint still works at this phase and points at the default DB.

### Task 2.1: Create the admin database schema

**Files:** Create `qmemory/db/admin_schema.surql`

- [ ] **Step 1: Create the schema file**

```sql
-- Qmemory admin database schema.
-- Only one table: user. Lives in qmemory.admin database.

DEFINE TABLE IF NOT EXISTS user SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS user_code ON user TYPE string
    ASSERT string::len($value) BETWEEN 4 AND 64
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS display_name ON user TYPE string
    ASSERT string::len($value) BETWEEN 1 AND 128
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS db_name ON user TYPE string
    ASSERT string::len($value) BETWEEN 4 AND 128
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS created_at ON user TYPE datetime
    VALUE $value OR time::now()
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS last_active_at ON user TYPE option<datetime>
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS is_active ON user TYPE bool
    DEFAULT true
    PERMISSIONS NONE;

DEFINE INDEX IF NOT EXISTS idx_user_code ON user FIELDS user_code UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_user_is_active ON user FIELDS is_active;
```

- [ ] **Step 2: Commit**

```bash
git add qmemory/db/admin_schema.surql
git commit -m "feat(db): add admin_schema.surql - user table for routing directory"
```

### Task 2.2: Add `get_admin_db()` and `apply_admin_schema()` helpers

**Files:**
- Modify: `qmemory/db/client.py`
- Create: `tests/test_db/test_admin_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_db/test_admin_db.py`:

```python
"""Tests for the admin database connection helper."""

from __future__ import annotations

import pytest

from qmemory.db.client import apply_admin_schema, get_admin_db, query


@pytest.fixture
async def clean_admin():
    yield
    async with get_admin_db(database="admin_test") as conn:
        await query(conn, "REMOVE TABLE IF EXISTS user")


async def test_get_admin_db_connects_to_admin_database(clean_admin):
    async with get_admin_db(database="admin_test") as conn:
        result = await query(conn, "RETURN 1")
    assert result == 1


async def test_apply_admin_schema_creates_user_table(clean_admin):
    async with get_admin_db(database="admin_test") as conn:
        await apply_admin_schema(conn)
        await query(
            conn,
            """CREATE user SET
                user_code = 'test-abc12',
                display_name = 'Test User',
                db_name = 'user_test-abc12'""",
        )
        result = await query(
            conn,
            "SELECT user_code, display_name, db_name, is_active FROM user WHERE user_code = 'test-abc12'",
        )
    assert result is not None
    assert len(result) == 1
    assert result[0]["user_code"] == "test-abc12"
    assert result[0]["is_active"] is True
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest tests/test_db/test_admin_db.py -v
```

Expected: `ImportError: cannot import name 'apply_admin_schema'`

- [ ] **Step 3: Add helpers to `qmemory/db/client.py`**

Find the `apply_schema` function (around line 268). Directly below it, add:

```python
async def apply_admin_schema(db: Any) -> None:
    """Apply the admin schema (user table only) to the current database."""
    schema_path = Path(__file__).parent / "admin_schema.surql"
    if not schema_path.exists():
        raise FileNotFoundError(f"admin_schema.surql not found at {schema_path}")

    surql = schema_path.read_text()
    logger.info("Applying admin schema from %s", schema_path.name)
    await db.query(surql)
```

Find the `get_db` function (around line 46). Directly below it (after its `finally`/`pass` block), add:

```python
@asynccontextmanager
async def get_admin_db(database: str = "admin"):
    """Connect to the qmemory.admin database (never uses _user_db)."""
    settings = get_settings()
    db = AsyncSurreal(settings.surreal_url)
    try:
        await db.connect()
        await db.signin({
            "username": settings.surreal_user,
            "password": settings.surreal_pass,
        })
        await db.use(settings.surreal_ns, database)
        yield db
    finally:
        try:
            await db.close()
        except Exception:
            pass
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_db/test_admin_db.py -v
```

Expected: 2 tests pass (requires running SurrealDB).

- [ ] **Step 5: Commit**

```bash
git add qmemory/db/client.py tests/test_db/test_admin_db.py
git commit -m "feat(db): add get_admin_db() and apply_admin_schema()"
```

### Task 2.3: Add the EFF word list data files

**Files:**
- Create: `qmemory/app/data/__init__.py`
- Download: `qmemory/app/data/eff_large_wordlist.txt`
- Create: `qmemory/app/data/excluded_words.txt`

- [ ] **Step 1: Create the data package**

```bash
mkdir -p qmemory/app/data
touch qmemory/app/data/__init__.py
```

- [ ] **Step 2: Download the EFF word list**

```bash
curl -sSL -o qmemory/app/data/eff_large_wordlist.txt \
    https://www.eff.org/files/2016/07/18/eff_large_wordlist.txt
wc -l qmemory/app/data/eff_large_wordlist.txt
head -3 qmemory/app/data/eff_large_wordlist.txt
```

Expected: 7776 lines. Format: `11111<TAB>abacus`.

- [ ] **Step 3: Create the exclusion list**

The exclusion list is a one-off manual curation step. Start with this set of ~250 negative-tone words and add more by skimming the EFF list once. Create `qmemory/app/data/excluded_words.txt`:

```
abrasive
abrupt
absurd
abuse
abuser
abusive
abysmal
ache
acidic
acne
acrid
adverse
afraid
agoniz
aimless
alarm
alcoholic
alien
allergic
aloof
amiss
angry
anguish
annoy
apathy
appall
arrogant
ashamed
assault
attack
awful
awkward
backlash
backstab
badge
banal
banish
bankrupt
barbaric
bark
belittle
bitter
bleak
blindly
bloody
boring
brash
breakup
broken
brutal
burn
cancer
cheat
coward
crabby
cranky
crazy
creepy
crime
crisis
crooked
crude
cruel
cry
curse
damaged
damn
dead
death
debt
decay
deceit
deceptive
defect
defile
deform
deject
delude
demolish
deplore
depression
deprive
deride
desecrate
deserted
despair
despise
destroy
devastate
die
dirty
disaster
discard
disease
disfigure
disgrace
disgust
dishonest
dishonor
disloyal
dismal
dismay
disobey
disrupt
distort
distress
ditch
doom
dread
dropout
drown
drunk
dull
dump
dungeon
dusty
embitter
enmesh
enslave
enthrall
envy
erase
error
evade
evict
evil
expel
expire
explode
exploit
extinct
fail
faint
fake
famine
farce
fear
feeble
fever
fight
filth
firebomb
firing
flee
flimsy
flunk
foolish
forbid
foul
fragile
fraud
freak
frighten
fume
fungus
furious
fussy
gag
gangster
gaunt
germ
ghost
ghoul
goof
gore
grave
greasy
greed
grief
grim
grimy
grouch
grudge
gruesome
grumpy
guilt
hack
haggle
halt
harm
harsh
hateful
haunt
havoc
hazard
headache
heartbreak
heartless
heist
hell
helpless
hideout
hinder
hives
hoard
hobble
hollow
homeless
horror
hostage
hostile
howl
humiliate
hunger
hurt
ignore
illicit
illness
illogical
imbalance
impair
impale
impend
impose
impure
inadequate
indecent
infamy
infect
infirm
inflame
injure
injustice
insane
insolent
intrude
invalid
irate
jeopardy
jerk
jilt
junk
kidnap
kill
lash
lazy
leakage
lethal
liar
lifeless
limp
listless
litter
loathe
lonely
loot
loser
loss
louse
lousy
lull
lust
madden
mafia
maggot
malice
malicious
malign
mangle
mania
maniac
massacre
mean
measly
meddle
menace
mess
misdeed
miser
misfortune
misery
mishap
mislead
mistake
mistrust
moan
mold
mongrel
moody
mope
morbid
moron
mourn
muck
murder
mushy
mutilate
nagging
nasty
naughty
nauseate
negate
neglect
nemesis
nervous
neurotic
nitpick
noisy
nonsense
nuisance
numb
obese
obsolete
obstruct
oddity
offend
omit
oppress
outrage
overbear
overflow
overload
overspend
overwork
pagan
pain
pale
panic
paranoid
parasite
pariah
peril
pester
petty
pillage
pity
plague
plot
plunder
poison
poor
poverty
powerless
prank
prejudice
prickly
problem
profane
protest
prowl
punish
puny
purge
putrid
quarrel
racist
rage
raid
rampage
ransack
ransom
rant
rash
ravage
recede
regret
reject
relinquish
remorse
remove
reproach
repugnant
repulsive
resent
resign
revenge
revile
revolt
ridicule
rigid
riot
risk
rob
rot
rough
rubble
rude
rumble
rumor
rust
sabotage
sad
sadistic
savage
scab
scam
scar
scare
scathe
scheme
scold
scorch
scorn
scoundrel
scrape
scream
screw
scum
seethe
segregate
seize
selfish
senseless
severe
shabby
sham
shame
shark
shatter
shiver
shrill
shun
sick
sickly
silly
sinful
sinister
skirmish
skull
slack
slam
slander
slap
slash
slaughter
sleazy
slime
slob
sloppy
sloth
slug
slum
slur
sly
smash
smell
smelly
smother
smug
snatch
sneak
snide
snob
sob
soggy
soil
solemn
solitary
sorrow
sorry
sour
spank
spew
spike
spite
splinter
spoil
spook
spray
spur
squall
squander
squat
stab
stall
stamp
stark
starve
steal
steep
stench
stern
stiff
stigma
sting
stink
stomp
storm
strangle
strip
struggle
stumble
stung
stupid
stupor
subdue
suffer
suicide
sulk
sultry
sunken
surge
suspicious
swamp
swarm
swat
swelter
swipe
swollen
symptom
tangle
tantrum
tarnish
taunt
tear
temper
tempt
tense
terror
theft
thief
thirst
thorn
threat
thud
thug
timid
tired
torment
torn
tornado
torture
toss
toxic
tragic
trap
trauma
treachery
tremor
trick
trivial
trouble
truant
trudge
tumor
turmoil
tussle
twist
tyrant
ugly
ulcer
unable
unaware
uneasy
unemployed
unending
unfair
unfit
unhappy
unhealthy
unholy
unjust
unkind
unknown
unlawful
unloved
unsafe
untamed
untidy
untimely
untreated
untrue
unwary
unwilling
unwise
uproar
upset
urgency
useless
vampire
vandal
vanish
vanity
venom
vermin
vex
vice
victim
vile
villain
violate
virus
void
volcano
vomit
voodoo
vulgar
vulture
wail
wallow
war
warrior
waste
weak
weary
weep
weird
whack
wheezy
whine
whip
widow
wiggle
wilt
wimp
wince
wish
wither
woe
worm
worry
worse
worsen
wound
wrangle
wrath
wreck
wrench
wretch
wrinkle
wrong
yell
yelp
yowl
zombie
```

- [ ] **Step 4: Commit**

```bash
git add qmemory/app/data/
git commit -m "feat(app): add EFF word list and exclusion filter for user codes"
```

### Task 2.4: User code generator

**Files:**
- Create: `qmemory/app/wordlist.py`
- Create: `qmemory/app/user_code.py`
- Create: `tests/test_app/__init__.py`
- Create: `tests/test_app/test_user_code.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_app/__init__.py` (empty).

Create `tests/test_app/test_user_code.py`:

```python
"""Tests for user code generation."""

from __future__ import annotations

import re

from qmemory.app.user_code import generate_user_code
from qmemory.app.wordlist import WORDLIST


def test_wordlist_loaded_with_thousands_of_words():
    assert len(WORDLIST) >= 6500
    assert len(WORDLIST) <= 7800


def test_wordlist_excludes_negative_words():
    for bad in ("abrasive", "abrupt", "doom", "zombie"):
        assert bad not in WORDLIST


def test_generate_user_code_matches_pattern():
    for _ in range(50):
        code = generate_user_code()
        assert re.match(r"^[a-z]+-[a-z0-9]{5}$", code), f"bad code: {code}"


def test_generate_user_code_has_varied_words():
    codes = {generate_user_code().split("-")[0] for _ in range(50)}
    assert len(codes) >= 20
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_app/test_user_code.py -v
```

- [ ] **Step 3: Create wordlist.py**

Create `qmemory/app/wordlist.py`:

```python
"""Word list loader for user code generation."""

from __future__ import annotations

from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
_EFF_PATH = _DATA_DIR / "eff_large_wordlist.txt"
_EXCLUDED_PATH = _DATA_DIR / "excluded_words.txt"


def _load_wordlist() -> list[str]:
    excluded = {
        line.strip()
        for line in _EXCLUDED_PATH.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    words: list[str] = []
    for line in _EFF_PATH.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            word = parts[1].strip()
            if word and word not in excluded:
                words.append(word)
    return words


WORDLIST: list[str] = _load_wordlist()
```

- [ ] **Step 4: Create user_code.py**

Create `qmemory/app/user_code.py`:

```python
"""
User code generator for /mcp/u/{code}/ URLs.

Format: {word}-{5 lowercase alphanumeric chars}
Example: abacus-k7m3p
"""

from __future__ import annotations

import secrets
import string

from qmemory.app.wordlist import WORDLIST

_SUFFIX_CHARS = string.ascii_lowercase + string.digits


def generate_user_code() -> str:
    """Return a new random user code. Does NOT check uniqueness."""
    word = secrets.choice(WORDLIST)
    suffix = "".join(secrets.choice(_SUFFIX_CHARS) for _ in range(5))
    return f"{word}-{suffix}"


async def generate_unique_user_code(max_attempts: int = 10) -> str:
    """Generate a code that does not collide with an existing user row."""
    from qmemory.db.client import get_admin_db, query

    for _ in range(max_attempts):
        code = generate_user_code()
        async with get_admin_db() as db:
            rows = await query(
                db,
                "SELECT id FROM user WHERE user_code = $code",
                {"code": code},
            )
        if not rows:
            return code
    raise RuntimeError(
        f"Could not generate unique user code after {max_attempts} attempts"
    )
```

- [ ] **Step 5: Run — expect pass**

```bash
uv run pytest tests/test_app/test_user_code.py -v
```

Expected: 4 tests pass.

- [ ] **Step 6: Commit**

```bash
git add qmemory/app/wordlist.py qmemory/app/user_code.py tests/test_app/__init__.py tests/test_app/test_user_code.py
git commit -m "feat(app): add user code generator"
```

### Task 2.5: MCPUserMiddleware

**Files:**
- Create: `qmemory/app/middleware/__init__.py`
- Create: `qmemory/app/middleware/user_context.py`
- Create: `tests/test_app/test_user_middleware.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_app/test_user_middleware.py`:

```python
"""Tests for MCPUserMiddleware path rewriting and routing."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from qmemory.app.middleware.user_context import MCPUserMiddleware
from qmemory.db.client import _user_db, apply_admin_schema, get_admin_db, query


@pytest.fixture
async def admin_with_test_user():
    async with get_admin_db(database="admin_test") as conn:
        await apply_admin_schema(conn)
        await query(
            conn,
            """CREATE user SET
                user_code = 'test-abc12',
                display_name = 'Tester',
                db_name = 'user_test-abc12',
                is_active = true""",
        )
    yield "test-abc12"
    async with get_admin_db(database="admin_test") as conn:
        await query(conn, "REMOVE TABLE IF EXISTS user")


@pytest.fixture
def probe_app(monkeypatch):
    monkeypatch.setattr(
        "qmemory.app.middleware.user_context._ADMIN_DB_NAME",
        "admin_test",
    )
    app = FastAPI()
    app.add_middleware(MCPUserMiddleware)

    @app.get("/mcp/{rest:path}")
    async def probe(rest: str):
        return JSONResponse({"seen_path": f"/mcp/{rest}", "user_db": _user_db.get()})

    return app


async def test_unknown_user_code_returns_404(admin_with_test_user, probe_app):
    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://test") as c:
        r = await c.get("/mcp/u/no-such-user/tools/list")
    assert r.status_code == 404


async def test_known_user_code_rewrites_path_and_sets_context(admin_with_test_user, probe_app):
    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://test") as c:
        r = await c.get("/mcp/u/test-abc12/tools/list")
    assert r.status_code == 200
    data = r.json()
    assert data["seen_path"] == "/mcp/tools/list"
    assert data["user_db"] == "user_test-abc12"


async def test_non_user_scoped_mcp_path_passes_through(admin_with_test_user, probe_app):
    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://test") as c:
        r = await c.get("/mcp/tools/list")
    assert r.status_code == 200
    data = r.json()
    assert data["seen_path"] == "/mcp/tools/list"
    assert data["user_db"] is None
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_app/test_user_middleware.py -v
```

- [ ] **Step 3: Create the middleware package**

```bash
mkdir -p qmemory/app/middleware
touch qmemory/app/middleware/__init__.py
```

Create `qmemory/app/middleware/user_context.py`:

```python
"""
MCPUserMiddleware - extracts user_code from /mcp/u/{code}/ URLs,
resolves it to a database name via the admin DB, and sets the
_user_db ContextVar for the duration of the request.

Path rewriting:
    Incoming:  /mcp/u/calm-k7m3p/tools/list
    Forwarded: /mcp/tools/list
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from qmemory.db.client import _user_db, get_admin_db, query

logger = logging.getLogger(__name__)

_USER_PATH_RE = re.compile(r"^/mcp/u/([a-z0-9-]+)(/.*)?$")

_ADMIN_DB_NAME: str = "admin"


class MCPUserMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        match = _USER_PATH_RE.match(path)
        if not match:
            return await call_next(request)

        user_code = match.group(1)
        tail = match.group(2) or "/"

        async with get_admin_db(database=_ADMIN_DB_NAME) as admin:
            rows = await query(
                admin,
                "SELECT db_name, is_active FROM user WHERE user_code = $code",
                {"code": user_code},
            )

        if not rows or not rows[0].get("is_active", False):
            logger.info("404 for unknown user_code: %s", user_code)
            return JSONResponse({"error": "not_found"}, status_code=404)

        db_name = rows[0]["db_name"]
        logger.info("MCP request for user %s -> db=%s", user_code, db_name)

        rewritten = "/mcp" + tail
        request.scope["path"] = rewritten
        request.scope["raw_path"] = rewritten.encode()

        token = _user_db.set(db_name)
        try:
            response = await call_next(request)
        finally:
            _user_db.reset(token)

        asyncio.create_task(_touch_user(user_code))

        return response


async def _touch_user(user_code: str) -> None:
    try:
        async with get_admin_db(database=_ADMIN_DB_NAME) as admin:
            await query(
                admin,
                "UPDATE user SET last_active_at = time::now() WHERE user_code = $code",
                {"code": user_code},
            )
    except Exception:
        logger.debug("last_active_at update failed for %s", user_code, exc_info=True)
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_app/test_user_middleware.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add qmemory/app/middleware/ tests/test_app/test_user_middleware.py
git commit -m "feat(app): add MCPUserMiddleware for per-user DB routing"
```

### Task 2.6: Wire middleware into main.py

**Files:** Modify `qmemory/app/main.py`

- [ ] **Step 1: Add import and middleware registration**

At the top of `qmemory/app/main.py` with the other imports, add:

```python
from qmemory.app.middleware.user_context import MCPUserMiddleware
```

After the `api.add_middleware(CORSMiddleware, ...)` call, add:

```python
api.add_middleware(MCPUserMiddleware)
logger.info("MCPUserMiddleware registered - /mcp/u/{code}/ routes are live")
```

- [ ] **Step 2: Run the full test suite**

```bash
uv run pytest tests/ -x --tb=short
```

Expected: all tests pass.

- [ ] **Step 3: Manual smoke test — both legacy and user paths**

Terminal A:
```bash
uv run uvicorn qmemory.app.main:api --port 3777
```

Terminal B:
```bash
# Legacy path - still works
curl -s -X POST http://localhost:3777/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python -m json.tool | head -20

# Unknown user - 404
curl -sv -X POST http://localhost:3777/mcp/u/no-such-user/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>&1 | tail -10
```

Expected: legacy returns JSON, unknown returns HTTP 404.

Stop uvicorn.

- [ ] **Step 4: Commit**

```bash
git add qmemory/app/main.py
git commit -m "feat(app): wire MCPUserMiddleware into HTTP pipeline"
```

### Task 2.7: Rewrite the signup route as zero-friction

**Files:**
- Modify or create: `qmemory/app/routes/signup.py`
- Modify: `qmemory/app/templates/connect.html`
- Create: `tests/test_app/test_signup_route.py`

- [ ] **Step 1: Locate existing signup logic**

```bash
grep -rn "signup\|sign_up\|POST.*signup" qmemory/app/routes/ 2>&1 | head -20
```

If signup logic lives in `auth.py`, read that file to understand its current shape, then move it to `signup.py`.

- [ ] **Step 2: Create/rewrite `qmemory/app/routes/signup.py`**

```python
"""
Zero-friction signup route.

Flow:
    GET  /signup          — form asking for display name only
    POST /signup          — generate user_code, provision DB, redirect
    GET  /connect?code=X  — show personal URL with loss warning
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from qmemory.app.user_code import generate_unique_user_code
from qmemory.db.client import apply_admin_schema, get_admin_db, query
from qmemory.db.provision import provision_user_db

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="qmemory/app/templates")


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request})


@router.post("/signup")
async def signup_submit(request: Request, display_name: str = Form(...)):
    display_name = display_name.strip()
    if not display_name:
        return RedirectResponse(url="/signup?error=missing_name", status_code=303)

    async with get_admin_db() as admin:
        await apply_admin_schema(admin)

    user_code = await generate_unique_user_code()
    logger.info("New signup: %s -> %s", display_name, user_code)

    db_name = await provision_user_db(user_code)

    async with get_admin_db() as admin:
        await query(
            admin,
            """CREATE user SET
                user_code = $code,
                display_name = $name,
                db_name = $db_name,
                is_active = true""",
            {"code": user_code, "name": display_name, "db_name": db_name},
        )

    return RedirectResponse(url=f"/connect?code={user_code}", status_code=303)
```

- [ ] **Step 3: Update the signup template**

Create or update `qmemory/app/templates/signup.html` to ask only for display name. Minimal version:

```html
{% extends "base.html" %}
{% block content %}
<div class="signup">
  <h1>Sign up for Qmemory</h1>
  <p>Zero friction. No email. Just pick a display name and get your personal URL.</p>
  <p><strong>Warning:</strong> Save the URL when you receive it. If you lose it, you lose your memories. There is no recovery.</p>
  <form method="post" action="/signup">
    <label>Display name: <input type="text" name="display_name" required minlength="1" maxlength="128"></label>
    <button type="submit">Create my memory graph</button>
  </form>
</div>
{% endblock %}
```

- [ ] **Step 4: Update connect.html to show the personal URL**

Add to `qmemory/app/templates/connect.html` (or create it):

```html
{% if user_code %}
<div class="personal-url-box">
  <h2>Your Personal Qmemory URL</h2>
  <p><strong>Save this URL. If you lose it, you lose your memory data. There is no recovery.</strong></p>
  <code>https://mem0.qusai.org/mcp/u/{{ user_code }}/</code>
  <h3>Claude Code</h3>
  <pre>claude mcp add --transport http qmemory https://mem0.qusai.org/mcp/u/{{ user_code }}/</pre>
  <h3>Claude.ai</h3>
  <p>Go to Settings -> Connectors -> Add custom connector. Paste the URL above.</p>
</div>
{% endif %}
```

In `qmemory/app/routes/connect.py`, update the route handler to pass `user_code=request.query_params.get("code")` to the template.

- [ ] **Step 5: Register signup router in main.py**

Add to imports in `qmemory/app/main.py`:
```python
from qmemory.app.routes.signup import router as signup_router
```

Add to the router registration block:
```python
api.include_router(signup_router)
```

- [ ] **Step 6: Write integration test**

Create `tests/test_app/test_signup_route.py`:

```python
"""Integration test for the zero-friction signup flow."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from qmemory.app.main import api
from qmemory.db.client import get_admin_db, query


@pytest.fixture(autouse=True)
async def cleanup_signup_users():
    yield
    async with get_admin_db() as admin:
        await query(
            admin,
            "DELETE user WHERE display_name = 'Integration Tester'",
        )


async def test_signup_creates_user_and_redirects_to_connect():
    async with AsyncClient(
        transport=ASGITransport(app=api), base_url="http://test", follow_redirects=False
    ) as c:
        r = await c.post("/signup", data={"display_name": "Integration Tester"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/connect?code=")
    code = r.headers["location"].split("code=")[1]
    assert len(code) >= 5
```

- [ ] **Step 7: Run**

```bash
uv run pytest tests/test_app/test_signup_route.py -v
```

Expected: 1 test passes.

- [ ] **Step 8: Commit**

```bash
git add qmemory/app/routes/signup.py qmemory/app/routes/connect.py qmemory/app/templates/ qmemory/app/main.py tests/test_app/test_signup_route.py
git commit -m "feat(app): zero-friction signup - display name to personal URL"
```

### Task 2.8: Two-user isolation integration test

**Files:**
- Create: `tests/test_integration/__init__.py`
- Create: `tests/test_integration/test_multi_user_isolation.py`

- [ ] **Step 1: Write the test**

```bash
mkdir -p tests/test_integration
touch tests/test_integration/__init__.py
```

Create `tests/test_integration/test_multi_user_isolation.py`:

```python
"""End-to-end test: two signed-up users see zero data overlap."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from qmemory.app.main import api
from qmemory.db.client import get_admin_db, get_db, query


async def _signup(client: AsyncClient, display_name: str) -> str:
    r = await client.post("/signup", data={"display_name": display_name}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].split("code=")[1]


async def _save_memory(client: AsyncClient, code: str, content: str) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "qmemory_save",
            "arguments": {"content": content, "category": "context"},
        },
    }
    r = await client.post(
        f"/mcp/u/{code}/",
        json=body,
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200


async def _search_memories(client: AsyncClient, code: str, query_text: str) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "qmemory_search",
            "arguments": {"query": query_text},
        },
    }
    r = await client.post(
        f"/mcp/u/{code}/",
        json=body,
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200
    data = r.json()
    content = data["result"]["content"][0]["text"]
    return json.loads(content)


def _flatten_contents(search_result: dict) -> list[str]:
    out: list[str] = []
    for cat_list in search_result.get("memories", {}).values():
        for mem in cat_list:
            out.append(mem.get("content", ""))
    return out


@pytest.fixture(autouse=True)
async def _cleanup_test_users():
    yield
    async with get_admin_db() as admin:
        rows = await query(
            admin,
            "SELECT user_code, db_name FROM user WHERE display_name IN ['IsoTest Alice', 'IsoTest Bob']",
        )
    if rows:
        for row in rows:
            async with get_db() as base:
                await query(base, f"REMOVE DATABASE IF EXISTS {row['db_name']}")
        async with get_admin_db() as admin:
            await query(
                admin,
                "DELETE user WHERE display_name IN ['IsoTest Alice', 'IsoTest Bob']",
            )


async def test_two_users_have_isolated_memory_graphs():
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as c:
        alice = await _signup(c, "IsoTest Alice")
        bob = await _signup(c, "IsoTest Bob")

        assert alice != bob

        await _save_memory(c, alice, "Alice's secret fact about project X")
        await _save_memory(c, bob, "Bob's unrelated note about fishing")

        alice_hits = await _search_memories(c, alice, "project X")
        bob_hits = await _search_memories(c, bob, "project X")

    alice_contents = _flatten_contents(alice_hits)
    bob_contents = _flatten_contents(bob_hits)

    assert any("Alice" in item for item in alice_contents), f"Alice missing: {alice_contents}"
    assert not any("Alice" in item for item in bob_contents), f"Bob leaked: {bob_contents}"
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/test_integration/test_multi_user_isolation.py -v -s
```

Expected: 1 test passes.

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration/
git commit -m "test: two-user isolation integration test"
```

**Phase 2 checkpoint — full test suite:**

```bash
uv run pytest tests/ --tb=short
```

All tests must pass. Multi-user isolation is live. Move on only if the isolation test is green.

---

# Phase 3 — Legacy Migration

**MEDIUM RISK MODIFICATION** — requires Phase 0 backup.

**Phase exit criteria:** `user_qusai` database exists with identical row counts to `main`, admin DB has a `qusai` user row, `https://mem0.qusai.org/mcp/u/qusai/` returns real data, and `main` is preserved untouched.

### Task 3.1: Add the four admin CLI commands

**Files:**
- Create: `qmemory/admin/__init__.py`
- Create: `qmemory/admin/cli.py`
- Modify: `qmemory/cli.py`

- [ ] **Step 1: Create the admin package**

```bash
mkdir -p qmemory/admin
touch qmemory/admin/__init__.py
```

Create `qmemory/admin/cli.py`:

```python
"""Admin CLI: status, create-db, create-user, list-users."""

from __future__ import annotations

import asyncio

import click

from qmemory.db.client import (
    apply_admin_schema,
    get_admin_db,
    is_healthy,
    query,
)
from qmemory.db.provision import provision_user_db


@click.group("admin")
def admin_group() -> None:
    """Administer the qmemory multi-user deployment."""


@admin_group.command("status")
def status_cmd() -> None:
    """Print admin DB status and row count."""

    async def _run() -> None:
        healthy = await is_healthy()
        click.echo(f"SurrealDB: {'healthy' if healthy else 'UNREACHABLE'}")
        if not healthy:
            return

        async with get_admin_db() as admin:
            try:
                await apply_admin_schema(admin)
            except Exception as exc:
                click.echo(f"WARNING: could not apply admin schema: {exc}")
            rows = await query(admin, "SELECT count() FROM user GROUP ALL")
        total = (rows[0]["count"] if rows else 0) if rows else 0
        click.echo(f"Admin DB: {total} users")

    asyncio.run(_run())


@admin_group.command("create-db")
@click.option("--name", required=True, help="User code (DB will be 'user_{name}')")
def create_db_cmd(name: str) -> None:
    """Create a user database with schema. No admin row created."""

    async def _run() -> None:
        db_name = await provision_user_db(name)
        click.echo(f"Provisioned database: {db_name}")
        click.echo(
            "Next: qmemory admin create-user "
            f"--user-code {name} --display-name '...' --db-name {db_name}"
        )

    asyncio.run(_run())


@admin_group.command("create-user")
@click.option("--user-code", required=True, help="user_code for the URL")
@click.option("--display-name", required=True, help="Human-friendly name")
@click.option("--db-name", required=True, help="Database name")
def create_user_cmd(user_code: str, display_name: str, db_name: str) -> None:
    """Insert a row linking user_code to db_name."""

    async def _run() -> None:
        async with get_admin_db() as admin:
            await apply_admin_schema(admin)
            existing = await query(
                admin,
                "SELECT id FROM user WHERE user_code = $code",
                {"code": user_code},
            )
            if existing:
                raise click.ClickException(f"user_code {user_code!r} already exists")

            await query(
                admin,
                """CREATE user SET
                    user_code = $code,
                    display_name = $name,
                    db_name = $db_name,
                    is_active = true""",
                {"code": user_code, "name": display_name, "db_name": db_name},
            )
        click.echo(f"Created user: {user_code} -> {db_name}")

    asyncio.run(_run())


@admin_group.command("list-users")
def list_users_cmd() -> None:
    """Print all rows in the admin user table."""

    async def _run() -> None:
        async with get_admin_db() as admin:
            rows = await query(
                admin,
                "SELECT user_code, display_name, db_name, is_active, last_active_at FROM user",
            )
        if not rows:
            click.echo("(no users)")
            return
        for row in rows:
            active = "active" if row.get("is_active") else "DISABLED"
            last = row.get("last_active_at") or "(never)"
            click.echo(
                f"  {row['user_code']:<20} {row['display_name']:<24} "
                f"{row['db_name']:<24} {active:<10} last_active={last}"
            )

    asyncio.run(_run())
```

- [ ] **Step 2: Register the admin group in `qmemory/cli.py`**

Find the existing CLI main group (likely `cli = click.Group()` or a function decorated with `@click.group()`). Add:

```python
from qmemory.admin.cli import admin_group

# After the group is defined, register:
cli.add_command(admin_group)
```

- [ ] **Step 3: Smoke test**

```bash
uv run qmemory admin status
uv run qmemory admin list-users
```

Expected: `status` reports `healthy` and `0 users`. `list-users` prints `(no users)`.

- [ ] **Step 4: Commit**

```bash
git add qmemory/admin/ qmemory/cli.py
git commit -m "feat(cli): add admin commands (status, create-db, create-user, list-users)"
```

### Task 3.2: Export production main database

**Files:** `~/qmemory-main-export-2026-04-10.surql`

- [ ] **Step 1: Confirm Phase 0 backup still exists**

```bash
ls -lh ~/Desktop/qmemory-backup-pre-rebuild.surql.gz
gunzip -t ~/Desktop/qmemory-backup-pre-rebuild.surql.gz && echo "gzip OK"
```

- [ ] **Step 2: Fresh export for migration**

```bash
export SURREAL_URL="https://surrealdb-production-d9ea.up.railway.app"
export SURREAL_PASS="$(railway variables --service surrealdb --json | jq -r '.SURREAL_PASS')"

surreal export \
  --endpoint "$SURREAL_URL" \
  --username root --password "$SURREAL_PASS" \
  --namespace qmemory --database main \
  ~/qmemory-main-export-2026-04-10.surql

wc -l ~/qmemory-main-export-2026-04-10.surql
grep -c "^UPDATE memory:" ~/qmemory-main-export-2026-04-10.surql
grep -c "^UPDATE entity:" ~/qmemory-main-export-2026-04-10.surql
```

Expected: ~8,635 memory rows, ~72+ entity rows. Write these numbers down.

### Task 3.3: Provision empty user_qusai

- [ ] **Step 1: Use admin CLI**

```bash
uv run qmemory admin create-db --name qusai
```

Expected: `Provisioned database: user_qusai`

- [ ] **Step 2: Verify empty**

```bash
surreal sql --endpoint "$SURREAL_URL" \
  --username root --password "$SURREAL_PASS" \
  --namespace qmemory --database user_qusai \
  --pretty <<EOF
SELECT count() FROM memory GROUP ALL;
SELECT count() FROM entity GROUP ALL;
EOF
```

Expected: 0 for both.

- [ ] **Step 3: DO NOT create the admin user row yet**

### Task 3.4: Import data into user_qusai

- [ ] **Step 1: Run import**

```bash
surreal import \
  --endpoint "$SURREAL_URL" \
  --username root --password "$SURREAL_PASS" \
  --namespace qmemory --database user_qusai \
  ~/qmemory-main-export-2026-04-10.surql
```

Expected: 5-15 minutes. HNSW index rebuild runs silently.

### Task 3.5: Verification

**Files:** `scripts/verify_migration.py`

- [ ] **Step 1: Write the verification script**

Create `scripts/verify_migration.py`:

```python
#!/usr/bin/env python3
"""Compare row counts between main and user_qusai. Exit 1 on mismatch."""

from __future__ import annotations

import asyncio
import sys

from qmemory.db.client import get_db, query

TABLES = ["memory", "entity", "relates", "session", "message", "scratchpad"]


async def counts(db_name: str) -> dict[str, int]:
    out: dict[str, int] = {}
    async with get_db(database=db_name) as conn:
        for table in TABLES:
            rows = await query(conn, f"SELECT count() FROM {table} GROUP ALL")
            out[table] = (rows[0]["count"] if rows else 0) if rows else 0
    return out


async def main() -> int:
    print("Counting rows in main...")
    main_counts = await counts("main")
    print("Counting rows in user_qusai...")
    new_counts = await counts("user_qusai")

    ok = True
    for table in TABLES:
        m = main_counts[table]
        n = new_counts[table]
        tag = "OK " if m == n else "FAIL"
        print(f"  {tag}  {table}: main={m}  user_qusai={n}")
        if m != n:
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Run verification**

```bash
chmod +x scripts/verify_migration.py
uv run python scripts/verify_migration.py
echo "exit: $?"
```

Expected: all tables `OK`, `exit: 0`. If any FAIL, STOP.

- [ ] **Step 3: Behavioral sanity check**

```bash
QMEMORY_SURREAL_DB=main uv run qmemory search "Rakeezah" 2>&1 | head -20 > /tmp/main-results.txt
QMEMORY_SURREAL_DB=user_qusai uv run qmemory search "Rakeezah" 2>&1 | head -20 > /tmp/new-results.txt
diff /tmp/main-results.txt /tmp/new-results.txt
```

Expected: no diff (or timing-only).

- [ ] **Step 4: Commit the verification script**

```bash
git add scripts/verify_migration.py
git commit -m "tooling: add scripts/verify_migration.py"
```

### Task 3.6: Create admin user row for qusai

- [ ] **Step 1: Only after 3.5 all OK, insert the admin row**

```bash
uv run qmemory admin create-user \
    --user-code qusai \
    --display-name "Qusai Abushanap" \
    --db-name user_qusai
```

- [ ] **Step 2: Verify**

```bash
uv run qmemory admin list-users
```

Expected: one row for `qusai`.

- [ ] **Step 3: Smoke test personal URL locally**

Terminal A:
```bash
uv run uvicorn qmemory.app.main:api --port 3777
```

Terminal B:
```bash
curl -s -X POST http://localhost:3777/mcp/u/qusai/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"qmemory_search","arguments":{"query":"Rakeezah","limit":3}}}' \
  | python -m json.tool | head -30
```

Expected: real memory results.

Stop the server.

### Task 3.7: Deprecate legacy /mcp/ endpoint

**Files:** Modify `qmemory/app/main.py`

- [ ] **Step 1: Add legacy endpoint handler**

Immediately BEFORE `api.mount("/mcp", mcp_app)`, add:

```python
@api.api_route("/mcp/", methods=["GET", "POST"], include_in_schema=False)
async def legacy_mcp_root(request: Request):
    """Legacy /mcp/ endpoint - now requires /mcp/u/{code}/."""
    return JSONResponse(
        {
            "error": "gone",
            "message": "This endpoint has moved. Use your personal URL at /mcp/u/{your_user_code}/",
            "signup": "https://mem0.qusai.org/signup",
        },
        status_code=410,
    )
```

Note: the mount at `/mcp` still stands because the middleware rewrites `/mcp/u/{code}/...` to `/mcp/...` before the mounted app sees it. The explicit route above only catches the exact `/mcp/` path.

- [ ] **Step 2: Smoke test**

Terminal A:
```bash
uv run uvicorn qmemory.app.main:api --port 3777
```

Terminal B:
```bash
curl -sv http://localhost:3777/mcp/ 2>&1 | grep "HTTP/1.1"
curl -s -X POST http://localhost:3777/mcp/u/qusai/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python -m json.tool | head -5
```

Expected: `HTTP/1.1 410 Gone` on legacy, tools list on user path.

- [ ] **Step 3: Commit**

```bash
git add qmemory/app/main.py
git commit -m "feat(app): deprecate legacy /mcp/ endpoint with HTTP 410"
```

**Phase 3 checkpoint:** admin DB has a `qusai` row, row counts match, personal URL returns data, main untouched.

---

# Phase 4 — Railway Worker Service

**Phase exit criteria:** `qmemory-worker` Railway service runs `qmemory worker --interval 3600 --all-users`. Health reports appear in each user's DB. `qmemory_health` returns a non-"no_report" response.

### Task 4.1: Add --all-users flag to worker

**Files:**
- Modify: `qmemory/worker/__init__.py`
- Modify: `qmemory/cli.py`
- Create: `tests/test_worker/test_all_users.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker/test_all_users.py`:

```python
"""Test worker iteration walks all active users in admin DB."""

from __future__ import annotations

import pytest

from qmemory.db.client import apply_admin_schema, get_admin_db, query
from qmemory.worker import _iter_active_user_dbs


@pytest.fixture
async def admin_with_users():
    async with get_admin_db(database="admin_test_worker") as admin:
        await apply_admin_schema(admin)
        await query(
            admin,
            """CREATE user SET user_code = 'active1', display_name = 'A', db_name = 'user_active1', is_active = true""",
        )
        await query(
            admin,
            """CREATE user SET user_code = 'active2', display_name = 'B', db_name = 'user_active2', is_active = true""",
        )
        await query(
            admin,
            """CREATE user SET user_code = 'disabled', display_name = 'C', db_name = 'user_disabled', is_active = false""",
        )
    yield
    async with get_admin_db(database="admin_test_worker") as admin:
        await query(admin, "REMOVE TABLE IF EXISTS user")


async def test_iter_active_user_dbs_returns_only_active(admin_with_users, monkeypatch):
    monkeypatch.setattr("qmemory.worker._ADMIN_DB_NAME", "admin_test_worker")

    collected = []
    async for user_code, db_name in _iter_active_user_dbs():
        collected.append((user_code, db_name))

    codes = {c for c, _ in collected}
    assert codes == {"active1", "active2"}
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest tests/test_worker/test_all_users.py -v
```

- [ ] **Step 3: Update `qmemory/worker/__init__.py`**

Read the current file first. Add at the top with other imports:

```python
from qmemory.db.client import _user_db, get_admin_db, query

_ADMIN_DB_NAME: str = "admin"
```

Add a new async generator (before the `run_worker` function):

```python
async def _iter_active_user_dbs():
    """Yield (user_code, db_name) for every active user in the admin DB."""
    async with get_admin_db(database=_ADMIN_DB_NAME) as admin:
        rows = await query(
            admin,
            "SELECT user_code, db_name FROM user WHERE is_active = true",
        )
    if not rows:
        return
    for row in rows:
        yield row["user_code"], row["db_name"]
```

Modify `run_worker` to accept `all_users: bool = False` and iterate when True:

```python
async def run_worker(
    interval: int = 86400,
    once: bool = False,
    all_users: bool = False,
) -> None:
    logger.info(
        "Worker starting (interval=%ds, once=%s, all_users=%s)",
        interval, once, all_users,
    )
    while True:
        if all_users:
            user_count = 0
            async for user_code, db_name in _iter_active_user_dbs():
                user_count += 1
                token = _user_db.set(db_name)
                try:
                    logger.info("Worker cycle START: user=%s db=%s", user_code, db_name)
                    await _run_one_cycle()
                    logger.info("Worker cycle END: user=%s", user_code)
                except Exception:
                    logger.exception("Worker cycle failed for user %s", user_code)
                finally:
                    _user_db.reset(token)
            logger.info("Worker cycle: %d users processed", user_count)
        else:
            logger.info("Worker cycle (single-user mode)")
            try:
                await _run_one_cycle()
            except Exception:
                logger.exception("Worker cycle failed")

        if once:
            return
        await asyncio.sleep(interval)
```

Where `_run_one_cycle` is the existing cycle logic extracted into a function if it wasn't already. The body should be whatever currently runs the 5 jobs (linker, dedup, decay, reflector, linter) in the existing code.

- [ ] **Step 4: Update CLI**

In `qmemory/cli.py`, find the `worker` command and add `--all-users`:

```python
@cli.command()
@click.option("--interval", default=86400, type=int, help="Seconds between cycles (default 24h)")
@click.option("--once", is_flag=True, help="Run one cycle and exit")
@click.option("--all-users", is_flag=True, help="Iterate all active users in the admin DB")
def worker(interval: int, once: bool, all_users: bool) -> None:
    """Run the background worker loop."""
    from qmemory.worker import run_worker
    asyncio.run(run_worker(interval=interval, once=once, all_users=all_users))
```

- [ ] **Step 5: Run the test**

```bash
uv run pytest tests/test_worker/test_all_users.py -v
```

Expected: 1 test passes.

- [ ] **Step 6: Smoke test locally with two users**

```bash
uv run qmemory admin create-db --name worker-test-1
uv run qmemory admin create-user --user-code worker-test-1 --display-name "WT1" --db-name user_worker-test-1
uv run qmemory admin create-db --name worker-test-2
uv run qmemory admin create-user --user-code worker-test-2 --display-name "WT2" --db-name user_worker-test-2

uv run qmemory worker --once --all-users 2>&1 | head -40
```

Expected: logs show "Worker cycle START" for qusai, worker-test-1, worker-test-2.

Cleanup:
```bash
surreal sql --endpoint "$SURREAL_URL" --username root --password "$SURREAL_PASS" --namespace qmemory --database admin <<EOF
DELETE user WHERE user_code IN ['worker-test-1', 'worker-test-2'];
EOF
surreal sql --endpoint "$SURREAL_URL" --username root --password "$SURREAL_PASS" --namespace qmemory <<EOF
REMOVE DATABASE user_worker-test-1;
REMOVE DATABASE user_worker-test-2;
EOF
```

- [ ] **Step 7: Commit**

```bash
git add qmemory/worker/__init__.py qmemory/cli.py tests/test_worker/test_all_users.py
git commit -m "feat(worker): --all-users iterates active users from admin DB"
```

### Task 4.2: Railway worker service runbook

**Files:** `scripts/railway-worker-setup.md`

- [ ] **Step 1: Write the runbook**

Create `scripts/railway-worker-setup.md`:

```markdown
# Railway Worker Service Setup Runbook

The worker service must be created manually in the Railway UI
because the CLI does not support custom start commands.

## Steps

1. Railway dashboard -> `qmemory` project -> **+ Create** -> **Empty Service**.
2. Rename to `qmemory-worker`.
3. Settings -> Source: connect `QusaiiSaleem/qmemory-py`, branch `main`.
4. Settings -> Deploy:
   - Build Command: (empty)
   - Start Command: `qmemory worker --interval 3600 --all-users`
   - Health Check Path: (empty)
   - Restart Policy: ON_FAILURE, max retries 3
5. Variables: copy from `qmemory` service:
   - `QMEMORY_SURREAL_URL=ws://surrealdb.railway.internal:8000`
   - `QMEMORY_SURREAL_USER=root`
   - `QMEMORY_SURREAL_PASS=${{surrealdb.SURREAL_PASS}}`
   - `QMEMORY_SURREAL_NS=qmemory`
   - `QMEMORY_SURREAL_DB=admin`
   - `ANTHROPIC_API_KEY=...`
   - `VOYAGE_API_KEY=...`
6. Resource limits: 512 MB RAM, 0.5 vCPU.
7. Deploy.

## Verification

```
railway logs --service qmemory-worker --lines 200 --filter "Worker cycle" --json
```

Expected: `Worker cycle: N users processed`.

## Health report check

```
curl -s -X POST https://mem0.qusai.org/mcp/u/qusai/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"qmemory_health","arguments":{}}}'
```

Expected: JSON with recent `generated_at`, not `no_report`.

## Rollback

Delete the `qmemory-worker` service from the Railway dashboard.
```

- [ ] **Step 2: Commit**

```bash
git add scripts/railway-worker-setup.md
git commit -m "docs: Railway worker service setup runbook"
```

### Task 4.3: Execute the Railway worker setup

- [ ] **Step 1: Follow `scripts/railway-worker-setup.md` in the Railway UI**

- [ ] **Step 2: Verify service is deployed**

```bash
railway service list 2>&1 | grep qmemory-worker
railway logs --service qmemory-worker --lines 100 --json | tail -20
```

- [ ] **Step 3: Wait for first cycle and verify health report**

```bash
curl -s -X POST https://mem0.qusai.org/mcp/u/qusai/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"qmemory_health","arguments":{"check":"all"}}}' \
  | python -m json.tool | head -30
```

Expected: not `status: no_report`.

**Phase 4 checkpoint:** worker runs on Railway, iterates users, produces health reports.

---

# Phase 5 — Search Polish

### Task 5.1: Add type diversity cap

**Files:**
- Modify: `qmemory/core/search.py`
- Modify: `tests/test_core/test_search.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_core/test_search.py`:

```python
async def test_diversity_cap_limits_single_category_to_60_percent(db):
    """Saving many same-category memories should be capped at 60% of limit."""
    from qmemory.core.save import save_memory
    from qmemory.core.search import search_memories

    for i in range(20):
        await save_memory(
            content=f"preference fact {i}: I like option {i}",
            category="preference",
            salience=0.5,
        )
    for i in range(3):
        await save_memory(
            content=f"context fact {i}: project detail {i}",
            category="context",
            salience=0.5,
        )

    result = await search_memories(query_text="option", limit=10)
    prefs = result.get("memories", {}).get("preference", [])
    assert len(prefs) <= 6, f"diversity cap violated: got {len(prefs)} preference"
```

- [ ] **Step 2: Run — expect fail**

```bash
uv run pytest tests/test_core/test_search.py::test_diversity_cap_limits_single_category_to_60_percent -v
```

- [ ] **Step 3: Add the cap to `qmemory/core/search.py`**

Near the top constants, add:

```python
DIVERSITY_CAP: float = 0.6  # max fraction of any single category in results
```

Add a helper function in the same file:

```python
def _apply_diversity_cap(
    grouped: dict[str, list[dict]],
    limit: int,
) -> dict[str, list[dict]]:
    """Limit any single category to at most DIVERSITY_CAP fraction of limit."""
    per_cat_cap = max(1, int(limit * DIVERSITY_CAP))
    return {
        cat: memories[:per_cat_cap]
        for cat, memories in grouped.items()
    }
```

In `search_memories()`, find where the final grouped dict is assembled (after RRF fusion + category grouping, before the response dict is built). Call the helper:

```python
grouped = _apply_diversity_cap(grouped, limit=limit)
```

The exact placement depends on the existing code — look for where `memories` or `grouped` becomes the dict keyed by category that flows into the response.

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_core/test_search.py::test_diversity_cap_limits_single_category_to_60_percent -v
```

- [ ] **Step 5: Run all search tests**

```bash
uv run pytest tests/test_core/test_search.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add qmemory/core/search.py tests/test_core/test_search.py
git commit -m "feat(search): type diversity cap - max 60% per category"
```

### Task 5.2: Audit pagination metadata

**Files:** `qmemory/core/search.py` (if fields missing)

- [ ] **Step 1: Check current meta shape**

```bash
uv run python -c "
import asyncio
from qmemory.core.search import search_memories

async def check():
    r = await search_memories(query_text='Rakeezah', limit=3)
    meta = r.get('meta', {})
    print('meta keys:', sorted(meta.keys()))
    required = ['total_found', 'returned', 'offset', 'has_more']
    missing = [k for k in required if k not in meta]
    if missing:
        print('MISSING:', missing)
    else:
        print('OK - all pagination fields present')

asyncio.run(check())
"
```

- [ ] **Step 2: If any fields missing, add to the meta dict**

Find where `meta = {...}` is built in `search_memories`. Ensure it contains:

```python
meta = {
    # ... existing fields ...
    "total_found": total_found,
    "returned": len(items),
    "offset": offset,
    "has_more": total_found > offset + len(items),
    "next_offset": offset + len(items) if total_found > offset + len(items) else None,
}
```

- [ ] **Step 3: Add a test**

Append to `tests/test_core/test_search.py`:

```python
async def test_search_meta_has_pagination_fields(db):
    from qmemory.core.save import save_memory
    from qmemory.core.search import search_memories

    for i in range(5):
        await save_memory(content=f"pagination test {i}", category="context")

    result = await search_memories(query_text="pagination", limit=2)
    meta = result["meta"]
    assert "total_found" in meta
    assert "returned" in meta
    assert "offset" in meta
    assert "has_more" in meta
    assert meta["returned"] <= 2
```

- [ ] **Step 4: Run**

```bash
uv run pytest tests/test_core/test_search.py::test_search_meta_has_pagination_fields -v
```

- [ ] **Step 5: Commit**

```bash
git add qmemory/core/search.py tests/test_core/test_search.py
git commit -m "fix(search): ensure meta includes pagination fields"
```

**Phase 5 checkpoint:** diversity cap + pagination metadata done.

---

# Phase 6 — Verification and Cutover

### Task 6.1: Full local smoke test

- [ ] **Step 1: Full test suite**

```bash
uv run pytest tests/ --tb=short
```

Expected: all tests pass.

- [ ] **Step 2: Manually verify 9 tools via stdio**

Open Claude Code, run each tool at least once: bootstrap, search, get, save, correct, link, person, books, health.

Record anomalies. Don't proceed if anything fails.

- [ ] **Step 3: HTTP smoke test via curl**

```bash
uv run uvicorn qmemory.app.main:api --port 3777 &
SERVER_PID=$!
sleep 2

curl -s -X POST http://localhost:3777/mcp/u/qusai/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python -m json.tool | grep '"name"' | head

kill $SERVER_PID
```

Expected: 9 tool names.

### Task 6.2: Production deploy

- [ ] **Step 1: Merge rebuild branch**

```bash
git checkout main
git merge --no-ff rebuild-2026-04-10
git push origin main
```

Railway auto-deploys the API.

- [ ] **Step 2: Wait for deploy**

```bash
railway logs --service qmemory --lines 50 --json | grep -i "starting\|ready\|error"
```

- [ ] **Step 3: Health check**

```bash
curl -s https://mem0.qusai.org/health | python -m json.tool
```

Expected: `status: healthy`, `database: connected`.

- [ ] **Step 4: Run production migration following Task 3.2 to 3.6**

Use the production `SURREAL_URL` and `SURREAL_PASS`. Phase 0 backup must be ready.

- [ ] **Step 5: Deploy the worker service per Task 4.3**

### Task 6.3: Production smoke test

- [ ] **Step 1: Test from Claude.ai**

Add `https://mem0.qusai.org/mcp/u/qusai/` in Claude.ai Settings -> Connectors. Start new chat, ask about something you know is in your memory.

Expected: Claude calls qmemory_search and returns real memories.

- [ ] **Step 2: Sign up a second test user**

Visit `https://mem0.qusai.org/signup`, use a test display name. Get the URL. Verify in a separate browser profile that the test user sees zero overlap with qusai.

- [ ] **Step 3: Verify worker logs**

```bash
railway logs --service qmemory-worker --lines 100 --json | grep "Worker cycle"
```

Expected: at least one "Worker cycle: N users processed".

- [ ] **Step 4: Monitor 24 hours**

Check Railway dashboards daily for error spikes.

### Task 6.4: Update README

**Files:** `README.md`

- [ ] **Step 1: Grep for stale references**

```bash
grep -rn "mem0.qusai.org/mcp/\"\|mem0.qusai.org/mcp/$" README.md docs/
```

Update to per-user URL pattern.

- [ ] **Step 2: Add "Multi-User" section if missing**

Briefly document the signup flow.

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: update for multi-user URLs"
```

### Task 6.5: Delete main database after 14-day safety window

- [ ] **Step 1: DO NOT execute this task until 14 days after Task 6.2 without incident**

Set a calendar reminder.

- [ ] **Step 2: Day 14+ re-verification**

```bash
uv run qmemory admin list-users | grep qusai
uv run python scripts/verify_migration.py
```

Both must pass.

- [ ] **Step 3: Delete main**

```bash
surreal sql --endpoint "$SURREAL_URL" \
  --username root --password "$SURREAL_PASS" \
  --namespace qmemory <<EOF
REMOVE DATABASE main;
EOF
```

- [ ] **Step 4: Commit the cleanup note**

Edit the spec to add "main database deleted on YYYY-MM-DD" and commit.

**Mission complete.**

---

## Plan self-review

- **Spec coverage:** all 10 decisions (D1-D10) and 5 resolved open questions are implemented across Phase 0-6. Phase 0 safety net. Phase 1 MCP rebuild + Pydantic + annotations + CORS. Phase 2 middleware + signup + admin DB + isolation test. Phase 3 export/import + admin CLI + /mcp/ deprecation. Phase 4 --all-users + Railway worker runbook. Phase 5 diversity cap + pagination. Phase 6 deploy + 14-day cleanup.
- **Placeholder scan:** zero TBD/TODO/FIXME.
- **Type consistency:** `Operation`, `OPERATIONS`, `mount_operations`, `safe_tool`, `MCPUserMiddleware`, `_user_db`, `get_admin_db`, `provision_user_db`, `generate_unique_user_code`, `_iter_active_user_dbs`, `DIVERSITY_CAP` — used consistently across tasks.
- **Every new file has tests:** errors.py, schemas.py, operations.py, registry.py, user_code.py, user_context.py, admin_db.py, all_users.py — all have matching test files. Plus integration tests for signup and two-user isolation.
- **Pre-existing infrastructure used, not rebuilt:** `provision_user_db`, `_user_db` ContextVar, `get_db()` routing, `worker` CLI command, `core/*` modules all stay intact.
