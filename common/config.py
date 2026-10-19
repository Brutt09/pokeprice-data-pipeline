"""Settings read from the environment (.env in the project root)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://pokeprice:pokeprice@localhost:5433/pokeprice"
)
TCGDEX_BASE_URL = os.getenv("TCGDEX_BASE_URL", "https://api.tcgdex.net/v2/en")
POKEMONTCG_BASE_URL = os.getenv("POKEMONTCG_BASE_URL", "https://api.pokemontcg.io/v2")
POKEMONTCG_API_KEY = os.getenv("POKEMONTCG_API_KEY", "")
FX_BASE_URL = os.getenv("FX_BASE_URL", "https://api.frankfurter.dev/v1")
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
EBAY_MARKETPLACE = os.getenv("EBAY_MARKETPLACE", "EBAY_US")

# MVP scope: one set. TCGdex id -> Pokémon TCG API id for the same set.
SETS = [s.strip() for s in os.getenv("SETS", "sv03.5").split(",") if s.strip()]
SET_ID_MAP = {"sv03.5": "sv3pt5"}

# All prices are normalised to this currency before any comparison.
BASE_CURRENCY = "EUR"

RAW_DIR = ROOT / "data" / "raw"
