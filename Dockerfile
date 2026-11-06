# DevOps Plan/Apply 에이전트 (Phase 1: mock 실행기). docs/DECISIONS.md D-54
#
# 이미지 안에 Python 3.14, AWS CLI v2, kubectl, 앱 패키지를 모두 넣는다. 자격증명(.env)은 이미지에 넣지 않고 실행할 때 --env-file로 준다.
#
#   docker build -t devops-agent .
#   docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -v devops-agent-data:/data devops-agent
#   → 브라우저에서 http://localhost:8000  (직접 실행과 같은 8000. 둘을 동시에 띄우면 포트가 겹치므로 하나만 실행한다)
#
# 호스트 쪽 포트는 반드시 127.0.0.1로만 연다(앱은 닉네임·토큰 인증뿐이다). 컨테이너 안에서는 0.0.0.0으로 듣는다(ALLOW_NON_LOOPBACK=1).
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip groff-base less \
 && rm -rf /var/lib/apt/lists/*

# groff·less: `aws <서비스> <명령> help`가 도움말 렌더러(groff 또는 mandoc)를 요구한다. 없으면 가드의 옵션 스냅샷을 못 만들어 모든 명령이 막힌다.
# kubectl: 공식 배포 파일 + 공식 SHA-256 검증. 버전을 고정하려면 --build-arg KUBECTL_VERSION=v1.xx.y
ARG KUBECTL_VERSION=
RUN set -eux; \
    case "$(uname -m)" in x86_64) k=amd64 ;; aarch64) k=arm64 ;; *) echo "지원하지 않는 CPU: $(uname -m)"; exit 1 ;; esac; \
    ver="${KUBECTL_VERSION:-$(curl -fsSL https://dl.k8s.io/release/stable.txt)}"; \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${ver}/bin/linux/${k}/kubectl"; \
    echo "$(curl -fsSL "https://dl.k8s.io/release/${ver}/bin/linux/${k}/kubectl.sha256")  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/kubectl; \
    kubectl version --client

# AWS CLI v2: 공식 설치 파일(HTTPS). 서명(GPG) 검증은 하지 않았다(docs/DECISIONS.md D-54 한계).
RUN set -eux; \
    curl -fsSLo /tmp/awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip"; \
    unzip -q /tmp/awscliv2.zip -d /tmp; \
    /tmp/aws/install; \
    rm -rf /tmp/aws /tmp/awscliv2.zip; \
    aws --version

WORKDIR /app
COPY requirements-runtime.txt .
RUN pip install -r requirements-runtime.txt

# 실행은 비root 사용자로. /data는 실행 중 생성물(색인·DB·키)이라 볼륨으로 둔다.
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app:app /data /app
USER app
ENV DEVOPS_DATA_DIR=/data \
    ALLOW_NON_LOOPBACK=1

COPY --chown=app:app app ./app
COPY --chown=app:app knowledge ./knowledge
COPY --chown=app:app inventory.example.yaml README.md ./

# 가드의 옵션 스냅샷(실제 CLI 도움말에서 만든다)을 미리 만들어 첫 실행을 빠르게 한다. 이 이미지의 CLI 버전과 일치한다.
# (named volume을 처음 만들 때 이 내용이 /data로 복사된다. 빈 bind mount를 쓰면 첫 실행 때 다시 만든다.)
# 문서의 모든 명령이 스냅샷에 있어야 한다(없으면 이 이미지에서는 가드가 명령을 막는다). 하나라도 빠지면 빌드를 실패시킨다.
RUN python -c "from app.guards.cli_snapshot import CliSnapshot; from app.rag.chunks import load_chunks; keys = [(c.cli, c.service, c.name) for c in load_chunks()]; s = CliSnapshot(); s.build(keys, only_missing=True); missing = [k for k in keys if s.get(*k) is None]; print('스냅샷', len(s._specs), '개, 문서 명령', len(keys), '개, 없음', len(missing)); assert not missing, missing[:5]"

VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.web.main", "--host", "0.0.0.0", "--port", "8000"]
