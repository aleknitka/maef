"""Render docs/diagrams/*.mmd to SVG with the Pele skill (.claude/skills/pele), recoloured to the MAEF dark
palette (the same tokens as the diagram-design skin) on an opaque graphite background.
Usage: python3 docs/diagrams/render.py   (or `make diagrams`). Needs Node.js 20+."""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RENDER = HERE.parents[1] / ".claude" / "skills" / "pele" / "scripts" / "render.mjs"
BACKGROUND = '<rect width="100%" height="100%" fill="#14171c"/>'
# Pele's default (static) colours -> MAEF dark tokens.
PALETTE = {"#fff": "#14171c", "#1f1f1f": "#e4e7ec", "#6e6e6e": "#a3acba", "#f3f3f3": "#1c2027",
           "#e6e6e6": "#232832", "#8a8a8a": "#5a6372", "#5b7bd5": "#5b9bff"}
HEX = re.compile(r"(?<![0-9A-Za-z])#(?:fff|1f1f1f|6e6e6e|f3f3f3|e6e6e6|8a8a8a|5b7bd5)(?![0-9A-Za-z])", re.IGNORECASE)


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
        svg = HEX.sub(lambda m: PALETTE[m.group(0).lower()], svg)
        svg = re.sub(r"(<svg\b[^>]*>)", r"\1" + BACKGROUND, svg, count=1)
        target.write_text(svg, encoding="utf-8")
        print(f"ok   {target.relative_to(HERE.parents[1])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
