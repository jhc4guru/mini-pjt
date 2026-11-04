---
cli: kubectl
service: kubectl
topic: manual
commands:
  - {name: exec, kind: manual, risk: high, required: [], optional: [--namespace, --container, --stdin, --tty], reason: "컨테이너 안에서 임의 명령을 실행하며 대화형일 수 있어 서버 실행기로 통제할 수 없음", impact: {disruption: possible, replaces: false, reversible: partial}}
  - {name: attach, kind: manual, risk: high, required: [], optional: [--namespace, --container, --stdin, --tty], reason: "실행 중인 프로세스에 대화형으로 붙음", impact: {disruption: possible, replaces: false, reversible: partial}}
  - {name: port-forward, kind: manual, risk: high, required: [], optional: [--namespace, --address], reason: "종료되지 않는 연결이며 서버 밖으로 내부 서비스를 노출할 수 있음", impact: {disruption: none, replaces: false, reversible: full}}
  - {name: cp, kind: manual, risk: high, required: [], optional: [--namespace, --container], reason: "컨테이너와 로컬 사이의 임의 파일 반출입. 서버 파일 시스템을 건드리지 않도록 사람의 터미널에서 실행", impact: {disruption: none, replaces: false, reversible: partial}}
  - {name: proxy, kind: manual, risk: high, required: [], optional: [--port, --address], reason: "API 서버 접근을 프록시로 노출하며 종료되지 않음", impact: {disruption: none, replaces: false, reversible: full}}
  - {name: debug, kind: manual, risk: high, required: [], optional: [--namespace, --stdin, --tty, --image, --target], reason: "임시 컨테이너로 임의 명령 실행, 노드 접근 가능", impact: {disruption: possible, replaces: false, reversible: partial}}
  - {name: edit, kind: manual, risk: high, required: [], optional: [--namespace], reason: "대화형 편집기를 띄우며 변경 내용이 계획에 남지 않음. 가능하면 patch 또는 apply 사용", impact: {disruption: possible, replaces: false, reversible: partial, preflight: [get], iac: true}}
---
# kubectl 수동 실행 명령 (manual)

아래 명령은 **서버가 실행하지 않는다.** 도구는 근거 문서에 맞는 명령 문구만 만들어 보여주고, 사람이 자기 터미널(또는 서버/배스천 셸)에서 직접 실행한다. 대화형이거나 종료되지 않는 명령, 임의 명령을 실행하는 명령이 여기에 속한다.

- 계획에는 "수동 실행" step으로 들어가고, plan 일괄 승인 때 다른 사람이 명령 문구를 검토한다.
- apply에서 이 step은 멈춘다. 요청자가 직접 실행하고 "실행 완료"를 보고하면, 다른 사람이 확인한 뒤 다음 step으로 넘어간다. 결과 붙여넣기는 선택이다.
- 이 명령으로는 서버의 파일·네트워크·자격증명을 사용하지 않는다.
- `exec -- <명령>`처럼 안에 임의 명령이 들어가면 승인 화면에서 눈에 띄게 표시한다.

## exec
- 설명: 실행 중인 컨테이너 안에서 명령 실행(쉘 접속 포함).
- 키워드: 컨테이너 접속, 쉘 접속, 파드 안에서 명령 실행, exec
- 예시(쉘): `kubectl exec --namespace <NAMESPACE_1> --stdin --tty <NAME_1> -- /bin/sh`
- 예시(단일 명령): `kubectl exec --namespace <NAMESPACE_1> <NAME_1> -- ls /app`
- 대안: 상태 확인만 필요하면 `logs`, `describe`, `top pod`.

## attach
- 설명: 실행 중인 프로세스의 표준 입출력에 붙는다.
- 키워드: 컨테이너 붙기, attach
- 예시: `kubectl attach <NAME_1> --namespace <NAMESPACE_1> --stdin --tty`
- 대안: 출력만 보면 `logs`.

## port-forward
- 설명: 로컬 포트를 파드/서비스 포트로 연결(종료할 때까지 유지).
- 키워드: 포트 포워딩, 로컬 접속, port-forward
- 예시: `kubectl port-forward pod/<NAME_1> 8080:80 --namespace <NAMESPACE_1>`
- 주의: `--address 0.0.0.0`은 외부에서 접근 가능해진다. 기본(127.0.0.1)을 권장한다.

## cp
- 설명: 컨테이너와 로컬 사이 파일 복사.
- 키워드: 파일 복사, 파드에서 파일 꺼내기, kubectl cp
- 예시(내려받기): `kubectl cp <NAMESPACE_1>/<NAME_1>:/app/logs/app.log ./app.log --container <NAME_2>`
- 주의: 본인 터미널의 작업 폴더에 저장된다. 민감 파일을 내려받지 않도록 주의한다.

## proxy
- 설명: 로컬에서 쿠버네티스 API 서버로 가는 프록시를 연다(종료할 때까지 유지).
- 키워드: 프록시, API 서버 프록시
- 예시: `kubectl proxy --port 8001`
- 주의: `--address`를 0.0.0.0으로 열면 API 서버가 외부에 노출된다.

## debug
- 설명: 문제 파드에 임시 디버그 컨테이너를 붙이거나, 노드 접근용 파드를 만든다.
- 키워드: 디버그 컨테이너, 파드 디버깅, 노드 디버그
- 예시: `kubectl debug <NAME_1> --namespace <NAMESPACE_1> --stdin --tty --image busybox --target <NAME_2>`
- 주의: 노드 디버깅은 호스트 파일 시스템에 접근할 수 있다.

## edit
- 설명: 리소스를 편집기로 열어 직접 수정.
- 키워드: 리소스 직접 수정, kubectl edit
- 예시: `kubectl edit deployment/<NAME_1> --namespace <NAMESPACE_1>`
- 대안: 변경 내용을 계획에 남기려면 `patch` 또는 `apply`를 쓴다.
