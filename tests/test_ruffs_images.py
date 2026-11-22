"""RUFFS: generar y editar imágenes con la API key de la persona (OpenRouter u
OpenAI/compatibles). Sin red: el proveedor se simula."""

import asyncio
import base64
import json

import httpx
import pytest

import src.agent_tools  # noqa: F401  (orden de imports: evita el import circular de tool_schemas)
from src import image_gen

PNG = b"\x89PNG\r\n\x1a\nfake"
B64 = base64.b64encode(PNG).decode()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(image_gen, "IMG_DIR", tmp_path / "generated")
    monkeypatch.setattr("src.settings.load_settings", lambda: {})
    seen = []

    def make_client(handler):
        real = httpx.AsyncClient

        def factory(*a, **k):
            k.pop("transport", None)
            return real(*a, transport=httpx.MockTransport(handler), **k)
        monkeypatch.setattr(image_gen.httpx if hasattr(image_gen, "httpx") else httpx, "AsyncClient", factory, raising=False)
        monkeypatch.setattr(httpx, "AsyncClient", factory)

    def pick(url, model):
        monkeypatch.setattr(image_gen, "pick_model", lambda spec="", owner=None, editing=False:
                            (url, spec or model, {"Authorization": "Bearer k", "Content-Type": "application/json"}))
    return {"seen": seen, "client": make_client, "pick": pick, "dir": tmp_path / "generated"}


def test_openrouter_generate_and_edit(env):
    def handler(req):
        body = json.loads(req.content)
        env["seen"].append(body)
        assert req.url.path.endswith("/chat/completions") and body["modalities"] == ["image", "text"]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "images": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}]}}],
            "usage": {"cost": 0.0387}})
    env["client"](handler)
    env["pick"]("https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash-image")
    r = asyncio.run(image_gen.run("un perro astronauta", size="1536x1024"))
    assert r["image_url"].startswith("/api/generated-image/") and r["image_cost_usd"] == 0.0387
    assert "US$ 0.0387" in r["results"] and r["image_model"] == "google/gemini-2.5-flash-image"
    assert env["seen"][0]["image_config"] == {"aspect_ratio": "3:2"}
    assert (env["dir"] / r["image_url"].rsplit("/", 1)[1]).read_bytes() == PNG
    # editar: la imagen va en el mensaje
    r2 = asyncio.run(image_gen.run("ponle un sombrero", images=[(PNG, "image/jpeg")]))
    parts = env["seen"][1]["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "ponle un sombrero"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,") and "error" not in r2


def test_openrouter_no_image_is_a_clear_error(env):
    env["client"](lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "No puedo dibujar"}}]}))
    env["pick"]("https://openrouter.ai/api/v1/chat/completions", "x/model")
    assert "No puedo dibujar" in asyncio.run(image_gen.run("algo"))["error"]


def test_openai_generate_and_edit(env):
    def handler(req):
        env["seen"].append((req.url.path, req.headers.get("content-type", ""), req.content))
        return httpx.Response(200, json={"data": [{"b64_json": B64}]})
    env["client"](handler)
    env["pick"]("https://api.openai.com/v1/chat/completions", "gpt-image-1")
    r = asyncio.run(image_gen.run("logo minimalista", size="16:9", quality="low"))
    path, ctype, body = env["seen"][0]
    assert path == "/v1/images/generations" and json.loads(body)["size"] == "1536x1024" and json.loads(body)["quality"] == "low"
    r2 = asyncio.run(image_gen.run("cambia el fondo a azul", images=[(PNG, "image/png")]))
    path, ctype, body = env["seen"][1]
    assert path == "/v1/images/edits" and ctype.startswith("multipart/form-data") and b'name="image[]"' in body
    assert "error" not in r and "error" not in r2 and "Edité" in r2["results"]


def test_provider_errors_are_readable(env):
    env["client"](lambda req: httpx.Response(402, json={"error": {"message": "Insufficient credits"}}))
    env["pick"]("https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash-image")
    assert "402" in asyncio.run(image_gen.run("x"))["error"] and "Insufficient credits" in asyncio.run(image_gen.run("x"))["error"]


def test_model_names_and_sizes():
    assert image_gen.is_image_model("google/gemini-2.5-flash-image")
    assert image_gen.is_image_model("openai/gpt-5-image-mini") and image_gen.is_image_model("gpt-image-1")
    assert not image_gen.is_image_model("deepseek/deepseek-chat") and not image_gen.is_image_model("qwen2.5-vl")
    assert image_gen.aspect_ratio("1024x1536") == "2:3" and image_gen.aspect_ratio("16:9") == "16:9"
    assert image_gen.aspect_ratio("vertical") == "9:16" and image_gen.aspect_ratio("") == "1:1"
    assert image_gen.openai_size("16:9", "gpt-image-1") == "1536x1024"
    assert image_gen.openai_size("1536x1024", "dall-e-3") == "1024x1024"


def test_agent_tool_calls_route_inside_ruffs(monkeypatch):
    import sys
    # Otros tests dejan un src.agent_tools de mentira (sin __file__): usar los reales.
    stub = sys.modules.get("src.agent_tools")
    if stub is not None and not getattr(stub, "__file__", None):
        for m in ("src.agent_tools", "src.tool_schemas"):
            if m in sys.modules:
                monkeypatch.delitem(sys.modules, m)
    import src.agent_tools  # noqa: F401  (primero: evita el import circular)
    from src.tool_schemas import function_call_to_tool_block, FUNCTION_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert {"generate_image", "edit_image"} <= names
    b = function_call_to_tool_block("edit_image", '{"prompt": "agrégale un sombrero", "images": ["abc123"]}')
    assert b.tool_type == "edit_image" and b.content.split("\n")[:2] == ["agrégale un sombrero", "abc123"]
    assert function_call_to_tool_block("edit_image", '{"prompt": "x"}').content.split("\n")[1] == "last"
    assert function_call_to_tool_block("generate_image", '{"prompt": "gato", "size": "16:9"}').content.split("\n") == ["gato", "", "16:9", ""]
    assert image_gen.mcp_call_to_native("mcp__image_gen__edit_image", '{"prompt": "p", "images": ["a", "b"]}') == ("edit_image", "p\na b\n\n")
    from src.agent_tools import TOOL_TAGS
    assert "edit_image" in TOOL_TAGS


def test_edit_needs_an_image_and_respects_owners(env, monkeypatch, tmp_path):
    from src.ai_interaction import do_edit_image
    monkeypatch.setattr(image_gen, "last_images", lambda sid, owner: [])
    r = asyncio.run(do_edit_image("ponle un sombrero\nlast", session_id="s", owner="ana"))
    assert "No encontré la imagen" in r["error"]
    # imagen generada de otra persona → no se puede usar
    env["dir"].mkdir(parents=True, exist_ok=True)
    (env["dir"] / "ajena.png").write_bytes(PNG)
    (env["dir"] / "propia.png").write_bytes(PNG)

    class Row:
        def __init__(self, owner): self.owner = owner

    class Q:
        def __init__(self, name): self.name = name
        def filter(self, cond):
            return self
        def first(self):
            return Row("beto") if self.name == "ajena" else Row("ana")

    class DB:
        last = None
        def query(self, *a): return Q(DB.last)
        def close(self): pass

    import core.database as D
    monkeypatch.setattr(D, "SessionLocal", lambda: DB())
    DB.last = "ajena"
    assert image_gen._read_generated("/api/generated-image/ajena.png", "ana") is None
    DB.last = "propia"
    assert image_gen._read_generated("/api/generated-image/propia.png", "ana") == (PNG, "image/png")
    assert image_gen._read_generated("../../secreto.png", "ana") is None


def test_builtin_image_skill(tmp_path):
    """Skill integrada: la ve todo el mundo, se inyecta en pedidos de imágenes
    (y no en otros), no se borra desde la app y una propia la reemplaza."""
    from services.memory.skills import SkillsManager
    sm = SkillsManager(str(tmp_path))
    sk = sm.load(owner="ana")
    assert [(s["name"], s.get("builtin")) for s in sk] == [("image-generation", True)]
    rel = lambda q: [x["name"] for x in sm.get_relevant_skills(q, skills=sk, threshold=0.25, max_items=3, min_confidence=0.6)]
    assert rel("genera una imagen de un perro astronauta") == ["image-generation"]
    assert rel("agrégale un sombrero a esta foto") == ["image-generation"]
    assert rel("escribe un informe de ventas") == [] and rel("genera un correo para mi jefe") == []
    assert sm.delete_skill("image-generation", owner="ana") is False
    md = sm.read_skill_md("image-generation", owner="ana")
    assert "edit_image" in md and "generate_image" in md and "US$0.04" in md
    assert sm.load(owner="beto")[0]["name"] == "image-generation"     # para todas las personas


def test_settings_lists_image_models_and_bad_setting_falls_back(monkeypatch):
    """Ajustes → Image Generation: lista los modelos de imagen de tus keys
    (OpenRouter los marca), y un modelo guardado que ya no existe no rompe nada."""
    import core.database as D

    class EP:
        name, base_url, api_key, is_enabled = "OpenRouter", "https://openrouter.ai/api/v1", "k", True

    class Q:
        def filter(self, *a): return self
        def all(self): return [EP()]

    class DB:
        def query(self, *a): return Q()
        def close(self): pass

    monkeypatch.setattr(D, "SessionLocal", lambda: DB())
    image_gen._MODELS_CACHE.clear()
    data = {"data": [
        {"id": "google/gemini-2.5-flash-image", "architecture": {"output_modalities": ["image", "text"]}},
        {"id": "deepseek/deepseek-chat", "architecture": {"output_modalities": ["text"]}},
        {"id": "openrouter/auto", "architecture": {"output_modalities": ["image", "text"]}}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=data))
    assert image_gen.available_models() == [{"id": "google/gemini-2.5-flash-image", "endpoint": "OpenRouter"}]
    # modelo de Ajustes inexistente → automático
    import src.ai_interaction as AI
    monkeypatch.setattr("src.settings.load_settings", lambda: {"image_model": "stable-diffusion-3.5-medium"})

    def resolve(spec, owner=None):
        if spec == "google/gemini-2.5-flash-image":
            return "https://openrouter.ai/api/v1/chat/completions", spec, {}
        raise ValueError("no")
    monkeypatch.setattr(AI, "_resolve_model", resolve)
    assert image_gen.pick_model()[1] == "google/gemini-2.5-flash-image"
    with pytest.raises(ValueError):            # pedido explícito que no existe: se avisa
        image_gen.pick_model("modelo-que-no-existe")
    html = (__import__("pathlib").Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
    assert '<div class="admin-card" hidden style="display:none">\n            <h2 style="display:flex;align-items:center;gap:6px;"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:1px;opacity:0.6;flex-shrink:0"><rect x="3" y="3" width="18" height="18" rx="2"/>' not in html.replace("\r\n", "\n")
    assert "/api/gallery/image-models" in (__import__("pathlib").Path(__file__).resolve().parents[1] / "static/js/settings.js").read_text(encoding="utf-8")
