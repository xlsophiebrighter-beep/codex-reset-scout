# Contributing

Thanks for helping make reset warnings earlier and more reliable.

## Ground rules

- Keep the default path read-only.
- Never add automatic reset-credit redemption or account switching.
- Do not commit real session logs, tokens, account IDs, personal paths, or raw
  private-endpoint responses.
- Label unofficial mirrors and community reports honestly.
- Add a fixture and test for every new parser or classification rule.

## Development setup

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
ruff check .
pytest
```

Pull requests should explain the source being added, its reliability limits,
the privacy impact, and how failures are surfaced.
