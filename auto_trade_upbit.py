import time
import datetime
import logging
from logging.handlers import RotatingFileHandler

import pyupbit
import requests
import schedule

# — 설정 —  
access = "YOUR_UPBIT_ACCESS_KEY"
secret = "YOUR_UPBIT_SECRET_KEY"
DISCORD_WEBHOOK_URL = ""  # 본인 웹훅 URL

ASSETS = {"BTC": 0.3, "ETH": 0.4, "SOL": 0.3}
K = 0.7               # 변동성 돌파 계수
MIN_KRW = 5000        # 최소 주문 금액

# — 로깅 설정 —  
logger = logging.getLogger("trading_bot")
handler = RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=5)
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.setLevel(logging.INFO)
logger.addHandler(handler)

def send_discord(msg: str):
    """Discord 웹훅으로 메시지 전송 (단일 시도)"""
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=5)
    except Exception as e:
        logger.error(f"Discord 알림 실패: {e}")

def get_target_price(ticker: str, k: float) -> float:
    df = pyupbit.get_ohlcv(ticker, interval="day", count=2)
    return df.iloc[0]["close"] + (df.iloc[0]["high"] - df.iloc[0]["low"]) * k

def get_start_time() -> datetime.datetime:
    df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=1)
    return df.index[0].to_pydatetime()

def get_balance(currency: str) -> float:
    for b in upbit.get_balances():
        if b["currency"] == currency:
            return float(b["balance"] or 0)
    return 0.0

def get_current_price(ticker: str) -> float:
    return pyupbit.get_orderbook(ticker=ticker)["orderbook_units"][0]["ask_price"]

def fetch_order_details(uuid: str):
    """주문 UUID로 체결 상세 조회 (평균 체결가, 체결량)"""
    try:
        return upbit.get_order(uuid)
    except Exception as e:
        logger.error(f"체결 상세 조회 실패(UUID={uuid}): {e}")
        return None

def notify(action: str, ticker: str, volume: float, price: float):
    """로그 출력 + Discord 알림"""
    msg = f"{action} | {ticker} | 수량: {volume:.6f} | 체결가: {price:.0f}"
    logger.info(msg)
    send_discord(msg)

def reset_flags():
    """자정에 플래그·로그 초기화"""
    global bought, sold, trade_log, remaining_krw
    for a in ASSETS:
        bought[a] = False
        sold[a]   = False
        trade_log[a] = {"buy": None, "sell": None}
    remaining_krw = get_balance("KRW")
    send_discord("🔄 자정 플래그 리셋 완료")

# — 초기화 —  
upbit = pyupbit.Upbit(access, secret)
send_discord("🔔 Autotrade 시작")
logger.info("Autotrade start")

remaining_krw = get_balance("KRW")
bought = {a: False for a in ASSETS}
sold   = {a: False for a in ASSETS}
trade_log = {a: {"buy": None, "sell": None} for a in ASSETS}

schedule.every().day.at("00:00").do(reset_flags)

# — 메인 루프 —  
while True:
    try:
        schedule.run_pending()
        now = datetime.datetime.now()
        start_time = get_start_time()
        end_time = start_time + datetime.timedelta(days=1)

        # 매수 구간 (00:00:00 ~ 23:59:50)
        if start_time < now < end_time - datetime.timedelta(seconds=10):
            if remaining_krw > MIN_KRW:
                for asset, weight in ASSETS.items():
                    if bought[asset]:
                        continue

                    ticker = f"KRW-{asset}"
                    target = get_target_price(ticker, K)
                    price  = get_current_price(ticker)

                    if price > target:
                        alloc = remaining_krw * weight
                        if alloc < MIN_KRW:
                            logger.warning(f"{asset} 할당금액 {alloc:.0f}원 미달로 스킵")
                            bought[asset] = True
                            continue

                        order = upbit.buy_market_order(ticker, alloc * 0.9995)
                        details = fetch_order_details(order["uuid"])
                        if details and details.get("average_price"):
                            vol = float(details["executed_volume"])
                            avg = float(details["average_price"])
                        else:
                            vol = float(order["volume"])
                            avg = float(order["price"])

                        notify("BUY", ticker, vol, avg)
                        trade_log[asset]["buy"] = {"volume": vol, "price": avg}
                        remaining_krw -= vol * avg
                        bought[asset] = True

        # 매도 구간 (그 외 시간)
        else:
            for asset in ASSETS:
                if sold[asset]:
                    continue

                ticker = f"KRW-{asset}"
                bal = get_balance(asset)
                if bal > 0:
                    order = upbit.sell_market_order(ticker, bal * 0.9995)
                    details = fetch_order_details(order["uuid"])
                    if details and details.get("average_price"):
                        vol = float(details["executed_volume"])
                        avg = float(details["average_price"])
                    else:
                        vol = float(order["volume"])
                        avg = float(order["price"])

                    notify("SELL", ticker, vol, avg)
                    trade_log[asset]["sell"] = {"volume": vol, "price": avg}
                    sold[asset] = True

        time.sleep(1)

    except (requests.exceptions.RequestException, pyupbit.PyUpbitError) as e:
        # 일시적 네트워크/API 에러
        msg = f"⚠️ 일시적 오류: {e} – 10초 후 재시도"
        logger.warning(msg)
        send_discord(msg)
        time.sleep(10)

    except Exception as e:
        # 치명적 오류 발생 시 종료
        msg = f"❌ 치명적 오류 발생: {e} – 종료합니다"
        logger.error(msg, exc_info=True)
        send_discord(msg)
        break
