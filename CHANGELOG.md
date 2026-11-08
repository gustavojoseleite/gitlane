# Changelog

## 0.4.0 — 2026-11-08

- **No more stuck page:** a slow pull, push or other action no longer freezes gitlane; the rest of
  the page keeps working while Git runs. A request with no answer after 30 seconds gives up with a
  message (the buttons come back), and a stopped server is reported as such. A second action in the
  same repository while one is still running is refused with a clear message
- **Stash: Include untracked files** is now a checkbox (ticked by default), so you can stash your
  edits and keep new files where they are
- **Stash inside a submodule:** a changed submodule in the Changes list has a *Stash changes inside*
  button; the stash goes to the submodule's own stash list
- **Toolbar actions stand out:** Fetch, Pull, Push, New branch and Stash are larger and tinted;
  Pull turns solid when there are commits to bring in (Push when there are commits to upload)
- Stashing no longer says “Stashed your changes” when the only changes are inside submodules (Git
  never stashes those from the project), and says which submodules still have changes

## 0.3.0 — 2026-11-04

- **New layout:** submodules, branches and tags on the left (submodules first, highlighted when one
  needs attention); on the right, a **Changes** tab (files, stashes, commit box) and a **Details**
  tab (the commit or stash you clicked)
- **Diffs open in place of the graph**, with *Back to graph* (or Esc); the file you are viewing stays
  highlighted
- When a merge, cherry-pick or revert stops with conflicts, the Changes tab opens by itself
- README: how to update gitlane

## 0.2.0 — 2026-11-04

- **Tags:** right-click a commit → *Create tag here…* (add a message for an annotated tag);
  a *Tags* section in the sidebar; push a tag, delete it, or delete it on the remote
- **Submodules:** a *Submodules* section (up to date / on another commit / not set up) with
  *Set up* and *Back to the recorded commit*; a changed submodule shows what moved (commits added
  or removed) instead of a text diff; open any submodule in its own tab. gitlane never throws away
  files inside a submodule
- **Choose folder…** in the repo picker opens your system's own folder window
  (zenity or kdialog on Linux, built in on macOS and Windows); it replaces the path field, which
  only appears when no folder window is available. The folder list stays
- **Hide branches in the graph:** the eye button in the sidebar or *Hide … in the graph* in the
  branch menu; a local branch takes its remote copy with it; *Hide all* for remote branches.
  Remembered per repository; the current branch is always shown

## 0.1.1 — 2026-11-04

- **Windows is supported** (tested in CI on Windows, macOS and Linux); settings live in `%APPDATA%\gitlane`
- Works on Python 3.10 and 3.11 again (0.1.0 failed to start there)
- Ctrl+C stops gitlane quietly instead of printing an error
- Branch labels show where a branch is: on your computer, on the remote, or both;
  a branch and its remote copy on the same commit share one label
- Stash pop with conflicts says the stash was kept, on newer Git versions too
- Clearer hint when the remote server can't be reached (newer curl wording)
- Commit dates no longer get cut off with wider fonts

## 0.1.0 — 2026-11-03

First version.

- Status, stage/unstage/discard whole files, commit with summary and description
- Diff viewer for working files and commits, with image previews
- Fetch, pull, push, force push with lease, and readable errors with hints
- Interactive commit graph with lanes, labels, stashes and paging for large repositories
- Branches: create, switch, merge, delete (local and remote); cherry-pick, revert, reset
- Simple merge-conflict flow: abort, or mark resolved and commit
- Stash: save (with untracked files), apply, pop, drop, view
- Several repositories in tabs, recent repos, folder browser
- Light and dark theme, resizable panels, minimizable sidebar sections
- Local-only server with a secret token, Origin/Host checks and confirmations for dangerous actions
- `--install-launcher` for the Linux app menu
