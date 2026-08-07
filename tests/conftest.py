"""Put the repository root on sys.path so the tests can import both
`scripts.build_corpus` and `plugins.mtg.server.mtg_server` regardless of how
pytest is invoked (`pytest`, `python -m pytest`, or from a subdirectory)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
