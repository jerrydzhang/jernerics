// dash-ag-grid evaluates {"function": "..."} props as JS expressions
// whose scope includes everything on window.dashAgGridFunctions (see
// dash.plotly.com/dash-ag-grid/javascript-and-the-grid). Registering the
// row-id functions here — instead of inline JS strings — is the only form
// the grid evaluates without dangerously_allow_code.
var dagfuncs = window.dashAgGridFunctions = window.dashAgGridFunctions || {};

dagfuncs.jernericsArtifactRowId = function (params) {
    return params.data.artifact_id;
};

dagfuncs.jernericsSweepRowId = function (params) {
    return params.data.sweep_id;
};

dagfuncs.jernericsTrialRowId = function (params) {
    return params.data.root || params.data.trial_id;
};

dagfuncs.jernericsColorSwatch = function (params) {
    const span = document.createElement("span");
    span.className = "trace-swatch";
    span.title = params.value || "";
    span.style.background = params.value || "#7f7f7f";
    return span;
};
