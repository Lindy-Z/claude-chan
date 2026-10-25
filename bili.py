"""
bili.py —— B站 接口
==================

这里刻意只实现 7 个动作：搜索、读视频信息、读字幕、读热评、读「回复我的」、发一级评论、回复别人对她评论的回复。
读热评只在视频没有字幕时用，只取评论文字；读「回复我的」只取评论 ID、对方 UID（用来数回过他几条）和回复文字。
两者都不取用户名。
没有私信、点赞、投币、关注、删除、改资料，也不会去别人的评论下面主动搭话——模型就算被骗，代码里也不存在这些能力。

模型从不接触登录凭据，也从不决定往哪发：视频 ID 和评论 ID 全部来自 B站 接口本身。
没有验证码破解、没有指纹伪装、没有代理池：B站 一旦拦，就停。
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import random
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bilibili_api import Credential, comment, hot, search, session, user, video

from guard import clean, valid_bvid

HERE = Path(__file__).resolve().parent
CRED_FILE = HERE / "cred.json"
_SUBTITLE_HOSTS = (".hdslb.com", ".bilibili.com", ".biliapi.net")  # 字幕文件只从这些域名下载
_CRED_KEYS = ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value")
_REPLY_PREFIX = re.compile(r"^回复\s*@[^:：]{1,40}[:：]\s*")  # 楼中楼回复开头的「回复 @某某 :」，里面是用户名，去掉

# 发评论时遇到这些错误码只跳过当前视频（评论区已关闭 / 重复评论）；其他错误码一律停止发送
SKIP_VIDEO_CODES = {12002, 12051}


def load_credential() -> Credential | None:
    if not CRED_FILE.exists():
        return None
    d = json.loads(CRED_FILE.read_text("utf-8"))
    return Credential(**{k: d.get(k) for k in _CRED_KEYS})


def save_credential(cred: Credential) -> None:
    tmp = CRED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: getattr(cred, k, None) for k in _CRED_KEYS}, indent=2), "utf-8")
    os.replace(tmp, CRED_FILE)
    try:
        os.chmod(CRED_FILE, 0o600)
    except OSError:
        pass


def _secs(v) -> int:
    """'12:34' / '1:02:03' / 754 → 秒数"""
    if isinstance(v, (int, float)):
        return int(v)
    total = 0
    for p in str(v or "").split(":"):
        if not p.strip().isdigit():
            return 0
        total = total * 60 + int(p)
    return total


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _brief(bvid, title, author, duration, pubdate, views) -> dict:
    title = html.unescape(re.sub(r"<[^>]+>", "", str(title or "")))  # 搜索结果标题带 <em class="keyword">
    return {"bvid": bvid, "title": clean(title, 80), "author": clean(author, 30),
            "duration": duration, "pubdate": _int(pubdate), "views": _int(views)}


class Bili:
    def __init__(self, cred: Credential, delay: tuple[float, float] = (1.5, 4.0)):
        self.cred, self.delay = cred, delay
        self.http = httpx.AsyncClient(timeout=20, follow_redirects=False,
                                      headers={"Referer": "https://www.bilibili.com/"})

    async def close(self) -> None:
        await self.http.aclose()

    async def _pause(self) -> None:
        await asyncio.sleep(random.uniform(*self.delay))

    async def whoami(self) -> dict:
        me = await user.get_self_info(self.cred)
        return {"name": me.get("name", "?"), "mid": me.get("mid")}

    async def refresh_if_needed(self) -> None:
        """登录态快过期时自动续期（需要扫码登录时拿到的 ac_time_value）。"""
        try:
            if self.cred.has_ac_time_value() and await self.cred.check_refresh():
                await self.cred.refresh()
                save_credential(self.cred)
        except Exception:
            pass

    # ───────── 读 ─────────

    async def search(self, keyword: str, n: int, order: str = "totalrank") -> list[dict]:
        await self._pause()
        orders = {"totalrank": search.OrderVideo.TOTALRANK, "pubdate": search.OrderVideo.PUBDATE,
                  "click": search.OrderVideo.CLICK}
        res = await search.search_by_type(keyword, search_type=search.SearchObjectType.VIDEO,
                                          order_type=orders.get(order, search.OrderVideo.TOTALRANK), page=1)
        out = []
        for it in (res or {}).get("result") or []:
            if it.get("type") != "video" or not valid_bvid(it.get("bvid")):
                continue
            out.append(_brief(it["bvid"], it.get("title"), it.get("author"), _secs(it.get("duration")),
                              it.get("pubdate"), it.get("play")))
            if len(out) >= n:
                break
        return out

    async def popular(self, n: int) -> list[dict]:
        await self._pause()
        res = await hot.get_hot_videos(pn=1, ps=50)
        items = [it for it in (res or {}).get("list") or [] if valid_bvid(it.get("bvid"))]
        random.shuffle(items)
        return [_brief(it["bvid"], it.get("title"), (it.get("owner") or {}).get("name"),
                       _secs(it.get("duration")), it.get("pubdate"), (it.get("stat") or {}).get("view"))
                for it in items[:n]]

    async def detail(self, bvid: str, max_subtitle_chars: int) -> dict:
        if not valid_bvid(bvid):
            raise ValueError(f"不合法的视频号：{bvid!r}")
        await self._pause()
        v = video.Video(bvid=bvid, credential=self.cred)
        info = await v.get_info()
        aid, cid = info.get("aid"), info.get("cid")
        if not isinstance(aid, int) or not isinstance(cid, int):
            raise ValueError("视频信息里缺少 aid/cid")
        sub_text = ""
        try:
            await self._pause()
            sub_text = await self._subtitle_text(await v.get_subtitle(cid=cid))
        except Exception:
            sub_text = ""  # 没字幕或读不到：照样进日报，但不会被评论
        stat = info.get("stat") or {}
        return {
            "bvid": bvid, "aid": aid,
            "title": clean(info.get("title"), 80),
            "desc": clean(info.get("desc"), 500),
            "author": clean((info.get("owner") or {}).get("name"), 30),
            "tname": clean(info.get("tname"), 12),
            "duration": _int(info.get("duration")),
            "pubdate": _int(info.get("pubdate")),
            "views": _int(stat.get("view")),
            "subtitle": sub_text[:max_subtitle_chars],
            "subtitle_truncated": len(sub_text) > max_subtitle_chars,
        }

    async def _subtitle_text(self, sub: dict | None) -> str:
        subs = [s for s in (sub or {}).get("subtitles") or [] if s.get("subtitle_url")]
        if not subs:
            return ""

        def rank(s):  # UP 主上传的中文字幕 > AI 中文字幕 > 其他
            lan = str(s.get("lan", ""))
            return 0 if lan.startswith("zh") else 1 if lan.startswith("ai-zh") else 2

        url = str(sorted(subs, key=rank)[0]["subtitle_url"])
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("http://"):
            url = "https://" + url[7:]
        u = urlparse(url)
        if u.scheme != "https" or not (u.hostname or "").endswith(_SUBTITLE_HOSTS):
            return ""
        r = await self.http.get(url)
        r.raise_for_status()
        if len(r.content) > 3_000_000:
            return ""
        lines, last = [], None
        for b in (r.json().get("body") or []):
            t = clean(b.get("content"), 300)
            if t and t != last:
                lines.append(t)
                last = t
        return " ".join(lines)

    async def hot_comments(self, aid: int, n: int) -> list[str]:
        """评论区点赞最多的 n 条一级评论，只取文字（不取用户名、不取置顶）。视频没有字幕时用来了解内容和梗。"""
        if not isinstance(aid, int) or aid <= 0:
            raise ValueError("不合法的 aid")
        await self._pause()
        res = await comment.get_comments_lazy(oid=aid, type_=comment.CommentResourceType.VIDEO,
                                              order=comment.OrderType.LIKE, credential=self.cred)
        out: list[str] = []
        for r in (res or {}).get("replies") or []:
            t = clean((r.get("content") or {}).get("message"), 100)
            if t and t not in out:
                out.append(t)
            if len(out) >= n:
                break
        return out

    async def replies_to_me(self) -> list[dict]:
        """「回复我的」最新一页（大约 20 条）里视频评论区的回复。只取评论 ID、对方 UID 和回复文字，不取昵称头像。
        是不是在回 Claude娘 自己发的评论，由调用方拿 state 里记的 rpid 去核对。"""
        await self._pause()
        res = await session.get_replies(self.cred)
        me = _int(getattr(self.cred, "dedeuserid", 0))
        out = []
        for it in (res or {}).get("items") or []:
            x, mid = it.get("item") or {}, (it.get("user") or {}).get("mid")
            if x.get("type") != "reply" or x.get("business_id") != 1:  # 只要视频评论区的回复
                continue
            ids = [x.get(k) for k in ("subject_id", "root_id", "target_id", "source_id")]
            if not all(type(i) is int and i > 0 for i in [*ids, mid]) or mid == me:
                continue
            text = clean(_REPLY_PREFIX.sub("", clean(x.get("source_content"), 1000)), 300)
            out.append({**dict(zip(("aid", "root", "target", "rpid"), ids)), "mid": mid, "text": text,
                        "t": _int(it.get("reply_time"))})
        return out

    # ───────── 写（只有这两个写操作）─────────

    async def post_comment(self, aid: int, text: str) -> int:
        """在视频下发一条一级评论。aid 来自 B站 接口，不来自模型。返回评论 rpid。"""
        if not isinstance(aid, int) or aid <= 0:
            raise ValueError("不合法的 aid")
        res = await comment.send_comment(text=text, oid=aid, type_=comment.CommentResourceType.VIDEO,
                                         credential=self.cred)
        return _int((res or {}).get("rpid"))

    async def post_reply(self, aid: int, root: int, parent: int, text: str) -> int:
        """回复别人对 Claude娘 评论的回复（楼中楼）。aid、root、parent 全部来自「回复我的」接口，
        调用方核对过对方回的是她自己发的评论；不来自模型。返回新回复的 rpid。"""
        if not all(type(i) is int and i > 0 for i in (aid, root, parent)):
            raise ValueError("不合法的评论 id")
        res = await comment.send_comment(text=text, oid=aid, type_=comment.CommentResourceType.VIDEO,
                                         root=root, parent=parent, credential=self.cred)
        return _int((res or {}).get("rpid"))
