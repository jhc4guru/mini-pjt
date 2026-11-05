"""mock 실행기: moto 서버(AWS API 모사) + 시뮬레이터(moto 미지원 명령) + kubectl 고정 응답.

실제 AWS로 나가지 않는다.
- aws 명령은 루프백 moto 서버로만 간다. 엔드포인트(`AWS_ENDPOINT_URL`)와 더미 키(`testing`)는 이 실행기가 명시적으로 지정하고,
  서버 프로세스의 환경변수(`.env`의 실제 키, 프로필 등)는 상속하지 않는다. 루프백이 아닌 엔드포인트는 거부한다.
- kubectl은 실제 바이너리를 실행하지 않고 고정 응답을 돌려준다.
- 결과에는 `mock=True`를 남긴다.
"""
from __future__ import annotations

import time
from pathlib import Path

from app.executor.base import ExecutionRequest, ExecutionResult, Executor
from app.executor.kubectl_fixture import KubectlFixture
from app.executor.moto_server import DUMMY_CREDENTIALS, MotoServer, assert_local_endpoint
from app.executor.process import ArgvError, run_cli, tokenize
from app.executor.simulator import AwsSimulator
from app.executor.world import MockWorld
from app.guards.command_guard import CommandGuard


class MockExecutor(Executor):
    name = "mock"
    mock = True

    def __init__(self, guard: CommandGuard, workspace: Path, *, moto: MotoServer | None = None, world: MockWorld | None = None):
        super().__init__(guard)
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.world = world or MockWorld()
        self.moto = moto or MotoServer()
        self._owns_moto = moto is None
        self.simulator = AwsSimulator(self.world)
        self.kubectl = KubectlFixture(self.world)

    def _run(self, request: ExecutionRequest) -> ExecutionResult:
        t0 = time.perf_counter()
        try:
            tokens = tokenize(request.command)
        except ArgvError:
            return self._result(request, "failed", 2, "", "mock: 명령을 토큰으로 나눌 수 없다", t0, ["argv_error"])
        if tokens[0] == "kubectl":
            out, err, code = self.kubectl.respond(tokens)
            return self._result(request, "ok" if code == 0 else "failed", code, out, err, t0, ["kubectl_fixture"])
        if tokens[0] != "aws":                                  # 가드가 이미 막지만 실행기에서도 거부한다
            return self._result(request, "failed", 2, "", "mock: aws와 kubectl만 실행한다", t0, ["disallowed_cli"])
        simulated = self.simulator.handle(tokens)
        if simulated is not None:
            return self._result(request, "ok", 0, simulated, "", t0, ["simulator"])
        self.moto.start()
        assert_local_endpoint(self.moto.endpoint)
        extra = {"AWS_ENDPOINT_URL": self.moto.endpoint, **DUMMY_CREDENTIALS}
        out = run_cli(request.command, workdir=self.workspace, extra_env=extra, timeout=request.timeout_seconds)
        stdout = self.simulator.decorate(tokens, out.stdout) if out.status == "ok" else out.stdout
        return ExecutionResult(request_id=request.request_id, status=out.status, exit_code=out.exit_code, stdout=stdout, stderr=out.stderr,
                               duration_seconds=out.duration, mock=True, executor=self.name, notes=("moto", *out.notes))

    def _result(self, request, status, code, out, err, t0, notes) -> ExecutionResult:
        return ExecutionResult(request_id=request.request_id, status=status, exit_code=code, stdout=out, stderr=err,
                               duration_seconds=time.perf_counter() - t0, mock=True, executor=self.name, notes=tuple(notes))

    # ---- 시연·시험용 초기 환경 ----
    def seed(self, *, cluster: str = "dev-main", asg: str = "eks-workers-a", bucket: str = "acme-logs",
             launch_template: str = "eks-workers-lt", version: str = "1.35") -> dict[str, str]:
        """moto에 기본 리소스(EKS 클러스터, ASG, launch template, 버킷)를 만든다. 이름은 합성 값이다."""
        import json

        import boto3

        self.moto.start()
        assert_local_endpoint(self.moto.endpoint)
        kw = dict(endpoint_url=self.moto.endpoint, aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        ec2, iam, eks, asg_c, s3 = (boto3.client(n, **kw) for n in ("ec2", "iam", "eks", "autoscaling", "s3"))
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
        sg = ec2.create_security_group(GroupName="mock-sg", Description="mock", VpcId=vpc)["GroupId"]
        trust = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "eks.amazonaws.com"}, "Action": "sts:AssumeRole"}]})
        role_arn = iam.create_role(RoleName="mock-eks-role", AssumeRolePolicyDocument=trust)["Role"]["Arn"]
        eks.create_cluster(name=cluster, version=version, roleArn=role_arn, resourcesVpcConfig={"subnetIds": [subnet], "securityGroupIds": [sg]})
        ami = ec2.describe_images()["Images"][0]["ImageId"]
        ec2.create_launch_template(LaunchTemplateName=launch_template, LaunchTemplateData={"ImageId": ami, "InstanceType": "t3.micro"})
        asg_c.create_auto_scaling_group(AutoScalingGroupName=asg, LaunchTemplate={"LaunchTemplateName": launch_template, "Version": "1"},
                                        MinSize=0, MaxSize=3, DesiredCapacity=1, VPCZoneIdentifier=subnet)
        s3.create_bucket(Bucket=bucket)
        self.world.control_plane_version = self.world.node_version = version
        return {"cluster": cluster, "asg": asg, "bucket": bucket, "launch_template": launch_template}

    def close(self) -> None:
        if self._owns_moto:
            self.moto.close()
