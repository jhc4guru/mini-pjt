---
cli: aws
service: ec2
topic: tags
commands:
  - {name: describe-tags, kind: read, risk: low, required: [], optional: [--filters]}
  - {name: create-tags, kind: mutate, risk: low, required: [--resources, --tags], optional: [], verify: describe-tags, impact: {disruption: none, replaces: false, reversible: full, iac: true}}
---
# EC2 리소스 태그

## describe-tags
- 설명: 리소스 태그 조회. 필터로 리소스 ID나 태그 키를 지정한다.
- 키워드: 태그 조회, 태그 확인, Name 태그 확인, list tags
- 예시: `aws ec2 describe-tags --filters Name=resource-id,Values=<SG_1>`

## create-tags
- 설명: 이미 만든 리소스에 태그를 붙이거나 같은 키의 값을 바꾼다. 새 리소스를 만들면서 붙일 때는 각 생성 명령의 `--tag-specifications`를 쓴다(한 단계로 끝나고 태그 없는 상태가 남지 않는다).
- 키워드: 태그 붙이기, 태그 추가, Name 태그 달기, 이름 태그 변경, create tags, tag a resource
- 예시: `aws ec2 create-tags --resources <SG_1> --tags Key=Name,Value=<NAME_1>`
- 주의: 같은 키가 있으면 값을 덮어쓴다. 태그 변경은 서비스에 영향이 없지만 태그 기준으로 동작하는 정책·비용 배분이 있으면 달라질 수 있다.
- 확인: describe-tags
