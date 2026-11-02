"""Illustrative synthetic retail price contracts, not live market exchange rates."""

DATASET_VERSION = "orders-v3"
SUPPORTED_DATASETS = {"orders-v2", DATASET_VERSION}
# Country -> currency, minimum minor price, maximum minor price, price step.
COUNTRY_PROFILES = {
    "HU": ("HUF", 200000, 8000000, 100),
    "DE": ("EUR", 500, 20000, 1),
    "FR": ("EUR", 500, 20000, 1),
    "PL": ("PLN", 2000, 90000, 1),
    "CZ": ("CZK", 12000, 500000, 100),
    "RO": ("RON", 2500, 100000, 1),
    "GB": ("GBP", 400, 18000, 1),
    "CH": ("CHF", 450, 19000, 1),
    "SE": ("SEK", 5000, 230000, 100),
    "NO": ("NOK", 5500, 240000, 100),
    "DK": ("DKK", 3500, 150000, 1),
}
FX_QUOTES = tuple(sorted({p[0] for p in COUNTRY_PROFILES.values()} - {"EUR"}))


def valid_price(country: str, currency: str, price: int, version: str) -> bool:
    if version not in SUPPORTED_DATASETS or country not in COUNTRY_PROFILES:
        return False
    if version == "orders-v2" and country not in {"HU", "DE", "FR"}:
        return False
    expected, minimum, maximum, step = COUNTRY_PROFILES[country]
    return currency == expected and minimum <= price <= maximum and price % step == 0
