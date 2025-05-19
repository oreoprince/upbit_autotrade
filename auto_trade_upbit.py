
import time
import datetime
import logging
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo  # Python 3.9+

import pyupbit
import requests
import schedule

# — 설정 —  
access = "YOUR_UPBIT_ACCESS_KEY"
secret = "YOUR_UPBIT_SECRET_KEY"
DISCORD_WEBHOOK_URL = "YOUR_DISCORD_WEBHOOK_URL"
ASSETS = {"ETH": 1.0}
K = 0.7               # 변동성 돌파 계수
MIN_KRW = 5000        # 최소 주문 금액

# — 로깅 설정 —
logger = logging.getLogger("trading_bot")
handler = RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=5)
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# — 전역 변수 초기화 —
daily_start_balance = None
remaining_krw = None
bought = {}
sold = {}
trade_log = {}

# — 유틸 함수 —
def send_discord(msg: str):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=5)
    except Exception as e:
        logger.error(f"Discord 알림 실패: {e}")


def get_target_price_12h(ticker: str, k: float) -> float:
    """12시간 기준 변동성 돌파 가격 계산"""
    df = pyupbit.get_ohlcv(ticker, interval="minute60", count=12)
    prev_close = df['close'].iloc[0]
    range_sum = (df['high'] - df['low']).sum()
    return prev_close + range_sum * k


def get_start_time() -> datetime.datetime:
    """Upbit 일봉 인덱스(00:00 KST) 리턴"""
    df = pyupbit.get_ohlcv("KRW-ETH", interval="day", count=1)
    return df.index[0].to_pydatetime()


def get_now_kst() -> datetime.datetime:
    """Asia/Seoul 기준 현재 시각(naive datetime)"""
    return datetime.datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)


def get_balance(currency: str) -> float:
    for b in upbit.get_balances():
        if b["currency"] == currency:
            return float(b.get("balance", 0) or 0)
    return 0.0


def get_current_price(ticker: str) -> float:
    return pyupbit.get_orderbook(ticker=ticker)["orderbook_units"][0]["ask_price"]


def fetch_order_details(uuid: str):
    try:
        return upbit.get_order(uuid)
    except Exception as e:
        logger.error(f"체결 상세 조회 실패(UUID={uuid}): {e}")
        return None


def notify(action: str, ticker: str, volume: float, price: float):
    msg = f"{action} | {ticker} | 수량: {volume:.6f} | 체결가: {price:.0f}원"
    logger.info(msg)
    send_discord(msg)


def reset_flags():
    global daily_start_balance, remaining_krw, bought, sold, trade_log
    # 일일 요약
    send_daily_summary()
    # 초기화
    remaining_krw = get_balance("KRW")
    daily_start_balance = remaining_krw
    bought = {a: False for a in ASSETS}
    sold   = {a: False for a in ASSETS}
    trade_log = {a: {"buy": None, "sell": None} for a in ASSETS}
    send_daily_start_report()
    send_discord("🔄 플래그 리셋 완료")


def send_daily_summary():
    global daily_start_balance, remaining_krw
    daily_end_balance = remaining_krw
    profit = daily_end_balance - daily_start_balance
    roi = (profit / daily_start_balance * 100) if daily_start_balance else 0.0
    lines = ["📊 거래 요약"]
    for asset, logs in trade_log.items():
        buy = logs.get("buy")
        sell = logs.get("sell")
        if buy:
            lines.append(f"- {asset} 매수: {buy['volume']:.6f} @ {buy['price']:.0f}원 ({buy['time']})")
        else:
            lines.append(f"- {asset} 매수: 없음")
        if sell:
            lines.append(f"  매도: {sell['volume']:.6f} @ {sell['price']:.0f}원 ({sell['time']})")
        else:
            lines.append(f"  매도: 없음")
    lines += [
        f"💰 시작 잔고: {daily_start_balance:,.0f}원",
        f"💰 종료 잔고: {daily_end_balance:,.0f}원",
        f"📈 하루 수익: {profit:,.0f}원 ({roi:.2f}%)"
    ]
    send_discord("\n".join(lines))


def send_daily_start_report():
    global daily_start_balance
    lines = ["🌅 시작 보고", f"💰 잔고: {daily_start_balance:,.0f}원"]
    for asset in ASSETS:
        ticker = f"KRW-{asset}"
        target = get_target_price_12h(ticker, K)
        lines.append(f"- {asset} 목표가: {target:.0f}원")
    send_discord("\n".join(lines))

# — 초기화 —
upbit = pyupbit.Upbit(access, secret)
remaining_krw = get_balance("KRW")
daily_start_balance = remaining_krw
send_discord("🔔 Autotrade 시작")
logger.info("Autotrade start")
bought = {a: False for a in ASSETS}
sold   = {a: False for a in ASSETS}
trade_log = {a: {"buy": None, "sell": None} for a in ASSETS}

# — 스케줄: KST 자정 및 정오에 플래그 리셋 —
schedule.every().day.at("00:00").do(reset_flags)
schedule.every().day.at("12:00").do(reset_flags)

# — 메인 루프 —
while True:
    try:
        schedule.run_pending()
        now = get_now_kst()
        start_time = get_start_time()
        end_time = start_time + datetime.timedelta(days=1)

        # 원래 시간 구간 (KST 기준)
        t1140 = start_time + datetime.timedelta(hours=11, minutes=40)
        t1200 = start_time + datetime.timedelta(hours=12)
        t2340 = start_time + datetime.timedelta(hours=23, minutes=40)

        # 1) 00:00~11:40 매수
        if start_time <= now < t1140:
            if remaining_krw > MIN_KRW:
                for asset, weight in ASSETS.items():
                    if bought[asset]: continue
                    ticker = f"KRW-{asset}"
                    target = get_target_price_12h(ticker, K)
                    price  = get_current_price(ticker)
                    if price > target:
                        alloc = remaining_krw * weight
                        if alloc < MIN_KRW:
                            logger.warning(f"{asset} 할당금액 {alloc:.0f}원 미달")
                            bought[asset] = True
                            continue
                        order = upbit.buy_market_order(ticker, alloc * 0.9995)
                        details = fetch_order_details(order["uuid"])
                        vol = float(details.get("executed_volume", order["volume"]))  
                        avg = float(details.get("average_price", order["price"]))
                        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                        trade_log[asset]["buy"] = {"volume": vol, "price": avg, "time": now_str}
                        notify("BUY", ticker, vol, avg)
                        remaining_krw -= vol * avg
                        bought[asset] = True
        # 2) 11:40~12:00 매도
        elif t1140 <= now < t1200:
            for asset in ASSETS:
                if sold[asset]: continue
                ticker = f"KRW-{asset}"
                bal = get_balance(asset)
                if bal > 0:
                    order = upbit.sell_market_order(ticker, bal * 0.9995)
                    details = fetch_order_details(order["uuid"])
                    vol = float(details.get("executed_volume", order["volume"]))  
                    avg = float(details.get("average_price", order["price"]))
                    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                    trade_log[asset]["sell"] = {"volume": vol, "price": avg, "time": now_str}
                    notify("SELL", ticker, vol, avg)
                    sold[asset] = True
        # 3) 12:00~23:40 매수
        elif t1200 <= now < t2340:
            if remaining_krw > MIN_KRW:
                for asset, weight in ASSETS.items():
                    if bought[asset]: continue
                    ticker = f"KRW-{asset}"
                    target = get_target_price_12h(ticker, K)
                    price  = get_current_price(ticker)
                    if price > target:
                        alloc = remaining_krw * weight
                        if alloc < MIN_KRW:
                            logger.warning(f"{asset} 할당금액 {alloc:.0f}원 미달")
                            bought[asset] = True
                            continue
                        order = upbit.buy_market_order(ticker, alloc * 0.9995)
                        details = fetch_order_details(order["uuid"])
                        vol = float(details.get("executed_volume", order["volume"]))  
                        avg = float(details.get("average_price", order["price"]))
                        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                        trade_log[asset]["buy"] = {"volume": vol, "price": avg, "time": now_str}
                        notify("BUY", ticker, vol, avg)
                        remaining_krw -= vol * avg
                        bought[asset] = True
        # 4) 23:40~24:00 매도
        elif t2340 <= now < end_time:
            for asset in ASSETS:
                if sold[asset]: continue
                ticker = f"KRW-{asset}"
                bal = get_balance(asset)
                if bal > 0:
                    order = upbit.sell_market_order(ticker, bal * 0.9995)
                    details = fetch_order_details(order["uuid"])
                    vol = float(details.get("executed_volume", order["volume"]))  
                    avg = float(details.get("average_price", order["price"]))
                    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                    trade_log[asset]["sell"] = {"volume": vol, "price": avg, "time": now_str}
                    notify("SELL", ticker, vol, avg)
                    sold[asset] = True

        time.sleep(1)

    except (requests.exceptions.RequestException, pyupbit.PyUpbitError) as e:
        msg = f"⚠️ 일시적 오류: {e} – 10초 후 재시도"
        logger.warning(msg)
        send_discord(msg)
        time.sleep(10)
    except Exception as e:
        msg = f"❌ 치명적 오류 발생: {e} – 종료합니다"
        logger.error(msg, exc_info=True)
        send_discord(msg)
        break
