# /scrapers/angel_one_client.py
# This file consolidates all three of your original scripts into a single,
# reusable class for interacting with the Angel One API.

import configparser
import json
import logging
import os
import time
import traceback
from datetime import datetime, date, timedelta

import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect

# --- Configure Logging ---
# Sets up a basic logger to output informational messages.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class AngelOneClient:
    """
    A unified client to handle authentication, and data fetching for equities,
    options, and greeks from the Angel One SmartAPI.
    """

    # --- Constants ---
    # URLs and filenames used by the client.
    INSTRUMENT_LIST_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    INSTRUMENT_FILE_NAME = "OpenAPIScripMaster.json"
    OPTION_GREEKS_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/marketData/v1/optionGreek"
    REQUEST_INTERVAL_SECONDS = 1  # To avoid hitting API rate limits.

    def __init__(self, config_path='config.ini'):
        """
        Initializes the client, reads configuration, and authenticates with the API.

        Args:
            config_path (str): The path to the configuration file.
        """
        logging.info("Initializing AngelOneClient...")
        self.config = self._load_config(config_path)
        if not self.config:
            raise ValueError("Configuration could not be loaded. Halting.")

        self.api_key = self.config['ANGEL_ONE']['API_KEY']
        self.client_id = self.config['ANGEL_ONE']['CLIENT_ID']
        self.pin = self.config['ANGEL_ONE']['PIN']
        self.totp_key = self.config['ANGEL_ONE']['TOTP_KEY']

        self.smart_api_obj = None
        self.instrument_list = None

        self._login()
        self._download_instrument_list()

    def _load_config(self, config_path):
        """Loads credentials and settings from the config.ini file."""
        try:
            parser = configparser.ConfigParser()
            if not parser.read(config_path):
                logging.error(f"Configuration file not found at '{config_path}'.")
                return None
            return parser
        except Exception as e:
            logging.error(f"Error reading config file: {e}")
            return None

    def _login(self):
        """
        Authenticates with the SmartAPI using credentials and automated TOTP.
        This replaces the manual TOTP input from your original scripts.
        """
        logging.info("Attempting to log in to Angel One...")
        try:
            self.smart_api_obj = SmartConnect(api_key=self.api_key)
            
            # --- Automated TOTP Generation ---
            # This is the key change to remove manual input.
            totp = pyotp.TOTP(self.totp_key).now()
            logging.info(f"Generated TOTP: {totp}")

            session_data = self.smart_api_obj.generateSession(self.client_id, self.pin, totp)

            if not session_data.get('status') or session_data.get('status') is False:
                logging.error(f"Login Failed: {session_data.get('message')}")
                raise ConnectionError("Failed to authenticate with Angel One.")
            
            logging.info("✅ Authentication Successful!")
            # Store the access token for direct HTTP requests (like for greeks)
            self.access_token = self.smart_api_obj.access_token

        except Exception as e:
            logging.error(f"An error occurred during login: {e}")
            logging.error(traceback.format_exc())
            raise

    def _download_instrument_list(self):
        """
        Downloads the master instrument list if it's missing or older than a day.
        This is necessary to find correct option symbols and tokens.
        """
        logging.info("Checking for instrument list...")
        if not os.path.exists(self.INSTRUMENT_FILE_NAME) or (time.time() - os.path.getmtime(self.INSTRUMENT_FILE_NAME) > 86400):
            logging.info("Downloading latest instrument list...")
            try:
                r = requests.get(self.INSTRUMENT_LIST_URL, timeout=15)
                r.raise_for_status()
                with open(self.INSTRUMENT_FILE_NAME, "w", encoding='utf-8') as f:
                    f.write(r.text)
                logging.info("Instrument list downloaded.")
            except requests.exceptions.RequestException as e:
                logging.error(f"Error downloading instrument list: {e}")
                raise
        
        with open(self.INSTRUMENT_FILE_NAME, "r", encoding='utf-8') as f:
            self.instrument_list = json.load(f)
        logging.info(f"Loaded {len(self.instrument_list)} instruments into memory.")

    def get_live_equity_data(self, exchange, symbol_token):
        """
        Fetches the Last Traded Price (LTP) for a single instrument.
        This replaces the historical data fetching script for our live engine.

        Args:
            exchange (str): The exchange (e.g., "NSE").
            symbol_token (str): The symbol token for the instrument.

        Returns:
            float: The last traded price, or None if an error occurs.
        """
        try:
            response = self.smart_api_obj.ltpData(exchange, exchange, symbol_token)
            if response.get("status") and response.get("data"):
                ltp = response["data"]["ltp"]
                logging.info(f"LTP for {symbol_token} on {exchange}: {ltp}")
                return ltp
            else:
                logging.warning(f"Could not fetch LTP for {symbol_token}. Message: {response.get('message')}")
                # Fallback for indices like SENSEX which may not work with ltpData
                if exchange == "BSE":
                    return self._get_ltp_from_candle(exchange, symbol_token)
                return None
        except Exception as e:
            logging.error(f"Error fetching LTP for {symbol_token}: {e}")
            return None

    def _get_ltp_from_candle(self, exchange, symbol_token):
        """Workaround to get LTP for indices by fetching the last daily candle."""
        logging.info(f"Using candle workaround to get LTP for {symbol_token} on {exchange}...")
        try:
            to_date = datetime.now()
            from_date = to_date - timedelta(days=5)
            params = {
                "exchange": exchange, "symboltoken": symbol_token, "interval": "ONE_DAY",
                "fromdate": from_date.strftime("%Y-%m-%d %H:%M"), "todate": to_date.strftime("%Y-%m-%d %H:%M")
            }
            response_data = self.smart_api_obj.getCandleData(params)
            if response_data.get('status') and response_data.get('data'):
                # The 5th element (index 4) is the closing price
                ltp = response_data['data'][-1][4]
                logging.info(f"Candle workaround LTP for {symbol_token}: {ltp}")
                return ltp
            logging.warning(f"Candle workaround failed for {symbol_token}.")
            return None
        except Exception as e:
            logging.error(f"Error in LTP candle workaround: {e}")
            return None
            
    def get_option_chain(self, index_name, ltp, num_strikes=5):
        """
        Finds the option chain for a given index around its LTP.
        
        Args:
            index_name (str): The name of the index (e.g., "NIFTY").
            ltp (float): The current Last Traded Price of the index.
            num_strikes (int): The number of strikes to fetch above and below the At-The-Money strike.

        Returns:
            list: A list of dictionaries, where each dictionary represents an option instrument.
                  Returns an empty list if no options are found.
        """
        logging.info(f"Fetching option chain for {index_name} around LTP {ltp}.")
        # These details would ideally be in a more dynamic config, but this is fine for now.
        strike_steps = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "SENSEX": 100, "MIDCPNIFTY": 25}
        step = strike_steps.get(index_name, 100)
        
        # Find the nearest expiry date
        now = datetime.now()
        today = now.date()
        min_expiry_date = today + timedelta(days=1) if now.time() > datetime.strptime("15:30", "%H:%M").time() else today
        
        possible_expiries = {
            datetime.strptime(item["expiry"], "%d%b%Y").date()
            for item in self.instrument_list
            if item.get("instrumenttype") == "OPTIDX" and item.get("name") == index_name and datetime.strptime(item.get("expiry", "01JAN1980"), "%d%b%Y").date() >= min_expiry_date
        }
        
        if not possible_expiries:
            logging.warning(f"No upcoming expiry found for {index_name}.")
            return []
            
        target_expiry = min(possible_expiries)
        target_expiry_str = target_expiry.strftime("%d%b%Y").upper()
        logging.info(f"Targeting expiry date: {target_expiry_str}")

        # Determine the strike prices to fetch
        atm_strike = int(round(ltp / step) * step)
        strikes_to_find = [atm_strike + (i * step) for i in range(-num_strikes, num_strikes + 1)]
        
        # Find matching instruments
        option_chain = [
            item for item in self.instrument_list
            if item.get("name") == index_name and
               item.get("instrumenttype") == "OPTIDX" and
               item.get("expiry") == target_expiry_str and
               item.get("strike") and
               (float(item.get("strike")) / 100.0) in strikes_to_find
        ]

        logging.info(f"Found {len(option_chain)} options in the chain for {index_name}.")
        return option_chain

    def get_option_greeks(self, index_name, expiry_date):
        """
        Fetches option greeks by making a direct, authenticated HTTP request.
        This is adapted from your greeks scraper.

        Args:
            index_name (str): The name of the index (e.g., "NIFTY").
            expiry_date (str): The expiry date in 'DDMMMYYYY' format (e.g., '07AUG2025').

        Returns:
            list: A list of dictionaries containing the greeks data, or None.
        """
        logging.info(f"Fetching greeks for {index_name} with expiry {expiry_date}...")
        try:
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json', 'Accept': 'application/json',
                'X-UserType': 'USER', 'X-SourceID': 'WEB', 'X-ClientLocalIP': '192.168.1.1',
                'X-ClientPublicIP': '192.168.1.1', 'X-MACAddress': '00:00:00:00:00:00',
                'X-PrivateKey': self.api_key
            }
            request_body = {"name": index_name, "expirydate": expiry_date}
            
            response = requests.post(self.OPTION_GREEKS_URL, headers=headers, json=request_body, timeout=10)
            response.raise_for_status()
            response_data = response.json()

            if response_data and response_data.get("status"):
                greeks_data = response_data.get("data", [])
                logging.info(f"Successfully fetched {len(greeks_data)} greeks records for {index_name}.")
                return greeks_data
            else:
                logging.warning(f"Could not fetch greeks for {index_name}: {response_data.get('message', 'Unknown error')}")
                return None
        except requests.exceptions.RequestException as e:
            logging.error(f"HTTP request error fetching greeks for {index_name}: {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred fetching greeks for {index_name}: {e}")
            logging.error(traceback.format_exc())
        return None

    def logout(self):
        """Logs out of the current session."""
        logging.info("Logging out...")
        try:
            if self.smart_api_obj:
                self.smart_api_obj.terminateSession(self.client_id)
                logging.info("Logged out successfully.")
        except Exception as e:
            logging.error(f"Logout failed: {e}")

# --- Example Usage ---
# This block demonstrates how to use the new client.
# In our final application, this logic will be in the main_engine.py file.
if __name__ == '__main__':
    # Create a dummy config file for testing
    if not os.path.exists('config.ini'):
        print("Creating a dummy 'config.ini'. Please edit it with your real credentials.")
        with open('config.ini', 'w') as f:
            f.write("[ANGEL_ONE]\n")
            f.write("API_KEY = your_api_key\n")
            f.write("CLIENT_ID = your_client_id\n")
            f.write("PIN = your_pin\n")
            f.write("TOTP_KEY = your_totp_secret_key\n\n")
            f.write("[TRADING_ENGINE]\n")
            f.write("SYMBOLS_TO_WATCH = NIFTY,BANKNIFTY\n")

    client = None # Initialize client to None
    try:
        # 1. Initialize and login
        client = AngelOneClient(config_path='config.ini')

        # 2. Define what to track
        # In the real app, this will come from config.ini
        indices_to_track = {
            "NIFTY": {"token": "99926000", "exchange": "NSE"},
            "BANKNIFTY": {"token": "99926009", "exchange": "NSE"},
        }

        # 3. Fetch data for each index
        for index_name, details in indices_to_track.items():
            print(f"\n{'='*20} Processing: {index_name} {'='*20}")
            
            # Get live price of the underlying index
            ltp = client.get_live_equity_data(details['exchange'], details['token'])
            if ltp is None:
                print(f"Could not get LTP for {index_name}. Skipping.")
                continue

            # Get the option chain based on the LTP
            option_chain_instruments = client.get_option_chain(index_name, ltp)
            if not option_chain_instruments:
                print(f"Could not get option chain for {index_name}. Skipping.")
                continue

            # The greeks endpoint needs the expiry date. We can get it from the first instrument.
            target_expiry = option_chain_instruments[0]['expiry']
            
            # Get the greeks for that entire expiry series
            greeks_data = client.get_option_greeks(index_name, target_expiry)

            # Now you have LTP, the full option chain, and the greeks.
            # You can combine them and pass them to the pricing model.
            print(f"LTP: {ltp}")
            print(f"Found {len(option_chain_instruments)} options for expiry {target_expiry}.")
            if greeks_data:
                print(f"Found {len(greeks_data)} greeks records.")
            
            # --- Next Step: Combine and Analyze ---
            # Here, you would merge this data and feed it into your Black-Scholes model.
            # For example, create a pandas DataFrame to easily merge the data.
            
            # *** FIX: Access the variable through the client object ***
            time.sleep(client.REQUEST_INTERVAL_SECONDS) # Be respectful of API limits

    except (ValueError, ConnectionError) as e:
        print(f"Could not start client: {e}")
    except Exception as e:
        print(f"A critical error occurred: {e}")
        traceback.print_exc()
    finally:
        if client:
            client.logout()
