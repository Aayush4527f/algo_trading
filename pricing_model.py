# /engine/pricing_model.py
# This module contains the Black-Scholes-Merton formula for calculating
# the theoretical fair value of a European option.

import logging
from math import log, sqrt, exp
from scipy.stats import norm
# *** FIX: Import timedelta alongside datetime ***
from datetime import datetime, timedelta

# --- Configure Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def black_scholes(
    option_type,       # "CE" for Call, "PE" for Put
    S,                 # Current price of the underlying asset (e.g., NIFTY index price)
    K,                 # Strike price of the option
    expiry_date,       # The expiry date of the option (as a datetime.date object)
    r,                 # Risk-free interest rate (annualized, e.g., 0.07 for 7%)
    sigma              # Volatility of the underlying asset (annualized, from IV)
):
    """
    Calculates the price of a European option using the Black-Scholes-Merton model.

    Args:
        option_type (str): The type of option, "CE" (Call) or "PE" (Put).
        S (float): Current price of the underlying asset.
        K (float): Strike price of the option.
        expiry_date (datetime.date): The date the option expires.
        r (float): Annualized risk-free interest rate.
        sigma (float): Annualized volatility of the underlying asset (Implied Volatility).

    Returns:
        float: The calculated theoretical price of the option, or 0.0 if inputs are invalid.
    """
    # --- 1. Input Validation ---
    if option_type not in ["CE", "PE"]:
        logging.error(f"Invalid option type: {option_type}. Must be 'CE' or 'PE'.")
        return 0.0
    if not all(isinstance(i, (int, float)) and i >= 0 for i in [S, K, r, sigma]):
        logging.error(f"Invalid numerical inputs: S={S}, K={K}, r={r}, sigma={sigma}. All must be non-negative numbers.")
        return 0.0

    # --- 2. Calculate Time to Expiry (T) ---
    # T is the time to expiration in years.
    today = datetime.now().date()
    time_to_expiry_days = (expiry_date - today).days
    
    if time_to_expiry_days < 0:
        logging.warning(f"Option has already expired ({expiry_date}). Price is 0.")
        return 0.0
    
    # Add 1 to include the current day for a more accurate TTM calculation
    T = (time_to_expiry_days + 1) / 365.0
    
    # --- 3. Black-Scholes Formula Calculations ---
    try:
        # d1 and d2 are intermediate values in the formula
        d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
        d2 = d1 - sigma * sqrt(T)

        if option_type == "CE":
            # Formula for a Call Option
            price = (S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2))
        else: # "PE"
            # Formula for a Put Option
            price = (K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
        
        return price

    except (ValueError, ZeroDivisionError) as e:
        logging.error(f"Mathematical error in Black-Scholes calculation: {e}. Inputs: S={S}, K={K}, T={T}, sigma={sigma}")
        return 0.0


# --- Example Usage ---
# This demonstrates how to use the pricing model.
if __name__ == '__main__':
    print("Running Black-Scholes model example...")

    # --- Example 1: At-the-money Call Option ---
    # Inputs for a hypothetical NIFTY call option
    underlying_price = 24000  # NIFTY is at 24000
    strike_price = 24000      # Strike price is also 24000
    days_to_expiry = 15
    expiry = datetime.now().date() + timedelta(days=days_to_expiry)
    risk_free_rate = 0.07     # Assume 7% risk-free rate
    implied_volatility = 0.15 # Assume 15% implied volatility

    call_price = black_scholes("CE", underlying_price, strike_price, expiry, risk_free_rate, implied_volatility)
    print(f"\n--- At-the-Money Call ---")
    print(f"Underlying: {underlying_price}, Strike: {strike_price}, Expiry: {days_to_expiry} days")
    print(f"Calculated Fair Price for Call Option: {call_price:.2f}")

    # --- Example 2: Out-of-the-money Put Option ---
    # Inputs for a hypothetical NIFTY put option
    strike_price_put = 23800  # Strike is below the current price

    put_price = black_scholes("PE", underlying_price, strike_price_put, expiry, risk_free_rate, implied_volatility)
    print(f"\n--- Out-of-the-Money Put ---")
    print(f"Underlying: {underlying_price}, Strike: {strike_price_put}, Expiry: {days_to_expiry} days")
    print(f"Calculated Fair Price for Put Option: {put_price:.2f}")

    # --- Example 3: Invalid Input ---
    print(f"\n--- Invalid Input Example ---")
    invalid_price = black_scholes("XX", 100, 100, expiry, 0.05, 0.2)
    print(f"Result for invalid option type: {invalid_price}")
