# summarization_demo.py - 내장 요약 미들웨어 관찰 (코드 리딩)
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_aws import ChatBedrockConverse
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import HumanMessage

load_dotenv()

llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
)

agent = create_agent(
    model=llm,
    tools=[],
    middleware=[
        SummarizationMiddleware(
            model=llm,
            trigger=("tokens", 500),        # 실습용으로 낮게 잡아 요약을 빨리 관찰
            keep=("messages", 4),            # 최근 4개만 원본 유지
        ),
    ],
    checkpointer=InMemorySaver(),  # 멀티턴이어야 요약을 관찰할 수 있습니다
)

config = {"configurable": {"thread_id": "summary_demo"}}
questions = [
    "사내 어시스턴트가 뭘 할 수 있는지 세 문장으로 설명해줘",
    "LangChain의 장점을 다섯 문장으로 설명해줘",
    "LangGraph와의 차이를 다섯 문장으로 설명해줘",
    "지금까지 대화를 바탕으로 오늘 배울 주제를 추천해줘",
]
for q in questions:
    result = agent.invoke({"messages": [HumanMessage(q)]}, config=config)
    print(f"질문: {q}")
    #print(f"{result['messages']}")
    print(f"  현재 메시지 개수: {len(result['messages'])}")