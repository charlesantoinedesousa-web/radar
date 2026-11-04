#!/usr/bin/env python3
"""RADAR - détecte les sujets qui montent et propose des angles de rebond créatif.

Usage:
  python radar.py            # collecte + synthèse Claude + dashboard (output/index.html)
  python radar.py --raw      # collecte seule (sans clé API), dashboard des signaux bruts
  python radar.py --demo     # dashboard avec fiches d'exemple (hors ligne)
Env: GEMINI_API_KEY (gratuit) ou ANTHROPIC_API_KEY, RADAR_MODEL,
     RADAR_WEBHOOK (Slack/Discord/Teams-compatible, alerte sur les pics), RADAR_CONTACT
"""
import argparse, datetime as dt, threading, html, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "docs"  # dossier publié par GitHub Pages
UA = f"radar/0.1 ({os.environ.get('RADAR_CONTACT', 'contact-non-renseigne')})"
SKIP_PREFIX = ("Special:", "Wikipedia:", "Portal:", "Category:", "Help:", "File:", "Template:", "Talk:", "User:", "Deaths_in", "List_of",
               "Spécial:", "Wikipédia:", "Portail:", "Catégorie:", "Aide:", "Fichier:",
               "Projet:", "Modèle:", "Discussion", "Utilisateur")
SKIP_TITLES = {"Wikipédia:Accueil_principal", "Accueil", "Main_Page", "-"}


_GNEWS = threading.BoundedSemaphore(2)


def get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    if "news.google.com" not in url:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    with _GNEWS:  # Google Actualités refuse (503/429) les rafales de requêtes
        for i, wait in enumerate((0.6, 4, 10)):
            time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503) or i == 2:
                    raise


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
        out.append({"source": f"Google Trends {geo}", "title": f.get("title", ""),
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


def feeds(max_age_h=72, per_feed=3):
    """Articles récents des flux de feeds.json (modifiable). Les flux en panne sont ignorés."""
    cfg = json.loads((HERE / "feeds.json").read_text(encoding="utf-8"))
    nl = HERE / "newsletters.txt"  # une newsletter par ligne : « Nom | https://adresse »
    if nl.exists():
        for line in nl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "|" in line:
                name, url = (x.strip() for x in line.split("|", 1))
                if url.startswith("http"):
                    cfg.append({"name": name, "home": url, "n": 2, "max_age_h": 240})
    now = dt.datetime.now(dt.timezone.utc)
    out, ok, ko = [], [], []

    def parse(url):
        root = ET.fromstring(get(url, timeout=15))
        if not any(e.tag.split("}")[-1] in ("item", "entry") for e in root.iter()):
            raise ValueError("flux vide")
        return root

    def load(f):
        home = (f.get("home") or "").rstrip("/")
        cands = [f["url"]] if f.get("url") else [home + p for p in ("/feed", "/rss/", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")]
        for u in cands:  # pour une adresse d'accueil : on essaie les chemins de flux usuels (Substack, Ghost, WordPress...)
            try:
                return parse(u)
            except Exception:
                pass
        site = f.get("site") or (urllib.parse.urlparse(home).netloc.removeprefix("www.") if home else "")
        if site:  # flux bloqué ou introuvable : on passe par l'index Google Actualités du site
            q = urllib.parse.quote_plus(f"site:{site} when:14d")
            try:
                return parse(f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr")
            except Exception:
                pass
        return None

    with ThreadPoolExecutor(max_workers=12) as ex:
        roots = list(ex.map(load, cfg))
    for f, root in zip(cfg, roots):
        if root is None:
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
            if n >= f.get("n", per_feed):
                break
        ok.append(f["name"])
    print(f"Flux : {len(ok)} OK, {len(ko)} en échec" + (f" ({', '.join(ko)})" if ko else ""), file=sys.stderr)
    return sorted(out, key=lambda x: x["age"])


def collect():
    signals = []
    for fn, label in [(wikipedia_spikes, "wikipedia"), (google_trends, "google_trends"),
                      (lambda: wikipedia_spikes("en", min_views=40000, limit=10), "wikipedia_en"),
                      (lambda: google_trends("US", 8), "trends_us"), (lambda: google_trends("GB", 8), "trends_gb"),
                      (lambda: google_trends("JP", 6), "trends_jp"), (lambda: google_trends("DE", 6), "trends_de"),
                      (lambda: google_trends("BR", 6), "trends_br"),
                      (google_news, "google_news"), (hacker_news, "hacker_news"), (feeds, "flux")]:
        signals += safe(fn, label)
    signals = signals[:400]
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
SOURCES : privilégie les sujets confirmés par au moins 2 signaux de sources différentes (sinon dis-le dans "a_verifier"). Au moins 4 fiches doivent \
venir de la culture, de la tech underground ou des communautés (Dazed, Dezeen, Hackaday, Know Your Meme, Mastodon, Lemmy, newsletters, Hacker News) \
et non des seules actualités généralistes. Quand un signal de type « GActu · sondages / études / baromètres » cite un chiffre, un institut ou une enquête, \
utilise-le comme "preuve" en nommant l'institut tel qu'il apparaît dans le titre (jamais d'autre).
PRIORITÉ FRANCE : une marque française doit pouvoir s'en servir. Au moins 6 fiches sur 10 s'appuient sur un signal français ou européen ; \
pour un signal venu de l'étranger, dis dans "a_verifier" s'il est déjà visible en France.
URGENCE : uniquement "chaud", "montant" ou "fond" (aucune autre valeur).
FICHES JAMAIS VIDES : n'écris une fiche que si le sujet est rebondable. Un sujet sensible (deuil, drame, hack, crise, polémique) ne fait PAS \
partie des 10 fiches : mets-le dans "ca_monte".
"ca_monte" = 6 à 8 sujets qui MONTENT aujourd'hui, repérés surtout via les pics Wikipédia, Google Trends et Google Actualités \
(événements, personnalités, sorties, polémiques, météo, sport, culture). Pour chacun : "pourquoi" (la cause, uniquement si un signal l'établit, \
sinon écris exactement "cause non identifiée dans les signaux"), "mesure" (chiffre repris d'un signal, ex. "x10 vs moyenne"), "rebondable" (booléen), \
"precaution" (ton à tenir ou raison de s'abstenir) et "idee" (une piste de rebond d'une phrase, null si non rebondable).
"chiffres" = 5 à 8 chiffres (un seul par signal, pas de doublon), études ou sondages du jour repris TELS QUELS des signaux (jamais reformulés en un autre chiffre) : \
"chiffre" (ex. "1 %"), "phrase" (ce qu'il mesure, d'après le signal), "source" (institut ou média tel qu'il apparaît dans le signal), "signal_ids". \
Si moins de 5 chiffres figurent dans les signaux, n'en liste que ce qui existe.
STYLE : français correct et sobre. Jamais de mot inventé, de métaphore physique (« s'arrachent les cheveux ») ni d'emphase (« colère épidermique »). \
L'insight doit se lire comme une phrase naturelle : la structure comportement / tension / contraste est implicite, pas récitée mot pour mot.
INSIGHT : chaque fiche a un champ "insight" : UNE phrase du type « Les gens [comportement/ressenti], parce que [tension], alors que [ce que les marques font encore] ». \
L'angle et le message en découlent directement ; si tu n'arrives pas à formuler l'insight, n'écris pas la fiche.
CHIFFRES UTILES : ne retiens dans "chiffres" que ceux qui éclairent consommation, usages, marques, tech, travail, médias ou culture. Écarte les chiffres \
de faits divers, de santé grave, de suicide, de fin de vie, de justice ou de politique partisane.
ANGLES : un angle part d'une tension réelle et prend position (« ce que la marque dit que les autres n'osent pas »). Interdit : slogans génériques, \
promesses vagues (« infiniment plus de goût »), jeux de mots creux. Le message doit pouvoir être affiché tel quel, en une à deux phrases.
Réponds UNIQUEMENT en JSON : {"chiffres":[{"chiffre":"","phrase":"","source":"","signal_ids":[]}],"ca_monte":[{"sujet":"","pourquoi":"","mesure":"","rebondable":true,"precaution":"","idee":null,"signal_ids":[]}],"fiches":[{"titre","categorie","insight","urgence":"chaud|montant|fond","signal","statut","sources":[],"signal_ids":[],"preuve":null,"rebondable":true,"risque":"","a_verifier":"","angles":[{"pour_qui":"","message":""}]}]}"""


def _gemini(user_msg, system, gem, PATIENCE, ALTS):
    def ask(model):
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 12000}}
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            json.dumps(body).encode(), {"content-type": "application/json", "x-goog-api-key": gem})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                parts = json.load(r)["candidates"][0]["content"]["parts"]
        except urllib.error.HTTPError as e:
            e.body = e.read().decode(errors="replace")
            raise
        return "".join(p.get("text", "") for p in parts)
    def transient(e):  # saturation, coupure ou délai dépassé : on peut réessayer
        if isinstance(e, urllib.error.HTTPError):
            if e.code == 429 and re.search(r"per ?day", getattr(e, "body", ""), re.I):
                return False  # quota du jour épuisé : attendre ne sert à rien
            return e.code in (429, 500, 502, 503, 504)
        return isinstance(e, OSError)

    def ask_retry(model, waits=PATIENCE):
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
        names = [m["name"].split("/")[-1] for m in ms
                 if "flash" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])
                 and not re.search(r"image|tts|live|preview|exp|thinking|omni|audio|robotics|embed", m["name"])]
        full = sorted((n for n in names if "lite" not in n), reverse=True)  # les « lite » écrivent moins bien : en dernier
        return [n for n in full + sorted(n for n in names if "lite" in n) if n != exclude]

    model = os.environ.get("RADAR_MODEL", "gemini-3.8-flash")
    try:
        return ask_retry(model)
    except (urllib.error.URLError, OSError) as e:
        if not (transient(e) or getattr(e, "code", None) in (404, 429)):
            raise
        err = e
    for alt in others(model)[:ALTS]:
        print(f"[!] modèle {model} indisponible, essai avec {alt}", file=sys.stderr)
        try:
            return ask_retry(alt, waits=(30, 90))
        except (urllib.error.URLError, OSError) as e:
            err = e
    raise err


def _mistral(user_msg, system, key):
    body = {"model": os.environ.get("RADAR_MISTRAL_MODEL", "mistral-large-latest"), "temperature": 0.4, "max_tokens": 9000,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]}
    for i, wait in enumerate((0, 15, 45, 90)):
        time.sleep(wait)
        req = urllib.request.Request("https://api.mistral.ai/v1/chat/completions", json.dumps(body).encode(),
                                     {"content-type": "application/json", "authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == 3:
                raise
            print(f"[!] Mistral : {e.code}, nouvel essai", file=sys.stderr)
        except OSError:
            if i == 3:
                raise
            print("[!] Mistral : coupure, nouvel essai", file=sys.stderr)


def _claude(user_msg, system, key):
    body = {"model": os.environ.get("RADAR_CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 8000,
            "system": system, "messages": [{"role": "user", "content": user_msg}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps(body).encode(),
                                 {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=240) as r:
        return "".join(b.get("text", "") for b in json.load(r)["content"])


def call_llm(user_msg, system=None):
    """Essaie les fournisseurs dans l'ordre : Gemini (gratuit), Mistral (gratuit), Claude (payant). Passe au suivant en cas d'échec."""
    system = system or SYSTEM
    env = os.environ.get
    chain = []
    if env("GEMINI_API_KEY"):
        chain.append(("Gemini", lambda p: _gemini(user_msg, system, env("GEMINI_API_KEY"), *p)))
    if env("MISTRAL_API_KEY"):
        chain.append(("Mistral", lambda p: _mistral(user_msg, system, env("MISTRAL_API_KEY"))))
    if env("ANTHROPIC_API_KEY"):
        chain.append(("Claude", lambda p: _claude(user_msg, system, env("ANTHROPIC_API_KEY"))))
    if not chain:
        sys.exit("Aucune clé (GEMINI_API_KEY, MISTRAL_API_KEY ou ANTHROPIC_API_KEY). Sinon lance avec --raw / --demo.")
    err = None
    for i, (name, fn) in enumerate(chain):
        last = i == len(chain) - 1
        patience, alts = ((20, 60, 120, 180), 3) if last else ((15, 45), 1)  # un secours existe : on n'insiste pas
        try:
            return fn((patience, alts))
        except (urllib.error.URLError, OSError) as e:
            err = e
            print(f"[!] {name} indisponible ({getattr(e, 'code', type(e).__name__)})" + ("" if last else ", bascule sur le fournisseur suivant"), file=sys.stderr)
    raise err


def recent_topics(days=3):
    """Titres des derniers jours (output/history) pour éviter de répéter les mêmes sujets."""
    out = []
    for p in sorted((OUT / "history").glob("*.json"))[-(days + 1):]:
        if days > 0 and p.stem == f"{dt.date.today():%Y-%m-%d}":
            continue
        if days == 0 and p.stem != f"{dt.date.today():%Y-%m-%d}":
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out += [f["titre"] for f in d.get("fiches", [])] + [m["sujet"] for m in d.get("ca_monte", [])]
        except Exception:
            pass
    return out


SELECT = """Tu es le rédacteur en chef d'une revue de presse pour un directeur de création en agence. À partir des SIGNAUX, tu SÉLECTIONNES les sujets ; \
un rédacteur écrira ensuite les fiches. Choisis :
- 10 sujets de fiches (environ 4 liés à l'actualité chaude, 6 frictions / contre-tendances / signaux) ;
- 7 sujets « ça monte aujourd'hui » (pics Wikipédia, Google Trends, Google Actualités : événements, personnalités, sorties, polémiques) ;
- 8 signaux contenant un chiffre, une étude ou un sondage utiles (consommation, usages, marques, tech, travail, médias, culture ; jamais faits divers, \
santé grave, suicide, fin de vie, justice, politique partisane).
CRITÈRES : zéro marronnier déjà rincé ; priorité aux frictions (agacement, saturation, rejet) et aux contrecoups ; regroupe les signaux d'un même sujet ; \
privilégie les sujets confirmés par au moins 2 sources différentes ; au moins 4 sujets venus de la culture, de la tech underground ou des communautés \
(pas seulement des actualités généralistes) ; au moins 6 sujets français ou européens ; ne répète pas les sujets déjà traités ; \
les sujets sensibles (deuil, drame, crime, hack, polémique clivante) vont dans « ca_monte », jamais dans « fiches ».
ÉQUILIBRE OBLIGATOIRE DES 10 FICHES : pas plus de 3 sujets tech/IA/numérique ; au moins 1 sujet food/lifestyle, 1 mode/design/esthétique, \
1 culture/musique/mèmes, 1 travail/société, 1 consommation/retail. Pour les 8 chiffres : un seul chiffre par signal, des chiffres français ou \
européens de préférence.
Réponds UNIQUEMENT en JSON : {"fiches":[{"sujet":"","signal_ids":[],"urgence":"chaud|montant|fond"}],"ca_monte":[{"sujet":"","signal_ids":[]}],"chiffres":[{"signal_ids":[]}]}"""

MONTE = """Tu mets à jour la rubrique « Ça monte aujourd'hui » d'une revue de presse pour un directeur de création. À partir des SIGNAUX (pics Wikipédia, \
Google Trends, Google Actualités, communautés), liste 6 à 8 sujets qui MONTENT maintenant (événements, personnalités, sorties, polémiques, météo, sport, culture). \
Pour chacun : "sujet", "pourquoi" (la cause, uniquement si un signal l'établit, sinon écris exactement "cause non identifiée dans les signaux"), \
"mesure" (chiffre repris d'un signal), "rebondable" (booléen ; false si deuil, drame, crime, catastrophe, polémique clivante), \
"precaution" (ton à tenir ou raison de s'abstenir), "idee" (une piste de rebond d'une phrase, null si non rebondable), "signal_ids". \
N'invente aucun chiffre ni cause. Ne répète pas les sujets déjà traités ce matin sauf évolution notable.
Réponds UNIQUEMENT en JSON : {"ca_monte":[{"sujet":"","pourquoi":"","mesure":"","rebondable":true,"precaution":"","idee":null,"signal_ids":[]}]}"""


SENSIBLE = re.compile(r"\b(mort|morts|décès|décédé|meurt|disparition d[eu]|hommage national|deuil|attentat|attaque|fusillade|crash|accident|tué|tués|victimes?|suicide|guerre|massacre|incendie meurtrier)\b", re.I)


def _securise(monte):
    """Filet de sécurité : un sujet de deuil / drame n'est jamais « rebondable », quoi qu'ait répondu le modèle."""
    for m in monte:
        if SENSIBLE.search(f"{m.get('sujet', '')} {m.get('pourquoi', '')}"):
            m["rebondable"], m["idee"] = False, None
            m["precaution"] = m.get("precaution") or "Sujet sensible : ne pas rebondir."
    return monte


def _dedoublonne(chiffres):
    seen, out = set(), []
    for c in chiffres:
        k = re.sub(r"\W+", " ", (c.get("phrase") or "").lower())[:60]
        if k and k not in seen:
            seen.add(k)
            out.append(c)
    return out[:8]


def _lines(signals):
    return [f"[{s['id']}] ({s['source']}) {s['title'][:140]} | {s['metric']} {('| ' + s['note'][:130]) if s.get('note') else ''}" for s in signals]


def _json(prompt, system):
    for attempt in (1, 2, 3):
        text = call_llm(prompt, system)
        try:
            return json.loads(text[text.index("{"): text.rindex("}") + 1])
        except ValueError:
            if attempt == 3:
                raise
            print("[!] réponse du modèle incomplète, nouvel essai", file=sys.stderr)


def _liens(by_id, ids):  # liens réels reconduits depuis les signaux, jamais générés
    return [{"label": f"{by_id[i]['title'][:70]} ({by_id[i]['source']}{', ' + by_id[i]['metric'] if by_id[i].get('age') is not None else ''})",
             "url": by_id[i]["url"]} for i in ids if i in by_id][:4]


def synthesize_monte(signals):
    by_id = {s["id"]: s for s in signals}
    seen = recent_topics(0)
    prompt = (f"Date : {dt.datetime.now():%d/%m/%Y %H:%M}\n"
              + (("SUJETS DÉJÀ TRAITÉS : " + " ; ".join(seen) + "\n") if seen else "") + "SIGNAUX :\n" + "\n".join(_lines(signals)))
    monte = _json(prompt, MONTE).get("ca_monte", [])
    for m in _securise(monte):
        m["liens"] = _liens(by_id, m.get("signal_ids", []))
    return monte


def synthesize(signals):
    by_id = {s["id"]: s for s in signals}
    seen = recent_topics()
    head = (f"Date : {dt.date.today():%d/%m/%Y}\n"
            + (("SUJETS DÉJÀ TRAITÉS CES DERNIERS JOURS (ne les répète pas, sauf évolution notable) : " + " ; ".join(seen) + "\n") if seen else ""))
    # passe 1 : sélection des sujets (réponse courte, ids seulement)
    sel = None
    try:
        sel = _json(head + "SIGNAUX :\n" + "\n".join(_lines(signals)), SELECT)
        ids = {i for k in ("fiches", "ca_monte", "chiffres") for it in sel.get(k, []) for i in it.get("signal_ids", []) if i in by_id}
        if len(sel.get("fiches", [])) < 5 or not ids:
            sel = None
    except Exception as e:
        print(f"[!] sélection impossible ({e}), passage en une seule étape", file=sys.stderr)
    # passe 2 : rédaction à partir des seuls signaux retenus
    if sel:
        subset = [by_id[i] for i in sorted(ids)]
        prompt = (head + "SÉLECTION DU RÉDACTEUR EN CHEF (écris exactement une fiche par sujet de \"fiches\", une entrée \"ca_monte\" par sujet de "
                  "\"ca_monte\", et extrais les \"chiffres\" des signaux listés ; écarte un sujet seulement si tu ne peux pas formuler son insight) :\n"
                  + json.dumps(sel, ensure_ascii=False) + "\nSIGNAUX RETENUS :\n" + "\n".join(_lines(subset)))
    else:
        prompt = head + "SIGNAUX :\n" + "\n".join(_lines(signals))
    data = _json(prompt, SYSTEM)
    fiches, monte, chiffres = data.get("fiches", []), data.get("ca_monte", []), data.get("chiffres", [])

    keep = []
    for f in fiches:
        if f.get("urgence") not in ("chaud", "montant", "fond"):
            f["urgence"] = "montant"
        f["liens"] = _liens(by_id, f.get("signal_ids", []))
        if f.get("angles") and not SENSIBLE.search(f.get("titre", "")):
            keep.append(f)
        else:  # fiche sans angle : bascule dans « Ça monte »
            monte.append({"sujet": f.get("titre", ""), "pourquoi": f.get("signal", ""), "mesure": f.get("preuve") or "",
                          "rebondable": False, "precaution": f.get("risque", ""), "idee": None,
                          "signal_ids": f.get("signal_ids", [])})
    monte, chiffres = _securise(monte), _dedoublonne(chiffres)
    for m in monte + chiffres:
        m["liens"] = _liens(by_id, m.get("signal_ids", []))
    return keep, monte, chiffres


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
    (OUT / "archive").mkdir(exist_ok=True)
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    today = f"{dt.date.today():%Y-%m-%d}"
    payload["archive"] = sorted({p.stem for p in (OUT / "archive").glob("*.html")} | {today}, reverse=True)[:60]
    data = json.dumps(payload, ensure_ascii=False)
    (OUT / "latest.json").write_text(data, encoding="utf-8")
    (OUT / "history" / f"{today}.json").write_text(data, encoding="utf-8")
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    page = tpl.replace("/*DATA*/null", data.replace("</", "<\\/"))
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / "archive" / f"{today}.html").write_text(page, encoding="utf-8")
    print(f"Site : {OUT / 'index.html'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true", help="signaux bruts, sans Claude")
    ap.add_argument("--demo", action="store_true", help="fiches d'exemple hors ligne")
    ap.add_argument("--si-absent", action="store_true", help="ne rien faire si l'édition du jour est déjà publiée")
    ap.add_argument("--monte", action="store_true", help="mise à jour de midi : seulement « Ça monte »")
    a = ap.parse_args()
    now = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    if a.demo:
        fiches = json.loads((HERE / "demo.json").read_text(encoding="utf-8"))
        return render({"mode": "demo", "generated": now, "fiches": fiches, "ca_monte": [], "chiffres": [], "feeds_ko": [], "signals": []})
    if a.si_absent and (OUT / "latest.json").exists():
        cur = json.loads((OUT / "latest.json").read_text(encoding="utf-8"))
        if cur.get("mode") == "live" and not cur.get("stale") and cur.get("generated", "")[:10] == f"{dt.date.today():%d/%m/%Y}":
            return print("Édition du jour déjà publiée : rien à faire.")
    prev = None
    if a.monte and (OUT / "latest.json").exists():
        prev = json.loads((OUT / "latest.json").read_text(encoding="utf-8"))
        if prev.get("mode") != "live" or prev.get("generated", "")[:10] != f"{dt.date.today():%d/%m/%Y}":
            prev = None  # pas d'édition du jour : on lance l'édition complète
    signals = collect()
    print(f"{len(signals)} signaux collectés : " + ", ".join(sorted({x['source'] for x in signals})))
    if not signals:
        sys.exit("Aucun signal : vérifie ta connexion / les accès réseau.")
    notify(signals)
    if prev:
        prev.update({"ca_monte": synthesize_monte(signals), "generated": now, "feeds_ko": FEEDS_KO, "signals": signals})
        return render(prev)
    try:
        fiches, monte, chiffres = ([], [], []) if a.raw else synthesize(signals)
    except Exception as e:
        print(f"::error::Génération impossible aujourd'hui ({type(e).__name__}: {str(e)[:300]} | {getattr(e, 'url', '').split('?')[0]})", file=sys.stderr)
        p = OUT / "latest.json"
        old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        if old and old.get("fiches"):
            old.update({"stale": f"La génération du {now} a échoué (modèle indisponible ou quota atteint) : édition précédente affichée.",
                        "feeds_ko": FEEDS_KO, "signals": signals})
            return render(old)
        fiches, monte, chiffres = [], [], []
    render({"mode": "raw" if a.raw else "live", "generated": now, "fiches": fiches, "ca_monte": monte, "chiffres": chiffres,
            "feeds_ko": FEEDS_KO, "signals": signals})


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:  # erreur API lisible dans les logs
        sys.exit(f"ERREUR API {e.code} sur {e.url.split('?')[0]} : {e.read().decode(errors='replace')[:600]}")
