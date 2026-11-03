# gitlane

A free, open-source Git client that runs in your browser. **Work in progress** — see [SPEC.md](SPEC.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest          # run the tests
.venv/bin/gitlane ~/dev/some-repo   # open a repo in the browser
```
