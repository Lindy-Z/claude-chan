# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Claude娘 · B站冲浪：基于 Claude API 的 B站 机器人（Python 3.10+，纯命令行，没有构建步骤）。每一轮按关键词找视频、读字幕，给主人写阅读笔记（`reports/YYYY-MM-DD.md`）；遇到好视频就用「Claude娘」人设写评论草稿，默认要人工审批才会发出去。有人回复她的评论时，先让审核模型（默认 Sonnet）判断有没有钓鱼，再决定接不接话，同一个人最多回 2 条。项目最核心的设计目标是**防提示词注入**，改代码前先读下面的「安全不变量」。

代码注释、docstring、提示词、终端输出和文档全部用简体中文，新代码也保持中文。

## 常用命令

```
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# 把 .env.example 复制为 .env，填上 ANTHROPIC_API_KEY
# 想改设置就把 config.example.toml 复制为 config.toml（不进 git；没有它时直接用示例）

python login.py                 # 用 B站 小号扫码登录，生成 cred.json
python surf.py --check          # 检查 API key、模型名、登录态、花费和各个开关
python surf.py                  # 跑一轮
python surf.py --review         # 逐条审批待发评论和回复：y 发送 / n 丢弃 / e 改写后发送 / s 先留着 / q 退出
python surf.py --loop 3         # 大约每 3 小时一轮（±15% 随机），每轮都会重新读取 config.toml 和 persona.md

python redteam.py               # 离线注入测试，只依赖 guard.py（纯标准库），不用装依赖、不用 key、不联网；有失败项时退出码为 1
python redteam.py --live        # 用真模型跑恶意视频和钓鱼回复，走完整链路；约 $0.1（审核用 Sonnet），没有发评论的代码路径
python redteam.py --live replies  # 只跑钓鱼回复那一半
```

依赖装在项目里的 `.venv`（Python 3.13）中，全局 Python 没装这些依赖。没激活虚拟环境时，用 `.venv\Scripts\python.exe` 代替上面的 `python`。PyPI 上的 `bilibili-api`（少了 `-python`）是另一个早已停更的旧包，依赖锁在老版本上，在 Python 3.13 上需要编译器才能装，不要装错。本项目用的是 `bilibili-api-python`，两者的 import 名都是 `bilibili_api`。

项目没有配置 pytest、linter 或类型检查，`redteam.py` 就是测试套件。想单独测一条输入，直接调用 guard 里的函数：

```
python -X utf8 -c "from guard import check_comment; print(check_comment('要测的评论', max_chars=120))"
python -X utf8 -c "from guard import scan_injection; print(scan_injection('要测的字幕'))"
python -X utf8 -c "from guard import scan_injection; print(scan_injection('别人回复她的话', reply=True))"
```

入口脚本会自己把 stdout 切到 UTF-8。用 `python -c` 跑临时片段时，Windows 上要加 `-X utf8`，不然打印中文可能报编码错误。

## 架构

依赖方向：`surf.py` → `brain.py` / `bili.py` → `guard.py`。

- `guard.py`：所有确定性规则，不调用模型，只用标准库。要保持这一点，离线测试才能在不装依赖的情况下运行。主要函数：输入侧扫描 `scan_injection`、摘要校验 `validate_reading`、审核结果校验 `validate_vet`、评论硬闸门 `check_comment`、隔离标签 `fence`、日报转义 `md_safe`。`scan_injection(..., reply=True)` 用来扫别人回复她的话：不查 `_AI_ADDRESS` 那一组（"你是 AI 吗"、叫 Claude娘 等名字），因为对一个公开的 AI 账号这样说很正常；扫视频内容时两组都查。
- `brain.py`：所有 Claude 调用都经过 `Brain._call`，一共七种：`read`（fast 模型，唯一接触原始字幕和热评的调用）、`score`、`opening`（也用 fast 模型）、`write`（write 模型）、`judge`（judge 模型）、`vet`（vet 模型，唯一接触别人回复原话的调用）、`reply`（write 模型）。`Ledger` 用 usage 乘以配置里 `[prices]` 的单价来估算花费，超出预算就抛 `BudgetExhausted`；key 无效、模型不存在或额度用完会抛 `FatalAPIError`。这两个异常都会让本轮提前收工。`Brain(client=...)` 可以注入一个假的 client 做测试。
- `bili.py`：B站 侧的全部 I/O。用的是 bilibili-api-python，外加一个 httpx 客户端，只用来下载字幕 JSON（限 https，限 B站 域名，最大 3MB）。`hot_comments` 用 `comment.get_comments_lazy`（新版评论区接口，按热度排）读第一页，只取一级评论的文字，不取用户名和置顶。`replies_to_me` 用 `session.get_replies`（`/x/msgfeed/reply`）读「回复我的」第一页，只留 `business_id == 1`（视频）的回复，只取 aid、root、target、rpid、对方 mid 和文字，去掉开头的「回复 @某某 :」；这个接口返回的 `target_reply_content` 是空的，所以她自己说过的话要从 `state["posted"]` 里找。`post_reply` 用 `send_comment(root=, parent=)` 发楼中楼。每次读请求前会随机等 1.5～4 秒（`_pause`）。
- `surf.py`：命令行入口、配置合并、`State`（state.json）、一轮的完整流程、日报、审批、`--check`。它对 bili、brain、guard 的 import 都写在函数内部，所以 `redteam.py --live` 能 `from surf import load_config`，不会连带引入 B站 依赖。
- `login.py`：扫码登录，复用 `bili.save_credential`。bilibili-api 17.4 的网页扫码登录只从轮询结果的 `url` 参数里解析 SESSDATA、bili_jct、DedeUserID，但 B站 现在只通过 `Set-Cookie` 下发这些 cookie，所以 login.py 监听库的 `request_log` `RESPONSE` 事件来补全。三个 cookie 不齐时不会写 cred.json。升级 bilibili-api 后要确认这个补丁还需不需要。
- `persona.md`：人设。加载时去掉 `<!-- -->` 注释，`{master}` 替换成 `master.name`。人设会进入写评论的提示词，同时也是泄露检测的比对语料。

一轮（`surf.run_once`）的数据流：

0. `reply_phase`（`reply.mode` 不是 off 时，排在最前面）：`bili.replies_to_me` → 只留 `target` 是 `state["posted"]` 里某条 rpid 的回复（也就是在回她自己发的评论或回复），按时间从新到旧，同一个人只接最新一条，超过 `max_age_hours` 的直接记为看过。然后逐条：每人上限（`state.replies_to(mid)`，待审的也算）→ `scan_injection(reply=True)` → `brain.vet`（看对方原话，输出类别、safe 和第三人称转述的 `gist`）→ `validate_vet`（safe 为 true、类别是「正常互动」、gist 干净才算 ok）→ `brain.reply`（只看她的评论、那条评论记下的 summary/crowd、她之前的回复和 gist）→ `check_comment`（`crowd=对方原话`，拦"跟我念"式复读）→ `brain.judge(reply=True)`。review 模式进 `state["pending"]`（`kind="reply"`），auto 模式直接 `post_reply(aid, root, parent=对方那条回复)`。每条有结果的回复都记进 `state["reply_seen"]`，以后不再处理；因为预算、上限或出错没处理到的不记，下一轮再看。回复环节里预算和 API 致命错误照常让整轮收工，别的异常只跳过回复环节。
1. `discover`：每个关键词的搜索结果都看整页（大约 40 条，排在前面的常是超长合集或老视频），和热门视频一起用 `zip_longest` 轮流取；然后按「近期读过、时长、发布时间、每个来源的上限」过滤，挑满 `max_videos_per_run` 为止。
2. 逐个视频：`bili.detail`（视频信息 + 字幕）→ 没字幕且 `brain.hot_comments` > 0 时，用 `bili.hot_comments` 读点赞最多的几条一级评论，存进 `v["comments"]`，读不到就是空列表 → `scan_injection`（扫标题、简介、字幕、热评）→ `brain.read` → `validate_reading`（摘要、要点或 `crowd` 里出现网址、联系方式、对 AI 喊话之类的内容，就标记 `tainted`）→ `brain.score`。有热评时，`read` 会多给一个 `crowd`（评论区在玩的梗）；没给热评时，`read()` 会把它清空。综合分 = `round((quality + relevance) / 2)`，打分失败时 relevance 用 quality 代替。
3. `comment_phase`：先由 `commentable()` 按确定性规则筛选。以下情况都不评论：没字幕（除非从热评里读出了 `crowd`）、有注入迹象、tainted、命中敏感 flags、分数或播放量不够、以前评论过。一个都不满足时，把各原因的数量写进日报提示并打印到终端，然后直接返回。通过筛选的视频，先排 `comment.prefer` 里的关键词搜到的，再按分数排；然后依次 `brain.write` → `check_comment`（传入 `crowd=热评原文`，整句照搬的会被拦下）→ `brain.judge`。review 模式下草稿进 `state["pending"]`，auto 模式下直接 `post_comment`。
4. `finally` 里一定会写日报、执行 `state.prune()` 和 `state.save()`，预算耗尽或 API 出错时也一样。

## 安全不变量（改代码前先读）

README 里那张防线表，靠的就是下面这些代码约束，不要削弱：

- **模型没有任何权限**：`_call` 每次都是单轮请求，不带 `tools`。不要加工具、多轮对话，也不要把凭据或 API key 放进提示词。
- **信息流隔离**：只有 `read()` 能看到原始的标题、简介、字幕和热评。`write()` 只拿到 `Brain._notes()`（校验过的 summary、key_points 和 crowd）加人设，看不到字幕和评论原文，也看不到 `master.interests`。`interests` 只进 `score()`，打分结果只进私人日报。`judge()` 只看评论本身。别人回复她的原话只给 `vet()` 看；`reply()` 只拿到 `vet()` 转述、经过 `validate_vet` 检查的 gist，看不到原话。对方的昵称从头到尾不进任何提示词。
- **不可信文本必须先用 `guard.fence()` 包起来**再放进提示词。它生成带随机后缀的标签，并把内容里的 `<>` 换成 `‹›`，所以无法伪造闭合标签。
- **B站 侧能力最小**：只有搜索、读视频信息、读字幕、读热评、读「回复我的」、发一级评论、回复别人对她评论的回复这 7 个动作，不要加私信、点赞、关注、删除，也不要加主动去别人评论下搭话的功能。读热评（2026-10 应用户要求加的，只读）只在视频没字幕时用，只取评论文字。回复（2026-10 应用户要求加的）只回 `target` 在 `state["posted"]` 里的回复，也就是在回她自己发的东西；同一个人最多 `HARD_MAX_PER_PERSON=2` 条，计数存在 `state["replied"]`，永远不清，`--review` 发送前还会再查一次。发评论用的 `aid`、回复用的 `aid`/`root`/`parent` 永远来自 B站 接口，不来自模型输出。项目明确不做验证码破解、指纹伪装、代理池、多开，被风控就停下；也不要缩短请求间隔。
- **拿不准就不发（fail closed）**：`_call` 遇到 `refusal`、`max_tokens` 或 JSON 解析失败都返回 `None`，调用方把 `None` 一律当成「不评论」「不回」。`validate_vet` 只在 safe 为 true、类别是「正常互动」、gist 非空且干净时给 ok。另外，`write()` 和 `reply()` 的输出只要包含 "skip"（不分大小写）就算 SKIP。
- **硬上限**写在 `surf.py` 里：`HARD_MAX_PER_RUN=5`、`HARD_MAX_PER_DAY=20`、`HARD_MIN_DELAY=30`、`HARD_MAX_PER_PERSON=2`，每轮最多审核 `REPLY_VET_PER_RUN=10` 条回复（防刷屏烧钱）。`load_config` 会把 config 里的值限制在这个范围内。评论和回复的每轮、每天上限各算各的，但两者加起来每天不超过 `HARD_MAX_PER_DAY`。
- **熔断**：有 `STOP` 文件时，本轮评论和回复都变成 off；auto 模式每次发送前会再查一次，`--review` 开始时也会查。同一轮有 3 条草稿被拦就停止写评论（回复另算 3 条）。auto 模式下，同一轮有 2 条以上草稿被拦，或者发送报错（`SKIP_VIDEO_CODES` 以外的错误码），会调用 `pause_auto(24)`，之后 24 小时评论和回复都退回 review 模式。
- **日报**：任何来自 B站 或模型的文字，写进 Markdown 之前都要经过 `md_safe()`。有注入迹象或被标记 tainted 的视频，永远不会出现在「值得看」里，防止分数被刷。
- **泄露检测**：每次创建 `Brain` 都会生成一个随机校验码（canary），写进写评论和写回复模型的系统提示。`check_comment` 会检查正文里有没有这个校验码，以及有没有和 `leak_corpus`（人设 + WRITER_RULES + REPLY_RULES）连续 16 个字相同的片段。同一套 16 字检查加上相似度 ≥ 0.5，也用来拦整句照搬热评或对方原话（`crowd` 参数）。

修改 `guard.py` 时：把新的攻击样本加进 `redteam.py` 对应的列表（`INPUT_ATTACKS`、`COMMENT_ATTACKS`、`READER_ATTACKS`、`MD_ATTACKS`、`CROWD_COPIES`、`REPLY_ATTACKS`、`VET_ATTACKS`、`REPLY_COPIES`），正常样本加进 `*_BENIGN`，然后跑 `python redteam.py`，必须全部通过。`REPLY_BENIGN` 是硬性测试：问她是不是 AI、叫她名字在回复里不能被当成喊话，前 3 条出现在视频里又必须被当成喊话。`COMMENT_ATTACKS` 末尾那批是以前发现过的绕过方式，作为回归测试保留。检测前会先做两种归一化：`_nfkc`（NFKC + 转小写，用于英文和网址）和 `_squash`（在 `_nfkc` 基础上再去掉空白和各种分隔符，用于中文模式和联系方式关键词）。归一化只用来判断，不会改动真正发出去的正文。

## Claude API 细节

- 结构化输出写法是 `output_config={"format": {"type": "json_schema", "schema": ...}}`。分数用 `enum: list(range(11))` 限定在 0 到 10（代码注释说结构化输出不支持 minimum/maximum），每个 schema 都带 `additionalProperties: False`。拿到结果后，`validate_reading`、`validate_vet`、`score`、`judge` 还会再校验一遍类型和范围。
- 模型：`fast_model` 用于 read、score、opening，`write_model` 用于 write 和 reply，`judge_model` 用于 judge，`vet_model` 用于 vet。`surf.py` 的 `DEFAULTS` 和 `config.example.toml` 里，fast 是 `claude-haiku-5-5`，另外三个是 `claude-sonnet-5-5`；想省钱可以在自己的 `config.toml` 里把 fast、write、judge 设成 `claude-haiku-5-5`，但 vet 是用户点名要用 Sonnet 5.5 的，别为了省钱换掉。改模型名时要同步改配置里的 `[prices]`，否则 `Ledger` 会按最贵的单价估算。
- 思考：这两个模型在请求不带 `thinking` 参数时默认会开启自适应思考，而思考用掉的 token 也算在 `max_tokens` 里。一旦被思考占满，`stop_reason` 就是 `max_tokens`，`_call` 返回 `None`，结果是视频显示「没读成」、草稿被当成 SKIP，或者终审否决。所以 `_call` 默认按 `THINKING_OFF` 表关掉思考（Haiku 5.5 用 `{"type": "disabled"}`，Sonnet 5.5 只接受 `{"type": "between_tools"}`），`max_tokens`（300 到 1000）只按答案长度给。例外有三个，都传 `think=True` 保留思考：`write()` 和 `reply()`（实测关掉思考后 Haiku 每次都回 SKIP；`max_tokens` 放宽到 4000，实测思考加答案约 450～930 token），以及 `vet()`（专门对付冲着 AI 来的试探，用思考换更稳的判断；思考加 JSON schema 一起用，`max_tokens` 3000）。表里没有的模型不传 `thinking`，换模型时要先补表。注意 `_call` 只在有 schema 时才设置 `output_config`，如果要加 `effort`，得和 `format` 合并到同一个 dict 里。

## 配置与运行时文件

- 配置文件由 `config_path()` 决定：有 `config.toml`（个人设置，在 `.gitignore` 里，不进 git）就用它，没有就用仓库里的 `config.example.toml`。读到的内容会合并到 `surf.py` 的 `DEFAULTS` 上，而且只合并 `DEFAULTS` 里已有的 section。新增配置项时，`DEFAULTS` 和 `config.example.toml` 都要加；本地的 `config.toml` 不会自动更新，得自己补上。
- 三个和评论、回复相关的配置项，`DEFAULTS` 里都是保守值：
  - `brain.hot_comments`：没字幕时读几条热评。`DEFAULTS` 里是 0（不读），`config.example.toml` 里是 15。
  - `comment.prefer`：优先评论哪些关键词搜到的视频。`DEFAULTS` 和 `config.example.toml` 里都为空，只按分数排。
  - `[reply]` 一节（`mode`、`per_person`、`max_per_run`、`max_per_day`、`max_age_hours`、`max_chars`）：`DEFAULTS` 里 `mode` 是 off，这样没有这一节的老 `config.toml` 升级后不会突然开始回复；`config.example.toml` 里是 review。审核模型是 `brain.vet_model`。
- `state.json`（`State` 类）包含 `seen`、`posted`、`pending`、`spend`、`auto_paused_until`、`auto_pause_reason`、`replied`（对方 mid → 回过几条，永远不清）和 `reply_seen`（处理过的回复 rpid → 日期，留 14 天）。`posted` 和 `pending` 里的记录用 `kind` 区分 `comment` 和 `reply`（早先的记录没有 `kind`，都按评论算）；评论记录还存 `aid`、`summary`、`crowd`，有人回复时写回复的模型靠它们了解视频；回复记录存 `root`、`parent`、`to`、`gist`。发出去的东西都通过 `State.add_posted` 记账。写入时先写 `.tmp` 再用 `os.replace` 原子替换；如果 JSON 损坏，会改名为 `state.broken.json`，然后重新开始记录。每轮都会执行 `prune()`，待审超过 3 天的草稿会被丢弃。
- `logs/audit.jsonl` 记录每条草稿的去向，`stage` 的取值有 `writer_skip`、`gate_reject`、`judge_reject`、`queued`、`posted`、`post_error`、`review_discard`；回复环节的记录带 `kind="reply"`，`parent` 是对方那条回复的 rpid，另外还有 `reply_cap`（这个人已经回满了）、`reply_scan`（对方原话里有喊话）、`reply_vet_reject`（审核没过）。查「为什么没评论」「为什么没回」先看这里。
- 以下文件都已经在 `.gitignore` 里：`.env`、`cred.json`（B站 登录态，等于账号钥匙）、`config.toml`（个人口味）、`state.json`、`reports/`、`logs/`、`STOP`。仓库是公开的，提交前要确认这些文件没被加进去，作者邮箱也要用 GitHub 的 noreply 地址。

## B站 侧常见问题（摘自 README）

- 搜索全部失败，或者报 -412：被风控了，过几个小时再跑，不要调低等待时间。未登录的请求（比如临时测试脚本里用 `Credential()`）读视频详情时很容易触发 412。
- 发评论报 12015：B站 要求输入验证码，程序会自动停下。12002 和 12051（评论区已关闭、重复评论）只会跳过当前视频。
- 很多视频显示「没有字幕」：B站 的 AI 字幕需要登录态才能拿到，先用 `--check` 确认登录正常。
- 报模型名不存在：把 config 里的模型名改成 `--check` 列出来的可用名字。
