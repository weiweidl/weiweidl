from flask import Flask, render_template, request, jsonify
import plotly
import plotly.graph_objs as go
import json
import pandas as pd
import datetime
import threading # For running strategy in background and for step event
import os # For TQSDK auth (example)

from flask import Flask, render_template, request, jsonify
from tqsdk import TqApi, TqAuth, TqBacktest, TqSim, BacktestFinished
from ..strategy.grid_strategy import GridStrategy
import logging # For app-level logging if needed, or use app.logger

app = Flask(__name__)
# Configure Flask logger if not using basicConfig elsewhere
if not app.debug: # Example: More verbose logging for debug mode might be set elsewhere
    app.logger.setLevel(logging.INFO)
else:
    app.logger.setLevel(logging.DEBUG)

# --- Global variables for TQSDK and Strategy state ---
tq_api_instance = None # Renamed for clarity
tq_auth_instance = None # Renamed for clarity
backtest_engine_instance = None # Renamed for clarity
strategy_instance_obj = None # Renamed for clarity
strategy_thread_obj = None # Renamed for clarity
backtest_step_event_obj = threading.Event() # Renamed for clarity
sim_account_instance = None # Added for TqSim

# Lock for thread-safe updates to latest_chart_data
data_lock = threading.Lock()
latest_chart_data = {
    "klines": [],
    "active_orders": [],
    "grid_lines": [],
    "trades": [],
    "position": {"current_position": 0, "symbol": ""},
    "account_balance": None,
    "timestamp": None,
    "status_message": "Idle",
    "backtest_summary": None, # For storing stats
    "backtest_finished": False # Flag
}
# --- End Globals ---

# --- TQSDK Auth Configuration ---
# Example: Load from environment variables for better security
# For a real deployment, consider more robust configuration management.
SIM_ACCOUNT = os.environ.get("TQ_ACCOUNT", "YOUR_SIM_ACCOUNT") # Replace with your SimNow or other account
SIM_PASSWORD = os.environ.get("TQ_PASSWORD", "YOUR_SIM_PASSWORD")
# Ensure users are warned if using default/placeholder credentials.
if SIM_ACCOUNT == "YOUR_SIM_ACCOUNT":
    print("WARNING: Using placeholder TQSDK account. Please set TQ_ACCOUNT and TQ_PASSWORD environment variables.")

tq_auth_instance = TqAuth(SIM_ACCOUNT, SIM_PASSWORD)


def handle_strategy_data_callback(data_from_strategy):
    """Callback function passed to GridStrategy to receive data updates."""
    global latest_chart_data
    with data_lock:
        if data_from_strategy.get('type') == 'backtest_summary':
            latest_chart_data['backtest_summary'] = data_from_strategy.get('tqsdk_stat')
            latest_chart_data['trade_log'] = data_from_strategy.get('trade_log') # Potentially large
            latest_chart_data['backtest_finished'] = True
            latest_chart_data['status_message'] = data_from_strategy.get('message', "Backtest finished.")
            print(f"Received backtest summary: {latest_chart_data['backtest_summary']}")
        else:
            # Normal data update
            latest_chart_data["klines"] = data_from_strategy.get("klines", latest_chart_data["klines"])
            latest_chart_data["active_orders"] = data_from_strategy.get("active_orders", latest_chart_data["active_orders"])
            latest_chart_data["grid_lines"] = data_from_strategy.get("grid_lines", latest_chart_data["grid_lines"])
            latest_chart_data["position"] = data_from_strategy.get("position", latest_chart_data["position"])
            latest_chart_data["account_balance"] = data_from_strategy.get("account_balance", latest_chart_data["account_balance"])
            latest_chart_data["timestamp"] = data_from_strategy.get("timestamp", latest_chart_data["timestamp"])
            latest_chart_data["status_message"] = f"Data updated at {latest_chart_data['timestamp']}"
            # Do not overwrite summary if it's just a regular update
            # latest_chart_data['backtest_summary'] = None
            # latest_chart_data['backtest_finished'] = False


def run_strategy_thread(current_tq_api, strategy_to_run, data_cb, step_event_to_use, current_sim_account):
    """Target function for the strategy execution thread."""
    global latest_chart_data # For setting error status directly if needed
    try:
        print("Strategy thread started.")
        strategy_to_run.run() # This will now block until strategy stops or backtest finishes
        print("Strategy run method completed.")
    except BacktestFinished:
        print("BacktestFinished exception caught in strategy thread.")
        if current_sim_account:
            summary_data = {
                "type": "backtest_summary",
                "message": "回测已自然结束 (BacktestFinished).",
                "tqsdk_stat": current_sim_account.tqsdk_stat,
                "trade_log": current_sim_account.trade_log_to_str() # Use string representation for easier JSON
            }
            data_cb(summary_data)
        else:
            data_cb({"type": "backtest_summary", "message": "回测结束但无法获取统计数据 (sim_account missing)."})
    except Exception as e:
        print(f"Exception in strategy thread: {e}")
        with data_lock:
            latest_chart_data["status_message"] = f"Strategy Error: {e}"
            latest_chart_data["backtest_finished"] = True # Consider it finished on error
            latest_chart_data["backtest_summary"] = {"error": str(e)}

    finally:
        if current_tq_api and not current_tq_api.closed:
            try:
                current_tq_api.close()
                print("TQApi closed in strategy thread finally block.")
            except Exception as e_close:
                print(f"Error closing TQApi in strategy thread: {e_close}")
        # Notify frontend that backtest is conclusively over, if not already done by BacktestFinished
        with data_lock:
            if not latest_chart_data.get("backtest_finished"): # If not set by BacktestFinished handler
                latest_chart_data["backtest_finished"] = True
                latest_chart_data["status_message"] = latest_chart_data.get("status_message", "Strategy thread ended.")
                if not latest_chart_data.get("backtest_summary"): # if no summary yet
                     latest_chart_data['backtest_summary'] = {"status": latest_chart_data["status_message"]}


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_backtest', methods=['POST'])
def start_backtest():
    # Use new global var names
    global tq_api_instance, backtest_engine_instance, strategy_instance_obj, \
           strategy_thread_obj, latest_chart_data, backtest_step_event_obj, sim_account_instance

    if strategy_thread_obj and strategy_thread_obj.is_alive():
        return jsonify({"status": "error", "message": "A backtest is already running. Please stop it first."}), 400

    params = request.json
    app.logger.info(f"Received strategy parameters for backtest: {params}")

    # Reset data and event for a new backtest session
    with data_lock:
        latest_chart_data = {
            "klines": [], "active_orders": [], "grid_lines": [], "trades": [],
            "position": {"current_position": 0, "symbol": params.get("symbol")},
            "account_balance": None, "timestamp": None, "status_message": "Initializing...",
            "backtest_summary": None, "backtest_finished": False
        }
    backtest_step_event_obj.clear()

    # Ensure any old API instance is closed before attempting to create a new one
    # This is important if a previous run failed before cleanup.
    global tq_api_instance, strategy_instance_obj, strategy_thread_obj, sim_account_instance, backtest_engine_instance
    if tq_api_instance and not tq_api_instance.closed:
        try:
            tq_api_instance.close()
            app.logger.info("Closed pre-existing TQApi instance before new backtest.")
        except Exception as e_close_old:
            app.logger.error(f"Error closing pre-existing TQApi: {e_close_old}")

    # Reset global instances that will be re-initialized
    tq_api_instance = None
    strategy_instance_obj = None
    strategy_thread_obj = None
    sim_account_instance = None
    backtest_engine_instance = None

    try:
        end_dt = datetime.date.today()
        # Allow configurable duration, default to 10 days
        backtest_days = int(params.get("backtestDays", 10))
        if backtest_days <= 0:
            app.logger.warning(f"Invalid backtestDays: {backtest_days}. Using default 10 days.")
            backtest_days = 10
        start_dt = end_dt - datetime.timedelta(days=backtest_days)

        # Handle initialBalance
        initial_balance_str = params.get('initialBalance', '10000000') # Get as string from JSON
        try:
            initial_balance = float(initial_balance_str)
            if initial_balance <= 0:
                app.logger.warning(f"Invalid initial balance: {initial_balance_str}. Using default 10,000,000.")
                initial_balance = 10000000.0
        except ValueError:
            app.logger.warning(f"Cannot convert initial balance '{initial_balance_str}' to float. Using default 10,000,000.")
            initial_balance = 10000000.0

        app.logger.info(f"Backtest Period: {start_dt} to {end_dt}. Initial Balance: {initial_balance}")

        backtest_engine_instance = TqBacktest(start_dt=start_dt, end_dt=end_dt)
        sim_account_instance = TqSim(init_balance=initial_balance) # Use validated initial_balance

        app.logger.info("Initializing TqApi...")
        tq_api_instance = TqApi(
            account=sim_account_instance,
            backtest=backtest_engine_instance,
            auth=tq_auth_instance
        )
        app.logger.info("TqApi initialized. Initializing GridStrategy...")

        strategy_instance_obj = GridStrategy(
            api=tq_api_instance,
            symbol=str(params.get("symbol")),
            grid_spacing=float(params.get("gridSpacing")),
            order_volume=int(params.get("orderVolume")),
            total_volume=int(params.get("totalVolume")),
            take_profit_price=float(params.get("takeProfitPrice")),
            stop_loss_price=float(params.get("stopLossPrice")),
            data_handler_callback=handle_strategy_data_callback,
            step_event=backtest_step_event_obj
        )
        app.logger.info("GridStrategy initialized.")

        if not strategy_instance_obj.strategy_active:
            error_msg = "策略未能激活，可能因为合约已过期、无效或行情服务异常。"
            app.logger.error(error_msg + f" (Symbol: {params.get('symbol')})")
            with data_lock:
                latest_chart_data['status_message'] = error_msg
                latest_chart_data['backtest_finished'] = True
            if tq_api_instance and not tq_api_instance.closed:
                tq_api_instance.close()
            tq_api_instance = None
            strategy_instance_obj = None
            sim_account_instance = None
            return jsonify({"status": "error", "message": error_msg})

        app.logger.info("Starting strategy thread...")
        strategy_thread_obj = threading.Thread(
            target=run_strategy_thread,
            args=(tq_api_instance, strategy_instance_obj, handle_strategy_data_callback, backtest_step_event_obj, sim_account_instance),
            daemon=True
        )
        strategy_thread_obj.start()

        with data_lock:
            latest_chart_data["status_message"] = "Backtest started. Waiting for first step or data."
        app.logger.info("Backtest successfully started in background.")
        return jsonify({"status": "success", "message": "Backtest started in background."})

    except Exception as e:
        error_msg_user = "回测启动失败：无法初始化交易接口或策略。请检查参数、网络连接或TQSDK账户配置。"
        app.logger.exception("Error during TQSDK API init or strategy instantiation:") # Logs full traceback

        with data_lock:
            latest_chart_data['status_message'] = error_msg_user + f" 技术细节: {str(e)}"
            latest_chart_data['backtest_finished'] = True
            latest_chart_data['backtest_summary'] = {"error": str(e)} # Store technical error for summary

        if tq_api_instance and not tq_api_instance.closed:
            try:
                tq_api_instance.close()
            except Exception as close_e:
                app.logger.error(f"Error closing TqApi during exception handling: {close_e}")

        # Reset globals to safe state
        tq_api_instance = None
        strategy_instance_obj = None
        strategy_thread_obj = None
        sim_account_instance = None
        backtest_engine_instance = None

        return jsonify({"status": "error", "message": error_msg_user, "details": str(e)})


@app.route('/control_backtest', methods=['POST'])
def control_backtest():
    global tq_api_instance, strategy_instance_obj, backtest_step_event_obj, strategy_thread_obj, sim_account_instance
    command = request.json.get('command')
    print(f"Received control command: {command}")

    # Check if backtest has already finished naturally
    with data_lock:
        if latest_chart_data.get("backtest_finished", False) and command != "stop": # Allow stop to be called even if finished to ensure cleanup
             return jsonify({"status": "info", "message": "Backtest has already finished."})


    if not strategy_instance_obj and command != "stop": # strategy_thread might be checked too
         return jsonify({"status": "error", "message": "No active backtest to control."}), 400
    if strategy_thread_obj and not strategy_thread_obj.is_alive() and command not in ["stop", "get_chart_data"]: # if thread died
         # If thread is dead, but not due to normal completion handled by run_strategy_thread's finally block
         if tq_api_instance and not tq_api_instance.closed:
             try: tq_api_instance.close(); print("Cleaned up API for dead thread.")
             except: pass
         return jsonify({"status": "error", "message": "Backtest thread is not alive. Consider stopping to reset."}), 400


    message = f"Command '{command}' received."
    status = "success"

    if command == "next_step":
        if strategy_instance_obj and strategy_instance_obj.strategy_active:
            backtest_step_event_obj.set()
            message = "Next step signaled."
            with data_lock: latest_chart_data["status_message"] = "Processing next step..."
        else:
            message = "Cannot perform next step, strategy not active or not initialized, or backtest finished."
            status = "error"

    elif command == "pause":
        message = "Pause command received (in step mode, stop clicking 'Next Step')."
        with data_lock: latest_chart_data["status_message"] = "Paused; awaiting 'Next Step'."

    elif command == "stop":
        print("Stop command received. Initiating stop sequence.")
        if strategy_instance_obj:
            strategy_instance_obj.strategy_active = False
        if backtest_step_event_obj:
            backtest_step_event_obj.set()

        if strategy_thread_obj and strategy_thread_obj.is_alive():
            print("Waiting for strategy thread to join...")
            strategy_thread_obj.join(timeout=10.0) # Increased timeout
            if strategy_thread_obj.is_alive():
                print("Warning: Strategy thread did not terminate cleanly after 10s.")
            else:
                print("Strategy thread joined.")

        # API close is now handled in run_strategy_thread's finally block
        # but as a fallback:
        if tq_api_instance and not tq_api_instance.closed:
            try:
                tq_api_instance.close()
                print("TQApi connection closed on stop command (fallback).")
            except Exception as e:
                print(f"Error closing TQApi on stop (fallback): {e}")

        tq_api_instance = None
        strategy_instance_obj = None
        strategy_thread_obj = None
        sim_account_instance = None # Clear sim account
        message = "Backtest stopped and resources attempt to be cleaned up."
        with data_lock:
            latest_chart_data["status_message"] = "Backtest stopped by user."
            latest_chart_data["backtest_finished"] = True # Mark as finished
            if not latest_chart_data.get("backtest_summary"): # If no summary from natural finish
                latest_chart_data["backtest_summary"] = {"status": "Manually stopped"}


    return jsonify({"status": status, "message": message})

@app.route('/get_chart_data', methods=['GET'])
def get_chart_data():
    with data_lock:
        # Make a copy to avoid issues if data is updated while processing
        data_for_chart = latest_chart_data.copy()

    if not data_for_chart.get("klines") and not data_for_chart.get("backtest_finished"):
        return jsonify({
            "status": "no_data",
            "message": data_for_chart.get("status_message", "No kline data available yet."),
            "graph_json": json.dumps({"data": [], "layout": {"title": "Backtest Chart - Waiting for Data"}}, cls=plotly.utils.PlotlyJSONEncoder),
            "backtest_finished": data_for_chart.get("backtest_finished", False), # send flag
            "backtest_summary": data_for_chart.get("backtest_summary") # send summary if any
        })

    # Create Candlestick trace
    klines_df = pd.DataFrame(data_for_chart["klines"])
    candlestick_trace = {}
    if not klines_df.empty:
        candlestick_trace = go.Candlestick(
            x=pd.to_datetime(klines_df['datetime']),
            open=klines_df['open'],
            high=klines_df['high'],
            low=klines_df['low'],
            close=klines_df['close'],
            name='Candlesticks'
        )

    traces = [candlestick_trace] if candlestick_trace else []
    shapes = [] # For grid lines and order lines

    # Add Grid Lines
    min_kline_time = pd.to_datetime(klines_df['datetime'].iloc[0]) if not klines_df.empty else datetime.datetime.now()
    max_kline_time = pd.to_datetime(klines_df['datetime'].iloc[-1]) if not klines_df.empty else datetime.datetime.now()

    for grid in data_for_chart["grid_lines"]:
        color = "blue" if grid["type"] == "BUY" else "orange"
        dash_style = "solid"
        if grid["status"] == "ACTIVE":
            color = "purple"
            dash_style = "dot"
        elif grid["status"] == "FILLED":
            color = "grey"
            dash_style = "longdashdot"

        shapes.append(go.layout.Shape(
            type="line", x0=min_kline_time, y0=grid["price"], x1=max_kline_time, y1=grid["price"],
            line=dict(color=color, width=1, dash=dash_style), name=f"{grid['type']} Grid {grid['price']}"
        ))

    # Add Active Orders (as lines, similar to grids but maybe different style)
    for order in data_for_chart["active_orders"]:
        color = "green" if order["direction"] == "BUY" else "red"
        shapes.append(go.layout.Shape(
            type="line", x0=min_kline_time, y0=order["price"], x1=max_kline_time, y1=order["price"],
            line=dict(color=color, width=2, dash="dashdot"), name=f"Order {order['order_id']}"
        ))

    # Add Trades (as markers)
    # Assuming 'trades' accumulate in latest_chart_data. This part needs strategy to send fills.
    # For now, this will be empty as _send_data_to_handler doesn't populate individual trades.
    # This needs to be completed once strategy sends individual trade fills.
    trades_df = pd.DataFrame(data_for_chart.get("trades", []))
    if not trades_df.empty:
        buy_trades = trades_df[trades_df['type'] == 'BUY'] # Adjust 'type' if strategy sends 'direction'
        sell_trades = trades_df[trades_df['type'] == 'SELL']

        if not buy_trades.empty:
            traces.append(go.Scatter(
                x=pd.to_datetime(buy_trades['time']), y=buy_trades['price'], mode='markers',
                marker=dict(color='darkgreen', size=10, symbol='triangle-up'), name='Buy Trades'
            ))
        if not sell_trades.empty:
            traces.append(go.Scatter(
                x=pd.to_datetime(sell_trades['time']), y=sell_trades['price'], mode='markers',
                marker=dict(color='darkred', size=10, symbol='triangle-down'), name='Sell Trades'
            ))

    layout = go.Layout(
        title=f"Backtest: {data_for_chart['position']['symbol']} (Pos: {data_for_chart['position']['current_position']}) Bal: {data_for_chart['account_balance']:.2f if data_for_chart['account_balance'] else 'N/A'}",
        xaxis={'title': 'Time', 'type': 'date', 'rangeslider': {'visible': False}},
        yaxis={'title': 'Price', 'autorange': True}, # autoscale y-axis
        shapes=shapes,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )

    fig = go.Figure(data=traces, layout=layout)
    graph_json = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

    return jsonify({
        "status": "success",
        "graph_json": graph_json,
        "position": data_for_chart.get("position", {}).get("current_position", 0),
        "balance": data_for_chart.get("account_balance"),
        "pnl": "N/A",
        "latest_time": data_for_chart.get("timestamp"),
        "status_message": data_for_chart.get("status_message", "Chart updated."),
        "backtest_summary": data_for_chart.get("backtest_summary"),
        "backtest_finished": data_for_chart.get("backtest_finished", False)
    })


if __name__ == '__main__':
    print("Starting Flask app for TQSDK Web UI...")
    app.logger.info("Starting Flask app for TQSDK Web UI...")
    app.logger.info(f"TQSDK Account for Backtesting (from env or default): {SIM_ACCOUNT}")
    if SIM_ACCOUNT == "YOUR_SIM_ACCOUNT" or SIM_PASSWORD == "YOUR_SIM_PASSWORD":
        app.logger.warning("IMPORTANT: TQSDK account is using default placeholders.")
        app.logger.warning("Please set TQ_ACCOUNT and TQ_PASSWORD environment variables for proper operation.")
    app.run(debug=True, port=5001, use_reloader=False)
