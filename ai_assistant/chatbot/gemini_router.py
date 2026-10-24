# gemini_router.py
"""
Sends a chat turn to Gemini, switching model or API key when one fails.

  - "high demand" / overloaded (503, 500, timeout)  -> try the next model
  - "quota exceeded" (429)                          -> try the next API key (same model);
                                                       when every key is out, try the next model
  - model not found (404)                           -> skip that model
  - invalid / blocked API key                       -> skip that key

A model or key that just failed is put on a short cooldown so the following
messages skip it straight away instead of wasting a request on it.
"""
import os
import time
import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler

from django.conf import settings
from google.ai import generativelanguage as glm
from google.api_core import exceptions as gexc
from google.generativeai import GenerativeModel

from .constants import API_KEYS, MODEL_FALLBACKS

# Cooldowns (seconds) after a failure
MODEL_BUSY_COOLDOWN = 60          # 503 high demand
KEY_RATE_COOLDOWN = 60            # 429 per-minute quota
KEY_DAILY_COOLDOWN = 60 * 60      # 429 per-day quota
BROKEN_COOLDOWN = 60 * 60         # model not found / invalid key

MAX_ATTEMPTS = 6                  # most requests sent for one user message
TIME_BUDGET_SECONDS = 90          # stop switching after this long
REQUEST_TIMEOUT_SECONDS = 60

# Stay a little under the free-tier per-minute limit of each key (check https://ai.dev/rate-limit).
MAX_CALLS_PER_MINUTE_PER_KEY = 8
MAX_CALLS_PER_MINUTE = MAX_CALLS_PER_MINUTE_PER_KEY * max(len(API_KEYS), 1)

# ---------------------------------------------------------------------------
# Usage tracing: console + log file
#   Log file: <BASE_DIR>/logs/gemini_usage.log
# ---------------------------------------------------------------------------
logger = logging.getLogger("gemini_usage")
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [GEMINI] %(message)s", "%Y-%m-%d %H:%M:%S")

    _console = logging.StreamHandler()
    _console.setFormatter(_fmt)
    logger.addHandler(_console)

    try:
        _log_dir = os.path.join(settings.BASE_DIR, "logs")
        os.makedirs(_log_dir, exist_ok=True)
        _file = RotatingFileHandler(
            os.path.join(_log_dir, "gemini_usage.log"),
            maxBytes=2_000_000, backupCount=3, encoding="utf-8",
        )
        _file.setFormatter(_fmt)
        logger.addHandler(_file)
    except Exception as log_error:
        print("Could not create gemini_usage.log:", log_error)

    logger.setLevel(logging.INFO)
    logger.propagate = False

# In-memory list of call times in the last 60 seconds (per server process).
_call_times = deque()
_call_lock = threading.Lock()


def record_call():
    with _call_lock:
        _call_times.append(time.time())


def calls_in_last_minute():
    cutoff = time.time() - 60
    with _call_lock:
        while _call_times and _call_times[0] < cutoff:
            _call_times.popleft()
        return len(_call_times)


def key_label(key_index):
    """Safe name for a key in logs and the usage page (never the key itself)."""
    return f"key{key_index + 1}"


def log_gemini_call(session_id, model_name, key_index, status, started, usage=None, error=None):
    """Write one trace line per Gemini request."""
    latency = time.time() - started
    line = (
        f"model={model_name} key={key_label(key_index)} session={session_id} status={status} "
        f"latency={latency:.2f}s "
        f"prompt_tokens={getattr(usage, 'prompt_token_count', 0) or 0} "
        f"output_tokens={getattr(usage, 'candidates_token_count', 0) or 0} "
        f"total_tokens={getattr(usage, 'total_token_count', 0) or 0} "
        f"calls_last_minute={calls_in_last_minute()}"
    )
    if error:
        line += f" error={str(error)[:300]!r}"
    if status == "success":
        logger.info(line)
    else:
        logger.warning(line)


# ---------------------------------------------------------------------------
# One API client per key (so switching keys never touches the global SDK config)
# ---------------------------------------------------------------------------
_clients = {}
_clients_lock = threading.Lock()


def _client_for(key_index):
    with _clients_lock:
        if key_index not in _clients:
            _clients[key_index] = glm.GenerativeServiceClient(
                client_options={"api_key": API_KEYS[key_index]}
            )
        return _clients[key_index]


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------
_model_cooldown = {}       # model -> until
_key_cooldown = {}         # key_index -> until
_pair_cooldown = {}        # (key_index, model) -> until  (quota is per project per model)
_cooldown_lock = threading.Lock()


def _cool(table, item, seconds):
    with _cooldown_lock:
        table[item] = time.time() + seconds


def _is_cooling(table, item):
    with _cooldown_lock:
        return table.get(item, 0) > time.time()


def _candidates():
    """(model, key_index) pairs to try, in order, skipping ones on cooldown."""
    pairs = []
    for model in MODEL_FALLBACKS:
        if _is_cooling(_model_cooldown, model):
            continue
        for key_index in range(len(API_KEYS)):
            if _is_cooling(_key_cooldown, key_index) or _is_cooling(_pair_cooldown, (key_index, model)):
                continue
            pairs.append((model, key_index))
    if not pairs:
        # Everything is cooling down: give the full list one more go rather than refusing outright.
        pairs = [(m, k) for m in MODEL_FALLBACKS for k in range(len(API_KEYS))]
    return pairs


def _is_daily_quota(error):
    text = str(error).lower()
    return "perday" in text or "per day" in text or "per_day" in text


def _is_bad_key(error):
    text = str(error).lower()
    return isinstance(error, (gexc.PermissionDenied, gexc.Unauthenticated)) or (
        isinstance(error, gexc.InvalidArgument) and "api key" in text
    )


class AllModelsFailed(Exception):
    """Every model/key we tried failed. `rate_limited` is True when quota was the main problem."""

    def __init__(self, last_error, rate_limited):
        super().__init__(str(last_error))
        self.last_error = last_error
        self.rate_limited = rate_limited


class GeminiResult:
    def __init__(self, response, model_name, key_index, attempts, started):
        self.response = response
        self.model_name = model_name
        self.key_index = key_index
        self.key_label = key_label(key_index)
        self.attempts = attempts
        self.latency = round(time.time() - started, 2)


def send_with_fallback(system_instruction, history, parts, session_id=None):
    """Send one chat turn, falling back across models and keys. Returns GeminiResult."""
    if not API_KEYS:
        raise RuntimeError("No Gemini API key configured (see API_KEYS in constants.py).")

    first_started = time.time()
    attempts = 0
    last_error = None
    saw_quota = saw_busy = False
    skip_models = set()       # models that failed during THIS message
    skip_pairs = set()

    for model_name, key_index in _candidates():
        if model_name in skip_models or (key_index, model_name) in skip_pairs:
            continue
        if attempts >= MAX_ATTEMPTS or time.time() - first_started > TIME_BUDGET_SECONDS:
            break

        attempts += 1
        model = GenerativeModel(model_name=model_name, system_instruction=system_instruction)
        model._client = _client_for(key_index)
        chat = model.start_chat(history=history)

        logger.info(
            "SENDING model=%s key=%s session=%s attempt=%s history_messages=%s",
            model_name, key_label(key_index), session_id, attempts, len(history),
        )
        started = time.time()
        record_call()
        try:
            response = chat.send_message(
                parts,
                request_options={"retry": None, "timeout": REQUEST_TIMEOUT_SECONDS},
            )
        except gexc.ResourceExhausted as e:
            # Quota exceeded for this key's project on this model -> next key
            last_error, saw_quota = e, True
            log_gemini_call(session_id, model_name, key_index, "quota_exceeded", started, error=e)
            _cool(_pair_cooldown, (key_index, model_name),
                  KEY_DAILY_COOLDOWN if _is_daily_quota(e) else KEY_RATE_COOLDOWN)
            skip_pairs.add((key_index, model_name))
            continue
        except (gexc.ServiceUnavailable, gexc.InternalServerError, gexc.DeadlineExceeded) as e:
            # Model overloaded ("high demand") -> next model
            last_error, saw_busy = e, True
            log_gemini_call(session_id, model_name, key_index, "model_busy", started, error=e)
            _cool(_model_cooldown, model_name, MODEL_BUSY_COOLDOWN)
            skip_models.add(model_name)
            continue
        except gexc.NotFound as e:
            # Model name does not exist / not available for this key -> next model
            last_error = e
            log_gemini_call(session_id, model_name, key_index, "model_not_found", started, error=e)
            _cool(_model_cooldown, model_name, BROKEN_COOLDOWN)
            skip_models.add(model_name)
            continue
        except Exception as e:
            if _is_bad_key(e):
                last_error = e
                log_gemini_call(session_id, model_name, key_index, "bad_key", started, error=e)
                _cool(_key_cooldown, key_index, BROKEN_COOLDOWN)
                skip_pairs.update((key_index, m) for m in MODEL_FALLBACKS)
                continue
            # Anything else (bad file, blocked prompt, ...) won't be fixed by switching
            log_gemini_call(session_id, model_name, key_index, "error", started, error=e)
            raise

        log_gemini_call(session_id, model_name, key_index, "success", started,
                        getattr(response, "usage_metadata", None))
        if attempts > 1:
            logger.info("FALLBACK used model=%s key=%s after %s attempts",
                        model_name, key_label(key_index), attempts)
        return GeminiResult(response, model_name, key_index, attempts, started)

    raise AllModelsFailed(last_error, rate_limited=saw_quota and not saw_busy)
