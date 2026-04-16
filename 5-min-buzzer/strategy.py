from dataclasses import dataclass


@dataclass
class OrderIntent:
    condition_id: str
    token_id: str
    side: str
    price: float
    size_usd: float
    reason: str


class StinkBid:
    name = "stink_bid"

    def __init__(self, cfg: dict):
        self.discount = float(cfg["discount_pct"])
        self.min_spread = float(cfg["min_spread"])
        self.min_big_bids = int(cfg["min_big_bids"])
        self.bet_size = float(cfg["bet_size_usd"])

    def evaluate(self, market: dict, book: dict) -> OrderIntent | None:
        if book["spread"] < self.min_spread:
            return None
        if book["big_bids"] < self.min_big_bids:
            return None
        best_ask = book["best_ask"]
        if not (0.05 <= best_ask <= 0.98):
            return None
        price = round(best_ask * (1.0 - self.discount), 3)
        if price < 0.02:
            return None
        return OrderIntent(
            condition_id=market["condition_id"],
            token_id=market["yes_token_id"],
            side="BUY",
            price=price,
            size_usd=self.bet_size,
            reason=(
                f"spread={book['spread']:.3f} "
                f"big_bids={book['big_bids']} "
                f"best_ask={best_ask:.3f}"
            ),
        )
