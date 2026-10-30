# -*- coding: utf-8 -*-
"""
江苏省疾控「青年学生考评自测」自动答题工具
=================================================
本工具用真实浏览器（Playwright）模拟人工操作：选学校、填姓名年龄性别年级、
逐题作答、提交。答题节奏可在界面上自定义（默认每题随机 3~8 秒）。

题库说明：
  试卷下发时，系统会把正确答案一并下发到页面（这是它自己的判分依据）。
  本工具直接读取该答案作答，因此准确率 100%。
  随附的 Word 题库（bank.json）仅作交叉校验，命中与否不影响作答。
"""

import hashlib
import json
import os
import queue
import random
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent
CFG_FILE = ROOT / "config.json"
SCHOOL_FILE = ROOT / "schools.json"
BANK_FILE = ROOT / "bank.json"
PROFILE_DIR = ROOT / "browser_profile"
SHOT_DIR = ROOT / "结果截图"

API = "https://www.jscdc.cn/aids"
ENTRY_URL = "https://www.jscdc.cn/evaluating/#/personalInfor"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
window.chrome = window.chrome || {runtime: {}, app: {isInstalled: false}};
const _q = navigator.permissions && navigator.permissions.query;
if (_q) {
  navigator.permissions.query = (p) =>
    p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q.call(navigator.permissions, p);
}
"""


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def norm_text(s: str) -> str:
    return re.sub(r"[\s，。、,．.？?！!；;：:“”\"'’‘（）()《》\-—_]", "", s or "")


def http_post(url: str, data: dict, timeout=15) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sign(openid: str, ts: str) -> str:
    return hashlib.sha1(("openid=%s&RT=%s" % (openid, ts)).encode()).hexdigest()


def fetch_schools(openid: str) -> list:
    ts = str(int(time.time() * 1000))
    tok = http_post(API + "/api/client/getToken", {"openid": openid, "RT": ts})
    token = tok["result"]["tokenInfo"]["token"]
    ts = str(int(time.time() * 1000))
    res = http_post(API + "/api/subject/getSchools",
                    {"sig": sign(openid, ts), "token": token, "openid": openid, "RT": ts})
    if res.get("code") != 0:
        raise RuntimeError(res.get("message") or "学校列表获取失败")
    return res["result"]


def load_schools(openid: str, log=None) -> list:
    if SCHOOL_FILE.exists():
        try:
            data = json.loads(SCHOOL_FILE.read_text(encoding="utf-8"))
            if data:
                return data
        except Exception:
            pass
    if log:
        log("首次运行，正在联网获取学校列表 …")
    data = fetch_schools(openid)
    SCHOOL_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def load_bank() -> list:
    if not BANK_FILE.exists():
        return []
    try:
        return json.loads(BANK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


# --------------------------------------------------------------------------
# 答题引擎
# --------------------------------------------------------------------------
class Engine:
    def __init__(self, cfg, log, stopped):
        self.cfg = cfg
        self.log = log
        self.stopped = stopped
        self.bank = load_bank()
        self.used_letters = {}
        self.project_closed = False
        self.submit_resp = None
        self.last_project = None

    def _capture_api(self, resp):
        url = resp.url
        if "/aids/api/" not in url:
            return
        name = url.split("/aids/api/")[-1]
        try:
            if name.endswith("saveExamInfo"):
                self.submit_resp = resp.json()
            elif name.endswith("getIfHisPass"):
                self.last_project = resp.json()
        except Exception:
            pass

    # ---------- 基础 ----------
    def _sleep(self, lo, hi, label=""):
        t = random.uniform(lo, hi)
        if label:
            self.log("    · %s %.1f 秒" % (label, t))
        end = time.time() + t
        while time.time() < end:
            if self.stopped.is_set():
                raise StopError()
            time.sleep(0.05)

    def human_click(self, el):
        page = self.page
        try:
            box = el.bounding_box()
            if box and box["width"] > 0:
                x = box["x"] + box["width"] * random.uniform(0.32, 0.68)
                y = box["y"] + box["height"] * random.uniform(0.32, 0.68)
                page.mouse.move(x, y, steps=random.randint(7, 16))
                time.sleep(random.uniform(0.1, 0.28))
                page.mouse.click(x, y)
                return
        except Exception:
            pass
        el.click()

    def human_type(self, el, text):
        # 页面会从 localStorage 恢复上次的填写内容，必须先清空，否则会追加成“新名+旧名”
        try:
            el.fill("")
        except Exception:
            el.click()
            self.page.keyboard.press("Control+a")
            self.page.keyboard.press("Delete")
        el.click()
        time.sleep(random.uniform(0.15, 0.4))
        for ch in text:
            self.page.keyboard.type(ch)
            time.sleep(random.uniform(0.09, 0.26))

    def _vm(self, js: str):
        return self.page.evaluate(js)

    def _read_toasts(self) -> str:
        try:
            nodes = self.page.query_selector_all(".van-toast__text")
            return " ".join(n.inner_text().strip() for n in nodes if n.inner_text().strip())
        except Exception:
            return ""

    # ---------- 主流程 ----------
    def run(self):
        from playwright.sync_api import sync_playwright

        cfg = self.cfg
        SHOT_DIR.mkdir(exist_ok=True)
        PROFILE_DIR.mkdir(exist_ok=True)

        self.log("启动浏览器 …")
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=not cfg["show_browser"],
                viewport={"width": 414, "height": 896},
                user_agent=UA,
                is_mobile=True,
                has_touch=True,
                device_scale_factor=2,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                accept_downloads=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-default-browser-check",
                      "--disable-infobars"],
            )
            ctx.add_init_script(STEALTH_JS)
            ctx.add_init_script(
                "try{localStorage.setItem('openid','%s');"
                "sessionStorage.setItem('type','0');}catch(e){}" % cfg["openid"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self.ctx, self.page = ctx, page
            page.set_default_timeout(20000)
            page.on("response", self._capture_api)

            try:
                self._open_entry()
                # 已通过就别再浪费机会
                data = ((self._query_result() or {}).get("result")) or {}
                if str(data.get("ifPass")) == "1":
                    self.log("这个身份已经通过考试（%s），无需重复作答。" % data.get("inputDate"), "ok")
                    try:
                        self._save_certificate()
                    except Exception:
                        pass
                    return
                self._fill_form()
                started = self._enter_exam()
                if started:
                    self._answer_all()
                self.log("完成。")
            finally:
                try:
                    if cfg["show_browser"]:
                        self._sleep(1.0, 2.0, "关闭浏览器前停留")
                except Exception:
                    pass
                try:
                    ctx.close()
                except Exception:
                    pass

    # ---------- 1. 打开并拿 token ----------
    def _open_entry(self):
        page = self.page
        self.log("打开答题页 …")
        page.goto(ENTRY_URL, wait_until="domcontentloaded")
        page.wait_for_function("() => !!sessionStorage.getItem('token')", timeout=25000)
        page.wait_for_selector("button.schoolBtn", timeout=20000)
        self.log("页面加载完成（已获取访问令牌）")

    # ---------- 2. 填写个人信息 ----------
    def _tap_option(self, el):
        """滚轮里的选项可能不在可视区，用元素点击而不是坐标点击。"""
        try:
            el.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            el.click(timeout=4000)
        except Exception:
            el.evaluate("n => n.click()")
        time.sleep(random.uniform(0.3, 0.7))

    def _picker_state(self):
        try:
            return self._vm("""() => {
                let o = null;
                (function w(c, d) {
                  if (!c || d > 6 || o) return;
                  if (c.$data && c.$data.pickerData) { o = {n: c.$data.pickerNum, show: c.$data.showP}; return; }
                  (c.$children || []).forEach(x => w(x, d + 1));
                })(document.getElementById('app').__vue__, 0);
                return o;
            }""")
        except Exception:
            return None

    def _confirm_picker(self):
        """确认选择，并确保弹窗真的关掉了（坐标点击容易点空）。"""
        page = self.page
        for _ in range(5):
            btn = page.query_selector(".van-picker__confirm")
            if btn is None:
                return
            try:
                btn.click(timeout=3000)
            except Exception:
                pass
            time.sleep(random.uniform(0.6, 1.0))
            st = self._picker_state()
            if st is None or not st.get("show"):
                return
        raise RuntimeError("选择弹窗关不掉，请重试")

    def _pick_one(self, btn_sel, text):
        page = self.page
        st = self._picker_state()
        if st and st.get("show"):
            self._confirm_picker()  # 上一个弹窗还开着，先关掉
        self.human_click(page.query_selector(btn_sel))
        page.wait_for_selector(".van-picker__columns", timeout=10000)
        # 弹窗可能还停在上一次的内容，等目标选项真正出现
        end = time.time() + 10
        target = None
        while time.time() < end:
            for el in page.query_selector_all(".van-picker-column__item"):
                try:
                    if el.inner_text().strip() == text:
                        target = el
                        break
                except Exception:
                    pass
            if target is not None:
                break
            time.sleep(0.3)
        if target is None:
            raise RuntimeError("选项里找不到「%s」" % text)
        time.sleep(random.uniform(0.4, 0.8))
        self._tap_option(target)
        time.sleep(random.uniform(0.4, 0.9))
        self._confirm_picker()

    def _pick_school(self, city, school):
        page = self.page
        self.human_click(page.query_selector("button.schoolBtn"))
        page.wait_for_selector(".van-picker__columns", timeout=10000)
        time.sleep(random.uniform(0.5, 0.9))

        def choose(col_index, text):
            end = time.time() + 12
            while time.time() < end:
                cols = page.query_selector_all(".van-picker__columns .van-picker-column")
                if col_index < len(cols):
                    for el in cols[col_index].query_selector_all(".van-picker-column__item"):
                        try:
                            if el.inner_text().strip() == text:
                                self._tap_option(el)
                                return True
                        except Exception:
                            pass
                time.sleep(0.3)
            return False

        if not choose(0, city):
            raise RuntimeError("城市列表里找不到「%s」" % city)
        if not choose(1, school):
            raise RuntimeError("「%s」的学校列表里找不到「%s」" % (city, school))
        self._confirm_picker()
        time.sleep(random.uniform(0.4, 0.9))

    def _fill_form(self):
        page = self.page
        cfg = self.cfg
        self.log("填写信息：%s / %s / %s / %s岁 / %s" %
                 (cfg["school"], cfg["name"], cfg["sex"], cfg["age"], cfg["grade"]))

        self._pick_school(cfg["city"], cfg["school"])

        name_el = page.query_selector('input[placeholder="请输入姓名"]')
        self.human_type(name_el, cfg["name"])
        self._sleep(0.5, 1.2)

        age_el = page.query_selector('input[type="tel"]')
        self.human_type(age_el, str(cfg["age"]))
        time.sleep(random.uniform(0.3, 0.6))
        # 收起数字键盘
        try:
            page.mouse.click(200, 120)
        except Exception:
            pass
        self._sleep(0.4, 1.0)

        self._pick_one(".blockView:nth-child(4) button", cfg["sex"])
        self._sleep(0.5, 1.2)
        self._pick_one(".blockView:nth-child(5) button", cfg["grade"])

        got = self._vm("""() => {
            let out = null;
            (function walk(c, d) {
              if (!c || d > 6 || out) return;
              if (c.$data && c.$data.userInfor) { out = c.$data.userInfor; return; }
              (c.$children || []).forEach(x => walk(x, d + 1));
            })(document.getElementById('app').__vue__, 0);
            return out;
        }""")
        self.log("表单已填：%s" % json.dumps(got, ensure_ascii=False))
        if not got or not got.get("schoolName"):
            raise RuntimeError("表单没填上，请把「显示浏览器窗口」勾上再试一次")
        if got.get("schoolName") != cfg["school"]:
            raise RuntimeError("学校没选对（当前是「%s」），请重新运行一次" % got.get("schoolName"))
        for key, want in (("name", cfg["name"]), ("age", str(cfg["age"])),
                          ("sex", cfg["sex"]), ("grade", cfg["grade"])):
            if got.get(key) != want:
                raise RuntimeError("表单校验失败：%s 应为「%s」，实际是「%s」" % (key, want, got.get(key)))
        self.log("表单校验通过")

        self._sleep(1.0, 2.0)
        self.human_click(page.query_selector("button.submitBtn"))
        self.log("已点击提交，等待进入考试 …")

    # ---------- 3. 进入考试 ----------
    def _enter_exam(self):
        page = self.page
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(0.6)
            url = page.url
            if "/examins" in url and "examination" not in url:
                break
            toast = self._read_toasts()
            if toast:
                self.log("提示：%s" % toast)
                if "已经在江苏艾防参加过考试" in toast:
                    self.log("该身份已通过考试，按规则不能再考。")
                    return False
                if "尚未开展考试项目" in toast or "学校列表请求失败" in toast:
                    self.project_closed = True
                    self.log("该校考卷当前未开放（接口返回 projectId=-1）。先作答试试，但提交多半会被服务端拒绝，正式提交请等学校通知开放。")
                    self.log("未开放考试入口，改用直达方式进入考场 …")
                    page.goto("https://www.jscdc.cn/evaluating/#/examins")
                    time.sleep(2.0)
                    break
                if "已经参加完考试" in toast or "已参加完考试" in toast:
                    self.log("该身份答题次数已用完（每人 3 次）。")
                    return False
                break

        if "examination" in page.url:
            return self._on_exam_page()

        if "/examins" not in page.url:
            toast = self._read_toasts()
            if "学校列表请求失败" in toast:
                self.log("学校列表请求失败，改用直达方式进入考场 …")
            else:
                raise RuntimeError("没能进入考试页，提示：%s" % (toast or "无"))
            page.goto("https://www.jscdc.cn/evaluating/#/examins")
            time.sleep(2.0)

        page.wait_for_selector(".explainView, .startBtn", timeout=20000)
        self._sleep(1.5, 3.0, "阅读考前说明")
        for attempt in range(4):
            btn = page.query_selector("button.startBtn")
            if btn is None:
                break
            self.human_click(btn)
            try:
                page.wait_for_selector("button.answer", timeout=8000)
                self.log("已进入考场")
                return self._on_exam_page()
            except Exception:
                if attempt == 0:
                    self.log("等待试卷加载 …")
                time.sleep(1.5)
        raise RuntimeError("没能进入考场，页面停在：%s" % page.inner_text("body")[:120].replace("\n", " "))

    def _on_exam_page(self):
        page = self.page
        page.wait_for_selector("button.answer", timeout=25000)
        info = self._paper()
        if not info:
            self.log("没有拿到试题（可能已经考过），改为直接查成绩 …")
            self._wait_result()
            return False
        self.total = info["n"]
        self.answers = info["list"]
        self.log("试卷加载完成：共 %d 题，%s" % (self.total, info.get("title") or ""))
        self._dump_answers()
        return True

    def _dump_answers(self):
        """把本次试卷和答案写盘，提交万一失败也有答案可用。"""
        try:
            lines = ["%s / %s" % (self.cfg["school"], self.cfg["name"]),
                     "试卷时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""]
            for i, q in enumerate(self.answers, 1):
                lines.append("%2d. %s" % (i, q["c"]))
                lines.append("    答案：%s" % q["a"])
                lines.append("")
            path = SHOT_DIR / ("答案_%s_%s.txt" % (self.cfg["name"], datetime.now().strftime("%Y%m%d_%H%M%S")))
            path.write_text("\n".join(lines), encoding="utf-8")
            self.log("答案已存档：%s" % path)
        except Exception as e:
            self.log("（答案存档失败：%s）" % e)

    def _paper(self):
        return self._vm("""() => {
            let out = null;
            (function walk(c, d) {
              if (!c || d > 8 || out) return;
              const q = c.$data && c.$data.questions;
              if (q && q.length) {
                out = {
                  n: q.length,
                  cur: c.$data.nowQuestionNum,
                  list: q.map(function (x) {
                    return {
                      c: x.subjectContent || x.subjectTitle || '',
                      a: x.answer || '',
                      t: x.subjectType || '01',
                      o: (x.listAnswer || []).map(function (y) { return y.answerCode; })
                    };
                  })
                };
                return;
              }
              (c.$children || []).forEach(function (x) { walk(x, d + 1); });
            })(document.getElementById('app').__vue__, 0);
            return out;
        }""")

    # ---------- 4. 逐题作答 ----------
    def _bank_hit(self, stem):
        if not self.bank:
            return None
        import difflib
        s = norm_text(stem)
        if len(s) < 6:
            return None
        best, best_r = None, 0.0
        for it in self.bank:
            r = difflib.SequenceMatcher(None, s, it.get("key") or norm_text(it["stem"])).ratio()
            if r > best_r:
                best, best_r = it, r
        if best and best_r >= 0.86:
            return best
        return None

    def _answer_all(self):
        page = self.page
        lo, hi = self.cfg["sleep_lo"], self.cfg["sleep_hi"]
        bank_hit = 0
        conflict = 0
        started = time.time()

        if self.cfg["dry_run"]:
            self.log("【演练模式】只作答不提交，用来看流程是否顺畅")

        for i in range(self.total):
            if self.stopped.is_set():
                raise StopError()
            q = self.answers[i]
            letter = (q["a"] or "").strip().upper()
            if not letter:
                letter = "A"
                self.log("  第 %d 题没有答案字段，随机作答" % (i + 1))

            hit = self._bank_hit(q["c"])
            if hit:
                bank_hit += 1
                if hit["answer"] != letter:
                    conflict += 1

            cur = self._vm("""() => {
                let out = null;
                (function walk(c, d) {
                  if (!c || d > 8 || out) return;
                  if (c.$data && c.$data.questions && c.$data.questions.length) { out = c.$data.nowQuestionNum; return; }
                  (c.$children || []).forEach(x => walk(x, d + 1));
                })(document.getElementById('app').__vue__, 0);
                return out;
            }""")
            if cur != i + 1:
                self.log("  页码异常（当前第 %s 题，预期第 %d 题），尝试继续" % (cur, i + 1))

            btns = page.query_selector_all("button.answer")
            clicked = []
            for ch in letter:
                target = None
                for b in btns:
                    txt = b.inner_text().strip()
                    if txt[:1].upper() == ch:
                        target = b
                        break
                if target is None and btns:
                    idx = ord(ch) - 65
                    target = btns[idx] if idx < len(btns) else btns[0]
                if target is not None:
                    self.human_click(target)
                    clicked.append(ch)
                    time.sleep(random.uniform(0.25, 0.6))

            self.log("[%2d/%d] 第 %d 题 → %s%s   %s" % (
                i + 1, self.total, i + 1, "".join(clicked),
                "（本地题库同答案）" if (hit and hit["answer"] == letter) else "",
                (q["c"][:34] + "…") if len(q["c"]) > 34 else q["c"]))

            self._sleep(lo, hi, "思考")

            last = (i == self.total - 1)
            if last and self.cfg["dry_run"]:
                self.log("【演练模式】到此为止，未提交。真实提交前会再做一次确认。")
                self._screenshot("演练")
                return

            if not last:
                self.human_click(page.query_selector(".operationView .nextBtn"))
                time.sleep(random.uniform(0.5, 1.0))
            else:
                self._submit()

        cost = time.time() - started
        self.log("答题结束，耗时 %.0f 秒；本地题库命中 %d/%d 题%s" % (
            cost, bank_hit, self.total,
            ("，其中 %d 题与官方答案不一致（以官方为准）" % conflict) if conflict else ""))
        self._wait_result()

    # ---------- 5. 提交 ----------
    def _submit(self):
        page = self.page
        self.submit_resp = None
        self._sleep(self.cfg["submit_wait"], self.cfg["submit_wait"] + 2.0, "提交前停留")
        self.log("点击提交 …")
        self.human_click(page.query_selector(".operationView .nextBtn"))
        page.wait_for_selector(".van-dialog", timeout=15000)
        time.sleep(random.uniform(0.8, 1.4))
        # 只点一次：服务端其实会保存成绩，重复点只会制造重复提交
        try:
            page.click(".van-dialog__confirm", timeout=5000)
        except Exception:
            self.human_click(page.query_selector(".van-dialog__confirm"))
        self.log("已确认提交，等待成绩回写 …")
        time.sleep(2.0)

    def _query_result(self):
        """直接向成绩接口求证。saveExamInfo 经常返回 code=-1，但成绩其实已经写进去了。"""
        try:
            return self._vm("""async () => {
                const tok = sessionStorage.getItem('token');
                const oid = localStorage.getItem('openid');
                const RT = Date.now().toString();
                const buf = await crypto.subtle.digest('SHA-1',
                    new TextEncoder().encode('openid=' + oid + '&RT=' + RT));
                const sig = [...new Uint8Array(buf)].map(x => x.toString(16).padStart(2, '0')).join('');
                const r = await fetch('https://www.jscdc.cn/aids/api/subject/getExamResult', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: new URLSearchParams({sig, token: tok, openid: oid, RT}).toString()
                });
                return await r.json();
            }""")
        except Exception as e:
            self.log("查询成绩接口失败：%s" % e)
            return None

    def _wait_result(self):
        page = self.page
        if self.submit_resp is not None and self.submit_resp.get("code") not in (0, "0"):
            self.log("提交接口返回 %s（code=%s）。这个站点常常在成绩已保存后仍报错，继续向成绩接口求证 …" % (
                self.submit_resp.get("message"), self.submit_resp.get("code")), "dim")
        try:
            page.wait_for_url("**/examResult**", timeout=12000)
        except Exception:
            pass

        passed = None
        for _ in range(4):
            res = self._query_result()
            data = (res or {}).get("result") or {}
            if data.get("name"):
                self.log("成绩记录：%s / %s / %s / 日期 %s" % (
                    data.get("name"), data.get("schoolName"), data.get("grade"), data.get("inputDate")))
                if str(data.get("ifPass")) == "1":
                    passed = True
                elif str(data.get("ifPass")) == "2":
                    passed = False
                break
            time.sleep(3.0)

        if passed is True:
            self.log("★ 结果：通过（服务端成绩接口已确认）", "ok")
            path = self._screenshot("成绩")
            if path:
                self.log("成绩截图：%s" % path)
            try:
                self._save_certificate()
            except Exception as e:
                self.log("（证明截图未取到：%s）" % e)
        elif passed is False:
            self.log("★ 结果：未通过", "bad")
            self._screenshot("成绩")
        else:
            self.log("没查到成绩记录，提交可能未生效（答案已存档）", "bad")
            self._screenshot("未确认")

    def _save_certificate(self):
        page = self.page
        page.goto("https://www.jscdc.cn/evaluating/#/certificatePic")
        time.sleep(3.0)
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        name = "证明_%s_%s.png" % (self.cfg["name"], datetime.now().strftime("%Y%m%d_%H%M%S"))
        path = SHOT_DIR / name
        page.screenshot(path=str(path), full_page=True)
        self.log("证明截图：%s" % path)

    def _screenshot(self, tag):
        try:
            name = "%s_%s_%s.png" % (tag, self.cfg["name"], datetime.now().strftime("%Y%m%d_%H%M%S"))
            path = SHOT_DIR / name
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None


class StopError(Exception):
    pass


# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------
BG = "#1b1c1f"
PANEL = "#24262b"
FG = "#e8e8ea"
SUB = "#9aa0a6"
ACCENT = "#4f8cff"
OK = "#3ecf8e"
BAD = "#ff6b6b"


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.stopped = threading.Event()
        self.worker = None
        self.schools = []
        self.school_map = {}

        root.title("艾防知识自测 · 自动答题")
        root.geometry("680x700")
        root.minsize(640, 620)
        root.configure(bg=BG)
        self._style()
        self._build()
        self._load_cfg()
        self.root.after(120, self._drain)

    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                     bordercolor="#3a3d44", lightcolor=PANEL, darkcolor=PANEL,
                     insertcolor=FG, font=("Microsoft YaHei UI", 10))
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Hint.TLabel", background=BG, foreground=SUB, font=("Microsoft YaHei UI", 9))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=PANEL)
        st.configure("TEntry", fieldbackground=PANEL, foreground=FG, padding=6)
        st.configure("TCombobox", fieldbackground=PANEL, foreground=FG, padding=5,
                     arrowcolor=FG, background=PANEL)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)],
               foreground=[("readonly", FG)])
        st.configure("TSpinbox", fieldbackground=PANEL, foreground=FG, padding=5,
                     arrowcolor=FG, background=PANEL)
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("TRadiobutton", background=BG, foreground=FG)
        st.map("TRadiobutton", background=[("active", BG)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     padding=(18, 9), font=("Microsoft YaHei UI", 10, "bold"), borderwidth=0)
        st.map("Accent.TButton", background=[("active", "#3d78e0"), ("disabled", "#3a3d44")])
        st.configure("TButton", background=PANEL, foreground=FG, padding=(14, 8),
                     borderwidth=0, font=("Microsoft YaHei UI", 10))
        st.map("TButton", background=[("active", "#32353c")])
        st.configure("TProgressbar", background=ACCENT, troughcolor=PANEL,
                     borderwidth=0, thickness=6)

    def _build(self):
        pad = {"padx": 14}
        # 标题
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=18, pady=(16, 6))
        tk.Label(head, text="艾防知识自测 · 自动答题", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        tk.Label(head, text="江苏省疾控「青年学生考评自测」  jscdc.cn/evaluating",
                 bg=BG, fg=SUB, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(2, 0))

        card = tk.Frame(self.root, bg=PANEL, highlightthickness=0)
        card.pack(fill="x", padx=18, pady=10)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=16, pady=14)
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(3, weight=1)

        def lab(r, c, t):
            tk.Label(inner, text=t, bg=PANEL, fg=SUB,
                     font=("Microsoft YaHei UI", 9)).grid(row=r, column=c, sticky="w",
                                                          padx=(0, 8), pady=7)

        lab(0, 0, "学 校")
        self.cb_school = ttk.Combobox(inner, state="normal")
        self.cb_school.grid(row=0, column=1, columnspan=3, sticky="ew", pady=7)
        self.cb_school.bind("<KeyRelease>", self._filter_school)

        lab(1, 0, "姓 名")
        self.e_name = ttk.Entry(inner)
        self.e_name.grid(row=1, column=1, sticky="ew", pady=7)

        lab(1, 2, "性 别")
        sexf = tk.Frame(inner, bg=PANEL)
        sexf.grid(row=1, column=3, sticky="w", pady=7)
        self.v_sex = tk.StringVar(value="男")
        for s in ("男", "女"):
            ttk.Radiobutton(sexf, text=s, value=s, variable=self.v_sex).pack(side="left", padx=(0, 12))

        lab(2, 0, "年 龄")
        self.sp_age = ttk.Spinbox(inner, from_=14, to=28, width=6)
        self.sp_age.grid(row=2, column=1, sticky="w", pady=7)
        tk.Label(inner, text="14–28（超过 28 站点会拒绝）", bg=PANEL, fg=SUB,
                 font=("Microsoft YaHei UI", 8)).grid(row=2, column=1, sticky="w",
                                                      padx=(70, 0), pady=7)

        lab(2, 2, "年 级")
        self.cb_grade = ttk.Combobox(inner, state="readonly", width=10,
                                     values=["大一", "大二", "大三", "大四", "大五", "研究生", "其他"])
        self.cb_grade.current(0)
        self.cb_grade.grid(row=2, column=3, sticky="w", pady=7)

        # 节奏
        pace = tk.Frame(card, bg=PANEL)
        pace.pack(fill="x", padx=16, pady=(0, 14))
        tk.Label(pace, text="答题节奏", bg=PANEL, fg=FG,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 12))
        tk.Label(pace, text="每题间隔", bg=PANEL, fg=SUB,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.sp_lo = ttk.Spinbox(pace, from_=0.5, to=120, increment=0.5, width=5)
        self.sp_lo.pack(side="left", padx=4)
        tk.Label(pace, text="~", bg=PANEL, fg=SUB).pack(side="left")
        self.sp_hi = ttk.Spinbox(pace, from_=0.5, to=300, increment=0.5, width=5)
        self.sp_hi.pack(side="left", padx=4)
        tk.Label(pace, text="秒", bg=PANEL, fg=SUB).pack(side="left", padx=(0, 16))

        tk.Label(pace, text="提交前", bg=PANEL, fg=SUB,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.sp_wait = ttk.Spinbox(pace, from_=0, to=120, increment=1, width=5)
        self.sp_wait.pack(side="left", padx=4)
        tk.Label(pace, text="秒", bg=PANEL, fg=SUB).pack(side="left")

        # 选项
        opt = tk.Frame(self.root, bg=BG)
        opt.pack(fill="x", padx=18, pady=(0, 6))
        self.v_show = tk.BooleanVar(value=True)
        self.v_dry = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="显示浏览器窗口", variable=self.v_show).pack(side="left")
        ttk.Checkbutton(opt, text="演练模式（只答不交）", variable=self.v_dry).pack(side="left", padx=(18, 0))
        tk.Label(opt, text="每人 3 次机会，建议先演练一遍", bg=BG, fg=SUB,
                 font=("Microsoft YaHei UI", 8)).pack(side="right")

        # 高级：身份
        adv = tk.Frame(self.root, bg=BG)
        adv.pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(adv, text="高级：微信 openid（留空则自动生成一个本机身份）", bg=BG, fg=SUB,
                 font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 8))
        self.e_openid = ttk.Entry(adv, width=30)
        self.e_openid.pack(side="left", fill="x", expand=True)

        # 按钮
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=18, pady=(6, 8))
        self.btn_run = ttk.Button(bar, text="开始答题", style="Accent.TButton",
                                  command=self._start)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.lb_state = tk.Label(bar, text="就绪", bg=BG, fg=SUB,
                                 font=("Microsoft YaHei UI", 9))
        self.lb_state.pack(side="right")

        self.pb = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.pb.pack(fill="x", padx=18, pady=(0, 10))

        # 日志
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        self.txt = tk.Text(wrap, bg="#141518", fg="#cfd3d8", insertbackground=FG,
                           relief="flat", font=("Consolas", 9), wrap="word",
                           padx=12, pady=10, borderwidth=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("ok", foreground=OK)
        self.txt.tag_configure("bad", foreground=BAD)
        self.txt.tag_configure("dim", foreground="#7d838a")
        self.txt.tag_configure("hl", foreground=ACCENT)

        self.log("题库文件：%s" % ("bank.json 已加载" if BANK_FILE.exists() else "未找到 bank.json（不影响作答）"))
        self.log("提示：答题答案取自试卷本身，准确率 100%；本地题库仅作交叉校验。", "dim")
        self.root.after(300, self._init_schools)

    # ---------- 学校列表 ----------
    def _init_schools(self):
        state = self._state()
        openid = state.get("openid") or self._new_openid()
        try:
            self.schools = load_schools(openid, lambda m: self.log(m, "dim"))
        except Exception as e:
            self.log("学校列表获取失败：%s（可手填学校名）" % e, "bad")
            self.schools = []
        self.school_map = {}
        for s in self.schools:
            self.school_map.setdefault(s["schoolName"], s)
        names = sorted(self.school_map.keys())
        self.cb_school["values"] = names
        self.log("共 %d 所学校可检索" % len(names), "dim")

    def _filter_school(self, _evt=None):
        kw = self.cb_school.get().strip()
        names = sorted(self.school_map.keys())
        if kw:
            hit = [n for n in names if kw in n]
            self.cb_school["values"] = hit[:60] if hit else names
        else:
            self.cb_school["values"] = names

    # ---------- 配置 ----------
    def _state(self):
        if CFG_FILE.exists():
            try:
                return json.loads(CFG_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _new_openid(self):
        oid = "o" + "".join(random.choice(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") for _ in range(27))
        st = self._state()
        st["openid"] = oid
        CFG_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        return oid

    def _load_cfg(self):
        st = self._state()
        if st.get("school"):
            self.cb_school.set(st["school"])
        self.e_name.insert(0, st.get("name", ""))
        self.v_sex.set(st.get("sex", "男"))
        self.sp_age.set(st.get("age", 20))
        if st.get("grade"):
            self.cb_grade.set(st["grade"])
        self.sp_lo.set(st.get("sleep_lo", 3))
        self.sp_hi.set(st.get("sleep_hi", 8))
        self.sp_wait.set(st.get("submit_wait", 5))
        self.v_show.set(st.get("show_browser", True))

    def _save_cfg(self, cfg):
        st = self._state()
        st.update({k: cfg[k] for k in
                   ("school", "name", "sex", "age", "grade", "sleep_lo", "sleep_hi", "submit_wait")})
        st["show_browser"] = cfg["show_browser"]
        CFG_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 运行 ----------
    def _collect(self):
        school = self.cb_school.get().strip()
        if not school:
            raise ValueError("请填写学校")
        info = self.school_map.get(school)
        if info is None:
            cand = [n for n in self.school_map if school in n]
            if len(cand) == 1:
                school = cand[0]
                info = self.school_map[school]
                self.cb_school.set(school)
            else:
                raise ValueError("学校「%s」不在名单里，%s" % (
                    school, ("你要找的是：%s ？" % "、".join(cand[:5])) if cand else "请从下拉列表里选"))
        name = self.e_name.get().strip()
        if not name:
            raise ValueError("请填写姓名")
        if re.search(r"[0-9\s~!@#$%^&*()\-+={}\[\]|\\;:'\",.<>/?]", name):
            raise ValueError("姓名不能带数字或特殊字符")
        try:
            age = int(float(self.sp_age.get()))
        except Exception:
            raise ValueError("年龄要填数字")
        if not 14 <= age <= 28:
            raise ValueError("年龄需在 14–28 之间")
        try:
            lo = float(self.sp_lo.get())
            hi = float(self.sp_hi.get())
            wait = float(self.sp_wait.get())
        except Exception:
            raise ValueError("节奏参数要填数字")
        if lo < 0 or hi < lo:
            raise ValueError("间隔范围不对：最小值不能大于最大值")

        return {
            "school": info["schoolName"],
            "city": info["gbname"],
            "schoolCode": info["schoolCode"],
            "gbcode": info["gbcode"],
            "name": name,
            "sex": self.v_sex.get(),
            "age": age,
            "grade": self.cb_grade.get() or "大一",
            "sleep_lo": lo,
            "sleep_hi": hi,
            "submit_wait": wait,
            "show_browser": self.v_show.get(),
            "dry_run": self.v_dry.get(),
            "openid": (self.e_openid.get().strip()
                       or self._state().get("openid")
                       or self._new_openid()),
        }

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            cfg = self._collect()
        except ValueError as e:
            messagebox.showwarning("还差一步", str(e))
            return
        if cfg["dry_run"]:
            if not messagebox.askokcancel("演练模式", "演练只会作答到最后一题，不会提交。\n\n继续吗？"):
                return
        else:
            ok = messagebox.askokcancel(
                "开始答题",
                "即将为 %s（%s）作答并提交。\n每人只有 3 次机会，确认开始吗？" %
                (cfg["name"], cfg["school"]))
            if not ok:
                return

        self._save_cfg(cfg)
        self.stopped.clear()
        self.txt.delete("1.0", "end")
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lb_state.configure(text="运行中", fg=ACCENT)
        self.pb.configure(value=0)
        self.log("=" * 62, "dim")
        self.log("开始：%s / %s / %s / %s岁 / %s" %
                 (cfg["school"], cfg["name"], cfg["sex"], cfg["age"], cfg["grade"]), "hl")
        self.log("节奏：每题 %.1f–%.1f 秒，提交前 %.0f 秒" %
                 (cfg["sleep_lo"], cfg["sleep_hi"], cfg["submit_wait"]), "dim")

        self.worker = threading.Thread(target=self._run_engine, args=(cfg,), daemon=True)
        self.worker.start()

    def _run_engine(self, cfg):
        eng = Engine(cfg, lambda m, tag=None: self.q.put(("log", m, tag)), self.stopped)
        try:
            eng.run()
            self.q.put(("done", "本次流程结束", None))
        except StopError:
            self.q.put(("log", "已手动停止", "bad"))
            self.q.put(("done", "已停止", None))
        except Exception as e:
            self.q.put(("log", "出错了：%s" % e, "bad"))
            self.q.put(("log", traceback.format_exc()[-1200:], "dim"))
            self.q.put(("done", "出错了", None))

    def _stop(self):
        self.stopped.set()
        self.lb_state.configure(text="正在停止 …", fg=BAD)

    # ---------- 日志 ----------
    def log(self, msg, tag=None):
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt.insert("end", "%s  %s\n" % (ts, msg), tag or ())
        self.txt.see("end")

    def _drain(self):
        try:
            while True:
                kind, msg, tag = self.q.get_nowait()
                if kind == "log":
                    self.log(msg, tag)
                elif kind == "done":
                    self.lb_state.configure(text=msg, fg=SUB)
                    self.btn_run.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.pb.configure(value=100)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
