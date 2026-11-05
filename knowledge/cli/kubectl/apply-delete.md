---
cli: kubectl
service: kubectl
topic: apply-delete
commands:
  - {name: apply, kind: mutate, risk: medium, required: [--filename], optional: [--filename, --namespace, --dry-run, --selector], verify: get, guard: [file-input], impact: {disruption: possible, replaces: false, reversible: partial, preflight: [get], dry_run: "--dry-run=server (kubectl diff 권장)", iac: true}}
  - {name: create, kind: mutate, risk: medium, required: [], optional: [--filename, --namespace, --dry-run], verify: get, guard: [file-input], impact: {disruption: none, replaces: false, reversible: full, preflight: [get], dry_run: "--dry-run=server", iac: true}}
  - {name: delete, kind: mutate, risk: high, required: [], optional: [--filename, --namespace, --selector, --all, --all-namespaces, --dry-run, --wait], verify: get, guard: [delete-all], impact: {disruption: likely, replaces: false, reversible: irreversible, preflight: [get, describe], dry_run: "--dry-run=server", iac: true}}
  - {name: patch, kind: mutate, risk: medium, required: [], optional: [--patch, --type, --namespace, --dry-run], verify: describe, impact: {disruption: possible, replaces: false, reversible: partial, preflight: [describe], dry_run: "--dry-run=server", iac: true}}
  - {name: label, kind: mutate, risk: low, required: [], optional: [--overwrite, --namespace, --selector], verify: get, impact: {disruption: possible, replaces: false, reversible: full, preflight: [get], dry_run: "--dry-run=server", iac: true}}
  - {name: annotate, kind: mutate, risk: low, required: [], optional: [--overwrite, --namespace, --selector], verify: describe, impact: {disruption: none, replaces: false, reversible: full, preflight: [describe], dry_run: "--dry-run=server", iac: true}}
  - {name: scale, kind: mutate, risk: medium, required: [--replicas], optional: [--namespace, --current-replicas], verify: get, impact: {disruption: possible, replaces: false, reversible: full, preflight: [get], iac: true}}
  - {name: set image, kind: mutate, risk: medium, required: [], optional: [--namespace, --all], verify: describe, impact: {disruption: possible, replaces: false, reversible: full, preflight: [describe, "rollout history"], dry_run: "--dry-run=server", iac: true}}
---
# kubectl 생성·변경·삭제

변경 전에 `--dry-run=server`로 먼저 검증하는 step을 계획에 넣는 것을 권장한다. 매니페스트(YAML) 파일은 사용자가 커스텀 step으로 제공하며, 내용은 승인 화면에 그대로 보여준다.

## apply
- 설명: 매니페스트를 선언적으로 적용(없으면 생성, 있으면 갱신).
- 키워드: 배포, 매니페스트 적용, yaml 적용, apply
- 예시: `kubectl apply --filename <NAME_1> --namespace <NAMESPACE_1> --dry-run=server`
- 주의: 파일 내용에 따라 영향 범위가 달라진다. 가드는 파일 입력(`file-input`)을 승인 화면에 내용과 함께 노출하도록 요구한다.
- 확인: get

## create
- 설명: 리소스 생성(이미 있으면 실패). 매니페스트 또는 `create namespace <이름>` 같은 하위 형태.
- 키워드: 네임스페이스 생성, 리소스 생성, create
- 예시: `kubectl create namespace <NAMESPACE_1>`
- 예시(파일): `kubectl create --filename <NAME_1> --namespace <NAMESPACE_1>`
- 확인: get

## delete
- 설명: 리소스 삭제. 이름, 라벨 셀렉터, 파일로 지정한다.
- 키워드: 파드 삭제, 배포 삭제, 네임스페이스 삭제, delete
- 예시: `kubectl delete pod <NAME_1> --namespace <NAMESPACE_1>`
- 예시(라벨): `kubectl delete pods --selector app=<NAME_1> --namespace <NAMESPACE_1> --dry-run=server`
- 주의: `--all`, `--all-namespaces`, 네임스페이스 삭제는 안의 모든 리소스를 지운다. 가드가 `delete-all`로 차단 또는 강한 경고한다. prod 네임스페이스면 추가 경고.
- 확인: get

## patch
- 설명: 리소스 일부를 JSON/YAML 패치로 수정.
- 키워드: 부분 수정, 패치
- 예시: `kubectl patch deployment <NAME_1> --namespace <NAMESPACE_1> --patch '{"spec":{"replicas":3}}'`
- 확인: describe

## label
- 설명: 라벨 추가/수정. 라벨은 셀렉터(서비스 연결 등)에 쓰이므로 영향이 있다.
- 키워드: 라벨 붙이기, 라벨 변경
- 예시: `kubectl label pod <NAME_1> --namespace <NAMESPACE_1> --overwrite tier=backend`
- 확인: get

## annotate
- 설명: 어노테이션 추가/수정.
- 키워드: 어노테이션 변경
- 예시: `kubectl annotate deployment <NAME_1> --namespace <NAMESPACE_1> --overwrite owner=<NAME_2>`
- 확인: describe

## scale
- 설명: 레플리카 수 조정. 0으로 만들면 서비스가 중단된다.
- 키워드: 파드 수 늘리기, 스케일 아웃, 스케일 인, 레플리카 변경
- 예시: `kubectl scale deployment <NAME_1> --replicas 3 --namespace <NAMESPACE_1>`
- 주의: `--replicas 0`은 서비스 중단이다. 승인 화면에 강조한다.
- 확인: get

## set image
- 설명: 배포의 컨테이너 이미지 변경(롤링 업데이트가 시작된다).
- 키워드: 이미지 변경, 버전 배포, 이미지 업데이트
- 예시: `kubectl set image deployment/<NAME_1> <NAME_2>=<NAME_3>:1.2.3 --namespace <NAMESPACE_1>`
- 주의: 롤아웃 진행 상태를 `rollout status`로 확인한다. 문제가 생기면 `rollout undo`.
- 확인: describe
