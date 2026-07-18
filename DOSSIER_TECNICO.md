# Antigravity Predictor — Dossier Técnico
**Versión:** 2.0  
**Fecha:** 2026-07-17  
**Repositorio:** https://github.com/serviciosnewtech-a11y/antigravity-predictor (privado)  
**Autor:** Luis E. Wilson — serviciosnewtech@gmail.com

---

## 1. Visión General

Antigravity Predictor es un sistema modular de señales de trading en tiempo real para criptomonedas. Corre sobre Docker Compose como un conjunto de servicios independientes que se comunican entre sí mediante REST y WebSocket. El sistema **no ejecuta órdenes de manera autónoma** — genera señales, las enriquece con razonamiento de IA, y registra evaluaciones de estrategias para revisión humana.

### Principios de diseño

- **Modularidad:** cada servicio se puede actualizar, reiniciar y reemplazar de forma independiente.
- **Sin fricciones:** `make up` levanta todo el stack; `make retrain` actualiza los modelos.
- **Independencia del SO anfitrión:** Docker abstrae el entorno — funciona igual en Ubuntu, macOS o cualquier VPS con Docker instalado.
- **Supervisión humana:** las señales se muestran, las estrategias se evalúan en papel, pero ninguna promoción ocurre automáticamente.

---

## 2. Arquitectura del Sistema

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Red Docker: antigravity                      │
│                                                                     │
│  ┌──────────────┐    WS + REST    ┌──────────────────────────────┐  │
│  │  dashboard   │ ─────────────▶ │         predictor            │  │
│  │  nginx:80    │                │  FastAPI  port 18910         │  │
│  └──────────────┘                │  • 6 modelos LightGBM        │  │
│         │                        │  • WebSocket (candles vivos) │  │
│         │ /executor/*            │  • signal_agent (Hermes)     │  │
│         ▼                        └──────────┬───────────────────┘  │
│  ┌──────────────┐                           │ WS feed              │
│  │   executor   │                           ▼                      │
│  │  FastAPI     │                ┌──────────────────────────────┐  │
│  │  port 18911  │                │           forge              │  │
│  │  ccxt / DRY  │                │  FastAPI  port 18912         │  │
│  └──────────────┘                │  • colecta datos en tiempo   │  │
│                                  │    real del Predictor        │  │
│  ┌──────────────┐                │  • 16+ estrategias en papel  │  │
│  │   retrain    │  (perfil)      │  • SQLite: candles + trades  │  │
│  │  bash script │ ─▶ models/    │  • leaderboard sin promoción │  │
│  └──────────────┘                └──────────────────────────────┘  │
│                                                                     │
│  Volúmenes compartidos: models/ · data/ · logs/ · forge_data/      │
└─────────────────────────────────────────────────────────────────────┘
```

### Puertos expuestos al host

| Puerto | Servicio   | Descripción                              |
|--------|------------|------------------------------------------|
| 80     | dashboard  | Panel de control (nginx)                 |
| 18910  | predictor  | API REST + WebSocket                     |
| 18911  | executor   | API de ejecución de órdenes              |
| 18912  | forge      | API de evaluación de estrategias         |

---

## 3. Servicios en Detalle

### 3.1 Predictor (`predictor/`)

**Imagen base:** `python:3.11-slim`  
**Puerto:** 18910  
**Archivo principal:** `src/predictor_server.py`

El núcleo del sistema. Conecta al WebSocket de Bybit, recibe candles en tiempo real, calcula features, corre los 6 modelos LightGBM y emite predicciones vía WebSocket al dashboard y al Forge.

#### Modelos

| Modelo             | Par        | Dirección | Archivo                  |
|--------------------|------------|-----------|--------------------------|
| BTC long           | BTC/USDT   | Long      | `models/model_btc_long.txt`  |
| BTC short          | BTC/USDT   | Short     | `models/model_btc_short.txt` |
| ETH long           | ETH/USDT   | Long      | `models/model_eth_long.txt`  |
| ETH short          | ETH/USDT   | Short     | `models/model_eth_short.txt` |
| SOL long           | SOL/USDT   | Long      | `models/model_sol_long.txt`  |
| SOL short          | SOL/USDT   | Short     | `models/model_sol_short.txt` |

> **Nota:** SOL long tiene win rate inferior al 40% en todos los umbrales evaluados. Los thresholds de entrada están configurados en 9.9999 (efectivamente desactivado) hasta que el retrain mejore el modelo.

#### Endpoints REST

| Método | Ruta                        | Descripción                                      |
|--------|-----------------------------|--------------------------------------------------|
| GET    | `/health`                   | Estado, uptime, versión de modelos               |
| GET    | `/predict/{symbol}`         | Predicción cruda: `long_prob`, `short_prob`, `trend` |
| GET    | `/enriched/{symbol}`        | Señal enriquecida por Hermes (narrativa + acción)|
| GET    | `/api/status`               | Estado completo de todos los motores             |
| WS     | `/ws`                       | Stream en tiempo real (tick cada ~15s)           |

#### Mensaje WebSocket (formato de tick)

```json
{
  "type":        "tick",
  "symbol":      "BTC/USDT",
  "ts":          "2026-07-17T18:05:00Z",
  "close":       67420.5,
  "high":        67500.0,
  "low":         67380.0,
  "volume":      1234.5,
  "long_prob":   0.712,
  "short_prob":  0.198,
  "trend":       "bullish"
}
```

#### Signal Agent — Hermes

El agente Hermes enriquece la señal cruda del modelo con un análisis narrativo. Se ejecuta dentro del mismo contenedor que el predictor.

**Backend configurable via `SA_INFERENCE_BACKEND`:**

| Valor    | Descripción                                 | Requisito               |
|----------|---------------------------------------------|-------------------------|
| `ollama` | Inferencia local (default)                  | Ollama corriendo en host|
| `claude` | Anthropic API                               | `ANTHROPIC_API_KEY`     |

**Respuesta enriquecida:**
```json
{
  "symbol":     "BTC/USDT",
  "action":     "LONG",
  "confidence": 0.712,
  "reasoning":  "Momentum alcista en 4h con soporte macro...",
  "risk_note":  "ATR elevado — considerar reducir tamaño",
  "timestamp":  "2026-07-17T18:05:12Z"
}
```

#### Parámetros de configuración (`config.json`)

```json
{
  "exchange":  "bybit",
  "timeframe": "15m",
  "assets": {
    "BTC/USDT": {
      "buy_threshold":        0.1898,
      "sell_threshold":       0.2568,
      "exit_threshold":       0.1537,
      "exit_short_threshold": 0.1981,
      "tp_atr_mult":          1.5,
      "sl_atr_mult":          1.0,
      "max_candles_held":     4
    }
  }
}
```

---

### 3.2 Dashboard (`dashboard/`)

**Imagen base:** `nginx:alpine`  
**Puerto:** 80  
**Archivos:** `dashboard/index.html`, `app.js`, `style.css`

Panel de control de una sola página (SPA). Se conecta al Predictor por WebSocket y muestra:
- Precio en tiempo real por par
- Probabilidades long/short con barras de confianza
- Panel "Agent Report" con el análisis de Hermes
- Historial de señales recientes

nginx actúa como proxy inverso para todos los servicios:

| Ruta nginx     | Servicio destino         |
|----------------|--------------------------|
| `/ws`          | `predictor:18910/ws`     |
| `/api/*`       | `predictor:18910/api/*`  |
| `/executor/*`  | `executor:18911/`        |
| `/forge/*`     | `forge:18912/`           |
| `/`            | archivos estáticos       |

---

### 3.3 Executor (`executor/`)

**Imagen base:** `python:3.11-slim`  
**Puerto:** 18911  
**Archivo principal:** `executor/server.py`  
**Librería de exchange:** ccxt

Recibe señales del Predictor (o de cualquier cliente HTTP) y las ejecuta en el exchange. Por defecto corre en modo **DRY_RUN=true** — registra las operaciones sin enviar órdenes reales.

#### Endpoints REST

| Método | Ruta                  | Descripción                                     |
|--------|-----------------------|-------------------------------------------------|
| GET    | `/health`             | Estado, posiciones abiertas, historial          |
| GET    | `/positions`          | Posiciones abiertas en el exchange              |
| POST   | `/execute`            | Ejecutar señal (body: `ExecuteRequest`)         |
| POST   | `/cancel/{symbol}`    | Cancelar órdenes abiertas para un par           |
| POST   | `/close/{symbol}`     | Cerrar posición a mercado                       |
| GET    | `/history`            | Historial de ejecuciones recientes              |

#### ExecuteRequest

```json
{
  "symbol":     "BTC/USDT",
  "side":       "long",
  "confidence": 0.712,
  "source":     "predictor",
  "reason":     "Breakout confirmado en 4h"
}
```

#### Flujo de ejecución

```
Señal recibida
    │
    ├─ confidence < MIN_LONG/SHORT_CONF? → action: "skipped"
    ├─ DRY_RUN=true?                    → action: "dry_run" (log only)
    └─ DRY_RUN=false
           │
           ├─ fetch_balance() → calcular qty (POSITION_SIZE_PCT del balance libre)
           ├─ create_market_order(symbol, side, qty)
           └─ log → action: "placed" | "error"
```

#### Variables de entorno clave

| Variable            | Default  | Descripción                               |
|---------------------|----------|-------------------------------------------|
| `DRY_RUN`           | `true`   | `false` para órdenes reales               |
| `EXCHANGE`          | `bybit`  | Exchange ccxt compatible                  |
| `EXCHANGE_API_KEY`  | —        | API key del exchange                      |
| `EXCHANGE_API_SECRET`| —       | API secret del exchange                   |
| `MIN_LONG_CONF`     | `0.60`   | Umbral mínimo de confianza para long      |
| `MIN_SHORT_CONF`    | `0.60`   | Umbral mínimo de confianza para short     |
| `POSITION_SIZE_PCT` | `0.02`   | Fracción del balance USDT por operación   |

---

### 3.4 Forge (`forge/`)

**Imagen base:** `python:3.11-slim`  
**Puerto:** 18912  
**Base de datos:** SQLite en `forge_data/forge.db`

Forge es el laboratorio de evaluación de estrategias. Se suscribe al WebSocket del Predictor, hace paper trading de múltiples variantes de estrategia en paralelo, y almacena todos los resultados para análisis humano posterior.

**Principio fundamental: nada se promueve automáticamente.**

#### Arquitectura interna

```
Predictor WS
    │
    ▼
collector.py          ← recibe ticks, calcula ATR, guarda candles en SQLite
    │
    ▼ on_candle()
simulator.py × N      ← un StrategySimulator por cada estrategia registrada
    │
    ▼
db.py (SQLite)        ← tabla candles + tabla trades
    │
    ▼
server.py             ← API REST para consultar resultados
```

#### Esquema de base de datos

**Tabla `candles`**
```sql
ts, symbol, open, high, low, close, volume, atr, long_prob, short_prob, trend
```
Retiene los últimos 5.000 candles por símbolo. Se usa para reconstruir contexto histórico.

**Tabla `trades`**
```sql
strategy_id, strategy_name, symbol, direction,
entry_ts, exit_ts, entry_price, exit_price,
tp_price, sl_price, exit_reason,  -- "tp" | "sl" | "timeout" | "open"
pnl_pct, candles_held, entry_conf
```

**Tabla `strategy_registry`**
```sql
id, name, symbol, direction, params (JSON), active, created_ts
```

#### Estrategias por defecto (16 variantes)

Las estrategias cubren una grilla de parámetros sobre los 3 pares y ambas direcciones:

| Grupo           | Variantes                                              |
|-----------------|--------------------------------------------------------|
| BTC long (5)    | baseline, tight_sl, loose_tp, hi_conf, scalp          |
| BTC short (3)   | baseline, tight_sl, hi_conf                            |
| ETH long (3)    | baseline, hi_conf, loose_tp                            |
| ETH short (2)   | baseline, hi_conf                                      |
| SOL short (3)   | baseline, hi_conf, scalp                               |

Parámetros que varían: `entry_threshold` (0.55 / 0.65), `tp_atr_mult` (0.8 / 1.5 / 2.0), `sl_atr_mult` (0.5 / 0.75 / 1.0), `max_candles_held` (2 / 4).

#### Endpoints REST

| Método | Ruta                        | Descripción                                     |
|--------|-----------------------------|-------------------------------------------------|
| GET    | `/health`                   | Uptime, candles recibidos, trades logueados     |
| GET    | `/strategies`               | Listado de estrategias activas                  |
| POST   | `/strategies`               | Registrar nueva estrategia en tiempo real       |
| DELETE | `/strategies/{id}`          | Desactivar estrategia                           |
| GET    | `/leaderboard`              | Ranking por `profit_factor` (o métrica elegida) |
| GET    | `/results`                  | Trades cerrados (filtrable por estrategia/par)  |
| GET    | `/results/{strategy_id}`    | Trades + métricas de una estrategia             |
| GET    | `/data/{symbol}`            | Candles recientes con predicciones              |
| GET    | `/open`                     | Posiciones simuladas abiertas actualmente       |

#### Métricas calculadas por el leaderboard

| Métrica          | Descripción                                        |
|------------------|----------------------------------------------------|
| `win_rate`       | % de trades con PnL > 0                           |
| `profit_factor`  | `avg_win / abs(avg_loss)`                          |
| `avg_pnl_pct`    | PnL promedio por trade en %                        |
| `total_pnl_pct`  | PnL acumulado (no compuesto)                       |
| `tp_exits`       | Trades que cerraron en TP                          |
| `sl_exits`       | Trades que cerraron en SL                          |
| `timeout_exits`  | Trades cerrados por tiempo máximo                  |
| `avg_candles_held`| Duración promedio en candles                      |
| `pnl_stddev`     | Desviación estándar del PnL (consistencia)         |

#### Ejemplo de leaderboard

```bash
curl "http://localhost:18912/leaderboard?min_trades=10&sort_by=profit_factor"
```

```json
{
  "leaderboard": [
    {
      "strategy_name": "btc_long_hi_conf",
      "symbol": "BTC/USDT",
      "direction": "long",
      "params": { "entry_threshold": 0.65, "tp_atr_mult": 1.5, "sl_atr_mult": 1.0 },
      "trade_count": 42,
      "win_rate": 61.9,
      "profit_factor": 1.87,
      "avg_pnl_pct": 0.38,
      "total_pnl_pct": 15.96
    }
  ],
  "sorted_by": "profit_factor"
}
```

---

### 3.5 Retrain (`retrain/`)

**Imagen base:** `python:3.11-slim`  
**Perfil Docker:** `retrain` (no arranca con `make up`)  
**Comando:** `make retrain`

Servicio de ejecución única que corre el pipeline completo de reentrenamiento.

#### Pipeline de retrain

```
Step 1  download_ohlcv.py
        ↓ OHLCV raw desde Bybit (BTC/ETH/SOL, 6 timeframes: 1m/5m/15m/1h/4h/1d)
        ↓ guarda en data/raw/*.parquet

Step 2  fetch_macro.py
        ↓ Gold, Oil, DXY, SPX, VIX via yfinance
        ↓ guarda en data/macro/*.parquet

Step 3  prepare_full_dataset.py
        ↓ merge OHLCV + macro + cross-asset correlations
        ↓ genera features (64 columnas) + labels long/short
        ↓ guarda en data/prepared/{btc,eth,sol}_{long,short}.parquet

Step 4  train_lightgbm.py × 6
        ↓ entrena un modelo por par/dirección
        ↓ evalúa AUC en test set
        ↓ gate: AUC ≥ MIN_AUC (default 0.54)
        ↓ si pasa: copia a models/ con timestamp

Step 5  summarize_run.py
        ↓ imprime tabla de métricas
        ↓ guarda training_report_{ts}.json en models/
```

#### Calidad gate

Si un modelo no alcanza el AUC mínimo, el modelo anterior **no es reemplazado**. El pipeline continúa con los demás pares. Los logs registran el rechazo.

```
[2026-07-17] BTC long  AUC=0.581 ✓ → models/model_btc_long.txt
[2026-07-17] BTC short AUC=0.521 ✗ → RECHAZADO (mínimo: 0.540)
```

---

## 4. Ingeniería de Features

Los modelos usan 64 features calculadas sobre cada candle de 15m.

### Features de precio y volumen
- RSI(14), RSI(7)
- MACD, señal MACD, histograma
- Bandas de Bollinger: posición relativa, ancho
- ATR(14), ATR(7)
- EMA(9), EMA(21), EMA(50), EMA(200)
- Pendiente de precios (5, 10, 20 candles)
- Ratio volumen vs promedio 20

### Features multi-timeframe
Los datos de 1h y 4h se resamplean y fusionan:
- RSI en 1h y 4h
- Tendencia EMA en 1h y 4h
- ATR relativo entre timeframes

### Features macro
- Gold (rendimiento 1d, 5d)
- Oil (rendimiento 1d, 5d)
- DXY — Dollar Index (correlación inversa)
- SPX — S&P 500 (correlación de risk-on/off)
- VIX — Índice de volatilidad (señal de miedo)

### Features cross-asset
- Correlación rolling BTC↔ETH (20 velas)
- Correlación rolling BTC↔SOL (20 velas)
- Spread de rendimiento BTC vs ETH

### Labels
- **Long label:** precio sube `tp_atr_mult × ATR` antes de bajar `sl_atr_mult × ATR` en las próximas 4 velas.
- **Short label:** precio baja `tp_atr_mult × ATR` antes de subir `sl_atr_mult × ATR` en las próximas 4 velas.

---

## 5. Estructura de Archivos

```
Predictor/
├── docker-compose.yml           ← orquestación de 4 servicios (+ retrain)
├── Makefile                     ← make up / down / retrain / logs / leaderboard
├── .env.example                 ← plantilla de configuración
├── .dockerignore
├── config.json                  ← parámetros de assets (thresholds, TP/SL)
├── DOSSIER.md                   ← dossier general en inglés
├── DOSSIER_TECNICO.md           ← este documento
├── CHANGES.md                   ← log de auditoría de todos los cambios
├── requirements.txt
├── retrain_all.sh               ← script principal de reentrenamiento
├── run_local.sh                 ← inicio rápido local (sin Docker)
├── setup.sh                     ← bootstrap de venv para uso sin Docker
│
├── dashboard/
│   ├── Dockerfile
│   ├── nginx.conf
│   ├── index.html
│   ├── app.js
│   └── style.css
│
├── predictor/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── lgbm_poc_setup.py        ← instalador del paquete lgbm_poc
│
├── executor/
│   ├── Dockerfile
│   └── server.py
│
├── forge/
│   ├── Dockerfile
│   ├── __init__.py
│   ├── server.py
│   ├── collector.py
│   ├── simulator.py
│   ├── strategies.py
│   └── db.py
│
├── retrain/
│   └── Dockerfile
│
├── src/                         ← código fuente Python
│   ├── predictor_server.py
│   ├── signal_agent/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   └── enricher.py
│   ├── lgbm_poc/                ← paquete instalable (sin conflictos con io built-in)
│   │   ├── __init__.py
│   │   ├── baseline.py
│   │   ├── features.py
│   │   ├── labels.py
│   │   ├── dataset.py
│   │   ├── futures.py
│   │   ├── io.py
│   │   └── resample_ohlcv.py
│   ├── fetch_macro.py
│   ├── prepare_full_dataset.py
│   ├── train_lightgbm.py
│   ├── download_ohlcv.py
│   └── summarize_run.py
│
├── models/                      ← volumen compartido: predictor + retrain + forge
│   ├── model_btc_long.txt
│   ├── model_btc_short.txt
│   ├── model_eth_long.txt
│   ├── model_eth_short.txt
│   ├── model_sol_long.txt
│   ├── model_sol_short.txt
│   └── training_report_*.json
│
├── data/                        ← volumen: retrain
│   ├── raw/                     ← OHLCV descargado
│   └── macro/                   ← datos macro
│
├── logs/                        ← volumen compartido: todos los servicios
│
└── forge_data/
    └── forge.db                 ← SQLite: candles + trades + strategy_registry
```

---

## 6. Guía de Operación

### 6.1 Primer arranque

```bash
# 1. Clonar el repositorio
git clone https://github.com/serviciosnewtech-a11y/antigravity-predictor.git
cd antigravity-predictor

# 2. Configurar entorno
cp .env.example .env
# Editar .env: ANTHROPIC_API_KEY o OLLAMA_URL según backend elegido

# 3. Crear directorios de volúmenes
mkdir -p models data logs forge_data

# 4. Construir imágenes
make build

# 5. Levantar stack
make up

# 6. Verificar estado
make ps
curl http://localhost:18910/health
curl http://localhost:18912/health
```

### 6.2 Comandos Makefile

| Comando             | Descripción                                           |
|---------------------|-------------------------------------------------------|
| `make up`           | Inicia todos los servicios en segundo plano           |
| `make down`         | Detiene todos los servicios                           |
| `make build`        | Reconstruye todas las imágenes (--no-cache)           |
| `make restart svc=predictor` | Reinicia un servicio específico             |
| `make logs`         | Tail de logs de todos los servicios                   |
| `make log svc=forge`| Tail de logs de un servicio específico                |
| `make ps`           | Estado de los contenedores                            |
| `make retrain`      | Ejecuta el pipeline completo de reentrenamiento       |
| `make retrain-auc auc=0.60` | Retrain con AUC mínimo personalizado       |
| `make leaderboard`  | Muestra el leaderboard de Forge en terminal           |
| `make forge-open`   | Posiciones simuladas abiertas actualmente             |
| `make shell svc=predictor` | Shell interactivo en un contenedor           |
| `make clean`        | Elimina contenedores detenidos e imágenes huérfanas   |

### 6.3 Agregar una estrategia nueva a Forge

```bash
curl -X POST http://localhost:18912/strategies \
  -H "Content-Type: application/json" \
  -d '{
    "name":             "btc_long_experimental",
    "symbol":           "BTC/USDT",
    "direction":        "long",
    "entry_threshold":  0.72,
    "tp_atr_mult":      2.2,
    "sl_atr_mult":      0.8,
    "max_candles_held": 3,
    "notes":            "Alta confianza, TP amplio, SL ajustado"
  }'
```

### 6.4 Consultar resultados de Forge

```bash
# Leaderboard general (mínimo 5 trades)
curl "http://localhost:18912/leaderboard?min_trades=5&sort_by=win_rate"

# Resultados de una estrategia específica
curl "http://localhost:18912/results/btc_long_hi_conf"

# Datos raw de BTC con predicciones (últimas 50 velas)
curl "http://localhost:18912/data/BTC%2FUSDT?limit=50"

# Posiciones simuladas actualmente abiertas
curl "http://localhost:18912/open"
```

### 6.5 Activar ejecución real en Executor

**Por defecto el executor está en DRY_RUN=true.** Para activar ejecución real:

1. Editar `.env`:
```env
DRY_RUN=false
EXCHANGE_API_KEY=tu_api_key
EXCHANGE_API_SECRET=tu_api_secret
MIN_LONG_CONF=0.65   # umbral conservador recomendado
POSITION_SIZE_PCT=0.01  # 1% por operación al inicio
```

2. Reiniciar el executor:
```bash
make restart svc=executor
```

3. Verificar en modo real antes de conectar señales:
```bash
curl http://localhost:18911/health
curl http://localhost:18911/positions
```

---

## 7. Variables de Entorno — Referencia Completa

| Variable              | Servicio    | Default                        | Descripción                          |
|-----------------------|-------------|--------------------------------|--------------------------------------|
| `DASHBOARD_PORT`      | dashboard   | `80`                           | Puerto del panel web                 |
| `PREDICTOR_PORT`      | predictor   | `18910`                        | Puerto del servidor predictor        |
| `EXECUTOR_PORT`       | executor    | `18911`                        | Puerto del executor                  |
| `FORGE_PORT`          | forge       | `18912`                        | Puerto del Forge                     |
| `MODELS_DIR`          | predictor   | `./models`                     | Ruta al directorio de modelos        |
| `DATA_DIR`            | retrain     | `./data`                       | Ruta a datos de entrenamiento        |
| `LOGS_DIR`            | todos       | `./logs`                       | Ruta a logs                          |
| `FORGE_DATA_DIR`      | forge       | `./forge_data`                 | Ruta a SQLite de Forge               |
| `SA_INFERENCE_BACKEND`| predictor   | `ollama`                       | `ollama` o `claude`                  |
| `OLLAMA_URL`          | predictor   | `http://host.docker.internal:11434` | Endpoint de Ollama              |
| `OLLAMA_MODEL`        | predictor   | `llama3.1`                     | Modelo Ollama a usar                 |
| `ANTHROPIC_API_KEY`   | predictor   | —                              | Requerido si backend = claude        |
| `EXCHANGE`            | executor    | `bybit`                        | Exchange ccxt                        |
| `EXCHANGE_API_KEY`    | executor    | —                              | API key del exchange                 |
| `EXCHANGE_API_SECRET` | executor    | —                              | API secret del exchange              |
| `DRY_RUN`             | executor    | `true`                         | `false` para órdenes reales          |
| `MIN_LONG_CONF`       | executor    | `0.60`                         | Confianza mínima para long           |
| `MIN_SHORT_CONF`      | executor    | `0.60`                         | Confianza mínima para short          |
| `POSITION_SIZE_PCT`   | executor    | `0.02`                         | Fracción del balance por operación   |
| `MIN_AUC`             | retrain     | `0.54`                         | AUC mínimo para promover modelo      |
| `PREDICTOR_WS_URL`    | forge       | `ws://predictor:18910/ws`      | WebSocket del predictor              |
| `ATR_PERIOD`          | forge       | `14`                           | Período ATR en Forge                 |

---

## 8. Integración con Freqtrade / LGBMStrategy

El sistema Predictor es **complementario** al bot Freqtrade que corre en AI_OS. La estrategia `LGBMStrategy` de Freqtrade carga los mismos modelos LightGBM directamente desde disco.

### Parámetros clave en LGBMStrategy (hardened)

| Parámetro              | Valor | Descripción                                     |
|------------------------|-------|-------------------------------------------------|
| `tp_atr_mult`          | 1.5   | Multiplicador ATR para take profit              |
| `sl_atr_mult`          | 1.5   | Multiplicador ATR para stop loss                |
| `startup_candle_count` | 200   | Velas de calentamiento (corregido off-by-one)   |

### Mejoras aplicadas en esta versión

1. `tp_atr_mult` y `sl_atr_mult` centralizados en `BASELINE` — una sola fuente de verdad.
2. `load_model()` con thread lock, logging de features y verificación de antigüedad (6h mtime).
3. `startup_candle_count` corregido de 199 a 200 y guard para pares distintos a BTC.
4. `custom_stoploss()` con lookup de fecha robusto (ya no falla en fechas fuera de rango).

---

## 9. Consideraciones de Seguridad

- Las API keys del exchange **nunca** van al repositorio. Se leen exclusivamente de `.env` (excluido por `.gitignore` y `.dockerignore`).
- El executor arranca en `DRY_RUN=true` por defecto. Se requiere intervención manual explícita para activar órdenes reales.
- El token PAT de GitHub usado para el push inicial fue eliminado inmediatamente después del push.
- Los modelos `.txt` de LightGBM contienen únicamente pesos del árbol — no datos de clientes ni credenciales.
- `ANTHROPIC_API_KEY` se pasa como variable de entorno, nunca se escribe en código.

---

## 10. Próximos Pasos (Roadmap)

| Prioridad | Tarea                                                                          |
|-----------|--------------------------------------------------------------------------------|
| Alta      | Despliegue en VPS con el agente Hermes ya instalado                            |
| Alta      | Configurar cron semanal para `make retrain` en VPS                             |
| Media     | Panel Forge en el dashboard (tabla leaderboard en tiempo real)                 |
| Media     | GitHub Actions CI (lint + tests en cada push)                                  |
| Media     | Tests unitarios para `features.py` y `labels.py`                               |
| Baja      | Versionado de modelos (archivos con timestamp, rollback automatizado)          |
| Baja      | Webhook Predictor → Freqtrade para override de entrada                         |
| Baja      | Exportación de resultados Forge a CSV / Google Sheets                          |

---

## 11. Referencia Rápida de URLs

| Servicio             | URL                                             |
|----------------------|-------------------------------------------------|
| Dashboard            | http://localhost                                |
| Predictor health     | http://localhost:18910/health                   |
| BTC predicción       | http://localhost:18910/predict/BTC%2FUSDT       |
| BTC señal enriquecida| http://localhost:18910/enriched/BTC%2FUSDT      |
| WebSocket predictor  | ws://localhost:18910/ws                         |
| Executor health      | http://localhost:18911/health                   |
| Executor posiciones  | http://localhost:18911/positions                |
| Forge health         | http://localhost:18912/health                   |
| Forge leaderboard    | http://localhost:18912/leaderboard              |
| Forge trades BTC     | http://localhost:18912/results?symbol=BTC/USDT  |
| Forge datos BTC      | http://localhost:18912/data/BTC%2FUSDT          |

---

*Documento generado por Claude (Cowork mode) — 2026-07-17*  
*Sistema: Antigravity Predictor v2.0 — Docker Compose*
