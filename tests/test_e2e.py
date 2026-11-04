"""Browser tests: real gitlane server + headless Chrome via Playwright.

Skipped automatically when Playwright or Chrome is not installed.
"""

from __future__ import annotations

import threading
import time

import pytest

from .conftest import run, set_home
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


@pytest.fixture(autouse=True)
def no_folder_window(monkeypatch):
    """Never show a real system dialog in tests (the picker then offers the path field)."""
    from gitlane import folder_dialog
    monkeypatch.setattr(folder_dialog, "available", lambda: False)


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

    def _open(repo_path, wait="#graph .row"):
        base = serve(repo_path)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        contexts.append(ctx)
        page = ctx.new_page()
        page.goto(f"{base}/?token={TOKEN}")
        page.wait_for_selector(wait)
        return page

    yield _open
    for c in contexts:
        c.close()


def notice(page):
    return page.wait_for_selector(".banner.notice, .banner.error").inner_text()


def answer_modal(page, answer=None, accept=True):
    """Answer gitlane's own dialog (confirmations and questions)."""
    page.wait_for_selector("#modal[open]")
    if answer is not None:
        page.fill("#modal-input", answer)
    page.click("#modal-ok" if accept else "#modal-cancel")
    page.wait_for_selector("#modal", state="hidden")


# ------------------------------------------------------------------------------

def test_page_loads_status_and_graph(make_repo, open_page):
    path = make_repo()
    page = open_page(path)
    assert page.inner_text("#tabs .tab.active .tab-label") == path.name
    assert page.inner_text("#toolbar .branch") == "main"
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
    page.click("#graph .row >> nth=0", button="right")
    page.wait_for_selector("#ctx-menu:not([hidden])")
    page.click("#ctx-menu button:has-text('Create branch here')")
    answer_modal(page, "from-menu")
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
    page.click("#graph .ref[data-ref-name='feature']", button="right")
    page.click("#ctx-menu button:has-text('Merge feature into main')")
    answer_modal(page)
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
    page.click("#graph .ref[data-ref-name='feature']", button="right")
    page.click("#ctx-menu button:has-text('Merge feature into main')")
    answer_modal(page)
    page.wait_for_selector("#status .operation")
    assert "1 file with conflicts" in page.inner_text("#status .operation")
    page.click("#status .operation button:has-text('Abort merge')")
    answer_modal(page)
    assert "Merge aborted" in page.wait_for_selector(".banner.notice").inner_text()
    assert page.locator("#status .operation").count() == 0
    assert (path / "README.md").read_text() == "main\n"


def test_infinite_scroll(make_repo, open_page, monkeypatch):
    from gitlane import graph

    path = make_repo()
    for i in range(24):
        run(path, "commit", "-q", "--allow-empty", "-m", f"c{i}")
    monkeypatch.setattr(graph.graph_page, "__defaults__", (0, 10, frozenset()))
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
    page.click("#toolbar button:has-text('New branch')")
    answer_modal(page, "feature/ação→nova")
    assert "Created branch feature/ação→nova" in notice(page)
    assert run(path, "branch", "--show-current") == "feature/ação→nova"


# --- stash --------------------------------------------------------------------

def test_stash_save_view_and_pop(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("work in progress\n")
    (path / "novo.txt").write_text("untracked")
    page = open_page(path)
    page.click("#toolbar button:has-text('Stash changes')")
    answer_modal(page, "login pela metade → ação")
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
    assert "Stash changes" in page.inner_text("#toolbar")  # changes are back, can stash again
    assert "Click a commit" in page.inner_text("#detail")  # stale stash view cleared


def test_stash_drop_needs_confirmation(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("x\n")
    page = open_page(path)
    page.click("#toolbar button:has-text('Stash changes')")
    answer_modal(page, "keep me")
    page.wait_for_selector("#status .stash-item")
    item = page.locator("#status .stash-item")
    item.hover()
    item.get_by_role("button", name="Drop").click()
    page.wait_for_selector("#modal[open].danger")  # red, and Cancel has the focus
    assert page.evaluate("document.activeElement.id") == "modal-cancel"
    assert page.inner_text("#modal-ok") == "Delete"
    answer_modal(page, accept=False)
    page.wait_for_timeout(300)
    assert "keep me" in run(path, "stash", "list")  # dismissed -> still there


def test_stash_in_graph_click_and_menu(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("wip\n")
    page = open_page(path)
    page.click("#toolbar button:has-text('Stash changes')")
    answer_modal(page, "from the graph")
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


# --- tabs & repo picker (milestone 7) ------------------------------------------

def tab_names(page):
    return page.locator("#tabs .tab-label").all_inner_texts()


def active_tab(page):
    return page.inner_text("#tabs .tab.active .tab-label")


def test_start_without_repo_shows_picker(open_page, make_repo, tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    make_repo("site")
    page = open_page(None, wait="#picker h1")
    assert page.locator(".layout").is_hidden() and page.locator("#toolbar-row").is_hidden()
    assert tab_names(page) == []
    page.click("#picker [data-open-path]:has-text('site')")  # the browser starts in the home folder
    page.wait_for_selector("#graph .row")
    assert tab_names(page) == ["site"] and page.locator("#picker").is_hidden()


def test_open_second_repo_and_switch(make_repo, open_page):
    a, b = make_repo("alpha"), make_repo("beta")
    (b / "beta-only.txt").write_text("x")
    page = open_page(a)
    page.click("#tabs .tab-new")
    page.fill("#open-path", str(b))
    page.press("#open-path", "Enter")
    page.wait_for_selector("#status button.file:has-text('beta-only.txt')")
    assert tab_names(page) == ["alpha", "beta"] and active_tab(page) == "beta"
    assert page.title() == "beta — gitlane"
    page.click("#tabs .tab-name:has-text('alpha')")
    page.wait_for_selector("#status .clean")
    assert active_tab(page) == "alpha"
    assert page.locator("#status button.file:has-text('beta-only.txt')").count() == 0


def test_actions_go_to_the_active_tab(make_repo, open_page):
    a, b = make_repo("alpha"), make_repo("beta")
    (a / "a.txt").write_text("a")
    (b / "b.txt").write_text("b")
    page = open_page(a)
    page.click("#tabs .tab-new")
    page.fill("#open-path", str(b))
    page.press("#open-path", "Enter")
    page.wait_for_selector("#status button.file:has-text('b.txt')")
    page.click("#status .untracked h2 button:has-text('Stage all')")
    page.wait_for_selector("#status .staged")
    assert run(b, "diff", "--cached", "--name-only") == "b.txt"
    assert run(a, "diff", "--cached", "--name-only") == ""


def test_picker_errors_are_shown(make_repo, open_page, tmp_path):
    page = open_page(make_repo())
    page.click("#tabs .tab-new")
    page.wait_for_selector("#open-path")
    page.fill("#open-path", str(tmp_path / "missing"))
    page.press("#open-path", "Enter")
    assert "does not exist" in page.wait_for_selector("#picker .banner.error").inner_text()
    assert tab_names(page) == ["repo"]


def test_close_tabs(make_repo, open_page):
    a, b = make_repo("alpha"), make_repo("beta")
    page = open_page(a)
    page.click("#tabs .tab-new")
    page.fill("#open-path", str(b))
    page.press("#open-path", "Enter")
    page.wait_for_function("document.title === 'beta — gitlane'")
    page.click("#tabs .tab-close[aria-label='Close beta']")
    page.wait_for_function("document.title === 'alpha — gitlane'")
    assert tab_names(page) == ["alpha"]
    assert (b / ".git").is_dir()  # closing never touches the repo
    page.click("#tabs .tab-close[aria-label='Close alpha']")
    page.wait_for_selector("#picker h1")
    assert tab_names(page) == []
    assert "beta" in page.inner_text("#picker .recent")  # closed repos are offered again


def test_tabs_remembered_after_reload(make_repo, open_page):
    a, b = make_repo("alpha"), make_repo("beta")
    page = open_page(a)
    page.click("#tabs .tab-new")
    page.fill("#open-path", str(b))
    page.press("#open-path", "Enter")
    page.wait_for_function("document.title === 'beta — gitlane'")
    page.reload()
    page.wait_for_selector("#graph .row")
    assert tab_names(page) == ["alpha", "beta"] and active_tab(page) == "beta"


def test_commit_draft_kept_per_tab(make_repo, open_page):
    a, b = make_repo("alpha"), make_repo("beta")
    (a / "README.md").write_text("staged\n")
    run(a, "add", "README.md")
    page = open_page(a)
    page.fill("#summary", "alpha message")
    page.click("#tabs .tab-new")
    page.fill("#open-path", str(b))
    page.press("#open-path", "Enter")
    page.wait_for_function("document.title === 'beta — gitlane'")
    page.wait_for_selector("#commit-hint", state="visible")  # nothing staged in beta
    assert page.input_value("#summary") == ""
    page.click("#tabs .tab-name:has-text('alpha')")
    page.wait_for_function("document.title === 'alpha — gitlane'")
    assert page.input_value("#summary") == "alpha message"


def test_toolbar_stays_visible_when_sidebar_scrolls(make_repo, open_page):
    path = make_repo()
    for i in range(60):
        (path / f"file{i:02}.txt").write_text("x")
    page = open_page(path)
    page.locator(".sidebar").evaluate("el => el.scrollTop = el.scrollHeight")
    assert page.locator("#toolbar button:has-text('Push')").is_visible()
    assert page.locator("#toolbar button:has-text('Stash changes')").is_visible()


def test_slow_answer_for_old_tab_neither_blocks_nor_lands(make_repo, open_page):
    a, b = make_repo("alpha"), make_repo("beta")
    (b / "beta-only.txt").write_text("x")
    page = open_page(a)
    page.click("#tabs .tab-new")
    page.fill("#open-path", str(b))
    page.press("#open-path", "Enter")
    page.wait_for_selector("#status button.file:has-text('beta-only.txt')")
    held = []
    # Hold back the first status answer (beta's), let later ones through.
    page.route("**/partials/status", lambda route: route.continue_() if held else held.append(route))
    page.click("#refresh")
    while not held:
        page.wait_for_timeout(20)
    page.click("#tabs .tab-name:has-text('alpha')")
    page.wait_for_selector("#status .clean")
    try:
        held[0].continue_()  # beta's answer finally arrives (if not cancelled already)
    except Exception:
        pass
    page.wait_for_timeout(400)
    assert page.locator("#status .clean").count() == 1
    assert "beta-only.txt" not in page.inner_text("#status")
    assert page.inner_text("#toolbar .branch") == "main" and active_tab(page) == "alpha"



# --- dialog & page size ---------------------------------------------------------

def test_dialog_keyboard_and_required_name(make_repo, open_page):
    path = make_repo()
    page = open_page(path)
    page.click("#toolbar button:has-text('New branch')")
    page.wait_for_selector("#modal[open]")
    assert page.inner_text("#modal-title") == "New branch"
    assert page.inner_text("#modal-ok") == "Create branch"
    assert page.evaluate("document.activeElement.id") == "modal-input"
    page.keyboard.press("Enter")  # empty name: stays open with a message
    assert page.is_visible("#modal-error") and page.locator("#modal[open]").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal", state="hidden")
    assert run(path, "branch", "--format=%(refname:short)") == "main"
    page.click("#toolbar button:has-text('New branch')")
    page.fill("#modal-input", "via-enter")
    page.keyboard.press("Enter")
    assert "Created branch via-enter" in notice(page)


def test_discard_confirm_uses_dialog(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("changed\n")
    page = open_page(path)
    row = page.locator("#status .files.unstaged li").first
    row.hover()
    row.get_by_role("button", name="Discard changes").click()
    page.wait_for_selector("#modal[open].danger")
    assert page.inner_text("#modal-title") == "Discard your changes to README.md?"
    assert "cannot be undone" in page.inner_text("#modal-text")
    assert page.inner_text("#modal-ok") == "Discard"
    answer_modal(page)
    page.wait_for_selector("#status .clean")
    assert (path / "README.md").read_text() == "hello\n"


@pytest.mark.parametrize("size", [(1280, 720), (1000, 640), (700, 600)])
def test_page_never_scrolls(make_repo, browser, serve, size):
    path = make_repo()
    for i in range(80):
        (path / f"f{i:02}.txt").write_text("x")
        run(path, "add", f"f{i:02}.txt")
        run(path, "commit", "-q", "-m", f"commit {i} with a fairly long subject line to fill the row")
    for i in range(30):
        (path / f"u{i:02}.txt").write_text("x")
    run(path, "add", "u00.txt")  # something staged, so the commit box is shown
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = ctx.new_page()
    page.goto(f"{serve(path)}/?token={TOKEN}")
    page.wait_for_selector("#graph .row")
    page.click("#graph .row >> nth=1")
    page.wait_for_selector("#detail .commit-detail")
    height = page.evaluate("document.documentElement.scrollHeight")
    assert height <= size[1]  # only the panels scroll, never the page
    assert page.locator("#commit-form button[type=submit]").is_visible()
    ctx.close()


# --- header & about -------------------------------------------------------------

def test_many_tabs_shrink_without_scrollbar(make_repo, open_page):
    paths = [make_repo(f"project-with-a-long-name-{i}") for i in range(12)]
    page = open_page(paths[0])
    for p in paths[1:]:
        page.click("#tabs .tab-new")
        page.fill("#open-path", str(p))
        page.press("#open-path", "Enter")
        page.wait_for_function(f"document.title.startsWith({p.name!r})")
    tabs = page.locator("#tabs")
    assert tabs.evaluate("el => el.scrollWidth <= el.clientWidth")  # nothing hidden behind a scroll
    assert tabs.evaluate("el => getComputedStyle(el).overflowX") == "hidden"
    assert page.locator("#tabs .tab-new").is_visible() and page.locator("#about-btn").is_visible()
    badge = page.locator("#tabs .tab.active .repo-badge")
    assert badge.inner_text() == "P"


def test_about_window(make_repo, open_page):
    from gitlane import __version__
    page = open_page(make_repo())
    page.click("#about-btn")
    page.wait_for_selector("#about[open]")
    text = page.inner_text("#about")
    assert "MIT License" in text and "Gustavo Leite" in text and __version__ in text
    assert "contribute on github" in text.lower() and "pull request" in text
    link = page.locator("#about a:has-text('Repository')")
    assert link.get_attribute("href") == "https://github.com/gustavojoseleite/gitlane"
    assert link.get_attribute("target") == "_blank" and "noopener" in link.get_attribute("rel")
    page.click("#about summary")
    assert "THE SOFTWARE IS PROVIDED" in page.inner_text("#about pre.license")
    page.keyboard.press("Escape")
    page.wait_for_selector("#about", state="hidden")



# --- commit box & remote branches ----------------------------------------------------

def test_commit_box_only_when_something_is_staged(make_repo, open_page):
    path = make_repo()
    (path / "README.md").write_text("changed\n")
    page = open_page(path)
    page.wait_for_selector("#commit-hint", state="visible")
    assert page.locator("#commit-form").is_hidden()
    page.click("#status .unstaged h2 button:has-text('Stage all')")
    page.wait_for_selector("#commit-form", state="visible")
    assert page.locator("#commit-hint").is_hidden()
    page.fill("#summary", "draft")
    page.click("#status .staged h2 button:has-text('Unstage all')")
    page.wait_for_selector("#commit-form button[type=submit]:disabled")
    # a typed message never disappears; it just can't be committed yet
    assert page.locator("#commit-form").is_visible()


def test_delete_remote_branch_from_graph_menu(make_repo, open_page, tmp_path):
    path = make_repo()
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(path, "remote", "add", "origin", str(tmp_path / "server.git"))
    run(path, "push", "-q", "-u", "origin", "main")
    run(path, "push", "-q", "origin", "main:old-feature")
    run(path, "fetch", "-q")
    page = open_page(path)
    page.click("#graph .ref[data-ref-name='origin/old-feature']", button="right")
    page.click("#ctx-menu button:has-text('Delete old-feature on origin')")
    page.wait_for_selector("#modal[open].danger")
    assert page.inner_text("#modal-title") == "Delete old-feature on origin?"
    answer_modal(page)
    assert "Deleted old-feature on origin." in notice(page)
    assert run(tmp_path / "server.git", "branch", "--format=%(refname:short)") == "main"
    page.wait_for_selector("#graph .ref[data-ref-name='origin/old-feature']", state="detached")


def test_delete_remote_branch_from_sidebar(make_repo, open_page, tmp_path):
    path = make_repo()
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(path, "remote", "add", "origin", str(tmp_path / "server.git"))
    run(path, "push", "-q", "-u", "origin", "main")
    run(path, "push", "-q", "origin", "main:gone-soon")
    run(path, "fetch", "-q")
    page = open_page(path)
    page.click("#remote-branches summary")
    row = page.locator("#remote-branches li", has_text="origin/gone-soon")
    row.hover()
    row.get_by_role("button", name="Delete on the remote").click()
    answer_modal(page, accept=False)  # cancelled: nothing happens
    assert "gone-soon" in run(tmp_path / "server.git", "branch", "--format=%(refname:short)")
    row.hover()
    row.get_by_role("button", name="Delete on the remote").click()
    answer_modal(page)
    assert "Deleted gone-soon on origin." in notice(page)


def test_minimize_sections(make_repo, open_page):
    path = make_repo()
    for i in range(20):
        (path / f"untracked{i:02}.txt").write_text("x")
    page = open_page(path)
    untracked = page.locator("#status section.untracked")
    assert "collapsed" in untracked.get_attribute("class")  # 20 files: minimized by default
    assert untracked.locator("ul").is_hidden()
    assert untracked.locator("h2 button:has-text('Stage all')").is_visible()  # still usable
    untracked.locator(".sec-toggle").click()
    assert untracked.locator("ul").is_visible()
    branches = page.locator("#status section.branches")
    branches.locator(".sec-toggle").click()  # short list, minimized by hand
    assert branches.locator("ul").is_hidden()
    assert branches.locator(".sec-toggle").get_attribute("aria-expanded") == "false"
    page.click("#refresh")  # choices survive refreshes and reloads
    page.wait_for_timeout(300)
    page.reload()
    page.wait_for_selector("#status section.untracked")
    assert page.locator("#status section.untracked ul").is_visible()
    assert page.locator("#status section.branches ul").is_hidden()


def test_merged_label_menu_offers_remote_delete(make_repo, open_page, tmp_path):
    path = make_repo()
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(path, "remote", "add", "origin", str(tmp_path / "server.git"))
    run(path, "push", "-q", "-u", "origin", "main")
    run(path, "checkout", "-q", "-b", "topic")
    run(path, "push", "-q", "-u", "origin", "topic")
    run(path, "checkout", "-q", "main")
    page = open_page(path)
    label = page.locator("#graph .ref[data-ref-name='topic']")
    assert label.get_attribute("data-ref-remote") == "origin/topic"
    assert label.locator(".where svg").count() == 2  # laptop + cloud
    label.click(button="right")
    labels = page.locator("#ctx-menu button").all_inner_texts()
    assert "Delete topic…" in labels and "Delete topic on origin…" in labels


def test_create_tag_from_menu_and_delete_from_sidebar(make_repo, open_page):
    path = make_repo()
    page = open_page(path)
    page.click("#graph .row >> nth=0", button="right")
    page.wait_for_selector("#ctx-menu:not([hidden])")
    page.click("#ctx-menu button:has-text('Create tag here')")
    page.wait_for_selector("#modal[open] #modal-extra:not([hidden])")
    page.fill("#modal-extra-input", "First release\nwith notes")
    answer_modal(page, "v1.0")
    assert "Created tag v1.0" in notice(page)
    assert run(path, "cat-file", "-t", "v1.0") == "tag"  # annotated: it had a message
    page.wait_for_selector("#graph .ref[data-ref-kind='tag'][data-ref-name='v1.0']")
    item = page.wait_for_selector(".tags .tag-item[data-ref-name='v1.0']")
    item.click()  # shows its commit
    page.wait_for_selector("#detail .commit-detail")
    # Other dialogs don't show the extra field
    page.click("#graph .row >> nth=0", button="right")
    page.click("#ctx-menu button:has-text('Create branch here')")
    page.wait_for_selector("#modal[open]")
    assert page.is_hidden("#modal-extra")
    answer_modal(page, accept=False)
    page.click(".tags .tag-item[data-ref-name='v1.0']", button="right")
    page.click("#ctx-menu button:has-text('Delete tag v1.0…')")
    answer_modal(page)
    page.wait_for_selector(".banner.notice:has-text('Deleted tag v1.0')")
    assert run(path, "tag") == ""
    page.wait_for_selector(".tags", state="detached")  # no tags left: no section


def test_hide_and_show_branch(make_repo, open_page):
    path = make_repo()
    run(path, "switch", "-q", "-c", "side")
    run(path, "commit", "-q", "--allow-empty", "-m", "side work")
    run(path, "switch", "-q", "main")
    page = open_page(path)
    page.wait_for_selector("#graph .row:has-text('side work')")
    page.click("#graph .ref[data-ref-name='side']", button="right")
    page.click("#ctx-menu button:has-text('Hide side in the graph')")
    page.wait_for_selector(".banner.notice:has-text('Hid side')")
    page.wait_for_selector("#graph .row:has-text('side work')", state="detached")
    page.wait_for_selector(".branches .branch-item.is-hidden[data-ref-name='side']")
    page.click(".branches .branch-item[data-ref-name='side'] .show-btn")
    page.wait_for_selector("#graph .row:has-text('side work')")
    assert page.locator(".branches .is-hidden").count() == 0


def test_choose_folder_window(make_repo, open_page, monkeypatch):
    from gitlane import folder_dialog
    other = make_repo("chosen")
    monkeypatch.setattr(folder_dialog, "available", lambda: True)
    answers = iter([None, other])  # first Cancel, then a folder
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda start=None: next(answers))
    page = open_page(make_repo())
    page.click("#tabs .tab-new")
    page.click("#picker [data-choose-folder]")
    page.wait_for_selector("#picker [data-choose-folder]:not([disabled])")
    assert tab_names(page) == ["repo"] and page.locator("#picker .banner.error").count() == 0
    assert page.locator("#open-path").count() == 0  # the window replaces the path field
    page.click("#picker [data-choose-folder]")
    page.wait_for_selector("#tabs .tab.active:has-text('chosen')")
    assert page.locator("#picker").is_hidden()
