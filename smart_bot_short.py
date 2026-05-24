"""
Smart Trading Bot v4 SHORT - Espejo Bajista
Solo opera en bajas confirmadas con trailing stop INVERTIDO
Mercado: Binance Futuros Perpetuos USDT-M
"""

import os
API_KEY    = os.environ.get("BINANCE_API_KEY", "TU_API_KEY_AQUI")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "TU_API_SECRET_AQUI")

# ============================================================
# CONFIGURACION
# ============================================================
CAPITAL_INICIAL   = 500
STOP_LOSS_GLOBAL  = 10           # % de drawdown global que detiene el bot
MODO_PAPER        = True
INTERVALO_CICLO   = 300
LEVERAGE          = 1            # 1x = sin apalancamiento (cambiar a 2, 3, 5...)
MARGIN_TYPE       = "ISOLATED"   # ISOLATED o CROSSED

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
        logging.FileHandler("bot_short.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("SmartBotShort")

# ============================================================
# UNIVERSO - mismos pares, pero los analizamos en futuros
# ============================================================
UNIVERSO_PARES = [
    {"symbol": "BTCUSDT",  "estrategia": "momentum",        "min_capital": 50},
    {"symbol": "ETHUSDT",  "estrategia": "momentum",        "min_capital": 50},
    {"symbol": "BNBUSDT",  "estrategia": "momentum",        "min_capital": 50},
    {"symbol": "SOLUSDT",  "estrategia": "momentum_volume", "min_capital": 100},
    {"symbol": "XRPUSDT",  "estrategia": "momentum_volume", "min_capital": 100},
]

REGLAS_CAPITAL = [
    {"min": 0,    "max": 299,    "max_pares": 2},
    {"min": 300,  "max": 699,    "max_pares": 3},
    {"min": 700,  "max": 1499,   "max_pares": 4},
    {"min": 1500, "max": 999999, "max_pares": 5},
]

def api_get(url):
    req = urllib.request.urlopen(url, timeout=15, context=SSL_CTX)
    return json.loads(req.read())


class SmartTradingBotShort:

    def __init__(self):
        self.client = None
        self.capital_actual = CAPITAL_INICIAL
        self.capital_inicial = CAPITAL_INICIAL
        self.pares_activos = []
        self.posiciones = {}          # symbol -> {qty, precio_entrada, ...}
        self.historial = []
        self.ganancia_total = 0
        self.ciclo = 0
        self.inicio = datetime.now()
        self.estado = "iniciando"
        self.capital_por_par = CAPITAL_INICIAL / 2
        self.wins = 0
        self.losses = 0
        self.min_precios = {}         # trailing INVERTIDO: trackea el minimo

    # ------------------------------------------------------------
    # CONEXION
    # ------------------------------------------------------------
    def conectar(self):
        if MODO_PAPER:
            log.info("MODO PAPER - Precios reales de futuros, ordenes simuladas")
            return True
        try:
            self.client = Client(API_KEY, API_SECRET)
            self.client.futures_ping()
            log.info(f"Conectado a Binance Futuros | Leverage: {LEVERAGE}x | Margen: {MARGIN_TYPE}")
            # Setear leverage y tipo de margen por par
            for par in UNIVERSO_PARES:
                try:
                    self.client.futures_change_leverage(
                        symbol=par["symbol"], leverage=LEVERAGE
                    )
                except BinanceAPIException as e:
                    log.warning(f"Leverage {par['symbol']}: {e}")
                try:
                    self.client.futures_change_margin_type(
                        symbol=par["symbol"], marginType=MARGIN_TYPE
                    )
                except BinanceAPIException as e:
                    # -4046 = ya esta en ese modo, no es error real
                    if e.code != -4046:
                        log.warning(f"MarginType {par['symbol']}: {e}")
            return True
        except Exception as e:
            log.error(f"Error conexion: {e}")
            return False

    # ------------------------------------------------------------
    # SELECCION DE PARES
    # ------------------------------------------------------------
    def seleccionar_pares(self):
        max_pares = 2
        for regla in REGLAS_CAPITAL:
            if regla["min"] <= self.capital_actual <= regla["max"]:
                max_pares = regla["max_pares"]
                break
        pares = []
        for par in UNIVERSO_PARES:
            if len(pares) >= max_pares:
                break
            if (self.capital_actual / max_pares) >= par["min_capital"]:
                pares.append(par)
        self.pares_activos = pares
        self.capital_por_par = self.capital_actual / len(pares)
        log.info(
            f"Pares ({len(pares)}): {[p['symbol'] for p in pares]} | "
            f"${self.capital_por_par:.2f} c/u | Leverage: {LEVERAGE}x"
        )

    # ------------------------------------------------------------
    # DATOS DE MERCADO (endpoint de FUTUROS)
    # ------------------------------------------------------------
    def obtener_precio(self, symbol):
        try:
            data = api_get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}")
            return float(data["price"])
        except Exception as e:
            log.error(f"Error precio {symbol}: {e}")
            return None

    def obtener_klines(self, symbol, intervalo="5m", limite=100):
        try:
            data = api_get(
                f"https://fapi.binance.com/fapi/v1/klines"
                f"?symbol={symbol}&interval={intervalo}&limit={limite}"
            )
            return [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                     "close": float(k[4]), "volume": float(k[5])} for k in data]
        except Exception as e:
            log.error(f"Error klines {symbol} {intervalo}: {e}")
            return []

    # ------------------------------------------------------------
    # INDICADORES (identicos al bot long)
    # ------------------------------------------------------------
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
            h = klines[i]["high"]
            l = klines[i]["low"]
            cp = klines[i-1]["close"]
            tr = max(h - l, abs(h - cp), abs(l - cp))
            trs.append(tr)
        if len(trs) < periodo:
            return sum(trs) / len(trs) if trs else 0
        atr = sum(trs[:periodo]) / periodo
        for i in range(periodo, len(trs)):
            atr = (atr * (periodo - 1) + trs[i]) / periodo
        return atr

    # ------------------------------------------------------------
    # TENDENCIA 1H - misma logica, identifica las 3 fases
    # ------------------------------------------------------------
    def tendencia_1h(self, symbol):
        klines = self.obtener_klines(symbol, "1h", 50)
        if len(klines) < 50:
            return "desconocida", 0
        cierres = [k["close"] for k in klines]
        ema21 = self.calcular_ema(cierres, 21)
        ema50 = self.calcular_ema(cierres, 50)
        precio = cierres[-1]
        rsi = self.calcular_rsi(cierres)
        if precio > ema21 > ema50 and rsi > 45:
            return "alcista", rsi
        elif precio < ema21 < ema50 and rsi < 55:
            return "bajista", rsi
        return "lateral", rsi

    # ------------------------------------------------------------
    # ANALISIS - LOGICA INVERTIDA para SHORT
    # ------------------------------------------------------------
    def analizar_par(self, par):
        klines_5m = self.obtener_klines(par["symbol"], "5m", 100)
        if not klines_5m or len(klines_5m) < 50:
            return "ESPERAR", 0, {}

        cierres = [k["close"] for k in klines_5m]
        volumenes = [k["volume"] for k in klines_5m]
        precio = cierres[-1]
        atr = self.calcular_atr(klines_5m)
        atr_pct = (atr / precio) * 100 if precio > 0 else 0

        rsi_5m = self.calcular_rsi(cierres)
        ema9 = self.calcular_ema(cierres, 9)
        ema21 = self.calcular_ema(cierres, 21)
        ema50 = self.calcular_ema(cierres, 50)

        tendencia_h, rsi_1h = self.tendencia_1h(par["symbol"])

        vol_spike = False
        if len(volumenes) >= 20:
            avg_vol = sum(volumenes[-20:-1]) / 19
            vol_spike = volumenes[-1] >= avg_vol * 1.5

        # ============================================
        # SCORING INVERTIDO - puntua tendencia BAJISTA
        # ============================================
        puntos = 0

        # Tendencia 1H: ahora premiamos BAJISTA
        if tendencia_h == "bajista":
            puntos += 30
        elif tendencia_h == "lateral":
            puntos += 5
        elif tendencia_h == "alcista":
            puntos -= 30

        # EMAs apiladas hacia ABAJO (precio < ema9 < ema21 < ema50)
        if precio < ema9 < ema21 < ema50:
            puntos += 25
        elif precio < ema9 < ema21:
            puntos += 12

        # RSI: zona neutral-baja es ideal para shortear sin sobreventa
        # Evitar RSI < 30 (sobrevendido, riesgo de rebote)
        if 40 <= rsi_5m <= 60:
            puntos += 15
        elif 60 < rsi_5m <= 65:
            puntos += 10  # bien para fade de rally en downtrend

        # Penalizar sobreventa extrema (rebote tecnico probable)
        if rsi_5m < 30:
            puntos -= 20

        # Volume spike vale igual en ambas direcciones
        if vol_spike:
            puntos += 15

        # 5 velas ROJAS consecutivas (precio cayendo de forma sostenida)
        ultimos_5 = cierres[-5:]
        if all(ultimos_5[i] >= ultimos_5[i+1] for i in range(len(ultimos_5)-1)):
            puntos += 15

        confianza = max(0, min(100, puntos))

        stop_loss_dinamico = max(0.15, min(0.5, atr_pct * 1.2))
        take_profit_dinamico = max(0.15, stop_loss_dinamico * 1.5)

        indicadores = {
            "rsi_5m": round(rsi_5m, 1),
            "rsi_1h": round(rsi_1h, 1),
            "precio": round(precio, 2),
            "confianza": confianza,
            "tendencia_1h": tendencia_h,
            "vol_spike": vol_spike,
            "atr_pct": round(atr_pct, 3),
            "sl": round(stop_loss_dinamico, 2),
            "tp": round(take_profit_dinamico, 2)
        }

        # ============================================
        # GESTION DE POSICION ABIERTA (SHORT)
        # En short ganamos cuando el precio BAJA
        # ============================================
        if par["symbol"] in self.posiciones:
            pos = self.posiciones[par["symbol"]]
            # gp invertido: entrada - precio_actual
            gp = ((pos["precio_entrada"] - precio) / pos["precio_entrada"]) * 100

            # Trailing INVERTIDO: trackea el MINIMO precio alcanzado
            if par["symbol"] not in self.min_precios:
                self.min_precios[par["symbol"]] = precio
            if precio < self.min_precios[par["symbol"]]:
                self.min_precios[par["symbol"]] = precio

            min_p = self.min_precios[par["symbol"]]

            # Ganancia en USD (apalancada): (entrada - actual) * qty
            ganancia_usd = (pos["precio_entrada"] - precio) * pos["qty"]
            # Subida desde minimo = retroceso contra nosotros
            subida_usd = (precio - min_p) * pos["qty"]

            # STOP LOSS DURO: perdida > $0.80
            if ganancia_usd <= -0.80:
                return "CERRAR", confianza, indicadores

            # Ganancia chica: solo cerrar si BTC se da vuelta a alcista
            if ganancia_usd < 0.50:
                if tendencia_h == "alcista" and ganancia_usd > 0.10:
                    return "CERRAR", confianza, indicadores
                return "ESPERAR", confianza, indicadores

            # Trailing zona 1: ganancia 0.50-1.00
            if 0.50 <= ganancia_usd < 1.00:
                if subida_usd > 0.30 or tendencia_h == "alcista":
                    return "CERRAR", confianza, indicadores
                return "ESPERAR", confianza, indicadores

            # Trailing zona 2: ganancia 1.00-2.00
            if 1.00 <= ganancia_usd < 2.00:
                if subida_usd > 0.40 or tendencia_h == "alcista":
                    return "CERRAR", confianza, indicadores
                return "ESPERAR", confianza, indicadores

            # Trailing zona 3: ganancia >= 2.00
            if ganancia_usd >= 2.00:
                if subida_usd > 0.50:
                    return "CERRAR", confianza, indicadores
                if tendencia_h == "alcista":
                    return "CERRAR", confianza, indicadores
                return "ESPERAR", confianza, indicadores

        # ============================================
        # FILTRO BTC INVERTIDO: no shortear alts si BTC no es bajista
        # ============================================
        if par["symbol"] != "BTCUSDT":
            btc_tend, _ = self.tendencia_1h("BTCUSDT")
            if btc_tend != "bajista":
                return "ESPERAR", confianza, indicadores

        # No abrir short en lateral salvo confianza muy alta
        if tendencia_h == "lateral" and confianza < 80:
            return "ESPERAR", confianza, indicadores

        # SEÑAL DE APERTURA SHORT
        if confianza >= 75:
            indicadores["sl_usado"] = stop_loss_dinamico
            indicadores["tp_usado"] = take_profit_dinamico
            return "ABRIR_SHORT", confianza, indicadores

        return "ESPERAR", confianza, indicadores

    # ------------------------------------------------------------
    # EJECUCION: ABRIR SHORT (SELL en futuros = abrir corto)
    # ------------------------------------------------------------
    def ejecutar_abrir_short(self, par, capital, indicadores):
        precio = self.obtener_precio(par["symbol"])
        if not precio:
            return False
        sl = indicadores.get("sl_usado", 0.5)
        tp = indicadores.get("tp_usado", 1.5)

        # Con apalancamiento: notional = capital * leverage
        # qty se calcula sobre el notional
        notional = capital * LEVERAGE

        if MODO_PAPER:
            qty = (notional * 0.999) / precio
            self.posiciones[par["symbol"]] = {
                "qty": qty,
                "precio_entrada": precio,
                "capital_usado": capital,
                "notional": notional,
                "leverage": LEVERAGE,
                "stop_loss": sl,
                "take_profit": tp,
                "timestamp": datetime.now().isoformat()
            }
            self.min_precios[par["symbol"]] = precio
            log.info(
                f"[SHORT ABIERTO] {par['symbol']} @ ${precio:.2f} | "
                f"SL:{sl:.2f}% TP:{tp:.2f}% | "
                f"Margen ${capital:.2f} x {LEVERAGE}x = ${notional:.2f}"
            )
            return True
        try:
            info = self.client.futures_exchange_info()
            sym_info = next(s for s in info["symbols"] if s["symbol"] == par["symbol"])
            lot = next(f for f in sym_info["filters"] if f["filterType"] == "LOT_SIZE")
            step = float(lot["stepSize"])
            qty = math.floor((notional * 0.999) / precio / step) * step
            # SIDE=SELL en futuros con posicion previa cero -> abre SHORT
            orden = self.client.futures_create_order(
                symbol=par["symbol"],
                side="SELL",
                type="MARKET",
                quantity=round(qty, 8)
            )
            # Precio de fill: pedir info de la orden ejecutada
            time.sleep(0.5)
            fills = self.client.futures_account_trades(symbol=par["symbol"], limit=5)
            pr = float(fills[-1]["price"]) if fills else precio
            self.posiciones[par["symbol"]] = {
                "qty": qty,
                "precio_entrada": pr,
                "capital_usado": capital,
                "notional": notional,
                "leverage": LEVERAGE,
                "stop_loss": sl,
                "take_profit": tp,
                "order_id": orden["orderId"],
                "timestamp": datetime.now().isoformat()
            }
            self.min_precios[par["symbol"]] = pr
            log.info(
                f"[SHORT REAL] {par['symbol']} @ ${pr:.2f} | "
                f"SL:{sl:.2f}% TP:{tp:.2f}% | Notional ${notional:.2f}"
            )
            return True
        except BinanceAPIException as e:
            log.error(f"Error ABRIR SHORT {par['symbol']}: {e}")
            return False

    # ------------------------------------------------------------
    # EJECUCION: CERRAR SHORT (BUY en futuros = cerrar corto)
    # ------------------------------------------------------------
    def ejecutar_cerrar_short(self, par):
        if par["symbol"] not in self.posiciones:
            return False
        pos = self.posiciones[par["symbol"]]
        precio = self.obtener_precio(par["symbol"])
        if not precio:
            return False

        # Ganancia % invertida: entrada -> abajo es ganancia
        gp = ((pos["precio_entrada"] - precio) / pos["precio_entrada"]) * 100
        # Ganancia USD apalancada
        gan = (pos["precio_entrada"] - precio) * pos["qty"]

        if gp >= pos.get("take_profit", 1.5):
            razon = "TAKE PROFIT"
        elif gp > 0:
            razon = "TRAILING STOP"
        else:
            razon = "STOP LOSS"

        if MODO_PAPER:
            self.ganancia_total += gan
            self.capital_actual += gan
            if gan > 0:
                self.wins += 1
            else:
                self.losses += 1
            total = self.wins + self.losses
            wr = (self.wins / total * 100) if total > 0 else 0
            log.info(
                f"[{razon}] {par['symbol']} SHORT: {gp:+.2f}% = ${gan:+.2f} | "
                f"Capital: ${self.capital_actual:.2f} | WR: {wr:.0f}% "
                f"({self.wins}W/{self.losses}L)"
            )
            self.historial.append({
                "symbol": par["symbol"],
                "side": "SHORT",
                "entrada": pos["precio_entrada"],
                "salida": precio,
                "ganancia_pct": round(gp, 3),
                "ganancia_usd": round(gan, 2),
                "razon": razon,
                "leverage": pos.get("leverage", LEVERAGE),
                "timestamp": datetime.now().isoformat()
            })
            del self.posiciones[par["symbol"]]
            if par["symbol"] in self.min_precios:
                del self.min_precios[par["symbol"]]
            return True
        try:
            # SIDE=BUY con posicion SHORT abierta = cerrarla
            # reduceOnly evita abrir un long por error
            self.client.futures_create_order(
                symbol=par["symbol"],
                side="BUY",
                type="MARKET",
                quantity=pos["qty"],
                reduceOnly=True
            )
            self.ganancia_total += gan
            self.capital_actual += gan
            if gan > 0:
                self.wins += 1
            else:
                self.losses += 1
            log.info(f"[{razon} REAL] {par['symbol']} SHORT: {gp:+.2f}% = ${gan:+.2f}")
            del self.posiciones[par["symbol"]]
            if par["symbol"] in self.min_precios:
                del self.min_precios[par["symbol"]]
            return True
        except BinanceAPIException as e:
            log.error(f"Error CERRAR SHORT {par['symbol']}: {e}")
            return False

    # ------------------------------------------------------------
    # PAUSA / STOP GLOBAL
    # ------------------------------------------------------------
    def verificar_pausa_manual(self):
        return os.path.exists("PAUSA.txt")

    def pausa_ordenada(self):
        log.info("PAUSA - cerrando shorts abiertos...")
        self.estado = "pausando"
        if not self.es_mi_turno():
            log.info("  Ciclo saltado - no es turno del bot SHORT")
            self._guardar_estado()
            return
        for par in self.pares_activos:
            if par["symbol"] in self.posiciones:
                self.ejecutar_cerrar_short(par)
                time.sleep(1)
        self.estado = "pausado"
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(
            f"Capital: ${self.capital_actual:.2f} | "
            f"Ganancia: ${self.ganancia_total:+.2f} ({pct:+.2f}%)"
        )
        self._guardar_estado()

    def verificar_stop_loss_global(self):
        caida = ((self.capital_inicial - self.capital_actual) / self.capital_inicial) * 100
        if caida >= STOP_LOSS_GLOBAL:
            log.warning(f"STOP-LOSS GLOBAL: -{caida:.1f}%")
            self.pausa_ordenada()
            return True
        return False

    def _guardar_estado(self):
        total = self.wins + self.losses
        estado = {
            "bot": "SHORT",
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
            "posiciones": {
                s: {
                    "side": "SHORT",
                    "entrada": p["precio_entrada"],
                    "sl": p.get("stop_loss"),
                    "tp": p.get("take_profit"),
                    "leverage": p.get("leverage", LEVERAGE),
                }
                for s, p in self.posiciones.items()
            },
            "ultimos_trades": self.historial[-15:]
        }
        with open("bot_short_state.json", "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------
    # CICLO PRINCIPAL
    # ------------------------------------------------------------
    def ciclo_trading(self):
        self.ciclo += 1
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(
            f"== Ciclo SHORT #{self.ciclo} | ${self.capital_actual:.2f} | "
            f"{pct:+.2f}% | {self.wins}W/{self.losses}L =="
        )
        if self.ciclo % 10 == 1:
            self.seleccionar_pares()
        for par in self.pares_activos:
            try:
                senal, conf, ind = self.analizar_par(par)
                t1h = ind.get("tendencia_1h", "?")
                r = ind.get("rsi_5m", 0)
                p = ind.get("precio", 0)
                sl = ind.get("sl", 0)
                tp = ind.get("tp", 0)
                log.info(
                    f"  {par['symbol']}: ${p:.2f} | RSI={r} | Conf={conf}% | "
                    f"1H:{t1h} | SL:{sl}% TP:{tp}% | -> {senal}"
                )
                if senal == "ABRIR_SHORT" and par["symbol"] not in self.posiciones:
                    self.ejecutar_abrir_short(par, self.capital_por_par, ind)
                elif senal == "CERRAR":
                    self.ejecutar_cerrar_short(par)
            except Exception as e:
                log.error(f"Error {par['symbol']}: {e}")
        self._guardar_estado()


    def es_mi_turno(self):
        tend, _ = self.tendencia_1h("BTCUSDT")
        if tend == "bajista":
            return True
        if tend == "alcista":
            log.info("  [CONTROL] Mercado alcista -> turno del bot LONG")
        else:
            log.info("  [CONTROL] Mercado lateral -> ninguno opera")
        return False

    def run(self):
        log.info("=" * 60)
        log.info("Smart Trading Bot v4 SHORT - Espejo Bajista")
        log.info(f"  Modo:        {'PAPER' if MODO_PAPER else 'REAL'}")
        log.info(f"  Mercado:     Binance Futuros Perpetuos USDT-M")
        log.info(f"  Capital:     ${CAPITAL_INICIAL}")
        log.info(f"  Leverage:    {LEVERAGE}x")
        log.info(f"  Margin:      {MARGIN_TYPE}")
        log.info(f"  Stop global: -{STOP_LOSS_GLOBAL}%")
        log.info(f"  Ciclo cada {INTERVALO_CICLO}s")
        log.info(f"  Solo abre SHORT en tendencia BAJISTA 1H")
        log.info(f"  Stop loss y take profit DINAMICOS (ATR)")
        log.info(f"  Trailing stop INVERTIDO (sigue al minimo)")
        log.info("=" * 60)
        if not self.conectar():
            return
        self.estado = "activo"
        self.seleccionar_pares()
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
                time.sleep(30)


# ============================================================
# SERVIDOR HTTP - puerto distinto del bot long
# ============================================================
class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with open("bot_short_state.json", "r") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode())
        except Exception:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_web():
    # Puerto 8081 por defecto para no chocar con el bot long (8080)
    port = int(os.environ.get("PORT_SHORT", 8081))
    server = HTTPServer(("0.0.0.0", port), StatusHandler)
    server.serve_forever()


if __name__ == "__main__":
    t = threading.Thread(target=start_web, daemon=True)
    t.start()
    bot = SmartTradingBotShort()
    bot.run()
