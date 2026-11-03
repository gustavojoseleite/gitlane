"""Browser tests: real gitlane server + headless Chrome via Playwright.

Skipped automatically when Playwright or Chrome is not installed.
"""

from __future__ import annotations

import threading
import time

import pytest

from .conftest import run
from .test_blobs import PNG

sync_api = pytest.importorskip("playwright.sync_api")

TOKEN = "e2e-token"


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # pragma: no cover - depends on the machine
            pytest.skip(f"Chrome not available: {e}")
        yield b
        b.close()


@pytest.fixture
def serve():
    """Start gitlane for a repo; returns the base URL."""
    import uvicorn

    from gitlane.cli import find_free_port
    from gitlane.server import create_app

    servers = []

    def _serve(repo_path):
        port = find_free_port()
        server = uvicorn.Server(uvicorn.Config(create_app(repo_path, TOKEN, port),
                                               host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.02)
        servers.append(server)
        return f"http://127.0.0.1:{port}"

    yield _serve
    for s in servers:
        s.should_exit = True


@pytest.fixture
def open_page(browser, serve):
    contexts = []

    def _open(repo_path):
        base = serve(repo_path)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        contexts.append(ctx)
        page = ctx.new_page()
        page.goto(f"{base}/?token={TOKEN}")
        page.wait_for_selector("#graph .row")
        return page

    yield _open
    for c in contexts:
        c.close()


def notice(page):
    return page.wait_for_selector(".banner.notice, .banner.error").inner_text()


def accept_dialogs(page, answer=None):
    page.on("dialog", lambda d: d.accept(answer) if answer is not None else d.accept())


# ------------------------------------------------------------------------------

def test_page_loads_status_and_graph(make_repo, open_page):
    path = make_repo()
    page = open_page(path)
    assert page.inner_text("#status h1") == path.name
    assert page.locator("#graph .row").count() == 1
    assert "Initial commit" in page.inner_text("#graph")


def test_stage_and_commit_with_description(make_repo, open_page):
    path = make_repo()
    (path / "new.txt").write_text("hello")
    page = open_page(path)
    item = page.locator("#status li", has_text="new.txt")
    item.hover()
    item.get_by_role("button", name="Stage").click()
    page.wait_for_selector("#status .files.staged")
    page.fill("#summary", "Add greeting")
    page.press("#summary", "Enter")  # moves to description, must NOT commit
    assert page.evaluate("document.activeElement.id") == "description"
    page.keyboard.type("Explains why.\nSecond line.")
    page.keyboard.press("Control+Enter")
    assert "Committed" in notice(page)
    assert run(path, "log", "-1", "--format=%s") == "Add greeting"
    assert run(path, "log", "-1", "--format=%b") == "Explains why.\nSecond line."
    assert page.input_value("#summary") == "" and page.input_value("#description") == ""
    page.wait_for_function("document.querySelector('#graph .row .subject').textContent === 'Add greeting'")


def test_click_commit_shows_description(make_repo, open_page):
    path = make_repo()
    run(path, "commit", "-q", "--allow-empty", "-m", "Title", "-m", "The longer description.")
    page = open_page(path)
    page.click("#graph .row >> nth=0")
    page.wait_for_selector("#detail .commit-detail")
    assert page.inner_text("#detail h2.subject") == "Title"
    assert page.inner_text("#detail .body") == "The longer description."


def test_keyboard_navigation(make_repo, open_page):
    path = make_repo()
    run(path, "commit", "-q", "--allow-empty", "-m", "second")
    page = open_page(path)
    page.click("#graph .row >> nth=0")
    page.wait_for_selector("#detail h2.subject")
    page.keyboard.press("ArrowDown")
    page.wait_for_function("document.querySelector('#detail h2.subject')?.textContent === 'Initial commit'")
    assert "selected" in page.get_attribute("#graph .row >> nth=1", "class")


def test_image_preview_loads(make_repo, open_page):
    path = make_repo()
    (path / "pic.png").write_bytes(PNG)
    run(path, "add", "pic.png")
    run(path, "commit", "-q", "-m", "add picture")
    page = open_page(path)
    page.click("#graph .row >> nth=0")
    page.click("#detail button.file:has-text('pic.png')")
    img = page.wait_for_selector("#commit-diff .image-compare img")
    page.wait_for_function("(img) => img.complete && img.naturalWidth > 0", arg=img)
    assert "No file" in page.inner_text("#commit-diff")  # nothing before


def test_context_menu_create_branch(make_repo, open_page):
    path = make_repo()
    page = open_page(path)
    accept_dialogs(page, "from-menu")
    page.click("#graph .row >> nth=0", button="right")
    page.wait_for_selector("#ctx-menu:not([hidden])")
    page.click("#ctx-menu button:has-text('Create branch here')")
    assert "Created branch from-menu" in notice(page)
    assert run(path, "branch", "--show-current") == "from-menu"
    page.wait_for_selector("#graph .ref[data-ref-name='from-menu']")  # graph reloaded


def test_menu_closes_on_escape(make_repo, open_page):
    page = open_page(make_repo())
    page.click("#graph .row >> nth=0", button="right")
    page.wait_for_selector("#ctx-menu:not([hidden])")
    page.keyboard.press("Escape")
    page.wait_for_selector("#ctx-menu", state="hidden")


def test_branch_label_menu_merge(make_repo, open_page):
    path = make_repo()
    run(path, "checkout", "-q", "-b", "feature")
    run(path, "commit", "-q", "--allow-empty", "-m", "feature work")
    run(path, "checkout", "-q", "main")
    page = open_page(path)
    accept_dialogs(page)
    page.click("#graph .ref[data-ref-name='feature']", button="right")
    page.click("#ctx-menu button:has-text('Merge feature into main')")
    assert "Merged feature into main" in notice(page)
    assert run(path, "log", "-1", "--format=%s") == "feature work"


def test_sidebar_switch_branch(make_repo, open_page):
    path = make_repo()
    run(path, "branch", "other")
    page = open_page(path)
    page.click("#status .branch-item button.file:has-text('other')")
    assert "Switched to other" in notice(page)
    assert run(path, "branch", "--show-current") == "other"


def test_conflict_box_abort(make_repo, open_page):
    path = make_repo()
    run(path, "checkout", "-q", "-b", "feature")
    (path / "README.md").write_text("feature\n")
    run(path, "commit", "-q", "-am", "feature")
    run(path, "checkout", "-q", "main")
    (path / "README.md").write_text("main\n")
    run(path, "commit", "-q", "-am", "main")
    page = open_page(path)
    accept_dialogs(page)
    page.click("#graph .ref[data-ref-name='feature']", button="right")
    page.click("#ctx-menu button:has-text('Merge feature into main')")
    page.wait_for_selector("#status .operation")
    assert "1 file with conflicts" in page.inner_text("#status .operation")
    page.click("#status .operation button:has-text('Abort merge')")
    assert "Merge aborted" in page.wait_for_selector(".banner.notice").inner_text()
    assert page.locator("#status .operation").count() == 0
    assert (path / "README.md").read_text() == "main\n"


def test_infinite_scroll(make_repo, open_page, monkeypatch):
    from gitlane import graph

    path = make_repo()
    for i in range(24):
        run(path, "commit", "-q", "--allow-empty", "-m", f"c{i}")
    monkeypatch.setattr(graph.graph_page, "__defaults__", (0, 10))
    page = open_page(path)
    # Pages load as the sentinel scrolls into view (several at once if the screen is tall).
    assert page.locator("#graph .row").count() < 25
    for _ in range(5):
        page.eval_on_selector(".graph-panel", "el => el.scrollTop = el.scrollHeight")
        page.wait_for_timeout(150)
    page.wait_for_function("document.querySelectorAll('#graph .row').length === 25")


# --- layout -------------------------------------------------------------------

def _width(page, selector):
    return page.eval_on_selector(selector, "el => el.getBoundingClientRect().width")


def _drag(page, selector, dx=0, dy=0):
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=5)
    page.mouse.up()


def test_resize_panels_persist_and_reset(make_repo, open_page):
    page = open_page(make_repo())
    side, detail = _width(page, ".sidebar"), _width(page, ".detail-panel")
    _drag(page, "#g1", dx=80)
    _drag(page, "#g2", dx=-120)  # details grow when dragged left
    assert abs(_width(page, ".sidebar") - (side + 80)) <= 2
    assert abs(_width(page, ".detail-panel") - (detail + 120)) <= 2
    page.reload()
    page.wait_for_selector("#graph .row")
    assert abs(_width(page, ".sidebar") - (side + 80)) <= 2  # remembered
    page.dblclick("#g1")
    assert abs(_width(page, ".sidebar") - side) <= 2


def test_resize_has_limits_and_keyboard(make_repo, open_page):
    page = open_page(make_repo())
    _drag(page, "#g1", dx=-2000)
    assert _width(page, ".sidebar") >= 219
    _drag(page, "#g1", dx=4000)
    assert _width(page, ".sidebar") <= 1600 * 0.45 + 2
    page.dblclick("#g1")
    before = _width(page, ".sidebar")
    page.focus("#g1")
    page.keyboard.press("ArrowRight")
    assert abs(_width(page, ".sidebar") - (before + 16)) <= 2


def test_stacked_layout_vertical_resize(make_repo, browser, serve):
    base = serve(make_repo())
    ctx = browser.new_context(viewport={"width": 1100, "height": 800})
    page = ctx.new_page()
    page.goto(f"{base}/?token={TOKEN}")
    page.wait_for_selector("#graph .row")
    assert page.get_attribute("#g2", "aria-orientation") == "horizontal"
    h = page.eval_on_selector(".graph-panel", "el => el.getBoundingClientRect().height")
    _drag(page, "#g2", dy=-100)
    h2 = page.eval_on_selector(".graph-panel", "el => el.getBoundingClientRect().height")
    assert abs(h2 - (h - 100)) <= 2
    ctx.close()


def test_branch_labels_are_readable(make_repo, open_page):
    path = make_repo()
    run(path, "branch", "feature/melhorias_visual")
    run(path, "commit", "-q", "--allow-empty", "-m", "x")
    page = open_page(path)
    label = page.locator("#graph .ref-branch", has_text="feature/melhorias_visual")
    color, bg = label.evaluate("el => [getComputedStyle(el).color, getComputedStyle(el).backgroundColor]")
    assert color != bg


def test_dates_not_cut_off(make_repo, open_page):
    page = open_page(make_repo())
    over = page.eval_on_selector("#graph .row .date", "el => el.scrollWidth > el.clientWidth")
    assert not over


def test_new_branch_button_keeps_name_intact(make_repo, open_page):
    path = make_repo()
    page = open_page(path)
    accept_dialogs(page, "feature/ação→nova")
    page.click("#status button:has-text('New branch')")
    assert "Created branch feature/ação→nova" in notice(page)
    assert run(path, "branch", "--show-current") == "feature/ação→nova"


# --- stash --------------------------------------------------------------------

def test_stash_save_view_and_pop(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("work in progress\n")
    (path / "novo.txt").write_text("untracked")
    page = open_page(path)
    accept_dialogs(page, "login pela metade → ação")
    page.click("#status button:has-text('Stash changes')")
    assert "Stashed your changes" in notice(page)
    assert "login pela metade → ação" in page.inner_text("#status .stashes")
    assert page.locator("#status .clean").count() == 1

    page.click("#status .stash-item button.file")
    page.wait_for_selector("#detail .stash-detail")
    page.click("#detail button.file:has-text('novo.txt')")
    page.wait_for_selector("#commit-diff tr.add")

    page.click("#detail button:has-text('Pop (apply and delete)')")
    page.wait_for_selector("#status .banner.notice:has-text('Applied and removed')")
    assert (path / "novo.txt").read_text() == "untracked"
    assert page.locator("#status .stash-item").count() == 0
    assert "Stash changes" in page.inner_text("#status .stashes")  # changes are back, can stash again
    assert "Click a commit" in page.inner_text("#detail")  # stale stash view cleared


def test_stash_drop_needs_confirmation(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("x\n")
    page = open_page(path)
    answers = iter(["keep me", None])  # prompt answer, then dismiss the confirm
    page.on("dialog", lambda d: d.accept(next(answers)) if d.type == "prompt" else d.dismiss())
    page.click("#status button:has-text('Stash changes')")
    page.wait_for_selector("#status .stash-item")
    item = page.locator("#status .stash-item")
    item.hover()
    item.get_by_role("button", name="Drop").click()
    page.wait_for_timeout(300)
    assert "keep me" in run(path, "stash", "list")  # dismissed -> still there


def test_stash_in_graph_click_and_menu(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("wip\n")
    page = open_page(path)
    accept_dialogs(page, "from the graph")
    page.click("#status button:has-text('Stash changes')")
    row = page.wait_for_selector("#graph .row.stash-row")  # graph reloads by itself
    assert "stash@{0}" in row.inner_text() and "from the graph" in row.inner_text()
    row.click()
    page.wait_for_selector("#detail .stash-detail:has-text('from the graph')")
    page.locator("#graph .row.stash-row").click(button="right")
    labels = page.locator("#ctx-menu button").all_inner_texts()
    assert labels == ["Apply (keep the stash)", "Pop (apply and delete)", "Drop stash@{0}…"]
    page.click("#ctx-menu button:has-text('Pop')")
    page.wait_for_selector("#status .banner.notice:has-text('Applied and removed')")
    page.wait_for_selector("#graph .row.stash-row", state="detached")
    assert (path / "README.md").read_text() == "wip\n"


def test_file_names_get_the_row_width(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("changed\n")
    page = open_page(path)
    row = page.locator("#status .files.unstaged li").first
    name = row.locator("button.file")
    row_w = row.bounding_box()["width"]
    assert name.bounding_box()["width"] > row_w * 0.55  # names aren't squeezed by hidden buttons
    assert page.eval_on_selector("#status .files.unstaged button.file",
                                 "el => el.getBoundingClientRect().height") < 30  # one line


def test_row_buttons_never_cover_the_name(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("changed\n")
    page = open_page(path)
    row = page.locator("#status .files.unstaged li").first
    row.hover()
    name = row.locator("button.file").bounding_box()
    for b in row.locator(".actions button").all():
        box = b.bounding_box()
        assert box["x"] >= name["x"] + name["width"] - 1  # buttons sit after the name
