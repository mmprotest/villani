from pathlib import Path

from villani_code.subagents import load_subagents


def test_subagent_loading_and_builtin_restrictions(tmp_path: Path):
    agents = load_subagents(tmp_path)
    assert "Explore" in agents
    assert "Write" in (agents["Explore"].denied_tools or [])

    d = tmp_path / ".villani" / "agents"
    d.mkdir(parents=True)
    (d / "custom.json").write_text('{"name":"Audit","denied_tools":["Bash"]}', encoding="utf-8")
    agents = load_subagents(tmp_path)
    assert "Audit" in agents
    assert agents["Audit"].denied_tools == ["Bash"]
