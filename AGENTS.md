# AGENTS instructions

This is a Python CLI tool. It pulls GitHub issue, PR, and comment data into a local DuckDB database, and then provides commands for inspecting and querying it.

## Development environment

This project uses uv for Python environment management and Just as a task runner.

- Use `uv run` for anything that needs to be run in the project's Python environment.
- Common actions are defined as recipes in the [`justfile`](/justfile). Run `just` by itself to see documentation. Several commands are variadic and pass through arguments. This can be useful for running the recipe on specific files.

## Code quality

- Linting: `just lint` (variadic)
- Auto-formatting: `just format` (variadic)

## Design decisions

Schema and data-modelling choices that are non-obvious are recorded in
[`docs/decisions.md`](/docs/decisions.md), each with the alternative that was rejected. Read it
before changing the shape of the derived views or the columns they expose — several choices look
arbitrary without the reasoning, and undoing them reintroduces known problems.

## Development and testing

- Use red/green test-driven development
- Testing uses pytest and goes in [`tests/`](/tests/)
- Run the test suite with `just test` (variadic)
