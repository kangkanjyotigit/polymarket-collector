
import asyncio
import time
from typing import List, Dict, Any, Optional

import httpx
import pandas as pd
from loguru import logger

# --- Configuration ---
POLYMARKET_API_URL = "https://gamma-api.polymarket.com"
TRADING_FEE = 0.01  # Represents 1% fee on the profit/loss of a trade
HIGH_ROI_ALERT_THRESHOLD = 0.05  # 5% ROI
MIN_LIQUIDITY_USD = 100 # Minimum liquidity in the order book to consider a market

# --- Logger Setup ---
logger.add("polymarket_bot.log", rotation="10 MB", level="INFO", enqueue=True, serialize=True)
logger.add(lambda msg: "HIGH_ROI_ALERT" in msg, level="WARNING", format="{message}")


class PolymarketDataCollector:
    """
    A production-ready module for fetching Polymarket data, detecting arbitrage
    opportunities, and validating market conditions.
    """

    def __init__(self, client: httpx.AsyncClient, usdc_balance: float = 1000.0):
        self.client = client
        self.available_capital = usdc_balance
        logger.info(f"Data Collector initialized with ${usdc_balance} USDC available capital.")

    async def _fetch_data(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Generic, error-resistant data fetching method."""
        start_time = time.time()
        try:
            response = await self.client.get(f"{POLYMARKET_API_URL}/{endpoint}", params=params)
            response.raise_for_status()
            latency = time.time() - start_time
            logger.info(f"Fetched {endpoint} in {latency:.4f}s")
            return response.json()
        except httpx.RequestError as e:
            logger.error(f"HTTP Request error fetching {e.request.url!r}: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP Status error for {e.request.url!r}: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.critical(f"An unexpected error occurred during fetch: {e}")
        return None

    async def get_active_markets(self) -> List[Dict[str, Any]]:
        """Fetches all active, non-resolved markets."""
        data = await self._fetch_data("markets", params={"closed": "false"})
        if data and "data" in data:
            active_markets = [
                m for m in data["data"]
                if not m.get("closed") and m.get("active")
            ]
            logger.info(f"Found {len(active_markets)} active markets.")
            return active_markets
        return []

    async def get_order_book(self, market_slug: str) -> Optional[Dict[str, Any]]:
        """Fetches the order book for a specific market."""
        return await self._fetch_data(f"markets/{market_slug}/orderbook")

    def validate_usdc_balance(self, trade_size: float) -> bool:
        """Validates if there is enough capital for a trade."""
        is_sufficient = self.available_capital >= trade_size
        if not is_sufficient:
            logger.warning(f"Insufficient capital: Required ${trade_size}, Available ${self.available_capital}")
        return is_sufficient

    def _calculate_arbitrage(self, asks: pd.DataFrame, bids: pd.DataFrame, market_info: Dict) -> Optional[Dict]:
        """Analyzes order book data to find arbitrage opportunities."""
        if asks.empty or bids.empty:
            return None
        
        # Ensure 'outcome_idx' exists and is of a consistent type
        if 'outcome_idx' not in asks.columns or 'outcome_idx' not in bids.columns:
            return None
        
        asks['outcome_idx'] = asks['outcome_idx'].astype(int)
        bids['outcome_idx'] = bids['outcome_idx'].astype(int)

        # Get best ask prices (where we can buy) for YES (1) and NO (0)
        best_ask_yes = asks[asks.outcome_idx == 1]['price'].min()
        best_ask_no = asks[asks.outcome_idx == 0]['price'].min()

        # Get best bid prices (where we can sell) for YES (1) and NO (0)
        best_bid_yes = bids[bids.outcome_idx == 1]['price'].max()
        best_bid_no = bids[bids.outcome_idx == 0]['price'].max()

        # LONG ARBITRAGE: Buy YES + Buy NO < $1.00
        if pd.notna(best_ask_yes) and pd.notna(best_ask_no):
            long_cost = best_ask_yes + best_ask_no
            if long_cost < 1.0:
                # Profit is the difference, minus fees on the $1 payout
                roi = (1.0 - long_cost - TRADING_FEE) / long_cost
                return {
                    "type": "LONG_ARB",
                    "roi_pct": roi * 100,
                    "buy_yes_price": best_ask_yes,
                    "buy_no_price": best_ask_no,
                    "total_cost": long_cost
                }

        # SHORT ARBITRAGE: Sell YES + Sell NO > $1.00
        if pd.notna(best_bid_yes) and pd.notna(best_bid_no):
            short_revenue = best_bid_yes + best_bid_no
            if short_revenue > 1.0:
                # Profit is the premium collected, minus fees
                roi = (short_revenue - 1.0 - TRADING_FEE) / 1.0  # ROI is based on the collateral ($1)
                return {
                    "type": "SHORT_ARB",
                    "roi_pct": roi * 100,
                    "sell_yes_price": best_bid_yes,
                    "sell_no_price": best_bid_no,
                    "total_revenue": short_revenue
                }
        return None

    async def process_market(self, market_info: Dict) -> Optional[Dict[str, Any]]:
        """Processes a single market to find data and arbitrage."""
        market_slug = market_info.get("slug")
        if not market_slug:
            return None

        order_book = await self.get_order_book(market_slug)
        if not order_book or "sells" not in order_book or "buys" not in order_book:
            logger.warning(f"No valid order book for market {market_slug}")
            return None
        
        # Convert to DataFrame for easier analysis
        asks_df = pd.DataFrame(order_book["sells"])
        bids_df = pd.DataFrame(order_book["buys"])

        # Data Validation
        if asks_df.empty or bids_df.empty:
            return None # Not a valid binary market for our purpose
            
        asks_df[['price', 'size']] = asks_df[['price', 'size']].astype(float)
        bids_df[['price', 'size']] = bids_df[['price', 'size']].astype(float)

        # Liquidity Check
        total_liquidity = asks_df['size'].sum() + bids_df['size'].sum()
        if total_liquidity < MIN_LIQUIDITY_USD:
            logger.debug(f"Skipping market {market_slug} due to low liquidity (${total_liquidity:.2f})")
            return None

        arbitrage = self._calculate_arbitrage(asks_df, bids_df, market_info)
        if not arbitrage:
            return None

        # Log and Alert
        log_message = f"Arbitrage detected in market '{market_info['question']}' ({market_slug}) | Type: {arbitrage['type']}, ROI: {arbitrage['roi_pct']:.4f}%"
        logger.info(log_message)

        if arbitrage['roi_pct'] > HIGH_ROI_ALERT_THRESHOLD * 100:
            logger.warning(f"HIGH_ROI_ALERT: {log_message}")

        # Construct final JSON output
        return {
            "timestamp": pd.Timestamp.utcnow().isoformat(),
            "market_slug": market_slug,
            "market_question": market_info["question"],
            "market_status": "active" if market_info["active"] and not market_info["resolved"] else "inactive",
            "trading_volume_24h": market_info.get("volume_24h"),
            "spread": (asks_df[asks_df.outcome_idx == 1]['price'].min() - bids_df[bids_df.outcome_idx == 1]['price'].max()) if not asks_df.empty and not bids_df.empty else None,
            "arbitrage_opportunity": arbitrage
        }

    async def find_all_opportunities(self) -> List[Dict[str, Any]]:
        """
        Main method to scan all active markets for arbitrage opportunities,
        rank them, and return as a JSON structure.
        """
        active_markets = await self.get_active_markets()
        if not active_markets:
            logger.warning("No active markets found.")
            return []

        tasks = [self.process_market(market) for market in active_markets]
        results = await asyncio.gather(*tasks)

        # Filter out None results and sort by ROI
        opportunities = [res for res in results if res is not None]
        opportunities.sort(key=lambda x: x['arbitrage_opportunity']['roi_pct'], reverse=True)
        
        if opportunities:
            logger.success(f"Scan complete. Found {len(opportunities)} ranked arbitrage opportunities.")
        else:
            logger.info("Scan complete. No arbitrage opportunities found in this cycle.")

        return opportunities

async def main():
    """Main execution loop for continuous monitoring."""
    async with httpx.AsyncClient() as client:
        collector = PolymarketDataCollector(client, usdc_balance=5000.0)
        
        while True:
            logger.info("--- Starting new arbitrage scan cycle ---")
            opportunities = await collector.find_all_opportunities()
            
            if opportunities:
                # In a real bot, this is where you'd pass opportunities to the execution layer
                print(pd.DataFrame(opportunities).to_json(orient="records", indent=2))
            
            logger.info("--- Scan cycle finished. Waiting 60 seconds... ---")
            await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shutting down manually.")

