# Concurrency

Concurrency is included in the normal diagnostic runner; no sibling checkout or
shared virtual environment is required. See [the diagnostic guide](docs/DIAGNOSTIC_RUNNER.md)
for installation, scheduling, cache behavior, limits and examples.

Both worker settings default to 1. Start with 2 for a controlled comparison and
compare coverage, errors and timing, not wall-clock duration alone.
