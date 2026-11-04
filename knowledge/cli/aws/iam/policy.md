---
cli: aws
service: iam
topic: policy
commands:
  - {name: list-policies, kind: read, risk: low, required: [], optional: [--scope, --only-attached, --query]}
  - {name: get-policy, kind: read, risk: low, required: [--policy-arn], optional: []}
  - {name: list-policy-versions, kind: read, risk: low, required: [--policy-arn], optional: []}
  - {name: get-policy-version, kind: read, risk: low, required: [--policy-arn, --version-id], optional: []}
  - {name: create-policy, kind: mutate, risk: high, required: [--policy-name, --policy-document], optional: [--description], verify: list-policies, guard: [wildcard-action, file-input], impact: {disruption: none, replaces: false, reversible: full, iac: true}}
  - {name: create-policy-version, kind: mutate, risk: high, required: [--policy-arn, --policy-document], optional: [--set-as-default], verify: list-policy-versions, guard: [wildcard-action, file-input], impact: {disruption: possible, replaces: false, reversible: full, preflight: [list-policy-versions, get-policy-version], iac: true}}
  - {name: delete-policy, kind: mutate, risk: high, required: [--policy-arn], optional: [], verify: list-policies, impact: {disruption: possible, replaces: false, reversible: irreversible, preflight: [get-policy], iac: true}}
---
# IAM 정책 (Policy)

## list-policies
- 설명: 정책 목록. `--scope Local`이면 고객 관리형만 나온다(AWS 관리형 제외).
- 키워드: 정책 목록, 커스텀 정책 조회
- 예시: `aws iam list-policies --scope Local --query "Policies[].PolicyName"`

## get-policy
- 설명: 정책 메타데이터(기본 버전, 연결 개수).
- 키워드: 정책 상세
- 예시: `aws iam get-policy --policy-arn <POLICY_ARN_1>`

## list-policy-versions
- 설명: 정책 버전 목록. 정책당 최대 5개 버전.
- 키워드: 정책 버전 확인
- 예시: `aws iam list-policy-versions --policy-arn <POLICY_ARN_1>`

## get-policy-version
- 설명: 특정 버전의 정책 JSON 조회. 정책 내용을 확인할 때 쓴다.
- 키워드: 정책 내용 확인, 권한 내용 보기
- 예시: `aws iam get-policy-version --policy-arn <POLICY_ARN_1> --version-id v1`

## create-policy
- 설명: 고객 관리형 정책 생성.
- 키워드: 정책 생성, policy 만들기
- 예시: `aws iam create-policy --policy-name <NAME_1> --policy-document file://<NAME_2>`
- 주의: `Action`/`Resource`에 `*`가 있으면 광범위 권한이다(`wildcard-action`). 생성만으로는 효과가 없고 연결해야 적용된다.
- 확인: list-policies

## create-policy-version
- 설명: 정책 새 버전 생성. `--set-as-default`를 주면 즉시 적용된다.
- 키워드: 정책 수정, 권한 변경, 정책 업데이트
- 예시: `aws iam create-policy-version --policy-arn <POLICY_ARN_1> --policy-document file://<NAME_1> --set-as-default`
- 주의: 즉시 연결된 모든 대상에 적용된다. 버전 5개 제한이 있어 오래된 버전을 지워야 할 수 있다.
- 확인: list-policy-versions

## delete-policy
- 설명: 정책 삭제. 연결된 대상이 있거나 기본 외 버전이 남아 있으면 실패한다.
- 키워드: 정책 삭제
- 예시: `aws iam delete-policy --policy-arn <POLICY_ARN_1>`
- 확인: list-policies
