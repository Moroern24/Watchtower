"""Local mock tender portals used by the tests and for demos.

Run:  python -m uvicorn mockportals.server:app --port 8090

Portals
  /ssr/tenders       German server-rendered table, pagination ?page=, label-based detail pages,
                     PDF + ZIP docs, a withdrawn tender. /control/layout?v=2 switches to a new
                     layout (cards, renamed labels) to exercise repair.
  /spa/              JavaScript app; data via POST /spa/api/search (JSON), detail API per id.
  /multi/avis        French portal; documents need 3 steps: detail → DCE page → download page → file.
  /captcha/tenders   Captcha widget on the page, but documents download without it.
  /login/tenders     List is public, documents redirect to a login form (must be reported).
  /notice/4711       A notice/summary page that links to the actual portal.
"""
from __future__ import annotations

import io
import time
import zipfile
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

app = FastAPI(title="Mock portals")
BERLIN = ZoneInfo("Europe/Berlin")
PARIS = ZoneInfo("Europe/Paris")
STATE = {"layout": 1, "hidden": set(), "hits": []}


@app.middleware("http")
async def record(request: Request, call_next):
    STATE["hits"].append((time.monotonic(), request.url.path))
    return await call_next(request)


def _now():
    return datetime.now(BERLIN).replace(microsecond=0, second=0)


BUYERS = ["Stadt Musterstadt", "Landkreis Beispiel", "Bundesamt für Bauwesen",
          "Universitätsklinikum Nord", "Stadtwerke Süd GmbH", "Gemeinde Waldhausen"]
TITLES = ["Neubau Grundschule – Rohbauarbeiten", "Lieferung von Laborgeräten",
          "IT-Dienstleistungen Rechenzentrum", "Sanierung Brücke B12",
          "Reinigungsleistungen Rathaus", "Beschaffung Feuerwehrfahrzeug",
          "Rahmenvertrag Büromaterial", "Planungsleistungen Kita", "Winterdienst 2027",
          "Softwarelizenzen ERP", "Glasfaserausbau Los 3", "Catering Mensa"]
CPVS = ["45214200-2", "38000000-5", "72000000-5", "45221111-3", "90910000-9", "34144210-3",
        "30190000-7", "71200000-0", "90620000-9", "48000000-8", "45231600-1", "55520000-1"]
NATURE = ["Bauleistung", "Lieferleistung", "Dienstleistung", "Bauleistung", "Dienstleistung",
          "Lieferleistung", "Lieferleistung", "Dienstleistung", "Dienstleistung",
          "Lieferleistung", "Bauleistung", "Dienstleistung"]


def tenders(prefix: str, n: int = 12):
    base = _now()
    out = []
    for i in range(n):
        pub = (base - timedelta(days=3 + i)).date()
        dl = (base + timedelta(days=10 + i * 3)).replace(hour=10 + (i % 4), minute=0)
        out.append({
            "id": f"{prefix}-2026-{1000 + i}", "num": 1000 + i,
            "title": TITLES[i % len(TITLES)], "buyer": BUYERS[i % len(BUYERS)],
            "pub": pub, "deadline": dl, "cpv": CPVS[i % len(CPVS)],
            "nature": NATURE[i % len(NATURE)],
            "proc": "Offenes Verfahren" if i % 3 else "Nicht offenes Verfahren",
            "status": "Aufgehoben" if i == 5 else "Aktiv",
            "city": ["Musterstadt", "Beispielheim", "Bonn", "Kiel", "München", "Waldhausen"][i % 6],
            "plz": ["12345", "54321", "53113", "24103", "80331", "79100"][i % 6],
            "desc": f"Ausführliche Beschreibung der Leistung Nr. {i}: {TITLES[i % len(TITLES)]}.",
        })
    return out


def pdf_bytes(label: str) -> bytes:
    return (b"%PDF-1.4\n% mock\n1 0 obj<<>>endobj\n" + label.encode() * 20 + b"\n%%EOF\n")


def zip_bytes(label: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Leistungsverzeichnis.pdf", pdf_bytes("LV " + label))
        zf.writestr("Formblaetter/Angebotsschreiben.docx", b"PK-mock-docx " + label.encode())
    return buf.getvalue()


def page(title: str, body: str, extra_head: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<title>{escape(title)}</title>{extra_head}<style>body{{font-family:sans-serif}}</style></head>
<body><header><nav><a href="/">Start</a> | <a href="/impressum">Impressum</a></nav></header>
<main>{body}</main><footer>Powered by MockVergabe 3.2</footer></body></html>""")


# ------------------------------------------------------------------ control

@app.get("/control/layout")
def set_layout(v: int = 1):
    STATE["layout"] = v
    return {"layout": v}


@app.get("/control/hide")
def hide(id: str):
    STATE["hidden"].add(id)
    return {"hidden": sorted(STATE["hidden"])}


@app.get("/control/reset")
def reset():
    STATE.update(layout=1, hidden=set(), hits=[])
    return {"ok": True}


@app.get("/control/redirect")
def redirect_to(to: str):
    """Open redirect used by the SSRF tests (a portal redirecting to an internal address)."""
    return RedirectResponse(to)


@app.get("/control/hits")
def hits():
    return {"hits": [[t, p] for t, p in STATE["hits"]]}


# ------------------------------------------------------------------ SSR (German)

def _ssr_list():
    return [t for t in tenders("VG") if t["id"] not in STATE["hidden"]]


@app.get("/ssr/tenders")
def ssr_list(page_no: int = 1, page: int | None = None):
    p = page or page_no
    items = _ssr_list()
    per = 5
    chunk = items[(p - 1) * per: p * per]
    pages = (len(items) + per - 1) // per
    nav = " ".join(f'<a class="pg" href="/ssr/tenders?page={i}">{i}</a>' for i in range(1, pages + 1))
    if p < pages:
        nav += f' <a class="next" rel="next" href="/ssr/tenders?page={p + 1}">Weiter »</a>'
    if STATE["layout"] == 1:
        rows = "".join(
            f"""<tr><td>{t['pub'].strftime('%d.%m.%Y')}</td>
<td><a href="/ssr/tender/{t['num']}">{escape(t['title'])}</a></td>
<td>{escape(t['buyer'])}</td><td>{t['deadline'].strftime('%d.%m.%Y %H:%M')}</td>
<td>{t['id']}</td><td>{t['status']}</td></tr>""" for t in chunk)
        body = f"""<h1>Aktuelle Ausschreibungen</h1>
<table class="results"><thead><tr><th>Veröffentlicht</th><th>Bezeichnung</th><th>Vergabestelle</th>
<th>Angebotsfrist</th><th>Vergabenummer</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>
<div class="paging">{nav}</div>"""
    else:
        cards = "".join(
            f"""<article class="notice-card"><h2 class="notice-title">
<a href="/ssr/vergabe?nr={t['num']}">{escape(t['title'])}</a></h2>
<div class="meta"><span class="authority">{escape(t['buyer'])}</span> ·
<span class="ref">Az. {t['id']}</span> · <span class="state">{t['status']}</span></div>
<div class="dates">Veröffentlicht am {t['pub'].strftime('%d.%m.%Y')} – Frist: {t['deadline'].strftime('%d.%m.%Y, %H:%M')} Uhr</div>
</article>""" for t in chunk)
        body = f"<h1>Bekanntmachungen</h1><section id='list'>{cards}</section><div class='paging'>{nav}</div>"
    return page_resp("Ausschreibungen", body)


def page_resp(title, body, extra_head=""):
    return page(title, body, extra_head)


def _find(prefix, num):
    for t in tenders(prefix):
        if t["num"] == num:
            return t
    return None


def _ssr_detail(t):
    if STATE["layout"] == 1:
        return f"""<h1>{escape(t['title'])}</h1><dl class="facts">
<dt>Vergabenummer</dt><dd>{t['id']}</dd>
<dt>Öffentlicher Auftraggeber</dt><dd>{escape(t['buyer'])}</dd>
<dt>Verfahrensart</dt><dd>{t['proc']}</dd><dt>Art des Auftrags</dt><dd>{t['nature']}</dd>
<dt>Veröffentlichungsdatum</dt><dd>{t['pub'].strftime('%d.%m.%Y')}</dd>
<dt>Ablauf der Angebotsfrist</dt><dd>{t['deadline'].strftime('%d.%m.%Y %H:%M')}</dd>
<dt>CPV-Code</dt><dd>{t['cpv']}</dd><dt>Erfüllungsort</dt><dd>{t['plz']} {t['city']}</dd>
<dt>Status</dt><dd>{t['status']}</dd></dl><p class="beschreibung">{escape(t['desc'])}</p>
<h2>Vergabeunterlagen</h2><ul class="docs">
<li><a href="/ssr/files/{t['num']}/Bekanntmachung.pdf">Bekanntmachung (PDF)</a></li>
<li><a href="/ssr/files/{t['num']}/Unterlagen.zip">Alle Unterlagen (ZIP)</a></li></ul>"""
    return f"""<div class="head"><h1 class="t">{escape(t['title'])}</h1></div><table class="info">
<tr><th>Aktenzeichen:</th><td>{t['id']}</td></tr><tr><th>Auftraggeber:</th><td>{escape(t['buyer'])}</td></tr>
<tr><th>Verfahren:</th><td>{t['proc']}</td></tr><tr><th>Leistungsart:</th><td>{t['nature']}</td></tr>
<tr><th>Bekanntmachung vom:</th><td>{t['pub'].strftime('%d.%m.%Y')}</td></tr>
<tr><th>Frist:</th><td>{t['deadline'].strftime('%d.%m.%Y, %H:%M')} Uhr</td></tr>
<tr><th>CPV:</th><td>{t['cpv']}</td></tr><tr><th>Ort:</th><td>{t['plz']} {t['city']}</td></tr>
<tr><th>Zustand:</th><td>{t['status']}</td></tr></table><div class="text">{escape(t['desc'])}</div>
<div class="downloads"><a class="dl" href="/ssr/files/{t['num']}/Bekanntmachung.pdf">Bekanntmachung</a>
<a class="dl" href="/ssr/files/{t['num']}/Unterlagen.zip">Unterlagen</a></div>"""


@app.get("/ssr/tender/{num}")
def ssr_detail(num: int):
    t = _find("VG", num)
    if not t:
        return page_resp("Nicht gefunden", "<p>Nicht gefunden</p>")
    if STATE["layout"] != 1:
        return RedirectResponse(f"/ssr/vergabe?nr={num}")
    return page_resp(t["title"], _ssr_detail(t))


@app.get("/ssr/vergabe")
def ssr_detail_v2(nr: int):
    t = _find("VG", nr)
    if not t:
        return page_resp("Nicht gefunden", "<p>Nicht gefunden</p>")
    return page_resp(t["title"], _ssr_detail(t))


@app.get("/ssr/files/{num}/{name}")
def ssr_file(num: int, name: str):
    if name.endswith(".zip"):
        return Response(zip_bytes(str(num)), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return Response(pdf_bytes(f"{name}-{num}"), media_type="application/pdf")


# ------------------------------------------------------------------ SPA (JSON API)

SPA_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>eTender SPA</title></head>
<body><div id="app">Loading…</div><script>
async function load(p){const r=await fetch('/spa/api/search',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({page:p,size:5,status:'OPEN'})});
const d=await r.json();document.getElementById('app').innerHTML='<ul>'+d.results.map(t=>
'<li><a href="/spa/#/tender/'+t.tenderId+'">'+t.name+'</a></li>').join('')+'</ul>'+
'<button onclick="load('+(p+1)+')">Next</button>';}
async function detail(id){const r=await fetch('/spa/api/tenders/'+id);const t=await r.json();
document.getElementById('app').innerHTML='<h1>'+t.name+'</h1><p>'+t.description+'</p>'+
t.documents.map(x=>'<a href="'+x.href+'">'+x.fileName+'</a>').join(' ');}
function route(){const m=location.hash.match(/#\\/tender\\/(\\d+)/);if(m)detail(m[1]);else load(0);}
window.onhashchange=route;route();</script></body></html>"""


@app.get("/spa/")
def spa_index():
    return HTMLResponse(SPA_HTML)


def _spa_obj(t, full=False):
    o = {"tenderId": t["num"], "reference": t["id"], "name": t["title"],
         "contractingAuthority": {"name": t["buyer"], "address": {"zip": t["plz"],
                                  "town": t["city"], "country": "DE"}},
         "publishedAt": t["pub"].isoformat(),
         "deadline": t["deadline"].astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "state": "CANCELLED" if t["status"] == "Aufgehoben" else "OPEN"}
    if full:
        o.update(description=t["desc"], cpv=[t["cpv"]], procedure="OPEN",
                 documents=[{"fileName": "Leistungsbeschreibung.pdf",
                             "href": f"/spa/api/files/{t['num']}/1"},
                            {"fileName": "Vertrag.pdf", "href": f"/spa/api/files/{t['num']}/2"}])
    return o


@app.post("/spa/api/search")
async def spa_search(request: Request):
    body = await request.json()
    p, size = int(body.get("page", 0)), int(body.get("size", 5))
    items = tenders("SPA")
    return {"total": len(items), "page": p,
            "results": [_spa_obj(t) for t in items[p * size:(p + 1) * size]]}


@app.get("/spa/api/tenders/{num}")
def spa_detail(num: int):
    t = _find("SPA", num)
    return JSONResponse(_spa_obj(t, True) if t else {"error": "not found"},
                        status_code=200 if t else 404)


@app.get("/spa/api/files/{num}/{n}")
def spa_file(num: int, n: int):
    return Response(pdf_bytes(f"spa-{num}-{n}"), media_type="application/pdf")


# ------------------------------------------------------------------ multi-step (French)

@app.get("/multi/avis")
def multi_list():
    items = tenders("FR", 7)
    rows = "".join(f"""<div class="avis"><span class="ref">{t['id']}</span>
<a class="objet" href="/multi/avis/{t['num']}">{escape(t['title'])}</a>
<span class="acheteur">{escape(t['buyer'])}</span>
<span class="limite">Date limite : {t['deadline'].astimezone(PARIS).strftime('%d/%m/%Y à %Hh%M')}</span>
</div>""" for t in items)
    return page_resp("Avis de marchés", f"<h1>Consultations en cours</h1>{rows}")


@app.get("/multi/avis/{num}")
def multi_detail(num: int):
    t = _find("FR", num)
    body = f"""<h1>{escape(t['title'])}</h1><table>
<tr><td>Référence</td><td>{t['id']}</td></tr><tr><td>Acheteur public</td><td>{escape(t['buyer'])}</td></tr>
<tr><td>Date de publication</td><td>{t['pub'].strftime('%d/%m/%Y')}</td></tr>
<tr><td>Date limite de remise des plis</td><td>{t['deadline'].astimezone(PARIS).strftime('%d/%m/%Y %H:%M')}</td></tr>
<tr><td>Procédure</td><td>Procédure adaptée ouverte</td></tr></table>
<p><a class="btn" href="/multi/dce/{num}">Accéder au DCE</a></p>"""
    return page_resp(t["title"], body)


@app.get("/multi/dce/{num}")
def multi_dce(num: int):
    body = "".join(f'<li><a href="/multi/dl/{num}/{n}">{name}</a></li>'
                   for n, name in [(1, "RC.pdf"), (2, "CCAP.pdf")])
    return page_resp("Dossier de consultation", f"<h1>DCE</h1><ul class='fichiers'>{body}</ul>")


@app.get("/multi/dl/{num}/{n}")
def multi_dl(num: int, n: int):
    return page_resp("Téléchargement",
                     f'<p>Votre téléchargement va commencer. '
                     f'<a id="go" href="/multi/file/{num}/{n}">Télécharger le fichier</a></p>')


@app.get("/multi/file/{num}/{n}")
def multi_file(num: int, n: int):
    name = {1: "RC.pdf", 2: "CCAP.pdf"}.get(n, "doc.pdf")
    return Response(pdf_bytes(f"fr-{num}-{n}"), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ captcha + login

def _simple_list(prefix, path, extra=""):
    rows = "".join(f"""<tr><td><a href="/{path}/t/{t['num']}">{escape(t['title'])}</a></td>
<td>{escape(t['buyer'])}</td><td>{t['pub'].strftime('%d.%m.%Y')}</td>
<td>{t['deadline'].strftime('%d.%m.%Y %H:%M')}</td></tr>""" for t in tenders(prefix, 6))
    return page_resp("Ausschreibungen", f"""{extra}<table id="list"><tr><th>Titel</th><th>Auftraggeber</th>
<th>Datum</th><th>Frist</th></tr>{rows}</table>""")


@app.get("/captcha/tenders")
def captcha_list():
    widget = ('<form action="/captcha/search"><div class="g-recaptcha" data-sitekey="x"></div>'
              '<input name="q"><button>Suchen</button></form>')
    return _simple_list("CP", "captcha", widget)


@app.get("/captcha/t/{num}")
def captcha_detail(num: int):
    t = _find("CP", num)
    return page_resp(t["title"], f"""<h1>{escape(t['title'])}</h1><p>Vergabestelle: {escape(t['buyer'])}</p>
<p>Kennung: {t['id']}</p><a href="/captcha/doc/{num}.pdf">Unterlagen herunterladen</a>""")


@app.get("/captcha/doc/{name}")
def captcha_doc(name: str):
    return Response(pdf_bytes("cp" + name), media_type="application/pdf")


@app.get("/captcha/docpage/{num}")
def captcha_docpage(num: int):
    """Public download page whose header has a login box and a turnstile widget (trap C2)."""
    head = ('<form class="login" action="/login/signin" method="post"><input name="u">'
            '<input type="password" name="p"><button>Login</button></form>'
            '<div class="cf-turnstile" data-sitekey="x"></div>')
    return page_resp("Unterlagen", f'{head}<h1>Vergabeunterlagen</h1>'
                     f'<a href="/captcha/doc/{num}.pdf">Leistungsbeschreibung.pdf</a>')


@app.get("/forbidden/doc/{name}")
def forbidden_doc(name: str):
    return Response("Access denied by firewall", status_code=403)


@app.get("/login/tenders")
def login_list():
    return _simple_list("LG", "login")


@app.get("/login/t/{num}")
def login_detail(num: int):
    t = _find("LG", num)
    return page_resp(t["title"], f"""<h1>{escape(t['title'])}</h1><p>Vergabestelle: {escape(t['buyer'])}</p>
<p>Kennung: {t['id']}</p><a href="/login/doc/{num}.pdf">Vergabeunterlagen</a>""")


@app.get("/login/doc/{name}")
def login_doc(name: str):
    return RedirectResponse("/login/signin?next=/login/doc/" + name)


@app.get("/login/signin")
def login_form():
    return page_resp("Anmelden", """<h1>Anmeldung</h1><form method="post"><label>Benutzername
<input name="u"></label><label>Passwort <input type="password" name="p"></label>
<button>Anmelden</button></form>""")


# ------------------------------------------------------------------ notice / summary

@app.get("/notice/{nid}")
def notice(nid: int):
    return page_resp("Bekanntmachung", f"""<h1>Bekanntmachung {nid}</h1>
<p>Kurzbeschreibung der Bekanntmachung. Die Vergabeunterlagen und alle aktuellen Ausschreibungen
finden Sie auf der Vergabeplattform.</p><p><a href="/ssr/tenders">Zur Vergabeplattform – aktuelle Ausschreibungen</a></p>""")


@app.get("/")
def root():
    return page_resp("Mock portals", "<ul>" + "".join(
        f'<li><a href="{u}">{u}</a></li>' for u in
        ["/ssr/tenders", "/spa/", "/multi/avis", "/captcha/tenders", "/login/tenders",
         "/notice/4711"]) + "</ul>")
