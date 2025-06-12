import unittest
from unittest.mock import MagicMock, call, patch
import sys
import os
import pandas as pd
import threading

# Add project_root to Python path
project_root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root_path)

from strategy.grid_strategy import GridStrategy
from tqsdk import TqApi # For mocking TqApi (TargetPosTask is no longer used)

class TestStopProfitLossManualClose(unittest.TestCase): # Renamed class

    def setUp(self):
        self.mock_api = MagicMock(spec=TqApi)

        self.mock_quote = MagicMock()
        self.mock_quote.last_price = 100.0
        self.mock_api.get_quote.return_value = self.mock_quote

        mock_kline_df = pd.DataFrame({'datetime': [pd.Timestamp('2023-01-01 09:00:00').value]})
        self.mock_api.get_kline_serial.return_value = mock_kline_df

        self.mock_position_obj = MagicMock()
        self.mock_position_obj.pos_long = 0
        self.mock_position_obj.pos_short = 0
        self.mock_api.get_position.return_value = self.mock_position_obj

        self.mock_account = MagicMock(); self.mock_account.balance = 100000.0
        self.mock_api.get_account.return_value = self.mock_account

        # Mock insert_order for closing orders
        self.mock_closing_order = MagicMock()
        self.mock_closing_order.is_error.return_value = False
        self.mock_closing_order.is_finished.return_value = False # Initially not finished
        self.mock_closing_order.volume_left = 1 # Initially has volume left
        self.mock_api.insert_order.return_value = self.mock_closing_order

        self.symbol = "TEST.tpls00"
        self.take_profit_price = 120.0
        self.stop_loss_price = 80.0

        self.strategy = GridStrategy(
            api=self.mock_api,
            symbol=self.symbol,
            grid_spacing=10.0,
            order_volume=1,
            total_volume=2,
            take_profit_price=self.take_profit_price,
            stop_loss_price=self.stop_loss_price,
            data_handler_callback=None,
            step_event=None
        )
        self.strategy.strategy_active = True

    def tearDown(self):
        pass # No patcher to stop

    def test_take_profit_long_position_manual_close(self):
        """Test TP for long position with manual closing logic."""
        current_pos_volume = 2
        self.strategy.current_position = current_pos_volume
        self.mock_position_obj.pos_long = current_pos_volume
        self.mock_position_obj.pos_short = 0

        # Mock active orders to be cancelled
        mock_active_order1 = MagicMock(order_id="active1")
        mock_active_order1.is_finished.return_value = False
        self.strategy.active_orders = {"active1": mock_active_order1}

        self.strategy.quote.last_price = self.take_profit_price

        # Simulate position becoming zero after closing order
        pos_after_tp_call = MagicMock()
        pos_after_tp_call.pos_long = current_pos_volume # Still has position when check_profit_loss is first called
        pos_after_tp_call.pos_short = 0

        pos_after_close_order_placed = MagicMock()
        pos_after_close_order_placed.pos_long = 0 # Position becomes zero after closing order "fills"
        pos_after_close_order_placed.pos_short = 0

        # get_position will be called multiple times:
        # 1. At the start of check_profit_loss to get net_position.
        # 2. Inside the while loop to monitor position closing.
        self.mock_api.get_position.side_effect = [
            pos_after_tp_call,  # Initial check
            pos_after_tp_call,  # First check in while loop
            pos_after_close_order_placed # Second check in while loop (position now zero)
        ]

        # Simulate closing order finishing successfully
        self.mock_closing_order.is_finished.return_value = True
        self.mock_closing_order.volume_left = 0


        self.strategy.check_profit_loss()

        self.mock_api.cancel_order.assert_called_once_with(mock_active_order1)
        self.mock_api.insert_order.assert_called_once_with(
            symbol=self.symbol,
            direction="SELL",
            offset="CLOSE",
            volume=current_pos_volume,
            price_type="ANY"
        )
        self.assertFalse(self.strategy.strategy_active, "Strategy should be stopped after TP.")
        self.assertEqual(len(self.strategy.grids), 0, "Grids should be cleared after TP.")
        self.assertEqual(len(self.strategy.active_orders), 0, "Active orders should be cleared.")


    def test_stop_loss_short_position_manual_close(self):
        """Test SL for short position with manual closing logic."""
        current_pos_volume = -2
        self.strategy.current_position = current_pos_volume
        self.mock_position_obj.pos_long = 0
        self.mock_position_obj.pos_short = abs(current_pos_volume)

        self.strategy.stop_loss_price = 110.0
        self.strategy.take_profit_price = 90.0
        self.strategy.quote.last_price = 110.0

        pos_at_sl_call = MagicMock()
        pos_at_sl_call.pos_long = 0
        pos_at_sl_call.pos_short = abs(current_pos_volume)

        pos_after_close_order = MagicMock()
        pos_after_close_order.pos_long = 0
        pos_after_close_order.pos_short = 0

        self.mock_api.get_position.side_effect = [
            pos_at_sl_call,
            pos_at_sl_call,
            pos_after_close_order
        ]
        self.mock_closing_order.is_finished.return_value = True
        self.mock_closing_order.volume_left = 0

        self.strategy.check_profit_loss()

        self.mock_api.insert_order.assert_called_once_with(
            symbol=self.symbol,
            direction="BUY",
            offset="CLOSE",
            volume=abs(current_pos_volume),
            price_type="ANY"
        )
        self.assertFalse(self.strategy.strategy_active)


    def test_no_action_if_no_tp_sl_hit(self):
        """Test no action is taken if price does not hit TP/SL."""
        self.strategy.current_position = 1
        self.mock_position_obj.pos_long = 1
        self.mock_position_obj.pos_short = 0

        self.strategy.quote.last_price = self.mock_quote.last_price + 5

        initial_strategy_active_state = self.strategy.strategy_active
        self.strategy.check_profit_loss()

        self.mock_api.insert_order.assert_not_called() # No closing order
        self.assertEqual(self.strategy.strategy_active, initial_strategy_active_state)

    def test_tp_sl_with_no_position(self):
        """Test TP/SL logic does nothing if there's no current position."""
        self.strategy.current_position = 0
        self.mock_position_obj.pos_long = 0
        self.mock_position_obj.pos_short = 0

        self.strategy.quote.last_price = self.take_profit_price

        initial_strategy_active_state = self.strategy.strategy_active
        self.strategy.check_profit_loss()

        self.mock_api.insert_order.assert_not_called() # No closing order
        self.assertEqual(self.strategy.strategy_active, initial_strategy_active_state)

    def test_cancel_active_orders_on_tp_sl_manual_close(self): # Renamed
        """Test that active orders are cancelled when TP/SL is hit (manual close)."""
        self.strategy.current_position = 1
        self.mock_position_obj.pos_long = 1
        self.mock_position_obj.pos_short = 0

        mock_order1 = MagicMock(order_id="active_sell_1"); mock_order1.is_finished.return_value = False
        mock_order2 = MagicMock(order_id="active_buy_1"); mock_order2.is_finished.return_value = False
        self.strategy.active_orders = {
            "active_sell_1": mock_order1,
            "active_buy_1": mock_order2
        }

        self.strategy.quote.last_price = self.take_profit_price

        # Simulate position becoming zero for the loop to exit
        pos_at_call = MagicMock(); pos_at_call.pos_long=1; pos_at_call.pos_short=0;
        pos_after_close = MagicMock(); pos_after_close.pos_long=0; pos_after_close.pos_short=0;
        self.mock_api.get_position.side_effect = [pos_at_call, pos_at_call, pos_after_close]
        self.mock_closing_order.is_finished.return_value = True # Ensure closing order "fills"
        self.mock_closing_order.volume_left = 0

        self.strategy.check_profit_loss()

        self.mock_api.cancel_order.assert_any_call(mock_order1)
        self.mock_api.cancel_order.assert_any_call(mock_order2)
        self.assertEqual(self.mock_api.cancel_order.call_count, 2)
        self.assertEqual(len(self.strategy.active_orders), 0, "Active orders should be cleared after TP/SL.")


if __name__ == '__main__':
    unittest.main()
