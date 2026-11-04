---
cli: aws
service: ec2
topic: launch-template
commands:
  - {name: describe-launch-templates, kind: read, risk: low, required: [], optional: [--launch-template-ids, --launch-template-names, --filters]}
  - {name: describe-launch-template-versions, kind: read, risk: low, required: [], optional: [--launch-template-id, --launch-template-name, --versions]}
  - {name: describe-images, kind: read, risk: low, required: [], optional: [--image-ids, --owners, --filters]}
---
# EC2 Launch Template 조회

ASG가 새 인스턴스를 띄울 때 쓰는 설정(AMI, 인스턴스 타입, 사용자 데이터 등). 노드 업그레이드 전에 현재 AMI와 버전을 확인한다. launch template 생성과 버전 추가는 Terraform으로 관리하는 환경이라 이 문서에서는 읽기만 다룬다.

## describe-launch-templates
- 설명: launch template 목록과 기본/최신 버전 번호.
- 키워드: 런치 템플릿 목록, launch template 조회
- 예시: `aws ec2 describe-launch-templates --launch-template-names <NAME_1>`

## describe-launch-template-versions
- 설명: launch template 버전별 상세(AMI ID, 인스턴스 타입, 보안 그룹, 사용자 데이터 등).
- 키워드: 런치 템플릿 버전, 노드 AMI 확인, 현재 AMI
- 예시: `aws ec2 describe-launch-template-versions --launch-template-name <NAME_1> --versions 1 2`
- 주의: 사용자 데이터에 민감 정보가 들어 있을 수 있다. 결과는 마스킹 스캔을 거친다. `$Latest`, `$Default` 같은 별칭은 `$` 때문에 명령 가드가 환경변수 확장으로 막으므로 버전 번호를 쓰고, 최신 번호는 `--versions` 없이 목록을 조회해 확인한다.

## describe-images
- 설명: AMI 상세(이름, 생성일, 아키텍처). 노드 AMI가 어떤 쿠버네티스 버전용인지 이름으로 확인할 때 쓴다.
- 키워드: AMI 조회, 이미지 확인
- 예시: `aws ec2 describe-images --image-ids <NAME_1>`
