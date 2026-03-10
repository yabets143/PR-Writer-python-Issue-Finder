import asyncio
import contextlib
import io
import json
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import github_issue_pr_finder as finder

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"
ACTIVE_SCAN_STATES = {"queued", "running", "stopping"}


class RepoScanRequest(BaseModel):
    repo: str


class SettingsUpdateRequest(BaseModel):
    settings: dict[str, Any]


class ScanStreamWriter(io.TextIOBase):
    def __init__(self, log_callback):
        self._buffer = ""
        self._log_callback = log_callback

    def write(self, data):
        if not data:
            return 0

        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                self._log_callback(line)
        return len(data)

    def flush(self):
        if self._buffer.strip():
            self._log_callback(self._buffer.strip())
        self._buffer = ""


class ScanSession:
    def __init__(self, repo: str, mode: str):
        self.scan_id = uuid.uuid4().hex
        self.repo = repo
        self.mode = mode
        self.status = "queued"
        self.error = None
        self.result_matches = []
        self.match_count = 0
        self.new_match_count = 0
        self.started_at = utc_now_iso()
        self.finished_at = None
        self._stop_event = threading.Event()
        self._events = []
        self._lock = threading.Lock()
        self.append_event("status", self._status_payload())

    def _status_payload(self, **extra):
        return {
            "status": self.status,
            "repo": self.repo,
            "mode": self.mode,
            "can_stop": self.can_stop,
            "stop_requested": self.stop_requested,
            **extra,
        }

    def append_event(self, event_type: str, payload: dict[str, Any]):
        with self._lock:
            event = {
                "index": len(self._events),
                "event": event_type,
                "timestamp": utc_now_iso(),
                "payload": payload,
            }
            self._events.append(event)
            return event

    def log(self, message: str):
        self.append_event("log", {"message": message})

    def set_status(self, status: str, **extra):
        self.status = status
        if status in {"completed", "error", "stopped"}:
            self.finished_at = utc_now_iso()
        self.append_event("status", self._status_payload(**extra))

    def set_result(self, matches, new_match_count: int):
        self.result_matches = matches
        self.match_count = len(matches)
        self.new_match_count = new_match_count

    def set_counts(self, match_count: int, new_match_count: int):
        self.match_count = match_count
        self.new_match_count = new_match_count

    def set_error(self, message: str):
        self.error = message

    @property
    def stop_requested(self):
        return self._stop_event.is_set()

    @property
    def can_stop(self):
        return self.status in ACTIVE_SCAN_STATES and not self.stop_requested

    def request_stop(self):
        self._stop_event.set()
        if self.status in ACTIVE_SCAN_STATES:
            self.set_status("stopping")

    def should_stop(self):
        return self._stop_event.is_set()

    def events_since(self, index: int):
        with self._lock:
            return self._events[index:]

    def event_count(self):
        with self._lock:
            return len(self._events)

    def summary(self):
        return {
            "scan_id": self.scan_id,
            "repo": self.repo,
            "mode": self.mode,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "can_stop": self.can_stop,
            "stop_requested": self.stop_requested,
            "match_count": self.match_count,
            "new_match_count": self.new_match_count,
            "matches": self.result_matches,
        }


class ScanManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}
        self._active_scan_id = None
        self._latest_scan_id = None

    def get_active_session(self):
        with self._lock:
            if not self._active_scan_id:
                return None
            return self._sessions.get(self._active_scan_id)

    def get_latest_session(self):
        with self._lock:
            if not self._latest_scan_id:
                return None
            return self._sessions.get(self._latest_scan_id)

    def get_session(self, scan_id: str):
        with self._lock:
            return self._sessions.get(scan_id)

    def _get_active_session_locked(self):
        if not self._active_scan_id:
            return None
        return self._sessions.get(self._active_scan_id)

    def _register_session(self, session: ScanSession):
        self._sessions[session.scan_id] = session
        self._active_scan_id = session.scan_id
        self._latest_scan_id = session.scan_id

    def _ensure_no_conflicting_scan(self, session_key: str):
        active = self._get_active_session_locked()
        if not active or active.status not in ACTIVE_SCAN_STATES:
            return None

        if active.repo.lower() == session_key.lower():
            return active

        raise HTTPException(
            status_code=409,
            detail={
                "message": "Another scan is already running.",
                "active_scan": active.summary(),
            },
        )

    def start_repo_scan(self, repo: str):
        normalized_repo = finder.normalize_repo_name(repo)

        with self._lock:
            active = self._ensure_no_conflicting_scan(normalized_repo)
            if active:
                return active, False

            session = ScanSession(normalized_repo, mode="repo")
            self._register_session(session)

        thread = threading.Thread(target=self._run_repo_scan, args=(session,), daemon=True)
        thread.start()
        return session, True

    def start_live_scan(self):
        live_label = "All popular Python repos"

        with self._lock:
            active = self._ensure_no_conflicting_scan(live_label)
            if active:
                return active, False

            session = ScanSession(live_label, mode="continuous")
            self._register_session(session)

        thread = threading.Thread(target=self._run_live_scan, args=(session,), daemon=True)
        thread.start()
        return session, True

    def stop_live_scan(self):
        with self._lock:
            active = self._get_active_session_locked()
            if not active or active.mode != "continuous" or active.status not in ACTIVE_SCAN_STATES:
                raise HTTPException(status_code=409, detail={"message": "No nonstop live scan is currently running."})

            active.request_stop()
            return active

    def _run_repo_scan(self, session: ScanSession):
        session.set_status("running")
        session.log(f"Starting targeted scan for {session.repo}")

        before_matches = finder.get_repo_matches(finder.load_matches(), session.repo)
        before_keys = {finder.build_match_key(match) for match in before_matches}

        writer = ScanStreamWriter(session.log)
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                matches = finder.collect_matches_for_repos([session.repo], stop_callback=session.should_stop)
            repo_matches = finder.get_repo_matches(matches, session.repo)
            after_keys = {finder.build_match_key(match) for match in repo_matches}
            new_match_count = len(after_keys - before_keys)
            session.set_result(repo_matches, new_match_count)
            session.log(
                f"Finished scan for {session.repo}. Found {len(repo_matches)} qualified issues, {new_match_count} new in this run."
            )
            session.set_status("completed", match_count=len(repo_matches), new_match_count=new_match_count)
        except finder.ScanStopped:
            repo_matches = finder.get_repo_matches(finder.load_matches(), session.repo)
            after_keys = {finder.build_match_key(match) for match in repo_matches}
            new_match_count = len(after_keys - before_keys)
            session.set_result(repo_matches, new_match_count)
            session.log(f"Scan for {session.repo} stopped by request.")
            session.set_status("stopped", match_count=len(repo_matches), new_match_count=new_match_count)
        except Exception as exc:
            writer.flush()
            session.set_error(str(exc))
            session.log(traceback.format_exc().rstrip())
            session.set_status("error", error=str(exc))
        finally:
            writer.flush()
            with self._lock:
                if self._active_scan_id == session.scan_id:
                    self._active_scan_id = None

    def _run_live_scan(self, session: ScanSession):
        session.set_status("running")
        session.log("Starting nonstop discovery across popular Python repositories")

        before_count = len(finder.load_matches())
        writer = ScanStreamWriter(session.log)
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                matches = finder.collect_matches(target_matches=None, run_until_stop=True, stop_callback=session.should_stop)
            total_count = len(matches)
            new_match_count = max(total_count - before_count, 0)
            session.set_counts(total_count, new_match_count)
            session.log(f"Nonstop live scan finished with {total_count} total matches and {new_match_count} new matches.")
            session.set_status("completed", match_count=total_count, new_match_count=new_match_count)
        except finder.ScanStopped:
            matches = finder.load_matches()
            total_count = len(matches)
            new_match_count = max(total_count - before_count, 0)
            session.set_counts(total_count, new_match_count)
            session.log("Nonstop live scan stopped by request.")
            session.set_status("stopped", match_count=total_count, new_match_count=new_match_count)
        except Exception as exc:
            writer.flush()
            session.set_error(str(exc))
            session.log(traceback.format_exc().rstrip())
            session.set_status("error", error=str(exc))
        finally:
            writer.flush()
            with self._lock:
                if self._active_scan_id == session.scan_id:
                    self._active_scan_id = None


app = FastAPI(title="PR Match Radar")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
manager = ScanManager()


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/matches")
def get_matches(repo: str | None = None):
    matches = finder.load_matches()
    matches = finder.enrich_matches_with_checkout_state(matches)

    if repo:
        normalized_repo = finder.normalize_repo_name(repo)
        matches = finder.get_repo_matches(matches, normalized_repo)

    matches = sorted(
        matches,
        key=lambda item: item.get("pr_merged_at") or item.get("issue_closed_at") or "",
        reverse=True,
    )
    return JSONResponse({"count": len(matches), "matches": matches})


def build_settings_response():
    return {
        "settings": finder.get_settings_payload(),
        "schema": finder.UI_SETTINGS_SCHEMA,
    }


@app.get("/api/settings")
def get_settings():
    return JSONResponse(build_settings_response())


@app.patch("/api/settings")
def update_settings(request: SettingsUpdateRequest):
    try:
        settings = finder.save_settings(request.settings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return JSONResponse({
        "settings": settings,
        "schema": finder.UI_SETTINGS_SCHEMA,
        "saved": True,
    })


@app.get("/api/scan-status")
def get_scan_status():
    session = manager.get_active_session() or manager.get_latest_session()
    if not session:
        return JSONResponse({"scan": None})
    return JSONResponse({"scan": session.summary()})


@app.post("/api/scan-repo")
def scan_repo(request: RepoScanRequest):
    session, started = manager.start_repo_scan(request.repo)
    return JSONResponse({
        "started": started,
        "scan": session.summary(),
    })


@app.post("/api/scan-live/start")
def start_live_scan():
    session, started = manager.start_live_scan()
    return JSONResponse({
        "started": started,
        "scan": session.summary(),
    })


@app.post("/api/scan-live/stop")
def stop_live_scan():
    session = manager.stop_live_scan()
    return JSONResponse({
        "stopped": True,
        "scan": session.summary(),
    })


@app.get("/api/scan-events")
async def scan_events(scan_id: str | None = None):
    session = None
    if scan_id:
        session = manager.get_session(scan_id)
    else:
        session = manager.get_active_session() or manager.get_latest_session()

    if not session:
        raise HTTPException(status_code=404, detail="No scan session found")

    async def event_generator():
        cursor = 0
        while True:
            events = session.events_since(cursor)
            if events:
                for event in events:
                    yield to_sse(event["event"], event["payload"])
                cursor += len(events)
            else:
                yield ": keep-alive\n\n"

            if session.status not in ACTIVE_SCAN_STATES and cursor >= session.event_count():
                yield to_sse("summary", session.summary())
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_sse(event_type: str, payload: dict[str, Any]):
    data = json.dumps(payload)
    return f"event: {event_type}\ndata: {data}\n\n"
