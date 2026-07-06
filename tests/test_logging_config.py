from __future__ import annotations

from loguru import logger

from core.logging_config import setup_logging


def test_setup_logging_creates_log_file(tmp_path) -> None:
    log_file = tmp_path / "logs" / "quietgrid.log"

    setup_logging(
        {
            "logging": {
                "level": "INFO",
                "file": str(log_file),
                "rotation": "1 MB",
                "retention": "1 day",
            }
        }
    )
    logger.info("logging smoke")
    logger.complete()

    assert log_file.exists()
    assert "logging smoke" in log_file.read_text(encoding="utf-8")

