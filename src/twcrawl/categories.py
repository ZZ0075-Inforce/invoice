"""店家分類：兩層規則——通用連鎖隨工具散布，個人小店本機私有。

規則是「子字串 → 分類」，比對正規化後的賣方名稱；個人層優先，同層內長樣式優先。
個人規則放工作區根目錄 `categories.local.json`（已 gitignore，勿入版控）：

    {
      "rules": {"小巷麵館": "餐飲", "拾光": "咖啡"},
      "aliases": {"拾光": "DAYLIGHT COFFEE"},
      "item_rules": {"無鉛汽油": "加油"},
      "unnecessary": ["手搖飲", "甜點零食"]
    }

`unnecessary` 選填，提供時整組取代預設的非必要分類集合。
`aliases` 選填：發票上的公司登記名 → 招牌名，儀表板顯示用（比對規則同 rules）。
`item_rules` 選填：品項覆寫——品項名稱命中就覆寫**整張發票**的分類（比對語意
同 rules）。店家分類表達不了跨業態店家（好市多加油站的稅籍同列超級市場與
汽油零售），而油品這類發票實測張張單品項，覆寫整張即精確；店家本身的業態
分類不受影響。
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

UNCATEGORIZED = "未分類"

# 通用層：台灣常見連鎖。發票賣方多為公司登記名（例：和德昌＝麥當勞、
# 富利餐飲＝肯德基、三商家購＝美廉社、悠旅生活＝星巴克、富邦媒體＝momo）。
GENERIC_RULES: dict[str, str] = {
    # 超市／量販
    "全聯": "超市", "美廉社": "超市", "三商家購": "超市", "楓康": "超市",
    "家樂福": "量販", "好市多": "量販", "大潤發": "量販", "愛買": "量販",
    # 便利商店
    "統一超商": "便利商店", "全家便利商店": "便利商店",
    "萊爾富": "便利商店", "來來超商": "便利商店",
    # 速食
    "和德昌": "速食", "富利餐飲": "速食", "台灣必勝客": "速食",
    "摩斯": "速食", "安心食品": "速食", "頂呱呱": "速食", "三商餐飲": "速食",
    # 手搖飲
    "五十嵐": "手搖飲", "清心福全": "手搖飲", "迷客夏": "手搖飲",
    "麻古": "手搖飲", "大苑子": "手搖飲", "可不可熟成": "手搖飲",
    # 甜點零食／咖啡
    "85度": "甜點零食", "統一多拿滋": "甜點零食", "亞尼克": "甜點零食",
    "路易莎": "咖啡", "悠旅生活": "咖啡", "星巴克": "咖啡", "cama": "咖啡",
    # 百貨
    "遠百": "百貨", "新光三越": "百貨", "遠東百貨": "百貨", "微風": "百貨",
    # 藥妝藥局
    "屈臣氏": "藥妝", "康是美": "藥妝", "寶雅": "藥妝", "大樹醫藥": "藥妝",
    # 交通三分：加油／停車獨立成類（金額與頻率的性質不同，混在一起
    # 看不出各自趨勢）；「交通」留給大眾運輸與計程車
    "台灣中油": "加油", "全國加油": "加油", "台亞石油": "加油",
    "悠遊卡": "交通", "台灣高鐵": "交通", "台灣大車隊": "交通",
    "嘟嘟房": "停車", "俥亭": "停車", "台灣聯通": "停車",
    "歐特儀": "停車", "便利停車": "停車",
    # 電商
    "富邦媒體": "電商", "網路家庭": "電商", "樂購蝦皮": "電商",
    "酷澎": "電商", "博客來": "電商",
    # 水電通信
    "台灣電力": "水電通信", "台灣自來水": "水電通信",
    "中華電信": "水電通信", "台灣大哥大": "水電通信", "遠傳電信": "水電通信",
    # 業種通用詞（不指涉特定店家，放通用層安全；長樣式優先，
    # 所以「富利餐飲→速食」仍會贏過「餐飲→餐飲」）
    "小吃店": "餐飲", "餐飲": "餐飲", "鍋物": "餐飲", "小館": "餐飲",
    "餐館": "餐飲", "食堂": "餐飲", "便當": "餐飲", "日本料理": "餐飲",
    "壽司郎": "餐飲", "茶行": "手搖飲", "冷飲店": "手搖飲",
    "藥局": "藥妝", "購物中心": "百貨", "百貨": "百貨", "加油站": "加油",
    "停車": "停車",
}

DEFAULT_UNNECESSARY = {"手搖飲", "甜點零食", "咖啡"}

# 品項覆寫的通用層：只收「一看品名就知道這筆消費是什麼」的高確度詞
# （油品品名固定：95無鉛汽油／九二無鉛汽油／超級柴油…）。曖昧詞放個人層自己加。
GENERIC_ITEM_RULES: dict[str, str] = {
    "無鉛汽油": "加油", "柴油": "加油",
}

# 稅籍行業名稱 → 店家分類（bizreg 對照表的自動分類後備層；
# 只在 rules 兩層都沒中時使用）
INDUSTRY_RULES: dict[str, str] = {
    "咖啡館": "咖啡", "咖啡": "咖啡",
    "飲料店": "手搖飲", "手搖飲": "手搖飲", "飲料": "手搖飲",
    "茶飲": "手搖飲", "茶葉": "手搖飲", "冰果": "手搖飲",
    "餐盒": "餐飲", "便當": "餐飲", "餐館": "餐飲", "餐廳": "餐飲",
    "小吃": "餐飲", "麵店": "餐飲", "早餐": "餐飲", "自助餐": "餐飲",
    "烘焙": "甜點零食", "麵包": "甜點零食", "糕餅": "甜點零食",
    "超級市場": "超市", "便利商店": "便利商店", "百貨公司": "百貨",
    "藥局": "藥妝", "藥品": "藥妝", "藥粧": "藥妝", "化粧品": "藥妝",
    "加油站": "加油", "汽油": "加油", "柴油": "加油",
    "停車": "停車", "汽車客運": "交通",
    "旅行業": "旅遊", "旅館": "旅遊",
    "電信": "水電通信", "電力": "水電通信",
    "書籍": "書店", "文具": "書店",
}


def industry_category(industry_text) -> str | None:
    """從稅籍行業名稱（可能多個、以「、」相連）推分類。"""
    t = _norm(industry_text)
    if not t:
        return None
    for pat, cat in INDUSTRY_RULES.items():
        if _norm(pat) in t:
            return cat
    return None


def _norm(s) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s　]+", "", s).lower()


def load_local_config(path: Path | str) -> dict:
    """讀個人規則檔；手貼 JSON 難免貼壞，壞掉時給人話（哪一行、怎麼修）而非 traceback。"""
    p = Path(path)
    if not p.exists():
        return {}

    def no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise SystemExit(
                    f"{p} 裡「{k}」重複出現——JSON 的後者會悄悄蓋掉前者，"
                    "請合併成一筆再重跑。")
            d[k] = v
        return d

    try:
        cfg = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=no_dupes)
    except SystemExit:
        raise
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"{p} 第 {e.lineno} 行第 {e.colno} 欄有語法錯誤：{e.msg}\n"
            "常見原因：多了／少了逗號、引號沒關。正確格式範例：\n"
            '{ "rules": {"小巷麵館": "餐飲"}, "unnecessary": ["手搖飲"] }') from None
    if not isinstance(cfg, dict):
        raise SystemExit(f"{p} 最外層要是物件（{{...}}）。")
    for key in ("rules", "aliases", "item_rules"):
        v = cfg.get(key)
        if v is not None and not isinstance(v, dict):
            raise SystemExit(f'{p} 的 {key} 要是物件（{{"店名": "…"}}）。')
        for k2, v2 in (v or {}).items():
            if not isinstance(v2, str) or not v2.strip():
                raise SystemExit(f"{p} 的 {key}「{k2}」對應值要是非空字串。")
    unn = cfg.get("unnecessary")
    if unn is not None and (not isinstance(unn, list)
                            or any(not isinstance(x, str) for x in unn)):
        raise SystemExit(f'{p} 的 unnecessary 要是字串清單（["手搖飲", …]）。')
    return cfg


class Classifier:
    def __init__(self, local_path: Path | str):
        personal: dict[str, str] = {}
        aliases: dict[str, str] = {}
        personal_items: dict[str, str] = {}
        unnecessary = set(DEFAULT_UNNECESSARY)
        p = Path(local_path)
        if p.exists():
            cfg = load_local_config(p)
            personal = dict(cfg.get("rules") or {})
            aliases = dict(cfg.get("aliases") or {})
            personal_items = dict(cfg.get("item_rules") or {})
            if cfg.get("unnecessary") is not None:
                unnecessary = set(cfg["unnecessary"])
        self._aliases: list[tuple[str, str]] = [
            (_norm(pat), disp)
            for pat, disp in sorted(aliases.items(), key=lambda kv: -len(kv[0]))
            if _norm(pat)
        ]
        # 個人層排前面；同層內長樣式優先，避免短樣式攔截長樣式
        self._rules: list[tuple[str, str]] = [
            (_norm(pat), cat)
            for pat, cat in (
                sorted(personal.items(), key=lambda kv: -len(kv[0]))
                + sorted(GENERIC_RULES.items(), key=lambda kv: -len(kv[0]))
            )
            if _norm(pat)
        ]
        self._item_rules: list[tuple[str, str]] = [
            (_norm(pat), cat)
            for pat, cat in (
                sorted(personal_items.items(), key=lambda kv: -len(kv[0]))
                + sorted(GENERIC_ITEM_RULES.items(), key=lambda kv: -len(kv[0]))
            )
            if _norm(pat)
        ]
        self.unnecessary = unnecessary

    def category(self, seller_name) -> str:
        n = _norm(seller_name)
        if n:
            for pat, cat in self._rules:
                if pat in n:
                    return cat
        return UNCATEGORIZED

    def item_category(self, descriptions) -> str | None:
        """品項覆寫：任一品項命中規則就回覆寫分類；沒中回 None。

        規則優先序（個人優先、長樣式優先）看整張發票，不受品項順序影響。
        """
        descs = [n for n in (_norm(d) for d in descriptions) if n]
        if descs:
            for pat, cat in self._item_rules:
                if any(pat in n for n in descs):
                    return cat
        return None

    def is_unnecessary(self, category: str) -> bool:
        return category in self.unnecessary

    def display_name(self, seller_name) -> str | None:
        """招牌名別名；沒設定回 None。"""
        n = _norm(seller_name)
        if n:
            for pat, disp in self._aliases:
                if pat in n:
                    return disp
        return None
