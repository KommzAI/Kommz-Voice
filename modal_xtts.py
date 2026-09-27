"""
KOMMZ VOICE - XTTS v2 Modal endpoint

Deploy:
  pip install modal
  modal deploy modal_xtts.py

API:
  POST /clone
  FormData:
    - speaker_wav: file
    - text: str
    - reference_text: str (optional)
    - language: str (default: fr)
    - speed: float (default: 1.0)
    - temperature: float (accepted for API compatibility, currently not used by XTTS)

  POST /warmup
  Header (optional): Authorization: Bearer <api_key>
  Required when deployed with XTTS_WARMUP_REQUIRE_KEY=1.

  /clone and /synthesis accept the same header (/synthesis also reads
  `api_key` from the body). Required when deployed with
  XTTS_INFER_REQUIRE_KEY=1.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import wave
import functools
import threading
import subprocess
import re
import hashlib
from pathlib import Path
from typing import Optional

import modal
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response


XTTS_MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"

# Le volume `kommz-xtts-cache` n'est plus utilise : il n'a jamais rien
# conserve, faute de commit(). Il peut etre supprime avec
#   modal volume rm kommz-xtts-cache
# Le modele vit desormais dans l'image (voir _bake_xtts_model).


def _bake_xtts_model():
    """Telecharge XTTS v2 pendant la construction de l'image.

    Pourquoi : le volume `kommz-xtts-cache` etait monte sur
    /root/.local/share/tts, mais un modal.Volume ne persiste PAS les ecritures
    sans `commit()`, et ce code ne l'appelait jamais. Le modele telecharge a
    l'execution disparaissait donc a la mort du conteneur, et chaque cold start
    le retelechargeait depuis Internet. C'est ce que mesurait
    `load_time=62.20s`.

    Un modele place dans l'image est un calque, monte instantanement, sans
    reseau et sans commit a gerer. En contrepartie l'image est plus grosse,
    ce qui est exactement le compromis qu'on veut ici.
    """
    import os

    os.environ["COQUI_TOS_AGREED"] = "1"
    try:
        from TTS.utils.manage import ModelManager

        ModelManager(progress_bar=False).download_model(XTTS_MODEL_NAME)
        print(f"[XTTS][build] modele telecharge via ModelManager : {XTTS_MODEL_NAME}")
        return
    except Exception as exc_manager:
        print(f"[XTTS][build] ModelManager indisponible ({exc_manager}), repli sur TTS()")

    # Repli : charger via l'API haut niveau. Necessite le meme correctif
    # torch.load que le runtime, torch 2.5 refusant les checkpoints Coqui.
    import functools

    import torch

    _torch_load = torch.load

    @functools.wraps(_torch_load)
    def _compat_torch_load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return _torch_load(*args, **kwargs)

    torch.load = _compat_torch_load  # type: ignore[assignment]

    from TTS.api import TTS

    TTS(XTTS_MODEL_NAME, gpu=False)
    print(f"[XTTS][build] modele telecharge via TTS() : {XTTS_MODEL_NAME}")


image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .env({"COQUI_TOS_AGREED": "1"})
    .pip_install(
        "TTS==0.22.0",
        "transformers==4.39.3",
        "tokenizers==0.15.2",
        "torch==2.5.1",
        "torchaudio==2.5.1",
        "numpy",
        "soundfile",
        "fastapi",
        "python-multipart",
        "cutlet",
        "fugashi",
        "unidic-lite",
        "supabase==2.6.0",
    )
    # Le modele est fige dans l'image. Si cette etape echoue, le deploiement
    # echoue : mieux vaut le savoir maintenant que payer 60 s a chaque
    # demarrage en production.
    .run_function(_bake_xtts_model)
)


# Image legere pour les endpoints web. `clone`, `warmup`, `synthesis` et
# `health` sont de simples relais : ils ne chargent ni torch ni TTS, ils
# appellent la classe GPU. Les faire demarrer sur l'image complete obligeait a
# tirer plusieurs gigaoctets (torch 2.5.1 + TTS 0.22 + transformers) pour
# renvoyer un JSON. C'est un cold start paye avant meme d'avoir atteint le GPU.
#
# XTTS_WARMUP_REQUIRE_KEY est lu au `modal deploy`, comme les reglages
# ci-dessous, puis inscrit dans l'environnement de l'image des relais : c'est
# ce qui le rend visible dans le conteneur. A "1", /warmup refuse les appels
# sans cle d'API. XTTS_INFER_REQUIRE_KEY fait de meme pour /clone et
# /synthesis.
XTTS_WARMUP_REQUIRE_KEY = os.environ.get("XTTS_WARMUP_REQUIRE_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
XTTS_INFER_REQUIRE_KEY = os.environ.get("XTTS_INFER_REQUIRE_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
proxy_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi",
        "python-multipart",
        "supabase==2.6.0",  # utilise uniquement par /synthesis
        # Pour reduire la reference avant de la transmettre au conteneur GPU.
        # Quelques dizaines de mega-octets, sans commune mesure avec l'image
        # complete, et le relais demarre toujours en quelques centaines de ms.
        "numpy",
        "soundfile",
        "soxr",  # reechantillonnage : 1,34 Mo -> 896 Ko sur la meme reference
    )
    .env({
        "XTTS_WARMUP_REQUIRE_KEY": "1" if XTTS_WARMUP_REQUIRE_KEY else "0",
        "XTTS_INFER_REQUIRE_KEY": "1" if XTTS_INFER_REQUIRE_KEY else "0",
    })
)

app = modal.App("kommz-voice-xtts", image=image)

# ATTENTION : ces deux valeurs sont lues par `os.environ` au moment du
# `modal deploy`, sur la machine qui deploie — PAS dans le conteneur. Un secret
# Modal ou une variable definie dans le dashboard n'a aucun effet ici : il
# arrive trop tard. Pour les changer il faut les exporter avant de deployer :
#
#   XTTS_MIN_CONTAINERS=1 XTTS_IDLE_TIMEOUT=600 modal deploy modal_xtts.py
#
# Sans cela, min_containers vaut 0 : le conteneur GPU s'eteint apres
# XTTS_IDLE_TIMEOUT secondes d'inactivite et la requete suivante paie le
# demarrage complet plus le chargement du modele.
XTTS_MIN_CONTAINERS = int(os.environ.get("XTTS_MIN_CONTAINERS", "0"))
XTTS_IDLE_TIMEOUT = int(os.environ.get("XTTS_IDLE_TIMEOUT", "300"))
# Nombre maximal de conteneurs GPU simultanes, toutes routes confondues. Au-dela,
# Modal met les appels en file au lieu d'allumer un A10G de plus.
XTTS_MAX_CONTAINERS = int(os.environ.get("XTTS_MAX_CONTAINERS", "3"))
if XTTS_MAX_CONTAINERS < 1 or XTTS_MIN_CONTAINERS > XTTS_MAX_CONTAINERS:
    raise ValueError(
        f"XTTS_MAX_CONTAINERS={XTTS_MAX_CONTAINERS} doit valoir au moins 1 et "
        f"au moins XTTS_MIN_CONTAINERS={XTTS_MIN_CONTAINERS}"
    )
print(
    f"[XTTS][deploy] min_containers={XTTS_MIN_CONTAINERS} "
    f"max_containers={XTTS_MAX_CONTAINERS} "
    f"idle_timeout={XTTS_IDLE_TIMEOUT}s "
    f"warmup_require_key={int(XTTS_WARMUP_REQUIRE_KEY)} "
    f"infer_require_key={int(XTTS_INFER_REQUIRE_KEY)} "
    f"(lus a l'instant du deploy, pas dans le conteneur)"
)
XTTS_POSTPROCESS_MODE = os.environ.get("XTTS_POSTPROCESS_MODE", "strong").strip().lower()
XTTS_MASTERING_ENABLED = os.environ.get("XTTS_MASTERING_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
XTTS_LAUGH_MASTERING_ENABLED = os.environ.get("XTTS_LAUGH_MASTERING_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
XTTS_LAUGH_BREATH_REDUCTION_ENABLED = os.environ.get("XTTS_LAUGH_BREATH_REDUCTION_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
XTTS_REF_CLEAN_ENABLED = os.environ.get("XTTS_REF_CLEAN_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
XTTS_REF_MAX_SEC = max(5.0, min(15.0, float(os.environ.get("XTTS_REF_MAX_SEC", "10"))))
XTTS_DEFAULT_TOP_K = int(os.environ.get("XTTS_DEFAULT_TOP_K", "60"))
XTTS_DEFAULT_TOP_P = float(os.environ.get("XTTS_DEFAULT_TOP_P", "0.90"))
XTTS_DEFAULT_REPETITION_PENALTY = float(os.environ.get("XTTS_DEFAULT_REPETITION_PENALTY", "2.2"))
XTTS_DEFAULT_LENGTH_PENALTY = float(os.environ.get("XTTS_DEFAULT_LENGTH_PENALTY", "1.0"))
XTTS_DEFAULT_ENABLE_TEXT_SPLITTING = os.environ.get("XTTS_DEFAULT_ENABLE_TEXT_SPLITTING", "1").strip().lower() in {"1", "true", "yes", "on"}
XTTS_DEFAULT_GPT_COND_LEN = int(os.environ.get("XTTS_DEFAULT_GPT_COND_LEN", "12"))
XTTS_DEFAULT_GPT_COND_CHUNK_LEN = int(os.environ.get("XTTS_DEFAULT_GPT_COND_CHUNK_LEN", "4"))
XTTS_DEFAULT_MAX_REF_LEN = int(os.environ.get("XTTS_DEFAULT_MAX_REF_LEN", "10"))
XTTS_DEFAULT_SOUND_NORM_REFS = os.environ.get("XTTS_DEFAULT_SOUND_NORM_REFS", "0").strip().lower() in {"1", "true", "yes", "on"}
XTTS_CONDITIONING_CACHE_ENABLED = os.environ.get("XTTS_CONDITIONING_CACHE_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
# Le warmup execute une inference jetable pour absorber la compilation des
# noyaux CUDA, que la premiere generation reelle payait a la place.
XTTS_WARMUP_RUN_INFERENCE = os.environ.get("XTTS_WARMUP_RUN_INFERENCE", "1").strip().lower() in {"1", "true", "yes", "on"}
# Instantane memoire : restaure un conteneur qui a deja importe Coqui et deja
# charge le modele. Mesure visee : import_tts=26.6s + read_and_build=15.5s.
# Mettre a "0" et redeployer suffit a revenir au comportement precedent si
# l'instantane pose probleme.
XTTS_MEMORY_SNAPSHOT = os.environ.get("XTTS_MEMORY_SNAPSHOT", "1").strip().lower() in {"1", "true", "yes", "on"}
XTTS_CONDITIONING_CACHE_MAX_ITEMS = max(8, min(256, int(os.environ.get("XTTS_CONDITIONING_CACHE_MAX_ITEMS", "64"))))
XTTS_FORCE_SPLIT_CHAR_LIMITS = {"ja": 71}
XTTS_SUPPORTED_LANGS = {
    "en",
    "es",
    "fr",
    "de",
    "it",
    "pt",
    "pl",
    "tr",
    "ru",
    "nl",
    "cs",
    "ar",
    "zh-cn",
    "hu",
    "ko",
    "ja",
    "hi",
}
XTTS_LANGUAGE_ALIASES = {
    "zh": "zh-cn",
    "zh_cn": "zh-cn",
    "zh-tw": "zh-cn",
    "pt-br": "pt",
    "pt-pt": "pt",
    "cs-cz": "cs",
    "kk": "ru",
    "uk": "ru",
    "bg": "ru",
    "sr": "ru",
    "mk": "ru",
    "be": "ru",
}


@app.cls(
    gpu="A10G",
    memory=16384,
    scaledown_window=XTTS_IDLE_TIMEOUT,
    min_containers=XTTS_MIN_CONTAINERS,
    max_containers=XTTS_MAX_CONTAINERS,
    # PAS de volume sur /root/.local/share/tts : un volume monte MASQUE le
    # contenu de l'image a cet emplacement. Le modele etant desormais fige
    # dans l'image, le monter ici reviendrait a le cacher et a retelecharger.
    enable_memory_snapshot=XTTS_MEMORY_SNAPSHOT,
)
class XTTSModel:
    @staticmethod
    def _normalize_xtts_language(language: str, text: str = "") -> str:
        lang = (language or "fr").strip().lower().replace("_", "-")
        if lang in XTTS_SUPPORTED_LANGS:
            return lang
        if lang in XTTS_LANGUAGE_ALIASES:
            return XTTS_LANGUAGE_ALIASES[lang]

        short = lang.split("-", 1)[0]
        if short in XTTS_SUPPORTED_LANGS:
            return short
        if short in XTTS_LANGUAGE_ALIASES:
            return XTTS_LANGUAGE_ALIASES[short]
        if short == "zh":
            return "zh-cn"

        sample = str(text or "")
        if re.search(r"[\u0600-\u06FF]", sample):
            return "ar"
        if re.search(r"[\u0900-\u097F]", sample):
            return "hi"
        if re.search(r"[\uAC00-\uD7AF]", sample):
            return "ko"
        if re.search(r"[\u3040-\u30FF]", sample):
            return "ja"
        if re.search(r"[\u4E00-\u9FFF]", sample):
            return "zh-cn"
        if re.search(r"[\u0400-\u04FF]", sample):
            return "ru"
        return "en"

    @staticmethod
    def _to_bool(value, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _file_sha1(path: str, chunk_size: int = 1024 * 1024) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _conditioning_cache_key(
        self,
        speaker_path: str,
        language: str,
        gpt_cond_len: int,
        gpt_cond_chunk_len: int,
        max_ref_len: int,
        sound_norm_refs: bool,
    ) -> str:
        ref_hash = self._file_sha1(speaker_path)
        raw = f"{ref_hash}|{language}|{gpt_cond_len}|{gpt_cond_chunk_len}|{max_ref_len}|{int(sound_norm_refs)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _get_conditioning_latents_cached(
        self,
        speaker_path: str,
        language: str,
        gpt_cond_len: int,
        gpt_cond_chunk_len: int,
        max_ref_len: int,
        sound_norm_refs: bool,
    ):
        if not XTTS_CONDITIONING_CACHE_ENABLED:
            return None, None, False

        if not hasattr(self, "_cond_cache_lock"):
            self._cond_cache_lock = threading.Lock()
        if not hasattr(self, "_cond_cache"):
            self._cond_cache = {}

        key = self._conditioning_cache_key(
            speaker_path=speaker_path,
            language=language,
            gpt_cond_len=gpt_cond_len,
            gpt_cond_chunk_len=gpt_cond_chunk_len,
            max_ref_len=max_ref_len,
            sound_norm_refs=sound_norm_refs,
        )

        with self._cond_cache_lock:
            row = self._cond_cache.get(key)
            if row:
                row["last_used"] = time.time()
                return row.get("gpt_cond_latent"), row.get("speaker_embedding"), True

        # Build latents (cache miss)
        model = self.tts.synthesizer.tts_model
        # API-compatible call in TTS 0.22.x
        gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
            audio_path=[speaker_path],
            gpt_cond_len=gpt_cond_len,
            gpt_cond_chunk_len=gpt_cond_chunk_len,
            max_ref_length=max_ref_len,
            sound_norm_refs=sound_norm_refs,
        )

        with self._cond_cache_lock:
            self._cond_cache[key] = {
                "gpt_cond_latent": gpt_cond_latent,
                "speaker_embedding": speaker_embedding,
                "created_at": time.time(),
                "last_used": time.time(),
            }
            # LRU trim
            if len(self._cond_cache) > XTTS_CONDITIONING_CACHE_MAX_ITEMS:
                oldest_key = min(self._cond_cache.items(), key=lambda kv: kv[1].get("last_used", 0.0))[0]
                self._cond_cache.pop(oldest_key, None)

        return gpt_cond_latent, speaker_embedding, False

    def _xtts_infer_with_cached_conditioning(
        self,
        text: str,
        language: str,
        speaker_path: str,
        out_path: str,
        speed: float,
        temperature: float,
        top_k: int,
        top_p: float,
        repetition_penalty: float,
        length_penalty: float,
        enable_text_splitting: bool,
        gpt_cond_len: int,
        gpt_cond_chunk_len: int,
        max_ref_len: int,
        sound_norm_refs: bool,
    ) -> tuple[bool, str]:
        """
        Try the fast path using cached conditioning latents.
        Returns (ok, status) where status is one of:
        - cache_hit
        - cache_miss
        - fallback_error:<reason>
        """
        try:
            import numpy as np
            import soundfile as sf

            model = self.tts.synthesizer.tts_model
            gpt_cond_latent, speaker_embedding, cache_hit = self._get_conditioning_latents_cached(
                speaker_path=speaker_path,
                language=language,
                gpt_cond_len=gpt_cond_len,
                gpt_cond_chunk_len=gpt_cond_chunk_len,
                max_ref_len=max_ref_len,
                sound_norm_refs=sound_norm_refs,
            )

            # XTTS 0.22 inference path
            infer = model.inference(
                text=text,
                language=language,
                gpt_cond_latent=gpt_cond_latent,
                speaker_embedding=speaker_embedding,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                length_penalty=length_penalty,
                speed=speed,
                enable_text_splitting=enable_text_splitting,
            )

            wav = None
            if isinstance(infer, dict):
                wav = infer.get("wav")
            if wav is None and hasattr(infer, "get"):
                try:
                    wav = infer.get("wav")
                except Exception:
                    wav = None
            if wav is None:
                wav = infer

            wav = np.asarray(wav, dtype=np.float32)
            if wav.ndim > 1:
                wav = wav.squeeze()
            sf.write(out_path, wav, 24000, subtype="PCM_16")
            return True, ("cache_hit" if cache_hit else "cache_miss")
        except Exception as e:
            return False, f"fallback_error:{e}"

    @staticmethod
    def _normalize_emotive_text(text: str, language: str) -> str:
        """Make laugh-like interjections easier for XTTS to pronounce."""
        src = (text or "").strip()
        if not src:
            return src

        lang = (language or "fr").strip().lower().split("-")[0]
        out = src

        # Normalize common laugh variants from speech recognition.
        # Examples: "AH AH AH", "ah ah", "ha ha ha", "ahaha"
        laugh_token = r"(?:a+h+|h+a+)"
        laugh_seq = re.compile(rf"\b{laugh_token}(?:[\s,.;:!?-]+{laugh_token})+\b", re.IGNORECASE)
        laugh_repeat = re.compile(r"\b(?:a?h){3,}a?\b", re.IGNORECASE)

        if lang in {"ja"}:
            out = laugh_seq.sub("ははは…", out)
            out = laugh_repeat.sub("ははは…", out)
        else:
            out = laugh_seq.sub("ha ha ha !", out)
            out = laugh_repeat.sub("ha ha ha !", out)

        # Other common emotion markers from live speech recognition.
        if lang in {"ja"}:
            rules = [
                (re.compile(r"\b(?:えー+|えっと+|うー+ん|うーん)\b", re.IGNORECASE), "えっと…"),
                (re.compile(r"\b(?:わあ+|おお+|おー+)\b", re.IGNORECASE), "わあ！"),
                (re.compile(r"\b(?:はぁ+|ふぅ+)\b", re.IGNORECASE), "はぁ…"),
                (re.compile(r"\b(?:しくしく|えーん)\b", re.IGNORECASE), "しくしく…"),
                (re.compile(r"\b(?:ぐるる+|ぐぬぬ)\b", re.IGNORECASE), "ぐるる…"),
                (re.compile(r"\b(?:あっ+|あー+|うわ+)\b", re.IGNORECASE), "あっ！"),
                (re.compile(r"\b(?:やった+)\b", re.IGNORECASE), "やった！"),
                (re.compile(r"\b(?:おっと+)\b", re.IGNORECASE), "おっと…"),
                (re.compile(r"\b(?:えっ+)\b", re.IGNORECASE), "えっ？"),
            ]
        else:
            rules = [
                (re.compile(r"\b(?:rire|rires|je\s+ris|je\s+rigole|rigole|rigoler)\b", re.IGNORECASE), "ha ha ha !"),
                (re.compile(r"\b(?:mdr|lol)\b", re.IGNORECASE), "ha ha ha !"),
                (re.compile(r"\b(?:euh+|heu+|hmm+|hum+)\b", re.IGNORECASE), "euh..."),
                (re.compile(r"\b(?:wow+|wo+w+|oh+)\b", re.IGNORECASE), "oh !"),
                (re.compile(r"\b(?:pff+|pfou+|soupir+)\b", re.IGNORECASE), "pff..."),
                (re.compile(r"\b(?:snif+|sob+)\b", re.IGNORECASE), "snif..."),
                (re.compile(r"\b(?:grr+|grrr+)\b", re.IGNORECASE), "grr..."),
                (re.compile(r"\b(?:hein+)\b", re.IGNORECASE), "hein ?"),
                (re.compile(r"\b(?:bah+|ben+|bof+)\b", re.IGNORECASE), "bah..."),
                (re.compile(r"\b(?:ouf+)\b", re.IGNORECASE), "ouf..."),
                (re.compile(r"\b(?:hop+)\b", re.IGNORECASE), "hop !"),
                (re.compile(r"\b(?:aie+|ouch+|aouh+)\b", re.IGNORECASE), "aïe !"),
                (re.compile(r"\b(?:beurk+|berk+)\b", re.IGNORECASE), "beurk..."),
                (re.compile(r"\b(?:bravo+|yeah+|yes+)\b", re.IGNORECASE), "yeah !"),
                (re.compile(r"\b(?:hein ?quoi+)\b", re.IGNORECASE), "hein ?"),
            ]
        for pat, repl in rules:
            out = pat.sub(repl, out)

        if lang in {"fr"}:
            # STT occasionally outputs single letters for laugh syllables.
            out = re.sub(
                r"\b(?:a+h?|h+a?)(?:[\s,.;:!?-]+(?:a+h?|h+a?)){2,}\b",
                "ha ha ha !",
                out,
                flags=re.IGNORECASE,
            )

        # Long repeated vowels often come from live dictation ("noooon", "ouiiii").
        out = re.sub(r"\b([A-Za-zÀ-ÿ])\1{3,}\b", r"\1\1\1", out)

        # Keep punctuation sane for synthesis.
        out = re.sub(r"([!?.,]){2,}", r"\1", out)
        out = re.sub(r"\s{2,}", " ", out).strip()
        return out

    @staticmethod
    def _wav_duration_seconds(wav_bytes: bytes) -> float:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_bytes)
                tmp_path = tmp.name
            try:
                with wave.open(tmp_path, "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate() or 1
                    return float(frames) / float(rate)
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
        except Exception:
            return 0.0

    @staticmethod
    def _postprocess_audio(audio, sr: int, laugh_mode: bool = False):
        """Reduce startup artifacts and avoid clipping without changing voice identity."""
        import numpy as np

        if audio is None:
            return audio

        y = np.asarray(audio, dtype=np.float32)
        if y.size == 0:
            return y

        # Remove DC offset (helps "muffled/pop" onset on some generations).
        y = y - float(np.mean(y))

        # Peak management.
        peak = float(np.max(np.abs(y)))
        if XTTS_POSTPROCESS_MODE == "ultra_safe":
            target_peak = 0.58
        elif XTTS_POSTPROCESS_MODE == "strong":
            target_peak = 0.72
        else:
            target_peak = 0.90
        # Laugh-like segments can spike harder: force extra headroom.
        if laugh_mode:
            target_peak = min(target_peak, 0.68)
        if peak > target_peak and peak > 1e-6:
            y = y * (target_peak / peak)

        # Soft limiter to tame harsh transients.
        if XTTS_POSTPROCESS_MODE == "ultra_safe":
            y = np.tanh(y * 1.45) / np.tanh(1.45)
        elif XTTS_POSTPROCESS_MODE == "strong":
            y = np.tanh(y * 1.15) / np.tanh(1.15)
        if laugh_mode:
            y = np.tanh(y * 1.20) / np.tanh(1.20)
            y = y * 0.98

        # Fade-in/out to remove clicks and "muffled burst" at start.
        if XTTS_POSTPROCESS_MODE == "ultra_safe":
            fade_ms = 180
        elif XTTS_POSTPROCESS_MODE == "strong":
            fade_ms = 120
        else:
            fade_ms = 14
        n = max(1, int(sr * (fade_ms / 1000.0)))
        n = min(n, max(1, y.shape[0] // 8))
        if n > 1:
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            if y.ndim == 1:
                y[:n] *= ramp
                y[-n:] *= ramp[::-1]
            else:
                y[:n, :] *= ramp[:, None]
                y[-n:, :] *= ramp[::-1, None]

        # Extra startup guard for strong/ultra modes: hard mute at the very beginning.
        if XTTS_POSTPROCESS_MODE in {"strong", "ultra_safe"}:
            if XTTS_POSTPROCESS_MODE == "ultra_safe":
                guard_sec = 0.080
                pad_sec = 0.050
            else:
                guard_sec = 0.040
                pad_sec = 0.030
            guard_n = min(y.shape[0], max(1, int(sr * guard_sec)))
            if y.ndim == 1:
                y[:guard_n] *= 0.0
            else:
                y[:guard_n, :] *= 0.0

            # Prepend a tiny silence to hide startup artifacts from playback devices.
            pad_n = max(1, int(sr * pad_sec))
            if y.ndim == 1:
                y = np.concatenate([np.zeros(pad_n, dtype=np.float32), y], axis=0)
            else:
                y = np.concatenate([np.zeros((pad_n, y.shape[1]), dtype=np.float32), y], axis=0)

        return np.clip(y, -1.0, 1.0)

    @staticmethod
    def _master_with_ffmpeg(in_wav: str, out_wav: str, laugh_mode: bool = False) -> bool:
        """
        Apply a conservative mastering chain to reduce sporadic clipping/saturation.
        Falls back silently when ffmpeg or filter is unavailable.
        """
        # Keep chain conservative to preserve voice identity.
        if laugh_mode and XTTS_LAUGH_MASTERING_ENABLED:
            laugh_chain = [
                "highpass=f=70",
                "lowpass=f=13500",
            ]
            if XTTS_LAUGH_BREATH_REDUCTION_ENABLED and XTTS_POSTPROCESS_MODE == "ultra_safe":
                # Tames breath-like hiss/noise often heard during laugh interjections.
                laugh_chain.append("afftdn=nf=-22:tn=1")
            laugh_chain.extend(
                [
                    "acompressor=threshold=-20dB:ratio=2.4:attack=5:release=110:makeup=0",
                    "alimiter=limit=0.62",
                    "volume=-3.0dB",
                ]
            )
            af = ",".join(laugh_chain)
        else:
            af = ",".join(
                [
                    "highpass=f=55",
                    "lowpass=f=15500",
                    "alimiter=limit=0.74",
                    "volume=-1.5dB",
                ]
            )
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            in_wav,
            "-af",
            af,
            "-c:a",
            "pcm_s16le",
            out_wav,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=20)
            return p.returncode == 0 and os.path.exists(out_wav)
        except Exception:
            return False

    @staticmethod
    def _probe_duration_seconds(path: str) -> float:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=8)
            if p.returncode != 0:
                return 0.0
            return float((p.stdout or "0").strip())
        except Exception:
            return 0.0

    @staticmethod
    def _prepare_reference_audio(in_path: str) -> str:
        """
        Normalize/clean reference audio for stable cloning.
        Returns processed path or original path on failure.
        """
        dur = XTTSModel._probe_duration_seconds(in_path)
        trim_args = []
        if dur > XTTS_REF_MAX_SEC:
            trim_args = ["-ss", "0", "-t", f"{XTTS_REF_MAX_SEC:.2f}"]

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out_path = tmp.name

        # Conservative chain for reference cleaning (not output mastering).
        af = ",".join(
            [
                "highpass=f=60",
                "lowpass=f=15000",
                "dynaudnorm=f=120:g=12",
            ]
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            in_path,
            *trim_args,
            "-af",
            af,
            "-ac",
            "1",
            "-ar",
            "32000",
            "-c:a",
            "pcm_s16le",
            out_path,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            if p.returncode == 0 and os.path.exists(out_path):
                return out_path
        except Exception:
            pass
        try:
            os.remove(out_path)
        except Exception:
            pass
        return in_path

    # ------------------------------------------------------------------
    # Chargement en deux temps, pour l'instantane memoire
    # ------------------------------------------------------------------
    # Mesure decisive : import_tts=26.64s, import_torch=2.21s,
    # read_and_build=15.47s, cuda_init=0.05s, to_cuda=0.35s.
    # Autrement dit 44 des 45 secondes sont du travail CPU pur, refait a
    # l'identique par chaque conteneur qui demarre. Un instantane memoire
    # Modal restaure un processus qui a deja importe et deja charge.
    #
    # Contrainte : pendant la prise d'instantane, aucun GPU n'est disponible.
    # Toute touche a CUDA a ce moment empoisonnerait l'instantane. D'ou la
    # separation stricte :
    #   phase 1 (snap=True)  : imports + chargement CPU du modele
    #   phase 2 (snap=False) : detection CUDA + transfert sur la carte
    # C'est exactement la ou passe le temps, et exactement ce qui est
    # instantaneable.

    def _load_model_cpu(self):
        """Phase 1 : imports et chargement CPU. Ne touche JAMAIS a CUDA."""
        if getattr(self, "_cpu_loaded", False):
            return
        if not hasattr(self, "_init_lock"):
            self._init_lock = threading.Lock()
        with self._init_lock:
            if getattr(self, "_cpu_loaded", False):
                return
            init_t0 = time.perf_counter()
            os.environ.setdefault("COQUI_TOS_AGREED", "1")

            _t = time.perf_counter()
            try:
                import torch
                _torch_load = torch.load

                @functools.wraps(_torch_load)
                def _compat_torch_load(*args, **kwargs):
                    kwargs.setdefault("weights_only", False)
                    return _torch_load(*args, **kwargs)

                torch.load = _compat_torch_load  # type: ignore[assignment]
            except Exception:
                pass
            t_torch_import = time.perf_counter() - _t

            # Le poste le plus lourd, et de loin : l'arbre de dependances de
            # Coqui lu depuis le systeme de fichiers paresseux de l'image.
            _t = time.perf_counter()
            from TTS.api import TTS
            t_tts_import = time.perf_counter() - _t

            load_t0 = time.perf_counter()
            self.tts = TTS(XTTS_MODEL_NAME)
            cpu_load_dt = time.perf_counter() - load_t0

            self._cpu_loaded = True
            self._on_gpu = False
            total = max(0.001, time.perf_counter() - init_t0)
            print(
                f"[XTTS] load_cpu import_torch={t_torch_import:.2f}s "
                f"import_tts={t_tts_import:.2f}s read_and_build={cpu_load_dt:.2f}s "
                f"total={total:.2f}s snapshot={'on' if XTTS_MEMORY_SNAPSHOT else 'off'}"
            )

    def _move_model_to_device(self):
        """Phase 2 : CUDA. Executee apres restauration, jamais dans l'instantane."""
        if getattr(self, "_on_gpu", False):
            return
        # Verrou distinct de la phase 1 : celui de la phase 1 est capture dans
        # l'instantane memoire. On ne reutilise pas un verrou restaure.
        if not hasattr(self, "_gpu_lock"):
            self._gpu_lock = threading.Lock()
        with self._gpu_lock:
            if getattr(self, "_on_gpu", False):
                return
            t0 = time.perf_counter()
            use_gpu = False
            try:
                import torch
                use_gpu = bool(torch.cuda.is_available())
                if use_gpu:
                    torch.set_float32_matmul_precision("high")
            except Exception:
                use_gpu = False
            t_cuda = time.perf_counter() - t0

            move_t0 = time.perf_counter()
            if use_gpu:
                try:
                    self.tts.to("cuda")
                except Exception as exc_to:
                    print(f"[XTTS] .to(cuda) a echoue ({exc_to}), le modele reste sur CPU")
                    use_gpu = False
            move_dt = time.perf_counter() - move_t0

            self._on_gpu = True
            print(
                f"[XTTS] to_device device={'cuda' if use_gpu else 'cpu'} "
                f"pid={os.getpid()} cuda_init={t_cuda:.2f}s to_cuda={move_dt:.2f}s"
            )

    def _ensure_model(self):
        """Garde defensive : les methodes appelees a chaud passent par ici."""
        if getattr(self, "_cpu_loaded", False) and getattr(self, "_on_gpu", False):
            return
        self._load_model_cpu()
        self._move_model_to_device()

    @modal.enter(snap=True)
    def load_snapshot(self):
        # Capturee dans l'instantane : imports + modele en RAM.
        self._load_model_cpu()

    @modal.enter(snap=False)
    def load_runtime(self):
        # Rejouee a chaque demarrage, y compris apres restauration.
        self._move_model_to_device()

    @modal.method()
    def clone(
        self,
        text: str,
        speaker_wav_bytes: bytes,
        speaker_filename: str,
        language: str = "fr",
        speed: float = 1.0,
        temperature: float = 0.7,  # accepted for API compatibility
        top_k: int = XTTS_DEFAULT_TOP_K,
        top_p: float = XTTS_DEFAULT_TOP_P,
        repetition_penalty: float = XTTS_DEFAULT_REPETITION_PENALTY,
        length_penalty: float = XTTS_DEFAULT_LENGTH_PENALTY,
        enable_text_splitting: Optional[bool] = None,
        gpt_cond_len: int = XTTS_DEFAULT_GPT_COND_LEN,
        gpt_cond_chunk_len: int = XTTS_DEFAULT_GPT_COND_CHUNK_LEN,
        max_ref_len: int = XTTS_DEFAULT_MAX_REF_LEN,
        sound_norm_refs: Optional[bool] = None,
        dispatch_ts: float = 0.0,
    ) -> bytes:
        import soundfile as sf

        # Ecart entre l'instant ou le relais a lance l'appel et l'instant ou le
        # conteneur GPU commence a travailler : c'est le temps d'attente pur,
        # serialisation des arguments comprise. Mesure : 14,5 s pour 2,5 s de
        # synthese, sans savoir ou ils partaient.
        if dispatch_ts:
            try:
                print(
                    f"[XTTS] dispatch_wait_ms={(time.time() - float(dispatch_ts)) * 1000:.0f} "
                    f"ref_bytes={len(speaker_wav_bytes or b'')} chars={len(text or '')}"
                )
            except Exception:
                pass

        self._ensure_model()
        t0 = time.perf_counter()

        requested_language = (language or "fr").strip().lower()
        language = self._normalize_xtts_language(requested_language, text)
        if language != requested_language:
            print(f"[XTTS] unsupported language '{requested_language}' -> fallback '{language}'")
        if not text or not text.strip():
            raise ValueError("text is required")
        text = self._normalize_emotive_text(text, language)
        laugh_mode = bool(re.search(r"\b(?:ha+|haha+|hahaha+|はは|ふふ)\b", text, flags=re.IGNORECASE))

        # Clamp speed to sane range.
        try:
            speed = float(speed)
        except Exception:
            speed = 1.0
        speed = max(0.5, min(2.0, speed))

        # XTTS decoding parameters
        try:
            temperature = float(temperature)
        except Exception:
            temperature = 0.7
        temperature = max(0.01, min(2.0, temperature))
        try:
            top_k = int(top_k)
        except Exception:
            top_k = XTTS_DEFAULT_TOP_K
        top_k = max(1, min(200, top_k))
        try:
            top_p = float(top_p)
        except Exception:
            top_p = XTTS_DEFAULT_TOP_P
        top_p = max(0.1, min(1.0, top_p))
        try:
            repetition_penalty = float(repetition_penalty)
        except Exception:
            repetition_penalty = XTTS_DEFAULT_REPETITION_PENALTY
        repetition_penalty = max(1.0, min(10.0, repetition_penalty))
        try:
            length_penalty = float(length_penalty)
        except Exception:
            length_penalty = XTTS_DEFAULT_LENGTH_PENALTY
        length_penalty = max(0.1, min(5.0, length_penalty))
        if enable_text_splitting is None:
            enable_text_splitting = XTTS_DEFAULT_ENABLE_TEXT_SPLITTING
        enable_text_splitting = self._to_bool(enable_text_splitting, XTTS_DEFAULT_ENABLE_TEXT_SPLITTING)
        try:
            gpt_cond_len = int(gpt_cond_len)
        except Exception:
            gpt_cond_len = XTTS_DEFAULT_GPT_COND_LEN
        gpt_cond_len = max(1, min(30, gpt_cond_len))
        try:
            gpt_cond_chunk_len = int(gpt_cond_chunk_len)
        except Exception:
            gpt_cond_chunk_len = XTTS_DEFAULT_GPT_COND_CHUNK_LEN
        gpt_cond_chunk_len = max(1, min(10, gpt_cond_chunk_len))
        try:
            max_ref_len = int(max_ref_len)
        except Exception:
            max_ref_len = XTTS_DEFAULT_MAX_REF_LEN
        max_ref_len = max(3, min(20, max_ref_len))
        if sound_norm_refs is None:
            sound_norm_refs = XTTS_DEFAULT_SOUND_NORM_REFS
        sound_norm_refs = self._to_bool(sound_norm_refs, XTTS_DEFAULT_SOUND_NORM_REFS)
        # Keep short emotive utterances unsplit to preserve style/intonation.
        if laugh_mode and len(text) < 180:
            enable_text_splitting = False
        force_split_limit = XTTS_FORCE_SPLIT_CHAR_LIMITS.get(language)
        if force_split_limit and len(text) > force_split_limit:
            if not enable_text_splitting:
                print(
                    f"[XTTS] force enable text splitting lang={language} "
                    f"chars={len(text)} limit={force_split_limit}"
                )
            enable_text_splitting = True

        suffix = Path(speaker_filename or "speaker.wav").suffix or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as spk:
            spk.write(speaker_wav_bytes)
            speaker_path = spk.name
        prepared_speaker_path = speaker_path
        if XTTS_REF_CLEAN_ENABLED:
            prepared_speaker_path = self._prepare_reference_audio(speaker_path)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out:
            out_path = out.name

        try:
            used_fast_path = False
            cache_status = "disabled"
            if XTTS_CONDITIONING_CACHE_ENABLED:
                ok_fast, cache_status = self._xtts_infer_with_cached_conditioning(
                    text=text,
                    language=language,
                    speaker_path=prepared_speaker_path,
                    out_path=out_path,
                    speed=speed,
                    temperature=temperature,
                    top_k=top_k,
                    top_p=top_p,
                    repetition_penalty=repetition_penalty,
                    length_penalty=length_penalty,
                    enable_text_splitting=enable_text_splitting,
                    gpt_cond_len=gpt_cond_len,
                    gpt_cond_chunk_len=gpt_cond_chunk_len,
                    max_ref_len=max_ref_len,
                    sound_norm_refs=sound_norm_refs,
                )
                used_fast_path = bool(ok_fast)

            if not used_fast_path:
                self.tts.tts_to_file(
                    text=text,
                    speaker_wav=prepared_speaker_path,
                    language=language,
                    file_path=out_path,
                    speed=speed,
                    temperature=temperature,
                    top_k=top_k,
                    top_p=top_p,
                    repetition_penalty=repetition_penalty,
                    length_penalty=length_penalty,
                    enable_text_splitting=enable_text_splitting,
                    gpt_cond_len=gpt_cond_len,
                    gpt_cond_chunk_len=gpt_cond_chunk_len,
                    max_ref_len=max_ref_len,
                    sound_norm_refs=sound_norm_refs,
                )
                if cache_status.startswith("fallback_error:"):
                    print(f"[XTTS] conditioning_cache fallback -> tts_to_file ({cache_status})")
            mastered_path = out_path
            if XTTS_MASTERING_ENABLED:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as mastered_tmp:
                    candidate_mastered = mastered_tmp.name
                if self._master_with_ffmpeg(out_path, candidate_mastered, laugh_mode=laugh_mode):
                    mastered_path = candidate_mastered
                else:
                    try:
                        os.remove(candidate_mastered)
                    except Exception:
                        pass

            audio, sr = sf.read(mastered_path, dtype="float32")
            audio = self._postprocess_audio(audio, int(sr), laugh_mode=laugh_mode)
            # Normalize to 16-bit PCM WAV bytes for downstream compatibility.
            import numpy as np

            pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("int16")
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as final_wav:
                final_path = final_wav.name
            sf.write(final_path, pcm16, sr, subtype="PCM_16")
            with open(final_path, "rb") as f:
                out_bytes = f.read()

            elapsed = max(0.001, time.perf_counter() - t0)
            audio_sec = self._wav_duration_seconds(out_bytes)
            rtf = (elapsed / audio_sec) if audio_sec > 0 else 0.0
            print(
                f"[XTTS] synth_time={elapsed:.3f}s audio={audio_sec:.3f}s "
                f"rtf={rtf:.3f} lang={language} chars={len(text)} speed={speed:.2f} "
                f"temp={temperature:.2f} top_k={top_k} top_p={top_p:.2f} split={int(enable_text_splitting)} "
                f"gpt_cond_len={gpt_cond_len} chunk={gpt_cond_chunk_len} max_ref={max_ref_len} "
                f"norm_ref={int(sound_norm_refs)} cond_cache={cache_status}"
            )
            return out_bytes
        finally:
            for p in (speaker_path, out_path):
                try:
                    os.remove(p)
                except Exception:
                    pass
            try:
                if prepared_speaker_path not in {None, "", speaker_path}:
                    os.remove(prepared_speaker_path)
            except Exception:
                pass
            try:
                if "mastered_path" in locals() and mastered_path not in {None, "", out_path}:
                    os.remove(mastered_path)
            except Exception:
                pass
            try:
                os.remove(final_path)  # type: ignore[name-defined]
            except Exception:
                pass

    @modal.method()
    def warmup(self) -> dict:
        """Charge le modele ET execute une inference jetable.

        Charger le modele ne suffit pas : la premiere inference d'un conteneur
        paie la compilation des noyaux CUDA et l'allocation des buffers. Mesure
        a l'appui, la premiere generation reelle affichait rtf=1.254 alors que
        le regime etabli est bien plus rapide. Cette penalite etait donc payee
        par l'utilisateur au lieu d'etre payee par le warmup.

        La reference utilisee est un signal synthetique : la qualite n'a aucune
        importance, seul le passage dans le graphe compte. L'audio produit est
        jete.
        """
        t0 = time.perf_counter()
        self._ensure_model()
        load_ms = (time.perf_counter() - t0) * 1000.0

        infer_ms = None
        infer_error = ""
        if XTTS_WARMUP_RUN_INFERENCE:
            t1 = time.perf_counter()
            ref_path = ""
            out_path = ""
            try:
                import numpy as np
                import soundfile as sf

                sr = 24000
                dur = 3.0
                t = np.linspace(0.0, dur, int(sr * dur), endpoint=False, dtype=np.float32)
                # Fondamentale + harmoniques, suffisamment "voise" pour que le
                # calcul des latents de conditionnement ne parte pas en erreur.
                sig = (
                    0.35 * np.sin(2 * np.pi * 140.0 * t)
                    + 0.20 * np.sin(2 * np.pi * 280.0 * t)
                    + 0.10 * np.sin(2 * np.pi * 420.0 * t)
                )
                env = 0.5 * (1.0 - np.cos(2 * np.pi * np.clip(t / dur, 0.0, 1.0)))
                sig = (sig * env).astype(np.float32)

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
                    ref_path = ref.name
                sf.write(ref_path, sig, sr, subtype="PCM_16")
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out:
                    out_path = out.name

                ok, status = self._xtts_infer_with_cached_conditioning(
                    text="hello there",
                    language="en",
                    speaker_path=ref_path,
                    out_path=out_path,
                    speed=1.0,
                    temperature=0.7,
                    top_k=XTTS_DEFAULT_TOP_K,
                    top_p=XTTS_DEFAULT_TOP_P,
                    repetition_penalty=XTTS_DEFAULT_REPETITION_PENALTY,
                    length_penalty=XTTS_DEFAULT_LENGTH_PENALTY,
                    enable_text_splitting=False,
                    gpt_cond_len=XTTS_DEFAULT_GPT_COND_LEN,
                    gpt_cond_chunk_len=XTTS_DEFAULT_GPT_COND_CHUNK_LEN,
                    max_ref_len=XTTS_DEFAULT_MAX_REF_LEN,
                    sound_norm_refs=False,
                )
                if not ok:
                    infer_error = str(status)
                infer_ms = (time.perf_counter() - t1) * 1000.0
            except Exception as exc:
                infer_error = f"{type(exc).__name__}: {exc}"
            finally:
                for p in (ref_path, out_path):
                    if not p:
                        continue
                    try:
                        os.remove(p)
                    except Exception:
                        pass

        print(
            f"[XTTS] warmup load_ms={load_ms:.0f} "
            f"infer_ms={(f'{infer_ms:.0f}' if infer_ms is not None else 'skipped')} "
            f"infer_error={infer_error or 'none'}"
        )
        return {
            "ready": bool(hasattr(self, "tts")),
            "model": "xtts_v2",
            "load_ms": round(load_ms, 1),
            "warm_infer_ms": round(infer_ms, 1) if infer_ms is not None else None,
            "warm_infer_error": infer_error,
        }

xtts_actor = XTTSModel()


@app.function(
    # Relais pur : aucune inference ici, donc image legere et pas de volume.
    image=proxy_image,
    secrets=[modal.Secret.from_name("kommz-secrets")],  # verification de la cle
    timeout=600,
    scaledown_window=XTTS_IDLE_TIMEOUT,
    min_containers=XTTS_MIN_CONTAINERS,
)
@modal.fastapi_endpoint(method="POST")
async def clone(
    request: Request,
    speaker_wav: UploadFile = File(...),
    text: str = Form(...),
    reference_text: str = Form(default=""),
    language: str = Form(default="fr"),
    speed: float = Form(default=1.0),
    temperature: float = Form(default=0.7),
    top_k: int = Form(default=XTTS_DEFAULT_TOP_K),
    top_p: float = Form(default=XTTS_DEFAULT_TOP_P),
    repetition_penalty: float = Form(default=XTTS_DEFAULT_REPETITION_PENALTY),
    length_penalty: float = Form(default=XTTS_DEFAULT_LENGTH_PENALTY),
    enable_text_splitting: Optional[bool] = Form(default=None),
    gpt_cond_len: int = Form(default=XTTS_DEFAULT_GPT_COND_LEN),
    gpt_cond_chunk_len: int = Form(default=XTTS_DEFAULT_GPT_COND_CHUNK_LEN),
    max_ref_len: int = Form(default=XTTS_DEFAULT_MAX_REF_LEN),
    sound_norm_refs: Optional[bool] = Form(default=None),
):
    api_key = _bearer_key(request)
    auth, refusal = await _authorize(api_key, XTTS_INFER_REQUIRE_KEY)
    print(
        f"[XTTS][clone] auth={auth} "
        f"key={_key_fingerprint(api_key)[:8] if api_key else '-'} "
        f"{'rejected' if refusal is not None else 'accepted'}"
    )
    if refusal is not None:
        return refusal

    if not text.strip():
        return JSONResponse(status_code=400, content={"error": "text is required"})

    speaker_bytes = await speaker_wav.read()
    if not speaker_bytes:
        return JSONResponse(status_code=400, content={"error": "speaker_wav is empty"})

    try:
        wav_bytes = await xtts_actor.clone.remote.aio(
            text=text.strip(),
            speaker_wav_bytes=speaker_bytes,
            speaker_filename=speaker_wav.filename or "speaker.wav",
            language=language,
            speed=speed,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            length_penalty=length_penalty,
            enable_text_splitting=enable_text_splitting,
            gpt_cond_len=gpt_cond_len,
            gpt_cond_chunk_len=gpt_cond_chunk_len,
            max_ref_len=max_ref_len,
            sound_norm_refs=sound_norm_refs,
            dispatch_ts=time.time(),
        )
        return Response(content=wav_bytes, media_type="audio/wav")
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"XTTS error: {e}"})


# Verification des cles d'API cote relais. Le cache porte une empreinte
# SHA-256 de la cle, jamais la cle elle-meme. Une cle valide est gardee
# 10 minutes, une cle inconnue 1 minute.
_KEY_CACHE = {}
_KEY_CACHE_MAX = 1024
_KEY_TTL_VALID_S = 600.0
_KEY_TTL_INVALID_S = 60.0
_SUPABASE_CLIENT = None


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _bearer_key(request: Request) -> str:
    raw = (request.headers.get("authorization") or "").strip()
    if raw[:7].lower() == "bearer ":
        return raw[7:].strip()
    return ""


def _lookup_api_key_sync(api_key: str) -> Optional[bool]:
    """True : cle connue. False : cle inconnue. None : verification impossible."""
    global _SUPABASE_CLIENT
    try:
        if _SUPABASE_CLIENT is None:
            from supabase import create_client

            supabase_url = os.environ.get("SUPABASE_URL", "").strip()
            supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
            if not supabase_url or not supabase_key:
                print("[XTTS][auth] SUPABASE_URL ou SUPABASE_KEY absent")
                return None
            _SUPABASE_CLIENT = create_client(supabase_url, supabase_key)
        result = _SUPABASE_CLIENT.table("users").select("id").eq("api_key", api_key).limit(1).execute()
        return bool(result.data)
    except Exception as exc:
        # Le type seul : le message d'erreur peut reprendre l'URL de la
        # requete, donc la cle.
        print(f"[XTTS][auth] verification impossible : {type(exc).__name__}")
        return None


async def _check_api_key(api_key: str) -> str:
    """Renvoie "key" (cle connue), "invalid" ou "unverified"."""
    fingerprint = _key_fingerprint(api_key)
    now = time.monotonic()
    cached = _KEY_CACHE.get(fingerprint)
    if cached and cached[1] > now:
        return "key" if cached[0] else "invalid"

    found = await asyncio.to_thread(_lookup_api_key_sync, api_key)
    if found is None:
        return "unverified"
    if len(_KEY_CACHE) >= _KEY_CACHE_MAX:
        for fp in [fp for fp, (_, exp) in _KEY_CACHE.items() if exp <= now]:
            _KEY_CACHE.pop(fp, None)
        if len(_KEY_CACHE) >= _KEY_CACHE_MAX:
            _KEY_CACHE.clear()
    ttl = _KEY_TTL_VALID_S if found else _KEY_TTL_INVALID_S
    _KEY_CACHE[fingerprint] = (found, now + ttl)
    return "key" if found else "invalid"


async def _authorize(api_key: str, require_key: bool):
    """Renvoie (auth, refus) ; refus vaut None si l'appel est accepte.

    auth vaut "key", "legacy" (aucune cle), "unverified" ou "invalid".
    """
    auth = await _check_api_key(api_key) if api_key else "legacy"
    if auth == "invalid":
        return auth, JSONResponse(status_code=401, content={"error": "invalid api key"})
    if require_key and auth == "legacy":
        return auth, JSONResponse(status_code=401, content={"error": "api key required"})
    if require_key and auth == "unverified":
        return auth, JSONResponse(status_code=503, content={"error": "api key verification unavailable"})
    return auth, None


# Rechauffements regroupes par appelant : un groupe par cle d'API, un seul
# groupe pour tous les appels sans cle. Un rechauffement en cours est partage
# entre les appels du groupe ; un rechauffement reussi depuis moins de
# _WARMUP_REUSE_S secondes est renvoye tel quel, sans repasser par le GPU.
_WARMUP_REUSE_S = 60.0
_WARMUP_INFLIGHT = {}
_WARMUP_DONE = {}
_WARMUP_DONE_MAX = 1024


async def _run_warmup_grouped(group: str):
    """Renvoie (resultat, mode), mode valant "run", "shared" ou "reused"."""
    now = time.monotonic()
    done = _WARMUP_DONE.get(group)
    if done and (now - done[0]) < _WARMUP_REUSE_S:
        return done[1], "reused"

    task = _WARMUP_INFLIGHT.get(group)
    if task is not None:
        return await asyncio.shield(task), "shared"

    async def _run():
        try:
            data = await xtts_actor.warmup.remote.aio()
            if len(_WARMUP_DONE) >= _WARMUP_DONE_MAX:
                _WARMUP_DONE.clear()
            _WARMUP_DONE[group] = (time.monotonic(), data or {})
            return data or {}
        finally:
            _WARMUP_INFLIGHT.pop(group, None)

    task = asyncio.ensure_future(_run())
    _WARMUP_INFLIGHT[group] = task
    return await asyncio.shield(task), "run"


@app.function(
    # Relais pur. C'est la route la plus critique pour le cold start : elle
    # doit demarrer en une seconde, pas en tirant plusieurs gigaoctets.
    image=proxy_image,
    secrets=[modal.Secret.from_name("kommz-secrets")],
    timeout=900,
    scaledown_window=XTTS_IDLE_TIMEOUT,
    min_containers=min(XTTS_MIN_CONTAINERS, 1),
    # Un seul conteneur relais, qui accepte de nombreux appels simultanes :
    # le regroupement des rechauffements vaut ainsi pour tous les appelants.
    max_containers=1,
)
@modal.concurrent(max_inputs=100)
@modal.fastapi_endpoint(method="POST")
async def warmup(request: Request):
    t0 = time.perf_counter()
    api_key = _bearer_key(request)
    fingerprint = _key_fingerprint(api_key)[:8] if api_key else "-"

    def _log(auth: str, mode: str) -> None:
        print(
            f"[XTTS][warmup] auth={auth} mode={mode} key={fingerprint} "
            f"total_ms={(time.perf_counter() - t0) * 1000.0:.0f}"
        )

    auth, refusal = await _authorize(api_key, XTTS_WARMUP_REQUIRE_KEY)
    if refusal is not None:
        _log(auth, "rejected")
        return refusal

    # Une cle qui n'a pas pu etre verifiee rejoint le groupe sans cle.
    group = f"key:{_key_fingerprint(api_key)}" if auth == "key" else "legacy"
    try:
        data, mode = await _run_warmup_grouped(group)
    except Exception as e:
        _log(auth, "error")
        return JSONResponse(status_code=500, content={"error": f"warmup failed: {e}"})
    _log(auth, mode)
    return JSONResponse(content={"status": "ok", **data, "mode": mode})


# Cache de references cote relais. La reference ne change pas d'une phrase a
# l'autre, mais elle etait retelechargee depuis Supabase a chaque appel : 1,19 s
# mesurees, sur chaque phrase. Le relais reste vivant XTTS_IDLE_TIMEOUT
# secondes, donc le cache couvre toute une session de jeu.
# La cle porte une empreinte de la cle d'API, jamais la cle elle-meme.
_REF_CACHE = {}
_REF_CACHE_MAX = 8


def _shrink_reference_bytes(raw, max_ref_sec, gpt_cond_sec):
    """Reduit la reference a ce que le modele utilisera reellement.

    Le serveur tronque de toute facon a XTTS_REF_MAX_SEC et reechantillonne en
    mono. Transmettre davantage au conteneur GPU, c'est payer un transfert pour
    des octets jetes a l'arrivee. Mesure : 5 097 682 octets transmis par phrase.

    En cas de doute on renvoie l'original : une reference degradee coute plus
    cher qu'un transfert plus gros.
    """
    if not raw:
        return raw, {"changed": False, "in_bytes": 0, "out_bytes": 0}
    info = {"changed": False, "in_bytes": len(raw), "out_bytes": len(raw)}
    try:
        import io

        import numpy as np
        import soundfile as sf

        keep_sec = max(float(max_ref_sec or 10.0), float(gpt_cond_sec or 12.0)) + 2.0
        keep_sec = max(4.0, min(30.0, keep_sec))

        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        if data.size == 0 or sr <= 0:
            return raw, info
        mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
        max_samples = int(keep_sec * sr)
        if mono.shape[0] > max_samples:
            mono = mono[:max_samples]

        # Le serveur reechantillonne en 32 kHz de toute facon.
        target_sr = 32000
        if sr != target_sr:
            try:
                import soxr

                mono = soxr.resample(mono, sr, target_sr, quality="VHQ")
                sr = target_sr
            except Exception:
                pass  # sans soxr, la troncature seule apporte deja l'essentiel

        out = io.BytesIO()
        pcm16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16)
        sf.write(out, pcm16, sr, format="WAV", subtype="PCM_16")
        shrunk = out.getvalue()
        if shrunk and len(shrunk) < len(raw):
            info.update(changed=True, out_bytes=len(shrunk),
                        seconds=round(float(pcm16.shape[0]) / float(sr), 2), sr=sr)
            return shrunk, info
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return raw, info


@app.function(
    # Relais pur : seul supabase est importe ici, il est dans proxy_image.
    image=proxy_image,
    secrets=[modal.Secret.from_name("kommz-secrets")],
    timeout=900,
    scaledown_window=XTTS_IDLE_TIMEOUT,
    min_containers=XTTS_MIN_CONTAINERS,
)
@modal.fastapi_endpoint(method="POST")
async def synthesis(request: Request):
    """
    Endpoint direct /v1/synthesis — bypass Render.com.
    Accepte JSON ou FormData. Modal fait le lookup Supabase si speaker_wav absent.
    """
    import json, base64, os, time as _time

    # Chronometrage par etape. Mesure a l'appui : 24 s d'execution pour 3 s de
    # synthese, sans savoir si le temps partait dans Supabase, dans l'attente
    # d'un conteneur GPU, ou ailleurs. Supposer ne sert a rien ici.
    _t_start = _time.perf_counter()
    _t_parse = _t_lookup = _t_gpu = 0.0

    # Parse JSON or FormData
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        body = await request.json()
        text = str(body.get("text", "")).strip()
        voice_id = str(body.get("voice_id", "")).strip()
        api_key = str(body.get("api_key", "")).strip()
        language = str(body.get("language", "fr"))
        speed = float(body.get("speed", 1.0))
        temperature = float(body.get("temperature", 0.7))
        top_k = int(body.get("top_k", XTTS_DEFAULT_TOP_K))
        top_p = float(body.get("top_p", XTTS_DEFAULT_TOP_P))
        repetition_penalty = float(body.get("repetition_penalty", XTTS_DEFAULT_REPETITION_PENALTY))
        length_penalty = float(body.get("length_penalty", XTTS_DEFAULT_LENGTH_PENALTY))
        enable_text_splitting = bool(body.get("enable_text_splitting", True))
        gpt_cond_len = int(body.get("gpt_cond_len", XTTS_DEFAULT_GPT_COND_LEN))
        gpt_cond_chunk_len = int(body.get("gpt_cond_chunk_len", XTTS_DEFAULT_GPT_COND_CHUNK_LEN))
        max_ref_len = int(body.get("max_ref_len", XTTS_DEFAULT_MAX_REF_LEN))
        sound_norm_refs = bool(body.get("sound_norm_refs", False))
        speaker_bytes = None
    else:
        form = await request.form()
        text = str(form.get("text", "")).strip()
        voice_id = str(form.get("voice_id", "")).strip()
        api_key = str(form.get("api_key", "")).strip()
        language = str(form.get("language", "fr"))
        speed = float(form.get("speed", 1.0))
        temperature = float(form.get("temperature", 0.7))
        top_k = int(form.get("top_k", XTTS_DEFAULT_TOP_K))
        top_p = float(form.get("top_p", XTTS_DEFAULT_TOP_P))
        repetition_penalty = float(form.get("repetition_penalty", XTTS_DEFAULT_REPETITION_PENALTY))
        length_penalty = float(form.get("length_penalty", XTTS_DEFAULT_LENGTH_PENALTY))
        enable_text_splitting = str(form.get("enable_text_splitting", "1")).strip().lower() in {"1", "true", "yes", "on"}
        gpt_cond_len = int(form.get("gpt_cond_len", XTTS_DEFAULT_GPT_COND_LEN))
        gpt_cond_chunk_len = int(form.get("gpt_cond_chunk_len", XTTS_DEFAULT_GPT_COND_CHUNK_LEN))
        max_ref_len = int(form.get("max_ref_len", XTTS_DEFAULT_MAX_REF_LEN))
        sound_norm_refs = str(form.get("sound_norm_refs", "0")).strip().lower() in {"1", "true", "yes", "on"}
        speaker_wav = form.get("speaker_wav")
        if hasattr(speaker_wav, "read"):
            speaker_bytes = await speaker_wav.read()
        else:
            speaker_bytes = None

    _t_parse = _time.perf_counter() - _t_start

    # La cle peut venir de l'en-tete ou du corps ; l'en-tete l'emporte.
    api_key = _bearer_key(request) or api_key
    auth, refusal = await _authorize(api_key, XTTS_INFER_REQUIRE_KEY)
    print(
        f"[XTTS][synthesis] auth={auth} "
        f"key={_key_fingerprint(api_key)[:8] if api_key else '-'} "
        f"{'rejected' if refusal is not None else 'accepted'}"
    )
    if refusal is not None:
        return refusal

    if not text:
        return JSONResponse(status_code=400, content={"error": "text is required"})
    if not voice_id:
        return JSONResponse(status_code=400, content={"error": "voice_id is required"})

    # Supabase lookup si pas de speaker_wav
    _t_lookup_start = _time.perf_counter()
    _ref_source = "client"
    _cache_key = ""
    if not speaker_bytes and api_key:
        import hashlib as _hl

        _cache_key = _hl.sha1(f"{api_key}|{voice_id}".encode("utf-8")).hexdigest()
        _cached = _REF_CACHE.get(_cache_key)
        if _cached:
            speaker_bytes = _cached
            _ref_source = "cache"

    if not speaker_bytes and api_key:
        _ref_source = "supabase"
        try:
            from supabase import create_client
            supabase_url = os.environ.get("SUPABASE_URL", "").strip()
            supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
            if supabase_url and supabase_key:
                sb = create_client(supabase_url, supabase_key)
                user_result = sb.table("users").select("id").eq("api_key", api_key).single().execute()
                if user_result.data:
                    user_id = user_result.data["id"]
                    prof_result = sb.table("voice_profiles").select("*").eq("id", voice_id).eq("user_id", user_id).single().execute()
                    if prof_result.data:
                        file_id = prof_result.data.get("file_id", "")
                        if file_id:
                            storage_path = f"{user_id}/{file_id}"
                            bucket = sb.storage.from_("voice-references")
                            speaker_bytes = bucket.download(storage_path)
        except Exception as exc_sb:
            # Le type seul : le message peut reprendre l'URL de la requete.
            print(f"[XTTS][synthesis] lookup Supabase echoue : {type(exc_sb).__name__}")

    if not speaker_bytes:
        _t_lookup = _time.perf_counter() - _t_lookup_start
        return JSONResponse(status_code=400, content={"error": "speaker_wav required or Supabase lookup failed"})

    # Reduction avant le saut vers le conteneur GPU : c'est ce saut qui coute,
    # pas la lecture locale. On ne reduit qu'une fois, puis on met en cache la
    # version reduite.
    _shrink_info = {}
    if _ref_source != "cache":
        speaker_bytes, _shrink_info = _shrink_reference_bytes(
            speaker_bytes, max_ref_len, gpt_cond_len
        )
        if _cache_key:
            _REF_CACHE[_cache_key] = speaker_bytes
            while len(_REF_CACHE) > _REF_CACHE_MAX:
                _REF_CACHE.pop(next(iter(_REF_CACHE)), None)

    _t_lookup = _time.perf_counter() - _t_lookup_start

    _t_gpu_start = _time.perf_counter()
    try:
        wav_bytes = await xtts_actor.clone.remote.aio(
            text=text,
            speaker_wav_bytes=speaker_bytes,
            speaker_filename="reference.wav",
            language=language,
            speed=speed,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            length_penalty=length_penalty,
            enable_text_splitting=enable_text_splitting,
            gpt_cond_len=gpt_cond_len,
            gpt_cond_chunk_len=gpt_cond_chunk_len,
            max_ref_len=max_ref_len,
            sound_norm_refs=sound_norm_refs,
            dispatch_ts=_time.time(),
        )
        _t_gpu = _time.perf_counter() - _t_gpu_start
        audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
        _t_total = _time.perf_counter() - _t_start
        # `gpu` inclut l'attente d'un conteneur disponible ET la synthese.
        # L'ecart entre ce chiffre et le `synth_time` logue par la classe est
        # exactement le temps d'attente d'un conteneur.
        print(
            f"[XTTS][synthesis] parse={_t_parse:.2f}s ref_fetch={_t_lookup:.2f}s "
            f"ref_source={_ref_source} "
            f"gpu_dispatch_plus_synth={_t_gpu:.2f}s total={_t_total:.2f}s "
            f"ref_bytes={len(speaker_bytes or b'')} "
            f"shrink={_shrink_info.get('in_bytes', '-')}->{_shrink_info.get('out_bytes', '-')} "
            f"chars={len(text)}"
        )
        return JSONResponse(content={
            "success": True,
            "audio_b64": audio_b64,
            "estimated_seconds": len(wav_bytes) / 32000,
            "timing_ms": {
                "parse": round(_t_parse * 1000, 1),
                "supabase": round(_t_lookup * 1000, 1),
                "gpu_dispatch_plus_synth": round(_t_gpu * 1000, 1),
                "total": round(_t_total * 1000, 1),
            },
        })
    except Exception as e:
        _t_gpu = _time.perf_counter() - _t_gpu_start
        print(
            f"[XTTS][synthesis] ECHEC parse={_t_parse:.2f}s supabase={_t_lookup:.2f}s "
            f"gpu={_t_gpu:.2f}s erreur={type(e).__name__}: {e}"
        )
        return JSONResponse(status_code=500, content={"error": f"XTTS error: {e}"})


@app.function(image=proxy_image, scaledown_window=XTTS_IDLE_TIMEOUT)
@modal.fastapi_endpoint(method="GET")
async def health():
    # Cette route ne touche JAMAIS au conteneur GPU : c'est une fonction
    # distincte qui renvoie une constante. Elle repond "ok" meme si le
    # modele n'est pas charge, et meme si aucun conteneur GPU ne tourne.
    # Elle sert a verifier que le deploiement existe, rien d'autre. Pour
    # savoir si la synthese est chaude, il faut appeler /warmup et regarder
    # si la reponse arrive vite.
    return JSONResponse(
        content={
            "status": "ok",
            "service": "kommz-voice-xtts",
            "model": "xtts_v2",
            "reflects_gpu_state": False,
        }
    )

