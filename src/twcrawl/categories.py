"""店家分類：一條優先序鏈，收在單一介面之後。

`for_seller` 給店家分類、`for_invoice` 給發票分類（品項覆寫後），兩者都回傳
`Category`——分類名、命中的是哪一層、是不是非必要、是不是餐飲現調，一次給齊。
呼叫端不再需要知道優先序、不需要自己比對「未分類」、也不需要自己記得問稅籍行業。

    品項覆寫  個人 item_rules → 通用 item_rules      source="item"（整張發票）
              ↓ 沒中
    店家規則  個人 rules（長樣式優先）               source="personal"
              通用 rules（長樣式優先）               source="generic"
              稅籍行業後備 industries[店家名]        source="industry"
              都沒中                                 source="none"

規則是「子字串 → 分類」，比對正規化後的名稱。個人規則放 repo 根目錄
`categories.local.json`（已 gitignore，勿入版控）：

    {
      "rules": {"小巷麵館": "餐飲", "拾光": "咖啡"},
      "aliases": {"拾光": "DAYLIGHT COFFEE"},
      "item_rules": {"無鉛汽油": "加油"},
      "unnecessary": ["手搖飲", "甜點零食"],
      "eatery": ["麵食"]
    }

`aliases` 選填：發票上的公司登記名 → 招牌名，儀表板顯示用（比對規則同 rules）。
`item_rules` 選填：品項覆寫——品項名稱命中就覆寫**整張發票**的分類（比對語意
同 rules）。店家分類表達不了跨業態店家（好市多加油站的稅籍同列超級市場與
汽油零售），而油品這類發票實測張張單品項，覆寫整張即精確；店家本身的業態
分類不受影響。

兩個清單的語意刻意不同：
- `unnecessary` 選填，提供時**整組取代**預設集合——非必要是價值判斷，取代合理。
- `eatery` 選填，提供時與內建集合**聯集**——餐飲現調是事實不是偏好，內建四類
  清不掉。它決定 FDA 比對要不要濾掉品項層級的撞名（菜名撞包裝品名必為誤報）；
  自創分類名（例：把巷口麵店歸「麵食」）要在這裡宣告，否則濾除會靜默失效。
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

LOCAL_RULES_PATH = Path("categories.local.json")
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

# 餐飲現調＝發票品項是菜名而非商品名的店家。FDA 品項層級比對對它們無意義
# （菜名撞包裝品名必為誤報），內建這四類；個人層可再加（聯集，清不掉）。
DEFAULT_EATERY = {"餐飲", "速食", "手搖飲", "咖啡"}

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


def normalize(s) -> str:
    """比對用正規化：全形→半形、去空白、轉小寫。

    公開是因為 match.py 對 FDA 欄位也用同一套——兩邊必須一致，
    各留一份遲早會偷偷分岔。
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s　]+", "", s).lower()


@dataclass(frozen=True)
class Category:
    """一次回答呼叫端會問的全部四件事。

    `source` 是命中的層級（personal / generic / industry / item / none）。
    「沒有任何規則命中」看 source == "none"，不要拿 name 跟 UNCATEGORIZED 比
    ——那是顯示用的名字，不是 sentinel。
    """

    name: str
    source: str
    unnecessary: bool
    eatery: bool


def _industry_category(industry_text) -> str | None:
    """從稅籍行業名稱（可能多個、以「、」相連）推分類。"""
    t = normalize(industry_text)
    if not t:
        return None
    for pat, cat in INDUSTRY_RULES.items():
        if normalize(pat) in t:
            return cat
    return None


def load_local_config(path: Path | str = LOCAL_RULES_PATH) -> dict:
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
    for key, sample in (("unnecessary", "手搖飲"), ("eatery", "麵食")):
        v = cfg.get(key)
        if v is not None and (not isinstance(v, list)
                              or any(not isinstance(x, str) for x in v)):
            raise SystemExit(f'{p} 的 {key} 要是字串清單（["{sample}", …]）。')
    return cfg


def _compile(personal: dict[str, str],
             generic: dict[str, str]) -> list[tuple[str, str, str]]:
    """個人層排前面；同層內長樣式優先，避免短樣式攔截長樣式。"""
    return [
        (normalize(pat), cat, layer)
        for layer, table in (("personal", personal), ("generic", generic))
        for pat, cat in sorted(table.items(), key=lambda kv: -len(kv[0]))
        if normalize(pat)
    ]


class Classifier:
    def __init__(self, local_path: Path | str = LOCAL_RULES_PATH,
                 industries: Mapping[str, str | None] | None = None):
        personal: dict[str, str] = {}
        aliases: dict[str, str] = {}
        personal_items: dict[str, str] = {}
        unnecessary = set(DEFAULT_UNNECESSARY)
        eatery = set(DEFAULT_EATERY)
        p = Path(local_path)
        if p.exists():
            cfg = load_local_config(p)
            personal = dict(cfg.get("rules") or {})
            aliases = dict(cfg.get("aliases") or {})
            personal_items = dict(cfg.get("item_rules") or {})
            if cfg.get("unnecessary") is not None:
                unnecessary = set(cfg["unnecessary"])      # 整組取代
            eatery |= set(cfg.get("eatery") or [])         # 聯集
        self._aliases: list[tuple[str, str]] = [
            (normalize(pat), disp)
            for pat, disp in sorted(aliases.items(), key=lambda kv: -len(kv[0]))
            if normalize(pat)
        ]
        self._rules = _compile(personal, GENERIC_RULES)
        self._item_rules = _compile(personal_items, GENERIC_ITEM_RULES)
        self.unnecessary = unnecessary
        self.eatery = eatery
        self._industries: Mapping[str, str | None] = industries or {}
        self._cache: dict[object, Category] = {}

    def with_industries(self, industries: Mapping[str, str | None]) -> Classifier:
        """接上稅籍行業後備，回傳新的 Classifier（規則表共用，快取重來）。

        存在的理由：Classifier 常在拿到資料庫連線之前就建好（serve、測試都是），
        而少接這一層不會報錯、只會讓兩成店家靜默掉回未分類——正是這個模組要
        消滅的那類錯誤。所以由 build_payload／run_match 統一在用之前接上。
        """
        new = copy.copy(self)
        new._industries = dict(industries)
        new._cache = {}
        return new

    def for_seller(self, seller_name) -> Category:
        """店家分類：規則兩層 → 稅籍行業後備 → 未分類。"""
        hit = self._cache.get(seller_name)
        if hit is None:
            hit = self._cache[seller_name] = self._resolve_seller(seller_name)
        return hit

    def for_invoice(self, seller_name, descriptions: Iterable) -> Category:
        """發票分類：品項覆寫優先，沒中就用店家分類。

        覆寫看整張發票的品項，不受品項順序影響（規則本身已按個人優先、
        長樣式優先排序）。
        """
        descs = [n for n in (normalize(d) for d in descriptions) if n]
        for pat, cat, _layer in self._item_rules:
            if any(pat in n for n in descs):
                return self._make(cat, "item")
        return self.for_seller(seller_name)

    def display_name(self, seller_name) -> str:
        """招牌名別名；沒設定就回原本的名稱。"""
        n = normalize(seller_name)
        if n:
            for pat, disp in self._aliases:
                if pat in n:
                    return disp
        return seller_name

    # ---- 內部 ----------------------------------------------------------

    def _resolve_seller(self, seller_name) -> Category:
        n = normalize(seller_name)
        if n:
            for pat, cat, layer in self._rules:
                if pat in n:
                    return self._make(cat, layer)
        ind = _industry_category(self._industries.get(seller_name))
        if ind:
            return self._make(ind, "industry")
        return self._make(UNCATEGORIZED, "none")

    def _make(self, name: str, source: str) -> Category:
        return Category(name=name, source=source,
                        unnecessary=name in self.unnecessary,
                        eatery=name in self.eatery)
