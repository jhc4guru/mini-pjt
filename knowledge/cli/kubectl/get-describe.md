---
cli: kubectl
service: kubectl
topic: get-describe
commands:
  - {name: get, kind: read, risk: low, required: [], optional: [--namespace, --output, --all-namespaces, --selector, --watch], guard: [output-secret, no-follow]}
  - {name: describe, kind: read, risk: low, required: [], optional: [--namespace, --selector, --all-namespaces]}
  - {name: logs, kind: read, risk: low, required: [], optional: [--namespace, --container, --tail, --since, --previous, --follow], guard: [no-follow]}
  - {name: events, kind: read, risk: low, required: [], optional: [--namespace, --all-namespaces, --for]}
  - {name: version, kind: read, risk: low, required: [], optional: [--output]}
  - {name: api-resources, kind: read, risk: low, required: [], optional: [--api-group, --namespaced, --output]}
  - {name: api-versions, kind: read, risk: low, required: [], optional: []}
  - {name: top pod, kind: read, risk: low, required: [], optional: [--namespace, --all-namespaces, --containers]}
  - {name: top node, kind: read, risk: low, required: [], optional: [--selector]}
  - {name: config current-context, kind: read, risk: low, required: [], optional: []}
  - {name: config get-contexts, kind: read, risk: low, required: [], optional: [--output]}
  - {name: auth can-i, kind: read, risk: low, required: [], optional: [--namespace, --list]}
---
# kubectl 조회 명령

대상 클러스터는 kubeconfig의 현재 컨텍스트로 결정된다. 변경 작업 전에 `config current-context`로 대상을 확인하는 step을 계획에 넣는 것이 안전하다.

## get
- 설명: 리소스 목록·상태 조회(pods, deployments, services, nodes, namespaces 등).
- 키워드: 파드 목록, 배포 조회, 서비스 확인, 노드 상태, 네임스페이스 목록, get pods
- 예시: `kubectl get pods --namespace <NAMESPACE_1>`
- 예시(전체 네임스페이스): `kubectl get deployments --all-namespaces`
- 예시(라벨): `kubectl get pods --namespace <NAMESPACE_1> --selector app=<NAME_1> --output wide`
- 주의: `secret` 리소스를 `--output yaml/json/jsonpath`로 조회하면 시크릿 값(base64)이 그대로 나온다. 가드가 이 조합을 막는다(`output-secret`). 이름과 개수만 보려면 `get secrets`(기본 출력)나 `describe secret`을 쓴다. `--watch`는 종료되지 않으므로 막는다(`no-follow`).

## describe
- 설명: 리소스 상세와 이벤트(스케줄링 실패, 이미지 풀 오류 등 원인 파악에 유용).
- 키워드: 파드 상세, 왜 안 뜨는지, 이벤트 확인, 상태 상세
- 예시: `kubectl describe pod <NAME_1> --namespace <NAMESPACE_1>`
- 주의: `describe secret`은 값 자체는 보여주지 않는다(키 이름과 크기만).

## logs
- 설명: 파드/컨테이너 로그 조회.
- 키워드: 로그 확인, 에러 로그, 최근 로그, tail
- 예시: `kubectl logs <NAME_1> --namespace <NAMESPACE_1> --tail 200 --since 1h`
- 예시(이전 컨테이너): `kubectl logs <NAME_1> --namespace <NAMESPACE_1> --previous`
- 주의: `--follow`는 종료되지 않으므로 막는다. 로그 안에 토큰·계정 정보가 있을 수 있어 결과는 마스킹 스캔을 거친다.

## events
- 설명: 네임스페이스 이벤트를 시간순으로 조회.
- 키워드: 이벤트, 최근 이벤트, 경고 이벤트
- 예시: `kubectl events --namespace <NAMESPACE_1> --for pod/<NAME_1>`

## version
- 설명: kubectl 클라이언트와 클러스터 API 서버 버전. 업그레이드 전후 버전 확인과 kubectl 버전 차이(클러스터와 ±1 마이너 이내 권장) 점검에 쓴다.
- 키워드: 쿠버네티스 버전, 클러스터 버전 확인, kubectl 버전
- 예시: `kubectl version --output yaml`

## api-resources
- 설명: 클러스터가 지원하는 리소스 종류 목록(그룹, 버전, 네임스페이스 여부).
- 키워드: 리소스 종류, 지원 API 목록
- 예시: `kubectl api-resources --namespaced=true --output wide`

## api-versions
- 설명: 클러스터가 지원하는 API 그룹/버전 목록. 업그레이드 후 사용하던 API 버전이 사라졌는지 확인할 때 쓴다.
- 키워드: API 버전 확인, 지원 API 버전, 제거된 API
- 예시: `kubectl api-versions`

## top pod
- 설명: 파드 CPU/메모리 사용량(metrics-server 필요).
- 키워드: 파드 리소스 사용량, 메모리 사용
- 예시: `kubectl top pod --namespace <NAMESPACE_1> --containers`

## top node
- 설명: 노드 CPU/메모리 사용량.
- 키워드: 노드 사용량, 노드 부하
- 예시: `kubectl top node`

## config current-context
- 설명: 현재 대상 클러스터(컨텍스트) 이름.
- 키워드: 어느 클러스터, 현재 컨텍스트
- 예시: `kubectl config current-context`

## config get-contexts
- 설명: 등록된 컨텍스트 목록.
- 키워드: 컨텍스트 목록, 클러스터 목록
- 예시: `kubectl config get-contexts`

## auth can-i
- 설명: 현재 자격증명으로 특정 동작이 허용되는지 확인.
- 키워드: 권한 확인, 할 수 있는지 확인
- 예시: `kubectl auth can-i create deployments --namespace <NAMESPACE_1>`
