from watchtower import normalize as N


def test_dates_local_time_dst():
    assert N.to_datetime("09.12.2026 10:00", ["%d.%m.%Y %H:%M"], "Europe/Berlin") == \
        "2026-12-09T10:00:00+01:00"
    assert N.to_datetime("15.07.2026 10:00", None, "Europe/Berlin") == "2026-07-15T10:00:00+02:00"
    assert N.to_datetime("2026-12-09", None, "Europe/Paris") == "2026-12-09T23:59:59+01:00"
    assert N.to_datetime("2026-12-09T09:00:00Z", None, "Europe/Berlin") == \
        "2026-12-09T10:00:00+01:00"
    assert N.to_datetime("9 décembre 2026 à 10h30", None, "Europe/Paris") == \
        "2026-12-09T10:30:00+01:00"
    assert N.to_datetime("12 de diciembre de 2026 a las 14:00 horas", None, "Europe/Madrid") == \
        "2026-12-12T14:00:00+01:00"
    assert N.to_datetime("garbage", None, "Europe/Berlin") is None
    assert N.to_datetime("10.12.2026 10:00", None, None) is None  # no tz → no guess
    assert N.to_date("20.11.2026") == "2026-11-20"
    assert N.to_date("2026-11-20") == "2026-11-20"


def test_cpv_and_enums():
    assert N.cpv_codes("45000000-7, 45210000-2 Bau") == ["45000000", "45210000"]
    assert N.cpv_codes(["72000000"]) == ["72000000"]
    assert N.procedure_type("Offenes Verfahren") == "open"
    assert N.procedure_type("Nicht offenes Verfahren") == "restricted"
    assert N.procedure_type("Verhandlungsverfahren mit Teilnahmewettbewerb") == "negotiated"
    assert N.procedure_type("Procedimiento abierto") == "open"
    assert N.procedure_type("Hello") is None
    assert N.contract_nature("Bauleistung") == "works"
    assert N.contract_nature("Fournitures") == "supplies"
    assert N.contract_nature("Dienstleistungen") == "services"
    assert N.country_code("Deutschland") == "DE" and N.country_code("DE212") == "DE"


def test_status():
    assert N.tender_status("Aufgehoben", None, True) == "withdrawn"
    assert N.tender_status(None, "2000-01-01T10:00:00+01:00", True) == "closed"
    assert N.tender_status(None, "2099-01-01T10:00:00+01:00", True) == "open"
    assert N.tender_status(None, "2099-01-01T10:00:00+01:00", False) == "closed"
