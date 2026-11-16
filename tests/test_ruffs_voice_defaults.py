"""Voz gratuita por defecto (dictado + lectura con la voz del dispositivo)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.settings as S  # noqa: E402
from src import ruffs_bootstrap as B  # noqa: E402


def _settings_file(tmp_path, monkeypatch, data):
    f = tmp_path / "settings.json"
    if data is not None:
        f.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(S, "SETTINGS_FILE", str(f))
    S._invalidate_caches()
    return f


def _saved(f):
    S._invalidate_caches()
    return json.loads(f.read_text(encoding="utf-8"))


def test_new_install_defaults_are_free_browser_voice(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, None)
    s = S.load_settings()
    assert s["stt_enabled"] is True and s["stt_provider"] == "browser"
    assert s["tts_enabled"] is True and s["tts_provider"] == "browser"


def test_old_install_with_voice_disabled_gets_it_once(tmp_path, monkeypatch):
    f = _settings_file(tmp_path, monkeypatch, {
        "stt_enabled": False, "stt_provider": "disabled",
        "tts_enabled": False, "tts_provider": "disabled",
        "default_model": "x/y",
    })
    assert B.enable_free_voice_once() is True
    s = _saved(f)
    assert s["stt_enabled"] is True and s["stt_provider"] == "browser"
    assert s["tts_enabled"] is True and s["tts_provider"] == "browser"
    assert s[B.VOICE_DEFAULTS_MARKER] is True
    assert s["default_model"] == "x/y"  # el resto de ajustes no se toca


def test_runs_only_once_and_respects_turning_it_off_later(tmp_path, monkeypatch):
    f = _settings_file(tmp_path, monkeypatch, {"stt_provider": "disabled", "tts_provider": "disabled"})
    assert B.enable_free_voice_once() is True
    # La persona lo apaga después…
    s = _saved(f)
    s.update({"stt_enabled": False, "stt_provider": "disabled", "tts_enabled": False})
    f.write_text(json.dumps(s), encoding="utf-8")
    S._invalidate_caches()
    # …y al volver a arrancar sigue apagado.
    assert B.enable_free_voice_once() is False
    s = _saved(f)
    assert s["stt_enabled"] is False and s["stt_provider"] == "disabled"
    assert s["tts_enabled"] is False


def test_keeps_a_provider_the_person_chose(tmp_path, monkeypatch):
    f = _settings_file(tmp_path, monkeypatch, {
        "stt_enabled": True, "stt_provider": "local", "stt_model": "small",
        "tts_enabled": True, "tts_provider": "endpoint:abc",
    })
    assert B.enable_free_voice_once() is False
    s = _saved(f)
    assert s["stt_provider"] == "local" and s["stt_model"] == "small"
    assert s["tts_provider"] == "endpoint:abc"
    assert s[B.VOICE_DEFAULTS_MARKER] is True


def test_only_the_disabled_half_is_switched(tmp_path, monkeypatch):
    f = _settings_file(tmp_path, monkeypatch, {"stt_provider": "local", "tts_provider": "disabled"})
    assert B.enable_free_voice_once() is True
    s = _saved(f)
    assert s["stt_provider"] == "local"
    assert s["tts_provider"] == "browser"
