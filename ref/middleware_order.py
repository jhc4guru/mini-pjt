# middleware_order.py - 미들웨어 조합의 실행 순서 관찰
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage

load_dotenv()


class TagMiddleware(AgentMiddleware):
    """순서 확인용: 이름표를 붙여 훅 호출 순서를 출력"""

    def __init__(self, tag: str):
        super().__init__()
        self.tag = tag

    @property
    def name(self) -> str:
        # 미들웨어는 이름으로 구분합니다. 같은 클래스를 여러 개 넣으려면 이름이 서로 달라야 합니다
        return f"TagMiddleware_{self.tag}"

    def before_model(self, state, runtime):
        print(f"[{self.tag}] before_model")
        return None

    def after_model(self, state, runtime):
        print(f"[{self.tag}] after_model")
        return None


llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
)
agent = create_agent(
    model=llm,
    tools=[],
    middleware=[TagMiddleware("A"), TagMiddleware("B"), TagMiddleware("C")],
)
agent.invoke({"messages": [HumanMessage("안녕이라고만 답해줘")]})