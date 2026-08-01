/* twcrawl 五頁共用的頁面骨架與小工具。

   為什麼是全域 `TW` 而不是 ES module：檢視路徑要能 file:// 開檔即用
   （ADR-0002），而瀏覽器對 file:// 頁面的 `import` 走 CORS 會被擋——這跟
   data.js 當初不走 fetch 而走 <script src> 是同一個理由。別「現代化」成
   `<script type="module">`，那會讓雙擊開檔的路徑整個壞掉。

   核心是 TW.page()：各頁的開場白本來各寫一份，而且每份的「payload 可用嗎」
   判準都不一樣；更要緊的是誰都沒有錯誤圍堵——render 中途 throw 會留下一個
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

  /** 分類色槽：指派由匯出端決定並持久化（categories[].slot，1..6），這裡
   *  只讀結果——同一分類跨次匯出才不會跳色（issue #10）。沒有槽的（含
   *  未分類）為中性灰；舊版 data.js 沒有 slot 欄位時全部落灰，重跑
   *  twcrawl export 即可。top 依 categories 的既有排序（金額），給堆疊
   *  圖與圖例當系列順序——順序歸排序、顏色歸槽位。 */
  function catColors(categories) {
    const slots = {};
    for (const c of categories || []) if (c.slot >= 1) slots[c.name] = c.slot;
    const top = (categories || []).map(c => c.name).filter(n => slots[n]);
    const of = name => slots[name] ? css("--s" + slots[name]) : css("--other");
    return {
      top,
      at: i => (i >= 0 && i < top.length ? of(top[i]) : css("--other")),
      of,
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

  /** serve 模式？（`twcrawl serve` 之下才有後端可打，ADR-0002 的雙模式判準）
   *
   *  這個判斷本來各頁各寫一份，加上控制台又多一種寫法，所以收進 TW 出一份。
   *  行為與原本的 `location.protocol.startsWith("http")` 相同。 */
  function isServe() {
    return location.protocol === "http:" || location.protocol === "https:";
  }

  /** 五頁通往控制台的入口（issue #20）。
   *
   *  只在 serve 模式下畫：file:// 之下控制台沒有後端可打，給一條按了沒反應
   *  的連結比沒有更糟。這也順帶讓五頁的 golden 快照不受影響——那些測試走
   *  file://，所以看不到這個節點。
   *
   *  掛在各頁的 `.sub` 那一行；四頁的 .sub 是 render 時才建的，而 render 走
   *  body 底部的 inline script（在 DOMContentLoaded 之前跑完），所以這裡等到
   *  DOMContentLoaded 才找得穩。
   *
   *  後備鏈是有原因的：payload 缺漏時 `TW.page` 會直接 return，於是 .sub
   *  根本不存在——那正是最需要「重生報表」的時候，卻剛好沒地方掛。所以往下
   *  退到 header（地圖是這個）／#app／body，總有一個在。
   */
  function controlLink() {
    if (!isServe()) return;
    if (/control\.html$/.test(location.pathname)) return;   // 自己不連自己
    const host = document.querySelector(".sub")
      || document.querySelector("header")
      || document.querySelector("#app")
      || document.body;
    if (!host || host.querySelector("a.ctl")) return;
    const a = el("a", "ctl map", "🎛 控制台");
    a.href = "control.html";
    host.append(" ", a);
  }

  if (document.readyState === "loading") {
    addEventListener("DOMContentLoaded", controlLink);
  } else {
    controlLink();
  }

  const TW = { THEMES, esc, nt, css, el, themeButton, catColors,
               unnecessaryCats, monthTotals, monthBounds, page, isServe };
  window.TW = TW;
})();
