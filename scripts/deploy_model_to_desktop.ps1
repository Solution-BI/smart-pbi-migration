<#
.SYNOPSIS
  Create the semantic model from powerbi_build_spec.json in the open Power BI Desktop file.

.DESCRIPTION
  Connects to the local model engine of the running Power BI Desktop instance and adds the
  Power Query parameters, tables (M partitions), columns and measures from the build spec.
  It does NOT refresh data (the source credentials are entered in Power BI Desktop).
  Stops without changes if any object it would create already exists.

.EXAMPLE
  .\scripts\deploy_model_to_desktop.ps1 -DashboardId 9D1AA7CA0A4728650BE279A1DC273F93
  .\scripts\deploy_model_to_desktop.ps1 -DashboardId 9D1A... -Server "xy12345.ap-southeast-2.snowflakecomputing.com" -Warehouse "BI_WH" -Database "NEW_FAIRTHORNE"
#>
param(
    [Parameter(Mandatory = $true)][string]$DashboardId,
    [int]$Port = 0,
    [string]$Server,
    [string]$Warehouse,
    [string]$Database
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'PbiSpecModel.ps1')

$specPath = Join-Path $PSScriptRoot "..\2_work\$DashboardId\2_transform\powerbi_build_spec.json"
$spec = Get-Content $specPath -Raw -Encoding UTF8 | ConvertFrom-Json
$model = $spec.semanticModel

# --- Locate Power BI Desktop and its model engine ---------------------------------------
$pbi = Get-Process PBIDesktop -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pbi) { throw "Power BI Desktop is not running. Open the target .pbix first." }
$engine = Get-CimInstance Win32_Process -Filter "Name = 'msmdsrv.exe'" |
    Where-Object { $_.ParentProcessId -eq $pbi.Id } | Select-Object -First 1
if (-not $engine) { $engine = Get-CimInstance Win32_Process -Filter "Name = 'msmdsrv.exe'" | Select-Object -First 1 }
if ($Port -eq 0) {
    $Port = (Get-NetTCPConnection -OwningProcess $engine.ProcessId -State Listen |
        Select-Object -First 1).LocalPort
}
Add-Type -Path (Get-PbiTabularDll)
Write-Host "Power BI Desktop: '$($pbi.MainWindowTitle)'  engine port $Port"

$tom = New-Object Microsoft.AnalysisServices.Tabular.Server
$tom.Connect("Data Source=localhost:$Port")
try {
    $m = $tom.Databases[0].Model

    # --- Safety check: never overwrite existing objects ---------------------------------
    $clashes = @()
    foreach ($p in $model.parameters) { if ($m.Expressions.Find($p.name)) { $clashes += "parameter $($p.name)" } }
    foreach ($t in $model.tables) {
        if ($m.Tables.Find($t.name)) { $clashes += "table $($t.name)" }
        foreach ($ms in $t.measures) {
            foreach ($existing in $m.Tables) { if ($existing.Measures.Find($ms.name)) { $clashes += "measure $($ms.name)" } }
        }
    }
    if ($clashes) { throw "Already in the model, nothing changed: $($clashes -join ', ')" }

    Add-SpecObjects -Model $m -Spec $spec -Overrides @{ SourceServer = $Server; SourceWarehouse = $Warehouse; SourceDatabase = $Database }
    $m.SaveChanges() | Out-Null
    Write-Host "Saved to the model. Next: set the parameters in Power BI Desktop and click Refresh."
}
finally {
    $tom.Disconnect()
}
