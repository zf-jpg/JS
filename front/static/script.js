// 全局变量
let currentPatentPage = 1;
let currentAlertPage = 1;
const pageSize = 20;
let alertsLineChart = null;
let riskPieChart = null;
let currentKeywordFilter = '';
let isCrawling = false;
let currentTagPage = 1;
let tagHotChart = null;
let tagColdChart = null;
let tagHeatChart = null;
let tagLineTrendChart = null;
let isDownloadingReport = false;
let currentCategoryFilter = '';
let isUpdatingCompany = false;
let isUpdatingAll = false;
let coGraphList = [];
let crawlProgressTimer = null;
let crawlProgressVal = 0;
const crawlProgressMaxPages = 60; // 60页视为100%
const crawlProgressCap = 95; // 未完成前最多到95%，避免假满

// 简写显示用：控制坐标文字长度
function shortenText(text, maxLen = 8) {
    if (!text || typeof text !== 'string') return text;
    return text.length > maxLen ? text.slice(0, maxLen - 1) + '…' : text;
}

// 检查 Chart.js 是否已加载
function checkChartJS() {
    if (typeof Chart === 'undefined') {
        console.warn('Chart.js 未加载，图表功能将不可用');
        return false;
    }
    return true;
}

// Chart.js 加载成功回调
window.onChartJSLoaded = function() {
    console.log('Chart.js 已成功加载');
    // 如果页面已经初始化，可以重新初始化图表
    if (document.readyState === 'complete' || document.readyState === 'interactive') {
        // 如果统计页面已加载，重新加载图表
        const statsTab = document.getElementById('statistics-tab');
        if (statsTab && statsTab.classList.contains('active')) {
            loadStatistics();
        }
        // 如果标签页面已加载，重新加载标签图表
        const tagTab = document.getElementById('tag-tab');
        if (tagTab && tagTab.classList.contains('active')) {
            loadTagSummary();
        }
    }
};

// Chart.js 加载失败回调
window.onChartJSLoadFailed = function() {
    console.error('Chart.js 所有CDN源均加载失败');
    // 显示友好的错误提示，但不阻止页面其他功能
    const showWarning = sessionStorage.getItem('chartWarningShown') !== 'true';
    if (showWarning) {
        setTimeout(function() {
            // 使用更友好的通知方式
            const notification = document.createElement('div');
            notification.id = 'chartWarningNotification';
            notification.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
                color: white;
                padding: 16px 20px;
                border-radius: 8px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                z-index: 10000;
                max-width: 380px;
                font-size: 14px;
                line-height: 1.6;
                animation: slideInRight 0.3s ease-out;
            `;
            notification.innerHTML = `
                <div style="display: flex; align-items: flex-start; gap: 12px;">
                    <i class="fas fa-exclamation-triangle" style="font-size: 20px; flex-shrink: 0; margin-top: 2px;"></i>
                    <div style="flex: 1;">
                        <div style="font-weight: 600; margin-bottom: 6px;">图表库加载失败</div>
                        <div style="font-size: 13px; opacity: 0.95; margin-bottom: 10px;">
                            部分图表功能可能无法使用。请检查网络连接或刷新页面重试。
                        </div>
                        <button onclick="this.closest('#chartWarningNotification').remove(); sessionStorage.setItem('chartWarningShown', 'true');" 
                                style="padding: 6px 14px; background: rgba(255,255,255,0.25); border: 1px solid rgba(255,255,255,0.4); 
                                       border-radius: 4px; color: white; cursor: pointer; font-size: 12px; font-weight: 500;">
                            我知道了
                        </button>
                    </div>
                    <button onclick="this.closest('#chartWarningNotification').remove(); sessionStorage.setItem('chartWarningShown', 'true');" 
                            style="background: none; border: none; color: white; cursor: pointer; font-size: 20px; padding: 0; width: 24px; height: 24px; flex-shrink: 0; line-height: 1;">
                        ×
                    </button>
                </div>
            `;
            
            // 添加动画样式（如果还没有）
            if (!document.getElementById('chartWarningStyle')) {
                const style = document.createElement('style');
                style.id = 'chartWarningStyle';
                style.textContent = `
                    @keyframes slideInRight {
                        from {
                            transform: translateX(100%);
                            opacity: 0;
                        }
                        to {
                            transform: translateX(0);
                            opacity: 1;
                        }
                    }
                `;
                document.head.appendChild(style);
            }
            
            document.body.appendChild(notification);
            sessionStorage.setItem('chartWarningShown', 'true');
            
            // 8秒后自动消失
            setTimeout(function() {
                if (notification.parentNode) {
                    notification.style.animation = 'slideInRight 0.3s ease-out reverse';
                    setTimeout(function() {
                        if (notification.parentNode) {
                            notification.remove();
                        }
                    }, 300);
                }
            }, 8000);
        }, 1500);
    }
};

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', function() {
    // 初始化侧边栏状态
    initSidebar();
    // 立即初始化不依赖Chart.js的基础功能
    initTabs();

    const crawlInput = document.getElementById('crawlKeywordInput');
    if (crawlInput) {
        crawlInput.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                triggerCrawl();
            }
        });
    }

    loadKeywordOptions();
    loadCooccurrenceGraphs();
    
    // 等待Chart.js加载（最多等待5秒），然后加载统计数据
    let waitCount = 0;
    const maxWait = 50; // 50 * 100ms = 5秒
    
    function tryLoadData() {
        // 如果Chart.js已加载、已失败，或等待超时，都加载数据
        if (checkChartJS() || window.chartJSStatus === 'failed' || waitCount >= maxWait) {
            // 默认加载统计页面（第一个标签页）
            loadStatistics();
        } else {
            waitCount++;
            setTimeout(tryLoadData, 100);
        }
    }
    
    // 监听Chart.js加载事件
    window.addEventListener('chartjsloaded', function() {
        console.log('收到Chart.js加载成功事件');
        // 如果统计页面已激活，重新加载
        const statsTab = document.getElementById('statistics-tab');
        if (statsTab && statsTab.classList.contains('active')) {
            loadStatistics();
        }
        // 如果标签页面已激活，重新加载
        const tagTab = document.getElementById('tag-tab');
        if (tagTab && tagTab.classList.contains('active')) {
            loadTagSummary();
        }
    });
    
    // 开始尝试加载数据
    tryLoadData();
});

// 一键打开共现知识图谱（共现曲线模块生成的 HTML）
async function openCooccurrenceGraph() {
    try {
        if (!coGraphList.length) {
            alert('当前尚未生成共现知识图谱文件，请刷新列表或先运行标签分析生成图谱。');
            return;
        }
        const select = document.getElementById('coGraphSelect');
        let selected = [];
        if (select && select.value) {
            if (select.value === 'all') {
                selected = coGraphList.slice(0, 3); // 最多同时打开3个，避免被浏览器拦截
            } else {
                const found = coGraphList.find(f => f.url === select.value);
                if (found) selected = [found];
            }
        } else {
            selected = coGraphList.slice(0, 1); // 默认打开第一个
        }
        if (!selected.length) {
            alert('请选择要打开的图谱。');
            return;
        }
        selected.forEach(f => {
            const a = document.createElement('a');
            a.href = f.url;
            a.target = '_blank';
            a.rel = 'noopener';
            a.style.display = 'none';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        });
        if (select && select.value === 'all' && coGraphList.length > 3) {
            alert(`已尝试打开前 3 个图谱（共 ${coGraphList.length} 个）。如被拦截，请允许弹窗或手动点击剩余列表。`);
        }
    } catch (e) {
        alert('打开共现图谱失败：' + e.message);
    }
}

async function loadCooccurrenceGraphs() {
    try {
        const res = await fetch('/api/cooccurrence-graphs');
        const data = await res.json();
        if (!data.success) {
            alert('获取共现图谱列表失败：' + (data.error || '未知错误'));
            return;
        }
        coGraphList = data.result || [];
        const select = document.getElementById('coGraphSelect');
        if (!select) return;
        let html = '<option value="">请选择共现图谱</option>';
        if (coGraphList.length) {
            html += '<option value="all">全部（最多同时弹出3个窗口）</option>';
            coGraphList.forEach(f => {
                const safeName = f.name || f.url;
                html += `<option value="${f.url}">${safeName}</option>`;
            });
        }
        select.innerHTML = html;
    } catch (e) {
        alert('加载共现图谱列表失败：' + e.message);
    }
}

// 侧边栏展开/收缩功能
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const mainWrapper = document.getElementById('mainWrapper');

    if (sidebar) {
        sidebar.classList.toggle('collapsed');

        // 保存状态到 localStorage
        const isCollapsed = sidebar.classList.contains('collapsed');
        localStorage.setItem('sidebarCollapsed', isCollapsed);

        // 同步更新 body class 以便 CSS 选择器工作
        if (isCollapsed) {
            document.body.classList.add('sidebar-collapsed');
        } else {
            document.body.classList.remove('sidebar-collapsed');
        }
    }
}

// 初始化侧边栏状态
function initSidebar() {
    const sidebar = document.getElementById('sidebar');
    const savedState = localStorage.getItem('sidebarCollapsed');

    if (sidebar && savedState === 'true') {
        sidebar.classList.add('collapsed');
        document.body.classList.add('sidebar-collapsed');
    }
}

// 页面标题映射
const pageTitles = {
    'statistics': '<i class="fas fa-chart-bar"></i> 数据统计',
    'alerts': '<i class="fas fa-shield-alt"></i> 预警分析',
    'tag': '<i class="fas fa-tags"></i> 标签热度',
    'patents': '<i class="fas fa-file-alt"></i> 相关专利'
};

// 更新页面标题
function updatePageTitle(tabName) {
    const titleElement = document.getElementById('currentPageTitle');
    if (titleElement && pageTitles[tabName]) {
        titleElement.innerHTML = pageTitles[tabName];
    }
}

// 初始化标签页切换
function initTabs() {
    // 支持新的侧边栏导航按钮
    const navButtons = document.querySelectorAll('.nav-btn');
    // 也支持旧的 tab-btn（兼容性）
    const tabButtons = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');

    // 处理侧边栏导航按钮
    navButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.getAttribute('data-tab');

            // 移除所有活动状态
            navButtons.forEach(b => b.classList.remove('active'));
            tabButtons.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));

            // 激活当前标签
            btn.classList.add('active');
            // 同步激活对应的 tab-btn（如果存在）
            tabButtons.forEach(b => {
                if (b.getAttribute('data-tab') === targetTab) {
                    b.classList.add('active');
                }
            });
            document.getElementById(`${targetTab}-tab`).classList.add('active');

            // 更新页面标题
            updatePageTitle(targetTab);

            // 根据标签加载数据
            if (targetTab === 'patents') {
                loadPatents();
            } else if (targetTab === 'alerts') {
                loadAlerts();
            } else if (targetTab === 'tag') {
                loadTagSummary();
                loadTagResults();
            } else if (targetTab === 'statistics') {
                loadStatistics();
            } else if (targetTab === 'dashboard') {
                // 数据大屏通过iframe加载，无需额外处理
            }
        });
    });

    // 处理旧的标签页按钮（兼容性）
    tabButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.getAttribute('data-tab');

            // 移除所有活动状态
            tabButtons.forEach(b => b.classList.remove('active'));
            navButtons.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));

            // 激活当前标签
            btn.classList.add('active');
            // 同步激活对应的 nav-btn
            navButtons.forEach(b => {
                if (b.getAttribute('data-tab') === targetTab) {
                    b.classList.add('active');
                }
            });
            document.getElementById(`${targetTab}-tab`).classList.add('active');

            // 更新页面标题
            updatePageTitle(targetTab);

            // 根据标签加载数据
            if (targetTab === 'patents') {
                loadPatents();
            } else if (targetTab === 'alerts') {
                loadAlerts();
            } else if (targetTab === 'tag') {
                loadTagSummary();
                loadTagResults();
            } else if (targetTab === 'statistics') {
                loadStatistics();
            } else if (targetTab === 'dashboard') {
                // 数据大屏通过iframe加载，无需额外处理
            }
        });
    });

    // 搜索框回车事件
    document.getElementById('patentSearch').addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            searchPatents();
        }
    });

    document.getElementById('alertSearch').addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            searchAlerts();
        }
    });

    const tagSearch = document.getElementById('tagSearch');
    if (tagSearch) {
        tagSearch.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                searchTagResults();
            }
        });
    }
}

// 下载风险报告（PDF）
async function downloadReport() {
    if (isDownloadingReport) return;
    const btn = document.getElementById('downloadReportBtn');
    try {
        isDownloadingReport = true;
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 正在生成...';
        }
        const url = currentKeywordFilter ? `/api/report?keyword=${encodeURIComponent(currentKeywordFilter)}` : '/api/report';
        const res = await fetch(url);
        if (!res.ok) {
            let errMsg = '报告生成失败';
            try {
                const j = await res.json();
                errMsg = j.error || errMsg;
            } catch (_) {}
            throw new Error(errMsg);
        }
        const blob = await res.blob();
        const filename = `risk_report_${currentKeywordFilter || 'all'}.pdf`;
        const link = document.createElement('a');
        const objUrl = window.URL.createObjectURL(blob);
        link.href = objUrl;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        window.URL.revokeObjectURL(objUrl);
    } catch (e) {
        alert(e.message || '报告生成失败');
    } finally {
        isDownloadingReport = false;
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-file-pdf"></i> 下载风险报告';
        }
    }
}
// 一键运行预警分析
async function runAnalysis() {
    const btn = document.getElementById('runAnalysisBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 正在分析...';
    }
    try {
        const res = await fetch('/api/run-analysis', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            alert('分析完成，数据已更新');
            // 刷新所有相关数据
            loadStatistics();
            loadAlerts();
            loadPatents();
            loadTagSummary();
            loadTagResults();
        } else {
            alert('分析失败: ' + (data.error || '未知错误'));
            if (data.log) console.error('分析日志:', data.log);
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-play"></i> 一键运行分析';
        }
    }
}


// 加载统计数据
async function loadStatistics() {
    try {
        const url = currentKeywordFilter ? `/api/statistics?keyword=${encodeURIComponent(currentKeywordFilter)}` : '/api/statistics';
        const response = await fetch(url);
        const result = await response.json();
        
        if (result.success) {
            const stats = result.result;

            // 更新顶部统计
            document.getElementById('totalPatents').textContent = stats.total_patents || 0;
            document.getElementById('totalAlerts').textContent = stats.total_alerts || 0;

            // 更新侧边栏统计
            const sidebarPatents = document.getElementById('sidebarTotalPatents');
            const sidebarAlerts = document.getElementById('sidebarTotalAlerts');
            if (sidebarPatents) sidebarPatents.textContent = stats.total_patents || 0;
            if (sidebarAlerts) sidebarAlerts.textContent = stats.total_alerts || 0;

            // 更新统计卡片
            document.getElementById('statTotalPatents').textContent = stats.total_patents || 0;
            document.getElementById('statTotalAlerts').textContent = stats.total_alerts || 0;
            document.getElementById('statAvgScore').textContent = stats.avg_risk_score || 0;
            document.getElementById('statTotalPdfs').textContent = stats.total_pdfs || 0;
            document.getElementById('statRecentAlerts').textContent = stats.recent_alerts || 0;
            // 风险分布数
            const rd = stats.risk_distribution || {};
            const high = rd['高'] || 0;
            const mid = rd['中'] || 0;
            const low = rd['低'] || 0;
            const setVal = (id, val) => {
                const el = document.getElementById(id);
                if (el) el.textContent = val;
            };
            setVal('statHighAlerts', high);
            setVal('statMidAlerts', mid);
            setVal('statLowAlerts', low);
            
            // 更新风险分布
            updateRiskDistribution(rd);
            
            // 更新风险芯片与关键词芯片
            updateChips(rd);

            // 更新图表
            updateCharts({
                dailyAlerts: stats.daily_alerts || [],
                riskDistribution: stats.risk_distribution_list || []
            });
        } else {
            showError('加载统计数据失败: ' + result.error);
        }
    } catch (error) {
        showError('加载统计数据时发生错误: ' + error.message);
    }
}

// 更新风险分布图表
function updateRiskDistribution(distribution) {
    const container = document.getElementById('riskDistribution');
    container.innerHTML = '';
    
    const levels = [
        { 
            key: '高', 
            label: '高风险', 
            color: '#ef4444',
            gradient: 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)',
            icon: 'fas fa-exclamation-triangle'
        },
        { 
            key: '中', 
            label: '中风险', 
            color: '#f59e0b',
            gradient: 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)',
            icon: 'fas fa-exclamation-circle'
        },
        { 
            key: '低', 
            label: '低风险', 
            color: '#10b981',
            gradient: 'linear-gradient(135deg, #10b981 0%, #059669 100%)',
            icon: 'fas fa-check-circle'
        }
    ];
    
    levels.forEach((level, index) => {
        const count = distribution[level.key] || 0;
        const item = document.createElement('div');
        item.className = 'risk-item';
        item.style.animationDelay = `${index * 0.1}s`;
        item.innerHTML = `
            <h4 style="color: ${level.color}">
                <i class="${level.icon}"></i>
                ${level.label}
            </h4>
            <div class="risk-count" style="background: ${level.gradient}; -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;">
                ${count}
            </div>
        `;
        container.appendChild(item);
    });
}

// 更新图表（折线图 + 饼图）
function updateCharts(data) {
    const lineCtx = document.getElementById('alertsLineChart');
    const pieCtx = document.getElementById('riskPieChart');
    
    if (!lineCtx || !pieCtx) return;
    
    // 检查Chart.js是否可用
    if (!checkChartJS()) {
        // 显示友好的错误提示
        const showChartError = (ctx, title) => {
            if (ctx && ctx.parentElement) {
                ctx.parentElement.innerHTML = `
                    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; padding: 20px; color: #94a3b8; text-align: center;">
                        <i class="fas fa-exclamation-triangle" style="font-size: 48px; color: #f59e0b; margin-bottom: 16px;"></i>
                        <h4 style="color: #cbd5e1; margin-bottom: 8px;">${title}</h4>
                        <p style="font-size: 14px; line-height: 1.6;">图表库加载失败，无法显示图表<br>请检查网络连接或刷新页面重试</p>
                    </div>
                `;
            }
        };
        showChartError(lineCtx, '预警趋势图表');
        showChartError(pieCtx, '风险分布图表');
        return;
    }
    
    const dates = (data.dailyAlerts || []).map(d => d.date);
    const counts = (data.dailyAlerts || []).map(d => d.count);
    const maxCount = counts.length ? Math.max(...counts) : 0;
    const suggestedMax = maxCount ? Math.ceil(maxCount * 1.2) : 5;
    
    // 折线图
    if (alertsLineChart) {
        alertsLineChart.destroy();
    }
    alertsLineChart = new Chart(lineCtx, {
        type: 'line',
        data: {
            labels: dates,
            datasets: [{
                label: '预警数量',
                data: counts,
                fill: false,
                tension: 0.35,
                borderColor: '#6aa8ff',
                borderWidth: 2.5,
                backgroundColor: 'rgba(106, 168, 255, 0.18)',
                pointRadius: 5,
                pointHoverRadius: 8,
                pointBackgroundColor: '#3b82f6',
                pointBorderColor: '#0ea5e9',
                pointBorderWidth: 2
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            animation: {
                duration: 900,
                easing: 'easeInOutCubic',
                delay: (ctx) => ctx.dataIndex * 12
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: '#0f172a',
                    borderColor: '#334155',
                    borderWidth: 1,
                    padding: 10,
                    titleColor: '#e2e8f0',
                    bodyColor: '#cbd5e1'
                },
                filler: { propagate: false }
            },
            scales: {
                x: {
                    ticks: { color: '#1e293b', font: { weight: 'bold', size: 12 } },
                    grid: { color: 'rgba(148, 163, 184, 0.08)', borderDash: [4, 4] }
                },
                y: {
                    beginAtZero: true,
                    suggestedMax,
                    ticks: { color: '#1e293b', font: { weight: 'bold', size: 12 }, precision: 0 },
                    grid: { color: 'rgba(148, 163, 184, 0.1)', borderDash: [4, 4] }
                }
            }
        }
    });
    
    // 柱状图（风险等级分布）
    const pieLabels = (data.riskDistribution || []).map(r => r.level);
    const pieCounts = (data.riskDistribution || []).map(r => r.count);
    const pieColors = pieLabels.map(l => {
        if (l === '高') return '#f43f5e';
        if (l === '中') return '#fb923c';
        if (l === '低') return '#22c55e';
        return '#60a5fa';
    });
    
    // 已在函数开头检查，这里不需要重复检查
    if (riskPieChart) {
        riskPieChart.destroy();
    }
    riskPieChart = new Chart(pieCtx, {
        type: 'bar',
        data: {
            labels: pieLabels,
            datasets: [{
                label: '数量',
                data: pieCounts,
                backgroundColor: pieColors,
                borderColor: '#0f172a',
                borderWidth: 1.2,
                borderRadius: 10,
                maxBarThickness: 36,
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            animation: {
                duration: 750,
                easing: 'easeInOutCubic',
                delay: (ctx) => ctx.dataIndex * 25
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: '#0f172a',
                    borderColor: '#334155',
                    borderWidth: 1,
                    callbacks: {
                        label: ctx => {
                            const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
                            const val = ctx.parsed.x;
                            const pct = total ? ((val / total) * 100).toFixed(1) : 0;
                            return `${ctx.label}: ${val} (${pct}%)`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    beginAtZero: true,
                    ticks: { color: '#1e293b', font: { weight: 'bold', size: 12 }, precision: 0 },
                    grid: { color: 'rgba(148, 163, 184, 0.08)', borderDash: [4, 4] }
                },
                y: {
                    ticks: { color: '#1e293b', font: { weight: 'bold', size: 12 } },
                    grid: { display: false }
                }
            }
        }
    });
}

// 更新顶部芯片：关键词 + 风险分布
function updateChips(riskDistribution) {
    const kwChip = document.getElementById('chipKeyword');
    if (kwChip) {
        kwChip.textContent = currentKeywordFilter ? currentKeywordFilter : '全部关键词';
    }
    const total = Object.values(riskDistribution || {}).reduce((a, b) => a + (b || 0), 0);
    const high = riskDistribution['高'] || 0;
    const mid = riskDistribution['中'] || 0;
    const low = riskDistribution['低'] || 0;
    const pct = (v) => total ? ((v / total) * 100).toFixed(1) + '%' : '0%';
    const chipHigh = document.getElementById('chipHigh');
    const chipMid = document.getElementById('chipMid');
    const chipLow = document.getElementById('chipLow');
    if (chipHigh) chipHigh.textContent = `${high} (${pct(high)})`;
    if (chipMid) chipMid.textContent = `${mid} (${pct(mid)})`;
    if (chipLow) chipLow.textContent = `${low} (${pct(low)})`;
}

// 加载专利列表
async function loadPatents(page = 1, search = null) {
    try {
        currentPatentPage = page;
        const searchParam = search || document.getElementById('patentSearch').value || null;
        
        let url = `/api/patents?page=${page}&page_size=${pageSize}`;
        if (searchParam) {
            url += `&search=${encodeURIComponent(searchParam)}`;
        }
        if (currentKeywordFilter) {
            url += `&keyword=${encodeURIComponent(currentKeywordFilter)}`;
        }
        
        const response = await fetch(url);
        const result = await response.json();
        
        if (result.success) {
            displayPatents(result.result);
            updatePagination('patentsPagination', result.result, loadPatents);
        } else {
            showError('加载专利数据失败: ' + result.error);
        }
    } catch (error) {
        showError('加载专利数据时发生错误: ' + error.message);
    }
}

// 显示专利列表
function displayPatents(data) {
    const tbody = document.getElementById('patentsTableBody');
    
    if (!data.data || data.data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="loading">暂无数据</td></tr>';
        return;
    }
    
    tbody.innerHTML = data.data.map(patent => `
        <tr>
            <td class="nowrap">${patent.id || '-'}</td>
            <td>${patent.title || '-'}</td>
            <td>${patent.applicant || '-'}</td>
            <td class="nowrap">${patent.publication_no || '-'}</td>
            <td class="nowrap">${formatDate(patent.publication_date) || '-'}</td>
            <td class="nowrap">${formatDate(patent.application_date) || '-'}</td>
            <td>
                <button class="action-btn" onclick="showPatentDetail(${patent.id})">
                    <i class="fas fa-eye"></i> 查看详情
                </button>
            </td>
        </tr>
    `).join('');
    addCellTitle('patentsTable');
}

// 搜索专利
function searchPatents() {
    const search = document.getElementById('patentSearch').value;
    loadPatents(1, search);
}

// 加载预警分析结果
async function loadAlerts(page = 1, riskLevel = null, search = null) {
    try {
        currentAlertPage = page;
        const levelFilter = riskLevel || document.getElementById('riskLevelFilter').value || null;
        const searchParam = search || document.getElementById('alertSearch').value || null;
        
        let url = `/api/alerts?page=${page}&page_size=${pageSize}`;
        if (levelFilter) {
            url += `&risk_level=${encodeURIComponent(levelFilter)}`;
        }
        if (searchParam) {
            url += `&search=${encodeURIComponent(searchParam)}`;
        }
        if (currentKeywordFilter) {
            url += `&keyword=${encodeURIComponent(currentKeywordFilter)}`;
        }
        
        const response = await fetch(url);
        const result = await response.json();
        
        if (result.success) {
            displayAlerts(result.result);
            updatePagination('alertsPagination', result.result, loadAlerts);
        } else {
            showError('加载预警数据失败: ' + result.error);
        }
    } catch (error) {
        showError('加载预警数据时发生错误: ' + error.message);
    }
}

// 显示预警列表
function displayAlerts(data) {
    const tbody = document.getElementById('alertsTableBody');
    
    if (!data.data || data.data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">暂无数据</td></tr>';
        return;
    }
    
    tbody.innerHTML = data.data.map(alert => {
        const riskClass = getRiskClass(alert.risk_level);
        const deltaDisplay = alert.risk_delta ? 
            (alert.risk_delta > 0 ? `+${alert.risk_delta}` : alert.risk_delta) : '-';
        const deltaColor = alert.risk_delta > 0 ? '#ef4444' : alert.risk_delta < 0 ? '#10b981' : '';
        
        const rowHtml = `
            <tr>
                <td class="nowrap"><span class="risk-badge ${riskClass}">${alert.risk_level || '-'}</span></td>
                <td class="nowrap">
                    <strong style="color: ${getRiskColor(alert.risk_level)}">${alert.risk_score || '-'}</strong>
                    ${alert.risk_delta ? `<span style="color: ${deltaColor}; font-size: 0.85em;">(${deltaDisplay})</span>` : ''}
                </td>
                <td>${alert.title || '-'}</td>
                <td>${alert.applicants || '-'}</td>
                <td>${alert.risk_tags || '-'}</td>
                <td class="nowrap"><span class="risk-badge risk-medium">${alert.risk_confidence || '-'}</span></td>
                <td class="nowrap">${formatDate(alert.updated_at) || '-'}</td>
                <td>
                    <button class="action-btn secondary" onclick="showAlertDetail(${alert.patent_id})">
                        <i class="fas fa-info-circle"></i> 详情
                    </button>
                </td>
            </tr>
        `;
        return rowHtml;
    }).join('');
    addCellTitle('alertsTable');
}

// 过滤预警
function filterAlerts() {
    const riskLevel = document.getElementById('riskLevelFilter').value;
    loadAlerts(1, riskLevel);
}

// 搜索预警
function searchAlerts() {
    const search = document.getElementById('alertSearch').value;
    loadAlerts(1, null, search);
}

// -------------------- 标签热度模块 --------------------
async function loadTagSummary() {
    try {
        let url = '/api/tag-summary';
        if (currentKeywordFilter) {
            url += `?keyword=${encodeURIComponent(currentKeywordFilter)}`;
        }
        const res = await fetch(url);
        const result = await res.json();
        if (!result.success) {
            showError('加载标签汇总失败: ' + result.error);
            return;
        }
        const data = result.result || {};
        const summary = data.summary || {};
        const hot = summary.hot || [];
        const cold = summary.cold || [];
        const co = summary.co_occurrence_top || [];

        const setText = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };

        setText('tagGeneratedAt', data.generated_at || '--');
        setText('tagHotCount', hot.length);
        setText('tagColdCount', cold.length);

        const renderList = (tbodyId, list) => {
            const tbody = document.getElementById(tbodyId);
            if (!tbody) return;
            if (!list.length) {
                tbody.innerHTML = `<tr><td colspan="2" class="loading">暂无数据</td></tr>`;
                return;
            }
            tbody.innerHTML = list
                .slice(0, 20)
                .map(item => `<tr><td>${item.label || '-'}</td><td>${item.count || 0}</td></tr>`)
                .join('');
        };
        renderList('tagHotBody', hot);
        renderList('tagColdBody', cold);
        updateTagCharts(hot, cold);
        updateTagExtraCharts(co);

        const coBody = document.getElementById('tagCoBody');
        if (coBody) {
            if (!co.length) {
                coBody.innerHTML = `<tr><td colspan="2" class="loading">暂无数据</td></tr>`;
            } else {
                coBody.innerHTML = co
                    .slice(0, 30)
                    .map(item => `<tr><td>${(item.pair || []).join(' / ')}</td><td>${item.count || 0}</td></tr>`)
                    .join('');
            }
        }
    } catch (err) {
        showError('加载标签汇总异常: ' + err.message);
    }
}

function updateTagCharts(hot, cold) {
    if (!checkChartJS()) {
        // 显示友好的错误提示
        const showChartError = (ctxId, title) => {
            const ctx = document.getElementById(ctxId);
            if (ctx && ctx.parentElement) {
                ctx.parentElement.innerHTML = `
                    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; padding: 20px; color: #94a3b8; text-align: center;">
                        <i class="fas fa-exclamation-triangle" style="font-size: 48px; color: #f59e0b; margin-bottom: 16px;"></i>
                        <h4 style="color: #cbd5e1; margin-bottom: 8px;">${title}</h4>
                        <p style="font-size: 14px; line-height: 1.6;">图表库加载失败，无法显示图表<br>请检查网络连接或刷新页面重试</p>
                    </div>
                `;
            }
        };
        showChartError('tagHotChart', '热门标签图表');
        showChartError('tagColdChart', '冷门标签图表');
        return;
    }
    const buildDataset = (list, colorMain, colorBorder) => {
        const top = (list || []).slice(0, 10);
        return {
            labels: top.map(i => i.label || '-'),
            data: top.map(i => i.count || 0),
            colorMain,
            colorBorder
        };
    };
    const hotData = buildDataset(hot, '#f97316', '#fb923c');
    const coldData = buildDataset(cold, '#60a5fa', '#93c5fd');

    const renderBar = (ctxId, chartRef, dataObj) => {
        const ctx = document.getElementById(ctxId);
        if (!ctx) return chartRef;
        if (chartRef) chartRef.destroy();
        return new Chart(ctx, {
            type: 'bar',
            data: {
                labels: dataObj.labels,
                datasets: [{
                    label: '出现次数',
                    data: dataObj.data,
                    backgroundColor: dataObj.colorMain + 'cc',
                    borderColor: dataObj.colorBorder,
                    borderWidth: 1.2,
                    borderRadius: 8,
                    maxBarThickness: 32
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: '#0f172a',
                        borderColor: '#334155',
                        borderWidth: 1,
                        callbacks: {
                            label: ctx => `${ctx.label}: ${ctx.formattedValue}`
                        }
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            color: '#1e293b',
                            font: { weight: 'bold', size: 11 },
                            callback: (val, idx, ticks) => shortenText(dataObj.labels[idx])
                        },
                        grid: { color: 'rgba(148, 163, 184, 0.08)', borderDash: [4,4] }
                    },
                    y: {
                        beginAtZero: true,
                        ticks: { color: '#1e293b', font: { weight: 'bold', size: 12 }, precision: 0 },
                        grid: { color: 'rgba(148, 163, 184, 0.08)', borderDash: [4,4] }
                    }
                },
                animation: {
                    duration: 700,
                    easing: 'easeOutCubic',
                    delay: (ctx) => ctx.dataIndex * 25
                }
            }
        });
    };

    tagHotChart = renderBar('tagHotChart', tagHotChart, hotData);
    tagColdChart = renderBar('tagColdChart', tagColdChart, coldData);
}

function updateTagExtraCharts(co) {
    if (!checkChartJS()) {
        // 显示友好的错误提示
        const showChartError = (ctxId, title) => {
            const ctx = document.getElementById(ctxId);
            if (ctx && ctx.parentElement) {
                ctx.parentElement.innerHTML = `
                    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; padding: 20px; color: #94a3b8; text-align: center;">
                        <i class="fas fa-exclamation-triangle" style="font-size: 48px; color: #f59e0b; margin-bottom: 16px;"></i>
                        <h4 style="color: #cbd5e1; margin-bottom: 8px;">${title}</h4>
                        <p style="font-size: 14px; line-height: 1.6;">图表库加载失败，无法显示图表<br>请检查网络连接或刷新页面重试</p>
                    </div>
                `;
            }
        };
        showChartError('tagHeatmap', '共现热力图');
        showChartError('tagLineTrend', '共现趋势图');
        return;
    }
    // Heatmap using scatter-like bubbles
    const heatCtx = document.getElementById('tagHeatmap');
    if (heatCtx) {
        if (tagHeatChart) tagHeatChart.destroy();
        const pairs = (co || []).slice(0, 40);
        // collect labels
        const labels = new Set();
        pairs.forEach(p => (p.pair || []).forEach(x => labels.add(x)));
        const labelList = Array.from(labels);
        const dataPoints = pairs.map(p => {
            const [a, b] = p.pair || [];
            const v = p.count || 0;
            return { x: a, y: b, v };
        });
        const maxV = Math.max(...dataPoints.map(d => d.v || 0), 1);
        tagHeatChart = new Chart(heatCtx, {
            type: 'bubble',
            data: {
                datasets: [{
                    label: '共现',
                    data: dataPoints.map(d => ({
                        x: d.x,
                        y: d.y,
                        r: 6 + (d.v / maxV) * 14,
                        v: d.v
                    })),
                    backgroundColor: dataPoints.map(d => {
                        const ratio = d.v / maxV;
                        return `rgba(99, 102, 241, ${0.35 + ratio * 0.5})`;
                    }),
                    borderColor: '#6366f1',
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: ctx => {
                                const d = ctx.raw;
                                return `${d.x} / ${d.y}: ${d.v || 0}`;
                            }
                        },
                        backgroundColor: '#0f172a',
                        borderColor: '#334155',
                        borderWidth: 1
                    }
                },
                scales: {
                    x: {
                        type: 'category',
                        labels: labelList,
                        ticks: {
                            color: '#1e293b',
                            font: { weight: 'bold', size: 11 },
                            maxRotation: 45,
                            minRotation: 35,
                            callback: (val, idx) => shortenText(labelList[idx], 6)
                        },
                        grid: { color: 'rgba(148,163,184,0.06)', borderDash: [4,4] }
                    },
                    y: {
                        type: 'category',
                        labels: labelList,
                        ticks: {
                            color: '#1e293b',
                            font: { weight: 'bold', size: 11 },
                            callback: (val, idx) => shortenText(labelList[idx], 6)
                        },
                        grid: { color: 'rgba(148,163,184,0.06)', borderDash: [4,4] }
                    }
                },
                animation: {
                    duration: 700,
                    easing: 'easeOutCubic',
                    delay: ctx => ctx.dataIndex * 15
                }
            }
        });
    }

    // Line trend from top co-occurrence counts (rank vs count)
    const lineCtx = document.getElementById('tagLineTrend');
    if (lineCtx) {
        if (tagLineTrendChart) tagLineTrendChart.destroy();
        // 已在函数开头检查，这里不需要重复检查
        const top = (co || []).slice(0, 20);
        tagLineTrendChart = new Chart(lineCtx, {
            type: 'line',
            data: {
                labels: top.map((p, idx) => `${idx + 1}.${(p.pair || []).join('/')}`),
                datasets: [{
                    label: '共现次数',
                    data: top.map(p => p.count || 0),
                    borderColor: '#34d399',
                    backgroundColor: 'rgba(52,211,153,0.16)',
                    tension: 0.35,
                    borderWidth: 2.5,
                    pointRadius: 5,
                    pointBackgroundColor: '#10b981',
                    pointBorderColor: '#34d399'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: '#0f172a',
                        borderColor: '#334155',
                        borderWidth: 1
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            color: '#1e293b',
                            font: { weight: 'bold', size: 11 },
                            maxRotation: 45,
                            minRotation: 20,
                            callback: (val, idx) => shortenText(top[idx]?.pair?.join('/') || '', 10)
                        },
                        grid: { color: 'rgba(148,163,184,0.08)', borderDash: [4,4] }
                    },
                    y: { beginAtZero: true, ticks: { color: '#1e293b', font: { weight: 'bold', size: 12 }, precision: 0 }, grid: { color: 'rgba(148,163,184,0.08)', borderDash: [4,4] } }
                },
                animation: { duration: 700, easing: 'easeOutCubic', delay: ctx => ctx.dataIndex * 20 }
            }
        });
    }
}

async function loadTagResults(page = 1, search = null) {
    try {
        currentTagPage = page;
        const searchParam = search || document.getElementById('tagSearch').value || null;
        let url = `/api/tag-results?page=${page}&page_size=${pageSize}`;
        if (searchParam) {
            url += `&search=${encodeURIComponent(searchParam)}`;
        }
        if (currentKeywordFilter) {
            url += `&keyword=${encodeURIComponent(currentKeywordFilter)}`;
        }
        const res = await fetch(url);
        const result = await res.json();
        if (!result.success) {
            showError('加载标签命中结果失败: ' + result.error);
            return;
        }
        displayTagResults(result.result);
        updatePagination('tagResultPagination', result.result, loadTagResults);
    } catch (err) {
        showError('加载标签命中结果异常: ' + err.message);
    }
}

function displayTagResults(data) {
    const tbody = document.getElementById('tagResultBody');
    if (!tbody) return;
    if (!data.data || data.data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="loading">暂无数据</td></tr>';
        return;
    }
    tbody.innerHTML = data.data.map(item => {
        const safeItem = JSON.stringify(item).replace(/'/g, "\\'");
        return `
            <tr>
                <td class="nowrap">${item.patent_id || '-'}</td>
                <td>${item.title || '-'}</td>
                <td>${item.language || '-'}</td>
                <td class="nowrap">${item.total_matches || 0}</td>
                <td class="nowrap">${item.updated_at || '-'}</td>
                <td>
                    <button class="action-btn" onclick='showTagDetail(${safeItem})'>
                        <i class="fas fa-eye"></i> 详情
                    </button>
                </td>
            </tr>
        `;
    }).join('');
    addCellTitle('tagResultTable');
}

function searchTagResults() {
    const val = document.getElementById('tagSearch').value;
    loadTagResults(1, val);
}

function showTagDetail(item) {
    const body = document.getElementById('modalBody');
    const title = document.getElementById('modalTitle');
    if (!body || !title) return;
    title.textContent = `标签详情 - ${item.title || item.patent_id || ''}`;
    const themes = item.themes || {};
    const themeBlocks = Object.entries(themes).map(([theme, detail]) => {
        const third = detail.third_level_counts || {};
        const top = Object.entries(third)
            .sort((a,b)=>b[1]-a[1])
            .slice(0,8)
            .map(([k,v])=>`<span class="pill-badge">${k}: ${v}</span>`)
            .join(' ');
        return `
            <div class="modal-section">
                <h4>${theme} <small>总命中: ${detail.total_matches || 0}</small></h4>
                <div class="pill-row">${top || '<span class="muted">无</span>'}</div>
            </div>
        `;
    }).join('');

    body.innerHTML = `
        <div class="detail-grid">
            <div><strong>ID:</strong> ${item.patent_id || '-'}</div>
            <div><strong>语言:</strong> ${item.language || '-'}</div>
            <div><strong>总命中:</strong> ${item.total_matches || 0}</div>
            <div><strong>更新时间:</strong> ${item.updated_at || '-'}</div>
        </div>
        <div class="detail-block">
            <p><strong>标题:</strong> ${item.title || '-'}</p>
        </div>
        <div class="detail-block">
            <h4>主题命中</h4>
            ${themeBlocks || '<span class="muted">暂无命中</span>'}
        </div>
    `;
    openModal();
}
// 显示专利详情
async function showPatentDetail(patentId) {
    try {
        const response = await fetch(`/api/patents/${patentId}`);
        const result = await response.json();
        
        if (result.success) {
            const patent = result.result;
            const modalBody = document.getElementById('modalBody');
            
            modalBody.innerHTML = `
                <div class="detail-item">
                    <div class="detail-label">专利标题</div>
                    <div class="detail-value">${patent.title || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">申请号 (ANE)</div>
                    <div class="detail-value">${patent.ane || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">申请号</div>
                    <div class="detail-value">${patent.application_no || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">申请日期</div>
                    <div class="detail-value">${formatDate(patent.application_date) || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">公开号</div>
                    <div class="detail-value">${patent.publication_no || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">公开日期</div>
                    <div class="detail-value">${formatDate(patent.publication_date) || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">授权号</div>
                    <div class="detail-value">${patent.grant_no || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">授权日期</div>
                    <div class="detail-value">${formatDate(patent.grant_date) || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">主分类号</div>
                    <div class="detail-value">${patent.main_class || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">申请人</div>
                    <div class="detail-value">${patent.applicant || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">专利权人</div>
                    <div class="detail-value">${patent.patentee || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">发明人</div>
                    <div class="detail-value">${patent.inventors || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">摘要</div>
                    <div class="detail-value">${patent.abstract || '-'}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">PDF文件数量</div>
                    <div class="detail-value">${patent.pdf_count || 0}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">创建时间</div>
                    <div class="detail-value">${patent.created_at || '-'}</div>
                </div>
            `;
            
            document.getElementById('modalTitle').textContent = '专利详细信息';
            document.getElementById('detailModal').classList.add('active');
        } else {
            showError('加载专利详情失败: ' + result.error);
        }
    } catch (error) {
        showError('加载专利详情时发生错误: ' + error.message);
    }
}

// 显示预警详情
async function showAlertDetail(patentId) {
    try {
        // 先获取专利详情
        const patentResponse = await fetch(`/api/patents/${patentId}`);
        const patentResult = await patentResponse.json();
        
        if (!patentResult.success) {
            showError('加载专利详情失败');
            return;
        }
        
        // 获取预警详情
        const alertResponse = await fetch(`/api/alerts?page=1&page_size=1000`);
        const alertResult = await alertResponse.json();
        
        if (!alertResult.success) {
            showError('加载预警详情失败');
            return;
        }
        
        const alert = alertResult.result.data.find(a => a.patent_id === patentId);
        const patent = patentResult.result;
        
        if (!alert) {
            showError('未找到对应的预警信息');
            return;
        }
        
        const modalBody = document.getElementById('modalBody');
        const riskClass = getRiskClass(alert.risk_level);
        
        modalBody.innerHTML = `
            <div class="detail-item">
                <div class="detail-label">风险等级</div>
                <div class="detail-value">
                    <span class="risk-badge ${riskClass}" style="font-size: 1.1em; padding: 8px 15px;">
                        ${alert.risk_level || '-'}
                    </span>
                </div>
            </div>
            <div class="detail-item">
                <div class="detail-label">风险分数</div>
                <div class="detail-value">
                    <strong style="color: ${getRiskColor(alert.risk_level)}; font-size: 1.5em;">
                        ${alert.risk_score || '-'}
                    </strong>
                    ${alert.risk_delta ? 
                        `<span style="color: ${alert.risk_delta > 0 ? '#ef4444' : '#10b981'}; margin-left: 10px;">
                            (${alert.risk_delta > 0 ? '+' : ''}${alert.risk_delta})
                        </span>` : ''}
                </div>
            </div>
            <div class="detail-item">
                <div class="detail-label">置信度</div>
                <div class="detail-value">
                    <span class="risk-badge risk-medium">${alert.risk_confidence || '-'}</span>
                </div>
            </div>
            <div class="detail-item">
                <div class="detail-label">专利标题</div>
                <div class="detail-value">${alert.title || patent.title || '-'}</div>
            </div>
            <div class="detail-item">
                <div class="detail-label">申请人</div>
                <div class="detail-value">${alert.applicants || patent.applicant || '-'}</div>
            </div>
            <div class="detail-item">
                <div class="detail-label">发明人</div>
                <div class="detail-value">${alert.inventors || patent.inventors || '-'}</div>
            </div>
            <div class="detail-item">
                <div class="detail-label">公开日期</div>
                <div class="detail-value">${formatDate(alert.publication_date) || formatDate(patent.publication_date) || '-'}</div>
            </div>
            <div class="detail-item">
                <div class="detail-label">风险标签</div>
                <div class="detail-value">${alert.risk_tags || '-'}</div>
            </div>
            <div class="detail-item">
                <div class="detail-label">风险原因</div>
                <div class="detail-value" style="white-space: pre-wrap;">${alert.risk_reason || '-'}</div>
            </div>
            <div class="detail-item">
                <div class="detail-label">创建时间</div>
                <div class="detail-value">${alert.created_at || '-'}</div>
            </div>
            <div class="detail-item">
                <div class="detail-label">更新时间</div>
                <div class="detail-value">${alert.updated_at || '-'}</div>
            </div>
        `;
        
        document.getElementById('modalTitle').textContent = '预警分析详情';
        document.getElementById('detailModal').classList.add('active');
    } catch (error) {
        showError('加载预警详情时发生错误: ' + error.message);
    }
}

// 打开/关闭模态框
function openModal() {
    const modal = document.getElementById('detailModal');
    if (modal) modal.classList.add('active');
}

function closeModal() {
    document.getElementById('detailModal').classList.remove('active');
}

// 点击模态框外部关闭
document.getElementById('detailModal').addEventListener('click', function(e) {
    if (e.target === this) {
        closeModal();
    }
});

// 更新分页
function updatePagination(paginationId, data, loadFunction) {
    const pagination = document.getElementById(paginationId);
    if (!pagination) return;
    
    const totalPages = data.total_pages || 1;
    const currentPage = data.page || 1;
    
    let html = '';
    
    // 上一页按钮
    html += `<button ${currentPage === 1 ? 'disabled' : ''} onclick="${loadFunction.name}(${currentPage - 1})">
        <i class="fas fa-chevron-left"></i> 上一页
    </button>`;
    
    // 页码按钮
    const maxButtons = 5;
    let startPage = Math.max(1, currentPage - Math.floor(maxButtons / 2));
    let endPage = Math.min(totalPages, startPage + maxButtons - 1);
    
    if (endPage - startPage < maxButtons - 1) {
        startPage = Math.max(1, endPage - maxButtons + 1);
    }
    
    if (startPage > 1) {
        html += `<button onclick="${loadFunction.name}(1)">1</button>`;
        if (startPage > 2) {
            html += `<span class="page-info">...</span>`;
        }
    }
    
    for (let i = startPage; i <= endPage; i++) {
        html += `<button class="${i === currentPage ? 'active' : ''}" onclick="${loadFunction.name}(${i})">${i}</button>`;
    }
    
    if (endPage < totalPages) {
        if (endPage < totalPages - 1) {
            html += `<span class="page-info">...</span>`;
        }
        html += `<button onclick="${loadFunction.name}(${totalPages})">${totalPages}</button>`;
    }
    
    // 下一页按钮
    html += `<button ${currentPage === totalPages ? 'disabled' : ''} onclick="${loadFunction.name}(${currentPage + 1})">
        下一页 <i class="fas fa-chevron-right"></i>
    </button>`;
    
    // 页面信息
    html += `<span class="page-info">共 ${data.total || 0} 条，第 ${currentPage}/${totalPages} 页</span>`;
    
    pagination.innerHTML = html;
}

// 工具函数
function truncateText(text, maxLength) {
    if (!text) return '-';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
}

// 为表格单元格添加 title 提示，便于查看完整内容
function addCellTitle(tableId) {
    const table = document.getElementById(tableId);
    if (!table) return;
    table.querySelectorAll('tbody td').forEach(td => {
        const txt = (td.textContent || '').trim();
        if (txt) td.title = txt;
    });
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    // 如果是8位数字格式 (YYYYMMDD)，转换为 YYYY-MM-DD
    if (/^\d{8}$/.test(dateStr)) {
        return `${dateStr.substring(0, 4)}-${dateStr.substring(4, 6)}-${dateStr.substring(6, 8)}`;
    }
    return dateStr;
}

function getRiskClass(level) {
    if (level === '高') return 'risk-high';
    if (level === '中') return 'risk-medium';
    if (level === '低') return 'risk-low';
    return 'risk-medium';
}

function getRiskColor(level) {
    if (level === '高') return '#ef4444';
    if (level === '中') return '#f59e0b';
    if (level === '低') return '#10b981';
    return '#94a3b8';
}

function showError(message) {
    alert('错误: ' + message);
    console.error(message);
}

// 触发关键词爬取 + 预警分析
async function triggerCrawl() {
    const btn = document.getElementById('crawlKeywordBtn');
    const input = document.getElementById('crawlKeywordInput');
    const catInput = document.getElementById('keywordCategoryInput');
    const status = document.getElementById('crawlStatus');
    const stopBtn = document.getElementById('crawlStopBtn');
    if (!btn || !input) return;
    const kw = input.value.trim();
    const category = catInput ? catInput.value.trim() : '';
    if (!kw) {
        alert('请输入关键词');
        return;
    }
    isCrawling = true;
    btn.disabled = true;
    btn.textContent = '正在获取...';
    if (stopBtn) stopBtn.disabled = false;
    if (status) status.textContent = '正在获取，预计约1~2分钟，请稍候...';
    startCrawlProgress();
    try {
        const res = await fetch('/api/crawl', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keyword: kw, category })
        });
        const data = await res.json();
        if (data.success) {
            const tagInfo = data.logs && data.logs.tag_analysis ? 
                '\n\n标签分析：已使用该关键词的专属标签进行分析' : '';
            alert('获取与分析完成' + tagInfo);
            // 重新加载数据
            await loadKeywordOptions();
            loadStatistics();
            loadPatents();
            loadAlerts();
            if (status) status.textContent = '获取完成（已生成专属标签）';
            finishCrawlProgress(true);
        } else {
            const msg = data.error || '未知错误';
            alert('失败: ' + msg);
            if (data.log) console.error('后端日志:', data.log);
            if (status) status.textContent = '获取失败';
            finishCrawlProgress(false);
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
        if (status) status.textContent = '请求失败';
        finishCrawlProgress(false);
    } finally {
        isCrawling = false;
        btn.disabled = false;
        btn.textContent = '开始获取';
        if (stopBtn) stopBtn.disabled = true;
        setTimeout(() => {
            if (status) status.textContent = '';
        }, 3000);
    }
}

// 手动更新（公司/全部）
async function triggerUpdate(companyOnly = true) {
    const isCompany = !!companyOnly;
    if (isCompany ? isUpdatingCompany : isUpdatingAll) return;
    if (isCompany) {
        isUpdatingCompany = true;
    } else {
        isUpdatingAll = true;
    }
    const startBtn = document.getElementById(isCompany ? 'updateCompanyBtn' : 'updateAllBtn');
    const stopBtn = document.getElementById(isCompany ? 'stopCompanyUpdateBtn' : 'stopAllUpdateBtn');
    if (startBtn) {
        startBtn.disabled = true;
        startBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 更新中...';
    }
    if (stopBtn) stopBtn.disabled = false;
    try {
        const res = await fetch('/api/monthly-update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ company_only: companyOnly })
        });
        const data = await res.json();
        if (data.success) {
            alert('更新完成，结果可在日志中查看。');
            await loadKeywordOptions();
            loadStatistics();
            loadAlerts();
            loadPatents();
        } else {
            alert('更新失败: ' + (data.error || '请查看日志'));
            console.error('更新详情:', data.results || data.error);
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    } finally {
        if (isCompany) {
            isUpdatingCompany = false;
        } else {
            isUpdatingAll = false;
        }
        if (startBtn) {
            startBtn.disabled = false;
            startBtn.innerHTML = isCompany
                ? '<i class="fas fa-industry"></i> 更新公司类别'
                : '<i class="fas fa-globe"></i> 更新全部关键词';
        }
        if (stopBtn) stopBtn.disabled = true;
    }
}

// 停止爬取：终止进程并对已爬数据分析
async function stopCrawl() {
    const stopBtn = document.getElementById('crawlStopBtn');
    const status = document.getElementById('crawlStatus');
    const btn = document.getElementById('crawlKeywordBtn');
    if (stopBtn) stopBtn.disabled = true;
    if (btn) btn.disabled = true;
    if (status) status.textContent = '正在停止、生成标签并分析...';
    try {
        const res = await fetch('/api/stop-crawl', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            alert('已停止爬取并完成分析');
            await loadKeywordOptions();
            loadStatistics();
            loadPatents();
            loadAlerts();
            loadTagSummary();
            loadTagResults();
            finishCrawlProgress(false);
        } else {
            alert('停止失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    } finally {
        isCrawling = false;
        if (btn) {
            btn.disabled = false;
            btn.textContent = '开始获取';
        }
        if (stopBtn) stopBtn.disabled = true;
        if (status) status.textContent = '';
    }
}

// 抓取进度条逻辑：按60页视为100%，无真实页数时使用时间推进（每秒 ~1/60）
function startCrawlProgress() {
    const wrap = document.getElementById('crawlProgressWrapper');
    const bar = document.getElementById('crawlProgressBar');
    const txt = document.getElementById('crawlProgressText');
    crawlProgressVal = 0;
    if (wrap) wrap.style.display = 'block';
    if (bar) bar.style.width = '0%';
    if (txt) txt.textContent = '0%';
    if (crawlProgressTimer) {
        clearInterval(crawlProgressTimer);
        crawlProgressTimer = null;
    }
    // 每秒前进 100/60 ≈ 1.67%
    crawlProgressTimer = setInterval(() => {
        const step = 100 / crawlProgressMaxPages;
        crawlProgressVal = Math.min(crawlProgressCap, crawlProgressVal + step);
        updateCrawlProgressUI();
        if (crawlProgressVal >= crawlProgressCap) {
            clearInterval(crawlProgressTimer);
            crawlProgressTimer = null;
        }
    }, 1000);
}

function finishCrawlProgress(success) {
    if (crawlProgressTimer) {
        clearInterval(crawlProgressTimer);
        crawlProgressTimer = null;
    }
    crawlProgressVal = success ? 100 : crawlProgressVal;
    updateCrawlProgressUI();
    // 完成后延时隐藏
    setTimeout(() => {
        const wrap = document.getElementById('crawlProgressWrapper');
        if (wrap) wrap.style.display = 'none';
    }, 1500);
}

function updateCrawlProgressUI() {
    const bar = document.getElementById('crawlProgressBar');
    const txt = document.getElementById('crawlProgressText');
    const val = Math.round(crawlProgressVal);
    if (bar) bar.style.width = `${val}%`;
    if (txt) txt.textContent = `${val}%`;
}

// 停止更新（公司/全部），不会影响另一类按钮状态
async function stopUpdate(companyOnly = true) {
    const isCompany = !!companyOnly;
    const stopBtn = document.getElementById(isCompany ? 'stopCompanyUpdateBtn' : 'stopAllUpdateBtn');
    try {
        if (stopBtn) stopBtn.disabled = true;
        const res = await fetch('/api/stop-monthly-update', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            alert('已请求停止更新，当前轮次将尽快中止。');
        } else {
            alert('停止请求失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    } finally {
        if (isCompany) {
            isUpdatingCompany = false;
            const startBtn = document.getElementById('updateCompanyBtn');
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.innerHTML = '<i class="fas fa-industry"></i> 更新公司类别';
            }
        } else {
            isUpdatingAll = false;
            const startBtn = document.getElementById('updateAllBtn');
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.innerHTML = '<i class="fas fa-globe"></i> 更新全部关键词';
            }
        }
    }
}

// 拉取关键词选项，用于筛选（包含分类）
async function loadKeywordOptions() {
    const select = document.getElementById('keywordFilter');
    const catSelect = document.getElementById('keywordCategoryFilter');
    const deleteBtn = document.getElementById('deleteKeywordBtn');
    if (!select) return;
    try {
        const res = await fetch('/api/keywords');
        const data = await res.json();
        if (!data.success) return;
        const payload = data.result || {};
        const keywords = payload.keywords || [];
        const categories = payload.categories || [];
        const currentKw = select.value;

        // 分类下拉
        if (catSelect) {
            let catHtml = '<option value="">全部分类</option>';
            categories.forEach(c => {
                catHtml += `<option value="${c}">${c}</option>`;
            });
            catSelect.innerHTML = catHtml;
            catSelect.value = currentCategoryFilter;
        }

        // 关键词下拉
        let html = '<option value="">全部关键词</option>';
        const filtered = keywords.filter(k => !currentCategoryFilter || k.category === currentCategoryFilter);
        filtered.forEach(k => {
            const label = k.category ? `${k.keyword} (${k.category})` : k.keyword;
            const escapedVal = k.keyword.replace(/"/g, '&quot;');
            const escapedLabel = label.replace(/"/g, '&quot;');
            html += `<option value="${escapedVal}">${escapedLabel}</option>`;
        });
        select.innerHTML = html;

        // 如果当前选项仍存在，则保持；否则重置为空并刷新
        const exists = filtered.some(k => k.keyword === currentKw);
        if (currentKw && exists) {
            select.value = currentKw;
            currentKeywordFilter = currentKw;
        } else if (currentKw && !exists) {
            currentKeywordFilter = '';
            select.value = '';
            loadStatistics();
            loadPatents();
            loadAlerts();
        }
        // 更新删除按钮状态
        if (deleteBtn) {
            deleteBtn.disabled = !select.value || select.value === '';
        }
        select.onchange = () => {
            currentKeywordFilter = select.value || '';
            if (deleteBtn) {
                deleteBtn.disabled = !select.value || select.value === '';
            }
            loadStatistics();
            loadPatents();
            loadAlerts();
            if (document.getElementById('tag-tab')?.classList.contains('active')) {
                loadTagSummary();
                loadTagResults();
            }
        };
        if (catSelect) {
            catSelect.onchange = () => {
                currentCategoryFilter = catSelect.value;
                loadKeywordOptions();
            };
        }
    } catch (e) {
        console.error('加载关键词列表失败', e);
    }
}

// 删除关键词
async function deleteKeyword() {
    const select = document.getElementById('keywordFilter');
    const deleteBtn = document.getElementById('deleteKeywordBtn');
    if (!select || !select.value || select.value === '') {
        alert('请先选择一个关键词');
        return;
    }
    
    const keyword = select.value;
    if (!confirm(`确定要删除关键词 "${keyword}" 及其所有相关数据吗？\n\n此操作将删除：\n- 该关键词的所有专利记录\n- 相关的预警分析结果\n- 相关的标签分析结果\n- 相关的PDF文件\n- 该关键词的专属标签\n\n此操作不可恢复！`)) {
        return;
    }
    
    try {
        deleteBtn.disabled = true;
        deleteBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        
        const encodedKeyword = encodeURIComponent(keyword);
        const res = await fetch(`/api/keywords/${encodedKeyword}`, {
            method: 'DELETE'
        });
        const data = await res.json();
        
        if (data.success) {
            const result = data.result;
            const counts = result.deleted_counts || {};
            alert(`删除成功！\n\n已删除：\n- 专利: ${counts.patents || 0} 条\n- 预警: ${counts.risk_alerts || 0} 条\n- 标签分析: ${counts.tag_analysis_results || 0} 条\n- PDF文件: ${counts.pdfs || 0} 条\n- 关键词标签: ${counts.keyword_tags || 0} 条\n\n总计: ${result.total_deleted || 0} 条记录`);
            
            // 重置选择并刷新数据
            select.value = '';
            currentKeywordFilter = '';
            await loadKeywordOptions();
            loadStatistics();
            loadPatents();
            loadAlerts();
            loadTagSummary();
            loadTagResults();
        } else {
            alert('删除失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    } finally {
        if (deleteBtn) {
            deleteBtn.disabled = true;
            deleteBtn.innerHTML = '<i class="fas fa-trash-alt"></i>';
        }
    }
}

// ========================================
// 数据大屏相关函数
// ========================================

// 大屏图表实例
let dashboardCharts = {
    hourly: null,
    monthly: null,
    gaugeHigh: null,
    gaugeMid: null,
    gaugeLow: null,
    radar: null,
    echarts1: null,
    echarts2: null,
    echarts3: null,
    pe01: null,
    pe02: null,
    pe03: null,
    lastecharts: null
};

// 当前大屏关键词筛选
let currentDashboardKeyword = '';

// 加载大屏数据
async function loadDashboardData() {
    const keyword = document.getElementById('dashboardKeywordFilter')?.value || '';
    currentDashboardKeyword = keyword;

    try {
        const url = keyword ? `/api/dashboard?keyword=${encodeURIComponent(keyword)}` : '/api/dashboard';
        const res = await fetch(url);
        const data = await res.json();

        if (data.error) {
            console.error('加载大屏数据失败:', data.error);
            return;
        }

        // 更新指标卡片
        updateDashboardMetrics(data);

        // 初始化/更新图表
        initDashboardCharts(data);

    } catch (e) {
        console.error('加载大屏数据失败:', e);
    }
}

// 更新大屏指标卡片
function updateDashboardMetrics(data) {
    const stats = data.stats || {};
    const categoryStats = data.category_stats || { stats: [] };

    // 基础统计
    document.getElementById('dashTotalPatents').textContent = stats.total_patents || 0;
    document.getElementById('dashTotalAlerts').textContent = stats.total_alerts || 0;
    document.getElementById('dashAvgScore').textContent = stats.avg_risk_score || 0;
    document.getElementById('dashOverviewTotal').textContent = stats.total_patents || 0;

    // 关键词数量
    document.getElementById('dashKeywordCount').textContent = categoryStats.total || 0;

    // 分类统计
    const statsMap = {};
    categoryStats.stats.forEach(item => {
        statsMap[item.category || '未分类'] = item.count;
    });
    document.getElementById('dashCompanyCount').textContent = statsMap['公司'] || 0;
    document.getElementById('dashPersonCount').textContent = statsMap['人名'] || 0;
    document.getElementById('dashGeneralCount').textContent = statsMap['通用'] || 0;

    // 更新威胁公司排行榜
    updateThreatRanking(data.threat_companies || []);
}

// 更新威胁公司排行榜
function updateThreatRanking(companies) {
    const container = document.getElementById('threatRanking');
    if (!container) return;

    if (!companies || companies.length === 0) {
        container.innerHTML = '<p style="text-align:center;color:var(--text-secondary);padding:20px;">暂无数据</p>';
        return;
    }

    const maxValue = Math.max(...companies.map(c => c.count || 0));

    container.innerHTML = companies.map((company, index) => {
        const count = company.count || 0;
        const score = company.avg_score || 0;
        const percentage = maxValue > 0 ? (count / maxValue * 100) : 0;

        return `
            <div class="ranking-item">
                <span class="ranking-number">${index + 1}</span>
                <div class="ranking-info">
                    <div class="ranking-name">${company.company}</div>
                    <div class="ranking-bar">
                        <div class="ranking-progress">
                            <div class="ranking-progress-fill" style="width: ${percentage}%"></div>
                        </div>
                        <span class="ranking-value">${count}件</span>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

// 初始化大屏图表
function initDashboardCharts(data) {
    if (typeof echarts === 'undefined') {
        console.error('ECharts 未加载');
        return;
    }

    // 时段分布折线图
    initHourlyChart(data.hourly_alerts || []);

    // 月度趋势柱状图
    initMonthlyChart(data.monthly_trends || []);

    // 风险占比仪表盘
    const riskDist = data.stats?.risk_distribution || {};
    const total = (riskDist['高'] || 0) + (riskDist['中'] || 0) + (riskDist['低'] || 0);
    initGaugeCharts(riskDist, total);

    // 雷达图
    initRadarChart(data);
}

// 初始化时段分布折线图
function initHourlyChart(hourlyData) {
    const container = document.getElementById('hourlyChart');
    if (!container) return;

    if (dashboardCharts.hourly) {
        dashboardCharts.hourly.dispose();
    }

    dashboardCharts.hourly = echarts.init(container);

    const hours = hourlyData.map(d => d.hour);
    const counts = hourlyData.map(d => d.count);

    const option = {
        tooltip: {
            trigger: 'axis',
            backgroundColor: 'rgba(255, 255, 255, 0.9)',
            borderColor: 'rgba(37, 99, 235, 0.2)',
            textStyle: { color: '#1e293b' }
        },
        grid: {
            left: '5%',
            right: '5%',
            top: '10%',
            bottom: '15%',
            containLabel: true
        },
        xAxis: {
            type: 'category',
            data: hours,
            axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.3)' } },
            axisLabel: { color: 'rgba(71, 85, 105, 0.8)', fontSize: 11 }
        },
        yAxis: {
            type: 'value',
            splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.15)', type: 'dashed' } },
            axisLabel: { color: 'rgba(71, 85, 105, 0.8)' }
        },
        series: [{
            name: '预警数量',
            type: 'line',
            smooth: true,
            symbol: 'circle',
            symbolSize: 6,
            data: counts,
            itemStyle: { color: '#3b82f6' },
            lineStyle: { color: '#3b82f6', width: 2 },
            areaStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    { offset: 0, color: 'rgba(59, 130, 246, 0.3)' },
                    { offset: 1, color: 'rgba(59, 130, 246, 0.05)' }
                ])
            }
        }]
    };

    dashboardCharts.hourly.setOption(option);
}

// 初始化月度趋势柱状图
function initMonthlyChart(monthlyData) {
    const container = document.getElementById('monthlyChart');
    if (!container) return;

    if (dashboardCharts.monthly) {
        dashboardCharts.monthly.dispose();
    }

    dashboardCharts.monthly = echarts.init(container);

    const months = monthlyData.map(d => d.month);
    const counts = monthlyData.map(d => d.count);

    const option = {
        tooltip: {
            trigger: 'axis',
            backgroundColor: 'rgba(255, 255, 255, 0.9)',
            borderColor: 'rgba(37, 99, 235, 0.2)',
            textStyle: { color: '#1e293b' },
            axisPointer: { type: 'shadow' }
        },
        grid: {
            left: '5%',
            right: '5%',
            top: '10%',
            bottom: '10%',
            containLabel: true
        },
        xAxis: {
            type: 'category',
            data: months,
            axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.3)' } },
            axisLabel: { color: 'rgba(71, 85, 105, 0.8)', fontSize: 11, rotate: 30 }
        },
        yAxis: {
            type: 'value',
            splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.15)', type: 'dashed' } },
            axisLabel: { color: 'rgba(71, 85, 105, 0.8)' }
        },
        series: [{
            name: '专利数量',
            type: 'bar',
            data: counts,
            barWidth: '60%',
            itemStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    { offset: 0, color: '#6366f1' },
                    { offset: 1, color: '#3b82f6' }
                ]),
                borderRadius: [6, 6, 0, 0]
            }
        }]
    };

    dashboardCharts.monthly.setOption(option);
}

// 初始化仪表盘（风险占比）
function initGaugeCharts(riskDist, total) {
    const high = riskDist['高'] || 0;
    const mid = riskDist['中'] || 0;
    const low = riskDist['低'] || 0;

    const highPercent = total > 0 ? Math.round(high / total * 100) : 0;
    const midPercent = total > 0 ? Math.round(mid / total * 100) : 0;
    const lowPercent = total > 0 ? Math.round(low / total * 100) : 0;

    // 高风险仪表盘
    initSingleGauge('gaugeHigh', highPercent, '#ef4444', dashboardCharts, 'gaugeHigh');
    // 中风险仪表盘
    initSingleGauge('gaugeMid', midPercent, '#f59e0b', dashboardCharts, 'gaugeMid');
    // 低风险仪表盘
    initSingleGauge('gaugeLow', lowPercent, '#10b981', dashboardCharts, 'gaugeLow');
}

// 初始化单个仪表盘
function initSingleGauge(containerId, percent, color, chartsStore, chartKey) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (chartsStore[chartKey]) {
        chartsStore[chartKey].dispose();
    }

    chartsStore[chartKey] = echarts.init(container);

    const option = {
        title: {
            text: percent + '%',
            x: 'center',
            y: 'center',
            textStyle: {
                fontSize: 16,
                fontWeight: 'bold',
                color: '#1e293b'
            }
        },
        series: [{
            type: 'pie',
            clockWise: true,
            radius: ['70%', '90%'],
            itemStyle: {
                label: { show: false },
                labelLine: { show: false }
            },
            hoverAnimation: false,
            data: [{
                value: percent,
                name: '占比',
                itemStyle: { color: color }
            }, {
                value: 100 - percent,
                itemStyle: {
                    color: 'rgba(148, 163, 184, 0.15)'
                }
            }]
        }]
    };

    chartsStore[chartKey].setOption(option);
}

// 初始化雷达图
function initRadarChart(data) {
    const container = document.getElementById('radarChart');
    if (!container) return;

    if (dashboardCharts.radar) {
        dashboardCharts.radar.dispose();
    }

    dashboardCharts.radar = echarts.init(container);

    const stats = data.stats || {};
    const total = stats.total_patents || 1;
    const high = (stats.risk_distribution?.['高'] || 0) / total * 100;
    const mid = (stats.risk_distribution?.['中'] || 0) / total * 100;
    const low = (stats.risk_distribution?.['低'] || 0) / total * 100;

    const recent = stats.recent_alerts || 0;
    const recentPercent = Math.min(recent / total * 100, 100);

    const option = {
        tooltip: {
            backgroundColor: 'rgba(255, 255, 255, 0.9)',
            borderColor: 'rgba(37, 99, 235, 0.2)',
            textStyle: { color: '#1e293b' }
        },
        radar: {
            indicator: [
                { name: '高风险', max: 100 },
                { name: '中风险', max: 100 },
                { name: '低风险', max: 100 },
                { name: '活跃度', max: 100 },
                { name: '覆盖度', max: 100 }
            ],
            center: ['50%', '55%'],
            radius: '65%',
            splitNumber: 4,
            name: {
                textStyle: {
                    color: 'rgba(71, 85, 105, 0.8)',
                    fontSize: 12
                }
            },
            splitArea: {
                areaStyle: {
                    color: ['rgba(99, 102, 241, 0.03)', 'rgba(99, 102, 241, 0.08)']
                }
            },
            axisLine: {
                lineStyle: { color: 'rgba(148, 163, 184, 0.2)' }
            },
            splitLine: {
                lineStyle: { color: 'rgba(148, 163, 184, 0.2)' }
            }
        },
        series: [{
            name: '风险评估',
            type: 'radar',
            data: [{
                value: [
                    high,
                    mid,
                    low,
                    recentPercent,
                    Math.min(total / 10 * 10, 100)
                ],
                name: '当前数据',
                itemStyle: { color: '#6366f1' },
                areaStyle: {
                    color: 'rgba(99, 102, 241, 0.3)'
                },
                lineStyle: { color: '#6366f1', width: 2 }
            }]
        }]
    };

    dashboardCharts.radar.setOption(option);
}

// 全屏切换
function toggleFullscreen() {
    const dashboardTab = document.getElementById('dashboard-tab');
    if (!dashboardTab) return;

    if (dashboardTab.classList.contains('fullscreen')) {
        dashboardTab.classList.remove('fullscreen');
        if (document.exitFullscreen) {
            document.exitFullscreen();
        }
    } else {
        dashboardTab.classList.add('fullscreen');
        if (document.documentElement.requestFullscreen) {
            document.documentElement.requestFullscreen();
        }
    }

    // 全屏切换后重新调整图表大小
    setTimeout(() => {
        Object.values(dashboardCharts).forEach(chart => {
            if (chart && chart.resize) {
                chart.resize();
            }
        });
    }, 100);
}

// 窗口大小改变时调整图表
window.addEventListener('resize', () => {
    Object.values(dashboardCharts).forEach(chart => {
        if (chart && chart.resize) {
            chart.resize();
        }
    });
});

// ========================================
// 数据大屏相关函数 - 完全复刻文件夹5的原始样式
// ========================================

// 加载大屏数据
async function loadDashboardData() {
    try {
        // 隐藏加载动画
        const loading = document.getElementById('dashboardLoading');
        if (loading) loading.style.display = 'none';

        // 获取数据（使用全局关键词筛选）
        const data = await fetch('/api/dashboard').then(r => r.json());

        // 更新指标卡片
        updateDashboardMetrics(data);

        // 初始化/更新图表
        initDashboardCharts(data);

    } catch (e) {
        console.error('加载大屏数据失败:', e);
    }
}

// 更新大屏指标卡片
function updateDashboardMetrics(data) {
    const stats = data.stats || {};
    const categoryStats = data.category_stats || { stats: [] };

    // 基础统计
    const totalPatents = stats.total_patents || 0;
    const totalAlerts = stats.total_alerts || 0;
    const riskDist = stats.risk_distribution || {};

    // 更新左侧顶部三个指标
    const categoryTotal = categoryStats.total || 0;
    document.getElementById('dashValue1').textContent = categoryTotal;
    document.getElementById('dashValue2').textContent = totalPatents;
    document.getElementById('dashValue3').textContent = totalAlerts;

    // 更新中间专利总数大数字
    document.getElementById('dashTotal').textContent = totalPatents.toLocaleString();

    // 更新高风险/中风险/低风险数量和进度条
    const highCount = riskDist['高'] || 0;
    const midCount = riskDist['中'] || 0;
    const lowCount = riskDist['低'] || 0;
    const totalRisk = highCount + midCount + lowCount || 1;

    document.getElementById('dashHighCount').textContent = highCount;
    document.getElementById('dashMidCount').textContent = midCount;
    document.getElementById('dashLowCount').textContent = lowCount;

    document.getElementById('dashHighBar').style.width = (highCount / totalRisk * 100) + '%';
    document.getElementById('dashMidBar').style.width = (midCount / totalRisk * 100) + '%';
    document.getElementById('dashLowBar').style.width = (lowCount / totalRisk * 100) + '%';

    // 更新同比数据（使用最近7天预警数）
    const recentAlerts = stats.recent_alerts || 0;
    const highYoY = recentAlerts > 0 ? Math.round((highCount / recentAlerts) * 100 - 100) : 34;
    const midYoY = recentAlerts > 0 ? Math.round((midCount / recentAlerts) * 100 - 100) : 34;
    const lowYoY = recentAlerts > 0 ? Math.round((lowCount / recentAlerts) * 100 - 100) : -50;

    document.getElementById('dashHighYoY').innerHTML = highYoY + '<i>%</i>';
    document.getElementById('dashMidYoY').innerHTML = midYoY + '<i>%</i>';
    document.getElementById('dashLowYoY').innerHTML = lowYoY + '<i>%</i>';

    // 更新右侧分类统计
    const statsMap = {};
    categoryStats.stats.forEach(item => {
        statsMap[item.category || '未分类'] = item.count;
    });
    document.getElementById('dashValue4').textContent = statsMap['公司'] || 0;
    document.getElementById('dashValue5').textContent = statsMap['人名'] || 0;
    document.getElementById('dashValue6').textContent = statsMap['通用'] || 0;

    // 更新威胁公司排行榜
    updateThreatRanking(data.threat_companies || []);
}

// 更新威胁公司排行榜（原始5文件夹样式）
function updateThreatRanking(companies) {
    const container = document.getElementById('threatRanking');
    if (!container) return;

    if (!companies || companies.length === 0) {
        container.innerHTML = '';
        return;
    }

    const maxValue = Math.max(...companies.map(c => c.count || 0));

    container.innerHTML = companies.map((company, index) => {
        const count = company.count || 0;
        const percentage = maxValue > 0 ? (count / maxValue * 100) : 0;

        return `
            <li>
                <span>${index + 1}</span>
                <div class="pmnav">
                    <p>${company.company}</p>
                    <div class="pmbar"><span style="width:${percentage}%"></span><i>${count}</i></div>
                </div>
            </li>
        `;
    }).join('');
}

// 初始化大屏图表（使用原始5文件夹的ECharts配置）
function initDashboardCharts(data) {
    if (typeof echarts === 'undefined') {
        console.error('ECharts 未加载');
        return;
    }

    // 销毁旧图表
    Object.values(dashboardCharts).forEach(chart => {
        if (chart) {
            chart.dispose();
        }
    });

    // 获取数据
    const stats = data.stats || {};
    const riskDist = stats.risk_distribution || {};
    const highCount = riskDist['高'] || 0;
    const midCount = riskDist['中'] || 0;
    const lowCount = riskDist['低'] || 0;
    const totalRisk = highCount + midCount + lowCount || 1;

    const highPercent = Math.round(highCount / totalRisk * 100);
    const midPercent = Math.round(midCount / totalRisk * 100);
    const lowPercent = Math.round(lowCount / totalRisk * 100);

    // 初始化各个图表
    initEcharts1(data.monthly_trends || []);
    initEcharts2(data.hourly_alerts || []);
    initEcharts3(data.hourly_alerts || []);
    initPe01(highPercent);
    initPe02(midPercent);
    initPe03(lowPercent);
    initPe04(data);

    // 窗口大小改变时调整图表
    window.addEventListener('resize', () => {
        Object.values(dashboardCharts).forEach(chart => {
            if (chart && chart.resize) {
                chart.resize();
            }
        });
    });
}

// 柱状图（月度趋势）- 原始5文件夹配置
function initEcharts1(monthlyData) {
    const container = document.getElementById('echarts1');
    if (!container) return;

    dashboardCharts.echarts1 = echarts.init(container);

    const months = monthlyData.map(d => d.month);
    const counts = monthlyData.map(d => d.count);

    const option = {
        tooltip: {
            trigger: 'axis',
            axisPointer: {type: 'shadow'}
        },
        legend: {
            x: 'center',
            y: '0',
            icon: 'circle',
            itemGap: 8,
            textStyle: {color: 'rgba(255,255,255,.5)'},
            itemWidth: 10,
            itemHeight: 10,
        },
        grid: {
            left: '0',
            top: '30',
            right: '15',
            bottom: '0',
            containLabel: true
        },
        xAxis: {
            type: 'category',
            data: months,
            axisLine: {show: false},
            axisLabel: {
                textStyle: {
                    color: 'rgba(255,255,255,.6)',
                    fontSize: 14
                }
            },
        },
        yAxis: {
            type: 'value',
            splitNumber: 4,
            axisLine: { show: false },
            axisTick: {show: false},
            splitLine: {
                show: true,
                lineStyle: {
                    color: 'rgba(255,255,255,0.05)'
                }
            },
            axisLabel: {
                textStyle: {
                    color: 'rgba(255,255,255,.6)',
                    fontSize: 14
                },
            },
        },
        series: [{
            name: '专利数量',
            type: 'bar',
            barWidth: '15%',
            itemStyle: {
                normal: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{
                        offset: 0,
                        color: '#8bd46e'
                    }, {
                        offset: 1,
                        color: '#03b48e'
                    }]),
                    barBorderRadius: 11,
                }
            },
            data: counts
        },
        {
            name: '预警数量',
            type: 'bar',
            barWidth: '15%',
            itemStyle: {
                normal: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{
                        offset: 0,
                        color: '#3893e5'
                    }, {
                        offset: 1,
                        color: '#248ff7'
                    }]),
                    barBorderRadius: 11,
                }
            },
            data: monthlyData.map(d => {
                // 简单计算：用月度数据模拟预警数据
                return Math.floor(d.count * 0.8);
            })
        },
        {
            name: '分析数量',
            type: 'bar',
            barWidth: '15%',
            itemStyle: {
                normal: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{
                        offset: 0,
                        color: '#43cadd'
                    }, {
                        offset: 1,
                        color: '#0097c9'
                    }]),
                    barBorderRadius: 11,
                }
            },
            data: monthlyData.map(d => {
                return Math.floor(d.count * 0.5);
            })
        }]
    };

    dashboardCharts.echarts1.setOption(option);
}

// 折线图（时段分布）- 原始5文件夹配置
function initEcharts2(hourlyData) {
    const container = document.getElementById('echarts2');
    if (!container) return;

    dashboardCharts.echarts2 = echarts.init(container);

    const hours = hourlyData.map(d => d.hour);
    const counts1 = hourlyData.map(d => d.count);
    const counts2 = hourlyData.map(d => Math.floor(d.count * 0.6));

    const option = {
        tooltip: {
            trigger: 'axis',
            axisPointer: {
                lineStyle: {
                    color: '#dddc6b'
                }
            }
        },
        grid: {
            left: '0',
            top: '30',
            right: '20',
            bottom: '-10',
            containLabel: true
        },
        legend: {
            data: ['预警数量', '分析数量'],
            right: 'center',
            top: 0,
            textStyle: {
                color: "#fff"
            },
            itemWidth: 12,
            itemHeight: 10,
        },
        xAxis: [{
            type: 'category',
            boundaryGap: false,
            axisLabel: {
                textStyle: {
                    color: "rgba(255,255,255,.6)",
                    fontSize: 14,
                },
            },
            axisLine: {
                lineStyle: {
                    color: 'rgba(255,255,255,.1)'
                }
            },
            data: hours
        }],
        yAxis: [{
            type: 'value',
            axisTick: {show: false},
            axisLine: {
                lineStyle: {
                    color: 'rgba(255,255,255,.1)'
                }
            },
            axisLabel: {
                textStyle: {
                    color: "rgba(255,255,255,.6)",
                    fontSize: 14,
                },
            },
            splitLine: {
                lineStyle: {
                    color: 'rgba(255,255,255,.1)'
                }
            }
        }],
        series: [
            {
                name: '预警数量',
                type: 'line',
                smooth: true,
                symbol: 'circle',
                symbolSize: 5,
                showSymbol: false,
                lineStyle: {
                    normal: {
                        color: 'rgba(228, 228, 126, 1)',
                        width: 2
                    }
                },
                areaStyle: {
                    normal: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{
                            offset: 0,
                            color: 'rgba(228, 228, 126, .8)'
                        }, {
                            offset: 0.8,
                            color: 'rgba(228, 228, 126, 0.1)'
                        }], false),
                        shadowColor: 'rgba(0, 0, 0, 0.1)',
                    }
                },
                itemStyle: {
                    normal: {
                        color: '#dddc6b',
                        borderColor: 'rgba(221, 220, 107, .1)',
                        borderWidth: 12
                    }
                },
                data: counts1
            },
            {
                name: '分析数量',
                type: 'line',
                smooth: true,
                symbol: 'circle',
                symbolSize: 5,
                showSymbol: false,
                lineStyle: {
                    normal: {
                        color: 'rgba(255, 128, 128, 1)',
                        width: 2
                    }
                },
                areaStyle: {
                    normal: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{
                            offset: 0,
                            color: 'rgba(255, 128, 128,.8)'
                        }, {
                            offset: 0.8,
                            color: 'rgba(255, 128, 128, .1)'
                        }], false),
                        shadowColor: 'rgba(0, 0, 0, 0.1)',
                    }
                },
                itemStyle: {
                    normal: {
                        color: '#dddc6b',
                        borderColor: 'rgba(221, 220, 107, .1)',
                        borderWidth: 12
                    }
                },
                data: counts2
            },
        ]
    };

    dashboardCharts.echarts2.setOption(option);
}

// 单系列折线图 - 原始5文件夹配置
function initEcharts3(hourlyData) {
    const container = document.getElementById('echarts3');
    if (!container) return;

    dashboardCharts.echarts3 = echarts.init(container);

    const hours = hourlyData.map(d => d.hour);
    const counts = hourlyData.map(d => d.count);

    const option = {
        tooltip: {
            trigger: 'axis',
            axisPointer: {
                lineStyle: {
                    color: '#dddc6b'
                }
            }
        },
        grid: {
            left: '0',
            top: '30',
            right: '20',
            bottom: '-10',
            containLabel: true
        },
        xAxis: [{
            type: 'category',
            boundaryGap: false,
            axisLabel: {
                textStyle: {
                    color: "rgba(255,255,255,.6)",
                    fontSize: 14,
                },
            },
            axisLine: {
                lineStyle: {
                    color: 'rgba(255,255,255,.1)'
                }
            },
            data: hours
        }],
        yAxis: [{
            type: 'value',
            axisTick: {show: false},
            axisLine: {
                lineStyle: {
                    color: 'rgba(255,255,255,.1)'
                }
            },
            axisLabel: {
                textStyle: {
                    color: "rgba(255,255,255,.6)",
                    fontSize: 14,
                },
            },
            splitLine: {
                lineStyle: {
                    color: 'rgba(255,255,255,.1)',
                }
            }
        }],
        series: [
            {
                name: '预警时段分布',
                type: 'line',
                smooth: true,
                symbol: 'circle',
                symbolSize: 10,
                showSymbol: true,
                lineStyle: {
                    normal: {
                        color: 'rgba(228, 228, 126, 1)',
                        width: 2
                    }
                },
                areaStyle: {
                    normal: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{
                            offset: 0,
                            color: 'rgba(228, 228, 126, .8)'
                        }, {
                            offset: 0.8,
                            color: 'rgba(228, 228, 126, 0.1)'
                        }], false),
                        shadowColor: 'rgba(0, 0, 0, 0.1)',
                    }
                },
                itemStyle: {
                    normal: {
                        color: '#dddc6b',
                        borderColor: 'rgba(221, 220, 107, .1)',
                        borderWidth: 12
                    }
                },
                data: counts
            }
        ]
    };

    dashboardCharts.echarts3.setOption(option);
}

// 仪表盘1（高风险）- 原始5文件夹配置
function initPe01(percent) {
    const container = document.getElementById('pe01');
    if (!container) return;

    dashboardCharts.pe01 = echarts.init(container);

    const option = {
        title: {
            text: percent + '%',
            x: 'center',
            y: 'center',
            textStyle: {
                fontWeight: 'normal',
                color: '#fff',
                fontSize: '18'
            }
        },
        color: '#49bcf7',
        series: [{
            name: 'Line 1',
            type: 'pie',
            clockWise: true,
            radius: ['65%', '80%'],
            itemStyle: {
                normal: {
                    label: {
                        show: false
                    },
                    labelLine: {
                        show: false
                    }
                }
            },
            hoverAnimation: false,
            data: [{
                value: percent,
                name: '高风险',
                itemStyle: {
                    normal: {
                        color: '#eaff00',
                        label: {
                            show: false
                        },
                        labelLine: {
                            show: false
                        }
                    }
                }
            }, {
                name: '其他',
                value: 100 - percent
            }]
        }]
    };

    dashboardCharts.pe01.setOption(option);
}

// 仪表盘2（中风险）- 原始5文件夹配置
function initPe02(percent) {
    const container = document.getElementById('pe02');
    if (!container) return;

    dashboardCharts.pe02 = echarts.init(container);

    const option = {
        title: {
            text: percent + '%',
            x: 'center',
            y: 'center',
            textStyle: {
                fontWeight: 'normal',
                color: '#fff',
                fontSize: '18'
            }
        },
        color: '#49bcf7',
        series: [{
            name: 'Line 1',
            type: 'pie',
            clockWise: true,
            radius: ['65%', '80%'],
            itemStyle: {
                normal: {
                    label: {
                        show: false
                    },
                    labelLine: {
                        show: false
                    }
                }
            },
            hoverAnimation: false,
            data: [{
                value: percent,
                name: '中风险',
                itemStyle: {
                    normal: {
                        color: '#ea4d4d',
                        label: {
                            show: false
                        },
                        labelLine: {
                            show: false
                        }
                    }
                }
            }, {
                name: '其他',
                value: 100 - percent
            }]
        }]
    };

    dashboardCharts.pe02.setOption(option);
}

// 仪表盘3（低风险）- 原始5文件夹配置
function initPe03(percent) {
    const container = document.getElementById('pe03');
    if (!container) return;

    dashboardCharts.pe03 = echarts.init(container);

    const option = {
        title: {
            text: percent + '%',
            x: 'center',
            y: 'center',
            textStyle: {
                fontWeight: 'normal',
                color: '#fff',
                fontSize: '18'
            }
        },
        color: '#49bcf7',
        series: [{
            name: 'Line 1',
            type: 'pie',
            clockWise: true,
            radius: ['65%', '80%'],
            itemStyle: {
                normal: {
                    label: {
                        show: false
                    },
                    labelLine: {
                        show: false
                    }
                }
            },
            hoverAnimation: false,
            data: [{
                value: percent,
                name: '低风险',
                itemStyle: {
                    normal: {
                        color: '#395ee6',
                        label: {
                            show: false
                        },
                        labelLine: {
                            show: false
                        }
                    }
                }
            }, {
                name: '其他',
                value: 100 - percent
            }]
        }]
    };

    dashboardCharts.pe03.setOption(option);
}

// 雷达图（多维评估）- 原始5文件夹配置
function initPe04(data) {
    const container = document.getElementById('lastecharts');
    if (!container) return;

    dashboardCharts.lastecharts = echarts.init(container);

    const stats = data.stats || {};
    const total = stats.total_patents || 1;
    const riskDist = stats.risk_distribution || {};

    // 计算五个维度
    const highRatio = (riskDist['高'] || 0) / total * 100;
    const midRatio = (riskDist['中'] || 0) / total * 100;
    const lowRatio = (riskDist['低'] || 0) / total * 100;
    const recent = Math.min((stats.recent_alerts || 0) / total * 100, 100);
    const coverage = Math.min(total / 10 * 10, 100);

    const option = {
        tooltip: {
            trigger: 'axis'
        },
        radar: [{
            indicator: [{
                text: '高风险',
                max: 100
            }, {
                text: '中风险',
                max: 100
            }, {
                text: '低风险',
                max: 100
            }, {
                text: '活跃度',
                max: 100
            }, {
                text: '覆盖度',
                max: 100
            }],
            textStyle: {
                color: 'red'
            },
            center: ['50%', '50%'],
            radius: '70%',
            startAngle: 90,
            splitNumber: 4,
            shape: 'circle',
            name: {
                padding: -5,
                formatter: '{value}',
                textStyle: {
                    fontSize: 14,
                    color: 'rgba(255,255,255,.6)'
                }
            },
            splitArea: {
                areaStyle: {
                    color: 'rgba(255,255,255,.05)'
                }
            },
            axisLine: {
                lineStyle: {
                    color: 'rgba(255,255,255,.05)'
                }
            },
            splitLine: {
                lineStyle: {
                    color: 'rgba(255,255,255,.05)'
                }
            }
        }],
        series: [{
            name: '雷达图',
            type: 'radar',
            tooltip: {
                trigger: 'item'
            },
            data: [{
                name: '当前数据',
                value: [highRatio, midRatio, lowRatio, recent, coverage],
                lineStyle: {
                    normal: {
                        color: '#03b48e',
                        width: 2,
                    }
                },
                areaStyle: {
                    normal: {
                        color: '#03b48e',
                        opacity: .4
                    }
                },
                symbolSize: 0,
            }, {
                name: '平均值',
                value: [30, 20, 75, 80, 70],
                symbolSize: 0,
                lineStyle: {
                    normal: {
                        color: '#3893e5',
                        width: 2,
                    }
                },
                areaStyle: {
                    normal: {
                        color: 'rgba(19, 173, 255, 0.5)'
                    }
                }
            }]
        }]
    };

    dashboardCharts.lastecharts.setOption(option);
}

// 实时时钟
function updateDashboardTime() {
    const showTime = document.getElementById('showTime');
    if (!showTime) return;

    const dt = new Date();
    const y = dt.getFullYear();
    const mt = dt.getMonth() + 1;
    const day = dt.getDate();
    const h = dt.getHours();
    const m = dt.getMinutes();
    const s = dt.getSeconds();

    function pad(obj) {
        if (obj < 10) return "0" + "" + obj;
        else return obj;
    }

    showTime.innerHTML = y + "年" + pad(mt) + "月" + pad(day) + "日" + pad(h) + "时" + pad(m) + "分" + pad(s) + "秒";
}

// 每秒更新时钟
setInterval(updateDashboardTime, 1000);

// 当切换到大屏标签时加载数据
document.addEventListener('DOMContentLoaded', () => {
    // 监听标签切换
    const navButtons = document.querySelectorAll('.nav-btn');
    navButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            if (btn.dataset.tab === 'dashboard') {
                // 数据大屏通过iframe加载，无需额外处理
            }
        });
    });
});

