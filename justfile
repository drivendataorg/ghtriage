python := shell("cat .python-version")

# Print this help documentation
help:
    just --list

# Sync requirements
sync:
    uv sync

# Run linting (variadic)
lint *args:
    uv run -- ruff format --check {{args}}
    uv run -- ruff check {{args}}

# Run formatting (variadic)
format *args:
    uv run -- ruff format {{args}}
    uv run -- ruff check --fix --extend-fixable=F {{args}}

# Run type checking (variadic)
[arg("python", long)]
typecheck python=python *args:
    uv run --python={{python}} --isolated --no-dev --group typecheck -- \
        ty check --python-version={{python}} {{args}}

# Run test suite (variadic)
[arg("python", long)]
test python=python *args:
    uv run --python={{python}} --isolated --no-editable --no-dev --group tests --reinstall-package=ghtriage -- \
        python -I -m pytest {{args}}
