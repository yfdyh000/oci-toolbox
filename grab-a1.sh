#!/bin/bash
set -euo pipefail

# ========== 在这里修改你的参数 ==========
COMPARTMENT_ID="ocid1.tenancy.oc1..……"
SUBNET_ID="ocid1.subnet.oc1.ap-tokyo-1.……"
IMAGE_ID="ocid1.image.oc1.ap-tokyo-1.……"
SSH_KEY_FILE="$HOME/.oci/oci_public.pem"
REGION="ap-tokyo-1"
# 目标规格 2OCPU 12GB
OCPUS=2
MEM=12
AD_LIST=("dUjU:AP-TOKYO-1-AD-1")
SLEEP=100 # 基础轮询间隔，60秒（不要低于60）
RATE_LIMIT_SLEEP=300 # 遇到429限流，等待5分钟
# =======================================

echo "==== A1 抢机脚本启动，目标：${OCPUS}OCPU ${MEM}GB ===="
echo "轮询AD列表：${AD_LIST[*]}"
echo "轮询间隔：${SLEEP}s"

while true; do
  for AD in "${AD_LIST[@]}"; do
    echo "----------------------------------------"
    echo "尝试在 $AD 创建实例 $(date)"
    if oci compute instance launch \
      --compartment-id "$COMPARTMENT_ID" \
      --availability-domain "$AD" \
      --shape "VM.Standard.A1.Flex" \
      --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM}" \
      --image-id "$IMAGE_ID" \
      --subnet-id "$SUBNET_ID" \
      --display-name "arm-1c6g" \
      --assign-public-ip true \
      --ssh-authorized-keys-file "$SSH_KEY_FILE"; then
      echo "✅ 成功！实例创建完成！"
      exit 0
    fi
    ret=$?
    if [[ $ret -eq 429 ]]; then
      echo "⚠️ 429限流，等待${RATE_LIMIT_SLEEP}秒"
      sleep $RATE_LIMIT_SLEEP
    fi
  done
  echo "本轮所有AD尝试完毕，等待${SLEEP}s再次循环"
  sleep $SLEEP
done
