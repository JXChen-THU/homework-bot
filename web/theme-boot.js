/* ============================================================================
   主题解析：三态 auto / light / dark
   ----------------------------------------------------------------------------
   auto（默认）= 跟随系统 prefers-color-scheme，并且系统切换时**实时**跟随
   light / dark  = 显式覆盖（当前没有 UI 去设，留给以后要固定主题时用）

   为什么单独一份、还要内联进 <head>：
   主窗口和两个浮窗是三个**独立**的 WebView2 文档，谁也不知道谁。
   三边各跑同一套规则、只监听同一个系统信号，就不会出现
   "主窗口已经暗了、浮窗还是亮的"这种不同步。

   为什么必须内联在 <head>（不能只在 .js 里）：
   页面第一次绘制之前就得把 data-lg-dark 定下来，否则会先闪一下浅色再变暗。

   属性名 data-lg-dark 与三份样式文件里的选择器一致。
   ========================================================================== */
(function () {
  'use strict';

  var KEY = 'hwThemeMode';          // 记住上一次的模式；读不到就当 auto
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function mode() {
    try {
      var v = localStorage.getItem(KEY);
      return (v === 'light' || v === 'dark') ? v : 'auto';
    } catch (e) { return 'auto'; }
  }

  function resolved(m) {
    if (m === 'light' || m === 'dark') return m;
    return (mq && mq.matches) ? 'dark' : 'light';
  }

  function apply() {
    var m = mode();
    var dark = resolved(m) === 'dark';
    var root = document.documentElement;
    if (dark) root.setAttribute('data-lg-dark', '');
    else root.removeAttribute('data-lg-dark');
    root.setAttribute('data-lg-mode', m);
    return dark ? 'dark' : 'light';
  }

  /* 系统主题变化 → auto 模式下立刻跟随（不用重开程序） */
  if (mq) {
    var onChange = function () { if (mode() === 'auto') apply(); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }

  /* 主窗口改主题 → 两个浮窗立刻跟着变。
     ----------------------------------------------------------------------
     三个页面是**各自独立的文档**，谁也拿不到谁的 window。之前在设置里点主题，
     只改了 localStorage，浮窗收不到任何通知，所以必须重开程序才会变 ——
     这就是"切深浅时浮窗没自动切换"。
     同一个源（file:///…/web/）的窗口共享 localStorage，改动会在**其他**窗口
     触发 storage 事件，正好用来广播。写改动的那一方自己不会收到，但它在
     setMode() 里已经 apply() 过了，不会漏。
     用 key 过滤：只有主题这一项变化才重画，别被别的键吵醒。 */
  window.addEventListener('storage', function (e) {
    if (e && e.key && e.key !== KEY) return;
    apply();
  });

  window.hwTheme = {
    apply: apply,
    mode: mode,
    resolved: function () { return resolved(mode()); },
    /* 供以后做"主题：跟随系统 / 始终浅色 / 始终深色"的开关用 */
    setMode: function (m) {
      try {
        if (m === 'light' || m === 'dark' || m === 'auto') localStorage.setItem(KEY, m);
      } catch (e) { /* 隐私模式写不了，忽略 */ }
      return apply();
    }
  };

  apply();   // 首帧前就定下来
})();
