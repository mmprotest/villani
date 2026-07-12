from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _python_imports(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_controller_does_not_import_agentd() -> None:
    source = (
        ROOT
        / "components/villani-ops/villani_ops/closed_loop/controller.py"
    ).read_text(encoding="utf-8")
    assert "villani_agentd" not in source


def test_domain_services_do_not_import_api_routes() -> None:
    source = _python_imports(
        ROOT / "components/villani-control-plane/villani_control_plane/services"
    )
    assert "villani_control_plane.api" not in source
    assert "from ..api" not in source


def test_shared_run_model_does_not_import_ui_package() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "components/villani-run-model").rglob("*.ts")
        if "node_modules" not in path.parts and "dist" not in path.parts
    )
    assert "villani-web" not in source


def test_local_data_plane_does_not_import_web_application() -> None:
    source = _python_imports(ROOT / "components/villani-agentd/villani_agentd")
    assert "villani_web" not in source
    assert "villani-web" not in source
