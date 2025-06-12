import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import pandas as pd

# Add project_root to Python path to allow relative imports
project_root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root_path)

from strategy.grid_strategy import GridStrategy
from tqsdk import TqApi # For TqApi.serial_to_datetime in strategy if needed by mocks

class TestGridGeneration(unittest.TestCase):

    def setUp(self):
        # Mock TQSDK API
        self.mock_api = MagicMock(spec=TqApi) # Use spec for more accurate mocking

        # Mock get_quote
        self.mock_quote = MagicMock()
        self.mock_quote.last_price = 100.0
        self.mock_api.get_quote.return_value = self.mock_quote

        # Mock get_kline_serial for initial price fetching in generate_grids
        # It expects a DataFrame, so we provide a minimal one.
        # The strategy's generate_grids has a loop that waits for last_price.
        # We need to ensure that either last_price is set, or klines are available.

        # If generate_grids primarily uses self.quote.last_price, this kline mock might be secondary
        # but it's good to have if any part of init or generate_grids uses it.
        mock_kline_df = pd.DataFrame({
            'datetime': [pd.Timestamp('2023-01-01 09:00:00').value // 10**9], # TQSDK serial format (nanoseconds to seconds)
            'open': [99.0], 'high': [101.0], 'low': [98.0], 'close': [100.0], 'volume': [1000]
        })
        self.mock_api.get_kline_serial.return_value = mock_kline_df

        # Mock get_position and get_account as they are called in GridStrategy __init__
        self.mock_position = MagicMock()
        self.mock_position.pos_long = 0
        self.mock_position.pos_short = 0
        self.mock_api.get_position.return_value = self.mock_position

        self.mock_account = MagicMock()
        self.mock_account.balance = 100000.0
        self.mock_api.get_account.return_value = self.mock_account

        # Strategy parameters
        self.symbol = "TEST.rb0000" # Using a test symbol
        self.grid_spacing = 10.0
        self.order_volume = 1
        # total_volume in GridStrategy is used to determine num_buy_grids and num_sell_grids
        # num_buy_grids = (self.total_volume // 2) // self.order_volume
        # num_sell_grids = (self.total_volume // 2) // self.order_volume
        # So, if total_volume is 4, order_volume is 1, then num_buy_grids = 2, num_sell_grids = 2
        self.total_volume_for_test = 4
        self.tp_price = 150.0
        self.sl_price = 50.0

        self.strategy = GridStrategy(
            api=self.mock_api, # Corrected: tq_api to api
            symbol=self.symbol,
            grid_spacing=self.grid_spacing,
            order_volume=self.order_volume,
            total_volume=self.total_volume_for_test,
            take_profit_price=self.tp_price,
            stop_loss_price=self.sl_price,
            data_handler_callback=None,
            step_event=None
        )
        # generate_grids is NOT called in __init__ anymore.
        # Call it explicitly for tests that require grids to be pre-generated.
        self.strategy.generate_grids()


    def test_generate_grids_basic(self):
        """
        Test basic grid generation: correct number of buy/sell grids, prices, types, and volumes.
        """
        # Expected number of grids based on total_volume_for_test = 4, order_volume = 1
        # num_buy_grids = (4 // 2) // 1 = 2
        # num_sell_grids = (4 // 2) // 1 = 2
        # Total grids = 4
        self.assertEqual(len(self.strategy.grids), 4, "Should generate 4 grid lines in total.")

        grids_df = pd.DataFrame(self.strategy.grids).sort_values(by="price").reset_index(drop=True)

        # Expected buy grids
        expected_buy_grid_1_price = self.mock_quote.last_price - 1 * self.grid_spacing # 100 - 10 = 90
        expected_buy_grid_2_price = self.mock_quote.last_price - 2 * self.grid_spacing # 100 - 20 = 80

        # Expected sell grids
        expected_sell_grid_1_price = self.mock_quote.last_price + 1 * self.grid_spacing # 100 + 10 = 110
        expected_sell_grid_2_price = self.mock_quote.last_price + 2 * self.grid_spacing # 100 + 20 = 120

        # Check buy grids (should be the first 2 after sorting by price)
        self.assertEqual(grids_df.loc[0, "price"], expected_buy_grid_2_price)
        self.assertEqual(grids_df.loc[0, "type"], "BUY")
        self.assertEqual(grids_df.loc[0, "volume"], self.order_volume)
        self.assertEqual(grids_df.loc[0, "status"], "PENDING")

        self.assertEqual(grids_df.loc[1, "price"], expected_buy_grid_1_price)
        self.assertEqual(grids_df.loc[1, "type"], "BUY")
        self.assertEqual(grids_df.loc[1, "volume"], self.order_volume)
        self.assertEqual(grids_df.loc[1, "status"], "PENDING")

        # Check sell grids (should be the next 2)
        self.assertEqual(grids_df.loc[2, "price"], expected_sell_grid_1_price)
        self.assertEqual(grids_df.loc[2, "type"], "SELL")
        self.assertEqual(grids_df.loc[2, "volume"], self.order_volume)
        self.assertEqual(grids_df.loc[2, "status"], "PENDING")

        self.assertEqual(grids_df.loc[3, "price"], expected_sell_grid_2_price)
        self.assertEqual(grids_df.loc[3, "type"], "SELL")
        self.assertEqual(grids_df.loc[3, "volume"], self.order_volume)
        self.assertEqual(grids_df.loc[3, "status"], "PENDING")

    def test_generate_grids_edge_cases(self):
        """Test grid generation with edge case parameters."""
        # Case 1: total_volume = 0
        self.strategy.total_volume = 0
        self.strategy.generate_grids() # Regenerate with new params
        self.assertEqual(len(self.strategy.grids), 0, "Should generate 0 grids if total_volume is 0.")

        # Reset total_volume for next case
        self.strategy.total_volume = self.total_volume_for_test

        # Case 2: grid_spacing = 0
        # What is the expected behavior? The current implementation would create overlapping grids.
        # This might be an undesirable parameter, but the code would place them at current_price.
        # Let's test the current behavior.
        self.strategy.grid_spacing = 0
        self.mock_quote.last_price = 105.0 # Change price to see if it picks it up
        self.mock_api.get_quote.return_value.last_price = 105.0 # Ensure the mock is updated

        # Re-initialize strategy or call generate_grids with updated mock
        # For simplicity, create a new strategy instance for this specific sub-test
        # or ensure generate_grids correctly re-uses the mocked quote.
        # The strategy's generate_grids uses self.quote.last_price, which is set once in __init__.
        # So, we need to update self.strategy.quote.last_price or re-init.
        self.strategy.quote.last_price = 105.0 # Update the quote object used by strategy
        self.strategy.generate_grids()

        expected_grids_at_same_price = (self.total_volume_for_test // 2) // self.order_volume * 2
        self.assertEqual(len(self.strategy.grids), expected_grids_at_same_price)
        if expected_grids_at_same_price > 0:
            for grid in self.strategy.grids:
                self.assertEqual(grid["price"], 105.0, "All grids should be at the current price if spacing is 0.")

        # Reset grid_spacing for other tests if strategy instance is reused by them.
        self.strategy.grid_spacing = 10.0


    def test_initial_grid_placement_around_market_price(self):
        """Verify initial grids are correctly placed relative to the market price."""
        # This is somewhat implicitly tested in test_generate_grids_basic.
        # We can add a more direct check here.
        self.mock_quote.last_price = 150.0 # New market price
        self.mock_api.get_quote.return_value.last_price = 150.0
        self.strategy.quote.last_price = 150.0 # Update the quote object used by strategy

        self.strategy.generate_grids() # Regenerate grids

        self.assertTrue(len(self.strategy.grids) > 0, "Grids should be generated.")

        buy_grids = [g for g in self.strategy.grids if g['type'] == 'BUY']
        sell_grids = [g for g in self.strategy.grids if g['type'] == 'SELL']

        self.assertTrue(len(buy_grids) > 0, "Should have buy grids.")
        self.assertTrue(len(sell_grids) > 0, "Should have sell grids.")

        for buy_grid in buy_grids:
            self.assertLess(buy_grid['price'], 150.0, "Buy grids should be below market price.")

        for sell_grid in sell_grids:
            self.assertGreater(sell_grid['price'], 150.0, "Sell grids should be above market price.")

if __name__ == '__main__':
    unittest.main()
