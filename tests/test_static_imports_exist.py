"""Every relative JS import in static/ must point at a file that exists.

A missing module makes the browser abort the whole ES-module graph, so the
UI silently fails to start (no sidebar setup, no composer toolbar…). This
happened when static/js/editor/build/ was caught by a broad `build/`
.gitignore rule: the files existed locally but never reached git, so every
install built from git shipped a broken interface.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

# import x from './a.js' · import './a.js' · export … from './a.js' · import('./a.js')
_IMPORT_RE = re.compile(
    r"""(?:^|[\s;])(?:import|export)\s[^'"`;]*?from\s*['"](\.{1,2}/[^'"]+)['"]"""
    r"""|(?:^|[\s;])import\s*['"](\.{1,2}/[^'"]+)['"]"""
    r"""|import\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""",
    re.MULTILINE,
)


def _own_js_files():
    for p in STATIC.rglob("*.js"):
        rel = p.relative_to(STATIC).as_posix()
        if rel.startswith(("lib/", "vendor/")) or ".min." in p.name:
            continue
        yield p


def test_relative_js_imports_resolve():
    missing = []
    for js in _own_js_files():
        text = js.read_text(encoding="utf-8", errors="ignore")
        for m in _IMPORT_RE.finditer(text):
            spec = next(g for g in m.groups() if g)
            target = (js.parent / spec.split("?")[0]).resolve()
            if not target.exists():
                missing.append(f"{js.relative_to(ROOT).as_posix()} -> {spec}")
    assert not missing, "Imports a archivos inexistentes:\n" + "\n".join(sorted(set(missing)))


def test_index_html_scripts_exist():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    missing = []
    for src in re.findall(r"""<(?:script|link)[^>]+(?:src|href)=["'](/static/[^"'?#]+)""", html):
        if not (ROOT / src.lstrip("/")).exists():
            missing.append(src)
    assert not missing, "index.html referencia archivos inexistentes: " + ", ".join(missing)
