# Round 1 — Coder (Claude)
_2026-06-19 00:18:05_

All 7 tests pass.

## Summary

- **`scripts/line_counter.py`** — added `count_lines(file_path, encoding="utf-8")`, a small function that returns the line count of a text file by streaming it line-by-line (memory-efficient, no full read). Accepts `str` or `Path`, raises `FileNotFoundError` for missing files, and doubles as a CLI (`python scripts/line_counter.py <file>`).
- **`scripts/test_line_counter.py`** — 7 pytest cases covering the edge cases that matter: empty file (0), single line without trailing newline (1), multiple lines, trailing line without newline, blank-line counting, missing-file error, and `str` path input.
- Verified: installed pytest and ran the suite — **7 passed**. Chose Python + pytest to match the project's existing Python tooling stack.
