# multiturn_history.py - 권장 방식: RunnableWithMessageHistory
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory

load_dotenv()

# 세션별 대화 기록 저장소 (실제 서비스에서는 Redis나 DB로 교체합니다)
_store: dict[str, ChatMessageHistory] = {}

def get_session_history(session_id: str) -> ChatMessageHistory:
    """세션 ID별 기록 객체를 반환. 함수 시그니처만 유지하면 저장소는 무엇이든 됩니다."""
    if session_id not in _store:
        _store[session_id] = ChatMessageHistory()
    return _store[session_id]

model = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
)

# 모델에 메모리를 결합: 기록 저장과 결합이 자동으로 처리됩니다
with_memory = RunnableWithMessageHistory(model, get_session_history)

# session_id로 사용자를 구분합니다
config = {"configurable": {"session_id": "user_123"}}
print("[1차]", with_memory.invoke("안녕, 내 이름은 김하늘이야.", config=config).content)
print("[2차]", with_memory.invoke("내 이름이 뭐였지?", config=config).content)

# 다른 세션은 격리됩니다: 같은 체인, 다른 기억
other = {"configurable": {"session_id": "user_456"}}
print("[다른 사용자]", with_memory.invoke("내 이름 알아?", config=other).content)