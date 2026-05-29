"""
Swing Trading Bot v8 — Institutional Grade
===========================================
Futuros Perpetuos USDT-M | Timeframe 4H | Leverage 3x

MEJORAS vs v7:
1. Pyramiding: entradas escalonadas en 3 tramos
2. TPs multiples: cerrar 40% en TP1, 35% en TP2, trailing en TP3
3. Correlacion beta: shortear/longear el par de mayor beta segun BTC
4. Drawdown adaptativo: reducir tamano cuando hay perdidas
5. Filtro de sesiones: operar en ventanas de mayor volatilidad
6. Perfiles calibrados por par con SL/TP especificos
"""

import os

API_KEY    = os.environ.get("BINANCE_API_KEY", "TU_API_KEY_AQUI")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "TU_API_SECRET_AQUI")

# ============================================================
# CONFIGURACION
# ============================================================
CAPITAL_INICIAL     = 500
STOP_LOSS_GLOBAL    = 25          # % max drawdown desde pico
MODO_PAPER          = True
INTERVALO_CICLO     = 60          # segundos entre chequeos
LEVERAGE            = 3
MARGIN_TYPE         = "ISOLATED"

# Pyramiding: porcentaje del capital por tramo
TRAMO_1_PCT         = 0.40        # 40% del capital disponible
TRAMO_2_PCT         = 0.30        # 30% adicional si trade va bien
TRAMO_3_PCT         = 0.30        # 30% restante en tendencia fuerte

# TPs escalonados
TP1_PCT_GANANCIA    = 1.5         # Cerrar 40% de la pos en TP1
TP2_PCT_GANANCIA    = 3.0         # Cerrar 35% de la pos en TP2
TP1_CIERRE_PCT      = 0.40        # % de la posicion a cerrar en TP1
TP2_CIERRE_PCT      = 0.35        # % en TP2

# Sesiones activas UTC (mayor volatilidad)
SESIONES_ACTIVAS = [
    (8, 12),   # Londres / NY open
    (20, 24),  # NY cierre / Asia open
]

# Drawdown adaptativo
DD_75 = 10    # Si cae 10% del pico -> operar al 75%
DD_50 = 20    # Si cae 20% del pico -> operar al 50%
DD_PAUSA = 25 # Si cae 25% -> pausa total

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import time
import json
import math
import logging
import ssl
import urllib.request
from datetime import datetime, timezone
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
log = logging.getLogger("SwingBotV8")

# ============================================================
# PERFILES POR PAR — calibrados por volatilidad real 4H
# beta: movimiento relativo a BTC (>1 = mas volatil)
# sl_min/max: limites de SL en %
# tp1/tp2/tp3: niveles de take profit en %
# ============================================================
PERFIL_PARES = {
    "BTCUSDT": {
        "beta": 1.0, "sl_mult": 1.8, "sl_min": 1.2, "sl_max": 2.5,
        "tp1": 1.5, "tp2": 3.0, "tp3_trail": 1.0,
        "min_capital": 50, "prioridad": 3
    },
    "ETHUSDT": {
        "beta": 1.15, "sl_mult": 2.0, "sl_min": 1.5, "sl_max": 3.0,
        "tp1": 2.0, "tp2": 4.0, "tp3_trail": 1.2,
        "min_capital": 50, "prioridad": 4
    },
    "SOLUSDT": {
        "beta": 1.5, "sl_mult": 2.2, "sl_min": 2.5, "sl_max": 5.0,
        "tp1": 3.0, "tp2": 6.0, "tp3_trail": 1.5,
        "min_capital": 100, "prioridad": 5  # Mayor beta = mas ganancia
    },
    "XRPUSDT": {
        "beta": 1.3, "sl_mult": 2.0, "sl_min": 1.5, "sl_max": 3.5,
        "tp1": 2.5, "tp2": 5.0, "tp3_trail": 1.3,
        "min_capital": 100, "prioridad": 4
    },
    "BNBUSDT": {
        "beta": 0.9, "sl_mult": 1.8, "sl_min": 1.2, "sl_max": 2.5,
        "tp1": 1.5, "tp2": 3.0, "tp3_trail": 1.0,
        "min_capital": 50, "prioridad": 3
    },
}

# Orden de prioridad: si BTC es bajista, shortear primero SOL, luego XRP, etc.
PARES_POR_BETA = sorted(PERFIL_PARES.keys(), key=lambda x: PERFIL_PARES[x]["beta"], reverse=True)


def api_get(url):
    req = urllib.request.urlopen(url, timeout=15, context=SSL_CTX)
    return json.loads(req.read())


class SwingBotV8:

    def __init__(self):
        self.client = None
        self.capital_actual = CAPITAL_INICIAL
        self.capital_inicial = CAPITAL_INICIAL
        self.capital_pico    = CAPITAL_INICIAL   # Para drawdown adaptativo

        # Posicion actual — soporta pyramiding (multiples tramos)
        self.posicion = None
        """
        posicion = {
            symbol, side,
            tramos: [
                {qty, precio_entrada, capital_usado, tp1_done, tp2_done}
            ],
            precio_entrada_avg,  # precio promedio ponderado
            sl_pct, tp1_pct, tp2_pct, tp3_trail_pct,
            max_ganancia_pct,    # para trailing
            timestamp
        }
        """

        self.historial = []
        self.ganancia_total = 0
        self.ciclo = 0
        self.inicio = datetime.now()
        self.estado = "iniciando"
        self.wins = 0
        self.losses = 0

    # ─────────────────────────────────────────────
    # UTILIDADES
    # ─────────────────────────────────────────────
    def en_sesion_activa(self):
        """Verifica si estamos en una ventana de alta volatilidad"""
        hora_utc = datetime.now(timezone.utc).hour
        for inicio, fin in SESIONES_ACTIVAS:
            if inicio <= hora_utc < fin:
                return True
        return False

    def factor_drawdown(self):
        """Retorna el multiplicador de capital segun el drawdown actual"""
        if self.capital_pico <= 0:
            return 1.0
        caida = ((self.capital_pico - self.capital_actual) / self.capital_pico) * 100
        if caida >= DD_50:
            return 0.5
        elif caida >= DD_75:
            return 0.75
        return 1.0

    def capital_disponible(self):
        """Capital efectivo considerando drawdown adaptativo"""
        return self.capital_actual * self.factor_drawdown()

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
            for sym in PERFIL_PARES:
                try:
                    self.client.futures_change_leverage(symbol=sym, leverage=LEVERAGE)
                except BinanceAPIException:
                    pass
                try:
                    self.client.futures_change_margin_type(symbol=sym, marginType=MARGIN_TYPE)
                except BinanceAPIException as e:
                    if e.code != -4046:
                        log.warning(f"MarginType {sym}: {e}")
            log.info(f"Conectado a Binance Futuros | {LEVERAGE}x {MARGIN_TYPE}")
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
                     "close": float(k[4]), "volume": float(k[5])} for k in data]
        except Exception as e:
            log.error(f"Error klines {symbol}: {e}")
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
        if not precios or len(precios) < periodo:
            return precios[-1] if precios else 0.0
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
    # TENDENCIA BTC (filtro macro)
    # ─────────────────────────────────────────────
    def tendencia_btc(self):
        klines = self.obtener_klines("BTCUSDT", "4h", 100)
        if not klines or len(klines) < 55:
            return "NEUTRAL", 0

        cierres = [k["close"] for k in klines]
        precio = cierres[-1]
        ema21 = self.calcular_ema(cierres, 21)
        ema50 = self.calcular_ema(cierres, 50)
        rsi   = self.calcular_rsi(cierres)

        puntos_long  = 0
        puntos_short = 0

        if precio > ema21 > ema50:
            puntos_long += 30
        elif precio < ema21 < ema50:
            puntos_short += 30

        if len(cierres) >= 25:
            ema21_prev = self.calcular_ema(cierres[:-4], 21)
            slope = ((ema21 - ema21_prev) / ema21_prev) * 100
            if slope > 0.3:
                puntos_long += 15
            elif slope < -0.3:
                puntos_short += 15

        if 50 < rsi < 70:
            puntos_long += 10
        elif 30 < rsi < 50:
            puntos_short += 10
        elif rsi >= 70:
            puntos_long += 5
        elif rsi <= 30:
            puntos_short += 5

        if puntos_long >= 40 and puntos_long > puntos_short + 10:
            return "ALCISTA", puntos_long
        elif puntos_short >= 40 and puntos_short > puntos_long + 10:
            return "BAJISTA", puntos_short
        return "NEUTRAL", max(puntos_long, puntos_short)

    # ─────────────────────────────────────────────
    # ANALIZAR PAR
    # ─────────────────────────────────────────────
    def analizar_par(self, symbol, direccion_macro):
        """Analiza un par y retorna confianza y parametros de entrada"""
        klines = self.obtener_klines(symbol, "4h", 100)
        if not klines or len(klines) < 55:
            return 0, {}

        cierres  = [k["close"] for k in klines]
        volumenes = [k["volume"] for k in klines]
        precio   = cierres[-1]
        perfil   = PERFIL_PARES[symbol]
        atr      = self.calcular_atr(klines)
        atr_pct  = (atr / precio) * 100 if precio > 0 else 0

        ema21 = self.calcular_ema(cierres, 21)
        ema50 = self.calcular_ema(cierres, 50)
        rsi   = self.calcular_rsi(cierres)

        vol_spike = False
        if len(volumenes) >= 10:
            avg_vol = sum(volumenes[-10:-1]) / 9
            vol_spike = volumenes[-1] >= avg_vol * 1.4

        puntos = 0

        if direccion_macro == "BAJISTA":
            # Scoring SHORT
            if precio < ema21 < ema50:
                puntos += 30
            elif precio < ema21:
                puntos += 15
            if 35 <= rsi <= 60:
                puntos += 15
            elif rsi > 60:
                puntos += 10
            elif rsi < 30:
                puntos -= 10

        elif direccion_macro == "ALCISTA":
            # Scoring LONG
            if precio > ema21 > ema50:
                puntos += 30
            elif precio > ema21:
                puntos += 15
            if 40 <= rsi <= 65:
                puntos += 15
            elif rsi < 40:
                puntos += 10
            elif rsi > 70:
                puntos -= 10

        # Pendiente EMA21
        if len(cierres) >= 25:
            ema21_prev = self.calcular_ema(cierres[:-4], 21)
            slope = ((ema21 - ema21_prev) / ema21_prev) * 100
            if direccion_macro == "BAJISTA" and slope < -0.2:
                puntos += 15
            elif direccion_macro == "ALCISTA" and slope > 0.2:
                puntos += 15

        # Higher highs / lower lows
        if len(klines) >= 20:
            recent_h = max(k["high"] for k in klines[-10:])
            recent_l = min(k["low"]  for k in klines[-10:])
            prev_h   = max(k["high"] for k in klines[-20:-10])
            prev_l   = min(k["low"]  for k in klines[-20:-10])
            if direccion_macro == "BAJISTA" and recent_h < prev_h and recent_l < prev_l:
                puntos += 15
            elif direccion_macro == "ALCISTA" and recent_h > prev_h and recent_l > prev_l:
                puntos += 15

        if vol_spike:
            puntos += 10

        confianza = max(0, min(100, puntos))

        # SL calibrado por par
        sl_pct = max(perfil["sl_min"], min(perfil["sl_max"], atr_pct * perfil["sl_mult"]))
        tp1    = perfil["tp1"]
        tp2    = perfil["tp2"]
        tp3_trail = perfil["tp3_trail"]

        return confianza, {
            "precio": precio, "rsi": round(rsi, 1),
            "ema21": round(ema21, 2), "ema50": round(ema50, 2),
            "atr_pct": round(atr_pct, 3), "confianza": confianza,
            "sl_pct": round(sl_pct, 2),
            "tp1": tp1, "tp2": tp2, "tp3_trail": tp3_trail,
        }

    # ─────────────────────────────────────────────
    # ABRIR POSICION (tramo 1)
    # ─────────────────────────────────────────────
    def abrir_posicion(self, symbol, side, indicadores):
        precio = self.obtener_precio(symbol)
        if not precio:
            return False

        cap_disp = self.capital_disponible()
        capital_tramo = cap_disp * TRAMO_1_PCT
        notional = capital_tramo * LEVERAGE
        qty = notional / precio

        if not MODO_PAPER:
            try:
                order_side = "BUY" if side == "LONG" else "SELL"
                self.client.futures_create_order(
                    symbol=symbol, side=order_side,
                    type="MARKET", quantity=round(qty, 6)
                )
            except BinanceAPIException as e:
                log.error(f"Error apertura {symbol}: {e}")
                return False

        self.posicion = {
            "symbol": symbol,
            "side": side,
            "tramos": [{
                "qty": qty,
                "precio_entrada": precio,
                "capital_usado": capital_tramo,
                "tp1_done": False,
                "tp2_done": False,
                "tramo_num": 1
            }],
            "precio_entrada_avg": precio,
            "qty_total": qty,
            "capital_total": capital_tramo,
            "sl_pct": indicadores["sl_pct"],
            "tp1_pct": indicadores["tp1"],
            "tp2_pct": indicadores["tp2"],
            "tp3_trail_pct": indicadores["tp3_trail"],
            "max_ganancia_pct": 0.0,
            "timestamp": datetime.now().isoformat(),
            "pyramiding_done": 1
        }

        log.info(
            f"  [ABRIR {side} T1] {symbol} @ ${precio:,.2f} | "
            f"SL:{indicadores['sl_pct']:.2f}% | "
            f"TP1:{indicadores['tp1']:.1f}% TP2:{indicadores['tp2']:.1f}% | "
            f"Capital:${capital_tramo:.2f}x{LEVERAGE}=${notional:.2f}"
        )
        return True

    # ─────────────────────────────────────────────
    # PYRAMIDING — agregar tramos
    # ─────────────────────────────────────────────
    def agregar_tramo(self, tramo_num):
        if not self.posicion:
            return
        pos = self.posicion
        if pos["pyramiding_done"] >= tramo_num:
            return

        precio = self.obtener_precio(pos["symbol"])
        if not precio:
            return

        pct = TRAMO_2_PCT if tramo_num == 2 else TRAMO_3_PCT
        cap_disp = self.capital_disponible()
        capital_tramo = cap_disp * pct
        notional = capital_tramo * LEVERAGE
        qty = notional / precio

        if not MODO_PAPER:
            try:
                order_side = "BUY" if pos["side"] == "LONG" else "SELL"
                self.client.futures_create_order(
                    symbol=pos["symbol"], side=order_side,
                    type="MARKET", quantity=round(qty, 6)
                )
            except BinanceAPIException as e:
                log.error(f"Error pyramiding T{tramo_num}: {e}")
                return

        pos["tramos"].append({
            "qty": qty, "precio_entrada": precio,
            "capital_usado": capital_tramo,
            "tp1_done": False, "tp2_done": False,
            "tramo_num": tramo_num
        })
        pos["qty_total"] += qty
        pos["capital_total"] += capital_tramo

        # Actualizar precio promedio ponderado
        total_cost = sum(t["qty"] * t["precio_entrada"] for t in pos["tramos"])
        pos["precio_entrada_avg"] = total_cost / pos["qty_total"]
        pos["pyramiding_done"] = tramo_num

        log.info(
            f"  [PYRAMID T{tramo_num}] {pos['symbol']} @ ${precio:,.2f} | "
            f"Precio avg: ${pos['precio_entrada_avg']:,.2f} | "
            f"Capital total: ${pos['capital_total']:.2f}"
        )

    # ─────────────────────────────────────────────
    # CERRAR PARCIAL (TP1, TP2)
    # ─────────────────────────────────────────────
    def cerrar_parcial(self, pct_pos, razon):
        if not self.posicion:
            return
        pos = self.posicion
        precio = self.obtener_precio(pos["symbol"])
        if not precio:
            return

        qty_cerrar = pos["qty_total"] * pct_pos
        side = pos["side"]
        entrada_avg = pos["precio_entrada_avg"]

        if side == "LONG":
            ganancia_pct = ((precio - entrada_avg) / entrada_avg) * 100
        else:
            ganancia_pct = ((entrada_avg - precio) / entrada_avg) * 100

        ganancia_lev = ganancia_pct * LEVERAGE
        ganancia_usd = (ganancia_lev / 100) * (pos["capital_total"] * pct_pos)

        if not MODO_PAPER:
            try:
                close_side = "SELL" if side == "LONG" else "BUY"
                self.client.futures_create_order(
                    symbol=pos["symbol"], side=close_side,
                    type="MARKET", quantity=round(qty_cerrar, 6),
                    reduceOnly=True
                )
            except BinanceAPIException as e:
                log.error(f"Error cierre parcial: {e}")
                return

        # Actualizar posicion
        pos["qty_total"] -= qty_cerrar
        pos["capital_total"] *= (1 - pct_pos)
        self.ganancia_total += ganancia_usd
        self.capital_actual += ganancia_usd
        self.capital_pico = max(self.capital_pico, self.capital_actual)

        log.info(
            f"  [{razon}] {pos['symbol']} {pct_pos*100:.0f}% cerrado | "
            f"{ganancia_pct:+.2f}% (x{LEVERAGE}={ganancia_lev:+.2f}%) = ${ganancia_usd:+.2f} | "
            f"Capital: ${self.capital_actual:.2f}"
        )

    # ─────────────────────────────────────────────
    # CERRAR TOTAL
    # ─────────────────────────────────────────────
    def cerrar_total(self, razon):
        if not self.posicion:
            return
        pos = self.posicion
        precio = self.obtener_precio(pos["symbol"])
        if not precio:
            return

        side = pos["side"]
        entrada_avg = pos["precio_entrada_avg"]

        if side == "LONG":
            ganancia_pct = ((precio - entrada_avg) / entrada_avg) * 100
        else:
            ganancia_pct = ((entrada_avg - precio) / entrada_avg) * 100

        ganancia_lev = ganancia_pct * LEVERAGE
        ganancia_usd = (ganancia_lev / 100) * pos["capital_total"]

        if not MODO_PAPER:
            try:
                close_side = "SELL" if side == "LONG" else "BUY"
                self.client.futures_create_order(
                    symbol=pos["symbol"], side=close_side,
                    type="MARKET", quantity=round(pos["qty_total"], 6),
                    reduceOnly=True
                )
            except BinanceAPIException as e:
                log.error(f"Error cierre total: {e}")
                return

        self.ganancia_total += ganancia_usd
        self.capital_actual += ganancia_usd
        self.capital_pico = max(self.capital_pico, self.capital_actual)

        if ganancia_usd > 0:
            self.wins += 1
        else:
            self.losses += 1

        total = self.wins + self.losses
        wr = (self.wins / total * 100) if total > 0 else 0

        log.info(
            f"  [{razon}] {pos['symbol']} {side}: "
            f"{ganancia_pct:+.2f}% (x{LEVERAGE}={ganancia_lev:+.2f}%) = ${ganancia_usd:+.2f} | "
            f"Capital: ${self.capital_actual:.2f} | WR:{wr:.0f}% ({self.wins}W/{self.losses}L)"
        )

        self.historial.append({
            "symbol": pos["symbol"], "side": side,
            "entrada": round(entrada_avg, 2), "salida": round(precio, 2),
            "ganancia_pct": round(ganancia_lev, 3),
            "ganancia_usd": round(ganancia_usd, 2),
            "razon": razon, "leverage": LEVERAGE,
            "tramos": pos["pyramiding_done"],
            "timestamp": datetime.now().isoformat()
        })
        self.posicion = None

    # ─────────────────────────────────────────────
    # GESTIONAR POSICION ABIERTA
    # ─────────────────────────────────────────────
    def gestionar_posicion(self):
        if not self.posicion:
            return
        pos = self.posicion
        precio = self.obtener_precio(pos["symbol"])
        if not precio:
            return

        side = pos["side"]
        entrada = pos["precio_entrada_avg"]

        if side == "LONG":
            ganancia_pct = ((precio - entrada) / entrada) * 100
        else:
            ganancia_pct = ((entrada - precio) / entrada) * 100

        ganancia_lev = ganancia_pct * LEVERAGE
        pos["max_ganancia_pct"] = max(pos["max_ganancia_pct"], ganancia_pct)

        log.info(
            f"  [POS] {pos['symbol']} {side} T{pos['pyramiding_done']} | "
            f"Avg:${entrada:,.2f} Actual:${precio:,.2f} | "
            f"G:{ganancia_pct:+.2f}% ({ganancia_lev:+.2f}%lev) | "
            f"Max:{pos['max_ganancia_pct']:+.2f}%"
        )

        # STOP LOSS — cierre total
        if ganancia_pct <= -pos["sl_pct"]:
            self.cerrar_total("STOP LOSS")
            return

        # TAKE PROFIT 1 — cerrar 40% de la posicion
        if ganancia_pct >= pos["tp1_pct"] and not pos.get("tp1_done"):
            self.cerrar_parcial(TP1_CIERRE_PCT, f"TP1 +{pos['tp1_pct']:.1f}%")
            pos["tp1_done"] = True

        # TAKE PROFIT 2 — cerrar 35% de lo restante
        if ganancia_pct >= pos["tp2_pct"] and not pos.get("tp2_done"):
            self.cerrar_parcial(TP2_CIERRE_PCT / (1 - TP1_CIERRE_PCT), f"TP2 +{pos['tp2_pct']:.1f}%")
            pos["tp2_done"] = True

        # TRAILING STOP para el resto (TP3)
        if pos.get("tp2_done") and pos["max_ganancia_pct"] > pos["tp2_pct"]:
            retroceso = pos["max_ganancia_pct"] - ganancia_pct
            if retroceso > pos["tp3_trail_pct"]:
                self.cerrar_total(f"TP3 TRAILING (max:{pos['max_ganancia_pct']:.1f}%)")
                return

        # PYRAMIDING — agregar tramos si el trade va bien
        if ganancia_pct >= pos["tp1_pct"] * 0.5 and pos["pyramiding_done"] < 2:
            log.info(f"  [PYRAMID] Ganancia {ganancia_pct:.2f}% -> agregando tramo 2")
            self.agregar_tramo(2)

        if ganancia_pct >= pos["tp1_pct"] * 1.2 and pos["pyramiding_done"] < 3:
            log.info(f"  [PYRAMID] Ganancia {ganancia_pct:.2f}% -> agregando tramo 3")
            self.agregar_tramo(3)

        # Trailing breakeven: si ganó algo y retrocede a 0
        if pos["max_ganancia_pct"] > 1.0 and ganancia_pct < 0.1:
            self.cerrar_total("BREAKEVEN")

        # Tendencia se invirtio
        tendencia, _ = self.tendencia_btc()
        if side == "LONG" and tendencia == "BAJISTA" and ganancia_pct > 0:
            self.cerrar_total("TENDENCIA INVERTIDA")
        elif side == "SHORT" and tendencia == "ALCISTA" and ganancia_pct > 0:
            self.cerrar_total("TENDENCIA INVERTIDA")

    # ─────────────────────────────────────────────
    # BUSCAR ENTRADA
    # ─────────────────────────────────────────────
    def buscar_entrada(self):
        if self.posicion:
            return

        if not self.en_sesion_activa():
            log.info(
                f"  [SESION] Fuera de ventana activa "
                f"(hora UTC: {datetime.now(timezone.utc).hour}) — no abrir nuevas"
            )
            return

        tendencia_macro, fuerza = self.tendencia_btc()
        if tendencia_macro == "NEUTRAL":
            log.info(f"  [MACRO] BTC NEUTRAL ({fuerza}pts) — esperando")
            return

        log.info(f"  [MACRO] BTC {tendencia_macro} ({fuerza}pts) — escaneando pares")

        # Analizar pares ordenados por beta (mayor potencial primero)
        mejor = None
        for symbol in PARES_POR_BETA:
            if symbol not in PERFIL_PARES:
                continue
            perfil = PERFIL_PARES[symbol]
            if self.capital_disponible() < perfil["min_capital"]:
                continue
            try:
                confianza, indicadores = self.analizar_par(symbol, tendencia_macro)
                side = "SHORT" if tendencia_macro == "BAJISTA" else "LONG"
                log.info(
                    f"  {symbol}: ${indicadores.get('precio',0):,.2f} | "
                    f"RSI={indicadores.get('rsi',0)} | "
                    f"Conf={confianza}% | beta={perfil['beta']} | "
                    f"SL:{indicadores.get('sl_pct',0):.2f}% | -> {side}"
                )
                if confianza >= 55:
                    # Priorizar por: confianza * beta (más ganancia potencial)
                    score = confianza * perfil["beta"]
                    if mejor is None or score > mejor[0]:
                        mejor = (score, symbol, side, indicadores)
            except Exception as e:
                log.error(f"Error analisis {symbol}: {e}")

        if mejor:
            _, symbol, side, indicadores = mejor
            dd = self.factor_drawdown()
            log.info(
                f"  >>> ENTRADA: {symbol} {side} | "
                f"Conf:{indicadores['confianza']}% | "
                f"Beta:{PERFIL_PARES[symbol]['beta']} | "
                f"DD factor:{dd:.2f}"
            )
            self.abrir_posicion(symbol, side, indicadores)

    # ─────────────────────────────────────────────
    # CONTROL
    # ─────────────────────────────────────────────
    def verificar_pausa_manual(self):
        return os.path.exists("PAUSA.txt")

    def pausa_ordenada(self):
        log.info("PAUSA — cerrando posicion...")
        self.estado = "pausando"
        if self.posicion:
            self.cerrar_total("PAUSA MANUAL")
        self.estado = "pausado"
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(f"Capital: ${self.capital_actual:.2f} | G: ${self.ganancia_total:+.2f} ({pct:+.2f}%)")
        self._guardar_estado()

    def verificar_stop_loss_global(self):
        if self.capital_pico <= 0:
            return False
        caida = ((self.capital_pico - self.capital_actual) / self.capital_pico) * 100
        if caida >= DD_PAUSA:
            log.warning(f"STOP GLOBAL: -{caida:.1f}% desde pico ${self.capital_pico:.2f}")
            self.pausa_ordenada()
            return True
        return False

    def _guardar_estado(self):
        total = self.wins + self.losses
        pos_data = {}
        if self.posicion:
            pos = self.posicion
            pos_data[pos["symbol"]] = {
                "side": pos["side"],
                "entrada": round(pos["precio_entrada_avg"], 2),
                "sl": pos["sl_pct"],
                "tp": pos["tp2_pct"],
                "leverage": LEVERAGE,
                "tramos": pos["pyramiding_done"],
                "capital_usado": round(pos["capital_total"], 2),
                "max_ganancia": round(pos["max_ganancia_pct"], 2),
            }

        estado = {
            "bot": "SWING v8",
            "leverage": LEVERAGE,
            "timestamp": datetime.now().isoformat(),
            "capital_actual": round(self.capital_actual, 2),
            "capital_pico": round(self.capital_pico, 2),
            "ganancia_total": round(self.ganancia_total, 2),
            "ganancia_pct": round((self.ganancia_total / self.capital_inicial) * 100, 2),
            "drawdown_factor": round(self.factor_drawdown(), 2),
            "estado": self.estado,
            "ciclo": self.ciclo,
            "wins": self.wins,
            "losses": self.losses,
            "winrate": round(self.wins / total * 100, 1) if total > 0 else 0,
            "posiciones": pos_data,
            "ultimos_trades": self.historial[-20:]
        }
        for fname in ["bot_state.json", "bot_short_state.json"]:
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(estado, f, indent=2, ensure_ascii=False)

    # ─────────────────────────────────────────────
    # CICLO PRINCIPAL
    # ─────────────────────────────────────────────
    def ciclo_trading(self):
        self.ciclo += 1
        pct   = (self.ganancia_total / self.capital_inicial) * 100
        dd    = ((self.capital_pico - self.capital_actual) / self.capital_pico * 100) if self.capital_pico > 0 else 0
        sesion = "ON" if self.en_sesion_activa() else "OFF"
        pos_info = (f"{self.posicion['symbol']} {self.posicion['side']} "
                    f"T{self.posicion['pyramiding_done']}") if self.posicion else "NINGUNA"

        log.info(
            f"== Ciclo #{self.ciclo} | ${self.capital_actual:.2f} | "
            f"{pct:+.2f}% | DD:{dd:.1f}% | "
            f"{self.wins}W/{self.losses}L | "
            f"Sesion:{sesion} | Pos:{pos_info} =="
        )

        if self.posicion:
            self.gestionar_posicion()

        if not self.posicion:
            self.buscar_entrada()

        self._guardar_estado()

    def run(self):
        log.info("=" * 65)
        log.info("Swing Trading Bot v8 — Institutional Grade")
        log.info(f"  Modo:        {'PAPER' if MODO_PAPER else 'REAL'}")
        log.info(f"  Capital:     ${CAPITAL_INICIAL}")
        log.info(f"  Leverage:    {LEVERAGE}x {MARGIN_TYPE}")
        log.info(f"  Pyramiding:  40% / 30% / 30%")
        log.info(f"  TPs:         TP1={TP1_PCT_GANANCIA}% TP2={TP2_PCT_GANANCIA}% + Trailing")
        log.info(f"  Sesiones:    {SESIONES_ACTIVAS} UTC")
        log.info(f"  DD adaptivo: 75%@{DD_75}% 50%@{DD_50}% Pausa@{DD_PAUSA}%")
        log.info(f"  Beta order:  {PARES_POR_BETA}")
        log.info("=" * 65)

        if not self.conectar():
            return

        self.estado = "activo"
        while True:
            try:
                if self.verificar_pausa_manual():
                    self.pausa_ordenada()
                    log.info("Pausa. Elimina PAUSA.txt para reanudar.")
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
                time.sleep(30)


# ============================================================
# SERVIDOR WEB
# ============================================================
class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        from pathlib import Path
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path in ("/long", "/short"):
            self._json("bot_state.json")
        elif path == "/price":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sym = qs.get("symbol", [""])[0]
            if sym:
                try:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    r = urllib.request.urlopen(
                        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}",
                        timeout=5, context=ctx
                    )
                    self._respond(200, json.loads(r.read()))
                except Exception as e:
                    self._respond(200, {"price": "0"})
            else:
                self._respond(400, {"error": "missing symbol"})
        elif path in ("/", "/dashboard.html"):
            self._html("dashboard.html")
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        from pathlib import Path
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/pause":
            Path("PAUSA.txt").write_text("pause")
            self._respond(200, {"status": "pausa solicitada"})
        elif path == "/close_now":
            import threading
            def do_close():
                try:
                    if _bot_instance and _bot_instance.posicion:
                        _bot_instance.cerrar_total("CIERRE MANUAL")
                except Exception as e:
                    log.error(f"Error cierre manual: {e}")
            threading.Thread(target=do_close, daemon=True).start()
            self._respond(200, {"status": "cerrando"})
        else:
            self.send_response(404); self.end_headers()

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
            self.send_response(404); self.end_headers()

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

if __name__ == "__main__":
    t = threading.Thread(target=start_web, daemon=True)
    t.start()
    bot = SwingTradingBot() if False else SwingBotV8()
    _bot_instance = bot
    bot.run()
