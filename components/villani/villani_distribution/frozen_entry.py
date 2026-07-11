from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    name = Path(sys.argv[0]).stem.lower()
    if name == "villani-agentd":
        from villani_agentd.cli import main as agentd_main

        return agentd_main()
    if name == "villani-code":
        from villani_code.cli import app

        app(prog_name="villani-code")
        return 0
    from villani_distribution.cli import main as villani_main

    return villani_main()


if __name__ == "__main__":
    raise SystemExit(main())
