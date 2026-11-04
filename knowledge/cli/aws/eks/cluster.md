---
cli: aws
service: eks
topic: cluster
commands:
  - {name: list-clusters, kind: read, risk: low, required: [], optional: [--max-items]}
  - {name: describe-cluster, kind: read, risk: low, required: [--name], optional: []}
  - {name: describe-update, kind: read, risk: low, required: [--name, --update-id], optional: [--nodegroup-name]}
  - {name: list-updates, kind: read, risk: low, required: [--name], optional: [--nodegroup-name, --addon-name]}
  - {name: create-cluster, kind: mutate, risk: high, required: [--name, --role-arn, --resources-vpc-config], optional: [--kubernetes-version, --tags], verify: describe-cluster, impact: {disruption: none, replaces: false, reversible: full, iac: true, long_running: true}}
  - {name: update-cluster-version, kind: mutate, risk: high, required: [--name, --kubernetes-version], optional: [], verify: describe-cluster, impact: {disruption: possible, replaces: false, reversible: irreversible, preflight: [list-insights, describe-cluster, describe-addon-versions], iac: true, long_running: true}}
  - {name: delete-cluster, kind: mutate, risk: high, required: [--name], optional: [], verify: list-clusters, impact: {disruption: likely, replaces: false, reversible: irreversible, preflight: [list-nodegroups], iac: true, long_running: true}}
  - {name: update-kubeconfig, kind: manual, risk: low, reason: "서버 전역 kubeconfig를 바꾼다. 사람이 자기 터미널에서 실행", required: [--name], optional: [--alias, --region], guard: [local-file], impact: {disruption: none, replaces: false, reversible: full}}
---
# EKS 클러스터 (aws eks)

클러스터 자체의 생성·조회·삭제는 `aws eks`로, 클러스터 안의 파드·배포는 `kubectl`로 다룬다(kubectl 문서 참조).

## list-clusters
- 설명: 리전의 EKS 클러스터 이름 목록.
- 키워드: 클러스터 목록, EKS 조회, list clusters
- 예시: `aws eks list-clusters`

## describe-cluster
- 설명: 클러스터 상세(상태, 버전, 엔드포인트, VPC 설정).
- 키워드: 클러스터 상태, 버전 확인, EKS 상세
- 예시: `aws eks describe-cluster --name <CLUSTER_1>`

## describe-update
- 설명: 클러스터/노드그룹 업데이트 진행 상태 조회.
- 키워드: 업그레이드 진행, 업데이트 상태
- 예시: `aws eks describe-update --name <CLUSTER_1> --update-id <NAME_1>`

## list-updates
- 설명: 클러스터(또는 애드온)에 진행됐거나 진행 중인 업데이트 ID 목록. 진행 상태는 `describe-update`로 본다.
- 키워드: 업데이트 이력, 업그레이드 진행 중인지, 업데이트 목록
- 예시: `aws eks list-updates --name <CLUSTER_1>`

## create-cluster
- 설명: 클러스터 생성. 역할 ARN과 VPC 설정(서브넷, SG)이 필수. 완료까지 10분 이상 걸린다.
- 키워드: 클러스터 생성, EKS 만들기, create cluster
- 예시: `aws eks create-cluster --name <CLUSTER_1> --role-arn <ARN_1> --resources-vpc-config subnetIds=<SUBNET_1>,<SUBNET_2>,securityGroupIds=<SG_1>`
- 주의: 비용이 발생하고 오래 걸린다. 실행 후 상태가 `ACTIVE`가 될 때까지 확인 step을 반복해야 한다.
- 확인: describe-cluster

## update-cluster-version
- 설명: 컨트롤 플레인 쿠버네티스 버전 업그레이드. 한 번에 한 마이너 버전씩만 올릴 수 있다.
- 키워드: 클러스터 업그레이드, 버전 올리기
- 예시: `aws eks update-cluster-version --name <CLUSTER_1> --kubernetes-version 1.31`
- 주의: 되돌릴 수 없다. 한 번에 한 마이너 버전씩만 올릴 수 있다. 노드는 별도로 업그레이드해야 한다(self-managed 노드는 AMI를 바꾼 launch template과 ASG 인스턴스 리프레시). Terraform으로 클러스터를 관리한다면 CLI로 직접 올리지 말고 코드의 버전 값을 바꿔 적용해야 코드와 상태가 어긋나지 않는다. 사전 점검(인사이트, 애드온 호환, 서브넷 여유 IP)을 먼저 한다.
- 확인: describe-cluster

## delete-cluster
- 설명: 클러스터 삭제. 노드그룹이 남아 있으면 실패한다.
- 키워드: 클러스터 삭제, EKS 제거
- 예시: `aws eks delete-cluster --name <CLUSTER_1>`
- 주의: 되돌릴 수 없다. 먼저 노드그룹 삭제(nodegroup 문서)가 필요하다.
- 확인: list-clusters

## update-kubeconfig
- 설명: 로컬 kubeconfig 파일에 클러스터 접속 정보를 추가. 서버 전역 설정을 바꾸므로 수동 실행(manual)으로 분류한다. 서버는 실행하지 않는다.
- 키워드: kubeconfig 설정, kubectl 접속 설정
- 예시: `aws eks update-kubeconfig --name <CLUSTER_1> --alias <NAME_1>`
- 주의: 실행 서버의 `~/.kube/config`가 바뀐다. 이후 `kubectl` 명령의 대상 클러스터가 달라진다.
- 확인: describe-cluster
