# Holy-Sword-Alliance

用来管理圣剑联盟脚本的版本，便于控制版本找出不同版本的优缺点以进行优化。

一套在 Windows + MuMu 模拟器上运行的 Android 游戏挂机引擎：视觉识别页面状态、自动完成排位/荣耀时刻/全民争霸的对局循环，并提供一个本地 Web 控制台做启动、预约与状态查看。

---

## 运行环境

| 项 | 值 |
|---|---|
| 模拟器 | MuMu 12/15（实例 `MuMuPlayer-15.0-0`），adb `127.0.0.1:16384` |
| 分辨率 | **1600x900 @ 240dpi** —— 代码里的点击坐标是**全图绝对坐标**，换分辨率必须重新标定 |
| 目标包名 | `com.holyblade.sjlm.ganxt` |
| Python | 虚拟环境 `~/.workbuddy/binaries/python/envs/sj_bot` |
| 控制台 | `http://127.0.0.1:5050` |

启动控制台：双击 `start_console.bat`。

> ⚠️ 控制台必须由**计划任务实例前台持有**。挂在交互窗口、调试会话或 `nohup &` 下的控制台会随宿主进程一起死，已登记的预约就会静默不触发。见 `scripts/ensure_console.py`。

---

## 目录结构

```
sj_bot/          引擎核心
  battler.py       主循环：页面分类、战斗判定、结算链、自愈、防挂机
  server.py        Flask 控制台 + JobScheduler（到点自动下发任务）
  reward_icons.py  开箱奖励图标识别（颜色为主判据，模板兜底）
  state_machine.py 时段闸门（荣耀 18-20 / 争霸 周六日 16-17）
  vision.py        模板匹配 / OCR / 颜色掩码
  rank_layout.py   坐标与关键词常量
  llm_client.py    防挂机交叉作答的模型调用
  collector.py     挑战记录采集
  db.py            对战记录库
scripts/         60+ 个脚本，分五类
  *_selftest.py    离线自测（纯逻辑，不需要设备）
  verify_*.py      真实帧回归验证（改判据/阈值后必跑）
  diag_*.py        诊断探针
  live_*.py        真机端到端验证
  ensure_console.py / install_console_task.py   运维：看门狗
  daily_prep.py / install_prep_task.py          运维：每日冷启动链（起模拟器+登录+预约）
  cleanup_captures.py / install_cleanup_task.py 运维：每日截图回收（三层闸门，含硬上限）
  label_reward_icon.py                          运维：奖励图标补命名 + 历史回填
web/index.html   控制台前端（单文件，Tailwind + 原生 JS）
assets/          模板图 + 回归参考帧（verify_*/diag_* 依赖，勿删）
  reward_icons/    奖励图标模板（.png=模板，ref/=回归样本）
  reward_frames/   奖励**整帧**回归样本（压缩比与回图管道，勿删）
  modal_ref/       模态遮挡回归样本（转盘/背包/干净主城）
data/lists/      对战名单（can_beat / cannot_beat）
reports/         性能与事故分析报告
```

---

## 核心设计约束

这些是踩过坑之后定下的规矩，改动前请先读对应源码注释：

1. **OCR 是稀缺资源。** 本机全图 OCR 需要 12~25 秒，所以设计原则是"少做 OCR"：能用模板门控（`_hint_or_tpl(hi, lo)` 三值判定）就不要回退到全图 OCR。
2. **扁条 ROI 高度必须 ≥192px。** 低于此高度 OCR 恒返回 0 块且**不报错**——静默死代码。新 ROI 上线前必跑 `scripts/diag_roi_audit.py`。
3. **中文路径 + OpenCV 会静默失效。** `cv2.imwrite` 返回 `False` 而不抛异常，`cv2.imread` 传含中文的绝对路径直接返回 `None`。必须用 `cv2.imencode(...)[1].tofile()` 与 `cv2.imdecode(np.fromfile(...))`。
4. **中文路径的计划任务必须走 XML。** `schtasks` 的 `-tr/-sd` 参数走控制台代码页，中文会被按 ANSI 误解。
5. **页面分类优先级：session > 结算链 > 战斗页。** 半透明模态（如设置浮层）下底层文字仍可被 OCR 读出，因此守卫判据必须排在"全图回退"之前。**全屏模态（「背包/宝石合成」面板、「幸运大转盘」活动页）同理**：它们中央不透明、但四周围仍露出主城建筑文字，`_is_in_main_city` 会判成立而入口「王者之巅」正好被压住 ⇒ 导航必败。识别不了就退而求其次：**"判成主城却两帧都找不到锚点"本身就是模态遮挡的稳定特征**，此时按**一次**返回键即可退回主城（`battler._nav_modal_back`，绝不连按——主城连按返回键会把游戏按退出到桌面）。
6. **回归样本放 `assets/*_ref/`，不要放 `captures/`。** `captures/` 有例行清理，样本丢了测试会莫名其妙地崩。

---

## 定时与无人值守

三个计划任务，各自装一次即永久有效（**都不带 `<EndBoundary>`**）：

| 任务 | 频率 | 作用 |
|---|---|---|
| `SJBot_Console_Guard` | 每 10 分钟 | 确认控制台存活，掉了就拉起并由任务实例前台持有 |
| `SJBot_Daily_Prep` | 每天 15:40 / 17:40 | 冷启动链：起 MuMu → 等 adb → 拉起游戏 → 登录 → 登记当日预约 |
| `SJBot_Daily_Cleanup` | 每天 03:30 | 回收过期截图 + 硬上限兜底（见下） |

### 体积维持 = 三层闸门

`captures/` 是唯一会持续变大的目录。2026-09-29 实测：一天写了 **57 张 / 86.8 MB** 奖励整帧（1600×900 PNG，1.52 MB/张）——按此速率"留一年"就是 **30.9 GB**。单靠一个参数治不住，所以分三层，每层挡不同的东西：

| 层 | 手段 | 挡什么 | 落在哪 |
|---|---|---|---|
| 1. 源头减量 | 奖励图存 **JPEG q88**（实测省 88%，1.51 MB → 0.19 MB） | "单张太大" | `battler.REWARD_IMG_QUALITY` |
| 2. 按龄回收 | 异常图 30 天 / 奖励图 90 天 / `outputs/` 30 天 | "越攒越多" | `cleanup_captures.py --days/--rewards-days` |
| 3. **硬上限** | `captures/` 超 2000 MB 就从最旧删 | "**速率突变**" | `cleanup_captures.py --max-mb` |

第 3 层不是重复劳动：**保留期按时间设闸，挡不住速率变化**——落盘格式、截图频率或分辨率一改，同一个"留 365 天"就会从 1 GB 变成 30 GB。上限才是几何意义上的"不会爆"。它删除时**不碰 6 小时内的新文件**（删掉"刚刚那次异常"的现场，往往就是删掉了最想看的证据）。经验法则：`--max-mb` ≥ 稳态 × 1.5，调大保留期时要把上限一起调大，否则保留期会被上限架空。

稳态估算 ≈ 奖励图 0.92 GB + 异常图 0.33 GB ≈ **1.25 GB**，上限 2000 MB 留约 1.6 倍余量。

奖励图**不是**永久：用户口径是"图片每个月清一次，我只需要看开箱的奖励**是什么**就行"——"是什么"已以文字落在 `data/rewards.jsonl`（**永不删**、几乎不占空间），图只是佐证。换 JPEG 是有损，但降的是"像素保真"不是"信息量"：q88 下图标轮廓、`×N` 数量角标、弹窗标题都清晰可读，压完**再解回来重新识别**仍认得出物品名（`reward_storage_selftest` E 段实证）。

```bash
<venv python> scripts/install_console_task.py     # 看门狗
<venv python> scripts/install_prep_task.py        # 冷启动链
<venv python> scripts/install_cleanup_task.py     # 每日回收（会自动卸下旧的月度版）
<venv python> scripts/cleanup_captures.py --dry                     # 先看清单再动手
<venv python> scripts/cleanup_captures.py --max-mb 2000 --dry       # 单独看硬上限会删什么
```

装完务必 `--check` 确认 **NextRun 非空**。历史事故：旧版触发器带 `<EndBoundary>`，只在安装当晚有效，次日 `NextRun` 为空、任务静默失效，表现为"到点了什么都没发生"。

---

## 开箱奖励

「宝箱奖励」弹窗**只画图标不写物品名**，所以由 `sj_bot/reward_icons.py` 识别后把物品名写进 `data/rewards.jsonl`，控制台「开箱奖励」卡片按名字展示并做今日聚合。

判据是**颜色为主、模板兜底**（不是纯模板匹配）：

- 纯灰度模板匹配不够稳 —— 屏蔽角标后「紫卷轴 vs 紫卡片」仍拿 0.871，而「金卷轴 vs 紫卷轴」拿 0.858，同类自身才 1.000，余量只有 0.13。根因是这几种卡片共用同一套底版（金边框 + 紫内底），只有图案本体不同。
- 改按用户给的颜色口径后，「紫卷轴 vs 紫卡片」的**银灰占比差 160 倍**（0.2% vs 33.4%，后者是三张银灰卡牌）。
- 右下角「×N」数量角标会变（实测 ×2、×4），匹配与取色前**必须屏蔽**，否则同一物品换个数量自匹配掉到 0.753。

认不出的记「待命名」并落 `captures/rewards/_unknown/`，**绝不猜**。补命名流程：

```bash
<venv python> scripts/label_reward_icon.py --list      # 看待命名队列
<venv python> scripts/label_reward_icon.py --promote <图标.png> <模板名>
<venv python> scripts/label_reward_icon.py --backfill  # 回填全部历史记录
```

标定与诊断：`scripts/diag_reward_icons.py [--dump --emit --hue]`。

---

## 测试

项目目前**没有统一的测试入口**，需要按类别手动跑：

```bash
# 离线逻辑自测
<venv python> scripts/classify_page_selftest.py
<venv python> scripts/job_reason_selftest.py
<venv python> scripts/schedule_selftest.py
<venv python> scripts/reward_selftest.py       # 开箱奖励: 识别 + 落账改名
<venv python> scripts/nav_modal_selftest.py # 导航: 主城被全屏模态盖住 -> 按一次返回键清障
                                            # (含真实帧前置条件; --no-real 只跑桩化段)
<venv python> scripts/anomaly_dedup_selftest.py # 异常现场图同画面去重（判据阈值 + 批量 + 运行时闸门）
<venv python> scripts/reward_storage_selftest.py # 奖励图存储: JPEG 落盘 / 历史 PNG 改名兼容 /
                                                 # 回图端点白名单 / 硬上限（52 项）

# 真实帧回归（改了判据/阈值后必跑）
<venv python> scripts/verify_classify_real.py
<venv python> scripts/verify_menu_overlay.py
```

改动落码（终止原因）时必须同步更新 `server.JOB_REASON`，否则 `job_reason_selftest` 会失败。
