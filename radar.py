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
from email.utils import parsedate_to_datetime
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


def _txt(el, *names):
    for n in names:
        for c in el:
            if c.tag.split("}")[-1] == n and (c.text or c.attrib.get("href")):
                return (c.text or "").strip() or c.attrib.get("href", "")
    return ""


def _when(s):
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


FEEDS_KO = []


def feeds(max_age_h=72, per_feed=4):
    """Articles récents des flux de feeds.json (modifiable). Les flux en panne sont ignorés."""
    cfg = json.loads((HERE / "feeds.json").read_text(encoding="utf-8"))
    now = dt.datetime.now(dt.timezone.utc)
    out, ok, ko = [], [], []
    for f in cfg:
        try:
            root = ET.fromstring(get(f["url"], timeout=15))
        except Exception:
            ko.append(f["name"])
            FEEDS_KO.append(f["name"])
            continue
        items = [e for e in root.iter() if e.tag.split("}")[-1] in ("item", "entry")]
        n = 0
        for it in items:
            when = _when(_txt(it, "pubDate", "published", "updated", "date"))
            if not when or (now - when).total_seconds() > f.get("max_age_h", max_age_h) * 3600:
                continue
            link = ""
            for c in it:
                if c.tag.split("}")[-1] == "link":
                    link = c.attrib.get("href") or (c.text or "").strip()
                    if link:
                        break
            desc = html.unescape(re.sub(r"<[^>]+>", " ", _txt(it, "description", "summary", "encoded", "content")))
            age = int((now - when).total_seconds() // 3600)
            out.append({"source": f["name"], "title": html.unescape(_txt(it, "title")), "url": link,
                        "metric": f"il y a {age} h", "note": " ".join(desc.split())[:220], "age": age})
            n += 1
            if n >= per_feed:
                break
        ok.append(f["name"])
    print(f"Flux : {len(ok)} OK, {len(ko)} en échec" + (f" ({', '.join(ko)})" if ko else ""), file=sys.stderr)
    return sorted(out, key=lambda x: x["age"])


def collect():
    signals = []
    for fn, label in [(wikipedia_spikes, "wikipedia"), (google_trends, "google_trends"),
                      (google_news, "google_news"), (hacker_news, "hacker_news"), (feeds, "flux")]:
        signals += safe(fn, label)
    signals = signals[:170]
    for i, s in enumerate(signals):
        s["id"] = i
    return signals


# ---------- Synthèse Claude ----------
SYSTEM = """Tu es l'assistant de veille d'un directeur de création en agence : une REVUE DE PRESSE quotidienne de signaux, \
frictions et contre-mouvements sur lesquels une marque peut rebondir en com. Il veut du récent, de l'actualité, de l'insight \
et du chiffre, pas des généralités.
À partir des SIGNAUX fournis (articles des dernières 72h, pics Wikipédia, Google Trends, Reddit, newsletters), produis 10 fiches.
MIX : environ 4 rebonds liés à l'actualité chaude, 6 signaux/frictions/contre-tendances. Équilibre : digital/tech, conso/retail, \
société/travail, esthétique/DA, culture web/mèmes, data/étude, écologie, food/lifestyle.
RÈGLES ÉDITORIALES :
- Zéro marronnier : écarte ce que la presse grand public a déjà rincé (ex. run clubs, matcha, quiet quitting classique) \
sauf si le signal du jour est le contrecoup (backlash) ou une évolution nette.
- Priorité à la friction : agacement, saturation, honte, rejet (tracking, QR codes, contenus IA, frais cachés, abonnements...).
- Regroupe les signaux qui parlent du même sujet. Ignore le bruit (sport de routine, faits divers sans angle, politique politicienne).
- Chaque fiche = une observation tirée des signaux + son angle de marque concret avec message rédigé entre guillemets.
RÈGLES STRICTES ANTI-INVENTION :
- N'invente AUCUN chiffre, étude, institut ni source. "preuve" ne contient que des chiffres présents dans les signaux (reprends-les tels quels, avec leur source), sinon null.
- "sources" = noms des sources des signaux utilisés ; "signal_ids" = leurs id. Ne cite jamais une source absente des signaux.
- "statut" : Émergent / Contre-tendance / Friction d'actualité / Micro-niche / Saturation (une de ces valeurs + courte précision).
- "a_verifier" : ce que tu supposes sans le voir dans les signaux, et le niveau de confiance si le signal est mince.
- "rebondable" = false si deuil, drame, catastrophe, sujet judiciaire ou politique clivant : explique dans "risque". Sinon "risque" = brève précaution de ton.
- Angles : 2 à 3 par fiche, avec "pour_qui" (secteurs) et "message" (accroche, dispositif ou action concrète).
PRIORITÉ FRANCE : une marque française doit pouvoir s'en servir. Au moins 6 fiches sur 10 s'appuient sur un signal français ou européen ; \
pour un signal venu de l'étranger, dis dans "a_verifier" s'il est déjà visible en France.
URGENCE : uniquement "chaud", "montant" ou "fond" (aucune autre valeur).
FICHES JAMAIS VIDES : n'écris une fiche que si le sujet est rebondable. Un sujet sensible (deuil, drame, hack, crise, polémique) ne fait PAS \
partie des 10 fiches : mets-le dans "ca_monte".
"ca_monte" = 6 à 8 sujets qui MONTENT aujourd'hui, repérés surtout via les pics Wikipédia, Google Trends et Google Actualités \
(événements, personnalités, sorties, polémiques, météo, sport, culture). Pour chacun : "pourquoi" (la cause, uniquement si un signal l'établit, \
sinon écris exactement "cause non identifiée dans les signaux"), "mesure" (chiffre repris d'un signal, ex. "x10 vs moyenne"), "rebondable" (booléen), \
"precaution" (ton à tenir ou raison de s'abstenir) et "idee" (une piste de rebond d'une phrase, null si non rebondable).
Réponds UNIQUEMENT en JSON : {"ca_monte":[{"sujet":"","pourquoi":"","mesure":"","rebondable":true,"precaution":"","idee":null,"signal_ids":[]}],"fiches":[{"titre","categorie","urgence":"chaud|montant|fond","signal","statut","sources":[],"signal_ids":[],"preuve":null,"rebondable":true,"risque":"","a_verifier":"","angles":[{"pour_qui":"","message":""}]}]}"""


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
            with urllib.request.urlopen(req, timeout=300) as r:
                parts = json.load(r)["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        def transient(e):  # saturation, coupure ou délai dépassé : on peut réessayer
            if isinstance(e, urllib.error.HTTPError):
                return e.code in (429, 500, 502, 503, 504)
            return isinstance(e, OSError)

        def ask_retry(model, waits=(15, 45, 90)):
            for i in range(len(waits) + 1):
                try:
                    return ask(model)
                except (urllib.error.URLError, OSError) as e:
                    if not transient(e) or i == len(waits):
                        raise
                    print(f"[!] {model} : {getattr(e, 'code', type(e).__name__)}, nouvel essai dans {waits[i]}s", file=sys.stderr)
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
        except (urllib.error.URLError, OSError) as e:
            if not (transient(e) or getattr(e, "code", None) == 404):
                raise
            err = e
        for alt in others(model)[:3]:
            print(f"[!] modèle {model} indisponible, essai avec {alt}", file=sys.stderr)
            try:
                return ask_retry(alt, waits=(20,))
            except (urllib.error.URLError, OSError) as e:
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
    prompt = f"Date : {dt.date.today():%d/%m/%Y}\nSIGNAUX :\n" + "\n".join(lines)
    for attempt in (1, 2):
        text = call_llm(prompt)
        try:
            data = json.loads(text[text.index("{"): text.rindex("}") + 1])
            break
        except ValueError:
            if attempt == 2:
                raise
            print("[!] réponse du modèle incomplète, nouvel essai", file=sys.stderr)
    fiches, monte = data.get("fiches", []), data.get("ca_monte", [])
    by_id = {s["id"]: s for s in signals}

    def liens(ids):  # liens réels reconduits depuis les signaux, jamais générés
        return [{"label": f"{by_id[i]['title'][:70]} ({by_id[i]['source']}{', ' + by_id[i]['metric'] if by_id[i].get('age') is not None else ''})",
                 "url": by_id[i]["url"]} for i in ids if i in by_id][:4]

    keep = []
    for f in fiches:
        if f.get("urgence") not in ("chaud", "montant", "fond"):
            f["urgence"] = "montant"
        f["liens"] = liens(f.get("signal_ids", []))
        if f.get("angles"):
            keep.append(f)
        else:  # fiche sans angle : bascule dans « Ça monte »
            monte.append({"sujet": f.get("titre", ""), "pourquoi": f.get("signal", ""), "mesure": f.get("preuve") or "",
                          "rebondable": False, "precaution": f.get("risque", ""), "idee": None,
                          "signal_ids": f.get("signal_ids", [])})
    for m in monte:
        m["liens"] = liens(m.get("signal_ids", []))
    return keep, monte


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
        return render({"mode": "demo", "generated": now, "fiches": fiches, "ca_monte": [], "feeds_ko": [], "signals": []})
    signals = collect()
    print(f"{len(signals)} signaux collectés : " + ", ".join(sorted({x['source'] for x in signals})))
    if not signals:
        sys.exit("Aucun signal : vérifie ta connexion / les accès réseau.")
    notify(signals)
    fiches, monte = ([], []) if a.raw else synthesize(signals)
    render({"mode": "raw" if a.raw else "live", "generated": now, "fiches": fiches, "ca_monte": monte,
            "feeds_ko": FEEDS_KO, "signals": signals})


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:  # erreur API lisible dans les logs
        sys.exit(f"ERREUR API {e.code} sur {e.url.split('?')[0]} : {e.read().decode(errors='replace')[:600]}")
