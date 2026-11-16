#!/usr/bin/env bash
# 修复 GPU 设备节点缺失 —— 让 nvidia-smi / torch.cuda 能用上显卡
#
# ## 背景
#
# 这台机器上 nvidia 内核模块**是加载的** (595.99.02), 驱动也认到了卡
# (/proc/driver/nvidia/gpus/0000:01:00.0 存在), 但 `/dev/nvidia*` 设备
# 节点缺失 —— 于是:
#
#   - `nvidia-smi` 报 "couldn't communicate with the NVIDIA driver"
#   - `torch.cuda.is_available()` 返回 False
#   - YOLO 只能走 CPU, 训练慢到不可用
#
# 根因: udev 规则 `/usr/lib/udev/rules.d/71-nvidia.rules` 里的
# `RUN+="/sbin/ub-device-create"` 只在 **PCI 驱动 bind 的那一刻** 触发。
# 如果模块是在规则生效前就加载的 (比如内核启动早期), 节点就永远建不出来。
#
# ## 用法
#
#   sudo ./scripts/fix_gpu.sh          # 修完顺便验证
#   ./scripts/fix_gpu.sh --check       # 只检查不修
#
# **需要 sudo** —— 建设备节点是 root 权限的操作。
set -euo pipefail

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

ok()   { echo "[ok]   $*"; }
warn() { echo "[warn] $*"; }
fail() { echo "[FAIL] $*"; }

# ------------------------------------------------------------ 现状

echo "=== GPU 现状 ==="
if ls /dev/nvidia0 >/dev/null 2>&1; then
    ok "/dev/nvidia* 设备节点已存在"
    NODES_OK=1
else
    warn "/dev/nvidia* 设备节点缺失"
    NODES_OK=0
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi >/dev/null 2>&1; then
        ok "nvidia-smi 正常"
    else
        warn "nvidia-smi 失败 (设备节点或驱动有问题)"
    fi
else
    warn "nvidia-smi 不存在"
fi

if [ "$CHECK_ONLY" = "1" ]; then
    echo
    echo "=== torch 验证 ==="
    if command -v conda >/dev/null 2>&1; then
        conda run -n yolo python -c "
import torch
print('  cuda available:', torch.cuda.is_available())
if torch.cuda.is_available():
    print('  device:', torch.cuda.get_device_name(0))
" 2>/dev/null || echo "  (跳过 torch 验证)"
    fi
    exit 0
fi

# ------------------------------------------------------------ 修复

if [ "$NODES_OK" = "1" ]; then
    echo
    ok "节点已存在, 无需修复"
else
    echo
    echo "=== 尝试重建设备节点 ==="

    # 优先走 udev —— 让内核按正确的 major/minor 建节点
    if command -v udevadm >/dev/null 2>&1; then
        udevadm trigger --subsystem-match=nvidia --action=add 2>/dev/null || true
        udevadm trigger --subsystem-match=pci --action=add 2>/dev/null || true
        sleep 2
    fi

    if ls /dev/nvidia0 >/dev/null 2>&1; then
        ok "udev 触发成功, 节点已建出"
    else
        # udev 不灵 (规则没生效 / 容器里没有 udev) —— 手工建节点
        #
        # major/minor 从 /proc/devices 查, 不能瞎写:
        #   nvidia       -> 195, 次设备号 0   (nvidia0)
        #   nvidiactl    -> 195, 次设备号 255 (nvidiactl)
        #   nvidia-modeset -> 195, 次设备号 254
        #   nvidia-uvm   -> 507, 次设备号 0   (nvidia-uvm)
        warn "udev 没建出节点, 手工 mknod"

        dev_major=$(awk '$2=="nvidia" {print $1}' /proc/devices | head -1)
        uvm_major=$(awk '$2=="nvidia-uvm" {print $1}' /proc/devices | head -1)

        if [ -z "$dev_major" ] || [ -z "$uvm_major" ]; then
            fail "从 /proc/devices 读不到 nvidia 的 major 号 —— 驱动可能没真正加载"
            exit 1
        fi

        [ -e /dev/nvidia0 ]         || mknod -m 666 /dev/nvidia0 c "$dev_major" 0
        [ -e /dev/nvidiactl ]      || mknod -m 666 /dev/nvidiactl c "$dev_major" 255
        [ -e /dev/nvidia-modeset ] || mknod -m 666 /dev/nvidia-modeset c "$dev_major" 254
        [ -e /dev/nvidia-uvm ]     || mknod -m 666 /dev/nvidia-uvm c "$uvm_major" 0

        ok "手工节点已建 (nvidia major=$dev_major, uvm major=$uvm_major)"
    fi
fi

# nvidia_uvm 模块没加载的话, 深度学习要用它 (cudaMalloc 走 uvm)
if ! lsmod 2>/dev/null | grep -q "^nvidia_uvm"; then
    echo
    echo "=== 加载 nvidia_uvm 模块 ==="
    modprobe nvidia_uvm 2>/dev/null && ok "nvidia_uvm 已加载" || warn "nvidia_uvm 加载失败 (不影响推理, 但影响部分显存管理)"
fi

# ------------------------------------------------------------ 验证

echo
echo "=== 验证 ==="
ls -la /dev/nvidia* 2>/dev/null || fail "设备节点仍不存在"

if nvidia-smi >/dev/null 2>&1; then
    ok "nvidia-smi 正常"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/       /'
else
    fail "nvidia-smi 仍然失败"
    exit 1
fi

echo
echo "=== torch 验证 ==="
if command -v conda >/dev/null 2>&1; then
    conda run -n yolo python - <<'PY'
import torch
print("  cuda available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("  device:", torch.cuda.get_device_name(0))
    x = torch.randn(512, 512, device="cuda")
    print("  GPU 算子测试:", "PASS" if (x @ x).sum().item() != 0 else "FAIL")
else:
    print("  FAIL: CUDA 仍不可用")
    raise SystemExit(1)
PY
else
    python - <<'PY'
import torch
print("  cuda available:", torch.cuda.is_available())
PY
fi

echo
ok "GPU 已就绪, 可以训练了"
