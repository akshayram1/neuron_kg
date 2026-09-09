"""Project-root paths. Import this before reading ledgers, the bridge, or .env.

Every path here is anchored to this file, not the process cwd, so
`python -m graph.ingest` from another directory cannot spawn a second ledger.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

_raw = os.getenv("DATA_DIR")
DATA_DIR = Path(_raw) if _raw else ROOT
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT / DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

LEDGER_PDF = DATA_DIR / ".ingested.json"
LEDGER_ARGUS = DATA_DIR / ".ingested_argus.json"
LEDGER_PERSONAL = DATA_DIR / ".ingested_personal.json"
BRIDGE_PATH = DATA_DIR / ".bridge.json"
DOCS_DIR = Path(os.getenv("DOCS_DIR") or ROOT / "docs")
ARGUS_DIR = Path(os.getenv("ARGUS_DIR") or ROOT / "argus")
ARGUS_CATALOG = ARGUS_DIR / "catalog.json"
