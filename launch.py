"""
OCI Always Free A1.Flex 인스턴스 생성 재시도 스크립트.
GitHub Actions에서 주기적으로 실행되며, 결과를 GITHUB_OUTPUT에 기록한다.
  result=created   : 이번 실행에서 생성 성공
  result=exists    : 같은 이름의 인스턴스가 이미 있음 (중복 생성 방지)
  result=capacity  : 용량 부족 등으로 실패, 다음 실행에서 재시도
"""
import os
import sys

import oci

env = os.environ.get

INSTANCE_NAME = env("INSTANCE_NAME", "free-a1")
SHAPE = "VM.Standard.A1.Flex"
OCPUS = float(env("OCPUS", "2"))
MEMORY_GB = float(env("MEMORY_GB", "12"))
BOOT_GB = int(env("BOOT_GB", "100"))
OS_NAME = env("OS_NAME", "Canonical Ubuntu")
OS_VERSION = env("OS_VERSION", "22.04")


def set_output(result: str, detail: str = "") -> None:
    print(f"RESULT: {result} {detail}".strip())
    out = env("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"result={result}\n")
            # 여러 줄이 들어가지 않도록 한 줄로 정리
            f.write(f"detail={detail.replace(chr(10), ' ')}\n")


config = {
    "user": env("OCI_USER_OCID"),
    "tenancy": env("OCI_TENANCY_OCID"),
    "fingerprint": env("OCI_FINGERPRINT"),
    "key_content": env("OCI_PRIVATE_KEY"),
    "region": env("OCI_REGION"),
}
oci.config.validate_config(config)

compartment_id = env("OCI_COMPARTMENT_OCID") or config["tenancy"]
subnet_id = env("OCI_SUBNET_OCID")
ssh_key = env("SSH_PUBLIC_KEY")
if not subnet_id or not ssh_key:
    sys.exit("OCI_SUBNET_OCID와 SSH_PUBLIC_KEY secret이 필요합니다.")

compute = oci.core.ComputeClient(config)
identity = oci.identity.IdentityClient(config)

# 1) 이미 만들어진 인스턴스가 있으면 아무것도 하지 않음
existing = [
    i
    for i in compute.list_instances(compartment_id, display_name=INSTANCE_NAME).data
    if i.lifecycle_state not in ("TERMINATED", "TERMINATING")
]
if existing:
    set_output("exists", existing[0].id)
    sys.exit(0)

# 2) 이미지: 직접 지정하지 않으면 최신 Ubuntu (ARM 호환) 이미지 자동 선택
image_id = env("OCI_IMAGE_OCID")
if not image_id:
    images = compute.list_images(
        compartment_id,
        operating_system=OS_NAME,
        operating_system_version=OS_VERSION,
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        sys.exit(f"{OS_NAME} {OS_VERSION} 이미지를 찾지 못했습니다.")
    image_id = images[0].id
    print(f"Image: {images[0].display_name}")

# 3) 모든 가용 도메인(AD)을 돌면서 시도
ads = [ad.name for ad in identity.list_availability_domains(config["tenancy"]).data]
last_error = ""
for ad in ads:
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=compartment_id,
        display_name=INSTANCE_NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=MEMORY_GB
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id, boot_volume_size_in_gbs=BOOT_GB
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )
    try:
        inst = compute.launch_instance(details).data
        set_output("created", f"{inst.id} ({ad})")
        sys.exit(0)
    except oci.exceptions.ServiceError as e:
        last_error = f"{ad}: {e.status} {e.code} - {e.message}"
        print(last_error)
        # 용량 부족(500 InternalError)·요청 과다(429)는 재시도 대상
        # 그 외(권한, 한도 초과, 잘못된 OCID 등)는 설정 문제이므로 실패로 표시
        if e.status not in (429, 500, 503):
            sys.exit(f"설정 오류로 중단: {last_error}")

set_output("capacity", last_error)
