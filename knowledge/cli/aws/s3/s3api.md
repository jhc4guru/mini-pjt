---
cli: aws
service: s3api
topic: s3-lowlevel
commands:
  - {name: list-buckets, kind: read, risk: low, required: [], optional: [--query]}
  - {name: head-bucket, kind: read, risk: low, required: [--bucket], optional: []}
  - {name: list-objects-v2, kind: read, risk: low, required: [--bucket], optional: [--prefix, --max-items, --query]}
  - {name: get-bucket-policy, kind: read, risk: low, required: [--bucket], optional: []}
  - {name: get-bucket-versioning, kind: read, risk: low, required: [--bucket], optional: []}
  - {name: get-bucket-encryption, kind: read, risk: low, required: [--bucket], optional: []}
  - {name: get-bucket-acl, kind: read, risk: low, required: [--bucket], optional: []}
  - {name: get-public-access-block, kind: read, risk: low, required: [--bucket], optional: []}
  - {name: put-bucket-versioning, kind: mutate, risk: medium, required: [--bucket, --versioning-configuration], optional: [], verify: get-bucket-versioning, impact: {disruption: none, replaces: false, reversible: partial, preflight: [get-bucket-versioning], iac: true}}
  - {name: put-bucket-encryption, kind: mutate, risk: medium, required: [--bucket, --server-side-encryption-configuration], optional: [], verify: get-bucket-encryption, impact: {disruption: none, replaces: false, reversible: full, preflight: [get-bucket-encryption], iac: true}}
  - {name: put-public-access-block, kind: mutate, risk: high, required: [--bucket, --public-access-block-configuration], optional: [], verify: get-public-access-block, guard: [public-access], impact: {disruption: possible, replaces: false, reversible: full, preflight: [get-public-access-block], iac: true}}
  - {name: put-bucket-policy, kind: mutate, risk: high, required: [--bucket, --policy], optional: [], verify: get-bucket-policy, guard: [wildcard-principal, file-input], impact: {disruption: possible, replaces: false, reversible: partial, preflight: [get-bucket-policy], iac: true}}
  - {name: put-bucket-acl, kind: mutate, risk: high, required: [--bucket], optional: [--acl], verify: get-bucket-acl, guard: [public-access], impact: {disruption: possible, replaces: false, reversible: full, preflight: [get-bucket-acl], iac: true}}
  - {name: delete-object, kind: mutate, risk: high, required: [--bucket, --key], optional: [--version-id], verify: list-objects-v2, impact: {disruption: none, replaces: false, reversible: partial, preflight: [list-objects-v2, get-bucket-versioning]}}
---
# S3 저수준 명령 (aws s3api)

버킷 설정(정책, 버저닝, 암호화, 퍼블릭 액세스 차단)과 객체 단위 API. 정책 JSON은 `--policy file://<파일>` 또는 문자열로 전달한다.

## list-buckets
- 설명: 계정의 버킷 목록(생성일 포함).
- 키워드: 버킷 목록, 버킷 전체 조회
- 예시: `aws s3api list-buckets --query "Buckets[].Name"`

## head-bucket
- 설명: 버킷 존재 여부와 접근 가능 여부 확인(본문 없음).
- 키워드: 버킷 있는지 확인, 접근 확인
- 예시: `aws s3api head-bucket --bucket <BUCKET_1>`

## list-objects-v2
- 설명: 객체 목록 조회(접두사 필터, 페이징).
- 키워드: 객체 목록, 파일 조회, list objects
- 예시: `aws s3api list-objects-v2 --bucket <BUCKET_1> --prefix logs/ --max-items 100`

## get-bucket-policy
- 설명: 버킷 정책 JSON 조회. 정책이 없으면 `NoSuchBucketPolicy` 오류.
- 키워드: 버킷 정책 확인
- 예시: `aws s3api get-bucket-policy --bucket <BUCKET_1>`

## get-bucket-versioning
- 설명: 버저닝 상태 조회(Enabled/Suspended/없음).
- 키워드: 버저닝 확인, 버전 관리 상태
- 예시: `aws s3api get-bucket-versioning --bucket <BUCKET_1>`

## get-bucket-encryption
- 설명: 기본 암호화 설정 조회.
- 키워드: 암호화 확인, SSE 설정
- 예시: `aws s3api get-bucket-encryption --bucket <BUCKET_1>`

## get-bucket-acl
- 설명: 버킷 ACL 조회.
- 키워드: ACL 확인, 공개 여부 확인
- 예시: `aws s3api get-bucket-acl --bucket <BUCKET_1>`

## get-public-access-block
- 설명: 퍼블릭 액세스 차단 설정 조회.
- 키워드: 퍼블릭 차단 확인, 공개 차단 상태
- 예시: `aws s3api get-public-access-block --bucket <BUCKET_1>`

## put-bucket-versioning
- 설명: 버저닝 켜기/일시중지. 한 번 켜면 완전히 끌 수 없고 일시중지만 가능하다.
- 키워드: 버저닝 활성화, 버전 관리 켜기
- 예시: `aws s3api put-bucket-versioning --bucket <BUCKET_1> --versioning-configuration Status=Enabled`
- 확인: get-bucket-versioning

## put-bucket-encryption
- 설명: 기본 서버측 암호화 설정.
- 키워드: 암호화 켜기, SSE-S3, KMS 암호화
- 예시: `aws s3api put-bucket-encryption --bucket <BUCKET_1> --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'`
- 확인: get-bucket-encryption

## put-public-access-block
- 설명: 퍼블릭 액세스 차단 설정 변경. 네 가지 항목(BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy, RestrictPublicBuckets)이 있다.
- 키워드: 퍼블릭 액세스 차단, 외부 공개 막기, 공개 허용
- 예시(모두 차단): `aws s3api put-public-access-block --bucket <BUCKET_1> --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true`
- 주의: 항목을 `false`로 바꾸면 외부 공개 위험이 커진다. 가드가 `false` 값을 위험으로 표시한다.
- 확인: get-public-access-block

## put-bucket-policy
- 설명: 버킷 정책 설정(기존 정책을 통째로 교체).
- 키워드: 버킷 정책 적용, 접근 권한 부여
- 예시: `aws s3api put-bucket-policy --bucket <BUCKET_1> --policy file://<NAME_1>`
- 주의: `"Principal": "*"`는 전체 공개다. 정책 JSON 파일 내용은 승인 화면에 그대로 보여준다. 기존 정책을 덮어쓰므로 먼저 `get-bucket-policy`로 현재 값을 확인한다.
- 확인: get-bucket-policy

## put-bucket-acl
- 설명: 버킷 ACL 변경. `--acl public-read` 같은 canned ACL로 공개할 수 있다.
- 키워드: ACL 변경, 버킷 공개
- 예시: `aws s3api put-bucket-acl --bucket <BUCKET_1> --acl private`
- 주의: `public-read`, `public-read-write`는 전체 공개다. 가드가 차단 대상에 가깝게 경고한다.
- 확인: get-bucket-acl

## delete-object
- 설명: 객체 하나 삭제. 버저닝이 켜져 있으면 삭제 마커만 생기고, `--version-id`를 주면 해당 버전을 영구 삭제한다.
- 키워드: 객체 삭제, 파일 하나 삭제
- 예시: `aws s3api delete-object --bucket <BUCKET_1> --key <NAME_1>`
- 주의: `--version-id` 지정 시 되돌릴 수 없다.
- 확인: list-objects-v2
