"""Tests for the MCP operations table."""

from __future__ import annotations

from qmemory.mcp.operations import OPERATIONS
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
