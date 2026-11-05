# IMPLEMENTATION.md

## 1. 가드 구현 (`app/guards/`)
처리 순서와 담당 모듈. 단계마다 판정을 `AuditLog`에 남긴다(입력 텍스트는 저장하지 않고 사유 코드·길이·HMAC 해시(설치별 키, 시크릿 차단 입력은 해시 없음)만).

| 단계 | 모듈 | 하는 일 |
|---|---|---|
| 1 사용자 입력 | `input_guard.check_user_input` | 길이·제어 문자, 전각·보이지 않는 문자 우회, 인젝션·가드 우회·승인 우회 표현, 시크릿 차단 → 통과하면 마스킹 |
| 0 마스킹 상태 보관 | `masking.Masker`, `mapping_store.SqliteMappingStore`, `keyfile` | 매핑은 세션 메모리에 두고, 재시작 후 이어가기용으로 세션별 AES-256-GCM 암호화 저장(세션 id를 AAD로 묶음, 키는 DB 밖 `mapping.key`, TTL 24시간, 종료 시 삭제, 복호화 실패는 `MappingUnreadable`로 중단). 저장소는 `MappingStore` 인터페이스(이후 Redis 교체 가능, D-29) |
| 3-0 LLM 호출 | `llm.LLMClient.complete` | 역할별 모델 선택, 발송 전 `assert_outbound_clean`, MAIN 쓰로틀링 시 재시도 2회(1초·3초) 후 SUB로만 폴백(다른 오류·SUB/READ는 `LLMUnavailable`로 중단), 토큰은 `AgentBudget`에 합산, 폴백은 감사 기록(`llm_fallback:MAIN->SUB`) (D-19) |
| 2 검색 | `rag.HybridRetriever.search` | 마스킹되지 않은 질의는 외부 호출 전에 거부 |
| 3 모델에 넘기는 외부 텍스트 | `untrusted.sanitize_untrusted`, `boundary.assert_outbound_clean` | 문서·도구 결과·실행 출력을 마스킹, 시크릿·지시문 의심 구간 제거, 데이터 틀로 감쌈. 경계 직전 누출 검사 |
| 4 에이전트 도구 호출 | `agent_limits.AgentBudget`, `agents.tools.ReadToolbox` | 읽기 전용 도구 허용 목록(`search_docs`, `lookup_inventory`, `run_read_command`), 호출·단계·토큰·시간 상한, 반복 감지. 도구 인자는 마스킹된 것만 받고(누출이면 오류), `run_read_command`는 `CommandGuard.check` → `assert_read_only_tool_call` → `assert_server_executable`을 모두 통과한 문서 근거의 읽기 명령만 주입된 `ReadCommandRunner`로 실행한다. `lookup_inventory`는 플레이스홀더만(prod 여부 없이) 돌려주고, 모든 도구 결과는 `sanitize_untrusted`를 거친다. 차단·실패는 코드만 담은 오류로 돌려준다 |
| 5 명령 검사 | `command_guard.CommandGuard.check` | 모델 출력과 사용자 직접 입력 step 공통. 폴백 포함. 결과는 `CommandVerdict` |
| 6 승인 화면 | (T6) | 서버가 복원한 명령과 `verdict.flags`·`requires` 표시 |
| 7 실행 | `execution_policy.assert_server_executable` | 이 관문을 통과한 명령만 서버가 실행 |
| 8 실행 결과 | `untrusted.sanitize_untrusted` | LLM으로 돌아가기 전에 마스킹·정리 |

`CommandVerdict.requires`가 승인 엔진(T6)에 요구하는 조치:

| 코드 | 승인 엔진이 해야 할 일 |
|---|---|
| `open_cidr_confirm` | 요청자의 사유 입력 + 승인자의 별도 확인 |
| `strong_warning` | 위험 플래그 목록을 강조해 표시하고 승인자 확인 |
| `dry_run_first` | 같은 대상의 dry-run step(`--dryrun`, `--dry-run=server`)이 앞서 있어야 함 |
| `prod_warning` | prod 대상 경고와 추가 확인(prod 판별은 서버 규칙) |
| `fallback_approval` | "AI 추정" 표시, 항상 다른 사람 승인 |
| `show_file_contents` | 정책 JSON·매니페스트 등 `local_paths` 파일 내용을 승인 화면에 그대로 표시 |

- `flags` 예: `open_cidr`, `wide_cidr`, `wildcard_principal`, `wildcard_action`, `admin_policy`, `recursive_delete`, `sync_delete`, `delete_all`, `namespace_delete`, `public_access_relax`, `public_acl`, `multiple_targets`, `prod_target`, `fallback`, `unknown_impact`, `iac_drift`.
- 차단 사유 코드(`blocks`)는 사용자에게 값을 되풀이하지 않는 안내로 바꿔 보여준다.
- 에이전트 한도 기본값, 금지 옵션 목록, 폴백 서비스 범위는 `docs/DECISIONS.md` D-23, D-26.
- **한계:** 시크릿·인젝션 탐지는 패턴 기반이다. 정책 분석은 Principal·Action 와일드카드와 관리형 관리자 정책 중심이고 조건·리소스 범위는 보지 않는다. 검사 시점과 실행 시점 사이에 작업 폴더의 파일이 바뀔 수 있어 실행기(T5)가 실행 직전에 경로를 다시 확인해야 한다. 인벤토리에 없는 리소스 이름은 마스킹되지 않는다.

### 명령 가드가 검사하는 것 (`command_guard.py`)
- **구조:** 셸 연산자(`;` `|` `&&` `>` 등), 명령 치환, 환경변수 확장, 여러 줄을 차단한다. `aws`와 `kubectl`만 허용한다.
- **범위:** 문서에 없는 명령(폴백)은 문서에 있는 AWS 서비스(ec2, eks, s3, s3api, iam, autoscaling, ssm)에서만 허용하고, `ec2 get-password-data`·`ssm send-command` 같은 작업은 거부한다. 실제 CLI에 없는 명령·옵션은 차단한다. 문서의 `optional`은 전체 목록이 아니므로 실제 도움말에서 만든 스냅샷(`data/cli_snapshot.json`)으로 검사한다. 문서에 없는 kubectl 명령은 내장 명령일 때만 도움말을 읽는다(플러그인 차단).
- **금지 옵션:** 대상·자격증명을 바꾸거나 숨은 입력을 읽는 옵션(`--profile`, `--endpoint-url`, `--kubeconfig`, `--context`, `--token`, `--as`, `--raw`, `--cli-input-json` 등). 대화형(`--stdin --tty`)은 `manual` 명령만 쓴다.
- **위험 패턴(차단 또는 강한 경고):** 전체 개방과 넓은 CIDR(`/7` 이하, 또는 사설 RFC1918이 아닌 `/16` 이하; 사설 서브넷은 제외), `kubectl config`의 전역 kubeconfig 변경(차단, `kubeconfig_mutation`), 와일드카드 Principal·Action, 관리형 관리자 정책, `s3 rm --recursive`·`rb --force`·`sync --delete`, `kubectl delete --all`·네임스페이스 삭제(`-A`는 차단), `scale --replicas=0`, 공개 ACL·퍼블릭 액세스 차단 해제, 여러 대상 종료, prod 대상(서버 규칙).
- **시크릿:** `kubectl get secret -o yaml/json/jsonpath`(파일 지정 포함)와 `kubectl create secret`을 차단한다. SSM은 `/aws/service/` 공개 경로만 읽고 복호화를 금지한다. 명령 안의 시크릿은 차단하며 `--password`는 `<PASSWORD>` 자리 표시만 허용한다. 실행 결과의 로그·presign URL 서명은 저장·LLM 전달 전에 제거한다.
- **종료되지 않는 명령:** `--watch`·`--follow`는 차단하고 `rollout status`·`drain`은 `--timeout`을 강제한다. 실행기에는 전체 타임아웃을 둔다(T5).
- **로컬 경로 샌드박스:** `file://`, `--filename`, `s3 cp/sync`의 로컬 경로는 세션 작업 폴더 안만 허용한다(`..`·절대 경로·`~`·URL·`-` 차단). 서버 파일의 읽기·쓰기·유출을 막기 위함이며 정책 파일은 읽어서 검사한다.
- **오류:** 예상치 못한 오류와 CLI 실행 불가는 모두 차단한다(fail-closed).
- `서버`·`노드`는 모호 표현이 아니다(항상 EC2 인스턴스, D-31). 모호한 표현(`방화벽` 등 `ambiguous`)은 가드가 아니라 접수 단계(`planner/intake.py`)에서 처리한다: 서버가 탐지하고, READ 모델이 요청 속 근거 인용으로 후보를 고르면 서버가 (후보 목록 안, 근거가 마스킹된 요청에 실재하고 별칭 자체가 아님, 확신 high)를 검증해 해소로 인정하고, 아니면 되묻는다. 입력 가드 차단은 모델 호출 전에 거절, 모델 출력은 누출 검사 후 사용, 스키마 오류는 2회 재시도 후 `SchemaViolation`(사람에게 넘김). 결과(`IntakeResult`)는 proceed/clarify/reject와 해석·질문 목록을 담는다(D-30).

### 앞 절차 결과 참조 (`app/guards/refs.py`, `ref_resolve.py`, D-48)
- `refs.py`(순수 문법): `REF_RE`=`@{절차id.경로}`, `find_refs`, `has_unparsed_ref`(문법에 안 맞는 `@{`), `with_dummy`(참조 자리를 `ref-0000000000000000`로). `CommandGuard.check`가 참조가 있으면 `with_dummy` 명령을 검사하고 판정 `command`는 템플릿 그대로, 플래그 `uses_output`을 더한다(문법 오류 `bad_reference`, 4개 초과 `too_many_references`는 차단).
- `ref_resolve.py`: `resolve(command, output_of, masker)`가 각 참조를 `extract_id`로 치환한다. 저장된 마스킹 출력을 JSON으로 읽어 경로의 값을 꺼내고(없으면 `ref:path_not_found`), 값이 아는 플레이스홀더이거나 리소스 ID 패턴(`SENSITIVE_PATTERNS["resource_id"]`)과 전체 일치하는 문자열일 때만 플레이스홀더로 돌려준다(아니면 `ref:value_not_resource_id`). `RefError`는 `ExecutionDenied`의 하위 클래스라 apply가 `denied:ref:<이유>`로 절차를 실패시킨다.
- `ApplyEngine._exec`가 `_resolved`로 참조를 풀고(참조한 절차 `done` + 성공 출력 필요, 이벤트 `step_ref_resolved`/`step_ref_failed`) 치환한 명령으로 `guard.check`와 `ExecutionRequest`를 만든다. `Executor.execute`는 `@{`가 남은 요청을 `unresolved_reference`로 거부하고, `ReadToolbox`의 `run_read_command`는 `references_not_allowed`, `preview_read`는 `preview_has_references`로 막는다.
- `build_plan`(`_check_refs`)은 참조를 `depends_on`에 더하고 `ref_unknown_or_forward`·`ref_target_not_runnable`·`ref_not_allowed_here`로 거부한다. 연습장 `validate`는 `uses`·`ref_fields`(`REF_HINTS`)와 이유 코드(`ref_unknown_step`·`ref_order`·`ref_target_not_runnable`·`ref_not_allowed_here`)를 내고 `_group`·`_unconfirm_closure`는 참조를 의존으로 본다.

### 계획 스키마 (`app/planner/schema.py`)
- 모델은 `PlanDraft`(plan → phase → step 초안)만 만든다. 초안에는 읽기/변경 분류, 위험, 근거, 폴백, 영향이 없고(필드를 넣으면 스키마 오류), 서버가 `build_plan`에서 `CommandGuard.check` 판정으로 채워 `Plan`을 만든다. step 종류는 `cli`·`manual`·`iac`·`check`·`poll`이다.
- 구조 규칙(위반 시 `PlanInvalid`, 코드 목록): 유일한 step id, `depends_on`·`check_for`는 앞선 step만 가리킴, 가드가 막은 명령 금지, 모델이 붙인 종류와 가드 판정 불일치 금지(예: `cli`인데 manual 명령), 변경 step마다 같은 phase 안에서 뒤따르는 확인 step(읽기), `poll`은 읽기 명령과 간격·최대 시간·완료 조건 필수, `iac`·`check`는 명령 없이 체크리스트, 모델 출력에 민감값이 있으면 거부, plan·phase당 step 상한.
- `irreversible` step이 있는 phase는 서버가 게이트를 켜고 잠근다(`gate_locked`). 근거 없는(폴백) 명령은 `grounded=False`이고 읽기/변경과 무관하게 다른 사람의 승인이 필요하다(`Step.needs_other_approval`). 분류가 애매한 명령은 가드가 변경으로 분류한다.
- 후처리(`postprocess.apply_postprocess`, D-33): 변경 step의 `impact.preflight` 읽기 step을 그 phase 맨 앞에 넣고(변경 명령의 옵션·대상에서 인자를 복사해 만들고 가드를 통과한 문서 근거의 읽기 명령일 때만, 못 만들면 `preflight_not_inserted` 경고), `iac_managed`이면 `impact.iac` 변경의 확인 step 뒤에 코드 동기화 확인 `iac` step과 `iac_drift`·`consider_iac_step` 경고를 더하고, `dry_run`은 `dry_run_available` 경고로 알린다. 더한 step은 `Step.auto`, 경고는 `Plan.warnings`이고 둘 다 해시에 들어간다. 더한 뒤 크기 상한을 다시 검사한다(`plan_too_large_after_auto_steps`).
- 대화 압축(`ToolLoop.stage(compact=True)`): 윤곽 단계와 phase 단계가 수락되면 그 단계의 중간 대화(검색 결과, 실패한 제출, 오류 결과)를 버리고 수락된 제출만 짧게 남긴다. 다음 요청에는 서버가 수락된 윤곽 요약, 앞선 phase의 step 요약, `search_docs`로 찾은 문서 명령 목록(`ToolLoop.docs_seen`)을 다시 준다. 호출마다 대화 전체가 다시 실리므로 이것이 없으면 입력 토큰이 phase마다 늘어난다.
- `plan_hash`는 사용자에게 보이는 모든 내용(제목, 게이트, 롤백 포함)과 step 해시의 순서를 묶는다(승인 잠금, T6). 실제 값 복원본의 확인은 승인 엔진(T6)이 맡는다.

### 계획 에이전트 (`app/planner/agent.py`, 프롬프트 `app/planner/prompts.py`, D-32)
- 구현 방식(D-52): LangGraph·LangChain 없이 `LLMClient`(Bedrock Converse 직접 호출)와 자체 `ToolLoop`로 만든다. LangGraph로의 이전은 Phase 2 supervisor 도입 때(견적 3~5일, 가드 관련 테스트와 실제 모델 측정값을 기준값으로 고정한 뒤 비교).
- 흐름: `IntakeResult`(proceed) → 읽기 도구로 조사(`search_docs`, `lookup_inventory`, `run_read_command`) → `submit_outline`(phase 윤곽) 또는 `request_clarification` → phase마다 `submit_phase` → 서버가 지금까지의 계획을 `build_plan`으로 검증하고 오류 코드를 돌려줘 수정(단계당 2회) → `PlanResult`(`plan` | `clarify` | `stopped`).
- 제출 통로 3개는 아무것도 실행하지 않는다(읽기 전용 도구 집합과 별개). 목록 밖 도구 이름, 단계에 맞지 않는 제출, 평문 답변(2회 재요청 후 중단)은 오류로 돌려주고 수정 기회를 소모한다.
- 중단 코드(`stopped.reasons`): `model_output_unsafe`(출력에 실제 값, 재시도 없음), `repair_exhausted`, `output_truncated`, `no_submission`, `llm_unavailable`, `leak_blocked`, 상한(`steps_exceeded`, `tool_calls_exceeded`, `repeated_call`, `tokens_exceeded`, `time_exceeded`). 판정은 감사 기록에 남고 입력 텍스트는 없다.
- `LLMClient.chat`: 여러 턴·도구 호출 대화. 발송 전에 시스템·메시지·도구 결과·도구 입력·도구 정의 전체를 누출 검사하고, MAIN→SUB 폴백과 토큰 합산은 `complete`와 같은 경로다. 출력 한도 8,192 토큰.
- 시스템 프롬프트는 역할·입력(마스킹)·고정 사실(서버·노드=EC2, Pod, 운영 환경)·도구와 한도·작업 순서·step 규칙·서버 오류 코드·출력 형식으로 구성된다. 금지 옵션, `--timeout` 필수 명령, 한도, step 상한은 가드 상수에서 채우고 테스트가 대조한다. manual 명령 목록은 문서와 대조한다.

### 리뷰어 (`app/planner/review.py`, D-35)
- 서버 규칙(`build_review`): phase별 체크리스트(`irreversible`, `disruption_likely/possible`, `replaces`, `no_gate`, `no_rollback`, `no_preconditions`, `fallback_step`, `risk_flag`(HIGH_FLAGS), 정보성 `manual_step`·`iac_step`·`auto_step`)와 위험 등급(high: 되돌릴 수 없음·위험 플래그·폴백 step, medium: 그 밖의 경고나 후처리 경고, low). 모델은 관여하지 않는다.
- 요약(`Reviewer.review`): 서버가 만든 사실 JSON만 SUB 모델에 주고 headline·summary·attention·phase_notes를 받는다. 스키마 검증(2회 재시도), 누출·모르는 플레이스홀더 검사를 통과해야 하고, 실패하거나 모델을 쓸 수 없으면 요약 없이 체크리스트만 돌려준다. 모델은 등급·체크리스트를 바꿀 수 없다. 화면(T8)은 서버 체크리스트와 등급을 요약과 함께 보인다.
- `step_evidence(step, chunks)`: 근거 있는 step은 근거 문서 텍스트·경로, 근거 없는 읽기/변경 step은 "AI 추정" 표시.

### 마스킹·패턴의 한글 조사 경계
- 파이썬의 `\b`·`\w`는 한글을 단어 문자로 보아 값 뒤에 조사가 붙으면(`sg-0123…를`, `123456789012의`, `AKIA…를`) 경계가 사라져 마스킹·시크릿(JWT)·인젝션(한글에 붙은 영어 구문) 패턴을 피해 갔다(2026-11-04 발견, 리뷰어 실측 중 `0.0.0.0/0으로`에서 드러남). `patterns.py`는 모든 패턴의 `\b`를 ASCII 경계로 바꿔 컴파일하고, ARN은 ASCII 문자까지만 잡아 조사를 남긴다. 회귀 테스트는 `tests/test_masking.py`·`tests/test_input_guard.py`, 검색 문서에 값이 없는지 확인하는 테스트는 `tests/test_docs_sync.py`.

## 2. 검색 구성 (구현: `app/rag/`, 진입점 `app/rag/factory.py`)
- **chunk:** 명령 하나(141개). 텍스트에 문서 제목, 실행 형태, 종류·위험도·영향 요약, 본문(설명·키워드·예시·주의)을 넣고 메타데이터(cli, service, kind, risk, guard)를 함께 저장한다.
- **BM25:** kiwipiepy 한국어 형태소 + 영문 CLI 복합 토큰(`create-security-group`, `--group-id`). 플레이스홀더는 제거한다.
- **벡터:** Bedrock Titan v2(`EMBED_MODEL`) + Chroma(`data/chroma`). 문서 해시가 바뀐 chunk만 다시 임베딩한다.
- **결합과 리랭크:** RRF로 결합하고, READ 모델(LLM)이 후보 20개를 다시 정렬한다(실패하면 원래 순서 유지). `ref/`의 크로스인코더(bge-reranker)는 torch와 약 2GB 모델이 필요해 쓰지 않았다. 지연이나 비용이 문제되면 대안이다.
- **입력:** 마스킹된 질의만 받는다. 마스킹되지 않은 민감값이 보이면 외부 모델(임베딩·리랭크)을 호출하기 전에 거부한다(`SensitiveQueryError`, 값은 출력하지 않음). 임베딩 호출 오류는 계정 정보가 섞일 수 있어 길이를 잘라서 올린다.
- **용어 사전:** 질의 확장은 기본으로 끈다. 평가에서 BM25는 확장하면 나빠지고(문서의 키워드 줄이 이미 동의어 역할), 벡터·하이브리드는 이득과 손해가 엇갈려 차이가 케이스 2~3개 수준이었다. 모호 표현 탐지와 되묻기 후보 제공에 쓴다.
- **평가**(`eval/eval_retrieval.py`, 80 케이스, 2026-11-04). 평가셋을 문서 작성자가 만들어 낙관적일 수 있다. 사용자 실제 표현이 쌓이면 보강한다.

| 방식 | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|
| BM25 | 0.74 | 0.94 | 0.97 | 0.836 |
| 벡터(Titan) | 0.65 | 0.85 | 0.91 | 0.762 |
| 하이브리드(RRF) | 0.78 | 0.96 | 0.97 | 0.870 |
| **하이브리드 + 리랭크(기본)** | **0.99** | **1.00** | **1.00** | **0.994** |

- 지연: 벡터 질의 약 0.25초, 리랭크 약 1초.
- 한계: 이 계정에서는 Cohere 임베딩과 Bedrock 리랭크 모델에 권한이 없다(2026-11-04 확인).

### 계획 생성 흐름 (`app/planner/pipeline.py`)
- `PlanPipeline.run(text)`: 접수(`Intake`) → 계획 에이전트(`PlanningAgent`) → 리뷰어(`Reviewer`) → step별 근거(`step_evidence`)를 이어 붙이고 결과를 네 상태로 모은다: `reject`(입력 가드 차단·범위 밖, 안내문 포함), `clarify`(접수의 모호 표현 또는 계획 에이전트의 되묻기), `plan`(검증된 Plan, 리뷰어 체크리스트·위험 등급·요약, 근거), `stopped`(모델·한도·검증 때문에 중단, 사람에게 넘김). 리뷰어 실패는 계획을 막지 않는다. 승인 엔진(T6)이 이 결과를 받는다. 이 모듈은 아무것도 실행하지 않는다.
- 접수(`intake.py`, D-30·D-38): 입력 가드 → 마스킹 → READ 모델(범위·서비스·의도·정규화 질의·모호 표현 해소, 구조화 출력) → 서버 검증. 필수 값 누락은 묻지 않고(계획 에이전트가 확인 후 묻는다), 모델의 범위 밖 판정은 서버가 인프라 근거(마스킹된 플레이스홀더, 용어 사전 표현)를 보면 `scope_override`로 뒤집는다.

### 실패 분석 에이전트 (`app/planner/failure.py`, D-37)
- 입력: 실패한 step(종류, 읽기/변경, 마스킹된 명령, 영향·플래그, 되돌릴 수 없음 여부), phase, 의존·확인 step, 시도 횟수, 서버 분류(`server_hint`), 실행 결과(stderr·stdout을 `sanitize_untrusted`로 마스킹·시크릿 제거·지시문 제거·데이터 틀 처리). 모델은 SUB, 도구는 읽기 도구 3개와 제출 통로 `submit_analysis`(계획의 `request_clarification`, `submit_phase`는 없음).
- 서버 분류(`classify_error`): throttled, permission, quota, dependency, already_exists, not_found, timeout, conflict, invalid_argument, unknown(정규식, 앞의 것이 우선).
- 추천 좁히기: `retry`는 서버·모델 분류가 모두 일시적(throttled, timeout, conflict, unknown)이고 시도 3회 미만일 때만. `skip`은 뒤 step이 의존하지 않을 때만. 좁혀지면 대안이 있으면 `replan`, 없으면 `abort`이고 `adjustments`에 이유 코드(`retry_not_allowed:<분류>`, `retry_limit_reached`, `skip_has_dependents:<ids>`, `irreversible_step`). 변경 step이 timeout·unknown이거나 되돌릴 수 없으면 `needs_state_check`, 계획의 확인 step id는 `check_step_ids`.
- 대안 step: `alt-` 접두사, 계획의 id와 충돌 금지, 자기 완결. 제출될 때마다 `build_plan`(복구용 phase 하나)으로 검증하고 오류 코드를 돌려줘 고치게 한다(수정 기회 2회). 통과한 대안은 후처리(사전 점검 삽입, IaC 동기화)까지 거친 `Step`으로 `FailureAnalysis.alternatives`에 담기고 실행되지 않는다.
- 중단 처리와 대화 루프는 계획 에이전트와 같다(`toolloop.ToolLoop`: 읽기 도구, 수정·재요청 한도, 누출 시 중단, 출력이 잘리면 재요청, `compact`로 단계가 끝나면 중간 대화 압축). 프롬프트는 계획 프롬프트의 "고정 사실", "step 규칙", "서버 오류 코드" 절을 재사용한다.

### 실행기 (`app/executor/`, D-36)
- **인터페이스(`base.py`):** `ExecutionRequest`(요청 id, 세션·plan·step id, 종류, `masked_command`, 실제 값 `command`, `command_digest`, 타임아웃)와 `ExecutionResult`(상태 ok/failed/timeout, 종료 코드, stdout·stderr 원문, 소요 시간, `mock`, 실행기 이름, notes)는 `to_wire`/`from_wire`로 직렬화된다. `Executor.execute(request, verdict=, approved_by_other=, confirmations=)`가 유일한 실행 경로다: `assert_server_executable` → 승인된 내용과 같은지(마스킹 문구·종류, 다이제스트) → 실행하는 쪽 가드 재검사(복원된 명령) → `_run`. 서브클래스가 `execute`를 재정의하거나 `_run`을 다른 곳에서 호출하면 테스트가 실패하고, 프로세스를 띄우는 모듈은 `executor/process.py`·`moto_server.py`·`guards/cli_snapshot.py`로 제한된다.
- **프로세스(`process.py`):** 셸 없이 가드와 같은 토큰화(`_split`)로 실행한다. 환경은 비어 있는 상태에서 PATH(실행 파일 탐색)·Windows 필수 변수와 요청별 임시 HOME·AWS 설정 경로·KUBECONFIG, 호출자가 준 값만 채운다. 끝나면 임시 폴더를 지운다. 타임아웃 시 프로세스를 죽이고 출력은 100만 자로 자른다.
- **mock(`mock.py`):** aws 명령은 시뮬레이터(`simulator.py`)가 맡는 것이면 응답을 만들고, 아니면 루프백 moto 서버(`moto_server.py`)로 보낸다. `eks describe-cluster`는 moto 응답의 버전·상태를 시뮬레이션 상태로 덮어쓴다. kubectl은 고정 응답(`kubectl_fixture.py`). 공유 상태는 `world.py`의 `MockWorld`. `seed()`가 moto에 기본 리소스(클러스터, ASG, launch template, 버킷, 합성 이름)를 만든다.
- **시뮬레이터:** `eks update-cluster-version`(+`describe-update`, `list-updates`), `list-addons`·`describe-addon`·`update-addon`·`describe-addon-versions`·`list-insights`, `autoscaling start/describe/cancel-instance-refresh`, EKS AMI SSM 파라미터. 진행은 조회 3회째에 완료이며 컨트롤 플레인·노드 버전이 `MockWorld`를 거쳐 kubectl 응답에 보인다.
- **연결:** `adapter.ReadRunnerAdapter`가 에이전트 읽기 도구의 `ReadCommandRunner`를 구현한다(도구 가드 + 실행기 관문 + 실행기 재검사, 실패 시 오류 출력을 돌려줘 모델이 원인을 본다). `factory.build_executor`는 `EXECUTOR_MODE`(기본 mock)만 지원한다.

## 3. 세션·승인·웹 (`app/session/`, `app/web/`, D-39)
- **상태(`states.py`):** 세션 `draft → plan_review → plan_approved → applying ⇄ paused → completed | aborted`(`paused → plan_review`는 부분 재계획), step `pending → approved → running → done | failed`, manual·iac는 `approved → awaiting_report → awaiting_confirm → done`, 실패 후 `failed → pending(재시도) | skipped | aborted`. 표에 없는 전이는 `InvalidTransition`.
- **저장(`store.py`, SQLite):** 세션, 참여자(멤버 토큰 해시), plan 버전(JSON + 해시, 읽을 때 재계산, 다르면 `PlanTampered`), step 상태(시도 횟수, `expect`로 전이 선점), 승인(추가만, 같은 사람·대상 중복 무시, 재시도 시 무효 처리는 이벤트로 남김), 실행 결과(마스킹·시크릿 제거 텍스트만), 이벤트(추가만), 세션 부가 정보(리뷰·근거 JSON). 저장되는 텍스트는 모두 마스킹된 것이다.
- **승인(`approval.py`):** `submit_plan`, `approve_plan`, `start_apply`, `approve_step`(→ `StepGrant` 또는 manual·iac는 None), `approve_gate`, `provide_reason`, `confirm_requirement`, `auto_approve_check`, `grant_for`(승인 기록에서 재계산), `pending_requirements`, `reset_step_approvals`. 모든 허용·거부는 `AuditLog` stage `approval`에 사유 코드로 남는다.
- **apply(`apply.py`):** `run_step`(승인 자격 재계산 → `running` 선점 → `Executor.execute` → 결과 정리·저장 → 변경이면 확인 step 자동 실행), 실패 시 세션 `paused` + 분석기(선택, 호출 실패는 무시), `decide`(재시도·건너뛰기·중단 규칙), `report_done/failed`·`confirm_report`, `poll_once/poll_complete/run_poll`, `recover_after_restart`, `preview_read`(plan 검토 중 문서 근거 읽기 명령만).
- **재계획(`replan.py`):** `ReplanEngine`이 일시정지와 완료 step 잠금·상태 승계·게이트 승인 이월을 맡는다. 옛 `PlanEditor`(`edit.py`)는 쓰지 않는다(plan은 연습장에서 `promote`로만 만들어지며 `edit` 메시지는 없다, D-47).
- **연습장(`scratch.py`, D-47):** `ScratchPad`가 세션 부가 정보 `scratch`(JSON: 단계 `DPhase`·절차 `DStep`·`suppressed`)에 초안을 저장한다. 연산: `add_step(s)`(빈 명령도 허용)·`update_step`·`delete_step`·`move_step`(단계 사이 이동, `after`=바로 앞 절차)·`duplicate_step`·`set_confirmed`·`confirm_valid`·`add/update/delete_phase`·`undo/redo`(이력 30개, `scratch_hist`·`scratch_redo`). `validate`가 절차마다 `ok|warn|err`(화면은 ✔/⚠/⛔)와 이유 코드(`command_required`, `blocked`, `missing_param`, `missing_check`, `order`, `unknown_ref` 등)를 계산하고(명령 가드를 부르되 거부하지 않고 표시만), 확정은 `err|warn`이 없고 묶음(의존·`check_for`·변경↔확인)이 함께일 때만 된다. `promote`가 확정 절차를 `build_plan(..., suppressed=, frozen=)`으로 승격하며(전체 검증 다시) `service._send`가 `submit_plan`(재계획이면 `submit_replan`) 뒤 `sync_from_plan`으로 서버가 더한 절차를 연습장에 반영한다. `locks`는 완료·건너뜀과 apply 중인 확정 절차를 잠그고, `dirty`는 확정 목록이 보낸 plan과 다른지 비교한다(다르면 plan 승인·apply 진행이 막힘). `import_plan`(에이전트 plan → 확정된 초안)과 `import_as_drafts`(빈 plan·연습장에 절차가 있을 때 → 확정하지 않은 초안, id 충돌은 이름 바꿈), `catalog_search`(BM25, `Chunk.required/optional`, 빌더 전용 힌트 `BUILDER_REQUIRED`), `suggest`(인벤토리 이름), `lookup`(vpc·subnet·sg는 읽기 명령, ami_ubuntu·ami_al2023은 SSM 공개 파라미터를 `ReadRunnerAdapter`로 실행), 
- **말로 수정(`planner/scratch_edit.py`, D-47):** `ScratchEditor.propose`가 SUB 모델에 마스킹된 연습장 요약(`<scratchpad>`)과 요청(`<user_request>`)을 보내 JSON 수정안을 받는다. `validate_structured`로 스키마(허용 연산 5개, 최대 8개, 확정·승인·실행 연산 없음)를 검증하고 `clean_ops`가 있는 절차 id·허용 필드(title·command·rationale)·kind(cli|manual)를 확인하며, 글 전체를 `leak_kinds`와 `unresolved`(지어낸 플레이스홀더)로 검사해 어긋나면 수정안을 버린다. `service.propose_edit`(스레드 `edit-…`, 입력 가드·마스킹, 이미 처리 중이면 `busy`)이 `proposal` 부가 정보와 대화 `kind=proposal`을 남기고, `accept_proposal`은 연산을 `ScratchPad.op`로 차례로 적용하다 실패하면 이미 적용한 만큼 `undo`한다(전부 아니면 전무). `dismiss_proposal`은 아무것도 바꾸지 않는다. 스냅샷에 `proposal`(열린 것)·`proposing`이 나간다.
- **서비스(`service.py`):** 세션별 `Masker`(재시작 시 `mapping_store`에서 복원, 읽을 수 없으면 중단, 종료·취소 시 삭제), 메시지 종류·필드 검증(`handle`), 화면용 스냅샷(`view`, 여기서만 실제 값 복원), 계획 생성·poll 백그라운드 스레드, 리스너 알림.
- **대화로 하는 진행 요청(D-49):** `intents.parse_intent(text, step_ids)`가 낱말 규칙으로 `Intent(kind, step_hint)`를 돌려준다(고치는 말은 None). `service._propose_edit`이 입력 가드 뒤 먼저 의도를 보고, 있으면 `_propose_action`→`_action_for`가 상태별로 `action`(정식 메시지 필드 + 표시용 `label`·`who`)을 만들어 `proposal` 부가 정보에 둔다. `_accept_proposal`은 `action`이 있으면 `label`·`who`를 뺀 메시지를 누른 사람의 닉네임으로 `_dispatch`한다(예외는 그대로 올라가 카드가 남는다). 화면은 `actionCard`(실행·무시)와 Apply 탭의 `todoPanel`, 줄의 `todoChip`, 상세의 필요한 조치별 담당 표시(`stepTodo`)를 그린다.
- **사전 승인(D-50):** `Step.preapprove_blockers`(이유 코드: `not_grounded`·`high_risk`·`irreversible`·`disruption_*`·`replaces`·`needs_confirmation`·`flag:*`·`not_server_change`)가 비면 `preapprovable`. `ApprovalEngine.approve_plan`이 대상 절차의 `step` 승인을 `preapproved_with_plan`으로 기록하고, `preapproved_ids`가 현재 해시의 사전 승인을 돌려주며, `apply_preapprovals`가 순서 조건(앞 phase·의존·게이트·요구 조치)을 채운 절차만 `approved`로 올린다(서비스 `handle`이 apply 중 매 동작 뒤 호출). 스냅샷의 `preapprovable`·`preapproved`·`preapprove_blockers`로 화면이 확정 전 카드와 줄 표시를 그린다.
- **반려(D-51):** `ApprovalEngine.reject_plan`(요청자 아님, `plan_review`·`plan_approved`, 최신 버전·해시 일치, 있는 절차만)이 plan을 `rejected`·세션을 `draft`로 바꾸고 `service._reject`가 코멘트를 입력 가드·마스킹해 `rejection` 부가 정보와 대화(`kind=reject`)에 남긴다. `_send`(다시 검토 요청)가 `rejection`을 거두고 `timeline`에 `rejected`가 들어간다. 화면은 `rejectPanel`(확인자), 배너·줄 아래 💬(요청자)을 그린다.
- **작업 시각(D-47 보완):** `service.timeline(sid)`가 `session_state` 이벤트에서 `created·plan(→plan_review)·approved(→plan_approved)·apply(→applying, 처음)·ended(→completed|aborted)·updated`를 돌려주고(`updated`는 최댓값) `history`의 `times`와 `view.session.times`로 나간다. 화면은 `dedupeHistory`로 같은 세션을 한 줄로 묶고(`loadHistory`) `timesLine`으로 시각 줄을 그린다.
- **작업 이력·초대(D-43):** `service.history`가 토큰으로 인증된 세션의 제목·상태·절차 상태 수·위험 등급을 모아 최근 갱신 순으로 돌려준다(제목은 가능하면 실제 값으로 복원). 세션 제목은 만들 때 필수로 받아 마스킹해 저장한다. `store.rotate_invite`는 새 초대 토큰을 만들고 이전 해시를 덮어쓴다.
- **대화 맥락(D-46):** `service._combined`가 첫 요청, 에이전트의 되묻기 질문(`kind=clarify`로 대화 기록에 남음), 사용자의 답을 `[이전 질문]`·`[추가 정보]`로 이어 붙여 계획 생성에 보낸다(길면 오래된 질문부터 뺌, 입력 횟수 상한 6). `Glossary`의 모호 항목은 `exclude`에 적은 복합어(ssh키, 키 페어 등)를 제거한 문장에서만 일치를 본다. `service.history`는 `participants`(작업자 수)를 포함한다.
- **가드 완화(D-45):** `agent_limits.RepeatedCall`(`MAX_REPEAT_NOTICES`=2)은 반복 호출을 `ReadToolbox.call`에서 오류 결과(`repeated_call` + 안내)로 바꾸고, 넘으면 `BudgetExceeded`로 중단한다. `patterns.PUBLIC_ACCOUNT_IDS`의 12자리는 `account_id` 패턴의 부정 선행 검사로 제외한다. `ToolLoop`는 글(텍스트) 누출은 즉시 중단하고, 도구 호출 인자의 누출은 그 호출을 실행하지 않고 `tool_input_contains_sensitive_value` 오류로 돌려주며 대화 기록의 입력은 `{}`로 비운다(수정 기회 `max_repairs`).
- **실패 분석의 대화 표시(D-45):** `ApplyEngine`이 실패(`failed`)·분석 끝(`analysis`) 이벤트를 `on_event`로 알리면 서비스가 대화에 에이전트 메시지를 싣고 분석 중에는 `analyzing`(절차 id)을 설정한다. 스냅샷의 `analyzing`이 있으면 화면이 "답변을 생성 중입니다"를 보인다. `run_read` 메시지는 읽기 절차에 `approve_step`과 `run_step`(poll은 `start_poll`)을 이어서 호출한다.
- **중단 안내와 다시 시도(D-44):** `pipeline.stop_message(code, detail)`가 중단 원인을 한국어로 바꾼다(반복된 도구 이름, 누출 종류 이름만 덧붙이고 값은 없다). `PlanResult.detail`·`PipelineResult.detail`·`PlanInvalid.detail`·`LoopStop.detail`·`BudgetExceeded.detail`이 이 상세를 나르고 감사 기록에는 `agent_stopped:<코드>:<상세>`로 남는다. 서비스의 `retry_generation`은 `stopped` 상태의 세션에서 요청자만 새 입력 없이 같은 내용으로 다시 시도한다("다시 시도" 표시는 모델로 가는 문장에서 빠진다).
- **대화(D-42):** `service._answer`가 되묻기·중단·거절 뒤의 추가 입력을 받아(요청자만, 입력 가드·마스킹) 마스킹된 요청에 `[추가 정보]`로 이어 붙여 계획 생성을 다시 시작한다. 대화 기록(`thread`)과 `generating` 상태는 세션 부가 정보에 저장되고, 스냅샷의 `thread`·`session.title`(마스킹된 제목)로 나간다.
- **웹(`app/web/`):** `server.py`(FastAPI, REST 3개 + WebSocket `/ws/{sid}`, 첫 메시지 인증, Origin 검사, 상한), `factory.py`(실제 Bedrock + mock 실행기 구성, 세션마다 마스커·LLM·예산 분리), `main.py`(`python -m app.web.main`, 기본 127.0.0.1:8000).
- **WebSocket 메시지:** `approve_plan`, `start_apply`, `approve_step`, `provide_reason`, `confirm_requirement`, `approve_gate`, `run_step`, `start_poll`, `poll_complete`, `report_done`, `report_failed`, `confirm_report`, `decide`, `preview_read`, `pause`, `abort`, `scratch`(연습장 연산), `send_to_apply`, `catalog_search`, `suggest`, `lookup`, `answer`, `retry_generation`, `create_invite`, `run_read`, `propose_edit`, `accept_proposal`, `dismiss_proposal`(옛 `edit`는 `unknown_message`). `id`를 붙인 요청의 응답은 `{"type":"result","id":...}`에 조회 데이터를 합쳐 요청한 쪽에만 처리된다. 응답은 `{"type":"result","ok":...,"code":...}`, 상태는 `{"type":"view","data":...}`로 모든 연결에 보낸다.

### 화면 (`app/static/index.html`, D-41)
- 빌드 도구 없는 단일 HTML. 서버는 `GET /`로 내려주고 CSP(`default-src 'self'`)와 `nosniff`를 붙인다. 외부 리소스·CDN을 쓰지 않는다.
- 렌더링은 작은 DOM 도우미(`h()`)로만 한다. 값은 텍스트 노드로만 들어가므로 모델 출력·실행 결과에 HTML이 있어도 해석되지 않는다(`innerHTML` 등 사용 금지를 테스트가 확인). 서버가 보내는 스냅샷(`view`)을 매번 통째로 다시 그리고, 입력 중에는 새 스냅샷을 보류했다가 포커스를 잃으면 그린다(입력 내용이 사라지지 않게).
- 구조(D-43): 세로 3분할. 좌측(`＋ 새 작업`, 작업 이력, 작업자, 초대 링크 만들기), 중앙(제목·상태·위험 등급, plan 승인, 리뷰, 절차 목록, 활동 로그는 접어 둠), 우측(대화와 입력창). 입력창은 상태에 따라 요청(새 작업, 제목은 중앙에서) / 답하기(되묻기·중단·거절 뒤, 요청자만) / 비활성(생성 중, 계획 검토 이후)이다. 1000px 이하에서는 상단 탭(작업·절차·대화)으로 바뀐다.
- 화면 개정(D-46): 이력은 한 줄(제목)과 오른쪽 끝 상태 점(`dot plan|live|livewait|paused|done|aborted`, live는 깜빡임)이며 상세는 툴팁이다. 절차 줄 이름 옆에 승인(☐→☑)·실행(▶→⟳→✔) 아이콘을 두고 읽기 절차는 ▶ 하나(`run_read`)다. 상태 글자는 아이콘이다(✔ 완료, ✖ 실패, ↷ 건너뜀, ⊘ 중단됨, ⟳ 진행 중, ✎ 보고 작성, ⏳ 확인 대기). 펼친 상세에는 입력이 필요한 것만 남는다. Apply 탭은 apply를 진행한 뒤(applying·paused·completed)에만 있다. Apply 탭 아래에 터미널 형태의 실행 로그(`terminalPanel`: 시각·주체·절차·결과, `$ 명령`, 출력)가 있고 `column-reverse` 스크롤로 최신이 보인다.
- Plan / Apply 탭(D-45): Plan 탭은 검토·수정·승인(`plan 일괄 승인`, 승인 뒤 `apply 진행 →`), Apply 탭은 plan 승인 뒤에 생기고 같은 절차를 수정 없이(드래그·삽입·편집 없음) 승인·확인·실행한다. apply가 시작되면 모든 참여자의 화면이 Apply 탭으로 넘어간다. 모든 절차 줄 앞에 실행 주체 표시 `[AGENT]`(서버가 실행: 읽기·변경·폴링) / `[MANUAL]`(사람이 실행: 수동 명령·IaC·수동 체크)가 있다. 읽기 절차(문서 근거, 추가 조치 없음)는 `확인` 한 번(`run_read`)으로 진행한다. 작업자 목록과 초대 링크는 제목 아래에, 대화 헤더에는 작업 제목을 둔다.
- 연습장 화면(D-47): Plan 탭은 `planView`(툴바: `＋ 직접 입력`·`＋ 명령 만들기`·`＋ 단계`·`확정 가능한 것 모두 확정`·↶/↷·`Apply로 보내기 (확정 N개)`), 보낸 plan 카드(`sentPlanCard`: 일괄 승인·apply 진행, 연습장이 달라졌으면 비활성), 빌더(`builderPanel`: 문서 검색 → 옵션 입력칸·조회 버튼 → 미리보기 → 확인 명령까지 한 번에 추가), 단계별 `dphaseView`(단계 이름은 입력칸)·줄 `dstepRow`(체크박스 확정 — 막혀 있어도 눌리고 이유를 알려 주며 줄 아래에 이유가 보임, ✔/⚠/⛔, 절차 이름 더블클릭 수정, 드래그로 단계 사이 이동, `＋`는 그 뒤에 명령 만들기, `✎＋`는 그 뒤에 직접 입력)·상세 `dstepDetail`(제목·명령·이유·의존·확인 대상 수정, 빠진 필수 옵션 값 고르기, 복제·삭제)이다. 연습장은 에이전트가 되묻거나 멈춘 중에도 열려 있다. 대화는 수정안 카드(`proposalCard`: 연산 목록, 수락·무시)와 보낸 입력의 즉시 표시(`S.echo`: 내 글 + "처리 중", 서버 상태가 오거나 거부되면 거둠)를 갖고, 입력창은 쓰는 글이 없으면 서버 갱신을 보류하지 않는다(`typing()`). 입력창 모드: 요청 / 답하기·초안 요청 / 수정안 만들기(연습장에 절차가 있을 때) / 비활성. 아래 "절차 목록" 설명 중 같은 phase 안 드래그·`add_steps`·`edit` 관련 내용은 Apply 탭과 옛 구조 기준이며 Plan 탭은 이 항목이 우선한다.
- 절차 목록: 한 줄씩 접힌 행(⋮⋮ 손잡이, id, 제목, 배지, 상태)이고 눌러서 상세·수정·승인 버튼을 펼친다. apply 중에는 처리할 차례인 절차를 자동으로 펼친다. 계획을 수정할 수 있는 동안(plan 검토·승인 후·일시정지)에만 행이 `draggable`이고, 같은 phase 안에서 대상 행 앞(또는 phase 끝 영역)에 놓으면 `move_step`(`after` = 바로 앞 절차 id, 맨 앞이면 null)을 보낸다. 다른 phase로는 옮길 수 없다. 절차 추가는 유형(CLI·수동·IaC·체크)과 선택적 확인 명령을 받아 `add_steps`로 한 번에 보낸다.
- 브라우저에는 자격(세션 id, 닉네임, 멤버 토큰)만 `localStorage`의 `creds`에 둔다. 이력의 내용(제목, 상태, 위험 등급, 절차 결과 요약)은 `POST /api/history`로 서버 DB에서 읽는다(토큰이 맞는 항목만 내용을 주고, 아니면 `valid: false`, 서버에 없는 세션의 자격은 브라우저가 지움). 탭이 보고 있는 항목은 `sessionStorage`에 둔다. 그래서 탭 두 개를 다른 닉네임으로 열어 4-eyes를 확인한다. 초대 링크(`/?s=...&i=...`)는 요청자가 `create_invite`로 그때그때 만들고(해시만 저장, 새로 만들면 이전 링크 무효), 링크로 들어오면 닉네임만 입력해 참여한다.
- 버튼의 활성·비활성은 안내일 뿐이다(예: 요청자에게 plan 승인이 꺼짐). 실제 거부는 항상 서버가 한다. 서버가 거부하면 코드를 한국어로 바꿔 토스트로 보인다.
- 테스트: `tests/test_ui.py`(정적), `tests/test_ui_render.py` + `tests/ui_harness.js`(node의 가짜 DOM에서 실제 서비스 스냅샷을 그려 버튼 상태와 전송 메시지를 확인, 브라우저가 아니므로 레이아웃·스타일은 확인하지 못한다).
- 실제 브라우저 확인(수동, pytest 아님): `python tests/browser/serve_fake.py`(가짜 파이프라인·스크립트 실행기, 포트 8124)를 띄우고 `node tests/browser/cdp.js <폴더>`가 Chrome(DevTools Protocol, 외부 패키지 없음)으로 새 작업(제목 없이 보내면 막힘) → 되묻기 → 답하기 → plan → 절차 추가 → 실제 DragEvent로 순서 변경 → 초대 링크 만들기 → 다른 탭 참여·승인 → apply 시작 → DB 이력 조회 → 모바일 폭까지 조작하며 스크린샷을 남긴다. Chrome 경로는 `CHROME_PATH`로 바꾼다. 이 확인에서 `after: null`을 서버가 거부하는 버그를 발견해 고쳤다(가짜 DOM 테스트는 보내는 메시지만 보므로 놓쳤다).

## 4. 관측(Trace)과 평가
### 관측
- 백엔드: `TRACE_BACKEND=off|local|langfuse`. 기본은 `local`(SQLite 이력과 연결된 로컬 트레이스). Langfuse는 **개발 단계 한정**이고 명시적으로 켠 경우에만 쓴다(사용자 결정 2026-11-04). self-hosted를 권장하고 cloud는 개발 한정이다. 운영 전환 전에 재검토한다(Phase 3 인증과 함께).
- **트레이스 경계:** 트레이스에는 마스킹 경계 안쪽 데이터(LLM이 보는 것)만 기록한다. 원본 사용자 입력, 복원된 실제 값, 마스킹 매핑, 실행 결과 원문, `.env` 값은 어떤 백엔드에도 보내지 않는다. 트레이싱은 마스킹 이후의 LLM 호출 래퍼와 마스킹된 도구 결과에만 붙이고, 요청·세션 최상위 span에는 입력 원문을 넣지 않는다(세션 ID, 길이 같은 메타만).
- **export 전 누출 스캔:** 외부(Langfuse)로 보내기 전에 마스킹 누출 스캔을 거친다. 누출이 의심되면 export를 막고(fail-closed) 로컬에만 기록하며 경고한다.
- 기록 항목: LLM 호출(역할 MAIN/SUB/READ, 토큰, 지연, 비용, 마스킹된 입출력), 도구 호출(이름, 마스킹된 인자·결과), 가드 판정(통과/차단 사유), 에이전트 step 수와 상한 도달, plan 버전·phase, 승인 이벤트(요청자/승인자 역할만). trace id는 SQLite 이력의 plan id와 연결한다.
- 개발용 지표: 호출 횟수·토큰, 폴백 비율, 가드 차단 비율, 재시도 횟수.
- Langfuse 키(`LANGFUSE_*`)는 `.env`에서 읽고 로그에 출력하지 않으며 실행기 환경에 전달하지 않는다.
- 참고 패턴: `ref/local_tracer.py`, `ref/hook_trace.py`, `ref/trace_run.py`.

### 평가
- 3층 구조: (1) 규칙 기반(명령 정확도, 필수 step·순서, 마스킹 누출 0건), (2) 검색 평가(hit@k, MRR + RAGAS의 context precision·recall·faithfulness), (3) LLM-as-Judge(계획 타당성: 필수 step, 순서·의존성, 위험·게이트 표시, 근거 일치, 폴백 판정의 적절성).
- **평가 데이터 규칙:** 평가셋에는 실제 환경의 값을 쓰지 않는다. 합성 값(`tests/fixtures/inventory.sample.yaml`의 이름, 문서의 예시 ID)은 마스킹 경로를 실제처럼 태우기 위해 입력에 쓸 수 있고 `tests/test_eval_cases.py`의 허용 목록으로 확인한다. judge와 외부 모델에는 마스킹된 형태로만 보낸다(judge 호출도 마스킹 경계 안쪽). 입력 가드가 막은 입력은 원문 대신 `[입력 차단됨: <사유 코드>]`를 쓴다(`eval/agent_cases.py`의 `judge_view_of_input`). 합성 값 검사는 CSV의 모든 열에 적용한다.
- **에이전트 평가셋** `eval/agent_cases.csv`: 42건(positive 16, negative 8, edge 12, guardrail 6 = 권장 비율 40/20/25/15). 열은 `id, category, input, expected_traits, forbidden, expected_tools, note`이고 세미콜론으로 항목을 구분한다. `expected_tools`는 에이전트의 읽기 도구 이름(`search_docs`, `lookup_inventory`, `run_read_command`)만 쓴다. guardrail 6건 중 4건(인젝션·승인 우회·프롬프트 노출·비밀값·문서 속 지시)은 입력 가드에서, 2건(시크릿 출력, 금지 옵션)은 명령 가드 단계에서 막히는지 본다. 로더는 `eval/agent_cases.py`, 형식·데이터 규칙 검증은 `tests/test_eval_cases.py`. 에이전트(T4) 구현 전이라 아직 실행하지 않았고, 채점은 LLM-as-Judge(T9)가 `expected_traits`·`forbidden`·`expected_tools`로 한다.
- **Judge:** 생성과 다른 모델을 쓴다(기록된 실제 생성 모델의 반대쪽으로 채점: MAIN 생성이면 SUB, 폴백으로 SUB가 생성했으면 MAIN. READ는 채점용으로 약하다). temperature 0, 고정 루브릭, JSON 스키마 출력. 수동으로 라벨링한 샘플(5~10개)과의 일치율을 함께 보고해 judge의 신뢰도를 확인한다.
- **RAGAS는 평가 전용 환경(`.venv-eval`, `requirements-eval.txt`)에서 실행한다.** 앱 런타임 패키지(tenacity, rich 등)를 다운그레이드하기 때문이다. Bedrock과의 호환은 구현 시 확인한다.
- 리포트는 `eval/reports/`에 JSON과 요약으로 저장하고, 목표 수치와 이전 실행을 비교한다. 평가 실행을 Langfuse에 기록하는 것은 선택이다(개발 단계).

## 5. 참고 예제 (`ref/`)
스택을 고를 때 참고한 교육 예제다(사용자 요청). 앱이 import하지 않으며 구성은 그대로 따르지 않았다(예: 검색은 평가로 결정, D-14). 구현할 때 참고할 예제: 검색 `hybrid`, `rerank_demo`, `rag_chain`, 구조화 출력 `structured_output`, 에이전트 `react_agent`, `plan_and_execute`, `rag_agent`, 승인 `hitl_flow`, 체크포인트 `checkpointer_basic`, 가드·마스킹 `guards_*`, `masking_middleware`.
