from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_web_and_flight_recorder_share_the_monochrome_theme():
    theme = (ROOT / "components/villani-ui/theme.css").read_text(encoding="utf-8")
    module = (ROOT / "components/villani-ui/index.js").read_text(encoding="utf-8")
    web = (ROOT / "components/villani-web/src/main.tsx").read_text(encoding="utf-8")
    recorder = (
        ROOT / "components/villani-flight-recorder/src/render/theme.ts"
    ).read_text(encoding="utf-8")
    assert 'import "@villani/ui/theme.css"' in web
    assert 'from "@villani/ui"' in recorder
    assert "--v-bg-root: #f6f6f3" in theme
    assert "--v-bg-panel: #ffffff" in theme
    assert "--v-text-primary: #171717" in theme
    assert "--v-sidebar-width: 232px" in theme
    assert "color-scheme: light" in theme
    assert "green" not in module.lower()
    assert "#0f0" not in theme.lower()
    assert "#090d19" not in theme.lower()
    assert "#45dfa7" not in theme.lower()


def test_existing_application_shells_are_present():
    web = (ROOT / "components/villani-web/src/App.tsx").read_text(encoding="utf-8")
    fleet = (ROOT / "components/villani-web/src/FleetApp.tsx").read_text(
        encoding="utf-8"
    )
    ask = (ROOT / "components/villani-web/src/InterrogateApp.tsx").read_text(
        encoding="utf-8"
    )
    product_shell = (ROOT / "components/villani-web/src/ProductShell.tsx").read_text(
        encoding="utf-8"
    )
    recorder = (
        ROOT / "components/villani-flight-recorder/src/render/components/appShell.ts"
    ).read_text(encoding="utf-8")
    assert 'from "./ProductShell"' in web
    assert 'from "./ProductShell"' in fleet
    assert 'from "./ProductShell"' in ask
    assert (
        "<ProductShell" in web and "<ProductShell" in fleet and "<ProductShell" in ask
    )
    assert 'from "@villani/ui/react"' in product_shell
    assert "<AppShell" in product_shell and "<Sidebar" in product_shell
    assert 'class="v-app-shell vfr-shell"' in recorder
    assert 'class="v-sidebar"' in recorder
