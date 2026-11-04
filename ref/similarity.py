# similarity.py - 코사인 유사도 체험
import math
from dotenv import load_dotenv
from langchain_aws import BedrockEmbeddings

load_dotenv()
embeddings = BedrockEmbeddings(
    model_id="amazon.titan-embed-text-v2:0",
    region_name="us-east-1",
)


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb)


pairs = [
    ("연차 며칠 받을 수 있어?", "월차 휴가 일수 알려줘"),   # 같은 의미, 다른 단어
    ("연차 며칠 받을 수 있어?", "VPN 설정 방법"),           # 무관한 주제
    ("Python", "파이썬"),                                    # 다른 언어, 같은 의미
    ("출장비 정산", "경비 처리 기한"),                       # 비슷한 업무 영역
]

for a, b in pairs:
    va, vb = embeddings.embed_query(a), embeddings.embed_query(b)
    print(f"'{a}' vs '{b}' -> {cosine(va, vb):.3f}")