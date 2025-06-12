import unittest
from unittest.mock import MagicMock, call # import call for checking multiple calls
import sys
import os
import pandas as pd
import threading

# Add project_root to Python path
project_root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root_path)

from strategy.grid_strategy import GridStrategy
from tqsdk import TqApi # For type hinting and TqApi.serial_to_datetime if necessary

class TestOrderLogic(unittest.TestCase):

    def setUp(self):
        self.mock_api = MagicMock(spec=TqApi)

        self.mock_quote = MagicMock()
        self.mock_quote.last_price = 100.0
        self.mock_api.get_quote.return_value = self.mock_quote

        # Mock klines, position, account as in TestGridGeneration
        # Use pd.Timestamp().value for nanoseconds since epoch, matching TQSDK kline 'datetime' format
        mock_kline_df = pd.DataFrame({'datetime': [pd.Timestamp('2023-01-01 09:00:00').value]}) # Minimal
        self.mock_api.get_kline_serial.return_value = mock_kline_df
        self.mock_position = MagicMock(); self.mock_position.pos_long = 0; self.mock_position.pos_short = 0
        self.mock_api.get_position.return_value = self.mock_position
        self.mock_account = MagicMock(); self.mock_account.balance = 100000.0
        self.mock_api.get_account.return_value = self.mock_account

        self.symbol = "TEST.order00"
        self.grid_spacing = 10.0
        self.order_volume = 1
        self.total_volume = 4 # For 2 buy, 2 sell grids initially

        self.strategy = GridStrategy(
            api=self.mock_api,
            symbol=self.symbol,
            grid_spacing=self.grid_spacing,
            order_volume=self.order_volume,
            total_volume=self.total_volume,
            take_profit_price=150.0,
            stop_loss_price=50.0,
            data_handler_callback=None,
            step_event=None
        )
        # Initial grids: buy at 90, 80; sell at 110, 120, assuming last_price was 100
        self.strategy.generate_grids() # Ensure grids are generated

    def test_identify_buy_order_grid(self):
        """Test if strategy identifies the correct buy grid when market price hits it."""
        self.mock_api.insert_order.return_value = MagicMock() # Mock the returned order object from insert_order

        # Market price drops to hit the first buy grid (price 90)
        self.strategy.quote.last_price = 90.0
        self.strategy.place_orders()

        # Check if insert_order was called for the grid at 90
        # self.mock_api.insert_order.assert_called_once() # Might be called multiple times if logic is loose
        found_call = False
        for mock_call in self.mock_api.insert_order.call_args_list:
            args, kwargs = mock_call
            if kwargs.get("limit_price") == 90.0 and kwargs.get("direction") == "BUY":
                found_call = True
                break
        self.assertTrue(found_call, "Should have placed a BUY order at 90.0")

        # Verify the grid status changed to ACTIVE
        grid_90 = next(g for g in self.strategy.grids if g["price"] == 90.0 and g["type"] == "BUY")
        self.assertEqual(grid_90["status"], "ACTIVE")

    def test_identify_sell_order_grid(self):
        """Test if strategy identifies the correct sell grid."""
        self.mock_api.insert_order.return_value = MagicMock()

        self.strategy.quote.last_price = 110.0 # Price hits first sell grid
        self.strategy.place_orders()

        found_call = False
        for mock_call in self.mock_api.insert_order.call_args_list:
            args, kwargs = mock_call
            if kwargs.get("limit_price") == 110.0 and kwargs.get("direction") == "SELL":
                found_call = True
                break
        self.assertTrue(found_call, "Should have placed a SELL order at 110.0")
        grid_110 = next(g for g in self.strategy.grids if g["price"] == 110.0 and g["type"] == "SELL")
        self.assertEqual(grid_110["status"], "ACTIVE")

    def test_no_order_if_price_between_grids(self):
        """Test no order is placed if market price is between grid lines."""
        self.strategy.quote.last_price = 95.0 # Between 90 (buy) and 100 (initial) or 110 (sell)
        self.strategy.place_orders()
        self.mock_api.insert_order.assert_not_called()

    def test_no_duplicate_orders_for_active_grid(self):
        """Test that no duplicate order is placed for a grid that's already active."""
        self.mock_api.insert_order.return_value = MagicMock(order_id="order_at_90") # Mock order object

        # First, price hits the buy grid
        self.strategy.quote.last_price = 90.0
        self.strategy.place_orders()
        self.mock_api.insert_order.assert_called_once() # Called once for the first hit

        # Simulate the order is active (strategy does this by changing grid status and adding to self.active_orders)
        # The current place_orders logic checks self.active_orders to prevent duplicates.
        # We need to make sure an order object is in self.active_orders
        active_order_mock = MagicMock()
        active_order_mock.limit_price = 90.0
        active_order_mock.direction = "BUY"
        active_order_mock.is_finished.return_value = False # Mark as not finished
        self.strategy.active_orders["order_at_90"] = active_order_mock # Manually add to active_orders for test

        # Price stays at 90, call place_orders again
        self.strategy.place_orders()
        # insert_order should still have been called only once in total from the first hit.
        self.mock_api.insert_order.assert_called_once()

    def test_buy_order_filled_creates_sell_grid(self):
        """Simulate a buy order fill and verify a new sell grid is created."""
        # Initial state: buy grids at 80, 90; sell at 110, 120
        # Simulate buy order at 90.0 filled
        filled_order = MagicMock()
        filled_order.order_id = "buy_order_filled_at_90"
        filled_order.limit_price = 90.0
        filled_order.direction = "BUY"
        filled_order.offset = "OPEN"
        filled_order.volume_orign = self.order_volume
        filled_order.volume_traded = self.order_volume
        filled_order.trade_price = 90.0 # Average fill price
        filled_order.status_msg = "已成交"
        filled_order.is_error.return_value = False
        filled_order.is_finished.return_value = True
        # filled_order.insert_date_time = TqApi.datetime_to_serial(pd.Timestamp.now()) # For trade_info if used

        # Add this order to active_orders so update_order_status can find it
        self.strategy.active_orders[filled_order.order_id] = filled_order
        # Mark the corresponding grid as ACTIVE (as if an order was just placed)
        grid_at_90 = next(g for g in self.strategy.grids if g["price"] == 90.0 and g["type"] == "BUY")
        grid_at_90["status"] = "ACTIVE"

        self.strategy.update_order_status(filled_order)

        self.assertEqual(grid_at_90["status"], "FILLED")
        self.assertEqual(self.strategy.current_position, self.order_volume) # Position updated

        # Check for new sell grid: 90 (fill) + 10 (spacing) = 100
        new_sell_grid_exists = any(g["price"] == 100.0 and g["type"] == "SELL" and g["status"] == "PENDING" for g in self.strategy.grids)
        self.assertTrue(new_sell_grid_exists, "New sell grid should be created at 100.0")

    def test_sell_order_filled_creates_buy_grid(self):
        """Simulate a sell order fill and verify a new buy grid is created."""
        # Simulate sell order at 110.0 filled
        filled_order = MagicMock()
        filled_order.order_id = "sell_order_filled_at_110"
        filled_order.limit_price = 110.0
        filled_order.direction = "SELL"
        filled_order.offset = "OPEN"
        filled_order.volume_orign = self.order_volume
        filled_order.volume_traded = self.order_volume
        filled_order.trade_price = 110.0
        filled_order.status_msg = "已成交"
        filled_order.is_error.return_value = False
        filled_order.is_finished.return_value = True

        self.strategy.active_orders[filled_order.order_id] = filled_order
        grid_at_110 = next(g for g in self.strategy.grids if g["price"] == 110.0 and g["type"] == "SELL")
        grid_at_110["status"] = "ACTIVE"

        self.strategy.update_order_status(filled_order)

        self.assertEqual(grid_at_110["status"], "FILLED")
        self.assertEqual(self.strategy.current_position, -self.order_volume) # Position updated

        # Check for new buy grid: 110 (fill) - 10 (spacing) = 100
        new_buy_grid_exists = any(g["price"] == 100.0 and g["type"] == "BUY" and g["status"] == "PENDING" for g in self.strategy.grids)
        self.assertTrue(new_buy_grid_exists, "New buy grid should be created at 100.0")

    def test_filled_grid_marked_as_filled(self):
        """Ensure a grid is marked FILLED after its order is filled."""
        # This is covered by the previous two tests, but can be a specific check.
        filled_order = MagicMock()
        filled_order.order_id = "order_to_be_filled"
        filled_order.limit_price = 90.0 # A buy grid
        filled_order.direction = "BUY"
        filled_order.volume_traded = self.order_volume
        filled_order.is_error.return_value = False
        filled_order.is_finished.return_value = True

        self.strategy.active_orders[filled_order.order_id] = filled_order
        original_grid = next(g for g in self.strategy.grids if g["price"] == 90.0 and g["type"] == "BUY")
        original_grid["status"] = "ACTIVE" # Pre-condition for update_order_status logic

        self.strategy.update_order_status(filled_order)
        self.assertEqual(original_grid["status"], "FILLED")


if __name__ == '__main__':
    unittest.main()
