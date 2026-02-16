# Bot Discord AlpInn (Python)

Bot Discord pour recuperer et afficher les donnees de l'API AlpInn avec:
- Authentification `X-API-Key`
- Catalogue d'endpoints predefinis
- Cooldown global API: **1 requete toutes les 60 secondes**
- Affichage stylise en Markdown (resume lisible, pas uniquement JSON brut)
- Affichage d'image automatique si une URL d'image est presente dans les donnees API
- Commandes de configuration directement depuis Discord
- Endpoint `news`: un message par article (titre + texte + lien)
- Endpoint `association`: plusieurs messages (un par section utile)

## Installation

1. Creer un environnement virtuel:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Installer les dependances:
```powershell
pip install -r requirements.txt
```

3. Configurer le token Discord:
```powershell
Copy-Item .env.example .env
```
Puis renseigner `DISCORD_BOT_TOKEN` dans `.env`.
Puis ajouter aussi `ALPINN_API_KEY` dans `.env`.

4. Lancer le bot:
```powershell
python bot.py
```

## Version Python recommandee

- Recommande: **Python 3.12**
- Python 3.13/3.14: installer les dependances avec `requirements.txt` (inclut `audioop-lts` pour compatibilite).

## Commandes Discord

- `!help` : aide
- `!set_base_url <url>` : definir l'URL de base API (admin)
- `!set_channel <endpoint> <#salon>` : ajouter un salon a un endpoint (admin)
- `!unset_channel <endpoint> [#salon]` : retirer un salon precis ou tous les salons d'un endpoint (admin)
- `!enable_endpoint <endpoint>` : activer l'auto-affichage d'un endpoint (admin)
- `!disable_endpoint <endpoint>` : desactiver l'auto-affichage d'un endpoint (admin)
- `!enable_all_endpoints` : activer l'auto-affichage de tous les endpoints ayant un salon configure (admin)
- `!refresh_all_now` : forcer une passe immediate sur tous les jobs endpoint/salon actifs (admin, 1 requete/60s)
- `!enable_news` : activer l'auto-affichage de `news` (admin)
- `!disable_news` : desactiver l'auto-affichage de `news` (admin)
- `!auto_status` : afficher les endpoints en auto-refresh
- `!clear <#salon|all>` : supprimer les messages auto suivis dans un salon ou partout (admin)
- `!reboot` : redemarrer le bot (admin)
- `!show_channels` : afficher les associations endpoint/salon
- `!show_config` : afficher la configuration actuelle (la cle n'est jamais affichee)
- `!endpoints` : afficher le catalogue endpoints
- `!list_endpoints` : alias de `!endpoints`
- `!fetch <endpoint> [k=v ...]` : appel generique (endpoint parmi `association`, `news`, `statuts`, `staff`, `activities`, `events`)
- `!association`
- `!news [k=v ...]`
- `!statuts [k=v ...]`
- `!staff [k=v ...]`
- `!activities [k=v ...]`
- `!events [k=v ...]`

## Exemples

```text
!set_base_url http://localhost/alpinn.ch_dynamic/public
!set_channel news #annonces
!set_channel news #news-backup
!enable_news
!set_channel events #agenda
!enable_endpoint events
!enable_all_endpoints
!refresh_all_now
!clear #annonces
!clear all
!auto_status
!news limit=5 page=1 sort=desc
!events date_from=2026-01-01 status=active
!fetch activities category=randonnee limit=3
```

## Notes

- Le cooldown est global au bot (pas par utilisateur).
- La configuration est stockee dans `bot_config.json`.
- La cle API n'est jamais affichable via commande Discord.
- La cle API se configure uniquement en local via variable d'environnement `ALPINN_API_KEY`.
- Le bot est reserve aux administrateurs du serveur (utilisateurs normaux bloques).
- Si un endpoint est associe a un salon, les commandes de cet endpoint ne fonctionnent que dans ce salon.
- Un endpoint peut etre associe a plusieurs salons.
- Mode auto-refresh:
  - Le bot traite **1 job endpoint+salon toutes les 60 secondes**.
  - Le bot fait **une requete API par job** (pas de regroupement multi-salons dans une meme requete).
  - Le bot compare les donnees collectees avec la derniere version envoyee et met a jour le message seulement si le contenu a change.
  - Le bot modifie le message precedent dans le salon cible (si possible), sinon il recree un message.
  - Pour `news`, le bot gere plusieurs messages (un par article) et supprime les messages d'articles disparus.
  - Pour `association`, le bot gere plusieurs messages (un par section) et supprime les sections disparues.
  - Il n'est pas possible d'interroger tous les endpoints "en une seule seconde" si l'API impose 1 requete/minute globale.

### Nettoyage et conflits

- `!clear #salon` :
  - supprime les messages auto suivis dans ce salon
  - nettoie uniquement les signatures/messages suivis lies a ces messages dans `bot_config.json`
  - ne modifie pas les autres informations (channels, endpoints actifs, etc.)
- `!clear all` :
  - supprime tous les messages auto suivis
  - nettoie uniquement toutes les donnees de suivi des messages dans `bot_config.json`
  - ne modifie pas les autres informations (channels, endpoints actifs, etc.)

## Liste des endpoints actuels

- `association` -> `http://localhost/alpinn.ch_dynamic/public/api/v1/association.php`
- `news` -> `http://localhost/alpinn.ch_dynamic/public/api/v1/news.php`
- `statuts` -> `http://localhost/alpinn.ch_dynamic/public/api/v1/statuts.php`
- `staff` -> `http://localhost/alpinn.ch_dynamic/public/api/v1/staff.php`
- `activities` -> `http://localhost/alpinn.ch_dynamic/public/api/v1/activities.php`
- `events` -> `http://localhost/alpinn.ch_dynamic/public/api/v1/events.php`

## Permissions Discord necessaires

### Permissions bot (recommandees, minimum)

- `View Channels`
- `Send Messages`
- `Read Message History`
- `Pin Messages`
- `Bypass Slowmode`
- `Embed Links`





### Permission optionnelle

- `Embed Links` : utile uniquement si tu modifies le bot pour envoyer des embeds.

### Permissions non necessaires pour cette version

- Toutes les permissions vocales (`Connect`, `Speak`, etc.)
- Permissions de moderation (`Kick Members`, `Ban Members`, etc.)
- Permissions admin globales (`Administrator`, `Manage Server`, etc.)
- `Use Slash Commands` (le bot actuel utilise des commandes prefixees `!`)

### Important (portail Discord Developer)

Dans l'application du bot, active aussi **MESSAGE CONTENT INTENT** (Privileged Gateway Intents), sinon les commandes `!` ne fonctionneront pas.

## Depannage

Si tu vois l'erreur `ModuleNotFoundError: No module named 'audioop'`:

1. Mets a jour les dependances:
```powershell
pip install -r requirements.txt
```
2. Si l'erreur reste presente, recree l'environnement avec Python 3.12:
```powershell
deactivate
Remove-Item -Recurse -Force .venv
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```
