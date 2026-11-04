# logging_middleware.py - 모든 요청과 응답을 agent_log.jsonl에 기록
import json
import datetime
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


def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


class LoggingMiddleware(AgentMiddleware):
    """모델 호출 전후를 agent_log.jsonl 파일에 기록하는 미들웨어"""

    def _write(self, record: dict) -> None:
        record["time"] = datetime.datetime.now().isoformat()
        with open("agent_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def before_model(self, state, runtime):
        """모델 호출 전: 마지막 입력 메시지를 기록합니다."""
        last = state["messages"][-1] if state["messages"] else None
        if last:
            self._write({
                "event": "before_model",
                "message_type": type(last).__name__,
                "preview": get_text(last)[:80],
            })
        return None  # State 변경 없음

    def after_model(self, state, runtime):
        """모델 호출 후: 응답 타입, 도구 호출 결정, 토큰 사용량을 기록합니다."""
        last = state["messages"][-1]
        record = {"event": "after_model", "message_type": type(last).__name__}
        if hasattr(last, "tool_calls") and last.tool_calls:
            record["tool_calls"] = [t["name"] for t in last.tool_calls]
        if hasattr(last, "usage_metadata") and last.usage_metadata:
            record["total_tokens"] = last.usage_metadata.get("total_tokens", 0)
        self._write(record)
        return None


if __name__ == "__main__":
    llm = ChatBedrockConverse(
        model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        region_name="us-east-1",
    )
    agent = create_agent(
        model=llm,
        tools=[search_employee],
        middleware=[LoggingMiddleware()],
    )
    result = agent.invoke({"messages": [HumanMessage("김하늘 소속 팀 알려줘")]})
    print("최종 응답:", get_text(result["messages"][-1]))