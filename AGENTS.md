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

- Non-obvious choices are recorded in [`docs/decisions.md`](/docs/decisions.md), each with the alternatives that were rejected.
- Read it before changing behavior an entry covers. Several choices look arbitrary without the reasoning, and undoing them reintroduces problems that are already known.
- Add an entry when you make a decision a future reader might reasonably undo. That file's header states what qualifies and the format to use.

## Defensive code and review findings

The database is a disposable per-user cache that `ghtriage pull --full` fully rebuilds in
minutes. Calibrate defenses to that.

- Classify a failure before defending against it: (1) a **silent wrong answer** gets real logic
  and a test per hazard; (2) a **loud error** is an acceptable outcome, not a bug to code
  around; (3) a state **one `pull --full` fixes** needs at most a warning that says so; (4) a
  state an **upstream layer guarantees impossible** (e.g. dlt's merge key makes `id` unique and
  non-null) gets no runtime defense — pin the guarantee with a test on the declaration.
- When something might be absent, prefer guaranteeing it present at one boundary over handling
  absence at each use site. Code paths that don't exist have no edge cases.
- Triage review findings against the classes above before fixing anything; only class 1
  mandates a code change. A finding must name a concrete trigger in this system's actual
  lifecycle, and its harm given the warnings that already exist. A fix that adds a new code
  path must say why a boundary invariant can't replace it. When a review round's findings
  concern only code added by the previous round's fixes, stop patching and simplify instead.

## Development and testing

- Use red/green test-driven development
- Testing uses pytest and goes in [`tests/`](/tests/)
- Run the test suite with `just test` (variadic)
