---
cli: aws
service: iam
topic: role
commands:
  - {name: list-roles, kind: read, risk: low, required: [], optional: [--path-prefix, --query]}
  - {name: get-role, kind: read, risk: low, required: [--role-name], optional: []}
  - {name: list-attached-role-policies, kind: read, risk: low, required: [--role-name], optional: []}
  - {name: list-role-policies, kind: read, risk: low, required: [--role-name], optional: []}
  - {name: get-role-policy, kind: read, risk: low, required: [--role-name, --policy-name], optional: []}
  - {name: create-role, kind: mutate, risk: high, required: [--role-name, --assume-role-policy-document], optional: [--description, --max-session-duration], verify: get-role, guard: [wildcard-principal, file-input], impact: {disruption: none, replaces: false, reversible: full, iac: true}}
  - {name: attach-role-policy, kind: mutate, risk: high, required: [--role-name, --policy-arn], optional: [], verify: list-attached-role-policies, guard: [admin-policy], impact: {disruption: none, replaces: false, reversible: full, preflight: [list-attached-role-policies], iac: true}}
  - {name: detach-role-policy, kind: mutate, risk: high, required: [--role-name, --policy-arn], optional: [], verify: list-attached-role-policies, impact: {disruption: possible, replaces: false, reversible: full, preflight: [list-attached-role-policies], iac: true}}
  - {name: put-role-policy, kind: mutate, risk: high, required: [--role-name, --policy-name, --policy-document], optional: [], verify: get-role-policy, guard: [wildcard-action, file-input], impact: {disruption: possible, replaces: false, reversible: partial, preflight: [list-role-policies, get-role-policy], iac: true}}
  - {name: delete-role, kind: mutate, risk: high, required: [--role-name], optional: [], verify: list-roles, impact: {disruption: possible, replaces: false, reversible: irreversible, preflight: [list-attached-role-policies, list-role-policies], iac: true}}
---
# IAM 역할 (Role)

IAM 변경은 영향 범위가 넓어 모두 risk `high`다. 정책 JSON은 `file://<파일>` 또는 문자열로 전달하며, 내용은 승인 화면에 그대로 보여준다.

## list-roles
- 설명: 계정의 역할 목록.
- 키워드: 역할 목록, role 조회
- 예시: `aws iam list-roles --query "Roles[].RoleName"`

## get-role
- 설명: 역할 상세(신뢰 정책, ARN 등).
- 키워드: 역할 상세, 신뢰 정책 확인
- 예시: `aws iam get-role --role-name <ROLE_1>`

## list-attached-role-policies
- 설명: 역할에 붙은 관리형 정책 목록.
- 키워드: 역할 권한 확인, 연결된 정책
- 예시: `aws iam list-attached-role-policies --role-name <ROLE_1>`

## list-role-policies
- 설명: 역할의 인라인 정책 이름 목록.
- 키워드: 인라인 정책 목록
- 예시: `aws iam list-role-policies --role-name <ROLE_1>`

## get-role-policy
- 설명: 인라인 정책 내용 조회.
- 키워드: 인라인 정책 확인
- 예시: `aws iam get-role-policy --role-name <ROLE_1> --policy-name <NAME_1>`

## create-role
- 설명: 역할 생성. 신뢰 정책(누가 이 역할을 맡을 수 있는지)이 필수.
- 키워드: 역할 생성, role 만들기, create role
- 예시: `aws iam create-role --role-name <ROLE_1> --assume-role-policy-document file://<NAME_1>`
- 주의: 신뢰 정책의 `Principal`이 `*`면 누구나 맡을 수 있다. 가드가 `wildcard-principal`로 검사한다. 역할 생성만으로는 권한이 없다(정책을 별도로 붙여야 함).
- 확인: get-role

## attach-role-policy
- 설명: 관리형 정책을 역할에 연결.
- 키워드: 권한 부여, 정책 연결, attach policy
- 예시: `aws iam attach-role-policy --role-name <ROLE_1> --policy-arn <POLICY_ARN_1>`
- 주의: `AdministratorAccess`, `*FullAccess` 등 광범위 정책은 가드가 `admin-policy`로 강하게 경고한다. 최소 권한 원칙을 검토한다.
- 확인: list-attached-role-policies

## detach-role-policy
- 설명: 역할에서 관리형 정책 연결 해제.
- 키워드: 권한 회수, 정책 해제
- 예시: `aws iam detach-role-policy --role-name <ROLE_1> --policy-arn <POLICY_ARN_1>`
- 주의: 해당 역할을 쓰는 서비스가 권한 오류로 중단될 수 있다.
- 확인: list-attached-role-policies

## put-role-policy
- 설명: 인라인 정책 생성/교체(같은 이름이면 덮어씀).
- 키워드: 인라인 정책 적용
- 예시: `aws iam put-role-policy --role-name <ROLE_1> --policy-name <NAME_1> --policy-document file://<NAME_2>`
- 주의: `"Action": "*"`, `"Resource": "*"` 조합은 광범위 권한이다. 가드가 `wildcard-action`으로 검사한다.
- 확인: get-role-policy

## delete-role
- 설명: 역할 삭제. 연결된 정책·인스턴스 프로파일이 남아 있으면 실패한다.
- 키워드: 역할 삭제
- 예시: `aws iam delete-role --role-name <ROLE_1>`
- 주의: 먼저 연결된 정책을 모두 해제해야 한다.
- 확인: list-roles
