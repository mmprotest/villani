import re
import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ASSET = re.compile(r'(?:src|href)=["\']([^"\'#?]+)')


def test_generated_html_references_only_existing_local_assets():
    html_files = [ROOT / "components/villani-web/dist/index.html"]
    assert all(path.is_file() for path in html_files)
    for html in html_files:
        for reference in ASSET.findall(html.read_text(encoding="utf-8")):
            if reference.startswith(("http:", "https:", "data:", "mailto:")):
                continue
            assert (html.parent / reference.lstrip("/")).is_file(), (
                f"{html} references missing asset {reference}"
            )


def test_console_asset_sync_is_idempotent_when_manifest_and_bytes_match(
    tmp_path, monkeypatch
):
    source = tmp_path / "dist"
    destination = tmp_path / "console_assets"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<title>Villani Console</title>", encoding="utf-8")
    (source / "assets" / "index.js").write_text("export {};", encoding="utf-8")
    namespace = runpy.run_path(str(ROOT / "scripts" / "sync-console-assets.py"))
    synchronize = namespace["synchronize"]

    expected = synchronize(source, destination)
    replace = namespace["os"].replace

    def unexpected_replace(*_args, **_kwargs):
        raise AssertionError("matching packaged assets must not replace directories")

    monkeypatch.setattr(namespace["os"], "replace", unexpected_replace)
    try:
        assert synchronize(source, destination) == expected
    finally:
        monkeypatch.setattr(namespace["os"], "replace", replace)
