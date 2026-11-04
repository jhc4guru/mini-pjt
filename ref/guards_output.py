# guards_output.py - 출력 가드레일: 민감정보 마스킹 + 범위 이탈 차단 + 단정 검증
import re
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, AIMessage
from pydantic import BaseModel, Field

load_dotenv()

llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
    temperature=0,
)

# 마스킹 대상: 사번, 이메일, 전화번호 (2-2와 동일)
PII_PATTERNS = {
    "emp_id": r"\b\d{8}\b",
    "phone": r"01[0-9][-\s]?\d{3,4}[-\s]?\d{4}",
    "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
}

FORBIDDEN_TOPICS = ["의료 진단", "법률 자문", "투자 추천", "주식 추천"]


def has_pii(text: str) -> bool:
    return any(re.search(p, text) for p in PII_PATTERNS.values())


def mask_pii(text: str) -> str:
    for kind, pattern in PII_PATTERNS.items():
        text = re.sub(pattern, f"[MASKED_{kind.upper()}]", text)
    return text


def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


def is_off_topic(text: str) -> bool:
    return any(t in text for t in FORBIDDEN_TOPICS)


class GroundingCheck(BaseModel):
    is_grounded: bool = Field(description="응답의 주장에 근거가 있는지")
    problematic_claim: str = Field(description="근거 없는 단정 문장, 없으면 빈 문자열")


def check_grounding(answer: str) -> GroundingCheck:
    """근거 없는 단정 검증. RAG와 같이 처리 가능합니다. """
    checker = llm.with_structured_output(GroundingCheck)
    return checker.invoke(
        "다음 어시스턴트 응답에 근거 제시 없이 규정이나 사실을 단정하는 문장이 있는지 "
        f"검사하세요. 일반 인사말이나 안내는 문제가 아닙니다.\n\n응답: {answer}"
    )


class OutputGuardrailMiddleware(AgentMiddleware):
    def after_model(self, state, runtime):
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.content:
            return None

        # 도구 호출이 붙은 응답은 건드리지 않습니다.
        # 여기서 AIMessage를 새로 만들어 돌려주면 tool_calls가 통째로 사라져
        # 도구가 실행되지 않고 HITL 승인도 걸리지 않습니다.
        # 출력 가드레일은 사용자에게 나가는 마지막 응답만 검사하면 됩니다.
        if getattr(last, "tool_calls", None):
            return None

        text = get_text(last)

        # 1. 민감정보 노출 -> 마스킹
        if has_pii(text):
            print("[guard] 출력에서 민감정보 발견, 마스킹")
            return {"messages": [AIMessage(content=mask_pii(text), id=last.id)]}

        # 2. 범위 이탈 -> 고정 문구로 대체
        if is_off_topic(text):
            print("[guard] 범위 이탈 발견, 차단")
            return {"messages": [AIMessage(
                content="죄송합니다. 그 주제는 제 담당 범위가 아닙니다.", id=last.id
            )]}

        # 3. 근거 없는 단정 -> 확인 안내 덧붙이기
        try:
            g = check_grounding(text)
            if not g.is_grounded and g.problematic_claim:
                print(f"[guard] 근거 없는 단정: {g.problematic_claim}")
                return {"messages": [AIMessage(
                    content=text + "\n\n(안내: 위 내용 중 일부는 규정 원문 확인이 "
                                   "필요할 수 있습니다. 담당 부서에 확인해 주세요.)", id=last.id
                )]}
        except Exception as e:
            print(f"[guard] 단정 검증 실패, 통과 처리: {e}")

        return None


@tool
def lookup_employee(name: str) -> str:
    """임직원 상세 정보를 조회한다."""
    # 교육용 더미 데이터입니다
    return (f"{name} / 클라우드운영팀 / 사번 25010042 / "
            "haneul.kim@samsungsds.example.com / 010-2345-6789")


if __name__ == "__main__":
    agent = create_agent(
        model=llm,
        tools=[lookup_employee],
        middleware=[OutputGuardrailMiddleware()],
    )

    print("=== 민감정보 노출 테스트 ===")
    r = agent.invoke({"messages": [HumanMessage("김하늘 연락처 알려줘")]})
    print(get_text(r["messages"][-1]))

    print("\n=== 범위 이탈 테스트 ===")
    r = agent.invoke({"messages": [HumanMessage("머리가 아픈데 의료 진단해줘")]})
    print(get_text(r["messages"][-1]))