# Contributing

## Development setup

1. Create a Python 3.11+ virtual environment.
2. Install runtime dependencies with `python -m pip install -r requirements.txt`.
3. Copy `config.example.yaml` to `config.yaml` only for local work. Never commit
   the copy or any file under `data/`.

## Tests

Run the Python suite with:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

The dashboard safety test also requires Node.js:

```bash
npm ci --prefix tests
node tests/verify_dashboard_ui.js
node tests/verify_subscription_ui.js
node tests/verify_codex_binding_ui.js
```

Do not use real provider keys or send paid requests in tests. Prefer the local
mock upstream in `tests/mock_upstream.py`.

## Pull requests

- Keep changes focused and explain behavior changes and test coverage.
- Add regression tests for routing, authentication, persistence, or dashboard
  changes.
- Do not commit credentials, runtime data, logs, production screenshots,
  generated packages, editor state, or unrelated vendored repositories.
- Documentation screenshots must use isolated synthetic fixtures. Reproduce them
  with `python tools/capture_docs.py` (requires Playwright and a browser); never
  capture real accounts, provider responses, IPs, keys, or billing data.
- Use placeholders such as `replace-with-upstream-key` in examples.
