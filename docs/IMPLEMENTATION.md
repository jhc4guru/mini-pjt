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

## 3. 관측(Trace)과 평가
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

## 4. 참고 예제 (`ref/`)
스택을 고를 때 참고한 교육 예제다(사용자 요청). 앱이 import하지 않으며 구성은 그대로 따르지 않았다(예: 검색은 평가로 결정, D-14). 구현할 때 참고할 예제: 검색 `hybrid`, `rerank_demo`, `rag_chain`, 구조화 출력 `structured_output`, 에이전트 `react_agent`, `plan_and_execute`, `rag_agent`, 승인 `hitl_flow`, 체크포인트 `checkpointer_basic`, 가드·마스킹 `guards_*`, `masking_middleware`.
