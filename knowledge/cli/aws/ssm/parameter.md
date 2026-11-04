---
cli: aws
service: ssm
topic: parameter
commands:
  - {name: get-parameter, kind: read, risk: low, required: [--name], optional: [--query], guard: [output-secret]}
---
# SSM 파라미터 조회

EKS 최적화 AMI ID처럼 AWS가 공개하는 파라미터를 조회할 때 쓴다. 사용자 파라미터에는 비밀값(SecureString)이 있을 수 있어 조회 범위를 제한한다.

## get-parameter
- 설명: 파라미터 값 조회. EKS 최적화 AMI의 권장 이미지 ID는 `/aws/service/eks/optimized-ami/<쿠버네티스 버전>/amazon-linux-2023/x86_64/standard/recommended/image_id` 경로에 있다. 해당 버전의 AMI가 아직 없으면 `ParameterNotFound`가 난다.
- 키워드: AMI ID 조회, EKS 최적화 AMI, 노드 AMI, 권장 이미지, get parameter
- 예시: `aws ssm get-parameter --name /aws/service/eks/optimized-ami/1.36/amazon-linux-2023/x86_64/standard/recommended/image_id --query Parameter.Value --output text`
- 주의: `--with-decryption`은 쓰지 않는다. 가드가 `/aws/service/` 공개 경로 외의 파라미터 조회와 복호화 옵션을 차단한다(`output-secret`). 이 도구로 SecureString을 읽지 않는다.
