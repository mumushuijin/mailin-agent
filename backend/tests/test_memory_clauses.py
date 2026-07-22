from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.context.memory.clauses import (
    CLAUSE_DELIMITER,
    SECTION_MARKER,
    Clause,
    detect_drift,
    parse_clauses,
    render_sections,
)
from app.context.memory.consolidator import extract_remember_fact, user_requested_remember
from app.context.memory.registry import section_keys
from app.context.memory.router import RawCandidate, route_candidates, validate_raw_candidates
from app.context.memory.consolidator import _parse_arbiter_decisions
from app.context.memory.state_machine import MergeDecision, MergeOperation, apply_decisions
from app.context.memory.store import HotMemoryStore
from app.agent.messages import is_real_user_message, make_system_message, is_system_maintenance
from langchain_core.messages import HumanMessage


def test_render_fixed_sections_empty_placeholder():
    sections = {k: [] for k in section_keys("memory")}
    sections["environment"] = [Clause("env.stack", "environment", "Python + LangGraph")]
    rendered = render_sections(sections, target="memory")
    assert "§§§ Preference §§§" in rendered
    assert "§§§ Environment §§§" in rendered
    assert "[env.stack]" in rendered
    assert "（空）" in rendered
    assert not detect_drift(rendered, target="memory")


def test_json_store_roundtrip(tmp_path: Path):
    store = HotMemoryStore(tmp_path, "memory")
    store.set_section("preference", [Clause("pref.lang", "preference", "用户偏好中文")])
    store.commit()
    assert store.json_path.exists()
    assert store.md_path.exists()
    reloaded = HotMemoryStore(tmp_path, "memory")
    reloaded._sections = None
    clauses = reloaded.get_section("preference")
    assert len(clauses) == 1
    assert clauses[0].clause_id == "pref.lang"


def test_router_low_confidence_to_misc():
    raw = RawCandidate(
        fact="某事实",
        target="memory",
        section="preference",
        clause_id="pref.x",
        confidence="low",
    )
    groups = route_candidates([raw], {"memory": set(), "user": set()})
    assert groups[("memory", "misc")][0].section == "misc"


def test_state_machine_add_update():
    clauses = [Clause("pref.lang", "preference", "旧")]
    decisions = [
        MergeDecision(operation=MergeOperation.UPDATE, clause_id="pref.lang", new_text="新"),
        MergeDecision(operation=MergeOperation.ADD, clause_id="pref.new", new_text="追加"),
    ]
    updated, _ = apply_decisions(clauses, decisions, section_key="preference", section_char_limit=800)
    assert len(updated) == 2
    assert updated[0].text == "新"


def test_validate_candidates_retry_format():
    valid, errors = validate_raw_candidates([{"fact": "x", "target": "memory"}])
    assert valid
    assert not errors


def test_extract_remember_fact():
    assert extract_remember_fact("请记住我喜欢简洁回复") == "我喜欢简洁回复"
    assert extract_remember_fact("帮我记：项目用 Python") == "项目用 Python"


def test_user_requested_remember():
    assert user_requested_remember("请记住这个")
    assert not user_requested_remember("今天天气不错")


def test_store_repairs_stale_md_from_json(tmp_path: Path):
    boot = tmp_path / "bootstraps"
    boot.mkdir(parents=True)
    (boot / "user_sections.json").write_text(
        json.dumps(
            {
                "identity": [{"id": "user.name", "text": "张三"}],
                "style": [],
                "habit": [],
                "misc": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (boot / "USER.md").write_text("# 旧格式\n\n§§§ 身份 §§§\n旧内容\n", encoding="utf-8")
    store = HotMemoryStore(tmp_path, "user")
    store.initialize()
    md = (boot / "USER.md").read_text(encoding="utf-8")
    assert "[user.name]" in md
    assert "张三" in md
    assert (boot / ".hot_memory_snapshots" / "user" / "sections.json").exists()


def test_store_restores_corrupt_json_from_snapshot(tmp_path: Path):
    boot = tmp_path / "bootstraps"
    boot.mkdir(parents=True)
    good = {
        "preference": [{"id": "pref.lang", "text": "中文"}],
        "environment": [],
        "tools": [],
        "convention": [],
        "misc": [],
    }
    (boot / "memory_sections.json").write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    store = HotMemoryStore(tmp_path, "memory")
    store.initialize()

    (boot / "memory_sections.json").write_text("{not valid json", encoding="utf-8")
    store._sections = None
    sections = store.load()
    assert sections["preference"][0].clause_id == "pref.lang"
    md = (boot / "MEMORY.md").read_text(encoding="utf-8")
    assert "[pref.lang]" in md


def test_initialize_empty_workspace(tmp_path: Path):
    store = HotMemoryStore(tmp_path, "memory")
    store.initialize()
    assert store.json_path.exists()
    assert store.md_path.exists()
    assert "§§§ Preference §§§" in store.md_path.read_text(encoding="utf-8")
    assert (tmp_path / "bootstraps" / ".hot_memory_snapshots" / "memory" / "sections.json").exists()


def test_commit_updates_snapshot(tmp_path: Path):
    store = HotMemoryStore(tmp_path, "memory")
    store.initialize()
    store.set_section("preference", [Clause("pref.x", "preference", "测试")])
    store.commit()
    snap = json.loads(
        (tmp_path / "bootstraps" / ".hot_memory_snapshots" / "memory" / "sections.json").read_text(
            encoding="utf-8"
        )
    )
    assert snap["preference"][0]["id"] == "pref.x"


def test_parse_arbiter_decisions():
    raw = [{"operation": "ADD", "clause_id": "pref.lang", "new_text": "中文"}]
    decisions = _parse_arbiter_decisions(raw)
    assert len(decisions) == 1
    assert decisions[0].operation == MergeOperation.ADD


def test_system_maintenance_message():
    msg = make_system_message("维护", "memory_nudge")
    assert is_system_maintenance(msg)
    assert not is_real_user_message(msg)
    real = HumanMessage(content="hello")
    assert is_real_user_message(real)
