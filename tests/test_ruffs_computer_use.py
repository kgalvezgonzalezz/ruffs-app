"""RUFFS: Computer Use (mcp_servers/computer_use_server.py) sin tocar la pantalla,
y las capturas de herramientas que se pasan a los modelos con visión."""
import importlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def cu(tmp_path, monkeypatch):
    monkeypatch.setenv("RUFFS_COMPUTER_USE_DIR", str(tmp_path))
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers"))
    try:
        mod = importlib.import_module("computer_use_server")
        mod = importlib.reload(mod)
    finally:
        sys.path.pop(0)
    return mod


def test_sensitive_words_use_word_boundaries(cu):
    assert cu._sensitive_word("botón Enviar") == "enviar"
    assert cu._sensitive_word("Eliminar cuenta") == "eliminar"
    assert cu._sensitive_word("Código postal") is None          # "post" no es "postal"
    assert cu._sensitive_word("Enviado ayer") is None            # "enviar" ≠ "enviado"
    assert cu._sensitive_word("") is None


def test_collect_and_element_under_point(cu, monkeypatch):
    tree = {"bounds": {"x": 0, "y": 0, "width": 800, "height": 600}, "role": "window", "children": [
        {"bounds": {"x": 100, "y": 100, "width": 300, "height": 40}, "role": "group", "ref_id": "@s:e1",
         "available_actions": ["ScrollTo"], "children": [
             {"bounds": {"x": 110, "y": 105, "width": 80, "height": 30}, "role": "button",
              "role_description": "botón", "name": "Enviar", "ref_id": "@s:e2",
              "available_actions": ["Click", "SetFocus"]}]},
        {"bounds": {"x": 100, "y": 200, "width": 300, "height": 30}, "role": "textfield",
         "name": "Asunto", "ref_id": "@s:e3", "available_actions": ["SetValue"]}]}
    out = []
    cu._collect(tree, out)
    assert [e["name"] for e in out] == ["Enviar", "Asunto"]          # el grupo no cuenta
    monkeypatch.setattr(cu, "_snapshot", lambda *a, **k: {"app": "x", "tree": tree})
    assert cu._elements_at((150, 120)) == "botón Enviar"
    assert cu._elements_at((700, 500)) == ""


def test_coordinates_multi_monitor_and_retina(cu):
    # Segundo monitor a la derecha, captura reducida a la mitad.
    view = {"screen": 2, "left": 1920, "top": 0, "width": 2560, "height": 1440,
            "img_w": 1280, "img_h": 720}
    assert cu._to_screen(0, 0, view) == (1920, 0)
    assert cu._to_screen(640, 360, view) == (1920 + 1280, 720)
    assert cu._to_img(1920 + 1280, 720, view) == (640, 360)
    with pytest.raises(ValueError):
        cu._to_screen(5000, 10, view)


def test_chat_confirmation_tokens(cu):
    with open(cu.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"confirmacion": "chat"}, f)
    msg = cu._confirm("enviar el correo", None)
    assert msg and "confirm_token" in msg
    token = msg.split('confirm_token="')[1].split('"')[0]
    assert cu._confirm("enviar el correo", "ffffff") is not None      # inventado
    assert cu._confirm("borrar todo", token) is not None              # para otra acción
    assert cu._confirm("enviar el correo", token) is None             # el correcto
    assert cu._confirm("enviar el correo", token) is not None         # un solo uso


def test_blocked_and_confirm_app_rules(cu):
    with open(cu.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"confirmacion": "chat"}, f)
    cfg = cu._cfg()
    assert cu._matches({"app": "Bitwarden.exe", "title": "Bitwarden"}, cfg["apps_bloqueadas"])
    assert cu._matches({"app": "KeePassXC", "title": ""}, cfg["apps_bloqueadas"])
    assert not cu._matches({"app": "notepad.exe", "title": "Sin título"}, cfg["apps_bloqueadas"])


def test_keys(cu):
    pytest.importorskip("pynput")
    from pynput.keyboard import Key
    assert cu._key("mod") in (Key.ctrl, Key.cmd)
    assert cu._key("Intro") == Key.enter and cu._key("f5") == Key.f5
    with pytest.raises(ValueError):
        cu._key("teclainventada")


def test_tool_screenshots_reach_vision_models():
    import src.agent_loop as al
    msgs = [{"role": "user", "content": "hola"}]
    for i in range(3):
        al._append_tool_results(msgs, "r", [], ["res"], ["res"], False, i,
                                images=[{"data": f"IMG{i}", "mimeType": "image/jpeg"}])
    with_imgs = [m for m in msgs if isinstance(m.get("content"), list)]
    assert len(with_imgs) == 3
    urls = [p["image_url"]["url"] for m in with_imgs for p in m["content"] if p.get("type") == "image_url"]
    assert urls == ["data:image/jpeg;base64,IMG2"]                    # solo la última sigue entera
    os.environ["RUFFS_TOOL_IMAGES"] = "off"
    try:
        assert al._tool_images_ok("deepseek/deepseek-v4-flash-vision-exp", "") is False
    finally:
        os.environ.pop("RUFFS_TOOL_IMAGES")
    assert al._tool_images_ok("deepseek/deepseek-v4-flash-vision-exp", "") is True


def test_computer_ruffs_persona_is_seeded_once(tmp_path):
    from src.preset_manager import PresetManager

    pm = PresetManager(str(tmp_path))
    t = next(x for x in pm.get_user_templates() if x["name"] == "Computer Ruffs")
    prompt = t["system_prompt"]
    assert 0 < len(prompt) <= 10000 and len(prompt.splitlines()) < 120
    assert "__screenshot" in prompt and "request_confirmation" in prompt
    pm.delete_user_template(t["id"])
    assert not PresetManager(str(tmp_path)).get_user_templates()   # borrada → no vuelve


def test_agent_desktop_launcher_node_script_or_native(cu, tmp_path):
    script = tmp_path / "agent-desktop"
    script.write_text("#!/usr/bin/env node\nconsole.log(1)\n", encoding="utf-8")
    cmd = cu._launcher(str(script))
    assert len(cmd) == 2 and cmd[1] == str(script)            # script de Node (Windows)
    script.write_bytes(b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01")      # binario nativo (macOS)
    assert cu._launcher(str(script)) == [str(script)]
