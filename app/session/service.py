"""세션 서비스: 웹 계층이 부르는 단일 진입점. 전송(HTTP·WebSocket)과 무관하게 세션 생성, 계획 생성, 승인·실행 동작, 화면용 스냅샷을 맡는다.

- 세션마다 `Masker`를 하나 둔다(재시작 후에는 `mapping_store`에서 복호화해 복원, 실패하면 중단). 동작이 끝날 때마다 매핑을 저장하고,
  세션이 끝나거나 취소되면 매핑을 삭제한다(D-29).
- 동작(`handle`)은 허용된 메시지 종류만 받고 값의 종류를 검사한다. 오류는 코드로만 돌려준다(값 없음).
- 사용자에게 보이는 스냅샷(`view`)만 플레이스홀더를 실제 값으로 복원한다. 저장·로그·모델로 가는 데이터는 마스킹된 채다.
- 오래 걸리는 일(계획 생성, poll)은 백그라운드 스레드에서 돌고, 바뀔 때마다 리스너(웹 계층의 브로드캐스트)를 부른다.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from typing import Any, Callable

from app.executor.base import Executor
from app.guards.audit import AuditLog
from app.guards.command_guard import CommandGuard
from app.guards.input_guard import check_user_input, user_message
from app.guards.mapping_store import MappingStore, MappingUnreadable
from app.guards.masking import Inventory, Masker
from app.planner.pipeline import PipelineResult, PlanPipeline
from app.planner.schema import Plan, PlanInvalid
from app.session.apply import ApplyEngine
from app.session.approval import AUTO, ApprovalEngine, ApprovalError
from app.session.edit import PlanEditor
from app.session.replan import ReplanEngine
from app.session.states import InvalidTransition
from app.session.store import SessionStore, StoreError

MAX_TEXT = 2000
MAX_TITLE = 60
MAX_ROUNDS = 6                 # 한 세션에서 보탤 수 있는 입력 수(요청 포함)
CLOSED = ("completed", "aborted")


class ServiceError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(code)
        self.code, self.message = code, message


def _need(msg: dict, key: str, typ: type, *, optional: bool = False, default: Any = None):
    if key not in msg or (optional and msg[key] is None):          # 선택 필드는 null도 "없음"으로 본다(화면이 "맨 앞으로"를 after: null로 보낸다)
        if optional:
            return default
        raise ServiceError(f"missing_field:{key}")
    v = msg[key]
    if typ is int and isinstance(v, bool):
        raise ServiceError(f"bad_field:{key}")
    if not isinstance(v, typ):
        raise ServiceError(f"bad_field:{key}")
    return v


class SessionService:
    def __init__(self, store: SessionStore, mapping_store: MappingStore, *, executor: Executor, guard: CommandGuard,
                 pipeline_factory: Callable[[Masker, str], PlanPipeline], inventory_factory: Callable[[], Inventory] = Inventory.empty,
                 analyzer_factory: Callable[[Masker], Any] | None = None, audit: AuditLog | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self._sleep = sleep
        self.store, self._maps = store, mapping_store
        self._pipeline_factory, self._inventory_factory, self._analyzer_factory = pipeline_factory, inventory_factory, analyzer_factory
        self._maskers: dict[str, Masker] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._glock = threading.Lock()
        self._threads: dict[tuple[str, str], threading.Thread] = {}
        self.listeners: list[Callable[[str], None]] = []
        self.ap = ApprovalEngine(store, self.masker_for, audit=audit)
        self.apply = ApplyEngine(store, self.ap, executor, guard, self.masker_for, analyzer=self._analyze if analyzer_factory else None, audit=audit, clock=clock,
                                 on_event=self._apply_event)
        self.replan = ReplanEngine(store, self.ap)
        self.editor = PlanEditor(guard, self.masker_for)
        self._audit = audit

    # ---------------- 마스킹 매핑 ----------------
    def masker_for(self, sid: str) -> Masker:
        with self._glock:
            m = self._maskers.get(sid)
            if m is not None:
                return m
            m = Masker(self._inventory_factory())
            if self.store.session(sid)["state"] not in CLOSED:
                try:
                    self._maps.load_into(sid, m)
                except MappingUnreadable:
                    raise ServiceError("mapping_unreadable", "세션의 매핑을 읽을 수 없어 중단했습니다. 새 세션으로 다시 시작해 주세요.") from None
            self._maskers[sid] = m
            return m

    def _persist(self, sid: str) -> None:
        state = self.store.session(sid)["state"]
        if state in CLOSED:
            self._maps.delete(sid)                                   # 종료·취소되면 실제 값과의 매핑을 지운다
            with self._glock:
                self._maskers.pop(sid, None)
            return
        self._maps.save(sid, self.masker_for(sid))

    def _lock(self, sid: str) -> threading.RLock:
        with self._glock:
            return self._locks.setdefault(sid, threading.RLock())

    def _analyze(self, plan, step_id, result, attempt, sid):
        return self._analyzer_factory(self.masker_for(sid)).analyze(plan, step_id, result, attempt=attempt, session_id=sid)

    HINT_KO = {"throttled": "요청 제한", "permission": "권한 오류", "not_found": "대상 없음", "invalid_argument": "인자 오류", "dependency": "다른 리소스가 쓰고 있음",
               "already_exists": "이미 존재", "conflict": "상태 충돌", "timeout": "시간 초과", "quota": "한도 초과", "unknown": "알 수 없는 오류"}
    REC_KO = {"retry": "재시도", "skip": "건너뛰기", "abort": "중단", "replan": "남은 절차 수정(재계획)"}

    def _apply_event(self, sid: str, kind: str, data: dict) -> None:
        """실패와 AI 분석을 대화에 싣는다. 분석하는 동안은 `analyzing`에 절차 id를 두어 화면이 "답변을 생성 중"을 보이게 한다."""
        step = data.get("step_id", "")
        if kind == "failed":
            why = self.HINT_KO.get(data.get("hint", "unknown"), "오류")
            self._thread_add(sid, "assistant", f"절차 {step}가 실패했습니다({why})." + (" 원인을 분석하는 중입니다…" if data.get("analyzing") else " 재시도, 건너뛰기, 중단 중에서 고르세요."))
            self.store.set_meta(sid, "analyzing", json.dumps(step if data.get("analyzing") else None))
        elif kind == "analysis":
            self.store.set_meta(sid, "analyzing", json.dumps(None))
            a = data.get("summary") or {}
            if a.get("status") == "analysis":
                rec = self.REC_KO.get(a.get("recommendation", ""), a.get("recommendation", ""))
                note = "\n(적용됐을 수 있어 재시도 전에 상태 확인이 필요합니다.)" if a.get("needs_state_check") else ""
                self._thread_add(sid, "assistant", f"절차 {step} 실패 분석(AI 추정, 참고용): {a.get('cause', '')}\n서버 규칙으로 좁힌 추천: {rec}.{note}\n재시도, 건너뛰기, 중단 중에서 고르세요.")
            else:
                self._thread_add(sid, "assistant", f"절차 {step}의 실패 원인을 분석하지 못했습니다. 재시도, 건너뛰기, 중단 중에서 직접 고르세요.")
        self._notify(sid)

    def _notify(self, sid: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(sid)
            except Exception:                                        # 알림 실패가 동작을 막지 않는다
                pass

    # ---------------- 세션 ----------------
    def create(self, nick: str, title: str, text: str, *, iac_managed: bool = False) -> dict:
        """세션을 만든다. 입력 가드가 막은 요청은 세션을 만들지 않는다. 요청 원문은 저장하지 않는다(마스킹된 문장만)."""
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ServiceError("input_rejected", user_message(("too_long",)))
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            raise ServiceError("title_required", f"작업 제목을 1~{MAX_TITLE}자로 입력하세요.")
        probe = Masker(self._inventory_factory())
        tv = check_user_input(title.strip(), probe)
        if not tv.allow or tv.masked_text is None:
            raise ServiceError("input_rejected", user_message(tv.reasons))
        v = check_user_input(text, probe)
        if not v.allow or v.masked_text is None:
            raise ServiceError("input_rejected", user_message(v.reasons))
        try:
            sid, invite, token = self.store.create_session(nick, v.masked_text, iac_managed=iac_managed, title=tv.masked_text)
        except StoreError as e:
            raise ServiceError(e.code) from None
        self._maskers[sid] = probe                                   # 입력을 마스킹한 매핑을 그대로 이어서 쓴다
        self._thread_add(sid, "user", v.masked_text)
        self._set_pipeline(sid, "generating")
        self._persist(sid)
        return {"session_id": sid, "invite": invite, "member_token": token}

    def join(self, sid: str, invite: str, nick: str) -> dict:
        try:
            token = self.store.join(sid, invite, nick)
        except StoreError as e:
            raise ServiceError(e.code) from None
        self._notify(sid)
        return {"member_token": token}

    def authenticate(self, sid: str, token: str) -> str | None:
        try:
            return self.store.authenticate(sid, token)
        except StoreError:
            return None

    # ---------------- 계획 생성 ----------------
    def generate_plan(self, sid: str, text: str) -> PipelineResult:
        """요청 문장으로 계획을 만든다(동기). 성공하면 plan 검토로 넘어간다."""
        sess = self.store.session(sid)
        masker = self.masker_for(sid)
        res = self._pipeline_factory(masker, sid).run(text, iac_managed=bool(sess["iac_managed"]), session_id=sid)
        meta = {"status": res.status, "message": res.message, "questions": list(res.questions), "reasons": list(res.reasons), "detail": list(res.detail),
                "interpretations": list(res.intake.interpretations) if res.intake else []}
        if res.status != "plan":
            qs = [q.get("question") or f"{q.get('alias', '')}: {' / '.join(q.get('candidates', []))}" for q in res.questions]
            self._thread_add(sid, "assistant", "\n".join([res.message, *[f"- {q}" for q in qs]]).strip(), kind="clarify" if res.status == "clarify" else "")
        if res.status == "plan" and res.plan_result is not None and res.plan_result.plan is not None:
            if res.review is not None:
                self.store.set_meta(sid, "review", json.dumps(dataclasses.asdict(res.review), ensure_ascii=False, default=list))
            self.store.set_meta(sid, "evidence", json.dumps(res.evidence, ensure_ascii=False, default=list))
            self.ap.submit_plan(sid, self.store.requester_of(sid), res.plan_result.plan)
            self._thread_add(sid, "assistant", "계획을 만들었습니다. 아래에서 검토하고, 다른 참여자가 승인하면 apply를 시작할 수 있습니다.")
        self.store.set_meta(sid, "pipeline", json.dumps(meta, ensure_ascii=False, default=list))      # 상태는 모든 준비가 끝난 뒤 마지막에 바꾼다(중간 상태가 화면에 보이지 않게)
        self._persist(sid)
        self._notify(sid)
        return res

    def start_generation(self, sid: str, text: str) -> None:
        def work():
            try:
                self.generate_plan(sid, text)
            except Exception as e:                                   # 예외 내용(값이 섞일 수 있음)은 남기지 않고 종류만 남긴다
                self._set_pipeline(sid, "stopped", "계획 생성 중 오류가 나 중단했습니다. 내용을 보태서 다시 보내 주세요.", reasons=[type(e).__name__])
                self._thread_add(sid, "assistant", "계획 생성 중 오류가 나 중단했습니다. 내용을 보태서 다시 보내 주세요.")
                self._notify(sid)
        t = threading.Thread(target=work, name=f"plan-{sid[:6]}", daemon=True)
        t.start()

    # ---------------- 대화(같은 세션에서 추가 정보를 이어서 받는다) ----------------
    def _thread(self, sid: str) -> list[dict]:
        return json.loads(self.store.get_meta(sid, "thread") or "[]")

    def _thread_add(self, sid: str, role: str, text: str, *, kind: str = "") -> None:        # kind: retry(다시 시도 표시), clarify(되묻기 질문)
        t = self._thread(sid)
        t.append({"role": role, "text": text[:1500], "ts": time.time(), **({"kind": kind} if kind else {})})
        self.store.set_meta(sid, "thread", json.dumps(t[-60:], ensure_ascii=False))

    def _set_pipeline(self, sid: str, status: str, message: str = "", *, reasons: list | None = None) -> None:
        self.store.set_meta(sid, "pipeline", json.dumps({"status": status, "message": message, "questions": [], "reasons": reasons or [], "interpretations": []},
                                                        ensure_ascii=False))

    def _answer(self, sid: str, nick: str, text: str) -> None:
        """계획이 만들어지기 전(되묻기·중단·거절 뒤)에 요청자가 같은 세션에서 내용을 보태 계획 생성을 다시 시작한다.
        입력은 입력 가드·마스킹을 거치고, 모델에는 지금까지의 마스킹된 요청과 보탠 내용을 이어 붙여 보낸다."""
        if self.store.role_of(sid, nick) is None:
            raise ApprovalError("not_a_participant")
        sess = self.store.session(sid)
        if nick.lower() != sess["requester"].lower():
            raise ApprovalError("answer_by_requester_only")
        meta = json.loads(self.store.get_meta(sid, "pipeline") or "null") or {}
        if sess["state"] != "draft" or meta.get("status") in (None, "plan", "generating"):
            raise ServiceError("answer_not_expected")
        v = check_user_input(text, self.masker_for(sid))
        if not v.allow or v.masked_text is None:
            raise ServiceError("input_rejected", user_message(v.reasons))
        combined = self._combined(sid, v.masked_text)
        self._thread_add(sid, "user", v.masked_text)
        self._set_pipeline(sid, "generating")
        self.start_generation(sid, combined)

    def _combined(self, sid: str, extra: str | None = None) -> str:
        """지금까지의 마스킹된 요청, 에이전트가 이전에 한 질문, 사용자의 답을 이어 붙인다(모델에는 이것만 간다).
        질문을 함께 줘야 모델이 "기본으로 해줘" 같은 짧은 답이 무엇에 대한 답인지 알고 같은 것을 다시 묻지 않는다.
        길이를 넘으면 오래된 질문부터 빼고, 입력 횟수·길이 상한을 넘으면 거부한다."""
        thread = [m for m in self._thread(sid) if m.get("kind") != "retry"]          # 다시 시도 표시는 모델로 보내지 않는다
        first = next((m for m in thread if m["role"] == "user"), None)
        if first is None:
            raise ServiceError("no_request")
        rounds: list[tuple[str | None, str]] = []                                   # (직전의 되묻기 질문, 사용자의 답)
        question: str | None = None
        for m in thread[thread.index(first) + 1:]:
            if m["role"] == "assistant" and m.get("kind") == "clarify":
                question = m["text"][:300]
            elif m["role"] == "user":
                rounds.append((question, m["text"]))
                question = None
        if extra:
            rounds.append((question, extra))
        if 1 + len(rounds) > MAX_ROUNDS:
            raise ServiceError("conversation_too_long", "대화가 길어져 이어 갈 수 없습니다. 새 작업으로 시작해 주세요.")

        def build(drop_questions: int) -> str:
            parts = [first["text"]]
            for i, (q, a) in enumerate(rounds):
                if q and i >= drop_questions:
                    parts.append(f"[이전 질문] {q}")
                parts.append(f"[추가 정보] {a}")
            return "\n\n".join(parts)

        for drop in range(len(rounds) + 1):                                         # 길면 오래된 질문부터 뺀다
            combined = build(drop)
            if len(combined) <= MAX_TEXT - 100:
                return combined
        raise ServiceError("conversation_too_long", "대화가 길어져 이어 갈 수 없습니다. 새 작업으로 시작해 주세요.")

    def _retry(self, sid: str, nick: str) -> None:
        """계획 생성이 중단된 세션에서 요청자가 같은 내용으로 다시 시도한다(새 입력 없이). 계획 생성 중이거나 만들어진 뒤에는 받지 않는다."""
        if self.store.role_of(sid, nick) is None:
            raise ApprovalError("not_a_participant")
        sess = self.store.session(sid)
        if nick.lower() != sess["requester"].lower():
            raise ApprovalError("retry_by_requester_only")
        meta = json.loads(self.store.get_meta(sid, "pipeline") or "null") or {}
        if sess["state"] != "draft" or meta.get("status") != "stopped":
            raise ServiceError("retry_not_expected")
        combined = self._combined(sid)
        self._thread_add(sid, "user", "(같은 내용으로 다시 시도)", kind="retry")
        self._set_pipeline(sid, "generating")
        self.start_generation(sid, combined)

    # ---------------- 동작 ----------------
    def handle(self, sid: str, nick: str, msg: dict) -> dict:
        """참여자의 동작 하나를 처리한다. 성공하면 {"ok": True}, 거부되면 {"ok": False, "code": ...}."""
        try:
            with self._lock(sid):
                data = self._dispatch(sid, nick, msg)
                if self.store.session(sid)["state"] == "applying":
                    self._resume_polls(sid)                      # 확인 step이 poll이라 자동으로 시작된 조회에도 반복 스레드를 붙인다
                self._persist(sid)
            out = {"ok": True, **(data or {})}
        except ApprovalError as e:
            out = {"ok": False, "code": e.code}
        except (StoreError, InvalidTransition) as e:
            out = {"ok": False, "code": e.code}
        except PlanInvalid as e:
            out = {"ok": False, "code": "plan_invalid", "codes": e.codes[:8]}
        except ServiceError as e:
            out = {"ok": False, "code": e.code, **({"message": e.message} if e.message else {})}
        self._notify(sid)
        return out

    def _dispatch(self, sid: str, nick: str, m: dict) -> dict | None:
        t = _need(m, "type", str)
        v = lambda: _need(m, "version", int)          # noqa: E731
        step = lambda: _need(m, "step", str)          # noqa: E731
        if t == "approve_plan":
            self.ap.approve_plan(sid, nick, v(), _need(m, "hash", str))
        elif t == "start_apply":
            self.ap.start_apply(sid, nick)
            self._resume_polls(sid)
        elif t == "approve_step":
            self.ap.approve_step(sid, nick, v(), step(), _need(m, "hash", str))
        elif t == "provide_reason":
            self.ap.provide_reason(sid, nick, v(), step(), _need(m, "text", str))
        elif t == "confirm_requirement":
            self.ap.confirm_requirement(sid, nick, v(), step(), _need(m, "requirement", str))
        elif t == "approve_gate":
            self.ap.approve_gate(sid, nick, v(), _need(m, "phase", str))
        elif t == "run_step":
            self.apply.run_step(sid, v(), step(), nick=nick)
        elif t == "run_read":
            self._run_read(sid, nick, v(), step(), _need(m, "hash", str))
        elif t == "start_poll":
            self._start_poll(sid, v(), step(), nick)
        elif t == "poll_complete":
            self.apply.poll_complete(sid, nick, v(), step())
        elif t == "report_done":
            self.apply.report_done(sid, nick, v(), step(), text=_need(m, "text", str, optional=True, default=""),
                                   checked=[_need({"x": i}, "x", int) for i in _need(m, "checked", list, optional=True, default=[])])
        elif t == "report_failed":
            self.apply.report_failed(sid, nick, v(), step(), text=_need(m, "text", str, optional=True, default=""))
        elif t == "confirm_report":
            self.apply.confirm_report(sid, nick, v(), step(), accept=_need(m, "accept", bool, optional=True, default=True),
                                      note=_need(m, "note", str, optional=True, default=""))
        elif t == "decide":
            self.apply.decide(sid, nick, v(), step(), _need(m, "choice", str), state_checked=_need(m, "state_checked", bool, optional=True, default=False))
        elif t == "preview_read":
            self.apply.preview_read(sid, v(), step(), nick=nick)
        elif t == "pause":
            self.replan.pause(sid, nick)
        elif t == "abort":
            self.apply.abort(sid, nick, v())
        elif t == "edit":
            self._edit(sid, nick, m)
        elif t == "answer":
            self._answer(sid, nick, _need(m, "text", str))
        elif t == "retry_generation":
            self._retry(sid, nick)
        elif t == "create_invite":
            if nick.lower() != self.store.requester_of(sid).lower():
                raise ApprovalError("invite_by_requester_only")
            if self.store.session(sid)["state"] in CLOSED:
                raise ServiceError("session_closed")
            return {"invite": self.store.rotate_invite(sid, nick=nick)}
        else:
            raise ServiceError("unknown_message")

    def _edit(self, sid: str, nick: str, m: dict) -> None:
        op = _need(m, "op", str)
        if self.store.role_of(sid, nick) is None:
            raise ApprovalError("not_a_participant")
        v = self.store.latest_version(sid)
        if v is None:
            raise ServiceError("no_plan")
        plan = self.store.load_plan(sid, v)
        if op == "add_step":
            new = self.editor.add_step(sid, plan, _need(m, "phase", str), _need(m, "step", dict), after=_need(m, "after", str, optional=True))
        elif op == "add_steps":
            new = self.editor.add_steps(sid, plan, _need(m, "phase", str), _need(m, "steps", list), after=_need(m, "after", str, optional=True))
        elif op == "update_step":
            new = self.editor.update_step(sid, plan, _need(m, "step_id", str), **_need(m, "fields", dict))
        elif op == "delete_step":
            new = self.editor.delete_step(sid, plan, _need(m, "step_id", str))
        elif op == "move_step":
            new = self.editor.move_step(sid, plan, _need(m, "step_id", str), after=_need(m, "after", str, optional=True))
        elif op == "set_gate":
            new = self.editor.set_gate(sid, plan, _need(m, "phase", str), _need(m, "gate", bool))
        else:
            raise ServiceError("unknown_edit_op")
        if self.store.session(sid)["state"] == "paused":
            self.replan.submit_replan(sid, nick, new)                 # apply 중 수정은 일시정지 후 부분 재계획으로만
        else:
            self.ap.submit_plan(sid, nick, new, parent_version=v)

    def _run_read(self, sid: str, nick: str, version: int, step_id: str, plan_hash: str) -> None:
        """변경이 아닌 읽기 절차(사전 점검 포함)는 별도 승인 없이 "확인" 한 번으로 승인과 실행을 이어서 한다.
        문서 근거가 있고(AI 추정 아님) 추가 조치(`requires`)가 없는 읽기 step만 가능하다. 승인 규칙·순서·해시 검사는 `approve_step`이 그대로 한다."""
        plan = self.store.load_plan(sid, version)
        s = next((x for x in plan.all_steps() if x.id == step_id), None)
        if s is None:
            raise ApprovalError("unknown_step")
        if s.op != "read" or s.needs_other_approval or s.requires or s.kind == "manual":
            raise ApprovalError("confirm_not_allowed")                # 변경·AI 추정·추가 조치가 있는 절차는 승인 절차를 거친다
        self.ap.approve_step(sid, nick, version, step_id, plan_hash)
        if s.kind == "poll":
            self._start_poll(sid, version, step_id, nick)
        else:
            self.apply.run_step(sid, version, step_id, nick=nick)

    # ---------------- poll(백그라운드) ----------------
    def _start_poll(self, sid: str, version: int, step_id: str, nick: str) -> None:
        key = (sid, step_id)
        t = self._threads.get(key)
        if t is not None and t.is_alive():
            raise ServiceError("poll_already_running")
        self.apply.poll_once(sid, version, step_id, nick=nick)         # 첫 조회는 요청 안에서(승인·선점 오류를 바로 돌려준다)
        self._spawn_poll(sid, version, step_id, nick)

    def _spawn_poll(self, sid: str, version: int, step_id: str, nick: str) -> None:
        def sleep(sec: float) -> None:
            self._notify(sid)
            self._sleep(sec)

        def work():
            try:
                self.apply.run_poll(sid, version, step_id, nick=nick, sleep=sleep)
            except (ApprovalError, StoreError, InvalidTransition):
                pass
            finally:
                self._notify(sid)
        t = threading.Thread(target=work, name=f"poll-{step_id}", daemon=True)
        self._threads[(sid, step_id)] = t
        t.start()

    def _resume_polls(self, sid: str) -> None:
        v = self.store.latest_version(sid)
        if v is None:
            return
        for step_id, st in self.store.step_states(sid, v).items():
            if st["state"] == "running" and st["detail"].startswith("poll_started="):
                t = self._threads.get((sid, step_id))
                if t is None or not t.is_alive():
                    self._spawn_poll(sid, v, step_id, AUTO)

    def recover_all(self) -> dict[str, dict]:
        """서버 시작 시 apply 중이던 세션을 정리한다(poll은 이어서, 그 밖의 실행 중 step은 중단으로 멈춘다)."""
        out = {}
        for sid in self.store.sessions_in("draft"):                   # 계획을 만드는 중 서버가 멈췄다면 답이 오지 않으니 사람이 다시 보내게 한다
            meta = json.loads(self.store.get_meta(sid, "pipeline") or "null") or {}
            if meta.get("status") == "generating":
                self._set_pipeline(sid, "stopped", "서버가 다시 시작되어 계획 생성이 중단되었습니다. 요청을 다시 보내 주세요.")
                self._thread_add(sid, "assistant", "서버가 다시 시작되어 계획 생성이 중단되었습니다. 요청을 다시 보내 주세요.")
        for sid in self.store.sessions_in("applying"):
            v = self.store.latest_version(sid)
            if v is None:
                continue
            res = self.apply.recover_after_restart(sid, v)
            for step_id in res["resumable"]:
                self._spawn_poll(sid, v, step_id, AUTO)
            out[sid] = res
        return out

    def purge_expired_mappings(self) -> int:
        return self._maps.purge_expired()

    # ---------------- 작업 이력(DB) ----------------
    def history(self, creds: list[dict]) -> list[dict]:
        """브라우저가 가진 멤버 토큰으로 그 사람이 참여한 작업의 제목·상태·결과 요약을 DB에서 읽어 돌려준다.
        토큰이 맞지 않는 항목은 `valid: false`로 돌려주고 내용은 주지 않는다. 계정이 없는 Phase 1에서 토큰이 곧 자격이다."""
        out = []
        for c in creds[:50]:
            sid, token = str(c.get("sid", "")), str(c.get("token", ""))
            nick = self.authenticate(sid, token) if sid and token else None
            if nick is None:
                out.append({"sid": sid, "valid": False})
                continue
            s = self.store.session(sid)
            v = self.store.latest_version(sid)
            counts: dict[str, int] = {}
            risk = None
            if v is not None:
                for st in self.store.step_states(sid, v).values():
                    counts[st["state"]] = counts.get(st["state"], 0) + 1
                try:
                    risk = (json.loads(self.store.get_meta(sid, "review") or "null") or {}).get("facts", {}).get("risk_level")
                except ValueError:
                    risk = None
            try:
                title = self.masker_for(sid).unmask(s["title"])
            except ServiceError:
                title = s["title"]
            out.append({"sid": sid, "valid": True, "nick": nick, "title": title or "(제목 없음)", "state": s["state"], "requester": s["requester"],
                        "created_at": s["created_at"], "updated_at": s["updated_at"], "steps": counts, "risk": risk, "version": v,
                        "participants": len(self.store.participants(sid))})
        out.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
        return out

    # ---------------- 화면용 스냅샷 ----------------
    def view(self, sid: str, nick: str) -> dict:
        s = self.store.session(sid)
        masker = self.masker_for(sid)
        v = self.store.latest_version(sid)

        def show(x):
            if isinstance(x, str):
                return masker.unmask(x)
            if isinstance(x, list):
                return [show(i) for i in x]
            if isinstance(x, dict):
                return {k: show(i) for k, i in x.items()}
            return x

        meta = lambda k: json.loads(self.store.get_meta(sid, k) or "null")          # noqa: E731
        review = meta("review")
        if review:                                                                  # 체크리스트 문구는 서버 한 곳(`review.py`)에서 온다
            from app.planner.review import _CHECK_MESSAGES
            for it in review["facts"]["checklist"]:
                it["message"] = _CHECK_MESSAGES.get(it["code"], it["code"])
        out: dict = {"session": {"id": sid, "state": s["state"], "requester": s["requester"], "iac_managed": bool(s["iac_managed"]),
                                 "request": show(s["masked_request"]), "title": show(s["title"]), "created_at": s["created_at"]},
                     "you": {"nick": nick, "role": self.store.role_of(sid, nick)},
                     "participants": self.store.participants(sid), "pipeline": show(meta("pipeline")), "version": v, "thread": show(meta("thread") or []),
                     "analyzing": meta("analyzing")}
        if v is not None:
            plan: Plan = self.store.load_plan(sid, v)
            st = self.store.step_states(sid, v)
            pm = self.store.plan_meta(sid, v)
            pending = {}
            if s["state"] in ("applying", "paused"):
                for sp in plan.all_steps():
                    if st[sp.id]["state"] == "pending":
                        try:
                            pending[sp.id] = self.ap.pending_requirements(sid, v, sp.id)
                        except ApprovalError:
                            pass
            out.update({
                "plan": show(plan.model_dump(mode="json")), "plan_hash": pm["plan_hash"], "plan_status": pm["status"],
                "steps": st, "pending_requirements": pending,
                "approvals": [{k: a[k] for k in ("kind", "target", "nick", "ts")} | {"detail": show(a["detail"])} for a in self.store.approvals(sid, v)],
                "outputs": show([o for o in self.store.outputs(sid, v)][-80:]),
                "review": show(review), "evidence": show(meta("evidence")),
            })
        out["events"] = show(self.store.events(sid)[-60:])
        return out
