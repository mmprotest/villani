from pathlib import Path

from villani_code.checkpoints import CheckpointManager


def test_checkpoint_and_rewind(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("before", encoding="utf-8")
    cm = CheckpointManager(tmp_path)
    cp = cm.create([Path("a.txt")], message_index=2)
    f.write_text("after", encoding="utf-8")
    cm.rewind(cp.id)
    assert f.read_text(encoding="utf-8") == "before"
