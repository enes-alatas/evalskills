import sys

from playwright.sync_api import sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
    for name, vp in (("desktop", {"width": 1280, "height": 900}), ("mobile", {"width": 390, "height": 844})):
        pg = b.new_page(viewport=vp)
        pg.goto(base)
        pg.wait_for_selector("#board tbody tr")
        pg.screenshot(path=f"/home/loop/work/shots/{name}-board.png")
        pg.click("#board tbody tr")
        pg.wait_for_selector("#detail-body h2")
        pg.wait_for_timeout(500)
        pg.screenshot(path=f"/home/loop/work/shots/{name}-detail.png")
        pg.keyboard.press("Escape")
        pg.click("#add-btn")
        pg.fill("#url", "https://github.com/vercel-labs/agent-skills")
        pg.click("#lookup")
        pg.wait_for_selector("#found:not([hidden])", timeout=60000)
        pg.screenshot(path=f"/home/loop/work/shots/{name}-add.png")
    b.close()
