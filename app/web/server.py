"""웹 서버(FastAPI + WebSocket). 전송만 맡고 규칙은 모두 `SessionService`와 그 아래 엔진에 있다.

- REST: 세션 생성(`POST /api/sessions`), 초대 수락(`POST /api/sessions/{sid}/join`), `GET /api/health`.
- WebSocket `/ws/{sid}`: 첫 메시지로 멤버 토큰 인증(`{"type":"auth","token":...}`) 후 동작 메시지를 보내고, 세션의 모든 연결이 같은 시점의
  스냅샷(`{"type":"view"}`)을 받는다. 동작의 주체는 인증된 토큰의 닉네임이다(메시지 안의 닉네임 필드는 쓰지 않는다).
- 브라우저의 Origin이 이 서버 호스트가 아니면 거부한다(다른 사이트가 열어 둔 탭에서의 요청 방지). 기본은 루프백에만 바인딩한다.
- 메시지 크기·연결 수 상한이 있다. 처리 중 예외는 내용을 돌려주지 않고 `internal_error`만 돌려준다(값이 섞일 수 있다).
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.session.service import ServiceError, SessionService

MAX_MESSAGE_BYTES = 64 * 1024
MAX_CONNECTIONS_PER_SESSION = 10
AUTH_TIMEOUT = 10.0
PURGE_INTERVAL = 3600.0
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nick: str = Field(max_length=40)
    title: str = Field(min_length=1, max_length=80)
    request: str = Field(max_length=4000)
    iac_managed: bool = False


class Cred(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sid: str = Field(max_length=64)
    token: str = Field(max_length=200)


class HistoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Cred] = Field(max_length=50)


class JoinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invite: str = Field(max_length=200)
    nick: str = Field(max_length=40)


def _origin_ok(origin: str | None, host: str | None) -> bool:
    """Origin이 없으면(브라우저가 아닌 클라이언트) 허용하고, 있으면 요청 호스트와 같아야 한다."""
    if not origin:
        return True
    return bool(host) and urlparse(origin).netloc.lower() == host.lower()


def create_app(service: SessionService, *, static_dir: Path | None = None, purge_interval: float = PURGE_INTERVAL) -> FastAPI:
    conns: dict[str, dict[WebSocket, str]] = {}
    state: dict = {"loop": None}
    static = static_dir or STATIC_DIR

    async def broadcast(sid: str) -> None:
        for ws, nick in list((conns.get(sid) or {}).items()):
            try:
                view = await run_in_threadpool(service.view, sid, nick)
                await ws.send_json({"type": "view", "data": view})
            except Exception:                                        # 끊긴 연결은 정리되고, 스냅샷 실패 내용은 보내지 않는다
                pass

    def on_change(sid: str) -> None:
        loop = state["loop"]
        if loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast(sid), loop)

    async def purge_loop() -> None:
        while True:
            await asyncio.sleep(purge_interval)
            try:
                await run_in_threadpool(service.purge_expired_mappings)
            except Exception:
                pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["loop"] = asyncio.get_running_loop()
        service.listeners.append(on_change)
        await run_in_threadpool(service.recover_all)                # 재시작 전에 진행 중이던 세션 정리, poll 재개
        task = asyncio.create_task(purge_loop())
        try:
            yield
        finally:
            task.cancel()
            if on_change in service.listeners:
                service.listeners.remove(on_change)

    app = FastAPI(title="DevOps Plan/Apply Agent", lifespan=lifespan)

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok(request.headers.get("origin"), request.headers.get("host")):
            return JSONResponse({"detail": "origin_not_allowed"}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/api/sessions")
    async def create_session(body: CreateBody) -> dict:
        try:
            out = await run_in_threadpool(service.create, body.nick, body.title, body.request, iac_managed=body.iac_managed)
        except ServiceError as e:
            raise HTTPException(400, {"code": e.code, "message": e.message}) from None
        service.start_generation(out["session_id"], body.request)      # 계획 생성은 백그라운드. 진행은 WebSocket 스냅샷으로 알린다
        return {**out, "join_path": f"/?s={out['session_id']}&i={out['invite']}"}

    @app.post("/api/history")
    async def history(body: HistoryBody) -> dict:
        return {"items": await run_in_threadpool(service.history, [c.model_dump() for c in body.items])}

    @app.post("/api/sessions/{sid}/join")
    async def join_session(sid: str, body: JoinBody) -> dict:
        try:
            return await run_in_threadpool(service.join, sid, body.invite, body.nick)
        except ServiceError as e:
            raise HTTPException(400, {"code": e.code}) from None

    @app.websocket("/ws/{sid}")
    async def ws_endpoint(ws: WebSocket, sid: str) -> None:
        if not _origin_ok(ws.headers.get("origin"), ws.headers.get("host")):
            await ws.close(code=4403)
            return
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT)
            msg = json.loads(first) if len(first) <= MAX_MESSAGE_BYTES else {}
            nick = await run_in_threadpool(service.authenticate, sid, str(msg.get("token", ""))) if msg.get("type") == "auth" else None
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            nick = None
        if nick is None:
            await ws.close(code=4401)
            return
        group = conns.setdefault(sid, {})
        if len(group) >= MAX_CONNECTIONS_PER_SESSION:
            await ws.close(code=4429)
            return
        group[ws] = nick
        try:
            await ws.send_json({"type": "view", "data": await run_in_threadpool(service.view, sid, nick)})
            while True:
                raw = await ws.receive_text()
                if len(raw) > MAX_MESSAGE_BYTES:
                    await ws.send_json({"type": "result", "ok": False, "code": "message_too_large"})
                    continue
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError
                except ValueError:
                    await ws.send_json({"type": "result", "ok": False, "code": "bad_json"})
                    continue
                try:
                    res = await run_in_threadpool(service.handle, sid, nick, msg)
                except Exception:                                    # 예외 내용은 돌려주지 않는다
                    res = {"ok": False, "code": "internal_error"}
                await ws.send_json({"type": "result", "id": msg.get("id") if isinstance(msg.get("id"), (str, int)) else None, **res})
        except WebSocketDisconnect:
            pass
        finally:
            group.pop(ws, None)

    @app.get("/")
    def index():
        f = static / "index.html"
        if f.exists():
            return FileResponse(f, headers={"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:"})
        return JSONResponse({"detail": "UI는 T8에서 추가됩니다. API는 /api, WebSocket은 /ws/{session_id}"})

    return app
