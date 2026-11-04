# masking_middleware.py - 개인정보 마스킹 미들웨어
import re
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage

load_dotenv()

# 마스킹 대상: 사번, 이메일, 전화번호 (교육용 더미 데이터 기준)
PII_PATTERNS = {
    "emp_id": r"\b\d{8}\b",                                       # 사번 (8자리 숫자)
    "phone": r"01[0-9][-\s]?\d{3,4}[-\s]?\d{4}",                  # 휴대전화
    "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",   # 이메일
}


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


def mask_message_content(message):
    """읽기가 아니라 고쳐서 되돌려주는 자리라 블록 구조를 살려 둔 채 텍스트만 마스킹합니다."""
    content = message.content
    if isinstance(content, list):
        return [
            {**block, "text": mask_pii(block["text"])}
            if isinstance(block, dict) and "text" in block else block
            for block in content
        ]
    return mask_pii(content)


class MaskingMiddleware(AgentMiddleware):
    """모델 호출 전에 사용자 입력의 개인정보를 마스킹하는 미들웨어"""

    def before_model(self, state, runtime):
        # 가장 최근 사용자 메시지를 찾습니다
        last_human = next(
            (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
            None,
        )
        if not last_human:
            return None

        masked = mask_message_content(last_human)
        if masked != last_human.content:
            print("[masking] 개인정보 마스킹 적용")
            # 찾은 사용자 메시지를 마스킹된 버전으로 교체합니다
            # 같은 id로 돌려주면 add_messages가 그 메시지를 제자리에서 교체합니다
            return {"messages": [HumanMessage(content=masked, id=last_human.id)]}
        return None


if __name__ == "__main__":
    llm = ChatBedrockConverse(
        model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        region_name="us-east-1",
    )
    agent = create_agent(model=llm, tools=[], middleware=[MaskingMiddleware()])
    # 출력할 때도 content가 블록 리스트일 수 있으므로 get_text를 거칩니다
    result = agent.invoke({"messages": [HumanMessage(
        "김하늘 사번이 25010042 맞는지 확인해줘. 연락처는 010-2345-6789이고 "
        "메일은 haneul.kim@samsungsds.example.com이야."
    )]})
    print("최종 응답:", get_text(result["messages"][-1]))