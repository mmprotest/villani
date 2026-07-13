import re
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
