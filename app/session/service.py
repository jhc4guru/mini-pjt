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
import sqlite3
import threading
import uuid
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
from app.executor.adapter import ReadRunnerAdapter
from app.session.intents import parse_intent
from app.session.scratch import ScratchError, ScratchPad
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
                 sleep: Callable[[float], None] = time.sleep, catalog_retriever: Any = None, editor_factory: Callable[[Masker], Any] | None = None,
                 tracer: Any = None):
        self._sleep = sleep
        self._tracer = tracer                              # `app.observability` 트레이서(선택, D-60)
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
        self.scratch = ScratchPad(store, self.masker_for, guard, retriever=catalog_retriever,
                                  runner_for=lambda sid: ReadRunnerAdapter(executor, guard, self.masker_for(sid), session_id=sid))     # 연습장(초안)과 명령 만들기 도우미
        self._audit = audit
        self._editor_factory = editor_factory

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
    def create(self, nick: str, title: str, text: str = "", *, iac_managed: bool = False) -> dict:
        """세션을 만든다. 입력 가드가 막은 요청은 세션을 만들지 않는다. 요청 원문은 저장하지 않는다(마스킹된 문장만).
        요청이 비어 있으면 빈 plan(연습장) 모드로 시작한다: 모델이 계획을 만들지 않고 사용자가 직접 절차를 채운다."""
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ServiceError("input_rejected", user_message(("too_long",)))
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            raise ServiceError("title_required", f"작업 제목을 1~{MAX_TITLE}자로 입력하세요.")
        probe = Masker(self._inventory_factory())
        tv = check_user_input(title.strip(), probe)
        if not tv.allow or tv.masked_text is None:
            raise ServiceError("input_rejected", user_message(tv.reasons))
        empty = not text.strip()
        v = None if empty else check_user_input(text, probe)
        if v is not None and (not v.allow or v.masked_text is None):
            raise ServiceError("input_rejected", user_message(v.reasons))
        try:
            sid, invite, token = self.store.create_session(nick, "" if v is None else v.masked_text, iac_managed=iac_managed, title=tv.masked_text)
        except StoreError as e:
            raise ServiceError(e.code) from None
        self._maskers[sid] = probe                                   # 입력을 마스킹한 매핑을 그대로 이어서 쓴다
        if v is None:
            self.store.set_meta(sid, "manual_session", "1")                                # 빈 plan 모드: 에이전트의 결과는 plan이 아니라 초안으로 연습장에 덧붙는다
            self._set_pipeline(sid, "manual", "빈 plan입니다. 가운데에서 절차를 직접 추가하거나, 오른쪽 대화에서 작업을 설명하면 에이전트가 초안을 만듭니다.")
            self._thread_add(sid, "assistant", "빈 plan으로 시작했습니다. 가운데의 ‘명령 만들기’로 절차를 직접 추가하거나, 여기에 작업을 설명하면 초안을 만들어 드립니다.")
        else:
            self._thread_add(sid, "user", v.masked_text)
            self._set_pipeline(sid, "generating")
        self._persist(sid)
        return {"session_id": sid, "invite": invite, "member_token": token, "empty": v is None}

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
        drafts_mode = self._drafts_mode(sid)                       # 결과를 초안으로만 덧붙일지(빈 plan이거나 연습장에 절차가 있을 때)
        # 어느 세션이든 에이전트는 빠진 값을 되묻지 않고 기본값으로 만든다: 결과는 연습장에서 고칠 수 있다(D-47)
        res = self._pipeline_factory(masker, sid).run(text, iac_managed=bool(sess["iac_managed"]), session_id=sid, drafts=True,
                                                       resume=self._load_checkpoint(sid), on_checkpoint=lambda cp: self.store.set_meta(sid, "plan_checkpoint", cp.to_json()))
        if res.status != "stopped":
            self.store.set_meta(sid, "plan_checkpoint", "")                      # 끝났거나(plan) 되물음·거절이면 이어할 것이 없다
        elif res.plan_result is not None and res.plan_result.checkpoint is not None:
            self.store.set_meta(sid, "plan_checkpoint", res.plan_result.checkpoint.to_json())
        self._trace_pipeline(sid, res)
        meta = {"status": res.status, "message": res.message, "questions": list(res.questions), "reasons": list(res.reasons), "detail": list(res.detail),
                "interpretations": list(res.intake.interpretations) if res.intake else []}
        if res.status != "plan":
            qs = [q.get("question") or f"{q.get('alias', '')}: {' / '.join(q.get('candidates', []))}" for q in res.questions]
            self._thread_add(sid, "assistant", "\n".join([res.message, *[f"- {q}" for q in qs]]).strip(), kind="clarify" if res.status == "clarify" else "")
        if res.status == "plan" and res.plan_result is not None and res.plan_result.plan is not None:
            if res.review is not None:
                self.store.set_meta(sid, "review", json.dumps(dataclasses.asdict(res.review), ensure_ascii=False, default=list))
            self.store.set_meta(sid, "evidence", json.dumps(res.evidence, ensure_ascii=False, default=list))
            if drafts_mode:
                added = self.scratch.import_as_drafts(sid, res.plan_result.plan)           # 빈 plan 모드: 확정하지 않은 초안으로만 덧붙인다
                self._thread_add(sid, "assistant", f"초안 {len(added)}개를 연습장에 추가했습니다. 확인하고 고친 뒤 검토 요청에 포함해 [검토 요청]을 누르세요.")
            else:
                self.ap.submit_plan(sid, self.store.requester_of(sid), res.plan_result.plan)
                self.scratch.import_plan(sid, res.plan_result.plan)                        # 연습장에서 고치고 지울 수 있게 같은 절차로 채운다
                self._thread_add(sid, "assistant", "계획을 만들었습니다. 아래에서 검토하고, 다른 참여자가 승인하면 apply를 시작할 수 있습니다.")
        if drafts_mode and res.status == "plan":
            meta = {**meta, "status": "manual", "message": "초안을 연습장에 추가했습니다. 더 요청하거나 직접 고치세요."}      # 초안 모드는 계속 요청할 수 있다
        elif drafts_mode and res.status in ("clarify", "reject", "stopped"):
            meta = {**meta, "status": "manual"}                                                  # 연습장은 막히지 않는다: 되묻기·거절·중단 뒤에도 계속 요청하거나 직접 쓸 수 있다
            if res.status == "stopped":
                self._thread_add(sid, "assistant", "초안 요청이 중단되었습니다. 연습장에서 직접 쓰거나 내용을 바꿔 다시 요청해 보세요.")
        self.store.set_meta(sid, "pipeline", json.dumps(meta, ensure_ascii=False, default=list))      # 상태는 모든 준비가 끝난 뒤 마지막에 바꾼다(중간 상태가 화면에 보이지 않게)
        self._persist(sid)
        self._notify(sid)
        return res

    def _load_checkpoint(self, sid: str):
        """중단된 계획 생성의 체크포인트(D-68). 요청 키가 다르면 에이전트가 버린다."""
        from app.planner.checkpoint import PlanCheckpoint
        return PlanCheckpoint.from_json(self.store.get_meta(sid, "plan_checkpoint"))

    def _trace_pipeline(self, sid: str, res) -> None:
        """계획 생성 결과를 트레이스에 남긴다(상태·원인 코드·수정 시도 횟수·계획 해시만. 요청·계획 내용은 없다)."""
        if self._tracer is None:
            return
        pr = res.plan_result
        h = ""
        if pr is not None and getattr(pr, "plan", None) is not None:
            from app.planner.schema import plan_hash
            h = plan_hash(pr.plan)
        self._tracer.record("pipeline", res.status, session_id=sid, ok=res.status == "plan", code=",".join(res.reasons)[:80], n=getattr(pr, "repairs", 0) or 0, plan_hash=h)

    def start_generation(self, sid: str, text: str) -> None:
        def work():
            try:
                self.generate_plan(sid, text)
            except Exception as e:                                   # 예외 내용(값이 섞일 수 있음)은 남기지 않고 종류만 남긴다
                kind = type(e).__name__
                msg = ("문서 검색용 임베딩 모델을 사용할 수 없어 중단했습니다(모델 접근 권한·설정을 확인하세요). 연습장에서는 직접 쓸 수 있습니다."
                       if kind == "EmbeddingError" else "계획 생성 중 오류가 나 중단했습니다. 내용을 보태서 다시 보내 주세요.")
                try:
                    self._set_pipeline(sid, "manual" if self._drafts_mode(sid) else "stopped", msg, reasons=[kind])
                    self._thread_add(sid, "assistant", msg)
                    self._notify(sid)
                except sqlite3.ProgrammingError:                     # 서버·테스트가 이미 저장소를 닫은 뒤의 오류 기록은 남길 곳이 없다
                    pass
        t = threading.Thread(target=work, name=f"plan-{sid[:6]}", daemon=True)
        t.start()

    # ---------------- 대화(같은 세션에서 추가 정보를 이어서 받는다) ----------------
    def _thread(self, sid: str) -> list[dict]:
        return json.loads(self.store.get_meta(sid, "thread") or "[]")

    def _thread_add(self, sid: str, role: str, text: str, *, kind: str = "") -> None:        # kind: retry(다시 시도 표시), clarify(되묻기 질문)
        t = self._thread(sid)
        t.append({"role": role, "text": text[:1500], "ts": time.time(), **({"kind": kind} if kind else {})})
        self.store.set_meta(sid, "thread", json.dumps(t[-60:], ensure_ascii=False))

    # ---------------- 말로 하는 수정 요청: 모델은 수정안만 제안하고 사람이 수락해야 적용한다 ----------------
    def _propose_edit(self, sid: str, nick: str, text: str) -> None:
        if self.store.role_of(sid, nick) is None:
            raise ApprovalError("not_a_participant")
        if self.store.session(sid)["state"] in ("completed", "aborted"):
            raise ServiceError("session_closed")
        if self.store.get_meta(sid, "proposing") or (json.loads(self.store.get_meta(sid, "pipeline") or "{}").get("status") == "generating"):
            raise ServiceError("busy")
        v = check_user_input(text, self.masker_for(sid))
        if not v.allow or v.masked_text is None:
            raise ServiceError("input_rejected", user_message(v.reasons))
        sc = self.scratch.get(sid)
        intent = parse_intent(v.masked_text, self._step_ids(sid, sc))
        if intent is not None:                                                   # "apply 진행해줘" 같은 진행 요청: 모델 없이 실행 제안 카드로(D-49)
            self._propose_action(sid, nick, intent, v.masked_text)
            return
        if self._editor_factory is None:
            raise ServiceError("edit_unavailable")
        if not sc.steps():
            raise ServiceError("nothing_to_edit")
        self._thread_add(sid, "user", v.masked_text, kind="edit")
        self.store.set_meta(sid, "proposing", "1")
        steps = [{"id": s.id, "kind": s.kind, "title": s.title, "command": s.command, "confirmed": s.confirmed, "phase": p.id} for p in sc.phases for s in p.steps]
        masked = v.masked_text

        def work():
            try:
                pr = self._editor_factory(self.masker_for(sid)).propose(masked, steps, session_id=sid)
                if pr.ops:
                    pid = uuid.uuid4().hex[:8]
                    self.store.set_meta(sid, "proposal", json.dumps({"id": pid, "summary": pr.summary, "ops": list(pr.ops), "by": nick, "status": "open"}, ensure_ascii=False))
                    self._thread_add(sid, "assistant", f"수정안을 만들었습니다: {pr.summary}\n아래에서 확인하고 수락하거나 무시하세요. 수락하기 전에는 연습장이 바뀌지 않습니다.", kind="proposal")
                else:
                    msg = {"model_output_unsafe": "수정안에 민감값 형태가 있어 버렸습니다. 요청을 바꿔 다시 말씀해 주세요.", "no_change": pr.summary}.get(
                        pr.reasons[0] if pr.reasons else "", "서버 규칙에 맞는 수정안을 만들지 못했습니다. 더 구체적으로 말씀해 주세요.")
                    self._thread_add(sid, "assistant", msg)
            except sqlite3.ProgrammingError:
                return
            except Exception as e:                                   # 내용은 남기지 않고 종류만
                try:
                    self._thread_add(sid, "assistant", "수정안을 만드는 중 오류가 나 중단했습니다(" + type(e).__name__ + "). 연습장에서 직접 고칠 수 있습니다.")
                except sqlite3.ProgrammingError:
                    return
            finally:
                try:
                    self.store.set_meta(sid, "proposing", "")
                    self._notify(sid)
                except sqlite3.ProgrammingError:
                    pass
        threading.Thread(target=work, name=f"edit-{sid[:6]}", daemon=True).start()

    # ---------------- 반려: 확인자가 코멘트와 함께 수정을 요청한다(D-51) ----------------
    def _reject(self, sid: str, nick: str, m: dict) -> None:
        summary = _need(m, "summary", str).strip()
        raw_steps = _need(m, "steps", dict, optional=True, default={}) or {}
        if not summary:
            raise ServiceError("comment_required")
        if len(summary) > 500 or len(raw_steps) > 40:
            raise ServiceError("comment_too_long")
        masker = self.masker_for(sid)

        def clean(text: str, limit: int) -> str:
            v = check_user_input(text, masker)
            if len(text) > limit or not v.allow or v.masked_text is None:
                raise ServiceError("input_rejected", user_message(v.reasons) if not v.allow else "코멘트가 너무 깁니다.")
            return v.masked_text
        masked_summary = clean(summary, 500)
        steps = {str(k): clean(str(x).strip(), 300) for k, x in raw_steps.items() if isinstance(x, str) and x.strip()}
        self.ap.reject_plan(sid, nick, _need(m, "version", int), _need(m, "hash", str), list(steps))
        self.store.set_meta(sid, "rejection", json.dumps({"version": _need(m, "version", int), "by": nick, "ts": time.time(), "summary": masked_summary, "steps": steps}, ensure_ascii=False))
        extra = f" (절차 코멘트 {len(steps)}건)" if steps else ""
        self._thread_add(sid, "assistant", f"{nick}님이 plan을 반려했습니다{extra}: {masked_summary}\n코멘트를 보고 연습장에서 고친 뒤 다시 검토를 요청하세요.", kind="reject")

    # ---------------- 대화로 하는 진행 요청 → 실행 제안 카드(D-49) ----------------
    def _step_ids(self, sid: str, sc) -> set[str]:
        ids = {s.id for s in sc.steps()}
        v = self.store.latest_version(sid)
        if v is not None:
            ids |= {s.id for s in self.store.load_plan(sid, v).all_steps()}
        return ids

    def _propose_action(self, sid: str, nick: str, intent, masked_text: str) -> None:
        self._thread_add(sid, "user", masked_text, kind="edit")
        action, why = self._action_for(sid, nick, intent, masked_text)
        if action is None:
            self._thread_add(sid, "assistant", why)
            return
        pid = uuid.uuid4().hex[:8]
        self.store.set_meta(sid, "proposal", json.dumps({"id": pid, "summary": action["label"], "action": action, "by": nick, "status": "open"}, ensure_ascii=False))
        self._thread_add(sid, "assistant", f"실행 제안을 만들었습니다: {action['label']}\n카드의 버튼을 눌러야 실행됩니다. 누르는 사람의 권한으로 처리되고 서버의 승인 규칙이 그대로 적용됩니다.", kind="proposal")

    def _action_for(self, sid: str, nick: str, intent, text: str = "") -> tuple[dict | None, str]:
        """지금 세션 상태에서 할 수 있는 동작 하나를 `action`(정식 메시지 + 표시용 label·who)으로 만든다. 못 하면 (None, 이유)."""
        sess = self.store.session(sid)
        state, is_req = sess["state"], nick.lower() == sess["requester"].lower()
        v = self.store.latest_version(sid)
        k = intent.kind
        if k == "send_review":
            n = sum(1 for s in self.scratch.get(sid).steps() if s.confirmed)
            if state not in ("draft", "plan_review", "plan_approved", "paused"):
                return None, "지금은 검토를 요청할 수 없습니다(apply 진행 중이면 일시정지 후 요청하세요)."
            if n == 0:
                return None, "검토 요청에 포함한 절차가 없습니다. 연습장에서 절차의 체크박스를 먼저 눌러 주세요."
            return {"type": "send_to_apply", "label": f"검토 요청 (절차 {n}개)", "who": "requester"}, ""
        if k == "reject_plan":
            if state not in ("plan_review", "plan_approved"):
                return None, "반려할 plan이 없습니다(검토 요청 뒤, apply 시작 전에만 반려할 수 있습니다)."
            if is_req:
                return None, "요청자는 자기 plan을 반려할 수 없습니다. 연습장에서 고치고 다시 검토를 요청하세요."
            if v is None:
                return None, "반려할 plan이 없습니다."
            return {"type": "reject_plan", "version": v, "hash": self.store.plan_meta(sid, v)["plan_hash"], "summary": text[:500], "steps": {}, "label": f"plan v{v} 반려(수정 요청): {text[:80]}", "who": "approver"}, ""
        if k == "start_apply":
            if state == "plan_approved":
                return {"type": "start_apply", "label": "apply 시작", "who": "requester"}, ""
            why = {"plan_review": "아직 apply를 시작할 수 없습니다. 요청자가 아닌 확인자가 plan을 확정(일괄 승인)해야 합니다.", "applying": "이미 apply가 진행 중입니다.",
                   "paused": "일시정지 중입니다. 남은 절차를 고쳐 재계획을 요청하세요."}.get(state, "지금은 apply를 시작할 수 없습니다(검토 요청 → 확인자의 확정(승인) 뒤에 가능합니다).")
            return None, why
        if v is None:
            return None, "아직 검토를 요청한 plan이 없습니다. 먼저 연습장에서 검토를 요청하세요."
        plan = self.store.load_plan(sid, v)
        steps = {s.id: s for s in plan.all_steps()}
        st = self.store.step_states(sid, v)
        if k == "approve_plan" or (k == "approve_step" and state == "plan_review"):
            if state != "plan_review":
                return None, "확정(승인)할 plan이 없습니다(이미 확정했거나 아직 검토 요청 전입니다)."
            if is_req:
                return None, "요청자는 plan을 확정(승인)할 수 없습니다. 요청자가 아닌 확인자가 확정해야 apply로 갈 수 있습니다."
            return {"type": "approve_plan", "version": v, "hash": self.store.plan_meta(sid, v)["plan_hash"], "label": f"plan v{v} 확정(일괄 승인)", "who": "approver"}, ""
        if state != "applying":
            return None, "절차 승인·실행은 apply가 진행 중일 때만 할 수 있습니다(검토 요청 → 확인자의 확정 → apply 시작 순서입니다)."
        h = self.store.plan_meta(sid, v)["plan_hash"]
        confirmable = lambda s: s.op == "read" and s.grounded and not s.requires and s.kind != "manual"      # noqa: E731
        order = [steps[x.id] for x in plan.all_steps()]
        if k == "approve_step":
            cands = [s for s in order if st[s.id]["state"] == "pending" and not confirmable(s) and s.op in ("read", "mutate")]
            if intent.step_hint:
                cands = [s for s in cands if s.id == intent.step_hint]
            if not cands:
                return None, "승인할 절차가 없습니다(승인 대기 중인 절차가 없거나 이미 승인했습니다)."
            if is_req:
                return None, f"요청자는 변경 절차를 승인할 수 없습니다. 요청자가 아닌 확인자가 {cands[0].id}을(를) 승인해야 합니다."
            s = cands[0]
            return {"type": "approve_step", "version": v, "step": s.id, "hash": h, "label": f"{s.id} 승인 ({s.title})", "who": "approver"}, ""
        # 실행: 승인된 절차(run_step·start_poll) 또는 확인 한 번으로 되는 읽기 절차(run_read)
        cands = [s for s in order if (st[s.id]["state"] == "approved" and s.op in ("read", "mutate")) or (st[s.id]["state"] == "pending" and confirmable(s))]
        if intent.step_hint:
            cands = [s for s in cands if s.id == intent.step_hint]
        if not cands:
            waiting = [s.id for s in order if st[s.id]["state"] == "pending" and not confirmable(s) and s.op in ("read", "mutate")][:3]
            return None, "지금 실행할 수 있는 절차가 없습니다." + (f" 승인을 기다리는 절차: {', '.join(waiting)} (요청자가 아닌 확인자의 승인이 필요합니다)." if waiting else "")
        s = cands[0]
        if st[s.id]["state"] == "approved":
            msg = {"type": "start_poll"} if s.kind == "poll" else {"type": "run_step"}
            return {**msg, "version": v, "step": s.id, "label": f"{s.id} {'폴링 시작' if s.kind == 'poll' else '실행'} ({s.title})", "who": "anyone"}, ""
        return {"type": "run_read", "version": v, "step": s.id, "hash": h, "label": f"{s.id} 확인(실행) ({s.title})", "who": "anyone"}, ""

    def _open_proposal(self, sid: str, pid: str) -> dict:
        p = json.loads(self.store.get_meta(sid, "proposal") or "null")
        if not p or p.get("id") != pid or p.get("status") != "open":
            raise ServiceError("proposal_not_open")
        return p

    def _accept_proposal(self, sid: str, nick: str, pid: str) -> dict:
        """수정안을 사람이 수락하면 연산을 차례로 적용한다. 하나라도 거부되면 이미 적용한 것을 되돌리고(전부 아니면 전무) 이유를 알린다."""
        if self.store.role_of(sid, nick) is None:
            raise ApprovalError("not_a_participant")
        p = self._open_proposal(sid, pid)
        if p.get("action"):                                                       # 진행 요청 카드: 누른 사람의 신원으로 정식 메시지를 처리한다(서버 검사 그대로, 거부되면 카드는 남는다)
            act = {k: x for k, x in p["action"].items() if k not in ("label", "who")}
            self._dispatch(sid, nick, act)
            p["status"] = "accepted"
            self.store.set_meta(sid, "proposal", json.dumps(p, ensure_ascii=False))
            self._thread_add(sid, "assistant", f"{nick}님이 실행했습니다: {p['action']['label']}")
            return {"applied": 1}
        applied = 0
        try:
            for op in p["ops"]:
                self.scratch.op(sid, nick, {"type": "scratch", **op})
                applied += 1
        except ScratchError as e:
            for _ in range(applied):
                try:
                    self.scratch.op(sid, nick, {"type": "scratch", "op": "undo"})
                except ScratchError:
                    break
            p["status"] = "failed"
            self.store.set_meta(sid, "proposal", json.dumps(p, ensure_ascii=False))
            self._thread_add(sid, "assistant", "수정안을 적용하지 못했습니다(" + str(e.code if hasattr(e, "code") else e)[:80] + "). 연습장은 바뀌지 않았습니다.")
            self._notify(sid)
            return {"applied": 0}
        p["status"] = "accepted"
        self.store.set_meta(sid, "proposal", json.dumps(p, ensure_ascii=False))
        self._thread_add(sid, "assistant", f"수정안 {applied}개를 적용했습니다. 검토 요청 포함이 풀린 절차는 다시 포함하세요(↶로 되돌릴 수 있습니다).")
        self._persist(sid)
        return {"applied": applied}

    def _dismiss_proposal(self, sid: str, nick: str, pid: str) -> None:
        if self.store.role_of(sid, nick) is None:
            raise ApprovalError("not_a_participant")
        p = self._open_proposal(sid, pid)
        p["status"] = "dismissed"
        self.store.set_meta(sid, "proposal", json.dumps(p, ensure_ascii=False))
        self._thread_add(sid, "assistant", "수정안을 무시했습니다. 연습장은 바뀌지 않았습니다.")

    def _drafts_mode(self, sid: str) -> bool:
        """에이전트의 결과를 plan이 아니라 확정하지 않은 초안으로만 연습장에 덧붙이는가: 빈 plan 세션이거나, 연습장에 이미 작업한 절차가 있을 때."""
        return bool(self.store.get_meta(sid, "manual_session")) or bool(self.scratch.get(sid).steps())

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
        if sess["state"] not in ("draft", "plan_review", "plan_approved", "paused") or meta.get("status") in (None, "plan", "generating") or                 (sess["state"] != "draft" and meta.get("status") != "manual"):
            raise ServiceError("answer_not_expected")
        v = check_user_input(text, self.masker_for(sid))
        if not v.allow or v.masked_text is None:
            raise ServiceError("input_rejected", user_message(v.reasons))
        if meta.get("status") == "manual":                                                  # 초안 모드: 새 요청이면 그 문장만, 직전이 되묻기였으면 이어서 보낸다
            last = (self._thread(sid) or [{}])[-1]
            text_out = self._combined(sid, v.masked_text) if last.get("kind") == "clarify" else v.masked_text
            self._thread_add(sid, "user", v.masked_text)
            self._set_pipeline(sid, "generating")
            self.start_generation(sid, text_out)
            return
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
                    pv = self.store.latest_version(sid)
                    if pv is not None:
                        self.ap.apply_preapprovals(sid, pv)      # plan 확정 때 사전 승인된 절차가 순서 조건을 채우면 승인 상태로 올린다(D-50)
                self._persist(sid)
            out = {"ok": True, **(data or {})}
        except ApprovalError as e:
            out = {"ok": False, "code": e.code}
        except (StoreError, InvalidTransition) as e:
            out = {"ok": False, "code": e.code}
        except PlanInvalid as e:
            out = {"ok": False, "code": "plan_invalid", "codes": e.codes[:8]}
        except ScratchError as e:
            out = {"ok": False, "code": e.code, **({"codes": e.codes[:6]} if e.codes else {})}
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
        elif t == "scratch":
            self._member(sid, nick)
            return self.scratch.op(sid, nick, m)
        elif t == "reject_plan":
            self._reject(sid, nick, m)
        elif t == "propose_edit":
            self._propose_edit(sid, nick, _need(m, "text", str))
        elif t == "accept_proposal":
            return self._accept_proposal(sid, nick, _need(m, "proposal", str))
        elif t == "dismiss_proposal":
            self._dismiss_proposal(sid, nick, _need(m, "proposal", str))
        elif t == "send_to_apply":
            self._send(sid, nick)
        elif t == "catalog_search":
            self._member(sid, nick)
            return {"items": self.scratch.catalog_search(sid, _need(m, "query", str))}
        elif t == "suggest":
            self._member(sid, nick)
            return {"names": self.scratch.suggest(sid, _need(m, "option", str))}
        elif t == "lookup":
            self._member(sid, nick)
            return {"items": self.scratch.lookup(sid, _need(m, "kind", str), _need(m, "vpc", str, optional=True, default=""))}
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

    def _member(self, sid: str, nick: str) -> None:
        if self.store.role_of(sid, nick) is None:
            raise ApprovalError("not_a_participant")

    def _send(self, sid: str, nick: str) -> None:
        """연습장에서 확정한 절차만으로 plan을 만들어 Apply용으로 보낸다(요청자가 아닌 사람의 승인을 기다리는 새 plan 버전).
        `build_plan`의 전체 검증을 다시 거친다. 일시정지 중이면 부분 재계획(완료 절차 잠금)으로 보낸다."""
        self._member(sid, nick)
        sess = self.store.session(sid)
        if sess["state"] not in ("draft", "plan_review", "plan_approved", "paused"):
            raise ServiceError("send_not_allowed")
        plan = self.scratch.promote(sid, bool(sess["iac_managed"]))
        parent = self.store.latest_version(sid)
        if sess["state"] == "paused":
            version = self.replan.submit_replan(sid, nick, plan)
        else:
            version = self.ap.submit_plan(sid, nick, plan, parent_version=parent)
        self.scratch.sync_from_plan(sid, plan)
        self.store.set_meta(sid, "rejection", "null")                          # 새로 검토를 요청하면 지난 반려 코멘트는 거둔다(대화 기록에는 남는다)
        n = len(plan.all_steps())
        self._thread_add(sid, "assistant", f"검토를 요청했습니다(절차 {n}개, plan v{version}). 요청자가 아닌 확인자가 확정(일괄 승인)하면 apply를 진행할 수 있습니다.")

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
    def timeline(self, sid: str) -> dict:
        """작업의 주요 시각(초): 생성, plan 전송(검토 시작), plan 승인, apply 시작, 종료(완료·중단), 마지막 수정. 상태 전이 이벤트에서 읽는다(없으면 None)."""
        s = self.store.session(sid)
        t = {"created": s["created_at"], "updated": s["updated_at"], "plan": None, "approved": None, "apply": None, "ended": None, "rejected": None}
        for e in self.store.events(sid):
            if e["kind"] != "session_state" or "->" not in e["detail"]:
                continue
            new = e["detail"].split("->", 1)[1]
            if new == "plan_review" and t["plan"] is None:
                t["plan"] = e["ts"]
            elif new == "plan_approved":
                t["approved"] = e["ts"]
            elif new == "applying" and t["apply"] is None:
                t["apply"] = e["ts"]
            elif new in ("completed", "aborted"):
                t["ended"] = e["ts"]
            elif new == "draft" and e["detail"].split("->", 1)[0] in ("plan_review", "plan_approved"):
                t["rejected"] = e["ts"]                                                  # 반려(마지막)
        t["updated"] = max(x for x in t.values() if x)                  # 마지막 수정은 어떤 시각보다 앞설 수 없다
        return t

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
                        "participants": len(self.store.participants(sid)), "times": self.timeline(sid)})
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
                                 "request": show(s["masked_request"]), "title": show(s["title"]), "created_at": s["created_at"], "times": self.timeline(sid)},
                     "you": {"nick": nick, "role": self.store.role_of(sid, nick)},
                     "participants": self.store.participants(sid), "pipeline": show(meta("pipeline")), "version": v, "thread": show(meta("thread") or []),
                     "analyzing": meta("analyzing"), "proposing": bool(meta("proposing")), "rejection": show(meta("rejection")),
                     "proposal": (lambda p: show(p) if p and p.get("status") == "open" else None)(meta("proposal"))}
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
                "preapprovable": [s.id for s in plan.all_steps() if s.preapprovable], "preapproved": self.ap.preapproved_ids(sid, v),
                "preapprove_blockers": {s.id: list(s.preapprove_blockers) for s in plan.all_steps() if s.op == "mutate" and not s.preapprovable},
                "approvals": [{k: a[k] for k in ("kind", "target", "nick", "ts")} | {"detail": show(a["detail"])} for a in self.store.approvals(sid, v)],
                "outputs": show([o for o in self.store.outputs(sid, v)][-80:]),
                "review": show(review), "evidence": show(meta("evidence")),
            })
        sc = self.scratch.get(sid)
        status = self.scratch.validate(sid, sc)
        locks = self.scratch.locks(sid, sc)
        undo, redo = self.scratch.can_undo_redo(sid)
        out["scratch"] = show({
            "phases": [{**{k: val for k, val in ph.model_dump().items() if k != "steps"},
                        "gate": ph.gate or (any(status[x.id]["irreversible"] for x in ph.steps) and not ph.gate_waiver.strip()),      # 보이는 값 = plan이 될 값(D-64)
                        "gate_locked": any(status[x.id]["irreversible"] for x in ph.steps),
                        "steps": [{**x.model_dump(), **status[x.id], "locked": locks.get(x.id, "")} for x in ph.steps]} for ph in sc.phases],
            "undo": undo, "redo": redo, "dirty": self.scratch.dirty(sid),
            "confirmed": sum(1 for x in sc.steps() if x.confirmed), "total": len(sc.steps())})
        out["events"] = show(self.store.events(sid)[-60:])
        return out
