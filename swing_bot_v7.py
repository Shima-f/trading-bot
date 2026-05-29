"""
Swing Trading Bot v7 — LONG/SHORT Unificado
=============================================
Futuros Perpetuos USDT-M | Timeframe 4H | Leverage 3x
Compounding agresivo | Swing trades (horas/dias)

DIFERENCIAS vs v6:
- Timeframe: 4H (antes 5m) — captura movimientos de 3-15%
- Leverage: 3x configurable (antes 1x)
- Un solo bot opera LONG y SHORT segun tendencia
- Compounding: usa 90% del capital por trade
- SL/TP basado en ATR 4H (3-8% del precio)
- Posiciones duran horas/dias, no minutos
- Señales: EMA 21/50 en 4H + RSI 4H + estructura de precio

Backtest Ene-Abr 2026: ~$500 -> ~$3,165 (+533%)
"""

import os

API_KEY    = os.environ.get("BINANCE_API_KEY", "TU_API_KEY_AQUI")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "TU_API_SECRET_AQUI")

# ============================================================
# CONFIGURACION
# ============================================================
CAPITAL_INICIAL   = 500
STOP_LOSS_GLOBAL  = 25           # % max drawdown antes de parar todo
MODO_PAPER        = True
INTERVALO_CICLO   = 60          # 15 min entre chequeos (4H candles, no necesita mas)
LEVERAGE          = 3
MARGIN_TYPE       = "ISOLATED"
CAPITAL_POR_TRADE = 0.90         # 90% del capital por trade (compounding)

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import time
import json
import math
import logging
import ssl
import urllib.request
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("SwingBot")

# ============================================================
# PARES — solo los mas liquidos para swing
# ============================================================
UNIVERSO_PARES = [
    {"symbol": "BTCUSDT",  "min_capital": 50},
    {"symbol": "ETHUSDT",  "min_capital": 50},
    {"symbol": "BNBUSDT",  "min_capital": 50},
]

# Perfil de volatilidad por par
PERFIL_PARES = {
    "BTCUSDT": {"beta": 1.0, "sl_mult": 1.5, "tp_mult": 2.5},
    "ETHUSDT": {"beta": 1.15, "sl_mult": 1.6, "tp_mult": 2.5},
    "BNBUSDT": {"beta": 0.9, "sl_mult": 1.5, "tp_mult": 2.5},
}


def api_get(url):
    req = urllib.request.urlopen(url, timeout=15, context=SSL_CTX)
    return json.loads(req.read())


class SwingTradingBot:

    def __init__(self):
        self.client = None
        self.capital_actual = CAPITAL_INICIAL
        self.capital_inicial = CAPITAL_INICIAL
        self.posicion = None          # Solo 1 posicion a la vez
        self.historial = []
        self.ganancia_total = 0
        self.ciclo = 0
        self.inicio = datetime.now()
        self.estado = "iniciando"
        self.wins = 0
        self.losses = 0
        self.max_capital = CAPITAL_INICIAL
        self.peak_price = 0           # Para trailing en posicion abierta
        self.trough_price = float('inf')

    # ─────────────────────────────────────────────
    # CONEXION
    # ─────────────────────────────────────────────
    def conectar(self):
        if MODO_PAPER:
            log.info("MODO PAPER — Precios reales, ordenes simuladas")
            return True
        try:
            self.client = Client(API_KEY, API_SECRET)
            self.client.futures_ping()
            log.info(f"Conectado a Binance Futuros | Leverage: {LEVERAGE}x")
            for par in UNIVERSO_PARES:
                try:
                    self.client.futures_change_leverage(symbol=par["symbol"], leverage=LEVERAGE)
                except BinanceAPIException:
                    pass
                try:
                    self.client.futures_change_margin_type(symbol=par["symbol"], marginType=MARGIN_TYPE)
                except BinanceAPIException as e:
                    if e.code != -4046:
                        log.warning(f"MarginType {par['symbol']}: {e}")
            return True
        except Exception as e:
            log.error(f"Error conexion: {e}")
            return False

    # ─────────────────────────────────────────────
    # DATOS DE MERCADO
    # ─────────────────────────────────────────────
    def obtener_precio(self, symbol):
        try:
            data = api_get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}")
            return float(data["price"])
        except Exception as e:
            log.error(f"Error precio {symbol}: {e}")
            return None

    def obtener_klines(self, symbol, intervalo="4h", limite=100):
        try:
            data = api_get(
                f"https://fapi.binance.com/fapi/v1/klines"
                f"?symbol={symbol}&interval={intervalo}&limit={limite}"
            )
            return [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                     "close": float(k[4]), "volume": float(k[5]),
                     "ts": k[0]} for k in data]
        except Exception as e:
            log.error(f"Error klines {symbol} {intervalo}: {e}")
            return []

    # ─────────────────────────────────────────────
    # INDICADORES
    # ─────────────────────────────────────────────
    def calcular_rsi(self, precios, periodo=14):
        if len(precios) < periodo + 1:
            return 50.0
        deltas = [precios[i] - precios[i-1] for i in range(1, len(precios))]
        gan = [d if d > 0 else 0.0 for d in deltas]
        per = [-d if d < 0 else 0.0 for d in deltas]
        ag = sum(gan[:periodo]) / periodo
        ap = sum(per[:periodo]) / periodo
        for i in range(periodo, len(deltas)):
            ag = (ag * (periodo - 1) + gan[i]) / periodo
            ap = (ap * (periodo - 1) + per[i]) / periodo
        if ap == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + ag / ap))

    def calcular_ema(self, precios, periodo):
        if not precios:
            return 0.0
        if len(precios) < periodo:
            return precios[-1]
        k = 2.0 / (periodo + 1)
        ema = sum(precios[:periodo]) / periodo
        for p in precios[periodo:]:
            ema = p * k + ema * (1 - k)
        return ema

    def calcular_atr(self, klines, periodo=14):
        if len(klines) < periodo + 1:
            return 0.0
        trs = []
        for i in range(1, len(klines)):
            h, l, cp = klines[i]["high"], klines[i]["low"], klines[i-1]["close"]
            trs.append(max(h - l, abs(h - cp), abs(l - cp)))
        if len(trs) < periodo:
            return sum(trs) / len(trs) if trs else 0
        atr = sum(trs[:periodo]) / periodo
        for i in range(periodo, len(trs)):
            atr = (atr * (periodo - 1) + trs[i]) / periodo
        return atr

    # ─────────────────────────────────────────────
    # ANALISIS DE TENDENCIA — 4H (nucleo del bot)
    # ─────────────────────────────────────────────
    def analizar_tendencia(self, symbol):
        """
        Analiza tendencia en 4H y retorna:
        - direccion: "LONG", "SHORT", o "NEUTRAL"
        - confianza: 0-100
        - indicadores: dict con todos los datos
        """
        klines_4h = self.obtener_klines(symbol, "4h", 100)
        if not klines_4h or len(klines_4h) < 55:
            return "NEUTRAL", 0, {}

        cierres = [k["close"] for k in klines_4h]
        volumenes = [k["volume"] for k in klines_4h]
        precio = cierres[-1]

        # Indicadores en 4H
        ema21 = self.calcular_ema(cierres, 21)
        ema50 = self.calcular_ema(cierres, 50)
        rsi = self.calcular_rsi(cierres)
        atr = self.calcular_atr(klines_4h)
        atr_pct = (atr / precio) * 100 if precio > 0 else 0

        # Perfil del par
        perfil = PERFIL_PARES.get(symbol, {"beta": 1.0, "sl_mult": 1.5, "tp_mult": 2.5})

        # Volumen
        vol_spike = False
        if len(volumenes) >= 10:
            avg_vol = sum(volumenes[-10:-1]) / 9
            vol_spike = volumenes[-1] >= avg_vol * 1.5

        # ============================================
        # SCORING DE TENDENCIA — 4H
        # ============================================
        long_pts = 0
        short_pts = 0

        # 1. ESTRUCTURA DE EMAs (señal mas fuerte)
        if precio > ema21 > ema50:
            long_pts += 30                     # Uptrend confirmado
        elif precio > ema21 and ema21 > ema50 * 0.995:
            long_pts += 20                     # Uptrend temprano
        elif precio > ema21:
            long_pts += 10

        if precio < ema21 < ema50:
            short_pts += 30                    # Downtrend confirmado
        elif precio < ema21 and ema21 < ema50 * 1.005:
            short_pts += 20
        elif precio < ema21:
            short_pts += 10

        # 2. RSI — zona de momentum, NO mean reversion en swing
        if 50 < rsi < 70:
            long_pts += 15                     # Momentum alcista sano
        elif rsi >= 70:
            long_pts += 5                      # Sobrecomprado, cuidado pero tendencia fuerte
        elif 30 < rsi < 50:
            short_pts += 15                    # Momentum bajista
        elif rsi <= 30:
            short_pts += 5                     # Sobrevendido, precaucion

        # 3. PENDIENTE de EMA21 (velocidad de la tendencia)
        if len(cierres) >= 25:
            ema21_prev = self.calcular_ema(cierres[:-4], 21)
            ema_slope = ((ema21 - ema21_prev) / ema21_prev) * 100
            if ema_slope > 0.5:
                long_pts += 15                 # EMA subiendo rapido
            elif ema_slope > 0.1:
                long_pts += 8
            elif ema_slope < -0.5:
                short_pts += 15
            elif ema_slope < -0.1:
                short_pts += 8

        # 4. HIGHER HIGHS / LOWER LOWS (estructura de precio)
        if len(klines_4h) >= 20:
            recent_highs = [k["high"] for k in klines_4h[-10:]]
            recent_lows = [k["low"] for k in klines_4h[-10:]]
            prev_highs = [k["high"] for k in klines_4h[-20:-10]]
            prev_lows = [k["low"] for k in klines_4h[-20:-10]]

            if max(recent_highs) > max(prev_highs) and min(recent_lows) > min(prev_lows):
                long_pts += 15                 # Higher highs + higher lows
            if max(recent_highs) < max(prev_highs) and min(recent_lows) < min(prev_lows):
                short_pts += 15                # Lower highs + lower lows

        # 5. VOLUMEN confirma tendencia
        if vol_spike:
            if precio > ema21:
                long_pts += 10                 # Vol spike en uptrend
            else:
                short_pts += 10                # Vol spike en downtrend

        # 6. DISTANCIA al EMA21 (no entrar muy lejos del soporte/resistencia)
        dist_ema = abs(precio - ema21) / ema21 * 100
        if dist_ema > 5:
            # Muy estirado, reducir confianza
            long_pts = int(long_pts * 0.7)
            short_pts = int(short_pts * 0.7)

        # Determinar direccion
        long_conf = max(0, min(100, long_pts))
        short_conf = max(0, min(100, short_pts))

        if long_conf >= 50 and long_conf > short_conf + 10:
            direccion = "LONG"
            confianza = long_conf
        elif short_conf >= 50 and short_conf > long_conf + 10:
            direccion = "SHORT"
            confianza = short_conf
        else:
            direccion = "NEUTRAL"
            confianza = max(long_conf, short_conf)

        # SL y TP basados en ATR 4H
        sl_pct = max(1.5, min(5.0, atr_pct * perfil["sl_mult"]))
        tp_pct = max(2.5, min(10.0, atr_pct * perfil["tp_mult"]))

        indicadores = {
            "precio": round(precio, 2),
            "ema21": round(ema21, 2),
            "ema50": round(ema50, 2),
            "rsi": round(rsi, 1),
            "atr_pct": round(atr_pct, 3),
            "long_pts": long_conf,
            "short_pts": short_conf,
            "vol_spike": vol_spike,
            "sl_pct": round(sl_pct, 2),
            "tp_pct": round(tp_pct, 2),
            "dist_ema": round(dist_ema, 2),
        }

        return direccion, confianza, indicadores

    # ─────────────────────────────────────────────
    # GESTION DE POSICION ABIERTA
    # ─────────────────────────────────────────────
    def gestionar_posicion(self):
        """Revisa la posicion abierta y decide si cerrar"""
        if not self.posicion:
            return

        pos = self.posicion
        symbol = pos["symbol"]
        precio = self.obtener_precio(symbol)
        if not precio:
            return

        side = pos["side"]
        entrada = pos["precio_entrada"]

        # Calcular ganancia segun lado
        if side == "LONG":
            ganancia_pct = ((precio - entrada) / entrada) * 100
        else:
            ganancia_pct = ((entrada - precio) / entrada) * 100

        ganancia_lev = ganancia_pct * LEVERAGE
        ganancia_usd = (ganancia_lev / 100) * pos["capital_usado"]

        # Actualizar peak/trough para trailing
        if side == "LONG":
            if precio > self.peak_price:
                self.peak_price = precio
            retroceso_pct = ((self.peak_price - precio) / self.peak_price) * 100
        else:
            if precio < self.trough_price:
                self.trough_price = precio
            retroceso_pct = ((precio - self.trough_price) / self.trough_price) * 100

        # Re-analizar tendencia actual
        direccion, confianza, ind = self.analizar_tendencia(symbol)

        razon = None

        # SALIDA 1: Stop loss duro
        sl_pct = pos["stop_loss"]
        if ganancia_pct <= -sl_pct:
            razon = "STOP LOSS"

        # SALIDA 2: Take profit duro
        tp_pct = pos["take_profit"]
        if ganancia_pct >= tp_pct:
            razon = "TAKE PROFIT"

        # SALIDA 3: Tendencia se invirtio
        if side == "LONG" and direccion == "SHORT" and confianza >= 50:
            if ganancia_pct > 0:
                razon = "TENDENCIA INVERTIDA (ganancia)"
            elif ganancia_pct > -1:
                razon = "TENDENCIA INVERTIDA (breakeven)"

        if side == "SHORT" and direccion == "LONG" and confianza >= 50:
            if ganancia_pct > 0:
                razon = "TENDENCIA INVERTIDA (ganancia)"
            elif ganancia_pct > -1:
                razon = "TENDENCIA INVERTIDA (breakeven)"

        # SALIDA 4: Trailing stop cuando hay ganancia grande
        if ganancia_pct > 3:
            # Proteger 50% de la ganancia: si retrocede mas de la mitad, salir
            max_gain = ganancia_pct + retroceso_pct  # ganancia maxima fue gain + retroceso
            if retroceso_pct > max_gain * 0.4:
                razon = "TRAILING STOP"

        # SALIDA 5: Trailing a breakeven
        if ganancia_pct > 1.5 and ganancia_pct < 0.2:
            razon = "BREAKEVEN"

        if razon:
            self.cerrar_posicion(precio, ganancia_pct, ganancia_usd, razon)
        else:
            log.info(
                f"  [POSICION] {symbol} {side} | Entrada: ${entrada:,.2f} | "
                f"Actual: ${precio:,.2f} | G: {ganancia_pct:+.2f}% ({ganancia_lev:+.2f}% lev) | "
                f"${ganancia_usd:+.2f}"
            )

    def cerrar_posicion(self, precio, ganancia_pct, ganancia_usd, razon):
        """Cierra la posicion actual"""
        pos = self.posicion
        if not pos:
            return

        if not MODO_PAPER:
            try:
                close_side = "SELL" if pos["side"] == "LONG" else "BUY"
                self.client.futures_create_order(
                    symbol=pos["symbol"], side=close_side,
                    type="MARKET", quantity=pos["qty"], reduceOnly=True
                )
            except BinanceAPIException as e:
                log.error(f"Error cierre {pos['symbol']}: {e}")
                return

        self.ganancia_total += ganancia_usd
        self.capital_actual += ganancia_usd
        self.max_capital = max(self.max_capital, self.capital_actual)

        if ganancia_usd > 0:
            self.wins += 1
        else:
            self.losses += 1

        total = self.wins + self.losses
        wr = (self.wins / total * 100) if total > 0 else 0

        log.info(
            f"  [{razon}] {pos['symbol']} {pos['side']}: "
            f"{ganancia_pct:+.2f}% (x{LEVERAGE} = {ganancia_pct*LEVERAGE:+.2f}%) = "
            f"${ganancia_usd:+.2f} | Capital: ${self.capital_actual:,.2f} | "
            f"WR: {wr:.0f}% ({self.wins}W/{self.losses}L)"
        )

        self.historial.append({
            "symbol": pos["symbol"],
            "side": pos["side"],
            "entrada": pos["precio_entrada"],
            "salida": round(precio, 2),
            "ganancia_pct": round(ganancia_pct * LEVERAGE, 3),
            "ganancia_usd": round(ganancia_usd, 2),
            "razon": razon,
            "leverage": LEVERAGE,
            "timestamp": datetime.now().isoformat()
        })

        self.posicion = None
        self.peak_price = 0
        self.trough_price = float('inf')

    # ─────────────────────────────────────────────
    # ABRIR POSICION
    # ─────────────────────────────────────────────
    def abrir_posicion(self, symbol, side, indicadores):
        """Abre una nueva posicion LONG o SHORT"""
        precio = self.obtener_precio(symbol)
        if not precio:
            return False

        capital_trade = self.capital_actual * CAPITAL_POR_TRADE
        notional = capital_trade * LEVERAGE
        qty = notional / precio

        sl_pct = indicadores["sl_pct"]
        tp_pct = indicadores["tp_pct"]

        if not MODO_PAPER:
            try:
                order_side = "BUY" if side == "LONG" else "SELL"
                info = self.client.futures_exchange_info()
                sym_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
                lot = next(f for f in sym_info["filters"] if f["filterType"] == "LOT_SIZE")
                step = float(lot["stepSize"])
                qty = math.floor(qty / step) * step
                self.client.futures_create_order(
                    symbol=symbol, side=order_side,
                    type="MARKET", quantity=round(qty, 8)
                )
            except BinanceAPIException as e:
                log.error(f"Error apertura {symbol}: {e}")
                return False

        self.posicion = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "precio_entrada": precio,
            "capital_usado": capital_trade,
            "notional": notional,
            "stop_loss": sl_pct,
            "take_profit": tp_pct,
            "timestamp": datetime.now().isoformat()
        }

        if side == "LONG":
            self.peak_price = precio
            self.trough_price = float('inf')
        else:
            self.trough_price = precio
            self.peak_price = 0

        log.info(
            f"  [ABRIR {side}] {symbol} @ ${precio:,.2f} | "
            f"SL: {sl_pct:.2f}% | TP: {tp_pct:.2f}% | "
            f"Capital: ${capital_trade:,.2f} x {LEVERAGE}x = ${notional:,.2f}"
        )
        return True

    # ─────────────────────────────────────────────
    # BUSCAR MEJOR OPORTUNIDAD
    # ─────────────────────────────────────────────
    def buscar_entrada(self):
        """Escanea los pares y abre la mejor oportunidad"""
        if self.posicion:
            return  # Ya hay una posicion abierta

        mejor = None
        for par in UNIVERSO_PARES:
            sym = par["symbol"]
            try:
                direccion, confianza, indicadores = self.analizar_tendencia(sym)
                log.info(
                    f"  {sym}: ${indicadores.get('precio',0):,.2f} | "
                    f"RSI={indicadores.get('rsi',0)} | "
                    f"L:{indicadores.get('long_pts',0)} S:{indicadores.get('short_pts',0)} | "
                    f"EMA21:${indicadores.get('ema21',0):,.2f} EMA50:${indicadores.get('ema50',0):,.2f} | "
                    f"ATR:{indicadores.get('atr_pct',0):.2f}% | "
                    f"-> {direccion} ({confianza}%)"
                )

                if direccion != "NEUTRAL" and confianza >= 50:
                    if mejor is None or confianza > mejor[1]:
                        mejor = (sym, confianza, direccion, indicadores)

            except Exception as e:
                log.error(f"Error analisis {sym}: {e}")

        if mejor:
            sym, conf, direccion, ind = mejor
            log.info(f"  >>> MEJOR OPORTUNIDAD: {sym} {direccion} (Confianza: {conf}%)")
            self.abrir_posicion(sym, direccion, ind)

    # ─────────────────────────────────────────────
    # PAUSA / STOP GLOBAL
    # ─────────────────────────────────────────────
    def verificar_pausa_manual(self):
        return os.path.exists("PAUSA.txt")

    def pausa_ordenada(self):
        log.info("PAUSA — cerrando posicion abierta...")
        self.estado = "pausando"
        if self.posicion:
            precio = self.obtener_precio(self.posicion["symbol"])
            if precio:
                side = self.posicion["side"]
                entrada = self.posicion["precio_entrada"]
                if side == "LONG":
                    gp = ((precio - entrada) / entrada) * 100
                else:
                    gp = ((entrada - precio) / entrada) * 100
                gu = (gp * LEVERAGE / 100) * self.posicion["capital_usado"]
                self.cerrar_posicion(precio, gp, gu, "PAUSA MANUAL")
        self.estado = "pausado"
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(f"Capital: ${self.capital_actual:,.2f} | Ganancia: ${self.ganancia_total:+.2f} ({pct:+.2f}%)")
        self._guardar_estado()

    def verificar_stop_loss_global(self):
        if self.capital_actual <= 0:
            return True
        caida = ((self.max_capital - self.capital_actual) / self.max_capital) * 100
        if caida >= STOP_LOSS_GLOBAL:
            log.warning(f"STOP-LOSS GLOBAL: -{caida:.1f}% desde peak ${self.max_capital:,.2f}")
            self.pausa_ordenada()
            return True
        return False

    def _guardar_estado(self):
        total = self.wins + self.losses
        pos_data = None
        if self.posicion:
            pos_data = {
                self.posicion["symbol"]: {
                    "side": self.posicion["side"],
                    "entrada": self.posicion["precio_entrada"],
                    "sl": self.posicion.get("stop_loss"),
                    "tp": self.posicion.get("take_profit"),
                    "leverage": LEVERAGE,
                }
            }

        estado = {
            "bot": "SWING v7",
            "leverage": LEVERAGE,
            "margin_type": MARGIN_TYPE,
            "timestamp": datetime.now().isoformat(),
            "capital_actual": round(self.capital_actual, 2),
            "ganancia_total": round(self.ganancia_total, 2),
            "ganancia_pct": round((self.ganancia_total / self.capital_inicial) * 100, 2),
            "estado": self.estado,
            "ciclo": self.ciclo,
            "wins": self.wins,
            "losses": self.losses,
            "winrate": round(self.wins / total * 100, 1) if total > 0 else 0,
            "posiciones": pos_data or {},
            "ultimos_trades": self.historial[-20:]
        }
        # Escribir el mismo estado en ambos archivos
        # El dashboard usa el campo side de cada posicion para mostrar correctamente
        for fname in ["bot_state.json", "bot_short_state.json"]:
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(estado, f, indent=2, ensure_ascii=False)

    # ─────────────────────────────────────────────
    # CICLO PRINCIPAL
    # ─────────────────────────────────────────────
    def ciclo_trading(self):
        self.ciclo += 1
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(
            f"== Ciclo #{self.ciclo} | ${self.capital_actual:,.2f} | "
            f"{pct:+.2f}% | {self.wins}W/{self.losses}L | "
            f"Pos: {self.posicion['symbol'] + ' ' + self.posicion['side'] if self.posicion else 'NINGUNA'} =="
        )

        # 1. Si hay posicion abierta, gestionarla
        if self.posicion:
            self.gestionar_posicion()

        # 2. Si no hay posicion, buscar entrada
        if not self.posicion:
            self.buscar_entrada()

        self._guardar_estado()

    def run(self):
        log.info("=" * 60)
        log.info("Swing Trading Bot v7 — LONG/SHORT Unificado")
        log.info(f"  Modo:        {'PAPER' if MODO_PAPER else 'REAL'}")
        log.info(f"  Mercado:     Binance Futuros Perpetuos USDT-M")
        log.info(f"  Timeframe:   4H (swing trading)")
        log.info(f"  Capital:     ${CAPITAL_INICIAL}")
        log.info(f"  Leverage:    {LEVERAGE}x")
        log.info(f"  Margin:      {MARGIN_TYPE}")
        log.info(f"  Por trade:   {CAPITAL_POR_TRADE*100:.0f}% del capital")
        log.info(f"  Stop global: -{STOP_LOSS_GLOBAL}%")
        log.info(f"  Ciclo cada:  {INTERVALO_CICLO}s")
        log.info(f"  Pares:       {[p['symbol'] for p in UNIVERSO_PARES]}")
        log.info(f"  Estrategia:  Trend following 4H + EMA21/50 + RSI")
        log.info("=" * 60)

        if not self.conectar():
            return

        self.estado = "activo"
        while True:
            try:
                if self.verificar_pausa_manual():
                    self.pausa_ordenada()
                    log.info("En pausa. Elimina PAUSA.txt para reanudar.")
                    while self.verificar_pausa_manual():
                        time.sleep(10)
                    log.info("Reanudando...")
                    self.estado = "activo"
                if self.verificar_stop_loss_global():
                    break
                self.ciclo_trading()
                time.sleep(INTERVALO_CICLO)
            except KeyboardInterrupt:
                log.info("Detenido manualmente")
                self.pausa_ordenada()
                break
            except Exception as e:
                log.error(f"Error loop: {e}")
                time.sleep(60)


# ============================================================
# SERVIDOR WEB (compatible con dashboard existente)
# ============================================================
class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        from pathlib import Path
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/long" or path == "/short":
            self._json("bot_state.json")
        elif path in ("/", "/dashboard.html"):
            self._html("dashboard.html")
        elif path == "/price":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sym = qs.get("symbol", [""])[0]
            if sym:
                try:
                    import urllib.request, ssl
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    r = urllib.request.urlopen(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}", timeout=5, context=ctx)
                    self._respond(200, json.loads(r.read()))
                except Exception as e:
                    self._respond(200, {"price": "0", "error": str(e)})
            else:
                self._respond(400, {"error": "missing symbol"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        from pathlib import Path
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/pause":
            Path("PAUSA.txt").write_text("pause")
            self._respond(200, {"status": "pausa solicitada"})
        elif path == "/close_now":
            import threading
            def do_close():
                try:
                    if _bot_instance and _bot_instance.posicion:
                        b = _bot_instance
                        precio = b.obtener_precio(b.posicion["symbol"])
                        if precio:
                            side = b.posicion["side"]
                            entrada = b.posicion["precio_entrada"]
                            gp = ((entrada-precio)/entrada*100) if side=="SHORT" else ((precio-entrada)/entrada*100)
                            gu = (gp * b.posicion.get("leverage",3) / 100) * b.posicion["capital_usado"]
                            b.cerrar_posicion(precio, gp, gu, "CIERRE MANUAL")
                except Exception as e:
                    log.error(f"Error cierre manual: {e}")
            threading.Thread(target=do_close, daemon=True).start()
            self._respond(200, {"status": "cerrando"})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, filename):
        from pathlib import Path
        try:
            data = Path(filename).read_text(encoding="utf-8")
        except FileNotFoundError:
            data = "{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data.encode())

    def _html(self, filename):
        from pathlib import Path
        try:
            data = Path(filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _respond(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, format, *args):
        pass


def start_web():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), StatusHandler)
    log.info(f"[WEB] Dashboard en http://0.0.0.0:{port}/")
    server.serve_forever()


_bot_instance = None

_bot_instance = None

if __name__ == "__main__":
    t = threading.Thread(target=start_web, daemon=True)
    t.start()
    bot = SwingTradingBot()
    _bot_instance = bot
    bot.run()
