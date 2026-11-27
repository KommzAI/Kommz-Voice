"""Tests du durcissement des licences (cles enregistrees, renouvellements, appareils).

Aucun secret reel : tous les sels et secrets ci-dessous sont factices. Supabase est
remplace par une base en memoire.
"""
import hashlib
import importlib
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKE_DESKTOP_SALT = "test-desktop-salt-not-a-secret"
FAKE_VOICE_SALT = "test-voice-salt-not-a-secret"
FAKE_WEBHOOK = "test-webhook-secret-0123456789abcdef"
DAY = 86400


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload, self.conflict = "select", None, None
        self.filters, self._limit, self._single = [], None, False

    def select(self, *_a, **_k):
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def upsert(self, row, on_conflict=None):
        self.op, self.payload, self.conflict = "upsert", row, on_conflict
        return self

    def update(self, row):
        self.op, self.payload = "update", row
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def lt(self, col, val):
        self.filters.append(lambda r: (r.get(col) or "") < val)
        return self

    def gte(self, col, val):
        self.filters.append(lambda r: (r.get(col) or "") >= val)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def single(self):
        self._single = True
        return self

    def _match(self, rows):
        return [r for r in rows if all(f(r) for f in self.filters)]

    def execute(self):
        rows = self.db.tables.setdefault(self.name, [])
        if self.op == "insert":
            rows.append(dict(self.payload))
            return _Result([dict(self.payload)])
        if self.op == "upsert":
            cols = [c.strip() for c in (self.conflict or "key_value").split(",")]
            for r in rows:
                if all(r.get(c) == self.payload.get(c) for c in cols):
                    r.update(self.payload)
                    return _Result([dict(r)])
            rows.append(dict(self.payload))
            return _Result([dict(self.payload)])
        if self.op == "update":
            hit = self._match(rows)
            for r in hit:
                r.update(self.payload)
            return _Result([dict(r) for r in hit])
        if self.op == "delete":
            keep = [r for r in rows if r not in self._match(rows)]
            rows[:] = keep
            return _Result([])
        hit = [dict(r) for r in self._match(rows)]
        if self._limit is not None:
            hit = hit[: self._limit]
        if self._single:
            if len(hit) != 1:
                raise Exception("PGRST116: aucune ligne")
            return _Result(hit[0])
        return _Result(hit)


class FakeDB:
    def __init__(self):
        self.tables = {}

    def table(self, name):
        return _Query(self, name)


def load_server(monkeypatch, **env):
    """Charge vtp_web_server avec un environnement donne et une base en memoire."""
    base = {
        "FLASK_ENV": "development",
        "SUPABASE_URL": "https://example.invalid",
        "SUPABASE_KEY": "fake-service-key",
        "SECRET_KEY": "fake-flask-secret",
        "VOICE_SECRET_SALT": FAKE_VOICE_SALT,
        "DESKTOP_SECRET_SALT": FAKE_DESKTOP_SALT,
        "PASSWORD_SALT": "fake-password-salt",
        "ADMIN_SECRET": "fake-admin-secret-long-enough",
        "KOMMZ_LICENSE_WEBHOOK_SECRET": FAKE_WEBHOOK,
        "KOMMZ_LICENSE_RATE_LIMIT_PER_10MIN": "0",
    }
    base.update(env)
    for k in list(base):
        if base[k] is None:
            monkeypatch.delenv(k, raising=False)
            del base[k]
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    db = FakeDB()
    fake = types.ModuleType("supabase")
    fake.create_client = lambda *_a, **_k: db
    fake.Client = object
    monkeypatch.setitem(sys.modules, "supabase", fake)
    monkeypatch.syspath_prepend(str(ROOT))
    sys.modules.pop("vtp_web_server", None)
    mod = importlib.import_module("vtp_web_server")
    mod.app.config["TESTING"] = True
    return mod, db, mod.app.test_client()


def sign(prefix, ts, rand, salt):
    sig = hashlib.sha256(f"{ts}{rand}{salt}".encode()).hexdigest()[:8].upper()
    return f"{prefix}-{ts}-{rand}-{sig}"


def hdr(secret=FAKE_WEBHOOK):
    return {"X-Kommz-Webhook-Secret": secret}


def register(client, key, product, exp, sub="sub_1", email="client@example.invalid", secret=FAKE_WEBHOOK):
    return client.post("/internal/licenses/register", headers=hdr(secret), json={
        "key_value": key, "product": product, "email": email,
        "expiration": exp, "subscription_id": sub})


def activate_gamer(client, key, hwid="AAAAAAAA1", email="client@example.invalid"):
    return client.post("/license/desktop/activate", json={"license_key": key, "email": email, "hwid": hwid})


# --- Cles maitres et sels -----------------------------------------------------------------

def test_master_keys_are_refused(monkeypatch):
    srv, _db, client = load_server(monkeypatch)
    for key in ("VTP-VOICE-ADMIN", "NICOLAS-VOICE-PRO"):
        assert srv.verify_vcv_key(key)["valid"] is False
        resp = client.post("/license/voice/verify-web", json={"license_key": key})
        assert resp.get_json()["valid"] is False


def test_no_default_salt_means_no_signature_accepted(monkeypatch):
    srv, _db, client = load_server(monkeypatch, DESKTOP_SECRET_SALT=None)
    assert srv.DESKTOP_SECRET_SALT == ""
    ts = int(time.time()) + 30 * DAY
    for forged_salt in ("", "ancien-sel-par-defaut-factice"):
        key = sign("VTP", ts, "1234", forged_salt)
        assert srv.verify_vtp_key(key)["valid"] is False
        assert activate_gamer(client, key).status_code == 403


def test_empty_voice_salt_refuses_signed_keys(monkeypatch):
    srv, _db, _c = load_server(monkeypatch, FLASK_ENV="development", VOICE_SECRET_SALT="")
    key = sign("VCV", int(time.time()) + DAY, "1234", "")
    assert srv.verify_vcv_key(key)["valid"] is False


# --- Voie signature (heritage) : plafond de date ---------------------------------------------

def test_signed_unknown_key_within_cap_is_accepted(monkeypatch):
    _srv, _db, client = load_server(monkeypatch)
    key = sign("VTP", int(time.time()) + 30 * DAY, "1234", FAKE_DESKTOP_SALT)
    assert activate_gamer(client, key).status_code == 200


def test_signed_unknown_key_beyond_cap_is_refused(monkeypatch):
    _srv, _db, client = load_server(monkeypatch)
    far = sign("VTP", int(time.time()) + 3000 * DAY, "1234", FAKE_DESKTOP_SALT)  # ~ annee 2034
    assert activate_gamer(client, far).status_code == 403
    year_2100 = sign("VTP", 4102444800, "1234", FAKE_DESKTOP_SALT)
    assert activate_gamer(client, year_2100).status_code == 403


def test_require_registered_ignores_signature(monkeypatch):
    _srv, _db, client = load_server(monkeypatch, KOMMZ_LICENSE_REQUIRE_REGISTERED="1")
    key = sign("VTP", int(time.time()) + 30 * DAY, "1234", FAKE_DESKTOP_SALT)
    assert activate_gamer(client, key).status_code == 403
    exp = int(time.time()) + 30 * DAY
    assert register(client, key, "gamer", exp).status_code == 200
    assert activate_gamer(client, key).status_code == 200


# --- Routes internes : securite -----------------------------------------------------------------

def test_internal_routes_503_without_secret(monkeypatch):
    _srv, _db, client = load_server(monkeypatch, KOMMZ_LICENSE_WEBHOOK_SECRET=None)
    assert register(client, "VTP-ABCDEFGH", "gamer", int(time.time()) + DAY, secret="x" * 40).status_code == 503


def test_internal_routes_503_with_short_secret(monkeypatch):
    _srv, _db, client = load_server(monkeypatch, KOMMZ_LICENSE_WEBHOOK_SECRET="trop-court")
    assert register(client, "VTP-ABCDEFGH", "gamer", int(time.time()) + DAY, secret="trop-court").status_code == 503


def test_internal_routes_reject_wrong_or_missing_secret(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    exp = int(time.time()) + DAY
    assert register(client, "VTP-ABCDEFGH", "gamer", exp, secret="mauvais").status_code == 401
    assert client.post("/internal/licenses/register", json={}).status_code == 401
    assert client.post("/internal/licenses/renew", json={}).status_code == 401
    assert db.tables.get("license_keys", []) == []


def test_internal_routes_block_after_20_failures(monkeypatch):
    _srv, _db, client = load_server(monkeypatch)
    for _ in range(20):
        assert client.post("/internal/licenses/renew", headers=hdr("mauvais"), json={}).status_code == 401
    assert client.post("/internal/licenses/renew", headers=hdr("mauvais"), json={}).status_code == 429
    # Meme avec le bon secret, l'appelant reste bloque jusqu'a la fin de la fenetre.
    assert client.post("/internal/licenses/renew", headers=hdr(), json={}).status_code == 429


# --- Enregistrement a l'achat ---------------------------------------------------------------

def test_register_is_idempotent_and_keeps_activation(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    exp = int(time.time()) + 30 * DAY
    key = sign("VTP", exp, "1234", "autre-sel")
    assert register(client, key, "gamer", exp).status_code == 200
    assert activate_gamer(client, key, hwid="AAAAAAAA1").status_code == 200
    assert register(client, key, "gamer", exp).status_code == 200
    rows = [r for r in db.tables["license_keys"] if r["key_value"] == key]
    assert len(rows) == 1
    assert rows[0]["is_activated"] is True and rows[0]["activated_by_email"] == "client@example.invalid"
    assert rows[0]["desktop_hwid"] == "AAAAAAAA1"
    assert len(db.tables["license_subscriptions"]) == 1


def test_register_never_shortens(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    later, sooner = int(time.time()) + 300 * DAY, int(time.time()) + 30 * DAY
    key = "VTP-AAAA-BBBB-CCCC"
    register(client, key, "gamer", later)
    register(client, key, "gamer", sooner)
    assert db.tables["license_keys"][0]["expires_at"] == later


def test_register_validation(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    exp = int(time.time()) + 30 * DAY
    assert register(client, "VCV-ABCDEFGH", "gamer", exp).status_code == 400      # prefixe != produit
    assert register(client, "KOMMZ-ABCDEFGH", "access", exp).status_code == 400   # produit inconnu
    assert register(client, "VTP-AB", "gamer", exp).status_code == 400            # trop court
    assert register(client, "VTP-ABCDEFGH", "gamer", 4102444800).status_code == 400  # annee 2100
    assert register(client, "VTP-ABCDEFGH", "gamer", "demain").status_code == 400
    assert register(client, "VTP-ABCDEFGH", "gamer", True).status_code == 400
    assert db.tables.get("license_keys", []) == []


def test_register_several_keys_is_all_or_nothing(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    exp = int(time.time()) + 30 * DAY
    resp = client.post("/internal/licenses/register", headers=hdr(), json={
        "email": "client@example.invalid", "subscription_id": "sub_u",
        "keys": [{"key_value": "VTP-AAAA-1111-ZZZZ", "product": "gamer", "expiration": exp},
                 {"key_value": "VCV-AAAA-1111-ZZZZ", "product": "gamer", "expiration": exp}]})
    assert resp.status_code == 400 and db.tables.get("license_keys", []) == []
    resp = client.post("/internal/licenses/register", headers=hdr(), json={
        "email": "client@example.invalid", "subscription_id": "sub_u",
        "keys": [{"key_value": "VTP-AAAA-1111-ZZZZ", "product": "gamer", "expiration": exp},
                 {"key_value": "VCV-AAAA-1111-ZZZZ", "product": "voice", "expiration": exp}]})
    assert resp.status_code == 200 and len(db.tables["license_subscriptions"]) == 2


def test_registered_key_uses_database_date_not_key_date(monkeypatch):
    srv, db, client = load_server(monkeypatch, KOMMZ_LICENSE_REQUIRE_REGISTERED="1")
    old = int(time.time()) - 5 * DAY                                  # date lue dans la cle : passee
    key = sign("VTP", old, "1234", FAKE_DESKTOP_SALT)
    db.tables["license_keys"] = [{"key_value": key, "product": "gamer", "registered": True,
                                  "expires_at": int(time.time()) + 20 * DAY, "is_activated": False}]
    assert activate_gamer(client, key).status_code == 200


def test_expired_registered_key_is_refused(monkeypatch):
    _srv, db, client = load_server(monkeypatch, KOMMZ_LICENSE_REQUIRE_REGISTERED="1")
    db.tables["license_keys"] = [{"key_value": "VTP-AAAA-1111-ZZZZ", "product": "gamer", "registered": True,
                                  "expires_at": int(time.time()) - 60, "is_activated": True}]
    assert activate_gamer(client, "VTP-AAAA-1111-ZZZZ").status_code == 403


# --- Renouvellements ---------------------------------------------------------------------------

def test_renew_extends_all_keys_of_a_subscription(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    soon = int(time.time()) + DAY
    client.post("/internal/licenses/register", headers=hdr(), json={
        "email": "client@example.invalid", "subscription_id": "sub_u",
        "keys": [{"key_value": "VTP-AAAA-1111-ZZZZ", "product": "gamer", "expiration": soon},
                 {"key_value": "VCV-AAAA-1111-ZZZZ", "product": "voice", "expiration": soon}]})
    end = int(time.time()) + 31 * DAY
    resp = client.post("/internal/licenses/renew", headers=hdr(), json={
        "subscription_id": "sub_u", "period_end": end, "customer_email": "client@example.invalid"})
    assert resp.status_code == 200 and resp.get_json()["updated"] == 2
    assert {r["expires_at"] for r in db.tables["license_keys"]} == {end}


def test_renew_never_shortens(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    later = int(time.time()) + 300 * DAY
    register(client, "VTP-AAAA-1111-ZZZZ", "gamer", later, sub="sub_1")
    resp = client.post("/internal/licenses/renew", headers=hdr(), json={
        "subscription_id": "sub_1", "period_end": int(time.time()) + 30 * DAY})
    assert resp.status_code == 200
    assert db.tables["license_keys"][0]["expires_at"] == later


def test_renew_revives_an_expired_key(monkeypatch):
    _srv, _db, client = load_server(monkeypatch, KOMMZ_LICENSE_REQUIRE_REGISTERED="1")
    register(client, "VTP-AAAA-1111-ZZZZ", "gamer", int(time.time()) + 60, sub="sub_1")
    _db.tables["license_keys"][0]["expires_at"] = int(time.time()) - 10
    assert activate_gamer(client, "VTP-AAAA-1111-ZZZZ").status_code == 403
    client.post("/internal/licenses/renew", headers=hdr(), json={
        "subscription_id": "sub_1", "period_end": int(time.time()) + 30 * DAY})
    assert activate_gamer(client, "VTP-AAAA-1111-ZZZZ").status_code == 200


def test_renew_falls_back_on_activation_email_and_links_subscription(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    old_key = sign("VTP", int(time.time()) - 5 * DAY, "1234", FAKE_DESKTOP_SALT)   # ancienne cle expiree
    db.tables["license_keys"] = [
        {"key_value": old_key, "product": "gamer", "is_activated": True,
         "activated_by_email": "client@example.invalid"},
        {"key_value": "VCV-OTHER-1111-ZZZZ", "product": "voice", "is_activated": True,
         "activated_by_email": "client@example.invalid"},
    ]
    end = int(time.time()) + 31 * DAY
    resp = client.post("/internal/licenses/renew", headers=hdr(), json={
        "subscription_id": "sub_old", "period_end": end,
        "customer_email": "Client@Example.invalid", "products": ["gamer"]})
    assert resp.status_code == 200 and resp.get_json()["updated"] == 1
    gamer = next(r for r in db.tables["license_keys"] if r["key_value"] == old_key)
    voice = next(r for r in db.tables["license_keys"] if r["key_value"] == "VCV-OTHER-1111-ZZZZ")
    assert gamer["registered"] is True and gamer["expires_at"] == end
    assert "registered" not in voice                      # un autre produit n'est pas touche
    assert db.tables["license_subscriptions"][0]["subscription_id"] == "sub_old"


def test_renew_without_products_has_no_email_fallback(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    db.tables["license_keys"] = [{"key_value": "VTP-AAAA-1111-ZZZZ", "product": "gamer",
                                  "is_activated": True, "activated_by_email": "client@example.invalid"}]
    resp = client.post("/internal/licenses/renew", headers=hdr(), json={
        "subscription_id": "sub_x", "period_end": int(time.time()) + DAY,
        "customer_email": "client@example.invalid"})
    assert resp.status_code == 404


def test_renew_validation(monkeypatch):
    _srv, _db, client = load_server(monkeypatch)
    for body in ({}, {"subscription_id": "s", "period_end": "x"}, {"subscription_id": "s", "period_end": 4102444800},
                 {"period_end": int(time.time()) + DAY}):
        assert client.post("/internal/licenses/renew", headers=hdr(), json=body).status_code == 400


# --- Produits et appareils -----------------------------------------------------------------------

def test_gamer_key_cannot_be_used_as_voice_key(monkeypatch):
    _srv, _db, client = load_server(monkeypatch)
    exp = int(time.time()) + 30 * DAY
    register(client, "VTP-AAAA-1111-ZZZZ", "gamer", exp)
    resp = client.post("/license/voice/activate-desktop",
                       json={"license_key": "VTP-AAAA-1111-ZZZZ", "email": "client@example.invalid", "hwid": "AAAAAAAA1"})
    assert resp.status_code == 403
    resp = client.post("/license/voice/verify-web", json={"license_key": "VTP-AAAA-1111-ZZZZ"})
    assert resp.get_json()["valid"] is False


def test_device_cap(monkeypatch):
    _srv, db, client = load_server(monkeypatch)
    exp = int(time.time()) + 30 * DAY
    key = "VTP-AAAA-1111-ZZZZ"
    register(client, key, "gamer", exp)
    for hw in ("AAAAAAAA1", "AAAAAAAA2", "AAAAAAAA3"):
        assert activate_gamer(client, key, hwid=hw).status_code == 200
    assert activate_gamer(client, key, hwid="AAAAAAAA4").status_code == 409
    assert activate_gamer(client, key, hwid="AAAAAAAA2").status_code == 200       # appareil deja connu
    assert db.tables["license_keys"][0]["desktop_hwid"] == "AAAAAAAA1,AAAAAAAA2,AAAAAAAA3"


def test_device_cap_can_be_disabled(monkeypatch):
    _srv, _db, client = load_server(monkeypatch, KOMMZ_MAX_DEVICES_PER_KEY="0")
    register(client, "VTP-AAAA-1111-ZZZZ", "gamer", int(time.time()) + DAY)
    for i in range(6):
        assert activate_gamer(client, "VTP-AAAA-1111-ZZZZ", hwid=f"HWID000{i}").status_code == 200


def test_key_stays_bound_to_first_email(monkeypatch):
    _srv, _db, client = load_server(monkeypatch)
    register(client, "VTP-AAAA-1111-ZZZZ", "gamer", int(time.time()) + DAY)
    assert activate_gamer(client, "VTP-AAAA-1111-ZZZZ").status_code == 200
    assert activate_gamer(client, "VTP-AAAA-1111-ZZZZ", email="autre@example.invalid").status_code == 409


def test_public_rate_limit(monkeypatch):
    _srv, _db, client = load_server(monkeypatch, KOMMZ_LICENSE_RATE_LIMIT_PER_10MIN="3")
    codes = [client.post("/license/voice/verify-web", json={"license_key": "VCV-X"}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_prefix_is_checked_even_for_registered_rows_without_product(monkeypatch):
    srv, db, _client = load_server(monkeypatch, KOMMZ_LICENSE_REQUIRE_REGISTERED="1")
    db.tables["license_keys"] = [{"key_value": "VTP-AAAA-1111-ZZZZ", "product": "", "registered": True,
                                  "expires_at": int(time.time()) + DAY}]
    assert srv.resolve_license("VTP-AAAA-1111-ZZZZ", "voice")["valid"] is False
    assert srv.resolve_license("VTP-AAAA-1111-ZZZZ", "gamer")["valid"] is True
