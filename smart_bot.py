"""
Smart Trading Bot v4 - Ultra Selectivo
Solo opera en alzas confirmadas con trailing stop
"""

import os
API_KEY    = os.environ.get("BINANCE_API_KEY", "TU_API_KEY_AQUI")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "TU_API_SECRET_AQUI")

CAPITAL_INICIAL   = 500
STOP_LOSS_GLOBAL  = 10
MODO_PAPER        = True
INTERVALO_CICLO   = 300

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import time
import json
import math
import logging
import os
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
log = logging.getLogger("SmartBot")

API_KEY    = os.environ.get("BINANCE_API_KEY", "TU_API_KEY_AQUI")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "TU_API_SECRET_AQUI")

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


# Perfil de volatilidad por par (basado en datos historicos)
# beta: cuanto se mueve vs BTC (>1 = mas volatil)
# sl_mult: multiplicador de ATR para SL (alts volatiles necesitan mas margen)
# min_confianza: umbral minimo ajustado por volatilidad
PERFIL_PARES = {
    "BTCUSDT": {"beta": 1.0, "sl_mult": 2.0, "min_confianza": 60},
    "ETHUSDT": {"beta": 1.15, "sl_mult": 2.2, "min_confianza": 60},
    "BNBUSDT": {"beta": 0.9, "sl_mult": 2.0, "min_confianza": 60},
    "SOLUSDT": {"beta": 1.5, "sl_mult": 2.5, "min_confianza": 65},
    "XRPUSDT": {"beta": 1.3, "sl_mult": 2.3, "min_confianza": 62},
}
class SmartTradingBot:

    def __init__(self):
        self.client = None
        self.capital_actual = CAPITAL_INICIAL
        self.capital_inicial = CAPITAL_INICIAL
        self.pares_activos = []


        self.posiciones = {}
        self.historial = []
        self.ganancia_total = 0
        self.ciclo = 0
        self.inicio = datetime.now()
        self.estado = "iniciando"
        self.capital_por_par = CAPITAL_INICIAL / 2
        self.wins = 0
        self.losses = 0
        self.max_precios = {}

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

    def obtener_precio(self, symbol):
        try:
            data = api_get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}")
            return float(data["price"])
        except Exception as e:
            log.error(f"Error precio {symbol}: {e}")
            return None

    def obtener_klines(self, symbol, intervalo="5m", limite=100):
        try:
            data = api_get(
                f"https://api.binance.com/api/v3/klines"
                f"?symbol={symbol}&interval={intervalo}&limit={limite}"
            )
            return [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                     "close": float(k[4]), "volume": float(k[5])} for k in data]
        except Exception as e:
            log.error(f"Error klines {symbol} {intervalo}: {e}")
            return []

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

    def analizar_par(self, par):
        klines_5m = self.obtener_klines(par["symbol"], "5m", 100)
        if not klines_5m or len(klines_5m) < 50:
            return "ESPERAR", 0, {}

        cierres = [k["close"] for k in klines_5m]
        volumenes = [k["volume"] for k in klines_5m]
        precio = cierres[-1]
        atr_5m = self.calcular_atr(klines_5m)
        # ATR de 1H para SL/TP (mas representativo de volatilidad real)
        klines_1h_atr = self.obtener_klines(par["symbol"], "1h", 20)
        atr_1h = self.calcular_atr(klines_1h_atr) if len(klines_1h_atr) > 14 else atr_5m * 3
        atr = atr_1h  # Usar ATR 1H para calculos de SL/TP
        atr_pct = (atr / precio) * 100 if precio > 0 else 0

        rsi_5m = self.calcular_rsi(cierres)
        ema9 = self.calcular_ema(cierres, 9)
        ema21 = self.calcular_ema(cierres, 21)
        ema50 = self.calcular_ema(cierres, 50)

        tendencia_h, rsi_1h = self.tendencia_1h(par["symbol"])

        # Perfil de volatilidad del par
        perfil = PERFIL_PARES.get(par["symbol"], {"beta": 1.0, "sl_mult": 2.0, "min_confianza": 60})

        vol_spike = False
        if len(volumenes) >= 20:
            avg_vol = sum(volumenes[-20:-1]) / 19
            vol_spike = volumenes[-1] >= avg_vol * 1.3

        # --- SCORING v5 ---
        puntos = 0

        # Tendencia 1H (lateral ahora suma puntos)
        if tendencia_h == "alcista":
            puntos += 25
        elif tendencia_h == "lateral":
            puntos += 10
        elif tendencia_h == "bajista":
            puntos -= 25

        # EMAs apiladas (mas granular)
        if precio > ema9 > ema21 > ema50:
            puntos += 20
        elif precio > ema9 > ema21:
            puntos += 12
        elif precio > ema9:
            puntos += 5

        # RSI: sobrevendido en uptrend = oportunidad de rebote
        # Historicamente RSI < 30 en BTC rebota >0.5% en 70%+ de los casos
        if 35 <= rsi_5m <= 55:
            puntos += 15
        elif 28 <= rsi_5m < 35:
            puntos += 10
        elif rsi_5m > 70:
            puntos -= 15

        # Volume spike (umbral relajado a 1.3x)
        if vol_spike:
            puntos += 10

        # 3 velas verdes consecutivas (antes eran 5)
        ultimos_3 = cierres[-3:]
        if all(ultimos_3[i] <= ultimos_3[i+1] for i in range(len(ultimos_3)-1)):
            puntos += 10

        # Engulfing alcista: patron de reversion
        if len(klines_5m) >= 3:
            prev = klines_5m[-2]
            curr = klines_5m[-1]
            if prev["close"] < prev["open"] and curr["close"] > curr["open"]:
                if curr["close"] > prev["open"]:
                    puntos += 10

        confianza = max(0, min(100, puntos))

        # --- SL/TP DINAMICOS v5 ---
        # SL = ATR * multiplicador del par, min 0.4%, max 1.2%
        # TP = 1.5x SL, min 0.6%
        sl_mult = perfil["sl_mult"]
        stop_loss_dinamico = max(0.8, min(2.0, atr_pct * sl_mult))
        take_profit_dinamico = max(1.2, stop_loss_dinamico * 1.5)

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

        # --- GESTION POSICION ABIERTA v5 ---
        if par["symbol"] in self.posiciones:
            pos = self.posiciones[par["symbol"]]

            if par["symbol"] not in self.max_precios:
                self.max_precios[par["symbol"]] = precio
            if precio > self.max_precios[par["symbol"]]:
                self.max_precios[par["symbol"]] = precio

            ganancia_usd = (precio - pos["precio_entrada"]) * pos["qty"]
            max_p = self.max_precios[par["symbol"]]
            caida_usd = (max_p - precio) * pos["qty"]

            # STOP LOSS DURO basado en % desde entrada
            sl_pct = pos.get("stop_loss", 0.5)
            sl_precio = pos["precio_entrada"] * (1 - sl_pct / 100)
            if precio <= sl_precio:
                return "VENDER", confianza, indicadores

            # TAKE PROFIT duro
            tp_pct = pos.get("take_profit", 1.0)
            tp_precio = pos["precio_entrada"] * (1 + tp_pct / 100)
            if precio >= tp_precio:
                return "VENDER", confianza, indicadores

            # Sin ganancia significativa: paciencia
            if ganancia_usd < 0.30:
                if tendencia_h == "bajista" and rsi_5m > 65:
                    return "VENDER", confianza, indicadores
                return "ESPERAR", confianza, indicadores

            # TRAILING proporcional a la ganancia maxima
            ganancia_max_usd = (max_p - pos["precio_entrada"]) * pos["qty"]

            if 0.30 <= ganancia_usd < 1.00:
                if ganancia_max_usd > 0.30 and caida_usd > ganancia_max_usd * 0.50:
                    return "VENDER", confianza, indicadores
                if tendencia_h == "bajista":
                    return "VENDER", confianza, indicadores
                return "ESPERAR", confianza, indicadores

            if 1.00 <= ganancia_usd < 3.00:
                if caida_usd > ganancia_max_usd * 0.40:
                    return "VENDER", confianza, indicadores
                if tendencia_h == "bajista" and rsi_5m > 60:
                    return "VENDER", confianza, indicadores
                return "ESPERAR", confianza, indicadores

            if ganancia_usd >= 3.00:
                if caida_usd > ganancia_max_usd * 0.35:
                    return "VENDER", confianza, indicadores
                return "ESPERAR", confianza, indicadores

        # --- FILTRO BTC v5: relajado ---
        # Lateral tambien permite operar (antes solo alcista)
        if par["symbol"] != "BTCUSDT":
            btc_tend, _ = self.tendencia_1h("BTCUSDT")
            if btc_tend == "bajista":
                return "ESPERAR", confianza, indicadores

        # Lateral requiere confianza mas alta
        if tendencia_h == "lateral" and confianza < 70:
            return "ESPERAR", confianza, indicadores

        # UMBRAL ajustado por par
        umbral = perfil["min_confianza"]
        if confianza >= umbral:
            indicadores["sl_usado"] = stop_loss_dinamico
            indicadores["tp_usado"] = take_profit_dinamico
            return "COMPRAR", confianza, indicadores

        return "ESPERAR", confianza, indicadores


    def ejecutar_compra(self, par, capital, indicadores):
        precio = self.obtener_precio(par["symbol"])
        if not precio:
            return False
        sl = indicadores.get("sl_usado", 0.5)
        tp = indicadores.get("tp_usado", 1.5)
        if MODO_PAPER:
            qty = (capital * 0.999) / precio
            self.posiciones[par["symbol"]] = {
                "qty": qty,
                "precio_entrada": precio,
                "capital_usado": capital,
                "stop_loss": sl,
                "take_profit": tp,
                "timestamp": datetime.now().isoformat()
            }
            self.max_precios[par["symbol"]] = precio
            log.info(f"[COMPRA] {par['symbol']} @ ${precio:.2f} | SL:{sl:.2f}% TP:{tp:.2f}% | ${capital:.2f}")
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
                "stop_loss": sl, "take_profit": tp,
                "order_id": orden["orderId"],
                "timestamp": datetime.now().isoformat()
            }
            self.max_precios[par["symbol"]] = pr
            log.info(f"[COMPRA REAL] {par['symbol']} @ ${pr:.2f} | SL:{sl:.2f}% TP:{tp:.2f}%")
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
                f"[{razon}] {par['symbol']}: {gp:+.2f}% = ${gan:+.2f} | "
                f"Capital: ${self.capital_actual:.2f} | WR: {wr:.0f}% ({self.wins}W/{self.losses}L)"
            )
            self.historial.append({
                "symbol": par["symbol"],
                "entrada": pos["precio_entrada"],
                "salida": precio,
                "ganancia_pct": round(gp, 3),
                "ganancia_usd": round(gan, 2),
                "razon": razon,
                "timestamp": datetime.now().isoformat()
            })
            del self.posiciones[par["symbol"]]
            if par["symbol"] in self.max_precios:
                del self.max_precios[par["symbol"]]
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
            if par["symbol"] in self.max_precios:
                del self.max_precios[par["symbol"]]
            return True
        except BinanceAPIException as e:
            log.error(f"Error VENTA {par['symbol']}: {e}")
            return False

    def verificar_pausa_manual(self):
        return os.path.exists("PAUSA.txt")

    def pausa_ordenada(self):
        log.info("PAUSA - cerrando posiciones...")
        self.estado = "pausando"
        if not self.es_mi_turno():
            log.info("  Ciclo saltado - no es turno del bot LONG")
            self._guardar_estado()
            return
        for par in self.pares_activos:
            if par["symbol"] in self.posiciones:
                self.ejecutar_venta(par)
                time.sleep(1)
        self.estado = "pausado"
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(f"Capital: ${self.capital_actual:.2f} | Ganancia: ${self.ganancia_total:+.2f} ({pct:+.2f}%)")
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
            "capital_actual": round(self.capital_actual, 2),
            "ganancia_total": round(self.ganancia_total, 2),
            "ganancia_pct": round((self.ganancia_total / self.capital_inicial) * 100, 2),
            "estado": self.estado,
            "ciclo": self.ciclo,
            "wins": self.wins, "losses": self.losses,
            "winrate": round(self.wins / total * 100, 1) if total > 0 else 0,
            "posiciones": {
                s: {"entrada": p["precio_entrada"], "sl": p.get("stop_loss"), "tp": p.get("take_profit")}
                for s, p in self.posiciones.items()
            },
            "ultimos_trades": self.historial[-15:]
        }
        with open("bot_state.json", "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2, ensure_ascii=False)

    def ciclo_trading(self):
        self.ciclo += 1
        pct = (self.ganancia_total / self.capital_inicial) * 100
        log.info(f"== Ciclo #{self.ciclo} | ${self.capital_actual:.2f} | {pct:+.2f}% | {self.wins}W/{self.losses}L ==")
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
                log.info(f"  {par['symbol']}: ${p:.2f} | RSI={r} | Conf={conf}% | 1H:{t1h} | SL:{sl}% TP:{tp}% | -> {senal}")
                if senal == "COMPRAR" and par["symbol"] not in self.posiciones:
                    self.ejecutar_compra(par, self.capital_por_par, ind)
                elif senal == "VENDER":
                    self.ejecutar_venta(par)
            except Exception as e:
                log.error(f"Error {par['symbol']}: {e}")
        self._guardar_estado()


    def es_mi_turno(self):
        tend, _ = self.tendencia_1h("BTCUSDT")
        if tend == "alcista":
            return True
        if tend == "bajista":
            log.info("  [CONTROL] Mercado bajista -> turno del bot SHORT")
        else:
            log.info("  [CONTROL] Mercado lateral -> ninguno opera")
        return False

    def run(self):
        log.info("=" * 55)
        log.info("Smart Trading Bot v4 - Ultra Selectivo")
        log.info(f"  Modo:    {'PAPER' if MODO_PAPER else 'REAL'}")
        log.info(f"  Capital: ${CAPITAL_INICIAL}")
        log.info(f"  Stop global: -{STOP_LOSS_GLOBAL}%")
        log.info(f"  Ciclo cada {INTERVALO_CICLO}s")
        log.info(f"  Solo compra en tendencia ALCISTA 1H")
        log.info(f"  Stop loss y take profit DINAMICOS (ATR)")
        log.info(f"  Trailing stop para proteger ganancias")
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

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with open("bot_state.json", "r") as f:
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
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), StatusHandler)
    server.serve_forever()

if __name__ == "__main__":
    t = threading.Thread(target=start_web, daemon=True)
    t.start()
    bot = SmartTradingBot()
    bot.run()
