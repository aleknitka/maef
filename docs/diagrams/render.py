"""Render docs/diagrams/*.mmd to SVG with the Pele skill (.claude/skills/pele), then paint a white
background so the diagrams stay readable on dark themes (Pele's SVGs are transparent).
Usage: python3 docs/diagrams/render.py   (or `make diagrams`). Needs Node.js 20+."""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RENDER = HERE.parents[1] / ".claude" / "skills" / "pele" / "scripts" / "render.mjs"
BACKGROUND = '<rect width="100%" height="100%" fill="#ffffff"/>'


def main() -> int:
    if not RENDER.is_file():
        print("Pele skill missing: npx skills add https://github.com/jpmanson/pele-skill --skill pele -a claude-code",
              file=sys.stderr)
        return 2
    failed = 0
    for source in sorted(HERE.glob("*.mmd")):
        target = source.with_suffix(".svg")
        result = subprocess.run(["node", str(RENDER), "--static-colors", str(source), str(target)],
                                check=False, capture_output=True, text=True)
        receipt = (result.stderr.strip().splitlines() or ["(no receipt)"])[-1]
        if result.returncode != 0:
            print(f"FAIL {source.name}: {receipt}", file=sys.stderr)
            failed += 1
            continue
        svg = target.read_text(encoding="utf-8")
        svg = re.sub(r"(<svg\b[^>]*>)", r"\1" + BACKGROUND, svg, count=1)
        target.write_text(svg, encoding="utf-8")
        print(f"ok   {target.relative_to(HERE.parents[1])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
