# hook_trace.py - 네 가지 훅의 호출 순서 관찰
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage

load_dotenv()


@tool
def search_employee(name: str) -> str:
    """삼성SDS 임직원 디렉터리에서 이름으로 직원 정보를 조회한다."""
    # 교육용 더미 데이터입니다
    fake_db = {
        "김하늘": "김하늘 / 클라우드운영팀 / haneul.kim@samsungsds.example.com",
        "박도윤": "박도윤 / 물류플랫폼팀 / doyun.park@samsungsds.example.com",
    }
    return fake_db.get(name, f"{name}을(를) 찾을 수 없습니다.")


class HookTraceMiddleware(AgentMiddleware):
    """모든 훅에 print를 넣어 호출 순서를 관찰하는 미들웨어"""

    def before_model(self, state, runtime):
        print(f"[1] before_model: 모델 호출 직전: {state}")
        return None

    def after_model(self, state, runtime):
        print(f"[2] after_model: 모델 호출 직후: {state}")
        return None
    
    def wrap_tool_call(self, request, handler):
        print(f"[3] wrap_tool_call: 도구 실행 직전 : {request}")
        result = handler(request)
        print(f"[4] wrap_tool_call: 도구 실행 직후 : {result}")
        return result


llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
)

agent = create_agent(
    model=llm,
    tools=[search_employee],
    middleware=[HookTraceMiddleware()],
)

def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


result = agent.invoke({"messages": [HumanMessage("김하늘 정보 조회해줘")]})
print("최종 응답:", get_text(result["messages"][-1]))