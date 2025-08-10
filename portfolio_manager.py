# /engine/portfolio_manager.py
# This module handles all database interactions and manages the state
# of the virtual portfolio.

import logging
import os
from datetime import datetime

from sqlalchemy import (create_engine, Column, Integer, String, Float,
                        DateTime, inspect)
from sqlalchemy.orm import declarative_base, sessionmaker

# --- Configure Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Database Setup ---
# Define the base class for our database models (the tables)
Base = declarative_base()
DB_FILE = "portfolio.db"

class Holding(Base):
    """
    Represents a current position in the portfolio.
    Each row is a unique instrument we are currently holding.
    """
    __tablename__ = 'holdings'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, unique=True, nullable=False)
    quantity = Column(Integer, nullable=False)
    average_price = Column(Float, nullable=False)
    last_updated = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Holding(symbol='{self.symbol}', quantity={self.quantity}, avg_price={self.average_price})>"

class TradeHistory(Base):
    """
    Represents a log of every trade executed.
    This provides a permanent audit trail.
    """
    __tablename__ = 'trade_history'
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    symbol = Column(String, nullable=False)
    trade_type = Column(String, nullable=False)  # "BUY" or "SELL"
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    reason = Column(String) # e.g., "Price below fair value by 5.2%"

    def __repr__(self):
        return f"<Trade(time='{self.timestamp}', type='{self.trade_type}', symbol='{self.symbol}', qty={self.quantity}, price={self.price})>"


class PortfolioManager:
    """
    Provides an interface to manage all portfolio operations,
    such as recording trades and querying holdings.
    """
    def __init__(self, db_file=DB_FILE):
        """
        Initializes the PortfolioManager and connects to the database.
        It will create the database and tables if they don't exist.
        """
        self.db_file = db_file
        self.engine = create_engine(f'sqlite:///{self.db_file}')
        self._create_tables_if_not_exist()
        
        # Session is the object we use to talk to the database
        self.Session = sessionmaker(bind=self.engine)

    def _create_tables_if_not_exist(self):
        """
        Checks if the required tables exist in the database and creates them
        if they are missing.
        """
        inspector = inspect(self.engine)
        if not inspector.has_table('holdings') or not inspector.has_table('trade_history'):
            logging.info(f"Database file '{self.db_file}' not found or tables missing. Creating them.")
            Base.metadata.create_all(self.engine)
        else:
            logging.info(f"Database '{self.db_file}' and tables already exist.")

    def record_trade(self, symbol, trade_type, quantity, price, reason=""):
        """
        Records a new trade in the trade_history table and updates the
        holdings table accordingly.

        Args:
            symbol (str): The instrument symbol (e.g., 'NIFTY28AUG2555000CE').
            trade_type (str): "BUY" or "SELL".
            quantity (int): The number of units traded.
            price (float): The price per unit.
            reason (str, optional): The justification for the trade.
        """
        session = self.Session()
        try:
            # 1. Log the trade in history
            new_trade = TradeHistory(
                symbol=symbol,
                trade_type=trade_type.upper(),
                quantity=quantity,
                price=price,
                reason=reason
            )
            session.add(new_trade)
            logging.info(f"RECORDED TRADE: {trade_type.upper()} {quantity} {symbol} @ {price}")

            # 2. Update holdings
            holding = session.query(Holding).filter_by(symbol=symbol).first()
            
            if trade_type.upper() == "BUY":
                if holding:
                    # Update existing holding
                    total_cost_old = holding.average_price * holding.quantity
                    total_cost_new = price * quantity
                    new_quantity = holding.quantity + quantity
                    holding.average_price = (total_cost_old + total_cost_new) / new_quantity
                    holding.quantity = new_quantity
                    holding.last_updated = datetime.utcnow()
                else:
                    # Add new holding
                    new_holding = Holding(
                        symbol=symbol,
                        quantity=quantity,
                        average_price=price
                    )
                    session.add(new_holding)
            
            elif trade_type.upper() == "SELL":
                if not holding or holding.quantity < quantity:
                    logging.error(f"Cannot SELL {quantity} of {symbol}. Holding quantity is {holding.quantity if holding else 0}.")
                    session.rollback() # Cancel the transaction
                    return

                # Reduce quantity of existing holding
                holding.quantity -= quantity
                holding.last_updated = datetime.utcnow()
                # If all units are sold, remove the holding
                if holding.quantity == 0:
                    session.delete(holding)

            session.commit()
            logging.info(f"Portfolio updated for {symbol}.")

        except Exception as e:
            logging.error(f"Error recording trade: {e}")
            session.rollback()
        finally:
            session.close()

    def get_all_holdings(self):
        """Retrieves all current holdings from the database."""
        session = self.Session()
        try:
            holdings = session.query(Holding).all()
            return holdings
        finally:
            session.close()

    def get_trade_history(self):
        """Retrieves all trade history from the database."""
        session = self.Session()
        try:
            history = session.query(TradeHistory).order_by(TradeHistory.timestamp.desc()).all()
            return history
        finally:
            session.close()

# --- Example Usage ---
# This demonstrates how to use the PortfolioManager.
if __name__ == '__main__':
    print("Running PortfolioManager example...")
    
    # For testing, we can remove the old DB file to start fresh
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)

    # 1. Initialize the manager
    pm = PortfolioManager()

    # 2. Simulate some trades
    print("\n--- Simulating Trades ---")
    pm.record_trade("NIFTY28AUG2555000CE", "BUY", 10, 150.50, "Price below fair value by 5.2%")
    pm.record_trade("BANKNIFTY28AUG25120000PE", "BUY", 5, 320.00, "Price below fair value by 8.1%")
    pm.record_trade("NIFTY28AUG2555000CE", "BUY", 5, 155.25, "Price still attractive")
    pm.record_trade("BANKNIFTY28AUG25120000PE", "SELL", 2, 350.00, "Taking partial profit")
    
    # 3. View current portfolio
    print("\n--- Current Holdings ---")
    current_holdings = pm.get_all_holdings()
    if current_holdings:
        for h in current_holdings:
            print(h)
    else:
        print("No holdings.")

    # 4. View trade history
    print("\n--- Trade History ---")
    trade_log = pm.get_trade_history()
    if trade_log:
        for t in trade_log:
            print(t)
    else:
        print("No trade history.")
