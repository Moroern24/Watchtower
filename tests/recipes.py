"""Hand-written recipes for the mock portals (what a good LLM answer looks like).
Also used by the fake learner in orchestrator tests."""


def ssr_v1(base):
    return {
        "kind": "html",
        "list": {"url": f"{base}/ssr/tenders", "items": "table.results tbody tr",
                 "pagination": {"type": "next_link", "nextSelector": "a.next", "maxPages": 10},
                 "fields": {"url": {"css": "td:nth-child(2) a", "attr": "href"},
                            "title": {"css": "td:nth-child(2) a"},
                            "buyerName": {"css": "td:nth-child(3)"},
                            "publicationDate": {"css": "td:nth-child(1)"},
                            "submissionDeadline": {"css": "td:nth-child(4)"},
                            "id": {"css": "td:nth-child(5)"},
                            "status": {"css": "td:nth-child(6)"}}},
        "detail": {"fields": {"referenceNumber": {"label": "Vergabenummer"},
                              "procedureType": {"label": "Verfahrensart"},
                              "contractNature": {"label": "Art des Auftrags"},
                              "cpvCodes": {"label": "CPV-Code"},
                              "description": {"css": "p.beschreibung"},
                              "postalCode": {"label": "Erfüllungsort"},
                              "city": {"label": "Erfüllungsort", "regex": "\\d{5}\\s+(.+)"}}},
        "documents": {"links": "ul.docs a"},
        "dateFormats": ["%d.%m.%Y %H:%M", "%d.%m.%Y"],
        "timezone": "Europe/Berlin", "countryCode": "DE",
    }


def ssr_v2(base):
    return {
        "kind": "html",
        "list": {"url": f"{base}/ssr/tenders", "items": "article.notice-card",
                 "pagination": {"type": "page_param", "param": "page", "start": 1, "maxPages": 10},
                 "fields": {"url": {"css": "h2.notice-title a", "attr": "href"},
                            "title": {"css": "h2.notice-title a"},
                            "buyerName": {"css": "span.authority"},
                            "id": {"css": "span.ref", "regex": "Az\\.\\s*(\\S+)"},
                            "status": {"css": "span.state"},
                            "publicationDate": {"css": "div.dates",
                                                "regex": "am (\\d{2}\\.\\d{2}\\.\\d{4})"},
                            "submissionDeadline": {"css": "div.dates",
                                                   "regex": "Frist: ([\\d.]+, [\\d:]+)"}}},
        "detail": {"fields": {"referenceNumber": {"label": "Aktenzeichen"},
                              "description": {"css": "div.text"},
                              "procedureType": {"label": "Verfahren"},
                              "contractNature": {"label": "Leistungsart"},
                              "cpvCodes": {"label": "CPV"}}},
        "documents": {"links": "div.downloads a"},
        "dateFormats": ["%d.%m.%Y, %H:%M", "%d.%m.%Y"],
        "timezone": "Europe/Berlin", "countryCode": "DE",
    }


def spa(base):
    return {
        "kind": "json",
        "list": {"url": f"{base}/spa/api/search", "method": "POST",
                 "body": {"page": 0, "size": 5, "status": "OPEN"}, "items": "results",
                 "pagination": {"type": "page_param", "param": "page", "start": 0,
                                "inBody": True, "maxPages": 10},
                 "fields": {"id": {"path": "reference"},
                            "url": {"path": "tenderId", "template": f"{base}/spa/#/tender/{{value}}"},
                            "title": {"path": "name"},
                            "buyerName": {"path": "contractingAuthority.name"},
                            "publicationDate": {"path": "publishedAt"},
                            "submissionDeadline": {"path": "deadline"},
                            "status": {"path": "state"},
                            "postalCode": {"path": "contractingAuthority.address.zip"},
                            "city": {"path": "contractingAuthority.address.town"},
                            "countryCode": {"path": "contractingAuthority.address.country"},
                            "_num": {"path": "tenderId"}}},
        "detail": {"kind": "json", "urlTemplate": f"{base}/spa/api/tenders/{{_num}}",
                   "fields": {"description": {"path": "description"},
                              "cpvCodes": {"path": "cpv", "all": True},
                              "procedureType": {"path": "procedure"}}},
        "documents": {"items": "documents", "urlPath": "href", "namePath": "fileName"},
        "timezone": "Europe/Berlin", "countryCode": "DE",
    }


def multi(base):
    return {
        "kind": "html",
        "list": {"url": f"{base}/multi/avis", "items": "div.avis",
                 "fields": {"id": {"css": "span.ref"}, "url": {"css": "a.objet", "attr": "href"},
                            "title": {"css": "a.objet"}, "buyerName": {"css": "span.acheteur"},
                            "submissionDeadline": {"css": "span.limite",
                                                   "regex": "(\\d{2}/\\d{2}/\\d{4} à \\d{2}h\\d{2})"}}},
        "detail": {"fields": {"publicationDate": {"label": "Date de publication"},
                              "procedureType": {"label": "Procédure"}}},
        "documents": {"pageLink": {"css": "a.btn", "attr": "href"},
                      "links": "ul.fichiers a"},
        "dateFormats": ["%d/%m/%Y à %Hh%M", "%d/%m/%Y"], "dayFirst": True,
        "timezone": "Europe/Paris", "countryCode": "FR",
    }


def simple(base, path):
    return {
        "kind": "html",
        "list": {"url": f"{base}/{path}/tenders", "items": "table#list tr",
                 "fields": {"url": {"css": "td:nth-child(1) a", "attr": "href"},
                            "title": {"css": "td:nth-child(1) a"},
                            "buyerName": {"css": "td:nth-child(2)"},
                            "publicationDate": {"css": "td:nth-child(3)"},
                            "submissionDeadline": {"css": "td:nth-child(4)"}}},
        "detail": {"fields": {"id": {"label": "Kennung"}}},
        "documents": {"links": "a[href*='/doc/']"},
        "dateFormats": ["%d.%m.%Y %H:%M", "%d.%m.%Y"], "timezone": "Europe/Berlin",
    }
