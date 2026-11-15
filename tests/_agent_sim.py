"""Simulaciones del bucle del agente con un modelo falso (se ejecutan en un proceso
aparte desde los tests para no depender de módulos falsos que dejan otros tests).

    python tests/_agent_sim.py cost_cap|repeat_guard   → imprime un JSON con el resultado
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _run(al, messages, **kw):
    out = []
    async for chunk in al.stream_agent_loop("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash",
                                            messages, **kw):
        out.append(chunk)
    return "".join(out)


def cost_cap() -> dict:
    import src.agent_loop as al
    import src.ruffs_config as rc
    rc.AGENT_MAX_COST_USD = 1.0
    calls = {"n": 0}

    async def fake_llm(*args, **kwargs):
        calls["n"] += 1
        n = calls["n"]
        text = f"Busco el dato {n}.\n```web_search\nconsulta distinta {n}\n```\n"
        yield "data: " + json.dumps({"delta": text}) + "\n\n"
        yield "data: " + json.dumps({"type": "usage", "data": {"input_tokens": 100, "output_tokens": 10, "cost": 0.30}}) + "\n\n"
        yield "data: [DONE]\n\n"
    al.stream_llm_with_fallback = fake_llm
    text = asyncio.run(_run(al, [{"role": "user", "content": "investiga 50 cosas"}],
                            max_rounds=50, disabled_tools={"web_search"}))
    return {"calls": calls["n"], "capped": "tope por mensaje: $1.00" in text and "paused_at_cost_limit" in text}


def repeat_guard() -> dict:
    import src.agent_loop as al
    executed = {"n": 0}
    real_exec = al.execute_tool_block

    async def counting_exec(block, **kw):
        executed["n"] += 1
        return await real_exec(block, **kw)
    al.execute_tool_block = counting_exec
    calls = {"n": 0}

    async def fake_llm(*args, **kwargs):
        calls["n"] += 1
        if kwargs.get("tools") == []:
            yield "data: " + json.dumps({"delta": "No puedo seguir: la búsqueda está desactivada."}) + "\n\n"
        else:
            text = f"Voy a buscarlo de verdad ahora (intento {calls['n']}).\n```web_search\nlo mismo de siempre\n```\n"
            yield "data: " + json.dumps({"delta": text}) + "\n\n"
        yield "data: [DONE]\n\n"
    al.stream_llm_with_fallback = fake_llm
    text = asyncio.run(_run(al, [{"role": "user", "content": "busca algo"}],
                            max_rounds=50, disabled_tools={"web_search"}))
    return {"calls": calls["n"], "executed": executed["n"],
            "blocked_note": ("REPETIDA" in text or "NO EJECUTADA" in text)}


def dsml() -> dict:
    import src.agent_tools  # noqa: F401  (orden de importación)
    from src.tool_parsing import parse_tool_blocks, strip_tool_blocks
    nl = chr(10)
    leak = nl.join([
        "Pulso el botón.",
        "<｜DSML｜ calls>",
        '<｜DSML｜ invoke name="mcp__x__click">',
        '<｜DSML｜ parameter name="x" string="false">586</｜DSML｜ parameter>',
        "</｜DSML｜ invoke>",
        "</｜DSML｜ calls>",
    ])
    return {"tools": [b.tool_type for b in parse_tool_blocks(leak)],
            "leftover": "DSML" in strip_tool_blocks(leak)}


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    result = {"cost_cap": cost_cap, "repeat_guard": repeat_guard, "dsml": dsml}[sys.argv[1]]()
    print("RESULT " + json.dumps(result))
