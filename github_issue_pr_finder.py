import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from langdetect import DetectorFactory, LangDetectException, detect

DetectorFactory.seed = 0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOTENV_FILE = os.path.join(BASE_DIR, ".env")


def load_dotenv(path):
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue

            os.environ[key] = value.strip().strip('"').strip("'")


def env_int(name, default_value):
    value = os.getenv(name)
    if value is None:
        return default_value

    try:
        return int(value)
    except ValueError:
        return default_value


def env_bool(name, default_value=False):
    value = os.getenv(name)
    if value is None:
        return default_value

    return value.strip().lower() in {"1", "true", "yes", "on"}


load_dotenv(DOTENV_FILE)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

RATE_LIMIT_REFRESH_TTL_SECONDS = 15
LOW_RATE_LIMIT_THRESHOLD = 100
_LAST_RATE_LIMIT_STATE = {
    "limit": None,
    "remaining": None,
    "used": None,
    "reset_at": None,
    "resource": "core",
    "status": "unknown",
    "source": "uninitialized",
    "fetched_at": None,
}

MIN_STARS = env_int("MIN_STARS", 200)
MIN_FILES_CHANGED = env_int("MIN_FILES_CHANGED", 4)
TARGET_MATCHES = env_int("TARGET_MATCHES", 100)
REPOS_PER_PAGE = env_int("REPOS_PER_PAGE", 100)
MAX_REPO_PAGES = env_int("MAX_REPO_PAGES", 10)
PULLS_PER_PAGE = env_int("PULLS_PER_PAGE", 100)
MAX_PULL_PAGES_PER_REPO = env_int("MAX_PULL_PAGES_PER_REPO", 5)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 30)
FULL_SWEEP_PAUSE_SECONDS = env_int("FULL_SWEEP_PAUSE_SECONDS", 900)
RUN_UNTIL_STOP = env_bool("RUN_UNTIL_STOP", False)
ENGLISH_ONLY = env_bool("ENGLISH_ONLY", True)
MAX_REQUEST_RETRIES = env_int("MAX_REQUEST_RETRIES", 5)
RETRY_BACKOFF_SECONDS = env_int("RETRY_BACKOFF_SECONDS", 2)
OUTPUT_FILE = os.path.join(BASE_DIR, "github_issue_pr_matches.json")
STATE_FILE = os.path.join(BASE_DIR, "github_issue_pr_scan_state.json")
ISSUE_REF_PATTERN = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+)",
    re.IGNORECASE,
)

TRIVIAL_KEYWORDS = [
    "documentation",
    "docs",
    "typo",
    "readme",
    "spelling",
    "formatting",
    "comment",
]

UI_SETTINGS_SCHEMA = {
    "GITHUB_TOKEN": {"type": "string", "default": "", "sensitive": True},
    "TARGET_MATCHES": {"type": "int", "default": 100},
    "MIN_STARS": {"type": "int", "default": 200},
    "MIN_FILES_CHANGED": {"type": "int", "default": 4},
    "MAX_REPO_PAGES": {"type": "int", "default": 10},
    "MAX_PULL_PAGES_PER_REPO": {"type": "int", "default": 5},
    "REPOS_PER_PAGE": {"type": "int", "default": 100},
    "PULLS_PER_PAGE": {"type": "int", "default": 100},
    "REQUEST_TIMEOUT": {"type": "int", "default": 30},
    "RUN_UNTIL_STOP": {"type": "bool", "default": False},
    "FULL_SWEEP_PAUSE_SECONDS": {"type": "int", "default": 900},
}

BOOL_TRUE_VALUES = {"1", "true", "yes", "on"}

LANGUAGE_MIN_TEXT_LENGTH = 24
LANGUAGE_WORD_PATTERN = re.compile(r"[A-Za-z]{2,}")
CODE_FENCE_PATTERN = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_PATTERN = re.compile(r"`[^`]+`")
URL_PATTERN = re.compile(r"https?://\S+|www\.\S+")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^\)]+\)")


class RecoverableGitHubError(requests.RequestException):
    """Raised when a transient GitHub API failure persists after retries."""


class ScanStopped(Exception):
    """Raised when a caller requests that a scan stop gracefully."""


def configure_github_token(token):
    global GITHUB_TOKEN, HEADERS

    GITHUB_TOKEN = token or ""
    os.environ["GITHUB_TOKEN"] = GITHUB_TOKEN
    HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    SESSION.headers.pop("Authorization", None)
    if GITHUB_TOKEN:
        SESSION.headers.update(HEADERS)

    _LAST_RATE_LIMIT_STATE["source"] = "token_updated"
    _LAST_RATE_LIMIT_STATE["fetched_at"] = None


def parse_setting_value(name, value):
    schema = UI_SETTINGS_SCHEMA[name]
    setting_type = schema["type"]

    if setting_type == "int":
        return int(value)

    if setting_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in BOOL_TRUE_VALUES

    return "" if value is None else str(value)


def serialize_setting_value(name, value):
    schema = UI_SETTINGS_SCHEMA[name]
    if schema["type"] == "bool":
        return "1" if value else "0"
    return str(value)


def get_settings_payload():
    return {
        "GITHUB_TOKEN": GITHUB_TOKEN,
        "TARGET_MATCHES": TARGET_MATCHES,
        "MIN_STARS": MIN_STARS,
        "MIN_FILES_CHANGED": MIN_FILES_CHANGED,
        "MAX_REPO_PAGES": MAX_REPO_PAGES,
        "MAX_PULL_PAGES_PER_REPO": MAX_PULL_PAGES_PER_REPO,
        "REPOS_PER_PAGE": REPOS_PER_PAGE,
        "PULLS_PER_PAGE": PULLS_PER_PAGE,
        "REQUEST_TIMEOUT": REQUEST_TIMEOUT,
        "RUN_UNTIL_STOP": RUN_UNTIL_STOP,
        "FULL_SWEEP_PAUSE_SECONDS": FULL_SWEEP_PAUSE_SECONDS,
    }


def apply_settings(settings):
    global TARGET_MATCHES
    global MIN_STARS
    global MIN_FILES_CHANGED
    global MAX_REPO_PAGES
    global MAX_PULL_PAGES_PER_REPO
    global REPOS_PER_PAGE
    global PULLS_PER_PAGE
    global REQUEST_TIMEOUT
    global RUN_UNTIL_STOP
    global FULL_SWEEP_PAUSE_SECONDS

    for name, raw_value in settings.items():
        if name not in UI_SETTINGS_SCHEMA:
            continue

        parsed_value = parse_setting_value(name, raw_value)
        serialized_value = serialize_setting_value(name, parsed_value)
        os.environ[name] = serialized_value

        if name == "GITHUB_TOKEN":
            configure_github_token(parsed_value)
        elif name == "TARGET_MATCHES":
            TARGET_MATCHES = parsed_value
        elif name == "MIN_STARS":
            MIN_STARS = parsed_value
        elif name == "MIN_FILES_CHANGED":
            MIN_FILES_CHANGED = parsed_value
        elif name == "MAX_REPO_PAGES":
            MAX_REPO_PAGES = parsed_value
        elif name == "MAX_PULL_PAGES_PER_REPO":
            MAX_PULL_PAGES_PER_REPO = parsed_value
        elif name == "REPOS_PER_PAGE":
            REPOS_PER_PAGE = parsed_value
        elif name == "PULLS_PER_PAGE":
            PULLS_PER_PAGE = parsed_value
        elif name == "REQUEST_TIMEOUT":
            REQUEST_TIMEOUT = parsed_value
        elif name == "RUN_UNTIL_STOP":
            RUN_UNTIL_STOP = parsed_value
        elif name == "FULL_SWEEP_PAUSE_SECONDS":
            FULL_SWEEP_PAUSE_SECONDS = parsed_value

    return get_settings_payload()


def update_env_file(settings):
    if not settings:
        return

    lines = []
    if os.path.exists(DOTENV_FILE):
        with open(DOTENV_FILE, "r", encoding="utf-8") as handle:
            lines = handle.readlines()

    remaining = {key: serialize_setting_value(key, parse_setting_value(key, value)) for key, value in settings.items()}
    updated_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue

        key, _ = line.split("=", 1)
        env_key = key.strip()
        if env_key in remaining:
            updated_lines.append(f"{env_key}={remaining.pop(env_key)}\n")
        else:
            updated_lines.append(line)

    for key, value in remaining.items():
        updated_lines.append(f"{key}={value}\n")

    with open(DOTENV_FILE, "w", encoding="utf-8") as handle:
        handle.writelines(updated_lines)


def save_settings(settings):
    filtered_settings = {key: value for key, value in settings.items() if key in UI_SETTINGS_SCHEMA}
    update_env_file(filtered_settings)
    return apply_settings(filtered_settings)


def get_rate_limit_sleep_seconds(response):
    reset_at = response.headers.get("X-RateLimit-Reset")
    if not reset_at:
        return 60

    try:
        return max(int(reset_at) - int(time.time()) + 1, 1)
    except ValueError:
        return 60


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _classify_rate_limit_status(limit, remaining, reset_at):
    if limit is None or remaining is None:
        return "unknown"

    if remaining <= 0:
        return "exhausted"

    if remaining <= LOW_RATE_LIMIT_THRESHOLD:
        return "low"

    if limit > 0 and remaining / limit <= 0.1:
        return "warning"

    if reset_at is not None and reset_at <= int(time.time()) and remaining < limit:
        return "refreshing"

    return "healthy"


def update_rate_limit_state_from_response(response, source="response_headers", resource="core"):
    limit = _safe_int(response.headers.get("X-RateLimit-Limit"))
    remaining = _safe_int(response.headers.get("X-RateLimit-Remaining"))
    used = _safe_int(response.headers.get("X-RateLimit-Used"))
    reset_at = _safe_int(response.headers.get("X-RateLimit-Reset"))

    _LAST_RATE_LIMIT_STATE.update({
        "limit": limit,
        "remaining": remaining,
        "used": used,
        "reset_at": reset_at,
        "resource": resource,
        "status": _classify_rate_limit_status(limit, remaining, reset_at),
        "source": source,
        "fetched_at": int(time.time()),
    })


def get_rate_limit_payload():
    limit = _LAST_RATE_LIMIT_STATE.get("limit")
    remaining = _LAST_RATE_LIMIT_STATE.get("remaining")
    reset_at = _LAST_RATE_LIMIT_STATE.get("reset_at")
    used = _LAST_RATE_LIMIT_STATE.get("used")

    reset_in_seconds = None
    if reset_at is not None:
        reset_in_seconds = max(reset_at - int(time.time()), 0)

    usage_percent = None
    if limit and remaining is not None:
        usage_percent = round(((limit - remaining) / limit) * 100, 1)

    return {
        "limit": limit,
        "remaining": remaining,
        "used": used,
        "reset_at": reset_at,
        "reset_in_seconds": reset_in_seconds,
        "resource": _LAST_RATE_LIMIT_STATE.get("resource") or "core",
        "status": _LAST_RATE_LIMIT_STATE.get("status") or "unknown",
        "source": _LAST_RATE_LIMIT_STATE.get("source") or "unknown",
        "fetched_at": _LAST_RATE_LIMIT_STATE.get("fetched_at"),
        "usage_percent": usage_percent,
        "low_threshold": LOW_RATE_LIMIT_THRESHOLD,
        "has_token": bool(GITHUB_TOKEN),
    }


def fetch_rate_limit_status(force_refresh=False):
    fetched_at = _LAST_RATE_LIMIT_STATE.get("fetched_at")
    if not force_refresh and fetched_at is not None:
        if int(time.time()) - int(fetched_at) < RATE_LIMIT_REFRESH_TTL_SECONDS:
            return get_rate_limit_payload()

    url = "https://api.github.com/rate_limit"
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    resources = data.get("resources") or {}
    core = resources.get("core") or {}

    limit = _safe_int(core.get("limit"))
    remaining = _safe_int(core.get("remaining"))
    used = _safe_int(core.get("used"))
    reset_at = _safe_int(core.get("reset"))

    _LAST_RATE_LIMIT_STATE.update({
        "limit": limit,
        "remaining": remaining,
        "used": used,
        "reset_at": reset_at,
        "resource": "core",
        "status": _classify_rate_limit_status(limit, remaining, reset_at),
        "source": "rate_limit_endpoint",
        "fetched_at": int(time.time()),
    })

    return get_rate_limit_payload()


def build_retry_sleep_seconds(attempt_number):
    return max(RETRY_BACKOFF_SECONDS * attempt_number, 1)


def stop_requested(stop_callback=None):
    return bool(stop_callback and stop_callback())


def raise_if_stop_requested(stop_callback=None):
    if stop_requested(stop_callback):
        raise ScanStopped("Scan stopped by request")


def sleep_with_stop(total_seconds, stop_callback=None, step_seconds=1.0):
    remaining = max(float(total_seconds), 0.0)
    while remaining > 0:
        raise_if_stop_requested(stop_callback)
        sleep_seconds = min(step_seconds, remaining)
        time.sleep(sleep_seconds)
        remaining -= sleep_seconds


def github_get(url, params=None, extra_headers=None):
    headers = None
    if extra_headers:
        headers = dict(SESSION.headers)
        headers.update(extra_headers)

    for attempt in range(1, MAX_REQUEST_RETRIES + 1):
        try:
            response = SESSION.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            update_rate_limit_state_from_response(response)

            if response.status_code in {403, 429} and response.headers.get("X-RateLimit-Remaining") == "0":
                sleep_seconds = get_rate_limit_sleep_seconds(response)
                print(f"GitHub rate limit reached for {url}. Sleeping {sleep_seconds} seconds before retrying.")
                time.sleep(sleep_seconds)
                continue

            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            response = exc.response
            status_code = response.status_code if response is not None else None

            if status_code in {500, 502, 503, 504}:
                if attempt < MAX_REQUEST_RETRIES:
                    sleep_seconds = build_retry_sleep_seconds(attempt)
                    print(
                        f"Transient GitHub error {status_code} for {url}. "
                        f"Retrying in {sleep_seconds} seconds (attempt {attempt}/{MAX_REQUEST_RETRIES})."
                    )
                    time.sleep(sleep_seconds)
                    continue

                raise RecoverableGitHubError(
                    f"GitHub returned HTTP {status_code} for {url} after {MAX_REQUEST_RETRIES} attempts"
                ) from exc

            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < MAX_REQUEST_RETRIES:
                sleep_seconds = build_retry_sleep_seconds(attempt)
                print(
                    f"Transient network error for {url}: {exc}. "
                    f"Retrying in {sleep_seconds} seconds (attempt {attempt}/{MAX_REQUEST_RETRIES})."
                )
                time.sleep(sleep_seconds)
                continue

            raise RecoverableGitHubError(
                f"GitHub request failed for {url} after {MAX_REQUEST_RETRIES} attempts"
            ) from exc

    raise RecoverableGitHubError(f"GitHub request failed for {url} after exhausting retries")


def is_rate_limited(exc):
    response = exc.response
    if response is None:
        return False

    if response.status_code not in {403, 429}:
        return False

    remaining = response.headers.get("X-RateLimit-Remaining")
    return remaining == "0" or response.status_code == 429


def build_repo_search_query(star_ceiling=None):
    parts = ["language:python", f"stars:>={MIN_STARS}"]
    if star_ceiling is not None:
        parts.append(f"stars:<={star_ceiling}")
    return " ".join(parts)


def search_python_repos(page, star_ceiling=None):
    url = "https://api.github.com/search/repositories"
    params = {
        "q": build_repo_search_query(star_ceiling),
        "sort": "stars",
        "order": "desc",
        "per_page": REPOS_PER_PAGE,
        "page": page,
    }
    data = github_get(url, params=params)
    return data.get("items", [])


def get_repo_details(repo):
    url = f"https://api.github.com/repos/{repo}"
    return github_get(url)


def normalize_repo_name(repo_value):
    candidate = (repo_value or "").strip()
    if not candidate:
        raise ValueError("Repository value cannot be empty")

    if candidate.startswith("http://") or candidate.startswith("https://"):
        parsed = urlparse(candidate)
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        candidate = path

    parts = [part for part in candidate.split("/") if part]
    if len(parts) != 2:
        raise ValueError(f"Repository must be in 'owner/name' format or a GitHub URL: {repo_value}")

    return f"{parts[0]}/{parts[1]}"


def parse_target_repos(repo_values):
    normalized_repos = []
    seen = set()

    for repo_value in repo_values or []:
        normalized_repo = normalize_repo_name(repo_value)
        repo_key = normalized_repo.lower()
        if repo_key in seen:
            continue

        seen.add(repo_key)
        normalized_repos.append(normalized_repo)

    return normalized_repos


def get_closed_pulls(repo, page):
    url = f"https://api.github.com/repos/{repo}/pulls"
    params = {
        "state": "closed",
        "sort": "updated",
        "direction": "desc",
        "per_page": PULLS_PER_PAGE,
        "page": page,
    }
    return github_get(url, params=params)


def strip_markup_for_language(text):
    cleaned = text or ""
    cleaned = CODE_FENCE_PATTERN.sub(" ", cleaned)
    cleaned = INLINE_CODE_PATTERN.sub(" ", cleaned)
    cleaned = MARKDOWN_LINK_PATTERN.sub(r"\1", cleaned)
    cleaned = URL_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"[#>*_~\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def detect_is_english(text):
    cleaned = strip_markup_for_language(text)
    words = LANGUAGE_WORD_PATTERN.findall(cleaned)
    candidate = " ".join(words)

    if len(candidate) < LANGUAGE_MIN_TEXT_LENGTH:
        return None

    try:
        return detect(candidate) == "en"
    except LangDetectException:
        return None


def repo_looks_english(repo_name, repo_data):
    if not ENGLISH_ONLY:
        return True

    description = (repo_data or {}).get("description") or ""
    homepage = (repo_data or {}).get("homepage") or ""
    topics = " ".join((repo_data or {}).get("topics") or [])
    repo_text = " ".join(part for part in [repo_name.replace("/", " "), description, homepage, topics] if part).strip()
    detected = detect_is_english(repo_text)

    if detected is True:
        return True

    if detected is False:
        return False

    ascii_only_name = all(ord(char) < 128 for char in repo_name)
    return ascii_only_name and not description


def issue_looks_english(issue):
    if not ENGLISH_ONLY:
        return True

    text = " ".join(filter(None, [issue.get("title", ""), issue.get("body") or ""])).strip()
    detected = detect_is_english(text)
    return detected is True


def is_complex_issue(issue):
    text = (issue.get("title", "") + " " + (issue.get("body") or "")).lower()

    for word in TRIVIAL_KEYWORDS:
        if word in text:
            return False

    if len(text) < 100:
        return False

    if not issue_looks_english(issue):
        return False

    return True


def get_pr_details(repo, pr_number):
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    return github_get(url)


def get_issue(repo, issue_number):
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    return github_get(url)


def get_commit(repo, commit_sha):
    url = f"https://api.github.com/repos/{repo}/commits/{commit_sha}"
    return github_get(url)


def extract_issue_refs(repo_name, text):
    refs = []
    seen_numbers = set()

    for raw_ref in ISSUE_REF_PATTERN.findall(text or ""):
        repo_part, issue_number = raw_ref.split("#", 1)
        normalized_repo = repo_name if not repo_part else repo_part
        if normalized_repo.lower() != repo_name.lower():
            continue

        number = int(issue_number)
        if number in seen_numbers:
            continue

        seen_numbers.add(number)
        refs.append(number)

    return refs


def derive_checkout_state(repo_name, pr_details):
    base = pr_details.get("base") or {}
    base_ref = base.get("ref")
    base_sha = base.get("sha")
    merge_commit_sha = pr_details.get("merge_commit_sha")

    checkout_sha = base_sha
    checkout_sha_source = "pr_base_sha"

    if merge_commit_sha:
        try:
            merge_commit = get_commit(repo_name, merge_commit_sha)
            parents = merge_commit.get("parents") or []
            if parents:
                checkout_sha = parents[0].get("sha") or base_sha
                checkout_sha_source = "merge_commit_first_parent"
        except RecoverableGitHubError as exc:
            print(f"    Could not derive pre-fix checkout SHA for PR #{pr_details.get('number')}: {exc}")
        except requests.HTTPError as exc:
            print(f"    Could not derive pre-fix checkout SHA for PR #{pr_details.get('number')}: {exc}")

    return {
        "base_ref": base_ref,
        "base_sha": base_sha,
        "merge_commit_sha": merge_commit_sha,
        "checkout_sha": checkout_sha,
        "checkout_sha_source": checkout_sha_source,
    }


def build_match(repo_name, issue, pr_details):
    checkout_state = derive_checkout_state(repo_name, pr_details)

    return {
        "repo": repo_name,
        "issue_number": issue["number"],
        "issue_title": issue.get("title"),
        "issue_url": issue.get("html_url"),
        "pr_number": pr_details.get("number"),
        "pr_url": pr_details.get("html_url"),
        "files_changed": pr_details.get("changed_files", 0),
        "issue_comments": issue.get("comments", 0),
        "issue_closed_at": issue.get("closed_at"),
        "pr_merged_at": pr_details.get("merged_at"),
        "base_ref": checkout_state.get("base_ref"),
        "base_sha": checkout_state.get("base_sha"),
        "merge_commit_sha": checkout_state.get("merge_commit_sha"),
        "checkout_sha": checkout_state.get("checkout_sha"),
        "checkout_sha_source": checkout_state.get("checkout_sha_source"),
    }


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json_file(path, default_value):
    if not os.path.exists(path):
        return default_value

    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError:
            return default_value


def load_matches():
    data = load_json_file(OUTPUT_FILE, [])
    return data if isinstance(data, list) else []


def save_matches(matches):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as handle:
        json.dump(matches, handle, indent=2)


def load_scan_state():
    default_state = {
        "scanned_repos": [],
        "last_completed_repo": None,
        "current_star_ceiling": None,
        "current_repo_search_page": 1,
        "active_repo": None,
        "active_pull_page": 1,
        "completed_sweeps": 0,
        "updated_at": None,
    }
    data = load_json_file(STATE_FILE, default_state)
    if not isinstance(data, dict):
        return default_state

    scanned_repos = data.get("scanned_repos", [])
    if not isinstance(scanned_repos, list):
        scanned_repos = []

    return {
        "scanned_repos": scanned_repos,
        "last_completed_repo": data.get("last_completed_repo"),
        "current_star_ceiling": data.get("current_star_ceiling"),
        "current_repo_search_page": data.get("current_repo_search_page", 1),
        "active_repo": data.get("active_repo"),
        "active_pull_page": data.get("active_pull_page", 1),
        "completed_sweeps": data.get("completed_sweeps", 0),
        "updated_at": data.get("updated_at"),
    }


def save_scan_state(state):
    payload = {
        "scanned_repos": sorted(set(state.get("scanned_repos", []))),
        "last_completed_repo": state.get("last_completed_repo"),
        "current_star_ceiling": state.get("current_star_ceiling"),
        "current_repo_search_page": state.get("current_repo_search_page", 1),
        "active_repo": state.get("active_repo"),
        "active_pull_page": state.get("active_pull_page", 1),
        "completed_sweeps": state.get("completed_sweeps", 0),
        "updated_at": utc_now_iso(),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def update_search_progress(state, repo_search_page):
    state["current_repo_search_page"] = max(int(repo_search_page or 1), 1)
    save_scan_state(state)


def checkpoint_repo_progress(state, repo_name, pull_page):
    state["active_repo"] = repo_name
    state["active_pull_page"] = max(int(pull_page or 1), 1)
    save_scan_state(state)


def clear_repo_progress(state):
    state["active_repo"] = None
    state["active_pull_page"] = 1
    save_scan_state(state)


def mark_repo_scanned(state, repo_name):
    scanned_repos = set(state.get("scanned_repos", []))
    scanned_repos.add(repo_name)
    state["scanned_repos"] = sorted(scanned_repos)
    state["last_completed_repo"] = repo_name
    state["active_repo"] = None
    state["active_pull_page"] = 1
    save_scan_state(state)


def advance_repo_bucket(state, next_star_ceiling):
    state["current_star_ceiling"] = next_star_ceiling
    state["current_repo_search_page"] = 1
    state["active_repo"] = None
    state["active_pull_page"] = 1
    save_scan_state(state)


def reset_for_next_sweep(state):
    state["completed_sweeps"] = state.get("completed_sweeps", 0) + 1
    state["current_star_ceiling"] = None
    state["current_repo_search_page"] = 1
    state["active_repo"] = None
    state["active_pull_page"] = 1
    save_scan_state(state)


def build_match_key(match):
    return (match.get("repo"), match.get("issue_number"), match.get("pr_number"))


def persist_match(matches, seen_match_keys, match):
    match_key = build_match_key(match)
    if match_key in seen_match_keys:
        return False, match

    matches.append(match)
    seen_match_keys.add(match_key)
    save_matches(matches)
    return True, match


def enrich_matches_with_checkout_state(matches):
    changed = False

    for match in matches:
        if match.get("base_sha") and match.get("checkout_sha"):
            continue

        repo_name = match.get("repo")
        pr_number = match.get("pr_number")
        if not repo_name or not pr_number:
            continue

        try:
            pr_details = get_pr_details(repo_name, pr_number)
        except RecoverableGitHubError as exc:
            print(f"Skipping checkout SHA backfill for {repo_name} PR #{pr_number}: {exc}")
            continue
        except requests.HTTPError as exc:
            print(f"Skipping checkout SHA backfill for {repo_name} PR #{pr_number}: {exc}")
            continue

        checkout_state = derive_checkout_state(repo_name, pr_details)
        for key, value in checkout_state.items():
            if match.get(key) != value:
                match[key] = value
                changed = True

    if changed:
        save_matches(matches)
        print(f"Backfilled checkout SHA data in {OUTPUT_FILE}")

    return matches


def print_match_summary(match, prefix="  "):
    print(f"{prefix}Issue: {match['issue_url']}")
    print(f"{prefix}PR: {match['pr_url']}")
    print(f"{prefix}Files changed: {match['files_changed']}")
    print(f"{prefix}Checkout SHA: {match.get('checkout_sha')}")


def scan_repo(
    repo_name,
    matches,
    seen_issue_urls,
    seen_match_keys,
    target_matches=None,
    stop_callback=None,
    repo_data=None,
    state=None,
    start_pull_page=1,
):
    raise_if_stop_requested(stop_callback)
    print(f"\nChecking repo: {repo_name}")
    repo_failed = False

    try:
        repo_metadata = repo_data or get_repo_details(repo_name)
    except RecoverableGitHubError as exc:
        print(f"  Skipping repo metadata check for {repo_name} after repeated transient errors: {exc}")
        return True, False
    except requests.HTTPError as exc:
        print(f"  Skipping repo metadata check for {repo_name} due to API error: {exc}")
        return True, False

    if not repo_looks_english(repo_name, repo_metadata):
        print(f"  Skipping {repo_name} because the repository metadata does not look English.")
        return False, False

    pull_page_start = max(int(start_pull_page or 1), 1)

    for pull_page in range(pull_page_start, MAX_PULL_PAGES_PER_REPO + 1):
        raise_if_stop_requested(stop_callback)
        if state is not None:
            checkpoint_repo_progress(state, repo_name, pull_page)
        try:
            pulls = get_closed_pulls(repo_name, pull_page)
        except RecoverableGitHubError as exc:
            print(
                f"  Skipping remaining pull pages for {repo_name} after repeated transient errors "
                f"on pull page {pull_page}: {exc}"
            )
            repo_failed = True
            break
        except requests.HTTPError as exc:
            print(f"  Skipping remaining pull pages for {repo_name} due to API error: {exc}")
            repo_failed = True
            break

        if not pulls:
            break

        print(f"  Pull page {pull_page}: {len(pulls)} candidates")

        for pull in pulls:
            raise_if_stop_requested(stop_callback)
            if not pull.get("merged_at"):
                continue

            issue_numbers = extract_issue_refs(repo_name, pull.get("body") or "")
            if not issue_numbers:
                continue

            try:
                pr_details = get_pr_details(repo_name, pull["number"])
            except RecoverableGitHubError as exc:
                print(f"    Skipping PR #{pull['number']} after repeated transient errors: {exc}")
                continue
            except requests.HTTPError as exc:
                print(f"    Skipping PR #{pull['number']} due to API error: {exc}")
                continue

            if not pr_details.get("merged"):
                continue

            if pr_details.get("changed_files", 0) < MIN_FILES_CHANGED:
                continue

            for issue_number in issue_numbers:
                raise_if_stop_requested(stop_callback)
                try:
                    issue = get_issue(repo_name, issue_number)
                except RecoverableGitHubError as exc:
                    print(f"    Skipping issue #{issue_number} after repeated transient errors: {exc}")
                    continue
                except requests.HTTPError as exc:
                    print(f"    Skipping issue #{issue_number} due to API error: {exc}")
                    continue

                if "pull_request" in issue:
                    continue

                issue_url = issue.get("html_url")
                if not issue_url or issue_url in seen_issue_urls:
                    continue

                if not is_complex_issue(issue):
                    continue

                match = build_match(repo_name, issue, pr_details)
                was_added, stored_match = persist_match(matches, seen_match_keys, match)
                if not was_added:
                    continue

                seen_issue_urls.add(issue_url)

                progress_target = target_matches if target_matches is not None else "continuous"
                print(f"  FOUND {len(matches)}/{progress_target}: {issue['title']}")
                print_match_summary(stored_match, prefix="    ")

                if target_matches is not None and len(matches) >= target_matches:
                    return repo_failed, True

        sleep_with_stop(0.2, stop_callback=stop_callback, step_seconds=0.2)

    if state is not None and not repo_failed:
        clear_repo_progress(state)

    return repo_failed, False


def get_repo_matches(matches, repo_name):
    repo_key = repo_name.lower()
    return [match for match in matches if (match.get("repo") or "").lower() == repo_key]


def collect_matches_for_repos(repo_names, stop_callback=None):
    matches = load_matches()
    matches = enrich_matches_with_checkout_state(matches)
    seen_issue_urls = {match.get("issue_url") for match in matches if match.get("issue_url")}
    seen_match_keys = {build_match_key(match) for match in matches}

    for repo_name in repo_names:
        raise_if_stop_requested(stop_callback)
        repo_failed, _ = scan_repo(
            repo_name,
            matches,
            seen_issue_urls,
            seen_match_keys,
            target_matches=None,
            stop_callback=stop_callback,
            repo_data=None,
        )
        repo_matches = get_repo_matches(matches, repo_name)

        print(f"\nQualified issues for {repo_name}: {len(repo_matches)}")
        for match in repo_matches:
            print(f"- {match['issue_title']}")
            print_match_summary(match, prefix="  ")

        if repo_failed:
            print(f"\n{repo_name} had transient API errors and may need another pass for complete results.")

    return matches


def parse_args():
    parser = argparse.ArgumentParser(description="Find qualified GitHub issues linked to merged pull requests.")
    parser.add_argument(
        "--repo",
        dest="repos",
        action="append",
        default=[],
        help="Scan only the specified GitHub repo. Accepts owner/name or a full GitHub URL. Repeat to scan multiple repos.",
    )
    return parser.parse_args()


def collect_matches(target_matches=None, run_until_stop=False, stop_callback=None):
    matches = load_matches()
    matches = enrich_matches_with_checkout_state(matches)
    state = load_scan_state()
    save_scan_state(state)
    seen_issue_urls = {match.get("issue_url") for match in matches if match.get("issue_url")}
    seen_match_keys = {build_match_key(match) for match in matches}

    if matches:
        print(f"Loaded {len(matches)} existing matches from {OUTPUT_FILE}")

    if state.get("scanned_repos"):
        print(f"Loaded {len(state['scanned_repos'])} previously scanned repos from {STATE_FILE}")

    if target_matches is not None and len(matches) >= target_matches:
        return matches[:target_matches]

    while True:
        raise_if_stop_requested(stop_callback)
        scanned_repos = set(state.get("scanned_repos", []))
        star_ceiling = state.get("current_star_ceiling")
        repo_page_start = max(int(state.get("current_repo_search_page") or 1), 1)
        active_repo_name = state.get("active_repo")
        active_pull_page = max(int(state.get("active_pull_page") or 1), 1)
        lowest_star_seen = None
        saw_any_repos = False

        if star_ceiling is None:
            print("\nScanning highest-star Python repositories")
        else:
            print(f"\nScanning Python repositories with stars <= {star_ceiling}")

        for repo_page in range(repo_page_start, MAX_REPO_PAGES + 1):
            raise_if_stop_requested(stop_callback)
            update_search_progress(state, repo_page)
            try:
                repos = search_python_repos(repo_page, star_ceiling=star_ceiling)
            except RecoverableGitHubError as exc:
                print(f"\nSkipping repository search page {repo_page} after repeated transient GitHub failures: {exc}")
                sleep_with_stop(RETRY_BACKOFF_SECONDS, stop_callback=stop_callback)
                continue

            if not repos:
                break

            saw_any_repos = True
            print(f"\nScanning repository search page {repo_page} with {len(repos)} repos")

            for repo in repos:
                repo_name = repo["full_name"]
                repo_stars = repo.get("stargazers_count")
                if isinstance(repo_stars, int):
                    if lowest_star_seen is None or repo_stars < lowest_star_seen:
                        lowest_star_seen = repo_stars

                if repo_name in scanned_repos:
                    print(f"\nSkipping repo already scanned: {repo_name}")
                    continue

                start_pull_page = active_pull_page if active_repo_name and repo_name == active_repo_name else 1

                repo_failed, reached_target = scan_repo(
                    repo_name,
                    matches,
                    seen_issue_urls,
                    seen_match_keys,
                    target_matches=target_matches,
                    stop_callback=stop_callback,
                    repo_data=repo,
                    state=state,
                    start_pull_page=start_pull_page,
                )

                active_repo_name = None
                active_pull_page = 1

                if reached_target:
                    return matches

                if repo_failed:
                    print(f"  Leaving {repo_name} unmarked so it can be retried on a future pass.")
                else:
                    mark_repo_scanned(state, repo_name)
                    scanned_repos.add(repo_name)

                sleep_with_stop(0.5, stop_callback=stop_callback, step_seconds=0.5)

        state["current_repo_search_page"] = 1
        save_scan_state(state)

        if not run_until_stop:
            return matches

        if saw_any_repos and lowest_star_seen is not None and lowest_star_seen > MIN_STARS:
            next_star_ceiling = lowest_star_seen - 1
            print(f"\nCompleted current star bucket. Advancing to stars <= {next_star_ceiling}.")
            advance_repo_bucket(state, next_star_ceiling)
            continue

        reset_for_next_sweep(state)
        print(
            f"\nCompleted full sweep #{state['completed_sweeps']}. "
            f"Sleeping {FULL_SWEEP_PAUSE_SECONDS} seconds before restarting from the highest-star repos."
        )
        sleep_with_stop(FULL_SWEEP_PAUSE_SECONDS, stop_callback=stop_callback)


def main():
    args = parse_args()
    target_repos = parse_target_repos(args.repos)

    if not GITHUB_TOKEN:
        print("Warning: GITHUB_TOKEN is not set. Large searches may hit stricter GitHub API limits.")

    if target_repos:
        print(f"Repo-targeted scan mode enabled for: {', '.join(target_repos)}")
    elif RUN_UNTIL_STOP:
        print("Continuous scan mode enabled. The collector will keep running until you stop the process.")

    try:
        if target_repos:
            matches = collect_matches_for_repos(target_repos)
        else:
            target_matches = None if RUN_UNTIL_STOP else TARGET_MATCHES
            matches = collect_matches(target_matches=target_matches, run_until_stop=RUN_UNTIL_STOP)
    except requests.HTTPError as exc:
        if is_rate_limited(exc):
            print("GitHub API rate limit reached before the current scan completed.")
            return
        raise

    save_matches(matches)
    print(f"\nSaved {len(matches)} matches to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
