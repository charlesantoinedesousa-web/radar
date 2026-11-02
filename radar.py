#!/usr/bin/env python3
"""RADAR - détecte les sujets qui montent et propose des angles de rebond créatif.

Usage:
  python radar.py            # collecte + synthèse Claude + dashboard (output/index.html)
  python radar.py --raw      # collecte seule (sans clé API), dashboard des signaux bruts
  python radar.py --demo     # dashboard avec fiches d'exemple (hors ligne)
Env: GEMINI_API_KEY (gratuit) ou ANTHROPIC_API_KEY, RADAR_MODEL,
     RADAR_WEBHOOK (Slack/Discord/Teams-compatible, alerte sur les pics), RADAR_CONTACT
"""
import argparse, datetime as dt, html, json, math, os, re, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "output"
UA = f"radar/0.1 ({os.environ.get('RADAR_CONTACT', 'contact-non-renseigne')})"
SKIP_PREFIX = ("Spécial:", "Wikipédia:", "Portail:", "Catégorie:", "Aide:", "Fichier:",
               "Projet:", "Modèle:", "Discussion", "Utilisateur")
SKIP_TITLES = {"Wikipédia:Accueil_principal", "Accueil", "Main_Page", "-"}


def get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def safe(fn, label):
    try:
        return fn()
    except Exception as e:  # une source en panne ne bloque pas les autres
        print(f"[!] {label}: {e}", file=sys.stderr)
        return []


# ---------- Collecteurs ----------
def wikipedia_spikes(lang="fr", days=8, min_views=3000, min_ratio=2.0, limit=25):
    """Pics = vues du dernier jour dispo / moyenne des jours précédents (top 1000 par jour)."""
    base = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{lang}.wikipedia/all-access"
    series = {}
    day = dt.date.today() - dt.timedelta(days=1)
    for _ in range(days + 2):
        if len(series) >= days:
            break
        try:
            data = json.loads(get(f"{base}/{day:%Y/%m/%d}"))
            arts = data["items"][0]["articles"]
            series[day] = {a["article"]: a["views"] for a in arts}
        except Exception:
            pass  # jour pas encore publié
        day -= dt.timedelta(days=1)
    if len(series) < 3:
        return []
    days_sorted = sorted(series, reverse=True)
    latest, prev = days_sorted[0], days_sorted[1:]
    floors = {d: min(series[d].values()) for d in prev}
    out = []
    for art, views in series[latest].items():
        if art in SKIP_TITLES or art.startswith(SKIP_PREFIX) or views < min_views:
            continue
        avg = sum(series[d].get(art, floors[d]) for d in prev) / len(prev)
        ratio = views / max(avg, 1)
        if ratio >= min_ratio:
            out.append({"source": f"Wikipédia {lang.upper()}", "title": art.replace("_", " "),
                        "url": f"https://{lang}.wikipedia.org/wiki/{art}",
                        "metric": f"{views:,} vues le {latest:%d/%m} (x{ratio:.1f} vs moyenne {int(avg):,})".replace(",", " "),
                        "score": ratio * math.log10(views)})
    return sorted(out, key=lambda x: -x["score"])[:limit]


def google_trends(geo="FR", limit=20):
    root = ET.fromstring(get(f"https://trends.google.com/trending/rss?geo={geo}"))
    out = []
    for it in root.iter("item"):
        f = {c.tag.split("}")[-1]: (c.text or "") for c in it}
        news = [n for n in it if n.tag.endswith("news_item")]
        headline = ""
        if news:
            headline = next((c.text for c in news[0] if c.tag.endswith("news_item_title")), "") or ""
        out.append({"source": "Google Trends FR", "title": f.get("title", ""),
                    "url": f"https://trends.google.com/trending?geo={geo}",
                    "metric": f"{f.get('approx_traffic', '?')} recherches",
                    "note": html.unescape(headline)})
    return out[:limit]


def google_news(limit=25):
    root = ET.fromstring(get("https://news.google.com/rss?hl=fr&gl=FR&ceid=FR:fr"))
    return [{"source": "Google Actualités FR", "title": html.unescape(i.findtext("title", "")),
             "url": i.findtext("link", ""), "metric": ""} for i in root.iter("item")][:limit]


def hacker_news(limit=20):
    d = json.loads(get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"))
    return [{"source": "Hacker News", "title": h["title"],
             "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
             "metric": f"{h.get('points', 0)} pts, {h.get('num_comments', 0)} commentaires"}
            for h in d["hits"]][:limit]


def collect():
    signals = []
    for fn, label in [(wikipedia_spikes, "wikipedia"), (google_trends, "google_trends"),
                      (google_news, "google_news"), (hacker_news, "hacker_news")]:
        signals += safe(fn, label)
    for i, s in enumerate(signals):
        s["id"] = i
    return signals


# ---------- Synthèse Claude ----------
SYSTEM = """Tu es l'assistant de veille d'un directeur de création en agence. Il cherche des sujets d'actualité et des tendances \
sur lesquels les marques peuvent proposer des "rebonds" créatifs (réactifs ou de fond).
À partir des SIGNAUX fournis (pics Wikipédia, Google Trends, actus, Hacker News), produis 8 à 10 fiches. Mélange :
rebonds chauds (à saisir sous 24-72h) et tendances de fond (sujets de société/usage/esthétique qui émergent).
Regroupe les signaux qui parlent du même sujet. Ignore le bruit (sport de routine, people sans angle, pages techniques).
RÈGLES STRICTES :
- N'invente AUCUN chiffre ni source. "preuve" ne contient que des chiffres présents dans les signaux (reprends-les tels quels), sinon null.
- "signal_ids" liste les id des signaux utilisés. "sources" = noms de ces sources.
- "a_verifier" : ce qu'il faudrait confirmer avant de le présenter (contexte que tu supposes sans le voir dans les signaux).
- "rebondable" = false si le sujet est un deuil, un drame, une catastrophe, un sujet judiciaire/politique clivant : explique dans "risque". Sinon "risque" = brève précaution de ton.
- Angles : concrets, avec un message de marque rédigé (pas une généralité). 2 à 3 angles par fiche.
Réponds UNIQUEMENT en JSON : {"fiches":[{"titre","categorie","urgence":"chaud|montant|fond","signal","statut","sources":[],"signal_ids":[],"preuve":null,"rebondable":true,"risque":"","a_verifier":"","angles":[{"pour_qui":"","message":""}]}]}"""


def call_llm(user_msg):
    """Gemini (gratuit) si GEMINI_API_KEY, sinon Claude si ANTHROPIC_API_KEY."""
    gem, ant = os.environ.get("GEMINI_API_KEY"), os.environ.get("ANTHROPIC_API_KEY")
    if gem:
        def ask(model):
            body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                    "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                    "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 12000}}
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                json.dumps(body).encode(), {"content-type": "application/json", "x-goog-api-key": gem})
            with urllib.request.urlopen(req, timeout=180) as r:
                parts = json.load(r)["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        def ask_retry(model, waits=(15, 45, 90)):
            for i in range(len(waits) + 1):
                try:
                    return ask(model)
                except urllib.error.HTTPError as e:
                    if e.code not in (429, 500, 502, 503, 504) or i == len(waits):
                        raise
                    print(f"[!] {model} saturé ({e.code}), nouvel essai dans {waits[i]}s", file=sys.stderr)
                    time.sleep(waits[i])

        def others(exclude):  # autres modèles flash stables, du plus récent au plus ancien
            req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                         headers={"x-goog-api-key": gem})
            ms = json.load(urllib.request.urlopen(req, timeout=30))["models"]
            names = sorted((m["name"].split("/")[-1] for m in ms
                            if "flash" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])
                            and not re.search(r"image|tts|live|preview|exp|thinking", m["name"])), reverse=True)
            return [n for n in names if n != exclude]

        model = os.environ.get("RADAR_MODEL", "gemini-3.8-flash")
        try:
            return ask_retry(model)
        except urllib.error.HTTPError as e:
            if e.code not in (404, 429, 500, 502, 503, 504):
                raise
            err = e
        for alt in others(model)[:3]:
            print(f"[!] modèle {model} indisponible, essai avec {alt}", file=sys.stderr)
            try:
                return ask_retry(alt, waits=(20,))
            except urllib.error.HTTPError as e:
                err = e
        raise err
    if ant:
        body = {"model": os.environ.get("RADAR_MODEL", "claude-sonnet-5-5"), "max_tokens": 8000,
                "system": SYSTEM, "messages": [{"role": "user", "content": user_msg}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps(body).encode(),
                                     {"content-type": "application/json", "x-api-key": ant,
                                      "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return "".join(b.get("text", "") for b in json.load(r)["content"])
    sys.exit("Aucune clé (GEMINI_API_KEY gratuite, ou ANTHROPIC_API_KEY). Sinon lance avec --raw / --demo.")


def synthesize(signals):
    lines = [f"[{s['id']}] ({s['source']}) {s['title']} | {s['metric']} {('| ' + s['note']) if s.get('note') else ''}"
             for s in signals]
    text = call_llm(f"Date : {dt.date.today():%d/%m/%Y}\nSIGNAUX :\n" + "\n".join(lines))
    fiches = json.loads(text[text.index("{"): text.rindex("}") + 1])["fiches"]
    by_id = {s["id"]: s for s in signals}
    for f in fiches:  # liens réels reconduits depuis les signaux, jamais générés
        f["liens"] = [{"label": by_id[i]["title"], "url": by_id[i]["url"]}
                      for i in f.get("signal_ids", []) if i in by_id][:4]
    return fiches


# ---------- Sortie ----------
def notify(signals):
    hook = os.environ.get("RADAR_WEBHOOK")
    top = [s for s in signals if s["source"].startswith("Wikip")][:5]
    if not hook or not top:
        return
    text = "🔥 RADAR - ça monte :\n" + "\n".join(f"• {s['title']} - {s['metric']}\n  {s['url']}" for s in top)
    req = urllib.request.Request(hook, json.dumps({"text": text}).encode(), {"content-type": "application/json"})
    safe(lambda: urllib.request.urlopen(req, timeout=15).read() and [], "webhook")


def render(payload):
    OUT.mkdir(exist_ok=True)
    (OUT / "history").mkdir(exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False)
    (OUT / "latest.json").write_text(data, encoding="utf-8")
    (OUT / "history" / f"{dt.date.today():%Y-%m-%d}.json").write_text(data, encoding="utf-8")
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    (OUT / "index.html").write_text(tpl.replace("/*DATA*/null", data.replace("</", "<\\/")), encoding="utf-8")
    print(f"Dashboard : {OUT / 'index.html'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true", help="signaux bruts, sans Claude")
    ap.add_argument("--demo", action="store_true", help="fiches d'exemple hors ligne")
    a = ap.parse_args()
    now = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    if a.demo:
        fiches = json.loads((HERE / "demo.json").read_text(encoding="utf-8"))
        return render({"mode": "demo", "generated": now, "fiches": fiches, "signals": []})
    signals = collect()
    print(f"{len(signals)} signaux collectés : " + ", ".join(sorted({x['source'] for x in signals})))
    if not signals:
        sys.exit("Aucun signal : vérifie ta connexion / les accès réseau.")
    notify(signals)
    fiches = [] if a.raw else synthesize(signals)
    render({"mode": "raw" if a.raw else "live", "generated": now, "fiches": fiches, "signals": signals})


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:  # erreur API lisible dans les logs
        sys.exit(f"ERREUR API {e.code} sur {e.url.split('?')[0]} : {e.read().decode(errors='replace')[:600]}")
