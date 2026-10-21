# ProtoBot 文档索引

库是什么、目前支持什么，先看[主 README](../README_zh.md)；这里的文档各往下钻一层。英文索引：[README.md](README.md)。

| 文档 | 它回答什么问题 |
| --- | --- |
| [architecture.md](architecture.md) | bot 如何按关注点拆开（连接 / 移动 / 包路由 / 寻路）、新处理器和新功能该放哪、一次 tick 必须守住哪些不变量。 |
| [protocol-verification.md](protocol-verification.md) | 每个版本的每种负载是怎么对着真服 jar 核对的，版本升级后如何重新核验。 |
| [automation.md](automation.md) | Goal 与 Task 两层的契约：`is_reached()` 必须是什么意思、重试与超时如何报告、如何新增一个 goal 或一个 task step。 |
| [containers.md](containers.md) | 菜单怎么工作：协议包、布局表、点击语义、为什么光标要在本地预测、机器动词，以及使用过程中窗口失效的三种原因。 |
| [components.md](components.md) | 带数据组件的物品为什么在 clientbound 方向不能简单按长度解析、目前解了哪些组件、含糊的整槽列表为什么必须拒解。 |
| [tools.md](tools.md) | 硬度与工具等级从哪里来（它们不在任何包里）、原版破坏时间公式、按版本生成的数据表怎么来。 |
| [combat.md](combat.md) | 实体射线、真实客户端攻击/挖掘时的挥臂、以及如何用服务端自己的血量证明一次击杀。 |
| [anti-cheat.md](anti-cheat.md) | 让 bot 在 GrimAC 眼里像玩家的包形：一 tick 一个移动包、动作落在 tick 之间、冷却、指向真的能点到的地方。动手写动作前先读这篇。 |
| [acceptance-suite.md](acceptance-suite.md) | 33 项游戏内验收场景、如何在聊天框触发、每个场景如何搭景与清场、全绿长什么样。 |

## 这些文档反复强调的三条

1. **服务端是唯一真值。** 每一层报告的是服务端做了什么，而不是 bot 想做什么：收到方块更新才算挖掉，收到槽位更新才算点击生效。
2. **一 tick 一个移动包。** 朝向搭在它所属那一 tick 的移动包里；动作放在 tick 之间，绝不塞进 tick 里面。
3. **给原因，而不是盲目重试。** 失败要说清缺什么（少的物品、没打开的窗口、看不到的方块面），而不是再来一遍。

## 怎么自检

```powershell
.\tools\ci_local.ps1            # 字节编译 + import 卫生 + 单测
.\tools\ci_local.ps1 -Live      # 上面三项，再跑 33 项游戏内场景
```

游戏内套件需要一台 Paper 26.3 测试服（见[测试桥仓库](https://github.com/Yelastforbilibili/mc-prototest-bridge)），并用 `-ServerLog` 指向它的 `logs/latest.log`。
