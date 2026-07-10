from villani_code.hooks import HookRunner


def test_hook_blocks_action():
    runner = HookRunner(hooks={"PreToolUse": [{"type": "shell", "command": "python -c 'import sys; sys.exit(1)'"}]})
    result = runner.run_event("PreToolUse", {"tool": "Write", "input": {}})
    assert not result.allow
