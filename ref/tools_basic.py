# tools_basic.py - @tool 데코레이터 3단계 깊이로 써보기
from langchain_core.tools import tool
from pydantic import BaseModel, Field


# 단계 1: 가장 단순한 형태
# 이름은 함수 이름, 설명은 docstring, 스키마는 타입 힌트에서 자동 추론
@tool
def add(a: int, b: int) -> int:
    """두 정수를 더한다."""
    return a + b


# 단계 2: 이름과 설명을 데코레이터 인자로 명시
# 도구 이름은 calculator, 함수 이름은 _internal_calc로 분리 가능
@tool("calculator", description="수학 계산을 정확하게 수행한다. LLM은 큰 숫자 계산에 약하므로 계산이 필요하면 이 도구를 쓴다.")
def _internal_calc(expression: str) -> str:
    return str(eval(expression, {"__builtins__": {}}, {}))


# 단계 3: Pydantic으로 인자 스키마를 강제
# 타입 힌트만으로 부족할 때 (검증 규칙, 기본값, 인자별 설명)
class SearchEmployeeInput(BaseModel):
    name: str = Field(
        description="직원의 정확한 한국어 이름 (부분 일치 불가)",
        min_length=2, max_length=20,   # 너무 짧거나 긴 값을 실행 전에 차단
    )


@tool(args_schema=SearchEmployeeInput)
def search_employee(name: str) -> str:
    """삼성SDS 임직원 디렉터리에서 이름으로 직원 정보를 조회한다.

    직원의 소속 팀, 직무, 이메일이 필요할 때 사용한다.
    사내 규정이나 정책 질문에는 사용하지 않는다.
    없는 이름이면 "찾을 수 없습니다"를 돌려준다.
    """
    # 교육용 더미 데이터입니다
    fake_db = {
        "김하늘": "김하늘 / 클라우드운영팀 / 팀장 / haneul.kim@samsungsds.example.com",
        "박도윤": "박도윤 / 물류플랫폼팀 / 백엔드 / doyun.park@samsungsds.example.com",
    }
    return fake_db.get(name, f"'{name}' 직원을 찾을 수 없습니다.")


if __name__ == "__main__":
    # 도구는 함수가 아니라 객체이므로 .invoke()로 호출합니다
    print(add.invoke({"a": 3, "b": 5}))
    print(_internal_calc.invoke({"expression": "15 * 25"}))
    print(search_employee.invoke({"name": "김하늘"}))

    # Pydantic 검증: 잘못된 인자는 도구 실행 전에 차단됩니다
    try:
        search_employee.invoke({"name": "김"})   # min_length=2 위반
    except Exception as e:
        print(f"검증 에러 (예상된 동작): {type(e).__name__}")

    print("=== 도구 메타데이터 (LLM이 실제로 보는 것) ===")
    for t in [add, _internal_calc, search_employee]:
        print(f"이름: {t.name}")
        print(f"설명: {t.description}")
        print(f"스키마: {t.args_schema.model_json_schema() if t.args_schema else None}")
        print()