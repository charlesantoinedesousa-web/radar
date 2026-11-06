#!/usr/bin/env python3
"""RADAR - détecte les sujets qui montent et propose des angles de rebond créatif.

Usage:
  python radar.py            # collecte + synthèse Claude + dashboard (output/index.html)
  python radar.py --raw      # collecte seule (sans clé API), dashboard des signaux bruts
  python radar.py --demo     # dashboard avec fiches d'exemple (hors ligne)
Env: GEMINI_API_KEY (gratuit) ou ANTHROPIC_API_KEY, RADAR_MODEL,
     RADAR_WEBHOOK (Slack/Discord/Teams-compatible, alerte sur les pics), RADAR_CONTACT
"""
import argparse, types, datetime as dt, threading, html, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
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
                    cfg = [c for c in cfg if c["name"].lower() != name.lower()]  # pas de doublon avec feeds.json
                    cfg.append({"name": name, "home": url, "n": 2, "max_age_h": 240})
    now = dt.datetime.now(dt.timezone.utc)
    out, ok, ko = [], [], []

    BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
               "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"}

    def parse(url, headers=None):
        root = ET.fromstring(get(url, headers=headers, timeout=15))
        if not any(e.tag.split("}")[-1] in ("item", "entry") for e in root.iter()):
            raise ValueError("flux vide")
        return root

    def load(f):
        home = (f.get("home") or "").rstrip("/")
        cands = [f["url"]] if f.get("url") else [home + p for p in ("/feed", "/rss/", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")]
        for hdr in (None, BROWSER):  # d'abord notre identité, puis celle d'un navigateur
            for u in cands:  # pour une adresse d'accueil : on essaie les chemins de flux usuels (Substack, Ghost, WordPress...)
                try:
                    return parse(u, hdr)
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
                        "metric": f"il y a {age} h", "note": " ".join(desc.split())[:220], "age": age,
                        "cat": "lecture" if f.get("max_age_h", max_age_h) > 72 else "actu"})
            n += 1
            if n >= f.get("n", per_feed):
                break
        ok.append(f["name"])
    print(f"Flux : {len(ok)} OK, {len(ko)} en échec" + (f" ({', '.join(ko)})" if ko else ""), file=sys.stderr)
    return sorted(out, key=lambda x: x["age"])


CORE = ("Wikip", "Google Trends", "Google Actualités FR", "Hacker News")  # sources « ça monte » : toujours gardées en entier


def _plafonne(signals, cap):
    """Au-delà du plafond, on garde les sources « ça monte » en entier, puis on répartit le reste à tour de rôle entre les autres sources
    (le plus récent de chacune d'abord) : aucune newsletter n'est évincée parce qu'elle publie moins souvent."""
    if len(signals) <= cap:
        return signals
    core = [x for x in signals if x["source"].startswith(CORE)]
    groups = {}
    for x in signals:
        if not x["source"].startswith(CORE):
            groups.setdefault(x["source"], []).append(x)
    for g in groups.values():
        g.sort(key=lambda x: x.get("age", 0))
    out, i = list(core), 0
    while len(out) < cap and any(i < len(g) for g in groups.values()):
        for g in groups.values():
            if i < len(g) and len(out) < cap:
                out.append(g[i])
        i += 1
    return out


def collect():
    signals = []
    for fn, label in [(wikipedia_spikes, "wikipedia"), (google_trends, "google_trends"),
                      (lambda: wikipedia_spikes("en", min_views=40000, limit=10), "wikipedia_en"),
                      (lambda: google_trends("US", 8), "trends_us"), (lambda: google_trends("GB", 8), "trends_gb"),
                      (lambda: google_trends("JP", 6), "trends_jp"), (lambda: google_trends("DE", 6), "trends_de"),
                      (lambda: google_trends("BR", 6), "trends_br"),
                      (google_news, "google_news"), (hacker_news, "hacker_news"), (feeds, "flux")]:
        signals += safe(fn, label)
    signals = _plafonne(signals, 420)
    for i, s in enumerate(signals):
        s["id"] = i
    return signals


# ---------- Synthèse Claude ----------
SYSTEM = """Tu es l'assistant de veille d'un directeur de création en agence : une REVUE DE PRESSE quotidienne de signaux, \
frictions et contre-mouvements sur lesquels une marque peut rebondir en com. Il veut du récent, de l'actualité, de l'insight \
et du chiffre, pas des généralités.
À partir des SIGNAUX fournis (articles des dernières 72h, pics Wikipédia, Google Trends, Reddit, newsletters), produis 10 fiches en DEUX FAMILLES : 6 fiches "client" et 4 fiches "artefact3000" (voir FAMILLES plus bas).
MIX des 6 fiches client : environ 2 rebonds liés à l'actualité chaude, 4 signaux/frictions/contre-tendances. Équilibre : digital/tech, conso/retail, \
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
FAMILLES : chaque fiche porte "famille" = "client" ou "artefact3000".
- "client" (6 fiches) : un fait, une friction ou une contre-tendance utile à un ou plusieurs clients de l'agence ; champs "insight" et "angles" (règles ci-dessus).
- "artefact3000" (4 fiches) : « Artefact 3000 » est l'agence de publicité elle-même. Ces fiches proposent ce que l'agence pourrait sortir EN SON NOM, POUR LES GENS : \
un problème réel que des gens rencontrent à cause d'un événement du moment (grève, blocage, canicule, froid, hausse des prix, panne, réforme, échéance, rentrée, examens, pénurie, polémique...), \
résolu par une idée CRÉATIVE ET ORIGINALE, simple à réaliser vite. Ce peut être un outil, un objet, un affichage, un détournement, un service, un événement, un contenu : \
PAS forcément numérique. Exemple : des blocages privent des étudiants de cours -> une idée originale pour les aider à rattraper. \
Champs de ces fiches : "public" (qui est touché), "probleme" (ce qui leur manque, une phrase appuyée sur les signaux), "idee" (l'idée créative, une à trois phrases concrètes), \
"format" (outil, affichage, objet, événement, contenu, détournement...), "effort" ("léger" = quelques jours, "moyen" ou "lourd" : estimation à confirmer), \
"limites" (neutralité, droits, risques, ce qu'il ne faut pas promettre), "accroche" (le nom ou la baseline de l'idée, courte) et "originalite" (une phrase : ce qui la rend inattendue). \
Pas de "angles" pour ces fiches.
  SOLUTION ET FORME : ce qui compte d'abord, c'est une SOLUTION concrète et utile au problème des gens ; le directeur de création retravaillera la forme créative. \
Une solution pratique (guide, kit, outil, service) reste valable. Mais si une forme plus inattendue existe (un geste visible, un objet détourné, un dispositif dans la rue ou en ligne \
qu'on raconterait à des amis), préfère-la. Donne dans "piste_audacieuse" une SECONDE direction de forme, plus surprenante, en une à deux phrases : le directeur de création la prendra ou la laissera. \
Le titre de la fiche est court (8 mots maximum). L'idée ne reporte jamais l'effort sur les gens touchés (ne leur apprends pas à supporter une situation anormale) et ne désigne aucun coupable. \
"probleme" doit être appuyé sur un signal précis : cite dans "sources" les médias qui l'établissent ; si seul un signal indirect existe, écris-le dans "a_verifier". \
Si une marque cliente est concernée, vérifie que l'idée ne la met pas en porte-à-faux (ex. un fabricant de chauffage ne se moque pas d'un chauffage défaillant) et dis-le dans "limites".
  RÈGLES ARTEFACT 3000 : NON PARTISAN (aide les gens à s'adapter ou à s'en sortir, ne prend jamais parti pour ou contre un mouvement, un gouvernement ou une entreprise) ; \
jamais sur un drame, un deuil ou une catastrophe (pas d'exploitation) ; aucune donnée personnelle sensible ; aucun effet chiffré promis ; ne cite que des faits présents dans les signaux ; \
"a_verifier" liste ce qu'il faut confirmer (le besoin réel, la faisabilité, les droits).
CLIENTS : si une liste de clients figure dans le contexte, renseigne pour chaque fiche "clients" : les noms EXACTS des clients (pris dans la liste) pour qui le sujet ou l'idée est pertinent \
(secteur, thème, valeurs), [] si aucun. Favorise les sujets qui concernent au moins un client, sans écarter une bonne idée "artefact3000" qui n'en concerne aucun. \
Pour les associations et fondations de la liste, un sujet grave peut donner une fiche "client" seulement si l'idée est respectueuse et utile ; écris alors « sujet sensible : validation requise » dans "risque".
URGENCE : uniquement "chaud", "montant" ou "fond" (aucune autre valeur).
FICHES JAMAIS VIDES : n'écris une fiche que si le sujet est rebondable (une fiche client a des angles, une fiche artefact3000 a une idée). Un sujet sensible (deuil, drame, hack, crise, polémique) ne fait PAS \
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
Réponds UNIQUEMENT en JSON : {"chiffres":[{"chiffre":"","phrase":"","source":"","signal_ids":[]}],"ca_monte":[{"sujet":"","pourquoi":"","mesure":"","rebondable":true,"precaution":"","idee":null,"signal_ids":[]}],"fiches":[{"famille":"client|artefact3000","titre","categorie","insight","urgence":"chaud|montant|fond","signal","statut","sources":[],"signal_ids":[],"preuve":null,"rebondable":true,"risque":"","a_verifier":"","angles":[{"pour_qui":"","message":""}],"clients":[],"public":"","probleme":"","idee":"","accroche":"","originalite":"","piste_audacieuse":"","format":"","effort":"léger|moyen|lourd","limites":""}]}"""


DEAD = set()  # fournisseurs hors service pendant ce run
ERRORS = []   # détail lisible de chaque échec (affiché dans le bandeau du site)
FAILS = []    # nature des échecs de l'étape en cours : "quota", "zero", "size", "rate", "other"


def _nature(e):
    if isinstance(e, QuotaZero):
        return "zero"
    if isinstance(e, urllib.error.HTTPError):
        corps = getattr(e, "body", "") or ""
        if e.code == 413:
            return "size"
        if e.code == 429:
            return "quota" if re.search(r"per ?day|quota", corps, re.I) else "rate"
    return "other"


def _key(name):
    """Clé d'API nettoyée : un copier-coller ajoute souvent un espace ou un retour à la ligne invisible."""
    return (os.environ.get(name) or "").strip()


def providers_line():
    """Quels fournisseurs d'IA ont une clé ici (jamais la clé elle-même, seulement sa longueur)."""
    out = []
    for n, v in (("Gemini", "GEMINI_API_KEY"), ("Mistral", "MISTRAL_API_KEY"), ("Claude", "ANTHROPIC_API_KEY")):
        raw = os.environ.get(v) or ""
        k = raw.strip()
        out.append(f"{n} ✓ clé reçue ({len(k)} car.{', espaces parasites retirés' if k != raw else ''})" if k else f"{n} ✗ clé ABSENTE")
    return ", ".join(out)


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


class QuotaZero(Exception):
    """Le service annonce une limite de 0 requête par minute : la clé ne peut rien envoyer (offre non activée ou accès gratuit supprimé)."""


_MISTRAL_LAST = [0.0]


def _mistral(user_msg, system, key):
    models = [m.strip() for m in (os.environ.get("RADAR_MISTRAL_MODEL") or "mistral-small-latest,open-mistral-nemo").split(",") if m.strip()]
    err = None
    for model in models:  # un modèle absent de l'offre (403/404) : on essaie le suivant
        body = {"model": model, "temperature": 0.4, "max_tokens": 9000, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]}
        for i, wait in enumerate((0, 45)):
            gap = 50 - (time.time() - _MISTRAL_LAST[0])  # offre gratuite : limite de débit par minute, on espace les appels
            time.sleep(max(wait, gap if gap > 0 else 0))
            req = urllib.request.Request("https://api.mistral.ai/v1/chat/completions", json.dumps(body).encode(),
                                         {"content-type": "application/json", "authorization": f"Bearer {key}"})
            _MISTRAL_LAST[0] = time.time()
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.load(r)["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                err = e
                if e.code in (403, 404):
                    print(f"[!] Mistral : {model} non disponible dans ton offre ({e.code})", file=sys.stderr)
                    break
                if e.code == 429 and e.headers and str(e.headers.get("x-ratelimit-limit-req-minute", "")).strip() == "0":
                    raise QuotaZero("Mistral annonce une limite de 0 requête par minute pour cette clé : l'offre n'est probablement pas activée sur ce compte, "
                                    "ou l'accès gratuit à l'API n'existe plus. Inutile de réessayer.")
                if e.code not in (429, 500, 502, 503, 504) or i == 1:
                    raise
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra and ra.isdigit():
                    _MISTRAL_LAST[0] = time.time() + min(int(ra), 90) - 50  # on respecte le délai demandé par le service
                limites = {k: v for k, v in (e.headers.items() if e.headers else []) if "ratelimit" in k.lower() or "retry" in k.lower()}
                print(f"[!] Mistral : {e.code}, nouvel essai après une pause | requête : {len(user_msg)} car. | limites annoncées : {limites or 'aucune'}", file=sys.stderr)
            except OSError as e:
                err = e
                if i == 1:
                    raise
                print("[!] Mistral : coupure, nouvel essai", file=sys.stderr)
    raise err


def _claude(user_msg, system, key):
    body = {"model": os.environ.get("RADAR_CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 12000,
            "system": system, "messages": [{"role": "user", "content": user_msg}]}
    for i, wait in enumerate((0, 10, 40)):
        time.sleep(wait)
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps(body).encode(),
                                     {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                return "".join(b.get("text", "") for b in json.load(r)["content"])
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504, 529) or i == 2:
                raise
            print(f"[!] Claude : {e.code}, nouvel essai", file=sys.stderr)
        except OSError:
            if i == 2:
                raise
            print("[!] Claude : coupure, nouvel essai", file=sys.stderr)


def call_llm(user_msg, system=None):
    """Essaie les fournisseurs dans l'ordre : Gemini (gratuit), Mistral (gratuit), Claude (payant). Passe au suivant en cas d'échec."""
    system = system or SYSTEM
    env = os.environ.get
    makers = {
        "gemini": ("Gemini", _key("GEMINI_API_KEY"), lambda p: _gemini(user_msg, system, _key("GEMINI_API_KEY"), *p)),
        "mistral": ("Mistral", _key("MISTRAL_API_KEY"), lambda p: _mistral(user_msg, system, _key("MISTRAL_API_KEY"))),
        "claude": ("Claude", _key("ANTHROPIC_API_KEY"), lambda p: _claude(user_msg, system, _key("ANTHROPIC_API_KEY"))),
    }
    order = [k.strip() for k in (env("RADAR_ORDER") or "gemini,mistral,claude").split(",")]  # ex. RADAR_ORDER=mistral,gemini
    chain = [(makers[k][0], makers[k][2]) for k in order if k in makers and makers[k][1]]
    if not chain:
        raise RuntimeError("aucun fournisseur d'IA utilisable (clé absente ou absente de RADAR_ORDER)")
    chain = [c for c in chain if c[0] not in DEAD]  # un fournisseur déjà tombé dans ce run n'est pas réessayé
    if not chain:
        raise RuntimeError("tous les fournisseurs d'IA sont déjà tombés pendant ce run")
    err = None
    for i, (name, fn) in enumerate(chain):
        last = i == len(chain) - 1
        patience, alts = ((15, 45, 90), 2) if last else ((15, 45), 1)  # un secours existe : on n'insiste pas
        try:
            return fn((patience, alts))
        except Exception as e:  # toute erreur d'un fournisseur (réseau, clé refusée, réponse inattendue) : on passe au suivant
            err = e
            DEAD.add(name)
            FAILS.append(_nature(e))
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = getattr(e, "body", None) or e.read().decode(errors="replace")
                except Exception:
                    pass
            else:
                detail = str(e)
            for v in ("GEMINI_API_KEY", "MISTRAL_API_KEY", "ANTHROPIC_API_KEY"):
                if _key(v):
                    detail = detail.replace(_key(v), "***")
            ERRORS.append(f"{name} : {getattr(e, 'code', type(e).__name__)} {' '.join(detail.split())[:150]}".strip())
            print(f"[!] {name} indisponible ({getattr(e, 'code', type(e).__name__)}) {' '.join(detail.split())[:240]}"
                  + ("" if last else " -> bascule sur le fournisseur suivant"), file=sys.stderr)
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
- 6 sujets de fiches CLIENT (environ 2 liés à l'actualité chaude, 4 frictions / contre-tendances / signaux) ;
- 4 sujets de fiches ARTEFACT 3000 (ce que l'agence de publicité pourrait sortir en son nom POUR LES GENS) : un problème réel que des gens rencontrent à cause d'un événement du moment \
(grève, blocage, canicule, froid, hausse des prix, panne, réforme, échéance, examens, pénurie, polémique) et qui appelle une idée créative et originale, simple à réaliser vite, NON PARTISANE. \
Jamais un drame, un deuil ou une catastrophe. Cherche le problème pratique des gens, pas le débat politique ; privilégie les problèmes pour lesquels une solution concrète et utile existe ;
- 7 sujets « ça monte aujourd'hui » (pics Wikipédia, Google Trends, Google Actualités : événements, personnalités, sorties, polémiques) ;
- 8 signaux contenant un chiffre, une étude ou un sondage utiles (consommation, usages, marques, tech, travail, médias, culture ; jamais faits divers, \
santé grave, suicide, fin de vie, justice, politique partisane).
CRITÈRES : priorité aux sujets qui concernent au moins un client de la liste fournie dans le contexte (secteur, thème, valeurs) ; zéro marronnier déjà rincé ; priorité aux frictions (agacement, saturation, rejet) et aux contrecoups ; regroupe les signaux d'un même sujet ; \
privilégie les sujets confirmés par au moins 2 sources différentes ; au moins 4 sujets venus de la culture, de la tech underground ou des communautés \
(pas seulement des actualités généralistes) ; au moins 6 sujets français ou européens ; ne répète pas les sujets déjà traités ; \
les sujets sensibles (deuil, drame, crime, hack, polémique clivante) vont dans « ca_monte », jamais dans « fiches ».
ÉQUILIBRE OBLIGATOIRE DES 6 FICHES CLIENT : pas plus de 3 sujets tech/IA/numérique ; au moins 1 sujet food/lifestyle, 1 mode/design/esthétique, \
1 culture/musique/mèmes, 1 travail/société, 1 consommation/retail. Pour les 8 chiffres : un seul chiffre par signal, des chiffres français ou \
européens de préférence.
Réponds UNIQUEMENT en JSON : {"fiches":[{"sujet":"","signal_ids":[],"urgence":"chaud|montant|fond","famille":"client|artefact3000"}],"ca_monte":[{"sujet":"","signal_ids":[]}],"chiffres":[{"signal_ids":[]}]}"""

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


def _lines_courtes(signals):
    """Version compacte pour la sélection : titre + source (+ mesure pour les pics). Environ trois fois moins de texte."""
    out = []
    for s in signals:
        mesure = f" | {s['metric']}" if s["source"].startswith(("Wikip", "Google Trends")) else ""
        out.append(f"[{s['id']}] ({s['source']}) {s['title'][:115]}{mesure}")
    return out


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


IA_QUOTAS = {"Wikipédia FR": 12, "Wikipédia EN": 4, "Google Trends FR": 8, "Google Trends US": 3, "Google Trends GB": 3, "Google Trends JP": 2,
             "Google Trends DE": 2, "Google Trends BR": 2, "Google Actualités FR": 12, "Hacker News": 8}


def ia_subset(signals, cap):
    """Sous-ensemble équilibré envoyé à l'IA : quelques lignes de chaque source « ça monte », puis un tour de rôle entre les autres sources."""
    core, groups, used = [], {}, Counter()
    for x in signals:
        q = IA_QUOTAS.get(x["source"])
        if q is not None:
            if used[x["source"]] < q:
                used[x["source"]] += 1
                core.append(x)
        else:
            groups.setdefault(x["source"], []).append(x)
    for g in groups.values():
        g.sort(key=lambda x: x.get("age") if x.get("age") is not None else 24)
    out, i = core[:cap], 0
    while len(out) < cap and any(i < len(g) for g in groups.values()):
        for g in groups.values():
            if i < len(g) and len(out) < cap:
                out.append(g[i])
        i += 1
    return out


def _limite_taille():
    """Vrai si au moins un refus peut venir de la taille de la requête (413, ou 429 de débit) : alors on réessaie en plus petit.
    Un quota épuisé ou une limite à 0 n'a rien à voir avec la taille : inutile de réduire."""
    return any(k in ("size", "rate") for k in FAILS)


def clients_cfg():
    """Clients lus dans profil.md : lignes « CLIENT: Nom | mot-clé, mot-clé ». Renvoie [(nom, [mots-clés])]."""
    p = HERE / "profil.md"
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*CLIENT\s*:\s*([^|]+?)\s*(?:\|\s*(.*))?$", line)
            if m:
                out.append((m.group(1).strip(), [k.strip() for k in (m.group(2) or "").split(",") if k.strip()]))
    return out


def profil():
    """Contexte facultatif décrit dans profil.md (clients, secteurs, publics, ton) : les lignes qui commencent par # sont ignorées."""
    p = HERE / "profil.md"
    if not p.exists():
        return ""
    return "\n".join(l.rstrip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#"))[:3000]


def synthesize(signals):
    by_id = {s["id"]: s for s in signals}
    seen = recent_topics()
    ctx = profil()
    head = (f"Date : {dt.date.today():%d/%m/%Y}\n"
            + (f"CONTEXTE DE L'AGENCE (à privilégier pour choisir et orienter les sujets et les angles) :\n{ctx}\n" if ctx else "")
            + (("SUJETS DÉJÀ TRAITÉS CES DERNIERS JOURS (ne les répète pas, sauf évolution notable) : " + " ; ".join(seen) + "\n") if seen else ""))
    # passe 1 : sélection des sujets (réponse courte, ids seulement) ; si le service limite le débit, on réessaie avec moins de signaux
    sel, ids, used_cap = None, set(), None
    for k, cap in enumerate((int(os.environ.get("RADAR_IA_MAX", "220")), 110, 60)):
        sub = ia_subset(signals, cap)
        FAILS.clear()
        try:
            sel = _json(head + "SIGNAUX :\n" + "\n".join(_lines_courtes(sub)), SELECT)
            ids = {i for key in ("fiches", "ca_monte", "chiffres") for it in sel.get(key, []) for i in it.get("signal_ids", []) if i in by_id}
            if len(sel.get("fiches", [])) < 5 or not ids:
                sel = None
            used_cap = cap
            break
        except Exception as e:
            sel = None
            if k < 2 and _limite_taille():
                print(f"[!] requête refusée ({str(e)[:60]}) : nouvel essai avec {(110, 60)[k]} signaux dans 60 s", file=sys.stderr)
                DEAD.clear()
                time.sleep(60)
                continue
            print(f"[!] sélection impossible ({e}), passage en une seule étape", file=sys.stderr)
            break
    # passe 2 : rédaction à partir des seuls signaux retenus
    if sel:
        subset = [by_id[i] for i in sorted(ids)]
        prompt = (head + "SÉLECTION DU RÉDACTEUR EN CHEF (écris exactement une fiche par sujet de \"fiches\", une entrée \"ca_monte\" par sujet de "
                  "\"ca_monte\", et extrais les \"chiffres\" des signaux listés ; écarte un sujet seulement si tu ne peux pas formuler son insight) :\n"
                  + json.dumps(sel, ensure_ascii=False) + "\nSIGNAUX RETENUS :\n" + "\n".join(_lines(subset)))
    else:
        prompt = head + "SIGNAUX :\n" + "\n".join(_lines(ia_subset(signals, 110)))
    data = _json(prompt, SYSTEM)
    fiches, monte, chiffres = data.get("fiches", []), data.get("ca_monte", []), data.get("chiffres", [])

    keep = []
    for f in fiches:
        if f.get("urgence") not in ("chaud", "montant", "fond"):
            f["urgence"] = "montant"
        if f.get("famille") not in ("client", "artefact3000"):
            f["famille"] = "artefact3000" if (f.get("idee") and not f.get("angles")) else "client"
        f["liens"] = _liens(by_id, f.get("signal_ids", []))
        valides = {n for n, _ in clients_cfg()}
        f["clients"] = [c for c in (f.get("clients") or []) if c in valides]  # jamais un nom que l'IA aurait inventé
        complete = f.get("idee") if f["famille"] == "artefact3000" else f.get("angles")
        if complete and not SENSIBLE.search(f.get("titre", "")):
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


def _auto():
    """Module de l'édition automatique, intégré à ce fichier (aucun fichier à ajouter à côté)."""
    mod = types.ModuleType("auto")
    exec(compile(_AUTO_SRC, "auto(embarqué)", "exec"), mod.__dict__)
    return mod


def has_ai():
    return any(_key(k) for k in ("GEMINI_API_KEY", "MISTRAL_API_KEY", "ANTHROPIC_API_KEY"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true", help="signaux bruts, sans IA ni regroupement")
    ap.add_argument("--demo", action="store_true", help="fiches d'exemple hors ligne")
    ap.add_argument("--sans-ia", action="store_true", help="édition automatique même si des clés d'API existent")
    ap.add_argument("--si-absent", action="store_true", help="ne rien faire si l'édition du jour est déjà publiée")
    ap.add_argument("--monte", action="store_true", help="mise à jour de midi : seulement « Ça monte » (avec IA) ou nouvelle édition automatique")
    a = ap.parse_args()
    now = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    ai = has_ai() and not a.sans_ia
    if not (a.demo or a.raw):
        print("Mode : " + (f"avec IA ({providers_line()})" if ai else "édition automatique, SANS IA ni clé d'API"))
    if a.demo:
        fiches = json.loads((HERE / "demo.json").read_text(encoding="utf-8"))
        return render({"mode": "demo", "generated": now, "fiches": fiches, "ca_monte": [], "chiffres": [], "feeds_ko": [], "signals": []})
    today = f"{dt.date.today():%d/%m/%Y}"
    if a.si_absent and (OUT / "latest.json").exists():
        cur = json.loads((OUT / "latest.json").read_text(encoding="utf-8"))
        if cur.get("mode") in ("live", "auto") and not cur.get("stale") and cur.get("generated", "")[:10] == today and (cur["mode"] == "auto") == (not ai):
            return print("Édition du jour déjà publiée : rien à faire.")
    prev = None
    if a.monte and ai and (OUT / "latest.json").exists():
        prev = json.loads((OUT / "latest.json").read_text(encoding="utf-8"))
        if prev.get("mode") != "live" or prev.get("generated", "")[:10] != today:
            prev = None  # pas d'édition du jour : on lance l'édition complète
    signals = collect()
    print(f"{len(signals)} signaux collectés : " + ", ".join(sorted({x['source'] for x in signals})))
    if not signals:
        sys.exit("Aucun signal : vérifie ta connexion / les accès réseau.")
    notify(signals)
    if prev:
        try:
            prev.update({"ca_monte": synthesize_monte(signals), "generated": now, "feeds_ko": FEEDS_KO, "signals": signals})
            return render(prev)
        except Exception as e:
            print(f"::warning::Mise à jour « Ça monte » par l'IA impossible ({type(e).__name__}) : l'édition du matin est conservée.", file=sys.stderr)
            return
    if a.raw:
        return render({"mode": "raw", "generated": now, "fiches": [], "ca_monte": [], "chiffres": [], "feeds_ko": FEEDS_KO, "signals": signals})
    payload = None
    if ai:
        try:
            fiches, monte, chiffres = synthesize(signals)
            payload = {"mode": "live", "generated": now, "fiches": fiches, "ca_monte": monte, "chiffres": chiffres}
        except Exception as e:
            print(f"::warning::L'IA n'a pas répondu ({type(e).__name__}: {str(e)[:200]}) : édition automatique à la place.", file=sys.stderr)
    if payload is None:
        auto = _auto()
        payload = auto.build(signals, now, clients_cfg(), profil())
        payload["generated"] = now
        if ai:
            raisons = " | ".join(dict.fromkeys(ERRORS))[:600]
            payload["stale"] = ("L'IA n'a pas répondu ce matin : édition automatique (sans rédaction de fiches) affichée à la place."
                                + (f" Raisons : {raisons}" if raisons else ""))
    payload.update({"feeds_ko": FEEDS_KO, "signals": signals})
    render(payload)


# <<AUTO_SRC>>
_AUTO_SRC = r'''"""Édition automatique de RADAR, SANS modèle de langage ni clé d'API.

Tout est calculé par des règles simples et vérifiables :
  - regroupement des articles qui parlent du même sujet (mots rares en commun),
  - « ça monte » = pics Wikipédia / Google Trends, avec le titre d'actualité le plus proche,
  - chiffres et sondages extraits des titres (jamais reformulés),
  - frictions repérées par un lexique (ras-le-bol, boycott, backlash, fatigue...),
  - lectures de la semaine (newsletters et sites culture),
  - un prompt prêt à coller dans Claude / Gemini / ChatGPT pour rédiger les fiches à la main.
Aucune information n'est inventée : chaque ligne vient d'un article collecté et garde son lien.
"""
import math
import re
import unicodedata
from collections import Counter, defaultdict

SENSIBLE = re.compile(r"\b(mort|morts|d[ée]c[èe]s|d[ée]c[ée]d[ée]|meurt|disparition d[eu]|hommage national|deuil|attentat|attaque|fusillade|crash|accident|tu[ée]s?|victimes?|suicide|guerre|massacre|incendie meurtrier|piratage|cyberattaque|hack)\b", re.I)

STOP = set("""
le la les un une des de du et en au aux pour par sur dans avec sans ce ces cet cette qui que quoi dont est sont ont ete etre plus moins tres tout tous toute toutes comme mais ainsi apres avant entre vers chez son ses leur leurs nous vous ils elles elle fait faire selon alors aussi encore depuis pendant contre sous cela celui ceux celles dit dire veut veulent peut peuvent doit doivent pourquoi comment quand ici deja bien tres autre autres meme memes chaque
the and for with from that this have has are was were will not but you your about into over after before more less their they them than what when who how why new says said just like could would should there here been being also very much many
monde jour jours video videos photos photo direct live info infos actu news article articles france francais francaise paris annee annees semaine mois ans apres dernier derniere premier premiere nouveau nouvelle nouveaux nouvelles grand grande grands grandes
""".split())

FRICTION = ["ras-le-bol", "ras le bol", "exasp", "boycott", "polémique", "polemique", "bad buzz", "backlash", "rejet", "fatigue", "saturation",
            "colère", "colere", "dénonc", "denonc", "arnaque", "frais cachés", "frais caches", "enshittification", "slop", "intrus", "lassitude",
            "ne supportent plus", "n'en peuvent plus", "indignation", "scandale", "revolt", "fed up", "sick of", "tired of", "pushback",
            "outrage", "irrité", "irrite", "méfiance", "mefiance", "refus", "contestation", "critiqué", "critique "]

NOMBRE = re.compile(r"(\d[\d\s.,]*\s?(?:%|pour cent|pc\b)|\b\d{1,2}\s?(?:sur|out of|in)\s?\d{1,2}\b|\b\d[\d\s.,]*\s?(?:millions?|milliards?|billions?)\b)", re.I)
FR_WORDS = {"le", "la", "les", "des", "du", "une", "est", "sont", "pour", "dans", "sur", "avec", "qui", "que", "au", "aux", "en", "et"}
CORE = ("Wikip", "Google Trends")


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def clean(title):
    """Titre sans le nom du média collé à la fin (« … - Le Monde »)."""
    return re.sub(r"\s[-–|]\s[^-–|]{2,40}$", "", (title or "").strip())


def publisher(s):
    """Vrai média d'un article : pour Google Actualités, c'est le suffixe du titre."""
    if s["source"].startswith(("GActu", "Google Actualités")):
        m = re.search(r"\s[-–|]\s([^-–|]{2,40})$", s["title"])
        return m.group(1).strip() if m else "Google Actualités"
    if s["source"].startswith("Google Trends"):
        return "Google Trends"
    if s["source"].startswith("Wikip"):
        return "Wikipédia"
    return s["source"]


def kw(title):
    return {w for w in re.findall(r"[a-z]{4,}", norm(clean(title))) if w not in STOP}


def _age(s, default=24):
    """Âge en heures ; les sources sans date (Google Actualités, pics) sont supposées récentes."""
    a = s.get("age")
    return default if a is None else a


def is_fr(text):
    toks = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", (text or "").lower())
    return sum(t in FR_WORDS for t in toks) >= 2


def sensible(text):
    return bool(SENSIBLE.search(text or ""))


# ---------- 1. regroupement par sujet ----------
def clusters(signals):
    n = len(signals)
    K = [kw(s["title"]) for s in signals]
    df = Counter(w for k in K for w in k)
    idf = {w: math.log(n / c) for w, c in df.items()}
    index = defaultdict(list)
    for i, k in enumerate(K):
        for w in k:
            if 2 <= df[w] <= max(8, n // 40):  # mots assez rares pour désigner un sujet
                index[w].append(i)
    score = defaultdict(float)
    for w, ids in index.items():
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                score[(ids[a], ids[b])] += idf[w]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (i, j), sc in score.items():
        if sc >= 6.0:  # au moins deux mots rares en commun
            parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    out = []
    for ids in groups.values():
        if not 2 <= len(ids) <= 30:
            continue
        pubs = {publisher(signals[i]) for i in ids}
        if len(pubs) < 2:
            continue
        words = Counter(w for i in ids for w in K[i])
        top = [w for w, _ in sorted(words.items(), key=lambda x: -(x[1] * idf.get(x[0], 0)))[:3]]
        best = max(ids, key=lambda i: sum(idf.get(w, 0) for w in K[i] & set(top)) - 0.01 * _age(signals[i]))
        spike = any(signals[i]["source"].startswith(CORE) for i in ids)
        fresh = min(_age(signals[i]) for i in ids)
        sc = len(pubs) * 3 + min(len(ids), 8) + (3 if fresh <= 24 else 0) + (4 if spike else 0)
        out.append({"ids": ids, "pubs": sorted(pubs), "tags": top, "best": best, "score": sc, "spike": spike})
    return sorted(out, key=lambda c: -c["score"])


def _lien(s):
    age = f", {s['metric']}" if s.get("age") is not None else ""
    return {"label": f"{clean(s['title'])[:80]} ({publisher(s)}{age})", "url": s["url"]}


# ---------- 2. ça monte ----------
def ca_monte(signals, groups):
    where = {}
    for g in groups:
        for i in g["ids"]:
            where[i] = g
    picks = []
    for prefix, k in (("Wikipédia FR", 4), ("Google Trends FR", 3), ("Wikipédia EN", 1), ("Google Trends US", 1), ("Google Trends GB", 1)):
        picks += [i for i, s in enumerate(signals) if s["source"] == prefix][:k]
    out = []
    for i in picks:
        s = signals[i]
        g = where.get(i)
        related = [signals[j] for j in (g["ids"] if g else []) if j != i and not signals[j]["source"].startswith(CORE)]
        mots = kw(s["title"])
        if mots:  # rapprochement direct : tous les mots du pic figurent dans le titre d'un article
            deja = {id(x) for x in related}
            related += [x for x in signals if not x["source"].startswith(CORE) and id(x) not in deja and mots <= kw(x["title"])]
        related.sort(key=_age)
        head = related[0] if related else None
        pourquoi = f"Titre associé : « {clean(head['title'])[:140]} » ({publisher(head)})" if head else "cause non identifiée dans les signaux"
        texte = f"{s['title']} {head['title'] if head else ''}"
        rebond = not sensible(texte)
        out.append({"sujet": s["title"], "pourquoi": pourquoi, "mesure": s.get("metric", ""), "rebondable": rebond,
                    "precaution": "" if rebond else "Sujet sensible : ne pas rebondir.", "idee": None,
                    "liens": [_lien(s)] + [_lien(x) for x in related[:2]]})
    return out


# ---------- 3. chiffres ----------
def chiffres(signals, limit=10):
    seen, out = set(), []
    for s in signals:
        if s["source"].startswith(CORE) or s["source"] == "Hacker News":
            continue
        for texte in (clean(s["title"]), s.get("note", "")):
            m = NOMBRE.search(texte or "")
            if m and not sensible(texte):
                key = norm(clean(s["title"]))[:60]
                if key in seen:
                    break
                seen.add(key)
                out.append({"chiffre": m.group(1).strip(), "phrase": (clean(s["title"]) if texte == clean(s["title"]) else texte)[:170],
                            "source": publisher(s), "liens": [_lien(s)], "fr": is_fr(s["title"])})
                break
    out.sort(key=lambda c: (not c["fr"], 0))
    return [{k: v for k, v in c.items() if k != "fr"} for c in out[:limit]]


# ---------- 4. frictions ----------
def frictions(signals, limit=14):
    out, seen = [], set()
    for s in signals:
        if s["source"].startswith(CORE):
            continue
        t = norm(f"{clean(s['title'])} {s.get('note', '')[:120]}")
        mot = next((m for m in FRICTION if norm(m) in t), None)
        if mot and not sensible(s["title"]):
            key = norm(clean(s["title"]))[:50]
            if key in seen:
                continue
            seen.add(key)
            out.append({"titre": clean(s["title"])[:150], "source": publisher(s), "url": s["url"], "age": s.get("age"), "mot": mot.strip()})
    out.sort(key=lambda x: (x["age"] is None, x["age"] or 0))
    return out[:limit]


# ---------- 5. lectures (newsletters, culture) ----------
def lectures(signals, limit=30):
    by = defaultdict(list)
    for s in signals:
        if s.get("cat") == "lecture":
            by[s["source"]].append(s)
    out = []
    for src, items in by.items():
        for s in sorted(items, key=lambda x: _age(x, 999))[:1]:
            out.append({"source": src, "titre": clean(s["title"])[:150], "url": s["url"], "age": s.get("age"), "note": (s.get("note") or "")[:160]})
    out.sort(key=lambda x: (x["age"] is None, x["age"] or 0))
    return out[:limit]


# ---------- 5 bis. clients et problèmes concrets ----------
def tag_clients(texte, clients):
    """Clients de l'agence dont un mot-clé figure dans le texte (mot entier, pluriel accepté)."""
    t = norm(texte)
    out = []
    for nom, mots in clients or []:
        for m in mots:
            k = norm(m).strip()
            if len(k) >= 3 and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"s?(?![a-z0-9])", t):
                out.append(nom)
                break
    return out


DISRUPT = ["grève", "blocage", "bloqué", "bloqués", "perturbation", "annulé", "annulation", "retard", "pénurie", "rupture", "panne", "coupure",
           "fermeture", "canicule", "vigilance", "inondation", "tempête", "neige", "verglas", "hausse", "flambée", "augmentation", "suppression",
           "réforme", "échéance", "date limite", "manifestation", "mobilisation", "saturé", "files d'attente", "délai", "bug"]
PUBLICS = [
    ("Étudiants et lycéens", ["étudiant", "lycéen", "lycée", "élève", "partiels", "examen", "bac ", "université", "fac ", "parcoursup", "rentrée"]),
    ("Voyageurs et usagers des transports", ["sncf", "train", "voyageur", "usager", "rer", "métro", "tgv", "aérien", "avion", "aéroport", "vol ", "bagage", "ratp"]),
    ("Automobilistes", ["automobiliste", "carburant", "essence", "gazole", "péage", "embouteillage", "permis"]),
    ("Locataires et propriétaires", ["locataire", "loyer", "logement", "propriétaire", "immobilier", "copropriété"]),
    ("Retraités et futurs retraités", ["retraité", "retraite", "pension"]),
    ("Salariés et demandeurs d'emploi", ["salarié", "chômage", "emploi", "licenciement", "employeur", "travailleur"]),
    ("Parents et familles", ["parent", "crèche", "enfant", "école", "garde d'enfant", "famille"]),
    ("Patients", ["patient", "hôpital", "médecin", "pharmacie", "médicament", "urgences", "santé"]),
    ("Ménages (budget, énergie, courses)", ["facture", "électricité", "gaz", "énergie", "prix", "courses", "supermarché", "inflation", "pouvoir d'achat", "chauffage"]),
    ("Petites entreprises et commerçants", ["commerçant", "artisan", "pme", "entreprise", "restaurateur"]),
    ("Agriculteurs", ["agriculteur", "éleveur", "récolte", "sécheresse"]),
    ("Vacanciers et touristes", ["vacances", "touriste", "tourisme", "séjour", "week-end", "station de ski"]),
]


def _mot(t, mot):
    return re.search(r"(?<![a-z0-9])" + re.escape(norm(mot)).replace("\\ ", " ") + r"s?", t)


def problemes(signals, clients, limit=6):
    """Problèmes concrets du moment : un événement perturbateur (grève, panne, canicule, hausse...) + un public touché.
    Ce sont des POINTS DE DÉPART pour des idées « Artefact 3000 » : la règle repère, l'idée créative reste à inventer."""
    par = defaultdict(list)
    for s in signals:
        if s["source"].startswith(CORE) or sensible(s["title"]):
            continue
        t = norm(f"{clean(s['title'])} {s.get('note', '')[:140]}")
        evts = [m for m in DISRUPT if _mot(t, m)]
        if not evts:
            continue
        for label, mots in PUBLICS:
            if any(_mot(t, m) for m in mots):
                par[label].append((s, evts))
    out = []
    for label, items in par.items():
        items.sort(key=lambda x: _age(x[0]))
        pubs = {publisher(x[0]) for x in items}
        vus, lignes = set(), []
        for s, evts in items:
            cle = norm(clean(s["title"]))[:50]
            if cle in vus:
                continue
            vus.add(cle)
            lignes.append({"titre": clean(s["title"])[:150], "source": publisher(s), "url": s["url"], "age": s.get("age"), "evenement": evts[0]})
            if len(lignes) >= 3:
                break
        texte = " ".join(l["titre"] for l in lignes)
        out.append({"public": label, "items": lignes, "nb_medias": len(pubs), "clients": tag_clients(texte, clients)})
    out.sort(key=lambda p: (-p["nb_medias"], -len(p["items"])))
    return out[:limit]


# ---------- 6. prompt à coller dans un outil de chat ----------
PROMPT_HEAD = """Tu es l'assistant de veille d'une agence de publicité, « Artefact 3000 », dirigée par un directeur de création. Voici les signaux du jour repérés automatiquement (articles récents, pics, chiffres, frictions, problèmes concrets).
À partir de ces seuls éléments, rédige 10 fiches en DEUX FAMILLES :

A) 6 fiches « POUR LES CLIENTS » : un fait, une friction ou une contre-tendance utile à un ou plusieurs clients de l'agence (liste ci-dessous). Format :
N. Domaine : titre
- Clients concernés : noms pris dans la liste
- Le signal : fait précis tiré des signaux
- Le statut : Émergent / Contre-tendance / Friction d'actualité / Micro-niche / Saturation
- La source : médias et liens
- La preuve : un chiffre seulement s'il figure dans les signaux (sinon omets la ligne)
- L'insight : une phrase naturelle (comportement, tension, ce que les marques font encore)
- L'angle : pour qui + message de marque rédigé, affichable tel quel
- À vérifier

B) 4 fiches « POUR LES GENS (Artefact 3000) » : ce que l'agence pourrait sortir en son nom pour les gens. Un problème réel que des gens rencontrent à cause d'un événement du moment (grève, blocage, canicule, hausse des prix, panne, réforme, examens...), résolu par une idée CRÉATIVE ET ORIGINALE, simple à réaliser vite. Ce peut être un outil, un objet, un affichage, un détournement, un service, un événement, un contenu : pas forcément numérique. Format :
N. Titre
- Public touché
- Le problème : une phrase appuyée sur les signaux
- L'idée : une à trois phrases concrètes
- Le format : outil, affichage, objet, événement, contenu, détournement...
- Effort estimé : léger (quelques jours) / moyen / lourd, à confirmer
- Clients possibles : noms pris dans la liste, ou aucun
- Limites : neutralité, droits, risques, ce qu'il ne faut pas promettre
- Sources : médias et liens, et À vérifier

Règles : n'invente aucun chiffre, étude, institut ni source ; zéro marronnier déjà rincé sauf contrecoup ; priorité aux frictions et aux problèmes pratiques ; privilégie les sujets confirmés par plusieurs sources et ceux qui concernent au moins un client ; français sobre, sans métaphore physique ni mot inventé.
Pour les idées « Artefact 3000 » : NON PARTISANES (elles aident les gens, ne prennent jamais parti pour ou contre un mouvement, un gouvernement ou une entreprise), jamais sur un drame, un deuil ou une catastrophe, aucune donnée personnelle sensible, aucun effet chiffré promis.
Les sujets sensibles (deuil, drame, crime, polémique clivante) ne reçoivent AUCUNE idée : liste-les à part sous « Ça monte, ne pas rebondir ». Pour les associations de la liste, un sujet grave n'est possible que si l'idée est respectueuse et utile (note « validation requise »).
"""


def prompt(date, sujets, monte, chif, fric, lect, probs, clients, contexte):
    L = [PROMPT_HEAD, f"DATE : {date}", ""]
    if clients:
        L += ["CLIENTS DE L'AGENCE (nom : thèmes) :"] + [f"- {n} : {', '.join(m[:8])}" for n, m in clients] + [""]
    if contexte:
        L += ["CONTEXTE :", contexte, ""]
    L += ["PROBLÈMES CONCRETS DU MOMENT (matière pour la famille B) :"]
    for p in probs:
        L.append(f"- {p['public']}" + (f"  [clients possibles : {', '.join(p['clients'])}]" if p["clients"] else ""))
        L += [f"    · {i['titre']} ({i['source']}) {i['url']}" for i in p["items"]]
    L += ["", "SUJETS REPÉRÉS PAR PLUSIEURS SOURCES :"]
    for s in sujets[:14]:
        L.append(f"- {s['titre']} [{', '.join(s['medias'][:5])}]" + (f"  [clients : {', '.join(s['clients'])}]" if s["clients"] else "") + ("  (SENSIBLE)" if s["sensible"] else ""))
        for l in s["liens"][:3]:
            L.append(f"    · {l['label']} {l['url']}")
    L += ["", "ÇA MONTE (pics Wikipédia / Google Trends) :"]
    L += [f"- {m['sujet']} | {m['mesure']} | {m['pourquoi']}" + ("" if m["rebondable"] else "  (SENSIBLE)") for m in monte]
    L += ["", "CHIFFRES ET ÉTUDES (repris tels quels) :"] + [f"- {c['chiffre']} : {c['phrase']} ({c['source']}) {c['liens'][0]['url']}" for c in chif]
    L += ["", "FRICTIONS :"] + [f"- {f['titre']} ({f['source']}) {f['url']}" + (f"  [clients : {', '.join(f['clients'])}]" if f["clients"] else "") for f in fric]
    L += ["", "LECTURES (newsletters, culture) :"] + [f"- {l['titre']} ({l['source']}) {l['url']}" for l in lect]
    return "\n".join(L)


# ---------- assemblage ----------
def build(signals, date, clients=None, contexte=""):
    clients = clients or []
    groups = clusters(signals)
    sujets = []
    for g in groups:
        if len(sujets) >= 14:
            break
        best = signals[g["best"]]
        texte = " ".join(signals[i]["title"] for i in g["ids"])
        members = sorted((signals[i] for i in g["ids"] if not signals[i]["source"].startswith(CORE)), key=_age)
        if not members:
            continue
        sujets.append({"titre": clean(best["title"])[:140], "tags": g["tags"], "medias": g["pubs"], "nb": len(g["ids"]),
                       "sensible": sensible(texte), "clients": tag_clients(texte, clients), "liens": [_lien(x) for x in members[:4]]})
    monte, chif, fric, lect = ca_monte(signals, groups), chiffres(signals), frictions(signals), lectures(signals)
    for f in fric:
        f["clients"] = tag_clients(f["titre"], clients)
    probs = problemes(signals, clients)
    return {"mode": "auto", "fiches": [], "ca_monte": monte, "chiffres": chif, "sujets": sujets, "frictions": fric, "lectures": lect, "problemes": probs,
            "clients_liste": [n for n, _ in clients],
            "prompt": prompt(date, sujets, monte, chif, fric, lect, probs, clients, contexte)}
'''
# <<END_AUTO_SRC>>


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:  # erreur API lisible dans les logs
        sys.exit(f"ERREUR API {e.code} sur {e.url.split('?')[0]} : {e.read().decode(errors='replace')[:600]}")
