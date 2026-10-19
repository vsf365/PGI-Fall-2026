"""
Central configuration for the Nuclear Outage Prediction Pipeline.

Loads API keys from configs/.env and provides shared constants, paths,
and settings used across all pipeline phases.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
CURATED_DIR = DATA_DIR / "curated"
MANIFEST_DIR = DATA_DIR / "manifests"

# Source-specific raw directories
RAW_NRC = RAW_DIR / "nrc"
RAW_EIA = RAW_DIR / "eia"
RAW_NOAA = RAW_DIR / "noaa"
RAW_USGS = RAW_DIR / "usgs"
RAW_ISO = RAW_DIR / "iso"
RAW_SEC = RAW_DIR / "sec"
RAW_MARKET = RAW_DIR / "market"

# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
_env_path = PROJECT_ROOT / "configs" / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    print(f"⚠️  No .env file found at {_env_path}")
    print("   Copy configs/.env.template → configs/.env and add your API keys.")

# ---------------------------------------------------------------------------
# API Keys & Auth
# ---------------------------------------------------------------------------
EIA_API_KEY = os.getenv("EIA_API_KEY", "")
NOAA_CDO_TOKEN = os.getenv("NOAA_CDO_TOKEN", "")
PJM_API_KEY = os.getenv("PJM_API_KEY", "")
SEC_CONTACT_EMAIL = os.getenv("SEC_CONTACT_EMAIL", "research@example.com")
NWS_USER_AGENT = os.getenv("NWS_USER_AGENT", "(nuclear-research, research@example.com)")

# ---------------------------------------------------------------------------
# HTTP Headers (pre-built for each source)
# ---------------------------------------------------------------------------
EIA_PARAMS_BASE = {"api_key": EIA_API_KEY}

NOAA_CDO_HEADERS = {"token": NOAA_CDO_TOKEN}

NWS_HEADERS = {
    "User-Agent": NWS_USER_AGENT,
    "Accept": "application/geo+json",
}

USGS_HEADERS = {"Accept-Encoding": "gzip"}

PJM_HEADERS = {
    "Ocp-Apim-Subscription-Key": PJM_API_KEY,
    "Accept": "application/json",
}

SEC_HEADERS = {
    "User-Agent": f"NuclearOutageResearch {SEC_CONTACT_EMAIL}",
    "Accept-Encoding": "gzip, deflate",
}

# ---------------------------------------------------------------------------
# Scope defaults (from project plan §2)
# ---------------------------------------------------------------------------
PRIMARY_ISO = "PJM"
SECONDARY_ISO = "MISO"
DEFAULT_FREQUENCY = "daily"

# Derating thresholds (% of capacity)
DERATING_THRESHOLDS = {
    "derating_10pct": 0.90,   # output <= 90% of capacity
    "derating_25pct": 0.75,
    "derating_50pct": 0.50,
    "near_outage": 0.20,
}

# Forecast horizons (hours)
FORECAST_HORIZONS = [1, 3, 6, 12, 24, 48, 72]

# Decision clocks (local time)
DECISION_CLOCKS = {
    "d_minus_1_morning": "06:00",
    "d_minus_1_post_wx": "10:00",
    "d_minus_1_close": "16:00",
}

# ---------------------------------------------------------------------------
# Rate limit helpers
# ---------------------------------------------------------------------------
RATE_LIMITS = {
    "eia": {"req_per_sec": 5, "sleep": 0.22},
    "noaa_cdo": {"req_per_sec": 5, "sleep": 0.22},
    "nws": {"req_per_sec": 3, "sleep": 0.35},
    "usgs": {"req_per_sec": 8, "sleep": 0.13},
    "pjm": {"req_per_min": 6, "sleep": 11.0},  # non-member
    "sec": {"req_per_sec": 8, "sleep": 0.13},
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def check_keys():
    """Print status of all API keys."""
    keys = {
        "EIA_API_KEY": EIA_API_KEY,
        "NOAA_CDO_TOKEN": NOAA_CDO_TOKEN,
        "PJM_API_KEY": PJM_API_KEY,
        "SEC_CONTACT_EMAIL": SEC_CONTACT_EMAIL,
    }
    all_ok = True
    for name, val in keys.items():
        if not val or val.startswith("PASTE_"):
            print(f"  ❌ {name}: not set")
            all_ok = False
        else:
            print(f"  ✅ {name}: configured ({val[:8]}...)")
    return all_ok
