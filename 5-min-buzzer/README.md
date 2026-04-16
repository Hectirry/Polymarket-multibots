# 5-min Buzzer

Paper-trading bot minimalista para mercados de cripto de 5 min en Polymarket
(BTC/ETH up-or-down). Reescritura honesta del stack del repo padre que cortó
whale detection, LLM validator, ranker y el pipeline de 9 etapas — ninguno
mostraba edge real en la DB.

## Dos modos

| Modo | Qué hace | Cuándo |
|---|---|---|
| `observe` | Descubre mercados, snapshotea orderbooks cada 3s a SQLite. No coloca órdenes. | Primero 24–48 h para validar que hay spreads reales |
| `paper` | Mismo loop + estrategia `stink_bid` simulando órdenes (no firma nada) | Después de confirmar edge en los snapshots |

## Archivos

| Archivo | LOC | Qué hace |
|---|---|---|
| `bot.py` | ~140 | Loop principal, modos, órdenes simuladas |
| `market.py` | ~120 | Gamma (discovery) + CLOB `/book` (orderbook) |
| `strategy.py` | ~45  | Stink bid puro |
| `storage.py` | ~110 | `sqlite3` directo, 2 tablas, sin ORM |
| `status.py`  | ~85  | Readout CLI |
| `config.yaml`| ~20  | Thresholds |

Total ≈ 520 LOC (vs 6,455 del repo padre).

## Instalación

```bash
cd 5-min-buzzer
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Reutiliza el `.env` del repo padre automáticamente (`load_env()` busca
primero `./.env`, luego `../.env`). No necesita credenciales: `/book`
de CLOB es endpoint público.

## Uso

```bash
# 1) observar 24–48 h
python bot.py --mode observe

# 2) readout en otra terminal
watch -n 5 python status.py

# 3) cuando haya datos, saltar a paper
python bot.py --mode paper
```

## Estrategia: stink_bid

Coloca bid 30% bajo el `best_ask` cuando:

- `spread >= 0.05` (mercado ancho)
- `>= 2` bids en el top-of-book con notional ≥ $50

La simulación de fill es optimista: si en algún snapshot posterior
`best_ask <= nuestro_bid`, cuenta como filled. Sirve para medir **fill rate**,
no P&L real. Resolución de outcome (win/loss) queda para una segunda iteración.

## Antes de pasar a paper

Corre esto para validar que el edge existe:

```sql
SELECT slug,
       COUNT(*) AS n,
       AVG(spread) AS avg_spread,
       SUM(CASE WHEN spread >= 0.05 THEN 1 ELSE 0 END) AS wide_spread_snaps,
       SUM(CASE WHEN big_bids >= 2 THEN 1 ELSE 0 END) AS stackable
FROM snapshots
GROUP BY slug;
```

Si `wide_spread_snaps` y `stackable` son cercanos a cero, la tesis de
stink bid no aplica — ajusta thresholds o descarta la estrategia antes
de escribir más código.

## Limitaciones conocidas

- **Single account**. Multi-account del prompt original está diferido
  hasta confirmar edge con 1.
- **No firma órdenes reales**. La private key del `.env` padre es en
  realidad una dirección (40 hex, no 64). Hay que regenerarla.
- **Sin notificaciones**. Slack/email diferidos — logger a archivo basta
  mientras el bot no toque dinero real.
- **Sin resolución de outcome**. Tracking de P&L real requiere poll a
  `/markets/{id}` tras cierre; pendiente.

## Por qué esta estructura y no la del prompt original

El prompt pedía `strategies/`, `data/`, `persistence/` separados. Con ~500 LOC
totales, partir en paquetes añade import ceremony sin claridad. Cuando el
código crezca y la estrategia demuestre edge, se reorganiza — no antes.
