'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const storageKey = 'arcus-workspace-v1';
  const readSelection = () => { try { return JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch { return {}; } };
  const saved = readSelection();
  const state = { data: { accounts: [], markets: [], strategies: [], events: [], server: {} }, accountId: saved.accountId || '', market: saved.market || '', view: ['grid', 'trend', 'future'].includes(saved.view) ? saved.view : 'grid', tab: 'positions', candles: [], candleMarket: '', candleError: '', preview: null, previewBusy: false, previewRequest: 0, trendPreview: null, trendPreviewBusy: false, trendPreviewRequest: 0, trendSignal: null, trendSignalMarket: '', trendSignalError: '', trendSignalRequest: 0, trendSignalBusy: false, trendSignalUpdatedAt: 0, trendCandles: [], selectedStrategyId: null, pollBusy: false, online: false, authenticated: false, confirmAction: null };
  let pollTimer, previewTimer, trendPreviewTimer, candleRequest = 0, resizeTimer;
  const number = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  const fmt = (value, digits = 2) => number(value) === null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(Number(value));
  const compact = (value, digits = 8) => number(value) === null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(Number(value));
  const signed = (value) => number(value) === null ? '—' : `${Number(value) > 0 ? '+' : ''}${fmt(value)}`;
  const signClass = (value) => number(value) === null || Number(value) === 0 ? '' : Number(value) > 0 ? 'positive' : 'negative';
  const timeValue = (value) => { const numeric = number(value); return numeric === null ? value : numeric < 100000000000 ? numeric * 1000 : numeric; };
  const time = (value, withDate = false) => { if (!value) return '等待同步'; const date = new Date(timeValue(value)); return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { month: withDate ? '2-digit' : undefined, day: withDate ? '2-digit' : undefined, hour: '2-digit', minute: '2-digit', second: withDate ? undefined : '2-digit', hour12: false }); };
  const getAccount = () => state.data.accounts.find((account) => account.id === state.accountId);
  const getMarket = () => state.data.markets.find((market) => market.symbol === state.market);
  const selectedStrategies = () => state.data.strategies.filter((strategy) => strategy.accountId === state.accountId);
  const isTrendStrategy = (strategy) => strategy.type === 'trend';
  const maskedAddress = (address) => /^0x[0-9a-fA-F]{40}$/.test(String(address || '')) ? `${address.slice(0, 6)}…${address.slice(-4)}` : '地址待同步';
  const accountIdentity = (account) => account ? `${account.name || '未命名账户'} · ${number(account.accountIndex) === null ? '索引待同步' : `子账户 ${compact(account.accountIndex, 0)}`} · ${maskedAddress(account.address)}` : '账户待同步';
  const saveSelection = () => { try { localStorage.setItem(storageKey, JSON.stringify({ accountId: state.accountId, market: state.market, view: state.view })); } catch { /* The dashboard works if storage is disabled. */ } };

  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: 'same-origin', ...options, headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers }, body: options.body ? JSON.stringify(options.body) : undefined });
    let result; try { result = await response.json(); } catch { result = {}; }
    if (!response.ok) {
      if (response.status === 401 && path !== '/api/login') showLogin();
      const detail = result.detail || result.error || `请求失败（${response.status}）`;
      const error = new Error(Array.isArray(detail) ? detail.map((item) => `${(item.loc || []).filter((item) => item !== 'body').join(' · ')}：${item.msg}`).join('；') : typeof detail === 'string' ? detail : '请求失败');
      error.status = response.status;
      throw error;
    }
    return result;
  }

  function setError(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
  function toast(message, isError = false) { const item = document.createElement('div'); item.className = `toast${isError ? ' error' : ''}`; item.textContent = `${isError ? '!' : '✓'}  ${message}`; $('toast-region').append(item); window.setTimeout(() => item.remove(), isError ? 7500 : 4500); }
  function busy(button, isBusy, text) { if (!button) return; if (isBusy) { button.dataset.originalText = button.textContent; button.textContent = text || '处理中…'; } else if (button.dataset.originalText) { button.textContent = button.dataset.originalText; delete button.dataset.originalText; } button.disabled = isBusy; }
  function showLogin() { state.authenticated = false; clearTimeout(pollTimer); $('app-shell').hidden = true; $('login-screen').hidden = false; document.title = '登录 · Arcus 自动交易工作台'; }
  async function showApp() { state.authenticated = true; $('login-screen').hidden = true; $('app-shell').hidden = false; document.title = 'Arcus · 自动交易工作台'; setView(state.view); await pollState(); }

  function setConnection(online, message) {
    state.online = online;
    $('sidebar-status-dot').className = `status-dot ${online ? 'online' : 'error'}`;
    $('sidebar-status-text').textContent = message || (online ? '控制台已连接' : '连接中断');
    $('connection-status').innerHTML = `<span class="status-dot ${online ? 'online' : 'error'}"></span>${escape(message || (online ? '控制台已连接' : '连接中断'))}`;
  }

  async function pollState() {
    if (!state.authenticated || state.pollBusy) return;
    state.pollBusy = true;
    clearTimeout(pollTimer);
    try {
      const result = await api('/api/state');
      state.data = { accounts: [], markets: [], strategies: [], events: [], server: {}, ...result };
      if (!state.data.accounts.some((account) => account.id === state.accountId)) state.accountId = state.data.accounts[0]?.id || '';
      if (!state.data.markets.some((market) => market.symbol === state.market)) state.market = state.data.markets[0]?.symbol || '';
      if (state.view === 'trend' && state.data.markets.some((market) => market.symbol === 'ETH-USD')) state.market = 'ETH-USD';
      const marketError = state.data.server.marketsError;
      setError('global-error', marketError ? `行情同步异常：${marketError}。请在行情恢复后启动策略。` : '');
      setConnection(true, marketError ? '行情同步异常' : '控制台已连接');
      renderState();
      saveSelection();
      if (state.market && (state.candleMarket !== state.market || !state.candles.length)) await loadCandles();
      else renderChart();
      if (state.view === 'trend' && (state.trendSignalMarket !== state.market || Date.now() - state.trendSignalUpdatedAt >= 15000)) await loadTrendSignal();
    } catch (error) {
      if (error.status !== 401) { setConnection(false); setError('global-error', `控制台连接失败：${error.message}。显示的数据可能已过期。`); }
    } finally {
      state.pollBusy = false;
      if (state.authenticated) pollTimer = window.setTimeout(pollState, Math.max(3000, Number(state.data.server.pollingInterval) || 5000));
    }
  }

  function renderState() {
    renderAccounts();
    renderMarket();
    renderStrategies();
    renderTrendStrategies();
    renderTrendSignal();
    if ($('strategy-dialog').open) renderStrategyDetail();
    renderDetails();
    $('server-version').textContent = `Arcus Console ${state.data.server.version || ''}`;
    $('footer-sync').textContent = state.data.server.time ? `服务器同步 ${time(state.data.server.time)} · 每 ${Math.round((Number(state.data.server.pollingInterval) || 5000) / 1000)} 秒更新` : '数据来自交易所与策略执行记录';
    $('refresh-account').disabled = Boolean($('refresh-account').dataset.originalText) || !state.accountId;
    $('create-strategy').disabled = Boolean($('create-strategy').dataset.originalText) || !state.accountId || !state.market;
    $('create-trend-strategy').disabled = Boolean($('create-trend-strategy').dataset.originalText) || !state.accountId || !state.market;
    $('suggest-range').disabled = number(getMarket()?.markPrice) === null;
  }

  function setOptions(element, items, selected, emptyText) {
    const desired = items.map((item) => `${item.value}\u0000${item.label}`).join('\u0001');
    if (element.dataset.options !== desired) { element.innerHTML = items.length ? items.map((item) => `<option value="${escape(item.value)}">${escape(item.label)}</option>`).join('') : `<option value="">${escape(emptyText)}</option>`; element.dataset.options = desired; }
    element.value = selected;
  }

  function renderAccounts() {
    const accounts = state.data.accounts;
    const account = getAccount();
    setOptions($('account-select'), accounts.map((item) => ({ value: item.id, label: accountIdentity(item) })), state.accountId, '请先添加账户');
    $('account-select').title = account ? accountIdentity(account) : '请先添加账户';
    $('sidebar-accounts').innerHTML = accounts.length ? accounts.map((item) => `<button class="side-account ${item.id === state.accountId ? 'active' : ''} ${item.connected ? 'connected' : ''}" data-account-id="${escape(item.id)}" title="${escape(accountIdentity(item))}" aria-label="${escape(accountIdentity(item))}" ${item.id === state.accountId ? 'aria-current="true"' : ''}><span class="account-dot"></span><span class="side-account-copy"><span>${escape(item.name)}</span><small>${number(item.accountIndex) === null ? '索引待同步' : `子账户 ${compact(item.accountIndex, 0)}`} · ${escape(maskedAddress(item.address))}</small></span></button>`).join('') : '<div class="sidebar-empty">尚未添加账户</div>';
    $('account-equity').textContent = fmt(account?.equity);
    $('account-available').textContent = fmt(account?.availableBalance);
    $('account-pnl').textContent = signed(account?.unrealizedPnl);
    $('account-pnl').className = signClass(account?.unrealizedPnl);
    $('running-count').innerHTML = `${selectedStrategies().filter((strategy) => strategy.status === 'running').length} <small>个</small>`;
    $('last-updated').textContent = account?.updatedAt ? time(account.updatedAt) : '等待账户同步';
    setError('account-error', account?.error ? `${account.name}：${account.error}${account.updatedAt ? `（最近成功同步 ${time(account.updatedAt)}）` : ''}` : account && !account.connected ? '账户尚未连接。同步成功后可查看余额与持仓。' : '');
    $('position-count').textContent = account?.positions?.length || 0;
    $('order-count').textContent = account?.orders?.length || 0;
    renderAccountStatistics();
  }

  function statisticsDate(value) {
    if (value === null || value === undefined || value === '') return '—';
    const numeric = number(value);
    const stamp = numeric === null ? value : numeric >= 100000000000000 ? numeric / 1000 : numeric < 100000000000 ? numeric * 1000 : numeric;
    const date = new Date(stamp);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  }
  function renderAccountStatistics() {
    const account = getAccount(), statistics = account?.statistics;
    const historyReady = Boolean(account && (statistics?.complete === true || statistics?.historyComplete === true) && statistics.stale !== true);
    const turnoverReady = historyReady && statistics.turnoverKnown === true;
    const feesReady = historyReady && statistics.feesKnown === true;
    const pnlReady = historyReady && statistics.pnlKnown === true;
    $('statistics-account').textContent = account ? accountIdentity(account) : '请选择交易账户';
    $('statistics-turnover').textContent = turnoverReady ? fmt(statistics.turnover) : '—';
    $('statistics-realized-pnl').textContent = pnlReady ? signed(statistics.realizedPnl) : '—';
    $('statistics-realized-pnl').className = pnlReady ? signClass(statistics.realizedPnl) : '';
    $('statistics-fees').textContent = feesReady ? fmt(statistics.fees) : '—';
    const status = statistics?.stale === true && statistics.updatedAt !== null && statistics.updatedAt !== undefined ? 'stale' : statistics?.status || 'waiting';
    $('statistics-status').textContent = !account ? '未选择账户' : ({ ready: historyReady ? '已同步' : '记录不完整', sync: '统计同步中', syncing: '统计同步中', partial: historyReady ? '部分指标待核验' : '记录不完整', error: '统计同步失败', stale: '统计已过期', waiting: '等待同步' })[status] || '等待同步';
    $('statistics-status').className = `statistics-status ${status === 'ready' && historyReady ? 'ready' : status === 'error' || status === 'partial' || status === 'stale' ? 'warning' : ''}`;
    const period = statistics?.toTime !== null && statistics?.toTime !== undefined ? statistics.fromTime !== null && statistics.fromTime !== undefined ? `${statisticsDate(statistics.fromTime)} – ${statisticsDate(statistics.toTime)}（上海时间）` : `交易所可读取的累计记录 · 截至 ${statisticsDate(statistics.toTime)}（上海时间）${historyReady && number(statistics.fillCount) === 0 ? '（暂无成交）' : ''}` : '统计区间待同步';
    const count = historyReady && number(statistics.fillCount) !== null ? ` · ${compact(statistics.fillCount, 0)} 笔成交` : '';
    const updated = statistics?.updatedAt ? ` · 更新 ${statisticsDate(statistics.updatedAt)}` : '';
    $('statistics-period').textContent = `${period}${count}${updated}`;
    const message = !account ? '选择账户后显示该子账户的真实成交统计。' : statistics?.error || (historyReady ? status === 'sync' || status === 'syncing' ? '正在同步；当前数值为上方截止时间前已核验的记录。' : pnlReady ? '' : number(statistics.liquidationFillCount) > 0 ? '部分清算成交缺少可核验的已实现盈亏，仅显示已确认的成交金额与手续费。' : '已实现盈亏记录未核验完整，仅显示已确认的成交金额与手续费。' : status === 'partial' ? '历史记录尚未完整核验，累计值暂不显示。' : status === 'stale' ? '统计已过期，等待重新同步后显示累计值。' : '成交记录正在同步，未知数值以 — 显示。');
    $('statistics-message').textContent = message;
    $('statistics-message').hidden = !message;
  }

  function renderMarket() {
    const market = getMarket();
    const options = state.data.markets.map((item) => ({ value: item.symbol, label: item.symbol }));
    setOptions($('chart-market'), options, state.market, '等待行情');
    setOptions($('strategy-market'), options, state.market, '等待行情');
    setOptions($('trend-market'), options.filter((item) => item.value === 'ETH-USD'), state.market === 'ETH-USD' ? state.market : '', '等待 ETH-USD 行情');
    const symbol = state.market.replace(/[-_/].*$/, '');
    $('market-avatar').textContent = symbol === 'BTC' ? '₿' : symbol === 'ETH' ? 'Ξ' : symbol.slice(0, 1) || '↗';
    $('market-price').textContent = compact(market?.markPrice, priceDigits(market));
    const rawChange = number(market?.priceChange24h);
    const change = rawChange === null ? null : rawChange * 100;
    $('market-change').textContent = change === null ? '24 小时变动 —' : `${change > 0 ? '+' : ''}${fmt(change)}%  24h`;
    $('market-change').className = signClass(change);
    $('maker-fee').textContent = number(market?.makerFee) === null ? '—' : `${compact(Number(market.makerFee) * 100, 4)}%`;
    $('taker-fee').textContent = number(market?.takerFee) === null ? '—' : `${compact(Number(market.takerFee) * 100, 4)}%`;
    $('market-status').textContent = market ? ({ ONLINE: '交易中', PAUSED: '交易暂停', OFFLINE: '已下线', CLOSED: '已关闭' })[String(market.status).toUpperCase()] || market.status || '行情已同步' : '市场待连接';
    $('market-status').className = `market-status ${market && !state.data.server.marketsError ? 'online' : ''}`;
    renderContractControls();
    renderContractControls('trend');
  }

  function priceDigits(market = getMarket()) { const tick = number(market?.tickSize); if (tick === null || tick === 0) return 4; return Math.max(0, Math.min(8, Math.ceil(-Math.log10(tick)))); }
  const marginModeLabel = (mode) => mode === 'isolated' ? '逐仓' : '全仓';
  const isMarginBudget = (strategy) => strategy.budgetType === 'margin';
  const strategyLeverage = (strategy) => isMarginBudget(strategy) ? number(strategy.leverage) ?? 1 : null;
  const contractSummary = (strategy) => isMarginBudget(strategy) ? `${compact(strategyLeverage(strategy), 0)}× · ${marginModeLabel(strategy.marginMode)}` : '旧配置：名义预算；未设置合约杠杆';
  const strategyNotionalBudget = (strategy) => isMarginBudget(strategy) ? number(strategy.notionalBudget) ?? Number(strategy.budget) * strategyLeverage(strategy) : number(strategy.budget);
  function renderContractControls(prefix = 'grid') {
    const market = prefix === 'trend' ? state.data.markets.find((item) => item.symbol === 'ETH-USD') : getMarket();
    const exchangeMaximum = number(market?.maxLeverage);
    const maximum = prefix === 'trend' ? exchangeMaximum === null ? 5 : Math.min(exchangeMaximum, 5) : exchangeMaximum;
    const marginUnknown = market?.marginStateKnown === false;
    const input = $(`${prefix}-leverage`);
    const selected = number(input.value);
    if (maximum !== null && maximum >= 1) input.max = String(Math.floor(maximum));
    else input.removeAttribute('max');
    const presets = marginUnknown || maximum === 0 ? [] : [1, 2, 5, 10, 20, 40].filter((value) => value === 1 || maximum !== null && value <= maximum);
    const presetElement = $(prefix === 'grid' ? 'leverage-presets' : `${prefix}-leverage-presets`);
    const presetMarkup = presets.map((value) => `<button type="button" data-leverage="${value}" aria-pressed="${value === selected}" class="${value === selected ? 'active' : ''}">${value}×</button>`).join('');
    if (presetElement.innerHTML !== presetMarkup) presetElement.innerHTML = presetMarkup;
    const limitElement = $(prefix === 'grid' ? 'leverage-limit' : `${prefix}-leverage-limit`);
    limitElement.textContent = marginUnknown ? '交易时段保证金状态未确认，暂不可开仓。' : maximum !== null && maximum >= 1 ? `${selected > maximum ? '已选杠杆超过上限。' : ''}${prefix === 'trend' ? '参考策略杠杆上限' : '交易所杠杆上限'} ${compact(maximum, 0)}×${market?.isOutsideRth === true ? '。非交易时段已按提高的保证金率计算。' : ''}` : '等待交易所杠杆上限';
    limitElement.classList.toggle('control-warning', marginUnknown || maximum !== null && selected > maximum);
    const isolated = $(`${prefix}-margin-mode`).value === 'isolated';
    $(prefix === 'grid' ? 'margin-mode-help' : `${prefix}-margin-mode-help`).textContent = isolated ? '逐仓使用交易所分配给该合约的保证金。杠杆与模式按账户的该合约生效。' : '全仓共用账户保证金，其他仓位盈亏也会影响可用保证金。杠杆与模式按账户的该合约生效。';
  }
  function strategyStatus(status) { return ({ draft: '待启动', configuring: '合约设置确认中', running: '运行中', paused: '已暂停', pausing: '暂停处理中', stopped: '已停止', stopping: '停止处理中', error: '异常', recovering: '恢复处理中' })[status] || status || '未知'; }
  function strategyActions(strategy) {
    const id = escape(strategy.id);
    const actions = [];
    if (['draft', 'paused', 'stopped', 'error'].includes(strategy.status)) actions.push(`<button class="action-button" data-strategy-id="${id}" data-action="start">${strategy.status === 'paused' ? '恢复' : '启动'}</button>`);
    if (strategy.status === 'running') actions.push(`<button class="action-button" data-strategy-id="${id}" data-action="pause">暂停</button>`);
    if (!['draft', 'stopped'].includes(strategy.status)) actions.push(`<button class="action-button danger" data-strategy-id="${id}" data-action="stop">停止</button>`);
    const blocked = strategyDeleteReason(strategy);
    actions.push(`<button class="action-button danger" data-delete-strategy="${id}" ${blocked ? `disabled title="${escape(blocked)}" aria-label="删除策略：${escape(blocked)}"` : ''}>删除</button>${blocked ? `<span class="strategy-delete-hint">${escape(blocked)}</span>` : ''}`);
    return actions.length ? actions.join('') : '<span class="secondary-text">等待执行</span>';
  }
  function strategyDeleteReason(strategy) {
    if (!['draft', 'paused', 'stopped'].includes(strategy.status)) return '先停止并处理持仓/订单';
    if (strategy.mode === 'paper') return '';
    if ([strategy.residualQuantity, strategy.inventory, strategy.position?.size].some((quantity) => number(quantity) !== null && Math.abs(Number(quantity)) > 0)) return '先停止并处理持仓/订单';
    if (!Array.isArray(strategy.orders) || strategy.orders.length) return '先停止并处理持仓/订单';
    if (number(strategy.residualQuantity ?? strategy.inventory) !== 0) return '持仓状态待核验';
    const terminal = ['FILLED', 'CANCELED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'TPSL_CANCELED'];
    if ([strategy.protection, strategy.emergency].some((order) => order && !terminal.includes(String(order.status || '').toUpperCase()))) return '先停止并处理持仓/订单';
    if (strategy.marginSetup?.state === 'pending') return '合约设置待确认';
    return '';
  }

  function renderStrategies() {
    const strategies = selectedStrategies().filter((strategy) => !isTrendStrategy(strategy));
    $('strategy-count').textContent = strategies.length;
    $('strategies-body').innerHTML = strategies.length ? strategies.map((strategy) => {
      const stats = strategy.stats || {};
      const value = stats.realizedPnl ?? stats.gridProfit;
      return `<tr><td><div class="primary-text" title="${escape(strategy.name)}"><button class="strategy-name-button" data-inspect-strategy="${escape(strategy.id)}">${escape(strategy.name)}</button><span class="mode-tag ${strategy.mode === 'live' ? 'live' : ''}">${strategy.mode === 'live' ? '实盘' : '模拟'}</span></div><div class="secondary-text">${escape(strategy.market)} · ${strategy.direction === 'short' ? '做空' : '做多'} · ${escape(strategy.gridCount)} 格</div><div class="secondary-text">永续合约 · ${contractSummary(strategy)}</div></td><td><span class="status-pill ${escape(strategy.status)}">${escape(strategyStatus(strategy.status))}</span>${number(strategy.residualQuantity) > 0 ? `<div class="strategy-residual">剩余持仓 ${compact(strategy.residualQuantity)}</div>` : ''}${strategy.outOfRange ? '<div class="strategy-residual">价格超出区间</div>' : ''}</td><td>${compact(strategy.lower, 5)} – ${compact(strategy.upper, 5)}<div class="secondary-text">${isMarginBudget(strategy) ? '投入保证金' : '名义额度（旧配置）'} ${fmt(strategy.budget)} USD</div>${isMarginBudget(strategy) ? `<div class="secondary-text">名义额度上限 ${fmt(strategyNotionalBudget(strategy))} USD</div>` : ''}</td><td><span class="${signClass(value)}">${signed(value)}</span><div class="secondary-text">网格已实现毛收益${number(stats.cycles) !== null ? ` · ${stats.cycles} 轮` : ''}</div></td><td class="align-right"><div class="strategy-actions">${strategyActions(strategy)}</div></td></tr>${strategy.error ? `<tr class="strategy-error-row"><td colspan="5">${escape(strategy.error)}</td></tr>` : ''}`;
    }).join('') : `<tr><td colspan="5" class="table-empty"><strong>${state.accountId ? '还没有合约网格策略' : '连接账户后开始创建'}</strong>${state.accountId ? '在右侧设定区间、保证金与杠杆，创建你的第一条策略。' : '<button type="button" data-open-account>添加交易账户</button>，查看余额并配置合约网格。'}</td></tr>`;
  }

  function renderTrendStrategies() {
    const strategies = selectedStrategies().filter(isTrendStrategy);
    $('trend-strategy-count').textContent = strategies.length;
    $('trend-strategies-body').innerHTML = strategies.length ? strategies.map((strategy) => {
      const value = strategy.stats?.realizedPnl;
      return `<tr><td><div class="primary-text"><button class="strategy-name-button" data-inspect-strategy="${escape(strategy.id)}">${escape(strategy.name)}</button><span class="mode-tag ${strategy.mode === 'live' ? 'live' : ''}">${strategy.mode === 'live' ? '实盘' : '模拟'}</span></div><div class="secondary-text">${escape(strategy.market)} · ${contractSummary(strategy)}</div></td><td><span class="status-pill ${escape(strategy.status)}">${escape(strategyStatus(strategy.status))}</span><div class="secondary-text">${escape(trendDirectionLabel(trendEntryDirection(strategy.signal)))}</div></td><td>投入 ${fmt(strategy.budget)} USD<div class="secondary-text">名义额度上限 ${fmt(strategyNotionalBudget(strategy))} USD</div><div class="secondary-text">持仓数量 ${compact(strategy.residualQuantity)}</div></td><td><span class="${signClass(value)}">${signed(value)}</span><div class="secondary-text">已实现毛收益</div></td><td class="align-right"><div class="strategy-actions">${strategyActions(strategy)}</div></td></tr>${strategy.error ? `<tr class="strategy-error-row"><td colspan="5">${escape(strategy.error)}</td></tr>` : ''}`;
    }).join('') : `<tr><td colspan="5" class="table-empty"><strong>${state.accountId ? '还没有合约趋势策略' : '连接账户后开始创建'}</strong>${state.accountId ? '配置投入保证金，审阅趋势规则后创建策略。' : '<button type="button" data-open-account>添加交易账户</button>以连接资产与自动交易。'}</td></tr>`;
  }
  function trendDirectionLabel(value) { return ({ long: '做多信号', short: '做空信号', none: '暂无开仓信号', flat: '暂无开仓信号', hold: '等待趋势条件' })[String(value || '').toLowerCase()] || value || '等待信号'; }
  function trendEntryDirection(signal) { return signal ? Number(signal.entry_signal) === 1 ? 'long' : Number(signal.entry_signal) === -1 ? 'short' : 'none' : null; }
  function sideLabel(value) { const side = String(value || '').toLowerCase(); return ['long', 'buy', 'b'].includes(side) ? '做多 / 买入' : ['short', 'sell', 's', 'a'].includes(side) ? '做空 / 卖出' : value || '—'; }
  function renderDetails() {
    const account = getAccount();
    document.querySelectorAll('[data-tab]').forEach((tab) => { const active = tab.dataset.tab === state.tab; tab.classList.toggle('active', active); tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; });
    let html;
    if (state.tab === 'logs') {
      const ownedIds = new Set(selectedStrategies().map((strategy) => strategy.id));
      const events = state.data.events.filter((event) => event.accountId ? event.accountId === state.accountId : event.strategyId ? ownedIds.has(event.strategyId) : ['error', 'warning', 'warn'].includes(event.level));
      html = events.length ? `<div class="log-list">${events.slice(0, 100).map((event) => `<div class="log-row"><span class="log-time">${escape(time(event.time, true))}</span><span class="log-level ${escape(event.level)}">${escape(({ info: '信息', error: '错误', warning: '提醒', warn: '提醒', success: '成功' })[event.level] || event.level)}</span><span class="log-message">${escape(event.message)}</span></div>`).join('')}</div>` : '<div class="table-empty"><strong>暂无运行记录</strong>创建策略、启动与成交后，相关事件会显示在这里。</div>';
    } else if (!account) {
      html = '<div class="table-empty"><strong>尚未添加交易账户</strong><button type="button" data-open-account>添加交易账户</button>以读取真实余额、持仓和订单。</div>';
    } else if (state.tab === 'assets') {
      html = `<div class="asset-grid"><div class="asset-item"><span>账户权益</span><div><strong>${fmt(account.equity)}</strong><small>USD</small></div></div><div class="asset-item"><span>可用余额</span><div><strong>${fmt(account.availableBalance)}</strong><small>USD</small></div></div><div class="asset-item"><span>未实现盈亏</span><div><strong class="${signClass(account.unrealizedPnl)}">${signed(account.unrealizedPnl)}</strong><small>USD</small></div></div></div><div class="account-detail-info"><span class="account-address">${escape(accountIdentity(account))} · ${account.connected ? '已连接' : '连接异常'}</span><button class="text-button danger" data-delete-account="${escape(account.id)}">移除账户</button></div>`;
    } else if (state.tab === 'positions') {
      const positions = account.positions || [];
      html = `<div class="table-wrap"><table class="data-table"><thead><tr><th>市场</th><th>方向</th><th>持仓数量</th><th>开仓均价</th><th>标记价格</th><th class="align-right">未实现盈亏</th></tr></thead><tbody>${positions.length ? positions.map((position) => `<tr><td class="primary-text">${escape(position.market || position.symbol)}</td><td class="${['long', 'buy'].includes(String(position.side).toLowerCase()) ? 'positive' : 'negative'}">${escape(sideLabel(position.side))}</td><td>${compact(position.size)}</td><td>${compact(position.entryPrice, 5)}</td><td>${compact(position.markPrice, 5)}</td><td class="align-right ${signClass(position.unrealizedPnl)}">${signed(position.unrealizedPnl)} <span class="secondary-text">USD</span></td></tr>`).join('') : `<tr><td colspan="6" class="table-empty"><strong>${account.connected ? '当前账户暂无持仓' : '持仓数据待同步'}</strong>${account.connected ? '策略成交后，可在这里查看交易所返回的持仓。' : '请同步账户，连接成功后读取交易所持仓。'}</td></tr>`}</tbody></table></div>`;
    } else {
      const orders = account.orders || [];
      html = `<div class="table-wrap"><table class="data-table"><thead><tr><th>市场 / 订单</th><th>方向</th><th>委托价格</th><th>委托数量</th><th>状态</th><th class="align-right">订单类型</th></tr></thead><tbody>${orders.length ? orders.map((order) => `<tr><td><div class="primary-text">${escape(order.market || order.symbol)}</div><div class="secondary-text">${escape(order.id || order.orderId || order.clientOrderId || '—')}</div></td><td>${escape(sideLabel(order.side))}${order.reduceOnly ? '<div class="secondary-text">只减仓</div>' : ''}</td><td>${compact(order.price ?? order.limitPrice, 5)}</td><td>${compact(order.size ?? order.quantity)}</td><td>${escape(order.status || '挂单中')}</td><td class="align-right">${escape(order.type || order.orderType || '限价')}</td></tr>`).join('') : `<tr><td colspan="6" class="table-empty"><strong>${account.connected ? '暂无当前订单' : '订单数据待同步'}</strong>${account.connected ? '交易所未返回当前挂单。模拟策略不会产生交易所订单。' : '连接成功后显示交易所返回的当前订单。'}</td></tr>`}</tbody></table></div>`;
    }
    $('detail-content').innerHTML = html;
  }

  function setView(view) {
    state.view = view;
    const labels = { grid: ['合约网格', '以保证金和杠杆配置永续合约网格，优先挂单开仓与只减仓退出。'], trend: ['合约趋势', '根据已收盘行情判断多空趋势，按保证金和风险规则自动执行。'], future: ['第三模块', '预留永续合约策略模块，等待确定交易逻辑。'] };
    document.querySelectorAll('[data-view]').forEach((button) => { const active = button.dataset.view === view; button.classList.toggle('active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    document.querySelectorAll('.module-view').forEach((section) => { section.hidden = section.id !== `view-${view}`; });
    $('page-title').textContent = labels[view][0]; $('breadcrumb-current').textContent = labels[view][0]; $('page-description').textContent = labels[view][1];
    saveSelection();
    if (view === 'grid') renderChart();
    if (view === 'trend') { if (state.market !== 'ETH-USD' && state.data.markets.some((market) => market.symbol === 'ETH-USD')) changeMarket('ETH-USD'); renderTrendSignal(); renderTrendChart(); if (state.authenticated) loadTrendSignal(); }
  }

  async function loadCandles() {
    if (!state.market) return;
    const request = ++candleRequest;
    const market = state.market;
    state.candleMarket = market;
    state.candleError = '';
    try {
      const result = await api(`/api/markets/${encodeURIComponent(market)}/candles`);
      if (request !== candleRequest || market !== state.market) return;
      state.candles = (result.candles || []).filter((candle) => [candle.open, candle.high, candle.low, candle.close].every((value) => number(value) !== null)).slice(-72);
      if (!state.candles.length) state.candleError = '交易所尚未返回可用的价格历史。';
    } catch (error) { if (request !== candleRequest) return; state.candles = []; state.candleError = error.message; }
    renderChart();
  }

  function gridLevels() {
    const selected = state.data.strategies.find((strategy) => !isTrendStrategy(strategy) && strategy.id === state.selectedStrategyId && strategy.market === state.market);
    const levels = state.preview?.levels || selected?.levels;
    if (levels?.length) return levels.map((level, index) => ({ price: number(typeof level === 'object' ? level.price ?? level.level ?? level.value : level), side: typeof level === 'object' ? level.side : null, index })).filter((level) => level.price !== null);
    return [];
  }

  function renderChart() {
    const chart = $('price-chart');
    const market = getMarket();
    const candles = state.candleMarket === state.market ? state.candles : [];
    const current = number(market?.markPrice);
    const levels = gridLevels();
    if (!candles.length && !levels.length && current === null) {
      chart.innerHTML = `<div class="chart-empty"><span aria-hidden="true">▥</span><strong>${state.candleError ? '价格历史暂不可用' : '等待市场数据'}</strong><p>${escape(state.candleError || '连接后显示价格走势与网格分布。')}</p></div>`;
      chart.setAttribute('aria-label', state.candleError || '等待市场数据');
      return;
    }
    const width = Math.max(340, chart.clientWidth);
    const height = chart.clientHeight || 305;
    const left = 12, right = 75, top = 24, bottom = 30;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const values = [...candles.flatMap((candle) => [Number(candle.high), Number(candle.low)]), ...levels.map((level) => level.price), ...(current === null ? [] : [current])];
    let low = Math.min(...values), high = Math.max(...values);
    const padding = (high - low) * .13 || high * .03 || 1;
    low -= padding; high += padding;
    const y = (price) => top + (high - price) / (high - low) * plotHeight;
    const x = (index) => left + (index + .5) / Math.max(1, candles.length) * plotWidth;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="presentation"><defs><clipPath id="chart-clip"><rect x="${left}" y="${top}" width="${plotWidth}" height="${plotHeight}"/></clipPath></defs>`;
    for (let i = 0; i <= 4; i++) { const price = high - (high - low) * i / 4; const py = y(price); svg += `<line x1="${left}" x2="${width - right + 3}" y1="${py}" y2="${py}" stroke="#edf2f8"/><text x="${width - right + 11}" y="${py + 3}" fill="#a1b0c4" font-size="8.5" font-family="Segoe UI,Microsoft YaHei,sans-serif">${escape(compact(price, priceDigits(market)))}</text>`; }
    for (let i = 1; i <= 5; i++) { const px = left + plotWidth * i / 6; svg += `<line x1="${px}" x2="${px}" y1="${top}" y2="${height - bottom}" stroke="#f5f7fa"/>`; }
    svg += '<g clip-path="url(#chart-clip)">';
    if (levels.length > 1) { const upperY = y(Math.max(...levels.map((level) => level.price))); const lowerY = y(Math.min(...levels.map((level) => level.price))); svg += `<rect x="${left}" y="${upperY}" width="${plotWidth}" height="${lowerY - upperY}" fill="#2168e8" opacity=".025"/>`; }
    levels.forEach((level) => { const isBuy = level.side ? ['buy', 'b', 'long'].includes(String(level.side).toLowerCase()) : current === null || level.price < current; const py = y(level.price); svg += `<line x1="${left}" x2="${width - right}" y1="${py}" y2="${py}" stroke="${isBuy ? '#75bea8' : '#e4a1aa'}" stroke-dasharray="5 4" stroke-width=".85" opacity=".8"/>`; });
    const candleWidth = Math.max(2, Math.min(7, plotWidth / Math.max(1, candles.length) * .55));
    candles.forEach((candle, index) => { const color = Number(candle.close) >= Number(candle.open) ? '#36a58b' : '#d77a86'; const highY = y(Number(candle.high)), lowY = y(Number(candle.low)); const openY = y(Number(candle.open)), closeY = y(Number(candle.close)); const px = x(index); svg += `<line x1="${px}" x2="${px}" y1="${highY}" y2="${lowY}" stroke="${color}" stroke-width="1"/><rect x="${px - candleWidth / 2}" y="${Math.min(openY, closeY)}" width="${candleWidth}" height="${Math.max(1.1, Math.abs(openY - closeY))}" fill="${color}" rx=".6"/>`; });
    if (current !== null) svg += `<line x1="${left}" x2="${width - right}" y1="${y(current)}" y2="${y(current)}" stroke="#4c88e9" stroke-width="1.1" stroke-dasharray="3 3"/>`;
    svg += '</g>';
    if (current !== null) svg += `<rect x="${width - right + 1}" y="${y(current) - 9}" width="${right - 5}" height="18" fill="#2168e8" rx="3"/><text x="${width - right + 5}" y="${y(current) + 3}" fill="#ffffff" font-size="8" font-family="Segoe UI,Microsoft YaHei,sans-serif">${escape(compact(current, priceDigits(market)))}</text>`;
    if (levels.length > 1) { [['区间上限', Math.max(...levels.map((level) => level.price))], ['区间下限', Math.min(...levels.map((level) => level.price))]].forEach(([label, price]) => { svg += `<rect x="${left + 4}" y="${y(price) - 14}" width="48" height="13" fill="#fff" opacity=".85" rx="2"/><text x="${left + 8}" y="${y(price) - 4}" fill="#8395af" font-size="8" font-family="Microsoft YaHei,sans-serif">${label}</text>`; }); }
    if (candles.length) { [0, Math.floor(candles.length / 4), Math.floor(candles.length / 2), Math.floor(candles.length * .75), candles.length - 1].forEach((index) => { const date = new Date(timeValue(candles[index].time)); const label = Number.isNaN(date.getTime()) ? '' : `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`; svg += `<text x="${x(index)}" y="${height - 10}" fill="#a5b2c5" font-size="7.5" text-anchor="${index === 0 ? 'start' : index === candles.length - 1 ? 'end' : 'middle'}" font-family="Segoe UI,sans-serif">${escape(label)}</text>`; }); }
    if (!candles.length) svg += `<text x="${left + plotWidth / 2}" y="${height - 9}" fill="#a5b2c5" font-size="9" text-anchor="middle" font-family="Microsoft YaHei,sans-serif">${escape(state.candleError ? '历史价格暂不可用，仅显示已获取的标记价格与网格' : '等待价格历史')}</text>`;
    svg += '</svg>';
    chart.innerHTML = svg;
    chart.setAttribute('aria-label', `${state.market} 价格图，当前标记价格 ${compact(current)}，${candles.length} 根价格蜡烛，${levels.length} 个网格价位。`);
    const selectedStrategy = state.data.strategies.find((strategy) => strategy.id === state.selectedStrategyId);
    $('chart-note').textContent = levels.length ? state.preview ? `${levels.length} 个网格价位 · 当前配置预览` : `${selectedStrategy?.name || '选中策略'} · ${levels.length} 个网格价位` : state.candleError ? `价格历史暂不可用：${state.candleError}` : '输入网格参数后，在图中预览挂单价位。';
  }

  function gridInput() {
    const form = $('grid-form');
    return { accountId: state.accountId, name: $('strategy-name').value.trim() || `${state.market} 合约网格`, market: state.market, direction: form.elements.direction.value, lower: $('grid-lower').value, upper: $('grid-upper').value, gridCount: Number($('grid-count').value), budget: $('grid-budget').value, budgetType: 'margin', leverage: Number($('grid-leverage').value), marginMode: $('grid-margin-mode').value, mode: form.elements.mode.value };
  }
  function validateGrid(input, partial = false) {
    const errors = [];
    if (!input.accountId) errors.push('请先添加并选择交易账户。');
    if (!input.market) errors.push('请等待交易市场同步。');
    const lower = number(input.lower), upper = number(input.upper), budget = number(input.budget);
    if (lower === null || lower <= 0) errors.push('区间下限需要大于 0。');
    if (upper === null || upper <= 0) errors.push('区间上限需要大于 0。');
    if (lower !== null && upper !== null && lower >= upper) errors.push('区间上限需要大于下限。');
    if (!Number.isInteger(input.gridCount) || input.gridCount < 2 || input.gridCount > 50) errors.push('网格数量需要为 2 至 50 之间的整数。');
    if (budget === null || budget <= 0) errors.push('投入保证金需要大于 0。');
    const maximum = number(getMarket()?.maxLeverage);
    if (getMarket()?.marginStateKnown === false || maximum === 0) errors.push('当前交易时段的保证金要求未确认，暂不可创建开仓策略，请等待行情同步。');
    if (!Number.isInteger(input.leverage) || input.leverage < 1) errors.push('合约杠杆需要为不小于 1 的整数。');
    else if (maximum !== null && maximum >= 1 && input.leverage > maximum) errors.push(`所选市场当前杠杆上限为 ${compact(maximum, 0)} 倍，请降低杠杆。`);
    if (!['cross', 'isolated'].includes(input.marginMode)) errors.push('请选择全仓或逐仓保证金模式。');
    if (input.name.length > 50) errors.push('策略名称不能超过 50 个字符。');
    if (partial && (!input.lower || !input.upper || !input.budget)) return null;
    return errors;
  }
  function renderPreview() {
    const preview = state.preview;
    [['preview-margin', 'allocatedMargin'], ['preview-notional-budget', 'notionalBudget'], ['preview-total-notional', 'totalNotional'], ['preview-initial-margin', 'estimatedInitialMargin'], ['preview-margin-reserve', 'marginReserve'], ['preview-required-collateral', 'requiredCollateral']].forEach(([id, key]) => { $(id).textContent = number(preview?.[key]) === null ? '—' : `${fmt(preview[key], 4)} USD`; });
    $('preview-quantity').textContent = preview ? compact(preview.quantity) : '—';
    $('preview-notional').textContent = preview ? `${fmt(preview.orderNotional)}${number(preview.maxOrderNotional) !== null && Number(preview.maxOrderNotional) !== Number(preview.orderNotional) ? ` – ${fmt(preview.maxOrderNotional)}` : ''} USD` : '—';
    $('preview-profit').textContent = preview ? `${compact(preview.estimatedCycleProfit, 4)} USD` : '—';
    $('preview-status').textContent = state.previewBusy ? '计算中…' : preview ? preview.errors?.length ? '参数待调整' : '参数已校验' : '等待参数';
    renderChart();
  }
  function showGridErrors(errors) { $('grid-errors').hidden = !errors?.length; $('grid-errors').innerHTML = errors?.length ? `<ul>${errors.map((error) => `<li>${escape(typeof error === 'string' ? error : error.message || JSON.stringify(error))}</li>`).join('')}</ul>` : ''; }
  async function previewGrid(full = false) {
    const request = ++state.previewRequest;
    const input = gridInput();
    const errors = validateGrid(input, !full);
    state.preview = null;
    if (errors === null) { state.previewBusy = false; showGridErrors([]); renderPreview(); return null; }
    if (errors.length) { state.previewBusy = false; showGridErrors(full ? errors : []); renderPreview(); return null; }
    state.previewBusy = true; showGridErrors([]); renderPreview();
    try {
      const result = await api('/api/strategies/preview', { method: 'POST', body: input });
      if (request !== state.previewRequest) return null;
      state.preview = result;
      showGridErrors(result.errors || []);
      return result;
    } catch (error) { if (request === state.previewRequest) showGridErrors([error.message]); return null; }
    finally { if (request === state.previewRequest) { state.previewBusy = false; renderPreview(); } }
  }
  function queuePreview() { clearTimeout(previewTimer); state.previewRequest++; state.selectedStrategyId = null; state.preview = null; renderPreview(); previewTimer = window.setTimeout(() => previewGrid(), 450); }
  function updateMode() { const live = $('grid-form').elements.mode.value === 'live'; $('mode-description').textContent = live ? '启动后设置该合约杠杆与保证金模式，再提交真实挂单。' : '使用真实市场价格模拟合约网格，不修改交易所设置或提交订单。'; $('mode-description').className = `field-help${live ? ' live-description' : ''}`; renderContractControls(); }

  function trendInput() {
    return { accountId: state.accountId, name: $('trend-name').value.trim() || 'ETH-USD 趋势', market: 'ETH-USD', budget: $('trend-budget').value, budgetType: 'margin', leverage: Number($('trend-leverage').value), marginMode: $('trend-margin-mode').value, mode: $('trend-form').elements.mode.value };
  }
  function validateTrend(input, partial = false) {
    if (partial && !input.budget) return null;
    const errors = [];
    if (!input.accountId) errors.push('请先添加并选择交易账户。');
    if (input.market !== 'ETH-USD' || !state.data.markets.some((market) => market.symbol === 'ETH-USD')) errors.push('请等待 ETH-USD 永续合约市场同步。');
    if (number(input.budget) === null || Number(input.budget) <= 0) errors.push('投入保证金需要大于 0。');
    const market = state.data.markets.find((item) => item.symbol === input.market);
    const maximum = number(market?.maxLeverage);
    if (market?.marginStateKnown === false || maximum === 0) errors.push('当前交易时段的保证金要求未确认，暂不可创建开仓策略。');
    if (!Number.isInteger(input.leverage) || input.leverage < 1) errors.push('合约杠杆需要为不小于 1 的整数。');
    else if (input.leverage > 5) errors.push('趋势参考策略杠杆上限为 5 倍。');
    else if (maximum !== null && input.leverage > maximum) errors.push(`所选市场杠杆上限为 ${compact(maximum, 0)} 倍，请降低杠杆。`);
    if (!['cross', 'isolated'].includes(input.marginMode)) errors.push('请选择全仓或逐仓保证金模式。');
    if (!['paper', 'live'].includes(input.mode)) errors.push('请选择模拟或实盘执行模式。');
    if (input.name.length > 50) errors.push('策略名称不能超过 50 个字符。');
    return errors;
  }
  function showTrendErrors(errors) { $('trend-errors').hidden = !errors?.length; $('trend-errors').innerHTML = errors?.length ? `<ul>${errors.map((error) => `<li>${escape(typeof error === 'string' ? error : error.message || JSON.stringify(error))}</li>`).join('')}</ul>` : ''; }
  function setTrendData(result, market) {
    state.trendSignal = result;
    state.trendSignalMarket = market;
    state.trendSignalError = '';
    state.trendSignalUpdatedAt = Date.now();
    state.trendCandles = (result.candles || []).filter((candle) => [candle.open, candle.high, candle.low, candle.close].every((value) => number(value) !== null)).slice(-96);
    renderTrendSignal(); renderTrendChart();
  }
  async function loadTrendSignal() {
    if (!state.authenticated || state.market !== 'ETH-USD' || state.trendSignalBusy) return null;
    const request = ++state.trendSignalRequest;
    state.trendSignalBusy = true;
    try { const result = await api('/api/trend/signals?market=ETH-USD'); if (request !== state.trendSignalRequest || state.market !== 'ETH-USD') return null; setTrendData(result, 'ETH-USD'); return result; }
    catch (error) { if (request === state.trendSignalRequest) { state.trendSignalError = `信号同步失败：${error.message}。请刷新后再核对。`; renderTrendSignal(); renderTrendChart(); } return null; }
    finally { if (request === state.trendSignalRequest) state.trendSignalBusy = false; }
  }
  function renderTrendSignal() {
    const result = state.trendSignalMarket === state.market ? state.trendSignal : null;
    const signal = result?.signal;
    const rules = result?.rules;
    const direction = trendEntryDirection(signal);
    $('trend-signal-direction').textContent = state.trendSignalError ? '信号同步异常' : result?.warmup === false ? '指标预热中' : trendDirectionLabel(direction);
    $('trend-signal-direction').className = direction === 'long' ? 'positive' : direction === 'short' ? 'negative' : '';
    $('trend-signal-time').textContent = number(signal?.open_time_ms) !== null ? time(Number(signal.open_time_ms) + 3600000, true) : '—';
    $('trend-atr').textContent = compact(signal?.atr, 4);
    $('trend-adx').textContent = compact(signal?.adx, 2);
    $('trend-supertrend').textContent = Number(signal?.trend) === 1 ? '多头' : Number(signal?.trend) === -1 ? '空头' : '—';
    $('trend-initial-stop').textContent = compact(state.trendPreview?.initialStop ?? state.trendPreview?.plan?.initialStop, priceDigits());
    const errors = result?.errors || [];
    $('trend-signal-reason').textContent = state.trendSignalError || (errors.length ? errors.map((item) => typeof item === 'string' ? item : item.message || JSON.stringify(item)).join('；') : signal ? direction === 'none' ? '最近已收盘小时线没有 ADX > 20 的合格翻转，等待新的趋势信号。' : '该小时线出现合格翻转。策略仅处理启动后的新信号，不追入已发生的历史信号。' : '读取最新已收盘 K 线后计算信号。');
    $('trend-source-description').textContent = rules?.source ? `${rules.source} · 使用 Arcus 独立行情` : '等待已验证的策略规则与行情';
    $('trend-rule-name').textContent = rules?.name || rules?.source || '策略规则待同步';
    $('trend-period').textContent = rules?.timeframe === '1h' ? '1 小时' : rules?.timeframe || '周期待同步';
    const parameters = rules ? [['ATR / ADX 周期', `${compact(rules.atrPeriod, 0)} / ${compact(rules.adxPeriod, 0)}`], ['趋势带宽', `${compact(rules.bandAtr)} × ATR`], ['入场 ADX', `> ${compact(rules.adxMin)}`], ['初始止损', `${compact(rules.stopAtr)} × ATR`], ['单笔风险上限', `${compact(Number(rules.tradeRisk) * 100)}%`], ['单日亏损上限', `${compact(Number(rules.dailyLossLimit) * 100)}%`], ['保本触发', `${compact(rules.breakevenR)}R`], ['开仓挂单偏移', `${compact(rules.entryOffset)} USD`], ['普通平仓挂单偏移', `${compact(rules.exitOffset)} USD`], ['止损超时兜底', `${compact(Number(rules.stopTimeoutMs) / 1000)} 秒`]] : [];
    $('trend-rule-details').innerHTML = parameters.map(([label, value]) => `<div><dt>${escape(label)}</dt><dd>${escape(typeof value === 'object' ? JSON.stringify(value) : value)}</dd></div>`).join('');
    $('trend-execution-note').textContent = rules?.executionNote || '优先挂单开仓与平仓；保护限价止损可能按 Taker 成交。初始止损由交易所托管；2R 保本与触发后 10 秒的市价兜底需服务器在线。';
  }
  function renderTrendChart() {
    const chart = $('trend-price-chart');
    const candles = state.trendSignalMarket === state.market ? state.trendCandles : [];
    if (!candles.length) { chart.innerHTML = `<div class="chart-empty"><span aria-hidden="true">↗</span><strong>${state.trendSignalError ? '趋势行情暂不可用' : '等待趋势行情'}</strong><p>${escape(state.trendSignalError || '读取真实行情后显示价格与趋势判定。')}</p></div>`; chart.setAttribute('aria-label', '趋势图等待真实行情'); return; }
    const width = Math.max(340, chart.clientWidth), height = chart.clientHeight || 270;
    const left = 14, right = 74, top = 22, bottom = 30;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const prices = candles.flatMap((candle) => [Number(candle.high), Number(candle.low)]);
    let low = Math.min(...prices), high = Math.max(...prices);
    const padding = (high - low) * .12 || high * .02 || 1;
    low -= padding; high += padding;
    const y = (value) => top + (high - value) / (high - low) * plotHeight;
    const x = (index) => left + (index + .5) / candles.length * plotWidth;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="presentation">`;
    for (let index = 0; index <= 4; index++) { const price = high - (high - low) * index / 4, py = y(price); svg += `<line x1="${left}" x2="${width - right}" y1="${py}" y2="${py}" stroke="#edf2f8"/><text x="${width - right + 9}" y="${py + 3}" fill="#95a6bc" font-size="8.5" font-family="Segoe UI,Microsoft YaHei,sans-serif">${escape(compact(price, priceDigits()))}</text>`; }
    const candleWidth = Math.max(2, Math.min(8, plotWidth / candles.length * .55));
    candles.forEach((candle, index) => { const color = Number(candle.close) >= Number(candle.open) ? '#36a58b' : '#d77a86'; const px = x(index), openY = y(Number(candle.open)), closeY = y(Number(candle.close)); svg += `<line x1="${px}" x2="${px}" y1="${y(Number(candle.high))}" y2="${y(Number(candle.low))}" stroke="${color}" stroke-width="1"/><rect x="${px - candleWidth / 2}" y="${Math.min(openY, closeY)}" width="${candleWidth}" height="${Math.max(1.1, Math.abs(openY - closeY))}" fill="${color}" rx=".6"/>`; });
    [0, Math.floor(candles.length / 2), candles.length - 1].forEach((index) => { const date = new Date(timeValue(candles[index].time)); const label = Number.isNaN(date.getTime()) ? '' : `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:00`; svg += `<text x="${x(index)}" y="${height - 10}" fill="#95a6bc" font-size="8" text-anchor="${index === 0 ? 'start' : index === candles.length - 1 ? 'end' : 'middle'}" font-family="Segoe UI,Microsoft YaHei,sans-serif">${escape(label)}</text>`; });
    chart.innerHTML = `${svg}</svg>`;
    chart.setAttribute('aria-label', `${state.market} 趋势价格图，包含 ${candles.length} 根交易所返回的价格 K 线。`);
  }
  function renderTrendPreview() {
    const preview = state.trendPreview;
    const values = { ...preview, riskCapital: preview?.riskCapital ?? preview?.plan?.plannedRiskCapital, plannedRisk: preview?.plannedRisk ?? preview?.plannedLoss, totalNotional: preview?.totalNotional ?? preview?.orderNotional };
    [['trend-preview-margin', 'allocatedMargin'], ['trend-preview-risk-capital', 'riskCapital'], ['trend-preview-planned-risk', 'plannedRisk'], ['trend-preview-notional-budget', 'notionalBudget'], ['trend-preview-notional', 'totalNotional'], ['trend-preview-initial-margin', 'estimatedInitialMargin'], ['trend-preview-reserve', 'marginReserve'], ['trend-preview-buffer', 'collateralBuffer'], ['trend-preview-required', 'requiredCollateral']].forEach(([id, key]) => { $(id).textContent = number(values[key]) === null ? '—' : `${fmt(values[key], 4)} USD`; });
    $('trend-preview-quantity').textContent = compact(preview?.quantity);
    $('trend-preview-status').textContent = state.trendPreviewBusy ? '计算中…' : preview ? preview.errors?.length ? '参数待调整' : '参数已校验' : '等待配置';
    if (preview && !preview.errors?.length && preview.eligible === false) $('trend-preview-status').textContent = '等待开仓条件';
    const warnings = preview?.warnings;
    $('trend-preview-note').textContent = warnings?.length ? warnings.map((item) => typeof item === 'string' ? item : item.message || JSON.stringify(item)).join('；') : '风险资金取投入保证金与可用保证金的较小值，单笔计划风险上限 3%。实际亏损以成交为准。';
    $('trend-initial-stop').textContent = compact(preview?.initialStop ?? preview?.plan?.initialStop, priceDigits());
  }
  async function previewTrend(full = false) {
    const request = ++state.trendPreviewRequest, market = state.market;
    const input = trendInput(), errors = validateTrend(input, !full);
    state.trendPreview = null;
    if (errors === null || errors.length) { state.trendPreviewBusy = false; showTrendErrors(full ? errors : []); renderTrendPreview(); return null; }
    state.trendPreviewBusy = true; showTrendErrors([]); renderTrendPreview();
    try { const result = await api('/api/trend/preview', { method: 'POST', body: input }); if (request !== state.trendPreviewRequest || market !== state.market) return null; state.trendPreview = result; if (result.signal || result.candles || result.rules) setTrendData(result, market); showTrendErrors(result.errors || []); return result; }
    catch (error) { if (request === state.trendPreviewRequest) showTrendErrors([error.message]); return null; }
    finally { if (request === state.trendPreviewRequest) { state.trendPreviewBusy = false; renderTrendPreview(); } }
  }
  function queueTrendPreview() { clearTimeout(trendPreviewTimer); state.trendPreviewRequest++; state.trendPreview = null; renderTrendPreview(); trendPreviewTimer = window.setTimeout(() => previewTrend(), 450); }
  function updateTrendMode() { const live = $('trend-form').elements.mode.value === 'live'; $('trend-mode-description').textContent = live ? '启动后设置合约杠杆与保证金模式，信号触发时提交真实订单。' : '使用真实行情模拟，不修改交易所设置或提交订单。'; $('trend-mode-description').className = `field-help${live ? ' live-description' : ''}`; renderContractControls('trend'); }

  function orderStatus(value) { return ({ OPEN: '挂单中', NEW: '新委托', PARTIALLY_FILLED: '部分成交', PARTIAL: '部分成交', FILLED: '已成交', CANCELED: '已撤单', CANCELLED: '已撤单', REJECTED: '被拒绝', EXPIRED: '已过期', SUBMITTING: '提交中', UNKNOWN: '待核对' })[String(value || '').toUpperCase()] || value || '—'; }
  function renderStrategyDetail() {
    const strategy = state.data.strategies.find((item) => item.id === state.selectedStrategyId);
    if (!strategy || strategy.accountId !== state.accountId) { state.selectedStrategyId = null; if ($('strategy-dialog').open) closeDialog('strategy-dialog'); return; }
    const blocked = strategyDeleteReason(strategy);
    $('delete-inspected-strategy').disabled = Boolean(blocked);
    $('delete-inspected-strategy').dataset.deleteStrategy = strategy.id;
    $('delete-inspected-strategy').title = blocked || '删除这条策略记录';
    $('strategy-delete-reason').textContent = blocked;
    $('strategy-delete-reason').hidden = !blocked;
    if (isTrendStrategy(strategy)) { renderTrendStrategyDetail(strategy); return; }
    const stats = strategy.stats || {};
    const orders = strategy.orders || [];
    const simulated = strategy.mode === 'paper';
    const residual = number(strategy.residualQuantity ?? strategy.inventory);
    $('strategy-detail-title').textContent = strategy.name;
    $('strategy-detail-subtitle').textContent = `${strategy.market} 永续合约 · ${strategy.direction === 'short' ? '做空' : '做多'} · ${contractSummary(strategy)} · ${simulated ? '模拟运行' : '实盘交易'} · ${strategyStatus(strategy.status)}`;
    $('strategy-detail-body').innerHTML = `<div class="strategy-metrics"><div><span>网格已实现毛收益 / USD</span><strong class="${signClass(stats.realizedPnl)}">${signed(stats.realizedPnl)}</strong></div><div><span>完成循环</span><strong>${compact(stats.cycles, 0)}</strong></div><div><span>${simulated ? '模拟' : '策略'}剩余持仓数量</span><strong>${compact(residual)}</strong></div><div><span>当前策略挂单</span><strong>${orders.length}</strong></div></div>${residual > 0 && ['paused', 'stopped', 'error'].includes(strategy.status) ? '<div class="strategy-detail-alert">策略仍有剩余持仓。暂停或停止不会市价平仓，恢复后会继续处理相应的只减仓退出单。</div>' : ''}${strategy.outOfRange ? '<div class="strategy-detail-alert">当前价格已超出网格区间，请检查持仓和剩余订单。</div>' : ''}<p class="strategy-detail-note">区间 ${compact(strategy.lower)} – ${compact(strategy.upper)} · ${strategy.gridCount} 格 · 每格数量 ${compact(strategy.quantity)}。毛收益未扣除手续费与资金费。${simulated ? '这里显示的是模拟订单与模拟持仓，交易所账户资产不受影响。' : '这里显示本策略管理的订单，账户全部持仓与当前订单请在工作台下方查看。'}</p><h3 class="strategy-order-heading">${simulated ? '模拟策略订单' : '策略订单'}</h3><div class="table-wrap"><table class="data-table"><thead><tr><th>网格</th><th>方向 / 用途</th><th>价格</th><th>数量 / 已成交</th><th>订单状态</th><th>执行方式</th></tr></thead><tbody>${orders.length ? orders.map((order) => `<tr><td>第 ${Number(order.slot) + 1} 格</td><td>${escape(sideLabel(order.side))}<div class="secondary-text">${order.leg === 'exit' ? '退出 / 只减仓' : '开仓'}</div></td><td>${compact(order.price, 6)}</td><td>${compact(order.size)}<div class="secondary-text">已成交 ${compact(order.filledSize)}</div></td><td>${escape(orderStatus(order.status || order.state))}</td><td>${simulated ? '模拟' : escape(order.timeInForce || '—')}</td></tr>`).join('') : '<tr><td colspan="6" class="table-empty"><strong>当前没有策略订单</strong>订单提交与成交后，状态会自动同步。</td></tr>'}</tbody></table></div>${strategy.error ? `<p class="form-error">${escape(strategy.error)}</p>` : ''}`;
  }
  function renderTrendStrategyDetail(strategy) {
    const simulated = strategy.mode === 'paper';
    const orders = strategy.orders || [];
    const residual = number(strategy.residualQuantity);
    $('strategy-detail-title').textContent = strategy.name;
    $('strategy-detail-subtitle').textContent = `${strategy.market} 永续合约 · 趋势自动多空 · ${contractSummary(strategy)} · ${simulated ? '模拟运行' : '实盘交易'} · ${strategyStatus(strategy.status)}`;
    $('strategy-detail-body').innerHTML = `<div class="strategy-metrics"><div><span>已实现毛收益 / USD</span><strong class="${signClass(strategy.stats?.realizedPnl)}">${signed(strategy.stats?.realizedPnl)}</strong></div><div><span>${simulated ? '模拟' : '策略'}持仓数量</span><strong>${compact(residual)}</strong></div><div><span>投入保证金 / USD</span><strong>${fmt(strategy.budget)}</strong></div><div><span>当前策略订单</span><strong>${orders.length}</strong></div></div>${residual > 0 && ['paused', 'stopped', 'error'].includes(strategy.status) ? '<div class="strategy-detail-alert">新开仓已停止。已有持仓继续受初始止损、保本和超时兜底管理；不会立即市价平仓。</div>' : ''}<p class="strategy-detail-note">参考 quant 当前 ETH 1 小时 SuperTrend 规则，使用 Arcus 自身行情。合格反转信号先退出反向持仓，不立即反手；启动时不追入历史信号。</p><p class="strategy-detail-note">优先挂单开仓与平仓，限价保护止损可能按 Taker 成交。初始止损由交易所托管；2R 保本与触发后 10 秒的市价兜底需服务器在线。毛收益未扣除手续费与资金费。</p><h3 class="strategy-order-heading">${simulated ? '模拟策略订单' : '策略订单'}</h3><div class="table-wrap"><table class="data-table"><thead><tr><th>用途</th><th>方向</th><th>价格</th><th>数量 / 已成交</th><th>订单状态</th><th>执行方式</th></tr></thead><tbody>${orders.length ? orders.map((order) => `<tr><td>${escape(({ entry: '开仓', exit: '退出', stop: '保护止损', protection: '保护止损', emergency: '市价兜底' })[order.leg] || order.leg || '策略委托')}</td><td>${escape(sideLabel(order.side))}</td><td>${compact(order.price, 6)}</td><td>${order.positionTpsl && number(order.size) === 0 ? '全部策略持仓' : compact(order.size)}<div class="secondary-text">已成交 ${compact(order.filledSize)}</div></td><td>${escape(orderStatus(order.status || order.state))}</td><td>${simulated ? '模拟' : escape(order.timeInForce || order.orderType || '—')}${!simulated && order.positionTpsl ? '<div class="secondary-text">可能按 Taker 成交</div>' : ''}</td></tr>`).join('') : '<tr><td colspan="6" class="table-empty"><strong>当前没有策略订单</strong>信号满足开仓条件时，策略按规则执行。</td></tr>'}</tbody></table></div>${strategy.error ? `<p class="form-error">${escape(strategy.error)}</p>` : ''}`;
  }
  function inspectStrategy(id) {
    const strategy = state.data.strategies.find((item) => item.id === id && item.accountId === state.accountId);
    if (!strategy) return;
    if (strategy.market !== state.market) changeMarket(strategy.market);
    clearTimeout(previewTimer); state.previewRequest++; state.preview = null; state.selectedStrategyId = id;
    renderStrategyDetail(); renderChart(); $('strategy-dialog').showModal();
  }

  function openAccount() { setError('account-form-error', ''); $('account-dialog').showModal(); $('new-account-name').focus(); }
  function closeDialog(id) { const dialog = $(id); dialog.close(); if (id === 'account-dialog') { $('account-form').reset(); setError('account-form-error', ''); } if (id === 'confirm-dialog') state.confirmAction = null; }
  function openConfirm({ title, message, details = [], live = false, danger = false, button = '确认', action }) {
    $('confirm-title').textContent = title; $('confirm-message').textContent = message;
    $('confirm-details').innerHTML = details.map(([key, value]) => `<div><dt>${escape(key)}</dt><dd>${escape(value)}</dd></div>`).join('');
    $('confirm-checkbox-label').hidden = !live; $('confirm-checkbox').checked = false;
    $('confirm-submit').className = `button ${danger ? 'button-danger' : 'button-primary'}`; $('confirm-submit').textContent = button; $('confirm-submit').disabled = live;
    setError('confirm-error', ''); state.confirmAction = action;
    $('confirm-dialog').showModal();
  }
  async function performStrategyAction(strategy, action, button) {
    busy(button, true);
    try { await api(`/api/strategies/${encodeURIComponent(strategy.id)}/${action}`, { method: 'POST' }); toast(`${strategy.name}：${({ start: '已请求启动', pause: '已请求暂停', stop: '已请求停止' })[action]}`); await pollState(); }
    catch (error) { toast(error.message, true); }
    finally { busy(button, false); }
  }
  function handleStrategyAction(strategy, action, button) {
    if (strategy.accountId !== state.accountId) return;
    if (isTrendStrategy(strategy)) { handleTrendStrategyAction(strategy, action, button); return; }
    if (action === 'start' && strategy.mode === 'live') {
      const account = state.data.accounts.find((item) => item.id === strategy.accountId);
      const fundingDetails = isMarginBudget(strategy) ? [['投入保证金', `${fmt(strategy.budget)} USD（含缓冲预留）`], ['名义额度上限', `${fmt(strategyNotionalBudget(strategy))} USD`]] : [['名义额度（旧配置）', `${fmt(strategy.budget)} USD`]];
      const contractDetails = isMarginBudget(strategy) ? ['杠杆与保证金模式', `${compact(strategyLeverage(strategy), 0)} 倍 · ${marginModeLabel(strategy.marginMode)}`] : ['合约设置', '沿用交易所现有设置，旧配置未指定'];
      const message = isMarginBudget(strategy) ? '确认后先同步该账户的合约杠杆与保证金模式，再向 Arcus 提交真实挂单。全仓共用账户保证金，价格超出区间时也需关注持仓。' : '旧配置按名义金额全额预留资金，沿用交易所已有杠杆与保证金模式，不放大订单。确认后向 Arcus 提交真实挂单。';
      openConfirm({ title: '启动实盘合约网格', message, live: true, button: '确认并启动实盘', details: [['策略', strategy.name], ['交易账户', accountIdentity(account)], ['永续合约与方向', `${strategy.market} · ${strategy.direction === 'short' ? '做空' : '做多'}`], contractDetails, ['价格区间', `${compact(strategy.lower)} – ${compact(strategy.upper)}`], ...fundingDetails], action: async () => { await api(`/api/strategies/${encodeURIComponent(strategy.id)}/start`, { method: 'POST' }); toast('已请求启动实盘合约网格'); await pollState(); } });
    } else if (action === 'stop') {
      openConfirm({ title: '停止网格策略', message: '策略将停止新开仓并撤销面板挂单。已有持仓不会市价平仓，请在账户持仓中确认并自行处理。', button: '停止策略', danger: true, details: [['策略', strategy.name], ['执行模式', strategy.mode === 'live' ? '实盘' : '模拟']], action: async () => { await api(`/api/strategies/${encodeURIComponent(strategy.id)}/stop`, { method: 'POST' }); toast('已请求停止策略'); await pollState(); } });
    } else performStrategyAction(strategy, action, button);
  }
  function handleTrendStrategyAction(strategy, action, button) {
    if (action === 'start' && strategy.mode === 'live') {
      const account = state.data.accounts.find((item) => item.id === strategy.accountId);
      openConfirm({ title: '启动实盘合约趋势', message: '启动后同步合约设置，按新的已收盘信号自动交易。开仓与普通平仓优先挂单；限价保护止损可能吃单，触发后 10 秒仍有余量时可市价退出。初始止损由交易所托管，2R 保本与超时兜底需服务器在线。同一账户的 ETH-USD 持仓不可与手工交易或其他策略混用。其他市场可继续交易，共享可用保证金。', live: true, button: '确认并启动实盘', details: [['策略', strategy.name], ['交易账户', accountIdentity(account)], ['永续合约', strategy.market], ['趋势规则', '1h SuperTrend · ATR14 × 4 · ADX14 > 20'], ['杠杆与保证金模式', `${compact(strategyLeverage(strategy), 0)} 倍 · ${marginModeLabel(strategy.marginMode)}`], ['投入保证金', `${fmt(strategy.budget)} USD`], ['名义额度上限', `${fmt(strategyNotionalBudget(strategy))} USD`], ['计划风险', '可用资金的 3%，受策略投入上限约束']], action: async () => { await api(`/api/strategies/${encodeURIComponent(strategy.id)}/start`, { method: 'POST' }); toast('已请求启动实盘合约趋势'); await pollState(); } });
    } else if (action === 'stop') {
      openConfirm({ title: '停止趋势新开仓', message: '停止新开仓并撤销未成交开仓单；已有持仓继续受初始止损、保本和超时兜底管理。不会立即市价平仓，请在账户与策略详情中持续查看持仓。', button: '停止新开仓', danger: true, details: [['策略', strategy.name], ['执行模式', strategy.mode === 'live' ? '实盘' : '模拟']], action: async () => { await api(`/api/strategies/${encodeURIComponent(strategy.id)}/stop`, { method: 'POST' }); toast('趋势新开仓已请求停止，已有持仓继续受策略保护'); await pollState(); } });
    } else performStrategyAction(strategy, action, button);
  }

  function handleDeleteStrategy(strategy) {
    if (strategy.accountId !== state.accountId) return;
    const blocked = strategyDeleteReason(strategy);
    if (blocked) { toast(blocked, true); return; }
    const account = state.data.accounts.find((item) => item.id === strategy.accountId);
    const message = strategy.mode === 'paper' ? '清除这条模拟策略及其模拟持仓和模拟订单，账户真实成交统计保留。此操作不改变交易所的真实持仓或订单。' : '删除该账户的这条面板策略记录。此操作不发送交易所撤单或平仓请求，已发生的真实成交仍计入账户统计。后台会再次核验策略状态、持仓和待处理订单。';
    openConfirm({ title: '删除策略记录', message, button: '删除策略', danger: true, details: [['交易账户', accountIdentity(account)], ['策略', strategy.name], ['模块与市场', `${isTrendStrategy(strategy) ? '合约趋势' : '合约网格'} · ${strategy.market}`], ['执行模式', strategy.mode === 'live' ? '实盘' : '模拟']], action: async () => {
      const result = await api(`/api/strategies/${encodeURIComponent(strategy.id)}`, { method: 'DELETE' });
      if (result.deleted !== true || result.id !== strategy.id) throw new Error('删除结果尚未确认，请刷新策略列表。');
      state.data.strategies = state.data.strategies.filter((item) => item.id !== strategy.id);
      if (state.selectedStrategyId === strategy.id) { state.selectedStrategyId = null; if ($('strategy-dialog').open) closeDialog('strategy-dialog'); }
      renderStrategies(); renderTrendStrategies(); renderChart(); renderDetails();
      toast(`已删除策略：${strategy.name}`);
      await pollState();
    } });
  }

  function changeAccount(id) { if (id !== state.accountId) { state.selectedStrategyId = null; if ($('strategy-dialog').open) closeDialog('strategy-dialog'); if ($('confirm-dialog').open) closeDialog('confirm-dialog'); } state.accountId = id; saveSelection(); renderAccounts(); renderStrategies(); renderTrendStrategies(); renderDetails(); queuePreview(); if (state.view === 'trend') queueTrendPreview(); else { state.trendPreview = null; state.trendPreviewRequest++; renderTrendPreview(); } }
  function changeMarket(symbol) { if (symbol === state.market) return; state.market = symbol; state.candles = []; state.candleError = ''; state.preview = null; state.previewRequest++; state.trendPreview = null; state.trendPreviewRequest++; $('grid-lower').value = ''; $('grid-upper').value = ''; saveSelection(); renderMarket(); renderChart(); renderTrendSignal(); renderTrendChart(); queuePreview(); if (state.view === 'trend') { queueTrendPreview(); loadTrendSignal(); } loadCandles(); }

  $('login-form').addEventListener('submit', async (event) => { event.preventDefault(); const button = event.submitter; busy(button, true, '正在登录…'); setError('login-error', ''); try { await api('/api/login', { method: 'POST', body: { password: $('login-password').value } }); $('login-password').value = ''; await showApp(); } catch (error) { setError('login-error', error.message); } finally { busy(button, false); } });
  $('logout-button').addEventListener('click', async () => { try { await api('/api/logout', { method: 'POST' }); showLogin(); } catch (error) { toast(error.message, true); } });
  $('refresh-button').addEventListener('click', async (event) => { busy(event.currentTarget, true, '刷新中…'); await Promise.allSettled([pollState(), loadCandles()]); busy($('refresh-button'), false); });
  $('refresh-account').addEventListener('click', async (event) => { if (!state.accountId) return; const button = event.currentTarget; busy(button, true, '同步中…'); try { const account = await api(`/api/accounts/${encodeURIComponent(state.accountId)}/refresh`, { method: 'POST' }); await pollState(); toast(account.connected ? '账户已同步' : account.error || '账户同步失败', !account.connected); } catch (error) { toast(error.message, true); } finally { busy(button, false); } });
  $('account-select').addEventListener('change', (event) => changeAccount(event.target.value));
  $('chart-market').addEventListener('change', (event) => changeMarket(event.target.value));
  $('strategy-market').addEventListener('change', (event) => changeMarket(event.target.value));
  $('trend-market').addEventListener('change', (event) => changeMarket(event.target.value));
  $('add-account-button').addEventListener('click', openAccount); $('sidebar-add-account').addEventListener('click', openAccount);
  $('account-dialog').addEventListener('cancel', () => { $('account-form').reset(); });
  $('account-dialog').addEventListener('close', () => { $('account-form').reset(); });
  $('account-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const button = $('account-submit'); const form = event.currentTarget;
    const input = { name: form.elements.name.value.trim(), address: form.elements.address.value.trim(), apiKey: form.elements.apiKey.value.trim(), signingKey: form.elements.signingKey.value.trim(), accountIndex: Number(form.elements.accountIndex.value) };
    if (!input.name) return setError('account-form-error', '请输入账户名称。');
    busy(button, true, '连接中…'); setError('account-form-error', '');
    try { const account = await api('/api/accounts', { method: 'POST', body: input }); state.accountId = account.id; closeDialog('account-dialog'); toast('交易账户已连接'); await pollState(); queuePreview(); }
    catch (error) { setError('account-form-error', error.message); }
    finally { input.apiKey = ''; input.signingKey = ''; busy(button, false); }
  });
  $('grid-form').addEventListener('input', () => { updateMode(); queuePreview(); });
  $('grid-form').addEventListener('change', (event) => { if (event.target.id !== 'strategy-market') { updateMode(); queuePreview(); } });
  $('leverage-presets').addEventListener('click', (event) => { const button = event.target.closest('[data-leverage]'); if (!button) return; $('grid-leverage').value = button.dataset.leverage; renderContractControls(); queuePreview(); });
  $('suggest-range').addEventListener('click', () => { const market = getMarket(); const price = number(market?.markPrice); if (price === null) return; const tick = number(market.tickSize) || .01; const digits = priceDigits(market); $('grid-lower').value = (Math.floor(price * .95 / tick) * tick).toFixed(digits); $('grid-upper').value = (Math.ceil(price * 1.05 / tick) * tick).toFixed(digits); queuePreview(); });
  $('grid-form').addEventListener('submit', async (event) => {
    event.preventDefault(); clearTimeout(previewTimer); const button = $('create-strategy'); busy(button, true, '校验并创建…');
    try { const preview = await previewGrid(true); if (!preview || preview.errors?.length) return; const strategy = await api('/api/strategies', { method: 'POST', body: gridInput() }); toast(`已创建${strategy.mode === 'live' ? '实盘' : '模拟'}网格，请在列表中启动`); await pollState(); }
    catch (error) { showGridErrors([error.message]); }
    finally { busy(button, false); button.disabled = !state.accountId || !state.market; }
  });
  $('trend-form').addEventListener('input', () => { updateTrendMode(); queueTrendPreview(); });
  $('trend-form').addEventListener('change', (event) => { if (event.target.id !== 'trend-market') { updateTrendMode(); queueTrendPreview(); } });
  $('trend-leverage-presets').addEventListener('click', (event) => { const button = event.target.closest('[data-leverage]'); if (!button) return; $('trend-leverage').value = button.dataset.leverage; renderContractControls('trend'); queueTrendPreview(); });
  $('refresh-trend').addEventListener('click', async (event) => { const button = event.currentTarget; busy(button, true, '同步中…'); try { await loadTrendSignal(); if ($('trend-budget').value) await previewTrend(); } finally { busy(button, false); } });
  $('trend-form').addEventListener('submit', async (event) => {
    event.preventDefault(); clearTimeout(trendPreviewTimer); const button = $('create-trend-strategy'); busy(button, true, '校验并创建…');
    try { const preview = await previewTrend(true); if (!preview || preview.errors?.length) return; const strategy = await api('/api/trend/strategies', { method: 'POST', body: trendInput() }); toast(`已创建${strategy.mode === 'live' ? '实盘' : '模拟'}趋势策略，请在列表中启动`); await pollState(); }
    catch (error) { showTrendErrors([error.message]); }
    finally { busy(button, false); button.disabled = !state.accountId || !state.market; }
  });
  $('confirm-checkbox').addEventListener('change', (event) => { $('confirm-submit').disabled = !event.target.checked; });
  $('confirm-form').addEventListener('submit', async (event) => {
    event.preventDefault(); if (!state.confirmAction) return; if (!$('confirm-checkbox-label').hidden && !$('confirm-checkbox').checked) return;
    const action = state.confirmAction; const button = $('confirm-submit'); busy(button, true, '执行中…'); setError('confirm-error', '');
    try { await action(); closeDialog('confirm-dialog'); }
    catch (error) { setError('confirm-error', error.message); }
    finally { busy(button, false); }
  });
  $('emergency-button').addEventListener('click', () => openConfirm({ title: '停止全部策略', message: '网格停止并撤销面板挂单；趋势停止新开仓并撤销未成交开仓单，已有趋势持仓继续受初始止损、保本和超时兜底管理。已有持仓不会立即市价平仓，其他来源订单不在此操作范围内。', button: '停止全部策略', danger: true, action: async () => { const result = await api('/api/emergency-stop', { method: 'POST' }); await pollState(); if (result.errors?.length) throw new Error(`部分策略停止失败：${result.errors.map((error) => error.error || error.message).join('；')}`); toast('已请求停止全部策略，请检查策略状态与持仓'); } }));
  document.addEventListener('click', (event) => {
    const view = event.target.closest('[data-view]'); if (view) setView(view.dataset.view);
    const account = event.target.closest('[data-account-id]'); if (account) changeAccount(account.dataset.accountId);
    const tab = event.target.closest('[data-tab]'); if (tab) { state.tab = tab.dataset.tab; renderDetails(); }
    const close = event.target.closest('[data-close]'); if (close) closeDialog(close.dataset.close);
    if (event.target.closest('[data-open-account]')) openAccount();
    const inspect = event.target.closest('[data-inspect-strategy]'); if (inspect) inspectStrategy(inspect.dataset.inspectStrategy);
    const deletion = event.target.closest('[data-delete-strategy]'); if (deletion && !deletion.disabled) { const strategy = state.data.strategies.find((item) => item.id === deletion.dataset.deleteStrategy); if (strategy) handleDeleteStrategy(strategy); }
    const action = event.target.closest('[data-strategy-id]'); if (action) { const strategy = state.data.strategies.find((item) => item.id === action.dataset.strategyId); if (strategy) handleStrategyAction(strategy, action.dataset.action, action); }
    const remove = event.target.closest('[data-delete-account]'); if (remove) { const account = getAccount(); openConfirm({ title: '移除交易账户', message: '移除后将删除本面板保存的账户凭证。请先停止该账户的所有策略，并确认挂单与持仓已经处理。', details: [['账户', accountIdentity(account)]], button: '移除账户', danger: true, action: async () => { await api(`/api/accounts/${encodeURIComponent(remove.dataset.deleteAccount)}`, { method: 'DELETE' }); toast('账户已移除'); await pollState(); } }); }
  });
  document.querySelector('.detail-tabs').addEventListener('keydown', (event) => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const tabs = [...document.querySelectorAll('[data-tab]')]; const current = tabs.findIndex((tab) => tab.dataset.tab === state.tab); const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; state.tab = tabs[index].dataset.tab; renderDetails(); tabs[index].focus(); });
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = window.setTimeout(() => { renderChart(); renderTrendChart(); }, 100); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && state.authenticated) { pollState(); loadCandles(); } });
  async function boot() { try { const auth = await api('/api/auth'); $('logout-button').hidden = !auth.configured; if (auth.authenticated) await showApp(); else { showLogin(); if (!auth.configured) { setError('login-error', '服务器尚未配置控制台密码，请完成服务器访问配置后登录。'); $('login-form').querySelector('button[type=submit]').disabled = true; } } } catch (error) { showLogin(); setError('login-error', `无法连接控制台：${error.message}`); } }
  boot();
})();
