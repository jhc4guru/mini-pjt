# TASK.md

구현 단계와 진행사항 추적 문서. 작업을 시작/완료할 때마다 상태와 변경 이력을 갱신한다.
상태: `[ ]` 대기 · `[~]` 진행중 · `[x]` 완료 · `[!]` 막힘

최종 갱신: 2026-11-04 · 현재 단계: **T3 완료(코드·테스트), T4 진입 대기** → T3 가드레일(T2 결과로 항목 추가됨) → T4

## T0. 인터뷰·문서
- [x] 요구사항 인터뷰 (목표, 승인 정책, 가드레일, 범위/Phase)
- [x] `CLAUDE.md` 작성
- [x] `SERVICE.md` 작성
- [x] `SPEC.md` 작성 (수용 기준 10개)

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
- [ ] (T4) 계획 에이전트가 입력→마스킹→검색→LLM→명령 가드를 통합 사용, 범위 밖 요청 거절(의미 판단), 모델 자유 텍스트 출력의 누출 스캔
- [ ] (T5) 실행기가 `assert_server_executable`을 반드시 거치는 구조(관문 우회 테스트), 실행기 전체 타임아웃, 실행 직전 작업 폴더 경로 재검증(검사 후 파일이 바뀌는 경우)
- [ ] (T6) 승인 엔진이 `verdict.requires` 처리(계약은 `docs/IMPLEMENTATION.md` 1절): `open_cidr_confirm`(사유 입력 + 승인자 확인), `strong_warning`, `dry_run_first`(계획에서 dry-run step이 앞서는지), `prod_warning`, `fallback_approval`, `show_file_contents`. 가드 판정을 `AuditLog`에 연결
- [ ] (T7-1) 트레이스 export 전 `assert_outbound_clean` 연결
- [ ] (T8) 승인 화면에 정책 JSON·매니페스트 내용(`local_paths`) 표시

## T4. 계획 생성
- [x] 질의 정규화·범위 판단·모호 표현 해소 `app/planner/intake.py` (READ 모델 구조화 출력, 서버 검증, 누출 검사), 테스트 16개 (D-30)
- [ ] 근거 기반 명령 생성 + 근거 조각 첨부
- [x] 되묻기 (모호함/필수 파라미터 누락): `IntakeResult.questions`. 화면 표시와 답변 재입력 처리는 T6/T8
- [ ] 조건부 폴백 ("AI 추정" 표시, 강화 승인 플래그)
- [ ] 변경 step ↔ 확인 step 짝 생성
- [x] 읽기 전용 도구 세트 `app/agents/tools.py` (search_docs, lookup_inventory, run_read_command) + 변경 경로 차단 테스트 17개. 실행은 주입된 `ReadCommandRunner`(T5 실행기가 구현)
- [ ] 계획 에이전트 (목표 분해, 의존성, 도구 호출, 상태 기반 조정) — 인터페이스 뒤에 배치. 큰 계획은 phase 단위로 생성하고 출력 `maxTokens`는 8K 이상(D-26)
- [x] LLM 호출 래퍼 `app/llm.py`: 역할별 모델 선택, 발송 전 누출 검사, MAIN 쓰로틀링 시 재시도 2회 후 SUB 폴백(요청·실제 역할 기록, 토큰은 같은 예산에 합산, 다른 오류·SUB/READ는 폴백 없이 중단), 테스트 10개 (D-19)
- [ ] 리뷰어 에이전트 (영향 범위, 위험, 롤백 누락 요약)
- [ ] 실패 분석 에이전트 (원인, 대안 step 제안 → plan 복귀 후 재승인)
- [ ] 문서를 CLI 도구별·서비스별로 분리 유지 (`knowledge/cli/aws/<service>/`, `knowledge/cli/kubectl/`) — 폴더 구조는 완료, 파일 작성은 T2
- [ ] plan을 phase 구조로 생성 (단계, 사전 조건, 게이트, 롤백) + 리뷰어 phase별 체크리스트 (`SERVICE.md` 3-2절)
- [ ] step 종류 `cli`/`manual`/`iac`/`check`/`poll` 모델링, `impact.preflight` 읽기 step 자동 삽입, `dry_run` 사전 검증 step 제안
- [ ] IaC 동기화: `iac_managed` 설정, `impact.iac: true` 변경 step에 코드 동기화 확인 step·드리프트 경고 자동 추가, `iac` step 대체 권고
- [ ] 영향 불명(`impact` 없음/폴백) 처리: 높은 위험 취급

## T5. 실행기
- [ ] 실행 위치 결정(D-28: 이 서버 직접 vs 권한 서버에 요청) — T5 시작 전
- [ ] `executor/base.py` 인터페이스 + 직렬화 가능한 `ExecutionRequest`, 요청별 격리 kubeconfig·자격증명 계약(전역 `~/.kube/config` 불변)
- [ ] mock 실행기: moto server + 환경변수 강제 (실제 AWS 차단)
- [ ] kubectl fixture 응답
- [ ] 읽기/변경 분류 규칙 (허용 목록, 애매하면 변경)

## T6. 세션·승인
- [ ] 링크 초대, 닉네임, WebSocket 동기화
- [ ] plan 일괄 승인 + 해시 잠금
- [ ] apply step별 4-eyes 승인
- [ ] 확인 실패 처리 (재시도/건너뛰기/중단)
- [ ] apply 중 수정 차단 (plan 복귀 후 재승인)
- [ ] 커스텀 step (직접 CLI, 자연어, 체크, 수정/삭제/순서변경)
- [ ] 수동 실행(manual) step: 서버 미실행, 명령 복사 버튼, apply에서 멈춤 → 요청자 "실행 완료" 보고(결과 붙여넣기 선택, 가드·마스킹 적용) → 다른 사람 확인 → 진행. 이력에는 보고자·시각 기록
- [ ] phase 게이트 승인 (요청자가 아닌 사람 확인, `irreversible` phase 게이트 제거 불가)
- [ ] 부분 재계획: apply 일시정지, 완료 step 잠금, 남은 step만 새 plan 버전으로 수정·재승인·재개 (완료 step 해시 승계)
- [ ] poll step: 간격·최대 시간·완료 조건, 타임아웃 시 중단·알림, 서버 재시작 후 재개
- [ ] plan·phase당 step 수 상한 (초과 시 차단, 분할 요청)
- [ ] `iac` step: 코드 변경 체크리스트, 완료 보고, (선택) `terraform plan` 출력 붙여넣기 → 입력 가드·마스킹 → 리뷰어 요약

## T7. 저장
- [ ] SQLite 스키마 (요청, 계획, 근거, 승인, 실행/확인 결과, Phase 2 대비 필드)
- [ ] plan 버전, phase, step 상태 저장 (일시정지·재개, 서버 재시작 후 이어서 진행)
- [x] 매핑 암호화 저장소 `mapping_store.py`(AES-GCM, 세션 AAD, 키 파일 분리, TTL, `Masker.export_state/load_state`), 테스트 5개 (D-29)
- [ ] 세션·apply 흐름에 연결: 매핑 변경 시 저장, 재시작 후 세션 열 때 복원, apply 종료·취소 시 삭제, `purge_expired` 주기 호출

## T7-1. 관측 (Trace)
- [x] 의존성: `langfuse` 설치 확인(런타임 `.venv`, 충돌·다운그레이드 없음), `requirements.txt` 반영
- [ ] 트레이스 모듈 `app/observability/`: 백엔드 추상화(`off`/`local`/`langfuse`, 기본 local), 마스킹 이후 LLM 호출 래퍼·마스킹된 도구 결과에만 부착
- [ ] 로컬 트레이스: SQLite 이력과 연결(trace id ↔ plan id). `ref/local_tracer.py`, `hook_trace.py` 패턴
- [ ] Langfuse 연동(개발 단계): `.env`에 `LANGFUSE_*` 키(로그 출력 금지, 실행기 비전달), self-hosted 권장·cloud는 개발 한정, 명시적 활성화
- [ ] export 전 누출 스캔 + 누출 시 export 차단·로컬 기록·경고 (fail-closed)
- [ ] 트레이스 누출 테스트 (원본 입력·실제 값·매핑·실행 결과 원문이 백엔드로 가지 않음)
- [ ] 개발용 지표 요약 (호출·토큰, 폴백 비율, 가드 차단 비율, 재시도)

## T8. UI
- [ ] 단일 HTML 페이지 (plan 검토, apply 진행, 승인 버튼, 근거 패널, 경고 표시)

## T9. 평가
- [ ] 질문 30~50개 + 기대 명령 (다양한 표현)
- [x] 에이전트 평가셋 `eval/agent_cases.csv` 42건(positive 16, negative 8, edge 12, guardrail 6; 41·42번은 D-30 모호 표현 해소 케이스)과 로더 `eval/agent_cases.py`, 형식·데이터 규칙 검증 `tests/test_eval_cases.py`. 에이전트 구현(T4) 전이라 실행·채점은 남음
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
- [ ] `SPEC.md` 수용 기준 10개 점검
- [ ] `CLAUDE.md`, `SERVICE.md` 최종 동기화

## 로드맵 (이번 범위 아님)
- Phase 2: 이력 기반 계획 재사용, 엑셀 계획서 import, 서비스별 전문 에이전트 + supervisor (서비스 약 8개 이상 또는 서비스별 정확도 저하 시)
- Phase 3: 테라큐브·GitHub PR, Terraform plan/apply 연동(`iac` step 자동화), AWS role 분리, 실제 인증(SSO)

## 미확인 / 막힌 항목
- 정확도 목표 수치 확정
- (해결) Bedrock 모델 ID·권한: T1에서 확인 완료
- 엑셀 형식, 테라큐브 연동 방식 (Phase 2/3 시작 시)

## 변경 이력
- 2026-11-04: 초안 작성 (T0 완료, T1 대기)
- 2026-11-04: T2 문서 작성 완료 (15개 문서, 116개 명령, 용어 사전, 검증 스크립트). 문서 취합 결과로 T3 가드레일 항목 8개 추가, restricted 9개 명령 지정(사용자 확인 필요)
- 2026-11-04: 개발 스택을 `CLAUDE.md`에서 `SERVICE.md` 1-1절로 이동(사용 중/예정 구분, 실제 import 기준으로 현황 정정). 초반에 정한 웹·저장 스택 결정이 `DECISIONS.md`에 빠져 있어 D-27로 기록
- 2026-11-04: 에이전트 평가셋 `eval/agent_cases.csv` 40건 작성(과정 템플릿 형식: id, category, input, expected_traits, forbidden, expected_tools, note, 카테고리 비율 40/20/25/15). 평가 데이터 규칙을 "플레이스홀더만"에서 "실제 값 금지, 합성 값 허용"으로 명확히 함(사용자 검토 필요, D-16 보완)
- 2026-11-04: README에 구조도(Mermaid, 구현/일부/예정 색 구분)와 과정 템플릿 항목(활용한 패턴, 평가 결과, 인-아웃 통과율, 회고, 핵심 코드 위치)을 추가. RAGAS는 "미측정"으로 표기하고 검색 수치는 `eval/reports/retrieval_20261104_143610.json`에서 옮김(BM25 75/80 → 하이브리드+리랭크 80/80). Mermaid 문법은 실제 파서로 검증(화면 렌더링은 미확인). 규칙은 `CLAUDE.md` 문서 작성 규칙에 반영
- 2026-11-04: 사용자 결정: 스택 이름을 `CLAUDE.md` 제약 줄과 `SERVICE.md` 1-1절 두 곳에 유지하고 변경 시 함께 고친다. 어긋남을 잡는 `tests/test_docs_sync.py` 추가
- 2026-11-04: 스택 제약 한 줄을 `CLAUDE.md`에 복구(스택 표는 `SERVICE.md` 1-1절 유지). 표를 옮길 때 제약까지 같이 지워 임의 프레임워크·패키지 추가를 막는 규칙이 빠져 있었음
- 2026-11-04: 위 축소 후 사용자 지적("지워도 동작이 달라지면 안 된다")으로 재점검: 1차는 정보 보존만 확인해 `CLAUDE.md`에서 동작 규칙 9묶음(간접 인젝션 무시, 되묻기·범위 밖 거절, 전체 개방 허용+통제, 시크릿 출력·샌드박스·옵션 검증·종료되지 않는 명령, 트레이스 경계, 재시도 금지, plan 세부, 자격증명, prod 판별)이 빠졌음을 확인하고 복구(76줄/10.2KB). `CLAUDE.md`만으로 31개 구절 대조, 문서 작성 규칙에 축소 시 확인 기준 추가
- 2026-11-04: `CLAUDE.md`(94줄/16KB)와 `SERVICE.md`(145줄/18.5KB) 축소: `CLAUDE.md`의 정책 8개·가드 규칙 21개를 "불변 규칙"으로 합쳐 한 줄씩(세부는 `docs/IMPLEMENTATION.md` 1절 "명령 가드가 검사하는 것"으로 이동), 낡은 줄 삭제. `SERVICE.md`는 3-4절을 9절과 합치고 중복된 검색 구성·스택 비고·`ref/` 목록을 정리하며 3-2절을 압축. 지운 내용이 남는 문서에 있는지 구절 단위로 대조
- 2026-11-04: `SERVICE.md`와 `CLAUDE.md`의 중복 정리: `SERVICE.md` 6절은 "서비스가 보장하는 것" 요약으로 줄이고 규칙 본문은 `CLAUDE.md`에, 대규모 작업·운영 환경은 `SERVICE.md`에 본문을 두고 `CLAUDE.md`는 불변 조건과 링크만. `CLAUDE.md` 안의 반복 2건 삭제, `SERVICE.md` 8절의 frontmatter·impact 설명은 `SCHEMA.md`로 일원화. 지운 내용이 남는 문서에 있는지 구절 단위로 대조하고 사라진 2건은 복구
- 2026-11-04: `SERVICE.md`(193줄)에서 구현 상세 3개 구간(가드 구현, 검색 구성, 관측·평가)을 `docs/IMPLEMENTATION.md`로 분리하고 요약+링크만 남김(137줄). 문서 관계를 설명하던 `SERVICE.md` 머리말은 `CLAUDE.md` 문서 체계 표와 중복이라 삭제. 원본의 모든 줄이 새 위치에 있음을 줄 단위로 대조해 확인
- 2026-11-04: T3 가드레일 완료(코드·테스트 219개): 마스킹·복원, 입력 가드, 간접 인젝션 방어, 실제 CLI 옵션 스냅샷, 명령 가드(폴백 포함), 에이전트 한도, 실행 정책 관문, 감사 기록, 종단 누출 테스트. 자체 레드팀 점검에서 우회 가능성 6건 발견·수정·회귀 테스트화. 승인 엔진·실행기·UI 연결은 T4~T8. 결정 D-22~D-26 기록(D-23~D-26은 사용자 검토 필요)
- 2026-11-04: 문서 체계 정리: `docs/README.md` 삭제 → 루트 `README.md`(서비스 설명·실행 방법, 실제로 동작하는 명령만), `DECISIONS.md` → `docs/DECISIONS.md`, 문서 작성 규칙은 `CLAUDE.md`에 "문서 체계와 작성 규칙" 절로 이동. `.gitignore`(.env 등 제외)와 `.env.example` 추가(`.env`에 실제 키가 있어 git 업로드 전 필수)
- 2026-11-04: 디렉터리 정리: `docs/cli` → `knowledge/cli`(앱이 읽는 데이터라 문서 폴더에 맞지 않음, 생성물 `data/`와도 분리). `docs/`는 비어 있어 보조 문서용으로 유지(`docs/README.md`). 경로 참조 코드·문서 동기화, 색인 메타데이터에 경로를 포함해 재색인. 결정과 이유를 `DECISIONS.md`(루트)에 기록 시작
- 2026-11-04: T2 검색기 완료. `ref/` 구성을 그대로 따르지 않고 평가로 결정: 하이브리드(RRF) + LLM 리랭크가 기본, 질의 확장은 BM25를 악화시켜 제외(가설과 반대 결과), 크로스인코더는 불필요. 평가셋 80 케이스·비교 스크립트·테스트 30개 추가
- 2026-11-04: 교육 과정 12개 항목 대비 점검(포함 5, 일부 4, 없음 3) 후 11 관측(Trace, Langfuse는 개발 단계 한정)·12 평가(RAGAS, LLM-as-Judge)를 Phase 1에 추가. 트레이스 경계 규칙 신설. ragas는 런타임 패키지를 다운그레이드해 `.venv-eval`로 분리, langfuse는 런타임에 설치
- 2026-11-04: 사용자 승인으로 영향 사전 확인(`impact`), EKS 업그레이드용 문서, 대규모 작업 설계(phase·게이트·부분 재계획·poll·IaC 동기화)를 반영. 운영 환경(self-managed ASG, Terraform 주도, 코드 동기화 유지) 기록. 문서 20개·명령 141개, 검증 오류 0
- 2026-11-04: 사용자 제안으로 9개 명령을 `restricted`(생성 금지) → `manual`(서버 미실행, 사람이 자기 터미널에서 직접 실행)로 전환. `kubectl/restricted.md` → `manual.md`, iam 2개 전환, 검증기·스키마·문서 동기화. 결과 붙여넣기는 선택(추천안 적용)
- 2026-11-04: `docs/cli` 구조를 `ec2_sg`/`eks_kubectl`/`s3`/`iam`에서 CLI 도구별(aws/kubectl) → 서비스별(`aws/{ec2,eks,s3,iam}`, `kubectl`)로 변경. SERVICE.md·CLAUDE.md 동기화
- 2026-11-04: T1 완료 (의존성 설치, Bedrock·moto 동작 확인, 디렉터리 생성). 관리 포트 전체 개방은 a(동일 통제)로 결정
- 2026-11-04: 에이전트 구조 반영 (T4에 도구·계획·리뷰어·실패 분석 추가, T9에 계획 평가 추가, supervisor는 Phase 2)
- 2026-11-04: 가드 검토(D-16 보완, D-23~D-26) 사용자 승인 후 반영, 테스트 전부 통과: `kubectl config` 전역 변경 차단(`kubeconfig_mutation`), `aws eks update-kubeconfig`를 manual로(10개), 넓은 CIDR 기준 보정, 감사 해시를 설치별 키 HMAC으로(시크릿 차단 입력은 해시 없음), 한도 상향(도구 30·누적 토큰 250,000·plan 120·phase 40), judge 입력은 차단 시 사유 코드만, 평가셋 전 열 합성 값 검사. 실행 위치 미정(D-28)
- 2026-11-04: D-29 결정과 구현: 마스킹 매핑을 세션별 AES-GCM 암호화로 SQLite에 저장해 서버 재시작 후에도 이어가기(Redis는 다중 서버·원격 실행 시 재검토). `cryptography` 추가, 키 파일 처리를 `keyfile.py`로 공통화, 테스트 236개 통과. 세션 연결은 T6/T7
- 2026-11-04: D-19 확정(MAIN=계획, SUB=리뷰어·실패 분석·Judge·MAIN 폴백, READ=가벼운 일). `app/llm.py` 구현, 테스트 246개. T4 시작
- 2026-11-04: T4 읽기 전용 도구 세트 구현(`ReadToolbox`): 변경·manual·차단 명령은 실행기에 도달하지 않음, 인자·결과 모두 마스킹 경계, 결과는 `sanitize_untrusted`, 호출 상한·반복 감지, 모든 판정 감사 기록. 테스트 263개
- 2026-11-04: D-30 결정(모호 표현을 모델이 근거로 해소하고 서버가 검증, 해석은 보여 주되 예/아니오 확인은 받지 않음)과 `Intake` 구현, 평가셋 41·42번 추가(42건), DECISIONS 요약 표의 D-23~D-26 상태 갱신. 테스트 279개
- 2026-11-04: D-31(서버·노드=EC2 인스턴스, Pod=Pod, 되묻지 않음): 용어 사전의 `서버` 모호 항목 제거·`노드`를 EC2 별칭으로 이동, 접수 프롬프트에 고정 사실, 평가셋 26·41·42번과 검색 평가 r14 태그 갱신, 회귀 테스트 추가. 테스트 284개
