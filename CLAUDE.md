# Kommz Voice — instructions pour Claude Code

Backend moteur vocal de l'écosystème Kommz : synthèse (XTTS, GPT-SoVITS),
transcription (Whisper) et serveur d'orchestration.

- `modal_xtts.py`, `modal_gptsovits.py`, `modal_whisper.py` : applications
  Modal (GPU)
- `vtp_web_server.py` : serveur Flask hébergé sur Render

Ce dépôt est **public**. Tout ce qui y est poussé est visible par tous.

## Règle absolue : ne jamais pousser sur `main`

**Render redéploie automatiquement la branche `main` en production.** Un push
sur `main` met en ligne le serveur immédiatement, sans relecture.

- Travaille toujours sur une branche dédiée.
- Ouvre une pull request et laisse Nicolas la relire et la fusionner.
- Ne fusionne jamais toi-même dans `main`.

## Déploiements

- **Render** : automatique à chaque fusion dans `main`. Voir la règle ci-dessus.
- **Modal** : manuel, par Nicolas, avec `modal deploy <fichier>`. Tu ne peux
  pas déployer sur Modal depuis ici, et tu ne dois pas essayer.
- Les variables `XTTS_MIN_CONTAINERS`, `XTTS_MAX_CONTAINERS`,
  `XTTS_IDLE_TIMEOUT`, `XTTS_MEMORY_SNAPSHOT`, `XTTS_WARMUP_REQUIRE_KEY` et
  `XTTS_INFER_REQUIRE_KEY` sont lues par `os.environ` **au moment du `modal deploy`, sur la machine qui
  déploie**, pas dans le conteneur. Un secret Modal ne les modifie pas. À
  chaque deploy, ré-exporter toutes celles qui ne sont pas à leur valeur par
  défaut.
- Chaque `modal deploy` invalide l'instantané mémoire : le premier démarrage
  qui suit le refabrique. Un temps de démarrage mesuré juste après un
  déploiement n'est pas représentatif.

## Secrets

Les secrets vivent uniquement dans les variables d'environnement de Render et
dans les secrets Modal. N'en écris jamais dans un fichier, un exemple, un test
ou un message de commit. Ne demande jamais de clé à Nicolas. Pour tout exemple
de configuration, utilise des valeurs factices évidentes.

## Ce que cet environnement ne peut pas faire

Tu n'as accès ni à la production Render, ni à Modal, ni aux journaux de
production. Vérifie ce qui peut l'être (`python -m py_compile`, lecture du
code, tests locaux ciblés) et indique précisément ce que Nicolas devra
vérifier après déploiement : quelles lignes chercher dans les journaux, et
quelle valeur attendre.

## Méthode

Mesurer avant de conclure. Plusieurs diagnostics sur ce backend se sont
révélés faux une fois les journaux obtenus. Pour un bug, commence par trouver
et expliquer la cause, puis propose la correction avant de modifier le code.
