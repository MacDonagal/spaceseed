<div align="center">

<img src="assets/spaceseed-bot.png" alt="Logo SpaceSeed" width="160">

# SpaceSeed

**Repérez les bons torrents avant la cohue, sans mettre votre ratio en danger.**

[![Tests](https://github.com/MacDonagal/spaceseed/actions/workflows/tests.yml/badge.svg)](https://github.com/MacDonagal/spaceseed/actions/workflows/tests.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![Docker](https://img.shields.io/badge/docker-compose-2496ED)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-green)](LICENSE)

</div>

SpaceSeed surveille un indexeur Torznab, note chaque torrent et vous alerte sur Telegram. Le téléchargement se lance d'un clic, ou automatiquement, après vérification de votre compte C411.

> [!NOTE]
> **Projet créé avec une IA.** Le code, les tests et cette documentation ont été écrits en grande partie par [Claude Code](https://claude.com/claude-code) (Anthropic), sous la direction de l'auteur du dépôt. Relisez le code et testez-le avant de l'utiliser sur un compte réel.

## Fonctionnalités

- 📡 **Radar** : scan périodique et score selon les leechers, les seeders, l'âge, le FreeLeech et la taille.
- 🖥️ **Interface web** : grille triable et filtrable, état du swarm et historique.
- 🛡️ **Protection C411** : vérification de la marge et du blocage du compte avant chaque envoi.
- 🤖 **Envoi automatique** : désactivé par défaut, avec plafonds de volume et d'espace disque.
- 📱 **Alertes Telegram** : notification avec bouton **Télécharger**, sans port à ouvrir.

```text
Prowlarr ──flux Torznab──> SpaceSeed ──envois──> qBittorrent
                             │    │
           API Profil C411 <─┘    └─> Telegram
```

Prowlarr et qBittorrent ne sont pas inclus : SpaceSeed se connecte à vos instances.

## Démarrage rapide

**Prérequis :** Docker avec Compose. qBittorrent doit pouvoir joindre Prowlarr à l'adresse indiquée dans `TORZNAB_URL`.

**1. Récupérer le projet**

```bash
git clone https://github.com/MacDonagal/spaceseed.git
```

```bash
cd spaceseed && cp .env.example .env
```

**2. Générer le mot de passe et renseigner l'utilisateur**

```bash
sed -i "s/^SPACESEED_PASSWORD=.*/SPACESEED_PASSWORD=$(openssl rand -hex 16)/; s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
```

**3. Compléter la configuration**

```bash
nano .env
```

| Variable | Valeur |
|---|---|
| `TORZNAB_URL` | Adresse Torznab de l'indexeur dans Prowlarr, par exemple `http://IP_PROWLARR:9696/1/api` |
| `TORZNAB_API_KEY` | Clé API de Prowlarr |
| `QBITTORRENT_URL` | Adresse de l'interface Web de qBittorrent |
| `QBITTORRENT_USERNAME` | Identifiant qBittorrent |
| `QBITTORRENT_PASSWORD` | Mot de passe qBittorrent |
| `C411_API_KEY` | Clé C411 créée avec le scope « Profil (lecture) » |

**4. Lancer SpaceSeed**

```bash
docker compose up -d --build
```

L'interface écoute sur `127.0.0.1:8095`. Pour y accéder à distance :

```bash
ssh -L 8095:127.0.0.1:8095 user@IP_DU_SERVEUR
```

Ouvrez ensuite `http://localhost:8095` et connectez-vous avec `admin` et le mot de passe de votre `.env`.

**5. Vérifier**

La page Historique doit afficher un scan réussi, votre ratio et votre marge. Envoyez ensuite un petit torrent FreeLeech à la main avant d'activer l'envoi automatique.

### Mettre à jour

```bash
git pull && docker compose up -d --build
```

Les fichiers `.env` et `data/` ne sont pas suivis par Git et restent intacts.

## Configuration

Toutes les variables sont décrites dans [.env.example](.env.example).

<details>
<summary><strong>🛡️ Protection C411</strong></summary>

<br>

Créez une clé avec le scope « Profil (lecture) » : le scope d'une clé existante ne se modifie pas.

C411 bloque le téléchargement quand l'upload passe sous 80 % du download. SpaceSeed calcule la même marge, crédits inclus :

```text
marge = upload / 0,8 - download
```

Avant chaque envoi :

- rien ne part si C411 signale `download.allowed: false` ;
- rien ne part si l'API échoue ou répond de façon illisible ;
- un torrent non FreeLeech doit laisser au moins `C411_MIN_MARGIN_GB` de marge ;
- un torrent FreeLeech, ou un FreeLeech global, ne consomme pas de marge.

**Limites**

- L'API n'expose pas les hit-and-run : ils ne sont pas contrôlés.
- Seuls les envois du scan en cours sont déduits, pas les téléchargements déjà lancés : `C411_MIN_MARGIN_GB` doit les couvrir.
- Le contrôle s'applique à tous les torrents de SpaceSeed, même ceux d'un autre indexeur.

</details>

<details>
<summary><strong>🤖 Envoi automatique</strong></summary>

<br>

Désactivé par défaut. Avec `AUTO_GRAB_ENABLED=true`, chaque scan réussi envoie les meilleures opportunités qui respectent ces règles et la protection C411.

| Variable | Défaut | Rôle |
|---|---|---|
| `AUTO_GRAB_MIN_SCORE` | `150` | Score minimal |
| `AUTO_GRAB_MIN_SEEDERS` | `1` | Seeders minimaux |
| `AUTO_GRAB_FREELEECH_ONLY` | `true` | FreeLeech uniquement |
| `AUTO_GRAB_MAX_SIZE_GB` | `15` | Taille maximale par torrent |
| `AUTO_GRAB_MAX_PER_SCAN` | `1` | Envois maximaux par scan |
| `AUTO_GRAB_MAX_ACTIVE` | `2` | Torrents incomplets maximaux dans `QBITTORRENT_CATEGORY` |
| `AUTO_GRAB_MIN_FREE_SPACE_GB` | `50` | Espace à conserver sur le disque de qBittorrent |

- Un torrent déjà envoyé n'est jamais renvoyé.
- Un envoi refusé est retenté 3 fois au maximum.
- Les torrents de taille inconnue sont ignorés.

Ces valeurs conviennent à 20 Mbps d'upload. Sur une ligne plus rapide, augmentez les trois limites de taille, d'envois et de torrents actifs.

Vérifiez les règles de votre tracker avant d'activer cette fonction. SpaceSeed ne supprime aucun torrent : le temps de seed et le nettoyage se règlent dans qBittorrent.

</details>

<details>
<summary><strong>📱 Alertes Telegram</strong></summary>

<br>

1. Créez un bot avec [@BotFather](https://t.me/BotFather) et copiez son token dans `TELEGRAM_BOT_TOKEN`.
2. Envoyez un message au bot.
3. Récupérez le `chat.id` de ce message et copiez-le dans `TELEGRAM_CHAT_ID` :

   ```bash
   curl -s "https://api.telegram.org/botTOKEN/getUpdates"
   ```

4. Relancez le conteneur, puis envoyez un message de test :

   ```bash
   curl -X POST -u "admin:$(grep ^SPACESEED_PASSWORD .env | cut -d= -f2)" http://127.0.0.1:8095/telegram/test
   ```

| Variable | Défaut | Rôle |
|---|---|---|
| `TELEGRAM_MIN_SCORE` | `150` | Score minimal pour une alerte |
| `TELEGRAM_FREELEECH_ONLY` | `false` | Alertes FreeLeech uniquement |
| `TELEGRAM_MAX_PER_SCAN` | `5` | Alertes maximales par scan, meilleurs scores d'abord |

- Chaque torrent n'est signalé qu'une fois.
- Le bouton **Télécharger** applique la protection C411 et ne répond qu'à `TELEGRAM_CHAT_ID`.
- Le bouton reste utilisable tant que le torrent figure dans les opportunités.
- Un même bot ne peut alimenter qu'une seule instance de SpaceSeed.
- Une panne de Telegram n'interrompt pas le scan.

</details>

<details>
<summary><strong>🔒 Sécurité</strong></summary>

<br>

- Sans `SPACESEED_PASSWORD`, l'interface est ouverte et un avertissement apparaît dans les logs.
- HTTP Basic circule en clair : passez par un tunnel SSH ou un reverse proxy HTTPS.
- Les POST venant d'un autre site sont refusés : un reverse proxy doit transmettre l'en-tête `Host`.
- `/health` reste public pour le healthcheck Docker.
- Le conteneur démarre en root pour donner `./data` à `PUID:PGID`, puis l'application tourne avec cet utilisateur.

</details>

## Dépannage

| Symptôme | Cause probable |
|---|---|
| Erreur `401` ou `403` sur C411 | Clé invalide, ou clé créée sans le scope « Profil (lecture) » |
| Tous les envois sont bloqués | API C411 injoignable, ou compte bloqué : voir la page Historique |
| « Envoyé » mais rien dans qBittorrent | qBittorrent ne joint pas l'adresse de `TORZNAB_URL` |
| Page Opportunités vide | Filtres trop stricts, ou erreur de scan : voir la page Historique |
| Aucune alerte Telegram | `TELEGRAM_MIN_SCORE` trop haut, ou bot mal configuré |
| Swarm affiche « Non ajouté » | Le nom du torrent diffère dans qBittorrent |

## Développement

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

<details>
<summary><strong>Structure du projet</strong></summary>

<br>

```text
app/
├── main.py          routes et protection de l'interface
├── scanner.py       scan périodique et mise à jour de la base
├── torznab.py       lecture du flux Torznab
├── scoring.py       calcul du score
├── autograb.py      sélection et envoi vers qBittorrent
├── c411.py          contrôle du compte C411
├── qbittorrent.py   client qBittorrent et état du swarm
├── telegram.py      alertes et bouton Télécharger
├── security.py      authentification et contrôle d'origine
├── config.py        variables d'environnement
├── database.py      connexion SQLite
├── models.py        tables
├── static/          CSS et JS de la grille
└── templates/       pages HTML
assets/              logo du projet
tests/               tests automatisés
```

</details>

## Avertissement

Ce projet est un outil d'automatisation pour vos propres comptes et instances. Respectez la loi de votre pays et le règlement de vos trackers, et ne téléchargez que des contenus que vous avez le droit d'obtenir. Les seuils C411 viennent de la documentation du site et peuvent changer sans préavis. Le projet n'est pas affilié à C411.

## Licence

[MIT](LICENSE)
