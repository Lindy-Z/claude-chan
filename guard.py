"""
guard.py —— 注入防护（全是确定性代码，不调用任何模型）
======================================================

所有来自 B站 的文字（标题、简介、字幕、评论、别人的回复、UP 主名……）都按"不可信数据"处理。

防护的思路不是"教模型别上当"——没人能保证模型 100% 不被骗——而是让它被骗了也做不成事：

  1. 模型没有工具、没有账号权限，只返回文字（见 brain.py）；B站 侧代码只有 7 个动作（见 bili.py）。
  2. 读原文的模型和写评论的模型隔离：写评论的只拿到经过本文件检查的短摘要；
     回复别人时，写回复的只拿到审核模型转述、经过本文件检查的一句大意。
  3. 每条评论和回复发出前必须通过本文件的 check_comment()，再通过一个只看正文本身的独立终审。
  4. 默认审批模式：你在终端按 y 之前，什么都不会发出去。
  5. 任何一步出错或拿不准，一律不发（fail closed）。宁可少发，不可错发。

本文件可以用 `python redteam.py` 离线测试。
"""
from __future__ import annotations

import re
import secrets
import unicodedata

# 读字幕的模型可以给视频打的敏感标签；命中任何一个都不评论
FLAGS = ["时政", "灾难或去世", "未成年人", "广告带货", "投资理财", "医疗健康", "争议撕逼", "色情暴力", "个人隐私"]

# 评论里允许出现的 B站 表情（其余方括号内容一律拒绝）
EMOTES = ["doge", "笑哭", "OK", "星星眼", "吃瓜", "思考", "妙啊", "嗑瓜子", "打call", "捂脸",
          "大哭", "疑惑", "喜欢", "呲牙", "给心心", "微笑", "藏狐", "脱单doge", "歪嘴"]
EMOTES_FOR_PROMPT = " ".join(f"[{e}]" for e in ["doge", "思考", "妙啊", "吃瓜", "星星眼", "笑哭"])
_EMOTES_LC = {e.lower() for e in EMOTES}


# ───────────────────────────── 基础清洗 ─────────────────────────────

def strip_controls(s) -> str:
    """去掉控制字符和格式字符（零宽字符、方向控制符等）；换行、制表符变成空格。"""
    out = []
    for ch in str(s or ""):
        if ch in "\n\r\t":
            out.append(" ")
        elif unicodedata.category(ch)[0] != "C":
            out.append(ch)
    return "".join(out)


def clean(s, limit: int) -> str:
    """清洗一段不可信文本：去控制字符、压缩空白、截断。不改动原有的中文标点。"""
    return re.sub(r"\s+", " ", strip_controls(s)).strip()[:limit]


def _nfkc(s) -> str:
    """检测专用：NFKC 归一化（全角→半角、①→1、ⓦ→w）并转小写。只用来判断，不改动要发出去的文字。"""
    return unicodedata.normalize("NFKC", strip_controls(s)).lower()


_SEP = re.compile(r"[\s_\-·•・.。,，、/\\|~～*'\"`^:：;；!！?？()（）\[\]【】{}<>《》「」『』“”‘’…]+")


def _squash(s) -> str:
    """检测专用：在 _nfkc 基础上去掉空白和各种分隔符，对付"微 信""v.x""加-我"这类拆字绕过。"""
    return _SEP.sub("", _nfkc(s))


# 检测长串数字时，把各种"念数字"的写法都当成数字：中文数字、大写数字、电话读法（幺洞拐勾）、字母 o/l
_DIGITISH = str.maketrans({c: "9" for c in "零〇一二三四五六七八九十壹贰叁肆伍陆柒捌玖拾两俩幺洞拐勾ol"})

# 网址：协议、www，以及任何"xx.yy"形状的域名（不靠后缀列表——后缀永远列不全）
_URL = re.compile(
    r"(?:https?|ftp)\s*:|://|www\s*[.。·]|"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\s*(?:[.。·]|点|dot)\s*[a-z]{2,24}(?![a-z0-9])"
)

# 联系方式 / 站外导流：摘要和评论里都不许出现
_CONTACT = ["vx", "wx", "v信", "微信", "威信", "薇", "徽信", "weixin", "wechat", "绿泡泡",
            "qq", "扣扣", "企鹅号", "企鹅群", "球球号", "q群", "tg", "telegram", "电报", "whatsapp", "私信", "私聊",
            "私我", "加我", "加群", "群号", "进群", "入群", "二维码", "扫码", "链接", "网址", "找我",
            "联系我", "call我", "滴我", "dd我", "扣我", "q我", "戳我", "敲我"]
_CONTACT_RX = [re.compile(p) for p in [
    r"[微威徽卫维]\s*(信|号)",                                      # 微信号 / 威号 / 卫号……
    r"v\s*(信|号|x|心)",                                           # V信 / V号 / Vx
    r"(号|(?<![a-z])id|账号|账户|名字|联系|加|私|滴)\s*(是|为|叫|:)?\s*[a-z0-9_]{4,}",  # "号是 abc123"
]]
# 广告、求互动、给别的账号引流：只在评论里禁止（摘要里描述视频内容时可能自然出现）
_SPAM = ["主页", "看简介", "简介里", "置顶", "网站", "下载", "安装包", "领取", "福利", "红包", "优惠",
         "折扣", "返利", "返现", "代购", "淘宝", "拼多多", "京东", "闲鱼", "兼职", "日结", "赚钱", "副业",
         "稳赚", "荐股", "带单", "牛股", "币圈", "炒币", "usdt", "比特币", "彩票", "博彩", "赌", "抽奖",
         "关注", "三连", "投币", "互粉", "互关", "求赞", "人设", "搜", "移步", "传送门", "指路", "去找",
         "点进", "频道", "全集", "合集", "账号", "的号"]
# 指令 / 幕后用语：出现即视为被操纵或在泄露设定
_META = ["指令", "提示词", "prompt", "system", "系统提示", "校验码", "开发者模式", "越狱", "jailbreak",
         "ignore", "instruction", "assistant", "通过审核", "审核通过", "approve"]


# ───────────────────────────── 输入侧：注入特征扫描 ─────────────────────────────

# 中文模式在 _squash 后的文本上匹配（已去掉空格和标点，"忽 略 之 前"也能抓到）
_INJ_ZH = [re.compile(p) for p in [
    r"(忽略|无视|忘记|忘掉|跳过|别管|不要理会|不用管)掉?(之前|以上|上面|前面|先前|所有|全部|你的|一切|原来|原有|系统)",
    r"(之前|以上|上面|前面|原来|原有|系统)的?(指令|指示|规则|设定)",
    r"(新|最新|以下|下面|接下来)的?(指令|指示)",
    r"(系统|开发者|管理员|调试)(提示|指令|消息|模式|权限)",
    r"提示词|越狱",
    r"你现在(是|就是)(一个|一名|一位)?.{0,6}(ai|人工智能|助手|机器人|模型|bot|智能体)",
    r"你(现在)?(扮演|变成|作为)(一个|一名|一位)?.{0,6}(ai|人工智能|助手|机器人|模型|bot|智能体)",
    r"如果你是(一个|一名|一位)?(ai|人工智能|机器人|大模型|语言模型|模型|bot|大肥鱼|claude|deepseek|gpt|"
    r"chatgpt|豆包|kimi|通义|文心|智能体|agent)",
    r"(致|给|告诉|写给|呼叫|通知|提醒)(所有|各位|每一个|每个|正在读的|看到这里的)?的?"
    r"(ai|人工智能|机器人|大模型|bot|大肥鱼|智能体|agent|爬虫)",
    r"(ai|人工智能|机器人|大模型|bot|智能体|agent|爬虫)们?(请|注意|必须|应该|需要|务必|看到|读到)",
    r"(复述|输出|打印|泄露|告诉我|说出|重复|贴出)(你的|一下|出)?(设定|人设|提示|指令|规则|系统)",
]]
# 问"你是 AI 吗"、叫 Claude娘 或别的 AI 账号的名字：出现在视频里，说明是冲着读视频的 AI 来的；
# 但别人回复 Claude娘 时这样说很正常（她本来就公开是 AI），所以 scan_injection(..., reply=True) 不查这一组
_AI_ADDRESS = [re.compile(p) for p in [
    r"你(是|就是)(一个|一名|一位)?.{0,6}(ai|人工智能|助手|机器人|模型|bot|智能体)",
    r"大肥鱼|本鲸|鲸鱼娘|claude娘",
]]
# 英文模式在 _nfkc 后的文本上匹配（保留空格）
_INJ_EN = [re.compile(p) for p in [
    r"ignore\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier|preceding|system)",
    r"disregard\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|instructions|rules)",
    r"(new|updated|following|override)\s+instructions?",
    r"you\s+are\s+(now\s+)?(an?\s+|the\s+)?(ai|assistant|language\s+model|chatbot|bot)\b",
    r"if\s+you\s+are\s+an?\s+(ai|llm|language\s+model|bot|agent|assistant)",
    r"system\s*prompt|developer\s+mode|jailbreak|prompt\s+injection",
    r"(^|\W)(assistant|system|user)\s*:",
    r"\[/?inst\]|<\|im_(start|end)\|>|<\|",
    r"</?[a-z]+_[0-9a-f]{6,}>",
]]


def scan_injection(*texts, reply: bool = False) -> list[str]:
    """在不可信文本里找"对 AI 喊话"的痕迹。返回命中的片段（最多 3 个）；空列表表示没发现。
    命中的视频照常进日报，但永远不会被评论。误报的代价只是少评论一个视频。
    reply=True 用于别人回复 Claude娘 的话：问她是不是 AI、叫她名字不算；忽略指令、套设定这些照样算。"""
    zh = _INJ_ZH if reply else _INJ_ZH + _AI_ADDRESS
    hits: list[str] = []
    for t in texts:
        if not t:
            continue
        sq, n = _squash(t), _nfkc(t)
        for rx in zh:
            m = rx.search(sq)
            if m:
                hits.append(m.group(0)[:24])
        for rx in _INJ_EN:
            m = rx.search(n)
            if m:
                hits.append(m.group(0).strip()[:24])
        if len(hits) >= 3:
            break
    return hits[:3]


# ───────────────────────────── 通用危险内容检查 ─────────────────────────────

def text_hazards(s, *, strict: bool) -> list[str]:
    """检查一段文字里有没有网址、联系方式、指令用语、对 AI 喊话等。
    strict=True 用于要发出去的评论（规则更严）；strict=False 用于检查模型生成的摘要。"""
    n, sq = _nfkc(s), _squash(s)
    p: list[str] = []
    if _URL.search(n) or "http" in sq or "www" in sq:
        p.append("网址/域名")
    if re.search(r"bv[0-9a-z]{10}", sq) or re.search(r"(?<![a-z])av\d{4,}", sq):
        p.append("视频号")
    if "@" in n:
        p.append("@")
    hits = [w for w in _CONTACT if w in sq] + [m.group(0) for rx in _CONTACT_RX for m in [rx.search(n)] if m]
    if hits:
        p.append("联系方式/导流：" + "、".join(hits[:3]))
    hits = [w for w in _META if w in sq]
    if hits:
        p.append("指令/幕后用语：" + "、".join(hits[:3]))
    if strict:
        if re.search(r"\d{5,}", sq.translate(_DIGITISH)):
            p.append("长串数字")
        for tok in re.findall(r"[a-z0-9_]+", n):  # 像账号 ID 的英文串
            if ("_" in tok or (len(tok) >= 5 and re.search(r"[a-z]", tok) and re.search(r"\d", tok))
                    or (len(tok) >= 10 and tok.isalpha())):
                p.append(f"疑似账号/ID：{tok[:16]}")
                break
        hits = [w for w in _SPAM if w in sq]
        if hits:
            p.append("广告/求互动/引流：" + "、".join(hits[:3]))
    inj = scan_injection(s)
    if inj:
        p.append("像是对 AI 喊话：" + inj[0])
    return p


# ───────────────────────────── 读字幕模型的输出校验 ─────────────────────────────

def validate_reading(d) -> dict | None:
    """校验读字幕模型的 JSON 输出。类型不对、缺字段 → None（这个视频就不评论）。
    摘要、要点和评论区的梗如果含有网址、联系方式、指令、对 AI 喊话 → 标记 tainted，不会交给写评论的模型。"""
    if not isinstance(d, dict) or not isinstance(d.get("summary"), str):
        return None
    summary = clean(d["summary"], 80)
    kps_raw, flags_raw = d.get("key_points"), d.get("flags")
    q, inj = d.get("quality"), d.get("injection_suspected")
    if not summary or not isinstance(kps_raw, list) or not isinstance(flags_raw, list):
        return None
    if type(q) is not int or not 0 <= q <= 10 or not isinstance(inj, bool):
        return None
    kps = [c for c in (clean(x, 40) for x in kps_raw[:4] if isinstance(x, str)) if c]
    crowd = clean(d.get("crowd"), 60) if isinstance(d.get("crowd"), str) else ""  # 没读评论区时为空
    flags = [f for f in flags_raw if f in FLAGS]
    tainted = any(text_hazards(x, strict=False) for x in [summary, *kps, crowd])
    return {"summary": summary, "key_points": kps, "crowd": crowd, "quality": q, "flags": flags,
            "injection_suspected": inj, "tainted": tainted}


# ───────────────────────────── 审核模型的输出校验 ─────────────────────────────

# 审核模型给"别人对 Claude娘 的回复"分的类；只有第一类才可能接话
VET_KINDS = ["正常互动", "套话", "指令或操控", "引流广告", "敏感话题", "辱骂骚扰", "个人隐私", "看不懂或拿不准"]


def validate_vet(d) -> dict | None:
    """校验审核模型对一条回复的判断。类型不对、缺字段 → None（不回）。
    只有 safe 是 true、类别是「正常互动」、转述的大意非空而且干净（没有网址、联系方式、指令、对 AI 喊话）时，
    ok 才是 True；写回复的模型只会拿到这句大意，看不到对方原话。"""
    if not isinstance(d, dict) or not isinstance(d.get("safe"), bool) or d.get("kind") not in VET_KINDS:
        return None
    gist = clean(d.get("gist"), 40) if isinstance(d.get("gist"), str) else ""
    tainted = bool(gist) and bool(text_hazards(gist, strict=False))
    ok = d["safe"] and d["kind"] == VET_KINDS[0] and bool(gist) and not tainted
    return {"ok": ok, "kind": d["kind"], "gist": gist, "reason": clean(d.get("reason"), 40), "tainted": tainted}


# ───────────────────────────── 输出侧：评论硬闸门 ─────────────────────────────

def _char_ok(ch: str) -> bool:
    """字符白名单：中日文字、标点、emoji、ASCII。拒绝希腊/西里尔等同形字母、组合符号、私用区字符。"""
    if ch.isascii():
        return ch.isprintable() and ch not in "@#<>{}|\\`"
    if ch in "︎️":  # emoji 变体选择符
        return True
    cat, name = unicodedata.category(ch), unicodedata.name(ch, "")
    if cat[0] == "L":
        return name.startswith(("CJK UNIFIED", "CJK COMPATIBILITY", "HIRAGANA", "KATAKANA"))
    if cat[0] == "N":
        return name.startswith(("IDEOGRAPHIC", "CJK"))
    if cat[0] in "PZ":
        return True
    if cat[0] == "S":
        return not any(w in name for w in ("LATIN", "LETTER", "DIGIT", "REGIONAL INDICATOR"))
    return False


_EMOTE = re.compile(r"\[([^\[\]]{1,12})\]")


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    A, B = _bigrams(_squash(a)), _bigrams(_squash(b))
    return len(A & B) / len(A | B) if A and B else 0.0


def _leaks(sq_text: str, corpus: str, n: int = 16) -> bool:
    """评论里是否出现了人设/规则原文中连续 n 个字（防止提示词泄露）。"""
    c = _squash(corpus)
    if len(sq_text) < n or len(c) < n:
        return False
    windows = {c[i:i + n] for i in range(len(c) - n + 1)}
    return any(sq_text[i:i + n] in windows for i in range(len(sq_text) - n + 1))


def check_comment(text, *, max_chars: int, canary: str = "", leak_corpus: str = "",
                  recent=(), crowd=()) -> tuple[bool, list[str], str]:
    """评论发出前的硬闸门。返回 (是否通过, 问题列表, 清洗后的正文)。命中任何一条都不发。
    crowd 是这个视频评论区的热评原文：可以顺着梗接话，但不能整句照搬别人的评论。"""
    body = clean(text, 5000).strip("\"'“”‘’「」『』 ")
    if not body:
        return False, ["空评论"], ""
    problems: list[str] = []
    if len(body) > max_chars:
        problems.append(f"超长（{len(body)}/{max_chars} 字）")
    if len(body) < 6:
        problems.append("太短")
    n, sq = _nfkc(body), _squash(body)

    bad = sorted({ch for ch in n if not _char_ok(ch)})
    if bad:
        problems.append("可疑字符：" + "".join(bad)[:10])
    rest = _EMOTE.sub(lambda m: "" if m.group(1) in _EMOTES_LC else m.group(0), n)
    if any(ch in rest for ch in "[]【】"):
        problems.append("非白名单表情或方括号")

    problems += text_hazards(body, strict=True)

    if canary and any(x in sq for x in (canary, canary[:8], canary[-8:])):
        problems.append("泄露了内部校验码")
    if leak_corpus and _leaks(sq, leak_corpus):
        problems.append("复述了人设或规则原文")

    cjk = sum(1 for ch in rest if unicodedata.name(ch, "").startswith("CJK UNIFIED"))
    latin = sum(1 for ch in rest if ch.isascii() and ch.isalpha())
    if cjk + latin == 0 or cjk / (cjk + latin) < 0.6:
        problems.append("中文太少（不像 Claude娘 会写的评论）")

    if any(similarity(body, r) >= 0.5 for r in recent if r):
        problems.append("和最近的评论太像")
    if any(similarity(body, c) >= 0.5 or _leaks(sq, c) for c in crowd if c):
        problems.append("照搬了评论区的评论")
    return (not problems), problems, body


# ───────────────────────────── 其他工具 ─────────────────────────────

_BVID = re.compile(r"BV[0-9A-Za-z]{10}")


def valid_bvid(s) -> bool:
    return isinstance(s, str) and _BVID.fullmatch(s) is not None


def fence(tag: str, body) -> tuple[str, str]:
    """把不可信内容装进带随机后缀的标签。内容里的尖括号会被替换，所以没法伪造或提前闭合标签。"""
    name = f"{tag}_{secrets.token_hex(4)}"
    safe = str(body).replace("<", "‹").replace(">", "›")
    return name, f"<{name}>\n{safe}\n</{name}>"


_URL_RAW = re.compile(r"(?:https?|ftp)://\S+|www\.\S+", re.IGNORECASE)
_BARE_DOMAIN = re.compile(r"(?<![A-Za-z0-9-])([A-Za-z0-9-]+)\.(?=[A-Za-z]{2,24}(?![A-Za-z0-9]))")
_MD = str.maketrans({"[": "［", "]": "］", "(": "（", ")": "）", "<": "‹", ">": "›", "!": "！", "`": "'",
                     "|": "｜", "*": "＊", "_": "＿", "#": "＃", "\\": "＼", "@": "＠", "~": "～", "$": "＄"})


def md_safe(s, limit: int = 300) -> str:
    """把任何不可信文字安全地写进 Markdown 日报：删掉网址；裸域名的点换成全角（"a．sh"，不可点击但能读）；
    再把能组成链接/图片/HTML 的符号换成全角。"""
    s = _URL_RAW.sub("〔链接已删除〕", clean(s, limit))
    prev = None
    while prev != s:
        prev, s = s, _BARE_DOMAIN.sub(r"\1．", s)
    return s.translate(_MD)
