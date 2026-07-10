from pathlib import Path

from villani_code.skills import discover_skills


def test_skill_discovery(tmp_path: Path):
    skill_dir = tmp_path / ".villani" / "skills" / "debug"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: debug\ndescription: dbg\n---\nUse this skill", encoding="utf-8")
    skills = discover_skills(tmp_path)
    assert "debug" in skills
    assert "Use this skill" in skills["debug"].prompt
