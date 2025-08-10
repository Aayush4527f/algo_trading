# engine.py
# This is the main file that runs the automated trading engine.
# It orchestrates the data fetching, analysis, and portfolio management.
# VERSION 2: Includes the Expiry Day Straddle Strategy with Session IV Rank.

import configparser
import logging
import time
import traceback
from datetime import datetime, time as dt_time
import os

import pandas as pd

# --- Import Our Custom Modules ---
from api import AngelOneClient
from portfolio_manager import PortfolioManager
from pricing_model import black_scholes

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [ENGINE] - %(message)s',
    handlers=[
        logging.FileHandler("engine.log"),
        logging.StreamHandler()
    ]
)

class TradingEngine:
    """
    The main class that orchestrates the trading strategy.
    """
    def __init__(self, config_path='config.ini'):
        logging.info("🚀 Starting Trading Engine v2...")
        self.config = self._load_config(config_path)
        
        # --- Load General Settings ---
        self.symbols_to_watch = self.config['TRADING_ENGINE']['SYMBOLS_TO_WATCH'].split(',')
        self.run_interval_seconds = int(self.config['TRADING_ENGINE']['RUN_INTERVAL_SECONDS'])
        self.trade_trigger_percentage = float(self.config['TRADING_ENGINE']['TRADE_TRIGGER_PERCENTAGE'])
        self.risk_free_rate = float(self.config['TRADING_ENGINE']['RISK_FREE_RATE'])
        self.trade_quantity = int(self.config['TRADING_ENGINE']['TRADE_QUANTITY'])

        # --- Load Expiry Strategy Settings ---
        self.expiry_strategy_enabled = self.config.getboolean('EXPIRY_STRATEGY', 'ENABLED', fallback=False)
        self.max_iv_rank = self.config.getfloat('EXPIRY_STRATEGY', 'MAX_STRADDLE_IV_RANK', fallback=20.0)
        self.expiry_weekday = self.config.getint('EXPIRY_STRATEGY', 'EXPIRY_WEEKDAY', fallback=3) # 3=Thursday
        self.strategy_start_time = dt_time.fromisoformat(self.config.get('EXPIRY_STRATEGY', 'STRATEGY_START_TIME', fallback='14:55:00'))

        # --- Session IV Rank Tracker ---
        # This dictionary will hold the highest and lowest IV seen today for each symbol.
        self.session_iv_tracker = {}
        # This dictionary prevents the expiry trade from firing more than once per day.
        self.expiry_trade_fired_today = {}

        self.symbol_details = {
            "NIFTY": {"token": "99926000", "exchange": "NSE"},
            "BANKNIFTY": {"token": "99926009", "exchange": "NSE"},
            "FINNIFTY": {"token": "99926037", "exchange": "NSE"},
            "SENSEX": {"token": "99919000", "exchange": "BSE"},
            "MIDCPNIFTY": {"token": "99926074", "exchange": "NSE"},
        }

        self.api_client = AngelOneClient(config_path)
        self.portfolio_manager = PortfolioManager()
        logging.info("Engine initialized successfully.")

    def _load_config(self, config_path):
        parser = configparser.ConfigParser()
        if not parser.read(config_path):
            raise ValueError(f"Configuration file not found at {config_path}")
        return parser

    def run(self):
        try:
            while True:
                logging.info(f"{'='*20} Starting New Trading Cycle {'='*20}")
                
                for index_name in self.symbols_to_watch:
                    if index_name not in self.symbol_details:
                        logging.warning(f"Symbol '{index_name}' from config is not defined in symbol_details. Skipping.")
                        continue
                    
                    self.process_index(index_name)
                    time.sleep(self.api_client.REQUEST_INTERVAL_SECONDS)

                logging.info(f"Cycle finished. Waiting for {self.run_interval_seconds} seconds...")
                time.sleep(self.run_interval_seconds)

        except KeyboardInterrupt:
            logging.info("Keyboard interrupt detected. Shutting down engine.")
        except Exception as e:
            logging.error(f"A critical error occurred in the main loop: {e}")
            logging.error(traceback.format_exc())
        finally:
            self.shutdown()

    def process_index(self, index_name):
        logging.info(f"--- Processing Index: {index_name} ---")
        details = self.symbol_details[index_name]

        underlying_ltp = self.api_client.get_live_equity_data(details['exchange'], details['token'])
        if underlying_ltp is None:
            logging.error(f"Could not get LTP for {index_name}. Skipping.")
            return

        option_chain = self.api_client.get_option_chain(index_name, underlying_ltp)
        if not option_chain:
            logging.warning(f"Could not get option chain for {index_name}. Skipping.")
            return
        
        target_expiry_str = option_chain[0]['expiry']
        greeks_data = self.api_client.get_option_greeks(index_name, target_expiry_str)
        if not greeks_data:
            logging.warning(f"Could not get greeks for {index_name}. Skipping.")
            return

        df_chain = pd.DataFrame(option_chain)
        df_greeks = pd.DataFrame(greeks_data)
        df_merged = pd.merge(df_chain, df_greeks, on='token', how='inner')
        logging.info(f"Successfully merged {len(df_merged)} options with their greeks.")

        # --- NEW: Update the IV tracker for the session ---
        self.update_session_iv(index_name, df_merged, underlying_ltp)

        # --- Run the two strategies ---
        # 1. The original "value" strategy
        for _, option in df_merged.iterrows():
            self.analyze_and_trade_value(option, underlying_ltp)
        
        # 2. The new expiry day straddle strategy
        if self.expiry_strategy_enabled:
            self.execute_expiry_straddle_strategy(index_name, df_merged, underlying_ltp)

    def update_session_iv(self, index_name, df_merged, underlying_ltp):
        """Tracks the highest and lowest IV for the ATM option during the session."""
        try:
            # Find the ATM strike to get its IV
            df_merged['strike_price'] = df_merged['strike'].astype(float) / 100.0
            atm_strike = df_merged.iloc[(df_merged['strike_price'] - underlying_ltp).abs().argsort()[:1]]
            
            if atm_strike.empty:
                return

            current_iv = atm_strike.iloc[0]['iv']

            # Initialize tracker for the symbol if it's the first time seeing it
            if index_name not in self.session_iv_tracker:
                self.session_iv_tracker[index_name] = {'high': current_iv, 'low': current_iv}
                logging.info(f"Initialized IV tracker for {index_name}: High={current_iv}, Low={current_iv}")
            else:
                # Update high and low
                self.session_iv_tracker[index_name]['high'] = max(self.session_iv_tracker[index_name]['high'], current_iv)
                self.session_iv_tracker[index_name]['low'] = min(self.session_iv_tracker[index_name]['low'], current_iv)
            
            logging.debug(f"IV Tracker for {index_name}: High={self.session_iv_tracker[index_name]['high']}, Low={self.session_iv_tracker[index_name]['low']}")

        except Exception as e:
            logging.error(f"Error updating session IV for {index_name}: {e}")

    def execute_expiry_straddle_strategy(self, index_name, df_merged, underlying_ltp):
        """
        Checks conditions for and executes the end-of-day straddle strategy
        using Session IV Rank.
        """
        # --- 1. Condition Checks ---
        now = datetime.now()
        # Check if the trade has already been fired today for this index
        if self.expiry_trade_fired_today.get(index_name) == now.date():
            return
        # Check if it's the right day and time
        if now.weekday() != self.expiry_weekday or now.time() < self.strategy_start_time:
            return

        logging.info(f"*** ACTIVATING EXPIRY STRADDLE STRATEGY FOR {index_name} ***")
        
        try:
            # --- 2. Calculate Session IV Rank ---
            tracker = self.session_iv_tracker.get(index_name)
            if not tracker:
                logging.warning(f"No IV tracker data for {index_name} to calculate rank. Skipping.")
                return

            iv_high = tracker['high']
            iv_low = tracker['low']
            
            df_merged['strike_price'] = df_merged['strike'].astype(float) / 100.0
            atm_option_row = df_merged.iloc[(df_merged['strike_price'] - underlying_ltp).abs().argsort()[:1]]
            current_iv = atm_option_row.iloc[0]['iv']

            iv_range = iv_high - iv_low
            if iv_range == 0:
                session_iv_rank = 50.0 # Avoid division by zero, assume neutral rank
            else:
                session_iv_rank = ((current_iv - iv_low) / iv_range) * 100

            logging.info(f"Current IV: {current_iv:.2f}, Day's Range: [{iv_low:.2f} - {iv_high:.2f}], Session IV Rank: {session_iv_rank:.2f}%")

            # --- 3. The Trade Decision ---
            if session_iv_rank < self.max_iv_rank:
                logging.info(f"SUCCESS: IV Rank ({session_iv_rank:.2f}%) is below threshold ({self.max_iv_rank}%)! EXECUTING STRADDLE.")
                
                # Find the ATM call and put to trade
                atm_strike_price = atm_option_row.iloc[0]['strike_price']
                
                atm_call = df_merged[(df_merged['strike_price'] == atm_strike_price) & (df_merged['symbol'].str.endswith('CE'))].iloc[0]
                atm_put = df_merged[(df_merged['strike_price'] == atm_strike_price) & (df_merged['symbol'].str.endswith('PE'))].iloc[0]

                # --- 4. Execute Virtual Trades ---
                reason = f"Expiry Straddle: IV Rank {session_iv_rank:.2f}% < {self.max_iv_rank}%"
                self.portfolio_manager.record_trade(atm_call['symbol'], "BUY", self.trade_quantity, atm_call['ltp_y'], reason)
                self.portfolio_manager.record_trade(atm_put['symbol'], "BUY", self.trade_quantity, atm_put['ltp_y'], reason)

                # Mark the trade as done for today to prevent re-firing
                self.expiry_trade_fired_today[index_name] = now.date()
            else:
                logging.info(f"IV Rank ({session_iv_rank:.2f}%) is NOT below threshold ({self.max_iv_rank}%). No trade.")

        except Exception as e:
            logging.error(f"Error during expiry straddle strategy for {index_name}: {e}")
            logging.error(traceback.format_exc())

    def analyze_and_trade_value(self, option_data, underlying_ltp):
        """
        Analyzes a single option for the simple value strategy.
        """
        try:
            symbol = option_data['symbol']
            option_type = option_data['symbol'][-2:]
            market_price = option_data['ltp_y']
            strike_price = float(option_data['strike']) / 100.0
            implied_vol = option_data['iv'] / 100.0
            
            if market_price <= 0 or implied_vol <= 0: return # Can't analyze without valid data

            expiry_date_obj = datetime.strptime(option_data['expiry'], '%d%b%Y').date()

            fair_value = black_scholes(option_type, underlying_ltp, strike_price, expiry_date_obj, self.risk_free_rate, implied_vol)
            if fair_value <= 0: return

            price_difference_pct = ((fair_value - market_price) / market_price) * 100
            logging.debug(f"Value Analyzed {symbol}: Market={market_price:.2f}, FairValue={fair_value:.2f}, Diff={price_difference_pct:.2f}%")

            if price_difference_pct > self.trade_trigger_percentage:
                reason = f"Value BUY: Fair value ({fair_value:.2f}) is {price_difference_pct:.2f}% > market price ({market_price:.2f})."
                logging.info(reason)
                self.portfolio_manager.record_trade(symbol, "BUY", self.trade_quantity, market_price, reason)
        except Exception as e:
            logging.error(f"Error analyzing value for option {option_data.get('symbol', 'N/A')}: {e}")

    def shutdown(self):
        logging.info("🔌 Shutting down engine...")
        if self.api_client:
            self.api_client.logout()
        logging.info("Engine has been stopped.")

if __name__ == '__main__':
    engine = TradingEngine(config_path='config.ini')
    engine.run()
