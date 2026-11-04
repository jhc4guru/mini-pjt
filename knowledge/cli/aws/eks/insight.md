---
cli: aws
service: eks
topic: insight
commands:
  - {name: list-insights, kind: read, risk: low, required: [--cluster-name], optional: [--filter, --max-items]}
  - {name: describe-insight, kind: read, risk: low, required: [--cluster-name, --id], optional: []}
---
# EKS 인사이트 (업그레이드 준비 점검)

EKS가 클러스터를 분석해 업그레이드 전에 문제가 될 항목(제거되는 API 사용, 호환되지 않는 구성 등)을 알려 준다. 업그레이드 계획의 **0단계 사전 점검**에서 가장 먼저 확인한다. 버전별 제거 API 같은 세부 내용은 이 조회 결과가 클러스터 기준으로 알려 준다.

## list-insights
- 설명: 클러스터 인사이트 목록(카테고리, 상태, 대상 버전). 업그레이드 준비 상태는 `UPGRADE_READINESS` 카테고리다.
- 키워드: 업그레이드 인사이트, 업그레이드 사전 점검, 제거되는 API, upgrade readiness, deprecated api
- 예시: `aws eks list-insights --cluster-name <CLUSTER_1> --filter categories=UPGRADE_READINESS`

## describe-insight
- 설명: 인사이트 하나의 상세(영향받는 리소스, 권장 조치).
- 키워드: 인사이트 상세, 업그레이드 문제 항목
- 예시: `aws eks describe-insight --cluster-name <CLUSTER_1> --id <NAME_1>`
