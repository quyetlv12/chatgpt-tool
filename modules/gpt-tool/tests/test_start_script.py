from pathlib import Path


def test_start_script_searches_versioned_python_when_python3_is_too_old() -> None:
    script = Path("start.sh").read_text(encoding="utf-8")

    assert "python3.11" in script
    assert "PYTHON_BIN" in script
