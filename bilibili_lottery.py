#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili 抽奖脚本 (Python 3.8+, 依赖: requests)

功能
  * 抽奖地址由命令行传入: BV号 / av号 / 视频链接 / 动态ID / t.bilibili.com / opus 链接 / b23.tv 短链
  * 内置 ID 转换: 视频 BV/av -> aid(oid) + cid ; 动态ID -> 评论区 oid + 评论区 type
  * 抽奖前询问目标是 "视频" 还是 "动态", 分别使用对应的 API
  * 抽奖条件可选: 评论 / 转发 / 点赞 / 关注 (可多选, 全部满足 或 满足任一)
  * 开始前可手动导入 cookie txt (k=v; k=v / 每行一个 / Netscape cookies.txt / JSON 导出)
  * 自动排除发布者 + 手动排除名单, 自动去重
  * 结果自动保存为 JSON (含候选名单与随机种子, 可用 --seed 复现抽奖结果)

示例
  python bili_lottery.py https://www.bilibili.com/video/BV1xx411c7mD -n 3
  python bili_lottery.py https://t.bilibili.com/123456789012345678 -c comment repost follow
  python bili_lottery.py BV1xx411c7mD -t video -c comment --cookie-file cookie.txt --no-prompt
"""

import argparse
import hashlib
import json
import os
import random
import re
import secrets
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime

try:
    import requests
except ImportError:  # pragma: no cover
    print("缺少依赖 requests, 请先执行: pip install requests")
    sys.exit(1)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

COND_NAMES = {"comment": "评论", "repost": "转发", "like": "点赞", "follow": "关注"}

# ------------------------------------------------------------------ 输出辅助
if os.name == "nt":
    os.system("")  # 让 Windows 终端启用 ANSI 颜色
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

C = {"cyan": "\033[96m", "green": "\033[92m", "yellow": "\033[93m",
     "red": "\033[91m", "gray": "\033[90m", "bold": "\033[1m", "end": "\033[0m"}


def say(tag, color, msg):
    print(f"{C[color]}[{tag}]{C['end']} {msg}")


def info(m): say("*", "cyan", m)
def ok(m): say("+", "green", m)
def warn(m): say("!", "yellow", m)
def err(m): say("x", "red", m)


def line():
    print(C["gray"] + "-" * 60 + C["end"])


def disp_width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, w):
    s = str(s)
    return s + " " * max(0, w - disp_width(s))


def print_table(headers, rows):
    widths = [max(disp_width(h), *(disp_width(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(pad(c, widths[i]) for i, c in enumerate(r)))


def ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


# ------------------------------------------------------------------ WBI 签名解决
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19,
             29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
             22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]


def get_mixin_key(img_key, sub_key):
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_TAB)[:32]


def wbi_query(params, mixin_key, wts=None):
    p = {k: str(v) for k, v in params.items()}
    p["wts"] = str(int(time.time()) if wts is None else wts)
    p = {k: "".join(ch for ch in v if ch not in "!'()*") for k, v in sorted(p.items())}
    query = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}" for k, v in p.items())
    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={w_rid}"


# ------------------------------------------------------------------ 客户端UA设置
class Client:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/",
                               "Accept": "application/json, text/plain, */*"})
        self.cookie = ""
        self.mixin_key = None
        self.fetch_error = False
        self.comment_upper = None
        self.debug = False
        self.me_mid = None
        self.me_name = None

    @staticmethod
    def polite():
        time.sleep(random.uniform(0.7, 1.4))

    def set_cookie(self, cookie):
        self.cookie = cookie
        self.mixin_key = None
        if cookie:
            self.s.headers["Cookie"] = cookie
        else:
            self.s.headers.pop("Cookie", None)

    def get(self, url, params=None):
        try:
            r = self.s.get(url, params=params, timeout=15)
        except Exception as e:
            warn(f"网络请求失败: {e}")
            return None
        r.encoding = "utf-8"
        if self.debug:
            print(C["gray"] + f"[debug] {r.status_code} {r.url}\n[debug] {r.text[:300]}" + C["end"])
        try:
            return r.json()
        except ValueError:
            snippet = r.text.strip().replace("\n", " ")[:80]
            warn(f"接口返回的不是 JSON (HTTP {r.status_code}){': ' + snippet if snippet else ' (空响应)'}")
            return None

    def get_api(self, url, params):
        r = None
        key = self.get_mixin()
        if key:
            r = self.get(url + "?" + wbi_query(params, key))
            if r and r.get("code") == 0:
                return r
        r2 = self.get(url, params)
        return r2 if (r2 is not None or r is None) else r

    def refresh_login(self):
        r = self.get("https://api.bilibili.com/x/web-interface/nav")
        d = (r or {}).get("data") or {}
        if d.get("isLogin") and d.get("mid"):
            self.me_mid, self.me_name = str(d["mid"]), d.get("uname")
        else:
            self.me_mid = self.me_name = None
        return self.me_mid

    def add_buvid(self):
        if "buvid3=" in self.cookie:
            return
        r = self.get("https://api.bilibili.com/x/frontend/finger/spi")
        if r and r.get("code") == 0 and r["data"].get("b_3"):
            extra = f"buvid3={r['data']['b_3']}; buvid4={r['data'].get('b_4', '')}"
            self.set_cookie(f"{self.cookie}; {extra}" if self.cookie else extra)

    def get_mixin(self):
        if self.mixin_key:
            return self.mixin_key
        r = self.get("https://api.bilibili.com/x/web-interface/nav")
        try:
            img = r["data"]["wbi_img"]
            fn = lambda u: u.rsplit("/", 1)[-1].split(".")[0]
            self.mixin_key = get_mixin_key(fn(img["img_url"]), fn(img["sub_url"]))
        except Exception:
            self.mixin_key = None
        return self.mixin_key


def show_api_error(r, what):
    if r is None:
        err(f"{what}: 网络请求失败 (可能被风控, 建议导入 cookie)。")
        return
    err(f"{what}: API 返回 code={r.get('code')} message={r.get('message')}")
    if r.get("code") in (-352, -412, -799, -101):
        warn("这通常是风控或未登录导致, 建议导入 cookie txt 后重试。")
    elif r.get("code") == 12002:
        warn("该评论区已关闭。")


# ------------------------------------------------------------------ Cookie
def select_cookie_file():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title="选择 cookie txt 文件",
                                          filetypes=[("Cookie 文本", "*.txt"), ("所有文件", "*.*")])
        root.destroy()
        return path or None
    except Exception:
        p = ask("请输入 cookie txt 文件的完整路径 (留空取消): ")
        return p.strip('"') or None


def parse_cookie_text(raw):
    pairs = {}
    t = raw.strip().lstrip("\ufeff")
    if t[:1] in "[{":
        try:
            data = json.loads(t)
            if isinstance(data, dict):
                data = data.get("cookies", [data])
            for c in data:
                if isinstance(c, dict) and c.get("name"):
                    pairs[str(c["name"])] = str(c.get("value", ""))
        except Exception:
            pass
    if not pairs:
        for ln in t.splitlines():
            ln = re.sub(r"^(?i:cookie:)\s*", "", ln.strip())
            if not ln or (ln.startswith("#") and not ln.startswith("#HttpOnly_")):
                continue
            f = ln.split("\t")
            if len(f) >= 7:
                if re.search(r"bilibili|bilivideo", f[0]):
                    pairs[f[5]] = f[6]
                continue
            for seg in ln.split(";"):
                if "=" in seg:
                    k, v = seg.strip().split("=", 1)
                    if k.strip():
                        pairs[k.strip()] = v.strip()
    return pairs


def import_cookie_file(client, path):
    if not os.path.isfile(path):
        err(f"找不到文件: {path}")
        return False
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as fh:
        pairs = parse_cookie_text(fh.read())
    if not pairs:
        err("cookie 文件内容无法解析。")
        return False
    client.set_cookie("; ".join(f"{k}={v}" for k, v in pairs.items()))
    if "SESSDATA" not in pairs:
        warn("未发现 SESSDATA, 可能不是登录状态的 cookie。")
    if client.refresh_login():
        ok(f"cookie 已导入, 当前登录账号: {client.me_name} (UID: {client.me_mid})")
    else:
        warn("cookie 已导入, 但未检测到登录状态 (可能已过期), 将继续尝试。")
    return True


# ------------------------------------------------------------------ 地址解析 / ID 转换
def parse_target(text, client=None):
    s = text.strip().strip('"')
    if re.search(r"(?i)(b23\.tv|bili2233\.cn)/", s):
        info("检测到短链, 正在还原...")
        u = s if s.startswith("http") else "https://" + s
        try:
            s = requests.get(u, headers={"User-Agent": UA}, timeout=15, allow_redirects=True).url
        except Exception as e:
            warn(f"短链还原失败: {e}")
    m = re.search(r"(?i)\b(BV[0-9A-Za-z]{10})", s)
    if m:
        return "Video", "BV" + m.group(1)[2:]
    m = re.search(r"(?i)(?:/video/|^)av(\d+)", s)
    if m:
        return "Video", "av" + m.group(1)
    m = re.search(r"(?i)(?:t\.bilibili\.com|/opus|/dynamic)/(\d{6,})", s)
    if m:
        return "Dynamic", m.group(1)
    if re.fullmatch(r"\d{15,}", s):
        return "Dynamic", s
    if re.fullmatch(r"\d{1,14}", s):
        return "Digits", s  # 纯数字: 视频aid 或 动态ID, 由用户选择
    return "Unknown", None


def resolve_video(client, vid):
    q = {"bvid": vid} if vid.upper().startswith("BV") else {"aid": re.sub(r"\D", "", vid)}
    r = client.get("https://api.bilibili.com/x/web-interface/view", q)
    if not r or r.get("code") != 0:
        show_api_error(r, "获取视频信息")
        return None
    d = r["data"]
    return {"kind": "Video", "oid": str(d["aid"]), "comment_type": 1, "cid": str(d.get("cid")),
            "bvid": d.get("bvid"), "dynamic_id": None, "upper_uid": str(d["owner"]["mid"]),
            "upper_name": d["owner"]["name"], "title": d.get("title"), "pages": len(d.get("pages", []))}


def _dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def resolve_dynamic(client, did):
    r = client.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail",
                   {"id": did, "features": "itemOpusStyle"})
    if not r or r.get("code") != 0:
        show_api_error(r, "获取动态信息")
        return None
    item = _dig(r, "data", "item")
    if not item:
        err("动态不存在或已被删除。")
        return None
    basic = item.get("basic", {})
    oid = str(basic.get("comment_id_str") or did)
    ctype = int(basic.get("comment_type") or 17)
    md = _dig(item, "modules", "module_dynamic") or {}
    title = (_dig(md, "major", "archive", "title") or _dig(md, "major", "opus", "title")
             or _dig(md, "major", "opus", "summary", "text") or _dig(md, "desc", "text") or "")
    title = title if len(title) <= 40 else title[:40] + "..."
    author = _dig(item, "modules", "module_author") or {}
    t = {"kind": "Dynamic", "oid": oid, "comment_type": ctype, "cid": None,
         "bvid": _dig(md, "major", "archive", "bvid"), "dynamic_id": str(item.get("id_str") or did),
         "upper_uid": str(author.get("mid", "")), "upper_name": author.get("name", ""), "title": title}
    if ctype == 1 and t["bvid"]:  # 视频动态: 顺带换出 cid
        v = resolve_video(client, t["bvid"])
        if v:
            t["cid"] = v["cid"]
    return t


def resolve_target(client, parsed, kind):
    pk, pid = parsed
    if kind == "Video":
        if pk == "Video":
            return resolve_video(client, pid)
        if pk == "Digits":
            return resolve_video(client, "av" + pid)
        if pk == "Dynamic":
            d = resolve_dynamic(client, pid)
            if not d:
                return None
            if d["comment_type"] == 1 and d["bvid"]:
                v = resolve_video(client, d["bvid"])
                if v:
                    v["dynamic_id"] = d["dynamic_id"]
                return v
            err("该动态不是视频动态, 无法按视频处理。请改选 [动态]。")
            return None
    else:
        if pk in ("Dynamic", "Digits"):
            return resolve_dynamic(client, pid)
        if pk == "Video":
            warn("输入的是视频ID, 无法反推出对应动态, 将按视频处理 (没有转发/点赞名单)。")
            return resolve_video(client, pid)
    err("无法识别输入的地址或ID。")
    return None


# ------------------------------------------------------------------ 名单获取  (均返回 {uid: 用户名})
def get_comments(client, target, max_pages):
    users, api, offset, nxt, page, done = {}, "wbi", "", 0, 0, False
    info(f"获取评论 (oid={target['oid']}, type={target['comment_type']}) ...")
    while page < max_pages:
        r = None
        if api == "wbi":
            key = client.get_mixin()
            if key:
                q = wbi_query({"oid": target["oid"], "type": target["comment_type"], "mode": 3,
                               "pagination_str": json.dumps({"offset": offset}, separators=(",", ":")),
                               "plat": 1, "web_location": "1315875"}, key)
                r = client.get("https://api.bilibili.com/x/v2/reply/wbi/main?" + q)
        else:
            r = client.get("https://api.bilibili.com/x/v2/reply/main",
                           {"oid": target["oid"], "type": target["comment_type"], "mode": 3, "next": nxt, "ps": 20})
        if not r or r.get("code") != 0:
            if api == "wbi" and page == 0:
                warn("WBI 评论接口失败, 切换到旧接口重试...")
                api = "legacy"
                continue
            show_api_error(r, "获取评论")
            client.fetch_error = True
            return users
        data = r.get("data") or {}
        if not client.comment_upper and _dig(data, "upper", "mid"):
            client.comment_upper = str(data["upper"]["mid"])
        replies = (data.get("replies") or []) + (data.get("top_replies") or [])
        if not replies:
            done = True
            break
        for rep in replies:
            uid = str(_dig(rep, "member", "mid") or rep.get("mid") or "")
            if uid and uid not in users:
                users[uid] = _dig(rep, "member", "uname") or f"UID{uid}"
        page += 1
        info(f"已获取评论第 {page} 页 (累计 {len(users)} 人)")
        cur = data.get("cursor") or {}
        if api == "wbi":
            offset = _dig(cur, "pagination_reply", "next_offset")
            if cur.get("is_end") or not offset:
                done = True
                break
        else:
            if cur.get("is_end") or cur.get("next") is None:
                done = True
                break
            nxt = cur["next"]
        client.polite()
    if not done:
        warn(f"评论已达 --max-pages={max_pages} 上限, 可能未取全, 可调大该参数。")
    return users


def get_reposts(client, dyn_id, max_pages):
    """新版接口: x/polymer/web-dynamic/v1/detail/forward"""
    users, offset, done = {}, "", False
    info("获取转发名单 ...")
    for i in range(max_pages):
        r = client.get_api("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail/forward",
                           {"id": dyn_id, "offset": offset, "web_location": "333.1369"})
        if not r or r.get("code") != 0:
            show_api_error(r, "获取转发名单")
            if i == 0:
                client.fetch_error = True
            return users
        data = r.get("data") or {}
        items = data.get("items") or []
        if not items:
            done = True
            break
        new = 0
        for it in items:
            u = it.get("user") or _dig(it, "modules", "module_author") or {}
            uid = str(u.get("mid") or "")
            if uid and uid not in users:
                users[uid] = u.get("name") or f"UID{uid}"
                new += 1
        offset = str(data.get("offset") or "")
        if new == 0 or not data.get("has_more") or not offset:
            done = True
            break
        info(f"已获取转发第 {i + 1} 页 (累计 {len(users)} 人)")
        client.polite()
    if not done:
        warn(f"转发名单已达 --max-pages={max_pages} 上限, 可能未取全。")
    return users


def get_likes(client, dyn_id, max_pages):
    """新版接口: x/polymer/web-dynamic/v1/detail/reaction"""
    users, offset, done = {}, "", False
    info("获取点赞名单 ...")
    for i in range(max_pages):
        r = client.get_api("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail/reaction",
                           {"id": dyn_id, "offset": offset, "web_location": "333.1369"})
        if not r or r.get("code") != 0:
            show_api_error(r, "获取点赞名单")
            if i == 0:
                client.fetch_error = True
            return users
        data = r.get("data") or {}
        items = data.get("items") or []
        if not items:
            done = True
            break
        new = 0
        for it in items:
            if "转发" in str(it.get("action") or ""):  # 该列表可能混有转发记录, 只要点赞
                continue
            uid = str(it.get("mid") or "")
            if uid and uid not in users:
                users[uid] = it.get("name") or f"UID{uid}"
                new += 1
        offset = str(data.get("offset") or "")
        if not data.get("has_more") or not offset:
            done = True
            break
        info(f"已获取点赞第 {i + 1} 页 (累计 {len(users)} 人)")
        client.polite()
    if not done:
        warn(f"点赞名单已达 --max-pages={max_pages} 上限, 可能未取全。")
    return users


def test_follows(client, uid, up_uid):
    """返回 (结果, 说明); 结果: True 已关注 / False 未关注 / None 无法验证

    方式1 (最准确): 以 UP 主身份登录, 直接查询 "对方是否关注了我", 不受对方隐私设置影响。
    方式2: 已登录但不是 UP 主, 翻对方的关注列表 (对方隐藏列表则无法验证)。
    未登录: 关注相关接口基本都要求登录, 无法验证。
    """
    why = ""
    if client.me_mid and client.me_mid == up_uid:
        url = "https://api.bilibili.com/x/space/wbi/acc/relation"
        key = client.get_mixin()
        r = client.get(url + "?" + wbi_query({"mid": uid}, key)) if key else client.get(url, {"mid": uid})
        client.polite()
        if r and r.get("code") == 0:
            attr = _dig(r, "data", "be_relation", "attribute") or 0
            return attr in (2, 6), f"UP主身份查询, 对方关系属性={attr} (2关注/6互关)"
        why = f"UP主身份查询失败(code={None if not r else r.get('code')}), 改用关注列表; "

    if not client.me_mid:
        return None, why + "未登录: 请导入 cookie (推荐导入发布者账号的 cookie)"

    for p in range(1, 6):
        r = client.get("https://api.bilibili.com/x/relation/followings",
                       {"vmid": uid, "pn": p, "ps": 50, "order_type": "attention"})
        client.polite()
        if not r:
            return None, why + "请求失败"
        if r.get("code") != 0:
            hint = "对方隐藏了关注列表" if r.get("code") in (22115, 22118) else r.get("message")
            return None, why + f"code={r.get('code')} {hint}"
        data = r.get("data") or {}
        lst = data.get("list") or []
        if any(str(f.get("mid")) == up_uid for f in lst):
            return True, why + "在对方关注列表中找到发布者"
        if not lst or p * 50 >= int(data.get("total") or 0):
            return False, why + "对方关注列表中没有发布者"
    return None, why + "对方关注数超过 250, 非本人无法翻页查看更多"


# ------------------------------------------------------------------ 抽奖核心 (纯函数, 便于测试)
def build_pool(sets, mode):
    """sets: {条件: {uid: name}} -> 候选 uid 列表"""
    pool = None
    for users in sets.values():
        keys = set(users)
        if pool is None:
            pool = keys
        elif mode == "all":
            pool &= keys
        else:
            pool |= keys
    return sorted(pool or [])


def draw(pool, count, seed, follow_check=None, on_unverifiable="skip"):
    """随机打乱 pool (由 seed 决定顺序) 后依次校验, 返回 (winners, rejected_uids)"""
    order = sorted(pool)
    random.Random(seed).shuffle(order)
    winners, rejected = [], []
    for uid in order:
        if len(winners) >= count:
            break
        remark = ""
        if follow_check:
            res = follow_check(uid)
            if res is False or (res is None and on_unverifiable == "skip"):
                rejected.append(uid)
                continue
            if res is None:
                remark = "关注状态无法验证, 请人工核实"
        winners.append({"uid": uid, "remark": remark})
    return winners, rejected


# ------------------------------------------------------------------ 主流程
def collect(client, target, conds, max_pages, notes):
    sets = {}
    if "comment" in conds:
        sets["comment"] = get_comments(client, target, max_pages)
    if "repost" in conds:
        if target["dynamic_id"]:
            sets["repost"] = get_reposts(client, target["dynamic_id"], max_pages)
        else:
            notes.append("视频没有公开的转发名单, [转发] 条件已忽略, 请人工核实")
    if "like" in conds:
        if target["dynamic_id"]:
            sets["like"] = get_likes(client, target["dynamic_id"], max_pages)
        else:
            notes.append("视频没有公开的点赞名单, [点赞] 条件已忽略, 请人工核实")
    return sets


def parse_args():
    p = argparse.ArgumentParser(description="Bilibili 抽奖脚本", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("示例")[-1])
    p.add_argument("url", nargs="?", help="抽奖地址/ID (BV号/av号/视频或动态链接/动态ID/b23.tv短链)")
    p.add_argument("-t", "--type", choices=["auto", "video", "dynamic"], default="auto",
                   help="目标类型, 默认 auto (运行时询问)")
    p.add_argument("-c", "--conditions", nargs="+", choices=list(COND_NAMES),
                   help="抽奖条件, 可多选; 不传则运行时询问")
    p.add_argument("--mode", choices=["all", "any"], default=None,
                   help="all=满足全部(默认) any=满足任一 (关注始终为必须过滤条件)")
    p.add_argument("-n", "--winners", type=int, default=None, help="中奖人数 (不传则在抽奖前询问, 默认 1)")
    p.add_argument("--max-pages", type=int, default=10, help="每类名单最多抓取页数, 默认 10")
    p.add_argument("--exclude", nargs="*", default=[], help="额外排除的 UID")
    p.add_argument("--cookie-file", help="cookie txt 路径 (不传则运行时询问是否导入)")
    p.add_argument("--on-unverifiable", choices=["skip", "pass"], default="skip",
                   help="关注状态无法验证时: skip=淘汰(默认) pass=放行并标注")
    p.add_argument("--seed", help="随机种子 (相同名单+相同种子可复现结果)")
    p.add_argument("--export", help="结果 JSON 保存路径 (默认自动命名)")
    p.add_argument("--no-save", action="store_true", help="不保存结果文件")
    p.add_argument("--no-prompt", action="store_true", help="不进行交互询问 (需配合 -t)")
    p.add_argument("--debug", action="store_true", help="打印每个请求的地址和返回内容前 300 字符 (排错用)")
    p.add_argument("--no-pause", action="store_true", help="结束后不等待按回车 (默认交互模式下会等待)")
    return p.parse_args()


def main():
    a = parse_args()
    client = Client()
    client.debug = a.debug

    # 1) 开始前: 是否导入 cookie
    if a.cookie_file:
        import_cookie_file(client, a.cookie_file)
    elif not a.no_prompt:
        if ask("是否导入 cookie txt 文件? (可避免风控/获取失败) [y/N]: ").lower() in ("y", "yes", "是"):
            f = select_cookie_file()
            if f:
                import_cookie_file(client, f)
            else:
                info("已取消导入。")
    client.add_buvid()

    # 2) 抽奖地址 (命令行变量)
    url = a.url
    if not url:
        if a.no_prompt:
            err("请提供抽奖地址。")
            return 1
        url = ask("请输入抽奖地址 (BV号/av号/视频或动态链接/动态ID/b23.tv短链): ")
    if not url:
        err("未提供地址。")
        return 1
    parsed = parse_target(url, client)

    # 3) 抽奖前询问: 视频 or 动态
    kind = {"video": "Video", "dynamic": "Dynamic"}.get(a.type)
    if not kind:
        default = parsed[0] if parsed[0] in ("Video", "Dynamic") else ""
        if a.no_prompt:
            kind = default
        else:
            hint = {"Video": "检测到: 视频, 直接回车确认", "Dynamic": "检测到: 动态, 直接回车确认"}.get(default, "无法自动判断, 请选择")
            ans = ask(f"抽奖对象是? [1] 视频  [2] 动态  ({hint}): ")
            kind = {"1": "Video", "2": "Dynamic"}.get(ans, default)
        if not kind:
            err("未能确定是视频还是动态, 请使用 -t video|dynamic 指定。")
            return 1

    target = resolve_target(client, parsed, kind)
    if not target:
        return 1

    line()
    ok(f"类型: {target['kind']}    标题: {target['title']}")
    info(f"发布者: {target['upper_name']} (UID: {target['upper_uid']})")
    info(f"oid(评论区): {target['oid']}    评论区type: {target['comment_type']}    "
         f"cid: {target['cid']}    动态ID: {target['dynamic_id']}")
    line()

    # 4) 抽奖条件
    conds = a.conditions
    mode = a.mode
    if not conds:
        if a.no_prompt:
            conds = ["comment"]
        else:
            keys = list(COND_NAMES)
            print("请选择抽奖条件 (可多选, 逗号分隔): 1 评论  2 转发  3 点赞  4 关注")
            ans = ask("例如 1,2,4 ; 直接回车 = 仅评论: ")
            conds = [keys[int(x) - 1] for x in re.split(r"[,，\s]+", ans) if x in ("1", "2", "3", "4")] or ["comment"]
    conds = list(dict.fromkeys(conds))
    if mode is None:
        mode = "all"
        if not a.no_prompt and not a.conditions and len([c for c in conds if c != "follow"]) > 1:
            if ask("多个条件: [1] 必须全部满足 (默认)  [2] 满足任一: ") == "2":
                mode = "any"
    info(f"抽奖条件: {' + '.join(COND_NAMES[c] for c in conds)}    组合方式: {'全部满足' if mode == 'all' else '满足任一'}")

    # 5) 采集名单 (失败时可导入 cookie 后重试)
    notes = []
    sets = collect(client, target, conds, a.max_pages, notes)
    if client.fetch_error and not any(sets.values()) and not a.no_prompt:
        if ask("获取失败, 是否现在导入 cookie txt 并重试? [y/N]: ").lower() in ("y", "yes", "是"):
            f = select_cookie_file()
            if f and import_cookie_file(client, f):
                client.fetch_error = False
                notes.clear()
                sets = collect(client, target, conds, a.max_pages, notes)

    if not sets and "comment" not in conds:
        notes.append("所选条件没有可用的公开名单, 已改用评论者作为候选池")
        sets["comment"] = get_comments(client, target, a.max_pages)

    names = {}
    for users in sets.values():
        for uid, nm in users.items():
            names.setdefault(uid, nm)

    exclude = {str(u) for u in a.exclude}
    exclude.add(target["upper_uid"] or client.comment_upper or "")
    info(f"已自动排除发布者 UID: {target['upper_uid']}")

    def show_sets():
        for k, users in sets.items():
            detail = ""
            if 0 < len(users) <= 5:
                detail = "  -> " + ", ".join(f"{n}({u})" for u, n in users.items())
            info(f"{COND_NAMES[k]}名单: {len(users)} 人{detail}")

    line()
    show_sets()
    pool_all = build_pool(sets, mode)
    pool = [u for u in pool_all if u not in exclude]

    # 候选池为空, 且有名单是空的 (多半是接口没返回数据) -> 询问是否忽略这些条件
    empty = [k for k, v in sets.items() if not v]
    if not pool and empty and len(empty) < len(sets):
        names_txt = "、".join(COND_NAMES[k] for k in empty)
        warn(f"{names_txt} 名单为空, 可能是接口没有返回数据 (未登录/风控), 也可能确实没人满足。")
        ignore = False
        if not a.no_prompt:
            ignore = ask(f"是否忽略这些条件, 用其余名单继续抽奖 (中奖后请人工核实)? [Y/n]: ", "y").lower() in ("y", "yes", "是")
        if ignore:
            for k in empty:
                del sets[k]
                notes.append(f"[{COND_NAMES[k]}] 名单为空, 条件已忽略, 请人工核实中奖者是否满足")
            pool_all = build_pool(sets, mode)
            pool = [u for u in pool_all if u not in exclude]
    elif empty:
        warn("名单为空: " + "、".join(COND_NAMES[k] for k in empty))

    excluded_n = len(pool_all) - len(pool)

    line()
    ok(f"统计完成: 候选池 {len(pool)} 人, 排除 {excluded_n} 人。")
    for n in notes:
        warn(n)
    if not pool:
        err("没有找到符合条件的参与者, 抽奖结束。")
        if pool_all and excluded_n:
            warn(f"满足条件的 {excluded_n} 人都被排除了 (发布者/--exclude)。")
        elif mode == "all" and len(sets) > 1:
            warn("多个名单的交集为空: 请对照上面各名单的人数/UID, 看是哪一项没有包含你的测试账号。")
            warn("可加 --debug 查看接口原始返回, 或改用 --mode any。")
        if client.fetch_error:
            warn("名单获取过程中出现错误, 建议导入 cookie 后重试。")
        return 1

    # 选了关注但没登录: 给一次现在导入的机会
    if "follow" in conds and not client.me_mid and not a.no_prompt:
        if ask("你选了 [关注] 条件, 但当前未登录, 是否现在导入 cookie txt? (推荐用发布者账号) [y/N]: ").lower() in ("y", "yes", "是"):
            f = select_cookie_file()
            if f:
                import_cookie_file(client, f)

    # 6) 抽奖
    seed = a.seed or secrets.token_hex(8)
    need_follow = "follow" in conds
    want = a.winners
    if want is None:
        want = 1
        if not a.no_prompt:
            while True:
                v = ask(f"候选池共 {len(pool)} 人, 本次抽取几名获奖者? [默认 1]: ", "1")
                if v.isdigit() and int(v) >= 1:
                    want = int(v)
                    break
                warn("请输入大于等于 1 的整数。")
    if want < 1:
        err("中奖人数必须大于等于 1。")
        return 1
    if want > len(pool):
        warn(f"设定 {want} 人, 但候选池只有 {len(pool)} 人, 将最多抽取 {len(pool)} 人。")
    count = min(want, len(pool))
    info(f"随机种子: {seed}  (相同候选名单 + 相同种子可复现结果)")
    info(f"正在抽取 {count} 名幸运儿...")
    if need_follow:
        if client.me_mid == target["upper_uid"]:
            ok("已用发布者账号登录, 关注校验将直接查询 '对方是否关注了你' (最准确)。")
        elif client.me_mid:
            warn("当前登录的不是发布者账号, 关注校验只能翻对方的关注列表, 对方隐藏时无法验证。")
        else:
            warn("当前未登录, 关注校验基本无法完成。建议导入发布者账号的 cookie (或加 --on-unverifiable pass 放行并人工核实)。")
        info("正在逐个校验关注状态 (较慢, 请耐心等待)...")
    line()
    follow_fn = None
    if need_follow:
        def follow_fn(uid):
            res, why = test_follows(client, uid, target["upper_uid"])
            tag = {True: "已关注", False: "未关注", None: "无法验证"}[res]
            info(f"关注校验 {names.get(uid, '')}({uid}): {tag} - {why}")
            return res
    winners, rejected = draw(pool, count, seed, follow_fn, a.on_unverifiable)

    if not winners:
        err("没有任何人通过全部条件校验。")
        return 1
    if len(winners) < count:
        warn(f"合格人数不足, 仅抽出 {len(winners)} 人 (未通过关注校验: {len(rejected)} 人)。")

    rows = [[i, names.get(w["uid"], ""), w["uid"], f"https://space.bilibili.com/{w['uid']}", w["remark"]]
            for i, w in enumerate(winners, 1)]
    print(C["green"] + C["bold"] + "中奖名单" + C["end"])
    print_table(["#", "用户名", "UID", "主页", "备注"], rows)
    line()

    # 7) 保存结果
    if not a.no_save:
        path = a.export or f"lottery_{target['oid']}_{datetime.now():%Y%m%d_%H%M%S}.json"
        result = {
            "time": datetime.now().isoformat(timespec="seconds"), "input": url, "target": target,
            "conditions": conds, "mode": mode, "seed": seed, "winner_count": want,
            "pool": [{"uid": u, "name": names.get(u, "")} for u in pool],
            "winners": [{"uid": w["uid"], "name": names.get(w["uid"], ""), "remark": w["remark"]} for w in winners],
            "rejected_uids": rejected, "notes": notes,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        ok(f"结果已保存: {os.path.abspath(path)}")
    return 0


def pause_if_needed():
    if "--no-prompt" in sys.argv or "--no-pause" in sys.argv:
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车键退出...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    code = 0
    try:
        code = main()
    except KeyboardInterrupt:
        print()
        warn("已取消。")
        code = 130
    except SystemExit as e:  # argparse 报错/--help
        code = e.code if isinstance(e.code, int) else 1
    except Exception:
        import traceback
        err("程序出现异常, 详细信息如下:")
        traceback.print_exc()
        code = 1
    pause_if_needed()
    sys.exit(code)
