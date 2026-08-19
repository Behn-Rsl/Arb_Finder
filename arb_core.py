"""
arb_core -- arbitrage detection maths and odds-feed access.

Pure logic, no GUI imports, no third-party dependencies (stdlib only).
Import this from a UI, a script, or a test suite.

Terminology used consistently throughout:

    price / odds      decimal odds, e.g. 2.50 means stake 1 -> return 2.50
    book_sum (S)      sum of 1/price over every outcome of a market
    margin            1 - S            (the bookmaker's overround, negative = arb)
    roi               1/S - 1          (profit as a fraction of TOTAL STAKE)

Note that margin and roi are NOT the same number. With S = 0.95 the margin is
5.00% but the actual return on stake is 1/0.95 - 1 = 5.263%. Everything the user
sees is quoted as roi, because that is the number that turns into money.
"""

from __future__ import annotations

import csv
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "OddsFeedError",
    "Quote",
    "MarketGroup",
    "Leg",
    "Opportunity",
    "Allocation",
    "american_to_decimal",
    "effective_price",
    "book_sum",
    "roi_from_book_sum",
    "allocate_stakes",
    "parse_events",
    "find_opportunities",
    "OddsAPIClient",
    "demo_payload",
    "opportunities_to_csv",
    "DEFAULT_SPORTS",
    "REGIONS",
    "MARKETS",
]

API_BASE = "https://api.the-odds-api.com/v4"

REGIONS = ("uk", "eu", "us", "us2", "au")
MARKETS = ("h2h", "spreads", "totals")

# Fallback list used when the sport index cannot be fetched. "upcoming" is a
# special key that returns the next games across all sports.
DEFAULT_SPORTS: Tuple[Tuple[str, str], ...] = (
    ("upcoming", "Upcoming (all sports)"),
    ("soccer_epl", "Soccer - EPL"),
    ("soccer_uefa_champs_league", "Soccer - UEFA Champions League"),
    ("soccer_spain_la_liga", "Soccer - La Liga"),
    ("soccer_italy_serie_a", "Soccer - Serie A"),
    ("soccer_germany_bundesliga", "Soccer - Bundesliga"),
    ("basketball_nba", "Basketball - NBA"),
    ("americanfootball_nfl", "American Football - NFL"),
    ("baseball_mlb", "Baseball - MLB"),
    ("icehockey_nhl", "Ice Hockey - NHL"),
    ("mma_mixed_martial_arts", "MMA"),
)

_EPS = 1e-12


class OddsFeedError(RuntimeError):
    """Raised when an odds feed cannot be read or returns an error."""


# --------------------------------------------------------------------------
# Odds arithmetic
# --------------------------------------------------------------------------

def american_to_decimal(american: float) -> float:
    """Convert American (moneyline) odds to decimal odds."""
    a = float(american)
    if a == 0:
        raise ValueError("American odds cannot be 0")
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def effective_price(price: float, commission: float = 0.0) -> float:
    """
    Decimal odds adjusted for commission charged on net winnings.

    Exchanges (Betfair, Smarkets, Matchbook) take a cut of the profit, not the
    stake, so 3.00 at 5% commission is worth 1 + 2.00 * 0.95 = 2.90.
    """
    p = float(price)
    c = float(commission)
    if p <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {p}")
    if not 0.0 <= c < 1.0:
        raise ValueError(f"Commission must be in [0, 1), got {c}")
    return 1.0 + (p - 1.0) * (1.0 - c)


def book_sum(prices: Sequence[float]) -> float:
    """Sum of implied probabilities. Below 1.0 means an arbitrage exists."""
    if len(prices) < 2:
        raise ValueError("A market needs at least two outcomes")
    total = 0.0
    for p in prices:
        if p <= 1.0:
            raise ValueError(f"Decimal odds must be > 1.0, got {p}")
        total += 1.0 / float(p)
    return total


def roi_from_book_sum(s: float) -> float:
    """Return on total stake implied by a book sum."""
    if s <= 0:
        raise ValueError("Book sum must be positive")
    return 1.0 / s - 1.0


@dataclass
class Allocation:
    """A concrete, rounded set of stakes and what they actually pay."""

    stakes: List[float]
    returns: List[float]
    total_staked: float
    worst_return: float
    best_return: float
    profit: float          # guaranteed profit = worst_return - total_staked
    roi: float             # profit / total_staked


def allocate_stakes(
    prices: Sequence[float],
    total_stake: float,
    increment: float = 0.01,
) -> Allocation:
    """
    Split ``total_stake`` across outcomes so every result pays the same.

    Stakes are whole multiples of ``increment`` (0.01 for pennies, 1.0 where a
    book only accepts whole pounds), and the split maximises the worst-case
    return -- the number that actually matters, since that is what a guaranteed
    profit is made of.

    Rounding to the nearest ideal stake is not good enough here: with a coarse
    increment it can leave one leg short and quietly turn a real edge into a
    loss. Instead this bisects on the target return T, using the fact that
    covering a return of T on a leg priced p needs ceil(T / (increment * p))
    units, so T is affordable exactly when those unit counts fit the budget.
    That finds the true optimum, verified against exhaustive search.

    The figures returned are the real post-rounding outcome, so an edge that
    rounding has eaten shows up as a negative ``profit`` instead of being
    reported as a win.
    """
    n = len(prices)
    if n < 2:
        raise ValueError("A market needs at least two outcomes")
    for p in prices:
        if p <= 1.0:
            raise ValueError(f"Decimal odds must be > 1.0, got {p}")
    if increment <= 0:
        raise ValueError("Increment must be positive")
    if total_stake <= 0:
        raise ValueError("Total stake must be positive")

    total_units = int(round(total_stake / increment))
    if total_units < n:
        raise ValueError(
            "Total stake is too small for the chosen rounding increment: "
            f"{total_stake} / {increment} leaves fewer units than outcomes"
        )

    # Return produced by one unit on each leg.
    step = [increment * p for p in prices]

    # lo is always affordable (one unit per leg, and total_units >= n).
    # hi is the continuous optimum, which integer stakes can never beat.
    lo = min(step)
    hi = total_units * increment / book_sum(prices)
    for _ in range(200):
        mid = (lo + hi) / 2.0
        needed = sum(math.ceil(mid / st - 1e-12) for st in step)
        if needed <= total_units:
            lo = mid
        else:
            hi = mid

    units = [max(1, math.ceil(lo / st - 1e-12)) for st in step]
    while sum(units) > total_units:  # guard against float drift at the boundary
        i = max(range(n), key=lambda j: units[j] * step[j] if units[j] > 1 else -1.0)
        units[i] -= 1
    for _ in range(total_units - sum(units)):
        # Spare units go to whichever leg currently returns least.
        i = min(range(n), key=lambda j: units[j] * step[j])
        units[i] += 1

    stakes = [round(u * increment, 10) for u in units]
    returns = [round(u * increment * p, 10) for u, p in zip(units, prices)]
    total_staked = round(sum(stakes), 10)
    worst = min(returns)
    best = max(returns)
    profit = round(worst - total_staked, 10)
    return Allocation(
        stakes=stakes,
        returns=returns,
        total_staked=total_staked,
        worst_return=worst,
        best_return=best,
        profit=profit,
        roi=profit / total_staked if total_staked else 0.0,
    )


# --------------------------------------------------------------------------
# Feed data model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    bookmaker_key: str
    bookmaker_title: str
    outcome: str
    price: float
    point: Optional[float] = None
    last_update: Optional[datetime] = None


@dataclass
class MarketGroup:
    """Every quote for one event + one market + one line."""

    event_id: str
    sport_key: str
    sport_title: str
    commence_time: Optional[datetime]
    home_team: str
    away_team: str
    market_key: str
    line: Optional[float]
    quotes: List[Quote] = field(default_factory=list)

    @property
    def event_name(self) -> str:
        if self.home_team and self.away_team:
            return f"{self.home_team} v {self.away_team}"
        return self.home_team or self.away_team or self.event_id


@dataclass
class Leg:
    outcome: str
    bookmaker_key: str
    bookmaker_title: str
    price: float            # advertised decimal odds
    net_price: float        # after commission
    commission: float
    stake: float
    payout: float
    last_update: Optional[datetime] = None


@dataclass
class Opportunity:
    event_id: str
    sport_key: str
    sport_title: str
    event_name: str
    commence_time: Optional[datetime]
    market_key: str
    line: Optional[float]
    legs: List[Leg]
    book_sum: float
    theoretical_roi: float   # before stake rounding
    total_stake: float
    guaranteed_profit: float
    roi: float               # after stake rounding
    warnings: List[str] = field(default_factory=list)

    @property
    def bookmakers(self) -> List[str]:
        seen: List[str] = []
        for leg in self.legs:
            if leg.bookmaker_title not in seen:
                seen.append(leg.bookmaker_title)
        return seen

    @property
    def line_label(self) -> str:
        if self.line is None:
            return "-"
        return f"{self.line:+g}" if self.market_key == "spreads" else f"{self.line:g}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _line_for_market(market_key: str, outcomes: Sequence[dict], home_team: str) -> Optional[float]:
    """
    Identify which line a market snapshot belongs to.

    totals: both outcomes carry the same point, so use it directly.
    spreads: the two sides carry mirrored points (home -2.5 / away +2.5), so the
             line is defined as the HOME team's handicap. Without this
             normalisation the two halves of the same market would never group.
    h2h: no line.
    """
    if market_key == "totals":
        for o in outcomes:
            if o.get("point") is not None:
                return float(o["point"])
        return None
    if market_key == "spreads":
        for o in outcomes:
            if o.get("name") == home_team and o.get("point") is not None:
                return float(o["point"])
        for o in outcomes:
            if o.get("point") is not None:
                return -float(o["point"])
        return None
    return None


def parse_events(
    payload: Iterable[dict],
    markets_wanted: Optional[Sequence[str]] = None,
) -> List[MarketGroup]:
    """Turn a v4 /odds response into MarketGroup objects."""
    wanted = set(markets_wanted) if markets_wanted else None
    groups: Dict[Tuple[str, str, Optional[float]], MarketGroup] = {}

    for event in payload or []:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id", ""))
        home = event.get("home_team") or ""
        away = event.get("away_team") or ""
        commence = _parse_iso(event.get("commence_time"))
        sport_key = event.get("sport_key", "")
        sport_title = event.get("sport_title", sport_key)

        for book in event.get("bookmakers") or []:
            bk_key = book.get("key", "")
            bk_title = book.get("title") or bk_key
            book_update = _parse_iso(book.get("last_update"))

            for market in book.get("markets") or []:
                m_key = market.get("key", "")
                if wanted is not None and m_key not in wanted:
                    continue
                outcomes = [o for o in (market.get("outcomes") or []) if isinstance(o, dict)]
                if len(outcomes) < 2:
                    continue
                m_update = _parse_iso(market.get("last_update")) or book_update
                line = _line_for_market(m_key, outcomes, home)

                gkey = (event_id, m_key, line)
                group = groups.get(gkey)
                if group is None:
                    group = MarketGroup(
                        event_id=event_id,
                        sport_key=sport_key,
                        sport_title=sport_title,
                        commence_time=commence,
                        home_team=home,
                        away_team=away,
                        market_key=m_key,
                        line=line,
                    )
                    groups[gkey] = group

                for o in outcomes:
                    name = o.get("name")
                    price = o.get("price")
                    if name is None or price is None:
                        continue
                    try:
                        price = float(price)
                    except (TypeError, ValueError):
                        continue
                    if price <= 1.0 or not math.isfinite(price):
                        continue
                    point = o.get("point")
                    group.quotes.append(
                        Quote(
                            bookmaker_key=bk_key,
                            bookmaker_title=bk_title,
                            outcome=str(name),
                            price=price,
                            point=float(point) if point is not None else None,
                            last_update=m_update,
                        )
                    )

    return list(groups.values())


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def find_opportunities(
    groups: Iterable[MarketGroup],
    total_stake: float = 100.0,
    min_roi: float = 0.0,
    increment: float = 0.01,
    commissions: Optional[Dict[str, float]] = None,
    max_age_minutes: Optional[float] = None,
    now: Optional[datetime] = None,
    require_full_coverage: bool = True,
    require_distinct_books: bool = True,
    suspicious_roi: float = 0.20,
) -> List[Opportunity]:
    """
    Scan market groups and return every arbitrage, best first.

    require_full_coverage
        Only use a bookmaker's prices for a market if that bookmaker quotes
        every outcome of it. This is the guard against the classic false
        positive: a two-way "draw no bet" price sitting in a three-way soccer
        market looks like free money and is not.
    require_distinct_books
        Drop "arbs" whose legs all sit with one bookmaker -- those are pricing
        errors that get voided, not opportunities.
    """
    commissions = {k.lower(): v for k, v in (commissions or {}).items()}
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max_age_minutes) if max_age_minutes else None
    results: List[Opportunity] = []

    for group in groups:
        quotes = group.quotes
        if cutoff is not None:
            quotes = [q for q in quotes if q.last_update is None or q.last_update >= cutoff]
        if not quotes:
            continue

        # book -> outcome -> quote (keep the best price if a book repeats one)
        by_book: Dict[str, Dict[str, Quote]] = {}
        for q in quotes:
            slot = by_book.setdefault(q.bookmaker_key, {})
            if q.outcome not in slot or q.price > slot[q.outcome].price:
                slot[q.outcome] = q

        canonical = set()
        for slot in by_book.values():
            canonical |= set(slot)
        if len(canonical) < 2:
            continue

        if require_full_coverage:
            by_book = {b: s for b, s in by_book.items() if canonical <= set(s)}
            if not by_book:
                continue

        best: Dict[str, Tuple[Quote, float]] = {}
        for bk, slot in by_book.items():
            comm = commissions.get(bk.lower(), 0.0)
            for name, q in slot.items():
                try:
                    net = effective_price(q.price, comm)
                except ValueError:
                    continue
                if name not in best or net > best[name][1]:
                    best[name] = (q, net)

        if set(best) != canonical or len(best) < 2:
            continue

        names = sorted(best)
        net_prices = [best[n][1] for n in names]
        try:
            s = book_sum(net_prices)
        except ValueError:
            continue
        if s >= 1.0 - _EPS:
            continue

        chosen = [best[n][0] for n in names]
        if require_distinct_books and len({q.bookmaker_key for q in chosen}) < 2:
            continue

        theoretical_roi = roi_from_book_sum(s)
        try:
            alloc = allocate_stakes(net_prices, total_stake, increment)
        except ValueError:
            continue
        if alloc.roi < min_roi:
            continue

        legs = []
        for name, stake, payout in zip(names, alloc.stakes, alloc.returns):
            q, net = best[name]
            comm = commissions.get(q.bookmaker_key.lower(), 0.0)
            legs.append(
                Leg(
                    outcome=name,
                    bookmaker_key=q.bookmaker_key,
                    bookmaker_title=q.bookmaker_title,
                    price=q.price,
                    net_price=net,
                    commission=comm,
                    stake=stake,
                    payout=payout,
                    last_update=q.last_update,
                )
            )

        warnings: List[str] = []
        if theoretical_roi >= suspicious_roi:
            warnings.append("Unusually large edge - check for a stale or mistyped price")
        if group.commence_time and group.commence_time <= now:
            warnings.append("Event has already started")
        stamps = [leg.last_update for leg in legs if leg.last_update]
        if stamps:
            age = (now - min(stamps)).total_seconds() / 60.0
            if age > 10:
                warnings.append(f"Oldest price is {age:.0f} min old")

        results.append(
            Opportunity(
                event_id=group.event_id,
                sport_key=group.sport_key,
                sport_title=group.sport_title,
                event_name=group.event_name,
                commence_time=group.commence_time,
                market_key=group.market_key,
                line=group.line,
                legs=legs,
                book_sum=s,
                theoretical_roi=theoretical_roi,
                total_stake=alloc.total_staked,
                guaranteed_profit=alloc.profit,
                roi=alloc.roi,
                warnings=warnings,
            )
        )

    results.sort(key=lambda o: o.roi, reverse=True)
    return results


# --------------------------------------------------------------------------
# The Odds API client
# --------------------------------------------------------------------------

class OddsAPIClient:
    """
    Minimal client for the-odds-api.com v4 (stdlib only).

    Endpoints used:
        GET /v4/sports
        GET /v4/sports/{sport}/odds
    """

    def __init__(self, api_key: str, base_url: str = API_BASE, timeout: float = 20.0):
        if not api_key or not api_key.strip():
            raise OddsFeedError("An API key is required. Get a free one at the-odds-api.com.")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests_remaining: Optional[str] = None
        self.requests_used: Optional[str] = None

    def _get(self, path: str, params: Dict[str, str]):
        query = dict(params)
        query["apiKey"] = self.api_key
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                self.requests_remaining = response.headers.get("x-requests-remaining")
                self.requests_used = response.headers.get("x-requests-used")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = exc.read().decode("utf-8", "replace")
                parsed = json.loads(body)
                detail = parsed.get("message") or parsed.get("error_code") or body
            except Exception:
                detail = exc.reason or ""
            if exc.code == 401:
                raise OddsFeedError(f"The API key was rejected (401). {detail}") from exc
            if exc.code == 422:
                raise OddsFeedError(f"The feed rejected these parameters (422). {detail}") from exc
            if exc.code == 429:
                raise OddsFeedError(f"Request quota exhausted (429). {detail}") from exc
            raise OddsFeedError(f"Feed returned HTTP {exc.code}. {detail}") from exc
        except urllib.error.URLError as exc:
            raise OddsFeedError(f"Could not reach the feed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OddsFeedError("The feed timed out.") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OddsFeedError("The feed returned something that is not JSON.") from exc

    def list_sports(self, include_inactive: bool = False) -> List[Tuple[str, str]]:
        data = self._get("/sports", {"all": "true"} if include_inactive else {})
        out = [("upcoming", "Upcoming (all sports)")]
        for item in data or []:
            key = item.get("key")
            if not key:
                continue
            group = item.get("group", "")
            title = item.get("title", key)
            out.append((key, f"{group} - {title}" if group else title))
        return out

    def fetch_odds(
        self,
        sport_key: str,
        regions: Sequence[str],
        markets: Sequence[str],
        bookmakers: Optional[Sequence[str]] = None,
    ) -> List[dict]:
        if not regions and not bookmakers:
            raise OddsFeedError("Pick at least one region.")
        if not markets:
            raise OddsFeedError("Pick at least one market.")
        params = {
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        else:
            params["regions"] = ",".join(regions)
        data = self._get(f"/sports/{urllib.parse.quote(sport_key)}/odds", params)
        return data if isinstance(data, list) else []


# --------------------------------------------------------------------------
# Demo feed
# --------------------------------------------------------------------------

def demo_payload(now: Optional[datetime] = None) -> List[dict]:
    """
    A hand-built response in exactly the shape the live feed returns, so the
    same parsing path is exercised offline. Contains one three-way soccer arb,
    one two-way totals arb, one ordinary no-arb market, and one draw-no-bet
    trap that must not be reported.
    """
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def book(key, title, market, outcomes):
        return {
            "key": key,
            "title": title,
            "last_update": stamp,
            "markets": [{"key": market, "last_update": stamp, "outcomes": outcomes}],
        }

    kick = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = (now + timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return [
        {
            "id": "demo-soccer-1",
            "sport_key": "soccer_epl",
            "sport_title": "EPL",
            "commence_time": kick,
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "bookmakers": [
                book("book_a", "Alpha Bet", "h2h", [
                    {"name": "Arsenal", "price": 2.15},
                    {"name": "Chelsea", "price": 3.50},
                    {"name": "Draw", "price": 3.45},
                ]),
                book("book_b", "Beta Books", "h2h", [
                    {"name": "Arsenal", "price": 2.05},
                    {"name": "Chelsea", "price": 3.85},
                    {"name": "Draw", "price": 3.40},
                ]),
                book("book_c", "Gamma Sports", "h2h", [
                    {"name": "Arsenal", "price": 2.25},
                    {"name": "Chelsea", "price": 3.55},
                    {"name": "Draw", "price": 3.60},
                ]),
                # A two-way market on a three-way event: no draw priced, so
                # each side is likelier and the odds look temptingly long.
                # Coverage filtering must exclude it, or it fakes a 15% edge
                # that pays nothing when the game is drawn.
                book("book_2way", "Delta (2-way market)", "h2h", [
                    {"name": "Arsenal", "price": 1.95},
                    {"name": "Chelsea", "price": 4.20},
                ]),
            ],
        },
        {
            "id": "demo-basket-1",
            "sport_key": "basketball_nba",
            "sport_title": "NBA",
            "commence_time": later,
            "home_team": "Boston Celtics",
            "away_team": "Denver Nuggets",
            "bookmakers": [
                book("book_a", "Alpha Bet", "totals", [
                    {"name": "Over", "price": 2.08, "point": 224.5},
                    {"name": "Under", "price": 1.80, "point": 224.5},
                ]),
                book("book_b", "Beta Books", "totals", [
                    {"name": "Over", "price": 1.83, "point": 224.5},
                    {"name": "Under", "price": 2.06, "point": 224.5},
                ]),
                book("book_c", "Gamma Sports", "totals", [
                    {"name": "Over", "price": 1.95, "point": 226.5},
                    {"name": "Under", "price": 1.92, "point": 226.5},
                ]),
            ],
        },
        {
            "id": "demo-tennis-1",
            "sport_key": "tennis_atp",
            "sport_title": "ATP",
            "commence_time": later,
            "home_team": "J. Sinner",
            "away_team": "C. Alcaraz",
            "bookmakers": [
                book("book_a", "Alpha Bet", "h2h", [
                    {"name": "J. Sinner", "price": 1.90},
                    {"name": "C. Alcaraz", "price": 1.90},
                ]),
                book("book_b", "Beta Books", "h2h", [
                    {"name": "J. Sinner", "price": 1.87},
                    {"name": "C. Alcaraz", "price": 1.95},
                ]),
            ],
        },
        {
            "id": "demo-nfl-1",
            "sport_key": "americanfootball_nfl",
            "sport_title": "NFL",
            "commence_time": later,
            "home_team": "Kansas City Chiefs",
            "away_team": "Buffalo Bills",
            "bookmakers": [
                book("book_a", "Alpha Bet", "spreads", [
                    {"name": "Kansas City Chiefs", "price": 2.05, "point": -2.5},
                    {"name": "Buffalo Bills", "price": 1.80, "point": 2.5},
                ]),
                book("book_b", "Beta Books", "spreads", [
                    {"name": "Kansas City Chiefs", "price": 1.83, "point": -2.5},
                    {"name": "Buffalo Bills", "price": 2.04, "point": 2.5},
                ]),
            ],
        },
    ]


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def opportunities_to_csv(opportunities: Sequence[Opportunity], path: str) -> None:
    """Write one row per leg, so the file is ready to paste into a bet log."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "sport", "event", "starts", "market", "line", "roi_pct",
            "total_stake", "guaranteed_profit", "outcome", "bookmaker",
            "odds", "odds_after_commission", "stake", "payout", "warnings",
        ])
        for opp in opportunities:
            starts = opp.commence_time.isoformat() if opp.commence_time else ""
            for leg in opp.legs:
                writer.writerow([
                    opp.sport_title,
                    opp.event_name,
                    starts,
                    opp.market_key,
                    opp.line_label,
                    f"{opp.roi * 100:.3f}",
                    f"{opp.total_stake:.2f}",
                    f"{opp.guaranteed_profit:.2f}",
                    leg.outcome,
                    leg.bookmaker_title,
                    f"{leg.price:.3f}",
                    f"{leg.net_price:.3f}",
                    f"{leg.stake:.2f}",
                    f"{leg.payout:.2f}",
                    "; ".join(opp.warnings),
                ])
