"""Regression tests for the generated documentation checks."""

import time

from docs import build_docs


def test_readme_test_count_scan_is_linear_on_a_long_digit_run(tmp_path, monkeypatch):
    """Untrusted README text must not turn the docs check into a ReDoS sink."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("9" * 50_000 + " testX\n1 tests\n", encoding="utf-8")
    monkeypatch.setattr(build_docs, "ROOT", str(tmp_path))

    started = time.perf_counter()
    error = build_docs.check_test_count()
    elapsed = time.perf_counter() - started

    assert error is None
    assert elapsed < 1.0, f"README test-count scan took {elapsed:.3f}s"
