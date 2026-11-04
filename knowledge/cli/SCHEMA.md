# knowledge/cli 문서 규격

CLI 지식베이스 문서의 작성 규칙. 검색(RAG), 읽기/변경 분류, 출력 가드(허용 명령 화이트리스트)의 근거 데이터다.
검증: `.venv\Scripts\python.exe tests\check_docs.py` (실제 `aws`/`kubectl` 도움말과 대조).

## 폴더
```
knowledge/cli/
  aws/{ec2,eks,s3,iam}/   # AWS CLI. 폴더는 서비스 묶음, 파일은 주제
  kubectl/
  glossary.yaml
```
- 서로 다른 CLI(aws, kubectl)를 한 폴더에 섞지 않는다.
- `aws/s3/`에는 `s3.md`(`aws s3`, 고수준)와 `s3api.md`(`aws s3api`, 저수준)가 함께 있다.

## 파일 구조
파일 맨 위 YAML frontmatter(기계용) + 본문(검색·사람용).

```yaml
---
cli: aws                  # aws | kubectl
service: ec2              # aws CLI의 실제 서비스명(ec2, eks, s3, s3api, iam). kubectl은 kubectl
topic: security-group
commands:
  - name: create-security-group      # kubectl은 "rollout status"처럼 공백 포함 가능
    kind: mutate                     # read | mutate | manual | restricted
    risk: low                        # low | medium | high
    required: [--group-name, --description]
    optional: [--vpc-id]
    verify: describe-security-groups # 변경 후 확인용 read 명령(같은 cli·service 안)
    guard: [open-cidr]               # 선택. 출력 가드가 추가 검사할 항목 태그
    reason: "..."                    # manual, restricted일 때 필수
---
```

### 필드
| 필드 | 의미 |
|---|---|
| `kind: read` | 조회 전용. 요청자가 바로 실행 가능 |
| `kind: mutate` | 상태 변경. 4-eyes 승인 필요 |
| `kind: manual` | 명령 문구만 생성하고 **서버는 실행하지 않음**. 사람이 자기 터미널(또는 서버 셸)에서 직접 실행. 대화형·종료되지 않는 명령, 시크릿 평문 출력/입력 명령. `reason` 필수 |
| `kind: restricted` | 어떤 경우에도 생성·실행하지 않음(폴백 포함). `reason` 필수. 현재 쓰는 명령 없음, 예약 |
| `risk` | 승인 화면 경고 수준 |
| `required` | 문서가 요구하는 필수 옵션(긴 형식만) |
| `optional` | 주로 쓰는 옵션. 전체 목록이 아니다(출력 가드는 실제 CLI 옵션 존재 여부로 별도 검사) |
| `verify` | 변경 step 뒤에 짝으로 생성할 읽기 확인 step |
| `guard` | 가드 태그: `open-cidr`, `wildcard-principal`, `wildcard-action`, `admin-policy`, `recursive-delete`, `sync-delete`, `public-access`, `output-secret`, `input-secret`, `local-file`, `no-follow`, `delete-all`, `file-input` |

### impact (mutate·manual 명령은 필수, read는 생략)
변경이 서비스에 미치는 영향을 문서에 적는다. 계획 생성 시 사전 점검 step 자동 추가, 승인 화면 경고, 리뷰어 요약의 근거가 된다.
```yaml
    impact: {disruption: possible, replaces: false, reversible: partial, preflight: [describe-nodegroup, "ec2:describe-subnets"], dry_run: "--dry-run=server", iac: true, long_running: true}
```
| 필드 | 값 | 의미 |
|---|---|---|
| `disruption` | `none` / `possible` / `likely` | 서비스 중단 가능성 |
| `replaces` | `true` / `false` | 리소스를 삭제 후 재생성하는지 |
| `reversible` | `full` / `partial` / `irreversible` | 되돌릴 수 있는지. YAML에서 `yes`/`no`는 불리언으로 해석되므로 쓰지 않는다 |
| `preflight` | 읽기 명령 이름 목록 | 실행 전 확인할 읽기 명령. 같은 service면 이름만, 다른 service면 `service:이름`. 모두 `kind: read`여야 함 |
| `dry_run` | 문자열 | 있으면 사전 검증 방법(예: `--dry-run=server`). AWS `--dry-run`은 권한 검사일 뿐 영향 분석이 아님 |
| `iac` | `true` | IaC(Terraform 등)로 관리될 수 있는 리소스. 환경 설정에서 IaC 관리가 켜져 있으면 코드 동기화 step이 자동 추가됨 |
| `long_running` | `true` | 완료까지 오래 걸림. 상태 확인을 반복(polling)하는 step이 필요 |

- 영향 정보가 없는 명령(문서 밖 폴백 포함)은 "영향 불명"으로 표시하고 높은 위험으로 취급한다.
- 이 정보는 정적 지식이므로 완벽하지 않다. 실제 "삭제 후 재생성" 판정은 IaC plan(Phase 3)의 몫이다.
- 쿠버네티스 명령은 `--dry-run=server`와 `kubectl diff`로 사전 확인할 수 있다. AWS CLI의 `--dry-run`은 권한 검사일 뿐 영향 분석이 아니다.

### 본문
- 명령마다 `## <name>` 제목 하나(frontmatter의 `name`과 동일). 검색 단위(chunk)가 된다.
- 항목: 설명, 키워드(한국어·영어 동의어), 예시, 주의, 확인.
- 예시는 반드시 플레이스홀더를 쓴다. 실제 값을 넣지 않는다.

### 플레이스홀더 타입
`<ACCOUNT_1>`, `<ARN_1>`, `<SG_1>`, `<VPC_1>`, `<SUBNET_1>`, `<INSTANCE_1>`, `<CLUSTER_1>`, `<NODEGROUP_1>`, `<BUCKET_1>`, `<ROLE_1>`, `<USER_1>`, `<POLICY_ARN_1>`, `<NAME_1>`(일반 이름), `<CIDR_1>`, `<NAMESPACE_1>`
- 리전, 서비스명, 포트, 프로토콜은 마스킹하지 않는다.

## 검증 항목 (`tests/check_docs.py`)
1. frontmatter 필수 필드와 값 범위
2. 명령이 실제 CLI에 존재하는지(`help` 종료 코드)
3. `required`/`optional` 옵션이 실제 도움말에 존재하는지. 필수 옵션 불일치는 경고
4. 본문에 명령별 `##` 제목이 있는지
5. `verify`가 같은 cli·service의 read 명령인지
6. `manual`/`restricted`에 `reason`이 있는지, 명령 중복이 없는지
7. `glossary.yaml` 구조
8. mutate·manual 명령의 `impact` 필드 존재와 값 범위, `preflight`가 같은 CLI의 read 명령인지
