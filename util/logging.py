"""Console + file logging for sync runs and the API server. Without this,
every `logging.getLogger("neuron.*")` call across the codebase (semantic
extraction, rejected facts, LLM failures) goes to Python's default no-op
handler and is invisible — the user explicitly asked to see Jira sync output.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from util.paths import DATA_DIR

LOG_PATH = DATA_DIR / "logs" / "neuron.log"


def configure_logging(level: int = logging.INFO) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("neuron")
    if root.handlers:
        return  # already configured (e.g. re-imported in the same process)
    root.setLevel(level)

    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
