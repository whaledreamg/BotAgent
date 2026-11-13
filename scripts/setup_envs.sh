#!/usr/bin/env bash
# 在新机器上重建全部 conda 环境。
#
# 为什么不用 `conda env export` 出来的 yml:
#   export 产出的是"这台机器的快照", 会把三样不该固化的东西写死:
#     1. conda build 号  (如 bzip2=1.0.8=h5eee18b_6) —— 平台相关, 新机器上无解
#     2. prefix 绝对路径 (/home/gxh/miniconda3/envs/xxx) —— 新机器路径不同
#     3. pip local version (torch==2.6.0+cu124) —— PyPI 上根本不存在这个版本
#   实测这三个都会让 `conda env create` 在新机器上失败。
#
# 所以这里的分工是:
#   conda 只负责 python 本体
#   pip  只负责纯 PyPI 包 (requirements/*.txt)
#   torch 单独用 pytorch 官方索引装
#
# 用法:
#   ./scripts/setup_envs.sh              # 装全部
#   ./scripts/setup_envs.sh asrt         # 只装一个
#   ./scripts/setup_envs.sh asrt yolo
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQ_DIR="$ROOT/requirements"

# torch 的 CUDA 版本, 换卡时改这里 (cu118 / cu121 / cu124 / cu126)
PYTORCH_INDEX="${PYTORCH_INDEX:-https://download.pytorch.org/whl/cu124}"
# TTS 用的 torch 版本
TTS_TORCH_VER="${TTS_TORCH_VER:-2.6.0}"
TTS_TORCHAUDIO_VER="${TTS_TORCHAUDIO_VER:-2.6.0}"
TTS_TORCHVISION_VER="${TTS_TORCHVISION_VER:-0.21.0}"

log()  { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

need_conda() {
    command -v conda >/dev/null 2>&1 || die "找不到 conda, 请先安装 Miniconda/Anaconda"
}

# 建 env (已存在则跳过, 不会动已有环境)
create_env() {
    local name="$1" pyver="$2"
    if conda env list | awk '{print $1}' | grep -qx "$name"; then
        log "环境 $name 已存在, 跳过创建"
    else
        log "创建环境 $name (python=$pyver)"
        conda create -y -n "$name" "python=$pyver" pip
    fi
}

# 在指定 env 里跑 pip
pipin() {
    local env="$1"; shift
    conda run --no-capture-output -n "$env" python -m pip "$@"
}

# 装单个 requirements 文件
install_reqs() {
    local env="$1" file="$2"
    log "安装 $file -> $env"
    pipin "$env" install -r "$file"
}

setup_asrt() {
    create_env asrt 3.10
    install_reqs asrt "$REQ_DIR/asr.txt"
    warn "PyAudio 需要系统库: sudo apt install -y portaudio19-dev"
}

setup_agent() {
    # Agent 与 Arm 共用 botagent
    create_env botagent 3.11
    install_reqs botagent "$REQ_DIR/agent.txt"
}

setup_tts() {
    create_env chattts 3.11
    # torch 必须先装, 且用不带 local version 的写法。
    # 先装是为了让后面的包看到已满足的 torch 约束, 避免 resolver 把 torch 降级。
    log "安装 torch (索引: $PYTORCH_INDEX)"
    pipin chattts install \
        "torch==$TTS_TORCH_VER" \
        "torchaudio==$TTS_TORCHAUDIO_VER" \
        "torchvision==$TTS_TORCHVISION_VER" \
        --index-url "$PYTORCH_INDEX"
    install_reqs chattts "$REQ_DIR/tts.txt"
}

setup_vision() {
    create_env yolo 3.11
    # ⚠️ 顺序很重要:
    #   pyorbbecsdk2 要求 numpy<2.0, 而 opencv-python 5.x 要求 numpy>=2,
    #   两者同时进 resolver 必然失败。
    #   先装 opencv + numpy, 再用 --no-deps 装 pyorbbecsdk2, 绕开它的依赖约束。
    log "安装 vision 依赖 (torch + opencv)"
    pipin yolo install \
        "torch==2.15.0" "torchvision==0.29.2" \
        "numpy==2.4.6" "opencv-python==4.14.0.94" \
        "ultralytics==8.4.208" "ultralytics-thop==2.2.5"
    log "安装 pyorbbecsdk2 (--no-deps, 避免 numpy 约束冲突)"
    pipin yolo install --no-deps "pyorbbecsdk2==2.1.3"
    log "安装 vision 剩余依赖"
    pipin yolo install \
        "fastapi==0.162.4" "uvicorn==0.54.0" "websockets==17.4" \
        "pyyaml==6.0.3" "requests==2.34.2" "pydantic==2.14.3"
    warn "pyorbbecsdk2 的 wheel 没有 USB PAL, 必须源码编译 OrbbecSDK, 见 docs/vision-service.md"
}

main() {
    need_conda
    local targets=("$@")
    if [ ${#targets[@]} -eq 0 ]; then
        targets=(asrt agent tts vision)
    fi
    for t in "${targets[@]}"; do
        case "$t" in
            asrt)   setup_asrt ;;
            agent)  setup_agent ;;
            tts)    setup_tts ;;
            vision) setup_vision ;;
            *)      die "未知目标: $t (可选: asrt agent tts vision)" ;;
        esac
    done

    cat <<'EOF'

============================================================
环境创建完成。还需要手工做的事:

  1. Vosk 中文模型
       下载中文模型解压到 ~/.cache/vosk/
       configs/pipeline.yaml 的 model_path 留空时会自动在这里找

  2. OrbbecSDK 源码编译 (Vision 服务必需)
       pyorbbecsdk2 的 wheel 没有 USB PAL, 必须自己编:
         cd <OrbbecSDK_v2>
         mkdir -p build && cd build
         cmake .. -DOB_BUILD_USB_PAL=ON -DOB_BUILD_NET_PAL=ON
         cmake --build . -j$(nproc)
       运行时设置 (scripts/start_vision.sh 会自动找):
         export LD_LIBRARY_PATH=<OrbbecSDK_v2>/build/linux_x86_64/lib:$LD_LIBRARY_PATH
       详见 docs/vision-service.md

  3. udev 规则
         sudo cp <OrbbecSDK_v2>/scripts/env_setup/99-obsensor-libusb.rules /etc/udev/rules.d/
         sudo udevadm trigger && sudo udevadm settle

  4. 系统依赖
         sudo apt install -y portaudio19-dev libsndfile1

  5. 配置密钥
         cp configs/.env.example configs/.env 并填入 LLM / API key
EOF
}

main "$@"
