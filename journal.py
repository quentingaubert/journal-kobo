"""Journal IA quotidien : RSS -> Gemini -> page HTML -> Instapaper -> Kobo.

Usage :
  python journal.py build [--out docs]        génère docs/AAAA-MM-JJ.html
  python journal.py fetch                     teste les flux RSS (sans IA)
  python journal.py send URL                  envoie une page à Instapaper
"""

import argparse
import calendar
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
PARIS = ZoneInfo("Europe/Paris")
USER_AGENT = "Mozilla/5.0 (journal-kobo; +https://github.com)"
WINDOW_HOURS = 26          # fenêtre d'actualité retenue
MAX_PER_FEED = 25
SUMMARY_CHARS = 500
KEEP_DAYS = 30             # numéros conservés dans docs/
INSTAPAPER_API = "https://www.instapaper.com/api/2"
MODELS = os.environ.get("GEMINI_MODELS", "gemini-flash-latest,gemini-3.8-flash,gemini-3.5-flash-lite").split(",")

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


def date_fr(d):
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- RSS

def clean_text(raw):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:SUMMARY_CHARS] + ("…" if len(text) > SUMMARY_CHARS else "")


def norm_title(title):
    return re.sub(r"\W+", " ", title.lower()).strip()


def fetch_items():
    config = yaml.safe_load((ROOT / "feeds.yaml").read_text())
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    items, seen_urls, seen_titles = [], set(), set()

    for rubrique, feeds in config["rubriques"].items():
        for feed in feeds:
            try:
                resp = requests.get(feed["url"], headers={"User-Agent": USER_AGENT}, timeout=20)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except Exception as exc:  # un flux en panne ne doit pas bloquer le journal
                log(f"  ! {feed['source']} ({feed['url']}) : {exc}")
                continue

            kept = 0
            for entry in parsed.entries:
                if kept >= MAX_PER_FEED:
                    break
                stamp = entry.get("published_parsed") or entry.get("updated_parsed")
                if stamp and datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc) < since:
                    continue
                url = (entry.get("link") or "").split("?")[0]
                title = clean_text(entry.get("title"))
                if not url.startswith(("https://", "http://")) or not title or url in seen_urls or norm_title(title) in seen_titles:
                    continue
                seen_urls.add(url)
                seen_titles.add(norm_title(title))
                items.append({
                    "id": len(items) + 1,
                    "rubrique": rubrique,
                    "source": feed["source"],
                    "titre": title,
                    "extrait": clean_text(entry.get("summary")),
                    "url": url,
                })
                kept += 1
            log(f"  {feed['source']:<22} {rubrique:<18} {kept:>3} articles")

    log(f"Total : {len(items)} articles")
    return items, list(config["rubriques"])


# --------------------------------------------------------------------------- IA

PROMPT = """Tu es le rédacteur en chef d'un journal quotidien en français, lu chaque matin
sur liseuse en 10 à 15 minutes (environ 2 000 à 2 500 mots au total).

Nous sommes le {date}. Tu reçois ci-dessous une liste d'articles (id, rubrique indicative,
source, titre, extrait) publiés dans les dernières 24 heures.

Ta mission :
1. Sélectionner les sujets les plus importants, en regroupant les articles qui parlent
   du même événement (dédoublonnage).
2. Pour chaque rubrique ({rubriques}), retenir 2 à 4 sujets (moins si l'actualité est pauvre).
   Un sujet n'apparaît que dans une seule rubrique. Ignore faits divers mineurs, sport,
   people et articles purement promotionnels.
3. Pour chaque sujet : un titre clair, un résumé factuel (60 à 100 mots) et l'enjeu.
   L'enjeu dit concrètement ce qui en découle ou ce qui se joue ensuite : une décision
   attendue, une échéance, qui est touché et comment. Interdits : les formules creuses
   (« illustre », « met en évidence », « souligne », « témoigne de », « marque un tournant »).
   Si les extraits ne permettent rien de concret, laisse "enjeu" vide.
4. En tête de journal, « L'essentiel » : 4 à 6 phrases courtes sur les faits majeurs du jour.
5. En fin de journal, « À suivre aujourd'hui » : 0 à 5 événements prévus le {date}
   (vote, sommet, procès, publication de chiffres, grève…), uniquement s'ils sont
   explicitement annoncés pour ce jour dans les extraits, et pas encore commencés ou terminés.
   Ne déduis aucune date : « dimanche » dans un extrait d'hier ne désigne pas forcément aujourd'hui.

Suivi depuis le numéro précédent ({veille}) :
{sujets_veille}
- Un sujet déjà traité n'est repris que s'il y a un fait nouveau. Mets alors "suite" à true
  et consacre le résumé à ce qui a changé, sans répéter le contexte déjà donné.
- Un sujet déjà traité sans fait nouveau est ignoré.

Règles absolues :
- N'utilise QUE les informations présentes dans les titres et extraits fournis.
  N'ajoute aucun chiffre, nom, date, citation ou fait qui n'y figure pas.
- Tu peux seulement rappeler un contexte général notoire et stable (ex. ce qu'est une
  institution), jamais un fait d'actualité absent des extraits.
- En cas d'incertitude ou de contradiction entre sources, dis-le explicitement.
- Chaque sujet et chaque point de « L'essentiel » cite dans "sources" les id des articles
  qui le justifient. Aucun élément sans source.
- Style factuel, neutre, phrases simples, pas de sensationnalisme.

ARTICLES :
{articles}
"""

SOURCED_TEXT = {
    "type": "OBJECT",
    "properties": {
        "texte": {"type": "STRING"},
        "sources": {"type": "ARRAY", "items": {"type": "INTEGER"}},
    },
    "required": ["texte", "sources"],
}

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "essentiel": {"type": "ARRAY", "items": SOURCED_TEXT},
        "rubriques": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "nom": {"type": "STRING"},
                    "sujets": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "titre": {"type": "STRING"},
                                "resume": {"type": "STRING"},
                                "enjeu": {"type": "STRING"},
                                "suite": {"type": "BOOLEAN"},
                                "sources": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                            },
                            "required": ["titre", "resume", "enjeu", "suite", "sources"],
                        },
                    },
                },
                "required": ["nom", "sujets"],
            },
        },
        "a_suivre": {"type": "ARRAY", "items": SOURCED_TEXT},
    },
    "required": ["essentiel", "rubriques", "a_suivre"],
}


def call_gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
        },
    }
    last_error = None
    for model in MODELS:
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model.strip()}:generateContent",
                    headers={"x-goog-api-key": key},
                    json=body,
                    timeout=300,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {resp.status_code} : {resp.text[:300]}")
                if resp.status_code >= 400:  # modèle inconnu ou refusé : inutile d'insister
                    last_error = RuntimeError(f"HTTP {resp.status_code} : {resp.text[:300]}")
                    log(f"  ! {model} : {last_error}")
                    break
                resp.raise_for_status()
                parts = resp.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                log(f"Réponse IA reçue ({model})")
                return json.loads(text)
            except Exception as exc:
                last_error = exc
                log(f"  ! {model} tentative {attempt + 1} : {exc}")
                time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"Gemini indisponible : {last_error}")


def validate(edition, items, rubriques):
    """Écarte tout élément sans source réelle : c'est le code, pas l'IA, qui fournit les liens."""
    by_id = {it["id"]: it for it in items}

    def resolve(ids):
        return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]

    def sourced(points):
        kept = []
        for point in points:
            src = resolve(point.get("sources", []))
            if src and point.get("texte"):
                kept.append({"texte": point["texte"], "sources": src})
        return kept

    sections = {}
    for rub in edition.get("rubriques", []):
        for sujet in rub.get("sujets", []):
            src = resolve(sujet.get("sources", []))
            if not src:
                log(f"  ! sujet sans source écarté : {sujet.get('titre')}")
                continue
            sections.setdefault(rub.get("nom", "Divers"), []).append({**sujet, "sources": src})

    ordered = [(n, sections.pop(n)) for n in rubriques if n in sections]
    ordered += list(sections.items())
    return sourced(edition.get("essentiel", [])), ordered, sourced(edition.get("a_suivre", []))


# --------------------------------------------------------------------------- Météo

WMO = [(0, "ensoleillé"), (2, "éclaircies"), (3, "couvert"), (48, "brouillard"), (57, "bruine"),
       (67, "pluie"), (77, "neige"), (82, "averses"), (86, "averses de neige"), (99, "orages")]


def fetch_meteo():
    """Une ligne de prévision pour la journée, ou None : la météo ne doit jamais bloquer le journal."""
    conf = yaml.safe_load((ROOT / "feeds.yaml").read_text()).get("meteo")
    if not conf:
        return None
    try:
        resp = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": conf["latitude"], "longitude": conf["longitude"], "timezone": "Europe/Paris",
            "forecast_days": 1,
            "daily": "weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max",
        }, timeout=20)
        resp.raise_for_status()
        d = {k: v[0] for k, v in resp.json()["daily"].items()}
    except Exception as exc:
        log(f"  ! météo indisponible : {exc}")
        return None
    ciel = next(label for code, label in WMO if d["weather_code"] <= code)
    line = (f"{conf['ville']} : {ciel}, {round(d['temperature_2m_min'])} à "
            f"{round(d['temperature_2m_max'])} °C")
    if d.get("precipitation_probability_max"):
        line += f", risque de pluie {d['precipitation_probability_max']} %"
    return line + "."


# --------------------------------------------------------------------------- HTML

def e(text):
    return html.escape(text or "", quote=True)


def source_links(sources):
    """Une fois par média, avec le lien de son premier article : « Franceinfo (4), Le Monde »."""
    by_source = {}
    for s in sources:
        by_source.setdefault(s["source"], []).append(s)
    return ", ".join(f'<a href="{e(arts[0]["url"])}">{e(name)}</a>' + (f" ({len(arts)})" if len(arts) > 1 else "")
                     for name, arts in by_source.items())


def render(essentiel, sections, a_suivre, meteo, day):
    title = f"Journal du {date_fr(day)}"
    words = sum(len((s["resume"] + " " + s["enjeu"]).split()) for _, sujets in sections for s in sujets)
    words += sum(len(p["texte"].split()) for p in essentiel + a_suivre)
    minutes = max(1, round(words / 200))

    out = [
        "<!doctype html>",
        '<html lang="fr"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{e(title)}</title>",
        f'<meta name="author" content="Journal IA">',
        "<style>body{font-family:Georgia,serif;max-width:40em;margin:auto;padding:1em;line-height:1.5}"
        "h2{border-bottom:2px solid #000}.src{font-size:.9em;font-style:italic}</style>",
        "</head><body><article>",
        f"<h1>{e(title)}</h1>",
        f"<p><em>Environ {minutes} minutes de lecture · synthèse générée par IA à partir de flux RSS, "
        "sources citées sous chaque sujet.</em></p>",
    ]
    if meteo:
        out.append(f"<p><strong>Météo</strong> · {e(meteo)}</p>")
    if essentiel:
        out.append("<h2>L'essentiel</h2><ul>")
        out += [f"<li>{e(p['texte'])}</li>" for p in essentiel]
        out.append("</ul>")
    for nom, sujets in sections:
        out.append(f"<h2>{e(nom)}</h2>")
        for s in sujets:
            out.append(f"<h3>{e(s['titre'])}{' <em>(suite)</em>' if s.get('suite') else ''}</h3>")
            out.append(f"<p>{e(s['resume'])}</p>")
            if s["enjeu"].strip():
                out.append(f"<p><strong>Pourquoi c'est important :</strong> {e(s['enjeu'])}</p>")
            out.append(f'<p class="src">Sources : {source_links(s["sources"])}</p>')
    if a_suivre:
        out.append("<h2>À suivre aujourd'hui</h2><ul>")
        out += [f'<li>{e(p["texte"])} <span class="src">— {source_links(p["sources"])}</span></li>'
                for p in a_suivre]
        out.append("</ul>")
    out.append("</article></body></html>")
    return title, "\n".join(out)


def previous_issue(out_dir, day):
    """Sujets du numéro précédent. Une relance le même jour réutilise ceux de la veille."""
    try:
        latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if latest.get("date") == f"{day:%Y-%m-%d}":
        return latest.get("precedent")
    return {"date": latest.get("date"), "sujets": latest.get("sujets", [])}


def write_site(out_dir, day, title, page, sujets, precedent):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{day:%Y-%m-%d}.html").write_text(page, encoding="utf-8")

    limit = f"{day - timedelta(days=KEEP_DAYS):%Y-%m-%d}"
    issues = sorted(out_dir.glob("????-??-??.html"), reverse=True)
    for old in [p for p in issues if p.stem < limit]:
        old.unlink()
    links = "".join(f'<li><a href="{p.name}">{p.stem}</a></li>' for p in issues if p.stem >= limit)
    (out_dir / "index.html").write_text(
        f'<!doctype html><html lang="fr"><meta charset="utf-8"><title>Journal IA</title>'
        f"<h1>Journal IA</h1><ul>{links}</ul></html>", encoding="utf-8")
    (out_dir / "latest.json").write_text(
        json.dumps({"date": f"{day:%Y-%m-%d}", "title": title, "file": f"{day:%Y-%m-%d}.html",
                    "sujets": sujets, "precedent": precedent}, ensure_ascii=False, indent=1),
        encoding="utf-8")


# --------------------------------------------------------------------------- Instapaper

def send_to_instapaper(url, title):
    # Attendre que GitHub Pages serve la page : Instapaper la télécharge immédiatement.
    for _ in range(30):
        try:
            if requests.get(url, timeout=20).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(10)
    else:
        raise RuntimeError(f"Page non accessible : {url}")

    token = os.environ.get("INSTAPAPER_TOKEN")
    if not token:  # API simple : ajout seulement, pas de ménage possible
        resp = requests.post("https://www.instapaper.com/api/add", data={
            "username": os.environ["INSTAPAPER_USERNAME"],
            "password": os.environ.get("INSTAPAPER_PASSWORD", ""),
            "url": url,
            "title": title,
        }, timeout=60)
        if resp.status_code != 201:
            raise RuntimeError(f"Instapaper a refusé l'ajout (HTTP {resp.status_code}) : {resp.text[:200]}")
        log(f"Envoyé à Instapaper : {url}")
        return

    api = requests.Session()
    api.headers["Authorization"] = f"Bearer {token}"
    resp = api.post(f"{INSTAPAPER_API}/bookmarks", json={"url": url, "title": title}, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"Instapaper a refusé l'ajout (HTTP {resp.status_code}) : {resp.text[:200]}")
    new_id = resp.json()["id"]
    log(f"Envoyé à Instapaper : {url}")

    # Ménage : archive les numéros précédents (même dossier GitHub Pages) restés dans la liste.
    # Un échec ici ne doit pas faire échouer le journal du jour, juste laisser un avertissement.
    site = url.rsplit("/", 1)[0] + "/"
    try:
        resp = api.get(f"{INSTAPAPER_API}/bookmarks", params={"section": "home", "limit": 500}, timeout=60)
        resp.raise_for_status()
        old = [b for b in resp.json()["bookmarks"]
               if b["id"] != new_id and (b.get("url") or "").startswith(site)]
        for b in old:
            api.post(f"{INSTAPAPER_API}/bookmarks/{b['id']}/move", json={"section": "archive"},
                     timeout=60).raise_for_status()
            log(f"Archivé : {b.get('title')}")
    except requests.RequestException as exc:
        print(f"::warning::Ménage Instapaper impossible : {exc}", flush=True)


# --------------------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build")
    build.add_argument("--out", default="docs")
    sub.add_parser("fetch")
    send = sub.add_parser("send")
    send.add_argument("url")
    send.add_argument("--title", default=None)
    args = parser.parse_args()

    if args.cmd == "fetch":
        fetch_items()
    elif args.cmd == "build":
        day = datetime.now(PARIS).date()
        items, rubriques = fetch_items()
        if len(items) < 10:
            raise SystemExit("Trop peu d'articles récupérés, journal annulé.")
        articles = "\n".join(
            json.dumps({k: it[k] for k in ("id", "rubrique", "source", "titre", "extrait")},
                       ensure_ascii=False) for it in items)
        out_dir = ROOT / args.out
        precedent = previous_issue(out_dir, day)
        sujets_veille = "\n".join(f"- {t}" for t in (precedent or {}).get("sujets", [])) or "(aucun)"
        edition = call_gemini(PROMPT.format(
            date=date_fr(day), rubriques=", ".join(rubriques), articles=articles,
            veille=(precedent or {}).get("date") or "aucun", sujets_veille=sujets_veille))
        essentiel, sections, a_suivre = validate(edition, items, rubriques)
        if not sections:
            raise SystemExit("L'IA n'a produit aucun sujet sourcé, journal annulé.")
        title, page = render(essentiel, sections, a_suivre, fetch_meteo(), day)
        sujets = [s["titre"] for _, sujets_rub in sections for s in sujets_rub]
        write_site(out_dir, day, title, page, sujets, precedent)
        log(f"Écrit : {args.out}/{day:%Y-%m-%d}.html")
    elif args.cmd == "send":
        title = args.title or f"Journal du {date_fr(datetime.now(PARIS).date())}"
        send_to_instapaper(args.url, title)


if __name__ == "__main__":
    main()
