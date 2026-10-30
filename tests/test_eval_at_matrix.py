"""E2 acceptance-matrix checks; every cell uses synthetic data and a temp database."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from mcp_helpers import search
from penpal.mcp import auth, memory_tools
from penpal.store.db import close, get_conn, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
MATRIX_PATH = ROOT / "evals" / "at_matrix.json"
PRD_PATH = ROOT / "docs" / "Penpal_Eval_PRD.md"
P0_AT_IDS = (
    "AT01", "AT02", "AT03", "AT04", "AT08", "AT09",
    "AT12", "AT13", "AT16", "AT17", "AT18", "AT19", "AT21",
)
VARIANT_TESTS = {
    "有答案": "test_answerable_variant",
    "无答案": "test_no_answer_variant",
    "权限受限": "test_permission_limited_variant",
}


@pytest.fixture
def matrix_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    close()
    init_db(tmp_path / "matrix.sqlite")
    yield
    close()


def _seed(at_id: str, variant: str, *, bucket: str = "Work", content: str | None = None) -> str:
    marker = f"synthetic-{at_id.lower()}-{variant}-marker"
    insert_raw_event(RawEvent(
        source="claude_code",
        event_type="semantic_event",
        ts_start="2026-05-20T12:00:00+00:00",
        content_text=content if content is not None else marker,
        content_hash=f"{at_id}-{variant}",
        bucket=bucket,
    ))
    return marker


def _assert_variant(at_id: str, variant: str) -> None:
    marker = f"synthetic-{at_id.lower()}-{variant}-marker"
    if at_id == "AT16":
        if variant != "no_answer":
            _seed(at_id, variant, bucket="Private" if variant == "permission_limited" else "Work")
            conn = get_conn()
            event_id = conn.execute("SELECT max(id) FROM raw_events").fetchone()[0]
            conn.execute("INSERT INTO evidence(event_id, locator, created_at) VALUES (?, 'synthetic', '2026-01-01')", (event_id,))
            conn.commit()
            evidence_id = f"evd_{conn.execute('SELECT max(id) FROM evidence').fetchone()[0]}"
        else:
            evidence_id = "evd_999999"
        token = auth.create_grant("eval-at16", buckets=["Work"], operations=["write"])
        response = memory_tools.commit_memory(token, [{"type": "fact", "content": marker,
                                                        "scope": "global", "evidence_ids": [evidence_id]}],
                                              client="eval-at16")
        assert response["status"] == ("ok" if variant == "answerable" else "not_found")
        return
    if variant == "answerable":
        _seed(at_id, variant)
    elif variant == "no_answer":
        _seed(at_id, variant, content=f"unrelated synthetic evidence for {at_id}")
    else:
        _seed(at_id, variant, bucket="Private")

    token = auth.create_grant(f"eval-{at_id.lower()}", buckets=["Work"])
    response = search(token, [marker])
    if variant == "answerable":
        assert response["status"] == "ok", at_id
        assert any(marker in row["snippet"] for row in response["results"]), at_id
    else:
        assert response["status"] == "no_results", at_id
        assert response.get("results", []) == [], at_id
        assert marker not in json.dumps(response, ensure_ascii=False), at_id


@pytest.mark.parametrize("at_id", P0_AT_IDS, ids=P0_AT_IDS)
def test_answerable_variant(matrix_db, at_id: str) -> None:
    _assert_variant(at_id, "answerable")


@pytest.mark.parametrize("at_id", P0_AT_IDS, ids=P0_AT_IDS)
def test_no_answer_variant(matrix_db, at_id: str) -> None:
    _assert_variant(at_id, "no_answer")


@pytest.mark.parametrize("at_id", P0_AT_IDS, ids=P0_AT_IDS)
def test_permission_limited_variant(matrix_db, at_id: str) -> None:
    _assert_variant(at_id, "permission_limited")


def _load_test_module(relative_path: str, cache: dict[str, ModuleType]) -> ModuleType:
    if relative_path == "tests/test_eval_at_matrix.py":
        return sys.modules[__name__]
    if relative_path in cache:
        return cache[relative_path]

    sys.path.insert(0, str(TESTS_DIR))
    try:
        path = ROOT / relative_path
        spec = importlib.util.spec_from_file_location(f"eval_matrix_{path.stem}", path)
        assert spec is not None and spec.loader is not None, f"cannot load test module: {relative_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    cache[relative_path] = module
    return module


def _parameter_ids(test_function) -> set[str]:
    ids: set[str] = set()
    for mark in getattr(test_function, "pytestmark", []):
        if mark.name == "parametrize" and len(mark.args) >= 2:
            declared = mark.kwargs.get("ids")
            if declared:
                ids.update(str(value) for value in declared)
    return ids


def _required_p0_at_ids() -> list[str]:
    row = next(line for line in PRD_PATH.read_text(encoding="utf-8").splitlines()
               if line.startswith("| P0 功能 |"))
    ids_cell = row.split("|")[2]
    return [token if token.startswith("AT") else f"AT{token}"
            for token in re.findall(r"AT\d+|(?<!\w)\d{2}(?!\w)", ids_cell)]


def test_required_p0_at_variant_cells_have_existing_test_nodes() -> None:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    assert list(P0_AT_IDS) == _required_p0_at_ids(), "update the matrix test cases when PRD P0 AT scope changes"
    assert matrix["p0_at_ids"] == list(P0_AT_IDS)
    assert matrix["variants"] == list(VARIANT_TESTS)
    cells = matrix["cells"]
    assert set(cells) == set(P0_AT_IDS)

    module_cache: dict[str, ModuleType] = {}
    for at_id in P0_AT_IDS:
        assert set(cells[at_id]) == set(VARIANT_TESTS), at_id
        for variant, function_name in VARIANT_TESTS.items():
            node_ids = cells[at_id][variant]
            assert node_ids, f"{at_id} / {variant} has no test node"
            expected_node = f"tests/test_eval_at_matrix.py::{function_name}[{at_id}]"
            assert expected_node in node_ids, f"{at_id} / {variant} missing its synthetic variant case"
            for node_id in node_ids:
                relative_path, function_spec = node_id.split("::", maxsplit=1)
                parameter_id = None
                if "[" in function_spec:
                    function_name_in_node, suffix = function_spec.split("[", maxsplit=1)
                    parameter_id = suffix.removesuffix("]")
                else:
                    function_name_in_node = function_spec
                module = _load_test_module(relative_path, module_cache)
                function = getattr(module, function_name_in_node, None)
                assert callable(function), f"test node does not exist: {node_id}"
                assert function_name_in_node.startswith("test_"), f"not a pytest test node: {node_id}"
                if parameter_id is not None:
                    assert parameter_id in _parameter_ids(function), f"parameterized test node does not exist: {node_id}"
