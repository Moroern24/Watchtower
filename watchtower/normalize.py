"""Deterministic normalization of raw extracted strings into spec-conformant values.

Rule: when unsure return None. A wrong value is worse than a missing one.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as dparser

# ---------------------------------------------------------------- text

def clean_text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = " ".join(str(x) for x in v if x is not None)
    s = unicodedata.normalize("NFKC", str(v))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ---------------------------------------------------------------- dates

_MONTHS = {
    # de
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
    # fr
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    "decembre": 12,
    # es
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
    # nl
    "januari": 1, "februari": 2, "maart": 3, "mei": 5, "augustus": 8,
    # it
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
    "settembre": 9, "ottobre": 10, "dicembre": 12,
    # pl (genitive forms used in dates)
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
    "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

_TZ_ABBR = {"cet": "+01:00", "cest": "+02:00", "mez": "+01:00", "mesz": "+02:00",
            "utc": "+00:00", "gmt": "+00:00", "wet": "+00:00", "west": "+01:00",
            "eet": "+02:00", "eest": "+03:00"}


def get_tz(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _prep_date_string(s: str) -> str:
    s = s.strip()
    low = s.lower()
    for word, num in sorted(_MONTHS.items(), key=lambda kv: -len(kv[0])):
        if word in low:
            low = re.sub(rf"\b{re.escape(word)}\b\.?", f" {num} ", low)
    s = low
    # "10:00 Uhr", "à 10h00", "a las 10:00 horas"
    s = re.sub(r"(\d{1,2})\s*h\s*(\d{2})", r"\1:\2", s)
    s = re.sub(r"\b(uhr|horas|hrs|h|godz\.?|godzina|om|à|a las|um|le|el|du|de)\b", " ", s)
    for abbr, off in _TZ_ABBR.items():
        s = re.sub(rf"\b{abbr}\b", off, s)
    s = re.sub(r"\s+", " ", s).strip(" ,;-")
    # "20 11 2026" from month-name replacement -> 20.11.2026
    s = re.sub(r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\b", r"\1.\2.\3", s)
    return s


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")


def parse_datetime(raw, formats: list[str] | None = None, tz: str | None = None,
                   dayfirst: bool = True) -> tuple[datetime | None, bool]:
    """Return (datetime, has_time). Naive results are localized with tz (if given)."""
    if raw is None:
        return None, False
    if isinstance(raw, (int, float)):  # epoch seconds / millis
        ts = float(raw)
        if ts > 1e11:
            ts /= 1000
        if 9e8 < ts < 5e9:
            return datetime.fromtimestamp(ts, ZoneInfo("UTC")).astimezone(
                get_tz(tz) or ZoneInfo("UTC")), True
        return None, False
    s = clean_text(raw)
    if not s:
        return None, False
    zone = get_tz(tz)
    dt: datetime | None = None
    has_time = False
    if _ISO_RE.match(s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            has_time = "T" in s or " " in s
        except ValueError:
            dt = None
    if dt is None:
        for fmt in formats or []:
            try:
                dt = datetime.strptime(s, fmt)
                has_time = any(c in fmt for c in ("%H", "%I", "%M"))
                break
            except ValueError:
                continue
    if dt is None:
        prepped = _prep_date_string(s)
        m = re.search(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}(?:[ ,T]+\d{1,2}:\d{2}(?::\d{2})?)?"
                      r"(?:\s*[+-]\d{2}:?\d{2})?", prepped)
        candidate = m.group(0) if m else prepped
        if not re.search(r"\d", candidate):
            return None, False
        try:
            iso_like = bool(re.match(r"^\d{4}", candidate))
            dt = dparser.parse(candidate, dayfirst=dayfirst and not iso_like,
                               default=datetime(2000, 1, 1, 0, 0))
            has_time = bool(re.search(r"\d{1,2}:\d{2}", candidate))
        except (ValueError, OverflowError, dparser.ParserError):
            return None, False
    if not (1990 <= dt.year <= 2100):
        return None, False
    if dt.tzinfo is None and zone is not None:
        dt = dt.replace(tzinfo=zone)
    return dt, has_time


def to_date(raw, formats=None, tz=None, dayfirst=True) -> str | None:
    dt, _ = parse_datetime(raw, formats, tz, dayfirst)
    if dt is None:
        return None
    if dt.tzinfo is not None and tz:
        z = get_tz(tz)
        if z:
            dt = dt.astimezone(z)
    return dt.date().isoformat()


def to_datetime(raw, formats=None, tz=None, dayfirst=True) -> str | None:
    """ISO 8601 with offset. Date-only deadlines become 23:59:59 local time.
    Without any timezone information we return None (offset would be a guess)."""
    dt, has_time = parse_datetime(raw, formats, tz, dayfirst)
    if dt is None:
        return None
    if not has_time:
        dt = datetime.combine(dt.date(), time(23, 59, 59), tzinfo=dt.tzinfo)
    if dt.tzinfo is None:
        return None
    zone = get_tz(tz)
    if zone is not None:  # same instant, shown in the portal's local offset
        dt = dt.astimezone(zone)
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------- codes

_CPV_RE = re.compile(r"(?<!\d)(\d{8})(?:-\d)?(?!\d)")


def cpv_codes(raw) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(x) for x in raw if x is not None)
    out: list[str] = []
    for code in _CPV_RE.findall(str(raw)):
        if code not in out and not code.startswith("00"):
            out.append(code)
    return out or None


_PROCEDURE = [
    ("restricted", ["restricted", "nicht offen", "nichtoffen", "beschränkt", "beschraenkt",
                    "restreint", "restringido", "niet-openbaar", "niet openbaar", "ograniczon",
                    "ristrett"]),
    ("negotiated", ["negotiat", "verhandlung", "négoci", "negoci", "onderhandel",
                    "negocjac", "negoziat", "dialog", "freihändig", "freihaendig",
                    "direktvergabe", "gré à gré"]),
    ("open", ["open procedure", "offenes verfahren", "öffentliche ausschreibung",
              "oeffentliche ausschreibung", "offen", "ouvert", "abierto", "openbaar",
              "przetarg nieograniczony", "nieograniczon", "aperta", "open"]),
]

_NATURE = [
    ("works", ["works", "bauleistung", "bau", "travaux", "obras", "werken", "roboty budowlane",
               "lavori"]),
    ("supplies", ["supplies", "supply", "lieferleistung", "lieferung", "liefer", "fournitures",
                  "suministro", "leveringen", "dostawy", "dostaw", "forniture"]),
    ("services", ["services", "service", "dienstleistung", "dienst", "servicios", "diensten",
                  "usługi", "uslugi", "servizi"]),
]


def _keyword_enum(raw, table, other: str | None) -> str | None:
    s = clean_text(raw)
    if not s:
        return None
    low = s.lower()
    for value, words in table:
        if low == value:
            return value
    for value, words in table:
        if any(w in low for w in words):
            return value
    return other


def procedure_type(raw) -> str | None:
    # Unknown wording but something was shown -> "other" only for clear procedure words
    s = clean_text(raw)
    if not s:
        return None
    if re.search(r"adapt|mapa|unterschwell|vereinfacht|simplificad|menor|"
                 r"onderhands|zapytanie ofertowe", s.lower()):
        return "other"
    val = _keyword_enum(s, _PROCEDURE, None)
    if val:
        return val
    if re.search(r"verfahren|procedure|procédure|procedimiento|procedure|tryb|procedura|"
                 r"wettbewerb|competiti|concours|concurso", s.lower()):
        return "other"
    return None


def contract_nature(raw) -> str | None:
    return _keyword_enum(raw, _NATURE, None)


_COUNTRIES = {
    "germany": "DE", "deutschland": "DE", "allemagne": "DE", "alemania": "DE",
    "france": "FR", "frankreich": "FR", "francia": "FR", "frankrijk": "FR",
    "spain": "ES", "españa": "ES", "espana": "ES", "spanien": "ES", "espagne": "ES",
    "netherlands": "NL", "nederland": "NL", "niederlande": "NL", "pays-bas": "NL",
    "poland": "PL", "polska": "PL", "polen": "PL", "pologne": "PL",
    "austria": "AT", "österreich": "AT", "oesterreich": "AT", "autriche": "AT",
    "italy": "IT", "italia": "IT", "italien": "IT", "italie": "IT",
    "belgium": "BE", "belgië": "BE", "belgique": "BE", "belgien": "BE",
    "switzerland": "CH", "schweiz": "CH", "suisse": "CH",
    "luxembourg": "LU", "luxemburg": "LU", "portugal": "PT", "ireland": "IE",
    "denmark": "DK", "danmark": "DK", "sweden": "SE", "sverige": "SE", "finland": "FI",
    "suomi": "FI", "norway": "NO", "norge": "NO", "czechia": "CZ", "czech republic": "CZ",
    "česko": "CZ", "slovakia": "SK", "slovensko": "SK", "hungary": "HU", "magyarország": "HU",
    "romania": "RO", "românia": "RO", "bulgaria": "BG", "greece": "GR", "ελλάδα": "GR",
    "croatia": "HR", "hrvatska": "HR", "slovenia": "SI", "slovenija": "SI",
    "estonia": "EE", "eesti": "EE", "latvia": "LV", "latvija": "LV", "lithuania": "LT",
    "lietuva": "LT", "cyprus": "CY", "malta": "MT",
}
_ISO2 = set(_COUNTRIES.values()) | {"UK", "GB", "IS", "LI"}


def country_code(raw) -> str | None:
    s = clean_text(raw)
    if not s:
        return None
    if len(s) == 2 and s.upper() in _ISO2:
        return "GR" if s.upper() == "EL" else s.upper()
    if s.upper() == "EL":
        return "GR"
    m = re.match(r"^([A-Z]{2})[\d\s-]", s)  # NUTS like DE212
    if m and m.group(1) in _ISO2:
        return m.group(1)
    return _COUNTRIES.get(s.lower())


def postal_code(raw) -> str | None:
    s = clean_text(raw)
    if not s:
        return None
    m = re.search(r"\b(\d{2}-\d{3}|\d{4}\s?[A-Z]{2}|\d{4,5})\b", s)
    return m.group(1) if m else None


# ---------------------------------------------------------------- status

DEFAULT_WITHDRAWN = ["withdrawn", "cancelled", "canceled", "aufgehoben", "zurückgezogen",
                     "zurueckgezogen", "annulé", "annule", "annulée", "infructueux",
                     "anulado", "anulada", "desistido", "desierto", "ingetrokken", "geannuleerd",
                     "unieważnion", "uniewaznion", "odwołan", "revocat", "annullat"]
DEFAULT_CLOSED = ["closed", "expired", "abgelaufen", "beendet", "abgeschlossen", "clôturé",
                  "cloture", "clos", "cerrado", "adjudicado", "resuelta", "gesloten",
                  "zakończon", "zakonczon", "scaduto", "chiuso", "awarded", "vergeben",
                  "attribué"]


def tender_status(raw_status, deadline_iso: str | None, listed: bool,
                  keywords: dict | None = None, now: datetime | None = None) -> str:
    keywords = keywords or {}
    s = (clean_text(raw_status) or "").lower()
    withdrawn = [w.lower() for w in (keywords.get("withdrawn") or [])] + DEFAULT_WITHDRAWN
    closed = [w.lower() for w in (keywords.get("closed") or [])] + DEFAULT_CLOSED
    if s and any(w in s for w in withdrawn):
        return "withdrawn"
    if not listed:
        return "closed"
    if s and any(w in s for w in closed):
        return "closed"
    if deadline_iso:
        try:
            dl = datetime.fromisoformat(deadline_iso)
            ref = now or datetime.now(dl.tzinfo)
            if dl < ref:
                return "closed"
        except ValueError:
            pass
    return "open"


def http_url(v) -> str | None:
    """Only absolute http(s) URLs leave Watchtower. Anything else (javascript:, data:, relative
    leftovers) becomes None — a portal page must never be able to inject a script link."""
    s = clean_text(v)
    if not s:
        return None
    s = "".join(ch for ch in s if ch >= " ")          # strip control characters
    m = re.match(r"^\s*(https?)://[^\s/?#]+", s, re.I)
    return s.strip() if m else None


def looks_like_date_or_number(s: str | None) -> bool:
    if not s:
        return False
    return bool(re.fullmatch(r"[\d\s./:,-]+(uhr|h)?", s.strip().lower()))


def deadline_passed_grace(deadline_iso: str | None, days: int = 0) -> bool:
    if not deadline_iso:
        return False
    try:
        dl = datetime.fromisoformat(deadline_iso)
    except ValueError:
        return False
    return dl + timedelta(days=days) < datetime.now(dl.tzinfo)


def is_iso_date(s) -> bool:
    try:
        date.fromisoformat(str(s))
        return True
    except ValueError:
        return False
