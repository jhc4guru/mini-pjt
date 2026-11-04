---
cli: aws
service: iam
topic: user
commands:
  - {name: list-users, kind: read, risk: low, required: [], optional: [--path-prefix, --query]}
  - {name: get-user, kind: read, risk: low, required: [], optional: [--user-name]}
  - {name: list-access-keys, kind: read, risk: low, required: [], optional: [--user-name]}
  - {name: list-attached-user-policies, kind: read, risk: low, required: [--user-name], optional: []}
  - {name: create-user, kind: mutate, risk: high, required: [--user-name], optional: [--path, --tags], verify: get-user, impact: {disruption: none, replaces: false, reversible: full, iac: true}}
  - {name: delete-user, kind: mutate, risk: high, required: [--user-name], optional: [], verify: list-users, impact: {disruption: possible, replaces: false, reversible: irreversible, preflight: [list-access-keys, list-attached-user-policies], iac: true}}
  - {name: attach-user-policy, kind: mutate, risk: high, required: [--user-name, --policy-arn], optional: [], verify: list-attached-user-policies, guard: [admin-policy], impact: {disruption: none, replaces: false, reversible: full, preflight: [list-attached-user-policies], iac: true}}
  - {name: detach-user-policy, kind: mutate, risk: high, required: [--user-name, --policy-arn], optional: [], verify: list-attached-user-policies, impact: {disruption: possible, replaces: false, reversible: full, preflight: [list-attached-user-policies], iac: true}}
  - {name: update-access-key, kind: mutate, risk: high, required: [--access-key-id, --status], optional: [--user-name], verify: list-access-keys, impact: {disruption: possible, replaces: false, reversible: full, preflight: [list-access-keys]}}
  - {name: delete-access-key, kind: mutate, risk: high, required: [--access-key-id], optional: [--user-name], verify: list-access-keys, impact: {disruption: possible, replaces: false, reversible: irreversible, preflight: [list-access-keys]}}
  - {name: create-access-key, kind: manual, risk: high, required: [], optional: [--user-name], reason: "응답에 시크릿 키가 평문으로 한 번만 포함됨. 서버가 실행하면 화면·이력·LLM 경계에 남으므로 사람이 자기 터미널에서 직접 실행", guard: [output-secret], impact: {disruption: none, replaces: false, reversible: full}}
  - {name: create-login-profile, kind: manual, risk: high, required: [], optional: [--user-name, --password, --password-reset-required], reason: "비밀번호가 명령행 인자로 들어감. 입력창·이력·LLM 경계에 비밀값이 남지 않도록 사람이 자기 터미널에서 직접 채워 실행", guard: [input-secret], impact: {disruption: none, replaces: false, reversible: full}}
---
# IAM 사용자 (User)

장기 자격증명(액세스 키, 비밀번호)이 얽혀 있어 제한이 많다. `manual` 명령은 서버가 실행하지 않고, 명령 문구만 보여준다. 사람이 자기 터미널에서 직접 실행하므로 시크릿이 이 시스템에 남지 않는다.

## list-users
- 설명: IAM 사용자 목록.
- 키워드: 사용자 목록, user 조회
- 예시: `aws iam list-users --query "Users[].UserName"`

## get-user
- 설명: 사용자 상세. `--user-name`을 생략하면 현재 자격증명의 사용자.
- 키워드: 사용자 상세, 계정 확인
- 예시: `aws iam get-user --user-name <USER_1>`

## list-access-keys
- 설명: 사용자의 액세스 키 ID·상태·생성일 목록(시크릿은 나오지 않는다). 오래된 키 점검에 쓴다.
- 키워드: 액세스 키 목록, 키 사용 현황
- 예시: `aws iam list-access-keys --user-name <USER_1>`

## list-attached-user-policies
- 설명: 사용자에게 연결된 관리형 정책 목록.
- 키워드: 사용자 권한 확인
- 예시: `aws iam list-attached-user-policies --user-name <USER_1>`

## create-user
- 설명: IAM 사용자 생성. 생성만으로는 권한과 자격증명이 없다.
- 키워드: 사용자 생성, 계정 만들기
- 예시: `aws iam create-user --user-name <USER_1>`
- 확인: get-user

## delete-user
- 설명: 사용자 삭제. 액세스 키, 정책, 그룹 등이 남아 있으면 실패한다.
- 키워드: 사용자 삭제
- 예시: `aws iam delete-user --user-name <USER_1>`
- 확인: list-users

## attach-user-policy
- 설명: 관리형 정책을 사용자에게 연결.
- 키워드: 사용자 권한 부여
- 예시: `aws iam attach-user-policy --user-name <USER_1> --policy-arn <POLICY_ARN_1>`
- 주의: 광범위 정책은 `admin-policy`로 강하게 경고한다. 역할(Role) 사용을 우선 권장한다.
- 확인: list-attached-user-policies

## detach-user-policy
- 설명: 사용자에서 정책 연결 해제.
- 키워드: 사용자 권한 회수
- 예시: `aws iam detach-user-policy --user-name <USER_1> --policy-arn <POLICY_ARN_1>`
- 확인: list-attached-user-policies

## update-access-key
- 설명: 액세스 키 활성/비활성 전환. 삭제 전에 비활성화로 영향을 먼저 확인하는 용도.
- 키워드: 키 비활성화, 액세스 키 정지
- 예시: `aws iam update-access-key --user-name <USER_1> --access-key-id <NAME_1> --status Inactive`
- 확인: list-access-keys

## delete-access-key
- 설명: 액세스 키 삭제. 되돌릴 수 없다.
- 키워드: 액세스 키 삭제, 키 폐기
- 예시: `aws iam delete-access-key --user-name <USER_1> --access-key-id <NAME_1>`
- 주의: 먼저 `update-access-key --status Inactive`로 비활성화해 영향이 없는지 확인하는 순서를 계획에 포함하는 것이 안전하다.
- 확인: list-access-keys

## create-access-key
- 설명: **수동 실행.** 새 액세스 키 생성. 응답에 시크릿 키가 평문으로 한 번만 나오므로 서버가 실행하지 않는다. 요청자가 자기 터미널에서 실행하고, 시크릿은 그 터미널에만 나타난다.
- 키워드: 액세스 키 발급, 키 생성, create access key
- 예시: `aws iam create-access-key --user-name <USER_1>`
- 주의: 출력된 시크릿을 이 도구의 입력창이나 결과 붙여넣기에 넣지 않는다(입력 가드가 차단한다). 사용자당 키는 최대 2개다.

## create-login-profile
- 설명: **수동 실행.** 콘솔 로그인 비밀번호 설정. 비밀번호가 명령행 인자로 필요하므로 서버가 실행하지 않는다.
- 키워드: 콘솔 비밀번호 설정, 로그인 프로파일
- 예시: `aws iam create-login-profile --user-name <USER_1> --password <PASSWORD> --password-reset-required`
- 주의: `<PASSWORD>`는 자리 표시일 뿐이다. 도구는 비밀번호 값을 받지 않고 만들지도 않는다. 사람이 자기 터미널에서 직접 채운다. 입력창에 비밀번호를 쓰면 입력 가드가 차단한다. `--password-reset-required`로 첫 로그인 때 변경하게 한다.
