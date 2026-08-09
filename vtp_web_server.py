"""
=============================================================================
  KOMMZ VOICE â€” WEB SERVER (vtp_web_server.py)
  Backend Flask pour le site de clonage vocal (hÃ©bergÃ© sur Render.com)
=============================================================================

ARCHITECTURE :
  Client (index.html)  â†’  Flask (vtp_web_server.py)  â†’  Supabase (DB + Storage)
                                      â†“
                              Modal.run (Whisper + XTTS v2)

ROUTES :
  AUTH         POST /login              â€” Connexion utilisateur
               POST /register           â€” Inscription (nÃ©cessite licence VCV-)
               GET  /logout             â€” DÃ©connexion
               GET  /me                 â€” Info utilisateur courant

  LICENCE      POST /license/voice/verify-web  â€” VÃ©rification clÃ© VCV- (avant inscription)

  PROFILS      GET  /api/profiles               â€” Liste des profils de l'utilisateur
               POST /api/profiles               â€” Sauvegarde un nouveau clone vocal
               DELETE /api/voices/delete/<id>   â€” Supprime un profil

  FICHIERS     POST /api/upload-reference       â€” Upload fichier audio de rÃ©fÃ©rence
               POST /api/transcribe/<file_id>   â€” Transcription Whisper via Modal

  GÃ‰NÃ‰RATION   POST /api/generate               â€” GÃ©nÃ©ration vocale XTTS via Modal

INSTALLATION :
  pip install flask flask-session supabase python-dotenv requests gunicorn

VARIABLES D'ENVIRONNEMENT (.env) :
  SUPABASE_URL=https://YOUR_PROJECT.supabase.co
  SUPABASE_KEY=CHANGE_ME_SUPABASE_SERVICE_ROLE   (jamais cÃ´tÃ© client)
  SUPABASE_ANON_KEY=CHANGE_ME_SUPABASE_ANON      (optionnel)
  MODAL_WHISPER_URL=https://votre-app--whisper.modal.run
  MODAL_XTTS_URL=https://votre-app--kommz-voice-xtts.modal.run
  SECRET_KEY=CHANGE_ME_FLASK_SECRET
  VOICE_SECRET_SALT=CHANGE_ME_VOICE_SALT

=============================================================================
"""

import os
import uuid
import hashlib
import hmac
import time
import re
import ipaddress
import tempfile
import base64
import json as pyjson
import requests
import threading
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
from flask import (
    Flask, request, jsonify, session,
    send_from_directory, render_template_string, redirect, Response
)
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================

FLASK_ENV = os.environ.get("FLASK_ENV", "production").lower()
IS_PRODUCTION = FLASK_ENV == "production"

# Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

# Modal endpoints
MODAL_WHISPER_URL = os.environ.get(
    "MODAL_WHISPER_URL",
    os.environ.get("MODAL_URL", "https://your-app--whisper-transcribe.modal.run"),
).strip().rstrip("/")
MODAL_XTTS_URL = os.environ.get(
    "MODAL_XTTS_URL",
    "https://your-app--kommz-voice-xtts.modal.run",
).strip().rstrip("/")
MODAL_XTTS_WARMUP_URL = os.environ.get("MODAL_XTTS_WARMUP_URL", "").strip().rstrip("/")
MODAL_GPTSOVITS_URL = os.environ.get(
    "MODAL_GPTSOVITS_URL",
    "https://your-app--kommz-voice-gptsovits.modal.run",
).strip().rstrip("/")
MODAL_GPTSOVITS_WARMUP_URL = os.environ.get("MODAL_GPTSOVITS_WARMUP_URL", "").strip().rstrip("/")
XTTS_KEEPALIVE_ENABLED = os.environ.get("XTTS_KEEPALIVE_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
XTTS_KEEPALIVE_INTERVAL_SECONDS = max(60, int(os.environ.get("XTTS_KEEPALIVE_INTERVAL_SECONDS", "240")))
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "voice-references").strip() or "voice-references"
STORAGE_ALLOW_PUBLIC_FALLBACK = os.environ.get("STORAGE_ALLOW_PUBLIC_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"}

# Fish Audio API
FISH_API_KEY = os.environ.get("FISH_API_KEY", "").strip()
FISH_AUDIO_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_AUDIO_MODEL_URL = "https://api.fish.audio/v1/models"


def _derive_modal_endpoint(base_url: str, target: str) -> str:
    """Supporte URL Modal de type function (-clone.modal.run) et path (/clone)."""
    target = (target or "").strip().lower()
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return ""
    try:
        sp = urlsplit(base_url)
        host = (sp.netloc or "").strip()
        path = (sp.path or "").rstrip("/")
        if host.endswith(".modal.run"):
            for suffix in ("-clone.modal.run", "-warmup.modal.run", "-health.modal.run", "-generate.modal.run", "-tts.modal.run", "-transcribe.modal.run"):
                if host.endswith(suffix):
                    host = host[: -len(suffix)] + f"-{target}.modal.run"
                    return urlunsplit((sp.scheme or "https", host, "", "", ""))
        if path.endswith("/clone"):
            path = path[:-6] + f"/{target}"
        elif path.endswith("/generate"):
            path = path[:-9] + f"/{target}"
        elif path.endswith("/transcribe"):
            path = path[:-11] + f"/{target}"
        elif path.endswith("/warmup") or path.endswith("/health") or path.endswith("/tts"):
            path = path.rsplit("/", 1)[0] + f"/{target}"
        else:
            path = f"{path}/{target}" if path else f"/{target}"
        return urlunsplit((sp.scheme or "https", sp.netloc, path, "", ""))
    except Exception:
        return f"{base_url}/{target}"


def _get_xtts_clone_url() -> str:
    return _derive_modal_endpoint(MODAL_XTTS_URL, "clone")


def _get_xtts_health_url() -> str:
    return _derive_modal_endpoint(MODAL_XTTS_URL, "health")


def _get_gptsovits_tts_url() -> str:
    return _derive_modal_endpoint(MODAL_GPTSOVITS_URL, "tts")


def _get_gptsovits_health_url() -> str:
    return _derive_modal_endpoint(MODAL_GPTSOVITS_URL, "health")

# Secrets
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
VOICE_SECRET_SALT = os.environ.get("VOICE_SECRET_SALT", "").strip()
DESKTOP_SECRET_SALT = os.environ.get("DESKTOP_SECRET_SALT", "VTP-2025-MAKE-AUTOMATION-X99").strip()
PASSWORD_SALT = os.environ.get("PASSWORD_SALT", "").strip()
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "").strip()
SECURE_TTS_OWNER_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("SECURE_TTS_OWNER_EMAILS", "").split(",")
    if e.strip()
}

# Trial config (free plan hard limits)
TRIAL_WINDOW_HOURS = int(os.environ.get("TRIAL_WINDOW_HOURS", "24"))
TRIAL_MAX_AUDIO_SECONDS = int(os.environ.get("TRIAL_MAX_AUDIO_SECONDS", "1800"))  # 30 min
TRIAL_MAX_GENERATIONS = int(os.environ.get("TRIAL_MAX_GENERATIONS", "120"))
TRIAL_REGISTER_RATE_LIMIT_PER_10MIN = int(os.environ.get("TRIAL_REGISTER_RATE_LIMIT_PER_10MIN", "6"))
TRIAL_ACTIVATE_RATE_LIMIT_PER_10MIN = int(os.environ.get("TRIAL_ACTIVATE_RATE_LIMIT_PER_10MIN", "12"))
TRIAL_GUARD_WINDOW_SECONDS = int(os.environ.get("TRIAL_GUARD_WINDOW_SECONDS", "600"))
TRIAL_GUARD_ENABLED = os.environ.get("TRIAL_GUARD_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}

# Desktop update channel (Kommz Gamer)
DESKTOP_STABLE_VERSION = os.environ.get("DESKTOP_STABLE_VERSION", "4.1").strip()
DESKTOP_DOWNLOAD_URL = os.environ.get("DESKTOP_DOWNLOAD_URL", "").strip()
DESKTOP_CHANGELOG_URL = os.environ.get("DESKTOP_CHANGELOG_URL", "").strip()
DESKTOP_DOWNLOAD_SHA256 = os.environ.get("DESKTOP_DOWNLOAD_SHA256", "").strip().lower()
DESKTOP_FORCE_UPDATE = os.environ.get("DESKTOP_FORCE_UPDATE", "0").strip() in {"1", "true", "yes", "on"}
DESKTOP_MINIMUM_VERSION = os.environ.get("DESKTOP_MINIMUM_VERSION", "").strip()
_UPDATE_CHANGELOG_CACHE = {"url": "", "text": "", "ts": 0.0}


def _fetch_desktop_changelog_summary(url: str, ttl_seconds: int = 300) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    now = time.time()
    if _UPDATE_CHANGELOG_CACHE["url"] == url and _UPDATE_CHANGELOG_CACHE["text"] and (now - _UPDATE_CHANGELOG_CACHE["ts"]) < ttl_seconds:
        return _UPDATE_CHANGELOG_CACHE["text"]
    try:
        r = requests.get(
            url,
            timeout=(3, 10),
            headers={"User-Agent": "KommzVoice-UpdateServer/1.0"},
        )
        r.raise_for_status()
        lines = []
        for raw_line in r.text.splitlines():
            line = re.sub(r"\s+", " ", raw_line.strip())
            if not line or line.startswith("```"):
                continue
            if line.startswith("#"):
                continue
            line = re.sub(r"^\d+\.\s*", "- ", line)
            line = re.sub(r"^[-*+]\s*", "- ", line)
            if not line.startswith("- "):
                continue
            if len(line) > 180:
                line = line[:177].rstrip() + "..."
            lines.append(line)
            if len(lines) >= 5:
                break
        summary = "\n".join(lines[:5]).strip()
        _UPDATE_CHANGELOG_CACHE.update({"url": url, "text": summary, "ts": now})
        return summary
    except Exception:
        return ""


def _jwt_payload(token: str) -> dict:
    """Decode unverified JWT payload (best effort)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        return pyjson.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _is_probably_anon_supabase_key(key: str) -> bool:
    payload = _jwt_payload(key)
    return payload.get("role") == "anon"


def validate_runtime_config() -> None:
    missing = []
    required = {
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
        "SECRET_KEY": SECRET_KEY,
        "VOICE_SECRET_SALT": VOICE_SECRET_SALT,
        "PASSWORD_SALT": PASSWORD_SALT,
        "ADMIN_SECRET": ADMIN_SECRET,
    }
    for name, value in required.items():
        if not value:
            missing.append(name)

    if missing and IS_PRODUCTION:
        raise RuntimeError(f"Variables manquantes en production: {', '.join(missing)}")

    if SUPABASE_KEY and _is_probably_anon_supabase_key(SUPABASE_KEY):
        msg = "SUPABASE_KEY semble Ãªtre une clÃ© anon. Une service_role est requise cÃ´tÃ© serveur."
        if IS_PRODUCTION:
            raise RuntimeError(msg)
        print(f"[WARN] {msg}")

    if IS_PRODUCTION and SECRET_KEY in {"dev-secret-change-in-production", "CHANGE_ME"}:
        raise RuntimeError("SECRET_KEY invalide en production.")
    if IS_PRODUCTION and ADMIN_SECRET in {"admin-secret-change-this", "CHANGE_ME"}:
        raise RuntimeError("ADMIN_SECRET invalide en production.")


validate_runtime_config()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = SECRET_KEY or "dev-only-secret-change-me"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB max upload

# Supabase client (service role cÃ´tÃ© serveur uniquement)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Bucket Supabase Storage pour les fichiers audio
_storage_bucket_checked = False
_last_xtts_warmup_ts = 0.0
_xtts_warmup_lock = threading.Lock()
_xtts_status_cache = {"ts": 0.0, "payload": None}
_xtts_keepalive_started = False
_request_guards = {}
_request_guards_lock = threading.Lock()
_last_trial_guard_cleanup_ts = 0.0

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
HWID_RE = re.compile(r"^[A-Z0-9._:-]{8,128}$")
LAUGH_RE = re.compile(r"(?:\bha[\s\-.,;:!?]*ha\b|\bhahaha+\b|\brire\b|\brigole\b|\blol\b)", re.IGNORECASE)


def ensure_storage_bucket() -> None:
    """VÃ©rifie que le bucket Storage existe, sinon le crÃ©e."""
    global _storage_bucket_checked
    if _storage_bucket_checked:
        return

    buckets = supabase.storage.list_buckets() or []
    names = set()
    for b in buckets:
        if isinstance(b, dict):
            name = b.get("name")
        else:
            name = getattr(b, "name", None)
        if name:
            names.add(str(name))

    if STORAGE_BUCKET not in names:
        created = False
        errors = []

        # Compat multi-versions supabase-py/storage.
        # Les références audio utilisateur doivent rester privées par défaut.
        create_attempts = [
            lambda: supabase.storage.create_bucket(STORAGE_BUCKET),
            lambda: supabase.storage.create_bucket(STORAGE_BUCKET, {"public": False}),
            lambda: supabase.storage.create_bucket({"name": STORAGE_BUCKET, "public": False}),
            lambda: supabase.storage.create_bucket({"id": STORAGE_BUCKET, "name": STORAGE_BUCKET, "public": False}),
            lambda: supabase.storage.create_bucket(name=STORAGE_BUCKET),
            lambda: supabase.storage.create_bucket(name=STORAGE_BUCKET, options={"public": False}),
        ]

        for attempt in create_attempts:
            try:
                attempt()
                created = True
                break
            except Exception as e:
                errors.append(str(e))

        if not created:
            raise RuntimeError(
                f"Impossible de creer le bucket '{STORAGE_BUCKET}'. "
                f"Erreurs: {' | '.join(errors)}"
            )

        print(f"[INFO] Bucket cree automatiquement: {STORAGE_BUCKET}")

    _storage_bucket_checked = True


try:
    ensure_storage_bucket()
except Exception as e:
    print(f"[WARN] Storage bucket check Ã©chouÃ© au dÃ©marrage: {e}")


# =============================================================================
# HELPERS
# =============================================================================

def get_current_user():
    """Retourne l'utilisateur de la session ou None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        result = supabase.table("users").select("*").eq("id", user_id).single().execute()
        return result.data
    except Exception:
        return None


def login_required(f):
    """DÃ©corateur â€” refuse si non connectÃ©."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Non authentifiÃ©"}), 401
        return f(*args, **kwargs)
    return decorated


def hash_password(password: str) -> str:
    """Hash bcrypt pour les nouveaux mots de passe."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _hash_password_legacy(password: str) -> str:
    """Ancien hash SHA-256 + salt fixe, conservé pour migration douce."""
    salt = PASSWORD_SALT or "dev-password-salt-change-me"
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def _is_bcrypt_hash(stored_hash: str) -> bool:
    value = (stored_hash or "").strip()
    return value.startswith("$2a$") or value.startswith("$2b$") or value.startswith("$2y$")


def verify_password(password: str, stored_hash: str) -> tuple[bool, bool]:
    """
    Vérifie un mot de passe en supportant :
    - bcrypt (format actuel)
    - SHA-256 legacy (anciens comptes)

    Retourne : (is_valid, needs_upgrade)
    """
    raw = (stored_hash or "").strip()
    if not raw:
        return False, False

    if _is_bcrypt_hash(raw):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), raw.encode("utf-8")), False
        except Exception:
            return False, False

    legacy_ok = hmac.compare_digest(_hash_password_legacy(password), raw)
    return legacy_ok, legacy_ok


def generate_api_key() -> str:
    """GÃ©nÃ¨re une clÃ© API unique pour l'utilisateur."""
    return f"KV-{uuid.uuid4().hex.upper()}"


def _get_client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return (request.remote_addr or "0.0.0.0").strip()


def _stable_hash(value: str) -> str:
    seed = f"{ADMIN_SECRET}|{value or ''}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _clean_email(email: str) -> str:
    return (email or "").strip().lower()


def _is_secure_tts_owner(user: dict | None) -> bool:
    if not user:
        return False
    email = _clean_email(user.get("email") or "")
    if not email:
        return False
    if SECURE_TTS_OWNER_EMAILS:
        return email in SECURE_TTS_OWNER_EMAILS
    # No hardcoded owner fallback to avoid leaking personal data in source.
    return False


def _is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(_clean_email(email)))


def _is_valid_hwid(hwid: str) -> bool:
    return bool(HWID_RE.match((hwid or "").strip().upper()))


def _has_laugh_intent(text: str) -> bool:
    return bool(LAUGH_RE.search((text or "").strip()))


def _ip_prefix(ip: str) -> str:
    """Retourne un prÃ©fixe rÃ©seau stable (IPv4 /24, IPv6 /64)."""
    raw = (ip or "").strip()
    try:
        parsed = ipaddress.ip_address(raw)
        if parsed.version == 4:
            net = ipaddress.ip_network(f"{parsed}/24", strict=False)
        else:
            net = ipaddress.ip_network(f"{parsed}/64", strict=False)
        return str(net.network_address)
    except Exception:
        return raw or "0.0.0.0"


def _too_many_attempts(scope: str, key: str, max_attempts: int, window_seconds: int = 600) -> bool:
    """Rate-limit mÃ©moire (best effort)."""
    if max_attempts <= 0:
        return False
    now = time.time()
    bucket_key = f"{scope}:{key}"
    with _request_guards_lock:
        hits = _request_guards.get(bucket_key, [])
        cutoff = now - float(window_seconds)
        hits = [ts for ts in hits if ts >= cutoff]
        blocked = len(hits) >= int(max_attempts)
        if not blocked:
            hits.append(now)
        _request_guards[bucket_key] = hits
        return blocked


def _too_many_attempts_persistent(scope: str, key: str, max_attempts: int, window_seconds: int = 600) -> bool:
    """Rate-limit persistant en DB (compatible multi-instance)."""
    global _last_trial_guard_cleanup_ts
    if not TRIAL_GUARD_ENABLED or max_attempts <= 0:
        return False
    # Purge opportuniste des anciennes lignes (au plus 1 fois / heure).
    now = time.time()
    if (now - _last_trial_guard_cleanup_ts) > 3600:
        try:
            cutoff_iso = (datetime.utcnow() - timedelta(days=2)).isoformat()
            supabase.table("license_keys").delete().eq("product", "trial_guard").lt("activated_at", cutoff_iso).execute()
            _last_trial_guard_cleanup_ts = now
        except Exception:
            pass

    guard_id = _stable_hash(f"{scope}:{key}")[:24]
    now_iso = datetime.utcnow().isoformat()
    since_iso = (datetime.utcnow() - timedelta(seconds=max(60, int(window_seconds)))).isoformat()
    try:
        rows = (
            supabase.table("license_keys")
            .select("key_value")
            .eq("product", "trial_guard")
            .eq("activated_by_user_id", guard_id)
            .gte("activated_at", since_iso)
            .limit(int(max_attempts) + 1)
            .execute()
        )
        count = len(rows.data or [])
        if count >= int(max_attempts):
            return True

        # On journalise la tentative pour les prochaines instances/process.
        attempt_key = f"TRIAL-GUARD-{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        supabase.table("license_keys").insert({
            "key_value": attempt_key,
            "product": "trial_guard",
            "is_activated": True,
            "activated_by_email": "trial_guard",
            "activated_by_user_id": guard_id,
            "activated_at": now_iso,
            "expiration": datetime.utcfromtimestamp(time.time() + max(60, int(window_seconds))).strftime("%d/%m/%Y"),
        }).execute()
        return False
    except Exception:
        # Fallback mÃ©moire si DB indisponible.
        return _too_many_attempts(scope, key, max_attempts, window_seconds=window_seconds)


def _trial_signature(expiration_ts: int, rand: str, scope: str) -> str:
    material = f"{int(expiration_ts)}|{rand}|{scope}"
    return hmac.new(
        (DESKTOP_SECRET_SALT or VOICE_SECRET_SALT or "dev-trial-salt").encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16].upper()


def _estimate_audio_seconds(text: str, speed: float = 1.0) -> int:
    # Heuristic: ~14 chars/sec at speed=1.0 (French conversational output)
    safe_speed = max(0.5, min(2.0, float(speed or 1.0)))
    cps = 14.0 * safe_speed
    return max(1, int(round(len(text) / cps)))


def _clamp_float(value, default: float, min_v: float, max_v: float) -> float:
    try:
        v = float(value)
    except Exception:
        v = float(default)
    return max(min_v, min(max_v, v))


def _clamp_int(value, default: int, min_v: int, max_v: int) -> int:
    try:
        v = int(value)
    except Exception:
        v = int(default)
    return max(min_v, min(max_v, v))


def _is_xtts_url_configured() -> bool:
    val = (MODAL_XTTS_URL or "").strip().lower()
    if not val:
        return False
    placeholders = (
        "your-app--kommz-voice-xtts",
        "votre-app--kommz-voice-xtts",
        "example.modal.run",
    )
    return not any(p in val for p in placeholders)


def _is_gptsovits_url_configured() -> bool:
    val = (MODAL_GPTSOVITS_URL or "").strip().lower()
    if not val:
        return False
    placeholders = (
        "your-app--kommz-voice-gptsovits",
        "votre-app--kommz-voice-gptsovits",
        "example.modal.run",
    )
    return not any(p in val for p in placeholders)


def _storage_public_or_signed_url(path: str, expires_in: int = 3600) -> str:
    """
    Retourne une URL d'accÃ¨s Storage robuste:
    - tente d'abord une URL signÃ©e (compatible bucket privÃ©)
    - fallback URL publique uniquement si explicitement autorisÃ©
    """
    bucket = supabase.storage.from_(STORAGE_BUCKET)
    try:
        signed = bucket.create_signed_url(path, int(expires_in))
        if isinstance(signed, str) and signed:
            return signed
        if isinstance(signed, dict):
            direct = (signed.get("signedURL") or signed.get("signedUrl") or "").strip()
            if direct:
                return direct
            data = signed.get("data")
            if isinstance(data, dict):
                nested = (data.get("signedURL") or data.get("signedUrl") or "").strip()
                if nested:
                    return nested
    except Exception:
        pass
    if not STORAGE_ALLOW_PUBLIC_FALLBACK:
        return ""
    try:
        pub = bucket.get_public_url(path)
        if isinstance(pub, str):
            return pub
        if isinstance(pub, dict):
            return (pub.get("publicURL") or pub.get("publicUrl") or "").strip()
    except Exception:
        return ""
    return ""


def _get_xtts_warmup_url() -> str:
    if MODAL_XTTS_WARMUP_URL:
        return MODAL_XTTS_WARMUP_URL
    return _derive_modal_endpoint(MODAL_XTTS_URL, "warmup")


def _get_gptsovits_warmup_url() -> str:
    if MODAL_GPTSOVITS_WARMUP_URL:
        return MODAL_GPTSOVITS_WARMUP_URL
    return _derive_modal_endpoint(MODAL_GPTSOVITS_URL, "warmup")


def prewarm_xtts_sync(force: bool = False, cooldown_seconds: int = 90) -> None:
    """PrÃ©-rÃ©veille XTTS en mode bloquant (usage interne/keepalive)."""
    global _last_xtts_warmup_ts
    now = time.time()
    if not force and (now - _last_xtts_warmup_ts) < cooldown_seconds:
        return
    try:
        with _xtts_warmup_lock:
            now2 = time.time()
            if not force and (now2 - _last_xtts_warmup_ts) < cooldown_seconds:
                return
            warmup_url = _get_xtts_warmup_url()
            ok = False
            try:
                r = requests.post(warmup_url, timeout=(4, 20))
                ok = r.ok
            except Exception:
                pass
            if not ok:
                try:
                    requests.get(_get_xtts_health_url(), timeout=(4, 10))
                except Exception:
                    pass
            _last_xtts_warmup_ts = time.time()
    except Exception:
        pass


def prewarm_xtts_async(force: bool = False, cooldown_seconds: int = 90) -> None:
    """PrÃ©-rÃ©veille XTTS sans bloquer la requÃªte utilisateur."""
    def _runner():
        prewarm_xtts_sync(force=force, cooldown_seconds=cooldown_seconds)

    threading.Thread(target=_runner, daemon=True).start()


def _start_xtts_keepalive_thread() -> None:
    """Maintient XTTS chaud pour rÃ©duire la latence du premier appel."""
    global _xtts_keepalive_started
    if _xtts_keepalive_started or not XTTS_KEEPALIVE_ENABLED:
        return
    _xtts_keepalive_started = True

    def _runner():
        # Prime rapide au dÃ©marrage (sans bloquer le boot).
        time.sleep(3.0)
        while True:
            try:
                prewarm_xtts_sync(force=True, cooldown_seconds=10)
            except Exception:
                pass
            time.sleep(float(XTTS_KEEPALIVE_INTERVAL_SECONDS))

    threading.Thread(target=_runner, daemon=True).start()


def _get_xtts_runtime_status(force: bool = False) -> dict:
    """
    Retourne l'Ã©tat runtime du serveur XTTS:
    - online: endpoint /health joignable
    - warm: warmup rÃ©cent (moins de 10 min)
    - cold_start_likely: online mais pas warm
    """
    now = time.time()
    if not force:
        cached = _xtts_status_cache.get("payload")
        ts = float(_xtts_status_cache.get("ts") or 0.0)
        if cached and (now - ts) < 20.0:
            return cached

    online = False
    status_code = 0
    err = ""
    try:
        r = requests.get(_get_xtts_health_url(), timeout=(2, 5))
        status_code = int(r.status_code)
        online = bool(r.ok)
    except Exception as e:
        err = str(e)

    warm = (now - float(_last_xtts_warmup_ts or 0.0)) < 600.0
    cold_start_likely = bool(online and not warm)

    if not online:
        state = "offline"
        message = "Serveur clonage indisponible pour le moment."
    elif cold_start_likely:
        state = "cold"
        message = "Serveur en veille: la premiÃ¨re gÃ©nÃ©ration peut Ãªtre plus lente."
    else:
        state = "ready"
        message = "Serveur clonage prÃªt."

    payload = {
        "success": True,
        "state": state,
        "online": online,
        "warm": warm,
        "cold_start_likely": cold_start_likely,
        "status_code": status_code,
        "message": message,
        "error": err,
        "last_warmup_ts": int(_last_xtts_warmup_ts or 0),
    }
    _xtts_status_cache["ts"] = now
    _xtts_status_cache["payload"] = payload
    return payload


def _get_gptsovits_runtime_status() -> dict:
    if not _is_gptsovits_url_configured():
        return {
            "success": True,
            "configured": False,
            "online": False,
            "warm": False,
            "status_code": 0,
            "message": "Serveur GPT-SoVITS non configurÃ©.",
            "error": "",
            "health_url": _get_gptsovits_health_url(),
            "tts_url": _get_gptsovits_tts_url(),
        }

    online = False
    status_code = 0
    err = ""
    try:
        r = requests.get(_get_gptsovits_health_url(), timeout=(2, 5))
        status_code = int(r.status_code)
        online = bool(r.ok)
    except Exception as e:
        err = str(e)

    return {
        "success": True,
        "configured": True,
        "online": online,
        "warm": online,
        "status_code": status_code,
        "message": "Serveur GPT-SoVITS prÃªt." if online else "Serveur GPT-SoVITS indisponible.",
        "error": err,
        "health_url": _get_gptsovits_health_url(),
        "tts_url": _get_gptsovits_tts_url(),
    }


def _get_trial_status_for_user(user: dict) -> dict:
    if not user:
        return {"is_trial": False, "active": False}

    key = (user.get("license_key") or "").strip().upper()
    if not key.startswith("TRIAL-"):
        return {"is_trial": False, "active": False}

    created_raw = user.get("created_at")
    try:
        created_dt = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        if created_dt.tzinfo is not None:
            created_dt = created_dt.replace(tzinfo=None)
    except Exception:
        created_dt = datetime.utcnow()

    expires_at = created_dt + timedelta(hours=TRIAL_WINDOW_HOURS)
    now = datetime.utcnow()
    expired = now >= expires_at

    used_seconds = 0
    generations = 0
    try:
        logs = (
            supabase.table("generation_logs")
            .select("duration_ms,source")
            .eq("user_id", user["id"])
            .execute()
        )
        rows = logs.data or []
        generations = len(rows)
        used_ms = sum(int((r or {}).get("duration_ms") or 0) for r in rows)
        used_seconds = int(used_ms / 1000)
    except Exception:
        pass

    remaining_seconds = max(0, TRIAL_MAX_AUDIO_SECONDS - used_seconds)
    remaining_generations = max(0, TRIAL_MAX_GENERATIONS - generations)
    active = (not expired) and remaining_seconds > 0 and remaining_generations > 0

    return {
        "is_trial": True,
        "active": active,
        "expired": expired,
        "window_hours": TRIAL_WINDOW_HOURS,
        "max_audio_seconds": TRIAL_MAX_AUDIO_SECONDS,
        "max_generations": TRIAL_MAX_GENERATIONS,
        "used_seconds": used_seconds,
        "used_generations": generations,
        "remaining_seconds": remaining_seconds,
        "remaining_generations": remaining_generations,
        "expires_at": expires_at.isoformat() + "Z",
    }


# =============================================================================
# VÃ‰RIFICATION LICENCE VCV-
# =============================================================================

def verify_vcv_key(key: str) -> dict:
    """
    VÃ©rifie une clÃ© VCV-TIMESTAMP-RANDOM-SIG8.

    Format : VCV-{timestamp_expiration}-{random4}-{sha256[:8]}
    Salt    : VOICE_SECRET_SALT

    Retourne : {"valid": bool, "expired": bool, "expiration_ts": int}
    """
    MASTER_KEYS = ["VTP-VOICE-ADMIN", "NICOLAS-VOICE-PRO"]
    if key in MASTER_KEYS:
        return {"valid": True, "expired": False, "expiration_ts": 9999999999}

    parts = key.split("-")
    # Format : VCV-TIMESTAMP-RANDOM-SIG8 â†’ 4 parties
    if len(parts) != 4 or parts[0] != "VCV":
        return {"valid": False, "expired": False, "expiration_ts": 0}

    try:
        ts_str, rand, sig = parts[1], parts[2], parts[3]
        expiration_ts = int(ts_str)
    except (ValueError, IndexError):
        return {"valid": False, "expired": False, "expiration_ts": 0}

    # VÃ©rification signature (compatibilitÃ©: ancien + nouveau format)
    sig_upper = sig.upper()
    expected_ts_rand_salt = hashlib.sha256(f"{ts_str}{rand}{VOICE_SECRET_SALT}".encode()).hexdigest()[:8].upper()
    expected_salt_ts_rand = hashlib.sha256(f"{VOICE_SECRET_SALT}{ts_str}{rand}".encode()).hexdigest()[:8].upper()

    if not (
        hmac.compare_digest(sig_upper, expected_ts_rand_salt)
        or hmac.compare_digest(sig_upper, expected_salt_ts_rand)
    ):
        return {"valid": False, "expired": False, "expiration_ts": 0}

    # VÃ©rification expiration
    expired = expiration_ts < int(time.time())
    return {"valid": True, "expired": expired, "expiration_ts": expiration_ts}


def verify_vtp_key(key: str) -> dict:
    """
    VÃ©rifie une clÃ© VTP desktop.
    Format : VTP-{timestamp_expiration}-{random4}-{sha256[:8]}
    """
    parts = key.split("-")
    if len(parts) != 4 or parts[0] != "VTP":
        return {"valid": False, "expired": False, "expiration_ts": 0}

    try:
        ts_str, rand, sig = parts[1], parts[2], parts[3]
        expiration_ts = int(ts_str)
    except (ValueError, IndexError):
        return {"valid": False, "expired": False, "expiration_ts": 0}

    sig_upper = sig.upper()
    expected_ts_rand_salt = hashlib.sha256(f"{ts_str}{rand}{DESKTOP_SECRET_SALT}".encode()).hexdigest()[:8].upper()
    expected_salt_ts_rand = hashlib.sha256(f"{DESKTOP_SECRET_SALT}{ts_str}{rand}".encode()).hexdigest()[:8].upper()
    if not (
        hmac.compare_digest(sig_upper, expected_ts_rand_salt)
        or hmac.compare_digest(sig_upper, expected_salt_ts_rand)
    ):
        return {"valid": False, "expired": False, "expiration_ts": 0}

    expired = expiration_ts < int(time.time())
    return {"valid": True, "expired": expired, "expiration_ts": expiration_ts}


def verify_trial_desktop_key(key: str) -> dict:
    """
    VÃ©rifie une clÃ© TRIAL-{timestamp_expiration}-{random4}-{sig8}
    utilisÃ©e pour l'essai desktop.
    """
    parts = key.split("-")
    if len(parts) != 4 or parts[0] != "TRIAL":
        return {"valid": False, "expired": False, "expiration_ts": 0}

    try:
        ts_str, rand, sig = parts[1], parts[2], parts[3]
        expiration_ts = int(ts_str)
    except (ValueError, IndexError):
        return {"valid": False, "expired": False, "expiration_ts": 0}

    sig_upper = sig.upper()
    if len(sig_upper) < 8:
        return {"valid": False, "expired": False, "expiration_ts": 0}
    expected_new = _trial_signature(expiration_ts, rand, "desktop")
    # Compat hÃ©ritage: anciennes clÃ©s 8 chars.
    expected_old_a = hashlib.sha256(f"{ts_str}{rand}{DESKTOP_SECRET_SALT}".encode()).hexdigest()[:8].upper()
    expected_old_b = hashlib.sha256(f"{DESKTOP_SECRET_SALT}{ts_str}{rand}".encode()).hexdigest()[:8].upper()
    if not (
        hmac.compare_digest(sig_upper, expected_new)
        or (len(sig_upper) == 8 and (hmac.compare_digest(sig_upper, expected_old_a) or hmac.compare_digest(sig_upper, expected_old_b)))
    ):
        return {"valid": False, "expired": False, "expiration_ts": 0}

    expired = expiration_ts < int(time.time())
    return {"valid": True, "expired": expired, "expiration_ts": expiration_ts}


# =============================================================================
# ROUTES STATIQUES â€” Sert index.html
# =============================================================================

@app.route("/")
def index():
    """Sert l'application principale."""
    user = get_current_user()
    # On passe le user au template Jinja si besoin
    # Pour une SPA pure, on peut retourner juste le fichier statique
    try:
        with open(os.path.join(app.static_folder or ".", "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
        return render_template_string(
            html,
            user=user,
            secure_tts_enabled=_is_secure_tts_owner(user),
            secure_tts_unlocked=bool(session.get("secure_tts_unlocked")),
        )
    except FileNotFoundError:
        return "<h1>index.html introuvable dans le dossier static/</h1>", 404


# =============================================================================
# ROUTES AUTH
# =============================================================================

@app.route("/register", methods=["POST"])
def register():
    """
    Inscription.
    Body JSON : { email, password, license_key }

    La clÃ© VCV- est obligatoire pour s'inscrire.
    Elle est marquÃ©e comme utilisÃ©e en Supabase aprÃ¨s succÃ¨s.
    """
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()
    license_key = (data.get("license_key") or "").strip().upper()
    trial_mode = str(data.get("trial_mode", "")).strip().lower() in {"1", "true", "yes", "on"}
    trial_fingerprint_raw = (data.get("trial_fingerprint") or "").strip()

    # Validations basiques
    if not email or "@" not in email:
        return jsonify({"success": False, "error": "Email invalide"}), 400
    if len(password) < 8:
        return jsonify({"success": False, "error": "Mot de passe trop court (8 caractÃ¨res min)"}), 400
    if not license_key:
        return jsonify({"success": False, "error": "ClÃ© de licence VCV- requise"}), 400

    # VÃ©rification clÃ© licence
    vcv_result = verify_vcv_key(license_key)
    if not vcv_result["valid"]:
        return jsonify({"success": False, "error": "ClÃ© de licence invalide"}), 403
    if vcv_result["expired"]:
        return jsonify({"success": False, "error": "ClÃ© de licence expirÃ©e"}), 403

    # VÃ©rification que la clÃ© n'est pas dÃ©jÃ  utilisÃ©e par un autre compte
    try:
        lic_check = supabase.table("license_keys")\
            .select("*")\
            .eq("key_value", license_key)\
            .eq("product", "voice")\
            .single()\
            .execute()

        if lic_check.data and lic_check.data.get("is_activated") and lic_check.data.get("activated_by_email") != email:
            return jsonify({
                "success": False,
                "error": "Cette clÃ© est dÃ©jÃ  associÃ©e Ã  un autre compte"
            }), 409
    except Exception:
        # La clÃ© n'existe pas encore en base â†’ OK pour un nouvel utilisateur
        pass

    # VÃ©rification que l'email n'existe pas dÃ©jÃ 
    try:
        existing = supabase.table("users").select("id").eq("email", email).execute()
        if existing.data:
            return jsonify({"success": False, "error": "Email dÃ©jÃ  utilisÃ©"}), 409
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur DB: {e}"}), 500

    # CrÃ©ation de l'utilisateur
    new_user = {
        "id":           str(uuid.uuid4()),
        "email":        email,
        "password":     hash_password(password),
        "api_key":      generate_api_key(),
        "license_key":  license_key,
        "created_at":   datetime.utcnow().isoformat(),
    }

    try:
        result = supabase.table("users").insert(new_user).execute()
        created_user = result.data[0]
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur crÃ©ation compte: {e}"}), 500

    # Marquer la clÃ© comme activÃ©e dans license_keys
    expiration_date = datetime.utcfromtimestamp(vcv_result["expiration_ts"]).strftime("%d/%m/%Y") \
        if vcv_result["expiration_ts"] < 9999999999 else "IllimitÃ©"

    try:
        # Upsert (insert ou update si dÃ©jÃ  prÃ©sent)
        supabase.table("license_keys").upsert({
            "key_value":           license_key,
            "product":             "voice",
            "is_activated":        True,
            "activated_by_email":  email,
            "activated_by_user_id": created_user["id"],
            "activated_at":        datetime.utcnow().isoformat(),
            "expiration":          expiration_date,
        }, on_conflict="key_value").execute()
    except Exception as e:
        # Non bloquant â€” l'utilisateur est crÃ©Ã©, on log juste l'erreur
        print(f"[WARN] Impossible de marquer la licence: {e}")

    # Session
    session["user_id"] = created_user["id"]
    session["email"]   = created_user["email"]

    return jsonify({
        "success":  True,
        "user_id":  created_user["id"],
        "email":    created_user["email"],
        "api_key":  created_user["api_key"],
    })


@app.route("/trial/register", methods=["POST"])
def register_trial():
    """CrÃ©ation d'un compte essai gratuit avec anti-abus."""
    data = request.get_json() or {}
    email = _clean_email(data.get("email") or "")
    password = (data.get("password") or "").strip()
    trial_fingerprint_raw = (data.get("trial_fingerprint") or "").strip()
    client_ip = _get_client_ip()
    ip_hint = _ip_prefix(client_ip)

    if _too_many_attempts("trial_register_ip", ip_hint, TRIAL_REGISTER_RATE_LIMIT_PER_10MIN, window_seconds=TRIAL_GUARD_WINDOW_SECONDS):
        return jsonify({"success": False, "error": "Trop de tentatives, rÃ©essayez dans 10 minutes"}), 429
    if _too_many_attempts("trial_register_email", email or "empty", TRIAL_REGISTER_RATE_LIMIT_PER_10MIN, window_seconds=TRIAL_GUARD_WINDOW_SECONDS):
        return jsonify({"success": False, "error": "Trop de tentatives, rÃ©essayez dans 10 minutes"}), 429
    if _too_many_attempts_persistent("trial_register_ip", ip_hint, TRIAL_REGISTER_RATE_LIMIT_PER_10MIN, window_seconds=TRIAL_GUARD_WINDOW_SECONDS):
        return jsonify({"success": False, "error": "Trop de tentatives, rÃ©essayez dans 10 minutes"}), 429
    if _too_many_attempts_persistent("trial_register_email", email or "empty", TRIAL_REGISTER_RATE_LIMIT_PER_10MIN, window_seconds=TRIAL_GUARD_WINDOW_SECONDS):
        return jsonify({"success": False, "error": "Trop de tentatives, rÃ©essayez dans 10 minutes"}), 429

    if not _is_valid_email(email):
        return jsonify({"success": False, "error": "Email invalide"}), 400
    if len(password) < 8:
        return jsonify({"success": False, "error": "Mot de passe trop court (8 caractÃ¨res min)"}), 400

    try:
        existing = supabase.table("users").select("id").eq("email", email).execute()
        if existing.data:
            return jsonify({"success": False, "error": "Email dÃ©jÃ  utilisÃ©"}), 409
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur DB: {e}"}), 500

    # Empreinte serveur: ne jamais dÃ©pendre uniquement d'un fingerprint client modifiable.
    ua = (request.user_agent.string or "").strip()
    al = (request.headers.get("Accept-Language") or "").strip()
    trial_fp_base = f"ip:{ip_hint}|ua:{ua}|lang:{al}|client:{trial_fingerprint_raw[:256]}"
    fp_key = f"TRIAL-FP-{_stable_hash(f'fp:{trial_fp_base}')[:24]}"
    ip_key = f"TRIAL-IP-{_stable_hash(f'ip:{ip_hint}')[:24]}"

    try:
        by_email = (
            supabase.table("license_keys")
            .select("key_value")
            .eq("product", "trial")
            .eq("activated_by_email", email)
            .limit(1)
            .execute()
        )
        if by_email.data:
            return jsonify({"success": False, "error": "Essai dÃ©jÃ  utilisÃ© sur cet email"}), 409
    except Exception:
        pass

    for lock_key, reason in ((fp_key, "cet appareil"), (ip_key, "cette connexion")):
        try:
            lock_row = (
                supabase.table("license_keys")
                .select("key_value")
                .eq("key_value", lock_key)
                .eq("product", "trial")
                .limit(1)
                .execute()
            )
            if lock_row.data:
                return jsonify({"success": False, "error": f"Essai dÃ©jÃ  utilisÃ© sur {reason}"}), 409
        except Exception:
            pass

    trial_expiry_ts = int(time.time()) + (TRIAL_WINDOW_HOURS * 3600)
    trial_rand = uuid.uuid4().hex[:4].upper()
    trial_sig = _trial_signature(trial_expiry_ts, trial_rand, "web")
    trial_license = f"TRIAL-{trial_expiry_ts}-{trial_rand}-{trial_sig}"

    new_user = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password": hash_password(password),
        "api_key": generate_api_key(),
        "license_key": trial_license,
        "created_at": datetime.utcnow().isoformat(),
    }
    try:
        result = supabase.table("users").insert(new_user).execute()
        created_user = result.data[0]
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur crÃ©ation compte: {e}"}), 500

    expiration_date = (datetime.utcnow() + timedelta(hours=TRIAL_WINDOW_HOURS)).strftime("%d/%m/%Y")
    try:
        supabase.table("license_keys").upsert({
            "key_value": trial_license,
            "product": "trial",
            "is_activated": True,
            "activated_by_email": email,
            "activated_by_user_id": created_user["id"],
            "activated_at": datetime.utcnow().isoformat(),
            "expiration": expiration_date,
        }, on_conflict="key_value").execute()

        for lk in (fp_key, ip_key):
            supabase.table("license_keys").upsert({
                "key_value": lk,
                "product": "trial",
                "is_activated": True,
                "activated_by_email": email,
                "activated_by_user_id": created_user["id"],
                "activated_at": datetime.utcnow().isoformat(),
                "expiration": expiration_date,
            }, on_conflict="key_value").execute()
    except Exception as e:
        print(f"[WARN] Trial lock save failed: {e}")

    session["user_id"] = created_user["id"]
    session["email"] = created_user["email"]
    trial_status = _get_trial_status_for_user(created_user)

    return jsonify({
        "success": True,
        "user_id": created_user["id"],
        "email": created_user["email"],
        "api_key": created_user["api_key"],
        "trial": trial_status,
    })


@app.route("/login", methods=["POST"])
def login():
    """
    Connexion.
    Body JSON : { email, password }
    """
    data = request.get_json() or {}
    email    = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"success": False, "error": "Email et mot de passe requis"}), 400

    try:
        result = supabase.table("users")\
            .select("*")\
            .eq("email", email)\
            .single()\
            .execute()
    except Exception:
        return jsonify({"success": False, "error": "Identifiants incorrects"}), 401

    if not result.data:
        return jsonify({"success": False, "error": "Identifiants incorrects"}), 401

    user = result.data
    is_valid_password, needs_upgrade = verify_password(password, user.get("password") or "")
    if not is_valid_password:
        return jsonify({"success": False, "error": "Identifiants incorrects"}), 401

    if needs_upgrade:
        try:
            upgraded_hash = hash_password(password)
            supabase.table("users").update({"password": upgraded_hash}).eq("id", user["id"]).execute()
            user["password"] = upgraded_hash
            print(f"[INFO] Password hash migrated to bcrypt for {user.get('email') or user.get('id')}")
        except Exception as upgrade_err:
            print(f"[WARN] Password hash migration failed for {user.get('email') or user.get('id')}: {upgrade_err}")

    session["user_id"] = user["id"]
    session["email"]   = user["email"]

    # Synchroniser license_keys si l'entrÃ©e est manquante
    # (cas des comptes crÃ©Ã©s avant que la table license_keys existait)
    license_key = user.get("license_key", "")
    if license_key:
        try:
            supabase.table("license_keys").upsert({
                "key_value":            license_key,
                "product":              "voice",
                "is_activated":         True,
                "activated_by_email":   user["email"],
                "activated_by_user_id": user["id"],
                "activated_at":         datetime.utcnow().isoformat(),
            }, on_conflict="key_value").execute()
        except Exception as sync_err:
            print(f"[WARN] Sync license_keys Ã©chouÃ©: {sync_err}")

    return jsonify({
        "success": True,
        "user_id": user["id"],
        "email":   user["email"],
        "api_key": user["api_key"],
    })


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    if request.method == "GET":
        return redirect("/")
    return jsonify({"success": True})


@app.route("/me", methods=["GET"])
@login_required
def me():
    """Retourne les infos de l'utilisateur connectÃ©."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Session invalide"}), 401
    return jsonify({
        "id":      user["id"],
        "email":   user["email"],
        "api_key": user["api_key"],
        "trial":   _get_trial_status_for_user(user),
    })


@app.route("/api/trial/status", methods=["GET"])
@login_required
def trial_status():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Session invalide"}), 401

    status = _get_trial_status_for_user(user)
    if not status.get("is_trial"):
        return jsonify({
            "success": True,
            "is_trial": False,
            "active": False,
        })

    remaining_seconds = int(status.get("remaining_seconds", 0))
    phrases = {
        "courtes": max(0, remaining_seconds // 4),   # ~4 sec/phrase
        "moyennes": max(0, remaining_seconds // 8),  # ~8 sec/phrase
        "longues": max(0, remaining_seconds // 14),  # ~14 sec/phrase
    }
    return jsonify({
        "success": True,
        **status,
        "remaining_phrases": phrases,
    })


@app.route("/api/xtts/warmup", methods=["POST"])
@login_required
def xtts_warmup():
    prewarm_xtts_async(force=True, cooldown_seconds=10)
    return jsonify({"success": True, "message": "XTTS warmup lancÃ©"})


@app.route("/api/xtts/status", methods=["GET"])
@login_required
def xtts_status():
    return jsonify(_get_xtts_runtime_status(force=False))


@app.route("/api/gptsovits/warmup", methods=["POST"])
@login_required
def gptsovits_warmup():
    if not _is_gptsovits_url_configured():
        return jsonify({"success": False, "error": "MODAL_GPTSOVITS_URL non configuree."}), 500
    try:
        r = requests.post(_get_gptsovits_warmup_url(), timeout=(4, 30))
        if r.ok:
            payload = {}
            try:
                payload = r.json() if r.content else {}
            except Exception:
                payload = {}
            return jsonify({"success": True, "message": "GPT-SoVITS warmup lance", **payload})
        return jsonify({"success": False, "error": f"Warmup GPT-SoVITS HTTP {r.status_code}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502


@app.route("/api/gptsovits/status", methods=["GET"])
@login_required
def gptsovits_status():
    return jsonify(_get_gptsovits_runtime_status())


@app.route("/api/gptsovits/style", methods=["POST"])
@login_required
def gptsovits_style():
    """
    Proxy backend vers Modal GPT-SoVITS.
    Attend un upload ref_audio + metadata de style.
    """
    if not _is_gptsovits_url_configured():
        return jsonify({"success": False, "error": "MODAL_GPTSOVITS_URL non configuree."}), 500

    ref_audio = request.files.get("ref_audio")
    if not ref_audio or not getattr(ref_audio, "filename", ""):
        return jsonify({"success": False, "error": "ref_audio requis"}), 400

    text = (request.form.get("text") or "").strip()
    text_lang = (request.form.get("text_lang") or "").strip().lower()
    prompt_lang = (request.form.get("prompt_lang") or "").strip().lower()
    prompt_text = (request.form.get("prompt_text") or "").strip()
    style_text = (request.form.get("style_text") or "").strip()
    model_variant = (request.form.get("model_variant") or "auto").strip().lower()
    text_split_method = (request.form.get("text_split_method") or "cut5").strip()
    media_type = (request.form.get("media_type") or "wav").strip().lower()
    speed_factor = _clamp_float(request.form.get("speed_factor", 1.0), 1.0, 0.5, 1.8)
    top_k = _clamp_int(request.form.get("top_k", 5), 5, 1, 100)
    top_p = _clamp_float(request.form.get("top_p", 1.0), 1.0, 0.1, 1.0)
    temperature = _clamp_float(request.form.get("temperature", 1.0), 1.0, 0.1, 2.0)
    repetition_penalty = _clamp_float(request.form.get("repetition_penalty", 1.35), 1.35, 0.1, 10.0)
    sample_steps = _clamp_int(request.form.get("sample_steps", 32), 32, 1, 64)
    parallel_infer = str(request.form.get("parallel_infer", "true")).strip().lower() in {"1", "true", "yes", "on"}

    if not text:
        return jsonify({"success": False, "error": "text requis"}), 400
    if not text_lang:
        return jsonify({"success": False, "error": "text_lang requis"}), 400
    if not prompt_lang:
        return jsonify({"success": False, "error": "prompt_lang requis"}), 400
    if model_variant not in {"auto", "custom", "generic"}:
        return jsonify({"success": False, "error": "model_variant invalide"}), 400

    files = {
        "ref_audio": (ref_audio.filename, ref_audio.stream, ref_audio.mimetype or "audio/wav"),
    }
    data = {
        "text": text,
        "text_lang": text_lang,
        "prompt_lang": prompt_lang,
        "prompt_text": prompt_text,
        "style_text": style_text,
        "model_variant": model_variant,
        "media_type": media_type,
        "text_split_method": text_split_method,
        "speed_factor": str(speed_factor),
        "top_k": str(top_k),
        "top_p": str(top_p),
        "temperature": str(temperature),
        "repetition_penalty": str(repetition_penalty),
        "sample_steps": str(sample_steps),
        "parallel_infer": "true" if parallel_infer else "false",
    }

    try:
        r = requests.post(
            _get_gptsovits_tts_url(),
            data=data,
            files=files,
            timeout=600,
        )
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur Modal GPT-SoVITS: {e}"}), 502

    if not r.ok:
        body = (r.text or "")[:500]
        return jsonify({"success": False, "error": f"Erreur Modal GPT-SoVITS (HTTP {r.status_code}): {body}"}), 502

    return Response(r.content, mimetype=r.headers.get("Content-Type", "audio/wav"))


@app.route("/api/secure-tts/unlock", methods=["POST"])
@login_required
def secure_tts_unlock():
    user = get_current_user()
    if not _is_secure_tts_owner(user):
        return jsonify({"success": False, "error": "Acces refuse"}), 403

    expected = (os.environ.get("SECURE_TTS_PASSWORD", "") or "").strip()
    if not expected:
        return jsonify({"success": False, "error": "SECURE_TTS_PASSWORD non configure"}), 500

    data = request.get_json() or {}
    provided = str(data.get("password") or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        return jsonify({"success": False, "error": "Mot de passe invalide"}), 401

    session["secure_tts_unlocked"] = True
    return jsonify({"success": True})


# =============================================================================
# ROUTE LICENCE â€” VÃ©rification avant inscription (appelÃ©e depuis index.html)
# =============================================================================

@app.route("/license/voice/verify-web", methods=["POST"])
def verify_voice_license_web():
    """
    VÃ©rifie une clÃ© VCV- AVANT inscription.

    Body JSON : { license_key: "VCV-..." }

    RÃ©ponses :
      { valid: true,  already_used: false, expiration: "30/06/2027" }
      { valid: true,  already_used: true,  email: "user@email.com" }  â† proposer login
      { valid: false, error: "..." }
    """
    data = request.get_json() or {}
    raw_key = (data.get("license_key") or "").strip().upper()

    if not raw_key:
        return jsonify({"valid": False, "error": "ClÃ© manquante"}), 400

    # VÃ©rification cryptographique locale
    vcv_result = verify_vcv_key(raw_key)
    if not vcv_result["valid"]:
        return jsonify({"valid": False, "error": "ClÃ© de licence invalide"}), 200
    if vcv_result["expired"]:
        return jsonify({"valid": False, "error": "ClÃ© de licence expirÃ©e"}), 200

    expiration_date = datetime.utcfromtimestamp(vcv_result["expiration_ts"]).strftime("%d/%m/%Y") \
        if vcv_result["expiration_ts"] < 9999999999 else "IllimitÃ©"

    # VÃ©rification en base â€” est-elle dÃ©jÃ  utilisÃ©e ?
    try:
        lic = supabase.table("license_keys")\
            .select("*")\
            .eq("key_value", raw_key)\
            .eq("product", "voice")\
            .single()\
            .execute()

        if lic.data and lic.data.get("is_activated"):
            # ClÃ© dÃ©jÃ  activÃ©e â€” proposer la connexion
            return jsonify({
                "valid":        True,
                "already_used": True,
                "email":        lic.data.get("activated_by_email", ""),
                "expiration":   lic.data.get("expiration", expiration_date),
            })
    except Exception:
        pass  # Pas encore en base â†’ nouvelle clÃ©

    return jsonify({
        "valid":        True,
        "already_used": False,
        "expiration":   expiration_date,
    })


def _activate_desktop_license_common(product: str):
    """
    Activation serveur pour desktop (source de vÃ©ritÃ©).
    RÃ¨gle: une clÃ© est liÃ©e Ã  un seul email, mais cet email peut changer de PC.
    """
    data = request.get_json() or {}
    key = (data.get("license_key") or "").strip().upper()
    email = (data.get("email") or "").strip().lower()
    hwid = (data.get("hwid") or "").strip().upper()

    if not key or not email or "@" not in email or not hwid:
        return jsonify({"ok": False, "error": "ParamÃ¨tres manquants"}), 400

    if product == "voice":
        check = verify_vcv_key(key)
        expected_products = {"voice", "bundle"}
    else:
        check = verify_vtp_key(key)
        expected_products = {"gamer", "bundle"}

    if not check["valid"]:
        return jsonify({"ok": False, "error": "ClÃ© invalide"}), 403
    if check["expired"]:
        return jsonify({"ok": False, "error": "ClÃ© expirÃ©e"}), 403

    expiration_date = datetime.utcfromtimestamp(check["expiration_ts"]).strftime("%d/%m/%Y") \
        if check["expiration_ts"] < 9999999999 else "IllimitÃ©"

    try:
        existing_resp = supabase.table("license_keys").select("*").eq("key_value", key).execute()
        existing = (existing_resp.data or [{}])[0]

        existing_email = (existing.get("activated_by_email") or "").strip().lower()
        if existing_email and existing_email != email:
            return jsonify({
                "ok": False,
                "error": "Cette clÃ© est dÃ©jÃ  associÃ©e Ã  un autre utilisateur"
            }), 409

        existing_product = (existing.get("product") or "").strip().lower()
        if existing_product and existing_product not in expected_products:
            return jsonify({"ok": False, "error": "ClÃ© incompatible avec ce produit"}), 409

        hwid_history_raw = (existing.get("desktop_hwid") or "").strip()
        hwids = [h.strip().upper() for h in hwid_history_raw.split(",") if h.strip()]
        if hwid not in hwids:
            hwids.append(hwid)
        desktop_hwid = ",".join(hwids)

        row = {
            "key_value": key,
            "product": existing_product or product,
            "is_activated": True,
            "activated_by_email": email,
            "desktop_hwid": desktop_hwid,
            "expiration": expiration_date,
            "activated_at": datetime.utcnow().isoformat(),
        }
        supabase.table("license_keys").upsert(row, on_conflict="key_value").execute()
        return jsonify({"ok": True, "expiration": expiration_date})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erreur DB: {e}"}), 500


@app.route("/license/desktop/activate", methods=["POST"])
def activate_desktop_gamer_license():
    return _activate_desktop_license_common("gamer")


@app.route("/license/voice/activate-desktop", methods=["POST"])
def activate_desktop_voice_license():
    return _activate_desktop_license_common("voice")


@app.route("/license/trial/activate-desktop", methods=["POST"])
def activate_desktop_trial_license():
    """
    Active (ou revalide) un essai desktop 24h.
    RÃ¨gles anti-abus:
    - 1 essai par email
    - 1 essai par HWID
    - mÃªme couple email+HWID: revalidation autorisÃ©e
    """
    data = request.get_json() or {}
    key = (data.get("license_key") or "").strip().upper()
    email = _clean_email(data.get("email") or "")
    hwid = (data.get("hwid") or "").strip().upper()
    ip_hint = _ip_prefix(_get_client_ip())

    if _too_many_attempts("trial_activate_ip", ip_hint, TRIAL_ACTIVATE_RATE_LIMIT_PER_10MIN, window_seconds=TRIAL_GUARD_WINDOW_SECONDS):
        return jsonify({"ok": False, "error": "Trop de tentatives, rÃ©essayez dans 10 minutes"}), 429
    if _too_many_attempts("trial_activate_email", email or "empty", TRIAL_ACTIVATE_RATE_LIMIT_PER_10MIN, window_seconds=TRIAL_GUARD_WINDOW_SECONDS):
        return jsonify({"ok": False, "error": "Trop de tentatives, rÃ©essayez dans 10 minutes"}), 429
    if _too_many_attempts_persistent("trial_activate_ip", ip_hint, TRIAL_ACTIVATE_RATE_LIMIT_PER_10MIN, window_seconds=TRIAL_GUARD_WINDOW_SECONDS):
        return jsonify({"ok": False, "error": "Trop de tentatives, rÃ©essayez dans 10 minutes"}), 429
    if _too_many_attempts_persistent("trial_activate_email", email or "empty", TRIAL_ACTIVATE_RATE_LIMIT_PER_10MIN, window_seconds=TRIAL_GUARD_WINDOW_SECONDS):
        return jsonify({"ok": False, "error": "Trop de tentatives, rÃ©essayez dans 10 minutes"}), 429

    if not _is_valid_email(email) or not _is_valid_hwid(hwid):
        return jsonify({"ok": False, "error": "ParamÃ¨tres manquants"}), 400

    def _hwid_list(raw: str) -> list[str]:
        return [h.strip().upper() for h in (raw or "").split(",") if h.strip()]

    try:
        rows_resp = (
            supabase.table("license_keys")
            .select("*")
            .eq("product", "trial_desktop")
            .execute()
        )
        rows = rows_resp.data or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erreur DB: {e}"}), 500

    # Revalidation d'une clÃ© trial existante (au redÃ©marrage du logiciel)
    if key:
        chk = verify_trial_desktop_key(key)
        if not chk["valid"] or chk["expired"]:
            return jsonify({"ok": False, "error": "ClÃ© essai invalide ou expirÃ©e"}), 403

        for row in rows:
            if (row.get("key_value") or "").strip().upper() != key:
                continue
            row_email = (row.get("activated_by_email") or "").strip().lower()
            row_hwids = _hwid_list((row.get("desktop_hwid") or "").strip())
            if row_email != email:
                return jsonify({"ok": False, "error": "Essai associÃ© Ã  un autre email"}), 409
            if hwid not in row_hwids:
                return jsonify({"ok": False, "error": "Essai associÃ© Ã  un autre appareil"}), 409
            expiration = row.get("expiration") or datetime.utcfromtimestamp(chk["expiration_ts"]).strftime("%d/%m/%Y")
            return jsonify({
                "ok": True,
                "trial": True,
                "license_key": key,
                "voice_license_key": key,
                "expiration": expiration,
            })
        return jsonify({"ok": False, "error": "Essai introuvable"}), 404

    # CrÃ©ation d'un nouvel essai (2 passes pour Ã©viter les faux blocages liÃ©s Ã  l'ordre DB)
    # Pass 1: si le couple (email + hwid) existe dÃ©jÃ , on le rÃ©utilise.
    exact_match_found = False
    for row in rows:
        row_email = (row.get("activated_by_email") or "").strip().lower()
        row_hwids = _hwid_list((row.get("desktop_hwid") or "").strip())
        row_key = (row.get("key_value") or "").strip().upper()
        row_exp = (row.get("expiration") or "").strip()

        if row_email == email and hwid in row_hwids and row_key:
            exact_match_found = True
            chk = verify_trial_desktop_key(row_key)
            if chk["valid"] and not chk["expired"]:
                return jsonify({
                    "ok": True,
                    "trial": True,
                    "license_key": row_key,
                    "voice_license_key": row_key,
                    "expiration": row_exp or datetime.utcfromtimestamp(chk["expiration_ts"]).strftime("%d/%m/%Y"),
                })
            # Couple reconnu mais essai expirÃ©.
            return jsonify({"ok": False, "error": "Essai expirÃ© pour cet email/appareil"}), 409

    # Pass 2: anti-abus strict uniquement si aucun couple exact n'a Ã©tÃ© trouvÃ©.
    if not exact_match_found:
        for row in rows:
            row_email = (row.get("activated_by_email") or "").strip().lower()
            row_hwids = _hwid_list((row.get("desktop_hwid") or "").strip())
            if row_email == email:
                return jsonify({"ok": False, "error": "Essai dÃ©jÃ  utilisÃ© avec cet email"}), 409
            if hwid in row_hwids:
                return jsonify({"ok": False, "error": "Essai dÃ©jÃ  utilisÃ© sur cet appareil"}), 409

    # PATCH: log permanent HWID (resiste a la suppression Supabase)
    try:
        hwid_log = supabase.table("license_keys").select("key_value") \
            .eq("product", "trial_hwid_log").eq("desktop_hwid", hwid).limit(1).execute()
        if hwid_log.data:
            return jsonify({"ok": False, "error": "Essai deja utilise sur cet appareil"}), 409
        email_log = supabase.table("license_keys").select("key_value") \
            .eq("product", "trial_hwid_log").eq("activated_by_email", email).limit(1).execute()
        if email_log.data:
            return jsonify({"ok": False, "error": "Essai deja utilise avec cet email"}), 409
    except Exception as _log_err:
        print(f"[TRIAL] Erreur lecture trial_hwid_log: {_log_err}", flush=True)

    expiration_ts = int(time.time()) + (TRIAL_WINDOW_HOURS * 3600)
    rand = uuid.uuid4().hex[:4].upper()
    sig = _trial_signature(expiration_ts, rand, "desktop")
    trial_key = f"TRIAL-{expiration_ts}-{rand}-{sig}"
    expiration_date = datetime.utcfromtimestamp(expiration_ts).strftime("%d/%m/%Y")

    try:
        supabase.table("license_keys").upsert({
            "key_value": trial_key,
            "product": "trial_desktop",
            "is_activated": True,
            "activated_by_email": email,
            "desktop_hwid": hwid,
            "expiration": expiration_date,
            "activated_at": datetime.utcnow().isoformat(),
        }, on_conflict="key_value").execute()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erreur DB: {e}"}), 500

    # PATCH: log permanent HWID pour bloquer futurs trials meme apres suppression
    try:
        log_key = f"TRIAL-LOG-{hwid[:16]}-{_stable_hash(f'{email}:{hwid}')[:12].upper()}"
        supabase.table("license_keys").upsert({
            "key_value": log_key,
            "product": "trial_hwid_log",
            "is_activated": True,
            "activated_by_email": email,
            "desktop_hwid": hwid,
            "expiration": "permanent",
            "activated_at": datetime.utcnow().isoformat(),
        }, on_conflict="key_value").execute()
    except Exception as _log_e:
        print(f"[TRIAL] Erreur ecriture trial_hwid_log: {_log_e}", flush=True)

    return jsonify({
        "ok": True,
        "trial": True,
        "license_key": trial_key,
        "voice_license_key": trial_key,
        "expiration": expiration_date,
    })


# =============================================================================
# ROUTES PROFILS VOCAUX
# =============================================================================

@app.route("/api/profiles", methods=["GET"])
@login_required
def get_profiles():
    """Retourne tous les profils vocaux de l'utilisateur connectÃ©."""
    user_id = session["user_id"]
    try:
        result = supabase.table("voice_profiles")\
            .select("*")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .execute()
        rows = result.data or []
        for row in rows:
            fid = (row or {}).get("file_id")
            uid = (row or {}).get("user_id") or user_id
            if fid and uid:
                row["audio_url"] = _storage_public_or_signed_url(f"{uid}/{fid}", expires_in=3600)
        return jsonify({"success": True, "profiles": rows})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/profiles", methods=["POST"])
@login_required
def save_profile():
    """
    Sauvegarde un nouveau profil vocal en Supabase.

    Body JSON :
    {
      file_id:        "uuid",          â† ID du fichier uploadÃ© via /api/upload-reference
      name:           "Narrateur Pro",
      reference_text: "Transcription Whisper...",
      description:    "...",
      tags:           "Profond, Masculin, Narration",
      visibility:     "public" | "private"
    }
    """
    data    = request.get_json() or {}
    user_id = session["user_id"]

    file_id        = data.get("file_id")
    name           = (data.get("name") or "").strip()
    reference_text = (data.get("reference_text") or "").strip()
    description    = data.get("description", "")
    tags           = data.get("tags", "")
    visibility     = data.get("visibility", "public")
    language       = data.get("language", "fr")

    if not name:
        return jsonify({"success": False, "error": "Nom requis"}), 400
    if not reference_text:
        return jsonify({"success": False, "error": "Transcription Whisper requise"}), 400
    if not file_id:
        return jsonify({"success": False, "error": "Fichier audio requis"}), 400

    # RÃ©cupÃ©ration de l'URL publique du fichier en Supabase Storage
    audio_url = _storage_public_or_signed_url(f"{user_id}/{file_id}", expires_in=3600)

    profile_id = str(uuid.uuid4())
    new_profile = {
        "id":             profile_id,
        "user_id":        user_id,
        "name":           name,
        "reference_text": reference_text,
        "description":    description,
        "tags":           tags,
        "visibility":     visibility,
        "language":       language,
        "file_id":        file_id,
        "audio_url":      audio_url,
        "created_at":     datetime.utcnow().isoformat(),
    }

    try:
        result = supabase.table("voice_profiles").insert(new_profile).execute()
        return jsonify({"success": True, "profile": result.data[0]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/voices/delete/<profile_id>", methods=["DELETE"])
@login_required
def delete_profile(profile_id):
    """Supprime un profil vocal (uniquement si appartient Ã  l'utilisateur)."""
    user_id = session["user_id"]
    try:
        # VÃ©rification propriÃ©tÃ©
        check = supabase.table("voice_profiles")\
            .select("id, file_id")\
            .eq("id", profile_id)\
            .eq("user_id", user_id)\
            .single()\
            .execute()

        if not check.data:
            return jsonify({"success": False, "error": "Profil introuvable"}), 404

        # Suppression du fichier audio dans Storage
        file_id = check.data.get("file_id")
        if file_id:
            try:
                supabase.storage.from_(STORAGE_BUCKET)\
                    .remove([f"{user_id}/{file_id}"])
            except Exception:
                pass  # Non bloquant

        # Suppression du profil en base
        supabase.table("voice_profiles")\
            .delete()\
            .eq("id", profile_id)\
            .execute()

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# ROUTE UPLOAD â€” Fichier audio de rÃ©fÃ©rence
# =============================================================================

@app.route("/api/upload-reference", methods=["POST"])
@login_required
def upload_reference():
    """
    Upload un fichier audio de rÃ©fÃ©rence vers Supabase Storage.

    FormData : file (audio/*)

    Retourne : { success: true, file_id: "uuid", duration_estimate: 30 }
    """
    if "file" not in request.files:
        return jsonify({"success": False, "error": "Aucun fichier fourni"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"success": False, "error": "Nom de fichier manquant"}), 400

    # VÃ©rification extension
    ext = os.path.splitext(f.filename)[1].lower()
    allowed = {".wav", ".mp3", ".ogg", ".flac", ".webm", ".m4a"}
    if ext not in allowed:
        return jsonify({"success": False, "error": f"Format non supportÃ©: {ext}"}), 400

    user_id = session["user_id"]
    file_id = str(uuid.uuid4())
    storage_path = f"{user_id}/{file_id}{ext}"

    try:
        # Lecture du contenu
        # Auto-creation du bucket si necessaire (premier run / mauvais projet).
        ensure_storage_bucket()

        file_bytes = f.read()

        # Upload vers Supabase Storage
        supabase.storage.from_(STORAGE_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": f.content_type or "audio/wav"}
        )

        return jsonify({
            "success":           True,
            "file_id":           f"{file_id}{ext}",  # On garde l'extension pour retrouver le fichier
            "storage_path":      storage_path,
            "original_filename": f.filename,
        })

    except Exception as e:
        err = str(e)
        if "Bucket not found" in err:
            err = f"Bucket Supabase introuvable: {STORAGE_BUCKET}. Verifie SUPABASE_URL/SUPABASE_KEY (service_role)."
        return jsonify({"success": False, "error": f"Upload echoue: {err}"}), 500
# =============================================================================

@app.route("/api/transcribe/<file_id>", methods=["POST"])
@login_required
def transcribe_audio(file_id):
    """
    Lance la transcription Whisper d'un fichier audio via Modal.run.

    URL params : file_id = nom du fichier uploadÃ©
    Body JSON  : { model: "small" | "large-v3" }

    Le flux :
      1. RÃ©cupÃ¨re le fichier depuis Supabase Storage
      2. L'envoie au endpoint Whisper sur Modal.run
      3. Retourne la transcription

    Retourne : { success: true, text: "La transcription...", language: "fr" }
    """
    data        = request.get_json() or {}
    model       = data.get("model", "small")  # "small" ou "large-v3"
    user_id     = session["user_id"]

    # Validation du model
    if model not in ("small", "large", "large-v3"):
        model = "small"
    if model == "large":
        model = "large-v3"

    # RÃ©cupÃ©ration du fichier depuis Supabase Storage
    storage_path = f"{user_id}/{file_id}"
    try:
        file_bytes = supabase.storage.from_(STORAGE_BUCKET).download(storage_path)
    except Exception as e:
        return jsonify({"success": False, "error": f"Fichier audio introuvable: {e}"}), 404

    # Envoi vers Modal Whisper endpoint
    try:
        if (
            not MODAL_WHISPER_URL
            or "your-app--whisper-transcribe" in MODAL_WHISPER_URL
            or "votre-app--whisper" in MODAL_WHISPER_URL
        ):
            return jsonify({
                "success": False,
                "error": "MODAL_WHISPER_URL non configurÃ©e (placeholder dÃ©tectÃ©)."
            }), 500

        whisper_payload_files = {"audio": (file_id, file_bytes, "audio/wav")}
        whisper_payload_data = {"model": model}

        whisper_endpoint = _derive_modal_endpoint(MODAL_WHISPER_URL, "transcribe") or MODAL_WHISPER_URL
        whisper_response = requests.post(
            whisper_endpoint,
            files=whisper_payload_files,
            data=whisper_payload_data,
            timeout=120
        )

        if not whisper_response.ok:
            return jsonify({
                "success": False,
                "error":   f"Erreur Modal Whisper (HTTP {whisper_response.status_code})"
            }), 502

        result = whisper_response.json()
        text = result.get("text") or result.get("transcript") or ""

        if not text:
            return jsonify({"success": False, "error": "Transcription vide"}), 422

        return jsonify({
            "success":  True,
            "text":     text.strip(),
            "language": result.get("language", "fr"),
            "model":    model,
        })

    except requests.Timeout:
        return jsonify({"success": False, "error": "Timeout â€” Whisper a pris trop de temps. Essayez le modÃ¨le Small."}), 504
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur transcription: {e}"}), 500


# =============================================================================
# ROUTE GÃ‰NÃ‰RATION VOCALE â€” XTTS v2 via Modal
# =============================================================================

@app.route("/api/generate", methods=["POST"])
@login_required
def generate_voice():
    """
    GÃ©nÃ¨re un fichier audio via XTTS v2 sur Modal.run.

    Body JSON :
    {
      text:       "Le texte Ã  synthÃ©tiser",
      profile_id: "uuid",          â† ID du profil vocal Supabase
      language:   "fr",
      speed:      1.0,
      temperature: 0.7
    }

    Retourne : { success: true, audio_url: "https://..." } ou stream audio

    Note : Pour de grosses demandes, prÃ©fÃ©rer un systÃ¨me de job async.
           Pour l'instant, on attend la rÃ©ponse Modal directement.
    """
    data       = request.get_json() or {}
    user_id    = session["user_id"]
    text       = (data.get("text") or "").strip()
    profile_id = data.get("profile_id")
    language   = data.get("language", "fr")
    speed      = _clamp_float(data.get("speed", 1.0), 1.0, 0.5, 2.0)
    temperature = _clamp_float(data.get("temperature", 0.7), 0.7, 0.01, 2.0)
    top_k = _clamp_int(data.get("top_k", 60), 60, 1, 200)
    top_p = _clamp_float(data.get("top_p", 0.90), 0.90, 0.1, 1.0)
    repetition_penalty = _clamp_float(data.get("repetition_penalty", 2.2), 2.2, 1.0, 10.0)
    length_penalty = _clamp_float(data.get("length_penalty", 1.0), 1.0, 0.1, 5.0)
    enable_text_splitting = str(data.get("enable_text_splitting", "1")).strip().lower() in {"1", "true", "yes", "on"}
    gpt_cond_len = _clamp_int(data.get("gpt_cond_len", 12), 12, 1, 30)
    gpt_cond_chunk_len = _clamp_int(data.get("gpt_cond_chunk_len", 4), 4, 1, 10)
    max_ref_len = _clamp_int(data.get("max_ref_len", 10), 10, 3, 20)
    sound_norm_refs = str(data.get("sound_norm_refs", "0")).strip().lower() in {"1", "true", "yes", "on"}

    # Safer decode profile for laugh/emotive bursts, without truncating repetitions.
    if _has_laugh_intent(text):
        temperature = min(temperature, 0.55)
        top_k = min(top_k, 60)
        top_p = min(top_p, 0.90)
        repetition_penalty = max(repetition_penalty, 1.9)
        length_penalty = min(length_penalty, 1.0)
        sound_norm_refs = True

    if not text:
        return jsonify({"success": False, "error": "Texte requis"}), 400
    if len(text) > 5000:
        return jsonify({"success": False, "error": "Texte trop long (max 5000 caractÃ¨res)"}), 400

    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Session invalide"}), 401

    est_seconds = _estimate_audio_seconds(text, speed)
    trial = _get_trial_status_for_user(user)
    if trial.get("is_trial"):
        if not trial.get("active"):
            return jsonify({
                "success": False,
                "error": "Essai expirÃ© ou quota atteint",
                "trial": trial,
            }), 403
        if trial.get("remaining_seconds", 0) < est_seconds:
            return jsonify({
                "success": False,
                "error": "Quota essai insuffisant pour ce texte",
                "trial": trial,
                "required_seconds": est_seconds,
            }), 403

    # RÃ©cupÃ©ration du profil vocal
    if not profile_id:
        return jsonify({"success": False, "error": "profile_id requis"}), 400

    try:
        profile_result = (
            supabase.table("voice_profiles")
            .select("*")
            .eq("id", profile_id)
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        profile = profile_result.data
    except Exception:
        return jsonify({"success": False, "error": "Profil vocal introuvable"}), 404

    # TÃ©lÃ©chargement du fichier audio de rÃ©fÃ©rence
    file_id      = profile.get("file_id", "")
    storage_path = f"{profile['user_id']}/{file_id}"

    try:
        reference_audio = supabase.storage.from_(STORAGE_BUCKET).download(storage_path)
    except Exception as e:
        return jsonify({"success": False, "error": f"Audio de rÃ©fÃ©rence introuvable: {e}"}), 404

    # Synthèse vocale — Fish Audio API (priorité) ou Modal XTTS (fallback)
    try:
        fish_reference_id = profile.get("fish_reference_id") or ""
        audio_bytes = None

        if FISH_API_KEY:
            # --- Fish Audio API ---
            fish_payload = {
                "text": text,
                "format": "wav",
                "latency": "normal",
                "normalize": True,
            }
            if fish_reference_id:
                fish_payload["reference_id"] = fish_reference_id
            else:
                # Zero-shot : encode l'audio de référence en base64
                import base64 as _b64
                fish_payload["references"] = [{
                    "audio": _b64.b64encode(reference_audio).decode(),
                    "text": profile.get("reference_text", "") or "",
                }]

            fish_resp = requests.post(
                FISH_AUDIO_TTS_URL,
                headers={
                    "Authorization": f"Bearer {FISH_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=fish_payload,
                timeout=60,
            )
            if fish_resp.ok:
                audio_bytes = fish_resp.content
            else:
                err = ""
                try:
                    err = fish_resp.json().get("message", fish_resp.text[:200])
                except Exception:
                    err = fish_resp.text[:200]
                print(f"[WARN] Fish Audio API error {fish_resp.status_code}: {err} — fallback XTTS")

        if audio_bytes is None:
            # --- Fallback Modal XTTS ---
            if not _is_xtts_url_configured():
                return jsonify({
                    "success": False,
                    "error": "Fish API non configurée et MODAL_XTTS_URL absent."
                }), 500
            xtts_response = requests.post(
                _get_xtts_clone_url(),
                files={"speaker_wav": (file_id, reference_audio, "audio/wav")},
                data={
                    "text":                   text,
                    "reference_text":         profile.get("reference_text", ""),
                    "language":               language,
                    "speed":                  str(speed),
                    "temperature":            str(temperature),
                    "top_k":                  str(top_k),
                    "top_p":                  str(top_p),
                    "repetition_penalty":     str(repetition_penalty),
                    "length_penalty":         str(length_penalty),
                    "enable_text_splitting":  "1" if enable_text_splitting else "0",
                    "gpt_cond_len":           str(gpt_cond_len),
                    "gpt_cond_chunk_len":     str(gpt_cond_chunk_len),
                    "max_ref_len":            str(max_ref_len),
                    "sound_norm_refs":        "1" if sound_norm_refs else "0",
                },
                timeout=300,
            )
            if not xtts_response.ok:
                err = ""
                try:
                    err = xtts_response.json().get("error", "")
                except Exception:
                    pass
                return jsonify({
                    "success": False,
                    "error": f"Erreur Modal XTTS (HTTP {xtts_response.status_code}): {err}"
                }), 502
            audio_bytes = xtts_response.content

        # Upload du rÃ©sultat dans Supabase Storage pour accÃ¨s ultÃ©rieur
        result_path = f"generated/{user_id}/{uuid.uuid4()}.wav"
        try:
            supabase.storage.from_(STORAGE_BUCKET).upload(
                path=result_path,
                file=audio_bytes,
                file_options={"content-type": "audio/wav"}
            )
            audio_url = _storage_public_or_signed_url(result_path, expires_in=3600)
        except Exception:
            audio_url = ""  # Non bloquant

        source = "trial:web" if trial.get("is_trial") else "web"
        try:
            supabase.table("generation_logs").insert({
                "user_id": user_id,
                "profile_id": profile_id,
                "text_length": len(text),
                "duration_ms": int(est_seconds * 1000),
                "source": source,
            }).execute()
        except Exception as log_err:
            print(f"[WARN] generation_logs insert failed: {log_err}")

        # Retourne le fichier directement en streaming si possible
        return jsonify({
            "success":   True,
            "audio_url": audio_url,
            "profile":   profile.get("name", ""),
            "estimated_seconds": est_seconds,
        })

    except requests.Timeout:
        return jsonify({"success": False, "error": "Timeout — Synthèse trop longue. Réduisez le texte."}), 504
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur génération: {e}"}), 500


# =============================================================================
# ROUTE FISH AUDIO — Upload référence vocale → fish_reference_id
# =============================================================================

@app.route("/api/fish/upload-reference", methods=["POST"])
@login_required
def fish_upload_reference():
    """
    Upload l'audio de référence d'un profil vocal vers Fish Audio API,
    stocke le fish_reference_id dans Supabase voice_profiles.

    Body JSON : { "profile_id": "uuid" }
    Retourne  : { "success": true, "fish_reference_id": "..." }

    Appeler une fois après création du profil vocal pour pré-enregistrer
    la voix chez Fish Audio. Les appels /api/generate suivants utiliseront
    cet ID au lieu de renvoyer l'audio à chaque fois → latence réduite.
    """
    if not FISH_API_KEY:
        return jsonify({"success": False, "error": "FISH_API_KEY non configurée"}), 501

    data = request.get_json() or {}
    user_id = session["user_id"]
    profile_id = data.get("profile_id")

    if not profile_id:
        return jsonify({"success": False, "error": "profile_id requis"}), 400

    # Récupération du profil
    try:
        profile_result = (
            supabase.table("voice_profiles")
            .select("*")
            .eq("id", profile_id)
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        profile = profile_result.data
    except Exception:
        return jsonify({"success": False, "error": "Profil vocal introuvable"}), 404

    # Téléchargement de l'audio de référence depuis Supabase
    file_id = profile.get("file_id", "")
    storage_path = f"{profile['user_id']}/{file_id}"
    try:
        reference_audio = supabase.storage.from_(STORAGE_BUCKET).download(storage_path)
    except Exception as e:
        return jsonify({"success": False, "error": f"Audio de référence introuvable: {e}"}), 404

    # Upload vers Fish Audio API
    try:
        import base64 as _b64
        fish_resp = requests.post(
            "https://api.fish.audio/v1/models",
            headers={"Authorization": f"Bearer {FISH_API_KEY}"},
            json={
                "title": profile.get("name", f"kommz-{profile_id[:8]}"),
                "type": "tts",
                "train_mode": "fast",
                "visibility": "private",
                "base_model": "speech-1.5",
                "voices": [{
                    "audio": _b64.b64encode(reference_audio).decode(),
                    "text": profile.get("reference_text", "") or "",
                }],
            },
            timeout=120,
        )
        if not fish_resp.ok:
            err = ""
            try:
                err = fish_resp.json().get("message", fish_resp.text[:300])
            except Exception:
                err = fish_resp.text[:300]
            return jsonify({
                "success": False,
                "error": f"Fish Audio upload error {fish_resp.status_code}: {err}"
            }), 502

        fish_model_id = fish_resp.json().get("_id") or fish_resp.json().get("id") or ""
        if not fish_model_id:
            return jsonify({"success": False, "error": "Fish Audio n'a pas retourné d'ID modèle"}), 502

    except requests.Timeout:
        return jsonify({"success": False, "error": "Timeout upload Fish Audio"}), 504
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur upload Fish Audio: {e}"}), 500

    # Sauvegarde du fish_reference_id dans Supabase
    try:
        supabase.table("voice_profiles").update(
            {"fish_reference_id": fish_model_id}
        ).eq("id", profile_id).eq("user_id", user_id).execute()
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Fish upload OK mais échec sauvegarde Supabase: {e}",
            "fish_reference_id": fish_model_id,
        }), 500

    return jsonify({
        "success": True,
        "fish_reference_id": fish_model_id,
        "profile_id": profile_id,
    })


# =============================================================================
# ROUTE API EXTERNE — Génération via clé API (pour intégrations tierces)
# =============================================================================

@app.route("/v1/synthesis", methods=["POST"])
@app.route("/v1/synthesis/", methods=["POST"])
@app.route("/api/v1/synthesis", methods=["POST"])
@app.route("/api/v1/synthesis/", methods=["POST"])
def api_synthesis():
    """
    Endpoint API externe â€” authentification par clÃ© API dans le header.

    Header : Authorization: Bearer KV-VOTRE_CLE_API
    Body JSON :
    {
      "text":     "Texte Ã  synthÃ©tiser",
      "voice_id": "uuid_profil",
      "speed":    1.0,
      "language": "fr"
    }
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "ClÃ© API manquante"}), 401

    api_key = auth_header.split(" ", 1)[1].strip()

    try:
        user_result = supabase.table("users")\
            .select("*")\
            .eq("api_key", api_key)\
            .single()\
            .execute()
        api_user = user_result.data
    except Exception:
        return jsonify({"error": "ClÃ© API invalide"}), 401

    if not api_user:
        return jsonify({"error": "ClÃ© API invalide"}), 401

    # On simule une session pour rÃ©utiliser generate_voice()
    data       = request.get_json() or {}
    text       = (data.get("text") or "").strip()
    profile_id = data.get("voice_id")
    language   = data.get("language", "fr")
    speed      = _clamp_float(data.get("speed", 1.0), 1.0, 0.5, 2.0)
    temperature = _clamp_float(data.get("temperature", 0.7), 0.7, 0.01, 2.0)
    top_k = _clamp_int(data.get("top_k", 60), 60, 1, 200)
    top_p = _clamp_float(data.get("top_p", 0.90), 0.90, 0.1, 1.0)
    repetition_penalty = _clamp_float(data.get("repetition_penalty", 2.2), 2.2, 1.0, 10.0)
    length_penalty = _clamp_float(data.get("length_penalty", 1.0), 1.0, 0.1, 5.0)
    enable_text_splitting = str(data.get("enable_text_splitting", "1")).strip().lower() in {"1", "true", "yes", "on"}
    gpt_cond_len = _clamp_int(data.get("gpt_cond_len", 12), 12, 1, 30)
    gpt_cond_chunk_len = _clamp_int(data.get("gpt_cond_chunk_len", 4), 4, 1, 10)
    max_ref_len = _clamp_int(data.get("max_ref_len", 10), 10, 3, 20)
    sound_norm_refs = str(data.get("sound_norm_refs", "0")).strip().lower() in {"1", "true", "yes", "on"}

    # Safer decode profile for laugh/emotive bursts, without truncating repetitions.
    if _has_laugh_intent(text):
        temperature = min(temperature, 0.55)
        top_k = min(top_k, 60)
        top_p = min(top_p, 0.90)
        repetition_penalty = max(repetition_penalty, 1.9)
        length_penalty = min(length_penalty, 1.0)
        sound_norm_refs = True

    if not text or not profile_id:
        return jsonify({"error": "text et voice_id requis"}), 400

    est_seconds = _estimate_audio_seconds(text, speed)
    trial = _get_trial_status_for_user(api_user)
    if trial.get("is_trial"):
        if not trial.get("active"):
            return jsonify({"error": "Essai expirÃ© ou quota atteint", "trial": trial}), 403
        if trial.get("remaining_seconds", 0) < est_seconds:
            return jsonify({
                "error": "Quota essai insuffisant pour ce texte",
                "trial": trial,
                "required_seconds": est_seconds,
            }), 403

    # Logique identique Ã  /api/generate (extrait pour Ã©viter la dÃ©pendance session)
    try:
        profile_result = supabase.table("voice_profiles")\
            .select("*")\
            .eq("id", profile_id)\
            .single()\
            .execute()
        profile = profile_result.data
    except Exception:
        return jsonify({"error": "Profil vocal introuvable"}), 404

    # VÃ©rification accÃ¨s (public OU appartient Ã  l'utilisateur API)
    if profile.get("visibility") == "private" and profile.get("user_id") != api_user["id"]:
        return jsonify({"error": "AccÃ¨s refusÃ© Ã  ce profil privÃ©"}), 403

    storage_path = f"{profile['user_id']}/{profile.get('file_id', '')}"
    try:
        reference_audio = supabase.storage.from_(STORAGE_BUCKET).download(storage_path)
    except Exception as e:
        return jsonify({"error": f"Audio de rÃ©fÃ©rence introuvable: {e}"}), 404

    try:
        if not _is_xtts_url_configured():
            return jsonify({"error": "MODAL_XTTS_URL non configuree (placeholder detecte)."}), 500

        xtts_response = requests.post(
            _get_xtts_clone_url(),
            files={"speaker_wav": (profile.get("file_id", "ref.wav"), reference_audio, "audio/wav")},
            data={
                "text":           text,
                "reference_text": profile.get("reference_text", ""),
                "language":       language,
                "speed":          str(speed),
                "temperature":    str(temperature),
                "top_k":          str(top_k),
                "top_p":          str(top_p),
                "repetition_penalty": str(repetition_penalty),
                "length_penalty": str(length_penalty),
                "enable_text_splitting": "1" if enable_text_splitting else "0",
                "gpt_cond_len": str(gpt_cond_len),
                "gpt_cond_chunk_len": str(gpt_cond_chunk_len),
                "max_ref_len": str(max_ref_len),
                "sound_norm_refs": "1" if sound_norm_refs else "0",
            },
            timeout=300
        )

        if not xtts_response.ok:
            return jsonify({"error": "Erreur synthÃ¨se XTTS"}), 502

        result_path = f"generated/{api_user['id']}/{uuid.uuid4()}.wav"
        supabase.storage.from_(STORAGE_BUCKET).upload(
            path=result_path,
            file=xtts_response.content,
            file_options={"content-type": "audio/wav"}
        )
        audio_url = _storage_public_or_signed_url(result_path, expires_in=3600)
        source = "trial:api" if trial.get("is_trial") else "api"
        try:
            supabase.table("generation_logs").insert({
                "user_id": api_user["id"],
                "profile_id": profile_id,
                "text_length": len(text),
                "duration_ms": int(est_seconds * 1000),
                "source": source,
            }).execute()
        except Exception as log_err:
            print(f"[WARN] generation_logs insert failed (api): {log_err}")

        return jsonify({"success": True, "audio_url": audio_url, "estimated_seconds": est_seconds})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# ADMIN â€” Routes de gestion des clÃ©s (protÃ©gÃ©es par clÃ© admin)
# =============================================================================

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Admin-Secret", "")
        if not hmac.compare_digest(token, ADMIN_SECRET):
            return jsonify({"error": "Non autorisÃ©"}), 403
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/keys", methods=["GET"])
@admin_required
def admin_list_keys():
    """Liste toutes les clÃ©s de licence."""
    try:
        result = supabase.table("license_keys").select("*").order("created_at", desc=True).execute()
        return jsonify({"success": True, "keys": result.data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/admin/users", methods=["GET"])
@admin_required
def admin_list_users():
    """Liste tous les utilisateurs (sans les mots de passe)."""
    try:
        result = supabase.table("users")\
            .select("id, email, license_key, created_at")\
            .order("created_at", desc=True)\
            .execute()
        return jsonify({"success": True, "users": result.data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/admin/stats", methods=["GET"])
@admin_required
def admin_stats():
    """Statistiques globales."""
    try:
        users_count    = len(supabase.table("users").select("id").execute().data or [])
        profiles_count = len(supabase.table("voice_profiles").select("id").execute().data or [])
        keys_count     = len(supabase.table("license_keys").select("key_value").eq("is_activated", True).execute().data or [])
        return jsonify({
            "users":            users_count,
            "voice_profiles":   profiles_count,
            "activated_keys":   keys_count,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.route("/health", methods=["GET"])
def health():
    """Endpoint de santÃ© pour Render.com."""
    return jsonify({
        "status":  "ok",
        "version": "1.0.0",
        "service": "Kommz Voice Web Server",
    })


def _parse_version_tuple(version_text: str) -> tuple:
    """Convertit '4.2.1' en tuple comparable (4, 2, 1)."""
    raw = (version_text or "").strip()
    if not raw:
        return tuple()
    parts = []
    for p in raw.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        if not num:
            break
        parts.append(int(num))
    return tuple(parts)


@app.route("/update/check-desktop", methods=["GET"])
def update_check_desktop():
    """
    Endpoint de check update pour le logiciel desktop.
    Ex: GET /update/check-desktop?current=4.1&channel=stable
    """
    current = (request.args.get("current") or "").strip()
    channel = (request.args.get("channel") or "stable").strip().lower()
    platform = (request.args.get("platform") or "windows").strip().lower()

    latest = (DESKTOP_STABLE_VERSION or "").strip().lstrip("vV")
    current_t = _parse_version_tuple(current)
    latest_t = _parse_version_tuple(latest)
    update_available = bool(latest_t and current_t and latest_t > current_t)
    if not current_t and latest_t:
        update_available = True
    message = "Vous êtes à jour."
    if update_available:
        message = f"Nouvelle version disponible: {latest}"
        summary = _fetch_desktop_changelog_summary(DESKTOP_CHANGELOG_URL)
        if summary:
            message = f"{message}\n{summary}"

    return jsonify({
        "ok": True,
        "channel": channel,
        "platform": platform,
        "current_version": current,
        "latest_version": latest,
        "update_available": update_available,
        "download_url": DESKTOP_DOWNLOAD_URL,
        "changelog_url": DESKTOP_CHANGELOG_URL,
        "download_sha256": DESKTOP_DOWNLOAD_SHA256,
        "force_update": DESKTOP_FORCE_UPDATE,
        "minimum_version": DESKTOP_MINIMUM_VERSION,
        "message": message,
    })


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    print(f"[KOMMZ VOICE] DÃ©marrage sur port {port} â€” debug={debug}")
    app.run(host="0.0.0.0", port=port, debug=debug)
