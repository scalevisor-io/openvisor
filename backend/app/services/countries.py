"""Where a customer may be billed from, in data (§18 billing details).

The billing country decides three separate things, and they are kept together
here because they change together: whether we accept the account at all, whether
an address there needs a state or province to resolve a tax rate, and what a tax
number entered on that account IS - a VAT number across the EU and the UK, an
EIN in the United States, a GST/HST account in Canada.

Accepting a country here is not the same as being registered to collect tax in
it. Stripe Tax collects only where a registration exists in the Stripe dashboard
(see docs/stripe-billing.md); this list is about who may hold an account.
"""
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    # Two-letter subdivision codes, when an address here needs one to resolve a
    # tax rate. Empty for every EU member: VAT is a national rate, so asking a
    # customer in Lisbon for a "province" is a field they cannot fill.
    subdivisions: dict[str, str] = field(default_factory=dict)
    # What to call a business tax number here, on the form and on the invoice.
    tax_id_label: str = "Tax number"
    tax_id_hint: str = ""


# Canada Post province and territory codes.
CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}

# US states, DC and the territories Stripe Tax resolves a rate for.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}

# EU VAT number prefixes. Almost always the country code; Greece files under EL,
# which is the kind of exception that turns a regex into a wrong rejection.
_EU_VAT_PREFIX = {
    "AT": "ATU", "BE": "BE", "BG": "BG", "HR": "HR", "CY": "CY", "CZ": "CZ",
    "DK": "DK", "EE": "EE", "FI": "FI", "FR": "FR", "DE": "DE", "GR": "EL",
    "HU": "HU", "IE": "IE", "IT": "IT", "LV": "LV", "LT": "LT", "LU": "LU",
    "MT": "MT", "NL": "NL", "PL": "PL", "PT": "PT", "RO": "RO", "SK": "SK",
    "SI": "SI", "ES": "ES", "SE": "SE",
}

_EU_NAMES = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia",
    "CY": "Cyprus", "CZ": "Czechia", "DK": "Denmark", "EE": "Estonia",
    "FI": "Finland", "FR": "France", "DE": "Germany", "GR": "Greece",
    "HU": "Hungary", "IE": "Ireland", "IT": "Italy", "LV": "Latvia",
    "LT": "Lithuania", "LU": "Luxembourg", "MT": "Malta", "NL": "Netherlands",
    "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SK": "Slovakia",
    "SI": "Slovenia", "ES": "Spain", "SE": "Sweden",
}

SUPPORTED: dict[str, Country] = {
    # One hint for all 27 rather than one naming each prefix: the prefix is
    # accepted with or without it (see tax_id_for), so 27 near-identical
    # sentences would describe a difference the field does not care about.
    **{code: Country(code, name, {}, "VAT number",
                     "Your EU VAT number, with or without the country prefix.")
       for code, name in _EU_NAMES.items()},
    "GB": Country("GB", "United Kingdom", {}, "VAT number",
                  "For example GB123456789."),
    "CH": Country("CH", "Switzerland", {}, "VAT number",
                  "For example CHE-123.456.789 MWST."),
    "NO": Country("NO", "Norway", {}, "VAT number",
                  "Your organisation number followed by MVA, for example "
                  "123456789MVA."),
    "US": Country("US", "United States", US_STATES, "EIN",
                  "Your federal employer identification number, for example "
                  "12-3456789."),
    "CA": Country("CA", "Canada", CA_PROVINCES, "Business number (GST-HST or QST)",
                  "Your GST-HST account, for example 123456789RT0001, or a Quebec "
                  "QST number, for example 1234567890TQ0001."),
}

# Kept apart from SUPPORTED so a caller can ask "is this the EU" without
# re-deriving it from the VAT table.
EU_MEMBERS = frozenset(_EU_NAMES)


def is_supported(code: str | None) -> bool:
    return bool(code) and code.upper() in SUPPORTED


def name_of(code: str | None) -> str | None:
    country = SUPPORTED.get((code or "").upper())
    return country.name if country else None


def subdivisions_for(code: str | None) -> dict[str, str]:
    country = SUPPORTED.get((code or "").upper())
    return country.subdivisions if country else {}


def needs_subdivision(code: str | None) -> bool:
    return bool(subdivisions_for(code))


def tax_id_for(country: str | None, value: str | None) -> tuple[str, str] | None:
    """The Stripe tax id type and canonical value for a customer's tax number.

    Returns None for anything unrecognisable rather than guessing. Stripe
    validates the format and rejects the call, and that call is in the middle of
    a payment, so a wrong guess here does not produce a wrong invoice - it takes
    the whole top-up down.

    The value is normalised because the same number is written several ways: a
    UK VAT number is often given without its GB, an EIN with or without its
    hyphen, and Stripe accepts exactly one of those spellings.
    """
    code = (country or "").upper()
    raw = re.sub(r"[\s.\-]", "", (value or "")).upper()
    if not raw:
        return None

    prefix = _EU_VAT_PREFIX.get(code)
    if prefix:
        body = raw[len(prefix):] if raw.startswith(prefix) else raw
        if re.fullmatch(r"[A-Z0-9]{2,12}", body):
            return "eu_vat", f"{prefix}{body}"
        return None

    if code == "GB":
        digits = raw[2:] if raw.startswith("GB") else raw
        if re.fullmatch(r"\d{9}|\d{12}", digits):
            return "gb_vat", f"GB{digits}"
        return None

    if code == "CH":
        # CHE-123.456.789 MWST, whose punctuation and suffix are already gone by
        # here. Stripe wants the printed form back, suffix included.
        body = raw[3:] if raw.startswith("CHE") else raw
        body = body[:-4] if body.endswith("MWST") else body
        if re.fullmatch(r"\d{9}", body):
            return "ch_vat", f"CHE-{body[:3]}.{body[3:6]}.{body[6:]} MWST"
        return None

    if code == "NO":
        body = raw[:-3] if raw.endswith("MVA") else raw
        if re.fullmatch(r"\d{9}", body):
            return "no_vat", f"{body}MVA"
        return None

    if code == "US":
        if re.fullmatch(r"\d{9}", raw):
            return "us_ein", f"{raw[:2]}-{raw[2:]}"
        return None

    if code == "CA":
        if re.fullmatch(r"\d{9}RT\d{4}", raw):
            return "ca_gst_hst", raw
        # Quebec runs its own sales tax through Revenu Quebec, so a QST number is
        # a different registration from the federal one and is written
        # differently: ten digits, TQ, four more.
        if re.fullmatch(r"\d{10}TQ\d{4}", raw):
            return "ca_qst", raw
        # A bare business number is not a GST/HST account: it identifies the
        # company but says nothing about registration, and Stripe treats only
        # the RT form as tax-relevant.
        if re.fullmatch(r"\d{9}", raw):
            return "ca_bn", raw
        return None

    return None


def missing_address_fields(org) -> list[str]:
    """Which parts of a billing address are absent, in Stripe's field names.

    One definition, because two places need the same answer and they must not
    drift: `stripe_svc._address_of` decides whether to SEND the address, and the
    account API tells the customer whether we hold one. When those two disagree,
    an account shows a complete profile on screen while Stripe receives nothing,
    which is how an invoice ends up addressed to nobody and a sale ends up
    untaxed. Empty means the address is usable.

    A subdivision counts as missing only where the country bills by one. Asking a
    German customer for a province would report a profile broken that is not.
    """
    missing = []
    if not (org.address_line1 or "").strip():
        missing.append("line1")
    if not (org.city or "").strip():
        missing.append("city")
    if not (org.postal_code or "").strip():
        missing.append("postal_code")
    if not (org.country or "").strip():
        missing.append("country")
    elif needs_subdivision(org.country) and not (org.province or "").strip():
        missing.append("state")
    return missing


def out() -> list[dict]:
    """The list the SPA renders, so the form and the validator cannot disagree.

    Sorted by name rather than by code: a customer scans this for "Germany", not
    for "DE".
    """
    return [
        {
            "code": c.code,
            "name": c.name,
            "subdivisions": [{"code": k, "name": v}
                             for k, v in sorted(c.subdivisions.items(),
                                                key=lambda kv: kv[1])],
            "tax_id_label": c.tax_id_label,
            "tax_id_hint": c.tax_id_hint,
        }
        for c in sorted(SUPPORTED.values(), key=lambda c: c.name)
    ]
