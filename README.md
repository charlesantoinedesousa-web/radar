# RADAR
Détecte ce qui monte (Wikipédia FR, Google Trends, Actus, Hacker News) et propose des angles de rebond.

    python3 radar.py --demo    # 20 fiches d'exemple, hors ligne
    python3 radar.py --raw     # signaux bruts, sans clé API
    GEMINI_API_KEY=... python3 radar.py   # clé gratuite : aistudio.google.com/apikey

Une fois par jour (Mac/Linux) : `crontab -e` puis
    0 8 * * * cd /chemin/vers/radar && ANTHROPIC_API_KEY=... RADAR_WEBHOOK=... python3 radar.py
Sans machine allumée : pousser ce dossier sur GitHub ; `.github/workflows/radar-daily.yml` tourne chaque jour
(secrets : GEMINI_API_KEY, RADAR_WEBHOOK optionnel). Le brief est commité dans `output/`.
