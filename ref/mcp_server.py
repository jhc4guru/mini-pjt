# mcp_server.py - 삼성SDS 교육용 더미 사내 데이터 MCP 서버 (fastmcp)
"""노출하는 도구 4개:
  - search_employee(name)      임직원 개인 조회
  - list_team_members(team)    팀별 임직원 목록
  - get_asset(owner)           자산 조회
  - list_projects(team)        프로젝트 목록

실행: python mcp_server.py
(stdio 모드로 대기합니다.)
"""
import json
import os
from fastmcp import FastMCP

mcp = FastMCP("sds-company-data")

# make_data.py가 만든 JSON 3개를 코드와 같은 폴더에서 로드합니다 (교육용 더미 데이터)
BASE = os.path.dirname(os.path.abspath(__file__))
EMPLOYEES = json.load(open(os.path.join(BASE, "employees.json"), encoding="utf-8"))
ASSETS = json.load(open(os.path.join(BASE, "assets.json"), encoding="utf-8"))
PROJECTS = json.load(open(os.path.join(BASE, "projects.json"), encoding="utf-8"))


@mcp.tool()
def search_employee(name: str) -> str:
    """삼성SDS 임직원 디렉터리에서 이름으로 직원 정보를 조회한다.

    직원의 소속 팀, 직무, 이메일이 필요할 때 사용한다.
    팀 전체 명단이나 인원수에는 사용하지 않는다 (list_team_members 사용).
    자산이나 프로젝트 질문에도 사용하지 않는다 (get_asset, list_projects 사용).

    Args:
        name: 직원의 정확한 한국어 이름
    """
    for emp in EMPLOYEES:
        if emp["name"] == name:
            return json.dumps(emp, ensure_ascii=False)
    names = ", ".join(e["name"] for e in EMPLOYEES)
    return f"'{name}' 직원을 찾을 수 없습니다. 등록된 직원: {names}"


@mcp.tool()
def list_team_members(team: str) -> str:
    """팀 이름으로 소속 임직원 목록을 조회한다.

    특정 팀의 인원수나 명단이 필요할 때 사용한다.
    개별 직원의 상세 정보는 search_employee를 사용한다.

    Args:
        team: 팀 이름 (예: '클라우드운영팀', '물류플랫폼팀')
    """
    rows = [e for e in EMPLOYEES if e["team"] == team]
    if not rows:
        teams = sorted({e["team"] for e in EMPLOYEES})
        return f"'{team}' 팀을 찾을 수 없습니다. 등록된 팀: {teams}"
    return json.dumps({"count": len(rows), "members": rows}, ensure_ascii=False, indent=2)


@mcp.tool()
def get_asset(owner: str = "") -> str:
    """사내 자산(노트북, 모니터 등)을 조회한다.

    Args:
        owner: 사용자 이름으로 필터. 빈 문자열이면 전체 자산 반환
    """
    rows = [a for a in ASSETS if not owner or a["owner"] == owner]
    if not rows:
        return f"'{owner}' 사용자의 자산이 없습니다."
    return json.dumps({"count": len(rows), "assets": rows}, ensure_ascii=False, indent=2)


@mcp.tool()
def list_projects(team: str = "") -> str:
    """진행 중인 프로젝트 목록을 조회한다.

    Args:
        team: 팀 이름으로 필터 (예: '물류플랫폼팀'). 빈 문자열이면 전체
    """
    rows = [p for p in PROJECTS if not team or p["team"] == team]
    if not rows:
        return f"'{team}' 팀의 프로젝트가 없습니다."
    return json.dumps(rows, ensure_ascii=False, indent=2)

# mcp_server.py 에 추가

# Resource: URI 기반 데이터. 도구는 "호출"이고 리소스는 "읽기"
@mcp.resource("employees://{employee_id}")
def get_employee_resource(employee_id: str) -> str:
    """직원 상세 정보를 URI로 제공한다. 예: employees://1"""
    emp = next((e for e in EMPLOYEES if e["id"] == int(employee_id)), {})
    if not emp:
            return f"'{employee_id}' id의 직원이 없습니다."
    return json.dumps(emp, ensure_ascii=False)


# Prompt: 미리 정의된 프롬프트 템플릿. 클라이언트가 명령 형태로 노출 가능
@mcp.prompt()
def asset_report(team: str) -> str:
    """팀별 자산 현황 보고서 작성용 프롬프트."""
    return f"""{team}의 자산 현황을 조사해 보고서를 작성하세요.
1. search_employee로 팀원을 확인하고
2. get_asset으로 팀원별 자산을 조회하고
3. 유형별 수량을 표로 정리하고 재고 자산이 있으면 배정 제안을 덧붙이세요."""

if __name__ == "__main__":
    mcp.run()   # stdio 모드로 실행