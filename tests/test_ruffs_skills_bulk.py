"""RUFFS: importación de skills en bloque (manage_skills import / add_many)."""
import asyncio
import json
import os

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import src.constants as constants
    import src.tool_execution as te
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(constants, "DATA_DIR", str(data))
    monkeypatch.setattr(te, "_resolve_tool_path", lambda p: os.path.realpath(p))
    src = tmp_path / "mis-skills"
    for i in range(163):
        d = src / f"skill-{i:03d}"
        (d / "references").mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: skill-{i:03d}\ndescription: Skill de prueba número {i}\n---\n\n"
            f"# Skill {i}\n\nHaz el paso {i} con cuidado.\n", encoding="utf-8")
        (d / "references" / "guia.md").write_text(f"Referencia {i}", encoding="utf-8")
    return data, src


def run(args, owner="admin"):
    from src.tool_implementations import do_manage_skills
    return asyncio.run(do_manage_skills(json.dumps(args), owner=owner))


def test_import_163_skills_in_one_call(env):
    data, src = env
    res = run({"action": "import", "path": str(src)})
    assert "Imported 163 skill(s)" in res["results"], res
    from services.memory.skills import SkillsManager
    sm = SkillsManager(str(data))
    skills = sm.load(owner="admin")
    assert len(skills) == 163 and all(s["status"] == "published" for s in skills)
    assert sm.read_skill_reference("skill-042", "references/guia.md", owner="admin") == "Referencia 42"
    again = run({"action": "import", "path": str(src)})               # segunda vez: no duplica
    assert "Imported 0 skill(s), skipped 163" in again["results"]


def test_import_errors_are_reported(env):
    _, src = env
    res = run({"action": "import", "path": str(src / "no-existe")})
    assert "error" in res
    res = run({"action": "import"})
    assert "error" in res


def test_add_many(env):
    items = [{"name": f"rapida-{i}", "description": f"d{i}", "procedure": [f"paso {i}"]} for i in range(20)]
    res = run({"action": "add_many", "skills": items})
    assert res["results"].startswith("Created 20 skill(s), 0 failed"), res


def test_round_and_cost_defaults(monkeypatch):
    import importlib
    import src.ruffs_config as rc
    monkeypatch.delenv("RUFFS_AGENT_MAX_ROUNDS", raising=False)
    monkeypatch.delenv("RUFFS_AGENT_MAX_COST_USD", raising=False)
    importlib.reload(rc)
    assert rc.AGENT_MAX_ROUNDS == 200 and rc.AGENT_MAX_COST_USD == 1.0
    monkeypatch.setenv("RUFFS_AGENT_MAX_COST_USD", "0")
    importlib.reload(rc)
    assert rc.AGENT_MAX_COST_USD == 0.0
    importlib.reload(rc)


def test_agent_pauses_at_cost_cap(monkeypatch):
    """Cada ronda «cuesta» $0.30: con tope de $1 el agente se pausa tras la 4.ª ronda."""
    import importlib
    import sys
    from unittest.mock import MagicMock
    # Otros tests heredados dejan módulos falsos (MagicMock) en sys.modules.
    for mod in list(sys.modules):
        if isinstance(sys.modules.get(mod), MagicMock):
            monkeypatch.delitem(sys.modules, mod)
    import src.agent_loop as al
    al = importlib.reload(al)
    import src.ruffs_config as rc
    monkeypatch.setattr(rc, "AGENT_MAX_COST_USD", 1.0)
    calls = {"n": 0}

    async def fake_llm(*args, **kwargs):
        calls["n"] += 1
        n = calls["n"]
        text = f"Busco el dato {n}.\n```web_search\nconsulta distinta {n}\n```\n"
        yield "data: " + json.dumps({"delta": text}) + "\n\n"
        yield "data: " + json.dumps({"type": "usage", "data": {"input_tokens": 100, "output_tokens": 10, "cost": 0.30}}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_llm)

    async def go():
        out = []
        async for chunk in al.stream_agent_loop(
                "https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash",
                [{"role": "user", "content": "investiga 50 cosas"}],
                max_rounds=50, disabled_tools={"web_search"}):
            out.append(chunk)
        return "".join(out)
    text = asyncio.run(go())
    assert calls["n"] == 4, calls
    assert "tope por mensaje: $1.00" in text and "paused_at_cost_limit" in text
