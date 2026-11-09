"""Versioned mappings from physical generation assets to securities."""

from src.market_mapping.exposure import EXPOSURE_FIELDS, get_exposure, validate_exposures

__all__ = ["EXPOSURE_FIELDS", "get_exposure", "validate_exposures"]
