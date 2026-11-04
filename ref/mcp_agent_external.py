# mcp_agent_external.py - 우리 서버 + 공개 MCP 서버를 한 Agent에 붙이기 (심화 과제)
import asyncio
import os
import sys
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

load_dotenv()


def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")

# 공개 서버는 여기 한 항목만 바꿔 끼웁니다. 셋 중 하나를 고르거나 직접 찾은 서버를 넣습니다.
#   time  : python -m pip install mcp-server-time      -> ["-m", "mcp_server_time", "--local-timezone", "Asia/Seoul"]
#   ddg   : python -m pip install duckduckgo-mcp-server -> ["-m", "duckduckgo_mcp_server.server"]
#   fetch : python -m pip install mcp-server-fetch      -> ["-m", "mcp_server_fetch"]
EXTERNAL = {
    "command": sys.executable,
    "args": ["-m", "mcp_server_time", "--local-timezone", "Asia/Seoul"],
    "transport": "stdio",
}

SYSTEM_PROMPT = """당신은 삼성SDS 사내 어시스턴트입니다. 교육용 더미 데이터를 조회해 답합니다.
사내 데이터(임직원, 팀, 자산, 프로젝트)는 sds-company-data 서버의 도구를, 그 밖의 정보는 다른 서버의 도구를 씁니다.
도구 결과에 근거해서만 답하세요."""


async def main():
    client = MultiServerMCPClient({
        "sds-company-data": {"command": sys.executable, "args": [SERVER_PATH], "transport": "stdio"},
        "external": EXTERNAL,
    })
    tools = await client.get_tools()
    print(f"도구 {len(tools)}개: {[t.name for t in tools]}")

    llm = ChatBedrockConverse(
        model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        region_name="us-east-1",
    )
    agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)

    # 두 서버의 도구를 모두 써야 답할 수 있는 질문으로 바꿔 봅니다
    question = "지금 서울 시각을 알려주고, 클라우드운영팀 인원도 함께 알려주세요."
    async for event in agent.astream({"messages": [("user", question)]}, stream_mode="updates"):
        for node, update in event.items():
            for m in update.get("messages", []):
                if getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        print(f"  도구 호출: {tc['name']}({tc['args']})")
                elif m.type == "tool":
                    print(f"  도구 결과: [{m.name}] {get_text(m)[:80]}")
                elif m.content:
                    print(f"  답변: {get_text(m)[:200]}")


if __name__ == "__main__":
    asyncio.run(main())