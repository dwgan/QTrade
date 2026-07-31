from __future__ import annotations

import hashlib
import json

FUTURES_PRODUCT_SECTORS = {
    "A": "agriculture",
    "AD": "base_metals",
    "AG": "precious_metals",
    "AL": "base_metals",
    "AO": "base_metals",
    "AP": "agriculture",
    "AU": "precious_metals",
    "B": "agriculture",
    "BB": "agriculture",
    "BC": "base_metals",
    "BR": "energy_chemicals",
    "BU": "energy_chemicals",
    "C": "agriculture",
    "CF": "agriculture",
    "CJ": "agriculture",
    "CS": "agriculture",
    "CU": "base_metals",
    "CY": "agriculture",
    "EB": "energy_chemicals",
    "EC": "shipping",
    "EG": "energy_chemicals",
    "FB": "agriculture",
    "FG": "energy_chemicals",
    "FU": "energy_chemicals",
    "HC": "ferrous",
    "I": "ferrous",
    "IC": "equity_index",
    "IF": "equity_index",
    "IH": "equity_index",
    "IM": "equity_index",
    "J": "ferrous",
    "JD": "agriculture",
    "JM": "ferrous",
    "JR": "agriculture",
    "L": "energy_chemicals",
    "LC": "energy_chemicals",
    "LH": "livestock",
    "LR": "agriculture",
    "LU": "energy_chemicals",
    "M": "agriculture",
    "MA": "energy_chemicals",
    "NI": "base_metals",
    "NR": "energy_chemicals",
    "OI": "agriculture",
    "P": "agriculture",
    "PB": "base_metals",
    "PF": "energy_chemicals",
    "PG": "energy_chemicals",
    "PK": "agriculture",
    "PM": "agriculture",
    "PP": "energy_chemicals",
    "PR": "energy_chemicals",
    "PS": "energy_chemicals",
    "PX": "energy_chemicals",
    "RB": "ferrous",
    "RI": "agriculture",
    "RM": "agriculture",
    "RR": "agriculture",
    "RS": "agriculture",
    "RU": "energy_chemicals",
    "SA": "energy_chemicals",
    "SC": "energy_chemicals",
    "SF": "ferrous",
    "SH": "energy_chemicals",
    "SI": "base_metals",
    "SM": "ferrous",
    "SN": "base_metals",
    "SP": "energy_chemicals",
    "SR": "agriculture",
    "SS": "base_metals",
    "T": "rates",
    "TA": "energy_chemicals",
    "TF": "rates",
    "TL": "rates",
    "TS": "rates",
    "UR": "energy_chemicals",
    "V": "energy_chemicals",
    "WH": "agriculture",
    "WR": "ferrous",
    "Y": "agriculture",
    "ZC": "ferrous",
    "ZN": "base_metals",
}


def futures_sector_registry_id() -> str:
    payload = json.dumps(FUTURES_PRODUCT_SECTORS, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def futures_sector(product_code: str) -> str:
    normalized = product_code.strip().upper()
    try:
        return FUTURES_PRODUCT_SECTORS[normalized]
    except KeyError as error:
        raise ValueError(f"No frozen futures sector mapping for product: {normalized}") from error
