"""Arcus grid control plane. UI reads cached data; only worker and actions trade."""
import asyncio
import os
import re
import threading
import time
from decimal import Decimal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .arcus import ArcusClient, ArcusError
from .grid import GridEngine
from .trend import TrendEngine
from .security import authenticated, check_password, create_session, revoke_session
from .store import Store
from .account_statistics import AccountStatisticsService
from .strategy_management import delete_strategy as remove_strategy, StrategyDeletionBlocked

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.3.0'
DATA_DIR = Path(os.environ.get('DATA_DIR', str(ROOT / 'data')))
store = Store(DATA_DIR, os.environ['VAULT_KEY'])
control_lock = threading.RLock()
public_client = ArcusClient()
markets_cache = []
markets_time = 0.0
markets_error = None
fee_cache = {}
fee_time = 0.0
login_attempts = {}
clients = {}


def client_factory(account):
    with control_lock:
        if account['id'] not in clients:
            clients[account['id']] = ArcusClient(store.credentials(account))
        return clients[account['id']]


engine = GridEngine(store, client_factory)
trend_engine = TrendEngine(store, client_factory)
account_statistics = AccountStatisticsService(DATA_DIR / 'account-statistics', client_factory)
trend_candle_cache = {}


def strategy_engine(strategy):
    return trend_engine if strategy.get('type') == 'trend' else engine


def trend_market(market):
    """One public hourly candle cache shared by all accounts; never invent closed bars."""
    symbol = market['symbol']
    cached = trend_candle_cache.get(symbol)
    if cached is None or time.time() - cached['time'] > 30:
        try:
            candles = public_client.trend_candles(symbol, '1h', 500)
            cached = {'time': time.time(), 'candles': candles, 'error': None}
            trend_candle_cache[symbol] = cached
        except Exception as exc:
            cached = {**(cached or {'time': 0, 'candles': []}), 'error': safe_error(exc)}
            trend_candle_cache[symbol] = cached
    # A failed read is explicit. Consumers may manage existing inventory, not open.
    return {**market, 'candles': cached['candles'], 'candlesUpdatedAt': int(cached['time'] * 1000),
            'candlesError': cached['error']}


def safe_error(error):
    # Never serialize headers, raw requests, or private key material.
    text = str(error)
    text = re.sub(r'(?i)(?:0x)?[0-9a-f]{64,}', '[redacted]', text)
    return text[:400]


def account_view(account):
    return {**account['snapshot'], 'id': account['id'], 'name': account['name'], 'address': account['address'],
            'accountIndex': int(store.credentials(account).get('accountIndex', 0)),
            'statistics': account_statistics.snapshot(account['id'])}


def refresh_account(account):
    try:
        snap = client_factory(account).account()
        snap.update(connected=True, error=None, updatedAt=int(time.time() * 1000))
    except Exception as exc:
        snap = {**account['snapshot'], 'connected': False, 'error': safe_error(exc)}
    store.save_snapshot(account['id'], snap)
    return {**account, 'snapshot': snap}


def market_for(symbol):
    market = next((m for m in markets_cache if m['symbol'] == symbol), None)
    if not market:
        raise HTTPException(400, '交易市场不存在或行情尚未同步')
    return market


def strategy_market(strategy):
    return next((m for m in markets_cache if m['symbol'] == strategy['market']),
                {'symbol': strategy['market'], 'marketId': strategy['marketId'], 'status': 'OFFLINE',
                 'markPrice': '0', 'bestBid': None, 'bestAsk': None})


def one_cycle():
    global markets_cache, markets_time, markets_error, fee_cache, fee_time
    try:
        if time.time() - fee_time > 3600:
            tiers = public_client.fee_tiers()
            base = next((t for t in tiers if t['level'] == 0), None)
            if base:
                fee_cache = {'makerFee': base['makerRate'], 'takerFee': base['takerRate'], 'feeSource': '基础费率'}
                fee_time = time.time()
        fresh = public_client.markets()
        fresh = [{**m, **fee_cache} for m in fresh]
        with control_lock:
            markets_cache = fresh
            markets_time = time.time()
            markets_error = None
    except Exception as exc:
        markets_error = safe_error(exc)
    for account in store.accounts():
        if time.time() * 1000 - account['snapshot'].get('updatedAt', 0) > 15000:
            with control_lock:
                refresh_account(account)
    for strategy in store.strategies():
        holding_trend = strategy.get('type') == 'trend' and (Decimal(str(strategy.get('residualQuantity') or '0')) != 0
                         or bool(strategy.get('orders')) or bool(strategy.get('protection')))
        if strategy.get('status') not in ('running', 'pausing', 'stopping', 'recovering', 'error', 'configuring') and not holding_trend:
            continue
        with control_lock:
            strategy = store.get_strategy(strategy['id'])
            if not strategy:
                # A user may delete a safe record after this cycle took its list.
                continue
            account = store.get_account(strategy['accountId'])
            if not account:
                continue
            market = None
            try:
                market = strategy_market(strategy)
                if strategy.get('type') == 'trend':
                    market = trend_market(market)
                if time.time() - markets_time > 20:
                    # Continue order reconciliation and cancellations with invalid pricing.
                    market = {**market, 'markPrice': '0', 'bestBid': None, 'bestAsk': None}
                # BBO is mandatory for entry gating; the exchange additionally enforces ALO.
                if market.get('markPrice') != '0':
                    try:
                        bbo = public_client.bbo(market)
                        market = {**market, 'bestBid': (bbo.get('bestBid') or {}).get('price'),
                                  'bestAsk': (bbo.get('bestAsk') or {}).get('price')}
                    except Exception as exc:
                        # A BBO read failure blocks post-only placement, while a
                        # freshly synchronized mark remains valid for risk exits.
                        market = {**market, 'bestBid': None, 'bestAsk': None}
                if strategy.get('type') == 'trend':
                    if strategy['mode'] == 'paper':
                        market['availableForStrategy'] = strategy['budget']
                    else:
                        try:
                            reserved = sum((strategy_engine(other).remaining_collateral(other, account, strategy_market(other))
                                            for other in store.strategies() if other['id'] != strategy['id']
                                            and other['accountId'] == account['id'] and other['mode'] == 'live'
                                            and other['status'] not in ('draft', 'stopped')), Decimal(0))
                            market['availableForStrategy'] = str(max(Decimal(0),
                                Decimal(str(account['snapshot'].get('availableBalance') or '0')) - reserved))
                        except Exception as exc:
                            # An unknown reservation blocks fresh exposure, but
                            # must not skip native protection and exit reconciliation.
                            market['availableForStrategy'] = '0'
                            trend_engine._fault(strategy, '其他策略预留保证金尚未确认：' + safe_error(exc))
                strategy_engine(strategy).tick(strategy, account, market)
            except Exception as exc:
                # A fault must not abandon resting orders. Keep polling persisted IDs.
                changed = strategy.get('error') != safe_error(exc)
                strategy.update(error=safe_error(exc), suspended=True,
                                status='error' if strategy['mode'] == 'paper' else 'recovering')
                store.save_strategy(strategy)
                if changed:
                    store.event('error', f"{strategy['name']}：{safe_error(exc)}", strategy['id'])


async def worker():
    while True:
        try:
            await asyncio.to_thread(one_cycle)
        except Exception as exc:
            store.event('error', '后台同步异常，下一轮重试：' + safe_error(exc))
        await asyncio.sleep(5)


async def statistics_worker():
    # Historical pagination never holds up the trading/reconciliation worker.
    while True:
        for account in store.accounts():
            try:
                await asyncio.to_thread(account_statistics.sync_step, account, max_pages=1)
            except Exception as exc:
                store.event('warning', '账户成交统计同步异常：' + safe_error(exc), accountId=account['id'])
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    # Durable states survive restart; worker reconciles every persisted client id.
    tasks = [asyncio.create_task(worker()), asyncio.create_task(statistics_worker())]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title='Arcus Grid', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware('http')
async def protect(request: Request, call_next):
    path = request.url.path
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and path.startswith('/api/'):
        origin = request.headers.get('origin')
        expected = os.environ.get('PUBLIC_ORIGIN', str(request.base_url).rstrip('/'))
        if origin and origin.rstrip('/') != expected.rstrip('/'):
            return JSONResponse({'detail': '请求来源不匹配'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '跨站请求已拒绝'}, status_code=403)
        if int(request.headers.get('content-length', '0') or 0) > 16384:
            return JSONResponse({'detail': '请求内容过大'}, status_code=413)
    if path.startswith('/api/') and path not in ('/api/health', '/api/auth', '/api/login'):
        if not authenticated(store, request.cookies.get('arcus_session')):
            return JSONResponse({'detail': '请先登录'}, status_code=401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


@app.exception_handler(ArcusError)
async def arcus_error_handler(request, exc):
    return JSONResponse({'detail': safe_error(exc)}, status_code=400)


@app.exception_handler(ValueError)
async def value_error_handler(request, exc):
    return JSONResponse({'detail': safe_error(exc)}, status_code=400)


@app.exception_handler(RequestValidationError)
async def invalid_input_handler(request, exc):
    # Pydantic's default error includes the user's supplied value, including keys.
    fields = [str(error['loc'][-1]) for error in exc.errors()]
    return JSONResponse({'detail': '输入格式不正确：' + '、'.join(dict.fromkeys(fields))}, status_code=422)


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class AccountInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=50)
    address: str = Field(pattern=r'^0x[0-9a-fA-F]{40}$')
    apiKey: str = Field(pattern=r'^(0x)?[0-9a-fA-F]{64}$')
    signingKey: str = Field(pattern=r'^(0x)?[0-9a-fA-F]{64}$')
    accountIndex: int = Field(default=0, ge=0, le=9)


class GridInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    accountId: str
    name: str = Field(default='网格策略', min_length=1, max_length=50)
    market: str = Field(min_length=1, max_length=30)
    direction: str = Field(pattern='^(long|short)$')
    lower: str | float
    upper: str | float
    gridCount: int = Field(ge=2, le=50)
    budget: str | float
    budgetType: str = Field(default='margin', pattern='^margin$')
    leverage: int = Field(default=1, ge=1, le=1000, strict=True)
    marginMode: str = Field(default='cross', pattern='^(cross|isolated)$')
    mode: str = Field(pattern='^(paper|live)$')


class TrendInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    accountId: str
    name: str = Field(default='ETH SuperTrend 合约趋势', min_length=1, max_length=50)
    market: str = Field(default='ETH-USD', pattern='^ETH-USD$')
    budget: str | float
    budgetType: str = Field(default='margin', pattern='^margin$')
    leverage: int = Field(default=5, ge=1, le=5, strict=True)
    marginMode: str = Field(default='isolated', pattern='^(cross|isolated)$')
    mode: str = Field(pattern='^(paper|live)$')


@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': VERSION}


@app.get('/api/auth')
def auth(request: Request):
    return {'authenticated': authenticated(store, request.cookies.get('arcus_session')),
            'configured': bool(os.environ.get('ADMIN_PASSWORD_HASH'))}


@app.post('/api/login')
def login(body: Login, request: Request, response: Response):
    ip = request.client.host
    now = time.time()
    attempts = [t for t in login_attempts.get(ip, []) if t > now - 300]
    if len(attempts) >= 8:
        raise HTTPException(429, '登录尝试过多，请 5 分钟后重试')
    if not check_password(body.password, os.environ.get('ADMIN_PASSWORD_HASH', '')):
        login_attempts[ip] = attempts + [now]
        raise HTTPException(401, '密码不正确')
    login_attempts.pop(ip, None)
    token = create_session(store)
    response.set_cookie('arcus_session', token, httponly=True, secure=os.environ.get('COOKIE_SECURE', '1') == '1',
                        samesite='strict', max_age=43200, path='/')
    return {'authenticated': True}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    revoke_session(store, request.cookies.get('arcus_session'))
    response.delete_cookie('arcus_session')
    return {'authenticated': False}


@app.get('/api/state')
def state():
    with control_lock:
        strategies = store.strategies()
        # Internal reconciliation records contain no secrets, but keep them in backend.
        public_strategies = [{k: v for k, v in s.items() if k != 'slots'} for s in strategies]
        return {'accounts': [account_view(a) for a in store.accounts()], 'markets': markets_cache,
                'strategies': public_strategies, 'events': store.events(),
                'server': {'time': int(time.time() * 1000), 'version': VERSION, 'pollingInterval': 5000,
                           'marketsUpdatedAt': int(markets_time * 1000), 'marketsError': markets_error}}


@app.post('/api/accounts')
def add_account(body: AccountInput):
    with control_lock:
        credentials = body.model_dump(exclude={'name'})
        credentials['address'] = credentials['address'].lower()
        credentials['apiKey'] = credentials['apiKey'].removeprefix('0x')
        credentials['signingKey'] = credentials['signingKey'].removeprefix('0x')
        for old in store.accounts():
            if old['address'] == credentials['address'] and store.credentials(old)['accountIndex'] == body.accountIndex:
                raise HTTPException(409, '这个钱包的子账户已经添加')
        client = ArcusClient(credentials)
        client.validate_key()
        snap = client.account()
        snap.update(connected=True, error=None, updatedAt=int(time.time() * 1000))
        account = store.add_account(body.name.strip(), credentials, snap)
        clients[account['id']] = client
        account_statistics.request_sync(account['id'])
        store.event('info', f'已添加账户：{body.name.strip()}', accountId=account['id'])
        return account_view(account)


@app.post('/api/accounts/{account_id}/refresh')
def account_refresh(account_id: str):
    with control_lock:
        account = store.get_account(account_id)
        if not account:
            raise HTTPException(404, '账户不存在')
        account_statistics.request_sync(account_id)
        return account_view(refresh_account(account))


@app.delete('/api/accounts/{account_id}')
def delete_account(account_id: str):
    with control_lock:
        if not store.get_account(account_id):
            raise HTTPException(404, '账户不存在')
        strategies = [s for s in store.strategies() if s['accountId'] == account_id]
        if any(s['status'] != 'stopped' or Decimal(s.get('residualQuantity', '0')) != 0 or bool(s.get('orders'))
               or bool(s.get('protection')) for s in strategies):
            raise HTTPException(409, '请先停止该账户的所有策略；有待处理挂单或持仓时不能删除账户')
        store.delete_account(account_id)
        account_statistics.forget(account_id)
        clients.pop(account_id, None)
        return {'deleted': True}


@app.delete('/api/strategies/{strategy_id}')
def delete_strategy(strategy_id: str):
    with control_lock:
        strategy = store.get_strategy(strategy_id)
        if not strategy:
            raise HTTPException(404, '策略不存在')
        account = store.get_account(strategy['accountId'])
        try:
            return remove_strategy(store, strategy, account, client_factory)
        except StrategyDeletionBlocked as exc:
            raise HTTPException(409, safe_error(exc)) from exc


@app.get('/api/markets/{symbol}/candles')
def candles(symbol: str):
    return {'candles': public_client.candles(symbol)}


@app.post('/api/strategies/preview')
def preview(body: GridInput):
    return engine.preview(body.model_dump(), market_for(body.market))


@app.post('/api/strategies')
def create_strategy(body: GridInput):
    with control_lock:
        if not store.get_account(body.accountId):
            raise HTTPException(400, '请先添加并选择账户')
        strategy = engine.create(body.model_dump(), market_for(body.market))
        store.save_strategy(strategy)
        store.event('info', f'已创建{body.mode == "paper" and "模拟" or "实盘"}网格：{body.name}', strategy['id'])
        return {k: v for k, v in strategy.items() if k != 'slots'}


@app.get('/api/trend/signals')
def trend_signals(market: str = 'ETH-USD'):
    if market != 'ETH-USD':
        raise HTTPException(400, '当前移植策略仅支持 ETH-USD 永续合约')
    with control_lock:
        enriched = trend_market(market_for(market))
        return trend_engine.signals(enriched)


@app.post('/api/trend/preview')
def trend_preview(body: TrendInput):
    with control_lock:
        account = store.get_account(body.accountId)
        if not account:
            raise HTTPException(400, '请先添加并选择账户')
        market = trend_market(market_for(body.market))
        market['availableForStrategy'] = account['snapshot'].get('availableBalance') if body.mode == 'live' else body.budget
        return trend_engine.preview(body.model_dump(), market)


@app.post('/api/trend/strategies')
def trend_create(body: TrendInput):
    with control_lock:
        if not store.get_account(body.accountId):
            raise HTTPException(400, '请先添加并选择账户')
        strategy = trend_engine.create(body.model_dump(), trend_market(market_for(body.market)))
        store.save_strategy(strategy)
        store.event('info', f'已创建合约趋势策略：{body.name}', strategy['id'])
        return {k: v for k, v in strategy.items() if k != 'slots'}


@app.post('/api/strategies/{strategy_id}/{action}')
def strategy_action(strategy_id: str, action: str):
    if action not in ('start', 'pause', 'stop'):
        raise HTTPException(404, '操作不存在')
    with control_lock:
        strategy = store.get_strategy(strategy_id)
        if not strategy:
            raise HTTPException(404, '策略不存在')
        account = store.get_account(strategy['accountId'])
        market = strategy_market(strategy)
        chosen_engine = strategy_engine(strategy)
        if strategy.get('type') == 'trend':
            market = trend_market(market)
            if strategy['mode'] == 'paper':
                market['availableForStrategy'] = strategy['budget']
        if action == 'start':
            if time.time() - markets_time > 20:
                raise HTTPException(409, '请等待行情恢复同步后启动')
            for other in store.strategies():
                if other['id'] != strategy_id and other['accountId'] == strategy['accountId'] and other['market'] == strategy['market']:
                    if other['status'] not in ('stopped', 'draft'):
                        raise HTTPException(409, '同一账户同一市场已有策略，请先处理已有策略')
            if strategy['mode'] == 'live':
                account = refresh_account(account)
                reserved = sum((strategy_engine(other).remaining_collateral(other, account, strategy_market(other)) for other in store.strategies()
                                if other['id'] != strategy_id and other['accountId'] == account['id']
                                and other['mode'] == 'live' and other['status'] not in ('draft', 'stopped')
                                ), Decimal(0))
                if not strategy.get('hasStarted') and Decimal(str(account['snapshot'].get('availableBalance') or '0')) < reserved + Decimal(strategy['budget']):
                    raise HTTPException(409, '可用保证金不足以覆盖本策略投入与其他策略的预留保证金')
                market['availableForStrategy'] = str(max(Decimal(0), Decimal(str(account['snapshot'].get('availableBalance') or '0')) - reserved))
            chosen_engine.start(strategy, account, market)
        else:
            getattr(chosen_engine, action)(strategy, account, market)
        store.save_strategy(strategy)
        return {k: v for k, v in strategy.items() if k != 'slots'}


@app.post('/api/emergency-stop')
def emergency():
    errors = []
    with control_lock:
        for strategy in store.strategies():
            if strategy['status'] == 'draft':
                continue
            try:
                strategy_engine(strategy).stop(strategy, store.get_account(strategy['accountId']), strategy_market(strategy))
            except Exception as exc:
                errors.append({'strategyId': strategy['id'], 'error': safe_error(exc)})
        store.event('warning', '已请求停止全部策略的新开仓；趋势持仓继续保留保护退出，网格持仓保留且不强制市价平仓')
    return {'requested': True, 'errors': errors}


app.mount('/static', StaticFiles(directory=ROOT / 'frontend'), name='static')


@app.get('/')
def index():
    return FileResponse(ROOT / 'frontend' / 'index.html')
