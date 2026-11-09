import math


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_probability(forecast, t_low, t_high, sigma):
    s = max(float(sigma or 1.0), 0.35)
    f = float(forecast)
    low_bound = t_low - 0.5 if t_low != -999 else -999
    high_bound = t_high + 0.5 if t_high != 999 else 999

    if t_low == -999:
        prob = norm_cdf((high_bound - f) / s)
    elif t_high == 999:
        prob = 1.0 - norm_cdf((low_bound - f) / s)
    else:
        prob = norm_cdf((high_bound - f) / s) - norm_cdf((low_bound - f) / s)

    return max(0.0, min(prob, 0.999))


def calc_ev(probability, price):
    if price <= 0 or price >= 1:
        return 0.0
    return probability * (1.0 / price - 1.0) - (1.0 - probability)


def build_ladder_signals(outcomes, forecast_temp, sigma, unit_symbol):
    if not outcomes:
        return []

    ladder = []
    for index, outcome in enumerate(outcomes):
        t_low, t_high = outcome["range"]
        yes_price = float(outcome.get("ask", outcome.get("price", 0.5)))
        no_price = float(outcome.get("price_no", 1.0 - yes_price))
        model_yes = bucket_probability(forecast_temp, t_low, t_high, sigma)
        model_no = 1.0 - model_yes

        left = outcomes[index - 1] if index > 0 else None
        right = outcomes[index + 1] if index + 1 < len(outcomes) else None

        left_label = "N/A"
        right_label = "N/A"
        if left:
            left_label = f"{left.get('price', 0.0) * 100:.1f}% ({left['range'][0]}-{left['range'][1]}{unit_symbol})"
        if right:
            right_label = f"{right.get('price', 0.0) * 100:.1f}% ({right['range'][0]}-{right['range'][1]}{unit_symbol})"

        yes_edge = model_yes - yes_price
        no_edge = model_no - no_price
        yes_ev = calc_ev(model_yes, yes_price)
        no_ev = calc_ev(model_no, no_price)

        common = {
            "market_id": outcome["market_id"],
            "question": outcome["question"],
            "bucket_low": t_low,
            "bucket_high": t_high,
            "spread": float(outcome.get("spread", 0.0)),
            "prob_below": left_label,
            "prob_above": right_label,
            "market_yes": round(yes_price, 4),
            "market_no": round(no_price, 4),
            "model_yes": round(model_yes, 4),
            "model_no": round(model_no, 4),
        }

        ladder.append({
            **common,
            "side": "YES",
            "entry_price": round(yes_price, 4),
            "p": round(model_yes, 4),
            "edge": round(yes_edge, 4),
            "ev": round(yes_ev, 4),
            "score": round((yes_ev * 100.0) + (max(yes_edge, 0.0) * 25.0), 4),
        })
        ladder.append({
            **common,
            "side": "NO",
            "entry_price": round(no_price, 4),
            "p": round(model_yes, 4),
            "edge": round(no_edge, 4),
            "ev": round(no_ev, 4),
            "score": round((no_ev * 100.0) + (max(no_edge, 0.0) * 25.0), 4),
        })

    ladder.sort(key=lambda item: item["score"], reverse=True)
    return ladder
