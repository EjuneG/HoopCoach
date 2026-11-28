# AGENTS.md — HoopCoach 本地只读协作指南

本目录是《篮球少女：飞跃巅峰》(*Hoop Girls*) 玩家本地的 **HoopCoach** 运行目录（或公开源码仓）。你可以帮玩家分析本地战绩、笔记/器材库存并生成专属养成计划。

## 安全红线

- **严格只读**：禁止任何内存读取、DLL Hook、修改游戏文件或模拟键鼠点击脚本；只读取本目录生成的本地 JSON/Markdown 文件与游戏明文日志。
- **不凭印象心算笔记相性**：38 名角色的相性矩阵超过 1400 组，请用 `python3 src/note_advisor.py --char <角色>` 或 `--note <分数/角色>` 实算，只引用脚本验算结果。

## 本地只读数据位置

- `data/runs/detailed_runs.json`：历史完赛战绩总表（主力、总评、六维、均回合 EV 损耗、器材满级回合等）。
- `data/runs/archive/<run_id>.json`：单局逐回合详细复盘。
- `data/note_pool.json`：已同步的已有角色（`owned_actors`）、自备笔记（`own_notes`）与好友笔记（`borrowed_notes`）。
- `data/equip_pool.json`：已同步的自有 SSR 器材与练度（`owned_cards`）。
- `data/hoopgirls/`：全图鉴静态数据（角色 `characters.json`、器材 `equipments.json`、相性矩阵 `inheritance_rules.json`）。
- `docs/verified_mechanics.md`：已验证的育成机制真值库（如有）。

## 把专属养成计划自动展示到作战室

只要在 `docs/` 下新增任意 `*_plan.md`（例如 `docs/my_training_plan.md`），并在文件顶部加上如下 HTML 注释，作战室的 **「计划与攻略」** 页签就会自动收录渲染，并生成可点击直达配装器的角色头像按钮：

```markdown
<!--
plan_tag: 个人养成计划
plan_status: 进行中
plan_chars: 1002, 1011, 1030
plan_summary: 一句话总结当前冲分或刷彩笔记优先级
-->

# 我的阶段养成计划
...
```

- `plan_chars` 填角色数字 ID（可在 `data/hoopgirls/characters.json` 查到，逗号分隔）。
- 写完后提醒玩家在作战室右上角点一下「刷新」即可看到新计划。
