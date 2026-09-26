"""The frontend's JavaScript as the browser loads it: frontend/app.js plus its classic-script modules
(frontend/js/*.js), in index.html <script> order. For tests that assert on frontend source text."""

import re
from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "frontend"


def frontend_script_paths() -> list[Path]:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    names = [name for name in re.findall(r'<script src="([^"]+)"></script>', html) if name != "app-config.js"]
    return [FRONTEND / name for name in names]


def frontend_script_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in frontend_script_paths())
