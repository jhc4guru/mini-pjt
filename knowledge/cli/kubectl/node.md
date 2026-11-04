---
cli: kubectl
service: kubectl
topic: node
commands:
  - {name: cordon, kind: mutate, risk: medium, required: [], optional: [--selector, --dry-run], verify: get, impact: {disruption: none, replaces: false, reversible: full, preflight: [get], dry_run: "--dry-run=server"}}
  - {name: uncordon, kind: mutate, risk: low, required: [], optional: [--selector, --dry-run], verify: get, impact: {disruption: none, replaces: false, reversible: full, preflight: [get], dry_run: "--dry-run=server"}}
  - {name: drain, kind: mutate, risk: high, required: [], optional: [--ignore-daemonsets, --delete-emptydir-data, --force, --grace-period, --timeout, --dry-run], verify: get, impact: {disruption: likely, replaces: false, reversible: partial, preflight: [get, describe], dry_run: "--dry-run=server", long_running: true}}
  - {name: taint, kind: mutate, risk: high, required: [], optional: [--overwrite, --all, --selector, --dry-run], verify: describe, impact: {disruption: possible, replaces: false, reversible: full, preflight: [describe], dry_run: "--dry-run=server"}}
---
# kubectl 노드 관리

노드 단위 작업은 여러 서비스에 영향을 준다. 대상 노드를 먼저 `get nodes`와 `describe node`로 확인한다.

## cordon
- 설명: 노드를 스케줄 불가로 표시(새 파드 배치 금지). 기존 파드는 그대로 유지된다.
- 키워드: 노드 격리, 스케줄링 막기
- 예시: `kubectl cordon <NAME_1>`
- 확인: get

## uncordon
- 설명: 노드를 다시 스케줄 가능으로 되돌림.
- 키워드: 노드 복구, 스케줄링 재개
- 예시: `kubectl uncordon <NAME_1>`
- 확인: get

## drain
- 설명: 노드의 파드를 안전하게 내보낸 뒤 비운다(노드 점검·교체 전).
- 키워드: 노드 비우기, 노드 점검 준비, drain
- 예시: `kubectl drain <NAME_1> --ignore-daemonsets --delete-emptydir-data --timeout 300s --dry-run=server`
- 주의: 실행 전에 `--dry-run=server`로 대상 파드를 확인한다. `--force`는 컨트롤러가 없는 파드를 영구 삭제할 수 있다. `--delete-emptydir-data`는 emptyDir 데이터를 지운다.
- 확인: get

## taint
- 설명: 노드에 taint를 설정해 특정 파드만 배치되게 하거나 배치를 막는다. `NoExecute`는 기존 파드도 내쫓는다.
- 키워드: 노드 taint, 노드 격리 설정
- 예시: `kubectl taint nodes <NAME_1> dedicated=<NAME_2>:NoSchedule --overwrite`
- 주의: `NoExecute`는 즉시 파드를 퇴출한다. 승인 화면에 강조한다.
- 확인: describe
