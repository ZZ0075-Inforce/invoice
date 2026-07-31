/* twcrawl 四頁共用的頁面骨架與小工具。

   為什麼是全域 `TW` 而不是 ES module：檢視路徑要能 file:// 開檔即用
   （ADR-0002），而瀏覽器對 file:// 頁面的 `import` 走 CORS 會被擋——這跟
   data.js 當初不走 fetch 而走 <script src> 是同一個理由。別「現代化」成
   `<script type="module">`，那會讓雙擊開檔的路徑整個壞掉。

   核心是 TW.page()：四頁的開場白本來各寫一份，而且四份的「payload 可用嗎」
   判準都不一樣；更要緊的是四份都沒有錯誤圍堵——render 中途 throw 會留下一個
   已清空的容器，畫面與「找不到 data.js」無法區分。
*/
(function () {
  "use strict";

  // 主題要在 <head> 同步套用，否則會先畫亮色再跳暗色
  const saved = localStorage.getItem("twcrawl-theme");
  if (saved === "light" || saved === "dark") {
    document.documentElement.dataset.theme = saved;
  }

  const THEMES = [["", "🌗 自動"], ["light", "☀️ 亮"], ["dark", "🌙 暗"]];
  const SLOTS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"];

  const esc = s => String(s).replace(/[&<>"]/g,
    ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[ch]));
  const nt = x => "NT$" + Math.round(x).toLocaleString("zh-TW");
  const css = n => getComputedStyle(document.documentElement)
    .getPropertyValue(n).trim();
  const el = (tag, cls, html) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  };

  /** 亮暗切換鈕（自動→亮→暗）。圖表顏色在繪製時取樣，所以直接重載最單純。 */
  function themeButton() {
    const cur = document.documentElement.dataset.theme || "";
    const tb = el("button", "theme", THEMES.find(x => x[0] === cur)[1]);
    tb.title = "亮暗主題（自動→亮→暗）";
    tb.addEventListener("click", () => {
      const next = THEMES[(THEMES.findIndex(x => x[0] === cur) + 1)
        % THEMES.length][0];
      if (next) localStorage.setItem("twcrawl-theme", next);
      else localStorage.removeItem("twcrawl-theme");
      location.reload();
    });
    return tb;
  }

  /** 分類色槽：全期金額前 6 大分類各佔一槽，其餘（含未分類）為中性灰。 */
  function catColors(categories) {
    const top = (categories || [])
      .filter(c => c.name !== "未分類").slice(0, 6).map(c => c.name);
    return {
      top,
      at: i => (i >= 0 && i < top.length ? css(SLOTS[i]) : css("--other")),
      of: name => {
        const i = top.indexOf(name);
        return i >= 0 ? css(SLOTS[i]) : css("--other");
      },
    };
  }

  /** 非必要分類的名字集合。判準是 categories[].unnecessary 這個旗標，
   *  頁面拿它篩 invoices——payload 不另外帶一份非必要清單。 */
  function unnecessaryCats(categories) {
    return new Set((categories || [])
      .filter(c => c.unnecessary).map(c => c.name));
  }

  /** 一組發票在各月份的合計。月份清單由 payload 的 months 決定（含零的月，
   *  折線才不會跳過空月）；發票是頁面篩過的，所以這一步只能在客戶端算。 */
  function monthTotals(invoices, months) {
    return (months || []).map(m => ({
      month: m.month,
      v: (invoices || [])
        .filter(v => v.date.slice(0, 7) === m.month)
        .reduce((s, v) => s + v.amount, 0),
    }));
  }

  /** 月份區間 → 日期字串邊界。日期一律 YYYY-MM-DD 且比較用字串比，所以
   *  上界取 "-31" 對每個月都成立（不必知道當月幾天）。 */
  function monthBounds(fromM, toM) {
    return { from: fromM ? fromM + "-01" : "", to: toM ? toM + "-31" : "" };
  }

  /**
   * 頁面骨架。
   *
   *   needs   payload 必須有的頂層鍵；陣列還要非空。缺了就原地不動——頁面
   *           HTML 裡本來就有一段「找不到 data.js」的靜態說明，那才是使用者
   *           該看到的東西
   *   ready   選填的額外前置條件（地圖用它確認 Leaflet 載入了）
   *   clear   開畫前清空哪個容器，預設 "#app"；傳 null 表示自己管
   *   render  (payload, TW) => void
   */
  function page(opts) {
    const D = window.TWCRAWL_DATA;
    const ok = k => {
      const v = D && D[k];
      return Array.isArray(v) ? v.length > 0 : !!v;
    };
    if (!D || !(opts.needs || []).every(ok)) return;
    if (opts.ready && !opts.ready()) return;

    const sel = opts.clear === undefined ? "#app" : opts.clear;
    const root = sel ? document.querySelector(sel) : null;
    if (root) root.innerHTML = "";
    try {
      opts.render(D, TW);
    } catch (e) {
      console.error(e);
      (root || document.body).append(el("div", "empty",
        "這一頁沒能畫完：" + esc(e && e.message ? e.message : String(e)) +
        "<br><br>data.js 可能是舊版或不完整——重跑 " +
        "<code>twcrawl export</code> 通常就能修好；" +
        "詳細錯誤在瀏覽器主控台。"));
    }
  }

  const TW = { THEMES, SLOTS, esc, nt, css, el, themeButton, catColors,
               unnecessaryCats, monthTotals, monthBounds, page };
  window.TW = TW;
})();
