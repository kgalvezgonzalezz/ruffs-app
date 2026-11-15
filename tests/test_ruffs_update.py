"""RUFFS: aviso de nueva versión (routes/update_routes.py)."""
import os
import subprocess
import shutil

import pytest

import routes.update_routes as up


class _Req:
    def __init__(self, host="127.0.0.1"):
        class C: pass
        self.client = C(); self.client.host = host
        class S: pass
        self.state = S(); self.state.current_user = "admin"
        class A: pass
        self.app = A(); self.app.state = S()
        self.headers = {}


@pytest.fixture()
def rel(monkeypatch, tmp_path):
    monkeypatch.setenv("RUFFS_UPDATE_REPO", "usuario/ruffs-app")
    monkeypatch.setenv("RUFFS_VERSION_OVERRIDE", "1.1.2")
    monkeypatch.setenv("AUTH_ENABLED", "false")      # require_admin deja pasar
    data = {"tag": "v1.1.3", "name": "RUFFS 1.1.3", "notes": "## Novedades\n- 200 rondas", "html_url": "https://x",
            "published_at": "2026-11-15", "repo": "usuario/ruffs-app",
            "assets": {"windows": {"name": "RUFFS-Setup.exe", "url": "https://x/RUFFS-Setup.exe", "sha256": "ab"},
                       "mac": {"name": "RUFFS.dmg", "url": "https://x/RUFFS.dmg", "sha256": "cd"}}}
    monkeypatch.setattr(up, "latest_release", lambda force=False: data)
    return data


def test_version_compare():
    assert up.is_newer("1.1.3", "1.1.2") and up.is_newer("v1.2", "1.1.9") and up.is_newer("1.10.0", "1.9.9")
    assert not up.is_newer("1.1.2", "1.1.2") and not up.is_newer("1.1.1", "1.1.2")
    assert not up.is_newer("", "1.0") and not up.is_newer("1.0", "")


def test_newer_version_is_offered_with_notes(rel):
    info = up.update_info(_Req())
    assert info["newer"] and info["latest"] == "1.1.3" and info["current"] == "1.1.2"
    assert "200 rondas" in info["notes"] and info["can_install"]


def test_remote_device_cannot_install(rel):
    info = up.update_info(_Req("100.88.77.66"))
    assert info["newer"] and not info["can_install"] and "equipo" in info["why_not"]


def test_no_notice_without_version_or_repo(monkeypatch, rel):
    monkeypatch.setenv("RUFFS_VERSION_OVERRIDE", "")
    monkeypatch.setattr(up, "_read_file", lambda name: "")
    assert up.update_info(_Req())["newer"] is False          # copia de desarrollo: sin aviso
    monkeypatch.setenv("RUFFS_VERSION_OVERRIDE", "1.0.0")
    monkeypatch.setenv("RUFFS_UPDATE_REPO", "")
    assert up.update_info(_Req())["newer"] is False          # sin repositorio: sin aviso
    monkeypatch.setenv("RUFFS_UPDATE_REPO", "usuario/ruffs-app")
    monkeypatch.setenv("RUFFS_UPDATE_CHECK", "false")
    assert up.update_info(_Req())["newer"] is False          # desactivado


def test_same_version_no_notice(monkeypatch, rel):
    monkeypatch.setenv("RUFFS_VERSION_OVERRIDE", "1.1.3")
    assert up.update_info(_Req())["newer"] is False


def test_download_rejects_bad_checksum(tmp_path, monkeypatch):
    import io
    class R(io.BytesIO):
        headers = {"Content-Length": "5"}
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(up.urllib.request, "urlopen", lambda *a, **k: R(b"hello"))
    with pytest.raises(RuntimeError):
        up._download("https://x/f", str(tmp_path / "f.exe"), "0" * 64)
    assert not (tmp_path / "f.exe").exists()
    up._download("https://x/f", str(tmp_path / "g.exe"),
                 "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
    assert (tmp_path / "g.exe").read_bytes() == b"hello"


def _bash():
    if os.name == "nt":  # en Windows, "bash" suele ser el de WSL, que no ve estas rutas
        for c in ("C:/Program Files/Git/bin/bash.exe", "C:/Program Files (x86)/Git/bin/bash.exe"):
            if os.path.isfile(c):
                return c
        return None
    return shutil.which("bash")


@pytest.mark.skipif(not _bash(), reason="sin bash")
def test_mac_script_is_valid_shell(tmp_path):
    script = tmp_path / "s.sh"
    script.write_text(up.mac_install_script("/tmp/RUFFS 1.dmg", "/Applications/RUFFS.app"), encoding="utf-8", newline="\n")
    assert subprocess.run([_bash(), "-n", str(script)]).returncode == 0
    assert "'/Applications/RUFFS.app'" in script.read_text(encoding="utf-8")
