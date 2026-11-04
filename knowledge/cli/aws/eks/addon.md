---
cli: aws
service: eks
topic: addon
commands:
  - {name: list-addons, kind: read, risk: low, required: [--cluster-name], optional: []}
  - {name: describe-addon, kind: read, risk: low, required: [--cluster-name, --addon-name], optional: []}
  - {name: describe-addon-versions, kind: read, risk: low, required: [], optional: [--kubernetes-version, --addon-name]}
  - {name: update-addon, kind: mutate, risk: high, required: [--cluster-name, --addon-name], optional: [--addon-version, --resolve-conflicts, --configuration-values], verify: describe-addon, impact: {disruption: possible, replaces: false, reversible: partial, preflight: [describe-addon, describe-addon-versions], iac: true, long_running: true}}
---
# EKS 애드온

kube-proxy, coredns, vpc-cni, ebs-csi-driver 등 EKS 관리형 애드온. 클러스터 업그레이드 때 애드온 버전도 맞춰야 한다. 애드온을 Helm이나 매니페스트로 직접 설치했다면 이 명령 대상이 아니다.

## list-addons
- 설명: 클러스터에 설치된 EKS 애드온 이름 목록.
- 키워드: 애드온 목록, add-on 조회, 설치된 애드온
- 예시: `aws eks list-addons --cluster-name <CLUSTER_1>`

## describe-addon
- 설명: 애드온의 현재 버전·상태.
- 키워드: 애드온 버전 확인, add-on 상태
- 예시: `aws eks describe-addon --cluster-name <CLUSTER_1> --addon-name coredns`

## describe-addon-versions
- 설명: 쿠버네티스 버전별로 사용할 수 있는 애드온 버전 목록. 업그레이드 대상 버전과 호환되는 애드온 버전을 찾을 때 쓴다.
- 키워드: 호환 애드온 버전, 업그레이드용 애드온 버전, addon versions
- 예시: `aws eks describe-addon-versions --kubernetes-version 1.36 --addon-name coredns`

## update-addon
- 설명: 애드온 버전 변경. `--resolve-conflicts`로 사용자가 수정한 설정을 덮어쓸지(OVERWRITE) 유지할지(PRESERVE) 정한다.
- 키워드: 애드온 업데이트, 애드온 업그레이드, update addon
- 예시: `aws eks update-addon --cluster-name <CLUSTER_1> --addon-name coredns --addon-version <NAME_1> --resolve-conflicts PRESERVE`
- 주의: `OVERWRITE`는 직접 수정한 설정을 지운다. 코어 애드온(coredns, kube-proxy, vpc-cni)은 업데이트 중 일시적인 DNS·네트워크 영향이 있을 수 있다. Terraform으로 애드온을 관리하면 CLI 변경은 코드와 어긋난다.
- 확인: describe-addon
