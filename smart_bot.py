"""
Smart Trading Bot v3 - Datos reales de Binance
"""

API_KEY    = "bFMJCy0Kkfef1mFHuPKUG3tYNDqhXX1T9Oxv1UQUS9caNZh5CxbexozHqH3eudxU"
API_SECRET = "8nhqZ7DXJxUjmdGJ78hpKodNcMof69LgfIYabOAwg7YUyuwx5r23v8N4LcYq4QrO"

CAPITAL_INICIAL   = 500
STOP_LOSS_GLOBAL  = 10
TAKE_PROFIT_PAR   = 0.8
STOP_LOSS_PAR     = 0.4
MODO_PAPER        = True
INTERVALO_CICLO   = 300

import time
import json
import math
import logging
import os
import random
import urllib.request
import ssl
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("SmartBot")

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

class SmartTradingBot:

    def __init__(self):
        self.client = None
        self.capital_actual = CAPITAL_INICIAL
        self.capital_inicial = CAPITAL_INICIAL
        self.pares_activos = []
        self.posiciones = {}
        self.historial_trades = []
        self.ganancia_total = 0
        self.ciclo = 0
        self.inicio = datetime.now()
        self.estado = "iniciando"
        self.capital_por_par = CAPITAL_INICIAL / 2
        self.wins = 0
        self.losses = 0

    def conectar(self):
        if MODO_PAPER:
            log.info("MODO PAPER - Precios reales, ordenes simuladas")
            return True
        try:
            self.client = Client(API_KEY, API_SECRET)
            self.client.ping()
            log.info("Conectado a Binance")
            return True
        except Exception as e:
            log.error(f"Error conexion: {e}")
            return False

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
        log.info(f"Pares ({len(pares)}): {[p['symbol'] for p in pares]} | ${self.capital_por_par:.2f} c/u")
        self._guardar_estado()

    def obtener_precio(self, symbol):
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
            req = urllib.request.urlopen(url, timeout=10, context=SSL_CTX)
            data = json.loads(req.read())
            return float(data["price"])
        except Exception as e:
            log.error(f"Error precio {symbol}: {e}")
            return None

    def obtener_klines_reales(self, symbol, intervalo="5m", limite=100):
        try:
            url = (
                f"https://api.binance.com/api/v3/klines"
                f"?symbol={symbol}&interval={intervalo}&limit={limite}"
            )
            req = urllib.request.urlopen(url, timeout=15, context=SSL_CTX)
            raw = json.loads(req.read())
            klines = []
            for k in raw:
                klines.append({
                    "open":   float(k[1]),
                    "high":   float(k[2]),
                    "low":    float(k[3]),
                    "close":  float(k[4]),
                    "volume": float(k[5])
                })
            return klines
        except Exception as e:
            log.error(f"Error klines {symbol}: {e}")
            return []

    def calcular_rsi(self, precios, periodo=14):
        if len(precios) < periodo + 1:
            return 50.0
        deltas = [precios[i] - precios[i-1] for i in range(1, len(precios))]
        ganancias = [d if d > 0 else 0.0 for d in deltas]
        perdidas = [-d if d < 0 else 0.0 for d in deltas]
        avg_g = sum(ganancias[:periodo]) / periodo
        avg_p = sum(perdidas[:periodo]) / periodo
        for i in range(periodo, len(deltas)):
            avg_g = (avg_g * (periodo - 1) + ganancias[i]) / periodo
            avg_p = (avg_p * (periodo - 1) + perdidas[i]) / periodo
        if avg_p == 0:
            return 100.0
        rs = avg_g / avg_p
        return 100.0 - (100.0 / (1.0 + rs))

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

    def calcular_macd(self, precios):
        if len(precios) < 26:
            return 0.0, 0.0
        ema12 = self.calcular_ema(precios, 12)
        ema26 = self.calcular_ema(precios, 26)
        macd = ema12 - ema26
        vals = []
        for i in range(25, len(precios)):
            v = self.calcular_ema(precios[:i+1], 12) - self.calcular_ema(precios[:i+1], 26)
            vals.append(v)
        signal = self.calcular_ema(vals, 9) if len(vals) >= 9 else macd
        return macd, signal

    def detectar_tendencia(self, klines):
        if len(klines) < 20:
            return "lateral"
        ultimos = [k["close"] for k in klines[-20:]]
        primeros = [k["close"] for k in klines[-40:-20]] if len(klines) >= 40 else ultimos
        prom_reciente = sum(ultimos) / len(ultimos)
        prom_anterior = sum(primeros) / len(primeros)
        cambio = ((prom_reciente - prom_anterior) / prom_anterior) * 100
        if cambio > 0.3:
            return "alcista"
        elif cambio < -0.3:
            return "bajista"
        return "lateral"

    def analizar_par(self, par):
        klines = self.obtener_klines_reales(par["symbol"])
        if not klines or len(klines) < 50:
            return "ESPERAR", 0, {}

        cierres = [k["close"] for k in klines]
        volumenes = [k["volume"] for k in klines]
        precio = cierres[-1]

        rsi = self.calcular_rsi(cierres)
        ema9 = self.calcular_ema(cierres, 9)
        ema21 = self.calcular_ema(cierres, 21)
        ema50 = self.calcular_ema(cierres, 50)
        macd, signal = self.calcular_macd(cierres)
        tendencia = self.detectar_tendencia(klines)

        vol_spike = False
        if len(volumenes) >= 20:
            avg_vol = sum(volumenes[-20:-1]) / 19
            vol_spike = volumenes[-1] >= avg_vol * 1.5

        puntos = 0

        if 40 <= rsi <= 60:
            puntos += 20
        elif 35 <= rsi < 40 or 60 < rsi <= 65:
            puntos += 10

        if precio > ema9 > ema21 > ema50:
            puntos += 30
        elif precio > ema9 > ema21:
            puntos += 18
        elif precio > ema9:
            puntos += 8

        if macd > signal and macd > 0:
            puntos += 25
        elif macd > signal:
            puntos += 10

        if vol_spike:
            puntos += 15

        if tendencia == "alcista":
            puntos += 10
        elif tendencia == "bajista":
            puntos -= 15

        confianza = max(0, min(100, puntos))
        umbral = 65 if par["estrategia"] == "momentum_volume" else 55

        indicadores = {
            "rsi": round(rsi, 1),
            "precio": round(precio, 4),
            "confianza": confianza,
            "tendencia": tendencia,
            "vol_spike": vol_spike,
            "macd_pos": macd > signal
        }

        if par["symbol"] in self.posiciones:
            pos = self.posiciones[par["symbol"]]
            gp = ((precio - pos["precio_entrada"]) / pos["precio_entrada"]) * 100

            if gp >= TAKE_PROFIT_PAR:
                return "VENDER", confianza, indicadores
            if gp <= -STOP_LOSS_PAR:
                return "VENDER", confianza, indicadores

            if gp > 0.3 and tendencia == "bajista":
                return "VENDER", confianza, indicadores

            return "ESPERAR", confianza, indicadores

        if tendencia == "bajista":
            return "ESPERAR", confianza, indicadores

        if confianza >= umbral:
            return "COMPRAR", confianza, indicadores

        return "ESPERAR", confianza, indicadores

    def ejecutar_compra(self, par, capital):
        precio = self.obtener_precio(par["symbol"])
        if not precio:
            return False
        if MODO_PAPER:
            qty = (capital * 0.999) / precio
            self.posiciones[par["symbol"]] = {
                "qty": qty,
                "precio_entrada": precio,
                "capital_usado": capital,
                "timestamp": datetime.now().isoformat()
            }
            log.info(f"[COMPRA] {par['symbol']} @ ${precio:.2f} | ${capital:.2f}")
            return True
        try:
            info = self.client.get_symbol_info(par["symbol"])
            lot = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
            step = float(lot["stepSize"])
            qty = math.floor((capital * 0.999) / precio / step) * step
            orden = self.client.create_order(
                symbol=par["symbol"], side="BUY",
                type="MARKET", quantity=round(qty, 8)
            )
            pr = float(orden["fills"][0]["price"]) if orden.get("fills") else precio
            self.posiciones[par["symbol"]] = {
                "qty": qty, "precio_entrada": pr,
                "capital_usado": capital,
                "order_id": orden["orderId"],
                "timestamp": datetime.now().isoformat()
            }
            log.info(f"[COMPRA REAL] {par['symbol']} @ ${pr:.2f}")
            return True
        except BinanceAPIException as e:
            log.error(f"Error COMPRA {par['symbol']}: {e}")
            return False

    def ejecutar_venta(self, par):
        if par["symbol"] not in self.posiciones:
            return False
        pos = self.posiciones[par["symbol"]]
        precio = self.obtener_precio(par["symbol"])
        if not precio:
            return False
        gp = ((precio - pos["precio_entrada"]) / pos["precio_entrada"]) * 100
        gan = pos["qty"] * precio - pos["capital_usado"]
        razon = "TAKE PROFIT" if gp >= TAKE_PROFIT_PAR else ("TENDENCIA" if gp > 0 else "STOP LOSS")

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
                f"[{razon}] {par['symbol']}: {gp:+.2f}% = ${gan:+.2f} | "
                f"Capital: ${self.capital_actual:.2f} | WR: {wr:.0f}% ({self.wins}W/{self.losses}L)"
            )
            self.historial_trades.append({
                "symbol": par["symbol"],
                "entrada": pos["precio_entrada"],
                "salida": precio,
                "ganancia_pct": round(gp, 3),
                "ganancia_usd": round(gan, 2),
                "razon": razon,
                "timestamp": datetime.now().isoformat()
            })
            del self.posiciones[par["symbol"]]
            return True
        try:
            self.client.create_order(
                symbol=par["symbol"], side="SELL",
                type="MARKET", quantity=pos["qty"]
            )
            self.ganancia_total += gan
            self.capital_actual += gan
            if gan > 0:
                self.wins += 1
            else:
                self.losses += 1
            log.info(f"[{razon} REAL] {par['symbol']}: {gp:+.2f}% = ${gan:+.2f}")
            del self.posiciones[par["symbol"]]
            return True
        except BinanceAPIException as e:
            log.error(f"Error VENTA {par['symbol']}: {e}")
            return False

    def verificar_pausa_manual(self):
        return os.path.exists("PAUSA.txt")

    def pausa_ordenada(self):
        log.info("PAUSA - cerrando posiciones...")
        self.estado = "pausando"
        for par in self.pares_activos:
            if par["symbol"] in self.posiciones:
                self.ejecutar_venta(par)
                time.sleep(1)
        self.estado = "pausado"
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(f"Capital final: ${self.capital_actual:.2f} | Ganancia: ${self.ganancia_total:+.2f} ({pct:+.2f}%)")
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
            "timestamp": datetime.now().isoformat(),
            "capital_inicial": self.capital_inicial,
            "capital_actual": round(self.capital_actual, 2),
            "ganancia_total": round(self.ganancia_total, 2),
            "ganancia_pct": round((self.ganancia_total / self.capital_inicial) * 100, 2),
            "estado": self.estado,
            "ciclo": self.ciclo,
            "modo": "PAPER" if MODO_PAPER else "REAL",
            "pares_activos": [p["symbol"] for p in self.pares_activos],
            "wins": self.wins,
            "losses": self.losses,
            "winrate": round(self.wins / total * 100, 1) if total > 0 else 0,
            "posiciones_abiertas": len(self.posiciones),
            "posiciones": {
                s: {"precio_entrada": p["precio_entrada"], "capital": round(p["capital_usado"], 2)}
                for s, p in self.posiciones.items()
            },
            "ultimos_trades": self.historial_trades[-15:],
            "uptime_horas": round((datetime.now() - self.inicio).total_seconds() / 3600, 2)
        }
        with open("bot_state.json", "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2, ensure_ascii=False)

    def ciclo_trading(self):
        self.ciclo += 1
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(f"== Ciclo #{self.ciclo} | ${self.capital_actual:.2f} | {pct:+.2f}% ==")
        if self.ciclo % 10 == 1:
            self.seleccionar_pares()
        for par in self.pares_activos:
            try:
                senal, conf, ind = self.analizar_par(par)
                t = ind.get("tendencia", "?")
                r = ind.get("rsi", 0)
                p = ind.get("precio", 0)
                log.info(f"  {par['symbol']}: ${p:.2f} | RSI={r} | Conf={conf}% | {t} | -> {senal}")
                if senal == "COMPRAR" and par["symbol"] not in self.posiciones:
                    self.ejecutar_compra(par, self.capital_por_par)
                elif senal == "VENDER":
                    self.ejecutar_venta(par)
            except Exception as e:
                log.error(f"Error {par['symbol']}: {e}")
        self._guardar_estado()

    def run(self):
        log.info("=" * 55)
        log.info("Smart Trading Bot v3 - Datos Reales")
        log.info(f"  Modo:    {'PAPER' if MODO_PAPER else 'REAL'}")
        log.info(f"  Capital: ${CAPITAL_INICIAL}")
        log.info(f"  TP: +{TAKE_PROFIT_PAR}% | SL: -{STOP_LOSS_PAR}% | Global: -{STOP_LOSS_GLOBAL}%")
        log.info(f"  Ciclo cada {INTERVALO_CICLO}s")
        log.info("=" * 55)
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

if __name__ == "__main__":
    bot = SmartTradingBot()
    bot.run()
