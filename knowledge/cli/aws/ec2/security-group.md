---
cli: aws
service: ec2
topic: security-group
commands:
  - {name: describe-security-groups, kind: read, risk: low, required: [], optional: [--group-ids, --group-names, --filters]}
  - {name: describe-security-group-rules, kind: read, risk: low, required: [], optional: [--filters, --security-group-rule-ids]}
  - {name: create-security-group, kind: mutate, risk: low, required: [--group-name, --description], optional: [--vpc-id, --tag-specifications], verify: describe-security-groups, impact: {disruption: none, replaces: false, reversible: full, iac: true}}
  - {name: authorize-security-group-ingress, kind: mutate, risk: medium, required: [], optional: [--group-id, --protocol, --port, --cidr, --source-group, --ip-permissions], verify: describe-security-group-rules, guard: [open-cidr], impact: {disruption: none, replaces: false, reversible: full, preflight: [describe-security-group-rules], iac: true}}
  - {name: authorize-security-group-egress, kind: mutate, risk: medium, required: [--group-id], optional: [--ip-permissions], verify: describe-security-group-rules, guard: [open-cidr], impact: {disruption: none, replaces: false, reversible: full, preflight: [describe-security-group-rules], iac: true}}
  - {name: revoke-security-group-ingress, kind: mutate, risk: medium, required: [], optional: [--group-id, --protocol, --port, --cidr, --security-group-rule-ids], verify: describe-security-group-rules, impact: {disruption: possible, replaces: false, reversible: full, preflight: [describe-security-group-rules], iac: true}}
  - {name: revoke-security-group-egress, kind: mutate, risk: medium, required: [--group-id], optional: [--ip-permissions, --security-group-rule-ids], verify: describe-security-group-rules, impact: {disruption: possible, replaces: false, reversible: full, preflight: [describe-security-group-rules], iac: true}}
  - {name: delete-security-group, kind: mutate, risk: high, required: [], optional: [--group-id, --group-name], verify: describe-security-groups, impact: {disruption: none, replaces: false, reversible: irreversible, preflight: [describe-network-interfaces], iac: true}}
---
# EC2 보안 그룹 (Security Group)

보안 그룹은 인스턴스 등에 붙는 가상 방화벽이다. 인바운드(ingress)와 아웃바운드(egress) 규칙으로 구성된다. 규칙은 허용만 있고 거부 규칙은 없다.

## describe-security-groups
- 설명: 보안 그룹 목록·상세 조회.
- 키워드: 보안그룹 조회, SG 목록, 시큐리티그룹 확인, list security groups
- 예시: `aws ec2 describe-security-groups --group-ids <SG_1>`
- 예시(VPC 필터): `aws ec2 describe-security-groups --filters Name=vpc-id,Values=<VPC_1>`

## describe-security-group-rules
- 설명: 규칙 단위 조회. 규칙 ID(`sgr-...`)를 얻을 때 쓴다.
- 키워드: SG 규칙 조회, 인바운드 규칙 확인, 포트 열려있는지 확인
- 예시: `aws ec2 describe-security-group-rules --filters Name=group-id,Values=<SG_1>`

## create-security-group
- 설명: 보안 그룹 생성. 이름과 설명이 필수. VPC를 지정하지 않으면 기본 VPC에 만들어진다.
- 키워드: SG 생성, 보안그룹 만들기, 방화벽 그룹 새로 만들기, create security group, 태그 달아서 생성, Name 태그, 태그 붙이기, 이름 태그, 생성하면서 태그
- 예시: `aws ec2 create-security-group --group-name <NAME_1> --description "<NAME_2>" --vpc-id <VPC_1>`
- 예시(생성하면서 태그): `aws ec2 create-security-group --group-name <NAME_1> --description "<NAME_2>" --vpc-id <VPC_1> --tag-specifications 'ResourceType=security-group,Tags=[{Key=Name,Value=<NAME_3>}]'`
- 주의: 태그는 만들 때 `--tag-specifications`로 함께 붙이면 한 단계로 끝나고 태그 없는 상태가 남지 않는다. 이미 만든 리소스에 나중에 붙일 때만 `create-tags`(tags 문서)를 쓴다.
- 확인: describe-security-groups

## authorize-security-group-ingress
- 설명: 인바운드 규칙 추가(포트 열기). 대상은 `--cidr`(IP 대역) 또는 `--source-group`(다른 SG).
- 키워드: 인바운드 열기, 포트 오픈, 방화벽 열어줘, 접속 허용, allow inbound, open port
- 예시: `aws ec2 authorize-security-group-ingress --group-id <SG_1> --protocol tcp --port 443 --cidr <CIDR_1>`
- 주의: `--cidr 0.0.0.0/0`은 전체 개방이다. 가드가 위험 표시, 사유 입력, 승인자 별도 확인을 요구한다. 이미 같은 규칙이 있으면 `InvalidPermission.Duplicate` 오류가 난다(실패 분석 대상).
- 확인: describe-security-group-rules

## authorize-security-group-egress
- 설명: 아웃바운드 규칙 추가. 기본 SG는 모든 아웃바운드가 허용돼 있다.
- 키워드: 아웃바운드 허용, egress 추가
- 예시: `aws ec2 authorize-security-group-egress --group-id <SG_1> --ip-permissions IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=<CIDR_1>}]`
- 확인: describe-security-group-rules

## revoke-security-group-ingress
- 설명: 인바운드 규칙 제거(포트 닫기). 규칙 ID 또는 프로토콜·포트·CIDR로 지정.
- 키워드: 인바운드 닫기, 포트 차단, 접속 허용 해제, 규칙 삭제, revoke inbound
- 예시: `aws ec2 revoke-security-group-ingress --group-id <SG_1> --protocol tcp --port 22 --cidr <CIDR_1>`
- 주의: 운영 중인 접속(예: SSH)이 끊길 수 있다. 제거할 규칙을 먼저 `describe-security-group-rules`로 확인한다.
- 확인: describe-security-group-rules

## revoke-security-group-egress
- 설명: 아웃바운드 규칙 제거.
- 키워드: 아웃바운드 제거, egress 삭제
- 예시: `aws ec2 revoke-security-group-egress --group-id <SG_1> --security-group-rule-ids <NAME_1>`
- 확인: describe-security-group-rules

## delete-security-group
- 설명: 보안 그룹 삭제. 인스턴스 등에 연결되어 있거나 다른 SG가 참조 중이면 `DependencyViolation`으로 실패한다.
- 키워드: SG 삭제, 보안그룹 제거, delete security group
- 예시: `aws ec2 delete-security-group --group-id <SG_1>`
- 주의: 되돌릴 수 없다. 삭제 전 연결 상태를 `describe-network-interfaces`(network 문서) 등으로 확인한다.
- 확인: describe-security-groups
