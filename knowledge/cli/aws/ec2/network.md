---
cli: aws
service: ec2
topic: network
commands:
  - {name: describe-vpcs, kind: read, risk: low, required: [], optional: [--vpc-ids, --filters]}
  - {name: describe-subnets, kind: read, risk: low, required: [], optional: [--subnet-ids, --filters]}
  - {name: describe-route-tables, kind: read, risk: low, required: [], optional: [--route-table-ids, --filters]}
  - {name: describe-network-interfaces, kind: read, risk: low, required: [], optional: [--network-interface-ids, --filters]}
---
# EC2 네트워크 조회

보안 그룹과 인스턴스 작업 전에 대상 네트워크를 확인하는 읽기 전용 명령 모음.

## describe-vpcs
- 설명: VPC 목록·CIDR 조회.
- 키워드: VPC 목록, 네트워크 확인, 기본 VPC
- 예시: `aws ec2 describe-vpcs --vpc-ids <VPC_1>`

## describe-subnets
- 설명: 서브넷 목록·AZ·CIDR 조회.
- 키워드: 서브넷 조회, 퍼블릭 프라이빗 서브넷 확인
- 예시: `aws ec2 describe-subnets --filters Name=vpc-id,Values=<VPC_1>`

## describe-route-tables
- 설명: 라우트 테이블 조회. 서브넷이 인터넷 게이트웨이로 가는지 확인할 때 쓴다.
- 키워드: 라우팅 확인, 라우트 테이블
- 예시: `aws ec2 describe-route-tables --filters Name=vpc-id,Values=<VPC_1>`

## describe-network-interfaces
- 설명: ENI 조회. 보안 그룹이 어디에 연결돼 있는지 확인할 때 쓴다(SG 삭제 전 점검).
- 키워드: SG 사용 중인지, 연결된 리소스, ENI
- 예시: `aws ec2 describe-network-interfaces --filters Name=group-id,Values=<SG_1>`
