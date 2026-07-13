from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_web_and_flight_recorder_share_the_monochrome_theme():
    theme = (ROOT / "components/villani-ui/theme.css").read_text(encoding="utf-8")
    module = (ROOT / "components/villani-ui/index.js").read_text(encoding="utf-8")
    web = (ROOT / "components/villani-web/src/main.tsx").read_text(encoding="utf-8")
    recorder = (ROOT / "components/villani-flight-recorder/src/render/theme.ts").read_text(encoding="utf-8")
    assert 'import "@villani/ui/theme.css"' in web
    assert 'from "@villani/ui"' in recorder
    assert "--villani-bg-deepest: #050505" in theme
    assert "--villani-text-primary: #f2f2f2" in theme
    assert "color-scheme: dark" in theme
    assert "green" not in module.lower()
    assert "#0f0" not in theme.lower()


def test_existing_application_shells_are_present():
    web = (ROOT / "components/villani-web/src/App.tsx").read_text(encoding="utf-8")
    fleet = (ROOT / "components/villani-web/src/FleetApp.tsx").read_text(encoding="utf-8")
    recorder = (ROOT / "components/villani-flight-recorder/src/render/components/appShell.ts").read_text(encoding="utf-8")
    assert "<nav" in web and "<nav" in fleet
    assert 'class="app-shell"' in recorder
