---
cli: aws
service: autoscaling
topic: asg
commands:
  - {name: describe-auto-scaling-groups, kind: read, risk: low, required: [], optional: [--auto-scaling-group-names, --filters, --query]}
  - {name: describe-auto-scaling-instances, kind: read, risk: low, required: [], optional: [--instance-ids]}
  - {name: describe-instance-refreshes, kind: read, risk: low, required: [--auto-scaling-group-name], optional: [--instance-refresh-ids]}
  - {name: describe-scaling-activities, kind: read, risk: low, required: [], optional: [--auto-scaling-group-name, --max-items]}
  - {name: start-instance-refresh, kind: mutate, risk: high, required: [--auto-scaling-group-name], optional: [--strategy, --preferences, --desired-configuration], verify: describe-instance-refreshes, impact: {disruption: likely, replaces: false, reversible: partial, preflight: [describe-auto-scaling-groups, describe-instance-refreshes, "ec2:describe-launch-template-versions"], iac: true, long_running: true}}
  - {name: cancel-instance-refresh, kind: mutate, risk: medium, required: [--auto-scaling-group-name], optional: [], verify: describe-instance-refreshes, impact: {disruption: possible, replaces: false, reversible: full, preflight: [describe-instance-refreshes]}}
  - {name: update-auto-scaling-group, kind: mutate, risk: high, required: [--auto-scaling-group-name], optional: [--min-size, --max-size, --desired-capacity, --launch-template], verify: describe-auto-scaling-groups, impact: {disruption: possible, replaces: false, reversible: full, preflight: [describe-auto-scaling-groups], iac: true}}
  - {name: set-desired-capacity, kind: mutate, risk: medium, required: [--auto-scaling-group-name, --desired-capacity], optional: [--honor-cooldown], verify: describe-auto-scaling-groups, impact: {disruption: possible, replaces: false, reversible: partial, preflight: [describe-auto-scaling-groups], iac: true}}
  - {name: suspend-processes, kind: mutate, risk: medium, required: [--auto-scaling-group-name], optional: [--scaling-processes], verify: describe-auto-scaling-groups, impact: {disruption: possible, replaces: false, reversible: full, preflight: [describe-auto-scaling-groups]}}
  - {name: resume-processes, kind: mutate, risk: low, required: [--auto-scaling-group-name], optional: [--scaling-processes], verify: describe-auto-scaling-groups, impact: {disruption: none, replaces: false, reversible: full}}
  - {name: terminate-instance-in-auto-scaling-group, kind: mutate, risk: high, required: [--should-decrement-desired-capacity], optional: [--instance-id, --instance-ids, --auto-scaling-group-name], verify: describe-auto-scaling-groups, impact: {disruption: likely, replaces: false, reversible: irreversible, preflight: [describe-auto-scaling-groups]}}
---
# Auto Scaling 그룹 (ASG)

self-managed 노드를 ASG로 운영하는 환경에서 노드 교체·확장에 쓴다. 노드 AMI는 launch template(ec2 문서)에서 바뀌고, 기존 인스턴스는 **인스턴스 리프레시**로 새 AMI 인스턴스로 교체된다. Terraform으로 ASG를 관리한다면 리프레시 설정과 launch template 변경은 코드에서 하고, 이 명령은 읽기 점검과 긴급 조치에 쓴다.

## describe-auto-scaling-groups
- 설명: ASG 상세(최소/최대/희망 용량, launch template 버전, 인스턴스 목록, 상태).
- 키워드: ASG 조회, 오토스케일링 그룹 확인, 노드 그룹 용량, asg
- 예시: `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <NAME_1>`

## describe-auto-scaling-instances
- 설명: ASG 인스턴스의 수명 주기 상태(InService, Terminating 등).
- 키워드: ASG 인스턴스 상태
- 예시: `aws autoscaling describe-auto-scaling-instances --instance-ids <INSTANCE_1>`

## describe-instance-refreshes
- 설명: 인스턴스 리프레시 진행 상태(진행률, 상태, 이력). 노드 교체 진행을 확인할 때 반복 조회한다.
- 키워드: 인스턴스 리프레시 상태, 노드 교체 진행, instance refresh
- 예시: `aws autoscaling describe-instance-refreshes --auto-scaling-group-name <NAME_1>`

## describe-scaling-activities
- 설명: 최근 스케일링 활동(인스턴스 시작·종료 기록과 실패 원인).
- 키워드: 스케일링 이력, 인스턴스 종료 이유
- 예시: `aws autoscaling describe-scaling-activities --auto-scaling-group-name <NAME_1> --max-items 20`

## start-instance-refresh
- 설명: ASG의 인스턴스를 launch template 최신 설정(AMI 등)으로 롤링 교체. `MinHealthyPercentage`, `InstanceWarmup`으로 교체 속도와 최소 정상 비율을 정한다.
- 키워드: 인스턴스 리프레시, 노드 교체, 노드 롤링 업그레이드, AMI 반영, start instance refresh
- 예시: `aws autoscaling start-instance-refresh --auto-scaling-group-name <NAME_1> --strategy Rolling --preferences MinHealthyPercentage=90,InstanceWarmup=300`
- 주의: 노드가 교체되며 파드가 재스케줄되어 서비스 중단이 생길 수 있다. 실행 전 PDB, 레플리카 수, 여유 용량을 확인한다. 오래 걸리고 도중 취소해도 이미 교체된 인스턴스는 되돌아가지 않는다. 교체 시 노드를 drain하지 않으면 파드가 갑자기 종료될 수 있으므로 종료 훅(lifecycle hook) 또는 노드 종료 핸들러 구성이 있는지 확인한다. Terraform 관리 ASG는 코드와 동기화해야 한다.
- 확인: describe-instance-refreshes

## cancel-instance-refresh
- 설명: 진행 중인 리프레시 취소. 이미 교체된 인스턴스는 그대로다.
- 키워드: 리프레시 취소, 노드 교체 중단
- 예시: `aws autoscaling cancel-instance-refresh --auto-scaling-group-name <NAME_1>`
- 확인: describe-instance-refreshes

## update-auto-scaling-group
- 설명: ASG 설정 변경(용량 범위, launch template 버전 등).
- 키워드: ASG 수정, 최소 최대 변경, launch template 버전 변경
- 예시: `aws autoscaling update-auto-scaling-group --auto-scaling-group-name <NAME_1> --min-size 2 --max-size 6`
- 주의: launch template 버전 변경은 새로 뜨는 인스턴스에만 적용된다. 기존 인스턴스는 리프레시가 필요하다. Terraform 관리 ASG는 코드와 동기화해야 한다.
- 확인: describe-auto-scaling-groups

## set-desired-capacity
- 설명: 희망 용량 변경. 줄이면 인스턴스가 종료된다.
- 키워드: 노드 수 변경, 스케일 아웃, 스케일 인
- 예시: `aws autoscaling set-desired-capacity --auto-scaling-group-name <NAME_1> --desired-capacity 4`
- 주의: 용량을 줄이면 어떤 인스턴스가 종료될지 ASG 정책이 정한다. 파드가 있는 노드가 종료될 수 있다.
- 확인: describe-auto-scaling-groups

## suspend-processes
- 설명: ASG 프로세스(AZRebalance, Launch, Terminate 등) 일시 중지. 업그레이드 중 의도치 않은 재조정을 막을 때 쓴다.
- 키워드: ASG 프로세스 중지, AZRebalance 중지
- 예시: `aws autoscaling suspend-processes --auto-scaling-group-name <NAME_1> --scaling-processes AZRebalance`
- 주의: 중지한 프로세스는 반드시 다시 재개해야 한다. 계획에 `resume-processes` step을 짝으로 넣는다.
- 확인: describe-auto-scaling-groups

## resume-processes
- 설명: 중지된 ASG 프로세스 재개.
- 키워드: ASG 프로세스 재개
- 예시: `aws autoscaling resume-processes --auto-scaling-group-name <NAME_1> --scaling-processes AZRebalance`
- 확인: describe-auto-scaling-groups

## terminate-instance-in-auto-scaling-group
- 설명: ASG 인스턴스를 종료. 대상은 `--instance-id` 하나, 또는 `--instance-ids`(여러 개), `--auto-scaling-group-name`(그룹 단위)로도 지정할 수 있다. `--should-decrement-desired-capacity`면 희망 용량도 줄이고, `--no-should-decrement-desired-capacity`면 새 인스턴스가 대신 뜬다.
- 키워드: 노드 하나 종료, ASG 인스턴스 종료, 노드 교체
- 예시: `aws autoscaling terminate-instance-in-auto-scaling-group --instance-id <INSTANCE_1> --no-should-decrement-desired-capacity`
- 주의: 되돌릴 수 없다. 해당 노드는 먼저 `kubectl cordon`과 `drain`으로 비우는 것이 안전하다. `--instance-ids`나 `--auto-scaling-group-name`을 쓰면 한 번에 여러 노드가 종료될 수 있으므로 대상 개수를 승인 화면에 강조한다. 계획에는 한 번에 인스턴스 하나만 지정하도록 한다.
- 확인: describe-auto-scaling-groups
