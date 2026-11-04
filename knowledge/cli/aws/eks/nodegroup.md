---
cli: aws
service: eks
topic: nodegroup
commands:
  - {name: list-nodegroups, kind: read, risk: low, required: [--cluster-name], optional: []}
  - {name: describe-nodegroup, kind: read, risk: low, required: [--cluster-name, --nodegroup-name], optional: []}
  - {name: create-nodegroup, kind: mutate, risk: high, required: [--cluster-name, --nodegroup-name, --subnets, --node-role], optional: [--scaling-config, --instance-types, --disk-size, --labels], verify: describe-nodegroup, impact: {disruption: none, replaces: false, reversible: full, iac: true, long_running: true}}
  - {name: update-nodegroup-config, kind: mutate, risk: medium, required: [--cluster-name, --nodegroup-name], optional: [--scaling-config, --labels], verify: describe-nodegroup, impact: {disruption: possible, replaces: false, reversible: full, preflight: [describe-nodegroup], iac: true}}
  - {name: delete-nodegroup, kind: mutate, risk: high, required: [--cluster-name, --nodegroup-name], optional: [], verify: list-nodegroups, impact: {disruption: likely, replaces: false, reversible: irreversible, preflight: [describe-nodegroup], iac: true, long_running: true}}
---
# EKS 노드그룹

## list-nodegroups
- 설명: 클러스터의 노드그룹 이름 목록.
- 키워드: 노드그룹 목록, 워커 노드 그룹
- 예시: `aws eks list-nodegroups --cluster-name <CLUSTER_1>`

## describe-nodegroup
- 설명: 노드그룹 상세(상태, 스케일링 설정, 인스턴스 타입).
- 키워드: 노드그룹 상태, 노드 수 확인
- 예시: `aws eks describe-nodegroup --cluster-name <CLUSTER_1> --nodegroup-name <NODEGROUP_1>`

## create-nodegroup
- 설명: 관리형 노드그룹 생성. 서브넷과 노드 IAM 역할 ARN이 필수.
- 키워드: 노드그룹 생성, 워커 노드 추가, create nodegroup
- 예시: `aws eks create-nodegroup --cluster-name <CLUSTER_1> --nodegroup-name <NODEGROUP_1> --subnets <SUBNET_1> <SUBNET_2> --node-role <ARN_1> --scaling-config minSize=1,maxSize=3,desiredSize=2 --instance-types t3.medium`
- 주의: 비용이 발생한다. 노드 수(`desiredSize`)와 인스턴스 타입을 승인 화면에 표시한다.
- 확인: describe-nodegroup

## update-nodegroup-config
- 설명: 노드 수(스케일링)나 라벨 변경.
- 키워드: 노드 수 늘리기, 노드 줄이기, 스케일 조정
- 예시: `aws eks update-nodegroup-config --cluster-name <CLUSTER_1> --nodegroup-name <NODEGROUP_1> --scaling-config minSize=1,maxSize=5,desiredSize=3`
- 확인: describe-nodegroup

## delete-nodegroup
- 설명: 노드그룹 삭제. 그 위에서 도는 파드가 중단된다.
- 키워드: 노드그룹 삭제, 워커 노드 제거
- 예시: `aws eks delete-nodegroup --cluster-name <CLUSTER_1> --nodegroup-name <NODEGROUP_1>`
- 주의: 되돌릴 수 없다. 서비스 영향이 있으므로 prod면 경고한다.
- 확인: list-nodegroups
