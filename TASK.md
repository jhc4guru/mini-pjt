# TASK.md

구현 단계와 진행사항 추적 문서. 작업을 시작/완료할 때마다 상태와 변경 이력을 갱신한다.
상태: `[ ]` 대기 · `[~]` 진행중 · `[x]` 완료 · `[!]` 막힘

최종 갱신: 2026-11-05 · 현재 단계: **T4·T5·T6·T8 완료(화면은 3분할+Plan/Apply 탭, 이력 한 줄·상태 점, 절차 줄 아이콘, 터미널 실행 로그). T7 저장은 이력 상세 보기(종료된 작업의 결과 전체)가 남음. 다음: T7-1 트레이싱, T9 평가, T10 시연**

## T0. 인터뷰·문서
- [x] 요구사항 인터뷰 (목표, 승인 정책, 가드레일, 범위/Phase)
- [x] `CLAUDE.md` 작성
- [x] `SERVICE.md` 작성
- [x] `SPEC.md` 작성 (수용 기준은 이후 50개까지 확장)

## T1. 기반
- [x] `requirements.txt`에 moto[server], fastapi, uvicorn[standard] 추가 및 `.venv` 설치 (moto 5.2.5, fastapi 0.162.0, langgraph 1.2.19, langchain-aws 1.9.1 확인)
- [x] `.env` Bedrock 확인(재확인 2026-11-04, `.env` 변경 반영): `MAIN_MODEL`(Sonnet 4.6), `SUB_MODEL`(Sonnet 4.5), `READ_MODEL`(Haiku 4.5), 3개 모두 호출 성공
- [x] 에이전트별 모델 매핑 결정 (D-19, 2026-11-04): MAIN=계획, SUB=리뷰어·실패 분석·Judge·MAIN 쓰로틀링 폴백, READ=가벼운 일(질의 정규화·범위 판단·리랭크·읽기 출력 요약)
- [x] 프로젝트 디렉터리 구조 생성 (`app/{guards,rag,planner,agents,executor,static}`, `knowledge/cli/{aws/{ec2,eks,s3,iam},kubectl}`, `eval/`, `tests/`; 2026-11-04 CLI 도구별 구조로 변경)
- [x] moto 스모크 테스트: `moto.server` + AWS CLI `--endpoint-url`로 SG 생성/조회 성공
- 주의: `.env`에 Bedrock용 실제 AWS 키가 있다. 실행기 프로세스에는 절대 전달하지 않는다(T5에서 더미 키 강제, LLM 클라이언트와 실행기의 환경 분리).

## T2. CLI 지식베이스
- [x] 문서 frontmatter 규격 정의 (`knowledge/cli/SCHEMA.md`: `kind` read/mutate/manual(+예약 restricted), `risk`, `required`, `verify`, `guard`)
- [x] `knowledge/cli/aws/ec2/` 작성 (security-group, instance, network)
- [x] `knowledge/cli/aws/eks/` 작성 (cluster, nodegroup)
- [x] `knowledge/cli/aws/s3/` 작성 (s3, s3api)
- [x] `knowledge/cli/aws/iam/` 작성 (role, policy, user)
- [x] `knowledge/cli/kubectl/` 작성 (get-describe, apply-delete, rollout, node, manual)
- [x] 용어 사전 `knowledge/cli/glossary.yaml` (entities 26, actions 12, ambiguous 6)
- [x] 문서 검증 스크립트 `tests/check_docs.py` (실제 aws/kubectl 도움말 대조, 음성 테스트로 검증기 동작 확인). 현재 문서 20개, 명령 141개(read 64, mutate 68, manual 9), 오류 0, 경고 0 (impact 검증 포함)
- [x] `impact` 메타데이터 도입: 스키마(`knowledge/cli/SCHEMA.md`), 검증기, mutate·manual 77개 명령에 값 기입. 검증기가 YAML `yes/no` 불리언 함정을 잡아 `reversible`을 `full/partial/irreversible`로 변경
- [x] EKS 업그레이드용 문서 추가: eks(insight, addon, list-updates), autoscaling(asg), ssm(parameter), ec2(launch-template), kubectl(version, api-resources, api-versions). 실제 CLI 대조 중 `terminate-instance-in-auto-scaling-group`이 문서보다 넓게(여러 인스턴스·ASG 단위) 호출 가능함을 발견해 반영
- [x] 검색기 `app/rag/`: 명령 단위 chunk 141개(kind·risk·impact 포함), 한국어+CLI 토큰 BM25, Titan v2 + Chroma(`data/chroma`, 해시 기반 증분 임베딩), RRF 결합, LLM 리랭크(READ 모델), 민감 질의 거부(`app/guards/patterns.py`), 기본 구성 `app/rag/factory.py`
- [x] 검색 평가셋 `eval/retrieval_cases.yaml`(80 케이스, 동의어·구어·영어·모호·수동 명령 포함) + 비교 스크립트 `eval/eval_retrieval.py`. 결과: BM25 hit@3 0.94, 벡터 0.85, 하이브리드 0.96, **하이브리드+리랭크 hit@1 0.99 / hit@5 1.00**. 용어 사전 질의 확장은 BM25를 악화시켜 기본에서 제외(상세 `docs/IMPLEMENTATION.md` 2절)
- [x] 검색 테스트: 오프라인 24개(`tests/test_rag.py`), Bedrock 온라인 6개(`tests/test_rag_online.py`, `RUN_BEDROCK_TESTS=1`)
- [x] 용어 사전: 질의 확장 대신 모호 표현 탐지·되묻기 후보 제공으로 사용 (에이전트 연동은 T4)
- (참고) Cohere 임베딩·Bedrock 리랭크는 이 계정에서 권한 없음. 크로스인코더(bge-reranker)는 torch·2GB라 보류

## T3. 가드레일 — 코드·테스트 완료 (연결은 T4~T8)
코드는 `app/guards/`, 테스트는 `tests/test_{masking,input_guard,cli_snapshot,command_guard,limits_and_policy,leak_e2e}.py`. 전체 219개 통과(실제 CLI 대조·Bedrock 온라인 포함).
- [x] 마스킹·복원 `masking.py`: 정규식(ARN, 리소스 ID, 액세스 키, 계정 ID, 이메일, IP/CIDR) + 인벤토리 이름, 세션별 매핑, 복원, 미복원 플레이스홀더 탐지, prod 판별. AWS 관리형 정책 ARN과 `0.0.0.0/0`은 공개 값이라 마스킹하지 않음. 인벤토리는 `INVENTORY_FILE`(없으면 `data/inventory.yaml`), 명시했는데 없으면 오류. 샘플 `tests/fixtures/inventory.sample.yaml`
- [x] 입력 가드 `input_guard.py`: 길이·제어 문자, 전각·보이지 않는 문자 우회, 프롬프트 인젝션·가드 우회·승인 우회 표현, 시크릿 입력 차단(비밀번호·키·토큰·개인키·JWT·presign 서명), 마스킹 후 재검사. 안내 메시지는 값을 되풀이하지 않음
- [x] 간접 인젝션 방어 `untrusted.py`: 문서·도구 결과·실행 출력을 마스킹하고, 시크릿과 지시문 의심 구간을 제거하고, 데이터 틀로 감싼다(틀을 안에서 닫는 시도 차단)
- [x] 옵션 스냅샷 `cli_snapshot.py`: 실제 `aws`/`kubectl` 도움말에서 옵션·값 여부·짧은 옵션·하위 명령을 읽어 캐시(`data/cli_snapshot.json`, CLI 버전이 바뀌면 폐기). 문서 141개 명령의 모든 문서화된 옵션이 스냅샷에 있음을 실제 CLI로 검증
- [x] 명령 가드 `command_guard.py` (모델 출력·사용자 직접 입력 step 공통, 폴백 포함):
  - 구조: 셸 연산자·명령 치환·환경변수 확장·여러 줄 차단, `aws`/`kubectl`만 허용
  - 범위: 폴백은 문서에 있는 AWS 서비스(ec2, eks, s3, s3api, iam, autoscaling, ssm)만, 문서 밖 위험 작업 거부(`ec2 get-password-data`, `ssm send-command` 등), 존재하지 않는 명령·옵션 차단, kubectl 플러그인 명령 차단
  - 금지 옵션: `--profile`, `--endpoint-url`, `--kubeconfig`, `--context`, `--token`, `--as`, `--raw`, `--cli-input-json` 등(대상·자격증명 변경, 숨은 입력)
  - 위험 패턴(차단 또는 강한 경고): 전체 개방·`/7` 이하 넓은 CIDR, 와일드카드 Principal/Action, 관리형 관리자 정책, `s3 rm --recursive`·`rb --force`·`sync --delete`, `kubectl delete --all`·네임스페이스 삭제(`-A`는 차단), `scale --replicas=0`, 공개 ACL·퍼블릭 차단 해제, 다중 대상 종료, prod 대상(서버 규칙)
  - 시크릿: `kubectl get secret -o yaml/json/…` 차단(파일 지정 포함), `create secret` 차단, SSM은 `/aws/service/` 공개 경로만·복호화 금지, 명령 안의 시크릿 차단(`--password`는 `<PASSWORD>`만)
  - 종료되지 않는 명령: `--watch`·`--follow` 차단, `rollout status`·`drain`은 `--timeout` 필수, 대화형(`--stdin --tty`)은 manual만
  - 로컬 경로 샌드박스: `file://`·`--filename`·`s3 cp/sync` 경로는 세션 작업 폴더 안만(`..`, 절대 경로, `~`, URL, `-` 차단), 정책 파일은 읽어서 검사
  - 예상치 못한 오류·CLI 실행 불가는 모두 차단(fail-closed)
- [x] 마스킹 누출 테스트 + 양방향 스캔 `boundary.py`, `tests/test_leak_e2e.py`: 사용자 입력부터 승인 화면, 실행 결과까지 흘려 외부 경계(임베딩·리랭크·LLM)로 나간 모든 텍스트에 실제 값 0건. 승인 화면에는 복원된 실제 값이 보임
- [x] 에이전트 실행 제한 `agent_limits.py`: 읽기 전용 도구 허용 목록, 호출·단계·토큰·시간 상한, 같은 호출 반복 감지, plan·phase step 상한, 구조화 출력 검증(재시도 후 사람에게 넘김)
- [x] 실행 정책 관문 `execution_policy.py`: `assert_server_executable`(차단·manual 거부, 변경은 다른 사람 승인, `requires` 확인 필수), `assert_read_only_tool_call`(읽기 도구가 변경을 못 하게)
- [x] 감사 기록 `audit.py`(SQLite): 판정과 사유 코드, 입력은 길이·해시(12자)만 저장(차단된 입력의 시크릿이 남지 않게)
- [x] 우회 경로 없음 검증: 가드 함수에 끄는 인자 없음(AST 검사), 가드 모듈이 환경변수를 읽지 않음, 환경변수로 끄려 해도 차단 유지, 문서의 guard 태그가 모두 구현됨
- [x] 레드팀 회귀 테스트(자체 점검에서 발견·수정): `Principal`의 `AWS` 외 키 와일드카드, `0.0.0.0/00` 표기와 `/1`로 쪼갠 전체 개방, `kubectl run -it` 폴백, 플러그인 명령 실행(도움말 확인 중), 파일 지정 `kubectl get -o yaml`로 시크릿 읽기, presign 서명이 LLM으로 돌아가는 문제
- (다른 단계에서 연결해야 남는 것)
- [x] (T4) 계획 에이전트가 입력→마스킹→검색→LLM→명령 가드를 통합 사용(`PlanPipeline`), 범위 밖 요청 거절(의미 판단 + 서버 근거 보정), 모델 자유 텍스트 출력의 누출 스캔 (D-38)
- [~] (T5) 실행기가 `assert_server_executable`을 반드시 거치는 구조(관문 우회 테스트)와 전체 타임아웃은 완료. 실행 직전 작업 폴더 경로 재검증은 실행하는 쪽 가드 재검사로 일부 충족, 검사 후 파일이 바뀌는 경합은 남음
- [x] (T6) 승인 엔진이 `verdict.requires` 처리(계약은 `docs/IMPLEMENTATION.md` 1절): `open_cidr_confirm`(사유 입력 + 승인자 확인), `strong_warning`, `dry_run_first`(계획에서 dry-run step이 앞서는지), `prod_warning`, `fallback_approval`, `show_file_contents`. 가드 판정을 `AuditLog`에 연결
- [ ] (T7-1) 트레이스 export 전 `assert_outbound_clean` 연결
- [ ] (T8) 승인 화면에 정책 JSON·매니페스트 내용(`local_paths`) 표시. 엔진은 `show_file_contents`를 다른 사람의 확인으로 요구한다

## T4. 계획 생성
- [x] 질의 정규화·범위 판단·모호 표현 해소 `app/planner/intake.py` (READ 모델 구조화 출력, 서버 검증, 누출 검사), 테스트 16개 (D-30)
- [x] 근거 기반 명령 생성(`chunk_id`·`grounded`) + 근거 조각 조회 `step_evidence`. 근거 조각을 이력에 저장·화면에 표시하는 것은 T7·T8
- [x] 되묻기 (모호함/필수 파라미터 누락): `IntakeResult.questions`. 화면 표시와 답변 재입력 처리는 T6/T8
- [x] 조건부 폴백: `Step.fallback`(문서 근거 없음)·`needs_other_approval` (표시·승인 UI는 T6/T8)
- [x] 계획 스키마 `app/planner/schema.py`: PlanDraft(모델) → `build_plan`(서버 보강: 분류·위험·근거·폴백·impact, 게이트 강제), 구조 검증, `plan_hash`, 테스트 14개. 변경 step ↔ 확인 step 짝은 검증으로 강제(자동 생성은 계획 에이전트가 함)
- [x] 읽기 전용 도구 세트 `app/agents/tools.py` (search_docs, lookup_inventory, run_read_command) + 변경 경로 차단 테스트 17개. 실행은 주입된 `ReadCommandRunner`(T5 실행기가 구현)
- [x] 계획 에이전트 `app/planner/agent.py` + 시스템 프롬프트 `prompts.py` (도구 호출, phase별 제출, 서버 검증·수정 루프, 중단 처리, `LLMClient.chat`), 테스트 23개 (D-32). 실제 MAIN 모델로 EKS 업그레이드 시나리오 실행(5 phase·50 step 계획 생성, 331,000 토큰). 한도는 D-26에서 상향. 평가셋 채점은 T9에서
- [x] LLM 호출 래퍼 `app/llm.py`: 역할별 모델 선택, 발송 전 누출 검사, MAIN 쓰로틀링 시 재시도 2회 후 SUB 폴백(요청·실제 역할 기록, 토큰은 같은 예산에 합산, 다른 오류·SUB/READ는 폴백 없이 중단), 테스트 10개 (D-19)
- [x] 리뷰어 `app/planner/review.py` (서버 규칙 체크리스트·위험 등급 + SUB 모델 요약, 참고용), 테스트 12개 (D-35). 실제 SUB 모델로 확인
- [x] 실패 분석 에이전트 `app/planner/failure.py` (서버 분류·추천 좁히기·대안 step 검증, SUB 모델, 읽기 도구) + 공용 대화 루프 `toolloop.py`, 테스트 29개 (D-37). 실제 SUB 모델·mock 실행기로 확인. 실패 시 호출·사람의 선택 반영은 T6
- [x] plan을 phase 구조로 생성 (단계, 사전 조건, 게이트, 롤백) + 리뷰어 phase별 체크리스트 (`SERVICE.md` 3-2절)
- [x] step 종류 `cli`/`manual`/`iac`/`check`/`poll` 모델링(스키마), `impact.preflight` 읽기 step 서버 자동 삽입(못 만들면 경고), `dry_run`은 경고로 제안 (D-33)
- [x] IaC 동기화: `iac_managed`이고 `impact.iac: true`인 변경 뒤에 코드 동기화 확인 `iac` step과 `iac_drift`·`consider_iac_step` 경고 자동 추가 (D-33). `iac_managed` 설정 화면은 T6/T8
- [x] 영향 불명 처리: 가드가 `unknown_impact` 플래그와 높은 위험으로 판정(`Step.flags`). 표시는 T8

## T5. 실행기
- [x] 실행 위치(D-28)는 Phase 3 전까지 미정으로 두고 T5는 위치와 무관하게 구현(사용자 승인)
- [x] `executor/base.py` 인터페이스 + 직렬화 가능한 `ExecutionRequest`·`ExecutionResult`, 단일 관문(`assert_server_executable` + 승인 내용 일치 + 실행하는 쪽 가드 재검사), 요청별 격리 환경·kubeconfig(전역 불변), 전체 타임아웃, 관문 우회 불가 정적 테스트 (D-36)
- [x] mock 실행기: 루프백 moto 서버 + 더미 키·엔드포인트 지정 + 서버 환경 비상속, 시뮬레이터(EKS 업그레이드·애드온·인사이트·ASG 인스턴스 리프레시·SSM), `seed()` (D-36)
  - moto 지원 범위 확인(2026-11-04, 로컬 moto 서버에 aws CLI 46개 명령): 36개 성공. EC2(SG·인스턴스·launch template)·IAM·S3·ASG 기본·EKS 클러스터 기본은 동작. **미지원:** `eks update-cluster-version`·`list-updates`·`list-addons`·`describe-addon-versions`·`list-insights`, `autoscaling start-instance-refresh`·`describe-instance-refreshes`(500). 그 밖 실패는 시험 준비 문제(AWS 관리형 정책 미탑재, 버전 관리 버킷 비우기, SSM 파라미터 미탑재). 미지원 명령은 실행기가 상태를 가진 시뮬레이터로 대신한다(EKS 업그레이드 시나리오의 핵심 변경 명령이라)
- [x] kubectl 고정 응답(읽기 표·변경 응답, 노드 격리 상태, 모르는 명령은 오류) + 에이전트 읽기 도구 연결(`ReadRunnerAdapter`)
- [x] 읽기/변경 분류 규칙(허용 목록, 애매하면 변경): T3 `CommandGuard`에 구현·테스트됨

## T6. 세션·승인
완료(2026-11-05, D-39): 코드는 `app/session/`(`states`, `store`, `approval`, `apply`, `edit`, `replan`, `service`)와 `app/web/`(`server`, `factory`, `main`), 테스트는 `tests/test_{session_store,approval,apply,apply_flows,replan,service,web}.py`. 화면(T8)은 아직 없어 WebSocket API와 테스트로 확인한다. 남은 것은 아래 "T6 남은 것".
- [x] 링크 초대(초대 토큰), 닉네임, 멤버 토큰 인증, WebSocket 동기화(세션의 모든 연결이 같은 스냅샷을 받음)
- [x] plan 일괄 승인 + 해시 잠금(요청자 승인 불가, 해시 재계산, 새 버전이면 이전 승인 무효)
- [x] apply step별 4-eyes 승인(순서·의존·게이트·`requires` 조치, 실행 직전 승인 자격 재계산)
- [x] 확인 실패 처리 (재시도/건너뛰기/중단: 서버 규칙으로 재시도 가능 오류·횟수 상한·상태 확인, 건너뛰기는 다른 사람·의존 없음일 때만, 실패 분석 연결)
- [x] apply 중 수정 차단 (plan 복귀 후 재승인: 일시정지 후 새 버전 제출은 부분 재계획 단위에서)
- [~] 커스텀 step: 직접 CLI·체크·수정·삭제·이동·게이트 설정 완료(`PlanEditor`). **자연어로 만드는 step은 남음**(계획 에이전트를 부분 호출하는 방식, 결정 필요)
- [x] 수동 실행(manual) step(엔진): 서버 미실행, apply에서 멈춤 → 요청자 "실행 완료" 보고(결과 붙여넣기 선택, 가드·마스킹 적용) → 다른 사람 확인 → 진행. 이력에는 보고자·시각 기록. 명령 복사 버튼은 T8
- [x] phase 게이트 승인 (요청자가 아닌 사람 확인, 앞 phase 완료 후, `irreversible` phase 게이트 제거 불가)
- [x] 부분 재계획: apply 일시정지, 완료 step 잠금, 남은 step만 새 plan 버전으로 수정·재승인·재개 (완료 step 해시 승계)
- [x] poll step: 간격·최대 시간, 타임아웃 시 중단, 서버 재시작 후 재개. 완료 조건은 사람이 최신 결과를 보고 확인(서버 자동 판정은 결정 필요, DECISIONS "아직 정해지지 않은 것")
- [x] plan·phase당 step 수 상한 (초과 시 차단, 분할 요청): `build_plan`이 직접 수정 후에도 다시 검사
- [x] `iac` step: 코드 변경 체크리스트 전체 표시 후 완료 보고, (선택) `terraform plan` 출력 붙여넣기 → 입력 가드·마스킹(저장). 붙여넣은 출력의 리뷰어 요약은 남음

## T6 남은 것
- 자연어 커스텀 step, poll 완료의 서버 판정 여부, 세션별 작업 폴더 분리(결정 필요 항목은 `docs/DECISIONS.md` "아직 정해지지 않은 것")
- T5 잔여(검사 후 파일이 바뀌는 경합): 실행 직전 해시 재계산과 실행기 가드 재검사로 일부 막음. 세션별 작업 폴더를 분리하면 함께 줄어든다

## T7. 저장
- [x] SQLite 스키마 (세션, 참여자, plan 버전, 근거·리뷰, 승인, 실행/확인 결과, 이벤트). Phase 2 대비 필드와 이력 조회 화면은 남음
- [x] plan 버전, phase, step 상태 저장 (일시정지·재개, 서버 재시작 후 이어서 진행)
- [x] 매핑 암호화 저장소 `mapping_store.py`(AES-GCM, 세션 AAD, 키 파일 분리, TTL, `Masker.export_state/load_state`), 테스트 5개 (D-29)
- [x] 세션·apply 흐름에 연결: 동작마다 매핑 저장, 재시작 후 세션 열 때 복원(읽을 수 없으면 중단), 종료·취소 시 삭제, `purge_expired` 1시간마다 호출

## T7-1. 관측 (Trace)
- [x] 의존성: `langfuse` 설치 확인(런타임 `.venv`, 충돌·다운그레이드 없음), `requirements.txt` 반영
- [ ] 트레이스 모듈 `app/observability/`: 백엔드 추상화(`off`/`local`/`langfuse`, 기본 local), 마스킹 이후 LLM 호출 래퍼·마스킹된 도구 결과에만 부착
- [ ] 로컬 트레이스: SQLite 이력과 연결(trace id ↔ plan id). `ref/local_tracer.py`, `hook_trace.py` 패턴
- [ ] Langfuse 연동(개발 단계): `.env`에 `LANGFUSE_*` 키(로그 출력 금지, 실행기 비전달), self-hosted 권장·cloud는 개발 한정, 명시적 활성화
- [ ] export 전 누출 스캔 + 누출 시 export 차단·로컬 기록·경고 (fail-closed)
- [ ] 트레이스 누출 테스트 (원본 입력·실제 값·매핑·실행 결과 원문이 백엔드로 가지 않음)
- [ ] 개발용 지표 요약 (호출·토큰, 폴백 비율, 가드 차단 비율, 재시도)

## T8. UI
- [x] 단일 HTML 페이지 `app/static/index.html` (D-41): 요청·초대 참여, plan 검토(리뷰 위험 등급·체크리스트·AI 요약 표시, 근거 패널, 경고·위험 표시, 수정·삭제·이동·게이트·step 추가), 승인 버튼(역할에 맞게 비활성), apply 진행(요구 조치·사유 입력·실행·폴링·manual 보고·확인), 실패 후 재시도·건너뛰기·중단과 AI 분석 표시, 이력. 가짜 DOM(node) 렌더링 테스트와 정적 검사, 실제 Chrome 조작 확인(수동)으로 확인
- [x] 좌측 이력·새 작업, 대화형 화면, 하단 입력창, 같은 세션에서 추가 정보 이어 받기(D-42). 실제 Chrome에서 새 작업 → 되묻기 → 답하기 → plan → 다른 탭 참여·승인 → apply 시작, 모바일 폭까지 확인(`tests/browser/`, 2026-11-05)
- [x] 화면을 세로 3분할로 재구성(D-43): 좌측 이력(DB)·새 작업·작업자·초대 링크 재발급, 중앙 절차 목록(승인·선택 수정·추가·드래그앤드롭), 우측 대화, 작업 제목 필수 저장, 활동 로그는 접어 둠. 실제 Chrome에서 확인
- [x] Plan 탭을 연습장으로(D-47): 초안·확정·Apply로 보내기, 빈 plan 시작, 직접 입력·명령 만들기(조회로 값 고르기), 단계 간 드래그·복제·삭제, 되돌리기, 말로 수정 요청(수정안 수락), 보낸 입력 즉시 표시. 실제 Chrome에서 확인(2026-11-05)
- [ ] 남은 화면: 종료된 작업의 결과 상세 보기(매핑이 지워져 플레이스홀더로 보임), 정책 JSON·매니페스트 파일 내용 표시(`local_paths`는 plan에 저장되지 않아 서버 변경이 먼저 필요), 자연어 커스텀 step 입력, 모바일 세부 조정

## T9. 평가
- [ ] 질문 30~50개 + 기대 명령 (다양한 표현)
- [x] 에이전트 평가셋 `eval/agent_cases.csv` 42건(positive 16, negative 8, edge 12, guardrail 6; 41·42번은 D-30 모호 표현 해소 케이스)과 로더 `eval/agent_cases.py`, 형식·데이터 규칙 검증 `tests/test_eval_cases.py`. 실행·채점은 남음
- [ ] 다단계 목표 질문 10~15개 + 기대 step (에이전트 계획 평가): 일부는 위 평가셋(8, 9, 15, 30번)에 포함, 기대 step 상세 채점 기준은 별도
- [ ] EKS 1.35→1.36 업그레이드 시나리오 (self-managed ASG, Terraform 환경): 기대 phase 0~5, 게이트 위치, 되돌릴 수 없는 step 표시, 사전 점검 자동 삽입 확인
- [ ] 평가 전용 환경 `.venv-eval` 생성(`requirements-eval.txt`: ragas) + Bedrock 호환 확인
- [~] 검색 평가: hit@k·MRR은 구현 완료(`eval/eval_retrieval.py`, 80 케이스). RAGAS(context precision·recall, faithfulness)는 남음
- [ ] LLM-as-Judge: 고정 루브릭, JSON 스키마, temperature 0, 생성과 다른 모델(MAIN 출력은 SUB로 채점), 계획 타당성·근거 일치·폴백 판정
- [ ] Judge 신뢰도: 수동 라벨 샘플 5~10개와 일치율 측정
- [ ] 평가 리포트 `eval/reports/` (규칙 기반 + 검색 + judge 통합, 이전 실행과 비교), 평가 데이터는 플레이스홀더만 사용. 측정이 끝나면 README의 "RAGAS 평가 결과"·"인-아웃 세트 통과율"·구조도 상태를 함께 갱신
- [ ] 자동 채점 스크립트 (정확도, 폴백 비율, 누출 여부, 계획 타당성)
- [ ] 정확도 목표 수치 확정 (제안 90%)

## T10. 마무리
- [ ] 로컬 end-to-end 시연 (탭 2개, 다른 닉네임)
- [ ] `SPEC.md` 수용 기준 전체 점검
- [ ] `CLAUDE.md`, `SERVICE.md` 최종 동기화

## 로드맵 (이번 범위 아님)
- Phase 2: 이력 기반 계획 재사용, 엑셀 계획서 import, 서비스별 전문 에이전트 + supervisor (서비스 약 8개 이상 또는 서비스별 정확도 저하 시), 에이전트 계층의 LangGraph 이전(D-52, 견적 3~5일, 이전 전 기준값 고정)
- Phase 3: 테라큐브·GitHub PR, Terraform plan/apply 연동(`iac` step 자동화), AWS role 분리, 실제 인증(SSO)

## 미확인 / 막힌 항목
- 엑셀 형식, 테라큐브 연동 방식 (Phase 2/3 시작 시)

## 변경 이력
상세한 결정과 이유는 `docs/DECISIONS.md`, 구현 상세는 `docs/IMPLEMENTATION.md`에 있다. 여기에는 단계별 요약만 둔다.
- 2026-11-04: T0~T2 완료. 요구사항 인터뷰와 문서(CLAUDE/SERVICE/SPEC), CLI 지식베이스(문서 20개·명령 141개, 용어 사전, `check_docs.py`), 검색기(하이브리드+리랭크, 평가셋 80건). 사용자 승인으로 `impact`, EKS 업그레이드 문서, 대규모 작업 설계(phase·게이트·부분 재계획·poll·IaC 동기화) 반영, `restricted` 9개를 `manual`로 전환
- 2026-11-04: 문서 체계 정리. `CLAUDE.md`·`SERVICE.md` 축소와 중복 제거(지운 내용은 남는 문서와 구절 단위로 대조), 구현 상세를 `docs/IMPLEMENTATION.md`로 분리, `docs/cli` → `knowledge/cli`, README 구조도, 스택 규칙과 `tests/test_docs_sync.py`. 관측(Trace)·평가(RAGAS, Judge)를 Phase 1에 추가
- 2026-11-04: T3 가드레일 완료(테스트 219개). 자체 레드팀 점검에서 우회 6건 발견·수정·회귀 테스트화. 가드 검토 반영(D-23~D-26): `kubectl config` 차단, `update-kubeconfig` manual, 넓은 CIDR 기준, 감사 해시 HMAC, 한도 상향. D-29: 마스킹 매핑 암호화 저장
- 2026-11-04: T4 구현. 모델 역할·폴백(D-19), 읽기 도구, 접수(D-30·31), 계획 스키마, 계획 에이전트(D-32), 후처리(D-33), 한도 상향·`irreversible` 게이트 잠금(D-26·34), 리뷰어(D-35). 리뷰어 실측 중 한글 조사가 붙은 값이 마스킹을 피하는 구멍을 발견해 ASCII 경계 패턴으로 수정
- 2026-11-04: T5 완료(D-36): 단일 관문·이중 가드, 격리 환경, 루프백 moto와 시뮬레이터(moto가 EKS 업그레이드·ASG 인스턴스 리프레시를 지원하지 않아 필요), kubectl 고정 응답. 리소스 ID 마스킹을 일반 패턴으로 확장
- 2026-11-04: T4 완료: 실패 분석(D-37), 공용 `ToolLoop`, `PlanPipeline`. 실제 모델 실행에서 접수 모델의 범위 밖 오판을 발견해 D-38로 보정, 윤곽 단계 대화 압축(506,000 중단 → 282,000~298,000 성공), 리뷰어 출력 한도 3,500 토큰, `eval/run_eks_scenario.py` 추가. 테스트 466개(플래그 포함 473개)
- 2026-11-05: T6 완료(D-39): 세션 저장소·상태 전이 표, 멤버 토큰, plan 승인·해시 잠금, step·게이트·`requires` 승인과 실행 직전 재계산, apply 진행(실행 선점, 확인 step 자동 실행, 실패 후 재시도·건너뛰기·중단 규칙), manual·iac 보고·확인, poll, 재시작 복구, 직접 수정·부분 재계획, 서비스 계층, FastAPI+WebSocket(Origin 검사·인증). 승인 사유 입력을 시험하다 입력 가드의 문장형 한국어 시크릿 누락을 발견해 보완(D-40, 회귀 테스트 먼저). 에이전트 토큰 한도 700,000(D-26 재조정 2). 테스트 579개(플래그 포함 586개)
- 2026-11-05: T6 종단 확인(`eval/run_eks_session.py`, 실제 모델 + mock): 6 phase·48 step, 303,795 토큰, 승인 후 48 step 모두 완료. 첫 실행에서 poll step이 확인 step인 계획이 자동 확인 실행을 멈추는 것을 발견해 수정(poll은 조회 시작, 자동 승인도 의존·앞선 phase·게이트 조건 확인, 회귀 테스트 2개). 테스트 581개(플래그 포함 588개)
- 2026-11-05: T8 화면 구현(`app/static/index.html`, 외부 리소스 없음, DOM은 텍스트 노드로만 생성). 서비스 스냅샷에 체크리스트 문구와 분석 step id를 추가. 테스트: 정적 검사(`tests/test_ui.py`: 문법, 위험한 DOM API 금지, 보내는 메시지·서버 메시지·플래그 라벨·상태 라벨 일치), 가짜 DOM 렌더링(`tests/test_ui_render.py`, `tests/ui_harness.js`: 역할별 버튼 활성·비활성, 눌렀을 때 나가는 메시지). 테스트 594개(플래그 포함 601개)
- 2026-11-05: 사용자 피드백(입력창 없음, 추가 정보를 새 창에서 처음부터 해야 함, 좌측 이력·새 작업, 하단 입력창)을 반영(D-42): 같은 세션에서 `answer`로 추가 정보를 이어 받음(서버 대화 기록·`generating` 상태·재시작 복구), 화면을 좌측 이력 + 대화 + 하단 입력창으로 재구성, 실제 Chrome(DevTools Protocol) 확인 스크립트를 `tests/browser/`에 추가. 계획 준비가 끝나기 전에 상태가 `plan`으로 보이는 경쟁 조건을 발견해 상태를 마지막에 바꾸도록 수정
- 2026-11-05: 사용자 요청으로 화면을 세로 3분할로 재구성하고(D-43) 이력을 서버 DB에서 읽게 바꿈(브라우저에는 자격만 저장), 작업 제목 필수 저장, 초대 링크를 요청자가 필요할 때 재발급, 절차 드래그앤드롭·선택 수정·추가(변경+확인 동시), 좁은 화면 탭. 실제 Chrome 확인에서 `after: null`을 서버가 거부하는 버그를 발견해 수정(회귀 테스트 추가). 브라우저 확인 스크립트가 백그라운드 탭에서 멈추는 문제는 앞으로 가져온 뒤 캡처하고 응답 시간 제한을 둠
- 2026-11-05: 사용자 테스트 중 발견한 문제(D-44): (1) 계획 생성 중단의 원인을 감사 기록으로 확인 — 한 건은 `repeated_call` 한도, 한 건은 모델이 Ubuntu AMI를 찾으려고 쓴 `--owners 099720109477`(Canonical 공개 계정 ID)이 계정 ID 패턴에 걸림(같은 요청 4번 재현). 가드는 그대로 두고 프롬프트로 AMI를 SSM 공개 파라미터로 찾게 하고, mock에 해당 파라미터를 추가해 같은 요청이 3번 중 2번 계획까지 도달(약 45~50초, 9만 토큰). (2) 중단 안내를 원인별 한국어로, "다시 시도" 버튼, 입력창 안내. (3) kubectl 우선(aws eks는 제한). (4) 리뷰는 기본 접힘. (5) 옛 빈 세션 2개 삭제. 남은 결정: 반복 호출·공개 계정 ID·도구 인자 누출 처리 3건(DECISIONS "아직 정해지지 않은 것"). 테스트 619개
- 2026-11-05: 사용자 승인으로 가드 완화 3건(반복 호출 알림, 공개 AMI 제작사 계정 ID, 도구 인자 누출 오류 반환)을 구현·시험하고(D-45) 규칙 문서(CLAUDE.md, SERVICE.md)를 같이 고침. 요청 반영: apply의 읽기 절차는 확인 한 번, 실패·AI 분석을 대화로(분석 중 "답변을 생성 중"), Plan/Apply 탭, `[AGENT]`/`[MANUAL]` 표시·글씨 크기·색, 작업자·초대를 제목 아래로, 대화 헤더에 작업 제목, 절차 끼워 넣기. 렌더링 하네스가 `append(null)`이 브라우저에 "null" 글자를 찍는 버그를 잡아 수정. 테스트 639개(플래그 포함 646개)
- 2026-11-05: kubectl 우선 규칙을 프롬프트에 넣어 실제 모델로 EKS를 두 번 측정한 결과 `aws eks` 26/59, 23/54로 효과가 부족해 서버 안내(리뷰 `aws_eks_heavy` info)와 화면 배지를 추가(D-45). 강제 방식은 결정 대기. EC2 요청은 3번 중 1번 계획 생성(나머지는 없는 플레이스홀더로 `repair_exhausted`), 계정 ID·반복 호출 중단은 재발하지 않음. 테스트 641개(플래그 포함 648개)
- 2026-11-05: 사용자 요청(D-46): 이력 한 줄·상태 점(초록 깜빡임=APPLY 진행 중·작업자 초대 완료), 절차 줄의 승인(체크박스)·실행(액션) 아이콘과 상태 글자의 아이콘화, 상세의 승인·확인 버튼 제거, Apply 탭은 apply를 진행한 뒤에만, 터미널 형태 실행 로그, `inventory.example.yaml`(주석 가이드)과 `data/inventory.yaml`(dev-main, rcs, ops). 사용 중 발견: 이전 질문 없이 답만 받아 같은 질문을 되풀이하고 "ssh키"에 Access Key를 되묻는 문제를 수정(`[이전 질문]` 전달, 사전 복합어 제외, "기본" 처리 프롬프트). 실제 Chrome 확인(아이콘으로 승인·실행, 터미널, 점 깜빡임). 테스트 651개(플래그 포함 658개)
- 2026-11-05: 사용자 요청으로 Plan 탭을 연습장으로 바꿈(D-47). 서버: `app/session/scratch.py`(초안 저장·검증·확정 묶음·승격·되돌리기·명령 만들기 도우미), `build_plan`의 `suppressed`·`frozen`, 빈 plan 세션, 에이전트 결과를 초안으로 덧붙이기, 옛 `edit` 메시지 제거, 말로 수정(`app/planner/scratch_edit.py`). 화면: 연습장 편집기·빌더·수정안 카드, 보낸 입력의 즉시 표시(원인: 포커스가 남은 입력창이 서버 갱신을 보류하던 것). 사용 중 발견: 새 키에 Titan 임베딩 권한이 없어 계획 생성이 `EmbeddingError`로 중단됨(권한 요청 중, 안내 메시지 추가). 종료 시점의 생성 스레드 경쟁(닫힌 DB에 기록) 정리. 테스트는 관련 파일만 실행(전체 실행은 하지 않음)
- 2026-11-05: 연습장에서 사람이 하는 절차(IaC 코드 변경·수동 확인·수동 명령)를 만들고 종류를 바꿀 수 있게 함. 실제 모델(Bedrock + mock)로 빈 plan 세션의 EC2 요청을 확인: 키 페어 되묻기(18초) → 답 뒤 초안 5개(AMI는 SSM, 기본 서브넷·보안 그룹 조회, run-instances, poll, 40초), 말로 수정 제안 4초에 올바른 절차 id로 수정안. 말로 수정 카드는 실제 Chrome에서 수락·되돌리기까지 확인(가짜 편집기). 앞 절차가 만드는 리소스 ID를 뒤 절차가 쓰는 기능은 없어 결정 대기(DECISIONS)
- 2026-11-05: 사용 피드백 반영(D-47 보완): 막힌 초안 표시를 ✖ 대신 ⛔로, 확정을 막는 이유를 줄 아래에 보이고 체크박스를 눌러도 이유를 알려 줌, 변경 절차의 확인 절차 만들기 바로가기, 단계·절차 이름 바로 고치기, `aws eks → kubectl 일괄 변환` 제거(필요 없다는 의견), IaC 동기화 안내는 위험 등급에서 빼고 info로. 임베딩(Titan) 접근이 열려 호출·검색(리랭크 포함) 확인(실제 모델로 계획 생성한 것은 아님)
- 2026-11-05: 앞 절차 결과 참조 구현(D-48, 사용자 승인): `@{절차.필드}` 템플릿 승인 + 실행 직전 서버의 리소스 ID 치환(가드·관문 재통과), plan 검증·의존 자동 추가, 연습장 검증·묶음 확정·선택 목록·배지, 계획 에이전트 프롬프트. mock(moto) 종단: SG 생성 → 같은 plan에서 그 GroupId로 규칙 추가, 이력에는 마스킹된 명령만. 관련 테스트 파일만 실행
- 2026-11-05: 이력 오동작 확인: 서버 DB에는 작업마다 세션이 하나인데 한 브라우저의 다른 닉네임 탭(자격 둘)이 이력을 두 줄로 그렸다. 세션마다 한 줄로 묶음. 작업 시각(생성·Plan 전송·Plan 승인·Apply 시작·종료·마지막 수정)을 서버가 계산해 헤더와 툴팁에 표시. 실제 Chrome으로 확인
- 2026-11-05: 초안 요청이 되묻기만 하고 초안을 안 만들던 문제 수정(D-47 보완 4): 초안 모드 신호(`drafts`)를 계획 에이전트에 전달해 기본값으로 만들게 함. 실제 모델로 같은 문장을 돌려 초안 4개 확인. 참조 대상의 `--query`/`--output text` 검증 추가. 관련 테스트 파일만 실행
- 2026-11-05: 사용자 지적("확인자가 확정해야 apply로 가는 것 아니냐")에 따라 화면 용어를 정리: 요청자의 체크 = 검토 요청에 포함, 버튼 = 검토 요청, 확인자의 일괄 승인 = 확정(승인)(규칙은 그대로, D-47 보완 5). 또 어느 세션에서든 에이전트가 되묻지 않고 기본값으로 초안을 만들게 함: 요청으로 시작한 일반 세션의 "SG 생성 + 2022 인바운드"를 실제 모델로 44초에 plan까지 확인(`@{create-sg.GroupId}` 참조, 허용 범위는 기본 VPC CIDR로 가정)
- 2026-11-05: 사용자 요청 2건(D-49): (1) "필요한 조치"에서 누가 무엇을 해야 하는지 눈에 띄게: Apply 탭 "내가 지금 할 일/상대가 할 일" 패널, 줄의 내 차례·상대 차례·앞 절차 대기 칩, 필요한 조치별 담당(색 구분). (2) 대화로 "apply 진행해줘/확정해줘/다음 절차 실행해줘"를 하면 모델 없이 실행 제안 카드가 되고 사람이 눌러야 실행(누른 사람의 권한·서버 규칙 그대로). 소수 승인 면제(2인 1조 완화)는 규칙 변경이라 결정 대기. 관련 테스트 파일만 실행
- 2026-11-05: 저위험 변경 절차의 사전 승인 구현(D-50, 사용자 선택): 확인자가 plan을 확정할 때 기준(문서 근거·위험 high 아님·중단·교체·되돌릴 수 없음 없음·추가 조치·강한 경고 없음, IaC 불일치 안내 무관)을 통과한 변경 절차의 승인도 함께 기록, apply에서는 ▶ 실행만. 확정 전 카드에 대상과 개별 승인 사유 표시, 위험 medium 배지는 숨김. 모델 중단 원인 진단: 오류 종류 기록·안내, Bedrock 읽기 제한 300초(MAIN 일일 토큰 한도 쓰로틀링을 한 번 관찰). 관련 테스트 파일만 실행
- 2026-11-05: 반려 구현(D-51, 사용자 제안): 확인자가 plan을 코멘트(전체 필수·절차별 선택)와 함께 돌려보내면 plan은 `rejected`·세션은 `draft`, 요청자의 연습장에 반려 사유·절차별 💬가 보이고 고쳐 다시 검토를 요청해 새 plan을 확정. 대화의 "반려해줘"는 실행 제안 카드. 실제 Chrome(두 탭)으로 전 과정 확인. 관련 테스트 파일만 실행
- 2026-11-05: 사용자 질문("랭체인·랭그래프는 안 쓴 거냐")에 따라 스택 문서를 실제 코드에 맞춤(D-52): Phase 1 에이전트는 LangGraph 없이 Bedrock 직접 호출 + 자체 도구 루프로 구현했음을 사후 기록(승인·기록 없이 벗어났던 것), LangGraph 이전은 Phase 2 supervisor 때로. `CLAUDE.md` 스택 줄, `SERVICE.md` 1-1절(웹·저장·mock 실행기의 오래된 "예정"도 "사용 중"으로)·로드맵, DECISIONS 색인 표(D-47~D-52 누락분 추가). 코드 변경 없음
