# 训练与实验代码

`research/` 集中保存训练、策略实验、样本验证和可复用回归代码。当前培育实验在 `produce/`，尚未实现策略模型训练。

## 职责边界

| 目录 | 内容 |
| --- | --- |
| `agent/` | 客户端动作、采集监听、后台识别及运行日志所需代码 |
| `tools/` | 项目构建、CI、翻译、正式数据与资源同步等维护代码 |
| `research/produce/` | 独立采样、离线报告、证据重放、模板取样和合成数据回归 |
| `debug/` | 本地截图、日志、数据集、实验输出和一次性排查脚本，Git 忽略 |

实验代码可以调用 `agent` 的读取器和数据结构，客户端不能反向依赖 `research`。`agent/produce/analysis.py` 同时用于运行时后台识别，继续归客户端管理；技能卡和奖励库同步脚本生成正式资源，继续放在 `tools/`。`research/` 不进入现有 Agent 发布包，无需修改打包配置。

## 培育实验入口

以下命令从仓库根目录执行，`python` 可换成 beta 的内置 Python 路径：

```powershell
python research/produce/report_produce_session.py <会话目录> --capture-root <采集目录> --output debug/research/produce/report
python research/produce/report_produce_session.py <会话根目录> --all --capture-root <采集目录> --output debug/research/produce/summary
python research/produce/report_decision_capture.py <单个采集目录>
python research/produce/replay_reward_effects.py <单个采集目录> --database assets/data/produce_skill_cards.json
python research/produce/build_buff_templates.py --capture-root <采集目录> --preview debug/research/produce/buff_preview.png
python research/produce/analyze_choices.py <已验收报告根目录> --output debug/research/produce/baseline
python research/produce/test_reporting.py
python research/produce/test_produce_decisions.py
python research/produce/test_external_decider.py
python research/produce/eval_battle_frames.py --fixture <GHL>/fixtures/maa-live/<run>.json --capture-root <采集仓库根目录> --output debug/research/produce/frame_reader
python research/produce/test_battle_frames.py
python tools/decider/ghl_readings.py --mailbox <采集仓库根目录>/debug/decider/mailbox --out debug/research/produce/ledger/<run>-readings.jsonl
python tools/decider/ghl_readings.py --mailbox debug/decider/mailbox --follow
python research/produce/test_ghl_readings.py
```

`eval_battle_frames.py` 用 GHL（gakumas-headless-lab）实况夹具中的人工读数逐字段核对战斗画面识别（课程/考试 HUD、Buff、手牌、饮料），输出按字段统计的 `report.json` 与逐帧不一致的 `mismatches.jsonl`；需要安装 maafw，人工读数与采集信箱均不入库。`test_battle_frames.py` 为替身 OCR 与合成图像的回归。

`tools/decider/ghl_readings.py` 从决策信箱逐条读出 GHL 牌组账本（`src/bridge/maaLedger.ts`）所需的画面读数：战斗帧、奖励的种类、候选名称与剩余次数、行动选择画面的周数与成长加成、特别指导与相谈画面的 P 点数，以及特别指导改造预览中的卡牌、菜单选项与选中项；只读画面，不推断牌组。`--follow` 为实况模式：监视 `requests/`，请求一写入就读画面并追加到 `ghl/readings.jsonl`，供 GHL 的实况桥（`scripts/maa/live.ts`）即时作答；不写 `responses/`。`test_ghl_readings.py` 为替身 OCR 的回归。

`capture_battle.py` 是显式启动的只读 ADB 旁路采样入口，参数见 `--help`；不会因生成报告或运行测试而启动。它使用可选开发依赖 OpenCV，模板取样使用 Pillow，均不增加客户端运行依赖。详细操作见 [出牌数据采集](../docs/zh_cn/出牌数据采集.md)。

回归测试使用合成事件和文件，不连接设备，临时素材写入 `debug/research/produce/tests/`，校验路径后由测试清理。`test_produce_decisions.py` 是客户端培育决策（出牌、事件优先级、偶像卡识别、试镜门槛）的特征测试，固定当前行为而不评价优劣，需要安装 maafw，CI 在资源检查任务中运行。已有轮次及历史派生结果保持原位。迁移前 `tools/` 中的六个实验入口已移到本目录，统一使用新路径，不保留转发文件。

`analyze_choices.py` 读取已经验收的 `*/report.json`，输出 `baseline.json` / `baseline.md`。仅纳入正常结束且 `complete=true` 的轮次，重复会话去重、跨会话重复分段排除；按剧本、难度、配置偶像、歌曲与完整代码指纹分组。事件按候选出现的决策次数、奖励按每页唯一库条目统计选择频率，预览重试不增加分母。身份未知、选择未核对和未采集字段分别保留，不把选择输入当作实际获得。

描述性对照可按已记录的相同任务配置和策略源码指纹寻找样本，完整采集版本仍单列。该对照只分解端点评分与属性差异，不把随机候选、战斗过程或未知状态的影响归因于某次选择，也不生成策略训练标签或修改客户端策略。输入报告路径、文件 SHA 与原报告来源保存在输出中；实际轮次的分析结论只写入 `debug/`。
