"""跨服务共享的小工具。

目前只有 :mod:`common.audio_output` —— TTS 播放后端,
asr_service 和 arm_service 都要用 (两边在不同的 conda 环境里跑,
但这个模块只用标准库, 没有任何环境依赖)。
"""
