---
description: Code style and conventions for Python projects.
---

This file contains coding standards and practices for use within this project.

- *Use modern Python.*  Use the latest stable version of Python3 by default.
- *Follow PEP-8.*  Use [PEP 8](https://peps.python.org/pep-0008/) formatting by default.
- *Use type hinting.*  Use [PEP 484](https://peps.python.org/pep-0484/) type hinting.
- *Use `uv`.*  Use `uv` for all dependency management purposes including Python interpreter, libraries, and execution of scripts or parts of the progam.
- *Use project-local dependencies.* 
- *Use dependency cooldowns in `uv`.*  Explicitly set the `exclude-newer` option in `tool.uv` to `P3D`.
- *Use `pytest` for tests.*  Doctests are acceptable only where necessary for good documentation.
- *Use `hypothesis` for property-based testing.*  Particularly where the specifications or code is of a declarative nature, use property-based testing to verify correctness.
- *Use `ruff` for formatting and linting.*  Format with `uv run --extra dev ruff format` and lint with `uv run --extra dev ruff check` (`--fix` for autofixes). The tools live in the `dev` extra. Keep the rule set and line length in `pyproject.toml`.
- *Use `mypy` for type checking.*  Run it with `uv run --extra dev mypy` and keep its settings in `pyproject.toml`.
- *New code passes without new ignores.*  Fix the code instead. Use a narrow, commented ignore (`# noqa: <rule>`, `# type: ignore[<code>]`, or a per-file ignore) only when unavoidable, and say why.
