# engine.py
# VERSION 2.2: Includes a Flask web server for free hosting on platforms like Render/Heroku.

import configparser
import logging
import time
import traceback
from datetime import datetime, time as dt_time
import os
import pytz
from threading import Thread # <-- Import Thread

# --- NEW: Import Flask ---
from flask import Flask

import pandas as pd
from api import AngelOneClient
from portfolio_manager import PortfolioManager
from pricing_model import black_scholes

# --- NEW: Create a Flask App ---
# This gives us a web endpoint to ping.
app = Flask(__name__)

@app.route('/')
def home():
    """A simple endpoint to show the engine is running and to be pinged."""
    return "Trading engine is alive."

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [ENGINE] - %(message)s',
    handlers=[
        logging.FileHandler("engine.log"),
        logging.StreamHandler()
    ]
)

IST = pytz.timezone('Asia/Kolkata')
MARKET_OPEN_TIME = dt_time(9, 15)
MARKET_CLOSE_TIME = dt_time(15, 30)

class TradingEngine:
    """
    The main class that orchestrates the trading strategy.
    """
    def __init__(self, config_path='config.ini'):
        logging.info("🚀 Starting Trading Engine v2.2...")
        self.config = self._load_config(config_path)
        
        # Load settings... (rest of the __init__ method is the same)
        self.symbols_to_watch = self.config['TRADING_ENGINE']['SYMBOLS_TO_WATCH'].split(',')
        self.run_interval_seconds = int(self.config['TRADING_ENGINE']['RUN_INTERVAL_SECONDS'])
        self.trade_trigger_percentage = float(self.config['TRADING_ENGINE']['TRADE_TRIGGER_PERCENTAGE'])
        self.risk_free_rate = float(self.config['TRADING_ENGINE']['RISK_FREE_RATE'])
        self.trade_quantity = int(self.config['TRADING_ENGINE']['TRADE_QUANTITY'])
        self.expiry_strategy_enabled = self.config.getboolean('EXPIRY_STRATEGY', 'ENABLED', fallback=False)
        self.max_iv_rank = self.config.getfloat('EXPIRY_STRATEGY', 'MAX_STRADDLE_IV_RANK', fallback=20.0)
        self.expiry_weekday = self.config.getint('EXPIRY_STRATEGY', 'EXPIRY_WEEKDAY', fallback=3)
        self.strategy_start_time = dt_time.fromisoformat(self.config.get('EXPIRY_STRATEGY', 'STRATEGY_START_TIME', fallback='14:55:00'))
        self.session_iv_tracker = {}
        self.expiry_trade_fired_today = {}
        self.symbol_details = {
            "NIFTY": {"token": "99926000", "exchange": "NSE"},
            "BANKNIFTY": {"token": "99926009", "exchange": "NSE"},
        }
        api_key = os.environ.get('API_KEY') or self.config['ANGEL_ONE']['API_KEY']
        client_id = os.environ.get('CLIENT_ID') or self.config['ANGEL_ONE']['CLIENT_ID']
        pin = os.environ.get('PIN') or self.config['ANGEL_ONE']['PIN']
        totp_key = os.environ.get('TOTP_KEY') or self.config['ANGEL_ONE']['TOTP_KEY']
        self.api_client = AngelOneClient(api_key, client_id, pin, totp_key)
        self.portfolio_manager = PortfolioManager()
        logging.info("Engine initialized successfully.")

    def _load_config(self, config_path):
        parser = configparser.ConfigParser()
        if not parser.read(config_path):
            raise ValueError(f"Configuration file not found at {config_path}")
        return parser
    
    def is_market_open(self):
        now_ist = datetime.now(IST)
        if now_ist.weekday() > 4: return False, "Weekend"
        if MARKET_OPEN_TIME <= now_ist.time() <= MARKET_CLOSE_TIME: return True, "Market is Open"
        return False, "Market is Closed"

    def run(self):
        """The main trading loop, designed to run in a background thread."""
        logging.info("Trading logic thread started.")
        while True:
            try:
                market_open, reason = self.is_market_open()
                if not market_open:
                    logging.info(f"Market is currently closed ({reason}). Sleeping for 15 minutes...")
                    time.sleep(900)
                    continue

                logging.info(f"{'='*20} Starting New Trading Cycle {'='*20}")
                for index_name in self.symbols_to_watch:
                    self.process_index(index_name)
                    time.sleep(self.api_client.REQUEST_INTERVAL_SECONDS)

                logging.info(f"Cycle finished. Waiting for {self.run_interval_seconds} seconds...")
                time.sleep(self.run_interval_seconds)
            except Exception as e:
                logging.error(f"An error occurred in the trading loop: {e}")
                traceback.print_exc()
                # Wait before retrying to avoid spamming logs on a persistent error
                time.sleep(60)

    # ... (The rest of your TradingEngine class methods: process_index, update_session_iv, etc. remain unchanged) ...
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
        self.update_session_iv(index_name, df_merged, underlying_ltp)
        for _, option in df_merged.iterrows():
            self.analyze_and_trade_value(option, underlying_ltp)
        if self.expiry_strategy_enabled:
            self.execute_expiry_straddle_strategy(index_name, df_merged, underlying_ltp)
    def update_session_iv(self, index_name, df_merged, underlying_ltp):
        try:
            df_merged['strike_price'] = df_merged['strike'].astype(float) / 100.0
            atm_strike = df_merged.iloc[(df_merged['strike_price'] - underlying_ltp).abs().argsort()[:1]]
            if atm_strike.empty: return
            current_iv = atm_strike.iloc[0]['iv']
            if index_name not in self.session_iv_tracker:
                self.session_iv_tracker[index_name] = {'high': current_iv, 'low': current_iv}
                logging.info(f"Initialized IV tracker for {index_name}: High={current_iv}, Low={current_iv}")
            else:
                self.session_iv_tracker[index_name]['high'] = max(self.session_iv_tracker[index_name]['high'], current_iv)
                self.session_iv_tracker[index_name]['low'] = min(self.session_iv_tracker[index_name]['low'], current_iv)
            logging.debug(f"IV Tracker for {index_name}: High={self.session_iv_tracker[index_name]['high']}, Low={self.session_iv_tracker[index_name]['low']}")
        except Exception as e:
            logging.error(f"Error updating session IV for {index_name}: {e}")
    def execute_expiry_straddle_strategy(self, index_name, df_merged, underlying_ltp):
        now = datetime.now(IST)
        if self.expiry_trade_fired_today.get(index_name) == now.date(): return
        if now.weekday() != self.expiry_weekday or now.time() < self.strategy_start_time: return
        logging.info(f"*** ACTIVATING EXPIRY STRADDLE STRATEGY FOR {index_name} ***")
        try:
            tracker = self.session_iv_tracker.get(index_name)
            if not tracker:
                logging.warning(f"No IV tracker data for {index_name} to calculate rank. Skipping.")
                return
            iv_high, iv_low = tracker['high'], tracker['low']
            df_merged['strike_price'] = df_merged['strike'].astype(float) / 100.0
            atm_option_row = df_merged.iloc[(df_merged['strike_price'] - underlying_ltp).abs().argsort()[:1]]
            current_iv = atm_option_row.iloc[0]['iv']
            iv_range = iv_high - iv_low
            session_iv_rank = 50.0 if iv_range == 0 else ((current_iv - iv_low) / iv_range) * 100
            logging.info(f"Current IV: {current_iv:.2f}, Day's Range: [{iv_low:.2f} - {iv_high:.2f}], Session IV Rank: {session_iv_rank:.2f}%")
            if session_iv_rank < self.max_iv_rank:
                logging.info(f"SUCCESS: IV Rank ({session_iv_rank:.2f}%) is below threshold ({self.max_iv_rank}%)! EXECUTING STRADDLE.")
                atm_strike_price = atm_option_row.iloc[0]['strike_price']
                atm_call = df_merged[(df_merged['strike_price'] == atm_strike_price) & (df_merged['symbol'].str.endswith('CE'))].iloc[0]
                atm_put = df_merged[(df_merged['strike_price'] == atm_strike_price) & (df_merged['symbol'].str.endswith('PE'))].iloc[0]
                reason = f"Expiry Straddle: IV Rank {session_iv_rank:.2f}% < {self.max_iv_rank}%"
                self.portfolio_manager.record_trade(atm_call['symbol'], "BUY", self.trade_quantity, atm_call['ltp_y'], reason)
                self.portfolio_manager.record_trade(atm_put['symbol'], "BUY", self.trade_quantity, atm_put['ltp_y'], reason)
                self.expiry_trade_fired_today[index_name] = now.date()
            else:
                logging.info(f"IV Rank ({session_iv_rank:.2f}%) is NOT below threshold ({self.max_iv_rank}%). No trade.")
        except Exception as e:
            logging.error(f"Error during expiry straddle strategy for {index_name}: {e}")
            logging.error(traceback.format_exc())
    def analyze_and_trade_value(self, option_data, underlying_ltp):
        try:
            symbol = option_data['symbol']
            option_type = option_data['symbol'][-2:]
            market_price = option_data['ltp_y']
            strike_price = float(option_data['strike']) / 100.0
            implied_vol = option_data['iv'] / 100.0
            if market_price <= 0 or implied_vol <= 0: return
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

def run_trading_engine():
    """Function to initialize and run the engine."""
    engine = TradingEngine(config_path='config.ini')
    engine.run()

if __name__ == '__main__':
    # --- Start the trading logic in a separate thread ---
    trading_thread = Thread(target=run_trading_engine)
    trading_thread.daemon = True # Allows main thread to exit even if this thread is running
    trading_thread.start()

    # --- Start the Flask web server ---
    # The web server's only job is to stay alive and respond to pings.
    # Use the PORT environment variable if available (required by Render/Heroku).
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)