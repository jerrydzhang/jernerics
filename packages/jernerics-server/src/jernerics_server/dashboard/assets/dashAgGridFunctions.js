// dash-ag-grid evaluates {"function": "..."} props as JS expressions
// whose scope includes everything on window.dashAgGridFunctions (see
// dash.plotly.com/dash-ag-grid/javascript-and-the-grid). Row ids are
// plain expression strings (workspace._SWEEP_ROW_ID etc.) so the
// component's own selectedRows handling can rewrite them — function
// objects break it — and no registered helpers remain.
