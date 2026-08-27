from __future__ import annotations

import re


_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_KNOWN_ISO_4217_CURRENCIES = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD
    BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY
    COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP
    GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR
    ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD
    LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN
    NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD
    RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP
    SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW
    UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF
    XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWG
    """.split()
)
_NON_SETTLEMENT_ISO_4217_CODES = frozenset({"XTS", "XXX"})


def normalize_settlement_currency(value: object) -> str:
    """Return a known settlement-capable ISO 4217 currency code."""

    normalized = str(value or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(normalized):
        raise ValueError("currency must contain exactly three Latin letters")
    if (
        normalized not in _KNOWN_ISO_4217_CURRENCIES
        or normalized in _NON_SETTLEMENT_ISO_4217_CODES
    ):
        raise ValueError("currency must be a known ISO 4217 code")
    return normalized

_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW",
        "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)
_THREE_DECIMAL_CURRENCIES = frozenset(
    {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
)
_FOUR_DECIMAL_CURRENCIES = frozenset({"CLF", "UYW"})


def settlement_currency_minor_unit_exponent(value: object) -> int:
    """Return the ISO-4217 minor-unit exponent for a settlement currency."""

    currency = normalize_settlement_currency(value)
    if currency in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if currency in _THREE_DECIMAL_CURRENCIES:
        return 3
    if currency in _FOUR_DECIMAL_CURRENCIES:
        return 4
    return 2


__all__ = [
    "normalize_settlement_currency",
    "settlement_currency_minor_unit_exponent",
]
