import time
import datetime
import logging
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import pyupbit
import requests
import schedule

# — 설정 —
access = "YOUR_UPBIT_ACCESS_KEY"
secret = "YOUR_UPBIT_SECRET_KEY"
DISCORD_WEBHOOK_URL = "YOUR_DISCORD_WEBHOOK_URL"

# 단일 자산: ETH 100% 비중
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
    # 최근 완성된 12개 1시간 봉을 사용하도록 count=13, 마지막 미완성봉 제외
    df = pyupbit.get_ohlcv(ticker, interval="minute60", count=13)
    prev_close = df['close'].iloc[-2]
    range_sum = (df['high'] - df['low'])[:-1].sum()
    return prev_close + range_sum * k

def get_start_time() -> datetime.datetime:
    # 일간 시점을 KST로 변환
    df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=1)
    dt = df.index[0].to_pydatetime()  # UTC naive
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Seoul"))

def get_balance(currency: str) -> float:
    for b in upbit.get_balances():
        if b["currency"] == currency:
            return float(b.get("balance", 0) or 0)
    return 0.0

def get_current_price(ticker: str) -> float:
    ob = pyupbit.get_orderbook(ticker=ticker)
    return ob["orderbook_units"][0]["ask_price"]

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

def send_daily_summary():
    global daily_start_balance, remaining_krw
    end_bal = remaining_krw
    profit = end_bal - daily_start_balance
    roi = (profit / daily_start_balance * 100) if daily_start_balance else 0.0
    lines = ["📊 오늘의 거래 요약"]
    for asset, logs in trade_log.items():
        buy, sell = logs["buy"], logs["sell"]
        if buy:
            lines.append(f"- {asset} 매수: {buy['volume']:.6f} @ {buy['price']:.0f}원 ({buy['time']})")
        else:
            lines.append(f"- {asset} 매수: 없음")
        if sell:
            lines.append(f"  매도: {sell['volume']:.6f} @ {sell['price']:.0f}원 ({sell['time']})")
        else:
            lines.append("  매도: 없음")
    lines += [
        f"💰 시작 잔고: {daily_start_balance:,.0f}원",
        f"💰 종료 잔고: {end_bal:,.0f}원",
        f"📈 수익: {profit:,.0f}원 ({roi:.2f}% ROI)"
    ]
    send_discord("\n".join(lines))

def send_daily_start_report():
    global daily_start_balance
    lines = ["🌅 오늘의 시작 보고", f"💰 잔고: {daily_start_balance:,.0f}원"]
    for asset in ASSETS:
        ticker = f"KRW-{asset}"
        target = get_target_price_12h(ticker, K)
        lines.append(f"- {asset} 목표가: {target:.0f}원")
    send_discord("\n".join(lines))

def reset_flags():
    global bought, sold, trade_log, remaining_krw, daily_start_balance
    send_daily_summary()
    bought = {a: False for a in ASSETS}
    sold   = {a: False for a in ASSETS}
    trade_log = {a: {"buy": None, "sell": None} for a in ASSETS}
    remaining_krw = get_balance("KRW")
    daily_start_balance = remaining_krw
    send_daily_start_report()
    send_discord("🔄 자정 리셋 완료")

# — 초기화 및 스케줄링 —
upbit = pyupbit.Upbit(access, secret)
remaining_krw = get_balance("KRW")
daily_start_balance = remaining_krw
bought = {a: False for a in ASSETS}
sold   = {a: False for a in ASSETS}
trade_log = {a: {"buy": None, "sell": None} for a in ASSETS}

send_discord("🔔 Autotrade 시작")
logger.info("Autotrade start")

schedule.every().day.at("00:00").do(reset_flags)

# — 메인 루프 —
while True:
    try:
        schedule.run_pending()
        now = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
        start_time = get_start_time()
        end_time = start_time + datetime.timedelta(days=1)
        buy_end = end_time - datetime.timedelta(minutes=20)

        # 매수 구간
        if start_time <= now < buy_end:
            if remaining_krw > MIN_KRW:
                for asset in ASSETS:
                    if bought[asset]:
                        continue
                    ticker = f"KRW-{asset}"
                    target = get_target_price_12h(ticker, K)
                    price = get_current_price(ticker)
                    if price > target:
                        alloc = remaining_krw
                        if alloc < MIN_KRW:
                            logger.warning(f"{asset} 할당금액 미달: {alloc:.0f}원")
                            bought[asset] = True
                            continue
                        order = upbit.buy_market_order(ticker, alloc * 0.9995)
                        if not order or "uuid" not in order:
                            raise RuntimeError(f"매수 주문 실패: {order}")
                        # 체결 정보 조회
                        for _ in range(3):
                            details = fetch_order_details(order["uuid"])
                            if details and details.get("executed_volume"):
                                vol = float(details["executed_volume"])
                                avg = float(details["average_price"])
                                break
                            time.sleep(1)
                        else:
                            raise RuntimeError(f"체결 정보 조회 실패: {order}")
                        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                        trade_log[asset]["buy"] = {"volume": vol, "price": avg, "time": now_str}
                        notify("BUY", ticker, vol, avg)
                        remaining_krw -= vol * avg
                        bought[asset] = True

        # 매도 구간
        else:
            for asset in ASSETS:
                if sold[asset]:
                    continue
                bal = get_balance(asset)
                if bal > 0:
                    ticker = f"KRW-{asset}"
                    order = upbit.sell_market_order(ticker, bal * 0.9995)
                    if not order or "uuid" not in order:
                        raise RuntimeError(f"매도 주문 실패: {order}")
                    for _ in range(3):
                        details = fetch_order_details(order["uuid"])
                        if details and details.get("executed_volume"):
                            vol = float(details["executed_volume"])
                            avg = float(details["average_price"])
                            break
                        time.sleep(1)
                    else:
                        raise RuntimeError(f"체결 정보 조회 실패: {order}")
                    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                    trade_log[asset]["sell"] = {"volume": vol, "price": avg, "time": now_str}
                    notify("SELL", ticker, vol, avg)
                    sold[asset] = True

        time.sleep(1)

    except requests.exceptions.RequestException as e:
        msg = f"⚠️ 네트워크/API 오류: {e} – 10초 후 재시도"
        logger.warning(msg)
        send_discord(msg)
        time.sleep(10)

    except Exception as e:
        logger.exception("❌ 치명적 오류 발생")
        send_discord(f"❌ 치명적 오류: {e} – 종료")
        break
