/* dashboard_charts.js - 专利预警大屏 高级视觉版 + 实时刷新 */
(function () {
  var cM = null, cK = null, cT = null, cR = null;

  /* ========== 顶部时钟 ========== */
  function startClock() {
    function tick() {
      var d = new Date();
      var p = function (n) { return n < 10 ? '0' + n : String(n); };
      var e = document.getElementById('showTime');
      if (e) {
        e.textContent =
          d.getFullYear() + '年' +
          p(d.getMonth() + 1) + '月' +
          p(d.getDate()) + '日 ' +
          p(d.getHours()) + '时' +
          p(d.getMinutes()) + '分' +
          p(d.getSeconds()) + '秒';
      }
      setTimeout(tick, 1000);
    }
    tick();
  }

  /* ========== 背景粒子动画 ========== */
  function initParticles() {
    var cv = document.getElementById('particles-canvas');
    if (!cv) return;
    var cx = cv.getContext('2d');

    function rs() {
      cv.width = window.innerWidth;
      cv.height = window.innerHeight;
    }
    rs();
    window.addEventListener('resize', rs);

    var pt = [];
    for (var i = 0; i < 80; i++) {
      pt.push({
        x: Math.random() * cv.width,
        y: Math.random() * cv.height,
        vx: (Math.random() - .5) * .5,
        vy: (Math.random() - .5) * .5,
        r: Math.random() * 2 + 1
      });
    }

    function fr() {
      cx.clearRect(0, 0, cv.width, cv.height);
      for (var i = 0; i < pt.length; i++) {
        var p = pt[i];
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < 0 || p.x > cv.width) p.vx *= -1;
        if (p.y < 0 || p.y > cv.height) p.vy *= -1;

        cx.beginPath();
        cx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        cx.fillStyle = 'rgba(73,188,247,.75)';
        cx.fill();

        for (var j = i + 1; j < pt.length; j++) {
          var q = pt[j], dx = p.x - q.x, dy = p.y - q.y;
          var dd = Math.sqrt(dx * dx + dy * dy);
          if (dd < 130) {
            cx.globalAlpha = (1 - dd / 130) * .25;
            cx.beginPath();
            cx.moveTo(p.x, p.y);
            cx.lineTo(q.x, q.y);
            cx.strokeStyle = 'rgba(73,188,247,1)';
            cx.lineWidth = .8;
            cx.stroke();
            cx.globalAlpha = 1;
          }
        }
      }
      requestAnimationFrame(fr);
    }
    fr();
  }

  /* ========== 通用工具函数 ========== */
  function setText(id, val) {
    var e = document.getElementById(id);
    if (e) e.textContent = val;
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  /* 数字从旧值缓动到新值，提升视觉高级感 */
  function animateNumber(id, target, suffix, decimals) {
    var el = document.getElementById(id);
    if (!el) return;

    var txt = el.textContent || '0';
    var num = parseFloat(String(txt).replace(/[^\d\.]/g, ''));
    if (!isFinite(num)) num = 0;

    target = +target || 0;
    suffix = suffix || '';
    decimals = typeof decimals === 'number' ? decimals : 0;

    var duration = 600;
    var start = null;

    function step(ts) {
      if (!start) start = ts;
      var progress = Math.min((ts - start) / duration, 1);
      var cur = num + (target - num) * progress;
      var shown = decimals > 0 ? cur.toFixed(decimals) : Math.round(cur).toString();
      el.textContent = shown + suffix;
      if (progress < 1) requestAnimationFrame(step);
    }

    requestAnimationFrame(step);
  }

  /* 风险条更新 */
  function updateRiskBars(high, mid, low) {
    var maxV = Math.max(high, mid, low, 1);
    var bh = document.getElementById('bar-high');
    var bm = document.getElementById('bar-mid');
    var bl = document.getElementById('bar-low');
    if (bh) bh.style.width = Math.round(high / maxV * 100) + '%';
    if (bm) bm.style.width = Math.round(mid / maxV * 100) + '%';
    if (bl) bl.style.width = Math.round(low / maxV * 100) + '%';

    setText('count-high', high.toLocaleString());
    setText('count-mid', mid.toLocaleString());
    setText('count-low', low.toLocaleString());
  }

  /* ========== 单图渲染函数 ========== */

  /* 小环形占比饼图（高/中/低风险） */
  function renderPie(id, val, tot, col) {
    var el = document.getElementById(id);
    if (!el) return;
    var ec = echarts.init(el);
    var pct = tot > 0 ? Math.round(val / tot * 100) : 0;

    ec.setOption({
      animationDuration: 1000,
      animationEasing: 'cubicOut',
      title: {
        text: pct + '%',
        x: 'center',
        y: 'center',
        textStyle: {
          color: '#fff',
          fontSize: 18,
          fontWeight: 'normal'
        }
      },
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(0, 10, 40, 0.92)',
        borderColor: '#49bcf7',
        borderWidth: 1,
        padding: [6, 10],
        textStyle: { color: '#e6f7ff', fontSize: 11 },
        extraCssText: 'box-shadow:0 0 18px rgba(0,0,0,.8);border-radius:4px;'
      },
      series: [{
        type: 'pie',
        radius: ['62%', '82%'],
        clockwise: true,
        label: { show: false },
        labelLine: { show: false },
        hoverAnimation: false,
        data: [
          {
            value: val,
            itemStyle: { color: col }
          },
          {
            value: Math.max(tot - val, 0),
            itemStyle: { color: 'rgba(255,255,255,.07)' }
          }
        ]
      }]
    });

    window.addEventListener('resize', function () { ec.resize(); });
  }

  /* 月度柱状图（近12月专利申请量） */
  function renderMonthly(trends) {
    var el = document.getElementById('echarts1');
    if (!el) return;
    if (cM) {
      try { cM.dispose(); } catch (e) {}
    }
    cM = echarts.init(el);

    var mo = trends.map(function (t) { return t.month; });
    var co = trends.map(function (t) { return t.count; });

    cM.setOption({
      animationDuration: 1200,
      animationEasing: 'cubicOut',
      animationDurationUpdate: 800,
      animationEasingUpdate: 'cubicInOut',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: 'rgba(0, 10, 40, 0.92)',
        borderColor: '#49bcf7',
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: '#e6f7ff', fontSize: 11 },
        extraCssText: 'box-shadow:0 0 18px rgba(0,0,0,.8);border-radius:4px;'
      },
      grid: { left: 0, top: 36, right: 15, bottom: 0, containLabel: true },
      xAxis: {
        type: 'category',
        data: mo,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: 'rgba(255,255,255,.7)',
          fontSize: 11,
          rotate: mo.length > 8 ? 30 : 0,
          interval: 0
        }
      },
      yAxis: {
        type: 'value',
        splitNumber: 4,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,.06)' } },
        axisLabel: { color: 'rgba(255,255,255,.7)', fontSize: 11 }
      },
      series: [{
        name: '专利申请量',
        type: 'bar',
        barMaxWidth: 36,
        data: co,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#7cf7ff' },
            { offset: 0.4, color: '#49bcf7' },
            { offset: 1, color: '#1565c0' }
          ]),
          borderRadius: [6, 6, 0, 0]
        },
        label: {
          show: true,
          position: 'top',
          color: 'rgba(255,255,255,.8)',
          fontSize: 11,
          formatter: function (p) {
            return p.value > 0 ? p.value.toLocaleString() : '';
          }
        }
      }]
    });

    window.addEventListener('resize', function () { cM.resize(); });
  }

  /* 关键词饼图（按类别 / 关键词数量分布） */
  function renderKeywordPie(cs) {
    var el = document.getElementById('echarts2');
    if (!el) return;
    if (cK) {
      try { cK.dispose(); } catch (e) {}
    }
    cK = echarts.init(el);

    var kl = [];
    if (cs && cs.stats && cs.stats.length) {
      kl = cs.stats;
    } else if (cs && cs.keywords && cs.keywords.length) {
      var km = {};
      cs.keywords.forEach(function (k) {
        var c = k.category || '未分类';
        km[c] = (km[c] || 0) + 1;
      });
      Object.keys(km).forEach(function (c) {
        kl.push({ category: c, count: km[c] });
      });
    }

    var pal = ['#49bcf7', '#faad14', '#52c41a', '#ff4d4f', '#9254de', '#13c2c2', '#eb2f96', '#fa8c16', '#a0d911', '#1890ff'];
    var pd = kl.slice(0, 10).map(function (it, i) {
      return {
        name: it.category || '未分类',
        value: it.count,
        itemStyle: { color: pal[i % pal.length] }
      };
    });
    if (!pd.length) {
      pd = [{
        name: '暂无数据',
        value: 1,
        itemStyle: { color: '#234' }
      }];
    }

    cK.setOption({
      animationDuration: 1200,
      animationEasing: 'cubicOut',
      tooltip: {
        trigger: 'item',
        formatter: '{b}: {c}个 ({d}%)',
        backgroundColor: 'rgba(0, 10, 40, 0.92)',
        borderColor: '#49bcf7',
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: '#e6f7ff', fontSize: 11 },
        extraCssText: 'box-shadow:0 0 18px rgba(0,0,0,.8);border-radius:4px;'
      },
      legend: {
        orient: 'vertical',
        right: 8,
        top: 'middle',
        textStyle: { color: 'rgba(255,255,255,.8)', fontSize: 11 },
        itemWidth: 10,
        itemHeight: 10
      },
      series: [{
        type: 'pie',
        radius: ['40%', '64%'],
        center: ['36%', '50%'],
        label: { show: false },
        labelLine: { show: false },
        data: pd
      }]
    });

    window.addEventListener('resize', function () { cK.resize(); });
  }

  /* 近 12 月预警趋势 + 专利量折线 */
  function renderTrend(da, mt) {
    var el = document.getElementById('echarts3');
    if (!el) return;
    if (cT) {
      try { cT.dispose(); } catch (e) {}
    }
    cT = echarts.init(el);

    var lb = (da || []).map(function (d) { return d.date; });
    var ac = (da || []).map(function (d) { return d.count; });

    var mm = {};
    (mt || []).forEach(function (t) {
      mm[t.month] = t.count;
    });
    var pc = lb.map(function (l) { return mm[l] || 0; });

    cT.setOption({
      animationDuration: 1200,
      animationEasing: 'cubicOut',
      tooltip: {
        trigger: 'axis',
        axisPointer: { lineStyle: { color: '#49bcf7' } },
        backgroundColor: 'rgba(0, 10, 40, 0.92)',
        borderColor: '#49bcf7',
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: '#e6f7ff', fontSize: 11 },
        extraCssText: 'box-shadow:0 0 18px rgba(0,0,0,.8);border-radius:4px;'
      },
      legend: {
        data: ['预警数量', '专利申请量'],
        right: 10,
        top: 4,
        textStyle: { color: 'rgba(255,255,255,.8)', fontSize: 11 },
        itemWidth: 12,
        itemHeight: 8
      },
      grid: { left: 0, top: 40, right: 15, bottom: 0, containLabel: true },
      xAxis: [{
        type: 'category',
        boundaryGap: false,
        data: lb,
        axisLine: { lineStyle: { color: 'rgba(255,255,255,.2)' } },
        axisLabel: { color: 'rgba(255,255,255,.8)', fontSize: 11 }
      }],
      yAxis: [{
        type: 'value',
        axisTick: { show: false },
        axisLine: { lineStyle: { color: 'rgba(255,255,255,.2)' } },
        axisLabel: { color: 'rgba(255,255,255,.8)', fontSize: 11 },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,.08)' } }
      }],
      series: [
        {
          name: '预警数量',
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 6,
          showSymbol: true,
          lineStyle: { color: '#ff4d4f', width: 2 },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(255,77,79,.45)' },
              { offset: 1, color: 'rgba(255,77,79,.02)' }
            ])
          },
          itemStyle: { color: '#ff4d4f' },
          data: ac,
          markLine: {
            silent: true,
            symbol: 'none',
            label: { color: 'rgba(255,255,255,.65)', fontSize: 10 },
            lineStyle: { color: 'rgba(255,77,79,.45)', type: 'dashed' },
            data: [
              { yAxis: 50, name: '风险关注阈值' }
            ]
          }
        },
        {
          name: '专利申请量',
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 6,
          showSymbol: true,
          lineStyle: { color: '#49bcf7', width: 2 },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(73,188,247,.4)' },
              { offset: 1, color: 'rgba(73,188,247,.02)' }
            ])
          },
          itemStyle: { color: '#49bcf7' },
          data: pc
        }
      ]
    });

    window.addEventListener('resize', function () { cT.resize(); });
  }

  /* 威胁申请人风险条形图 */
  function renderRiskBar(tc) {
    var el = document.getElementById('lastecharts');
    if (!el) return;
    if (cR) {
      try { cR.dispose(); } catch (e) {}
    }
    cR = echarts.init(el);

    var data = (tc || []).slice(0, 8);
    if (!data.length) {
      cR.setOption({
        graphic: [{
          type: 'text',
          left: 'center',
          top: 'middle',
          style: {
            text: '暂无威胁申请人数据',
            fill: 'rgba(255,255,255,.4)',
            fontSize: 14
          }
        }]
      });
      return;
    }

    var nm = data.map(function (d) {
      var n = d.company || '未知';
      return n.length > 10 ? n.substring(0, 10) + '...' : n;
    });
    var sc = data.map(function (d) { return +(d.avg_score || 0).toFixed(1); });
    var ct = data.map(function (d) { return d.count || 0; });

    cR.setOption({
      animationDuration: 1200,
      animationEasing: 'cubicOut',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: function (params) {
          var i = params[0].dataIndex;
          return (data[i].company || '未知') +
            '<br/>平均风险分: ' + sc[i] +
            '<br/>预警数量: ' + ct[i];
        },
        backgroundColor: 'rgba(0, 10, 40, 0.92)',
        borderColor: '#49bcf7',
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: '#e6f7ff', fontSize: 11 },
        extraCssText: 'box-shadow:0 0 18px rgba(0,0,0,.8);border-radius:4px;'
      },
      grid: { left: 0, top: 8, right: 58, bottom: 0, containLabel: true },
      xAxis: {
        type: 'value',
        max: 100,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,.06)' } },
        axisLabel: { color: 'rgba(255,255,255,.7)', fontSize: 10 }
      },
      yAxis: {
        type: 'category',
        data: nm,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: 'rgba(255,255,255,.85)', fontSize: 11 }
      },
      series: [{
        type: 'bar',
        barMaxWidth: 18,
        data: sc.map(function (s) {
          var c = s >= 70 ? '#ff4d4f' : (s >= 50 ? '#faad14' : '#52c41a');
          var ca = s >= 70 ? 'rgba(255,77,79,.35)' : (s >= 50 ? 'rgba(250,173,20,.35)' : 'rgba(82,196,26,.35)');
          return {
            value: s,
            itemStyle: {
              color: new echarts.graphic.LinearGradient(1, 0, 0, 0, [
                { offset: 0, color: c },
                { offset: 1, color: ca }
              ]),
              borderRadius: [0, 4, 4, 0]
            }
          };
        }),
        label: {
          show: true,
          position: 'right',
          color: 'rgba(255,255,255,.9)',
          fontSize: 11,
          formatter: function (p) { return p.value + '分'; }
        },
        emphasis: {
          focus: 'series',
          itemStyle: {
            shadowBlur: 18,
            shadowColor: 'rgba(73,188,247,.8)'
          },
          label: { color: '#fff' }
        }
      }]
    });

    window.addEventListener('resize', function () { cR.resize(); });
  }

  /* 右下排行榜（关键词专利量） */
  function renderPaimList(cs) {
    var el = document.getElementById('paim-list');
    if (!el) return;

    var raw = [];
    if (cs && cs.keywords && cs.keywords.length) {
      raw = cs.keywords.slice(0, 8);
    }
    if (!raw.length) {
      el.innerHTML = '<div style="color:rgba(255,255,255,.4);text-align:center;padding-top:30px">暂无数据</div>';
      return;
    }

    var counts = raw.map(function (k) { return k.patentCount || 0; });
    var maxC = Math.max.apply(null, counts) || 1;
    var rc = ['#ed405d', '#f78c44', '#49bcf7', '#49bcf7', '#49bcf7', '#49bcf7', '#49bcf7', '#49bcf7'];

    var html = '<ul style="height:100%;padding:0;margin:0;list-style:none;">';
    raw.forEach(function (k, i) {
      var pct = Math.round((k.patentCount || 0) / maxC * 100) || 5;
      var bgc = rc[i] || '#49bcf7';
      html += '<li style="display:flex;align-items:center;height:12.5%;padding:0 8px;">';
      html += '<span style="width:26px;height:26px;text-align:center;line-height:26px;background:' + bgc +
        ';border-radius:4px;margin-right:8px;font-size:14px;flex-shrink:0;box-shadow:0 0 10px rgba(0,0,0,.7);">' + (i + 1) + '</span>';
      html += '<div style="flex:1;min-width:0;">';
      html += '<p style="color:rgba(255,255,255,.85);font-size:13px;margin:0 0 3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' +
        esc(k.keyword || '未知') + '</p>';
      html += '<div style="display:flex;align-items:center;gap:6px;">';
      html += '<div style="flex:1;height:8px;background:rgba(255,255,255,.1);border-radius:4px;overflow:hidden;box-shadow:inset 0 0 6px rgba(0,0,0,.7);">';
      html += '<div style="width:' + pct + '%;height:100%;background:linear-gradient(90deg,' + bgc +
        ',rgba(73,188,247,.7));border-radius:4px;"></div></div>';
      html += '<span style="color:#7cf7ff;font-size:12px;flex-shrink:0;text-shadow:0 0 6px rgba(0,0,0,.8);">' +
        (k.patentCount || 0) + '件</span>';
      html += '</div></div></li>';
    });
    html += '</ul>';
    el.innerHTML = html;
  }

  function applyStageEntrance() {
    var cards = document.querySelectorAll('.main-grid .card');
    for (var i = 0; i < cards.length; i++) {
      var delay = i * 80;
      cards[i].style.animationDelay = delay + 'ms';
      cards[i].classList.add('stage-entered');
    }
  }

  function markPriorityCard() {
    var titles = document.querySelectorAll('.card-title');
    for (var i = 0; i < titles.length; i++) {
      var t = (titles[i].textContent || '').trim();
      if (t.indexOf('TOP威胁申请人风险评分') !== -1) {
        var card = titles[i].closest('.card');
        if (card) card.classList.add('focus-priority');
        break;
      }
    }
  }

  function startGuideTour() {
    var titleOrder = [
      '风险等级分布',
      '近12月专利预警趋势',
      '月度专利申请量（近12月）',
      '关键词专利数量分布',
      'TOP威胁申请人风险评分',
      '关键词专利量排行榜'
    ];

    function getCardByTitle(title) {
      var all = document.querySelectorAll('.card-title');
      for (var i = 0; i < all.length; i++) {
        if ((all[i].textContent || '').trim() === title) {
          return all[i].closest('.card');
        }
      }
      return null;
    }

    var queue = titleOrder.map(getCardByTitle).filter(Boolean);
    if (!queue.length) return;

    var idx = 0;
    function run() {
      var prev = document.querySelector('.card.guide-active');
      if (prev) prev.classList.remove('guide-active');

      var card = queue[idx % queue.length];
      if (card) card.classList.add('guide-active');

      idx += 1;
    }

    run();
    setInterval(run, 5500);
  }

  /* ========== 仪表盘整体渲染入口 ========== */
  function renderDashboard(d) {
    var st = d.stats || {};
    var dist = st.risk_distribution || {};
    var high = dist['高'] || 0;
    var mid = dist['中'] || 0;
    var low = dist['低'] || 0;
    var totalAlerts = st.total_alerts || 0;
    var total = Math.max(totalAlerts, 1);

    /* 左右 + 中间 KPI 数字动画 */
    animateNumber('kpi-patents', st.total_patents || 0, '件', 0);
    animateNumber('kpi-alerts', totalAlerts || 0, '件', 0);
    animateNumber('kpi-avgscore', +(st.avg_risk_score || 0), '分', 1);

    animateNumber('kpi-recent', st.recent_alerts || 0, '', 0);
    animateNumber('kpi-keywords', (d.category_stats && d.category_stats.total) || 0, '个', 0);
    animateNumber('kpi-pdfs', st.total_pdfs || 0, '', 0);

    animateNumber('kpi-high', high || 0, '件', 0);
    animateNumber('kpi-mid', mid || 0, '件', 0);
    animateNumber('kpi-low', low || 0, '件', 0);

    updateRiskBars(high, mid, low);

    renderPie('pe01', high, total, '#ff4d4f');
    renderPie('pe02', mid, total, '#faad14');
    renderPie('pe03', low, total, '#52c41a');

    renderMonthly(d.monthly_trends || []);
    renderKeywordPie(d.category_stats || {});
    renderTrend(st.daily_alerts || [], d.monthly_trends || []);
    renderRiskBar(d.threat_companies || []);
  }

  /* ========== 数据请求 ========== */
  function loadKeywords() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/keywords', true);
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var res = JSON.parse(xhr.responseText);
          if (res.success && res.result) renderPaimList(res.result);
        } catch (e) { }
      }
    };
    xhr.send();
  }

  function hideLoading() {
    var el = document.querySelector('.loading');
    if (el) {
      el.style.transition = 'opacity .6s';
      el.style.opacity = '0';
      setTimeout(function () { el.style.display = 'none'; }, 650);
    }
  }

  function loadData(cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/dashboard', true);
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          renderDashboard(JSON.parse(xhr.responseText));
          hideLoading();
          if (typeof cb === 'function') cb(true);
        } catch (e) {
          console.error('dashboard parse error', e);
          hideLoading();
          if (typeof cb === 'function') cb(false);
        }
      } else {
        hideLoading();
        if (typeof cb === 'function') cb(false);
      }
    };
    xhr.onerror = function () {
      console.error('dashboard api error');
      hideLoading();
      if (typeof cb === 'function') cb(false);
    };
    xhr.send();
  }

  function triggerLatestUpdate() {
    var btn = document.getElementById('btn-update-latest');
    var status = document.getElementById('update-status');
    if (!btn || !status) return;

    if (btn.disabled) return;

    btn.disabled = true;
    status.textContent = '刷新中...';
    status.classList.remove('success', 'error');
    status.classList.add('running');

    var done = 0;
    function finish(ok) {
      done += 1;
      if (!ok) {
        status.textContent = '刷新失败';
        status.classList.remove('running', 'success');
        status.classList.add('error');
        btn.disabled = false;
        return;
      }
      if (done >= 2) {
        status.textContent = '刷新完成';
        status.classList.remove('running', 'error');
        status.classList.add('success');
        btn.disabled = false;
      }
    }

    loadData(function (ok) { finish(ok); });

    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/keywords', true);
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var res = JSON.parse(xhr.responseText);
          if (res.success && res.result) {
            renderPaimList(res.result);
            finish(true);
          } else {
            finish(false);
          }
        } catch (e) {
          finish(false);
        }
      } else {
        finish(false);
      }
    };
    xhr.onerror = function () { finish(false); };
    xhr.send();
  }

  function bindUpdateButton() {
    var btn = document.getElementById('btn-update-latest');
    if (!btn) return;
    btn.addEventListener('click', triggerLatestUpdate);
  }

  /* ========== 启动入口 ========== */
  document.addEventListener('DOMContentLoaded', function () {
    startClock();
    initParticles();
    applyStageEntrance();
    markPriorityCard();
    bindUpdateButton();
    loadData();
    loadKeywords();

    // 等首屏数据出来后再启动讲解动线
    setTimeout(startGuideTour, 1800);

    // 每 60s 自动刷新
    setInterval(function () {
      loadData();
      loadKeywords();
    }, 60000);
  });

})();
