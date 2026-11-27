# Licences Gamer / Voice : clés enregistrées à l'achat et renouvelables

Ce document accompagne le durcissement du serveur de licences (`vtp_web_server.py`).
Il ne contient aucun secret : les noms de variables sont réels, les valeurs sont à
générer par Nicolas.

## Ce qui change

| Avant | Après |
|---|---|
| Une clé inconnue de la base était acceptée si sa signature était bonne, avec la date écrite dans la clé. | Une clé enregistrée en base est la seule référence. La voie « signature » reste ouverte pendant la transition, avec une fin plafonnée à 400 jours, puis se ferme (`KOMMZ_LICENSE_REQUIRE_REGISTERED=1`). |
| Sel Gamer en dur dans le code, sel Voice dans un fichier public, deux clés maîtres en dur. | Plus de sel par défaut, plus de clés maîtres. Sans sel configuré, aucune signature n'est acceptée. |
| Les clés vendues n'étaient qu'envoyées par e-mail. | Make les enregistre avant l'e-mail : `POST /internal/licenses/register`. |
| Un renouvellement ne prolongeait rien : la clé mensuelle expirait au bout de 30 jours. | `POST /internal/licenses/renew` prolonge la **même** clé (la date de la base fait foi, pas celle de la clé). |
| Nombre d'appareils illimité par clé. | 3 appareils différents par clé (`KOMMZ_MAX_DEVICES_PER_KEY`). |
| Pas de limitation de débit sur l'activation. | 30 appels par 10 minutes et par IP (`KOMMZ_LICENSE_RATE_LIMIT_PER_10MIN`). |

## Variables d'environnement (Render)

| Variable | Rôle |
|---|---|
| `KOMMZ_LICENSE_WEBHOOK_SECRET` | Secret des routes `/internal/licenses/*`, 24 caractères minimum, **différent** de celui d'Access. Absent ou trop court : ces routes répondent 503. |
| `DESKTOP_SECRET_SALT` | Sel des clés Gamer (`VTP-`). Plus de valeur par défaut. |
| `VOICE_SECRET_SALT` | Sel des clés Voice (`VCV-`), déjà obligatoire. |
| `KOMMZ_LICENSE_REQUIRE_REGISTERED` | `1` : seules les clés enregistrées passent. Défaut `0`. |
| `KOMMZ_LICENSE_MAX_FUTURE_DAYS` | Fin maximale d'une clé non enregistrée, et plafond des enregistrements et renouvellements. Défaut 400. |
| `KOMMZ_MAX_DEVICES_PER_KEY` | Appareils par clé, `0` = illimité. Défaut 3. |
| `KOMMZ_LICENSE_RATE_LIMIT_PER_10MIN` | Limite par IP des routes publiques de licence. Défaut 30. |

## Base de données (à exécuter AVANT de fusionner)

Dans Supabase, éditeur SQL :

```sql
alter table license_keys add column if not exists registered boolean not null default false;
alter table license_keys add column if not exists expires_at bigint;

create table if not exists license_subscriptions (
  subscription_id text not null,
  key_value       text not null,
  product         text,
  customer_email  text,
  updated_at      timestamptz not null default now(),
  primary key (subscription_id, key_value)
);
alter table license_subscriptions enable row level security;
```

`license_keys.key_value` doit être unique (les activations l'utilisent déjà comme clé
d'`upsert`).

## Ordre de mise en service

1. **Supabase** : exécuter le SQL ci-dessus.
2. **Render** : ajouter `KOMMZ_LICENSE_WEBHOOK_SECRET`. Définir `DESKTOP_SECRET_SALT` à la
   valeur qui figure **aujourd'hui** dans les formules Make (c'était la valeur par défaut du
   code). Sans cela, les clés Gamer vendues et pas encore enregistrées sont refusées dès le
   déploiement. Ne pas toucher à `VOICE_SECRET_SALT`.
3. **Fusionner la PR** (Render redéploie `main` tout de suite).
4. **Make** : ajouter les modules de la section suivante.
5. **Remise à niveau** des abonnés existants (section « Migration »).
6. **Verrouillage** : `KOMMZ_LICENSE_REQUIRE_REGISTERED=1`, puis changer les deux sels à la
   fois dans Render et dans Make (voir plus bas).

## Make : routes d'achat

Sur chacune des 6 routes (Gamer Mensuel/Annuel, Ultimate Mensuel/Annuel, Bundle Voice
Mensuel/Annuel), ajouter un module **HTTP > Make a request** juste **avant** l'e-mail :

- URL : `https://kommzvoice.onrender.com/internal/licenses/register`, méthode `POST`.
- En-tête `X-Kommz-Webhook-Secret` : le nouveau secret.
- Type de corps : Raw, JSON. Cocher « Evaluate all states as errors » : un enregistrement
  raté doit bloquer l'e-mail et apparaître dans les exécutions incomplètes.
- Corps, Gamer seul :
  ```json
  {"email": "{{1.object.customer_details.email}}",
   "subscription_id": "{{1.object.subscription}}",
   "keys": [{"key_value": "{{99.VTPKey}}", "product": "gamer", "expiration": {{98.Timestamp}}}]}
  ```
- Corps, Ultimate et Bundle (deux clés) :
  ```json
  {"email": "{{1.object.customer_details.email}}",
   "subscription_id": "{{1.object.subscription}}",
   "keys": [
     {"key_value": "{{VTPKey}}", "product": "gamer", "expiration": {{Timestamp}}},
     {"key_value": "{{VCVKey}}", "product": "voice", "expiration": {{Timestamp}}}]}
  ```
  Remplacer `{{VTPKey}}`, `{{VCVKey}}`, `{{Timestamp}}` par les variables du module
  `SetVariables` de la même route (pour Ultimate : `106`/`107`, `148`/`149` ; pour Bundle
  Voice : `118`/`119`, `122`/`123` ; pour Gamer : `98`/`99`, `102`/`103`). Dans les formules
  actuelles, `VoiceTimestamp` est identique à `Timestamp` sur Bundle.

## Make : renouvellements

Dans le scénario « Renouvellements d'abonnement » (déclencheur `invoice.paid`, filtre
`billing_reason = subscription_cycle` et `status = paid`), ajouter un second module HTTP vers
`https://kommzvoice.onrender.com/internal/licenses/renew`, avec un **filtre sur le texte de la
ligne de facture** (`{{1.object.lines.data[1].description}}`) :

- contient « Gamer » ou « Ultimate » ou « Voice » : serveur Voice, avec
  `"products": ["gamer"]` pour Gamer seul, `["gamer", "voice"]` pour Ultimate et Bundle ;
- contient « Access » **ou** « Ultimate » : serveur Access (le pack Ultimate contient Access).

Corps :
```json
{"subscription_id": "{{1.object.subscription}}",
 "period_end": {{1.object.lines.data[1].period.end}},
 "customer_email": "{{1.object.customer_email}}",
 "products": ["gamer"]}
```
Sans ce filtre, une facture Gamer pourrait prolonger une clé Access du même e-mail.

## Migration des abonnés existants

Les clés ne sont pas dans Stripe : Make les tire au hasard et ne les garde pas. Les clés déjà
activées sont dans `license_keys` ; c'est elles qu'on remet à niveau.

1. Dans Stripe : Abonnements, filtre « Actifs », Exporter en CSV.
2. Sur le PC de Nicolas (Windows cmd), simulation d'abord :
   ```
   set KOMMZ_LICENSE_WEBHOOK_SECRET=<votre secret>
   python scripts\license_resync.py abonnements.csv
   ```
   Si les noms de colonnes de l'export diffèrent, le script les liste ; les options
   `--col-id`, `--col-email`, `--col-end`, `--col-plan` les corrigent.
3. Même commande avec `--apply` : les clés activées sont enregistrées, reliées à leur
   abonnement et prolongées jusqu'à la fin de période payée. Les abonnés bloqués par une
   clé expirée retrouvent l'accès sans changer de clé.
4. Les lignes « SANS CLE ACTIVEE » sont des abonnés payants qui n'ont jamais activé de
   clé : leur émettre une clé neuve (même scénario Make, enregistrement compris).

Les e-mails sont masqués dans la sortie du script ; le secret n'est jamais affiché.

## Verrouillage

1. `KOMMZ_LICENSE_REQUIRE_REGISTERED=1` dans Render : plus aucune clé non enregistrée ne passe.
2. Changer `DESKTOP_SECRET_SALT` et `VOICE_SECRET_SALT` **en même temps** dans Render et dans
   les formules Make. À ce stade les clés déjà vendues ne dépendent plus des sels.
   L'ancienne procédure de rotation (`docs/security-rotation-runbook.md`) ne s'applique
   qu'après l'étape 1.
3. Les anciens sels figurent dans l'historique git du dépôt public : seule cette rotation
   les rend inoffensifs.
4. Les essais gratuits de 24 h en cours au moment du changement de `DESKTOP_SECRET_SALT` sont
   invalidés (leur signature dépend de ce sel) ; les clients relancent un essai ou en demandent un
   nouveau.

## Vérification en production

1. **Clé forgée (doit être refusée)**, dans cmd :
   ```
   curl -s -X POST https://kommzvoice.onrender.com/license/desktop/activate -H "Content-Type: application/json" -d "{\"license_key\":\"VTP-4102444800-1234-ABCD1234\",\"email\":\"test@example.com\",\"hwid\":\"TESTHWID1\"}"
   ```
   Réponse attendue : `"ok": false` et un code HTTP 403 (ajouter `-w "%{http_code}"`).
2. **Clé réellement achetée** (achat avec un coupon à 100 %, ou le tien) : activation `ok: true`,
   puis dans Supabase la ligne a `registered = true` et `expires_at` non vide.
3. **Journaux Render** à chercher : `[LICENSE] register: 1 cle(s) enregistree(s)` après un
   achat ; `[LICENSE] renew: 1 cle(s) prolongee(s)` après un renouvellement. Une ligne
   `echec DB` indique un souci de colonnes (vérifier le SQL).
4. **Nettoyage** : supprimer la ligne de test dans `license_keys` et `license_subscriptions`,
   annuler l'abonnement de test dans Stripe. La clé forgée du point 1 ne crée aucune ligne.

## Appareils

Quand un client change plusieurs fois de PC, il peut atteindre la limite. Remise à zéro de
ses appareils (Supabase) :
```sql
update license_keys set desktop_hwid = '' where key_value = '<clé du client>';
```
