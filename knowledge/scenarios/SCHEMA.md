# knowledge/scenarios 규격 (D-72)

반복되는 큰 작업의 **조사 목록과 phase 골격**을 사람이 검토해 두는 곳이다. `knowledge/cli/`(명령 문서)와 달리 "무엇을 어떤 순서로 점검·변경하는가"를 적는다.
앱은 `PLANNER_MODE=batch`에서 요청이 시나리오에 맞으면 이 파일로 조사와 윤곽을 대신하고, 모델은 phase마다 step만 쓴다. 서버가 `expect`로 필수 항목을 검사하고,
`build_plan` 전체 검증·명령 가드·4-eyes 승인은 그대로다. 시나리오는 제안의 재료일 뿐 가드를 대신하지 않는다.

검증: `tests/test_scenarios.py`(구조, 조사 명령이 모두 읽기 명령이고 명령 가드를 통과하는지, `docs` 질의가 실제 문서를 찾는지). 값은 합성·플레이스홀더만 쓴다(실제 환경 값 금지).

## 파일
`knowledge/scenarios/<id>.yaml` 하나가 시나리오 하나다.

```yaml
id: eks-upgrade                    # 파일 이름과 같게
title: EKS 클러스터 버전 업그레이드
summary: "{cluster} 클러스터를 {to_version}으로 업그레이드한다(...)"   # plan 요약. {param} 치환, 300자까지

match:                             # 서버 규칙(모델 판단 아님). 모두 만족해야 맞는다
  services_any: [eks]              # 접수 결과의 서비스 중 하나(선택)
  all_of: [[업그레이드, upgrade], [클러스터, cluster, eks]]   # 바깥 AND, 안쪽 OR, 소문자 부분 문자열, 마스킹된 요청에서 찾는다
  none_of: [조회만, 삭제]           # 하나라도 있으면 맞지 않는다

params:                            # 마스킹된 요청에서 서버가 뽑는 값. required가 없으면 일반 계획으로 간다
  cluster: {placeholder: CLUSTER, required: true}   # 첫 <CLUSTER_n> 플레이스홀더
  to_version: {patterns: ['...(\d+\.\d+)...'], required: true}   # 정규식, 첫 캡처 그룹

investigation:                     # 서버가 일괄 실행하는 읽기 명령(최대 20개). 필요한 param이 없으면 그 항목은 건너뜀
  - aws eks describe-cluster --name {cluster}

phases:                            # 윤곽이 된다(순서대로). 모델이 바꿀 수 없다
  - id: preflight                  # step id 규칙(소문자·숫자·하이픈)
    title: 사전 점검
    purpose: ...                   # 200자까지
    preconditions: [...]           # 선택, 8개까지
    gate: false                    # true면 gate_reason 필수(160자까지)
    gate_reason: ...
    rollback: ...                  # 300자까지
    guidance: |                    # 이 phase를 쓰는 모델에게 주는 안내. {param} 치환
    docs: ["aws eks list-insights"]   # 서버가 미리 검색해 본문을 싣는 문서 질의(6개까지)
    expect:                        # 필수 항목. 바깥 AND, 안쪽 OR, step의 "종류 제목 명령"에 정규식 검색
      - ["insight"]
```

## 규칙
- 되돌릴 수 없는 step의 `irreversible` 표시와 게이트 잠금은 기존 서버 규칙(`build_plan`)이 한다. 시나리오는 `gate`와 `guidance`로 안내만 한다.
- `expect`가 모자라면 서버가 `scenario_missing:<phase>:<번호>` 오류로 돌려 모델이 고치게 한다(수정 기회는 기존 한도 안).
- 조사 명령은 `aws`·`kubectl` 읽기 명령만 쓴다. `lookup_inventory` 같은 도구 호출은 시나리오에 쓰지 않는다.
- 시나리오를 추가하거나 게이트·필수 항목을 바꿀 때는 사용자 검토, 테스트, `docs/DECISIONS.md` 갱신이 필요하다. 평가 케이스(`eval/agent_cases.csv`)도 함께 추가한다.
