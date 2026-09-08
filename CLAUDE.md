# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`news-search-workflow` is a Python 3.13 project managed with `uv`. It is currently in early scaffolding — `main.py` contains only a placeholder `main()` function.

## Commands

```bash
# Run the project
uv run python main.py

# Add a dependency
uv add <package>

# Install dependencies
uv sync
```

## Structure

- `main.py` — entry point; `main()` is the top-level function
- `pyproject.toml` — project metadata and dependencies (uv-managed)
- `.python-version` — pins Python 3.13
