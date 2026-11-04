---
cli: aws
service: ec2
topic: instance
commands:
  - {name: describe-instances, kind: read, risk: low, required: [], optional: [--instance-ids, --filters]}
  - {name: describe-instance-status, kind: read, risk: low, required: [], optional: [--instance-ids, --include-all-instances]}
  - {name: start-instances, kind: mutate, risk: low, required: [--instance-ids], optional: [], verify: describe-instance-status, impact: {disruption: none, replaces: false, reversible: full}}
  - {name: stop-instances, kind: mutate, risk: medium, required: [--instance-ids], optional: [--force], verify: describe-instance-status, impact: {disruption: likely, replaces: false, reversible: full, preflight: [describe-instances], dry_run: "--dry-run (권한 검사만)"}}
  - {name: reboot-instances, kind: mutate, risk: medium, required: [--instance-ids], optional: [], verify: describe-instance-status, impact: {disruption: likely, replaces: false, reversible: full, preflight: [describe-instances], dry_run: "--dry-run (권한 검사만)"}}
  - {name: terminate-instances, kind: mutate, risk: high, required: [--instance-ids], optional: [], verify: describe-instances, impact: {disruption: likely, replaces: false, reversible: irreversible, preflight: [describe-instances], dry_run: "--dry-run (권한 검사만)", iac: true}}
  - {name: run-instances, kind: mutate, risk: medium, required: [], optional: [--image-id, --instance-type, --count, --subnet-id, --security-group-ids, --key-name, --tag-specifications], verify: describe-instances, impact: {disruption: none, replaces: false, reversible: full, dry_run: "--dry-run (권한 검사만)", iac: true}}
---
# EC2 인스턴스

## describe-instances
- 설명: 인스턴스 목록·상세 조회(상태, IP, SG, 서브넷 등).
- 키워드: 인스턴스 조회, 서버 목록, EC2 상태 확인, list instances
- 예시: `aws ec2 describe-instances --instance-ids <INSTANCE_1>`
- 예시(실행 중만): `aws ec2 describe-instances --filters Name=instance-state-name,Values=running`

## describe-instance-status
- 설명: 인스턴스 상태 점검 결과(시스템/인스턴스 상태). 기본은 실행 중인 인스턴스만 나온다.
- 키워드: 상태 점검, 헬스 체크, 중지됐는지 확인
- 예시: `aws ec2 describe-instance-status --instance-ids <INSTANCE_1> --include-all-instances`

## start-instances
- 설명: 중지된 인스턴스 시작.
- 키워드: 서버 켜기, 인스턴스 시작, start instance
- 예시: `aws ec2 start-instances --instance-ids <INSTANCE_1>`
- 확인: describe-instance-status

## stop-instances
- 설명: 인스턴스 중지(삭제 아님). 인스턴스 스토어 볼륨의 데이터는 사라지고, 퍼블릭 IP가 바뀔 수 있다.
- 키워드: 서버 끄기, 인스턴스 중지, stop instance
- 예시: `aws ec2 stop-instances --instance-ids <INSTANCE_1>`
- 주의: 서비스 중단이 생긴다. prod 대상이면 승인 화면에 경고한다.
- 확인: describe-instance-status

## reboot-instances
- 설명: 인스턴스 재부팅.
- 키워드: 서버 재시작, 리부트, reboot
- 예시: `aws ec2 reboot-instances --instance-ids <INSTANCE_1>`
- 확인: describe-instance-status

## terminate-instances
- 설명: 인스턴스 종료(영구 삭제). 루트 볼륨은 기본적으로 함께 삭제된다. 종료 방지(termination protection)가 켜져 있으면 실패한다.
- 키워드: 인스턴스 삭제, 서버 제거, terminate
- 예시: `aws ec2 terminate-instances --instance-ids <INSTANCE_1>`
- 주의: 되돌릴 수 없다. 여러 ID를 한 번에 주는 경우 대상 개수를 승인 화면에 강조한다.
- 확인: describe-instances

## run-instances
- 설명: 인스턴스 새로 생성. AMI, 인스턴스 타입, 서브넷, SG 지정이 일반적이다.
- 키워드: 서버 만들기, 인스턴스 생성, launch instance
- 예시: `aws ec2 run-instances --image-id <NAME_1> --instance-type t3.micro --count 1 --subnet-id <SUBNET_1> --security-group-ids <SG_1>`
- 주의: 비용이 발생한다. 개수(`--count`)와 타입을 승인 화면에 표시한다.
- 확인: describe-instances
