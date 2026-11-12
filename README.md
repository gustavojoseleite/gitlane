# gitlane

**A free, open-source Git client that runs in your browser.** Commit, push, pull, see the
commit graph, manage branches and stashes — in a clean local web app, with one command.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/gustavojoseleite/gitlane/main/docs/screenshots/main-dark.png">
  <img alt="gitlane showing a repository: submodules, branches and tags on the left, the commit graph in the middle, changed files, stashes and the commit box on the right" src="https://raw.githubusercontent.com/gustavojoseleite/gitlane/main/docs/screenshots/main-light.png">
</picture>

## Features

- **Changes** — see changed, staged and untracked files; stage, unstage or discard whole files; commit with a summary and description
- **Diffs** — for your working files and any commit, opened in place of the graph, with **before/after previews for images**
- **Commit graph** — all branches with colored lanes, branch/tag labels and your stashes; fast on repositories with tens of thousands of commits
- **Right-click menus** — checkout, create a branch, cherry-pick, revert, reset (soft / mixed / hard), merge, delete branches (also on the remote)
- **Sync** — fetch, pull (which also moves your submodules to the commits the new history records), push, and force push that always uses `--force-with-lease`
- **Submodules** — see which are out of date or not set up, what changed (commits added or removed), set them up or move them back, and open one in its own tab
- **Hide branches** — keep the graph focused: hide a branch (or all remote branches) and show it again any time
- **Tags** — create (lightweight or with a message), push, delete here or on the remote
- **Stash** — save all your changes or only the files you tick (with or without untracked files), apply, pop — onto the current branch, another branch or a new one — drop, look inside a stash, and stash the changes inside a submodule
- **Merge conflicts** — see the conflicted files, then *abort* or *mark resolved and commit*
- **Tabs** — several repositories at once; open more with your system's *Choose folder* window, recent repos or the built-in folder browser
- **Safe by default** — dangerous actions ask first; runs only on your computer (see [Security](#security))
- Light and dark theme (follows your system)

![Right-click menu on a branch, with the commit details on the right](https://raw.githubusercontent.com/gustavojoseleite/gitlane/main/docs/screenshots/menu.png)

![A file's diff opened in place of the graph](https://raw.githubusercontent.com/gustavojoseleite/gitlane/main/docs/screenshots/diff.png)

## Install

You need **Python 3.10+** and **Git 2.23+** (most systems already have both).

The recommended way is [pipx](https://pipx.pypa.io/) or [uv](https://docs.astral.sh/uv/). They
install gitlane in its own environment, so it never conflicts with your other Python projects:

```bash
pipx install gitlane
# or
uv tool install gitlane
```

Just want to try it, without installing anything? With uv:

```bash
uvx gitlane
```

<details>
<summary>Don't have pipx or uv yet?</summary>

| System | Install pipx |
|---|---|
| Ubuntu / Debian | `sudo apt install pipx && pipx ensurepath` |
| Fedora | `sudo dnf install pipx && pipx ensurepath` |
| macOS | `brew install pipx && pipx ensurepath` |
| Windows | `py -m pip install --user pipx` then `py -m pipx ensurepath` |

Open a new terminal afterwards so the `gitlane` command is found.
</details>

<details>
<summary>Install the latest development version from GitHub</summary>

```bash
pipx install git+https://github.com/gustavojoseleite/gitlane
```
</details>

**Uninstall:** `pipx uninstall gitlane` (or `uv tool uninstall gitlane`). Your remembered tabs stay in
gitlane's settings folder (see [Usage](#usage)); delete that folder too if you want them gone.

## Updating

Already have gitlane installed? Update it with the same tool you installed it with:

| Installed with | Update with |
|---|---|
| pipx | `pipx upgrade gitlane` |
| uv | `uv tool upgrade gitlane` |
| `uvx gitlane` | nothing to do — `uvx gitlane@latest` always runs the newest version |
| pip (in a virtual environment) | `pip install --upgrade gitlane` |
| from GitHub | `pipx install --force git+https://github.com/gustavojoseleite/gitlane` |

Then:

1. **Stop gitlane** if it is running (Ctrl+C in its terminal) and **start it again**. A running gitlane keeps
   the old version until it restarts; reload the browser tab after it starts.
2. **Check the version:** `gitlane --version` (or *About* in the top-right corner of the page).

Updating keeps your open tabs, recent repos and hidden branches. See what changed in each version in
[CHANGELOG.md](CHANGELOG.md).

<details>
<summary>Problems updating?</summary>

- **"gitlane is already at the latest version" but it isn't:** PyPI can take a few minutes to show a new
  release. Try again later, or install an exact version: `pipx install --force gitlane==X.Y.Z`.
- **Go back to an older version:** `pipx install --force gitlane==0.2.0` (any version listed on
  [PyPI](https://pypi.org/project/gitlane/#history)).
- **`pipx: command not found`:** you installed gitlane another way — use the matching row of the table above.
- **Still the old version after updating:** make sure you stopped the old gitlane. Run `which gitlane`
  (`where gitlane` on Windows) to see which copy your terminal starts.
</details>

## Usage

```bash
gitlane                      # inside a repository: open it
gitlane ~/dev/my-project     # open a specific repository
gitlane                      # anywhere else: reopen your tabs, or pick a repository
```

gitlane starts a small server on your computer and opens it in your browser. Keep the terminal
open while you use it; press **Ctrl+C** to stop. Use **+** next to the tabs to open more
repositories. Open tabs and recent repos are remembered (in `~/.config/gitlane/`, or
`%APPDATA%\gitlane` on Windows).

| Option | What it does |
|---|---|
| `--port 8000` | use a fixed port (default: a free one) |
| `--no-browser` | only print the link, don't open the browser |
| `--install-launcher` | add gitlane to your app menu (Linux) |
| `--remove-launcher` | remove it from the app menu |
| `--version` | show the version |

**Keyboard:** `↑` `↓` move through commits · `Enter` open · `R` refresh · `Ctrl+Enter` commit ·
`Esc` close menus and dialogs. Drag the dividers between panels to resize them (double-click to reset).

### Push and pull: logging in

gitlane uses **your own Git setup** and never asks for or stores passwords. If `git push` works in
your terminal without typing a password, it works in gitlane too.

- **SSH remotes** (`git@github.com:you/repo.git`) — recommended. Set up an SSH key once:
  [GitHub guide](https://docs.github.com/en/authentication/connecting-to-github-with-ssh).
- **HTTPS remotes** (`https://github.com/you/repo.git`) — need a credential helper, for example
  `gh auth setup-git` ([GitHub CLI](https://cli.github.com/)) or
  [Git Credential Manager](https://github.com/git-ecosystem/git-credential-manager).
  Or switch the remote to SSH: `git remote set-url origin git@github.com:you/repo.git`

## Security

gitlane is a local app, but a web server on your computer still needs care:

- It listens **only on `127.0.0.1`** — other computers can't reach it.
- Every start creates a **secret token** in the link; without it every request is refused.
  The token is then kept in a cookie that other websites can't use.
- It rejects requests from other websites (Origin and Host checks, which also block DNS rebinding).
- Changes are only made with `POST` requests; dangerous actions ask you first.
- Image previews are served so that an SVG from a repository can never run scripts.

Found a security problem? Please open an issue without the details first, and we'll find a private way to talk.

## Contributing

Bug reports, ideas and pull requests are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for how to
set up the project, run the tests and send a change.

## License

[MIT](LICENSE) © 2026 Gustavo Leite — free to use, change and share, including for commercial work.

gitlane includes [htmx](https://htmx.org/) (Zero-Clause BSD license).
