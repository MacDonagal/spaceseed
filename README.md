# SpaceSeed

Détecte les torrents intéressants d'un indexeur Torznab, les note et les envoie à qBittorrent, à la main ou automatiquement, sans mettre le compte C411 en danger.

```text
Prowlarr ──flux Torznab──> SpaceSeed ──envois──> qBittorrent
                               │
                               └──marge et blocage──> API Profil C411
```

Prowlarr et qBittorrent ne sont pas inclus : SpaceSeed se connecte à vos instances existantes.

> **Projet créé avec une IA.** Le code, les tests et cette documentation ont été écrits en grande partie par [Claude Code](https://claude.com/claude-code) (Anthropic), sous la direction de l'auteur du dépôt. Relisez le code et testez-le avant de l'utiliser sur un compte réel.

## Installation

Prérequis : Docker avec Compose. qBittorrent doit pouvoir joindre Prowlarr à l'adresse utilisée dans `TORZNAB_URL`, car il télécharge les fichiers `.torrent` depuis les liens fournis par Prowlarr.

```bash
cp .env.example .env
```

```bash
sed -i "s/^SPACESEED_PASSWORD=.*/SPACESEED_PASSWORD=$(openssl rand -hex 16)/; s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
```

```bash
nano .env
```

Renseigner au minimum `TORZNAB_URL`, `TORZNAB_API_KEY`, `QBITTORRENT_URL`, `QBITTORRENT_USERNAME`, `QBITTORRENT_PASSWORD` et `C411_API_KEY`.

```bash
docker compose up -d --build
```

```bash
docker compose logs -f
```

Par défaut, l'interface écoute sur `127.0.0.1:8095`. Pour y accéder à distance :

```bash
ssh -L 8095:127.0.0.1:8095 user@IP_DU_SERVEUR
```

Ouvrir ensuite `http://localhost:8095`. L'identifiant est `SPACESEED_USERNAME`, `admin` par défaut.

## Vérification

Connexion à qBittorrent :

```bash
curl -u "admin:$(grep ^SPACESEED_PASSWORD .env | cut -d= -f2)" http://127.0.0.1:8095/qbittorrent/test
```

La page Historique affiche le dernier scan, le ratio et la marge C411. Envoyer un petit torrent FreeLeech à la main avant d'activer l'envoi automatique.

| Symptôme | Cause probable |
|---|---|
| Erreur `401` ou `403` sur C411 | Clé invalide, ou clé créée sans le scope « Profil (lecture) » |
| Tous les envois sont bloqués | API C411 injoignable, ou compte bloqué : voir la page Historique |
| « Envoyé » mais rien dans qBittorrent | qBittorrent ne joint pas l'adresse de `TORZNAB_URL` |
| Page Opportunités vide | Filtres trop stricts, ou erreur de scan : voir la page Historique |

## Fonctionnement

- Le premier scan démarre avec le conteneur, puis se répète toutes les `SCAN_INTERVAL_MINUTES`.
- Le score combine leechers, seeders, âge, FreeLeech et taille.
- Les torrents absents du flux depuis `STALE_AFTER_MINUTES` sont retirés.
- Les 5 dernières réponses XML invalides sont conservées dans `./data`.
- La colonne Swarm associe les torrents par leur nom : un nom différent dans qBittorrent affichera « Non ajouté ».

## Contrôle C411

Créer une clé avec le scope « Profil (lecture) » : le scope d'une clé existante ne se modifie pas.

C411 bloque le téléchargement quand l'upload passe sous 80 % du download. SpaceSeed calcule la même marge, crédits inclus :

```text
marge = upload / 0,8 - download
```

Avant chaque envoi, automatique ou manuel :

- rien ne part si C411 signale `download.allowed: false` ;
- rien ne part si l'API échoue ou répond de façon illisible ;
- un torrent non FreeLeech doit laisser au moins `C411_MIN_MARGIN_GB` de marge une fois sa taille déduite ;
- un torrent FreeLeech, ou un FreeLeech global, ne consomme pas de marge.

Le profil est gardé 60 secondes en cache.

Limites :

- l'API n'expose pas les hit-and-run : ils ne sont pas contrôlés ;
- seuls les envois du scan en cours sont déduits, pas les téléchargements déjà lancés : `C411_MIN_MARGIN_GB` doit les couvrir ;
- le contrôle s'applique à tous les torrents de SpaceSeed, même ceux d'un autre indexeur.

## Envoi automatique

Désactivé par défaut. Avec `AUTO_GRAB_ENABLED=true`, chaque scan réussi envoie les meilleures opportunités qui respectent ces règles et le contrôle C411 :

| Variable | Défaut | Rôle |
|---|---|---|
| `AUTO_GRAB_MIN_SCORE` | `150` | Score minimal |
| `AUTO_GRAB_MIN_SEEDERS` | `1` | Seeders minimaux |
| `AUTO_GRAB_FREELEECH_ONLY` | `true` | FreeLeech uniquement |
| `AUTO_GRAB_MAX_SIZE_GB` | `15` | Taille maximale par torrent |
| `AUTO_GRAB_MAX_PER_SCAN` | `1` | Envois maximaux par scan |
| `AUTO_GRAB_MAX_ACTIVE` | `2` | Torrents incomplets maximaux dans `QBITTORRENT_CATEGORY` |
| `AUTO_GRAB_MIN_FREE_SPACE_GB` | `50` | Espace à conserver sur le disque de qBittorrent |

- Un torrent déjà envoyé, à la main ou automatiquement, n'est jamais renvoyé.
- Un envoi refusé est retenté 3 fois au maximum.
- Les torrents de taille inconnue sont ignorés.
- La page Historique liste les 50 derniers envois.

Ces valeurs conviennent à 20 Mbps d'upload. Sur une ligne plus rapide, augmenter `AUTO_GRAB_MAX_SIZE_GB`, `AUTO_GRAB_MAX_PER_SCAN` et `AUTO_GRAB_MAX_ACTIVE`, puis relancer le conteneur :

```bash
docker compose up -d --force-recreate
```

Vérifier les règles du tracker avant d'activer cette fonction. SpaceSeed ne supprime aucun torrent : le temps de seed et le nettoyage se règlent dans qBittorrent.

## Notifications Telegram

1. Créer un bot avec [@BotFather](https://t.me/BotFather) et copier son token dans `TELEGRAM_BOT_TOKEN`.
2. Envoyer un message au bot.
3. Lire le `chat.id` de ce message et le copier dans `TELEGRAM_CHAT_ID` :

```bash
curl -s "https://api.telegram.org/botTOKEN/getUpdates"
```

4. Relancer le conteneur, puis envoyer un message de test :

```bash
curl -X POST -u "admin:$(grep ^SPACESEED_PASSWORD .env | cut -d= -f2)" http://127.0.0.1:8095/telegram/test
```

| Variable | Défaut | Rôle |
|---|---|---|
| `TELEGRAM_MIN_SCORE` | `150` | Score minimal pour une alerte |
| `TELEGRAM_FREELEECH_ONLY` | `false` | Alertes FreeLeech uniquement |
| `TELEGRAM_MAX_PER_SCAN` | `5` | Alertes maximales par scan, les meilleurs scores d'abord |

- Chaque torrent n'est signalé qu'une fois. Ce souvenir est conservé `SCAN_HISTORY_DAYS` jours.
- L'alerte indique si l'envoi automatique a déjà transmis le torrent à qBittorrent.
- Une panne de Telegram n'interrompt pas le scan : les alertes manquantes repartent au scan suivant.

## Sécurité

- Sans `SPACESEED_PASSWORD`, l'interface est ouverte et un avertissement apparaît dans les logs.
- HTTP Basic circule en clair : passer par un tunnel SSH ou un reverse proxy HTTPS.
- Les POST venant d'un autre site sont refusés : un reverse proxy doit transmettre l'en-tête `Host`.
- `/health` reste public pour le healthcheck Docker.
- Le conteneur démarre en root pour donner `./data` à `PUID:PGID`, puis l'application tourne avec cet utilisateur.

## Développement

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

```text
app/
├── main.py          routes et protection de l'interface
├── scanner.py       scan périodique et mise à jour de la base
├── torznab.py       lecture du flux Torznab
├── scoring.py       calcul du score
├── autograb.py      sélection et envoi automatique
├── c411.py          contrôle du compte C411
├── qbittorrent.py   client qBittorrent et état du swarm
├── telegram.py      alertes Telegram
├── security.py      authentification et contrôle d'origine
├── config.py        variables d'environnement
├── database.py      connexion SQLite
├── models.py        tables
├── static/          CSS et JS de la grille
└── templates/       pages HTML
tests/               tests automatisés
```

## Avertissement

Ce projet est un outil d'automatisation pour vos propres comptes et instances. Respectez la loi de votre pays et le règlement de vos trackers, et ne téléchargez que des contenus que vous avez le droit d'obtenir. Les seuils C411 viennent de la documentation du site et peuvent changer sans préavis. Le projet n'est pas affilié à C411.

## Licence

[MIT](LICENSE)
