---
cli: kubectl
service: kubectl
topic: rollout
commands:
  - {name: rollout status, kind: read, risk: low, required: [], optional: [--namespace, --timeout, --watch]}
  - {name: rollout history, kind: read, risk: low, required: [], optional: [--namespace, --revision]}
  - {name: rollout restart, kind: mutate, risk: medium, required: [], optional: [--namespace], verify: rollout status, impact: {disruption: possible, replaces: false, reversible: full, preflight: [get]}}
  - {name: rollout undo, kind: mutate, risk: medium, required: [], optional: [--namespace, --to-revision], verify: rollout status, impact: {disruption: possible, replaces: false, reversible: partial, preflight: ["rollout history"], iac: true}}
  - {name: rollout pause, kind: mutate, risk: low, required: [], optional: [--namespace], verify: rollout status, impact: {disruption: none, replaces: false, reversible: full}}
  - {name: rollout resume, kind: mutate, risk: low, required: [], optional: [--namespace], verify: rollout status, impact: {disruption: none, replaces: false, reversible: full}}
---
# kubectl 롤아웃

Deployment, StatefulSet, DaemonSet의 배포 상태 확인과 제어. 대상은 `deployment/<이름>` 형태로 지정한다.

## rollout status
- 설명: 롤아웃이 끝날 때까지 기다리며 진행 상태를 보여준다. 기본은 완료까지 대기하므로 반드시 `--timeout`을 준다.
- 키워드: 배포 상태, 롤아웃 확인, 배포 끝났는지
- 예시: `kubectl rollout status deployment/<NAME_1> --namespace <NAMESPACE_1> --timeout 120s`
- 주의: `--timeout` 없이 쓰면 오래 대기할 수 있다. 가드가 타임아웃을 강제한다.

## rollout history
- 설명: 리비전 이력 조회.
- 키워드: 배포 이력, 리비전 확인, 이전 버전
- 예시: `kubectl rollout history deployment/<NAME_1> --namespace <NAMESPACE_1>`

## rollout restart
- 설명: 파드를 순차 재시작(롤링). 이미지/설정 변경 없이 새 파드로 교체.
- 키워드: 재시작, 파드 재기동, restart
- 예시: `kubectl rollout restart deployment/<NAME_1> --namespace <NAMESPACE_1>`
- 확인: rollout status

## rollout undo
- 설명: 이전 리비전으로 되돌림(`--to-revision`으로 지정 가능).
- 키워드: 롤백, 되돌리기, 이전 버전으로
- 예시: `kubectl rollout undo deployment/<NAME_1> --namespace <NAMESPACE_1> --to-revision 2`
- 주의: 먼저 `rollout history`로 되돌릴 리비전을 확인한다.
- 확인: rollout status

## rollout pause
- 설명: 롤아웃 일시 중지(변경을 여러 개 묶어 적용할 때 사용).
- 키워드: 배포 일시정지
- 예시: `kubectl rollout pause deployment/<NAME_1> --namespace <NAMESPACE_1>`
- 확인: rollout status

## rollout resume
- 설명: 일시 중지된 롤아웃 재개.
- 키워드: 배포 재개
- 예시: `kubectl rollout resume deployment/<NAME_1> --namespace <NAMESPACE_1>`
- 확인: rollout status
