## Kommz Voice

Kommz Voice est la brique moteur de l'écosystème Kommz.

Ce repo regroupe la partie vocale et services autour de :
- la synthèse vocale temps réel
- les moteurs XTTS et GPT-SoVITS
- la transcription Whisper
- les services backend liés aux usages temps réel

[Français](README.md) | [English](README.fr.md)

Kommz Voice est la **brique moteur vocale** de l'écosystème Kommz.
Elle alimente les flux de **transcription**, **traduction** (via client) et **synthèse vocale** utilisés en temps réel par les applications clientes (ex: *Kommz Gamer Community*).

## Ce que fait le projet

<<<<<<< Updated upstream
- XTTS pour la synthèse multilingue
- GPT-SoVITS pour le timbre et le style vocal (Hybrid)
- Whisper pour la transcription
- API serveur pour l'intégration côté client
- Routage et fallback selon la disponibilité des moteurs
=======
L'objectif de Kommz Voice est de proposer une base plus claire, plus modulaire et plus robuste pour alimenter l'écosystème vocal autour de Kommz.

### Rôle dans l'écosystème
- `Kommz Gamer Community` : client desktop open source
- `Kommz Voice` : moteurs et services vocaux
- `Discord` : communauté, support, retours
- `Patreon` : soutien au développement et avantages liés à l'écosystème

### Priorités actuelles
- fiabilisation des moteurs vocaux
- amélioration des fallbacks et du routage
- meilleure structure documentaire
- meilleure séparation entre client et brique moteur
- amélioration du support pour les usages temps réel

### Liens
- Community : https://github.com/Kommz-Gamer/Kommz-Gamer
- Discord : https://discord.gg/uv25d6uGKZ
- Patreon : https://www.patreon.com/KommzInnovations
- Site : https://kommz.fr


## Ecosystem positioning
>>>>>>> Stashed changes

## Rôle dans l'écosystème

- Client / communauté open source : `Kommz Gamer Community` (repo séparé)
- Brique moteur : `Kommz Voice` (ce repo)
- Support & communauté : Discord + Patreon

## Références

- Architecture : `docs/architecture-overview.md`
- Guidelines messages : `docs/message-guidelines.md`
- Sécurité et rotation : `docs/security-rotation-runbook.md`
- Checklist de release : `docs/release-security-checklist.md`
- Roadmap : `ROADMAP.md`

## Démarrage rapide

1. Copier `env.template` vers `.env`.
2. Renseigner uniquement les secrets nécessaires.
3. Copier `settings.example.json` vers `settings.json` pour la config locale.
4. Ne jamais committer `.env` ni `settings.json`.
5. Installer les dépendances :
   - `pip install -r requirements.txt`
6. Lancer le serveur :
   - `python vtp_web_server.py`

## Sécurité

- Hooks recommandés :
  - `pip install pre-commit detect-secrets`
  - `pre-commit install`
- Baseline secrets :
  - `detect-secrets scan > .secrets.baseline`
- Pour la rotation ou la purge d'historique Git :
  - `scripts/purge-git-history.ps1`

`vtp_web_server.py` applique des vérifications fail-fast en production quand des secrets critiques sont absents ou invalides.

## Contribuer / Support

- Contribuer : `CONTRIBUTING.md`
- Support : `SUPPORT.md`
- Politique de sécurité : `SECURITY.md` / `SECURITY.fr.md`

## Liens publics

- GitHub Communauté : https://github.com/Kommz-Gamer/Kommz-Gamer
- Discord : https://discord.gg/uv25d6uGKZ
- Patreon : https://www.patreon.com/KommzInnovations
- Site : https://kommz.fr

## Contribuer / Support

- Contribuer : `CONTRIBUTING.md`
- Support : `SUPPORT.md`

## Releases

Pour standardiser les notes de version :

- Français : `release-template.fr.md`
- English : `release-template.en.md`
