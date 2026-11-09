# Weather market bot for Polymarket

A bot for Polymarket's daily temperature markets ("Highest temperature in London on …"). It turns weather forecasts into a probability for every temperature bucket, compares that with market prices, and sizes positions with fractional Kelly.

**Paper mode by default.** Live orders on the Polymarket CLOB run only when `WEATHER_DRY_RUN=false` and a key is set in `.env`.

## How it works

1. **Five-model ensemble.** Daily max/min forecasts from ECMWF, GFS (HRRR over the US), ICON, JMA and UKMO via [Open-Meteo](https://open-meteo.com), blended with region weights: GFS and ECMWF lead in the US, ECMWF and ICON in Europe, JMA and ECMWF in Asia.
2. **Per-city calibration.** After markets resolve, the bot compares forecasts with the official station reading and updates each city's bias and error (sigma). Forecasts are bias-corrected before pricing.
3. **Bucket pricing.** Each bucket's probability comes from a normal distribution around the corrected forecast, `P(low ≤ T ≤ high)` with the market's rounding rules (`ladder_engine.py`).
4. **Live observations.** On the resolution day it pulls METAR readings from the airport station the market resolves on, plus an optional Tomorrow.io nowcast, so it doesn't bet on buckets the day has already ruled out.
5. **Trade filter and sizing.** Expected value against the ask, minimum edge, price, volume, spread and time-to-resolution limits, per-city risk, then fractional-Kelly sizing with a max bet.
6. **Monitoring.** It checks open positions every minute against per-city stop-losses, resolves markets against actual temperatures, and sends Telegram alerts.

It covers 50 cities across the US, Europe, Asia and other regions, each mapped to the exact weather station Polymarket uses for resolution.

## Files

| File | What it does |
|---|---|
| `bot.py` | Main loop: forecasts, calibration, pricing, filters, paper/live execution, monitoring, reports |
| `ladder_engine.py` | Bucket probabilities and EV across a market's whole temperature ladder |
| `ml_model.py` | Historical baseline per city and date, blended with the forecast |
| `download_history.py`, `update_history.py` | Build and refresh the historical temperature dataset used by `ml_model.py` |
| `dashboard.py`, `dashboard.html` | Local web dashboard with positions and PnL |

## Run

```bash
pip install -r requirements.txt
cp .env.example .env          # paper mode by default
python bot.py                 # main loop
python bot.py status          # balance and open positions
python bot.py report          # full report
python dashboard.py           # local dashboard
```

Some log and Telegram messages are in Russian.

## Credits

This started from an open-source paper-trading weather bot for Polymarket: ECMWF/HRRR/METAR forecasts, per-city sigma calibration, bucket probabilities, Kelly sizing and stop checks. My additions: the five-model regional ensemble, per-city bias correction, intraday METAR extremes and Tomorrow.io nowcast checks, per-city risk profiles, the historical baseline model, bucket-ladder pricing, live CLOB execution, Telegram alerts and the dashboard.

Built by [Denis Bobrik](https://www.linkedin.com/in/denis-bobrik/).
