# inquiry_chain.py - 사내 문의 응답
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from langchain_aws import ChatBedrockConverse
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from langchain_core.runnables import RunnableParallel

load_dotenv()
llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
    temperature=0,
)

# --- 부품 A: 문의 분류 체인 (구조화 출력) ---
class Routing(BaseModel):
    department: str = Field(description="담당 부서: 'IT지원팀', '인사팀', '총무팀' 중 하나")
    urgency: str = Field(description="긴급도: '높음', '보통', '낮음' 중 하나")

routing_parser = JsonOutputParser(pydantic_object=Routing)
routing_prompt = PromptTemplate(
    template="다음 사내 문의의 담당 부서와 긴급도를 판단해.\n\n문의: {inquiry}\n\n{format_instructions}",
    input_variables=["inquiry"],
    partial_variables={"format_instructions": routing_parser.get_format_instructions()},
)
routing_chain = routing_prompt | llm | routing_parser

# --- 부품 B: 답변 초안 체인 (문자열 출력) ---
answer_prompt = ChatPromptTemplate.from_messages([
    ("system",
     "너는 삼성SDS 사내 헬프데스크 상담원이야. 정중한 존댓말로 답하고 "
     "마지막 문장에 예상 처리 기한을 안내해. 세 문장 이내로 써."),
    ("human", "{inquiry}"),
])
answer_chain = answer_prompt | llm | StrOutputParser()

# --- 조립: 두 체인을 병렬 실행 후 하나의 dict로 합칩니다 ---
full_chain = RunnableParallel(routing=routing_chain, draft=answer_chain)

# --- 실행 ---
inquiry = {"inquiry": "재택근무 신청 절차가 어떻게 되나요? 다음 주부터 필요합니다."}
result = full_chain.invoke(inquiry)

print("담당 부서:", result["routing"]["department"])
print("긴급도:", result["routing"]["urgency"])
print("답변 초안:")
print(result["draft"])