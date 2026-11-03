"""Make the README screenshots from a made-up demo repository.

    .venv/bin/python scripts/screenshots.py

Needs the e2e extra (Playwright) and Google Chrome. Everything happens in a
temporary folder with its own Git and gitlane settings, so your real repos
and remembered tabs are never shown or touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
sys.path.insert(0, str(ROOT / "src"))

PEOPLE = [("Ana Souza", "ana@example.com"), ("Bruno Lima", "bruno@example.com"),
          ("Carla Mendes", "carla@example.com")]


def git(repo: Path, *args: str, who: int = 0, when: int = 0) -> None:
    name, email = PEOPLE[who % len(PEOPLE)]
    date = f"2026-10-{1 + when // 4:02d}T{9 + when % 4 * 2:02d}:15:00"
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
           "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def make_demo(base: Path) -> Path:
    repo = base / "acme-shop"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    n = 0

    def commit(path: str, text: str, msg: str, who: int = 0) -> None:
        nonlocal n
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        git(repo, "add", path, who=who, when=n)
        git(repo, "commit", "-q", "-m", msg, who=who, when=n)
        n += 1

    commit("README.md", "# Acme Shop\n", "Initial commit")
    commit("src/app.py", "app = 'shop'\n", "Add the web app skeleton", who=1)
    commit("src/cart.py", "def total(items):\n    return sum(items)\n", "Add a shopping cart", who=2)
    git(repo, "tag", "v0.1.0")
    git(repo, "checkout", "-q", "-b", "feature/checkout")
    commit("src/checkout.py", "def pay():\n    pass\n", "Start the checkout page", who=1)
    commit("src/checkout.py", "def pay(card):\n    return card.charge()\n", "Charge the card on checkout", who=1)
    git(repo, "checkout", "-q", "main")
    commit("src/cart.py", "def total(items):\n    return round(sum(items), 2)\n",
           "Round cart totals to cents\n\nPrices like 0.1 + 0.2 showed 0.30000000000000004 in the cart. "
           "Totals are now rounded to two decimals before they are shown.", who=2)
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'feature/checkout'", "feature/checkout",
        when=n)
    n += 1
    git(repo, "checkout", "-q", "-b", "fix/login-redirect")
    commit("src/login.py", "NEXT = '/'\n", "Send users back to the page they came from", who=0)
    git(repo, "checkout", "-q", "main")
    commit("docs/setup.md", "Run `make dev`.\n", "Document the local setup", who=0)
    commit("src/app.py", "app = 'shop'\nDEBUG = False\n", "Turn off debug mode in production", who=1)
    (repo / "src" / "app.py").write_text("app = 'shop'\nDEBUG = False\nTHEME = 'dark'\n")
    git(repo, "stash", "push", "-q", "-m", "try a dark theme", when=n)
    (repo / "src" / "cart.py").write_text("def total(items, discount=0):\n    return round(sum(items) - discount, 2)\n")
    git(repo, "add", "src/cart.py")
    (repo / "README.md").write_text("# Acme Shop\n\nAn example online shop.\n")
    (repo / "notes.txt").write_text("ideas\n")
    return repo


def main() -> None:
    import uvicorn
    from playwright.sync_api import sync_playwright

    from gitlane.cli import find_free_port
    from gitlane.server import create_app
    from gitlane.workspace import Workspace

    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        os.environ.update(HOME=str(base), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(base / "gitconfig"))
        (base / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
        repo = make_demo(base)
        other = base / "website"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "commit", "-q", "--allow-empty", "-m", "init")
        ws = Workspace(base / "state.json")
        ws.open(other)
        port = find_free_port()
        server = uvicorn.Server(uvicorn.Config(create_app(repo, "t", port, ws), host="127.0.0.1",
                                               port=port, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.02)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            for scheme in ("light", "dark"):
                ctx = browser.new_context(viewport={"width": 1440, "height": 860}, color_scheme=scheme,
                                          device_scale_factor=1)
                page = ctx.new_page()
                page.goto(f"http://127.0.0.1:{port}/?token=t")
                page.wait_for_selector("#graph .row")
                page.click("#graph .row:has-text('Round cart totals')")
                page.wait_for_selector("#detail .commit-detail")
                page.click("#detail button.file:has-text('cart.py')")
                page.wait_for_selector("#commit-diff table.diff")
                page.wait_for_timeout(300)
                page.screenshot(path=str(OUT / f"main-{scheme}.png"))
                if scheme == "light":
                    page.click("#graph .ref[data-ref-name='fix/login-redirect']", button="right")
                    page.wait_for_timeout(200)
                    page.screenshot(path=str(OUT / "menu.png"))
                ctx.close()
            browser.close()
        server.should_exit = True
    print(f"Saved screenshots to {OUT}")


if __name__ == "__main__":
    main()
