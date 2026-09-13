import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kalecki.parse import parse_workbook  # noqa: E402

SAMPLE = ROOT / "tests" / "data" / "sample.xlsx"


@pytest.fixture(scope="session")
def sample_bytes() -> bytes:
    return SAMPLE.read_bytes()


@pytest.fixture(scope="session")
def parsed(sample_bytes):
    return parse_workbook(sample_bytes)
