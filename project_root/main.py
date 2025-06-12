import os
import sys

# Add project_root to Python path to allow relative imports like from webapp.app import app
project_root_path = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root_path)

from webapp.app import app, SIM_ACCOUNT, SIM_PASSWORD # Import the Flask app instance

if __name__ == '__main__':
    print("Starting Quant Web Application...")

    # Output TQSDK account being used (reminder for user)
    print(f"TQSDK Account for Backtesting (from env or default): {SIM_ACCOUNT}")
    if SIM_ACCOUNT == "YOUR_SIM_ACCOUNT" or SIM_PASSWORD == "YOUR_SIM_PASSWORD":
        print("--------------------------------------------------------------------------------")
        print("IMPORTANT: TQSDK account is using default/placeholder credentials.")
        print("The backtest might not run correctly or use an unintended account.")
        print("Please set the TQ_ACCOUNT and TQ_PASSWORD environment variables,")
        print("or modify the tq_auth initialization in webapp/app.py directly (not recommended for shared code).")
        print("--------------------------------------------------------------------------------")

    # When running Flask directly like this, it's often in debug mode.
    # use_reloader=False is important as TQSDK runs in background threads
    # and the reloader can cause issues with TQSDK's global state or API instances.
    app.run(debug=True, port=5001, use_reloader=False)
