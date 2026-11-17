"""Screenshots of the board, a detail sheet and Add skill, desktop and mobile, for PR descriptions (CONVENTIONS).

  python3 -I pipeline/screens.py [BASE_URL] [OUT_DIR]

OUT_DIR defaults to SHOTS_DIR, else shots/ in this repository (gitignored). CHROMIUM sets the browser binary."""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
out = Path(
    sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SHOTS_DIR") or Path(__file__).resolve().parents[1] / "shots"
)
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM", "/usr/bin/chromium"), args=["--no-sandbox"])
    for name, vp in (("desktop", {"width": 1280, "height": 900}), ("mobile", {"width": 390, "height": 844})):
        pg = b.new_page(viewport=vp)
        pg.goto(base)
        pg.wait_for_selector("#board tbody tr")
        pg.screenshot(path=out / f"{name}-board.png")
        pg.click("#board tbody tr")
        pg.wait_for_selector("#detail-body h2")
        pg.wait_for_timeout(500)
        pg.screenshot(path=out / f"{name}-detail.png")
        pg.keyboard.press("Escape")
        pg.click("#add-btn")
        pg.fill("#url", "https://github.com/vercel-labs/agent-skills")
        pg.click("#lookup")
        pg.wait_for_selector("#found:not([hidden])", timeout=60000)
        pg.screenshot(path=out / f"{name}-add.png")
    b.close()
