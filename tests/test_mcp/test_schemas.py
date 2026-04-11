"""Tests for MCP tool input validation schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qmemory.mcp.schemas import (
    BooksInput,
    BootstrapInput,
    CorrectInput,
    GetInput,
    HealthInput,
    LinkInput,
    PersonInput,
    SaveInput,
    SearchInput,
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
