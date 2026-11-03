# gitlane

A free, open-source Git client that runs in your browser. **Work in progress** — see [SPEC.md](SPEC.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,e2e]"   # e2e = browser tests with your Chrome
.venv/bin/pytest          # run the tests
.venv/bin/gitlane ~/dev/some-repo   # open a repo in the browser
.venv/bin/gitlane                   # inside a repo: open it; elsewhere: pick one in the browser
```

Each repo opens in its own tab; use **+** to open more (recent repos, a folder
browser, or a pasted path). Open tabs are remembered in `~/.config/gitlane/`.
