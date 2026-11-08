# Vosk 实时语音识别（唤醒词 + 流式转写）

基于 conda `vosk` 环境，实现「自定义唤醒词 → 开始录音 → 实时流式转文字 → 保存 WAV」。
纯本地推理，不需要训练、不需要联网。

## 环境准备（已完成）

```bash
# 依赖
/home/gxh/miniconda3/envs/vosk/bin/pip install \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    vosk==0.3.45 sounddevice numpy

# 中文小模型（43MB）
curl -L -o /tmp/vosk-cn.zip \
    https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
unzip -q /tmp/vosk-cn.zip -d ~/.cache/vosk/
```

模型目录 `~/.cache/vosk/vosk-model-small-cn-0.22` 会被脚本自动找到，也可用
`--model` 或环境变量 `VOSK_MODEL_PATH` 指定。

## 使用

```bash
cd /home/gxh/workspace/project/ASRT

# 基本用法
/home/gxh/miniconda3/envs/vosk/bin/python wakeup_asr.py --wake "你好助手"

# 多个唤醒词
/home/gxh/miniconda3/envs/vosk/bin/python wakeup_asr.py --wake "你好助手,小助手"

# 指定麦克风（不指定则自动挑原生 16kHz 的设备）
/home/gxh/miniconda3/envs/vosk/bin/python wakeup_asr.py --wake "你好助手" --device 5

# 用 wav 文件回放测试，不占麦克风
/home/gxh/miniconda3/envs/vosk/bin/python wakeup_asr.py --replay test.wav --wake "你好助手"
```

运行后：

```
[模型] /home/gxh/.cache/vosk/vosk-model-small-cn-0.22
[语法] ["你好", "助手"]
[麦克风] sof-hda-dsp: - (hw:0,7) (index=5, 输入声道=2, 原生采样率=16000)

[*] 正在监听唤醒词, 按 Ctrl+C 退出
[*] 唤醒词: 你好助手  (词表切分: 你好 + 助手)

[电平] |||||...............  -42.5 dB  状态: IDLE
...
============================================================
已唤醒! 唤醒词: [你好助手]  请开始说话...
============================================================
    … 今天 天气 怎么样
✓ 今天 天气 怎么样

[结束] 原因: 静音 1.2s   时长: 3.4s
[完整文本] 今天天气怎么样
[已保存] /home/gxh/workspace/project/ASRT/recordings/rec_20261106_152130.wav
============================================================
```

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--wake` | `你好助手` | 唤醒词，多个用逗号分隔 |
| `--model` | 自动查找 | 模型目录 |
| `--device` | 自动挑选 | 输入设备索引 |
| `--duration` | `30` | 单次录音最长秒数 |
| `--silence` | `1.2` | 静音多少秒后结束录音 |
| `--vad-threshold` | `-45` | 语音活动检测最低电平(dB) |
| `--replay` | 无 | 用 16kHz wav 回放测试 |
| `--no-save` | 关 | 不保存 WAV |
| `--quiet` | 关 | 不显示电平条 |

## 实现要点

### 唤醒词为什么要分词

vosk 的语法受限解码（grammar）**只能使用模型词表里存在的词**。直接传
`"你好助手"` 会被拒绝：

```
WARNING (VoskAPI:UpdateGrammarFst():recognizer.cc:308) Ignoring word missing in vocabulary: '你好助手'
```

脚本用 `Model.vosk_model_find_word()` 查询词表，把唤醒词按最长优先贪心切分成
词表里有的单元：

| 唤醒词 | 切分结果 |
| --- | --- |
| 你好助手 | `你好` + `助手` |
| 小助手 | `小助手` |
| 芝麻开门 | `芝麻` + `开门` |
| 机器人 | `机器人` |

分词失败的唤醒词会在启动时报错并忽略。

### 双重判定（关键设计）

单独用语法识别器做唤醒，**误触发率极高**——语法解码器会在任何语音里强行凑出
词表里的词。实测在 4 段无关中文语音上：

| 方案 | 召回 | 误触发 |
| --- | --- | --- |
| 仅语法识别器 + 子串匹配 | 5/5 | **3/4** |
| 仅通用识别器 + 子串匹配 | 4/5 | 0/4 |
| **语法 + 通用 双重判定** | **5/5** | **0/4** |

因此脚本让两个识别器并行处理同一条音频流，**两者同时命中同一个唤醒词才触发**：

- 语法识别器：灵敏度高，负责捕捉模糊的唤醒发音
- 通用识别器：精度高，负责排除误触发

```
音频 ──┬─→ 语法识别器 (grammar=[你好, 助手])  ──┐
       │                                        ├─→ AND → 唤醒
       └─→ 通用识别器 (自由中文)               ──┘
```

### 大模型不支持语法图

`vosk-model-cn-0.22`（大模型）只有静态 `HCLG.fst`，没有 `Gr.fst`，
vosk 会报 `Runtime graphs are not supported by this model`，语法受限解码无法使用。

脚本会自动检测 `graph/Gr.fst` 是否存在：

- 存在 → 走「语法 + 通用」双重判定（召回和抗误触发都最好）
- 不存在 → 退化为只用通用识别器匹配（实测误触发同样是 0/4，只是模糊发音的召回略低）

用 `--model` 可显式指定模型：

```bash
python wakeup_asr.py --model ~/.cache/vosk/vosk-model-cn-0.22 --wake "你好助手"
```

### 其余机制

- **统一采集**：`RawInputStream` 16kHz/单声道/16bit，每块 4096 样本（256ms）
- **环形缓冲**：保留最近约 16s 音频，用于唤醒时的前滚
- **前滚**：唤醒时补上之前 0.5s 的音频，避免吞掉紧跟唤醒词的第一句话
- **录音状态机**：`IDLE` → `RECORDING` → `IDLE`，唤醒时重置两个检测器避免连续唤醒词二次触发
- **VAD 结束判定**：噪声基底自适应 + `--vad-threshold`，静音超过 `--silence` 秒或达到 `--duration` 自动结束
- **实时输出**：`partial` 用 `…` 覆盖行显示，`final` 用 `✓` 追加输出
- **录音落盘**：`recordings/rec_YYYYmmdd_HHMMSS.wav`，16kHz/16bit/单声道
- **设备回退**：自动挑选原生 16kHz 麦克风；指定设备打不开时自动换下一个

## 实测结果

测试环境：HP ZBook Power 15.6 (Linux)，麦克风 `sof-hda-dsp hw:0,7` (DMIC16kHz)，
模型 `vosk-model-small-cn-0.22`。

| 测试 | 结果 |
| --- | --- |
| 扬声器播放唤醒词，麦克风实时拾音 | 成功唤醒 |
| 唤醒后实时转写 | `今天天气怎么样` |
| 保存的 WAV 重新识别 | `今天 天气 怎么样` |
| 4 段无关中文语音 | 0 次误触发 |
| 6s 静音/噪声 | 0 次误触发 |
| 唤醒词换成 `芝麻开门` | 正常唤醒 |

## 已知限制

- 小模型精度有限，长句会有识别错误；需要更高精度可换 `vosk-model-cn-0.22`（1.3GB）
- 语法分词依赖模型词表，`你好助手` 这类组合能切分，但生僻词可能切不开
- 唤醒需要**清晰说出**唤醒词，环境噪声大时召回会下降
- 同一个唤醒词在录音开始后会被当作正文识别，脚本已自动去掉文本开头的唤醒词

## 相关文件

- `wakeup_asr.py` — 主脚本
- `main.py` — 原有录音脚本
- `mp4_to_wav.py` / `resample_wav.py` — 音频格式转换
- `vosk-api/` — vosk 源码（本项目用 pip wheel，未编译）
