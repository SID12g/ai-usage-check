#!/usr/bin/env python3
"""Claude / ChatGPT 남은 사용량 대시보드 (로컬 전용, 표준 라이브러리만 사용).

    python3 usage_server.py            # http://127.0.0.1:8787 을 브라우저로 엽니다
    python3 usage_server.py --no-open  # 서버만 실행

- Claude: Claude Code 로그인 토큰(macOS 키체인)으로 Anthropic 사용량 API를 조회합니다.
  토큰은 api.anthropic.com 으로만 전송되며 페이지나 로그에는 남지 않습니다.
- ChatGPT: `codex app-server`에 사용량을 물어봅니다. 인증은 Codex CLI가 직접 처리합니다.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8787"))
INDEX = Path(__file__).resolve().parent / "index.html"

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# 응답 키 -> (표시 이름, 정렬 순서). 모르는 키도 utilization 값이 있으면 그대로 보여줍니다.
CLAUDE_WINDOWS = {
    "five_hour": ("5시간", 0),
    "seven_day": ("주간 · 전체 모델", 1),
    "seven_day_opus": ("주간 · Opus", 2),
    "seven_day_sonnet": ("주간 · Sonnet", 3),
    "seven_day_oauth_apps": ("주간 · 연동 앱", 4),
    "extra_usage": ("추가 사용량 · 월간", 5),
}

CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"


class SourceError(Exception):
    """사용자에게 그대로 보여줄 수 있는 조회 실패."""


# ---------- Claude ----------

def _claude_credentials():
    candidates = []
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        candidates.append(json.loads(out)["claudeAiOauth"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass
    try:
        path = Path.home() / ".claude" / ".credentials.json"
        candidates.append(json.loads(path.read_text())["claudeAiOauth"])
    except (OSError, ValueError, KeyError):
        pass
    if not candidates:
        raise SourceError("Claude Code 로그인 정보를 찾지 못했습니다. 터미널에서 `claude`를 실행해 로그인하세요.")
    return max(candidates, key=lambda c: c.get("expiresAt") or 0)


def _claude_plan(creds):
    plan = (creds.get("subscriptionType") or "").capitalize()
    multiplier = re.search(r"(\d+x)$", creds.get("rateLimitTier") or "")
    if plan and multiplier:
        return f"{plan} {multiplier.group(1)}"
    return plan or None


def _epoch(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fetch_claude():
    creds = _claude_credentials()
    if (creds.get("expiresAt") or 0) / 1000 < time.time():
        # 여기서 직접 갱신하면 Claude Code의 refresh token이 바뀌어 로그인이 풀릴 수 있어 갱신은 Claude Code에 맡깁니다.
        raise SourceError("Claude Code 토큰이 만료되었습니다. 터미널에서 `claude`를 한 번 실행한 뒤 새로고침하세요.")

    req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {creds['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "usage-dashboard/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        hint = " 잠시 후 다시 시도하세요." if e.code == 429 else ""
        raise SourceError(f"Anthropic 사용량 API가 {e.code} 응답을 반환했습니다.{hint}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise SourceError(f"Anthropic 사용량 API에 연결하지 못했습니다: {getattr(e, 'reason', e)}") from None

    windows = []
    for key, value in data.items():
        if not isinstance(value, dict) or not isinstance(value.get("utilization"), (int, float)):
            continue
        label, order = CLAUDE_WINDOWS.get(key, (key.replace("_", " "), 99))
        windows.append({
            "id": key,
            "label": label,
            "order": order,
            "usedPercent": value["utilization"],
            "resetsAt": _epoch(value.get("resets_at")),
        })
    if not windows:
        raise SourceError("사용량 응답 형식을 인식하지 못했습니다 (키: " + ", ".join(data) + ").")
    windows.sort(key=lambda w: w["order"])
    return {"plan": _claude_plan(creds), "windows": windows}


# ---------- ChatGPT (Codex) ----------

def _codex_rpc(calls, timeout=20):
    """calls = {id: (method, params)} 를 codex app-server 에 보내고 {id: result} 를 돌려줍니다."""
    try:
        proc = subprocess.Popen(
            [CODEX_BIN, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        raise SourceError("codex CLI를 찾지 못했습니다. Codex를 설치하고 `codex login`으로 로그인하세요.") from None

    lines = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    messages = [
        {"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "usage_dashboard", "version": "1.0"}}},
        {"method": "initialized"},
    ]
    for call_id, (method, params) in calls.items():
        msg = {"id": call_id, "method": method}
        if params is not None:
            msg["params"] = params
        messages.append(msg)

    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages))
        proc.stdin.flush()
        results, deadline = {}, time.monotonic() + timeout
        while len(results) < len(calls):
            try:
                line = lines.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise SourceError("codex app-server 응답이 너무 늦습니다.") from None
            if line is None:
                raise SourceError("codex app-server가 예기치 않게 종료되었습니다.")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") not in calls:
                continue
            if "error" in msg:
                error = msg["error"]
                raise SourceError("Codex: " + str(error.get("message", error) if isinstance(error, dict) else error))
            results[msg["id"]] = msg.get("result") or {}
        return results
    except OSError:
        raise SourceError("codex app-server와 통신하지 못했습니다.") from None
    finally:
        proc.kill()
        proc.wait()


def _window_label(mins):
    if not mins:
        return "사용 한도"
    if mins == 10080:
        return "주간"
    if mins % 1440 == 0:
        return f"{mins // 1440}일"
    if mins % 60 == 0:
        return f"{mins // 60}시간"
    return f"{mins}분"


def fetch_chatgpt():
    res = _codex_rpc({1: ("account/rateLimits/read", None), 2: ("account/read", {})})
    account = res[2].get("account")
    if not account or account.get("type") != "chatgpt":
        raise SourceError("Codex에 ChatGPT 계정으로 로그인되어 있지 않습니다. 터미널에서 `codex login`을 실행하세요.")

    rate = res[1]
    limits = rate.get("rateLimitsByLimitId") or {"codex": rate.get("rateLimits")}
    windows = []
    for limit_id, limit in limits.items():
        if not limit:
            continue
        prefix = "" if len(limits) == 1 else f"{limit.get('limitName') or limit_id} · "
        for slot in ("primary", "secondary"):
            window = limit.get(slot)
            if window:
                windows.append({
                    "id": f"{limit_id}.{slot}",
                    "label": prefix + _window_label(window.get("windowDurationMins")),
                    "usedPercent": window.get("usedPercent", 0),
                    "resetsAt": window.get("resetsAt"),
                })
    if not windows:
        raise SourceError("Codex가 사용 한도 정보를 돌려주지 않았습니다.")

    available = [c for c in (rate.get("rateLimitResetCredits") or {}).get("credits", []) if c.get("status") == "available"]
    credits = (rate.get("rateLimits") or {}).get("credits") or {}
    return {
        "plan": (account.get("planType") or "").capitalize() or None,
        "windows": windows,
        "resetCredits": {
            "count": len(available),
            "nextExpiry": min((c["expiresAt"] for c in available if c.get("expiresAt")), default=None),
        },
        "creditBalance": credits.get("balance") if credits.get("hasCredits") else None,
    }


# ---------- HTTP ----------

SOURCES = {"/api/claude": fetch_claude, "/api/chatgpt": fetch_chatgpt}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 다른 사이트가 DNS 리바인딩으로 이 서버를 읽지 못하도록 로컬 호스트 이름만 허용합니다.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            return self._send(403, b"forbidden", "text/plain")

        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        fetch = SOURCES.get(path)
        if not fetch:
            return self._send(404, b"not found", "text/plain")

        try:
            status, body = 200, {"ok": True, "fetchedAt": time.time(), **fetch()}
        except SourceError as e:
            status, body = 502, {"ok": False, "error": str(e)}
        except Exception as e:  # 예상치 못한 실패도 페이지에 이유를 보여줍니다
            status, body = 500, {"ok": False, "error": f"예상치 못한 오류: {type(e).__name__}"}
        self._send(status, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"사용량 대시보드: {url}  (종료: Ctrl+C)")
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
