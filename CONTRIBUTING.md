# Contributing to Palmtop

Thanks for your interest in contributing! This guide will help you get set up and ship your first PR.

## Development Setup

**Prerequisites:** Python 3.11+ and [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/jbxter/palmtop.git
cd palmtop
uv sync --extra dev
```

## Running Tests

```bash
uv run python -m pytest tests/ -v
```

Some tests depend on modules not included in the public repo and will be automatically skipped.

## Linting and Formatting

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting:

```bash
uv run ruff check src/ tests/     # lint
uv run ruff format src/ tests/    # format
```

CI runs both checks on every PR. Run them locally before pushing to avoid surprises.

## Code Style

- Type hints on all public function signatures
- Docstrings on modules and classes
- `ruff` handles import sorting and formatting
- Line length: 120 characters

## How to Add a New Channel

1. Create `src/palmtop/channels/your_channel.py`
2. Implement the channel pattern (see `telegram.py` or `sms.py` as reference):
   - Accept an `AgentLoop` instance
   - Implement `send_message(user_id, text)` for outbound messages
   - Handle inbound messages and route through `AgentLoop.handle()`
3. Add channel selection to `src/palmtop/__main__.py`
4. Add config section to `config.example.toml`
5. Add any new dependencies as an optional extra in `pyproject.toml`
6. Add docs to `docs/configuration.html`

## How to Add a New Tool

1. Create `src/palmtop/tools/your_tool.py`
2. Subclass `Tool` from `palmtop.tools.base`:
   - Set `name` and `description`
   - Implement `async def run(self, query: str) -> str`
3. Register in `__main__.py` via `registry.register(YourTool())`
4. Add config section to `config.example.toml` if needed
5. Write tests in `tests/test_your_tool.py`

## Pull Request Guidelines

- Branch from `main`, PR back to `main`
- Keep PRs focused — one feature or fix per PR
- Write a clear description of what changed and why
- Include tests for new functionality
- Make sure CI passes (tests + lint)

## Commit Messages

- Use imperative mood: "Add feature" not "Added feature"
- First line under 72 characters
- Reference issues: "Closes #42"

## Reporting Bugs

Use the [bug report template](https://github.com/jbxter/palmtop/issues/new?template=bug_report.yml) on GitHub Issues. Include:
- What you expected vs. what happened
- Steps to reproduce
- Your environment (OS, Python version, Palmtop version)

## Feature Requests

Use the [feature request template](https://github.com/jbxter/palmtop/issues/new?template=feature_request.yml). We especially welcome:
- New channel integrations
- New tool integrations
- Performance improvements
- Documentation improvements
