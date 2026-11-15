"""RUFFS: automatic, content-based chat titles (routes/chat_helpers.py)."""
import asyncio
from types import SimpleNamespace

import pytest

import routes.chat_helpers as ch


@pytest.mark.parametrize("name,expected", [
    ("deepseek-v4.1-flash-1017 12:30:55 p. m.", True),   # Spanish locale
    ("deepseek-v4.1-flash-1017 12:30:55 a. m.", True),
    ("qwen 下午2:05:33", True),                            # Chinese locale
    ("gemma3 23:59:59", True),
    ("Reunión a las 10:30", False),                       # user title with a time
    ("Diagnosticar errores de arranque en RUFFS", False),
])
def test_placeholder_names_in_any_locale(name, expected):
    assert ch.needs_auto_name(name) is expected


@pytest.mark.parametrize("raw,expected", [
    ('<think>hmm</think>Título: "Reparar MCP de Ghidra".', "Reparar MCP de Ghidra"),
    ("**Auto-título de sesiones en RUFFS**", "Auto-título de sesiones en RUFFS"),
    ("Title: Fix login bug\nBecause the user…", "Fix login bug"),
    ("", ""),
])
def test_clean_title(raw, expected):
    assert ch.clean_title(raw) == expected


def _msg(role, content):
    return SimpleNamespace(role=role, content=content)


def _sess(name, n_user, sid="s1"):
    hist = []
    for i in range(n_user):
        hist += [_msg("user", f"pregunta {i}"), _msg("assistant", f"respuesta {i}")]
    return SimpleNamespace(id=sid, name=name, history=hist, owner="alice",
                           endpoint_url="https://openrouter.ai/api/v1", model="m", headers={})


def test_should_auto_title_first_and_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_AUTO_TITLES_FILE", str(tmp_path / "auto_titles.json"))
    # Placeholder → always titled.
    assert ch.should_auto_title(_sess("m 12:30:55 p. m.", 1))
    # Auto title is refreshed only at the growth checkpoints.
    ch._remember_auto_title("s1", "Primer título")
    assert not ch.should_auto_title(_sess("Primer título", 2))
    assert ch.should_auto_title(_sess("Primer título", ch.RETITLE_AT[0]))
    # A title the user typed is never replaced.
    assert not ch.should_auto_title(_sess("Mi título", ch.RETITLE_AT[0]))


def test_auto_name_uses_conversation_and_respects_manual_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_AUTO_TITLES_FILE", str(tmp_path / "auto_titles.json"))
    seen = {}

    async def fake_llm(url, model, messages, **kw):
        seen["prompt"] = messages[-1]["content"]
        return "Análisis del binario Atla.exe."

    import src.llm_core, src.task_endpoint
    monkeypatch.setattr(src.llm_core, "llm_call_async", fake_llm)
    monkeypatch.setattr(src.task_endpoint, "resolve_task_endpoint", lambda u, m, h, owner=None: (u, m, h))

    sess = _sess("m 12:30:55 p. m.", 1)
    renamed = {}

    class SM:
        sessions = {"s1": sess}
        def update_session_name(self, sid, name):
            renamed[sid] = name
            sess.name = name

    asyncio.run(ch.auto_name_session(SM(), sess))
    assert renamed == {"s1": "Análisis del binario Atla.exe"}
    assert "pregunta 0" in seen["prompt"] and "respuesta 0" in seen["prompt"]

    # The user renames the chat while a refresh is being generated → kept.
    renamed.clear()
    sess.name = "Mi título"
    asyncio.run(ch.auto_name_session(SM(), sess))
    assert renamed == {}
