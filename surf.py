"""
Claude娘 · B站冲浪
=================

  python surf.py            跑一轮：看看谁回复了她 → 找视频 → 读字幕 → 打分 → 写阅读笔记（和评论、回复草稿）
  python surf.py --review   逐条审批待发评论和回复，你按 y 才会真的发出去
  python surf.py --loop 3   每 3 小时左右跑一轮，Ctrl+C 退出
  python surf.py --check    检查登录、API key、花费和各个开关

想立刻停掉一切评论和回复：在这个文件夹里新建一个名叫 STOP 的空文件（不用重启）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import zip_longest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

HERE = Path(__file__).resolve().parent
STOP = HERE / "STOP"
REPORTS = HERE / "reports"
LOGS = HERE / "logs"
WORTH = 5  # 日报里"值得看"的分数线

# 代码里的硬上限：config.toml 写得再大也不会超过。每轮、每天的上限评论和回复各算各的，
# 但两者加起来每天也不超过 HARD_MAX_PER_DAY
HARD_MAX_PER_RUN, HARD_MAX_PER_DAY, HARD_MIN_DELAY = 5, 20, 30
HARD_MAX_PER_PERSON = 2  # 同一个人最多回 2 条，不能多回
REPLY_VET_PER_RUN = 10   # 每轮最多审核几条别人的回复，防止有人刷屏烧钱

DEFAULTS = {
    "master": {"name": "主人", "interests": ""},
    "discover": {"keywords": [], "per_keyword": 5, "order": "totalrank", "popular": 3,
                 "max_videos_per_run": 15, "max_age_days": 60, "min_minutes": 2, "max_minutes": 90,
                 "revisit_after_days": 30},
    "brain": {"fast_model": "claude-haiku-5-5", "write_model": "claude-sonnet-5-5",
              "judge_model": "claude-sonnet-5-5", "vet_model": "claude-sonnet-5-5", "max_subtitle_chars": 12000,
              "hot_comments": 0, "daily_budget_usd": 1.0, "monthly_budget_usd": 50.0},
    "comment": {"mode": "review", "min_score": 8, "min_views": 1000, "max_per_run": 2, "max_per_day": 5,
                "delay_seconds": [90, 240], "max_chars": 120, "signature": "（Claude娘·AI自动评论）",
                "prefer": []},
    # 回复别人对她评论的回复。默认关：老的 config.toml 里没有这一节，升级后不会突然开始回复
    "reply": {"mode": "off", "max_per_run": 3, "max_per_day": 10, "per_person": 2, "max_age_hours": 72,
              "max_chars": 80},
    "prices": {"claude-haiku-5-5": {"input": 0.10, "output": 0.50},
               "claude-sonnet-5-5": {"input": 2.0, "output": 10.0}},
}


def config_path() -> Path:
    """自己的 config.toml（不进 git）优先；还没有时用仓库里的示例。"""
    own = HERE / "config.toml"
    return own if own.exists() else HERE / "config.example.toml"


def load_config() -> dict:
    with open(config_path(), "rb") as f:
        user_cfg = tomllib.load(f)
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    for sec, vals in user_cfg.items():
        if sec in cfg and isinstance(vals, dict):
            cfg[sec].update(vals)
    c = cfg["comment"]
    if c["mode"] not in ("review", "auto", "off"):
        raise SystemExit("config.toml 里 comment.mode 只能是 review / auto / off")
    c["max_per_run"] = max(0, min(int(c["max_per_run"]), HARD_MAX_PER_RUN))
    c["max_per_day"] = max(0, min(int(c["max_per_day"]), HARD_MAX_PER_DAY))
    lo, hi = (list(c["delay_seconds"]) + [0, 0])[:2]
    c["delay_seconds"] = (max(HARD_MIN_DELAY, float(lo)), max(HARD_MIN_DELAY, float(lo), float(hi)))
    c["max_chars"] = max(20, min(int(c["max_chars"]), 300))
    c["signature"] = str(c.get("signature") or "")
    c["prefer"] = [str(k) for k in c["prefer"] or []]
    cfg["brain"]["hot_comments"] = max(0, min(int(cfg["brain"]["hot_comments"]), 20))  # 一页最多 20 条
    r = cfg["reply"]
    if r["mode"] not in ("review", "auto", "off"):
        raise SystemExit("config.toml 里 reply.mode 只能是 review / auto / off")
    r["max_per_run"] = max(0, min(int(r["max_per_run"]), HARD_MAX_PER_RUN))
    r["max_per_day"] = max(0, min(int(r["max_per_day"]), HARD_MAX_PER_DAY))
    r["per_person"] = max(0, min(int(r["per_person"]), HARD_MAX_PER_PERSON))
    r["max_age_hours"] = max(1, min(int(r["max_age_hours"]), 24 * 7))
    r["max_chars"] = max(20, min(int(r["max_chars"]), 300))
    return cfg


def load_persona(cfg: dict) -> str:
    text = (HERE / "persona.md").read_text("utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()  # 文件开头的说明注释不进提示词
    return text.replace("{master}", cfg["master"]["name"])


# ───────────────────────────── 状态与日志 ─────────────────────────────

class State:
    """state.json：读过哪些视频、发过哪些评论和回复、回过谁几条、待审队列、花费记录。"""

    def __init__(self, path: Path):
        self.path = path
        self.d = {"seen": {}, "posted": [], "pending": [], "spend": {}, "auto_paused_until": "",
                  "replied": {}, "reply_seen": {}}
        if path.exists():
            try:
                self.d.update(json.loads(path.read_text("utf-8")))
            except ValueError:
                path.replace(path.with_suffix(".broken.json"))
                print("state.json 损坏，已改名备份，重新开始记录。")

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    def seen_recently(self, bvid: str, days: int) -> bool:
        t = self.d["seen"].get(bvid)
        return bool(t) and date.fromisoformat(t) > date.today() - timedelta(days=days)

    def mark_seen(self, bvid: str) -> None:
        self.d["seen"][bvid] = date.today().isoformat()

    def commented(self, bvid: str) -> bool:
        return any(p.get("bvid") == bvid for p in self.d["posted"] + self.d["pending"])

    def posted_today(self, kind: str = "") -> int:
        """今天发了几条；kind 是 "comment" 或 "reply" 时只数这一种（早先的记录没有 kind，都是评论）。"""
        t = date.today().isoformat()
        return sum(1 for p in self.d["posted"] if str(p.get("t", "")).startswith(t)
                   and (not kind or p.get("kind", "comment") == kind))

    def posted_texts(self, n: int = 60) -> list[str]:
        return [p.get("text", "") for p in self.d["posted"][-n:]]

    def add_posted(self, rec: dict) -> None:
        """记下发出去的评论或回复。回复还要给对方记一笔：这个计数永远不清，同一个人最多回 per_person 条。"""
        self.d["posted"].append({"t": datetime.now().isoformat(timespec="seconds"), **rec})
        if rec.get("kind") == "reply":
            k = str(rec["to"])
            self.d["replied"][k] = int(self.d["replied"].get(k, 0)) + 1

    def replies_to(self, mid: int, queued: bool = True) -> int:
        """已经回过这个人几条；queued=True 时，还在待审队列里的也算上。"""
        n = int(self.d["replied"].get(str(mid), 0))
        if queued:
            n += sum(1 for p in self.d["pending"] if p.get("kind") == "reply" and p.get("to") == mid)
        return n

    def auto_paused(self) -> str:
        u = self.d.get("auto_paused_until") or ""
        try:
            return u if u and datetime.fromisoformat(u) > datetime.now() else ""
        except ValueError:
            return ""

    def pause_auto(self, hours: float, why: str) -> None:
        self.d["auto_paused_until"] = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.d["auto_pause_reason"] = why

    def prune(self, revisit_days: int) -> None:
        cutoff = (date.today() - timedelta(days=max(1, revisit_days) * 2)).isoformat()
        self.d["seen"] = {k: v for k, v in self.d["seen"].items() if v >= cutoff}
        cutoff = (date.today() - timedelta(days=14)).isoformat()  # 回复最多只看 7 天内的，记 14 天足够
        self.d["reply_seen"] = {k: v for k, v in self.d["reply_seen"].items() if v >= cutoff}
        self.d["posted"] = self.d["posted"][-500:]
        old = (datetime.now() - timedelta(days=3)).isoformat()
        self.d["pending"] = [p for p in self.d["pending"] if str(p.get("t", "")) >= old][-30:]
        keep = sorted(self.d["spend"])[-62:]
        self.d["spend"] = {k: self.d["spend"][k] for k in keep}


def audit(**event) -> None:
    """每条评论的来龙去脉都记在 logs/audit.jsonl：被哪一关拦下、为什么。"""
    LOGS.mkdir(exist_ok=True)
    event = {"t": datetime.now().isoformat(timespec="seconds"), **event}
    with open(LOGS / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


@dataclass
class Item:
    v: dict                                  # 视频信息（来自 B站，已清洗）
    source: str                              # 怎么找到的
    hits: list = field(default_factory=list)  # 输入侧注入扫描命中
    rd: dict | None = None                   # 读字幕模型的输出（已校验）
    rel: int | None = None                   # 相关度
    why: str = ""                            # 相关度理由
    score: int | None = None                 # 综合分
    status: str = ""                         # 评论环节的结果


def effective_mode(cfg: dict, state: State, sec: str = "comment") -> tuple[str, str]:
    """这一轮评论（sec="comment"）或回复（sec="reply"）实际用什么模式。两者共用 STOP 文件和自动发送暂停。"""
    mode, what = cfg[sec]["mode"], "评论" if sec == "comment" else "回复"
    if STOP.exists():
        return "off", f"发现 STOP 文件：这一轮不写{what}"
    if mode == "auto":
        until = state.auto_paused()
        if until:
            return "review", (f"自动发送暂停到 {until[:16].replace('T', ' ')}（{state.d.get('auto_pause_reason', '')}），"
                              f"这一轮的{what}改进待审队列")
    return mode, ""


# ───────────────────────────── 一轮冲浪 ─────────────────────────────

async def discover(cfg, state, bili, log, notes) -> list[tuple[dict, str]]:
    D = cfg["discover"]
    rows, fails = [], 0
    for kw in D["keywords"]:
        try:
            # 一页大约 40 条，全部拿来筛：排在前面的常是超长合集或老视频，只看前几条很容易一个都剩不下
            rows.append([(it, f"搜索「{kw}」") for it in await bili.search(kw, 50, D["order"])])
        except Exception as e:
            fails += 1
            log(f"搜索「{kw}」失败：{e}")
    if D["popular"] > 0:
        try:
            rows.append([(it, "热门里随手翻到") for it in await bili.popular(D["popular"] * 4)])
        except Exception as e:
            log(f"热门列表读取失败：{e}")
    if D["keywords"] and fails == len(D["keywords"]):
        notes.append("所有搜索都失败了，可能被 B站 风控了，过几个小时再试")
    pool = [x for row in zip_longest(*rows) for x in row if x]  # 各关键词和热门轮流取，避免一个词霸屏、热门被挤掉

    now = datetime.now().timestamp()
    picked, taken, per_src = [], set(), {}
    for it, src in pool:
        b = it["bvid"]
        if b in taken or state.seen_recently(b, D["revisit_after_days"]):
            continue
        if not D["min_minutes"] * 60 <= it["duration"] <= D["max_minutes"] * 60:
            continue
        if it["pubdate"] and now - it["pubdate"] > D["max_age_days"] * 86400:
            continue
        limit = D["popular"] if src.startswith("热门") else D["per_keyword"]
        if per_src.get(src, 0) >= limit:
            continue
        per_src[src] = per_src.get(src, 0) + 1
        taken.add(b)
        picked.append((it, src))
        if len(picked) >= D["max_videos_per_run"]:
            break
    return picked


def commentable(i: Item, cfg: dict, state: State) -> tuple[bool, str]:
    """确定性规则：哪些视频允许进入写评论环节。分数是模型给的，但其余每一条都由代码判断。"""
    C, rd = cfg["comment"], i.rd
    if rd is None:
        return False, ""
    if not i.v["subtitle"] and not rd.get("crowd"):  # 没字幕时，只有从热评里看出了梗才评论
        if i.v.get("comments"):
            return False, "没有字幕，评论区也没看出梗：不评论"
        return False, "没有字幕：不评论没真正读过的内容"
    if i.hits or rd["injection_suspected"]:
        return False, "疑似对 AI 喊话：这个视频不评论"
    if rd["tainted"]:
        return False, "摘要里出现可疑内容：这个视频不评论"
    if rd["flags"]:
        return False, "敏感类别（" + "、".join(rd["flags"]) + "）：不评论"
    if i.score is None or i.score < C["min_score"]:
        return False, ""
    if i.v["views"] < C["min_views"]:
        return False, f"播放不到 {C['min_views']}：不去打扰小 UP"
    if state.commented(i.v["bvid"]):
        return False, "以前评论过"
    return True, ""


async def comment_phase(cfg, state, bili, brain, items, mode, notes, log) -> None:
    from bili import SKIP_VIDEO_CODES
    from guard import check_comment

    C = cfg["comment"]
    queue, skipped = [], Counter()
    for i in items:
        ok, why = commentable(i, cfg, state)
        if ok:
            queue.append(i)
            continue
        if why:
            i.status = why
        skipped[why.split("：")[0] if why else "没读成" if i.rd is None else f"不到 {C['min_score']} 分"] += 1
    if not queue:  # 说清为什么一条都没写，免得只看到日报、不知道评论去哪了
        msg = "这一轮没有视频满足评论条件：" + "，".join(f"{k} {n} 个" for k, n in skipped.most_common())
        notes.append(msg)
        log(msg)
        return
    prefer = {f"搜索「{k}」" for k in C["prefer"]}
    queue.sort(key=lambda i: (i.source not in prefer, -(i.score or 0)))  # 优先关键词搜到的先写，其余按分数

    made = posted = rejected = 0
    for i in queue:
        if made >= C["max_per_run"]:
            break
        if mode == "auto" and (state.posted_today("comment") >= C["max_per_day"]
                               or state.posted_today() >= HARD_MAX_PER_DAY):
            notes.append("今天的评论数到上限了")
            break
        if rejected >= 3:
            notes.append("这一轮已经有 3 条草稿被拦下，停止写评论")
            break
        b = i.v["bvid"]
        text = await brain.write(i.rd)
        if not text:
            i.status = "Claude娘觉得没什么好说的（SKIP）"
            audit(bvid=b, stage="writer_skip")
            log(f"  Claude娘觉得没什么好说的，跳过（{b}）")
            continue
        recent = state.posted_texts() + [p.get("text", "") for p in state.d["pending"]]
        ok, problems, body = check_comment(text, max_chars=C["max_chars"], canary=brain.canary,
                                           leak_corpus=brain.leak_corpus, recent=recent,
                                           crowd=i.v.get("comments") or ())
        if not ok:
            rejected += 1
            i.status = "草稿被硬规则拦下：" + "；".join(problems)
            audit(bvid=b, stage="gate_reject", problems=problems, draft=text)
            log(f"  拦下一条草稿（{b}）：{'；'.join(problems)}")
        else:
            passed, issues = await brain.judge(body)
            if not passed:
                rejected += 1
                i.status = "草稿被终审否决：" + "、".join(issues or ["未说明理由"])
                audit(bvid=b, stage="judge_reject", problems=issues, draft=body)
                log(f"  终审否决一条草稿（{b}）：{'、'.join(issues)}")
            else:
                final = body + C["signature"]
                if mode == "review":
                    state.d["pending"].append({
                        "id": f"{b}-{random.randrange(16**6):06x}", "t": datetime.now().isoformat(timespec="seconds"),
                        "bvid": b, "aid": i.v["aid"], "title": i.v["title"], "author": i.v["author"],
                        "summary": i.rd["summary"], "crowd": i.rd.get("crowd", ""), "text": body, "final": final})
                    made += 1
                    i.status = f"草稿已进待审队列：{final}"
                    audit(bvid=b, stage="queued", final=final)
                else:  # auto
                    if posted:
                        await asyncio.sleep(random.uniform(*C["delay_seconds"]))
                    if STOP.exists():
                        notes.append("发现 STOP 文件，停止发送")
                        break
                    try:
                        rpid = await bili.post_comment(i.v["aid"], final)
                    except Exception as e:
                        code = getattr(e, "code", None)
                        i.status = f"发送失败：{e}"
                        audit(bvid=b, stage="post_error", code=code, error=str(e))
                        if code in SKIP_VIDEO_CODES:
                            continue
                        state.pause_auto(24, f"发送失败 {code}")
                        notes.append("发送出错（可能被风控或需要验证码）：自动发送暂停 24 小时")
                        break
                    # 摘要也记下来：以后有人回复这条评论时，写回复的模型靠它知道视频讲了什么
                    state.add_posted({"kind": "comment", "bvid": b, "aid": i.v["aid"], "rpid": rpid, "text": body,
                                      "final": final, "summary": i.rd["summary"], "crowd": i.rd.get("crowd", ""),
                                      "via": "auto"})
                    posted += 1
                    made += 1
                    i.status = f"已发送：{final}"
                    audit(bvid=b, stage="posted", rpid=rpid, final=final)
        if rejected >= 2 and mode == "auto":
            state.pause_auto(24, "同一轮有多条草稿被拦")
            notes.append("同一轮有 2 条以上草稿被拦，可能有人在搞注入：自动发送暂停 24 小时，后面的改进待审队列")
            mode = "review"


async def reply_phase(cfg, state, bili, brain, mode, notes, log) -> None:
    """接住别人对 Claude娘 评论的回复。每条都要过：确定性筛选（是不是在回她自己发的评论、够不够新、回过这个人几条）
    → 注入扫描 → 审核模型（默认 Sonnet）判断有没有钓鱼 → 写回复（只看审核转述的大意）→ 硬闸门 → 终审。"""
    from bili import SKIP_VIDEO_CODES
    from guard import check_comment, scan_injection

    R, C = cfg["reply"], cfg["comment"]
    try:
        inbox = await bili.replies_to_me()
    except Exception as e:
        log(f"读不到「回复我的」：{e}")
        return
    ours = {p["rpid"]: p for p in state.d["posted"] if type(p.get("rpid")) is int and p["rpid"] > 0}
    seen, today = state.d["reply_seen"], date.today().isoformat()
    oldest = datetime.now().timestamp() - R["max_age_hours"] * 3600
    todo, persons = [], set()
    for r in sorted(inbox, key=lambda r: -r["t"]):  # 新的在前：同一个人连着回了几条，只接最新那条
        if str(r["rpid"]) in seen or r["target"] not in ours:  # 只管回她自己发的评论的
            continue
        if r["t"] < oldest or r["mid"] in persons:
            seen[str(r["rpid"])] = today
            continue
        persons.add(r["mid"])
        todo.append(r)
    if not todo:
        return

    skipped = Counter()

    def done(r: dict, stage: str, why: str = "", **extra) -> None:
        """这条回复处理完了：记为看过（以后不再处理），写审计日志；没回的话记下原因。
        日志里 parent 是对方那条回复的 rpid（她要回到它下面），rpid 留给她发出去的新回复。"""
        seen[str(r["rpid"])] = today
        audit(kind="reply", stage=stage, parent=r["rpid"], to=r["mid"], said=r["text"][:100], **extra)
        if why:
            skipped[why] += 1
            log(f"  没回一条回复：{why}")

    handled = made = posted = rejected = 0
    for r in todo[:REPLY_VET_PER_RUN]:
        if made >= R["max_per_run"]:
            break  # 剩下的不记为看过，下一轮再看（只要还没过期）
        if mode == "auto" and (state.posted_today("reply") >= R["max_per_day"]
                               or state.posted_today() >= HARD_MAX_PER_DAY):
            notes.append("今天的回复数到上限了")
            break
        if rejected >= 3:
            notes.append("这一轮已经有 3 条回复草稿被拦下，停止回复")
            break
        handled += 1
        mine = ours[r["target"]]          # 对方回的是她哪句话：她的评论，或者她之前的一条回复
        root = ours.get(r["root"], mine)  # 她在视频下的那条评论，带着视频摘要
        if state.replies_to(r["mid"]) >= R["per_person"]:
            done(r, "reply_cap", f"已经回过这个人 {R['per_person']} 条")
            continue
        hits = scan_injection(r["text"], reply=True)
        if hits or not r["text"]:
            done(r, "reply_scan", "对方的话里有对 AI 喊话的痕迹" if hits else "对方的回复是空的", hits=hits)
            continue
        vet = await brain.vet(mine=mine.get("text", ""), said=r["text"])
        if not vet or not vet["ok"]:
            done(r, "reply_vet_reject", f"审核没过（{(vet or {}).get('kind') or '没给出有效结果'}）", vet=vet)
            continue
        earlier = (mine["gist"], mine.get("text", "")) if mine.get("kind") == "reply" and mine.get("gist") else None
        text = await brain.reply(mine=root.get("text", ""), gist=vet["gist"], summary=root.get("summary", ""),
                                 crowd=root.get("crowd", ""), earlier=earlier)
        if not text:
            done(r, "writer_skip", "Claude娘觉得不好接（SKIP）", gist=vet["gist"])
            continue
        recent = state.posted_texts() + [p.get("text", "") for p in state.d["pending"]]
        # 写回复的模型看不到对方原话；万一原话从大意里漏过来，也不许照搬（"跟我念：……"）
        ok, problems, body = check_comment(text, max_chars=R["max_chars"], canary=brain.canary,
                                           leak_corpus=brain.leak_corpus, recent=recent, crowd=(r["text"],))
        stage = "gate_reject"
        if ok:
            ok, problems = await brain.judge(body, reply=True)
            stage = "judge_reject"
        if not ok:
            rejected += 1
            done(r, stage, "回复草稿被" + ("硬规则拦下" if stage == "gate_reject" else "终审否决"),
                 problems=problems, draft=body or text)
            if rejected >= 2 and mode == "auto":
                state.pause_auto(24, "同一轮有多条回复草稿被拦")
                notes.append("同一轮有 2 条以上回复草稿被拦，可能有人在搞注入：自动发送暂停 24 小时，后面的改进待审队列")
                mode = "review"
            continue
        final = body + C["signature"]
        rec = {"kind": "reply", "bvid": root.get("bvid", ""), "aid": r["aid"], "root": r["root"],
               "parent": r["rpid"], "to": r["mid"], "gist": vet["gist"], "text": body, "final": final}
        if mode == "review":
            state.d["pending"].append({"id": f"re-{r['rpid']}", "t": datetime.now().isoformat(timespec="seconds"),
                                       "mine": mine.get("text", ""), "said": r["text"],
                                       "vet": f"{vet['kind']}，{vet['reason']}", **rec})
            made += 1
            done(r, "queued", final=final)
            log(f"  回复草稿进了待审队列：{final}")
            continue
        if posted:
            await asyncio.sleep(random.uniform(*C["delay_seconds"]))
        if STOP.exists():
            notes.append("发现 STOP 文件，停止发送")
            break
        try:
            rpid = await bili.post_reply(r["aid"], r["root"], r["rpid"], final)
        except Exception as e:
            code = getattr(e, "code", None)
            done(r, "post_error", f"发送失败 {code}", code=code, error=str(e))
            if code in SKIP_VIDEO_CODES:
                continue
            state.pause_auto(24, f"回复发送失败 {code}")
            notes.append("回复发送出错（可能被风控或需要验证码）：自动发送暂停 24 小时")
            break
        state.add_posted({**rec, "rpid": rpid, "via": "auto"})
        posted += 1
        made += 1
        done(r, "posted", rpid=rpid, final=final)
        log(f"  已回复：{final}")

    msg = f"有人回复她：新回复 {len(todo)} 条，回了 {posted} 条，写了 {made - posted} 条进待审队列"
    if skipped:
        msg += "；没回的：" + "，".join(f"{k} {n} 条" for k, n in skipped.most_common())
    if handled < len(todo):
        msg += f"；还有 {len(todo) - handled} 条下一轮再看"
    notes.append(msg)
    log(msg)


async def run_once(cfg, state, bili, brain, log=print) -> Path:
    from brain import BudgetExhausted, FatalAPIError
    from guard import scan_injection

    started = datetime.now()
    mode, mode_note = effective_mode(cfg, state)
    rmode, rmode_note = effective_mode(cfg, state, "reply") if cfg["reply"]["mode"] != "off" else ("off", "")
    notes = [n for n in (mode_note, rmode_note) if n]
    items: list[Item] = []
    try:
        if rmode != "off":  # 先看看有没有人回复她：回复讲究及时，也不该等读视频把预算花光
            try:
                await reply_phase(cfg, state, bili, brain, rmode, notes, log)
            except (BudgetExhausted, FatalAPIError):
                raise
            except Exception as e:  # 别的错误不耽误冲浪；没处理完的回复没记为看过，下一轮再看
                notes.append(f"回复环节出错，这一轮先不回复：{e}")
                log(notes[-1])
        picked = await discover(cfg, state, bili, log, notes)
        log(f"这一轮挑了 {len(picked)} 个视频。")
        for it, src in picked:
            if brain.ledger.exhausted():
                notes.append("预算到了，剩下的视频没读")
                break
            try:
                v = await bili.detail(it["bvid"], cfg["brain"]["max_subtitle_chars"])
            except Exception as e:
                log(f"  读不到 {it['bvid']}：{e}")
                continue
            v["comments"] = []
            if not v["subtitle"] and cfg["brain"]["hot_comments"]:
                try:  # 没字幕（多半是纯音乐）就看看评论区在聊什么；读不到就算了，这个视频照样不评论
                    v["comments"] = await bili.hot_comments(v["aid"], cfg["brain"]["hot_comments"])
                except Exception as e:
                    log(f"  读不到 {v['bvid']} 的评论区：{e}")
            state.mark_seen(v["bvid"])
            item = Item(v=v, source=src, hits=scan_injection(v["title"], v["desc"], v["subtitle"], *v["comments"]))
            items.append(item)
            try:
                item.rd = await brain.read(v)
                if item.rd:
                    r = await brain.score(item.rd)
                    if r:
                        item.rel, item.why = r
                    rel = item.rel if item.rel is not None else item.rd["quality"]
                    item.score = round((item.rd["quality"] + rel) / 2)
            except (BudgetExhausted, FatalAPIError):
                raise
            except Exception as e:
                log(f"  模型调用失败，跳过 {v['bvid']}：{e}")
            flag = " [疑似对AI喊话]" if item.hits or (item.rd and item.rd["injection_suspected"]) else ""
            heard = f" [没字幕，看了 {len(v['comments'])} 条热评]" if v["comments"] else ""
            log(f"  {item.score if item.score is not None else '-':>2} 分  {v['title'][:40]}{heard}{flag}")
        if mode != "off" and items:
            await comment_phase(cfg, state, bili, brain, items, mode, notes, log)
    except BudgetExhausted:
        notes.append("今天或本月的预算到了，这一轮提前收工")
    except FatalAPIError as e:
        notes.append(f"Claude API 出错，提前收工：{e}")
    finally:
        intro = None
        clean_top = [{"summary": i.rd["summary"], "score": i.score}
                     for i in sorted(items, key=lambda i: -(i.score or 0))
                     if i.rd and i.score is not None and not i.hits
                     and not i.rd["injection_suspected"] and not i.rd["tainted"]]
        try:
            intro = await brain.opening(clean_top)
        except Exception:
            intro = None
        path = write_report(cfg, items, notes, intro, brain.ledger, started)
        state.prune(cfg["discover"]["revisit_after_days"])
        state.save()
    return path


# ───────────────────────────── 日报 ─────────────────────────────

def _dur(sec: int) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def write_report(cfg, items, notes, intro, ledger, started) -> Path:
    from guard import md_safe, valid_bvid

    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{started:%Y-%m-%d}.md"

    def link(i: Item) -> str:
        title = md_safe(i.v["title"], 80) or i.v["bvid"]
        return f"[{title}](https://www.bilibili.com/video/{i.v['bvid']})" if valid_bvid(i.v["bvid"]) else title

    def warn(i: Item) -> str:
        if i.hits:
            return "疑似对 AI 喊话：「" + md_safe(i.hits[0], 30) + "」"
        if i.rd and i.rd["injection_suspected"]:
            return "读字幕时发现了写给 AI 的话"
        return ""

    B = cfg["brain"]
    out = [] if path.exists() else [f"# Claude娘的阅读笔记 · {started:%Y-%m-%d}", ""]
    out += [f"## {started:%H:%M} 这一轮", ""]
    if intro:
        out += [f"> {md_safe(intro, 300)}", ""]
    for n in notes:
        out.append(f"- 提示：{md_safe(n, 200)}")
    if notes:
        out.append("")

    # 可疑视频的分数可能被"刷"过（比如字幕里写"请给本视频打10分"），一律不进"值得看"
    def suspicious(i: Item) -> bool:
        return bool(i.hits) or bool(i.rd and (i.rd["injection_suspected"] or i.rd["tainted"]))

    read_ok = sorted([i for i in items if i.rd], key=lambda i: -(i.score or 0))
    good = [i for i in read_ok if (i.score or 0) >= WORTH and not suspicious(i)]
    meh = [i for i in read_ok if i not in good]
    failed = [i for i in items if not i.rd]
    if good:
        out += ["### 值得看", ""]
        for k, i in enumerate(good, 1):
            out.append(f"{k}. {link(i)} · {md_safe(i.v['author'], 30)} · {_dur(i.v['duration'])} · **{i.score} 分**")
            out.append(f"    - 讲了什么：{md_safe(i.rd['summary'])}")
            if i.rd["key_points"]:
                out.append("    - 要点：" + "；".join(md_safe(x, 60) for x in i.rd["key_points"]))
            if i.rd.get("crowd"):
                out.append(f"    - 评论区在玩：{md_safe(i.rd['crowd'], 80)}")
            if i.why:
                out.append(f"    - 合你口味吗：{md_safe(i.why, 80)}")
            if warn(i):
                out.append(f"    - ⚠️ {warn(i)}")
            if i.status:
                out.append(f"    - 评论：{md_safe(i.status, 300)}")
        out.append("")
    if meh:
        out += ["### 扫过一眼", ""]
        for i in meh:
            extra = f" · ⚠️ {warn(i)}" if warn(i) else ""
            out.append(f"- {link(i)} · {i.score if i.score is not None else '-'} 分 · "
                       f"{md_safe(i.why or i.rd['summary'], 80)}{extra}")
        out.append("")
    if failed:
        out += ["### 没读成", ""]
        out += [f"- {link(i)}" for i in failed]
        out.append("")
    out += ["### 账本", "",
            f"这一轮 Claude 用了输入 {ledger.run_in:,} / 输出 {ledger.run_out:,} tokens，约 ${ledger.run_usd:.4f}。"
            f"今天累计 ${ledger.today():.4f} / 预算 ${B['daily_budget_usd']:.2f}；"
            f"本月 ${ledger.month():.2f} / ${B['monthly_budget_usd']:.2f}。", ""]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return path


# ───────────────────────────── 审批 ─────────────────────────────

async def review(cfg: dict, state: State, bili) -> None:
    from bili import SKIP_VIDEO_CODES
    from guard import check_comment, clean, valid_bvid

    C, R = cfg["comment"], cfg["reply"]
    pend = state.d["pending"]
    if not pend:
        print("待审队列是空的。")
        return
    if STOP.exists():
        print("发现 STOP 文件：审批模式也不会发送。删掉 STOP 再来。")
        return
    ask = lambda prompt: asyncio.to_thread(input, prompt)

    def full(kind: str) -> bool:
        """今天这一种发够了没有：评论和回复各有上限，两者加起来还有总上限。"""
        cap = R["max_per_day"] if kind == "reply" else C["max_per_day"]
        return state.posted_today(kind) >= cap or state.posted_today() >= HARD_MAX_PER_DAY

    print(f"有 {len(pend)} 条待审（评论和回复）。每一条都要你按 y 才会发出去。")
    sent, capped = 0, set()
    for p in list(pend):
        kind = "reply" if p.get("kind") == "reply" else "comment"
        what, limit = ("回复", R["max_chars"]) if kind == "reply" else ("评论", C["max_chars"])
        bvid, aid = p.get("bvid"), p.get("aid")
        ids = [aid, *([p.get("root"), p.get("parent"), p.get("to")] if kind == "reply" else [])]
        if not valid_bvid(bvid) or not all(type(i) is int and i > 0 for i in ids):
            pend.remove(p)
            continue
        if full(kind):
            capped.add(what)
            continue
        if kind == "reply" and state.replies_to(p["to"], queued=False) >= R["per_person"]:
            print(f"\n有一条回复是给已经回过 {R['per_person']} 条的人的，不能再回，丢掉了。")
            pend.remove(p)
            audit(kind="reply", stage="review_discard", parent=p["parent"], to=p["to"], why="已到每人上限")
            state.save()
            continue
        print("\n" + "─" * 48)
        if kind == "reply":
            print(f"有人回复了她：https://www.bilibili.com/video/{bvid}")
            print(f"她说过：{p.get('mine', '')}")
            print(f"对方说：{clean(p.get('said', ''), 300)}")
            print(f"审核：{p.get('vet', '')}")
            print(f"之前回过这个人 {state.replies_to(p['to'], queued=False)} 条（最多 {R['per_person']} 条）")
        else:
            print(f"视频：{p.get('title', '')}（{p.get('author', '')}）")
            print(f"链接：https://www.bilibili.com/video/{bvid}")
            print(f"内容：{p.get('summary', '')}")
            if p.get("crowd"):  # 没字幕、靠热评接梗的草稿：给你看看她接的是什么梗
                print(f"评论区在玩：{p['crowd']}")
        print(f"\n{what}：{p.get('text', '')}{C['signature']}\n")  # 和按 y 后真正发出去的拼法一致
        ans = (await ask("[y] 发送  [n] 丢弃  [e] 改写后发送  [s] 先留着  [q] 退出 > ")).strip().lower()
        if ans == "q":
            break
        if ans == "n":
            pend.remove(p)
            audit(kind=kind, bvid=bvid, stage="review_discard")
            state.save()
            continue
        if ans not in ("y", "e"):
            continue
        if ans == "e":
            mine = (await ask(f"新的{what}正文（签名会自动加上）：")).strip()
            ok, problems, body = check_comment(mine, max_chars=limit, recent=state.posted_texts())
            if not ok:
                print("硬规则提示：" + "；".join(problems))
                if (await ask("这是你自己写的，仍然要发吗？输入 yes 确认 > ")).strip().lower() != "yes":
                    continue
            final, text = clean(mine, 1000) + C["signature"], clean(mine, 1000)
        else:
            # 发之前再过一遍硬闸门，防止 state.json 被改过；发出去的用这次检查过的正文重新拼，不用存着的 final
            ok, problems, body = check_comment(p.get("text", ""), max_chars=limit, recent=state.posted_texts())
            if not ok:
                print("这条现在过不了硬规则（" + "；".join(problems) + "），不发。")
                continue
            final, text = body + C["signature"], body
        if sent:
            wait = random.uniform(*C["delay_seconds"])
            print(f"等 {wait:.0f} 秒再发……")
            await asyncio.sleep(wait)
        try:
            if kind == "reply":  # 回到对方那条回复下面；root、parent 都是当时从 B站 通知里拿的
                rpid = await bili.post_reply(aid, p["root"], p["parent"], final)
            else:
                rpid = await bili.post_comment(aid, final)
        except Exception as e:
            code = getattr(e, "code", None)
            print(f"发送失败：{e}")
            audit(kind=kind, bvid=bvid, stage="post_error", code=code, error=str(e))
            if code in SKIP_VIDEO_CODES:
                pend.remove(p)
                state.save()
                continue
            print("先停下来：可能被风控或需要验证码，过几个小时再试。")
            break
        pend.remove(p)
        keep = ("root", "parent", "to", "gist") if kind == "reply" else ("summary", "crowd")
        state.add_posted({"kind": kind, "bvid": bvid, "aid": aid, **{k: p.get(k, "") for k in keep},
                          "rpid": rpid, "text": text, "final": final, "via": "review"})
        audit(kind=kind, bvid=bvid, stage="posted", rpid=rpid, final=final, via="review")
        state.save()
        sent += 1
        print("已发送。")
    state.save()
    print(f"\n这次发出 {sent} 条，还剩 {len(pend)} 条待审。")
    if capped:
        print(f"今天的{'和'.join(sorted(capped))}发到上限了，剩下的明天再审。")


# ───────────────────────────── 检查 ─────────────────────────────

async def check(cfg: dict, state: State) -> None:
    import anthropic
    from bili import Bili, load_credential

    print("== Claude API ==")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  没找到 ANTHROPIC_API_KEY：在 .env 里填上（见 README）。")
    else:
        try:
            page = await anthropic.AsyncAnthropic().models.list(limit=100)
            ids = {m.id for m in page.data}
            print("  API key 可用。")
            for k in ("fast_model", "write_model", "judge_model", "vet_model"):
                m = cfg["brain"][k]
                print(f"  {k} = {m}" + ("" if m in ids else "  ← 模型列表里没看到这个名字，确认一下"))
        except Exception as e:
            print(f"  API key 不可用：{e}")

    print("== B站 登录 ==")
    cred = load_credential()
    if cred is None:
        print("  还没登录：运行 python login.py")
    else:
        bili = Bili(cred)
        try:
            me = await bili.whoami()
            print(f"  已登录：{me['name']}（UID {me['mid']}）")
        except Exception as e:
            print(f"  登录态不可用（{e}）：重新运行 python login.py")
        finally:
            await bili.close()

    from brain import Ledger
    B, C, R = cfg["brain"], cfg["comment"], cfg["reply"]
    led = Ledger(state.d["spend"], cfg["prices"], B["daily_budget_usd"], B["monthly_budget_usd"])
    mode, note = effective_mode(cfg, state)
    print("== 开关和花费 ==")
    own = config_path().name == "config.toml"
    print(f"  配置文件：{config_path().name}" + ("" if own else "（还没有自己的 config.toml：想改设置就复制一份示例）"))
    print(f"  评论模式：{C['mode']}" + (f"（这一轮实际：{mode}，{note}）" if note else ""))
    hc = f"看点赞最多的 {B['hot_comments']} 条热评" if B["hot_comments"] else "不看热评"
    print(f"  没字幕的视频：{hc}；优先评论的关键词：{'、'.join(C['prefer']) or '没有（只按分数排）'}")
    if R["mode"] == "off":
        print("  回复别人的回复：关（reply.mode = off）")
    else:
        rmode, rnote = effective_mode(cfg, state, "reply")
        print(f"  回复别人的回复：{R['mode']}" + (f"（这一轮实际：{rmode}，{rnote}）" if rnote else "")
              + f"；先过 {B['vet_model']} 审核；同一个人最多回 {R['per_person']} 条")
    print(f"  今天已发评论 {state.posted_today('comment')} / {C['max_per_day']} 条、"
          f"回复 {state.posted_today('reply')} / {R['max_per_day']} 条；待审 {len(state.d['pending'])} 条")
    print(f"  今天花费 ${led.today():.4f} / ${B['daily_budget_usd']:.2f}；本月 ${led.month():.2f} / ${B['monthly_budget_usd']:.2f}")


# ───────────────────────────── 入口 ─────────────────────────────

def make_brain(cfg: dict, state: State):
    from brain import Brain, Ledger
    B = cfg["brain"]
    return Brain(models={"fast": B["fast_model"], "write": B["write_model"], "judge": B["judge_model"],
                         "vet": B["vet_model"]},
                 persona=load_persona(cfg), interests=cfg["master"]["interests"], master=cfg["master"]["name"],
                 max_chars=cfg["comment"]["max_chars"], max_subtitle_chars=int(B["max_subtitle_chars"]),
                 max_reply_chars=cfg["reply"]["max_chars"],
                 ledger=Ledger(state.d["spend"], cfg["prices"], B["daily_budget_usd"], B["monthly_budget_usd"]))


async def amain(args, state: State) -> None:
    from bili import Bili, load_credential

    cfg = load_config()
    if args.check:
        return await check(cfg, state)
    cred = load_credential()
    if cred is None:
        raise SystemExit("还没登录 B站 小号。先运行：python login.py")
    bili = Bili(cred)
    try:
        await bili.refresh_if_needed()
        if args.review:
            return await review(cfg, state, bili)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("没找到 ANTHROPIC_API_KEY：先在 .env 里填上（见 README）。")
        while True:
            path = await run_once(cfg, state, bili, make_brain(cfg, state))
            print(f"\n阅读笔记写好了：{path}")
            if state.d["pending"]:
                print(f"待审 {len(state.d['pending'])} 条（评论和回复）：运行 python surf.py --review 审批。")
            if not args.loop:
                break
            hours = max(0.5, args.loop) * random.uniform(0.85, 1.15)
            print(f"下一轮大约在 {datetime.now() + timedelta(hours=hours):%H:%M}。Ctrl+C 退出。")
            await asyncio.sleep(hours * 3600)
            cfg = load_config()  # 每轮重新读配置，改了 config.toml / persona.md 不用重启
            await bili.refresh_if_needed()  # 循环可能连跑好几天，每轮都看一下登录态要不要续期
    finally:
        await bili.close()


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")

    ap = argparse.ArgumentParser(description="Claude娘 · B站冲浪")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--review", action="store_true", help="逐条审批待发评论和回复")
    g.add_argument("--loop", type=float, metavar="小时", help="每隔大约 N 小时跑一轮")
    g.add_argument("--check", action="store_true", help="检查登录、API key、花费")
    args = ap.parse_args()

    state = State(HERE / "state.json")
    try:
        asyncio.run(amain(args, state))
    except KeyboardInterrupt:
        state.save()
        print("\n已退出。")


if __name__ == "__main__":
    main()
