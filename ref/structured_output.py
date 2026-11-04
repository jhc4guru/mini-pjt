# structured_output.py - 응답을 JSON 스키마로 받기
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from langchain_aws import ChatBedrockConverse
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser

load_dotenv()

## 삼성SDS 사내 문의 텍스트를 분류용 JSON으로 구조화 Example
# 1) 출력 스키마 정의 - Field의 description이 모델에게 전달되는 설명입니다
class InquiryAnalysis(BaseModel):
    category: str = Field(description="문의 분류: 'IT지원', '인사', '총무' 중 하나")
    urgency: str = Field(description="긴급도: '높음', '보통', '낮음' 중 하나")
    summary: str = Field(description="문의 내용 한 줄 요약")

# 2) 파서 생성 - 스키마 기반 형식 지침을 자동 생성합니다
parser = JsonOutputParser(pydantic_object=InquiryAnalysis)

# 3) 프롬프트에 형식 지침 끼워 넣기
#    partial_variables: 사용자 입력이 아닌 값을 미리 채워 두는 자리입니다, 여기서는 위에 정의된 json 스키마 형식으로 보내달라고 전달해주는 역할이라고 봐라
prompt = PromptTemplate(
    template="다음 사내 문의를 분석해줘.\n\n문의: {inquiry}\n\n{format_instructions}",
    input_variables=["inquiry"],
    partial_variables={"format_instructions": parser.get_format_instructions()},
)

llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
    temperature=0,
)
chain = prompt | llm | parser

result = chain.invoke({"inquiry": "노트북이 갑자기 안 켜지는데 오후에 고객 발표가 있습니다. 빨리 도와주세요."})

print("타입:", type(result).__name__)
print("전체:", result)
print("분류만:", result["category"])