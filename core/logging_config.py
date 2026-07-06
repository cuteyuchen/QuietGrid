from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger


def setup_logging(raw_config: dict[str, Any]) -> None:
    logging_config = raw_config.get("logging", {})
    level = str(logging_config.get("level", "INFO"))
    log_file = Path(str(logging_config.get("file", "logs/trader.log")))
    rotation = str(logging_config.get("rotation", "100 MB"))
    retention = str(logging_config.get("retention", "30 days"))

    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=level)
    logger.add(log_file, level=level, rotation=rotation, retention=retention, enqueue=True)

