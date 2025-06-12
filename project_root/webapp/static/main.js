document.addEventListener('DOMContentLoaded', function() {
    const paramsForm = document.getElementById('paramsForm');
    const startBacktestBtn = document.getElementById('startBacktestBtn');
    const pauseBacktestBtn = document.getElementById('pauseBacktestBtn');
    const nextStepBtn = document.getElementById('nextStepBtn');
    const stopBacktestBtn = document.getElementById('stopBacktestBtn'); // Added
    const chartContainer = document.getElementById('chartContainer');
    const statusMessages = document.getElementById('statusMessages');
    const positionInfo = document.getElementById('positionInfo');
    const pnlInfo = document.getElementById('pnlInfo');
    const backtestReportDiv = document.getElementById('backtestReportDiv'); // Assuming a div for the report

    let chartInitialized = false;
    let latestSymbol = ""; // To store the symbol for display purposes

    // Function to update status messages and other info from backend data
    function updateInfoDisplay(data) {
        if (data.status_message) {
            statusMessages.textContent = `Status: ${data.status_message}`;
        }
        // Update position, including symbol
        if (data.position !== undefined) {
            const displaySymbol = latestSymbol || "N/A"; // Use stored symbol
            positionInfo.textContent = `Position (${displaySymbol}): ${data.position}`;
        }
        // Update balance or PnL
        if (data.balance !== undefined && data.balance !== null) {
            pnlInfo.textContent = `Balance: ${data.balance.toFixed(2)}`;
        } else if (data.pnl) { // Fallback for PnL if balance is not primary
             pnlInfo.textContent = `PnL: ${data.pnl}`; // This was a placeholder, actual PnL needs calculation or comes from summary
        }
        // Append latest data timestamp if available
         if (data.latest_time && !data.backtest_finished) { // Don't add timestamp if backtest finished to avoid clutter
            statusMessages.textContent += ` (Data as of: ${data.latest_time})`;
        }

        // Handle display of backtest summary
        if (data.backtest_finished && data.backtest_summary) {
            let summaryHtml = '<h3>回测结果统计:</h3><ul>';
            for (const [key, value] of Object.entries(data.backtest_summary)) {
                // For objects/arrays in summary, pretty print; otherwise, direct value
                let displayValue = value;
                if (typeof value === 'object' && value !== null) {
                    // Basic pretty print for objects, could be more sophisticated
                    displayValue = '<pre>' + JSON.stringify(value, null, 2) + '</pre>';
                     summaryHtml += `<li><strong>${key}:</strong> ${displayValue}</li>`;
                } else {
                     summaryHtml += `<li><strong>${key}:</strong> ${value}</li>`;
                }
            }
            summaryHtml += '</ul>';
            if (data.trade_log) { // Display trade log if available
                 summaryHtml += '<h3>交易记录:</h3><pre>' + data.trade_log + '</pre>';
            }
            backtestReportDiv.innerHTML = summaryHtml;

            // Disable controls as backtest is over
            startBacktestBtn.disabled = false; // Allow starting a new one
            pauseBacktestBtn.disabled = true;
            nextStepBtn.disabled = true;
            stopBacktestBtn.disabled = true;
            statusMessages.textContent = `Status: ${data.status_message || "Backtest Finished."}`;
        } else {
             backtestReportDiv.innerHTML = ""; // Clear summary if not finished or no summary
        }
    }
    // Function to fetch and update chart
    async function fetchAndUpdateChart() {
        try {
            const response = await fetch('/get_chart_data');
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json(); // Contains graph_json and other info

            if (data.graph_json) {
                const graphData = JSON.parse(data.graph_json);
                if (chartInitialized) {
                    Plotly.react(chartContainer, graphData.data, graphData.layout);
                } else {
                    Plotly.newPlot(chartContainer, graphData.data, graphData.layout);
                    chartInitialized = true;
                }
            } else if (data.status === "no_data" && !chartInitialized) {
                 Plotly.newPlot(chartContainer, [], {title: 'Backtest Chart - No Data Yet', paper_bgcolor: '#f4f4f4'},{responsive: true});
                 chartInitialized = true;
            }
            // Update other info fields from the same backend response
            updateInfoDisplay(data);

        } catch (error) {
            console.error('Error fetching chart data:', error);
            statusMessages.textContent = `Status: Error fetching chart data: ${error.message}`;
            // Potentially update other fields to show error state if needed
            positionInfo.textContent = "Position: Error";
            pnlInfo.textContent = "Balance: Error";
        }
    }

    // Event listener for Start Backtest button
    startBacktestBtn.addEventListener('click', async function() {
        // Enable controls that might have been disabled
        pauseBacktestBtn.disabled = false;
        nextStepBtn.disabled = false;
        stopBacktestBtn.disabled = false;
        backtestReportDiv.innerHTML = ""; // Clear previous report

        const formData = new FormData(paramsForm);
        const params = {};
        formData.forEach((value, key) => {
            const numValue = parseFloat(value); // Convert to number if possible
            params[key] = isNaN(numValue) ? value : numValue;
        });
        latestSymbol = params['symbol']; // Store symbol for later display

        // Update UI to reflect starting state
        statusMessages.textContent = "Status: Starting backtest...";
        positionInfo.textContent = `Position (${latestSymbol}): N/A`;
        pnlInfo.textContent = "Balance: N/A";

        try {
            const response = await fetch('/start_backtest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(params),
            });
            const result = await response.json();
            // Backend's response message will be displayed by fetchAndUpdateChart or updateInfoDisplay
            statusMessages.textContent = `Status: ${result.message || "Backtest initiation processed."}`;
            if (result.status === "success") {
                // Fetch initial data. If backend auto-steps once, this will show it.
                fetchAndUpdateChart();
            }
        } catch (error) {
            console.error('Error starting backtest:', error);
            statusMessages.textContent = `Status: Error starting backtest: ${error.message}`;
        }
    });

    // Event listener for Pause Backtest button
    pauseBacktestBtn.addEventListener('click', async function() {
        statusMessages.textContent = "Status: Pausing backtest (Note: step mode is implicitly paused)...";
        try {
            const response = await fetch('/control_backtest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command: 'pause' }),
            });
            const result = await response.json();
            statusMessages.textContent = `Status: ${result.message || "Pause command processed."}`;
        } catch (error) {
            console.error('Error pausing backtest:', error);
            statusMessages.textContent = `Status: Error pausing backtest: ${error.message}`;
        }
    });

    // Event listener for Next Step button
    nextStepBtn.addEventListener('click', async function() {
        statusMessages.textContent = "Status: Processing next step...";
        try {
            const response = await fetch('/control_backtest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command: 'next_step' }),
            });
            const result = await response.json();
            // The main update will come from fetchAndUpdateChart
            statusMessages.textContent = `Status: ${result.message || "Next step command sent."}`;
            if (result.status === "success") {
                fetchAndUpdateChart(); // Update chart after processing step
            }
        } catch (error) {
            console.error('Error processing next step:', error);
            statusMessages.textContent = `Status: Error processing next step: ${error.message}`;
        }
    });

    // Event listener for Stop Backtest button (New)
    stopBacktestBtn.addEventListener('click', async function() {
        statusMessages.textContent = "Status: Stopping backtest...";
        try {
            const response = await fetch('/control_backtest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command: 'stop' }),
            });
            const result = await response.json();
            statusMessages.textContent = `Status: ${result.message || "Stop command processed."}`;
            // Update UI to reflect stopped state
            positionInfo.textContent = `Position (${latestSymbol}): N/A (Stopped)`;
            pnlInfo.textContent = "Balance: N/A (Stopped)";
            // Optionally, could clear or reset the chart:
            // Plotly.purge(chartContainer);
            // chartInitialized = false;
            // Plotly.newPlot(chartContainer, [], {title: 'Backtest Chart - Stopped', paper_bgcolor: '#f4f4f4'},{responsive: true});
        } catch (error) {
            console.error('Error stopping backtest:', error);
            statusMessages.textContent = `Status: Error stopping backtest: ${error.message}`;
        }
    });

    // Initial chart and info display setup on page load
    Plotly.newPlot(chartContainer, [], {title: 'Backtest Chart - Ready to Start', paper_bgcolor: '#f4f4f4'}, {responsive: true});
    chartInitialized = true;
    statusMessages.textContent = "Status: Ready to start backtest.";
    positionInfo.textContent = "Position: N/A";
    pnlInfo.textContent = "Balance: N/A";
    backtestReportDiv.innerHTML = ""; // Ensure it's clear initially
});
