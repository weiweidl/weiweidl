from tqsdk import TqApi, TqAuth, TqAccount, TqKq
import threading
import time
import logging # Added for logging

class GridStrategy:
    def __init__(self,
                 api: TqApi, # Changed: API instance is now passed in
                 symbol: str,
                 grid_spacing: float,
                 order_volume: int,
                 total_volume: int,
                 take_profit_price: float,
                 stop_loss_price: float,
                 data_handler_callback=None, # New: For sending data to UI
                 step_event: threading.Event = None): # New: For single-step control
        """
        Initializes the Grid Trading Strategy.

        Args:
            api (TqApi): An existing TQSDK API instance.
            symbol (str): The trading instrument symbol.
            grid_spacing (float): The price difference between grid levels.
            order_volume (int): The volume for each order placed at a grid level.
            total_volume (int): The maximum total volume to be traded by the strategy.
            take_profit_price (float): The price at which to take profit and close all positions.
            stop_loss_price (float): The price at which to stop loss and close all positions.
            data_handler_callback (callable, optional): Callback for sending data to UI.
            step_event (threading.Event, optional): Event for controlling single-step execution.
        """
        self.api = api # Use the passed-in API instance
        self.symbol = symbol
        self.grid_spacing = grid_spacing # Original user input
        self.order_volume = order_volume
        self.total_volume = total_volume
        self.take_profit_price = take_profit_price
        self.stop_loss_price = stop_loss_price
        self.data_handler_callback = data_handler_callback
        self.step_event = step_event

        self.logger = logging.getLogger(__name__)
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        self.quote = self.api.get_quote(self.symbol)

        # Validate quote and check if expired
        if self.quote is None or self.quote.instrument_id is None: # Check if essential field is missing
            self.logger.error(f"Failed to get valid quote information for symbol {self.symbol}. Strategy cannot start.")
            self.strategy_active = False
            self.price_tick = 0.0 # Default if quote fails
            return

        if self.quote.get("expired", False):
            self.logger.error(f"Symbol {self.symbol} is expired. Strategy cannot run.")
            self.strategy_active = False
            self.price_tick = self.quote.get("price_tick", 0.0) # Still get price_tick if available
            return
        else:
            self.strategy_active = True # Explicitly set active if not expired
            self.logger.info(f"Symbol {self.symbol} is not expired. Strategy proceeding.")


        self.price_tick = self.quote.get("price_tick", 0.0)
        if self.price_tick <= 0:
            self.logger.warning(f"Price tick for {self.symbol} is {self.price_tick}. Price rounding will be skipped. This might be unexpected for some instruments.")
        else:
            # Validate grid_spacing against price_tick
            original_grid_spacing = self.grid_spacing
            if self.grid_spacing < self.price_tick:
                self.logger.warning(f"Grid spacing {self.grid_spacing} is less than price_tick {self.price_tick} for {self.symbol}. Adjusting grid_spacing to {self.price_tick}.")
                self.grid_spacing = self.price_tick
            elif self.grid_spacing % self.price_tick != 0:
                # Adjust grid_spacing to be a multiple of price_tick
                self.grid_spacing = round(self.grid_spacing / self.price_tick) * self.price_tick
                if self.grid_spacing == 0 and original_grid_spacing > 0 : # if rounding made it zero, set to one tick
                    self.grid_spacing = self.price_tick
                self.logger.warning(f"Grid spacing {original_grid_spacing} is not a multiple of price_tick {self.price_tick}. Adjusted to {self.grid_spacing}.")

        self.klines = self.api.get_kline_serial(self.symbol, duration_seconds=60)
        self.account = self.api.get_account()

        self.grids = []
        self.active_orders = {}
        self.current_position = self.api.get_position(self.symbol).pos_long - self.api.get_position(self.symbol).pos_short

        self.logger.info(f"GridStrategy initialized for {self.symbol}. Price Tick: {self.price_tick}, Adjusted Grid Spacing: {self.grid_spacing}, Initial position: {self.current_position}")


    def _send_data_to_handler(self):
        if not self.strategy_active and not self.data_handler_callback : # if strategy stopped early and no callback
             return
        if self.data_handler_callback:
            # Collect K-lines (last N, e.g., 100)
            # TQSDK kline_serial is a pandas DataFrame
            kline_data = []
            if not self.klines.empty:
                # Select relevant columns and convert to list of dicts
                df = self.klines.tail(100)[['datetime', 'open', 'high', 'low', 'close', 'volume']]
                df['datetime'] = df['datetime'].apply(lambda x: x if isinstance(x, str) else TqApi.serial_to_datetime(x).strftime("%Y-%m-%d %H:%M:%S"))
                kline_data = df.to_dict(orient='records')


            # Collect active orders
            active_orders_data = []
            for order_id, order in self.active_orders.items():
                # Ensure order object is serializable or extract needed fields
                if order.is_online and not order.is_finished(): # is_online means still active in some way
                    active_orders_data.append({
                        "order_id": order.order_id,
                        "price": order.limit_price,
                        "volume": order.volume_orign,
                        "direction": order.direction,
                        "offset": order.offset,
                        "status": order.status_msg
                    })

            # Collect trades (last N, e.g., 20, from TQSDK)
            # This requires getting all trades and filtering, or TQSDK might offer a more direct way for recent trades.
            # For simplicity, let's assume we get all trades for the symbol and filter by time if needed.
            # This part can be resource-intensive if not handled carefully.
            # A better approach for live/backtest is to accumulate trades as they happen.
            # For now, let's send only newly detected trades in `update_order_status`.
            # So, `handle_strategy_data` in app.py will need to accumulate trades.
            # Here, we'll just send all current grid states.

            grid_lines_data = [{"price": g["price"], "type": g["type"], "status": g["status"]} for g in self.grids]

            position_data = {
                "current_position": self.current_position, # self.api.get_position(self.symbol).pos might be better
                "symbol": self.symbol
            }

            account_balance = self.account.balance if self.account else None

            data_payload = {
                "klines": kline_data,
                "active_orders": active_orders_data,
                "grid_lines": grid_lines_data,
                "position": position_data,
                "account_balance": account_balance,
                "timestamp": TqApi.serial_to_datetime(self.api.get_kline_serial(self.symbol, 0)[-1]['datetime']).strftime("%Y-%m-%d %H:%M:%S") if len(self.api.get_kline_serial(self.symbol, 0)) > 0 else "N/A"
            }
            self.data_handler_callback(data_payload)


    def update_order_status(self, order):
        """
        Updates the status of an active order and handles filled orders.
        """
        if order.order_id in self.active_orders: # Check if it's one of our strategy's orders
            # self.active_orders[order.order_id]["status"] = order.status_msg # TQSDK status_msg gives text like "已成"
            original_order_details_from_grid_ref = None
            # Find the grid this order corresponds to, this is a bit indirect now
            # We need to ensure that the `order` object from TQSDK can be linked back to our `grid` structure
            # The `active_orders` now stores TQSDK order objects. We need to find the grid_ref.

            # Let's assume we stored grid_ref when creating the order or can find it.
            # For simplicity, let's refine how active_orders are stored or how grid_ref is accessed.
            # Option: active_orders could store a dict: { "tq_order": order_obj, "grid_ref": grid_dict_ref }
            # For now, let's iterate grids to find the one matching the order's price and direction if it's filled or errored.

            grid_associated_with_order = None
            for g in self.grids:
                # This matching is simplistic. A better way is to store grid ID with order.
                if g["price"] == order.limit_price and g["type"] == order.direction and g["status"] == "ACTIVE":
                    grid_associated_with_order = g
                    break

            if order.is_error():
                print(f"Order {order.order_id} error: {order.status_msg}")
                if grid_associated_with_order:
                     grid_associated_with_order["status"] = "PENDING" # Reset grid status
                if order.order_id in self.active_orders:
                    del self.active_orders[order.order_id]

            elif order.is_finished() and order.volume_traded > 0: # Ensure it actually traded
                # order_detail = self.active_orders[order.order_id] # This was the old way
                print(f"Order {order.order_id} ({order.direction}@{order.limit_price}) filled. Volume traded: {order.volume_traded}/{order.volume_orign}")

                # Update current position using TQSDK's position object for reliability
                # self.current_position will be updated via self.api.get_position() in _send_data_to_handler or at start of loop.
                # For immediate reflection:
                if order.direction == "BUY":
                    self.current_position += order.volume_traded
                elif order.direction == "SELL":
                    self.current_position -= order.volume_traded
                print(f"Position updated: {self.current_position} lots of {self.symbol}.")

                if grid_associated_with_order:
                    grid_associated_with_order["status"] = "FILLED"

                # Remove filled order from active_orders
                if order.order_id in self.active_orders:
                    del self.active_orders[order.order_id]

                # If data_handler is present, send info about this specific trade
                if self.data_handler_callback:
                    trade_info = {
                        "type": "trade_fill",
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "direction": order.direction,
                        "offset": order.offset,
                        "price": order.trade_price, # Average fill price
                        "volume": order.volume_traded,
                        "time": TqApi.serial_to_datetime(order.insert_date_time).strftime("%Y-%m-%d %H:%M:%S") # or use trade_date_time if available
                    }
                    # This sends individual trades. The main data handler sends snapshots.
                    # Consider how to consolidate this. For now, app.py will get snapshots.
                    # self.data_handler_callback({"trades": [trade_info]})


                # Simple logic: if a buy order fills, create a sell grid above it.
                # If a sell order fills, create a buy grid below it.
                # This needs to be managed carefully to avoid exceeding total_volume or creating too many grids.
                if order.direction == "BUY" and grid_associated_with_order:
                    new_sell_price = grid_associated_with_order["price"] + self.grid_spacing
                    if not any(g['price'] == new_sell_price and g['type'] == 'SELL' for g in self.grids):
                        print(f"Buy filled at {grid_associated_with_order['price']}. Adding new SELL grid at {new_sell_price}")
                        self.grids.append({"price": new_sell_price, "type": "SELL", "volume": self.order_volume, "status": "PENDING"})
                elif order.direction == "SELL" and grid_associated_with_order:
                    new_buy_price = grid_associated_with_order["price"] - self.grid_spacing
                    if not any(g['price'] == new_buy_price and g['type'] == 'BUY' for g in self.grids):
                        print(f"Sell filled at {grid_associated_with_order['price']}. Adding new BUY grid at {new_buy_price}")
                        self.grids.append({"price": new_buy_price, "type": "BUY", "volume": self.order_volume, "status": "PENDING"})

                self.grids.sort(key=lambda x: x["price"]) # Sort grids again

            elif order.is_finished() and order.volume_traded == 0 and order.order_id in self.active_orders: # e.g. cancelled
                print(f"Order {order.order_id} finished with no volume traded (e.g. cancelled). Status: {order.status_msg}")
                if grid_associated_with_order: # If it was associated with a grid
                    grid_associated_with_order["status"] = "PENDING" # Reset grid
                del self.active_orders[order.order_id]


    def generate_grids(self):
        """
        Generates buy and sell grid levels based on the current market price.
        It aims to distribute a certain number of buy and sell orders around the current price.
        """
        # Ensure quote is up-to-date
        # self.api.wait_update() # This might not be needed if run loop handles it.

        # Get current price from quote
        # Retry getting last_price a few times if it's None initially in backtest
        current_price = None
        if not self.strategy_active: # If strategy was deactivated in init
            self.logger.warning("generate_grids called but strategy is inactive.")
            return

        for _ in range(5):
            current_price = self.quote.last_price
            if current_price is not None:
                break
            self.logger.info("Waiting for market data (last_price) to generate grids...")
            # In a live or backtest scenario driven by run(), wait_update would be external.
            # For direct calls to generate_grids (e.g. in tests or if run() is not yet started),
            # we might need a wait_update if last_price is critical and not yet populated.
            # However, __init__ now ensures quote is fetched. If last_price is still None, it's an issue.
            if self.api.is_backtest() and len(self.api.get_kline_serial(self.symbol, 0)) > 0 : # Check if klines exist
                 self.api.wait_update(deadline=self.api.get_kline_serial(self.symbol, 0).iloc[-1]["datetime"] + 60)
            else: # if not backtest or no klines, just a short wait
                 self.api.wait_update(deadline=time.time() + 1)


        if current_price is None:
             self.logger.error("Last price is not available after retries. Cannot generate grids.")
             self.strategy_active = False # Stop strategy if price is essential and missing
             return

        self.logger.info(f"Current market price for grid generation: {current_price}")
        self.grids = []

        # Determine the number of grid levels based on total_volume and order_volume
        # For simplicity, let's assume half for buy and half for sell initially.
        # This logic can be more sophisticated, e.g., based on how much capital to deploy.
        num_buy_grids = (self.total_volume // 2) // self.order_volume
        num_sell_grids = (self.total_volume // 2) // self.order_volume

        print(f"Generating grids around price: {current_price}")

        # Generate buy grids below current price
        for i in range(1, num_buy_grids + 1):
            buy_price = current_price - i * self.grid_spacing
            if self.price_tick > 0:
                buy_price = round(buy_price / self.price_tick) * self.price_tick
            self.grids.append({"price": buy_price, "type": "BUY", "volume": self.order_volume, "status": "PENDING"})

        # Generate sell grids above current price
        for i in range(1, num_sell_grids + 1):
            sell_price = current_price + i * self.grid_spacing
            if self.price_tick > 0:
                sell_price = round(sell_price / self.price_tick) * self.price_tick
            self.grids.append({"price": sell_price, "type": "SELL", "volume": self.order_volume, "status": "PENDING"})

        self.grids.sort(key=lambda x: x["price"])

        self.logger.info(f"Generated {len(self.grids)} grid levels.")
        for grid in self.grids:
            self.logger.debug(f"  - Price: {grid['price']}, Type: {grid['type']}, Volume: {grid['volume']}")


    def place_orders(self):
        """
        Monitors market price and places orders when price crosses a grid level.
        Ensures not to place duplicate orders for the same grid level if an order is already active.
        Checks against upper/lower limits.
        """
        if not self.strategy_active: return
        if self.quote.last_price is None:
            self.logger.warning("Last price is not available for placing orders.")
            return

        current_price = self.quote.last_price
        upper_limit = self.quote.get("upper_limit", float('inf'))
        lower_limit = self.quote.get("lower_limit", float('-inf'))

        # self.logger.debug(f"Current market price: {current_price}. Checking grids for order placement. Limits: L={lower_limit}, U={upper_limit}")

        for grid in self.grids:
            if grid["status"] == "PENDING":
                # Check if an order for this grid price and type is already active to avoid duplicates
                # This check is simplified; a more robust check would involve order IDs and their states.
                order_already_placed = False
                # Check if an order for this grid price and type is already active
                is_order_active_for_grid = False
                for active_order_id, active_tq_order in self.active_orders.items():
                    if active_tq_order.limit_price == grid["price"] and \
                       active_tq_order.direction == grid["type"] and \
                       not active_tq_order.is_finished(): # Check if it's not finished
                        is_order_active_for_grid = True
                        break

                if is_order_active_for_grid:
                    continue

                grid_price = grid["price"]

                if grid["type"] == "BUY":
                    if current_price <= grid_price:
                        if grid_price > upper_limit : # Strictly, buy orders use current_price <= grid_price; limit orders should not exceed upper_limit.
                                                     # For a buy limit order, the price itself should not be above upper_limit.
                                                     # Also, TQSDK might place it at market if it's a favorable price against limit.
                                                     # A more robust check for buy: grid_price should not be above upper_limit.
                            self.logger.info(f"Buy order price {grid_price} is above upper limit {upper_limit}. Skipping.")
                            continue
                        # Also, technically a buy order should not be placed if grid_price < lower_limit as it might not fill or indicates issues
                        if grid_price < lower_limit:
                             self.logger.info(f"Buy order price {grid_price} is below lower limit {lower_limit}. Skipping (or TQSDK might handle).")
                             continue

                        self.logger.info(f"Placing BUY order at {grid_price} for {grid['volume']} lots.")
                        inserted_order = self.api.insert_order(
                            symbol=self.symbol, direction="BUY", offset="OPEN",
                            limit_price=grid_price, volume=grid["volume"]
                        )
                        self.active_orders[inserted_order.order_id] = inserted_order
                        grid["status"] = "ACTIVE"
                elif grid["type"] == "SELL":
                    if current_price >= grid_price:
                        if grid_price < lower_limit: # Sell order price should not be below lower_limit
                            self.logger.info(f"Sell order price {grid_price} is below lower limit {lower_limit}. Skipping.")
                            continue
                        # Also, a sell order should not be placed if grid_price > upper_limit
                        if grid_price > upper_limit:
                            self.logger.info(f"Sell order price {grid_price} is above upper limit {upper_limit}. Skipping (or TQSDK might handle).")
                            continue

                        self.logger.info(f"Placing SELL order at {grid_price} for {grid['volume']} lots.")
                        inserted_order = self.api.insert_order(
                            symbol=self.symbol, direction="SELL", offset="OPEN",
                            limit_price=grid_price, volume=grid["volume"]
                        )
                        self.active_orders[inserted_order.order_id] = inserted_order
                        grid["status"] = "ACTIVE"

    def check_profit_loss(self):
        """
        Monitors current position and market price for take profit or stop loss conditions.
        If a condition is met, it attempts to close all open positions.
        """
        if not self.strategy_active: return # Do nothing if strategy is not active

        if self.quote.last_price is None or self.current_position == 0:
            return

        current_price = self.quote.last_price
        should_close_all = False
        close_reason = ""

        # Check for Take Profit
        if self.current_position > 0: # Long position
            if current_price >= self.take_profit_price:
                should_close_all = True
                close_reason = f"Take Profit hit at {current_price} for long position."
        elif self.current_position < 0: # Short position
            if current_price <= self.take_profit_price: # Note: For short, take profit is below entry
                should_close_all = True
                close_reason = f"Take Profit hit at {current_price} for short position."

        # Check for Stop Loss (overrides take profit if both met, though unlikely with typical setup)
        if self.current_position > 0: # Long position
            if current_price <= self.stop_loss_price:
                should_close_all = True
                close_reason = f"Stop Loss hit at {current_price} for long position."
        elif self.current_position < 0: # Short position
            if current_price >= self.stop_loss_price: # Note: For short, stop loss is above entry
                should_close_all = True
                close_reason = f"Stop Loss hit at {current_price} for short position."

        if should_close_all:
            print(close_reason)
            print(f"Attempting to close all positions for {self.symbol}. Current position: {self.current_position}")

            self.logger.info(close_reason)
            # Cancel all pending orders first
            self.logger.info("TP/SL triggered: Cancelling all active orders...")
            active_order_ids_to_cancel = list(self.active_orders.keys())
            for order_id in active_order_ids_to_cancel:
                order_obj = self.active_orders.get(order_id)
                if order_obj and not order_obj.is_finished():
                    try:
                        self.api.cancel_order(order_obj)
                        self.logger.info(f"Cancelled order {order_id} due to TP/SL.")
                    except Exception as e:
                        self.logger.error(f"Error cancelling order {order_id} for TP/SL: {e}")

            # Manual closing logic
            position = self.api.get_position(self.symbol)
            # Ensure we use the most up-to-date net position from the API directly
            net_position = position.pos_long - position.pos_short

            if net_position != 0:
                self.logger.info(f"TP/SL hit. Current net position: {net_position}. Attempting manual close.")
                direction_to_close = "SELL" if net_position > 0 else "BUY"
                volume_to_close = abs(net_position)

                self.logger.info(f"Inserting {direction_to_close} order for {volume_to_close} lots of {self.symbol} at market price to close position.")
                closing_order = self.api.insert_order(
                    symbol=self.symbol,
                    direction=direction_to_close,
                    offset="CLOSE",
                    volume=volume_to_close,
                    price_type="ANY"
                )

                self.logger.info(f"Waiting for closing order {closing_order.order_id} to fill and position to be zero...")
                start_time = time.time()
                timeout_seconds = 60

                # Loop until position is zero or timeout
                pos_object = self.api.get_position(self.symbol) # Get initial position object
                while (pos_object.pos_long - pos_object.pos_short) != 0:
                    if time.time() - start_time > timeout_seconds:
                        self.logger.warning(f"Timeout waiting for position to close after {timeout_seconds}s. Position might still be open: {pos_object.pos_long - pos_object.pos_short}")
                        break
                    if closing_order.is_error():
                        self.logger.error(f"Closing order {closing_order.order_id} encountered an error: {closing_order.status_msg}")
                        break
                    if closing_order.is_finished() and closing_order.volume_left > 0:
                        self.logger.warning(f"Closing order {closing_order.order_id} finished but not fully filled ({closing_order.volume_traded}/{closing_order.volume_orign}).")
                        if (pos_object.pos_long - pos_object.pos_short) != 0:
                             self.logger.warning(f"Position is still non-zero: {pos_object.pos_long - pos_object.pos_short}. Further manual intervention might be needed.")
                        break

                    self.api.wait_update(deadline=time.time() + 1)
                    pos_object = self.api.get_position(self.symbol) # Refresh position object

                current_pos_after_close_attempt = pos_object.pos_long - pos_object.pos_short
                if current_pos_after_close_attempt == 0:
                    self.logger.info("Position successfully closed.")
                else:
                    self.logger.warning(f"Position not fully closed. Current position: {current_pos_after_close_attempt}")

                self.current_position = current_pos_after_close_attempt
            else:
                self.logger.info("TP/SL hit, but no net position to close according to self.current_position (or just updated from API).")

            self.logger.info("Strategy stopping due to TP/SL.")
            self.strategy_active = False
            self.grids = []
            self.active_orders.clear()

            if self.data_handler_callback:
                self._send_data_to_handler()


    def run(self):
        """
        The main loop of the strategy.
        Can be controlled by self.step_event for single-step execution.
        """
        self.logger.info(f"Starting strategy run for {self.symbol}...")
        if not self.strategy_active: # Check if __init__ already deactivated strategy
            self.logger.warning("Strategy run called but strategy is inactive (possibly due to init errors).")
            if self.data_handler_callback: self._send_data_to_handler()
            return

        if not self.grids: # Generate initial grids if not already present
            self.generate_grids()
            if not self.strategy_active:
                self.logger.error("Strategy cannot run as grids were not generated or strategy became inactive.")
                if self.data_handler_callback: self._send_data_to_handler()
                return

        self.logger.info("Strategy run loop started. Waiting for updates or step events.")
        while self.strategy_active:
            if self.step_event:
                self.logger.debug("Strategy waiting for step_event...")
                self.step_event.wait()
                if not self.strategy_active: break
                self.step_event.clear()
                self.logger.debug("Step event received, processing one update cycle.")

            # Determine deadline for wait_update
            deadline = None
            if self.step_event and len(self.klines) > 0: # Use self.klines which is DataFrame
                # Ensure klines is not empty and has 'datetime'
                # TQSDK klines datetime are nanoseconds (int)
                # Using time.time() + 300 for a 5-minute deadline in step mode is simpler if klines might be empty
                deadline = time.time() + 300 # 5 minute deadline for step mode

            has_update = self.api.wait_update(deadline=deadline)

            if not has_update and self.step_event:
                self.logger.info("wait_update timed out in step mode. Still sending data.")
            elif not has_update and not self.api.is_backtest():
                 self.logger.warning("Warning: wait_update timed out in live mode.")

            if not self.strategy_active: break

            # Update current position from API
            position_obj = self.api.get_position(self.symbol)
            self.current_position = position_obj.pos_long - position_obj.pos_short

            # 1. Check for TP/SL
            if self.current_position != 0: # Only check if there's an actual position
                self.check_profit_loss()
                if not self.strategy_active: break

            # 2. Place new orders
            self.place_orders() # This method also checks self.strategy_active

            # 3. Check status of active orders
            for order_id in list(self.active_orders.keys()):
                order = self.active_orders.get(order_id)
                if order and self.api.is_changing(order):
                    self.update_order_status(order)

            # 4. Send data to UI
            if self.data_handler_callback:
                self._send_data_to_handler()

            # Check for backtest end condition
            # This check should be robust against empty klines or non-backtest scenarios
            if self.api.is_backtest():
                # Get current simulated time. api.get_kline_serial with duration 0 gives latest kline.
                # If get_kline_serial(self.symbol, 0) is empty, it means no klines yet for this symbol.
                latest_klines = self.api.get_kline_serial(self.symbol, 0)
                if not latest_klines.empty:
                    current_sim_time_ns = latest_klines.iloc[-1]['datetime']
                    # api.backtest._end_dt is a date object. Convert to datetime at end of day for comparison.
                    end_dt_datetime = datetime.datetime.combine(self.api.backtest._end_dt, datetime.time.max)
                    # TQSDK uses nanoseconds for serial datetime
                    end_dt_ns = TqApi.datetime_to_serial(end_dt_datetime)

                    if current_sim_time_ns >= end_dt_ns:
                        self.logger.info("Backtest end time reached based on last kline time vs. backtest end_dt.")
                        self.strategy_active = False
                        # The BacktestFinished exception should ideally handle this,
                        # but this is a manual check.
                        break
                elif self.api.get_api_status() == "FINISHING": # Another way to check if backtest is ending
                    self.logger.info("TQApi status is FINISHING, likely end of backtest.")
                    self.strategy_active = False
                    break


        self.logger.info(f"Strategy run for {self.symbol} has finished or been stopped.")
        if self.data_handler_callback:
            self._send_data_to_handler()


if __name__ == '__main__':
    # This example is now less relevant as strategy is meant to be run by webapp.
    # For direct testing, you'd need to mock the API or run a simple backtest.
    print("GridStrategy class defined. For execution, use within the Flask web application or a dedicated backtesting script.")

    # Example of how one might run it directly for testing (simplified):
    # from tqsdk import TqBacktest, TqAuth
    # import datetime
    # auth = TqAuth("YOUR_ACCOUNT", "YOUR_PASSWORD")
    # api = TqApi(auth=auth,
    #             backtest=TqBacktest(start_dt=datetime.date(2023,1,10), end_dt=datetime.date(2023,1,11)))
    #
    # def simple_test_data_handler(data):
    #     print(f"Data at {data['timestamp']}: Pos: {data['position']['current_position']}, Grids: {len(data['grid_lines'])}, Kline O: {data['klines'][-1]['open'] if data['klines'] else 'N/A'}")

    # strategy_test_event = threading.Event() # Mock event for testing
    # strategy = GridStrategy(api=api, symbol="SHFE.rb2310", grid_spacing=10, order_volume=1, total_volume=10,
    #                         take_profit_price=4200, stop_loss_price=3800,
    #                         data_handler_callback=simple_test_data_handler,
    #                         step_event=None) # Set to None to run continuously for this test
    # try:
    #    strategy.run()
    # except Exception as e:
    #    print(f"Error during direct test run: {e}")
    # finally:
    #    api.close()
    pass
